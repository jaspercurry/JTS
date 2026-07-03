# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-language contract for the fan-in -> Camilla transport pipe.

The Rust ``FifoWriter`` writes the fan-in -> Camilla named pipe and the Python
emitter describes it as a CamillaDSP RawFile capture. If the default path, env
names, token, or S32_LE wire width diverge, fan-in writes a pipe nobody reads
or CamillaDSP misreads the byte stream.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jasper.camilla_config_contract import DEFAULT_CAPTURE_FORMAT
from jasper.fanin_coupling import (
    COUPLING_ENV_VAR,
    COUPLING_TRANSPORT_PIPE,
    DEFAULT_FANIN_CAMILLA_PIPE,
    PIPE_PATH_ENV_VAR,
    PIPE_WIRE_FORMAT,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FANIN_CONFIG_RS = _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "config.rs"
_FANIN_FIFO_RS = _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "fifo.rs"
_FANIN_LANE_RESAMPLER_RS = (
    _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "lane_resampler.rs"
)
_FANIN_MIXER_RS = _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "mixer.rs"
_FANIN_STATE_RS = _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "state.rs"


def _config_rs_text() -> str:
    if not _FANIN_CONFIG_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_CONFIG_RS}")
    return _FANIN_CONFIG_RS.read_text(encoding="utf-8")


def _fifo_rs_text() -> str:
    if not _FANIN_FIFO_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_FIFO_RS}")
    return _FANIN_FIFO_RS.read_text(encoding="utf-8")


def _lane_resampler_rs_text() -> str:
    if not _FANIN_LANE_RESAMPLER_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_LANE_RESAMPLER_RS}")
    return _FANIN_LANE_RESAMPLER_RS.read_text(encoding="utf-8")


def _mixer_rs_text() -> str:
    if not _FANIN_MIXER_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_MIXER_RS}")
    return _FANIN_MIXER_RS.read_text(encoding="utf-8")


def _state_rs_text() -> str:
    if not _FANIN_STATE_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_STATE_RS}")
    return _FANIN_STATE_RS.read_text(encoding="utf-8")


def test_default_pipe_path_agrees_between_rust_and_python():
    # The Rust default is a string literal in Config::from_env (env_str fallback).
    text = _config_rs_text()
    assert f'"{DEFAULT_FANIN_CAMILLA_PIPE}"' in text, (
        "Rust jasper-fanin config.rs must default the Camilla pipe to the same "
        f"path Python uses ({DEFAULT_FANIN_CAMILLA_PIPE})"
    )


def test_pipe_path_env_var_name_agrees():
    # Both sides resolve the override from the SAME env var name.
    text = _config_rs_text()
    assert f'"{PIPE_PATH_ENV_VAR}"' in text, (
        f"Rust must read the pipe path override from {PIPE_PATH_ENV_VAR}"
    )


def test_coupling_selector_env_var_name_agrees():
    text = _config_rs_text()
    assert f'"{COUPLING_ENV_VAR}"' in text, (
        f"Rust must read the coupling selector from {COUPLING_ENV_VAR}"
    )


def test_coupling_transport_pipe_token_agrees():
    text = _config_rs_text()
    assert f'Some("{COUPLING_TRANSPORT_PIPE}")' in text, (
        f"Rust coupling parse must accept the {COUPLING_TRANSPORT_PIPE!r} token"
    )
    assert 'Some("fifo") => Coupling::Fifo' not in text


def test_wire_format_is_s32le_on_both_sides():
    # Python declares the File-capture format as S32_LE (== the shared ALSA
    # capture format); the Rust writer widens i16->i32-LE to match. Pin both.
    assert PIPE_WIRE_FORMAT == "S32_LE"
    assert PIPE_WIRE_FORMAT == DEFAULT_CAPTURE_FORMAT
    fifo_text = _fifo_rs_text()
    # The Rust doc + widening function pin the S32_LE contract; the writer never
    # emits any other width. Assert the doc references the shared constant name
    # so a future format change forces a doc/code update on the Rust side too.
    assert "PIPE_WIRE_FORMAT" in fifo_text
    assert "S32_LE" in fifo_text
    # The widening helper is the actual i16->i32-LE promotion (4 bytes/sample).
    assert "widen_i16_to_i32le" in fifo_text
    assert "S32_BYTES: usize = 4" in fifo_text


def test_input_resampler_status_exports_live_lock_state():
    resampler_text = _lane_resampler_rs_text()
    state_text = _state_rs_text()

    assert "pub locked: Arc<AtomicBool>" in resampler_text
    assert "locked_state.store(true, Ordering::Relaxed)" in resampler_text
    assert "locked_state.store(false, Ordering::Relaxed)" in resampler_text
    assert '"locked"' in state_text
    assert "r.locked.load(Ordering::Relaxed)" in state_text


def test_input_resampler_recovery_restarts_capture_pcm():
    text = _mixer_rs_text()
    recovery_start = text.index("fn recover_resampler_input_xrun(")
    recovery_end = text.index("fn read_into_resampler_and_render(", recovery_start)
    recovery_body = text[recovery_start:recovery_end]

    assert ".try_recover(error, true)" in recovery_body
    # `input.pcm` is now `Option<PCM>` (None only on the USB DIRECT lane, which
    # uses recover_direct_xrun instead); the aloop resampler lane binds it and
    # still restarts the capture PCM if a post-recover try_recover left it
    # PREPARED. Assert the state-check + restart on the bound handle.
    assert "pcm.state() != State::Running" in recovery_body
    assert ".start()" in recovery_body
