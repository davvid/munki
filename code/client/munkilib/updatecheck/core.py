#!/usr/bin/python
# encoding: utf-8
#
# Copyright 2009-2016 Greg Neagle.
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
updatecheck.core

Created by Greg Neagle on 2008-11-13.

"""

# standard libs
import os

# our libs
from . import analyze
from . import catalogs
from . import download
from . import licensing
from . import manifestutils

from .. import keychain
from .. import munkicommon
from .. import munkistatus
from .. import FoundationPlist


class UpdateCheckAbortedError(Exception):
    '''Exception used to break out of checking for updates'''
    pass


def check(client_id='', localmanifestpath=None):
    """Checks for available new or updated managed software, downloading
    installer items if needed. Returns 1 if there are available updates,
    0 if there are no available updates, and -1 if there were errors."""

    munkicommon.report['MachineInfo'] = munkicommon.getMachineFacts()

    # initialize our Munki keychain if we are using custom certs or CAs
    dummy_keychain_obj = keychain.MunkiKeychain()

    managed_install_dir = munkicommon.pref('ManagedInstallDir')
    if munkicommon.munkistatusoutput:
        munkistatus.activate()

    munkicommon.log('### Beginning managed software check ###')
    munkicommon.display_status_major('Checking for available updates...')
    munkistatus.percent('-1')
    munkistatus.detail('')

    installinfo = {}

    try:
        if localmanifestpath:
            mainmanifestpath = localmanifestpath
        else:
            try:
                mainmanifestpath = manifestutils.get_primary_manifest(client_id)
            except manifestutils.ManifestException:
                munkicommon.display_error(
                    'Could not retrieve managed install primary manifest.')
                raise

        if munkicommon.stop_requested():
            return 0

        # initialize our installinfo record
        installinfo['processed_installs'] = []
        installinfo['processed_uninstalls'] = []
        installinfo['managed_updates'] = []
        installinfo['optional_installs'] = []
        installinfo['managed_installs'] = []
        installinfo['removals'] = []

        # record info object for conditional item comparisons
        munkicommon.report['Conditions'] = munkicommon.predicate_info_object()

        munkicommon.display_detail('**Checking for installs**')
        analyze.process_manifest_for_key(
            mainmanifestpath, 'managed_installs', installinfo)
        if munkicommon.stop_requested():
            return 0

        # reset progress indicator and detail field
        munkistatus.message('Checking for additional changes...')
        munkistatus.percent('-1')
        munkistatus.detail('')

        # now generate a list of items to be uninstalled
        munkicommon.display_detail('**Checking for removals**')
        analyze.process_manifest_for_key(
            mainmanifestpath, 'managed_uninstalls', installinfo)
        if munkicommon.stop_requested():
            return 0

        # now check for implicit removals
        # use catalogs from main manifest
        cataloglist = manifestutils.get_manifest_value_for_key(
            mainmanifestpath, 'catalogs')
        autoremovalitems = catalogs.get_auto_removal_items(
            installinfo, cataloglist)
        if autoremovalitems:
            munkicommon.display_detail('**Checking for implicit removals**')
        for item in autoremovalitems:
            if munkicommon.stop_requested():
                return 0
            analyze.process_removal(item, cataloglist, installinfo)

        # look for additional updates
        munkicommon.display_detail('**Checking for managed updates**')
        analyze.process_manifest_for_key(
            mainmanifestpath, 'managed_updates', installinfo)
        if munkicommon.stop_requested():
            return 0

        # build list of optional installs
        analyze.process_manifest_for_key(
            mainmanifestpath, 'optional_installs', installinfo)
        if munkicommon.stop_requested():
            return 0

        # verify available license seats for optional installs
        if installinfo.get('optional_installs'):
            licensing.update_available_license_seats(installinfo)

        # process LocalOnlyManifest installs
        localonlymanifestname = munkicommon.pref('LocalOnlyManifest')
        if localonlymanifestname:
            localonlymanifest = os.path.join(
                managed_install_dir, 'manifests', localonlymanifestname)

            # if the manifest already exists, the name is being reused
            if localonlymanifestname in manifestutils.manifests():
                munkicommon.display_error(
                    "LocalOnlyManifest %s has the same name as an existing "
                    "manifest, skipping...", localonlymanifestname
                )
            elif os.path.exists(localonlymanifest):
                manifestutils.set_manifest(
                    localonlymanifestname, localonlymanifest)
                # use catalogs from main manifest for local only manifest
                cataloglist = manifestutils.get_manifest_value_for_key(
                    mainmanifestpath, 'catalogs')
                munkicommon.display_detail(
                    '**Processing local-only choices**'
                )

                localonlyinstalls = manifestutils.get_manifest_value_for_key(
                    localonlymanifest, 'managed_installs') or []
                for item in localonlyinstalls:
                    dummy_result = analyze.process_install(
                        item,
                        cataloglist,
                        installinfo
                    )

                localonlyuninstalls = manifestutils.get_manifest_value_for_key(
                    localonlymanifest, 'managed_uninstalls') or []
                for item in localonlyuninstalls:
                    analyze.process_removal(item, cataloglist, installinfo)
            else:
                munkicommon.display_debug1(
                    "LocalOnlyManifest %s is set but is not present. "
                    "Skipping...", localonlymanifestname
                )

        # now process any self-serve choices
        usermanifest = '/Users/Shared/.SelfServeManifest'
        selfservemanifest = os.path.join(
            managed_install_dir, 'manifests', 'SelfServeManifest')
        if os.path.exists(usermanifest):
            # copy user-generated SelfServeManifest to our
            # managed_install_dir
            try:
                plist = FoundationPlist.readPlist(usermanifest)
                if plist:
                    FoundationPlist.writePlist(plist, selfservemanifest)
                    # now remove the user-generated manifest
                    try:
                        os.unlink(usermanifest)
                    except OSError:
                        pass
            except FoundationPlist.FoundationPlistException:
                # problem reading the usermanifest
                # better remove it
                munkicommon.display_error('Could not read %s', usermanifest)
                try:
                    os.unlink(usermanifest)
                except OSError:
                    pass

        if os.path.exists(selfservemanifest):
            # use catalogs from main manifest for self-serve manifest
            cataloglist = manifestutils.get_manifest_value_for_key(
                mainmanifestpath, 'catalogs')
            munkicommon.display_detail('**Processing self-serve choices**')
            selfserveinstalls = manifestutils.get_manifest_value_for_key(
                selfservemanifest, 'managed_installs')

            # build list of items in the optional_installs list
            # that have not exceeded available seats
            available_optional_installs = [
                item['name']
                for item in installinfo.get('optional_installs', [])
                if (not 'licensed_seats_available' in item
                    or item['licensed_seats_available'])]
            if selfserveinstalls:
                # filter the list, removing any items not in the current list
                # of available self-serve installs
                selfserveinstalls = [item for item in selfserveinstalls
                                     if item in available_optional_installs]
                for item in selfserveinstalls:
                    dummy_result = analyze.process_install(
                        item, cataloglist, installinfo)

            # we don't need to filter uninstalls
            selfserveuninstalls = manifestutils.get_manifest_value_for_key(
                selfservemanifest, 'managed_uninstalls') or []
            for item in selfserveuninstalls:
                analyze.process_removal(item, cataloglist, installinfo)

            # update optional_installs with install/removal info
            for item in installinfo['optional_installs']:
                if (not item.get('installed') and
                        analyze.item_in_installinfo(
                            item, installinfo['managed_installs'])):
                    item['will_be_installed'] = True
                elif (item.get('installed') and
                      analyze.item_in_installinfo(
                          item, installinfo['removals'])):
                    item['will_be_removed'] = True

        # filter managed_installs to get items already installed
        installed_items = [item.get('name', '')
                           for item in installinfo['managed_installs']
                           if item.get('installed')]
        # filter managed_installs to get problem items:
        # not installed, but no installer item
        problem_items = [item
                         for item in installinfo['managed_installs']
                         if item.get('installed') is False and
                         not item.get('installer_item')]
        # filter removals to get items already removed
        # (or never installed)
        removed_items = [item.get('name', '')
                         for item in installinfo['removals']
                         if item.get('installed') is False]

        if os.path.exists(selfservemanifest):
            # for any item in the managed_uninstalls in the self-serve
            # manifest that is not installed, we should remove it from
            # the list
            try:
                plist = FoundationPlist.readPlist(selfservemanifest)
            except FoundationPlist.FoundationPlistException:
                pass
            else:
                plist['managed_uninstalls'] = [
                    item for item in plist.get('managed_uninstalls', [])
                    if item not in removed_items]
                try:
                    FoundationPlist.writePlist(plist, selfservemanifest)
                except FoundationPlist.FoundationPlistException:
                    pass

        # record detail before we throw it away...
        munkicommon.report['ManagedInstalls'] = installinfo['managed_installs']
        munkicommon.report['InstalledItems'] = installed_items
        munkicommon.report['ProblemInstalls'] = problem_items
        munkicommon.report['RemovedItems'] = removed_items

        munkicommon.report['managed_installs_list'] = installinfo[
            'processed_installs']
        munkicommon.report['managed_uninstalls_list'] = installinfo[
            'processed_uninstalls']
        munkicommon.report['managed_updates_list'] = installinfo[
            'managed_updates']

        # filter managed_installs and removals lists
        # so they have only items that need action
        installinfo['managed_installs'] = [
            item for item in installinfo['managed_installs']
            if item.get('installer_item')]
        installinfo['removals'] = [
            item for item in installinfo['removals']
            if item.get('installed')]

        # also record problem items so MSC.app can provide feedback
        installinfo['problem_items'] = problem_items

        # download display icons for optional installs
        # and active installs/removals
        item_list = list(installinfo.get('optional_installs', []))
        item_list.extend(installinfo['managed_installs'])
        item_list.extend(installinfo['removals'])
        download.download_icons(item_list)

        # get any custom client resources
        download.download_client_resources()

        # record the filtered lists
        munkicommon.report['ItemsToInstall'] = installinfo['managed_installs']
        munkicommon.report['ItemsToRemove'] = installinfo['removals']

        # clean up catalogs directory
        catalogs.clean_up()

        # clean up manifests directory
        manifestutils.clean_up_manifests()

        # clean up cache dir
        # remove any item in the cache that isn't scheduled
        # to be used for an install or removal
        # this could happen if an item is downloaded on one
        # updatecheck run, but later removed from the manifest
        # before it is installed or removed - so the cached item
        # is no longer needed.
        cache_list = [item['installer_item']
                      for item in installinfo.get('managed_installs', [])]
        cache_list.extend([item['uninstaller_item']
                           for item in installinfo.get('removals', [])
                           if item.get('uninstaller_item')])
        cachedir = os.path.join(managed_install_dir, 'Cache')
        for item in munkicommon.listdir(cachedir):
            if item.endswith('.download'):
                # we have a partial download here
                # remove the '.download' from the end of the filename
                fullitem = os.path.splitext(item)[0]
                if os.path.exists(os.path.join(cachedir, fullitem)):
                    # we have a partial and a full download
                    # for the same item. (This shouldn't happen.)
                    # remove the partial download.
                    os.unlink(os.path.join(cachedir, item))
                elif problem_items == []:
                    # problem items is our list of items
                    # that need to be installed but are missing
                    # the installer_item; these might be partial
                    # downloads. So if we have no problem items, it's
                    # OK to get rid of any partial downloads hanging
                    # around.
                    os.unlink(os.path.join(cachedir, item))
            elif item not in cache_list:
                munkicommon.display_detail('Removing %s from cache', item)
                os.unlink(os.path.join(cachedir, item))

        # write out install list so our installer
        # can use it to install things in the right order
        installinfochanged = True
        installinfopath = os.path.join(managed_install_dir, 'InstallInfo.plist')
        if os.path.exists(installinfopath):
            try:
                oldinstallinfo = FoundationPlist.readPlist(installinfopath)
            except FoundationPlist.NSPropertyListSerializationException:
                oldinstallinfo = None
                munkicommon.display_error(
                    'Could not read InstallInfo.plist. Deleting...')
                try:
                    os.unlink(installinfopath)
                except OSError, err:
                    munkicommon.display_error(
                        'Failed to delete InstallInfo.plist: %s', str(err))
            if oldinstallinfo == installinfo:
                installinfochanged = False
                munkicommon.display_detail('No change in InstallInfo.')

        if installinfochanged:
            FoundationPlist.writePlist(
                installinfo,
                os.path.join(managed_install_dir, 'InstallInfo.plist'))

    except (manifestutils.ManifestException, UpdateCheckAbortedError):
        # Update check aborted. Check to see if we have a valid
        # install/remove list from an earlier run.
        installinfopath = os.path.join(managed_install_dir, 'InstallInfo.plist')
        if os.path.exists(installinfopath):
            try:
                installinfo = FoundationPlist.readPlist(installinfopath)
            except FoundationPlist.NSPropertyListSerializationException:
                installinfo = {}
            munkicommon.report['ItemsToInstall'] = \
                installinfo.get('managed_installs', [])
            munkicommon.report['ItemsToRemove'] = \
                installinfo.get('removals', [])

    munkicommon.savereport()
    munkicommon.log('###    End managed software check    ###')

    installcount = len(installinfo.get('managed_installs', []))
    removalcount = len(installinfo.get('removals', []))

    if installcount or removalcount:
        return 1
    else:
        return 0


def get_primary_manifest_catalogs(client_id='', force_refresh=False):
    """Return list of catalogs from primary client manifest

    Args:
      force_refresh: Boolean. If True, downloads primary manifest
      and listed catalogs; False, uses locally cached information.
    Returns:
      cataloglist: list of catalogs from primary manifest
    """
    cataloglist = []
    if (force_refresh or
            manifestutils.PRIMARY_MANIFEST_TAG
            not in manifestutils.manifests()):
        # Fetch manifest from repo
        manifest = manifestutils.get_primary_manifest(client_id)
        # set force_refresh = True so we'll also download any missing catalogs
        force_refresh = True
    else:
        # Use cached manifest if available
        manifest_dir = os.path.join(
            munkicommon.pref('managed_install_dir'), 'manifests')
        manifest = os.path.join(
            manifest_dir,
            manifestutils.get_manifest(manifestutils.PRIMARY_MANIFEST_TAG))

    if manifest:
        manifestdata = manifestutils.get_manifest_data(manifest)
        cataloglist = manifestdata.get('catalogs')
        if cataloglist and force_refresh:
            # download catalogs since we might not have them
            catalogs.get_catalogs(cataloglist)

    return cataloglist


if __name__ == '__main__':
    print 'This is a library of support tools for the Munki Suite.'
