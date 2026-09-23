# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from jasper.audio_hardware import dac, output_probe
from jasper.audio_hardware.hat_eeprom import HatEeprom
from jasper.audio_hardware.output_probe import (
    parse_aplay_listing,
    probe_aplay_listing,
    probe_system_cards,
)
import jasper.cli.output_hardware as output_hardware_cli
from jasper.output_hardware import (
    APPLE_USB_C_DONGLE_DEVICE_ID,
    OutputHardwareState,
    classify_output_cards,
)


def test_parse_aplay_listing_classifies_known_output_cards() -> None:
    cards = parse_aplay_listing("""
hw:CARD=A,DEV=0
    Apple USB-C to 3.5mm Headphone Jack, USB Audio
hw:CARD=DAC8XStudio,DEV=0
    HiFiBerry DAC8x Studio, USB Audio
""")

    assert [card.card_id for card in cards] == ["A", "DAC8XStudio"]
    assert cards[0].device_id == APPLE_USB_C_DONGLE_DEVICE_ID
    assert cards[1].device_id == dac.HIFIBERRY_DAC8X_STUDIO_ID


def test_probe_aplay_listing_bounds_a_hung_aplay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The only caller runs on the DAC-vanished path under a unit with a 50 s
    TimeoutStartSec; a wedged USB stack must not block it indefinitely."""
    monkeypatch.setattr(output_probe, "_APLAY_LISTING_TIMEOUT_SEC", 0.2)
    stub = tmp_path / "aplay"
    stub.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    stub.chmod(0o755)

    started = time.monotonic()
    result = probe_aplay_listing(aplay=str(stub))
    elapsed = time.monotonic() - started

    assert result == ""
    assert elapsed < 2.0


def test_probe_system_cards_uses_usb_device_path_as_stable_path(
    tmp_path: Path,
) -> None:
    sys_class = tmp_path / "sys" / "class" / "sound"
    proc_asound = tmp_path / "proc" / "asound"
    usb_device = (
        tmp_path / "sys" / "devices" / "platform" / "xhci-hcd.0" / "usb1" / "1-2"
    )
    card_dir = usb_device / "1-2:1.0" / "sound" / "card5"
    sys_class.mkdir(parents=True)
    proc_asound.mkdir(parents=True)
    card_dir.mkdir(parents=True)
    for name, value in {
        "idVendor": "05ac",
        "idProduct": "110a",
        "busnum": "1",
        "devpath": "1-2",
        "product": "Apple USB-C to 3.5mm Headphone Jack",
    }.items():
        (usb_device / name).write_text(value, encoding="utf-8")
    (sys_class / "card5").symlink_to(card_dir)
    proc_card = proc_asound / "card5"
    proc_card.mkdir()
    (proc_card / "id").write_text("A", encoding="utf-8")
    (proc_card / "pcm0p").mkdir()
    (proc_card / "stream0").write_text(
        "Playback:\n  Endpoint: 0x01 (SYNC)\n",
        encoding="utf-8",
    )

    (card,) = probe_system_cards(
        sys_class_sound=sys_class,
        proc_asound=proc_asound,
    )

    assert card.card_id == "A"
    assert card.stable_path == str(usb_device.resolve())
    assert "card5" not in card.stable_path
    assert card.usb_path == "1-2"


def test_probe_system_cards_classifies_non_usb_hifiberry_from_proc_cards(
    tmp_path: Path,
) -> None:
    sys_class = tmp_path / "sys" / "class" / "sound"
    proc_asound = tmp_path / "proc" / "asound"
    card_dir = tmp_path / "sys" / "devices" / "platform" / "soc" / "sound" / "card2"
    sys_class.mkdir(parents=True)
    proc_asound.mkdir(parents=True)
    card_dir.mkdir(parents=True)
    (sys_class / "card2").symlink_to(card_dir)
    proc_card = proc_asound / "card2"
    proc_card.mkdir()
    (proc_card / "id").write_text("sndrpihifiberry", encoding="utf-8")
    (proc_card / "pcm0p").mkdir()
    (proc_asound / "cards").write_text(
        " 2 [sndrpihifiberry]: RPi-simple - snd_rpi_hifiberry_dac8x\n"
        "                      snd_rpi_hifiberry_dac8x\n",
        encoding="utf-8",
    )

    (card,) = probe_system_cards(
        sys_class_sound=sys_class,
        proc_asound=proc_asound,
    )
    state = classify_output_cards([card])

    assert card.device_id == dac.HIFIBERRY_DAC8X_ID
    assert state.profile_id == dac.HIFIBERRY_DAC8X_ID
    assert state.status == "ready"
    assert state.physical_output_count == 8


_STUDIO_HAT = HatEeprom(
    vendor="HiFiBerry",
    product="StudioDAC8x",
    uuid="be3b8164-dd7b-48fc-ab27-79dd7c641980",
)
# rpi-6.18.y names every Studio-family card this, so the label carries no
# product token and no width (#2258).
_UNIFIED_STUDIO_LABEL = "Hifiberry Studio Soundcard"


def _unified_studio_sysfs(tmp_path: Path) -> tuple[Path, Path]:
    sys_class = tmp_path / "sys" / "class" / "sound"
    proc_asound = tmp_path / "proc" / "asound"
    card_dir = tmp_path / "sys" / "devices" / "platform" / "soc" / "sound" / "card1"
    sys_class.mkdir(parents=True)
    proc_asound.mkdir(parents=True)
    card_dir.mkdir(parents=True)
    (sys_class / "card1").symlink_to(card_dir)
    proc_card = proc_asound / "card1"
    proc_card.mkdir()
    (proc_card / "id").write_text("HiFiBerryStudio", encoding="utf-8")
    (proc_card / "pcm0p").mkdir()
    (proc_asound / "cards").write_text(
        f" 1 [HiFiBerryStudio]: HifiberryStudio - {_UNIFIED_STUDIO_LABEL}\n"
        f"                      {_UNIFIED_STUDIO_LABEL}\n",
        encoding="utf-8",
    )
    return sys_class, proc_asound


@pytest.mark.parametrize("discovery", ["sysfs", "aplay_fallback"])
def test_hat_eeprom_routes_the_shared_studio_name_into_the_record(
    discovery: str,
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """`python -m jasper.cli.output_hardware --write` is the record's one writer.

    Both card-discovery paths must consult the same EEPROM, and the record has
    to carry the evidence it routed on — `/state` and doctor read only what
    this publishes (#2258).
    """
    state_file = tmp_path / "output_hardware.json"
    monkeypatch.setenv("JASPER_OUTPUT_HARDWARE_STATE_PATH", str(state_file))
    monkeypatch.setattr(output_probe, "read_hat_eeprom", lambda: _STUDIO_HAT)
    if discovery == "sysfs":
        sys_class, proc_asound = _unified_studio_sysfs(tmp_path)
        monkeypatch.setenv("JASPER_SYS_CLASS_SOUND", str(sys_class))
        monkeypatch.setenv("JASPER_PROC_ASOUND", str(proc_asound))
    else:
        monkeypatch.setattr(output_probe, "probe_system_cards", lambda **_: ())
        monkeypatch.setattr(
            output_probe,
            "probe_aplay_listing",
            lambda _aplay: (
                f"hw:CARD=HiFiBerryStudio,DEV=0\n    {_UNIFIED_STUDIO_LABEL}\n"
            ),
        )

    assert output_hardware_cli.main(["--write"]) == 0

    capsys.readouterr()
    published = json.loads(state_file.read_text(encoding="utf-8"))
    assert published["profile_id"] == dac.HIFIBERRY_DAC8X_STUDIO_ID
    assert published["hat_eeprom"] == {
        "vendor": "HiFiBerry",
        "product": "StudioDAC8x",
        "uuid": "be3b8164-dd7b-48fc-ab27-79dd7c641980",
    }
    assert OutputHardwareState.from_mapping(published).hat_eeprom == _STUDIO_HAT


def test_the_shared_studio_name_parks_and_publishes_a_null_hat_eeprom(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """No HAT is a published fact, so a reader can tell it from an old record."""

    state_file = tmp_path / "output_hardware.json"
    sys_class, proc_asound = _unified_studio_sysfs(tmp_path)
    monkeypatch.setenv("JASPER_OUTPUT_HARDWARE_STATE_PATH", str(state_file))
    monkeypatch.setenv("JASPER_SYS_CLASS_SOUND", str(sys_class))
    monkeypatch.setenv("JASPER_PROC_ASOUND", str(proc_asound))
    monkeypatch.setattr(output_probe, "read_hat_eeprom", lambda: None)

    assert output_hardware_cli.main(["--write"]) == 0

    capsys.readouterr()
    published = json.loads(state_file.read_text(encoding="utf-8"))
    assert published["hat_eeprom"] is None
    assert published["profile_id"] == "unknown"
    assert OutputHardwareState.from_mapping(published).hat_eeprom is None
