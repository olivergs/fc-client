# -*- coding: utf-8 -*-
# vi:ts=4 sw=4 sts=4

# Copyright (C) 2019 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the licence, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this program; if not, see <http://www.gnu.org/licenses/>.
#
# Authors: Alberto Ruiz <aruiz@redhat.com>
#          Oliver Gutiérrez <ogutierrez@redhat.com>

import os
import sys
import pwd
import grp
import platform
import logging
import json
import shutil

import dns.resolver

import ldap
import ldap.sasl
import ldap.modlist


from gi.repository import Gio
from gi.repository import GLib

from fleetcommanderclient.configloader import ConfigLoader
from fleetcommanderclient.settingscompiler import SettingsCompiler
from fleetcommanderclient import adapters

# Basic constants
FC_PROFILE_PREFIX = "_FC_%s"

FC_DEFAULT_PRIORITY = 50
FC_DEFAULT_PROFILE_DATA = json.dumps({"priority": FC_DEFAULT_PRIORITY, "settings": {}})

FC_GLOBAL_POLICY_NS = "org.freedesktop.FleetCommander"
FC_GLOBAL_POLICY_DEFAULT = 1
FC_GLOBAL_POLICY_MAPPINGS = [
    "ughx",
    "ugxh",
    "uhgx",
    "uhxg",
    "uxgh",
    "uxhg",
    "guhx",
    "guxh",
    "ghux",
    "ghxu",
    "gxuh",
    "gxhu",
    "hugx",
    "huxg",
    "hgux",
    "hgxu",
    "hxug",
    "hxgu",
    "xugh",
    "xuhg",
    "xguh",
    "xghu",
    "xhug",
    "xhgu",
]


