# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Exact capture reconstruction, candidate forecasts, and measured comparisons."""

import json
import re
import shutil
from collections import Counter
import shlex
from pathlib import Path

import numpy as np
import pytest
import yaml

from jasper.active_speaker.commissioning_evidence_store import EVIDENCE_ROOT
from jasper.active_speaker.measurement_emit import compile_tuning_graph
from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.active_speaker.crossover_v2 import round_captures
from jasper.active_speaker.crossover_v2.capture_prediction import (
    compare_transfer,
    predict_transfer,
    read_diagnostic,
)
from jasper.active_speaker.crossover_v2.contracts import POSITION_EVIDENCE_KIND
from jasper.active_speaker.crossover_v2.forward_model import (
    ACCEPTANCE_JUDGED,
    ACCEPTANCE_NOT_RUN,
    ForwardModelError,
    PredictedSum,
    predicted_minus_measured_db,
)
from jasper.active_speaker.crossover_v2.journey import PHASE_MEASURE
from jasper.active_speaker.crossover_v2.position_cycle import parse_curve_complex
from jasper.active_speaker.crossover_v2.round_captures import (
    REFUSE_CLOSE_REFERENCE_NO_CAPTURE,
    RoundCapturesRefused,
)
from jasper.audio_measurement.branch_program import build_branch_program
from jasper.audio_measurement.program import build_verify_program
from jasper.audio_measurement.program_analysis import (
    MeasurementPriors,
    analyze_program_capture,
)
from jasper.audio_measurement.sweep import write_sweep_wav
from jasper.cli._refusal import EXIT_REFUSED, EXIT_UNREADABLE
from jasper.cli.round_views import (
    ACCEPTANCE_RUNS,
    REASON_UNREADABLE,
    build_parser,
    main as cli_main,
)

from tests.crossover_v2_fixtures import bank_capture_round
from tests.run_manifest_fixture import write_manifest, manifest_set
from tests.test_audio_measurement_program_analysis import (
    SR,
    _band_impulse,
    _synthesize,
)
from tests.test_crossover_v2_tuning_scope import _trial_candidate

pytest_plugins = ("tests.test_crossover_v2_tuning_scope",)

FC_HZ = 1800.0
BAND = (200.0, 12000.0)


def _grid(points: int = 512, band=BAND) -> np.ndarray:
    return np.linspace(band[0], band[1], points)


def _banked(role: str, tf: np.ndarray, freqs: np.ndarray, band=BAND) -> dict:
    """One curve in ``spatial.pose_curve_record``'s exact banked shape."""

    return {
        "role": role,
        "band_hz": [float(band[0]), float(band[1])],
        "freqs_hz": [float(hz) for hz in freqs],
        "magnitude_db": [float(db) for db in 20.0 * np.log10(np.abs(tf))],
        "phase_deg": [float(deg) for deg in np.degrees(np.angle(tf))],
    }


def _bank_take(
    tmp_path: Path,
    curves,
    *,
    phase: str = PHASE_MEASURE,
    position_deg: int = 0,
    take_id: str = "p0_a01",
) -> Path:
    """A bundle carrying one banked take, at the path the store writes.

    No index file: ``bundle_measurements`` rescans the take files on disk,
    which is what a hand-built fixture like this one relies on.
    """

    positions = (
        tmp_path / EVIDENCE_ROOT / "artifacts" / "crossover_v2" / "capture-1"
        / "positions"
    )
    positions.mkdir(parents=True, exist_ok=True)
    (positions / f"{take_id}.json").write_text(
        json.dumps({
            "schema_version": 1,
            "kind": POSITION_EVIDENCE_KIND,
            "phase": phase,
            "take_id": take_id,
            "position_deg": position_deg,
            "curves": curves,
        }),
        encoding="utf-8",
    )
    return tmp_path


def _translated(ir: np.ndarray, samples: int) -> np.ndarray:
    """Move a finite fixture impulse without wrapping its ends."""

    moved = np.zeros_like(ir)
    if samples >= 0:
        moved[samples:] = ir[: ir.size - samples] if samples else ir
    else:
        moved[:samples] = ir[-samples:]
    return moved


