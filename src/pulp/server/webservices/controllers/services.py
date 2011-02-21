#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright © 2010 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public License,
# version 2 (GPLv2). There is NO WARRANTY for this software, express or
# implied, including the implied warranties of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. You should have received a copy of GPLv2
# along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.
#
# Red Hat trademarks are not licensed under GPLv2. No permission is
# granted to use or replicate Red Hat trademarks that are incorporated
# in this software or its documentation.
import logging
import time
import base64
import web

from pulp.server.api.package import PackageApi
from pulp.server.api.repo import RepoApi
from pulp.server.api.upload import File
from pulp.server.api.upload import ImportUploadContent
from pulp.server.auth.authorization import READ, EXECUTE
from pulp.server.webservices import mongo
from pulp.server.webservices.controllers.base import JSONController

# globals ---------------------------------------------------------------------

rapi = RepoApi()
papi = PackageApi()
log = logging.getLogger('pulp')

# services controllers --------------------------------------------------------

class DependencyActions(JSONController):

    @JSONController.error_handler
    @JSONController.auth_required(EXECUTE)
    def POST(self):
        """
        list of available dependencies required \
        for a specified package per repo.
        expects passed in pkgnames and repoids from POST data
        @return: a dict of printable dependency result and suggested packages
        """
        data = self.params()
        return self.ok(papi.package_dependency(data['pkgnames'], data['repoids'], recursive=data['recursive']))


class PackageSearch(JSONController):

    @JSONController.error_handler
    @JSONController.auth_required(READ)
    def GET(self):
        """
        List available packages.
        @return: a list of packages
        """
        log.info("search:   GET received")
        valid_filters = ('id', 'name')
        filters = self.filters(valid_filters)
        spec = mongo.filters_to_re_spec(filters)
        return self.ok(papi.package_descriptions(spec))


    @JSONController.error_handler
    @JSONController.auth_required(EXECUTE)
    def POST(self):
        """
        Search for matching packages 
        expects passed in regex search strings from POST data
        @return: package meta data on successful creation of package
        """
        data = self.params()
        name = None
        if data.has_key("name"):
            name = data["name"]
        epoch = None
        if data.has_key("epoch"):
            epoch = data["epoch"]
        version = None
        if data.has_key("version"):
            version = data["version"]
        release = None
        if data.has_key("release"):
            release = data["release"]
        arch = None
        if data.has_key("arch"):
            arch = data["arch"]
        filename = None
        if data.has_key("filename"):
            filename = data["filename"]
        checksum_type=None
        if data.has_key("checksum_type"):
            checksum_type = data["checksum_type"]
        checksum=None
        if data.has_key("checksum"):
            checksum = data["checksum"]
        start_time = time.time()
        pkgs = papi.packages(name=name, epoch=epoch, version=version,
            release=release, arch=arch, filename=filename, checksum=checksum,
            checksum_type=checksum_type, regex=True)
        initial_search_end = time.time()
        for p in pkgs:
            p["repos"] = rapi.find_repos_by_package(p["id"])
        repo_lookup_time = time.time()
        log.info("Search [%s]: package lookup: %s, repo correlation: %s, total: %s" % \
                (data, (initial_search_end - start_time),
                    (repo_lookup_time - initial_search_end),
                    (repo_lookup_time - start_time)))
        return self.ok(pkgs)

    # this was not written correctly...
    def PUT(self):
        log.warning('deprecated DependencyActions.PUT called')
        return self.POST()

class StartUpload(JSONController):

    @JSONController.error_handler
    def POST(self):
        request = self.params()
        name = request['name']
        checksum = request['checksum']
        size = request['size']
        f = File.open(name, checksum, size)
        offset = f.next()
        d = dict(id=f.id, offset=offset)
        return self.ok(d)


class AppendUpload(JSONController):

    @JSONController.error_handler
    def POST(self, id):
        f = File(id)
        segment = self.params()
        encoding = segment['encoding']
        content = segment['content']
        f.append(base64.b64decode(content))
        return self.ok(True)

class ImportUpload(JSONController):
    
    @JSONController.error_handler
    @JSONController.auth_required(EXECUTE)
    def POST(self):
        """
        finalize the uploaded file(s)/package(s) on pulp server and
        import the metadata into pulp db to create an object;
        expects passed in metadata and upload_id from POST data
        @return: a dict of printable dependency result and suggested packages
        """
        data = self.params()
        capi = ImportUploadContent(data['metadata'], data['uploadid'])        
        return self.ok(capi.process())


# web.py application ----------------------------------------------------------

URLS = (
    '/dependencies/$', 'DependencyActions',
    '/search/packages/$', 'PackageSearch',
    '/upload/$', 'StartUpload',
    '/upload/append/([^/]+)/$', 'AppendUpload',
    '/upload/import/$',  'ImportUpload',
    
)

application = web.application(URLS, globals())
