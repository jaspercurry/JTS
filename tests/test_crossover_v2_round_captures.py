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

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2 import round_captures
from jasper.active_speaker.crossover_v2.round_captures import (
    RoundCapturesRefused,
    discover_captures,
)
from jasper.audio_measurement.sweep import synchronized_swept_sine, write_sweep_wav

RATE = 48_000
PEAK_IDX = 480
IR_LEN = 4800
PLAYED_PROGRAM = "cloud_verify_program.wav"


def _write_round(
    root: Path,
    *,
    poses: int = 2,
    declared_sha: str | None = None,
    vertical_deg: float = 0.0,
    distance_m: float | None = 1.0,
    curves: bool = True,
) -> Path:
    """A banked-round-shaped directory whose captures are known convolutions.

    Two programs are written and the sidecars declare the CLOUD one's hash
    while their ``provenance.stimulus.phase`` says ``verify`` — the live
    mislabel #3504 documents. A consumer that trusted the label would
    deconvolve against the wrong sweep.
    """
    bundle = root / "bundle" / "b0"
    programs = bundle / "crossover_v2" / "wired-test"
    summed = bundle / "summed"
    programs.mkdir(parents=True)
    summed.mkdir(parents=True)

    played, _ = synchronized_swept_sine(duration_approx_s=1.0, sample_rate=RATE)
    other, _ = synchronized_swept_sine(f1=30.0, duration_approx_s=1.0, sample_rate=RATE)
    write_sweep_wav(programs / PLAYED_PROGRAM, played, RATE)
    write_sweep_wav(programs / "verify_program.wav", other, RATE)
    played_sha = hashlib.sha256((programs / PLAYED_PROGRAM).read_bytes()).hexdigest()

    ir = np.zeros(IR_LEN, dtype=np.float64)
    ir[PEAK_IDX] = 1.0
    for index in range(poses):
        capture = np.convolve(played.astype(np.float64), ir)
        capture = 0.5 * capture / float(np.max(np.abs(capture)))
        stem = f"summed_cloud_verify_{index:02d}"
        write_sweep_wav(summed / f"{stem}.wav", capture.astype(np.float32), RATE)
        doc = {
            "position_id": f"cloud_verify_{index:02d}",
            "phase": "cloud_verify",
            "position_deg": float(index * 7),
            "vertical_deg": vertical_deg,
            "mark_distance_m": distance_m,
            "provenance": {
                "stimulus": {
                    "phase": "verify",
                    "wav_sha256": declared_sha or played_sha,
                }
            },
        }
        if curves:
            doc["curves"] = [{"role": "summed", "band_hz": [150.0, 20000.0]}]
        (summed / f"{stem}.json").write_text(json.dumps(doc))
    return root


def test_a_capture_binds_to_the_program_its_bytes_name(tmp_path: Path) -> None:
    """#3504: every sidecar's declared stimulus PHASE points at the wrong WAV."""
    captures = discover_captures(_write_round(tmp_path))

    assert {capture.program.name for capture in captures} == {PLAYED_PROGRAM}
    # The deconvolution is against the program the hash named, so the recovered
    # peak sits where the synthesized IR put it.
    assert all(abs(capture.peak_idx - PEAK_IDX) <= 1 for capture in captures)
    assert all(capture.sample_rate == RATE for capture in captures)


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
