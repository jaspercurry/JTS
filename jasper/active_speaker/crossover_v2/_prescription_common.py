# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared strict parsing; each prescription judge supplies its refusal codes."""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, NoReturn

RATIONALE_MAX_CHARS = 1_200


class BlendPrescriptionRefused(ValueError):
    """A judge refusal with its code and measured evidence."""

    def __init__(
        self,
        reason: str,
        detail: str,
        *,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.evidence: Mapping[str, Any] = dict(evidence or {})


def _refuse(reason: str, detail: str, **evidence: Any) -> NoReturn:
    raise BlendPrescriptionRefused(reason, detail, evidence=evidence or None)


def _finite_number(value: Any, *, reason: str, field: str) -> float:
    """One numeric field, strictly — no coercion, ever.

    ``bool`` is refused because it is an ``int`` and ``gain=True`` would read as
    a +1 dB boost; strings because ``float("1900")`` succeeds, and
    :func:`~.blend_correction.blend_filters_from_mapping` refuses both.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _refuse(reason, f"{field} must be a number, got {type(value).__name__}")
    try:
        number = float(value)
    except OverflowError:
        # `10 ** 400` is a legal JSON number and a legal Python int that passes
        # the isinstance check, and `float()` raises rather than returning inf.
        # Refusing here keeps it inside the closed vocabulary instead of
        # escaping the gate as an OverflowError.
        _refuse(reason, f"{field} is too large to be a filter coefficient")
    if not math.isfinite(number):
        _refuse(reason, f"{field} must be finite, got {number!r}")
    return number


def _prescriber(raw: Any, *, reason: str) -> tuple[str, str]:
    """Who authored this, strictly and non-blank. Both halves required."""
    if not isinstance(raw, Mapping):
        _refuse(
            reason,
            "a prescription must carry a prescriber object naming its model "
            "and operator",
        )
    unknown = sorted(set(raw) - {"model", "operator"})
    if unknown:
        _refuse(
            reason,
            f"prescriber carries unknown field(s): {', '.join(unknown)}",
        )
    values: list[str] = []
    for field in ("model", "operator"):
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            _refuse(
                reason,
                f"prescriber.{field} must be a non-blank name",
            )
        values.append(" ".join(value.split()))
    return values[0], values[1]


def _rationale(raw: Any, *, reason: str) -> tuple[str, int]:
    """The prescriber's own words, banked to the ceiling, and what was dropped.

    Truncates rather than refusing (ADR-0207), counting the loss onto
    :attr:`BlendPrescription.rationale_dropped_chars`. Still strictly TEXT.
    """
    if raw is None:
        return "", 0
    if not isinstance(raw, str):
        _refuse(
            reason,
            f"rationale must be text, got {type(raw).__name__}",
        )
    text = " ".join(raw.split())
    return text[:RATIONALE_MAX_CHARS], max(0, len(text) - RATIONALE_MAX_CHARS)
