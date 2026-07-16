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
and the volume that puts the mic anywhere in the window is exact. An earlier
draft reused ``capture_relay.alignment.cross_correlation_alignment`` to recover
the transport delay ``τ``; that estimator is waveform-domain and structurally
near-degenerate on a monotonic ramp envelope. The replacement never estimates
``τ`` — it *waits out* the modeled worst-case loop latency after every volume
change and reads only samples that postdate it.

How the settle read works under the real transport (the adversarial-panel fix —
the phone posts batched samples at ≤2 Hz behind a ~0.75 s relay poll, so most
kernel ticks deliver nothing):

  * every volume change stamps ``blank_until = now + max_loop_latency_s``;
    samples arriving before that reflect a stale (pre-change) level and are
    excluded from settle/confirm decisions (clip detection is NEVER blanked);
  * SETTLING accumulates post-blank trusted samples into a hold buffer and
    completes only when the hold has elapsed AND the buffer holds at least
    ``settle_min_samples`` — a momentarily-empty tick *extends* the hold; the
    machine never bounces back to CLIMBING (the review-reproduced bounce
    re-stepped the staircase past the window top);
  * the settled level is the **median of that buffered tail**; the jump aims at
    the **window midpoint** (symmetric ±half-window tolerance for gain-map
    noise — the *staircase* still stops-ahead below the window bottom via
    ``pre_window``); CONFIRMING then requires ``confirm_k`` consecutive
    post-blank in-window samples before locking;
  * if CONFIRMING instead sees ``confirm_k`` consecutive out-of-window samples
    all on one side, it recomputes the gain map from their median and takes a
    bounded corrective jump (total jumps ≤ ``max_jumps``).

Safety is the whole point. Every ramp-commanded volume passes the
``_safe_target`` choke point: ``<=`` the dynamic cap (the lower of
``original + bump`` and the absolute ceiling) AND
``<= 0 dB``, and a non-finite target raises instead of propagating (a hostile
relay post can never become ``set_main_volume_db(nan)``). The coarse staircase
stops at a pre-window set below the window bottom by the worst-case in-flight
overshoot — ``step_db + ramp_rate × max_loop_latency_s``, the step-quantization
term included. A ``clip=true`` sample is an immediate abort; readings that are
non-finite, AGC-compressed, or below ``noise_floor + trust_margin`` are never
trusted; a feed that goes silent aborts (a vanished phone also has no clip
protection). At the cap, the kernel normally returns ``MAXED_OUT`` and restores.
The sole exception is an explicitly labeled ``bounded_low_level`` lock after a
fresh post-latency tail proves a known noise floor, the existing SNR trust
margin, frozen AGC, no clipping, live delivery, consecutive samples, and
bounded spread; it never claims the normal target window was reached. A phone
that never produced a usable sample is an ERROR, not a low-level verdict. The
safety timeout is derived from the config's own worst-case walk; and every stop
fades down before the tone is killed. Restoring the user's own pre-ramp volume
is exempt from the dynamic cap (it is not a ramp command) and honors only the
0 dB hard ceiling.

Tone contract: ``play_continuous_tone`` must play until ``cancel_tone()`` is
called (the ``correction.playback.TonePlayer.play`` shape). The kernel runs it
as a task; if it finishes while the ramp is still working (WAV too short,
player crash) the ramp ends in ERROR and restores — a silent tone must never
blind-climb.

