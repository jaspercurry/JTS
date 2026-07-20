# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve the active bass-management crossover corner — the single READ seam.

The crossover *corner* itself has one shared definition (the constants in
:mod:`jasper.camilla_emit`; the SPEAKER layer owns the value). Two subsystems
*carry* a live corner, though, and they are chosen by hardware/bond state:

  - a LOCAL-DAC subwoofer, declared in the persisted output topology
    (:class:`jasper.output_topology.SpeakerChannel.crossover_fc_hz` on a
    ``subwoofer`` group) — the active-speaker Layer-A path high-passes the
    mains and low-passes the sub at this corner in its own CamillaDSP graph;
  - a WIRELESS subwoofer in a multi-room bond
    (:data:`jasper.multiroom.config.GroupingConfig.crossover_hz`) — a "sub"
    member low-passes and every main member high-passes at this corner.

This module is the one place that composes those two reads into "what corner is
bass-managing this speaker right now, and who owns it." Two consumers use it:

  - the ROOM correction designer READS the corner (never re-picks it) so it can
    refuse to boost inside the crossover region (revision plan §3.3);
  - the ``/correction/bass/`` wizard DISPLAYS the corner, its owner, and the
    sub/mains-HP state (read-only — the wizard does not own the corner).

CORNER PRECEDENCE (revision plan §6 default): when a speaker is BOTH an active
main AND bonded to a wireless sub, the active-speaker LOCAL config wins — the
wireless path defers (the reconciler's ``outputd_grouping_env`` clears the
wireless HP for an active endpoint). For an active main WITH a local sub that
means mains-HP is applied exactly once, in that box's CamillaDSP graph. KNOWN
GAP — the fourth quadrant: an active main bonded to a wireless-only sub (no
local sub) gets mains-HP applied ZERO times — its dac_content lane is cleared
AND its Layer-A graph only folds a mains HP for a local sub. That wiring gap is
the documented "Remaining" active-endpoint sub path in
HANDOFF-distributed-active.md; this resolver REPORTS it honestly
(``mains_highpass_enabled=False`` with
``mains_highpass_unwired_reason=MAINS_HP_UNWIRED_ACTIVE_ENDPOINT``) so displays
never claim a high-pass the box does not run. In every quadrant the resolver
reports what the reconciler actually wired.

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
# active-endpoint sub path in HANDOFF-distributed-active.md). Displays use this
# to distinguish "deliberately off" from "not applied on this speaker yet."
MAINS_HP_UNWIRED_ACTIVE_ENDPOINT = "active_endpoint_wireless_sub"


@dataclass(frozen=True)
class BassManagementState:
    """The resolved bass-management picture for this speaker, right now.

    ``corner_hz`` is ``None`` exactly when nothing is bass-managing the speaker
    (no local-DAC sub, no wireless sub) — the room designer treats that as "no
    crossover region to protect," and the wizard shows "not configured."
    """

    corner_hz: float | None
    owner: str | None            # OWNER_* or None when corner_hz is None
    sub_present: bool
    # Whether the mains high-pass (the complementary upper half of the sub
    # crossover) is actually wired ON THIS BOX. On a local-DAC active sub it is
    # folded into the CamillaDSP graph whenever the sub is present. On a
    # wireless bond it is the per-bond toggle for a dumb member — but False,
    # with ``mains_highpass_unwired_reason`` set, for the fourth-quadrant gap
    # (an active box whose wireless mains-HP is deferred yet whose graph has no
    # local sub to fold one for).
    mains_highpass_enabled: bool
    # Set (only with mains_highpass_enabled=False) when the bond WANTS mains-HP
    # but this box does not actually wire it — today only
    # MAINS_HP_UNWIRED_ACTIVE_ENDPOINT. None whenever the on/off state is the
    # whole truth.
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


def _is_active_speaker_box() -> bool:
    """Whether this box is a declared ACTIVE (multi-driver) speaker.

    Deliberately the reconciler's OWN branch signal
    (:func:`jasper.multiroom.reconcile.is_active_speaker_box`) rather than a
    re-derivation, so this resolver can never disagree with the gate that
    actually cleared (or kept) the wireless mains-HP env for this box. That
    function is total + fail-soft (False on any load failure — the safe passive
    read); the guard here only covers the lazy import itself."""
    try:
        from jasper.multiroom.reconcile import is_active_speaker_box
    except ImportError:
        return False
    return is_active_speaker_box()


def resolve_bass_management() -> BassManagementState:
    """Resolve the live bass-management corner + ownership, fail-soft.

    Precedence (§6): a local-DAC (active-speaker) sub owns the corner over a
    wireless-sub bond. When neither is present, returns the "no bass management"
    state (``corner_hz=None``).
    """
    # 1) Local-DAC active-speaker sub — highest precedence.
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

    # 2) Wireless-sub bond — defers to a local sub, but wins over nothing.
    try:
        from jasper.multiroom.config import bond_has_subwoofer, load_config

        cfg = load_config()
        if cfg.enabled and cfg.error is None and bond_has_subwoofer(cfg):
            wired = bool(cfg.mains_highpass_enabled)
            unwired_reason: str | None = None
            # The fourth quadrant (known gap — see module docstring): a NON-sub
            # member of a wireless-sub bond that is itself an active-speaker box
            # has its wireless mains-HP env CLEARED by the reconciler (§6
            # defer), and — having no local sub, or we'd be in branch 1 — its
            # Layer-A graph folds no mains HP either. The bond toggle says
            # "on"; this box actually runs full-range. Report the truth for
            # THIS box. (A channel=="sub" member never carries mains-HP itself;
            # for it the toggle describes the bond's mains, so it is passed
            # through unchanged.)
            if wired and cfg.channel != "sub" and _is_active_speaker_box():
                wired = False
                unwired_reason = MAINS_HP_UNWIRED_ACTIVE_ENDPOINT
            return BassManagementState(
                corner_hz=float(cfg.crossover_hz),
                owner=OWNER_WIRELESS_SUB,
                sub_present=True,
                mains_highpass_enabled=wired,
                mains_highpass_unwired_reason=unwired_reason,
            )
    except (OSError, ValueError, TypeError, AttributeError, ImportError):
        # Fail-soft: `load_config` is itself total (returns disabled on a
        # missing/bad file), so this only guards the import + attribute reads.
        logger.debug("wireless-sub corner read failed", exc_info=True)

    return _NO_BASS_MANAGEMENT


def active_crossover_corner_hz() -> float | None:
    """The live bass-management crossover corner (Hz), or ``None`` when nothing
    is bass-managing this speaker. The thin read the room-correction designer
    uses — it reads the corner, never re-picks it (revision plan §3.3)."""
    return resolve_bass_management().corner_hz
