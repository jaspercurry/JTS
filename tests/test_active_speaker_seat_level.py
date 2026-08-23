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

    ``deaf=True`` is the failure the guard exists for: the device is open and
    delivering samples, but they never respond to the speaker — the wrong card,
    a mic in a bag, muted at the OS. It pins at ``stuck_dbfs`` forever, and
    because its ambient reading IS its signal reading, its rise is zero.
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
    # Any span is ceil(1 / BITE_FRACTION) bites; plus the first reading and the
    # two allowed misses, that is a fixed reading count of (settle + window),
    # the ambient window, and the slack.
    per_reading = 2 * slr.MIC_SETTLE_S
    bites = math.ceil(1 / slr.BITE_FRACTION)
    assert seconds == pytest.approx(
        slr.MIC_SETTLE_S
        + (1 + bites + slr.MAX_MISSED_FULL_STEPS) * per_reading
        + slr.WATCHDOG_SLACK_S
    )
    # ...and because the bite scales with the span, the budget is the SAME for a
    # wider one: the bite count is what the watchdog prices, and it is fixed.
    assert slr._watchdog_seconds(start_db=-70.0, ceiling_db=-6.8) == pytest.approx(
        seconds
    )


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
    """The jts3 rig: a 61 dB SPL room, the measured chain, the real ceiling."""
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
            spl_ceiling_db_spl=80.0,
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
    assert max(step["observed_db_spl"] for step in steps) < 80.0


def test_the_noisy_room_pass_is_audible_for_under_ten_seconds(tmp_path):
    """The owner's requirement, measured rather than asserted.

    Seven readings at one mic-settle drain plus one median window each. The
    ambient window before it is SILENT — nothing plays — so it is not audible
    time at all.
    """
    _result, _volume, tone, clock = _jts3_pass(tmp_path)

    assert tone.started_at is not None and tone.cancelled_at is not None
    audible_s = tone.cancelled_at - tone.started_at
    assert audible_s < 10.0
    # Seven readings account for it, plus the fade-before-tone-kill that follows
    # the last one -- which is audible time as well, and is bounded by the fade's
    # own step size.
    readings_s = 7 * (2 * slr.MIC_SETTLE_S)
    fade_s = (abs(-12.0 - slr.FADE_FLOOR_DB) / slr.FADE_STEP_DB) * slr.FADE_STEP_S
    assert readings_s <= audible_s <= readings_s + fade_s + slr.MIC_SETTLE_S


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

    Characterization, not approval: this is the behaviour a fix has to change.
    Both runs climbed in full bites from -50.00 dB, banked five readings, and
    the sixth window was abandoned by a sample above the profile's 80.0 dB SPL
    commissioning stop. Nothing was banked and the household volume came back.
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
    """No reading pair in either run showed an expansive chain to act on.

    A chain that answered a commanded dB with more than a dB would be visible
    here, and it is not: every consecutive pair of banked readings measured
    under 1.0 dB SPL per commanded dB. Subtracting the room does not lift the
    pair the estimator actually used either, so "the readings were still
    emerging from the room" does not account for the estimate being low.

    The 2.21 dB SPL per dB figure quoted for these runs is not in this set: it
    is the abandoned window's single sample measured against the previous
    window's twelve-sample median, which is a different statistic.
    """
    run = NEW_HORN_RUNS[name]
    slopes = _consecutive_slopes(run["readings"])
    assert max(slopes) < 1.0, slopes
    # The estimator's own window is the LAST two banked readings, and it is the
    # number the box printed (to the receipt's 2 dp rounding).
    assert slopes[-1] == pytest.approx(run["slope_estimate"], abs=0.002)

    room = run["ambient_db_spl"]
    (a_db, a_spl), (b_db, b_spl) = run["readings"][-2:]
    room_subtracted = (
        _room_subtracted_db(b_spl, room) - _room_subtracted_db(a_spl, room)
    ) / (b_db - a_db)
    assert room_subtracted < 1.0
    # ...and both readings were already well clear of the room by the pass's own
    # emergence bar, so neither was room-pinned.
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
