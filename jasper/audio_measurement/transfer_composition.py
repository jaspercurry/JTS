# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Exact linear transfer replacement for protected crossover measurements.

The measurement graph emits a known, role-specific protection transfer ``P``.
The configured crossover fitter consumes the complete desired transfer ``C``
instead.  This module owns the only replacement equation::

    S = M * C / P

``C`` replaces ``P``; the ratio is never emitted to hardware.  The finite and
conditioning policy is intentionally closed and fingerprinted because an
offline replay must reject the same bins as the live fit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.sound.profile import (
    RESPONSE_SAMPLE_RATE_HZ,
    FilterSpec,
    _filter_response_complex,
    _freq_trig,
)

RAW_EVIDENCE_KIND = "jts_crossover_raw_evidence_v1"
SELECTOR_RESULT_KIND = "jts_crossover_selector_result_v1"

CONDITIONING_FLOOR_DB = -12.0
CONDITIONING_RATIO_CAP_DB = 12.0
CONDITIONING_FLOOR_AMPLITUDE = 10.0 ** (CONDITIONING_FLOOR_DB / 20.0)
CONDITIONING_RATIO_CAP_AMPLITUDE = 10.0 ** (
    CONDITIONING_RATIO_CAP_DB / 20.0
)
ILL_CONDITIONED_REASON = "ill_conditioned_protection_deembedding"
NONFINITE_REASON = "non_finite_total_transfer_composition"


