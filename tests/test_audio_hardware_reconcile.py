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

import pytest

from jasper.audio_hardware.dac import final_edge_format_for
from jasper.fanin_coupling import RING_SLOT_FRAMES
from tests.reconcile_fixtures import (
    fake_systemctl as _fake_systemctl,
    systemctl_log as _systemctl_log,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-audio-hardware-reconcile"


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


def _fake_active_speaker_cli(tmp_path: Path) -> Path:
    """Default success seam for tests not about graph convergence itself."""
    fake = tmp_path / "jasper-active-speaker"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ -n \"${JASPER_FAKE_ACTIVE_SPEAKER_LOG:-}\" ]]; then\n"
        "  printf '%s\\n' \"$*\" >> \"$JASPER_FAKE_ACTIVE_SPEAKER_LOG\"\n"
        "fi\n"
        "if [[ -n \"${JASPER_FAKE_ACTIVE_SPEAKER_HOOK:-}\" ]]; then\n"
        "  exec \"$JASPER_FAKE_ACTIVE_SPEAKER_HOOK\" \"$@\"\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def _run_reconcile(
    tmp_path: Path,
    listing: str,
    *args: str,
    initial_env: str | None = None,
    initial_outputd_env: str | None = None,
    initial_fanin_env: str | None = None,
    initial_template: str | None = None,
    initial_boot_config: str | None = None,
    board_model: str = "Raspberry Pi 5 Model B Rev 1.0",
    active_usb_role: str = "peripheral",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    fake_systemctl, systemctl_log = _fake_systemctl(tmp_path)
    fake_aplay = _fake_aplay(tmp_path, listing)
    fake_renderer, render_log = _fake_renderer(tmp_path)
    fake_active_speaker = _fake_active_speaker_cli(tmp_path)
    source_template = tmp_path / "asoundrc.jasper.source"
    source_template.write_text(
        "__OUTPUTD_DAC_PCM_BLOCK__\n"
        "__OUTPUTD_DAC_CTL_BLOCK__\n"
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
    if initial_template is not None:
        (tmp_path / "asoundrc.jasper.template").write_text(
            initial_template,
            encoding="utf-8",
        )
    model = tmp_path / "model"
    boot_config = tmp_path / "config.txt"
    udc = tmp_path / f"udc-{active_usb_role}"
    model.write_text(board_model, encoding="utf-8")
    boot_config.write_text(
        initial_boot_config
        or "[all]\ndtoverlay=dwc2,dr_mode=peripheral\n",
        encoding="utf-8",
    )
    udc.mkdir(parents=True, exist_ok=True)
    if active_usb_role == "peripheral":
        (udc / "3f980000.usb").mkdir(exist_ok=True)

    env = os.environ.copy()
    env.update(
        {
            "JASPER_ENV_FILE": str(tmp_path / "jasper.env"),
            "JASPER_OUTPUTD_ENV_FILE": str(tmp_path / "outputd.env"),
            "JASPER_FANIN_ENV_FILE": str(tmp_path / "fanin.env"),
            "JASPER_ASOUND_SOURCE_TEMPLATE": str(source_template),
            "JASPER_ASOUND_TEMPLATE": str(tmp_path / "asoundrc.jasper.template"),
            "JASPER_ASOUND_CONF": str(tmp_path / "asound.conf"),
            "JASPER_AUDIO_QUALITY_FILE": str(audio_quality),
            "JASPER_RENDER_ASOUND_CONF": str(fake_renderer),
            "JASPER_RENDER_LOG": str(render_log),
            "JASPER_ACTIVE_SPEAKER_CLI": str(fake_active_speaker),
            "JASPER_SYSTEMCTL": str(fake_systemctl),
            "JASPER_SYSTEMCTL_LOG": str(systemctl_log),
            "JASPER_APLAY": str(fake_aplay),
            "JASPER_FAKE_APLAY_LISTING": str(tmp_path / "aplay-L.txt"),
            "JASPER_OUTPUT_HARDWARE_STATE_PATH": str(
                tmp_path / "output_hardware.json"
            ),
            "JASPER_I2S_HAT_INTENT_FILE": str(tmp_path / "i2s_hat.env"),
            "JASPER_I2S_HAT_REBOOT_REQUIRED_PATH": str(tmp_path / "i2s-reboot"),
            "JASPER_INSTALL_PROFILE_FILE": str(tmp_path / "install_profile"),
            "JASPER_OUTPUT_HARDWARE_PYTHON": sys.executable,
            "JASPER_PI_MODEL_FILE": str(model),
            "JTS_BOOT_CONFIG_FILE": str(boot_config),
            "JASPER_UDC_CLASS_DIR": str(udc),
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


def _log_token(value: str) -> str:
    """Mirror `jasper_asound_log_token`'s `tr -c 'A-Za-z0-9_.:,-' '_'`."""
    return re.sub(r"[^A-Za-z0-9_.:,-]", "_", value)


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

    # #2285 P2: staged at the ACTIVE RING, the one legal ACTIVE endpoint. This
    # used to stage `outputd_active_content_playback`; a graph naming that lane
    # is no longer a legal active graph, so the reconciler declines to arm and
    # falls through to the passive branch — which is the correct forward-only
    # behaviour for a box left on the retired lane, and the wrong INPUT for a
    # fixture whose whole subject is the armed path.
    from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE

    active_config = tmp_path / "active_speaker_baseline.yml"
    active_text = emit_active_speaker_baseline_config(
        preset,
        playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
        baseline_id=f"test-{channels}",
    )
    if channels not in {2, 4, 6}:
        active_text = active_text.replace(
            "channels: { in: 2, out: 2 }",
            f"channels: {{ in: 2, out: {channels} }}",
        ).replace(
            f'channels: 2\n    device: "{RING_ACTIVE_PLAYBACK_DEVICE}"',
            f'channels: {channels}\n    device: "{RING_ACTIVE_PLAYBACK_DEVICE}"',
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
    # #2285 P2: camilla#2's endpoint is the ACTIVE RING, for the same reason
    # `_active_graph_env` stages it there — a graph naming the retired snd-aloop
    # lane is no longer a legal active graph and the gate declines to arm it.
    from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE

    crossover_config = tmp_path / "grouping_active_leader_crossover.yml"
    crossover_config.write_text(
        emit_active_speaker_driver_domain_config(
            preset,
            playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
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


INNOMAKER_LISTING = """
hw:CARD=sndrpimerusamp,DEV=0
    snd_rpi_merus_amp, Merus Audio Amp ma120x0p-amp-0
"""


def test_i2s_reboot_marker_is_created_only_by_the_boot_setting_change(
    tmp_path: Path,
):
    model = "Raspberry Pi Zero 2 W Rev 1.0"
    (tmp_path / "install_profile").write_text("streambox\n", encoding="utf-8")
    intent = tmp_path / "i2s_hat.env"
    marker = tmp_path / "i2s-reboot"
    intent.write_text(
        "JASPER_I2S_HAT_PROFILE=innomaker_hifi_amp_pro\n",
        encoding="utf-8",
    )

    first = _run_reconcile(tmp_path, "", "--reason", "hat-enable", initial_boot_config="[all]\ndtoverlay=dwc2,dr_mode=host\n", board_model=model, active_usb_role="host")
    applied_boot = (tmp_path / "config.txt").read_text(encoding="utf-8")
    assert first.returncode == 0, first.stderr
    assert marker.is_file()
    assert "dtoverlay=dwc2,dr_mode=peripheral" in applied_boot and "output_parked" in first.stderr

    def rerun(listing: str = "", *, reason: str = "udev", **kwargs):
        return _run_reconcile(
            tmp_path, listing, "--reason", reason, initial_boot_config=applied_boot,
            board_model=model, active_usb_role="peripheral", **kwargs,
        )

    marker.unlink()  # a reboot naturally clears /run
    second = rerun(reason="boot")
    assert second.returncode == 0, second.stderr
    assert not marker.exists()  # missing hardware does not recreate it

    marker.touch()
    third = rerun()
    assert third.returncode == 0, third.stderr
    assert marker.is_file()  # unrelated reconciles leave a pending marker alone

    (tmp_path / "systemctl.log").unlink(missing_ok=True)
    matched = rerun(INNOMAKER_LISTING)
    assert matched.returncode == 0, matched.stderr
    assert not marker.exists()  # desired and runtime now agree
    commands = _systemctl_log(tmp_path)
    assert "--no-block restart jasper-outputd.service" in commands
    assert "stop jasper-voice.service" not in commands and "restart jasper-aec-reconcile.service" not in commands

    malformed_python = tmp_path / "malformed-python"
    malformed_python.write_text(
        "#!/bin/sh\ncase \"$*\" in\n"
        f"*jasper.output_hardware*) \"{sys.executable}\" \"$@\" | sed 's/}}$//'; exit 0;;\n"
        f'esac\nPYTHONOPTIMIZE=1 exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    malformed_python.chmod(0o755)
    intent.unlink()
    for expected, listing, extra_env in (
        (None, INNOMAKER_LISTING, {"JASPER_OUTPUT_HARDWARE_STATE_PATH": str(tmp_path)}),
        (None, INNOMAKER_LISTING + DAC8X_AND_APPLE_LISTING, {"JASPER_OUTPUT_HARDWARE_PYTHON": str(malformed_python)}),
        (True, INNOMAKER_LISTING + DAC8X_AND_APPLE_LISTING, None),
    ):
        for marker_present in (False, True):
            marker.unlink(missing_ok=True)
            if marker_present:
                marker.touch()
            observed = rerun(listing, extra_env=extra_env)
            assert observed.returncode == 0, observed.stderr
            assert marker.exists() is (marker_present if expected is None else expected)
            assert "dtoverlay=merus-amp" not in (tmp_path / "config.txt").read_text()
            assert "dtoverlay=dwc2,dr_mode=host" in (tmp_path / "config.txt").read_text()

    disabled_boot = (tmp_path / "config.txt").read_text(encoding="utf-8")
    marker.unlink()
    parked = _run_reconcile(tmp_path, "", initial_boot_config=disabled_boot, board_model=model, active_usb_role="host")
    assert parked.returncode == 0 and not marker.exists() and "output_parked" in parked.stderr, parked.stderr


def test_published_not_durable_boot_change_still_sets_marker(tmp_path: Path):
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "*usb_port_role*) echo '{\"board_topology\": \"separate_host_ports\", "
        "\"i2s_hat_profile\": \"innomaker_hifi_amp_pro\", "
        "\"i2s_hat_boot_config_changed\": true, "
        "\"boot_config_published_not_durable\": true}'; exit 74;;\n"
        "*jasper.output_hardware*) echo '{\"profile_id\": \"unknown\", "
        "\"status\": \"unavailable\"}'; exit 0;;\n"
        f'esac\nexec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    result = _run_reconcile(
        tmp_path, "", extra_env={"JASPER_OUTPUT_HARDWARE_PYTHON": str(fake_python)}
    )

    assert result.returncode == 74
    assert (tmp_path / "i2s-reboot").is_file()
    assert "error=boot_config_published_not_durable" in result.stderr


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


def test_reconcile_innomaker_uses_registry_identity_and_renders_raw_hw(
    tmp_path: Path,
):
    result = _run_reconcile(
        tmp_path,
        INNOMAKER_LISTING,
        "--reason",
        "test",
    )

    assert result.returncode == 0, result.stderr
    env_text = (tmp_path / "jasper.env").read_text(encoding="utf-8")
    assert "JASPER_AUDIO_DAC_ID=innomaker_hifi_amp_pro" in env_text
    assert "JASPER_AUDIO_DAC_CARD=sndrpimerusamp" in env_text
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=''" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_LANE=''" in outputd_env
    # Registry-declared final-edge format. LIVE since the outputd read:
    # outputd parks at exit 78 on anything outside
    # {S16_LE, S24_3LE, S32_LE, empty},
    # REQUESTS this format on its DAC PCM, and parks if the device installs a
    # different one. STATUS dac.format then reports what outputd's client edge
    # negotiated (not an echo of this value), and the chip-AEC alignment
    # identity records that. This coupling is now the ONLY place the
    # registry's declared format reaches the hardware edge: PR-4
    # (format-foundation) deleted the render's own pinned-slave copy, so a
    # raw `hw:` open is what outputd's format request lands on.
    assert "JASPER_OUTPUTD_DAC_FORMAT=S32_LE" in outputd_env
    assert final_edge_format_for("innomaker_hifi_amp_pro") == "S32_LE"
    template = (tmp_path / "asoundrc.jasper.template").read_text(encoding="utf-8")
    # No profile-scoped plug anymore: InnoMaker renders identically to every
    # other recognized single DAC, a raw hw alias to the detected card.
    assert "type plug" not in template
    assert "type hw" in template
    assert "card sndrpimerusamp" in template
    assert "device 0" in template
    assert _render_log(tmp_path) == "render\n"


def test_reconcile_innomaker_stays_passive_without_a_legal_active_graph(
    tmp_path: Path,
):
    """THE FAIL-CLOSED ACTIVATION PROPERTY — the reason flipping the InnoMaker's
    lane flag cannot change any running box.

    Declaring the lane makes ``active_lane_channels_for_dac`` return 2, which
    only means the width gate now RUNS. Active mode still needs a legal active
    graph to already be the live CamillaDSP config, which only commissioning
    produces. With no statefile staged the gate declines, and the box resolves
    byte-identically passive: stereo content lane, no width, no active-lane
    marker.

    The journal token proves WHICH state this is. It used to read
    ``dac_no_active_lane`` (the gate never ran); it must now name the gate's own
    decline reason, because "choose a different layout at /sound/setup/" is no
    longer the remedy — commissioning is.
    """
    result = _run_reconcile(
        tmp_path,
        INNOMAKER_LISTING,
        "--reason",
        "test",
    )

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env
    assert "JASPER_OUTPUTD_CONTENT_PCM" not in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=''" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_LANE=''" in outputd_env
    # The gate RAN and declined — it is no longer skipped for this DAC.
    assert "active_graph=camilla_statefile_missing" in result.stderr
    assert "active_graph=dac_no_active_lane" not in result.stderr
    assert "active_graph=none" not in result.stderr


def _lane_query_python(tmp_path: Path, *, name: str, action: str) -> Path:
    """Real python for everything EXCEPT the lane-cap registry query.

    The reconciler feeds that query as a heredoc on stdin (``python -`` ), so
    the shim has to route on stdin content rather than argv the way the other
    fake pythons here do. ``action`` is shell run in place of the real query:
    ``exit 1`` simulates the spawn dying, a preamble+passthrough simulates a
    different registry. Every other spawn — crucially the
    ``jasper.output_hardware`` recognition probe — still runs for real, which
    is what puts us on the recognized-single-DAC branch.
    """

    shim = tmp_path / name
    shim.write_text(
        "#!/bin/bash\n"
        'if [ "${1:-}" = "-" ]; then\n'
        '  src="$(cat)"\n'
        '  case "$src" in\n'
        f"    *active_outputd_lane_channels_for*) {action} ;;\n"
        "  esac\n"
        f'  printf %s "$src" | "{sys.executable}" "$@"\n'
        "  exit $?\n"
        "fi\n"
        f'exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


def test_reconcile_lane_probe_failure_is_named_apart_from_a_lane_less_dac(
    tmp_path: Path,
):
    """A TRANSIENT failure of the lane-cap probe must not be reported as "this
    DAC has no active lane".

    ``active_lane_channels_for_dac`` swallows its own spawn failure
    (``2>/dev/null || true``), so an OOM-kill or fork-EAGAIN hitting THAT one
    spawn — the mid-install window AGENTS.md warns about — yields an empty cap
    while every other probe succeeds and the DAC is still RECOGNIZED. Before
    this token split that landed on ``dac_no_active_lane``, whose remedy
    ("re-running cannot change it; choose a different layout") is FALSE here:
    the condition is transient and the next reconcile pass converges.

    Resolving passive is still the correct fail-closed outcome — only the
    reported reason changes.
    """
    result = _run_reconcile(
        tmp_path,
        INNOMAKER_LISTING,
        "--reason",
        "test",
        extra_env={
            "JASPER_OUTPUT_HARDWARE_PYTHON": str(
                _lane_query_python(tmp_path, name="flaky-python", action="exit 1")
            )
        },
    )

    assert result.returncode == 0, result.stderr
    assert "active_graph=lane_probe_failed" in result.stderr
    assert "active_graph=dac_no_active_lane" not in result.stderr
    # Fail-closed is unchanged: the box is passive either way.
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=''" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_LANE=''" in outputd_env


@pytest.mark.parametrize(
    "stale",
    ["JASPER_OUTPUTD_CONTENT_PCM=''\n", "JASPER_OUTPUTD_CONTENT_PCM=outputd_content_capture\n"],
)
def test_reconcile_removes_a_stale_content_pcm_line(tmp_path: Path, stale: str):
    """A box that reconciled before ADR-0100 carries the retired key; ONE
    reconcile must drop the LINE, not merely stop restating it.

    The writes are gone, and set_env_file_var_if_changed is a per-key upsert — a
    key nobody writes is never touched again — so without an active removal the
    leftover outlives the lane forever. Present-but-EMPTY is the shape that
    bites: jasper.audio_runtime_plan's retired-route describer defaults on an
    ABSENT key, so an empty one reports a post-DSP route disconnection that no
    later reconcile could clear. Both spellings must heal to absent.
    """
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_outputd_env=stale,
    )

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_CONTENT_PCM" not in outputd_env


def test_reconcile_lane_less_profile_still_reports_dac_no_active_lane(
    tmp_path: Path,
):
    """The other side of the split: a DAC whose profile genuinely declares no
    lane keeps the actionable token.

    Exercises the REAL resolver against a patched registry — the shim prepends
    a preamble that rewrites the InnoMaker entry to a lane-less clone, then
    runs the reconciler's own query heredoc — so this pins the `none` sentinel
    end to end rather than faking the resolver's answer. That is the token's
    surviving population: the next passive-only board the registry meets.
    """
    preamble = (
        "import dataclasses as _dc;"
        "import jasper.audio_hardware.dac as _d;"
        "_p = _dc.replace("
        "_d.INNOMAKER_HIFI_AMP_PRO,"
        " supports_active_outputd_lane=False,"
        " active_outputd_lane_channels=None);"
        "_d._BY_ID = {**_d._BY_ID, _p.id: _p}"
    )
    result = _run_reconcile(
        tmp_path,
        INNOMAKER_LISTING,
        "--reason",
        "test",
        extra_env={
            "JASPER_OUTPUT_HARDWARE_PYTHON": str(
                _lane_query_python(
                    tmp_path,
                    name="lane-less-python",
                    # Prepend the registry patch, then run the real query.
                    action=f'src="{preamble}"$\'\\n\'"$src"',
                )
            )
        },
    )

    assert result.returncode == 0, result.stderr
    assert "active_graph=dac_no_active_lane" in result.stderr
    assert "active_graph=lane_probe_failed" not in result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=''" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_LANE=''" in outputd_env


def test_reconcile_innomaker_arms_the_width_two_lane_on_a_legal_active_graph(
    tmp_path: Path,
):
    """The other half of the gate: with a legal width-2 active graph live, the
    InnoMaker arms the active lane at exactly that width.

    Drive-what-we-use — the emitted width is the config's ACTUAL driven width,
    and the explicit ``JASPER_OUTPUTD_ACTIVE_LANE=1`` marker fences off the
    stereo-only TTS mixer / rate-match so full-range audio cannot reach a bare
    tweeter through outputd.
    """
    result = _run_reconcile(
        tmp_path,
        INNOMAKER_LISTING,
        "--reason",
        "test",
        extra_env=_active_graph_env(tmp_path, channels=2),
    )

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env
    # #2285 P2 (A6): a ROLEFUL box reaches outputd over the ACTIVE RING,
    # which outputd reads as a FILE — it opens no content PCM at all.
    # ADR-0100 retired the key with the lane; the reconciler states it
    # nowhere, and the retired capture name appears nowhere either.
    assert "JASPER_OUTPUTD_CONTENT_PCM" not in outputd_env
    assert "outputd_active_content_capture" not in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=2" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_LANE=1" in outputd_env
    # The declared final-edge format is unchanged by arming the lane.
    assert "JASPER_OUTPUTD_DAC_FORMAT=S32_LE" in outputd_env
    assert "mode=single_alsa_active" in result.stderr
    assert "active_channels=2" in result.stderr
    assert "active_lane_cap=2" in result.stderr


def test_reconcile_apple_role_enables_apple_helpers_and_renders(tmp_path: Path):
    result = _run_reconcile(tmp_path, APPLE_LISTING, "--reason", "test")

    assert result.returncode == 0, result.stderr
    env_text = (tmp_path / "jasper.env").read_text(encoding="utf-8")
    assert "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle" in env_text
    assert "JASPER_AUDIO_DAC_CARD=A" in env_text
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env
    # Registry-declared final-edge format. LIVE since the outputd read:
    # outputd parks at exit 78 on anything outside
    # {S16_LE, S24_3LE, S32_LE, empty},
    # REQUESTS this format on its DAC PCM, and parks if the device installs a
    # different one. STATUS dac.format then reports what outputd's client edge
    # negotiated (not an echo of this value), and the chip-AEC alignment
    # identity records that.
    #
    # wide-output-path PR-8 b3: the packed 24-bit edge, which the dongle's USB
    # descriptor advertises (`aplay -D hw:A --dump-hw-params` -> S16_LE S24_3LE)
    # and a live `aplay -D hw:A -f S24_3LE` open confirmed on jts.local
    # (2026-08-08). This is the SINGLE-dongle arm; the dual-Apple composite arm
    # emits its own S16_LE declaration instead, asserted in
    # test_reconcile_dual_apple_pins_pcm_order_from_saved_topology (the one
    # test that already drives the composite emit arm end to end).
    assert "JASPER_OUTPUTD_DAC_FORMAT=S24_3LE" in outputd_env
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
    # The monitor is ensured idempotently, never restarted: this gate runs on
    # every udev/reconcile pass and a deploy fires it repeatedly inside the
    # unit's StartLimitIntervalSec. A restart-per-pass burned StartLimitBurst
    # and parked the monitor 'start-limit-hit'. reset-failed clears any parked
    # state; start is a no-op when it is already running.
    assert "reset-failed jasper-headphone-monitor.service" in commands
    assert "start jasper-headphone-monitor.service" in commands
    assert "restart jasper-headphone-monitor.service" not in commands
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


def test_reconcile_script_selects_the_final_graph_before_outputd_gating() -> None:
    """One root path renders, applies, then derives outputd's active lane."""
    code = "\n".join(
        line
        for line in SCRIPT.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "runtime-safe-graph" in code
    assert "converge_runtime_graph" in code
    # rindex targets the execution block, rather than the function definitions.
    assert code.rindex("render_flat_cutover_if_needed") < code.rindex(
        "converge_runtime_graph"
    )
    assert code.rindex("converge_runtime_graph") < code.rindex("gate_role_services")


def test_runtime_convergence_only_writes_statefile_when_camilla_is_active(
    tmp_path: Path,
) -> None:
    cli_log = tmp_path / "active-speaker.log"

    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        extra_env={"JASPER_FAKE_ACTIVE_SPEAKER_LOG": str(cli_log)},
    )

    assert result.returncode == 0, result.stderr
    call = cli_log.read_text(encoding="utf-8")
    assert "runtime-safe-graph" in call
    assert "--write-statefile" in call
    assert "--apply-live" not in call
    assert "--preserve-live-transport" not in call
    assert "is-active --quiet jasper-camilla.service" not in SCRIPT.read_text(
        encoding="utf-8"
    )


def test_reconcile_refuses_a_post_convergence_outputd_rejection() -> None:
    """The second candidate is the final safety gate, not a best-effort write."""
    code = SCRIPT.read_text(encoding="utf-8")
    final_commit = code.rindex("if commit_outputd_env_stage; then")
    rejection = code.index(
        'reason=post_convergence_outputd_env_rejected', final_commit
    )
    gate = code.index("gate_role_services", final_commit)

    assert final_commit < rejection < gate
    assert 'runtime_converge_failed=1' in code[final_commit:gate]
    assert "exit 78" in code[final_commit:gate]


def test_camilla_boot_requires_successful_runtime_graph_convergence() -> None:
    """A stale statefile cannot start Camilla after a failed boot reconcile."""
    camilla_unit = (
        ROOT / "deploy" / "systemd" / "jasper-camilla.service"
    ).read_text(encoding="utf-8")
    hardware_unit = (
        ROOT
        / "deploy"
        / "systemd"
        / "jasper-audio-hardware-reconcile.service"
    ).read_text(encoding="utf-8")

    assert "Requires=jasper-audio-hardware-reconcile.service" in camilla_unit
    after_line = next(
        line for line in camilla_unit.splitlines() if line.startswith("After=")
    )
    assert "jasper-audio-hardware-reconcile.service" in after_line
    # The required oneshot runs the same reconciler whose final command status
    # is nonzero when runtime convergence fails.
    assert (
        "ExecStart=/usr/local/sbin/jasper-audio-hardware-reconcile"
        in hardware_unit
    )
    assert '[[ "$runtime_converge_failed" == "0" ]]' in SCRIPT.read_text(
        encoding="utf-8"
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
        "defaults.pcm.rate_converter \"__RATE_CONVERTER__\"\n"
    )
    outputd_env = (
        "JASPER_OUTPUTD_BACKEND=alsa\n"
        "JASPER_OUTPUTD_SINK=single_alsa\n"
        "JASPER_OUTPUTD_DAC_PCM=outputd_dac\n"
        "JASPER_OUTPUTD_DUAL_DAC_A_PCM=''\n"
        "JASPER_OUTPUTD_DUAL_DAC_B_PCM=''\n"
        # The coupling-derived content-lane width (S32_LE on a loopback box) is
        # part of the steady state too — see test_reconcile_emits_content_format_*.
        "JASPER_OUTPUTD_CONTENT_FORMAT=S32_LE\n"
        # The registry-declared final-edge format (LIVE: outputd reads it and
        # parks at exit 78 on an unknown value) is part of the steady state —
        # seed it so a second reconcile is a true no-op. The Apple dongle's
        # steady state is the packed S24_3LE edge (wide-output-path PR-8 b3).
        "JASPER_OUTPUTD_DAC_FORMAT=S24_3LE\n"
        # The single stereo path now also manages the wide-lane width knob,
        # cleared so a stale active width can't mis-size the stereo lane.
        "JASPER_OUTPUTD_ACTIVE_CHANNELS=''\n"
        # A passive stereo sink is not an active-crossover lane, so the
        # active-lane marker is cleared here too. Seeding it keeps the
        # steady state truly unchanged (no spurious outputd restart).
        "JASPER_OUTPUTD_ACTIVE_LANE=''\n"
        # Its PAIR. The two are written together by one helper from one
        # decision, so the steady state states both or the next reconcile
        # reports a change. (A box deployed before this key existed writes it
        # once, on its first reconcile, and is idempotent from then on.)
        "JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT=''\n"
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
    assert "JASPER_FANIN_INPUT_RESAMPLER=enabled" in fanin_env
    assert "JASPER_FANIN_INPUT_RESAMPLER_LANE=usbsink" in fanin_env
    assert "JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES=512" in fanin_env
    assert "JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES=1536" in fanin_env
    assert "JASPER_FANIN_INPUT_RESAMPLER_RING_FRAMES=4096" in fanin_env


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
    usb_role = json.loads(state_text)["usb_data_role"]
    assert usb_role["desired_role"] == "peripheral"
    assert usb_role["gadget_available"] is True
    template = (tmp_path / "asoundrc.jasper.template").read_text(encoding="utf-8")
    _assert_parked_outputd_dac_template(template)
    assert _render_log(tmp_path) == "render\n"
    commands = _systemctl_log(tmp_path)
    assert "enable jasper-dac-init.service jasper-headphone-monitor.service" in commands
    assert "--no-block stop jasper-voice.service jasper-outputd.service" in commands
    assert "event=audio_hardware_reconcile.dual_apple_detected" in result.stderr
    assert (
        "event=hardware.usb_role_resolved topology=separate_host_ports "
        "desired=peripheral active=peripheral gadget_available=true "
        "management_transport_available=true reason=available"
    ) in result.stderr
    _assert_publications_agree(tmp_path)



def _dual_apple_active_topology(tmp_path: Path) -> Path:
    """Save the ACTIVE roleful topology of a commissioned dual-Apple speaker.

    Unlike ``_dual_apple_topology`` this one is a legal active-speaker topology
    (roleful groups plus passed clock evidence), so the active-graph gate can
    accept a staged graph against it and the composite arm can reach
    ``recognized=1``. Both the child-order test and the partial-presence tests
    need exactly this shape.
    """
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
    return topology_path


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
    topology_path = _dual_apple_active_topology(tmp_path)

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
    # The composite's OWN declaration reaches outputd, not its children's. Both
    # children of this composite are the Apple dongle profile, which declares
    # the packed S24_3LE edge (wide-output-path PR-8 b3) — and outputd's paired
    # composite sink has NO packed-24 child write path: `ChildPeriods::new`
    # refuses that width and `PairedCompositeSink::new` parks the unit at
    # EX_CONFIG 78 before either dongle opens (#2249). So an S24_3LE emitted
    # here is a silent speaker on every dual-Apple box. This assertion is the
    # tripwire for the whole single-vs-composite split model: it fails the
    # moment the emission starts resolving through child_profile_ids.
    assert "JASPER_OUTPUTD_DAC_FORMAT=S16_LE" in outputd_env
    assert "JASPER_OUTPUTD_DAC_FORMAT=S24_3LE" not in outputd_env
    # A wide composite sink (4ch) is already fenced off outputd's stereo-only
    # features by its channel width, so the reconciler does NOT set the 2-ch
    # WIDTH knob here — it stays cleared.
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=''" in outputd_env
    # The lane PAIR is staged, because the accepted graph names the ACTIVE
    # RING: a composite with a legal active graph is a RING composite. The two
    # markers are one fact, so both are asserted — outputd bails at startup on
    # an incoherent pair.
    assert "JASPER_OUTPUTD_ACTIVE_LANE=1" in outputd_env
    assert "JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT=1" in outputd_env
    template = (tmp_path / "asoundrc.jasper.template").read_text(encoding="utf-8")
    assert "pcm.outputd_dac" in template
    assert "type null" in template
    assert "ctl.outputd_dac" not in template
    _assert_no_empty_alsa_card(template)
    assert "order_source=saved_topology" in result.stderr


def _output_hardware_record(tmp_path: Path) -> dict:
    return json.loads(
        (tmp_path / "output_hardware.json").read_text(encoding="utf-8")
    )


def test_reconcile_parks_a_declared_composite_missing_one_child(tmp_path: Path):
    """A saved composite with one dongle gone parks instead of taking over.

    Before #2813 the surviving dongle classified as an ordinary
    ``apple_usb_c_dongle``, ``apply_observed_single_policy`` marked it
    recognized, and the final output was rewired onto it as a plain stereo
    DAC. The graph layer never followed — it reads only the saved topology —
    so the box stayed quiet, but by nobody's decision and with nothing said.
    This pins the decision: park, and name the child that is gone.
    """
    sys_class, proc_asound = _fake_sys_output_card(
        tmp_path,
        card_index=1,
        card_id="A",
        usb_path="1-1",
        serial="left",
    )
    _dual_apple_active_topology(tmp_path)

    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        extra_env={
            "JASPER_SYS_CLASS_SOUND": str(sys_class),
            "JASPER_PROC_ASOUND": str(proc_asound),
            "JASPER_OUTPUT_TOPOLOGY_PATH": str(tmp_path / "output_topology.json"),
            **_active_graph_env(tmp_path, write_topology=False),
        },
    )

    assert result.returncode == 0, result.stderr
    record = _output_hardware_record(tmp_path)
    assert record["status"] == "partial"
    blockers = [
        issue for issue in record["issues"] if issue["severity"] == "blocker"
    ]
    assert [issue["code"] for issue in blockers] == [
        "saved_composite_partially_present"
    ]
    # The household-visible reason names the child that is gone.
    assert "right" in blockers[0]["message"]
    env_text = (tmp_path / "jasper.env").read_text(encoding="utf-8")
    # The degraded state this issue is about: NOT recognized as a plain dongle.
    assert "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle" not in env_text
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    # The parked markers, not a live edge onto the surviving dongle.
    assert "JASPER_OUTPUTD_BACKEND=fake" in outputd_env
    assert "JASPER_OUTPUTD_BACKEND=alsa" not in outputd_env
    assert "JASPER_OUTPUTD_DAC_FORMAT=''" in outputd_env
    template = (tmp_path / "asoundrc.jasper.template").read_text(encoding="utf-8")
    _assert_parked_outputd_dac_template(template)
    commands = _systemctl_log(tmp_path)
    assert "--no-block stop jasper-voice.service jasper-outputd.service" in commands
    assert "--no-block restart jasper-outputd.service" not in commands
    assert (
        "event=audio_hardware_reconcile.runtime_env reason=test mode=parked"
    ) in result.stderr
    # The reason reaches the JOURNAL, not just the record: an operator reading
    # `output_parked` sees WHY, not only `recognized=0`.
    assert (
        "event=audio_hardware_reconcile.output_parked reason=test "
        "output_dac_id=unknown output_dac_card=A recognized=0 "
        "observed_blockers=saved_composite_partially_present"
    ) in result.stderr
    _assert_publications_agree(tmp_path)



def test_reconcile_unparks_when_the_missing_composite_child_returns(
    tmp_path: Path,
):
    """Recovery is the udev chain re-running this script — no operator step."""
    sys_class, proc_asound = _fake_sys_output_card(
        tmp_path,
        card_index=1,
        card_id="A",
        usb_path="1-1",
        serial="left",
    )
    _dual_apple_active_topology(tmp_path)
    extra_env = {
        "JASPER_SYS_CLASS_SOUND": str(sys_class),
        "JASPER_PROC_ASOUND": str(proc_asound),
        "JASPER_OUTPUT_TOPOLOGY_PATH": str(tmp_path / "output_topology.json"),
        **_active_graph_env(tmp_path, write_topology=False),
    }

    parked = _run_reconcile(
        tmp_path, APPLE_LISTING, "--reason", "test", extra_env=extra_env,
    )
    assert parked.returncode == 0, parked.stderr
    assert _output_hardware_record(tmp_path)["status"] == "partial"
    commands_before = len(_systemctl_log(tmp_path))

    # The missing dongle comes back; udev re-runs the reconciler.
    _fake_sys_output_card(
        tmp_path,
        card_index=2,
        card_id="B",
        usb_path="1-2",
        serial="right",
    )
    recovered = _run_reconcile(
        tmp_path, DUAL_APPLE_LISTING, "--reason", "test", extra_env=extra_env,
    )

    assert recovered.returncode == 0, recovered.stderr
    record = _output_hardware_record(tmp_path)
    assert record["status"] == "ready"
    assert record["profile_id"] == "dual_apple_usb_c_dac_4ch"
    assert record["issues"] == []
    env_text = (tmp_path / "jasper.env").read_text(encoding="utf-8")
    assert "JASPER_AUDIO_DAC_ID=dual_apple_usb_c_dac_4ch" in env_text
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_BACKEND=alsa" in outputd_env
    assert "JASPER_OUTPUTD_SINK=dual_apple" in outputd_env
    assert "JASPER_OUTPUTD_DUAL_DAC_A_PCM=hw:CARD=A,DEV=0" in outputd_env
    assert "JASPER_OUTPUTD_DUAL_DAC_B_PCM=hw:CARD=B,DEV=0" in outputd_env
    commands = _systemctl_log(tmp_path)[commands_before:]
    assert "--no-block restart jasper-outputd.service" in commands
    assert "--no-block stop jasper-voice.service jasper-outputd.service" not in commands


def test_reconcile_saved_single_topology_still_takes_the_single_dongle(
    tmp_path: Path,
):
    """A saved SINGLE topology keeps today's behaviour: stereo is legal there.

    Named for the single *device* it saves. The passive **composite** case —
    `kind == "composite"` but no per-driver DSP, where the park must also stand
    down — is a record-level decision and is pinned in
    ``test_saved_passive_composite_missing_a_child_still_plays``.
    """
    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text(
        json.dumps({
            "artifact_schema_version": 1,
            "kind": "jts_output_topology",
            "topology_id": "solo",
            "name": "Solo",
            "status": "ready",
            "hardware": {
                "device_id": "apple_usb_c_dongle",
                "device_label": "Apple USB-C audio adapter",
                "physical_output_count": 2,
                "card_id": "A",
                "outputs": [],
            },
            "speaker_groups": [],
            "routing": {},
            "safety": {},
        }),
        encoding="utf-8",
    )

    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        extra_env={"JASPER_OUTPUT_TOPOLOGY_PATH": str(topology_path)},
    )

    assert result.returncode == 0, result.stderr
    assert _output_hardware_record(tmp_path)["status"] == "ready"
    env_text = (tmp_path / "jasper.env").read_text(encoding="utf-8")
    assert "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle" in env_text
    assert "JASPER_AUDIO_DAC_CARD=A" in env_text
    commands = _systemctl_log(tmp_path)
    assert "enable jasper-dac-init.service jasper-headphone-monitor.service" in commands
    assert "--no-block stop jasper-voice.service jasper-outputd.service" not in commands


def _dual_apple_topology(tmp_path: Path) -> Path:
    """Write the saved output topology that pins the composite's child order.

    Without it ``apply_observed_composite_policy`` parks at
    ``park_unstable_child_order`` before it ever reaches the active-graph gate,
    so any test that needs the gate to run has to stage this first.
    """
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
    return topology_path


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
    topology_path = _dual_apple_topology(tmp_path)

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
    assert "JASPER_OUTPUTD_CONTENT_PCM" not in outputd_env
    assert "JASPER_OUTPUTD_DUAL_DAC_A_PCM=''" in outputd_env
    # Parked/unrecognized: no profile to query, so the declared format clears
    # too — explicit empty, matching how ACTIVE_CHANNELS/ACTIVE_LANE clear
    # elsewhere in this same branch.
    assert "JASPER_OUTPUTD_DAC_FORMAT=''" in outputd_env
    state_text = (tmp_path / "output_hardware.json").read_text(encoding="utf-8")
    assert '"profile_id": "dual_apple_usb_c_dac_4ch"' in state_text
    assert "action=park_until_active_graph" in result.stderr
    assert "reason=camilla_statefile_missing" in result.stderr
    template = (tmp_path / "asoundrc.jasper.template").read_text(encoding="utf-8")
    _assert_parked_outputd_dac_template(template)
    assert _render_log(tmp_path) == "render\n"
    commands = _systemctl_log(tmp_path)
    assert "--no-block stop jasper-voice.service jasper-outputd.service" in commands


# --- the preserve_runtime_env fallback (issue #2489) --------------------------
#
# The endpoint-contract step resolves outputd's capture half by shelling out to
# `jasper.cli.audio_config outputd-capture-device`. When that step fails the
# reconciler exits 66 before writing any outputd env, which leaves outputd
# running whatever the file already said. On jts.local (2026-08-14) what it
# already said was the REAL ALSA backend at `outputd_dac` while the composite
# had parked that alias to `type null` — an output loop with no clock on either
# side, which spun to SIGKILL three times per burst and rode
# StartLimitAction=reboot through three reboots.
#
# The shim below reproduces the failing step and nothing else: every other
# Python call in the run (hardware observation, the active-graph gate, env
# validation) still reaches the real interpreter.

_CLOCKLESS_PRESERVED_ENV = (
    "JASPER_OUTPUTD_BACKEND=alsa\n"
    "JASPER_OUTPUTD_SINK=single_alsa\n"
    "JASPER_OUTPUTD_DAC_PCM=outputd_dac\n"
    "JASPER_OUTPUTD_CONTENT_PCM=outputd_content_capture\n"
)

# The ALSA artifact a PREVIOUS pass left on disk. The guard reads this rather
# than re-deriving what the current pass would render, because the
# endpoint-contract exit is ~87 lines ahead of render_asound_if_needed and this
# pass renders nothing — so these two templates are the only evidence about what
# outputd will actually open.
_PARKED_ASOUND_TEMPLATE = (
    "pcm.outputd_dac {\n"
    "    type null\n"
    "}\n"
    'defaults.pcm.rate_converter "samplerate_medium"\n'
)
_LIVE_ASOUND_TEMPLATE = (
    "pcm.outputd_dac {\n"
    "    type hw\n"
    "    card A\n"
    "    device 0\n"
    "}\n"
    "ctl.outputd_dac {\n"
    "    type hw\n"
    "    card A\n"
    "}\n"
    'defaults.pcm.rate_converter "samplerate_medium"\n'
)


def _python_shim(tmp_path: Path, name: str, guard: str) -> dict[str, str]:
    """Delegate to the real interpreter except where ``guard`` says otherwise.

    ``guard`` is bash run before the delegation; it exits non-zero to inject a
    failure into one specific Python call the reconciler makes.
    """
    shim = tmp_path / name
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f"{guard}\n"
        'exec "$JASPER_TEST_REAL_PYTHON" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return {
        "JASPER_OUTPUT_HARDWARE_PYTHON": str(shim),
        "JASPER_TEST_REAL_PYTHON": sys.executable,
    }


def _endpoint_contract_fails(tmp_path: Path) -> dict[str, str]:
    return _python_shim(
        tmp_path,
        "python-endpoint-contract-fails",
        'for arg in "$@"; do\n'
        '    if [[ "$arg" == "outputd-capture-device" ]]; then\n'
        '        echo "injected outputd-capture-device failure" >&2\n'
        "        exit 1\n"
        "    fi\n"
        "done",
    )


def _dual_apple_cards(tmp_path: Path) -> dict[str, str]:
    sys_class, proc_asound = _fake_sys_output_card(
        tmp_path, card_index=1, card_id="A", usb_path="1-1", serial="left",
    )
    _fake_sys_output_card(
        tmp_path, card_index=2, card_id="A_1", usb_path="1-2", serial="right",
    )
    return {
        "JASPER_SYS_CLASS_SOUND": str(sys_class),
        "JASPER_PROC_ASOUND": str(proc_asound),
    }


def _assert_contract_really_failed(result: subprocess.CompletedProcess[str]) -> None:
    """Positive control: the injected failure reached the path under test.

    Without this an assertion about the fallback could pass on a run that never
    took the fallback at all.
    """
    assert result.returncode == 66, result.stderr
    assert (
        "event=audio_hardware_reconcile.outputd_endpoint_contract_failed"
        in result.stderr
    ), result.stderr


@pytest.mark.parametrize(
    ("preserved_env", "expected_env"),
    [
        pytest.param(
            _CLOCKLESS_PRESERVED_ENV,
            _CLOCKLESS_PRESERVED_ENV.replace(
                "JASPER_OUTPUTD_BACKEND=alsa", "JASPER_OUTPUTD_BACKEND=fake"
            ),
            id="stated-alsa-backend",
        ),
        # Unstated uses the service's ALSA/outputd_dac defaults, the same pair.
        pytest.param(None, None, id="service-defaults"),
    ],
)
def test_contract_failure_parks_a_clockless_output_alias(
    tmp_path: Path,
    preserved_env: str | None,
    expected_env: str | None,
):
    result = _run_reconcile(
        tmp_path,
        DUAL_APPLE_LISTING,
        "--reason",
        "test",
        initial_template=_PARKED_ASOUND_TEMPLATE,
        initial_outputd_env=preserved_env,
        extra_env={
            **_dual_apple_cards(tmp_path),
            **_endpoint_contract_fails(tmp_path),
        },
    )

    _assert_contract_really_failed(result)
    assert "action=park_backend_fake" in result.stderr
    assert (
        "event=audio_hardware_reconcile.outputd_env_clockless_park" in result.stderr
    )
    rendered_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    if expected_env is None:
        assert "JASPER_OUTPUTD_BACKEND=fake" in rendered_env
    else:
        # Exactly one key moves; every other preserved key is already coherent.
        assert rendered_env == expected_env


def test_contract_failure_preserves_when_the_artifact_still_names_real_hardware(
    tmp_path: Path,
):
    """Two passes: the guard must read the artifact, not re-derive one.

    The endpoint-contract exit returns ~87 lines AHEAD of
    render_asound_if_needed, so this pass renders nothing and the alias outputd
    opens is whatever the LAST rendering pass left. Pass 1 recognizes an Apple
    dongle and renders `type hw card A`; pass 2 sees no recognized DAC and fails
    the contract. A guard that asked "what would THIS pass render" answered
    "null" and parked a box whose DAC was still live, on a detail string that
    was false. (Gate blocker B1 on PR #2498.)
    """
    first = _run_reconcile(tmp_path, APPLE_LISTING, "--reason", "test")
    assert first.returncode == 0, first.stderr
    template = (tmp_path / "asoundrc.jasper.template").read_text(encoding="utf-8")
    assert "type hw" in template and "card A" in template
    outputd_env_after_first = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_BACKEND=alsa" in outputd_env_after_first

    # Pass 2: no recognized DAC (empty listing) AND the contract fails, so
    # nothing re-renders and the live artifact still points at real hardware.
    second = _run_reconcile(
        tmp_path,
        "",
        "--reason",
        "test",
        extra_env=_endpoint_contract_fails(tmp_path),
    )

    _assert_contract_really_failed(second)
    assert "action=preserve_runtime_env" in second.stderr
    assert "outputd_env_clockless_park" not in second.stderr
    # The artifact is untouched and still real, and the env is byte-unchanged.
    assert (tmp_path / "asoundrc.jasper.template").read_text(
        encoding="utf-8"
    ) == template
    assert (tmp_path / "outputd.env").read_text(
        encoding="utf-8"
    ) == outputd_env_after_first


@pytest.mark.parametrize(
    (
        "listing",
        "template",
        "operator_env",
        "preserved_env",
        "needs_dual_cards",
        "observation_fails",
        "positive_event",
    ),
    [
        pytest.param(
            DUAL_APPLE_LISTING,
            _PARKED_ASOUND_TEMPLATE,
            None,
            _CLOCKLESS_PRESERVED_ENV.replace(
                "JASPER_OUTPUTD_BACKEND=alsa", "JASPER_OUTPUTD_BACKEND="
            ),
            True,
            False,
            None,
            id="stated-empty-backend",
        ),
        pytest.param(
            APPLE_LISTING,
            _PARKED_ASOUND_TEMPLATE,
            None,
            _CLOCKLESS_PRESERVED_ENV,
            False,
            True,
            "event=audio_hardware_reconcile.state_written_failed",
            id="hardware-observation-failed",
        ),
        pytest.param(
            APPLE_LISTING,
            _LIVE_ASOUND_TEMPLATE,
            None,
            _CLOCKLESS_PRESERVED_ENV,
            False,
            False,
            None,
            id="alias-still-names-real-hardware",
        ),
        pytest.param(
            DUAL_APPLE_LISTING,
            _PARKED_ASOUND_TEMPLATE,
            None,
            _CLOCKLESS_PRESERVED_ENV.replace(
                "JASPER_OUTPUTD_BACKEND=alsa", "JASPER_OUTPUTD_BACKEND=fake"
            ),
            True,
            False,
            None,
            id="backend-already-parked",
        ),
        pytest.param(
            DUAL_APPLE_LISTING,
            _PARKED_ASOUND_TEMPLATE,
            "JASPER_OUTPUTD_SINK=dual_apple\n",
            "JASPER_OUTPUTD_BACKEND=alsa\n"
            "JASPER_OUTPUTD_DAC_PCM=outputd_dac\n"
            "JASPER_OUTPUTD_DUAL_DAC_A_PCM=hw:CARD=A,DEV=0\n"
            "JASPER_OUTPUTD_DUAL_DAC_B_PCM=hw:CARD=A_1,DEV=0\n",
            True,
            False,
            None,
            id="composite-sink-does-not-open-the-alias",
        ),
        pytest.param(
            DUAL_APPLE_LISTING,
            _PARKED_ASOUND_TEMPLATE,
            "JASPER_OUTPUTD_DAC_PCM=hw:CARD=A,DEV=0\n",
            "JASPER_OUTPUTD_BACKEND=alsa\n"
            "JASPER_OUTPUTD_SINK=single_alsa\n",
            True,
            False,
            None,
            id="overridden-dac-pcm-is-not-the-alias",
        ),
    ],
)
def test_contract_failure_preserves_an_env_without_a_clockless_output_loop(
    tmp_path: Path,
    listing: str,
    template: str,
    operator_env: str | None,
    preserved_env: str,
    needs_dual_cards: bool,
    observation_fails: bool,
    positive_event: str | None,
):
    """Each row keeps one conjunct of the clockless-loop guard false."""
    extra_env = _dual_apple_cards(tmp_path) if needs_dual_cards else {}
    if observation_fails:
        extra_env.update(
            _python_shim(
                tmp_path,
                "python-observation-and-contract-fail",
                'for arg in "$@"; do\n'
                '    if [[ "$arg" == "outputd-capture-device" || "$arg" == "jasper.output_hardware" ]]; then\n'
                "        exit 1\n"
                "    fi\n"
                "done",
            )
        )
    else:
        extra_env.update(_endpoint_contract_fails(tmp_path))

    result = _run_reconcile(
        tmp_path,
        listing,
        "--reason",
        "test",
        initial_env=operator_env,
        initial_template=template,
        initial_outputd_env=preserved_env,
        extra_env=extra_env,
    )

    _assert_contract_really_failed(result)
    if positive_event is not None:
        assert positive_event in result.stderr
    assert "action=preserve_runtime_env" in result.stderr
    assert "outputd_env_clockless_park" not in result.stderr
    assert (tmp_path / "outputd.env").read_text(
        encoding="utf-8"
    ) == preserved_env


def test_dual_apple_park_names_a_silent_active_graph_probe(tmp_path: Path):
    """`active_graph_status` prints a reason on every path it declines on.

    So an empty capture is not an unknown reason — it is the probe producing no
    output at all. The park line has to say which of the two it saw.
    """
    result = _run_reconcile(
        tmp_path,
        DUAL_APPLE_LISTING,
        "--reason",
        "test",
        extra_env={
            **_dual_apple_cards(tmp_path),
            "JASPER_OUTPUT_TOPOLOGY_PATH": str(_dual_apple_topology(tmp_path)),
            # The active-graph gate is the one call that carries this variable.
            **_python_shim(
                tmp_path,
                "python-active-graph-dies-silently",
                'if [[ -n "${JASPER_ACTIVE_GRAPH_CAP_CHANNELS:-}" ]]; then\n'
                "    exit 1\n"
                "fi",
            ),
        },
    )

    assert result.returncode == 0, result.stderr
    assert (
        "action=park_until_active_graph reason=active_graph_probe_no_output"
        in result.stderr
    ), result.stderr


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
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=''" in outputd_env
    # wide-output-path PR-7: the registry-declared final-edge format is LIVE
    # (outputd reads it, requests it on the DAC PCM, and parks at exit 78 on
    # a mismatch) — see the fuller comment in
    # test_reconcile_apple_role_enables_apple_helpers_and_renders. DAC8x
    # declares S32_LE now, unlike the Apple dongle's S16_LE default.
    assert "JASPER_OUTPUTD_DAC_FORMAT=S32_LE" in outputd_env
    assert "single_alsa_active" not in result.stderr
    assert not (tmp_path / "tts.env").exists()
    template = (tmp_path / "asoundrc.jasper.template").read_text(encoding="utf-8")
    assert "pcm.outputd_dac" in template
    assert "type hw" in template
    assert "card sndrpihifiberry" in template
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
    # ADR-0100: the retired content lane's capture name appears nowhere.
    assert "outputd_active_content_capture" not in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=6" in outputd_env
    assert "JASPER_OUTPUTD_DAC_PCM=outputd_dac" in outputd_env
    assert "JASPER_OUTPUTD_DUAL_DAC_A_PCM=''" in outputd_env
    # The declared final-edge format is unchanged by arming the lane (mirrors
    # test_reconcile_innomaker_arms_the_width_two_lane_on_a_legal_active_graph
    # above).
    assert "JASPER_OUTPUTD_DAC_FORMAT=S32_LE" in outputd_env
    assert "mode=single_alsa_active active_channels=6 active_lane_cap=8" in result.stderr


@pytest.mark.parametrize(
    "graph_kind",
    [
        pytest.param("single", id="single-camilla-graph"),
        pytest.param("active-leader", id="program-bake-plus-crossover-endpoint"),
    ],
)
def test_reconcile_dac8x_width_two_graph_arms_the_active_ring(
    tmp_path: Path,
    graph_kind: str,
):
    """Both graph layouts drive two outputs, not the DAC's eight-channel cap."""
    if graph_kind == "active-leader":
        args = ("--reason", "outputd-failure", "--no-restart")
        graph_env = _active_leader_graph_env(tmp_path, channels=2)
    else:
        args = ("--reason", "test")
        graph_env = _active_graph_env(tmp_path, channels=2)
    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        *args,
        extra_env=graph_env,
    )

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env
    assert "outputd_active_content_capture" not in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=2" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_LANE=1" in outputd_env
    assert "JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT=1" in outputd_env
    assert (
        "mode=single_alsa_active active_channels=2 active_lane_cap=8 "
        "active_endpoint=jts_ring_active_playback" in result.stderr
    )


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
    # ADR-0100: the retired content lane's capture name appears nowhere.
    assert "outputd_active_content_capture" not in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=2" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_LANE=1" in outputd_env
    assert "mode=single_alsa_active active_channels=2 active_lane_cap=2" in result.stderr


@pytest.mark.parametrize(
    ("listing", "channels", "write_crossover_statefile", "reason"),
    [
        pytest.param(
            DAC8X_AND_APPLE_LISTING,
            2,
            False,
            "program_bake_pipe_without_active_crossover:camilla2_statefile_missing",
            id="crossover-endpoint-missing",
        ),
        pytest.param(
            APPLE_LISTING,
            6,
            True,
            "program_bake_pipe_without_active_crossover:"
            "active_graph_width_out_of_range got=6 cap=2",
            id="crossover-wider-than-dac",
        ),
    ],
)
def test_reconcile_active_leader_without_a_legal_endpoint_stays_stereo(
    tmp_path: Path,
    listing: str,
    channels: int,
    write_crossover_statefile: bool,
    reason: str,
):
    result = _run_reconcile(
        tmp_path,
        listing,
        "--reason",
        "outputd-failure",
        "--no-restart",
        extra_env=_active_leader_graph_env(
            tmp_path,
            channels=channels,
            write_crossover_statefile=write_crossover_statefile,
        ),
    )

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=''" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_LANE=''" in outputd_env
    assert "single_alsa_active" not in result.stderr
    assert f"active_graph={reason}" in result.stderr


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
    # ADR-0100: the retired content lane's capture name appears nowhere.
    assert "outputd_active_content_capture" not in outputd_env
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
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=''" in outputd_env
    assert "single_alsa_active" not in result.stderr
    assert "active_graph=active_graph_unsafe:active_graph_output_count_mismatch" in result.stderr


def test_reconcile_unknown_role_renders_null_outputd_dac(tmp_path: Path):
    result = _run_reconcile(tmp_path, "", "--reason", "test")

    assert result.returncode == 0, result.stderr
    env_text = (tmp_path / "jasper.env").read_text(encoding="utf-8")
    assert "JASPER_AUDIO_DAC_ID=unknown" in env_text
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


def test_reconcile_outputd_runtime_env_change_restarts_outputd_only(
    tmp_path: Path,
):
    # DAC identity unchanged (apple already active) and asound already rendered
    # for card A, but the outputd RUNTIME env moves (fake -> alsa). That class of
    # change cannot shift the mic/input profile, so the reconciler must bounce
    # jasper-outputd ALONE — it must NOT stop jasper-voice (which would deafen
    # wake for ~10-15 s) and must NOT re-run jasper-aec-reconcile. Regression for
    # #1257 (previously this stopped voice and kicked the AEC reconciler).
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
    assert "event=audio_hardware_reconcile.outputd_only_restarted" in result.stderr
    # The split: wake stays up (voice not stopped), AEC reconciler not re-run.
    assert "stop jasper-voice.service" not in commands
    assert "restart jasper-aec-reconcile.service" not in commands


def test_reconcile_floor_only_outputd_change_restarts_outputd_only(
    tmp_path: Path,
):
    # The #1257 scenario proper: a converged apple steady state where the ONLY
    # outputd.env delta is a codified latency FLOOR re-emit — the DAC-buffer
    # floor. DAC identity and asound are unchanged, so wake must stay up:
    # bounce jasper-outputd alone, never stop jasper-voice, never re-run
    # jasper-aec-reconcile.
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
        "defaults.pcm.rate_converter \"__RATE_CONVERTER__\"\n"
    )
    # Converged apple outputd.env: every runtime key at steady state EXCEPT the
    # DAC-buffer floor — so its re-emit is the sole delta and this is a
    # genuinely floor-only re-emit.
    outputd_env = (
        "JASPER_OUTPUTD_BACKEND=alsa\n"
        "JASPER_OUTPUTD_SINK=single_alsa\n"
        "JASPER_OUTPUTD_DAC_PCM=outputd_dac\n"
        "JASPER_OUTPUTD_DUAL_DAC_A_PCM=''\n"
        "JASPER_OUTPUTD_DUAL_DAC_B_PCM=''\n"
        # The coupling-derived content-lane width (S32_LE on a loopback box) is
        # part of the steady state too — see test_reconcile_emits_content_format_*.
        "JASPER_OUTPUTD_CONTENT_FORMAT=S32_LE\n"
        # The registry-declared final-edge format (LIVE: outputd reads it and
        # parks at exit 78 on an unknown value) is part of the steady state —
        # seed it so the floor stays the sole delta. The Apple dongle's steady
        # state is the packed S24_3LE edge (wide-output-path PR-8 b3); seeding
        # the old S16_LE here would leave the format as a SECOND delta and
        # quietly falsify this fixture's "floor-only" premise while the
        # assertions below still passed.
        "JASPER_OUTPUTD_DAC_FORMAT=S24_3LE\n"
        "JASPER_OUTPUTD_ACTIVE_CHANNELS=''\n"
        "JASPER_OUTPUTD_ACTIVE_LANE=''\n"
        # ACTIVE_LANE's pair — written by the same helper from the same decision,
        # so a converged outputd.env states both. Seed it so the floor pass stays
        # a no-op and nothing commits (a box deployed before this key existed
        # writes it once, on its first reconcile, and converges from then on).
        "JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT=''\n"
        "JASPER_CAMILLA_CHUNKSIZE=256\n"
        "JASPER_CAMILLA_TARGET_LEVEL=1536\n"
        "JASPER_OUTPUTD_PERIOD_FRAMES=128\n"
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
        initial_template=rendered_template,
    )

    assert result.returncode == 0, result.stderr
    assert _render_log(tmp_path) == ""
    new_outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_DAC_BUFFER_FRAMES=256" in new_outputd_env
    commands = _systemctl_log(tmp_path)
    assert "--no-block restart jasper-outputd.service" in commands
    assert "event=audio_hardware_reconcile.outputd_only_restarted" in result.stderr
    # Floor-only: wake stays up (voice not stopped), AEC reconciler not re-run.
    assert "stop jasper-voice.service" not in commands
    assert "restart jasper-aec-reconcile.service" not in commands


def test_dac_change_brain_restart_gate_follows_profile_marker(tmp_path: Path):
    for name, marker, brain in (
        ("absent", None, True), ("full", "full\n", True),
        ("streambox", "streambox\n", False), ("empty", "", False),
        ("invalid", "invalid\n", False), ("unreadable", "<unreadable>", False),
    ):
        case = tmp_path / name
        case.mkdir()
        profile = case / "install_profile"
        if marker == "<unreadable>":
            profile.write_text("full\n", encoding="utf-8")
            profile.chmod(0)
            assert profile.is_file() and not os.access(profile, os.R_OK)
        elif marker is not None:
            profile.write_text(marker, encoding="utf-8")
        result = _run_reconcile(case, INNOMAKER_LISTING, initial_env="JASPER_AUDIO_DAC_ID=A\nJASPER_AUDIO_DAC_CARD=A\n")
        if marker == "<unreadable>": profile.chmod(0o600)
        commands = _systemctl_log(case)
        assert result.returncode == 0 and "--no-block restart jasper-outputd.service" in commands, result.stderr
        assert (("stop jasper-voice.service" in commands), ("restart jasper-aec-reconcile.service" in commands)) == (brain, brain)
        assert f"brain_restarted={int(brain)}" in result.stderr


def test_reconcile_dac_change_with_floor_delta_takes_full_path(
    tmp_path: Path,
):
    # Fail-safe direction: a DAC-identity transition coincident with a latency
    # floor delta MUST take the full path (stop jasper-voice + kick
    # jasper-aec-reconcile). A real DAC change can move the mic/input profile, so
    # the DAC change wins even though the only OTHER delta is a floor re-emit —
    # the outputd-only shortcut requires BOTH dac_env_changed==0 AND
    # render_changed==0 (#1257).
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
        "defaults.pcm.rate_converter \"__RATE_CONVERTER__\"\n"
    )
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        # Stored DAC id differs from the detected apple dongle -> dac_env_changed;
        # asound is pre-rendered for card A so render_changed stays 0; the
        # dongle's declared floor lands in an empty outputd.env, which is the
        # coincident floor delta.
        initial_env=(
            "JASPER_AUDIO_DAC_ID=A\n"
            "JASPER_AUDIO_DAC_CARD=A\n"
            "JASPER_AUDIO_ROUTE_PROFILE=usb_low_latency_48k\n"
        ),
        initial_template=rendered_template,
    )

    assert result.returncode == 0, result.stderr
    assert _render_log(tmp_path) == ""
    env_text = (tmp_path / "jasper.env").read_text(encoding="utf-8")
    assert "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle" in env_text
    # The floor delta really was coincident with the DAC change.
    new_outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_CAMILLA_TARGET_LEVEL=1536" in new_outputd_env
    assert "JASPER_OUTPUTD_PERIOD_FRAMES=128" in new_outputd_env
    commands = _systemctl_log(tmp_path)
    # Full path: voice stops and the AEC reconciler is re-run.
    assert "stop jasper-voice.service" in commands
    assert "--no-block restart jasper-aec-reconcile.service" in commands
    assert "event=audio_hardware_reconcile.audio_restarted" in result.stderr
    # NOT the outputd-only shortcut.
    assert "event=audio_hardware_reconcile.outputd_only_restarted" not in result.stderr


def test_reconcile_route_only_change_restarts_fanin_not_voice(tmp_path: Path):
    # The route-only widening of #1257: a converged apple steady state (DAC id +
    # card already match the detected card, asound pre-rendered, the outputd
    # latency floor already emitted so outputd_committed=0) where the ONLY moving
    # dimension is the route/fanin env. That must restart fan-in via the route
    # runtime path and leave jasper-voice up — no voice stop, no aec-reconcile
    # kick, and no outputd RESTART (start-if-recognized only). On main this took
    # the full path (env_changed forced restart_audio_if_needed, stopping voice);
    # this pins that it no longer does.
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
        "defaults.pcm.rate_converter \"__RATE_CONVERTER__\"\n"
    )
    # Fully converged apple + usb_low_latency outputd.env: the apple profile
    # floor is ALREADY present, so the floor pass is a no-op and nothing
    # commits to outputd.env.
    outputd_env = (
        "JASPER_OUTPUTD_BACKEND=alsa\n"
        "JASPER_OUTPUTD_SINK=single_alsa\n"
        "JASPER_OUTPUTD_DAC_PCM=outputd_dac\n"
        "JASPER_OUTPUTD_DUAL_DAC_A_PCM=''\n"
        "JASPER_OUTPUTD_DUAL_DAC_B_PCM=''\n"
        # The coupling-derived content-lane width (S32_LE on a loopback box) is
        # part of the steady state too — see test_reconcile_emits_content_format_*.
        "JASPER_OUTPUTD_CONTENT_FORMAT=S32_LE\n"
        # The registry-declared final-edge format (LIVE: outputd reads it and
        # parks at exit 78 on an unknown value) is part of the steady state —
        # seed it so nothing commits. The Apple dongle's steady state is the
        # packed S24_3LE edge (wide-output-path PR-8 b3).
        "JASPER_OUTPUTD_DAC_FORMAT=S24_3LE\n"
        "JASPER_OUTPUTD_ACTIVE_CHANNELS=''\n"
        "JASPER_OUTPUTD_ACTIVE_LANE=''\n"
        # ACTIVE_LANE's pair — written by the same helper from the same decision,
        # so a converged outputd.env states both. Seed it so the floor pass stays
        # a no-op and nothing commits (a box deployed before this key existed
        # writes it once, on its first reconcile, and converges from then on).
        "JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT=''\n"
        "JASPER_OUTPUTD_CONTENT_BRIDGE=direct\n"
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
            "JASPER_AUDIO_ROUTE_PROFILE=usb_low_latency_48k\n"
        ),
        initial_outputd_env=outputd_env,
        # fanin.env carries a STALE warmup cushion, so the reconcile rewrites the
        # route env (ROUTE_FANIN_CHANGED=1) while nothing else moves.
        initial_fanin_env="JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES=512\n",
        initial_template=rendered_template,
    )

    assert result.returncode == 0, result.stderr
    assert _render_log(tmp_path) == ""
    fanin_env = (tmp_path / "fanin.env").read_text(encoding="utf-8")
    assert "JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES=1536" in fanin_env
    commands = _systemctl_log(tmp_path)
    # Fan-in restarts via the route runtime path.
    assert "restart jasper-fanin.service" in commands
    assert "event=audio_hardware_reconcile.route_runtime_restarted" in result.stderr
    assert "fanin_restarted=1" in result.stderr
    # Voice stays up; the AEC reconciler is not re-run; outputd is not RESTARTED.
    assert "stop jasper-voice.service" not in commands
    assert "restart jasper-aec-reconcile.service" not in commands
    assert "--no-block restart jasper-outputd.service" not in commands
    # The recognized-but-nothing-committed arm still ensures outputd is running.
    assert "--no-block start jasper-outputd.service" in commands
    assert "event=audio_hardware_reconcile.outputd_only_restarted" not in result.stderr


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


