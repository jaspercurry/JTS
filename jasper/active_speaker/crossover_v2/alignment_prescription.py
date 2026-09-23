# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Parse inter-driver delay and disclose its residual against an optional basis."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any, Mapping

from jasper.audio_measurement.program_analysis import half_period_us
from jasper.json_fields import finite_float

from ._prescription_common import (
    PRESCRIPTION_MALFORMED as _PRESCRIPTION_MALFORMED,
    BlendPrescriptionRefused,
    _finite_number,
    _read_artifacts,
)
from .contracts import POLARITY_INVERT, POLARITY_KEEP

__all__ = [
    "ALIGNMENT_NO_CROSSOVER_REGION",
    "ALIGNMENT_PRESCRIPTION_KEY",
    "ALIGNMENT_PRESCRIPTION_KIND",
    "ALIGNMENT_PRESCRIPTION_MALFORMED",
    "ALIGNMENT_PRESCRIPTION_REFUSAL_REASONS",
    "ALIGNMENT_PRESCRIPTION_SCHEMA_UNSUPPORTED",
    "ALIGNMENT_PRESCRIPTION_SCHEMA_VERSION",
    "AlignmentPrescription",
    "AlignmentPrescriptionRefused",
    "alignment_delay_search_bounds_us",
    "alignment_prescription_response_format",
    "read_alignment_prescription",
]

# Milliseconds added to each declared window edge for the delay search.
ALIGNMENT_DELAY_PLAUSIBILITY_MARGIN_MS = 0.1


def _declared_alignment_delay_range_ms(
    source_preset: Any,
) -> tuple[Any, float, float] | None:
    """Return the single v2 region plus its valid declared delay range."""
    regions = getattr(source_preset, "crossover_regions", None)
    if not regions:
        return None
    region = regions[0]
    delay_range_ms = getattr(region, "delay_range_ms", None)
    if not (isinstance(delay_range_ms, (tuple, list)) and len(delay_range_ms) == 2):
        return None
    lo_ms, hi_ms = float(delay_range_ms[0]), float(delay_range_ms[1])
    if not (math.isfinite(lo_ms) and math.isfinite(hi_ms)) or lo_ms > hi_ms:
        return None
    return region, lo_ms, hi_ms


def alignment_delay_search_bounds_us(
    source_preset: Any,
    *,
    margin_ms: float = ALIGNMENT_DELAY_PLAUSIBILITY_MARGIN_MS,
) -> tuple[float, float] | None:
    """Flatness-search magnitude bounds from the preset's declaration."""
    declared = _declared_alignment_delay_range_ms(source_preset)
    if declared is None:
        return None
    _region, lo_ms, hi_ms = declared
    lo_ms = max(0.0, lo_ms - margin_ms)
    hi_ms += margin_ms
    return lo_ms * 1000.0, hi_ms * 1000.0


#: The request-body key a prescription arrives under.
ALIGNMENT_PRESCRIPTION_KEY = "alignment_prescription"

#: A document naming another version is refused, never best-effort parsed.
ALIGNMENT_PRESCRIPTION_SCHEMA_VERSION = 1

#: The ``kind`` discriminator, distinct from every sibling class's own string.
ALIGNMENT_PRESCRIPTION_KIND = "jts_crossover_alignment_prescription"

#: The closed refusal vocabulary: a caller branches on a code, never on prose.
ALIGNMENT_PRESCRIPTION_MALFORMED = _PRESCRIPTION_MALFORMED
PRESCRIPTION_DELAY_INVALID = "prescription_delay_invalid"
PRESCRIPTION_FC_UNKNOWN = "prescription_fc_unknown"
#: A way-1 speaker: no corner and no second driver, so nothing to align. Its
#: own reason because an unknown corner is a number to go and derive while this
#: one never exists (#3480).
ALIGNMENT_NO_CROSSOVER_REGION = "alignment_no_crossover_region"
PRESCRIPTION_OUT_OF_LOBE = "prescription_out_of_lobe"
#: The preset's own declared delay window — the one bound here that does not
#: depend on a number the operator supplied.
PRESCRIPTION_OUTSIDE_DECLARED_WINDOW = "prescription_outside_declared_window"
#: A ``polarity`` outside the candidate's two action words. Its own reason so a
#: misspelled basin sends the operator to the vocabulary, not to the shape.
PRESCRIPTION_POLARITY_INVALID = "prescription_polarity_invalid"
ALIGNMENT_PRESCRIPTION_SCHEMA_UNSUPPORTED = "alignment_prescription_schema_unsupported"
ALIGNMENT_PRESCRIPTION_REFUSAL_REASONS = frozenset({
    ALIGNMENT_PRESCRIPTION_MALFORMED,
    PRESCRIPTION_DELAY_INVALID,
    ALIGNMENT_NO_CROSSOVER_REGION,
    PRESCRIPTION_OUTSIDE_DECLARED_WINDOW,
    PRESCRIPTION_POLARITY_INVALID,
    ALIGNMENT_PRESCRIPTION_SCHEMA_UNSUPPORTED,
})

