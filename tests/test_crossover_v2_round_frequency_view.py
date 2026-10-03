# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import partial
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.bundles import open_bundle
from jasper.active_speaker.commissioning_evidence_store import CommissioningEvidenceStore, EVIDENCE_ROOT
from jasper.active_speaker.crossover_v2.capture_provenance import analysis_blocks, analysis_provenance
from jasper.active_speaker.crossover_v2.record_index import reopen_measurement_record
from jasper.active_speaker.crossover_v2.record_store import BankedRecordStore
from jasper.active_speaker.crossover_v2.room_selection import select_seat_takes
from jasper.active_speaker.crossover_v2.take_impulses import write_take_impulses
from jasper.active_speaker.crossover_v2.wired_stimulus import CapturedRecordStore, WiredStimulusCapture
from jasper.active_speaker.measurement_analysis import analyzed_measurements
from jasper.active_speaker.measurement_bass import bass_evidence
from jasper.audio_measurement.calibration import CalibrationCurve, CalibrationRecord
from jasper.audio_measurement.evidence_reasons import REASON_COVERAGE_SHORT, TAKE_CURVES_NOT_BANKED, EvidenceUnavailable
from jasper.active_speaker.measurement_programs import POSE_KIND_BEARING, POSE_KIND_SEAT, gate_exemption
from jasper.audio_measurement.household_mic import resolve_setup_calibration
from jasper.audio_measurement.program import ExcitationProgram, build_verify_program, render_program_pcm
from jasper.audio_measurement.program_analysis import MeasurementGeometry, analyze_program_capture
from jasper.audio_measurement.repeated_sweep import repeat_summed_program
from jasper.audio_measurement.wired_capture import WiredMicDevice, WiredRecording, decode_wav_to_mono
from tests.active_speaker_fixtures import mono_output_topology
from tests.run_manifest_fixture import manifest_set, write_bundle_manifest, write_manifest
from jasper.active_speaker.crossover_v2.contracts import POSITION_EVIDENCE_KIND
from jasper.active_speaker.measurement_archive import ArchivedMeasurement
from jasper.active_speaker.measurement_document import frequency_run_from_documents
from jasper.active_speaker.frequency_view import (
    FrequencyRun, FrequencyViewError, frequency_series, build_frequency_view as neutral_view,
)
from jasper.active_speaker.frequency_plot import render_frequency_view
from jasper.active_speaker import frequency_plot
from jasper.active_speaker.round_bank import bank_round
from jasper.active_speaker import measurement_archive
from tests.crossover_v2_banked_round import bank_executor_take
from jasper.cli._refusal import EXIT_REFUSED
from jasper.cli.round_views import build_parser, main as round_views_main, run_bookkeeping
from jasper.web import correction_measurements


def _run(run_id: str, *, offset: float = 0.0) -> FrequencyRun:
    """One saved run carrying one take's curve."""
    series = frequency_series(
        series_id="take", label="Take", kind="measurement", freqs_hz=[100.0, 1000.0, 10000.0],
        magnitude_db=[-25.0 + offset, -25.0 + offset, -27.0 + offset], reference_db=-25.0 + offset,
    )
    return FrequencyRun(run_id, "speaker_response", (series,), started_at=1000.0 + offset, state="applied")


@pytest.mark.parametrize("reference", [-24, None])
def test_saved_view_display_rules(reference):
    raw = {"freqs_hz": [50, 150, 200, 500, 1000, 20000],
           "magnitude_db": [-29, -25, -24, -23, -22, -21], "band_hz": [100, 10000]}
    metadata = {"reference_db": reference, "validity_floor_hz": 143, "trusted_floor_hz": 357,
                "excluded_bands_hz": [[400, 450], [440, 500]]}
    series = frequency_series(series_id="take", label="Take", kind="measurement",
                              reference_db=reference, **raw)
    assert series is not None
    saved = neutral_view(FrequencyRun("run", "speaker_response", (series,), metadata=metadata))
    display = saved["runs"][0]["series"][0]["display"]
    assert display == {
        "deviation_db": [None, -1, 0, 1, 2, None] if reference is not None else [None] * 6,
        "valid_band_hz": [143, 10000],
        "untrusted_intervals_hz": [[0, 357], [400, 500]],
    }
    assert saved["runs"][0]["series"][0]["magnitude_db"] == raw["magnitude_db"]
    json.dumps(saved, allow_nan=False)


def test_image_uses_shared_trust_markings_and_keeps_untrusted_data(tmp_path, monkeypatch):
    figure = pytest.importorskip("matplotlib.figure")
    series = frequency_series(
        series_id="take", label="Take", kind="measurement", reference_db=-24,
        freqs_hz=[50, 150, 200, 500, 1000], magnitude_db=[-29, -25, -24, -23, -22],
        validity_floor_hz=143, trusted_floor_hz=357, excluded_intervals_hz=[[440, 500], [900, 900]],
    )
    view = neutral_view(FrequencyRun("run", "speaker_response", (series,), metadata={
        "trusted_floor_hz": 200, "excluded_bands_hz": [[400, 450]],
    }))
    figures = []
    monkeypatch.setattr(figure.Figure, "savefig", lambda fig, *a, **kw: figures.append(fig))
    render_frequency_view(view, tmp_path / "response.png", band_hz=(50, 1000))
    ax = figures[0].axes[0]
    plotted = list(ax.lines[0].get_ydata())
    assert plotted[0] is None
    assert plotted[1:] == pytest.approx([-1, 0, 1, 2])
    spans = [patch.get_path().transformed(patch.get_patch_transform()).vertices[:, 0]
             for patch in ax.patches]
    assert [(min(xs), max(xs)) for xs in spans] == [(50, 357), (400, 500)]
    assert list(ax.lines[-1].get_xdata()) == [900, 900]


