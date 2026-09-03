# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared diagnostics vocabulary and coercions for the active-speaker package.

The one issue/gate vocabulary (plain dicts the web, `/state`, and doctor
surfaces already serialize), the shared nullable coercions, and the constant
vocabularies two sides must agree on without importing each other.

Stdlib only: the package's IO-free, import-light contract depends on it.
"""

from __future__ import annotations

import math
import re
from typing import Any


# Float round-trip noise only; must never bridge a real crossover setting change.
REGION_FC_MATCH_TOLERANCE_HZ = 1e-6


# Why the commissioning eligibility receipt did not vouch for the applied
# automatic crossover. These name what cannot be CLAIMED, never what is refused:
# room correction runs in all five (ADR-0019). Reader's guide: ADR-0196.
ROOM_AUTHORITY_RECEIPT_ABSENT = "active_commissioning_receipt_absent"
ROOM_AUTHORITY_RECEIPT_STALE = "active_commissioning_receipt_stale"
ROOM_AUTHORITY_RECEIPT_MALFORMED = "active_commissioning_receipt_malformed"
ROOM_AUTHORITY_RECEIPT_SUPERSEDED = "active_commissioning_receipt_superseded"
ROOM_AUTHORITY_RECEIPT_UNREADABLE = "active_commissioning_receipt_unreadable"


# The saved topology no longer hashes to what the applied baseline was minted
# against. A DISCLOSURE, not a blocker (ADR-0019).
BASELINE_TOPOLOGY_CHANGED = "active_baseline_topology_changed"


ACTIVE_CROSSOVER_ROLE_PAIRS: dict[str, tuple[tuple[str, str], ...]] = {
    "active_2_way": (("woofer", "tweeter"),),
    "active_3_way": (("woofer", "mid"), ("mid", "tweeter")),
}

# Closed vocabulary shared by component entry (design_draft.py's schema, the
# /sound/ wizard's driver-type pick) and the correction-envelope math
# (linearization_envelope.compose_envelope's class_prior_limit term), so neither
# side imports the other. linearization_envelope re-exports the name.
DRIVER_CLASSES: tuple[str, ...] = (
    "compression_horn",
    "soft_dome",
    "metal_dome",
    "beryllium_diamond_dome",
    "ribbon_amt",
    "unknown",
)

# Per-driver keys retired from the component-entry schema that an older saved
# record can still carry: every gate TOLERATES them, every normaliser DROPS
# them. Append-only.
#
# ADDING A KEY HERE IS ONLY HALF THE DECISION — two unreachable-from-here places
# answer their own question about a retired key:
# ``driver_safety._RETIRED_TARGET_FIELDS`` (stale-but-fixable vs corrupt;
# reported, never dropped) and
# ``driver_safety.validate_driver_research_request`` (the FINGERPRINTED
# ``operator_declared_context`` digest must stay acceptable and be re-stamped).
LEGACY_DROPPED_DRIVER_FIELDS: frozenset[str] = frozenset({
    "horn_coverage_deg",
    "crossover_search_band_hz",
})

_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")


def issue(severity: str, code: str, message: str) -> dict[str, str]:
    """A severity-tagged diagnostic record (`blocker`/`warning`/…)."""

    return {"severity": severity, "code": code, "message": message}


def blocker_issue(code: str, message: str) -> dict[str, str]:
    """A blocker diagnostic for fail-closed operator paths."""

    return issue("blocker", code, message)


def gate(gate_id: str, *, label: str, passed: bool, message: str) -> dict[str, Any]:
    """A named pass/fail readiness gate with an operator-facing label."""

    return {
        "id": gate_id,
        "label": label,
        "passed": bool(passed),
        "message": message,
    }


def finite_float(value: Any) -> float | None:
    """Return ``value`` as a finite float, or ``None`` when unusable.

    The COERCING reader: unlike :func:`jasper.json_fields.finite_float` it
    accepts a numeric string and a ``bool``. ``OverflowError`` is caught because
    an arbitrary-precision ``int`` is legal JSON and raises rather than
    returning ``inf``.
    """

    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def bounded_int(value: Any, *, default: int, lo: int, hi: int) -> int:
    """Coerce an integer and clamp it to the inclusive ``lo``/``hi`` range."""

    try:
        out = int(value)
    except (TypeError, ValueError):
        out = default
    return min(max(out, lo), hi)


def require_sha256_hex(
    value: Any,
    field_name: str,
    exc_type: type[BaseException],
    *,
    message: str | None = None,
) -> str:
    """Return ``value`` if it is a 64-character lowercase-hex SHA-256 digest.

    Otherwise raises ``exc_type(message)``; ``message`` defaults to the wording
    most call sites use, and a call site needing different wording passes it.
    """

    if isinstance(value, str) and _SHA256_HEX_RE.fullmatch(value) is not None:
        return value
    raise exc_type(
        message
        if message is not None
        else f"{field_name} must be a lowercase SHA-256 fingerprint"
    )


def region_key(lower_role: str, upper_role: str) -> str:
    """The join key one crossover region's paired evidence is grouped under.

    ``measurement.py`` writes ``latest_summed_pairs_by_group`` keyed by this and
    ``commissioning_capture.py`` reads it back; the format must match exactly.
    """

    return f"{lower_role}:{upper_role}"
