# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Classify local ALSA capture cards against the measurement-mic registry.

The shell bridge for ``deploy/bin/jasper-aec-reconcile``, which must never
select a calibrated measurement microphone (a UMIK-2 and friends) as the
voice/wake input: those mics carry no wake or AEC contract, so selecting one
would silently replace the household's room microphone with an instrument.

Both halves of that decision live in ``jasper.audio_measurement.mic_identity``
(ADR-0235 D1) — the registry vocabulary and the ``/proc/asound/<card>/usbid``
read that answers it — so this module is just the shell payload and bash keeps
only the membership test. Identity is the USB ``vid:pid``, never the serial: a
UMIK-2's USB serial descriptor is the literal "00000" on every unit.

Import ``jasper.audio_measurement.mic_identity``, never ``calibration``: the
reconciler spawns this on every hotplug pass that sees a USB capture card, and
``calibration`` costs the full numpy import.

Output contract:

* **exit 0** — stdout holds one shell assignment,
  ``JASPER_CAPTURE_MEASUREMENT_CARDS=<space-separated card ids>``, naming the
  subset of the requested cards that are registered measurement microphones.
  It is legitimately EMPTY when none of them is.
* **non-zero exit** — the registry could not be read (module unimportable,
  killed). The caller must treat this as "I could not classify", never as "no
  measurement mics are present": the reconciler excludes nothing in that case,
  because refusing to classify must never be able to leave a speaker with no
  microphone at all.
"""
from __future__ import annotations

import argparse
import shlex
import sys
from collections.abc import Iterable
from pathlib import Path

from jasper.audio_measurement.mic_identity import (
    measurement_mic_usb_ids,
    read_card_usb_id,
)


ENV_KEY = "JASPER_CAPTURE_MEASUREMENT_CARDS"


def measurement_class_cards(
    cards: Iterable[str],
    *,
    asound_root: Path,
) -> tuple[str, ...]:
    """Which of `cards` are registered measurement microphones, in order."""
    registered = set(measurement_mic_usb_ids())
    matched: list[str] = []
    for card in dict.fromkeys(cards):
        usb_id = read_card_usb_id(asound_root / card)
        if usb_id and usb_id in registered:
            matched.append(card)
    return tuple(matched)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--asound-root",
        default="/proc/asound",
        help="procfs ALSA root to inspect (test hook; default: /proc/asound)",
    )
    parser.add_argument(
        "cards",
        nargs="*",
        help="ALSA card ids to classify",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    matched = measurement_class_cards(
        args.cards, asound_root=Path(args.asound_root)
    )
    sys.stdout.write(f"{ENV_KEY}={shlex.quote(' '.join(matched))}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