@pytest.mark.parametrize(
    ("stub_body", "good", "reason", "expected_detail"),
    [
        pytest.param(
            "    return 64",
            "GOOD LIVE ALSA CONFIG — must survive a render failure\n",
            "render-fail",
            "preserved_existing=1",
            id="renderer-fails-before-writing",
        ),
        pytest.param(
            '    : > "$2"\n    return 0',
            "GOOD LIVE ALSA CONFIG — survives an empty render\n",
            "render-empty",
            None,
            id="renderer-returns-an-empty-file",
        ),
    ],
)
def test_failed_or_empty_render_preserves_the_live_template(
    tmp_path: Path,
    stub_body: str,
    good: str,
    reason: str,
    expected_detail: str | None,
):
    """Neither a nonzero render nor an empty result may clobber live ALSA."""
    stub = _stub_render_lib(tmp_path, stub_body)
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        reason,
        initial_template=good,
        extra_env={"JASPER_ASOUND_RENDER_LIB": str(stub)},
    )

    assert result.returncode == 0, result.stderr
    template_path = tmp_path / "asoundrc.jasper.template"
    assert template_path.read_text(encoding="utf-8") == good
    assert template_path.stat().st_size > 0
    assert "event=audio_hardware_reconcile.asound_render_failed" in result.stderr
    assert "event=audio_hardware_reconcile.asound_rendered" not in result.stderr
    if expected_detail is not None:
        assert expected_detail in result.stderr
    assert _render_log(tmp_path) == ""
    leftovers = list(template_path.parent.glob("asoundrc.jasper.template.*"))
    assert leftovers == [], leftovers


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
    # channel write). The retired content-buffer key is never emitted.
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


