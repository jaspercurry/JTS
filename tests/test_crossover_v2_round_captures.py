# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The shared round loader: what a capture binds to, and what it refuses.

Every fixture here is built, not banked — a program sweep, a known impulse
response, and the convolution of the two written as a capture — so the
binding the loader has to get right is known in advance. The two verbs that
read a round through it (``gate_sweep``, ``close_reference``) pin their own
answers; what is pinned here is the loader.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2 import round_captures
from jasper.active_speaker.crossover_v2.contracts import POSITION_EVIDENCE_KIND
from jasper.active_speaker.crossover_v2.round_captures import (
    RoundCapturesRefused,
    discover_captures,
    doc_pose_key,
    select_capture,
)
from jasper.audio_measurement.bundles import sha256_file
from tests.crossover_v2_fixtures import CAPTURE_RATE as RATE, bank_capture_round

PEAK_IDX = 480
IR_LEN = 4800
PLAYED_PROGRAM = "cloud_verify_program.wav"


def _write_round(root: Path, *, poses: int = 2, curves: bool = True, **kwargs) -> Path:
    """This suite's round: every pose a bare delta, so only the BINDING varies.

    ``curves=False`` banks a round with no declared radiated band.
    """
    ir = np.zeros(IR_LEN, dtype=np.float64)
    ir[PEAK_IDX] = 1.0
    return bank_capture_round(
        root,
        [ir] * poses,
        radiated_band_hz=(150.0, 20000.0) if curves else None,
        **kwargs,
    )


@pytest.mark.parametrize("metadata", [
    "legacy", "canonical", "capture_hash_mismatch", "program_hash_mismatch", "two_rounds",
])
def test_a_capture_binds_to_the_program_its_bytes_name(tmp_path: Path, metadata: str) -> None:
    root = _write_round(tmp_path)
    bundle = root / "bundle" / "b0"
    sidecar = bundle / "summed" / "summed_cloud_verify_00.json"
    wav = sidecar.with_suffix(".wav")
    if metadata != "legacy":
        doc = json.loads(sidecar.read_text())
        doc.update({
            "kind": POSITION_EVIDENCE_KIND, "session_id": "wired-test",
            "candidate_id": "reviewed-candidate", "graph_scope": "speaker_tune",
            "take_id": "cloud_verify_00_a02", "wav_sha256": sha256_file(wav),
        })
        if metadata == "capture_hash_mismatch":
            doc["wav_sha256"] = "0" * 64
        elif metadata == "program_hash_mismatch":
            doc["provenance"]["stimulus"]["wav_sha256"] = "0" * 64
        positions = bundle / "evidence/v1/artifacts/crossover_v2/wired-test/positions"
        positions.mkdir(parents=True)
        (positions / "take-00.json").write_text(json.dumps(doc))
        sidecar.write_text(json.dumps({"phase": "verify", "measurement_status": "captured"}))
        if metadata == "two_rounds":
            (positions.parent.parent / "other-candidate-round").mkdir()
    refusal = {
        "capture_hash_mismatch": round_captures.REFUSE_CAPTURE_UNREADABLE,
        "program_hash_mismatch": round_captures.REFUSE_PROGRAM_UNMATCHED,
        "two_rounds": round_captures.REFUSE_CAPTURE_UNREADABLE,
    }.get(metadata)
    if refusal:
        with pytest.raises(RoundCapturesRefused) as excinfo:
            discover_captures(root)
        assert excinfo.value.reason == refusal
        return
    captures = discover_captures(root)

    first_id = "cloud_verify_00" if metadata == "legacy" else "cloud_verify_00_a02"
    assert [capture.capture_id for capture in captures] == [first_id, "cloud_verify_01"]
    assert {capture.program.name for capture in captures} == {PLAYED_PROGRAM}
    assert all(abs(capture.peak_idx - PEAK_IDX) <= 1 for capture in captures)
    assert all(capture.sample_rate == RATE for capture in captures)
    assert select_capture(bundle, capture_id=wav.stem).capture_id == first_id
    assert select_capture(bundle, capture_id=first_id).capture_id == first_id
    if metadata == "canonical":
        selected = discover_captures(root, select=lambda doc: doc.get("candidate_id") == "reviewed-candidate")
        assert [capture.capture_id for capture in selected] == [first_id]


def test_one_capture_is_a_round(tmp_path: Path) -> None:
    """How many poses a reader needs is the READER's bar, not the loader's.

    The gate sweep wants two (across-pose sigma has no meaning below that);
    a close reference reads exactly one.
    """
    assert len(discover_captures(_write_round(tmp_path, poses=1))) == 1


def test_a_filtered_pose_is_never_decoded(tmp_path: Path, monkeypatch) -> None:
    """The reader's filter runs on the sidecar DOC, before the expensive half.

    A close reference keeps one pose out of a round; deconvolving the other
    two is work nothing reads. The count is of capture WAVs decoded — the
    program WAV is decoded once whatever the filter says.
    """
    root = _write_round(tmp_path, poses=3)
    decoded: list[str] = []
    real = round_captures.read_wav_mono

    def counting(path: Path):
        decoded.append(Path(path).name)
        return real(path)

    monkeypatch.setattr(round_captures, "read_wav_mono", counting)
    captures = discover_captures(
        root, select=lambda doc: doc.get("position_id") == "cloud_verify_01"
    )

    assert [capture.capture_id for capture in captures] == ["cloud_verify_01"]
    assert [name for name in decoded if name.startswith("summed_")] == [
        "summed_cloud_verify_01.wav"
    ]


