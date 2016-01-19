# Connection brokers facilitate & manage incoming and outgoing connections.
#
# The idea is that code actually tells us what it wants to do, so we can
# choose an appropriate mechanism for connecting or receiving incoming
# connections.
#
# Libraries which use socket.create_connection can be monkey-patched
# to use a broker on a connection-by-connection bases like so:
#
#     with broker.context(need=[broker.OUTGOING_CLEARTEXT,
#                               broker.OUTGOING_SMTP]) as ctx:
#         conn = somelib.connect(something)
#         print 'Connected with encryption: %s' % ctx.encryption
#
# The context variable will then contain metadata about what sort of
# connection was made.
#
# See the Capability class below for a list of attributes that can be
# used to describe an outgoing (or incoming) connection.
#
# In particular, using the master broker will implement a prioritised
# connection strategy where the most secure options are tried first and
# things gracefully degrade. Protocols like IMAP, SMTP or POP3 will be
# transparently upgraded to use STARTTLS.
#
# TODO:
#    - Implement a TorBroker
#    - Implement a PageKiteBroker
#    - Implement HTTP/SMTP/IMAP/POP3 TLS upgrade-brokers
#    - Prevent unbrokered socket.socket connections
#
import socket
import ssl
import sys
import threading
import traceback

# Import SOCKS proxy support...
try:
    import sockschain as socks
except ImportError:
    try:
        import socks
    except ImportError:
        socks = None


KNOWN_ONION_MAP = {
    'www.mailpile.is': 'clgs64523yi2bkhz.onion'
}


org_cconn = socket.create_connection
org_sslwrap = ssl.wrap_socket
try:
    org_context_wrap_socket = ssl.SSLContext.wrap_socket
    have_ssl_context = True
except AttributeError:
    org_context_wrap_socket = None
    have_ssl_context = False

monkey_lock = threading.RLock()


class Capability(object):
    """
    These are constants defining different types of outgoing or incoming
    connections. Brokers use these to describe what sort of connections they
    are capable of handling, and calling code uses these to describe the
    intent of network connection.
    """
    OUTGOING_RAW = 'o:raw'      # Request this to avoid meddling brokers
    OUTGOING_ENCRYPTED = 'o:e'  # Request this if sending encrypted data
    OUTGOING_CLEARTEXT = 'o:c'  # Request this if sending clear-text data
    OUTGOING_TRACKABLE = 'o:t'  # Reject this to require anonymity
    OUTGOING_SMTP = 'o:smtp'    # These inform brokers what protocol is being
    OUTGOING_IMAP = 'o:imap'    # .. used, to allow protocol-specific features
    OUTGOING_POP3 = 'o:pop3'    # .. such as enabling STARTTLS or upgrading
    OUTGOING_HTTP = 'o:http'    # .. HTTP to HTTPS.
    OUTGOING_HTTPS = 'o:https'  # ..

    INCOMING_RAW = 20
    INCOMING_LOCALNET = 21
    INCOMING_INTERNET = 22
    INCOMING_DARKNET = 23
    INCOMING_SMTP = 24
    INCOMING_IMAP = 25
    INCOMING_POP3 = 26
    INCOMING_HTTP = 27
    INCOMING_HTTPS = 28

    ALL_OUTGOING = set([OUTGOING_RAW, OUTGOING_ENCRYPTED, OUTGOING_CLEARTEXT,
                        OUTGOING_TRACKABLE,
                        OUTGOING_SMTP, OUTGOING_IMAP, OUTGOING_POP3,
                        OUTGOING_HTTP, OUTGOING_HTTPS])

    ALL_OUTGOING_ENCRYPTED = set([OUTGOING_RAW, OUTGOING_TRACKABLE,
                                  OUTGOING_ENCRYPTED, OUTGOING_HTTPS])

    ALL_INCOMING = set([INCOMING_RAW, INCOMING_LOCALNET, INCOMING_INTERNET,
                        INCOMING_DARKNET, INCOMING_SMTP, INCOMING_IMAP,
                        INCOMING_POP3, INCOMING_HTTP, INCOMING_HTTPS])


