# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-settings: verb x fixture -> exit code and JSON fields (ADR-0350).

Every case runs the real ``main`` over tmp settings files seeded with two fake
API keys, and checks that neither key reaches stdout, stderr or the log, that
the CLI never touches the keys file, and that a write lands in its file at the
mode the wizard writes it.
"""
from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from jasper import wake_models
from jasper.cli import _refusal, settings
from jasper.env_file import parse_env_mapping
from jasper.voice import model_discovery, provider_state
from jasper.web._common import RestartOutcome

OPENAI_KEY = "sk-test-0123456789abcdef-never-printed"
GEMINI_KEY = "AIza-test-0123456789abcdef-never-printed"
KEYS_FILE_TEXT = f"OPENAI_API_KEY={OPENAI_KEY}\n".encode()
MODES = {"provider": 0o640, "wake": 0o644}
RAN, SKIPPED, REFUSED = RestartOutcome.RAN, RestartOutcome.SKIPPED, RestartOutcome.REFUSED


class Case(NamedTuple):
    argv: list[str]
    code: int
    fields: dict[str, Any]
    writes: dict[str, str] | None = None
    provider: str = "openai"
    restart: RestartOutcome = RAN
    euid: int = 0
    keys: bytes = KEYS_FILE_TEXT
    event: str | None = None


CASES = {
    "show_prints_keys_as_set_or_unset": Case(["show"], 0, {
        "voice.provider": "openai",
        "voice.providers.openai.key": "set",
        "voice.providers.gemini.key": "set",
        "voice.providers.grok.key": "unset",
        "wake.models.alexa": "available",
        "wake.models.jarvis_v2": "not_downloaded",
    }),
    "voice_without_flags_only_reads": Case(
        ["voice"], 0, {"provider": "openai", "model": "gpt-realtime-2"},
    ),
    "voice_provider_writes_and_restarts": Case(
        ["voice", "--provider", "gemini"], 0,
        {"provider": "gemini", "changed": ["provider", "model"], "restart": "ran"},
        writes={"JASPER_VOICE_PROVIDER": "gemini"}, event="voice.save",
    ),
    "voice_model_the_wizard_discovered": Case(
        ["voice", "--model", "gpt-realtime-new"], 0,
        {"provider": "openai", "model": "gpt-realtime-new", "changed": ["model"]},
        writes={"JASPER_OPENAI_MODEL": "gpt-realtime-new"},
    ),
    "unknown_provider_is_refused": Case(
        ["voice", "--provider", "nope"], 1, {"reason": "unknown_provider"},
    ),
    "unknown_model_is_refused": Case(
        ["voice", "--model", "nope"], 1, {"reason": "unknown_model"},
    ),
    "provider_without_a_key_is_refused": Case(
        ["voice", "--provider", "grok"], 1, {"reason": "key_unset"},
    ),
    "unreadable_keys_file_exits_2_before_any_write": Case(
        ["voice", "--provider", "gemini"], 2, {"reason": "unreadable"}, keys=b"\xff\xfe\n",
    ),
    "discovered_model_no_env_file_can_hold_is_refused": Case(
        ["voice", "--model", "gpt-realtime\nbroken"], 1, {"reason": "unusable_value"},
    ),
    "skipped_restart_still_saves": Case(
        ["voice", "--provider", "gemini"], 0,
        {"restart": "skipped", "restart_reason": "bonded_follower"},
        writes={"JASPER_VOICE_PROVIDER": "gemini"}, restart=SKIPPED,
    ),
    "refused_restart_reports_the_save": Case(
        ["voice", "--provider", "gemini"], 1,
        {"reason": "restart_refused", "detail.saved": True, "detail.provider": "gemini"},
        writes={"JASPER_VOICE_PROVIDER": "gemini"}, restart=REFUSED,
    ),
    "wake_model_writes_and_restarts": Case(
        ["wake", "--model", "alexa"], 0, {"model": "alexa", "restart": "ran"},
        writes={"JASPER_WAKE_MODEL": "alexa"}, event="wake.model",
    ),
    "wake_with_no_provider_skips_the_restart": Case(
        ["wake", "--model", "alexa"], 0,
        {"restart": "skipped", "restart_reason": "provider_unset"},
        writes={"JASPER_WAKE_MODEL": "alexa"}, provider="", restart=SKIPPED,
    ),
    "unknown_wake_model_is_refused": Case(
        ["wake", "--model", "nope"], 1, {"reason": "unknown_model"},
    ),
    "undownloaded_wake_model_is_refused": Case(
        ["wake", "--model", "jarvis_v2"], 1, {"reason": "not_downloaded"},
    ),
    "not_root_is_refused": Case(["show"], 1, {"reason": "not_root"}, euid=1000),
}


def _at(document: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        document = document[part]
    return document


def _snapshot(paths: dict[str, Path]) -> dict[str, bytes | None]:
    return {name: path.read_bytes() if path.exists() else None for name, path in paths.items()}


@pytest.mark.parametrize("case", CASES.values(), ids=CASES.keys())
def test_settings_cli(case: Case, tmp_path, monkeypatch, capsys, caplog):
    paths = {name: tmp_path / f"{name}.env" for name in ("provider", "keys", "wake")}
    paths["keys"].write_bytes(case.keys)
    if case.provider:
        paths["provider"].write_text(f"JASPER_VOICE_PROVIDER={case.provider}\n")
    (tmp_path / "jasper.env").write_text(f"GEMINI_API_KEY={GEMINI_KEY}\n")
    (tmp_path / "discovery.json").write_text(json.dumps(
        {"providers": {"openai": {"models": ["gpt-realtime-new", "gpt-realtime\nbroken"]}}},
    ))
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("JASPER_LOG_JSON", "1")
    monkeypatch.setenv("JASPER_ENV_FILE", str(tmp_path / "jasper.env"))
    monkeypatch.setenv("JASPER_VOICE_PROVIDER_FILE", str(paths["provider"]))
    monkeypatch.setattr(provider_state, "KEYS_FILE", str(paths["keys"]))
    monkeypatch.setattr(wake_models, "WAKE_MODEL_FILE", str(paths["wake"]))
    monkeypatch.setattr(model_discovery, "DEFAULT_CACHE_PATH", str(tmp_path / "discovery.json"))
    monkeypatch.setattr(wake_models, "is_available", lambda entry: entry.bundled)
    restarts: list[int] = []
    monkeypatch.setattr(settings, "restart_voice_daemon", lambda: restarts.append(1) or case.restart)
    monkeypatch.setattr(os, "geteuid", lambda: case.euid)
    before = _snapshot(paths)

    code = settings.main(case.argv)

    out, err = capsys.readouterr()
    document = json.loads(out)
    assert code == case.code
    for dotted, expected in case.fields.items():
        assert _at(document, dotted) == expected, dotted
    if code:
        assert document["status"] == _refusal.STATUS_BY_CODE[code]
    for key in (OPENAI_KEY, GEMINI_KEY):
        assert key not in out and key not in err and key not in caplog.text
    if case.event:
        emitted = [
            json.loads(record.getMessage()) for record in caplog.records
            if getattr(record, "jasper_event", None) == case.event
        ]
        assert [(line["event"], line["via"]) for line in emitted] == [(case.event, "cli")]
    after = _snapshot(paths)
    assert after["keys"] == before["keys"]
    if case.writes is None:
        assert after == before
        assert restarts == []
        return
    target = "wake" if case.argv[0] == "wake" else "provider"
    written = after.pop(target)
    assert written is not None
    assert parse_env_mapping(written.decode()).items() >= case.writes.items()
    assert stat.S_IMODE(paths[target].stat().st_mode) == MODES[target]
    assert after == {name: data for name, data in before.items() if name != target}
    assert restarts == [1]
