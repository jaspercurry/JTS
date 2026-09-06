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

import json
from types import SimpleNamespace

import pytest

from jasper.audio_measurement.calibration import (
    MicSensitivity,
    resolve_mic_sensitivity,
)
from jasper.active_speaker.seat_level_reference import (
    seat_level_reference_state_path,
)
from jasper.cli import seat_level
from jasper.cli._refusal import STATUS_BY_CODE

CAL_WITH_SENS = (
    '"Sens Factor =-12.07dB, AGain =18dB, SERNO: 8108494"\n10.0\t-6.6\n10.2\t-6.5\n'
)
CAL_CURVE_ONLY = "10.0\t-6.6\n10.2\t-6.5\n"


def test_resolve_sensitivity_reads_an_explicit_calibration_file(tmp_path):
    path = tmp_path / "umik2.txt"
    path.write_text(CAL_WITH_SENS)
    assert resolve_mic_sensitivity(calibration_file=str(path)) == (
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
    assert resolve_mic_sensitivity(calibration_file=str(path)) is None


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
    """The shared refusal document, whose ``status`` its exit code picks."""
    cal = tmp_path / "umik2.txt"
    cal.write_text(CAL_WITH_SENS)
    code = seat_level.main(
        ["--stimulus-wav", str(tmp_path / "nope.wav"), "--calibration-file", str(cal)]
    )
    assert code == seat_level.EXIT_REFUSED

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == STATUS_BY_CODE[seat_level.EXIT_REFUSED]
    assert payload["reason"] == seat_level.REFUSE_STIMULUS_MISSING
    # Everything the eight-key document used to publish as top-level keys.
    assert set(payload["detail"]) == {
        "detail",
        "measured_db_spl",
        "ramp",
        "reachable_target_db_spl",
        "reference_volume_db",
        "restored",
    }


def test_the_verb_installs_a_handler_so_its_receipt_reaches_the_journal(
    tmp_path,
):
    """The disclosure receipt has a production reader, proved without caplog.

    ``unsegmented_stimulus_ceiling_db`` logs the caps this ceiling drives past
    at INFO. The root logger defaults to WARNING and this module registers no
    handler of its own, so before ``main`` called ``basicConfig`` every field
    of that receipt was computed and discarded on a real run — and a guard
    written with ``caplog.at_level("INFO")`` cannot see that, because forcing
    the level is precisely the thing production does not do.

    So this test takes the root logger away from pytest for the duration:
    handlers cleared, level restored to the WARNING default, ``main`` run for
    real, and then a record emitted through the module's OWN logger at INFO
    has to arrive somewhere. Restored in ``finally`` either way.
    """
    import logging

    cal = tmp_path / "umik2.txt"
    cal.write_text(CAL_WITH_SENS)

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    received: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            received.append(record)

    try:
        root.handlers = []
        root.setLevel(logging.WARNING)
        code = seat_level.main(
            [
                "--stimulus-wav",
                str(tmp_path / "nope.wav"),
                "--calibration-file",
                str(cal),
            ]
        )
        assert code == 1
        # ``basicConfig`` ran and it ran at INFO, not at the WARNING default
        # that hid the event.
        assert root.handlers, "the CLI installed no log handler"
        assert root.level == logging.INFO
        root.addHandler(_Capture())
        logging.getLogger(
            "jasper.active_speaker.session_volume_plan"
        ).info("event=active_speaker.unsegmented_ceiling_bound probe=1")
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)

    assert any("unsegmented_ceiling_bound" in r.getMessage() for r in received)


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
    assert seat_level.stimulus_provenance(wav).peak_dbfs == pytest.approx(
        -6.02, abs=0.05
    )


def test_the_same_read_measures_the_RMS_the_refusal_discloses(tmp_path):
    """One read, both numbers — a second read is a second answer.

    A constant half-scale on one channel of two: the peak is -6.02 dBFS and the
    RMS over the whole interleaved array is 3 dB below it, because half the
    samples are the silent channel. The crest that falls out is what decides
    whether a seat-SPL target is reachable at all, so it is measured rather
    than assumed.
    """
    wav = _stereo_wav(tmp_path / "half.wav", peak_int16=16384)

    levels = seat_level.stimulus_provenance(wav)

    assert levels.peak_dbfs == pytest.approx(-6.02, abs=0.05)
    assert levels.rms_dbfs == pytest.approx(-9.03, abs=0.05)


def test_a_silent_stimulus_is_refused_not_treated_as_infinitely_quiet(tmp_path):
    wav = _stereo_wav(tmp_path / "silent.wav", peak_int16=0)
    with pytest.raises(ValueError, match="no signal"):
        seat_level.stimulus_provenance(wav)


