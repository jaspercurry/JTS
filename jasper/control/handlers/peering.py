# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Multi-device peering daemon lifecycle and the bonded-follower pair-action
forward proxy: the two concerns that route a request (or this process' own
lifecycle) toward another speaker in the household rather than handling it
locally."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
import urllib.error
import urllib.request
from typing import Any, cast

from ...log_event import log_event
from ...service_units import read_unit_states
from ..supervisor_runtime import signal_on_control_loop, spawn_on_control_loop
from ._base import ControlHandlerMixin, logger

# ---------- peering daemon ----------

# The peering daemon needs an asyncio loop; jasper-control is stdlib threaded
# HTTP. It runs as one coroutine on the shared control loop that also hosts
# the supervisors (jasper/control/supervisor_runtime.py).
_peering_lock = threading.Lock()
_peering_task: concurrent.futures.Future[None] | None = None
_peering_shutdown: asyncio.Event | None = None


async def _run_peering(shutdown: asyncio.Event) -> None:
    """Own the peering daemon until `shutdown` is set by stop_peering_daemon."""
    global _peering_task
    # lazy: import cost — these load on the control loop's thread rather
    # than on jasper-control's startup import path.
    from ...peering import load_config
    from ...peering.daemon import PeeringDaemon

    daemon = None
    try:
        daemon = PeeringDaemon(load_config())
        await daemon.start()
        await shutdown.wait()
    finally:
        if daemon is not None:
            try:
                await daemon.stop()
            except Exception:  # noqa: BLE001
                logger.exception("peering daemon stop failed")
        with _peering_lock:
            _peering_task = None


def start_peering_daemon_if_enabled() -> None:
    """Start the peering daemon coroutine. Idempotent.

    PeeringDaemon.start() owns the enabled check: it reads
    /var/lib/jasper/peering.env and opens no socket and installs no mDNS
    advert when peering is off.
    """
    global _peering_task, _peering_shutdown
    with _peering_lock:
        if _peering_task is not None:
            return
        shutdown = asyncio.Event()
        _peering_shutdown = shutdown
        _peering_task = spawn_on_control_loop(
            target=lambda: _run_peering(shutdown),
            name="peering-daemon",
            logger=logger,
            crash_event="peering.daemon.crash",
        )


def stop_peering_daemon(*, timeout: float = 5.0) -> None:
    """Stop the peering coroutine so daemon.stop() can unpublish mDNS.

    Joins the coroutine's own cleanup — the future resolves only after it
    has unwound — under a hard bound: a peering daemon that will not stop
    must not hold jasper-control's shutdown open.
    """
    with _peering_lock:
        future, shutdown = _peering_task, _peering_shutdown
    if future is None or shutdown is None:
        return
    signal_on_control_loop(shutdown)
    try:
        future.result(timeout)
    except concurrent.futures.TimeoutError:
        log_event(
            logger,
            "peering.daemon.stop_timeout",
            timeout=f"{timeout:.1f}",
            level=logging.WARNING,
        )


# ---------- bonded-follower pair-action forward ----------

# Forwarded pair action requests carry this header; its presence stops a
# second hop (see PeeringRoutes._maybe_forward_pair_action_to_leader's loop
# breaker).
_PAIR_FORWARD_HEADER = "X-JTS-Pair-Forwarded"
_VOICE_UNIT = "jasper-voice.service"
_VOICE_TRANSIENT_ACTIVE_STATES = frozenset({
    "activating",
    "deactivating",
    "reloading",
})
# Bounds the /mic request this read sits on; a wedged systemd must not hold it.
_VOICE_UNIT_SHOW_TIMEOUT_SECONDS = 1.0

# Patch seam scoping a test double to the forward's ONE network call;
# patching stdlib urllib.request.urlopen would also intercept the test
# driver's own HTTP client.
_pair_urlopen = urllib.request.urlopen


def _pair_follower_leader_addr() -> str | None:
    """The leader's handle when THIS speaker is an active bonded follower,
    else None. One tiny env-file read per call (multiroom.config.load_config
    — never the runtime derive with its systemctl/RPC probes: this gates
    every /volume request). The predicate is the shared effective-role
    reader, so a refused bond that safely landed solo does not forward local
    controls to the requested leader."""
    from ...multiroom.config import load_config
    from ...multiroom.effective_role import effective_follower_leader_addr

    return effective_follower_leader_addr(load_config())


def _bonded_follower_mic_payload(leader: str) -> dict[str, Any]:
    return {
        "status": "parked",
        "reason": "bonded_follower",
        "available": False,
        "muted": True,
        "pair_leader": leader,
        "message": "Paired — the assistant listens on the pair leader",
    }


