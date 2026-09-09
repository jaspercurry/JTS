# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Single-source-of-truth guard for the core-graph park roster.

Before restarting the core DSP graph (CamillaDSP / outputd / fan-in), the
units that can hold a DAC / Camilla / renderer ALSA endpoint must be
stopped, or the graph start fails with "Device or resource busy" (EBUSY).
The installer (park_audio_clients_for_core_graph_restart in
deploy/lib/install/systemd-units.sh) drives that from the sourced fragment
deploy/lib/jasper-core-graph-park-units.sh; a second copy would drift and
re-leak a holder. These tests pin the fragment's ordered content and that
the consumer does not re-inline it.

Scope note: the multiroom-follower park set
(jasper.local_sources.registry.local_source_park_units) is a DIFFERENT,
legitimately-separate set (it parks bluealsa/bt-agent/usbsink and omits
the core daemons) and is intentionally NOT consolidated here.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAGMENT = ROOT / "deploy" / "lib" / "jasper-core-graph-park-units.sh"
SYSTEMD_UNITS = ROOT / "deploy" / "lib" / "install" / "systemd-units.sh"

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


# The full multi-line park stop-loop shape, present ONLY in the fragment.
# If the consumer re-inlines the list, this pattern reappears there and the
# no-re-inline test below fails. (jasper-voice.service is the first park unit
# and the most distinctive head of the stop list.)
_INLINE_PARK_BLOCK = re.compile(
    r"jasper-voice\.service\s*\\?\s*\n\s*"
    r"jasper-aec-bridge\.service\s*\\?\s*\n\s*"
    r"jasper-outputd\.service",
)


def test_installer_consumer_sources_fragment_and_has_no_inline_park_list():
    text = SYSTEMD_UNITS.read_text(encoding="utf-8")
    assert "source " in text and "jasper-core-graph-park-units.sh" in text
    assert 'for unit in "${JASPER_CORE_GRAPH_PARK_UNITS[@]}"' in text
    assert not _INLINE_PARK_BLOCK.search(text), (
        "park_audio_clients_for_core_graph_restart re-inlined the park list; "
        "iterate JASPER_CORE_GRAPH_PARK_UNITS from the shared fragment instead"
    )
