# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the shared active-voice-provider SSOT reader.

These encode the contract that bit us on /system/: the active provider
must be read *fresh from the wizard file* and there is **no fallback
default** — unset means unconfigured, never a guessed "gemini".
"""
from __future__ import annotations

import textwrap

from jasper.voice.catalog import default_model_id
from jasper.voice import provider_state as provider_state_mod
from jasper.env_load import EnvFileState
from jasper.voice.provider_state import (
    read_active_provider_state,
    read_active_model,
    read_active_provider,
    read_active_provider_and_model,
    resolve_active_provider,
)


def _write(tmp_path, body: str) -> str:
    p = tmp_path / "voice_provider.env"
    p.write_text(textwrap.dedent(body))
    return str(p)


def test_reads_configured_provider_and_model(tmp_path):
    path = _write(
        tmp_path,
        """
        JASPER_VOICE_PROVIDER=openai
        JASPER_OPENAI_MODEL=gpt-realtime-2
        """,
    )
    assert read_active_provider(path) == "openai"
    assert read_active_model("openai", path) == "gpt-realtime-2"
    assert read_active_provider_and_model(path) == ("openai", "gpt-realtime-2")
    state = read_active_provider_state(path)
    assert state.configured
    assert state.status == "configured"
    assert state.provider == "openai"
    assert state.model == "gpt-realtime-2"


def test_unset_provider_has_no_default(tmp_path):
    # The whole point: unset == unconfigured, NEVER a guessed provider.
    path = _write(tmp_path, "# nothing configured yet\n")
    assert read_active_provider(path) == ""
    assert read_active_provider_and_model(path) == ("", None)
    assert read_active_provider_state(path).status == "unset"


def test_invalid_provider_value_rejected(tmp_path):
    path = _write(tmp_path, "JASPER_VOICE_PROVIDER=bogus\n")
    assert read_active_provider(path) == ""
    assert read_active_provider_and_model(path) == ("", None)
    state = read_active_provider_state(path)
    assert state.status == "invalid"
    assert state.raw_provider == "bogus"


def test_missing_file_is_unconfigured(tmp_path):
    path = str(tmp_path / "does-not-exist.env")
    assert read_active_provider(path) == ""
    assert read_active_provider_and_model(path) == ("", None)
    assert read_active_provider_state(path).status == "missing"


def test_unreadable_file_is_not_reported_as_plain_unset(monkeypatch, tmp_path):
    path = tmp_path / "voice_provider.env"
    path.write_text("JASPER_VOICE_PROVIDER=gemini\n")

    monkeypatch.setattr(
        provider_state_mod,
        "read_env_file_state",
        lambda p: EnvFileState(p, {}, "unreadable", "PermissionError: blocked"),
    )

    state = read_active_provider_state(str(path))
    assert state.status == "unreadable"
    assert state.provider == ""
    assert state.model is None
    assert state.detail == "PermissionError: blocked"


def test_model_falls_back_to_catalog_default(tmp_path):
    # Provider set but model not pinned → catalog default for that
    # provider (matches what jasper-voice resolves).
    path = _write(tmp_path, "JASPER_VOICE_PROVIDER=gemini\n")
    provider, model = read_active_provider_and_model(path)
    assert provider == "gemini"
    assert model == default_model_id("gemini")


def test_read_active_model_unknown_provider_is_none(tmp_path):
    path = _write(tmp_path, "JASPER_VOICE_PROVIDER=gemini\n")
    assert read_active_model("not-a-provider", path) is None


def test_read_active_model_missing_file_is_none(tmp_path):
    path = str(tmp_path / "does-not-exist.env")
    assert read_active_model("gemini", path) is None


def test_resolve_is_pure_validates_and_strips():
    assert resolve_active_provider({"JASPER_VOICE_PROVIDER": "grok"}) == "grok"
    assert resolve_active_provider({"JASPER_VOICE_PROVIDER": " openai "}) == "openai"
    assert resolve_active_provider({}) == ""
    assert resolve_active_provider({"JASPER_VOICE_PROVIDER": ""}) == ""
    assert resolve_active_provider({"JASPER_VOICE_PROVIDER": "x"}) == ""