_PRESCRIPTION_FIELDS = frozenset({
    "kind",
    "artifact_schema_version",
    "delay_us",
    "basis_delay_us",
    "basis_artifacts",
    "basis_note",
    # Optional; absent is the automatic path.
    "polarity",
    # Not required by the gate, but tolerated rather than refused as unknown:
    # a request that echoes a receipt this gate already emitted is harmless,
    # since the gate overwrites the first two with what it actually checked
    # and ``residual_us``/``out_of_lobe`` are properties, so the values are
    # never read.
    "checked_at_fc_hz",
    "lobe_us",
    "residual_us",
    "out_of_lobe",
})


#: One refusal class for the whole prescription family (:mod:`._prescription_common`),
#: so a caller's one ``except`` and the CLI's one handler cover every door.
AlignmentPrescriptionRefused = BlendPrescriptionRefused


@dataclass(frozen=True)
class AlignmentPrescription:
    """Signed delay (D_woofer - D_tweeter): positive delays the tweeter.

    The named measurement supplies the basis, not the incumbent delay.
    """

    delay_us: float
    basis_delay_us: float | None
    basis_artifacts: tuple[str, ...]
    basis_note: str = ""
    polarity: str | None = None
    checked_at_fc_hz: float | None = None
    lobe_us: float | None = None

    @property
    def polarity_sign(self) -> int | None:
        """The pin in the MEASUREMENT frame, or ``None`` when unpinned."""
        if self.polarity is None:
            return None
        return -1 if self.polarity == POLARITY_INVERT else 1

    @property
    def residual_us(self) -> float | None:
        return self.delay_us - self.basis_delay_us if self.basis_delay_us is not None else None

    @property
    def out_of_lobe(self) -> bool | None:
        if self.residual_us is None or self.lobe_us is None:
            return None
        return abs(self.residual_us) > self.lobe_us

    def to_dict(self) -> dict[str, Any]:
        """The receipt's view: what was prescribed, and what justifies it."""
        return {
            "artifact_schema_version": ALIGNMENT_PRESCRIPTION_SCHEMA_VERSION,
            "kind": ALIGNMENT_PRESCRIPTION_KIND,
            "delay_us": self.delay_us,
            "basis_delay_us": self.basis_delay_us,
            "residual_us": self.residual_us,
            "basis_artifacts": list(self.basis_artifacts),
            "basis_note": self.basis_note,
            "polarity": self.polarity,
            "checked_at_fc_hz": self.checked_at_fc_hz,
            "lobe_us": self.lobe_us,
            "out_of_lobe": self.out_of_lobe,
        }


def _parse_prescription(raw: Mapping[str, Any]) -> AlignmentPrescription:
    """The shape and the provenance, and NOT the bound."""
    if not isinstance(raw, Mapping):
        raise AlignmentPrescriptionRefused(
            ALIGNMENT_PRESCRIPTION_MALFORMED,
            f"a prescription must be a mapping, got {type(raw).__name__}",
        )
    unknown = sorted(set(raw) - _PRESCRIPTION_FIELDS)
    if unknown:
        raise AlignmentPrescriptionRefused(
            ALIGNMENT_PRESCRIPTION_MALFORMED,
            f"unknown prescription field(s): {', '.join(unknown)}",
        )
    if raw.get("kind") != ALIGNMENT_PRESCRIPTION_KIND:
        raise AlignmentPrescriptionRefused(
            ALIGNMENT_PRESCRIPTION_MALFORMED,
            f"a prescription must name kind={ALIGNMENT_PRESCRIPTION_KIND!r}, "
            f"got {raw.get('kind')!r}",
        )
    version = raw.get("artifact_schema_version")
    if version != ALIGNMENT_PRESCRIPTION_SCHEMA_VERSION:
        raise AlignmentPrescriptionRefused(
            ALIGNMENT_PRESCRIPTION_SCHEMA_UNSUPPORTED,
            f"this build speaks alignment-prescription schema "
            f"{ALIGNMENT_PRESCRIPTION_SCHEMA_VERSION}, got {version!r}",
        )
    if "delay_us" not in raw:
        raise AlignmentPrescriptionRefused(
            PRESCRIPTION_DELAY_INVALID, "a prescription must state delay_us",
        )
    delay_us = _finite_number(
        raw["delay_us"], reason=PRESCRIPTION_DELAY_INVALID, field="delay_us",
    )
    basis_delay_us = finite_float(raw.get("basis_delay_us"))
    artifacts = _read_artifacts(raw.get("basis_artifacts"))
    note = raw.get("basis_note", "")
    return AlignmentPrescription(
        delay_us=delay_us,
        basis_delay_us=basis_delay_us,
        basis_artifacts=artifacts,
        basis_note=note if isinstance(note, str) else "",
        polarity=_read_polarity(raw.get("polarity")),
        checked_at_fc_hz=finite_float(raw.get("checked_at_fc_hz")),
        lobe_us=finite_float(raw.get("lobe_us")),
    )


