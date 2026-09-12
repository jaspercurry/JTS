# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0
"""Closed-loop behavior with a calibrated, clocked microphone feed."""
import asyncio
import math
import random
from types import SimpleNamespace

import pytest

from jasper.active_speaker import auto_level as level
from jasper.audio_measurement.calibration import MicSensitivity
from jasper.audio_measurement.ramp import LevelSample, SPL_CEILING_EXCEEDED


class Chain:
    def __init__(self, monkeypatch, *, slope=1.0, offset=95.0, limiter=math.inf,
                 current=-10.0, ambient=30.0, cap=0.0, unstable=False, clip=False,
                 jitter=None, jitter_floor_margin=0.0):
        self.slope, self.offset, self.limiter = slope, offset, limiter
        self.gain, self.ambient, self.cap = current, ambient, cap
        self.unstable, self.clip = unstable, clip
        self.jitter, self.jitter_floor_margin = jitter, jitter_floor_margin
        self.time = 0.0
        self.playing = False
        self.writes = []
        self.windows = 0
        self.peak_observed = -math.inf
        monkeypatch.setattr(level, 'time', SimpleNamespace(monotonic=lambda: self.time))
        monkeypatch.setattr(level, 'asyncio', SimpleNamespace(
            sleep=self.sleep, timeout=asyncio.timeout,
        ))

    async def sleep(self, seconds):
        self.time += seconds

    async def get(self):
        return self.gain

    async def set(self, gain):
        self.writes.append(gain)
        self.gain = gain
        return True

    async def play(self):
        self.playing = True

    async def stop(self):
        self.playing = False

    async def samples(self):
        observed = self.ambient
        if self.playing:
            signal = min(self.limiter, self.offset + self.slope * self.gain)
            observed = max(observed, signal)
            if self.jitter:
                buried, clear = self.jitter
                floor = self.ambient + level.MIC_RESPONSE_MIN_RISE_DB
                jitter = buried if signal < floor + self.jitter_floor_margin else clear
                observed += jitter * (-1) ** self.windows
            if self.unstable:
                observed += 2.0 * (-1) ** self.windows
        self.peak_observed = max(self.peak_observed, observed)
        self.windows += 1
        self.time += 0.501
        return [LevelSample(self.windows, 0, observed - 94.0, observed - 94.0, clip=self.clip)]

    async def run(self):
        return await level.level_to(75.0, tolerance_db=1.0, stop_db_spl=85.0,
            max_main_volume_db=self.cap, sensitivity=MicSensitivity(0.0),
            next_samples=self.samples, get_main_volume_db=self.get,
            set_main_volume_db=self.set, play=self.play, stop_playback=self.stop)


@pytest.mark.parametrize('slope', [0.8, 1.0, 1.2])
@pytest.mark.parametrize('offset', [85.0, 95.0, 105.0])
def test_converges_from_below_within_reading_budget(monkeypatch, slope, offset):
    chain = Chain(monkeypatch, slope=slope, offset=offset)
    result = asyncio.run(chain.run())
    assert result.status == 'converged'
    assert abs(result.leveled_db_spl - 75.0) <= 1.0
    assert len(result.readings) <= math.ceil(abs(75.0 - result.readings[0][1]) / level.MAX_STEP_DB) + 4
    assert result.gain_db == chain.gain
    assert not chain.playing
    assert all(b - a <= level.MAX_STEP_DB for a, b in zip(chain.writes, chain.writes[1:]))


