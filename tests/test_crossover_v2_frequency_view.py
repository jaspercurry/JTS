# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.bundles import open_bundle
from jasper.active_speaker.commissioning_evidence_store import CommissioningEvidenceStore, EVIDENCE_ROOT
from jasper.active_speaker.crossover_v2.record_store import BankedRecordStore
from jasper.active_speaker.crossover_v2.wired_stimulus import CapturedRecordStore, WiredStimulusCapture
from jasper.active_speaker.measurement_analysis import MeasurementAnalysisRefused, analyze_measurement_bundle
from jasper.audio_measurement.calibration import CalibrationCurve, CalibrationRecord
from jasper.audio_measurement.program import ExcitationProgram, build_verify_program, render_program_pcm
from jasper.audio_measurement.wired_capture import WiredMicDevice, WiredRecording
from tests.active_speaker_fixtures import mono_output_topology
from jasper.active_speaker.crossover_v2.contracts import POSITION_EVIDENCE_KIND
from jasper.active_speaker.crossover_v2.frequency_view import (
    FrequencyViewError,
    build_frequency_view,
    frequency_run,
)
from jasper.active_speaker.measurement_archive import ArchivedMeasurement
from jasper.active_speaker.measurement_document import frequency_run_from_documents
from jasper.active_speaker.frequency_view import FrequencyRun, frequency_series
from jasper.active_speaker.frequency_view import build_frequency_view as neutral_view
from jasper.active_speaker.frequency_plot import render_frequency_view
from jasper.active_speaker.crossover_envelope_v2 import chart_cloud_status, prediction_status
from jasper.active_speaker.round_bank import bank_round
from jasper.active_speaker import measurement_archive
from jasper.cli._refusal import EXIT_UNREADABLE
from jasper.cli.round_views import main as round_views_main
from jasper.web import correction_measurements


def _packet(run_id: str, *, offset: float = 0.0) -> dict:
    return {
        "session": {
            "bundle_session_id": run_id,
            "round_id": f"round-{run_id}",
            "started_at": 1000.0 + offset,
            "state": "applied",
        },
        "identity": {
            "topology_id": "speaker",
            "graph_fingerprint": "graph",
            "mic": {"calibration_id": "mic-a"},
        },
        "round": {
            "entry_graph_fingerprint": "before",
            "applied_graph_fingerprint": "after",
            "adoption": {"outcome": "keep"},
            "verification": {"spec": "passed"},
        },
        "spec": {"reference_db": -24.0},
        "curve": {
            "freqs_hz": [100.0, 1000.0, 10000.0],
            "magnitude_db": [-25.0 + offset, -24.0 + offset, -26.0 + offset],
        },
        "positions": {
            "n_positions": 2,
            "angle_deg": {"available": True, "angles_deg": [-7, 0]},
            "curve_grid": {
                "freqs_hz": [100.0, 1000.0, 10000.0],
                "fractional_octave": 12,
                "smoothing_fraction": 6,
            },
            "positions": [
                {
                    "position_id": "axis",
                    "role": "onax",
                    "position_axis": "horizontal",
                    "position_deg": 0,
                    "mark_distance_m": 1.0,
                    "magnitude_db": [-25.0, -24.0, -26.0],
                },
                {
                    "position_id": "left",
                    "role": "offax",
                    "position_axis": "horizontal",
                    "position_deg": -7,
                    "mark_distance_m": 1.0,
                    "magnitude_db": [-26.0, -25.0, -29.0],
                },
            ],
        },
        "entry_baseline": {
            "available": True,
            "captured_at": "2026-08-29T12:00:00Z",
            "program_id": "summed_sweep",
            "reference_mark": "design_axis",
            "graph_fingerprint": "before",
            "freqs_hz": [100.0, 1000.0, 10000.0],
            "magnitude_db": [-25.0, -25.0, -27.0],
            "excluded": [False, False, True],
        },
        "honesty_mask": {
            "validity_floor_hz": 80.0,
            "trusted_floor_hz": 200.0,
            "merged_excluded_bands_hz": [[900.0, 1100.0]],
        },
    }