def test_the_note_prefix_the_script_matches_is_the_one_the_cli_prints(
    tmp_path: Path, capsys
) -> None:
    """The bash/Python seam for the waypoint note, pinned from BOTH sides.

    ``validate_outputd_env_stage`` recognises a coherent-but-transient result by
    the literal prefix the CLI prints on its exit-0 path. Nothing else couples
    them, so a reworded CLI would silently stop the reconciler logging
    ``outputd_env_note`` — a silent-failure path, not a loud one. Asserting the
    literal in the script alone would be half a guard (it would still pass if the
    CLI changed), so this also PRODUCES a real note and checks the CLI's actual
    bytes start with the same prefix.
    """
    from tests.test_ring_active_endpoint import (
        _active_topology,
        _emit_active_baseline,
        _mono_two_way_preset,
        _run_validate_outputd_env,
    )
    from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE

    script_text = SCRIPT.read_text(encoding="utf-8")
    # Matched anywhere, not as a prefix: the capture merges stderr, so a warning
    # emitted before stdout would push the marker off the front and silently drop
    # the event.
    assert '*"ok note="*' in script_text
    assert 'log_event "outputd_env_note"' in script_text

    # A REAL emitted graph, not a hand-written stanza: the CLI demotes any
    # active-endpoint graph that fails `outputd_active_lane_decision` to
    # devices=None, and a stub stanza fails it — so a stub would print a bare
    # "ok" and make this contract vacuous.
    topology = _active_topology("mono", "active_2_way")
    rc, out = _run_validate_outputd_env(
        tmp_path,
        capsys,
        graph_yaml=_emit_active_baseline(
            _mono_two_way_preset(), RING_ACTIVE_PLAYBACK_DEVICE
        ),
        topology=topology,
        coupling="loopback",
        marker=None,
    )

    assert rc == 0, out
    assert out.startswith("ok note="), out


