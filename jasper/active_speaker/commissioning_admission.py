# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any

import yaml

from jasper.audio_measurement.evidence_identity import json_fingerprint


class ActiveCommissioningAdmissionError(RuntimeError):
    """A running graph cannot provide a usable identity."""


def parse_running_graph(running_config_raw: str | None) -> dict[str, Any]:
    """One parseable CamillaDSP graph as an object, unrepaired.

    Shared with callers that hash a SUBSET of the graph (see
    :func:`~jasper.active_speaker.crossover_v2.tuning_scope.tuning_scope_fingerprint`)
    so both refuse an unparseable readback identically.
    """

    try:
        parsed = yaml.safe_load(running_config_raw or "")
    except yaml.YAMLError as exc:
        raise ActiveCommissioningAdmissionError(
            "running CamillaDSP graph is not parseable"
        ) from exc
    if not isinstance(parsed, dict):
        raise ActiveCommissioningAdmissionError(
            "running CamillaDSP graph is not an object"
        )
    return parsed


def running_graph_fingerprint(running_config_raw: str | None) -> str:
    """Fingerprint one parseable fresh CamillaDSP readback without repairing it."""

    return json_fingerprint(parse_running_graph(running_config_raw))
