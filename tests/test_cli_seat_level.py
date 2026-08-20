# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The seat-SPL leveling CLI's refusals — the ones that must land before audio.

Every check that can be made without touching hardware runs BEFORE the mic is
opened or a note is played, so an operator who typed something wrong hears
nothing at all. The load-bearing one: a microphone with no parseable
``Sens Factor`` has no absolute level reference, so the verb refuses rather than
ramping a speaker against an uncalibrated number.
"""

from __future__ import annotations

import pytest

from jasper.audio_measurement.calibration import MicSensitivity
from jasper.cli import seat_level

CAL_WITH_SENS = (
    '"Sens Factor =-12.07dB, AGain =18dB, SERNO: 8108494"\n10.0\t-6.6\n10.2\t-6.5\n'
)
CAL_CURVE_ONLY = "10.0\t-6.6\n10.2\t-6.5\n"


def _args(**overrides):
    args = seat_level.build_parser().parse_args(
        ["--stimulus-wav", overrides.pop("stimulus_wav", "/nonexistent.wav")]
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_resolve_sensitivity_reads_an_explicit_calibration_file(tmp_path):
    path = tmp_path / "umik2.txt"
    path.write_text(CAL_WITH_SENS)
    assert seat_level._resolve_sensitivity(_args(calibration_file=str(path))) == (
        MicSensitivity(sens_factor_db=-12.07, analog_gain_db=18.0, serial="8108494")
    )


@pytest.mark.parametrize(
    "text, name",
    [
        pytest.param(CAL_CURVE_ONLY, "curve_only.txt", id="no_sens_factor_line"),
        pytest.param(None, "absent.txt", id="file_missing"),
    ],
)
def test_resolve_sensitivity_is_none_when_there_is_no_absolute_reference(
    tmp_path, text, name
):
    path = tmp_path / name
    if text is not None:
        path.write_text(text)
    assert seat_level._resolve_sensitivity(_args(calibration_file=str(path))) is None


def test_missing_calibration_refuses_before_the_mic_is_opened(tmp_path, monkeypatch):
    stimulus = tmp_path / "check.wav"
    stimulus.write_bytes(b"RIFF....WAVE")
    cal = tmp_path / "curve_only.txt"
    cal.write_text(CAL_CURVE_ONLY)

    def _never(*_a, **_k):  # pragma: no cover - asserted by not being called
        raise AssertionError("hardware was touched despite a missing calibration")

    monkeypatch.setattr(
        "jasper.audio_measurement.wired_capture.resolve_wired_mic", _never
    )
    monkeypatch.setattr("jasper.camilla.primary_controller", _never)

    code = seat_level.main(
        ["--stimulus-wav", str(stimulus), "--calibration-file", str(cal)]
    )
    assert code == 1


def test_a_missing_stimulus_refuses_first(tmp_path, capsys):
    cal = tmp_path / "umik2.txt"
    cal.write_text(CAL_WITH_SENS)
    code = seat_level.main(
        [
            "--stimulus-wav",
            str(tmp_path / "nope.wav"),
            "--calibration-file",
            str(cal),
            "--json",
        ]
    )
    assert code == 1
    assert seat_level.REFUSE_STIMULUS_MISSING in capsys.readouterr().out


def test_the_verb_requires_a_way_to_find_the_calibration(tmp_path):
    with pytest.raises(SystemExit):
        seat_level.main(["--stimulus-wav", str(tmp_path / "x.wav")])


def test_defaults_are_the_operators_stated_band():
    args = seat_level.build_parser().parse_args(["--stimulus-wav", "x.wav"])
    target = seat_level.SeatLevelTarget(
        target_db_spl=args.target_db_spl, tolerance_db=args.tolerance_db
    )
    assert (target.low_db_spl, target.high_db_spl) == (75.0, 80.0)
