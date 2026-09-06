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
from tests._lock_holder import spawn_lock_holder
from tests._log_events import stderr_events
from tests.reconcile_fixtures import (
    fake_systemctl as _fake_systemctl,
    systemctl_log as _systemctl_log,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-audio-hardware-reconcile"
SHIPPED_RING_CONF = ROOT / "deploy" / "alsa" / "conf.d" / "60-jts-ring.conf"


def _fake_aplay(tmp_path: Path, listing: str) -> Path:
    fake = tmp_path / "aplay"
    fake.write_text(
        "#!/usr/bin/env bash\ncat \"$JASPER_FAKE_APLAY_LISTING\"\n",
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
        "cp \"$JASPER_ASOUND_TEMPLATE\" \"$JASPER_ASOUND_CONF\"\n"
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
    script: Path = SCRIPT,
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
        "JASPER_ALSA_RATE_CONVERTER=samplerate_medium\n", encoding="utf-8"
    )
    for text, name in (
        (initial_env, "jasper.env"),
        (initial_outputd_env, "outputd.env"),
        (initial_fanin_env, "fanin.env"),
        (initial_template, "asoundrc.jasper.template"),
    ):
        if text is not None:
            (tmp_path / name).write_text(text, encoding="utf-8")
    model = tmp_path / "model"
    boot_config = tmp_path / "config.txt"
    udc = tmp_path / f"udc-{active_usb_role}"
    model.write_text(board_model, encoding="utf-8")
    boot_config.write_text(
        initial_boot_config or "[all]\ndtoverlay=dwc2,dr_mode=peripheral\n",
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
            # Hermetic active-graph gate inputs: tmp paths that are ABSENT
            # unless a test stages them via _active_graph_env(). Without this
            # the gate reads the real /var/lib/jasper paths on a dev box.
            "JASPER_CAMILLA_STATEFILE": str(tmp_path / "outputd-statefile.yml"),
            "JASPER_OUTPUT_TOPOLOGY_PATH": str(tmp_path / "output_topology.json"),
            "JASPER_CAMILLA2_STATEFILE": str(tmp_path / "crossover-statefile.yml"),
            "JASPER_CAMILLA_CONF_DIR": str(tmp_path / "camilladsp"),
            # Hermetic: source the repo's shared libs, never a stale installed
            # copy under /usr/local/lib.
            "JASPER_ENV_FILE_LIB": str(ROOT / "deploy" / "lib" / "jasper-env-file.sh"),
            "JASPER_ASOUND_RENDER_LIB": str(
                ROOT / "deploy" / "lib" / "jasper-asound-render.sh"
            ),
        }
    )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(script), *args],
        check=False,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
    )


def _assert_no_empty_alsa_card(rendered: str) -> None:
    assert not re.search(r"(?m)^\s*card\s*$", rendered)
    assert not re.search(r"\bcard\s+}", rendered)


def _assert_parked_outputd_dac_template(rendered: str) -> None:
    _assert_states(rendered, "pcm.outputd_dac", "type null")
    assert "ctl.outputd_dac" not in rendered
    _assert_no_empty_alsa_card(rendered)


def _log_token(value: str) -> str:
    """Mirror `jasper_asound_log_token`'s `tr -c 'A-Za-z0-9_.:,-' '_'`."""
    return re.sub(r"[^A-Za-z0-9_.:,-]", "_", value)


def _render_log(tmp_path: Path) -> str:
    log = tmp_path / "render.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""



def _assert_states(text: str, *needles: str) -> None:
    """Every needle present. Reports ALL that are missing, not just the first."""
    assert [n for n in needles if n not in text] == [], text


def _assert_omits(text: str, *needles: str) -> None:
    assert [n for n in needles if n in text] == [], text

def _outputd_env(tmp_path: Path) -> str:
    return (tmp_path / "outputd.env").read_text(encoding="utf-8")


def _jasper_env(tmp_path: Path) -> str:
    return (tmp_path / "jasper.env").read_text(encoding="utf-8")


def _template(tmp_path: Path) -> str:
    return (tmp_path / "asoundrc.jasper.template").read_text(encoding="utf-8")


def _outputd_env_key_present(outputd_env: str, key: str) -> bool:
    return any(
        re.match(rf"^\s*{re.escape(key)}\s*=", line)
        for line in outputd_env.splitlines()
    )


def _output_hardware_record(tmp_path: Path) -> dict:
    return json.loads(
        (tmp_path / "output_hardware.json").read_text(encoding="utf-8")
    )


def _fake_sys_output_card(
    tmp_path: Path, *, card_index: int, card_id: str, usb_path: str, serial: str
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
        "Playback:\n  Endpoint: 0x01 (SYNC)\n", encoding="utf-8"
    )
    return sys_class, proc_asound


# (card index, /proc/asound id, USB serial). The composite's A/B order comes
# from the saved topology's serials, so a listing whose card ids sort the
# other way round is what proves the order is not enumeration order.
_DUAL_APPLE_CARDS = ((1, "A", "left"), (2, "A_1", "right"))
_DUAL_APPLE_CARDS_SWAPPED = ((1, "B", "right"), (2, "A", "left"))


def _dual_apple_cards(tmp_path: Path, cards=_DUAL_APPLE_CARDS) -> dict[str, str]:
    sys_class = proc_asound = Path()
    for card_index, card_id, serial in cards:
        sys_class, proc_asound = _fake_sys_output_card(
            tmp_path,
            card_index=card_index,
            card_id=card_id,
            usb_path=f"1-{card_index}",
            serial=serial,
        )
    return {
        "JASPER_SYS_CLASS_SOUND": str(sys_class),
        "JASPER_PROC_ASOUND": str(proc_asound),
    }


def _dual_apple_topology(tmp_path: Path, *, active: bool = False) -> Path:
    """The saved topology of a dual-Apple pair, pinning its child order.

    Without a saved order ``apply_observed_composite_policy`` parks at
    ``park_unstable_child_order`` before the active-graph gate ever runs.
    ``active=True`` makes it a legal ACTIVE topology (roleful groups plus
    passed clock evidence) so the composite arm can reach ``recognized=1``.
    """
    hardware: dict = {
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
    }
    if active:
        from tests.test_active_speaker_runtime_contract import _active_topology

        payload = _active_topology("stereo", "active_2_way").to_dict()
    else:
        hardware["outputs"] = []
        payload = {
            "artifact_schema_version": 1,
            "kind": "jts_output_topology",
            "status": "ready",
            "speaker_groups": [],
            "routing": {},
            "safety": {},
        }
    payload.update(
        {"topology_id": "dual_apple", "name": "Dual Apple", "hardware": hardware}
    )
    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text(json.dumps(payload), encoding="utf-8")
    return topology_path


def _preset_and_topology(channels: int, *, strict: bool = False):
    from jasper.active_speaker import ActiveSpeakerPreset
    from tests.test_active_speaker_profile import _three_way_preset, _two_way_preset
    from tests.test_active_speaker_runtime_contract import _active_topology

    known = {
        2: ("mono", "active_2_way", _two_way_preset),
        4: ("stereo", "active_2_way", _two_way_preset),
        6: ("stereo", "active_3_way", _three_way_preset),
    }
    if strict and channels not in known:
        raise AssertionError(f"unsupported test channel count: {channels}")
    shape, layout, preset_for = known.get(
        channels, ("mono", "active_2_way", _two_way_preset)
    )
    return (
        _active_topology(shape, layout),
        ActiveSpeakerPreset.from_mapping(preset_for(shape)),
    )


def _active_graph_env(
    tmp_path: Path, *, channels: int = 4, write_topology: bool = True
) -> dict[str, str]:
    """Stage a legal active-speaker graph at ``channels`` width for the gate.

    Default 4 = the dual-Apple composite shape; 2 = the deployed mono 2-way,
    6 = a stereo 3-way DAC8x. The width-aware gate reads the runtime
    contract's playback width and compares it to the DAC's active-lane cap.
    The graph is staged at the ACTIVE RING, the one legal active endpoint: a
    graph naming the retired snd-aloop lane is not a legal active graph, so
    the gate would decline and fall through to the passive branch.
    """
    from jasper.active_speaker import emit_active_speaker_baseline_config
    from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE
    from jasper.output_topology import save_output_topology

    topology, preset = _preset_and_topology(channels)
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
    (tmp_path / "outputd-cutover.yml").write_text(
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
    out = {"JASPER_CAMILLA_STATEFILE": str(statefile)}
    if write_topology:
        out["JASPER_OUTPUT_TOPOLOGY_PATH"] = str(topology_path)
    return out


def _active_leader_graph_env(
    tmp_path: Path, *, channels: int = 2, write_crossover_statefile: bool = True
) -> dict[str, str]:
    """Stage camilla#1 program bake + camilla#2 endpoint graph for the gate."""
    from jasper.active_speaker import (
        emit_active_speaker_driver_domain_config,
        emit_active_speaker_program_bake_config,
    )
    from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE
    from jasper.output_topology import save_output_topology
    from jasper.sound.profile import SimpleEq, SoundProfile

    topology, preset = _preset_and_topology(channels, strict=True)
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
            f"config_path: {crossover_config}\n", encoding="utf-8"
        )
    return {
        "JASPER_CAMILLA_STATEFILE": str(outputd_statefile),
        "JASPER_CAMILLA2_STATEFILE": str(crossover_statefile),
        "JASPER_OUTPUT_TOPOLOGY_PATH": str(topology_path),
    }


def _apple_active_graph_env(tmp_path: Path) -> dict[str, str]:
    from jasper.output_topology import OutputTopology, save_output_topology

    env = _active_graph_env(tmp_path, channels=2)
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


APPLE_ENV = "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\nJASPER_AUDIO_DAC_CARD=A\n"

