# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

from tests.status_socket_fixtures import JsonStatusSocket


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy" / "bin" / "jasper-apply-airplay-mode"


NO_OUTPUTD_ENV = object()


def _outputd_env(dac_buffer_frames: int = 3072) -> str:
    """Production-shape outputd env fixture.

    The AirPlay latency offset compensates CamillaDSP's chunk-floor term
    (target_level over chunksize, floored at one chunk), Ring A (fan-in ->
    CamillaDSP) and Ring B (CamillaDSP -> outputd) queue depths, plus
    jasper-outputd's DAC buffer. The old output dmix is retired from the
    outputd path, and the optional rate-match content bridge (once a
    fourth term) was deleted.
    """
    return f"JASPER_OUTPUTD_DAC_BUFFER_FRAMES={dac_buffer_frames}\n"


def _render(
    tmp_path: Path,
    camilla_yaml: str,
    outputd_env: object = None,
    jasper_env_content: str = "",
    grouping_airplay_content: str | None = None,
    fanin_status_socket: Path | None = None,
    outputd_status_socket: Path | None = None,
    ring_alsa_conf_content: str | None = None,
) -> tuple[str, subprocess.CompletedProcess[str]]:
    template = tmp_path / "shairport-sync.conf.template"
    target = tmp_path / "shairport-sync.conf"
    statefile = tmp_path / "statefile.yml"
    camilla = tmp_path / "camilla.yml"
    airplay_env = tmp_path / "airplay_mode.env"
    outputd_env_path = tmp_path / "outputd.env"
    jasper_env_path = tmp_path / "jasper.env"
    speaker_env = tmp_path / "speaker_name.env"
    grouping_airplay_env = tmp_path / "grouping-airplay.env"
    ring_alsa_conf = tmp_path / "60-jts-ring.conf"

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
    jasper_env_path.write_text(jasper_env_content)
    speaker_env.write_text('JASPER_SPEAKER_NAME="Unit Test"\n')

    # Bonded-leader AirPlay offset delta. None = no file (solo/follower —
    # the common case), so the offset stays byte-identical to before.
    if grouping_airplay_content is not None:
        grouping_airplay_env.write_text(grouping_airplay_content)
    else:
        grouping_airplay_env = tmp_path / "no-grouping-airplay.env"

    # Absent by default (a nonexistent sentinel path): ring geometry falls
    # back to the script's DEFAULT_RING_A/B_LATENCY_FRAMES, which mirror the
    # shipped 60-jts-ring.conf values (256 / 128) — see
    # test_airplay_renderer_ring_conf_geometry_propagates for the parsed path.
    if ring_alsa_conf_content is not None:
        ring_alsa_conf.write_text(ring_alsa_conf_content)
    else:
        ring_alsa_conf = tmp_path / "no-ring-conf.conf"

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
            "JASPER_OUTPUTD_ENV_FILE": str(outputd_env_path),
            "JASPER_ENV_FILE": str(jasper_env_path),
            "JASPER_SPEAKER_NAME_FILE": str(speaker_env),
            "JASPER_CAMILLA_STATEFILE": str(statefile),
            "JASPER_CAMILLA_DEFAULT_CONFIG": str(camilla),
            "JASPER_RING_ALSA_CONF": str(ring_alsa_conf),
            "JASPER_GROUPING_AIRPLAY_ENV_FILE": str(grouping_airplay_env),
            "JASPER_FANIN_STATUS_SOCKET": str(
                fanin_status_socket or tmp_path / "no-fanin-status.sock"
            ),
            "JASPER_OUTPUTD_STATUS_SOCKET": str(
                outputd_status_socket or tmp_path / "no-outputd-status.sock"
            ),
            # Hermetic: never read the HOST's renderer-lane map — on an
            # armed box these tests would otherwise render the ring device.
            # The armed/disarmed device behavior itself is pinned end to end
            # in tests/test_renderer_ring_lanes.py's conf-renderer facts.
            "JASPER_RENDERER_LANES_ENV": str(tmp_path / "no-renderer-lanes.env"),
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
    # target_level=4096, chunksize=1024 -> Camilla term 3072+1024=4096.
    # Ring A=256 (no live fanin STATUS, no ring conf -> parsed default);
    # Ring B=128 (same fallback); outputd DAC=3072 (static default).
    # Total invisible = 256 + 4096 + 128 + 3072 = 7552 / 48000 = 0.157333 s.
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
    assert "audio_backend_latency_offset_in_seconds = -0.157333;" in rendered
    assert "__AUDIO_BACKEND_LATENCY_OFFSET_SECONDS__" not in rendered
    assert "renderer device 'shairport_substream'" in result.stderr
    assert "latency offset -0.157333s" in result.stderr


