# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Calibrated seat-SPL leveling: the climb, the guards, the banked result.

Synthetic throughout — a fake clock, a fake confirming volume pair, a blocking
tone, and a mic that hears the POWER SUM of a room floor and a speaker that only
contributes while the tone plays. No ALSA, no CamillaDSP, no microphone.

That mic model is the point of this file. A speaker quieter than the room pins
the reading at ambient for the first steps, so the level does not track the
commanded volume down there — and every rule about "is the microphone actually
hearing this" has to survive that without either false-aborting a healthy chain
or passing a mic that is not listening.

What must hold, and what would break if it did not:

* the dBFS -> dB SPL conversion matches a hand-computed value from a real
  UMIK-2 header — every SPL decision downstream is this arithmetic;
* the step is the measured gap, saturated by the run's OWN bite —
  ``BITE_FRACTION`` of the span it has to sweep, a different question from the
  per-request cap ``calibration_level`` declares; the mutation test drops the
  saturation and shows a single step lunging the whole way;
* the start is low and uninformed on purpose, so jts3's 61 dB SPL / 75 dB SPL
  incident — 51 s of timeout at -31.6 dB — converges in seven readings and
  under ten audible seconds;
* a mic that is plugged in but NOT observing the speaker aborts the climb
  (``mic_not_observing``) instead of walking the volume to the ceiling; the
  mutation test disarms the guard and shows the ramp doing exactly that;
* a speaker quieter than the room is NOT aborted, and the control shows a
  first-reading-relative rule really would abort it;
* two steps that commanded the whole measured gap and still missed refuse with
  the measured dB-per-dB slope, rather than chasing a non-monotone chain;
* a stuck-constant mic never banks a reference, and the control shows that
  without the rise-against-ambient test it banks the ramp's own start volume;
* a measured level above the profile's commissioning ceiling aborts, and an
  unreachable target refuses and banks NOTHING;
* the household volume is restored on every exit path — converged, refused,
  aborted, raised, and CANCELLED — and the hold is visible to the shared
  recovery machinery;
* only a reading inside the band that rose clear of the room writes a
  reference, its document keeps its exact shape, and the reader accepts it
  only inside the safe envelope.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time

import pytest

from jasper.active_speaker import seat_level_ramp as slr
from jasper.active_speaker import session_volume_plan as svp
from jasper.active_speaker.session_volume_plan import (
    SessionVolumePlan,
    live_measurement_session,
)
from jasper.active_speaker.seat_level_reference import (
    SeatLevelTarget,
    SeatLevelTargetError,
    load_seat_level_reference,
    seat_level_reference_volume_db,
    write_seat_level_reference,
)
from jasper.audio_measurement.calibration import (
    CALIBRATOR_REFERENCE_DB_SPL,
    MicSensitivity,
    parse_calibration_sensitivity,
)
from jasper.audio_measurement.ramp import LevelSample, capped_gap_step_db

# The real header of the household UMIK-2 (serial 810-8494), verbatim, plus two
# curve rows so the file is a realistic whole.
UMIK2_CAL_TEXT = (
    '"Sens Factor =-12.07dB, AGain =18dB, SERNO: 8108494"\n'
    "10.054\t-6.6664\n"
    "10.179\t-6.4980\n"
)
UMIK2 = MicSensitivity(sens_factor_db=-12.07, analog_gain_db=18.0, serial="8108494")


class FakeWindow:
    """Recording stand-in for ``coordinator.measurement_window()``.

    The real window talks to jasper-mux over UDS, jasper-voice over UDS, and
    jasper-control over HTTP — none of which exist here. It records enter/exit
    so a test can assert the ramp really runs INSIDE it, and can be told to
    fail on either boundary.
    """

    def __init__(
        self,
        log: list[str],
        *,
        enter_error: Exception | None = None,
        exit_error: Exception | None = None,
    ) -> None:
        self.log = log
        self.enter_error = enter_error
        self.exit_error = exit_error

    async def __aenter__(self):
        if self.enter_error is not None:
            raise self.enter_error
        self.log.append("window_enter")
        return None

    async def __aexit__(self, *exc):
        self.log.append("window_exit")
        if self.exit_error is not None:
            raise self.exit_error
        return False


class WindowRecorder:
    """What the autouse fixture hands back: the boundary log + the kwargs."""

    def __init__(self) -> None:
        self.log: list[str] = []
        self.kwargs: list[dict] = []


@pytest.fixture(autouse=True)
def measurement_window_log(monkeypatch):
    """Every ramp in this file runs under a recorded, in-process window."""
    recorder = WindowRecorder()

    def _window(**kw):
        recorder.kwargs.append(kw)
        return FakeWindow(recorder.log)

    monkeypatch.setattr(slr, "measurement_window", _window)
    return recorder


@pytest.fixture(autouse=True)
def _control_unreachable(monkeypatch):
    """No jasper-control in this suite, so the door uses its documented
    fallback: the durable volume statefile. Pinned rather than left to whether
    something happens to be listening on 8780 on the machine running the tests.
    """
    monkeypatch.setattr(
        svp, "read_measurement_hold", lambda: None,
    )

# The household level the latch records and restores. Below anything the ramp
# commands, so a test that reads the restored value cannot confuse it with a
# volume the climb happened to pass through.
HOUSEHOLD_VOLUME_DB = -44.0
CEILING_DB = -6.0
# A realistically quiet listening room. Every rig's chain gain is expressed
# against this floor, so an unrealistic one would model a speaker no room ever
# meets and make the rise assertions meaningless.
ROOM_DB_SPL = 45.0
SPL_CEILING = 85.0
TARGET = SeatLevelTarget(target_db_spl=77.5, tolerance_db=2.5)
# One bite for the shared rig, read from the module so a fraction change cannot
# leave these assertions asserting a stale number.
DEFAULT_BITE_DB = slr.bite_db(
    start_db=slr.SEAT_LEVEL_START_DB, ceiling_db=CEILING_DB
)


