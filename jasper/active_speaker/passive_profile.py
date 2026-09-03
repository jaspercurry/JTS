# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What a PASSIVE box's profile has become: roleful or flat.

The topology-only half lives in :mod:`jasper.output_topology`; these predicates
answer the profile-shaped half it cannot. See ADR-0212.
"""

from __future__ import annotations

from typing import Any, Mapping

from jasper.output_topology import (
    OutputTopology,
    subwoofer_speaker_groups,
    topology_is_passive_mains,
)

__all__ = [
    "measured_candidate_fingerprint",
    "passive_mains_compiles_roleful",
]


def measured_candidate_fingerprint(source: Any) -> str:
    """The candidate fingerprint a profile's ``source`` block names, or ``""``.

    Absence is ordinary: a profile levelled by the guided captures names none.
    """
    if not isinstance(source, Mapping):
        return ""
    value = source.get("measured_candidate_fingerprint")
    return value if isinstance(value, str) else ""


def passive_mains_compiles_roleful(
    topology: OutputTopology,
    measured_candidate: Any,
    applied_anchor: Mapping[str, Any] | None,
) -> bool:
    """Does this passive box compile a roleful preset rather than a flat graph?

    With a local subwoofer, unconditionally: bass management splits the program.
    SUBLESS, only where a recommissioning round measured its one full-range
    branch and fitted a linearization; otherwise the box keeps the flat
    ``emit_sound_config`` lane.

    That measured fact reaches the compiler two ways and BOTH are it: the
    candidate being compiled, on the apply itself, and the fingerprint the
    applied profile's source records, on every later read-back. Read off the
    applied anchor, never the mutable saved candidate: the question is what the
    speaker is PLAYING.
    """
    if not topology_is_passive_mains(topology):
        return False
    if subwoofer_speaker_groups(topology) or measured_candidate is not None:
        return True
    if not isinstance(applied_anchor, Mapping):
        return False
    return bool(measured_candidate_fingerprint(applied_anchor.get("source")))