_PINNABLE_POLARITIES = frozenset({POLARITY_KEEP, POLARITY_INVERT})


def _read_polarity(value: Any) -> str | None:
    """The optional basin pin, strictly, or ``None`` for the automatic path.

    ``None`` and absent are one answer here, unlike ``delay_us``.
    """
    if value is None:
        return None
    # ``isinstance`` before membership: a JSON list is unhashable, so
    # ``value in frozenset`` would raise TypeError past every refusal handler.
    if not isinstance(value, str) or value not in _PINNABLE_POLARITIES:
        raise AlignmentPrescriptionRefused(
            PRESCRIPTION_POLARITY_INVALID,
            f"polarity must be one of {sorted(_PINNABLE_POLARITIES)} or absent, "
            f"got {value!r}",
        )
    return value


def read_alignment_prescription(
    raw: Mapping[str, Any] | None,
    *,
    fc_hz: float | None,
    declared_bounds_us: tuple[float, float] | None,
    way_count: int | None = None,
) -> AlignmentPrescription | None:
    """Check the preset's declared delay window and disclose the optional lobe.

    ``declared_bounds_us`` is the preset's unsigned delay-magnitude window,
    margin-expanded by ``alignment_delay_search_bounds_us``. ``None`` means
    the preset declares no window. A way-1 speaker has nothing to align.
    """
    if raw is None:
        return None
    # Before the parse and the corner: on a way-1 speaker any other answer
    # sends a prescriber to re-derive a number that cannot exist.
    if way_count == 1:
        raise AlignmentPrescriptionRefused(
            ALIGNMENT_NO_CROSSOVER_REGION,
            "this speaker is full_range_passive (way-1): it has no crossover "
            "region, so there is no handoff for a delay to align",
        )
    prescription = _parse_prescription(raw)
    corner = finite_float(fc_hz)
    if corner is not None and corner <= 0.0:
        corner = None
    if declared_bounds_us is not None:
        lo_us, hi_us = (abs(float(b)) for b in declared_bounds_us)
        lo_us, hi_us = min(lo_us, hi_us), max(lo_us, hi_us)
        magnitude_us = abs(prescription.delay_us)
        if not (lo_us <= magnitude_us <= hi_us):
            raise AlignmentPrescriptionRefused(
                PRESCRIPTION_OUTSIDE_DECLARED_WINDOW,
                f"{prescription.delay_us:.1f} us is {magnitude_us:.1f} us of "
                f"delay, outside the preset's declared window of "
                f"{lo_us:.1f}-{hi_us:.1f} us",
            )
    return replace(prescription, checked_at_fc_hz=corner,
                   lobe_us=half_period_us(corner) if corner is not None else None)


def alignment_prescription_response_format() -> dict[str, Any]:
    """What a prescriber must send to pin the inter-driver delay, and where."""
    return {
        "key": ALIGNMENT_PRESCRIPTION_KEY,
        "entry": "request_body",
        "entry_detail": (
            "staged as the '" + ALIGNMENT_PRESCRIPTION_KEY + "' section through "
            "jasper-crossover-prescriber judge|compose"
        ),
        "severity": (
            "a refused prescription refuses the whole session at the tap; it "
            "is never clamped to the nearest legal delay and never partially "
            "applied"
        ),
        "fields": {
            "kind": f"required, must be exactly {ALIGNMENT_PRESCRIPTION_KIND!r}",
            "artifact_schema_version": (
                "required, must be exactly "
                f"{ALIGNMENT_PRESCRIPTION_SCHEMA_VERSION}"
            ),
            "delay_us": (
                "required number, signed (D_woofer - D_tweeter): positive "
                "delays the tweeter, negative delays the woofer"
            ),
            "basis_delay_us": (
                "optional number, the delay the named measurement says would "
                "leave the drivers coincident"
            ),
            "basis_artifacts": (
                "optional list of names — what this delay was "
                "measured from"
            ),
            "basis_note": "optional human line beside the artifacts",
            "polarity": (
                "optional, one of "
                + ", ".join(sorted(_PINNABLE_POLARITIES))
                + " — pins the basin the automatic objective would otherwise "
                "solve; absent leaves it to the objective"
            ),
        },
        "bound": "delay_us must stay inside the preset's declared delay window",
        "disclosures": "residual_us, lobe_us and out_of_lobe compare the optional basis at this round's corner",
        "refusals": sorted(ALIGNMENT_PRESCRIPTION_REFUSAL_REASONS),
    }