def test_the_stimulus_read_carries_the_file_identity_the_reference_banks(tmp_path):
    """#3477: a level with no identity beside it is a half-recorded stimulus.

    ``dB SPL = stimulus dBFS + chain gain + volume``. Two references banked
    against different WAVs differ by the stimulus term, and a consumer holding
    only the levels cannot tell that from a chain change — so WHICH file was
    played is part of the same one read, never a second one that could see a
    swapped symlink.
    """
    import hashlib

    wav = _stereo_wav(tmp_path / "half.wav", peak_int16=16384)

    provenance = seat_level.stimulus_provenance(wav)

    assert provenance.path == str(wav)
    assert provenance.sha256 == hashlib.sha256(wav.read_bytes()).hexdigest()
    assert provenance.band_hz is None
    assert provenance.peak_dbfs == pytest.approx(-6.02, abs=0.05)


def _band_profile(*bands):
    """A driver-safety profile that declares nothing but each driver's band."""
    return {
        "targets": [
            {"target_fingerprint": f"fp-{index}", "measurement_band_hz": list(band)}
            for index, band in enumerate(bands)
        ]
    }


@pytest.mark.parametrize(
    "bands, expected",
    [
        # Two declared bands that do not overlap: the generated default has to
        # cover BOTH drivers, so the hull is the answer and an intersection
        # would be empty.
        pytest.param(([45.0, 3000.0], [2000.0, 18000.0]), (45.0, 18000.0), id="two_way"),
        # A single declaration is its own hull.
        pytest.param(([60.0, 16000.0],), (60.0, 16000.0), id="full_range"),
        # Declarations outside the global driver-test limits are clamped to
        # them, never obeyed past them.
        pytest.param(([1.0, 40_000.0],), (20.0, 23_000.0), id="clamped_to_the_limits"),
    ],
)
def test_the_default_stimulus_is_derived_from_the_declared_bands(
    tmp_path, monkeypatch, bands, expected
):
    """#3475: with no ``--stimulus-wav`` the tool synthesizes its own.

    Band, level and duration all come from declarations that already exist —
    the drivers' ``measurement_band_hz``, the active-driver capture source
    level, and the branch-peak render bound — so nothing here is a property of
    one rig, one room, or one operator's home directory.
    """
    from jasper.active_speaker.branch_peak import MAX_STIMULUS_SAMPLES
    from jasper.active_speaker.commissioning_admission import (
        ACTIVE_DRIVER_CAPTURE_SOURCE_DBFS,
    )

    profile = _band_profile(*bands)
    targets = [
        {"target_fingerprint": target["target_fingerprint"]}
        for target in profile["targets"]
    ]
    _stub_draft(monkeypatch, {"driver_safety_profile": profile}, targets)

    seen = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return tmp_path / "generated.wav"

    monkeypatch.setattr(
        "jasper.audio_measurement.playback.ensure_bandlimited_noise_wav", _capture
    )

    args = seat_level.build_parser().parse_args([])
    assert args.stimulus_wav is None
    declarations = seat_level._load_declarations(args)
    path, band_hz = seat_level.default_stimulus_wav(declarations)

    assert path == tmp_path / "generated.wav"
    assert band_hz == expected
    assert (seen["f_lo_hz"], seen["f_hi_hz"]) == expected
    # The level is the one the driver-capture excitation already uses, so a
    # reference banked against the default sits at the same digital level the
    # session's own programs do.
    assert seen["dbfs"] == ACTIVE_DRIVER_CAPTURE_SOURCE_DBFS
    # Long enough to be worth playing, short enough that the per-branch peak
    # solve stays EXACT — past the render bound it degrades to the conservative
    # full-band bound, which is exactly the second-order effect #3475 reports.
    assert seen["duration_s"] * seen["sample_rate"] <= MAX_STIMULUS_SAMPLES


