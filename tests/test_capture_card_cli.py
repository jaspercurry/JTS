# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""`jasper.cli.capture_card` — the reconciler's capture-card classifier.

Everything here drives the real CLI against a tmp_path ALSA tree and parses
its shell payload; nothing asserts on prose. What the classification then
decides — `aec_ready`, candidate selection, the park under them — is pinned
against the real bash in `tests/test_aec_reconcile.py`; this file pins only
the payload that bash reads, so the two never restate each other.
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

from jasper.audio_measurement import mic_identity
from jasper.cli import capture_card
from jasper.mics import xvf3800
from tests.test_aec_reconcile import _write_card, _write_usb_card


UMIK2_USB_ID = mic_identity.SUPPORTED_MODELS["minidsp_umik2"]["usb_ids"][0]
XVF_USB_ID = xvf3800.USB_VID_PIDS[0]


def _run(tmp_path: Path, *cards: str) -> dict[str, str]:
    """Run the CLI as bash does and parse the shell assignments it prints."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "jasper.cli.capture_card",
            "--asound-root",
            str(tmp_path / "asound"),
            "--",
            *cards,
        ],
        check=False,
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, raw = line.partition("=")
        values[key] = " ".join(shlex.split(raw))
    return values


def test_only_registered_ids_are_excluded(tmp_path: Path) -> None:
    """An over-broad filter leaves the speaker deaf, which is worse than not
    excluding an instrument — so the excluded set is exactly the registered
    one, in the order asked."""
    _write_usb_card(tmp_path, "UMIK2", UMIK2_USB_ID, channels=1)
    _write_usb_card(tmp_path, "Array", XVF_USB_ID, channels=6)
    _write_card(tmp_path, card="I2S", channels=2)

    values = _run(tmp_path, "Array", "UMIK2", "I2S", "Absent")

    assert values[capture_card.ENV_KEY].split() == ["UMIK2"]


def test_a_card_the_kernel_never_enumerated_excludes_nothing(
    tmp_path: Path,
) -> None:
    """No usbid file at all — an absent card. Naming it must not classify it,
    and must not fail the pass."""
    values = _run(tmp_path, "Array", "UMIK2")

    assert values[capture_card.ENV_KEY] == ""


def test_a_hand_written_usbid_is_normalised(tmp_path: Path) -> None:
    """The kernel writes %04x:%04x, but a hand-made fixture (and a future
    kernel) may not: case and surrounding whitespace cannot decide whether a
    speaker keeps its microphone."""
    _write_card(tmp_path, card="UMIK2", channels=1)
    usbid = tmp_path / "asound" / "UMIK2" / "usbid"
    usbid.write_text(f"  {UMIK2_USB_ID.upper()}  \n")

    values = _run(tmp_path, "UMIK2")

    assert values[capture_card.ENV_KEY].split() == ["UMIK2"]


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

    assert values[capture_card.ENV_KEY].split() == ["UMIK2"]