# An asound template already rendered for the Apple dongle's card, so a
# reconcile of that same card leaves render_changed=0.
APPLE_RENDERED_TEMPLATE = (
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

_APPLE_STEADY_OUTPUTD_ENV: tuple[tuple[str, str], ...] = (
    ("JASPER_OUTPUTD_BACKEND", "alsa"),
    ("JASPER_OUTPUTD_SINK", "single_alsa"),
    ("JASPER_OUTPUTD_DAC_PCM", "outputd_dac"),
    ("JASPER_OUTPUTD_DUAL_DAC_A_PCM", "''"),
    ("JASPER_OUTPUTD_DUAL_DAC_B_PCM", "''"),
    ("JASPER_OUTPUTD_CONTENT_FORMAT", "S32_LE"),
    ("JASPER_OUTPUTD_DAC_FORMAT", "S24_3LE"),
    ("JASPER_OUTPUTD_ACTIVE_CHANNELS", "''"),
    ("JASPER_OUTPUTD_ACTIVE_LANE", "''"),
    ("JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT", "''"),
    ("JASPER_CAMILLA_CHUNKSIZE", "256"),
    ("JASPER_CAMILLA_TARGET_LEVEL", "1536"),
    ("JASPER_OUTPUTD_PERIOD_FRAMES", "128"),
    ("JASPER_OUTPUTD_DAC_BUFFER_FRAMES", "256"),
)


def _apple_steady_outputd_env(*, drop: tuple[str, ...] = (), extra: str = "") -> str:
    """Every outputd runtime key at the Apple dongle's converged value.

    A key missing from this set is a delta the next reconcile commits, so a
    test wanting exactly one delta drops exactly one key. The values are the
    ones the reconciler itself writes (registry edge format, declared floor,
    coupling content format, and the ACTIVE_LANE/RING_ACTIVE_ENDPOINT pair
    that one helper writes together); seeding a stale one would falsify an
    "only X moved" premise while its assertions still passed.
    """
    return "".join(
        f"{key}={value}\n"
        for key, value in _APPLE_STEADY_OUTPUTD_ENV
        if key not in drop
    ) + extra


# --- source-level pins: things a subprocess run cannot observe ----------------

_RING_CONF_LOG_KEYS = (
    "result",
    "period_frames",
    "previous_period_frames",
    "sample_format",
    "ring_a_channels",
    "ring_b_channels",
    "topology",
    "reason",
    "conf",
)

_SCRIPT_STATES = (
    # The env writer's permission arguments. A DAC reconcile must not turn
    # root:jasper jasper.env into root:root (jasper-control needs group read
    # for fresh /state), and must repair generated /var/lib/jasper env-file
    # permissions on no-op runs.
    'jasper_env_file_set "$file" "$key" "$value" 0640 0750',
    'jasper_env_file_repair_permissions "$OUTPUTD_ENV_FILE" 0640',
    'jasper_env_file_repair_permissions "$FANIN_ENV_FILE" 0640',
    # The latency floor and the endpoint contract are the runtime plan's
    # answers, fetched over the CLI with the fan-in env and BOTH camilla
    # statefiles — never a second copy of the registry in bash.
    "jasper.cli.audio_config",
    "outputd-floor-actions",
    "validate-outputd-env",
    '--fanin-env "$FANIN_ENV_FILE"',
    '--camilla-statefile "$CAMILLA_STATEFILE"',
    '--camilla2-statefile "$CAMILLA2_STATEFILE"',
    "outputd-capture-device",
    # The ring conf.d render is delegated to the Python layer, which owns the
    # installed path; the only --conf-d passed is the test override.
    "render-ring-conf-wire",
    'RING_CONF_D_OVERRIDE="${JASPER_RING_CONF_D:-}"',
    '--conf-d "$RING_CONF_D_OVERRIDE"',
    # Ring B's channel count is topology-resolved.
    '--output-topology "$OUTPUT_TOPOLOGY_PATH"',
    # The `key value` protocol is a WHITELIST — an unmatched key is dropped
    # silently, so every key the renderer emits needs an arm or the wire it
    # resolved never reaches the journal.
    *(f"            {key}) " for key in _RING_CONF_LOG_KEYS),
)

_SCRIPT_OMITS = (
    # Convergence writes a statefile; it never gates on a running CamillaDSP.
    "is-active --quiet jasper-camilla.service",
    # ADR-0100 retired the content lane; neither spelling survives.
    '"outputd_active_content_capture"',
    '"outputd_content_capture"',
    # The floor is not re-derived in bash.
    "latency_floor_for_dac()",
    "from jasper.audio_hardware.dac import latency_floor_for",
    # The installed conf.d path is the Python SSOT's, not a fourth copy here.
    "/etc/alsa/conf.d",
    # Arming is the coupling reconciler's job; ALSA re-reads conf.d at the
    # next PCM open, so the render must not feed a restart flag.
    "render_ring_conf_if_needed && ",
    # The i16-only rate_match content bridge was deleted, and so was the
    # S16_LE narrowing that existed only to feed it. A behavioural test
    # passes just as well with a loop that never matches; this is what fails
    # if the bash side is left behind.
    "RATE_MATCH_BRIDGE_ALIASES",
    "reason=rate_match_content_bridge",
)


@pytest.mark.parametrize("needle", _SCRIPT_STATES)
def test_the_script_states_the_contract_it_must_state(needle: str) -> None:
    assert needle in SCRIPT.read_text(encoding="utf-8")


@pytest.mark.parametrize("needle", _SCRIPT_OMITS)
def test_the_script_carries_no_retired_machinery(needle: str) -> None:
    assert needle not in SCRIPT.read_text(encoding="utf-8")


def test_reconcile_script_selects_the_final_graph_before_outputd_gating() -> None:
    """One root path renders, applies, then derives outputd's active lane."""
    code = "\n".join(
        line
        for line in SCRIPT.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    _assert_states(code, "runtime-safe-graph", "converge_runtime_graph")
    # rindex targets the execution block, rather than the function definitions.
    assert code.rindex("render_flat_cutover_if_needed") < code.rindex(
        "converge_runtime_graph"
    )
    assert code.rindex("converge_runtime_graph") < code.rindex("gate_role_services")


def test_reconcile_refuses_a_post_convergence_outputd_rejection() -> None:
    """The second candidate is the final safety gate, not a best-effort write."""
    code = SCRIPT.read_text(encoding="utf-8")
    final_commit = code.rindex("if commit_outputd_env_stage; then")
    rejection = code.index("reason=post_convergence_outputd_env_rejected", final_commit)
    gate = code.index("gate_role_services", final_commit)

    assert final_commit < rejection < gate
    assert "runtime_converge_failed=1" in code[final_commit:gate]
    assert "exit 78" in code[final_commit:gate]


def test_camilla_boot_requires_successful_runtime_graph_convergence() -> None:
    """A stale statefile cannot start Camilla after a failed boot reconcile."""
    camilla_unit = (ROOT / "deploy" / "systemd" / "jasper-camilla.service").read_text(
        encoding="utf-8"
    )
    hardware_unit = (
        ROOT / "deploy" / "systemd" / "jasper-audio-hardware-reconcile.service"
    ).read_text(encoding="utf-8")

    assert "Requires=jasper-audio-hardware-reconcile.service" in camilla_unit
    after_line = next(
        line for line in camilla_unit.splitlines() if line.startswith("After=")
    )
    assert "jasper-audio-hardware-reconcile.service" in after_line
    # The required oneshot runs the same reconciler whose final command status
    # is nonzero when runtime convergence fails.
    assert "ExecStart=/usr/local/sbin/jasper-audio-hardware-reconcile" in hardware_unit
    assert '[[ "$runtime_converge_failed" == "0" ]]' in SCRIPT.read_text(
        encoding="utf-8"
    )


def test_runtime_convergence_only_writes_statefile(tmp_path: Path) -> None:
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
    _assert_states(call, "runtime-safe-graph", "--write-statefile")
    _assert_omits(call, "--apply-live", "--preserve-live-transport")


# --- I2S HAT boot intent ------------------------------------------------------


def test_i2s_reboot_marker_tracks_desired_versus_observed(tmp_path: Path):
    model = "Raspberry Pi Zero 2 W Rev 1.0"
    (tmp_path / "install_profile").write_text("streambox\n", encoding="utf-8")
    intent = tmp_path / "i2s_hat.env"
    marker = tmp_path / "i2s-reboot"
    intent.write_text(
        "JASPER_I2S_HAT_PROFILE=innomaker_hifi_amp_pro\n", encoding="utf-8"
    )

    first = _run_reconcile(
        tmp_path, "", "--reason", "hat-enable",
        initial_boot_config="[all]\ndtoverlay=dwc2,dr_mode=host\n",
        board_model=model, active_usb_role="host",
    )
    applied_boot = (tmp_path / "config.txt").read_text(encoding="utf-8")
    assert first.returncode == 0, first.stderr
    assert marker.is_file()
    assert "dtoverlay=dwc2,dr_mode=peripheral" in applied_boot
    assert "output_parked" in first.stderr

    def rerun(listing: str = "", *, reason: str = "udev", **kwargs):
        return _run_reconcile(
            tmp_path, listing, "--reason", reason, initial_boot_config=applied_boot,
            board_model=model, active_usb_role="peripheral", **kwargs,
        )

    marker.unlink()  # a reboot naturally clears /run
    # The boot line is already in place, so this pass changes nothing -- and
    # the marker is still raised, because the desired HAT is not what is
    # running. State, not edge (an install-time pass can write the line first).
    second = rerun(reason="boot")
    assert second.returncode == 0, second.stderr
    assert marker.is_file()

    third = rerun()
    assert third.returncode == 0, third.stderr
    assert marker.is_file()

    (tmp_path / "systemctl.log").unlink(missing_ok=True)
    matched = rerun(INNOMAKER_LISTING)
    assert matched.returncode == 0, matched.stderr
    assert not marker.exists()  # desired and runtime now agree
    commands = _systemctl_log(tmp_path)
    assert "--no-block restart jasper-outputd.service" in commands
    assert "stop jasper-voice.service" not in commands
    assert "restart jasper-aec-reconcile.service" not in commands

    malformed_python = tmp_path / "malformed-python"
    malformed_python.write_text(
        "#!/bin/sh\ncase \"$*\" in\n"
        f"*jasper.cli.output_hardware*) \"{sys.executable}\" \"$@\""
        " | sed '/OBSERVED_OUTPUT_PROFILE_STATUS=/d'; exit 0;;\n"
        f'esac\nPYTHONOPTIMIZE=1 exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    malformed_python.chmod(0o755)
    intent.unlink()
    # The intent FILE is gone now, not present-and-empty: absent means the
    # reconciler touches NEITHER the managed I2S block NOR the reboot
    # marker, no matter what gets observed -- including a malformed or
    # failed observation (#i2s-hat-intent).
    for extra_env in (
        {"JASPER_OUTPUT_HARDWARE_STATE_PATH": str(tmp_path)},
        {"JASPER_OUTPUT_HARDWARE_PYTHON": str(malformed_python)},
        None,
    ):
        for marker_present in (False, True):
            marker.unlink(missing_ok=True)
            if marker_present:
                marker.touch()
            observed = rerun(INNOMAKER_LISTING, extra_env=extra_env)
            assert observed.returncode == 0, observed.stderr
            assert marker.exists() is marker_present
            config_text = (tmp_path / "config.txt").read_text()
            assert "dtoverlay=merus-amp" in config_text
            assert "dtoverlay=dwc2,dr_mode=peripheral" in config_text

    disabled_boot = (tmp_path / "config.txt").read_text(encoding="utf-8")
    marker.unlink()
    parked = _run_reconcile(
        tmp_path, "", initial_boot_config=disabled_boot,
        board_model=model, active_usb_role="host",
    )
    assert parked.returncode == 0 and not marker.exists(), parked.stderr
    assert "output_parked" in parked.stderr


def _not_durable_python(tmp_path: Path, *, extra_case: str = "") -> Path:
    """A `python` stand-in whose boot-config CLI reports a non-durable publish.

    Stubbed rather than real because exit 74 needs a directory-fsync failure.
    It emits the `--env` keys `reconcile_i2s_hat_boot` reads; every other test
    here runs the real CLI, so a rename fails those rather than passing here.
    """
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "*usb_port_role*) printf '%s\\n' "
        "'JASPER_BOOT_BOARD_TOPOLOGY=separate_host_ports' "
        "'JASPER_BOOT_I2S_HAT_PROFILE=innomaker_hifi_amp_pro' "
        "'JASPER_BOOT_I2S_HAT_CHANGED=true' "
        "'JASPER_BOOT_CONFIG_PUBLISHED_NOT_DURABLE=true'; exit 74;;\n"
        f"{extra_case}"
        f'esac\nexec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    return fake_python


def test_published_not_durable_boot_change_still_sets_marker(tmp_path: Path):
    fake_python = _not_durable_python(
        tmp_path,
        extra_case=(
            "*jasper.cli.output_hardware*) echo \"OBSERVED_OUTPUT_PROFILE_ID=unknown\"; "
            "echo \"OBSERVED_OUTPUT_PROFILE_STATUS=unavailable\"; exit 0;;\n"
        ),
    )

    result = _run_reconcile(
        tmp_path, "", extra_env={"JASPER_OUTPUT_HARDWARE_PYTHON": str(fake_python)}
    )

    assert result.returncode == 74
    assert (tmp_path / "i2s-reboot").is_file()
    assert "error=boot_config_published_not_durable" in result.stderr


def test_boot_config_payload_missing_a_key_refuses_instead_of_proceeding(
    tmp_path: Path,
):
    """A key the emitter stopped writing reaches this reconciler's own refusal.

    66 and not a `set -u` abort (1) partway through the function: the boot
    config is preserved either way, but only 66 says so in the journal.
    """
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "*usb_port_role*) "
        "printf '%s\\n' 'JASPER_BOOT_BOARD_TOPOLOGY=separate_host_ports'; exit 0;;\n"
        f'esac\nexec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    result = _run_reconcile(
        tmp_path, "", extra_env={"JASPER_OUTPUT_HARDWARE_PYTHON": str(fake_python)}
    )

    assert result.returncode == 66, result.stderr


def test_record_change_with_i2s_apply_error_restarts_dac_init_before_exit(
    tmp_path: Path,
):
    """The first-ever pass always writes a changed record (an absent record
    reads as empty), so a same-pass I2S HAT apply error is enough to exercise
    the gap: the exit-74 early return sits between the record write and
    gate_role_services, so the pin restart the changed record earned has to
    fire at the exit site itself, not only from gate_role_services."""
    fake_python = _not_durable_python(tmp_path)

    result = _run_reconcile(
        tmp_path, "", extra_env={"JASPER_OUTPUT_HARDWARE_PYTHON": str(fake_python)}
    )

    assert result.returncode == 74
    assert "--no-block restart jasper-dac-init.service" in _systemctl_log(tmp_path)


# --- identity: what the registry says reaches env, template and record --------


def test_print_env_prefers_dac8x_but_keeps_apple_control_role(tmp_path: Path):
    result = _run_reconcile(tmp_path, DAC8X_AND_APPLE_LISTING, "--print-env")

    assert result.returncode == 0, result.stderr
    assert "DONGLE_CARD=A" in result.stdout
    assert "APPLE_DONGLE_PRESENT=1" in result.stdout
    assert "APPLE_DONGLE_SERVICE_CARD=auto" in result.stdout
    assert "OUTPUT_DAC_CARD=sndrpihifiberry" in result.stdout
    assert "OUTPUT_DAC_ID=hifiberry_dac8x" in result.stdout
    assert "OUTPUT_DAC_RECOGNIZED=1" in result.stdout
    assert "OUTPUT_DAC_ROUTE" not in result.stdout
    assert not (tmp_path / "jasper.env").exists()
    assert not (tmp_path / "output_hardware.json").exists()


def _pythonpath_recording_python(tmp_path: Path) -> Path:
    """The real interpreter, with the PYTHONPATH of each emitter spawn logged."""
    fake = tmp_path / "recording-python"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "${2:-}" == "jasper.cli.output_hardware" ]]; then\n'
        '  printf \'%s\\n\' "${PYTHONPATH:-}" >> "$JASPER_FAKE_PYTHONPATH_LOG"\n'
        "fi\n"
        'exec "$JASPER_FAKE_PYTHON_REAL" "$@"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def test_the_emitter_is_pinned_to_the_checkout_the_script_ran_from(tmp_path: Path):
    """install.sh runs `--print-env` from the rsynced checkout BEFORE the venv
    is refreshed, so an unpinned spawn pairs the NEW shell with the PREVIOUS
    build's emitter and every key that build never emitted reads as empty."""
    log = tmp_path / "pythonpath.log"
    # An inherited PYTHONPATH that is NOT the checkout, so the pin below can
    # only be satisfied by the script prepending its own tree.
    inherited = str(tmp_path / "inherited-site")
    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--print-env",
        extra_env={
            "JASPER_OUTPUT_HARDWARE_PYTHON": str(
                _pythonpath_recording_python(tmp_path)
            ),
            "JASPER_FAKE_PYTHON_REAL": sys.executable,
            "JASPER_FAKE_PYTHONPATH_LOG": str(log),
            "PYTHONPATH": inherited,
        },
    )

    assert result.returncode == 0, result.stderr
    recorded = log.read_text(encoding="utf-8").splitlines()
    assert recorded, "the emitter never ran"
    assert recorded == [os.pathsep.join((str(ROOT), inherited))] * len(recorded)
    assert "OUTPUT_DAC_ID=hifiberry_dac8x" in result.stdout


def _pythonpath_recording_python_for_usb_port_role(tmp_path: Path) -> Path:
    """The real interpreter, with the PYTHONPATH of the usb_port_role spawn
    logged. A sibling of `_pythonpath_recording_python` for the boot-config
    probe `reconcile_i2s_hat_boot` calls on every full (non-`--print-env`)
    pass, proving the checkout pin is not the emitter's alone."""
    fake = tmp_path / "recording-python-usb-port-role"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "${2:-}" == "jasper.audio_hardware.usb_port_role" ]]; then\n'
        '  printf \'%s\\n\' "${PYTHONPATH:-}" >> "$JASPER_FAKE_PYTHONPATH_LOG"\n'
        "fi\n"
        'exec "$JASPER_FAKE_PYTHON_REAL" "$@"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def test_the_usb_port_role_probe_is_pinned_to_the_checkout_the_script_ran_from(
    tmp_path: Path,
):
    """The checkout pin above is not the emitter's alone -- every spawn of
    `$OUTPUT_HARDWARE_PYTHON` this script makes must resolve `jasper` from the
    same tree during install's `--print-env` window and after. usb_port_role
    is `reconcile_i2s_hat_boot`'s probe, which runs on every full pass."""
    log = tmp_path / "pythonpath.log"
    inherited = str(tmp_path / "inherited-site")
    result = _run_reconcile(
        tmp_path,
        "",
        "--reason", "test",
        extra_env={
            "JASPER_OUTPUT_HARDWARE_PYTHON": str(
                _pythonpath_recording_python_for_usb_port_role(tmp_path)
            ),
            "JASPER_FAKE_PYTHON_REAL": sys.executable,
            "JASPER_FAKE_PYTHONPATH_LOG": str(log),
            "PYTHONPATH": inherited,
        },
    )

    assert result.returncode == 0, result.stderr
    recorded = log.read_text(encoding="utf-8").splitlines()
    assert recorded, "the usb_port_role probe never ran"
    assert recorded == [os.pathsep.join((str(ROOT), inherited))] * len(recorded)


def test_no_interpreter_leaves_every_observed_fact_at_its_absent_value(
    tmp_path: Path,
):
    """The classifier is the only source of hardware facts (ADR-0235 R2), so
    losing the interpreter loses all of them at once rather than half of them.

    The Apple control role is one of those facts now: with no record there is
    no card to name, and the mixer helpers stay off. The run still succeeds --
    install reads this and must not abort on a box whose venv is not built yet.
    """
    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--print-env",
        extra_env={
            "JASPER_OUTPUT_HARDWARE_PYTHON": str(tmp_path / "absent-python")
        },
    )

    assert result.returncode == 0, result.stderr
    _assert_states(
        result.stderr,
        "event=audio_hardware_reconcile.state_observed_skip ",
        "reason=python_unavailable",
    )
    _assert_states(
        result.stdout,
        "DONGLE_CARD=A",
        "APPLE_DONGLE_PRESENT=0",
        "APPLE_DONGLE_SERVICE_CARD=auto",
        "OUTPUT_DAC_ID=unknown",
        "OUTPUT_DAC_RECOGNIZED=0",
    )
    assert not (tmp_path / "output_hardware.json").exists()


def test_print_env_recognizes_dac8x_studio_role(tmp_path: Path):
    result = _run_reconcile(tmp_path, DAC8X_STUDIO_LISTING, "--print-env")

    assert result.returncode == 0, result.stderr
    assert "OUTPUT_DAC_CARD=DAC8XStudio" in result.stdout
    assert "OUTPUT_DAC_ID=hifiberry_dac8x_studio" in result.stdout
    assert "OUTPUT_DAC_RECOGNIZED=1" in result.stdout


def test_reconcile_innomaker_uses_registry_identity_and_renders_raw_hw(
    tmp_path: Path,
):
    result = _run_reconcile(tmp_path, INNOMAKER_LISTING, "--reason", "test")

    assert result.returncode == 0, result.stderr
    env_text = _jasper_env(tmp_path)
    assert "JASPER_AUDIO_DAC_ID=innomaker_hifi_amp_pro" in env_text
    assert "JASPER_AUDIO_DAC_CARD=sndrpimerusamp" in env_text
    outputd_env = _outputd_env(tmp_path)
    assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=''" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_LANE=''" in outputd_env
    assert "JASPER_OUTPUTD_DAC_FORMAT=S32_LE" in outputd_env
    assert final_edge_format_for("innomaker_hifi_amp_pro") == "S32_LE"
    template = _template(tmp_path)
    # No profile-scoped plug: every recognized single DAC renders a raw hw
    # alias, which is what outputd's format request lands on.
    assert "type plug" not in template
    _assert_states(template, "type hw", "card sndrpimerusamp", "device 0")
    assert _render_log(tmp_path) == "render\n"


def test_reconcile_apple_role_enables_apple_helpers_and_renders(tmp_path: Path):
    result = _run_reconcile(tmp_path, APPLE_LISTING, "--reason", "test")

    assert result.returncode == 0, result.stderr
    env_text = _jasper_env(tmp_path)
    assert "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle" in env_text
    assert "JASPER_AUDIO_DAC_CARD=A" in env_text
    outputd_env = _outputd_env(tmp_path)
    assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env
    # The dongle's USB descriptor advertises S16_LE and S24_3LE; the packed
    # 24-bit edge is the widest it will install.
    assert "JASPER_OUTPUTD_DAC_FORMAT=S24_3LE" in outputd_env
    assert not (tmp_path / "tts.env").exists()
    template = _template(tmp_path)
    _assert_states(template, "pcm.outputd_dac", "type hw", "card A")
    _assert_no_empty_alsa_card(template)
    assert _render_log(tmp_path) == "render\n"
    commands = _systemctl_log(tmp_path)
    assert "enable jasper-dac-init.service" in commands
    assert "enable jasper-headphone-monitor.service" in commands
    # The pin is RESTARTED: RemainAfterExit makes a `start` a no-op once the
    # one-shot has run, and at boot it can run before the record it reads
    # exists.
    assert "--no-block restart jasper-dac-init.service" in commands
    # The monitor is ensured idempotently, never restarted: this gate runs on
    # every udev/reconcile pass and a deploy fires it repeatedly inside the
    # unit's StartLimitIntervalSec, so a restart-per-pass burns StartLimitBurst
    # and parks it 'start-limit-hit'.
    assert "reset-failed jasper-headphone-monitor.service" in commands
    assert "start jasper-headphone-monitor.service" in commands
    assert "restart jasper-headphone-monitor.service" not in commands
    assert "stop jasper-voice.service" in commands
    assert "reset-failed jasper-outputd.service" in commands
    assert "--no-block restart jasper-outputd.service" in commands
    assert "--no-block restart jasper-aec-reconcile.service" in commands


