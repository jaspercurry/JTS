# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.web import correction_crossover_v2_state as v2state
from tests.test_correction_crossover_v2_endpoints import (
    _seed_baseline_apply_environment, _bg_run_async, _apply, _FakeApplyCam, _FakeApplyAndVolumeCam,
    _boosting_candidate,
)

import hashlib
import json
from pathlib import Path

from jasper.active_speaker import baseline_profile
from jasper.active_speaker.measurement_emit import compile_tuning_graph, load_tuning_declaration
from jasper.sound.profile import SoundProfile, SimpleEq, save_profile
from jasper.sound.settings import saved_sound_layers

import pytest
import yaml

from tests.test_active_speaker_baseline_profile import (
    _v2_candidate,
)

@pytest.fixture(autouse=True)
def _isolated_v2_state(tmp_path):
    v2state.set_state_path_for_tests(tmp_path / "v2_state.json")
    yield
    v2state.set_state_path_for_tests(None)


def test_apply_ignores_a_legacy_graph_finding(monkeypatch, tmp_path):
    topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SESSIONS_DIR", str(tmp_path / "sessions"))
    declaration = load_tuning_declaration(topology)
    candidate = _v2_candidate(preset)
    graph = hashlib.sha256(compile_tuning_graph(declaration, candidate).encode()).hexdigest()[:16]
    findings = tmp_path / "candidate_graph_findings"
    findings.mkdir()
    (findings / f"{graph}.json").write_text("{")
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(tmp_path / "sound.json"))
    save_profile(SoundProfile(simple_eq=SimpleEq(bass_db=2.0)))
    cam = _FakeApplyCam()
    result = _apply({"expected_candidate_fingerprint": candidate.fingerprint, "candidate": candidate.to_dict()}, _bg_run_async, lambda: cam)

    assert result["status"] == "applied"
    preference_filters, trim_db = saved_sound_layers()
    assert Path(cam.path).read_text() == compile_tuning_graph(declaration, candidate,
        preference_filters=preference_filters, output_trim_db=trim_db)


_DISPLACED_GAIN = "    type: Gain\n    parameters: { gain: -2.5000, inverted: false, mute: false }\n"


@pytest.mark.parametrize(("displaced_text", "displaced_db"), [
    pytest.param(f"filters:\n  active_baseline_headroom:\n{_DISPLACED_GAIN}", 2.5, id="charged"),
    pytest.param(f"filters:\n  as_woofer_gain:\n{_DISPLACED_GAIN}", 0.0, id="no-filter"),
    pytest.param(f"filters:\n  active_baseline_headroom:\n{_DISPLACED_GAIN.replace('-2.5000', 'nan')}", 0.0,
                 id="malformed"),
    pytest.param(None, 0.0, id="no-file"),
])
def test_the_declared_offset_is_the_move_of_the_two_graphs_written_charges(
    monkeypatch, tmp_path, displaced_text, displaced_db,
):
    """#1811 and ADR-0385: the displaced graph's written charge less the new
    graph's. A displaced graph with no readable headroom filter, or no file,
    charged nothing, and the apply goes ahead."""
    _topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    displaced = tmp_path / "displaced.yml"
    if displaced_text is not None:
        displaced.write_text(displaced_text, encoding="utf-8")
    (tmp_path / "baseline_profile.json").write_text(json.dumps({
        "artifact_schema_version": baseline_profile.SCHEMA_VERSION, "kind": baseline_profile.BASELINE_PROFILE_KIND,
        "status": "applied", "config": {"path": str(displaced)},
    }), encoding="utf-8")
    candidate = _boosting_candidate(preset, boost_db=6.0)

    payload = _apply({"expected_candidate_fingerprint": candidate.fingerprint, "candidate": candidate.to_dict()},
                     _bg_run_async, _FakeApplyAndVolumeCam)

    assert payload["status"] == "applied", payload.get("issues")
    new_path = baseline_profile.load_applied_baseline_profile_state()["config"]["path"]
    new_db = -yaml.safe_load(Path(new_path).read_text())["filters"]["active_baseline_headroom"]["parameters"]["gain"]
    assert new_db > 0.0
    assert payload["expected_post_apply_offset_db"] == pytest.approx(displaced_db - new_db, abs=1e-3)
