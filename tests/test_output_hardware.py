from __future__ import annotations

from pathlib import Path

from jasper.audio_hardware.dac import (
    APPLE_USB_C_DONGLE_ID,
    DUAL_APPLE_USB_C_DAC_4CH_ID,
    HIFIBERRY_DAC8X_ID,
)
from jasper.output_hardware import (
    OutputCardFact,
    classify_output_cards,
    parse_aplay_listing,
    probe_system_cards,
    topology_hardware_mapping,
)


def test_parse_aplay_listing_classifies_known_output_cards() -> None:
    cards = parse_aplay_listing(
        """
hw:CARD=A,DEV=0
    Apple USB-C to 3.5mm Headphone Jack, USB Audio
hw:CARD=sndrpihifiberry,DEV=0
    snd_rpi_hifiberry_dac8x, HiFiBerry DAC8x
"""
    )

    assert [card.card_id for card in cards] == ["A", "sndrpihifiberry"]
    assert cards[0].profile_id == "apple_usb_c_dongle"
    assert cards[1].profile_id == "hifiberry_dac8x"


def test_dual_apple_state_keeps_observed_hardware_separate_from_active_runtime():
    state = classify_output_cards(
        (
            OutputCardFact(
                card_id="A",
                profile_id=APPLE_USB_C_DONGLE_ID,
                controller="xhci-hcd.0",
                busnum="001",
                stable_path="/sys/devices/apple-a",
            ),
            OutputCardFact(
                card_id="B",
                profile_id=APPLE_USB_C_DONGLE_ID,
                controller="xhci-hcd.0",
                busnum="001",
                stable_path="/sys/devices/apple-b",
            ),
        ),
        active_profile_id=APPLE_USB_C_DONGLE_ID,
        active_card_id="A",
        active_recognized=True,
        observed_at="2026-06-09T00:00:00Z",
    )

    assert state["active"]["profile_id"] == APPLE_USB_C_DONGLE_ID
    assert state["active"]["runtime_ready"] is True
    assert state["observed"]["profile_id"] == DUAL_APPLE_USB_C_DAC_4CH_ID
    assert state["observed"]["status"] == "ready"
    assert state["observed"]["same_usb_bus"] is True
    assert state["observed"]["runtime_ready"] is False
    assert {issue["code"] for issue in state["issues"]} == {
        "observed_active_profile_mismatch",
        "dual_apple_runtime_handoff_pending",
    }

    topology = topology_hardware_mapping(state)
    assert topology is not None
    assert topology["device_id"] == DUAL_APPLE_USB_C_DAC_4CH_ID
    assert topology["physical_output_count"] == 4
    assert [output["terminal_label"] for output in topology["outputs"]] == [
        "A-L",
        "A-R",
        "B-L",
        "B-R",
    ]


def test_dual_apple_requires_same_usb_bus_for_ready_observed_shape():
    state = classify_output_cards(
        (
            OutputCardFact(
                card_id="A",
                profile_id=APPLE_USB_C_DONGLE_ID,
                controller="xhci-hcd.0",
                busnum="001",
                stable_path="/sys/devices/apple-a",
            ),
            OutputCardFact(
                card_id="B",
                profile_id=APPLE_USB_C_DONGLE_ID,
                controller="xhci-hcd.1",
                busnum="002",
                stable_path="/sys/devices/apple-b",
            ),
        ),
        active_profile_id=APPLE_USB_C_DONGLE_ID,
        active_card_id="A",
        active_recognized=True,
    )

    assert state["observed"]["profile_id"] == DUAL_APPLE_USB_C_DAC_4CH_ID
    assert state["observed"]["status"] == "blocked"
    assert state["observed"]["same_usb_bus"] is False
    assert topology_hardware_mapping(state) is None
    assert "dual_apple_usb_topology_mismatch" in {
        issue["code"] for issue in state["issues"]
    }