@pytest.mark.parametrize(
    "locked_env,initial_fanin_env",
    [
        ("jasper.env", None),
        # The route actions are all `unset` on fanin.env, so seeding one of
        # their keys reaches apply_route_env's drop branch. That function runs
        # in an `if` CONDITION, which disables set -e for its whole body — a
        # refused lock there was discarded and the caller restarted anyway.
        ("fanin.env", "JASPER_FANIN_INPUT_RESAMPLER=1\n"),
    ],
)
def test_a_refused_env_lock_fails_the_pass_without_restarting(
    tmp_path: Path, locked_env: str, initial_fanin_env: str | None
):
    """A refused lock returns 1 from the shared writer WITHOUT writing. The
    `… && changed=1` / `file_changed=1` idioms would otherwise read that as
    "changed" and restart jasper-outputd onto the OLD lane/PCM/format while
    the unit exited 0. Removal condition: the bash env writers are gone."""
    os.mkfifo(tmp_path / f".{locked_env}.lock")

    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_fanin_env=initial_fanin_env,
    )

    assert result.returncode != 0
    assert "restart" not in _systemctl_log(tmp_path)


def test_reconcile_dac8x_role_disables_apple_helpers(tmp_path: Path):
    result = _run_reconcile(tmp_path, DAC8X_AND_APPLE_LISTING, "--reason", "test")

    assert result.returncode == 0, result.stderr
    env_text = _jasper_env(tmp_path)
    assert "JASPER_AUDIO_DAC_ID=hifiberry_dac8x" in env_text
    assert "JASPER_AUDIO_DAC_CARD=sndrpihifiberry" in env_text
    # No active baseline loaded => a DAC8x is an ordinary stereo speaker, NOT
    # the wide 8-channel active lane (fail-closed: the gate kept it stereo).
    outputd_env = _outputd_env(tmp_path)
    assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=''" in outputd_env
    assert "JASPER_OUTPUTD_DAC_FORMAT=S32_LE" in outputd_env
    assert "single_alsa_active" not in result.stderr
    assert not (tmp_path / "tts.env").exists()
    template = _template(tmp_path)
    _assert_states(template, "pcm.outputd_dac", "type hw", "card sndrpihifiberry")
    _assert_no_empty_alsa_card(template)
    commands = _systemctl_log(tmp_path)
    # The pin is enabled on every box — a DAC that declares no mixer controls
    # is jasper-dac-init's own clean exit, not a unit to disable.
    assert "enable jasper-dac-init.service" in commands
    assert "disable --now jasper-headphone-monitor.service" in commands
    assert "stop jasper-voice.service" in commands
    assert "--no-block restart jasper-outputd.service" in commands
    assert "--no-block restart jasper-aec-reconcile.service" in commands


def test_reconcile_studio_role_enables_the_mixer_pin_without_the_apple_monitor(
    tmp_path: Path,
):
    """The Studio driver writes no mixer defaults of its own, so its profile
    declares pins and the boot pin is enabled for it. The drift monitor stays
    Apple-only."""
    result = _run_reconcile(tmp_path, DAC8X_STUDIO_LISTING, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "JASPER_AUDIO_DAC_ID=hifiberry_dac8x_studio" in _jasper_env(tmp_path)
    commands = _systemctl_log(tmp_path)
    assert "enable jasper-dac-init.service" in commands
    assert "--no-block restart jasper-dac-init.service" in commands
    assert "enable jasper-headphone-monitor.service" not in commands
    assert "disable --now jasper-headphone-monitor.service" in commands


def test_reconcile_leaves_an_unchanged_record_pin_alone(tmp_path: Path):
    """The pin reads the record, so only a record that CHANGED has to re-run
    it. A restart per pass would spawn an interpreter on every udev sound
    event (ADR-0226); `start` is a no-op under RemainAfterExit."""
    _run_reconcile(tmp_path, DAC8X_STUDIO_LISTING, "--reason", "test")
    first = _systemctl_log(tmp_path)
    assert "--no-block restart jasper-dac-init.service" in first.splitlines()

    _run_reconcile(tmp_path, DAC8X_STUDIO_LISTING, "--reason", "test")
    second = _systemctl_log(tmp_path)[len(first):].splitlines()

    assert "start jasper-dac-init.service" in second
    assert "--no-block restart jasper-dac-init.service" not in second


def test_reconcile_unknown_role_renders_null_outputd_dac(tmp_path: Path):
    result = _run_reconcile(tmp_path, "", "--reason", "test")

    assert result.returncode == 0, result.stderr
    env_text = _jasper_env(tmp_path)
    _assert_states(env_text, "JASPER_AUDIO_DAC_ID=unknown", "JASPER_AUDIO_DAC_CARD=A")
    _assert_parked_outputd_dac_template(_template(tmp_path))
    assert _render_log(tmp_path) == "render\n"
    commands = _systemctl_log(tmp_path)
    assert "disable --now jasper-headphone-monitor.service" in commands
    assert "--no-block stop jasper-voice.service jasper-outputd.service" in commands
    assert "reset-failed jasper-voice.service jasper-outputd.service" in commands
    assert "restart jasper-outputd.service" not in commands
    assert "restart jasper-aec-reconcile.service" not in commands
    assert "event=audio_hardware_reconcile.output_parked" in result.stderr


@pytest.mark.parametrize(
    ("key", "filename", "mode"),
    [
        # /var/lib/jasper is 0770 root:jasper so the non-root jasper-voice/-mux
        # can write speaker_volume.json; /etc/jasper is 0755 so the group-jasper
        # doctor-json oneshot can traverse it. A blanket `install -d -m 0750`
        # in the env writer, or `-m 0755` in the asound render, re-stripped
        # those bits on every install / boot / udev-hotplug reconcile.
        pytest.param("JASPER_OUTPUTD_ENV_FILE", "outputd.env", 0o770, id="state-dir"),
        pytest.param("JASPER_ENV_FILE", "jasper.env", 0o755, id="etc-dir"),
        pytest.param(
            "JASPER_ASOUND_TEMPLATE",
            "asoundrc.jasper.template",
            0o700,
            id="asound-template-dir",
        ),
    ],
)
def test_reconcile_preserves_an_existing_parent_dir_mode(
    tmp_path: Path, key: str, filename: str, mode: int
):
    parent = tmp_path / f"parent-{filename}"
    parent.mkdir()
    parent.chmod(mode)  # explicit: mkdir's mode arg is masked by umask

    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        extra_env={key: str(parent / filename)},
    )

    assert result.returncode == 0, result.stderr
    # The reconcile actually wrote into the dir, so the mode assertion below
    # is not vacuous.
    assert (parent / filename).exists()
    assert _render_log(tmp_path) == "render\n"
    assert oct(parent.stat().st_mode & 0o777) == oct(mode)


# --- the active-lane gate -----------------------------------------------------


