# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

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
    speaker_name_content: str = 'JASPER_SPEAKER_NAME="Unit Test"\n',
    grouping_airplay_content: str | None = None,
    fanin_status_socket: Path | None = None,
    outputd_status_socket: Path | None = None,
    ring_alsa_conf_content: str | None = None,
    extra_env: dict[str, str] | None = None,
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
    speaker_env.write_text(speaker_name_content)

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
    if extra_env:
        env.update(extra_env)

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        # cwd=REPO (not pytest's own cwd): the script's ring-conf-parsed
        # tier shells into `python -` and imports jasper.ring_assets, which
        # resolves via the interpreter's own site-packages in production
        # (the runtime venv) but via the empty-string sys.path[0]-as-cwd
        # rule for the bare-python3 fallback this test environment uses —
        # pinning cwd here keeps that resolution independent of wherever
        # the test suite itself happens to be invoked from.
        cwd=str(REPO),
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return target.read_text(), result


def test_env_file_values_are_data_never_shell(tmp_path: Path):
    """This renderer runs as root from shairport-sync's `ExecStartPre=+`, and
    every file it reads carries operator or wizard text. It must read those
    files, never evaluate them: no command substitution in /etc/jasper's
    jasper.env runs, and a speaker name carrying a space and a `#` reaches
    the rendered conf whole instead of being split at the space and truncated
    at the `#`."""
    marker = tmp_path / "marker"
    rendered, result = _render(
        tmp_path,
        """
        devices:
          samplerate: 48000
          chunksize: 1024
          target_level: 4096
        """,
        jasper_env_content=f"JASPER_SOMETHING=$(touch {marker})\n",
        speaker_name_content="JASPER_SPEAKER_NAME=Kitchen #1 Speaker\n",
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert 'name = "Kitchen #1 Speaker";' in rendered


def test_render_falls_back_to_defaults_when_the_env_lib_is_absent(tmp_path: Path):
    """install_renderers seeds /etc/shairport-sync.conf by running the
    INSTALLED copy of this script, and it runs before install_systemd_units
    publishes deploy/lib/jasper-env-file.sh — so on a fresh install the lib is
    genuinely absent here. Aborting would abort install.sh; every read must
    take its documented fallback instead, which is exactly the seed render.
    shairport-sync's ExecStartPre re-renders with the real lib on next start."""
    rendered, result = _render(
        tmp_path,
        """
        devices:
          samplerate: 48000
          chunksize: 1024
          target_level: 4096
        """,
        speaker_name_content="JASPER_SPEAKER_NAME=Configured Name\n",
        extra_env={"JASPER_ENV_FILE_LIB": str(tmp_path / "absent-lib.sh")},
    )

    assert result.returncode == 0, result.stderr
    assert 'name = "JTS";' in rendered
    assert 'output_device = "shairport_substream";' in rendered
    assert 'disable_synchronization = "no";' in rendered
    assert "audio_backend_latency_offset_in_seconds = -0.157333;" in rendered


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


def test_airplay_default_dac_buffer_matches_the_runtime_plan(tmp_path: Path):
    """The packaged DAC-buffer default has ONE owner and a pin to prove it.

    With neither env layer naming the key, jasper-outputd runs its compile-time
    default and this renderer must charge exactly that number — the AirPlay
    presentation offset is derived from it. The script keeps its own python-free
    constant (this tier is what answers when the venv is the broken thing), so
    the drift is closed here instead: against jasper.audio_runtime_plan, which
    the plan's own contract test pins to the Rust const.
    """
    import re

    from jasper.audio_runtime_plan import DEFAULT_OUTPUTD_DAC_BUFFER_FRAMES

    match = re.search(
        r"^DEFAULT_OUTPUTD_DAC_BUFFER_FRAMES=(\d+)$",
        SCRIPT.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert match is not None
    assert int(match.group(1)) == DEFAULT_OUTPUTD_DAC_BUFFER_FRAMES

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

    # Ring A 256 + Camilla 4096 + Ring B 128 + the packaged DAC default.
    expected = -(256 + 4096 + 128 + DEFAULT_OUTPUTD_DAC_BUFFER_FRAMES) / 48000
    assert f"audio_backend_latency_offset_in_seconds = {expected:.6f};" in rendered


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
    fanin_socket = JsonStatusSocket(
        {"output": {"snd_pcm_delay_frames": 1536}},
        name="fanin.sock",
    )
    outputd_socket = JsonStatusSocket(
        {"dac": {"snd_pcm_delay_frames": 1024}},
        name="outputd.sock",
    )
    with fanin_socket as fanin_status, outputd_socket as outputd_status:
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
    # One STATUS connect for outputd: it always serves Ring B's AND the DAC's
    # fields together, so this payload distinguishes one connect (new) from
    # main's three (occupancy, slot_frames, dac_delay, unconditionally).
    # Fanin's connect count is NOT pinned here: this payload hits Ring A's
    # first tier immediately, which main's per-field fetcher also does in
    # exactly one connect — see test_airplay_renderer_prefers_live_ring_a_occupancy
    # for the fanin pin, on the payload shape (Ring A's SECOND tier) where
    # main provably needs three.
    assert len(outputd_socket.requests) == 1


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


def test_airplay_renderer_follows_jasper_env_and_ignores_retired_bridge_knobs(
    tmp_path: Path,
):
    """/etc/jasper/jasper.env is the OPERATOR SEAM for outputd's DAC buffer.

    jasper-outputd.service pins no `Environment=` for that key, so jasper.env is
    what the daemon resolves when outputd.env is silent — and this renderer must
    charge the same number, because the AirPlay offset is downstream of it. The
    unit used to carry a literal BELOW that EnvironmentFile, which silently beat
    it; the renderer's own literal agreed with the unit and so hid the shear.

    The RETIRED bridge knobs in the same file still add nothing: that route is
    gone (ADR-0100) and no term is charged for it.
    """
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

    # Ring A 256 + Camilla 2048 + Ring B 128 + outputd DAC 1024 (jasper.env,
    # no longer shadowed) = 3456 / 48000. No rate_match term.
    assert "audio_backend_latency_offset_in_seconds = -0.072000;" in rendered


@pytest.mark.parametrize(
    ("outputd_env", "jasper_env", "expected"),
    [
        # The LAST assignment in a file wins, as it does for systemd.
        (
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES=3072\n"
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES=1024\n",
            "",
            "-0.072000",
        ),
        # A key PRESENT but empty in the higher layer is terminal: the daemon
        # then sees that value, not jasper.env's, and falls to its default.
        (
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES=\n",
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES=1024\n",
            "-0.114667",
        ),
    ],
)
def test_airplay_dac_buffer_layers_resolve_as_systemd_does(
    tmp_path: Path, outputd_env: str, jasper_env: str, expected: str
):
    rendered, _ = _render(
        tmp_path,
        _PROD_CAMILLA,
        outputd_env=outputd_env,
        jasper_env_content=jasper_env,
    )
    assert f"audio_backend_latency_offset_in_seconds = {expected};" in rendered


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


_PARSED_TIER_CAMILLA = """
devices:
  samplerate: 48000
  chunksize: 1024
  queuelimit: 4
  target_level: 2048
"""


def test_airplay_renderer_ring_conf_geometry_propagates(tmp_path: Path):
    """A ring retune (different period_frames/n_slots in the shipped ALSA
    ring conf) must change the derived offset — the geometry is parsed via
    jasper.ring_assets at render time, never hardcoded blind, so a future
    retune needs no code change here. No live STATUS on either socket, so
    both terms fall through to this parsed tier.

    period_frames is CONSISTENT across both blocks (256): the real conf.d
    shares one period value fleet-wide (jasper.ring_assets.
    ring_conf_period_frames scans every block for a single shared value),
    so a fixture with two different period_frames would be a torn conf.d
    the parser correctly refuses, not a valid retune — that shape is
    test_airplay_renderer_torn_ring_conf_falls_back_to_default below.
    n_slots MAY legitimately differ per block (Ring A's is retuned to 3;
    Ring B's own n_slots is irrelevant since Ring B's term never multiplies
    by it — see DEFAULT_RING_B_LATENCY_FRAMES's comment in the script).
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
            period_frames 256
            n_slots 2
            format S32_LE
        }
        """
    ).lstrip()

    rendered, result = _render(
        tmp_path,
        _PARSED_TIER_CAMILLA,
        ring_alsa_conf_content=ring_conf,
    )

    assert "ring_a tier=parsed frames=768" in result.stderr
    assert "ring_b tier=parsed frames=256" in result.stderr
    # Ring A retuned to 256x3=768; Ring B retuned to one slot at 256.
    # Camilla 2048 + outputd DAC default 3072 unchanged.
    # Total = 768 + 2048 + 256 + 3072 = 6144 / 48000 = 0.128000.
    assert "audio_backend_latency_offset_in_seconds = -0.128000;" in rendered


def test_airplay_renderer_torn_ring_conf_falls_back_to_default(tmp_path: Path):
    """A conf.d whose blocks disagree on period_frames (a torn file — a
    half-applied retune, or hand edit) must never be silently picked from;
    jasper.ring_assets.ring_conf_period_frames reports indeterminate for
    it, so both Ring A and Ring B fall all the way through to their
    DEFAULT_RING_A/B_LATENCY_FRAMES tier — the same total as no conf.d at
    all, never a guess built from mismatched blocks.
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

    rendered, result = _render(
        tmp_path,
        _PARSED_TIER_CAMILLA,
        ring_alsa_conf_content=ring_conf,
    )

    assert "ring_a tier=default frames=256" in result.stderr
    assert "ring_b tier=default frames=128" in result.stderr
    # Identical total to test_airplay_solo_offset_unchanged_without_grouping_env
    # (which supplies no ring conf.d at all): 256 + 2048 + 128 + 3072 = 5504.
    assert "audio_backend_latency_offset_in_seconds = -0.114667;" in rendered


def test_airplay_renderer_prefers_live_ring_a_occupancy(tmp_path: Path):
    """Ring A's SECOND tier: fanin STATUS output.ring.occupancy (live
    write_seq-read_seq depth) times output.period_frames, used when the
    ALSA-delay field (output.snd_pcm_delay_frames — correct on a loopback
    box) is absent, which is the ring topology's normal case (ADR-0100
    left no playback PCM there for shairport's own snd_pcm_delay() either).
    """
    fanin_socket = JsonStatusSocket(
        {"output": {"period_frames": 100, "ring": {"occupancy": 3}}},
        name="fanin.sock",
    )
    with fanin_socket as fanin_status:
        rendered, result = _render(
            tmp_path, _PARSED_TIER_CAMILLA, fanin_status_socket=fanin_status
        )

    # Labeled values, not just the tier name: occupancy*period is commutative,
    # so an accidental occupancy<->period_frames swap at the call site would
    # still land on the same 300 and pass an unlabeled assertion here.
    assert (
        "ring_a tier=live-status basis=ring-occupancy occupancy=3 period_frames=100"
        in result.stderr
    )
    # Ring A live 3*100=300 + Camilla 2048 + Ring B default 128 + outputd
    # DAC default 3072 = 5548 / 48000.
    assert "audio_backend_latency_offset_in_seconds = -0.115583;" in rendered
    # This payload has no snd_pcm_delay_frames at all (Ring A's REAL shape on
    # a box, per ADR-0100 — the field is null every period, unconditionally),
    # so main's per-field fetcher provably needs three connects here (a
    # failed delay_frames attempt, then occupancy, then period_frames) where
    # this renderer needs one. Unlike the fanin pin dropped from
    # test_airplay_renderer_prefers_live_fanin_output_delay (whose payload
    # hits Ring A's first tier immediately, so main also manages one connect
    # there), this is the payload shape that actually distinguishes them.
    assert len(fanin_socket.requests) == 1


def test_airplay_renderer_alsa_delay_beats_ring_occupancy_for_ring_a(
    tmp_path: Path,
):
    """When fanin STATUS offers BOTH fields (only possible on a genuinely
    mixed/transitional STATUS shape), the real ALSA delay wins over the
    occupancy-derived figure — tier order matters, not just tier presence.
    On a real loopback box output.ring is simply absent, so this pins the
    PRECEDENCE the two-field case must resolve, not a shape that occurs on
    a real ring or loopback box today.
    """
    with JsonStatusSocket(
        {
            "output": {
                "snd_pcm_delay_frames": 777,
                "period_frames": 128,
                "ring": {"occupancy": 2},
            }
        },
        name="fanin.sock",
    ) as fanin_status:
        rendered, result = _render(
            tmp_path, _PARSED_TIER_CAMILLA, fanin_status_socket=fanin_status
        )

    assert "ring_a tier=live-status basis=alsa-delay frames=777" in result.stderr
    # 777 (ALSA delay, NOT the occupancy-derived 2*128=256) + Camilla 2048 +
    # Ring B default 128 + outputd DAC default 3072 = 6025 / 48000.
    assert "audio_backend_latency_offset_in_seconds = -0.125521;" in rendered


def test_airplay_renderer_prefers_live_ring_b_occupancy(tmp_path: Path):
    """Ring B's live tier: outputd STATUS shm_ring.occupancy (live
    write_seq-read_seq depth, refreshed every DAC period) times
    shm_ring.slot_frames — the true currently-queued frame count, not just
    a static capacity/floor estimate.
    """
    with JsonStatusSocket(
        {"shm_ring": {"occupancy": 1, "slot_frames": 200}},
        name="outputd.sock",
    ) as outputd_status:
        rendered, result = _render(
            tmp_path, _PARSED_TIER_CAMILLA, outputd_status_socket=outputd_status
        )

    assert "ring_b tier=live-status occupancy=1 slot_frames=200" in result.stderr
    # Ring A default 256 + Camilla 2048 + Ring B live 1*200=200 + outputd
    # DAC default 3072 (same payload has no "dac" key) = 5576 / 48000.
    assert "audio_backend_latency_offset_in_seconds = -0.116167;" in rendered


def test_ring_a_and_ring_b_defaults_match_shipped_conf_via_ring_assets():
    """DEFAULT_RING_A_LATENCY_FRAMES / DEFAULT_RING_B_LATENCY_FRAMES (the
    script's own last-tier constants, 256 / 128) must equal what
    jasper.ring_assets — the production authority — actually parses from
    the real shipped deploy/alsa/conf.d/60-jts-ring.conf TODAY, so a ring
    retune that moves the shipped file and forgets these two bash
    constants is caught here rather than silently drifting.
    """
    from jasper import ring_assets
    from tests.test_ring_assets import SHIPPED_RING_CONF

    conf = str(SHIPPED_RING_CONF)
    period = ring_assets.ring_conf_period_frames(conf)
    ring_a_slots = ring_assets.ring_conf_n_slots(ring_assets.RING_A_CONF_PCM, conf)

    assert period is not None and ring_a_slots is not None
    assert period * ring_a_slots == 256  # DEFAULT_RING_A_LATENCY_FRAMES
    assert period == 128  # DEFAULT_RING_B_LATENCY_FRAMES (one slot, no x n_slots)