def test_frequency_view_exposes_stored_average_baseline_and_positions():
    view = build_frequency_view(_packet("aaa"))

    assert view["schema"] == "jts_frequency_view/1"
    run = view["runs"][0]
    assert (run["slot"], run["id"], run["measurement_family"]) == (
        "a", "aaa", "summed_cloud",
    )
    assert [series["id"] for series in run["series"]] == [
        "average", "entry_baseline", "axis", "left",
    ]
    assert [series["visible_by_default"] for series in run["series"]] == [
        True, False, False, False,
    ]
    assert run["series"][1]["smoothing_fractional_octave"] == 3
    assert run["series"][1]["excluded_intervals_hz"] == [[10000.0, 10000.0]]
    assert run["series"][2]["label"] == "0° · On axis"
    assert run["series"][3]["label"] == "-7° · Off axis"
    assert run["metadata"]["smoothing"] == {
        "average_fractional_octave": 3,
        "positions_fractional_octave": 6,
    }
    assert run["metadata"]["mic_calibration_id"] == "mic-a"


def test_frequency_view_gives_the_baseline_its_own_reference_frame():
    packet = _packet("aaa")
    packet["entry_baseline"]["magnitude_db"] = [-35.0, -34.0, -36.0]

    view = build_frequency_view(packet)
    average, baseline = view["runs"][0]["series"][:2]

    assert average["reference_db"] == -24.0
    assert baseline["reference_db"] == -34.0
    assert average["display"]["deviation_db"] == [-1.0, 0.0, -2.0]
    assert baseline["display"]["deviation_db"] == [-1.0, 0.0, -2.0]


@pytest.mark.parametrize("reference", [-24, None])
def test_saved_live_and_predicted_views_share_display_rules(reference):
    raw = {"freqs_hz": [50, 150, 200, 500, 1000, 20000],
           "magnitude_db": [-29, -25, -24, -23, -22, -21], "band_hz": [100, 10000]}
    metadata = {"reference_db": reference, "validity_floor_hz": 143, "trusted_floor_hz": 357,
                "excluded_bands_hz": [[400, 450], [440, 500]]}
    series = frequency_series(series_id="take", label="Take", kind="measurement",
                              reference_db=reference, **raw)
    assert series is not None
    saved = neutral_view(FrequencyRun("run", "speaker_response", (series,), metadata=metadata))
    pipeline = {**metadata, "available": True, "curve": raw, "spec": {"reference_db": reference},
                "merged_excluded_bands_hz": metadata["excluded_bands_hz"]}
    shifted = {**pipeline, "curve": {**raw, "magnitude_db": [db - 10 for db in raw["magnitude_db"]]},
               "spec": {"reference_db": reference - 10 if reference is not None else None}}
    live = chart_cloud_status({"cloud_verify": {"pipeline": pipeline}, "cloud_measure": {"pipeline": shifted}})
    predicted = prediction_status({"verify_priors": {"predicted_sum": raw, "predicted_spec": {
        **metadata, "excluded_intervals": metadata["excluded_bands_hz"],
    }}})
    display = saved["runs"][0]["series"][0]["display"]
    assert live["cloud_verify"]["curve"]["display"] == predicted["curve"]["display"] == display
    assert live["cloud_measure"]["curve"]["display"] == display
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
    assert list(ax.lines[0].get_ydata()) == view["runs"][0]["series"][0]["display"]["deviation_db"]
    spans = [patch.get_path().transformed(patch.get_patch_transform()).vertices[:, 0]
             for patch in ax.patches]
    assert [(min(xs), max(xs)) for xs in spans] == [(50, 357), (400, 500)]
    assert list(ax.lines[-1].get_xdata()) == [900, 900]


def test_frequency_view_adds_optional_run_b_without_changing_run_a():
    view = build_frequency_view(_packet("aaa"), _packet("bbb", offset=1.0))

    assert [(run["slot"], run["id"]) for run in view["runs"]] == [
        ("a", "aaa"), ("b", "bbb"),
    ]
    assert view["runs"][0]["series"][0]["magnitude_db"] == [
        -25.0, -24.0, -26.0,
    ]


