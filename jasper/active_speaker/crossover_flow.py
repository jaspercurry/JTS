# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The crossover-measurement flow selector (v2 conductor, Wave 5a/W6).

``docs/crossover-measurement-productization-design.md`` §6 W5a shipped the v2
conductor flow (CHECK -> MEASURE -> REVIEW/APPLY -> VERIFY) **alongside** the
legacy per-driver flow. One environment flag picks which flow the
``/correction/crossover/`` envelope + endpoints run:

    JASPER_CROSSOVER_FLOW = "v2"  (default) | "legacy"

W6 (2026-07-19) validated v2 end-to-end on hardware — scripted and real-UI
full passes, applied-and-verified candidates, repeatability benchmarks — so
the default is now ``v2``. ``legacy`` remains an explicit opt-out escape
hatch until W5b deletes the legacy flow outright; when it goes, this selector
goes with it.

Codify-don't-memorize: the flag is seeded in ``.env.example`` with a prose
block. With the selector resolving legacy, the legacy path behaves
byte-identically to its pre-W5a self (pinned by a selector byte-identity
test).

This module owns ONLY the selector — a tiny, env-injectable resolver with no
product policy. The envelope dispatch and the endpoint wiring read it through
:func:`active_crossover_flow`; the pure envelope also honours a
``status["crossover_flow"]`` override so callers/tests can thread the choice
in without touching the process environment.
"""

from __future__ import annotations

import os
from typing import Mapping

CROSSOVER_FLOW_ENV = "JASPER_CROSSOVER_FLOW"

CROSSOVER_FLOW_LEGACY = "legacy"
CROSSOVER_FLOW_V2 = "v2"

# The default the whole product runs on. Flipped legacy -> v2 after W6's
# hardware validation (2026-07-19). NOT a Config field: this is a transitional
# deployment flag read at the envelope/endpoint dispatch point (like the
# JASPER_AEC_* / JASPER_OUTPUTD_* daemon knobs), so it lives in a dedicated
# resolver rather than jasper.config.Config.
DEFAULT_CROSSOVER_FLOW = CROSSOVER_FLOW_V2

_VALID_FLOWS = frozenset({CROSSOVER_FLOW_LEGACY, CROSSOVER_FLOW_V2})


def active_crossover_flow(env: Mapping[str, str] | None = None) -> str:
    """Resolve the active crossover flow from the environment.

    Returns :data:`CROSSOVER_FLOW_LEGACY` only for the exact literal
    ``"legacy"`` (case-insensitive, whitespace-trimmed); any other value —
    including an unset variable, an empty string, or a typo — resolves to
    :data:`CROSSOVER_FLOW_V2`, the hardware-validated default. Fail-safe by
    construction: an unrecognized value resolves to the supported flow, and
    only a deliberate ``legacy`` opt-out reaches the deprecated path.
    """
    raw = (env if env is not None else os.environ).get(CROSSOVER_FLOW_ENV, "")
    value = str(raw or "").strip().lower()
    return CROSSOVER_FLOW_LEGACY if value == CROSSOVER_FLOW_LEGACY else CROSSOVER_FLOW_V2


def resolve_crossover_flow(
    status: Mapping[str, object] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """The flow the envelope should render for ``status``.

    Precedence: a valid ``status["crossover_flow"]`` override (the endpoint
    threads the resolved flow onto the status it hands the pure envelope) wins;
    otherwise the process environment via :func:`active_crossover_flow`. An
    invalid override is ignored (fail-safe to the env-resolved flow), never
    trusted, so a malformed status can't activate v2.
    """
    if isinstance(status, Mapping):
        override = status.get("crossover_flow")
        if isinstance(override, str) and override.strip().lower() in _VALID_FLOWS:
            return override.strip().lower()
    return active_crossover_flow(env)
