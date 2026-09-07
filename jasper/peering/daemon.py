# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Asyncio orchestrator for the peering subsystem.

Ties together:
  - the pure state machine (jasper.peering.state)
  - the multicast transport (jasper.peering.transport)
  - the UDS server for voice→peering RPC (jasper.peering.uds)
  - the Avahi service-file management (jasper.peering.avahi)

This module owns the asyncio plumbing — timer scheduling,
Future-based RPC response correlation, and the translation of pure
state-machine Actions into actual I/O. The state machine itself
remains pure and unit-testable; this layer is the only place where
real time / real sockets / real OS calls happen.

When PeeringConfig.mode is OFF, `run()` is a fast no-op — no sockets
opened, no Avahi file written. The user opts
in via the /sound/pair/ Speakers page, which restarts jasper-control and picks
up the new config.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from jasper.log_event import log_event

from .config import (
    DEFAULT_HEARTBEAT_INTERVAL_SEC,
    DEFAULT_HEARTBEAT_TIMEOUT_SEC,
    MAX_ARB_WINDOW_MS,
    PEERING_UDS_PATH,
    PeeringConfig,
)
from . import avahi, uds
from .state import (
    Action,
    BroadcastClaim,
    BroadcastEnd,
    BroadcastHeartbeat,
    BroadcastWake,
    CancelTimer,
    Event,
    LocalWake,
    PeerClaim,
    PeerEnd,
    PeerHeartbeat,
    PeerWake,
    PeeringStateMachine,
    ScheduleTimer,
    StandDown,
    StartSession,
    StateMachineParams,
    TimerFired,
    VoiceSessionEnded,
    VoiceSessionStarted,
)
from .transport import (
    IncomingClaim,
    IncomingEnd,
    IncomingHeartbeat,
    IncomingMessage,
    IncomingWake,
    MulticastTransport,
    encode_claim,
    encode_end,
    encode_heartbeat,
    encode_wake,
)

logger = logging.getLogger(__name__)


# Hard timeout for an ARBITRATE RPC. On timeout we fail open (WIN) —
# voice was going to proceed anyway in single-device mode, so a wedged
# peering daemon shouldn't silence the speaker.
#
# It must sit strictly ABOVE the widest configured arb window
# (MAX_ARB_WINDOW_MS, the clamp ceiling), not merely equal it: the
# state machine only emits StartSession/StandDown when the arb-window
# timer fires, so a timeout equal to the window max could fire the
# fail-open WIN in the same tick as (or ahead of) the real decision —
# a race between a stale fail-open WIN and the real StartSession/
# StandDown. The fixed margin guarantees the real decision always
# wins that race even at the maximum window.
ARBITRATE_RPC_MARGIN_SEC = 0.15
ARBITRATE_RPC_TIMEOUT_SEC = MAX_ARB_WINDOW_MS / 1000.0 + ARBITRATE_RPC_MARGIN_SEC


