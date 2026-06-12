"""Runtime liveness supervisor for a bonded multiroom member.

The grouping reconciler converges a bond at RECONCILE TIME (boot,
wizard save, deploy). This supervisor closes the runtime gap between
reconciles — the two silence classes observed during the 2026-06-11
bring-up, both of which left every systemd unit green:

  1. **Round-trip starvation.** The member's snapclient stops feeding
     /run/jasper-grouping/member-content.fifo (process gone, wedged,
     or the dac_content lane never armed because a reconcile failed).
     outputd's per-period fallback keeps the DAC fed with silence —
     correct for a transient, but a SUSTAINED fallback means the
     speaker plays no music indefinitely (inv-B). Detection: outputd's
     STATUS `dac_content.serving_fifo`. Repair: kick the reconciler —
     the designed convergence engine (units, env, camilla config,
     stream binding) — rate-limited so a non-converging failure makes
     bounded noise instead of a restart storm.

  2. **Binding drift** (leader only). Snapcast PERSISTS group→stream
     bindings in server.json, and any snapcast app on the LAN can
     rebind a group at runtime; a group bound elsewhere receives no
     chunks while snapclient faithfully plays the silence. The
     reconciler pins bindings at reconcile time;
     `ensure_groups_on_stream` re-run on every poll makes that pin
     continuous. The pin IS the repair — no kick needed, and a healthy
     poll costs one loopback RPC (~1 ms).

Starvation watch runs on EVERY bonded member (leader and follower —
the local dataplane is identical); binding repair runs on the leader
(snapserver's RPC is loopback-only).

No active-session gate, deliberately: a starved lane means no music is
reaching the DAC, so there is nothing to disrupt, and the kick is a
near-no-op on a converged system (compare-before-write env, idempotent
unit plan, idempotent pin). A follower whose leader is powered off
will kick once per rate-limit window indefinitely — harmless, and the
bond IS degraded; /rooms and the doctor carry the user-facing story.

Out of scope (deferred until a real non-converging shape is observed):
auto-unwind to solo. Tearing down a bond the user explicitly created
is a judgment call we don't make from a 30 s poll; disband stays one
tap away on /rooms.

Disable knob: set `JASPER_GROUPING_SUPERVISOR=disabled` in
/etc/jasper/jasper.env and restart jasper-control. Mirrors
JASPER_SHAIRPORT_SUPERVISOR / JASPER_SYSTEM_SUPERVISOR (exact match,
case-insensitive; anything else logs a warning and stays enabled).

Design home: docs/HANDOFF-multiroom.md (Increment 5).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import threading
import time
from typing import Any

from ..multiroom.config import GroupingConfig, is_active_member, load_config
from ..multiroom.reconcile import SNAP_STREAM_ID
from ..multiroom.snapcast_rpc import ensure_groups_on_stream

logger = logging.getLogger(__name__)

OUTPUTD_CONTROL_SOCKET = "/run/jasper-outputd/control.sock"
RECONCILE_UNIT = "jasper-grouping-reconcile.service"


class GroupingSupervisor:
    """Polls bond runtime truth; kicks the reconciler on confident starvation.

    Override `load_grouping`, `outputd_status`, `repair_bindings`, or
    `kick_reconciler` to inject test doubles. The policy in `_tick` is
    the unit under test.
    """

    DEFAULT_INTERVAL_SEC = 30.0
    DEFAULT_JITTER_SEC = 3.0
    DEFAULT_PROBE_TIMEOUT_SEC = 2.0
    # 3 polls ≈ 90 s of sustained fallback before the kick — long
    # enough to absorb a deploy's outputd restart or a reconcile
    # mid-run, short enough that a dead dataplane self-heals inside
    # two minutes.
    DEFAULT_STARVED_THRESHOLD = 3
    DEFAULT_KICK_RATE_LIMIT_SEC = 600.0
    DEFAULT_COLD_START_SEC = 60.0

    def __init__(
        self,
        *,
        interval_sec: float = DEFAULT_INTERVAL_SEC,
        jitter_sec: float = DEFAULT_JITTER_SEC,
        probe_timeout_sec: float = DEFAULT_PROBE_TIMEOUT_SEC,
        starved_threshold: int = DEFAULT_STARVED_THRESHOLD,
        kick_rate_limit_sec: float = DEFAULT_KICK_RATE_LIMIT_SEC,
        cold_start_sec: float = DEFAULT_COLD_START_SEC,
    ) -> None:
        self._interval = interval_sec
        self._jitter = jitter_sec
        self._probe_timeout = probe_timeout_sec
        self._threshold = starved_threshold
        self._rate_limit = kick_rate_limit_sec
        self._cold_start = cold_start_sec
        # Observable state — read by snapshot() for /state. *_at fields
        # are wall-clock (time.time()) for dashboard rendering.
        self.watching: bool = False          # bonded-active at last poll
        self.last_poll_at: float | None = None
        self.last_poll_starved: bool | None = None
        self.consecutive_starved: int = 0
        self.kick_count: int = 0
        self.last_kick_at: float | None = None
        self.rate_limited_count: int = 0
        self.binding_last_reachable: bool | None = None
        self.binding_fixed_total: int = 0
        self.binding_failed_total: int = 0
        self.binding_last_repair_at: float | None = None
        # Monotonic clock for rate-limit math — separate from the
        # wall-clock display fields so NTP jumps can't reopen or
        # extend the window.
        self._last_kick_monotonic: float | None = None
        # Journal-noise latches. A follower whose leader is unplugged
        # for WEEKS is a legitimate long-lived state — the journal gets
        # the full WARN buildup once per starvation streak and one
        # rate-limited WARN per kick window, then one ERROR per kick;
        # everything else drops to DEBUG. (/state carries the live
        # counters regardless.) Both reset on a healthy poll.
        self._streak_warned: bool = False
        self._rate_limit_warned_window: float | None = None

    # ---- main loop ----

    async def run(self) -> None:
        logger.info(
            "event=grouping_supervisor.start "
            "interval=%.0fs threshold=%d rate_limit=%.0fs",
            self._interval, self._threshold, self._rate_limit,
        )
        await asyncio.sleep(self._cold_start)
        while True:
            try:
                await self._tick()
            except Exception:  # noqa: BLE001
                logger.exception("event=grouping_supervisor.tick_crash")
            await asyncio.sleep(self._interval + random.uniform(
                -self._jitter, self._jitter,
            ))

    async def _tick(self) -> None:
        cfg = self.load_grouping()
        self.last_poll_at = time.time()
        if not is_active_member(cfg):
            # Solo, or bonded-but-invalid (the reconciler refuses to run
            # a broken bond, so there is no dataplane to supervise — the
            # doctor owns the configured-but-broken story). Reset so a
            # later re-bond starts from a clean confidence window.
            self.watching = False
            self.last_poll_starved = None
            self.consecutive_starved = 0
            return
        self.watching = True
        if cfg.role == "leader":
            await self._binding_tick()
        await self._starvation_tick()

    async def _binding_tick(self) -> None:
        """Continuous read-repair of snapcast group→stream bindings."""
        try:
            report = await self.repair_bindings()
        except Exception:  # noqa: BLE001
            logger.exception("event=grouping_supervisor.binding_crash")
            return
        self.binding_last_reachable = bool(report.get("reachable"))
        fixed = int(report.get("fixed", 0))
        failed = int(report.get("failed", 0))
        self.binding_fixed_total += fixed
        self.binding_failed_total += failed
        if fixed or failed:
            self.binding_last_repair_at = time.time()
            # Runtime drift is rare and always operator-relevant —
            # someone or something rebound a group out from under the
            # bond since the last reconcile.
            logger.warning(
                "event=grouping_supervisor.binding_repaired "
                "fixed=%d failed=%d stream=%s",
                fixed, failed, SNAP_STREAM_ID,
            )

    async def _starvation_tick(self) -> None:
        status = None
        try:
            status = await self.outputd_status()
        except Exception:  # noqa: BLE001
            logger.exception("event=grouping_supervisor.status_crash")
        dac = (status or {}).get("dac_content") or {}
        # A bonded member is healthy only when the lane is armed AND
        # serving. "outputd unreachable" and "lane never armed" both
        # count as starved on purpose: each is a state the reconciler
        # kick converges (env write + outputd restart), and the
        # threshold absorbs the transient shapes (deploy restart,
        # reconcile mid-run).
        starved = not (
            dac.get("enabled") is True and dac.get("serving_fifo") is True
        )
        self.last_poll_starved = starved
        if not starved:
            self.consecutive_starved = 0
            self._streak_warned = False
            self._rate_limit_warned_window = None
            return
        self.consecutive_starved += 1
        log = logger.debug if self._streak_warned else logger.warning
        log(
            "event=grouping_supervisor.starved consecutive=%d threshold=%d "
            "outputd_reachable=%s lane_enabled=%s",
            self.consecutive_starved, self._threshold,
            status is not None, dac.get("enabled") is True,
        )
        if self.consecutive_starved >= self._threshold:
            self._streak_warned = True
        if self.consecutive_starved < self._threshold:
            return
        mono = self._now()
        if (
            self._last_kick_monotonic is not None
            and mono - self._last_kick_monotonic < self._rate_limit
        ):
            self.rate_limited_count += 1
            log = (
                logger.debug
                if self._rate_limit_warned_window == self._last_kick_monotonic
                else logger.warning
            )
            self._rate_limit_warned_window = self._last_kick_monotonic
            log(
                "event=grouping_supervisor.kick_rate_limited "
                "since_last_kick=%.0fs limit=%.0fs",
                mono - self._last_kick_monotonic, self._rate_limit,
            )
            return
        self._last_kick_monotonic = mono
        self.last_kick_at = time.time()
        self.kick_count += 1
        # Reset the confidence window so the reconciler gets a full
        # threshold's worth of polls to take effect before we conclude
        # the kick didn't help.
        self.consecutive_starved = 0
        logger.error(
            "event=grouping_supervisor.starved_detected action=kick_reconcile "
            "count=%d",
            self.kick_count,
        )
        try:
            await self.kick_reconciler()
        except Exception:  # noqa: BLE001
            logger.exception("event=grouping_supervisor.kick_failed")

    # ---- overridable IO ----

    def load_grouping(self) -> GroupingConfig:
        """Fresh read of the wizard-owned grouping.env (one file read)."""
        return load_config()

    async def outputd_status(self) -> dict | None:
        """One-shot STATUS probe of outputd's control socket.

        Self-contained UDS probe (the supervisor owns its probe, like
        ShairportSupervisor owns its RTSP probe). None on any failure —
        the caller treats None as "outputd unreachable".
        """
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(OUTPUTD_CONTROL_SOCKET),
                timeout=self._probe_timeout,
            )
        except (OSError, asyncio.TimeoutError):
            return None
        try:
            writer.write(b"STATUS\n")
            await writer.drain()
            body = await asyncio.wait_for(
                reader.read(8192), timeout=self._probe_timeout,
            )
        except (OSError, asyncio.TimeoutError):
            return None
        finally:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except (OSError, asyncio.TimeoutError, AssertionError):
                pass
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    async def repair_bindings(self) -> dict[str, Any]:
        """Run the binding pin once (no retries — we poll again in 30 s).

        ensure_groups_on_stream is sync urllib against loopback; run it
        off the event loop so a hung snapserver accept can't stall the
        supervisor's loop beyond the RPC timeout.
        """
        return await asyncio.to_thread(
            ensure_groups_on_stream, SNAP_STREAM_ID, attempts=1,
        )

    async def kick_reconciler(self) -> None:
        """`reset-failed` clears StartLimitBurst parking from prior failed
        reconciles (rc=1 on unreachable snapserver is by-design); then
        `restart --no-block` (restart, not start — a oneshot mid-run must
        not make the kick a no-op; mirrors _kick_grouping_reconciler)."""
        reset = await asyncio.create_subprocess_exec(
            "systemctl", "reset-failed", RECONCILE_UNIT,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(reset.wait(), timeout=5.0)
        restart = await asyncio.create_subprocess_exec(
            "systemctl", "--no-block", "restart", RECONCILE_UNIT,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(restart.wait(), timeout=5.0)

    # ---- accessors ----

    def _now(self) -> float:
        # Seam for rate-limit testing — overridden in tests.
        return time.monotonic()

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "watching": self.watching,
            "last_poll_at": self.last_poll_at,
            "last_poll_starved": self.last_poll_starved,
            "consecutive_starved": self.consecutive_starved,
            "kick_count": self.kick_count,
            "last_kick_at": self.last_kick_at,
            "rate_limited_count": self.rate_limited_count,
            "binding": {
                "last_reachable": self.binding_last_reachable,
                "fixed_total": self.binding_fixed_total,
                "failed_total": self.binding_failed_total,
                "last_repair_at": self.binding_last_repair_at,
            },
        }


# Module-level supervisor instance, set by start_supervisor() when
# enabled. snapshot() reads it; the HTTP handler thread is read-only
# wrt the instance and Python attribute reads are atomic at the
# snapshot dict's resolution.
_supervisor: GroupingSupervisor | None = None
_supervisor_thread: threading.Thread | None = None


def snapshot() -> dict[str, Any]:
    """Read-only state for /state. Returns `{"enabled": False}` when
    the supervisor is disabled or not yet running."""
    if _supervisor is None:
        return {"enabled": False}
    return _supervisor.snapshot()


def start_supervisor() -> threading.Thread | None:
    """Start the supervisor in a background thread. No-op when
    `JASPER_GROUPING_SUPERVISOR=disabled` (exact match, case-
    insensitive). Idempotent under sequential calls — the sole
    caller is `jasper-control`'s single-threaded `main()`."""
    global _supervisor, _supervisor_thread
    if _supervisor_thread is not None:
        return _supervisor_thread
    mode = os.environ.get("JASPER_GROUPING_SUPERVISOR", "auto").lower()
    if mode == "disabled":
        logger.info("event=grouping_supervisor.disabled")
        return None
    if mode != "auto":
        logger.warning(
            "JASPER_GROUPING_SUPERVISOR=%r unrecognized; "
            "treating as 'auto'. Use 'disabled' to turn the supervisor off.",
            mode,
        )
    _supervisor = GroupingSupervisor()

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_supervisor.run())
        except Exception:  # noqa: BLE001
            logger.exception("event=grouping_supervisor.thread_crash")
        finally:
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    _supervisor_thread = threading.Thread(
        target=_run, name="grouping-supervisor", daemon=True,
    )
    _supervisor_thread.start()
    return _supervisor_thread
