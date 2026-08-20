# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Calibrated seat-SPL leveling: the conversion, the guards, the banked result.

Synthetic throughout — a fake clock, a fake confirming volume pair, a blocking
tone, and a mic that hears the POWER SUM of a room floor and a speaker that only
contributes while the tone plays. No ALSA, no CamillaDSP, no microphone.

That mic model is the point of this file. A speaker quieter than the room pins
the reading at ambient for the first several steps, so the level does not track
the commanded volume down there — and every rule about "is the microphone
actually hearing this" has to survive that without either false-aborting a
healthy chain or passing a mic that is not listening.

What must hold, and what would break if it did not:

* the dBFS -> dB SPL conversion matches a hand-computed value from a real
  UMIK-2 header — every SPL decision downstream is this arithmetic;
* a mic that is plugged in but NOT observing the speaker aborts the climb
  (``mic_not_observing``) instead of walking the volume to the ceiling; the
  mutation test below removes the guard and shows the ramp doing exactly that;
* a speaker quieter than the room is NOT aborted, and the no-op control shows
  the first-reading-relative rule really did abort it;
* a non-finite feed scores as no rise rather than leaving the guard unarmed;
* a stuck-constant mic never banks a reference, and the control shows that
  without a measured ambient floor it banked the ramp's own start volume;
* a measured level above the profile's commissioning ceiling aborts;
* an unreachable target refuses and banks NOTHING;
* the household volume is restored on every exit path — converged, refused,
  aborted, raised — and the hold is visible to the shared recovery machinery;
* only a converged, in-window lock writes a reference, and the reader accepts
  it only inside the safe envelope.
