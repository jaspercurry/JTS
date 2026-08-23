# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-driver-trim`` — the headless measured auto-trim verb.

The verb is wiring, so these pin the wiring: the excitation-ledger normalize
that makes two captures taken at different protected drive levels comparable,
the refusals an operator can actually reach, and one end-to-end pass over REAL
synthesized captures whose level ratio is known in advance.

The chain from levels to trims, the record, and the re-key refusal live in
``test_active_speaker_driver_base_trim.py``.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.signal import firwin, fftconvolve

from jasper.active_speaker import driver_acoustics as da
from jasper.active_speaker import driver_base_trim as dbt
from jasper.audio_measurement import sweep as sweep_mod
from jasper.audio_measurement.calibration import (
    MIC_CALIBRATION_UNAVAILABLE_DETAIL,
    REFUSE_MIC_CALIBRATION_UNAVAILABLE,
)
from jasper.cli import driver_trim

SR = 48000
FC_HZ = 2000.0

CAL_TEXT = (
    '"Sens Factor =-12.07dB, AGain =18dB, SERNO: 8108494"\n'
    + "\n".join(f"{freq}\t0.0" for freq in (10.0, 100.0, 1000.0, 10000.0, 20000.0))
    + "\n"
)


def _preset(way_count=2, regions=((("woofer", "tweeter"), FC_HZ),)):
    """The duck preset the analysis surfaces read: way count + regions."""
    return SimpleNamespace(
        way_count=way_count,
        crossover_regions=tuple(
            SimpleNamespace(lower_driver=pair[0], upper_driver=pair[1], fc_hz=fc)
            for pair, fc in regions
        ),
    )


def _excitation(effective_peak_dbfs: float, commissioning_gain_db: float) -> dict:
    return {
        "schema_version": 1,
        "scope": "sweep_plus_role_varying_commission_gain",
        "sweep_peak_dbfs": effective_peak_dbfs - commissioning_gain_db,
        "commissioning_gain_db": commissioning_gain_db,
        "effective_peak_dbfs": effective_peak_dbfs,
    }


def _write_manifest(captures_dir: Path, captures: list[dict]) -> Path:
    captures_dir.mkdir(parents=True, exist_ok=True)
    path = captures_dir / driver_trim.CAPTURES_MANIFEST_NAME
    path.write_text(
        json.dumps({
            "artifact_schema_version": 1,
            "kind": driver_trim.CAPTURES_MANIFEST_KIND,
            "captures": captures,
        })
    )
    return path


# ---------- the excitation-ledger normalize ---------------------------------


class _FakeResult:
    """What ``analyze_driver_capture`` hands back, reduced to what is read."""

    def __init__(self, level_db, *, verdict=da.VERDICT_PRESENT, usable=True):
        self.verdict = verdict
        self.overlap_levels = (
            {"fc_hz": FC_HZ, "level_db": level_db, "usable": usable},
        )


def _stub_analysis(monkeypatch, levels_by_role):
    def fake(wav, sweep_meta, **kwargs):
        return levels_by_role[Path(wav).stem]

    monkeypatch.setattr(driver_trim_analysis(), "analyze_driver_capture", fake)


def driver_trim_analysis():
    return da


def _two_captures(tmp_path: Path, *, woofer_gain_db, tweeter_gain_db) -> Path:
    captures_dir = tmp_path / "captures"
    captures_dir.mkdir(parents=True, exist_ok=True)
    for role in ("woofer", "tweeter"):
        (captures_dir / f"{role}.wav").write_bytes(b"RIFF....WAVE")
    _write_manifest(captures_dir, [
        {
            "speaker_group_id": "mono", "role": "woofer", "wav": "woofer.wav",
            "sweep_meta": {"sample_rate": SR},
            "excitation": _excitation(-52.0, woofer_gain_db),
        },
        {
            "speaker_group_id": "mono", "role": "tweeter", "wav": "tweeter.wav",
            "sweep_meta": {"sample_rate": SR},
            "excitation": _excitation(-52.0 + tweeter_gain_db - woofer_gain_db,
                                      tweeter_gain_db),
        },
    ])
    return captures_dir


