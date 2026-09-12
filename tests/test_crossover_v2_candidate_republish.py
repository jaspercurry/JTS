# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Banked candidate identity and retention."""
import json
from pathlib import Path

import pytest

from jasper.active_speaker.candidate_bank import CandidateBankRefusal, banked_candidates, find_banked_candidate
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
    assert find_banked_candidate(candidate.fingerprint).fingerprint == candidate.fingerprint
    live.unlink()
    for index in range(65):
        _publish(bank, _candidate(program_id=f"new-{index}"), bundle=f"new-{index:03}")
    assert len(banked_candidates()) == 64
    assert saved.is_file()
    assert find_banked_candidate(candidate.fingerprint).path == saved



@pytest.mark.parametrize("fault,code", [("missing", "not_found"), ("tampered", "not_found"),
                                        ("empty", "fingerprint_required"), ("ambiguous", "ambiguous")])
def test_bank_refuses_an_unresolved_identity(bank, fault, code):
    candidate = _candidate()
    if fault != "missing":
        path = _publish(bank, candidate)
        if fault == "tampered":
            raw = json.loads(path.read_text())
            raw["role_attenuations_db"]["tweeter"] -= 1.
            path.write_text(json.dumps(raw))
        if fault == "ambiguous":
            _publish(bank, candidate, bundle="other")
    with pytest.raises(CandidateBankRefusal) as refusal:
        find_banked_candidate("" if fault == "empty" else candidate.fingerprint)
    assert refusal.value.code == code
