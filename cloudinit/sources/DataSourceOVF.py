# Copyright (C) 2011 Canonical Ltd.
# Copyright (C) 2012 Hewlett-Packard Development Company, L.P.
# Copyright (C) 2012 Yahoo! Inc.
#
# Author: Scott Moser <scott.moser@canonical.com>
# Author: Juerg Hafliger <juerg.haefliger@hp.com>
# Author: Joshua Harlow <harlowja@yahoo-inc.com>
#
# This file is part of cloud-init. See LICENSE file for license information.

import base64
import os
import re
import time
from xml.dom import minidom

from cloudinit import dmi
from cloudinit import log as logging
from cloudinit import safeyaml
from cloudinit import sources
from cloudinit import subp
from cloudinit import util
from cloudinit.sources.helpers.vmware.imc.config \
    import Config
from cloudinit.sources.helpers.vmware.imc.config_custom_script \
    import PreCustomScript, PostCustomScript
from cloudinit.sources.helpers.vmware.imc.config_file \
    import ConfigFile
from cloudinit.sources.helpers.vmware.imc.config_nic \
    import NicConfigurator
from cloudinit.sources.helpers.vmware.imc.config_passwd \
    import PasswordConfigurator
from cloudinit.sources.helpers.vmware.imc.guestcust_error \
    import GuestCustErrorEnum
from cloudinit.sources.helpers.vmware.imc.guestcust_event \
    import GuestCustEventEnum as GuestCustEvent
from cloudinit.sources.helpers.vmware.imc.guestcust_state \
    import GuestCustStateEnum
from cloudinit.sources.helpers.vmware.imc.guestcust_util import (
    enable_nics,
    get_nics_to_enable,
    set_customization_status,
    get_tools_config,
    set_gc_status
)

LOG = logging.getLogger(__name__)

CONFGROUPNAME_GUESTCUSTOMIZATION = "deployPkg"
GUESTCUSTOMIZATION_ENABLE_CUST_SCRIPTS = "enable-custom-scripts"
VMWARE_IMC_DIR = "/var/run/vmware-imc"


