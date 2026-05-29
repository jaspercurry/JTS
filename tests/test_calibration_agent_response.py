from __future__ import annotations

import json
from pathlib import Path

from jasper.calibration_agent import cli, response, tools

from .correction_bundle_fixtures import write_golden_correction_bundle


def _context(tmp_path: Path) -> dict:
    bundle = tools.load_measurement_bundle(
        bundle_dir=write_golden_correction_bundle(tmp_path),
    )
    return tools.build_intake(bundle)["advisor_context"]


def _valid_response() -> dict:
    return {
        "artifact_schema_version": response.RESPONSE_SCHEMA_VERSION,
        "kind": "jts_advisor_response",
        "summary": "Evidence is good enough for a small preference audition.",
        "recommended_next_action": "Audition the proposed profile before saving.",
        "action_plan": [
            {
                "type": "explain",
                "message": "The measurement is high confidence but not FIR-ready.",
            },
            {
                "type": "propose_preference_eq_audition",
                "rationale": "A mild bass lift is preference EQ, not room correction.",
                "profile": {
                    "enabled": True,
                    "curve_id": "harman",
                    "simple_eq": {
                        "bass_db": 1.5,
                        "mid_db": 0.0,
                        "treble_db": -0.5,
                    },
                    "parametric_bands": [{
                        "enabled": True,
                        "type": "Peaking",
                        "freq_hz": 1400.0,
                        "gain_db": -1.0,
                        "q": 1.2,
                    }],
                },
            },
        ],
    }


def test_validate_advisor_response_accepts_ephemeral_preference_action(
    tmp_path: Path,
):
    result = response.validate_advisor_response(
        _valid_response(),
        advisor_context=_context(tmp_path),
    )

    assert result["accepted"] is True
    assert result["side_effects"] == []
    audition = result["validated_action_plan"][1]
    assert audition["type"] == "propose_preference_eq_audition"
    assert audition["status"] == "ready_for_ephemeral_audition"
    assert audition["side_effect"] == "ephemeral_audio_state"
    assert audition["execution_ready"] is True
    assert audition["requires_user_confirmation"] is False
    assert set(audition["profile"]) == {
        "enabled",
        "curve_id",
        "simple_eq",
        "parametric_bands",
    }
    assert audition["profile"]["curve_id"] == "harman"
    assert audition["headroom_db"] > 0.0


def test_validate_advisor_response_strips_model_profile_identity(
    tmp_path: Path,
):
    raw = _valid_response()
    profile = raw["action_plan"][1]["profile"]
    profile["profile_id"] = "custom_deadbeefcafe"
    profile["profile_name"] = "Model controlled identity"
    profile["updated_at"] = "1970-01-01T00:00:00+00:00"

    result = response.validate_advisor_response(raw, advisor_context=_context(tmp_path))

    assert result["accepted"] is True
    sanitized = result["validated_action_plan"][1]["profile"]
    assert sanitized["curve_id"] == "harman"
    assert "profile_id" not in sanitized
    assert "profile_name" not in sanitized
    assert "updated_at" not in sanitized


def test_validate_advisor_response_rejects_forbidden_model_authority(
    tmp_path: Path,
):
    raw = _valid_response()
    raw["action_plan"][0]["camilladsp_yaml"] = "devices: {}"
    raw["action_plan"][0]["volume_db"] = 6

    result = response.validate_advisor_response(raw, advisor_context=_context(tmp_path))

    assert result["accepted"] is False
    assert result["validated_action_plan"] == []
    issue = result["issues"][0]
    assert issue["code"] == "prohibited_fields_present"
    assert "camilladsp_yaml" in issue["fields"]
    assert "volume_db" in issue["fields"]


def test_validate_advisor_response_rejects_out_of_bounds_peq(tmp_path: Path):
    raw = _valid_response()
    raw["action_plan"][1]["profile"]["parametric_bands"][0]["gain_db"] = 18.0

    result = response.validate_advisor_response(raw, advisor_context=_context(tmp_path))

    assert result["accepted"] is False
    assert {
        issue["code"] for issue in result["issues"]
    } >= {"gain_db_out_of_range"}


