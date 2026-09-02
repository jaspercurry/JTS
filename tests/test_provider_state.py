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
    barge_in_env_key,
    read_active_provider_state,
    read_active_model,
    read_active_model_from_env_files,
    read_active_provider,
    read_barge_in_enabled,
    resolve_active_provider,
    resolve_barge_in_enabled,
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
    state = read_active_provider_state(path)
    assert state.configured
    assert state.status == "configured"
    assert state.provider == "openai"
    assert state.model == "gpt-realtime-2"


def test_unset_provider_has_no_default(tmp_path):
    # The whole point: unset == unconfigured, NEVER a guessed provider.
    path = _write(tmp_path, "# nothing configured yet\n")
    assert read_active_provider(path) == ""
    state = read_active_provider_state(path)
    assert (state.provider, state.model) == ("", None)
    assert state.status == "unset"


def test_invalid_provider_value_rejected(tmp_path):
    path = _write(tmp_path, "JASPER_VOICE_PROVIDER=bogus\n")
    assert read_active_provider(path) == ""
    state = read_active_provider_state(path)
    assert (state.provider, state.model) == ("", None)
    assert state.status == "invalid"
    assert state.raw_provider == "bogus"


def test_missing_file_is_unconfigured(tmp_path):
    path = str(tmp_path / "does-not-exist.env")
    assert read_active_provider(path) == ""
    state = read_active_provider_state(path)
    assert (state.provider, state.model) == ("", None)
    assert state.status == "missing"


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
    state = read_active_provider_state(path)
    assert state.provider == "gemini"
    assert state.model == default_model_id("gemini")


def test_read_active_model_unknown_provider_is_none(tmp_path):
    path = _write(tmp_path, "JASPER_VOICE_PROVIDER=gemini\n")
    assert read_active_model("not-a-provider", path) is None


def test_read_active_model_missing_file_is_none(tmp_path):
    path = str(tmp_path / "does-not-exist.env")
    assert read_active_model("gemini", path) is None


# --- Model resolution from the merged env files (issue #3133) ----------
#
# The model's documented home is EITHER jasper.env (the operator base
# file — .env.example ships the keys there) or this module's own wizard
# file (scripts/switch-gemini-model.sh writes that one), so unlike
# read_active_model above (PROVIDER_FILE alone) this reads the merged
# env-file set — jasper.env_load.merged_env_files — and must ignore
# os.environ entirely: a calling-shell export outranks both files there
# (load_env_files uses setdefault), which is the drift #3133 closes.


def test_model_from_env_files_prefers_the_later_wizard_file(tmp_path):
    operator_env = tmp_path / "jasper.env"
    operator_env.write_text("JASPER_GEMINI_MODEL=operator-pinned\n")
    wizard_env = tmp_path / "voice_provider.env"
    wizard_env.write_text(
        "JASPER_VOICE_PROVIDER=gemini\nJASPER_GEMINI_MODEL=wizard-pinned\n",
    )

    model = read_active_model_from_env_files(
        "gemini", paths=(str(operator_env), str(wizard_env)),
    )

    assert model == "wizard-pinned"


def test_model_from_env_files_falls_back_to_the_operator_env(tmp_path):
    # The model's documented default home (.env.example) — no wizard file
    # has ever pinned one for this provider.
    operator_env = tmp_path / "jasper.env"
    operator_env.write_text("JASPER_GEMINI_MODEL=operator-pinned\n")
    wizard_env = tmp_path / "voice_provider.env"
    wizard_env.write_text("JASPER_VOICE_PROVIDER=gemini\n")

    model = read_active_model_from_env_files(
        "gemini", paths=(str(operator_env), str(wizard_env)),
    )

    assert model == "operator-pinned"


def test_model_from_env_files_falls_back_to_catalog_default(tmp_path):
    wizard_env = tmp_path / "voice_provider.env"
    wizard_env.write_text("JASPER_VOICE_PROVIDER=gemini\n")

    model = read_active_model_from_env_files(
        "gemini", paths=(str(wizard_env),),
    )

    assert model == default_model_id("gemini")


