# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Calibrated seat-SPL leveling: the conversion, the guards, the banked result.

Synthetic throughout — a fake clock, a fake confirming volume pair, a modelled
mic chain (``mic_dbfs = commanded volume + G``) and a blocking tone. No ALSA, no
CamillaDSP, no mic.

What must hold, and what would break if it did not:

* the dBFS -> dB SPL conversion matches a hand-computed value from a real
  UMIK-2 header — every SPL decision downstream is this arithmetic;
* a mic that is plugged in but NOT observing the speaker aborts the climb
  (``mic_not_observing``) instead of walking the volume to the ceiling. The
  mutation test below removes the guard and shows the ramp doing exactly that;
* a measured level above the profile's commissioning ceiling aborts;
* an unreachable target refuses and banks NOTHING;
* the household volume is restored on every exit path — converged, refused,
  aborted;
* only a converged, in-window lock writes a reference, and the reader accepts
  it only inside the safe envelope.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from jasper.active_speaker import seat_level_ramp as slr
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


class Mic:
    """A modelled mic feed.

    ``gain_db`` is the chain gain ``G`` in ``mic_dbfs = commanded + G``.
    ``deaf=True`` models the failure this whole guard exists for: the device is
    open and delivering samples, but they never respond to the speaker — a mic
    capturing the wrong card, sealed in a bag, or muted at the OS. It pins at
    ``deaf_dbfs`` forever.
    """

    def __init__(
        self,
        volume: Volume,
        *,
        gain_db: float = -10.0,
        deaf: bool = False,
        deaf_dbfs: float = -75.0,
    ) -> None:
        self._volume = volume
        self.gain_db = gain_db
        self.deaf = deaf
        self.deaf_dbfs = deaf_dbfs
        self._seq = 0

    async def next_samples(self) -> list[LevelSample]:
        self._seq += 1
        rms = self.deaf_dbfs if self.deaf else self._volume.value + self.gain_db
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


async def _level(
    *,
    mic: Mic,
    volume: Volume,
    tmp_path,
    target: SeatLevelTarget = TARGET,
    sensitivity: MicSensitivity = UMIK2,
    max_main_volume_db: float = CEILING_DB,
    spl_ceiling_db_spl: float = SPL_CEILING,
    noise_floor_dbfs: float | None = -80.0,
):
    clock = FakeClock()
    tone = BlockingTone()
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
        noise_floor_dbfs=noise_floor_dbfs,
        clock=clock.now,
        sleep=clock.sleep,
        volume_state_path=tmp_path / "seat_level_volume.json",
        reference_state_path=tmp_path / "seat_level_reference.json",
    )
    return result, tone


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
    volume = Volume()
    mic = Mic(volume, gain_db=-10.0)
    result, tone = asyncio.run(_level(mic=mic, volume=volume, tmp_path=tmp_path))

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


def test_the_ramp_never_commands_above_the_driver_cap_ceiling(tmp_path):
    volume = Volume()
    mic = Mic(volume, gain_db=-10.0)
    asyncio.run(_level(mic=mic, volume=volume, tmp_path=tmp_path))
    assert max(volume.commanded) <= CEILING_DB + 1e-9


# --- the runaway guard (the load-bearing one) -------------------------------


def test_a_mic_that_is_not_observing_aborts_the_climb(tmp_path):
    volume = Volume()
    mic = Mic(volume, deaf=True)
    result, tone = asyncio.run(_level(mic=mic, volume=volume, tmp_path=tmp_path))

    assert result.status == "refused"
    assert result.reason == slr.REFUSE_MIC_NOT_OBSERVING
    # It stopped near the probe span, NOT at the ceiling: the whole point.
    highest = max(volume.commanded)
    assert highest <= slr.SEAT_LEVEL_START_DB + slr.MIC_RESPONSE_PROBE_DB + 2.0
    assert highest < CEILING_DB
    assert tone.cancelled
    # Nothing banked.
    assert not (tmp_path / "seat_level_reference.json").exists()


def test_removing_the_runaway_guard_lets_a_dead_mic_walk_to_the_ceiling(
    tmp_path, monkeypatch
):
    """Mutation proof for the guard above.

    Neuter ONLY the runaway predicate and re-run the identical dead-mic
    scenario. The hazard then happens: the staircase walks the speaker all the
    way to the driver-cap ceiling on a mic that never heard a thing, ~32 dB
    louder than where the guard stops it. Neither the kernel's feed-liveness
    timeout (samples ARE arriving) nor its trust floor (they are merely
    dropped) prevents that climb — only this guard does.
    """
    monkeypatch.setattr(
        slr._MicObservationGuard, "_runaway", lambda self, commanded_db: False
    )
    volume = Volume()
    mic = Mic(volume, deaf=True)
    result, _tone = asyncio.run(_level(mic=mic, volume=volume, tmp_path=tmp_path))

    assert max(volume.commanded) == pytest.approx(CEILING_DB)
    assert result.reason != slr.REFUSE_MIC_NOT_OBSERVING
    # The guarded run stops ~32 dB quieter than this.
    assert CEILING_DB - (slr.SEAT_LEVEL_START_DB + slr.MIC_RESPONSE_PROBE_DB) > 30.0


