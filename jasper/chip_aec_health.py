# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One judge for whether this box's chip-AEC alignment is healthy.

`jasper-aec-init` resolves the banked K against the live chip-reference queue;
this leaf turns that resolution into the single household-facing verdict —
status, reason, action, selection — that `deploy/bin/jasper-aec-reconcile`
publishes as the `JASPER_AEC_CHIP_AEC_ALIGNMENT_*` record and
`jasper.audio_profile_state` reads back into `/state`.

`status` and `action` are matched on: the ladder branches on the status, and
`action` is decoded against `ACTION_RECOMMISSION` so surfaces gate on a boolean
instead of scanning prose.  `reason` is operator text — it is shown, never
matched, so a consumer that switches on it is reading the wrong field.

Pure: no device, filesystem, service or env access.  `jasper.chip_aec_policy`
owns who may READ the published record; this module owns what it says.

`checking` and the two `unavailable` records stay reconciler literals — they
report the lifecycle around an alignment pass, not a verdict about one, and
carry no alignment inputs to judge.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .chip_aec_alignment import PER_UNIT_IDENTITY_FIELDS

STATUS_READY = "ready"
STATUS_DISCLOSED_STALE = "disclosed_stale"
STATUS_CHECKING = "checking"
STATUS_UNAVAILABLE = "unavailable"
STATUS_FAULT = "fault"

ACTION_RECOMMISSION = "Run sudo jasper-aec-commission"
ACTION_WAIT_FOR_OUTPUTD = "Wait for jasper-outputd to restart, then run the reconciler"
ACTION_INSPECT_ALIGNMENT = (
    "Inspect jasper-aec-init and jasper-outputd, then run the reconciler"
)
ACTION_INSPECT_OUTPUTD = "Inspect jasper-outputd, then run the reconciler"
ACTION_INSPECT_BRIDGE = "Inspect jasper-aec-bridge, then run the reconciler"

REASON_APPLIED = "commissioned alignment applied and verified"
REASON_NOT_COMMISSIONED = (
    "chip-AEC alignment is not commissioned for this hardware/output identity"
)
REASON_OUTPUTD_ENV_STALE = "jasper-outputd has not loaded the current output declaration"
REASON_REAPPLY_FAILED = "silent chip-AEC alignment reapply failed"
REASON_REFERENCE_PRODUCER_DOWN = "final chip-reference producer failed to start"
REASON_BRIDGE_FAILED = "chip-AEC bridge failed after alignment reapply"

# What one alignment pass resolved.  APPLIED means the chip is holding a banked
# K; COMMISSION_REQUIRED/OUTPUTD_ENV_STALE/REAPPLY_FAILED are jasper-aec-init's
# non-zero exits, and the last two are the reconciler's own faults around it.
APPLIED = "applied"
COMMISSION_REQUIRED = "commission_required"
OUTPUTD_ENV_STALE = "outputd_env_stale"
REAPPLY_FAILED = "reapply_failed"
REFERENCE_PRODUCER_DOWN = "reference_producer_down"
BRIDGE_FAILED = "bridge_failed"

_FAILURES = {
    COMMISSION_REQUIRED: (
        STATUS_DISCLOSED_STALE, REASON_NOT_COMMISSIONED, ACTION_RECOMMISSION,
    ),
    OUTPUTD_ENV_STALE: (
        STATUS_DISCLOSED_STALE, REASON_OUTPUTD_ENV_STALE, ACTION_WAIT_FOR_OUTPUTD,
    ),
    REAPPLY_FAILED: (STATUS_FAULT, REASON_REAPPLY_FAILED, ACTION_INSPECT_ALIGNMENT),
    REFERENCE_PRODUCER_DOWN: (
        STATUS_FAULT, REASON_REFERENCE_PRODUCER_DOWN, ACTION_INSPECT_OUTPUTD,
    ),
    BRIDGE_FAILED: (STATUS_FAULT, REASON_BRIDGE_FAILED, ACTION_INSPECT_BRIDGE),
}

STATUS_KEY = "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS"
REASON_KEY = "JASPER_AEC_CHIP_AEC_ALIGNMENT_REASON"
ACTION_KEY = "JASPER_AEC_CHIP_AEC_ALIGNMENT_ACTION"
SELECTION_KEY = "JASPER_AEC_CHIP_AEC_ALIGNMENT_SELECTION"
ENV_KEYS = (STATUS_KEY, REASON_KEY, ACTION_KEY, SELECTION_KEY)


@dataclass(frozen=True)
class AlignmentHealth:
    """The published chip-AEC alignment record."""

    status: str
    reason: str = ""
    action: str = ""
    selection: str = ""

    def to_env(self) -> dict[str, str]:
        return {
            STATUS_KEY: self.status,
            REASON_KEY: self.reason,
            ACTION_KEY: self.action,
            SELECTION_KEY: self.selection,
        }

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> AlignmentHealth:
        """Read a published record back, leniently.

        The writer is shell on a box this build may not have written, so an
        unknown status travels rather than raising: `/state` reports what the
        reconciler said.  An absent record reads as an empty status, which is
        what the ladder already treats as "no verdict for this selection".
        """

        return cls(
            status=env.get(STATUS_KEY, ""),
            reason=env.get(REASON_KEY, ""),
            action=env.get(ACTION_KEY, ""),
            selection=env.get(SELECTION_KEY, ""),
        )


def alignment_health(
    disposition: str,
    *,
    selection: str = "",
    shipped_label: str = "",
    identity_diff: Sequence[str] = (),
) -> AlignmentHealth:
    """Judge one chip-AEC alignment pass.

    ADR-0101: a proof that stopped describing this box is applied and
    disclosed, not parked — so `shipped_label` (running on a sibling's proof)
    and `identity_diff` (the compared identity fields that moved) turn an
    APPLIED pass into `disclosed_stale` while the chip keeps its alignment.
    They are mutually exclusive: a box with nothing banked has no commissioned
    identity to diverge from.
    """

    failure = _FAILURES.get(disposition)
    if failure is not None:
        return AlignmentHealth(*failure, selection)
    if disposition != APPLIED:
        raise ValueError(f"unknown alignment disposition: {disposition!r}")

    if shipped_label:
        disclosure = (
            f"running on the shipped class alignment for {shipped_label}; "
            "run sudo jasper-aec-commission to personalize it to this unit"
        )
    elif identity_diff:
        # K is a property of the hardware CLASS, so a proof measured on a
        # sibling unit still describes this box; anything else moved the edge K
        # was measured against.
        disclosure = (
            (
                "commissioned alignment was measured on a different unit"
                if set(identity_diff) <= PER_UNIT_IDENTITY_FIELDS
                else "commissioned alignment no longer matches this hardware class"
            )
            + f" ({', '.join(identity_diff)})"
        )
    else:
        disclosure = ""
    return AlignmentHealth(
        status=STATUS_DISCLOSED_STALE if disclosure else STATUS_READY,
        reason=disclosure or REASON_APPLIED,
        action=ACTION_RECOMMISSION if disclosure else "",
        selection=selection,
    )