class CapabilityFailure(IOError):
    """
    This exception is raised when capability requirements can't be satisfied.
    It extends the IOError, so unaware code just thinks the network is lame.

    >>> try:
    ...     raise CapabilityFailure('boo')
    ... except IOError:
    ...     print 'ok'
    ok
    """
    pass


class Url(str):
    def __init__(self, *args, **kwargs):
        str.__init__(self, *args, **kwargs)
        self.encryption = None
        self.anonymity = None
        self.on_internet = False
        self.on_localnet = False
        self.on_darknet = None


class BrokeredContext(object):
    """
    This is the context returned by the BaseConnectionBroker.context()
    method. It takes care of monkey-patching the socket.create_connection
    method and then cleaning the mess up afterwards, and collecting metadata
    from the brokers describing what sort of connection was established.

    WARNING: In spite of our best efforts (locking, etc.), mixing brokered
             and unbrokered code will not work well at all. The patching
             approach also limits us to initiating one outgoing connection
             at a time.
    """
    def __init__(self, broker, need=None, reject=None, oneshot=False):
        self._broker = broker
        self._need = need
        self._reject = reject
        self._oneshot = oneshot
        self._monkeys = []
        self._reset()

    def _reset(self):
        self.encryption = None
        self.anonymity = None
        self.on_internet = False
        self.on_localnet = False
        self.on_darknet = None

    def _unmonkey(self):
        if self._monkeys:
            (socket.create_connection, ) = self._monkeys
            self._monkeys = []
            monkey_lock.release()

    def __enter__(self, *args, **kwargs):
        monkey_lock.acquire()
        self._monkeys = (socket.create_connection, )
        def create_brokered_conn(address, *a, **kw):
            self._reset()
            try:
                return self._broker.create_conn_with_caps(
                    address, self, self._need, self._reject, *a, **kw)
            finally:
                if self._oneshot:
                    self._unmonkey()
        socket.create_connection = create_brokered_conn
        return self

    def __exit__(self, *args, **kwargs):
        self._unmonkey()


class BaseConnectionBroker(Capability):
    """
    This is common code used by most of the connection brokers.
    """
    SUPPORTS = []

    def __init__(self, master=None):
        self.supports = list(self.SUPPORTS)[:]
        self.master = master
        self._config = None
        self._debug = master._debug if (master is not None) else None

    def configure(self):
        self.supports = list(self.SUPPORTS)[:]

    def set_config(self, config):
        self._config = config
        self.configure()

    def config(self):
        if self._config is not None:
            return self._config
        if self.master is not None:
            return self.master.config()
        return None

    def _raise_or_none(self, exc, why):
        if exc is not None:
            raise exc(why)
        return None

    def _check(self, need, reject, _raise=CapabilityFailure):
        for n in need or []:
            if n not in self.supports:
                if self._debug is not None:
                    self._debug('%s: lacking capabilty %s' % (self, n))
                return self._raise_or_none(_raise, 'Lacking %s' % n)
        for n in reject or []:
            if n in self.supports:
                if self._debug is not None:
                    self._debug('%s: unwanted capabilty %s' % (self, n))
                return self._raise_or_none(_raise, 'Unwanted %s' % n)
        if self._debug is not None:
            self._debug('%s: checks passed!' % (self, ))
        return self

    def _describe(self, context, conn):
        return conn

    def debug(self, val):
        self._debug = val
        return self

    def context(self, need=None, reject=None, oneshot=False):
        return BrokeredContext(self, need=need, reject=reject, oneshot=oneshot)

    def create_conn_with_caps(self, address, context, need, reject,
                              *args, **kwargs):
        conn = self._check(need, reject)._create_connection(context, address,
                                                            *args, **kwargs)
        return self._describe(context, conn)

    def create_connection(self, address, *args, **kwargs):
        n = kwargs.get('need', None)
        r = kwargs.get('reject', None)
        c = kwargs.get('context', None)
        for kw in ('need', 'reject', 'context'):
            if kw in kwargs:
                del kwargs[kw]
        return self.create_conn_with_caps(address, c, n, r, *args, **kwargs)

    # Should implement socket.create_connection or an equivalent.
    # Context, if not None, should be informed with metadata about the
    # connection.
    def _create_connection(self, context, address, *args, **kwargs):
        raise NotImplementedError('Subclasses override this')

    def get_urls(self, listening_fd,
                 need=None, reject=None, **kwargs):
        try:
            return self._check(need, reject)._get_urls(listening_fd, **kwargs)
        except CapabilityFailure:
            return []

    # Returns a list of Url objects for this listener
    def _get_urls(self, listening_fd,
                  proto=None, username=None, password=None):
        raise NotImplementedError('Subclasses override this')