@pytest.mark.parametrize("offset,shape", [(-30, False), (-30, True), (12, True)])
def test_plot_power_reference_smoothing_and_statistics(monkeypatch, offset, shape):
    calls = []
    smooth = frequency_plot.smooth_fractional_octave

    def observed(freqs, values, *, fraction):
        calls.append(fraction)
        return smooth(freqs, values, fraction=fraction)

    monkeypatch.setattr(frequency_plot, "smooth_fractional_octave", observed)
    freqs = np.array([25, 35, 45, 55, 70, 100, 150, 300, 1000, 1010, 4000, 9000])
    raw = np.array([2] * 7 + [0, 0, 10 * np.log10(3), 0, 4]) if shape else np.zeros(12)
    plot = frequency_plot.prepare_plot_curve({"id": "test", "freqs_hz": freqs[::-1].tolist(), "magnitude_db": (raw + offset)[::-1].tolist()})
    assert calls == [6]
    assert plot["freqs_hz"] == freqs.tolist()
    assert plot["display"] == "normalized"
    reference = 10 * np.log10(1.5) if shape else 0
    expected = np.array([2] * 7 + [0, 10 * np.log10(2), 10 * np.log10(2), 0, 4]) - reference if shape else np.zeros(12)
    assert plot["deviation_db"] == pytest.approx(expected, abs=1e-10)
    ref = np.asarray(plot["deviation_db"])[(freqs >= 200) & (freqs <= 5000)]
    assert 10 * np.log10(np.mean(10 ** (ref / 10))) == pytest.approx(0, abs=1e-10)
    assert plot["rms_db"] == pytest.approx(np.sqrt(np.mean(expected[5:] ** 2)), abs=1e-10)
    assert plot["peak_to_peak_db"] == pytest.approx(4 if shape else 0, abs=1e-10)
    assert [band["mean_db"] for band in plot["band_means"]] == pytest.approx(expected[[0, 1, 2, 3, 4, 5, 6, 7]], abs=1e-10)


@pytest.mark.parametrize("normalize,mode,levels", [
    (False, "run_reference", [0, 3]), (True, "normalized", [0, 0]),
])
def test_frequency_reference_modes_preserve_or_remove_level_differences(tmp_path, monkeypatch, capsys, normalize, mode, levels):
    figure = pytest.importorskip("matplotlib.figure")
    freqs = [25, 35, 45, 55, 70, 100, 150, 300, 1000, 9000]
    curves = tuple(frequency_series(
        series_id=str(offset), label=str(offset), kind="measurement", reference_db=-20,
        freqs_hz=freqs[::-1], magnitude_db=[-20 + offset] * len(freqs), position={"azimuth_deg": 0},
    ) for offset in (0, 3))
    source = tmp_path / "source.json"
    source.write_text(json.dumps(neutral_view(FrequencyRun("trial", "speaker_response", curves))))
    figures = []
    monkeypatch.setattr(figure.Figure, "savefig", lambda fig, *a, **kw: figures.append(fig))
    assert round_views_main([
        "frequency", str(source), "--image", str(tmp_path / "plot.png"),
        *(["--normalize"] if normalize else []),
    ]) == 0
    answer = json.loads(capsys.readouterr().out)
    for series, line, level in zip(answer["series"], figures[0].axes[0].lines, levels):
        assert series["display"] == mode
        assert series["rms_db"] == pytest.approx(level, abs=1e-10)
        assert series["peak_to_peak_db"] == pytest.approx(0, abs=1e-10)
        assert [band["mean_db"] for band in series["band_means"]] == pytest.approx([level] * 8, abs=1e-10)
        assert list(line.get_xdata()) == freqs
        assert list(line.get_ydata()) == pytest.approx([level] * len(freqs), abs=1e-10)