@pytest.fixture
def diagnostic_round(tmp_path: Path) -> Path:
    """Three exact complete-tune takes in the production bank shape."""

    size = 1800
    woofer = np.zeros(size)
    tweeter = np.zeros(size)
    woofer[220] = 1.0
    woofer[237] = 0.16
    tweeter[232] = -0.995
    tweeter[253] = 0.11
    summed = woofer + tweeter
    physical = {
        "old": (woofer, tweeter, summed),
        "new-level": (0.5 * woofer, 0.5 * tweeter, 0.5 * summed),
        "new-shape": (
            0.5 * woofer,
            0.5 * tweeter,
            0.5 * (summed + 0.3 * _translated(summed, 24)),
        ),
    }
    clocks = {
        "old": {"woofer": 3, "tweeter": -2, "summed": 1},
        "new-level": {"woofer": -4, "tweeter": 2, "summed": -1},
        "new-shape": {"woofer": 2, "tweeter": -3, "summed": 4},
    }
    root = bank_capture_round(
        tmp_path,
        [physical[name][2] for name in physical],
        capture_ids=tuple(physical),
        positions_deg=(0.0, 0.0, 0.0),
    )
    summed_dir = root / "bundle" / "b0" / "summed"
    for take_id, role_irs in physical.items():
        path = summed_dir / f"summed_{take_id}.json"
        document = json.loads(path.read_text())
        document.update({
            "take_id": take_id,
            "candidate_id": f"candidate-{take_id}",
            "graph_fingerprint": f"graph-{take_id}",
            "branch_diagnostic": {
                "sample_rate_hz": 48_000,
                "global_offset_samples": 731,
                "clock_epsilon_ppm": 37.0,
                "timing_reference": (
                    "shared recording schedule; clock drift removed from complex phase"
                ),
                "delay_coordinates": "physical delay retained",
                "normalization": "exact emitted stimulus; no branch level fitting",
                "responses": [
                    {
                        "role": role,
                        "segment_id": f"sweep_{role}",
                        "input_channel": 1 if role == "tweeter" else 0,
                        "scheduled_start_sample": 10_000 + index * 20_000,
                        "pre_guard_samples": 64,
                        "clock_shift_samples": clocks[take_id][role],
                        "gate": {},
                        "band_hz": [150.0, 20_000.0],
                        "impulse": _translated(
                            role_irs[index], clocks[take_id][role]
                        ).tolist(),
                    }
                    for index, role in enumerate(("woofer", "tweeter", "summed"))
                ],
            },
        })
        path.write_text(json.dumps(document))
    write_manifest(root, groups=[manifest_set(
        [(str(path.relative_to(root / "bundle/b0")), json.loads(path.read_text()))], set_id=name)
        for name in physical for path in [root / "bundle/b0/summed" / f"summed_{name}.json"]])
    return root


def test_inventory_resolves_sets_without_reading_diagnostic_metadata(diagnostic_round, capsys):
    broken = diagnostic_round / "bundle/b0/summed/summed_old.json"
    broken.write_text("{")
    assert cli_main(["inventory", str(diagnostic_round)]) == 0
    payload = json.loads(Path(json.loads(capsys.readouterr().out)["out"]).read_text())
    rows = [row for row in payload["artifacts"] if row["view"] == "forward-model"]
    assert {row["set_id"] for row in rows} == {"old", "new-level", "new-shape"}
    for row in rows:
        assert row["required_inputs"] == []
        assert shlex.split(row["next_command"])[-2:] == ["--set", row["set_id"]]


def _bind_candidate_take(
    root: Path, take_id: str, candidate, profile, *, corrupt_graph: bool = False,
) -> None:
    path = root / "bundle" / "b0" / "summed" / f"summed_{take_id}.json"
    document = json.loads(path.read_text())
    config = yaml.safe_load(compile_tuning_graph(
        profile, scope="candidate_branches", candidate=candidate,
    ))
    document["candidate_id"] = candidate.fingerprint
    document["provenance"]["graph"] = {
        "config": config,
        "fingerprint": (
            "0" * 64 if corrupt_graph else json_fingerprint(config)
        ),
    }
    path.write_text(json.dumps(document))


def test_the_shared_complex_parse_inverts_the_banked_serialization() -> None:
    """The one place a banked curve becomes a transfer function, and the exact
    inverse of ``pose_curve_record``'s ``magnitude_db`` / ``phase_deg`` pair."""

    freqs = _grid(128)
    tf = (freqs / FC_HZ) * np.exp(-2j * np.pi * freqs * 90.0 * 1e-6)

    parsed = parse_curve_complex(_banked("tweeter", tf, freqs))

    assert parsed is not None
    grid, transfer, band = parsed
    assert np.allclose(grid, freqs)
    assert np.allclose(transfer, tf)
    assert band == BAND