class DataSourceOVF(sources.DataSource):

    dsname = "OVF"

    def __init__(self, sys_cfg, distro, paths):
        sources.DataSource.__init__(self, sys_cfg, distro, paths)
        self.seed = None
        self.seed_dir = os.path.join(paths.seed_dir, 'ovf')
        self.environment = None
        self.cfg = {}
        self.supported_seed_starts = ("/", "file://")
        self.vmware_customization_supported = True
        self._network_config = None
        self._vmware_nics_to_enable = None
        self._vmware_cust_conf = None
        self._vmware_cust_found = False

    def __str__(self):
        root = sources.DataSource.__str__(self)
        return "%s [seed=%s]" % (root, self.seed)

    def _get_data(self):
        found = []
        md = {}
        ud = ""
        vd = ""
        vmwareImcConfigFilePath = None
        nicspath = None

        defaults = {
            "instance-id": "iid-dsovf",
        }

        (seedfile, contents) = get_ovf_env(self.paths.seed_dir)

        system_type = dmi.read_dmi_data("system-product-name")
        if system_type is None:
            LOG.debug("No system-product-name found")

        if seedfile:
            # Found a seed dir
            seed = os.path.join(self.paths.seed_dir, seedfile)
            (md, ud, cfg) = read_ovf_environment(contents)
            self.environment = contents
            found.append(seed)
        elif system_type and 'vmware' in system_type.lower():
            LOG.debug("VMware Virtualization Platform found")
            allow_vmware_cust = False
            allow_raw_data = False
            if not self.vmware_customization_supported:
                LOG.debug("Skipping the check for "
                          "VMware Customization support")
            else:
                allow_vmware_cust = not util.get_cfg_option_bool(
                    self.sys_cfg, "disable_vmware_customization", True)
                allow_raw_data = util.get_cfg_option_bool(
                    self.ds_cfg, "allow_raw_data", True)

            if not (allow_vmware_cust or allow_raw_data):
                LOG.debug(
                    "Customization for VMware platform is disabled.")
            else:
                search_paths = (
                    "/usr/lib/vmware-tools", "/usr/lib64/vmware-tools",
                    "/usr/lib/open-vm-tools", "/usr/lib64/open-vm-tools",
                    "/usr/lib/x86_64-linux-gnu/open-vm-tools",
                    "/usr/lib/aarch64-linux-gnu/open-vm-tools")

                plugin = "libdeployPkgPlugin.so"
                deployPkgPluginPath = None
                for path in search_paths:
                    deployPkgPluginPath = search_file(path, plugin)
                    if deployPkgPluginPath:
                        LOG.debug("Found the customization plugin at %s",
                                  deployPkgPluginPath)
                        break

                if deployPkgPluginPath:
                    # When the VM is powered on, the "VMware Tools" daemon
                    # copies the customization specification file to
                    # /var/run/vmware-imc directory. cloud-init code needs
                    # to search for the file in that directory which indicates
                    # that required metadata and userdata files are now
                    # present.
                    max_wait = get_max_wait_from_cfg(self.ds_cfg)
                    vmwareImcConfigFilePath = util.log_time(
                        logfunc=LOG.debug,
                        msg="waiting for configuration file",
                        func=wait_for_imc_cfg_file,
                        args=("cust.cfg", max_wait))
                else:
                    LOG.debug("Did not find the customization plugin.")

                md_path = None
                if vmwareImcConfigFilePath:
                    imcdirpath = os.path.dirname(vmwareImcConfigFilePath)
                    cf = ConfigFile(vmwareImcConfigFilePath)
                    self._vmware_cust_conf = Config(cf)
                    LOG.debug("Found VMware Customization Config File at %s",
                              vmwareImcConfigFilePath)
                    try:
                        (md_path, ud_path, nicspath) = collect_imc_file_paths(
                            self._vmware_cust_conf)
                    except FileNotFoundError as e:
                        _raise_error_status(
                            "File(s) missing in directory",
                            e,
                            GuestCustEvent.GUESTCUST_EVENT_CUSTOMIZE_FAILED,
                            vmwareImcConfigFilePath,
                            self._vmware_cust_conf)
                    # Don't handle the customization for below 2 cases:
                    # 1. meta data is found, allow_raw_data is False.
                    # 2. no meta data is found, allow_vmware_cust is False.
                    if md_path and not allow_raw_data:
                        LOG.debug(
                            "Customization using raw data is disabled.")
                        # reset vmwareImcConfigFilePath to None to avoid
                        # customization for VMware platform
                        vmwareImcConfigFilePath = None
                    if md_path is None and not allow_vmware_cust:
                        LOG.debug(
                            "Customization using VMware config is disabled.")
                        vmwareImcConfigFilePath = None
                else:
                    LOG.debug("Did not find VMware Customization Config File")

        use_raw_data = bool(vmwareImcConfigFilePath and md_path)
        if use_raw_data:
            set_gc_status(self._vmware_cust_conf, "Started")
            LOG.debug("Start to load cloud-init meta data and user data")
            try:
                (md, ud, cfg, network) = load_cloudinit_data(md_path, ud_path)

                if network:
                    self._network_config = network
                else:
                    self._network_config = (
                        self.distro.generate_fallback_config()
                    )

            except safeyaml.YAMLError as e:
                _raise_error_status(
                    "Error parsing the cloud-init meta data",
                    e,
                    GuestCustErrorEnum.GUESTCUST_ERROR_WRONG_META_FORMAT,
                    vmwareImcConfigFilePath,
                    self._vmware_cust_conf)
            except Exception as e:
                _raise_error_status(
                    "Error loading cloud-init configuration",
                    e,
                    GuestCustEvent.GUESTCUST_EVENT_CUSTOMIZE_FAILED,
                    vmwareImcConfigFilePath,
                    self._vmware_cust_conf)

            self._vmware_cust_found = True
            found.append('vmware-tools')

            util.del_dir(imcdirpath)
            set_customization_status(
                GuestCustStateEnum.GUESTCUST_STATE_DONE,
                GuestCustErrorEnum.GUESTCUST_ERROR_SUCCESS)
            set_gc_status(self._vmware_cust_conf, "Successful")

        elif vmwareImcConfigFilePath:
            # Load configuration from vmware_imc
            self._vmware_nics_to_enable = ""
            try:
                set_gc_status(self._vmware_cust_conf, "Started")

                (md, ud, cfg) = read_vmware_imc(self._vmware_cust_conf)
                self._vmware_nics_to_enable = get_nics_to_enable(nicspath)
                product_marker = self._vmware_cust_conf.marker_id
                hasmarkerfile = check_marker_exists(
                    product_marker, os.path.join(self.paths.cloud_dir, 'data'))
                special_customization = product_marker and not hasmarkerfile
                # Delphix: disable custom user scripts
                customscript = None

                # In case there is a custom script, check whether VMware
                # Tools configuration allow the custom script to run.
                if special_customization and customscript:
                    defVal = "false"
                    if self._vmware_cust_conf.default_run_post_script:
                        LOG.debug(
                            "Set default value to true due to"
                            " customization configuration."
                        )
                        defVal = "true"

                    custScriptConfig = get_tools_config(
                        CONFGROUPNAME_GUESTCUSTOMIZATION,
                        GUESTCUSTOMIZATION_ENABLE_CUST_SCRIPTS,
                        defVal)
                    if custScriptConfig.lower() != "true":
                        # Update the customization status if custom script
                        # is disabled
                        msg = "Custom script is disabled by VM Administrator"
                        LOG.debug(msg)
                        set_customization_status(
                            GuestCustStateEnum.GUESTCUST_STATE_RUNNING,
                            GuestCustErrorEnum.GUESTCUST_ERROR_SCRIPT_DISABLED)
                        raise RuntimeError(msg)

                ccScriptsDir = os.path.join(
                    self.paths.get_cpath("scripts"),
                    "per-instance")
            except Exception as e:
                _raise_error_status(
                    "Error parsing the customization Config File",
                    e,
                    GuestCustEvent.GUESTCUST_EVENT_CUSTOMIZE_FAILED,
                    vmwareImcConfigFilePath,
                    self._vmware_cust_conf)

            if special_customization:
                if customscript:
                    try:
                        precust = PreCustomScript(customscript, imcdirpath)
                        precust.execute()
                    except Exception as e:
                        _raise_error_status(
                            "Error executing pre-customization script",
                            e,
                            GuestCustEvent.GUESTCUST_EVENT_CUSTOMIZE_FAILED,
                            vmwareImcConfigFilePath,
                            self._vmware_cust_conf)

            try:
                LOG.debug("Preparing the Network configuration")
                self._network_config = get_network_config_from_conf(
                    self._vmware_cust_conf,
                    True,
                    True,
                    self.distro.osfamily)
            except Exception as e:
                _raise_error_status(
                    "Error preparing Network Configuration",
                    e,
                    GuestCustEvent.GUESTCUST_EVENT_NETWORK_SETUP_FAILED,
                    vmwareImcConfigFilePath,
                    self._vmware_cust_conf)

            if special_customization:
                LOG.debug("Applying password customization")
                pwdConfigurator = PasswordConfigurator()
                adminpwd = self._vmware_cust_conf.admin_password
                try:
                    resetpwd = self._vmware_cust_conf.reset_password
                    if adminpwd or resetpwd:
                        pwdConfigurator.configure(adminpwd, resetpwd,
                                                  self.distro)
                    else:
                        LOG.debug("Changing password is not needed")
                except Exception as e:
                    _raise_error_status(
                        "Error applying Password Configuration",
                        e,
                        GuestCustEvent.GUESTCUST_EVENT_CUSTOMIZE_FAILED,
                        vmwareImcConfigFilePath,
                        self._vmware_cust_conf)

                if customscript:
                    try:
                        postcust = PostCustomScript(customscript,
                                                    imcdirpath,
                                                    ccScriptsDir)
                        postcust.execute()
                    except Exception as e:
                        _raise_error_status(
                            "Error executing post-customization script",
                            e,
                            GuestCustEvent.GUESTCUST_EVENT_CUSTOMIZE_FAILED,
                            vmwareImcConfigFilePath,
                            self._vmware_cust_conf)

            if product_marker:
                try:
                    setup_marker_files(
                        product_marker,
                        os.path.join(self.paths.cloud_dir, 'data'))
                except Exception as e:
                    _raise_error_status(
                        "Error creating marker files",
                        e,
                        GuestCustEvent.GUESTCUST_EVENT_CUSTOMIZE_FAILED,
                        vmwareImcConfigFilePath,
                        self._vmware_cust_conf)

            self._vmware_cust_found = True
            found.append('vmware-tools')

            # TODO: Need to set the status to DONE only when the
            # customization is done successfully.
            util.del_dir(os.path.dirname(vmwareImcConfigFilePath))
            enable_nics(self._vmware_nics_to_enable)
            set_customization_status(
                GuestCustStateEnum.GUESTCUST_STATE_DONE,
                GuestCustErrorEnum.GUESTCUST_ERROR_SUCCESS)
            set_gc_status(self._vmware_cust_conf, "Successful")

        else:
            np = [('com.vmware.guestInfo', transport_vmware_guestinfo),
                  ('iso', transport_iso9660)]
            name = None
            for name, transfunc in np:
                contents = transfunc()
                if contents:
                    break
            if contents:
                (md, ud, cfg) = read_ovf_environment(contents, True)
                self.environment = contents
                if 'network-config' in md and md['network-config']:
                    self._network_config = md['network-config']
                found.append(name)

        # There was no OVF transports found
        if len(found) == 0:
            return False

        if 'seedfrom' in md and md['seedfrom']:
            seedfrom = md['seedfrom']
            seedfound = False
            for proto in self.supported_seed_starts:
                if seedfrom.startswith(proto):
                    seedfound = proto
                    break
            if not seedfound:
                LOG.debug("Seed from %s not supported by %s",
                          seedfrom, self)
                return False

            (md_seed, ud, vd) = util.read_seeded(seedfrom, timeout=None)
            LOG.debug("Using seeded cache data from %s", seedfrom)

            md = util.mergemanydict([md, md_seed])
            found.append(seedfrom)

        # Now that we have exhausted any other places merge in the defaults
        md = util.mergemanydict([md, defaults])

        self.seed = ",".join(found)
        self.metadata = md
        self.userdata_raw = ud
        self.vendordata_raw = vd
        self.cfg = cfg
        return True

    def _get_subplatform(self):
        system_type = dmi.read_dmi_data("system-product-name").lower()
        if system_type == 'vmware':
            return 'vmware (%s)' % self.seed
        return 'ovf (%s)' % self.seed

    def get_public_ssh_keys(self):
        if 'public-keys' not in self.metadata:
            return []
        pks = self.metadata['public-keys']
        if isinstance(pks, (list)):
            return pks
        else:
            return [pks]

    # The data sources' config_obj is a cloud-config formatted
    # object that came to it from ways other than cloud-config
    # because cloud-config content would be handled elsewhere
    def get_config_obj(self):
        return self.cfg

    # Delphix: Assume instance id never changes if system was initially
    # configured via OVF. This prevents re-configuration if the system is
    # cloned, which would behave incorrectly. Implementing this function is
    # necessary to allow cloud-init to restore OVFDataSource from cache on
    # reboot.
    def check_instance_id(self, sys_cfg):
        return True

    @property
    def network_config(self):
        return self._network_config


