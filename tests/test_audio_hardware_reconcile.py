# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-audio-hardware-reconcile"


def _fake_systemctl(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "systemctl.log"
    fake = tmp_path / "systemctl"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$JASPER_SYSTEMCTL_LOG\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake, log


def _fake_aplay(tmp_path: Path, listing: str) -> Path:
    fake = tmp_path / "aplay"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "cat \"$JASPER_FAKE_APLAY_LISTING\"\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    (tmp_path / "aplay-L.txt").write_text(listing, encoding="utf-8")
    return fake


def _fake_renderer(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "render.log"
    fake = tmp_path / "jasper-render-asound-conf"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'render\\n' >> \"$JASPER_RENDER_LOG\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake, log


def _run_reconcile(
    tmp_path: Path,
    listing: str,
    *args: str,
    initial_env: str | None = None,
    initial_outputd_env: str | None = None,
    initial_fanin_env: str | None = None,
    initial_usbsink_env: str | None = None,
    initial_template: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    fake_systemctl, systemctl_log = _fake_systemctl(tmp_path)
    fake_aplay = _fake_aplay(tmp_path, listing)
    fake_renderer, render_log = _fake_renderer(tmp_path)
    source_template = tmp_path / "asoundrc.jasper.source"
    source_template.write_text(
        "__OUTPUTD_DAC_PCM_BLOCK__\n"
        "__OUTPUTD_DAC_CTL_BLOCK__\n"
        "pcm.jasper_out { card __DONGLE_CARD__ }\n"
        "defaults.pcm.rate_converter \"__RATE_CONVERTER__\"\n",
        encoding="utf-8",
    )
    audio_quality = tmp_path / "audio_quality.env"
    audio_quality.write_text(
        "JASPER_ALSA_RATE_CONVERTER=samplerate_medium\n",
        encoding="utf-8",
    )
    if initial_env is not None:
        (tmp_path / "jasper.env").write_text(initial_env, encoding="utf-8")
    if initial_outputd_env is not None:
        (tmp_path / "outputd.env").write_text(initial_outputd_env, encoding="utf-8")
    if initial_fanin_env is not None:
        (tmp_path / "fanin.env").write_text(initial_fanin_env, encoding="utf-8")
    if initial_usbsink_env is not None:
        (tmp_path / "usbsink.env").write_text(initial_usbsink_env, encoding="utf-8")
    if initial_template is not None:
        (tmp_path / "asoundrc.jasper.template").write_text(
            initial_template,
            encoding="utf-8",
        )

    env = os.environ.copy()
    env.update(
        {
            "JASPER_ENV_FILE": str(tmp_path / "jasper.env"),
            "JASPER_OUTPUTD_ENV_FILE": str(tmp_path / "outputd.env"),
            "JASPER_FANIN_ENV_FILE": str(tmp_path / "fanin.env"),
            "JASPER_USBSINK_ENV_FILE": str(tmp_path / "usbsink.env"),
            "JASPER_TTS_ENV_FILE": str(tmp_path / "tts.env"),
            "JASPER_ASOUND_SOURCE_TEMPLATE": str(source_template),
            "JASPER_ASOUND_TEMPLATE": str(tmp_path / "asoundrc.jasper.template"),
            "JASPER_ASOUND_CONF": str(tmp_path / "asound.conf"),
            "JASPER_AUDIO_QUALITY_FILE": str(audio_quality),
            "JASPER_RENDER_ASOUND_CONF": str(fake_renderer),
            "JASPER_RENDER_LOG": str(render_log),
            "JASPER_SYSTEMCTL": str(fake_systemctl),
            "JASPER_SYSTEMCTL_LOG": str(systemctl_log),
            "JASPER_APLAY": str(fake_aplay),
            "JASPER_FAKE_APLAY_LISTING": str(tmp_path / "aplay-L.txt"),
            "JASPER_OUTPUT_HARDWARE_STATE_PATH": str(
                tmp_path / "output_hardware.json"
            ),
            "JASPER_OUTPUT_HARDWARE_PYTHON": sys.executable,
            # Hermetic active-graph gate inputs: point the cutover gate's
            # statefile + topology at tmp paths that are ABSENT unless a test
            # explicitly stages them via _active_graph_env(). Without this the
            # gate would read the real /var/lib/jasper paths on a dev box.
            "JASPER_CAMILLA_STATEFILE": str(tmp_path / "outputd-statefile.yml"),
            "JASPER_OUTPUT_TOPOLOGY_PATH": str(tmp_path / "output_topology.json"),
            # Hermetic: always source the repo's shared env-file lib, never
            # a (possibly stale) installed copy under /usr/local/lib.
            "JASPER_ENV_FILE_LIB": str(
                ROOT / "deploy" / "lib" / "jasper-env-file.sh"
            ),
            "JASPER_ASOUND_RENDER_LIB": str(
                ROOT / "deploy" / "lib" / "jasper-asound-render.sh"
            ),
        }
    )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        check=False,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )


def _assert_no_empty_alsa_card(rendered: str) -> None:
    assert not re.search(r"(?m)^\s*card\s*$", rendered)
    assert not re.search(r"\bcard\s+}", rendered)


def _assert_parked_outputd_dac_template(rendered: str) -> None:
    assert "pcm.outputd_dac" in rendered
    assert "type null" in rendered
    assert "ctl.outputd_dac" not in rendered
    _assert_no_empty_alsa_card(rendered)


def _fake_sys_output_card(
    tmp_path: Path,
    *,
    card_index: int,
    card_id: str,
    usb_path: str,
    serial: str,
) -> tuple[Path, Path]:
    sys_class = tmp_path / "sys" / "class" / "sound"
    proc_asound = tmp_path / "proc" / "asound"
    sys_class.mkdir(parents=True, exist_ok=True)
    proc_asound.mkdir(parents=True, exist_ok=True)
    usb_device = (
        tmp_path / "sys" / "devices" / "platform" / "xhci-hcd.0" / "usb1" / usb_path
    )
    card_dir = usb_device / "sound" / f"card{card_index}"
    card_dir.mkdir(parents=True, exist_ok=True)
    for name, value in {
        "idVendor": "05ac",
        "idProduct": "110a",
        "serial": serial,
        "busnum": "1",
        "devpath": usb_path,
        "product": "Apple USB-C to 3.5mm Headphone Jack",
    }.items():
        (usb_device / name).write_text(value, encoding="utf-8")
    (sys_class / f"card{card_index}").symlink_to(card_dir)
    proc_card = proc_asound / f"card{card_index}"
    proc_card.mkdir(parents=True, exist_ok=True)
    (proc_card / "id").write_text(card_id, encoding="utf-8")
    (proc_card / "pcm0p").mkdir()
    (proc_card / "stream0").write_text(
        "Playback:\n  Endpoint: 0x01 (SYNC)\n",
        encoding="utf-8",
    )
    return sys_class, proc_asound


def _systemctl_log(tmp_path: Path) -> str:
    log = tmp_path / "systemctl.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""


def _render_log(tmp_path: Path) -> str:
    log = tmp_path / "render.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""


def _active_graph_env(
    tmp_path: Path,
    *,
    channels: int = 4,
    write_topology: bool = True,
) -> dict[str, str]:
    """Stage a legal active-speaker graph at ``channels`` width for the gate.

    Default 4 = the dual-Apple composite shape; pass channels=2 for the
    currently deployed mono 2-way shape or 6 for a stereo 3-way DAC8x shape.
    The reconciler's width-aware gate reads the runtime contract's playback
    width and compares it against the DAC's active-lane cap.
    """
    from jasper.active_speaker import (
        ActiveSpeakerPreset,
        emit_active_speaker_baseline_config,
    )
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_profile import _three_way_preset, _two_way_preset
    from tests.test_active_speaker_runtime_contract import _active_topology

    if channels == 2:
        topology = _active_topology("mono", "active_2_way")
        preset = ActiveSpeakerPreset.from_mapping(_two_way_preset("mono"))
    elif channels == 4:
        topology = _active_topology("stereo", "active_2_way")
        preset = ActiveSpeakerPreset.from_mapping(_two_way_preset("stereo"))
    elif channels == 6:
        topology = _active_topology("stereo", "active_3_way")
        preset = ActiveSpeakerPreset.from_mapping(_three_way_preset("stereo"))
    else:
        topology = _active_topology("mono", "active_2_way")
        preset = ActiveSpeakerPreset.from_mapping(_two_way_preset("mono"))

    active_config = tmp_path / "active_speaker_baseline.yml"
    active_text = emit_active_speaker_baseline_config(
        preset,
        playback_device="outputd_active_content_playback",
        baseline_id=f"test-{channels}",
    )
    if channels not in {2, 4, 6}:
        active_text = active_text.replace(
            "channels: { in: 2, out: 2 }",
            f"channels: {{ in: 2, out: {channels} }}",
        ).replace(
            "channels: 2\n    device: \"outputd_active_content_playback\"",
            f"channels: {channels}\n    device: \"outputd_active_content_playback\"",
        )
    active_config.write_text(active_text, encoding="utf-8")
    topology_path = tmp_path / "output_topology.json"
    if write_topology:
        save_output_topology(topology, path=topology_path)
    prior_config = tmp_path / "outputd-cutover.yml"
    prior_config.write_text(
        "devices:\n"
        "  samplerate: 48000\n"
        "  channels: 2\n"
        "  playback:\n"
        "    type: Alsa\n"
        "    device: outputd_content_playback\n",
        encoding="utf-8",
    )
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {active_config}\n", encoding="utf-8")
    out = {
        "JASPER_CAMILLA_STATEFILE": str(statefile),
    }
    if write_topology:
        out["JASPER_OUTPUT_TOPOLOGY_PATH"] = str(topology_path)
    return out


def _active_leader_graph_env(
    tmp_path: Path,
    *,
    channels: int = 2,
    write_crossover_statefile: bool = True,
) -> dict[str, str]:
    """Stage camilla#1 program bake + camilla#2 endpoint graph for the gate."""
    from jasper.active_speaker import (
        ActiveSpeakerPreset,
        emit_active_speaker_driver_domain_config,
        emit_active_speaker_program_bake_config,
    )
    from jasper.output_topology import save_output_topology
    from jasper.sound.profile import SimpleEq, SoundProfile
    from tests.test_active_speaker_profile import _three_way_preset, _two_way_preset
    from tests.test_active_speaker_runtime_contract import _active_topology

    if channels == 2:
        topology = _active_topology("mono", "active_2_way")
        preset = ActiveSpeakerPreset.from_mapping(_two_way_preset("mono"))
    elif channels == 4:
        topology = _active_topology("stereo", "active_2_way")
        preset = ActiveSpeakerPreset.from_mapping(_two_way_preset("stereo"))
    elif channels == 6:
        topology = _active_topology("stereo", "active_3_way")
        preset = ActiveSpeakerPreset.from_mapping(_three_way_preset("stereo"))
    else:
        raise AssertionError(f"unsupported test channel count: {channels}")

    bake_config = tmp_path / "grouping_active_leader_bake.yml"
    bake_config.write_text(
        emit_active_speaker_program_bake_config(
            SoundProfile(enabled=True, simple_eq=SimpleEq(bass_db=3.0)),
        ),
        encoding="utf-8",
    )
    crossover_config = tmp_path / "grouping_active_leader_crossover.yml"
    crossover_config.write_text(
        emit_active_speaker_driver_domain_config(
            preset,
            playback_device="outputd_active_content_playback",
            program_channel="mono",
        ),
        encoding="utf-8",
    )

    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, path=topology_path)
    outputd_statefile = tmp_path / "outputd-statefile.yml"
    outputd_statefile.write_text(f"config_path: {bake_config}\n", encoding="utf-8")
    crossover_statefile = tmp_path / "crossover-statefile.yml"
    if write_crossover_statefile:
        crossover_statefile.write_text(
            f"config_path: {crossover_config}\n",
            encoding="utf-8",
        )
    return {
        "JASPER_CAMILLA_STATEFILE": str(outputd_statefile),
        "JASPER_CAMILLA2_STATEFILE": str(crossover_statefile),
        "JASPER_OUTPUT_TOPOLOGY_PATH": str(topology_path),
    }


def _apple_active_graph_env(tmp_path: Path) -> dict[str, str]:
    env = _active_graph_env(tmp_path, channels=2)
    from jasper.output_topology import OutputTopology, save_output_topology

    topology_path = Path(env["JASPER_OUTPUT_TOPOLOGY_PATH"])
    raw = json.loads(topology_path.read_text(encoding="utf-8"))
    raw["hardware"] = {
        "device_id": "apple_usb_c_dongle",
        "device_label": "Apple USB-C audio adapter",
        "physical_output_count": 2,
        "card_id": "A",
    }
    save_output_topology(OutputTopology.from_mapping(raw), path=topology_path)
    return env


APPLE_LISTING = """
hw:CARD=A,DEV=0
    Apple USB-C to 3.5mm Headphone Jack, USB Audio
"""


DUAL_APPLE_LISTING = """
hw:CARD=A,DEV=0
    Apple USB-C to 3.5mm Headphone Jack, USB Audio
hw:CARD=A_1,DEV=0
    Apple USB-C to 3.5mm Headphone Jack, USB Audio
"""


DAC8X_AND_APPLE_LISTING = """
hw:CARD=A,DEV=0
    Apple USB-C to 3.5mm Headphone Jack, USB Audio
hw:CARD=sndrpihifiberry,DEV=0
    snd_rpi_hifiberry_dac8x, HiFiBerry DAC8x
"""


DAC8X_STUDIO_LISTING = """
hw:CARD=DAC8XStudio,DEV=0
    HiFiBerry DAC8x Studio, USB Audio
"""


def test_print_env_prefers_dac8x_but_keeps_apple_control_role(tmp_path: Path):
    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--print-env",
    )

    assert result.returncode == 0, result.stderr
    assert "DONGLE_CARD=A" in result.stdout
    assert "APPLE_DONGLE_PRESENT=1" in result.stdout
    assert "APPLE_DONGLE_SERVICE_CARD=A" in result.stdout
    assert "OUTPUT_DAC_CARD=sndrpihifiberry" in result.stdout
    assert "OUTPUT_DAC_ID=hifiberry_dac8x" in result.stdout
    assert "OUTPUT_DAC_RECOGNIZED=1" in result.stdout
    assert "OUTPUT_DAC_ROUTE" not in result.stdout
    assert not (tmp_path / "jasper.env").exists()
    assert not (tmp_path / "output_hardware.json").exists()