def test_frequency_view_requires_the_packet_bundle_identity():
    with pytest.raises(FrequencyViewError, match="bundle session id"):
        build_frequency_view({})


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
        lambda entry: frequency_run(_packet(entry.id)),
    )

    data = correction_measurements.build_data(
        sessions_dir=tmp_path,
        campaign_root=tmp_path / "campaigns",
        run_a_id="aaa",
        run_b_id="bbb",
    )

    assert data["catalog_schema"] == "jts_frequency_catalog/1"
    assert [entry["id"] for entry in data["catalog"]] == ["bbb", "aaa"]
    assert data["selected"] == {"a": "aaa", "b": "bbb"}
    assert [run["id"] for run in data["view"]["runs"]] == ["aaa", "bbb"]


def _bank_one_round(root: Path, session_id: str) -> Path:
    """A campaign home holding one round banked from a one-take bundle."""

    bundle = root / "sessions" / session_id
    positions = (
        bundle / EVIDENCE_ROOT / "artifacts/crossover_v2" / session_id / "positions"
    )
    positions.mkdir(parents=True)
    (positions / "t1.json").write_text(json.dumps({
        "kind": POSITION_EVIDENCE_KIND,
        "session_id": session_id,
        "take_id": "t1",
        "phase": "measure",
        "position_deg": 0,
        "curves": [{
            "role": "summed",
            "reference_db": 0.0,
            "freqs_hz": [100.0, 1000.0, 10000.0],
            "magnitude_db": [-1.0, 0.0, 1.0],
        }],
    }))
    (bundle / "info.json").write_text(json.dumps({
        "session_id": session_id, "started_at": 1000.0, "state": "applied",
    }))
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


def test_archive_reference_is_authoritative_for_direct_records():
    run = frequency_run_from_documents(
        run_id="saved",
        run_reference_db=-30.0,
        documents=({
            "reference_db": -20.0,
            "freqs_hz": [300.0, 1000.0],
            "magnitude_db": [-20.0, -20.0],
        },),
    )

    assert run.series[0].reference_db == -30.0


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


@pytest.mark.parametrize("compared", [False, True])
def test_frequency_cli_reads_capture_prediction_evidence(
    tmp_path: Path, compared: bool, monkeypatch,
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
            "measured": measured if compared else None,
            "window": {
                "window_ms": 7.0,
                "validity_floor_hz": 286.0,
                "trusted_floor_hz": 572.0,
            },
            "comparison_kind": "changed_candidate" if compared else "unmeasured_forecast",
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
        "predicted_minus_measured": comparison if compared else None,
        "limitations": ["No score authorizes playback."],
    }
    source = tmp_path / f"prediction-{compared}.json"
    output = tmp_path / f"frequency-{compared}.json"
    source.write_text(json.dumps(document))

    assert round_views_main([
        "frequency", str(source), "--out", str(output),
    ]) == 0
    [run] = json.loads(output.read_text())["runs"]

    labels = [series["label"] for series in run["series"]]
    assert labels == [
        *([] if compared else ["Forecast predicted response"]),
        "Reconstruction predicted response",
        "Reconstruction measured response",
        *(
            ["Prediction comparison predicted response", "Prediction comparison measured response"]
            if compared else []
        ),
        "Reconstruction level-aligned difference (predicted − measured)",
        *(
            ["Prediction comparison level-aligned difference (predicted − measured)"]
            if compared else []
        ),
    ]
    responses = [series for series in run["series"] if series["role"] != "difference"]
    assert len({series["reference_db"] for series in responses}) == 1
    reconstruction = {series["id"]: series for series in run["series"]}
    predicted_id = "comparison:predicted" if compared else "prediction:predicted"
    forecast = reconstruction[predicted_id]
    assert forecast["candidate_id"] == "target-candidate"
    assert forecast["basis_capture_id"] == "basis-take"
    assert forecast["basis_graph_fingerprint"] == "basis-graph"
    assert "graph_fingerprint" not in forecast
    assert [series["id"] for series in run["series"] if series["visible_by_default"]] == [predicted_id]
    assert reconstruction["reconstruction:predicted"]["magnitude_db"][0] - reconstruction["reconstruction:measured"]["magnitude_db"][0] == 1.0
    assert reconstruction["reconstruction:difference"]["reference_db"] == 0.0
    assert reconstruction["reconstruction:difference"]["level_offset_db"] == 1.0
    assert reconstruction["reconstruction:difference"]["measured_capture_id"] == "basis-take"
    assert reconstruction["reconstruction:difference"]["display"]["deviation_db"] == [0.0, 0.0, 0.0]
    assert run["metadata"]["summary"]["basis"] == basis
    assert run["metadata"]["summary"]["candidate_id"] == "target-candidate"
    assert run["metadata"]["summary"]["measured"] == (
        measured if compared else None
    )
    assert run["metadata"]["summary"]["window"]["window_ms"] == 7.0
    assert run["metadata"]["summary"]["limits"] == (
        "Forecast assumes unchanged setup."
    )

    if compared:
        predicted = reconstruction["comparison:predicted"]
        observed = reconstruction["comparison:measured"]
        assert predicted["reference_db"] == observed["reference_db"]
        assert predicted["display"]["deviation_db"][0] - observed["display"]["deviation_db"][0] == 5.0
        assert observed["capture_id"] == "measured-take"
        assert observed["candidate_id"] == "target-candidate"
        difference = reconstruction["comparison:difference"]
        assert difference["measured_capture_id"] == "measured-take"
        assert difference["measured_graph_fingerprint"] == "measured-graph"
        assert difference["measured_take_path"] == "/captures/measured.json"

        figure = pytest.importorskip("matplotlib.figure")
        figures = []
        monkeypatch.setattr(figure.Figure, "savefig", lambda fig, *a, **kw: figures.append(fig))
        render_frequency_view(json.loads(output.read_text()), tmp_path / "prediction.png")
        evidence = " ".join(figures[0].axes[-1].texts[0].get_text().split())
        assert "basis take: basis-take | candidate: target-candidate | basis graph: basis-graph" in evidence
        assert "measured-take | candidate: target-candidate | graph: measured-graph" in evidence
        assert "measured take: measured-take" in evidence
        assert "measured graph: measured-graph" in evidence

        narrower = json.loads(json.dumps(document))
        narrower["predicted_minus_measured"]["compared_band_hz"] = [1000.0, 2000.0]
        narrow_run = frequency_run_from_documents(
            run_id="narrow", documents=(narrower,),
        )
        assert "prediction:predicted" in {series.id for series in narrow_run.series}


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
    assert run.metadata["summary"]["limits"] == (
        "Comparison response arrays were not retained."
    )


