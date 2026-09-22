# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import math
from typing import Any

from jasper.active_speaker.camilla_yaml import LINEARIZATION_BIQUAD_TYPES
from jasper.audio_measurement.evidence_identity import (
    EvidenceIdentityError,
    json_fingerprint,
)

from ..feature_classification import FeatureVerdict, read_feature_verdicts
from ..round_inputs import CrossoverEvidencePacketError
from .offline_reads import _mapping

#: Bumped when a reader that understood the previous version would misread
#: this one — never merely because the document grew. The
#: EVIDENCE document's version only: a prescription answering this packet
#: carries its own :data:`~.blend_prescription.PRESCRIPTION_SCHEMA_VERSION`.
PACKET_SCHEMA_VERSION = 2

PACKET_KIND = "jts_crossover_v2_evidence_packet"


def _fingerprint(packet: dict[str, Any]) -> str:
    try:
        return json_fingerprint(
            {key: value for key, value in packet.items() if key != "packet_fingerprint"},
            field_name="evidence_packet",
        )
    except EvidenceIdentityError as exc:
        raise CrossoverEvidencePacketError(f"packet is not exact JSON data: {exc}") from exc


class PacketSchemaUnsupported(CrossoverEvidencePacketError):
    reason = "packet_schema_unsupported"


def validate_packet(packet: Any) -> dict[str, Any]:
    """Check the complete frozen input, without consulting mutable source files."""
    if (
        not isinstance(packet, dict)
        or packet.get("kind") != PACKET_KIND
        or packet.get("artifact_schema_version") != PACKET_SCHEMA_VERSION
    ):
        raise PacketSchemaUnsupported(PacketSchemaUnsupported.reason)
    if packet.get("packet_fingerprint") != _fingerprint(packet):
        raise CrossoverEvidencePacketError("evidence packet content does not match its fingerprint")
    return packet


# --- the readers the gate uses, so the packet owns its own shape ---


def packet_region_band_hz(packet: Any) -> tuple[float, float] | None:
    """The crossover region, or ``None`` when the packet does not carry one."""
    if not isinstance(packet, dict):
        return None
    region = packet.get("crossover_region")
    if not isinstance(region, dict) or not region.get("available"):
        return None
    band = region.get("band_hz")
    if not isinstance(band, (list, tuple)) or len(band) != 2:
        return None
    try:
        lo, hi = float(band[0]), float(band[1])
    except (TypeError, ValueError, OverflowError):
        return None
    if not (lo > 0.0 and hi > lo):
        return None
    return (lo, hi)


def packet_driver_passbands_hz(packet: Any) -> dict[str, tuple[float, float]]:
    """Each role's own declared band, or ``{}`` when the packet carries none."""
    if not isinstance(packet, dict):
        return {}
    drivers = packet.get("drivers")
    if not isinstance(drivers, dict) or not drivers.get("available"):
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


def packet_incumbent_linearization(
    packet: Any,
) -> dict[str, tuple[dict[str, Any], ...]] | None:
    """The per-driver correction the graph is already carrying, or ``None``.

    A reader rather than an attribute access: the packet owns its own layout.

    ``None`` ("this packet does not say") and ``{}`` ("it says the graph
    carries none") are DIFFERENT and both callers must keep them apart — a
    document that replaces a role it cannot see is the defect this reader
    exists to expose.

    Strict, and it fails the WHOLE map rather than a filter: a partial read
    would understate the displacement, the one direction this number must never
    err in. Permitted biquad types are the emitter's own
    ``camilla_yaml.LINEARIZATION_BIQUAD_TYPES``, consumed rather than restated.

    Entries come back in the reduced ``{biquad_type, freq, q, gain}`` shape
    :func:`~jasper.active_speaker.branch_chain.chain_response` takes.
    """

    if not isinstance(packet, dict):
        return None
    block = _mapping(packet.get("incumbent")).get("linearization")
    if not isinstance(block, dict):
        return None
    roles = block.get("from_applied_profile")
    if not isinstance(roles, dict):
        return None
    # The builder writes an ``_absence`` here when no profile reached it, and
    # that shape is checked by name rather than inferred from its contents —
    # ``_incumbent_record``'s rule, for the same reason: an absence and a role
    # map are both dicts, and telling them apart by duck-typing would make a
    # banked role called ``status`` change the answer.
    if roles.get("status") == "not_evaluated":
        return None
    out: dict[str, tuple[dict[str, Any], ...]] = {}
    for role, filters in roles.items():
        if not isinstance(role, str) or not role.strip():
            return None
        if isinstance(filters, (str, bytes)) or not isinstance(filters, list):
            return None
        entries: list[dict[str, Any]] = []
        for entry in filters:
            if not isinstance(entry, dict):
                return None
            if entry.get("biquad_type") not in LINEARIZATION_BIQUAD_TYPES:
                return None
            # Real numbers, NOT anything ``float()`` will coerce, and ``bool``
            # excluded because it is an ``int`` subclass — the same test
            # ``blend_filters_from_mapping`` applies, for the same reason: this
            # system writes floats, so a string here is by definition a record
            # something else wrote.
            numbers: list[float] = []
            for value in (entry.get("freq"), entry.get("q"), entry.get("gain")):
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    return None
                numbers.append(float(value))
            freq, q, gain = numbers
            if not all(map(math.isfinite, numbers)):
                return None
            if freq <= 0.0 or q <= 0.0:
                return None
            entries.append({
                "biquad_type": str(entry["biquad_type"]),
                "freq": freq,
                "q": q,
                "gain": gain,
            })
        out[role.strip()] = tuple(entries)
    return out


def packet_feature_classifications(packet: Any) -> tuple[FeatureVerdict, ...] | None:
    """The banked verdicts, or ``None`` when this round has none."""
    if not isinstance(packet, dict):
        return None
    block = packet.get("feature_classification")
    if not isinstance(block, dict) or not block.get("available"):
        return None
    return read_feature_verdicts(block.get("verdicts"))


def packet_positional_evidence(
    packet: Any,
) -> tuple[list[dict[str, Any]], list[float], float] | None:
    """The per-position curves, their shared grid, and the flat reference.

    ``None`` when any of the three is missing — they are only meaningful
    together, and a boost judged against two of them would be judged against a
    reference that did not come from the same evaluation as the curves.
    """
    if not isinstance(packet, dict):
        return None
    positions = packet.get("positions")
    spec = packet.get("spec")
    if not isinstance(positions, dict) or not isinstance(spec, dict):
        return None
    rows = positions.get("positions")
    grid = (positions.get("curve_grid") or {}).get("freqs_hz")
    reference = spec.get("reference_db")
    if not isinstance(rows, list) or not rows:
        return None
    if not isinstance(grid, list) or not grid:
        return None
    if isinstance(reference, bool) or not isinstance(reference, (int, float)):
        return None
    # `reference` is coerced inside the same guard as the grid: an
    # arbitrary-precision int passes the isinstance check above and then
    # raises on `float()`, so leaving it outside would reintroduce the escape
    # this guard exists to close.
    try:
        freqs = [float(value) for value in grid]
        reference_db = float(reference)
    except (TypeError, ValueError, OverflowError):
        return None
    return ([row for row in rows if isinstance(row, dict)], freqs, reference_db)
