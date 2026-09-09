# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Heal supervisor — the two silences every unit state calls healthy: a
speaker emitting nothing with its audio path up, and a reachable voice daemon
that has heard no wake word in a day. Facts come from jasper-control's own
memory; the answer is an action the /system dashboard already offers. It
observes only — nothing here reaches an actuator. See ADR-0271.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from jasper.install_profile import (
    install_profile_supports_wake_detection,
    read_install_profile,
)
from jasper.log_event import log_event
from jasper.service_units import read_unit_states, unit_not_running

from ..measurement_window import DEFAULT_VOICE_SOCKET_PATH
from ..platform.uds import voice_socket_command
from .supervisor_runtime import (
    run_supervisor_loop,
    snapshot_or_disabled,
    spawn_on_control_loop,
)

logger = logging.getLogger(__name__)

CASE_SILENT = "silent"
CASE_DEAF = "deaf"

#: The dashboard's own actions (`handlers/system._post_system_action`,
#: `/system/restart/audio` and `/system/restart/voice`); heal names the action
#: and that handler owns which units each one touches. NOTHING CALLS THEM: heal
#: publishes and logs the action it would take, and ADR-0271 records what a
#: fortnight of those lines has to show before one is wired to the broker.
ACTION_RESTART_AUDIO = "restart-audio"
ACTION_RESTART_VOICE = "restart-voice"

#: `audio_health` signal-path codes at status "issue" that restart-audio can
#: actually answer: the household detail names RESTART_REMEDY, and the fault is
#: not `_UNDECLARED_OUTPUT_CODES` (`output_absent`/`output_backend_inactive`,
#: which mean outputd is not delivering at all — restart-audio does not carry
#: jasper-outputd).
SILENT_CODES = frozenset({
    "output_deaf",
    "output_ring_stalled",
    "output_stalled",
    "path_stalled",
})

TICK_INTERVAL_SEC = 600.0  # seconds
TICK_JITTER_SEC = 30.0  # seconds
COLD_START_SEC = 300.0  # seconds; longer than any boot-time audio settle
#: At most one `heal.would_act` line per case per window.
WOULD_ACT_WINDOW_SEC = 1800.0  # seconds
#: Mirrors WAKE_RECENCY_STALE_SEC in jasper/cli/doctor/wake.py. Converging them
#: would make one side import the other's package for one integer: control must
#: not drag jasper.cli.doctor into a long-lived daemon, and the doctor must not
#: drag a control supervisor into every run.
WAKE_STALE_SEC = 24 * 60 * 60  # seconds


@dataclass(frozen=True)
class Verdict:
    #: `fact` is the episode's identity: the same fact re-observed is one
    #: episode, so it is logged once.
    case: str
    reason: str
    action: str
    fact: str


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def decide(
    *,
    code: str,
    warmup: bool,
    guards_ok: bool,
    voice: dict[str, Any] | None,
    profile_expects_wake: bool,
    now: float,
) -> Verdict | None:
    """The whole fact→case map. None means nothing here belongs to heal."""
    if not guards_ok:
        return None
    if code in SILENT_CODES and not warmup:
        return Verdict(CASE_SILENT, code, ACTION_RESTART_AUDIO, code)
    last_wake = _seconds(_mapping(voice).get("last_wake_at"))
    if (
        profile_expects_wake
        and voice is not None
        # The daemon's own live answer to "this box arms zero wake legs by
        # design". Its two inputs include JASPER_LOCAL_MIC_PRESENT, which
        # jasper-aec-reconcile rewrites without restarting jasper-control, so
        # this process's os.environ is not a usable source for it.
        and not voice.get("push_to_talk_only")
        and not voice.get("mic_muted")
        and last_wake is not None
        and now - last_wake > WAKE_STALE_SEC
    ):
        return Verdict(
            CASE_DEAF, "wake_stale", ACTION_RESTART_VOICE, f"{last_wake:.0f}",
        )
    return None


def guards_active() -> bool:
    """Every shared-path unit present and active, in one `systemctl show`.
    Blocking: call it off the control loop."""
    # lazy: audio_health owns the roster but costs ~3k lines of imports the
    # doctor's own `--core` run (which reads TICK_INTERVAL_SEC from here) must
    # not pay.
    from .audio_health import RESTART_WATCH_UNITS

    units = tuple(RESTART_WATCH_UNITS)
    records = read_unit_states(units)
    if records is None:
        return False
    return all(unit_not_running(records.get(u)) is None for u in units)


def profile_expects_wake() -> bool:
    """Whether this tier can run always-on wake inference at all. Blocking:
    reads the install-profile marker."""
    try:
        return install_profile_supports_wake_detection(read_install_profile())
    except (TypeError, ValueError, OSError):
        return False


