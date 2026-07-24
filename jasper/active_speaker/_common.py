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

Deliberately NOT consolidated here: `_finite_float` and `_level_at_floor`.
`_level_at_floor` genuinely encodes different contracts across modules
(return-None vs raise vs return-default; dict-arg vs float-arg), so those
really are distinct functions. `_finite_float` is not: `calibration_level.py`,
`baseline_profile.py`, `measurement.py`, `safe_playback.py`, and
`commissioning_coordinator.py` all share the same body (return `None` on a
non-finite/unconvertible value), and `driver_protection.py`'s version is the
same logic with an if/return instead of a ternary — that cluster is
duplicated, not distinct, and a candidate for future consolidation.

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

import re
from typing import Any


# An analyzed summed Fc originates from the preset region itself.  This tolerance
# permits only float round-trip noise; it must never bridge a real crossover
# setting change.
REGION_FC_MATCH_TOLERANCE_HZ = 1e-6


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

_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")


def issue(severity: str, code: str, message: str) -> dict[str, str]:
    """A severity-tagged diagnostic record (`blocker`/`warning`/…)."""

    return {"severity": severity, "code": code, "message": message}


def gate(gate_id: str, *, label: str, passed: bool, message: str) -> dict[str, Any]:
    """A named pass/fail readiness gate with an operator-facing label."""

    return {
        "id": gate_id,
        "label": label,
        "passed": bool(passed),
        "message": message,
    }


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
