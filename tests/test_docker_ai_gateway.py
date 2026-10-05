"""Contracts for the production fixed-origin AI gateway; no network is used."""

from __future__ import annotations

import io
import ipaddress
import os
import socket
import ssl
import time
from email import message_from_string
from typing import Any, cast
from unittest import mock

import pytest

from mergerail.execution import gateway

PRIVATE = ("10.0.0.1", "172.16.5.4", "192.168.1.7")
LOOPBACK_AND_META = (
    "127.0.0.1",
    "127.1.2.3",
    "169.254.169.254",
    "169.254.170.2",
    "100.100.100.200",
)
MAPPED_AND_OTHER = (
    "::ffff:10.0.0.1",
    "::ffff:127.0.0.1",
    "::ffff:8.8.8.8",
    "64:ff9b::a00:1",
    "2002:0a00:0001::1",
    "fd00:ec2::254",
    "fe80::1",
    "fec0::1",
    "::1",
    "::",
    "0.0.0.0",
    "224.0.0.1",
    "239.255.255.250",
    "255.255.255.255",
    "240.0.0.1",
    "192.0.0.1",
    "198.18.0.1",
    "203.0.113.9",
    "192.0.2.5",
    "192.88.99.1",
)
PUBLIC = ("8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111")


@pytest.mark.parametrize("literal", (*PRIVATE, *LOOPBACK_AND_META, *MAPPED_AND_OTHER))
def test_non_public_addresses_are_denied(literal: str) -> None:
    assert gateway.is_public_unicast(ipaddress.ip_address(literal)) is False


@pytest.mark.parametrize("literal", PUBLIC)
def test_public_addresses_are_allowed(literal: str) -> None:
    assert gateway.is_public_unicast(ipaddress.ip_address(literal)) is True


def _answer(family: int, address: str) -> tuple[Any, ...]:
    return (family, socket.SOCK_STREAM, 6, "", (address, 443, 0, 0))


def _resolve(answers: list[tuple[Any, ...]], error: Exception | None = None) -> Any:
    if error is not None:
        return mock.patch.object(socket, "getaddrinfo", side_effect=error)
    return mock.patch.object(socket, "getaddrinfo", return_value=answers)


@pytest.mark.parametrize("address", (*PRIVATE, *LOOPBACK_AND_META, *MAPPED_AND_OTHER))
def test_pin_refuses_any_non_public_answer(address: str) -> None:
    with _resolve([_answer(socket.AF_INET, address)]), pytest.raises(gateway.ConfigError):
        gateway.pin_upstream("opencode.ai")


def test_pin_refuses_a_mixed_answer_set() -> None:
    answers = [_answer(socket.AF_INET, "8.8.8.8"), _answer(socket.AF_INET, "192.168.0.1")]
    with _resolve(answers), pytest.raises(gateway.ConfigError):
        gateway.pin_upstream("opencode.ai")


def test_pin_refuses_alternative_ipv6_mapped_answers() -> None:
    answers = [_answer(socket.AF_INET6, "::ffff:93.184.216.34")]
    with _resolve(answers), pytest.raises(gateway.ConfigError):
        gateway.pin_upstream("opencode.ai")


def test_pin_returns_the_literal_sockaddr() -> None:
    with _resolve([_answer(socket.AF_INET, "93.184.216.34")]):
        pinned = gateway.pin_upstream("opencode.ai")
    assert pinned.host == "opencode.ai"
    assert pinned.address == "93.184.216.34"
    assert pinned.sockaddr == (socket.AF_INET.value, "93.184.216.34", 443)


def test_a_later_resolver_answer_never_changes_a_pin() -> None:
    resolver = mock.Mock(return_value=[_answer(socket.AF_INET, "93.184.216.34")])
    with mock.patch.object(socket, "getaddrinfo", resolver):
        pinned = gateway.pin_upstream("opencode.ai")
        resolver.return_value = [_answer(socket.AF_INET, "10.0.0.1")]
        raw_socket = mock.Mock()
        with mock.patch.object(socket, "socket", return_value=raw_socket):
            connection = gateway.PinnedHTTPSConnection(pinned, 30.0, mock.Mock())
            connection.connect()
            raw_socket.connect.assert_called_once_with(("93.184.216.34", 443))
    assert resolver.call_count == 1
    assert pinned.address == "93.184.216.34"