@pytest.mark.parametrize('offset,jitter_floor_margin', [
    (78.0, 0.0), (80.0, 0.0), (82.0, 0.0), (84.0, 0.0), (90.0, 0.0),
    (80.0, 2.0),
])
@pytest.mark.parametrize('phase', [0, 1])
def test_buried_jitter_steps_up_without_settling(monkeypatch, phase, offset, jitter_floor_margin):
    chain = Chain(monkeypatch, ambient=52.0, offset=offset, jitter=(0.8, 0.2),
                  jitter_floor_margin=jitter_floor_margin)
    chain.windows = phase
    result = asyncio.run(chain.run())
    floor = chain.ambient + level.MIC_RESPONSE_MIN_RISE_DB
    buried = [(gain, reading) for gain, reading in result.readings if reading < floor]
    assert result.status == 'converged'
    assert abs(result.leveled_db_spl - 75.0) <= 1.0
    budget = math.ceil(abs(75.0 - result.readings[0][1]) / level.MAX_STEP_DB) + 4 + len(buried)
    assert len(result.readings) <= budget
    assert len(buried) >= 2
    assert result.readings[:len(buried)] == buried
    assert all(b[0] - a[0] == level.MAX_STEP_DB for a, b in zip(buried, buried[1:]))


def test_a_loud_room_uses_small_buried_steps(monkeypatch):
    result = asyncio.run(Chain(monkeypatch, ambient=68.0).run())
    assert result.status == 'converged'
    assert max(reading for _, reading in result.readings) <= 77.0


def test_an_unsettled_reading_above_target_steps_down(monkeypatch):
    chain = Chain(monkeypatch, unstable=True, offset=85.0)
    result = asyncio.run(chain.run())
    assert any(reading > 75.0 for _, reading in result.readings)
    for (gain, reading), (next_gain, _) in zip(result.readings, result.readings[1:]):
        if reading > 75.0:
            assert next_gain <= gain
    assert chain.peak_observed < 75.0 + level.MAX_STEP_DB


def test_random_non_hot_unsettled_climbs_never_step_up_when_high(monkeypatch):
    rng = random.Random(212)
    high_transitions = 0
    for _ in range(500):
        slope = rng.uniform(0.8, 2.0)
        target_gain = rng.uniform(-28.0, -6.0)
        chain = Chain(monkeypatch, slope=slope, offset=75.0 - slope * target_gain,
                      ambient=rng.uniform(30.0, 60.0), unstable=True,
                      jitter=(rng.uniform(0.6, 0.9), rng.uniform(0.1, 0.4)))
        chain.windows = rng.randrange(2)
        result = asyncio.run(chain.run())
        assert result.reason != level.SPL_CEILING_EXCEEDED
        for (gain, reading), (next_gain, _) in zip(result.readings, result.readings[1:]):
            if reading > 76.0:
                high_transitions += 1
                assert next_gain <= gain
    assert high_transitions > 0


@pytest.mark.parametrize("slope,offset", [(1.0, 120.0), (1.0, 124.0), (1.2, 132.0)])
def test_converges_downward_from_above(monkeypatch, slope, offset):
    chain = Chain(monkeypatch, slope=slope, offset=offset)
    result = asyncio.run(chain.run())
    assert result.status == 'converged'
    assert result.readings[0][1] == pytest.approx(offset - 40 * slope)
    assert len(result.readings) <= math.ceil(abs(75.0 - result.readings[0][1]) / level.MAX_STEP_DB) + 4
    assert result.gain_db < result.readings[0][0]


def test_random_linear_and_limiter_chains_respect_both_stops(monkeypatch):
    rng = random.Random(131)
    for _ in range(100):
        chain = Chain(monkeypatch, slope=rng.uniform(0.5, 2.0), offset=rng.uniform(70, 160),
                      limiter=rng.choice([math.inf, rng.uniform(45, 95)]), cap=rng.uniform(-30, 10))
        result = asyncio.run(chain.run())
        assert all(gain <= min(chain.cap, 0.0) for gain in chain.writes)
        if any(observed > 85.0 for _, observed in result.readings):
            assert result.reason == SPL_CEILING_EXCEEDED
        if result.leveled_db_spl is not None:
            assert result.leveled_db_spl <= 85.0
        assert not chain.playing


