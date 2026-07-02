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
    COUPLING_SHM_RING,
    COUPLING_TRANSPORT_PIPE,
    DEFAULT_FANIN_CAMILLA_PIPE,
    DEFAULT_FANIN_RING_PATH,
    DEFAULT_FANIN_RING_SLOTS,
    PIPE_PATH_ENV_VAR,
    PIPE_WIRE_FORMAT,
    RING_PATH_ENV_VAR,
    RING_SLOTS_ENV_VAR,
    RING_SLOTS_MAX,
    RING_SLOTS_MIN,
    resolve_ring_slots,
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


def test_coupling_shm_ring_token_agrees():
    # Ring A: the Rust normalizer MUST accept the same shm_ring token Python's
    # resolve_coupling accepts, or the daemon and the emitted/armed config
    # disagree on the transport of the SHARED realtime capture.
    text = _config_rs_text()
    assert f'Some("{COUPLING_SHM_RING}") => Coupling::ShmRing' in text, (
        f"Rust coupling parse must map the {COUPLING_SHM_RING!r} token to "
        "Coupling::ShmRing"
    )


def test_shm_ring_env_var_names_and_defaults_agree():
    # The Rust daemon resolves the ring path + slot count from the SAME env var
    # names, with the SAME defaults, that Python fanin_coupling exposes — the
    # n_slots <-> JASPER_FANIN_RING_SLOTS pairing is the drift axis.
    text = _config_rs_text()
    assert f'"{RING_PATH_ENV_VAR}"' in text, (
        f"Rust must read the ring path from {RING_PATH_ENV_VAR}"
    )
    assert f'"{RING_SLOTS_ENV_VAR}"' in text, (
        f"Rust must read the ring slots from {RING_SLOTS_ENV_VAR}"
    )
    assert f'"{DEFAULT_FANIN_RING_PATH}"' in text, (
        f"Rust must default the ring path to {DEFAULT_FANIN_RING_PATH}"
    )
    # The default slot count is a bare integer literal in the env_u32 fallback.
    assert f'"{RING_SLOTS_ENV_VAR}", {DEFAULT_FANIN_RING_SLOTS}' in text, (
        f"Rust must default JASPER_FANIN_RING_SLOTS to {DEFAULT_FANIN_RING_SLOTS}"
    )


def test_shm_ring_slots_out_of_range_fails_loud_on_both_sides():
    # SF-1: the JASPER_FANIN_RING_SLOTS normalizer is a declared must-agree axis.
    # BOTH sides fail loud on a present out-of-range value — no silent clamp,
    # per repo doctrine. Otherwise a future arm script that resolved slots via the
    # Python resolver could write an N-slot ioplug conf.d geometry while the
    # daemon refuses to start on the same env (split-brain, fail-closed but
    # maximally confusing on-Pi). This pins the exact agreed behavior:
    #   unset      -> the same default (8) on both sides
    #   2 and 16   -> accepted on both sides
    #   17 (and other out-of-range) -> rejected on both sides

    # Python side (runs live).
    assert resolve_ring_slots(None) == DEFAULT_FANIN_RING_SLOTS
    assert resolve_ring_slots(str(RING_SLOTS_MIN)) == RING_SLOTS_MIN
    assert resolve_ring_slots(str(RING_SLOTS_MAX)) == RING_SLOTS_MAX
    for bad in (RING_SLOTS_MAX + 1, RING_SLOTS_MIN - 1, 0, 100):
        with pytest.raises(ValueError):
            resolve_ring_slots(str(bad))

    # Rust side (source pin — the crate does not build on macOS). The daemon
    # bails on the same range with the same bound constants, and its from_env
    # fail-loud is exercised by the Rust unit test in the CI rust job.
    text = _config_rs_text()
    assert f"pub const RING_SLOTS_MIN: u32 = {RING_SLOTS_MIN};" in text, (
        "Rust RING_SLOTS_MIN must match the Python RING_SLOTS_MIN bound"
    )
    assert f"pub const RING_SLOTS_MAX: u32 = {RING_SLOTS_MAX};" in text, (
        "Rust RING_SLOTS_MAX must match the Python RING_SLOTS_MAX bound"
    )
    # The out-of-range guard bails (anyhow::bail!), it does NOT clamp.
    assert "if !(RING_SLOTS_MIN..=RING_SLOTS_MAX).contains(&ring_slots) {" in text, (
        "Rust must range-check JASPER_FANIN_RING_SLOTS against the shared bounds"
    )
    guard = text.split(
        "if !(RING_SLOTS_MIN..=RING_SLOTS_MAX).contains(&ring_slots) {", 1
    )[1].split("}", 1)[0]
    assert "anyhow::bail!" in guard, (
        "Rust out-of-range ring slots must FAIL LOUD (anyhow::bail!), not clamp"
    )
    assert "clamp" not in guard.lower(), "Rust must not silently clamp ring slots"


def test_shm_ring_status_block_emitted_by_rust_state():
    # The Rust STATUS snapshot emits the ring counter block under shm_ring —
    # the /state.transport + ring:{...} contract the doctor/dashboard read.
    text = _state_rs_text()
    assert '"shm_ring"' in text, "Rust STATUS must echo transport shm_ring"
    assert '"ring":{' in text, "Rust STATUS must emit a ring block"
    for field in (
        "path",
        "slots",
        "occupancy",
        "published",
        "full_waits",
        "drops",
        "mirror_frames",
        "mirror_drops",
    ):
        assert f'"{field}"' in text, f"ring block missing {field!r} key"


def test_shm_ring_mixer_publishes_slots_and_keeps_mirror():
    # The mixer's Output::Ring arm publishes period_frames/128 slots and keeps
    # the lossy aloop mirror (write_music_only-shaped, never the pacer).
    text = _mixer_rs_text()
    assert "Output::Ring" in text
    assert "RingWriter" in text
    assert ".publish(" in text
    # The mirror uses the same write_music_only side-tap shape as the multiroom
    # tap, so it can never back-pressure the loop.
    assert "write_music_only(" in text
    # The 128-frame slot is pinned via the shared RING_SLOT_FRAMES constant.
    assert "RING_SLOT_FRAMES" in text


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