class FakeClock:
    """Deterministic monotonic clock advanced by the fake sleep."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += max(seconds, 0.01)
        await asyncio.sleep(0)


class BlockingTone:
    """The tone contract: play() blocks until cancel() (a TonePlayer shape)."""

    def __init__(self) -> None:
        self.started = False
        self.cancelled = False
        self._event = asyncio.Event()

    async def play(self) -> None:
        self.started = True
        await self._event.wait()

    def cancel(self) -> None:
        self.cancelled = True
        self._event.set()


class Volume:
    """A confirming main-volume pair: readback always echoes the last set."""

    def __init__(self, value: float = HOUSEHOLD_VOLUME_DB) -> None:
        self.value = float(value)
        self.commanded: list[float] = []

    async def set(self, db: float) -> bool:
        self.value = float(db)
        self.commanded.append(self.value)
        return True

    async def get(self) -> float:
        return self.value


def _power_sum_db(*levels: float) -> float:
    """Incoherent sum of independent sources, the way a real mic hears them."""
    return 10.0 * math.log10(sum(10.0 ** (level / 10.0) for level in levels))


class Mic:
    """A modelled mic feed: the room's ambient floor plus whatever plays.

    The speaker contributes only while the tone is running (``mic_dbfs =
    commanded + gain_db``), and the mic hears the POWER SUM of that and the
    room. That is what makes the pre-tone ambient window meaningful and what
    reproduces the shape the runaway guard has to tolerate: a speaker quieter
    than the room pins the reading at ambient for the first several steps, so
    the level does NOT track the commanded volume 1:1 down there.

    ``deaf=True`` models ONE shape of the failure the guard exists for: the
    device is open and delivering samples, but they never respond to the
    speaker. It pins at ``stuck_dbfs`` forever, so its ambient reading IS its
    signal reading and its rise is zero. That is the CONSTANT sub-case only —
    a mic on the wrong card hears a room that wanders and is not modelled here;
    ``WrongCardMic`` below is that one, and it is the shape a premise about
    constants got wrong.
    """

    def __init__(
        self,
        volume: Volume,
        tone: BlockingTone,
        *,
        gain_db: float = -10.0,
        ambient_dbfs: float | None = None,
        deaf: bool = False,
        stuck_dbfs: float = -75.0,
        nan_once_playing: bool = False,
    ) -> None:
        self._volume = volume
        self._tone = tone
        self.gain_db = gain_db
        self.ambient_dbfs = (
            UMIK2.dbfs_from_db_spl(ROOM_DB_SPL)
            if ambient_dbfs is None
            else ambient_dbfs
        )
        self.deaf = deaf
        self.stuck_dbfs = stuck_dbfs
        self.nan_once_playing = nan_once_playing
        self._seq = 0

    def _rms_dbfs(self) -> float:
        if self.deaf:
            return self.stuck_dbfs
        if not self._tone.started:
            return self.ambient_dbfs
        if self.nan_once_playing:
            return float("nan")
        return _power_sum_db(self.ambient_dbfs, self._volume.value + self.gain_db)

    async def next_samples(self) -> list[LevelSample]:
        self._seq += 1
        rms = self._rms_dbfs()
        return [
            LevelSample(
                seq=self._seq,
                t_client_ms=self._seq * 10,
                rms_dbfs=rms,
                peak_dbfs=rms + 3.0,
                clip=False,
                agc_frozen=True,
            )
        ]


def gain_for_seat_spl(db_spl: float, *, at_volume_db: float) -> float:
    """Chain gain that puts the seat at ``db_spl`` when main volume is given."""
    return UMIK2.dbfs_from_db_spl(db_spl) - at_volume_db


async def _level(
    *,
    mic: Mic,
    volume: Volume,
    tone: BlockingTone,
    tmp_path,
    target: SeatLevelTarget = TARGET,
    sensitivity: MicSensitivity = UMIK2,
    max_main_volume_db: float = CEILING_DB,
    spl_ceiling_db_spl: float = SPL_CEILING,
):
    clock = FakeClock()
    result = await slr.run_seat_level_ramp(
        target=target,
        sensitivity=sensitivity,
        max_main_volume_db=max_main_volume_db,
        spl_ceiling_db_spl=spl_ceiling_db_spl,
        get_main_volume_db=volume.get,
        set_main_volume_db=volume.set,
        play_continuous_tone=tone.play,
        cancel_tone=tone.cancel,
        next_samples=mic.next_samples,
        clock=clock.now,
        sleep=clock.sleep,
        volume_state_path=tmp_path / "seat_level_volume.json",
        reference_state_path=tmp_path / "seat_level_reference.json",
    )
    return result


def _far_chain_gain_db() -> float:
    """A chain that needs more capped bites than the miss budget allows misses.

    At the start it measures 5 dB UNDER the room, so the first bites are
    ambient-dominated -- and the band is ~37 dB up from there.
    """
    return gain_for_seat_spl(ROOM_DB_SPL - 5.0, at_volume_db=slr.SEAT_LEVEL_START_DB)


def _rig(**mic_kwargs) -> tuple[Volume, BlockingTone, Mic]:
    """A speaker, a tone, and a mic that hears both the room and the speaker."""
    volume = Volume()
    tone = BlockingTone()
    return volume, tone, Mic(volume, tone, **mic_kwargs)


# --- the conversion ---------------------------------------------------------


def test_sensitivity_parses_the_real_umik2_header():
    parsed = parse_calibration_sensitivity(UMIK2_CAL_TEXT)
    assert parsed == UMIK2


def test_sensitivity_parses_a_umik1_header_without_analog_gain():
    # UMIK-1 files carry no AGain field and a leading-dot sensitivity.
    parsed = parse_calibration_sensitivity(
        '"Sens Factor =-.9099dB, SERNO: 7031234"\n20 0.0\n30 0.1\n'
    )
    assert parsed == MicSensitivity(
        sens_factor_db=-0.9099, analog_gain_db=None, serial="7031234"
    )


def test_sensitivity_absent_is_none_never_a_guess():
    # A curve-only file has NO absolute reference. Returning any number here
    # would silently mis-scale every SPL decision downstream.
    assert parse_calibration_sensitivity("20 0.0\n30 0.1\n") is None
    assert parse_calibration_sensitivity("") is None


def test_db_spl_conversion_matches_hand_computation():
    # By hand, from the fixture header: dB SPL = dBFS - (-12.07) + 94, so
    #   -31.07 dBFS -> -31.07 + 12.07 + 94 = 75.00 dB SPL
    #   -26.07 dBFS -> -26.07 + 12.07 + 94 = 80.00 dB SPL
    assert UMIK2.db_spl_from_dbfs(-31.07) == pytest.approx(75.0)
    assert UMIK2.db_spl_from_dbfs(-26.07) == pytest.approx(80.0)
    assert UMIK2.dbfs_from_db_spl(75.0) == pytest.approx(-31.07)
    # The definition self-check: the calibrator level reads exactly the sens
    # factor, whatever the sens factor is.
    assert UMIK2.dbfs_from_db_spl(CALIBRATOR_REFERENCE_DB_SPL) == pytest.approx(-12.07)


def test_conversion_round_trips():
    for db_spl in (45.0, 65.0, 77.5, 85.0):
        assert UMIK2.db_spl_from_dbfs(
            UMIK2.dbfs_from_db_spl(db_spl)
        ) == pytest.approx(db_spl)


# --- the target band --------------------------------------------------------


def test_target_band_top_must_clear_the_commissioning_ceiling():
    # 84 +/- 2 tops out at 86, above the profile's 85 dB SPL ceiling. Refused
    # rather than silently clipped: a clipped band would converge somewhere the
    # operator never asked for and bank THAT as the reference.
    with pytest.raises(SeatLevelTargetError, match="ceiling"):
        SeatLevelTarget(target_db_spl=84.0, tolerance_db=2.0).validate(
            ceiling_db_spl=85.0
        )
    SeatLevelTarget(target_db_spl=77.5, tolerance_db=2.5).validate(ceiling_db_spl=85.0)


@pytest.mark.parametrize("tolerance", [0.0, -1.0, float("nan")])
def test_target_tolerance_must_be_a_real_positive_width(tolerance):
    with pytest.raises(SeatLevelTargetError):
        SeatLevelTarget(target_db_spl=77.5, tolerance_db=tolerance).validate(
            ceiling_db_spl=85.0
        )


# --- the shared climb policy ------------------------------------------------


def test_the_step_is_the_measured_gap():
    # 62.3 dB SPL measured, 75 wanted: the step is the 12.7 dB that remain,
    # saturated by the cap. No staircase rung size is guessed anywhere.
    assert capped_gap_step_db(measured_db=62.3, target_db=75.0, cap_db=100.0) == (
        pytest.approx(12.7)
    )
    assert capped_gap_step_db(measured_db=67.4, target_db=75.0, cap_db=10.0) == (
        pytest.approx(7.6)
    )
    # Uncapped by default: a caller whose own geometry bounds the jump passes
    # no cap (the phone-relay kernel's settled jump).
    assert capped_gap_step_db(measured_db=0.0, target_db=90.0) == pytest.approx(90.0)


def test_the_cap_saturates_upward_only():
    # Up: capped. Down: never — a downward move reduces risk, the same
    # asymmetry calibration_level states for its own upward_step_limit_db.
    assert capped_gap_step_db(measured_db=40.0, target_db=75.0, cap_db=10.0) == (
        pytest.approx(10.0)
    )
    assert capped_gap_step_db(measured_db=95.0, target_db=75.0, cap_db=10.0) == (
        pytest.approx(-20.0)
    )


def test_the_bite_is_a_fraction_of_the_runs_own_span():
    """Hardware-independent by construction, which a dB constant cannot be.

    An unknown amplifier moves WHERE inside the span the speaker becomes
    audible; it does not change the span. So the bite count to sweep any chain
    is the same whatever the gain, which is the whole reason this is a fraction
    and not a number of dB.
    """
    assert slr.bite_db(start_db=-50.0, ceiling_db=-6.8) == pytest.approx(
        slr.BITE_FRACTION * 43.2
    )
    # Same fraction of a span half as wide is half as big -- and either way the
    # sweep takes at most ceil(1 / BITE_FRACTION) bites.
    narrow = slr.bite_db(start_db=-50.0, ceiling_db=-28.4)
    assert narrow == pytest.approx(slr.bite_db(start_db=-50.0, ceiling_db=-6.8) / 2)
    for ceiling in (-6.8, -28.4, 0.0, -40.0):
        span = ceiling - (-50.0)
        bite = slr.bite_db(start_db=-50.0, ceiling_db=ceiling)
        assert math.ceil(span / bite) == math.ceil(1 / slr.BITE_FRACTION)


def test_a_degenerate_span_yields_no_bite_rather_than_a_divide_by_zero():
    assert slr.bite_db(start_db=-50.0, ceiling_db=-50.0) == 0.0
    assert slr.bite_db(start_db=-6.8, ceiling_db=-50.0) == 0.0


def test_the_climb_bite_is_deliberately_not_the_calibration_step_limit():
    """Two questions, two vocabularies — recorded so it does not read as drift.

    ``calibration_level.AUDIBLE_RAMP_STEP_DB`` clamps ONE call to the
    calibration ``set`` endpoint: a per-request bound on an operator-driven
    jump. The climb bite is sized to the span this run has to sweep. An earlier
    draft of this pass borrowed the calibration constant as its cap; that made
    the bite a guess about a stranger's amplifier, which is exactly what the
    range-fraction exists to avoid.
    """
    from jasper.active_speaker.calibration_level import AUDIBLE_RAMP_STEP_DB

    assert AUDIBLE_RAMP_STEP_DB == 10.0  # unchanged; this PR does not touch it
    assert not hasattr(slr, "AUDIBLE_RAMP_STEP_DB")
    # ...and the bite genuinely differs from it on a real span.
    assert slr.bite_db(start_db=-50.0, ceiling_db=-6.8) != pytest.approx(
        AUDIBLE_RAMP_STEP_DB
    )


def test_the_kernel_jump_is_the_same_policy():
    """The phone-relay kernel's settled jump computes the shared step too.

    ``RampController._apply_jump`` aims at the window midpoint from a settled
    read; that is ``capped_gap_step_db`` with no cap. Pinned so a future edit
    cannot quietly fork a second arithmetic for the same question.
    """
    import inspect

    from jasper.audio_measurement.ramp import RampController

    source = inspect.getsource(RampController._apply_jump)
    assert "capped_gap_step_db(" in source


# --- the window conversion, the ceiling, and the start ----------------------


def test_the_window_is_the_band_converted_through_the_mic():
    low, high = slr.validate_seat_level_window(target=TARGET, sensitivity=UMIK2)
    assert low == pytest.approx(-31.07)
    assert high == pytest.approx(-26.07)


def test_the_window_refuses_a_band_the_mic_cannot_capture():
    # A mic gained so hot it already reads +20 dBFS at the 94 dB SPL calibrator
    # clips long before 80 dB SPL: 80 + 20 - 94 = +6 dBFS, past full scale.
    hot = MicSensitivity(sens_factor_db=20.0)
    with pytest.raises(
        slr.SeatLevelRampError, match=slr.REFUSE_SPL_TARGET_UNCAPTURABLE
    ):
        slr.validate_seat_level_window(target=TARGET, sensitivity=hot)


@pytest.mark.parametrize(
    "ceiling_db",
    [
        pytest.param(-55.0, id="below_the_start"),
        pytest.param(slr.SEAT_LEVEL_START_DB, id="at_the_start"),
        pytest.param(float("nan"), id="not_a_number"),
    ],
)
def test_a_ceiling_with_no_room_to_climb_refuses(tmp_path, ceiling_db):
    """One comparison, two failures — and a NaN is the second one.

    ``not start_db < ceiling_db`` is False for a ceiling at or under the start
    AND for a non-finite one, because every NaN comparison is False. A ramp with
    nowhere to climb must say so rather than command a single volume.
    """
    volume, tone, mic = _rig(gain_db=-10.0)
    with pytest.raises(
        slr.SeatLevelRampError, match=slr.REFUSE_VOLUME_CEILING_TOO_LOW
    ):
        asyncio.run(
            _level(
                mic=mic,
                volume=volume,
                tone=tone,
                tmp_path=tmp_path,
                max_main_volume_db=ceiling_db,
            )
        )
    assert not tone.started
    assert not (tmp_path / "seat_level_reference.json").exists()


def test_full_scale_clamps_a_headroom_ceiling_that_asks_for_gain():
    """The rail that survives the 2026-08-23 de-nanny, pinned (#2910).

    ``unsegmented_stimulus_ceiling_db`` is digital headroom now, so a quiet
    stimulus legitimately asks for a ceiling ABOVE 0 dB — a -12 dBFS program
    has 12 dB of room. The main volume has none: 0 dB is full scale, the
    ``devices.volume_limit`` the graph ships with, and the kernel's own rail.
    The ramp must never command above it however much headroom the stimulus
    has.

    Mutation guard: drop ``min(..., HARD_CEILING_DBFS)`` from
    ``seat_level_ceiling_db`` and this asks the fader for +12 dB.
    """
    from jasper.audio_measurement.ramp import HARD_CEILING_DBFS

    assert slr.seat_level_ceiling_db(12.0) == 0.0
    assert HARD_CEILING_DBFS == 0.0
    # ...and an honest sub-rail ceiling is passed through untouched.
    assert slr.seat_level_ceiling_db(-6.8) == pytest.approx(-6.8)


def test_the_start_is_low_and_uninformed_on_purpose(tmp_path):
    """The bite size, not the start, is what makes the pass fast.

    A start derived from the measured room was tried and dropped: from -50 dB
    the bites still cross a 61 dB SPL room in five of them, so the derivation
    bought a second and cost a constant, a helper, and a refusal. Keeping the
    start low is also what makes the bite fraction meaningful -- it is a
    fraction of the span BETWEEN this floor and the ceiling.
    """
    result, _volume, _tone, _clock = _jts3_pass(tmp_path)
    assert result.ramp["start_db"] == pytest.approx(slr.SEAT_LEVEL_START_DB)
    assert result.ramp["steps"][0]["volume_db"] == pytest.approx(
        slr.SEAT_LEVEL_START_DB
    )


def test_the_watchdog_prices_this_pass_not_a_continuous_climb():
    """Derived from the ACTUAL start and the ACTUAL step cap.

    The retired kernel budgeted ``span / ramp_rate`` — a continuous climb —
    for a walk that was settle-gated, which is how a 51 s budget expired 25 dB
    below its own ceiling. This prices the readings the pass really takes.
    """
    seconds = slr._watchdog_seconds(start_db=-50.0, ceiling_db=-6.8)
    # Any span is ceil(1 / BITE_FRACTION) bites; plus the ambient reading and
    # the two allowed misses, that is a fixed reading count -- each priced at
    # the MOST one reading can spend, which since #2919 is the settle timeout
    # rather than a fixed drain-plus-window.
    bites = math.ceil(1 / slr.BITE_FRACTION)
    readings = 1 + bites + slr.MAX_MISSED_FULL_STEPS + slr.REMEASURE_READINGS
    assert seconds == pytest.approx(
        readings * slr.SETTLE_TIMEOUT_S + slr.WATCHDOG_SLACK_S
    )
    # The silent re-measure is PRICED, not absorbed by the slack: a budget that
    # leaned on slack for a reading the pass can deliberately spend would be the
    # retired kernel's mistake in miniature.
    assert slr.REMEASURE_READINGS == 1
    # ...and because the bite scales with the span, the budget is the SAME for a
    # wider one: the bite count is what the watchdog prices, and it is fixed.
    assert slr._watchdog_seconds(start_db=-70.0, ceiling_db=-6.8) == pytest.approx(
        seconds
    )
    # The budget FOLLOWS the settle timeout, so an operator who widens it does
    # not get the backstop firing before the honest per-reading refusal can --
    # which is the failure the retired kernel had in the other direction.
    assert slr._watchdog_seconds(
        start_db=-50.0, ceiling_db=-6.8, settle_timeout_s=2 * slr.SETTLE_TIMEOUT_S
    ) == pytest.approx(seconds + readings * slr.SETTLE_TIMEOUT_S)


# --- the incident: a 61 dB SPL room, a 75 dB SPL target ---------------------


JTS3_AMBIENT_DB_SPL = 61.0
JTS3_CEILING_DB = -6.8
JTS3_TARGET = SeatLevelTarget(target_db_spl=75.0, tolerance_db=2.5)
# captures/new-horn-2026-08: 75 dB SPL was predicted at -12.099 dB main volume,
# so the chain maps commanded volume to seat SPL as `volume + 87.099`.
JTS3_GAIN_DB = UMIK2.dbfs_from_db_spl(75.0) - (-12.099)
_JTS3_BITE_DB = slr.bite_db(
    start_db=slr.SEAT_LEVEL_START_DB, ceiling_db=JTS3_CEILING_DB
)


class StampingTone(BlockingTone):
    """A tone that records when it started, so audible seconds are measurable."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__()
        self._clock = clock
        self.started_at: float | None = None
        self.cancelled_at: float | None = None

    async def play(self) -> None:
        self.started_at = self._clock.now()
        await super().play()

    def cancel(self) -> None:
        if self.cancelled_at is None:
            self.cancelled_at = self._clock.now()
        super().cancel()


def _jts3_pass(tmp_path, *, gain_db: float = JTS3_GAIN_DB):
    """The jts3 rig: a 61 dB SPL room, the measured chain, the resolved stop.

    Synthetic like the rest of this file — a MODELLED mic, not a replayed
    trace — so it characterizes today's ramp and takes today's commissioning
    stop (``SPL_CEILING``, the ruled 85). The frozen pre-ruling evidence is the
    ``NEW_HORN_*`` replay below, which is a different thing and says so.
    """
    clock = FakeClock()
    volume = Volume()
    tone = StampingTone(clock)
    mic = Mic(
        volume,
        tone,
        gain_db=gain_db,
        ambient_dbfs=UMIK2.dbfs_from_db_spl(JTS3_AMBIENT_DB_SPL),
    )
    result = asyncio.run(
        slr.run_seat_level_ramp(
            target=JTS3_TARGET,
            sensitivity=UMIK2,
            max_main_volume_db=JTS3_CEILING_DB,
            spl_ceiling_db_spl=SPL_CEILING,
            get_main_volume_db=volume.get,
            set_main_volume_db=volume.set,
            play_continuous_tone=tone.play,
            cancel_tone=tone.cancel,
            next_samples=mic.next_samples,
            clock=clock.now,
            sleep=clock.sleep,
            volume_state_path=tmp_path / "seat_level_volume.json",
            reference_state_path=tmp_path / "seat_level_reference.json",
        )
    )
    return result, volume, tone, clock


def test_the_noisy_room_that_timed_out_now_converges_in_seven_readings(tmp_path):
    """The incident, reproduced and fixed.

    On 2026-08-22 this exact condition — ambient 61 dB SPL, target 75 dB SPL,
    ceiling -6.8 dB — refused ``ramp_timeout`` after 51 s at -31.6 dB, still
    25 dB under its own ceiling, because the ladder started at -50 dB (~26 dB
    UNDER the room) and its noise gate discarded 1138 of 1194 samples while the
    clock ran. The gap-stepped ramp starts under anything that could hurt and
    bites its way up, landing in the band in seven readings.
    """
    result, _volume, tone, _clock = _jts3_pass(tmp_path)

    assert result.status == "converged", (result.reason, result.detail)
    steps = result.ramp["steps"]
    assert len(steps) == 7, steps
    assert result.ramp["start_db"] == pytest.approx(slr.SEAT_LEVEL_START_DB)
    assert result.ramp["bite_db"] == pytest.approx(_JTS3_BITE_DB, abs=0.01)
    # Full bites while buried in the room, then steps aimed at the measured gap
    # once the speaker emerges from it.
    volumes = [step["volume_db"] for step in steps]
    rises = [b - a for a, b in zip(volumes, volumes[1:])]
    assert volumes[0] == pytest.approx(slr.SEAT_LEVEL_START_DB)
    assert rises[:4] == [pytest.approx(_JTS3_BITE_DB)] * 4
    assert rises[-1] < _JTS3_BITE_DB  # the closing step is the measured gap
    assert -14.0 < volumes[-1] < -12.0
    assert JTS3_TARGET.low_db_spl <= result.measured_db_spl <= JTS3_TARGET.high_db_spl
    assert -14.0 < result.reference_volume_db < -12.0
    # Under the ceiling and under the commissioning stop, throughout.
    assert max(step["volume_db"] for step in steps) < JTS3_CEILING_DB
    assert max(step["observed_db_spl"] for step in steps) < SPL_CEILING


def test_the_noisy_room_pass_is_audible_for_under_ten_seconds(tmp_path):
    """The owner's requirement, measured rather than asserted.

    Seven readings, each two windows on a chain that answers a step at once.
    The ambient reading before them is SILENT — nothing plays — so it is not
    audible time at all.

    This is also the cost check on the #2919 stability wait: settling on
    AGREEMENT rather than on a guessed drain leaves a still chain paying the
    same second per reading it always paid, so the owner's ten-second budget
    survives the honesty fix. Only a level that is genuinely still moving buys
    more windows, which is the whole point.
    """
    result, _volume, tone, clock = _jts3_pass(tmp_path)

    assert tone.started_at is not None and tone.cancelled_at is not None
    audible_s = tone.cancelled_at - tone.started_at
    assert audible_s < 10.0
    # This modelled chain answers a step instantly, so every reading settles in
    # the minimum TWO windows -- asserted off the receipt rather than assumed.
    assert [step["windows"] for step in result.ramp["steps"]] == [2] * 7
    # Seven readings account for it, plus the fade-before-tone-kill that follows
    # the last one -- which is audible time as well, and is bounded by the fade's
    # own step size.
    readings_s = 7 * (2 * slr.MIC_WINDOW_S)
    fade_s = (abs(-12.0 - slr.FADE_FLOOR_DB) / slr.FADE_STEP_DB) * slr.FADE_STEP_S
    assert readings_s <= audible_s <= readings_s + fade_s + slr.MIC_WINDOW_S


def test_no_sample_is_discarded_for_being_quieter_than_the_room(tmp_path):
    """The starvation the noise gate caused, pinned as its absence.

    At the start the speaker measures far under the 61 dB SPL room, so the
    first reading is ambient-dominated — the exact population the
    retired kernel's trust floor threw away. Here it is EVIDENCE: it is what
    sizes the first step.
    """
    result, _volume, _tone, _clock = _jts3_pass(tmp_path)
    first = result.ramp["steps"][0]
    # Below the retired trust floor (ambient + 10 dB) and still counted.
    assert first["rise_db"] < 10.0
    assert first["samples"] > 0
    assert first["gap_db"] > _JTS3_BITE_DB


# --- the 2026-08-23 new-horn refusals: the trace, and what it rules out -----


# Both refused runs, verbatim from captures/new-horn-2026-08/bringup/
# 83-seatlevel-run1-refused.json and 84-seatlevel-run2-refused.json. Each entry
# is one BANKED reading: the median of one settle window. The sixth value below
# is deliberately NOT in that tuple, because it is not a reading -- it is the
# single sample that tripped the commissioning stop and abandoned its window
# before any median was taken.
NEW_HORN_TARGET = SeatLevelTarget(target_db_spl=75.0, tolerance_db=2.5)
# Frozen at the stop those runs actually met. The owner raised the stop to 85
# on 2026-08-23 and no shipped preset declares 80 any more, but this replay
# reproduces the RECORDED runs, not today's profile -- so it passes its own
# ceiling explicitly rather than resolving one. The live value has its own
# owner and its own test (tests/test_active_speaker_safety_envelope_ssot.py).
NEW_HORN_STOP_DB_SPL = 80.0
NEW_HORN_CEILING_DB = 0.0
NEW_HORN_BITE_DB = slr.bite_db(
    start_db=slr.SEAT_LEVEL_START_DB, ceiling_db=NEW_HORN_CEILING_DB
)
NEW_HORN_STOP_VOLUME_DB = -12.5

NEW_HORN_RUNS = {
    "run1": {
        "ambient_db_spl": 49.65,
        "readings": (
            (-50.0, 50.78),
            (-42.5, 53.45),
            (-35.0, 52.33),
            (-27.5, 57.90),
            (-20.0, 64.61),
        ),
        "stop_sample_db_spl": 80.50,
        "slope_estimate": 0.895,
        # Every consecutive pair with the room's power taken back out, and the
        # stop sample measured against the previous window's median. Both are
        # quoted in prose below, so both are pinned here rather than left to
        # rot away from the readings above.
        "room_subtracted_slopes": (0.897, -0.286, 1.098, 0.970),
        "stop_sample_slope": 2.119,
    },
    "run2": {
        "ambient_db_spl": 47.82,
        "readings": (
            (-50.0, 49.01),
            (-42.5, 50.60),
            (-35.0, 54.81),
            (-27.5, 58.84),
            (-20.0, 64.27),
        ),
        "stop_sample_db_spl": 80.90,
        "slope_estimate": 0.725,
        "room_subtracted_slopes": (0.605, 0.866, 0.619, 0.758),
        "stop_sample_slope": 2.217,
    },
}


def _room_subtracted_db(total_db_spl: float, room_db_spl: float) -> float:
    """The speaker's own level, with the room's power taken back out."""
    power = 10.0 ** (total_db_spl / 10.0) - 10.0 ** (room_db_spl / 10.0)
    return 10.0 * math.log10(power) if power > 0.0 else float("-inf")


def _consecutive_slopes(readings) -> list[float]:
    """dB SPL per commanded dB between consecutive readings."""
    return [
        (b_spl - a_spl) / (b_db - a_db)
        for (a_db, a_spl), (b_db, b_spl) in zip(readings, readings[1:])
    ]


class TracedMic(Mic):
    """A mic that replays ONE recorded run instead of modelling a chain.

    Every other mic in this file is a model: a room plus a speaker whose level
    tracks the commanded volume by a fixed gain. This one answers only with
    levels jts3 actually produced, and REFUSES a volume the run never visited
    rather than interpolating one. That refusal is the point. The recorded run
    stops at -12.50 dB, so a climb policy that commands anything else is asking
    a question this evidence cannot answer, and the test says so by name
    instead of inventing a level for it.
    """

    def __init__(self, volume, tone, *, run: dict) -> None:
        super().__init__(
            volume,
            tone,
            ambient_dbfs=UMIK2.dbfs_from_db_spl(run["ambient_db_spl"]),
        )
        self._levels = {
            round(volume_db, 2): db_spl for volume_db, db_spl in run["readings"]
        }
        self._levels[NEW_HORN_STOP_VOLUME_DB] = run["stop_sample_db_spl"]

    def _rms_dbfs(self) -> float:
        if not self._tone.started:
            return self.ambient_dbfs
        key = round(self._volume.value, 2)
        if key not in self._levels:
            raise AssertionError(
                f"the recorded run has no measurement at {key:+.2f} dB "
                f"(it visited {sorted(self._levels)}); a policy that commands "
                "this volume needs new bench evidence, not an interpolation"
            )
        return UMIK2.dbfs_from_db_spl(self._levels[key])


def _new_horn_pass(tmp_path, run: dict):
    clock = FakeClock()
    volume = Volume()
    tone = BlockingTone()
    mic = TracedMic(volume, tone, run=run)
    result = asyncio.run(
        slr.run_seat_level_ramp(
            target=NEW_HORN_TARGET,
            sensitivity=UMIK2,
            max_main_volume_db=NEW_HORN_CEILING_DB,
            spl_ceiling_db_spl=NEW_HORN_STOP_DB_SPL,
            get_main_volume_db=volume.get,
            set_main_volume_db=volume.set,
            play_continuous_tone=tone.play,
            cancel_tone=tone.cancel,
            next_samples=mic.next_samples,
            clock=clock.now,
            sleep=clock.sleep,
            volume_state_path=tmp_path / "seat_level_volume.json",
            reference_state_path=tmp_path / "seat_level_reference.json",
        )
    )
    return result, volume


@pytest.mark.parametrize("name", sorted(NEW_HORN_RUNS))
def test_the_new_horn_refusal_replays_from_its_own_trace(tmp_path, name):
    """jts3's two 2026-08-23 refusals, reproduced from the recorded levels.

    Characterization, not approval: this is the behaviour the owner's
    2026-08-23 ruling changed. Both runs climbed in full bites from -50.00 dB,
    banked five readings, and the sixth window was abandoned by a sample above
    the 80.0 dB SPL commissioning stop the profile declared THEN. Nothing was
    banked and the household volume came back.
    """
    run = NEW_HORN_RUNS[name]
    result, volume = _new_horn_pass(tmp_path, run)

    assert result.status == "refused"
    assert result.reason == slr.REFUSE_SPL_CEILING_EXCEEDED
    assert result.reference_volume_db is None
    assert result.measured_db_spl is None
    assert result.restored is True
    assert not (tmp_path / "seat_level_reference.json").exists()

    steps = result.ramp["steps"]
    assert [step["volume_db"] for step in steps] == [
        pytest.approx(volume_db) for volume_db, _ in run["readings"]
    ]
    assert result.ramp["final_volume_db"] == pytest.approx(NEW_HORN_STOP_VOLUME_DB)
    assert result.ramp["bite_db"] == pytest.approx(NEW_HORN_BITE_DB)
    # The estimate the refusal discloses is the one the box printed. The
    # tolerance is the receipt's own rounding: it publishes readings to 2 dp,
    # so a slope re-derived from them can differ from the box's by ~0.0013.
    assert result.ramp["slope_db_per_db"] == pytest.approx(
        run["slope_estimate"], abs=0.002
    )
    assert volume.commanded[-1] == pytest.approx(HOUSEHOLD_VOLUME_DB)


@pytest.mark.parametrize("name", sorted(NEW_HORN_RUNS))
def test_the_new_horn_climb_was_bite_limited_at_every_reading(name):
    """The bite bound every step, so the gap term never decided anything.

    This is what rules out one proposed fix. The step is ``min(gap, bite)``; at
    all five readings of both runs the measured gap was LARGER than the bite, so
    the commanded sequence -50.00, -42.50, -35.00, -27.50, -20.00, -12.50 is
    fixed by the bite alone.

    In particular, dividing the gap by the run's OWN measured slope under a
    unit floor -- ``min(gap / max(slope, 1.0), bite)`` -- reproduces this climb
    move for move, because every slope this run measured was under 1.0, so the
    floor makes the divisor exactly 1.0 at every step. Moving this climb takes a
    smaller bite or a trustworthier reading, not a rescaled gap.
    """
    run = NEW_HORN_RUNS[name]
    readings = run["readings"]
    for index, (volume_db, observed_db_spl) in enumerate(readings):
        gap_db = NEW_HORN_TARGET.target_db_spl - observed_db_spl
        assert gap_db > NEW_HORN_BITE_DB, (volume_db, gap_db)
        step_db = capped_gap_step_db(
            measured_db=observed_db_spl,
            target_db=NEW_HORN_TARGET.target_db_spl,
            cap_db=NEW_HORN_BITE_DB,
        )
        assert step_db == pytest.approx(NEW_HORN_BITE_DB)
        # The slope the loop's estimator holds when it sizes THIS step: none at
        # the first reading, else the pair behind it.
        if index == 0:
            continue
        (prev_db, prev_spl) = readings[index - 1]
        slope = (observed_db_spl - prev_spl) / (volume_db - prev_db)
        assert max(slope, 1.0) == 1.0
        assert min(gap_db / max(slope, 1.0), NEW_HORN_BITE_DB) == pytest.approx(
            step_db
        )


@pytest.mark.parametrize("name", sorted(NEW_HORN_RUNS))
def test_the_new_horn_slope_estimate_never_reached_unity(name):
    """The pair the estimator held never reached unity in either run.

    A chain that answered a commanded dB with more than a dB would be visible
    in the readings, and the ones this estimate rests on are not: every RAW
    consecutive pair of banked readings measured under 1.0 dB SPL per commanded
    dB, and the pair the estimator actually used — the last two — stays under
    1.0 with the room's power taken back out too (0.970 for run 1, 0.758 for
    run 2). So "the readings were still emerging from the room" does not
    account for the estimate being low.

    Room-subtracting DOES lift other pairs past 1.0 — run 1's -35.00 → -27.50
    is 1.098 — which is why the claim above is scoped to the estimator's own
    pair and to the raw statistic the assertion checks, rather than to every
    pair in the run.

    The ~2.2 dB SPL per dB figure quoted for these runs is not in this set at
    all: it is the abandoned window's single sample measured against the
    previous window's twelve-sample median, which is a different statistic
    (2.217 for run 2, which is where the quoted figure comes from; run 1's
    equivalent is 2.119).
    """
    run = NEW_HORN_RUNS[name]
    slopes = _consecutive_slopes(run["readings"])
    assert max(slopes) < 1.0, slopes
    # The estimator's own window is the LAST two banked readings, and it is the
    # number the box printed (to the receipt's 2 dp rounding).
    assert slopes[-1] == pytest.approx(run["slope_estimate"], abs=0.002)

    room = run["ambient_db_spl"]
    subtracted = _consecutive_slopes(
        [
            (volume_db, _room_subtracted_db(spl, room))
            for volume_db, spl in run["readings"]
        ]
    )
    assert subtracted == pytest.approx(run["room_subtracted_slopes"], abs=0.001)
    # The estimator's own pair stays under unity room-subtracted too...
    assert subtracted[-1] < 1.0
    # ...while OTHER pairs can cross it once the room is out, which is exactly
    # why the claim above is scoped to this pair rather than to the run.
    assert (max(subtracted) > 1.0) == (name == "run1")
    # The statistic the quoted ~2.2 figure comes from, which is none of these.
    (_last_db, last_spl) = run["readings"][-1]
    assert (run["stop_sample_db_spl"] - last_spl) / NEW_HORN_BITE_DB == pytest.approx(
        run["stop_sample_slope"], abs=0.001
    )
    # ...and both readings the estimator used were already well clear of the
    # room by the pass's own emergence bar, so neither was room-pinned.
    (_a_db, a_spl), (_b_db, b_spl) = run["readings"][-2:]
    assert a_spl - room > slr.MIC_RESPONSE_MIN_RISE_DB
    assert b_spl - room > slr.MIC_RESPONSE_MIN_RISE_DB


# --- the comfort cap --------------------------------------------------------


def test_no_single_step_raises_the_room_by_more_than_the_cap(tmp_path):
    """A large gap is covered in capped strides, never one lunge.

    From a very quiet room the ramp has ~50 dB to cover. Every upward move must
    still be at most one audible step, because an operator is sitting in front
    of the speaker.
    """
    volume, tone, mic = _rig(gain_db=_far_chain_gain_db())
    result = asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))

    assert result.status == "converged", (result.reason, result.detail)
    volumes = [step["volume_db"] for step in result.ramp["steps"]]
    rises = [b - a for a, b in zip(volumes, volumes[1:])]
    assert rises, volumes
    assert max(rises) <= DEFAULT_BITE_DB + 1e-6
    # ...and the cap really bound: at least one step was a full cap stride.
    assert max(rises) == pytest.approx(DEFAULT_BITE_DB)