@pytest.mark.parametrize("candidates,labels", [
    (("base", "candidate-123456"), ["applied", "candidat"]),
    (("base", "abcdefghijkl-one", "abcdefghijkl-two", "abcdefghijkl-one"),
     ["applied", "abcdefghijkl-o", "abcdefghijkl-t"]),
])
def test_image_groups_configurations_by_pose(tmp_path, monkeypatch, candidates, labels):
    figure = pytest.importorskip("matplotlib.figure")
    curves = tuple(frequency_series(
        series_id=f"{pose}:{index}", label=candidate, kind="measurement",
        freqs_hz=[20, 100, 1000, 10000, 20000], magnitude_db=[-10] * 5,
        position={"azimuth_deg": pose}, candidate_id=candidate, base=candidate == "base",
    ) for pose in (-20, 20) for index, candidate in enumerate(candidates))
    figures = []
    monkeypatch.setattr(figure.Figure, "savefig", lambda fig, *a, **kw: figures.append(fig))
    render_frequency_view(neutral_view(FrequencyRun("trial", "speaker_response", curves)), tmp_path / "plot.png", low_end=True)
    for index, ax in enumerate(figures[0].axes[:4]):
        assert ax.get_xscale() == "log"
        assert ax.get_xlim() == (20, 300 if index % 2 else 20000)
        assert ax.get_ylim() == (-20, 20)
        assert str((-20, 20)[index // 2]) in ax.get_title()
        assert [line.get_linestyle() for line in ax.lines[:2]] == ["--", "-"]
        assert [text.get_text() for text in ax.get_legend().get_texts()] == labels
        assert [tick for tick in ax.get_xticks()] == ([20, 50, 100, 200] if index % 2 else [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000])


@pytest.mark.parametrize("with_image", [False, True])
def test_frequency_without_matplotlib_writes_json(tmp_path, monkeypatch, capsys, with_image):
    source = tmp_path / "source.json"
    source.write_text(json.dumps(neutral_view(_run("test"))))
    output, png = tmp_path / "view.json", tmp_path / "view.png"
    monkeypatch.setitem(sys.modules, "matplotlib.figure", None)
    flags = ["--image", str(png), "--low-end"] if with_image else []
    assert round_views_main(["frequency", str(source), "--out", str(output), *flags]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["image"] is None
    assert answer.get("reason") == ("plots_extra_missing" if with_image else None)
    assert output.is_file() and not png.exists()
    assert answer["series"][0]["rms_db"] == pytest.approx(np.sqrt(4 / 3))


def test_frequency_view_adds_optional_run_b_without_changing_run_a():
    view = neutral_view(_run("aaa"), _run("bbb", offset=1.0))

    assert [(run["slot"], run["id"]) for run in view["runs"]] == [
        ("a", "aaa"), ("b", "bbb"),
    ]
    assert view["runs"][0]["series"][0]["magnitude_db"] == [
        -25.0, -25.0, -27.0,
    ]


def test_measurement_page_uses_the_canonical_shell_and_static_module():
    page = correction_measurements.render_page("jts.local", "csrf-token").decode()

    assert page.startswith("<!doctype html>")
    assert "/assets/correction/measurements.css?v=" in page
    assert "/assets/correction/js/measurements.js" in page
    assert 'id="measurement-run-a"' in page
    assert 'id="measurement-run-b"' in page
    assert 'id="measurement-chart"' in page


def test_web_data_uses_the_same_frequency_view_contract(tmp_path, monkeypatch):
    entries = tuple(
        ArchivedMeasurement(run_id, tmp_path / run_id, started_at, "applied")
        for run_id, started_at in (("aaa", 1.0), ("bbb", 2.0))
    )
    monkeypatch.setattr(
        correction_measurements, "list_measurements", lambda _root: entries,
    )
    monkeypatch.setattr(
        correction_measurements,
        "load_measurement",
        lambda entry: _run(entry.id),
    )

    data = correction_measurements.build_data(
        sessions_dir=tmp_path,
        campaign_root=tmp_path / "campaigns",
        run_a_id="aaa",
        run_b_id="bbb",
    )

    assert data["catalog_schema"] == "jts_frequency_catalog/1"
    assert [entry["id"] for entry in data["catalog"]] == ["bbb", "aaa"]
    assert data["selected"] == {"a": "aaa", "b": "bbb", "b_source": None}
    assert [run["id"] for run in data["view"]["runs"]] == ["aaa", "bbb"]


def _bank_one_round(root: Path, session_id: str, sets: dict | None = None, banked_at: str | None = None) -> Path:
    """One round as ``bank_round`` banks it: each set's takes, ``(take_id,
    bearing)``, under a finalized run manifest (a set named ``base…`` is base);
    with no ``sets``, one take and no manifest. ``banked_at`` dates the bank."""

    bundle = root / "sessions" / session_id
    positions = (
        bundle / EVIDENCE_ROOT / "artifacts/crossover_v2" / session_id / "positions"
    )
    positions.mkdir(parents=True)
    groups = []
    for set_id, takes in (sets or {"": [("t1", 0)]}).items():
        records = []
        for take_id, bearing in takes:
            record = {
                "kind": POSITION_EVIDENCE_KIND,
                "run_id": session_id,
                "take_id": take_id,
                "phase": "measure",
                "position_deg": bearing,
                "vertical_deg": 0,
                "pose_kind": "bearing",
                "curves": [{
                    "role": "summed",
                    "window": "ungated",
                    "reference_db": 0.0,
                    "freqs_hz": [100.0, 1000.0, 10000.0],
                    "magnitude_db": [-1.0, 0.0, 1.0],
                }],
            }
            path = positions / f"{take_id}.json"
            path.write_text(json.dumps(record))
            records.append((str(path.relative_to(bundle)), record))
        groups.append({**manifest_set(records, set_id=set_id), "base": set_id.startswith("base")})
    (bundle / "info.json").write_text(json.dumps({
        "session_id": session_id, "started_at": 1000.0, "state": "applied",
    }))
    if sets:
        write_bundle_manifest(bundle, groups=groups)
    campaign_root = root / "campaigns"
    absent = root / "absent.json"
    bank_round(
        bundle,
        campaign_root=campaign_root,
        state_path=absent,
        design_draft_path=absent,
        applied_profile_path=absent,
        repeat_floor_path=absent,
        declared_geometry_path=absent,
    )
    if banked_at is not None:
        provenance = campaign_root / session_id / "provenance.json"
        provenance.write_text(json.dumps({**json.loads(provenance.read_text()), "banked_at_utc": banked_at}))
    return campaign_root


def test_web_data_offers_a_banked_round_and_graphs_its_curves(tmp_path):
    campaign_root = _bank_one_round(tmp_path, "sess-1")

    data = correction_measurements.build_data(
        sessions_dir=tmp_path / "no-sessions", campaign_root=campaign_root,
    )

    [entry] = data["catalog"]
    assert (entry["origin"], entry["name"]) == ("banked", "sess-1")
    assert data["selected"]["a"] == entry["id"] != "sess-1"
    [run] = data["view"]["runs"]
    assert [series["magnitude_db"] for series in run["series"]] == [[-1.0, 0.0, 1.0]]


def test_web_data_lists_a_banked_rounds_live_bundle_once(tmp_path):
    """The bank hard-links its live bundle: the round lists, its live twin does not; a bundle no bank holds still does."""
    _bank_one_round(tmp_path, "banked")
    campaign_root = _bank_one_round(tmp_path, "unbanked")
    shutil.rmtree(campaign_root / "unbanked")

    data = correction_measurements.build_data(sessions_dir=tmp_path / "sessions", campaign_root=campaign_root)

    assert sorted((entry["origin"], entry["name"]) for entry in data["catalog"]) == [
        ("banked", "banked"), ("live", "unbanked")]


# Run A is round r. Its base takes find their comparands in o (0°) and e (30°); "later" is banked after r.
_EARLIER_COMPARANDS = {"o": {"base": [("o0", 0)]}, "e": {"base": [("e30", 30)]},
                       "r": {"base": [("r0", 0), ("r30", 30)], "cand": [("c0", 0)]},
                       "later": {"base": [("l0", 0), ("l30", 30)]}}


@pytest.mark.parametrize("rounds,run_b_id,expected", [
    pytest.param(_EARLIER_COMPARANDS, None, ("round:e", "comparand"), id="newest-earlier-comparand"),
    # c0's comparand is r's own base, drawn in A; no earlier round holds b0's place.
    pytest.param({"e": {"base": [("e30", 30)]}, "r": {"base": [("b0", 0)], "cand": [("c0", 0)]}},
                 None, (None, None), id="own-base"),
    pytest.param({"e": {"base": [("e0", 0)]}, "r": None}, None, (None, None), id="no-manifest"),
    pytest.param({"r": {"base": [("r0", 0)]}, "later": {"base": [("l0", 0)]}}, None, (None, None),
                 id="no-earlier-round"),
    pytest.param(_EARLIER_COMPARANDS, "round:o", ("round:o", None), id="chosen-run"),
    pytest.param(_EARLIER_COMPARANDS, correction_measurements.NO_RUN_B, (None, None), id="chosen-none"),
])
def test_run_b_defaults_to_the_round_holding_run_a_comparands(tmp_path, rounds, run_b_id, expected):
    """#5737 P6, the owner's answer A on #5925: with no run B chosen, B is the
    newest round banked before A that holds the comparand (ADR-0391) of any of
    A's takes, and says so; a chosen B, "None" included, wins."""
    for index, (name, sets) in enumerate(rounds.items()):
        campaign_root = _bank_one_round(tmp_path, name, sets, banked_at=f"2026-09-{20 + index:02d}T12:00:00Z")

    data = correction_measurements.build_data(
        sessions_dir=tmp_path / "no-sessions", campaign_root=campaign_root, run_a_id="round:r", run_b_id=run_b_id,
    )

    run_b, b_source = expected
    assert (data["selected"]["b"], data["selected"]["b_source"]) == (run_b, b_source)
    assert [run["id"] for run in data["view"]["runs"]] == ["round:r"] + ([run_b] if run_b else [])


def test_web_data_returns_an_empty_view_when_no_runs_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(correction_measurements, "list_measurements", lambda _root: ())
    empty = correction_measurements.build_data(
        sessions_dir=tmp_path, campaign_root=tmp_path / "campaigns",
        run_a_id="missing",
    )
    assert empty["view"] is None


def test_web_data_rejects_a_run_outside_the_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(
        correction_measurements,
        "list_measurements",
        lambda _root: (ArchivedMeasurement("known", tmp_path / "known"),),
    )

    with pytest.raises(
        correction_measurements.MeasurementViewRequestError,
        match="measurement not found",
    ):
        correction_measurements.build_data(
            sessions_dir=tmp_path, campaign_root=tmp_path / "campaigns",
            run_a_id="../outside",
        )


def test_neutral_adapter_reads_measurement_and_analysis_documents():
    run = frequency_run_from_documents(
        run_id="saved",
        documents=({
            "take_id": "axis",
            "position_deg": 0,
            "phase": "measure",
            "curves": [{
                "role": "woofer",
                "freqs_hz": [100.0, 1000.0],
                "magnitude_db": [-30.0, -20.0],
            }],
            "analysis": {
                "summed_response": {
                    "freqs_hz": [100.0, 1000.0],
                    "magnitude_db": [-28.0, -21.0],
                },
            },
        },),
    )

    assert [series.kind for series in run.series] == ["measurement", "analysis"]
    assert [series.visible_by_default for series in run.series] == [True, False]
    assert run.metadata["angles_deg"] == [0]


def test_neutral_adapter_uses_one_reference_for_the_whole_direct_run():
    run = frequency_run_from_documents(
        run_id="saved",
        documents=(
            {
                "take_id": "axis",
                "position_deg": 0,
                "curves": [{
                    "role": "woofer",
                    "freqs_hz": [300.0, 1000.0],
                    "magnitude_db": [-20.0, -20.0],
                }],
            },
            {
                "take_id": "off-axis",
                "position_deg": 14,
                "curves": [{
                    "role": "tweeter",
                    "freqs_hz": [300.0, 1000.0],
                    "magnitude_db": [-26.0, -26.0],
                }],
            },
        ),
    )

    assert len({series.reference_db for series in run.series}) == 1
    assert run.series[0].magnitude_db[0] - run.series[0].reference_db == pytest.approx(0.0)
    assert run.series[1].magnitude_db[0] - run.series[1].reference_db == pytest.approx(-6.0)


def test_a_series_names_its_documents_azimuth_and_the_shared_reference_anchors_on_the_axis():
    run = frequency_run_from_documents(run_id="saved", documents=tuple({
        "take_id": f"at-{azimuth}", "position_deg": azimuth,
        "curves": [{"role": "summed", "freqs_hz": [300.0, 1000.0], "magnitude_db": [level, level]}],
    } for azimuth, level in ((20, -26.0), (0, -20.0))))

    assert [series.details["position"]["azimuth_deg"] for series in run.series] == [20, 0]
    assert run.series[1].magnitude_db[0] - run.series[1].reference_db == pytest.approx(0.0)


def test_neutral_adapter_never_exposes_bins_outside_the_stored_valid_band():
    run = frequency_run_from_documents(
        run_id="saved",
        documents=({
            "curves": [{
                "role": "summed",
                "band_hz": [500.0, 2000.0],
                "freqs_hz": [100.0, 1000.0, 10000.0],
                "magnitude_db": [-60.0, -20.0, -70.0],
            }],
        },),
    )

    assert run.series[0].freqs_hz == (1000.0,)
    assert run.series[0].magnitude_db == (-20.0,)


def test_neutral_adapter_requires_an_honest_display_reference():
    with pytest.raises(FrequencyViewError, match="no stored reference"):
        frequency_run_from_documents(
            run_id="saved",
            documents=({
                "freqs_hz": [5000.0, 10000.0],
                "magnitude_db": [-20.0, -21.0],
            },),
        )


@pytest.mark.parametrize("legacy", [False, True])
def test_frequency_cli_reads_capture_prediction_evidence(
    tmp_path: Path, legacy: bool,
) -> None:
    basis = {
        "capture_id": "basis-take",
        "candidate_id": "basis-candidate",
        "graph_fingerprint": "basis-graph",
        "record_path": "/captures/basis.json",
    }
    measured = {
        "capture_id": "measured-take",
        "candidate_id": "target-candidate",
        "graph_fingerprint": "measured-graph",
        "record_path": "/captures/measured.json",
    }
    comparison = {
        "freqs_hz": [500.0, 1000.0, 2000.0],
        "predicted_db": [-18.0, -17.0, -16.0],
        "measured_db": [-23.0, -22.0, -21.0],
        "delta_db": [0.0, 0.0, 0.0],
        "compared_band_hz": [500.0, 2000.0],
        "level_offset_db": 5.0,
        "take_path": "/captures/basis.json",
    }
    document = {
        "schema_version": 1,
        "kind": "jts_capture_prediction",
        "summary": {
            "basis": basis,
            "candidate_id": "target-candidate",
            "window": {
                "window_ms": 7.0,
                "validity_floor_hz": 286.0,
                "trusted_floor_hz": 572.0,
            },
            "limits": "Forecast assumes unchanged setup.",
        },
        "prediction": {
            "freqs_hz": [500.0, 1000.0, 2000.0],
            "predicted_db": [-18.0, -17.0, -16.0],
            "sum_band_hz": [500.0, 2000.0],
            "take_path": "/captures/basis.json",
        },
        "reconstruction": {
            **comparison,
            "predicted_db": [-18.0, -17.0, -16.0],
            "measured_db": [-19.0, -18.0, -17.0],
            "level_offset_db": 1.0,
        },
        "limitations": ["No score authorizes playback."],
    }
    if legacy:
        document["predicted_minus_measured"] = comparison
        document["summary"].update(
            measured=measured, comparison_kind="changed_candidate",
            predicted_minus_measured=comparison, comparison_context={},
            forecast_binding={"status": "matched"},
        )
    source = tmp_path / f"prediction-{legacy}.json"
    output = tmp_path / f"frequency-{legacy}.json"
    source.write_text(json.dumps(document))

    assert round_views_main([
        "frequency", str(source), "--out", str(output),
    ]) == 0
    [run] = json.loads(output.read_text())["runs"]

    assert [series["id"] for series in run["series"]] == [
        "prediction:predicted", "reconstruction:predicted",
        "reconstruction:measured", "reconstruction:difference",
    ]
    responses = [series for series in run["series"] if series["role"] != "difference"]
    assert len({series["reference_db"] for series in responses}) == 1
    reconstruction = {series["id"]: series for series in run["series"]}
    predicted_id = "prediction:predicted"
    forecast = reconstruction[predicted_id]
    assert forecast["candidate_id"] == "target-candidate"
    assert forecast["basis_capture_id"] == "basis-take"
    assert forecast["basis_graph_fingerprint"] == "basis-graph"
    assert "graph_fingerprint" not in forecast
    assert [series["id"] for series in run["series"] if series["visible_by_default"]] == [predicted_id]
    assert reconstruction["reconstruction:predicted"]["magnitude_db"][0] - reconstruction["reconstruction:measured"]["magnitude_db"][0] == 1.0
    assert reconstruction["reconstruction:difference"]["reference_db"] == 0.0
    assert reconstruction["reconstruction:difference"]["level_offset_db"] == 1.0
    assert reconstruction["reconstruction:difference"]["display"]["deviation_db"] == [0.0, 0.0, 0.0]
    assert run["metadata"]["summary"]["basis"] == basis
    assert run["metadata"]["summary"]["candidate_id"] == "target-candidate"
    assert run["metadata"]["summary"]["window"]["window_ms"] == 7.0


def test_legacy_capture_prediction_exposes_no_invented_response_curves():
    run = frequency_run_from_documents(run_id="legacy", documents=({
        "kind": "jts_capture_prediction",
        "summary": {"limits": "Comparison response arrays were not retained."},
        "prediction": {
            "freqs_hz": [500.0, 1000.0],
            "predicted_db": [-20.0, -19.0],
            "sum_band_hz": [500.0, 1000.0],
        },
        "reconstruction": {
            "freqs_hz": [500.0, 1000.0],
            "delta_db": [0.5, -0.5],
            "compared_band_hz": [500.0, 1000.0],
            "level_offset_db": 1.0,
        },
    },))

    assert [series.id for series in run.series] == [
        "prediction:predicted", "reconstruction:difference",
    ]


@pytest.mark.parametrize("banked, listed", [("positions/take_a01.json", True), ("cloud_verify.json", False)])
def test_the_archive_lists_a_bundle_only_when_it_banked_a_take(tmp_path, banked, listed):
    sessions = tmp_path / "sessions"
    info = open_bundle(mono_output_topology(mode="active_2_way"), calibration_id="", sessions_dir=sessions)
    path = Path(info["bundle_dir"]) / EVIDENCE_ROOT / "artifacts/crossover_v2/cap1" / banked
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"kind": POSITION_EVIDENCE_KIND, "take_id": "take_a01", "phase": "lateral"}))

    assert [run.id for run in measurement_archive.list_measurements(sessions)] == ([info["session_id"]] if listed else [])