def test_pin_reports_an_empty_or_failed_resolution() -> None:
    with _resolve([]), pytest.raises(gateway.ConfigError):
        gateway.pin_upstream("opencode.ai")
    with _resolve([], error=OSError("resolver is unreachable")), pytest.raises(gateway.ConfigError):
        gateway.pin_upstream("opencode.ai")


def test_connection_uses_the_pinned_literal_and_fixed_sni() -> None:
    class FakeContext:
        def __init__(self) -> None:
            self.server_hostname: str | None = None

        def wrap_socket(self, sock: socket.socket, server_hostname: str) -> str:
            self.server_hostname = server_hostname
            return "wrapped"

    class FakeSocket:
        def __init__(self) -> None:
            self.connected: tuple[str, int] | None = None
            self.timeout: float | None = None
            self.closed = False

        def settimeout(self, value: float | None) -> None:
            self.timeout = value

        def setsockopt(self, *args: Any) -> None:
            pass

        def connect(self, address: tuple[str, int]) -> None:
            self.connected = address

        def close(self) -> None:
            self.closed = True

    fake_socket = FakeSocket()
    context = FakeContext()
    upstream = gateway.Upstream("opencode.ai", socket.AF_INET, "93.184.216.34")
    connection = gateway.PinnedHTTPSConnection(upstream, 30.0, cast(ssl.SSLContext, context))
    assert connection.host == "opencode.ai"
    assert connection.port == 443
    with (
        mock.patch.object(socket, "socket", return_value=fake_socket) as factory,
        mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("no resolver")),
    ):
        connection.connect()
    assert factory.call_args.args[0] == socket.AF_INET.value
    assert fake_socket.connected == ("93.184.216.34", 443)
    assert fake_socket.timeout == 30.0
    assert context.server_hostname == "opencode.ai"
    assert connection.sock == "wrapped"


def test_connection_closes_the_socket_when_tls_fails() -> None:
    class FakeContext:
        def wrap_socket(self, sock: socket.socket, server_hostname: str) -> None:
            raise ssl.SSLCertVerificationError("untrusted chain")

    class FakeSocket:
        def settimeout(self, value: float | None) -> None:
            pass

        def setsockopt(self, *args: Any) -> None:
            pass

        def connect(self, address: tuple[str, int]) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    fake_socket = FakeSocket()
    upstream = gateway.Upstream("opencode.ai", socket.AF_INET, "93.184.216.34")
    connection = gateway.PinnedHTTPSConnection(upstream, 30.0, cast(ssl.SSLContext, FakeContext()))
    with (
        mock.patch.object(socket, "socket", return_value=fake_socket),
        pytest.raises(ssl.SSLError),
    ):
        connection.connect()
    assert fake_socket.closed is True


@pytest.mark.parametrize("value", ["opencode.ai", "OpenCode.AI.", "a-b.example.com"])
def test_upstream_host_forms(value: str) -> None:
    assert gateway.validate_upstream_host(value) == value.strip().lower().rstrip(".")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "localhost",
        "https://opencode.ai",
        "opencode.ai/v1",
        "opencode.ai:443",
        "10.0.0.1",
        "-bad.example.com",
        "bad-.example.com",
        "open code.ai",
        "opencode..ai",
        "a_b.example.com",
    ],
)
def test_upstream_host_refusals(value: str) -> None:
    with pytest.raises(gateway.ConfigError):
        gateway.validate_upstream_host(value)


def test_prefix_forms() -> None:
    assert gateway.validate_prefix("/zen/v1") == "/zen/v1"
    assert gateway.validate_prefix("/v1") == "/v1"
    assert gateway.validate_prefix("/zen/v1/models~x") == "/zen/v1/models~x"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "zen/v1",
        "/zen/v1/",
        "/",
        "/a/../b",
        "/a/./b",
        "/a/%2e%2e",
        "/a?b=1",
        "/a#frag",
        "//zen",
        "/a b",
        "/a/../../etc",
    ],
)
def test_prefix_refusals(value: str) -> None:
    with pytest.raises(gateway.ConfigError):
        gateway.validate_prefix(value)


def test_bind_refusals() -> None:
    assert gateway.validate_bind("10.11.12.13") == "10.11.12.13"
    assert gateway.validate_bind("::1") == "::1"
    for value in ("0.0.0.0", "::", "224.0.0.1", "localhost", "10.0.0.1:8080", ""):
        with pytest.raises(gateway.ConfigError):
            gateway.validate_bind(value)