@pytest.mark.parametrize(
    "curve",
    [
        pytest.param({"role": "w"}, id="missing_arrays"),
        pytest.param(
            {"role": "w", "freqs_hz": [1.0, 2.0], "magnitude_db": [0.0, 0.0]},
            id="no_phase",
        ),
        pytest.param(
            {"role": "w", "freqs_hz": [1.0, 2.0], "magnitude_db": [0.0, 0.0],
             "phase_deg": [0.0]},
            id="ragged_phase",
        ),
    ],
)
def test_the_shared_complex_parse_declines_a_curve_it_cannot_reconstruct(
    curve,
) -> None:
    assert parse_curve_complex(curve) is None


# --------------------------------------------------------------------------- #
# exact-recording diagnostic basis
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("capture_id, scale", [("old", 1.0), ("new-level", 0.5)])
def test_the_common_window_reconstructs_each_named_take_in_its_recording_clock(
    diagnostic_round: Path, capture_id: str, scale: float,
) -> None:
    basis = read_diagnostic(diagnostic_round, capture_id, 7.0)

    reconstructed = basis.transfers["woofer"] + basis.transfers["tweeter"]
    assert np.allclose(basis.transfers["summed"], reconstructed, atol=1e-11)
    assert np.allclose(predict_transfer(basis, {}), reconstructed, atol=1e-11)
    assert basis.source["capture_id"] == capture_id
    assert basis.source["candidate_id"] == f"candidate-{capture_id}"
    assert basis.source["graph_fingerprint"] == f"graph-{capture_id}"
    assert np.max(np.abs(reconstructed)) == pytest.approx(
        scale * np.max(np.abs(read_diagnostic(diagnostic_round, "old", 7.0).transfers["summed"])),
        rel=1e-10,
    )


@pytest.mark.parametrize("canonical", [False, True])
def test_a_diagnostic_reads_each_record_and_hashes_each_audio_file_once(
    diagnostic_round: Path, monkeypatch, canonical: bool,
) -> None:
    bundle = diagnostic_round / "bundle" / "b0"
    record = bundle / "summed" / "summed_old.json"
    wav = record.with_suffix(".wav")
    if canonical:
        document = json.loads(record.read_text())
        document.update(kind=POSITION_EVIDENCE_KIND, wav_path="summed/summed_old.wav",
                        wav_sha256=round_captures.sha256_file(wav))
        record = bundle / EVIDENCE_ROOT / "artifacts/crossover_v2/banked/positions/old.json"
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps(document))
    reads, hashes = Counter(), Counter()
    read_text, hash_file = Path.read_text, round_captures.sha256_file

    def read(path, *args, **kwargs):
        reads[path] += 1
        return read_text(path, *args, **kwargs)

    def digest(path):
        hashes[path] += 1
        return hash_file(path)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(round_captures, "sha256_file", digest)
    basis = read_diagnostic(diagnostic_round, "old", 7.0)

    assert reads[record] == 1
    assert hashes[wav] == 1
    assert all(count == 1 for count in hashes.values())
    assert set(basis.captures) == {"woofer", "tweeter", "summed"}
    assert all(capture.record_document is basis.document for capture in basis.captures.values())


@pytest.mark.parametrize("change", ["diagnostic", "window", "candidate", "pose"])
def test_forecast_binding_changes_with_its_evidence_or_analysis(
    diagnostic_round: Path, change: str,
) -> None:
    before = read_diagnostic(diagnostic_round, "old", 7.0)
    record = before.captures["summed"].record_path
    document = json.loads(record.read_text())
    if change == "diagnostic":
        document["branch_diagnostic"]["responses"][0]["impulse"][0] += .01
    elif change == "candidate":
        document["candidate_id"] = "different-candidate"
    elif change == "pose":
        document["position_deg"] = .001
    record.write_text(json.dumps(document))
    after = read_diagnostic(diagnostic_round, "old", 6.0 if change == "window" else 7.0)

    if change == "window":
        assert before.source["capture_fingerprint"] == after.source["capture_fingerprint"]
        assert before.window != after.window
    else:
        assert before.source["capture_fingerprint"] != after.source["capture_fingerprint"]


