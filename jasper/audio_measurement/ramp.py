# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Settle-based level-match ramp controller (shared measurement kernel).

This is the P2 generalization of ``jasper.correction.autolevel`` per
``docs/HANDOFF-correction-revision-plan.md`` §3.1. The analog amplifier's gain
is unknown; JTS controls only the digital ``main_volume``. The controller drives
``main_volume`` up from a quiet start until a phone-reported mic level settles
inside the safe measurement window, then locks — never blasting up to find it.

Why *settle-based two-point mapping*, not cross-correlation (the design review's
supersession note, §3.1): the played level and the mic-reported level are related
by a single unknown constant gain ``G`` because the whole chain is LTI and
``main_volume`` adds in dB:

    mic_dbfs(v) = v + G          (G = amp + room + mic path gain, unknown)

So one *trusted, settled* reading fixes ``G`` (the line's slope is a known ``1``),
and the target volume that lands the mic in the window is exact:

    G          = settled_mic_dbfs - v_held
    target_v   = window_target_dbfs - G

An earlier draft reused ``capture_relay.alignment.cross_correlation_alignment`` to
recover the transport delay ``τ``; that estimator is waveform-domain and
structurally near-degenerate on a monotonic ramp envelope (a ramp correlated with
a ramp is a broad unimodal plateau; confidence reads ≈0 on perfect data). The
replacement never estimates ``τ`` at all — it *holds* longer than the modeled
loop latency so the transport delay has already elapsed before it reads the
settled level.

Safety is the whole point. Every commanded volume is provably ``<=`` the dynamic
cap (``original + bump`` clamped to ``[-20, -6] dBFS`` ``main_volume`` — the
operative ceiling, tighter than the 0 dB hard clamp in
``camilla.py::_coerce_main_volume_db``) **and** ``<= 0 dB``. The coarse staircase
stops at a *pre-window* threshold set below the window bottom by the worst-case
in-flight overshoot, so the staircase never climbs into the window; the only
approach into the window is a single computed jump after the settle. A
``clip=true`` sample is an immediate abort; a reading below
``noise_floor + trust_margin`` is discarded as ambient-dominated; a safety
timeout always fires; and the ramp fades down before the tone is killed.

The controller is **pure and synthetically testable**: inject a fake clock, a
fake volume setter, and a fake mic-sample source (:class:`LevelSample` batches).
No CamillaDSP, no ``aplay``, no relay.
"""
from __future__ import annotations

import logging
import os
import statistics
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from jasper.log_event import log_event

logger = logging.getLogger(__name__)

# Level-event schema version. Bump when :class:`LevelSample`'s shape changes so a
# stale phone payload is detectable rather than silently misread. Pinned by
# tests/test_audio_measurement_ramp.py.
LEVEL_EVENT_SCHEMA_VERSION = 1

# The digital-full-scale hard ceiling: main_volume must never exceed this,
# independent of the dynamic cap. Mirrors camilla.py::_coerce_main_volume_db,
# duplicated here as defense-in-depth so the kernel is safe even if a caller
# forgets to clamp. Do not raise.
HARD_CEILING_DBFS = 0.0


# --- env-knob helpers ---------------------------------------------------------
#
# Every threshold whose true value is hardware-gated is a deploy-time knob
# (H1 supplies the real numbers on-device — the defaults here are conservative
# placeholders, NOT empirically derived), mirroring the
# JASPER_CAPTURE_ALIGNMENT_THRESHOLD pattern in capture_relay/alignment.py. Set
# them in jasper.env once measured; no rebuild required. Out-of-range or
# unparseable values fall back to the documented default.


def _env_float(name: str, default: float, *, lo: float, hi: float) -> float:
    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return default
        if lo <= value <= hi:
            return value
    return default


def _env_int(name: str, default: int, *, lo: int, hi: int) -> int:
    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            return default
        if lo <= value <= hi:
            return value
    return default


class RampState(Enum):
    """Ramp sub-state, orthogonal to the measurement session state.

    The happy path is ``IDLE → CLIMBING → SETTLING → CONFIRMING → LOCKED``.
    ``CLIMBING`` is the quiet-start coarse staircase up to the pre-window; once a
    trusted reading crosses the pre-window the volume freezes and the transport
    delay is waited out in ``SETTLING``; a single computed jump lands the target;
    ``CONFIRMING`` requires ``confirm_k`` consecutive in-window samples before the
    lock is trusted.
    """

    IDLE = "idle"
    CLIMBING = "climbing"
    SETTLING = "settling"
    CONFIRMING = "confirming"
    LOCKED = "locked"
    MAXED_OUT = "maxed_out"
    ABORTED = "aborted"
    CANCELLED = "cancelled"
    ERROR = "error"


TERMINAL_STATES = frozenset(
    {
        RampState.LOCKED,
        RampState.MAXED_OUT,
        RampState.ABORTED,
        RampState.CANCELLED,
        RampState.ERROR,
    }
)


@dataclass(frozen=True)
class LevelSample:
    """One phone-reported mic-level sample.

    Batched, client-timestamped sample arrays (not singular events) ride the
    relay's last-write-wins ``event`` slot, so the Pi's ~0.75 s poll never
    decimates the series. ``rms_dbfs`` / ``peak_dbfs`` are computed on the phone
    the same way the Pi's ``quality._dbfs`` computes them. ``clip`` marks a
    full-scale sample (immediate abort). ``agc_frozen`` is the phone's realized
    ``autoGainControl:false`` state — ``False`` means the browser ignored the
    request (iOS historically does) and the level is AGC-compressed, so it must
    not be trusted as a gain-map reference.
    """

    seq: int
    t_client_ms: int
    rms_dbfs: float
    peak_dbfs: float
    clip: bool = False
    agc_frozen: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LevelSample:
        """Parse one sample from an untrusted phone payload. Lenient on missing
        optional flags, strict on the numeric fields it needs."""
        return cls(
            seq=int(data.get("seq", 0)),
            t_client_ms=int(data.get("t_client_ms", 0)),
            rms_dbfs=float(data["rms_dbfs"]),
            peak_dbfs=float(data.get("peak_dbfs", data["rms_dbfs"])),
            clip=bool(data.get("clip", False)),
            agc_frozen=bool(data.get("agc_frozen", True)),
        )


@dataclass(frozen=True)
class MeasurementRamp:
    """The ramp's tuning knobs — one self-describing, validated config.

    Constructing an instance whose overshoot invariant would be violated raises
    ``ValueError``, so a config that *could* drive the mic past the window can
    never be built (the panel will probe this). All bounds are dBFS
    ``main_volume``; all durations seconds.

    Overshoot invariant (validated in ``__post_init__``):

        ramp_rate * max_loop_latency < 0.5 * window_width

    with ``ramp_rate = step_db / step_interval_s``. Because the coarse staircase
    stops at ``pre_window_db`` (set below ``window_low_dbfs`` by at least the
    worst-case in-flight overshoot), the staircase never climbs into the window;
    the sole approach into the window is a single computed jump after the settle.
    """

    # Target window (aim at the BOTTOM, not the centre — with loop latency the
    # played level runs ahead of the newest report; aiming centre would eat half
    # the window). §3.1 (d).
    window_low_dbfs: float = -20.0
    window_high_dbfs: float = -12.0

    # Trust floor: a reading is only trustable once it clears
    # noise_floor + trust_margin_db (§3.1 (a)). Below that the RMS is
    # ambient-dominated and the early ramp shape is meaningless.
    trust_margin_db: float = 10.0

    # Consecutive in-window trusted samples required before the level is treated
    # as trustworthy → lock (§3.1 settle-based mapping, k >= 3).
    confirm_k: int = 3

    # Coarse staircase.
    start_db: float = -50.0
    step_db: float = 1.0
    step_interval_s: float = 0.6  # 1.667 dB/s → invariant holds with margin

    # Hold >= the max loop latency after crossing the pre-window so the transport
    # delay has elapsed before the settled level is read (§3.1 settle-based
    # mapping). settle_hold_s should be >= max_loop_latency_s.
    settle_hold_s: float = 2.0
    max_loop_latency_s: float = 2.0

    # Safety timeout + graceful fade-before-tone-kill (preserved from
    # AutolevelController).
    safety_timeout_s: float = 25.0
    fade_down_to_db: float = -50.0
    fade_step_db: float = 2.0
    fade_step_s: float = 0.03

    # Dynamic cap: original + bump clamped to [floor, ceil]. This is the OPERATIVE
    # ceiling — tighter than HARD_CEILING_DBFS. Preserves AutolevelController's
    # semantics exactly (bump=6, floor=-20, ceil=-6).
    cap_bump_db: float = 6.0
    cap_floor_db: float = -20.0
    cap_ceil_db: float = -6.0

    # Derived pre-window: the coarse staircase stops here. Defaulted from the
    # window bottom minus the worst-case in-flight overshoot in __post_init__ when
    # left as None so the staircase provably never climbs into the window.
    pre_window_db: float | None = None

    def __post_init__(self) -> None:
        if self.window_high_dbfs <= self.window_low_dbfs:
            raise ValueError(
                "window_high_dbfs must be above window_low_dbfs, got "
                f"[{self.window_low_dbfs}, {self.window_high_dbfs}]"
            )
        if self.cap_ceil_db > HARD_CEILING_DBFS:
            raise ValueError(
                f"cap_ceil_db {self.cap_ceil_db} must be <= the hard ceiling "
                f"{HARD_CEILING_DBFS}"
            )
        if self.step_db <= 0 or self.step_interval_s <= 0:
            raise ValueError("step_db and step_interval_s must be positive")
        if self.max_loop_latency_s < 0:
            raise ValueError("max_loop_latency_s must be >= 0")
        if self.settle_hold_s < self.max_loop_latency_s:
            raise ValueError(
                "settle_hold_s must be >= max_loop_latency_s so the transport "
                "delay has elapsed before the settled level is read"
            )
        if self.confirm_k < 1:
            raise ValueError("confirm_k must be >= 1")
        window_width = self.window_high_dbfs - self.window_low_dbfs
        overshoot = self.ramp_rate * self.max_loop_latency_s
        if not overshoot < 0.5 * window_width:
            raise ValueError(
                f"overshoot guard violated: ramp_rate*max_loop_latency="
                f"{overshoot:.3f} dB must be < half the window width "
                f"{0.5 * window_width:.3f} dB (slow the ramp, shorten latency, "
                "or widen the window)"
            )
        # Fill the derived pre-window so the coarse staircase stops below the
        # window by at least the worst-case in-flight overshoot.
        ceiling = self.window_low_dbfs - overshoot
        pre_window = ceiling if self.pre_window_db is None else self.pre_window_db
        if pre_window > ceiling + 1e-9:
            raise ValueError(
                "pre_window_db must be <= window_low_dbfs - worst-case "
                "in-flight overshoot so the staircase cannot enter the window"
            )
        object.__setattr__(self, "pre_window_db", pre_window)

    @property
    def ramp_rate(self) -> float:
        """Coarse-staircase climb rate, dB/s."""
        return self.step_db / self.step_interval_s

    @property
    def pre_window(self) -> float:
        """The resolved pre-window threshold (never None after ``__post_init__``).

        The coarse staircase stops here; a typed accessor so callers don't carry
        the ``float | None`` of the raw field, which ``__post_init__`` always
        fills.
        """
        assert self.pre_window_db is not None  # set in __post_init__
        return self.pre_window_db

    def dynamic_cap(self, original_db: float) -> float:
        """The operative end-of-ramp cap: original + bump, clamped to bounds.

        Identical to ``autolevel.compute_autolevel_cap`` — preserved so callers
        that relied on that semantics get the same ceiling.
        """
        return max(
            self.cap_floor_db, min(original_db + self.cap_bump_db, self.cap_ceil_db)
        )

    @classmethod
    def from_env(cls, **overrides: Any) -> MeasurementRamp:
        """Build a config with hardware-gated knobs read from the environment.

        Explicit ``overrides`` win over env (tests pass exact values); anything
        not overridden and not in the env falls back to the documented default.
        Mirrors ``alignment._env_threshold``: out-of-range / unparseable env
        values are ignored, never raised.
        """
        env_values: dict[str, Any] = {
            "window_low_dbfs": _env_float(
                "JASPER_RAMP_WINDOW_LOW_DBFS", cls.window_low_dbfs, lo=-60.0, hi=0.0
            ),
            "window_high_dbfs": _env_float(
                "JASPER_RAMP_WINDOW_HIGH_DBFS", cls.window_high_dbfs, lo=-60.0, hi=0.0
            ),
            "trust_margin_db": _env_float(
                "JASPER_RAMP_TRUST_MARGIN_DB", cls.trust_margin_db, lo=0.0, hi=40.0
            ),
            "settle_hold_s": _env_float(
                "JASPER_RAMP_SETTLE_HOLD_S", cls.settle_hold_s, lo=0.0, hi=30.0
            ),
            "max_loop_latency_s": _env_float(
                "JASPER_RAMP_MAX_LOOP_LATENCY_S",
                cls.max_loop_latency_s,
                lo=0.0,
                hi=30.0,
            ),
            "confirm_k": _env_int(
                "JASPER_RAMP_CONFIRM_K", cls.confirm_k, lo=1, hi=20
            ),
        }
        env_values.update(overrides)
        return cls(**env_values)


@dataclass
class RampData:
    """Live state of one ramp run. Replaced when a new run starts."""

    state: RampState = RampState.IDLE
    current_main_volume_db: float = -50.0
    original_main_volume_db: float | None = None
    locked_main_volume_db: float | None = None
    cap_db: float | None = None
    # The recovered chain gain G = settled_mic_dbfs - v_held (dB). Persisted into
    # the geometry lock so the drift check has the mapping. None until a settle.
    gain_map_db: float | None = None
    settled_mic_dbfs: float | None = None
    noise_floor_dbfs: float | None = None
    agc_frozen: bool = True
    error: str | None = None
    # Idempotency guard for terminal-state listening-level restore.
    restored: bool = False

    def snapshot(self) -> dict[str, Any]:
        def r(x: float | None) -> float | None:
            return round(x, 2) if x is not None else None

        return {
            "state": self.state.value,
            "current_main_volume_db": r(self.current_main_volume_db),
            "original_main_volume_db": r(self.original_main_volume_db),
            "locked_main_volume_db": r(self.locked_main_volume_db),
            "cap_db": r(self.cap_db),
            "gain_map_db": r(self.gain_map_db),
            "settled_mic_dbfs": r(self.settled_mic_dbfs),
            "noise_floor_dbfs": r(self.noise_floor_dbfs),
            "agc_frozen": self.agc_frozen,
            "error": self.error,
        }


# A source of the next batch of phone-reported samples. Injected so the loop is
# testable with a synthetic feed; the real feed polls the relay. Returning an
# empty list means "no new samples this tick" (the loop keeps its clock running).
SampleSource = Callable[[], Awaitable[list[LevelSample]]]

# Injected monotonic clock (seconds) + async sleep, so tests drive time directly.
Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]

VolumeSetter = Callable[[float], Awaitable[Any]]
VolumeGetter = Callable[[], Awaitable[float]]


class RampController:
    """Owns the settle-based ramp loop, its state, and volume restoration.

    Public surface mirrors ``AutolevelController`` (``run`` / ``lock`` /
    ``cancel`` / ``restore_listening_volume_if_ramped``) so the correction
    session's adapter can swap the engine without changing its callers. The
    engine is what changed: blind browser-locked ramp → Pi-side settle-based
    two-point map with a stop-ahead window and clip/trust/timeout safety.
    """

    def __init__(
        self,
        *,
        session_id: str,
        config: MeasurementRamp | None = None,
    ) -> None:
        self.session_id = session_id
        self.config = config or MeasurementRamp()
        self.data = RampData(current_main_volume_db=self.config.start_db)
        self._lock_requested = False
        self._cancel_requested = False
        self._main_volume_setter: VolumeSetter | None = None

    # -- listening-level restore (idempotent, best-effort) --

    @property
    def main_volume_setter(self) -> VolumeSetter | None:
        return self._main_volume_setter

    @main_volume_setter.setter
    def main_volume_setter(self, setter: VolumeSetter | None) -> None:
        self._main_volume_setter = setter

    async def restore_listening_volume_if_ramped(self) -> None:
        """Restore main_volume when a measurement ends outside apply/reset.

        The ramp leaves main_volume at the measurement level (LOCKED / MAXED_OUT)
        for the whole measurement; failed / verify-ended measurements skip the web
        apply/reset handlers, so this best-effort hook restores the user's level
        there. Idempotent; swallows errors. Mirrors
        ``AutolevelController.restore_listening_volume_if_ramped``.
        """
        d = self.data
        if d.restored:
            return
        if d.state not in (RampState.LOCKED, RampState.MAXED_OUT):
            return
        if d.original_main_volume_db is None or self._main_volume_setter is None:
            return
        d.restored = True
        try:
            await self._main_volume_setter(d.original_main_volume_db)
            log_event(
                logger,
                "ramp_volume_restored",
                session=self.session_id,
                to_db=f"{d.original_main_volume_db:.1f}",
                trigger="measurement_ended",
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "ramp volume restore on measurement end failed (session=%s) — "
                "speaker may remain at the measurement level until /reset",
                self.session_id,
            )

    async def lock(self) -> bool:
        """Signal the running ramp to lock at the current main_volume (the user
        tapped a manual Lock)."""
        if self.data.state in TERMINAL_STATES:
            return False
        self._lock_requested = True
        return True

    async def cancel(self) -> bool:
        """Signal the running ramp to abort and restore the original volume."""
        if self.data.state in TERMINAL_STATES:
            return False
        self._cancel_requested = True
        return True

    # -- the ramp loop --

    def _safe_target(self, desired_db: float) -> float:
        """Clamp a desired volume to the operative cap AND the hard ceiling.

        The single choke point every commanded volume passes through, so the
        cap-safety invariant is one line, not scattered. Never returns a value
        above ``min(dynamic_cap, HARD_CEILING_DBFS)``.
        """
        cap = self.data.cap_db
        ceil = HARD_CEILING_DBFS if cap is None else min(cap, HARD_CEILING_DBFS)
        return min(desired_db, ceil)

    async def run(
        self,
        *,
        get_main_volume_db: VolumeGetter,
        set_main_volume_db: VolumeSetter,
        play_continuous_tone: Callable[[], Awaitable[Any]],
        cancel_tone: Callable[[], None],
        next_samples: SampleSource,
        noise_floor_dbfs: float | None = None,
        clock: Clock,
        sleep: Sleep,
    ) -> RampData:
        """Run the settle-based level-match ramp. Returns the terminal RampData.

        Injected dependencies keep the loop pure/testable:
          - ``get/set_main_volume_db``: read the pre-ramp level, drive the ramp.
          - ``play_continuous_tone`` / ``cancel_tone``: the band-limited tone the
            phone measures (started AFTER the quiet start volume is set, killed
            AFTER the fade-down — audio-safety order).
          - ``next_samples``: awaited each tick for the phone's newest batch.
          - ``noise_floor_dbfs``: the phone's pre-sweep noise floor (trust gate).
          - ``clock`` / ``sleep``: injected monotonic time so tests drive cadence.
        """
        cfg = self.config
        self.data = d = RampData(
            current_main_volume_db=cfg.start_db,
            noise_floor_dbfs=noise_floor_dbfs,
        )
        self._main_volume_setter = set_main_volume_db
        self._lock_requested = False
        self._cancel_requested = False
        tone_started = False

        async def _set(db: float) -> None:
            safe = self._safe_target(db)
            await set_main_volume_db(safe)
            d.current_main_volume_db = safe

        async def _graceful_stop(final_db: float | None) -> None:
            """Fade down before killing the tone, then set the final volume.

            Preserves AutolevelController's click-free stop: never kill the tone
            at a loud level. ``final_db`` is passed through ``_safe_target`` so
            even the terminal set can't exceed the caps.
            """
            try:
                cur = d.current_main_volume_db
                while cur > cfg.fade_down_to_db:
                    cur = max(cfg.fade_down_to_db, cur - cfg.fade_step_db)
                    try:
                        await set_main_volume_db(cur)
                        d.current_main_volume_db = cur
                    except Exception:  # noqa: BLE001
                        break
                    await sleep(cfg.fade_step_s)
            finally:
                if tone_started:
                    cancel_tone()
                if final_db is not None:
                    try:
                        await _set(final_db)
                    except Exception:  # noqa: BLE001
                        pass

        try:
            d.original_main_volume_db = float(await get_main_volume_db())
            d.cap_db = cfg.dynamic_cap(d.original_main_volume_db)
            d.state = RampState.CLIMBING
            log_event(
                logger,
                "ramp_start",
                session=self.session_id,
                original_db=f"{d.original_main_volume_db:.1f}",
                cap_db=f"{d.cap_db:.1f}",
                window=f"[{cfg.window_low_dbfs:.0f},{cfg.window_high_dbfs:.0f}]",
                rate_db_s=f"{cfg.ramp_rate:.2f}",
            )

            # Audio-safety order: quiet start BEFORE the tone.
            await _set(cfg.start_db)
            await sleep(0.1)
            await play_continuous_tone()
            tone_started = True

            start_time = clock()
            last_step_time = clock()
            settle_start: float | None = None
            confirm_count = 0
            jumped = False

            while True:
                # --- terminal signals checked every tick ---
                if self._cancel_requested:
                    d.state = RampState.CANCELLED
                    log_event(
                        logger,
                        "ramp_cancelled",
                        session=self.session_id,
                        at_db=f"{d.current_main_volume_db:.1f}",
                    )
                    await _graceful_stop(d.original_main_volume_db)
                    return d
                if self._lock_requested:
                    # Manual lock: trust the user, lock where we are.
                    d.state = RampState.LOCKED
                    d.locked_main_volume_db = d.current_main_volume_db
                    log_event(
                        logger,
                        "ramp_locked",
                        session=self.session_id,
                        at_db=f"{d.current_main_volume_db:.1f}",
                        trigger="manual",
                    )
                    await _graceful_stop(d.current_main_volume_db)
                    return d
                if clock() - start_time > cfg.safety_timeout_s:
                    d.state = RampState.CANCELLED
                    d.error = f"safety timeout after {cfg.safety_timeout_s}s"
                    log_event(
                        logger,
                        "ramp_safety_timeout",
                        level=logging.WARNING,
                        session=self.session_id,
                        at_db=f"{d.current_main_volume_db:.1f}",
                    )
                    await _graceful_stop(d.original_main_volume_db)
                    return d

                batch = await next_samples()
                trusted = self._process_batch(d, batch)
                if d.state == RampState.ABORTED:
                    # A clip in the batch aborted immediately.
                    await _graceful_stop(d.original_main_volume_db)
                    return d

                now = clock()

                if d.state == RampState.CLIMBING:
                    # Freeze + settle the moment a trusted reading crosses the
                    # pre-window (the coarse staircase never climbs into the
                    # window itself).
                    if trusted and trusted[-1].rms_dbfs >= cfg.pre_window:
                        d.state = RampState.SETTLING
                        settle_start = now
                        log_event(
                            logger,
                            "ramp_pre_window",
                            session=self.session_id,
                            at_db=f"{d.current_main_volume_db:.1f}",
                            mic_dbfs=f"{trusted[-1].rms_dbfs:.1f}",
                        )
                    elif now - last_step_time >= cfg.step_interval_s:
                        last_step_time = now
                        # Reached the cap without ever crossing the pre-window →
                        # the amp is too quiet.
                        if d.current_main_volume_db >= self._safe_target(1e9) - 1e-9:
                            d.state = RampState.MAXED_OUT
                            d.locked_main_volume_db = d.current_main_volume_db
                            log_event(
                                logger,
                                "ramp_maxed_out",
                                level=logging.WARNING,
                                session=self.session_id,
                                at_db=f"{d.current_main_volume_db:.1f}",
                                reason="cap_reached_below_pre_window",
                            )
                            await _graceful_stop(d.current_main_volume_db)
                            return d
                        await _set(d.current_main_volume_db + cfg.step_db)

                elif d.state == RampState.SETTLING:
                    assert settle_start is not None
                    if now - settle_start < cfg.settle_hold_s:
                        pass  # keep waiting out the transport delay
                    else:
                        settled = self._settled_level(d, trusted)
                        if settled is None:
                            # No trusted samples during the hold — extend by
                            # dropping back to CLIMBING to gather more.
                            d.state = RampState.CLIMBING
                            last_step_time = now
                            continue
                        d.settled_mic_dbfs = settled
                        d.gain_map_db = settled - d.current_main_volume_db
                        if cfg.window_low_dbfs <= settled <= cfg.window_high_dbfs:
                            d.state = RampState.CONFIRMING
                            confirm_count = 0
                        elif not jumped:
                            # One computed jump so the mic lands at the window
                            # BOTTOM. The gain map's slope is 1, so this target is
                            # exact; the jump can be up (amp quiet) or DOWN (amp
                            # loud — the mic is already above the window even here).
                            # Going down is always cap-safe; going up is clamped by
                            # `_set`. If the up-target clamps to the cap and the mic
                            # is still below the window, the CONFIRMING branch ends
                            # it as MAXED_OUT.
                            target = cfg.window_low_dbfs - d.gain_map_db
                            safe = self._safe_target(target)
                            log_event(
                                logger,
                                "ramp_settle_jump",
                                session=self.session_id,
                                settled_mic_dbfs=f"{settled:.1f}",
                                gain_map_db=f"{d.gain_map_db:.1f}",
                                target_db=f"{target:.1f}",
                                applied_db=f"{safe:.1f}",
                            )
                            if abs(safe - d.current_main_volume_db) <= 1e-9:
                                # Nowhere to move (already clamped there) but still
                                # out of window → confirm; a persistent below/above
                                # window resolves in CONFIRMING.
                                d.state = RampState.CONFIRMING
                                confirm_count = 0
                            else:
                                await _set(target)
                                settle_start = now  # re-hold after the jump
                                jumped = True
                        else:
                            # Jumped once, still out of window after a full settle:
                            # clamped at the cap (too quiet) or the map moved.
                            # CONFIRMING resolves it (below-cap window → lock;
                            # pinned-at-cap below window → MAXED_OUT).
                            d.state = RampState.CONFIRMING
                            confirm_count = 0

                elif d.state == RampState.CONFIRMING:
                    for s in trusted:
                        if cfg.window_low_dbfs <= s.rms_dbfs <= cfg.window_high_dbfs:
                            confirm_count += 1
                        else:
                            confirm_count = 0
                    if confirm_count >= cfg.confirm_k:
                        d.state = RampState.LOCKED
                        d.locked_main_volume_db = d.current_main_volume_db
                        log_event(
                            logger,
                            "ramp_locked",
                            session=self.session_id,
                            at_db=f"{d.current_main_volume_db:.1f}",
                            settled_mic_dbfs=(
                                f"{d.settled_mic_dbfs:.1f}"
                                if d.settled_mic_dbfs is not None
                                else ""
                            ),
                            trigger="window",
                        )
                        await _graceful_stop(d.current_main_volume_db)
                        return d
                    # If we are pinned at the cap and the last trusted reading is
                    # below the window, we cannot reach it without exceeding the
                    # cap → maxed out.
                    at_cap = (
                        d.current_main_volume_db >= self._safe_target(1e9) - 1e-9
                    )
                    if (
                        at_cap
                        and trusted
                        and trusted[-1].rms_dbfs < cfg.window_low_dbfs
                    ):
                        d.state = RampState.MAXED_OUT
                        d.locked_main_volume_db = d.current_main_volume_db
                        log_event(
                            logger,
                            "ramp_maxed_out",
                            level=logging.WARNING,
                            session=self.session_id,
                            at_db=f"{d.current_main_volume_db:.1f}",
                            reason="cap_reached_below_window",
                        )
                        await _graceful_stop(d.current_main_volume_db)
                        return d

                await sleep(0.01)
        except Exception as e:  # noqa: BLE001
            d.state = RampState.ERROR
            d.error = str(e)
            logger.exception("ramp failed (session=%s)", self.session_id)
            try:
                if d.original_main_volume_db is not None:
                    await _graceful_stop(d.original_main_volume_db)
                elif tone_started:
                    cancel_tone()
            except Exception:  # noqa: BLE001
                pass
            return d

    def _process_batch(
        self, d: RampData, batch: list[LevelSample]
    ) -> list[LevelSample]:
        """Fold a phone batch into ramp state; return the *trusted* samples.

        A ``clip=true`` sample flips the state to ABORTED immediately (the caller
        stops). A sample below ``noise_floor + trust_margin`` is dropped as
        ambient-dominated. ``agc_frozen=false`` on any sample is recorded so the
        adapter can degrade + disable drift (§3.1 (c)) — the ramp still runs, but
        the level must not become a trusted gain-map reference, so an
        AGC-compressed sample is treated as untrusted here.
        """
        cfg = self.config
        trusted: list[LevelSample] = []
        floor = d.noise_floor_dbfs
        for s in batch:
            if s.clip:
                d.state = RampState.ABORTED
                d.error = "clip detected"
                log_event(
                    logger,
                    "ramp_clip_abort",
                    level=logging.WARNING,
                    session=self.session_id,
                    at_db=f"{d.current_main_volume_db:.1f}",
                    peak_dbfs=f"{s.peak_dbfs:.1f}",
                )
                return trusted
            if not s.agc_frozen:
                d.agc_frozen = False
                # AGC-compressed: usable as a liveness signal but never as a
                # trusted level. Skip it from the trusted set.
                continue
            if floor is not None and s.rms_dbfs < floor + cfg.trust_margin_db:
                continue  # ambient-dominated; not trustable
            trusted.append(s)
        return trusted

    def _settled_level(
        self, d: RampData, trusted: list[LevelSample]
    ) -> float | None:
        """The settled mic level = median of the trusted samples in the tail.

        Median (not mean) so one noisy sample can't skew the gain map. Returns
        None when no trusted samples arrived during the hold.
        """
        if not trusted:
            return None
        return float(statistics.median(s.rms_dbfs for s in trusted))