class DataSourceOVFNet(DataSourceOVF):
    def __init__(self, sys_cfg, distro, paths):
        DataSourceOVF.__init__(self, sys_cfg, distro, paths)
        self.seed_dir = os.path.join(paths.seed_dir, 'ovf-net')
        self.supported_seed_starts = ("http://", "https://")
        self.vmware_customization_supported = False


def get_max_wait_from_cfg(cfg):
    default_max_wait = 15
    max_wait_cfg_option = 'vmware_cust_file_max_wait'
    max_wait = default_max_wait

    if not cfg:
        return max_wait

    try:
        max_wait = int(cfg.get(max_wait_cfg_option, default_max_wait))
    except ValueError:
        LOG.warning("Failed to get '%s', using %s",
                    max_wait_cfg_option, default_max_wait)

    if max_wait < 0:
        LOG.warning("Invalid value '%s' for '%s', using '%s' instead",
                    max_wait, max_wait_cfg_option, default_max_wait)
        max_wait = default_max_wait

    return max_wait


def wait_for_imc_cfg_file(filename, maxwait=180, naplen=5,
                          dirpath="/var/run/vmware-imc"):
    waited = 0
    if maxwait <= naplen:
        naplen = 1

    while waited < maxwait:
        fileFullPath = os.path.join(dirpath, filename)
        if os.path.isfile(fileFullPath):
            return fileFullPath
        LOG.debug("Waiting for VMware Customization Config File")
        time.sleep(naplen)
        waited += naplen
    return None


