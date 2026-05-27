from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jasper import audio_quality

SCRIPT = Path(__file__).resolve().parent.parent / "deploy/bin/jasper-render-asound-conf"


def test_default_requested_converter_is_medium(tmp_path):
    assert audio_quality.read_requested_converter(tmp_path / "missing.env") == (
        "samplerate_medium"
    )


def test_write_requested_converter_accepts_human_alias(tmp_path):
    path = tmp_path / "audio_quality.env"
    assert audio_quality.write_requested_converter("best", path) == (
        "samplerate_best"
    )
    assert "JASPER_ALSA_RATE_CONVERTER=samplerate_best" in path.read_text()


def test_read_active_converter_parses_rendered_asound(tmp_path):
    path = tmp_path / "asound.conf"
    path.write_text('defaults.pcm.rate_converter "samplerate_medium"\n')
    assert audio_quality.read_active_converter(path) == "samplerate_medium"


def test_invalid_converter_rejected():
    with pytest.raises(ValueError):
        audio_quality.normalize_converter("linear")
    with pytest.raises(ValueError):
        audio_quality.normalize_converter("")


def test_apply_requested_converter_writes_state_and_runs_renderer(
    monkeypatch,
    tmp_path,
):
    state_path = tmp_path / "audio_quality.env"
    asound_path = tmp_path / "asound.conf"
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        render_state_path = kwargs["env"]["JASPER_AUDIO_QUALITY_FILE"]
        assert render_state_path != str(state_path)
        assert "JASPER_ALSA_RATE_CONVERTER=samplerate_best" in (
            Path(render_state_path).read_text()
        )
        assert not state_path.exists()
        asound_path.write_text(
            'defaults.pcm.rate_converter "samplerate_best"\n',
        )
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(audio_quality.subprocess, "run", fake_run)

    result = audio_quality.apply_requested_converter(
        "best",
        state_path=state_path,
        asound_path=asound_path,
        render_command="/tmp/render",
    )

    assert calls == [["/tmp/render"]]
    assert result["converter"] == "samplerate_best"
    assert result["active_converter"] == "samplerate_best"
    assert "JASPER_ALSA_RATE_CONVERTER=samplerate_best" in state_path.read_text()


def test_apply_requested_converter_does_not_persist_on_render_failure(
    monkeypatch,
    tmp_path,
):
    state_path = tmp_path / "audio_quality.env"
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        assert "JASPER_ALSA_RATE_CONVERTER=samplerate_best" in (
            Path(kwargs["env"]["JASPER_AUDIO_QUALITY_FILE"]).read_text()
        )
        raise subprocess.CalledProcessError(64, cmd)

    monkeypatch.setattr(audio_quality.subprocess, "run", fake_run)

    with pytest.raises(subprocess.CalledProcessError):
        audio_quality.apply_requested_converter(
            "best",
            state_path=state_path,
            render_command="/tmp/render",
        )

    assert calls == [["/tmp/render"]]
    assert not state_path.exists()
    assert list(tmp_path.glob("audio_quality.env.render.*")) == []


def test_render_script_accepts_spaced_quoted_env(tmp_path):
    state_path = tmp_path / "audio_quality.env"
    template_path = tmp_path / "asound.template"
    output_path = tmp_path / "asound.conf"
    state_path.write_text('  JASPER_ALSA_RATE_CONVERTER = "samplerate_best"\n')
    template_path.write_text(
        'defaults.pcm.rate_converter "__RATE_CONVERTER__"\n',
    )

    subprocess.run(
        ["bash", str(SCRIPT)],
        check=True,
        env={
            "PATH": "/usr/bin:/bin",
            "JASPER_AUDIO_QUALITY_FILE": str(state_path),
            "JASPER_ASOUND_TEMPLATE": str(template_path),
            "JASPER_ASOUND_CONF": str(output_path),
        },
    )

    assert (
        'defaults.pcm.rate_converter "samplerate_best"'
        in output_path.read_text()
    )


def test_render_script_rejects_empty_env_value(tmp_path):
    state_path = tmp_path / "audio_quality.env"
    template_path = tmp_path / "asound.template"
    output_path = tmp_path / "asound.conf"
    state_path.write_text("JASPER_ALSA_RATE_CONVERTER=   \n")
    template_path.write_text(
        'defaults.pcm.rate_converter "__RATE_CONVERTER__"\n',
    )

    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "JASPER_AUDIO_QUALITY_FILE": str(state_path),
            "JASPER_ASOUND_TEMPLATE": str(template_path),
            "JASPER_ASOUND_CONF": str(output_path),
        },
    )

    assert proc.returncode == 64
    assert "invalid converter" in proc.stderr
    assert not output_path.exists()
