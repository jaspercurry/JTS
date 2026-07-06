# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""P6 correction-scope advisor vocabulary (schema v2) — bounds + blocklist.

Fixture-driven; no paid calls. Pins the promise that the two new proposal
actions stay bounded to the active strategy caps and that the safety
substrate (prohibited-key blocklist, preference actions) is intact.
"""
from __future__ import annotations

from jasper.calibration_agent import response as R


def _ctx(*, cuts_only=True, max_boost=3.0, max_total_boost=0.0):
    return {
        "advisor_policy": {
            "allowed_actions": [
                {"id": "propose_correction_peq_adjustment", "allowed": True, "reasons": []},
                {"id": "propose_target_move", "allowed": True, "reasons": []},
            ]
        },
        "correction": {
            "strategy_bounds": {
                "f_low_hz": 20.0,
                "f_high_hz": 350.0,
                "max_filters": 5,
                "max_cut_db": -10.0,
                "max_boost_db": max_boost,
                "cuts_only": cuts_only,
                "q_min": 1.0,
                "q_max": 8.0,
                "max_total_boost_db": max_total_boost,
            }
        },
    }


def _resp(action):
    return {
        "artifact_schema_version": R.RESPONSE_SCHEMA_VERSION,
        "kind": "jts_advisor_response",
        "action_plan": [action],
    }


def test_schema_version_is_two():
    assert R.RESPONSE_SCHEMA_VERSION == 2


def test_correction_cut_within_caps_accepted():
    v = R.validate_advisor_response(
        _resp({
            "type": R.ACTION_PROPOSE_CORRECTION_PEQ,
            "correction_peqs": [{"freq_hz": 62.0, "q": 3.0, "gain_db": -4.0}],
            "rationale": "tighter 62 Hz cut",
        }),
        advisor_context=_ctx(),
    )
    assert v["accepted"]
    action = v["validated_action_plan"][0]
    assert action["type"] == R.ACTION_PROPOSE_CORRECTION_PEQ
    # Never execution-ready from validation — the simulate + confirm gate
    # decides that downstream.
    assert action["execution_ready"] is False
    assert action["requires_simulation"] is True
    assert action["requires_user_confirmation"] is True


def test_correction_out_of_band_freq_rejected():
    v = R.validate_advisor_response(
        _resp({
            "type": R.ACTION_PROPOSE_CORRECTION_PEQ,
            "correction_peqs": [{"freq_hz": 5000.0, "q": 3.0, "gain_db": -4.0}],
            "rationale": "x",
        }),
        advisor_context=_ctx(),
    )
    assert not v["accepted"]
    assert any(i["code"] == "freq_hz_out_of_range" for i in v["issues"])


def test_correction_boost_rejected_when_cuts_only():
    v = R.validate_advisor_response(
        _resp({
            "type": R.ACTION_PROPOSE_CORRECTION_PEQ,
            "correction_peqs": [{"freq_hz": 62.0, "q": 3.0, "gain_db": 2.0}],
            "rationale": "x",
        }),
        advisor_context=_ctx(cuts_only=True),
    )
    assert not v["accepted"]
    assert any(i["code"] == "gain_db_out_of_range" for i in v["issues"])


def test_correction_boost_stack_exceeds_headroom_rejected():
    # cuts_only False, per-filter boost allowed to 3 dB, but the summed
    # boost must stay within max_total_boost_db (0 here).
    v = R.validate_advisor_response(
        _resp({
            "type": R.ACTION_PROPOSE_CORRECTION_PEQ,
            "correction_peqs": [
                {"freq_hz": 80.0, "q": 1.5, "gain_db": 2.0},
                {"freq_hz": 120.0, "q": 1.5, "gain_db": 2.0},
            ],
            "rationale": "x",
        }),
        advisor_context=_ctx(cuts_only=False, max_boost=3.0, max_total_boost=0.0),
    )
    assert not v["accepted"]
    assert any(
        i["code"] == "correction_boost_stack_exceeds_headroom" for i in v["issues"]
    )


def test_correction_too_many_filters_rejected():
    peqs = [{"freq_hz": 40.0 + i * 30, "q": 3.0, "gain_db": -3.0} for i in range(7)]
    v = R.validate_advisor_response(
        _resp({
            "type": R.ACTION_PROPOSE_CORRECTION_PEQ,
            "correction_peqs": peqs,
            "rationale": "x",
        }),
        advisor_context=_ctx(),
    )
    assert not v["accepted"]
    assert any(i["code"] == "too_many_correction_peqs" for i in v["issues"])


def test_target_move_named_accepted_with_sentinel_warmth():
    # The strict model schema sends both fields; an unused warmth arrives as
    # 0.0 alongside a named target. target_id wins, no ambiguity error.
    v = R.validate_advisor_response(
        _resp({
            "type": R.ACTION_PROPOSE_TARGET_MOVE,
            "target_id": "warm",
            "warmth": 0.0,
            "rationale": "you asked for warmer",
        }),
        advisor_context=_ctx(),
    )
    assert v["accepted"]
    assert v["validated_action_plan"][0]["target_id"] == "warm"


def test_target_move_warmth_accepted():
    v = R.validate_advisor_response(
        _resp({
            "type": R.ACTION_PROPOSE_TARGET_MOVE,
            "target_id": "",
            "warmth": 1.5,
            "rationale": "x",
        }),
        advisor_context=_ctx(),
    )
    assert v["accepted"]
    assert v["validated_action_plan"][0]["warmth"] == 1.5


def test_target_move_out_of_range_warmth_rejected():
    v = R.validate_advisor_response(
        _resp({
            "type": R.ACTION_PROPOSE_TARGET_MOVE,
            "target_id": "",
            "warmth": 9.0,
            "rationale": "x",
        }),
        advisor_context=_ctx(),
    )
    assert not v["accepted"]
    assert any(i["code"] == "warmth_out_of_range" for i in v["issues"])


def test_target_move_invalid_id_rejected():
    v = R.validate_advisor_response(
        _resp({
            "type": R.ACTION_PROPOSE_TARGET_MOVE,
            "target_id": "boomy",
            "warmth": 0.0,
            "rationale": "x",
        }),
        advisor_context=_ctx(),
    )
    assert not v["accepted"]
    assert any(i["code"] == "target_id_invalid" for i in v["issues"])


def test_prohibited_yaml_still_blocked_on_correction_action():
    # The blocklist is untouched by the vocabulary extension.
    v = R.validate_advisor_response(
        _resp({
            "type": R.ACTION_PROPOSE_CORRECTION_PEQ,
            "correction_peqs": [{"freq_hz": 62.0, "q": 3.0, "gain_db": -4.0}],
            "rationale": "x",
            "yaml": "devices: {}",
        }),
        advisor_context=_ctx(),
    )
    assert not v["accepted"]
    assert any(i["code"] == "prohibited_fields_present" for i in v["issues"])


def test_correction_action_blocked_when_policy_denies():
    ctx = _ctx()
    ctx["advisor_policy"]["allowed_actions"] = [
        {"id": "propose_correction_peq_adjustment", "allowed": False,
         "reasons": ["low confidence"]},
    ]
    v = R.validate_advisor_response(
        _resp({
            "type": R.ACTION_PROPOSE_CORRECTION_PEQ,
            "correction_peqs": [{"freq_hz": 62.0, "q": 3.0, "gain_db": -4.0}],
            "rationale": "x",
        }),
        advisor_context=ctx,
    )
    assert not v["accepted"]
    assert any(i["code"] == "action_not_allowed_by_context" for i in v["issues"])


def test_response_contract_advertises_new_actions():
    contract = R.response_contract()
    types = {a["type"] for a in contract["allowed_action_types"]}
    assert R.ACTION_PROPOSE_CORRECTION_PEQ in types
    assert R.ACTION_PROPOSE_TARGET_MOVE in types
    assert contract["target_move_limits"]["target_ids"] == sorted(
        {"flat", "neutral", "warm", "bright"}
    )
