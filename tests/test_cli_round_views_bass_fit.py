# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-round-views bass-fit``: one round's seat median, one family out."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from jasper.active_speaker.crossover_v2.round_inputs import DESIGN_DRAFT_FILENAME
from jasper.active_speaker.design_draft import load_design_draft
from jasper.active_speaker.crossover_v2.room_prescription import (
    ROOM_MEDIAN_UNAVAILABLE,
)
from jasper.bass_extension.adapters.base import BassExtensionRefusal
from jasper.cli._refusal import EXIT_OK, EXIT_REFUSED, EXIT_UNREADABLE
from jasper.cli.round_views import main
from jasper.cli.round_views._common import ARTIFACT_BY_VIEW, REASON_UNREADABLE
from jasper.cli.round_views.bass_fit import MEDIAN_FILENAME
from tests.bass_fit_fixture import (
    CABINET,
    SEALED_TARGET,
    bank_bass_fit_inputs,
    design_draft,
)

ARTIFACT = ARTIFACT_BY_VIEW["bass-fit"].artifact


def _round_dir(tmp_path: Path, **documents) -> Path:
    """The banked shape ``round_inputs`` resolves, carrying those documents."""
    round_dir = tmp_path / "round-1"
    (round_dir / "bundle" / "sess1").mkdir(parents=True)
    return bank_bass_fit_inputs(round_dir, **documents)


def test_bass_fit_writes_the_family_and_answers_with_its_shape(tmp_path, capsys):
    round_dir = _round_dir(tmp_path)

    assert main(["bass-fit", str(round_dir)]) == EXIT_OK

    answer = json.loads(capsys.readouterr().out)
    published = json.loads((round_dir / ARTIFACT).read_text())
    fit = published["bass_fit"]
    assert published["status"] == "fitted"
    assert fit["adapter_id"] == "sealed_v1"
    assert fit["owner_target_id"] == SEALED_TARGET["target_id"]
    assert fit["margin"] == "conservative"
    assert fit["cabinet"] == asdict(CABINET)
    for key, name in (("median", MEDIAN_FILENAME),
                      ("design_draft", DESIGN_DRAFT_FILENAME)):
        assert fit[key]["path"] == str(round_dir / name)
        assert len(fit[key]["sha256"]) == 64
    assert "status" not in answer
    assert answer["out"] == str(round_dir / ARTIFACT)
    assert answer["rung_count"] == len(fit["rungs"])
    assert answer["plant_source"] == "seat_median_fit"
    assert answer["effective_corner_hz"] == fit["effective_corner_hz"]
    assert answer["deepest_boost_headroom_db"] > 0.0
    assert answer["deepest_target_id"] == min(
        fit["rungs"], key=lambda rung: rung["target"]["fp_hz"]
    )["target"]["target_id"]


@pytest.mark.parametrize("documents,code,reason", (
    ({"median": False}, EXIT_UNREADABLE, REASON_UNREADABLE),
    # The room door owns what a median may be; a gated one refuses by ITS name.
    ({"window": "gated"}, EXIT_UNREADABLE, ROOM_MEDIAN_UNAVAILABLE),
    ({"draft": "{ not a draft"}, EXIT_UNREADABLE, "design_draft_unreadable"),
    ({"draft": design_draft()}, EXIT_REFUSED, BassExtensionRefusal.ENCLOSURE_UNKNOWN),
    ({"draft": design_draft(driver_safety_profile={"targets": [
        {**SEALED_TARGET, "cabinet": {"enclosure_kind": "open_baffle"}},
    ]})}, EXIT_REFUSED, BassExtensionRefusal.ENCLOSURE_UNSUPPORTED),
))
def test_a_document_this_verb_cannot_fit_from_refuses_by_name(
    tmp_path, capsys, documents, code, reason,
):
    round_dir = _round_dir(tmp_path, **documents)

    assert main(["bass-fit", str(round_dir)]) == code

    document = json.loads(capsys.readouterr().out)
    assert document["reason"] == reason
    if reason == BassExtensionRefusal.ENCLOSURE_UNKNOWN:
        assert json.loads(document["detail"])["draft_status"] == load_design_draft(
            round_dir / DESIGN_DRAFT_FILENAME
        ).get("status")
    assert not (round_dir / ARTIFACT).exists()


def test_a_directory_that_is_no_round_refuses_as_the_round_not_the_view(
    tmp_path, capsys,
):
    """The round resolver runs in the load stage, as every sibling view's does:
    an unresolvable directory sends the operator to the round, not here."""
    assert main(["bass-fit", str(tmp_path / "nothing-here")]) == EXIT_UNREADABLE

    assert json.loads(capsys.readouterr().out)["reason"] == REASON_UNREADABLE


def test_half_a_declared_plant_is_a_usage_error(tmp_path):
    round_dir = _round_dir(tmp_path)

    with pytest.raises(SystemExit):
        main(["bass-fit", str(round_dir), "--declared-f0-hz", "52"])
