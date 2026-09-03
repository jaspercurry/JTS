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

`jasper-aec-init` publishes the record for the pass it ran; the reconciler
asks this module by disposition token for the ones only it can see — the
lifecycle around a pass (`checking`, the two `unavailable` shapes) and the
faults on either side of it.  Dispositions whose reason is a capability detail
the caller measured take it as `reason`/`action`; the status is always this
module's.
"""
from __future__ import annotations

import shlex
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
ACTION_RECONNECT_XVF = "Reconnect the managed XVF3800 microphone"
ACTION_CONNECT_SUPPORTED_XVF = "Connect a supported XVF microphone"

REASON_APPLIED = "commissioned alignment applied and verified"
REASON_NOT_COMMISSIONED = (
    "chip-AEC alignment is not commissioned for this hardware/output identity"
)
REASON_OUTPUTD_ENV_STALE = "jasper-outputd has not loaded the current output declaration"
REASON_REAPPLY_FAILED = "silent chip-AEC alignment reapply failed"
REASON_REFERENCE_PRODUCER_DOWN = "final chip-reference producer failed to start"
REASON_BRIDGE_FAILED = "chip-AEC bridge failed after alignment reapply"
REASON_VALIDATING = "validating commissioned alignment"
REASON_XVF_ABSENT = "managed XVF is not present"

# What one alignment pass resolved.  APPLIED means the chip is holding a banked
# K; COMMISSION_REQUIRED/OUTPUTD_ENV_STALE/REAPPLY_FAILED are jasper-aec-init's
# exits, and the rest are what the reconciler sees around a pass: the faults on
# either side of it, the transient while it runs, an absent or unusable managed
# XVF, and the two capability disclosures whose detail it measured itself.
APPLIED = "applied"
COMMISSION_REQUIRED = "commission_required"
OUTPUTD_ENV_STALE = "outputd_env_stale"
REAPPLY_FAILED = "reapply_failed"
REFERENCE_PRODUCER_DOWN = "reference_producer_down"
BRIDGE_FAILED = "bridge_failed"
CHECKING = "checking"
XVF_ABSENT = "xvf_absent"
XVF_UNUSABLE = "xvf_unusable"
DAC_UNCALIBRATED = "dac_uncalibrated"
DISCLOSED = "disclosed"

# disposition -> (status, reason, action); None takes the caller's own text.
_DISPOSITIONS = {
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
    CHECKING: (STATUS_CHECKING, REASON_VALIDATING, ""),
    XVF_ABSENT: (STATUS_UNAVAILABLE, REASON_XVF_ABSENT, ACTION_RECONNECT_XVF),
    XVF_UNUSABLE: (STATUS_UNAVAILABLE, None, ACTION_CONNECT_SUPPORTED_XVF),
    DAC_UNCALIBRATED: (STATUS_DISCLOSED_STALE, None, ACTION_RECOMMISSION),
    DISCLOSED: (STATUS_DISCLOSED_STALE, None, None),
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

    def to_shell(self) -> str:
        """The record as the shell assignments its consumers eval.

        One line per key, so free text is folded onto one line here rather than
        in each writer.
        """

        return "".join(
            f"{key}={shlex.quote(' '.join(value.split()))}\n"
            for key, value in self.to_env().items()
        )

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
    reason: str = "",
    action: str = "",
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

    `reason`/`action` are read only by the dispositions that declare their slot
    open; every other disposition's text is this module's.
    """

    fixed = _DISPOSITIONS.get(disposition)
    if fixed is not None:
        status, fixed_reason, fixed_action = fixed
        return AlignmentHealth(
            status,
            reason if fixed_reason is None else fixed_reason,
            action if fixed_action is None else fixed_action,
            selection,
        )
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