def _archive_serves(monkeypatch, *documents: dict) -> None:
    monkeypatch.setattr(measurement_archive, "measurement_documents",
                        lambda _bundle: [(None, document) for document in documents])


def _applied(calibration_id: str, *, applied: bool = True) -> dict:
    return {"applied": applied, "calibration_id": calibration_id, "curve_fingerprint": "curve" if applied else None}


@pytest.mark.parametrize("calibrations, expected", [
    ([_applied("mic-a")], "mic-a"),
    ([_applied("mic-b"), _applied("mic-a"), _applied("mic-a")], "mic-a, mic-b"),
    ([None, _applied("mic-a")], "mic-a"),
    ([_applied("mic-a", applied=False)], None),
    ([None], None),
])
def test_an_archive_run_names_the_mic_calibration_its_takes_applied(tmp_path, monkeypatch, calibrations, expected):
    (tmp_path / "info.json").write_text(json.dumps({"fingerprints": {"mic": {"calibration_id": "opened-with"}}}))
    _archive_serves(monkeypatch, *({"take_id": f"take_{index}", **({"capture_calibration": calibration} if calibration else {})}
                                   for index, calibration in enumerate(calibrations)))

    run = measurement_archive.load_measurement(ArchivedMeasurement("saved", tmp_path, 1.0, "applied"))

    assert run.metadata["mic_calibration_id"] == expected


