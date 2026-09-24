# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from jasper.active_speaker import driver_base_trim as dbt
from jasper.active_speaker.driver_base_trim import measured_level_trims
from jasper.active_speaker.crossover_preview import crossover_preview_fingerprint


# A two-way stand-in carrying only what measured_level_trims reads.
TWO_WAY = SimpleNamespace(way_count=2)

# Any mapping fingerprints; what matters is that the WRITER and the READER
# derive the key from the same declaration document.
PREVIEW = {
    "kind": "jts_active_speaker_crossover_preview",
    "status": "ready_for_protected_staging",
    "drivers": {"woofer": {"sensitivity_db_2v83_1m": 88.0},
                "tweeter": {"sensitivity_db_2v83_1m": 106.0}},
}


def _bank_base_trim(tmp_path, monkeypatch, *, trims, declaration):
    state = tmp_path / "driver_base_trim.json"
    monkeypatch.setenv(dbt.STATE_PATH_ENV, str(state))
    dbt.write_base_trim(
        trims_db=trims,
        roles=tuple(trims),
        speaker_group_ids=["mono"],
        declaration_fingerprint=declaration,
        trim_source="strict_measured_candidate",
        state_path=state,
    )
    return state


def test_a_banked_base_trim_for_this_declaration_answers(tmp_path, monkeypatch):
    """The ledger says which evidence produced the number."""
    _bank_base_trim(
        tmp_path, monkeypatch,
        trims={"woofer": 0.0, "tweeter": -6.0},
        declaration=crossover_preview_fingerprint(PREVIEW),
    )

    trims, meta = measured_level_trims(TWO_WAY, PREVIEW)

    assert trims == {"woofer": 0.0, "tweeter": -6.0}
    assert meta["source"] == "banked_base_trim"
    assert meta["base_trim"]["status"] == dbt.STATUS_APPLIED
    # The record banks an already-applied level match; no per-crossover deltas.
    assert meta["deltas"] == []
    # The groups the banked record DID level.
    assert meta["measured_group_ids"] == ["mono"]
    assert meta["groups_measured"] == 1


def test_no_banked_trim_answers_empty_and_names_no_source():
    trims, meta = measured_level_trims(TWO_WAY, PREVIEW)
    assert trims == {}
    assert "source" not in meta
    assert meta["base_trim"]["status"] == dbt.STATUS_ABSENT


@pytest.mark.parametrize(
    "tamper, status",
    [
        pytest.param(
            lambda r: r.__setitem__("declaration_fingerprint", "f" * 64),
            dbt.STATUS_DECLARATION_CHANGED,
            id="declaration_moved",
        ),
        pytest.param(
            lambda r: r["trims_db"].__setitem__("tweeter", 4.0),
            dbt.STATUS_UNUSABLE,
            id="hand_edited_to_a_boost",
        ),
        pytest.param(
            lambda r: r["trims_db"].__setitem__("supertweeter", -3.0),
            dbt.STATUS_ROLES_CHANGED,
            id="roles_do_not_match",
        ),
    ],
)
def test_every_refused_status_is_reported_on_the_ledger(
    tmp_path, monkeypatch, tamper, status
):
    """All three refusals reach the ledger. A record dropped without a word
    reads exactly like a speaker nobody ever measured."""
    state = _bank_base_trim(
        tmp_path, monkeypatch,
        trims={"woofer": 0.0, "tweeter": -6.0},
        declaration=crossover_preview_fingerprint(PREVIEW),
    )
    record = json.loads(state.read_text())
    tamper(record)
    state.write_text(json.dumps(record))

    _trims, meta = measured_level_trims(TWO_WAY, PREVIEW)

    assert meta["base_trim"]["status"] == status
    assert meta["base_trim"]["status"] in dbt.REFUSED_STATUSES
    assert meta["base_trim"]["remediation"] == dbt.REMEASURE_REMEDIATION