def get_network_config_from_conf(config, use_system_devices=True,
                                 configure=False, osfamily=None):
    nicConfigurator = NicConfigurator(config.nics, use_system_devices)
    nics_cfg_list = nicConfigurator.generate(configure, osfamily)

    return get_network_config(nics_cfg_list,
                              config.name_servers,
                              config.dns_suffixes)


def get_network_config(nics=None, nameservers=None, search=None):
    config_list = nics

    if nameservers or search:
        config_list.append({'type': 'nameserver', 'address': nameservers,
                            'search': search})

    return {'version': 1, 'config': config_list}


# This will return a dict with some content
#  meta-data, user-data, some config
def read_vmware_imc(config):
    md = {}
    cfg = {}
    ud = None
    if config.host_name:
        if config.domain_name:
            md['local-hostname'] = config.host_name + "." + config.domain_name
        else:
            md['local-hostname'] = config.host_name

    if config.timezone:
        cfg['timezone'] = config.timezone

    md['instance-id'] = "iid-vmware-imc"
    return (md, ud, cfg)


# This will return a dict with some content
#  meta-data, user-data, some config
def read_ovf_environment(contents, read_network=False):
    props = get_properties(contents)
    md = {}
    cfg = {}
    ud = None
    cfg_props = ['password']
    md_props = ['seedfrom', 'local-hostname', 'public-keys', 'instance-id']
    network_props = ['network-config']
    for (prop, val) in props.items():
        if prop == 'hostname':
            prop = "local-hostname"
        if prop in md_props:
            md[prop] = val
        elif prop in cfg_props:
            cfg[prop] = val
        elif prop in network_props and read_network:
            try:
                network_config = base64.b64decode(val.encode())
                md[prop] = safeload_yaml_or_dict(network_config).get('network')
            except Exception:
                LOG.debug("Ignore network-config in wrong format")
        elif prop == "user-data":
            try:
                ud = base64.b64decode(val.encode())
            except Exception:
                ud = val.encode()
    return (md, ud, cfg)


