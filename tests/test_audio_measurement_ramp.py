# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Synthetic tests for the settle-based level-match ramp kernel.

The controller is pure: a fake clock, a fake volume setter, and a fake mic-sample
source that models the chain gain ``G`` (``mic_dbfs = main_volume + G``) let us
drive the whole state machine deterministically — no CamillaDSP, no aplay, no
relay. Two fakes model two regimes:

  * :class:`ChainModel` — a dense per-tick feed (fast property tests for the
    cap/clip/trust invariants);
  * :class:`SparseChain` — the REAL transport shape the adversarial panel
    reproduced the blocker with: the phone measures at batch cadence, each
    reading reflects the volume ``meas_lag`` earlier, the batch reaches the Pi
    ``transport_lag`` after measurement, and each batch is delivered exactly
    once (the relay's last-write-wins slot + seq dedup). The panel's exact
    failing schedules (0.75 s cadence across phases; 0.5 s ± 30 ms jitter) are
    encoded below and MUST LOCK.

The safety proof the panel probes:

  * every ramp-commanded volume is ``<=`` the dynamic cap AND ``<= 0 dB``, and
    never non-finite (a NaN-poisoned relay batch cannot reach the setter);
  * stop-ahead + the settle jump never imply a mic level above ``window_high``;
  * ``clip=true`` aborts immediately with a fade; a vanished feed aborts;
  * ``maxed_out`` requires trusted evidence, never creates a measurement lock,
    restores the listening volume, and never exceeds the caps; a phone that
    never produced a usable sample is an ERROR that restores;
  * the safety timeout (derived from the config's own worst-case walk) always
    fires, and a quiet amp reaches MAXED_OUT *before* it;
  * cancel/timeout/clip restore the user's own volume EXACTLY (no cap clamp);
  * the tone-player contract (play-until-cancelled) is enforced.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import random

import pytest

from jasper.audio_measurement.ramp import (
    HARD_CEILING_DBFS,
    LEVEL_EVENT_SCHEMA_VERSION,
    LevelSample,
    MeasurementRamp,
    RampController,
    RampData,
    RampLockKind,
    RampState,
    _LoopVars,
)

# A fast test config: same shape as the defaults, shorter holds. Overshoot
# invariant: 0.75 + 1.5*0.5 = 1.5 < 4.0.
FAST = dict(settle_hold_s=0.5, max_loop_latency_s=0.5, settle_min_samples=2)


class FakeClock:
    """Deterministic monotonic clock advanced by the fake sleep."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        # Advance time by at least a tick so a `sleep(0.01)` loop makes progress,
        # and YIELD to the event loop so sibling tasks (the tone task) actually
        # run — a real sleep yields; the fake must too.
        self.t += max(seconds, 0.01)
        await asyncio.sleep(0)


class BlockingTone:
    """The tone-player contract: play() blocks until cancel() (TonePlayer shape)."""

    def __init__(self) -> None:
        self.started = False
        self.cancelled = False
        self._event = asyncio.Event()

    async def play(self) -> None:
        self.started = True
        # Real 20 s wall-clock backstop so a buggy test can't hang forever.
        try:
            await asyncio.wait_for(self._event.wait(), timeout=20.0)
        except asyncio.TimeoutError:
            pass

    def cancel(self) -> None:
        self.cancelled = True
        self._event.set()


class ChainModel:
    """Dense per-tick feed: mic_dbfs = commanded volume + gain_db, fresh sample
    every call. The fast regime for cap/clip/trust property tests.

    ``agc_unattested`` + ``agc_gain_fraction`` model the empirical-verification
    scenarios: an unattested chain reports ``rms = base + gain_db +
    agc_gain_fraction * (commanded - base)`` — ``agc_gain_fraction=1.0`` (the
    default) is a perfectly linear chain (slope 1); a fraction well below the
    ``agc_slope_threshold`` models AGC clawing back most of a commanded change
    (a flattened, AGC'd response), optionally with ``jitter_db`` reading noise
    layered on top (a real, noisy-but-linear chain still passes at threshold).
    """

    def __init__(
        self,
        *,
        gain_db: float,
        start_vol: float = -50.0,
        noise_floor_dbfs: float = -80.0,
        agc_frozen: bool = True,
        agc_unattested: bool = False,
        agc_gain_fraction: float = 1.0,
        jitter_db: float = 0.0,
        seed: int = 0,
        clip_at_vol: float | None = None,
        nan_every: int | None = None,
    ) -> None:
        self.gain_db = gain_db
        self.noise_floor_dbfs = noise_floor_dbfs
        self.agc_frozen = agc_frozen
        self.agc_unattested = agc_unattested
        self.agc_gain_fraction = agc_gain_fraction
        self.jitter_db = jitter_db
        self._rng = random.Random(seed)
        self.clip_at_vol = clip_at_vol
        self.nan_every = nan_every
        self._vol = start_vol
        self._base_vol = start_vol
        self._seq = 0
        self.commanded: list[float] = []

    async def set_vol(self, db: float) -> None:
        self._vol = db
        self.commanded.append(db)

    async def next_samples(self) -> list[LevelSample]:
        delta = self._vol - self._base_vol
        mic = self._base_vol + self.gain_db + delta * self.agc_gain_fraction
        if self.jitter_db:
            mic += self._rng.uniform(-self.jitter_db, self.jitter_db)
        clip = self.clip_at_vol is not None and self._vol >= self.clip_at_vol
        self._seq += 1
        rms = mic
        if self.nan_every and self._seq % self.nan_every == 0:
            rms = float("nan")
        return [
            LevelSample(
                seq=self._seq,
                t_client_ms=self._seq * 10,
                rms_dbfs=rms,
                peak_dbfs=(0.0 if clip else mic + 3.0),
                clip=clip,
                agc_frozen=self.agc_frozen,
                agc_unattested=self.agc_unattested,
            )
        ]


class SparseChain:
    """The REAL transport shape (the panel's repro): batches on a schedule.

    The phone measures the mic every ``batch_interval`` (± ``jitter_ms``), each
    reading reflecting the commanded volume ``meas_lag`` earlier; the batch
    arrives Pi-side ``transport_lag`` after measurement and is delivered by
    ``next_samples`` exactly once, on the first call at/after arrival.
    """

    def __init__(
        self,
        *,
        clock: FakeClock,
        gain_db: float,
        start_vol: float,
        noise_floor_dbfs: float = -80.0,
        batch_interval: float = 0.75,
        phase: float = 0.0,
        jitter_ms: float = 0.0,
        seed: int = 0,
        meas_lag: float = 0.05,
        transport_lag: float = 0.75,
        agc_frozen: bool = True,
        clip_at_mic: float | None = None,
        silent_after: float | None = None,
        gain_shift_after_jump: float = 0.0,
    ) -> None:
        self.clock = clock
        self.gain_db = gain_db
        self.noise_floor_dbfs = noise_floor_dbfs
        self.batch_interval = batch_interval
        self.jitter_ms = jitter_ms
        self.meas_lag = meas_lag
        self.transport_lag = transport_lag
        self.agc_frozen = agc_frozen
        self.clip_at_mic = clip_at_mic
        self.silent_after = silent_after
        self.gain_shift_after_jump = gain_shift_after_jump
        self._rng = random.Random(seed)
        self._next_meas = phase if phase > 0 else batch_interval
        self._seq = 0
        self._timeline: list[tuple[float, float]] = [(0.0, start_vol)]
        self.commanded: list[float] = []
        self._start_vol = start_vol

    async def set_vol(self, db: float) -> None:
        prev = self._timeline[-1][1]
        # A move bigger than 2 dB is a settle jump, not a staircase step; apply
        # the optional mid-run gain shift there (the corrective-jump scenario).
        if self.gain_shift_after_jump and abs(db - prev) > 2.0:
            self.gain_db += self.gain_shift_after_jump
            self.gain_shift_after_jump = 0.0
        self._timeline.append((self.clock.now(), db))
        self.commanded.append(db)

    def vol_at(self, t: float) -> float:
        vol = self._timeline[0][1]
        for when, value in self._timeline:
            if when <= t:
                vol = value
            else:
                break
        return vol

    async def next_samples(self) -> list[LevelSample]:
        now = self.clock.now()
        out: list[LevelSample] = []
        while True:
            jitter = (
                self._rng.uniform(-self.jitter_ms, self.jitter_ms) / 1000.0
                if self.jitter_ms
                else 0.0
            )
            meas_t = self._next_meas + jitter
            if meas_t + self.transport_lag > now:
                break
            self._next_meas += self.batch_interval
            if self.silent_after is not None and meas_t >= self.silent_after:
                continue  # phone vanished: batches stop arriving
            mic = self.vol_at(max(0.0, meas_t - self.meas_lag)) + self.gain_db
            clip = self.clip_at_mic is not None and mic >= self.clip_at_mic
            self._seq += 1
            out.append(
                LevelSample(
                    seq=self._seq,
                    t_client_ms=int(meas_t * 1000),
                    rms_dbfs=mic,
                    peak_dbfs=(0.0 if clip else mic + 3.0),
                    clip=clip,
                    agc_frozen=self.agc_frozen,
                )
            )
        return out


async def _run(
    chain,
    config: MeasurementRamp,
    *,
    clock: FakeClock | None = None,
    original: float | None = None,
    manual_lock_after: int | None = None,
    cancel_after: int | None = None,
    tone: BlockingTone | None = None,
):
    clock = clock or FakeClock()
    controller = RampController(session_id="test", config=config)
    tone = tone or BlockingTone()
    orig = original if original is not None else getattr(chain, "_start_vol", None)
    if orig is None:
        orig = chain._vol  # dense ChainModel starts at its volume

    async def get_vol() -> float:
        return orig

    counter = {"n": 0}
    base_next = chain.next_samples

    async def next_samples():
        counter["n"] += 1
        if manual_lock_after is not None and counter["n"] == manual_lock_after:
            await controller.lock()
        if cancel_after is not None and counter["n"] == cancel_after:
            await controller.cancel()
        return await base_next()

    data = await controller.run(
        get_main_volume_db=get_vol,
        set_main_volume_db=chain.set_vol,
        play_continuous_tone=tone.play,
        cancel_tone=tone.cancel,
        next_samples=next_samples,
        noise_floor_dbfs=chain.noise_floor_dbfs,
        clock=clock.now,
        sleep=clock.sleep,
    )
    return controller, data, tone


# --- config validation (the overshoot invariant can't be built violated) ------


def test_schema_version_pinned():
    assert LEVEL_EVENT_SCHEMA_VERSION == 1


def test_default_config_is_valid_and_pre_window_below_window():
    cfg = MeasurementRamp()
    assert cfg.pre_window <= cfg.window_low_dbfs
    # Overshoot invariant INCLUDING the step-quantization term (the review's
    # arithmetic fix): the first trusted crossing can sit a full step above the
    # pre-window before latency is added.
    overshoot = cfg.step_db + cfg.ramp_rate * cfg.max_loop_latency_s
    assert overshoot < 0.5 * (cfg.window_high_dbfs - cfg.window_low_dbfs)
    assert cfg.pre_window == pytest.approx(cfg.window_low_dbfs - overshoot)


def test_overshoot_invariant_rejects_a_too_fast_ramp():
    with pytest.raises(ValueError, match="overshoot guard"):
        MeasurementRamp(step_db=4.0, step_interval_s=0.5, max_loop_latency_s=2.0)


def test_overshoot_invariant_includes_step_quantization_term():
    # rate*latency alone = 0.8 < 4 (the OLD invariant would pass) but a huge
    # step means the crossing sample can already breach the window: rejected.
    with pytest.raises(ValueError, match="overshoot guard"):
        MeasurementRamp(step_db=8.0, step_interval_s=20.0, max_loop_latency_s=2.0)


def test_cap_ceil_cannot_exceed_hard_ceiling():
    with pytest.raises(ValueError, match="hard ceiling"):
        MeasurementRamp(cap_ceil_db=6.0)


@pytest.mark.parametrize("field", ["cap_bump_db", "cap_ceil_db"])
def test_dynamic_cap_inputs_must_be_finite(field):
    with pytest.raises(ValueError, match="must be finite"):
        MeasurementRamp(**{field: float("nan")})


def test_settle_hold_must_cover_loop_latency():
    with pytest.raises(ValueError, match="settle_hold_s"):
        MeasurementRamp(settle_hold_s=1.0, max_loop_latency_s=2.0)


def test_dynamic_cap_matches_relay_level_defaults():
    cfg = MeasurementRamp()
    # The cap may be limited by the absolute ceiling, but never floored upward
    # beyond original+bump.
    assert cfg.dynamic_cap(-20.0) == -8.0
    assert cfg.dynamic_cap(-15.2) == pytest.approx(-3.2)
    assert cfg.dynamic_cap(-10.0) == -3.0
    assert cfg.dynamic_cap(-5.0) == -3.0
    assert cfg.dynamic_cap(-45.0) == -33.0


@pytest.mark.parametrize(
    ("original", "bump", "ceiling"),
    [
        (-80.0, 6.0, -6.0),
        (-45.0, 6.0, -6.0),
        (-20.0, 6.0, -6.0),
        (-10.0, 6.0, -6.0),
        (-5.0, 6.0, -6.0),
        (-45.0, 3.0, -12.0),
    ],
)
def test_dynamic_cap_never_exceeds_bump_or_absolute_ceiling(original, bump, ceiling):
    cfg = MeasurementRamp(cap_bump_db=bump, cap_ceil_db=ceiling)
    cap = cfg.dynamic_cap(original)
    assert cap <= original + bump
    assert cap <= ceiling
    assert cap <= HARD_CEILING_DBFS


def test_safety_timeout_derived_from_worst_case_walk():
    cfg = MeasurementRamp()
    climb = (cfg.cap_ceil_db - cfg.start_db) / cfg.ramp_rate
    # The derived timeout must exceed the staircase's own worst-case climb —
    # the review's SF: a quiet amp must reach MAXED_OUT, not the timeout.
    assert cfg.safety_timeout > climb + cfg.settle_hold_s
    # Explicit values are honored verbatim.
    assert MeasurementRamp(safety_timeout_s=9.0).safety_timeout == 9.0


def test_from_env_cross_field_conflict_falls_back(monkeypatch):
    # Individually-valid values that conflict as a pair must not brick the
    # ramp: the env set is dropped with a warning.
    monkeypatch.setenv("JASPER_RAMP_WINDOW_LOW_DBFS", "-10")  # above default high
    cfg = MeasurementRamp.from_env()
    assert cfg.window_low_dbfs == MeasurementRamp.window_low_dbfs

    monkeypatch.delenv("JASPER_RAMP_WINDOW_LOW_DBFS")
    monkeypatch.setenv("JASPER_RAMP_SETTLE_HOLD_S", "1.0")  # < default latency 2.0
    cfg = MeasurementRamp.from_env()
    assert cfg.settle_hold_s == MeasurementRamp.settle_hold_s


def test_from_env_valid_values_apply(monkeypatch):
    monkeypatch.setenv("JASPER_RAMP_TRUST_MARGIN_DB", "14")
    monkeypatch.setenv("JASPER_RAMP_FEED_TIMEOUT_S", "12")
    monkeypatch.setenv("JASPER_RAMP_CAP_BUMP_DB", "9")
    monkeypatch.setenv("JASPER_RAMP_CAP_CEIL_DB", "-4")
    cfg = MeasurementRamp.from_env()
    assert cfg.trust_margin_db == 14.0
    assert cfg.feed_timeout_s == 12.0
    assert cfg.cap_bump_db == 9.0
    assert cfg.cap_ceil_db == -4.0


@pytest.mark.parametrize(
    ("key", "value", "field"),
    [
        ("JASPER_RAMP_CAP_BUMP_DB", "-0.1", "cap_bump_db"),
        ("JASPER_RAMP_CAP_BUMP_DB", "25", "cap_bump_db"),
        ("JASPER_RAMP_CAP_CEIL_DB", "-30.1", "cap_ceil_db"),
        ("JASPER_RAMP_CAP_CEIL_DB", "0.1", "cap_ceil_db"),
    ],
)
def test_from_env_cap_bounds_fall_back(key, value, field, monkeypatch):
    monkeypatch.setenv(key, value)
    cfg = MeasurementRamp.from_env()
    assert getattr(cfg, field) == getattr(MeasurementRamp, field)


def test_from_env_confirm_k_floor_is_two(monkeypatch):
    # The spec pins k>=3 as the default; the env knob may weaken to 2 but a
    # single sample is never "consecutive confirmation".
    monkeypatch.setenv("JASPER_RAMP_CONFIRM_K", "1")
    assert MeasurementRamp.from_env().confirm_k == MeasurementRamp.confirm_k
    monkeypatch.setenv("JASPER_RAMP_CONFIRM_K", "2")
    assert MeasurementRamp.from_env().confirm_k == 2


def test_bounded_low_stability_threshold_must_be_finite_and_nonnegative():
    with pytest.raises(ValueError, match="bounded_low_max_spread_db"):
        MeasurementRamp(bounded_low_max_spread_db=-0.1)
    with pytest.raises(ValueError, match="bounded_low_max_spread_db"):
        MeasurementRamp(bounded_low_max_spread_db=float("nan"))
    with pytest.raises(ValueError, match="bounded_low_max_shortfall_db"):
        MeasurementRamp(bounded_low_max_shortfall_db=0.0)
    with pytest.raises(ValueError, match="bounded_low_max_shortfall_db"):
        MeasurementRamp(bounded_low_max_shortfall_db=float("inf"))


# --- AGC empirical-slope-verification config ----------------------------------


def test_agc_slope_config_defaults_and_validation():
    cfg = MeasurementRamp()
    assert cfg.agc_slope_min_span_db == pytest.approx(6.0)
    assert cfg.agc_slope_min_steps == 3
    assert cfg.agc_slope_threshold == pytest.approx(0.7)
    with pytest.raises(ValueError, match="agc_slope_min_steps"):
        MeasurementRamp(agc_slope_min_steps=1)
    with pytest.raises(ValueError, match="agc_slope_min_span_db"):
        MeasurementRamp(agc_slope_min_span_db=0.0)
    with pytest.raises(ValueError, match="agc_slope_min_span_db"):
        MeasurementRamp(agc_slope_min_span_db=float("inf"))
    with pytest.raises(ValueError, match="agc_slope_threshold"):
        MeasurementRamp(agc_slope_threshold=0.0)
    with pytest.raises(ValueError, match="agc_slope_threshold"):
        MeasurementRamp(agc_slope_threshold=float("nan"))


def test_agc_slope_env_knobs_apply_and_fall_back(monkeypatch):
    monkeypatch.setenv("JASPER_RAMP_AGC_SLOPE_MIN_SPAN_DB", "8.0")
    monkeypatch.setenv("JASPER_RAMP_AGC_SLOPE_MIN_STEPS", "4")
    monkeypatch.setenv("JASPER_RAMP_AGC_SLOPE_THRESHOLD", "0.6")
    cfg = MeasurementRamp.from_env()
    assert cfg.agc_slope_min_span_db == pytest.approx(8.0)
    assert cfg.agc_slope_min_steps == 4
    assert cfg.agc_slope_threshold == pytest.approx(0.6)

    # Out-of-range values fall back to the documented default.
    monkeypatch.setenv("JASPER_RAMP_AGC_SLOPE_MIN_SPAN_DB", "0.5")
    monkeypatch.setenv("JASPER_RAMP_AGC_SLOPE_MIN_STEPS", "1")
    monkeypatch.setenv("JASPER_RAMP_AGC_SLOPE_THRESHOLD", "1.5")
    cfg = MeasurementRamp.from_env()
    assert cfg.agc_slope_min_span_db == MeasurementRamp.agc_slope_min_span_db
    assert cfg.agc_slope_min_steps == MeasurementRamp.agc_slope_min_steps
    assert cfg.agc_slope_threshold == MeasurementRamp.agc_slope_threshold


# --- AGC empirical slope verification (unattested chains) ---------------------
#
# An unattested browser (autoGainControl undefined/null — every WebKit build)
# no longer refuses client-side (capture-page/js/main.js). Instead the phone
# posts agc_frozen=false + agc_unattested=true, and the kernel verifies chain
# linearity empirically from the ramp's own staircase before trusting a lock.


@pytest.mark.asyncio
async def test_agc_attested_path_is_unaffected_by_slope_machinery():
    """Regression pin: an ordinary attested ChainModel run never sets
    agc_unattested/agc_verified, and agc_trusted collapses to the raw
    agc_frozen flag — the slope machinery is a no-op for this path."""
    cfg = MeasurementRamp(**FAST)
    chain = ChainModel(gain_db=10.0, start_vol=-30.0)  # agc_frozen=True default
    _, data, _ = await _run(chain, cfg, original=-30.0)
    assert data.state == RampState.LOCKED
    assert data.agc_unattested is False
    assert data.agc_verified is None
    assert data.agc_slope is None
    assert data.agc_trusted is data.agc_frozen is True


def test_agc_explicit_attestation_wins_over_contradictory_unattested_flag():
    """A sample claiming BOTH agc_frozen=true and agc_unattested=true (a
    malformed/hostile combination the wire format never intentionally
    produces) is treated as fully attested — the explicit attestation wins,
    never the contradictory hint."""
    cfg = MeasurementRamp()
    controller = RampController(session_id="t", config=cfg)
    data = RampData(current_main_volume_db=-30.0, noise_floor_dbfs=-80.0)
    batch = [
        LevelSample(
            seq=1, t_client_ms=0, rms_dbfs=-20.0, peak_dbfs=-17.0,
            agc_frozen=True, agc_unattested=True,
        )
    ]
    trusted = controller._process_batch(data, batch)
    assert len(trusted) == 1
    assert data.agc_unattested is False  # never set — agc_frozen=True short-circuits
    assert data.agc_frozen is True


@pytest.mark.asyncio
async def test_agc_unattested_linear_chain_is_verified_and_locks():
    cfg = MeasurementRamp(**FAST)
    chain = ChainModel(
        gain_db=10.0,
        start_vol=-30.0,
        agc_frozen=False,
        agc_unattested=True,
        agc_gain_fraction=1.0,  # a perfectly gain-stable chain, slope == 1
    )
    controller, data, tone = await _run(chain, cfg, original=-30.0)
    assert data.state == RampState.LOCKED
    assert data.lock_kind is RampLockKind.IN_WINDOW
    assert data.agc_unattested is True
    assert data.agc_verified is True
    assert data.agc_trusted is True
    assert data.agc_slope == pytest.approx(1.0, abs=0.01)
    # Locks exactly like an attested chain would: the recovered gain map is
    # exact (fraction=1.0 is a true LTI chain) and the true mic level (locked
    # volume + the chain's real gain) lands inside the window.
    assert data.gain_map_db == pytest.approx(10.0, abs=0.1)
    assert cfg.window_low_dbfs <= data.locked_main_volume_db + 10.0 <= cfg.window_high_dbfs


@pytest.mark.asyncio
async def test_agc_unattested_flat_chain_aborts_before_window_at_quiet_levels():
    # An AGC clawing back 80% of any commanded change: reported rms tracks
    # commanded volume at slope ~0.2, well under the 0.7 threshold. gain_db is
    # chosen so the INITIAL mic level (-30 dBFS) is comfortably above the
    # trust floor but comfortably below the pre-window (-21.5), so evidence
    # accumulates across several real staircase steps before either the
    # pre-window or the (tight, default cap_bump=12) cap is ever reached.
    cfg = MeasurementRamp(settle_hold_s=0.5, max_loop_latency_s=0.5, safety_timeout_s=60.0)
    chain = ChainModel(
        gain_db=20.0,
        start_vol=-50.0,  # matches cfg.start_db so delta==0 at the first tick
        agc_frozen=False,
        agc_unattested=True,
        agc_gain_fraction=0.2,
    )
    controller, data, tone = await _run(chain, cfg, original=-50.0)
    assert data.state == RampState.ERROR
    assert data.error == "agc_suspected"
    assert data.agc_verified is False
    assert data.agc_slope is not None and data.agc_slope < cfg.agc_slope_threshold
    # Caught at deeply quiet levels: evidence starts at the -50 dB staircase
    # start (the mic cleared the trust floor immediately), so the first
    # (marginal) span-gated estimate lands at start + agc_slope_min_span_db
    # — commanded -44.0 dB. A single marginal estimate is not terminal: the
    # gate holds for one more staircase step (+step_db) before confirming
    # the refusal, so the actual abort lands one step further out, at
    # -43.25 dB, still far below the pre-window. Never kept climbing an
    # AGC-suspected chain toward the target level.
    assert max(chain.commanded) == pytest.approx(
        -50.0 + cfg.agc_slope_min_span_db + cfg.step_db
    )
    assert max(chain.commanded) < cfg.pre_window - 20.0
    assert chain.commanded[-1] == pytest.approx(-50.0)  # restored
    # The terminal detail names both the marginal and the confirming slope
    # (identical here — a perfectly linear 0.2-fraction chain, no jitter —
    # but both are still reported) plus the final step count.
    assert data.error_detail is not None
    assert "slopes 0.20, 0.20 over 10 steps" == data.error_detail


# --- marginal-estimate retry (2026-07-16 jts3 false-positive) -----------------
#
# Hardware finding: a room-correction level ramp on jts3 refused with
# agc_suspected on a 3-step/6.65 dB-span estimate at slope 0.644 — just under
# the 0.70 threshold. The SAME mic passed the identical staircase-linearity
# gate cleanly (slopes 0.892/0.970/1.075/1.015, 4 steps each) in the
# crossover flow twenty minutes later: the 3-step estimate was noise, not a
# real AGC chain. A single marginal estimate must not be terminal.


def test_agc_marginal_estimate_holds_open_for_one_more_step():
    """PRE-FIX FAILING REPRO: feeds the exact jts3 shape (slope 0.644 over 3
    steps, 6.65 dB span) directly at the evidence-folding layer and asserts
    the gate holds it open rather than terminating on the spot. Against the
    unmodified kernel (a single evaluation immediately sets
    ``agc_verified = False``), the first two assertions below fail."""
    cfg = MeasurementRamp()  # unmodified thresholds: min_steps=3, min_span=6.0, threshold=0.70
    controller = RampController(session_id="t", config=cfg)
    data = RampData(current_main_volume_db=-50.0)
    loop_vars = _LoopVars()

    # Three distinct commanded levels spanning 6.65 dB, reported rms sitting
    # exactly on the line y = 0.644x (a noiseless chain — OLS reproduces the
    # slope exactly), reproducing the jts3 evidence verbatim.
    levels = [-50.0, -46.675, -43.35]
    for level in levels:
        data.current_main_volume_db = level
        controller._update_agc_evidence(data, loop_vars, cfg, [level * 0.644])

    # The marginal estimate must NOT terminate the run.
    assert data.agc_verified is None
    assert loop_vars.agc_marginal is not None
    marginal_slope, marginal_steps = loop_vars.agc_marginal
    assert marginal_slope == pytest.approx(0.644, abs=1e-6)
    assert marginal_steps == 3

    # One more staircase step of evidence at the SAME (still flat) slope: the
    # extension confirms the refusal rather than clearing it — the gate does
    # not hold open indefinitely.
    data.current_main_volume_db = -40.025  # levels[-1] + one more 3.325 dB step
    controller._update_agc_evidence(data, loop_vars, cfg, [-40.025 * 0.644])
    assert data.agc_verified is False
    assert data.agc_slope == pytest.approx(0.644, abs=1e-6)


def test_agc_marginal_estimate_that_clears_on_extension_verifies():
    """The mirror case: a first estimate that reads marginal (0.644 over 3
    steps, same as the jts3 finding) but the very next staircase step's
    evidence pulls the regression comfortably above threshold — the gate
    must verify, not refuse. This is the actual jts3 outcome: the identical
    mic passed cleanly once the crossover flow gave it more steps."""
    cfg = MeasurementRamp()
    controller = RampController(session_id="t", config=cfg)
    data = RampData(current_main_volume_db=-50.0)
    loop_vars = _LoopVars()

    levels = [-50.0, -46.675, -43.35]
    for level in levels:
        data.current_main_volume_db = level
        controller._update_agc_evidence(data, loop_vars, cfg, [level * 0.644])
    assert data.agc_verified is None  # held open, not refused

    # One more step whose reading is steep enough that the FULL regression
    # (all 4 points) clears the threshold.
    data.current_main_volume_db = -40.025
    controller._update_agc_evidence(
        data, loop_vars, cfg, [(-43.35 * 0.644) + 1.5 * (-40.025 - -43.35)]
    )
    assert data.agc_verified is True
    assert data.agc_slope is not None and data.agc_slope > cfg.agc_slope_threshold


def test_ramp_agc_suspected_event_has_exactly_one_emitter():
    """2026-07-16 jts3 finding: ramp_agc_suspected was logged TWICE per
    terminal from two call sites with two different field orders
    (session/slope/steps/at_db vs session/at_db/slope/steps/span_db). Only
    the terminal's own emission in run() may name this event now —
    _update_agc_evidence's marginal-hold branch logs the distinct
    ramp_agc_marginal event instead."""
    source = inspect.getsource(
        __import__("jasper.audio_measurement.ramp", fromlist=["ramp"])
    )
    assert source.count('"ramp_agc_suspected"') == 1


@pytest.mark.asyncio
async def test_agc_suspected_terminal_logs_exactly_one_event(caplog):
    """Behavioral companion to the source-scan pin above: drive a real
    AGC-suspected terminal end to end and confirm exactly one
    ramp_agc_suspected line reaches the log, not two."""
    cfg = MeasurementRamp(settle_hold_s=0.5, max_loop_latency_s=0.5, safety_timeout_s=60.0)
    chain = ChainModel(
        gain_db=20.0,
        start_vol=-50.0,
        agc_frozen=False,
        agc_unattested=True,
        agc_gain_fraction=0.2,
    )
    caplog.set_level(logging.INFO, logger="jasper.audio_measurement.ramp")
    controller, data, tone = await _run(chain, cfg, original=-50.0)
    assert data.state == RampState.ERROR
    assert data.error == "agc_suspected"
    suspected = [
        r for r in caplog.records if "event=ramp_agc_suspected" in r.getMessage()
    ]
    assert len(suspected) == 1
    # The held-open marginal estimate is a distinct, single-shot event too.
    marginal = [
        r for r in caplog.records if "event=ramp_agc_marginal" in r.getMessage()
    ]
    assert len(marginal) == 1


@pytest.mark.asyncio
async def test_agc_unattested_noisy_but_linear_chain_still_verifies():
    # The reviewer-flagged fragility case: a true-slope-1.0 chain with
    # realistic per-reading jitter. Over a mere ~1.5 dB of x-leverage (3
    # adjacent 0.75 dB steps) this jitter could push the OLS slope under the
    # threshold by chance; the 6 dB minimum-span gate is what makes the
    # verdict robust. 1.0 dB uniform jitter over the full span must verify.
    cfg = MeasurementRamp(**FAST)
    chain = ChainModel(
        gain_db=10.0,
        start_vol=-30.0,
        agc_frozen=False,
        agc_unattested=True,
        agc_gain_fraction=1.0,
        jitter_db=1.0,
        seed=7,
    )
    controller, data, tone = await _run(chain, cfg, original=-30.0)
    assert data.state == RampState.LOCKED
    assert data.agc_verified is True
    assert data.agc_slope > cfg.agc_slope_threshold


@pytest.mark.asyncio
async def test_agc_unattested_insufficient_evidence_at_lock_fails_closed():
    # The chain crosses the pre-window in a SINGLE staircase step (a near-cap
    # original volume), so the slope check never sees the required span/steps
    # before CONFIRMING would otherwise lock. Indeterminate must never
    # silently trust the lock — and it aborts under the DISTINCT
    # agc_indeterminate wire code (no AGC was observed, only insufficient
    # evidence; the phone renders different copy).
    cfg = MeasurementRamp(**FAST, agc_slope_min_steps=3)
    chain = ChainModel(
        gain_db=32.0,  # pre_window (-21.5) is crossed on the very first step
        start_vol=-50.0,
        agc_frozen=False,
        agc_unattested=True,
        agc_gain_fraction=1.0,
    )
    controller, data, tone = await _run(chain, cfg, original=-50.0)
    assert data.state == RampState.ERROR
    assert data.error == "agc_indeterminate"
    assert data.agc_verified is None  # indeterminate, not a computed failure
    assert data.lock_kind is None
    assert chain.commanded[-1] == pytest.approx(-50.0)  # restored


@pytest.mark.asyncio
async def test_agc_unattested_steps_met_but_span_unmet_is_still_indeterminate():
    # Isolates the SPAN gate from the steps floor: gain 28.2 crosses the
    # pre-window on the second staircase step, and the settle jump lands at
    # -44.2 — THREE distinct commanded levels (-50, -49.25, -44.2) satisfy
    # agc_slope_min_steps, but the 5.8 dB total span is under the 6.0 dB
    # minimum. The verdict must stay indeterminate (span is the regression's
    # x-leverage; a threshold met on steps alone is exactly the fragile shape
    # the span gate exists to reject) and the would-be lock fails closed.
    cfg = MeasurementRamp(**FAST)
    chain = ChainModel(
        gain_db=28.2,
        start_vol=-50.0,
        agc_frozen=False,
        agc_unattested=True,
        agc_gain_fraction=1.0,
    )
    controller, data, tone = await _run(chain, cfg, original=-50.0)
    assert data.state == RampState.ERROR
    assert data.error == "agc_indeterminate"
    assert data.agc_verified is None
    assert data.lock_kind is None
    assert chain.commanded[-1] == pytest.approx(-50.0)  # restored


@pytest.mark.asyncio
async def test_agc_unattested_indeterminate_bounded_low_fails_closed_to_maxed_out():
    # A driver capped early (a tweeter ramp shape, per the design brief): the
    # mic only clears the noise+margin trust threshold at the single
    # cap-clamped commanded level, so the slope check never sees the >= 3
    # distinct steps it needs for a verdict. The bounded-low degraded-lock
    # policy must NOT trust an unproven chain on that indeterminate evidence
    # — it fails closed to the EXISTING bounded_low_evidence_insufficient
    # MAXED_OUT path (the same one an attested run reaches with insufficient
    # sample/spread evidence), never a silently-trusted lock.
    cfg = MeasurementRamp(settle_hold_s=0.5, max_loop_latency_s=0.5, settle_min_samples=2, allow_bounded_low_level=True)
    original = -40.0
    cap = cfg.dynamic_cap(original)
    chain = ChainModel(
        gain_db=-6.0,
        start_vol=original,
        noise_floor_dbfs=-44.05,
        agc_frozen=False,
        agc_unattested=True,
        agc_gain_fraction=1.0,
    )
    controller, data, tone = await _run(chain, cfg, original=original)
    assert data.state == RampState.MAXED_OUT
    assert data.error is not None and "safe cap reached" in data.error
    assert data.agc_verified is None  # too few distinct steps for a verdict
    assert data.lock_kind is None
    assert chain.commanded[-1] == pytest.approx(original)  # restored
    assert cap < cfg.window_low_dbfs  # sanity: this really is the cap-bound case


# --- THE BLOCKER REGRESSION: sparse/misphased feeds MUST LOCK -----------------
#
# These encode the panel's exact failing schedules against the shipped kernel:
# 0.75 s batch cadence across arrival phases (was 0/40 locks) and 0.5 s cadence
# with ±30 ms jitter (was 3/60 locks). The settle buffer + hold extension must
# lock every one of them, with the implied mic level never above window_high.


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [0.05, 0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.74])
async def test_sparse_relay_cadence_locks(phase):
    clock = FakeClock()
    cfg = MeasurementRamp()
    chain = SparseChain(
        clock=clock,
        gain_db=10.0,
        start_vol=-30.0,
        batch_interval=0.75,
        phase=phase,
    )
    controller, data, tone = await _run(chain, cfg, clock=clock, original=-30.0)
    assert data.state == RampState.LOCKED, (
        f"phase={phase}: expected LOCKED, got {data.state} ({data.error})"
    )
    mic_at_lock = data.locked_main_volume_db + chain.gain_db
    assert cfg.window_low_dbfs <= mic_at_lock <= cfg.window_high_dbfs


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(6))
async def test_sparse_jittered_cadence_locks(seed):
    clock = FakeClock()
    cfg = MeasurementRamp()
    chain = SparseChain(
        clock=clock,
        gain_db=10.0,
        start_vol=-30.0,
        batch_interval=0.5,
        jitter_ms=30.0,
        seed=seed,
        transport_lag=0.5,
    )
    controller, data, tone = await _run(chain, cfg, clock=clock, original=-30.0)
    assert data.state == RampState.LOCKED, (
        f"seed={seed}: expected LOCKED, got {data.state} ({data.error})"
    )
    mic_at_lock = data.locked_main_volume_db + chain.gain_db
    assert cfg.window_low_dbfs <= mic_at_lock <= cfg.window_high_dbfs


@pytest.mark.asyncio
async def test_sparse_feed_no_ratchet_past_window_top():
    # The review's secondary blocker effect: the SETTLING→CLIMBING bounce
    # re-stepped the staircase ~2 dB PAST window_high. With the hold extension
    # there is no bounce: the implied mic level never exceeds window_high.
    clock = FakeClock()
    cfg = MeasurementRamp()
    chain = SparseChain(clock=clock, gain_db=18.0, start_vol=-40.0, batch_interval=0.75)
    controller, data, tone = await _run(chain, cfg, clock=clock, original=-40.0)
    assert data.state == RampState.LOCKED
    max_implied_mic = max(v + chain.gain_db for v in chain.commanded)
    assert max_implied_mic <= cfg.window_high_dbfs + 1e-6


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_lag", [0.5, 1.0, 1.5])
async def test_sparse_transport_lag_never_overshoots(transport_lag):
    # The latency machinery this design exists for, exercised at lag > 0
    # (the review: lag_ticks was defined but never used).
    clock = FakeClock()
    cfg = MeasurementRamp()
    chain = SparseChain(
        clock=clock,
        gain_db=18.0,
        start_vol=-40.0,
        batch_interval=0.5,
        transport_lag=transport_lag,
    )
    controller, data, tone = await _run(chain, cfg, clock=clock, original=-40.0)
    assert data.state == RampState.LOCKED
    max_implied_mic = max(v + chain.gain_db for v in chain.commanded)
    assert max_implied_mic <= cfg.window_high_dbfs + 1e-6
    mic_at_lock = data.locked_main_volume_db + chain.gain_db
    assert cfg.window_low_dbfs <= mic_at_lock <= cfg.window_high_dbfs


@pytest.mark.asyncio
async def test_sparse_down_jump_locks_loud_amp():
    # gain +40: the mic is ABOVE the window even at the -50 quiet floor. The
    # settle jump must go DOWN and lock in-window; after the freeze the
    # commanded volume never rises.
    clock = FakeClock()
    cfg = MeasurementRamp()
    chain = SparseChain(
        clock=clock,
        gain_db=40.0,
        start_vol=-30.0,
        batch_interval=0.5,
        transport_lag=0.5,
    )
    controller, data, tone = await _run(chain, cfg, clock=clock, original=-30.0)
    assert data.state == RampState.LOCKED
    mic_at_lock = data.locked_main_volume_db + chain.gain_db
    assert cfg.window_low_dbfs <= mic_at_lock <= cfg.window_high_dbfs
    # Monotone-down after the peak (the freeze point): no post-freeze climb.
    peak_index = chain.commanded.index(max(chain.commanded))
    tail = chain.commanded[peak_index:]
    assert all(b <= a + 1e-9 for a, b in zip(tail, tail[1:]))


@pytest.mark.asyncio
async def test_sparse_confirming_accepts_explicit_bounded_low_lock():
    # gain -2 with cap -24: the jump target clamps at the cap and the confirm
    # stream reads consistently below the window. Stable trusted post-latency
    # evidence may proceed, but only as an explicitly degraded lock.
    clock = FakeClock()
    cfg = MeasurementRamp(
        cap_bump_db=6.0,
        cap_ceil_db=-6.0,
        allow_bounded_low_level=True,
    )
    chain = SparseChain(
        clock=clock,
        gain_db=-2.0,
        start_vol=-30.0,
        batch_interval=0.5,
        transport_lag=0.5,
    )
    controller, data, tone = await _run(chain, cfg, clock=clock, original=-30.0)
    assert data.state == RampState.LOCKED
    assert data.lock_kind is RampLockKind.BOUNDED_LOW_LEVEL
    assert data.trusted_sample_count > 0  # evidence-gated
    cap = cfg.dynamic_cap(-30.0)
    assert data.locked_main_volume_db == pytest.approx(cap)
    assert data.settled_mic_dbfs == pytest.approx(-26.0)
    assert data.settled_snr_db == pytest.approx(54.0)
    assert data.window_shortfall_db == pytest.approx(6.0)
    assert data.settled_spread_db == pytest.approx(0.0)
    for vol in chain.commanded:
        assert vol <= cap + 1e-9


@pytest.mark.asyncio
async def test_sparse_quiet_amp_reaches_bounded_lock_not_timeout():
    # The review's SF1.0 repro: gain=-14/original=-14 (cap -8) died at the old
    # fixed 25 s timeout mid-climb. The derived timeout must let it reach the
    # bounded-low verdict.
    clock = FakeClock()
    cfg = MeasurementRamp(
        cap_bump_db=6.0,
        cap_ceil_db=-6.0,
        allow_bounded_low_level=True,
    )
    chain = SparseChain(
        clock=clock, gain_db=-14.0, start_vol=-14.0, batch_interval=0.75
    )
    controller, data, tone = await _run(chain, cfg, clock=clock, original=-14.0)
    assert data.state == RampState.LOCKED
    assert data.lock_kind is RampLockKind.BOUNDED_LOW_LEVEL
    assert data.error is None or "timeout" not in data.error


@pytest.mark.asyncio
async def test_sparse_corrective_rejump_recovers_gain_shift():
    # The amp knob moves +6 dB right as the first jump lands: CONFIRMING sees a
    # consistent out-of-window stream, recomputes the gain map, and takes the
    # ONE bounded corrective jump — then locks.
    clock = FakeClock()
    cfg = MeasurementRamp()
    chain = SparseChain(
        clock=clock,
        gain_db=2.0,
        start_vol=-30.0,
        batch_interval=0.5,
        transport_lag=0.5,
        gain_shift_after_jump=6.0,
    )
    controller, data, tone = await _run(chain, cfg, clock=clock, original=-30.0)
    assert data.state == RampState.LOCKED
    mic_at_lock = data.locked_main_volume_db + chain.gain_db
    assert cfg.window_low_dbfs <= mic_at_lock <= cfg.window_high_dbfs


# --- SAFETY: every commanded volume <= dynamic cap AND <= 0 dB ----------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "gain_db,original",
    [
        (2.0, -30.0),
        (0.0, -30.0),
        (10.0, -20.0),
        (-5.0, -25.0),
        (20.0, -18.0),
        (-30.0, -40.0),  # amp very quiet → will max out
    ],
)
async def test_no_commanded_volume_exceeds_caps(gain_db, original):
    cfg = MeasurementRamp(**FAST)
    chain = ChainModel(gain_db=gain_db, start_vol=original, noise_floor_dbfs=-75.0)
    controller, data, tone = await _run(chain, cfg, original=original)
    cap = cfg.dynamic_cap(original)
    ceiling = min(cap, HARD_CEILING_DBFS)
    assert chain.commanded, "controller must command at least the start volume"
    for vol in chain.commanded:
        assert math.isfinite(vol)
        # The restore-to-original final is exempt from the dynamic cap by
        # design; every other command must respect it. All commands respect
        # the hard ceiling.
        assert vol <= HARD_CEILING_DBFS + 1e-9
        assert vol <= max(ceiling, original) + 1e-9, (
            f"commanded {vol} exceeds ceiling {ceiling} (gain={gain_db}, "
            f"orig={original})"
        )


@pytest.mark.asyncio
async def test_hard_ceiling_belt_holds_in_loosest_config():
    # Pin the 0 dB belt itself (review nit): the loosest constructible cap
    # (cap_ceil_db=0, huge bump) must still never command above 0 dB.
    cfg = MeasurementRamp(cap_ceil_db=0.0, cap_bump_db=60.0, **FAST)
    chain = ChainModel(gain_db=-5.0, start_vol=-30.0)
    controller, data, tone = await _run(chain, cfg, original=-30.0)
    for vol in chain.commanded:
        assert vol <= HARD_CEILING_DBFS + 1e-9


# --- SAFETY: NaN / non-finite handling ----------------------------------------


def test_level_sample_from_dict_rejects_non_finite():
    with pytest.raises(ValueError, match="non-finite"):
        LevelSample.from_dict({"seq": 1, "rms_dbfs": float("nan")})
    with pytest.raises(ValueError, match="non-finite"):
        LevelSample.from_dict({"seq": 1, "rms_dbfs": -20.0, "peak_dbfs": float("inf")})


@pytest.mark.asyncio
async def test_nan_poisoned_batches_never_reach_the_setter():
    # Every third sample is NaN (a hostile relay post): the ramp must still
    # lock on the finite samples, and no commanded volume is ever non-finite.
    cfg = MeasurementRamp(**FAST)
    chain = ChainModel(gain_db=10.0, start_vol=-30.0, nan_every=3)
    controller, data, tone = await _run(chain, cfg, original=-30.0)
    assert data.state == RampState.LOCKED
    for vol in chain.commanded:
        assert math.isfinite(vol), f"non-finite volume commanded: {vol!r}"
    assert data.gain_map_db is not None and math.isfinite(data.gain_map_db)


@pytest.mark.asyncio
async def test_non_finite_noise_floor_is_treated_as_unknown():
    cfg = MeasurementRamp(**FAST)
    chain = ChainModel(gain_db=10.0, start_vol=-30.0)
    clock = FakeClock()
    controller = RampController(session_id="t", config=cfg)
    tone = BlockingTone()

    async def get_vol():
        return -30.0

    data = await controller.run(
        get_main_volume_db=get_vol,
        set_main_volume_db=chain.set_vol,
        play_continuous_tone=tone.play,
        cancel_tone=tone.cancel,
        next_samples=chain.next_samples,
        noise_floor_dbfs=float("nan"),
        clock=clock.now,
        sleep=clock.sleep,
    )
    assert data.noise_floor_dbfs is None  # normalized, not silently trusted
    assert data.state == RampState.LOCKED


@pytest.mark.asyncio
async def test_non_finite_original_errors_before_any_volume_change():
    cfg = MeasurementRamp(**FAST)
    chain = ChainModel(gain_db=2.0, start_vol=-30.0)
    clock = FakeClock()
    controller = RampController(session_id="t", config=cfg)
    tone = BlockingTone()

    async def get_vol():
        return float("nan")

    data = await controller.run(
        get_main_volume_db=get_vol,
        set_main_volume_db=chain.set_vol,
        play_continuous_tone=tone.play,
        cancel_tone=tone.cancel,
        next_samples=chain.next_samples,
        noise_floor_dbfs=-80.0,
        clock=clock.now,
        sleep=clock.sleep,
    )
    assert data.state == RampState.ERROR
    assert chain.commanded == []  # nothing was touched
    assert not tone.started


# --- SAFETY: clip aborts immediately ------------------------------------------


@pytest.mark.asyncio
async def test_clip_aborts_and_restores():
    cfg = MeasurementRamp(**FAST)
    # Clip fires as soon as the ramp climbs above -35 dB.
    chain = ChainModel(gain_db=2.0, start_vol=-40.0, clip_at_vol=-35.0)
    controller, data, tone = await _run(chain, cfg, original=-40.0)
    assert data.state == RampState.ABORTED
    assert data.error == "clip detected"
    # Restored to the original level (last commanded value).
    assert chain.commanded[-1] == pytest.approx(data.original_main_volume_db)
    assert tone.cancelled


# --- SAFETY: restore is EXACT, never cap-clamped -------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("original", [-4.0, -5.0, -8.5])
async def test_cancel_restores_original_exactly_even_above_cap(original):
    # The review's regression: original -4/-5 sits ABOVE the -6 cap; the
    # restore must return the user's own level exactly (hard ceiling only).
    cfg = MeasurementRamp(settle_hold_s=5.0, max_loop_latency_s=2.0)
    chain = ChainModel(gain_db=2.0, start_vol=original)
    controller, data, tone = await _run(chain, cfg, original=original, cancel_after=3)
    assert data.state == RampState.CANCELLED
    assert chain.commanded[-1] == pytest.approx(original)


@pytest.mark.asyncio
async def test_timeout_restores_original_exactly_above_cap():
    cfg = MeasurementRamp(
        settle_hold_s=0.5,
        max_loop_latency_s=0.5,
        safety_timeout_s=1.0,
        step_db=0.1,
        step_interval_s=0.6,
    )
    chain = ChainModel(gain_db=-100.0, start_vol=-4.0, noise_floor_dbfs=-200.0)
    controller, data, tone = await _run(chain, cfg, original=-4.0)
    assert data.state == RampState.CANCELLED
    assert data.error is not None and "safety timeout" in data.error
    assert chain.commanded[-1] == pytest.approx(-4.0)


# --- SAFETY: MAXED_OUT requires evidence; empty/untrusted feeds don't diagnose --


@pytest.mark.asyncio
async def test_jts3_level_evidence_locks_only_as_bounded_low():
    cfg = MeasurementRamp(**FAST, allow_bounded_low_level=True)
    # Hardware evidence reproduced from JTS3: cap ~= -3.15 dB, stable UMIK
    # median ~= -33.07 dBFS, floor ~= -44.53 dBFS (11.46 dB SNR).
    original = -15.15
    cap = cfg.dynamic_cap(original)
    chain = ChainModel(
        gain_db=-33.07 - cap,
        start_vol=original,
        noise_floor_dbfs=-44.53,
    )
    controller, data, tone = await _run(chain, cfg, original=original)
    assert data.state == RampState.LOCKED
    assert data.lock_kind is RampLockKind.BOUNDED_LOW_LEVEL
    assert data.trusted_sample_count > 0
    assert data.locked_main_volume_db == pytest.approx(cap)
    assert data.settled_mic_dbfs == pytest.approx(-33.07)
    assert data.settled_snr_db == pytest.approx(11.46)
    assert data.window_shortfall_db == pytest.approx(13.07)
    snap = data.snapshot()
    assert snap["lock_kind"] == "bounded_low_level"
    assert snap["settled_mic_dbfs"] == -33.07
    assert snap["settled_snr_db"] == 11.46
    assert snap["window_shortfall_db"] == 13.07
    assert snap["settled_spread_db"] == 0.0
    for vol in chain.commanded:
        assert vol <= cap + 1e-9


@pytest.mark.asyncio
async def test_bounded_low_requires_known_noise_floor():
    cfg = MeasurementRamp(**FAST, allow_bounded_low_level=True)
    chain = ChainModel(
        gain_db=-30.0,
        start_vol=-15.0,
        noise_floor_dbfs=None,
    )
    controller, data, tone = await _run(chain, cfg, original=-15.0)
    assert data.state == RampState.MAXED_OUT
    assert data.lock_kind is None
    assert data.locked_main_volume_db is None
    assert data.settled_snr_db is None
    assert chain.commanded[-1] == pytest.approx(-15.0)


@pytest.mark.asyncio
async def test_bounded_low_is_opt_in_not_shared_room_policy():
    cfg = MeasurementRamp(**FAST)
    chain = ChainModel(
        gain_db=-30.0,
        start_vol=-15.0,
        noise_floor_dbfs=-45.0,
    )
    controller, data, tone = await _run(chain, cfg, original=-15.0)
    assert data.state == RampState.MAXED_OUT
    assert data.lock_kind is None


@pytest.mark.asyncio
async def test_bounded_low_rejects_signal_beyond_absolute_shortfall_limit():
    cfg = MeasurementRamp(
        **FAST,
        allow_bounded_low_level=True,
        bounded_low_max_shortfall_db=20.0,
    )
    chain = ChainModel(
        gain_db=-80.0 - cfg.dynamic_cap(-15.0),
        start_vol=-15.0,
        noise_floor_dbfs=-90.0,
    )
    controller, data, tone = await _run(chain, cfg, original=-15.0)
    assert data.state == RampState.MAXED_OUT
    assert data.settled_snr_db == pytest.approx(10.0)
    assert data.window_shortfall_db == pytest.approx(60.0)
    assert data.lock_kind is None


@pytest.mark.asyncio
async def test_unstable_cap_evidence_never_bounded_locks():
    cfg = MeasurementRamp(
        **FAST,
        allow_bounded_low_level=True,
        bounded_low_max_spread_db=1.5,
    )

    class UnstableChain(ChainModel):
        async def next_samples(self):
            samples = await super().next_samples()
            sample = samples[0]
            wobble = 3.0 if self._seq % 2 == 0 else 0.0
            return [
                LevelSample(
                    seq=sample.seq,
                    t_client_ms=sample.t_client_ms,
                    rms_dbfs=sample.rms_dbfs + wobble,
                    peak_dbfs=sample.peak_dbfs + wobble,
                    clip=False,
                    agc_frozen=True,
                )
            ]

    chain = UnstableChain(
        gain_db=-30.0,
        start_vol=-15.0,
        noise_floor_dbfs=-45.0,
    )
    controller, data, tone = await _run(chain, cfg, original=-15.0)
    assert data.state == RampState.MAXED_OUT
    assert data.lock_kind is None
    assert data.settled_spread_db == pytest.approx(3.0)
    assert data.locked_main_volume_db is None


@pytest.mark.asyncio
async def test_sparse_cap_evidence_loses_feed_instead_of_locking():
    clock = FakeClock()
    cfg = MeasurementRamp(
        **FAST,
        allow_bounded_low_level=True,
        feed_timeout_s=1.0,
    )
    cap = cfg.dynamic_cap(-15.0)

    class OnePostLatencyCapSample(ChainModel):
        def __init__(self):
            super().__init__(
                gain_db=-30.0,
                start_vol=-15.0,
                noise_floor_dbfs=-45.0,
            )
            self.cap_at = None
            self.emitted_at_cap = False

        async def set_vol(self, db):
            await super().set_vol(db)
            if math.isclose(db, cap, abs_tol=1e-9) and self.cap_at is None:
                self.cap_at = clock.now()

        async def next_samples(self):
            if self.cap_at is None:
                return await super().next_samples()
            if (
                not self.emitted_at_cap
                and clock.now() - self.cap_at >= cfg.max_loop_latency_s
            ):
                self.emitted_at_cap = True
                return await super().next_samples()
            return []

    chain = OnePostLatencyCapSample()
    controller, data, tone = await _run(chain, cfg, clock=clock, original=-15.0)
    assert data.state == RampState.ABORTED
    assert data.lock_kind is None
    assert data.error is not None and "phone feed lost" in data.error
    assert chain.commanded[-1] == pytest.approx(-15.0)


@pytest.mark.asyncio
async def test_clip_at_cap_aborts_before_bounded_lock():
    cfg = MeasurementRamp(**FAST, allow_bounded_low_level=True)
    cap = cfg.dynamic_cap(-15.0)
    chain = ChainModel(
        gain_db=-30.0,
        start_vol=-15.0,
        noise_floor_dbfs=-45.0,
        clip_at_vol=cap,
    )
    controller, data, tone = await _run(chain, cfg, original=-15.0)
    assert data.state == RampState.ABORTED
    assert data.lock_kind is None
    assert data.error == "clip detected"
    assert chain.commanded[-1] == pytest.approx(-15.0)


@pytest.mark.asyncio
async def test_all_untrusted_samples_is_error_not_maxed_out(caplog):
    # Noise floor so high nothing is ever trusted: the old kernel called this
    # MAXED_OUT ("raise your amp") and stored a lock — affirmatively wrong.
    cfg = MeasurementRamp(
        settle_hold_s=0.5, max_loop_latency_s=0.5, safety_timeout_s=60.0
    )
    chain = ChainModel(gain_db=2.0, start_vol=-40.0, noise_floor_dbfs=-24.0)
    controller, data, tone = await _run(chain, cfg, original=-40.0)
    assert data.state == RampState.ERROR
    assert data.error == "no usable phone samples"
    assert data.trusted_sample_count == 0
    assert data.observed_sample_count > 0
    assert data.finite_sample_count == data.observed_sample_count
    assert data.below_noise_sample_count == data.observed_sample_count
    assert data.agc_rejected_sample_count == 0
    assert data.nonfinite_sample_count == 0
    assert data.max_observed_rms_dbfs is not None
    assert data.max_observed_peak_dbfs is not None
    assert data.max_signal_over_noise_db is not None
    assert data.max_signal_over_noise_db < cfg.trust_margin_db
    snap = data.snapshot()
    assert snap["observed_sample_count"] == data.observed_sample_count
    assert snap["max_signal_over_noise_db"] == round(
        data.max_signal_over_noise_db, 2
    )
    assert snap["trust_margin_db"] == cfg.trust_margin_db
    assert snap["trust_threshold_dbfs"] == -14.0
    assert snap["trust_deficit_db"] > 0.0
    terminal = next(
        message
        for message in caplog.messages
        if "event=ramp_error" in message and "reason=no_usable_samples" in message
    )
    assert "observed_samples=" in terminal
    assert "below_noise_samples=" in terminal
    assert "max_signal_over_noise_db=" in terminal
    assert "trust_threshold_dbfs=" in terminal
    assert "trust_deficit_db=" in terminal
    # Restored, not held at the cap.
    assert chain.commanded[-1] == pytest.approx(-40.0)


@pytest.mark.asyncio
async def test_jts3_room_cap_clears_trust_without_weakening_shared_margin():
    """Pin the 2026-07-11 JTS3 UMIK listening-position regression."""
    original = -15.15
    noise_floor = -41.3
    measured_gain = -31.88 - (-3.15)
    common = {
        "settle_hold_s": 0.5,
        "max_loop_latency_s": 0.5,
        "safety_timeout_s": 90.0,
        "allow_bounded_low_level": True,
    }

    shared = MeasurementRamp(**common)
    assert shared.trust_margin_db == 10.0
    assert shared.dynamic_cap(original) == pytest.approx(-3.15)
    default_chain = ChainModel(
        gain_db=measured_gain,
        start_vol=original,
        noise_floor_dbfs=noise_floor,
    )
    _, default_data, _ = await _run(
        default_chain,
        shared,
        original=original,
    )
    assert default_data.state is RampState.ERROR
    assert default_data.trusted_sample_count == 0
    assert default_data.trust_deficit_db == pytest.approx(0.58)
    assert default_chain.commanded[-1] == pytest.approx(original)

    room = MeasurementRamp(
        **common,
        cap_bump_db=15.0,
        cap_ceil_db=0.0,
    )
    assert room.trust_margin_db == shared.trust_margin_db
    assert room.dynamic_cap(original) == pytest.approx(-0.15)
    room_chain = ChainModel(
        gain_db=measured_gain,
        start_vol=original,
        noise_floor_dbfs=noise_floor,
    )
    _, room_data, _ = await _run(room_chain, room, original=original)
    assert room_data.state is RampState.LOCKED
    assert room_data.lock_kind is RampLockKind.BOUNDED_LOW_LEVEL
    assert room_data.max_signal_over_noise_db >= room.trust_margin_db
    assert room_data.trust_deficit_db == 0.0
    # The kernel holds the reusable lock target for its owning flow; the room
    # MeasurementSession adapter owns the immediate exact restore (pinned in
    # test_room_session_jts3_evidence_locks_and_restores_exactly).
    assert room_chain.commanded[-1] == pytest.approx(room.dynamic_cap(original))


@pytest.mark.asyncio
async def test_agc_unfrozen_never_trusted_and_never_maxed_out():
    cfg = MeasurementRamp(
        settle_hold_s=0.5, max_loop_latency_s=0.5, safety_timeout_s=60.0
    )
    chain = ChainModel(gain_db=2.0, start_vol=-40.0, agc_frozen=False)
    controller, data, tone = await _run(chain, cfg, original=-40.0)
    assert data.agc_frozen is False
    assert data.state == RampState.ERROR  # zero trusted evidence
    assert data.state != RampState.LOCKED
    assert data.agc_rejected_sample_count > 0
    assert data.below_noise_sample_count == 0
    assert chain.commanded[-1] == pytest.approx(-40.0)


# --- SAFETY: feed liveness ------------------------------------------------------


@pytest.mark.asyncio
async def test_vanished_phone_aborts_instead_of_blind_climbing():
    # The phone dies mid-climb (no pagehide): batches stop. The kernel must
    # abort within feed_timeout_s and restore — never blind-climb to the cap
    # with clip protection gone.
    clock = FakeClock()
    cfg = MeasurementRamp()
    chain = SparseChain(
        clock=clock,
        gain_db=2.0,
        start_vol=-30.0,
        batch_interval=0.75,
        silent_after=5.0,
    )
    controller, data, tone = await _run(chain, cfg, clock=clock, original=-30.0)
    assert data.state == RampState.ABORTED
    assert data.error is not None and "phone feed lost" in data.error
    assert chain.commanded[-1] == pytest.approx(-30.0)  # restored
    # The blind climb was interrupted well before the cap.
    cap = cfg.dynamic_cap(-30.0)
    climbing = chain.commanded[:-1]
    assert max(climbing) < cap - 1.0


# --- SAFETY: safety timeout always fires ----------------------------------------


@pytest.mark.asyncio
async def test_safety_timeout_fires_and_restores():
    cfg_slow = MeasurementRamp(
        settle_hold_s=0.5,
        max_loop_latency_s=0.5,
        safety_timeout_s=1.0,
        step_db=0.1,
        step_interval_s=0.6,
    )
    chain = ChainModel(gain_db=-100.0, start_vol=-50.0, noise_floor_dbfs=-200.0)
    controller, data, tone = await _run(chain, cfg_slow, original=-50.0)
    assert data.state == RampState.CANCELLED
    assert data.error is not None and "safety timeout" in data.error
    assert chain.commanded[-1] == pytest.approx(-50.0)


# --- tone contract ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_tone_ending_early_errors_and_restores():
    # A tone that returns immediately (WAV too short / player crash) must not
    # leave the ramp blind-climbing a silent speaker.
    cfg = MeasurementRamp(**FAST)
    chain = ChainModel(gain_db=2.0, start_vol=-30.0)
    clock = FakeClock()
    controller = RampController(session_id="t", config=cfg)

    async def instant_tone():
        return None

    async def get_vol():
        return -30.0

    data = await controller.run(
        get_main_volume_db=get_vol,
        set_main_volume_db=chain.set_vol,
        play_continuous_tone=instant_tone,
        cancel_tone=lambda: None,
        next_samples=chain.next_samples,
        noise_floor_dbfs=-80.0,
        clock=clock.now,
        sleep=clock.sleep,
    )
    assert data.state == RampState.ERROR
    assert data.error == "tone ended before the ramp completed"
    assert chain.commanded[-1] == pytest.approx(-30.0)


# --- order of operations + fade -------------------------------------------------


@pytest.mark.asyncio
async def test_quiet_start_before_tone_and_fade_before_kill():
    cfg = MeasurementRamp(**FAST)
    events: list[tuple[str, float | None]] = []
    controller = RampController(session_id="order", config=cfg)
    chain = ChainModel(gain_db=10.0, start_vol=-30.0)
    clock = FakeClock()

    async def set_vol(db):
        events.append(("set", float(db)))
        await chain.set_vol(db)

    tone_event = asyncio.Event()

    async def play_tone():
        events.append(("tone_start", None))
        try:
            await asyncio.wait_for(tone_event.wait(), timeout=20.0)
        except asyncio.TimeoutError:
            pass

    def cancel_tone():
        events.append(("tone_cancel", None))
        tone_event.set()

    async def get_vol():
        return -30.0

    data = await controller.run(
        get_main_volume_db=get_vol,
        set_main_volume_db=set_vol,
        play_continuous_tone=play_tone,
        cancel_tone=cancel_tone,
        next_samples=chain.next_samples,
        noise_floor_dbfs=-80.0,
        clock=clock.now,
        sleep=clock.sleep,
    )
    assert data.state == RampState.LOCKED
    # Quiet start BEFORE the tone.
    first_set = next(i for i, e in enumerate(events) if e[0] == "set")
    first_tone = next(i for i, e in enumerate(events) if e[0] == "tone_start")
    assert first_set < first_tone
    assert events[first_set] == ("set", cfg.start_db)
    # Fade-down BEFORE the tone kill: the last set before tone_cancel is at
    # (or below) the fade floor — the review asked for the ORDER pin, not just
    # "both happened".
    cancel_idx = next(i for i, e in enumerate(events) if e[0] == "tone_cancel")
    sets_before = [e[1] for e in events[:cancel_idx] if e[0] == "set"]
    assert sets_before[-1] <= cfg.fade_down_to_db + 1e-9


# --- manual lock + cancel ---------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_lock_trusts_the_user():
    cfg = MeasurementRamp(settle_hold_s=5.0, max_loop_latency_s=2.0)
    chain = ChainModel(gain_db=2.0, start_vol=-40.0)
    controller, data, tone = await _run(chain, cfg, original=-40.0, manual_lock_after=3)
    assert data.state == RampState.LOCKED
    assert data.lock_kind is RampLockKind.MANUAL
    assert data.locked_main_volume_db is not None
    cap = cfg.dynamic_cap(-40.0)
    for vol in chain.commanded:
        assert vol <= cap + 1e-9


@pytest.mark.asyncio
async def test_lock_cancel_return_false_after_terminal():
    controller = RampController(session_id="t")
    controller.data.state = RampState.LOCKED
    assert await controller.lock() is False
    assert await controller.cancel() is False


# --- restore hook is idempotent ---------------------------------------------------


@pytest.mark.asyncio
async def test_restore_is_idempotent():
    controller = RampController(session_id="restore")
    restored: list[float] = []

    async def setter(db):
        restored.append(db)

    controller.main_volume_setter = setter
    controller.data.state = RampState.LOCKED
    controller.data.original_main_volume_db = -18.0
    await controller.restore_listening_volume_if_ramped()
    await controller.restore_listening_volume_if_ramped()
    assert restored == [-18.0]
    assert controller.data.restored is True


@pytest.mark.asyncio
async def test_restore_rejection_stays_retryable():
    controller = RampController(session_id="restore-retry")
    calls: list[float] = []

    async def rejected(db):
        calls.append(db)
        return False

    controller.main_volume_setter = rejected
    controller.data.state = RampState.LOCKED
    controller.data.original_main_volume_db = -18.0
    await controller.restore_listening_volume_if_ramped()
    await controller.restore_listening_volume_if_ramped()
    assert calls == [-18.0, -18.0]
    assert controller.data.restored is False


# --- LevelSample parsing -----------------------------------------------------------


def test_level_sample_from_dict_strict_on_rms():
    s = LevelSample.from_dict(
        {"seq": 5, "t_client_ms": 500, "rms_dbfs": -22.0, "peak_dbfs": -18.0}
    )
    assert s.seq == 5 and s.rms_dbfs == -22.0 and s.agc_frozen is True
    with pytest.raises(KeyError):
        LevelSample.from_dict({"seq": 1})  # missing rms_dbfs


def test_level_sample_defaults_peak_to_rms():
    s = LevelSample.from_dict({"rms_dbfs": -30.0})
    assert s.peak_dbfs == -30.0 and math.isclose(s.rms_dbfs, -30.0)