def test_model_from_env_files_ignores_a_shell_exported_override(
    monkeypatch, tmp_path,
):
    """The whole point of issue #3133: a calling-shell export must not
    win. load_env_files's setdefault is what lets it win for Config; this
    reader never touches os.environ at all, so it can't inherit that."""
    monkeypatch.setenv("JASPER_GEMINI_MODEL", "shell-exported-should-lose")
    wizard_env = tmp_path / "voice_provider.env"
    wizard_env.write_text(
        "JASPER_VOICE_PROVIDER=gemini\nJASPER_GEMINI_MODEL=file-pinned\n",
    )

    model = read_active_model_from_env_files(
        "gemini", paths=(str(wizard_env),),
    )

    assert model == "file-pinned"


def test_model_from_env_files_unknown_provider_is_empty():
    assert read_active_model_from_env_files("not-a-provider", paths=()) == ""


def test_resolve_is_pure_validates_and_strips():
    assert resolve_active_provider({"JASPER_VOICE_PROVIDER": "grok"}) == "grok"
    assert resolve_active_provider({"JASPER_VOICE_PROVIDER": " openai "}) == "openai"
    assert resolve_active_provider({}) == ""
    assert resolve_active_provider({"JASPER_VOICE_PROVIDER": ""}) == ""
    assert resolve_active_provider({"JASPER_VOICE_PROVIDER": "x"}) == ""


# --- Per-provider barge-in enable flag (DEFAULT OFF) -------------------


def test_barge_in_env_key_is_per_provider():
    assert barge_in_env_key("gemini") == "JASPER_BARGE_IN_GEMINI"
    assert barge_in_env_key("openai") == "JASPER_BARGE_IN_OPENAI"
    assert barge_in_env_key("grok") == "JASPER_BARGE_IN_GROK"


def test_barge_in_defaults_off_when_unset(tmp_path):
    # The whole safety contract: absent flag == disabled, never a guess.
    path = _write(tmp_path, "JASPER_VOICE_PROVIDER=gemini\n")
    assert read_barge_in_enabled("gemini", path) is False


def test_barge_in_enabled_when_truthy(tmp_path):
    for raw in ("1", "true", "yes", "on", "enabled", "ON", "True"):
        path = _write(tmp_path, f"JASPER_BARGE_IN_GEMINI={raw}\n")
        assert read_barge_in_enabled("gemini", path) is True, raw


def test_barge_in_disabled_when_falsey(tmp_path):
    for raw in ("0", "false", "no", "off", "disabled", ""):
        path = _write(tmp_path, f"JASPER_BARGE_IN_GEMINI={raw}\n")
        assert read_barge_in_enabled("gemini", path) is False, raw


def test_barge_in_is_per_provider_isolated(tmp_path):
    # Enabling one provider's flag must not enable another's.
    path = _write(tmp_path, "JASPER_BARGE_IN_GEMINI=1\n")
    assert read_barge_in_enabled("gemini", path) is True
    assert read_barge_in_enabled("openai", path) is False
    assert read_barge_in_enabled("grok", path) is False


def test_barge_in_unknown_provider_is_off(tmp_path):
    path = _write(tmp_path, "JASPER_BARGE_IN_BOGUS=1\n")
    assert read_barge_in_enabled("bogus", path) is False


def test_barge_in_missing_file_is_off(tmp_path):
    path = str(tmp_path / "does-not-exist.env")
    assert read_barge_in_enabled("gemini", path) is False


def test_resolve_barge_in_is_pure():
    assert resolve_barge_in_enabled("gemini", {"JASPER_BARGE_IN_GEMINI": "1"}) is True
    assert resolve_barge_in_enabled("gemini", {"JASPER_BARGE_IN_GEMINI": " on "}) is True
    assert resolve_barge_in_enabled("gemini", {}) is False
    assert resolve_barge_in_enabled("bogus", {"JASPER_BARGE_IN_BOGUS": "1"}) is False
