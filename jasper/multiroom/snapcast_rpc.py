# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Snapserver JSON-RPC client + the group→stream binding pin.

THE INCIDENT THIS EXISTS FOR (2026-06-11 bring-up): snapcast PERSISTS
its client→group→stream assignments in server.json across restarts and
re-installs. Both speakers' groups predated our server (bound to a
stream named "default" from the distro-snapserver era), our stream is
named ``jts`` — and a group bound to a nonexistent stream receives no
chunks, so snapclient's software mixer faithfully played SILENCE into
the round-trip FIFO while every unit and health surface stayed green.
Fixed live via ``Group.SetStream``; this module makes that durable:

  - :func:`ensure_groups_on_stream` — the reconciler's binding pin,
    run after snapserver starts on a leader: every persisted group is
    re-bound to OUR stream (connected or not — a follower reconnecting
    later must not land in a stale binding). Bounded retries because
    snapserver has just been started.
  - :func:`read_stream_clients` — the health-truthing probe: the
    leader's runtime health (/state + doctor) derives from WHO is
    actually connected, bound to WHICH stream, at WHAT volume — the
    three silence classes unit-state health structurally cannot see.

Stdlib-only (urllib against 127.0.0.1; the RPC port is snapserver's
packaged default). Every I/O entry point is fail-soft and takes an
injectable ``transport`` so the logic is hardware-free testable.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any, Callable

from ..log_event import log_event

logger = logging.getLogger(__name__)

# snapserver's packaged default HTTP JSON-RPC endpoint (snapserver.conf
# [http] section; we ship no override). Loopback only — the reconciler
# and health probes run on the leader itself.
SNAPSERVER_RPC_URL = "http://127.0.0.1:1780/jsonrpc"

# Loopback-only RPC: a healthy snapserver answers in ~1 ms; the timeout
# exists for the pathological hung-accept case, and it bounds how long a
# /state caller can stall (see read_stream_clients_cached). 1 s is
# generous for localhost.
_RPC_TIMEOUT_SEC = 1.0


def rpc_call(
    method: str, params: dict | None = None, *, url: str = SNAPSERVER_RPC_URL,
) -> dict | None:
    """One JSON-RPC call. Returns the ``result`` dict, or None on any
    failure (connection refused while snapserver boots, timeout, bad
    payload) — callers treat None as "snapserver unreachable"."""
    body = {"id": 1, "jsonrpc": "2.0", "method": method}
    if params is not None:
        body["params"] = params
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_RPC_TIMEOUT_SEC) as resp:
            payload = json.loads(resp.read().decode())
        return payload.get("result")
    except Exception as e:  # noqa: BLE001 — fail-soft by contract
        logger.debug("snapcast rpc %s failed: %s", method, e)
        return None


Transport = Callable[..., "dict | None"]


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def summarize_groups(status: dict) -> list[dict[str, Any]]:
    """Flatten Server.GetStatus into per-client binding rows. PURE.

    Row: ``{group_id, stream_id, client_id, name, connected, muted,
    volume_percent, latency_ms, group_muted, stream_status}`` — one per
    client, carrying its group's stream binding. Tolerates missing keys
    (snapcast minor-version drift) by defaulting safe.

    Snapcast's documented control API exposes connection, binding,
    configured latency, stream status, and software volume. It does NOT
    expose follower buffer fill, drift, or a clock-lock bit; keep those
    out of this summary rather than implying stronger sync truth than the
    RPC can provide.
    """
    rows: list[dict[str, Any]] = []
    server = status.get("server", {})
    streams = {
        stream.get("id", ""): stream.get("status", "")
        for stream in server.get("streams", [])
        if isinstance(stream, dict)
    }
    for group in server.get("groups", []):
        stream_id = group.get("stream_id", "")
        for client in group.get("clients", []):
            volume = client.get("config", {}).get("volume", {})
            rows.append({
                "group_id": group.get("id", ""),
                "stream_id": stream_id,
                "stream_status": streams.get(stream_id, ""),
                "client_id": client.get("id", ""),
                "name": client.get("host", {}).get("name", ""),
                "connected": bool(client.get("connected", False)),
                "group_muted": bool(group.get("muted", False)),
                "muted": bool(volume.get("muted", False)),
                "volume_percent": _int_or(volume.get("percent", 100), 100),
                "latency_ms": _int_or(
                    client.get("config", {}).get("latency", 0), 0,
                ),
            })
    return rows