def test_host_forms_for_ipv4_and_ipv6() -> None:
    assert gateway.host_forms("10.0.0.4", 8080) == ("10.0.0.4:8080",)
    assert gateway.host_forms("10.0.0.4", 80) == ("10.0.0.4:80", "10.0.0.4")
    assert gateway.host_forms("::1", 8080) == ("[::1]:8080",)


def test_routes_are_exactly_the_four_fixed_endpoints() -> None:
    routes = gateway.build_routes("/zen/v1")
    assert set(routes) == {
        "/zen/v1/models",
        "/zen/v1/chat/completions",
        "/zen/v1/responses",
        "/zen/v1/messages",
    }
    assert sorted(route.name for route in routes.values()) == [
        "chat/completions",
        "messages",
        "models",
        "responses",
    ]


@pytest.mark.parametrize(
    ("method", "target"),
    [
        ("GET", "/zen/v1/models"),
        ("POST", "/zen/v1/chat/completions"),
        ("POST", "/zen/v1/responses"),
        ("POST", "/zen/v1/messages"),
    ],
)
def test_allowed_routes(method: str, target: str) -> None:
    routes = gateway.build_routes("/zen/v1")
    route = gateway.resolve_route(routes, method, target)
    assert (route.method, route.path) == (method, target)


@pytest.mark.parametrize(
    ("method", "target"),
    [
        ("POST", "/zen/v1/models"),
        ("GET", "/zen/v1/chat/completions"),
        ("GET", "/zen/v1/messages"),
        ("GET", "/zen/v1"),
        ("GET", "/zen/v1/"),
        ("GET", "/zen/v1/models?limit=1"),
        ("GET", "/zen/v1/models#frag"),
        ("GET", "/zen/v1//models"),
        ("GET", "/zen/v1/../zen/v1/models"),
        ("GET", "/zen/v1/./models"),
        ("GET", "/zen/%76/models"),
        ("GET", "/zen/v1/models/"),
        ("GET", "/models"),
        ("GET", "/zen/v2/models"),
        ("GET", "http://evil.example/zen/v1/models"),
        ("GET", "//evil.example/zen/v1/models"),
        ("GET", "/zen/v1/models HTTP/1.1"),
        ("GET", " /zen/v1/models"),
        ("GET", "/zen/v1/models\x00"),
    ],
)
def test_forbidden_targets(method: str, target: str) -> None:
    routes = gateway.build_routes("/zen/v1")
    with pytest.raises(gateway.RequestError):
        gateway.resolve_route(routes, method, target)


def _message(text: str) -> Any:
    return message_from_string(text)


def test_only_allowlisted_headers_are_forwarded() -> None:
    message = _message(
        "Host: 10.0.0.4:8080\r\n"
        "Authorization: Bearer secret\r\n"
        "Content-Type: application/json\r\n"
        "Accept: text/event-stream\r\n"
        "anthropic-version: 2023-06-01\r\n"
        "anthropic-beta: fine\r\n"
        "Cookie: session=secret\r\n"
        "User-Agent: agent\r\n"
        "X-Forwarded-Host: evil.example\r\n"
        "Proxy-Authorization: Basic secret\r\n"
    )
    forwarded = gateway.collect_headers(message, ("10.0.0.4:8080", "10.0.0.4"))
    assert forwarded == {
        "accept": "text/event-stream",
        "anthropic-beta": "fine",
        "anthropic-version": "2023-06-01",
        "authorization": "Bearer secret",
        "content-type": "application/json",
    }
    headers = gateway.build_upstream_headers("opencode.ai", forwarded, 17)
    assert headers[0] == ("Host", "opencode.ai")
    values = [value for _, value in headers]
    assert "session=secret" not in values
    assert "Basic secret" not in values
    assert "evil.example" not in values
    assert not [name for name, _ in headers if name.lower().startswith(("x-", "proxy-"))]


def test_upstream_headers_never_carry_client_framing() -> None:
    message = _message("Host: 10.0.0.4:8080\r\nAuthorization: Bearer s\r\n")
    forwarded = gateway.collect_headers(message, ("10.0.0.4:8080", "10.0.0.4"))
    names = [name for name, _ in gateway.build_upstream_headers("opencode.ai", forwarded, 2)]
    assert names == ["Host", "Content-Length", "Accept-Encoding", "authorization"]