def test_validate_advisor_response_accepts_new_simple_bands(tmp_path: Path):
    # The 5-band Simple model added sub_bass_db / presence_db. The advisor
    # may now propose them; they survive validation.
    raw = _valid_response()
    raw["action_plan"][1]["profile"]["simple_eq"]["sub_bass_db"] = 2.0
    raw["action_plan"][1]["profile"]["simple_eq"]["presence_db"] = -1.5

    result = response.validate_advisor_response(raw, advisor_context=_context(tmp_path))

    assert result["accepted"] is True
    simple = result["validated_action_plan"][1]["profile"]["simple_eq"]
    assert simple["sub_bass_db"] == 2.0
    assert simple["presence_db"] == -1.5


def test_validate_advisor_response_range_checks_new_simple_bands(tmp_path: Path):
    # The validator must bound the new bands too, not just bass/mid/treble.
    raw = _valid_response()
    raw["action_plan"][1]["profile"]["simple_eq"]["presence_db"] = 50.0

    result = response.validate_advisor_response(raw, advisor_context=_context(tmp_path))

    assert result["accepted"] is False
    assert {
        issue["code"] for issue in result["issues"]
    } >= {"presence_db_out_of_range"}


def test_validate_advisor_response_marks_persistent_commit_approval_boundary(
    tmp_path: Path,
):
    raw = {
        "artifact_schema_version": response.RESPONSE_SCHEMA_VERSION,
        "kind": "jts_advisor_response",
        "action_plan": [{
            "type": "request_user_approved_preference_commit",
            "rationale": "Save this after the user likes the audition.",
            "profile_name": "AI warm audition",
            "profile": {
                "enabled": True,
                "curve_id": "bk",
                "simple_eq": {"bass_db": 1.0, "mid_db": 0.0, "treble_db": 0.0},
                "parametric_bands": [],
            },
        }],
    }

    advisor_context = _context(tmp_path)
    pending = response.validate_advisor_response(raw, advisor_context=advisor_context)
    confirmed = response.validate_advisor_response(
        raw,
        advisor_context=advisor_context,
        user_confirmed=True,
    )

    assert pending["accepted"] is True
    assert pending["validated_action_plan"][0]["status"] == "awaiting_user_confirmation"
    assert pending["validated_action_plan"][0]["execution_ready"] is False
    assert pending["validated_action_plan"][0]["requires_user_confirmation"] is True
    assert confirmed["validated_action_plan"][0]["status"] == (
        "ready_for_user_approved_commit"
    )
    assert confirmed["validated_action_plan"][0]["execution_ready"] is True
    assert confirmed["validated_action_plan"][0]["user_confirmed"] is True


def test_cli_validate_advisor_response(tmp_path: Path, capsys):
    sessions = tmp_path / "sessions"
    write_golden_correction_bundle(sessions, "abc")
    response_path = tmp_path / "advisor-response.json"
    response_path.write_text(json.dumps(_valid_response()))

    rc = cli.main([
        "abc",
        "--sessions-dir",
        str(sessions),
        "--validate-advisor-response",
        str(response_path),
    ])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["kind"] == "jts_advisor_response_validation"
    assert out["accepted"] is True


def test_cli_validate_advisor_response_returns_nonzero_for_unsafe_response(
    tmp_path: Path,
    capsys,
):
    sessions = tmp_path / "sessions"
    write_golden_correction_bundle(sessions, "abc")
    response_path = tmp_path / "advisor-response.json"
    unsafe = _valid_response()
    unsafe["action_plan"][1]["profile"]["parametric_bands"][0]["freq_hz"] = 5.0
    response_path.write_text(json.dumps(unsafe))

    rc = cli.main([
        "abc",
        "--sessions-dir",
        str(sessions),
        "--validate-advisor-response",
        str(response_path),
    ])

    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["accepted"] is False