def test_removing_the_cap_lets_one_step_lunge_the_whole_gap(tmp_path, monkeypatch):
    """Mutation proof for the cap above.

    Neuter ONLY the saturation — the step becomes the raw measured gap — and
    re-run the identical scenario. One step now raises the room by far more
    than an audible step's worth, which is precisely what the cap exists to
    prevent.
    """
    monkeypatch.setattr(
        slr,
        "capped_gap_step_db",
        lambda *, measured_db, target_db, cap_db=None: target_db - measured_db,
    )
    volume, tone, mic = _rig(gain_db=_far_chain_gain_db())
    result = asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))

    volumes = [step["volume_db"] for step in result.ramp["steps"]]
    rises = [b - a for a, b in zip(volumes, volumes[1:])]
    assert max(rises) > DEFAULT_BITE_DB + 1.0


# --- the two-miss refusal, and the slope it discloses ------------------------


class LaggingMic(Mic):
    """A chain that answers a commanded dB with only a fraction of a dB.

    Not a fault — a real compressor, a protection limiter, or an amp near its
    rail behaves like this. The ramp must not chase it forever: two steps that
    commanded the whole measured gap and still missed is the point at which the
    honest answer is the measured slope.
    """

    def __init__(self, *args, slope: float = 0.15, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.slope = slope

    def _rms_dbfs(self) -> float:
        if not self._tone.started:
            return self.ambient_dbfs
        speaker = (self._volume.value * self.slope) + self.gain_db
        return _power_sum_db(self.ambient_dbfs, speaker)


def test_two_full_gap_steps_that_miss_refuse_with_the_measured_slope(tmp_path):
    volume = Volume()
    tone = BlockingTone()
    # Reads 72 dB SPL at the start -- inside one bite of the 75-80 band, so the
    # ramp commands the WHOLE measured gap rather than a truncated bite, and the
    # chain answers each dB with 0.15 dB. Two such steps miss, and that is the
    # point at which the honest answer is the measured slope.
    slope = 0.15
    mic = LaggingMic(
        volume,
        tone,
        gain_db=UMIK2.dbfs_from_db_spl(72.0) - slr.SEAT_LEVEL_START_DB * slope,
        ambient_dbfs=UMIK2.dbfs_from_db_spl(50.0),
        slope=slope,
    )
    result = asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))

    assert result.reason == slr.REFUSE_LEVEL_UNCONVERGED, (
        result.reason, result.ramp
    )
    # The refusal is an instrument answer: it says what the chain measured.
    assert "dB per commanded dB" in (result.detail or "")
    assert result.ramp["slope_db_per_db"] is not None
    assert result.ramp["slope_db_per_db"] < 0.5
    assert result.reference_volume_db is None
    assert not (tmp_path / "seat_level_reference.json").exists()


def test_a_capped_step_never_spends_the_miss_budget(tmp_path):
    """A truncated move is not a failed prediction.

    The quiet-room pass above takes several cap-limited strides before it can
    aim at the band. If those counted as misses it would refuse after two
    strides instead of converging — so the pass that converges IS the proof,
    read here off its own telemetry.
    """
    volume, tone, mic = _rig(gain_db=_far_chain_gain_db())
    result = asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))
    assert result.status == "converged", (result.reason, result.detail)
    volumes = [step["volume_db"] for step in result.ramp["steps"]]
    capped = [
        b - a
        for a, b in zip(volumes, volumes[1:])
        if abs((b - a) - DEFAULT_BITE_DB) < 1e-6
    ]
    assert len(capped) > slr.MAX_MISSED_FULL_STEPS


# --- convergence ------------------------------------------------------------


def test_a_converged_ramp_banks_the_volume_that_measured_the_band(tmp_path):
    volume, tone, mic = _rig(gain_db=-10.0)
    result = asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))

    assert result.status == "converged", (result.reason, result.detail)
    # G = -10, so the band [-31.07, -26.07] dBFS is reached at [-21.07, -16.07]
    # dB of main volume.
    assert -21.07 <= result.reference_volume_db <= -16.07
    assert TARGET.low_db_spl <= result.measured_db_spl <= TARGET.high_db_spl
    assert tone.cancelled

    banked = load_seat_level_reference(
        state_path=tmp_path / "seat_level_reference.json"
    )
    assert banked["reference_volume_db"] == pytest.approx(
        result.reference_volume_db, abs=1e-3
    )
    assert banked["target"]["low_db_spl"] == 75.0
    assert banked["target"]["high_db_spl"] == 80.0
    # The disclosure that makes the number auditable: which mic, at what gain.
    assert banked["mic_sensitivity"]["sens_factor_db"] == -12.07
    assert banked["mic_sensitivity"]["analog_gain_db"] == 18.0


