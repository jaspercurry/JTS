# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The previous candidate remains available for an operator to restore."""

from __future__ import annotations

from jasper.web import correction_crossover_v2_state as v2state

from dataclasses import replace

import pytest

from jasper.active_speaker import candidate_trials
from jasper.active_speaker.candidate_bank import CandidateBankRefusal
from jasper.active_speaker.boost_protection import (
    BOOST_OVER_DECLARED_BOUND, boost_finding_path, record_boost_finding,
)
from jasper.web import correction_crossover_v2 as v2host, correction_crossover_v2_status as v2status
from tests.test_active_speaker_baseline_profile import (
    _v2_candidate,
)

PREVIOUS = "fp-previous-measured"
CURRENT = "fp-current-measured"


@pytest.fixture(autouse=True)
def _isolated_v2_state(tmp_path):
    v2state.set_state_path_for_tests(tmp_path / "v2_state.json")
    yield
    v2state.set_state_path_for_tests(None)


def _seed_previous_candidate(*, paired: bool = True) -> None:
    v2state.save_v2_state({
        "session_id": "cap_x",
        "applied": True,
        "candidate": {"fingerprint": CURRENT},
        "previous_candidate_fingerprint": PREVIOUS,
        "previous_candidate_displaced_by": CURRENT if paired else "fp-older-apply",
    })


@pytest.mark.parametrize(
    ("paired", "preflight_code", "expected"),
    [
        pytest.param(True, None, True, id="paired+admitted"),
        pytest.param(False, None, False, id="unpaired"),
        pytest.param(True, "not_found", False, id="bank-refuses"),
    ],
)
def test_rollback_available_pairs_and_preflights(
    monkeypatch, paired, preflight_code, expected,
):
    _seed_previous_candidate(paired=paired)
    monkeypatch.setattr(
        v2status, "_offerable_previous_candidate", lambda state: PREVIOUS if preflight_code is None else None,
    )

    assert v2host._previous_candidate_known() is expected


@pytest.mark.parametrize(
    ("finding", "different_graph", "different_candidate", "expected_code"),
    [
        (True, False, False, BOOST_OVER_DECLARED_BOUND),
        (False, False, False, None),
        (True, True, False, None),
        (True, False, True, BOOST_OVER_DECLARED_BOUND),
        ("corrupt", False, False, "boost_finding_unreadable"),
    ],
)
def test_apply_refuses_the_graphs_measured_boost_excess(
    monkeypatch, tmp_path, finding, different_graph, different_candidate, expected_code,
):
    import hashlib
    from jasper.active_speaker.measurement_emit import compile_tuning_graph, load_tuning_declaration
    from tests.test_correction_crossover_v2_endpoints import _seed_baseline_apply_environment, _apply, _bg_run_async, _FakeApplyCam

    topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SESSIONS_DIR", str(tmp_path / "sessions"))
    declaration = load_tuning_declaration(topology)
    candidate = _v2_candidate(preset)
    config_path = tmp_path / "active.yml"
    state_path = tmp_path / "baseline_profile.json"
    graph = hashlib.sha256(compile_tuning_graph(declaration, candidate).encode()).hexdigest()[:16]
    if finding:
        recorded_graph = "0" * 16 if different_graph else graph
        record_boost_finding(recorded_graph, candidate_fingerprint=candidate.fingerprint, round_id="round-1")
        if finding == "corrupt":
            boost_finding_path(recorded_graph).write_text("{")
    if different_candidate:
        previous_id = candidate.fingerprint
        candidate = replace(candidate, analysis={**candidate.analysis, "capture_note": "new"})
        assert candidate.fingerprint != previous_id
        assert hashlib.sha256(compile_tuning_graph(declaration, candidate).encode()).hexdigest()[:16] == graph
    from jasper.sound.profile import SoundProfile, SimpleEq, save_profile
    from jasper.sound.settings import saved_sound_layers
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(tmp_path / "sound.json"))
    save_profile(SoundProfile(simple_eq=SimpleEq(bass_db=2.0)))
    cam = _FakeApplyCam()
    config_path.write_text("incumbent graph\n")
    state_path.write_text("{}")
    result = _apply({"expected_candidate_fingerprint": candidate.fingerprint, "candidate": candidate.to_dict()}, _bg_run_async, lambda: cam)

    assert result["status"] == ("blocked" if expected_code else "applied")
    if expected_code:
        assert result["issue"]["code"] == expected_code
        assert cam.path is None
        assert config_path.read_text() == "incumbent graph\n"
        assert state_path.read_text() == "{}"
    else:
        from pathlib import Path
        preference_filters, trim_db = saved_sound_layers()
        assert Path(cam.path).read_text() == compile_tuning_graph(declaration, candidate,
            preference_filters=preference_filters, output_trim_db=trim_db)


@pytest.mark.parametrize("code", ["not_found", "ambiguous", "authored_candidate_unreadable"])
def test_bank_refusal_becomes_an_apply_issue(monkeypatch, code):
    def refuse(graph):
        raise CandidateBankRefusal(code, "unavailable")

    monkeypatch.setattr(candidate_trials, "read_boost_finding", refuse)
    issue = candidate_trials.candidate_boost_issue("a" * 16)
    assert issue["code"] == code
    assert issue["severity"] == "blocker"
