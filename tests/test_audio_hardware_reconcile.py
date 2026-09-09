# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from jasper.audio_hardware import reconcile as reconcile_module
from jasper.audio_hardware.dac import final_edge_format_for
from jasper.audio_hardware.usb_port_role import (
    reconcile_boot_config as _real_boot_config,
)
from jasper import audio_runtime_plan, output_hardware
from jasper.fanin_coupling import RING_SLOT_FRAMES
from tests._lock_holder import spawn_lock_holder
from tests._log_events import parse_event, stderr_event, stderr_events
from tests.reconcile_fixtures import (
    fake_systemctl as _fake_systemctl,
    systemctl_log as _systemctl_log,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-audio-hardware-reconcile"
SHIPPED_RING_CONF = ROOT / "deploy" / "alsa" / "conf.d" / "60-jts-ring.conf"


def _script(tmp_path: Path, name: str, body: str) -> Path:
    """An executable stand-in at ``tmp_path/name`` whose whole job is ``body``."""
    script = tmp_path / name
    script.write_text(f"#!/usr/bin/env bash\n{body}", encoding="utf-8")
    script.chmod(0o755)
    return script


def _fake_aplay(tmp_path: Path, listing: str) -> Path:
    (tmp_path / "aplay-L.txt").write_text(listing, encoding="utf-8")
    return _script(tmp_path, "aplay", 'cat "$JASPER_FAKE_APLAY_LISTING"\n')


def _fake_renderer(tmp_path: Path) -> tuple[Path, Path]:
    fake = _script(
        tmp_path,
        "jasper-render-asound-conf",
        "printf 'render\\n' >> \"$JASPER_RENDER_LOG\"\n"
        'cp "$JASPER_ASOUND_TEMPLATE" "$JASPER_ASOUND_CONF"\n',
    )
    return fake, tmp_path / "render.log"


SELECTED_CONFIG_PATH = "/var/lib/camilladsp/configs/sound_current.yml"


def _converged(**kwargs: Any) -> SimpleNamespace:
    """Default success seam for tests not about graph convergence itself."""
    return SimpleNamespace(
        ok=True,
        error=None,
        statefile_written=True,
        topology=None,
        decision=SimpleNamespace(
            ok=True,
            status="select_flat",
            reason="ok",
            selected_config_path=SELECTED_CONFIG_PATH,
        ),
    )


def _reconcile_env(
    tmp_path: Path,
    listing: str,
    *,
    initial_env: str | None = None,
    initial_outputd_env: str | None = None,
    initial_fanin_env: str | None = None,
    initial_template: str | None = None,
    initial_boot_config: str | None = None,
    board_model: str = "Raspberry Pi 5 Model B Rev 1.0",
    active_usb_role: str = "peripheral",
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Stage the fixtures one hermetic pass reads, and the env naming them."""
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
            # Read only by the shim, which _run_shim exercises.
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
            # Hermetic: source the repo's shared lib, never a stale installed
            # copy under /usr/local/lib.
            "JASPER_ASOUND_RENDER_LIB": str(
                ROOT / "deploy" / "lib" / "jasper-asound-render.sh"
            ),
        }
    )
    if extra_env:
        env.update(extra_env)
    return env


@dataclass
class _Pass:
    """One in-process pass: what a caller of the unit's ExecStart observes."""

    returncode: int
    stdout: str
    stderr: str
    converge_calls: list[dict[str, Any]] = field(default_factory=list)


@contextlib.contextmanager
def _captured_events():
    """The `event=` stream the unit's StandardError carries to the journal."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


def _run_reconcile(
    tmp_path: Path,
    listing: str,
    *args: str,
    converge=_converged,
    patches: dict[str, Any] | None = None,
    **fixtures: Any,
) -> _Pass:
    """Run one pass in this process, the way the migrated siblings do.

    ``patches`` replaces the probe functions a pass calls — the in-process
    equivalent of the shell fixtures that stubbed each spawn.
    """
    env = _reconcile_env(tmp_path, listing, **fixtures)
    calls: list[dict[str, Any]] = []

    def recorded_converge(**kwargs: Any):
        calls.append(kwargs)
        return converge(**kwargs)

    out = io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
        stack.enter_context(
            mock.patch(
                "jasper.active_speaker.runtime_convergence.converge_boot_statefile",
                recorded_converge,
            )
        )
        for target, replacement in (patches or {}).items():
            stack.enter_context(mock.patch(target, replacement))
        err = stack.enter_context(_captured_events())
        stack.enter_context(contextlib.redirect_stdout(out))
        code = reconcile_module.main(list(args))
    return _Pass(code, out.getvalue(), err.getvalue(), calls)


def _raises(exc: BaseException):
    """A probe replacement that fails the way a dead spawn did."""

    def _fail(*_args: Any, **_kwargs: Any):
        raise exc

    return _fail


def _run_shim(
    tmp_path: Path, listing: str, *args: str, script: Path = SCRIPT, **fixtures: Any
) -> subprocess.CompletedProcess[str]:
    """Run the shell shim itself — for the verbs it owns (``--changed``, the
    interpreter pin, signal mapping), which by contract never start Python."""
    return subprocess.run(
        ["bash", str(script), *args],
        check=False,
        cwd=ROOT,
        env=_reconcile_env(tmp_path, listing, **fixtures),
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


def _topology_payload(
    *,
    topology_id: str,
    name: str,
    hardware: dict,
    status: str = "ready",
    speaker_groups: list | None = None,
    routing: dict | None = None,
) -> dict:
    """One saved-topology envelope: the schema keys every payload states,
    around the hardware the case is actually about."""
    return {
        "artifact_schema_version": 1,
        "kind": "jts_output_topology",
        "topology_id": topology_id,
        "name": name,
        "status": status,
        "hardware": hardware,
        "speaker_groups": speaker_groups or [],
        "routing": routing or {},
        "safety": {},
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
        payload.update(
            {"topology_id": "dual_apple", "name": "Dual Apple", "hardware": hardware}
        )
    else:
        hardware["outputs"] = []
        payload = _topology_payload(
            topology_id="dual_apple", name="Dual Apple", hardware=hardware
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


@pytest.mark.parametrize(
    "key,filename",
    [
        ("JASPER_ENV_FILE", "jasper.env"),
        ("JASPER_OUTPUTD_ENV_FILE", "outputd.env"),
        ("JASPER_FANIN_ENV_FILE", "fanin.env"),
    ],
)
def test_reconcile_publishes_every_env_file_it_owns_group_readable(
    tmp_path: Path, key: str, filename: str
) -> None:
    """A DAC reconcile must not turn a root:jasper env file into root-only:
    jasper-control needs group read for a fresh /state, and the reconciler both
    writes and repairs these files."""
    seeded = tmp_path / f"seed-{filename}"
    seeded.write_text("JASPER_SEEDED=1\n", encoding="utf-8")
    seeded.chmod(0o600)

    result = _run_reconcile(
        tmp_path, APPLE_LISTING, "--reason", "test", extra_env={key: str(seeded)}
    )

    assert result.returncode == 0, result.stderr
    assert oct(seeded.stat().st_mode & 0o777) == oct(0o640)


def test_ring_conf_journal_line_carries_every_field_the_renderer_resolved(
    tmp_path: Path, declare_slot_floor
) -> None:
    """The event's fields are a WHITELIST - a key the renderer resolves but the
    line never names leaves the wire it rendered undiagnosable."""
    from jasper.ring_assets import ring_conf_wire_report

    declare_slot_floor()
    conf = _staged_ring_conf(tmp_path)
    # The listing has to name the profile `declare_slot_floor` declares for, or
    # the pass short-circuits at `no_declared_floor` and the whitelist below is
    # compared against the three keys that shape carries.
    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--reason",
        "test",
        extra_env={"JASPER_RING_CONF_D": str(conf)},
    )

    assert result.returncode == 0, result.stderr
    resolved = ring_conf_wire_report(
        profile_id="hifiberry_dac8x",
        conf_d=str(conf),
        output_topology=str(tmp_path / "output_topology.json"),
    )
    assert resolved["ring_active_channels"], "the shape under test never rendered"
    fields = stderr_event(result.stderr, "audio_hardware_reconcile.ring_conf")
    # `conf` is journalled as a log token, so compare the key set plus the
    # values that travel verbatim.
    assert set(resolved) - {"conf"} <= set(fields)
    for name, value in resolved.items():
        if name != "conf":
            assert fields[name] == value, name


def test_camilla_boot_requires_successful_runtime_graph_convergence(
    tmp_path: Path,
) -> None:
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
    # The required oneshot runs the same reconciler whose exit status is
    # nonzero when runtime convergence fails.
    assert "ExecStart=/usr/local/sbin/jasper-audio-hardware-reconcile" in hardware_unit
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        converge=lambda **kwargs: SimpleNamespace(
            ok=False,
            error=None,
            statefile_written=False,
            topology=None,
            decision=SimpleNamespace(ok=False, status="parked_muted", reason="stale"),
        ),
    )
    assert result.returncode == 1, result.stderr
    assert (
        stderr_event(result.stderr, "audio_hardware_reconcile.complete")[
            "runtime_converge_failed"
        ]
        == "1"
    )


def test_a_blocking_lifecycle_verb_is_bounded_by_the_unit_not_the_manager_cap(
    tmp_path: Path, monkeypatch
) -> None:
    """``stop jasper-voice.service`` must be allowed to take its own
    ``TimeoutStopSec=14s`` — the window in which voice plays the mic-loss cue
    (ADR-0239). Capped at the manager-liveness bound it would be KILLED, and
    the pass would then restart jasper-outputd against a half-stopped voice.

    The fake logs each verb AFTER doing its work, so the transcript's order is
    completion order, and a killed stop leaves no line at all.
    """
    monkeypatch.setattr(reconcile_module, "SYSTEMCTL_TIMEOUT_SEC", 0.5)
    slow = _script(
        tmp_path,
        "slow-systemctl",
        'case "$*" in "stop jasper-voice.service") sleep 1 ;; esac\n'
        'printf \'%s\\n\' "$*" >> "$JASPER_SYSTEMCTL_LOG"\n',
    )

    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        extra_env={"JASPER_SYSTEMCTL": str(slow)},
    )

    assert result.returncode == 0, result.stderr
    transcript = _systemctl_log(tmp_path).splitlines()
    assert "stop jasper-voice.service" in transcript, transcript
    assert transcript.index("stop jasper-voice.service") < transcript.index(
        "--no-block restart jasper-outputd.service"
    ), transcript
    _assert_omits(result.stderr, "event=audio_hardware_reconcile.systemctl_timeout")


def test_a_candidate_refused_after_convergence_keeps_the_preliminary_env(
    tmp_path: Path,
) -> None:
    """The SECOND candidate is the only one that may enable final output, so
    its refusal must leave the box on the first — the one this same validator
    already accepted — and stop nothing.

    The first candidate publishes the DAC and latency facts the graph render
    needs; the second derives the active lane from the converged graph. A
    refusal there means the final graph did not validate, so restarting
    anything against that lane is what a rejected candidate exists to prevent.
    """
    seen: list[str] = []

    def accept_then_refuse(**_kwargs: Any) -> tuple[bool, tuple[str, ...]]:
        seen.append("validate")
        if len(seen) == 1:
            return True, ("ok",)
        return False, ("the converged graph does not validate this lane",)

    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_outputd_env="JASPER_OUTPUTD_BACKEND=stale\n",
        patches={"jasper.audio_runtime_plan.validate_outputd_env": accept_then_refuse},
    )

    assert result.returncode == 78, result.stderr
    assert len(seen) == 2, seen
    # The PRELIMINARY candidate stands: committed by the first validation, and
    # not rolled back to the stale value the box was found with.
    _assert_states(
        _outputd_env(tmp_path),
        "JASPER_OUTPUTD_BACKEND=alsa",
        "JASPER_OUTPUTD_DAC_PCM=outputd_dac",
    )
    refusal = stderr_events(result.stderr, "audio_hardware_reconcile.runtime_graph")[-1]
    assert refusal["reason"] == "post_convergence_outputd_env_rejected"
    assert refusal["action"] == "preserve_preliminary_env"
    # The mixer pin this pass's own changed record earned still ran; nothing
    # else was stopped or restarted, and no candidate was left behind.
    transcript = _systemctl_log(tmp_path)
    assert "--no-block restart jasper-dac-init.service" in transcript
    _assert_omits(
        transcript,
        "stop jasper-voice.service",
        "--no-block restart jasper-outputd.service",
    )
    assert list(tmp_path.glob(".outputd.env.candidate.*")) == []


def test_the_cutover_render_precedes_convergence_which_precedes_the_unit_gate(
    tmp_path: Path,
) -> None:
    """The flat cutover graph is the artifact convergence SELECTS from, and the
    lane the gate acts on is derived from what convergence wrote. Reordering
    any pair converges against the previous topology's bytes or gates units on
    a lane no graph proved."""
    order: list[str] = []
    gate = reconcile_module.Pass.gate_role_services

    def recorded_gate(run: reconcile_module.Pass) -> None:
        order.append("gate")
        return gate(run)

    def recorded_render(**_kwargs: Any) -> SimpleNamespace:
        order.append("render_cutover")
        return SimpleNamespace(changed=True)

    def recorded_converge(**kwargs: Any) -> SimpleNamespace:
        order.append("converge")
        return _converged(**kwargs)

    with mock.patch.object(
        reconcile_module.Pass, "gate_role_services", recorded_gate
    ):
        result = _run_reconcile(
            tmp_path,
            APPLE_LISTING,
            "--reason",
            "test",
            converge=recorded_converge,
            patches={
                "jasper.sound.camilla_yaml.render_flat_cutover_configs": (
                    recorded_render
                )
            },
        )

    assert result.returncode == 0, result.stderr
    assert order == ["render_cutover", "converge", "gate"]


def test_runtime_convergence_only_writes_statefile(tmp_path: Path) -> None:
    """This hardware owner seeds the proved boot statefile; it never mutates a
    live CamillaDSP graph, which the web/coupling paths own."""
    result = _run_reconcile(tmp_path, APPLE_LISTING, "--reason", "test")

    assert result.returncode == 0, result.stderr
    (call,) = result.converge_calls
    assert call["write_statefile"] is True
    assert call["statefile_path"] == str(tmp_path / "outputd-statefile.yml")
    fields = stderr_event(result.stderr, "audio_hardware_reconcile.runtime_graph")
    assert fields["apply_mode"] == "write-statefile"
    assert fields["selected"] == SELECTED_CONFIG_PATH


# --- I2S HAT boot intent ------------------------------------------------------


_REAL_OBSERVE = output_hardware.observe


def _statusless_observation(*args: Any, **kwargs: Any) -> tuple[Any, Any, bool]:
    """A classifier answer that stopped stating the profile status."""
    state, cards, record_changed = _REAL_OBSERVE(*args, **kwargs)
    return dataclasses.replace(state, status=""), cards, record_changed


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
    assert stderr_event(first.stderr, "audio_hardware_reconcile.output_parked")["recognized"] == "0"

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

    intent.unlink()
    # The intent FILE is gone now, not present-and-empty: absent means the
    # reconciler touches NEITHER the managed I2S block NOR the reboot
    # marker, no matter what gets observed -- including a malformed or
    # failed observation (#i2s-hat-intent).
    for extra_env, patches in (
        ({"JASPER_OUTPUT_HARDWARE_STATE_PATH": str(tmp_path)}, None),
        (None, {"jasper.audio_hardware.reconcile.observe": _statusless_observation}),
        (None, None),
    ):
        for marker_present in (False, True):
            marker.unlink(missing_ok=True)
            if marker_present:
                marker.touch()
            observed = rerun(
                INNOMAKER_LISTING, extra_env=extra_env, patches=patches
            )
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
    assert stderr_event(parked.stderr, "audio_hardware_reconcile.output_parked")["recognized"] == "0"


def _not_durable_boot_config(**kwargs: Any):
    """A boot-config reconcile that published without a durable fsync.

    Stubbed rather than real because exit 74 needs a directory-fsync failure.
    """
    state, _changed, _hat, _profile, _durable, collision = _real_boot_config(**kwargs)
    return state, True, True, "innomaker_hifi_amp_pro", True, collision


_BOOT_CONFIG_TARGET = "jasper.audio_hardware.usb_port_role.reconcile_boot_config"


def test_published_not_durable_boot_change_still_sets_marker(tmp_path: Path):
    result = _run_reconcile(
        tmp_path, "", patches={_BOOT_CONFIG_TARGET: _not_durable_boot_config}
    )

    assert result.returncode == 74
    assert (tmp_path / "i2s-reboot").is_file()
    assert stderr_event(result.stderr, "audio_hardware_reconcile.i2s_hat_apply")["error"] == "boot_config_published_not_durable"


def test_boot_config_reconcile_failure_refuses_instead_of_proceeding(
    tmp_path: Path,
):
    """A boot-config reconcile that cannot answer reaches this reconciler's own
    refusal. 66 and not a partway abort: the boot config is preserved either
    way, but only 66 says so in the journal."""
    result = _run_reconcile(
        tmp_path,
        "",
        patches={_BOOT_CONFIG_TARGET: _raises(OSError("boot config unreadable"))},
    )

    assert result.returncode == 66, result.stderr
    assert stderr_event(result.stderr, "audio_hardware_reconcile.i2s_hat_apply") == {
        "pass_reason": "manual",
        "result": "error",
        "action": "preserve_boot_config",
    }


def test_record_change_with_i2s_apply_error_restarts_dac_init_before_exit(
    tmp_path: Path,
):
    """The first-ever pass always writes a changed record (an absent record
    reads as empty), so a same-pass I2S HAT apply error is enough to exercise
    the gap: the exit-74 early return sits between the record write and
    gate_role_services, so the pin restart the changed record earned has to
    fire at the exit site itself, not only from gate_role_services."""
    result = _run_reconcile(
        tmp_path, "", patches={_BOOT_CONFIG_TARGET: _not_durable_boot_config}
    )

    assert result.returncode == 74
    assert "--no-block restart jasper-dac-init.service" in _systemctl_log(tmp_path)


# --- identity: what the registry says reaches env, template and record --------


def test_the_pass_is_pinned_to_the_checkout_the_shim_ran_from(tmp_path: Path):
    """install.sh runs `--print-env` from the rsynced checkout BEFORE the venv
    is refreshed, so an unpinned spawn pairs the NEW shim with the PREVIOUS
    build's pass and every key that build never emitted reads as empty."""
    log = tmp_path / "pythonpath.log"
    fake = _script(
        tmp_path,
        "recording-python",
        'printf \'%s\\n\' "${PYTHONPATH:-}" >> "$JASPER_FAKE_PYTHONPATH_LOG"\n'
        'exec "$JASPER_FAKE_PYTHON_REAL" "$@"\n',
    )
    # An inherited PYTHONPATH that is NOT the checkout, so the pin below can
    # only be satisfied by the shim prepending its own tree.
    inherited = str(tmp_path / "inherited-site")
    result = _run_shim(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--print-env",
        extra_env={
            "JASPER_OUTPUT_HARDWARE_PYTHON": str(fake),
            "JASPER_FAKE_PYTHON_REAL": sys.executable,
            "JASPER_FAKE_PYTHONPATH_LOG": str(log),
            "PYTHONPATH": inherited,
        },
    )

    assert result.returncode == 0, result.stderr
    recorded = log.read_text(encoding="utf-8").splitlines()
    assert recorded, "the pass never ran"
    assert recorded == [os.pathsep.join((str(ROOT), inherited))] * len(recorded)
    assert "OUTPUT_DAC_ID=hifiberry_dac8x" in result.stdout


def test_a_failed_classification_leaves_every_observed_fact_at_its_absent_value(
    tmp_path: Path,
):
    """The classifier is the only source of hardware facts (ADR-0235 R2), so
    losing it loses all of them at once rather than half of them.

    The Apple control role is one of those facts now: with no record there is
    no card to name, and the mixer helpers stay off. The run still succeeds --
    install reads this and must not abort on a box whose classifier could not
    answer.
    """
    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--print-env",
        patches={"jasper.audio_hardware.reconcile.observe": _raises(OSError("no /proc"))},
    )

    assert result.returncode == 0, result.stderr
    assert stderr_event(
        result.stderr, "audio_hardware_reconcile.state_observed_failed"
    ) == {
        "pass_reason": "manual",
        "path": str(tmp_path / "output_hardware.json"),
    }
    _assert_states(
        result.stdout,
        "DONGLE_CARD=A",
        "APPLE_DONGLE_PRESENT=0",
        "APPLE_DONGLE_SERVICE_CARD=auto",
        "OUTPUT_DAC_ID=unknown",
        "OUTPUT_DAC_RECOGNIZED=0",
    )
    assert not (tmp_path / "output_hardware.json").exists()


#: The role values a box with no recognized output DAC answers with. Also what
#: the shim prints when the pass could not be reached at all, so the two are
#: pinned to one another rather than restated.
_PRINT_ENV_NO_DAC = {
    "DONGLE_CARD": "A",
    "APPLE_DONGLE_PRESENT": "0",
    "APPLE_DONGLE_SERVICE_CARD": "auto",
    "OUTPUT_DAC_CARD": "A",
    "OUTPUT_DAC_ID": "unknown",
    "OUTPUT_DAC_RECOGNIZED": "0",
}


def _parse_print_env(stdout: str) -> dict[str, str]:
    """Parse `--print-env`'s `KEY=value` lines, unquoting each value the way
    a `bash eval` of install.sh's consumer would (deploy/install.sh:893)."""
    parsed: dict[str, str] = {}
    for line in stdout.splitlines():
        key, _, raw_value = line.partition("=")
        tokens = shlex.split(raw_value)
        parsed[key] = tokens[0] if tokens else ""
    return parsed


@pytest.mark.parametrize(
    ("listing", "expected"),
    [
        pytest.param(
            DAC8X_AND_APPLE_LISTING,
            {
                "DONGLE_CARD": "A",
                "APPLE_DONGLE_PRESENT": "1",
                "APPLE_DONGLE_SERVICE_CARD": "auto",
                "OUTPUT_DAC_CARD": "sndrpihifiberry",
                "OUTPUT_DAC_ID": "hifiberry_dac8x",
                "OUTPUT_DAC_RECOGNIZED": "1",
            },
            id="recognized-dac8x-with-apple-control",
        ),
        pytest.param(
            APPLE_LISTING,
            {
                "DONGLE_CARD": "A",
                "APPLE_DONGLE_PRESENT": "1",
                "APPLE_DONGLE_SERVICE_CARD": "auto",
                "OUTPUT_DAC_CARD": "A",
                "OUTPUT_DAC_ID": "apple_usb_c_dongle",
                "OUTPUT_DAC_RECOGNIZED": "1",
            },
            id="apple-dongle-as-output",
        ),
        pytest.param("", _PRINT_ENV_NO_DAC, id="no-dac-unrecognized"),
        pytest.param(
            DAC8X_STUDIO_LISTING,
            {
                "DONGLE_CARD": "A",
                "APPLE_DONGLE_PRESENT": "0",
                "APPLE_DONGLE_SERVICE_CARD": "auto",
                "OUTPUT_DAC_CARD": "DAC8XStudio",
                "OUTPUT_DAC_ID": "hifiberry_dac8x_studio",
                "OUTPUT_DAC_RECOGNIZED": "1",
            },
            id="hifiberry-dac8x-studio",
        ),
    ],
)
def test_print_env_pins_the_install_contract(
    tmp_path: Path, listing: str, expected: dict[str, str]
):
    """`--print-env` is install.sh's contract with this script (install.sh:893
    evals it and exports every key; deploy/lib/install/systemd-units.sh:1331,
    1650 call it too). #4478 ports this script to Python -- pin the exact key
    set and values here so that port cannot silently change this surface."""
    result = _run_reconcile(tmp_path, listing, "--print-env")

    assert result.returncode == 0, result.stderr
    assert _parse_print_env(result.stdout) == expected
    assert result.stdout.count("\n") == len(expected)
    # `--print-env` promises no mutations, and install.sh evals it mid-install.
    assert not (tmp_path / "jasper.env").exists()
    assert not (tmp_path / "output_hardware.json").exists()


@pytest.mark.parametrize(
    ("listing", "dac_id", "dac_card", "dac_format", "apple_output"),
    [
        pytest.param(
            INNOMAKER_LISTING, "innomaker_hifi_amp_pro", "sndrpimerusamp",
            "S32_LE", False, id="innomaker",
        ),
        # The dongle's USB descriptor advertises S16_LE and S24_3LE; the packed
        # 24-bit edge is the widest it will install. It is also the only
        # profile declaring the mixer control the drift monitor re-pins.
        pytest.param(
            APPLE_LISTING, "apple_usb_c_dongle", "A", "S24_3LE", True, id="apple",
        ),
        # An Apple card is PRESENT here and still does not drive: the DAC8x
        # wins the role, and the monitor follows the profile that drives.
        pytest.param(
            DAC8X_AND_APPLE_LISTING, "hifiberry_dac8x", "sndrpihifiberry",
            "S32_LE", False, id="dac8x-with-apple-present",
        ),
        # The Studio driver writes no mixer defaults of its own, so its profile
        # declares pins — and the boot pin is enabled for it — while the
        # Apple-only drift monitor stays off.
        pytest.param(
            DAC8X_STUDIO_LISTING, "hifiberry_dac8x_studio", "DAC8XStudio",
            "S16_LE", False, id="dac8x-studio",
        ),
    ],
)
def test_reconcile_arms_each_recognized_single_dac_role(
    tmp_path: Path,
    listing: str,
    dac_id: str,
    dac_card: str,
    dac_format: str,
    apple_output: bool,
):
    """One recognized single DAC, end to end: identity into jasper.env, the
    declared edge into outputd.env, a raw hw alias into the template, and the
    unit gate that follows from the profile that DRIVES."""
    result = _run_reconcile(tmp_path, listing, "--reason", "test")

    assert result.returncode == 0, result.stderr
    _assert_states(
        _jasper_env(tmp_path),
        f"JASPER_AUDIO_DAC_ID={dac_id}",
        f"JASPER_AUDIO_DAC_CARD={dac_card}",
    )
    outputd_env = _outputd_env(tmp_path)
    _assert_states(
        outputd_env,
        "JASPER_OUTPUTD_SINK=single_alsa",
        f"JASPER_OUTPUTD_DAC_FORMAT={dac_format}",
        # No active baseline loaded => an ordinary stereo speaker, never the
        # wide active lane (fail-closed: the gate kept it stereo).
        "JASPER_OUTPUTD_ACTIVE_CHANNELS=\n",
        "JASPER_OUTPUTD_ACTIVE_LANE=\n",
    )
    runtime = stderr_events(result.stderr, "audio_hardware_reconcile.runtime_env")
    assert "single_alsa_active" not in {e["mode"] for e in runtime}
    # The declared edge, not a value invented here.
    assert final_edge_format_for(dac_id) == dac_format
    # The unit owns the TTS socket names; the reconciler is not a second writer.
    assert not (tmp_path / "tts.env").exists()
    template = _template(tmp_path)
    # No profile-scoped plug: every recognized single DAC renders a raw hw
    # alias, which is what outputd's format request lands on.
    assert "type plug" not in template
    _assert_states(template, "pcm.outputd_dac", "type hw", f"card {dac_card}")
    _assert_no_empty_alsa_card(template)
    assert _render_log(tmp_path) == "render\n"

    commands = _systemctl_log(tmp_path)
    # The pin is enabled on every box — a DAC that declares no mixer controls
    # is jasper-dac-init's own clean exit, not a unit to disable. It is
    # RESTARTED, because RemainAfterExit makes a `start` a no-op once the
    # oneshot has run and at boot it can run before the record it reads exists.
    _assert_states(
        commands,
        "enable jasper-dac-init.service",
        "--no-block restart jasper-dac-init.service",
        "stop jasper-voice.service",
        "reset-failed jasper-outputd.service",
        "--no-block restart jasper-outputd.service",
        "--no-block restart jasper-aec-reconcile.service",
    )
    if apple_output:
        # The monitor is ensured idempotently, never restarted: this gate runs
        # on every udev/reconcile pass and a deploy fires it repeatedly inside
        # the unit's StartLimitIntervalSec, so a restart-per-pass burns
        # StartLimitBurst and parks it 'start-limit-hit'.
        _assert_states(
            commands,
            "enable jasper-headphone-monitor.service",
            "reset-failed jasper-headphone-monitor.service",
            "start jasper-headphone-monitor.service",
        )
        _assert_omits(commands, "restart jasper-headphone-monitor.service")
    else:
        assert "disable --now jasper-headphone-monitor.service" in commands
        _assert_omits(commands, "enable jasper-headphone-monitor.service")


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
    commands = _systemctl_log(tmp_path).splitlines()
    assert "disable --now jasper-headphone-monitor.service" in commands
    assert "--no-block stop jasper-voice.service jasper-outputd.service" in commands
    assert "reset-failed jasper-voice.service jasper-outputd.service" in commands
    assert not any(
        "restart" in args
        and {"jasper-outputd.service", "jasper-aec-reconcile.service"} & set(args)
        for args in map(str.split, commands)
    )
    assert stderr_event(result.stderr, "audio_hardware_reconcile.output_parked")["recognized"] == "0"


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


def _lane_less_registry():
    """The InnoMaker profile rewritten to a lane-less clone, so the ``none``
    sentinel is pinned through the real resolver rather than a faked answer."""
    import dataclasses

    from jasper.audio_hardware import dac

    lane_less = dataclasses.replace(
        dac.INNOMAKER_HIFI_AMP_PRO,
        supports_active_outputd_lane=False,
        active_outputd_lane_channels=None,
    )
    return mock.patch.dict(dac._BY_ID, {lane_less.id: lane_less})


#: The lane-cap probe has TWO names: the dac module's, and the module-scope
#: from-import in jasper/audio_runtime_plan.py that the pass reaches lazily.
#: Patch both, and import that module ABOVE any patch — a first import taken
#: while the dac copy is a raising stub binds the stub for the whole session.
_LANE_CAP_TARGETS = (
    "jasper.audio_hardware.dac.active_outputd_lane_channels_for",
    f"{audio_runtime_plan.__name__}.active_outputd_lane_channels_for",
)


def _lane_cap(replacement: Any) -> dict[str, Any]:
    return dict.fromkeys(_LANE_CAP_TARGETS, replacement)


@pytest.mark.parametrize(
    ("patches", "lane_less", "expected"),
    [
        # Declaring the lane only means the width gate RUNS. Active mode still
        # needs a legal active graph to be the live CamillaDSP config, which
        # only commissioning produces. With no statefile staged the gate
        # declines and the box resolves byte-identically passive - and the
        # token names the gate's own decline, because the remedy is
        # commissioning, not "choose a different layout at /sound/setup/".
        pytest.param(None, False, "camilla_statefile_missing",
                     id="no-active-graph-staged"),
        # `active_lane_channels_for_dac` swallows its own failure, so a probe
        # that died yields an empty cap while the DAC is still RECOGNIZED. That
        # is TRANSIENT - reporting it as dac_no_active_lane would give a remedy
        # ("re-running cannot change it") that is false here.
        pytest.param(_lane_cap(_raises(RuntimeError("registry gone"))),
                     False, "lane_probe_failed",
                     id="lane-probe-died"),
        # The other side of the split: a profile that genuinely declares no
        # lane keeps the actionable token. That is its surviving population -
        # the next passive-only board the registry meets.
        pytest.param(None, True, "dac_no_active_lane",
                     id="lane-less-profile"),
    ],
)
def test_reconcile_names_why_it_stayed_passive_and_stays_passive(
    tmp_path: Path,
    patches: dict[str, Any] | None,
    lane_less: bool,
    expected: str,
):
    """THE FAIL-CLOSED ACTIVATION PROPERTY, and the reason it reports.

    Every arm resolves passive; only the named reason differs, and the three
    reasons carry different remedies so they may not collapse into one token.
    """
    with _lane_less_registry() if lane_less else contextlib.nullcontext():
        result = _run_reconcile(
            tmp_path, INNOMAKER_LISTING, "--reason", "test", patches=patches
        )

    assert result.returncode == 0, result.stderr
    runtime = stderr_events(result.stderr, "audio_hardware_reconcile.runtime_env")
    assert {e["active_graph"] for e in runtime} == {expected}
    outputd_env = _outputd_env(tmp_path)
    assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env
    assert "JASPER_OUTPUTD_CONTENT_PCM" not in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=\n" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_LANE=\n" in outputd_env


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


def test_outputd_env_stage_refused_hold_publishes_without_touching_foreign_candidate(
    tmp_path: Path,
) -> None:
    """A refused hold permits publication but cannot sweep another holder's stage."""
    outputd_env = tmp_path / "outputd.env"
    outputd_env.write_text("JASPER_OUTPUTD_CONTENT_PCM=stale\n", encoding="utf-8")
    foreign_candidate = tmp_path / ".outputd.env.candidate.live"
    foreign_content = "JASPER_OUTPUTD_BACKEND=inflight\n"
    foreign_candidate.write_text(foreign_content, encoding="utf-8")

    # Held for the whole reconciler run — spawn_lock_holder's __exit__ kills
    # the holder instead of waiting hold_seconds out — so it always outlasts
    # both stages' `flock -w 10`, no matter how loaded the box is.
    with spawn_lock_holder(outputd_env, hold_seconds=300):
        result = _run_reconcile(tmp_path, APPLE_LISTING, "--reason", "test")

    assert result.returncode == 0, result.stderr
    unheld = stderr_events(
        result.stderr, "audio_hardware_reconcile.outputd_env_stage_unlocked"
    )
    assert unheld, result.stderr
    assert {fields["reason"] for fields in unheld} == {"stage_lock_unheld"}
    assert _outputd_env_key_present(_outputd_env(tmp_path), "JASPER_OUTPUTD_BACKEND")
    assert _stage_candidate_debris(tmp_path) == [foreign_candidate.name]
    assert foreign_candidate.read_text(encoding="utf-8") == foreign_content


def test_outputd_env_stage_sweeps_debris_from_an_earlier_pass(
    tmp_path: Path,
) -> None:
    """A pass SIGKILLed mid-stage leaves debris no trap could have swept."""
    stale_candidate = tmp_path / ".outputd.env.candidate.aaaaaa"
    stale_lock = tmp_path / "..outputd.env.candidate.aaaaaa.lock"
    stale_candidate.write_text("JASPER_OUTPUTD_BACKEND=stale\n", encoding="utf-8")
    stale_lock.write_text("", encoding="utf-8")

    result = _run_reconcile(tmp_path, APPLE_LISTING, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert _stage_candidate_debris(tmp_path) == []


def _event_names(stderr: str) -> list[str]:
    """Event names in emission order, so the terminal one is the last."""
    parsed = (parse_event(line) for line in stderr.splitlines())
    return [found[0] for found in parsed if found is not None]


@pytest.mark.parametrize(
    ("mode", "expected_rc"),
    [("success", 0), ("early_refusal", 66), ("mid_stage_abort", 1)],
)
def test_every_pass_ends_with_one_exit_event_carrying_its_own_status(
    tmp_path: Path, mode: str, expected_rc: int
) -> None:
    """Every pass ends with one `exit` line naming the code it exits with.

    One teardown runs the outputd.env stage cleanup and publishes that line —
    and neither may move the code systemd sees.
    """
    extra: dict[str, str] = {}
    patches: dict[str, Any] | None = None
    read_only = tmp_path / "read-only"
    if mode == "early_refusal":
        patches = {_BOOT_CONFIG_TARGET: _raises(OSError("boot config unreadable"))}
    elif mode == "mid_stage_abort":
        # A jasper.env this pass cannot lock aborts it between
        # stage_outputd_env and the commit — the one window in which the
        # teardown's cleanup arm, not finish_outputd_env_stage, ends the stage.
        read_only.mkdir()
        (read_only / "jasper.env").write_text(
            "JASPER_AUDIO_DAC_ID=stale\n", encoding="utf-8"
        )
        read_only.chmod(0o555)
        extra["JASPER_ENV_FILE"] = str(read_only / "jasper.env")
    try:
        result = _run_reconcile(
            tmp_path, APPLE_LISTING, "--reason", "test", extra_env=extra,
            patches=patches,
        )
    finally:
        if read_only.is_dir():
            read_only.chmod(0o755)

    assert result.returncode == expected_rc, result.stderr
    names = _event_names(result.stderr)
    assert names[-1] == "audio_hardware_reconcile.exit", result.stderr
    assert stderr_event(result.stderr, "audio_hardware_reconcile.exit") == {
        "pass_reason": "test",
        "signal": "none",
        "status": str(expected_rc),
    }
    # The nine result fields exist only where the pass got that far, so
    # `complete` stays the success-only line the terminal one closes over.
    assert ("audio_hardware_reconcile.complete" in names) == (mode == "success")
    assert _stage_candidate_debris(tmp_path) == []


def test_help_publishes_no_event_because_reading_usage_is_not_a_pass(
    tmp_path: Path,
) -> None:
    """Every other exit reports itself; an operator reading usage must not.

    One usage, both halves: the shim documents its own `--changed` and hands
    the rest to the pass's own parser (the interpreter ban is
    `ExecCondition=`'s alone, and `--help` is not that).
    """
    result = _run_shim(tmp_path, "", "--help")

    assert result.returncode == 0, result.stderr
    assert "--changed" in result.stdout
    assert "--print-env" in result.stdout
    assert "--no-restart" in result.stdout
    assert _event_names(result.stderr) == [], result.stderr


def _failing_interpreter(tmp_path: Path, rc: int) -> dict[str, str]:
    """An interpreter that cannot start: 127 absent, 126 not executable."""
    return {
        "JASPER_OUTPUT_HARDWARE_PYTHON": str(
            _script(tmp_path, f"unstartable-{rc}", f"exit {rc}\n")
        )
    }


@pytest.mark.parametrize("rc", [126, 127], ids=["not-executable", "not-found"])
def test_print_env_degrades_to_the_no_dac_row_when_the_pass_cannot_start(
    tmp_path: Path, rc: int
) -> None:
    """install.sh evals this output and reads every key under `set -u`
    (deploy/install.sh:893), so an unstartable interpreter must degrade to the
    unrecognized-DAC answer with rc 0 rather than abort the install on an
    empty eval. Only 126/127 mean "no pass ran"; see the sibling below."""
    result = _run_shim(
        tmp_path, "", "--print-env", extra_env=_failing_interpreter(tmp_path, rc)
    )

    assert result.returncode == 0, result.stderr
    assert _parse_print_env(result.stdout) == _PRINT_ENV_NO_DAC


def test_the_shim_propagates_a_verdict_the_pass_itself_reached(tmp_path: Path) -> None:
    """An unreadable shared render library is the pass's OWN 66, not a missing
    interpreter: it must fail the install rather than answer it with defaults.

    The pass runs that check ahead of --print-env, as the shell reconciler did
    -- install.sh runs this verb from the tree it is about to install from, so
    a broken library there has to be loud.
    """
    result = _run_shim(
        tmp_path,
        "",
        "--print-env",
        extra_env={"JASPER_ASOUND_RENDER_LIB": str(tmp_path / "absent-render-lib.sh")},
    )

    assert result.returncode == 66, result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(("--changed", "--resaon", "boot"), id="misspelled-flag"),
        pytest.param(("--changed", "--reason"), id="reason-without-a-value"),
    ],
)
def test_the_shim_rejects_bad_arguments_before_the_changed_predicate(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    """--changed answers and exits before the Python parser would ever see the
    argv, so a typo would otherwise be swallowed into a needless full pass (or
    a pass whose reason is the next flag)."""
    result = _run_shim(tmp_path, "", *args)

    assert result.returncode == 2, result.stdout


def test_reading_usage_survives_an_interpreter_that_cannot_start(
    tmp_path: Path,
) -> None:
    """`set -euo pipefail` plus a failing interpreter would kill the shell
    before the case arm's own exit 0, so the shim states every flag itself."""
    result = _run_shim(
        tmp_path, "", "--help", extra_env=_failing_interpreter(tmp_path, 127)
    )

    assert result.returncode == 0, result.stderr
    _assert_states(result.stdout, "--changed", "--print-env", "--no-restart")


def _signal_self(signum: int):
    """A probe that takes the signal systemd sends at TimeoutStartSec."""

    def _probe(*_args: Any, **_kwargs: Any):
        os.kill(os.getpid(), signum)
        raise AssertionError("the signal did not interrupt the pass")

    return _probe


def test_a_signal_between_render_and_publish_leaves_no_template_debris(
    tmp_path: Path,
) -> None:
    """/etc/jasper is not tmpfs, so a candidate leaked here survives reboots
    and accumulates one file per killed pass — nothing else sweeps this prefix
    the way the outputd.env stage sweeps its own."""
    template = tmp_path / "asoundrc.jasper.template"
    real_replace = os.replace

    def _signal_at_the_template_publish(src, dst, *args: Any, **kwargs: Any):
        if str(dst) == str(template):
            os.kill(os.getpid(), signal.SIGTERM)
            raise AssertionError("the signal did not interrupt the pass")
        return real_replace(src, dst, *args, **kwargs)

    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        patches={
            "jasper.audio_hardware.reconcile.os.replace": (
                _signal_at_the_template_publish
            )
        },
    )

    assert result.returncode == 143, result.stderr
    assert list(tmp_path.glob("asoundrc.jasper.template.*")) == []


