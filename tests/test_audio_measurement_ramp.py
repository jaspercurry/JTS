# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Synthetic tests for the settle-based level-match ramp kernel.

The controller is pure: a fake clock, a fake volume setter, and a fake mic-sample
source that models the chain gain ``G`` (``mic_dbfs = main_volume + G``) let us
drive the whole state machine deterministically — no CamillaDSP, no aplay, no
relay. These tests are the safety proof the adversarial panel probes:

  * every commanded volume is ``<=`` the dynamic cap AND ``<= 0 dB``;
  * stop-ahead never overshoots past ``window_high`` given the modeled latency;
  * ``clip=true`` aborts immediately with a fade;
  * ``maxed_out`` terminates without exceeding the caps and never above the cap;
  * the safety timeout always fires;
  * a below-trust-floor reading never drives a lock;
  * ``agc_frozen=false`` is surfaced and never used as a trusted reference.
"""
from __future__ import annotations

import math

import pytest

from jasper.audio_measurement.ramp import (
    HARD_CEILING_DBFS,
    LEVEL_EVENT_SCHEMA_VERSION,
    LevelSample,
    MeasurementRamp,
    RampController,
    RampState,
)


class FakeClock:
    """Deterministic monotonic clock advanced by the fake sleep."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        # Advance time by at least a tick so a `sleep(0.01)` loop makes progress.
        self.t += max(seconds, 0.01)


class ChainModel:
    """Models the acoustic chain: mic_dbfs = commanded_main_volume + gain_db.

    The controller sets volume through ``set_vol``; ``next_samples`` reports back
    the mic level at whatever volume is *currently* commanded (optionally with a
    modeled transport lag of N ticks). Clip / agc flags are injectable.
    """

    def __init__(
        self,
        *,
        gain_db: float,
        start_vol: float = -50.0,
        noise_floor_dbfs: float = -70.0,
        lag_ticks: int = 0,
        agc_frozen: bool = True,
        clip_at_vol: float | None = None,
    ) -> None:
        self.gain_db = gain_db
        self.noise_floor_dbfs = noise_floor_dbfs
        self.lag_ticks = lag_ticks
        self.agc_frozen = agc_frozen
        self.clip_at_vol = clip_at_vol
        self._vol = start_vol
        self._history: list[float] = [start_vol]  # commanded-volume timeline
        self._seq = 0
        self.commanded: list[float] = []

    async def set_vol(self, db: float) -> None:
        self._vol = db
        self.commanded.append(db)
        self._history.append(db)

    async def next_samples(self) -> list[LevelSample]:
        # The mic hears the volume commanded `lag_ticks` ago (transport delay).
        idx = max(0, len(self._history) - 1 - self.lag_ticks)
        heard_vol = self._history[idx]
        mic = heard_vol + self.gain_db
        # Below the noise floor the mic reads ambient, not signal.
        mic = max(mic, self.noise_floor_dbfs + 0.5 * (mic - self.noise_floor_dbfs))
        clip = self.clip_at_vol is not None and heard_vol >= self.clip_at_vol
        peak = 0.0 if clip else mic + 3.0
        self._seq += 1
        return [
            LevelSample(
                seq=self._seq,
                t_client_ms=self._seq * 100,
                rms_dbfs=mic,
                peak_dbfs=peak,
                clip=clip,
                agc_frozen=self.agc_frozen,
            )
        ]