class FleetCommanderIPARetriever(object):
    """
    Fleet commander Active Directory profile retriever
    """

    DOMAIN = None
    REALMD_BUS = Gio.BusType.SYSTEM
    FC_BUS = Gio.BusType.SYSTEM
    CACHED_DOMAIN_DN = None
    CACHED_SERVER_NAME = None

    def __init__(self, configfile="/etc/xdg/fleet-commander-client.conf"):
        """
        Class initialization
        """

        # Load configuration options
        self.config = ConfigLoader(configfile)

        # Set logging level
        self.log_level = self.config.get_value("log_level")
        loglevel = getattr(logging, self.log_level.upper())
        logging.basicConfig(level=loglevel)

        # Register configuration adapters
        self.adapters = {}

        self.register_adapter(
            adapters.DconfAdapter,
            self.config.get_value("dconf_profile_path"),
            self.config.get_value("dconf_db_path"),
        )

        self.register_adapter(
            adapters.GOAAdapter, self.config.get_value("goa_run_path")
        )

        self.register_adapter(adapters.NetworkManagerAdapter)

        self.register_adapter(
            adapters.ChromiumAdapter, self.config.get_value("chromium_policies_path")
        )

        self.register_adapter(
            adapters.ChromeAdapter, self.config.get_value("chrome_policies_path")
        )

        self.register_adapter(
            adapters.FirefoxAdapter, self.config.get_value("firefox_prefs_path")
        )

    def register_adapter(self, adapterclass, *args, **kwargs):
        self.adapters[adapterclass.NAMESPACE] = adapterclass(*args, **kwargs)

    def _get_domain_dn(self):
        if self.CACHED_DOMAIN_DN is None:
            self.CACHED_DOMAIN_DN = "DC=%s" % ",DC=".join(self.DOMAIN.split("."))
        return self.CACHED_DOMAIN_DN

    def _get_server_name(self):
        logging.debug("FCADRetriever: Getting LDAP service machine name")
        # Resolve LDAP service machine
        if self.CACHED_SERVER_NAME is None:
            result = dns.resolver.query(
                "_ldap._tcp.dc._msdcs.%s" % self.DOMAIN.lower(), "SRV"
            )
            self.CACHED_SERVER_NAME = str(result[0].target)[:-1]
        logging.debug("FCADRetriever: LDAP server: %s" % self.CACHED_SERVER_NAME)
        return self.CACHED_SERVER_NAME

    def _ldap_connect(self):
        """
        Connect to AD server
        """
        logging.debug("FCADRetriever: Connecting to AD LDAP server")
        server_name = self._get_server_name()
        # Connect to LDAP using Kerberos
        logging.debug("FCADRetriever: Initializing LDAP connection to %s" % server_name)
        self.connection = ldap.initialize("ldap://%s" % server_name)
        self.connection.set_option(ldap.OPT_REFERRALS, 0)
        sasl_auth = ldap.sasl.sasl({}, "GSSAPI")
        # Set option for getting security descriptors with non privileged user
        sdflags = 7
        control = ldap.controls.RequestControl(
            "1.2.840.113556.1.4.801",
            True,
            ("%c%c%c%c%c" % (48, 3, 2, 1, sdflags)).encode(),
        )
        self.connection.set_option(
            ldap.OPT_SERVER_CONTROLS,
            [
                control,
            ],
        )
        self.connection.protocol_version = 3
        logging.debug("FCADRetriever: Binding LDAP connection")
        self.connection.sasl_interactive_bind_s("", sasl_auth)

    def _get_smb_connection(self, service="SysVol"):
        # Connect to SMB using kerberos
        parm = param.LoadParm()
        creds = Credentials()
        creds.set_kerberos_state(MUST_USE_KERBEROS)
        creds.guess(parm)
        conn = smb.SMB(self._get_server_name(), service, lp=parm, creds=creds)
        return conn

    def _read_profile_data(self, ldap_data):
        cn = ldap_data["cn"][0].decode()
        profile = {
            "cn": cn,
            "name": ldap_data.get("displayName", (cn,))[0].decode(),
            "path": ldap_data["gPCFileSysPath"][0].decode(),
        }
        if "nTSecurityDescriptor" in ldap_data:
            # Read security descriptor
            sd = SecurityDescriptorHelper(ldap_data["nTSecurityDescriptor"][0], self)
            applies = sd.get_fc_applies()
            profile.update(applies)
        return profile

    def get_object_by_sid(self, sid, classes=["computer", "user", "group"]):
        base_dn = "%s" % self._get_domain_dn()
        object_classes = "".join(["(objectclass=%s)" % x for x in classes])
        filter = "(&(|%s)(objectSid=%s))" % (object_classes, sid)
        attrs = ["cn", "objectClass"]
        resultlist = self.connection.search_s(
            base_dn, ldap.SCOPE_SUBTREE, filter, attrs
        )
        resultlist = [x for x in resultlist if x[0] is not None]
        if len(resultlist) > 0:
            data = resultlist[0][1]
            return {"cn": data["cn"][0], "objectClass": data["objectClass"]}
        else:
            return None

    def get_sid(self, sid_ndr):
        return ndr_unpack(security.dom_sid, sid_ndr)

    def get_profile(self, filter):
        logging.debug(
            "FCADRetriever: Getting profile from AD LDAP. filter: %s" % filter
        )
        base_dn = "CN=Policies,CN=System,%s" % self._get_domain_dn()
        attrs = ["cn", "displayName", "gPCFileSysPath", "nTSecurityDescriptor"]
        resultlist = self.connection.search_s(
            base_dn, ldap.SCOPE_SUBTREE, filter, attrs
        )
        if len(resultlist) > 0:
            resdata = resultlist[0][1]
            if resdata:
                return self._read_profile_data(resdata)
        return None

    def get_profiles(self):
        logging.debug("FCADRetriever: Retrieving profiles")
        profiles = []
        base_dn = "CN=Policies,CN=System,%s" % self._get_domain_dn()
        filter = "(objectclass=groupPolicyContainer)"
        attrs = ["cn", "displayName", "gPCFileSysPath", "nTSecurityDescriptor"]
        resultlist = self.connection.search_s(
            base_dn, ldap.SCOPE_SUBTREE, filter, attrs
        )
        for res in resultlist:
            resdata = res[1]
            if resdata:
                logging.debug("FCADRetriever: Reading profile data: {}".format(resdata))
                profile = self._read_profile_data(resdata)
                if profile is not None:
                    profiles.append(profile)
        logging.debug("FCADRetriever: Read profiles: {}".format(profiles))
        return profiles

    def get_profile_cifs_data(self, cn):
        logging.debug("FCADRetriever: Getting CIFs data for profile %s" % cn)
        conn = self._get_smb_connection()
        furi = "%s\\Policies\\%s\\fleet-commander.json" % (self.DOMAIN, cn)
        logging.debug("FCADRetriever: Reading CIFs data from %s" % furi)
        try:
            return conn.loadfile(furi)
        except Exception as e:
            logging.error(
                "FCADRetriever: Failed reading CIFs data from {}: {}".format(furi, e)
            )
        return None

    def get_global_policy(self):
        global_policy = FC_GLOBAL_POLICY_DEFAULT
        # Get global policy profile from LDAP
        ldap_filter = "(displayName=%s)" % (
            FC_PROFILE_PREFIX % FC_GLOBAL_POLICY_PROFILE_NAME
        )
        profile = self.get_profile(ldap_filter)
        if profile is not None:
            logging.debug("FCADRetriever: Found global policy profile. Reading data.")
            data = self.get_profile_cifs_data(profile["cn"])
            jsondata = json.loads(data)
            global_policy = jsondata["settings"][FC_GLOBAL_POLICY_NS].get(
                "global_policy", FC_GLOBAL_POLICY_DEFAULT
            )
        return global_policy

    def check_realm(self):
        logging.debug("FCADRetriever: Checking realm configuration")
        sssd_provider = Gio.DBusProxy.new_for_bus_sync(
            self.REALMD_BUS,
            Gio.DBusProxyFlags.NONE,
            None,
            "org.freedesktop.realmd",
            "/org/freedesktop/realmd/Sssd",
            "org.freedesktop.realmd.Provider",
            None,
        )
        realms = sssd_provider.get_cached_property("Realms")
        if len(realms) > 0:
            logging.debug(
                "FCADRetriever: realmd queried. Realm object {}".format(realms[0])
            )
            realm = Gio.DBusProxy.new_for_bus_sync(
                self.REALMD_BUS,
                Gio.DBusProxyFlags.NONE,
                None,
                "org.freedesktop.realmd",
                realms[0],
                "org.freedesktop.realmd.Realm",
                None,
            )
            domain = str(realm.get_cached_property("Name")).replace("'", "")
            details = {str(k): str(v) for k, v in realm.get_cached_property("Details")}
            server = details.get("server-software", "not-ad")
            logging.debug(
                "FCADRetriever: Realm details: {} ({})".format(domain, server)
            )
            if server != "active-directory":
                logging.debug(
                    "FCADRetriever: Realm is not an Active Directory. Exiting."
                )
                self.quit()
            return domain
        else:
            logging.debug(
                "FCADRetriever: This computer is not part of any realm. Exiting."
            )
            self.quit()

    def check_elements_in_list(self, elements, element_list):
        for element in elements:
            if element in element_list:
                return True
        return False

    def generate_priority_applies(
        self, username, groups, hostname, priority, global_policy, profile
    ):

        # Check profile applies
        by_user = self.check_elements_in_list([username], profile["users"])
        by_group = self.check_elements_in_list(groups, profile["groups"])
        by_host = self.check_elements_in_list([hostname], profile["hosts"])

        if by_user:
            user_prio = priority
        else:
            user_prio = FC_NO_MATCH_PRIORITY

        if by_group:
            group_prio = priority
        else:
            group_prio = FC_NO_MATCH_PRIORITY

        if by_host:
            host_prio = priority
        else:
            host_prio = FC_NO_MATCH_PRIORITY

        # Hostgroup priority is always empty in AD
        hostgroup_prio = FC_NO_MATCH_PRIORITY

        gp_mapping = FC_GLOBAL_POLICY_MAPPINGS[global_policy - 1]

        # Generate list for priority applies formatting
        prio_applies = []
        for gp_ent in gp_mapping:
            if gp_ent == "u":
                prio_applies.append(user_prio)
            elif gp_ent == "g":
                prio_applies.append(group_prio)
            elif gp_ent == "h":
                prio_applies.append(host_prio)
            elif gp_ent == "x":
                prio_applies.append(hostgroup_prio)
        applies = "{}_{}_{}_{}".format(*prio_applies)
        return applies

    def process_profile(
        self, profile, userdir, username, groups, hostname, global_policy
    ):
        # Read CIFs data
        data = self.get_profile_cifs_data(profile["cn"])
        if data is not None:
            jsondata = json.loads(data)
            # Get priority
            priority = str(jsondata.get("priority", FC_DEFAULT_PRIORITY)).zfill(5)
            # Generate file name
            priority_applies = self.generate_priority_applies(
                username, groups, hostname, priority, global_policy, profile
            )
            # Generate filename with global policy, applies and priority
            filename = "{}_{}-{}".format(
                priority, priority_applies, profile["name"].replace(" ", "_")
            )
            # Write profile file in user directory
            with open(os.path.join(userdir, filename), "w") as fd:
                fd.write(json.dumps(jsondata["settings"]))
                fd.close()

    def call_fc_client(self):
        logging.debug("FCADRetriever: Calling FC client")
        fc = Gio.DBusProxy.new_for_bus_sync(
            self.FC_BUS,
            Gio.DBusProxyFlags.NONE,
            None,
            "org.freedesktop.FleetCommanderClient",
            "/org/freedesktop/FleetCommanderClient",
            "org.freedesktop.FleetCommanderClient",
            None,
        )
        fc.call_sync("ProcessFiles", None, Gio.DBusCallFlags.NONE, -1, None)

    def run(self):
        # Check realm
        self.DOMAIN = self.check_realm()
        # Connect to LDAP
        try:
            self._ldap_connect()
            logging.debug("FCADRetriever: LDAP connection succesful")
        except Exception as e:
            logging.error("FCADRetriever: LDAP connection failed. {}".format(e))
            sys.exit(1)

        # First of all, execute a deployment with existing data so we can
        # take our time in downloading new data from server
        logging.debug("FCADRetriever: Deploying existing cache data")
        self.call_fc_client()

        logging.debug("FCADRetriever: Resuming AD profile processing")
        # Get current user name
        username = pwd.getpwuid(os.getuid()).pw_name.split("@")[0]
        # Get current user UID
        uid = os.getuid()
        # Get current user groups
        group_ids = os.getgroups()
        groups = []
        for group_id in group_ids:
            groups.append(grp.getgrgid(group_id).gr_name.split("@")[0])
        # Get current machine name
        hostname = platform.node()
        # Get global policy
        global_policy = self.get_global_policy()
        # Generate user dir with base user dir path and UID
        logging.debug("FCADRetriever: Generating user cache directory")
        userdir = os.path.join(
            os.path.expanduser("~/.cache/fleet-commander-client"), str(uid)
        )
        profilesdir = os.path.join(userdir, "profiles")
        if os.path.exists(userdir):
            shutil.rmtree(userdir)
        os.makedirs(profilesdir)

        # Read all profiles
        logging.debug("FCADRetriever: Reading and processing profiles")
        profiles = self.get_profiles()
        # Process each profile
        for profile in profiles:
            self.process_profile(
                profile, profilesdir, username, groups, hostname, global_policy
            )

        # Compile profiles data
        logging.debug("FCADRetriever: Compiling settings data")
        sc = SettingsCompiler(profilesdir)
        compiled_settings = sc.compile_settings()

        # Prepare cached files
        for namespace, adapter in self.adapters.items():
            if namespace in compiled_settings:
                config_data = compiled_settings[namespace]
                adapter.generate_config(config_data)
            else:
                # Just clean up data
                adapter.cleanup_cache()

        # Call FC client dbus service giving user directory and user UID
        logging.debug("FCADRetriever: Deploying AD profiles")
        self.call_fc_client()

    def quit(self):
        sys.exit()


if __name__ == "__main__":
    fcad = FleetCommanderADProfileRetriever()
    fcad.run()