@pytest.mark.parametrize("curves", [[], [{"role": "summed", "freqs_hz": [100.0, 1000.0], "magnitude_db": [-20.0, -21.0]}]])
def test_an_archive_run_whose_takes_banked_no_curves_says_so(tmp_path, monkeypatch, curves):
    monkeypatch.setattr(measurement_archive, "measurement_documents",
                        lambda _bundle: [(None, {"take_id": "t", "position_deg": 0, "curves": curves})])

    run = measurement_archive.load_measurement(ArchivedMeasurement("saved", tmp_path))

    assert (run.measurement_family, run.metadata.get("curves")) == (
        "speaker_response", None if curves else {"status": "unavailable", "reason": TAKE_CURVES_NOT_BANKED})


def test_mixed_candidate_archive_keeps_exact_takes_and_played_graphs(tmp_path, monkeypatch):
    docs = [{"take_id": take, "candidate_id": candidate, "graph_fingerprint": "entry",
             "provenance": {"graph": {"fingerprint": graph}}, "position_deg": 0,
             "curves": [{"role": "summed", "freqs_hz": [500, 1000, 2000],
                         "magnitude_db": [-20, -20, -21], "reference_db": -20}]}
            for take, candidate, graph in (("a", "candidate-a", "played-a"), ("b", "candidate-b", "played-b"))]
    _archive_serves(monkeypatch, *docs)
    run = measurement_archive.load_measurement(ArchivedMeasurement("saved", tmp_path))
    assert len(run.series) == 2
    assert {r.details["take_id"] for r in run.series} == {"a", "b"}
    assert {r.details["graph_fingerprint"] for r in run.series} == {"played-a", "played-b"}

