# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The banked-round catalog: every round the bank filed, addressable by id or path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jasper.active_speaker import baseline_profile, bundles
from jasper.active_speaker.applied_identity import applied_identity
from jasper.active_speaker.bundles import mark_state
from jasper.active_speaker.crossover_v2.round_inputs import resolve_set, round_inputs
from jasper.active_speaker.round_bank import (
    REASON_ROUND_AMBIGUOUS, REASON_ROUND_NOT_FOUND, RoundBankError, bank_round, list_rounds, resolve_round,
    show_round,
)
from jasper.cli import round_views
from jasper.json_fields import parse_utc_iso
from tests.crossover_v2_banked_round import bank_measure_round, bank_seat_round, bank_verify_round
from tests.test_active_speaker_commissioning_coordinator import _applied_anchor

#: Banked out of id order, so newest-first cannot pass by sorting ids.
_BANKED_AT = {"r1": "2026-09-01T12:00:00Z", "r2": "2026-09-01T10:00:00Z", "r3": "2026-09-01T11:00:00Z"}


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    """Three rounds the bank filed in this box's campaign store: speaker r1
    (under an applied tune) and r2, room r3."""
    monkeypatch.setattr(bundles, "sessions_dir", lambda: tmp_path / "sessions")
    applied = tmp_path / "applied-profile.json"
    applied.write_text(json.dumps({**_applied_anchor(), "artifact_schema_version": baseline_profile.SCHEMA_VERSION,
                                   "kind": baseline_profile.BASELINE_PROFILE_KIND}))
    for bank_live in (bank_measure_round, bank_verify_round, bank_seat_round):
        source = bank_live(tmp_path / "live")
        session_dir = round_inputs(source).session_dir
        mark_state(session_dir, "applied")
        banked = bank_round(session_dir, campaign_root=tmp_path / "campaigns", state_path=source / "state.json",
                            applied_profile_path=applied if bank_live is bank_measure_round else None)
        (banked.path / "provenance.json").write_text(json.dumps(
            {**banked.provenance, "banked_at_utc": _BANKED_AT[banked.path.name]}))
    (tmp_path / "campaigns" / "unbanked").mkdir()
    return tmp_path / "campaigns"


def test_a_row_is_what_the_round_recorded(campaign):
    selected = resolve_set(round_inputs(campaign / "r1"))

    assert list_rounds()[0] == {
        "round_id": "r1", "round_dir": str(campaign / "r1"), "program": "speaker", "purposes": ["speaker"],
        "banked_at": parse_utc_iso(_BANKED_AT["r1"]), "status": "complete",
        "sets": {selected.set_id: len(selected.selected_ids)},
        "applied_identity": applied_identity(_applied_anchor()),
    }


@pytest.mark.parametrize("program,limit,expected", [
    (None, None, ["r1", "r3", "r2"]),
    (None, 2, ["r1", "r3"]),
    ("speaker", None, ["r1", "r2"]),
    ("speaker", 1, ["r1"]),
    ("room", None, ["r3"]),
    ("bass", None, []),
])
def test_rounds_list_newest_first_by_program(campaign, program, limit, expected):
    assert [row["round_id"] for row in list_rounds(program=program, limit=limit)] == expected


def test_a_round_resolves_by_id_or_path(campaign, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_round("r2") == campaign / "r2"
    assert resolve_round(str(campaign / "r2")) == campaign / "r2"
    assert resolve_round("campaigns/r2") == Path("campaigns/r2")
    (tmp_path / "r2").mkdir()
    assert resolve_round("./r2") == Path("r2")
    monkeypatch.chdir(campaign)
    assert resolve_round("r2") == Path("r2")


@pytest.mark.parametrize("ref,reason", [
    ("r9", REASON_ROUND_NOT_FOUND),
    ("unbanked", REASON_ROUND_NOT_FOUND),
    ("../r2", REASON_ROUND_NOT_FOUND),
    ("r2", REASON_ROUND_AMBIGUOUS),
])
def test_a_ref_naming_no_round_or_two_is_refused(campaign, tmp_path, monkeypatch, ref, reason):
    (tmp_path / "r2").mkdir()
    monkeypatch.chdir(tmp_path)

    with pytest.raises(RoundBankError) as refused:
        resolve_round(ref)

    assert refused.value.reason == reason


def test_show_names_the_takes_every_view_accepts(campaign):
    shown = show_round("r1")
    selected = resolve_set(round_inputs(campaign / "r1"))

    assert {key: value for key, value in shown.items() if key != "sets"} == {
        key: value for key, value in list_rounds()[0].items() if key != "sets"}
    assert [(group["set_id"], [take["take_id"] for take in group["takes"]]) for group in shown["sets"]] == [
        (selected.set_id, list(selected.selected_ids))]


def test_a_view_reads_a_round_by_id_as_by_its_path(campaign, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    answers = []
    for ref in ("r3", str(campaign / "r3")):
        assert round_views.main(["inventory", ref, "--out", str(tmp_path / "inventory.json")]) == 0
        answers.append(json.loads(capsys.readouterr().out))
    assert answers[0] == answers[1]

    (tmp_path / "r3").mkdir()
    with pytest.raises(SystemExit) as usage:
        round_views.main(["inventory", "r3"])
    assert usage.value.code == 2