# Returns tuple of filename (in 'dirname', and the contents of the file)
# on "not found", returns 'None' for filename and False for contents
def get_ovf_env(dirname):
    env_names = ("ovf-env.xml", "ovf_env.xml", "OVF_ENV.XML", "OVF-ENV.XML")
    for fname in env_names:
        full_fn = os.path.join(dirname, fname)
        if os.path.isfile(full_fn):
            try:
                contents = util.load_file(full_fn)
                return (fname, contents)
            except Exception:
                util.logexc(LOG, "Failed loading ovf file %s", full_fn)
    return (None, False)


def maybe_cdrom_device(devname):
    """Test if devname matches known list of devices which may contain iso9660
       filesystems.

    Be helpful in accepting either knames (with no leading /dev/) or full path
    names, but do not allow paths outside of /dev/, like /dev/foo/bar/xxx.
    """
    if not devname:
        return False
    elif not isinstance(devname, str):
        raise ValueError("Unexpected input for devname: %s" % devname)

    # resolve '..' and multi '/' elements
    devname = os.path.normpath(devname)

    # drop leading '/dev/'
    if devname.startswith("/dev/"):
        # partition returns tuple (before, partition, after)
        devname = devname.partition("/dev/")[-1]

    # ignore leading slash (/sr0), else fail on / in name (foo/bar/xvdc)
    if devname.startswith("/"):
        devname = devname.split("/")[-1]
    elif devname.count("/") > 0:
        return False

    # if empty string
    if not devname:
        return False

    # default_regex matches values in /lib/udev/rules.d/60-cdrom_id.rules
    # KERNEL!="sr[0-9]*|hd[a-z]|xvd*", GOTO="cdrom_end"
    default_regex = r"^(sr[0-9]+|hd[a-z]|xvd.*)"
    devname_regex = os.environ.get("CLOUD_INIT_CDROM_DEV_REGEX", default_regex)
    cdmatch = re.compile(devname_regex)

    return cdmatch.match(devname) is not None