def test_the_banked_artifact_keeps_its_exact_shape(tmp_path):
    """The document's consumers did not change, so neither may its keys.

    ``session_volume_plan.measurement_reference_volume_db`` and
    ``jasper-doctor`` read this file; a rework of HOW the volume was found must
    leave WHAT is written byte-identical in shape.
    """
    volume, tone, mic = _rig(gain_db=-10.0)
    asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))

    banked = json.loads((tmp_path / "seat_level_reference.json").read_text())
    assert set(banked) == {
        "artifact_schema_version",
        "kind",
        "updated_at",
        "state_path",
        "reference_volume_db",
        "measured_db_spl",
        "target",
        "mic_sensitivity",
        "max_main_volume_db",
    }
    assert banked["artifact_schema_version"] == 1
    assert banked["kind"] == "jts_active_speaker_seat_level_reference"
    assert set(banked["target"]) == {
        "target_db_spl", "tolerance_db", "low_db_spl", "high_db_spl",
    }
    # Rounding is part of the shape: 3 decimals for volumes, 2 for the SPL.
    assert banked["reference_volume_db"] == round(banked["reference_volume_db"], 3)
    assert banked["measured_db_spl"] == round(banked["measured_db_spl"], 2)
    assert banked["max_main_volume_db"] == pytest.approx(CEILING_DB)


def test_the_ramp_never_commands_above_the_stimulus_ceiling(tmp_path):
    volume, tone, mic = _rig(gain_db=-10.0)
    result = asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))
    assert max(step["volume_db"] for step in result.ramp["steps"]) <= CEILING_DB + 1e-9


# --- the runaway guard, and the ambient floor it reads ----------------------


def test_a_mic_that_is_not_observing_aborts_the_climb(tmp_path):
    volume, tone, mic = _rig(deaf=True)
    result = asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))

    assert result.status == "refused"
    assert result.reason == slr.REFUSE_MIC_NOT_OBSERVING
    # It ran out of ceiling -- the only non-arbitrary place to ask -- and named
    # the mic rather than blaming a quiet amplifier.
    highest = max(step["volume_db"] for step in result.ramp["steps"])
    assert highest == pytest.approx(CEILING_DB)
    assert "never rose" in (result.detail or "")
    assert tone.cancelled
    assert not (tmp_path / "seat_level_reference.json").exists()


def test_removing_the_runaway_guard_lets_a_dead_mic_walk_to_the_ceiling(
    tmp_path, monkeypatch
):
    """Mutation proof for the guard above.

    Disarm ONLY the predicate and re-run the identical dead-mic scenario. The
    ramp then walks the speaker to the ceiling on a mic that never heard a
    thing, and reports the wrong diagnosis. Nothing else in the pass stops that
    climb.
    """
    monkeypatch.setattr(slr, "mic_is_not_observing", lambda **_kw: False)
    volume, tone, mic = _rig(deaf=True)
    result = asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))

    assert result.reason != slr.REFUSE_MIC_NOT_OBSERVING
    # ...and the wrong diagnosis is what an operator would have chased.
    assert result.reason == slr.REFUSE_SPL_TARGET_UNREACHABLE


@pytest.mark.parametrize(
    "seat_spl_at_ceiling, ambient_db_spl",
    [
        pytest.param(85.0, 45.0, id="ceiling_85_ambient_45"),
        pytest.param(90.0, 42.0, id="ceiling_90_ambient_42"),
    ],
)
def test_a_speaker_quieter_than_the_room_is_not_falsely_aborted(
    tmp_path, seat_spl_at_ceiling, ambient_db_spl
):
    """The guard's false-abort cases, and they must converge.

    At the start the speaker is well BELOW the room, so the mic reads ambient
    and the level does not track the commanded volume at all down there. Measuring rise against the ambient floor (rather than against the
    first reading) is what lets this chain climb through the room and converge.
    """
    gain = gain_for_seat_spl(seat_spl_at_ceiling, at_volume_db=CEILING_DB)
    volume, tone, mic = _rig(
        gain_db=gain, ambient_dbfs=UMIK2.dbfs_from_db_spl(ambient_db_spl)
    )
    result = asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))
    assert result.status == "converged", (result.reason, result.detail)
    assert TARGET.low_db_spl <= result.measured_db_spl <= TARGET.high_db_spl
    # The first reading really was buried in the room -- otherwise this proves
    # nothing about the ambient-relative rule.
    assert result.ramp["steps"][0]["rise_db"] < slr.MIC_RESPONSE_MIN_RISE_DB


def test_a_first_reading_relative_guard_would_have_aborted_those(tmp_path):
    """The no-op control for the test above: a first-reading rule really fires.

    Score the rise against the FIRST reading instead of the measured ambient,
    leave everything else identical, and the same healthy chain is refused.
    That is what makes the pass above evidence rather than a coincidence.
    """
    gain = gain_for_seat_spl(85.0, at_volume_db=CEILING_DB)
    volume, tone, mic = _rig(
        gain_db=gain, ambient_dbfs=UMIK2.dbfs_from_db_spl(45.0)
    )
    real_reading = slr._settle_reading
    first: list[float] = []

    async def _first_relative(*args, **kwargs):
        reading = await real_reading(*args, **kwargs)
        if reading.rms_dbfs is None:
            return reading
        if not first:
            # Pretend the pre-tone ambient window measured the first IN-TONE
            # reading, which is what a first-reading-relative rule scores rise
            # against.
            first.append(reading.rms_dbfs)
        return reading

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(slr, "_settle_reading", _first_relative)
        asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))
    # The first in-tone reading sits AT the room, far above the true speaker
    # level -- so a rule anchored there demands the chain rise 6 dB over the
    # room's own floor before it has emerged, and refuses the healthy chain.
    assert first
    assert UMIK2.db_spl_from_dbfs(first[0]) == pytest.approx(45.0, abs=1.5)


def test_a_non_finite_feed_cannot_defeat_the_guard(tmp_path):
    """NaN scores as no rise, never as no evidence.

    The ambient window sees finite room samples; every sample once the tone
    starts is NaN, so no window can form a median and the feed reads as lost
    rather than as a silently unarmed guard.
    """
    volume, tone, mic = _rig(gain_db=-10.0, nan_once_playing=True)
    result = asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))
    assert result.reason == slr.REFUSE_MIC_FEED_LOST
    assert result.reference_volume_db is None


def test_no_ambient_sample_at_all_refuses_before_the_tone(tmp_path):
    volume = Volume()
    tone = BlockingTone()

    async def _silent() -> list:
        return []

    clock = FakeClock()
    result = asyncio.run(
        slr.run_seat_level_ramp(
            target=TARGET,
            sensitivity=UMIK2,
            max_main_volume_db=CEILING_DB,
            spl_ceiling_db_spl=SPL_CEILING,
            get_main_volume_db=volume.get,
            set_main_volume_db=volume.set,
            play_continuous_tone=tone.play,
            cancel_tone=tone.cancel,
            next_samples=_silent,
            clock=clock.now,
            sleep=clock.sleep,
            volume_state_path=tmp_path / "seat_level_volume.json",
            reference_state_path=tmp_path / "seat_level_reference.json",
        )
    )
    assert result.reason == slr.REFUSE_MIC_FEED_LOST
    assert not tone.started
    assert not volume.commanded  # the latch is opened only after ambient


# --- convergence needs a real rise, not just an in-band number --------------


class StuckOncePlaying(Mic):
    """Reads the room before the tone and a fixed in-band level after it.

    The failure the rise test exists for, in the only shape that can defeat a
    band check: a mic pinned at a value that happens to sit inside the target
    window.
    """

    def _rms_dbfs(self) -> float:
        if not self._tone.started:
            return self.ambient_dbfs
        return self.stuck_dbfs


def test_a_stuck_constant_in_band_mic_refuses_instead_of_banking(tmp_path):
    in_band_dbfs = UMIK2.dbfs_from_db_spl(TARGET.target_db_spl)
    volume = Volume()
    tone = BlockingTone()
    # Ambient only 2 dB under the stuck level: the reading sits inside the
    # band, but it never rises clear of the room.
    mic = StuckOncePlaying(
        volume, tone, ambient_dbfs=in_band_dbfs - 2.0, stuck_dbfs=in_band_dbfs
    )
    result = asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))

    assert result.status == "refused"
    assert result.reference_volume_db is None
    assert not (tmp_path / "seat_level_reference.json").exists()


def test_without_the_rise_test_the_stuck_mic_banks_its_own_start(
    tmp_path, monkeypatch
):
    """The control that names the fix: the rise against ambient is what stops it.

    Drop the required rise to zero — which is what a band-only convergence rule
    is — and change NOTHING else. The same stuck mic banks the ramp's own start
    volume as a "measured" reference.
    """
    monkeypatch.setenv("JASPER_SEAT_LEVEL_MIN_RISE_DB", "1.0")
    in_band_dbfs = UMIK2.dbfs_from_db_spl(TARGET.target_db_spl)
    volume = Volume()
    tone = BlockingTone()
    mic = StuckOncePlaying(
        volume, tone, ambient_dbfs=in_band_dbfs - 2.0, stuck_dbfs=in_band_dbfs
    )
    result = asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))

    assert result.status == "converged"
    assert result.reference_volume_db == pytest.approx(result.ramp["start_db"])
    assert (tmp_path / "seat_level_reference.json").exists()


# --- the SPL ceiling --------------------------------------------------------


def test_a_measured_level_over_the_commissioning_ceiling_aborts(tmp_path):
    # A chain hot enough that the very first tone reads past the 85 dB SPL stop.
    volume, tone, mic = _rig(
        gain_db=gain_for_seat_spl(90.0, at_volume_db=slr.SEAT_LEVEL_START_DB)
    )
    result = asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))

    assert result.reason == slr.REFUSE_SPL_CEILING_EXCEEDED
    assert "commissioning stop" in (result.detail or "")
    assert tone.cancelled
    assert not (tmp_path / "seat_level_reference.json").exists()


def test_a_clipped_capture_aborts_rather_than_reading_a_level(tmp_path):
    volume = Volume()
    tone = BlockingTone()

    class Clipping(Mic):
        async def next_samples(self):
            batch = await super().next_samples()
            if self._tone.started:
                batch[0] = LevelSample(
                    seq=batch[0].seq,
                    t_client_ms=batch[0].t_client_ms,
                    rms_dbfs=batch[0].rms_dbfs,
                    peak_dbfs=0.0,
                    clip=True,
                )
            return batch

    mic = Clipping(volume, tone, gain_db=-10.0)
    result = asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))
    assert result.reason == slr.REFUSE_MIC_CLIPPING
    assert not (tmp_path / "seat_level_reference.json").exists()


# --- refusals bank nothing, and always restore ------------------------------


def test_an_unreachable_target_refuses_and_banks_nothing(tmp_path):
    # A chain quiet enough that even the ceiling measures below the band -- but
    # loud enough to clear the room floor on the way, so this is a genuine
    # ceiling refusal and not a mic that never spoke.
    volume, tone, mic = _rig(
        gain_db=gain_for_seat_spl(TARGET.low_db_spl - 1.0, at_volume_db=CEILING_DB)
    )
    result = asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))

    assert result.reason == slr.REFUSE_SPL_TARGET_UNREACHABLE
    assert "raise the external amplifier" in (result.detail or "")
    assert result.reference_volume_db is None
    assert not (tmp_path / "seat_level_reference.json").exists()


def test_the_watchdog_refuses_a_pass_whose_feed_never_returns(tmp_path, monkeypatch):
    """The backstop, and it is a backstop only.

    Nothing in the ramp's own shape can reach this: it fires when something the
    pass awaits never returns at all.
    """
    monkeypatch.setattr(slr, "_watchdog_seconds", lambda **_kw: 0.05)
    volume = Volume()
    tone = BlockingTone()
    calls = {"n": 0}

    async def _hangs_once_playing():
        calls["n"] += 1
        if calls["n"] > 40:
            await asyncio.Event().wait()  # never returns
        return [
            LevelSample(seq=calls["n"], t_client_ms=0, rms_dbfs=-70.0, peak_dbfs=-67.0)
        ]

    result = asyncio.run(
        slr.run_seat_level_ramp(
            target=TARGET,
            sensitivity=UMIK2,
            max_main_volume_db=CEILING_DB,
            spl_ceiling_db_spl=SPL_CEILING,
            get_main_volume_db=volume.get,
            set_main_volume_db=volume.set,
            play_continuous_tone=tone.play,
            cancel_tone=tone.cancel,
            next_samples=_hangs_once_playing,
            volume_state_path=tmp_path / "seat_level_volume.json",
            reference_state_path=tmp_path / "seat_level_reference.json",
        )
    )
    assert result.reason == slr.REFUSE_WATCHDOG_EXPIRED
    assert "watchdog" in (result.detail or "")
    assert tone.cancelled
    assert volume.value == pytest.approx(HOUSEHOLD_VOLUME_DB)
    assert not (tmp_path / "seat_level_reference.json").exists()


@pytest.mark.parametrize(
    "mic_kwargs",
    [
        pytest.param({"gain_db": -10.0}, id="converged"),
        pytest.param(
            {
                "gain_db": gain_for_seat_spl(
                    TARGET.low_db_spl - 1.0, at_volume_db=CEILING_DB
                )
            },
            id="unreachable",
        ),
        pytest.param({"deaf": True}, id="mic_not_observing"),
        pytest.param(
            {
                "gain_db": gain_for_seat_spl(
                    90.0, at_volume_db=slr.SEAT_LEVEL_START_DB
                )
            },
            id="spl_ceiling_exceeded",
        ),
    ],
)
def test_the_household_volume_is_restored_on_every_exit_path(tmp_path, mic_kwargs):
    volume, tone, mic = _rig(**mic_kwargs)
    asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))
    assert volume.value == pytest.approx(HOUSEHOLD_VOLUME_DB)
    # The latch resolved itself rather than leaving a stale intent behind.
    state = json.loads((tmp_path / "seat_level_volume.json").read_text())
    assert state["status"] == "resolved"


# --- stopping the pass at any moment ----------------------------------------


def test_cancelling_mid_climb_stops_the_tone_restores_and_banks_nothing(tmp_path):
    """The owner's stop requirement, and the jts3 `restored: false` loose end.

    A `finally: await ...` is not enough on its own: once the task carrying it
    is cancelled, its next await raises immediately and the restore is skipped
    — which is how a stopped pass left the fader 6 dB below where it found it.
    ``run_teardown`` shields the teardown so the room goes quiet AND the
    household gets its volume back on the very same interrupt.
    """
    volume = Volume()
    tone = BlockingTone()
    mic = Mic(volume, tone, gain_db=-10.0)
    clock = FakeClock()
    started = asyncio.Event()

    async def _watched_samples():
        if tone.started:
            started.set()
        return await mic.next_samples()

    async def _go():
        task = asyncio.ensure_future(
            slr.run_seat_level_ramp(
                target=TARGET,
                sensitivity=UMIK2,
                max_main_volume_db=CEILING_DB,
                spl_ceiling_db_spl=SPL_CEILING,
                get_main_volume_db=volume.get,
                set_main_volume_db=volume.set,
                play_continuous_tone=tone.play,
                cancel_tone=tone.cancel,
                next_samples=_watched_samples,
                clock=clock.now,
                sleep=clock.sleep,
                volume_state_path=tmp_path / "seat_level_volume.json",
                reference_state_path=tmp_path / "seat_level_reference.json",
            )
        )
        await started.wait()
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_go())

    assert tone.cancelled                                   # the room goes quiet
    assert volume.value == pytest.approx(HOUSEHOLD_VOLUME_DB)  # ...and gets its level back
    state = json.loads((tmp_path / "seat_level_volume.json").read_text())
    assert state["status"] == "resolved"
    assert not (tmp_path / "seat_level_reference.json").exists()


def test_a_teardown_step_survives_a_repeated_cancellation(tmp_path):
    """What ``run_teardown`` is for, measured against the plain ``await``.

    CancelledError is delivered ONCE, so a bare ``finally: await ...`` does
    survive a single cancel. It is the SECOND cancel — an operator pressing
    Ctrl-C twice, or a supervisor that re-cancels — that strands the restore.
    Both halves are asserted here so the claim is a measurement, not a story.
    """

    async def _probe(*, shielded: bool, cancels: int) -> list[str]:
        done: list[str] = []

        async def _restore() -> None:
            await asyncio.sleep(0.01)
            done.append("restored")

        async def _body() -> None:
            try:
                await asyncio.sleep(10)
            finally:
                if shielded:
                    await slr.run_teardown("probe", _restore())
                else:
                    await _restore()

        task = asyncio.ensure_future(_body())
        await asyncio.sleep(0)
        for _ in range(cancels):
            task.cancel()
            await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await task
        return done

    assert asyncio.run(_probe(shielded=False, cancels=1)) == ["restored"]
    assert asyncio.run(_probe(shielded=False, cancels=2)) == []
    assert asyncio.run(_probe(shielded=True, cancels=2)) == ["restored"]
    # Bounded on purpose: past the shield budget the operator gets out.
    assert asyncio.run(
        _probe(shielded=True, cancels=slr.TEARDOWN_SHIELD_ATTEMPTS + 1)
    ) == []


