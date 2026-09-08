# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Room-advisor behavior using replayed or hand-authored provider fixtures.

No provider calls occur; these tests are not fresh model evaluations.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from jasper.calibration_agent import correction_advisor as ca
from jasper.calibration_agent import response as R

_FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> bytes:
    payload = json.loads((_FIXTURES / name).read_text())
    payload.pop("_comment", None)
    return json.dumps(payload).encode()


def _transport_from_fixture(name: str):
    body = _load_fixture(name)

    def transport(_url, _headers, _body, _timeout):
        return 200, body

    return transport


def _live_check_demo_session():
    """Use the original fixture context, not a second copy."""
    import importlib.util

    path = Path(__file__).parents[1] / "scripts" / "tuning-llm-live-check.py"
    spec = importlib.util.spec_from_file_location("tuning_llm_live_check", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._demo_session()


def _curve(freqs, mags):
    return SimpleNamespace(freqs_hz=freqs.tolist(), magnitude_db=mags.tolist())


def _fake_session(*, state="verified", verify=True):
    freqs = np.geomspace(20, 350, 60)
    measured = 8.0 * np.exp(-((np.log2(freqs / 62.0)) ** 2) / (2 * 0.25 ** 2))
    target = np.zeros_like(freqs)
    predicted = measured - 7.0 * np.exp(
        -((np.log2(freqs / 62.0)) ** 2) / (2 * 0.3 ** 2)
    )
    return SimpleNamespace(
        state=SimpleNamespace(value=state),
        target_choice="flat",
        strategy_choice="balanced",
        current_position=3,
        total_positions=3,
        measured_curve=_curve(freqs, measured),
        target_curve=_curve(freqs, target),
        predicted_curve=_curve(freqs, predicted),
        position1_curve=_curve(freqs, measured),
        peqs=[SimpleNamespace(freq_hz=62.0, q=3.0, gain_db=-7.0)],
        design_report={
            "dominant_residuals": {
                "peaks": [{"freq_hz": 62.0, "residual_db": 8.1}],
                "nulls": [],
            },
            "band_hz": [20.0, 350.0],
            "predicted": {
                "rms_db": 2.4,
                "max_abs_db": 7.0,
                "filter_count": 1,
                "total_positive_boost_db": 0.0,
            },
            "crossover_region": {
                "corner_hz": 80.0,
                "no_boost_band_hz": [63.5, 100.8],
                "excluded_boosts": [{"freq_hz": 78.0, "gain_db": 2.0}],
            },
        },
        confidence_report={
            "findings": [
                {"code": "uncalibrated_mic", "severity": "warn", "message": "no cal mic"}
            ]
        },
        acceptance={
            "verdict": "accept",
            "overall_rms_delta_db": 2.4,
            "reasons": ["62 Hz within target"],
            "bands": [],
        } if verify else None,
        verify_before_after={
            "delta": {"rms_db": 2.1, "max_db": 6.5},
            "band_hz": [50.0, 350.0],
        } if verify else None,
    )


# --- packet composition + privacy -------------------------------------

def test_packet_has_no_raw_audio_or_device_identifiers():
    ctx = ca.build_correction_advisor_context(_fake_session())
    blob = json.dumps(ctx)
    # Full magnitude arrays / raw audio / device ids never appear — only
    # downsampled summaries. The privacy-flag key names contain the words
    # "raw_audio"/"device_id" as assertions; assert on the actual data
    # markers instead.
    assert ".wav" not in blob
    assert "deviceId" not in blob
    assert "serial" not in blob
    assert "audio_bytes" not in blob
    # The full magnitude array is NOT in the packet (summaries only).
    assert "magnitude_db" not in blob
    assert ctx["privacy"]["raw_audio_excluded"] is True


def test_packet_carries_server_computed_evidence():
    ctx = ca.build_correction_advisor_context(_fake_session())
    assert ctx["acceptance"]["verdict"] == "accept"
    assert ctx["detected_modes"]["peaks"][0]["freq_hz"] == 62.0
    assert ctx["correction"]["crossover_region"]["corner_hz"] == 80.0
    assert ctx["correction"]["strategy_bounds"]["strategy_id"] == "balanced"
    # Residual is downsampled to <= 9 quantized points.
    rs = ctx["curves"]["residual_summary"]
    assert rs["available"] is True
    assert len(rs["sample_points"]) <= 9


@pytest.mark.parametrize(("text", "missing"), [
    ("An 8.1 dB peak near 62 Hz.", []),
    ("A peak at 95.5 Hz and null at 210 Hz.", [95.5, 210.0]),
    ("3 positions and 1 filter.", []),
    ("A 25 dB peak at 40 Hz.", [25.0, 40.0]),
    ("An 18Hz rumble.", [18.0]),
    ("A 62 dB peak at 8.1 Hz.", []),
])
def test_number_overlap_discloses_limits(text, missing):
    result = ca.check_number_provenance(
        text, ca.build_correction_advisor_context(_fake_session()),
    )
    assert result == {
        "kind": "number_overlap",
        "verifies_claims": False,
        "ok": not missing,
        "unverified": missing,
    }


def test_narration_text_is_clamped_to_text_limit():
    """The narration is assembled from the UNvalidated model response, so
    the validator's per-field bound doesn't apply — the advisor clamps it
    server-side before it enters the payload."""
    from jasper.calibration_agent import response as R

    huge = "x" * (3 * R.TEXT_LIMIT_CHARS)
    narration = ca._narration_text({
        "summary": huge,
        "action_plan": [{"type": "explain", "message": huge}],
    })
    assert len(narration) <= R.TEXT_LIMIT_CHARS
    assert narration.endswith("…")


# --- interpret (read-only) --------------------------------------------

def test_interpret_uses_live_fixture_and_passes_provenance():
    sess = _live_check_demo_session()
    out = ca.interpret(
        sess,
        environ={"OPENAI_API_KEY": "sk-x"},
        transport=_transport_from_fixture("tuning_llm_interpret_response.json"),
    )
    assert out["kind"] == ca.INTERPRET_KIND
    assert out["validation_accepted"] is True
    assert out["provenance"]["ok"] is True
    assert "62" in out["explanation"]
    # Token counts vary per capture — pin the shape, not the number.
    assert out["usage"]["input_tokens"] > 0
    assert out["usage"]["output_tokens"] > 0


def test_interpret_without_key_raises_advisor_error():
    from jasper.calibration_agent import model_client

    sess = _fake_session()
    with pytest.raises(model_client.AdvisorModelError):
        ca.interpret(sess, environ={}, transport=lambda *a: (200, b"{}"))


# --- propose (confirm-gated) ------------------------------------------

def test_propose_live_fixture_simulates_and_marks_applicable():
    # The LIVE-captured propose fixture against the session it was
    # captured from. The harness refuses to save a capture without a
    # room_correction proposal, so this path — validate → simulate →
    # confirm-gate — is pinned against REAL model output.
    sess = _live_check_demo_session()
    out = ca.propose(
        sess,
        environ={"OPENAI_API_KEY": "sk-x"},
        transport=_transport_from_fixture("tuning_llm_propose_response.json"),
    )
    assert out["kind"] == ca.PROPOSE_KIND
    assert out["validation_accepted"] is True
    kinds = {p["kind"] for p in out["proposals"]}
    assert "room_correction" in kinds

    corr = next(p for p in out["proposals"] if p["kind"] == "room_correction")
    # A bounded extra cut on the remaining 62 Hz residual simulates clean
    # on this room and stays confirm-gated.
    assert corr["applicable"] is True
    assert corr["simulation"]["issues"] == []
    assert corr["requires_user_confirmation"] is True


def test_propose_multikind_fixture_renders_every_kind():
    # Hand-authored REAL-SHAPE fixture carrying one proposal of every
    # kind, so the multi-kind rendering/validation path stays pinned
    # regardless of what the live model happened to propose at capture
    # time. Uses _fake_session — the session this fixture was authored
    # against.
    sess = _fake_session()
    out = ca.propose(
        sess,
        environ={"OPENAI_API_KEY": "sk-x"},
        transport=_transport_from_fixture(
            "tuning_llm_propose_multikind_response.json"
        ),
    )
    assert out["kind"] == ca.PROPOSE_KIND
    assert out["validation_accepted"] is True
    kinds = {p["kind"] for p in out["proposals"]}
    assert "room_correction" in kinds
    assert "preference_question" in kinds

    corr = next(p for p in out["proposals"] if p["kind"] == "room_correction")
    # The fixture's -7.5 dB cut at 62 Hz simulates clean on this room.
    assert corr["applicable"] is True
    assert corr["simulation"]["issues"] == []
    assert corr["requires_user_confirmation"] is True

    pref = next(p for p in out["proposals"] if p["kind"] == "preference_question")
    assert pref["target_id"] == "warm"
    # Honest suggestion-only semantics: a target move has NO apply path, so
    # it is never "applicable" and never claims a confirm-then-execute.
    assert pref["applicable"] is False
    assert pref["suggestion_only"] is True
    assert "requires_user_confirmation" not in pref


def test_propose_discloses_a_ringing_correction_without_withholding_apply():
    """Simulated ringing does not veto a valid experiment."""
    sess = _fake_session()
    validation = {
        "kind": "jts_advisor_response_validation",
        "accepted": True,
        "validated_action_plan": [{
            "type": R.ACTION_PROPOSE_CORRECTION_PEQ,
            "correction_peqs": [{"freq_hz": 62.0, "q": 8.0, "gain_db": 3.0}],
            "strategy_bounds": {"max_total_boost_db": 3.0, "f_high_hz": 350.0},
            "rationale": "boost it",
        }],
    }
    reviewed = ca._review_actions(sess, validation)
    assert reviewed[0]["applicable"] is True
    assert any(
        i["code"] == "boost_would_ring"
        for i in reviewed[0]["simulation"]["issues"]
    )
