# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Banked candidate identity and retention."""
import json
import logging
from pathlib import Path

import pytest

from jasper.active_speaker.candidate_bank import CandidateBankRefusal, banked_candidates, find_banked_candidate
from tests._log_events import event_fields
from tests.test_active_speaker_measured_crossover_candidate import _candidate

BUNDLE = "bundle0000aa"
CAPTURE = "capture-session-1"


@pytest.fixture
def bank(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    monkeypatch.setattr("jasper.active_speaker.bundles.sessions_dir", lambda: root)
    return root


def _publish(root: Path, candidate, *, bundle=BUNDLE, capture=CAPTURE) -> Path:
    path = (
        root / bundle / "evidence" / "v1" / "artifacts"
        / "crossover_v2" / capture / "candidate.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(candidate.to_dict()), encoding="utf-8")
    return path


def test_default_candidate_lookup_survives_live_session_retention(bank):
    candidate = _candidate()
    live = _publish(bank, candidate)
    saved = _publish(bank.parent / "campaigns" / "round-1" / "bundle", candidate)
    assert len(banked_candidates()) == 2
    assert find_banked_candidate(candidate.fingerprint).path == saved
    live.unlink()
    for index in range(65):
        _publish(bank, _candidate(program_id=f"new-{index}"), bundle=f"new-{index:03}")
    assert len(banked_candidates()) == 64
    assert saved.is_file()
    assert find_banked_candidate(candidate.fingerprint).path == saved



@pytest.mark.parametrize("fault,code", [("missing", "not_found"), ("tampered", "not_found"),
                                        ("empty", "fingerprint_required")])
def test_bank_refuses_an_unresolved_identity(bank, fault, code):
    candidate = _candidate()
    if fault != "missing":
        path = _publish(bank, candidate)
        if fault == "tampered":
            raw = json.loads(path.read_text())
            raw["role_attenuations_db"]["tweeter"] -= 1.
            path.write_text(json.dumps(raw))
    with pytest.raises(CandidateBankRefusal) as refusal:
        find_banked_candidate("" if fault == "empty" else candidate.fingerprint)
    assert refusal.value.code == code


@pytest.mark.parametrize("live_bundle", [
    pytest.param("live-only", id="two-lineages"),
    pytest.param("banked-round", id="bank-round-copy"),
])
def test_copies_sharing_a_fingerprint_resolve_to_the_campaign_store(bank, caplog, live_bundle):
    """R2-F15: the fingerprint hashes the whole candidate, so every copy that
    carries it is one candidate, not an ambiguity. The campaign-store copy
    wins: ``bank_round`` copies a bundle there under the same id, and
    retention deletes only the live copy. The event names each lineage once.
    """
    caplog.set_level(logging.INFO, logger="jasper.active_speaker.candidate_bank")
    candidate = _candidate()
    _publish(bank, candidate, bundle=live_bundle)
    campaign = _publish(
        bank.parent / "campaigns" / "round-1" / "bundle", candidate, bundle="banked-round",
    )
    assert find_banked_candidate(candidate.fingerprint).path == campaign
    fields = event_fields(caplog, "correction.crossover_v2_banked_candidate_found")
    lineages = {f"{live_bundle}/{CAPTURE}", f"banked-round/{CAPTURE}"}
    assert sorted(fields["lineages"].split(",")) == sorted(lineages)