def test_a_restore_that_fails_is_published_not_assumed(tmp_path):
    """``restored`` MEANS something, so a stranded fader cannot read as success.

    The volume seam raises when CamillaDSP rejects a write. The pass must not
    quietly log that and hand back a result indistinguishable from one that put
    the household level back.
    """

    class Refusing(Volume):
        def __init__(self) -> None:
            super().__init__()
            self.allow = True

        async def set(self, db: float) -> bool:
            if not self.allow:
                raise RuntimeError("camilladsp rejected the volume write")
            return await super().set(db)

    volume = Refusing()
    tone = BlockingTone()
    mic = Mic(volume, tone, gain_db=-10.0)
    real_close = SessionVolumePlan.close

    async def _close_after_break(self, *a, **kw):
        volume.allow = False
        return await real_close(self, *a, **kw)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(SessionVolumePlan, "close", _close_after_break)
        result = asyncio.run(
            _level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path)
        )

    assert result.restored is False
    assert result.to_dict()["restored"] is False
    assert volume.value != pytest.approx(HOUSEHOLD_VOLUME_DB)


def test_a_completed_pass_publishes_that_it_restored(tmp_path):
    volume, tone, mic = _rig(gain_db=-10.0)
    result = asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))
    assert result.restored is True
    assert volume.value == pytest.approx(HOUSEHOLD_VOLUME_DB)


def test_the_sigint_path_reports_the_restore_it_measured(tmp_path):
    """S3: prose and JSON must agree about the fader.

    ``restored: null`` beside a detail claiming "volume restored" is exactly the
    dishonesty this field exists to kill, so the CLI's interrupt refusal carries
    the value the pass's own teardown measured.
    """
    from jasper.cli import seat_level as cli

    volume, tone, mic = _rig(gain_db=-10.0)
    clock = FakeClock()
    started = asyncio.Event()

    async def _watched():
        if tone.started:
            started.set()
        return await mic.next_samples()

    async def _go():
        task = asyncio.ensure_future(
            slr.run_seat_level_ramp(
                target=TARGET,
                sensitivity=UMIK2,
                max_main_volume_db=CEILING_DB,
                spl_ceiling_db_spl=SPL_CEILING,
                get_main_volume_db=volume.get,
                set_main_volume_db=volume.set,
                play_continuous_tone=tone.play,
                cancel_tone=tone.cancel,
                next_samples=_watched,
                clock=clock.now,
                sleep=clock.sleep,
                volume_state_path=tmp_path / "seat_level_volume.json",
                reference_state_path=tmp_path / "seat_level_reference.json",
            )
        )
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError as exc:
            return slr.interrupted_restore_outcome(exc)
        raise AssertionError("the cancellation did not propagate")

    measured = asyncio.run(_go())
    # The pass really did restore, and really did say so on the exception.
    assert measured is True
    assert volume.value == pytest.approx(HOUSEHOLD_VOLUME_DB)

    # ...and the CLI turns that into a refusal whose prose and JSON agree.
    result, detail = cli._refused(
        slr.REFUSE_INTERRUPTED, f"stopped. {cli._restore_phrase(measured)}",
        restored=measured,
    )
    assert result.restored is True
    assert result.to_dict()["restored"] is True
    assert "was restored" in detail


def test_an_unobservable_interrupt_never_claims_a_restore(tmp_path):
    """The last-resort path knows nothing, and must say nothing.

    A KeyboardInterrupt that escaped the pass entirely carries no stamp, so the
    honest report is "could not be observed" and a null field -- never prose
    asserting a restore that no one measured.
    """
    from jasper.cli import seat_level as cli

    assert slr.interrupted_restore_outcome(KeyboardInterrupt()) is None
    phrase = cli._restore_phrase(None)
    assert "could not be observed" in phrase
    assert "restored." not in phrase  # never an assertion of success

    result, detail = cli._refused(
        slr.REFUSE_INTERRUPTED, f"stopped. {phrase}", restored=None
    )
    assert result.restored is None
    assert result.to_dict()["restored"] is None
    assert "could not be observed" in detail

    # A failed restore is reported as a failure, with the remedy.
    failed = cli._restore_phrase(False)
    assert "NOT restored" in failed
    assert "volume-recovery screen" in failed


def test_an_unconfirmable_volume_latch_refuses_before_any_tone(tmp_path):
    class Drifting(Volume):
        async def set(self, db: float) -> bool:
            self.commanded.append(float(db))
            return True  # accepted, but the readback never moves

    volume = Drifting()
    tone = BlockingTone()
    mic = Mic(volume, tone, gain_db=-10.0)
    result = asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))
    assert result.reason == slr.REFUSE_VOLUME_LATCH_UNCONFIRMED
    assert "did not confirm" in (result.detail or "")
    assert not tone.started


# --- the recovered-latch family ---------------------------------------------


def test_a_live_measurement_session_refuses_the_leveling_pass(tmp_path):
    """The same door jasper-angle-capture stands behind: a session holding the
    speaker means this may not start, read off the SHARED durable state."""
    state = tmp_path / "seat_level_volume.json"
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "jts_crossover_session_volume",
                "status": "active",
                "reason": None,
                "opened_at": time.time(),
                "wall_clock_ceiling_s": 1800.0,
                "measurement_volume_db": -20.0,
                "original_main_volume_db": -30.0,
            }
        )
    )
    volume, tone, mic = _rig(gain_db=-10.0)
    result = asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))

    assert result.reason == slr.REFUSE_SESSION_ALREADY_LIVE
    assert "already running" in (result.detail or "")
    assert "leveling the seat SPL" in (result.detail or "")
    assert not tone.started
    assert not volume.commanded  # nothing was touched


def test_the_hold_is_visible_to_the_recovery_family(tmp_path):
    """A killed pass must be drainable by the machinery that already exists.

    The interlock and the volume-recovery screen both read one durable file; if
    the hold went anywhere else they would report an idle speaker while it sat
    at measurement volume. Here: a leveling pass's own live state is exactly
    what the shared reader calls busy.
    """
    state = tmp_path / "seat_level_volume.json"
    volume, tone, mic = _rig(gain_db=-10.0)

    seen: list[str | None] = []
    original_close = SessionVolumePlan.close

    async def _peek(self, *a, **kw):
        # Mid-pass, before the restore drains it.
        seen.append(live_measurement_session(state_path=state))
        return await original_close(self, *a, **kw)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(SessionVolumePlan, "close", _peek)
        asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))

    assert seen and seen[0] is not None
    assert "already running" in seen[0]
    # ...and once it closes cleanly the family sees an idle speaker again.
    assert live_measurement_session(state_path=state) is None


# --- the persisted reference ------------------------------------------------


def test_reference_reader_is_absent_tolerant(tmp_path):
    missing = tmp_path / "nope.json"
    assert load_seat_level_reference(state_path=missing) is None
    assert seat_level_reference_volume_db(state_path=missing) is None


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("not json at all", id="unparseable"),
        pytest.param(json.dumps({"reference_volume_db": -18.0}), id="no_kind"),
        pytest.param(
            json.dumps(
                {
                    "kind": "jts_active_speaker_seat_level_reference",
                    "artifact_schema_version": 99,
                    "reference_volume_db": -18.0,
                }
            ),
            id="wrong_schema",
        ),
    ],
)
def test_a_malformed_reference_reads_as_absent(tmp_path, payload):
    path = tmp_path / "ref.json"
    path.write_text(payload)
    assert seat_level_reference_volume_db(state_path=path) is None


@pytest.mark.parametrize("stored", [3.0, 0.5, -60.0, -75.0, float("nan"), True, "loud"])
def test_an_out_of_envelope_reference_reads_as_absent(tmp_path, stored):
    """Fail-safe direction: a corrupt reference can only make the session
    QUIETER (back to the codified default), never louder."""
    path = tmp_path / "ref.json"
    path.write_text(
        json.dumps(
            {
                "kind": "jts_active_speaker_seat_level_reference",
                "artifact_schema_version": 1,
                "reference_volume_db": stored,
            }
        )
    )
    assert seat_level_reference_volume_db(state_path=path) is None


def test_writing_a_reference_the_reader_would_reject_raises(tmp_path):
    path = tmp_path / "ref.json"
    with pytest.raises(SeatLevelTargetError):
        write_seat_level_reference(
            reference_volume_db=-70.0,
            measured_db_spl=77.5,
            target=TARGET,
            sensitivity=UMIK2.to_dict(),
            max_main_volume_db=CEILING_DB,
            state_path=path,
        )
    assert not path.exists()


def test_a_written_reference_round_trips(tmp_path):
    path = tmp_path / "ref.json"
    write_seat_level_reference(
        reference_volume_db=-17.25,
        measured_db_spl=77.4,
        target=TARGET,
        sensitivity=UMIK2.to_dict(),
        max_main_volume_db=CEILING_DB,
        state_path=path,
    )
    assert seat_level_reference_volume_db(state_path=path) == pytest.approx(-17.25)


# --------------------------------------------------------------------------- #
# the shared measurement window: this pass runs inside it, or not at all
# --------------------------------------------------------------------------- #


def test_the_whole_pass_runs_inside_the_measurement_window(
    tmp_path, measurement_window_log,
):
    """Test 1 of the measurement-mode plan, and the jts3 incident's actual fix.

    Without the window, jasper-voice's ``VolumeCoordinator`` patrol reconciles
    the fader back toward the household level once a second and fights the ramp
    the whole way up (journal: ``event=volume.reconciled source=idle
    drift_db=+9.35``). The window is what sets ``_measurement_active``.

    The window must wrap ``plan.open`` too, not only the ramp: the latch's own
    first write (to the quiet start floor) is a fader write like any other.
    Mutation-verified: deleting the ``async with measurement_window(...)`` from
    ``run_seat_level_ramp`` empties the log and turns the first assertion red.
    """
    volume, tone, mic = _rig(gain_db=-10.0)
    result = asyncio.run(
        _level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path)
    )
    assert result.status == "converged", result.reason

    assert measurement_window_log.log == ["window_enter", "window_exit"]
    # Owner-scoped, and the owner is the one mux registers for this pass.
    assert measurement_window_log.kwargs == [
        {"gate_owner": slr.SEAT_LEVEL_GATE_OWNER}
    ]


def test_the_seat_level_owner_is_registered_with_mux():
    """An unregistered owner is refused by mux, so the pass could never isolate.

    ``TEST_RELEASE`` is owner-scoped, which is why the name has to be in the
    registry rather than merely passed.
    """
    from jasper.mux import FANIN_TEST_OWNERS

    assert slr.SEAT_LEVEL_GATE_OWNER in FANIN_TEST_OWNERS


def test_a_window_that_will_not_open_refuses_the_pass(tmp_path, monkeypatch):
    """Another measurement owns the speaker, or mux cannot prove isolation.

    A leveling pass that ran anyway would be measuring household music.
    """
    from jasper.correction.coordinator import MeasurementWindowError

    def _refusing(**_kw):
        return FakeWindow(
            [], enter_error=MeasurementWindowError("a measurement is in progress"),
        )

    monkeypatch.setattr(slr, "measurement_window", _refusing)
    volume, tone, mic = _rig(gain_db=-10.0)
    result = asyncio.run(
        _level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path)
    )

    assert result.status == "refused"
    assert result.reason == slr.REFUSE_ISOLATION_UNAVAILABLE
    assert "a measurement is in progress" in (result.detail or "")
    # Nothing was mutated and nothing was banked.
    assert volume.commanded == []
    assert not (tmp_path / "seat_level_reference.json").exists()


def test_a_converged_pass_survives_a_failed_isolation_teardown(
    tmp_path, monkeypatch,
):
    """The pass finished and already restored the household volume.

    Reporting "refused" here would be a lie in the other direction -- the
    reference is on disk. Every lease self-expires within ~2 minutes, so the
    stuck isolation is logged loudly rather than turned into a lost result.
    """
    from jasper.correction.coordinator import MeasurementWindowError

    log: list[str] = []

    def _leaky(**_kw):
        return FakeWindow(
            log, exit_error=MeasurementWindowError("mux did not confirm release"),
        )

    monkeypatch.setattr(slr, "measurement_window", _leaky)
    volume, tone, mic = _rig(gain_db=-10.0)
    result = asyncio.run(
        _level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path)
    )

    assert result.status == "converged", result.reason
    assert log == ["window_enter", "window_exit"]
    assert (tmp_path / "seat_level_reference.json").exists()


# --- the jts3 new-horn bench night, replayed --------------------------------
#
# Two independent instrument defects surfaced on jts3 on 2026-08-23 while
# leveling a new horn, and neither could be diagnosed from anything the build
# emitted. Every number in this section is read off the receipts in
# ``captures/new-horn-2026-08/bringup/``, and the fixtures replay those receipts
# rather than modelling a chain -- so the published rises, medians and volumes
# ARE the bench's, and a regression shows up as a number that stopped matching.

# 83-seatlevel-run1-refused.json: --target 75, refused `spl_ceiling_exceeded`
# on a sample measuring 80.5 dB SPL against the 80.0 stop, 0.955 s after the
# step to -12.50 dB. Its five settled medians, and the ambient it measured.
RUN83_AMBIENT_DB_SPL = 49.65
RUN83_LEVELS = {
    -50.00: 50.78,
    -42.50: 53.45,
    -35.00: 52.33,
    -27.50: 57.90,
    -20.00: 64.61,
    # NOT a receipt number: -12.50 never produced a settled median, because the
    # window was abandoned. This is the run-log's own extrapolation -- run 87's
    # measured 1.100 dB/dB slope applied to BOTH converged runs' settled levels,
    # giving 73.94 (from 72.63 @ -13.69) and 74.55 (from 69.63 @ -16.97). It
    # sits INSIDE the [72.5, 77.5] band, which is the sharp end of the incident.
    -12.50: 74.0,
}
RUN83_TRIP_DB_SPL = 80.5
RUN83_TARGET = SeatLevelTarget(target_db_spl=75.0, tolerance_db=2.5)

# 86-seatlevel-68-converged.json: --target 68, converged, honest ambient.
RUN86_AMBIENT_DB_SPL = 49.73
RUN86_LEVELS = {
    -50.00: 50.59,
    -42.50: 50.67,
    -35.00: 51.63,
    -27.50: 57.36,
    -20.00: 64.97,
    -16.97: 69.63,
}
RUN86_RISES = [0.86, 0.94, 1.89, 7.63, 15.24, 19.90]
RUN86_TARGET = SeatLevelTarget(target_db_spl=68.0, tolerance_db=2.5)

# 87-seatlevel-72-converged.json: --target 72, converged, but with an ambient
# 7.45 dB above what the same mic read one second later -- so it published three
# NEGATIVE rises: tone readings quieter than the room they were taken in.
RUN87_AMBIENT_DB_SPL = 57.18
RUN87_LEVELS = {
    -50.00: 50.21,
    -42.50: 50.86,
    -35.00: 53.48,
    -27.50: 58.24,
    -20.00: 65.69,
    -13.69: 72.63,
}
RUN87_PUBLISHED_RISES = [-6.97, -6.32, -3.70, 1.06, 8.50, 15.45]
RUN87_TARGET = SeatLevelTarget(target_db_spl=72.0, tolerance_db=2.5)

# The bench ran against the headroom ceiling and the profile's stop, not this
# file's shared rig, and its 7.5 dB bite follows from that span.
BENCH_CEILING_DB = 0.0
BENCH_SPL_CEILING = 80.0


