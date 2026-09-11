# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The previous candidate remains available for an operator to restore."""

from __future__ import annotations

from jasper.active_speaker import compile_preset_from_crossover_preview
from jasper.active_speaker.baseline_profile import apply_baseline_profile
from jasper.active_speaker.candidate_bank import publish_authored_candidate
from jasper.active_speaker.crossover_preview import build_crossover_preview
from tests.test_active_speaker_baseline_profile import (
    _draft, _dual_apple_topology, _measurements, _v2_candidate, _valid_config,
)
import json
from dataclasses import replace



import pytest

from jasper.web import correction_crossover_v2 as v2host
from jasper.web import correction_crossover_v2_republish as republish_door

RESTORE_EVENT = "correction.crossover_v2_delta_probe_restore"
REFUSED_EVENT = "correction.crossover_v2_delta_probe_restore_refused"

RUN_ASYNC = object()
CAMILLA_FACTORY = object()
PREVIOUS = "fp-previous-measured"


@pytest.fixture(autouse=True)
def _isolated_v2_state(tmp_path):
    v2host.set_state_path_for_tests(tmp_path / "v2_state.json")
    yield
    v2host.set_state_path_for_tests(None)


CURRENT = "fp-current-measured"


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
        republish_door, "republish_preflight", lambda fingerprint: preflight_code,
    )

    assert v2host._previous_candidate_known() is expected


@pytest.mark.parametrize(
    ("over_bound", "same_candidate", "expected"),
    [(True, True, "blocked"), (False, True, "applied"), (True, False, "applied")],
)
async def test_apply_refuses_the_candidates_measured_boost_excess(
    monkeypatch, tmp_path, over_bound, same_candidate, expected,
):

    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply.json"))
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    preset, issues, _ = compile_preset_from_crossover_preview(topology, preview)
    assert preset is not None, issues
    candidate = _v2_candidate(preset)
    candidate = replace(candidate, analysis={**candidate.analysis, "measurement_status": "unmeasured"})
    banked = publish_authored_candidate(candidate)
    receipt = {
        "evidence_identities": {
            "candidate_fingerprint": candidate.fingerprint if same_candidate else "other",
        },
        "round_axes": {
            "safety": {"evidence": {"boost_over_declared_bound": over_bound}},
        },
    }
    (banked.path.parent / "round_receipt.json").write_text(json.dumps(receipt))
    calls = []

    async def load_config(path):
        calls.append(path)
        return True

    config_path = tmp_path / "active.yml"
    state_path = tmp_path / "baseline.json"
    config_path.write_text("incumbent graph\n")
    state_path.write_text("{}")
    result = await apply_baseline_profile(
        topology, design_draft=draft, crossover_preview=preview,
        measurements=_measurements(topology, tmp_path), measured_candidate=candidate,
        tuning_owner="automatic",
        load_config=load_config, state_path=state_path, config_path=config_path,
        validate=_valid_config,
    )

    assert result["status"] == expected
    if expected == "blocked":
        assert {issue["code"] for issue in result["issues"]} == {"boost_over_declared_bound"}
        assert result["apply"] is None
        assert calls == []
        assert config_path.read_text() == "incumbent graph\n"
        assert state_path.read_text() == "{}"
    else:
        assert len(calls) == 1