async def _run(
    model: ChainModel,
    config: MeasurementRamp,
    *,
    clock: FakeClock | None = None,
    manual_lock_after: int | None = None,
    cancel_after: int | None = None,
):
    clock = clock or FakeClock()
    controller = RampController(session_id="test", config=config)
    tone = {"started": False, "cancelled": False}

    async def play_tone():
        tone["started"] = True

    def cancel_tone():
        tone["cancelled"] = True

    # Wrap next_samples to optionally trigger lock/cancel after N batches.
    counter = {"n": 0}
    base_next = model.next_samples

    async def next_samples():
        counter["n"] += 1
        if manual_lock_after is not None and counter["n"] == manual_lock_after:
            await controller.lock()
        if cancel_after is not None and counter["n"] == cancel_after:
            await controller.cancel()
        return await base_next()

    data = await controller.run(
        get_main_volume_db=lambda: _const(model._vol),
        set_main_volume_db=model.set_vol,
        play_continuous_tone=play_tone,
        cancel_tone=cancel_tone,
        next_samples=next_samples,
        noise_floor_dbfs=model.noise_floor_dbfs,
        clock=clock.now,
        sleep=clock.sleep,
    )
    return controller, data, tone


def _const(v):
    async def _c():
        return v

    return _c()


# --- config validation (the overshoot invariant can't be built violated) ------


def test_schema_version_pinned():
    assert LEVEL_EVENT_SCHEMA_VERSION == 1


def test_default_config_is_valid_and_pre_window_below_window():
    cfg = MeasurementRamp()
    assert cfg.pre_window_db is not None
    # The coarse staircase stops strictly below the window bottom.
    assert cfg.pre_window_db <= cfg.window_low_dbfs
    # Overshoot invariant holds with margin.
    assert cfg.ramp_rate * cfg.max_loop_latency_s < 0.5 * (
        cfg.window_high_dbfs - cfg.window_low_dbfs
    )


def test_overshoot_invariant_rejects_a_too_fast_ramp():
    with pytest.raises(ValueError, match="overshoot guard"):
        # 4 dB/step / 0.5 s * 2 s latency = 16 dB >> half of an 8 dB window.
        MeasurementRamp(step_db=4.0, step_interval_s=0.5, max_loop_latency_s=2.0)


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


# --- happy path: settle into the window and lock ------------------------------


@pytest.mark.asyncio
async def test_ramp_locks_in_window():
    # Gain such that the window [-20,-12] is reachable within the cap. With
    # gain=+2, mic=vol+2, so window bottom -20 needs vol=-22 (below cap floor?),
    # window is reached at vol in [-22,-14]. Cap for original -30 is -20..-6
    # clamped → dynamic_cap(-30) = clamp(-24, [-20,-6]) = -20. So vol can go to
    # -20 → mic -18, inside window.
    cfg = MeasurementRamp(settle_hold_s=0.5, max_loop_latency_s=0.5, confirm_k=3)
    model = ChainModel(gain_db=2.0, start_vol=-30.0, noise_floor_dbfs=-70.0)
    controller, data, tone = await _run(model, cfg)
    assert data.state == RampState.LOCKED
    assert data.locked_main_volume_db is not None
    # Locked mic level lands inside the window.
    assert data.settled_mic_dbfs is not None
    mic_at_lock = data.locked_main_volume_db + model.gain_db
    assert cfg.window_low_dbfs <= mic_at_lock <= cfg.window_high_dbfs
    assert tone["started"] and tone["cancelled"]


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
    cfg = MeasurementRamp(settle_hold_s=0.5, max_loop_latency_s=0.5)
    model = ChainModel(gain_db=gain_db, start_vol=original, noise_floor_dbfs=-75.0)
    controller, data, tone = await _run(model, cfg)
    cap = cfg.dynamic_cap(original)
    ceiling = min(cap, HARD_CEILING_DBFS)
    assert model.commanded, "controller must command at least the start volume"
    for v in model.commanded:
        assert v <= ceiling + 1e-9, (
            f"commanded {v} exceeds min(cap={cap}, hard_ceiling="
            f"{HARD_CEILING_DBFS})={ceiling} (gain={gain_db}, orig={original})"
        )
        assert v <= HARD_CEILING_DBFS + 1e-9