class ReplayableTone(BlockingTone):
    """A tone the pass can STOP and START again, which the re-measure needs.

    ``BlockingTone`` models a tone that plays once and is cancelled once, so its
    ``started`` latch never falls back. The silent re-measure genuinely stops the
    stimulus and starts it again, and a fixture whose mic cannot tell those apart
    would let a floor "measured in silence" be measured against a playing
    speaker -- the exact property under test.
    """

    def __init__(self) -> None:
        super().__init__()
        self.playing = False
        self.plays = 0
        self.stops = 0

    async def play(self) -> None:
        self.started = True
        self.playing = True
        self.plays += 1
        self._event = asyncio.Event()
        await self._event.wait()

    def cancel(self) -> None:
        self.cancelled = True
        if self.playing:
            self.stops += 1
        self.playing = False
        self._event.set()


class ScriptedMic:
    """A mic that replays one bench run's own medians, keyed by commanded volume.

    Deliberately not a model of a chain. The receipts' levels ARE the fixture,
    so the ramp steps through exactly the volumes the bench stepped through and
    publishes exactly the rises the bench published -- which is what lets a test
    assert against a receipt instead of against a simulation.

    ``excursion`` is ``(volume_db, sample_index, db_spl)``: from that sample of
    that volume's window on, the level is the excursion rather than the settled
    one. One sample of it is the incident's shape; a whole window of it is the
    shape the incident has to be told apart from, and one rig produces both.
    """

    def __init__(
        self,
        volume: Volume,
        tone: BlockingTone,
        *,
        ambient_db_spl: float,
        levels: dict[float, float],
        room_db_spl: float | None = None,
        excursion: tuple[float, int, float] | None = None,
        sensitivity: MicSensitivity = UMIK2,
    ) -> None:
        self._volume = volume
        self._tone = tone
        self._ambient_db_spl = ambient_db_spl
        # What a LATER silent window reads. ``None`` means the room really was
        # what the first window said, so a re-measure finds the same number --
        # the honest case. A different value models the run-87 defect: the first
        # window caught something the room no longer has.
        self._room_db_spl = (
            ambient_db_spl if room_db_spl is None else room_db_spl
        )
        self._levels = levels
        self._excursion = excursion
        self._sensitivity = sensitivity
        self._seq = 0
        self._at_volume = -1
        self._last_volume: float | None = None

    def _playing(self) -> bool:
        return getattr(self._tone, "playing", self._tone.started)

    def _db_spl(self, commanded: float) -> float:
        if not self._playing():
            # Before the tone has ever run this is the pre-tone ambient window;
            # after it has, the speaker is silent again and this is a re-measure
            # of the same room.
            return (
                self._room_db_spl
                if getattr(self._tone, "plays", 0)
                else self._ambient_db_spl
            )
        settled = self._levels[commanded]
        if self._excursion is None:
            return settled
        volume_db, index, level = self._excursion
        if commanded == volume_db and self._at_volume >= index:
            return level
        return settled

    async def next_samples(self) -> list[LevelSample]:
        commanded = round(self._volume.value, 2)
        if self._playing():
            self._at_volume = (
                self._at_volume + 1 if self._last_volume == commanded else 0
            )
            self._last_volume = commanded
        self._seq += 1
        rms = self._sensitivity.dbfs_from_db_spl(self._db_spl(commanded))
        return [
            LevelSample(
                seq=self._seq,
                t_client_ms=self._seq * 10,
                rms_dbfs=rms,
                peak_dbfs=rms + 3.0,
                clip=False,
                agc_frozen=True,
            )
        ]


def _bench_run(
    tmp_path,
    *,
    ambient_db_spl: float,
    levels: dict[float, float],
    target: SeatLevelTarget,
    room_db_spl: float | None = None,
    excursion: tuple[float, int, float] | None = None,
    mic_class: type[ScriptedMic] = ScriptedMic,
    tone: BlockingTone | None = None,
):
    """One replayed bench pass, against the bench's own ceiling and SPL stop."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    volume = Volume()
    tone = ReplayableTone() if tone is None else tone
    mic = mic_class(
        volume,
        tone,
        ambient_db_spl=ambient_db_spl,
        levels=levels,
        room_db_spl=room_db_spl,
        excursion=excursion,
    )
    return asyncio.run(
        _level(
            mic=mic,
            volume=volume,
            tone=tone,
            tmp_path=tmp_path,
            target=target,
            max_main_volume_db=BENCH_CEILING_DB,
            spl_ceiling_db_spl=BENCH_SPL_CEILING,
        )
    )


def test_the_replayed_bench_runs_reproduce_the_receipts_own_volumes(tmp_path):
    """The fixtures are the receipts, not an approximation of them.

    If this drifts, every assertion below is asserting against a simulation.
    """
    converged = _bench_run(
        tmp_path,
        ambient_db_spl=RUN86_AMBIENT_DB_SPL,
        levels=RUN86_LEVELS,
        target=RUN86_TARGET,
    )
    assert converged.status == "converged", converged.reason
    assert [step["volume_db"] for step in converged.ramp["steps"]] == list(RUN86_LEVELS)
    assert [step["observed_db_spl"] for step in converged.ramp["steps"]] == list(
        RUN86_LEVELS.values()
    )
    # The bite the bench ran with follows from the bench's own span.
    assert converged.ramp["bite_db"] == 7.5


# --- defect 1: the refusal published nothing about the window it stopped in --


def test_a_sample_domain_stop_publishes_the_window_it_abandoned(tmp_path):
    """The receipt, the event line and the prose all carry the aborted window.

    Before this, ``_settle_reading`` computed the aborted window's sample count
    and DROPPED it on the way out, and no per-sample SPL existed anywhere. The
    refusal's top-level keys were exactly ``detail, measured_db_spl, ramp,
    reason, reference_volume_db, restored, status`` -- nothing in them could
    separate one tail sample from a level that rose and stayed.
    """
    result = _bench_run(
        tmp_path,
        ambient_db_spl=RUN83_AMBIENT_DB_SPL,
        levels=RUN83_LEVELS,
        target=RUN83_TARGET,
        excursion=(-12.50, 19, RUN83_TRIP_DB_SPL),
    )

    assert result.reason == slr.REFUSE_SPL_CEILING_EXCEEDED
    window = result.ramp["stopped_window"]
    # Every field is present and typed -- a None here is a dropped measurement.
    assert isinstance(window["samples"], int) and window["samples"] > 0
    assert isinstance(window["retained"], int)
    for key in ("min_db_spl", "median_db_spl", "max_db_spl", "trip_db_spl"):
        assert isinstance(window[key], float), key
    assert isinstance(window["trip_offset_s"], float)
    # The trip is the loudest thing the window saw AND the sample that ended it,
    # so a reader can check those two against each other.
    assert window["trip_db_spl"] == pytest.approx(RUN83_TRIP_DB_SPL, abs=0.01)
    assert window["max_db_spl"] == window["trip_db_spl"]
    # ...about a second after the step that opened the window, as on the bench.
    assert window["trip_offset_s"] == pytest.approx(0.95, abs=0.06)
    # The settled median of the PRIOR window is already on the receipt, so the
    # window object does not restate it -- one writer per fact.
    assert "prior_db_spl" not in window
    assert result.ramp["steps"][-1]["observed_db_spl"] == RUN83_LEVELS[-20.00]


def test_the_refusal_prose_carries_the_window_and_names_the_prior_volume(tmp_path):
    """What an operator reading a terminal gets, without opening ``--json``.

    The prior reading was already in this sentence, but with no volume beside
    it -- which reads as if the ramp had SETTLED at the volume it stopped on,
    the one thing a sample-domain stop means it did not do.
    """
    result = _bench_run(
        tmp_path,
        ambient_db_spl=RUN83_AMBIENT_DB_SPL,
        levels=RUN83_LEVELS,
        target=RUN83_TARGET,
        excursion=(-12.50, 19, RUN83_TRIP_DB_SPL),
    )
    detail = result.detail or ""

    assert "stopped at -12.50 dB" in detail
    assert f"reading {RUN83_LEVELS[-20.00]:.1f} dB SPL at -20.00 dB" in detail
    assert "the window it stopped in saw" in detail
    assert f"{RUN83_TRIP_DB_SPL:.1f} dB SPL sample" in detail
    # The window's median is in the sentence too: it is the whole discriminator.
    assert f"median {RUN83_LEVELS[-12.50]:.1f}" in detail


def test_the_window_separates_a_tail_excursion_from_a_level_that_rose_and_stayed(
    tmp_path,
):
    """The discriminator the mechanism hunt needed and did not have.

    Two passes that refuse identically -- same reason, same measured value in
    the same sentence, same five settled readings behind them. One stopped on a
    single sample 6.5 dB above a settled level that was INSIDE the band; the
    other was over the stop from its first sample after the step. Only the
    window tells them apart.
    """
    tail = _bench_run(
        tmp_path / "tail",
        ambient_db_spl=RUN83_AMBIENT_DB_SPL,
        levels=RUN83_LEVELS,
        target=RUN83_TARGET,
        excursion=(-12.50, 19, RUN83_TRIP_DB_SPL),
    )
    stayed = _bench_run(
        tmp_path / "stayed",
        ambient_db_spl=RUN83_AMBIENT_DB_SPL,
        levels={**RUN83_LEVELS, -12.50: RUN83_TRIP_DB_SPL},
        target=RUN83_TARGET,
    )

    # Indistinguishable on everything that existed before the window did.
    assert tail.reason == stayed.reason == slr.REFUSE_SPL_CEILING_EXCEEDED
    assert tail.ramp["steps"] == stayed.ramp["steps"]
    assert (tail.detail or "").split("(")[0] == (stayed.detail or "").split("(")[0]

    # And separated by the window, in the direction the bench needs.
    tail_window, stayed_window = tail.ramp["stopped_window"], stayed.ramp["stopped_window"]
    assert tail_window["median_db_spl"] == pytest.approx(RUN83_LEVELS[-12.50], abs=0.1)
    assert tail_window["max_db_spl"] - tail_window["median_db_spl"] > 6.0
    assert stayed_window["median_db_spl"] == stayed_window["max_db_spl"]
    assert stayed_window["trip_offset_s"] == pytest.approx(0.0, abs=0.01)


def test_the_per_sample_series_is_one_debug_line_per_window(tmp_path, caplog):
    """The rising edge, reconstructable without new tooling -- and bounded.

    One line per WINDOW, values joined. One line per SAMPLE would be journal
    spam at ~12 samples a window and a dozen-plus windows a pass.

    Since #2919 a reading is SEVERAL windows -- as many as it takes for two of
    them to agree -- so the count is the settle's own published window counts,
    and ``attempt=`` is what orders them inside one reading.
    """
    with caplog.at_level(logging.DEBUG, logger=slr.logger.name):
        result = _bench_run(
            tmp_path,
            ambient_db_spl=RUN86_AMBIENT_DB_SPL,
            levels=RUN86_LEVELS,
            target=RUN86_TARGET,
        )
    lines = [
        record.getMessage()
        for record in caplog.records
        if "event=active_speaker.seat_level_window_samples" in record.getMessage()
    ]

    # Every window of every reading and not one more: the ambient reading (a
    # still room, so the minimum two) plus each climb reading's own count.
    windows = [step["windows"] for step in result.ramp["steps"]]
    assert windows == [2] * len(windows), "this fixture's levels never move"
    assert len(lines) == 2 + sum(windows)
    assert sum("window=ambient" in line for line in lines) == 2
    assert sum("window=-50.00" in line for line in lines) == 2
    # ...and the two windows of one reading are told apart by their attempt.
    at_start = [line for line in lines if "window=-50.00" in line]
    assert sorted(
        int(line.split(" attempt=")[1].split(" ")[0]) for line in at_start
    ) == [1, 2]
    assert all("\n" not in line for line in lines)
    # Each line carries its own samples as offset:level pairs, and the count it
    # claims is the count it printed.
    for line in lines:
        claimed = int(line.split(" samples=")[1].split(" ")[0])
        series = line.split('db_spl="')[1].rstrip('"')
        assert len(series.split(" ")) == claimed
        assert all(":" in pair for pair in series.split(" "))


def test_the_series_costs_nothing_when_debug_is_off(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger=slr.logger.name):
        _bench_run(
            tmp_path,
            ambient_db_spl=RUN86_AMBIENT_DB_SPL,
            levels=RUN86_LEVELS,
            target=RUN86_TARGET,
        )
    assert not [
        record
        for record in caplog.records
        if "seat_level_window_samples" in record.getMessage()
    ]


class FloodMic(ScriptedMic):
    """A source that delivers far faster than a sound card can.

    The cap's reason for existing: a production window holds ~24 samples, so
    nothing here can happen on the wired meter -- but the retained list and the
    single line built from it must be bounded by construction, not by trust.
    """

    async def next_samples(self):
        batch = []
        for _ in range(slr.WINDOW_TRACE_MAX_SAMPLES + 40):
            batch.extend(await super().next_samples())
        return batch


def test_the_window_trace_is_bounded_and_says_so(tmp_path):
    result = _bench_run(
        tmp_path,
        ambient_db_spl=RUN83_AMBIENT_DB_SPL,
        levels=RUN83_LEVELS,
        target=RUN83_TARGET,
        excursion=(-12.50, slr.WINDOW_TRACE_MAX_SAMPLES + 10, RUN83_TRIP_DB_SPL),
        mic_class=FloodMic,
    )

    window = result.ramp["stopped_window"]
    assert result.reason == slr.REFUSE_SPL_CEILING_EXCEEDED
    assert window["retained"] == slr.WINDOW_TRACE_MAX_SAMPLES
    assert window["samples"] > window["retained"]
    # The sample that STOPPED the window is recorded outside the cap, so
    # truncation can never be the reason a stop has no value attached.
    assert window["trip_db_spl"] == pytest.approx(RUN83_TRIP_DB_SPL, abs=0.01)
    assert window["max_db_spl"] < window["trip_db_spl"]


# --- defect 2: one half-second of room became every rise's denominator ------
#
# The floor that feeds the OBSERVING and BANKING guards is only ever a window
# measured with the speaker SILENT. Measured silence is the anti-coincidence
# property that makes "rise" mean "responded to the speaker": a mic that is not
# observing hears the same room either way, so its rise is ~0 however far the
# volume climbs. Deriving that floor from a climb reading destroys the property,
# which is what the first version of this fix did and what the demos below pin
# shut.

# jts3's true room on the night of runs 86/87, from the three passes' own
# -50.00 dB readings (50.78, 50.59, 50.21). Run 87's ambient window read 57.18.
RUN87_TRUE_ROOM_DB_SPL = 49.7


def test_a_reading_below_the_floor_re_measures_the_room_in_silence(tmp_path):
    """Run 87's exact numbers: an ambient window 7.45 dB above the real room.

    The tone is PLAYING, so a climb reading is the room plus the speaker and
    cannot be quieter than the room. One of the two windows is wrong, and the
    honest instrument answer is to measure the silence again -- not to believe
    the reading, which is a level the speaker was contributing to.
    """
    result = _bench_run(
        tmp_path,
        ambient_db_spl=RUN87_AMBIENT_DB_SPL,
        levels=RUN87_LEVELS,
        room_db_spl=RUN87_TRUE_ROOM_DB_SPL,
        target=RUN87_TARGET,
    )
    ramp = result.ramp

    assert result.status == "converged", result.reason
    # Both windows are published; neither overwrites the other.
    assert ramp["ambient_db_spl"] == pytest.approx(RUN87_AMBIENT_DB_SPL, abs=0.01)
    assert ramp["ambient_remeasured"] is True
    assert ramp["ambient_remeasured_db_spl"] == pytest.approx(
        RUN87_TRUE_ROOM_DB_SPL, abs=0.01
    )
    # Every rise is now against the SECOND silent window, and none is negative:
    # a reading cannot be quieter than the room it was taken in.
    rises = [step["rise_db"] for step in ramp["steps"]]
    assert min(rises) >= 0.0
    assert rises[0] == pytest.approx(
        RUN87_LEVELS[-50.00] - RUN87_TRUE_ROOM_DB_SPL, abs=0.02
    )

    # Control, from the same fixture: the un-remeasured floor is what published
    # the bench's three negative rises, so this is the instrument and not the
    # room. (The receipt rounds its medians to 2 decimals before this file reads
    # them, which is the only reason any cell moves by 0.01.)
    floor = UMIK2.dbfs_from_db_spl(RUN87_AMBIENT_DB_SPL)
    assert [
        UMIK2.dbfs_from_db_spl(level) - floor for level in RUN87_LEVELS.values()
    ] == pytest.approx(RUN87_PUBLISHED_RISES, abs=0.02)


def test_the_re_measure_stops_the_tone_and_starts_it_again(tmp_path):
    """The floor is measured in SILENCE, and the pass keeps playing afterwards.

    A "re-measure" that left the stimulus running would measure the speaker
    again under a new name, which is the whole property this fix restores.
    """
    tone = ReplayableTone()
    result = _bench_run(
        tmp_path,
        ambient_db_spl=RUN87_AMBIENT_DB_SPL,
        levels=RUN87_LEVELS,
        room_db_spl=RUN87_TRUE_ROOM_DB_SPL,
        target=RUN87_TARGET,
        tone=tone,
    )

    assert result.ramp["ambient_remeasured"] is True
    # Played, stopped for the silent window, played again -- and the final
    # teardown stops it once more.
    assert tone.plays == 2
    assert tone.stops >= 2
    assert not tone.playing


def test_the_room_is_re_measured_at_most_once(tmp_path):
    """Bounded by construction, so a contradicting room cannot loop the pass.

    A pass whose floor is contradicted twice is telling the operator about the
    room, not about the ambient window -- and the rise gate already reports
    that, as ``mic_not_observing``.
    """
    tone = ReplayableTone()
    # A mic reading far below every floor it is given, so every reading
    # contradicts. Only one re-measure may be spent.
    result = _bench_run(
        tmp_path,
        ambient_db_spl=RUN87_AMBIENT_DB_SPL,
        levels={
            volume: 40.0
            for volume in (-50.0, -42.5, -35.0, -27.5, -20.0, -12.5, -5.0, 0.0)
        },
        room_db_spl=RUN87_TRUE_ROOM_DB_SPL,
        target=RUN87_TARGET,
        tone=tone,
    )

    assert result.status == "refused"
    assert result.ramp["ambient_remeasured"] is True
    assert tone.plays == 2, "one re-measure, so exactly one replay"
    assert slr.REMEASURE_READINGS == 1


def test_an_honest_ambient_is_never_re_measured(tmp_path):
    """Run 86, two minutes earlier: the same room, an honest window.

    The re-measure must be invisible on a pass that did not need it, or it is a
    second source of truth for the room rather than a repair of a broken one --
    and it costs an audible pause nobody asked for.
    """
    tone = ReplayableTone()
    result = _bench_run(
        tmp_path,
        ambient_db_spl=RUN86_AMBIENT_DB_SPL,
        levels=RUN86_LEVELS,
        target=RUN86_TARGET,
        tone=tone,
    )
    ramp = result.ramp

    assert result.status == "converged", result.reason
    assert ramp["ambient_remeasured"] is False
    assert ramp["ambient_remeasured_db_spl"] is None
    assert tone.plays == 1, "no silent window, so no replay"
    # The rises are the receipt's, unchanged.
    assert [step["rise_db"] for step in ramp["steps"]] == pytest.approx(
        RUN86_RISES, abs=0.02
    )


def test_a_failed_silent_window_refuses_without_restarting_the_stimulus(tmp_path):
    """The re-measure's own failure path, and what it must not do to the room.

    The pass is about to refuse, so putting the stimulus back means an audible
    blip that lasts exactly as long as the fade takes to kill it. The refusal
    carries the silent window's own trace, because that is the window that
    failed -- not the climb reading that sent us there.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    volume = Volume()
    tone = ReplayableTone()

    class DeafOnceSilent(ScriptedMic):
        """Delivers nothing finite the moment the tone stops for the re-measure."""

        async def next_samples(self):
            if getattr(self._tone, "plays", 0) and not self._tone.playing:
                self._seq += 1
                return [
                    LevelSample(
                        seq=self._seq,
                        t_client_ms=self._seq * 10,
                        rms_dbfs=float("nan"),
                        peak_dbfs=float("nan"),
                        clip=False,
                        agc_frozen=True,
                    )
                ]
            return await super().next_samples()

    mic = DeafOnceSilent(
        volume,
        tone,
        ambient_db_spl=RUN87_AMBIENT_DB_SPL,
        levels=RUN87_LEVELS,
        room_db_spl=RUN87_TRUE_ROOM_DB_SPL,
    )
    result = asyncio.run(
        _level(
            mic=mic,
            volume=volume,
            tone=tone,
            tmp_path=tmp_path,
            target=RUN87_TARGET,
            max_main_volume_db=BENCH_CEILING_DB,
            spl_ceiling_db_spl=BENCH_SPL_CEILING,
        )
    )

    assert result.reason == slr.REFUSE_MIC_FEED_LOST
    assert tone.plays == 1, "the stimulus was restarted only to be torn down"
    assert not tone.playing
    # The silent window is the one that failed, so it is the one published.
    assert result.ramp["stopped_window"]["samples"] == 0
    assert not (tmp_path / "seat_level_reference.json").exists()