def _outputd_env_key_present(outputd_env: str, key: str) -> bool:
    return any(
        re.match(rf"^\s*{re.escape(key)}\s*=", line)
        for line in outputd_env.splitlines()
    )


def test_reconcile_dac8x_emits_the_soak_validated_floor(tmp_path: Path):
    # R7a: the DAC8x floor the jts3 soak validated reaches outputd.env through
    # the SAME bash plumbing (apply_latency_floor_env) the Apple dongle uses —
    # the four declared values verbatim. This is the end-to-end pin for the
    # outputd half of the floor: the soak moved these keys by hand, and this
    # asserts the reconciler now moves them on its own.
    result = _run_reconcile(tmp_path, DAC8X_AND_APPLE_LISTING, "--reason", "test")

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    for key, value in (
        ("JASPER_CAMILLA_CHUNKSIZE", "256"),
        ("JASPER_CAMILLA_TARGET_LEVEL", "1536"),
        ("JASPER_OUTPUTD_PERIOD_FRAMES", "128"),
        ("JASPER_OUTPUTD_DAC_BUFFER_FRAMES", "256"),
    ):
        assert f"{key}={value}" in outputd_env, (key, outputd_env)
    assert (
        "event=audio_hardware_reconcile.latency_floor "
        "reason=test output_dac_id=hifiberry_dac8x camilla_chunksize=256 "
        "camilla_target_level=1536 outputd_period_frames=128 "
        "outputd_dac_buffer_frames=256"
    ) in result.stderr