def test_two_captures_driven_at_different_levels_are_normalized_before_comparison(
    tmp_path: Path, monkeypatch
):
    """The tweeter was driven 12 dB quieter than the woofer AND measured 12 dB
    quieter, so the two drivers are actually level: without the ledger
    normalize this reads as a 12 dB gap and trims a driver that needs nothing."""
    captures_dir = _two_captures(
        tmp_path, woofer_gain_db=-40.0, tweeter_gain_db=-52.0
    )
    _stub_analysis(monkeypatch, {
        "woofer": _FakeResult(-30.0),
        "tweeter": _FakeResult(-42.0),
    })

    levels = driver_trim._capture_levels(
        driver_trim._load_captures_manifest(captures_dir),
        _preset(),
        captures_dir,
        None,
    )

    assert levels == {
        "mono": {"woofer": {FC_HZ: 22.0}, "tweeter": {FC_HZ: 22.0}}
    }
    assert dbt.solve_base_trims(
        levels, ("woofer", "tweeter"), [("woofer", "tweeter", FC_HZ)]
    ) == {"woofer": 0.0, "tweeter": 0.0}


def test_a_real_level_gap_survives_the_normalize(tmp_path: Path, monkeypatch):
    captures_dir = _two_captures(
        tmp_path, woofer_gain_db=-40.0, tweeter_gain_db=-40.0
    )
    _stub_analysis(monkeypatch, {
        "woofer": _FakeResult(-50.0),
        "tweeter": _FakeResult(-38.0),
    })

    levels = driver_trim._capture_levels(
        driver_trim._load_captures_manifest(captures_dir),
        _preset(),
        captures_dir,
        None,
    )

    assert dbt.solve_base_trims(
        levels, ("woofer", "tweeter"), [("woofer", "tweeter", FC_HZ)]
    ) == {"woofer": 0.0, "tweeter": -12.0}


# ---------- refusals an operator can reach ----------------------------------


def test_a_missing_manifest_refuses_by_name(tmp_path: Path):
    with pytest.raises(driver_trim.TrimRefusal) as excinfo:
        driver_trim._load_captures_manifest(tmp_path / "nowhere")
    assert excinfo.value.reason == driver_trim.REFUSE_CAPTURES_MISSING


@pytest.mark.parametrize(
    "text, why",
    [
        pytest.param("{", "unparseable", id="not_json"),
        pytest.param(json.dumps({"kind": "something_else"}), "wrong kind", id="kind"),
        pytest.param(
            json.dumps({"kind": driver_trim.CAPTURES_MANIFEST_KIND, "captures": []}),
            "no captures at all",
            id="empty",
        ),
        pytest.param(
            json.dumps(
                {"kind": driver_trim.CAPTURES_MANIFEST_KIND, "captures": ["woofer"]}
            ),
            "a capture that is not an object",
            id="scalar_capture",
        ),
    ],
)
def test_a_malformed_manifest_refuses_by_name(tmp_path: Path, text, why):
    captures_dir = tmp_path / "captures"
    captures_dir.mkdir()
    (captures_dir / driver_trim.CAPTURES_MANIFEST_NAME).write_text(text)
    with pytest.raises(driver_trim.TrimRefusal) as excinfo:
        driver_trim._load_captures_manifest(captures_dir)
    assert excinfo.value.reason == driver_trim.REFUSE_CAPTURES_INVALID, why


def test_a_capture_with_no_auditable_ledger_refuses_rather_than_assuming_a_level(
    tmp_path: Path
):
    captures_dir = _two_captures(
        tmp_path, woofer_gain_db=-40.0, tweeter_gain_db=-40.0
    )
    manifest = json.loads(
        (captures_dir / driver_trim.CAPTURES_MANIFEST_NAME).read_text()
    )
    # The recomputed total no longer matches the declared effective peak.
    manifest["captures"][1]["excitation"]["effective_peak_dbfs"] = -3.0
    (captures_dir / driver_trim.CAPTURES_MANIFEST_NAME).write_text(
        json.dumps(manifest)
    )
    with pytest.raises(driver_trim.TrimRefusal) as excinfo:
        driver_trim._capture_levels(
            driver_trim._load_captures_manifest(captures_dir),
            _preset(),
            captures_dir,
            None,
        )
    assert excinfo.value.reason == driver_trim.REFUSE_EXCITATION_LEDGER_INVALID