@pytest.mark.parametrize(
    ("signum", "name", "status"),
    [
        (signal.SIGTERM, "TERM", 143),
        (signal.SIGHUP, "HUP", 129),
        (signal.SIGINT, "INT", 130),
    ],
)
def test_a_signalled_pass_names_the_signal_and_exits_128_plus_it(
    tmp_path: Path, signum: int, name: str, status: int
) -> None:
    """A pass systemd kills at TimeoutStartSec reports 143, not 0, and the
    terminal line names the signal — the only place the cause is recorded."""
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        patches={"jasper.audio_hardware.reconcile.observe": _signal_self(signum)},
    )

    assert result.returncode == status, result.stderr
    assert _event_names(result.stderr)[-1] == "audio_hardware_reconcile.exit"
    assert stderr_event(result.stderr, "audio_hardware_reconcile.exit") == {
        "pass_reason": "test",
        "signal": name,
        "status": str(status),
    }


def test_the_shim_maps_a_signal_to_the_status_systemd_expects(
    tmp_path: Path,
) -> None:
    """The shim waits on the pass, so it must publish the mapped code rather
    than die of the signal itself and report `killed`."""
    marker = tmp_path / "pass-started"
    sleeper = tmp_path / "sleeper"
    sleeper.write_text(f'#!/bin/sh\n: > "{marker}"\nsleep 30\n', encoding="utf-8")
    sleeper.chmod(0o755)
    process = subprocess.Popen(
        ["bash", str(SCRIPT), "--reason", "test"],
        cwd=ROOT,
        env=_reconcile_env(
            tmp_path,
            APPLE_LISTING,
            extra_env={"JASPER_OUTPUT_HARDWARE_PYTHON": str(sleeper)},
        ),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Its own group, so the signal reaches the shim AND the pass it is
        # waiting on — systemd's default KillMode=control-group.
        start_new_session=True,
    )
    deadline = time.monotonic() + 60
    while not marker.exists():
        assert process.poll() is None, "the shim ended before it was signalled"
        assert time.monotonic() < deadline, "the shim never started the pass"
        time.sleep(0.02)
    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    _, stderr = process.communicate(timeout=180)

    assert process.returncode == 143, stderr