def _lane_query_python(tmp_path: Path, *, name: str, action: str) -> Path:
    """Real python for everything EXCEPT the lane-cap registry query.

    The reconciler feeds that query as a heredoc on stdin (``python -``),
    so the shim routes on stdin content rather than argv. ``action`` is
    shell run in place of the real query. Every other spawn — crucially
    the recognition probe — still runs for real.
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


# Rewrite the InnoMaker registry entry to a lane-less clone, then run the
# reconciler's own query heredoc — so the `none` sentinel is pinned end to end
# rather than by faking the resolver's answer.
_LANE_LESS_REGISTRY = (
    "import dataclasses as _dc;"
    "import jasper.audio_hardware.dac as _d;"
    "_p = _dc.replace("
    "_d.INNOMAKER_HIFI_AMP_PRO,"
    " supports_active_outputd_lane=False,"
    " active_outputd_lane_channels=None);"
    "_d._BY_ID = {**_d._BY_ID, _p.id: _p}"
)


@pytest.mark.parametrize(
    ("name", "action", "expected", "other"),
    [
        # Declaring the lane only means the width gate RUNS. Active mode still
        # needs a legal active graph to be the live CamillaDSP config, which
        # only commissioning produces. With no statefile staged the gate
        # declines and the box resolves byte-identically passive — and the
        # token names the gate's own decline, because the remedy is
        # commissioning, not "choose a different layout at /sound/setup/".
        pytest.param(None, None, "camilla_statefile_missing", "dac_no_active_lane",
                     id="no-active-graph-staged"),
        # `active_lane_channels_for_dac` swallows its own spawn failure, so an
        # OOM-kill or fork-EAGAIN hitting THAT one spawn yields an empty cap
        # while the DAC is still RECOGNIZED. That is TRANSIENT — reporting it
        # as dac_no_active_lane would give a remedy ("re-running cannot change
        # it") that is false here.
        pytest.param("flaky-python", "exit 1", "lane_probe_failed",
                     "dac_no_active_lane", id="lane-probe-died"),
        # The other side of the split: a profile that genuinely declares no
        # lane keeps the actionable token. That is its surviving population —
        # the next passive-only board the registry meets.
        pytest.param("lane-less-python", f'src="{_LANE_LESS_REGISTRY}"$\'\\n\'"$src"',
                     "dac_no_active_lane", "lane_probe_failed", id="lane-less-profile"),
    ],
)
def test_reconcile_names_why_it_stayed_passive_and_stays_passive(
    tmp_path: Path,
    name: str | None,
    action: str | None,
    expected: str,
    other: str,
):
    """THE FAIL-CLOSED ACTIVATION PROPERTY, and the reason it reports.

    Every arm resolves passive; only the named reason differs, and the three
    reasons carry different remedies so they may not collapse into one token.
    """
    extra_env = (
        None
        if name is None
        else {
            "JASPER_OUTPUT_HARDWARE_PYTHON": str(
                _lane_query_python(tmp_path, name=name, action=action or "")
            )
        }
    )
    result = _run_reconcile(
        tmp_path, INNOMAKER_LISTING, "--reason", "test", extra_env=extra_env
    )

    assert result.returncode == 0, result.stderr
    assert f"active_graph={expected}" in result.stderr
    assert f"active_graph={other}" not in result.stderr
    assert "active_graph=none" not in result.stderr
    outputd_env = _outputd_env(tmp_path)
    assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env
    assert "JASPER_OUTPUTD_CONTENT_PCM" not in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=''" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_LANE=''" in outputd_env


@pytest.mark.parametrize(
    "stale",
    [
        "JASPER_OUTPUTD_CONTENT_PCM=''\n",
        "JASPER_OUTPUTD_CONTENT_PCM=outputd_content_capture\n",
    ],
)
def test_reconcile_removes_a_stale_content_pcm_line(tmp_path: Path, stale: str):
    """A box that reconciled before ADR-0100 carries the retired key; ONE
    reconcile must drop the LINE, not merely stop restating it.

    set_env_file_var_if_changed is a per-key upsert, so without an active
    removal the leftover outlives the lane forever. Present-but-EMPTY is
    the shape that bites: audio_runtime_plan's retired-route describer
    defaults on an ABSENT key, so an empty one reports a post-DSP route
    disconnection no later reconcile could clear.
    """
    result = _run_reconcile(
        tmp_path, APPLE_LISTING, "--reason", "test", initial_outputd_env=stale
    )

    assert result.returncode == 0, result.stderr
    assert "JASPER_OUTPUTD_CONTENT_PCM" not in _outputd_env(tmp_path)


def _stage_candidate_debris(tmp_path: Path) -> list[str]:
    return sorted(
        name
        for name in os.listdir(tmp_path)
        if name.lstrip(".").startswith("outputd.env.candidate.")
    )


def test_outputd_env_stage_waits_out_a_concurrent_whole_file_writer(
    tmp_path: Path,
) -> None:
    """The stage→validate→rename sequence must be serialized, not just atomic."""
    outputd_env = tmp_path / "outputd.env"
    outputd_env.write_text(
        "JASPER_OUTPUTD_CONTENT_PCM=outputd_content_capture\n", encoding="utf-8"
    )

    # Longer than a whole unblocked pass, so the reconciler is provably still
    # at its first stage when the write-back lands.
    with spawn_lock_holder(
        outputd_env, hold_seconds=4, write_back="JASPER_OUTPUTD_HOLDER=1\n"
    ):
        result = _run_reconcile(tmp_path, APPLE_LISTING, "--reason", "test")

    assert result.returncode == 0, result.stderr
    committed = _outputd_env(tmp_path)
    assert _outputd_env_key_present(committed, "JASPER_OUTPUTD_HOLDER")
    assert _outputd_env_key_present(committed, "JASPER_OUTPUTD_BACKEND")
    # Staged from the holder's file, not from the pre-holder snapshot.
    assert not _outputd_env_key_present(committed, "JASPER_OUTPUTD_CONTENT_PCM")
    # Each pass mktemps a new candidate name, and the single-key writer locks
    # beside it: an unswept sibling per changing pass would accumulate forever.
    assert _stage_candidate_debris(tmp_path) == []


def test_outputd_env_stage_publishes_when_the_hold_is_refused(
    tmp_path: Path,
) -> None:
    """A hold nobody will hand over must not fail the pass."""
    outputd_env = tmp_path / "outputd.env"
    outputd_env.write_text("JASPER_OUTPUTD_CONTENT_PCM=stale\n", encoding="utf-8")

    # Held for the whole reconciler run — spawn_lock_holder's __exit__ kills
    # the holder instead of waiting hold_seconds out — so it always outlasts
    # the lib's own bounded `flock -w 10`, no matter how loaded the box is.
    with spawn_lock_holder(outputd_env, hold_seconds=300):
        result = _run_reconcile(tmp_path, APPLE_LISTING, "--reason", "test")

    assert result.returncode == 0, result.stderr
    unheld = stderr_events(
        result.stderr, "audio_hardware_reconcile.outputd_env_stage_unlocked"
    )
    assert unheld, result.stderr
    assert {fields["reason"] for fields in unheld} == {"stage_lock_unheld"}
    assert _outputd_env_key_present(_outputd_env(tmp_path), "JASPER_OUTPUTD_BACKEND")


def test_outputd_env_stage_refused_hold_leaves_a_foreign_candidate_in_place(
    tmp_path: Path,
) -> None:
    """A refused hold must not sweep a live holder's in-flight candidate."""
    outputd_env = tmp_path / "outputd.env"
    outputd_env.write_text("JASPER_OUTPUTD_CONTENT_PCM=stale\n", encoding="utf-8")
    foreign_candidate = tmp_path / ".outputd.env.candidate.live"
    foreign_candidate.write_text("JASPER_OUTPUTD_BACKEND=inflight\n", encoding="utf-8")

    # This script stages twice per pass (pre- and post-graph-convergence),
    # each retrying the hold with its own `flock -w 10`. Held for the whole
    # reconciler run — spawn_lock_holder's __exit__ kills the holder instead
    # of waiting hold_seconds out — so BOTH attempts always time out, no
    # matter how loaded the box is.
    with spawn_lock_holder(outputd_env, hold_seconds=300):
        result = _run_reconcile(tmp_path, APPLE_LISTING, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert _stage_candidate_debris(tmp_path) == [foreign_candidate.name]


def test_outputd_env_stage_sweeps_debris_from_an_earlier_pass(
    tmp_path: Path,
) -> None:
    """A pass killed between the mktemp and its trap must not leave a candidate."""
    stale_candidate = tmp_path / ".outputd.env.candidate.aaaaaa"
    stale_lock = tmp_path / "..outputd.env.candidate.aaaaaa.lock"
    stale_candidate.write_text("JASPER_OUTPUTD_BACKEND=stale\n", encoding="utf-8")
    stale_lock.write_text("", encoding="utf-8")

    result = _run_reconcile(tmp_path, APPLE_LISTING, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert _stage_candidate_debris(tmp_path) == []


@pytest.mark.parametrize(
    ("listing", "graph_env", "channels", "cap"),
    [
        # Drive-what-we-use: the emitted width is the graph's ACTUAL driven
        # width, never the DAC's cap. The explicit ACTIVE_LANE marker fences
        # off outputd's stereo-only TTS mixer so full-range audio cannot reach
        # a bare tweeter.
        pytest.param(INNOMAKER_LISTING, _active_graph_env, 2, 2, id="innomaker-2-of-2"),
        pytest.param(APPLE_LISTING, _apple_active_graph_env, 2, 2, id="apple-2-of-2"),
        pytest.param(DAC8X_AND_APPLE_LISTING, _active_graph_env, 2, 8,
                     id="dac8x-2-of-8"),
        pytest.param(DAC8X_AND_APPLE_LISTING, _active_graph_env, 6, 8,
                     id="dac8x-6-of-8"),
    ],
)
def test_reconcile_arms_the_active_lane_at_the_graphs_own_width(
    tmp_path: Path, listing: str, graph_env, channels: int, cap: int
):
    extra_env = (
        graph_env(tmp_path)
        if graph_env is _apple_active_graph_env
        else graph_env(tmp_path, channels=channels)
    )
    result = _run_reconcile(
        tmp_path, listing, "--reason", "test", extra_env=extra_env
    )

    assert result.returncode == 0, result.stderr
    outputd_env = _outputd_env(tmp_path)
    assert "JASPER_OUTPUTD_BACKEND=alsa" in outputd_env
    assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env
    assert "JASPER_OUTPUTD_DAC_PCM=outputd_dac" in outputd_env
    assert "JASPER_OUTPUTD_DUAL_DAC_A_PCM=''" in outputd_env
    # A ROLEFUL box reaches outputd over the ACTIVE RING, which outputd reads
    # as a FILE — it opens no content PCM at all, and ADR-0100 retired the key
    # with the lane.
    assert "JASPER_OUTPUTD_CONTENT_PCM" not in outputd_env
    assert "outputd_active_content_capture" not in outputd_env
    assert f"JASPER_OUTPUTD_ACTIVE_CHANNELS={channels}" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_LANE=1" in outputd_env
    # Arming the lane does not move the registry-declared final-edge format.
    expected_format = "S24_3LE" if listing is APPLE_LISTING else "S32_LE"
    assert f"JASPER_OUTPUTD_DAC_FORMAT={expected_format}" in outputd_env
    assert (
        f"mode=single_alsa_active active_channels={channels} active_lane_cap={cap}"
        in result.stderr
    )


@pytest.mark.parametrize(
    "graph_kind",
    [
        pytest.param("single", id="single-camilla-graph"),
        pytest.param("active-leader", id="program-bake-plus-crossover-endpoint"),
    ],
)
def test_reconcile_dac8x_width_two_graph_arms_the_active_ring(
    tmp_path: Path, graph_kind: str
):
    """Both graph layouts drive two outputs, over the same active ring."""
    args: tuple[str, ...]
    if graph_kind == "active-leader":
        args = ("--reason", "outputd-failure", "--no-restart")
        graph_env = _active_leader_graph_env(tmp_path, channels=2)
    else:
        args = ("--reason", "test")
        graph_env = _active_graph_env(tmp_path, channels=2)
    result = _run_reconcile(
        tmp_path, DAC8X_AND_APPLE_LISTING, *args, extra_env=graph_env
    )

    assert result.returncode == 0, result.stderr
    outputd_env = _outputd_env(tmp_path)
    assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env
    assert "outputd_active_content_capture" not in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=2" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_LANE=1" in outputd_env
    assert "JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT=1" in outputd_env
    assert (
        "mode=single_alsa_active active_channels=2 active_lane_cap=8 "
        "active_endpoint=jts_ring_active_playback" in result.stderr
    )


@pytest.mark.parametrize(
    ("listing", "channels", "write_crossover_statefile", "reason"),
    [
        pytest.param(
            DAC8X_AND_APPLE_LISTING, 2, False,
            "program_bake_pipe_without_active_crossover:camilla2_statefile_missing",
            id="crossover-endpoint-missing",
        ),
        pytest.param(
            APPLE_LISTING, 6, True,
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
    outputd_env = _outputd_env(tmp_path)
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=''" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_LANE=''" in outputd_env
    assert "single_alsa_active" not in result.stderr
    assert f"active_graph={reason}" in result.stderr


def test_reconcile_dac8x_active_graph_over_cap_stays_stereo(tmp_path: Path):
    """16 outputs on an 8-output DAC8x is impossible hardware, so it fails
    closed to ordinary stereo rather than emitting a topology the DAC cannot
    physically carry."""
    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--reason",
        "test",
        extra_env=_active_graph_env(tmp_path, channels=16),
    )

    assert result.returncode == 0, result.stderr
    outputd_env = _outputd_env(tmp_path)
    assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=''" in outputd_env
    assert "single_alsa_active" not in result.stderr
    assert (
        "active_graph=active_graph_unsafe:active_graph_output_count_mismatch"
        in result.stderr
    )


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
    outputd_env = _outputd_env(tmp_path)
    assert "outputd_active_content_capture" not in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=2" in outputd_env
    template = _template(tmp_path)
    assert "pcm.outputd_dac {\n    type hw\n    card sndrpihifiberry\n" in template
    _assert_omits(template, "type route", "0.4 0.5")
    _assert_no_empty_alsa_card(template)
    _assert_omits(result.stderr, "output_dac_route", "route_ignored")
    assert "outputd_active_mode=1 outputd_active_channels=2" in result.stderr


# --- the dual-Apple composite -------------------------------------------------


def _assert_publications_agree(tmp_path: Path) -> None:
    """After one reconcile pass, JASPER_AUDIO_DAC_ID names what the record's
    ``active_profile_id`` names — the one contract between the two."""
    from jasper.env_load import parse_env_file
    from jasper.output_hardware import active_dac_profile_id, published_dac_id

    env = parse_env_file(str(tmp_path / "jasper.env"))
    recorded = active_dac_profile_id(tmp_path / "output_hardware.json")
    assert published_dac_id(env) == (recorded or "unknown")


def test_reconcile_publishes_the_management_transport_verdict_as_a_marker(
    tmp_path: Path,
):
    """The gadget reads this field with `test -e` instead of an interpreter, so
    it has to CLEAR when the board stops being a peripheral."""
    marker = tmp_path / "management-transport.ok"

    peripheral = _run_reconcile(tmp_path, INNOMAKER_LISTING, "--reason", "test")
    assert peripheral.returncode == 0, peripheral.stderr
    assert marker.exists()
    assert _output_hardware_record(tmp_path)["usb_data_role"][
        "management_transport_available"
    ] is True

    host = _run_reconcile(
        tmp_path, INNOMAKER_LISTING, "--reason", "test", active_usb_role="host",
    )
    assert host.returncode == 0, host.stderr
    assert not marker.exists()


@pytest.mark.parametrize(
    ("starting_content", "active_usb_role"),
    [
        # A verdict that would REMOVE the marker if --print-env mutated.
        pytest.param("sentinel\n", "host", id="marker-present"),
        # A verdict that would CREATE the marker if --print-env mutated.
        pytest.param(None, "peripheral", id="marker-absent"),
    ],
)
def test_print_env_leaves_the_management_transport_marker_untouched(
    tmp_path: Path, starting_content: str | None, active_usb_role: str,
):
    """--print-env's usage text promises no mutations -- this pin does not
    expire. (Today's motivation is install.sh's mid-install probe of the
    PREVIOUS build, #4123, which would flip the gadget's management-transport
    gate off a stale verdict; that motivation lapses if --print-env ever
    moves after the source sync, but the no-mutations contract stays.)"""
    marker = tmp_path / "management-transport.ok"
    if starting_content is not None:
        marker.write_text(starting_content, encoding="utf-8")

    result = _run_reconcile(
        tmp_path, INNOMAKER_LISTING, "--print-env", active_usb_role=active_usb_role,
    )

    assert result.returncode == 0, result.stderr
    if starting_content is None:
        assert not marker.exists()
    else:
        assert marker.read_text(encoding="utf-8") == starting_content


def test_reconcile_dual_apple_records_profile_and_parks_until_dual_sink(
    tmp_path: Path,
):
    result = _run_reconcile(
        tmp_path,
        DUAL_APPLE_LISTING,
        "--reason",
        "test",
        extra_env=_dual_apple_cards(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    env_text = _jasper_env(tmp_path)
    assert "JASPER_AUDIO_DAC_ID=dual_apple_usb_c_dac_4ch" in env_text
    assert "JASPER_AUDIO_DAC_CARD=''" in env_text
    assert "JASPER_OUTPUTD_SINK=single_alsa" in _outputd_env(tmp_path)
    assert not (tmp_path / "tts.env").exists()
    record = _output_hardware_record(tmp_path)
    assert record["profile_id"] == "dual_apple_usb_c_dac_4ch"
    assert record["apple_dac_count"] == 2
    assert record["usb_data_role"]["desired_role"] == "peripheral"
    assert record["usb_data_role"]["gadget_available"] is True
    _assert_parked_outputd_dac_template(_template(tmp_path))
    assert _render_log(tmp_path) == "render\n"
    commands = _systemctl_log(tmp_path)
    assert "enable jasper-dac-init.service" in commands
    assert "enable jasper-headphone-monitor.service" in commands
    assert "--no-block stop jasper-voice.service jasper-outputd.service" in commands
    assert "event=audio_hardware_reconcile.dual_apple_detected" in result.stderr
    assert (
        "event=hardware.usb_role_resolved topology=separate_host_ports "
        "desired=peripheral active=peripheral gadget_available=true "
        "management_transport_available=true reason=available"
    ) in result.stderr
    _assert_publications_agree(tmp_path)
    # --print-env's DONGLE_CARD truncates OBSERVED_OUTPUT_APPLE_CARD_IDS to
    # its first id ("A", not "A_1") on this same dual-Apple pair.
    print_env_dir = tmp_path / "print_env"
    print_env_dir.mkdir()
    print_env_result = _run_reconcile(
        print_env_dir,
        DUAL_APPLE_LISTING,
        "--print-env",
        extra_env=_dual_apple_cards(print_env_dir),
    )
    assert "DONGLE_CARD=A" in print_env_result.stdout


def test_reconcile_dual_apple_pins_pcm_order_from_saved_topology(tmp_path: Path):
    topology_path = _dual_apple_topology(tmp_path, active=True)

    result = _run_reconcile(
        tmp_path,
        DUAL_APPLE_LISTING,
        "--reason",
        "test",
        extra_env={
            **_dual_apple_cards(tmp_path, _DUAL_APPLE_CARDS_SWAPPED),
            "JASPER_OUTPUT_TOPOLOGY_PATH": str(topology_path),
            **_active_graph_env(tmp_path, write_topology=False),
        },
    )

    assert result.returncode == 0, result.stderr
    assert "JASPER_AUDIO_DAC_ID=dual_apple_usb_c_dac_4ch" in _jasper_env(tmp_path)
    outputd_env = _outputd_env(tmp_path)
    assert "JASPER_OUTPUTD_SINK=dual_apple" in outputd_env
    # The armed composite names ITSELF on the DAC_PCM key — outputd reads it
    # back as the composite's label, not as a PCM to open.
    assert "JASPER_OUTPUTD_DAC_PCM=dual_apple_usb_c_dac_4ch" in outputd_env
    assert "JASPER_OUTPUTD_DUAL_DAC_A_PCM=hw:CARD=A,DEV=0" in outputd_env
    assert "JASPER_OUTPUTD_DUAL_DAC_B_PCM=hw:CARD=B,DEV=0" in outputd_env
    # The COMPOSITE's own declaration reaches outputd, not its children's.
    # Both children are Apple dongles declaring the packed S24_3LE edge, and
    # outputd's paired composite sink has NO packed-24 child write path:
    # ChildPeriods::new refuses that width and PairedCompositeSink::new parks
    # the unit at EX_CONFIG 78 before either dongle opens. So an S24_3LE
    # emitted here is a silent speaker on every dual-Apple box. This is the
    # tripwire for the single-vs-composite split: it fails the moment the
    # emission starts resolving through child_profile_ids.
    assert "JASPER_OUTPUTD_DAC_FORMAT=S16_LE" in outputd_env
    assert "JASPER_OUTPUTD_DAC_FORMAT=S24_3LE" not in outputd_env
    # A wide composite sink (4ch) is already fenced off outputd's stereo-only
    # features by its channel width, so the 2-ch WIDTH knob stays cleared.
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=''" in outputd_env
    # The lane PAIR is staged, because the accepted graph names the ACTIVE
    # RING. The two markers are one fact and outputd bails at startup on an
    # incoherent pair, so both are asserted.
    assert "JASPER_OUTPUTD_ACTIVE_LANE=1" in outputd_env
    assert "JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT=1" in outputd_env
    _assert_parked_outputd_dac_template(_template(tmp_path))
    assert "order_source=saved_topology" in result.stderr


def test_reconcile_parks_a_declared_composite_missing_one_child(tmp_path: Path):
    """A saved composite with one dongle gone parks instead of taking over.

    Otherwise the survivor classifies as an ordinary apple_usb_c_dongle,
    is marked recognized, and the final output is rewired onto it as a
    plain stereo DAC — while the graph layer, which reads only the saved
    topology, never follows. The box stays quiet by nobody's decision.
    """
    extra_env = {
        **_dual_apple_cards(tmp_path, ((1, "A", "left"),)),
        "JASPER_OUTPUT_TOPOLOGY_PATH": str(
            _dual_apple_topology(tmp_path, active=True)
        ),
        **_active_graph_env(tmp_path, write_topology=False),
    }

    result = _run_reconcile(
        tmp_path, APPLE_LISTING, "--reason", "test", extra_env=extra_env
    )

    assert result.returncode == 0, result.stderr
    record = _output_hardware_record(tmp_path)
    assert record["status"] == "partial"
    blockers = [i for i in record["issues"] if i["severity"] == "blocker"]
    assert [i["code"] for i in blockers] == ["saved_composite_partially_present"]
    # The household-visible reason names the child that is gone.
    assert "right" in blockers[0]["message"]
    # The degraded state this issue is about: NOT recognized as a plain dongle.
    assert "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle" not in _jasper_env(tmp_path)
    outputd_env = _outputd_env(tmp_path)
    # The parked markers, not a live edge onto the surviving dongle.
    assert "JASPER_OUTPUTD_BACKEND=fake" in outputd_env
    assert "JASPER_OUTPUTD_BACKEND=alsa" not in outputd_env
    assert "JASPER_OUTPUTD_DAC_FORMAT=''" in outputd_env
    _assert_parked_outputd_dac_template(_template(tmp_path))
    commands = _systemctl_log(tmp_path)
    assert "--no-block stop jasper-voice.service jasper-outputd.service" in commands
    assert "--no-block restart jasper-outputd.service" not in commands
    assert "event=audio_hardware_reconcile.runtime_env pass_reason=test mode=parked" in (
        result.stderr
    )
    # The reason reaches the JOURNAL, not just the record: an operator reading
    # `output_parked` sees WHY, not only `recognized=0`.
    assert (
        "event=audio_hardware_reconcile.output_parked pass_reason=test "
        "output_dac_id=unknown output_dac_card=A recognized=0 "
        "observed_blockers=saved_composite_partially_present"
    ) in result.stderr
    _assert_publications_agree(tmp_path)

    # Recovery is the udev chain re-running this script — no operator step.
    commands_before = len(_systemctl_log(tmp_path))
    _fake_sys_output_card(
        tmp_path, card_index=2, card_id="B", usb_path="1-2", serial="right"
    )
    recovered = _run_reconcile(
        tmp_path, DUAL_APPLE_LISTING, "--reason", "test", extra_env=extra_env
    )

    assert recovered.returncode == 0, recovered.stderr
    record = _output_hardware_record(tmp_path)
    assert record["status"] == "ready"
    assert record["profile_id"] == "dual_apple_usb_c_dac_4ch"
    assert record["issues"] == []
    assert "JASPER_AUDIO_DAC_ID=dual_apple_usb_c_dac_4ch" in _jasper_env(tmp_path)
    outputd_env = _outputd_env(tmp_path)
    assert "JASPER_OUTPUTD_BACKEND=alsa" in outputd_env
    assert "JASPER_OUTPUTD_SINK=dual_apple" in outputd_env
    assert "JASPER_OUTPUTD_DUAL_DAC_A_PCM=hw:CARD=A,DEV=0" in outputd_env
    assert "JASPER_OUTPUTD_DUAL_DAC_B_PCM=hw:CARD=B,DEV=0" in outputd_env
    commands = _systemctl_log(tmp_path)[commands_before:]
    assert "--no-block restart jasper-outputd.service" in commands
    assert (
        "--no-block stop jasper-voice.service jasper-outputd.service" not in commands
    )


def test_reconcile_saved_single_topology_still_takes_the_single_dongle(
    tmp_path: Path,
):
    """A saved SINGLE topology keeps today's behaviour: stereo is legal.

    The passive **composite** case — `kind == "composite"` with no
    per-driver DSP, where the park must also stand down — is a
    record-level decision pinned in
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
    env_text = _jasper_env(tmp_path)
    assert "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle" in env_text
    assert "JASPER_AUDIO_DAC_CARD=A" in env_text
    commands = _systemctl_log(tmp_path)
    assert "enable jasper-dac-init.service" in commands
    assert "enable jasper-headphone-monitor.service" in commands
    assert (
        "--no-block stop jasper-voice.service jasper-outputd.service" not in commands
    )


def test_reconcile_dual_apple_defers_runtime_until_active_graph_is_loaded(
    tmp_path: Path,
):
    result = _run_reconcile(
        tmp_path,
        DUAL_APPLE_LISTING,
        "--reason",
        "test",
        extra_env={
            **_dual_apple_cards(tmp_path, _DUAL_APPLE_CARDS_SWAPPED),
            "JASPER_OUTPUT_TOPOLOGY_PATH": str(_dual_apple_topology(tmp_path)),
        },
    )

    assert result.returncode == 0, result.stderr
    env_text = _jasper_env(tmp_path)
    assert "JASPER_AUDIO_DAC_ID=dual_apple_usb_c_dac_4ch" in env_text
    assert "JASPER_AUDIO_DAC_CARD=''" in env_text
    outputd_env = _outputd_env(tmp_path)
    assert "JASPER_OUTPUTD_BACKEND=fake" in outputd_env
    assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env
    assert "JASPER_OUTPUTD_CONTENT_PCM" not in outputd_env
    assert "JASPER_OUTPUTD_DUAL_DAC_A_PCM=''" in outputd_env
    # Parked/unrecognized: no profile to query, so the declared format clears
    # too — explicit empty, matching how ACTIVE_CHANNELS/ACTIVE_LANE clear in
    # this same branch. (A LOST probe is a different branch; see the
    # dac_format_skip tests.)
    assert "JASPER_OUTPUTD_DAC_FORMAT=''" in outputd_env
    assert (
        _output_hardware_record(tmp_path)["profile_id"] == "dual_apple_usb_c_dac_4ch"
    )
    assert "action=park_until_active_graph" in result.stderr
    assert "reason=camilla_statefile_missing" in result.stderr
    _assert_parked_outputd_dac_template(_template(tmp_path))
    assert _render_log(tmp_path) == "render\n"
    assert (
        "--no-block stop jasper-voice.service jasper-outputd.service"
        in _systemctl_log(tmp_path)
    )


def test_dual_apple_park_names_a_silent_active_graph_probe(tmp_path: Path):
    """`active_graph_status` prints a reason on every path it declines on, so
    an empty capture is not an unknown reason — it is the probe producing no
    output at all. The park line has to say which of the two it saw."""
    result = _run_reconcile(
        tmp_path,
        DUAL_APPLE_LISTING,
        "--reason",
        "test",
        extra_env={
            **_dual_apple_cards(tmp_path),
            "JASPER_OUTPUT_TOPOLOGY_PATH": str(_dual_apple_topology(tmp_path)),
            # The active-graph gate is the one call carrying this variable.
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


@pytest.mark.parametrize(
    "listing",
    [APPLE_LISTING, DUAL_APPLE_LISTING, INNOMAKER_LISTING, DAC8X_STUDIO_LISTING, ""],
)
def test_env_publication_names_the_dac_the_record_names(tmp_path: Path, listing: str):
    """One reconcile pass, two publications, one answer.

    JASPER_AUDIO_DAC_ID exists for consumers that can only read env. A
    reader that took it instead of the record could only answer
    differently if the two could differ — so they may not.
    """
    result = _run_reconcile(tmp_path, listing, "--reason", "test")

    assert result.returncode == 0, result.stderr
    _assert_publications_agree(tmp_path)


def test_env_publication_agrees_on_a_classify_time_partial_dual_apple_record(
    tmp_path: Path,
):
    """The composite counts as ACTIVE as soon as it is named, parked or not —
    unlike a single DAC. Pinned for a pair the CLASSIFIER marks ``partial``
    (one child's USB endpoint is not synchronous), a different park from the
    bash active-graph gate the other dual tests cover."""
    extra_env = _dual_apple_cards(tmp_path)
    (tmp_path / "proc" / "asound" / "card2" / "stream0").write_text(
        "Playback:\n  Endpoint: 0x01 (ASYNC)\n", encoding="utf-8"
    )

    result = _run_reconcile(
        tmp_path, DUAL_APPLE_LISTING, "--reason", "test", extra_env=extra_env
    )

    assert result.returncode == 0, result.stderr
    record = _output_hardware_record(tmp_path)
    assert record["status"] == "partial"
    assert [
        issue["code"] for issue in record["issues"] if issue["severity"] == "blocker"
    ] == ["dual_apple_endpoint_not_synchronous"]
    _assert_publications_agree(tmp_path)


# --- the preserve_runtime_env fallback ---------------------------------------
#
# The endpoint-contract step resolves outputd's capture half by shelling out to
# `jasper.cli.audio_config outputd-capture-device`. When that step fails the
# reconciler exits 66 before writing any outputd env, leaving outputd running
# whatever the file already said. If that was the REAL ALSA backend at
# `outputd_dac` while a composite had parked that alias to `type null`, the
# result is an output loop with no clock on either side: SIGKILL per burst and
# StartLimitAction=reboot.
#
# The shims below reproduce one failing step and nothing else: every other
# Python call in the run still reaches the real interpreter.

_CLOCKLESS_PRESERVED_ENV = (
    "JASPER_OUTPUTD_BACKEND=alsa\n"
    "JASPER_OUTPUTD_SINK=single_alsa\n"
    "JASPER_OUTPUTD_DAC_PCM=outputd_dac\n"
    "JASPER_OUTPUTD_CONTENT_PCM=outputd_content_capture\n"
)

# The ALSA artifact a PREVIOUS pass left on disk. The guard reads this rather
# than re-deriving what the current pass would render, because the
# endpoint-contract exit is ~87 lines ahead of render_asound_if_needed and this
# pass renders nothing — so these two templates are the only evidence about
# what outputd will actually open.
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


def _assert_contract_really_failed(result: subprocess.CompletedProcess[str]) -> None:
    """Positive control: the injected failure reached the path under test.

    Without this an assertion about the fallback could pass on a run that
    never took the fallback at all.
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
    tmp_path: Path, preserved_env: str | None, expected_env: str | None
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
    assert "event=audio_hardware_reconcile.outputd_env_clockless_park" in result.stderr
    rendered_env = _outputd_env(tmp_path)
    if expected_env is None:
        assert "JASPER_OUTPUTD_BACKEND=fake" in rendered_env
    else:
        # Exactly one key moves; every other preserved key is already coherent.
        assert rendered_env == expected_env


def test_contract_failure_preserves_when_the_artifact_still_names_real_hardware(
    tmp_path: Path,
):
    """Two passes: the guard must read the artifact, not re-derive one.

    Pass 1 renders `type hw card A`; pass 2 sees no recognized DAC and
    fails the contract, so nothing re-renders and the alias outputd opens
    is what pass 1 left. A guard asking what THIS pass would render
    answers null and parks a box whose DAC is still live.
    """
    first = _run_reconcile(tmp_path, APPLE_LISTING, "--reason", "test")
    assert first.returncode == 0, first.stderr
    template = _template(tmp_path)
    assert "type hw" in template and "card A" in template
    outputd_env_after_first = _outputd_env(tmp_path)
    assert "JASPER_OUTPUTD_BACKEND=alsa" in outputd_env_after_first

    second = _run_reconcile(
        tmp_path, "", "--reason", "test", extra_env=_endpoint_contract_fails(tmp_path)
    )

    _assert_contract_really_failed(second)
    assert "action=preserve_runtime_env" in second.stderr
    assert "outputd_env_clockless_park" not in second.stderr
    # The artifact is untouched and still real, and the env is byte-unchanged.
    assert _template(tmp_path) == template
    assert _outputd_env(tmp_path) == outputd_env_after_first


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
            DUAL_APPLE_LISTING, _PARKED_ASOUND_TEMPLATE, None,
            _CLOCKLESS_PRESERVED_ENV.replace(
                "JASPER_OUTPUTD_BACKEND=alsa", "JASPER_OUTPUTD_BACKEND="
            ),
            True, False, None, id="stated-empty-backend",
        ),
        pytest.param(
            APPLE_LISTING, _PARKED_ASOUND_TEMPLATE, None, _CLOCKLESS_PRESERVED_ENV,
            False, True, "event=audio_hardware_reconcile.state_written_failed",
            id="hardware-observation-failed",
        ),
        pytest.param(
            APPLE_LISTING, _LIVE_ASOUND_TEMPLATE, None, _CLOCKLESS_PRESERVED_ENV,
            False, False, None, id="alias-still-names-real-hardware",
        ),
        pytest.param(
            DUAL_APPLE_LISTING, _PARKED_ASOUND_TEMPLATE, None,
            _CLOCKLESS_PRESERVED_ENV.replace(
                "JASPER_OUTPUTD_BACKEND=alsa", "JASPER_OUTPUTD_BACKEND=fake"
            ),
            True, False, None, id="backend-already-parked",
        ),
        pytest.param(
            DUAL_APPLE_LISTING, _PARKED_ASOUND_TEMPLATE,
            "JASPER_OUTPUTD_SINK=dual_apple\n",
            "JASPER_OUTPUTD_BACKEND=alsa\n"
            "JASPER_OUTPUTD_DAC_PCM=outputd_dac\n"
            "JASPER_OUTPUTD_DUAL_DAC_A_PCM=hw:CARD=A,DEV=0\n"
            "JASPER_OUTPUTD_DUAL_DAC_B_PCM=hw:CARD=A_1,DEV=0\n",
            True, False, None, id="composite-sink-does-not-open-the-alias",
        ),
        pytest.param(
            DUAL_APPLE_LISTING, _PARKED_ASOUND_TEMPLATE,
            "JASPER_OUTPUTD_DAC_PCM=hw:CARD=A,DEV=0\n",
            "JASPER_OUTPUTD_BACKEND=alsa\nJASPER_OUTPUTD_SINK=single_alsa\n",
            True, False, None, id="overridden-dac-pcm-is-not-the-alias",
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
                '    if [[ "$arg" == "outputd-capture-device" '
                '|| "$arg" == "jasper.cli.output_hardware" ]]; then\n'
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
    assert _outputd_env(tmp_path) == preserved_env


# --- restart gating: which units a given delta may bounce --------------------


def test_reconcile_recognized_arrival_starts_outputd_when_values_unchanged(
    tmp_path: Path,
):
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_env=APPLE_ENV,
        initial_outputd_env=_apple_steady_outputd_env(),
        initial_template=APPLE_RENDERED_TEMPLATE,
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


def test_reconcile_recognized_role_restarts_outputd_after_unknown_state(
    tmp_path: Path,
):
    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--reason",
        "test",
        initial_env="JASPER_AUDIO_DAC_ID=A\nJASPER_AUDIO_DAC_CARD=A\n",
        initial_template=APPLE_RENDERED_TEMPLATE.replace("card A", "card sndrpihifiberry"),
    )

    assert result.returncode == 0, result.stderr
    assert _render_log(tmp_path) == ""
    env_text = _jasper_env(tmp_path)
    assert "JASPER_AUDIO_DAC_ID=hifiberry_dac8x" in env_text
    assert "JASPER_AUDIO_DAC_CARD=sndrpihifiberry" in env_text
    commands = _systemctl_log(tmp_path)
    assert "stop jasper-voice.service" in commands
    assert "reset-failed jasper-outputd.service" in commands
    assert "--no-block restart jasper-outputd.service" in commands
    assert "--no-block restart jasper-aec-reconcile.service" in commands


@pytest.mark.parametrize(
    ("initial_outputd_env", "moved_key"),
    [
        # The backend moves fake -> alsa.
        pytest.param("JASPER_OUTPUTD_BACKEND=fake\n", "JASPER_OUTPUTD_BACKEND=alsa",
                     id="outputd-backend"),
        # Converged EXCEPT the DAC-buffer floor, so its re-emit is the sole
        # delta and this is a genuinely floor-only pass.
        pytest.param(
            _apple_steady_outputd_env(drop=("JASPER_OUTPUTD_DAC_BUFFER_FRAMES",)),
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES=256",
            id="latency-floor-only",
        ),
    ],
)
def test_reconcile_outputd_only_delta_restarts_outputd_alone(
    tmp_path: Path, initial_outputd_env: str, moved_key: str
):
    """DAC identity and the rendered asound are unchanged, so this class of
    delta cannot shift the mic/input profile: bounce jasper-outputd ALONE.
    Stopping jasper-voice would deafen wake for ~10-15 s, and re-running
    jasper-aec-reconcile would re-derive an input profile nothing moved."""
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_env=APPLE_ENV,
        initial_outputd_env=initial_outputd_env,
        initial_template=APPLE_RENDERED_TEMPLATE,
    )

    assert result.returncode == 0, result.stderr
    assert _render_log(tmp_path) == ""
    assert moved_key in _outputd_env(tmp_path)
    commands = _systemctl_log(tmp_path)
    assert "--no-block restart jasper-outputd.service" in commands
    assert "event=audio_hardware_reconcile.outputd_only_restarted" in result.stderr
    assert "stop jasper-voice.service" not in commands
    assert "restart jasper-aec-reconcile.service" not in commands


@pytest.mark.parametrize(
    ("marker", "brain"),
    [
        pytest.param(None, True, id="absent"),
        pytest.param("full\n", True, id="full"),
        pytest.param("streambox\n", False, id="streambox"),
        pytest.param("", False, id="empty"),
        pytest.param("invalid\n", False, id="invalid"),
        pytest.param(
            "<unreadable>",
            False,
            id="unreadable",
            marks=pytest.mark.skipif(
                os.geteuid() == 0, reason="root bypasses the mode bits this asserts"
            ),
        ),
    ],
)
def test_dac_change_brain_restart_gate_follows_profile_marker(
    tmp_path: Path, marker: str | None, brain: bool
):
    profile = tmp_path / "install_profile"
    if marker == "<unreadable>":
        profile.write_text("full\n", encoding="utf-8")
        profile.chmod(0)
        assert profile.is_file() and not os.access(profile, os.R_OK)
    elif marker is not None:
        profile.write_text(marker, encoding="utf-8")

    result = _run_reconcile(
        tmp_path,
        INNOMAKER_LISTING,
        initial_env="JASPER_AUDIO_DAC_ID=A\nJASPER_AUDIO_DAC_CARD=A\n",
    )
    if marker == "<unreadable>":
        profile.chmod(0o600)

    commands = _systemctl_log(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "--no-block restart jasper-outputd.service" in commands
    assert ("stop jasper-voice.service" in commands) is brain
    assert ("restart jasper-aec-reconcile.service" in commands) is brain
    assert f"brain_restarted={int(brain)}" in result.stderr


def test_reconcile_dac_change_with_floor_delta_takes_full_path(tmp_path: Path):
    """Fail-safe direction: a DAC-identity transition coincident with a floor
    delta takes the FULL path, because a real DAC change can move the
    mic/input profile. The outputd-only shortcut requires BOTH
    dac_env_changed==0 AND render_changed==0."""
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        # Stored DAC id differs from the detected dongle -> dac_env_changed;
        # asound is pre-rendered for card A so render_changed stays 0; the
        # dongle's declared floor lands in an empty outputd.env, which is the
        # coincident floor delta.
        initial_env=(
            "JASPER_AUDIO_DAC_ID=A\n"
            "JASPER_AUDIO_DAC_CARD=A\n"
            "JASPER_AUDIO_ROUTE_PROFILE=usb_low_latency_48k\n"
        ),
        initial_template=APPLE_RENDERED_TEMPLATE,
    )

    assert result.returncode == 0, result.stderr
    assert _render_log(tmp_path) == ""
    assert "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle" in _jasper_env(tmp_path)
    # The floor delta really was coincident with the DAC change.
    outputd_env = _outputd_env(tmp_path)
    assert "JASPER_CAMILLA_TARGET_LEVEL=1536" in outputd_env
    assert "JASPER_OUTPUTD_PERIOD_FRAMES=128" in outputd_env
    commands = _systemctl_log(tmp_path)
    assert "stop jasper-voice.service" in commands
    assert "--no-block restart jasper-aec-reconcile.service" in commands
    assert "event=audio_hardware_reconcile.audio_restarted" in result.stderr
    assert "event=audio_hardware_reconcile.outputd_only_restarted" not in result.stderr


def test_reconcile_route_only_change_restarts_fanin_not_voice(tmp_path: Path):
    """A converged Apple steady state where the ONLY moving dimension is the
    route/fan-in env: restart fan-in via the route runtime path, leave
    jasper-voice up, and do not RESTART outputd (start-if-recognized only)."""
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_env=APPLE_ENV + "JASPER_AUDIO_ROUTE_PROFILE=usb_low_latency_48k\n",
        initial_outputd_env=_apple_steady_outputd_env(
            extra="JASPER_OUTPUTD_CONTENT_BRIDGE=direct\n"
        ),
        # A STALE warmup cushion, so the reconcile rewrites the route env
        # while nothing else moves.
        initial_fanin_env="JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES=512\n",
        initial_template=APPLE_RENDERED_TEMPLATE,
    )

    assert result.returncode == 0, result.stderr
    assert _render_log(tmp_path) == ""
    fanin_env = (tmp_path / "fanin.env").read_text(encoding="utf-8")
    assert "JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES=1536" in fanin_env
    commands = _systemctl_log(tmp_path)
    assert "restart jasper-fanin.service" in commands
    assert "event=audio_hardware_reconcile.route_runtime_restarted" in result.stderr
    assert "fanin_restarted=1" in result.stderr
    assert "stop jasper-voice.service" not in commands
    assert "restart jasper-aec-reconcile.service" not in commands
    assert "--no-block restart jasper-outputd.service" not in commands
    # The recognized-but-nothing-committed arm still ensures outputd is up.
    assert "--no-block start jasper-outputd.service" in commands
    assert "event=audio_hardware_reconcile.outputd_only_restarted" not in result.stderr


def test_route_env_change_restarts_fanin_exactly_once(tmp_path: Path):
    """The route profile's five fan-in keys are written, fan-in bounces once,
    and a semantically identical second pass bounces nothing (canonical-form
    change detection: nothing moved -> nothing restarts)."""
    route_env = "JASPER_AUDIO_ROUTE_PROFILE=usb_low_latency_48k\n"

    first = _run_reconcile(
        tmp_path, APPLE_LISTING, "--reason", "test", initial_env=route_env
    )
    assert first.returncode == 0, first.stderr
    fanin_env = (tmp_path / "fanin.env").read_text(encoding="utf-8")
    assert "JASPER_FANIN_INPUT_RESAMPLER=enabled" in fanin_env
    assert "JASPER_FANIN_INPUT_RESAMPLER_LANE=usbsink" in fanin_env
    assert "JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES=512" in fanin_env
    assert "JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES=1536" in fanin_env
    assert "JASPER_FANIN_INPUT_RESAMPLER_RING_FRAMES=4096" in fanin_env
    commands = _systemctl_log(tmp_path)
    assert "restart jasper-fanin.service" in commands
    assert "try-restart jasper-usbsink.service" not in commands
    assert "fanin_restarted=1" in first.stderr

    (tmp_path / "systemctl.log").write_text("", encoding="utf-8")
    second = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_env=route_env,
        initial_fanin_env=fanin_env,
    )

    assert second.returncode == 0, second.stderr
    assert "restart jasper-fanin.service" not in _systemctl_log(tmp_path)
    assert "fanin_restarted=0" in second.stderr


# --- the asound render is never allowed to clobber live ALSA ------------------


def _stub_render_lib(tmp_path: Path, body: str) -> Path:
    """A drop-in jasper-asound-render.sh with an overridable template
    renderer. Sources the real lib first (keeping jasper_asound_log_token
    et al. intact), so a test can drive the production failure shape: a
    card-less recognized DAC fails closed (require_output_dac_card -> 64)
    BEFORE the renderer opens the dest.
    """
    stub = tmp_path / "stub-asound-render.sh"
    real = ROOT / "deploy" / "lib" / "jasper-asound-render.sh"
    stub.write_text(
        f"#!/usr/bin/env bash\nsource {real}\n"
        "jasper_asound_render_template() {\n"
        f"{body}\n"
        "}\n",
        encoding="utf-8",
    )
    return stub


def _copy_script_with_sibling_render_lib(tmp_path: Path, sentinel: str) -> Path:
    """A `bin/` + `lib/` layout mirroring the installed tree, with a sibling
    render lib recognizable by `sentinel` -- distinct from the real one, so a
    test can tell whether `load_asound_render_lib`'s own sibling-vs-installed
    resolution found it, rather than `JASPER_ASOUND_RENDER_LIB` shortcutting
    the decision (every other test in this file sets that override)."""
    bin_dir = tmp_path / "layout" / "bin"
    lib_dir = tmp_path / "layout" / "lib"
    bin_dir.mkdir(parents=True)
    lib_dir.mkdir(parents=True)
    script_copy = bin_dir / "jasper-audio-hardware-reconcile"
    script_copy.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    script_copy.chmod(0o755)
    (lib_dir / "jasper-asound-render.sh").write_text(
        "#!/usr/bin/env bash\n"
        f"jasper_asound_log_token() {{ printf '%s' '{sentinel}'; }}\n",
        encoding="utf-8",
    )
    return script_copy


def test_print_env_dual_apple_ready_resolves_its_own_sibling_render_lib(
    tmp_path: Path,
):
    """Every other --print-env test pins JASPER_ASOUND_RENDER_LIB to the real
    lib, so load_asound_render_lib's own sibling-vs-installed resolution
    never runs. Unset it here and give the copied script only a sibling lib
    to find, with a jasper_asound_log_token distinct enough that its answer
    can only have come from that copy."""
    sentinel = "SIBLING-LIB-TOKEN"
    script_copy = _copy_script_with_sibling_render_lib(tmp_path, sentinel)
    topology_path = _dual_apple_topology(tmp_path, active=True)

    result = _run_reconcile(
        tmp_path,
        DUAL_APPLE_LISTING,
        "--print-env",
        script=script_copy,
        extra_env={
            **_dual_apple_cards(tmp_path),
            "JASPER_OUTPUT_TOPOLOGY_PATH": str(topology_path),
            **_active_graph_env(tmp_path, write_topology=False),
            "JASPER_ASOUND_RENDER_LIB": "",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "OUTPUT_DAC_ID=dual_apple_usb_c_dac_4ch" in result.stdout
    _assert_states(
        result.stderr,
        "event=audio_hardware_reconcile.dual_apple_detected ",
        "action=outputd_dual_sink",
        f"dac_a_pcm={sentinel} dac_b_pcm={sentinel}",
    )


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
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        reason,
        initial_template=good,
        extra_env={"JASPER_ASOUND_RENDER_LIB": str(_stub_render_lib(tmp_path, stub_body))},
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
    """Guards against an over-eager fix that makes render_asound_if_needed
    treat every render as a failure."""
    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--reason",
        "render-ok",
        initial_template="STALE PLACEHOLDER\n",
    )

    assert result.returncode == 0, result.stderr
    template = _template(tmp_path)
    _assert_states(template, "pcm.outputd_dac", "card sndrpihifiberry")
    _assert_no_empty_alsa_card(template)
    assert "event=audio_hardware_reconcile.asound_rendered" in result.stderr
    assert "event=audio_hardware_reconcile.asound_render_failed" not in result.stderr
    assert _render_log(tmp_path) == "render\n"
    # The live conf must carry THIS pass's template, not the one it replaced.
    _assert_states(
        (tmp_path / "asound.conf").read_text(encoding="utf-8"),
        "pcm.outputd_dac",
        "card sndrpihifiberry",
    )


def test_failed_asound_conf_render_fails_the_pass_without_restarting(tmp_path: Path):
    """A nonzero jasper-render-asound-conf may not pass as a rendered asound."""
    live_conf = tmp_path / "asound.conf"
    live_conf.write_bytes(b"GOOD LIVE ASOUND.CONF\n")
    good_template = "GOOD LIVE TEMPLATE\n"
    failing = tmp_path / "failing-render-asound-conf"
    failing.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'render\\n' >> \"$JASPER_RENDER_LOG\"\n"
        "exit 64\n",
        encoding="utf-8",
    )
    failing.chmod(0o755)

    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--reason",
        "render-conf-fail",
        initial_template=good_template,
        extra_env={"JASPER_RENDER_ASOUND_CONF": str(failing)},
    )

    assert result.returncode == 78, result.stderr
    assert _render_log(tmp_path) == "render\n"
    assert live_conf.read_bytes() == b"GOOD LIVE ASOUND.CONF\n"
    assert _template(tmp_path) == good_template
    _assert_omits(
        _systemctl_log(tmp_path),
        "restart jasper-outputd.service",
        "stop jasper-voice.service",
    )
    leftovers = list(tmp_path.glob("asoundrc.jasper.template.*"))
    assert leftovers == [], leftovers


# --- the per-DAC latency floor emit -------------------------------------------

_FLOOR_KEYS = (
    ("JASPER_CAMILLA_CHUNKSIZE", "256"),
    ("JASPER_CAMILLA_TARGET_LEVEL", "1536"),
    ("JASPER_OUTPUTD_PERIOD_FRAMES", "128"),
    ("JASPER_OUTPUTD_DAC_BUFFER_FRAMES", "256"),
)


@pytest.mark.parametrize(
    ("listing", "dac_id"),
    [
        pytest.param(APPLE_LISTING, "apple_usb_c_dongle", id="apple"),
        pytest.param(DAC8X_AND_APPLE_LISTING, "hifiberry_dac8x", id="dac8x"),
    ],
)
def test_reconcile_emits_the_declared_latency_floor(
    tmp_path: Path, listing: str, dac_id: str
):
    """The declared floor reaches the wizard-owned outputd.env verbatim,
    through the same bash plumbing for every profile, and the retired
    content-buffer key is never emitted."""
    result = _run_reconcile(tmp_path, listing, "--reason", "test")

    assert result.returncode == 0, result.stderr
    outputd_env = _outputd_env(tmp_path)
    for key, value in _FLOOR_KEYS:
        assert f"{key}={value}" in outputd_env, (key, outputd_env)
    assert not _outputd_env_key_present(
        outputd_env, "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES"
    )
    assert (
        f"event=audio_hardware_reconcile.latency_floor pass_reason=test "
        f"output_dac_id={dac_id} camilla_chunksize=256 "
        "camilla_target_level=1536 outputd_period_frames=128 "
        "outputd_dac_buffer_frames=256"
    ) in result.stderr


def test_reconcile_no_floor_drops_stale_floor_keys(tmp_path: Path):
    """A DAC with no declared floor DROPS a stale floor a prior DAC wrote —
    not left as `=''` (which would clobber an operator value) and not left at
    the stale numbers. DAC8x STUDIO is the floorless case; pointing this at a
    profile that later declares a floor would make the loop below unreachable
    rather than failing."""
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
    outputd_env = _outputd_env(tmp_path)
    for key, _value in _FLOOR_KEYS:
        assert not _outputd_env_key_present(outputd_env, key), key


@pytest.mark.parametrize(
    "initial_outputd_env",
    [
        pytest.param(None, id="not-previously-written"),
        # Defense in depth: even when a PRIOR reconcile already wrote the
        # floor into outputd.env, a later pass that sees the operator override
        # must REMOVE the outputd.env copy rather than leave it stale or `=''`.
        pytest.param("JASPER_OUTPUTD_DAC_BUFFER_FRAMES=512\n", id="pre-seeded"),
    ],
)
def test_reconcile_operator_env_override_survives_reconciler(
    tmp_path: Path, initial_outputd_env: str | None
):
    """jasper.env is loaded FIRST by the unit and outputd.env AFTER, so an
    empty `KEY=` in outputd.env would override the operator's value with empty
    and make Rust fall back to its default — silently discarding the tune. The
    key must be DROPPED from outputd.env entirely. Keys the operator did NOT
    set still get the profile floor."""
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_env=(
            "JASPER_CAMILLA_CHUNKSIZE=512\nJASPER_OUTPUTD_DAC_BUFFER_FRAMES=4096\n"
        ),
        initial_outputd_env=initial_outputd_env,
    )

    assert result.returncode == 0, result.stderr
    outputd_env = _outputd_env(tmp_path)
    assert not _outputd_env_key_present(outputd_env, "JASPER_CAMILLA_CHUNKSIZE")
    assert not _outputd_env_key_present(outputd_env, "JASPER_OUTPUTD_DAC_BUFFER_FRAMES")
    assert "JASPER_CAMILLA_TARGET_LEVEL=1536" in outputd_env
    assert "JASPER_OUTPUTD_PERIOD_FRAMES=128" in outputd_env


def _override_store(tmp_path: Path, **values: str) -> dict[str, str]:
    store = tmp_path / "audio_runtime_overrides.json"
    store.write_text(
        json.dumps({
            "kind": "jts_audio_runtime_overrides",
            "schema_version": 1,
            "overrides": {
                key: {"value": value, "reason": "test invalid staged outputd env"}
                for key, value in values.items()
            },
        }),
        encoding="utf-8",
    )
    return {"JASPER_AUDIO_RUNTIME_OVERRIDES_PATH": str(store)}


@pytest.mark.parametrize(
    ("listing", "prior_outputd", "overrides", "detail"),
    [
        pytest.param(
            INNOMAKER_LISTING,
            "JASPER_OUTPUTD_BACKEND=alsa\n"
            "JASPER_OUTPUTD_SINK=single_alsa\n"
            "JASPER_OUTPUTD_CONTENT_PCM=outputd_active_content_capture\n"
            "JASPER_OUTPUTD_ACTIVE_CHANNELS=8\n"
            "JASPER_OUTPUTD_ACTIVE_LANE=1\n"
            "JASPER_OUTPUTD_PERIOD_FRAMES=128\n"
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES=256\n",
            {"JASPER_OUTPUTD_PERIOD_FRAMES": "1024",
             "JASPER_OUTPUTD_DAC_BUFFER_FRAMES": "1536"},
            None,
            id="incoherent-period-against-a-live-active-lane",
        ),
        pytest.param(
            APPLE_LISTING,
            "JASPER_OUTPUTD_BACKEND=alsa\n"
            "JASPER_OUTPUTD_SINK=single_alsa\n"
            "JASPER_OUTPUTD_PERIOD_FRAMES=128\n"
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES=256\n",
            {"JASPER_OUTPUTD_PERIOD_FRAMES": "1024",
             "JASPER_OUTPUTD_DAC_BUFFER_FRAMES": "256"},
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES_256",
            id="dac-buffer-smaller-than-the-period",
        ),
    ],
)
def test_reconcile_refusal_preserves_env_and_leaves_every_service_running(
    tmp_path: Path,
    listing: str,
    prior_outputd: str,
    overrides: dict[str, str],
    detail: str | None,
):
    """A REFUSED reconcile leaves the box running exactly as it was found:
    outputd.env byte-unchanged, no render, and no unit stopped, because
    nothing this run did reached a daemon. The refusal also names the
    ORIGIN as a file that still exists — the validated candidate lives
    under a `.outputd.env.candidate.XXXXXX` temp name deleted on EXIT, so
    reporting the path it READ named a file the operator cannot open.
    """
    result = _run_reconcile(
        tmp_path,
        listing,
        "--reason",
        "test",
        initial_env="JASPER_AUDIO_ROUTE_PROFILE=usb_low_latency_48k\n",
        initial_outputd_env=prior_outputd,
        extra_env=_override_store(tmp_path, **overrides),
    )

    assert result.returncode == 78, result.stderr
    assert _outputd_env(tmp_path) == prior_outputd
    if detail is not None:
        assert detail in result.stderr
    assert "event=audio_hardware_reconcile.outputd_env_invalid" in result.stderr
    assert "event=audio_hardware_reconcile.outputd_candidate_rejected" in result.stderr
    assert "preserved=1" in result.stderr
    assert "action=preserve_runtime_env" in result.stderr
    # The log line PRINTS the promise the assertions below prove.
    assert "services=unchanged" in result.stderr
    # `log_event` tokenizes the detail, so match the tokenized spelling.
    assert "override_store" in result.stderr
    assert _log_token(str(tmp_path / "outputd.env")) in result.stderr
    assert "outputd.env.candidate" not in result.stderr
    assert not (tmp_path / "asoundrc.jasper.template").exists()
    assert _render_log(tmp_path) == ""
    # No unit stopped, so none can stay stopped. Matched on the systemctl VERB
    # (argv token `stop`), never a substring, so a unit name containing "stop"
    # could not make this pass vacuously.
    stopped = [
        line for line in _systemctl_log(tmp_path).splitlines() if "stop" in line.split()
    ]
    assert stopped == [], stopped
    assert "jasper-voice.service" not in _systemctl_log(tmp_path)


def test_the_note_prefix_the_script_matches_is_the_one_the_cli_prints(
    tmp_path: Path, capsys
) -> None:
    """The bash/Python seam for the waypoint note, pinned from BOTH sides.

    `validate_outputd_env_stage` recognises a coherent-but-transient
    result by the literal prefix the CLI prints on exit 0. Nothing else
    couples them, so a reworded CLI would silently stop the reconciler
    logging `outputd_env_note`. Asserting the literal in the script alone
    would still pass if the CLI changed, so this also PRODUCES a note.
    """
    from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE
    from tests.test_ring_active_endpoint import (
        _active_topology,
        _emit_active_baseline,
        _mono_two_way_preset,
        _run_validate_outputd_env,
    )

    script_text = SCRIPT.read_text(encoding="utf-8")
    # Matched anywhere, not as a prefix: the capture merges stderr, so a
    # warning emitted before stdout would push the marker off the front.
    _assert_states(script_text, '*"ok note="*', 'log_event "outputd_env_note"')

    # A REAL emitted graph, not a hand-written stanza: the CLI demotes any
    # active-endpoint graph failing `outputd_active_lane_decision` to
    # devices=None, and a stub stanza fails it — printing a bare "ok" and
    # making this contract vacuous.
    rc, out = _run_validate_outputd_env(
        tmp_path,
        capsys,
        graph_yaml=_emit_active_baseline(
            _mono_two_way_preset(), RING_ACTIVE_PLAYBACK_DEVICE
        ),
        topology=_active_topology("mono", "active_2_way"),
        coupling="loopback",
        marker=None,
    )

    assert rc == 0, out
    assert out.startswith("ok note="), out


# --- per-box shm-ring conf.d render -------------------------------------------
#
# The rule: the reconciler renders the ring conf.d slot period ONLY from the
# active DAC profile's DECLARED LatencyFloor. No declared floor — and any
# unrecognized DAC — leaves the SHIPPED conf.d genuinely untouched (byte AND
# mtime), so that box keeps its current coupling.


def _staged_ring_conf(tmp_path: Path) -> Path:
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_bytes(SHIPPED_RING_CONF.read_bytes())
    return conf


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


def _render_ring_conf(conf: Path, topology: Path | None = None) -> int:
    from jasper.cli.audio_config import main as audio_config_main

    args = [
        "render-ring-conf-wire", "--profile-id", "hifiberry_dac8x",
        "--conf-d", str(conf),
    ]
    if topology is not None:
        args += ["--output-topology", str(topology)]
    return audio_config_main(args)


@pytest.fixture
def declare_slot_floor(monkeypatch):
    """Declare a synthetic LatencyFloor for hifiberry_dac8x and nothing else.

    The floor is DATA, not a per-DAC code branch: a profile that declares the
    ring slot renders, and one that declares anything else is refused.
    """

    def _declare(period_frames: int = RING_SLOT_FRAMES) -> None:
        from jasper.audio_hardware.dac import LatencyFloor

        floor = LatencyFloor(
            outputd_period_frames=period_frames,
            outputd_dac_buffer_frames=8 * period_frames,
        )
        monkeypatch.setattr(
            "jasper.cli.audio_config.latency_floor_for",
            lambda profile_id: floor if profile_id == "hifiberry_dac8x" else None,
        )

    return _declare


@pytest.mark.parametrize(
    ("listing", "event"),
    [
        pytest.param(
            APPLE_LISTING,
            "event=audio_hardware_reconcile.ring_conf pass_reason=test "
            "result=unchanged output_dac_id=apple_usb_c_dongle period_frames=128 "
            "previous_period_frames=128 sample_format=S32_LE ring_a_channels=2 "
            "ring_b_channels=2 ring_active_channels=2 topology=",
            id="apple-floor-matches-shipped-wire",
        ),
        pytest.param(
            DAC8X_STUDIO_LISTING,
            "event=audio_hardware_reconcile.ring_conf pass_reason=test "
            "result=skipped "
            "output_dac_id=hifiberry_dac8x_studio period_frames=none "
            "previous_period_frames=none sample_format=none ring_a_channels=none "
            "ring_b_channels=none ring_active_channels=none topology=none "
            "reason=no_declared_floor",
            id="profile-declares-no-floor",
        ),
        pytest.param(
            DAC8X_AND_APPLE_LISTING,
            "event=audio_hardware_reconcile.ring_conf pass_reason=test "
            "result=unchanged output_dac_id=hifiberry_dac8x period_frames=128 "
            "previous_period_frames=128 sample_format=S32_LE ring_a_channels=2 "
            "ring_b_channels=2 ring_active_channels=2 topology=",
            id="dac8x-floor-matches-shipped-wire",
        ),
        pytest.param(
            "",
            "event=audio_hardware_reconcile.ring_conf pass_reason=test "
            "result=skipped reason=dac_unrecognized",
            id="dac-unrecognized",
        ),
    ],
)
def test_reconcile_preserves_a_ring_conf_that_needs_no_render(
    tmp_path: Path, listing: str, event: str
):
    conf = _staged_ring_conf(tmp_path)
    before_bytes = conf.read_bytes()
    before_mtime = conf.stat().st_mtime_ns

    result = _run_reconcile(
        tmp_path, listing, "--reason", "test",
        extra_env={"JASPER_RING_CONF_D": str(conf)},
    )

    assert result.returncode == 0, result.stderr
    assert event in result.stderr
    assert conf.read_bytes() == before_bytes
    assert conf.stat().st_mtime_ns == before_mtime


def test_render_subcommand_renders_for_any_profile_declaring_the_slot_floor(
    tmp_path: Path, declare_slot_floor, capsys
) -> None:
    conf = _drifted_ring_conf(tmp_path)
    declare_slot_floor()

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
    assert conf.read_text(encoding="utf-8").count(
        f"    period_frames {RING_SLOT_FRAMES}"
    ) == len(ring_assets.RING_CONF_PCMS)


def test_render_subcommand_refuses_a_floor_the_ring_slot_cannot_carry(
    tmp_path: Path, declare_slot_floor, capsys
) -> None:
    # Ring A's slot is fan-in's COMPILE-TIME RING_SLOT_FRAMES (128, no env
    # override): rust/jasper-ring/src/layout.rs pins it and mixer.rs creates
    # the ring with it. Rendering a non-128 period into pcm.jts_ring_capture
    # would make CamillaDSP's ioplug attach expect N against fan-in's
    # 128-frame ring — a hard RING_ATTACH_FATAL geometry error that CRASHES
    # shm_ring at arm rather than refusing it.
    conf = _staged_ring_conf(tmp_path)
    before_bytes = conf.read_bytes()
    before_mtime = conf.stat().st_mtime_ns
    declare_slot_floor(2 * RING_SLOT_FRAMES)

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
    tmp_path: Path, declare_slot_floor, capsys
) -> None:
    # Reconcile runs on every boot and udev event; a converged box must stop
    # writing rather than churn the mtime on each pass.
    conf = _drifted_ring_conf(tmp_path)
    declare_slot_floor()

    assert _render_ring_conf(conf) == 0
    capsys.readouterr()
    settled_bytes = conf.read_bytes()
    settled_mtime = conf.stat().st_mtime_ns

    assert _render_ring_conf(conf) == 0
    assert "result unchanged" in capsys.readouterr().out
    assert conf.read_bytes() == settled_bytes
    assert conf.stat().st_mtime_ns == settled_mtime


