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
  * ``maxed_out`` requires trusted evidence and never exceeds the caps; a phone
    that never produced a usable sample is an ERROR that restores;
  * the safety timeout (derived from the config's own worst-case walk) always
    fires, and a quiet amp reaches MAXED_OUT *before* it;
  * cancel/timeout/clip restore the user's own volume EXACTLY (no cap clamp);
  * the tone-player contract (play-until-cancelled) is enforced.
"""
from __future__ import annotations

import asyncio
import math
import random

import pytest

from jasper.audio_measurement.ramp import (
    HARD_CEILING_DBFS,
    LEVEL_EVENT_SCHEMA_VERSION,
    LevelSample,
    MeasurementRamp,
    RampController,
    RampState,
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
    every call. The fast regime for cap/clip/trust property tests."""

    def __init__(
        self,
        *,
        gain_db: float,
        start_vol: float = -50.0,
        noise_floor_dbfs: float = -80.0,
        agc_frozen: bool = True,
        clip_at_vol: float | None = None,
        nan_every: int | None = None,
    ) -> None:
        self.gain_db = gain_db
        self.noise_floor_dbfs = noise_floor_dbfs
        self.agc_frozen = agc_frozen
        self.clip_at_vol = clip_at_vol
        self.nan_every = nan_every
        self._vol = start_vol
        self._seq = 0
        self.commanded: list[float] = []

    async def set_vol(self, db: float) -> None:
        self._vol = db
        self.commanded.append(db)

    async def next_samples(self) -> list[LevelSample]:
        mic = self._vol + self.gain_db
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


def test_settle_hold_must_cover_loop_latency():
    with pytest.raises(ValueError, match="settle_hold_s"):
        MeasurementRamp(settle_hold_s=1.0, max_loop_latency_s=2.0)


def test_dynamic_cap_matches_autolevel_semantics():
    cfg = MeasurementRamp()
    # Same clamp table AutolevelController used: original+6 clamped to [-20,-6].
    assert cfg.dynamic_cap(-20.0) == -14.0
    assert cfg.dynamic_cap(-10.0) == -6.0
    assert cfg.dynamic_cap(-5.0) == -6.0
    assert cfg.dynamic_cap(-45.0) == -20.0


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
    cfg = MeasurementRamp.from_env()
    assert cfg.trust_margin_db == 14.0
    assert cfg.feed_timeout_s == 12.0


def test_from_env_confirm_k_floor_is_two(monkeypatch):
    # The spec pins k>=3 as the default; the env knob may weaken to 2 but a
    # single sample is never "consecutive confirmation".
    monkeypatch.setenv("JASPER_RAMP_CONFIRM_K", "1")
    assert MeasurementRamp.from_env().confirm_k == MeasurementRamp.confirm_k
    monkeypatch.setenv("JASPER_RAMP_CONFIRM_K", "2")
    assert MeasurementRamp.from_env().confirm_k == 2


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
        gain_db=2.0,
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
        gain_db=2.0,
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
    chain = SparseChain(
        clock=clock, gain_db=10.0, start_vol=-40.0, batch_interval=0.75
    )
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
        gain_db=6.0,
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
        clock=clock, gain_db=40.0, start_vol=-30.0, batch_interval=0.5,
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
async def test_sparse_confirming_maxed_out_quiet_amp():
    # gain -2 with cap -20: the jump target clamps at the cap and the confirm
    # stream reads consistently below the window → MAXED_OUT via CONFIRMING
    # (the review: this terminal existed only via CLIMBING in tests).
    clock = FakeClock()
    cfg = MeasurementRamp()
    chain = SparseChain(
        clock=clock, gain_db=-2.0, start_vol=-30.0, batch_interval=0.5,
        transport_lag=0.5,
    )
    controller, data, tone = await _run(chain, cfg, clock=clock, original=-30.0)
    assert data.state == RampState.MAXED_OUT
    assert data.trusted_sample_count > 0  # evidence-gated
    cap = cfg.dynamic_cap(-30.0)
    assert data.locked_main_volume_db == pytest.approx(cap)
    for vol in chain.commanded:
        assert vol <= cap + 1e-9


@pytest.mark.asyncio
async def test_sparse_quiet_amp_reaches_maxed_out_not_timeout():
    # The review's SF1.0 repro: gain=-14/original=-14 (cap -8) died at the old
    # fixed 25 s timeout mid-climb. The derived timeout must let it reach the
    # actionable MAXED_OUT verdict.
    clock = FakeClock()
    cfg = MeasurementRamp()
    chain = SparseChain(
        clock=clock, gain_db=-14.0, start_vol=-14.0, batch_interval=0.75
    )
    controller, data, tone = await _run(chain, cfg, clock=clock, original=-14.0)
    assert data.state == RampState.MAXED_OUT
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
    cfg = MeasurementRamp(
        cap_ceil_db=0.0, cap_bump_db=60.0, **FAST
    )
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
    chain = ChainModel(gain_db=2.0, start_vol=-30.0, nan_every=3)
    controller, data, tone = await _run(chain, cfg, original=-30.0)
    assert data.state == RampState.LOCKED
    for vol in chain.commanded:
        assert math.isfinite(vol), f"non-finite volume commanded: {vol!r}"
    assert data.gain_map_db is not None and math.isfinite(data.gain_map_db)


@pytest.mark.asyncio
async def test_non_finite_noise_floor_is_treated_as_unknown():
    cfg = MeasurementRamp(**FAST)
    chain = ChainModel(gain_db=2.0, start_vol=-30.0)
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
    chain = ChainModel(gain_db=2.0, start_vol=-50.0, clip_at_vol=-35.0)
    controller, data, tone = await _run(chain, cfg, original=-50.0)
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
async def test_maxed_out_when_amp_too_quiet_has_evidence():
    cfg = MeasurementRamp(**FAST)
    # Gain so negative that even at the cap the mic never reaches the window,
    # but readings clear the trust floor → genuine MAXED_OUT.
    chain = ChainModel(gain_db=-30.0, start_vol=-40.0, noise_floor_dbfs=-90.0)
    controller, data, tone = await _run(chain, cfg, original=-40.0)
    assert data.state == RampState.MAXED_OUT
    assert data.trusted_sample_count > 0
    cap = cfg.dynamic_cap(-40.0)
    assert data.locked_main_volume_db is not None
    assert data.locked_main_volume_db <= cap + 1e-9
    for vol in chain.commanded:
        assert vol <= cap + 1e-9


@pytest.mark.asyncio
async def test_all_untrusted_samples_is_error_not_maxed_out():
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
    # Restored, not held at the cap.
    assert chain.commanded[-1] == pytest.approx(-40.0)


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
    chain = ChainModel(gain_db=2.0, start_vol=-30.0)
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
    controller, data, tone = await _run(
        chain, cfg, original=-40.0, manual_lock_after=3
    )
    assert data.state == RampState.LOCKED
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
