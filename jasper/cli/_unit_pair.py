# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One length declared in inches or metric, read back in metres.

The pair is mutually exclusive: without the group the reader's check order
decides which of two supplied units wins. ``metric`` = (suffix, metres/unit).
"""
from __future__ import annotations

import argparse

from jasper.audio_measurement.measurement_geometry import METERS_PER_INCH

#: Each metric suffix's ``--help`` metavar and word.
_UNIT_WORDS = {"m": ("METERS", "meters"), "mm": ("MM", "millimetres")}

METRES = ("m", 1.0)
MILLIMETRES = ("mm", 0.001)


def add_unit_pair(
    parser: argparse.ArgumentParser, name: str, *,
    required: bool, label: str, metric: tuple[str, float] = METRES,
) -> None:
    """Add ``--<name>-in`` and ``--<name>-<suffix>``, never both."""
    suffix, _ = metric
    metavar, word = _UNIT_WORDS[suffix]
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument(f"--{name}-in", type=float, default=None,
                       metavar="INCHES", help=f"{label}, in inches")
    group.add_argument(f"--{name}-{suffix}", type=float, default=None,
                       metavar=metavar, help=f"{label}, in {word}")


def unit_pair_meters(
    args: argparse.Namespace, name: str, *, metric: tuple[str, float] = METRES
) -> float | None:
    """The pair's value in metres, or ``None`` when neither unit was named."""
    suffix, meters_per_unit = metric
    dest = name.replace("-", "_")
    inches = getattr(args, f"{dest}_in")
    if inches is not None:
        return float(inches) * METERS_PER_INCH
    declared = getattr(args, f"{dest}_{suffix}")
    return None if declared is None else float(declared) * meters_per_unit