def test_print_env_recognizes_dac8x_studio_role(tmp_path: Path):
    result = _run_reconcile(
        tmp_path,
        DAC8X_STUDIO_LISTING,
        "--print-env",
    )

    assert result.returncode == 0, result.stderr
    assert "OUTPUT_DAC_CARD=DAC8XStudio" in result.stdout
    assert "OUTPUT_DAC_ID=hifiberry_dac8x_studio" in result.stdout
    assert "OUTPUT_DAC_RECOGNIZED=1" in result.stdout


def test_reconcile_apple_role_enables_apple_helpers_and_renders(tmp_path: Path):
    result = _run_reconcile(tmp_path, APPLE_LISTING, "--reason", "test")

    assert result.returncode == 0, result.stderr
    env_text = (tmp_path / "jasper.env").read_text(encoding="utf-8")
    assert "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle" in env_text
    assert "JASPER_AUDIO_DAC_CARD=A" in env_text
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env
    assert not (tmp_path / "tts.env").exists()
    template = (tmp_path / "asoundrc.jasper.template").read_text(encoding="utf-8")
    assert "pcm.outputd_dac" in template
    assert "type hw" in template
    assert "card A" in template
    _assert_no_empty_alsa_card(template)
    assert _render_log(tmp_path) == "render\n"
    commands = _systemctl_log(tmp_path)
    assert "enable jasper-dac-init.service jasper-headphone-monitor.service" in commands
    assert "start jasper-dac-init.service" in commands
    assert "restart jasper-headphone-monitor.service" in commands
    assert "stop jasper-voice.service" in commands
    assert "reset-failed jasper-outputd.service" in commands
    assert "--no-block restart jasper-outputd.service" in commands
    assert "--no-block restart jasper-aec-reconcile.service" in commands