def test_a_signalled_changed_predicate_exits_rather_than_dying_of_the_signal(
    tmp_path: Path,
) -> None:
    """`--changed` is the unit's ExecCondition=, where exit 1..254 skips the
    unit and death BY SIGNAL fails it. A shell's own `$?` is 143 either way,
    so this reads the wait status (Popen reports -15 for the signal death): a
    SIGTERM landing in the fingerprint must leave the unit skippable."""
    # A FIFO with no writer: the fingerprint's `cat` blocks on it, which is the
    # window a TimeoutStartSec SIGTERM lands in.
    blocking = tmp_path / "blocking-topology"
    os.mkfifo(blocking)
    process = subprocess.Popen(
        ["bash", str(SCRIPT), "--reason", "test", "--changed"],
        cwd=ROOT,
        env=_reconcile_env(
            tmp_path,
            APPLE_LISTING,
            extra_env={"JASPER_OUTPUT_TOPOLOGY_PATH": str(blocking)},
        ),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    time.sleep(2)
    assert process.poll() is None, "the predicate never reached the blocking read"
    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    _, stderr = process.communicate(timeout=180)

    assert process.returncode == 143, (process.returncode, stderr)


@pytest.mark.parametrize("blocked", [False, True])
def test_state_written_carries_the_classifier_blocker_codes(
    tmp_path: Path, blocked: bool
) -> None:
    """The codes behind a park reach the journal on the pass's own line.

    The reconciler runs the classifier with its stderr on `/dev/null`, so this
    line is the only place a pass publishes its verdict.
    """
    extra: dict[str, str] = {}
    expected = "none"
    if blocked:
        # One child of a saved dual-Apple pair is absent, so the record is
        # partial and names which half is missing.
        extra = {
            **_dual_apple_cards(tmp_path, ((1, "A", "left"),)),
            "JASPER_OUTPUT_TOPOLOGY_PATH": str(
                _dual_apple_topology(tmp_path, active=True)
            ),
            **_active_graph_env(tmp_path, write_topology=False),
        }
        expected = "saved_composite_partially_present"

    result = _run_reconcile(
        tmp_path, APPLE_LISTING, "--reason", "test", extra_env=extra
    )

    assert result.returncode == 0, result.stderr
    written = stderr_event(result.stderr, "audio_hardware_reconcile.state_written")
    assert written["blockers"] == expected


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
    assert "JASPER_OUTPUTD_DUAL_DAC_A_PCM=\n" in outputd_env
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
    assert ("single_alsa_active", str(channels), str(cap)) in {
        (e["mode"], e.get("active_channels"), e.get("active_lane_cap"))
        for e in stderr_events(result.stderr, "audio_hardware_reconcile.runtime_env")
    }


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
    assert ("single_alsa_active", "2", "8", "jts_ring_active_playback") in {
        (e["mode"], e.get("active_channels"), e.get("active_lane_cap"), e.get("active_endpoint"))
        for e in stderr_events(result.stderr, "audio_hardware_reconcile.runtime_env")
    }


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
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=\n" in outputd_env
    assert "JASPER_OUTPUTD_ACTIVE_LANE=\n" in outputd_env
    runtime = stderr_events(result.stderr, "audio_hardware_reconcile.runtime_env")
    assert "single_alsa_active" not in {e["mode"] for e in runtime}
    passive = [
        found
        for found in stderr_events(result.stderr, "audio_hardware_reconcile.runtime_env")
        if found["mode"] == "single_alsa"
    ]
    assert {found["active_graph"] for found in passive} == {reason}


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
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=\n" in outputd_env
    runtime = stderr_events(result.stderr, "audio_hardware_reconcile.runtime_env")
    assert "single_alsa_active" not in {e["mode"] for e in runtime}
    assert {e["active_graph"] for e in stderr_events(result.stderr, "audio_hardware_reconcile.runtime_env")} == {
        "active_graph_unsafe:active_graph_output_count_mismatch"
    }


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
    assert not {"audio_hardware_reconcile.output_dac_route", "audio_hardware_reconcile.route_ignored"} & set(_event_names(result.stderr))
    complete = stderr_event(result.stderr, "audio_hardware_reconcile.complete")
    assert (complete["outputd_active_mode"], complete["outputd_active_channels"]) == ("1", "2")


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
    assert "JASPER_AUDIO_DAC_CARD=\n" in env_text
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
    assert stderr_event(result.stderr, "audio_hardware_reconcile.dual_apple_detected")["status"] == "ready"
    assert stderr_event(result.stderr, "hardware.usb_role_resolved") == {
        "topology": "separate_host_ports", "desired": "peripheral",
        "active": "peripheral", "gadget_available": "true",
        "management_transport_available": "true", "reason": "available",
    }
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
    assert "JASPER_OUTPUTD_SINK=composite" in outputd_env
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
    assert "JASPER_OUTPUTD_ACTIVE_CHANNELS=\n" in outputd_env
    # The lane PAIR is staged, because the accepted graph names the ACTIVE
    # RING. The two markers are one fact and outputd bails at startup on an
    # incoherent pair, so both are asserted.
    assert "JASPER_OUTPUTD_ACTIVE_LANE=1" in outputd_env
    assert "JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT=1" in outputd_env
    _assert_parked_outputd_dac_template(_template(tmp_path))
    assert stderr_event(result.stderr, "audio_hardware_reconcile.dual_apple_detected")["order_source"] == "saved_topology"


def test_a_composite_whose_accepted_graph_names_no_endpoint_clears_the_pair(
    tmp_path: Path,
):
    """FAIL-CLOSED, at the one arm the legal-endpoint set makes unreachable
    today: an accepted decision naming NO endpoint device must leave the lane
    PAIR clear rather than arm the marker off the acceptance alone.

    The pair is one fact with two consumers and outputd bails at startup on the
    incoherent half-set, so this is what stands between "the legal set grew"
    and "every composite arms the ring marker".
    """
    from jasper.active_speaker.runtime_contract import OutputdActiveLaneDecision

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
        patches={
            "jasper.active_speaker.runtime_contract.outputd_active_lane_decision":
                lambda *_a, **_k: OutputdActiveLaneDecision(
                    ok=True, width=4, reason="accepted", endpoint_device=None
                ),
            # The staged validator refuses this pair against a live ring graph,
            # which is its own job and its own pin. Out of the frame here so the
            # candidate the WRITER produced reaches disk to be read back.
            "jasper.audio_runtime_plan.validate_outputd_env":
                lambda **_kwargs: (True, ()),
        },
    )

    assert result.returncode == 0, result.stderr
    outputd_env = _outputd_env(tmp_path)
    # Armed as a composite — and still holding NEITHER half of the pair.
    assert "JASPER_OUTPUTD_SINK=dual_apple" in outputd_env
    _assert_states(
        outputd_env,
        "JASPER_OUTPUTD_ACTIVE_LANE=\n",
        "JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT=\n",
    )
    assert (
        stderr_event(result.stderr, "audio_hardware_reconcile.dual_apple_detected")[
            "active_endpoint"
        ]
        == "none"
    )


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
    assert "JASPER_OUTPUTD_DAC_FORMAT=\n" in outputd_env
    _assert_parked_outputd_dac_template(_template(tmp_path))
    commands = _systemctl_log(tmp_path)
    assert "--no-block stop jasper-voice.service jasper-outputd.service" in commands
    assert "--no-block restart jasper-outputd.service" not in commands
    runtime = stderr_events(result.stderr, "audio_hardware_reconcile.runtime_env")
    assert {(e["pass_reason"], e["mode"]) for e in runtime} == {("test", "parked")}
    assert stderr_event(result.stderr, "audio_hardware_reconcile.output_parked") == {
        "pass_reason": "test", "output_dac_id": "unknown", "output_dac_card": "A",
        "recognized": "0", "observed_blockers": "saved_composite_partially_present",
    }
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
    assert "JASPER_OUTPUTD_SINK=composite" in outputd_env
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
        json.dumps(
            _topology_payload(
                topology_id="solo",
                name="Solo",
                hardware={
                    "device_id": "apple_usb_c_dongle",
                    "device_label": "Apple USB-C audio adapter",
                    "physical_output_count": 2,
                    "card_id": "A",
                    "outputs": [],
                },
            )
        ),
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
    assert "JASPER_AUDIO_DAC_CARD=\n" in env_text
    outputd_env = _outputd_env(tmp_path)
    assert "JASPER_OUTPUTD_BACKEND=fake" in outputd_env
    assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env
    assert "JASPER_OUTPUTD_CONTENT_PCM" not in outputd_env
    assert "JASPER_OUTPUTD_DUAL_DAC_A_PCM=\n" in outputd_env
    # Parked/unrecognized: no profile to query, so the declared format clears
    # too — explicit empty, matching how ACTIVE_CHANNELS/ACTIVE_LANE clear in
    # this same branch. (A LOST probe is a different branch; see the
    # dac_format_skip tests.)
    assert "JASPER_OUTPUTD_DAC_FORMAT=\n" in outputd_env
    assert (
        _output_hardware_record(tmp_path)["profile_id"] == "dual_apple_usb_c_dac_4ch"
    )
    detected = stderr_event(result.stderr, "audio_hardware_reconcile.dual_apple_detected")
    assert (detected["action"], detected["reason"]) == ("park_until_active_graph", "camilla_statefile_missing")
    _assert_parked_outputd_dac_template(_template(tmp_path))
    assert _render_log(tmp_path) == "render\n"
    assert (
        "--no-block stop jasper-voice.service jasper-outputd.service"
        in _systemctl_log(tmp_path)
    )