def test_reconcile_no_floor_drops_stale_floor_keys(tmp_path: Path):
    # A DAC with no declared floor must DROP a stale floor a prior DAC wrote into
    # outputd.env, not leave it as `=''` (which would clobber an operator value)
    # and not leave the stale numbers. DAC8X STUDIO is the floorless case:
    # pointing this at a profile that later declares a floor would make the loop
    # below unreachable rather than failing (what an R7a DAC8x floor did here,
    # and what jts4's measured floor then did to its INNOMAKER replacement).
    result = _run_reconcile(
        tmp_path,
        DAC8X_STUDIO_LISTING,
        "--reason",
        "test",
        initial_outputd_env=(
            "JASPER_CAMILLA_CHUNKSIZE=256\n"
            "JASPER_CAMILLA_TARGET_LEVEL=1024\n"
            "JASPER_OUTPUTD_PERIOD_FRAMES=256\n"
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES=512\n"
        ),
    )

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    for key in (
        "JASPER_CAMILLA_CHUNKSIZE",
        "JASPER_CAMILLA_TARGET_LEVEL",
        "JASPER_OUTPUTD_PERIOD_FRAMES",
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


def test_reconcile_refusal_preserves_env_and_leaves_every_service_running(
    tmp_path: Path,
):
    """A REFUSED reconcile leaves the box running exactly as it was found.

    The candidate is rejected, so ``preserved=1``: the runtime outputd.env is
    byte-unchanged and the script exits before any render. Nothing this run did
    reached a daemon, so stopping one would silence a healthy box on behalf of a
    change that never landed.

    This used to ``park_output_audio`` — stop jasper-voice AND jasper-outputd —
    and on jts3 (2026-08-11) that took the assistant down with no cue, no doctor
    delta, and nothing to bring it back; recovery needed a hand-run
    ``systemctl start jasper-aec-reconcile``
    (``captures/r7b-jts3-arm-20260811T111338Z``, files 15 and 18). The loud
    signal is unchanged: exit 78 plus the ``outputd_env_invalid preserved=1``
    line naming the contradiction.
    """
    prior_outputd = (
        "JASPER_OUTPUTD_BACKEND=alsa\n"
        "JASPER_OUTPUTD_SINK=single_alsa\n"
        "JASPER_OUTPUTD_CONTENT_PCM=outputd_active_content_capture\n"
        "JASPER_OUTPUTD_ACTIVE_CHANNELS=8\n"
        "JASPER_OUTPUTD_ACTIVE_LANE=1\n"
        "JASPER_OUTPUTD_PERIOD_FRAMES=128\n"
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
                    "reason": "test invalid staged outputd env",
                },
                "JASPER_OUTPUTD_DAC_BUFFER_FRAMES": {
                    "value": "1536",
                    "reason": "test invalid staged outputd env",
                },
            },
        }),
        encoding="utf-8",
    )

    result = _run_reconcile(
        tmp_path,
        INNOMAKER_LISTING,
        "--reason",
        "test",
        initial_env="JASPER_AUDIO_ROUTE_PROFILE=usb_low_latency_48k\n",
        initial_outputd_env=prior_outputd,
        extra_env={"JASPER_AUDIO_RUNTIME_OVERRIDES_PATH": str(overrides)},
    )

    assert result.returncode == 78, result.stderr
    assert (tmp_path / "outputd.env").read_text(encoding="utf-8") == prior_outputd
    assert "event=audio_hardware_reconcile.outputd_env_invalid" in result.stderr
    assert "preserved=1" in result.stderr
    assert "JASPER_OUTPUTD_PERIOD_FRAMES=1024" not in (
        tmp_path / "outputd.env"
    ).read_text(encoding="utf-8")
    assert "event=audio_hardware_reconcile.outputd_candidate_rejected" in result.stderr
    assert "action=preserve_runtime_env" in result.stderr
    # The log line PRINTS the promise the assertion below proves.
    assert "services=unchanged" in result.stderr
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=8" in (
        tmp_path / "outputd.env"
    ).read_text(encoding="utf-8")
    assert not (tmp_path / "asoundrc.jasper.template").exists()
    assert _render_log(tmp_path) == ""
    # No unit stopped, so none can stay stopped. Matched on the systemctl VERB
    # (argv-token `stop`), never a substring, so a unit name containing "stop"
    # could not make this pass vacuously.
    stopped = [
        line
        for line in _systemctl_log(tmp_path).splitlines()
        if "stop" in line.split()
    ]
    assert stopped == [], stopped
    assert "jasper-voice.service" not in _systemctl_log(tmp_path)


