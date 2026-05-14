from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy" / "bin" / "jasper-apply-airplay-mode"


def _render(tmp_path: Path, camilla_yaml: str) -> tuple[str, subprocess.CompletedProcess[str]]:
    template = tmp_path / "shairport-sync.conf.template"
    target = tmp_path / "shairport-sync.conf"
    statefile = tmp_path / "statefile.yml"
    camilla = tmp_path / "camilla.yml"
    airplay_env = tmp_path / "airplay_mode.env"
    jasper_env = tmp_path / "jasper.env"

    template.write_text(
        textwrap.dedent(
            """
            general = {
                name = "__AIRPLAY_NAME__";
                audio_backend_latency_offset_in_seconds = __AUDIO_BACKEND_LATENCY_OFFSET_SECONDS__;
            };
            alsa = {
                disable_synchronization = "__DISABLE_SYNCHRONIZATION__";
            };
            """
        ).lstrip()
    )
    camilla.write_text(textwrap.dedent(camilla_yaml).lstrip())
    statefile.write_text(f"config_path: {camilla}\n")
    airplay_env.write_text("JASPER_AIRPLAY_FREE_RUNNING=no\n")
    jasper_env.write_text('JASPER_AIRPLAY_DEVICE_NAME="Unit Test"\n')

    env = os.environ.copy()
    env.update(
        {
            "JASPER_SHAIRPORT_TEMPLATE": str(template),
            "JASPER_SHAIRPORT_CONF": str(target),
            "JASPER_AIRPLAY_MODE_ENV": str(airplay_env),
            "JASPER_ENV_FILE": str(jasper_env),
            "JASPER_CAMILLA_STATEFILE": str(statefile),
            "JASPER_CAMILLA_DEFAULT_CONFIG": str(camilla),
            "JASPER_DERIVE_DEVICE_NAME": str(tmp_path / "missing-helper"),
        }
    )

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return target.read_text(), result


def test_airplay_renderer_derives_latency_offset_from_camilla_target(tmp_path: Path):
    rendered, result = _render(
        tmp_path,
        """
        devices:
          samplerate: 48000
          chunksize: 1024
          queuelimit: 4
          target_level: 4096
        """,
    )

    assert 'name = "Unit Test";' in rendered
    assert 'disable_synchronization = "no";' in rendered
    assert "audio_backend_latency_offset_in_seconds = -0.064000;" in rendered
    assert "__AUDIO_BACKEND_LATENCY_OFFSET_SECONDS__" not in rendered
    assert "latency offset -0.064000s" in result.stderr


def test_airplay_renderer_updates_offset_when_target_level_changes(tmp_path: Path):
    rendered, _ = _render(
        tmp_path,
        """
        devices:
          samplerate: 48000
          chunksize: 1024
          queuelimit: 4
          target_level: 2048
        """,
    )

    assert "audio_backend_latency_offset_in_seconds = -0.021333;" in rendered


def test_airplay_renderer_missing_target_level_matches_camilla_default(tmp_path: Path):
    rendered, _ = _render(
        tmp_path,
        """
        devices:
          samplerate: 48000
          chunksize: 1024
          queuelimit: 4
        """,
    )

    assert "audio_backend_latency_offset_in_seconds = 0.000000;" in rendered