"""

from __future__ import annotations

import asyncio
import json
import math
import time

import pytest

from jasper.active_speaker import seat_level_ramp as slr
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
from jasper.audio_measurement.ramp import LevelSample

# The real header of the household UMIK-2 (serial 810-8494), verbatim, plus two
# curve rows so the file is a realistic whole.
UMIK2_CAL_TEXT = (
    '"Sens Factor =-12.07dB, AGain =18dB, SERNO: 8108494"\n'
    "10.054\t-6.6664\n"
    "10.179\t-6.4980\n"
)
UMIK2 = MicSensitivity(sens_factor_db=-12.07, analog_gain_db=18.0, serial="8108494")

# Below the runaway guard's abort level (start + probe span) on purpose, so
# ``max(commanded)`` in the guard tests reads the RAMP's peak and not the
# household level the latch restores at the end.
HOUSEHOLD_VOLUME_DB = -44.0
CEILING_DB = -6.0
SPL_CEILING = 85.0
TARGET = SeatLevelTarget(target_db_spl=77.5, tolerance_db=2.5)


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
        ambient_dbfs: float = -80.0,
        deaf: bool = False,
        stuck_dbfs: float = -75.0,
        nan_once_playing: bool = False,
    ) -> None:
        self._volume = volume
        self._tone = tone
        self.gain_db = gain_db
        self.ambient_dbfs = ambient_dbfs
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


def _always_ambient(value: float):
    """A stand-in for the ambient measurement that reports a fixed floor."""

    async def _measure(next_samples, *, clock, sleep, window_s=None):
        return value

    return _measure


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


# --- the ramp config --------------------------------------------------------


def test_ramp_window_is_the_band_converted_through_the_mic():
    config = slr.build_seat_level_ramp_config(
        target=TARGET, sensitivity=UMIK2, max_main_volume_db=CEILING_DB
    )
    assert config.window_low_dbfs == pytest.approx(-31.07)
    assert config.window_high_dbfs == pytest.approx(-26.07)
    assert config.cap_ceil_db == CEILING_DB
    assert config.start_db == slr.SEAT_LEVEL_START_DB
    # The kernel's own overshoot invariant holds: the staircase provably stops
    # below the window rather than climbing into it.
    assert config.pre_window < config.window_low_dbfs


def test_ramp_config_refuses_a_band_the_mic_cannot_capture():
    # A mic gained so hot it already reads +20 dBFS at the 94 dB SPL calibrator
    # clips long before 80 dB SPL: 80 + 20 - 94 = +6 dBFS, past full scale.
    hot = MicSensitivity(sens_factor_db=20.0)
    with pytest.raises(slr.SeatLevelRampError, match=slr.REFUSE_SPL_TARGET_UNCAPTURABLE):
        slr.build_seat_level_ramp_config(
            target=TARGET, sensitivity=hot, max_main_volume_db=CEILING_DB
        )


def test_ramp_config_refuses_a_ceiling_with_no_room_to_climb():
    with pytest.raises(slr.SeatLevelRampError, match=slr.REFUSE_VOLUME_CEILING_TOO_LOW):
        slr.build_seat_level_ramp_config(
            target=TARGET, sensitivity=UMIK2, max_main_volume_db=-55.0
        )


def test_ramp_config_refuses_a_band_too_narrow_for_the_overshoot_invariant():
    with pytest.raises(slr.SeatLevelRampError, match=slr.REFUSE_RAMP_CONFIG_INVALID):
        slr.build_seat_level_ramp_config(
            target=SeatLevelTarget(target_db_spl=77.5, tolerance_db=0.3),
            sensitivity=UMIK2,
            max_main_volume_db=CEILING_DB,
        )


# --- convergence ------------------------------------------------------------


def test_converged_ramp_banks_the_volume_that_measured_the_band(tmp_path):
    volume, tone, mic = _rig(gain_db=-10.0)
    result = asyncio.run(
        _level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path)
    )

    assert result.status == "converged", result.reason
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


def test_the_ramp_never_commands_above_the_stimulus_ceiling(tmp_path):
    volume, tone, mic = _rig(gain_db=-10.0)
    asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))
    assert max(volume.commanded) <= CEILING_DB + 1e-9


# --- the runaway guard, and the ambient floor it reads ----------------------


def test_a_mic_that_is_not_observing_aborts_the_climb(tmp_path):
    volume, tone, mic = _rig(deaf=True)
    result = asyncio.run(
        _level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path)
    )

    assert result.status == "refused"
    assert result.reason == slr.REFUSE_MIC_NOT_OBSERVING
    # It stopped near the probe span, NOT at the ceiling: the whole point.
    highest = max(volume.commanded)
    assert highest <= slr.SEAT_LEVEL_START_DB + slr.MIC_RESPONSE_PROBE_DB + 2.0
    assert highest < CEILING_DB
    assert tone.cancelled
    assert not (tmp_path / "seat_level_reference.json").exists()


def test_removing_the_runaway_guard_lets_a_dead_mic_walk_to_the_ceiling(
    tmp_path, monkeypatch
):
    """Mutation proof for the guard above.

    Neuter ONLY the runaway predicate and re-run the identical dead-mic
    scenario. The hazard then happens: the staircase walks the speaker to the
    ceiling on a mic that never heard a thing. Neither the kernel's
    feed-liveness timeout (samples ARE arriving) nor its trust floor (they are
    merely dropped) prevents that climb — only this guard does.
    """
    monkeypatch.setattr(
        slr._MicObservationGuard, "_runaway", lambda self, commanded_db: False
    )
    volume, tone, mic = _rig(deaf=True)
    result = asyncio.run(
        _level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path)
    )

    assert max(volume.commanded) == pytest.approx(CEILING_DB)
    assert result.reason != slr.REFUSE_MIC_NOT_OBSERVING
    assert CEILING_DB - (slr.SEAT_LEVEL_START_DB + slr.MIC_RESPONSE_PROBE_DB) > 20.0


@pytest.mark.parametrize(
    "seat_spl_at_start, ambient_db_spl",
    [
        pytest.param(35.0, 45.0, id="chain_80_ambient_45"),
        pytest.param(32.0, 42.0, id="chain_85_ambient_42"),
    ],
)
def test_a_speaker_quieter_than_the_room_is_not_falsely_aborted(
    tmp_path, seat_spl_at_start, ambient_db_spl
):
    """The gate's false-abort cases, and they must converge.

    At the quiet start the speaker is 10 dB BELOW the room, so the mic reads
    ambient and the level does not track the commanded volume at all down
    there. Measuring rise against the ambient floor (rather than against the
    first reading) is what lets this chain climb through the room and converge.
    """
    gain = gain_for_seat_spl(seat_spl_at_start, at_volume_db=slr.SEAT_LEVEL_START_DB)
    volume, tone, mic = _rig(
        gain_db=gain, ambient_dbfs=UMIK2.dbfs_from_db_spl(ambient_db_spl)
    )
    result = asyncio.run(
        _level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path)
    )
    assert result.status == "converged", result.reason
    assert TARGET.low_db_spl <= result.measured_db_spl <= TARGET.high_db_spl


def test_a_first_reading_relative_guard_would_have_aborted_those(tmp_path):
    """The no-op control for the test above: the OLD rule really did fire.

    Swap the ambient-relative predicate back to the first-reading-relative one
    the gate found, leave everything else identical, and the same healthy chain
    is refused. This is what makes the pass above evidence rather than a
    coincidence.
    """
    gain = gain_for_seat_spl(35.0, at_volume_db=slr.SEAT_LEVEL_START_DB)

    def _first_reading_relative(self, commanded_db: float) -> bool:
        if self._first_seen is None:
            self._first_seen = self._max_rms_dbfs
        if commanded_db - self._start_volume_db < 12.0:
            return False
        return (self._max_rms_dbfs or 0.0) - (self._first_seen or 0.0) < 6.0

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        slr._MicObservationGuard, "_first_seen", None, raising=False
    )
    monkeypatch.setattr(
        slr._MicObservationGuard, "_runaway", _first_reading_relative
    )
    try:
        volume, tone, mic = _rig(
            gain_db=gain, ambient_dbfs=UMIK2.dbfs_from_db_spl(45.0)
        )
        result = asyncio.run(
            _level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path)
        )
    finally:
        monkeypatch.undo()
    assert result.reason == slr.REFUSE_MIC_NOT_OBSERVING


def test_a_non_finite_feed_cannot_defeat_the_guard(tmp_path):
    """NaN scores as no rise, never as no evidence.

    The ambient window sees finite room samples; every sample once the tone
    starts is NaN. Nothing raises the observed maximum, so the rise stays zero
    and the guard fires — rather than sitting unarmed while the volume climbs.
    """
    volume, tone, mic = _rig(gain_db=-10.0, nan_once_playing=True)
    result = asyncio.run(
        _level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path)
    )
    assert result.reason == slr.REFUSE_MIC_NOT_OBSERVING
    assert max(volume.commanded) < CEILING_DB


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


# --- convergence needs a real rise, not just an in-band number --------------


def test_a_stuck_constant_in_band_mic_refuses_instead_of_banking(tmp_path):
    """The gate's repro: a mic pinned at a value that happens to sit inside the
    target window settles and confirms, and used to bank -50.0 dB — the ramp's
    own start volume — as a measured reference. Its ambient reading IS its
    signal reading, so its rise is zero and convergence now refuses."""
    in_band_dbfs = UMIK2.dbfs_from_db_spl(TARGET.target_db_spl)
    volume, tone, mic = _rig(deaf=True, stuck_dbfs=in_band_dbfs)
    result = asyncio.run(
        _level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path)
    )

    assert result.status == "refused"
    assert result.reference_volume_db is None
    assert not (tmp_path / "seat_level_reference.json").exists()


def test_without_a_measured_ambient_floor_the_stuck_mic_banks_minus_fifty(
    tmp_path, monkeypatch
):
    """The control that names the fix: measuring the room is what stops it.

    Replace the measured ambient floor with a floor so low nothing can be
    ambient-dominated — which is exactly the pre-fix state, where the kernel was
    handed no noise floor at all — and change NOTHING else. The same stuck mic
    now settles, confirms, and banks the ramp's own quiet start volume as a
    "measured" reference: the gate's reported -50.0 dB, reproduced.
    """
    monkeypatch.setattr(
        slr, "measure_ambient_dbfs", _always_ambient(-120.0)
    )
    in_band_dbfs = UMIK2.dbfs_from_db_spl(TARGET.target_db_spl)
    volume, tone, mic = _rig(deaf=True, stuck_dbfs=in_band_dbfs)
    result = asyncio.run(
        _level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path)
    )

    assert result.status == "converged"
    assert result.reference_volume_db == pytest.approx(slr.SEAT_LEVEL_START_DB)
    assert (tmp_path / "seat_level_reference.json").exists()


# --- the SPL ceiling --------------------------------------------------------


def test_a_measured_level_over_the_commissioning_ceiling_aborts(tmp_path):
    # G = +35 puts the very first reading once the tone starts at -15 dBFS ==
    # 91 dB SPL, above the profile's 85 dB SPL ceiling.
    volume, tone, mic = _rig(gain_db=35.0)
    result = asyncio.run(
        _level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path)
    )

    assert result.reason == slr.REFUSE_SPL_CEILING_EXCEEDED
    assert tone.cancelled
    assert not (tmp_path / "seat_level_reference.json").exists()


# --- refusals bank nothing, and always restore ------------------------------


def test_an_unreachable_target_refuses_and_banks_nothing(tmp_path):
    # G = -40: quiet enough that the band needs ~+11 dB of main volume, far
    # above the cap -- but loud enough to clear the room floor on the way, so
    # this is a genuine ceiling refusal and not a mic that never spoke.
    volume, tone, mic = _rig(gain_db=-40.0)
    result = asyncio.run(
        _level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path)
    )

    assert result.reason == slr.REFUSE_SPL_TARGET_UNREACHABLE
    assert result.reference_volume_db is None
    assert not (tmp_path / "seat_level_reference.json").exists()


@pytest.mark.parametrize(
    "mic_kwargs",
    [
        pytest.param({"gain_db": -10.0}, id="converged"),
        pytest.param({"gain_db": -40.0}, id="unreachable"),
        pytest.param({"deaf": True}, id="mic_not_observing"),
        pytest.param({"gain_db": 35.0}, id="spl_ceiling_exceeded"),
    ],
)
def test_the_household_volume_is_restored_on_every_exit_path(tmp_path, mic_kwargs):
    volume, tone, mic = _rig(**mic_kwargs)
    asyncio.run(_level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path))
    assert volume.value == pytest.approx(HOUSEHOLD_VOLUME_DB)
    # The latch resolved itself rather than leaving a stale intent behind.
    state = json.loads((tmp_path / "seat_level_volume.json").read_text())
    assert state["status"] == "resolved"


def test_a_raising_ramp_still_restores_the_household_volume(tmp_path):
    # The `finally` is the contract: even a caller-visible explosion inside the
    # kernel must not leave the speaker parked at the measurement level.
    volume = Volume()
    tone = BlockingTone()
    calls = {"n": 0}

    async def _boom() -> list[LevelSample]:
        calls["n"] += 1
        if calls["n"] <= 30:  # let the ambient window collect a floor first
            return [
                LevelSample(
                    seq=calls["n"], t_client_ms=0, rms_dbfs=-80.0, peak_dbfs=-77.0
                )
            ]
        raise KeyboardInterrupt("operator hit ctrl-c")

    clock = FakeClock()

    async def _go():
        await slr.run_seat_level_ramp(
            target=TARGET,
            sensitivity=UMIK2,
            max_main_volume_db=CEILING_DB,
            spl_ceiling_db_spl=SPL_CEILING,
            get_main_volume_db=volume.get,
            set_main_volume_db=volume.set,
            play_continuous_tone=tone.play,
            cancel_tone=tone.cancel,
            next_samples=_boom,
            clock=clock.now,
            sleep=clock.sleep,
            volume_state_path=tmp_path / "seat_level_volume.json",
            reference_state_path=tmp_path / "seat_level_reference.json",
        )

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(_go())
    assert volume.value == pytest.approx(HOUSEHOLD_VOLUME_DB)


def test_an_unconfirmable_volume_latch_refuses_before_any_ramp(tmp_path):
    class Drifting(Volume):
        async def set(self, db: float) -> bool:
            self.commanded.append(float(db))
            return True  # accepted, but the readback never moves

    volume = Drifting()
    tone = BlockingTone()
    mic = Mic(volume, tone, gain_db=-10.0)
    result = asyncio.run(
        _level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path)
    )
    assert result.reason == slr.REFUSE_VOLUME_LATCH_UNCONFIRMED
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
    result = asyncio.run(
        _level(mic=mic, volume=volume, tone=tone, tmp_path=tmp_path)
    )

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
