"""Protocol-level liveness supervisor for shairport-sync — Tier 3
of the JTS resilience ladder.

shairport-sync's AP2 control plane occasionally wedges: the process
stays alive, mDNS still advertises, MPRIS still answers, but new
AirPlay SETUPs hang after `accept()`. `Restart=always` is blind to
this; the only existing fix is the manual `scripts/airplay-reset.sh`.

This supervisor talks RTSP `OPTIONS *` to localhost:7000 on a
cadence. After a confidence threshold of consecutive failures, gated
on "no active session", it restarts shairport-sync + nqptp — the same
units the manual fix already touches. The detection mechanism is
symmetric with the manual path; no new failure modes introduced.

Disable knob: set `JASPER_SHAIRPORT_SUPERVISOR=disabled` in
`/etc/jasper/jasper.env` and restart `jasper-control`.

Design rationale: docs/HANDOFF-resilience.md (Tier 3).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import threading
import time
from typing import Any

from . import mpris

logger = logging.getLogger(__name__)


# Minimal RFC 2326 OPTIONS. shairport-sync's handle_options_2 replies
# `RTSP/1.0 200 OK` pre-pair and independent of any in-flight
# `principal_conn`, so the probe doesn't disturb an active session.
_OPTIONS_REQUEST = (
    b"OPTIONS * RTSP/1.0\r\n"
    b"CSeq: 0\r\n"
    b"User-Agent: jasper-control/probe\r\n"
    b"\r\n"
)


class ShairportSupervisor:
    """Probes shairport-sync's RTSP surface; restarts on confident wedge.

    Override `probe`, `is_session_active`, or `restart_shairport` to
    inject test doubles. The policy in `_tick` is the unit under test.
    """

    DEFAULT_INTERVAL_SEC = 30.0
    DEFAULT_JITTER_SEC = 3.0
    DEFAULT_PROBE_TIMEOUT_SEC = 3.0
    DEFAULT_FAILURE_THRESHOLD = 3
    DEFAULT_RATE_LIMIT_SEC = 600.0
    DEFAULT_COLD_START_SEC = 60.0

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 7000,
        interval_sec: float = DEFAULT_INTERVAL_SEC,
        jitter_sec: float = DEFAULT_JITTER_SEC,
        probe_timeout_sec: float = DEFAULT_PROBE_TIMEOUT_SEC,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        rate_limit_sec: float = DEFAULT_RATE_LIMIT_SEC,
        cold_start_sec: float = DEFAULT_COLD_START_SEC,
    ) -> None:
        self._host = host
        self._port = port
        self._interval = interval_sec
        self._jitter = jitter_sec
        self._probe_timeout = probe_timeout_sec
        self._threshold = failure_threshold
        self._rate_limit = rate_limit_sec
        self._cold_start = cold_start_sec
        # Observable state — read by snapshot() for /state. Both
        # *_at fields are wall-clock (time.time()) so the dashboard
        # can render them as timestamps.
        self.consecutive_failures: int = 0
        self.last_probe_at: float | None = None
        self.last_probe_ok: bool | None = None
        self.last_restart_at: float | None = None
        self.restart_count: int = 0
        self.suppressed_count: int = 0
        # True while the probe is intentionally idle because this
        # speaker is a bonded follower with shairport parked.
        self.parked_by_role: bool = False
        # Monotonic clock for rate-limit math — separated from
        # last_restart_at so display and arithmetic don't share
        # a time base. time.monotonic() can't go backwards on NTP
        # jumps; time.time() can.
        self._last_restart_monotonic: float | None = None

    # ---- main loop ----

    async def run(self) -> None:
        logger.info(
            "event=shairport.start "
            "interval=%.0fs threshold=%d rate_limit=%.0fs",
            self._interval, self._threshold, self._rate_limit,
        )
        await asyncio.sleep(self._cold_start)
        while True:
            try:
                await self._tick()
            except Exception:  # noqa: BLE001
                logger.exception("event=shairport.tick_crash")
            await asyncio.sleep(self._interval + random.uniform(
                -self._jitter, self._jitter,
            ))

    async def _tick(self) -> None:
        if self.shairport_parked_by_role():
            # A bonded FOLLOWER deliberately parks shairport-sync (the
            # dumb-follower profile — its sources are structurally
            # unplayable while bonded). Probing it would WARN every tick
            # against intended state; the grouping reconciler owns the
            # unit, /rooms + the doctor own the user-facing story.
            self.consecutive_failures = 0
            self.last_probe_ok = None
            self.parked_by_role = True
            return
        self.parked_by_role = False
        ok = False
        try:
            ok = await self.probe()
        except Exception:  # noqa: BLE001
            logger.exception("event=shairport.probe_crash")
        self.last_probe_at = time.time()
        self.last_probe_ok = ok
        if ok:
            self.consecutive_failures = 0
            return
        self.consecutive_failures += 1
        logger.warning(
            "event=shairport.probe_fail consecutive=%d threshold=%d",
            self.consecutive_failures, self._threshold,
        )
        if self.consecutive_failures < self._threshold:
            return
        # Gate. Unexpected exception → fail-safe to "active" so we
        # never risk disrupting a real listener on an unknown state.
        try:
            active = await self.is_session_active()
        except Exception:  # noqa: BLE001
            logger.exception("event=shairport.gate_crash")
            active = True
        if active:
            self.suppressed_count += 1
            logger.warning("event=shairport.probe_suppressed reason=active")
            return
        # Rate-limit: at most one supervisor-driven restart per window.
        mono = self._now()
        if (
            self._last_restart_monotonic is not None
            and mono - self._last_restart_monotonic < self._rate_limit
        ):
            logger.warning(
                "event=shairport.probe_rate_limited "
                "since_last_restart=%.0fs limit=%.0fs",
                mono - self._last_restart_monotonic, self._rate_limit,
            )
            return
        self._last_restart_monotonic = mono
        self.last_restart_at = time.time()
        self.restart_count += 1
        self.consecutive_failures = 0
        logger.error(
            "event=shairport.wedge_detected action=restart count=%d",
            self.restart_count,
        )
        try:
            await self.restart_shairport()
        except Exception:  # noqa: BLE001
            logger.exception("event=shairport.restart_failed")

    # ---- overridable IO ----

    async def probe(self) -> bool:
        """Open localhost:port, send OPTIONS, expect RTSP/1.0 200."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=self._probe_timeout,
            )
        except (OSError, asyncio.TimeoutError):
            return False
        try:
            writer.write(_OPTIONS_REQUEST)
            await asyncio.wait_for(
                writer.drain(), timeout=self._probe_timeout,
            )
            data = await asyncio.wait_for(
                reader.read(256), timeout=self._probe_timeout,
            )
        except (OSError, asyncio.TimeoutError):
            return False
        finally:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except (OSError, asyncio.TimeoutError):
                pass
        # Trailing space pins to a real 3-digit 200 (RFC 2326 §6.1
        # mandates the SP after the status code) — keeps a future
        # 2001/2002 status from spuriously matching.
        return data.startswith(b"RTSP/1.0 200 ")

    async def is_session_active(self) -> bool:
        """True when MPRIS reports Playing.

        If the MPRIS probe is unknown, fail safe to "active" only
        while systemd still reports shairport-sync live or unknown. A
        dead/inactive unit cannot be protecting a listener, so it
        bypasses the gate and lets the restart path recover it.
        """
        playing = await mpris.shairport_playing(timeout=2.0)
        if playing is None:
            unit_active = await self.is_shairport_unit_active()
            if unit_active is False:
                logger.warning(
                    "event=shairport.gate_bypass reason=unit_inactive",
                )
                return False
            return True
        return playing

    def shairport_parked_by_role(self) -> bool:
        """True when this speaker is an ACTIVE bonded follower — the
        dumb-follower profile parks shairport-sync, so the wedge probe
        must idle. One tiny env read per tick via the shared predicate
        (jasper.multiroom.config.follower_leader_addr); overridable in
        tests. Fail-open to NOT-parked: a broken read must never
        silently disable the wedge supervisor on a solo speaker."""
        try:
            from ..multiroom.config import follower_leader_addr, load_config

            return follower_leader_addr(load_config()) is not None
        except Exception:  # noqa: BLE001 — fail-open, keep supervising
            return False

    async def is_shairport_unit_active(self) -> bool | None:
        """Return systemd's shairport unit liveness.

        ``False`` is load-bearing: a dead/failed shairport process must
        bypass the MPRIS "unknown means active" fail-safe, because the
        DBus probe returns unknown precisely when there is no live
        process to disrupt. Unknown systemctl failures still fail safe
        to None so the caller preserves the active-session guard.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "is-active", "shairport-sync.service",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return None
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=2.0,
            )
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            return None
        status = stdout.decode("utf-8", "replace").strip()
        if proc.returncode == 0 and status == "active":
            return True
        if status in {"inactive", "failed", "deactivating", "dead"}:
            return False
        if proc.returncode != 0 and status:
            return False
        return None

    async def restart_shairport(self) -> None:
        """`reset-failed` clears StartLimitBurst parking; `--no-block
        restart` returns as soon as the job is enqueued so we don't sit
        for shairport's full stop timeout (default 90 s). The next
        probe (~30 s out) confirms the restart took."""
        reset = await asyncio.create_subprocess_exec(
            "systemctl", "reset-failed",
            "shairport-sync.service", "nqptp.service",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(reset.wait(), timeout=5.0)
        restart = await asyncio.create_subprocess_exec(
            "systemctl", "--no-block", "restart",
            "shairport-sync.service", "nqptp.service",
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
            "parked_by_role": self.parked_by_role,
            "last_probe_at": self.last_probe_at,
            "last_probe_ok": self.last_probe_ok,
            "consecutive_failures": self.consecutive_failures,
            "restart_count": self.restart_count,
            "last_restart_at": self.last_restart_at,
            "suppressed_count": self.suppressed_count,
        }


# Module-level supervisor instance, set by start_supervisor() when
# enabled. snapshot() reads it; the HTTP handler thread is read-only
# wrt the instance and Python attribute reads are atomic at the
# snapshot dict's resolution.
_supervisor: ShairportSupervisor | None = None
_supervisor_thread: threading.Thread | None = None


def snapshot() -> dict[str, Any]:
    """Read-only state for /state. Returns `{"enabled": False}` when
    the supervisor is disabled or not yet running."""
    if _supervisor is None:
        return {"enabled": False}
    return _supervisor.snapshot()


def start_supervisor() -> threading.Thread | None:
    """Start the supervisor in a background thread. No-op when
    `JASPER_SHAIRPORT_SUPERVISOR=disabled` (exact match, case-
    insensitive). Idempotent under sequential calls — the sole
    caller is `jasper-control`'s single-threaded `main()`."""
    global _supervisor, _supervisor_thread
    if _supervisor_thread is not None:
        return _supervisor_thread
    mode = os.environ.get("JASPER_SHAIRPORT_SUPERVISOR", "auto").lower()
    if mode == "disabled":
        logger.info("event=shairport.disabled")
        return None
    if mode != "auto":
        logger.warning(
            "JASPER_SHAIRPORT_SUPERVISOR=%r unrecognized; "
            "treating as 'auto'. Use 'disabled' to turn the supervisor off.",
            mode,
        )
    _supervisor = ShairportSupervisor()

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_supervisor.run())
        except Exception:  # noqa: BLE001
            logger.exception("event=shairport.thread_crash")
        finally:
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    _supervisor_thread = threading.Thread(
        target=_run, name="shairport-supervisor", daemon=True,
    )
    _supervisor_thread.start()
    return _supervisor_thread