The controller stays **pure and synthetically testable**: inject a fake clock,
a fake volume setter, and a fake mic-sample source (:class:`LevelSample`
batches on any schedule, including the relay's real sparse cadence). No
CamillaDSP, no ``aplay``, no relay.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import statistics
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from jasper.log_event import log_event

logger = logging.getLogger(__name__)

# Level-event schema version. Bump when :class:`LevelSample`'s shape changes so a
# stale phone payload is detectable rather than silently misread. Pinned by
# tests/test_audio_measurement_ramp.py and mirrored by
# capture-page/js/level-events.js.
LEVEL_EVENT_SCHEMA_VERSION = 1

# The digital-full-scale hard ceiling: main_volume must never exceed this,
# independent of the dynamic cap. Mirrors camilla.py::_coerce_main_volume_db,
# duplicated here as defense-in-depth so the kernel is safe even if a caller
# forgets to clamp. Do not raise.
HARD_CEILING_DBFS = 0.0

# Fixed/listening-position measurements use the shared -12 dBFS stimulus at
# roughly one metre. This policy permits up to 15 dB above the household entry
# volume while retaining the digital-full-scale ceiling and live clipping abort.
# Near-field measurement deliberately keeps MeasurementRamp's tighter default.
LISTENING_POSITION_CAP_BUMP_DB = 15.0
LISTENING_POSITION_CAP_CEIL_DB = HARD_CEILING_DBFS

# Worst-case expected gap between consecutive phone samples reaching the kernel
# (≤2 Hz phone batches behind the relay's ~0.75 s poll). Used only to budget the
# derived safety timeout — not a gate.
SAMPLE_BUDGET_S = 1.5

# The exception set the ramp treats as recoverable-by-restore. Deliberately a
# broad-but-named tuple rather than a blind ``except Exception`` (lint contract:
# no new BLE001 suppressions): it covers every realistic failure of the injected
# callables (network/OS/timeout errors, protocol value errors, buggy-feed
# type/attribute/lookup errors, math-domain errors) while letting
# CancelledError / SystemExit / MemoryError propagate — the caller's
# restore-listening-volume hook is the backstop for those.
RECOVERABLE_ERRORS = (
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    AttributeError,
    LookupError,
    ArithmeticError,
)


# --- env-knob helpers ---------------------------------------------------------
#
# Every threshold whose true value is hardware-gated is a deploy-time knob
# (H1 supplies the real numbers on-device — the defaults here are conservative
# placeholders, NOT empirically derived), mirroring the
# JASPER_CAPTURE_ALIGNMENT_THRESHOLD pattern in capture_relay/alignment.py. Set
# them in jasper.env once measured; no rebuild required. Out-of-range or
# unparseable values fall back to the documented default; a *combination* of
# individually-valid values that fails cross-field validation also falls back
# as a whole (see :meth:`MeasurementRamp.from_env`) — a jasper.env edit can
# never brick the ramp at construction time.


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
    trusted reading crosses the pre-window the volume freezes and ``SETTLING``
    buffers post-latency samples until the settled read is trustworthy; a
    computed jump lands the window midpoint; ``CONFIRMING`` requires
    ``confirm_k`` consecutive in-window samples before the lock is trusted.
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

    ``BOUNDED_LOW_LEVEL`` is deliberately distinct from the ordinary
    in-window lock: it records that the hard/dynamic gain bound was honored and
    the live mic evidence was trustworthy and stable, but the measured level
    still fell short of the preferred window.  Downstream code may proceed
    while preserving that degraded evidence instead of pretending the normal
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

    Batched, client-timestamped sample arrays (not singular events) ride the
    relay's last-write-wins ``event`` slot, so the Pi's ~0.75 s poll never
    decimates the series. ``rms_dbfs`` / ``peak_dbfs`` are computed on the phone
    the same way the Pi's ``quality._dbfs`` computes them. ``clip`` marks a
    full-scale sample (immediate abort). ``agc_frozen`` is the phone's realized
    ``autoGainControl:false`` state — ``False`` means either the browser left
    AGC on (explicitly reported ``true``) or never reports the setting at all
    (``undefined`` — every WebKit build; ``getSettings()`` simply omits the
    key). ``agc_unattested`` disambiguates the two ``agc_frozen=False`` cases:
    ``True`` means the browser could not attest either way (the iOS/Safari
    shape) and the sample is eligible for the empirical slope verification in
    :class:`RampController` instead of being auto-rejected; ``False`` (the
    default) is the original meaning — the browser affirmatively reported AGC
    on, so the level is AGC-compressed and must never be trusted as a gain-map
    reference. Encoding it this way (never bare ``agc_frozen=True`` for an
    unattested chain) means an older Pi that has not learned about
    ``agc_unattested`` still falls back to the pre-existing "never trust"
    behavior instead of silently trusting an unproven chain — see
    ``docs/HANDOFF-correction.md`` "Level-match ramp".
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
        raises ``ValueError`` so a hostile relay post can never smuggle NaN into
        the gain map — callers treat a raising sample as malformed and drop it.
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
    """The ramp's tuning knobs — one self-describing, validated config.

    Constructing an instance whose overshoot invariant would be violated raises
    ``ValueError``, so a config that *could* drive the mic past the window can
    never be built. All bounds are dBFS ``main_volume``; all durations seconds.

    Overshoot invariant (validated in ``__post_init__``), including the
    step-quantization term — the first trusted crossing can already sit a full
    step above the pre-window before any transport latency is added:

        step_db + ramp_rate * max_loop_latency < 0.5 * window_width

    with ``ramp_rate = step_db / step_interval_s``. Because the coarse staircase
    stops at ``pre_window`` (set below ``window_low_dbfs`` by at least that
    worst-case in-flight overshoot), the staircase never climbs into the window;
    the sole approach into the window is a computed jump from a settled read.
    """

    # Target window. The coarse staircase stops-ahead BELOW the bottom (the
    # pre_window); the settled JUMP aims at the window MIDPOINT — a deliberate
    # refinement of §3.1(d)'s "aim at the bottom": aim-bottom exists to keep a
    # *moving* ramp from eating the window under loop latency, which does not
    # apply to a one-shot jump computed from a frozen-volume settled read.
    # Mid-window gives the jump symmetric ±half-window tolerance to gain-map
    # noise instead of parking half the noise distribution below the window.
    window_low_dbfs: float = -20.0
    window_high_dbfs: float = -12.0

    # Trust floor: a reading is only trustable once it clears
    # noise_floor + trust_margin_db (§3.1 (a)). Below that the RMS is
    # ambient-dominated and the early ramp shape is meaningless.
    trust_margin_db: float = 10.0

    # Consecutive in-window trusted samples required before the level is treated
    # as trustworthy → lock (§3.1 settle-based mapping, k >= 3).
    confirm_k: int = 3

    # Coarse staircase. step/interval chosen so the overshoot invariant holds
    # with margin at the default 2 s loop latency: 0.75 + 1.5*2.0 = 3.75 < 4.0.
    start_db: float = -50.0
    step_db: float = 0.75
    step_interval_s: float = 0.5

    # Hold at least this long after the pre-window crossing before the settled
    # read may complete; the read additionally requires settle_min_samples
    # post-latency samples, extending the hold on a sparse feed (never bouncing
    # back to CLIMBING).
    settle_hold_s: float = 2.0
    max_loop_latency_s: float = 2.0
    # Minimum post-latency samples in the settle buffer before the median is
    # trusted (one sample is too noise-prone to aim a jump with).
    settle_min_samples: int = 3

    # Total jump budget: the initial settle jump plus at most one corrective
    # re-jump from CONFIRMING evidence.
    max_jumps: int = 2

    # At the hard/dynamic cap, a below-window result may be accepted only as an
    # explicitly degraded bounded-low lock.  The final ``confirm_k`` trusted,
    # post-latency samples must fit inside this peak-to-peak spread.  This is a
    # stability policy, not permission to weaken the existing noise-floor,
    # AGC, clipping, liveness, cap, or timeout guards.
    allow_bounded_low_level: bool = False
    bounded_low_max_spread_db: float = 1.5
    bounded_low_max_shortfall_db: float = 20.0

    # Empirical AGC verification for an unattested chain (no browser attestation
    # either way — every WebKit build). Regress reported rms_dbfs against the
    # ramp's own commanded main_volume_db (both dB, so a gain-stable chain has
    # slope 1): a time-varying AGC gain flattens the reported response toward
    # the staircase (slope << 1), so a slope at/near 1 across several distinct
    # commanded levels is direct evidence the WHOLE mic->USB->OS->browser chain
    # held its gain fixed while climbing — stronger than the (frequently
    # unavailable) browser flag, and it works identically on iOS and Android.
    # ``agc_slope_min_steps`` — regression needs evidence spread across at
    # least this many distinct commanded-volume steps before a verdict is
    # trusted; fewer is INDETERMINATE, never auto-passed. ``agc_slope_threshold``
    # — recommended default 0.7: leaves headroom for real reading jitter (a
    # perfectly linear chain still won't measure exactly 1.0) while staying far
    # enough above a materially AGC'd chain's flattened response (an aggressive
    # AGC compresses the staircase toward 0.1-0.3) that the two regimes don't
    # overlap. H1 supplies a hardware-measured number; this is a placeholder.
    agc_slope_min_steps: int = 3
    agc_slope_threshold: float = 0.7

    # Feed liveness: if NO samples at all (trusted or not) arrive for this long
    # after the tone starts, the phone is gone — abort and restore (a vanished
    # phone also has no clip protection).
    feed_timeout_s: float = 8.0

    # Safety timeout. None (the default) derives it from the config's own
    # worst-case walk — see the `safety_timeout` property — so a quiet amp
    # reaches MAXED_OUT before the timeout instead of dying as a generic
    # CANCELLED. An explicit value is honored verbatim (tests).
    safety_timeout_s: float | None = None

    # Graceful fade-before-tone-kill (preserved from AutolevelController).
    fade_down_to_db: float = -50.0
    fade_step_db: float = 2.0
    fade_step_s: float = 0.03

    # Dynamic cap: the lower of original + bump and the absolute ceiling. This
    # is the OPERATIVE ceiling — tighter than HARD_CEILING_DBFS. There is no
    # floor: flooring a quiet listener's cap upward can turn a promised +12 dB
    # maximum rise into a much larger, unsafe jump.
    cap_bump_db: float = 12.0
    cap_ceil_db: float = -3.0

    # Derived pre-window: the coarse staircase stops here. Defaulted from the
    # window bottom minus the worst-case in-flight overshoot in __post_init__
    # when left as None so the staircase provably never climbs into the window.
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

    @property
    def window_target(self) -> float:
        """Where the settled jump aims: the window midpoint (see field notes)."""
        return 0.5 * (self.window_low_dbfs + self.window_high_dbfs)

    @property
    def safety_timeout(self) -> float:
        """The effective safety timeout.

        Explicit ``safety_timeout_s`` wins. Otherwise derived from the config's
        own worst-case walk — the full climb to the loosest cap, one settle
        (hold + latency + min-samples at the worst sample cadence), the jump
        budget's confirm phases, and a fixed margin — so the timeout is a true
        backstop rather than a bound the staircase itself exceeds (the review's
        quiet-amp CANCELLED-instead-of-MAXED_OUT failure at the old fixed 25 s).
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

        The cap is always ``<= original + bump`` and ``<= cap_ceil_db``.  The
        old ``max(cap_floor_db, ...)`` formula violated the first invariant for
        quiet listening levels (for example, ``-45 + 12`` became ``-20``).
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

        Explicit ``overrides`` win over env (tests pass exact values); anything
        not overridden and not in the env falls back to the documented default.
        Mirrors ``alignment._env_threshold``: out-of-range / unparseable env
        values are ignored. Additionally, when individually-valid env values
        fail *cross-field* validation (a window-low knob raised above the
        default window-high, a settle hold below the loop latency, a latency
        that breaks the overshoot invariant), the env set is dropped as a whole
        with a warning and the defaults (plus any explicit overrides) are used
        — a jasper.env edit can never brick the ramp at construction time.
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
            # Env floor is 2, not 1: the spec pins k >= 3 as the default; a
            # deploy knob may trade one confirmation for speed, but a single
            # sample is never "consecutive confirmation" (owned weakening,
            # documented in .env.example).
            "confirm_k": _env_int("JASPER_RAMP_CONFIRM_K", cls.confirm_k, lo=2, hi=20),
            "settle_min_samples": _env_int(
                "JASPER_RAMP_SETTLE_MIN_SAMPLES",
                cls.settle_min_samples,
                lo=1,
                hi=10,
            ),
            "feed_timeout_s": _env_float(
                "JASPER_RAMP_FEED_TIMEOUT_S", cls.feed_timeout_s, lo=2.0, hi=60.0
            ),
            "cap_bump_db": _env_float(
                "JASPER_RAMP_CAP_BUMP_DB", cls.cap_bump_db, lo=0.0, hi=24.0
            ),
            "cap_ceil_db": _env_float(
                "JASPER_RAMP_CAP_CEIL_DB", cls.cap_ceil_db, lo=-30.0, hi=0.0
            ),
            "agc_slope_min_steps": _env_int(
                "JASPER_RAMP_AGC_SLOPE_MIN_STEPS",
                cls.agc_slope_min_steps,
                lo=2,
                hi=10,
            ),
            "agc_slope_threshold": _env_float(
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
    window_shortfall_db: float | None = None
    settled_spread_db: float | None = None
    noise_floor_dbfs: float | None = None
    trust_margin_db: float | None = None
    agc_frozen: bool = True
    # True once any admitted sample carried agc_unattested=true (the browser
    # never reported autoGainControl either way — every WebKit build). Gates
    # the empirical slope verification below; irrelevant (stays False) for a
    # browser-attested or explicitly-AGC-on run.
    agc_unattested: bool = False
    # None = not yet decided (insufficient distinct commanded-level evidence).
    # True = the staircase's reported-vs-commanded slope cleared the threshold
    # — the chain is empirically gain-stable. False = it did not — the run
    # aborts (agc_suspected) the moment this is set.
    agc_verified: bool | None = None
    # The most recently computed regression slope, for diagnostics/logging.
    agc_slope: float | None = None
    # Count of trusted samples ever accepted. Reaching the cap may begin a
    # bounded-low evidence hold only when this is nonzero; a phone that never
    # produced a usable sample is an ERROR, not an acoustic diagnosis.
    trusted_sample_count: int = 0
    # Admission diagnostics for the full, fresh phone stream.  These counters
    # explain *why* a live meter produced zero trusted samples without retaining
    # the high-rate payload or weakening the clip/AGC/noise gates.
    observed_sample_count: int = 0
    finite_sample_count: int = 0
    below_noise_sample_count: int = 0
    agc_rejected_sample_count: int = 0
    nonfinite_sample_count: int = 0
    max_observed_rms_dbfs: float | None = None
    max_observed_peak_dbfs: float | None = None
    max_signal_over_noise_db: float | None = None
    error: str | None = None
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

        For an ordinary (non-unattested) run this is exactly ``agc_frozen`` —
        the raw browser-attested flag — so an attested run's behavior is
        byte-identical to before this property existed. For an unattested run
        (``agc_unattested`` True) it is the *empirical* verdict instead:
        ``agc_verified is True`` once the slope check passed, never the raw
        ``agc_frozen`` (which stays False for an unattested run at the wire
        level, by design — see :class:`LevelSample`, and the mixed
        old-Pi/new-page safety note in ``docs/HANDOFF-correction.md``). Every
        downstream consumer that used to gate on ``agc_frozen`` as "is this
        reference trustworthy" (:meth:`RampController._bounded_low_level_is_usable`,
        :meth:`jasper.correction.level_match.MeasurementLevelLock.from_ramp`)
        reads this property instead, so a verified-unattested lock behaves
        identically to an attested one.
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
            "restored": self.restored,
        }


# A source of the next batch of phone-reported samples. Injected so the loop is
# testable with a synthetic feed; the real feed polls the relay (rate-limited
# feed-side — the kernel tick is NOT the HTTP cadence). Returning an empty list
# means "no new samples this tick" (the loop keeps its clock running).
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
    # True only when SETTLING was entered because the safe cap was reached
    # below the pre-window.  It routes the stable evidence to the explicitly
    # degraded bounded-low policy instead of fabricating a normal window lock.
    bounded_low_candidate: bool = False
    # (commanded_main_volume_db, reported_rms_dbfs) pairs collected from an
    # unattested chain's trusted samples (raw, not blank_until-gated — see the
    # rationale where this is populated in run()), for the empirical AGC slope
    # check. Unused (stays empty) for an attested/explicitly-AGC-on run.
    agc_evidence: list[tuple[float, float]] = field(default_factory=list)


def _ols_slope(points: list[tuple[float, float]]) -> float | None:
    """Ordinary-least-squares slope of ``y`` (rms_dbfs) against ``x`` (commanded
    dB). ``None`` when there are too few points or the x-values are degenerate
    (all equal — a vertical/undefined fit), so a caller can distinguish "not
    enough evidence yet" from a real, low, computed slope."""
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
    session's adapter can swap the engine without changing its callers. The
    engine is what changed: blind browser-locked ramp → Pi-side settle-based
    two-point map with a stop-ahead window and clip/trust/liveness/timeout
    safety.
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

    # -- listening-level restore (idempotent, best-effort) --

    @property
    def main_volume_setter(self) -> VolumeSetter | None:
        return self._main_volume_setter

    @main_volume_setter.setter
    def main_volume_setter(self, setter: VolumeSetter | None) -> None:
        self._main_volume_setter = setter

    async def restore_listening_volume_if_ramped(self) -> None:
        """Restore main_volume when a measurement ends outside apply/reset.

        A LOCKED ramp leaves main_volume at the measurement level for the whole
        measurement; failed / verify-ended measurements skip the web apply/reset
        handlers, so this best-effort hook restores the user's level there. A
        MAXED_OUT run already attempts an immediate restore, but remains eligible
        here as a retry. Idempotent; swallows recoverable errors. Mirrors
        ``AutolevelController.restore_listening_volume_if_ramped``.
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

    # -- volume choke points --

    def _cap_value(self) -> float:
        """The effective ramp ceiling: min(dynamic cap, hard 0 dB ceiling)."""
        cap = self.data.cap_db
        return HARD_CEILING_DBFS if cap is None else min(cap, HARD_CEILING_DBFS)

    def _safe_target(self, desired_db: float) -> float:
        """Clamp a desired ramp volume to the operative cap AND the hard ceiling.

        The single choke point every ramp-commanded volume passes through, so
        the cap-safety invariant is one line, not scattered. Never returns a
        value above ``min(dynamic_cap, HARD_CEILING_DBFS)`` — and never returns
        a non-finite value: NaN would tunnel through ``min()``, so it raises
        instead (the run loop's recoverable-error path fades and restores).
        """
        if not math.isfinite(desired_db):
            raise ValueError(f"non-finite ramp volume target: {desired_db!r}")
        return min(desired_db, self._cap_value())

    def _at_cap(self) -> bool:
        return self.data.current_main_volume_db >= self._cap_value() - 1e-9

    # -- the ramp loop --

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
            phone measures. Contract: the coroutine plays until ``cancel_tone()``
            is called (the ``TonePlayer.play`` shape); the kernel runs it as a
            task and treats an early finish as an error (a silent tone must not
            blind-climb). Started AFTER the quiet start volume is set; killed
            AFTER the fade-down (audio-safety order).
          - ``next_samples``: awaited each tick for the phone's newest batch.
          - ``noise_floor_dbfs``: the phone's pre-ramp noise floor (trust gate).
            A non-finite value is treated as unknown (no trust floor) with a
            warning, never as a gate that silently passes everything.
          - ``clock`` / ``sleep``: injected monotonic time so tests drive cadence.
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

            Preserves AutolevelController's click-free stop: never kill the tone
            at a loud level. ``final_db`` is clamped only to the 0 dB HARD
            ceiling — NOT the dynamic cap — because the finals that arrive here
            are either a lock value already emitted through ``_set`` (≤ cap by
            construction) or the user's own pre-ramp volume, and "restoring" a
            −5 dB listener to the −6 dB measurement cap would be a regression,
            not safety (panel: cap-clamped restore).
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
            event: str | None = None,
            level: int = logging.INFO,
            **event_fields: Any,
        ) -> RampData:
            d.state = state
            if error is not None:
                d.error = error
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
                # --- terminal signals checked every tick ---
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
                    # A clip in the batch aborted immediately.
                    await _graceful_stop(d.original_main_volume_db)
                    return d
                d.trusted_sample_count += len(trusted)
                # Samples arriving before blank_until reflect a pre-change
                # level; exclude them from settle/confirm decisions. (Clip
                # detection already ran on the FULL batch above.)
                settled_stream = (
                    [s.rms_dbfs for s in trusted] if now >= v.blank_until else []
                )

                if d.agc_unattested and d.agc_verified is None and trusted:
                    # Deliberately NOT settled_stream: with the default step
                    # cadence (0.5 s) much faster than max_loop_latency_s
                    # (2 s), blank_until never clears mid-climb, so a
                    # blank-gated stream would starve the regression of the
                    # very staircase evidence it needs. Like the CLIMBING
                    # pre-window crossing check above, raw trusted samples are
                    # used — a rough linearity trend across many steps and
                    # points tolerates the same per-sample lag/smear that
                    # crossing detection already tolerates, and OLS averages
                    # it out.
                    self._update_agc_evidence(
                        d, v, cfg, [s.rms_dbfs for s in trusted]
                    )
                    if d.agc_verified is False:
                        # Explicit slope failure: abort NOW, at whatever
                        # (usually still-quiet) commanded level the staircase
                        # has reached — never keep climbing an AGC-suspected
                        # chain toward the window.
                        return await _terminal(
                            RampState.ERROR,
                            final_db=d.original_main_volume_db,
                            error="agc_suspected",
                            event="ramp_agc_suspected",
                            level=logging.WARNING,
                            slope=(
                                f"{d.agc_slope:.3f}" if d.agc_slope is not None else ""
                            ),
                            steps=len({round(x, 3) for x, _ in v.agc_evidence}),
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
            # Freeze the moment ANY trusted reading crosses the pre-window —
            # max over the batch, not the newest sample, so a mid-batch
            # crossing whose newest sample dipped doesn't delay the freeze.
            # Stale (pre-blank) samples are deliberately included here: a stale
            # crossing means the true level is even higher, so freezing is MORE
            # urgent — the conservative direction.
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
                        # evidence. A bounded-low lock is possible only after
                        # the same settle + confirmation discipline as the
                        # normal path; historical climb samples are not enough.
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
                    # Already pinned at the allowed cap: do not manufacture a
                    # jump or an in-window lock. Confirm a fresh stable tail and
                    # label the result explicitly if the bounded policy passes.
                    self._enter_confirming(v)
                    d.state = RampState.CONFIRMING
                else:
                    await self._apply_jump(d, v, cfg, settled, _set)
                    d.state = RampState.CONFIRMING
            # else: keep holding — a momentarily-empty feed EXTENDS the hold;
            # the machine never bounces back to CLIMBING (the review-reproduced
            # bounce ratcheted the staircase past the window top).

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
                    # A slope FAILURE already aborted earlier in run() — the
                    # only way to reach a would-be lock here with agc_trusted
                    # False is an indeterminate verdict (never enough distinct
                    # commanded-level evidence, e.g. the window sat close to
                    # the pre-ramp start). Fail closed to the honest error
                    # rather than lock on unproven gain stability.
                    return await _terminal(
                        RampState.ERROR,
                        final_db=d.original_main_volume_db,
                        error="agc_suspected",
                        event="ramp_agc_suspected",
                        level=logging.WARNING,
                        reason="insufficient_slope_evidence",
                        steps=len({round(x, 3) for x, _ in v.agc_evidence}),
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
                    # The cap was genuinely reached, but the bounded-low
                    # contract was not proven. Preserve the evidence and
                    # restore; never masquerade as a normal lock.
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
                # Jump budget exhausted and still out of window — keep
                # confirming until the timeout restores (fail-safe), rather
                # than oscillating.
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
        Appends ``(commanded_db, rms)`` pairs at the CURRENT commanded level
        (stable for this tick — only ``_set`` mutates it), then, once evidence
        spans at least ``agc_slope_min_steps`` distinct commanded levels,
        regresses reported rms against commanded dB and sets ``agc_verified``.
        Leaves it ``None`` (indeterminate) when there still aren't enough
        distinct steps — the caller in ``run()`` treats a ``False`` verdict as
        an immediate abort; ``None`` at lock time is handled separately (see
        ``_tick_state`` and ``_bounded_low_level_is_usable``).
        """
        for rms in rms_values:
            v.agc_evidence.append((d.current_main_volume_db, rms))
        steps = {round(x, 3) for x, _ in v.agc_evidence}
        if len(steps) < cfg.agc_slope_min_steps:
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
        else:
            d.agc_verified = False
            log_event(
                logger,
                "ramp_agc_suspected",
                level=logging.WARNING,
                session=self.session_id,
                slope=f"{slope:.3f}",
                steps=len(steps),
                at_db=f"{d.current_main_volume_db:.1f}",
            )

    @staticmethod
    def _bounded_low_level_is_usable(
        d: RampData,
        cfg: MeasurementRamp,
    ) -> bool:
        """Whether cap evidence satisfies the degraded lock contract.

        Uses ``agc_trusted``, not the raw ``agc_frozen`` flag: an attested run
        behaves exactly as before (``agc_trusted == agc_frozen`` when
        ``agc_unattested`` is False). An unattested run reaching the cap with
        too few distinct staircase steps to reach an AGC verdict (a driver
        capped early, e.g. a tweeter ramp with a small pre-cap window) is
        INDETERMINATE (``agc_verified is None``) — ``agc_trusted`` is False in
        that case too, so it fails closed to the ordinary
        ``bounded_low_evidence_insufficient`` MAXED_OUT path rather than
        manufacturing a degraded lock on unproven gain stability. A slope
        FAILURE never reaches here: it aborts immediately in ``run()``.
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

        The gain map's slope is 1, so the target is exact up to reading noise;
        the jump can be up (amp quiet) or DOWN (amp loud — the mic is already
        above the window even at the quiet floor). Going down is always
        cap-safe; going up is clamped by ``_set``. ``blank_until`` (stamped by
        ``_set``) excludes post-jump stale reports from the confirm stream.
        """
        gain = observed_mic_dbfs - d.current_main_volume_db
        target = cfg.window_target - gain
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

        A ``clip=true`` sample flips the state to ABORTED immediately (the caller
        stops) — clip is checked on EVERY sample before any other gate, so clip
        protection holds even for AGC-compressed or ambient-dominated readings.
        A non-finite level is dropped (never trusted — the NaN-pierce fix). A
        sample below ``noise_floor + trust_margin`` is dropped as
        ambient-dominated. ``agc_frozen=false`` on any sample is recorded so the
        adapter can degrade + disable drift (§3.1 (c)) — the ramp still runs (the
        user may manually lock; that is the degrade path), but an AGC-compressed
        level is never a trusted gain-map reference. The one exception is
        ``agc_frozen=false`` PAIRED with ``agc_unattested=true`` (the browser
        could not attest either way, not a proven AGC-on) — those samples are
        admitted through the SAME finite/clip/noise-floor gates as an attested
        sample, because whether they end up trustworthy is decided by the
        empirical slope check in ``run()``/``_update_agc_evidence``, not by an
        automatic reject here. A sample claiming BOTH ``agc_frozen=true`` and
        ``agc_unattested=true`` is treated as fully attested — the explicit
        attestation wins over the contradictory unattested hint.
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
                    # Not proven AGC-on — eligible for the slope check. Fall
                    # through to the SAME admission gates an attested sample
                    # gets; `run()` decides whether the accumulated evidence
                    # is actually trustworthy before any lock can use it.
                    d.agc_unattested = True
                else:
                    d.agc_frozen = False
                    d.agc_rejected_sample_count += 1
                    # AGC-compressed: usable as a liveness signal but never as
                    # a trusted level. Skip it from the trusted set.
                    continue
            if floor is not None and s.rms_dbfs < floor + cfg.trust_margin_db:
                d.below_noise_sample_count += 1
                continue  # ambient-dominated; not trustable
            trusted.append(s)
        return trusted
