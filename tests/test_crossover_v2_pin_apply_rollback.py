# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The previous candidate remains available for an operator to restore."""

from __future__ import annotations

from dataclasses import replace

import pytest

from jasper.active_speaker import compile_preset_from_crossover_preview
from jasper.active_speaker.baseline_profile import build_baseline_profile_candidate
from jasper.active_speaker import candidate_trials
from jasper.active_speaker.candidate_bank import CandidateBankRefusal
from jasper.active_speaker.boost_protection import (
    BOOST_OVER_DECLARED_BOUND, boost_finding_path, config_graph_fingerprint, record_boost_finding,
)
from jasper.active_speaker.crossover_preview import build_crossover_preview
from jasper.web import correction_crossover_v2 as v2host, correction_crossover_v2_status as v2status
from tests.test_active_speaker_baseline_profile import (
    apply_baseline_profile, _draft, _dual_apple_topology, _measurements, _v2_candidate, _valid_config,
)

PREVIOUS = "fp-previous-measured"
CURRENT = "fp-current-measured"


@pytest.fixture(autouse=True)
def _isolated_v2_state(tmp_path):
    v2host.set_state_path_for_tests(tmp_path / "v2_state.json")
    yield
    v2host.set_state_path_for_tests(None)


def _seed_previous_candidate(*, paired: bool = True) -> None:
    v2host.save_v2_state({
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
async def test_apply_refuses_the_graphs_measured_boost_excess(
    monkeypatch, tmp_path, finding, different_graph, different_candidate, expected_code,
):
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply.json"))
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    preset, issues, _ = compile_preset_from_crossover_preview(topology, preview)
    assert preset is not None, issues
    candidate = _v2_candidate(preset)
    config_path = tmp_path / "active.yml"
    state_path = tmp_path / "baseline.json"
    inputs = dict(
        design_draft=draft, crossover_preview=preview,
        measurements=_measurements(topology, tmp_path), tuning_owner="automatic",
        state_path=state_path, config_path=config_path, validate=_valid_config,
    )
    compiled = build_baseline_profile_candidate(
        topology, **inputs, measured_candidate=candidate, compile_config=True,
    )
    graph = config_graph_fingerprint(compiled)
    if finding:
        recorded_graph = "0" * 16 if different_graph else graph
        record_boost_finding(recorded_graph, candidate_fingerprint=candidate.fingerprint, round_id="round-1")
        if finding == "corrupt":
            boost_finding_path(recorded_graph).write_text("{")
    if different_candidate:
        previous_id = candidate.fingerprint
        candidate = replace(candidate, analysis={**candidate.analysis, "capture_note": "new"})
        assert candidate.fingerprint != previous_id
        assert config_graph_fingerprint(build_baseline_profile_candidate(
            topology, **inputs, measured_candidate=candidate, compile_config=True,
        )) == graph
    calls = []

    async def load_config(path):
        calls.append(path)
        return True

    config_path.write_text("incumbent graph\n")
    state_path.write_text("{}")
    result = await apply_baseline_profile(
        topology, **inputs, measured_candidate=candidate, load_config=load_config,
    )

    assert result["status"] == ("blocked" if expected_code else "applied")
    if expected_code:
        assert {issue["code"] for issue in result["issues"]} == {expected_code}
        assert result["apply"] is None
        assert calls == []
        assert config_path.read_text() == "incumbent graph\n"
        assert state_path.read_text() == "{}"
    else:
        assert len(calls) == 1


@pytest.mark.parametrize("code", ["not_found", "ambiguous", "authored_candidate_unreadable"])
def test_bank_refusal_becomes_an_apply_issue(monkeypatch, code):
    def refuse(graph):
        raise CandidateBankRefusal(code, "unavailable")

    monkeypatch.setattr(candidate_trials, "read_boost_finding", refuse)
    issue = candidate_trials.candidate_boost_issue("a" * 16)
    assert issue["code"] == code
    assert issue["severity"] == "blocker"