def test_the_same_driver_captured_twice_refuses_rather_than_picking_one(
    tmp_path: Path
):
    captures_dir = _two_captures(
        tmp_path, woofer_gain_db=-40.0, tweeter_gain_db=-40.0
    )
    manifest = json.loads(
        (captures_dir / driver_trim.CAPTURES_MANIFEST_NAME).read_text()
    )
    manifest["captures"].append(dict(manifest["captures"][0]))
    (captures_dir / driver_trim.CAPTURES_MANIFEST_NAME).write_text(
        json.dumps(manifest)
    )
    with pytest.raises(driver_trim.TrimRefusal) as excinfo:
        driver_trim._capture_levels(
            driver_trim._load_captures_manifest(captures_dir),
            _preset(),
            captures_dir,
            None,
        )
    assert excinfo.value.reason == driver_trim.REFUSE_CAPTURES_INVALID


def test_a_capture_naming_an_undeclared_role_refuses(tmp_path: Path):
    captures_dir = _two_captures(
        tmp_path, woofer_gain_db=-40.0, tweeter_gain_db=-40.0
    )
    manifest = json.loads(
        (captures_dir / driver_trim.CAPTURES_MANIFEST_NAME).read_text()
    )
    manifest["captures"][1]["role"] = "supertweeter"
    (captures_dir / driver_trim.CAPTURES_MANIFEST_NAME).write_text(
        json.dumps(manifest)
    )
    with pytest.raises(driver_trim.TrimRefusal) as excinfo:
        driver_trim._capture_levels(
            driver_trim._load_captures_manifest(captures_dir),
            _preset(),
            captures_dir,
            None,
        )
    assert excinfo.value.reason == driver_trim.REFUSE_CAPTURES_INVALID


@pytest.mark.parametrize(
    "result, why",
    [
        pytest.param(
            _FakeResult(-30.0, verdict=da.VERDICT_SILENT),
            "a silent driver is not evidence of its level",
            id="silent",
        ),
        pytest.param(
            _FakeResult(-30.0, verdict=da.VERDICT_UNUSABLE_CAPTURE),
            "an unusable capture is not evidence",
            id="unusable",
        ),
        pytest.param(
            _FakeResult(-30.0, verdict=da.VERDICT_OUT_OF_BAND),
            "energy outside the declared band is a routing fault, not a level",
            id="out_of_band",
        ),
        pytest.param(
            _FakeResult(-30.0, usable=False),
            "the band itself was gated out",
            id="band_unusable",
        ),
    ],
)
def test_an_unusable_capture_refuses_instead_of_contributing_a_number(
    tmp_path: Path, monkeypatch, result, why
):
    captures_dir = _two_captures(
        tmp_path, woofer_gain_db=-40.0, tweeter_gain_db=-40.0
    )
    _stub_analysis(monkeypatch, {
        "woofer": _FakeResult(-50.0),
        "tweeter": result,
    })
    with pytest.raises(driver_trim.TrimRefusal) as excinfo:
        driver_trim._capture_levels(
            driver_trim._load_captures_manifest(captures_dir),
            _preset(),
            captures_dir,
            None,
        )
    assert excinfo.value.reason == driver_trim.REFUSE_CAPTURE_UNUSABLE, why


def test_no_calibration_refuses_before_anything_is_analysed(tmp_path: Path, capsys):
    captures_dir = _two_captures(
        tmp_path, woofer_gain_db=-40.0, tweeter_gain_db=-40.0
    )
    cal = tmp_path / "curve_only.txt"
    cal.write_text("10.0\t-6.6\n10.2\t-6.5\n")  # a curve, but no Sens Factor
    code = driver_trim.main([
        "--captures-dir", str(captures_dir), "--calibration-file", str(cal),
    ])
    assert code == 1
    assert REFUSE_MIC_CALIBRATION_UNAVAILABLE in capsys.readouterr().err


