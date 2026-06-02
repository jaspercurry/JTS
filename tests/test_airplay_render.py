from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy" / "bin" / "jasper-apply-airplay-mode"


NO_OUTPUTD_ENV = object()


def _outputd_env(
    dac_buffer_frames: int = 3072,
    *,
    content_bridge: str = "direct",
    content_bridge_target_frames: int | str = 4096,
) -> str:
    """Production-shape outputd env fixture.

    The AirPlay latency offset compensates CamillaDSP's target_level
    over chunksize, the fan-in output buffer, jasper-outputd's optional
    content bridge, plus jasper-outputd's DAC buffer. The old output
    dmix is retired from the outputd path.
    """
    return (
        f"JASPER_OUTPUTD_DAC_BUFFER_FRAMES={dac_buffer_frames}\n"
        f"JASPER_OUTPUTD_CONTENT_BRIDGE={content_bridge}\n"
        "JASPER_OUTPUTD_CONTENT_BRIDGE_TARGET_FRAMES="
        f"{content_bridge_target_frames}\n"
    )


def _render(
    tmp_path: Path,
    camilla_yaml: str,
    outputd_env: object = None,
    fanin_output_buffer_frames: int | None = None,
    jasper_env_content: str = "",
) -> tuple[str, subprocess.CompletedProcess[str]]:
    template = tmp_path / "shairport-sync.conf.template"
    target = tmp_path / "shairport-sync.conf"
    statefile = tmp_path / "statefile.yml"
    camilla = tmp_path / "camilla.yml"
    airplay_env = tmp_path / "airplay_mode.env"
    fanin_env = tmp_path / "fanin.env"
    outputd_env_path = tmp_path / "outputd.env"
    jasper_env_path = tmp_path / "jasper.env"
    speaker_env = tmp_path / "speaker_name.env"

    template.write_text(
        textwrap.dedent(
            """
            general = {
                name = "__AIRPLAY_NAME__";
                audio_backend_latency_offset_in_seconds = __AUDIO_BACKEND_LATENCY_OFFSET_SECONDS__;
            };
            alsa = {
                output_device = "__RENDERER_DEVICE__";
                disable_synchronization = "__DISABLE_SYNCHRONIZATION__";
            };
            """
        ).lstrip()
    )
    camilla.write_text(textwrap.dedent(camilla_yaml).lstrip())
    statefile.write_text(f"config_path: {camilla}\n")
    airplay_env.write_text("JASPER_AIRPLAY_FREE_RUNNING=no\n")
    if fanin_output_buffer_frames is not None:
        fanin_env.write_text(
            f"JASPER_FANIN_OUTPUT_BUFFER_FRAMES={fanin_output_buffer_frames}\n"
        )
    jasper_env_path.write_text(jasper_env_content)
    speaker_env.write_text('JASPER_SPEAKER_NAME="Unit Test"\n')

    if outputd_env is NO_OUTPUTD_ENV:
        outputd_env_path = tmp_path / "no-outputd.env"
    else:
        content = outputd_env if isinstance(outputd_env, str) else _outputd_env()
        outputd_env_path.write_text(content)

    env = os.environ.copy()
    env.update(
        {
            "JASPER_SHAIRPORT_TEMPLATE": str(template),
            "JASPER_SHAIRPORT_CONF": str(target),
            "JASPER_AIRPLAY_MODE_ENV": str(airplay_env),
            "JASPER_FANIN_ENV_FILE": str(fanin_env),
            "JASPER_OUTPUTD_ENV_FILE": str(outputd_env_path),
            "JASPER_ENV_FILE": str(jasper_env_path),
            "JASPER_SPEAKER_NAME_FILE": str(speaker_env),
            "JASPER_CAMILLA_STATEFILE": str(statefile),
            "JASPER_CAMILLA_DEFAULT_CONFIG": str(camilla),
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
    # target_level=4096, chunksize=1024 -> 3072 Camilla frames.
    # fan-in output=3072; outputd DAC=3072.
    # Total invisible = 9216 / 48000 = 0.192000 s.
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
    assert 'output_device = "shairport_substream";' in rendered
    assert "audio_backend_latency_offset_in_seconds = -0.192000;" in rendered
    assert "__AUDIO_BACKEND_LATENCY_OFFSET_SECONDS__" not in rendered
    assert "renderer device 'shairport_substream'" in result.stderr
    assert "latency offset -0.192000s" in result.stderr


def test_airplay_renderer_updates_offset_when_target_level_changes(tmp_path: Path):
    # target=2048 -> 1024 Camilla frames; fan-in output=3072;
    # outputd DAC=3072. Total = 7168 / 48000 = 0.149333 s.
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

    assert "audio_backend_latency_offset_in_seconds = -0.149333;" in rendered


def test_airplay_renderer_missing_target_level_matches_camilla_default(tmp_path: Path):
    # target_level absent -> defaults to chunksize -> no Camilla extra.
    # fan-in output=3072; outputd DAC=3072 -> 0.128000 s.
    rendered, _ = _render(
        tmp_path,
        """
        devices:
          samplerate: 48000
          chunksize: 1024
          queuelimit: 4
        """,
    )

    assert "audio_backend_latency_offset_in_seconds = -0.128000;" in rendered


def test_airplay_renderer_falls_back_when_outputd_env_missing(tmp_path: Path):
    # No outputd env -> service default DAC buffer=3072. Camilla +
    # fan-in output + default outputd DAC = -(9216 / 48000) = -0.192000.
    rendered, _ = _render(
        tmp_path,
        """
        devices:
          samplerate: 48000
          chunksize: 1024
          queuelimit: 4
          target_level: 4096
        """,
        outputd_env=NO_OUTPUTD_ENV,
    )

    assert "audio_backend_latency_offset_in_seconds = -0.192000;" in rendered


def test_airplay_renderer_picks_up_alternate_outputd_dac_buffer_size(tmp_path: Path):
    rendered, _ = _render(
        tmp_path,
        """
        devices:
          samplerate: 48000
          chunksize: 1024
          queuelimit: 4
          target_level: 4096
        """,
        outputd_env=_outputd_env(dac_buffer_frames=1024),
    )

    # CamillaDSP 3072 + fan-in output 3072 + outputd DAC 1024 = 7168 frames.
    assert "audio_backend_latency_offset_in_seconds = -0.149333;" in rendered


def test_airplay_renderer_direct_content_bridge_adds_no_latency(tmp_path: Path):
    rendered, _ = _render(
        tmp_path,
        """
        devices:
          samplerate: 48000
          chunksize: 1024
          queuelimit: 4
          target_level: 4096
        """,
        outputd_env=_outputd_env(content_bridge="direct"),
    )

    assert "audio_backend_latency_offset_in_seconds = -0.192000;" in rendered


def test_airplay_renderer_rate_match_content_bridge_adds_target_fill(tmp_path: Path):
    rendered, _ = _render(
        tmp_path,
        """
        devices:
          samplerate: 48000
          chunksize: 1024
          queuelimit: 4
          target_level: 2048
        """,
        outputd_env=_outputd_env(
            content_bridge="rate_match",
            content_bridge_target_frames=4096,
        ),
    )

    # CamillaDSP 1024 + fan-in output 3072 + bridge 4096 + DAC 3072.
    assert "audio_backend_latency_offset_in_seconds = -0.234667;" in rendered


def test_airplay_renderer_rate_match_bridge_target_is_configurable(tmp_path: Path):
    rendered, _ = _render(
        tmp_path,
        """
        devices:
          samplerate: 48000
          chunksize: 1024
          queuelimit: 4
          target_level: 2048
        """,
        outputd_env=_outputd_env(
            content_bridge="rate_match",
            content_bridge_target_frames=2048,
        ),
    )

    # CamillaDSP 1024 + fan-in output 3072 + bridge 2048 + DAC 3072.
    assert "audio_backend_latency_offset_in_seconds = -0.192000;" in rendered


def test_airplay_renderer_ignores_stale_outputd_knobs_in_jasper_env(tmp_path: Path):
    rendered, _ = _render(
        tmp_path,
        """
        devices:
          samplerate: 48000
          chunksize: 1024
          queuelimit: 4
          target_level: 2048
        """,
        outputd_env="",
        jasper_env_content=(
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES=1024\n"
            "JASPER_OUTPUTD_CONTENT_BRIDGE=rate_match\n"
            "JASPER_OUTPUTD_CONTENT_BRIDGE_TARGET_FRAMES=4096\n"
        ),
    )

    # outputd.service applies packaged outputd defaults after /etc/jasper,
    # so the renderer must not let stale /etc outputd knobs add a bridge term.
    assert "audio_backend_latency_offset_in_seconds = -0.149333;" in rendered


def test_airplay_renderer_warns_on_unknown_bridge_mode(tmp_path: Path):
    rendered, result = _render(
        tmp_path,
        """
        devices:
          samplerate: 48000
          chunksize: 1024
          queuelimit: 4
          target_level: 2048
        """,
        outputd_env="JASPER_OUTPUTD_CONTENT_BRIDGE=pipewire\n",
    )

    assert "audio_backend_latency_offset_in_seconds = -0.149333;" in rendered
    assert "invalid JASPER_OUTPUTD_CONTENT_BRIDGE" in result.stderr


def test_airplay_renderer_falls_back_on_invalid_outputd_dac_buffer(tmp_path: Path):
    rendered, _ = _render(
        tmp_path,
        """
        devices:
          samplerate: 48000
          chunksize: 1024
          queuelimit: 4
          target_level: 4096
        """,
        outputd_env='JASPER_OUTPUTD_DAC_BUFFER_FRAMES="not-a-number"\n',
    )

    assert "audio_backend_latency_offset_in_seconds = -0.192000;" in rendered


def test_airplay_renderer_reads_fanin_output_buffer_from_env_file(tmp_path: Path):
    rendered, _ = _render(
        tmp_path,
        """
        devices:
          samplerate: 48000
          chunksize: 1024
          queuelimit: 4
          target_level: 2048
        """,
        fanin_output_buffer_frames=1024,
    )

    # CamillaDSP 1024 + fan-in output 1024 + outputd DAC 3072 = 5120 frames.
    assert "audio_backend_latency_offset_in_seconds = -0.106667;" in rendered


def test_renderer_device_placeholder_validated(tmp_path: Path):
    rendered, _ = _render(
        tmp_path,
        """
        devices:
          samplerate: 48000
          chunksize: 1024
          queuelimit: 4
          target_level: 4096
        """,
    )
    assert "__RENDERER_DEVICE__" not in rendered