@pytest.fixture
def summed_capture_bundle(tmp_path, request):
    info = open_bundle(
        mono_output_topology(mode="active_2_way"), calibration_id="",
        sessions_dir=tmp_path / "sessions",
    )
    bundle = Path(info["bundle_dir"])
    evidence = CommissioningEvidenceStore.open(bundle, expected_session_id=info["session_id"])
    calibration_root = tmp_path / "calibrations"
    calibration = CalibrationRecord(
        "recorded-mic", "minidsp", "minidsp_umik2", "Recorded microphone", "",
        "", "", "a" * 64, None, "0deg", "correction", 0, 2,
        CalibrationCurve([20, 20000], [2, 2]),
    )
    calibration_path = calibration_root / "minidsp" / "minidsp_umik2" / "recorded-mic.json"
    calibration_path.parent.mkdir(parents=True)
    calibration_path.write_text(json.dumps(calibration.to_dict()))
    program = build_verify_program(
        2500, sweep_band_hz=(20, getattr(request, "param", 20000)), gain_db=-14, downstream_gain_db=-20,
        sweep_s=1.5, leading_pilot_gains_db=(-24, -14),
    )
    pcm = render_program_pcm(program)[:, 0]
    signal = np.concatenate([np.zeros(800), pcm * 0.4 * 10 ** (-20 / 20), np.zeros(5000)])
    signal += np.random.default_rng(8).normal(0, 1e-8, signal.size)
    raw = np.column_stack([signal, np.zeros(signal.size)])

    def host_analysis(answer, record, *, exempt=POSE_KIND_SEAT, keep_impulses=False):
        """The analysis the capture host banks on a take (ADR-0383), read ungated as a seat take
        is, or through the gate with no ``exempt`` reason (ADR-0400); with ``keep_impulses``, the
        impulses the host keeps beside it (ADR-0354)."""
        if answer.program is None:
            return {}
        played = ExcitationProgram.from_dict(answer.program)
        calibration = resolve_setup_calibration(answer.setup, device=answer.device, root=calibration_root)
        curve = calibration.curve if calibration is not None else None
        geometry = MeasurementGeometry(gate_exempt_reason=exempt)
        samples, rate = decode_wav_to_mono(answer.wav)
        analysis = analyze_program_capture(played, samples, rate, calibration=curve,
                                           geometry=geometry, capture_report=answer.capture_integrity)
        analysis = replace(analysis, bass=bass_evidence(played, analysis, samples, curve))
        kept = {"impulses": write_take_impulses(bundle, record["take_id"], analysis, recording=None)} if keep_impulses else {}
        return {**analysis_provenance(played, analysis, calibration, curve, geometry), **analysis_blocks(analysis, played, None),
                **kept}

    async def bank(take_id, *, setup=None, scope="candidate", candidate="baseline-fp", retain_program=True, wav_hash=None, capture_gap_frames=0, capture_gain_db=0.0, analyzed=True, exempt=POSE_KIND_SEAT, keep_impulses=False, **fields):
        anchor = 800 + program.segment("sweep_verify").start_sample
        samples = np.delete(raw, np.s_[anchor - capture_gap_frames:anchor], axis=0)
        samples *= 10 ** (capture_gain_db / 20)
        recording = WiredRecording(
            ((samples * (2 ** 31 - 1)).astype("<i4").tobytes(),), len(samples),
            0, 0, False, program.sample_rate_hz, 2,
        )

        class Recorder:
            def start(self):
                pass

            def finish(self, **kwargs):
                return recording

            def abort(self):
                pass

        async def play():
            pass

        configured = WiredStimulusCapture(
            WiredMicDevice("UMIK2", 2, "2752:002b", "minidsp_umik2", "miniDSP UMIK-2"),
            bundle, recorder_factory=lambda *_: Recorder(), setup_reference=lambda: setup,
        )
        records = CapturedRecordStore(BankedRecordStore(evidence, "capture"), configured,
                                      enrich=partial(host_analysis, exempt=exempt, keep_impulses=keep_impulses) if analyzed else None)
        await configured.around(play, program=program)
        answer = configured.take_answer()
        if not retain_program:
            answer = replace(answer, program=None)
        if wav_hash is not None:
            answer = replace(answer, wav_sha256=wav_hash)
        return await records.bank_answer({
            "kind": "candidate" if candidate else "baseline", "take_id": take_id,
            "measurement_status": "captured", "incident": "", "phase": "measurement",
            "graph_scope": scope, "candidate_id": candidate, "graph_fingerprint": "a" * 16,
            "level_db": -20, "stimulus_dbfs": -14, "position_deg": 0, "vertical_deg": 0, "pose_kind": "bearing", **fields,
        }, answer)

    return bundle, calibration_root, program, bank


def test_frequency_reads_recorded_program_and_calibration_without_changing_level(
    summed_capture_bundle, tmp_path, capsys,
):
    bundle, _, program, bank = summed_capture_bundle
    first = asyncio.run(bank("baseline"))
    second = asyncio.run(bank("bass", scope="candidate", candidate="bass-6db", setup={
        "calibration": {"mode": "stored", "calibration_id": "recorded-mic", "model": "minidsp_umik2"},
    }))
    write_manifest(bundle, program="bass", groups=[
        {"set_id": set_id, "base": set_id == "base", "capture_basis": {},
         "takes": [{"take_id": take_id, "artifacts": {"record_id": path}}]}
        for set_id, take_id, path in (("base", "baseline", first), ("trial", "bass", second))
    ])
    before = {p: p.read_bytes() for p in bundle.rglob("*") if p.is_file()}
    record = json.loads((bundle / EVIDENCE_ROOT / "artifacts" / first).read_text())
    assert ExcitationProgram.from_dict(record["program"]).stimulus_id == program.stimulus_id
    destination = tmp_path / "frequency.json"
    assert round_views_main(["frequency", str(bundle), "--out", str(destination)]) == 0
    view = json.loads(destination.read_text())
    baseline, bass = view["runs"][0]["series"]
    assert view["runs"][0]["metadata"]["position_count"] == 1
    assert view["runs"][0]["metadata"]["take_count"] == 2
    assert baseline["candidate_id"] == "baseline-fp"
    assert bass["candidate_id"] == "bass-6db"
    assert [baseline["base"], bass["base"]] == [True, False]
    assert [s["graph_scope"] for s in (baseline, bass)] == ["candidate", "candidate"]
    assert [s["level_db"] for s in (baseline, bass)] == [-20, -20]
    assert [s["stimulus_dbfs"] for s in (baseline, bass)] == [-14, -14]
    assert bass["calibration"] == {"applied": True, "calibration_id": "recorded-mic"}
    assert 20 <= min(baseline["freqs_hz"]) <= 22
    frequencies = np.array(baseline["freqs_hz"])
    band = (frequencies >= 40) & (frequencies <= program.segment("sweep_verify").f2_hz * 0.9)
    assert np.array(baseline["magnitude_db"])[band] == pytest.approx(20 * np.log10(0.4) - 20, abs=0.3)
    assert np.array(bass["magnitude_db"]) - np.array(baseline["magnitude_db"]) == pytest.approx(2, abs=0.001)
    assert baseline["reference_db"] == pytest.approx(20 * np.log10(0.4) - 20, abs=0.3)
    assert before == {p: p.read_bytes() for p in bundle.rglob("*") if p.is_file()}


