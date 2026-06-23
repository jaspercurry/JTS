# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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

Ownership boundary (this module vs the sibling ``graph_safety`` leaf)
--------------------------------------------------------------------
This module owns the verifier's two emitter-coupled vocabularies:

* **Canonical filter names** — re-exposed from the emitter (``camilla_yaml``
  owns the spellings; see the public aliases there). Importing ``camilla_yaml``
  is exactly why this module is NOT a leaf.
* **Raw-dict accessors** (``filter_spec`` / ``filter_params`` / ``filter_type``)
  that pull one field straight out of an already-parsed CamillaDSP config
  mapping — for ``runtime_contract``'s baseline path, which works on the raw
  ``payload`` rather than a normalized view.

The complementary half — the normalized ``GraphView``, the parse adapters, the
fail-closed wiring predicates (``output_hard_muted_and_wired``,
``tweeter_guard_present``, …), and the shared scalar matchers (``float_matches``
/ ``float_value`` / ``truthy_bool``) — lives in the sibling ``graph_safety``
leaf; import those from there. The two modules are independent:
``graph_safety`` has no emitter dependency and stays promotable to a top-level
shared module.

The raw config mapping these accessors read::

    {"filters": {name: {"type": ..., "parameters": {...}}},
     "pipeline": [{"type": "Filter", "channels": [...]|"channel": N,
                   "names": [...]}]}
"""

from __future__ import annotations

from typing import Any

from .camilla_yaml import (
    bass_management_hp_name,
    channel_select_mixer_name,
    driver_baseline_gain_name,
    driver_baseline_limiter_name,
    driver_limiter_name,
    driver_mute_name,
    output_commission_mute_name,
    protective_tweeter_hp_name,
    sub_baseline_gain_name,
    sub_baseline_limiter_name,
    sub_lowpass_name,
)

__all__ = [
    # Canonical filter names (re-exported from the emitter, the single owner).
    "bass_management_hp_name",
    "channel_select_mixer_name",
    "driver_baseline_gain_name",
    "driver_baseline_limiter_name",
    "driver_limiter_name",
    "driver_mute_name",
    "output_commission_mute_name",
    "protective_tweeter_hp_name",
    "sub_baseline_gain_name",
    "sub_baseline_limiter_name",
    "sub_lowpass_name",
    # Raw-dict accessors (owned here).
    "filter_spec",
    "filter_params",
    "filter_type",
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
