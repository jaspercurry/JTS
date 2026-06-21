# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Typed client for the jasper-control daemon (the local control service).

jasper-control runs at ``http://127.0.0.1:8780`` and owns the household's
volume / source / mic / AEC / cue / state endpoints — its route table in
``jasper/control/server.py`` is the contract this module mirrors. Many
daemons and CLIs talk to it, so this is the ONE place that owns the base
URL, the timeout policy, the transport, and the error model. Call sites use
methods here instead of each re-deriving ``http://127.0.0.1:8780`` + a
urllib/httpx block (which previously scattered the base URL across ~8 files
with per-site timeouts and error handling).

Transport is stdlib only on purpose: jasper-control is always localhost with
no TLS, so httpx's connection-pool / TLS / HTTP2 machinery is pure RAM tax on
a 1 GB Pi, and a loopback connect is microseconds — no pooling is needed, and
each request closes its connection so the FD count stays flat.

- **Sync** one-shot callers (``jasper-doctor``, the cue CLI, audio validation,
  the web wizards) use the module-level functions.
- **Long-lived async** daemons (the accessory bridge, usbsink) use
  :class:`AsyncControlClient`, which runs the same blocking transport in
  ``asyncio.to_thread`` so the event loop stays responsive, and is trivially
  faked in tests.

When a new control endpoint gets a caller, add a semantic method here rather
than scattering another literal. ``tests/test_control_client.py`` asserts the
paths this client targets all exist in the server's route table.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import os
from urllib.parse import urlsplit

# No module logger by design: the client's failure surface is the ControlError
# it raises (which carries method + path + cause); callers log it with their
# own context (event=knob.adjust.failed, event=usbsink.volume_post_failed, …).
# A client-side log would just duplicate that without the caller's context.

def _connect_host(bind_host: str) -> str:
    """Map ``JASPER_CONTROL_HOST`` to a host a *client* can connect to.

    That var is primarily the **server's bind address** — installs seeded
    it as ``0.0.0.0`` so the rotary dial can reach port 8780 from the
    LAN. A client must never target the unspecified address: connecting
    to ``0.0.0.0`` happens to land on loopback on Linux, but the request
    goes out with ``Host: 0.0.0.0:8780``, which jasper-control's
    management-host guard rightly rejects (``host_not_allowed``) — that
    was the 2026-06-11 regression where every /system/ dashboard poll
    403ed on Pis whose jasper.env carried the seeded bind value.
    Unspecified or empty maps to loopback; any other value is an
    explicit operator override and is used verbatim.
    """
    host = (bind_host or "").strip()
    if host in ("", "0.0.0.0", "::", "[::]"):
        return "127.0.0.1"
    return host


DEFAULT_HOST = _connect_host(os.environ.get("JASPER_CONTROL_HOST", "127.0.0.1"))
DEFAULT_PORT = int(os.environ.get("JASPER_CONTROL_PORT") or "8780")
DEFAULT_BASE_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"
DEFAULT_TIMEOUT = 2.0


class ControlError(RuntimeError):
    """jasper-control was unreachable or the request failed at the transport
    level (connection refused, timeout, protocol error). A non-2xx HTTP
    response is NOT a ControlError — it is returned as
    :attr:`ControlResponse.status` for the caller to interpret.
    """


class ControlResponse:
    """A jasper-control HTTP response: status code + raw body bytes."""

    __slots__ = ("status", "body")

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> object:
        """Parsed JSON body, or None for an empty body."""
        return json.loads(self.body) if self.body else None


def _request(
    method: str,
    path: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    body: dict | None = None,
    data: bytes | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
) -> ControlResponse:
    """One blocking stdlib round-trip. Raises :class:`ControlError` on a
    transport failure; otherwise returns a :class:`ControlResponse` (including
    for non-2xx HTTP statuses). No pooling — the fresh connection is always
    closed in ``finally``, keeping the FD count flat on every outcome.

    ``body`` is a dict serialized to JSON; ``data`` is a pre-encoded JSON
    body sent verbatim (the byte-forwarding path the web wizards' proxy
    uses). Pass at most one. ``headers`` adds extra request headers — the
    web wizards forward a browser-supplied ``X-JTS-Token`` through this path
    so the control-token gate sees the operator's token (the wizards
    proxy server-side, so the header can't ride the original fetch). It never
    overrides ``Content-Type``.
    """
    parts = urlsplit(base_url)
    conn = http.client.HTTPConnection(
        parts.hostname or DEFAULT_HOST,
        parts.port or DEFAULT_PORT,
        timeout=timeout,
    )
    try:
        if data is not None:
            payload = data
        elif body is not None:
            payload = json.dumps(body).encode()
        else:
            payload = None
        req_headers = (
            {"Content-Type": "application/json"} if payload is not None else {}
        )
        if headers:
            for k, v in headers.items():
                # Don't let a caller header clobber Content-Type for a
                # JSON body; everything else (X-JTS-Token) is additive.
                if k.lower() != "content-type":
                    req_headers[k] = v
        conn.request(method, path, body=payload, headers=req_headers)
        resp = conn.getresponse()
        return ControlResponse(resp.status, resp.read())
    except (OSError, TimeoutError, http.client.HTTPException) as e:
        raise ControlError(f"jasper-control {method} {path}: {e}") from e
    finally:
        conn.close()