def test_reconcile_preserves_existing_env_dir_modes(tmp_path: Path):
    """Reconcile must NOT re-chmod an existing env-file parent dir.

    /var/lib/jasper is 0770 root:jasper (ensure_state_dir, so the now-non-root
    jasper-voice/-mux can write speaker_volume.json) and /etc/jasper is 0755
    (widen_control_secret_env_modes, so the group-jasper doctor-json oneshot
    can traverse to read jasper.env). A blanket ``install -d -m 0750`` in
    set_env_var / set_env_file_var re-stripped those bits on every
    install / boot / udev-hotplug reconcile. Pin that a pre-created env-file
    parent dir keeps its mode after a reconcile that writes into it.
    """
    state_dir = tmp_path / "var-lib-jasper"
    etc_dir = tmp_path / "etc-jasper"
    state_dir.mkdir()
    etc_dir.mkdir()
    # Set modes explicitly (mkdir's mode arg is masked by umask).
    state_dir.chmod(0o770)
    etc_dir.chmod(0o755)

    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        extra_env={
            "JASPER_ENV_FILE": str(etc_dir / "jasper.env"),
            "JASPER_OUTPUTD_ENV_FILE": str(state_dir / "outputd.env"),
        },
    )

    assert result.returncode == 0, result.stderr
    # The reconcile actually wrote both env files into those dirs, so the
    # mode-preservation assertions below are not vacuous.
    assert (etc_dir / "jasper.env").exists()
    assert (state_dir / "outputd.env").exists()
    assert oct(state_dir.stat().st_mode & 0o777) == "0o770"
    assert oct(etc_dir.stat().st_mode & 0o777) == "0o755"


def test_env_writer_preserves_existing_jasper_env_ownership() -> None:
    """A DAC reconcile must not turn root:jasper jasper.env into root:root.

    jasper-control relies on group-read access for fresh /state reads; the
    audio-hardware reconciler also atomically rewrites /etc/jasper/jasper.env
    and repairs generated /var/lib/jasper env-file permissions on no-op runs.
    """
    text = SCRIPT.read_text()
    assert 'jasper_env_file_set "$ENV_FILE" "$key" "$value" 0640 0750' in text
    assert 'jasper_env_file_set "$file" "$key" "$value" 0640 0750' in text
    assert (
        'jasper_env_file_repair_permissions "$OUTPUTD_ENV_FILE" 0640 0750'
        in text
    )
    assert 'jasper_env_file_repair_permissions "$FANIN_ENV_FILE" 0640 0750' in text
    assert (
        'jasper_env_file_repair_permissions "$USBSINK_ENV_FILE" 0640 0750'
        in text
    )


def test_reconcile_preserves_asound_template_dir_mode(tmp_path: Path):
    """render_asound_if_needed must NOT re-chmod the existing /etc/jasper.

    The asound-template dir create ran `install -d -m 0755 $(dirname
    $ASOUND_TEMPLATE)` (== /etc/jasper) on EVERY recognized-DAC reconcile,
    bypassing the env writer discipline — the same re-mode trap #827 closed.
    Pin that a pre-created non-0755 dir survives
    an Apple (recognized-DAC) reconcile that renders the template into it.
    """
    etc_dir = tmp_path / "etc-jasper"
    etc_dir.mkdir()
    etc_dir.chmod(0o700)  # deliberately not 0755, to prove it is preserved

    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        extra_env={"JASPER_ASOUND_TEMPLATE": str(etc_dir / "asoundrc.jasper.template")},
    )

    assert result.returncode == 0, result.stderr
    assert _render_log(tmp_path) == "render\n"  # the render path (and :602) ran
    assert oct(etc_dir.stat().st_mode & 0o777) == "0o700"


def test_reconcile_recognized_arrival_starts_outputd_when_values_unchanged(
    tmp_path: Path,
):
    rendered_template = (
        "pcm.outputd_dac {\n"
        "    type hw\n"
        "    card A\n"
        "    device 0\n"
        "}\n"
        "ctl.outputd_dac {\n"
        "    type hw\n"
        "    card A\n"
        "}\n"
        "pcm.jasper_out { card A }\n"
        "defaults.pcm.rate_converter \"__RATE_CONVERTER__\"\n"
    )
    outputd_env = (
        "JASPER_OUTPUTD_BACKEND=alsa\n"
        "JASPER_OUTPUTD_SINK=single_alsa\n"
        "JASPER_OUTPUTD_CONTENT_PCM=outputd_content_capture\n"
        "JASPER_OUTPUTD_DAC_PCM=outputd_dac\n"
        "JASPER_OUTPUTD_DUAL_DAC_A_PCM=''\n"
        "JASPER_OUTPUTD_DUAL_DAC_B_PCM=''\n"
        # The single stereo path now also manages the wide-lane width knob,
        # cleared so a stale active width can't mis-size the stereo lane.
        "JASPER_OUTPUTD_ACTIVE_CHANNELS=''\n"
        # A passive stereo sink is not an active-crossover lane, so the
        # active-lane marker is cleared here too. Seeding it keeps the
        # steady state truly unchanged (no spurious outputd restart).
        "JASPER_OUTPUTD_ACTIVE_LANE=''\n"
        # The Apple dongle's codified latency floor (#27) is part of the
        # steady state now — seed it so a second reconcile is a true no-op.
        "JASPER_CAMILLA_CHUNKSIZE=256\n"
        "JASPER_CAMILLA_TARGET_LEVEL=1536\n"
        "JASPER_OUTPUTD_PERIOD_FRAMES=128\n"
        "JASPER_OUTPUTD_DAC_BUFFER_FRAMES=256\n"
    )
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_env=(
            "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n"
            "JASPER_AUDIO_DAC_CARD=A\n"
        ),
        initial_outputd_env=outputd_env,
        initial_usbsink_env="JASPER_USBSINK_OUTPUT_MODE=aloop\n",
        initial_template=rendered_template,
    )

    assert result.returncode == 0, result.stderr
    assert "env_changed=0 render_changed=0" in result.stderr
    assert _render_log(tmp_path) == ""
    commands = _systemctl_log(tmp_path)
    assert "reset-failed jasper-outputd.service" in commands
    assert "--no-block start jasper-outputd.service" in commands
    assert "--no-block restart jasper-outputd.service" not in commands
    assert "stop jasper-voice.service" not in commands
    assert "--no-block restart jasper-aec-reconcile.service" not in commands


def test_reconcile_applies_usb_low_latency_route_env(tmp_path: Path):
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_env="JASPER_AUDIO_ROUTE_PROFILE=usb_low_latency_48k\n",
    )

    assert result.returncode == 0, result.stderr
    fanin_env = (tmp_path / "fanin.env").read_text(encoding="utf-8")
    usbsink_env = (tmp_path / "usbsink.env").read_text(encoding="utf-8")
    assert "JASPER_FANIN_INPUT_RESAMPLER=enabled" in fanin_env
    assert "JASPER_FANIN_INPUT_RESAMPLER_LANE=usbsink" in fanin_env
    assert "JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES=512" in fanin_env
    assert "JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES=1536" in fanin_env
    assert "JASPER_FANIN_INPUT_RESAMPLER_RING_FRAMES=4096" in fanin_env
    assert "JASPER_USBSINK_AUDIO_IMPL" not in usbsink_env
    assert "JASPER_USBSINK_BLOCK_FRAMES=256" in usbsink_env
    assert "JASPER_USBSINK_RING_PERIODS=3" in usbsink_env