@pytest.mark.parametrize(
    "text",
    [
        "Host: evil.example\r\n",
        "Host: 10.0.0.4:9999\r\n",
        "Host: 10.0.0.4:8080\r\nHost: 10.0.0.4:8080\r\n",
        "Content-Length: 2\r\n",
        "Host: 10.0.0.4:8080\r\nTransfer-Encoding: chunked\r\n",
        "Host: 10.0.0.4:8080\r\nUpgrade: websocket\r\n",
        "Host: 10.0.0.4:8080\r\nConnection: keep-alive, Upgrade\r\n",
        "Host: 10.0.0.4:8080\r\nAuthorization: a\r\nAuthorization: b\r\n",
        "Host: 10.0.0.4:8080\r\nContent-Type: application/json\r\n\tX-Injected\r\n",
        "Host: 10.0.0.4:8080\r\nAuthorization: Bearer a\r\n\tx\r\n",
    ],
)
def test_forbidden_header_sets(text: str) -> None:
    with pytest.raises(gateway.RequestError):
        gateway.collect_headers(_message(text), ("10.0.0.4:8080", "10.0.0.4"))


def test_host_without_port_is_accepted_only_on_port_80() -> None:
    message = _message("Host: 10.0.0.4\r\n")
    assert gateway.collect_headers(message, gateway.host_forms("10.0.0.4", 80)) == {}
    with pytest.raises(gateway.RequestError):
        gateway.collect_headers(message, gateway.host_forms("10.0.0.4", 8080))


def test_content_length_parsing() -> None:
    assert gateway.parse_content_length([]) == 0
    assert gateway.parse_content_length(["0"]) == 0
    assert gateway.parse_content_length([" 42 "]) == 42
    assert gateway.parse_content_length([str(8 * 1024 * 1024)]) == 8 * 1024 * 1024


@pytest.mark.parametrize(
    "values",
    [
        ["1", "1"],
        ["-1"],
        ["+1"],
        ["1.0"],
        ["0x10"],
        ["007"],
        [""],
        [" "],
        ["9" * 20],
        [str(8 * 1024 * 1024 + 1)],
        ["1e3"],
    ],
)
def test_content_length_refusals(values: list[str]) -> None:
    with pytest.raises(gateway.RequestError):
        gateway.parse_content_length(values)


def test_body_budget_is_eight_mib() -> None:
    assert gateway.MAX_BODY_BYTES == 8 * 1024 * 1024
    assert gateway.MAX_RESPONSE_BYTES == 20 * 1024 * 1024
    assert gateway.MAX_CONCURRENT_REQUESTS == 2
    assert gateway.UPSTREAM_SOCKET_TIMEOUT == 30.0
    assert gateway.UPSTREAM_TOTAL_TIMEOUT == 600.0


def test_ambient_proxy_environment_is_refused() -> None:
    with pytest.raises(gateway.ConfigError):
        gateway.check_proxy_environment({"HTTPS_PROXY": "http://10.0.0.1:3128"})
    with pytest.raises(gateway.ConfigError):
        gateway.check_proxy_environment({"all_proxy": "socks5://10.0.0.1:1080"})
    gateway.check_proxy_environment({"PATH": "/usr/bin"})


def test_redirects_are_never_relayed() -> None:
    headers = _message("Location: http://169.254.169.254/latest/meta-data\r\n")
    for status in (301, 302, 303, 307, 308, 399):
        with pytest.raises(gateway.UpstreamError):
            gateway.build_response_headers(status, headers)
    ok = gateway.build_response_headers(200, _message("Content-Type: application/json\r\n"))
    assert ok == {"Cache-Control": "no-store", "Content-Type": "application/json"}


def test_informational_and_encoded_upstream_responses_fail() -> None:
    with pytest.raises(gateway.UpstreamError):
        gateway.build_response_headers(100, _message(""))
    with pytest.raises(gateway.UpstreamError):
        gateway.build_response_headers(200, _message("Content-Encoding: gzip\r\n"))
    identity = gateway.build_response_headers(200, _message("Content-Encoding: identity\r\n"))
    assert identity == {"Cache-Control": "no-store"}


def test_only_content_type_survives_from_the_upstream() -> None:
    headers = _message(
        "Content-Type: text/event-stream\r\n"
        "Set-Cookie: session=secret\r\n"
        "Location: http://evil.example\r\n"
        "X-Account: secret\r\n"
    )
    assert gateway.build_response_headers(200, headers) == {
        "Cache-Control": "no-store",
        "Content-Type": "text/event-stream",
    }
    plain = gateway.build_response_headers(200, _message("Content-Type: text/plain\r\n"))
    assert plain == {"Cache-Control": "no-store", "Content-Type": "text/plain"}


