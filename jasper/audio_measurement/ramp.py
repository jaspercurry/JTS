# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Level-match wire schema and ramp tuning config (shared measurement kernel).

The analog amplifier's gain is unknown; JTS controls only the digital
``main_volume``. The whole chain is LTI and ``main_volume`` adds in dB, so

    mic_dbfs(v) = v + G          (G = amp + room + mic path gain, unknown)

and one trusted, settled reading fixes ``G`` (the line's slope is a known
``1``). :class:`LevelSample` is the phone-reported wire sample and
:class:`MeasurementRamp` is the validated tuning config a ramp engine
consumes; both are shared between :mod:`jasper.active_speaker.seat_level_ramp`
(the live engine) and :mod:`jasper.active_speaker.crossover_level_run`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

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
# (≤2 Hz batches behind the ~0.75 s status poll). Budgets the derived safety
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


@dataclass(frozen=True)
class LevelSample:
    """One phone-reported mic-level sample.

    Batched, client-timestamped sample arrays ride the last-write-wins
    ``event`` slot, so the Pi's ~0.75 s poll never decimates the series.
    ``rms_dbfs`` / ``peak_dbfs`` are computed on the phone the same way the Pi's
    ``quality._dbfs`` computes them; ``clip`` marks a full-scale sample
    (immediate abort). ``agc_frozen`` is the phone's realized
    ``autoGainControl:false`` state, and ``False`` means the browser either
    reported AGC on or never reports the setting at all (every WebKit build).
    ``agc_unattested`` disambiguates those two: ``True`` means the browser could
    not attest either way, so the sample needs empirical verification before it
    is trusted as a gain-map reference; ``False`` means AGC was affirmatively
    reported on, so the level must never be a gain-map reference.
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