def test_reconcile_dual_apple_records_profile_and_parks_until_dual_sink(
    tmp_path: Path,
):
    sys_class, proc_asound = _fake_sys_output_card(
        tmp_path,
        card_index=1,
        card_id="A",
        usb_path="1-1",
        serial="left",
    )
    _fake_sys_output_card(
        tmp_path,
        card_index=2,
        card_id="A_1",
        usb_path="1-2",
        serial="right",
    )
    result = _run_reconcile(
        tmp_path,
        DUAL_APPLE_LISTING,
        "--reason",
        "test",
        extra_env={
            "JASPER_SYS_CLASS_SOUND": str(sys_class),
            "JASPER_PROC_ASOUND": str(proc_asound),
        },
    )

    assert result.returncode == 0, result.stderr
    env_text = (tmp_path / "jasper.env").read_text(encoding="utf-8")
    assert "JASPER_AUDIO_DAC_ID=dual_apple_usb_c_dac_4ch" in env_text
    assert "JASPER_AUDIO_DAC_CARD=''" in env_text
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env
    assert not (tmp_path / "tts.env").exists()
    state_text = (tmp_path / "output_hardware.json").read_text(encoding="utf-8")
    assert '"profile_id": "dual_apple_usb_c_dac_4ch"' in state_text
    assert '"apple_dac_count": 2' in state_text
    template = (tmp_path / "asoundrc.jasper.template").read_text(encoding="utf-8")
    _assert_parked_outputd_dac_template(template)
    assert _render_log(tmp_path) == "render\n"
    commands = _systemctl_log(tmp_path)
    assert "enable jasper-dac-init.service jasper-headphone-monitor.service" in commands
    assert "--no-block stop jasper-voice.service jasper-outputd.service" in commands
    assert "event=audio_hardware_reconcile.dual_apple_detected" in result.stderr


def test_reconcile_dual_apple_pins_pcm_order_from_saved_topology(
    tmp_path: Path,
):
    sys_class, proc_asound = _fake_sys_output_card(
        tmp_path,
        card_index=1,
        card_id="B",
        usb_path="1-1",
        serial="right",
    )
    _fake_sys_output_card(
        tmp_path,
        card_index=2,
        card_id="A",
        usb_path="1-2",
        serial="left",
    )
    topology_path = tmp_path / "output_topology.json"
    from tests.test_active_speaker_runtime_contract import _active_topology

    topology = _active_topology("stereo", "active_2_way").to_dict()
    topology["topology_id"] = "dual_apple"
    topology["name"] = "Dual Apple"
    topology["hardware"] = {
        "device_id": "dual_apple_usb_c_dac_4ch",
        "device_label": "Dual Apple USB-C DAC 4-channel pair",
        "physical_output_count": 4,
        "child_devices": [
            {
                "child_id": "left",
                "device_id": "apple_usb_c_dongle",
                "device_label": "Apple USB-C audio adapter",
                "serial": "left",
                "physical_output_indexes": [0, 1],
            },
            {
                "child_id": "right",
                "device_id": "apple_usb_c_dongle",
                "device_label": "Apple USB-C audio adapter",
                "serial": "right",
                "physical_output_indexes": [2, 3],
            },
        ],
        "clock_domain_evidence": {
            "evidence_kind": "dual_apple_usb_c_dac_drift_measurement",
            "measurement_id": "unit-test-dual-apple-sync",
            "status": "passed",
            "duration_seconds": 900,
            "sample_rate_hz": 48000,
            "offset_frames": 0,
            "max_offset_delta_frames": 0,
            "drift_ppm": 0,
            "xrun_count": 0,
            "dac_serials": ["left", "right"],
        },
    }
    topology_path.write_text(
        json.dumps(topology),
        encoding="utf-8",
    )

    result = _run_reconcile(
        tmp_path,
        DUAL_APPLE_LISTING,
        "--reason",
        "test",
        extra_env={
            "JASPER_SYS_CLASS_SOUND": str(sys_class),
            "JASPER_PROC_ASOUND": str(proc_asound),
            "JASPER_OUTPUT_TOPOLOGY_PATH": str(topology_path),
            **_active_graph_env(tmp_path, write_topology=False),
        },
    )

    assert result.returncode == 0, result.stderr
    env_text = (tmp_path / "jasper.env").read_text(encoding="utf-8")
    assert "JASPER_AUDIO_DAC_ID=dual_apple_usb_c_dac_4ch" in env_text
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_SINK=dual_apple" in outputd_env
    assert "JASPER_OUTPUTD_DUAL_DAC_A_PCM=hw:CARD=A,DEV=0" in outputd_env
    assert "JASPER_OUTPUTD_DUAL_DAC_B_PCM=hw:CARD=B,DEV=0" in outputd_env
    # A wide composite sink (4ch) is already fenced off outputd's stereo-only
    # features by its channel width, so the reconciler does NOT set the 2-ch
    # active-lane marker here — it stays cleared.
    assert "JASPER_OUTPUTD_ACTIVE_LANE=''" in outputd_env
    template = (tmp_path / "asoundrc.jasper.template").read_text(encoding="utf-8")
    assert "pcm.outputd_dac" in template
    assert "type null" in template
    assert "ctl.outputd_dac" not in template
    _assert_no_empty_alsa_card(template)
    assert "order_source=saved_topology" in result.stderr


def test_reconcile_dual_apple_defers_runtime_until_active_graph_is_loaded(
    tmp_path: Path,
):
    sys_class, proc_asound = _fake_sys_output_card(
        tmp_path,
        card_index=1,
        card_id="B",
        usb_path="1-1",
        serial="right",
    )
    _fake_sys_output_card(
        tmp_path,
        card_index=2,
        card_id="A",
        usb_path="1-2",
        serial="left",
    )
    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text(
        json.dumps({
            "artifact_schema_version": 1,
            "kind": "jts_output_topology",
            "topology_id": "dual_apple",
            "name": "Dual Apple",
            "status": "ready",
            "hardware": {
                "device_id": "dual_apple_usb_c_dac_4ch",
                "device_label": "Dual Apple USB-C DAC 4-channel pair",
                "physical_output_count": 4,
                "outputs": [],
                "child_devices": [
                    {
                        "child_id": "left",
                        "device_id": "apple_usb_c_dongle",
                        "device_label": "Apple USB-C audio adapter",
                        "serial": "left",
                        "physical_output_indexes": [0, 1],
                    },
                    {
                        "child_id": "right",
                        "device_id": "apple_usb_c_dongle",
                        "device_label": "Apple USB-C audio adapter",
                        "serial": "right",
                        "physical_output_indexes": [2, 3],
                    },
                ],
            },
            "speaker_groups": [],
            "routing": {},
            "safety": {},
        }),
        encoding="utf-8",
    )

    result = _run_reconcile(
        tmp_path,
        DUAL_APPLE_LISTING,
        "--reason",
        "test",
        extra_env={
            "JASPER_SYS_CLASS_SOUND": str(sys_class),
            "JASPER_PROC_ASOUND": str(proc_asound),
            "JASPER_OUTPUT_TOPOLOGY_PATH": str(topology_path),
        },
    )

    assert result.returncode == 0, result.stderr
    env_text = (tmp_path / "jasper.env").read_text(encoding="utf-8")
    assert "JASPER_AUDIO_DAC_ID=dual_apple_usb_c_dac_4ch" in env_text
    assert "JASPER_AUDIO_DAC_CARD=''" in env_text
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_BACKEND=fake" in outputd_env
    assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env
    assert "JASPER_OUTPUTD_CONTENT_PCM=outputd_content_capture" in outputd_env
    assert "JASPER_OUTPUTD_DUAL_DAC_A_PCM=''" in outputd_env
    state_text = (tmp_path / "output_hardware.json").read_text(encoding="utf-8")
    assert '"profile_id": "dual_apple_usb_c_dac_4ch"' in state_text
    assert "action=park_until_active_graph" in result.stderr
    assert "reason=camilla_statefile_missing" in result.stderr
    template = (tmp_path / "asoundrc.jasper.template").read_text(encoding="utf-8")
    _assert_parked_outputd_dac_template(template)
    assert _render_log(tmp_path) == "render\n"
    commands = _systemctl_log(tmp_path)
    assert "--no-block stop jasper-voice.service jasper-outputd.service" in commands