@pytest.mark.parametrize("by_take_ids", [False, True])
def test_room_selection_analyzes_only_selected_takes_and_discloses_its_own_omissions(summed_capture_bundle, by_take_ids):
    bundle, _, _, bank = summed_capture_bundle
    fields = {"phase": "lateral", "measurement_purpose": "room", "gating_applied": False}
    asyncio.run(bank("good", **fields))
    asyncio.run(bank("skipped", measurement_status="incomplete", **fields))
    asyncio.run(bank("outside", candidate="another-set", **fields, **(
        {"wav_hash": "0" * 64} if by_take_ids else {"measurement_status": "incomplete"})))
    write_manifest(bundle, program="room")
    selection = select_seat_takes(bundle, capture_id="good", take_ids=("good", "skipped") if by_take_ids else None)
    assert [take.take_id for take in selection.takes] == ["good"]
    assert [(row["take_id"], row["reason"]) for row in selection.evidence["omitted_takes"]] == [
        ("skipped", "seat_curve_or_pose_unusable")]


def _without_recordings(bundle: Path) -> None:
    """A reader that opens a take's recording now fails."""
    for path in bundle.rglob("*.wav"):
        path.unlink()


_ROOM_PROGRAM = build_verify_program(2500, sweep_s=1.5, gain_db=-30, leading_pilot_gains_db=(-24, -14))
_BASS_PROGRAM = repeat_summed_program(build_verify_program(2500, sweep_band_hz=(20, 1100), sweep_s=1.5, gain_db=-30),
                                      passes=3, quiet_samples=96000, cooldown_s=2.0)


@pytest.mark.parametrize("purpose,kind,program,gap", [
    ("room", POSE_KIND_SEAT, _ROOM_PROGRAM, None), ("room", POSE_KIND_BEARING, _ROOM_PROGRAM, None),
    ("bass", POSE_KIND_SEAT, _BASS_PROGRAM, None), ("bass", POSE_KIND_BEARING, _BASS_PROGRAM, None),
    *(("room", POSE_KIND_SEAT, build_verify_program(2500, sweep_band_hz=band, sweep_s=1.5, gain_db=-30,
                                                    leading_pilot_gains_db=(-24, -14)), REASON_COVERAGE_SHORT)
      for band in ((250, 20000), (100, 300))),
])
def test_a_summed_take_banks_what_a_decode_of_its_recording_reads(tmp_path, monkeypatch, purpose, kind, program, gap):
    """The capture host banks a summed take's curves and bass reading, bit for
    bit, as a decode of its recording reads them, and the reader then serves
    them with the recording gone. A take at a seat banks its ungated curve; one
    at a bearing, whatever its purpose, banks both windows (ADR-0400). A sweep
    above the bass band, or too narrow for H3 in it, banks its curves and the
    harmonics' coded gap."""
    pcm = render_program_pcm(program)[:, 0] * 0.4 * 10 ** (-20 / 20)
    signal = np.concatenate([np.zeros(800), pcm + 2 * pcm ** 2, np.zeros(5000)])
    signal += np.random.default_rng(8).normal(0, 1e-6, signal.size)
    record = bank_executor_take(tmp_path, monkeypatch, program=program,
                                recording=(signal * (2 ** 31 - 1)).astype(np.int32),
                                pose={"purpose": purpose, **({"kind": kind, "seat_offset_m": (0.0, 0.0, 0.0)}
                                                             if kind == POSE_KIND_SEAT else {})},
                                raw_record={"measurement_status": "captured", "phase": "lateral"})
    bundle, = {path.parent for path in (tmp_path / "sessions").glob("*/info.json")}
    path, = (take.record_path for take in analyzed_measurements(bundle))
    wav = reopen_measurement_record(bundle, path)[1]()
    calibration = resolve_setup_calibration(record["capture_setup"], device=record["capture_device"],
                                            root=tmp_path / "calibration")
    samples, rate = decode_wav_to_mono(wav)
    analysis = analyze_program_capture(program, samples, rate, calibration=calibration.curve,
                                       geometry=MeasurementGeometry(gate_exempt_reason=gate_exemption(kind)),
                                       capture_report=record["capture_integrity"])
    decoded = analysis_blocks(replace(analysis, bass=bass_evidence(program, analysis, samples, calibration.curve)), program,
                              {curve["window"]: curve["trusted_band"] for curve in record["curves"]})
    reading = record["analysis"]["bass"]
    assert (record["curves"], reading) == (decoded["curves"], decoded["analysis"]["bass"])
    assert [curve["window"] for curve in record["curves"]] == (
        ["ungated"] if kind == POSE_KIND_SEAT else ["gated", "ungated"])
    assert reading["harmonics"].get("reason") == gap
    assert gap or (reading["bands"] and reading["harmonics"]["orders"])

    _without_recordings(bundle)
    banked, = analyzed_measurements(bundle)
    assert banked.document()["curves"] == record["curves"]
    assert banked.document()["calibration"] == {"applied": True, "calibration_id": calibration.calibration_id}