class FakeResponse:
    """A minimal http.client.HTTPResponse stand-in that records its reads."""

    def __init__(
        self,
        blocks: list[bytes],
        status: int = 200,
        headers: str = "",
        journal: list[str] | None = None,
    ) -> None:
        self.status = status
        self.headers = message_from_string(headers or "Content-Type: text/event-stream\r\n")
        self._blocks = list(blocks)
        self._journal: list[str] = journal if journal is not None else []
        self.reads: list[int] = []

    def read1(self, size: int = -1) -> bytes:
        self._journal.append("read")
        self.reads.append(size)
        return self._blocks.pop(0) if self._blocks else b""


class RecordingWriter(io.BytesIO):
    def __init__(self, journal: list[str]) -> None:
        super().__init__()
        self._journal = journal

    def write(self, data: Any) -> int:
        self._journal.append(f"write:{len(data)}")
        return super().write(data)


def _handler(peer: str = "127.0.0.1") -> tuple[Any, list[str]]:
    handler: Any = object.__new__(gateway.GatewayHandler)
    handler.request = None
    handler.client_address = (peer, 5555)
    handler.connection = None
    handler.command = "POST"
    handler.path = "/zen/v1/chat/completions"
    handler.requestline = "POST /zen/v1/chat/completions HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.close_connection = True
    handler._headers_buffer = []
    handler._response_started = False
    handler._route_name = "chat/completions"
    journal: list[str] = []
    handler.wfile = RecordingWriter(journal)
    return handler, journal


def test_sse_is_streamed_block_by_block() -> None:
    handler, journal = _handler()
    blocks = [b"data: one\n\n", b"data: two\n\n", b"data: [DONE]\n\n"]
    response = FakeResponse(blocks, journal=journal)
    handler._relay(response)
    written = handler.wfile.getvalue()
    assert written.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"Connection: close\r\n" in written
    assert b"Content-Type: text/event-stream\r\n" in written
    assert b"".join(blocks) in written
    # The first write is the header block; every later block is relayed as it
    # arrives, which is what makes SSE stream instead of buffer.
    assert journal[0].startswith("write:")
    expected: list[str] = []
    for block in blocks:
        expected.extend(("read", f"write:{len(block)}"))
    expected.append("read")
    assert journal[1:] == expected


def test_relay_stops_at_the_response_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    handler, _ = _handler()
    monkeypatch.setattr(gateway, "MAX_RESPONSE_BYTES", 1024)
    response = FakeResponse([b"x" * 600, b"x" * 600, b"x" * 600])
    with pytest.raises(gateway.UpstreamError):
        handler._relay(response)
    written = handler.wfile.getvalue()
    assert written.endswith(b"x" * 600)
    assert b"x" * 1200 not in written
    assert response.reads == [gateway.BLOCK_BYTES, gateway.BLOCK_BYTES]


def test_relay_stops_at_the_time_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    handler, _ = _handler()
    monkeypatch.setattr(gateway, "UPSTREAM_TOTAL_TIMEOUT", -1.0)
    response = FakeResponse([b"data: late\n\n"])
    with pytest.raises(gateway.UpstreamError):
        handler._relay(response)
    assert handler.wfile.getvalue().startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"data: late" not in handler.wfile.getvalue()
    assert response.reads == []


def test_relay_refuses_a_redirect_before_any_header_is_sent() -> None:
    handler, journal = _handler()
    response = FakeResponse([b"moved"], status=302, headers="Location: http://10.0.0.1/\r\n")
    with pytest.raises(gateway.UpstreamError):
        handler._relay(response)
    assert journal == []
    assert handler.wfile.getvalue() == b""


def test_failure_bodies_never_echo_the_request() -> None:
    handler, _ = _handler()
    handler._fail(400, "the request body is too large")
    written = handler.wfile.getvalue()
    assert written.startswith(b"HTTP/1.1 400 Bad Request\r\n")
    assert written.endswith(b"the request body is too large\n")
    assert b"secret" not in written


def _server() -> Any:
    upstream = gateway.Upstream("opencode.ai", socket.AF_INET, "93.184.216.34")
    return gateway.GatewayServer(
        "127.0.0.1", 0, upstream, gateway.build_routes("/zen/v1"), gateway.build_context()
    )