def test_prediction_changes_compose_on_the_reconstructed_complex_branches(
    diagnostic_round: Path,
) -> None:
    basis = read_diagnostic(diagnostic_round, "old", 7.0)
    freqs = basis.freqs_hz
    changes = {
        "woofer": 10 ** (-2.5 / 20) * np.exp(-2j * np.pi * freqs * 75e-6),
        "tweeter": -10 ** (-1.25 / 20) * np.exp(2j * np.pi * freqs * 40e-6),
    }

    predicted = predict_transfer(basis, changes)

    assert np.allclose(
        predicted,
        basis.transfers["woofer"] * changes["woofer"]
        + basis.transfers["tweeter"] * changes["tweeter"],
    )


@pytest.mark.parametrize(
    "capture_id, shape_floor_db",
    [("new-level", 0.0), ("new-shape", 1.0)],
)
def test_the_comparison_keeps_raw_level_error_separate_from_shape_error(
    diagnostic_round: Path, capture_id: str, shape_floor_db: float,
) -> None:
    basis = read_diagnostic(diagnostic_round, "old", 7.0)
    measured = read_diagnostic(diagnostic_round, capture_id, 7.0)

    comparison = compare_transfer(basis, predict_transfer(basis, {}), measured)

    assert comparison["raw_rms_db"] > 5.0
    assert comparison["raw_max_abs_db"] >= comparison["raw_rms_db"]
    assert comparison["max_abs_db"] >= shape_floor_db
    assert comparison["phase"]["status"] == "unavailable"
    if capture_id == "new-level":
        assert comparison["level_offset_db"] == pytest.approx(20 * np.log10(2))
        assert comparison["rms_db"] == pytest.approx(0.0, abs=1e-10)
        assert comparison["raw_rms_db"] == pytest.approx(20 * np.log10(2))


def test_phase_error_excludes_the_same_recordings_weak_cancellations(
    diagnostic_round: Path,
) -> None:
    basis = read_diagnostic(diagnostic_round, "old", 7.0)
    transfer = predict_transfer(basis, {}) * np.exp(1j * np.radians(12.0))

    phase = compare_transfer(basis, transfer, basis)["phase"]

    assert phase["status"] == "available"
    assert phase["rms_deg"] == pytest.approx(12.0)
    assert phase["max_abs_deg"] == pytest.approx(12.0)
    assert phase["included_points"] > 0
    assert phase["excluded_points"] > 0
    assert phase["included_points"] + phase["excluded_points"] == basis.freqs_hz.size


@pytest.mark.parametrize(
    "capture_id, window_ms, error",
    [
        pytest.param("missing", 7.0, RoundCapturesRefused, id="wrong-take"),
        pytest.param("old", 0.0, ForwardModelError, id="zero-window"),
        pytest.param("old", float("nan"), ForwardModelError, id="nan-window"),
        pytest.param("old", 100.0, ForwardModelError, id="overlong-window"),
        pytest.param("missing-clock", 7.0, RoundCapturesRefused, id="incomplete-clock"),
    ],
)
def test_the_diagnostic_reader_refuses_an_unanswerable_exact_read(
    diagnostic_round: Path, capture_id: str, window_ms: float, error: type[Exception],
) -> None:
    if capture_id == "missing-clock":
        path = diagnostic_round / "bundle/b0/summed/summed_old.json"
        document = json.loads(path.read_text())
        del document["branch_diagnostic"]["responses"][0]["clock_shift_samples"]
        path.write_text(json.dumps(document))
        capture_id = "old"
    with pytest.raises(error) as excinfo:
        read_diagnostic(diagnostic_round, capture_id, window_ms)
    if error is RoundCapturesRefused and capture_id == "old":
        assert excinfo.value.reason == round_captures.REFUSE_CAPTURE_UNREADABLE
        assert excinfo.value.detail["role"] == "woofer"
    if capture_id == "missing":
        assert excinfo.value.reason == REFUSE_CLOSE_REFERENCE_NO_CAPTURE


