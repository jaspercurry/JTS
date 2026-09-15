# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Bind a dynamic-bass descriptor to its round and disclose unqualified bands."""

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
BASS_PRESCRIPTION_REFUSAL_REASONS = DYNAMIC_BASS_REFUSAL_REASONS | {
    BASS_ROUND_MISMATCH, BASS_EVIDENCE_UNAVAILABLE,
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
    evidence_status: str
    unqualified_boost_bands_hz: list[list[float]]

    def to_dict(self) -> dict[str, Any]:
        return {**self.descriptor, "round_id": self.round_id,
                "evidence_status": self.evidence_status,
                "unqualified_boost_bands_hz": self.unqualified_boost_bands_hz}


def bass_prescription_response_format() -> dict[str, Any]:
    return {
        "required_top_level": {
            "round_id": "copy round_id from packet.json",
        },
        "refusal_reasons": sorted(BASS_PRESCRIPTION_REFUSAL_REASONS),
    }


def read_bass_prescription(raw: Any, *, evidence: Mapping[str, Any]) -> BassPrescription:
    try:
        descriptor = validate_dynamic_bass_descriptor({key: value for key, value in raw.items()
                                                       if key != "round_id"}
                                                      if isinstance(raw, Mapping) else raw)
    except DynamicBassDescriptorError as exc:
        _refuse(exc.reason, str(exc), field=exc.field)
    round_id = evidence.get("round_id")
    status = bass_evidence_status(evidence)["evidence_status"]
    if not round_id or status != "evaluated":
        _refuse(BASS_EVIDENCE_UNAVAILABLE, "The round has no bass evidence.", round_id=round_id)
    if raw.get("round_id") != round_id:
        _refuse(BASS_ROUND_MISMATCH, "The bass prescription must name this round.", round_id=round_id)
    lower = max(BASS_BANDS_HZ[0][0], descriptor["delta_highpass_hz"] or BASS_BANDS_HZ[0][0])
    upper = descriptor["detector_lowpass_hz"]
    bands = [(lo, hi) for lo, hi in BASS_BANDS_HZ if lo < upper and hi > lower]
    qualified = {tuple(band["band_hz"]) for view in evidence.get("bass", [])
                 for take in view.get("takes", []) for band in take.get("bands", [])
                 if band.get("fundamental_qualified") is True}
    # At the allowed 20 Hz detector minimum, the first measured band supplies the endpoint.
    unqualified = [list(band) for band in bands or [BASS_BANDS_HZ[0]] if band not in qualified]
    return BassPrescription(descriptor, round_id, status, unqualified)
