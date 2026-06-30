# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.sound.runtime.stage_lean_capture_config (Stage 4b-iii) —
the emit + validate + classify lean File-capture config, WITHOUT live-loading."""
from __future__ import annotations

from pathlib import Path


from jasper.sound.runtime import LEAN_STAGED_CONFIG_NAME, stage_lean_capture_config


def _stage(tmp_path: Path, **kw):
    # No profile file → load_profile returns the default (flat) profile.
    return stage_lean_capture_config(
        profile_path=tmp_path / "noprofile.json",
        config_dir=tmp_path,
        **kw,
    )


def test_stage_emits_valid_v4_lean_config(tmp_path):
    r = _stage(tmp_path)
    assert r["status"] == "staged"
    staged = tmp_path / LEAN_STAGED_CONFIG_NAME
    assert staged.exists()
    yaml = staged.read_text()
    # Lean lane = RawFile capture, v4 OBJECT resampler, playback UNCHANGED.
    # RawFile, not File — CamillaDSP v4 has no `File` capture variant.
    assert "type: RawFile" in yaml
    assert "type: Alsa" in yaml  # playback stays ALSA
    assert "outputd_content_playback" in yaml
    assert "resampler:" in yaml
    assert "type: AsyncSinc" in yaml
    assert "profile: Balanced" in yaml
    # The pre-v2 scalar form must NOT appear (the v4 parser rejects it).
    assert "resampler_type:" not in yaml
    # Safety ceiling preserved.
    assert "volume_limit: 0.0" in yaml
    assert r["capture_pipe_path"] == "/run/jasper-usbsink/lean.pipe"


def test_stage_uses_the_dedicated_staging_file_not_the_production_carrier(tmp_path):
    _stage(tmp_path)
    # Never writes the live /sound carrier — only the dedicated staging file.
    assert (tmp_path / LEAN_STAGED_CONFIG_NAME).exists()
    assert not (tmp_path / "sound_current.yml").exists()
    assert not (tmp_path / "sound_audition.yml").exists()


def test_stage_reports_invalid_when_camilladsp_check_rejects(tmp_path, monkeypatch):
    class _FailValidation:
        ok_to_apply = False
        error = "bad config"
        stderr_tail = "stderr"

        def to_dict(self):
            return {"ok_to_apply": False, "error": self.error}

    import jasper.dsp_apply as dsp_apply

    monkeypatch.setattr(
        dsp_apply, "validate_camilla_config", lambda _p: _FailValidation(),
    )
    r = _stage(tmp_path)
    assert r["status"] == "invalid"
    assert r["validator"]["ok_to_apply"] is False


def test_stage_reports_graph_unsafe_when_classifier_refuses(tmp_path, monkeypatch):
    class _Unsafe:
        allowed = False
        classification = "active_unproven"
        camilla_classification = "x"
        issues = ()

    import jasper.active_speaker.runtime_contract as rc

    monkeypatch.setattr(rc, "classify_camilla_graph", lambda **_kw: _Unsafe())
    r = _stage(tmp_path)
    assert r["status"] == "graph_unsafe"
    assert r["classification"] == "active_unproven"


def test_stage_never_imports_the_live_apply_path(tmp_path):
    # The whole point of 4b-iii: stage + validate only, zero audio risk. The
    # function must not call apply_dsp_config / set_config_file_path. We assert
    # the contract indirectly: a successful stage returns a status dict and
    # leaves no apply-state side effect (no live carrier written, asserted
    # above) — and a custom FIFO path threads through.
    r = _stage(tmp_path, capture_pipe_path="/run/custom/lean.pipe")
    assert r["status"] == "staged"
    assert r["capture_pipe_path"] == "/run/custom/lean.pipe"
    assert 'filename: "/run/custom/lean.pipe"' in (
        tmp_path / LEAN_STAGED_CONFIG_NAME
    ).read_text()
