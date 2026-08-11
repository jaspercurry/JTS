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
    assert "JASPER_OUTPUTD_CONTENT_PCM=outputd_content_capture" in outputd_env
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
    assert "JASPER_OUTPUTD_CONTENT_PCM=outputd_content_capture" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=''" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_LANE=''" in outputd_env


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
    assert "JASPER_OUTPUTD_CONTENT_PCM=outputd_active_content_capture" in outputd_env
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


def test_reconcile_script_never_reselects_the_camilla_graph() -> None:
    """This script converges outputd, NOT the CamillaDSP graph.

    Load-bearing for a claim made in another file: ``jasper/cli/output_topology_reset.py``
    tells an operator that a failed reset leaves a SAFE residual graph, and that
    "the reconcile kick converges outputd, not CamillaDSP". This script's own
    comment says the same thing — "This script never restarts jasper-camilla
    (it bounces outputd only) … so re-rendering here does NOT make the new
    graph run" — but a comment is not a guard, and #2164 shipped two operator
    strings built on exactly this fact.

    So pin the fact where a change would break it: the executable body names
    neither ``runtime-safe-graph`` nor ``jasper-camilla`` at all. Comment lines
    are stripped first, and that strip is load-bearing rather than tidiness —
    the sentence quoted above is the file's only ``jasper-camilla`` occurrence,
    so without it this assert would fail on the very prose it exists to pin. If
    this ever must change, the reset's module docstring and both
    ``_print_summary`` remediation strings change with it.
    """
    code = "\n".join(
        line
        for line in SCRIPT.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "runtime-safe-graph" not in code
    assert "safe_graph_for_current_topology" not in code
    assert "jasper-camilla" not in code


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
        "JASPER_OUTPUTD_CONTENT_PCM=outputd_content_capture\n"
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
    assert "JASPER_OUTPUTD_CONTENT_PCM=outputd_active_content_capture" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=6" in outputd_env
    assert "JASPER_OUTPUTD_DAC_PCM=outputd_dac" in outputd_env
    assert "JASPER_OUTPUTD_DUAL_DAC_A_PCM=''" in outputd_env
    # The declared final-edge format is unchanged by arming the lane (mirrors
    # test_reconcile_innomaker_arms_the_width_two_lane_on_a_legal_active_graph
    # above).
    assert "JASPER_OUTPUTD_DAC_FORMAT=S32_LE" in outputd_env
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
    # outputd.env delta is a codified latency FLOOR re-emit — the usb_low_latency
    # route writes JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES=1536. DAC identity and
    # asound are unchanged, so wake must stay up: bounce jasper-outputd alone,
    # never stop jasper-voice, never re-run jasper-aec-reconcile.
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
    # Converged apple outputd.env: every runtime key and every apple profile
    # floor key already at steady state, WITH the direct content bridge, but
    # WITHOUT the route's content-buffer floor — so the 1536 emit is the sole
    # delta and this is a genuinely floor-only re-emit.
    outputd_env = (
        "JASPER_OUTPUTD_BACKEND=alsa\n"
        "JASPER_OUTPUTD_SINK=single_alsa\n"
        "JASPER_OUTPUTD_CONTENT_PCM=outputd_content_capture\n"
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
        initial_template=rendered_template,
    )

    assert result.returncode == 0, result.stderr
    assert _render_log(tmp_path) == ""
    new_outputd_env = (tmp_path / "outputd.env").read_text(encoding="utf-8")
    assert "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES=1536" in new_outputd_env
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
        # asound is pre-rendered for card A so render_changed stays 0; the route
        # profile supplies a coincident content-buffer floor delta.
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
    assert "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES=1536" in new_outputd_env
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
    # Fully converged apple + usb_low_latency outputd.env: the route's content
    # buffer floor (1536) is ALREADY present alongside the apple profile floor,
    # so the floor pass is a no-op and nothing commits to outputd.env.
    outputd_env = (
        "JASPER_OUTPUTD_BACKEND=alsa\n"
        "JASPER_OUTPUTD_SINK=single_alsa\n"
        "JASPER_OUTPUTD_CONTENT_PCM=outputd_content_capture\n"
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
        "JASPER_OUTPUTD_CONTENT_BRIDGE=direct\n"
        "JASPER_CAMILLA_CHUNKSIZE=256\n"
        "JASPER_CAMILLA_TARGET_LEVEL=1536\n"
        "JASPER_OUTPUTD_PERIOD_FRAMES=128\n"
        "JASPER_OUTPUTD_DAC_BUFFER_FRAMES=256\n"
        "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES=1536\n"
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
    # The content buffer is NOT pinned: it stays at outputd's packaged 4096, so
    # the content capture runs the 128/4096 pair the soak's Stage B ran.
    assert not _outputd_env_key_present(
        outputd_env, "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES"
    )
    assert (
        "event=audio_hardware_reconcile.latency_floor "
        "reason=test output_dac_id=hifiberry_dac8x camilla_chunksize=256 "
        "camilla_target_level=1536 outputd_period_frames=128 "
        "outputd_content_buffer_frames=4096 outputd_dac_buffer_frames=256"
    ) in result.stderr


def test_reconcile_no_floor_drops_stale_floor_keys(tmp_path: Path):
    # A DAC with no declared floor must DROP a stale floor a prior DAC wrote into
    # outputd.env, not leave it as `=''` (which would clobber an operator value)
    # and not leave the stale numbers. INNOMAKER is the floorless case: pointing
    # this at a profile that later declares a floor would make the loop below
    # unreachable rather than failing (what an R7a DAC8x floor did here).
    result = _run_reconcile(
        tmp_path,
        INNOMAKER_LISTING,
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


def test_reconcile_parks_before_innomaker_render_when_candidate_is_invalid(
    tmp_path: Path,
):
    prior_outputd = (
        "JASPER_OUTPUTD_BACKEND=alsa\n"
        "JASPER_OUTPUTD_SINK=single_alsa\n"
        "JASPER_OUTPUTD_CONTENT_PCM=outputd_active_content_capture\n"
        "JASPER_OUTPUTD_ACTIVE_CHANNELS=8\n"
        "JASPER_OUTPUTD_ACTIVE_LANE=1\n"
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
    assert "action=park_before_asound_render" in result.stderr
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=8" in (
        tmp_path / "outputd.env"
    ).read_text(encoding="utf-8")
    assert not (tmp_path / "asoundrc.jasper.template").exists()
    assert _render_log(tmp_path) == ""
    assert "stop jasper-voice.service jasper-outputd.service" in _systemctl_log(
        tmp_path
    )


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

    assert result.returncode == 78, result.stderr
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


def test_reconcile_apple_leaves_the_shipped_ring_conf_byte_identical(
    tmp_path: Path,
):
    # GOLDEN no-change case: the Apple dongle's declared floor IS the shipped
    # 128, so a full reconcile pass resolves the floor, finds it already
    # rendered, and never writes.
    conf = _staged_ring_conf(tmp_path)
    before_bytes = conf.read_bytes()
    before_mtime = conf.stat().st_mtime_ns

    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        extra_env={"JASPER_RING_CONF_D": str(conf)},
    )

    assert result.returncode == 0, result.stderr
    assert (
        "event=audio_hardware_reconcile.ring_conf reason=test "
        "result=unchanged output_dac_id=apple_usb_c_dongle period_frames=128 "
        "previous_period_frames=128 sample_format=S16_LE ring_a_channels=2 "
        "ring_b_channels=2 topology="
    ) in result.stderr
    assert conf.read_bytes() == before_bytes
    assert conf.stat().st_mtime_ns == before_mtime


def test_reconcile_leaves_ring_conf_untouched_for_a_floorless_dac(
    tmp_path: Path,
):
    # A recognized DAC that declares NO latency floor keeps the shipped conf.d
    # — no same-content rewrite, no mtime churn. INNOMAKER is the floorless
    # case; a profile that later declares a floor would take the RENDER branch
    # instead of this skip branch, so the id is part of the assertion.
    conf = _staged_ring_conf(tmp_path)
    before_bytes = conf.read_bytes()
    before_mtime = conf.stat().st_mtime_ns

    result = _run_reconcile(
        tmp_path,
        INNOMAKER_LISTING,
        "--reason",
        "test",
        extra_env={"JASPER_RING_CONF_D": str(conf)},
    )

    assert result.returncode == 0, result.stderr
    assert (
        "event=audio_hardware_reconcile.ring_conf reason=test result=skipped "
        "output_dac_id=innomaker_hifi_amp_pro period_frames=none "
        "previous_period_frames=none sample_format=none ring_a_channels=none "
        "ring_b_channels=none topology=none reason=no_declared_floor"
    ) in result.stderr
    assert conf.read_bytes() == before_bytes
    assert conf.stat().st_mtime_ns == before_mtime


def test_reconcile_leaves_the_shipped_ring_conf_byte_identical_for_dac8x(
    tmp_path: Path,
):
    # R7a's conf.d consequence, stated as a test rather than left to be
    # discovered: declaring a 128-frame floor moves a DAC8x box from the
    # SKIP branch to the RENDER branch — and the render is a no-op, because the
    # shipped conf.d already carries this box's wire (period 128, and a roleful
    # topology has no ring width, so Ring B keeps the shipped stereo
    # declaration). Byte AND mtime identical: no ALSA file churn on any DAC8x
    # box at floor-deploy.
    conf = _staged_ring_conf(tmp_path)
    before_bytes = conf.read_bytes()
    before_mtime = conf.stat().st_mtime_ns

    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--reason",
        "test",
        extra_env={"JASPER_RING_CONF_D": str(conf)},
    )

    assert result.returncode == 0, result.stderr
    assert (
        "event=audio_hardware_reconcile.ring_conf reason=test "
        "result=unchanged output_dac_id=hifiberry_dac8x period_frames=128 "
        "previous_period_frames=128 sample_format=S16_LE ring_a_channels=2 "
        "ring_b_channels=2 topology="
    ) in result.stderr
    assert conf.read_bytes() == before_bytes
    assert conf.stat().st_mtime_ns == before_mtime


def test_reconcile_leaves_ring_conf_untouched_when_no_dac_is_recognized(
    tmp_path: Path,
):
    # No recognized DAC -> no profile -> no floor -> untouched, without even
    # reaching the Python layer.
    conf = _staged_ring_conf(tmp_path)
    before_bytes = conf.read_bytes()
    before_mtime = conf.stat().st_mtime_ns

    result = _run_reconcile(
        tmp_path,
        "",
        "--reason",
        "test",
        extra_env={"JASPER_RING_CONF_D": str(conf)},
    )

    assert result.returncode == 0, result.stderr
    assert (
        "event=audio_hardware_reconcile.ring_conf reason=test "
        "result=skipped reason=dac_unrecognized"
    ) in result.stderr
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
    assert (
        conf.read_text(encoding="utf-8").count(
            f"    period_frames {RING_SLOT_FRAMES}"
        )
        == 2
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
    ring = conf_dir / "outputd-cutover-ring.yml"
    # Width-matched: the mono topology claims output 0, so channel 1 is muted.
    assert "as_out1_commission_mute" in cutover.read_text(encoding="utf-8")
    assert ring.exists()
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
    reset's contract call, the carrier's `can_host_eq`). The reconciler has
    NOTHING behind it: it renders on boot / udev / topology-save, and CamillaDSP
    loads the cutover from its statefile on its next start with no ordering to
    this. So a soft failure here would overwrite a healthy width-matched graph
    with an unmuted one and log success — silently, in the hazard direction.
    Keeping the previous file is the fail-closed answer.
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
    """MISSING is not CORRUPT. A fresh box declares nothing, so nothing is
    undeclared, and the golden unmuted graph is the correct render."""
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


def test_reconcile_emits_the_narrow_content_format_on_a_ring_box(tmp_path: Path):
    """An armed shm_ring box keeps the content hop at the ring's wire format —
    the belt to ring_edge_width_ready's suspender (the PR-6 ring ruling). Its
    outputd must request what the ring actually carries, not the box-wide
    program-lane default."""
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
    assert "JASPER_OUTPUTD_CONTENT_FORMAT=S16_LE" in outputd_env
    assert "content_format=S16_LE" in result.stderr


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
    speaker). With the bridge gone that pairing cannot exist: outputd fail-safes
    every `rate_match` spelling to `direct`, which accepts the wide lane. So the
    reconciler must now emit the COUPLING's own format — proving the narrowing
    is really gone rather than merely unreachable, for every spelling the
    deleted parse used to accept.
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
    no error anywhere, and because the edge format is a chip-AEC alignment
    identity input it would also invalidate the box's commissioned artifact.
    Nothing about the hardware changed on a lost probe, so the previous value is
    still the right one: keep it and log the skip.

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
    assert "sample_format S16_LE" in out
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
    assert "sample_format S16_LE" in out
    assert "ring_b_channels 2" in out

    from jasper import ring_assets

    assert ring_assets.ring_conf_period_frames(str(conf)) == RING_SLOT_FRAMES
    for pcm in (ring_assets.RING_A_CONF_PCM, ring_assets.RING_B_CONF_PCM):
        assert ring_assets.ring_conf_format(pcm, str(conf)) == "S16_LE"
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