# --- sync API: one-shot callers (doctor, cue CLI, audio validation, wizards)
def request(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    data: bytes | None = None,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
) -> ControlResponse:
    return _request(
        method, path, base_url=base_url, body=body, data=data,
        timeout=timeout, headers=headers,
    )


def get(
    path: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
) -> ControlResponse:
    return _request("GET", path, base_url=base_url, timeout=timeout, headers=headers)


def post(
    path: str,
    body: dict | None = None,
    *,
    data: bytes | None = None,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
) -> ControlResponse:
    return _request(
        "POST", path, base_url=base_url, body=body, data=data,
        timeout=timeout, headers=headers,
    )


def get_state(
    *, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT
) -> dict:
    """The ``/state`` aggregate as a dict. Raises :class:`ControlError` if
    jasper-control is unreachable; returns ``{}`` for a non-dict body."""
    data = get("/state", base_url=base_url, timeout=timeout).json()
    return data if isinstance(data, dict) else {}


def get_dial_status(
    *, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT
) -> dict:
    """The ``/dial/status`` payload as a dict. Raises on unreachable."""
    data = get("/dial/status", base_url=base_url, timeout=timeout).json()
    return data if isinstance(data, dict) else {}


def healthz(
    *, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT
) -> bool:
    """True iff ``/healthz`` returns 2xx. Never raises — returns False on any
    transport failure (it is a liveness check, so absence == not healthy)."""
    try:
        return get("/healthz", base_url=base_url, timeout=timeout).ok
    except ControlError:
        return False


# --- async API: long-lived daemons (accessory bridge, usbsink) -------------
class AsyncControlClient:
    """Async control client for long-lived daemons. Runs the blocking stdlib
    transport in ``asyncio.to_thread`` so the event loop stays responsive.
    Bind the base URL once (e.g. from ``--control-url``) and reuse it; inject
    a fake in tests. Raises :class:`ControlError` on transport failure.

    Caveat: ``asyncio.to_thread`` cannot be cancelled mid-flight, so if the
    calling task is cancelled while a request is in progress, the worker
    thread runs until the socket ``timeout`` expires. This is bounded (the
    per-request timeout) and only matters on shutdown — the alternative
    (a cancellable transport) means httpx, the RAM cost this avoids.
    """

    def __init__(
        self, base_url: str = DEFAULT_BASE_URL, *, timeout: float = DEFAULT_TIMEOUT
    ) -> None:
        self._base_url = base_url
        self._timeout = timeout

    async def request(
        self, method: str, path: str, body: dict | None = None,
        *, headers: dict[str, str] | None = None,
    ) -> ControlResponse:
        return await asyncio.to_thread(
            _request,
            method,
            path,
            base_url=self._base_url,
            body=body,
            timeout=self._timeout,
            headers=headers,
        )

    async def get(self, path: str) -> ControlResponse:
        return await self.request("GET", path)

    async def post(
        self, path: str, body: dict | None = None,
        *, headers: dict[str, str] | None = None,
    ) -> ControlResponse:
        # `headers=` lets a daemon-side caller attach the household credential
        # (X-JTS-Household) on a cross-device /grouping/set — the autonomous
        # re-grouping path (Phase D). _request refuses to let a caller header
        # clobber Content-Type, so the bearer rides safely.
        return await self.request("POST", path, body, headers=headers)

    async def adjust_volume(self, delta_percent: int) -> ControlResponse:
        return await self.post("/volume/adjust", {"delta_percent": delta_percent})

    async def set_volume(
        self, percent: int, *, source: str | None = None
    ) -> ControlResponse:
        body: dict = {"percent": percent}
        if source is not None:
            body["source"] = source
        return await self.post("/volume/set", body)
