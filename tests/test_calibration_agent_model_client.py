from __future__ import annotations

import json

import pytest

from jasper.calibration_agent import model_client, prompt, response


def _prompt_package() -> dict:
    return prompt.build_advisor_prompt_package(
        {
            "artifact_schema_version": 1,
            "kind": "jts_advisor_context",
            "advisor_policy": {
                "allowed_actions": [
                    {"id": "explain", "allowed": True, "reasons": []},
                ]
            },
        }
    )


def _advisor_json() -> dict:
    return {
        "artifact_schema_version": response.RESPONSE_SCHEMA_VERSION,
        "kind": "jts_advisor_response",
        "summary": "The evidence is reviewable.",
        "recommended_next_action": "Explain the evidence.",
        "action_plan": [{
            "type": "explain",
            "message": "No side effects are needed.",
            "reason": "",
            "position_hint": "",
            "rationale": "",
            "profile_name": "",
            "profile": {
                "enabled": False,
                "curve_id": "flat",
                "simple_eq": {
                    "sub_bass_db": 0,
                    "bass_db": 0,
                    "mid_db": 0,
                    "presence_db": 0,
                    "treble_db": 0,
                },
                "parametric_bands": [],
            },
        }],
    }


def test_build_openai_request_uses_structured_output_and_no_store():
    payload = model_client.build_openai_request(_prompt_package(), "test-model")

    assert payload["model"] == "test-model"
    assert payload["store"] is False
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["strict"] is True
    assert payload["input"][0]["role"] == "system"
    assert "JTS_ADVISOR_CONTEXT_JSON" in payload["input"][1]["content"]
    assert "JTS_RESPONSE_CONTRACT_JSON" in payload["input"][1]["content"]


def test_resolve_settings_requires_key_and_model():
    with pytest.raises(model_client.AdvisorModelError, match="OPENAI_API_KEY"):
        model_client.resolve_settings(environ={})

    with pytest.raises(model_client.AdvisorModelError, match="advisor-model"):
        model_client.resolve_settings(environ={"OPENAI_API_KEY": "sk-test"})


def test_call_advisor_posts_to_responses_and_extracts_output_text():
    calls: list[dict] = []

    def transport(url: str, headers: dict, body: bytes, timeout: float):
        calls.append({
            "url": url,
            "headers": headers,
            "body": json.loads(body.decode("utf-8")),
            "timeout": timeout,
        })
        return 200, json.dumps({
            "id": "resp_123",
            "status": "completed",
            "output": [{
                "type": "message",
                "content": [{
                    "type": "output_text",
                    "text": json.dumps(_advisor_json()),
                }],
            }],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }).encode()

    result = model_client.call_advisor(
        _prompt_package(),
        environ={
            "OPENAI_API_KEY": "sk-test",
            "JASPER_CALIBRATION_ADVISOR_MODEL": "test-model",
            "OPENAI_BASE_URL": "https://example.test/v1",
        },
        transport=transport,
    )

    assert result["kind"] == "jts_advisor_model_call"
    assert result["provider"] == "openai"
    assert result["model"] == "test-model"
    assert result["response_id"] == "resp_123"
    assert result["advisor_response"]["kind"] == "jts_advisor_response"
    assert result["usage"] == {"input_tokens": 10, "output_tokens": 20}
    assert calls[0]["url"] == "https://example.test/v1/responses"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-test"
    assert calls[0]["body"]["store"] is False


def test_call_advisor_rejects_bad_provider_response():
    def transport(_url: str, _headers: dict, _body: bytes, _timeout: float):
        return 200, json.dumps({
            "id": "resp_123",
            "status": "completed",
            "output_text": "not json",
        }).encode()

    with pytest.raises(model_client.AdvisorModelError, match="valid JSON"):
        model_client.call_advisor(
            _prompt_package(),
            environ={
                "OPENAI_API_KEY": "sk-test",
                "JASPER_CALIBRATION_ADVISOR_MODEL": "test-model",
            },
            transport=transport,
        )
