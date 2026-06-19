"""Single-source verification vocabulary for active-speaker CamillaDSP graphs.

The active-speaker safety verifiers — ``runtime_contract`` (re-proves an emitted
graph before it loads), ``staging`` (proves the staged candidate text and the
read-back running graph), and ``commission_ramp`` (the Stage-5 live gate) — each
re-parse a CamillaDSP config and re-prove the protective invariants
*independently of the emitter*. That independence is deliberate and stays; what
must NOT be duplicated is (a) the filter-name spellings the emitter writes and
(b) the small, shape-generic scalar/filter accessors every verifier needs.

Before this module those were copied: three verifiers hardcoded
``"as_tweeter_protective_hp"`` / ``"as_tweeter_startup_limiter"`` and
``runtime_contract`` re-derived the commission-mute and baseline names, while
``_float_matches`` and the filter accessors were re-implemented verbatim. A
single name change in the emitter could then silently desync a verifier — it
would look for a filter that no longer exists and fail closed, spuriously
blocking commissioning.

This module is the verification side's single import point. It re-exposes the
emitter's canonical filter names (``camilla_yaml`` owns the spellings — see the
public aliases there) and owns the shared predicates/accessors. The graph shape
both consume is a parsed CamillaDSP config mapping::

    {"filters": {name: {"type": ..., "parameters": {...}}},
     "pipeline": [{"type": "Filter", "channels": [...]|"channel": N,
                   "names": [...]}]}
"""

from __future__ import annotations

from typing import Any

from .camilla_yaml import (
    driver_baseline_gain_name,
    driver_baseline_limiter_name,
    driver_limiter_name,
    driver_mute_name,
    output_commission_mute_name,
    protective_tweeter_hp_name,
)

__all__ = [
    # Canonical filter names (re-exported from the emitter, the single owner).
    "driver_baseline_gain_name",
    "driver_baseline_limiter_name",
    "driver_limiter_name",
    "driver_mute_name",
    "output_commission_mute_name",
    "protective_tweeter_hp_name",
    # Shared accessors / predicates.
    "filter_spec",
    "filter_params",
    "filter_type",
    "float_matches",
    "float_value",
    "truthy_bool",
]


def filter_spec(payload: dict[str, Any], name: str) -> dict[str, Any]:
    """The ``filters[name]`` mapping, or ``{}`` if absent/malformed."""
    filters = payload.get("filters")
    raw = filters.get(name) if isinstance(filters, dict) else None
    return raw if isinstance(raw, dict) else {}


def filter_params(payload: dict[str, Any], name: str) -> dict[str, Any]:
    """The ``filters[name].parameters`` mapping, or ``{}``."""
    params = filter_spec(payload, name).get("parameters")
    return params if isinstance(params, dict) else {}


def filter_type(payload: dict[str, Any], name: str) -> str | None:
    """The ``filters[name].type`` as a string, or ``None``."""
    raw = filter_spec(payload, name).get("type")
    return str(raw) if raw is not None else None


def float_matches(value: Any, expected: float) -> bool:
    """True when ``value`` parses to a float within 1e-4 of ``expected``."""
    try:
        return abs(float(value) - expected) < 0.0001
    except (TypeError, ValueError):
        return False


def float_value(value: Any) -> float | None:
    """``value`` as a float, or ``None`` if it does not parse."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def truthy_bool(value: Any) -> bool:
    """CamillaDSP YAML booleans read back as ``True`` or the string ``"true"``."""
    return value is True or (isinstance(value, str) and value.lower() == "true")
