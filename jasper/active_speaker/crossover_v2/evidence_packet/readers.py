# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jasper.audio_measurement.evidence_identity import (
    EvidenceIdentityError,
    json_fingerprint,
)

from ..feature_classification import FeatureVerdict, read_feature_verdicts
from ..round_inputs import CrossoverEvidencePacketError

#: Bumped when a reader that understood the previous version would misread
#: this one — never merely because the document grew. The
#: EVIDENCE document's version only: a prescription answering this packet
#: carries its own :data:`~.blend_prescription.PRESCRIPTION_SCHEMA_VERSION`.
PACKET_SCHEMA_VERSION = 4

PACKET_KIND = "jts_crossover_v2_evidence_packet"

#: The block citing optional views' outputs. The fingerprint skips it: a view
#: is a function of the round's takes and its own parameters, so running one
#: never moves the evidence a prescription answers.
DERIVED_VIEWS = "derived_views"


def fingerprinted(packet: Mapping[str, Any]) -> dict[str, Any]:
    """The part of a packet its ``packet_fingerprint`` covers: all but the derived views (ADR-0346)."""
    return {key: value for key, value in packet.items() if key not in ("packet_fingerprint", DERIVED_VIEWS)}


def _fingerprint(packet: dict[str, Any]) -> str:
    try:
        return json_fingerprint(fingerprinted(packet), field_name="evidence_packet")
    except EvidenceIdentityError as exc:
        raise CrossoverEvidencePacketError(f"packet is not exact JSON data: {exc}") from exc


# --- the readers the gate uses, so the packet owns its own shape ---


def packet_driver_passbands_hz(packet: Any) -> dict[str, tuple[float, float]]:
    """Each role's own declared band, or ``{}`` when the packet carries none."""
    if not isinstance(packet, dict):
        return {}
    drivers = packet.get("drivers")
    if not isinstance(drivers, dict) or drivers.get("status") != "available":
        return {}
    bands = drivers.get("passbands_hz")
    if not isinstance(bands, dict):
        return {}
    out: dict[str, tuple[float, float]] = {}
    for role, band in bands.items():
        if not isinstance(role, str) or not role.strip():
            continue
        if not isinstance(band, (list, tuple)) or len(band) != 2:
            continue
        try:
            lo, hi = float(band[0]), float(band[1])
        except (TypeError, ValueError, OverflowError):
            continue
        if lo > 0.0 and hi > lo:
            out[role.strip()] = (lo, hi)
    return out


def packet_feature_classifications(packet: Any) -> tuple[FeatureVerdict, ...] | None:
    """The classification view's verdicts, or ``None`` when this round has none."""
    if not isinstance(packet, dict):
        return None
    views = packet.get(DERIVED_VIEWS)
    block = views.get("feature_classification") if isinstance(views, dict) else None
    if not isinstance(block, dict) or block.get("status") != "available":
        return None
    return read_feature_verdicts(block.get("verdicts"))
