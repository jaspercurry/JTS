# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Admit a dynamic-bass descriptor and disclose the bands no bass round qualified."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jasper.active_speaker.bass_table_report import bass_table_rows
from jasper.active_speaker.measurement_bass import BASS_BANDS_HZ
from jasper.bass_extension.dynamic import (
    DYNAMIC_BASS_REFUSAL_REASONS, DynamicBassDescriptorError, validate_dynamic_bass_descriptor,
)

from ._prescription_common import _refuse

BASS_EVIDENCE_UNAVAILABLE = "bass_evidence_unavailable"


def _bound(evidence: Mapping[str, Any]) -> Mapping[str, Any]:
    """Only evidence that names its round counts."""
    return evidence if evidence.get("round_id") else {}


def bass_evidence_status(evidence: Mapping[str, Any]) -> dict[str, Any]:
    bound = _bound(evidence)
    table = bound.get("bass_table") or {}
    levels = bass_table_rows(table)
    code = table.get("code", evidence.get("code"))
    return {
        "evidence_status": "evaluated" if bound.get("bass") or levels else BASS_EVIDENCE_UNAVAILABLE,
        "evidence_status_detail": {"levels": levels, **({"code": code} if code is not None else {})},
    }


@dataclass(frozen=True)
class BassPrescription:
    descriptor: Mapping[str, Any]
    round_id: str | None
    evidence: Mapping[str, Any]
    unqualified_boost_bands_hz: list[list[float]]
    answers_round: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {**self.descriptor, "round_id": self.round_id, "answers_round": self.answers_round,
                **self.evidence, "unqualified_boost_bands_hz": self.unqualified_boost_bands_hz}


def bass_prescription_response_format() -> dict[str, Any]:
    return {
        "optional_top_level": {
            "round_id": "evidence echo; a mismatch is disclosed as answers_round=false",
        },
        "refusal_reasons": sorted(DYNAMIC_BASS_REFUSAL_REASONS),
    }


def read_bass_prescription(raw: Any, *, evidence: Mapping[str, Any]) -> BassPrescription:
    try:
        descriptor = validate_dynamic_bass_descriptor({key: value for key, value in raw.items()
                                                       if key != "round_id"}
                                                      if isinstance(raw, Mapping) else raw, new_section=True)
    except DynamicBassDescriptorError as exc:
        _refuse(exc.reason, str(exc), field=exc.field)
    bound = _bound(evidence)
    round_id = bound.get("round_id")
    lower = max(BASS_BANDS_HZ[0][0], descriptor["delta_highpass_hz"])
    upper = descriptor["detector_lowpass_hz"]
    bands = [(lo, hi) for lo, hi in BASS_BANDS_HZ if lo < upper and hi > lower]
    qualified = {tuple(band["band_hz"]) for view in bound.get("bass") or ()
                 for take in view.get("takes", []) for band in take.get("bands", [])
                 if band.get("fundamental_qualified") is True}
    # At the allowed 20 Hz detector minimum, the first measured band supplies the endpoint.
    unqualified = [list(band) for band in bands or [BASS_BANDS_HZ[0]] if band not in qualified]
    echo = raw.get("round_id")
    return BassPrescription(descriptor, round_id, bass_evidence_status(evidence), unqualified,
                            answers_round=None if echo is None else echo == round_id)