# Transport functions are called with no arguments and return
# either None (indicating not present) or string content of an ovf-env.xml
def transport_iso9660(require_iso=True):

    # Go through mounts to see if it was already mounted
    mounts = util.mounts()
    for (dev, info) in mounts.items():
        fstype = info['fstype']
        if fstype != "iso9660" and require_iso:
            continue
        if not maybe_cdrom_device(dev):
            continue
        mp = info['mountpoint']
        (_fname, contents) = get_ovf_env(mp)
        if contents is not False:
            return contents

    if require_iso:
        mtype = "iso9660"
    else:
        mtype = None

    # generate a list of devices with mtype filesystem, filter by regex
    devs = [dev for dev in
            util.find_devs_with("TYPE=%s" % mtype if mtype else None)
            if maybe_cdrom_device(dev)]
    for dev in devs:
        try:
            (_fname, contents) = util.mount_cb(dev, get_ovf_env, mtype=mtype)
        except util.MountFailedError:
            LOG.debug("%s not mountable as iso9660", dev)
            continue

        if contents is not False:
            return contents

    return None


def transport_vmware_guestinfo():
    rpctool = "vmware-rpctool"
    not_found = None
    if not subp.which(rpctool):
        return not_found
    cmd = [rpctool, "info-get guestinfo.ovfEnv"]
    try:
        out, _err = subp.subp(cmd)
        if out:
            return out
        LOG.debug("cmd %s exited 0 with empty stdout: %s", cmd, out)
    except subp.ProcessExecutionError as e:
        if e.exit_code != 1:
            LOG.warning("%s exited with code %d", rpctool, e.exit_code)
            LOG.debug(e)
    return not_found


def find_child(node, filter_func):
    ret = []
    if not node.hasChildNodes():
        return ret
    for child in node.childNodes:
        if filter_func(child):
            ret.append(child)
    return ret


def get_properties(contents):

    dom = minidom.parseString(contents)
    if dom.documentElement.localName != "Environment":
        raise XmlError("No Environment Node")

    if not dom.documentElement.hasChildNodes():
        raise XmlError("No Child Nodes")

    envNsURI = "http://schemas.dmtf.org/ovf/environment/1"

    # could also check here that elem.namespaceURI ==
    #   "http://schemas.dmtf.org/ovf/environment/1"
    propSections = find_child(dom.documentElement,
                              lambda n: n.localName == "PropertySection")

    if len(propSections) == 0:
        raise XmlError("No 'PropertySection's")

    props = {}
    propElems = find_child(propSections[0],
                           (lambda n: n.localName == "Property"))

    for elem in propElems:
        key = elem.attributes.getNamedItemNS(envNsURI, "key").value
        val = elem.attributes.getNamedItemNS(envNsURI, "value").value
        props[key] = val

    return props


def search_file(dirpath, filename):
    if not dirpath or not filename:
        return None

    for root, _dirs, files in os.walk(dirpath):
        if filename in files:
            return os.path.join(root, filename)

    return None


class XmlError(Exception):
    pass


# Used to match classes to dependencies
datasources = (
    (DataSourceOVF, (sources.DEP_FILESYSTEM, )),
    (DataSourceOVFNet, (sources.DEP_FILESYSTEM, sources.DEP_NETWORK)),
)