@pytest.mark.asyncio
async def test_settle_jump_never_overshoots_window_high():
    # A large gain would tempt a naive ramp to blast; the settle jump aims at the
    # window bottom, so even after the jump the mic level stays <= window_high.
    cfg = MeasurementRamp(settle_hold_s=0.5, max_loop_latency_s=0.5)
    model = ChainModel(gain_db=8.0, start_vol=-40.0, noise_floor_dbfs=-80.0)
    controller, data, tone = await _run(model, cfg)
    # Whatever it locked/ended at, the mic level implied by every commanded
    # volume never exceeds window_high by more than the modeled in-flight
    # overshoot (which is < half the window by construction).
    max_mic = max(v + model.gain_db for v in model.commanded)
    # Because the cap clamps first, the true bound is the cap's mic level.
    cap_mic = cfg.dynamic_cap(-40.0) + model.gain_db
    assert max_mic <= max(cap_mic, cfg.window_high_dbfs) + 1e-6


# --- SAFETY: clip aborts immediately ------------------------------------------


@pytest.mark.asyncio
async def test_clip_aborts_and_restores():
    cfg = MeasurementRamp(settle_hold_s=0.5, max_loop_latency_s=0.5)
    # Clip fires as soon as the ramp climbs above -35 dB.
    model = ChainModel(
        gain_db=2.0, start_vol=-50.0, noise_floor_dbfs=-80.0, clip_at_vol=-35.0
    )
    controller, data, tone = await _run(model, cfg)
    assert data.state == RampState.ABORTED
    assert data.error == "clip detected"
    # Restored to the original level (last commanded value).
    assert model.commanded[-1] == pytest.approx(data.original_main_volume_db)
    assert tone["cancelled"]


# --- SAFETY: maxed out when the amp is too quiet ------------------------------


@pytest.mark.asyncio
async def test_maxed_out_when_amp_too_quiet():
    cfg = MeasurementRamp(settle_hold_s=0.5, max_loop_latency_s=0.5)
    # Gain so negative that even at the cap the mic never reaches the window.
    model = ChainModel(gain_db=-30.0, start_vol=-40.0, noise_floor_dbfs=-90.0)
    controller, data, tone = await _run(model, cfg)
    assert data.state == RampState.MAXED_OUT
    cap = cfg.dynamic_cap(-40.0)
    # Ended at (not above) the cap.
    assert data.locked_main_volume_db is not None
    assert data.locked_main_volume_db <= cap + 1e-9
    for v in model.commanded:
        assert v <= cap + 1e-9


# --- SAFETY: safety timeout always fires --------------------------------------


@pytest.mark.asyncio
async def test_safety_timeout_fires_and_restores():
    # A model whose mic never crosses the pre-window AND never reaches the cap
    # quickly: gain so low the pre-window is unreachable but climbing is slow
    # enough the timeout fires first. Use a tiny step so the cap isn't hit.
    cfg_slow = MeasurementRamp(
        settle_hold_s=0.5,
        max_loop_latency_s=0.5,
        safety_timeout_s=1.0,
        step_db=0.1,
        step_interval_s=0.6,
    )
    model = ChainModel(gain_db=-100.0, start_vol=-50.0, noise_floor_dbfs=-200.0)
    controller, data, tone = await _run(model, cfg_slow)
    assert data.state == RampState.CANCELLED
    assert data.error is not None and "safety timeout" in data.error
    assert model.commanded[-1] == pytest.approx(data.original_main_volume_db)


# --- SAFETY: trust floor gates the lock ---------------------------------------


@pytest.mark.asyncio
async def test_below_trust_floor_never_locks():
    # Noise floor very high so every reading is within trust_margin of it → no
    # sample is trusted → the ramp climbs to the cap and maxes out, never locks.
    cfg = MeasurementRamp(
        settle_hold_s=0.5,
        max_loop_latency_s=0.5,
        trust_margin_db=10.0,
        safety_timeout_s=60.0,
    )
    # gain +2, but noise floor is only ~8 dB below the mic level at the cap, so
    # nothing ever clears noise_floor + 10.
    model = ChainModel(gain_db=2.0, start_vol=-40.0, noise_floor_dbfs=-24.0)
    controller, data, tone = await _run(model, cfg)
    assert data.state in (RampState.MAXED_OUT, RampState.CANCELLED)
    assert data.locked_main_volume_db is None or data.state == RampState.MAXED_OUT
    # Critically: it never LOCKED on an untrusted (ambient-dominated) reading.
    assert data.state != RampState.LOCKED