def test_dual_apple_park_names_an_unavailable_active_graph_contract(tmp_path: Path):
    """The gate answers a reason on every path it declines on, including one an
    exception raised inside the contract. The park line has to name it."""
    result = _run_reconcile(
        tmp_path,
        DUAL_APPLE_LISTING,
        "--reason",
        "test",
        extra_env={
            **_dual_apple_cards(tmp_path),
            "JASPER_OUTPUT_TOPOLOGY_PATH": str(_dual_apple_topology(tmp_path)),
        },
        patches={
            "jasper.active_speaker.runtime_contract.outputd_active_lane_decision": (
                _raises(RuntimeError("contract module unusable"))
            )
        },
    )

    assert result.returncode == 0, result.stderr
    assert stderr_event(
        result.stderr, "audio_hardware_reconcile.dual_apple_detected"
    ) == {
        "pass_reason": "test",
        "status": "ready",
        "action": "park_until_active_graph",
        "reason": "active_graph_contract_unavailable:RuntimeError:RuntimeError",
    }


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


# The endpoint contract resolves outputd's capture half; an unregistered
# playback PCM answers None, which is what fails the contract.
_ENDPOINT_CONTRACT_FAILS = {
    "jasper.camilla_config_contract.outputd_capture_device_for_playback": (
        lambda *_args, **_kwargs: None
    )
}