class TotalTransferCompositionError(ValueError):
    """The protected measurement cannot be composed into fitter input."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


@dataclass(frozen=True)
class LinearTransferSection:
    """One emitted Linkwitz-Riley protection/configured-transfer section."""

    highpass: bool
    frequency_hz: float
    order: int = 4
    reason: str = ""

    def __post_init__(self) -> None:
        frequency = float(self.frequency_hz)
        if not math.isfinite(frequency) or frequency <= 0.0:
            raise ValueError("transfer section frequency_hz must be finite and positive")
        if type(self.order) is not int or self.order < 2 or self.order % 2:
            raise ValueError("Linkwitz-Riley order must be a positive even integer")
        if not isinstance(self.reason, str):
            raise ValueError("transfer section reason must be a string")
        object.__setattr__(self, "frequency_hz", frequency)

    def to_dict(self) -> dict[str, Any]:
        return {
            "filter_type": "Linkwitz-Riley",
            "direction": "highpass" if self.highpass else "lowpass",
            "frequency_hz": self.frequency_hz,
            "order": self.order,
            "reason": self.reason,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "LinearTransferSection":
        expected = {"filter_type", "direction", "frequency_hz", "order", "reason"}
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise ValueError("linear transfer section schema is invalid")
        if raw["filter_type"] != "Linkwitz-Riley":
            raise ValueError("unsupported linear transfer filter type")
        direction = raw["direction"]
        if direction not in {"highpass", "lowpass"}:
            raise ValueError("unsupported linear transfer direction")
        return cls(
            highpass=direction == "highpass",
            frequency_hz=float(raw["frequency_hz"]),
            order=int(raw["order"]),
            reason=str(raw["reason"]),
        )


def transfer_policy_payload() -> dict[str, Any]:
    """The canonical v1 finite/conditioning policy, including boundaries."""

    core = {
        "schema_version": 1,
        "kind": "jts_crossover_total_transfer_composition_policy",
        "formula": "S=M*C_configured/P",
        "configured_transfer_semantics": "complete_replacement_for_P",
        "hardware_emits_inverse": False,
        "finite_rule": "P,C,C_over_P,S finite on every required trusted bin",
        "protection_floor_db": CONDITIONING_FLOOR_DB,
        "protection_floor_amplitude": CONDITIONING_FLOOR_AMPLITUDE,
        "ratio_cap_db": CONDITIONING_RATIO_CAP_DB,
        "ratio_cap_amplitude": CONDITIONING_RATIO_CAP_AMPLITUDE,
        "boundary_rule": "equal_to_floor_or_cap_is_allowed",
        "conditioning_rejection_reason": ILL_CONDITIONED_REASON,
        "nonfinite_rejection_reason": NONFINITE_REASON,
        "repair_policy": "reject_without_clipping_interpolation_or_bin_omission",
    }
    return {**core, "fingerprint": json_fingerprint(core)}


def _butterworth_qs(order: int) -> tuple[float, ...]:
    return tuple(
        1.0 / (2.0 * math.sin(math.pi * (2 * index + 1) / (2 * order)))
        for index in range(order // 2)
    )


def _first_order_response(
    freqs_hz: np.ndarray, *, frequency_hz: float, highpass: bool
) -> np.ndarray:
    k = math.tan(math.pi * frequency_hz / RESPONSE_SAMPLE_RATE_HZ)
    b0 = (1.0 / (1.0 + k)) if highpass else (k / (1.0 + k))
    b1 = -b0 if highpass else b0
    a1 = (k - 1.0) / (k + 1.0)
    z = np.exp(-1j * 2.0 * np.pi * freqs_hz / RESPONSE_SAMPLE_RATE_HZ)
    return (b0 + b1 * z) / (1.0 + a1 * z)


def linkwitz_riley_response_complex(
    freqs_hz: Sequence[float] | np.ndarray,
    sections: Sequence[LinearTransferSection],
    *,
    polarity_sign: int = 1,
) -> np.ndarray:
    """Complex response of the exact digital LR sections CamillaDSP emits."""

    if polarity_sign not in {-1, 1}:
        raise ValueError("polarity_sign must be -1 or +1")
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    if freqs.ndim != 1 or not np.all(np.isfinite(freqs)):
        raise ValueError("frequency basis must be a finite one-dimensional array")
    if np.any(freqs < 0.0) or np.any(freqs > RESPONSE_SAMPLE_RATE_HZ / 2.0):
        raise ValueError("frequency basis must stay inside the 48 kHz Nyquist interval")
    total = np.ones(freqs.shape, dtype=np.complex128) * polarity_sign
    trig = _freq_trig(freqs)
    for section in sections:
        butterworth_order = section.order // 2
        one_pass = np.ones(freqs.shape, dtype=np.complex128)
        for q in _butterworth_qs(butterworth_order):
            spec = FilterSpec(
                name="transfer",
                biquad_type="Highpass" if section.highpass else "Lowpass",
                freq=section.frequency_hz,
                gain=0.0,
                q=q,
            )
            one_pass *= np.asarray(
                _filter_response_complex(spec, freqs, trig), dtype=np.complex128
            )
        if butterworth_order % 2:
            one_pass *= _first_order_response(
                freqs,
                frequency_hz=section.frequency_hz,
                highpass=section.highpass,
            )
        total *= one_pass * one_pass
    return total


@dataclass(frozen=True)
class TotalTransferComposition:
    """One validated ``M,C,P -> S`` result on a fixed frequency basis."""

    fitter_input: np.ndarray
    protection: np.ndarray
    configured: np.ndarray
    ratio: np.ndarray
    required_mask: np.ndarray
    fingerprints: Mapping[str, str]


def _complex_payload(values: np.ndarray) -> dict[str, Any]:
    array = np.asarray(values, dtype=np.complex128)
    return {
        "real": array.real.astype(np.float64).tolist(),
        "imag": array.imag.astype(np.float64).tolist(),
    }


def complex_fingerprint(values: np.ndarray, *, kind: str) -> str:
    return json_fingerprint({"schema_version": 1, "kind": kind, **_complex_payload(values)})


def mask_fingerprint(mask: np.ndarray, *, kind: str) -> str:
    values = np.asarray(mask, dtype=bool)
    return json_fingerprint(
        {"schema_version": 1, "kind": kind, "mask": values.tolist()}
    )


def compose_total_transfer(
    measured: np.ndarray,
    configured: np.ndarray,
    protection: np.ndarray,
    *,
    required_mask: np.ndarray,
) -> TotalTransferComposition:
    """Return ``S=M*C/P`` after the canonical finite and 12 dB gates.

    Bins outside ``required_mask`` are zeroed in ``fitter_input``.  They are
    outside the declared, captured, trusted role band and therefore are not
    fitter inputs; no required bin is clipped, interpolated, or omitted.
    """

    M = np.asarray(measured, dtype=np.complex128)
    C = np.asarray(configured, dtype=np.complex128)
    P = np.asarray(protection, dtype=np.complex128)
    mask = np.asarray(required_mask, dtype=bool)
    if M.ndim != 1 or C.shape != M.shape or P.shape != M.shape or mask.shape != M.shape:
        raise ValueError("M, C, P, and required_mask must share one one-dimensional shape")
    if not np.any(mask):
        raise ValueError("required_mask must select at least one bin")
    required_p = P[mask]
    required_c = C[mask]
    required_m = M[mask]
    if not (
        np.all(np.isfinite(required_p.real))
        and np.all(np.isfinite(required_p.imag))
        and np.all(np.isfinite(required_c.real))
        and np.all(np.isfinite(required_c.imag))
        and np.all(np.isfinite(required_m.real))
        and np.all(np.isfinite(required_m.imag))
    ):
        raise TotalTransferCompositionError(
            NONFINITE_REASON, "M, C, or P is non-finite on a required trusted bin"
        )
    # Strict comparisons implement the pinned inclusive boundary rule.
    if np.any(np.abs(required_p) < CONDITIONING_FLOOR_AMPLITUDE):
        raise TotalTransferCompositionError(
            ILL_CONDITIONED_REASON,
            "protection magnitude falls below the -12 dB de-embedding floor",
        )
    ratio_required = required_c / required_p
    if not np.all(np.isfinite(ratio_required.real)) or not np.all(
        np.isfinite(ratio_required.imag)
    ):
        raise TotalTransferCompositionError(
            NONFINITE_REASON, "C/P is non-finite on a required trusted bin"
        )
    if np.any(np.abs(ratio_required) > CONDITIONING_RATIO_CAP_AMPLITUDE):
        raise TotalTransferCompositionError(
            ILL_CONDITIONED_REASON,
            "configured/protection ratio exceeds the +12 dB de-embedding cap",
        )
    S_required = required_m * ratio_required
    if not np.all(np.isfinite(S_required.real)) or not np.all(
        np.isfinite(S_required.imag)
    ):
        raise TotalTransferCompositionError(
            NONFINITE_REASON, "S is non-finite on a required trusted bin"
        )

    ratio = np.zeros(M.shape, dtype=np.complex128)
    ratio[mask] = ratio_required
    S = np.zeros(M.shape, dtype=np.complex128)
    S[mask] = S_required
    policy = transfer_policy_payload()
    fingerprints = {
        "protection": complex_fingerprint(P, kind="jts_crossover_protection_transfer"),
        "configured": complex_fingerprint(C, kind="jts_crossover_configured_transfer"),
        "policy": str(policy["fingerprint"]),
        "mask": mask_fingerprint(mask, kind="jts_crossover_transfer_required_mask"),
        "ratio": complex_fingerprint(ratio, kind="jts_crossover_transfer_ratio"),
        "fitter_input": complex_fingerprint(S, kind="jts_crossover_fitter_input"),
    }
    return TotalTransferComposition(
        fitter_input=S,
        protection=P,
        configured=C,
        ratio=ratio,
        required_mask=mask,
        fingerprints=fingerprints,
    )


def serialize_sections(
    sections_by_role: Mapping[str, Sequence[LinearTransferSection]],
) -> dict[str, list[dict[str, Any]]]:
    return {
        role: [section.to_dict() for section in sections]
        for role, sections in sections_by_role.items()
    }


def sections_fingerprint(
    sections_by_role: Mapping[str, Sequence[LinearTransferSection]], *, kind: str
) -> str:
    return json_fingerprint(
        {
            "schema_version": 1,
            "kind": kind,
            "sections_by_role": serialize_sections(sections_by_role),
        }
    )
