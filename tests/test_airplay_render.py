from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy" / "bin" / "jasper-apply-airplay-mode"


NO_ASOUNDRC = object()


def _default_asoundrc(output_dmix_buffer_size: int = 4096) -> str:
    """Production-shape fan-in asoundrc fixture.

    The AirPlay latency offset compensates CamillaDSP's target_level
    over chunksize, the fan-in output buffer, plus the output dmix
    (`pcm.jasper_out`). The renderer-side dmix is retired, so no
    renderer-dmix term appears.
    """
    return textwrap.dedent(
        f"""
        pcm.shairport_substream {{
            type plug
            slave.pcm "hw:Loopback,0,1"
        }}

        pcm.jasper_capture {{
            type dsnoop
            ipc_key 7778
            slave {{
                pcm "hw:Loopback,1,7"
                buffer_size 4096
            }}
        }}

        pcm.jasper_out {{
            type dmix
            ipc_key 7777
            slave {{
                pcm "hw:CARD=A,DEV=0"
                rate 48000
                channels 2
                format S16_LE
                period_size 1024
                buffer_size {output_dmix_buffer_size}
            }}
        }}
        """
    ).strip()


def _render(
    tmp_path: Path,
    camilla_yaml: str,
    asoundrc: object = None,
    fanin_output_buffer_frames: int | None = None,
) -> tuple[str, subprocess.CompletedProcess[str]]:
    template = tmp_path / "shairport-sync.conf.template"
    target = tmp_path / "shairport-sync.conf"
    statefile = tmp_path / "statefile.yml"
    camilla = tmp_path / "camilla.yml"
    airplay_env = tmp_path / "airplay_mode.env"
    fanin_env = tmp_path / "fanin.env"
    jasper_env = tmp_path / "jasper.env"

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
    jasper_env.write_text('JASPER_AIRPLAY_DEVICE_NAME="Unit Test"\n')

    if asoundrc is NO_ASOUNDRC:
        asoundrc_path = tmp_path / "no-asound.conf"
    else:
        asoundrc_path = tmp_path / "asound.conf"
        content = asoundrc if isinstance(asoundrc, str) else _default_asoundrc()
        asoundrc_path.write_text(content + "\n")

    env = os.environ.copy()
    env.update(
        {
            "JASPER_SHAIRPORT_TEMPLATE": str(template),
            "JASPER_SHAIRPORT_CONF": str(target),
            "JASPER_AIRPLAY_MODE_ENV": str(airplay_env),
            "JASPER_FANIN_ENV_FILE": str(fanin_env),
            "JASPER_ENV_FILE": str(jasper_env),
            "JASPER_CAMILLA_STATEFILE": str(statefile),
            "JASPER_CAMILLA_DEFAULT_CONFIG": str(camilla),
            "JASPER_ASOUNDRC": str(asoundrc_path),
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
    # target_level=4096, chunksize=1024 -> 3072 Camilla frames.
    # fan-in output=3072; output dmix=4096.
    # Total invisible = 10240 / 48000 = 0.213333 s.
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
    assert "audio_backend_latency_offset_in_seconds = -0.213333;" in rendered
    assert "__AUDIO_BACKEND_LATENCY_OFFSET_SECONDS__" not in rendered
    assert "renderer device 'shairport_substream'" in result.stderr
    assert "latency offset -0.213333s" in result.stderr


def test_airplay_renderer_updates_offset_when_target_level_changes(tmp_path: Path):
    # target=2048 -> 1024 Camilla frames; fan-in output=3072;
    # output dmix=4096. Total = 8192 / 48000 = 0.170667 s.
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

    assert "audio_backend_latency_offset_in_seconds = -0.170667;" in rendered


def test_airplay_renderer_missing_target_level_matches_camilla_default(tmp_path: Path):
    # target_level absent -> defaults to chunksize -> no Camilla extra.
    # fan-in output=3072; output dmix=4096 -> 0.149333 s.
    rendered, _ = _render(
        tmp_path,
        """
        devices:
          samplerate: 48000
          chunksize: 1024
          queuelimit: 4
        """,
    )

    assert "audio_backend_latency_offset_in_seconds = -0.149333;" in rendered


def test_airplay_renderer_falls_back_when_asoundrc_missing(tmp_path: Path):
    # No asoundrc -> output dmix lookup returns empty. Camilla +
    # fan-in output remain: -(6144 / 48000) = -0.128000.
    rendered, _ = _render(
        tmp_path,
        """
        devices:
          samplerate: 48000
          chunksize: 1024
          queuelimit: 4
          target_level: 4096
        """,
        asoundrc=NO_ASOUNDRC,
    )

    assert "audio_backend_latency_offset_in_seconds = -0.128000;" in rendered


def test_airplay_renderer_picks_up_alternate_output_dmix_buffer_size(tmp_path: Path):
    rendered, _ = _render(
        tmp_path,
        """
        devices:
          samplerate: 48000
          chunksize: 1024
          queuelimit: 4
          target_level: 4096
        """,
        asoundrc=_default_asoundrc(output_dmix_buffer_size=1024),
    )

    # CamillaDSP 3072 + fan-in output 3072 + output dmix 1024 = 7168 frames.
    assert "audio_backend_latency_offset_in_seconds = -0.149333;" in rendered


def test_airplay_renderer_skips_output_dmix_when_block_absent(tmp_path: Path):
    asoundrc_without_output = textwrap.dedent(
        """
        pcm.shairport_substream {
            type plug
            slave.pcm "hw:Loopback,0,1"
        }
        """
    ).strip()
    rendered, _ = _render(
        tmp_path,
        """
        devices:
          samplerate: 48000
          chunksize: 1024
          queuelimit: 4
          target_level: 4096
        """,
        asoundrc=asoundrc_without_output,
    )

    assert "audio_backend_latency_offset_in_seconds = -0.128000;" in rendered


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

    # CamillaDSP 1024 + fan-in output 1024 + output dmix 4096 = 6144 frames.
    assert "audio_backend_latency_offset_in_seconds = -0.128000;" in rendered


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
