# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The shared core-graph park roster contains every audio endpoint holder."""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAGMENT = ROOT / "deploy" / "lib" / "jasper-core-graph-park-units.sh"

# The canonical contract: the ordered set of audio clients to stop before a
# core-graph restart. Keep in sync with the fragment ONLY by editing the
# fragment — this literal is the assertion target, not a second source.
CANONICAL_PARK_UNITS = [
    "jasper-voice.service",
    "jasper-aec-bridge.service",
    "jasper-outputd.service",
    "jasper-camilla-crossover.service",
    "jasper-snapclient.service",
    "jasper-snapserver.service",
    "shairport-sync.service",
    "nqptp.service",
    "librespot.service",
    "bluealsa-aplay.service",
    "jasper-mux.service",
]


def _source_fragment_array(name: str) -> list[str]:
    """Source the fragment under bash and return one of its arrays.

    Tests the real array the consumer sees, not the source text — a typo
    that broke the array definition would fail here."""
    script = f'source "{FRAGMENT}"\nprintf "%s\\n" "${{{name}[@]}}"\n'
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return [line for line in proc.stdout.splitlines() if line]


def test_fragment_defines_canonical_ordered_park_list():
    assert _source_fragment_array("JASPER_CORE_GRAPH_PARK_UNITS") == CANONICAL_PARK_UNITS
