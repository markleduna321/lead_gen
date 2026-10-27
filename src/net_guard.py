"""Outbound network guard for fetching untrusted (scraped) URLs.

Scraped websites are attacker-controlled. Before the server connects to one,
every address its hostname resolves to must be publicly routable, otherwise a
listing could point the server at localhost, the database, or cloud metadata.

``FilteringProxy`` applies the same rule to every request a headless browser
makes (redirects, iframes, images), and connects to the exact IP it checked so
a hostname cannot swap to a private address between the check and the connect.
"""

import ipaddress
import select
import socket
import socketserver
import threading
import time
from urllib.parse import urljoin, urlsplit

ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})
CONNECT_TIMEOUT = 10
IDLE_TIMEOUT = 30
MAX_HEADER_BYTES = 64 * 1024


class UnsafeTarget(ValueError):
    """The destination is not a public internet address."""


class UnresolvableHost(UnsafeTarget):
    """The host name does not resolve at all."""


def is_public_ip(value) -> bool:
    ip = ipaddress.ip_address(value)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def resolve_public(host: str, port: int, allowed_ports=ALLOWED_PORTS):
    """All resolved socket addresses for host, only if every one is public."""
    if not host:
        raise UnsafeTarget("The address has no host name.")
    if port not in allowed_ports:
        raise UnsafeTarget(f"Port {port} is not allowed.")
    host = host.strip("[]")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as error:
        raise UnresolvableHost(f"Could not resolve {host}.") from error
    if not infos:
        raise UnresolvableHost(f"Could not resolve {host}.")
    for *_, sockaddr in infos:
        if not is_public_ip(sockaddr[0].split("%")[0]):
            raise UnsafeTarget(f"{host} resolves to a non-public address.")
    return [(family, sockaddr) for family, _, _, _, sockaddr in infos]


def check_url(url: str):
    """Validates an http(s) URL and returns its public socket addresses."""
    try:
        parts = urlsplit(str(url))
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as error:
        raise UnsafeTarget("The address is malformed.") from error
    if parts.scheme not in ("http", "https"):
        raise UnsafeTarget("Only http and https addresses are allowed.")
    if parts.username or parts.password:
        raise UnsafeTarget("Addresses with credentials are not allowed.")
    return resolve_public(parts.hostname, port)


def connect_public(host: str, port: int, allowed_ports=ALLOWED_PORTS,
                   timeout: float = CONNECT_TIMEOUT) -> socket.socket:
    last_error = None
    for family, sockaddr in resolve_public(host, port, allowed_ports):
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(sockaddr)
            return sock
        except OSError as error:
            sock.close()
            last_error = error
    raise OSError(f"Could not connect to {host}:{port}") from last_error


def _split_host_port(authority: str, default_port: int):
    parts = urlsplit(f"//{authority}")
    return parts.hostname, parts.port or default_port


def _relay(left: socket.socket, right: socket.socket):
    sockets = [left, right]
    for sock in sockets:
        sock.setblocking(True)
        sock.settimeout(None)
    while True:
        readable, _, errored = select.select(sockets, [], sockets, IDLE_TIMEOUT)
        if errored or not readable:
            return
        for source in readable:
            data = source.recv(65536)
            if not data:
                return
            (right if source is left else left).sendall(data)


class _ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        client.settimeout(CONNECT_TIMEOUT)
        try:
            buffer = b""
            while b"\r\n\r\n" not in buffer:
                chunk = client.recv(8192)
                if not chunk:
                    return
                buffer += chunk
                if len(buffer) > MAX_HEADER_BYTES:
                    return self._reply(400, "Bad Request")
            head, body = buffer.split(b"\r\n\r\n", 1)
            request_line, *header_lines = head.decode("latin-1").split("\r\n")
            method, target, version = request_line.split(" ", 2)
            if method.upper() == "CONNECT":
                host, port = _split_host_port(target, 443)
                upstream = connect_public(host, port)
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if body:
                    upstream.sendall(body)
            else:
                parts = urlsplit(target)
                if parts.scheme != "http" or not parts.hostname:
                    return self._reply(400, "Bad Request")
                upstream = connect_public(parts.hostname, parts.port or 80)
                path = parts.path or "/"
                if parts.query:
                    path += f"?{parts.query}"
                headers = [line for line in header_lines
                           if line.split(":", 1)[0].strip().lower()
                           not in ("proxy-connection", "connection", "keep-alive")]
                upstream.sendall("\r\n".join([f"{method} {path} {version}", *headers,
                                              "Connection: close", "", ""]).encode("latin-1") + body)
        except UnsafeTarget:
            return self._reply(403, "Forbidden")
        except (OSError, ValueError):
            return self._reply(502, "Bad Gateway")
        try:
            _relay(client, upstream)
        except OSError:
            pass
        finally:
            upstream.close()

    def _reply(self, status, reason):
        marker = "X-Net-Guard: blocked\r\n" if status == 403 else ""
        try:
            self.request.sendall(f"HTTP/1.1 {status} {reason}\r\nContent-Length: 0\r\n{marker}"
                                 "Connection: close\r\n\r\n".encode())
        except OSError:
            pass