def test_airplay_renderer_updates_offset_when_target_level_changes(tmp_path: Path):
    # target=2048 -> Camilla term 1024+1024=2048. Ring A=256, Ring B=128
    # (both fallback defaults); outputd DAC=3072.
    # Total = 256 + 2048 + 128 + 3072 = 5504 / 48000 = 0.114667 s.
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

    assert "audio_backend_latency_offset_in_seconds = -0.114667;" in rendered


def test_airplay_renderer_missing_target_level_matches_camilla_default(tmp_path: Path):
    # target_level absent -> defaults to chunksize -> Camilla term is just
    # one chunk: 1024. Ring A=256, Ring B=128 (fallback defaults);
    # outputd DAC=3072. Total = 256+1024+128+3072 = 4480 / 48000 = 0.093333 s.
    rendered, _ = _render(
        tmp_path,
        """
        devices:
          samplerate: 48000
          chunksize: 1024
          queuelimit: 4
        """,
    )

    assert "audio_backend_latency_offset_in_seconds = -0.093333;" in rendered


def test_airplay_renderer_falls_back_when_outputd_env_missing(tmp_path: Path):
    # No outputd env -> service default DAC buffer=3072. Same total as
    # test_airplay_renderer_derives_latency_offset_from_camilla_target:
    # 256 + 4096 + 128 + 3072 = 7552 / 48000 = -0.157333.
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

    assert "audio_backend_latency_offset_in_seconds = -0.157333;" in rendered


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

    # Ring A 256 + Camilla 4096 + Ring B 128 + outputd DAC 1024 = 5504 frames.
    assert "audio_backend_latency_offset_in_seconds = -0.114667;" in rendered


def test_airplay_renderer_prefers_live_outputd_dac_delay(tmp_path: Path):
    with JsonStatusSocket(
        {"dac": {"snd_pcm_delay_frames": 1024}},
        name="outputd.sock",
    ) as outputd_status:
        rendered, _ = _render(
            tmp_path,
            """
            devices:
              samplerate: 48000
              chunksize: 1024
              queuelimit: 4
              target_level: 2048
            """,
            outputd_env=_outputd_env(dac_buffer_frames=512),
            outputd_status_socket=outputd_status,
        )

    # Camilla term 1024+1024=2048 + Ring A fallback 256 + Ring B fallback
    # 128 + live outputd DAC 1024 = 3456 / 48000 = 0.072000.
    assert "audio_backend_latency_offset_in_seconds = -0.072000;" in rendered


def test_airplay_renderer_prefers_live_fanin_output_delay(tmp_path: Path):
    with JsonStatusSocket(
        {"output": {"snd_pcm_delay_frames": 1536}},
        name="fanin.sock",
    ) as fanin_status, JsonStatusSocket(
        {"dac": {"snd_pcm_delay_frames": 1024}},
        name="outputd.sock",
    ) as outputd_status:
        rendered, _ = _render(
            tmp_path,
            """
            devices:
              samplerate: 48000
              chunksize: 1024
              queuelimit: 4
              target_level: 2048
            """,
            outputd_env=_outputd_env(dac_buffer_frames=512),
            fanin_status_socket=fanin_status,
            outputd_status_socket=outputd_status,
        )

    # Camilla term 1024+1024=2048 + live fan-in (Ring A) output 1536 +
    # Ring B fallback 128 + live outputd DAC 1024 = 4736 / 48000 = 0.098667.
    assert "audio_backend_latency_offset_in_seconds = -0.098667;" in rendered


