# -*- coding: utf-8 -*-
#
# Copyright © 2011 Red Hat, Inc.
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

#from pulp.client.server.base import Server


class PulpAPI(object):
    """
    Base api class that allows an internal server object to be set after
    instantiation.
    @ivar server: L{Server} instance
    """

    def __init__(self):
        self.server = None

    def set_server(self, server):
#        assert isinstance(server, Server)
        self.server = server
