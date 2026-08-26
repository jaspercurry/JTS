# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The logged screen-envelope entry point.

``/sound/`` owns topology, driver protection, output identity, and the safe
starting profile.  The crossover screen envelope owns the distinct microphone
journey:

    mic/calibration + per-driver level -> driver sweeps -> combined alignment
    -> atomic apply

The envelope itself is :mod:`jasper.active_speaker.crossover_envelope_v2` --
the only flow since W5b retired the legacy per-driver flow and the
``JASPER_CROSSOVER_FLOW`` selector. What is left here is the logged wrapper
around it, which performs no I/O and mutates no measurement state.

A ``build_crossover_envelope`` compatibility dispatcher, an
``_active_level_solve_refusal`` reader, and ``describe_level_solve_refusal``
also lived here; the dispatcher's callers now import v2 directly, nothing in
production ever read the ``solve_refusal`` field the reader projected, and
the refusal copy died with the closed-loop level solver it described.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from ..log_event import log_event

logger = logging.getLogger(__name__)


def build_crossover_envelope_logged(status: Mapping[str, Any]) -> dict[str, Any]:
    """Serve the v2 session crossover envelope for ``status`` and log the serve."""

    from .crossover_envelope_v2 import build_crossover_envelope_v2

    envelope = build_crossover_envelope_v2(status)
    log_event(
        logger,
        "correction.crossover_envelope_serve",
        screen=envelope["screen"],
        active=envelope["active"],
        step_count=len(envelope["steps"]),
        nudge_count=len(envelope["nudges"]),
        action=(envelope.get("next_action") or {}).get("id"),
        alternate_action_count=len(envelope.get("alternate_actions") or []),
        applied=(envelope.get("applied") or {}).get("state"),
    )
    return envelope