def test_an_analyzed_raw_branch_record_reconstructs_after_measured_clock_drift(
    tmp_path: Path,
) -> None:
    """The 5% bound includes phase after measured drift correction; the
    separate 0.2 dB bound keeps that tolerance from hiding a level error."""

    base = build_verify_program(
        1600, measurement_band_hz=(150, 20_000), gain_db=-20,
        sweep_s=0.6, courtesy_prelude=False,
    )
    program = build_branch_program(base, {"woofer": 0, "tweeter": 1})
    capture = _synthesize(
        program,
        woofer_ir=_band_impulse(200, 150, 20_000, 1.0),
        tweeter_ir=_band_impulse(212, 150, 20_000, -0.7),
        epsilon=80e-6,
        noise=1e-7,
    )
    analysis = analyze_program_capture(
        program, capture, SR, priors=MeasurementPriors(crossover_fc_hz=1600),
    )
    root = bank_capture_round(tmp_path, [np.array([0.0, 1.0])], capture_ids=("raw",))
    sidecar = root / "bundle" / "b0" / "summed" / "summed_raw.json"
    wav = sidecar.with_suffix(".wav")
    write_sweep_wav(wav, capture.astype(np.float32), SR)
    document = json.loads(sidecar.read_text())
    document.update({
        "take_id": "raw",
        "graph_fingerprint": "raw-graph",
        "branch_diagnostic": analysis.branch_diagnostic,
    })
    sidecar.write_text(json.dumps(document))

    basis = read_diagnostic(root, "raw", 7.0)

    assert analysis.drift.epsilon_ppm == pytest.approx(80.0, abs=2.0)
    predicted = predict_transfer(basis, {})
    crossover = (basis.freqs_hz >= 1200) & (basis.freqs_hz <= 4000)
    relative_error = np.abs(predicted - basis.transfers["summed"]) / np.maximum(
        np.abs(basis.transfers["summed"]), 1e-8,
    )
    assert np.percentile(relative_error[crossover], 95) < 0.05
    magnitude_error_db = np.abs(20 * np.log10(
        np.maximum(np.abs(predicted), 1e-12)
        / np.maximum(np.abs(basis.transfers["summed"]), 1e-12)
    ))
    assert np.percentile(magnitude_error_db[crossover], 95) < 0.2


def test_the_cli_forecast_is_unjudged_until_the_exact_changed_candidate_take_exists(
    diagnostic_round: Path, tmp_path: Path, tuning_profile,
) -> None:
    source = _trial_candidate(tuning_profile, trim=-3.0, gain=-2.0)
    target = _trial_candidate(tuning_profile, trim=-5.0, gain=4.0)
    source_path = tmp_path / "source-candidate.json"
    target_path = tmp_path / "target-candidate.json"
    source_path.write_text(json.dumps(source.to_dict()))
    target_path.write_text(json.dumps(target.to_dict()))
    _bind_candidate_take(diagnostic_round, "old", source, tuning_profile)
    _bind_candidate_take(diagnostic_round, "new-shape", target, tuning_profile)
    command = [
        "forward-model", str(diagnostic_round),
        "--set", "old",
        "--candidate-json", str(target_path),
        "--basis-candidate-json", str(source_path),
        "--window-ms", "7",
    ]

    assert cli_main(command) == 0
    forecast = json.loads((diagnostic_round / "forward_model-old.json").read_text())
    assert forecast["summary"]["candidate_id"] == target.fingerprint
    assert forecast["summary"]["comparison_kind"] == "unmeasured_forecast"
    assert forecast["summary"]["acceptance"]["status"] == ACCEPTANCE_NOT_RUN
    assert forecast["summary"]["measured"] is None
    fingerprint = forecast["summary"]["prediction_fingerprint"]
    assert fingerprint
    assert forecast["relative_graph"]["usable_bins_by_role"]["woofer"] > 0
    assert forecast["relative_graph"]["usable_bins_by_role"]["tweeter"] > 0

    relocated = tmp_path / "relocated"
    shutil.copytree(diagnostic_round, relocated)
    bundle = relocated / "bundle" / "b0"
    source_record = bundle / "summed" / "summed_old.json"
    document = json.loads(source_record.read_text())
    document.update(kind=POSITION_EVIDENCE_KIND, wav_path="summed/summed_old.wav",
                    wav_sha256=round_captures.sha256_file(source_record.with_suffix(".wav")))
    canonical = bundle / EVIDENCE_ROOT / "artifacts/crossover_v2/banked/positions/source.json"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(json.dumps(document))
    source_record.unlink()
    command[1] = str(relocated)
    assert cli_main(command + [
        "--measured-round", str(diagnostic_round),
        "--measured-set", "new-shape",
        "--expected-prediction-fingerprint", fingerprint,
    ]) == 0
    judged = json.loads((relocated / "forward_model-old.json").read_text())
    assert judged["summary"]["basis"]["record_path"] != forecast["summary"]["basis"]["record_path"]
    assert judged["summary"]["prediction_fingerprint"] == fingerprint
    assert judged["summary"]["forecast_binding"] == {
        "status": "matched",
        "expected_prediction_fingerprint": fingerprint,
    }
    assert judged["summary"]["comparison_kind"] == "changed_candidate"
    assert judged["summary"]["acceptance"]["status"] == ACCEPTANCE_JUDGED
    assert judged["summary"]["measured"]["capture_id"] == "new-shape"
    assert judged["predicted_minus_measured"]["compared_points"] > 0


