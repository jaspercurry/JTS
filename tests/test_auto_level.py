# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0
"""Closed-loop behavior with one watched sweep per reading."""
import asyncio
import math
import random

import pytest

from jasper.active_speaker import auto_level as level
from jasper.audio_measurement.calibration import MicSensitivity
from jasper.audio_measurement.ramp import SPL_CEILING_EXCEEDED
from jasper.audio_measurement.wired_capture import WiredSplCeilingExceeded


class Chain:
    def __init__(self, monkeypatch, *, slope=1.0, offset=95.0, limiter=math.inf,
                 current=-10.0, ambient=30.0, cap=0.0, unstable=False,
                 jitter=None, jitter_floor_margin=0.0):
        self.slope, self.offset, self.limiter = slope, offset, limiter
        self.gain, self.ambient, self.cap = current, ambient, cap
        self.unstable = unstable
        self.jitter, self.jitter_floor_margin = jitter, jitter_floor_margin
        self.playing = False
        self.writes = []
        self.windows = 0
        self.peak_observed = -math.inf
    async def get(self):
        return self.gain

    async def set(self, gain):
        self.writes.append(gain)
        self.gain = gain
        return True

    async def read_ambient(self):
        assert not self.playing
        return self.ambient

    async def read_level(self):
        self.playing = True
        try:
            signal = min(self.limiter, self.offset + self.slope * self.gain)
            observed = max(self.ambient, signal)
            if self.jitter:
                buried, clear = self.jitter
                floor = self.ambient + level.MIC_RESPONSE_MIN_RISE_DB
                jitter = buried if signal < floor + self.jitter_floor_margin else clear
                observed += jitter * (-1) ** self.windows
            if self.unstable:
                observed += 2.0 * (-1) ** self.windows
            self.peak_observed = max(self.peak_observed, observed)
            self.windows += 1
            if observed > 85.0:
                raise WiredSplCeilingExceeded(observed, 85.0)
            return observed
        finally:
            self.playing = False

    async def run(self):
        return await level.level_to(75.0, tolerance_db=1.0, stop_db_spl=85.0,
            max_main_volume_db=self.cap, sensitivity=MicSensitivity(0.0),
            read_level=self.read_level, read_ambient=self.read_ambient, get_main_volume_db=self.get,
            set_main_volume_db=self.set)


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
    assert all(abs(b - a) <= level.MAX_STEP_DB for a, b in zip(chain.writes, chain.writes[1:]))


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


@pytest.mark.parametrize("second,status", [(75.5, "converged"), (75.51, "refused"), (74.49, "refused")])
def test_two_in_band_sweeps_hold_the_fader_and_must_agree(monkeypatch, second, status):
    chain = Chain(monkeypatch)
    readings = iter((75.0, second))
    async def read_level():
        return next(readings)
    chain.read_level = read_level
    result = asyncio.run(chain.run())
    assert result.status == status
    assert result.reason == (None if status == "converged" else level.REFUSE_LEVEL_UNSETTLED)
    assert result.readings == [(-40.0, 75.0), (-40.0, second)]
    assert chain.writes == [-40.0]


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
    ({'offset': 140.0}, SPL_CEILING_EXCEEDED),
])
def test_coded_refusals_stop_playback(monkeypatch, kwargs, reason):
    chain = Chain(monkeypatch, **kwargs)
    result = asyncio.run(chain.run())
    assert result.status == 'refused'
    assert result.reason == reason
    assert result.leveled_db_spl is None
    assert not chain.playing


@pytest.mark.parametrize('reading', [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize('source', ['read_level', 'read_ambient'])
def test_nonfinite_feed_cannot_drive_the_fader(monkeypatch, reading, source):
    chain = Chain(monkeypatch)
    async def read():
        return reading
    setattr(chain, source, read)
    result = asyncio.run(chain.run())
    assert result.reason == 'mic_feed_lost'
    assert len(chain.writes) == 1


def test_a_quieter_room_is_remeasured_in_silence(monkeypatch):
    chain = Chain(monkeypatch, ambient=60.0)
    original = chain.read_level
    async def read_level():
        chain.ambient = 30.0
        return await original()
    chain.read_level = read_level
    result = asyncio.run(chain.run())
    assert result.status == 'converged'
    assert result.ambient_db_spl == 30.0


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