def _assert_contract_really_failed(result: _Pass) -> None:
    """Positive control: the injected failure reached the path under test.

    Without this an assertion about the fallback could pass on a run that
    never took the fallback at all.
    """
    assert result.returncode == 66, result.stderr
    assert stderr_event(result.stderr, "audio_hardware_reconcile.outputd_endpoint_contract_failed")


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
        extra_env=_dual_apple_cards(tmp_path),
        patches=_ENDPOINT_CONTRACT_FAILS,
    )

    _assert_contract_really_failed(result)
    assert stderr_event(result.stderr, "audio_hardware_reconcile.outputd_endpoint_contract_failed")["action"] == "park_backend_fake"
    assert stderr_event(result.stderr, "audio_hardware_reconcile.outputd_env_clockless_park")["backend"] == "alsa->fake"
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
        tmp_path, "", "--reason", "test", patches=_ENDPOINT_CONTRACT_FAILS
    )

    _assert_contract_really_failed(second)
    assert stderr_event(second.stderr, "audio_hardware_reconcile.outputd_endpoint_contract_failed")["action"] == "preserve_runtime_env"
    assert not stderr_events(second.stderr, "audio_hardware_reconcile.outputd_env_clockless_park")
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
            False, True, "audio_hardware_reconcile.state_written_failed",
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
    patches = dict(_ENDPOINT_CONTRACT_FAILS)
    if observation_fails:
        patches["jasper.audio_hardware.reconcile.observe"] = _raises(OSError("no cards"))

    result = _run_reconcile(
        tmp_path,
        listing,
        "--reason",
        "test",
        initial_env=operator_env,
        initial_template=template,
        initial_outputd_env=preserved_env,
        extra_env=extra_env,
        patches=patches,
    )

    _assert_contract_really_failed(result)
    if positive_event is not None:
        assert stderr_event(result.stderr, positive_event)
    assert stderr_event(result.stderr, "audio_hardware_reconcile.outputd_endpoint_contract_failed")["action"] == "preserve_runtime_env"
    assert not stderr_events(result.stderr, "audio_hardware_reconcile.outputd_env_clockless_park")
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
    complete = stderr_event(result.stderr, "audio_hardware_reconcile.complete")
    assert (complete["env_changed"], complete["render_changed"]) == ("0", "0")
    assert _render_log(tmp_path) == ""
    commands = _systemctl_log(tmp_path).splitlines()
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
    commands = _systemctl_log(tmp_path).splitlines()
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
    assert stderr_event(result.stderr, "audio_hardware_reconcile.outputd_only_restarted")
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
    assert stderr_event(result.stderr, "audio_hardware_reconcile.audio_restarted")["brain_restarted"] == str(int(brain))


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
    assert stderr_event(result.stderr, "audio_hardware_reconcile.audio_restarted")
    assert not stderr_events(result.stderr, "audio_hardware_reconcile.outputd_only_restarted")


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
    assert stderr_event(result.stderr, "audio_hardware_reconcile.route_runtime_restarted")["fanin_restarted"] == "1"
    assert "stop jasper-voice.service" not in commands
    assert "restart jasper-aec-reconcile.service" not in commands
    assert "--no-block restart jasper-outputd.service" not in commands
    # The recognized-but-nothing-committed arm still ensures outputd is up.
    assert "--no-block start jasper-outputd.service" in commands
    assert not stderr_events(result.stderr, "audio_hardware_reconcile.outputd_only_restarted")


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
    assert stderr_event(first.stderr, "audio_hardware_reconcile.route_runtime_restarted")["fanin_restarted"] == "1"

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
    assert stderr_event(second.stderr, "audio_hardware_reconcile.route_runtime_restarted")["fanin_restarted"] == "0"


# --- the asound render is never allowed to clobber live ALSA ------------------


def _stub_render_lib(tmp_path: Path, body: str) -> Path:
    """A drop-in jasper-asound-render.sh with an overridable template
    renderer. Sources the real lib first (keeping jasper_asound_log_token
    et al. intact), so a test can drive the production failure shape: a
    card-less recognized DAC fails closed (require_output_dac_card -> 64)
    BEFORE the renderer opens the dest.
    """
    real = ROOT / "deploy" / "lib" / "jasper-asound-render.sh"
    return _script(
        tmp_path,
        "stub-asound-render.sh",
        f"source {real}\njasper_asound_render_template() {{\n{body}\n}}\n",
    )