class TcpConnectionBroker(BaseConnectionBroker):
    """
    The basic raw TCP/IP connection broker.

    The only clever thing this class does, is to avoid trying to connect
    to .onion addresses, preventing that from leaking over DNS.
    """
    SUPPORTS = (
        # Normal TCP/IP is not anonymous, and we do not have incoming
        # capability unless we have a public IP.
        (Capability.ALL_OUTGOING) |
        (Capability.ALL_INCOMING - set([Capability.INCOMING_INTERNET]))
    )

    DEBUG_FMT = '%s: Raw TCP conn to: %s'

    def configure(self):
        BaseConnectionBroker.configure(self)
        # FIXME: If our config indicates we have a public IP, add the
        #        INCOMING_INTERNET capability.
        # FIXME: If our coonfig indicates that the user does not care
        #        about anonymity at all, remove OUTGOING_TRACKABLE.
        if (self.config().sys.proxy.protocol != 'none' and
                not self.config().sys.proxy.fallback):
            self.supports = []

    def _describe(self, context, conn):
        context.encryption = None
        context.is_internet = True
        return conn

    def _avoid(self, address):
        if address[0].endswith('.onion'):
            raise CapabilityFailure('Cannot connect to .onion addresses')

    def _conn(self, address, *args, **kwargs):
        return org_cconn(address, *args, **kwargs)

    def _create_connection(self, context, address, *args, **kwargs):
        self._avoid(address)
        if self._debug is not None:
            self._debug(self.DEBUG_FMT % (self, address))
        return self._conn(address, *args, **kwargs)


class SocksConnBroker(TcpConnectionBroker):
    """
    This broker offers the same services as the TcpConnBroker, but over a
    SOCKS connection.
    """
    SUPPORTS = []
    CONFIGURED = Capability.ALL_OUTGOING
    DEBUG_FMT = '%s: Raw SOCKS5 conn to: %s'
    PROXY_TYPES = ('socks5', 'http', 'socks4')

    def __init__(self, *args, **kwargs):
        TcpConnectionBroker.__init__(self, *args, **kwargs)
        self.proxy_config = None
        self.typemap = {}

    def configure(self):
        BaseConnectionBroker.configure(self)
        if self.config().sys.proxy.protocol in self.PROXY_TYPES:
            self.proxy_config = self.config().sys.proxy
            self.supports = list(self.CONFIGURED)[:]
            self.typemap = {
                'socks5': socks.PROXY_TYPE_SOCKS5,
                'socks4': socks.PROXY_TYPE_SOCKS4,
                'http': socks.PROXY_TYPE_HTTP,
                'tor': socks.PROXY_TYPE_SOCKS5  # For TorConnBrokerk
            }
        else:
            self.proxy_config = None
            self.supports = []

    def _auth_args(self):
        return {
            'username': self.proxy_config.username or None,
            'password': self.proxy_config.username or None
        }

    def _fix_address_tuple(self, address):
        return (str(address[0]), address[1])

    def _conn(self, address, timeout=None, source_address=None):
        sock = socks.socksocket()
        sock.setproxy(proxytype=self.typemap[self.proxy_config.protocol],
                      addr=self.proxy_config.host,
                      port=self.proxy_config.port,
                      rdns=True,
                      **self._auth_args())
        if timeout and timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
            sock.settimeout(float(timeout))
        if source_address:
            raise IOError('Cannot bind source address')
        try:
            address = self._fix_address_tuple(address)
            sock.connect(address)
        except socks.ProxyError:
            if self._debug is not None:
                self._debug(traceback.format_exc())
            raise IOError('Proxy failed for %s:%s' % address)
        return sock