class PeeringDaemon:
    """Long-running peering coordinator. Owned by jasper-control.

    Lifecycle:
      d = PeeringDaemon(cfg)
      await d.start()      # no-op if cfg.mode is OFF
      ...
      await d.stop()
    """

    def __init__(self, cfg: PeeringConfig) -> None:
        self._cfg = cfg
        self._sm: Optional[PeeringStateMachine] = None
        self._transport: Optional[MulticastTransport] = None
        self._uds_server: Optional[asyncio.AbstractServer] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._timers: dict[str, asyncio.TimerHandle] = {}
        # Future that resolves to "WIN" or "LOSE". Created on each
        # ARBITRATE RPC, resolved when StartSession/StandDown fires.
        self._pending_decision: Optional[asyncio.Future[str]] = None
        # Tracks the in-flight arbitration's epoch so we can correlate
        # the StartSession/StandDown back to the RPC.
        self._pending_epoch: Optional[str] = None
        self._send_tasks: set[asyncio.Task[None]] = set()
        self._running = False

    # ---------- public lifecycle ----------

    async def start(self) -> None:
        if not self._cfg.enabled:
            log_event(
                logger,
                "peering.daemon.disabled",
                mode=self._cfg.mode.value,
                note="daemon not started",
            )
            return
        if self._running:
            logger.warning("peering daemon already running")
            return

        self._loop = asyncio.get_running_loop()
        self._sm = PeeringStateMachine(StateMachineParams(
            peer_id=self._cfg.peer_id,
            primary=self._cfg.primary,
            arb_window_sec=self._cfg.arb_window_ms / 1000.0,
            break_threshold=self._cfg.break_threshold,
            heartbeat_interval_sec=DEFAULT_HEARTBEAT_INTERVAL_SEC,
            heartbeat_timeout_sec=DEFAULT_HEARTBEAT_TIMEOUT_SEC,
        ))

        # Install the Avahi service file (best-effort — non-fatal if
        # the template is missing; we'll still arbitrate).
        avahi.render_and_install(
            peer_id=self._cfg.peer_id,
            room=self._cfg.room,
            primary=self._cfg.primary,
        )

        # Multicast transport.
        self._transport = MulticastTransport()
        try:
            await self._transport.start(on_message=self._on_multicast_message)
        except OSError as e:
            logger.error(
                "peering: could not bind multicast socket (%s); "
                "daemon staying down. Likely cause: another process "
                "holds the port.", e,
            )
            self._transport = None
            return

        # UDS server for voice ↔ peering RPC.
        self._uds_server = await uds.serve(
            path=PEERING_UDS_PATH,
            arbitrate=self._handle_arbitrate,
            notify_session_started=self._handle_session_started,
            notify_session_ended=self._handle_session_ended,
        )

        self._running = True
        log_event(
            logger,
            "peering.daemon.started",
            peer_id=self._cfg.peer_id,
            room=self._cfg.room,
            primary=int(self._cfg.primary),
            arb_window_ms=self._cfg.arb_window_ms,
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        log_event(logger, "peering.daemon.stopping")

        # Cancel all timers.
        for handle in self._timers.values():
            handle.cancel()
        self._timers.clear()

        if self._send_tasks:
            pending_sends = tuple(self._send_tasks)
            for task in pending_sends:
                task.cancel()
            await asyncio.gather(*pending_sends, return_exceptions=True)

        if self._uds_server is not None:
            self._uds_server.close()
            try:
                await self._uds_server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._uds_server = None

        if self._transport is not None:
            try:
                await self._transport.stop()
            except Exception:  # noqa: BLE001
                logger.exception("peering: transport stop failed")
            self._transport = None

        # Unpublish Avahi so peers stop seeing us promptly.
        avahi.uninstall()

        # Resolve any in-flight decision as WIN — voice falls back to
        # solo mode rather than hanging.
        if self._pending_decision is not None and not self._pending_decision.done():
            self._pending_decision.set_result("WIN")
        self._pending_decision = None
        self._pending_epoch = None

        log_event(logger, "peering.daemon.stopped")

    # ---------- inbound: multicast ----------

    async def _on_multicast_message(self, msg: IncomingMessage, addr: str) -> None:
        # Filter our own loopback first (IP_MULTICAST_LOOP=1 makes
        # the kernel echo every send back to us). Every message type
        # carries a sender peer id, just at different attribute paths.
        if _sender_peer_id(msg) == self._cfg.peer_id:
            return
        now = self._loop.time()  # type: ignore[union-attr]

        if isinstance(msg, IncomingWake):
            self._dispatch(PeerWake(epoch=msg.epoch, report=msg.report, now=now))
        elif isinstance(msg, IncomingClaim):
            self._dispatch(PeerClaim(
                epoch=msg.epoch, peer_id=msg.peer_id, now=now,
            ))
        elif isinstance(msg, IncomingHeartbeat):
            self._dispatch(PeerHeartbeat(
                epoch=msg.epoch, peer_id=msg.peer_id, now=now,
            ))
        elif isinstance(msg, IncomingEnd):
            self._dispatch(PeerEnd(
                epoch=msg.epoch, peer_id=msg.peer_id,
                reason=msg.reason, now=now,
            ))

    # ---------- inbound: UDS RPC from voice ----------

    async def _handle_arbitrate(self, req: dict) -> dict:
        if not self._running:
            return {"result": "WIN", "epoch": ""}  # daemon not active

        # Cancel any previous pending decision (shouldn't happen — wake
        # refractory prevents concurrent ARBITRATEs — but be defensive).
        if self._pending_decision is not None and not self._pending_decision.done():
            logger.warning("peering: stale pending decision; resolving as WIN")
            self._pending_decision.set_result("WIN")

        score = float(req.get("score", 0.0))
        snr_db = req.get("snr_db")
        rms_dbfs = req.get("rms_dbfs")
        can_serve = bool(req.get("can_serve", True))

        future: asyncio.Future[str] = self._loop.create_future()  # type: ignore[union-attr]
        self._pending_decision = future

        # Dispatch the LocalWake — state machine will emit BroadcastWake
        # + ScheduleTimer(arb_window). When the timer fires, the
        # machine emits StartSession or StandDown and we resolve the
        # future from inside _execute.
        self._dispatch(LocalWake(
            score=score,
            snr_db=_maybe_float(snr_db),
            rms_dbfs=_maybe_float(rms_dbfs),
            can_serve=can_serve,
            now=self._loop.time(),  # type: ignore[union-attr]
        ))

        # The current epoch is whatever the state machine set up.
        self._pending_epoch = self._sm.current_epoch if self._sm else None

        try:
            decision = await asyncio.wait_for(future, timeout=ARBITRATE_RPC_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            logger.warning(
                "peering: arbitrate timeout (>%.2fs) — failing open as WIN",
                ARBITRATE_RPC_TIMEOUT_SEC,
            )
            decision = "WIN"

        epoch = self._pending_epoch or ""
        return {"result": decision, "epoch": epoch}

    async def _handle_session_started(self, epoch: str) -> None:
        if not self._running:
            return
        self._dispatch(VoiceSessionStarted(
            epoch=epoch, now=self._loop.time(),  # type: ignore[union-attr]
        ))

    async def _handle_session_ended(self, epoch: str, reason: str) -> None:
        if not self._running:
            return
        self._dispatch(VoiceSessionEnded(
            epoch=epoch, reason=reason or "ended",
            now=self._loop.time(),  # type: ignore[union-attr]
        ))

    # ---------- dispatch + action execution ----------

    def _dispatch(self, event: Event) -> None:
        if self._sm is None:
            return
        actions = self._sm.handle(event)
        for a in actions:
            self._execute(a)

    def _execute(self, action: Action) -> None:
        ts = time.monotonic_ns()
        peer = self._cfg.peer_id
        match action:
            case BroadcastWake(epoch=epoch, report=report):
                self._spawn_send(encode_wake(epoch=epoch, report=report, ts_ns=ts))
            case BroadcastClaim(epoch=epoch):
                self._spawn_send(encode_claim(epoch=epoch, peer_id=peer, ts_ns=ts))
            case BroadcastHeartbeat(epoch=epoch):
                self._spawn_send(encode_heartbeat(epoch=epoch, peer_id=peer, ts_ns=ts))
            case BroadcastEnd(epoch=epoch, reason=reason):
                self._spawn_send(encode_end(
                    epoch=epoch, peer_id=peer, reason=reason, ts_ns=ts,
                ))
            case StartSession():
                self._resolve_pending("WIN")
            case StandDown():
                self._resolve_pending("LOSE")
            case ScheduleTimer(timer_id=tid, at_monotonic=at):
                self._schedule_timer(tid, at)
            case CancelTimer(timer_id=tid):
                self._cancel_timer(tid)

    def _resolve_pending(self, decision: str) -> None:
        """Resolve the in-flight ARBITRATE RPC future. No-op if nothing
        is pending (e.g. _execute fired StartSession after the RPC
        already timed out and another arbitration is in flight)."""
        if (
            self._pending_decision is not None
            and not self._pending_decision.done()
        ):
            self._pending_decision.set_result(decision)

    def _spawn_send(self, payload: bytes) -> None:
        # stop() clears _running before it yields to cancellation. Refuse any
        # action that races in after that quiesce point so a late send cannot
        # appear after stop's task snapshot and escape its drain.
        if not self._running or self._transport is None or self._loop is None:
            return
        # Best-effort fire-and-forget; the recv side has its own error
        # handling. Spawn a task rather than awaiting so the state
        # machine's action loop isn't blocked.
        task = self._loop.create_task(
            self._transport.send(payload),
            name="peering-send",
        )
        self._send_tasks.add(task)
        task.add_done_callback(self._send_tasks.discard)

    def _schedule_timer(self, timer_id: str, at_monotonic: float) -> None:
        if self._loop is None:
            return
        # If an existing timer of this id is pending, cancel it first
        # so we don't fire twice.
        self._cancel_timer(timer_id)
        delay = max(0.0, at_monotonic - self._loop.time())
        handle = self._loop.call_later(
            delay, self._on_timer_fired, timer_id,
        )
        self._timers[timer_id] = handle

    def _cancel_timer(self, timer_id: str) -> None:
        handle = self._timers.pop(timer_id, None)
        if handle is not None:
            handle.cancel()

    def _on_timer_fired(self, timer_id: str) -> None:
        # The TimerHandle has already fired; remove it from the table
        # before dispatching (so the handler can schedule a fresh timer
        # under the same id without double-cancellation surprises).
        self._timers.pop(timer_id, None)
        if self._loop is None:
            return
        self._dispatch(TimerFired(timer_id=timer_id, now=self._loop.time()))


def _maybe_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _sender_peer_id(msg: IncomingMessage) -> str:
    """Extract the sender's peer_id from any IncomingMessage variant.

    CLAIM/HEART/END carry it on `.peer_id`; WAKE nests it inside
    `.report.peer_id` because the WakeReport already has the field.
    Centralizing the lookup keeps the multicast dispatcher's
    self-loopback filter to one line."""
    if isinstance(msg, IncomingWake):
        return msg.report.peer_id
    return getattr(msg, "peer_id", "")