def test_the_render_lib_resolves_the_checkout_sibling_before_the_installed_copy(
    tmp_path: Path,
) -> None:
    """install.sh runs a --print-env pass from the rsynced checkout BEFORE
    /usr/local/lib is refreshed, so installed-first would pair new code with a
    stale library."""
    with mock.patch.dict(os.environ, {}, clear=True):
        assert reconcile_module._resolve_asound_render_lib() == str(
            ROOT / "deploy" / "lib" / "jasper-asound-render.sh"
        )
    override = tmp_path / "override-render.sh"
    override.write_text("", encoding="utf-8")
    with mock.patch.dict(os.environ, {"JASPER_ASOUND_RENDER_LIB": str(override)}):
        assert reconcile_module._resolve_asound_render_lib() == str(override)


def test_print_env_arms_a_ready_dual_apple_composite(tmp_path: Path):
    """--print-env resolves the same composite verdict a full pass does, so
    install.sh's role variables name the armed pair rather than the park."""
    topology_path = _dual_apple_topology(tmp_path, active=True)

    result = _run_reconcile(
        tmp_path,
        DUAL_APPLE_LISTING,
        "--print-env",
        extra_env={
            **_dual_apple_cards(tmp_path),
            "JASPER_OUTPUT_TOPOLOGY_PATH": str(topology_path),
            **_active_graph_env(tmp_path, write_topology=False),
        },
    )

    assert result.returncode == 0, result.stderr
    assert "OUTPUT_DAC_ID=dual_apple_usb_c_dac_4ch" in result.stdout
    fields = stderr_event(
        result.stderr, "audio_hardware_reconcile.dual_apple_detected"
    )
    assert fields["action"] == "outputd_dual_sink"
    assert fields["dac_a_pcm"] == _log_token("hw:CARD=A,DEV=0")


@pytest.mark.parametrize(
    ("stub_body", "good", "reason"),
    [
        pytest.param(
            "    return 64",
            "GOOD LIVE ALSA CONFIG — must survive a render failure\n",
            "render-fail",
            id="renderer-fails-before-writing",
        ),
        pytest.param(
            '    : > "$2"\n    return 0',
            "GOOD LIVE ALSA CONFIG — survives an empty render\n",
            "render-empty",
            id="renderer-returns-an-empty-file",
        ),
    ],
)
def test_failed_or_empty_render_preserves_the_live_template(
    tmp_path: Path,
    stub_body: str,
    good: str,
    reason: str,
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
    assert stderr_event(result.stderr, "audio_hardware_reconcile.asound_render_failed")["preserved_existing"] == "1"
    assert not stderr_events(result.stderr, "audio_hardware_reconcile.asound_rendered")
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
    assert stderr_event(result.stderr, "audio_hardware_reconcile.asound_rendered")
    assert not stderr_events(result.stderr, "audio_hardware_reconcile.asound_render_failed")
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


def test_an_unspawnable_asound_renderer_refuses_the_same_way_a_failing_one_does(
    tmp_path: Path,
) -> None:
    """A renderer that cannot be SPAWNED is the shell's 127 — the same refusal.

    Uncaught, the OSError escaped main() (which handles only _Abort and
    SystemExit), so the mixer pin this pass's own changed record earned never
    ran and the render template was left behind in /etc/jasper.
    """
    live_conf = tmp_path / "asound.conf"
    live_conf.write_bytes(b"GOOD LIVE ASOUND.CONF\n")

    result = _run_reconcile(
        tmp_path,
        DAC8X_AND_APPLE_LISTING,
        "--reason",
        "test",
        initial_template="GOOD LIVE TEMPLATE\n",
        extra_env={"JASPER_RENDER_ASOUND_CONF": str(tmp_path / "not-installed")},
    )

    assert result.returncode == 78, result.stderr
    assert live_conf.read_bytes() == b"GOOD LIVE ASOUND.CONF\n"
    assert _template(tmp_path) == "GOOD LIVE TEMPLATE\n"
    assert list(tmp_path.glob("asoundrc.jasper.template.*")) == []
    assert (
        "--no-block restart jasper-dac-init.service" in _systemctl_log(tmp_path)
    ), _systemctl_log(tmp_path)
    _assert_omits(
        _systemctl_log(tmp_path),
        "--no-block restart jasper-outputd.service",
        "stop jasper-voice.service",
    )


# --- the per-DAC latency floor emit -------------------------------------------

_FLOOR_KEYS = (
    ("JASPER_CAMILLA_CHUNKSIZE", "256"),
    ("JASPER_CAMILLA_TARGET_LEVEL", "1536"),
    ("JASPER_OUTPUTD_PERIOD_FRAMES", "128"),
    ("JASPER_OUTPUTD_DAC_BUFFER_FRAMES", "256"),
)

_FLOOR_PLAN_PROBE_FAILS = {
    "jasper.audio_runtime_plan.outputd_floor_plan": _raises(RuntimeError("gone"))
}


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
    floor = stderr_event(result.stderr, "audio_hardware_reconcile.latency_floor")
    assert {
        "pass_reason": "test", "output_dac_id": dac_id,
        "camilla_chunksize": "256", "camilla_target_level": "1536",
        "outputd_period_frames": "128", "outputd_dac_buffer_frames": "256",
    }.items() <= floor.items()


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


def test_reconcile_preserves_the_floor_when_the_plan_probe_cannot_answer(
    tmp_path: Path,
):
    """A floor plan that could not be built leaves the four keys ALONE, the
    way the DAC-format and content-format probes do. Clearing them would drop
    a tuned box to outputd's packaged defaults with nothing loud anywhere; the
    stale floor plus the degraded marker is the loud option, and the marker is
    what stops the shim stamping a state ``--changed`` could skip against."""
    stale = {
        "JASPER_CAMILLA_CHUNKSIZE": "512",
        "JASPER_CAMILLA_TARGET_LEVEL": "2048",
        "JASPER_OUTPUTD_PERIOD_FRAMES": "512",
        "JASPER_OUTPUTD_DAC_BUFFER_FRAMES": "1024",
    }
    state_path = tmp_path / "output_hardware.json"
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_outputd_env="".join(f"{k}={v}\n" for k, v in stale.items()),
        extra_env={"JASPER_OUTPUT_HARDWARE_STATE_PATH": str(state_path)},
        patches=_FLOOR_PLAN_PROBE_FAILS,
    )

    assert result.returncode == 0, result.stderr
    outputd_env = _outputd_env(tmp_path)
    for key, value in stale.items():
        assert f"{key}={value}" in outputd_env, (key, outputd_env)
    assert "event=audio_hardware_reconcile.latency_floor_skip" in result.stderr
    assert "reason=probe_unavailable" in result.stderr
    assert (state_path.parent / "reconcile.degraded").is_file()


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
    invalid = stderr_event(result.stderr, "audio_hardware_reconcile.outputd_env_invalid")
    if detail is not None:
        assert detail in invalid["detail"]
    assert invalid["preserved"] == "1"
    assert invalid["outputd_env"] == str(tmp_path / "outputd.env")
    assert "override_store" in invalid["detail"]
    assert _log_token(str(tmp_path / "outputd.env")) in invalid["detail"]
    assert "outputd.env.candidate" not in invalid["detail"]
    rejected = stderr_event(result.stderr, "audio_hardware_reconcile.outputd_candidate_rejected")
    assert (rejected["action"], rejected["services"]) == ("preserve_runtime_env", "unchanged")
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


def test_the_note_prefix_the_reconciler_matches_is_the_one_the_validator_emits(
    tmp_path: Path
) -> None:
    """The waypoint-note seam, pinned from BOTH sides.

    `validate_outputd_env_stage` recognises a coherent-but-transient result by
    the literal prefix the validator reports on the accepted path. Nothing else
    couples them, so a reworded report would silently stop the reconciler
    logging `outputd_env_note`.
    """
    from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE
    from tests.test_ring_active_endpoint import (
        _active_topology,
        _emit_active_baseline,
        _mono_two_way_preset,
        _run_validate_outputd_env,
    )

    # A REAL emitted graph, not a hand-written stanza: the validator demotes
    # any active-endpoint graph failing `outputd_active_lane_decision` to
    # devices=None, and a stub stanza fails it — reporting a bare "ok" and
    # making this contract vacuous.
    rc, out = _run_validate_outputd_env(
        tmp_path,
        graph_yaml=_emit_active_baseline(
            _mono_two_way_preset(), RING_ACTIVE_PLAYBACK_DEVICE
        ),
        topology=_active_topology("mono", "active_2_way"),
        coupling="loopback",
        marker=None,
    )

    assert rc == 0, out
    assert out.startswith("ok note="), out
    # The reconciler's own reader of that prefix, over the same literal.
    run = reconcile_module.Pass(reason="test", print_env=False, no_restart=False)
    run.outputd_env_stage = str(tmp_path / "candidate.env")
    with (
        mock.patch(
            "jasper.audio_runtime_plan.validate_outputd_env",
            lambda **_kwargs: (True, (out.strip(),)),
        ),
        _captured_events() as events,
    ):
        assert run.validate_outputd_env_stage() is True
    assert stderr_event(
        events.getvalue(), "audio_hardware_reconcile.outputd_env_note"
    )["detail"] == _log_token(out.strip()[len("ok note=") :])


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


def _render_ring_conf(conf: Path, topology: Path | None = None) -> dict[str, str]:
    """The renderer the reconciler itself calls, over the same two inputs."""
    from jasper.ring_assets import ring_conf_wire_report

    return ring_conf_wire_report(
        profile_id="hifiberry_dac8x",
        conf_d=str(conf),
        output_topology=str(topology) if topology is not None else None,
    )


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
            "jasper.ring_assets.latency_floor_for",
            lambda profile_id: floor if profile_id == "hifiberry_dac8x" else None,
        )

    return _declare


@pytest.mark.parametrize(
    "listing,dac_id,result_code,reason",
    [
        pytest.param(APPLE_LISTING, "apple_usb_c_dongle", "unchanged", "none", id="apple-floor-matches"),
        pytest.param(DAC8X_STUDIO_LISTING, "hifiberry_dac8x_studio", "skipped", "no_declared_floor", id="no-floor"),
        pytest.param(DAC8X_AND_APPLE_LISTING, "hifiberry_dac8x", "unchanged", "none", id="dac8x-floor-matches"),
        pytest.param("", None, "skipped", "dac_unrecognized", id="unrecognized"),
    ],
)
def test_reconcile_preserves_a_ring_conf_that_needs_no_render(
    tmp_path: Path, listing: str, dac_id: str | None, result_code: str, reason: str
):
    conf = _staged_ring_conf(tmp_path)
    before_bytes = conf.read_bytes()
    before_mtime = conf.stat().st_mtime_ns

    result = _run_reconcile(
        tmp_path, listing, "--reason", "test",
        extra_env={"JASPER_RING_CONF_D": str(conf)},
    )

    assert result.returncode == 0, result.stderr
    fields = stderr_event(result.stderr, "audio_hardware_reconcile.ring_conf")
    assert (fields["pass_reason"], fields["result"], fields["reason"]) == ("test", result_code, reason)
    if dac_id is not None:
        assert fields["output_dac_id"] == dac_id
        assert fields["period_frames"] == fields["previous_period_frames"] == ("128" if result_code == "unchanged" else "none")
        assert fields["sample_format"] == ("S32_LE" if result_code == "unchanged" else "none")
        assert fields["topology"] == ("loaded" if result_code == "unchanged" else "none")
        assert {fields[k] for k in ("ring_a_channels", "ring_b_channels", "ring_active_channels")} == ({"2"} if result_code == "unchanged" else {"none"})
    assert conf.read_bytes() == before_bytes
    assert conf.stat().st_mtime_ns == before_mtime