class TorConnBroker(SocksConnBroker):
    """
    This broker offers the same services as the TcpConnBroker, but over Tor.

    This removes the "trackable" capability, so requests that reject it can
    find their way here safely...

    This broker only volunteers to carry encrypted traffic, because Tor
    exit nodes may be hostile.
    """
    SUPPORTS = []
    CONFIGURED = (Capability.ALL_OUTGOING_ENCRYPTED
                  - set([Capability.OUTGOING_TRACKABLE]))
    REJECTS = None
    DEBUG_FMT = '%s: Raw Tor conn to: %s'
    PROXY_TYPES = ('tor', )

    def _auth_args(self):
        # FIXME: Tor uses the auth information as a signal to change
        #        circuits. We may have use for this at some point.
        return {}

    def _fix_address_tuple(self, address):
        host = str(address[0])
        return (KNOWN_ONION_MAP.get(host.lower(), host), address[1])

    def _avoid(self, address):
        pass


class TorOnionBroker(TorConnBroker):
    """
    This broker offers the same services as the TcpConnBroker, but over Tor.

    This removes the "trackable" capability, so requests that reject it can
    find their way here safely...

    This differs from the TorConnBroker in that it will allow "cleartext"
    traffic, since we trust the traffic never leaves the Tor network and
    we don't have hostile exits to worry about.
    """
    SUPPORTS = []
    CONFIGURED = (Capability.ALL_OUTGOING
                  - set([Capability.OUTGOING_TRACKABLE]))
    REJECTS = None
    DEBUG_FMT = '%s: Tor onion conn to: %s'
    PROXY_TYPES = ('tor', )

    def _avoid(self, address):
        host = KNOWN_ONION_MAP.get(address[0], address[0])
        if not host.endswith('.onion'):
            raise CapabilityFailure('Can only connect to .onion addresses')


class BaseConnectionBrokerProxy(TcpConnectionBroker):
    """
    Brokers based on this establish a RAW connection and then manipulate it
    in some way, generally to implement proxying or TLS wrapping.
    """
    SUPPORTS = []
    WANTS = [Capability.OUTGOING_RAW]
    REJECTS = None
    SSL_VERSION = ssl.PROTOCOL_TLSv1

    def _proxy_address(self, address):
        return address

    def _proxy(self, conn):
        raise NotImplementedError('Subclasses override this')

    def _wrap_ssl(self, conn):
        if self._debug is not None:
            self._debug('%s: Wrapping socket with SSL' % (self, ))
        # FIXME: We're losing the SNI stuff here, which is super lame.
        #        We should on Python 2.7.9+ try to fix that somehow.
        return org_sslwrap(conn, None, None, ssl_version=self.SSL_VERSION)

    def _create_connection(self, context, address, *args, **kwargs):
        address = self._proxy_address(address)
        if self.master:
            conn = self.master.create_conn_with_caps(
                address, context, self.WANTS, self.REJECTS, *args, **kwargs)
        else:
            conn = TcpConnectionBroker._create_connection(self, context,
                                                          address,
                                                          *args, **kwargs)
        return self._proxy(conn)


class AutoHttpsConnBroker(BaseConnectionBrokerProxy):
    """
    This broker tries to auto-upgrade HTTP connections to HTTPS.
    """
    SUPPORTS = [Capability.OUTGOING_HTTP, Capability.OUTGOING_HTTPS]
    WANTS = [Capability.OUTGOING_RAW, Capability.OUTGOING_ENCRYPTED]

    def _describe(self, context, conn):
        context.encryption = conn.cipher()
        return conn

    def _proxy_address(self, address):
        if address[0].endswith('.onion'):
            raise CapabilityFailure('I do not like .onion addresses')
        if int(address[1]) == 80:
            return (address[0], 443)
        return address

    def _proxy(self, conn):
        assert(self.SSL_VERSION == ssl.PROTOCOL_TLSv1)
        return self._wrap_ssl(conn)