def _voice_starting_mic_payload() -> dict[str, Any] | None:
    """Return a first-class /mic payload while jasper-voice is in flight.

    The voice daemon creates its UDS socket late in startup, so during a
    restart/provider switch/unbond a missing socket means "not ready yet",
    not "offline". The distinction is drawn here so the landing page stays a
    dumb renderer of /mic state.
    """
    states = read_unit_states((_VOICE_UNIT,), timeout=_VOICE_UNIT_SHOW_TIMEOUT_SECONDS)
    record = (states or {}).get(_VOICE_UNIT) or {}
    active_state = str(record.get("active_state") or "")
    if active_state not in _VOICE_TRANSIENT_ACTIVE_STATES:
        return None
    return {
        "status": "starting",
        "reason": "voice_daemon_starting",
        "available": False,
        "muted": True,
        "message": "Voice control is restarting",
        "unit": {
            "name": _VOICE_UNIT,
            "active_state": active_state,
            "sub_state": record.get("sub_state"),
            "result": record.get("result"),
        },
    }


def _voice_offline_mic_payload(error: str) -> dict[str, Any]:
    return {
        "status": "offline",
        "reason": "voice_daemon_unreachable",
        "available": False,
        "muted": True,
        "message": "Voice control offline",
        "error": error,
    }


class PeeringRoutes(ControlHandlerMixin):
    def _maybe_forward_pair_action_to_leader(self) -> bool:
        """Bonded-follower pair-action proxy. Returns True when the request
        was handled (forwarded or rejected) and the caller must stop.

        Used by the four /volume* handlers, /transport/*, and
        /source/select — every surface where a bonded follower's local
        action must target the PAIR. While this speaker is an ACTIVE
        bonded follower its local volume knobs are INERT: bonded content
        bypasses the local CamillaDSP entirely (the leader's one Camilla
        bakes the program). So those requests are forwarded verbatim to
        the leader's control API and its answer relayed, and every
        member's volume surface controls the PAIR volume. Solo and leader
        requests never enter this path; the grouping read is one tiny
        env-file parse (load_config), NOT the heavy runtime derive — this
        sits on every volume call.
        """
        leader = _pair_follower_leader_addr()
        if leader is None:
            return False
        # Loop breaker: a forwarded request never re-forwards. Two
        # speakers misconfigured as each other's follower would
        # otherwise ping-pong until a timeout stack built up.
        if self.headers.get(_PAIR_FORWARD_HEADER):
            # Drain any body before responding so connection state stays
            # sane if keep-alive is ever enabled (HTTP/1.0 today).
            try:
                stale = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                stale = 0
            if self.command == "POST" and stale > 0:
                self.rfile.read(stale)
            self._send_json(
                {"error": "pair forward loop (both speakers are "
                          "followers?)", "pair_leader": leader},
                status=502,
            )
            return True
        body: bytes | None = None
        if self.command == "POST":
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            body = self.rfile.read(length) if length > 0 else b"{}"
        # self.server is a ThreadingHTTPServer at runtime (an AF_INET
        # socketserver.TCPServer); BaseServer's broader socketserver.pyi
        # type covers AF_UNIX too, hence the cast.
        server_port = cast(
            "tuple[str, int]", self.server.server_address,
        )[1]
        url = "http://{}:{}{}".format(
            leader, server_port, self.path,
        )
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                _PAIR_FORWARD_HEADER: "1",
            },
            method=self.command,
        )
        try:
            with _pair_urlopen(req, timeout=2.5) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            # The leader ANSWERED — relay its status + JSON body verbatim.
            # Collapsing a 400 invalid-body reject into "unreachable"
            # would report a responding speaker as offline.
            try:
                relayed = json.loads(e.read().decode())
            except Exception:  # noqa: BLE001 — non-JSON error body
                relayed = {"error": f"pair leader error: {e}"}
            if isinstance(relayed, dict):
                relayed.setdefault("pair_leader", leader)
            log_event(
                logger,
                "pair.action_forward_rejected",
                leader=leader,
                path=self.path,
                status=e.code,
                level=logging.WARNING,
            )
            self._send_json(relayed, status=e.code)
            return True
        except Exception as e:  # noqa: BLE001 — transport failure: 502
            log_event(
                logger,
                "pair.action_forward_failed",
                leader=leader,
                path=self.path,
                error=str(e),
                level=logging.WARNING,
            )
            self._send_json(
                {"error": f"pair leader unreachable: {e}",
                 "pair_leader": leader},
                status=502,
            )
            return True
        if isinstance(payload, dict):
            # Additive marker so UIs can label the slider "pair volume".
            payload.setdefault("pair_leader", leader)
        self._send_json(payload)
        return True