def test_reconcile_dac8x_role_disables_apple_helpers(tmp_path: Path):
    result = _run_reconcile(tmp_path, DAC8X_AND_APPLE_LISTING, "--reason", "test")

    assert result.returncode == 0, result.stderr
    env_text = (tmp_path / "jasper.env").read_text(encoding="utf-8")
    assert "JASPER_AUDIO_DAC_ID=hifiberry_dac8x" in env_text
    assert "JASPER_AUDIO_DAC_CARD=sndrpihifiberry" in env_text
    # No active baseline loaded => a DAC8x is an ordinary stereo speaker, NOT
    # the wide 8-channel active lane (fail-closed: the gate kept it stereo).
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env
    assert "JASPER_OUTPUTD_CONTENT_PCM=outputd_content_capture" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=''" in outputd_env
    assert "single_alsa_active" not in result.stderr
    assert not (tmp_path / "tts.env").exists()
    template = (tmp_path / "asoundrc.jasper.template").read_text(encoding="utf-8")
    assert "pcm.outputd_dac" in template
    assert "type hw" in template
    assert "card sndrpihifiberry" in template
    assert "pcm.jasper_out { card A }" in template
    _assert_no_empty_alsa_card(template)
    commands = _systemctl_log(tmp_path)
    assert "disable --now jasper-dac-init.service jasper-headphone-monitor.service" in commands
    assert "reset-failed jasper-dac-init.service jasper-headphone-monitor.service" in commands
    assert "enable jasper-dac-init.service" not in commands
    assert "stop jasper-voice.service" in commands
    assert "--no-block restart jasper-outputd.service" in commands
    assert "--no-block restart jasper-aec-reconcile.service" in commands


def test_reconcile_dac8x_active_graph_wide_profile_emits_that_width(tmp_path: Path):
    # A DAC8x with a loaded active baseline that drives 6 outputs engages the
    # active lane at width 6: outputd reads the active content lane at the graph
    # width, not the DAC's maximum 8-channel capacity.
    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--reason",
        "test",
        extra_env=_active_graph_env(tmp_path, channels=6),
    )

    assert result.returncode == 0, result.stderr
    env_text = (tmp_path / "jasper.env").read_text(encoding="utf-8")
    assert "JASPER_AUDIO_DAC_ID=hifiberry_dac8x" in env_text
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_BACKEND=alsa" in outputd_env
    assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env
    assert "JASPER_OUTPUTD_CONTENT_PCM=outputd_active_content_capture" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=6" in outputd_env
    assert "JASPER_OUTPUTD_DAC_PCM=outputd_dac" in outputd_env
    assert "JASPER_OUTPUTD_DUAL_DAC_A_PCM=''" in outputd_env
    assert "mode=single_alsa_active active_channels=6 active_lane_cap=8" in result.stderr


def test_reconcile_dac8x_active_graph_two_way_drives_only_two(tmp_path: Path):
    # DRIVE WHAT WE USE: a 2-way baseline (2-channel config) on a DAC8x engages
    # the active lane at width 2 — outputd opens the DAC at 2 and powers the two
    # outputs the speaker actually uses, NOT all 8. This is the headline of the
    # capacity (<= cap) model: the config's actual width is emitted verbatim.
    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--reason",
        "test",
        extra_env=_active_graph_env(tmp_path, channels=2),
    )

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env
    assert "JASPER_OUTPUTD_CONTENT_PCM=outputd_active_content_capture" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=2" in outputd_env
    # A 2-ch active sink is the case channel width can't distinguish from a
    # full-range stereo L/R sink, so the reconciler marks it explicitly; outputd
    # reads this to fail its stereo-only post-crossover features closed.
    assert "JASPER_OUTPUTD_ACTIVE_LANE=1" in outputd_env
    assert "mode=single_alsa_active active_channels=2 active_lane_cap=8" in result.stderr


def test_reconcile_single_apple_active_graph_drives_width_two(tmp_path: Path):
    # A single Apple dongle has exactly the two coherent lanes a mono active
    # 2-way needs, so a legal loaded active graph should engage the same
    # outputd-owned active lane as wider coherent single DACs.
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        extra_env=_apple_active_graph_env(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    env_text = (tmp_path / "jasper.env").read_text(encoding="utf-8")
    assert "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle" in env_text
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env
    assert "JASPER_OUTPUTD_CONTENT_PCM=outputd_active_content_capture" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=2" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_LANE=1" in outputd_env
    assert "mode=single_alsa_active active_channels=2 active_lane_cap=2" in result.stderr


def test_reconcile_active_leader_program_bake_uses_crossover_endpoint(
    tmp_path: Path,
):
    # Grouped active-leader mode splits CamillaDSP: camilla#1's statefile points
    # at the File/SNAPFIFO program bake, while camilla#2 owns the driver-domain
    # endpoint graph that feeds outputd_active_content_playback. Reconcile must
    # prove the pair and keep outputd on the active lane during failure recovery.
    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--reason",
        "outputd-failure",
        "--no-restart",
        extra_env=_active_leader_graph_env(tmp_path, channels=2),
    )

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env
    assert "JASPER_OUTPUTD_CONTENT_PCM=outputd_active_content_capture" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=2" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_LANE=1" in outputd_env
    assert "mode=single_alsa_active active_channels=2 active_lane_cap=8" in result.stderr


def test_reconcile_program_bake_without_crossover_endpoint_stays_stereo(
    tmp_path: Path,
):
    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--reason",
        "outputd-failure",
        "--no-restart",
        extra_env=_active_leader_graph_env(
            tmp_path,
            channels=2,
            write_crossover_statefile=False,
        ),
    )

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_CONTENT_PCM=outputd_content_capture" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=''" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_LANE=''" in outputd_env
    assert "single_alsa_active" not in result.stderr
    assert (
        "active_graph=program_bake_pipe_without_active_crossover:"
        "camilla2_statefile_missing"
    ) in result.stderr


def test_reconcile_active_leader_crossover_over_cap_stays_stereo(
    tmp_path: Path,
):
    # The active-leader exception still obeys the final-output DAC cap. A graph
    # that is safe in isolation but wider than the currently detected coherent
    # sink must fail closed instead of emitting stale active-lane env.
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "outputd-failure",
        "--no-restart",
        extra_env=_active_leader_graph_env(tmp_path, channels=6),
    )

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_CONTENT_PCM=outputd_content_capture" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=''" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_LANE=''" in outputd_env
    assert "single_alsa_active" not in result.stderr
    assert (
        "active_graph=program_bake_pipe_without_active_crossover:"
        "active_graph_width_out_of_range got=6 cap=2"
    ) in result.stderr


def test_reconcile_active_graph_does_not_render_route_aliases(tmp_path: Path):
    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--reason",
        "test",
        initial_env="JASPER_OUTPUT_DAC_ROUTE=mono:5\n",
        extra_env=_active_graph_env(tmp_path, channels=2),
    )

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_CONTENT_PCM=outputd_active_content_capture" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=2" in outputd_env
    template = (tmp_path / "asoundrc.jasper.template").read_text(encoding="utf-8")
    assert "pcm.outputd_dac {\n    type hw\n    card sndrpihifiberry\n" in template
    assert "type route" not in template
    assert "0.4 0.5" not in template
    _assert_no_empty_alsa_card(template)
    assert "output_dac_route" not in result.stderr
    assert "route_ignored" not in result.stderr
    assert "outputd_active_mode=1 outputd_active_channels=2" in result.stderr


def test_reconcile_dac8x_active_graph_over_cap_stays_stereo(tmp_path: Path):
    # A config asking for MORE outputs than the DAC can drive (16 on an 8-output
    # DAC8x) is impossible hardware — it fails closed to ordinary stereo so the
    # speaker never tries to emit a topology the DAC cannot physically carry.
    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--reason",
        "test",
        extra_env=_active_graph_env(tmp_path, channels=16),
    )

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env
    assert "JASPER_OUTPUTD_CONTENT_PCM=outputd_content_capture" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=''" in outputd_env
    assert "single_alsa_active" not in result.stderr
    assert "active_graph=active_graph_unsafe:active_graph_output_count_mismatch" in result.stderr


def test_reconcile_unknown_role_renders_null_outputd_dac(tmp_path: Path):
    result = _run_reconcile(tmp_path, "", "--reason", "test")

    assert result.returncode == 0, result.stderr
    env_text = (tmp_path / "jasper.env").read_text(encoding="utf-8")
    assert "JASPER_AUDIO_DAC_ID=A" in env_text
    assert "JASPER_AUDIO_DAC_CARD=A" in env_text
    template = (tmp_path / "asoundrc.jasper.template").read_text(encoding="utf-8")
    _assert_parked_outputd_dac_template(template)
    assert _render_log(tmp_path) == "render\n"
    commands = _systemctl_log(tmp_path)
    assert "disable --now jasper-dac-init.service jasper-headphone-monitor.service" in commands
    assert "--no-block stop jasper-voice.service jasper-outputd.service" in commands
    assert "reset-failed jasper-voice.service jasper-outputd.service" in commands
    assert "restart jasper-outputd.service" not in commands
    assert "restart jasper-aec-reconcile.service" not in commands
    assert "event=audio_hardware_reconcile.output_parked" in result.stderr


