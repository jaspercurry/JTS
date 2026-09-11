# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The stimulus a capture plays, spelled once for every CLI that states one.

``jasper-measure`` states it per take and ``jasper-angle-capture`` states it once
for a whole walk, but both are naming the SAME
:class:`~jasper.active_speaker.crossover_v2.measure_spec.MeasureSpec` fields --
so a second spelling of ``--sweep-s`` would be a second vocabulary for one
concept. Nothing here validates: the spec judges every value when the capture is
built.
"""
from __future__ import annotations

import argparse


def add_stimulus_args(parser: argparse.ArgumentParser) -> None:
    """Add the four ``MeasureSpec`` stimulus flags, under their one spelling."""
    parser.add_argument(
        "--level-dbfs", type=float, action="append", default=[], metavar="DBFS",
        help="one stimulus level per ladder rung, repeatable",
    )
    parser.add_argument(
        "--sweep-band-hz", type=float, nargs=2, default=[], metavar=("LOW", "HIGH"),
        help="summed-sweep bounds in Hz; protected graph admission still applies",
    )
    parser.add_argument("--spl-ceiling-db-spl", type=float, default=None)
    parser.add_argument(
        "--sweep-s", type=float,
        help="summed sweep duration; still bounded by declared driver duration caps",
    )