class AutoSmtpStartTLSConnBroker(BaseConnectionBrokerProxy):
    pass


class AutoImapStartTLSConnBroker(BaseConnectionBrokerProxy):
    pass


class AutoPop3StartTLSConnBroker(BaseConnectionBrokerProxy):
    pass


class MasterBroker(BaseConnectionBroker):
    """
    This is the master broker. It implements a prioritised list of
    connection brokers, each of which is tried in turn until a match
    is found. As such, more secure brokers should register themselves
    with a higher priority - if they fail, we fall back to less
    secure connection strategies.
    """
    def __init__(self, *args, **kwargs):
        BaseConnectionBroker.__init__(self, *args, **kwargs)
        self.brokers = []
        self._debug = self._debugger
        self.debug_callback = None

    def configure(self):
        for prio, cb in self.brokers:
            cb.configure()

    def _debugger(self, *args, **kwargs):
        if self.debug_callback is not None:
            self.debug_callback(*args, **kwargs)

    def register_broker(self, priority, cb):
        """
        Brokers should register themselves with priorities as follows:

           - 1000-1999: Content-agnostic raw connections
           - 3000-3999: Secure network layers: VPNs, Tor, I2P, ...
           - 5000-5999: Proxies required to reach the wider Internet
           - 7000-7999: Protocol enhancments (non-securit related)
           - 9000-9999: Security-related protocol enhancements

        """
        self.brokers.append((priority, cb(master=self)))
        self.brokers.sort()
        self.brokers.reverse()

    def create_conn_with_caps(self, address, context, need, reject,
                              *args, **kwargs):
        et = v = t = None
        for prio, cb in self.brokers:
            try:
                conn = cb.debug(self._debug).create_conn_with_caps(
                    address, context, need, reject, *args, **kwargs)
                if conn:
                    return conn
            except (IOError, NotImplementedError) as e:
                et, v, t = sys.exc_info()
        if et is not None:
            raise et, v, t
        raise CapabilityFailure('No broker found')

    def get_urls(self, listening_fd, need=None, reject=None):
        urls = []
        for prio, cb in self.brokers:
            urls.extend(cb.debug(self._debug).get_urls(listening_fd))
        return urls


def DisableUnbrokeredConnections():
    """Enforce the use of brokers EVERYWHERE!"""
    def CreateConnWarning(*args, **kwargs):
        print '*** socket.create_connection used without a broker ***'
        traceback.print_stack()

        # FIXME: For now we just complain and let it go
        return org_cconn(*args, **kwargs)

        raise IOError('FIXME: Please use within a broker context')
    socket.create_connection = CreateConnWarning


if __name__ != "__main__":
    Master = MasterBroker()
    register = Master.register_broker
    register(1000, TcpConnectionBroker)
    register(9500, AutoHttpsConnBroker)
    register(9500, AutoSmtpStartTLSConnBroker)
    register(9500, AutoImapStartTLSConnBroker)
    register(9500, AutoPop3StartTLSConnBroker)

    if socks is not None:
        register(1500, SocksConnBroker)
        register(3500, TorConnBroker)
        register(3500, TorOnionBroker)

    def SslWrapOnlyOnce(sock, *args, **kwargs):
        """
        Since we like to wrap things our own way, this make ssl.wrap_socket
        into a no-op in the cases where we've alredy wrapped a socket.
        """
        if isinstance(sock, ssl.SSLSocket):
            return sock
        return org_sslwrap(sock, *args, **kwargs)

    ssl.wrap_socket = SslWrapOnlyOnce

    if have_ssl_context:
        # Same again with SSLContext, if we have it.

        def SslContextWrapOnlyOnce(self, sock, *args, **kwargs):
            if isinstance(sock, ssl.SSLSocket):
                return sock
            return org_context_wrap_socket(self, sock, *args, **kwargs)

        ssl.SSLContext.wrap_socket = SslContextWrapOnlyOnce

else:
    import doctest
    import sys
    results = doctest.testmod(optionflags=doctest.ELLIPSIS,
                              extraglobs={})
    print '%s' % (results, )
    if results.failed:
        sys.exit(1)
