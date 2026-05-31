from __future__ import annotations

import json
from pathlib import Path

from jasper.calibration_agent import actions, cli, model_client, response, tools

from .correction_bundle_fixtures import write_golden_correction_bundle


def _context(tmp_path: Path) -> dict:
    bundle = tools.load_measurement_bundle(
        bundle_dir=write_golden_correction_bundle(tmp_path),
    )
    return tools.build_intake(bundle)["advisor_context"]


def _advisor_response_with_audition() -> dict:
    return {
        "artifact_schema_version": response.RESPONSE_SCHEMA_VERSION,
        "kind": "jts_advisor_response",
        "action_plan": [
            {
                "type": "explain",
                "message": "The evidence supports a reversible preference audition.",
            },
            {
                "type": "recommend_remeasure",
                "reason": "Collect a quieter repeat if the user wants FIR later.",
            },
            {
                "type": "propose_preference_eq_audition",
                "rationale": "Try a small bass lift as preference EQ.",
                "profile": {
                    "enabled": True,
                    "curve_id": "harman",
                    "simple_eq": {"bass_db": 1.0, "mid_db": 0.0, "treble_db": 0.0},
                    "parametric_bands": [],
                },
            },
        ],
    }


def _validated(tmp_path: Path, raw: dict | None = None) -> dict:
    return response.validate_advisor_response(
        raw or _advisor_response_with_audition(),
        advisor_context=_context(tmp_path),
    )


def test_action_runner_presents_noop_actions_and_keeps_audition_pending(
    tmp_path: Path,
):
    run = actions.run_validated_action_plan(_validated(tmp_path))

    assert run["accepted"] is True
    assert run["status"] == "pending_human"
    assert run["side_effects"] == []
    assert run["human_in_loop"]["required"] is True
    assert run["action_results"][0]["status"] == "presented"
    assert run["action_results"][0]["side_effect"] == "none"
    assert run["action_results"][1]["status"] == "presented"
    audition = run["action_results"][2]
    assert audition["status"] == "ready_for_human_audition"
    assert audition["executed"] is False
    assert audition["pending"] is True
    assert audition["required_executor"] == "audition_executor"
    assert "listener decides" in run["human_in_loop"]["principle"]


def test_action_runner_invokes_audition_executor_only_for_ready_action(
    tmp_path: Path,
):
    calls: list[dict] = []

    def audition_executor(action: dict):
        calls.append(action)
        return {"loaded": True, "config": "sound_audition.yml"}

    run = actions.run_validated_action_plan(
        _validated(tmp_path),
        audition_executor=audition_executor,
    )

    assert run["status"] == "complete"
    assert run["side_effects"] == ["ephemeral_audio_state"]
    audition = run["action_results"][2]
    assert audition["status"] == "audition_executed"
    assert audition["executed"] is True
    assert audition["executor_result"]["loaded"] is True
    assert calls[0]["profile"]["curve_id"] == "harman"


def test_action_runner_does_not_execute_unconfirmed_persistent_commit(
    tmp_path: Path,
):
    raw = {
        "artifact_schema_version": response.RESPONSE_SCHEMA_VERSION,
        "kind": "jts_advisor_response",
        "action_plan": [{
            "type": "request_user_approved_preference_commit",
            "rationale": "Save only after the listener prefers it.",
            "profile_name": "AI audition",
            "profile": {
                "enabled": True,
                "curve_id": "bk",
                "simple_eq": {"bass_db": 1.0, "mid_db": 0.0, "treble_db": 0.0},
                "parametric_bands": [],
            },
        }],
    }
    validation = response.validate_advisor_response(
        raw,
        advisor_context=_context(tmp_path),
        user_confirmed=False,
    )
    calls: list[dict] = []

    run = actions.run_validated_action_plan(
        validation,
        commit_executor=lambda action: calls.append(action),
    )

    assert run["status"] == "pending_human"
    assert calls == []
    result = run["action_results"][0]
    assert result["status"] == "awaiting_human_confirmation"
    assert result["executed"] is False


def test_action_runner_reports_executor_failures(tmp_path: Path):
    def audition_executor(_action: dict):
        raise RuntimeError("camilla unavailable")

    run = actions.run_validated_action_plan(
        _validated(tmp_path),
        audition_executor=audition_executor,
    )

    assert run["accepted"] is False
    assert run["status"] == "rejected"
    assert run["action_results"][2]["status"] == "executor_failed"
    assert run["issues"][0]["code"] == "action_executor_failed"


def test_action_runner_rejects_failed_validation(tmp_path: Path):
    raw = _advisor_response_with_audition()
    raw["action_plan"][2]["profile"]["parametric_bands"] = [{
        "type": "Peaking",
        "freq_hz": 1000.0,
        "gain_db": 18.0,
        "q": 1.0,
    }]

    run = actions.run_validated_action_plan(_validated(tmp_path, raw))

    assert run["accepted"] is False
    assert run["status"] == "rejected"
    assert run["action_results"] == []


def test_cli_run_advisor_actions_is_side_effect_free(tmp_path: Path, capsys):
    sessions = tmp_path / "sessions"
    write_golden_correction_bundle(sessions, "abc")
    response_path = tmp_path / "advisor-response.json"
    response_path.write_text(json.dumps(_advisor_response_with_audition()))

    rc = cli.main([
        "abc",
        "--sessions-dir",
        str(sessions),
        "--run-advisor-actions",
        str(response_path),
    ])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["kind"] == "jts_advisor_action_run"
    assert out["status"] == "pending_human"
    assert out["side_effects"] == []


def test_cli_call_advisor_validates_model_output_without_sound_side_effect(
    tmp_path: Path,
    capsys,
    monkeypatch,
):
    sessions = tmp_path / "sessions"
    write_golden_correction_bundle(sessions, "abc")

    def fake_call_advisor(*_args, **_kwargs):
        return {
            "artifact_schema_version": model_client.MODEL_CALL_SCHEMA_VERSION,
            "kind": model_client.MODEL_CALL_KIND,
            "provider": "openai",
            "model": "test-model",
            "response_id": "resp_test",
            "provider_status": "completed",
            "advisor_response": _advisor_response_with_audition(),
            "usage": {"input_tokens": 1, "output_tokens": 2},
            "side_effects": ["provider_api_call"],
        }

    monkeypatch.setattr(model_client, "call_advisor", fake_call_advisor)

    rc = cli.main([
        "abc",
        "--sessions-dir",
        str(sessions),
        "--call-advisor",
        "--advisor-model",
        "test-model",
    ])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["kind"] == "jts_advisor_model_review"
    assert out["model_call"]["response_id"] == "resp_test"
    assert out["validation"]["accepted"] is True
    assert out["action_run"]["status"] == "pending_human"
    assert out["side_effects"] == ["provider_api_call"]