@pytest.mark.parametrize("fields,read", [
    ({}, ["take"]),
    ({"analyzed": False, "analysis_error": {"code": "internal_error", "error_type": "ValueError"}}, []),
    ({"analyzed": False}, TAKE_CURVES_NOT_BANKED),
])
def test_a_take_is_read_from_its_record_never_its_recording(summed_capture_bundle, monkeypatch, tmp_path, capsys, fields, read):
    """A speaker take reads the curves it banked (ADR-0383). A take whose
    analysis failed has none, so a run of only such takes draws none and says
    so; a take that banked neither refuses by name."""
    bundle, _, _, bank = summed_capture_bundle
    asyncio.run(bank("take", phase="lateral", **fields))
    _without_recordings(bundle)
    if read == TAKE_CURVES_NOT_BANKED:
        with pytest.raises(EvidenceUnavailable) as refused:
            list(analyzed_measurements(bundle))
        assert refused.value.reason == TAKE_CURVES_NOT_BANKED
        return
    assert [take.record["take_id"] for take in analyzed_measurements(bundle)] == read
    if not read:
        assert round_views_main(["frequency", str(bundle), "--out", str(tmp_path / "f.json")]) == EXIT_REFUSED
        assert json.loads(capsys.readouterr().out)["reason"] == TAKE_CURVES_NOT_BANKED


def test_a_take_banked_before_its_bass_reading_refuses_the_bass_view_by_that_field(
    summed_capture_bundle, monkeypatch, capsys,
):
    bundle, _, _, bank = summed_capture_bundle
    banked_blocks = analysis_blocks

    def before_the_field(analysis, program, bands):
        blocks = banked_blocks(analysis, program, bands)
        del blocks["analysis"]["bass"]
        return blocks

    monkeypatch.setattr(sys.modules[__name__], "analysis_blocks", before_the_field)
    asyncio.run(bank("baseline"))
    write_manifest(bundle, program="bass")
    assert round_views_main(["bass", str(bundle)]) == EXIT_REFUSED
    answer = json.loads(capsys.readouterr().out)
    assert (answer["reason"], json.loads(answer["detail"])["field"]) == (TAKE_CURVES_NOT_BANKED, "analysis.bass")


def test_a_selected_take_whose_analysis_failed_refuses_the_bass_view_by_name(summed_capture_bundle, tmp_path, capsys):
    bundle, _, _, bank = summed_capture_bundle
    error = {"code": "internal_error", "error_type": "ValueError"}
    asyncio.run(bank("baseline"))
    asyncio.run(bank("failed", analyzed=False, analysis_error=error))
    write_manifest(bundle, program="bass")
    assert round_views_main(["bass", str(bundle), "--out", str(tmp_path / "bass.json")]) == EXIT_REFUSED
    answer = json.loads(capsys.readouterr().out)
    detail = json.loads(answer["detail"])
    assert (answer["reason"], detail["take_id"], detail["analysis_error"]) == (TAKE_CURVES_NOT_BANKED, "failed", error)


@pytest.mark.parametrize('summed_capture_bundle', [20000, 200], indirect=True)
def test_bass_view_reads_banked_takes_and_discloses_unknown_harmonics(
    summed_capture_bundle, tmp_path,
):
    bundle, _, program, bank = summed_capture_bundle
    asyncio.run(bank('baseline'))
    asyncio.run(bank('repeat'))
    manifest = write_manifest(bundle, program='bass')
    _without_recordings(bundle)
    before = {p: p.read_bytes() for p in bundle.rglob('*') if p.is_file()}
    out = tmp_path / 'bass.json'
    assert round_views_main(['bass', str(bundle), '--out', str(out)]) == 0
    view = json.loads(out.read_text())
    assert view['set_id'] == manifest['sets'][0]['set_id']
    assert view['candidate_id'] == 'baseline-fp'
    first, repeat = view['takes']
    assert first['distortion'] == repeat['distortion'] == {'status': 'available'}
    assert first['stimulus_id'] == first['record']['stimulus_id'] == program.stimulus_id
    assert (first['record']['take_id'], repeat['record']['take_id'], 'program' in first['record']) == ('baseline', 'repeat', False)
    assert first['fundamental_db'] == repeat['fundamental_db']
    frequencies = np.array(first['freqs_hz'])
    assert frequencies.max() > 190
    assert np.array(first['fundamental_qualified'])[frequencies > 125].any()
    for order, harmonic in first['harmonics'].items():
        beyond = np.array(harmonic['freqs_hz']) > program.segment('sweep_verify').f2_hz / int(order)
        assert not np.array(harmonic['qualified'])[beyond].any()
        assert all(value is None for value in np.array(harmonic['relative_db'])[beyond])
    assert before == {p: p.read_bytes() for p in bundle.rglob('*') if p.is_file()}


@pytest.mark.parametrize("selected_broken", [False, True])
def test_bass_view_selects_accepted_takes_and_keeps_levels_when_harmonics_fail(
    summed_capture_bundle, tmp_path, monkeypatch, selected_broken,
):
    monkeypatch.chdir(tmp_path)
    bundle, _, _, bank = summed_capture_bundle
    records = []
    for take_id, gap in (("baseline", 0), ("broken", 2316), ("other-set", 0)):
        path = asyncio.run(bank(take_id, capture_gap_frames=gap,
                               wav_hash="0" * 64 if take_id == "other-set" else None))
        records.append((path, json.loads((bundle / EVIDENCE_ROOT / "artifacts" / path).read_text())))
    accepted = ("baseline", "broken") if selected_broken else ("baseline",)
    selected = manifest_set(records[:2], set_id="bass", selected=accepted)
    write_manifest(bundle, program="bass", groups=[selected, manifest_set(records[2:], set_id="other")])
    answer = run_bookkeeping("bass", bundle, set_id="bass")
    assert answer["status"] == "written"
    assert answer["takes"] == len(accepted)
    view = json.loads(Path(answer["out"]).read_text())
    assert view["set_id"] == "bass"
    assert view["candidate_id"] == "baseline-fp"
    takes = view["takes"]
    assert tuple(take["record"]["take_id"] for take in takes) == accepted
    assert takes[0]["distortion"] == {"status": "available"}
    if selected_broken:
        broken = takes[1]
        assert broken["distortion"] == {"status": "unavailable", "reason": "harmonic_window_out_of_range"}
        assert broken["harmonics"] == {}
        assert broken["bands"]
        assert all(np.isfinite(band["signal_plus_noise_dbfs"]) for band in broken["bands"])
        assert broken["freqs_hz"] and broken["fundamental_db"]


def test_bass_fit_verb_is_retired():
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(['bass-fit', 'request.json'])
    assert caught.value.code == 2