def test_reconcile_refuses_invalid_dac_buffer_candidate_and_preserves_prior(
    tmp_path: Path,
):
    prior_outputd = (
        "JASPER_OUTPUTD_BACKEND=alsa\n"
        "JASPER_OUTPUTD_SINK=single_alsa\n"
        "JASPER_OUTPUTD_PERIOD_FRAMES=128\n"
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

    assert result.returncode == 78, result.stderr
    assert (tmp_path / "outputd.env").read_text(encoding="utf-8") == prior_outputd
    assert "event=audio_hardware_reconcile.outputd_env_invalid" in result.stderr
    assert "JASPER_OUTPUTD_DAC_BUFFER_FRAMES_256" in result.stderr
    assert "preserved=1" in result.stderr
    # The refusal names the ORIGIN, and names it as a file that still exists.
    # This is the PRODUCTION path — the reconciler validates a staged candidate
    # under a `.outputd.env.candidate.XXXXXX` temp name that is deleted on EXIT,
    # so reporting the path it READ named a file the operator cannot open.
    # (Gate blocker B3 on PR #2498.) `log_event` tokenizes the detail, so match
    # the tokenized spellings.
    assert "override_store" in result.stderr
    assert _log_token(str(tmp_path / "outputd.env")) in result.stderr
    assert "outputd.env.candidate" not in result.stderr


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


def test_route_change_restarts_only_fanin_runtime(tmp_path: Path):
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
    assert "try-restart jasper-usbsink.service" not in commands
    assert "fanin_restarted=1" in result.stderr


def test_idempotent_second_run_makes_no_route_restart(tmp_path: Path):
    # A semantically-identical second run must not restart the route runtime at all
    # (canonical-form-stable change detection: nothing moved → nothing bounces).
    common = dict(
        initial_env="JASPER_AUDIO_ROUTE_PROFILE=usb_low_latency_48k\n",
    )
    first = _run_reconcile(tmp_path, APPLE_LISTING, "--reason", "test", **common)
    assert first.returncode == 0, first.stderr
    # Second run: fanin.env already carries the route values from run 1.
    (tmp_path / "systemctl.log").write_text("", encoding="utf-8")
    second = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_env="JASPER_AUDIO_ROUTE_PROFILE=usb_low_latency_48k\n",
        initial_fanin_env=(tmp_path / "fanin.env").read_text(encoding="utf-8"),
    )
    assert second.returncode == 0, second.stderr
    commands = _systemctl_log(tmp_path)
    assert "restart jasper-fanin.service" not in commands
    assert "fanin_restarted=0" in second.stderr


# --- Per-box shm-ring conf.d render (PR-6) ------------------------------------
#
# The rule: the reconciler renders the ring conf.d slot period ONLY from the
# active DAC profile's DECLARED LatencyFloor. No declared floor — and any
# unrecognized DAC — leaves the SHIPPED conf.d genuinely untouched (byte AND
# mtime), so that box keeps its current coupling. Zero behaviour change on any
# box until floor data is declared for its profile.

SHIPPED_RING_CONF = ROOT / "deploy" / "alsa" / "conf.d" / "60-jts-ring.conf"


def _staged_ring_conf(tmp_path: Path) -> Path:
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_bytes(SHIPPED_RING_CONF.read_bytes())
    return conf


@pytest.mark.parametrize(
    ("listing", "event"),
    [
        pytest.param(
            APPLE_LISTING,
            "event=audio_hardware_reconcile.ring_conf reason=test "
            "result=unchanged output_dac_id=apple_usb_c_dongle period_frames=128 "
            "previous_period_frames=128 sample_format=S32_LE ring_a_channels=2 "
            "ring_b_channels=2 ring_active_channels=2 topology=",
            id="apple-floor-matches-shipped-wire",
        ),
        pytest.param(
            DAC8X_STUDIO_LISTING,
            "event=audio_hardware_reconcile.ring_conf reason=test result=skipped "
            "output_dac_id=hifiberry_dac8x_studio period_frames=none "
            "previous_period_frames=none sample_format=none ring_a_channels=none "
            "ring_b_channels=none ring_active_channels=none topology=none "
            "reason=no_declared_floor",
            id="profile-declares-no-floor",
        ),
        pytest.param(
            DAC8X_AND_APPLE_LISTING,
            "event=audio_hardware_reconcile.ring_conf reason=test "
            "result=unchanged output_dac_id=hifiberry_dac8x period_frames=128 "
            "previous_period_frames=128 sample_format=S32_LE ring_a_channels=2 "
            "ring_b_channels=2 ring_active_channels=2 topology=",
            id="dac8x-floor-matches-shipped-wire",
        ),
        pytest.param(
            "",
            "event=audio_hardware_reconcile.ring_conf reason=test "
            "result=skipped reason=dac_unrecognized",
            id="dac-unrecognized",
        ),
    ],
)
def test_reconcile_preserves_a_ring_conf_that_needs_no_render(
    tmp_path: Path,
    listing: str,
    event: str,
):
    conf = _staged_ring_conf(tmp_path)
    before_bytes = conf.read_bytes()
    before_mtime = conf.stat().st_mtime_ns

    result = _run_reconcile(
        tmp_path,
        listing,
        "--reason",
        "test",
        extra_env={"JASPER_RING_CONF_D": str(conf)},
    )

    assert result.returncode == 0, result.stderr
    assert event in result.stderr
    assert conf.read_bytes() == before_bytes
    assert conf.stat().st_mtime_ns == before_mtime


def test_reconciler_delegates_the_ring_conf_render_to_the_python_layer() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "render-ring-conf-wire" in text
    # The INSTALLED conf.d path is NOT a fourth copy in bash: the only --conf-d
    # the script passes is the (empty by default) test override, so the Python
    # SSOT jasper.ring_assets.RING_CONF_D resolves the real path and reports
    # back which file it touched.
    assert "/etc/alsa/conf.d" not in text
    assert 'RING_CONF_D_OVERRIDE="${JASPER_RING_CONF_D:-}"' in text
    assert '--conf-d "$RING_CONF_D_OVERRIDE"' in text
    # Ring B's channel count is topology-resolved, so the render needs the
    # saved topology the rest of this script already resolves.
    assert '--output-topology "$OUTPUT_TOPOLOGY_PATH"' in text
    # The `key value` protocol is a WHITELIST — an unmatched key is dropped
    # silently. Every key the renderer emits needs an arm, or the wire it
    # resolved never reaches the journal.
    for key in (
        "result",
        "period_frames",
        "previous_period_frames",
        "sample_format",
        "ring_a_channels",
        "ring_b_channels",
        "topology",
        "reason",
        "conf",
    ):
        assert f"            {key}) " in text, key
    # The render must not feed a restart flag: arming is the coupling
    # reconciler's job, and ALSA re-reads the conf.d at the next PCM open.
    assert "render_ring_conf_if_needed && " not in text


# --- render-ring-conf-wire: the floor is DATA -------------------------------


def _render_ring_conf(conf: Path) -> int:
    from jasper.cli.audio_config import main as audio_config_main

    return audio_config_main(
        [
            "render-ring-conf-wire",
            "--profile-id",
            "hifiberry_dac8x",
            "--conf-d",
            str(conf),
        ]
    )


def _drifted_ring_conf(tmp_path: Path, period_frames: int = 1024) -> Path:
    """The shipped conf.d with its period drifted off the transport slot.

    The remaining live write path once non-slot floors are refused: a
    hand-edited or half-installed conf.d the render converges back.
    """
    conf = _staged_ring_conf(tmp_path)
    conf.write_text(
        conf.read_text(encoding="utf-8").replace(
            f"period_frames {RING_SLOT_FRAMES}", f"period_frames {period_frames}"
        ),
        encoding="utf-8",
    )
    return conf


def _synthetic_floor(period_frames: int):
    from jasper.audio_hardware.dac import LatencyFloor

    return LatencyFloor(
        camilla_chunksize=256,
        camilla_target_level=1536,
        outputd_period_frames=period_frames,
        outputd_dac_buffer_frames=8 * period_frames,
    )