class _ThreadingServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class FilteringProxy:
    """A loopback-only HTTP/CONNECT proxy that only reaches public addresses."""

    def __init__(self):
        self._server = _ThreadingServer(("127.0.0.1", 0), _ProxyHandler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, name="net-guard-proxy",
                         daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self._server.shutdown()
        self._server.server_close()


_shared_proxy = None
_shared_lock = threading.Lock()


def shared_proxy() -> FilteringProxy:
    global _shared_proxy
    with _shared_lock:
        if _shared_proxy is None:
            _shared_proxy = FilteringProxy()
        return _shared_proxy


MAX_FETCH_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
FETCH_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                 "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"}


class FetchError(ValueError):
    """The page could not be fetched within the safety limits."""


class SafeResponse:
    def __init__(self, url, status_code, headers, content, encoding):
        self.url = url
        self.status_code = status_code
        self.headers = headers
        self.content = content
        self.encoding = encoding or "utf-8"

    @property
    def content_type(self) -> str:
        return self.headers.get("Content-Type", "").lower()

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type

    @property
    def text(self) -> str:
        try:
            return self.content.decode(self.encoding, errors="ignore")
        except LookupError:
            return self.content.decode("utf-8", errors="ignore")


def site_host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _read_capped(response, max_bytes: int, deadline: float) -> bytes:
    declared = response.headers.get("Content-Length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise FetchError("The page is larger than the download limit.")
    chunks, size = [], 0
    for chunk in response.iter_content(chunk_size=65536):
        size += len(chunk)
        if size > max_bytes:
            raise FetchError("The page is larger than the download limit.")
        if time.monotonic() > deadline:
            raise FetchError("The page took too long to download.")
        chunks.append(chunk)
    return b"".join(chunks)


def safe_get(url: str, *, timeout: float = 6, max_bytes: int = MAX_FETCH_BYTES,
             max_redirects: int = MAX_REDIRECTS, accept_redirect=None) -> SafeResponse:
    """GETs an untrusted URL through the filtering proxy.

    Only public addresses are reachable (checked again at connect time by the
    proxy, which defeats DNS rebinding). Redirects stay on the same site
    (example.com <-> www.example.com), the body is capped at ``max_bytes`` and
    the whole fetch must finish within ``timeout`` seconds per hop.
    Raises ``UnsafeTarget`` for blocked destinations and ``FetchError`` or a
    ``requests`` exception for other failures.
    """
    import requests

    check_url(url)
    origin = site_host(url)
    proxy = shared_proxy().url
    with requests.Session() as session:
        session.trust_env = False
        session.proxies = {"http": proxy, "https": proxy}
        for _ in range(max_redirects + 1):
            deadline = time.monotonic() + timeout
            try:
                with session.get(url, headers=FETCH_HEADERS, timeout=(timeout, timeout),
                                 allow_redirects=False, stream=True) as response:
                    if response.headers.get("X-Net-Guard") == "blocked":
                        raise UnsafeTarget("The destination was refused by the network guard.")
                    if response.status_code in REDIRECT_STATUSES:
                        target = urljoin(url, response.headers.get("Location", ""))
                        check_url(target)
                        if site_host(target) != origin or (accept_redirect and not accept_redirect(target)):
                            raise UnsafeTarget("The page redirected to a different website.")
                        url = target
                        continue
                    content = _read_capped(response, max_bytes, deadline)
                    return SafeResponse(url, response.status_code, response.headers,
                                        content, response.encoding)
            except requests.exceptions.ProxyError as error:
                raise UnsafeTarget("The destination was refused by the network guard.") from error
    raise FetchError("The page redirected too many times.")
