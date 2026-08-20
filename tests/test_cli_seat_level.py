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

from types import SimpleNamespace

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


# --- the whole verb, on a stubbed healthy box -------------------------------


def _stereo_wav(path, *, peak_int16=16384):
    """A two-channel WAV whose true peak is a known fraction of full scale."""
    import struct
    import wave

    with wave.open(str(path), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(48_000)
        out.writeframes(
            b"".join(struct.pack("<hh", peak_int16, 0) for _ in range(64))
        )
    return path


def test_stimulus_peak_reads_the_loudest_channel_not_a_downmix(tmp_path):
    # Half of int16 full scale on ONE channel, silence on the other. A downmix
    # would average to a quarter and report ~-12 dBFS, which would hand the
    # ceiling 6 dB it has not earned.
    wav = _stereo_wav(tmp_path / "half.wav", peak_int16=16384)
    assert seat_level.stimulus_peak_dbfs(wav) == pytest.approx(-6.02, abs=0.05)


def test_a_silent_stimulus_is_refused_not_treated_as_infinitely_quiet(tmp_path):
    wav = _stereo_wav(tmp_path / "silent.wav", peak_int16=0)
    with pytest.raises(ValueError, match="no signal"):
        seat_level.stimulus_peak_dbfs(wav)


def test_the_verb_reaches_the_ramp_on_a_healthy_commissioned_box(
    tmp_path, monkeypatch, capsys
):
    """End-to-end through main() to the ramp boundary.

    The regression this pins: ``_derive_bounds`` used to call
    ``resolve_commission_inputs()``, which returns ``(None, preview)`` on an
    ordinary box, and then dereferenced ``preset.safety`` — an AttributeError
    that escaped ``main()`` before any of this ran. Every stub below is a
    collaborator; the assertions are about what the CLI computed and handed on.
    """
    stimulus = _stereo_wav(tmp_path / "check.wav", peak_int16=32767)
    cal = tmp_path / "umik2.txt"
    cal.write_text(CAL_WITH_SENS)

    handed: dict = {}

    async def _fake_ramp(**kwargs):
        handed.update(kwargs)
        from jasper.active_speaker.seat_level_ramp import SeatLevelResult

        return SeatLevelResult(
            status="converged", reference_volume_db=-17.5, measured_db_spl=77.4
        )

    monkeypatch.setattr(seat_level, "run_seat_level_ramp", _fake_ramp)
    monkeypatch.setattr(
        seat_level, "_derive_bounds", lambda args, stim: (-30.0, 85.0)
    )
    monkeypatch.setattr(
        "jasper.audio_measurement.wired_capture.resolve_wired_mic",
        lambda: SimpleNamespace(pcm="hw:CARD=UMIK2,DEV=0"),
    )
    monkeypatch.setattr(
        "jasper.audio_measurement.wired_level_meter.WiredLevelMeter",
        lambda *a, **k: SimpleNamespace(
            start=lambda **kw: None, drain=lambda: [], stop=lambda: None
        ),
    )
    monkeypatch.setattr(
        "jasper.camilla.primary_controller",
        lambda: SimpleNamespace(get_volume_db=None, set_volume_db=None),
    )

    code = seat_level.main(
        [
            "--stimulus-wav",
            str(stimulus),
            "--calibration-file",
            str(cal),
            "--target-db-spl",
            "77.5",
        ]
    )

    assert code == 0
    assert "converged" in capsys.readouterr().out
    # The bounds and the calibration reached the ramp intact.
    assert handed["max_main_volume_db"] == -30.0
    assert handed["spl_ceiling_db_spl"] == 85.0
    assert handed["sensitivity"].sens_factor_db == -12.07
    assert (handed["target"].low_db_spl, handed["target"].high_db_spl) == (75.0, 80.0)


def test_derive_bounds_resolves_a_preset_without_an_explicit_one(monkeypatch, tmp_path):
    """The B1 root cause, isolated: the preset resolver must produce a preset.

    ``resolve_commission_inputs()`` alone returns ``None`` for the preset on an
    ordinary box; ``resolve_capture_preset(topology)`` is the sibling that
    compiles one or falls back to the bundled preset. This drives the REAL
    resolver against a stubbed topology/draft and asserts a real SPL ceiling
    comes back rather than an AttributeError on ``None``.
    """
    stimulus = _stereo_wav(tmp_path / "s.wav")
    monkeypatch.setattr(
        "jasper.output_topology.load_output_topology_strict",
        lambda _p: SimpleNamespace(topology_id="t"),
    )
    monkeypatch.setattr(
        "jasper.active_speaker.design_draft.load_design_draft",
        lambda **kw: {"driver_safety_profile": {"drivers": []}},
    )
    monkeypatch.setattr(
        "jasper.active_speaker.design_draft.declared_driver_sensitivities",
        lambda draft: {},
    )
    monkeypatch.setattr(
        "jasper.active_speaker.measurement.active_driver_targets",
        lambda topo: [{"target_fingerprint": "fp-woofer"}],
    )
    # The CLI binds this at import, so patch it where the CLI looks it up.
    monkeypatch.setattr(
        seat_level, "unsegmented_stimulus_ceiling_db", lambda *a, **k: -30.0
    )

    args = seat_level.build_parser().parse_args(["--stimulus-wav", str(stimulus)])
    ceiling_db, spl_ceiling = seat_level._derive_bounds(args, stimulus)

    assert ceiling_db == -30.0
    # A real number from a real preset — never an AttributeError on None.
    assert 45.0 <= spl_ceiling <= 85.0