def test_a_busy_gateway_answers_503_without_reading_the_request() -> None:
    server = _server()
    right: socket.socket | None = None
    try:
        assert server.allowed_hosts[0] == f"127.0.0.1:{server.server_port}"
        server._slots.acquire()
        server._slots.acquire()
        left, right = socket.socketpair()
        right.settimeout(10)
        server.process_request(left, ("127.0.0.1", 5555))
        received = right.recv(4096)
        assert received.startswith(b"HTTP/1.1 503 Service Unavailable\r\n")
        assert b"too many concurrent requests" in received
        assert int(received.split(b"Content-Length: ")[1].split(b"\r\n")[0]) == len(
            gateway.BUSY_BODY
        )
    finally:
        server._slots.release()
        server._slots.release()
        if right is not None:
            right.close()
        server.server_close()


def test_a_free_gateway_slot_is_released_after_the_request() -> None:
    server = _server()
    try:
        for _ in range(gateway.MAX_CONCURRENT_REQUESTS + 2):
            left, right = socket.socketpair()
            server.process_request(left, ("127.0.0.1", 5555))
            right.close()
        for _ in range(gateway.MAX_CONCURRENT_REQUESTS):
            assert server._slots.acquire(timeout=10)
        assert not server._slots.acquire(blocking=False)
    finally:
        server.server_close()


def test_worker_pool_and_slots_match_the_two_request_budget() -> None:
    server = _server()
    try:
        assert server._pool._max_workers == gateway.MAX_CONCURRENT_REQUESTS
        assert server._slots._value == gateway.MAX_CONCURRENT_REQUESTS
    finally:
        server.server_close()


def test_binding_never_uses_the_resolver() -> None:
    with mock.patch.object(socket, "getfqdn", side_effect=AssertionError("no resolver")):
        server = _server()
    try:
        assert server.server_name == "docker-ai-gateway"
        assert server.allowed_hosts == (f"127.0.0.1:{server.server_port}",)
    finally:
        server.server_close()


def test_header_drip_cannot_hold_a_gateway_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway, "BODY_DEADLINE_SECONDS", 0.15)
    server = _server()
    left, right = socket.socketpair()
    right.settimeout(1)
    started = time.monotonic()
    try:
        server.process_request(left, ("127.0.0.1", 5555))
        for byte in b"GET /zen/v1/models HTTP/1.1":
            try:
                right.sendall(bytes([byte]))
            except BrokenPipeError:
                break
            time.sleep(0.03)
        else:
            pytest.fail("dripping request survived its absolute header deadline")
        assert time.monotonic() - started < 1
        for _ in range(gateway.MAX_CONCURRENT_REQUESTS):
            assert server._slots.acquire(timeout=1)
    finally:
        right.close()
        server.server_close()


def _run_main(argv: list[str], answers: list[tuple[Any, ...]]) -> int:
    clean = {
        name: value for name, value in os.environ.items() if name not in gateway.PROXY_ENVIRONMENT
    }
    with mock.patch.dict(os.environ, clean, clear=True), _resolve(answers):
        return gateway.main(argv)


def test_main_refuses_settings_it_cannot_serve() -> None:
    public = [_answer(socket.AF_INET, "93.184.216.34")]
    assert (
        _run_main(
            ["--upstream-host", "metadata.example", "--bind", "127.0.0.1"],
            [_answer(socket.AF_INET, "169.254.169.254")],
        )
        == 2
    )
    assert _run_main(["--upstream-host", "opencode.ai", "--bind", "0.0.0.0"], public) == 2
    assert _run_main(["--upstream-host", "opencode.ai", "--bind", "not-an-ip"], public) == 2
    assert _run_main(["--upstream-host", "http://opencode.ai", "--bind", "127.0.0.1"], public) == 2
    assert (
        _run_main(
            ["--upstream-host", "opencode.ai", "--bind", "127.0.0.1", "--upstream-prefix", "zen"],
            public,
        )
        == 2
    )
    assert (
        _run_main(["--upstream-host", "opencode.ai", "--bind", "127.0.0.1", "--port", "0"], public)
        == 2
    )
    assert (
        _run_main(
            ["--upstream-host", "opencode.ai", "--bind", "127.0.0.1", "--port", "70000"], public
        )
        == 2
    )