class HealSupervisor:
    """Observes the two cases every 10 minutes and logs what it would do.

    Override `audio_health` or `voice_status` to inject test doubles; the
    policy in `_tick` is the unit under test."""

    def __init__(
        self,
        *,
        audio_health_sampler: Any = None,
        voice_socket_path: str = DEFAULT_VOICE_SOCKET_PATH,
        interval_sec: float = TICK_INTERVAL_SEC,
        jitter_sec: float = TICK_JITTER_SEC,
        cold_start_sec: float = COLD_START_SEC,
    ) -> None:
        self._sampler = audio_health_sampler
        self._voice_socket_path = voice_socket_path
        self._interval = interval_sec
        self._jitter = jitter_sec
        self._cold_start = cold_start_sec
        self.last_tick: float | None = None
        self._would_act: dict[str, Any] | None = None
        self._episode: tuple[str, str] | None = None
        self._episode_at = 0.0
        self._logged_at: dict[str, float] = {}
        self._observed: tuple[str, str, bool] | None = None

    async def run(self) -> None:
        await run_supervisor_loop(
            tick=self._tick,
            cold_start_sec=self._cold_start,
            interval_sec=self._interval,
            jitter_sec=self._jitter,
            logger=logger,
            start_event="heal.start",
            tick_crash_event="heal.tick_crash",
            start_fields={"interval": f"{self._interval:.0f}s"},
        )

    async def _tick(self) -> None:
        now = time.time()
        signal = _mapping(self.audio_health().get("signal_path"))
        code = str(signal.get("code") or "") if signal.get("status") == "issue" else ""
        warmup = self.warmup_active()
        guards_ok = await asyncio.to_thread(guards_active)
        voice = await self.voice_status()
        verdict = decide(
            code=code, warmup=warmup, guards_ok=guards_ok, voice=voice,
            profile_expects_wake=await asyncio.to_thread(profile_expects_wake),
            now=now,
        )
        self._observe(code, guards_ok, warmup, voice, verdict, now)
        self._record(verdict, now)
        # Last, so a tick that fails every pass leaves `heal recency` stale
        # rather than green beside a repeating `heal.tick_crash`.
        self.last_tick = now

    def _observe(
        self,
        code: str,
        guards_ok: bool,
        warmup: bool,
        voice: dict[str, Any] | None,
        verdict: Verdict | None,
        now: float,
    ) -> None:
        """One line per change of posture, not one per tick."""
        posture = (code, verdict.case if verdict else "", guards_ok)
        if posture == self._observed:
            return
        self._observed = posture
        last_wake = _seconds(_mapping(voice).get("last_wake_at"))
        log_event(
            logger, "heal.observed",
            code=code or "-",
            warmup=warmup,
            guards_ok=guards_ok,
            wake_age="-" if last_wake is None else f"{now - last_wake:.0f}s",
            mic_muted=bool(_mapping(voice).get("mic_muted")),
            voice_reachable=voice is not None,
            case=verdict.case if verdict else "-",
        )

    def _record(self, verdict: Verdict | None, now: float) -> None:
        """Publish the live verdict, and log it once per episode per window."""
        if verdict is None:
            self._episode = None
            self._would_act = None
            return
        fresh = self._episode != (verdict.case, verdict.fact)
        if fresh:
            self._episode = (verdict.case, verdict.fact)
            self._episode_at = now
        self._would_act = {
            "case": verdict.case, "reason": verdict.reason,
            "action": verdict.action, "ts": self._episode_at,
        }
        last_logged = self._logged_at.get(verdict.case)
        if not fresh or (
            last_logged is not None and now - last_logged < WOULD_ACT_WINDOW_SEC
        ):
            return
        self._logged_at[verdict.case] = now
        log_event(
            logger, "heal.would_act",
            case=verdict.case, reason=verdict.reason, action=verdict.action,
        )

    # ---- overridable IO ----

    def audio_health(self) -> dict[str, Any]:
        """The resident sampler's current snapshot; `{}` when it has none."""
        return _mapping(None if self._sampler is None else self._sampler.snapshot())

    def warmup_active(self) -> bool:
        sampler = _mapping(_mapping(self.audio_health().get("technical")).get("sampler"))
        return bool(sampler.get("warmup_active"))

    async def voice_status(self) -> dict[str, Any] | None:
        """jasper-voice's STATUS over its control socket; None when down."""
        try:
            status = await voice_socket_command(
                self._voice_socket_path, "STATUS", timeout=2.0,
            )
        except (OSError, RuntimeError, ValueError):
            return None
        return status if isinstance(status, dict) else None

    # ---- accessors ----

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "last_tick": self.last_tick,
            "would_act": self._would_act,
        }


_supervisor: HealSupervisor | None = None


def snapshot() -> dict[str, Any]:
    """Read-only state for /state. `{"enabled": False}` before it starts."""
    return snapshot_or_disabled(
        None if _supervisor is None else _supervisor.snapshot,
    )


def start_supervisor(
    audio_health_sampler: Any = None,
    *,
    voice_socket_path: str = DEFAULT_VOICE_SOCKET_PATH,
) -> None:
    """Start the supervisor on jasper-control's shared background loop. No env
    knob: observation costs one `systemctl show` and one socket read per 10
    minutes. Idempotent under sequential calls."""
    global _supervisor
    if _supervisor is not None:
        return
    _supervisor = HealSupervisor(
        audio_health_sampler=audio_health_sampler,
        voice_socket_path=voice_socket_path,
    )
    spawn_on_control_loop(
        target=_supervisor.run,
        name="heal-supervisor",
        logger=logger,
        crash_event="heal.thread_crash",
    )