def read_stream_clients(
    *, url: str = SNAPSERVER_RPC_URL, transport: Transport = rpc_call,
) -> list[dict[str, Any]] | None:
    """The health-truthing probe: current client binding rows, or None
    when snapserver is unreachable (the caller decides what unreachable
    means for its health verdict). One RPC, fail-soft."""
    status = transport("Server.GetStatus", url=url)
    if status is None:
        return None
    return summarize_groups(status)


def set_client_latency(
    client_id: str,
    latency_ms: int,
    *,
    url: str = SNAPSERVER_RPC_URL,
    transport: Transport = rpc_call,
) -> bool:
    """Set Snapcast's fixed whole-client PCM/output-path latency.

    This is not the group/network buffer policy and not an acoustic
    room-delay knob. It maps directly to Snapcast JSON-RPC
    ``Client.SetLatency`` and returns a boolean so callers can fail
    loudly without knowing RPC payload details.
    """
    result = transport(
        "Client.SetLatency",
        {"id": client_id, "latency": int(latency_ms)},
        url=url,
    )
    return result is not None


def set_client_volume(
    client_id: str,
    *,
    percent: int,
    muted: bool,
    url: str = SNAPSERVER_RPC_URL,
    transport: Transport = rpc_call,
) -> bool:
    """Set Snapcast's per-client software volume/mute.

    Used only by guarded calibration sessions: callers snapshot first and
    restore after measurement. Returns a boolean so calibration can fail loudly
    when a hidden software mixer cannot be normalized.
    """
    pct = max(0, min(100, int(percent)))
    result = transport(
        "Client.SetVolume",
        {"id": client_id, "volume": {"percent": pct, "muted": bool(muted)}},
        url=url,
    )
    return result is not None


def set_group_mute(
    group_id: str,
    muted: bool,
    *,
    url: str = SNAPSERVER_RPC_URL,
    transport: Transport = rpc_call,
) -> bool:
    """Set Snapcast group mute state, fail-soft."""
    result = transport(
        "Group.SetMute",
        {"id": group_id, "mute": bool(muted)},
        url=url,
    )
    return result is not None


class _ProbeCache:
    """Thread-safe TTL cache for the stream-clients probe.

    Why: ``/state`` is polled by the dashboard (~7 s) and any LAN
    client; on a bonded leader each call probes snapserver. A healthy
    probe is ~1 ms — but a HUNG-accepting snapserver costs the full RPC
    timeout per call, turning a snapserver failure into a sluggish
    dashboard. The cache bounds that: at most one real probe per TTL,
    and FAILURES are cached too (a hung server must not be re-probed by
    every poller). The doctor deliberately bypasses this (operator-run,
    wants this-instant truth); the reconciler's pin uses its own
    GetStatus. Monotonic clock; injectable for tests.
    """

    def __init__(self, ttl_sec: float = 5.0) -> None:
        import threading

        self._ttl = ttl_sec
        self._lock = threading.Lock()
        self._at: float | None = None
        self._value: list[dict[str, Any]] | None = None

    def read(
        self,
        *,
        url: str = SNAPSERVER_RPC_URL,
        transport: Transport = rpc_call,
        now: Callable[[], float] = time.monotonic,
    ) -> list[dict[str, Any]] | None:
        with self._lock:
            t = now()
            if self._at is None or (t - self._at) >= self._ttl:
                self._value = read_stream_clients(url=url, transport=transport)
                self._at = t
            # Defensive copy: the cached rows are handed to every caller
            # in the TTL window — a caller mutating a row must never
            # poison the cache for the others. Today's one consumer
            # (derive_grouping_runtime) is pure, but that is a contract
            # this copy makes unnecessary to rely on. Cheap: a bond has
            # a handful of clients.
            if self._value is None:
                return None
            return [dict(row) for row in self._value]