def test_a_responding_mic_never_trips_the_runaway_guard(tmp_path):
    # A very quiet chain: every early reading is under the trust floor (so the
    # kernel drops them all) yet the level DOES track the commanded volume. The
    # guard must read the untrusted samples and let this through.
    volume = Volume()
    mic = Mic(volume, gain_db=-45.0)
    result, _tone = asyncio.run(_level(mic=mic, volume=volume, tmp_path=tmp_path))
    assert result.reason == slr.REFUSE_SPL_TARGET_UNREACHABLE


# --- the SPL ceiling --------------------------------------------------------


def test_a_measured_level_over_the_commissioning_ceiling_aborts(tmp_path):
    # G = +35 puts the very first quiet-start reading at -15 dBFS == 91 dB SPL,
    # above the profile's 85 dB SPL ceiling.
    volume = Volume()
    mic = Mic(volume, gain_db=35.0)
    result, tone = asyncio.run(_level(mic=mic, volume=volume, tmp_path=tmp_path))

    assert result.reason == slr.REFUSE_SPL_CEILING_EXCEEDED
    assert tone.cancelled
    assert not (tmp_path / "seat_level_reference.json").exists()


# --- refusals bank nothing, and always restore ------------------------------


def test_an_unreachable_target_refuses_and_banks_nothing(tmp_path):
    # G = -60: the band would need ~+30 dB of main volume, far above the cap.
    volume = Volume()
    mic = Mic(volume, gain_db=-60.0)
    result, _tone = asyncio.run(_level(mic=mic, volume=volume, tmp_path=tmp_path))

    assert result.reason == slr.REFUSE_SPL_TARGET_UNREACHABLE
    assert result.reference_volume_db is None
    assert not (tmp_path / "seat_level_reference.json").exists()


@pytest.mark.parametrize(
    "mic_kwargs",
    [
        pytest.param({"gain_db": -10.0}, id="converged"),
        pytest.param({"gain_db": -60.0}, id="unreachable"),
        pytest.param({"deaf": True}, id="mic_not_observing"),
        pytest.param({"gain_db": 35.0}, id="spl_ceiling_exceeded"),
    ],
)
def test_the_household_volume_is_restored_on_every_exit_path(tmp_path, mic_kwargs):
    volume = Volume()
    mic = Mic(volume, **mic_kwargs)
    asyncio.run(_level(mic=mic, volume=volume, tmp_path=tmp_path))
    assert volume.value == pytest.approx(HOUSEHOLD_VOLUME_DB)
    # The latch resolved itself rather than leaving a stale intent behind.
    state = json.loads((tmp_path / "seat_level_volume.json").read_text())
    assert state["status"] == "resolved"


def test_a_raising_ramp_still_restores_the_household_volume(tmp_path):
    # The `finally` is the contract: even a caller-visible explosion inside the
    # kernel must not leave the speaker parked at the measurement level.
    volume = Volume()
    mic = Mic(volume, gain_db=-10.0)

    async def _boom() -> list[LevelSample]:
        raise KeyboardInterrupt("operator hit ctrl-c")

    clock = FakeClock()
    tone = BlockingTone()

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
            noise_floor_dbfs=-80.0,
            clock=clock.now,
            sleep=clock.sleep,
            volume_state_path=tmp_path / "seat_level_volume.json",
            reference_state_path=tmp_path / "seat_level_reference.json",
        )

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(_go())
    assert volume.value == pytest.approx(HOUSEHOLD_VOLUME_DB)
    del mic


def test_an_unconfirmable_volume_latch_refuses_before_any_ramp(tmp_path):
    class Drifting(Volume):
        async def set(self, db: float) -> bool:
            self.commanded.append(float(db))
            return True  # accepted, but the readback never moves

    volume = Drifting()
    mic = Mic(volume, gain_db=-10.0)
    result, tone = asyncio.run(_level(mic=mic, volume=volume, tmp_path=tmp_path))
    assert result.reason == slr.REFUSE_VOLUME_LATCH_UNCONFIRMED
    assert not tone.started


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
