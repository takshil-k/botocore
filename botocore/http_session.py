import logging
import socket
from base64 import b64encode

from urllib3 import PoolManager, proxy_from_url
from urllib3.exceptions import NewConnectionError

from botocore.vendored import six
from botocore.vendored.six.moves.urllib_parse import unquote
from botocore.awsrequest import AWSResponse
from botocore.compat import filter_ssl_warnings, urlparse
from botocore.exceptions import ConnectionClosedError, EndpointConnectionError

try:
    from urllib3.contrib import pyopenssl
    pyopenssl.extract_from_urllib3()
except ImportError:
    pass

filter_ssl_warnings()
logger = logging.getLogger(__name__)
DEFAULT_TIMEOUT = 60
MAX_POOL_CONNECTIONS = 10
DEFAULT_CA_BUNDLE = 'TODO'

try:
    from certifi import where
except ImportError:
    def where():
        return DEFAULT_CA_BUNDLE


def construct_basic_auth(username, password):
    auth_str = '{0}:{1}'.format(username, password)
    encoded_str = b64encode(auth_str.encode('ascii')).strip().decode()
    return 'Basic {0}'.format(encoded_str)


def get_auth_from_url(url):
    parsed_url = urlparse(url)
    try:
        return unquote(parsed_url.username), unquote(parsed_url.password)
    except (AttributeError, TypeError):
        return None, None


def get_proxy_headers(proxy_url):
    headers = {}
    username, password = get_auth_from_url(proxy_url)
    if username and password:
        basic_auth = construct_basic_auth(username, password)
        headers['Proxy-Authorization'] = basic_auth
    return headers


def fix_proxy_url(proxy_url):
    if proxy_url.startswith('http:') or proxy_url.startswith('https:'):
        return proxy_url
    elif proxy_url.startswith('//'):
        return 'http:' + proxy_url
    else:
        return 'http://' + proxy_url


def get_cert_path(verify):
    if verify is not True:
        return verify

    return where()


class Urllib3Session(object):
    def __init__(self,
                 verify=True,
                 proxies=None,
                 timeout=DEFAULT_TIMEOUT,
                 max_pool_connections=MAX_POOL_CONNECTIONS,
    ):
        self._verify = verify
        self._proxies = proxies or {}
        self._timeout = timeout
        self._max_pool_connections = max_pool_connections
        self._proxy_managers = {}
        self._http_pool = PoolManager(maxsize=self._max_pool_connections)

    def _get_proxy_manager(self, proxy_url):
        proxy_url = fix_proxy_url(proxy_url)
        if proxy_url not in self._proxy_managers:
            proxy_headers = get_proxy_headers(proxy_url)
            self._proxy_managers[proxy_url] = proxy_from_url(
                proxy_url,
                proxy_headers=proxy_headers,
                maxsize=self._max_pool_connections
            )

        return self._proxy_managers[proxy_url]

    def _get_connection(self, url):
        scheme = urlparse(url.lower()).scheme
        proxy = self._proxies.get(scheme)

        if proxy:
            connection_manager = self._get_proxy_manager(proxy)
        else:
            connection_manager = self._http_pool

        return connection_manager.connection_from_url(url)

    def _verify_cert(self, conn, url, verify):
        if url.lower().startswith('https') and verify:
            conn.cert_reqs = 'CERT_REQUIRED'
            conn.ca_certs = get_cert_path(verify)
        else:
            conn.cert_reqs = 'CERT_NONE'
            conn.ca_certs = None

    def send(self, request, streaming=False):
        try:
            conn = self._get_connection(request.url)
            self._verify_cert(conn, request.url, self._verify)
            urllib_response = conn.urlopen(
                method=request.method,
                url=request.url,
                body=request.body,
                headers=request.headers,
                retries=False,
                assert_same_host=False,
                preload_content=False,
                decode_content=False,
            )

            http_response = AWSResponse()
            http_response.url = request.url
            http_response.status_code = urllib_response.status
            http_response.headers = dict(urllib_response.headers.items())
            http_response.raw = urllib_response

            if not streaming:
                # Cause the raw stream to be exhausted immediatly
                http_response.content

            return http_response
        except (NewConnectionError, socket.gaierror) as e:
            raise EndpointConnectionError(endpoint_url=request.url, error=e)
        except six.moves.http_client.BadStatusLine as e:
            raise ConnectionClosedError(
                request=request,
                endpoint_url=request.url
            )