def test_airplay_renderer_adds_no_content_bridge_term(tmp_path: Path):
    """The offset is Ring A + CamillaDSP + Ring B + DAC only.

    No content bridge contributes a fifth term. A box that still carries a
    stale bridge env must get the SAME offset as one that does not — otherwise
    the renderer would compensate for a hold that never happens, pushing
    AirPlay audio early.
    """
    camilla = """
        devices:
          samplerate: 48000
          chunksize: 1024
          queuelimit: 4
          target_level: 4096
        """
    # Ring A 256 + Camilla (4096-1024)+1024=4096 + Ring B 128 + outputd DAC
    # 3072 @ 48 kHz = 7552 / 48000.
    expected = "audio_backend_latency_offset_in_seconds = -0.157333;"

    rendered, _ = _render(tmp_path, camilla, outputd_env=_outputd_env())
    assert expected in rendered

    stale, result = _render(
        tmp_path,
        camilla,
        outputd_env=(
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES=3072\n"
            "JASPER_OUTPUTD_CONTENT_BRIDGE=rate_match\n"
            "JASPER_OUTPUTD_CONTENT_BRIDGE_TARGET_FRAMES=4096\n"
        ),
    )
    assert result.returncode == 0, result.stderr
    assert expected in stale, "a stale rate_match env must not move the offset"


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
    # Ring A 256 + Camilla 2048 + Ring B 128 + outputd DAC default 3072
    # = 5504 / 48000.
    assert "audio_backend_latency_offset_in_seconds = -0.114667;" in rendered


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

    assert "audio_backend_latency_offset_in_seconds = -0.157333;" in rendered


_PROD_CAMILLA = """
devices:
  samplerate: 48000
  chunksize: 1024
  queuelimit: 4
  target_level: 2048
"""


def test_airplay_solo_offset_unchanged_without_grouping_env(tmp_path: Path):
    # INVARIANT: a solo/follower speaker (no grouping-airplay.env) gets the
    # byte-identical production offset. The bonded Snapcast term must never
    # leak into a non-bonded speaker's AirPlay timing.
    # Ring A 256 + Camilla 2048 + Ring B 128 + outputd DAC 3072 = 5504/48000.
    rendered, _ = _render(tmp_path, _PROD_CAMILLA)
    assert "audio_backend_latency_offset_in_seconds = -0.114667;" in rendered


def test_airplay_bonded_leader_adds_snapcast_buffer(tmp_path: Path):
    # Active bonded leader: jasper-grouping-reconcile writes the Snapcast
    # playout buffer (400 ms default) as the extra delay. The offset becomes
    # the solo 0.114667 + 0.400 = 0.514667, so the leader's own output lands
    # back on the AirPlay anchor despite the round-trip buffer.
    rendered, result = _render(
        tmp_path,
        _PROD_CAMILLA,
        grouping_airplay_content="JASPER_AIRPLAY_BONDED_EXTRA_DELAY_SEC=0.400000\n",
    )
    assert "audio_backend_latency_offset_in_seconds = -0.514667;" in rendered
    assert "latency offset -0.514667s" in result.stderr


def test_airplay_empty_grouping_env_is_solo(tmp_path: Path):
    # A cleared (empty) grouping-airplay.env — the unbonded state the
    # reconciler writes on unbond — is treated as solo, not an error.
    rendered, _ = _render(tmp_path, _PROD_CAMILLA, grouping_airplay_content="")
    assert "audio_backend_latency_offset_in_seconds = -0.114667;" in rendered