# --- the gate's demos: an unresponsive mic is not always a CONSTANT ---------


class WrongCardMic:
    """A mic on the WRONG CARD -- it hears a room, and the room wanders.

    The failure ``mic_not_observing`` exists for, in the shape the module's own
    docstring enumerates first ("capturing the wrong card"). Volume-independent
    by construction, but NOT constant: what it reports has nothing to do with the
    commanded volume, and it moves. The first version of this fix assumed such a
    mic reports a constant, re-based the floor onto its own quietest wander, and
    let a later louder wander clear the rise bar and BANK a reference.
    """

    def __init__(self, tone, *, ambient_db_spl, wander, sensitivity=UMIK2) -> None:
        self._tone = tone
        self._ambient_db_spl = ambient_db_spl
        self._wander = list(wander)
        self._sensitivity = sensitivity
        self._seq = 0
        self._window = 0
        self._in_window = 0

    def _level(self) -> float:
        # Window 0 is the pre-tone ambient; every window after it is the next
        # step of the wander -- INCLUDING a silent re-measure window, because a
        # mic on the wrong card hears a room that keeps moving whether or not
        # our speaker is playing. A model that froze the room while the tone was
        # off would hand the re-measure the very reading that triggered it, and
        # quietly re-create the defect this class exists to catch.
        if self._window == 0:
            return self._ambient_db_spl
        return self._wander[min(self._window - 1, len(self._wander) - 1)]

    async def next_samples(self) -> list[LevelSample]:
        self._in_window += 1
        if self._in_window >= 21:
            self._window += 1
            self._in_window = 0
        self._seq += 1
        rms = self._sensitivity.dbfs_from_db_spl(self._level())
        return [
            LevelSample(
                seq=self._seq,
                t_client_ms=self._seq * 10,
                rms_dbfs=rms,
                peak_dbfs=rms + 3.0,
                clip=False,
                agc_frozen=True,
            )
        ]


def _wrong_card_run(tmp_path, *, wander, target, ambient_db_spl=66.0):
    tmp_path.mkdir(parents=True, exist_ok=True)
    volume = Volume()
    tone = ReplayableTone()
    mic = WrongCardMic(tone, ambient_db_spl=ambient_db_spl, wander=wander)
    result = asyncio.run(
        _level(
            mic=mic,
            volume=volume,
            tone=tone,
            tmp_path=tmp_path,
            target=target,
            max_main_volume_db=BENCH_CEILING_DB,
            spl_ceiling_db_spl=BENCH_SPL_CEILING,
        )
    )
    return result, tmp_path / "seat_level_reference.json"


def test_a_wrong_card_mic_banks_nothing_however_its_room_wanders(tmp_path):
    """The gate's demo 1, through the production entry point.

    Ambient window 66 dB SPL; volume-independent readings of 60 then 67, with 67
    inside the band. Re-basing the floor onto the 60 gives the 67 a rise of 7 dB
    -- clear of the 6 dB bar -- and banks a reference for a level nothing
    produced. Against a floor measured in SILENCE the same wander cannot: the
    silent window hears the same room, so the rise is what it should be, ~0.
    """
    result, banked = _wrong_card_run(
        tmp_path,
        wander=[60.0, 67.0],
        target=SeatLevelTarget(target_db_spl=67.0, tolerance_db=2.5),
    )

    assert result.status == "refused", result.detail
    assert result.reference_volume_db is None
    assert result.measured_db_spl is None
    assert not banked.exists(), "a mic that never heard the speaker banked a reference"
    # Whatever it published as a rise, nothing cleared the emergence bar.
    assert max(step["rise_db"] for step in result.ramp["steps"]) < 6.0


def test_a_wandering_wrong_card_mic_refuses_with_the_MIC_remedy(tmp_path):
    """The gate's demo 2: the right refusal carries the right remedy.

    ``spl_target_unreachable`` sends the operator to the amplifier;
    ``mic_not_observing`` sends them to the microphone. A mic on the wrong card
    needs the second one, and a floor re-based onto its own quietest wander
    turned it into the first.
    """
    result, banked = _wrong_card_run(
        tmp_path,
        wander=[60.0, 67.0, 61.0, 66.0, 62.0, 65.0, 63.0, 64.0, 60.5, 66.5, 61.5],
        target=SeatLevelTarget(target_db_spl=75.0, tolerance_db=2.5),
    )

    assert result.reason == slr.REFUSE_MIC_NOT_OBSERVING
    assert "check that the mic is capturing the right card" in (result.detail or "")
    assert "raise the external amplifier" not in (result.detail or "")
    assert not banked.exists()


def test_a_room_lull_spanning_both_windows_still_banks_DOCUMENTED_LIMITATION(
    tmp_path,
):
    """The accepted residual, asserted as it behaves TODAY rather than wished away.

    The silent re-measure is anti-coincident with the SPEAKER, which is the real
    fix -- but it is NOT independent of its own trigger. It runs BECAUSE a
    reading landed low, about a second later, and room lulls autocorrelate over
    seconds. A lull still present when the silent window runs hands back the same
    low level, and a mic that never responded to the speaker banks a reference:

      ambient window 66.0 dB SPL -> reading 60.0 (lull) -> silent re-measure
      60.0 (same lull) -> reading 67.0 clears the 6 dB bar -> CONVERGED.

    So the guard fails on P(first window low) + P(first window high AND the
    re-measure lands low inside the same lull), where before this PR it failed on
    the first term alone. The second term is added, not traded: the first term is
    untouched, so the banking guard is marginally worse than it was. What the pass
    buys is on the other error type -- a contaminated window no longer disqualifies
    good readings -- and this test is what stops the next reader believing it is more
    than that.

    **Why nothing here closes it.** Closing it needs a separator between "the
    level moved" and "the level moved BECAUSE of the speaker" -- a response test.
    Deliberately not built: at these reading counts it is spoofable by the same
    wandering room, and `docs/measurement-loop-doctrine.md` section 5 says a
    refusal earns its place by naming a component-damage mechanism. This is
    measurement integrity, not safety: the harm is bounded because the banked
    reference only ever reaches the speaker through a `min()` with the
    per-driver admission caps. The disclosure is the mitigation --
    `ambient_remeasured` on the receipt, and a large NEGATIVE
    `remeasured_delta_db` on the event line, which is exactly this shape.

    If a future change closes this, invert the assertions rather than deleting
    the test: the case is the specification of what was accepted and when.
    """
    result, banked = _wrong_card_run(
        tmp_path,
        # The lull holds 60.0 across BOTH the triggering reading and the silent
        # window taken ~1 s later; only then does the room come back up.
        wander=[60.0, 60.0, 67.0],
        target=SeatLevelTarget(target_db_spl=67.0, tolerance_db=2.5),
        ambient_db_spl=66.0,
    )

    # TODAY'S BEHAVIOUR, not the desired one: a mic that never responded banks.
    assert result.status == "converged", result.detail
    assert banked.exists(), "the documented limitation stopped reproducing"
    assert result.ramp["ambient_remeasured"] is True
    assert result.ramp["ambient_remeasured_db_spl"] == pytest.approx(60.0, abs=0.01)
    # The tell an operator greps for: the silent window agreed with the low
    # reading that triggered it, 6 dB below the window before it.
    assert result.ramp["ambient_remeasured_db_spl"] - result.ramp[
        "ambient_db_spl"
    ] == pytest.approx(-6.0, abs=0.01)
    # This path is INTRODUCED by the re-measure, not inherited from before it: the
    # pre-#2918 rule REFUSES this same fixture. That is the second term the docstring
    # above describes -- added alongside the first term, which is untouched, but new.
    # Asserted for real in the provenance test below rather than claimed here,
    # because the rise this run publishes is computed under the CURRENT rule and says
    # nothing about what the old one would have done.
    assert max(step["rise_db"] for step in result.ramp["steps"]) >= 6.0


def _pre_2918_fixed_floor(monkeypatch, *, ambient_db_spl: float) -> None:
    """Put the pre-#2918 rule back: the floor is the first window and never moves.

    Modelled rather than reverted, and here is exactly how, so the next reader
    can judge it: ``_remeasure_silence`` is replaced by one that returns the
    FIRST window's level, consumes no samples, and does not touch the tone. The
    floor arithmetic is then identical to the old rule (one fixed number for the
    whole pass) and the mic sees the same window sequence a run with no silent
    window would have seen. The one difference is cosmetic and not asserted on:
    the receipt still reports ``ambient_remeasured`` true, where the old build
    had no such field at all.
    """

    async def _never_moves(*, tone, sensitivity, **_kwargs):
        return (
            slr._Reading(
                rms_dbfs=sensitivity.dbfs_from_db_spl(ambient_db_spl),
                samples=1,
                trace=slr._WindowTrace(samples=(), seen=0),
            ),
            tone,
        )

    monkeypatch.setattr(slr, "_remeasure_silence", _never_moves)


def test_the_lull_residual_is_INTRODUCED_by_the_re_measure_not_inherited(
    tmp_path, monkeypatch
):
    """Provenance of the limitation above, asserted instead of asserted-about.

    The same fixture -- ambient window 66.0, a mic that never responds, a lull
    holding 60.0 across both windows, a later 67.0 -- under the rule that shipped
    BEFORE the silent re-measure existed. With a floor fixed at the first
    window, the 67.0 reading rises 1.0 and stays there, never clearing the 6 dB
    emergence bar, so the pass refuses `spl_level_unconverged` and banks nothing.

    That makes the lull path **new**: it is the second term of the two-term statement
    in `_remeasure_silence` (`P(first window high AND the re-measure lands low inside
    the same lull)`), which the re-measure introduced without touching the first term
    -- still a thing this PR added, which is the sentence the limitation test above
    used to get backwards.
    """
    _pre_2918_fixed_floor(monkeypatch, ambient_db_spl=66.0)
    result, banked = _wrong_card_run(
        tmp_path,
        wander=[60.0, 60.0, 67.0],
        target=SeatLevelTarget(target_db_spl=67.0, tolerance_db=2.5),
        ambient_db_spl=66.0,
    )

    assert result.status == "refused"
    assert result.reason == slr.REFUSE_LEVEL_UNCONVERGED
    assert not banked.exists(), "the pre-#2918 rule banked this fixture"
    # The 67.0 reading is INSIDE the band, and still refuses: what stops it is
    # the emergence bar, measured against a floor that never moved.
    rises = [step["rise_db"] for step in result.ramp["steps"]]
    assert rises[:3] == pytest.approx([-6.0, -6.0, 1.0], abs=0.01)
    assert max(rises) == pytest.approx(1.0, abs=0.01)
    assert max(rises) < slr.MIC_RESPONSE_MIN_RISE_DB

