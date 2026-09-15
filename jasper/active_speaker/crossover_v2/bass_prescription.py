# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Bind a dynamic-bass descriptor to qualified fundamentals in its round."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jasper.active_speaker.measurement_bass import BASS_BANDS_HZ
from jasper.bass_extension.dynamic import (
    DYNAMIC_BASS_REFUSAL_REASONS, DynamicBassDescriptorError, validate_dynamic_bass_descriptor,
)

from ._prescription_common import BlendPrescriptionRefused, _refuse

BASS_ROUND_MISMATCH = "bass_round_mismatch"
BASS_EVIDENCE_UNAVAILABLE = "bass_evidence_unavailable"
BASS_BAND_UNQUALIFIED = "bass_band_unqualified"
BASS_PRESCRIPTION_REFUSAL_REASONS = DYNAMIC_BASS_REFUSAL_REASONS | {
    BASS_ROUND_MISMATCH, BASS_EVIDENCE_UNAVAILABLE, BASS_BAND_UNQUALIFIED,
}
BassPrescriptionRefused = BlendPrescriptionRefused


def bass_evidence_status(evidence: Mapping[str, Any]) -> dict[str, Any]:
    table = evidence.get("bass_table") or {}
    levels = [{"candidate_id": entry.get("candidate_id"),
               "level_key": level.get("level_key"), "outcome": level.get("outcome")}
              for entry in table.get("tables", []) for level in entry.get("levels", [])]
    return {
        "evidence_status": "evaluated" if evidence.get("bass") or levels else BASS_EVIDENCE_UNAVAILABLE,
        "evidence_status_detail": {
            "levels": levels,
            "target_met_at_every_level": all(level["outcome"] == "target_met" for level in levels) if levels else None,
            **({"code": table["code"]} if "code" in table else {}),
        },
    }


@dataclass(frozen=True)
class BassPrescription:
    descriptor: Mapping[str, Any]
    round_id: str
    packet_fingerprint: str
    evidence_status: str
    evidence_status_detail: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {**self.descriptor, "round_id": self.round_id,
                "packet_fingerprint": self.packet_fingerprint, "evidence_status": self.evidence_status,
                "evidence_status_detail": dict(self.evidence_status_detail)}


def bass_prescription_response_format() -> dict[str, Any]:
    return {
        "required_top_level": {
            "round_id": "copy round_id from packet.json",
            "packet_fingerprint": "copy packet_fingerprint from the same packet.json",
        },
        "refusal_reasons": sorted(BASS_PRESCRIPTION_REFUSAL_REASONS),
    }


def read_bass_prescription(raw: Any, *, round_id: str, packet_fingerprint: str | None,
                           evidence: Mapping[str, Any]) -> BassPrescription:
    status = bass_evidence_status(evidence)
    if status["evidence_status"] != "evaluated":
        _refuse(BASS_EVIDENCE_UNAVAILABLE, "The round has no bass evidence.", round_id=round_id)
    try:
        descriptor = validate_dynamic_bass_descriptor({key: value for key, value in raw.items()
                                                       if key not in {"round_id", "packet_fingerprint"}}
                                                      if isinstance(raw, Mapping) else raw)
    except DynamicBassDescriptorError as exc:
        _refuse(exc.reason, str(exc), field=exc.field)
    if (not round_id or not packet_fingerprint
            or evidence.get("round_id") != round_id or evidence.get("packet_fingerprint") != packet_fingerprint
            or raw.get("round_id") != round_id or raw.get("packet_fingerprint") != packet_fingerprint):
        _refuse(BASS_ROUND_MISMATCH, "The bass prescription must name this round and packet.",
                round_id=round_id, packet_fingerprint=packet_fingerprint)
    lower = max(BASS_BANDS_HZ[0][0], descriptor["delta_highpass_hz"] or BASS_BANDS_HZ[0][0])
    upper = descriptor["detector_lowpass_hz"]
    bands = [(lo, hi) for lo, hi in BASS_BANDS_HZ if lo < upper and hi > lower]
    qualified = {tuple(band["band_hz"]) for view in evidence.get("bass", [])
                 for take in view.get("takes", []) for band in take.get("bands", [])
                 if band.get("fundamental_qualified") is True}
    for band in bands or [BASS_BANDS_HZ[0]]:
        if band not in qualified:
            _refuse(BASS_BAND_UNQUALIFIED, f"No qualified fundamental in {band[0]:g}-{band[1]:g} Hz.",
                    band_hz=list(band), boost_band_hz=[lower, upper])
    return BassPrescription(descriptor, round_id, packet_fingerprint, **status)
