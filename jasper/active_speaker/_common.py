# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared diagnostics vocabulary for the active-speaker commissioning flow.

`issue` and `gate` were duplicated byte-for-byte across the active_speaker
modules. They are consolidated here as the one issue/gate vocabulary, with their
shape unchanged — plain dicts the web, `/state`, and doctor surfaces already
serialize — so the dedup is purely structural, not behavioural. Consumers import
them aliased to their existing private names (`from ._common import issue as
_issue, gate as _gate`) so call sites stay identical.

`finite_float` owns the modules' shared nullable coercion contract (return
`None` for non-finite or unconvertible values). Deliberately NOT consolidated
here: `_level_at_floor`, whose module-specific variants genuinely encode
different contracts (return-None vs raise vs return-default; dict-arg vs
float-arg).

`require_sha256_hex` replaces the module-local `_sha256(value, field_name)`
hex-fingerprint checks — a regex `fullmatch` in some modules, an equivalent
char-loop in others, always the same 64-lowercase-hex-digit rule — with one
validator. Callers keep their own exception type and exact message wording
via `exc_type`/`message`, so every call site raises byte-for-byte what it did
before. `commissioning_host.py`'s `_sha256` is deliberately NOT migrated
here: `CommissioningHostError` takes two required positional args (`code`,
`detail`), not the single message string every other call site's exception
takes, so routing it through this helper would mean growing the shared
signature to serve one outlier.

This module is import-cheap (stdlib only), preserving the package's IO-free,
import-light contract.
"""

from __future__ import annotations

import math
import re
from typing import Any


# An analyzed summed Fc originates from the preset region itself.  This tolerance
# permits only float round-trip noise; it must never bridge a real crossover
# setting change.
REGION_FC_MATCH_TOLERANCE_HZ = 1e-6


# Why the commissioning eligibility receipt did not vouch for the applied
# automatic crossover.  Five distinguishable causes, five remedies; room
# correction runs in all five (ADR-0019, ruling S10) — these name what cannot
# be CLAIMED, never what is refused.  What each one means and how the reader
# decides between them: ADR-0196.
ROOM_AUTHORITY_RECEIPT_ABSENT = "active_commissioning_receipt_absent"
ROOM_AUTHORITY_RECEIPT_STALE = "active_commissioning_receipt_stale"
ROOM_AUTHORITY_RECEIPT_MALFORMED = "active_commissioning_receipt_malformed"
ROOM_AUTHORITY_RECEIPT_SUPERSEDED = "active_commissioning_receipt_superseded"
ROOM_AUTHORITY_RECEIPT_UNREADABLE = "active_commissioning_receipt_unreadable"


# The saved topology no longer hashes to what the applied baseline was minted
# against.  A DISCLOSURE, not a blocker (ruling S10, ADR-0019): playback stays
# on the applied graph and measuring stays open.
BASELINE_TOPOLOGY_CHANGED = "active_baseline_topology_changed"


ACTIVE_CROSSOVER_ROLE_PAIRS: dict[str, tuple[tuple[str, str], ...]] = {
    "active_2_way": (("woofer", "tweeter"),),
    "active_3_way": (("woofer", "mid"), ("mid", "tweeter")),
}

# The closed driver-technology vocabulary (design doc "Microphone doctrine" /
# artifact 02 §5's driver-class table). Hoisted here (#1665) from
# linearization_envelope.py so component-entry code (design_draft.py's schema,
# the /sound/ wizard's declared "driver type" pick) and the correction-envelope
# math (linearization_envelope.compose_envelope's class_prior_limit term) share
# one vocabulary without either side importing the other's module.
# linearization_envelope re-exports this name so its own callers/tests are
# unaffected.
DRIVER_CLASSES: tuple[str, ...] = (
    "compression_horn",
    "soft_dome",
    "metal_dome",
    "beryllium_diamond_dome",
    "ribbon_amt",
    "unknown",
)

# Per-driver keys retired from the component-entry schema that a record saved
# by an older build can still carry. Every gate that re-validates a stored
# driver record TOLERATES them and every normaliser DROPS them: refusing would
# make a draft the operator saved before the deletion unsaveable, over a field
# the wizard no longer shows them. Nothing reads these, and nothing stores them
# again -- a key here is gone, not deprecated. It shrinks only when no draft in
# the field can still carry the key, which is not observable from here, so
# treat the set as append-only.
#
# horn_coverage_deg (#2872): collected by /sound/setup/ for the Bessel
# beamwidth matcher #1675 was going to build. #1675 closed 2026-08-08 having
# built ka beaming guidance off the woofer's radiating_diameter_mm instead, so
# the coverage angle never gained a reader. Waveguide identity and coverage now
# travel as operator prose in the driver notes.
#
# crossover_search_band_hz (#2870): the declared range a driver could be crossed
# over IN. Introduced to bound R17's corner hunt, it outlived that hunt's
# deletion and its only remaining effect was to refuse corners both drivers'
# declared HARD excitation bands admit -- a bound naming no damage mechanism.
# It differs from the key above in one way that matters here: it was REQUIRED
# (``crossover_search_band_missing`` blocked confirmation), so EVERY box
# confirmed before the ruling carries it, not just some.
#
# ADDING A KEY HERE IS ONLY HALF THE DECISION. Two other places answer their own
# questions about a retired key, and neither is reachable from this set:
#   * ``driver_safety._RETIRED_TARGET_FIELDS`` -- does a stored safety PROFILE
#     carrying it read as stale-but-fixable, or as corrupt? (Opposite
#     disposition to this set: reported, never dropped.)
#   * ``driver_safety.validate_driver_research_request`` -- a stored research
#     request's ``operator_declared_context`` is FINGERPRINTED, so tolerating
#     the key in an allowlist is not enough on its own; that digest has to stay
#     acceptable and be re-stamped.
# Decide all three, or an old draft passes one gate and is refused at the next.
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
    """Return ``value`` as a finite float, or ``None`` when unusable."""

    try:
        out = float(value)
    except (TypeError, ValueError):
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
    """Validate ``value`` is a 64-character lowercase-hex SHA-256 fingerprint.

    Returns ``value`` unchanged when it validates. Otherwise raises
    ``exc_type(message)``, defaulting ``message`` to
    ``"<field_name> must be a lowercase SHA-256 fingerprint"`` — the wording
    most call sites already used — so most callers only need to pass their
    own exception type. A call site whose historical wording differs (no
    "fingerprint" suffix) passes ``message`` explicitly to keep raising
    exactly what it did before consolidation.
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

    Shared between ``measurement.py`` (writes ``latest_summed_pairs_by_group``
    keyed by this) and ``commissioning_capture.py`` (reads it back to resolve
    a region's paired in-phase/reverse evidence) — the two sides must agree
    on the exact format, so it lives here once rather than as a duplicated
    f-string.
    """

    return f"{lower_role}:{upper_role}"
