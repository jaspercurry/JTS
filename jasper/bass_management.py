# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve the active bass-management crossover corner — the single READ seam.

The crossover *corner* itself has one shared definition (the constants in
:mod:`jasper.camilla_emit`; the SPEAKER layer owns the value). One subsystem
*carries* a live corner: a LOCAL-DAC subwoofer, declared in the persisted
output topology
(:class:`jasper.output_topology.SpeakerChannel.crossover_fc_hz` on a
``subwoofer`` group). An active main folds its own mains high-pass at that
corner exactly when its preset declares a
:class:`jasper.active_speaker.profile.LocalSubwoofer` — sub low-pass and mains
high-pass are the two halves of one crossover inside that box's own CamillaDSP
Layer-A graph.

This module is the one place that turns that read into "what corner is
bass-managing this speaker right now, and who owns it." Two consumers use it:

  - the ROOM correction designer READS the corner (never re-picks it) so it can
    refuse to boost inside the crossover region (revision plan §3.3);
  - the ``/correction/bass/`` wizard DISPLAYS the corner, its owner, and the
    sub/mains-HP state (read-only — the wizard does not own the corner).

TOTAL + fail-soft. Every read is best-effort; any load/parse failure resolves
to "no bass management" (corner ``None``) rather than raising — a room
correction or a display must never break because a state file is momentarily
unreadable. Import-light: the heavier topology/config readers are imported
lazily so the socket-activated web process stays cheap.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# Who owns the live corner (or that nothing does). Stable string vocabulary so
# the room designer's report annotation, the wizard display, and any doctor/
# state surface agree on the same words.
OWNER_ACTIVE_SPEAKER_LOCAL = "active_speaker_local"
OWNER_WIRELESS_SUB = "wireless_sub"

# Why the mains high-pass is NOT wired on this box even though the bond's
# bass-management toggle is on. Today's only value: the known fourth-quadrant
# gap — an active-speaker box bonded to a wireless-only sub. Its dac_content
# lane is cleared (the §6 defer) and its Layer-A graph only folds a mains HP
# for a LOCAL sub, so the mains run full-range (the documented "Remaining"
# active-endpoint sub path). Displays use this
# to distinguish "deliberately off" from "not applied on this speaker yet."
MAINS_HP_UNWIRED_ACTIVE_ENDPOINT = "active_endpoint_wireless_sub"


@dataclass(frozen=True)
class BassManagementState:
    """The resolved bass-management picture for this speaker, right now.

    ``corner_hz`` is ``None`` exactly when nothing is bass-managing the speaker
    (no local-DAC sub) — the room designer treats that as "no crossover region
    to protect," and the wizard shows "not configured."
    """

    corner_hz: float | None
    owner: str | None            # OWNER_* or None when corner_hz is None
    sub_present: bool
    # Whether the mains high-pass (the complementary upper half of the sub
    # crossover) is actually wired ON THIS BOX. On a local-DAC active sub it is
    # folded into the CamillaDSP graph whenever the sub is present.
    mains_highpass_enabled: bool
    # Why mains-HP is not wired despite being wanted. Nothing sets it since
    # the wireless sub was removed; the wizard payload still carries the key.
    mains_highpass_unwired_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "corner_hz": self.corner_hz,
            "owner": self.owner,
            "sub_present": self.sub_present,
            "mains_highpass_enabled": self.mains_highpass_enabled,
            "mains_highpass_unwired_reason": self.mains_highpass_unwired_reason,
        }


_NO_BASS_MANAGEMENT = BassManagementState(
    corner_hz=None,
    owner=None,
    sub_present=False,
    mains_highpass_enabled=False,
)


def _local_dac_sub_corner() -> float | None:
    """The local-DAC subwoofer's crossover corner from the persisted topology,
    or ``None`` when this speaker has no local sub. Fail-soft."""
    try:
        from jasper.camilla_emit import BASS_MANAGEMENT_CORNER_HZ_DEFAULT
        from jasper.output_topology import load_output_topology

        topology = load_output_topology()
        sub_ids = set(topology.routing.subwoofer_group_ids)
        for group in topology.speaker_groups:
            # A subwoofer group is either referenced by routing OR self-declares
            # kind/mode "subwoofer" (routing may be mid-commission).
            is_sub = group.id in sub_ids or group.kind == "subwoofer" or (
                group.mode == "subwoofer"
            )
            if not is_sub:
                continue
            for channel in group.channels:
                if channel.crossover_fc_hz is not None:
                    return float(channel.crossover_fc_hz)
            # A sub group with no explicit per-channel corner uses the default
            # corner (the active builder falls back to it), so report that.
            return float(BASS_MANAGEMENT_CORNER_HZ_DEFAULT)
    except (OSError, ValueError, TypeError, AttributeError, ImportError):
        # Fail-soft: `load_output_topology` is itself total (returns an empty
        # draft on a missing/bad file), so this only guards the import + the
        # attribute walk. Any failure -> no local sub corner.
        logger.debug("local-DAC sub corner read failed", exc_info=True)
    return None


def resolve_bass_management() -> BassManagementState:
    """Resolve the live bass-management corner + ownership, fail-soft.

    Returns the "no bass management" state (``corner_hz=None``) when this
    speaker declares no local-DAC sub.
    """
    local_corner = _local_dac_sub_corner()
    if local_corner is not None:
        return BassManagementState(
            corner_hz=local_corner,
            owner=OWNER_ACTIVE_SPEAKER_LOCAL,
            sub_present=True,
            # An active-speaker graph always high-passes the mains at the corner
            # when a local sub is present (the emitter folds the complementary
            # upper half). There is no per-speaker "disable" toggle — the local
            # sub is only ever wired WITH bass management.
            mains_highpass_enabled=True,
        )

    return _NO_BASS_MANAGEMENT


def active_crossover_corner_hz() -> float | None:
    """The live bass-management crossover corner (Hz), or ``None`` when nothing
    is bass-managing this speaker. The thin read the room-correction designer
    uses — it reads the corner, never re-picks it (revision plan §3.3)."""
    return resolve_bass_management().corner_hz
