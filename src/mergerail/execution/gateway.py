"""Bounded HTTP gateway for the Docker agent's fixed AI origin.

DNS resolves once to a public address; TLS retains the fixed upstream hostname.
Literal API routes, strict framing, restricted headers, and bounded response
streams prevent the client from selecting another destination. Credentials and
request bodies are never logged.
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import os
import re
import signal
import socket
import socketserver
import ssl
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from email.message import Message
from http.client import HTTPException, HTTPResponse, HTTPSConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
BLOCK_BYTES = 64 * 1024
BODY_DEADLINE_SECONDS = 60.0
UPSTREAM_SOCKET_TIMEOUT = 30.0
UPSTREAM_TOTAL_TIMEOUT = 600.0
MAX_CONCURRENT_REQUESTS = 2
LISTEN_BACKLOG = 8
UPSTREAM_PORT = 443
MAX_CREDENTIAL_FILE_BYTES = 64 * 1024
MAX_AUTHORIZATION_LENGTH = 8 * 1024
MAX_ACCOUNT_ID_LENGTH = 256
DIAGNOSTICS_PATH = "/tmp/mergerail-gateway-diagnostics.jsonl"
_DIAGNOSTICS_LOCK = threading.Lock()

FORWARDED_HEADERS = (
    "accept",
    "anthropic-beta",
    "anthropic-version",
    "authorization",
    "content-type",
)
PROXY_ENVIRONMENT = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
)
ROUTES = (
    ("GET", "/models"),
    ("POST", "/chat/completions"),
    ("POST", "/responses"),
    ("POST", "/messages"),
)
CONTENT_LENGTH = re.compile(r"(?:0|[1-9][0-9]{0,9})")
HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
HOSTNAME = re.compile(r"[a-z0-9.-]+")
PREFIX_SEGMENT = re.compile(r"[A-Za-z0-9._~-]+")
BEARER_AUTHORIZATION = re.compile(r"Bearer [A-Za-z0-9._~+/\-]+=*")

# Denied explicitly instead of relying only on is_global, whose coverage differs
# between Python releases. Cloud metadata lives in link-local, CGNAT, ULA and NAT64
# ranges, all of which appear here.
BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "255.255.255.255/32",
        "::/128",
        "::1/128",
        "::ffff:0:0/96",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/32",
        "2002::/16",
        "fc00::/7",
        "fec0::/10",
        "fe80::/10",
        "ff00::/8",
    )
)

BUSY_BODY = b"too many concurrent requests\n"
BUSY_RESPONSE = b"".join(
    (
        b"HTTP/1.1 503 Service Unavailable\r\n",
        b"Content-Type: text/plain; charset=utf-8\r\n",
        b"Content-Length: %d\r\n" % len(BUSY_BODY),
        b"Connection: close\r\n",
        b"\r\n",
        BUSY_BODY,
    )
)


class ConfigError(ValueError):
    """Startup refused: the controller supplied unusable settings."""


class RequestError(Exception):
    """A client request is refused before anything is forwarded."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class UpstreamError(Exception):
    """The pinned upstream cannot serve the request."""


@dataclass(frozen=True)
class Route:
    """One exact request target, with the path the upstream receives."""

    method: str
    path: str
    name: str


@dataclass(frozen=True)
class Upstream:
    """A controller-selected destination pinned to one literal address."""

    host: str
    family: socket.AddressFamily
    address: str

    @property
    def sockaddr(self) -> tuple[int, str, int]:
        return (self.family.value, self.address, UPSTREAM_PORT)


def log(message: str) -> None:
    print(f"docker_ai_gateway: {message}", file=sys.stderr, flush=True)