def test_render_subcommand_reports_a_torn_conf_instead_of_inventing_one(
    tmp_path: Path, declare_slot_floor, capsys
) -> None:
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text("pcm.jts_ring_capture { type jts_ring }\n", encoding="utf-8")
    declare_slot_floor()

    assert _render_ring_conf(conf) == 1
    assert "no period_frames" in capsys.readouterr().err
    assert conf.read_text(encoding="utf-8") == (
        "pcm.jts_ring_capture { type jts_ring }\n"
    )


@pytest.mark.parametrize(
    ("topology_json", "expected"),
    [
        # Every axis the renderer resolved is emitted for the shell to log. A
        # key the CLI does not print is a key the journal reports as `none`,
        # which is how a per-box wire becomes invisible at the exact moment it
        # starts differing between boxes.
        pytest.param(None, "", id="no-topology-argument"),
        # Fail-safe direction for a RENDERER: a topology it cannot read must
        # never move the conf.d off what the box is already running. Refusing
        # to ARM on one is the preflights' job. CORRUPT, not absent —
        # load_output_topology_strict returns an empty draft for a missing
        # file ("not configured yet" is a real, ring-eligible shape).
        pytest.param("{not json", "topology_unreadable", id="corrupt-topology"),
        pytest.param("<stereo>", "loaded", id="readable-topology"),
    ],
)
def test_render_subcommand_reports_the_wire_and_the_topology_it_resolved(
    tmp_path: Path, declare_slot_floor, capsys, topology_json: str | None, expected: str
) -> None:
    from jasper import ring_assets
    from tests.test_active_speaker_runtime_contract import _full_range_stereo

    conf = _drifted_ring_conf(tmp_path)
    declare_slot_floor()
    topology_path: Path | None = None
    if topology_json is not None:
        topology_path = tmp_path / "output_topology.json"
        topology_path.write_text(
            json.dumps(_full_range_stereo().to_dict())
            if topology_json == "<stereo>"
            else topology_json,
            encoding="utf-8",
        )

    assert _render_ring_conf(conf, topology_path) == 0
    out = capsys.readouterr().out
    assert f"topology {expected}" in out
    assert "sample_format S32_LE" in out
    assert "ring_a_channels 2" in out
    assert "ring_b_channels 2" in out
    assert ring_assets.ring_conf_period_frames(str(conf)) == RING_SLOT_FRAMES
    for pcm in (ring_assets.RING_A_CONF_PCM, ring_assets.RING_B_CONF_PCM):
        assert ring_assets.ring_conf_format(pcm, str(conf)) == "S32_LE"
        assert ring_assets.ring_conf_channels(pcm, str(conf)) == 2