def test_archive_combines_stored_summary_with_direct_records(tmp_path, monkeypatch):
    from jasper.active_speaker.crossover_v2 import evidence_packet

    monkeypatch.setattr(
        measurement_archive,
        "_measurement_documents",
        lambda _bundle: [{
            "take_id": "axis",
            "position_deg": 0,
            "phase": "measure",
            "curves": [{
                "role": "woofer",
                "freqs_hz": [100.0, 1000.0],
                "magnitude_db": [-30.0, -20.0],
            }],
        }],
    )
    monkeypatch.setattr(
        evidence_packet,
        "build_crossover_evidence_packet",
        lambda _bundle: _packet("saved"),
    )

    run = measurement_archive.load_measurement(
        ArchivedMeasurement("saved", tmp_path / "saved", 1.0, "applied"),
    )

    assert [series.id for series in run.series] == [
        "average", "entry_baseline", "axis", "left", "axis:woofer",
    ]
    assert [series.visible_by_default for series in run.series] == [
        True, False, False, False, False,
    ]


def test_archive_keeps_old_packet_positions_when_a_record_has_only_a_baseline(
    tmp_path, monkeypatch,
):
    from jasper.active_speaker.crossover_v2 import evidence_packet

    monkeypatch.setattr(
        measurement_archive,
        "_measurement_documents",
        lambda _bundle: [{
            "take_id": "baseline",
            "phase": "entry_baseline",
            "freqs_hz": [100.0, 1000.0],
            "magnitude_db": [-25.0, -24.0],
        }],
    )
    monkeypatch.setattr(
        evidence_packet,
        "build_crossover_evidence_packet",
        lambda _bundle: _packet("saved"),
    )

    run = measurement_archive.load_measurement(
        ArchivedMeasurement("saved", tmp_path / "saved"),
    )

    assert [series.id for series in run.series] == [
        "average", "entry_baseline", "axis", "left",
    ]
    assert run.metadata["position_count"] == 2
    assert run.metadata["angles_deg"] == [-7, 0]


