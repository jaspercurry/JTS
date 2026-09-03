# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Settle-based level-match ramp controller (shared measurement kernel).

The analog amplifier's gain is unknown; JTS controls only the digital
``main_volume``. The whole chain is LTI and ``main_volume`` adds in dB, so

    mic_dbfs(v) = v + G          (G = amp + room + mic path gain, unknown)

and one trusted, settled reading fixes ``G`` (the line's slope is a known
``1``). The controller never estimates the transport delay: it waits out the
modeled worst-case loop latency after every volume change and reads only
samples that postdate it. Every volume change stamps
``blank_until = now + max_loop_latency_s``; samples arriving before that
reflect a stale level and are excluded from settle/confirm decisions (clip
detection is NEVER blanked). A momentarily-empty tick EXTENDS the settle hold;
the machine never bounces back to CLIMBING.

Safety is the whole point. Every ramp-commanded volume passes the
``_safe_target`` choke point: ``<=`` the dynamic cap (the lower of
``original + bump`` and the absolute ceiling) AND ``<= 0 dB``, with a
non-finite target raising instead of propagating. The coarse staircase stops at
a pre-window set below the window bottom by the worst-case in-flight overshoot
(``step_db + ramp_rate × max_loop_latency_s``). A ``clip=true`` sample aborts
immediately; readings that are non-finite, AGC-compressed, or below
``noise_floor + trust_margin`` are never trusted; a feed that goes silent
aborts. At the cap the kernel returns ``MAXED_OUT`` and restores, the sole
exception being an explicitly labeled ``bounded_low_level`` lock proven by a
fresh post-latency tail. Restoring the user's own pre-ramp volume is exempt
from the dynamic cap and honors only the 0 dB hard ceiling.

Tone contract: ``play_continuous_tone`` must play until ``cancel_tone()`` is
called (the ``correction.playback.TonePlayer.play`` shape); the kernel runs it
as a task and an early finish ends the ramp in ERROR -- a silent tone must
never blind-climb.
"""

from __future__ import annotations

import asyncio
import logging
import math
import statistics
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from jasper.env_load import bounded_env_float, bounded_env_int
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

# Level-event schema version. Bump when :class:`LevelSample`'s shape changes so
# a stale phone payload is detectable rather than silently misread. Mirrored by
# capture-page/js/level-events.js.
LEVEL_EVENT_SCHEMA_VERSION = 1

# The digital-full-scale hard ceiling: main_volume must never exceed this,
# independent of the dynamic cap. Mirrors camilla.py::_coerce_main_volume_db,
# duplicated here as defense-in-depth. Do not raise.
HARD_CEILING_DBFS = 0.0

# Fixed/listening-position measurements use the shared -12 dBFS stimulus at
# roughly one metre: up to 15 dB above the household entry volume, keeping the
# digital-full-scale ceiling and the live clipping abort.
LISTENING_POSITION_CAP_BUMP_DB = 15.0
LISTENING_POSITION_CAP_CEIL_DB = HARD_CEILING_DBFS

# Worst-case expected gap between consecutive phone samples reaching the kernel
# (≤2 Hz batches behind the relay's ~0.75 s poll). Budgets the derived safety
# timeout -- not a gate.
SAMPLE_BUDGET_S = 1.5


def capped_gap_step_db(
    *, measured_db: float, target_db: float, cap_db: float = math.inf
) -> float:
    """How far one measured level step moves the level: the remaining gap.

    The one climb policy in the tree. Every step re-measures, so the policy
    needs the chain to be only LOCALLY monotone in dB, never globally linear.
    ``cap_db`` saturates the step UPWARD only -- downward motion reduces risk,
    the same asymmetry :mod:`jasper.active_speaker.calibration_level` states for
    its ``upward_step_limit_db``. Returns the step in dB, to be ADDED to the
    current commanded level; the caller still clamps against its own ceiling.
    """
    return min(float(target_db) - float(measured_db), float(cap_db))

# The exception set the ramp treats as recoverable-by-restore. A broad-but-named
# tuple rather than a blind ``except Exception`` (lint contract: no new BLE001
# suppressions): it covers every realistic failure of the injected callables
# while letting CancelledError / SystemExit / MemoryError propagate.
RECOVERABLE_ERRORS = (
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    AttributeError,
    LookupError,
    ArithmeticError,
)


# --- env knobs ---------------------------------------------------------------
#
# Every threshold whose true value is hardware-gated is a deploy-time knob (the
# defaults here are conservative placeholders, NOT empirically derived).
# Out-of-range or unparseable values fall back to the documented default; a
# COMBINATION of individually-valid values that fails cross-field validation
# falls back as a whole (see :meth:`MeasurementRamp.from_env`), so a jasper.env
# edit can never brick the ramp at construction time.


class RampState(Enum):
    """Ramp sub-state, orthogonal to the measurement session state.

    The happy path is ``IDLE → CLIMBING → SETTLING → CONFIRMING → LOCKED``.
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