def test_reconcile_recognized_role_restarts_outputd_after_unknown_state(
    tmp_path: Path,
):
    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--reason",
        "test",
        initial_env="JASPER_AUDIO_DAC_ID=A\nJASPER_AUDIO_DAC_CARD=A\n",
        initial_template=(
            "pcm.outputd_dac {\n"
            "    type hw\n"
            "    card sndrpihifiberry\n"
            "    device 0\n"
            "}\n"
            "ctl.outputd_dac {\n"
            "    type hw\n"
            "    card sndrpihifiberry\n"
            "}\n"
            "pcm.jasper_out { card A }\n"
            "defaults.pcm.rate_converter \"__RATE_CONVERTER__\"\n"
        ),
    )

    assert result.returncode == 0, result.stderr
    assert _render_log(tmp_path) == ""
    env_text = (tmp_path / "jasper.env").read_text(encoding="utf-8")
    assert "JASPER_AUDIO_DAC_ID=hifiberry_dac8x" in env_text
    assert "JASPER_AUDIO_DAC_CARD=sndrpihifiberry" in env_text
    commands = _systemctl_log(tmp_path)
    assert "stop jasper-voice.service" in commands
    assert "reset-failed jasper-outputd.service" in commands
    assert "--no-block restart jasper-outputd.service" in commands
    assert "--no-block restart jasper-aec-reconcile.service" in commands


def test_reconcile_restarts_when_only_outputd_runtime_env_changes(
    tmp_path: Path,
):
    rendered_template = (
        "pcm.outputd_dac {\n"
        "    type hw\n"
        "    card A\n"
        "    device 0\n"
        "}\n"
        "ctl.outputd_dac {\n"
        "    type hw\n"
        "    card A\n"
        "}\n"
        "pcm.jasper_out { card A }\n"
        "defaults.pcm.rate_converter \"__RATE_CONVERTER__\"\n"
    )
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_env="JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\nJASPER_AUDIO_DAC_CARD=A\n",
        initial_outputd_env="JASPER_OUTPUTD_BACKEND=fake\n",
        initial_template=rendered_template,
    )

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_BACKEND=alsa" in outputd_env
    assert _render_log(tmp_path) == ""
    commands = _systemctl_log(tmp_path)
    assert "--no-block restart jasper-outputd.service" in commands
    assert "--no-block restart jasper-aec-reconcile.service" in commands


def _stub_render_lib(tmp_path: Path, body: str) -> Path:
    """A drop-in jasper-asound-render.sh whose template renderer is overridable.

    Sources the real lib first (so jasper_asound_log_token et al. stay intact),
    then redefines jasper_asound_render_template with the supplied body so a
    test can drive the production failure shape — a card-less recognized DAC
    makes the real renderer fail closed (require_output_dac_card -> 64) BEFORE
    it opens the dest, which the reconciler must not paper over.
    """
    stub = tmp_path / "stub-asound-render.sh"
    real = ROOT / "deploy" / "lib" / "jasper-asound-render.sh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f"source {real}\n"
        "jasper_asound_render_template() {\n"
        f"{body}\n"
        "}\n",
        encoding="utf-8",
    )
    return stub


def test_render_failure_preserves_live_template_and_fails_loud(tmp_path: Path):
    """A failed template render must NOT clobber the working /etc/asound.conf.

    Regression for the silent-failure bug: render_asound_if_needed invoked
    jasper_asound_render_template and IGNORED its return value, then
    unconditionally `mv`'d the temp over the live template. Because the caller
    runs the function in a `&& render_changed=1` list, `set -e` is suppressed
    inside it, so a fail-closed render (e.g. card-less recognized DAC ->
    require_output_dac_card returns 64, dest never written) fell straight
    through to the mv and replaced a working ALSA config with an empty file on
    the audio OUTPUT path — exactly the class JTS forbids. Pin that on render
    failure the existing template is left byte-for-byte intact AND the failure
    is surfaced (event=...asound_render_failed).
    """
    good = "GOOD LIVE ALSA CONFIG — must survive a render failure\n"
    # Stub renderer mirrors the production failure: write nothing, return 64
    # (same exit code as require_output_dac_card on a card-less recognized DAC).
    stub = _stub_render_lib(tmp_path, "    return 64")
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "render-fail",
        initial_template=good,
        extra_env={"JASPER_ASOUND_RENDER_LIB": str(stub)},
    )

    assert result.returncode == 0, result.stderr
    # The live template is untouched — NOT emptied, NOT partially written.
    template_path = tmp_path / "asoundrc.jasper.template"
    assert template_path.read_text(encoding="utf-8") == good
    assert template_path.stat().st_size > 0
    # Failure is loud: the structured event fired and the dac8x->asound render
    # never reported success.
    assert "event=audio_hardware_reconcile.asound_render_failed" in result.stderr
    assert "preserved_existing=1" in result.stderr
    assert "event=audio_hardware_reconcile.asound_rendered" not in result.stderr
    # A clobber would have re-run the conf renderer against the empty template;
    # the preserve path must not.
    assert _render_log(tmp_path) == ""
    # No stray temp left behind next to the template.
    leftovers = list(template_path.parent.glob("asoundrc.jasper.template.*"))
    assert leftovers == [], leftovers


def test_render_empty_output_preserves_live_template(tmp_path: Path):
    """A renderer that 'succeeds' (rc 0) but emits an empty file must not win.

    Defense in depth for the same never-clobber-to-empty invariant: even if a
    future helper regression returns 0 while writing nothing, the reconciler
    rejects the empty render and keeps the working template rather than emitting
    silence to the speaker.
    """
    good = "GOOD LIVE ALSA CONFIG — survives an empty render\n"
    # rc 0 but the dest is truncated to empty.
    stub = _stub_render_lib(tmp_path, '    : > "$2"\n    return 0')
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "render-empty",
        initial_template=good,
        extra_env={"JASPER_ASOUND_RENDER_LIB": str(stub)},
    )

    assert result.returncode == 0, result.stderr
    template_path = tmp_path / "asoundrc.jasper.template"
    assert template_path.read_text(encoding="utf-8") == good
    assert "event=audio_hardware_reconcile.asound_render_failed" in result.stderr
    assert "event=audio_hardware_reconcile.asound_rendered" not in result.stderr
    assert _render_log(tmp_path) == ""


def test_render_success_still_writes_template(tmp_path: Path):
    """The happy path is unchanged: a valid render replaces the template.

    Guards against an over-eager fix that makes render_asound_if_needed treat
    every render as a failure. A normal recognized-DAC reconcile must still
    write the rendered outputd_dac block and run the conf renderer.
    """
    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--reason",
        "render-ok",
        initial_template="STALE PLACEHOLDER\n",
    )

    assert result.returncode == 0, result.stderr
    template = (tmp_path / "asoundrc.jasper.template").read_text(encoding="utf-8")
    assert "pcm.outputd_dac" in template
    assert "card sndrpihifiberry" in template
    _assert_no_empty_alsa_card(template)
    assert "event=audio_hardware_reconcile.asound_rendered" in result.stderr
    assert "event=audio_hardware_reconcile.asound_render_failed" not in result.stderr
    assert _render_log(tmp_path) == "render\n"


# --- #27 per-DAC latency floor emit ------------------------------------------


def test_reconcile_apple_emits_codified_latency_floor(tmp_path: Path):
    # An Apple dongle declares a measured floor; the reconciler emits all
    # profile-floor keys into the wizard-owned outputd.env (mirroring the
    # channel write). Route-owned content buffering is absent on the default
    # corrected route, so it stays on the packaged outputd default.
    result = _run_reconcile(tmp_path, APPLE_LISTING, "--reason", "test")

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_CAMILLA_CHUNKSIZE=256" in outputd_env
    assert "JASPER_CAMILLA_TARGET_LEVEL=1536" in outputd_env
    assert "JASPER_OUTPUTD_PERIOD_FRAMES=128" in outputd_env
    assert "JASPER_OUTPUTD_DAC_BUFFER_FRAMES=256" in outputd_env
    assert not _outputd_env_key_present(
        outputd_env, "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES"
    )
    assert (
        "event=audio_hardware_reconcile.latency_floor "
        "reason=test output_dac_id=apple_usb_c_dongle camilla_chunksize=256"
    ) in result.stderr


