# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared active-speaker revalidation proof helpers."""

from __future__ import annotations

from typing import Any, Mapping

REVALIDATION_AFTER_DRIVER_TARGET_PROOF_STEPS = frozenset({
    "combined_check",
    "save_profile",
    "apply_profile",
})


def applied_profile_revalidation_satisfies_driver_target_proof(
    revalidation: Mapping[str, Any] | None,
) -> bool:
    """Return whether an applied-profile edit can skip first-time driver proof.

    New setup must prove every driver/output target with current evidence. Once
    a profile has been applied, the baseline-profile revalidation state owns the
    edit decision for changes that leave the physical target contract unchanged.
    If the superseded profile was applied and the next step is at or after the
    combined crossover check, callers can treat the driver-target gate as
    already proven.
    """

    if not isinstance(revalidation, Mapping):
        return False
    if revalidation.get("required") is not True:
        return False
    if (
        str(revalidation.get("next_step") or "")
        not in REVALIDATION_AFTER_DRIVER_TARGET_PROOF_STEPS
    ):
        return False
    changed = revalidation.get("changed")
    if isinstance(changed, list) and "topology_fingerprint" in {
        str(item) for item in changed
    }:
        return False
    superseded_raw = revalidation.get("superseded_profile")
    superseded: Mapping[str, Any] = (
        superseded_raw if isinstance(superseded_raw, Mapping) else {}
    )
    return (
        str(revalidation.get("reason") or "") == "applied_profile_superseded"
        or str(superseded.get("status") or "") == "applied"
    )