class RampLockKind(str, Enum):
    """Why a terminal ``LOCKED`` result is usable.

    ``BOUNDED_LOW_LEVEL`` records that the hard/dynamic gain bound was honored
    and the live mic evidence was trustworthy and stable, but the measured level
    still fell short of the preferred window -- never a claim that the normal
    target was reached.
    """

    IN_WINDOW = "in_window"
    BOUNDED_LOW_LEVEL = "bounded_low_level"
    MANUAL = "manual"


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

    Batched, client-timestamped sample arrays ride the relay's last-write-wins
    ``event`` slot, so the Pi's ~0.75 s poll never decimates the series.
    ``rms_dbfs`` / ``peak_dbfs`` are computed on the phone the same way the Pi's
    ``quality._dbfs`` computes them; ``clip`` marks a full-scale sample
    (immediate abort). ``agc_frozen`` is the phone's realized
    ``autoGainControl:false`` state, and ``False`` means the browser either
    reported AGC on or never reports the setting at all (every WebKit build).
    ``agc_unattested`` disambiguates those two: ``True`` means the browser could
    not attest either way and the sample is eligible for the empirical slope
    verification in :class:`RampController`; ``False`` means AGC was
    affirmatively reported on, so the level must never be a gain-map reference.
    An unattested chain is never encoded as bare ``agc_frozen=True``, so an older
    Pi falls back to "never trust" instead of trusting an unproven chain.
    """

    seq: int
    t_client_ms: int
    rms_dbfs: float
    peak_dbfs: float
    clip: bool = False
    agc_frozen: bool = True
    agc_unattested: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LevelSample:
        """Parse one sample from an untrusted phone payload.

        Strict on the numeric fields: a non-finite ``rms_dbfs`` / ``peak_dbfs``
        (JSON ``"NaN"`` / ``"Infinity"`` strings parse fine through ``float()``)
        raises ``ValueError``, so NaN can never reach the gain map.
        """
        rms = float(data["rms_dbfs"])
        peak = float(data.get("peak_dbfs", rms))
        if not (math.isfinite(rms) and math.isfinite(peak)):
            raise ValueError(f"non-finite level sample: rms={rms!r} peak={peak!r}")
        return cls(
            seq=int(data.get("seq", 0)),
            t_client_ms=int(data.get("t_client_ms", 0)),
            rms_dbfs=rms,
            peak_dbfs=peak,
            clip=bool(data.get("clip", False)),
            agc_frozen=bool(data.get("agc_frozen", True)),
            agc_unattested=bool(data.get("agc_unattested", False)),
        )


@dataclass(frozen=True)
class MeasurementRamp:
    """The ramp's tuning knobs -- one self-describing, validated config.

    All bounds are dBFS ``main_volume``; all durations seconds. Constructing an
    instance that would violate the overshoot invariant raises ``ValueError``:

        step_db + ramp_rate * max_loop_latency < 0.5 * window_width

    with ``ramp_rate = step_db / step_interval_s``. The coarse staircase stops at
    ``pre_window`` (below ``window_low_dbfs`` by at least that worst-case
    in-flight overshoot), so the sole approach into the window is a computed jump
    from a settled read. The invariant also ties the step to the WINDOW width, so
    this staircase cannot take audible-sized strides; a ramp that wants big
    strides steps on a fresh post-latency reading instead, which is what
    :mod:`jasper.active_speaker.seat_level_ramp` runs.
    """

    # Target window. The coarse staircase stops-ahead BELOW the bottom (the
    # pre_window); the settled JUMP aims at the window MIDPOINT, which gives it
    # symmetric ±half-window tolerance to gain-map noise.
    window_low_dbfs: float = -20.0
    window_high_dbfs: float = -12.0

    # Trust floor: a reading is trustable only once it clears
    # noise_floor + trust_margin_db. Below that the RMS is ambient-dominated.
    trust_margin_db: float = 10.0

    # Consecutive in-window trusted samples required before locking (k >= 3).
    confirm_k: int = 3

    # Coarse staircase. step/interval chosen so the overshoot invariant holds
    # with margin at the default 2 s loop latency: 0.75 + 1.5*2.0 = 3.75 < 4.0.
    start_db: float = -50.0
    step_db: float = 0.75
    step_interval_s: float = 0.5

    # Hold at least this long after the pre-window crossing before the settled
    # read may complete; it also requires settle_min_samples post-latency
    # samples, extending the hold on a sparse feed.
    settle_hold_s: float = 2.0
    max_loop_latency_s: float = 2.0
    # Minimum post-latency samples in the settle buffer before the median is
    # trusted (one sample is too noise-prone to aim a jump with).
    settle_min_samples: int = 3

    # Total jump budget: the initial settle jump plus at most one corrective
    # re-jump from CONFIRMING evidence.
    max_jumps: int = 2

    # At the hard/dynamic cap a below-window result may be accepted only as an
    # explicitly degraded bounded-low lock; the final ``confirm_k`` trusted,
    # post-latency samples must fit inside this peak-to-peak spread. A stability
    # policy, not permission to weaken any other guard.
    allow_bounded_low_level: bool = False
    bounded_low_max_spread_db: float = 1.5
    bounded_low_max_shortfall_db: float = 20.0

    # Empirical AGC verification for an unattested chain (no browser attestation
    # either way -- every WebKit build). Regress reported rms_dbfs against the
    # ramp's own commanded main_volume_db (both dB, so a gain-stable chain has
    # slope 1); a time-varying AGC gain flattens the response toward the
    # staircase. ``agc_slope_min_span_db`` is the PRIMARY evidence gate -- span
    # is the regression's x-leverage, and 3 steps at the default 0.75 dB is only
    # ~1.5 dB, over which OLS sampling noise can push a true-slope-1.0 chain
    # under the threshold by chance. 6 dB (8 steps) is robust while still
    # aborting a truly AGC'd chain far below the pre-window.
    # ``agc_slope_min_steps`` is a secondary floor on distinct commanded levels;
    # fewer than either bound is INDETERMINATE, never auto-passed. The 0.7
    # threshold leaves headroom for real reading jitter while staying above an
    # aggressive AGC's compressed 0.1-0.3. Placeholders until hardware-measured.
    agc_slope_min_span_db: float = 6.0
    agc_slope_min_steps: int = 3
    agc_slope_threshold: float = 0.7

    # Feed liveness: if NO samples at all arrive for this long after the tone
    # starts, the phone is gone -- abort and restore (a vanished phone also has
    # no clip protection).
    feed_timeout_s: float = 8.0

    # Safety timeout. None (the default) derives it from the config's own
    # worst-case walk -- see the `safety_timeout` property -- so a quiet amp
    # reaches MAXED_OUT rather than a generic CANCELLED. An explicit value is
    # honored verbatim.
    safety_timeout_s: float | None = None

    # Graceful fade-before-tone-kill.
    fade_down_to_db: float = -50.0
    fade_step_db: float = 2.0
    fade_step_s: float = 0.03

    # Dynamic cap: the lower of original + bump and the absolute ceiling. This is
    # the OPERATIVE ceiling, tighter than HARD_CEILING_DBFS. There is no floor:
    # flooring a quiet listener's cap upward can turn a promised +12 dB maximum
    # rise into a much larger, unsafe jump.
    cap_bump_db: float = 12.0
    cap_ceil_db: float = -3.0

    # Derived pre-window: the coarse staircase stops here, defaulted in
    # __post_init__ to the window bottom minus the worst-case in-flight
    # overshoot so the staircase provably never climbs into the window.
    pre_window_db: float | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.cap_bump_db) or not math.isfinite(self.cap_ceil_db):
            raise ValueError("cap_bump_db and cap_ceil_db must be finite")
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
        if self.settle_min_samples < 1:
            raise ValueError("settle_min_samples must be >= 1")
        if self.max_jumps < 1:
            raise ValueError("max_jumps must be >= 1")
        if self.agc_slope_min_steps < 2:
            raise ValueError(
                "agc_slope_min_steps must be >= 2 (a regression needs at least "
                "two distinct commanded levels)"
            )
        if (
            not math.isfinite(self.agc_slope_min_span_db)
            or self.agc_slope_min_span_db <= 0
        ):
            raise ValueError("agc_slope_min_span_db must be finite and > 0")
        if not math.isfinite(self.agc_slope_threshold) or self.agc_slope_threshold <= 0:
            raise ValueError("agc_slope_threshold must be finite and > 0")
        if (
            not math.isfinite(self.bounded_low_max_spread_db)
            or self.bounded_low_max_spread_db < 0
        ):
            raise ValueError("bounded_low_max_spread_db must be finite and >= 0")
        if (
            not math.isfinite(self.bounded_low_max_shortfall_db)
            or self.bounded_low_max_shortfall_db <= 0
        ):
            raise ValueError(
                "bounded_low_max_shortfall_db must be finite and > 0"
            )
        if self.feed_timeout_s <= 0:
            raise ValueError("feed_timeout_s must be positive")
        if self.safety_timeout_s is not None and self.safety_timeout_s <= 0:
            raise ValueError("safety_timeout_s must be positive when explicit")
        window_width = self.window_high_dbfs - self.window_low_dbfs
        overshoot = self.step_db + self.ramp_rate * self.max_loop_latency_s
        if not overshoot < 0.5 * window_width:
            raise ValueError(
                "overshoot guard violated: step_db + ramp_rate*max_loop_latency="
                f"{overshoot:.3f} dB must be < half the window width "
                f"{0.5 * window_width:.3f} dB (slow the ramp, shrink the step, "
                "shorten latency, or widen the window)"
            )
        # Fill the derived pre-window so the staircase stops below the window by
        # at least the worst-case in-flight overshoot.
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
        """The resolved pre-window threshold, never None after ``__post_init__``."""
        assert self.pre_window_db is not None  # set in __post_init__
        return self.pre_window_db

    @property
    def window_target(self) -> float:
        """Where the settled jump aims: the window midpoint (see field notes)."""
        return 0.5 * (self.window_low_dbfs + self.window_high_dbfs)

    @property
    def safety_timeout(self) -> float:
        """The effective safety timeout.

        Explicit ``safety_timeout_s`` wins. Otherwise derived from the config's
        own worst-case walk -- the full climb to the loosest cap, one settle,
        the jump budget's confirm phases, and a fixed margin -- so the timeout
        is a true backstop rather than a bound the staircase itself exceeds.
        """
        if self.safety_timeout_s is not None:
            return self.safety_timeout_s
        climb = (self.cap_ceil_db - self.start_db) / self.ramp_rate
        settle = (
            self.settle_hold_s
            + self.max_loop_latency_s
            + self.settle_min_samples * SAMPLE_BUDGET_S
        )
        confirm = self.max_loop_latency_s + self.confirm_k * SAMPLE_BUDGET_S
        return climb + settle + self.max_jumps * confirm + 5.0

    def dynamic_cap(self, original_db: float) -> float:
        """Return the operative cap without ever flooring a quiet start upward.

        Always ``<= original + bump`` and ``<= cap_ceil_db``: a
        ``max(cap_floor_db, ...)`` formula violates the first for quiet
        listening levels (``-45 + 12`` became ``-20``).
        """
        requested = original_db + self.cap_bump_db
        if not math.isfinite(requested):
            raise ValueError(
                "non-finite dynamic cap input: "
                f"original={original_db!r} bump={self.cap_bump_db!r}"
            )
        return min(requested, self.cap_ceil_db, HARD_CEILING_DBFS)

    @classmethod
    def from_env(cls, **overrides: Any) -> MeasurementRamp:
        """Build a config with hardware-gated knobs read from the environment.

        Explicit ``overrides`` win over env; anything else falls back to the
        documented default. Out-of-range or unparseable env values are ignored,
        and when individually-valid env values fail CROSS-FIELD validation the
        env set is dropped as a whole with a warning, so a jasper.env edit can
        never brick the ramp at construction time.
        """
        env_values: dict[str, Any] = {
            "window_low_dbfs": bounded_env_float(
                "JASPER_RAMP_WINDOW_LOW_DBFS", cls.window_low_dbfs, lo=-60.0, hi=0.0
            ),
            "window_high_dbfs": bounded_env_float(
                "JASPER_RAMP_WINDOW_HIGH_DBFS", cls.window_high_dbfs, lo=-60.0, hi=0.0
            ),
            "trust_margin_db": bounded_env_float(
                "JASPER_RAMP_TRUST_MARGIN_DB", cls.trust_margin_db, lo=0.0, hi=40.0
            ),
            "settle_hold_s": bounded_env_float(
                "JASPER_RAMP_SETTLE_HOLD_S", cls.settle_hold_s, lo=0.0, hi=30.0
            ),
            "max_loop_latency_s": bounded_env_float(
                "JASPER_RAMP_MAX_LOOP_LATENCY_S",
                cls.max_loop_latency_s,
                lo=0.0,
                hi=30.0,
            ),
            # Env floor is 2, not 1: the spec pins k >= 3 as the default and a
            # deploy knob may trade one confirmation for speed, but a single
            # sample is never "consecutive confirmation".
            "confirm_k": bounded_env_int(
                "JASPER_RAMP_CONFIRM_K", cls.confirm_k, lo=2, hi=20
            ),
            "settle_min_samples": bounded_env_int(
                "JASPER_RAMP_SETTLE_MIN_SAMPLES",
                cls.settle_min_samples,
                lo=1,
                hi=10,
            ),
            "feed_timeout_s": bounded_env_float(
                "JASPER_RAMP_FEED_TIMEOUT_S", cls.feed_timeout_s, lo=2.0, hi=60.0
            ),
            "cap_bump_db": bounded_env_float(
                "JASPER_RAMP_CAP_BUMP_DB", cls.cap_bump_db, lo=0.0, hi=24.0
            ),
            "cap_ceil_db": bounded_env_float(
                "JASPER_RAMP_CAP_CEIL_DB", cls.cap_ceil_db, lo=-30.0, hi=0.0
            ),
            "agc_slope_min_span_db": bounded_env_float(
                "JASPER_RAMP_AGC_SLOPE_MIN_SPAN_DB",
                cls.agc_slope_min_span_db,
                lo=1.0,
                hi=20.0,
            ),
            "agc_slope_min_steps": bounded_env_int(
                "JASPER_RAMP_AGC_SLOPE_MIN_STEPS",
                cls.agc_slope_min_steps,
                lo=2,
                hi=10,
            ),
            "agc_slope_threshold": bounded_env_float(
                "JASPER_RAMP_AGC_SLOPE_THRESHOLD",
                cls.agc_slope_threshold,
                lo=0.1,
                hi=1.0,
            ),
        }
        merged = {**env_values, **overrides}
        try:
            return cls(**merged)
        except ValueError as exc:
            log_event(
                logger,
                "ramp_env_config_invalid",
                level=logging.WARNING,
                error=str(exc),
                action="falling back to defaults for env-provided knobs",
            )
            return cls(**overrides)


@dataclass
class RampData:
    """Live state of one ramp run. Replaced when a new run starts."""

    state: RampState = RampState.IDLE
    current_main_volume_db: float = -50.0
    original_main_volume_db: float | None = None
    locked_main_volume_db: float | None = None
    lock_kind: RampLockKind | None = None
    cap_db: float | None = None
    # The recovered chain gain G = settled_mic_dbfs - v_held (dB). Persisted into
    # the geometry lock so the drift check has the mapping. None until a settle.
    gain_map_db: float | None = None
    settled_mic_dbfs: float | None = None
    settled_snr_db: float | None = None
    # How far the SETTLED median sits below the window bottom. Its population is
    # the settle/confirm evidence -- trusted, gate-passing samples at the volume
    # the settle happened at -- not "the last observed sample", and in an
    # ambient-dominated room the two are far apart (jts3 2026-08-22 printed
    # window_shortfall_db 1.39 beside an observed 54.8 dB SPL against a 72.5 dB
    # SPL edge; 1138 of that run's 1194 samples had failed the gate). Read it
    # against ``settled_mic_dbfs``, never against a raw reading.
    window_shortfall_db: float | None = None
    settled_spread_db: float | None = None
    noise_floor_dbfs: float | None = None
    trust_margin_db: float | None = None
    agc_frozen: bool = True
    # True once any admitted sample carried agc_unattested=true (the browser
    # never reported autoGainControl either way). Gates the empirical slope
    # verification below.
    agc_unattested: bool = False
    # None = not yet decided (insufficient distinct commanded-level evidence).
    # True = the reported-vs-commanded slope cleared the threshold. False = it
    # did not, and the run aborts (agc_suspected) the moment this is set.
    agc_verified: bool | None = None
    # The most recently computed regression slope, for diagnostics/logging.
    agc_slope: float | None = None
    # Count of trusted samples ever accepted. A phone that never produced a
    # usable sample is an ERROR, not an acoustic diagnosis.
    trusted_sample_count: int = 0
    # Admission diagnostics for the full, fresh phone stream: why a live meter
    # produced zero trusted samples, without retaining the high-rate payload or
    # weakening the clip/AGC/noise gates.
    observed_sample_count: int = 0
    finite_sample_count: int = 0
    below_noise_sample_count: int = 0
    agc_rejected_sample_count: int = 0
    nonfinite_sample_count: int = 0
    max_observed_rms_dbfs: float | None = None
    max_observed_peak_dbfs: float | None = None
    max_signal_over_noise_db: float | None = None
    error: str | None = None
    # Extra homeowner-facing specifics beyond the stable `error` code. None when
    # a terminal has nothing to add; see
    # jasper.correction.level_match.describe_ramp_refusal.
    error_detail: str | None = None
    # Idempotency guard for terminal-state listening-level restore.
    restored: bool = False

    @property
    def trust_threshold_dbfs(self) -> float | None:
        if self.noise_floor_dbfs is None or self.trust_margin_db is None:
            return None
        return self.noise_floor_dbfs + self.trust_margin_db

    @property
    def trust_deficit_db(self) -> float | None:
        threshold = self.trust_threshold_dbfs
        if threshold is None or self.max_observed_rms_dbfs is None:
            return None
        return max(0.0, threshold - self.max_observed_rms_dbfs)

    @property
    def agc_trusted(self) -> bool:
        """Whether this run's level evidence is a trustworthy gain-map reference.

        For an ordinary (non-unattested) run this is exactly ``agc_frozen``. For
        an unattested run it is the EMPIRICAL verdict instead
        (``agc_verified is True``), never the raw ``agc_frozen``, which stays
        False for an unattested run at the wire level by design.
        """
        if self.agc_unattested:
            return self.agc_verified is True
        return self.agc_frozen

    def snapshot(self) -> dict[str, Any]:
        def r(x: float | None) -> float | None:
            return round(x, 2) if x is not None else None

        return {
            "state": self.state.value,
            "current_main_volume_db": r(self.current_main_volume_db),
            "original_main_volume_db": r(self.original_main_volume_db),
            "locked_main_volume_db": r(self.locked_main_volume_db),
            "lock_kind": self.lock_kind.value if self.lock_kind is not None else None,
            "cap_db": r(self.cap_db),
            "gain_map_db": r(self.gain_map_db),
            "settled_mic_dbfs": r(self.settled_mic_dbfs),
            "settled_snr_db": r(self.settled_snr_db),
            "window_shortfall_db": r(self.window_shortfall_db),
            "settled_spread_db": r(self.settled_spread_db),
            "noise_floor_dbfs": r(self.noise_floor_dbfs),
            "trust_margin_db": r(self.trust_margin_db),
            "trust_threshold_dbfs": r(self.trust_threshold_dbfs),
            "trust_deficit_db": r(self.trust_deficit_db),
            "agc_frozen": self.agc_frozen,
            "agc_unattested": self.agc_unattested,
            "agc_verified": self.agc_verified,
            "agc_slope": r(self.agc_slope),
            "agc_trusted": self.agc_trusted,
            "trusted_sample_count": self.trusted_sample_count,
            "observed_sample_count": self.observed_sample_count,
            "finite_sample_count": self.finite_sample_count,
            "below_noise_sample_count": self.below_noise_sample_count,
            "agc_rejected_sample_count": self.agc_rejected_sample_count,
            "nonfinite_sample_count": self.nonfinite_sample_count,
            "max_observed_rms_dbfs": r(self.max_observed_rms_dbfs),
            "max_observed_peak_dbfs": r(self.max_observed_peak_dbfs),
            "max_signal_over_noise_db": r(self.max_signal_over_noise_db),
            "error": self.error,
            "error_detail": self.error_detail,
            "restored": self.restored,
        }


# A source of the next batch of phone-reported samples. Injected so the loop is
# testable with a synthetic feed; the real feed polls the relay (rate-limited
# feed-side). An empty list means "no new samples this tick" and the loop keeps
# its clock running.
SampleSource = Callable[[], Awaitable[list[LevelSample]]]

# Injected monotonic clock (seconds) + async sleep, so tests drive time directly.
Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]

VolumeSetter = Callable[[float], Awaitable[Any]]
VolumeGetter = Callable[[], Awaitable[float]]


@dataclass
class _LoopVars:
    """Mutable per-run loop state, grouped so the tick handler stays readable."""

    start_time: float = 0.0
    last_step_time: float = 0.0
    last_feed_time: float = 0.0
    # Samples arriving before this reflect a pre-change level (transport lag).
    blank_until: float = 0.0
    settle_start: float | None = None
    settle_buf: list[float] = field(default_factory=list)
    confirm_in_streak: int = 0
    confirm_out_buf: list[float] = field(default_factory=list)
    jumps_used: int = 0
    # True only when SETTLING was entered because the safe cap was reached below
    # the pre-window; it routes the evidence to the explicitly degraded
    # bounded-low policy instead of fabricating a normal window lock.
    bounded_low_candidate: bool = False
    # (commanded_main_volume_db, reported_rms_dbfs) pairs from an unattested
    # chain's trusted samples (raw, not blank_until-gated -- see run()), for the
    # empirical AGC slope check.
    agc_evidence: list[tuple[float, float]] = field(default_factory=list)
    # (slope, distinct_step_count) recorded the first time the AGC slope check
    # fails the threshold. One marginal estimate is not enough evidence to refuse
    # a measurement (jts3 2026-07-16: 0.644 over 3 steps / 6.65 dB span, the same
    # mic clean at 4 steps twenty minutes later), so the gate holds the verdict
    # open for one more staircase step. None means no failing estimate yet, or
    # that the one extension is already in flight.
    agc_marginal: tuple[float, int] | None = None


def _ols_slope(points: list[tuple[float, float]]) -> float | None:
    """OLS slope of ``y`` (rms_dbfs) against ``x`` (commanded dB).

    ``None`` when there are too few points or the x-values are degenerate (all
    equal), so a caller can tell "not enough evidence yet" from a real low slope.
    """
    if len(points) < 2:
        return None
    x_mean = sum(x for x, _ in points) / len(points)
    y_mean = sum(y for _, y in points) / len(points)
    denom = sum((x - x_mean) ** 2 for x, _ in points)
    if denom <= 1e-9:
        return None
    numer = sum((x - x_mean) * (y - y_mean) for x, y in points)
    return numer / denom


class RampController:
    """Owns the settle-based ramp loop, its state, and volume restoration.

    Public surface mirrors ``AutolevelController`` (``run`` / ``lock`` /
    ``cancel`` / ``restore_listening_volume_if_ramped``) so the correction
    session's adapter can swap the engine without changing its callers.
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
        self._restore_lock = asyncio.Lock()

    @property
    def main_volume_setter(self) -> VolumeSetter | None:
        return self._main_volume_setter

    @main_volume_setter.setter
    def main_volume_setter(self, setter: VolumeSetter | None) -> None:
        self._main_volume_setter = setter

    async def restore_listening_volume_if_ramped(self) -> None:
        """Restore main_volume when a measurement ends outside apply/reset.

        Failed / verify-ended measurements skip the web apply/reset handlers, so
        this best-effort hook restores the user's level there. A MAXED_OUT run
        already attempts an immediate restore but remains eligible here as a
        retry. Idempotent; swallows recoverable errors.
        """
        async with self._restore_lock:
            d = self.data
            if d.restored:
                return
            if d.state not in (RampState.LOCKED, RampState.MAXED_OUT):
                return
            if d.original_main_volume_db is None or self._main_volume_setter is None:
                return
            try:
                applied = await self._main_volume_setter(d.original_main_volume_db)
                if applied is False:
                    logger.error(
                        "ramp volume restore was rejected (session=%s)",
                        self.session_id,
                    )
                    return
                d.restored = True
                log_event(
                    logger,
                    "ramp_volume_restored",
                    session=self.session_id,
                    to_db=f"{d.original_main_volume_db:.1f}",
                    trigger="measurement_ended",
                )
            except RECOVERABLE_ERRORS:
                logger.exception(
                    "ramp volume restore on measurement end failed (session=%s) — "
                    "speaker may remain at the measurement level until /reset",
                    self.session_id,
                )

    async def lock(self) -> bool:
        """Signal the running ramp to lock at the current main_volume."""
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

    def _cap_value(self) -> float:
        """The effective ramp ceiling: min(dynamic cap, hard 0 dB ceiling)."""
        cap = self.data.cap_db
        return HARD_CEILING_DBFS if cap is None else min(cap, HARD_CEILING_DBFS)

    def _safe_target(self, desired_db: float) -> float:
        """Clamp a desired ramp volume to the operative cap AND the hard ceiling.

        The single choke point every ramp-commanded volume passes through. Never
        returns a value above ``min(dynamic_cap, HARD_CEILING_DBFS)``, and never
        a non-finite value: NaN would tunnel through ``min()``, so it raises.
        """
        if not math.isfinite(desired_db):
            raise ValueError(f"non-finite ramp volume target: {desired_db!r}")
        return min(desired_db, self._cap_value())

    def _at_cap(self) -> bool:
        return self.data.current_main_volume_db >= self._cap_value() - 1e-9

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

        Injected dependencies keep the loop pure and testable.
        ``play_continuous_tone`` must play until ``cancel_tone()`` is called (the
        ``TonePlayer.play`` shape); the kernel runs it as a task and treats an
        early finish as an error. It is started AFTER the quiet start volume is
        set and killed AFTER the fade-down (audio-safety order). A non-finite
        ``noise_floor_dbfs`` is treated as unknown (no trust floor) with a
        warning, never as a gate that silently passes everything.
        """
        cfg = self.config
        if noise_floor_dbfs is not None and not math.isfinite(noise_floor_dbfs):
            log_event(
                logger,
                "ramp_noise_floor_invalid",
                level=logging.WARNING,
                session=self.session_id,
                value=repr(noise_floor_dbfs),
            )
            noise_floor_dbfs = None
        self.data = d = RampData(
            current_main_volume_db=cfg.start_db,
            noise_floor_dbfs=noise_floor_dbfs,
            trust_margin_db=cfg.trust_margin_db,
        )
        self._main_volume_setter = set_main_volume_db
        self._lock_requested = False
        self._cancel_requested = False
        tone_task: asyncio.Future[Any] | None = None
        v = _LoopVars()

        async def _set(db: float) -> None:
            safe = self._safe_target(db)
            await set_main_volume_db(safe)
            d.current_main_volume_db = safe
            # Reports arriving within the loop latency reflect the OLD level.
            v.blank_until = clock() + cfg.max_loop_latency_s

        async def _graceful_stop(final_db: float | None) -> None:
            """Fade down before killing the tone, then set the final volume.

            Never kill the tone at a loud level. ``final_db`` is clamped only to
            the 0 dB HARD ceiling, NOT the dynamic cap: the finals that arrive
            here are either a lock value already emitted through ``_set`` (<= cap
            by construction) or the user's own pre-ramp volume, and "restoring" a
            −5 dB listener to the −6 dB measurement cap would be a regression.
            """
            try:
                cur = d.current_main_volume_db
                while cur > cfg.fade_down_to_db:
                    cur = max(cfg.fade_down_to_db, cur - cfg.fade_step_db)
                    try:
                        await set_main_volume_db(cur)
                        d.current_main_volume_db = cur
                    except RECOVERABLE_ERRORS:
                        break
                    await sleep(cfg.fade_step_s)
            finally:
                cancel_tone()
                if tone_task is not None:
                    try:
                        await asyncio.wait_for(tone_task, timeout=2.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
                    except RECOVERABLE_ERRORS:
                        logger.exception("tone task ended with an error")
                if final_db is not None and math.isfinite(final_db):
                    final = min(final_db, HARD_CEILING_DBFS)
                    try:
                        await set_main_volume_db(final)
                        d.current_main_volume_db = final
                    except RECOVERABLE_ERRORS:
                        logger.exception(
                            "ramp final volume set failed (session=%s)",
                            self.session_id,
                        )

        async def _terminal(
            state: RampState,
            *,
            final_db: float | None,
            error: str | None = None,
            error_detail: str | None = None,
            event: str | None = None,
            level: int = logging.INFO,
            **event_fields: Any,
        ) -> RampData:
            d.state = state
            if error is not None:
                d.error = error
            if error_detail is not None:
                d.error_detail = error_detail
            if event is not None:
                log_event(
                    logger,
                    event,
                    level=level,
                    session=self.session_id,
                    at_db=f"{d.current_main_volume_db:.1f}",
                    **event_fields,
                )
            await _graceful_stop(final_db)
            return d

        try:
            original = float(await get_main_volume_db())
            if not math.isfinite(original):
                # No volume was changed yet — fail before touching anything.
                d.state = RampState.ERROR
                d.error = f"non-finite pre-ramp main_volume: {original!r}"
                log_event(
                    logger,
                    "ramp_error",
                    level=logging.WARNING,
                    session=self.session_id,
                    error=d.error,
                    reason="non_finite_original",
                )
                return d
            d.original_main_volume_db = original
            d.cap_db = cfg.dynamic_cap(original)
            d.state = RampState.CLIMBING
            log_event(
                logger,
                "ramp_start",
                session=self.session_id,
                original_db=f"{original:.1f}",
                cap_db=f"{d.cap_db:.1f}",
                window=f"[{cfg.window_low_dbfs:.0f},{cfg.window_high_dbfs:.0f}]",
                rate_db_s=f"{cfg.ramp_rate:.2f}",
                safety_timeout_s=f"{cfg.safety_timeout:.0f}",
                trust_margin_db=f"{cfg.trust_margin_db:.1f}",
                trust_threshold_dbfs=(
                    f"{d.trust_threshold_dbfs:.1f}"
                    if d.trust_threshold_dbfs is not None
                    else ""
                ),
            )

            # Audio-safety order: quiet start BEFORE the tone. ensure_future
            # (not create_task) so any Awaitable-shaped tone callable works.
            await _set(cfg.start_db)
            await sleep(0.1)
            tone_task = asyncio.ensure_future(play_continuous_tone())

            v.start_time = v.last_step_time = v.last_feed_time = clock()

            while True:
                now = clock()
                if self._cancel_requested:
                    return await _terminal(
                        RampState.CANCELLED,
                        final_db=d.original_main_volume_db,
                        event="ramp_cancelled",
                    )
                if self._lock_requested:
                    # Manual lock: trust the user, lock where we are.
                    d.locked_main_volume_db = d.current_main_volume_db
                    d.lock_kind = RampLockKind.MANUAL
                    return await _terminal(
                        RampState.LOCKED,
                        final_db=d.current_main_volume_db,
                        event="ramp_locked",
                        trigger="manual",
                    )
                if now - v.start_time > cfg.safety_timeout:
                    return await _terminal(
                        RampState.CANCELLED,
                        final_db=d.original_main_volume_db,
                        error=f"safety timeout after {cfg.safety_timeout:.0f}s",
                        event="ramp_safety_timeout",
                        level=logging.WARNING,
                    )
                if tone_task.done():
                    # The tone must outlive the ramp (contract in the docstring):
                    # a silent speaker with a live mic feed would blind-climb.
                    return await _terminal(
                        RampState.ERROR,
                        final_db=d.original_main_volume_db,
                        error="tone ended before the ramp completed",
                        event="ramp_error",
                        level=logging.WARNING,
                        reason="tone_ended_early",
                    )

                batch = await next_samples()
                if batch:
                    v.last_feed_time = now
                elif now - v.last_feed_time > cfg.feed_timeout_s:
                    # A vanished phone has no clip protection — never blind-climb.
                    return await _terminal(
                        RampState.ABORTED,
                        final_db=d.original_main_volume_db,
                        error=(
                            "phone feed lost (no samples for "
                            f"{cfg.feed_timeout_s:.0f}s)"
                        ),
                        event="ramp_feed_lost",
                        level=logging.WARNING,
                    )
                trusted = self._process_batch(d, batch)
                if d.state == RampState.ABORTED:
                    await _graceful_stop(d.original_main_volume_db)
                    return d
                d.trusted_sample_count += len(trusted)
                # Samples arriving before blank_until reflect a pre-change level.
                # (Clip detection already ran on the FULL batch above.)
                settled_stream = (
                    [s.rms_dbfs for s in trusted] if now >= v.blank_until else []
                )

                if d.agc_unattested and d.agc_verified is None and trusted:
                    # Deliberately NOT settled_stream: the default step cadence
                    # (0.5 s) is much faster than max_loop_latency_s (2 s), so
                    # blank_until never clears mid-climb and a blank-gated stream
                    # would starve the regression of the staircase evidence it
                    # needs. OLS averages out the per-sample lag.
                    self._update_agc_evidence(
                        d, v, cfg, [s.rms_dbfs for s in trusted]
                    )
                    if d.agc_verified is False:
                        # A CONFIRMED slope failure (the marginal-estimate
                        # extension already ran and its evidence still fails):
                        # abort now, never keep climbing an AGC-suspected chain.
                        xs = [x for x, _ in v.agc_evidence]
                        steps = len({round(x, 3) for x in xs})
                        slopes: list[float] = []
                        if v.agc_marginal is not None:
                            slopes.append(v.agc_marginal[0])
                        if d.agc_slope is not None:
                            slopes.append(d.agc_slope)
                        return await _terminal(
                            RampState.ERROR,
                            final_db=d.original_main_volume_db,
                            error="agc_suspected",
                            error_detail=(
                                "slopes "
                                + ", ".join(f"{s:.2f}" for s in slopes)
                                + f" over {steps} steps"
                            ),
                            event="ramp_agc_suspected",
                            level=logging.WARNING,
                            slope=(
                                f"{d.agc_slope:.3f}" if d.agc_slope is not None else ""
                            ),
                            steps=steps,
                            span_db=(f"{max(xs) - min(xs):.2f}" if xs else "0"),
                        )

                outcome = await self._tick_state(
                    d, v, cfg, now, trusted, settled_stream, _set, _terminal
                )
                if outcome is not None:
                    return outcome

                await sleep(0.01)
        except RECOVERABLE_ERRORS as e:
            d.state = RampState.ERROR
            d.error = str(e)
            logger.exception("ramp failed (session=%s)", self.session_id)
            try:
                if d.original_main_volume_db is not None:
                    await _graceful_stop(d.original_main_volume_db)
                elif tone_task is not None:
                    cancel_tone()
            except RECOVERABLE_ERRORS:
                logger.exception("ramp error-path cleanup failed")
            return d

    async def _tick_state(
        self,
        d: RampData,
        v: _LoopVars,
        cfg: MeasurementRamp,
        now: float,
        trusted: list[LevelSample],
        settled_stream: list[float],
        _set: Callable[[float], Awaitable[None]],
        _terminal: Callable[..., Awaitable[RampData]],
    ) -> RampData | None:
        """One state-machine step. Returns terminal RampData, or None to continue."""
        if d.state == RampState.CLIMBING:
            # Freeze the moment ANY trusted reading crosses the pre-window -- max
            # over the batch, not the newest sample, so a mid-batch crossing
            # whose newest sample dipped does not delay the freeze. Stale
            # (pre-blank) samples are deliberately included: a stale crossing
            # means the true level is even higher, so freezing is MORE urgent.
            if trusted and max(s.rms_dbfs for s in trusted) >= cfg.pre_window:
                d.state = RampState.SETTLING
                v.settle_start = now
                v.settle_buf = []
                v.bounded_low_candidate = False
                log_event(
                    logger,
                    "ramp_pre_window",
                    session=self.session_id,
                    at_db=f"{d.current_main_volume_db:.1f}",
                    mic_dbfs=f"{max(s.rms_dbfs for s in trusted):.1f}",
                )
            elif now - v.last_step_time >= cfg.step_interval_s:
                v.last_step_time = now
                if self._at_cap():
                    if d.trusted_sample_count > 0:
                        # Reached the cap without crossing the pre-window. Hold
                        # the volume fixed and collect fresh post-latency
                        # evidence; historical climb samples are not enough.
                        d.state = RampState.SETTLING
                        v.settle_start = now
                        v.settle_buf = []
                        v.bounded_low_candidate = True
                        log_event(
                            logger,
                            "ramp_cap_settling",
                            session=self.session_id,
                            at_db=f"{d.current_main_volume_db:.1f}",
                            reason="below_pre_window",
                        )
                        return None
                    # Zero usable evidence the mic ever heard the speaker:
                    # NOT an amp diagnosis — error out and restore.
                    return await _terminal(
                        RampState.ERROR,
                        final_db=d.original_main_volume_db,
                        error="no usable phone samples",
                        event="ramp_error",
                        level=logging.WARNING,
                        reason="no_usable_samples",
                        observed_samples=d.observed_sample_count,
                        finite_samples=d.finite_sample_count,
                        below_noise_samples=d.below_noise_sample_count,
                        agc_rejected_samples=d.agc_rejected_sample_count,
                        max_rms_dbfs=(
                            f"{d.max_observed_rms_dbfs:.1f}"
                            if d.max_observed_rms_dbfs is not None
                            else ""
                        ),
                        max_signal_over_noise_db=(
                            f"{d.max_signal_over_noise_db:.1f}"
                            if d.max_signal_over_noise_db is not None
                            else ""
                        ),
                        trust_margin_db=f"{cfg.trust_margin_db:.1f}",
                        trust_threshold_dbfs=(
                            f"{d.trust_threshold_dbfs:.1f}"
                            if d.trust_threshold_dbfs is not None
                            else ""
                        ),
                        trust_deficit_db=(
                            f"{d.trust_deficit_db:.1f}"
                            if d.trust_deficit_db is not None
                            else ""
                        ),
                    )
                # A fixed rung, NOT :func:`capped_gap_step_db`. The climb's
                # target is a threshold it must CROSS, not a point to land on,
                # and a gap step aimed at a threshold asymptotes: each step is
                # the remaining gap, so the crossing never arrives. Measured, not
                # reasoned. The gap policy belongs where a ramp lands ON a value:
                # the settled jump below.
                await _set(d.current_main_volume_db + cfg.step_db)

        elif d.state == RampState.SETTLING:
            assert v.settle_start is not None
            v.settle_buf.extend(settled_stream)
            hold_elapsed = now - v.settle_start >= cfg.settle_hold_s
            if hold_elapsed and len(v.settle_buf) >= cfg.settle_min_samples:
                settled = self._record_settled_evidence(d, cfg, v.settle_buf)
                log_event(
                    logger,
                    "ramp_settled",
                    session=self.session_id,
                    at_db=f"{d.current_main_volume_db:.1f}",
                    settled_mic_dbfs=f"{settled:.1f}",
                    gain_map_db=f"{d.gain_map_db:.1f}",
                    samples=len(v.settle_buf),
                )
                if cfg.window_low_dbfs <= settled <= cfg.window_high_dbfs:
                    self._enter_confirming(v)
                    d.state = RampState.CONFIRMING
                elif (
                    v.bounded_low_candidate
                    and settled < cfg.window_low_dbfs
                    and self._at_cap()
                ):
                    # Already pinned at the allowed cap: confirm a fresh stable
                    # tail and label the result explicitly rather than
                    # manufacture a jump or an in-window lock.
                    self._enter_confirming(v)
                    d.state = RampState.CONFIRMING
                else:
                    await self._apply_jump(d, v, cfg, settled, _set)
                    d.state = RampState.CONFIRMING
            # else: keep holding -- a momentarily-empty feed EXTENDS the hold;
            # the machine never bounces back to CLIMBING.

        elif d.state == RampState.CONFIRMING:
            for value in settled_stream:
                if cfg.window_low_dbfs <= value <= cfg.window_high_dbfs:
                    v.confirm_in_streak += 1
                    v.confirm_out_buf = []
                else:
                    v.confirm_out_buf.append(value)
                    v.confirm_in_streak = 0
            if v.confirm_in_streak >= cfg.confirm_k:
                if d.agc_unattested and not d.agc_trusted:
                    # A slope FAILURE already aborted earlier in run(), so the
                    # only way to reach a would-be lock here with agc_trusted
                    # False is an INDETERMINATE verdict. Fail closed, under a
                    # DISTINCT wire code: no AGC was observed, only insufficient
                    # evidence, and the phone renders different copy for each.
                    xs = [x for x, _ in v.agc_evidence]
                    return await _terminal(
                        RampState.ERROR,
                        final_db=d.original_main_volume_db,
                        error="agc_indeterminate",
                        event="ramp_agc_indeterminate",
                        level=logging.WARNING,
                        reason="insufficient_slope_evidence",
                        steps=len({round(x, 3) for x in xs}),
                        span_db=(f"{max(xs) - min(xs):.2f}" if xs else "0"),
                    )
                d.locked_main_volume_db = d.current_main_volume_db
                d.lock_kind = RampLockKind.IN_WINDOW
                return await _terminal(
                    RampState.LOCKED,
                    final_db=d.current_main_volume_db,
                    event="ramp_locked",
                    trigger="window",
                    settled_mic_dbfs=(
                        f"{d.settled_mic_dbfs:.1f}"
                        if d.settled_mic_dbfs is not None
                        else ""
                    ),
                )
            if len(v.confirm_out_buf) >= cfg.confirm_k and settled_stream:
                below = all(x < cfg.window_low_dbfs for x in v.confirm_out_buf)
                above = all(x > cfg.window_high_dbfs for x in v.confirm_out_buf)
                if not (below or above):
                    # Straddling the window edges — boundary noise; reset and
                    # keep collecting (the safety timeout is the backstop).
                    v.confirm_out_buf = []
                    return None
                evidence_values = v.confirm_out_buf[-cfg.confirm_k :]
                evidence = self._record_settled_evidence(d, cfg, evidence_values)
                if below and self._at_cap():
                    if self._bounded_low_level_is_usable(d, cfg):
                        d.locked_main_volume_db = d.current_main_volume_db
                        d.lock_kind = RampLockKind.BOUNDED_LOW_LEVEL
                        return await _terminal(
                            RampState.LOCKED,
                            final_db=d.current_main_volume_db,
                            event="ramp_locked",
                            trigger=RampLockKind.BOUNDED_LOW_LEVEL.value,
                            settled_mic_dbfs=f"{evidence:.1f}",
                            snr_db=f"{d.settled_snr_db:.1f}",
                            shortfall_db=f"{d.window_shortfall_db:.1f}",
                            spread_db=f"{d.settled_spread_db:.1f}",
                        )
                    # The cap was genuinely reached but the bounded-low contract
                    # was not proven. Preserve the evidence and restore; never
                    # masquerade as a normal lock.
                    return await _terminal(
                        RampState.MAXED_OUT,
                        final_db=d.original_main_volume_db,
                        error=(
                            "safe cap reached below target window; raise the "
                            "external amplifier and retry"
                        ),
                        event="ramp_maxed_out",
                        level=logging.WARNING,
                        reason="bounded_low_evidence_insufficient",
                        settled_mic_dbfs=f"{evidence:.1f}",
                        snr_db=(
                            f"{d.settled_snr_db:.1f}"
                            if d.settled_snr_db is not None
                            else ""
                        ),
                        shortfall_db=(
                            f"{d.window_shortfall_db:.1f}"
                            if d.window_shortfall_db is not None
                            else ""
                        ),
                        spread_db=(
                            f"{d.settled_spread_db:.1f}"
                            if d.settled_spread_db is not None
                            else ""
                        ),
                    )
                if v.jumps_used < cfg.max_jumps:
                    await self._apply_jump(d, v, cfg, evidence, _set)
                    return None
                # Jump budget exhausted and still out of window: keep confirming
                # until the timeout restores, rather than oscillating.
                v.confirm_out_buf = []
        return None

    @staticmethod
    def _record_settled_evidence(
        d: RampData,
        cfg: MeasurementRamp,
        values: list[float],
    ) -> float:
        """Persist the actual mic evidence used for a lock/verdict."""
        settled = float(statistics.median(values))
        d.settled_mic_dbfs = settled
        d.gain_map_db = settled - d.current_main_volume_db
        d.settled_spread_db = max(values) - min(values)
        d.settled_snr_db = (
            settled - d.noise_floor_dbfs if d.noise_floor_dbfs is not None else None
        )
        d.window_shortfall_db = max(0.0, cfg.window_low_dbfs - settled)
        return settled

    def _update_agc_evidence(
        self,
        d: RampData,
        v: _LoopVars,
        cfg: MeasurementRamp,
        rms_values: list[float],
    ) -> None:
        """Fold this tick's trusted samples into the AGC slope evidence.

        Called only for an unattested run whose verdict is still undecided.
        Appends ``(commanded_db, rms)`` pairs at the CURRENT commanded level,
        then regresses reported rms against commanded dB once the evidence covers
        at least ``agc_slope_min_span_db`` of commanded-level SPAN (the primary
        gate) AND ``agc_slope_min_steps`` distinct commanded levels.

        A slope at or above ``agc_slope_threshold`` sets ``agc_verified = True``
        immediately. A slope BELOW threshold is provisional, not terminal -- one
        marginal estimate at the minimum evidence window is noisy (jts3
        2026-07-16: 0.644 over 3 steps / 6.65 dB span, the same mic clean at 4
        steps twenty minutes later) -- so the first failing estimate is held in
        ``v.agc_marginal`` and only a SECOND failing evaluation, with strictly
        more distinct commanded levels, sets ``agc_verified = False``.
        """
        for rms in rms_values:
            v.agc_evidence.append((d.current_main_volume_db, rms))
        steps = {round(x, 3) for x, _ in v.agc_evidence}
        if len(steps) < cfg.agc_slope_min_steps:
            return
        span = max(x for x, _ in v.agc_evidence) - min(x for x, _ in v.agc_evidence)
        if span < cfg.agc_slope_min_span_db:
            return
        if v.agc_marginal is not None and len(steps) <= v.agc_marginal[1]:
            # Holding for the one extension's extra step of evidence — the
            # step count hasn't advanced since the marginal estimate yet.
            return
        slope = _ols_slope(v.agc_evidence)
        if slope is None:
            return
        d.agc_slope = slope
        if slope >= cfg.agc_slope_threshold:
            d.agc_verified = True
            log_event(
                logger,
                "ramp_agc_verified",
                session=self.session_id,
                slope=f"{slope:.3f}",
                steps=len(steps),
                at_db=f"{d.current_main_volume_db:.1f}",
            )
            return
        if v.agc_marginal is None:
            v.agc_marginal = (slope, len(steps))
            log_event(
                logger,
                "ramp_agc_marginal",
                level=logging.INFO,
                session=self.session_id,
                slope=f"{slope:.3f}",
                steps=len(steps),
                at_db=f"{d.current_main_volume_db:.1f}",
            )
            return
        # The one-step extension's evidence still fails: `run()` reads
        # agc_verified is False and fires the ramp_agc_suspected terminal.
        d.agc_verified = False

    @staticmethod
    def _bounded_low_level_is_usable(
        d: RampData,
        cfg: MeasurementRamp,
    ) -> bool:
        """Whether cap evidence satisfies the degraded lock contract.

        Uses ``agc_trusted``, not the raw ``agc_frozen`` flag. An unattested run
        that reaches the cap with too little commanded-level span to render an
        AGC verdict is INDETERMINATE, and ``agc_trusted`` is False there too, so
        it fails closed to the ordinary ``bounded_low_evidence_insufficient``
        MAXED_OUT path rather than manufacturing a degraded lock on unproven gain
        stability. A slope FAILURE never reaches here: it aborts in ``run()``.
        """
        return bool(
            cfg.allow_bounded_low_level
            and d.agc_trusted
            and d.noise_floor_dbfs is not None
            and d.settled_snr_db is not None
            and d.settled_snr_db >= cfg.trust_margin_db
            and d.window_shortfall_db is not None
            and d.window_shortfall_db > 0.0
            and d.window_shortfall_db <= cfg.bounded_low_max_shortfall_db
            and d.settled_spread_db is not None
            and d.settled_spread_db <= cfg.bounded_low_max_spread_db
        )

    def _enter_confirming(self, v: _LoopVars) -> None:
        v.confirm_in_streak = 0
        v.confirm_out_buf = []

    async def _apply_jump(
        self,
        d: RampData,
        v: _LoopVars,
        cfg: MeasurementRamp,
        observed_mic_dbfs: float,
        _set: Callable[[float], Awaitable[None]],
    ) -> None:
        """One computed jump so the mic lands at the window midpoint.

        The step is :func:`capped_gap_step_db` with no cap: this jump's upward
        magnitude is already bounded by the staircase's own geometry (the settled
        read that triggers it sits at or above ``pre_window``, so the gap to
        ``window_target`` cannot exceed ``window_target - pre_window``), and
        ``_set`` clamps it against the dynamic cap either way. The jump can be up
        (amp quiet) or DOWN (amp loud); going down is always cap-safe.
        """
        gain = observed_mic_dbfs - d.current_main_volume_db
        target = d.current_main_volume_db + capped_gap_step_db(
            measured_db=observed_mic_dbfs, target_db=cfg.window_target
        )
        safe = self._safe_target(target)
        v.jumps_used += 1
        log_event(
            logger,
            "ramp_settle_jump",
            session=self.session_id,
            jump=v.jumps_used,
            observed_mic_dbfs=f"{observed_mic_dbfs:.1f}",
            gain_map_db=f"{gain:.1f}",
            target_db=f"{target:.1f}",
            applied_db=f"{safe:.1f}",
        )
        if abs(safe - d.current_main_volume_db) > 1e-9:
            await _set(target)
        self._enter_confirming(v)

    def _process_batch(
        self, d: RampData, batch: list[LevelSample]
    ) -> list[LevelSample]:
        """Fold a phone batch into ramp state; return the *trusted* samples.

        A ``clip=true`` sample flips the state to ABORTED immediately -- clip is
        checked on EVERY sample before any other gate, so clip protection holds
        even for AGC-compressed or ambient-dominated readings. A non-finite level
        is dropped, as is a sample below ``noise_floor + trust_margin``.
        ``agc_frozen=false`` is recorded so the adapter can degrade and disable
        drift; the ramp still runs, but an AGC-compressed level is never a
        trusted gain-map reference. The one exception is ``agc_frozen=false``
        PAIRED with ``agc_unattested=true``, admitted through the SAME gates as
        an attested sample because the empirical slope check in ``run()`` decides
        its trustworthiness. A sample claiming BOTH flags true is treated as
        fully attested.
        """
        cfg = self.config
        trusted: list[LevelSample] = []
        floor = d.noise_floor_dbfs
        for s in batch:
            d.observed_sample_count += 1
            finite = math.isfinite(s.rms_dbfs) and math.isfinite(s.peak_dbfs)
            if finite:
                d.finite_sample_count += 1
                d.max_observed_rms_dbfs = (
                    s.rms_dbfs
                    if d.max_observed_rms_dbfs is None
                    else max(d.max_observed_rms_dbfs, s.rms_dbfs)
                )
                d.max_observed_peak_dbfs = (
                    s.peak_dbfs
                    if d.max_observed_peak_dbfs is None
                    else max(d.max_observed_peak_dbfs, s.peak_dbfs)
                )
                if floor is not None:
                    margin = s.rms_dbfs - floor
                    d.max_signal_over_noise_db = (
                        margin
                        if d.max_signal_over_noise_db is None
                        else max(d.max_signal_over_noise_db, margin)
                    )
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
            if not finite:
                d.nonfinite_sample_count += 1
                continue  # hostile/broken payload; liveness only, never trusted
            if not s.agc_frozen:
                if s.agc_unattested:
                    # Not proven AGC-on -- eligible for the slope check, so it
                    # falls through to the SAME admission gates an attested
                    # sample gets.
                    d.agc_unattested = True
                else:
                    d.agc_frozen = False
                    d.agc_rejected_sample_count += 1
                    # AGC-compressed: a liveness signal, never a trusted level.
                    continue
            if floor is not None and s.rms_dbfs < floor + cfg.trust_margin_db:
                d.below_noise_sample_count += 1
                continue  # ambient-dominated; not trustable
            trusted.append(s)
        return trusted