def test_reconciler_gets_latency_floor_actions_from_runtime_plan() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "jasper.cli.audio_config" in text
    assert "outputd-floor-actions" in text
    assert "validate-outputd-env" in text
    assert '--fanin-env "$FANIN_ENV_FILE"' in text
    assert '--camilla-statefile "$CAMILLA_STATEFILE"' in text
    assert '--camilla2-statefile "$CAMILLA2_STATEFILE"' in text
    assert "outputd-capture-device" in text
    assert '"outputd_active_content_capture"' not in text
    assert '"outputd_content_capture"' not in text
    assert "latency_floor_for_dac()" not in text
    assert "from jasper.audio_hardware.dac import latency_floor_for" not in text


def test_outputd_env_validation_rejects_active_writer_passive_reader(
    tmp_path: Path,
    capsys,
) -> None:
    from jasper.cli.audio_config import main as audio_config_main

    graph_env = _active_graph_env(tmp_path, channels=2)
    base_env = tmp_path / "jasper.env"
    base_env.write_text("JASPER_AUDIO_DAC_ID=hifiberry_dac8x\n", encoding="utf-8")
    outputd_env = tmp_path / "outputd.env"
    outputd_env.write_text(
        "JASPER_OUTPUTD_CONTENT_PCM=outputd_content_capture\n",
        encoding="utf-8",
    )
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text(
        "JASPER_FANIN_CAMILLA_COUPLING=loopback\n",
        encoding="utf-8",
    )

    result = audio_config_main(
        [
            "validate-outputd-env",
            "--base-env",
            str(base_env),
            "--outputd-env",
            str(outputd_env),
            "--fanin-env",
            str(fanin_env),
            "--camilla-statefile",
            graph_env["JASPER_CAMILLA_STATEFILE"],
            "--camilla2-statefile",
            str(tmp_path / "crossover-statefile.yml"),
            "--output-topology",
            graph_env["JASPER_OUTPUT_TOPOLOGY_PATH"],
        ]
    )

    assert result == 1
    assert "post-DSP route disconnected" in capsys.readouterr().out


def _outputd_env_key_present(outputd_env: str, key: str) -> bool:
    return any(
        re.match(rf"^\s*{re.escape(key)}\s*=", line)
        for line in outputd_env.splitlines()
    )


def test_reconcile_dac8x_clears_floor_keys_no_profile_floor(tmp_path: Path):
    # A DAC8x declares NO floor — the reconciler does not write the keys, so the
    # shipped global default applies. (When a prior DAC left a floor in
    # outputd.env, the keys are dropped — see
    # test_reconcile_no_floor_drops_stale_floor_keys.)
    result = _run_reconcile(tmp_path, DAC8X_AND_APPLE_LISTING, "--reason", "test")

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    # No floor for this DAC and no prior entry => the key is simply absent. It is
    # NOT written as an empty `KEY=`: an empty assignment in outputd.env (loaded
    # AFTER jasper.env) would override any operator value with empty.
    for key in (
        "JASPER_CAMILLA_CHUNKSIZE",
        "JASPER_CAMILLA_TARGET_LEVEL",
        "JASPER_OUTPUTD_PERIOD_FRAMES",
        "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES",
        "JASPER_OUTPUTD_DAC_BUFFER_FRAMES",
    ):
        assert not _outputd_env_key_present(outputd_env, key), key


def test_reconcile_no_floor_drops_stale_floor_keys(tmp_path: Path):
    # A DAC with no declared floor must DROP a stale floor a prior DAC wrote into
    # outputd.env, not leave it as `=''` (which would clobber an operator value)
    # and not leave the stale numbers.
    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--reason",
        "test",
        initial_outputd_env=(
            "JASPER_CAMILLA_CHUNKSIZE=256\n"
            "JASPER_CAMILLA_TARGET_LEVEL=1024\n"
            "JASPER_OUTPUTD_PERIOD_FRAMES=256\n"
            "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES=1024\n"
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES=512\n"
        ),
    )

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    for key in (
        "JASPER_CAMILLA_CHUNKSIZE",
        "JASPER_CAMILLA_TARGET_LEVEL",
        "JASPER_OUTPUTD_PERIOD_FRAMES",
        "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES",
        "JASPER_OUTPUTD_DAC_BUFFER_FRAMES",
    ):
        assert not _outputd_env_key_present(outputd_env, key), key


def test_reconcile_operator_env_override_survives_reconciler(tmp_path: Path):
    # The HIGH inversion fix: operator set JASPER_OUTPUTD_DAC_BUFFER_FRAMES (and
    # JASPER_CAMILLA_CHUNKSIZE) in jasper.env (loaded FIRST by the unit). The
    # reconciler must NOT write an empty `KEY=` into outputd.env (loaded AFTER),
    # which would override the operator's value with empty and make Rust fall
    # back to its default — silently discarding the tune. It must DROP the key
    # from outputd.env entirely so the operator's jasper.env value survives.
    # Keys the operator did NOT set still get the profile floor.
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_env=(
            "JASPER_CAMILLA_CHUNKSIZE=512\n"
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES=4096\n"
        ),
    )

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    # Operator-set keys: ABSENT from outputd.env (not `=''`) so jasper.env wins.
    assert not _outputd_env_key_present(outputd_env, "JASPER_CAMILLA_CHUNKSIZE")
    assert not _outputd_env_key_present(
        outputd_env, "JASPER_OUTPUTD_DAC_BUFFER_FRAMES"
    )
    # Non-overridden keys: profile floor still emitted.
    assert "JASPER_CAMILLA_TARGET_LEVEL=1536" in outputd_env
    assert "JASPER_OUTPUTD_PERIOD_FRAMES=128" in outputd_env


def test_reconcile_usb_low_latency_route_emits_content_buffer(tmp_path: Path):
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_env="JASPER_AUDIO_ROUTE_PROFILE=usb_low_latency_48k\n",
    )

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES=1536" in outputd_env
    assert "outputd_content_buffer_frames=1536" in result.stderr


def test_reconcile_refuses_invalid_outputd_candidate_and_preserves_prior(
    tmp_path: Path,
):
    prior_outputd = (
        "JASPER_OUTPUTD_BACKEND=alsa\n"
        "JASPER_OUTPUTD_SINK=single_alsa\n"
        "JASPER_OUTPUTD_PERIOD_FRAMES=128\n"
        "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES=1536\n"
    )
    overrides = tmp_path / "audio_runtime_overrides.json"
    overrides.write_text(
        json.dumps({
            "kind": "jts_audio_runtime_overrides",
            "schema_version": 1,
            "overrides": {
                "JASPER_OUTPUTD_PERIOD_FRAMES": {
                    "value": "1024",
                    "reason": "test invalid staged outputd env",
                },
                "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES": {
                    "value": "1536",
                    "reason": "test invalid staged outputd env",
                },
            },
        }),
        encoding="utf-8",
    )

    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_env="JASPER_AUDIO_ROUTE_PROFILE=usb_low_latency_48k\n",
        initial_outputd_env=prior_outputd,
        extra_env={"JASPER_AUDIO_RUNTIME_OVERRIDES_PATH": str(overrides)},
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "outputd.env").read_text(encoding="utf-8") == prior_outputd
    assert "event=audio_hardware_reconcile.outputd_env_invalid" in result.stderr
    assert "preserved=1" in result.stderr
    assert "JASPER_OUTPUTD_PERIOD_FRAMES=1024" not in (
        tmp_path / "outputd.env"
    ).read_text(encoding="utf-8")