def test_airplay_blank_bonded_delay_value_is_solo(tmp_path: Path):
    rendered, _ = _render(
        tmp_path,
        _PROD_CAMILLA,
        grouping_airplay_content="JASPER_AIRPLAY_BONDED_EXTRA_DELAY_SEC=\n",
    )
    assert "audio_backend_latency_offset_in_seconds = -0.114667;" in rendered


def test_airplay_ignores_garbage_bonded_delay(tmp_path: Path):
    # A non-numeric or negative bonded delay is ignored (0.0): a bad write
    # must never advance playout PAST the anchor (a worse desync than lag).
    for bad in ("not-a-number", "-0.4", "1e3", "0.4 0.4"):
        rendered, _ = _render(
            tmp_path,
            _PROD_CAMILLA,
            grouping_airplay_content=(
                f"JASPER_AIRPLAY_BONDED_EXTRA_DELAY_SEC={bad}\n"
            ),
        )
        assert (
            "audio_backend_latency_offset_in_seconds = -0.114667;" in rendered
        ), f"garbage value {bad!r} should fall back to the solo offset"


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
    # With no renderer-lane map (the harness pins one that does not exist),
    # the device falls back to the shipped snd-aloop lane — the
    # byte-identical fleet default. The armed→ring flip is pinned in
    # tests/test_renderer_ring_lanes.py.
    assert 'output_device = "shairport_substream"' in rendered


def test_airplay_renderer_pins_ring_topology_offset_formula(tmp_path: Path):
    """Pins the corrected ring-topology formula against jts3's real active
    config (deploy/camilladsp/outputd-cutover.yml: chunksize=target_level=
    128, so the Camilla term is exactly one chunk) plus a live outputd DAC
    reading. No live fan-in STATUS (a ring box's SHM path reports none —
    ADR-0100) and no ring conf (falls back to the shipped 60-jts-ring.conf
    defaults): Ring A=256, Ring B=128.
    """
    dac_live = 248
    with JsonStatusSocket(
        {"dac": {"snd_pcm_delay_frames": dac_live}}, name="outputd.sock"
    ) as outputd_status:
        rendered, _ = _render(
            tmp_path,
            """
            devices:
              samplerate: 48000
              chunksize: 128
              queuelimit: 1
              target_level: 128
            """,
            outputd_status_socket=outputd_status,
        )

    expected = -(256 + 128 + 128 + dac_live) / 48000
    assert f"audio_backend_latency_offset_in_seconds = {expected:.6f};" in rendered


def test_airplay_renderer_ring_conf_geometry_propagates(tmp_path: Path):
    """A ring retune (different period_frames/n_slots in the shipped ALSA
    ring conf) must change the derived offset — the geometry is parsed from
    deploy/alsa/conf.d/60-jts-ring.conf at render time, never hardcoded
    blind, so a future retune needs no code change here.
    """
    ring_conf = textwrap.dedent(
        """
        pcm.jts_ring_capture {
            type jts_ring
            path "/dev/shm/jts-ring/program.ring"
            period_frames 256
            n_slots 3
            format S32_LE
        }

        pcm.jts_ring_playback {
            type jts_ring
            path "/dev/shm/jts-ring/content.ring"
            period_frames 64
            n_slots 2
            format S32_LE
        }
        """
    ).lstrip()

    rendered, _ = _render(
        tmp_path,
        """
        devices:
          samplerate: 48000
          chunksize: 1024
          queuelimit: 4
          target_level: 2048
        """,
        ring_alsa_conf_content=ring_conf,
    )

    # Ring A retuned to 256x3=768; Ring B retuned to one slot at 64.
    # Camilla 2048 + outputd DAC default 3072 unchanged.
    # Total = 768 + 2048 + 64 + 3072 = 5952 / 48000 = 0.124000.
    assert "audio_backend_latency_offset_in_seconds = -0.124000;" in rendered