def test_a_filter_that_matches_nothing_is_an_answer_not_a_refusal(
    tmp_path: Path,
) -> None:
    """The no-captures refusal is about the ROUND, not about the filter."""
    assert discover_captures(_write_round(tmp_path), select=lambda doc: False) == ()


def test_poses_are_keyed_on_the_full_declared_pose(tmp_path: Path) -> None:
    """#3503: same azimuth, different height, is a DIFFERENT pose."""
    ground = _write_round(tmp_path / "ground", vertical_deg=0.0)
    raised = _write_round(tmp_path / "raised", vertical_deg=12.0, distance_m=None)
    ground_keys = [capture.pose_key for capture in discover_captures(ground)]
    raised_keys = [capture.pose_key for capture in discover_captures(raised)]

    assert len(set(ground_keys)) == 2
    assert not set(ground_keys) & set(raised_keys)
    assert all(key.endswith("_dna") for key in raised_keys)


@pytest.mark.parametrize(
    "make, reason, evidence",
    [
        (
            lambda root: root,
            round_captures.REFUSE_NO_CAPTURES,
            {"looked_for": "**/summed/summed_*.json"},
        ),
        (
            lambda root: _write_round(root, declared_sha="0" * 64),
            round_captures.REFUSE_PROGRAM_UNMATCHED,
            {
                "declared_stimulus_sha256": "0" * 64,
                "programs_present": [PLAYED_PROGRAM, "verify_program.wav"],
            },
        ),
        (
            lambda root: _write_round(root, curves=False),
            round_captures.REFUSE_RADIATED_BAND_MISSING,
            {"sidecar": "summed_cloud_verify_00.json"},
        ),
    ],
)
def test_a_missing_input_is_refused_by_name(
    tmp_path: Path, make, reason, evidence
) -> None:
    """A capture is never bound to a plausible program, or graded bandless.

    Each refusal carries the evidence an operator needs to act on it: what
    was looked for, what was declared, and what was actually there.
    """
    with pytest.raises(RoundCapturesRefused) as excinfo:
        discover_captures(make(tmp_path))
    assert excinfo.value.reason == reason
    assert excinfo.value.detail.items() >= evidence.items()


@pytest.mark.parametrize(
    ("doc", "key"),
    [
        (
            {"position_deg": 0, "vertical_deg": 0, "mark_distance_m": 1.0},
            "az+0.00_el+0.00_d+1.00",
        ),
        # No ``pose_kind``: the doc is the bearing it always was, and a stray
        # offset is not a seat.
        (
            {
                "position_deg": 0, "vertical_deg": 0, "mark_distance_m": 1.0,
                "seat_offset_m": [0.3, 0.0, 0.0],
            },
            "az+0.00_el+0.00_d+1.00",
        ),
        (
            {
                "position_deg": 0, "vertical_deg": 0, "mark_distance_m": None,
                "pose_kind": "seat", "seat_offset_m": [0.3, 0.0, 0.0],
            },
            "seat_az+0.00_el+0.00_dna_r+0.30_f+0.00_u+0.00",
        ),
        (
            {
                "position_deg": 0, "vertical_deg": 0, "mark_distance_m": 0.3,
                "pose_kind": "close", "seat_offset_m": None,
            },
            "close_az+0.00_el+0.00_d+0.30",
        ),
    ],
    ids=["bearing", "offset-without-a-kind", "seat", "close"],
)
def test_doc_pose_key_tells_categorized_poses_apart_and_leaves_bearings_alone(
    doc: dict, key: str
) -> None:
    """A kind prefixes the key; a bearing's stays byte-identical (#3503)."""
    assert doc_pose_key(doc) == key


def test_window_view_keeps_one_capture_reference_and_exact_window_math(tmp_path):
    from jasper.active_speaker.crossover_v2 import gate_sweep
    from jasper.active_speaker.crossover_v2.window_view import window_view
    root = _write_round(tmp_path)
    cap = select_capture(root, capture_id="cloud_verify_00")
    result = window_view(root, capture_id=cap.capture_id, rungs_ms=(2, 7))
    run, = result["runs"]
    assert run["metadata"]["direct_peak_sample"] == cap.peak_idx
    assert run["metadata"]["stimulus_wav_sha256"] == cap.program_sha256
    expected, = gate_sweep._read_curves((cap,), gate_sweep.analysis_grid(), (2, 7))
    assert len({r["reference_db"] for r in run["series"]}) == 1
    for curve in run["series"]:
        np.testing.assert_allclose(np.array(curve["magnitude_db"]) - curve["reference_db"], expected.curves[curve["window_ms"]])
        assert curve["capture_id"] == cap.capture_id
        assert curve["validity_floor_hz"] == 1000 / curve["window_ms"]
    with pytest.raises(RoundCapturesRefused):
        window_view(root, capture_id=cap.capture_id, rungs_ms=(2, 7), role="woofer")