@pytest.mark.parametrize('kwargs,reason', [
    ({'limiter': 65.0}, level.REFUSE_LEVEL_UNREACHABLE),
    ({'slope': 0.0, 'offset': 30.0, 'cap': -20.0}, level.REFUSE_MIC_NOT_OBSERVING),
    ({'unstable': True, 'ambient': 68.0, 'offset': 114.0}, level.REFUSE_LEVEL_UNSETTLED),
    ({'ambient': 69.0}, level.REFUSE_AMBIENT_TOO_HIGH),
    ({'clip': True}, 'mic_clipping'),
    ({'offset': 140.0}, SPL_CEILING_EXCEEDED),
])
def test_coded_refusals_stop_playback(monkeypatch, kwargs, reason):
    chain = Chain(monkeypatch, **kwargs)
    result = asyncio.run(chain.run())
    assert result.status == 'refused'
    assert result.reason == reason
    assert result.leveled_db_spl is None
    assert not chain.playing


def test_one_loud_sample_cannot_hide_in_a_window_median(monkeypatch):
    chain = Chain(monkeypatch)
    original = chain.samples
    async def samples():
        normal = await original()
        return normal * 9 + ([LevelSample(100, 0, -8.0, -8.0)] if chain.playing else [])
    chain.samples = samples
    result = asyncio.run(chain.run())
    assert result.reason == SPL_CEILING_EXCEEDED
    assert result.readings[-1][1] == 86.0


@pytest.mark.parametrize('reading', [math.nan, math.inf, -math.inf])
def test_nonfinite_feed_cannot_drive_the_fader(monkeypatch, reading):
    chain = Chain(monkeypatch)
    async def samples():
        chain.time += 0.501
        return [LevelSample(1, 0, reading, reading)]
    chain.samples = samples
    result = asyncio.run(chain.run())
    assert result.reason == 'mic_feed_lost'
    assert len(chain.writes) == 1
    assert not chain.playing


def test_cancellation_stops_playback(monkeypatch):
    chain = Chain(monkeypatch)
    original = chain.samples
    async def samples():
        if chain.playing:
            raise asyncio.CancelledError
        return await original()
    chain.samples = samples
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(chain.run())
    assert not chain.playing


def test_a_quieter_room_is_remeasured_in_silence(monkeypatch):
    chain = Chain(monkeypatch, ambient=60.0)
    original = chain.samples
    silent = []
    async def samples():
        if chain.playing:
            chain.ambient = 30.0
        else:
            silent.append(chain.ambient)
        return await original()
    chain.samples = samples
    result = asyncio.run(chain.run())
    assert result.status == 'converged'
    assert result.ambient_db_spl == 30.0
    assert 60.0 in silent and 30.0 in silent


def test_a_stalled_feed_is_bounded_and_stops_playback(monkeypatch):
    chain = Chain(monkeypatch)
    monkeypatch.setattr(level, 'MIC_WINDOW_S', 0.01)
    async def samples():
        await asyncio.Future()
    chain.samples = samples
    result = asyncio.run(chain.run())
    assert result.reason == 'mic_feed_lost'
    assert not chain.playing


@pytest.mark.parametrize('current,cap', [(-55.0, -40.0), (-10.0, -55.0)])
def test_start_respects_current_gain_and_a_low_cap(monkeypatch, current, cap):
    chain = Chain(monkeypatch, current=current, cap=cap)
    asyncio.run(chain.run())
    assert chain.writes[0] == min(current, cap, level.START_FADER_DB)


def test_a_constant_mic_inside_the_band_still_needs_rise_above_ambient(monkeypatch):
    chain = Chain(monkeypatch, ambient=68.9, slope=0.0, offset=74.0)
    result = asyncio.run(chain.run())
    assert result.status == 'refused'
    assert result.leveled_db_spl is None


@pytest.mark.parametrize("rise,ambient,status", [(2, 70, "converged"), (12, 64, "refused")])
def test_ambient_and_convergence_use_the_configured_rise(monkeypatch, rise, ambient, status):
    monkeypatch.setenv("JASPER_SEAT_LEVEL_MIN_RISE_DB", str(rise))
    result = asyncio.run(Chain(monkeypatch, ambient=ambient, offset=115).run())
    assert result.status == status
    if status == "refused":
        assert result.reason == level.REFUSE_AMBIENT_TOO_HIGH