# --- agc_frozen=false is surfaced and never trusted ---------------------------


@pytest.mark.asyncio
async def test_agc_unfrozen_is_surfaced_and_not_trusted():
    cfg = MeasurementRamp(
        settle_hold_s=0.5, max_loop_latency_s=0.5, safety_timeout_s=60.0
    )
    model = ChainModel(
        gain_db=2.0, start_vol=-40.0, noise_floor_dbfs=-80.0, agc_frozen=False
    )
    controller, data, tone = await _run(model, cfg)
    # AGC-compressed samples are liveness-only, never trusted → no window lock.
    assert data.agc_frozen is False
    assert data.state != RampState.LOCKED


# --- manual lock + cancel -----------------------------------------------------


@pytest.mark.asyncio
async def test_manual_lock_trusts_the_user():
    cfg = MeasurementRamp(settle_hold_s=5.0, max_loop_latency_s=2.0)
    model = ChainModel(gain_db=2.0, start_vol=-40.0, noise_floor_dbfs=-80.0)
    # Lock manually after a couple of ticks, well before any settle.
    controller, data, tone = await _run(model, cfg, manual_lock_after=3)
    assert data.state == RampState.LOCKED
    assert data.locked_main_volume_db is not None
    # Manual lock never exceeds the cap either.
    cap = cfg.dynamic_cap(-40.0)
    for v in model.commanded:
        assert v <= cap + 1e-9


@pytest.mark.asyncio
async def test_cancel_restores_original():
    cfg = MeasurementRamp(settle_hold_s=5.0, max_loop_latency_s=2.0)
    model = ChainModel(gain_db=2.0, start_vol=-8.5, noise_floor_dbfs=-80.0)
    controller, data, tone = await _run(model, cfg, cancel_after=3)
    assert data.state == RampState.CANCELLED
    assert model.commanded[-1] == pytest.approx(-8.5)


# --- lock/cancel with no run in progress --------------------------------------


@pytest.mark.asyncio
async def test_lock_cancel_return_false_after_terminal():
    controller = RampController(session_id="t")
    controller.data.state = RampState.LOCKED
    assert await controller.lock() is False
    assert await controller.cancel() is False


# --- restore_listening_volume_if_ramped is idempotent -------------------------


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


# --- order of operations: quiet start before tone -----------------------------


@pytest.mark.asyncio
async def test_quiet_start_before_tone():
    cfg = MeasurementRamp(settle_hold_s=0.5, max_loop_latency_s=0.5)
    events: list[str] = []
    controller = RampController(session_id="order", config=cfg)
    model = ChainModel(gain_db=2.0, start_vol=-30.0, noise_floor_dbfs=-80.0)
    clock = FakeClock()

    orig_set = model.set_vol

    async def set_vol(db):
        events.append(f"set:{db:.0f}")
        await orig_set(db)

    async def play_tone():
        events.append("tone")

    def cancel_tone():
        events.append("cancel")

    await controller.run(
        get_main_volume_db=lambda: _const(-30.0),
        set_main_volume_db=set_vol,
        play_continuous_tone=play_tone,
        cancel_tone=cancel_tone,
        next_samples=model.next_samples,
        noise_floor_dbfs=-80.0,
        clock=clock.now,
        sleep=clock.sleep,
    )
    first_set = next(i for i, e in enumerate(events) if e.startswith("set:"))
    first_tone = next(i for i, e in enumerate(events) if e == "tone")
    assert first_set < first_tone
    assert events[first_set] == f"set:{cfg.start_db:.0f}"


# --- LevelSample parsing ------------------------------------------------------


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