def test_the_generated_default_reaches_the_ramp_with_its_own_provenance(
    tmp_path, monkeypatch, capsys
):
    """No ``--stimulus-wav`` is a supported run, not ``stimulus_wav_missing``."""
    cal = tmp_path / "umik2.txt"
    cal.write_text(CAL_WITH_SENS)
    generated = _stereo_wav(tmp_path / "noise.wav", peak_int16=8192)

    handed: dict = {}

    async def _fake_ramp(**kwargs):
        handed.update(kwargs)
        from jasper.active_speaker.seat_level_ramp import SeatLevelResult

        return SeatLevelResult(
            status="converged", reference_volume_db=-17.5, measured_db_spl=77.4
        )

    monkeypatch.setattr(seat_level, "run_seat_level_ramp", _fake_ramp)
    _stub_declarations(monkeypatch)
    monkeypatch.setattr(
        seat_level,
        "default_stimulus_wav",
        lambda declarations: (generated, (45.0, 18_000.0)),
    )
    monkeypatch.setattr(
        seat_level, "_derive_bounds", lambda stim, levels, declarations: (-30.0, 85.0)
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

    code = seat_level.main(["--calibration-file", str(cal)])

    assert code == 0
    assert "converged" in capsys.readouterr().err
    assert handed["stimulus"].path == str(generated)
    # The declared band rides with the file, so the reference banks WHAT was
    # generated rather than leaving it to be re-parsed out of a filename.
    assert handed["stimulus"].band_hz == (45.0, 18_000.0)


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
    _stub_declarations(monkeypatch)
    monkeypatch.setattr(
        seat_level, "_derive_bounds", lambda stim, levels, declarations: (-30.0, 85.0)
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
    assert "converged" in capsys.readouterr().err
    # The bounds and the calibration reached the ramp intact.
    assert handed["max_main_volume_db"] == -30.0
    assert handed["spl_ceiling_db_spl"] == 85.0
    assert handed["sensitivity"].sens_factor_db == -12.07
    assert (handed["target"].low_db_spl, handed["target"].high_db_spl) == (75.0, 80.0)
    # …and so did what the stimulus itself MEASURES, which bounds nothing and
    # is what lets `spl_target_unreachable` show its own arithmetic. Full scale
    # on one channel of two: 0 dBFS peak, 3 dB down in RMS.
    assert handed["stimulus"].peak_dbfs == pytest.approx(0.0, abs=0.05)
    assert handed["stimulus"].rms_dbfs == pytest.approx(-3.01, abs=0.05)


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
        "jasper.active_speaker.design_draft.declared_effective_driver_sensitivities",
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
    ceiling_db, spl_ceiling = seat_level._derive_bounds(
        stimulus,
        seat_level.stimulus_provenance(stimulus),
        seat_level._load_declarations(args),
    )

    assert ceiling_db == -30.0
    # A real number from a real preset — never an AttributeError on None.
    assert 45.0 <= spl_ceiling <= 85.0


# --- the honest ceiling: the two conservatisms, at the CLI's own call site ---
#
# JTS3's declared numbers: woofer admitted at -8.0 dBFS, declared sensitivities
# 108.5 (tweeter) / 83.3 (woofer), and a -14.4 dB L-pad on the tweeter recorded
# in the same design draft.

_PAD_DB = -14.4
_SENS_WOOFER = 83.3
_SENS_TWEETER = 108.5


def _draft_with_a_padded_tweeter(safety_profile):
    return {
        "driver_safety_profile": safety_profile,
        "manual_settings": {
            "drivers": [
                {"role": "woofer", "sensitivity_db_2v83_1m": _SENS_WOOFER},
                {
                    "role": "tweeter",
                    "sensitivity_db_2v83_1m": _SENS_TWEETER,
                    "pad": {"attenuation_db": _PAD_DB},
                },
            ]
        },
    }


def _stub_declarations(monkeypatch):
    """Stub the one declaration load for tests that stub the derivations too."""
    monkeypatch.setattr(
        seat_level,
        "_load_declarations",
        lambda args: seat_level._Declarations(None, {}, {}),
    )


def _stub_draft(monkeypatch, draft, targets):
    monkeypatch.setattr(
        "jasper.output_topology.load_output_topology_strict",
        lambda _p: SimpleNamespace(topology_id="t"),
    )
    monkeypatch.setattr(
        "jasper.active_speaker.design_draft.load_design_draft", lambda **kw: draft
    )
    monkeypatch.setattr(
        "jasper.active_speaker.measurement.active_driver_targets", lambda topo: targets
    )


def test_seat_level_hands_the_ceiling_the_PAD_FOLDED_sensitivities(
    tmp_path, monkeypatch
):
    """Conservatism 1, pinned at the one production site that originates it.

    ``jasper/cli/seat_level.py`` was the only place in the tree that read a
    sensitivity mapping out of the draft and fed it to ceiling math with the
    NAKED reader — ``/sound/room/``'s crossover-v2 flow already reads the
    pad-folded sibling. An L-pad'd tweeter's acoustic output is quieter than
    its datasheet rating by exactly the pad, and the derived HF ceiling is a
    sensitivity DELTA, so the naked figure protects the driver as if it were
    14.4 dB more sensitive than it physically is.

    Mutation guard: put ``declared_driver_sensitivities`` back in
    ``_derive_bounds`` and the captured mapping carries the naked 108.5.
    """
    stimulus = _stereo_wav(tmp_path / "s.wav")
    draft = _draft_with_a_padded_tweeter({"drivers": []})
    _stub_draft(monkeypatch, draft, [{"target_fingerprint": "fp", "output_index": 0}])
    monkeypatch.setattr(seat_level, "_applied_branch_peaks", lambda *a, **k: None)

    seen = {}

    def _capture(*_a, **kwargs):
        seen.update(kwargs)
        return -30.0

    monkeypatch.setattr(seat_level, "unsegmented_stimulus_ceiling_db", _capture)
    args = seat_level.build_parser().parse_args(["--stimulus-wav", str(stimulus)])
    seat_level._derive_bounds(
        stimulus,
        seat_level.stimulus_provenance(stimulus),
        seat_level._load_declarations(args),
    )

    assert seen["declared_sensitivities"] == {
        "woofer": _SENS_WOOFER,
        "tweeter": _SENS_TWEETER + _PAD_DB,
    }
    assert seen["declared_sensitivities"]["tweeter"] != _SENS_TWEETER


def test_an_unreadable_applied_graph_falls_back_to_the_conservative_bound(
    tmp_path, monkeypatch, caplog
):
    """Fail-conservative at the graph-reading boundary.

    No statefile, an unparseable config, a graph carrying a filter the render
    cannot model — every one of them hands the ceiling ``None`` and it derives
    the full-band bound, which is the number that shipped. The reason is
    logged: a silent fallback is indistinguishable from a genuinely tight
    graph, and sends an operator hunting the wrong number.
    """
    stimulus = _stereo_wav(tmp_path / "s.wav")
    monkeypatch.setattr(
        "jasper.active_speaker.environment.read_camilla_statefile_config_path",
        lambda *a, **k: None,
    )
    with caplog.at_level("INFO", logger="jasper.cli.seat_level"):
        peaks = seat_level._applied_branch_peaks(
            stimulus, [{"target_fingerprint": "fp", "output_index": 0}]
        )
    assert peaks is None
    assert "event=active_speaker.seat_level_branch_peaks_unavailable" in caplog.text

    caplog.clear()
    missing = tmp_path / "gone.yml"
    monkeypatch.setattr(
        "jasper.active_speaker.environment.read_camilla_statefile_config_path",
        lambda *a, **k: str(missing),
    )
    with caplog.at_level("INFO", logger="jasper.cli.seat_level"):
        assert (
            seat_level._applied_branch_peaks(
                stimulus, [{"target_fingerprint": "fp", "output_index": 0}]
            )
            is None
        )
    assert "seat_level_branch_peaks_unavailable" in caplog.text


@pytest.mark.parametrize(
    "document, shape",
    [
        ("", "an empty statefile"),
        ("[]", "a list document"),
        ("- one\n- two", "a populated list document"),
        ("just a string", "a scalar string document"),
        ("42", "a scalar int document"),
        ("{{{ not yaml", "an unparseable document"),
    ],
)
def test_an_unreadable_graph_DOCUMENT_still_reaches_the_conservative_fallback(
    tmp_path, monkeypatch, caplog, document, shape
):
    """The fallback has to survive the shapes a half-written statefile takes.

    ``yaml.safe_load`` answers an empty file with None, a list document with a
    list, and a scalar document with a str/int — none of which carry ``.get``.
    Reaching the render with one of those raised an AttributeError that the
    catch tuple here does not name, so the conservative fallback was skipped
    and the verb crashed instead of quietly bounding by the full-band peak.
    Every one of these must come back ``None``, logged.
    """
    stimulus = _stereo_wav(tmp_path / "s.wav")
    graph = tmp_path / "applied.yml"
    graph.write_text(document, encoding="utf-8")
    monkeypatch.setattr(
        "jasper.active_speaker.environment.read_camilla_statefile_config_path",
        lambda *a, **k: str(graph),
    )
    with caplog.at_level("INFO", logger="jasper.cli.seat_level"):
        peaks = seat_level._applied_branch_peaks(
            stimulus, [{"target_fingerprint": "fp", "output_index": 0}]
        )
    assert peaks is None, shape
    assert "event=active_speaker.seat_level_branch_peaks_unavailable" in caplog.text


def _jts3_shaped_graph(*, woofer_channel: int, tweeter_channel: int, fc_hz=1648.7):
    """A basin-2-shaped two-way: mono-sum split, an LR4 pair, and the tweeter's
    own inverted trim / shelf / peaking chain.

    The two channel indexes are arguments rather than literals because role and
    output index are NOT positionally related — the shipped 2-way emitter
    fixture puts the tweeter on channel 0, and this test's topology puts it on
    channel 1. Taking them from the topology is what makes this fixture a test
    of the render's own fingerprint-to-channel pairing instead of a restatement
    of it.
    """
    mono_leg = -6.020599913
    return {
        "devices": {
            "samplerate": 48000,
            "capture": {"channels": 2},
            "playback": {"channels": 2},
        },
        "filters": {
            "active_baseline_headroom": {
                "type": "Gain",
                "parameters": {"gain": 0.0, "inverted": False, "mute": False},
            },
            "as_woofer_lp": {
                "type": "BiquadCombo",
                "parameters": {
                    "type": "LinkwitzRileyLowpass", "freq": fc_hz, "order": 4,
                },
            },
            "as_woofer_delay": {
                "type": "Delay",
                "parameters": {"delay": 0.35, "unit": "ms"},
            },
            "as_woofer_baseline_limiter": {
                "type": "Limiter",
                "parameters": {"soft_clip": True, "clip_limit": -1.0},
            },
            "as_tweeter_hp": {
                "type": "BiquadCombo",
                "parameters": {
                    "type": "LinkwitzRileyHighpass", "freq": fc_hz, "order": 4,
                },
            },
            "as_tweeter_shelf": {
                "type": "Biquad",
                "parameters": {
                    "type": "Highshelf", "freq": 4000.0, "q": 0.707, "gain": -6.17,
                },
            },
            "as_tweeter_peak_1": {
                "type": "Biquad",
                "parameters": {
                    "type": "Peaking", "freq": 2600.0, "q": 2.0, "gain": -3.2,
                },
            },
            "as_tweeter_peak_2": {
                "type": "Biquad",
                "parameters": {
                    "type": "Peaking", "freq": 6100.0, "q": 3.0, "gain": -2.1,
                },
            },
            "as_tweeter_peak_3": {
                "type": "Biquad",
                "parameters": {
                    "type": "Peaking", "freq": 11000.0, "q": 1.5, "gain": -1.4,
                },
            },
            "as_tweeter_baseline_gain": {
                "type": "Gain",
                "parameters": {"gain": -4.6081, "inverted": True, "mute": False},
            },
            "as_tweeter_baseline_limiter": {
                "type": "Limiter",
                "parameters": {"soft_clip": True, "clip_limit": -1.0},
            },
        },
        "mixers": {
            "split_active_2way": {
                "channels": {"in": 2, "out": 2},
                "mapping": [
                    {
                        "dest": d,
                        "sources": [
                            {"channel": 0, "gain": mono_leg, "inverted": False},
                            {"channel": 1, "gain": mono_leg, "inverted": False},
                        ],
                    }
                    for d in (0, 1)
                ],
            }
        },
        "pipeline": [
            {
                "type": "Filter",
                "channels": [0, 1],
                "names": ["active_baseline_headroom"],
            },
            {"type": "Mixer", "name": "split_active_2way"},
            {
                "type": "Filter",
                "channels": [woofer_channel],
                "names": [
                    "as_woofer_lp", "as_woofer_delay", "as_woofer_baseline_limiter",
                ],
            },
            {
                "type": "Filter",
                "channels": [tweeter_channel],
                "names": [
                    "as_tweeter_hp", "as_tweeter_shelf", "as_tweeter_peak_1",
                    "as_tweeter_peak_2", "as_tweeter_peak_3",
                    "as_tweeter_baseline_gain", "as_tweeter_baseline_limiter",
                ],
            },
        ],
    }


def _broadband_wav(path, *, peak_dbfs=-12.0, seconds=3.0):
    """Content on BOTH sides of the crossover, on one channel only — the shape
    a session check program has, and the shape that makes the mono-sum leg and
    each branch's own filtering visible in the rendered peak."""
    import numpy as np
    from scipy.io import wavfile

    rate = 48000
    n = int(rate * seconds)
    t = np.arange(n) / rate
    rng = np.random.default_rng(11)
    signal = (
        np.sin(2.0 * np.pi * 220.0 * t)
        + 0.9 * np.sin(2.0 * np.pi * 5200.0 * t)
        + 0.35 * rng.normal(0.0, 1.0, n)
    )
    signal = signal / np.max(np.abs(signal)) * (10.0 ** (peak_dbfs / 20.0))
    stereo = np.zeros((n, 2))
    stereo[:, 0] = signal
    wavfile.write(str(path), rate, (stereo * 32767.0).astype(np.int16))
    return path


def _jts3_safety_profile(topology):
    from jasper.active_speaker.driver_safety import build_driver_safety_profile

    def _driver(target_id, role, peak, required):
        return {
            "target_id": target_id,
            "role": role,
            "model": f"model-{role}",
            "hard_excitation_band_hz": [500, 20_000],
            "measurement_band_hz": [500, 10_000],
            **({"recommended_highpass_hz": 1500} if role == "tweeter" else {}),
            "level_duration_limits": {
                "max_effective_peak_dbfs": peak,
                "max_sweep_duration_s": 6,
                "max_repeat_count": 3,
                "minimum_cooldown_s": 0,
            },
            "required_protection_filters": required,
            "cabinet": {
                "enclosure_kind": "sealed",
                "radiator_count": 1,
                "effective_radiating_diameter_mm": 132 if role == "woofer" else 25,
                **({"baffle_width_mm": 210} if role == "woofer" else {}),
            },
        }

    return build_driver_safety_profile(
        topology,
        manual_settings={
            "drivers": [
                _driver("mono:woofer", "woofer", -8.0, [
                    {"kind": "lowpass", "cutoff_hz": 3000,
                     "minimum_slope_db_per_octave": 24}
                ]),
                _driver("mono:tweeter", "tweeter", -65.0, [
                    {"kind": "highpass", "cutoff_hz": 5000,
                     "minimum_slope_db_per_octave": 24}
                ]),
            ],
            "crossover_candidates": [],
        },
        driver_research=None,
        saved_at="2026-07-13T12:00:00Z",
    )


def test_the_honest_ceiling_end_to_end_on_a_jts3_shaped_speaker(tmp_path, monkeypatch):
    """The de-nannied ceiling through the REAL derivation.

    Real ``_derive_bounds``, real ``unsegmented_stimulus_ceiling_db``, real
    branch render, real driver-safety profile — only the topology and the
    statefile lookup are stubbed.

    Two things are pinned end to end. The ceiling is full scale less the
    LOUDEST branch peak the render found, which on this fixture is the woofer's;
    and it clears the ceiling the declared caps used to impose (-21.2 dB on
    these numbers) by tens of decibels, which is the 2026-08-23 ruling.

    The magnitudes are this fixture's own — a branch peak is a property of one
    stimulus through one graph and is never transferable, which is the whole
    reason the render is redone per stimulus rather than cached.
    """
    import yaml

    from jasper.active_speaker.measurement import active_driver_targets
    from jasper.active_speaker.session_volume_plan import (
        unsegmented_stimulus_ceiling_db,
    )
    from tests.active_speaker_fixtures import mono_output_topology

    topology = mono_output_topology()
    profile = _jts3_safety_profile(topology)
    targets = active_driver_targets(topology)
    by_role = {t["role"]: t["target_fingerprint"] for t in targets}
    channels = {t["role"]: t["output_index"] for t in targets}
    # The topology's own channel assignment is what the render is keyed on, and
    # it is NOT positional — this topology puts the woofer on 0 where the
    # shipped 2-way emitter fixture puts it on 1. The graph below is built from
    # these indexes so the test proves the pairing rather than restating it.
    assert set(channels) == {"woofer", "tweeter"}
    assert sorted(channels.values()) == [0, 1]

    stimulus = _broadband_wav(tmp_path / "check.wav")
    graph = tmp_path / "applied.yml"
    graph.write_text(
        yaml.safe_dump(
            _jts3_shaped_graph(
                woofer_channel=channels["woofer"],
                tweeter_channel=channels["tweeter"],
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jasper.active_speaker.environment.read_camilla_statefile_config_path",
        lambda *a, **k: str(graph),
    )
    _stub_draft(monkeypatch, _draft_with_a_padded_tweeter(profile), targets)

    args = seat_level.build_parser().parse_args(["--stimulus-wav", str(stimulus)])
    ceiling_db, spl_ceiling = seat_level._derive_bounds(
        stimulus,
        seat_level.stimulus_provenance(stimulus),
        seat_level._load_declarations(args),
    )

    peak = seat_level.stimulus_provenance(stimulus).peak_dbfs
    fingerprints = [t["target_fingerprint"] for t in targets]
    full_band = unsegmented_stimulus_ceiling_db(
        profile, fingerprints, stimulus_peak_dbfs=peak,
        declared_sensitivities={"woofer": _SENS_WOOFER, "tweeter": _SENS_TWEETER},
    )

    # The bound with no render: full scale less the stimulus's own peak.
    assert full_band == pytest.approx(-peak, abs=0.01)
    # With the render: full scale less the LOUDEST branch, which is the woofer
    # here — the quiet tweeter branch no longer holds the volume down.
    branch = seat_level._applied_branch_peaks(stimulus, targets)
    assert set(branch) == set(fingerprints)
    assert branch[by_role["tweeter"]] < branch[by_role["woofer"]]
    assert ceiling_db == pytest.approx(-branch[by_role["woofer"]], abs=0.01)
    assert ceiling_db - full_band == pytest.approx(
        peak - max(branch.values()), abs=0.01
    )
    # The ruling, as a number: the declared caps used to pin this ceiling at
    # min(-8.0, -33.2) - peak. Mutation guard — restore that term and the
    # ceiling collapses to it, tens of decibels below what the box can drive.
    declared_cap_ceiling = min(
        -18.8 - branch[by_role["tweeter"]], -8.0 - branch[by_role["woofer"]]
    )
    assert ceiling_db > declared_cap_ceiling
    assert min(-8.0, -33.2) - peak == pytest.approx(-21.2, abs=0.01)
    assert ceiling_db > -21.2
    # The commissioning SPL stop is a SEPARATE bound and is untouched by any of
    # this — it is still a real number from the preset.
    assert 45.0 <= spl_ceiling <= 85.0


def test_the_measured_SPL_stop_still_rejects_a_target_above_the_profile_ceiling():
    """The runtime backstop this PR must not weaken.

    The volume ceiling got honest; the measured seat-SPL ceiling did not move.
    A band whose TOP exceeds ``max_commissioning_level_db_spl`` is still
    refused as ``seat_spl_target_rejected`` before a note is played, however
    much digital headroom the ledger now admits.
    """
    from jasper.active_speaker.seat_level_reference import (
        SeatLevelTarget,
        SeatLevelTargetError,
    )

    target = SeatLevelTarget(target_db_spl=90.0, tolerance_db=2.5)
    with pytest.raises(SeatLevelTargetError):
        target.validate(ceiling_db_spl=85.0)
    # And the 75-80 band this PR exists to make reachable is still accepted.
    SeatLevelTarget(target_db_spl=77.5, tolerance_db=2.5).validate(ceiling_db_spl=85.0)


def _stub_a_ramp_result(monkeypatch, tmp_path, result):
    """Everything between ``main()`` and the ramp, stubbed to a fixed outcome.

    The ramp's own behaviour is covered in
    ``tests/test_active_speaker_seat_level.py``; what is under test here is
    whether the CLI actually SHOWS what the ramp published.
    """
    stimulus = _stereo_wav(tmp_path / "check.wav", peak_int16=32767)
    cal = tmp_path / "umik2.txt"
    cal.write_text(CAL_WITH_SENS)

    async def _fake_ramp(**_kwargs):
        return result

    monkeypatch.setattr(seat_level, "run_seat_level_ramp", _fake_ramp)
    _stub_declarations(monkeypatch)
    monkeypatch.setattr(
        seat_level, "_derive_bounds", lambda stim, levels, declarations: (0.0, 80.0)
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
    return [str(stimulus), str(cal)]


def test_a_refusal_prints_the_window_it_stopped_in(tmp_path, monkeypatch, capsys):
    """The operator's own terminal carries the discriminator, not just stdout.

    jts3, 2026-08-23: two 75 dB SPL runs refused on a sample measuring 80.5 /
    80.9 dB SPL, and the line an operator read said only that -- with the prior
    settled median (64.6) beside it, and nothing at all about the window the
    stop abandoned. The two readings that separate "one tail sample crossed"
    from "the level rose and stayed" are the window's median and its max.
    """
    from jasper.active_speaker.seat_level_ramp import (
        REFUSE_SPL_CEILING_EXCEEDED,
        SeatLevelResult,
    )

    stimulus, cal = _stub_a_ramp_result(
        monkeypatch,
        tmp_path,
        SeatLevelResult(
            status="refused",
            reason=REFUSE_SPL_CEILING_EXCEEDED,
            detail=(
                "measured 80.5 dB SPL, above the profile's commissioning stop "
                "80.0 dB SPL (stopped at -12.50 dB against the 0.00 dB headroom "
                "ceiling, reading 64.6 dB SPL at -20.00 dB; the window it "
                "stopped in saw 20 samples spanning 74.0-80.5 dB SPL, median "
                "74.0, and stopped on the 80.5 dB SPL sample 0.950 s in)"
            ),
            restored=True,
            ramp={"ambient_remeasured": False},
        ),
    )

    code = seat_level.main(["--stimulus-wav", stimulus, "--calibration-file", cal])
    err = capsys.readouterr().err

    assert code == 1
    assert "refused (spl_ceiling_exceeded)" in err
    assert "the window it stopped in saw 20 samples" in err
    assert "median 74.0" in err
    assert "80.5 dB SPL sample 0.950 s in" in err
    assert "reading 64.6 dB SPL at -20.00 dB" in err

    # The detail above is a COPY of what the ramp writes, so it is pinned
    # against the ramp's own writer rather than left to go stale silently: this
    # is the window that produced it, phrased by the one function that phrases
    # it. `tests/test_active_speaker_seat_level.py` covers the ramp end putting
    # that clause on a real refusal.
    from jasper.active_speaker import seat_level_ramp as slr

    trace = slr._WindowTrace(
        samples=tuple((index * 0.05, 74.0) for index in range(19)) + ((0.95, 80.5),),
        seen=20,
        trip=(0.95, 80.5),
    )
    assert slr._window_phrase(trace.summary()) in err


def test_a_re_measured_room_floor_is_disclosed_on_the_line(
    tmp_path, monkeypatch, capsys
):
    """A floor the pass had to measure twice is stated, never applied invisibly.

    The rise gate reads ``observed - floor``, so which window supplied the floor
    decides which readings the pass trusted. Run 87 on jts3 measured 57.18 dB
    SPL of "room" and then published three negative rises against it.
    """
    from jasper.active_speaker.seat_level_ramp import SeatLevelResult

    stimulus, cal = _stub_a_ramp_result(
        monkeypatch,
        tmp_path,
        SeatLevelResult(
            status="converged",
            reference_volume_db=-13.69,
            measured_db_spl=72.63,
            restored=True,
            ramp={
                "ambient_db_spl": 57.18,
                "ambient_remeasured_db_spl": 49.7,
                "ambient_remeasured": True,
            },
        ),
    )

    code = seat_level.main(["--stimulus-wav", stimulus, "--calibration-file", cal])
    streams = capsys.readouterr()

    assert code == 0
    assert "converged: reference -13.69 dB measured 72.6 dB SPL" in streams.err
    # The sentence break, pinned because the nit survived two review rounds:
    # the phrase is appended to a detail that does not end in a period.
    assert "measured 72.6 dB SPL. A climb reading landed below" in streams.err
    assert "landed below the 57.2 dB SPL ambient window" in streams.err
    assert "re-measured in silence: 49.7 dB SPL" in streams.err
    assert "which is the floor every rise above was measured against" in streams.err
    # The same disclosure rides the answer, which is what a driver reads.
    answer = json.loads(streams.out)
    assert answer["reference_volume_db"] == -13.69
    assert answer["measured_db_spl"] == 72.63
    assert answer["restored"] is True
    assert "re-measured in silence: 49.7 dB SPL" in answer["detail"]
    assert answer["out"] == str(seat_level_reference_state_path())


def test_an_honest_ambient_floor_adds_nothing_to_the_line(
    tmp_path, monkeypatch, capsys
):
    """The control: a pass that needed no correction says nothing about one."""
    from jasper.active_speaker.seat_level_ramp import SeatLevelResult

    stimulus, cal = _stub_a_ramp_result(
        monkeypatch,
        tmp_path,
        SeatLevelResult(
            status="converged",
            reference_volume_db=-16.97,
            measured_db_spl=69.63,
            restored=True,
            ramp={
                "ambient_db_spl": 49.73,
                "ambient_remeasured_db_spl": None,
                "ambient_remeasured": False,
            },
        ),
    )

    code = seat_level.main(["--stimulus-wav", stimulus, "--calibration-file", cal])
    err = capsys.readouterr().err

    assert code == 0
    assert "ambient window" not in err
    assert "re-measured" not in err
    assert err.strip().endswith("69.6 dB SPL")


@pytest.mark.parametrize(
    "argv_extra, expected_level",
    [
        pytest.param([], "INFO", id="the_receipt_still_reaches_the_journal"),
        pytest.param(["--verbose"], "DEBUG", id="verbose_unlocks_the_sample_series"),
    ],
)
def test_verbose_lowers_the_log_floor_without_losing_the_receipt(
    tmp_path, monkeypatch, argv_extra, expected_level
):
    """``--verbose`` is the reader for the per-sample series.

    The series is a DEBUG line, so without a way to turn DEBUG on it is a write
    with no reader. And the floor without the flag stays INFO rather than
    ``_logging.configure_verbose_logging``'s WARNING, which is the level that
    would discard ``event=active_speaker.unsegmented_ceiling_bound``.
    """
    import logging

    from jasper.active_speaker.seat_level_ramp import SeatLevelResult

    seen: dict = {}

    def _basic_config(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(logging, "basicConfig", _basic_config)
    stimulus, cal = _stub_a_ramp_result(
        monkeypatch,
        tmp_path,
        SeatLevelResult(
            status="converged",
            reference_volume_db=-16.97,
            measured_db_spl=69.63,
            restored=True,
        ),
    )

    seat_level.main(
        ["--stimulus-wav", stimulus, "--calibration-file", cal, *argv_extra]
    )
    assert logging.getLevelName(seen["level"]) == expected_level


def test_a_ceiling_above_digital_zero_is_still_clamped_to_the_0_dB_rail():
    """``volume_limit 0.0`` is untouched, and the ramp is where it is enforced.

    An honest ceiling can now land ABOVE 0 dB — the JTS3-shaped fixture's does.
    The ramp clamps its ceiling to ``HARD_CEILING_DBFS``, so the derived number
    widens how far the ramp may climb but can never command positive main
    volume. Nothing in this PR touches that clamp; this pins that it still holds
    against the new, larger input.
    """
    from jasper.active_speaker.seat_level_ramp import seat_level_ceiling_db
    from jasper.audio_measurement.ramp import HARD_CEILING_DBFS

    assert HARD_CEILING_DBFS == 0.0
    assert seat_level_ceiling_db(12.68) == HARD_CEILING_DBFS
