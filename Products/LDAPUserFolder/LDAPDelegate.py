##############################################################################
#
# Copyright (c) 2000-2009 Jens Vagelpohl and Contributors. All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################
""" LDAPDelegate: A delegate that performs LDAP operations

$Id$
"""

# General python imports
import ldap
from ldapurl import LDAPUrl
from ldapurl import isLDAPUrl
from ldap.dn import escape_dn_chars
from ldap.filter import filter_format
import logging
import os
import random

# Zope imports
from Persistence import Persistent
from AccessControl.SecurityManagement import getSecurityManager

# LDAPUserFolder package imports
from Products.LDAPUserFolder.LDAPUser import LDAPUser
from Products.LDAPUserFolder.SharedResource import getResource
from Products.LDAPUserFolder.SharedResource import setResource
from Products.LDAPUserFolder.utils import BINARY_ATTRIBUTES
from Products.LDAPUserFolder.utils import from_utf8
from Products.LDAPUserFolder.utils import registerDelegate
from Products.LDAPUserFolder.utils import to_utf8

try:
    c_factory = ldap.ldapobject.ReconnectLDAPObject
except AttributeError:
    c_factory = ldap.ldapobject.SimpleLDAPObject
logger = logging.getLogger('event.LDAPDelegate')



class LDAPDelegate(Persistent):
    """ LDAPDelegate

    This object handles all LDAP operations. All search operations will
    return a dictionary, where the keys are as follows:

    exception   - Contains a string representing exception information
                  if an exception was raised during the operation.

    size        - An integer containing the length of the result set
                  generated by the operation. Will be 0 if an exception
                  was raised.

    results     - Sequence of results
    """

    def __setstate__(self, v):
        """
            __setstate__ is called whenever the instance is loaded
            from the ZODB, like when Zope is restarted.
        """
        # Call inherited __setstate__ methods if they exist
        LDAPDelegate.inheritedAttribute('__setstate__')(self, v)

        # Delete old-style logger instances
        if getattr(self, '_logger', None) is not None:
            del self._logger


    def __init__( self, server='', login_attr='', users_base='', rdn_attr=''
                , use_ssl=0, bind_dn='', bind_pwd='', read_only=0
                ):
        """ Create a new LDAPDelegate instance """
        self._hash = 'ldap_delegate%s' % str(random.random())
        self._servers = []
        self.edit( login_attr, users_base, rdn_attr
                 , 'top,person', bind_dn, bind_pwd
                 , 1, read_only
                 )

        if server != '':
            if server.find(':') != -1:
                host = server.split(':')[0].strip()
                port = int(server.split(':')[1])
            else:
                host = server

                if use_ssl == 2:
                    port = 0
                elif use_ssl == 1:
                    port = 636
                else:
                    port = 389

            self.addServer(host, port, use_ssl)


    def addServer( self
                 , host
                 , port='389'
                 , use_ssl=0
                 , conn_timeout=-1
                 , op_timeout=-1
                 ):
        """ Add a server to our list of servers """
        servers = self.getServers()

        if use_ssl == 2:
            protocol = 'ldapi'
            port = 0
        elif use_ssl == 1:
            protocol = 'ldaps'
        else:
            protocol = 'ldap'

        already_exists = 0
        for server in self._servers:
            if ( str(server['host']) == str(host) and 
                 str(server['port']) == str(port) and 
                 str(server['protocol']) == str(protocol) ):
                already_exists = 1
                server['conn_timeout'] = conn_timeout
                server['op_timeout'] = op_timeout
                break

        if not already_exists:
            servers.append( { 'host' : host
                            , 'port' : port
                            , 'protocol' : protocol
                            , 'conn_timeout' : conn_timeout
                            , 'op_timeout' : op_timeout
                            } )

        self._servers = servers

        # Delete the cached connection in case the new server was added
        # in response to the existing server failing in a way that leads
        # to nasty timeouts
        setResource('%s-connection' % self._hash, '')


    def getServers(self):
        """ Return info about all my servers """
        servers = getattr(self, '_servers', [])

        if isinstance(servers, dict):
            servers = servers.values()
            self._servers = servers

        return servers


    @property
    def _servers(self):
        """If any LDAP servers defined via environment variables are found,
        return them instead of the plugin's persistent server list.

        Servers defined via environment variables will therefore shadow
        the persistent server list in its entirety.
        """

        dynamic_servers = self.getDynamicServers()
        if dynamic_servers:
            return dynamic_servers

        return self.__dict__.get('_servers', [])


    @_servers.setter
    def _servers(self, value):
        if self.getDynamicServers():
            # TODO: Need to have a closer look at how to best handle
            # attempts to add / delete servers in various scenarios.
            logger.warn('Ignored write attempt at %r._servers' % self)
            return

        self.__dict__['_servers'] = value


    @property
    def bind_dn(self):
        dynamic_bind_dn = os.environ.get('PLONE_LDAP_BIND_DN')
        if dynamic_bind_dn:
            return dynamic_bind_dn

        return self.__dict__['bind_dn']


    @bind_dn.setter
    def bind_dn(self, value):
        if os.environ.get('PLONE_LDAP_BIND_DN'):
            logger.warn('Ignored write attempt at %r.bind_dn' % self)
            return

        self.__dict__['bind_dn'] = value


    @property
    def bind_pwd(self):
        # NOTE: Dollar signs must be escaped with $$ in zope.conf
        dynamic_bind_pwd = os.environ.get('PLONE_LDAP_BIND_PWD')
        if dynamic_bind_pwd:
            return dynamic_bind_pwd

        return self.__dict__['bind_pwd']


    @bind_pwd.setter
    def bind_pwd(self, value):
        if os.environ.get('PLONE_LDAP_BIND_PWD'):
            logger.warn('Ignored write attempt at %r.bind_pwd' % self)
            return

        self.__dict__['bind_pwd'] = value

    @property
    def u_base(self):
        dynamic_users_base = os.environ.get('PLONE_LDAP_USERS_BASE')
        if dynamic_users_base:
            return dynamic_users_base

        return self.__dict__['u_base']


    @u_base.setter
    def u_base(self, value):
        if os.environ.get('PLONE_LDAP_USERS_BASE'):
            logger.warn('Ignored write attempt at %r.u_base' % self)
            return

        self.__dict__['u_base'] = value


    def getDynamicServers(self):
        """Return list of LDAP servers defined via env vars (if any).
        """
        dynamic_servers = []

        # TODO: Support for multiple servers
        if 'PLONE_LDAP_HOST' in os.environ:
            host = os.environ['PLONE_LDAP_HOST']
            port = int(os.environ.get('PLONE_LDAP_PORT', 389))
            protocol = os.environ.get('PLONE_LDAP_PROTOCOL', 'ldap')
            conn_timeout = int(os.environ.get('PLONE_LDAP_CONN_TIMEOUT', 5))
            op_timeout = int(os.environ.get('PLONE_LDAP_OP_TIMEOUT', -1))

            dynamic_servers.append({
                'host': host,
                'port': port,
                'protocol': protocol,
                'conn_timeout': conn_timeout,
                'op_timeout': op_timeout,
            })

        return dynamic_servers


    def deleteServers(self, position_list=()):
        """ Delete server definitions """
        old_servers = self.getServers()
        new_servers = []
        position_list = [int(x) for x in position_list]

        for i in range(len(old_servers)):
            if i not in position_list:
                new_servers.append(old_servers[i])

        self._servers = new_servers

        # Delete the cached connection so that we don't accidentally
        # continue using a server we should not be using anymore
        setResource('%s-connection' % self._hash, '')


    def edit( self, login_attr, users_base, rdn_attr, objectclasses
            , bind_dn, bind_pwd, binduid_usage, read_only
            ):
        """ Edit this LDAPDelegate instance """
        self.login_attr = login_attr
        self.rdn_attr = rdn_attr
        self.bind_dn = bind_dn
        self.bind_pwd = bind_pwd
        self.binduid_usage = int(binduid_usage)
        self.read_only = not not read_only
        self.u_base = users_base

        if isinstance(objectclasses, basestring):
            objectclasses = [x.strip() for x in objectclasses.split(',')]
        self.u_classes = objectclasses


    def connect(self, bind_dn='', bind_pwd=''):
        """ initialize an ldap server connection """
        conn = None
        conn_string = ''

        if bind_dn != '':
            user_dn = bind_dn
            user_pwd = bind_pwd or '~'
        elif self.binduid_usage == 1:
            user_dn = self.bind_dn
            user_pwd = self.bind_pwd
        else:
            user = getSecurityManager().getUser()
            if isinstance(user, LDAPUser):
                user_dn = user.getUserDN()
                user_pwd = user._getPassword()
                if not user_pwd or user_pwd == 'undef':
                    # This user object did not result from a login
                    user_dn = user_pwd = ''
            else:
                user_dn = user_pwd = ''

        conn = getResource('%s-connection' % self._hash, str, ())
        if not isinstance(conn._type(), str):
            try:
                conn.simple_bind_s(user_dn, user_pwd)
                conn.search_s(self.u_base, self.BASE, '(objectClass=*)')
                return conn
            except ( AttributeError
                   , ldap.SERVER_DOWN
                   , ldap.NO_SUCH_OBJECT
                   , ldap.TIMEOUT
                   , ldap.INVALID_CREDENTIALS
                   ):
                pass

        e = None

        for server in self._servers:
            conn_string = self._createConnectionString(server)

            try:
                newconn = self._connect( conn_string
                                       , user_dn
                                       , user_pwd
                                       , conn_timeout=server['conn_timeout']
                                       , op_timeout=server['op_timeout']
                                       )
                return newconn
            except ( ldap.SERVER_DOWN
                   , ldap.TIMEOUT
                   , ldap.INVALID_CREDENTIALS
                   ), e:
                continue

        # If we get here it means either there are no servers defined or we
        # tried them all. Try to produce a meaningful message and raise
        # an exception.
        if len(self._servers) == 0:
            logger.critical('No servers defined')
        else:
            if e is not None:
                msg_supplement = str(e)
            else:
                msg_supplement = 'n/a'

            err_msg = 'Failure connecting, last attempted server: %s (%s)' % (
                        conn_string, msg_supplement )
            logger.critical(err_msg, exc_info=1)

        if e is not None:
            raise e

        return None


    def handle_referral(self, exception):
        """ Handle a referral specified in a exception """
        payload = exception.args[0]
        info = payload.get('info')
        ldap_url = info[info.find('ldap'):]

        if isLDAPUrl(ldap_url):
            conn_str = LDAPUrl(ldap_url).initializeUrl()

            if self.binduid_usage == 1:
                user_dn = self.bind_dn
                user_pwd = self.bind_pwd
            else:
                user = getSecurityManager().getUser()
                try:
                    user_dn = user.getUserDN()
                    user_pwd = user._getPassword()
                except AttributeError:  # User object is not a LDAPUser
                    user_dn = user_pwd = ''

            return self._connect(conn_str, user_dn, user_pwd)

        else:
            raise ldap.CONNECT_ERROR, 'Bad referral "%s"' % str(exception)


    def _connect( self
                , connection_string
                , user_dn
                , user_pwd
                , conn_timeout=5
                , op_timeout=-1
                ):
        """ Factored out to allow usage by other pieces """
        # Connect to the server to get a raw connection object
        connection = getResource( '%s-connection' % self._hash
                                , c_factory
                                , (connection_string,)
                                )
        if not connection._type is c_factory:
            connection = c_factory(connection_string)

        connection_strings = [self._createConnectionString(s) 
                                            for s in self._servers]

        if connection_string in connection_strings:
            # We only reuse a connection if it is in our own configuration
            # in order to prevent getting "stuck" on a connection created
            # while dealing with a ldap.REFERRAL exception
            setResource('%s-connection' % self._hash, connection)

        # Set the protocol version - version 3 is preferred
        try:
            connection.set_option(ldap.OPT_PROTOCOL_VERSION, ldap.VERSION3)
        except ldap.LDAPError: # Invalid protocol version, fall back safely
            connection.set_option(ldap.OPT_PROTOCOL_VERSION, ldap.VERSION2)

        # Deny auto-chasing of referrals to be safe, we handle them instead
        try:
            connection.set_option(ldap.OPT_REFERRALS, 0)
        except ldap.LDAPError: # Cannot set referrals, so do nothing
            pass

        # Set the connection timeout
        if conn_timeout > 0:
            connection.set_option(ldap.OPT_NETWORK_TIMEOUT, conn_timeout)

        # Set the operations timeout
        if op_timeout > 0:
            connection.timeout = op_timeout

        # Now bind with the credentials given. Let exceptions propagate out.
        connection.simple_bind_s(user_dn, user_pwd)

        return connection


    def search( self
              , base
              , scope
              , filter='(objectClass=*)'
              , attrs=[]
              , bind_dn=''
              , bind_pwd=''
              , convert_filter=True
              ):
        """ The main search engine """
        result = { 'exception' : ''
                 , 'size' : 0
                 , 'results' : []
                 }
        if convert_filter:
            filter = to_utf8(filter)
        base = self._clean_dn(base)

        try:
            connection = self.connect(bind_dn=bind_dn, bind_pwd=bind_pwd)
            if connection is None:
                result['exception'] = 'Cannot connect to LDAP server'
                return result

            try:
                res = connection.search_s(base, scope, filter, attrs)
            except ldap.PARTIAL_RESULTS:
                res_type, res = connection.result(all=0)
            except ldap.REFERRAL, e:
                connection = self.handle_referral(e)

                try:
                    res = connection.search_s(base, scope, filter, attrs)
                except ldap.PARTIAL_RESULTS:
                    res_type, res = connection.result(all=0)

            for rec_dn, rec_dict in res:
                # When used against Active Directory, "rec_dict" may not be
                # be a dictionary in some cases (instead, it can be a list)
                # An example of a useless "res" entry that can be ignored
                # from AD is
                # (None, ['ldap://ForestDnsZones.PORTAL.LOCAL/DC=ForestDnsZones,DC=PORTAL,DC=LOCAL'])
                # This appears to be some sort of internal referral, but
                # we can't handle it, so we need to skip over it.
                try:
                    items =  rec_dict.items()
                except AttributeError:
                    # 'items' not found on rec_dict
                    continue

                for key, value in items:
                    if ( not isinstance(value, str) and 
                         key.lower() not in BINARY_ATTRIBUTES ):
                        try:
                            for i in range(len(value)):
                                value[i] = from_utf8(value[i])
                        except:
                            pass

                rec_dict['dn'] = from_utf8(rec_dn)

                result['results'].append(rec_dict)
                result['size'] += 1

        except ldap.INVALID_CREDENTIALS:
            msg = 'Invalid authentication credentials'
            logger.debug(msg, exc_info=1)
            result['exception'] = msg

        except ldap.NO_SUCH_OBJECT:
            msg = 'Cannot find %s under %s' % (filter, base)
            logger.debug(msg, exc_info=1)
            result['exception'] = msg

        except (ldap.SIZELIMIT_EXCEEDED, ldap.ADMINLIMIT_EXCEEDED):
            msg = 'Too many results for this query'
            logger.warning(msg, exc_info=1)
            result['exception'] = msg

        except (KeyboardInterrupt, SystemExit):
            raise

        except Exception, e:
            msg = str(e)
            logger.error(msg, exc_info=1)
            result['exception'] = msg

        return result


    def insert(self, base, rdn, attrs=None):
        """ Insert a new record """
        if self.read_only:
            msg = 'Running in read-only mode, insertion is disabled'
            logger.info(msg)
            return msg

        msg = ''

        dn = self._clean_dn(to_utf8('%s,%s' % (rdn, base)))
        attribute_list = []
        attrs = attrs and attrs or {}

        for attr_key, attr_val in attrs.items():
            if attr_key.endswith(';binary'):
                is_binary = True
                attr_key = attr_key[:-7]
            else:
                is_binary = False

            if isinstance(attr_val, (str, unicode)) and not is_binary:
                attr_val = [x.strip() for x in attr_val.split(';')]

            if attr_val != ['']:
                if not is_binary:
                    attr_val = map(to_utf8, attr_val)
                attribute_list.append((attr_key, attr_val))

        try:
            connection = self.connect()
            connection.add_s(dn, attribute_list)
        except ldap.INVALID_CREDENTIALS, e:
            e_name = e.__class__.__name__
            msg = '%s No permission to insert "%s"' % (e_name, dn)
        except ldap.ALREADY_EXISTS, e:
            e_name = e.__class__.__name__
            msg = '%s Record with dn "%s" already exists' % (e_name, dn)
        except ldap.REFERRAL, e:
            try:
                connection = self.handle_referral(e)
                connection.add_s(dn, attribute_list)
            except ldap.INVALID_CREDENTIALS:
                e_name = e.__class__.__name__
                msg = '%s No permission to insert "%s"' % (e_name, dn)
            except Exception, e:
                e_name = e.__class__.__name__
                msg = '%s LDAPDelegate.insert: %s' % (e_name, str(e))
        except Exception, e:
            e_name = e.__class__.__name__
            msg = '%s LDAPDelegate.insert: %s' % (e_name, str(e))

        if msg != '':
            logger.info(msg, exc_info=1)

        return msg


    def delete(self, dn):
        """ Delete a record """
        if self.read_only:
            msg = 'Running in read-only mode, deletion is disabled'
            logger.info(msg)
            return msg

        msg = ''
        utf8_dn = self._clean_dn(to_utf8(dn))

        try:
            connection = self.connect()
            connection.delete_s(utf8_dn)
        except ldap.INVALID_CREDENTIALS:
            msg = 'No permission to delete "%s"' % dn
        except ldap.REFERRAL, e:
            try:
                connection = self.handle_referral(e)
                connection.delete_s(utf8_dn)
            except ldap.INVALID_CREDENTIALS:
                msg = 'No permission to delete "%s"' % dn
            except Exception, e:
                msg = 'LDAPDelegate.delete: %s' % str(e)
        except Exception, e:
            msg = 'LDAPDelegate.delete: %s' % str(e)

        if msg != '':
            logger.info(msg, exc_info=1)

        return msg


    def modify(self, dn, mod_type=None, attrs=None):
        """ Modify a record """
        if self.read_only:
            msg = 'Running in read-only mode, modification is disabled'
            logger.info(msg)
            return msg

        utf8_dn = self._clean_dn(to_utf8(dn))
        res = self.search(base=utf8_dn, scope=self.BASE)
        attrs = attrs and attrs or {}

        if res['exception']:
            return res['exception']

        if res['size'] == 0:
            return 'LDAPDelegate.modify: Cannot find dn "%s"' % dn

        cur_rec = res['results'][0]
        mod_list = []
        msg = ''

        for key, values in attrs.items():

            if key.endswith(';binary'):
                key = key[:-7]
            else:
                values = map(to_utf8, values)

            if mod_type is None:
                if cur_rec.get(key, ['']) != values and values != ['']:
                    mod_list.append((self.REPLACE, key, values))
                elif cur_rec.has_key(key) and values == ['']:
                    mod_list.append((self.DELETE, key, None))
            else:
                mod_list.append((mod_type, key, values))

        try:
            connection = self.connect()

            new_rdn = attrs.get(self.rdn_attr, [''])[0]
            if new_rdn and new_rdn != cur_rec.get(self.rdn_attr)[0]:
                raw_utf8_rdn = to_utf8('%s=%s' % (self.rdn_attr, new_rdn))
                new_utf8_rdn = self._clean_rdn(raw_utf8_rdn)
                connection.modrdn_s(utf8_dn, new_utf8_rdn)
                old_dn_exploded = self.explode_dn(utf8_dn)
                old_dn_exploded[0] = new_utf8_rdn
                utf8_dn = ','.join(old_dn_exploded)

            if mod_list:
                connection.modify_s(utf8_dn, mod_list)
            else:
                debug_msg = 'Nothing to modify: %s' % utf8_dn
                logger.debug('LDAPDelegate.modify: %s' % debug_msg)

        except ldap.INVALID_CREDENTIALS, e:
            e_name = e.__class__.__name__
            msg = '%s No permission to modify "%s"' % (e_name, dn)

        except ldap.REFERRAL, e:
            try:
                connection = self.handle_referral(e)
                connection.modify_s(dn, mod_list)
            except ldap.INVALID_CREDENTIALS, e:
                e_name = e.__class__.__name__
                msg = '%s No permission to modify "%s"' % (e_name, dn)
            except Exception, e:
                e_name = e.__class__.__name__
                msg = '%s LDAPDelegate.modify: %s' % (e_name, str(e))

        except Exception, e:
            e_name = e.__class__.__name__
            msg = '%s LDAPDelegate.modify: %s' % (e_name, str(e))

        if msg != '':
            logger.info(msg, exc_info=1)

        return msg


    # Some helper functions and constants that are now on the LDAPDelegate
    # object itself to make it easier to override in subclasses, paving
    # the way for different kinds of delegates.

    ADD = ldap.MOD_ADD
    DELETE = ldap.MOD_DELETE
    REPLACE = ldap.MOD_REPLACE
    BASE = ldap.SCOPE_BASE
    ONELEVEL = ldap.SCOPE_ONELEVEL
    SUBTREE = ldap.SCOPE_SUBTREE

    def _clean_rdn(self, rdn):
        """ Escape all characters that need escaping for a DN, see RFC 2253 """
        if rdn.find('\\') != -1:
            # already escaped, disregard
            return rdn

        try:
            key, val = rdn.split('=')
            val = val.lstrip()
            return '%s=%s' % (key, escape_dn_chars(val))
        except ValueError:
            return rdn

    def _clean_dn(self, dn):
        """ Escape all characters that need escaping for a DN, see RFC 2253 """
        elems = [self._clean_rdn(x) for x in self.explode_dn(dn)]

        return ','.join(elems)


    def explode_dn(self, dn, notypes=0):
        """ Indirection to avoid need for importing ldap elsewhere """
        return ldap.explode_dn(dn, notypes)


    def getScopes(self):
        """ Return simple tuple of ldap scopes

        This method is used to create a simple way to store LDAP scopes as
        numbers by the LDAPUserFolder. The returned tuple is used to find
        a scope by using a integer that is used as an index to the sequence.
        """
        return (self.BASE, self.ONELEVEL, self.SUBTREE)


    def _createConnectionString(self, server_info):
        """ Convert a server info mapping into a connection string
        """
        protocol = server_info['protocol']

        if protocol == 'ldapi':
            hostport = server_info['host']
        else:
            hostport = '%s:%s' % (server_info['host'], server_info['port'])

        ldap_url = LDAPUrl(urlscheme=protocol, hostport=hostport)

        return ldap_url.initializeUrl()



# Register this delegate class with the delegate registry
registerDelegate( 'LDAP delegate'
                , LDAPDelegate
                , 'The default LDAP delegate from the LDAPUserFolder package'
                )
