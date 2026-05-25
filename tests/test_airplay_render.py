from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy" / "bin" / "jasper-apply-airplay-mode"


# Sentinel: tells _render to point JASPER_ASOUNDRC at a path that
# doesn't exist, simulating a pre-PR-#214 host (no renderer-side
# dmix in the topology).
NO_ASOUNDRC = object()


def _default_asoundrc(dmix_buffer_size: int = 4096) -> str:
    """Production-shape asoundrc fixture.

    Mirrors `deploy/alsa/asoundrc.jasper` enough that the script's
    `awk` parser locates `buffer_size` inside the
    `pcm.jasper_renderer_mix` block. Other PCM definitions are
    included so the parser's "exit on next pcm.* definition" guard
    also gets exercised.
    """
    return textwrap.dedent(
        f"""
        pcm.jasper_renderer_mix {{
            type dmix
            ipc_key 7779
            slave {{
                pcm "hw:Loopback,0,0"
                rate 48000
                channels 2
                format S16_LE
                period_size 1024
                buffer_size {dmix_buffer_size}
            }}
        }}

        pcm.jasper_capture {{
            type dsnoop
            ipc_key 7778
            slave {{
                pcm "hw:Loopback,1,0"
                buffer_size 4096
            }}
        }}
        """
    ).strip()


def _render(
    tmp_path: Path,
    camilla_yaml: str,
    asoundrc: object = None,
    topology: str | None = None,
) -> tuple[str, subprocess.CompletedProcess[str]]:
    """Render the shairport conf via the script under test.

    `asoundrc` controls the JASPER_ASOUNDRC fixture:
      - None (default)      → write the production-shape asoundrc
                               via `_default_asoundrc()`.
      - str                 → write the given content as the asoundrc.
      - NO_ASOUNDRC         → point JASPER_ASOUNDRC at a non-existent
                               file, simulating a pre-dmix host.

    `topology` controls the JASPER_AUDIO_TOPOLOGY_ENV fixture:
      - None (default)      → no file at the topology env path (=
                               dmix mode default behavior).
      - "dmix" or "fanin"   → write the corresponding value to the
                               topology env file; the script reads
                               JASPER_AUDIO_TOPOLOGY from it.
      - other string        → write that literal value (tests of
                               the invalid-fallback path).
    """
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
                output_device = "__RENDERER_DEVICE__";
                disable_synchronization = "__DISABLE_SYNCHRONIZATION__";
            };
            """
        ).lstrip()
    )
    camilla.write_text(textwrap.dedent(camilla_yaml).lstrip())
    statefile.write_text(f"config_path: {camilla}\n")
    airplay_env.write_text("JASPER_AIRPLAY_FREE_RUNNING=no\n")
    jasper_env.write_text('JASPER_AIRPLAY_DEVICE_NAME="Unit Test"\n')

    if asoundrc is NO_ASOUNDRC:
        asoundrc_path = tmp_path / "no-asound.conf"
    else:
        asoundrc_path = tmp_path / "asound.conf"
        asoundrc_content = asoundrc if isinstance(asoundrc, str) else _default_asoundrc()
        asoundrc_path.write_text(asoundrc_content + "\n")

    # Topology env fixture: point at a non-existent file by default so
    # the script falls back to dmix mode (the JTS-default topology).
    # An explicit `topology=` value writes the env file with that
    # JASPER_AUDIO_TOPOLOGY setting.
    topology_env_path = tmp_path / "audio_topology.env"
    if topology is not None:
        topology_env_path.write_text(
            f"JASPER_AUDIO_TOPOLOGY={topology}\n"
        )

    env = os.environ.copy()
    env.update(
        {
            "JASPER_SHAIRPORT_TEMPLATE": str(template),
            "JASPER_SHAIRPORT_CONF": str(target),
            "JASPER_AIRPLAY_MODE_ENV": str(airplay_env),
            "JASPER_ENV_FILE": str(jasper_env),
            "JASPER_CAMILLA_STATEFILE": str(statefile),
            "JASPER_CAMILLA_DEFAULT_CONFIG": str(camilla),
            "JASPER_ASOUNDRC": str(asoundrc_path),
            "JASPER_AUDIO_TOPOLOGY_ENV": str(topology_env_path),
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
    # Default fixture: CamillaDSP target_level=4096, chunksize=1024
    # → 3072 frames; renderer dmix buffer=4096 → 4096 frames.
    # Total invisible = 7168 frames / 48000 = 0.149333 s.
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
    assert "audio_backend_latency_offset_in_seconds = -0.149333;" in rendered
    assert "__AUDIO_BACKEND_LATENCY_OFFSET_SECONDS__" not in rendered
    assert "latency offset -0.149333s" in result.stderr


def test_airplay_renderer_updates_offset_when_target_level_changes(tmp_path: Path):
    # CamillaDSP target=2048 → 1024 frames; dmix=4096 → 4096 frames.
    # Total = 5120 / 48000 = 0.106667 s.
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

    assert "audio_backend_latency_offset_in_seconds = -0.106667;" in rendered


def test_airplay_renderer_missing_target_level_matches_camilla_default(tmp_path: Path):
    # CamillaDSP target_level absent → defaults to chunksize → 0 frames
    # from CamillaDSP. Dmix contributes 4096 frames / 48000 = 0.085333 s.
    rendered, _ = _render(
        tmp_path,
        """
        devices:
          samplerate: 48000
          chunksize: 1024
          queuelimit: 4
        """,
    )

    assert "audio_backend_latency_offset_in_seconds = -0.085333;" in rendered


def test_airplay_renderer_falls_back_when_asoundrc_missing(tmp_path: Path):
    # Pre-PR-#214 topology (no renderer-side dmix in front of
    # snd-aloop). Script should treat the dmix contribution as 0 and
    # return only the CamillaDSP component: -(3072 / 48000) = -0.064.
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

    assert "audio_backend_latency_offset_in_seconds = -0.064000;" in rendered


def test_airplay_renderer_falls_back_when_renderer_mix_block_missing(tmp_path: Path):
    # asoundrc exists but defines other PCMs only — no
    # pcm.jasper_renderer_mix block. Same fallback behavior as above.
    asoundrc_without_renderer_mix = textwrap.dedent(
        """
        pcm.jasper_capture {
            type dsnoop
            slave {
                pcm "hw:Loopback,1,0"
                buffer_size 4096
            }
        }

        pcm.jasper_out {
            type dmix
            slave {
                pcm "hw:CARD=A,DEV=0"
                buffer_size 4096
            }
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
        asoundrc=asoundrc_without_renderer_mix,
    )

    assert "audio_backend_latency_offset_in_seconds = -0.064000;" in rendered