def record_close(reason: str, *, status: int = 0, size: int = 0) -> None:
    """Keep a bounded, payload-free journal for the controller before teardown."""
    data = (json.dumps({"close_reason": reason, "status": status, "bytes": size}) + "\n").encode()
    with _DIAGNOSTICS_LOCK, contextlib.suppress(OSError):
        fd = os.open(
            DIAGNOSTICS_PATH,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(fd, "ab") as journal:
            if os.fstat(journal.fileno()).st_size + len(data) <= 16384:
                journal.write(data)


def is_header_value_safe(value: str) -> bool:
    """A value that can be written into a request or response line safely."""

    return bool(value) and all(
        ord(character) >= 32 and ord(character) != 127 for character in value
    )


def is_public_unicast(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for globally routable unicast addresses of a single family."""

    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None or address.sixtofour is not None:
            return False
        if address.teredo is not None:
            return False
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return False
    if any(address in network for network in BLOCKED_NETWORKS):
        return False
    return address.is_global


def validate_upstream_host(value: str) -> str:
    """Accept a plain ASCII DNS name only: no scheme, port, path or literal IP."""

    host = value.strip().lower().rstrip(".")
    if "." not in host or not HOSTNAME.fullmatch(host):
        raise ConfigError("upstream host must be a plain ASCII DNS name")
    if not all(HOST_LABEL.fullmatch(label) for label in host.split(".")):
        raise ConfigError("upstream host contains an invalid DNS label")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ConfigError("upstream host must be a name, not an address literal")


def validate_prefix(value: str) -> str:
    """Accept a fixed path prefix built from literal, traversal free segments."""

    prefix = value.strip()
    if not prefix.startswith("/") or prefix.endswith("/"):
        raise ConfigError("upstream prefix must start with '/' and must not end with '/'")
    segments = prefix[1:].split("/")
    if len(segments) > 4 or not all(
        PREFIX_SEGMENT.fullmatch(segment) and segment not in {".", ".."} for segment in segments
    ):
        raise ConfigError("upstream prefix contains an unsafe path segment")
    return prefix


def validate_bind(value: str) -> str:
    """Accept the literal address of one interface, never a wildcard."""

    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError as error:
        raise ConfigError("bind must be a literal IP address") from error
    if address.is_unspecified or address.is_multicast:
        raise ConfigError("bind must be a specific unicast address")
    return str(address)


def host_forms(address: str, port: int) -> tuple[str, ...]:
    """Accepted Host values: with the port, and bare only when the port is 80."""

    literal = f"[{address}]" if ":" in address else address
    authority = f"{literal}:{port}"
    return (authority, literal) if port == 80 else (authority,)


def build_routes(prefix: str) -> dict[str, Route]:
    """The complete set of accepted request targets for a validated prefix."""

    return {prefix + path: Route(method, prefix + path, path[1:]) for method, path in ROUTES}


def validate_credentials(value: object) -> dict[str, str]:
    """Validate the small credential object without including its values in errors."""

    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ConfigError("credentials must be a JSON object")
    if set(value) - {"authorization", "chatgpt-account-id"}:
        raise ConfigError("credentials contain an unknown field")
    authorization = value.get("authorization")
    if (
        not isinstance(authorization, str)
        or len(authorization) > MAX_AUTHORIZATION_LENGTH
        or not BEARER_AUTHORIZATION.fullmatch(authorization)
    ):
        raise ConfigError("credentials require a valid Bearer authorization value")
    result = {"authorization": authorization}
    if "chatgpt-account-id" in value:
        account_id = value["chatgpt-account-id"]
        if (
            not isinstance(account_id, str)
            or not account_id
            or len(account_id) > MAX_ACCOUNT_ID_LENGTH
            or not account_id.isascii()
            or not is_header_value_safe(account_id)
            or account_id != account_id.strip()
        ):
            raise ConfigError("credentials contain an invalid ChatGPT account ID")
        result["chatgpt-account-id"] = account_id
    return result


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate credential keys instead of silently choosing one."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("credentials contain a duplicate field")
        result[key] = value
    return result


def load_credentials(path: str) -> dict[str, str]:
    """Load a bounded JSON credential file without exposing contents in errors."""

    try:
        with open(path, "rb") as source:
            payload = source.read(MAX_CREDENTIAL_FILE_BYTES + 1)
    except OSError:
        raise ConfigError("cannot read credentials file") from None
    if len(payload) > MAX_CREDENTIAL_FILE_BYTES:
        raise ConfigError("credentials file is too large")
    try:
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ConfigError("credentials file is malformed") from None
    return validate_credentials(document)


def validate_credential_destination(host: str, prefix: str) -> None:
    """Allow only the two fixed OpenAI credential destinations."""

    if (host, prefix) not in {
        ("api.openai.com", "/v1"),
        ("chatgpt.com", "/backend-api/codex"),
    }:
        raise ConfigError(
            "credential mode requires api.openai.com with /v1 or "
            "chatgpt.com with /backend-api/codex"
        )


def resolve_route(routes: Mapping[str, Route], method: str, target: str) -> Route:
    """Match the raw request target against the fixed table, byte for byte."""

    route = routes.get(target)
    if route is None:
        raise RequestError(404, "no such endpoint")
    if route.method != method:
        raise RequestError(405, "method not allowed for this endpoint")
    return route


def check_host(message: Message, allowed: Sequence[str]) -> None:
    """Refuse a missing, duplicated or foreign Host."""

    hosts = message.get_all("Host") or []
    if len(hosts) != 1:
        raise RequestError(400, "exactly one host header is required")
    if (hosts[0] or "").strip() not in allowed:
        raise RequestError(400, "host header does not name this gateway")


def collect_headers(
    message: Message, allowed: Sequence[str], *, credentialed: bool = False
) -> dict[str, str]:
    """Return the only client headers allowed to reach the upstream."""

    check_host(message, allowed)
    if message.get("Transfer-Encoding") is not None:
        raise RequestError(400, "transfer encoding is not allowed")
    if message.get("Upgrade") is not None:
        raise RequestError(400, "protocol upgrade is not allowed")
    if "upgrade" in (message.get("Connection") or "").lower():
        raise RequestError(400, "connection upgrade is not allowed")
    forwarded: dict[str, str] = {}
    allowed_headers = ("accept", "content-type") if credentialed else FORWARDED_HEADERS
    for name in allowed_headers:
        values = message.get_all(name) or []
        if len(values) > 1:
            raise RequestError(400, f"duplicate {name} header")
        if not values:
            continue
        value = (values[0] or "").strip()
        if not is_header_value_safe(value):
            raise RequestError(400, f"invalid {name} header value")
        forwarded[name] = value
    return forwarded


def parse_content_length(values: Sequence[str]) -> int:
    """Accept one non-negative decimal length inside the body budget."""

    if len(values) > 1:
        raise RequestError(400, "duplicate content-length header")
    if not values:
        return 0
    value = (values[0] or "").strip()
    if not CONTENT_LENGTH.fullmatch(value):
        raise RequestError(400, "invalid content-length header")
    length = int(value)
    if length > MAX_BODY_BYTES:
        raise RequestError(413, "request body is too large")
    return length


def build_upstream_headers(
    host: str,
    forwarded: Mapping[str, str],
    length: int,
    *,
    credentials: Mapping[str, str] | None = None,
) -> list[tuple[str, str]]:
    """Build the complete upstream header list; nothing else is ever sent."""

    headers = [
        ("Host", host),
        ("Content-Length", str(length)),
        ("Accept-Encoding", "identity"),
    ]
    if credentials is None:
        headers.extend(forwarded.items())
    else:
        headers.extend(
            (name, value) for name, value in forwarded.items() if name in {"accept", "content-type"}
        )
        headers.extend(
            (
                ("Authorization", credentials["authorization"]),
                ("OpenAI-Beta", "responses=experimental"),
            )
        )
        account_id = credentials.get("chatgpt-account-id")
        if host == "chatgpt.com" and account_id is not None:
            headers.extend((("chatgpt-account-id", account_id), ("originator", "codex_cli_rs")))
    return headers


def build_response_headers(status: int, headers: Message) -> dict[str, str]:
    """Keep the status and content type only; redirects and re-encodings fail."""

    if status < 200:
        raise UpstreamError("upstream returned an informational response")
    if 300 <= status < 400:
        raise UpstreamError("upstream redirects are not followed")
    encoding = headers.get("Content-Encoding")
    if encoding is not None and encoding.strip().lower() not in {"", "identity"}:
        raise UpstreamError("upstream returned an encoded body")
    result = {"Cache-Control": "no-store"}
    content_type = headers.get("Content-Type")
    if content_type is not None:
        value = content_type.strip()
        if not is_header_value_safe(value):
            raise UpstreamError("upstream returned an unsafe content type")
        result["Content-Type"] = value
    return result


def check_proxy_environment(environ: Mapping[str, str]) -> None:
    """Refuse an ambient proxy: the only egress this gateway has is the pin."""

    for name in PROXY_ENVIRONMENT:
        if environ.get(name):
            raise ConfigError(f"ambient {name} is not supported; unset it for the sidecar")


def pin_upstream(host: str) -> Upstream:
    """Resolve once and refuse the whole answer set unless all of it is public."""

    try:
        answers = socket.getaddrinfo(host, UPSTREAM_PORT, type=socket.SOCK_STREAM)
    except OSError as error:
        raise ConfigError(f"cannot resolve upstream host: {type(error).__name__}") from error
    if not answers:
        raise ConfigError("upstream host did not resolve to any address")
    pinned: list[Upstream] = []
    for family, _type, _proto, _canon, sockaddr in answers:
        if family not in (socket.AF_INET, socket.AF_INET6):
            raise ConfigError("upstream resolution returned an unsupported address family")
        try:
            literal = ipaddress.ip_address(sockaddr[0])
        except ValueError as error:
            raise ConfigError("upstream resolution returned a malformed address") from error
        if not is_public_unicast(literal):
            raise ConfigError("upstream resolution returned a non-public address")
        candidate = Upstream(host, family, str(literal))
        if candidate not in pinned:
            pinned.append(candidate)
    return pinned[0]


def build_context(ca_file: str | None = None) -> ssl.SSLContext:
    """Default chain verification, hostname checking and TLS 1.2 as a floor."""

    context = ssl.create_default_context(cafile=ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    return context


class PinnedHTTPSConnection(HTTPSConnection):
    """TLS connection to the pinned literal address, never to a name again."""

    def __init__(self, upstream: Upstream, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(upstream.host, UPSTREAM_PORT, timeout=timeout, context=context)
        self._pinned = upstream.sockaddr
        self._tls = context

    def connect(self) -> None:
        # A numeric literal goes straight into connect(); create_connection() and
        # http.client's own path would call getaddrinfo again.
        family, address, port = self._pinned
        sock = socket.socket(family, socket.SOCK_STREAM)
        self.sock = sock
        try:
            sock.settimeout(self.timeout)
            _disable_nagle(sock)
            sock.connect((address, port))
        except OSError:
            sock.close()
            self.sock = None
            raise
        try:
            self.sock = self._tls.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            self.sock = None
            raise


def _disable_nagle(sock: socket.socket) -> None:
    """Send small streamed blocks immediately instead of waiting for coalescing."""

    if not hasattr(socket, "IPPROTO_TCP"):
        return
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


class GatewayHandler(BaseHTTPRequestHandler):
    """One request per connection: validate, forward, stream, close."""

    server_version = "mergerail-ai-gateway/0"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = BODY_DEADLINE_SECONDS
    _response_started = False
    _route_name = "rejected"

    server: GatewayServer

    def handle_one_request(self) -> None:
        # Per-read socket timeouts do not stop a client dripping header bytes.
        self._upstream_connection: HTTPSConnection | None = None
        self._aborted = threading.Event()
        self._header_timer = threading.Timer(BODY_DEADLINE_SECONDS, self._abort)
        total_timer = threading.Timer(UPSTREAM_TOTAL_TIMEOUT, self._abort)
        self._header_timer.daemon = total_timer.daemon = True
        self._header_timer.start()
        total_timer.start()
        try:
            super().handle_one_request()
        finally:
            self._header_timer.cancel()
            total_timer.cancel()

    def _abort(self) -> None:
        self._aborted.set()
        upstream = self._upstream_connection
        for connection in (self.connection, upstream.sock if upstream else None):
            if connection is not None:
                with contextlib.suppress(OSError):
                    connection.shutdown(socket.SHUT_RDWR)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_CONNECT(self) -> None:
        self._fail(405, "method not allowed")

    do_DELETE = do_HEAD = do_OPTIONS = do_PATCH = do_PUT = do_TRACE = do_CONNECT

    def handle_expect_100(self) -> bool:
        """Refuse before the client streams any body."""

        self._fail(417, "expectation is not supported")
        return False

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # The raw request line is never logged: an unvalidated target may carry
        # a secret in its query string.
        log(f"{self._route_name} {code} from {self.client_address[0]}")

    def log_error(self, format: str, *args: Any) -> None:
        kind = sys.exc_info()[0]
        log(f"protocol layer rejected request: {kind.__name__ if kind else 'error'}")

    def _dispatch(self, method: str) -> None:
        timer = getattr(self, "_header_timer", None)
        if timer is not None:
            timer.cancel()
        self.close_connection = True
        self._response_started = False
        self._route_name = "rejected"
        try:
            route = resolve_route(self.server.routes, method, self.path)
            self._route_name = route.name
            headers = collect_headers(
                self.headers,
                self.server.allowed_hosts,
                credentialed=self.server.credentials is not None,
            )
            length = parse_content_length(self.headers.get_all("Content-Length") or [])
            if route.method == "GET" and length:
                raise RequestError(400, "a body is not allowed for this endpoint")
            if route.method != "GET" and not length:
                raise RequestError(400, "a request body is required")
            body = self._read_body(length) if length else b""
            self.connection.settimeout(UPSTREAM_SOCKET_TIMEOUT)
            if getattr(self, "_aborted", threading.Event()).is_set():
                raise RequestError(408, "request deadline exceeded")
            self._forward(route, headers, body)
        except RequestError as error:
            self._fail(error.status, error.message)
        except UpstreamError as error:
            self._fail(502, str(error))
        except ValueError:
            self._fail(400, "the request could not be encoded")
        except (OSError, HTTPException) as error:
            record_close("transport_error")
            log(f"upstream transport failed: {type(error).__name__}")
            self._fail(502, "upstream request failed")

    def _read_body(self, length: int) -> bytes:
        """Read exactly length bytes, one underlying read at a time."""

        deadline = time.monotonic() + BODY_DEADLINE_SECONDS
        remaining = length
        chunks: list[bytes] = []
        while remaining:
            left = deadline - time.monotonic()
            if left <= 0:
                raise RequestError(408, "the request body did not arrive in time")
            self.connection.settimeout(left)
            try:
                block = self.rfile.read1(min(remaining, BLOCK_BYTES))
            except (OSError, ValueError) as error:
                raise RequestError(408, "the request body did not arrive in time") from error
            if not block:
                raise RequestError(400, "the request body ended early")
            chunks.append(block)
            remaining -= len(block)
        return b"".join(chunks)

    def _forward(self, route: Route, headers: Mapping[str, str], body: bytes) -> None:
        upstream = self.server.upstream
        connection = PinnedHTTPSConnection(upstream, UPSTREAM_SOCKET_TIMEOUT, self.server.context)
        self._upstream_connection = connection
        try:
            connection.putrequest(
                route.method, route.path, skip_host=True, skip_accept_encoding=True
            )
            for name, value in build_upstream_headers(
                upstream.host,
                headers,
                len(body),
                credentials=self.server.credentials,
            ):
                connection.putheader(name, value)
            connection.endheaders(body)
            self._relay(connection.getresponse())
        finally:
            connection.close()
            self._upstream_connection = None

    def _relay(self, response: HTTPResponse) -> None:
        """Stream the response in bounded blocks; abort rather than grow."""

        if self.server.credentials is not None and response.status >= 400:
            record_close("upstream_http_error", status=response.status)
            message = (
                "upstream authentication failed; please sign in again"
                if response.status in {401, 403}
                else f"upstream request failed (HTTP {response.status}); "
                "check model and account limits"
            )
            self._fail(response.status, message)
            return
        headers = build_response_headers(response.status, response.headers)
        self.send_response(response.status)
        for name, value in headers.items():
            self.send_header(name, value)
        # No content length and no chunking: the body ends when the socket closes.
        self.send_header("Connection", "close")
        self.end_headers()
        self._response_started = True
        deadline = time.monotonic() + UPSTREAM_TOTAL_TIMEOUT
        total = 0
        while True:
            if time.monotonic() > deadline:
                record_close("timeout", status=response.status, size=total)
                raise UpstreamError("upstream response exceeded the time budget")
            block = response.read1(BLOCK_BYTES)
            if not block:
                record_close("upstream_eof", status=response.status, size=total)
                return
            total += len(block)
            if total > MAX_RESPONSE_BYTES:
                record_close("size_limit", status=response.status, size=total)
                raise UpstreamError("upstream response exceeded the size budget")
            self.wfile.write(block)

    def _fail(self, status: int, message: str) -> None:
        """Answer with a fixed body; never echo the request or the upstream."""

        if self._response_started:
            # The status line is already on the wire; a truncated body is the
            # only honest signal left that the upstream failed.
            log(f"{self._route_name} aborted after the headers were sent: {message}")
            self.close_connection = True
            return
        payload = (message + "\n").encode("utf-8")
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self._response_started = True
        if self.command != "HEAD":
            self.wfile.write(payload)


class GatewayServer(HTTPServer):
    """Accepts a fixed number of concurrent requests and refuses the rest."""

    daemon_threads = True
    request_queue_size = LISTEN_BACKLOG

    def __init__(
        self,
        bind: str,
        port: int,
        upstream: Upstream,
        routes: Mapping[str, Route],
        context: ssl.SSLContext,
        *,
        credentials: Mapping[str, str] | None = None,
    ) -> None:
        self.upstream = upstream
        self.credentials = None if credentials is None else validate_credentials(credentials)
        if self.credentials is None:
            self.routes = routes
        else:
            response_routes = {
                target: route
                for target, route in routes.items()
                if target == route.path and route.method == "POST" and route.name == "responses"
            }
            if len(response_routes) != 1:
                raise ConfigError("credential mode requires one fixed responses route")
            route = next(iter(response_routes.values()))
            suffix = "/responses"
            if not route.path.endswith(suffix):
                raise ConfigError("credential mode requires one fixed responses route")
            prefix = validate_prefix(route.path[: -len(suffix)])
            validate_credential_destination(upstream.host, prefix)
            self.routes = response_routes
        self.context = context
        self.allowed_hosts: tuple[str, ...] = ()
        self.authority = ""
        self.address_family = (
            socket.AF_INET6 if ipaddress.ip_address(bind).version == 6 else socket.AF_INET
        )
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
        self._pool = ThreadPoolExecutor(
            max_workers=MAX_CONCURRENT_REQUESTS, thread_name_prefix="gateway"
        )
        self._closed = False
        super().__init__((bind, port), GatewayHandler)
        self.allowed_hosts = host_forms(bind, self.server_port)
        self.authority = self.allowed_hosts[0]

    def server_bind(self) -> None:
        # HTTPServer.server_bind() reverse-resolves the bind address. The gateway
        # needs no name, and startup must not depend on a resolver once pinned.
        socketserver.TCPServer.server_bind(self)
        self.server_name = "docker-ai-gateway"
        self.server_port = self.server_address[1]

    def process_request(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: Any,
    ) -> None:
        connection = request[1] if isinstance(request, tuple) else request
        if not self._slots.acquire(blocking=False):
            self._refuse_busy(connection)
            return
        try:
            self._pool.submit(self._serve, connection, client_address)
        except RuntimeError:
            self._slots.release()
            self._refuse_busy(connection)

    def _serve(self, request: socket.socket, client_address: Any) -> None:
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)
            self._slots.release()

    def _refuse_busy(self, request: socket.socket) -> None:
        with contextlib.suppress(OSError):
            request.settimeout(0.1)
            request.sendall(BUSY_RESPONSE)
        self.shutdown_request(request)

    def handle_error(self, request: Any, client_address: Any) -> None:
        kind = sys.exc_info()[0]
        log(f"handler crashed: {kind.__name__ if kind else 'error'}")

    def server_close(self) -> None:
        if not self._closed:
            self._closed = True
            self._pool.shutdown(wait=False, cancel_futures=True)
        super().server_close()


def stop_on_signal(server: GatewayServer, signum: int) -> None:
    """Stop accepting on SIGINT or SIGTERM so the container can exit cleanly."""

    log(f"signal {signum} received, stopping the gateway")
    threading.Thread(target=server.shutdown, daemon=True).start()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bounded AI egress gateway for a constrained Docker sidecar."
    )
    parser.add_argument(
        "--upstream-host", required=True, help="controller-selected API hostname, ASCII DNS name"
    )
    parser.add_argument(
        "--upstream-prefix", default="/zen/v1", help="fixed API path prefix served by the gateway"
    )
    parser.add_argument(
        "--bind", required=True, help="literal IP the agent stage addresses, never a wildcard"
    )
    parser.add_argument("--port", type=int, default=8080, help="listen port, default 8080")
    parser.add_argument(
        "--ca-file", default=None, help="optional additional CA bundle for the pinned upstream"
    )
    parser.add_argument(
        "--credentials-file",
        default=None,
        help="optional root-controlled JSON file for fixed OpenAI credentials",
    )
    args = parser.parse_args(argv)
    try:
        check_proxy_environment(os.environ)
        host = validate_upstream_host(args.upstream_host)
        prefix = validate_prefix(args.upstream_prefix)
        credentials = (
            load_credentials(args.credentials_file) if args.credentials_file is not None else None
        )
        if credentials is not None:
            validate_credential_destination(host, prefix)
        upstream = pin_upstream(host)
        routes = build_routes(prefix)
        bind = validate_bind(args.bind)
        if not 0 < args.port < 65536:
            raise ConfigError("port must be between 1 and 65535")
        context = build_context(args.ca_file)
    except (ConfigError, OSError) as error:
        log(f"refusing to start: {error}")
        return 2
    log(f"pinned upstream {upstream.host} to {upstream.address}")
    try:
        server = GatewayServer(bind, args.port, upstream, routes, context, credentials=credentials)
    except ConfigError as error:
        log(f"refusing to start: {error}")
        return 2
    except OSError as error:
        log(f"cannot listen: {type(error).__name__}")
        return 1
    log(f"listening on {server.authority} for {len(server.routes)} fixed routes")

    def stop(number: int, frame: object) -> None:
        stop_on_signal(server, number)

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    except OSError as error:
        log(f"accept loop failed: {type(error).__name__}")
        return 1
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
