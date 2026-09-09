# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-round-views bass-ladder`` over a round whose steps are known.

Each step's capture is its own rendered program through a memoryless square
law, so the second-harmonic ratio the view should recover is set here and
knowable in advance; the third order has no energy at all and must come back
disclosed as floor-limited rather than counted clean.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jasper.audio_measurement.program import (
    build_verify_program,
    render_program_pcm,
)
from jasper.audio_measurement.sweep import write_sweep_wav
from jasper.bass_extension.candidate_field import EVIDENCE_PASS
from jasper.bass_extension.ladder_evidence import (
    BASS_LADDER_DIRNAME,
    EVIDENCE_FAIL,
    STEP_THD,
    bass_ladder_evidence,
)
from jasper.bass_extension.refusals import BassExtensionRefusal
from jasper.cli import round_views
from jasper.cli._refusal import EXIT_OK, EXIT_REFUSED

from tests.test_bass_extension_candidate_field import MARGIN
from tests.test_crossover_v2_bass_prescription import bass_fit_document
from tests.test_bass_extension_ladder_evidence import BOOSTED

#: A crossover low enough that the summed sweep reaches the rung's own band —
#: `build_verify_program` starts the sweep at min(VERIFY_F_LO_HZ, fc/2).
LOW_FC_HZ = 60.0
FADER_DB = -20.0


def _bank_round(
    root: Path, steps: list[tuple[float, float]], *, fc_hz: float = LOW_FC_HZ,
) -> Path:
    """A banked round whose ladder steps are ``(stimulus_dbfs, square_law)``."""
    bundle = root / "bundle" / "b0"
    artifacts = bundle / "evidence" / "v1" / "artifacts" / "crossover_v2" / "cap-1"
    (artifacts / "positions").mkdir(parents=True)
    (bundle / "summed").mkdir(parents=True)
    (bundle / "info.json").write_text(json.dumps({"session_id": "b0"}))
    root.mkdir(parents=True, exist_ok=True)
    (root / "bass_fit.json").write_text(json.dumps(bass_fit_document()))

    for index, (stimulus_dbfs, square_law) in enumerate(steps):
        program = build_verify_program(
            fc_hz, gain_db=stimulus_dbfs, guard_s=1.0, sweep_s=2.0, tail_s=0.5,
        )
        (artifacts / f"verify_{index:02d}_program.json").write_text(
            json.dumps(program.to_dict())
        )
        played = render_program_pcm(program)[:, 0].astype(np.float64)
        capture = played + square_law * played**2
        stem = f"summed_verify_{index:02d}"
        write_sweep_wav(
            bundle / "summed" / f"{stem}.wav",
            capture.astype(np.float32),
            program.sample_rate_hz,
        )
        (bundle / "summed" / f"{stem}.json").write_text(json.dumps({
            "speaker_group_id": "verify", "phase": "verify",
            "measurement_status": "captured",
            "provenance": {
                "stimulus": {"program_id": program.program_id, "phase": "verify"},
            },
        }))
        (artifacts / "positions" / f"take_{index:02d}.json").write_text(json.dumps({
            "schema_version": 1, "kind": "position_evidence",
            "capture_session_id": "cap-1", "measure_kind": "baseline",
            "take_id": f"take_{index:02d}", "graph_scope": "bass_candidate",
            "bass_target_id": BOOSTED, "candidate_id": "cand-a",
            "level_db": FADER_DB, "stimulus_dbfs": stimulus_dbfs,
            "incident": "", "wav_path": f"summed/{stem}.wav",
        }))
    return root


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, Any]:
    code = round_views.main(argv)
    return code, json.loads(capsys.readouterr().out)


def test_the_ladder_ends_at_the_step_whose_distortion_passes_the_policy(
    tmp_path, capsys,
):
    # The square law puts H2 at roughly (a * peak / 2) below the fundamental:
    # clean at the first two steps, past the conservative policy at the third.
    round_dir = _bank_round(tmp_path / "round-3", [
        (-12.0, 0.001), (-9.0, 0.001), (-6.0, 0.6),
    ])

    code, answer = _run(
        ["bass-ladder", str(round_dir), "--target-id", BOOSTED], capsys
    )

    assert code == EXIT_OK
    document = json.loads(Path(answer["out"]).read_text())
    assert Path(answer["out"]).parent.name == BASS_LADDER_DIRNAME
    rows = document["steps"]
    assert [row["verdict"] for row in rows] == [
        EVIDENCE_PASS, EVIDENCE_PASS, EVIDENCE_FAIL,
    ]
    assert rows[2]["reason"] == STEP_THD
    assert rows[2]["thd_ratio"] > MARGIN.thd_fail_ratio
    # H3 has no energy under a square law, so where H2 is loud enough to grade
    # H3 is disclosed rather than counted as a preternaturally clean order.
    assert rows[2]["clean_orders"] == [2]
    assert rows[2]["floor_limited_orders"] == [3]
    assert document["verdict"] == EVIDENCE_PASS
    assert document["max_level_db"] == pytest.approx(FADER_DB + -9.0)
    # The band graded is where the rung's boost and the sweep meet.
    assert document["basis"]["band_hz"][1] <= document["basis"]["rung_band_hz"][1]

    # And the door reads exactly this file back.
    evidence = bass_ladder_evidence(round_dir, [BOOSTED])
    assert evidence[BOOSTED]["max_level_db"] == document["max_level_db"]


def test_a_ladder_whose_stimulus_never_reaches_the_rung_is_refused_not_failed(
    tmp_path, capsys,
):
    """A two-way's summed sweep starts at 150 Hz; the rung boosts below 45."""
    round_dir = _bank_round(
        tmp_path / "round-4", [(-9.0, 0.001)], fc_hz=1800.0,
    )

    code, answer = _run(
        ["bass-ladder", str(round_dir), "--target-id", BOOSTED], capsys
    )

    assert code == EXIT_REFUSED
    assert answer["reason"] == BassExtensionRefusal.LADDER_INCOMPLETE
    assert not (round_dir / BASS_LADDER_DIRNAME).exists()


def test_a_round_banking_no_take_for_this_rung_is_refused(tmp_path, capsys):
    round_dir = _bank_round(tmp_path / "round-5", [(-9.0, 0.001)])

    code, answer = _run(
        ["bass-ladder", str(round_dir), "--target-id", "natural"], capsys
    )

    assert code == EXIT_REFUSED
    assert answer["reason"] == BassExtensionRefusal.TARGET_INVALID