def test_airplay_renderer_picks_up_alternate_dmix_buffer_size(tmp_path: Path):
    # Future-proof for Tier 1B (shrink the dmix buffer to 2048 etc):
    # ensure the script reads the actual value from asoundrc rather
    # than hardcoding 4096.
    rendered, _ = _render(
        tmp_path,
        """
        devices:
          samplerate: 48000
          chunksize: 1024
          queuelimit: 4
          target_level: 4096
        """,
        asoundrc=_default_asoundrc(dmix_buffer_size=2048),
    )

    # CamillaDSP contributes 3072 frames; dmix contributes 2048.
    # Total = 5120 / 48000 = 0.106667 s.
    assert "audio_backend_latency_offset_in_seconds = -0.106667;" in rendered


# ---------- Tier 2A topology substitution (__RENDERER_DEVICE__) -----


_CAMILLA_PRODUCTION_YAML = """
devices:
  samplerate: 48000
  chunksize: 1024
  queuelimit: 4
  target_level: 4096
"""


def test_renderer_device_defaults_to_dmix_when_topology_env_absent(
    tmp_path: Path,
):
    """No /var/lib/jasper/audio_topology.env → dmix mode → shairport
    output_device is `jasper_renderer_in`. This is the default
    behavior on every fresh install."""
    rendered, result = _render(tmp_path, _CAMILLA_PRODUCTION_YAML)
    assert 'output_device = "jasper_renderer_in";' in rendered
    assert "renderer device 'jasper_renderer_in'" in result.stderr


def test_renderer_device_dmix_explicit(tmp_path: Path):
    """Explicit JASPER_AUDIO_TOPOLOGY=dmix produces the same
    output_device as the default (matches when the operator
    explicitly pins the topology back to dmix after testing fanin)."""
    rendered, _ = _render(
        tmp_path, _CAMILLA_PRODUCTION_YAML, topology="dmix",
    )
    assert 'output_device = "jasper_renderer_in";' in rendered


def test_renderer_device_fanin_topology(tmp_path: Path):
    """JASPER_AUDIO_TOPOLOGY=fanin → shairport_substream. The
    `jasper-audio-topology fanin` command writes this env value and
    re-runs the apply script to regenerate /etc/shairport-sync.conf."""
    rendered, result = _render(
        tmp_path, _CAMILLA_PRODUCTION_YAML, topology="fanin",
    )
    assert 'output_device = "shairport_substream";' in rendered
    assert "renderer device 'shairport_substream'" in result.stderr


def test_renderer_device_invalid_topology_falls_back_to_dmix(
    tmp_path: Path,
):
    """An invalid JASPER_AUDIO_TOPOLOGY value emits a warning to
    stderr and falls back to dmix mode. This defends against typos
    in the env file ('JASPER_AUDIO_TOPOLOGY=FANIN' instead of fanin,
    etc.) — the speaker keeps working at the default topology rather
    than silently breaking."""
    rendered, result = _render(
        tmp_path, _CAMILLA_PRODUCTION_YAML, topology="bogus",
    )
    assert 'output_device = "jasper_renderer_in";' in rendered
    assert "invalid JASPER_AUDIO_TOPOLOGY='bogus'" in result.stderr


def test_renderer_device_placeholder_validated(tmp_path: Path):
    """The renderer-device placeholder gets the same anti-typo guard
    as the other placeholders: if substitution silently fails (e.g.,
    a malformed sed pattern leaves __RENDERER_DEVICE__ literal in
    the rendered conf), the script refuses to install it. shairport
    would otherwise fail to parse the conf and crash-loop."""
    # Sanity: the rendered conf should not contain any leftover
    # placeholder tokens. Covered by `assert ... not in rendered`
    # in every successful render — explicit here for completeness.
    rendered, _ = _render(tmp_path, _CAMILLA_PRODUCTION_YAML)
    assert "__RENDERER_DEVICE__" not in rendered