def test_render_subcommand_renders_for_any_profile_declaring_the_slot_floor(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    # The floor is DATA, not an Apple special case: synthesize a
    # RING_SLOT_FRAMES floor on a different profile and a drifted conf.d
    # converges. (A floor that is NOT the slot is refused — see the test
    # below; that is the #2147 boundary, not a per-DAC code branch.)
    conf = _drifted_ring_conf(tmp_path)
    synthetic = _synthetic_floor(RING_SLOT_FRAMES)
    monkeypatch.setattr(
        "jasper.cli.audio_config.latency_floor_for",
        lambda profile_id: synthetic if profile_id == "hifiberry_dac8x" else None,
    )

    assert _render_ring_conf(conf) == 0
    out = capsys.readouterr().out
    assert "result rendered" in out
    assert f"period_frames {RING_SLOT_FRAMES}" in out
    assert "previous_period_frames 1024" in out

    from jasper import ring_assets

    assert ring_assets.ring_conf_period_frames(str(conf)) == RING_SLOT_FRAMES
    # EVERY ring PCM the conf.d defines converges onto the one slot period —
    # Ring A, Ring B, and the ACTIVE ring. The count is derived from the block
    # list rather than spelled, so adding a fourth ring cannot leave this
    # assertion silently checking a subset.
    assert (
        conf.read_text(encoding="utf-8").count(
            f"    period_frames {RING_SLOT_FRAMES}"
        )
        == len(ring_assets.RING_CONF_PCMS)
    )


def test_render_subcommand_refuses_a_floor_the_ring_slot_cannot_carry(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    # Ring A's slot is fan-in's COMPILE-TIME RING_SLOT_FRAMES (128, no env
    # override): rust/jasper-fanin/src/config.rs pins it and mixer.rs creates
    # the ring with it. Rendering a non-128 period into pcm.jts_ring_capture
    # would make CamillaDSP's ioplug attach expect N against fan-in's 128-frame
    # ring — a hard RING_ATTACH_FATAL geometry error that CRASHES shm_ring at
    # arm rather than refusing it. So a declared floor that is not exactly
    # RING_SLOT_FRAMES must leave the conf.d untouched. See issue #2147.
    conf = _staged_ring_conf(tmp_path)
    before_bytes = conf.read_bytes()
    before_mtime = conf.stat().st_mtime_ns
    monkeypatch.setattr(
        "jasper.cli.audio_config.latency_floor_for",
        lambda _profile_id: _synthetic_floor(2 * RING_SLOT_FRAMES),
    )

    assert _render_ring_conf(conf) == 0
    out = capsys.readouterr().out
    assert "result skipped" in out
    assert f"reason ring_slot_fixed_{RING_SLOT_FRAMES}" in out
    assert conf.read_bytes() == before_bytes
    assert conf.stat().st_mtime_ns == before_mtime


def test_render_ring_conf_wire_itself_refuses_a_non_slot_period(
    tmp_path: Path,
) -> None:
    # Defence in depth: the writer cannot emit a period the ring transport
    # will not carry, even if a future caller forgets the floor gate.
    from jasper import ring_assets
    from jasper.fanin_coupling import RingWire

    conf = _staged_ring_conf(tmp_path)
    before_bytes = conf.read_bytes()

    with pytest.raises(ValueError, match="RING_SLOT_FRAMES"):
        ring_assets.render_ring_conf_wire(
            RingWire(
                sample_format="S16_LE",
                ring_a_channels=2,
                ring_b_channels=2,
                period_frames=2 * RING_SLOT_FRAMES,
            ),
            conf_d=str(conf),
        )
    assert conf.read_bytes() == before_bytes


def test_render_subcommand_is_idempotent(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    # Reconcile runs on every boot and udev event; a converged box must stop
    # writing rather than churn the mtime on each pass.
    conf = _drifted_ring_conf(tmp_path)
    monkeypatch.setattr(
        "jasper.cli.audio_config.latency_floor_for",
        lambda _profile_id: _synthetic_floor(RING_SLOT_FRAMES),
    )

    assert _render_ring_conf(conf) == 0
    capsys.readouterr()
    settled_bytes = conf.read_bytes()
    settled_mtime = conf.stat().st_mtime_ns

    assert _render_ring_conf(conf) == 0
    assert "result unchanged" in capsys.readouterr().out
    assert conf.read_bytes() == settled_bytes
    assert conf.stat().st_mtime_ns == settled_mtime


def test_render_subcommand_reports_a_torn_conf_instead_of_inventing_one(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text("pcm.jts_ring_capture { type jts_ring }\n", encoding="utf-8")
    monkeypatch.setattr(
        "jasper.cli.audio_config.latency_floor_for",
        lambda _profile_id: _synthetic_floor(RING_SLOT_FRAMES),
    )

    assert _render_ring_conf(conf) == 1
    captured = capsys.readouterr()
    assert "no period_frames" in captured.err
    assert conf.read_text(encoding="utf-8") == (
        "pcm.jts_ring_capture { type jts_ring }\n"
    )


# --- flat cutover render (issue #2179 / #2182) --------------------------------
#
# The startup graph is width-matched to the SAVED output topology, so it goes
# stale whenever the layout changes. The two paths that change it — the /sound/
# topology save and jasper-output-topology-reset — run inside jasper-web's
# sandbox, which has no /etc/camilladsp write path (WS1-deliberate). Both kick
# THIS reconciler, which runs as root, so the runtime render lives here.


def _flat_cutover_event(stderr: str) -> dict[str, str]:
    """The flat_cutover log line, parsed into its `key=value` fields.

    `log_event` emits `event=<name> reason=$REASON <rest>`, so the run reason is
    interleaved ahead of the function's own keys — asserting on an adjacent
    `flat_cutover result=...` substring silently never matches. Parsing also
    keeps the assertions off the tmp-path values that trail the line.
    """
    prefix = "event=audio_hardware_reconcile.flat_cutover "
    lines = [line for line in stderr.splitlines() if line.startswith(prefix)]
    assert len(lines) == 1, f"expected exactly one flat_cutover event, got {lines}"
    fields: dict[str, str] = {}
    for token in lines[0][len(prefix) :].split():
        key, _, value = token.partition("=")
        fields.setdefault(key, value)  # first `reason=` is the run reason
    return fields


def _fake_jasper_sound_cli(tmp_path: Path) -> Path:
    """A ``jasper-sound`` shim backed by the REAL CLI, so this is end-to-end."""
    script = tmp_path / "jasper-sound"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f"exec {sys.executable} -c "
        "'from jasper.cli.sound import main; raise SystemExit(main())' \"$@\"\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _mono_topology_payload() -> dict:
    return {
        "artifact_schema_version": 1,
        "kind": "jts_output_topology",
        "topology_id": "mono",
        "name": "Mono passive output",
        "status": "verified",
        "hardware": {
            "device_id": "innomaker_hifi_amp_pro",
            "device_label": "InnoMaker HiFi AMP Pro",
            "card_id": "sndrpimerusamp",
            "physical_output_count": 2,
        },
        "speaker_groups": [{
            "id": "main", "label": "Main speaker", "kind": "mono",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 0}],
        }],
        "routing": {"mono_group_id": "main"},
    }


def test_reconcile_renders_the_width_matched_cutover_and_is_idempotent(
    tmp_path: Path,
):
    """Write-on-change, and width-matched to the saved topology.

    The reconciler runs on every boot and every sound-card event, so an
    unconditional write would churn the file's mtime and make "did the graph
    change?" unanswerable from the filesystem — the same reason its env writes
    go through ``set_env_var_if_changed``.
    """
    conf_dir = tmp_path / "camilladsp"
    conf_dir.mkdir()
    (tmp_path / "output_topology.json").write_text(
        json.dumps(_mono_topology_payload()), encoding="utf-8"
    )
    extra = {
        "JASPER_SOUND_CLI": str(_fake_jasper_sound_cli(tmp_path)),
        "JASPER_CAMILLA_CONF_DIR": str(conf_dir),
        "PYTHONPATH": str(ROOT),
    }

    first = _run_reconcile(
        tmp_path, INNOMAKER_LISTING, "--reason", "test", extra_env=extra
    )
    assert first.returncode == 0, first.stderr
    first_event = _flat_cutover_event(first.stderr)
    assert first_event["result"] == "ok"
    assert first_event["changed"] == "yes"

    cutover = conf_dir / "outputd-cutover.yml"
    # Width-matched: the mono topology claims output 0, so channel 1 is muted.
    assert "as_out1_commission_mute" in cutover.read_text(encoding="utf-8")
    # ONE flat config: its `shm_ring` sibling collapsed into it (ADR-0100).
    assert not (conf_dir / "outputd-cutover-ring.yml").exists()
    assert cutover.stat().st_mode & 0o777 == 0o644
    before = (cutover.stat().st_mtime_ns, cutover.read_bytes())

    second = _run_reconcile(
        tmp_path, INNOMAKER_LISTING, "--reason", "test", extra_env=extra
    )
    assert second.returncode == 0, second.stderr
    second_event = _flat_cutover_event(second.stderr)
    assert second_event["result"] == "ok"
    assert second_event["changed"] == "no"
    assert (cutover.stat().st_mtime_ns, cutover.read_bytes()) == before


def test_reconcile_refuses_to_render_against_a_corrupt_topology(tmp_path: Path):
    """A corrupt topology must FAIL the render, not succeed unmuted.

    `flat_graph_muted_outputs` fails SOFT — mute nothing — which is right for
    every caller that has a guard behind it (install's statefile check, the
    reset's contract call, the carrier's `can_host_eq`). This renderer must also
    keep the last proved bytes: the later runtime selector can then reject stale
    intent, and the boot unit ordering prevents CamillaDSP from starting when
    that convergence fails.
    """
    conf_dir = tmp_path / "camilladsp"
    conf_dir.mkdir()
    topology = tmp_path / "output_topology.json"
    extra = {
        "JASPER_SOUND_CLI": str(_fake_jasper_sound_cli(tmp_path)),
        "JASPER_CAMILLA_CONF_DIR": str(conf_dir),
        "PYTHONPATH": str(ROOT),
    }

    # A healthy mono box first: the good, width-matched graph on disk.
    topology.write_text(json.dumps(_mono_topology_payload()), encoding="utf-8")
    healthy = _run_reconcile(
        tmp_path, INNOMAKER_LISTING, "--reason", "test", extra_env=extra
    )
    assert healthy.returncode == 0, healthy.stderr
    cutover = conf_dir / "outputd-cutover.yml"
    good = cutover.read_bytes()
    assert b"as_out1_commission_mute" in good

    # Now the topology goes unparseable underneath it.
    topology.write_text("{not json", encoding="utf-8")
    corrupt = _run_reconcile(
        tmp_path, INNOMAKER_LISTING, "--reason", "test", extra_env=extra
    )

    # BYTES FIRST: the substantive harm is the good graph being overwritten
    # with an unmuted one, so that is the assertion that must fail without the
    # fix — not the log line.
    assert cutover.read_bytes() == good
    # The reconcile itself still completes (a render failure is best-effort),
    # but it is reported FAILED rather than logged as a successful render.
    assert corrupt.returncode == 0, corrupt.stderr
    assert _flat_cutover_event(corrupt.stderr)["result"] == "failed"


def test_reconcile_renders_the_golden_when_no_topology_is_saved(tmp_path: Path):
    """MISSING is not CORRUPT, so rendering can still seed the golden artifact.

    This does not authorize playback. The runtime selector parks a fresh box
    until the household saves an explicit mono or stereo layout.
    """
    conf_dir = tmp_path / "camilladsp"
    conf_dir.mkdir()
    # No output_topology.json written at all.
    result = _run_reconcile(
        tmp_path,
        INNOMAKER_LISTING,
        "--reason",
        "test",
        extra_env={
            "JASPER_SOUND_CLI": str(_fake_jasper_sound_cli(tmp_path)),
            "JASPER_CAMILLA_CONF_DIR": str(conf_dir),
            "PYTHONPATH": str(ROOT),
        },
    )

    assert result.returncode == 0, result.stderr
    assert _flat_cutover_event(result.stderr)["result"] == "ok"
    rendered = (conf_dir / "outputd-cutover.yml").read_text(encoding="utf-8")
    assert "commission_mute" not in rendered


def test_reconcile_without_the_sound_cli_skips_the_render_instead_of_failing(
    tmp_path: Path,
):
    """Best-effort: a missing CLI must not abort a hardware reconcile."""
    result = _run_reconcile(
        tmp_path,
        INNOMAKER_LISTING,
        "--reason",
        "test",
        extra_env={"JASPER_SOUND_CLI": str(tmp_path / "absent")},
    )

    assert result.returncode == 0, result.stderr
    event = _flat_cutover_event(result.stderr)
    assert event["result"] == "skipped"
    # The run reason wins the first `reason=`; the skip cause trails it.
    assert "reason=cli_unavailable" in result.stderr


# --- wide-output-path PR-6: the content-lane format axis ----------------------
# The reconciler is the single writer of JASPER_OUTPUTD_CONTENT_FORMAT, and its
# value comes from the SAME function that decides what CamillaDSP emits
# (jasper.fanin_coupling.content_lane_format_for_coupling) — so outputd cannot
# ask for a width the emitters do not produce.


def test_reconcile_emits_the_wide_content_format_on_a_loopback_box(tmp_path: Path):
    """The default coupling (unset == loopback) carries the wide program lane."""
    result = _run_reconcile(tmp_path, APPLE_LISTING, "--reason", "test")

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_CONTENT_FORMAT=S32_LE" in outputd_env
    # The content lane and the DAC edge are separate hops with separate
    # declarations, and on this box they legitimately differ: an S32 lane into
    # the Apple dongle's packed S24_3LE edge, the widest width that device
    # advertises.
    assert "JASPER_OUTPUTD_DAC_FORMAT=S24_3LE" in outputd_env
    assert "content_format=S32_LE" in result.stderr


def test_reconcile_emits_the_wide_content_format_on_an_armed_ring_box(
    tmp_path: Path,
):
    """An armed shm_ring box's content hop now matches the box-wide default too —
    RENAMED and RE-POINTED from ..._narrow_content_format...: the ring wire's
    resolver default flipped WIDE (jasper.fanin_coupling.resolve_ring_wire_format,
    PR #2601), so an UNDECLARED box's shm_ring answer now equals loopback's
    S32_LE (see test_reconcile_emits_the_wide_content_format_on_a_loopback_box
    above). The "shm_ring forces the content lane narrow" asymmetry this test
    used to demonstrate is gone.

    An explicit OPERATOR narrow pin (JASPER_FANIN_RING_WIRE_FORMAT=S16_LE) would
    reproduce that asymmetry deliberately, but it is NOT reachable from this
    subprocess-level test: content_format_for_coupling() (the reconciler's own
    bash helper) calls content_lane_format_for_coupling, whose ring-wire read
    (jasper.fanin_coupling.read_declared_ring_wire_format) is FILE-FRESH against
    the REAL /etc/jasper/jasper.env and /var/lib/jasper/fanin.env — not this
    script's own $JASPER_ENV_FILE / $FANIN_ENV_FILE overrides, which reach no
    part of this probe. On a real Pi those two path pairs are literally the same
    files, so an operator's pin genuinely reaches this probe in production; only
    this hermetic tmp_path harness diverges them, and there is no test-only
    override to close that gap without touching real system paths. The pin is exercised instead at the
    direct-call level, where the resolver (or its file inputs) can be
    monkeypatched: tests/test_fanin_coupling.py, and
    tests/test_audio_runtime_plan.py's shm_ring transport-coherence tests
    (monkeypatch jasper.fanin_coupling.resolve_ring_wire).

    What this test still proves: the reconciler correctly PLUMBS
    content_lane_format_for_coupling's answer into
    JASPER_OUTPUTD_CONTENT_FORMAT for an armed ring box — the belt to
    ring_edge_width_ready's suspender (the PR-6 ring ruling)."""
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_fanin_env="JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n",
        # Ring A and Ring B move together; without Ring B's bridge the
        # reconciler's own transport-coherence validator rejects the stage
        # (correctly) before the format axis is reachable.
        initial_outputd_env="JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring\n",
    )

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_CONTENT_FORMAT=S32_LE" in outputd_env
    assert "content_format=S32_LE" in result.stderr


@pytest.mark.parametrize(
    "spelling", ["rate_match", "ratematch", "rate-matched", "rate_matched"]
)
def test_reconcile_no_longer_narrows_for_the_removed_rate_match_bridge(
    tmp_path: Path, spelling: str
):
    """The i16-only `rate_match` content bridge was DELETED, and its S16_LE
    format narrowing went with it.

    The narrowing existed so a routine deploy would not emit a wide content lane
    into a bridge outputd refuses (exit 78 -> parked final-output owner, silent
    speaker). With the bridge gone that pairing cannot exist: outputd parks on
    every `rate_match` spelling rather than reading a content format at all. So
    the reconciler must now emit the COUPLING's own format — proving the
    narrowing is really gone rather than merely unreachable, for every spelling
    the deleted parse used to accept.
    """
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_outputd_env=f"JASPER_OUTPUTD_CONTENT_BRIDGE={spelling}\n",
    )

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    # The loopback coupling's own width, NOT the narrowed S16_LE.
    assert "JASPER_OUTPUTD_CONTENT_FORMAT=S32_LE" in outputd_env
    # The stale operator value is left alone; outputd is what fail-safes it.
    assert f"JASPER_OUTPUTD_CONTENT_BRIDGE={spelling}" in outputd_env
    assert "content_format_narrowed" not in result.stderr
    assert "rate_match_content_bridge" not in result.stderr


def test_reconciler_carries_no_rate_match_narrowing_machinery():
    """No dead alias list or narrowing branch survives the bridge's deletion.

    A source-level guard because the behavioural test above passes just as well
    if the loop is still present but never matches — this is what fails if the
    bash side is left behind.
    """
    script = (
        ROOT / "deploy" / "bin" / "jasper-audio-hardware-reconcile"
    ).read_text(encoding="utf-8")
    assert "RATE_MATCH_BRIDGE_ALIASES" not in script
    assert "reason=rate_match_content_bridge" not in script


def _python_shim_that_cannot_answer_the_coupling(tmp_path: Path) -> Path:
    """A python wrapper that forwards every call to the real interpreter EXCEPT
    the coupling-format probe, which it fails.

    Fault-injected at that one call rather than by removing the interpreter
    outright: the reconciler runs several other Python probes first (the I²S HAT
    boot pass, the output-hardware state observation) and an absent interpreter
    aborts the whole reconcile long before the format axis is reached, which
    would prove nothing about this branch."""
    shim = tmp_path / "python-no-coupling"
    shim.write_text(
        "#!/bin/bash\n"
        'script="$(cat)"\n'
        'if [[ "$script" == *content_lane_format_for_coupling* ]]; then\n'
        '  echo "simulated: coupling policy probe unavailable" >&2\n'
        "  exit 1\n"
        "fi\n"
        f'printf "%s" "$script" | exec {sys.executable} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


def test_reconcile_leaves_content_format_alone_when_the_policy_probe_is_absent(
    tmp_path: Path,
):
    """No answer == no write. A bash-side fallback would be a second spelling of
    DEFAULT_PLAYBACK_FORMAT, and writing empty would silently narrow a wide box
    (outputd reads empty as S16_LE), so the key keeps whatever the box already had
    and the skip is logged."""
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_outputd_env="JASPER_OUTPUTD_CONTENT_FORMAT=S32_LE\n",
        extra_env={
            "JASPER_OUTPUT_HARDWARE_PYTHON": str(
                _python_shim_that_cannot_answer_the_coupling(tmp_path)
            )
        },
    )

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_CONTENT_FORMAT=S32_LE" in outputd_env
    assert "event=audio_hardware_reconcile.content_format_skip" in result.stderr
    assert "reason=coupling_probe_unavailable" in result.stderr
    assert "content_format=unset" in result.stderr