def test_the_re_measure_event_carries_the_delta_and_the_rise_it_produced(
    tmp_path, caplog
):
    """One grep answers both questions the re-measure raises.

    A large NEGATIVE delta is the lull-matched residual above. A POSITIVE delta
    means the floor went UP, so the triggering reading -- and everything under
    the new floor -- publishes a NEGATIVE rise. That direction is conservative
    for banking (it makes readings harder to trust, never easier), so it is an
    observability matter rather than a guard, and it gets said on the line
    instead of left to be derived from two other fields.
    """
    with caplog.at_level(logging.INFO, logger=slr.logger.name):
        higher = _bench_run(
            tmp_path,
            ambient_db_spl=RUN87_AMBIENT_DB_SPL,
            levels=RUN87_LEVELS,
            # A silent window that reads HIGHER than the first one did.
            room_db_spl=60.0,
            target=RUN87_TARGET,
        )
    line = next(
        record.getMessage()
        for record in caplog.records
        if "seat_level_ambient_remeasured" in record.getMessage()
    )

    assert "remeasured_delta_db=+2.82" in line
    # The triggering reading's rise against the new floor is negative, and the
    # line says so rather than making a reader subtract two other fields.
    assert "rise_after_remeasure_db=-9.79" in line
    assert [step["rise_db"] for step in higher.ramp["steps"]][0] == pytest.approx(
        -9.79, abs=0.02
    )

def test_a_stuck_constant_mic_still_refuses(tmp_path):
    """The sub-case the falsified premise was true for, kept green.

    A mic pinned at one level reads the same in silence and under the tone, so
    its rise stays zero and the runaway guard fires. That was always the easy
    half; the wrong-card cases above are the half the premise missed.
    """
    volume, tone, mic = _rig(deaf=True)
    result = asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))

    assert result.reason == slr.REFUSE_MIC_NOT_OBSERVING
    assert result.ramp["ambient_remeasured"] is False
    assert {step["rise_db"] for step in result.ramp["steps"]} == {0.0}
    assert not (tmp_path / "seat_level_reference.json").exists()


def test_a_runaway_refusal_names_the_floor_its_rise_was_measured_against(tmp_path):
    """One room per sentence: the rise, and the floor it is a rise above.

    Reachable whenever the ambient window is contradicted AND the mic then never
    rises. The rise is computed against the re-measured floor, so naming the
    first window beside it would put two different rooms in one sentence.
    """
    result = _bench_run(
        tmp_path,
        ambient_db_spl=RUN87_AMBIENT_DB_SPL,
        # A mic pinned at the true room level: it contradicts the inflated
        # ambient window, and then never rises above the re-measured floor.
        levels={
            volume: RUN87_TRUE_ROOM_DB_SPL
            for volume in (-50.0, -42.5, -35.0, -27.5, -20.0, -12.5, -5.0, 0.0)
        },
        room_db_spl=RUN87_TRUE_ROOM_DB_SPL,
        target=RUN87_TARGET,
    )

    assert result.reason == slr.REFUSE_MIC_NOT_OBSERVING
    assert result.ramp["ambient_remeasured"] is True
    assert f"above the {RUN87_TRUE_ROOM_DB_SPL:.1f} dB SPL room" in (result.detail or "")
    assert f"{RUN87_AMBIENT_DB_SPL:.1f} dB SPL room" not in (result.detail or "")


# --- defect 3: a fixed settle banked a level that was still climbing --------
#
# jts3, 2026-08-24 (issue #2919). The pass waited a guessed half-second, took a
# median, and banked it -- however hard the level was still moving. Instrumented
# per-window rise (the window's last third minus its first third) at equal
# 7.5 dB commanded steps: -35.00 -> -0.42 dB, -27.50 -> +1.51, -20.00 -> +2.70,
# -12.50 -> +6.03. The top step's last four samples were its highest four, and
# three independent runs agree the level ARRIVING at -12.50 dB commanded was
# about 79-80 dB SPL. Ambient masking and a fixed-time-constant volume ramp were
# both ruled out by measurement and the mechanism was never named -- which is
# the shape of the fix: settling on the instrument's own STABILITY needs no
# model of what is moving.


class RisingMic(Mic):
    """A chain whose level CLIMBS toward its answer instead of onto it.

    A first-order approach in dB: each poll closes ``approach`` of the remaining
    gap, so the rise is steep at first and shallow later. That shape is what
    makes a fixed-length window's answer depend on WHEN it looked -- the defect
    -- and it is also what a stability test terminates on, since consecutive
    windows converge as the level does.

    Deliberately a MODEL of the measured shape and not a replay: #2919's
    mechanism is unnamed, so nothing here claims to be it. What is reproduced is
    the one property that matters -- a level still moving at the moment the
    retired settle stopped listening.
    """

    def __init__(self, *args, approach: float = 0.06, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.approach = approach
        self._current: float | None = None

    def arrived_db_spl(self, at_volume_db: float) -> float:
        """Where this chain ends up once it has finished climbing."""
        return UMIK2.db_spl_from_dbfs(
            _power_sum_db(self.ambient_dbfs, at_volume_db + self.gain_db)
        )

    def _rms_dbfs(self) -> float:
        target = super()._rms_dbfs()
        if not self._tone.started:
            return target
        if self._current is None:
            # The climb starts from the room, which is what the mic was
            # reporting the instant before the tone began.
            self._current = self.ambient_dbfs
        self._current += (target - self._current) * self.approach
        return self._current


async def _retired_fixed_settle(next_samples, **kwargs):
    """The pre-#2919 reading: drain one window, then believe the next one.

    Written against today's window primitive so the control differs from the
    production path in exactly ONE thing -- whether the pass waits for two
    windows to AGREE, or just waits a fixed length and banks whatever it has.
    """
    kwargs.pop("agree_db", None)
    kwargs.pop("timeout_s", None)
    kwargs.pop("window_s", None)
    started = kwargs["clock"]()
    reading = None
    for attempt in (1, 2):
        reading = await slr._window_reading(
            next_samples,
            attempt=attempt,
            started=started,
            window_s=slr.MIC_WINDOW_S,
            **kwargs,
        )
        if reading.rms_dbfs is None:
            return reading
    return reading


def _rising_rig(**mic_kwargs) -> tuple[Volume, BlockingTone, RisingMic]:
    volume = Volume()
    tone = BlockingTone()
    return volume, tone, RisingMic(volume, tone, **mic_kwargs)


def test_a_level_still_climbing_is_not_banked_until_it_stops_moving(tmp_path):
    """#2919's fix at the user-visible surface: the banked number ARRIVED.

    The chain climbs toward each commanded level rather than snapping to it, so
    a window taken early reads low. The pass keeps reading until two consecutive
    windows agree, which costs it more than the minimum two -- and the level it
    banks is the level the seat actually reaches, not one it was passing
    through.
    """
    volume, tone, mic = _rising_rig(
        gain_db=gain_for_seat_spl(80.0, at_volume_db=CEILING_DB),
        ambient_dbfs=UMIK2.dbfs_from_db_spl(45.0),
    )
    result = asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))

    assert result.status == "converged", (result.reason, result.detail)
    # It did NOT bank on the first pair of windows: a moving level buys more.
    windows = [step["windows"] for step in result.ramp["steps"]]
    assert max(windows) > 2, windows
    # And what it banked is where the chain ends up at that volume. The residual
    # a first-order chain still has left when consecutive windows stop
    # disagreeing is on the order of one agreement bar, so that is the tolerance
    # -- and the control below shows the retired shape missing by multiples of it.
    assert result.reference_volume_db is not None
    arrived = mic.arrived_db_spl(result.reference_volume_db)
    assert result.measured_db_spl == pytest.approx(arrived, abs=1.0)
    assert TARGET.low_db_spl <= result.measured_db_spl <= TARGET.high_db_spl


def test_the_retired_fixed_settle_banks_a_level_that_never_arrived(tmp_path):
    """The no-op control: put the guessed settle back and the defect returns.

    Same rig, same chain, one difference -- the reading is a fixed drain plus a
    fixed median instead of a wait for agreement. It banks a level several dB
    under the one the seat reaches, which is the 2026-08-24 incident: at
    -12.50 dB commanded the frame was labelled 75 while the seat arrived at
    about 79-80 dB SPL.
    """
    volume, tone, mic = _rising_rig(
        gain_db=gain_for_seat_spl(80.0, at_volume_db=CEILING_DB),
        ambient_dbfs=UMIK2.dbfs_from_db_spl(45.0),
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(slr, "_settle_reading", _retired_fixed_settle)
        result = asyncio.run(
            _level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path)
        )

    banked = result.measured_db_spl
    assert banked is not None, (result.reason, result.detail)
    arrived = mic.arrived_db_spl(result.reference_volume_db)
    # Under-read, in the direction and the order of magnitude the bench measured.
    assert arrived - banked > 2.0, (banked, arrived)


def test_a_still_level_settles_in_the_minimum_two_windows(tmp_path):
    """A chain that answers a step at once pays what it always paid.

    The cost side of the fix, asserted on the primitive: two windows is the
    fewest that can answer "did it stop moving", and a level that is already
    still needs no more than that. This is why the ten-second budget on the
    pass above survives an unbounded-looking wait.
    """
    clock = FakeClock()
    constant = UMIK2.dbfs_from_db_spl(70.0)

    async def _samples():
        return [
            LevelSample(
                seq=1,
                t_client_ms=0,
                rms_dbfs=constant,
                peak_dbfs=constant + 3.0,
                clip=False,
                agc_frozen=True,
            )
        ]

    reading = asyncio.run(
        slr._settle_reading(
            _samples,
            sensitivity=UMIK2,
            spl_ceiling_db_spl=SPL_CEILING,
            clock=clock.now,
            sleep=clock.sleep,
            session_id="test",
            window="ambient",
        )
    )

    assert reading.refusal is None
    assert reading.windows == 2
    assert reading.rms_dbfs == pytest.approx(constant)
    assert clock.now() == pytest.approx(2 * slr.MIC_WINDOW_S, abs=0.06)


class NeverSettlingMic:
    """A feed that keeps MOVING and never agrees with itself.

    A sawtooth once the tone plays: every window's median sits a long way from
    the one before it, forever, and every sample stays well under the
    commissioning stop -- so the only thing that can end one of these readings
    is the settle timeout. The room is quiet and still BEFORE the tone, so the
    ambient reading settles normally and the refusal lands mid-climb, where the
    tone has to be stopped and the household volume handed back.
    """

    def __init__(self, tone, *, ambient_db_spl: float = 45.0) -> None:
        self._tone = tone
        self._ambient_db_spl = ambient_db_spl
        self._seq = 0
        self._poll = 0

    async def next_samples(self) -> list[LevelSample]:
        self._seq += 1
        if self._tone.started:
            self._poll += 1
            db_spl = 55.0 + (self._poll % 20)
        else:
            db_spl = self._ambient_db_spl
        rms = UMIK2.dbfs_from_db_spl(db_spl)
        return [
            LevelSample(
                seq=self._seq,
                t_client_ms=self._seq * 10,
                rms_dbfs=rms,
                peak_dbfs=rms + 3.0,
                clip=False,
                agc_frozen=True,
            )
        ]


def test_a_feed_that_never_settles_refuses_instead_of_banking(tmp_path):
    """The honest end of an unbounded wait: a refusal, not the last number seen.

    A wait with no lag model behind it needs an outer bound, and the one thing
    that bound must NOT do is bank. The refusal names the two windows that
    disagreed and how far apart they were, so an operator can tell "the chain is
    still settling" from "this feed is not a level at all".
    """
    tone = BlockingTone()
    volume = Volume()
    mic = NeverSettlingMic(tone)
    result = asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))

    assert result.reason == slr.REFUSE_LEVEL_UNSETTLED
    assert result.reference_volume_db is None
    assert result.measured_db_spl is None
    assert not (tmp_path / "seat_level_reference.json").exists()
    detail = result.detail or ""
    assert "still moving" in detail
    assert "dB apart" in detail
    assert f"{slr.SETTLED_AGREE_DB:.1f} dB agreement bar" in detail
    # Stopped and restored like every other refusal.
    assert tone.cancelled
    assert result.restored is True


def test_an_unsettled_reading_beats_the_whole_operation_watchdog_to_it(tmp_path):
    """The per-reading bound fires first, so the operator gets the diagnosis.

    ``seat_level_watchdog_expired`` says "something this pass awaited never
    returned" and names nothing else. If the whole-operation budget were priced
    below what a reading may spend, every unsettled feed would report that slug
    instead of the two windows that disagreed -- which is the retired kernel's
    mistake pointed the other way, and is why the watchdog prices the settle
    timeout rather than a fixed window.
    """
    tone = BlockingTone()
    volume = Volume()
    mic = NeverSettlingMic(tone)
    result = asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))

    assert result.reason == slr.REFUSE_LEVEL_UNSETTLED
    assert result.ramp["watchdog_s"] > slr.SETTLE_TIMEOUT_S


def test_the_commissioning_stop_still_fires_on_a_sample_taken_mid_wait(tmp_path):
    """#2917's per-sample stop covers the whole wait, not just its first second.

    The stop runs on every sample of every window, so a reading that takes five
    windows to settle is five windows of samples checked against it -- strictly
    MORE coverage than the retired fixed settle had, never less. Asserted where
    it counts: the sample that trips is one a fixed one-second reading would
    have stopped listening before ever seeing.
    """
    volume, tone, mic = _rising_rig(
        # Climbs past the 85 dB SPL stop, but slowly enough that it is still
        # under it when a fixed one-second reading would have banked and moved
        # on to the next volume.
        gain_db=gain_for_seat_spl(95.0, at_volume_db=slr.SEAT_LEVEL_START_DB),
        ambient_dbfs=UMIK2.dbfs_from_db_spl(45.0),
        approach=0.02,
    )
    result = asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))

    assert result.reason == slr.REFUSE_SPL_CEILING_EXCEEDED
    assert not (tmp_path / "seat_level_reference.json").exists()
    window = result.ramp["stopped_window"]
    assert window["trip_db_spl"] > SPL_CEILING
    # Offsets run from the moment the READING began, so this says the trip
    # happened after the second window -- past where the retired settle stopped.
    assert window["trip_offset_s"] > 2 * slr.MIC_WINDOW_S


def test_the_settle_contract_is_published_on_the_receipt(tmp_path):
    """``steps[].windows`` cannot be read without the bar it was measured against.

    Three numbers, because one of them is operator-overridable and the other two
    are what make a window count mean anything. The count is also the pass's own
    measurement of how long this chain takes to answer a step -- the thing the
    2026-08-24 bench had to instrument by hand to find this defect at all.
    """
    result, _volume, _tone, _clock = _jts3_pass(tmp_path)
    ramp = result.ramp

    assert ramp["settle_window_s"] == pytest.approx(slr.MIC_WINDOW_S)
    assert ramp["settle_agree_db"] == pytest.approx(slr.SETTLED_AGREE_DB)
    assert ramp["settle_timeout_s"] == pytest.approx(slr.SETTLE_TIMEOUT_S)
    assert all(step["windows"] >= 2 for step in ramp["steps"])


def test_the_settle_knobs_are_read_from_the_environment(tmp_path, monkeypatch):
    """Both halves of "settled" are deploy-time knobs, bounded, and disclosed.

    A room that cannot hold the default agreement bar, or a chain that settles
    slower than the default timeout, has a way forward that is not a redeploy --
    and the receipt says which numbers the run actually used, because widening
    the first one lowers the bar for banking.
    """
    monkeypatch.setenv("JASPER_SEAT_LEVEL_SETTLED_AGREE_DB", "1.25")
    monkeypatch.setenv("JASPER_SEAT_LEVEL_SETTLE_TIMEOUT_S", "12")
    result, _volume, _tone, _clock = _jts3_pass(tmp_path)

    assert result.ramp["settle_agree_db"] == pytest.approx(1.25)
    assert result.ramp["settle_timeout_s"] == pytest.approx(12.0)

    # Out of range falls back to the shipped default rather than being clamped
    # to a number nobody asked for -- `bounded_env_float`'s own contract.
    monkeypatch.setenv("JASPER_SEAT_LEVEL_SETTLED_AGREE_DB", "40")
    monkeypatch.setenv("JASPER_SEAT_LEVEL_SETTLE_TIMEOUT_S", "0.05")
    result, _volume, _tone, _clock = _jts3_pass(tmp_path / "bounded")

    assert result.ramp["settle_agree_db"] == pytest.approx(slr.SETTLED_AGREE_DB)
    assert result.ramp["settle_timeout_s"] == pytest.approx(slr.SETTLE_TIMEOUT_S)
