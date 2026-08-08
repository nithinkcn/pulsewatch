"""Probe tests against real local servers.

No mocking of httpx or sockets here. A probe's entire job is to interact
correctly with a real network peer, so a mocked transport would test the mock.
These spin up genuine listeners on ephemeral ports — still fast, still hermetic.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.checks.probes import probe_http, probe_tcp, run_probe
from app.models import CheckType


class _Handler(BaseHTTPRequestHandler):
    """Serves whatever status the path asks for: /200, /503, /301."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path == "/hang":
            threading.Event().wait(10)  # never responds within any sane timeout
            return
        try:
            code = int(self.path.strip("/"))
        except ValueError:
            code = 200
        self.send_response(code)
        if code in (301, 302):
            self.send_header("Location", "/200")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args: object) -> None:
        pass  # keep pytest output clean


@pytest.fixture(scope="module")
def http_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def closed_port() -> int:
    """A port that is definitely not listening."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


class TestHttpProbe:
    def test_matching_status_is_up(self, http_server: str) -> None:
        result = probe_http(f"{http_server}/200", timeout=5, expected_status=200)

        assert result.ok is True
        assert result.status_code == 200
        assert result.latency_ms > 0
        assert result.error is None

    def test_unexpected_status_is_down(self, http_server: str) -> None:
        result = probe_http(f"{http_server}/503", timeout=5, expected_status=200)

        assert result.ok is False
        assert result.status_code == 503
        assert "unexpected status 503" in (result.error or "")

    def test_none_expected_status_accepts_any_2xx_3xx(self, http_server: str) -> None:
        assert probe_http(f"{http_server}/204", timeout=5, expected_status=None).ok is True
        assert probe_http(f"{http_server}/301", timeout=5, expected_status=None).ok is True
        assert probe_http(f"{http_server}/500", timeout=5, expected_status=None).ok is False

    def test_redirect_is_not_followed(self, http_server: str) -> None:
        # A 301 to a login page is not a healthy service. We report the 301
        # itself rather than the 200 it points at.
        result = probe_http(f"{http_server}/301", timeout=5, expected_status=200)

        assert result.ok is False
        assert result.status_code == 301

    def test_connection_refused_is_down_not_an_exception(self, closed_port: int) -> None:
        result = probe_http(f"http://127.0.0.1:{closed_port}/", timeout=2, expected_status=200)

        assert result.ok is False
        assert result.status_code is None
        assert result.error

    def test_hung_endpoint_is_bounded_by_the_timeout(self, http_server: str) -> None:
        # The failure that starves a worker pool if probes are unbounded.
        result = probe_http(f"{http_server}/hang", timeout=0.5, expected_status=200)

        assert result.ok is False
        assert "timeout" in (result.error or "")
        assert result.latency_ms < 3000


class TestTcpProbe:
    def test_open_port_is_up(self, http_server: str) -> None:
        port = http_server.rsplit(":", 1)[1]
        result = probe_tcp(f"127.0.0.1:{port}", timeout=2)

        assert result.ok is True
        assert result.error is None

    def test_closed_port_is_down(self, closed_port: int) -> None:
        result = probe_tcp(f"127.0.0.1:{closed_port}", timeout=2)

        assert result.ok is False
        assert result.error

    @pytest.mark.parametrize("address", ["no-port", "host:notaport", ":8080", ""])
    def test_malformed_address_is_reported_not_raised(self, address: str) -> None:
        result = probe_tcp(address, timeout=1)

        assert result.ok is False
        assert "invalid tcp address" in (result.error or "")


class TestDispatch:
    def test_run_probe_routes_by_check_type(self, http_server: str) -> None:
        result = run_probe(
            check_type=CheckType.HTTP, address=f"{http_server}/200", timeout=5, expected_status=200
        )
        assert result.ok is True

    def test_unsupported_check_type_raises(self) -> None:
        with pytest.raises(ValueError, match="unsupported check type"):
            run_probe(
                check_type="carrier-pigeon",  # type: ignore[arg-type]
                address="x",
                timeout=1,
                expected_status=None,
            )
