# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from jasper.paths import resolve_state_path

ENV_NAME = "JASPER_TEST_STATE_PATH_RESOLVER"
DEFAULT = "/default/path"


@pytest.mark.parametrize(
    ("explicit", "env_name", "env_value", "expected"),
    [
        ("/explicit/path", ENV_NAME, "/env/path", "/explicit/path"),
        (None, ENV_NAME, "/env/path", "/env/path"),
        (None, ENV_NAME, None, DEFAULT),
        ("", ENV_NAME, None, DEFAULT),
        (None, None, "/env/path", DEFAULT),
    ],
    ids=[
        "explicit_wins_over_env",
        "env_wins_over_default",
        "default_when_nothing_set",
        "falsy_explicit_falls_through",
        "no_env_name_skips_lookup",
    ],
)
def test_resolve_state_path_precedence(
    monkeypatch, explicit, env_name, env_value, expected
):
    monkeypatch.delenv(ENV_NAME, raising=False)
    if env_value is not None:
        monkeypatch.setenv(ENV_NAME, env_value)
    assert resolve_state_path(explicit, env_name, DEFAULT) == Path(expected)


def test_resolve_state_path_reads_env_at_call_time(monkeypatch):
    monkeypatch.delenv(ENV_NAME, raising=False)
    assert resolve_state_path(None, ENV_NAME, DEFAULT) == Path(DEFAULT)
    monkeypatch.setenv(ENV_NAME, "/env/path")
    assert resolve_state_path(None, ENV_NAME, DEFAULT) == Path("/env/path")
