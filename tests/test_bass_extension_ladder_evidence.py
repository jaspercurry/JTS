# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The protection ladder's fail rule, and the door that reads its document.

The rule's constants are the rung's own margin policy, so every threshold in
this file is read off :data:`~jasper.bass_extension.targets.MARGINS` rather
than typed: a policy change moves the fixtures with it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jasper.active_speaker.crossover_v2.bass_prescription import (
    PROTECTION_MISSING,
    BassPrescriptionRefused,
)
from jasper.bass_extension.candidate_field import EVIDENCE_PASS
from jasper.bass_extension.ladder_evidence import (
    BASS_LADDER_DIRNAME,
    EVIDENCE_FAIL,
    STEP_COMPRESSION,
    STEP_FLOOR_LIMITED,
    STEP_GAP,
    STEP_INCIDENT,
    STEP_THD,
    LadderStep,
    bass_ladder_evidence,
    grade_ladder,
    ladder_document_path,
)
from jasper.bass_extension.targets import MARGINS

from tests.test_bass_extension_candidate_field import MARGIN, bass_family
from tests.test_crossover_v2_bass_prescription import (
    bass_fit_document,
    document,
    read,
)

#: The proven fader every step below was played at, and the top rung's dBFS.
FADER_DB = -20.0
TOP_DBFS = -6.0
BOOSTED = bass_family()[0]["target_id"]


def steps(
    *,
    n: int = 3,
    thd: tuple[float, ...] | None = None,
    fundamental: tuple[float, ...] | None = None,
    incident_at: int | None = None,
    dbfs: tuple[float, ...] | None = None,
    margin=MARGIN,
) -> list[LadderStep]:
    """A ladder of ``n`` steps at the margin's own rung step, clean by default.

    ``fundamental`` states each step's deconvolved band transfer; the default
    holds it, which is the linear system that compresses not at all.
    """
    levels = dbfs or tuple(
        TOP_DBFS - margin.rung_step_db * (n - 1 - index) for index in range(n)
    )
    return [
        LadderStep(
            take_id=f"take-{index}",
            stimulus_dbfs=levels[index],
            level_db=FADER_DB,
            fundamental_db=(fundamental[index] if fundamental else -40.0),
            thd_ratio=(thd[index] if thd else margin.thd_fail_ratio / 3.0),
            clean_orders=(2, 3),
            incident="unproven_level" if incident_at == index else "",
        )
        for index in range(len(levels))
    ]


def graded(ladder: list[LadderStep], *, margin=MARGIN) -> dict[str, Any]:
    return grade_ladder(
        ladder, target_id=BOOSTED, margin=margin,
        basis={"candidate_fingerprint": "cand-a", "round_id": "round-3"},
    )


def test_a_clean_ladder_proves_the_fader_its_top_step_played_at():
    evidence = graded(steps())

    assert evidence["verdict"] == EVIDENCE_PASS
    # The fader at which a full-scale program reaches the proven level.
    assert evidence["max_level_db"] == pytest.approx(FADER_DB + TOP_DBFS)
    assert [row["verdict"] for row in evidence["steps"]] == [EVIDENCE_PASS] * 3
    assert evidence["margin"]["policy"] == MARGIN.name


@pytest.mark.parametrize(
    "ladder, reason",
    [
        # A distortion rise past the policy's ratio, at the third step.
        (
            steps(thd=(0.0, 0.0, MARGIN.thd_fail_ratio * 1.5)),
            STEP_THD,
        ),
        # The third step's fundamental rises, but short of the stimulus step
        # by more than the policy allows: its transfer drops by that much.
        (
            steps(fundamental=(
                -40.0, -40.0, -40.0 - MARGIN.compression_fail_db - 0.5,
            )),
            STEP_COMPRESSION,
        ),
        (steps(incident_at=2), STEP_INCIDENT),
        # An unmeasured level between two banked ones is not a proven one.
        (
            steps(dbfs=(-24.0, -21.0, -6.0)),
            STEP_GAP,
        ),
    ],
)
def test_the_first_failing_step_ends_the_ladder(ladder, reason):
    evidence = graded(ladder)

    rows = evidence["steps"]
    assert len(rows) == 3
    assert [row["verdict"] for row in rows[:2]] == [EVIDENCE_PASS] * 2
    assert rows[2]["verdict"] == EVIDENCE_FAIL
    assert rows[2]["reason"] == reason
    # The rung still proved the two steps below the failure.
    assert evidence["verdict"] == EVIDENCE_PASS
    assert evidence["max_level_db"] == pytest.approx(FADER_DB + rows[1]["stimulus_dbfs"])