@pytest.mark.parametrize(
    "fault",
    ["wrong-source-candidate", "played-graph-identity", "wrong-forecast-fingerprint"],
)
def test_the_cli_refuses_candidate_prediction_without_exact_source_proof(
    diagnostic_round: Path, tmp_path: Path, tuning_profile, capsys, fault: str,
) -> None:
    source = _trial_candidate(tuning_profile)
    target = _trial_candidate(tuning_profile, trim=-5.0, gain=4.0)
    source_path = tmp_path / "source-candidate.json"
    target_path = tmp_path / "target-candidate.json"
    source_path.write_text(json.dumps(source.to_dict()))
    target_path.write_text(json.dumps(target.to_dict()))
    _bind_candidate_take(
        diagnostic_round, "old", source, tuning_profile,
        corrupt_graph=fault == "played-graph-identity",
    )
    if fault == "wrong-source-candidate":
        path = diagnostic_round / "bundle" / "b0" / "summed" / "summed_old.json"
        document = json.loads(path.read_text())
        document["candidate_id"] = target.fingerprint
        path.write_text(json.dumps(document))

    command = [
        "forward-model", str(diagnostic_round),
        "--set", "old",
        "--candidate-json", str(target_path),
        "--basis-candidate-json", str(source_path),
        "--window-ms", "7",
    ]
    if fault == "wrong-forecast-fingerprint":
        _bind_candidate_take(diagnostic_round, "new-shape", target, tuning_profile)
        command += [
            "--measured-round", str(diagnostic_round),
            "--measured-set", "new-shape",
            "--expected-prediction-fingerprint", "0" * 64,
        ]
    code = cli_main(command)
    refusal = json.loads(capsys.readouterr().out)

    assert code == EXIT_REFUSED
    assert refusal["reason"] == {
        "wrong-source-candidate": "forward_model_candidate_mismatch",
        "played-graph-identity": "forward_model_graph_mismatch",
        "wrong-forecast-fingerprint": "forward_model_forecast_mismatch",
    }[fault]
    field = {
        "wrong-source-candidate": "actual_candidate_id",
        "played-graph-identity": "capture_id",
        "wrong-forecast-fingerprint": "actual_prediction_fingerprint",
    }[fault]
    assert refusal["detail"][field]


@pytest.mark.parametrize("source", ["missing", "malformed", "tampered"])
def test_an_unreadable_named_candidate_is_a_source_failure(
    diagnostic_round: Path, tmp_path: Path, tuning_profile, capsys, source: str,
) -> None:
    path = tmp_path / "candidate.json"
    if source == "malformed":
        path.write_text("{")
    elif source == "tampered":
        candidate = _trial_candidate(tuning_profile).to_dict()
        candidate["fingerprint"] = "0" * 64
        path.write_text(json.dumps(candidate))

    code = cli_main([
        "forward-model", str(diagnostic_round), "--set", "old",
        "--candidate-json", str(path),
    ])
    failure = json.loads(capsys.readouterr().out)

    assert code == EXIT_UNREADABLE
    assert failure["status"] == "unreadable"
    assert failure["reason"] == REASON_UNREADABLE