# --- the flat cutover render --------------------------------------------------
#
# The startup graph is width-matched to the SAVED output topology, so it goes
# stale whenever the layout changes. The two paths that change it — the
# /sound/ topology save and jasper-output-topology-reset — run inside
# jasper-web's sandbox, which has no /etc/camilladsp write path
# (WS1-deliberate). Both kick THIS reconciler, which runs as root.


def _flat_cutover_event(stderr: str) -> dict[str, str]:
    """The flat_cutover log line, parsed into its `key=value` fields."""
    prefix = "event=audio_hardware_reconcile.flat_cutover "
    lines = [line for line in stderr.splitlines() if line.startswith(prefix)]
    assert len(lines) == 1, f"expected exactly one flat_cutover event, got {lines}"
    fields: dict[str, str] = {}
    for token in lines[0][len(prefix):].split():
        key, _, value = token.partition("=")
        assert key not in fields, key
        fields[key] = value
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


def _cutover_env(tmp_path: Path) -> dict[str, str]:
    conf_dir = tmp_path / "camilladsp"
    conf_dir.mkdir(exist_ok=True)
    return {
        "JASPER_SOUND_CLI": str(_fake_jasper_sound_cli(tmp_path)),
        "JASPER_CAMILLA_CONF_DIR": str(conf_dir),
        "PYTHONPATH": str(ROOT),
    }


