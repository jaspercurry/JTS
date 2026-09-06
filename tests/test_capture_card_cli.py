# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""`jasper.cli.capture_card` — the reconciler's capture-card classifier.

Everything here drives the real CLI against a tmp_path ALSA tree and parses
its shell payload; nothing asserts on prose. The classification it publishes
is one half of `aec_ready` in `deploy/bin/jasper-aec-reconcile` (the other
half is the channel count), and `aec_ready` decides whether the box hears at
all — so the pins are on the emitted key, not on internals.
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from jasper.audio_measurement import mic_identity
from jasper.cli import capture_card
from jasper.mics import xvf3800


UMIK2_USB_ID = mic_identity.SUPPORTED_MODELS["minidsp_umik2"]["usb_ids"][0]
XVF_USB_ID = xvf3800.USB_VID_PIDS[0]


def _write_card(
    root: Path, card: str, *, usb_id: str | None = None, channels: int = 2
) -> None:
    """A capture card as /proc/asound spells one: Playback block first."""
    card_dir = root / card
    card_dir.mkdir(parents=True)
    (card_dir / "stream0").write_text(
        "Playback:\n  Channels: 2\nCapture:\n"
        f"  Channels: {channels}\n"
    )
    if usb_id is not None:
        (card_dir / "usbid").write_text(f"{usb_id}\n")


def _run(root: Path, *cards: str) -> dict[str, str]:
    """Run the CLI as bash does and parse the shell assignments it prints."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "jasper.cli.capture_card",
            "--asound-root",
            str(root),
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


def _aec_ready(values: dict[str, str], card: str, channels: int) -> bool:
    """`aec_ready` as deploy/bin/jasper-aec-reconcile spells it: the channel
    count equals the XVF registry's recommendation AND the card is not
    measurement-class."""
    excluded = values[capture_card.ENV_KEY].split()
    return (
        channels == xvf3800.RECOMMENDED_CAPTURE_CHANNELS
        and card not in excluded
    )


@pytest.mark.parametrize(
    ("usb_id", "channels", "ready"),
    [
        # The stock commissioned speaker: 6-channel XVF voice array.
        (XVF_USB_ID, xvf3800.RECOMMENDED_CAPTURE_CHANNELS, True),
        # A registered measurement mic that also passes the format check.
        # Channel count alone is not eligibility — a 6-channel instrument
        # would put the software-AEC stack on a calibration microphone.
        (UMIK2_USB_ID, xvf3800.RECOMMENDED_CAPTURE_CHANNELS, False),
        # The same instrument as it really enumerates.
        (UMIK2_USB_ID, 1, False),
        # A non-registered USB capture card with the wrong channel count.
        (XVF_USB_ID, 2, False),
        # An I2S/virtual card: no usbid, so it can never be measurement-class.
        (None, xvf3800.RECOMMENDED_CAPTURE_CHANNELS, True),
    ],
)
def test_aec_ready_is_the_channel_count_and_not_a_measurement_mic(
    tmp_path: Path, usb_id: str | None, channels: int, ready: bool
) -> None:
    """The one pin. `aec_ready` gates the whole software/chip AEC stack and
    the direct-mic fallback under it, so its two inputs are pinned together:
    the recommended channel count, and "not a registered measurement mic"."""
    _write_card(tmp_path, "CARD0", usb_id=usb_id, channels=channels)

    values = _run(tmp_path, "CARD0")

    assert _aec_ready(values, "CARD0", channels) is ready


def test_only_registered_ids_are_excluded(tmp_path: Path) -> None:
    """An over-broad filter leaves the speaker deaf, which is worse than not
    excluding an instrument — so the excluded set is exactly the registered
    one, in the order asked."""
    _write_card(tmp_path, "UMIK2", usb_id=UMIK2_USB_ID, channels=1)
    _write_card(tmp_path, "Array", usb_id=XVF_USB_ID, channels=6)
    _write_card(tmp_path, "I2S", channels=2)

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
    _write_card(tmp_path, "UMIK2", channels=1)
    (tmp_path / "UMIK2" / "usbid").write_text(f"  {UMIK2_USB_ID.upper()}  \n")

    values = _run(tmp_path, "UMIK2")

    assert values[capture_card.ENV_KEY].split() == ["UMIK2"]