def test_reconcile_refuses_invalid_dac_buffer_candidate_and_preserves_prior(
    tmp_path: Path,
):
    prior_outputd = (
        "JASPER_OUTPUTD_BACKEND=alsa\n"
        "JASPER_OUTPUTD_SINK=single_alsa\n"
        "JASPER_OUTPUTD_PERIOD_FRAMES=128\n"
        "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES=1536\n"
        "JASPER_OUTPUTD_DAC_BUFFER_FRAMES=256\n"
    )
    overrides = tmp_path / "audio_runtime_overrides.json"
    overrides.write_text(
        json.dumps({
            "kind": "jts_audio_runtime_overrides",
            "schema_version": 1,
            "overrides": {
                "JASPER_OUTPUTD_PERIOD_FRAMES": {
                    "value": "1024",
                    "reason": "test invalid staged dac buffer",
                },
                "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES": {
                    "value": "4096",
                    "reason": "keep content buffer valid",
                },
                "JASPER_OUTPUTD_DAC_BUFFER_FRAMES": {
                    "value": "256",
                    "reason": "test invalid staged dac buffer",
                },
            },
        }),
        encoding="utf-8",
    )

    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_outputd_env=prior_outputd,
        extra_env={"JASPER_AUDIO_RUNTIME_OVERRIDES_PATH": str(overrides)},
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "outputd.env").read_text(encoding="utf-8") == prior_outputd
    assert "event=audio_hardware_reconcile.outputd_env_invalid" in result.stderr
    assert "JASPER_OUTPUTD_DAC_BUFFER_FRAMES_256" in result.stderr
    assert "preserved=1" in result.stderr


def test_reconcile_operator_outputd_override_dropped_even_when_pre_seeded(
    tmp_path: Path,
):
    # Defense in depth for the HIGH fix: even when a PRIOR reconcile already
    # wrote the floor into outputd.env, a later reconcile that sees the operator
    # override in jasper.env must REMOVE the outputd.env copy (so the operator's
    # earlier-loaded value is no longer shadowed), not leave it `=''` or stale.
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_env="JASPER_OUTPUTD_DAC_BUFFER_FRAMES=4096\n",
        initial_outputd_env="JASPER_OUTPUTD_DAC_BUFFER_FRAMES=512\n",
    )

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert not _outputd_env_key_present(
        outputd_env, "JASPER_OUTPUTD_DAC_BUFFER_FRAMES"
    )


# --- defect D: split restart edge + storm breaker ----------------------------

# The usbsink target values for the usb_low_latency route (from
# route_owned_env_actions): pre-seeding these makes a run change ONLY fanin keys.
_USBSINK_USB_LL_ENV = (
    "JASPER_USBSINK_BLOCK_FRAMES=256\n"
    "JASPER_USBSINK_RING_PERIODS=3\n"
    "JASPER_USBSINK_LATENCY=low\n"
    "JASPER_USBSINK_OUTPUT_MODE=aloop\n"
)


def test_fanin_only_route_change_does_not_restart_usbsink(tmp_path: Path):
    # Defect D core fix: when only fanin.env keys move (usbsink.env already carries
    # the route's usbsink values), fan-in restarts but jasper-usbsink MUST NOT —
    # a usbsink restart rebuilds the gadget → udev → this reconciler → storm.
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_env="JASPER_AUDIO_ROUTE_PROFILE=usb_low_latency_48k\n",
        initial_usbsink_env=_USBSINK_USB_LL_ENV,
    )
    assert result.returncode == 0, result.stderr
    commands = _systemctl_log(tmp_path)
    # fan-in was restarted (its resampler keys changed).
    assert "restart jasper-fanin.service" in commands
    # jasper-usbsink was NOT try-restarted (no gadget rebuild → no storm).
    assert "try-restart jasper-usbsink.service" not in commands
    assert "usbsink_restarted=0" in result.stderr
    assert "fanin_restarted=1" in result.stderr


def test_route_change_touching_usbsink_keys_restarts_usbsink(tmp_path: Path):
    # The mirror: a clean box adopting usb_low_latency moves BOTH fanin and usbsink
    # keys, so both are restarted (the usbsink restart is legitimate here — the
    # gadget genuinely needs the new block/ring geometry).
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_env="JASPER_AUDIO_ROUTE_PROFILE=usb_low_latency_48k\n",
    )
    assert result.returncode == 0, result.stderr
    commands = _systemctl_log(tmp_path)
    assert "restart jasper-fanin.service" in commands
    assert "try-restart jasper-usbsink.service" in commands
    assert "usbsink_restarted=1" in result.stderr


def test_idempotent_second_run_makes_no_route_restart(tmp_path: Path):
    # A semantically-identical second run must not restart the route runtime at all
    # (canonical-form-stable change detection: nothing moved → nothing bounces).
    common = dict(
        initial_env="JASPER_AUDIO_ROUTE_PROFILE=usb_low_latency_48k\n",
    )
    first = _run_reconcile(tmp_path, APPLE_LISTING, "--reason", "test", **common)
    assert first.returncode == 0, first.stderr
    # Second run: fanin.env + usbsink.env already carry the route values from run 1.
    (tmp_path / "systemctl.log").write_text("", encoding="utf-8")
    second = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_env="JASPER_AUDIO_ROUTE_PROFILE=usb_low_latency_48k\n",
        initial_fanin_env=(tmp_path / "fanin.env").read_text(encoding="utf-8"),
        initial_usbsink_env=(tmp_path / "usbsink.env").read_text(encoding="utf-8"),
    )
    assert second.returncode == 0, second.stderr
    commands = _systemctl_log(tmp_path)
    assert "restart jasper-fanin.service" not in commands
    assert "try-restart jasper-usbsink.service" not in commands
    assert "fanin_restarted=0" in second.stderr
    assert "usbsink_restarted=0" in second.stderr


def test_usbsink_restart_is_rate_limited_within_window(tmp_path: Path):
    # Storm breaker: two usbsink-changing runs in quick succession — the second is
    # rate-limited (the state stamp from run 1 is < the window old), so the gadget
    # is NOT rebuilt a second time and the storm is broken.
    state = tmp_path / "usbsink-restart.stamp"
    extra = {"JASPER_USBSINK_RESTART_STATE_FILE": str(state)}
    first = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_env="JASPER_AUDIO_ROUTE_PROFILE=usb_low_latency_48k\n",
        extra_env=extra,
    )
    assert first.returncode == 0, first.stderr
    assert "try-restart jasper-usbsink.service" in _systemctl_log(tmp_path)
    assert state.exists(), "the first usbsink restart must stamp the state file"

    # Force a fresh usbsink change on run 2 (clear usbsink.env so the keys move
    # again), same short window. The limiter must refuse the usbsink restart.
    (tmp_path / "systemctl.log").write_text("", encoding="utf-8")
    (tmp_path / "usbsink.env").write_text("", encoding="utf-8")
    second = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_env="JASPER_AUDIO_ROUTE_PROFILE=usb_low_latency_48k\n",
        extra_env=extra,
    )
    assert second.returncode == 0, second.stderr
    commands = _systemctl_log(tmp_path)
    assert "try-restart jasper-usbsink.service" not in commands, (
        "the storm breaker must refuse a second gadget-rebuilding restart in the window"
    )
    assert "event=audio_hardware_reconcile.usbsink_restart_ratelimited" in second.stderr
    # Defect D: the refusal must NAME the resulting env↔daemon drift (not a silent
    # no-op) so it is observable in the journal — mirroring the doctor's
    # check_usbsink_env_drift surface. usbsink.env is now ahead of the daemon.
    assert "event=audio_hardware_reconcile.route_env_drift" in second.stderr


def test_usbsink_restart_allowed_after_window_elapses(tmp_path: Path):
    # The mirror: a stamp OLDER than the window does NOT block — a legitimate later
    # restart proceeds (the limiter is a storm damper, not a permanent gate).
    state = tmp_path / "usbsink-restart.stamp"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("100\n", encoding="utf-8")  # epoch 100 = far in the past
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_env="JASPER_AUDIO_ROUTE_PROFILE=usb_low_latency_48k\n",
        extra_env={
            "JASPER_USBSINK_RESTART_STATE_FILE": str(state),
            "JASPER_USBSINK_RESTART_MIN_INTERVAL_SEC": "600",
        },
    )
    assert result.returncode == 0, result.stderr
    assert "try-restart jasper-usbsink.service" in _systemctl_log(tmp_path)
    # The stamp was refreshed to a recent epoch (not the stale 100).
    assert state.read_text(encoding="utf-8").strip() != "100"