# Return a list of data sources that match this set of dependencies
def get_datasource_list(depends):
    return sources.list_from_depends(depends, datasources)


# To check if marker file exists
def check_marker_exists(markerid, marker_dir):
    """
    Check the existence of a marker file.
    Presence of marker file determines whether a certain code path is to be
    executed. It is needed for partial guest customization in VMware.
    @param markerid: is an unique string representing a particular product
                     marker.
    @param: marker_dir: The directory in which markers exist.
    """
    if not markerid:
        return False
    markerfile = os.path.join(marker_dir, ".markerfile-" + markerid + ".txt")
    if os.path.exists(markerfile):
        return True
    return False


# Create a marker file
def setup_marker_files(markerid, marker_dir):
    """
    Create a new marker file.
    Marker files are unique to a full customization workflow in VMware
    environment.
    @param markerid: is an unique string representing a particular product
                     marker.
    @param: marker_dir: The directory in which markers exist.

    """
    LOG.debug("Handle marker creation")
    markerfile = os.path.join(marker_dir, ".markerfile-" + markerid + ".txt")
    for fname in os.listdir(marker_dir):
        if fname.startswith(".markerfile"):
            util.del_file(os.path.join(marker_dir, fname))
    open(markerfile, 'w').close()


def _raise_error_status(prefix, error, event, config_file, conf):
    """
    Raise error and send customization status to the underlying VMware
    Virtualization Platform. Also, cleanup the imc directory.
    """
    LOG.debug('%s: %s', prefix, error)
    set_customization_status(
        GuestCustStateEnum.GUESTCUST_STATE_RUNNING,
        event)
    set_gc_status(conf, prefix)
    util.del_dir(os.path.dirname(config_file))
    raise error


def load_cloudinit_data(md_path, ud_path):
    """
    Load the cloud-init meta data, user data, cfg and network from the
    given files

    @return: 4-tuple of configuration
        metadata, userdata, cfg={}, network

    @raises: FileNotFoundError if md_path or ud_path are absent
    """
    LOG.debug('load meta data from: %s: user data from: %s',
              md_path, ud_path)
    md = {}
    ud = None
    network = None

    md = safeload_yaml_or_dict(util.load_file(md_path))

    if 'network' in md:
        network = md['network']

    if ud_path:
        ud = util.load_file(ud_path).replace("\r", "")
    return md, ud, {}, network


def safeload_yaml_or_dict(data):
    '''
    The meta data could be JSON or YAML. Since YAML is a strict superset of
    JSON, we will unmarshal the data as YAML. If data is None then a new
    dictionary is returned.
    '''
    if not data:
        return {}
    return safeyaml.load(data)


def collect_imc_file_paths(cust_conf):
    '''
    collect all the other imc files.

    metadata is preferred to nics.txt configuration data.

    If metadata file exists because it is specified in customization
    configuration, then metadata is required and userdata is optional.

    @return a 3-tuple containing desired configuration file paths if present
        Expected returns:
             1. user provided metadata and userdata (md_path, ud_path, None)
             2. user provided metadata (md_path, None, None)
             3. user-provided network config (None, None, nics_path)
             4. No config found (None, None, None)
    '''
    md_path = None
    ud_path = None
    nics_path = None
    md_file = cust_conf.meta_data_name
    if md_file:
        md_path = os.path.join(VMWARE_IMC_DIR, md_file)
        if not os.path.exists(md_path):
            raise FileNotFoundError("meta data file is not found: %s"
                                    % md_path)

        ud_file = cust_conf.user_data_name
        if ud_file:
            ud_path = os.path.join(VMWARE_IMC_DIR, ud_file)
            if not os.path.exists(ud_path):
                raise FileNotFoundError("user data file is not found: %s"
                                        % ud_path)
    else:
        nics_path = os.path.join(VMWARE_IMC_DIR, "nics.txt")
        if not os.path.exists(nics_path):
            LOG.debug('%s does not exist.', nics_path)
            nics_path = None

    return md_path, ud_path, nics_path


# vi: ts=4 expandtab
