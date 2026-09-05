# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jasper.active_speaker.commissioning_evidence_store import EVIDENCE_ROOT
from jasper.active_speaker.crossover_v2.contracts import POSITION_EVIDENCE_KIND
from jasper.active_speaker.crossover_v2.frequency_view import (
    FrequencyViewError,
    build_frequency_view,
    frequency_run,
)
from jasper.active_speaker.measurement_archive import ArchivedMeasurement
from jasper.active_speaker.measurement_document import frequency_run_from_documents
from jasper.active_speaker.round_bank import bank_round
from jasper.active_speaker import measurement_archive
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
    assert [
        magnitude - average["reference_db"]
        for magnitude in average["magnitude_db"]
    ] == [-1.0, 0.0, -2.0]
    assert [
        magnitude - baseline["reference_db"]
        for magnitude in baseline["magnitude_db"]
    ] == [-1.0, 0.0, -2.0]


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
