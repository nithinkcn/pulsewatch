"""The probes themselves — the only part of the system that touches the network.

Synchronous on purpose. These run inside Celery workers, where an event loop
per task buys nothing and costs a class of bugs (connections bound to a loop
that has already closed). Concurrency here comes from worker processes, which
is the unit Celery already manages.

Every probe is bounded by a timeout. An endpoint that accepts a connection and
then never responds is the common failure in the field, and without a timeout
it holds a worker slot forever — one hung host quietly starving the checks for
everything else.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass

import httpx

from app.models import CheckType


@dataclass(frozen=True, slots=True)
class ProbeResult:
    ok: bool
    latency_ms: float
    status_code: int | None = None
    error: str | None = None


def probe_http(url: str, *, timeout: float, expected_status: int | None) -> ProbeResult:
    """GET a URL and judge the response.

    `expected_status=None` means "any 2xx or 3xx counts as up", which is the
    sane default for a health endpoint behind a redirect.
    """
    started = time.perf_counter()
    try:
        # follow_redirects=False: a 301 to a login page is not a healthy
        # service, and silently following it would hide exactly that.
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            response = client.get(url)
    except httpx.TimeoutException:
        return ProbeResult(
            ok=False, latency_ms=_elapsed_ms(started), error=f"timeout after {timeout}s"
        )
    except httpx.HTTPError as exc:
        return ProbeResult(ok=False, latency_ms=_elapsed_ms(started), error=str(exc)[:500])

    latency = _elapsed_ms(started)
    code = response.status_code
    ok = code == expected_status if expected_status is not None else 200 <= code < 400

    return ProbeResult(
        ok=ok,
        latency_ms=latency,
        status_code=code,
        error=None if ok else f"unexpected status {code}",
    )


def probe_tcp(address: str, *, timeout: float) -> ProbeResult:
    """Open a TCP connection to "host:port" and close it again.

    For anything that is not HTTP — an RTSP camera stream, a database port, a
    message broker. Reaching the port is the signal; we deliberately do not
    speak the protocol.
    """
    host, _, port_text = address.rpartition(":")
    if not host or not port_text.isdigit():
        return ProbeResult(ok=False, latency_ms=0.0, error=f"invalid tcp address {address!r}")

    started = time.perf_counter()
    try:
        with socket.create_connection((host, int(port_text)), timeout=timeout):
            return ProbeResult(ok=True, latency_ms=_elapsed_ms(started))
    except TimeoutError:
        return ProbeResult(
            ok=False, latency_ms=_elapsed_ms(started), error=f"timeout after {timeout}s"
        )
    except OSError as exc:
        return ProbeResult(ok=False, latency_ms=_elapsed_ms(started), error=str(exc)[:500])


def run_probe(
    *,
    check_type: CheckType,
    address: str,
    timeout: float,
    expected_status: int | None,
) -> ProbeResult:
    """Dispatch to the right probe for a target."""
    if check_type is CheckType.HTTP:
        return probe_http(address, timeout=timeout, expected_status=expected_status)
    if check_type is CheckType.TCP:
        return probe_tcp(address, timeout=timeout)
    raise ValueError(f"unsupported check type {check_type!r}")


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
