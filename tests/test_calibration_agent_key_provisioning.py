# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""P6 tuning-LLM key/model provisioning — all fixture-driven, no paid calls."""
from __future__ import annotations

from pathlib import Path

from jasper.calibration_agent import key_provisioning as kp


def _write_keys(tmp_path: Path, body: str) -> str:
    path = tmp_path / "voice_keys.env"
    path.write_text(body)
    return str(path)


def test_reads_key_from_compartment_file(tmp_path):
    keys = _write_keys(tmp_path, "OPENAI_API_KEY=sk-from-file\n")
    # No key in env -> falls through to the compartment file.
    assert kp.read_openai_key(environ={}, keys_path=keys) == "sk-from-file"


def test_env_key_wins_over_file(tmp_path):
    keys = _write_keys(tmp_path, "OPENAI_API_KEY=sk-from-file\n")
    assert (
        kp.read_openai_key(environ={"OPENAI_API_KEY": "sk-env"}, keys_path=keys)
        == "sk-env"
    )


def test_missing_file_and_env_reads_empty(tmp_path):
    missing = str(tmp_path / "nope.env")
    assert kp.read_openai_key(environ={}, keys_path=missing) == ""


def test_blank_key_in_file_is_not_available(tmp_path):
    keys = _write_keys(tmp_path, "OPENAI_API_KEY=\n")
    assert kp.read_openai_key(environ={}, keys_path=keys) == ""
    assert kp.tuning_llm_available(environ={}, keys_path=keys) is False


def test_model_default_is_current_gpt_class():
    # Tracks the async-research provider's default so both text surfaces
    # name the same current model. A rename is a config change, not a code
    # edit; the default is never empty.
    model = kp.resolve_tuning_model(environ={})
    assert model
    assert model.startswith("gpt-")


def test_model_override_via_env():
    assert (
        kp.resolve_tuning_model(environ={"JASPER_TUNING_LLM_MODEL": "gpt-x-turbo"})
        == "gpt-x-turbo"
    )


def test_availability_hidden_with_nudge_when_no_key(tmp_path):
    missing = str(tmp_path / "nope.env")
    block = kp.availability(environ={}, keys_path=missing).to_dict()
    assert block["available"] is False
    assert block["provider"] == "openai"
    assert "/voice" in block["nudge"]
    # No model id leaked when unavailable.
    assert "model" not in block


def test_availability_available_when_key_present(tmp_path):
    keys = _write_keys(tmp_path, "OPENAI_API_KEY=sk-x\n")
    block = kp.availability(environ={}, keys_path=keys).to_dict()
    assert block["available"] is True
    assert block["provider"] == "openai"
    assert block["model"].startswith("gpt-")
    # No nudge when available.
    assert "nudge" not in block
