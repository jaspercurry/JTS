# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One package-owned microphone/AEC observation snapshot."""
from __future__ import annotations

from pathlib import Path

from jasper.audio_measurement import mic_identity
from jasper.aec.reconcile.observe import observe
from jasper.mics import xvf3800
from tests.test_aec_reconcile import _write_card, _write_usb_card


UMIK2_USB_ID = mic_identity.SUPPORTED_MODELS["minidsp_umik2"]["usb_ids"][0]
XVF_USB_ID = xvf3800.USB_VID_PIDS[0]


def _run(tmp_path: Path, *cards: str) -> frozenset[str]:
    return observe(
        {"JASPER_MIC_DEVICE_CANDIDATES": ",".join(cards)},
        tmp_path / "asound", str(tmp_path / "absent-outputd.sock"), lambda _message: None,
    ).measurement_cards


def test_only_registered_ids_are_excluded(tmp_path: Path) -> None:
    """An over-broad filter leaves the speaker deaf, which is worse than not
    excluding an instrument — so the excluded set is exactly the registered
    one."""
    _write_usb_card(tmp_path, "UMIK2", UMIK2_USB_ID, channels=1)
    _write_usb_card(tmp_path, "Array", XVF_USB_ID, channels=6)
    _write_card(tmp_path, card="I2S", channels=2)

    values = _run(tmp_path, "Array", "UMIK2", "I2S", "Absent")

    assert values == {"UMIK2"}


def test_a_card_the_kernel_never_enumerated_excludes_nothing(
    tmp_path: Path,
) -> None:
    """No usbid file at all — an absent card. Naming it must not classify it,
    and must not fail the pass."""
    values = _run(tmp_path, "Array", "UMIK2")

    assert values == frozenset()


def test_a_hand_written_usbid_is_normalised(tmp_path: Path) -> None:
    """The kernel writes %04x:%04x, but a hand-made fixture may not: case and surrounding whitespace cannot decide whether a
    speaker keeps its microphone."""
    _write_card(tmp_path, card="UMIK2", channels=1)
    usbid = tmp_path / "asound" / "UMIK2" / "usbid"
    usbid.write_text(f"  {UMIK2_USB_ID.upper()}  \n")

    values = _run(tmp_path, "UMIK2")

    assert values == {"UMIK2"}


def test_an_undecodable_usbid_only_unclassifies_its_own_card(
    tmp_path: Path,
) -> None:
    """One card's unreadable id decides nothing about another's. Non-UTF-8
    bytes in a usbid used to abort the whole run, which turned classification
    off for every card asked about — and the instrument in that same pass then
    read as an ordinary voice mic."""
    _write_usb_card(tmp_path, "UMIK2", UMIK2_USB_ID, channels=1)
    _write_card(tmp_path, card="SPARE", channels=2)
    (tmp_path / "asound" / "SPARE" / "usbid").write_bytes(b"\xff\xfe\n")

    values = _run(tmp_path, "UMIK2", "SPARE")

    assert values == {"UMIK2"}


def test_each_owner_is_observed_once_and_the_status_is_forwarded(tmp_path: Path, monkeypatch) -> None:
    import importlib
    from collections import Counter

    observation = importlib.import_module("jasper.aec.reconcile.observe")
    calls = Counter()
    status = {"reference_outputs": {}}
    profile = xvf3800.detect_runtime_profile(asound_root=tmp_path / "asound")
    gate_owner = observation.resolve_chip_aec_dac_gate

    def mic(**kwargs):
        calls["mic"] += 1
        return profile

    def accessories():
        calls["accessories"] += 1
        return ("remote",)

    def outputd(_path):
        calls["outputd"] += 1
        return status

    def gate(*args, **kwargs):
        calls["policy"] += 1
        assert kwargs["outputd_status"] is status
        return gate_owner(*args, **kwargs)

    monkeypatch.setattr(observation.xvf3800, "detect_runtime_profile", mic)
    monkeypatch.setattr(observation, "read_accessory_mic_sources", accessories)
    monkeypatch.setattr(observation, "read_status_socket", outputd)
    monkeypatch.setattr(observation, "resolve_chip_aec_dac_gate", gate)
    facts = observation.observe({}, tmp_path / "asound", "unused", lambda _message: None)
    assert calls == {"mic": 1, "accessories": 1, "outputd": 1, "policy": 1}
    assert facts.mic is profile
    assert facts.accessory_sources == ("remote",)
