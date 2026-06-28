# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Stage-4b-iv LIVE lean lane-switch in jasper.sound.runtime:
apply_lean_capture_config (carrier-preserved enter) + restore_buffered_config
(always-succeeds leave). The CamillaDSP websocket is faked; --check is MISSING
on the dev host (no binary), so validate is ok_to_apply and the emitters are
exercised hardware-free.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jasper.dsp_apply import DspApplyError
from jasper.sound.camilla_yaml import BASE_CONFIG_PATH, sound_config_path
from jasper.sound.graph_carrier import CarrierCannotHostEq


@pytest.fixture(autouse=True)
def _isolate_dsp_apply_state(tmp_path, monkeypatch):
    # apply_dsp_config writes a last-result record; point it at tmp so the test
    # never touches /var/lib/jasper (PermissionError there is fail-soft but
    # noisy). Fail-soft is unrelated to what we assert here.
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply_state.json"),
    )
from jasper.sound.runtime import (
    LEAN_LIVE_CONFIG_NAME,
    apply_lean_capture_config,
    lean_live_config_path,
    restore_buffered_config,
)


class _FakeCamilla:
    """Tracks the loaded config path; set_config_file_path records the swap."""

    def __init__(self, initial: str | None):
        self.path = initial
        self.loads: list[str] = []

    async def get_config_file_path(self, *, best_effort: bool = False) -> str | None:
        return self.path

    async def set_config_file_path(self, path: str, *, best_effort: bool = False) -> bool:
        self.path = path
        self.loads.append(path)
        return True


def _factory(cam):
    return lambda: cam


# --------------------------------------------------------------------------
# enter-lean: carrier-preserved live load
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_apply_lean_loads_carrier_preserved_lean_config(tmp_path):
    # Loaded config is the JTS flat base -> a base_flat stereo host.
    cam = _FakeCamilla(str(BASE_CONFIG_PATH))
    result = await apply_lean_capture_config(
        profile_path=tmp_path / "noprofile.json",
        config_dir=tmp_path,
        camilla_factory=_factory(cam),
    )
    lean_path = lean_live_config_path(tmp_path)
    assert lean_path.exists()
    yaml = lean_path.read_text()
    # Lean RawFile capture + v4 object resampler + UNCHANGED outputd playback.
    # RawFile, not File — CamillaDSP v4 has no `File` capture variant.
    assert "type: RawFile" in yaml
    assert "type: File" not in yaml
    assert 'filename: "/run/jasper-usbsink/lean.pipe"' in yaml
    assert "type: AsyncSinc" in yaml
    assert "outputd_content_playback" in yaml
    assert "volume_limit: 0.0" in yaml
    # CamillaDSP was actually told to load the lean config.
    assert cam.loads == [str(lean_path)]
    assert result["result"] == "success"


@pytest.mark.asyncio
async def test_apply_lean_custom_fifo_threads_through(tmp_path):
    cam = _FakeCamilla(str(BASE_CONFIG_PATH))
    await apply_lean_capture_config(
        profile_path=tmp_path / "noprofile.json",
        config_dir=tmp_path,
        camilla_factory=_factory(cam),
        capture_pipe_path="/run/custom/lean.pipe",
    )
    yaml = lean_live_config_path(tmp_path).read_text()
    assert 'filename: "/run/custom/lean.pipe"' in yaml


@pytest.mark.asyncio
async def test_apply_lean_refused_on_unknown_graph_fails_loud(tmp_path):
    # CamillaDSP is on a config JTS didn't generate -> unknown carrier refuses,
    # and NOTHING is loaded (fail-loud BEFORE touching audio).
    foreign = tmp_path / "foreign.yml"
    foreign.write_text("devices: {}\n")
    cam = _FakeCamilla(str(foreign))
    with pytest.raises(DspApplyError) as exc:
        await apply_lean_capture_config(
            profile_path=tmp_path / "noprofile.json",
            config_dir=tmp_path,
            camilla_factory=_factory(cam),
        )
    # The carrier refusal is the prepare-phase cause; the apply engine wraps it
    # in a DspApplyError(prepare_failed). Either way, fail-loud BEFORE any load.
    assert isinstance(exc.value.__cause__, CarrierCannotHostEq)
    assert exc.value.state.result == "prepare_failed"
    assert cam.loads == []


# --------------------------------------------------------------------------
# leave-lean: restore + NO-OP fast path
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restore_is_noop_when_not_on_lean(tmp_path):
    # CamillaDSP is on the buffered sound config; restore must NOT churn it.
    buffered = sound_config_path(tmp_path)
    cam = _FakeCamilla(str(buffered))
    result = await restore_buffered_config(
        profile_path=tmp_path / "noprofile.json",
        config_dir=tmp_path,
        camilla_factory=_factory(cam),
    )
    assert result is None
    assert cam.loads == []


@pytest.mark.asyncio
async def test_restore_after_enter_returns_to_buffered(tmp_path):
    # Enter lean from the flat base, then restore -> buffered sound_current.yml.
    cam = _FakeCamilla(str(BASE_CONFIG_PATH))
    await apply_lean_capture_config(
        profile_path=tmp_path / "noprofile.json",
        config_dir=tmp_path,
        camilla_factory=_factory(cam),
    )
    lean_path = lean_live_config_path(tmp_path)
    assert cam.path == str(lean_path)

    result = await restore_buffered_config(
        profile_path=tmp_path / "noprofile.json",
        config_dir=tmp_path,
        camilla_factory=_factory(cam),
    )
    assert result is not None
    buffered = sound_config_path(tmp_path)
    assert cam.path == str(buffered)
    # The buffered config is the default ALSA fan-in capture (NOT a File pipe).
    yaml = buffered.read_text()
    assert "plug:jasper_capture" in yaml
    assert "type: File" not in yaml


@pytest.mark.asyncio
async def test_restore_always_succeeds_even_from_lean_only_graph(tmp_path):
    # The restore-always-succeeds invariant: even when CamillaDSP is on the lean
    # config (which is JTS-generated stereo), restore re-emits the buffered
    # config from saved intent and loads it — never refusing.
    lean_path = lean_live_config_path(tmp_path)
    # Seed a lean config on disk + point camilla at it (as if a prior enter).
    cam = _FakeCamilla(str(BASE_CONFIG_PATH))
    await apply_lean_capture_config(
        profile_path=tmp_path / "noprofile.json",
        config_dir=tmp_path,
        camilla_factory=_factory(cam),
    )
    assert lean_path.exists()
    result = await restore_buffered_config(
        profile_path=tmp_path / "noprofile.json",
        config_dir=tmp_path,
        camilla_factory=_factory(cam),
    )
    assert result is not None
    assert result["result"] == "success"


def test_lean_live_config_name_is_jts_generated():
    # The leave-lean restore resolves the carrier for the lean config; it MUST
    # be recognized as JTS-generated or restore would fail closed to unknown.
    from jasper.sound.camilla_yaml import is_jts_generated_config

    p = Path("/var/lib/camilladsp/configs") / LEAN_LIVE_CONFIG_NAME
    assert is_jts_generated_config(p, config_dir="/var/lib/camilladsp/configs")