def _python_shim_that_cannot_answer_the_edge_format(tmp_path: Path) -> Path:
    """The DAC-axis twin of the coupling shim above: forwards every call to the
    real interpreter EXCEPT the final-edge-format probe, which it fails.

    Same fault-injection reasoning — removing the interpreter outright aborts the
    reconcile before the DAC axis is reached and would prove nothing here."""
    shim = tmp_path / "python-no-edge-format"
    shim.write_text(
        "#!/bin/bash\n"
        'script="$(cat)"\n'
        'if [[ "$script" == *final_edge_format_for* ]]; then\n'
        '  echo "simulated: DAC registry probe unavailable" >&2\n'
        "  exit 1\n"
        "fi\n"
        f'printf "%s" "$script" | exec {sys.executable} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


def test_reconcile_leaves_the_edge_format_alone_when_the_registry_probe_is_absent(
    tmp_path: Path,
):
    """A lost probe must not commit an empty edge format.

    Empty is a MEANINGFUL value on this key — outputd reads it as S16_LE — so
    writing it here would silently NARROW this box's declared S24_3LE edge with
    no error anywhere, misrepresenting the electrical edge outputd actually
    opens at. Nothing about the hardware changed on a lost probe, so the
    previous value is still the right one: keep it and log the skip.

    The recognized-DAC path only. The deliberate explicit-empty write for a
    DAC with no queryable profile is a different branch, where emptiness IS the
    answer — asserted in
    test_reconcile_dual_apple_defers_runtime_until_active_graph_is_loaded.
    """
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_outputd_env="JASPER_OUTPUTD_DAC_FORMAT=S24_3LE\n",
        extra_env={
            "JASPER_OUTPUT_HARDWARE_PYTHON": str(
                _python_shim_that_cannot_answer_the_edge_format(tmp_path)
            )
        },
    )

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    # Preserved, not cleared — and specifically NOT the explicit-empty spelling
    # the unrecognized-DAC branch writes.
    assert "JASPER_OUTPUTD_DAC_FORMAT=S24_3LE" in outputd_env
    assert "JASPER_OUTPUTD_DAC_FORMAT=''" not in outputd_env
    assert "event=audio_hardware_reconcile.dac_format_skip" in result.stderr
    assert "reason=registry_probe_unavailable" in result.stderr
    assert "dac_id=apple_usb_c_dongle" in result.stderr


def test_reconcile_leaves_the_composite_edge_format_alone_when_the_registry_probe_is_absent(
    tmp_path: Path,
):
    """The dual-Apple composite arm's call to emit_dac_format_for_recognized,
    inside apply_audio_runtime_env's `mode=dual_apple` branch, is the
    single-arm test's twin. It shares the same helper, but until this test
    existed that call site was unpinned — a revert to write-through form
    there would still pass the whole suite.

    The value seeded here (S24_3LE) is the stale single-Apple-dongle format a
    box would carry across a single -> dual upgrade: outputd's composite sink
    has no packed-24 child write path, so S24_3LE is wrong for the composite
    id even though it was right for the prior single id. A lost probe must
    still SKIP rather than guess — committing empty would be read as S16_LE
    (arguably the right answer here) purely by accident, and the skip
    contract has to hold independent of whether the stale value happens to be
    survivable.
    """
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
            "measurement_id": "unit-test-dual-apple-composite-skip",
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
        initial_outputd_env="JASPER_OUTPUTD_DAC_FORMAT=S24_3LE\n",
        extra_env={
            "JASPER_SYS_CLASS_SOUND": str(sys_class),
            "JASPER_PROC_ASOUND": str(proc_asound),
            "JASPER_OUTPUT_TOPOLOGY_PATH": str(topology_path),
            **_active_graph_env(tmp_path, write_topology=False),
            "JASPER_OUTPUT_HARDWARE_PYTHON": str(
                _python_shim_that_cannot_answer_the_edge_format(tmp_path)
            ),
        },
    )

    assert result.returncode == 0, result.stderr
    outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    # Unchanged (skip, not empty) — the composite arm gets the same protection
    # as the single arm.
    assert "JASPER_OUTPUTD_DAC_FORMAT=S24_3LE" in outputd_env
    assert "JASPER_OUTPUTD_DAC_FORMAT=''" not in outputd_env
    assert "event=audio_hardware_reconcile.dac_format_skip" in result.stderr
    assert "reason=registry_probe_unavailable" in result.stderr
    assert "dac_id=dual_apple_usb_c_dac_4ch" in result.stderr
    # The value left in place is named on the skip line, and the runtime_env
    # summary line carries it under the same dac_format= key its
    # content_format= sibling already used.
    assert "preserved=S24_3LE" in result.stderr
    assert "event=audio_hardware_reconcile.runtime_env" in result.stderr
    assert "mode=dual_apple" in result.stderr
    assert "dac_format=S24_3LE" in result.stderr


# --- render-ring-conf-wire: the topology arm ---------------------------------


def _render_ring_conf_with_topology(conf: Path, topology_path: Path) -> int:
    from jasper.cli.audio_config import main as audio_config_main

    return audio_config_main(
        [
            "render-ring-conf-wire",
            "--profile-id",
            "hifiberry_dac8x",
            "--conf-d",
            str(conf),
            "--output-topology",
            str(topology_path),
        ]
    )


def test_render_subcommand_reports_the_full_wire_it_resolved(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    # Every axis the renderer resolved is emitted for the shell to log. A key
    # the CLI does not print is a key the reconcile journal reports as `none`,
    # which is how a per-box wire becomes invisible at the exact moment it
    # starts differing between boxes.
    conf = _drifted_ring_conf(tmp_path)
    synthetic = _synthetic_floor(RING_SLOT_FRAMES)
    monkeypatch.setattr(
        "jasper.cli.audio_config.latency_floor_for",
        lambda profile_id: synthetic if profile_id == "hifiberry_dac8x" else None,
    )

    assert _render_ring_conf(conf) == 0
    out = capsys.readouterr().out
    assert "sample_format S32_LE" in out
    assert "ring_a_channels 2" in out
    assert "ring_b_channels 2" in out
    assert "topology " in out


def test_render_subcommand_fails_safe_on_a_corrupt_topology(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """An indeterminate topology renders the SHIPPED wire and says so.

    Fail-safe direction for a RENDERER: a topology it cannot read must never
    move the conf.d off what the box is already running. Refusing to ARM on one
    is the preflights' job (they read it strictly); this only writes a file.

    CORRUPT, not absent — `load_output_topology_strict` deliberately returns an
    empty draft for a missing file ("not configured yet" is a real, ring-
    eligible shape), so an absent path is the `loaded` arm, not this one.
    """
    conf = _drifted_ring_conf(tmp_path)
    synthetic = _synthetic_floor(RING_SLOT_FRAMES)
    monkeypatch.setattr(
        "jasper.cli.audio_config.latency_floor_for",
        lambda profile_id: synthetic if profile_id == "hifiberry_dac8x" else None,
    )
    corrupt = tmp_path / "output_topology.json"
    corrupt.write_text("{not json", encoding="utf-8")

    assert _render_ring_conf_with_topology(conf, corrupt) == 0
    out = capsys.readouterr().out
    assert "topology topology_unreadable" in out
    assert "sample_format S32_LE" in out
    assert "ring_b_channels 2" in out

    from jasper import ring_assets

    assert ring_assets.ring_conf_period_frames(str(conf)) == RING_SLOT_FRAMES
    for pcm in (ring_assets.RING_A_CONF_PCM, ring_assets.RING_B_CONF_PCM):
        assert ring_assets.ring_conf_format(pcm, str(conf)) == "S32_LE"
        assert ring_assets.ring_conf_channels(pcm, str(conf)) == 2


def test_render_subcommand_reads_a_readable_topology(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    # The other arm: a topology that loads reports `loaded`, so the two cases
    # are distinguishable in the journal rather than both reading as "fine".
    import json

    from tests.test_active_speaker_runtime_contract import _full_range_stereo

    conf = _drifted_ring_conf(tmp_path)
    synthetic = _synthetic_floor(RING_SLOT_FRAMES)
    monkeypatch.setattr(
        "jasper.cli.audio_config.latency_floor_for",
        lambda profile_id: synthetic if profile_id == "hifiberry_dac8x" else None,
    )
    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text(
        json.dumps(_full_range_stereo().to_dict()), encoding="utf-8"
    )

    assert _render_ring_conf_with_topology(conf, topology_path) == 0
    out = capsys.readouterr().out
    assert "topology loaded" in out
    assert "ring_b_channels 2" in out


# --- #2285 P7: the DAC-swap edge into the coupling reconciler -----------------
#
# udev already reached this script on every controlC* event; the chain stopped
# here. These pin the edge that continues it, and — more importantly — the
# guard that keeps the two reconcilers from kicking each other forever.

_COUPLING_UNIT = "jasper-fanin-coupling-auto.service"


def _coupling_kick_lines(tmp_path: Path, result: subprocess.CompletedProcess[str]):
    """(systemctl starts of the coupling unit, the coupling_kick event lines)."""
    starts = [
        line
        for line in _systemctl_log(tmp_path).splitlines()
        if _COUPLING_UNIT in line and " start " in f" {line} "
    ]
    events = [
        line
        for line in result.stderr.splitlines()
        if "event=audio_hardware_reconcile.coupling_kick" in line
    ]
    return starts, events


def test_a_plugged_registered_dac_converges_without_an_operator(tmp_path: Path):
    """THE jts5 STORY: plug a registered DAC in, and the box arms itself.

    A first pass on a box that has never seen this DAC sets dac_env_changed and
    render_changed, so the edge fires and the coupling reconciler gets its
    chance to converge. Without this start the box would render a correct
    asound.conf, bounce outputd, and then sit on loopback forever waiting for a
    human to type the arm — which post-#2534 is the #2261 park, not a working
    speaker.
    """
    result = _run_reconcile(tmp_path, INNOMAKER_LISTING, "--reason", "udev")

    assert result.returncode == 0, result.stderr
    starts, events = _coupling_kick_lines(tmp_path, result)
    assert starts, _systemctl_log(tmp_path)
    assert all("--no-block" in line for line in starts), starts
    assert len(events) == 1 and "result=started" in events[0], events


def test_an_unrecognized_dac_parks_and_does_not_kick_the_coupling(tmp_path: Path):
    """THE OTHER HALF: an unproven shape parks loudly and converges nothing.

    There is no output for a coupling to converge onto, so the park (#2261) is
    the end state — not something to reconcile out of here.
    """
    result = _run_reconcile(tmp_path, "", "--reason", "udev")

    starts, events = _coupling_kick_lines(tmp_path, result)
    assert starts == [], _systemctl_log(tmp_path)
    assert events == [], events


def test_a_no_change_pass_still_reconciles_topology_coupling(tmp_path: Path):
    """Topology may change while DAC identity and rendered bytes stay stable."""
    from jasper.output_topology import OutputTopology, save_output_topology
    from tests.test_active_speaker_runtime_contract import _full_range_stereo

    configured = _full_range_stereo()
    unconfigured = configured.to_dict()
    unconfigured["speaker_groups"] = []
    unconfigured["routing"] = {}
    topology_path = tmp_path / "output_topology.json"
    save_output_topology(OutputTopology.from_mapping(unconfigured), path=topology_path)

    first = _run_reconcile(tmp_path, INNOMAKER_LISTING, "--reason", "udev")
    assert first.returncode == 0, first.stderr
    (tmp_path / "systemctl.log").write_text("", encoding="utf-8")

    # The household now commissions ordinary passive stereo. Hardware and all
    # generated DAC bytes are unchanged, but auto-coupling must see new intent.
    save_output_topology(configured, path=topology_path)
    second = _run_reconcile(tmp_path, INNOMAKER_LISTING, "--reason", "udev")

    assert second.returncode == 0, second.stderr
    assert "dac_env_changed=0" in second.stderr
    assert "render_changed=0" in second.stderr
    starts, events = _coupling_kick_lines(tmp_path, second)
    assert starts, _systemctl_log(tmp_path)
    assert all("--no-block" in line for line in starts), starts
    assert len(events) == 1 and "result=started" in events[0], events


def test_successful_runtime_convergence_is_the_coupling_kick_guard():
    """The trigger is final graph success, not DAC/render byte movement."""
    source = SCRIPT.read_text(encoding="utf-8")
    call = [
        line.strip()
        for line in source.splitlines()
        if "kick_fanin_coupling_auto_if_needed " in line and "()" not in line
    ]

    assert call == [
        'kick_fanin_coupling_auto_if_needed "$dac_env_changed" "$render_changed"'
    ], call
    call_offset = source.index(call[0])
    guard_offset = source.rfind(
        'if [[ "$runtime_converge_failed" == "0" ]]', 0, call_offset
    )
    assert guard_offset >= 0


def test_the_coupling_kick_never_blocks(tmp_path: Path):
    """--no-block IS THE DEADLOCK GUARD, not a nicety.

    The coupling pass kicks this script back SYNCHRONOUSLY inside its arm. A
    blocking start here would leave this script waiting on a pass that is
    waiting on this script.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    body = source.split("kick_fanin_coupling_auto_if_needed() {", 1)[1].split("\n}", 1)[0]
    start_lines = [line for line in body.splitlines() if "start" in line and "SYSTEMCTL" in line]

    assert start_lines, body
    assert all("--no-block" in line for line in start_lines), start_lines


def _assert_publications_agree(tmp_path: Path) -> None:
    """After one reconcile pass, JASPER_AUDIO_DAC_ID names what the record's
    ``active_profile_id`` names — the one contract between the two."""
    from jasper.env_load import parse_env_file
    from jasper.output_hardware import active_dac_profile_id, published_dac_id

    env = parse_env_file(str(tmp_path / "jasper.env"))
    recorded = active_dac_profile_id(tmp_path / "output_hardware.json")
    assert published_dac_id(env) == (recorded or "unknown")


@pytest.mark.parametrize(
    "listing",
    [APPLE_LISTING, DUAL_APPLE_LISTING, INNOMAKER_LISTING, DAC8X_STUDIO_LISTING, ""],
)
def test_env_publication_names_the_dac_the_record_names(tmp_path: Path, listing: str):
    """One reconcile pass, two publications, one answer.

    JASPER_AUDIO_DAC_ID exists for consumers that can only read env. A Python
    reader that took it instead of the record could only answer differently
    if the two publications could differ — so they may not, recognized or not.
    """
    result = _run_reconcile(tmp_path, listing, "--reason", "test")

    assert result.returncode == 0, result.stderr
    _assert_publications_agree(tmp_path)
