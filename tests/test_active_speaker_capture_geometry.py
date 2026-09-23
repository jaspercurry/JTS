# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Placement proof accepts the deployed capture protocols."""

from __future__ import annotations

import pytest

from jasper.active_speaker.capture_geometry import (
    PLACEMENT_PROOF_ACKNOWLEDGEMENT_CAPABLE_PROTOCOLS,
    placement_proof_shape_valid,
)
from jasper.active_speaker.crossover_v2.sweep_spec import CAPTURE_PROTOCOL_VERSION


def _reference_axis_proof(
    *, comparison_hex: str = "a", capture_session_id: str = "capture-reference"
) -> dict:
    return {
        "schema_version": 1,
        "accepted": True,
        "confirmation_source": "capture_begin",
        "acknowledgement_binding_sha256": "d" * 64,
        "capture_session_id": capture_session_id,
        "capture_protocol_version": 3,
        "capture_page_build": "20260712.1",
        "policy_id": "driver_reference_axis_v1",
        "comparison_set_id": comparison_hex * 32,
        "comparison_set_fingerprint": comparison_hex * 64,
        "target_fingerprint": "c" * 64,
        "speaker_group_id": "mono",
        "role": "woofer",
    }


def test_placement_proof_allowlist_contains_the_current_capture_protocol():
    """The allowlist duplicates its members as literals because
    crossover_v2.sweep_spec imports capture_geometry (not the other way round),
    so the module cannot import the constant back without inverting that
    dependency. Pin CONTAINMENT here — not equality: the allowlist is
    deliberately WIDER than the emitted protocol, because it reads persisted
    proofs (see the next test)."""
    assert (
        CAPTURE_PROTOCOL_VERSION in PLACEMENT_PROOF_ACKNOWLEDGEMENT_CAPABLE_PROTOCOLS
    )


def test_placement_proof_still_accepts_a_page_protocol_2_persisted_proof():
    """Regression: dropping 2 from the allowlist when the Pi stopped EMITTING
    protocol 2 would retroactively invalidate real persisted evidence.

    A placement proof stamps the PAGE's `capture_protocol_version`,
    and the published capture page build 20260712.3 advertised protocol 2. Any
    proof captured against it carries 2 forever. Invalidating those breaks
    repeat admission, crossover readiness, and replay for already-commissioned
    speakers."""
    # A proof exactly as the published 20260712.3 page caused it to be written.
    proof = {
        **_reference_axis_proof(),
        "capture_protocol_version": 2,
        "capture_page_build": "20260712.3",
    }
    assert placement_proof_shape_valid(
        proof,
        policy_id="driver_reference_axis_v1",
        role="woofer",
        speaker_group_id="mono",
        target_fingerprint="c" * 64,
    )


@pytest.mark.parametrize(
    ("protocol", "valid"), [(1, False), (2, True), (3, True), (4, False)]
)
def test_placement_proof_capture_protocol_is_an_explicit_allowlist(
    protocol, valid
):
    """PLACEMENT_PROOF_ACKNOWLEDGEMENT_CAPABLE_PROTOCOLS is a deliberate
    allowlist, not a ``>= 2`` floor: protocol 1 has no acknowledgement
    machinery for a proof to stand on, 2 and 3 authenticate the same
    acknowledgement through the same choreography, and a future protocol 4
    must be a deliberate addition here, never a silent pass-through (see the
    constant's comment in jasper.active_speaker.capture_geometry)."""
    proof = {**_reference_axis_proof(), "capture_protocol_version": protocol}
    assert placement_proof_shape_valid(
        proof,
        policy_id="driver_reference_axis_v1",
        role="woofer",
        speaker_group_id="mono",
        target_fingerprint="c" * 64,
    ) is valid