def test_a_same_candidate_repeat_refuses_a_changed_played_graph(
    diagnostic_round: Path, tuning_profile, capsys,
) -> None:
    source = _trial_candidate(tuning_profile)
    changed = _trial_candidate(tuning_profile, trim=-5.0, gain=4.0)
    _bind_candidate_take(diagnostic_round, "old", source, tuning_profile)
    _bind_candidate_take(diagnostic_round, "new-level", changed, tuning_profile)
    path = diagnostic_round / "bundle" / "b0" / "summed" / "summed_new-level.json"
    document = json.loads(path.read_text())
    document["candidate_id"] = source.fingerprint
    path.write_text(json.dumps(document))

    code = cli_main([
        "forward-model", str(diagnostic_round),
        "--set", "old",
        "--measured-round", str(diagnostic_round),
        "--measured-set", "new-level",
        "--window-ms", "7",
    ])
    refusal = json.loads(capsys.readouterr().out)

    assert code == EXIT_REFUSED
    assert refusal["reason"] == "forward_model_graph_mismatch"
    assert refusal["detail"]["measured_capture_id"] == "new-level"


# --------------------------------------------------------------------------- #
# predicted vs measured
# --------------------------------------------------------------------------- #


def test_a_pure_level_difference_is_reported_as_offset_not_as_shape_error() -> None:
    """A forward model over banked solos carries no absolute SPL reference, so
    the raw offset against a measured sum is a LEVEL difference. It is removed
    before subtracting and published as the fact it removed."""

    freqs = _grid()
    predicted = PredictedSum(freqs, np.full(freqs.size, 6.0), BAND, "take")
    offset_db = 7.5

    delta = predicted_minus_measured_db(
        predicted, freqs, predicted.predicted_db - offset_db
    )

    assert delta["predicted_db"] == pytest.approx(predicted.predicted_db)
    assert delta["measured_db"] == pytest.approx(predicted.predicted_db - offset_db)
    assert delta["level_offset_db"] == pytest.approx(offset_db)
    assert delta["max_abs_db"] == pytest.approx(0.0, abs=1e-9)
    assert delta["rms_db"] == pytest.approx(0.0, abs=1e-9)
    assert delta["compared_band_hz"] == [BAND[0], BAND[1]]
    assert delta["compared_points"] == freqs.size
    assert delta["take_path"] == predicted.take_path


def test_a_shape_difference_survives_the_level_normalisation() -> None:
    """The delta reports SHAPE: a single-bin bump on the measured curve comes
    back at its own size, on the bin it was put on."""

    freqs = _grid()
    predicted = PredictedSum(freqs, np.full(freqs.size, 6.0), BAND, "take")
    measured = predicted.predicted_db.copy()
    measured[100] -= 3.0

    delta = predicted_minus_measured_db(predicted, freqs, measured)

    assert delta["max_abs_db"] == pytest.approx(3.0)
    assert delta["delta_db"][100] == pytest.approx(3.0)
    assert delta["freqs_hz"][100] == pytest.approx(float(freqs[100]))


def test_a_measured_curve_that_is_not_a_curve_refuses() -> None:
    freqs = _grid()
    predicted = PredictedSum(freqs, np.full(freqs.size, 6.0), BAND, "take")

    with pytest.raises(ForwardModelError):
        predicted_minus_measured_db(predicted, freqs, freqs[:-1])


# --------------------------------------------------------------------------- #
# the operator door
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("flags", [["--capture-id", "take"], ["--measured-capture-id", "take"], ["--residual-delay-us", "100"], ["--polarity-sign", "-1"], ["--phase", "measure"], ["--position-deg", "0"]])
def test_forward_model_requires_exact_capture_and_rejects_legacy_flags(flags):
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(["forward-model", "round", "--set", "old", *flags])
    assert caught.value.code == 2

@pytest.mark.parametrize("example", [0, 1])
def test_acceptance_examples_run_exact_captures(diagnostic_round, tuning_profile, example):
    commands = re.findall(r"jasper-round-views .*?(?=\n\n|$)", ACCEPTANCE_RUNS, re.S)
    command = commands[example].replace("\\\n", " ")
    candidate = diagnostic_round / "candidate.json"
    source = _trial_candidate(tuning_profile)
    _bind_candidate_take(diagnostic_round, "old", source, tuning_profile)
    candidate.write_text(json.dumps(source.to_dict()))
    substitutions = {"<basis-round>": str(diagnostic_round), "<basis-set>": "old",
                     "<candidate.json>": str(candidate), "<basis-candidate.json>": str(candidate), "<candidate-round>": str(diagnostic_round),
                     "<candidate-set>": "old"}
    argv = [substitutions.get(token, token) for token in shlex.split(command)[1:]]
    assert cli_main(argv) == 0