def test_reconcile_renders_the_width_matched_cutover_and_is_idempotent(
    tmp_path: Path,
):
    """Write-on-change, and width-matched to the saved topology.

    The reconciler runs on every boot and every sound-card event, so an
    unconditional write would churn the file's mtime and make "did the graph
    change?" unanswerable from the filesystem.
    """
    extra = _cutover_env(tmp_path)
    (tmp_path / "output_topology.json").write_text(
        json.dumps(_mono_topology_payload()), encoding="utf-8"
    )

    first = _run_reconcile(
        tmp_path, INNOMAKER_LISTING, "--reason", "test", extra_env=extra
    )
    assert first.returncode == 0, first.stderr
    assert _flat_cutover_event(first.stderr)["result"] == "ok"
    assert _flat_cutover_event(first.stderr)["changed"] == "yes"

    cutover = Path(extra["JASPER_CAMILLA_CONF_DIR"]) / "outputd-cutover.yml"
    # Width-matched: the mono topology claims output 0, so channel 1 is muted.
    assert "as_out1_commission_mute" in cutover.read_text(encoding="utf-8")
    # ONE flat config: its `shm_ring` sibling collapsed into it (ADR-0100).
    assert not (cutover.parent / "outputd-cutover-ring.yml").exists()
    assert cutover.stat().st_mode & 0o777 == 0o644
    before = (cutover.stat().st_mtime_ns, cutover.read_bytes())

    second = _run_reconcile(
        tmp_path, INNOMAKER_LISTING, "--reason", "test", extra_env=extra
    )
    assert second.returncode == 0, second.stderr
    assert _flat_cutover_event(second.stderr)["result"] == "ok"
    assert _flat_cutover_event(second.stderr)["changed"] == "no"
    assert (cutover.stat().st_mtime_ns, cutover.read_bytes()) == before


def test_reconcile_refuses_to_render_against_a_corrupt_topology(tmp_path: Path):
    """A corrupt topology must FAIL the render, not succeed unmuted.

    `flat_graph_muted_outputs` fails SOFT, which is right for callers with
    a guard behind them. This renderer must keep the last proved bytes
    instead: the runtime selector then rejects stale intent, and the boot
    unit ordering keeps CamillaDSP from starting on a failed convergence.
    """
    extra = _cutover_env(tmp_path)
    topology = tmp_path / "output_topology.json"

    topology.write_text(json.dumps(_mono_topology_payload()), encoding="utf-8")
    healthy = _run_reconcile(
        tmp_path, INNOMAKER_LISTING, "--reason", "test", extra_env=extra
    )
    assert healthy.returncode == 0, healthy.stderr
    cutover = Path(extra["JASPER_CAMILLA_CONF_DIR"]) / "outputd-cutover.yml"
    good = cutover.read_bytes()
    assert b"as_out1_commission_mute" in good

    topology.write_text("{not json", encoding="utf-8")
    corrupt = _run_reconcile(
        tmp_path, INNOMAKER_LISTING, "--reason", "test", extra_env=extra
    )

    # BYTES FIRST: the substantive harm is the good graph being overwritten
    # with an unmuted one, so that is what must fail without the fix.
    assert cutover.read_bytes() == good
    # The reconcile still completes (a render failure is best-effort) but is
    # reported FAILED rather than logged as a successful render.
    assert corrupt.returncode == 0, corrupt.stderr
    assert _flat_cutover_event(corrupt.stderr)["result"] == "failed"


def test_reconcile_renders_the_golden_when_no_topology_is_saved(tmp_path: Path):
    """MISSING is not CORRUPT, so rendering can still seed the golden artifact.

    This does not authorize playback: the runtime selector parks a fresh box
    until the household saves an explicit mono or stereo layout.
    """
    extra = _cutover_env(tmp_path)
    result = _run_reconcile(
        tmp_path, INNOMAKER_LISTING, "--reason", "test", extra_env=extra
    )

    assert result.returncode == 0, result.stderr
    assert _flat_cutover_event(result.stderr)["result"] == "ok"
    rendered = (
        Path(extra["JASPER_CAMILLA_CONF_DIR"]) / "outputd-cutover.yml"
    ).read_text(encoding="utf-8")
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
    fields = _flat_cutover_event(result.stderr)
    assert fields["result"] == "skipped"
    assert fields["reason"] == "cli_unavailable"
    assert fields["pass_reason"] == "test"


def test_reconcile_degraded_marker_path_matches_the_output_hardware_helper(
    monkeypatch, tmp_path: Path
):
    """``RECONCILE_DEGRADED_MARKER`` (this script) and
    ``output_hardware.degraded_marker_path`` (the doctor's evidence) must
    resolve to the same file under the same
    ``JASPER_OUTPUT_HARDWARE_STATE_PATH`` -- one path for one fact."""
    from jasper.output_hardware import degraded_marker_path

    state_path = tmp_path / "output_hardware.json"
    result = _run_reconcile(
        tmp_path,
        INNOMAKER_LISTING,
        "--reason",
        "test",
        extra_env={
            "JASPER_SOUND_CLI": str(tmp_path / "absent"),
            "JASPER_OUTPUT_HARDWARE_STATE_PATH": str(state_path),
        },
    )
    assert result.returncode == 0, result.stderr

    monkeypatch.setenv("JASPER_OUTPUT_HARDWARE_STATE_PATH", str(state_path))
    assert degraded_marker_path().is_file()


# --- the content-lane format axis ---------------------------------------------
# The reconciler is the single writer of JASPER_OUTPUTD_CONTENT_FORMAT, and its
# value comes from the SAME function that decides what CamillaDSP emits
# (jasper.fanin_coupling.content_lane_format_for_coupling) — so outputd cannot
# ask for a width the emitters do not produce.


@pytest.mark.parametrize(
    ("initial_fanin_env", "initial_outputd_env"),
    [
        pytest.param(None, None, id="loopback-the-unset-default"),
        # Ring A and Ring B move together; without Ring B's bridge the
        # reconciler's own transport-coherence validator rejects the stage
        # (correctly) before the format axis is reachable.
        pytest.param(
            "JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n",
            "JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring\n",
            id="armed-shm-ring",
        ),
    ],
)
def test_reconcile_emits_the_wide_content_format(
    tmp_path: Path, initial_fanin_env: str | None, initial_outputd_env: str | None
):
    """Both couplings carry the wide program lane, plumbed verbatim from
    content_lane_format_for_coupling.

    An operator narrow pin (JASPER_FANIN_RING_WIRE_FORMAT=S16_LE) is not
    reachable here: the probe's ring-wire read is file-fresh against the
    REAL /etc/jasper/jasper.env and /var/lib/jasper/fanin.env, which on a
    Pi are the files this harness diverges into tmp_path. That pin is
    exercised in tests/test_fanin_coupling.py and
    tests/test_audio_runtime_plan.py.
    """
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_fanin_env=initial_fanin_env,
        initial_outputd_env=initial_outputd_env,
    )

    assert result.returncode == 0, result.stderr
    outputd_env = _outputd_env(tmp_path)
    assert "JASPER_OUTPUTD_CONTENT_FORMAT=S32_LE" in outputd_env
    # The content lane and the DAC edge are separate hops with separate
    # declarations, and on this box they legitimately differ: an S32 lane into
    # the Apple dongle's packed S24_3LE edge, the widest it advertises.
    assert "JASPER_OUTPUTD_DAC_FORMAT=S24_3LE" in outputd_env
    assert "content_format=S32_LE" in result.stderr