_probe_cache = _ProbeCache()


def read_stream_clients_cached(
    *,
    url: str = SNAPSERVER_RPC_URL,
    transport: Transport = rpc_call,
    now: Callable[[], float] = time.monotonic,
) -> list[dict[str, Any]] | None:
    """The /state-facing probe: same contract as
    :func:`read_stream_clients`, served through the module TTL cache so
    a hung snapserver costs at most one RPC timeout per TTL window
    instead of one per dashboard poll."""
    return _probe_cache.read(url=url, transport=transport, now=now)


def ensure_groups_on_stream(
    want_stream: str,
    *,
    allowed_streams: "set[str] | None" = None,
    url: str = SNAPSERVER_RPC_URL,
    transport: Transport = rpc_call,
    attempts: int = 6,
    delay_sec: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Re-bind persisted snapcast groups to ``want_stream`` — OWNERSHIP
    rule, not existence rule.

    A group is left alone iff its binding is in the JTS-owned allowlist
    (``{want_stream} | allowed_streams``); ANY other binding is rebound
    to ``want_stream`` — whether the foreign stream exists or not.
    Existence deliberately does not protect a binding: snapserver also
    registers the packaged /etc/snapserver.conf "default" pipe source,
    so the 2026-06-11 incident's stale groups were bound to a stream
    that EXISTS (idle, producer-less) — an existence rule would have
    left the bond silent. When a future feature adds a second JTS
    stream (e.g. group announcements), its caller extends
    ``allowed_streams``; until then the allowlist is just our one
    stream and this is the single-stream-era behavior.

    Runs on the leader after snapserver starts (bounded retries cover
    the just-started window). Fixes DISCONNECTED clients' groups too —
    a follower reconnecting tomorrow must not land in a stale binding.

    Returns a report dict: ``{reachable, groups, fixed, failed}``.
    Fail-soft: never raises; an unreachable server reports
    ``reachable=False`` (the caller logs + flips its exit code — a bond
    whose bindings cannot be verified is a degraded bond).
    """
    allowed = {want_stream} | (allowed_streams or set())
    status: dict | None = None
    for attempt in range(attempts):
        status = transport("Server.GetStatus", url=url)
        if status is not None:
            break
        if attempt < attempts - 1:
            sleep(delay_sec)
    if status is None:
        return {"reachable": False, "groups": 0, "fixed": 0, "failed": 0}

    fixed = 0
    failed = 0
    groups = status.get("server", {}).get("groups", [])
    for group in groups:
        group_id = group.get("id", "")
        stream_id = group.get("stream_id", "")
        if not group_id or stream_id in allowed:
            continue
        result = transport(
            "Group.SetStream",
            {"id": group_id, "stream_id": want_stream},
            url=url,
        )
        if result is not None and result.get("stream_id") == want_stream:
            fixed += 1
            log_event(
                logger,
                "multiroom.stream_binding.fixed",
                **{
                    "group": group_id[:8],
                    "from": stream_id or "(none)",
                    "to": want_stream,
                },
            )
        else:
            failed += 1
            log_event(
                logger,
                "multiroom.stream_binding.fix_failed",
                **{
                    "group": group_id[:8],
                    "from": stream_id or "(none)",
                    "to": want_stream,
                },
                level=logging.WARNING,
            )
    return {
        "reachable": True, "groups": len(groups), "fixed": fixed, "failed": failed,
    }
