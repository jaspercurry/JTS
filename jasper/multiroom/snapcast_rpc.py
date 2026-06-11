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

logger = logging.getLogger(__name__)

# snapserver's packaged default HTTP JSON-RPC endpoint (snapserver.conf
# [http] section; we ship no override). Loopback only — the reconciler
# and health probes run on the leader itself.
SNAPSERVER_RPC_URL = "http://127.0.0.1:1780/jsonrpc"

_RPC_TIMEOUT_SEC = 2.0


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


def summarize_groups(status: dict) -> list[dict[str, Any]]:
    """Flatten Server.GetStatus into per-client binding rows. PURE.

    Row: ``{group_id, stream_id, name, connected, muted, volume_percent}``
    — one per client, carrying its group's stream binding. Tolerates
    missing keys (snapcast minor-version drift) by defaulting safe."""
    rows: list[dict[str, Any]] = []
    for group in status.get("server", {}).get("groups", []):
        for client in group.get("clients", []):
            volume = client.get("config", {}).get("volume", {})
            rows.append({
                "group_id": group.get("id", ""),
                "stream_id": group.get("stream_id", ""),
                "name": client.get("host", {}).get("name", ""),
                "connected": bool(client.get("connected", False)),
                "muted": bool(volume.get("muted", False)),
                "volume_percent": int(volume.get("percent", 100)),
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


def ensure_groups_on_stream(
    want_stream: str,
    *,
    url: str = SNAPSERVER_RPC_URL,
    transport: Transport = rpc_call,
    attempts: int = 6,
    delay_sec: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Re-bind every persisted snapcast group to ``want_stream``.

    Runs on the leader after snapserver starts (bounded retries cover
    the just-started window). Fixes DISCONNECTED clients' groups too —
    a follower reconnecting tomorrow must not land in a stale binding.

    Returns a report dict: ``{reachable, groups, fixed, failed}``.
    Fail-soft: never raises; an unreachable server reports
    ``reachable=False`` (the caller logs + flips its exit code — a bond
    whose bindings cannot be verified is a degraded bond).
    """
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
        if not group_id or stream_id == want_stream:
            continue
        result = transport(
            "Group.SetStream",
            {"id": group_id, "stream_id": want_stream},
            url=url,
        )
        if result is not None and result.get("stream_id") == want_stream:
            fixed += 1
            logger.info(
                "event=multiroom.stream_binding.fixed group=%s from=%s to=%s",
                group_id[:8], stream_id or "(none)", want_stream,
            )
        else:
            failed += 1
            logger.warning(
                "event=multiroom.stream_binding.fix_failed group=%s from=%s to=%s",
                group_id[:8], stream_id or "(none)", want_stream,
            )
    return {
        "reachable": True, "groups": len(groups), "fixed": fixed, "failed": failed,
    }