def test_ring_render_renders_for_any_profile_declaring_the_slot_floor(
    tmp_path: Path, declare_slot_floor, capsys
) -> None:
    conf = _drifted_ring_conf(tmp_path)
    declare_slot_floor()

    report = _render_ring_conf(conf)
    assert report["result"] == "rendered"
    assert report["period_frames"] == str(RING_SLOT_FRAMES)
    assert report["previous_period_frames"] == "1024"

    from jasper import ring_assets

    assert ring_assets.ring_conf_period_frames(str(conf)) == RING_SLOT_FRAMES
    # EVERY ring PCM the conf.d defines converges onto the one slot period —
    # Ring A, Ring B, and the ACTIVE ring. The count is derived from the block
    # list rather than spelled, so adding a fourth ring cannot leave this
    # assertion silently checking a subset.
    assert conf.read_text(encoding="utf-8").count(
        f"    period_frames {RING_SLOT_FRAMES}"
    ) == len(ring_assets.RING_CONF_PCMS)


def test_ring_render_refuses_a_floor_the_ring_slot_cannot_carry(
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

    report = _render_ring_conf(conf)
    assert report["result"] == "skipped"
    assert report["reason"] == f"ring_slot_fixed_{RING_SLOT_FRAMES}"
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


def test_ring_render_is_idempotent(
    tmp_path: Path, declare_slot_floor, capsys
) -> None:
    # Reconcile runs on every boot and udev event; a converged box must stop
    # writing rather than churn the mtime on each pass.
    conf = _drifted_ring_conf(tmp_path)
    declare_slot_floor()

    assert _render_ring_conf(conf)["result"] == "rendered"
    settled_bytes = conf.read_bytes()
    settled_mtime = conf.stat().st_mtime_ns

    assert _render_ring_conf(conf)["result"] == "unchanged"
    assert conf.read_bytes() == settled_bytes
    assert conf.stat().st_mtime_ns == settled_mtime


def test_ring_render_reports_a_torn_conf_instead_of_inventing_one(
    tmp_path: Path, declare_slot_floor, capsys
) -> None:
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text("pcm.jts_ring_capture { type jts_ring }\n", encoding="utf-8")
    declare_slot_floor()

    with pytest.raises(ValueError, match="no period_frames"):
        _render_ring_conf(conf)
    assert conf.read_text(encoding="utf-8") == (
        "pcm.jts_ring_capture { type jts_ring }\n"
    )


@pytest.mark.parametrize(
    ("topology_json", "expected"),
    [
        # No argument resolves the SSOT path, and an absent file there is a
        # loaded empty draft — "not configured yet" is a ring-eligible shape,
        # not an unreadable one.
        pytest.param(None, "loaded", id="no-topology-argument"),
        # Fail-safe direction for a RENDERER: a topology it cannot read must
        # never move the conf.d off what the box is already running. Refusing
        # to ARM on one is the preflights' job. CORRUPT, not absent —
        # load_output_topology_strict returns an empty draft for a missing
        # file ("not configured yet" is a real, ring-eligible shape).
        pytest.param("{not json", "topology_unreadable", id="corrupt-topology"),
        pytest.param("<stereo>", "loaded", id="readable-topology"),
    ],
)
def test_ring_render_reports_the_wire_and_the_topology_it_resolved(
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

    report = _render_ring_conf(conf, topology_path)
    assert report["topology"] == expected
    assert report["sample_format"] == "S32_LE"
    assert report["ring_a_channels"] == "2"
    assert report["ring_b_channels"] == "2"
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
    return stderr_event(stderr, "audio_hardware_reconcile.flat_cutover")


def _mono_topology_payload() -> dict:
    return _topology_payload(
        topology_id="mono",
        name="Mono passive output",
        status="verified",
        hardware={
            "device_id": "innomaker_hifi_amp_pro",
            "device_label": "InnoMaker HiFi AMP Pro",
            "card_id": "sndrpimerusamp",
            "physical_output_count": 2,
        },
        speaker_groups=[{
            "id": "main", "label": "Main speaker", "kind": "mono",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 0}],
        }],
        routing={"mono_group_id": "main"},
    )


def _cutover_env(tmp_path: Path) -> dict[str, str]:
    conf_dir = tmp_path / "camilladsp"
    conf_dir.mkdir(exist_ok=True)
    return {
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
    runtime = stderr_events(result.stderr, "audio_hardware_reconcile.runtime_env")
    assert {e["content_format"] for e in runtime} == {"S32_LE"}


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
    assert not {"audio_hardware_reconcile.content_format_narrowed", "audio_hardware_reconcile.rate_match_content_bridge"} & set(_event_names(result.stderr))


# The two registry/policy probes that must degrade rather than write a guess.
_CONTENT_FORMAT_PROBE_FAILS = {
    "jasper.fanin_coupling.content_lane_format_for_coupling": _raises(
        RuntimeError("coupling policy unavailable")
    )
}
_EDGE_FORMAT_PROBE_FAILS = {
    "jasper.audio_hardware.dac.final_edge_format_for": _raises(
        RuntimeError("registry unavailable")
    )
}


_LANE_CAP_ANSWERS_FOUR = _lane_cap(lambda _id: 4)

# Every probe whose failure leaves an owned value UNWRITTEN, with the exit that
# failure produces. The two renderers are deliberately absent: a failed render
# leaves the previous artifact in place and states so on its own event, which
# is why the shell reconciler marked degraded only when the probe could not be
# reached at all.
_PROBE_FAILURES = {
    "observe": ({"jasper.audio_hardware.reconcile.observe": _raises(OSError("no /proc"))}, 0),
    "outputd_env_validator": (
        {"jasper.audio_runtime_plan.validate_outputd_env": _raises(RuntimeError("gone"))},
        78,
    ),
    "active_graph_decision": (
        {
            **_LANE_CAP_ANSWERS_FOUR,
            "jasper.active_speaker.runtime_contract.outputd_active_lane_decision": (
                _raises(RuntimeError("contract gone"))
            ),
        },
        0,
    ),
    "active_lane_cap": (_lane_cap(_raises(RuntimeError("registry gone"))), 0),
    "edge_format": (_EDGE_FORMAT_PROBE_FAILS, 0),
    "content_format": (_CONTENT_FORMAT_PROBE_FAILS, 0),
    "route_plan": (
        {"jasper.audio_runtime_plan.route_owned_env_actions": _raises(ValueError("x"))},
        0,
    ),
    "latency_floor": (_FLOOR_PLAN_PROBE_FAILS, 0),
    "runtime_graph": ({}, 1),
}


@pytest.mark.parametrize("probe", sorted(_PROBE_FAILURES))
def test_a_probe_that_could_not_answer_marks_the_pass_degraded(
    monkeypatch, tmp_path: Path, probe: str
):
    """A pass that could not run one of its probes left an owned value
    unwritten, so its result is NOT a state the shim's ``--changed`` may skip
    against — an operator following the doctor's remedy must get a real pass.

    The marker is also the doctor's own evidence, so this pins that the path
    the pass writes and ``output_hardware.degraded_marker_path`` reads are one
    file under one ``JASPER_OUTPUT_HARDWARE_STATE_PATH``.
    """
    from jasper.output_hardware import degraded_marker_path

    patches, expected_rc = _PROBE_FAILURES[probe]
    state_path = tmp_path / "output_hardware.json"
    result = _run_reconcile(
        tmp_path,
        INNOMAKER_LISTING,
        "--reason",
        "test",
        extra_env={"JASPER_OUTPUT_HARDWARE_STATE_PATH": str(state_path)},
        patches=patches,
        converge=_raises(RuntimeError("selector gone")) if probe == "runtime_graph"
        else _converged,
    )
    assert result.returncode == expected_rc, result.stderr

    monkeypatch.setenv("JASPER_OUTPUT_HARDWARE_STATE_PATH", str(state_path))
    assert degraded_marker_path().is_file()


def test_a_pass_whose_probes_all_answered_is_not_marked_degraded(
    monkeypatch, tmp_path: Path
):
    """The control for the parametrization above: without it every arm would
    pass on a marker some unrelated path always writes."""
    from jasper.output_hardware import degraded_marker_path

    state_path = tmp_path / "output_hardware.json"
    result = _run_reconcile(
        tmp_path,
        INNOMAKER_LISTING,
        "--reason",
        "test",
        extra_env={"JASPER_OUTPUT_HARDWARE_STATE_PATH": str(state_path)},
    )
    assert result.returncode == 0, result.stderr

    monkeypatch.setenv("JASPER_OUTPUT_HARDWARE_STATE_PATH", str(state_path))
    assert not degraded_marker_path().exists()


def test_reconcile_leaves_content_format_alone_when_the_policy_probe_is_absent(
    tmp_path: Path,
):
    """No answer == no write. A local fallback would be a second spelling of
    DEFAULT_PLAYBACK_FORMAT, and writing empty would silently narrow a wide
    box (outputd reads empty as S16_LE), so the key keeps whatever the box had
    and the skip is logged."""
    result = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "test",
        initial_outputd_env="JASPER_OUTPUTD_CONTENT_FORMAT=S32_LE\n",
        patches=_CONTENT_FORMAT_PROBE_FAILS,
    )

    assert result.returncode == 0, result.stderr
    assert "JASPER_OUTPUTD_CONTENT_FORMAT=S32_LE" in _outputd_env(tmp_path)
    assert {e["reason"] for e in stderr_events(result.stderr, "audio_hardware_reconcile.content_format_skip")} == {"coupling_probe_unavailable"}
    runtime = stderr_events(result.stderr, "audio_hardware_reconcile.runtime_env")
    assert {e["content_format"] for e in runtime} == {"unset"}


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
    extra_env: dict[str, str] = {}
    listing = APPLE_LISTING
    expected_dac_id = "apple_usb_c_dongle"
    stale_sink = "single_alsa" if composite else "composite"
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
        patches=_EDGE_FORMAT_PROBE_FAILS,
    )

    assert result.returncode == 0, result.stderr
    outputd_env = _outputd_env(tmp_path)
    # Preserved, not cleared — and specifically NOT the explicit-empty
    # spelling the unrecognized-DAC branch writes.
    assert "JASPER_OUTPUTD_DAC_FORMAT=S24_3LE" in outputd_env
    assert "JASPER_OUTPUTD_DAC_FORMAT=\n" not in outputd_env
    assert f"JASPER_OUTPUTD_SINK={stale_sink}" in outputd_env
    assert {(e["reason"], e["dac_id"], e["preserved"]) for e in stderr_events(result.stderr, "audio_hardware_reconcile.dac_format_skip")} == {
        ("registry_probe_unavailable", expected_dac_id, "S24_3LE")
    }
    runtime = stderr_events(result.stderr, "audio_hardware_reconcile.runtime_env")
    assert {e["dac_format"] for e in runtime} == {"S24_3LE"}
    if composite:
        assert {e["mode"] for e in runtime} == {"dual_apple"}


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
    events = stderr_events(result.stderr, "audio_hardware_reconcile.coupling_kick")
    return starts, events


def _assert_kicked_once(tmp_path: Path, result: subprocess.CompletedProcess[str]):
    starts, events = _coupling_kick_lines(tmp_path, result)
    assert starts, _systemctl_log(tmp_path)
    # --no-block IS THE DEADLOCK GUARD, not a nicety: the coupling pass kicks
    # this script back SYNCHRONOUSLY inside its arm, so a blocking start here
    # leaves this script waiting on a pass that is waiting on this script.
    assert all("--no-block" in line for line in starts), starts
    assert len(events) == 1 and events[0]["result"] == "started", events


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
    complete = stderr_event(second.stderr, "audio_hardware_reconcile.complete")
    assert (complete["dac_env_changed"], complete["render_changed"]) == ("0", "0")
    _assert_kicked_once(tmp_path, second)