def test_the_mic_refusal_is_the_one_the_calibration_module_owns():
    """One slug and one sentence for 'this mic has no absolute reference', so
    the two verbs that ask it cannot drift into two wordings."""
    assert driver_trim.REFUSE_MIC_CALIBRATION_UNAVAILABLE == (
        REFUSE_MIC_CALIBRATION_UNAVAILABLE
    )
    assert driver_trim.MIC_CALIBRATION_UNAVAILABLE_DETAIL == (
        MIC_CALIBRATION_UNAVAILABLE_DETAIL
    )
    # Imported, not re-spelled: neither string is a literal in this verb, so
    # editing the owner's wording cannot leave a stale copy behind here.
    tree = ast.parse(Path(driver_trim.__file__).read_text(encoding="utf-8"))
    literals = {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert REFUSE_MIC_CALIBRATION_UNAVAILABLE not in literals
    assert MIC_CALIBRATION_UNAVAILABLE_DETAIL not in literals


def test_the_parser_demands_a_named_microphone(capsys):
    with pytest.raises(SystemExit):
        driver_trim.main(["--captures-dir", "/tmp/nowhere"])
    assert "--calibration-file or --mic-serial" in capsys.readouterr().err


# ---------- end to end, over real synthesized captures ----------------------


def _driver_capture(tmp_path: Path, name: str, reference, *, taps, gain: float):
    """A capture of one driver: the reference sweep through a band-pass, scaled."""
    captured = fftconvolve(reference, taps, mode="full")[: len(reference)] * gain
    path = tmp_path / name
    sweep_mod.write_sweep_wav(path, captured.astype(np.float32), SR)
    return path


def test_end_to_end_recovers_a_known_level_gap_and_banks_it(
    tmp_path: Path, monkeypatch
):
    """Two real captures whose only difference is a 12 dB scalar: the verb must
    recover 12 dB, normalize it up, and bank it against this declaration."""
    reference, meta = sweep_mod.synchronized_swept_sine(
        f1=da.DEFAULT_F1_HZ, f2=da.DEFAULT_F2_HZ, duration_approx_s=1.0,
        sample_rate=SR, amplitude_dbfs=da.DEFAULT_AMPLITUDE_DBFS,
    )
    # Each driver gets a filter that keeps its energy in its OWN declared band
    # (so the verdict is "present", not "out_of_band") while both stay flat
    # across the one-octave overlap band about Fc, [1414, 2828] Hz. In that
    # band the only difference between the two captures is the 12 dB scalar —
    # the number the trim must recover.
    low_pass = firwin(255, 5000.0 / (SR / 2))
    high_pass = firwin(255, 800.0 / (SR / 2), pass_zero=False)
    captures_dir = tmp_path / "captures"
    captures_dir.mkdir()
    _driver_capture(captures_dir, "woofer.wav", reference, taps=low_pass, gain=0.05)
    _driver_capture(
        captures_dir, "tweeter.wav", reference,
        taps=high_pass, gain=0.05 * 10 ** (12 / 20),
    )
    _write_manifest(captures_dir, [
        {
            "speaker_group_id": "mono", "role": role, "wav": f"{role}.wav",
            "sweep_meta": meta.to_dict(),
            "excitation": _excitation(-52.0, -40.0),
        }
        for role in ("woofer", "tweeter")
    ])

    cal = tmp_path / "umik2.txt"
    cal.write_text(CAL_TEXT)
    state = tmp_path / "driver_base_trim.json"
    preview = {"kind": "jts_active_speaker_crossover_preview", "drivers": {}}
    monkeypatch.setattr(
        driver_trim, "_resolve_declaration",
        lambda: (_preset(), preview, "c" * 64),
    )

    code = driver_trim.main([
        "--captures-dir", str(captures_dir),
        "--calibration-file", str(cal),
        "--state", str(state),
    ])

    assert code == 0
    record = dbt.load_base_trim(state_path=state)
    assert record is not None
    assert record["trims_db"]["woofer"] == 0.0
    assert record["trims_db"]["tweeter"] == pytest.approx(-12.0, abs=0.6)
    assert record["declaration_fingerprint"] == "c" * 64
    assert record["microphone"]["serial"] == "8108494"
    assert record["microphone"]["sens_factor_db"] == -12.07
    # The banked record survives its own reader, which is what makes the write
    # a publication rather than a silent no-op.
    trims, meta_out = dbt.banked_base_trims(
        "c" * 64, ("woofer", "tweeter"), state_path=state
    )
    assert meta_out["status"] == dbt.STATUS_APPLIED
    assert trims == record["trims_db"]


def test_a_speaker_with_no_declaration_refuses_rather_than_banking_an_unkeyed_trim(
    tmp_path: Path, monkeypatch, capsys
):
    captures_dir = _two_captures(
        tmp_path, woofer_gain_db=-40.0, tweeter_gain_db=-40.0
    )
    cal = tmp_path / "umik2.txt"
    cal.write_text(CAL_TEXT)

    def _no_declaration():
        raise driver_trim.TrimRefusal(
            driver_trim.REFUSE_DECLARATION_UNAVAILABLE, "no preview"
        )

    monkeypatch.setattr(driver_trim, "_resolve_declaration", _no_declaration)
    code = driver_trim.main([
        "--captures-dir", str(captures_dir), "--calibration-file", str(cal),
        "--json",
    ])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == driver_trim.REFUSE_DECLARATION_UNAVAILABLE