@pytest.mark.parametrize(
    "spelling", ["rate_match", "ratematch", "rate-matched", "rate_matched"]
)
def test_reconcile_no_longer_narrows_for_the_removed_rate_match_bridge(
    tmp_path: Path, spelling: str
):
    """The i16-only `rate_match` content bridge was DELETED, and its S16_LE
    narrowing went with it.

    The narrowing kept a routine deploy from emitting a wide content lane
    into a bridge outputd refuses (exit 78 -> parked output owner, silent
    speaker). With the bridge gone outputd parks on every spelling rather
    than reading a content format at all.
    """
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_outputd_env=f"JASPER_OUTPUTD_CONTENT_BRIDGE={spelling}\n",
    )

    assert result.returncode == 0, result.stderr
    outputd_env = _outputd_env(tmp_path)
    # The loopback coupling's own width, NOT the narrowed S16_LE.
    assert "JASPER_OUTPUTD_CONTENT_FORMAT=S32_LE" in outputd_env
    # The stale operator value is left alone; outputd is what fail-safes it.
    assert f"JASPER_OUTPUTD_CONTENT_BRIDGE={spelling}" in outputd_env
    _assert_omits(result.stderr, "content_format_narrowed", "rate_match_content_bridge")


def _probe_fails_python(tmp_path: Path, name: str, needle: str) -> Path:
    """Real interpreter for every call EXCEPT the one whose heredoc holds
    ``needle``, which fails.

    Injected at one call rather than by removing the interpreter: earlier
    Python probes (the I2S HAT boot pass, the hardware observation) would
    abort the reconcile before either format axis is reached.
    """
    shim = tmp_path / name
    shim.write_text(
        "#!/bin/bash\n"
        'script="$(cat)"\n'
        f'if [[ "$script" == *{needle}* ]]; then\n'
        '  echo "simulated: probe unavailable" >&2\n'
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
    """No answer == no write. A bash-side fallback would be a second spelling
    of DEFAULT_PLAYBACK_FORMAT, and writing empty would silently narrow a wide
    box (outputd reads empty as S16_LE), so the key keeps whatever the box had
    and the skip is logged."""
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_outputd_env="JASPER_OUTPUTD_CONTENT_FORMAT=S32_LE\n",
        extra_env={
            "JASPER_OUTPUT_HARDWARE_PYTHON": str(
                _probe_fails_python(
                    tmp_path, "python-no-coupling", "content_lane_format_for_coupling"
                )
            )
        },
    )

    assert result.returncode == 0, result.stderr
    assert "JASPER_OUTPUTD_CONTENT_FORMAT=S32_LE" in _outputd_env(tmp_path)
    assert "event=audio_hardware_reconcile.content_format_skip" in result.stderr
    assert "reason=coupling_probe_unavailable" in result.stderr
    assert "content_format=unset" in result.stderr


@pytest.mark.parametrize("composite", [False, True], ids=["single-dac", "composite"])
def test_reconcile_leaves_the_edge_format_alone_when_the_registry_probe_is_absent(
    tmp_path: Path, composite: bool
):
    """A lost probe must not commit an empty edge format.

    Empty is MEANINGFUL on this key — outputd reads it as S16_LE — so
    writing it would silently narrow this box's declared S24_3LE edge with
    no error anywhere. Nothing about the hardware changed, so keep the
    previous value and log the skip. (The explicit-empty write for a DAC
    with no queryable profile is a different branch, where emptiness IS
    the answer.) The composite arm shares the helper from a second call
    site; the seeded S24_3LE is the stale single-dongle format a box
    carries across a single -> dual upgrade, so the skip contract has to
    hold independently of whether the stale value is survivable. The
    outputd sink kind (DacProfile.outputd_sink, ADR-0235 R1) comes from the
    same probe call and degrades the same way — seeded here to the OTHER
    shape's sink, so a preserved value is distinguishable from a re-derived
    one exactly like the format axis.
    """
    extra_env = {
        "JASPER_OUTPUT_HARDWARE_PYTHON": str(
            _probe_fails_python(
                tmp_path, "python-no-edge-format", "final_edge_format_for"
            )
        )
    }
    listing = APPLE_LISTING
    expected_dac_id = "apple_usb_c_dongle"
    stale_sink = "single_alsa" if composite else "dual_apple"
    if composite:
        listing = DUAL_APPLE_LISTING
        expected_dac_id = "dual_apple_usb_c_dac_4ch"
        extra_env.update(
            {
                **_dual_apple_cards(tmp_path, _DUAL_APPLE_CARDS_SWAPPED),
                "JASPER_OUTPUT_TOPOLOGY_PATH": str(
                    _dual_apple_topology(tmp_path, active=True)
                ),
                **_active_graph_env(tmp_path, write_topology=False),
            }
        )

    result = _run_reconcile(
        tmp_path,
        listing,
        "--reason",
        "test",
        initial_outputd_env=(
            "JASPER_OUTPUTD_DAC_FORMAT=S24_3LE\n"
            f"JASPER_OUTPUTD_SINK={stale_sink}\n"
        ),
        extra_env=extra_env,
    )

    assert result.returncode == 0, result.stderr
    outputd_env = _outputd_env(tmp_path)
    # Preserved, not cleared — and specifically NOT the explicit-empty
    # spelling the unrecognized-DAC branch writes.
    assert "JASPER_OUTPUTD_DAC_FORMAT=S24_3LE" in outputd_env
    assert "JASPER_OUTPUTD_DAC_FORMAT=''" not in outputd_env
    assert f"JASPER_OUTPUTD_SINK={stale_sink}" in outputd_env
    assert "event=audio_hardware_reconcile.dac_format_skip" in result.stderr
    assert "reason=registry_probe_unavailable" in result.stderr
    assert f"dac_id={expected_dac_id}" in result.stderr
    # The value left in place is named on the skip line, and the runtime_env
    # summary carries it under the same dac_format= key content_format= uses.
    assert "preserved=S24_3LE" in result.stderr
    assert "event=audio_hardware_reconcile.runtime_env" in result.stderr
    assert "dac_format=S24_3LE" in result.stderr
    if composite:
        assert "mode=dual_apple" in result.stderr


# --- the DAC-swap edge into the coupling reconciler ---------------------------
#
# udev already reached this script on every controlC* event; the chain stopped
# here. These pin the edge that continues it, and the guard that keeps the two
# reconcilers from kicking each other forever.

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


def _assert_kicked_once(tmp_path: Path, result: subprocess.CompletedProcess[str]):
    starts, events = _coupling_kick_lines(tmp_path, result)
    assert starts, _systemctl_log(tmp_path)
    # --no-block IS THE DEADLOCK GUARD, not a nicety: the coupling pass kicks
    # this script back SYNCHRONOUSLY inside its arm, so a blocking start here
    # leaves this script waiting on a pass that is waiting on this script.
    assert all("--no-block" in line for line in starts), starts
    assert len(events) == 1 and "result=started" in events[0], events


def test_a_plugged_registered_dac_converges_without_an_operator(tmp_path: Path):
    """Plug a registered DAC in, and the box arms itself.

    A first pass sets dac_env_changed and render_changed, so the edge
    fires and the coupling reconciler gets its chance to converge.
    Without it the box renders a correct asound.conf, bounces outputd,
    then sits on loopback forever waiting for a human to type the arm.
    """
    result = _run_reconcile(tmp_path, INNOMAKER_LISTING, "--reason", "udev")

    assert result.returncode == 0, result.stderr
    _assert_kicked_once(tmp_path, result)


def test_an_unrecognized_dac_parks_and_does_not_kick_the_coupling(tmp_path: Path):
    """THE OTHER HALF: an unproven shape parks loudly and converges nothing.

    There is no output for a coupling to converge onto, so the park is the end
    state — not something to reconcile out of here.
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
    save_output_topology(
        OutputTopology.from_mapping(unconfigured), path=topology_path
    )

    first = _run_reconcile(tmp_path, INNOMAKER_LISTING, "--reason", "udev")
    assert first.returncode == 0, first.stderr
    (tmp_path / "systemctl.log").write_text("", encoding="utf-8")

    # The household now commissions ordinary passive stereo. Hardware and all
    # generated DAC bytes are unchanged, but auto-coupling must see new intent.
    save_output_topology(configured, path=topology_path)
    second = _run_reconcile(tmp_path, INNOMAKER_LISTING, "--reason", "udev")

    assert second.returncode == 0, second.stderr
    _assert_states(second.stderr, "dac_env_changed=0", "render_changed=0")
    _assert_kicked_once(tmp_path, second)


def test_successful_runtime_convergence_is_the_coupling_kick_guard():
    """The trigger is final graph success, not DAC/render byte movement, and
    every start inside the kick is --no-block."""
    source = SCRIPT.read_text(encoding="utf-8")
    call = [
        line.strip()
        for line in source.splitlines()
        if "kick_fanin_coupling_auto_if_needed " in line and "()" not in line
    ]

    assert call == [
        'kick_fanin_coupling_auto_if_needed "$dac_env_changed" "$render_changed"'
    ], call
    guard_offset = source.rfind(
        'if [[ "$runtime_converge_failed" == "0" ]]', 0, source.index(call[0])
    )
    assert guard_offset >= 0

    body = source.split("kick_fanin_coupling_auto_if_needed() {", 1)[1].split("\n}", 1)[0]
    start_lines = [
        line for line in body.splitlines() if "start" in line and "SYSTEMCTL" in line
    ]
    assert start_lines, body
    assert all("--no-block" in line for line in start_lines), start_lines


# --- skipping a pass whose inputs have not moved ---


def _fake_proc_asound(tmp_path: Path) -> dict[str, str]:
    root = tmp_path / "proc-asound"
    root.mkdir(exist_ok=True)
    (root / "cards").write_text(" 0 [Loopback]: Loopback\n", encoding="utf-8")
    (root / "pcm").write_text("00-00: Loopback : playback 1\n", encoding="utf-8")
    return {"JASPER_PROC_ASOUND": str(root)}


def _hotplug_mid_pass(tmp_path: Path) -> dict[str, str]:
    """Move the card list while the pass is running, from inside its own CLI."""
    hook = tmp_path / "hotplug-hook"
    hook.write_text(
        "#!/usr/bin/env bash\n"
        "printf ' 9 [Late]: USB-Audio - Late arrival\\n'"
        ' >> "$JASPER_PROC_ASOUND/cards"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    return {"JASPER_FAKE_ACTIVE_SPEAKER_HOOK": str(hook)}


def _exit64_renderer(tmp_path: Path) -> dict[str, str]:
    failing = tmp_path / "exit64-render-asound-conf"
    failing.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'render\\n\' >> "$JASPER_RENDER_LOG"\n'
        "exit 64\n",
        encoding="utf-8",
    )
    failing.chmod(0o755)
    return {"JASPER_RENDER_ASOUND_CONF": str(failing)}


_SEED_MODES: dict[str | None, tuple[int, bool, str | None]] = {
    # seed mode -> (expected returncode, stamp written, stamp_skipped reason)
    None: (0, True, None),
    "renderer-fails": (78, False, None),
    "card-moves-mid-pass": (0, False, "hardware_moved_mid_pass"),
    "sound-cli-missing": (0, False, "probe_unavailable"),
}


@pytest.mark.parametrize(
    ("seed_mode", "mutate", "expected_rc"),
    [
        pytest.param(None, None, 1, id="unchanged-skips"),
        pytest.param(None, "cards", 0, id="card-set-moved-runs"),
        pytest.param(None, "topology", 0, id="input-file-moved-runs"),
        pytest.param("renderer-fails", None, 0, id="failed-pass-left-no-stamp"),
        pytest.param("card-moves-mid-pass", None, 0, id="mid-pass-hotplug-no-stamp"),
        pytest.param("sound-cli-missing", None, 0, id="probe-unavailable-no-stamp"),
    ],
)
def test_changed_check_skips_only_after_a_successful_pass_over_the_same_inputs(
    tmp_path: Path, seed_mode: str | None, mutate: str | None, expected_rc: int
) -> None:
    """The unit's ExecCondition: exit 0 means run, 1 means skip.

    A skipped call must reconcile nothing, and only an unchanged box that a
    successful pass already stamped may be skipped.
    """
    common = {**_fake_proc_asound(tmp_path), **_cutover_env(tmp_path)}
    seed_env = dict(common)
    if seed_mode == "renderer-fails":
        # A stamp an earlier good pass left must not survive a failed one.
        pre = _run_reconcile(
            tmp_path, APPLE_LISTING, "--reason", "pre-seed", extra_env=common
        )
        assert pre.returncode == 0, pre.stderr
        assert (tmp_path / "reconcile.stamp").exists(), pre.stderr
        # Drop the rendered template so the failing renderer is actually reached.
        (tmp_path / "asoundrc.jasper.template").unlink()
        seed_env.update(_exit64_renderer(tmp_path))
    elif seed_mode == "card-moves-mid-pass":
        seed_env.update(_hotplug_mid_pass(tmp_path))
    elif seed_mode == "sound-cli-missing":
        seed_env["JASPER_SOUND_CLI"] = str(tmp_path / "absent-jasper-sound")
    seed_rc, stamped, skip_reason = _SEED_MODES[seed_mode]
    seed = _run_reconcile(
        tmp_path, APPLE_LISTING, "--reason", "seed", extra_env=seed_env
    )
    assert seed.returncode == seed_rc, seed.stderr
    assert (tmp_path / "reconcile.stamp").exists() is stamped, seed.stderr
    if skip_reason is not None:
        _assert_states(
            seed.stderr,
            "event=audio_hardware_reconcile.stamp_skipped ",
            f"reason={skip_reason}",
        )

    if mutate == "cards":
        (tmp_path / "proc-asound" / "cards").write_text(
            " 1 [Dongle]: USB-Audio - Apple USB-C\n", encoding="utf-8"
        )
    elif mutate == "topology":
        (tmp_path / "output_topology.json").write_text("{}\n", encoding="utf-8")

    rendered_before = _render_log(tmp_path)
    issued_before = len(_systemctl_log(tmp_path).splitlines())
    check = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "unit-start",
        "--changed",
        # A pass may rewrite the boot config; _run_reconcile would otherwise
        # reset it under the check and manufacture a change.
        initial_boot_config=(tmp_path / "config.txt").read_text(encoding="utf-8"),
        extra_env=common,
    )
    assert check.returncode == expected_rc, check.stderr
    verdict = "skipped" if expected_rc == 1 else "changed"
    _assert_states(check.stderr, f"event=audio_hardware_reconcile.{verdict} ")
    # The check decides; it never reconciles.
    assert _render_log(tmp_path) == rendered_before
    assert _systemctl_log(tmp_path).splitlines()[issued_before:] == []
    _assert_omits(check.stderr, "event=audio_hardware_reconcile.complete")


def test_changed_check_reruns_while_the_degraded_marker_is_present(
    tmp_path: Path,
) -> None:
    """A probe outage during ``--print-env`` (install.sh's mid-install call)
    can set the degraded marker WITHOUT going through a full pass's own
    stamp/marker reset (that reset only runs on the mutating path) -- so an
    OLD stamp an earlier successful full pass left behind survives, and would
    otherwise still match the now-unchanged fingerprint. Without the marker
    check, the doctor's remedy (`systemctl start
    jasper-audio-hardware-reconcile`) would be skipped instead of re-running
    the pass."""
    common = {**_fake_proc_asound(tmp_path), **_cutover_env(tmp_path)}
    healthy = _run_reconcile(
        tmp_path, APPLE_LISTING, "--reason", "seed-healthy", extra_env=common
    )
    assert healthy.returncode == 0, healthy.stderr
    assert (tmp_path / "reconcile.stamp").exists()

    print_env = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--print-env",
        initial_boot_config=(tmp_path / "config.txt").read_text(encoding="utf-8"),
        extra_env={
            **common,
            "JASPER_OUTPUT_HARDWARE_PYTHON": str(tmp_path / "absent-python"),
        },
    )
    assert print_env.returncode == 0, print_env.stderr
    assert (tmp_path / "reconcile.degraded").exists()
    # The stamp from the earlier full pass is left as-is: --print-env never
    # touches it either way.
    assert (tmp_path / "reconcile.stamp").exists()

    check = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "unit-start",
        "--changed",
        initial_boot_config=(tmp_path / "config.txt").read_text(encoding="utf-8"),
        extra_env=common,
    )
    assert check.returncode == 0, check.stderr
    _assert_states(check.stderr, "event=audio_hardware_reconcile.changed ")
