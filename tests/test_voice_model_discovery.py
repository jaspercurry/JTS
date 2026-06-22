# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from jasper.voice import model_discovery


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_openai_discovery_filters_to_realtime_voice_models():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.openai.com/v1/models"
        assert request.headers["Authorization"] == "Bearer sk-test"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "gpt-5.2"},
                    {"id": "gpt-realtime-2"},
                    {"id": "gpt-realtime-new"},
                    {"id": "gpt-realtime-whisper"},
                    {"id": "gpt-realtime-translate"},
                ],
            },
        )

    models = model_discovery.fetch_provider_model_ids(
        "openai",
        "sk-test",
        http=_client(handler),
    )
    assert models == ("gpt-realtime-2", "gpt-realtime-new")


def test_gemini_discovery_filters_to_live_audio_candidates():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "AIza-test"
        assert "key" not in request.url.params
        assert request.url.params["pageSize"] == "1000"
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/gemini-2.5-pro",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/gemini-3.1-flash-live-preview",
                        "supportedGenerationMethods": ["bidiGenerateContent"],
                    },
                    {
                        "name": (
                            "models/"
                            "gemini-2.5-flash-native-audio-preview-12-2025"
                        ),
                        "supportedGenerationMethods": ["generateContent"],
                    },
                ],
            },
        )

    models = model_discovery.fetch_provider_model_ids(
        "gemini",
        "AIza-test",
        http=_client(handler),
    )
    assert models == (
        "gemini-3.1-flash-live-preview",
        "gemini-2.5-flash-native-audio-preview-12-2025",
    )


def test_grok_discovery_filters_to_voice_models():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.x.ai/v1/models"
        assert request.headers["Authorization"] == "Bearer xai-test"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "grok-4.3"},
                    {"id": "grok-voice-latest"},
                    {"id": "grok-voice-think-fast-1.0"},
                ],
            },
        )

    models = model_discovery.fetch_provider_model_ids(
        "grok",
        "xai-test",
        http=_client(handler),
    )
    assert models == ("grok-voice-latest", "grok-voice-think-fast-1.0")


def test_refresh_cache_preserves_previous_models_on_failure(tmp_path: Path):
    cache_path = str(tmp_path / "voice_model_discovery.json")

    ok_client = _client(
        lambda _request: httpx.Response(
            200,
            json={"data": [{"id": "gpt-realtime-new"}]},
        ),
    )
    first = model_discovery.refresh_provider_cache(
        "openai",
        "sk-test",
        path=cache_path,
        http=ok_client,
        now="2026-05-27T10:00:00Z",
    )
    assert first.models == ("gpt-realtime-new",)

    failing_client = _client(lambda _request: httpx.Response(503, text="nope"))
    with pytest.raises(model_discovery.ModelDiscoveryError):
        model_discovery.refresh_provider_cache(
            "openai",
            "sk-test",
            path=cache_path,
            http=failing_client,
            now="2026-05-27T10:05:00Z",
        )

    cached = model_discovery.load_cache(cache_path)["openai"]
    assert cached.models == ("gpt-realtime-new",)
    assert cached.fetched_at == "2026-05-27T10:00:00Z"
    assert cached.last_error == "models endpoint returned HTTP 503"
    assert cached.last_error_at == "2026-05-27T10:05:00Z"
    assert (tmp_path / "voice_model_discovery.json").stat().st_mode & 0o777 == 0o600