def test_a_failed_runtime_convergence_withholds_the_coupling_kick(tmp_path: Path):
    """The trigger is final graph success, not DAC/render byte movement — and a
    convergence that failed must not hand a stale graph to the coupling pass.

    --no-block on the kick is load-bearing: the coupling pass kicks this unit
    back synchronously during its arm, so a blocking start would deadlock.
    """
    result = _run_reconcile(
        tmp_path,
        INNOMAKER_LISTING,
        "--reason",
        "udev",
        converge=lambda **kwargs: SimpleNamespace(
            ok=False,
            error="statefile refused",
            statefile_written=False,
            topology=None,
            decision=SimpleNamespace(ok=False, status="parked_muted", reason="stale"),
        ),
    )

    assert result.returncode == 1, result.stderr
    commands = _systemctl_log(tmp_path)
    assert "jasper-fanin-coupling-auto.service" not in commands
    assert not stderr_events(result.stderr, "audio_hardware_reconcile.coupling_kick")


# --- skipping a pass whose inputs have not moved ---
#
# The stamp and the `--changed` predicate are the shim's, because
# ExecCondition= may not start an interpreter (ADR-0226 rule 2). Each case here
# runs a real pass in-process to bring the box to its converged state, then
# drives the shim over a stand-in pass whose outcome is the variable under
# test - the real convergence writes outside tmp, so it cannot run hermetically
# in a subprocess.


def _fake_proc_asound(tmp_path: Path) -> dict[str, str]:
    root = tmp_path / "proc-asound"
    root.mkdir(exist_ok=True)
    (root / "cards").write_text(" 0 [Loopback]: Loopback\n", encoding="utf-8")
    (root / "pcm").write_text("00-00: Loopback : playback 1\n", encoding="utf-8")
    return {"JASPER_PROC_ASOUND": str(root)}


def _stub_pass(
    tmp_path: Path, name: str = "clean", *, body: str = "", rc: int = 0
) -> dict[str, str]:
    """A stand-in for the Python pass, so the shim's own stamp contract can be
    driven through each outcome the real pass can reach."""
    stub = _script(tmp_path, f"stub-pass-{name}", f"{body}\nexit {rc}\n")
    return {"JASPER_OUTPUT_HARDWARE_PYTHON": str(stub)}


# mode -> (stub kwargs, expected rc, stamp written, stamp_skipped reason)
_STUB_MODES: dict[str, tuple[dict[str, Any], int, bool, str | None]] = {
    "clean": ({}, 0, True, None),
    "pass-fails": ({"rc": 78}, 78, False, None),
    "card-moves-mid-pass": (
        {
            "body": (
                "printf ' 9 [Late]: USB-Audio - Late arrival\\n'"
                ' >> "$JASPER_PROC_ASOUND/cards"'
            )
        },
        0,
        False,
        "hardware_moved_mid_pass",
    ),
    "probe-unavailable": (
        {"body": ': > "${JASPER_OUTPUT_HARDWARE_STATE_PATH%/*}/reconcile.degraded"'},
        0,
        False,
        "probe_unavailable",
    ),
}


@pytest.mark.parametrize(
    ("mode", "mutate", "expected_rc"),
    [
        pytest.param("clean", None, 1, id="unchanged-skips"),
        pytest.param("clean", "cards", 0, id="card-set-moved-runs"),
        pytest.param("clean", "topology", 0, id="input-file-moved-runs"),
        pytest.param("pass-fails", None, 0, id="failed-pass-left-no-stamp"),
        pytest.param("card-moves-mid-pass", None, 0, id="mid-pass-hotplug-no-stamp"),
        pytest.param("probe-unavailable", None, 0, id="probe-unavailable-no-stamp"),
    ],
)
def test_changed_check_skips_only_after_a_successful_pass_over_the_same_inputs(
    tmp_path: Path, mode: str, mutate: str | None, expected_rc: int
) -> None:
    """The unit's ExecCondition: exit 0 means run, 1 means skip.

    A skipped call must reconcile nothing, and only an unchanged box that a
    successful pass already stamped may be skipped.
    """
    common = {**_fake_proc_asound(tmp_path), **_cutover_env(tmp_path)}
    converged = _run_reconcile(
        tmp_path, APPLE_LISTING, "--reason", "converge", extra_env=common
    )
    assert converged.returncode == 0, converged.stderr
    boot_config = (tmp_path / "config.txt").read_text(encoding="utf-8")

    stub_kwargs, seed_rc, stamped, skip_reason = _STUB_MODES[mode]
    stub_env = _stub_pass(tmp_path, mode, **stub_kwargs)
    seed = _run_shim(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "seed",
        initial_boot_config=boot_config,
        extra_env={**common, **stub_env},
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
    check = _run_shim(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "unit-start",
        "--changed",
        # A pass may rewrite the boot config; _reconcile_env would otherwise
        # reset it under the check and manufacture a change.
        initial_boot_config=boot_config,
        extra_env=common,
    )
    assert check.returncode == expected_rc, check.stderr
    verdict = "skipped" if expected_rc == 1 else "changed"
    assert stderr_event(check.stderr, f"audio_hardware_reconcile.{verdict}")
    # The check decides; it never reconciles.
    assert _render_log(tmp_path) == rendered_before
    assert _systemctl_log(tmp_path).splitlines()[issued_before:] == []
    assert not stderr_events(check.stderr, "audio_hardware_reconcile.complete")


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
    converged = _run_reconcile(
        tmp_path, APPLE_LISTING, "--reason", "converge", extra_env=common
    )
    assert converged.returncode == 0, converged.stderr
    boot_config = (tmp_path / "config.txt").read_text(encoding="utf-8")
    healthy = _run_shim(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "seed-healthy",
        initial_boot_config=boot_config,
        extra_env={**common, **_stub_pass(tmp_path)},
    )
    assert healthy.returncode == 0, healthy.stderr
    assert (tmp_path / "reconcile.stamp").exists()

    print_env = _run_reconcile(
        tmp_path,
        APPLE_LISTING,
        "--print-env",
        initial_boot_config=boot_config,
        extra_env=common,
        patches=_CONTENT_FORMAT_PROBE_FAILS,
    )
    assert print_env.returncode == 0, print_env.stderr
    # --print-env mutates nothing and reaches no probe that marks the pass
    # degraded, so stand the marker up the way a degraded mid-install probe
    # leaves it.
    (tmp_path / "reconcile.degraded").touch()
    # The stamp from the earlier full pass is left as-is: --print-env never
    # touches it either way.
    assert (tmp_path / "reconcile.stamp").exists()

    check = _run_shim(
        tmp_path,
        APPLE_LISTING,
        "--reason",
        "unit-start",
        "--changed",
        initial_boot_config=boot_config,
        extra_env=common,
    )
    assert check.returncode == 0, check.stderr
    assert stderr_event(check.stderr, "audio_hardware_reconcile.changed")


# The shim's `${VAR:-default}` list is what --changed hashes. A default that
# drifts from the module's own is not a loud failure: the fingerprint covers a
# path nothing reads, and the pass the box needs is condition-skipped instead.
_SHIM_DEFAULT = re.compile(
    r'^[A-Z_0-9]+="\$\{([A-Z_0-9]+):-([^}]*)\}"$', re.MULTILINE
)
# Declared by the shim alone: which interpreter runs the pass is not a path the
# pass reads, so no module states a default for it.
_SHIM_ONLY_ENV = {"JASPER_OUTPUT_HARDWARE_PYTHON"}


def _module_defaults() -> dict[str, str]:
    """What the Python side answers for each env seam, with nothing set."""
    import inspect

    from jasper.audio_hardware.config_txt import DEFAULT_BOOT_CONFIG_PATH
    from jasper.audio_hardware.usb_port_role import DEFAULT_MODEL_PATH
    from jasper.audio_runtime_plan import (
        DEFAULT_CAMILLA2_STATEFILE_PATH,
        DEFAULT_CAMILLA_STATEFILE_PATH,
    )
    from jasper.output_hardware import probe_system_cards
    from jasper.usbgadget import DEFAULT_UDC_CLASS_DIR

    with mock.patch.dict(os.environ, {}, clear=True):
        run = reconcile_module.Pass(
            reason="drift", print_env=True, no_restart=True
        )
        return {
            "JASPER_ENV_FILE": run.env_file,
            "JASPER_OUTPUTD_ENV_FILE": run.outputd_env_file,
            "JASPER_FANIN_ENV_FILE": run.fanin_env_file,
            "JASPER_ASOUND_SOURCE_TEMPLATE": run.asound_source_template,
            "JASPER_ASOUND_TEMPLATE": run.asound_template,
            "JASPER_OUTPUT_HARDWARE_STATE_PATH": run.state_path,
            "JASPER_I2S_HAT_INTENT_FILE": run.i2s_hat_intent_file,
            "JASPER_I2S_HAT_REBOOT_REQUIRED_PATH": run.i2s_hat_reboot_required_path,
            "JASPER_INSTALL_PROFILE_FILE": run.install_profile_file,
            "JASPER_OUTPUT_TOPOLOGY_PATH": run.output_topology_path,
            "JASPER_CAMILLA_CONF_DIR": run.camilla_conf_dir,
            # Spelled by the pass AND by the plan module that reads the same
            # two files; all three have to agree.
            "JASPER_CAMILLA_STATEFILE": DEFAULT_CAMILLA_STATEFILE_PATH,
            "JASPER_CAMILLA2_STATEFILE": DEFAULT_CAMILLA2_STATEFILE_PATH,
            # Read by the boot-config and classifier layers, not by the pass.
            "JASPER_PI_MODEL_FILE": DEFAULT_MODEL_PATH,
            "JTS_BOOT_CONFIG_FILE": DEFAULT_BOOT_CONFIG_PATH,
            "JASPER_UDC_CLASS_DIR": DEFAULT_UDC_CLASS_DIR,
            "JASPER_PROC_ASOUND": str(
                inspect.signature(probe_system_cards)
                .parameters["proc_asound"]
                .default
            ),
        }


# The three paths the shim DERIVES from the state path rather than reading
# from the environment, so `_SHIM_DEFAULT` cannot see them.
_SHIM_DERIVED = re.compile(
    r'^[A-Z_0-9]+="\$\{OUTPUT_HARDWARE_STATE_PATH%/\*\}/([^"]+)"$', re.MULTILINE
)


def test_the_shim_and_the_pass_agree_on_every_derived_leaf_name():
    """The two markers and the stamp are addressed by NAME from both sides.

    A leaf that drifts is silent in both directions: the pass would write a
    degraded marker the shim's stamp guard never reads, and jasper-usbgadget's
    `test -e` would miss a transport marker the pass did publish.
    """
    from jasper.output_hardware import degraded_marker_path

    derived = _SHIM_DERIVED.findall(SCRIPT.read_text(encoding="utf-8"))
    with mock.patch.dict(os.environ, {}, clear=True):
        run = reconcile_module.Pass(reason="drift", print_env=True, no_restart=True)
        state_dir = Path(run.state_path).parent
        assert run.management_transport_marker.parent == state_dir
        assert degraded_marker_path().parent == state_dir
        assert set(derived) == {
            run.management_transport_marker.name,
            degraded_marker_path().name,
            # The stamp answers --changed and nothing in the pass reads it, so
            # the shim is its only owner; pinned here so a fourth derived path
            # cannot appear without a Python counterpart or this list.
            "reconcile.stamp",
        }


def test_the_shim_and_the_pass_agree_on_every_default_path():
    shim = dict(_SHIM_DEFAULT.findall(SCRIPT.read_text(encoding="utf-8")))
    expected = _module_defaults()

    assert set(shim) - _SHIM_ONLY_ENV == set(expected), (
        "a shim variable has no module-side default to compare against (or "
        "the reverse) — an unguarded default here condition-skips a pass the "
        "box needs"
    )
    assert {k: v for k, v in shim.items() if k in expected} == expected
    # The camilla statefiles are the one pair the pass spells for itself.
    with mock.patch.dict(os.environ, {}, clear=True):
        run = reconcile_module.Pass(reason="drift", print_env=True, no_restart=True)
    assert run.camilla_statefile == expected["JASPER_CAMILLA_STATEFILE"]
    assert run.camilla2_statefile == expected["JASPER_CAMILLA2_STATEFILE"]