def test_dual_apple_can_use_busnum_when_controller_label_is_unavailable():
    state = classify_output_cards(
        (
            OutputCardFact(
                card_id="A",
                profile_id=APPLE_USB_C_DONGLE_ID,
                busnum="001",
                stable_path="/sys/devices/apple-a",
            ),
            OutputCardFact(
                card_id="B",
                profile_id=APPLE_USB_C_DONGLE_ID,
                busnum="001",
                stable_path="/sys/devices/apple-b",
            ),
        ),
        active_profile_id=APPLE_USB_C_DONGLE_ID,
        active_card_id="A",
        active_recognized=True,
    )

    assert state["observed"]["profile_id"] == DUAL_APPLE_USB_C_DAC_4CH_ID
    assert state["observed"]["status"] == "ready"
    assert state["observed"]["same_usb_bus"] is True


def test_active_known_profile_preserves_observed_state_when_sysfs_is_sparse():
    state = classify_output_cards(
        (),
        active_profile_id=HIFIBERRY_DAC8X_ID,
        active_card_id="sndrpihifiberry",
        active_recognized=True,
        observed_at="2026-06-09T00:00:00Z",
    )

    assert state["active"]["profile_id"] == HIFIBERRY_DAC8X_ID
    assert state["active"]["runtime_ready"] is True
    assert state["observed"]["profile_id"] == HIFIBERRY_DAC8X_ID
    assert state["observed"]["status"] == "ready"
    assert state["observed"]["card_id"] == "sndrpihifiberry"
    assert state["child_devices"][0]["profile_id"] == HIFIBERRY_DAC8X_ID
    assert state["issues"] == []


def _write_fake_apple_card(
    root: Path,
    *,
    index: int,
    card_id: str,
    devpath: str,
) -> None:
    sys_root = root / "sys" / "class" / "sound"
    proc_root = root / "proc" / "asound"
    usb = (
        root
        / "devices"
        / "pci0000:00"
        / "xhci-hcd.0"
        / "usb1"
        / devpath
    )
    sound = usb / f"{devpath}:1.0" / "sound" / f"card{index}"
    sound.mkdir(parents=True)
    (usb / "idVendor").write_text("05ac\n", encoding="utf-8")
    (usb / "idProduct").write_text("110a\n", encoding="utf-8")
    (usb / "product").write_text(
        "Apple USB-C to 3.5mm Headphone Jack\n",
        encoding="utf-8",
    )
    (usb / "busnum").write_text("001\n", encoding="utf-8")
    (usb / "devpath").write_text(devpath.replace("1-", "") + "\n", encoding="utf-8")
    sys_root.mkdir(parents=True, exist_ok=True)
    (sys_root / f"card{index}").symlink_to(sound, target_is_directory=True)
    card_proc = proc_root / f"card{index}"
    (card_proc / "pcm0p").mkdir(parents=True)
    (card_proc / "id").write_text(card_id + "\n", encoding="utf-8")
    (card_proc / "stream0").write_text(
        "Playback:\n  Endpoint: 0x01 (SYNC)\n",
        encoding="utf-8",
    )


def test_probe_system_cards_reads_usb_identity_without_opening_pcm(tmp_path: Path):
    _write_fake_apple_card(tmp_path, index=0, card_id="A", devpath="1-1")
    _write_fake_apple_card(tmp_path, index=1, card_id="B", devpath="1-2")

    cards = probe_system_cards(
        sys_class_sound=tmp_path / "sys" / "class" / "sound",
        proc_asound=tmp_path / "proc" / "asound",
    )

    assert [card.card_id for card in cards] == ["A", "B"]
    assert {card.profile_id for card in cards} == {APPLE_USB_C_DONGLE_ID}
    assert {card.controller for card in cards} == {"xhci-hcd.0"}
    assert {card.busnum for card in cards} == {"001"}
    assert {card.endpoint_sync for card in cards} == {"SYNC"}