def test_a_step_with_no_clean_order_is_unproven_and_proves_nothing_below_it():
    ladder = steps(n=1)
    ladder[0] = LadderStep(
        take_id="take-0", stimulus_dbfs=TOP_DBFS, level_db=FADER_DB,
        fundamental_db=None, thd_ratio=None, clean_orders=(),
        floor_limited_orders=(2, 3),
    )

    evidence = graded(ladder)

    assert evidence["verdict"] == EVIDENCE_FAIL
    assert "max_level_db" not in evidence
    assert evidence["steps"][0]["reason"] == STEP_FLOOR_LIMITED


def test_a_positive_proven_level_is_refused_rather_than_published():
    evidence = graded(steps(n=1, dbfs=(0.0,))[:1] and [
        LadderStep(
            take_id="take-0", stimulus_dbfs=0.0, level_db=6.0,
            fundamental_db=-40.0, thd_ratio=0.0, clean_orders=(2, 3),
        )
    ])

    assert evidence["verdict"] == EVIDENCE_FAIL
    assert "max_level_db" not in evidence
    assert evidence["refused"] == "max_level_db_positive"


def test_an_aggressive_policy_passes_the_step_a_conservative_one_fails():
    aggressive = MARGINS["aggressive"]
    rise = steps(thd=(0.0, 0.0, MARGINS["conservative"].thd_fail_ratio * 1.5))

    assert graded(rise)["steps"][2]["verdict"] == EVIDENCE_FAIL
    assert graded(
        steps(thd=(0.0, 0.0, MARGINS["conservative"].thd_fail_ratio * 1.5),
              margin=aggressive),
        margin=aggressive,
    )["steps"][2]["verdict"] == EVIDENCE_PASS


# --------------------------------------------------------------------------- #
# the door: a rung's level is admitted from the document, or not at all
# --------------------------------------------------------------------------- #


def _bank(tmp_path: Path, evidence: dict[str, Any]) -> Path:
    path = ladder_document_path(tmp_path, evidence["target_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence))
    return path


def test_the_door_admits_a_boosted_rung_only_with_a_passing_document(tmp_path):
    passing = graded(steps())
    _bank(tmp_path, passing)
    read_back = bass_ladder_evidence(tmp_path, [BOOSTED])

    # The digest is the READER's, over the bytes on disk -- the document never
    # carries one of its own.
    assert "sha256" not in passing
    assert len(read_back[BOOSTED]["sha256"]) == 64
    assert read_back[BOOSTED]["max_level_db"] == passing["max_level_db"]

    prescription = read(
        document(targets=[BOOSTED]),
        bass_fit=bass_fit_document(),
        ladder=read_back,
        limiter={"sha256": "b" * 64, "verdict": EVIDENCE_PASS},
    )
    boosted = next(
        rung for rung in prescription.rungs
        if rung["target"]["target_id"] == BOOSTED
    )
    assert boosted["max_level_db"] == passing["max_level_db"]


def test_the_door_refuses_the_same_rung_on_a_failed_document(tmp_path):
    _bank(tmp_path, graded(steps(n=1, incident_at=0)))

    with pytest.raises(BassPrescriptionRefused) as refused:
        read(
            document(targets=[BOOSTED]),
            bass_fit=bass_fit_document(),
            ladder=bass_ladder_evidence(tmp_path, [BOOSTED]),
            limiter={"sha256": "b" * 64, "verdict": EVIDENCE_PASS},
        )

    assert refused.value.reason == PROTECTION_MISSING


def test_a_document_measuring_another_target_is_dropped_not_admitted(tmp_path):
    evidence = graded(steps())
    path = ladder_document_path(tmp_path, BOOSTED)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**evidence, "target_id": "someone-else"}))

    assert bass_ladder_evidence(tmp_path, [BOOSTED]) == {}
    assert (tmp_path / BASS_LADDER_DIRNAME).is_dir()