def test_mixed_candidate_archive_keeps_exact_takes_and_played_graphs(tmp_path, monkeypatch):
    from jasper.active_speaker.crossover_v2 import evidence_packet
    docs = [{"take_id": take, "candidate_id": candidate, "graph_fingerprint": "entry",
             "provenance": {"graph": {"fingerprint": graph}}, "position_deg": 0,
             "curves": [{"role": "summed", "freqs_hz": [500, 1000, 2000],
                         "magnitude_db": [-20, -20, -21], "reference_db": -20}]}
            for take, candidate, graph in (("a", "candidate-a", "played-a"), ("b", "candidate-b", "played-b"))]
    monkeypatch.setattr(measurement_archive, "_measurement_documents", lambda _: docs)
    monkeypatch.setattr(evidence_packet, "build_crossover_evidence_packet", lambda _: _packet("saved"))
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
    recording = WiredRecording(
        ((raw * (2 ** 31 - 1)).astype("<i4").tobytes(),), signal.size,
        0, 0, False, program.sample_rate_hz, 2,
    )

    class Recorder:
        def start(self):
            pass

        def finish(self, **kwargs):
            return recording

        def abort(self):
            pass

    capture = WiredStimulusCapture(
        WiredMicDevice("UMIK2", 2, "2752:002b", "minidsp_umik2", "miniDSP UMIK-2"),
        bundle, recorder_factory=lambda *_: Recorder(),
    )
    async def bank(take_id, *, setup=None, scope="room_tune", candidate="", retain_program=True, wav_hash=None):
        async def play():
            pass
        configured = replace(capture, setup_reference=lambda: setup)
        records = CapturedRecordStore(BankedRecordStore(evidence, "capture"), configured)
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
            "level_db": -20, "stimulus_dbfs": -14, "position_deg": 0,
        }, answer)

    return bundle, calibration_root, program, bank


@pytest.mark.parametrize("summed_capture_bundle,reference_db", [(20000, None), (200, -24.0)],
                         indirect=["summed_capture_bundle"])
def test_frequency_replays_recorded_program_and_calibration_without_changing_level(
    summed_capture_bundle, reference_db, tmp_path,
):
    bundle, calibration_root, program, bank = summed_capture_bundle
    first = asyncio.run(bank("baseline"))
    asyncio.run(bank("bass", scope="bass_candidate", candidate="bass-6db", setup={
        "calibration": {"mode": "stored", "calibration_id": "recorded-mic", "model": "minidsp_umik2"},
    }))
    before = {p: p.read_bytes() for p in bundle.rglob("*") if p.is_file()}
    record = json.loads((bundle / EVIDENCE_ROOT / "artifacts" / first).read_text())
    assert ExcitationProgram.from_dict(record["program"]).program_id == program.program_id
    destination = tmp_path / "frequency.json"
    assert round_views_main(["frequency", str(bundle), "--out", str(destination)]) == EXIT_UNREADABLE
    reference_args = [] if reference_db is None else ["--reference-db", str(reference_db)]
    if reference_db is not None:
        assert round_views_main([
            "frequency", str(bundle), "--analyze-wavs", "--calibration-root", str(calibration_root),
            "--out", str(destination),
        ]) == EXIT_UNREADABLE
    assert round_views_main([
        "frequency", str(bundle), "--analyze-wavs", "--calibration-root", str(calibration_root),
        "--out", str(destination), *reference_args,
    ]) == 0
    view = json.loads(destination.read_text())
    baseline, bass = view["runs"][0]["series"]
    assert baseline["candidate_id"] == ""
    assert bass["candidate_id"] == "bass-6db"
    assert [s["graph_scope"] for s in (baseline, bass)] == ["room_tune", "bass_candidate"]
    assert [s["level_db"] for s in (baseline, bass)] == [-20, -20]
    assert [s["stimulus_dbfs"] for s in (baseline, bass)] == [-14, -14]
    assert bass["calibration"] == {"applied": True, "calibration_id": "recorded-mic"}
    assert 20 <= min(baseline["freqs_hz"]) <= 22
    frequencies = np.array(baseline["freqs_hz"])
    band = (frequencies >= 40) & (frequencies <= program.segment("sweep_verify").f2_hz * 0.9)
    assert np.array(baseline["magnitude_db"])[band] == pytest.approx(20 * np.log10(0.4) - 20, abs=0.3)
    assert np.array(bass["magnitude_db"]) - np.array(baseline["magnitude_db"]) == pytest.approx(2, abs=0.001)
    assert baseline["reference_db"] == pytest.approx(
        20 * np.log10(0.4) - 20 if reference_db is None else reference_db, abs=0.3,
    )
    if reference_db is not None:
        assert baseline["reference_db"] == bass["reference_db"] == reference_db
        assert np.array(baseline["display"]["deviation_db"]) == pytest.approx(
            np.array(baseline["magnitude_db"]) - reference_db,
        )
    assert before == {p: p.read_bytes() for p in bundle.rglob("*") if p.is_file()}


