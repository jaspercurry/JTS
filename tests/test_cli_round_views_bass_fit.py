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
from jasper.active_speaker.design_draft import (
    DESIGN_DRAFT_KIND,
    SCHEMA_VERSION,
    load_design_draft,
)
from jasper.active_speaker.crossover_v2.room_prescription import (
    ROOM_MEDIAN_UNAVAILABLE,
)
from jasper.bass_extension.profile import BassExtensionRefusal
from jasper.cli._refusal import EXIT_OK, EXIT_REFUSED, EXIT_UNREADABLE
from jasper.cli.round_views import main
from jasper.cli.round_views._common import ARTIFACT_BY_VIEW, REASON_UNREADABLE
from jasper.cli.round_views.bass_fit import MEDIAN_FILENAME
from tests.test_bass_extension_seat_fit import (
    CABINET,
    SEALED_TARGET,
    seat_median_db,
    seat_median_json,
)

ARTIFACT = ARTIFACT_BY_VIEW["bass-fit"].artifact


def _draft(**profile) -> dict:
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": DESIGN_DRAFT_KIND,
        "revision": 1,
        **profile,
    }


def bank_bass_fit_inputs(
    round_dir: Path,
    *,
    draft: dict | str | None = None,
    median: bool = True,
    **median_fields,
) -> Path:
    """The two documents this verb reads, filed beside a banked round: the seat
    median the room-median view writes and the draft banked with the round.

    ``median_fields`` overwrite the median document's own, for a suite pinning
    what the room door will not read. Shared with
    ``tests/test_cli_exit_vocabulary.py``, which runs every view against its
    own fixture round.
    """
    if draft is None:
        draft = _draft(driver_safety_profile={"targets": [SEALED_TARGET]})
    (round_dir / DESIGN_DRAFT_FILENAME).write_text(
        draft if isinstance(draft, str) else json.dumps(draft)
    )
    if median:
        (round_dir / MEDIAN_FILENAME).write_text(json.dumps({
            **seat_median_json(seat_median_db(45.0, 0.707)), **median_fields,
        }))
    return round_dir


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
    ({"draft": _draft()}, EXIT_REFUSED, BassExtensionRefusal.ENCLOSURE_UNKNOWN),
    ({"draft": _draft(driver_safety_profile={"targets": [
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


def test_half_a_declared_plant_is_a_usage_error(tmp_path):
    round_dir = _round_dir(tmp_path)

    with pytest.raises(SystemExit):
        main(["bass-fit", str(round_dir), "--declared-f0-hz", "52"])
