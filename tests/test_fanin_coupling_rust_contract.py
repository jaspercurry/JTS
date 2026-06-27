# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-language contract: the fan-in→Camilla coupling pipe path, env vars, and
wire format MUST agree between the Rust producer (``rust/jasper-fanin``) and the
Python File-capture consumer (``jasper.fanin_coupling``).

The FIFO is a SHARED resource: the Rust ``FifoWriter`` writes the named pipe and
the Python-emitted CamillaDSP File capture reads it. If the default path, the
override env-var names, or the S32_LE wire width ever diverge between the two
languages, fan-in would write a pipe nobody reads (silent outage) or CamillaDSP
would misread the byte stream. There is no shared build artifact to enforce
this, so this test parses the Rust source for the literals and pins them against
the Python constants. It is the *only* guard against the two sides drifting.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jasper.camilla_config_contract import DEFAULT_CAPTURE_FORMAT
from jasper.fanin_coupling import (
    COUPLING_ENV_VAR,
    COUPLING_FIFO,
    DEFAULT_FANIN_CAMILLA_FIFO,
    FIFO_PATH_ENV_VAR,
    FIFO_WIRE_FORMAT,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FANIN_CONFIG_RS = _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "config.rs"
_FANIN_FIFO_RS = _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "fifo.rs"


def _config_rs_text() -> str:
    if not _FANIN_CONFIG_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_CONFIG_RS}")
    return _FANIN_CONFIG_RS.read_text(encoding="utf-8")


def _fifo_rs_text() -> str:
    if not _FANIN_FIFO_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_FIFO_RS}")
    return _FANIN_FIFO_RS.read_text(encoding="utf-8")


def test_default_pipe_path_agrees_between_rust_and_python():
    # The Rust default is a string literal in Config::from_env (env_str fallback).
    text = _config_rs_text()
    assert f'"{DEFAULT_FANIN_CAMILLA_FIFO}"' in text, (
        "Rust jasper-fanin config.rs must default the camilla FIFO to the same "
        f"path Python uses ({DEFAULT_FANIN_CAMILLA_FIFO})"
    )


def test_pipe_path_env_var_name_agrees():
    # Both sides resolve the override from the SAME env var name.
    text = _config_rs_text()
    assert f'"{FIFO_PATH_ENV_VAR}"' in text, (
        f"Rust must read the FIFO path override from {FIFO_PATH_ENV_VAR}"
    )


def test_coupling_selector_env_var_name_agrees():
    text = _config_rs_text()
    assert f'"{COUPLING_ENV_VAR}"' in text, (
        f"Rust must read the coupling selector from {COUPLING_ENV_VAR}"
    )


def test_coupling_fifo_token_agrees():
    # Rust's Coupling::from_env_value matches the lowercase "fifo" token; Python's
    # COUPLING_FIFO is the canonical value. They must be the same literal.
    text = _config_rs_text()
    assert f'Some("{COUPLING_FIFO}")' in text, (
        f"Rust coupling parse must accept the {COUPLING_FIFO!r} token"
    )


def test_wire_format_is_s32le_on_both_sides():
    # Python declares the File-capture format as S32_LE (== the shared ALSA
    # capture format); the Rust writer widens i16->i32-LE to match. Pin both.
    assert FIFO_WIRE_FORMAT == "S32_LE"
    assert FIFO_WIRE_FORMAT == DEFAULT_CAPTURE_FORMAT
    fifo_text = _fifo_rs_text()
    # The Rust doc + widening function pin the S32_LE contract; the writer never
    # emits any other width. Assert the doc references the shared constant name
    # so a future format change forces a doc/code update on the Rust side too.
    assert "FIFO_WIRE_FORMAT" in fifo_text
    assert "S32_LE" in fifo_text
    # The widening helper is the actual i16->i32-LE promotion (4 bytes/sample).
    assert "widen_i16_to_i32le" in fifo_text
    assert "S32_BYTES: usize = 4" in fifo_text