@pytest.mark.parametrize("reference_db", [float("nan"), float("inf"), float("-inf")])
def test_frequency_wav_analysis_rejects_nonfinite_reference(tmp_path, reference_db):
    with pytest.raises(MeasurementAnalysisRefused) as caught:
        analyze_measurement_bundle(tmp_path, run_reference_db=reference_db)
    assert caught.value.code == "measurement_reference_invalid"
    assert round_views_main([
        "frequency", str(tmp_path), "--analyze-wavs", f"--reference-db={reference_db}",
    ]) == EXIT_UNREADABLE


@pytest.mark.parametrize("fault,code", [
    ("scope", "measurement_analysis_program_unsupported"),
    ("program", "measurement_program_manifest_missing"),
    ("wav_hash", "measurement_capture_identity_mismatch"),
    ("dependency", "measurement_capture_identity_mismatch"),
])
def test_frequency_wav_analysis_refuses_unreplayable_takes(summed_capture_bundle, fault, code, tmp_path):
    bundle, _, _, bank = summed_capture_bundle
    asyncio.run(bank(
        "take", scope="drivers" if fault == "scope" else "room_tune",
        retain_program=fault != "program", wav_hash="0" * 64 if fault == "wav_hash" else None,
    ))
    if fault == "dependency":
        manifest = bundle / "artifact_manifest.json"
        document = json.loads(manifest.read_text())
        for row in document["artifacts"]:
            row["dependencies"] = []
        manifest.write_text(json.dumps(document))
    with pytest.raises(MeasurementAnalysisRefused) as caught:
        analyze_measurement_bundle(bundle)
    assert caught.value.code == code
    assert round_views_main([
        "frequency", str(bundle), "--analyze-wavs", "--out", str(tmp_path / "refused.json"),
    ]) == EXIT_UNREADABLE


@pytest.mark.parametrize('summed_capture_bundle', [20000, 200], indirect=True)
def test_bass_view_reopens_exact_captures_and_discloses_unknown_harmonics(
    summed_capture_bundle, tmp_path,
):
    bundle, calibration_root, program, bank = summed_capture_bundle
    asyncio.run(bank('baseline'))
    asyncio.run(bank('repeat'))
    before = {p: p.read_bytes() for p in bundle.rglob('*') if p.is_file()}
    destination = tmp_path / 'bass.json'
    assert round_views_main([
        'bass', str(bundle), '--calibration-root', str(calibration_root), '--out', str(destination),
    ]) == 0
    view = json.loads(destination.read_text())
    first, repeat = view['takes']
    assert first['program_id'] == program.program_id
    assert first['record']['take_id'] == 'baseline'
    assert repeat['record']['take_id'] == 'repeat'
    assert first['fundamental_db'] == repeat['fundamental_db']
    assert first['actual_dsp_drive'] is None
    frequencies = np.array(first['freqs_hz'])
    assert frequencies.max() > 190
    assert np.array(first['fundamental_qualified'])[frequencies > 125].any()
    for order, harmonic in first['harmonics'].items():
        beyond = np.array(harmonic['freqs_hz']) > program.segment('sweep_verify').f2_hz / int(order)
        assert not np.array(harmonic['qualified'])[beyond].any()
        assert all(value is None for value in np.array(harmonic['relative_db'])[beyond])
        assert harmonic['received_db_spl'] is None
    assert before == {p: p.read_bytes() for p in bundle.rglob('*') if p.is_file()}
