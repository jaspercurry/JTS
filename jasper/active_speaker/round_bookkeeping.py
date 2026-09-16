# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Publish the program's registered views as part of round completion."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from jasper.atomic_io import atomic_write_json
from .crossover_v2.refusal_copy import CrossoverV2Refused, refusal_copy_for
from .crossover_v2.round_captures import RoundCapturesRefused
from .crossover_v2.room_prescription import RoomPrescriptionRefused
from .crossover_v2.round_inputs import ROUND_INPUT_ERRORS, RoundSetRefused, default_out, round_inputs
from .round_inventory import inventory_payload, inventory_summary
from .round_view_artifacts import ARTIFACT_BY_VIEW, REASON_UNREADABLE, REASON_UNWRITABLE
from .round_view_builders import (
    analyzed_frequency_run, frequency_payload, frequency_image, bass_payload, room_payload, room_grade_payload,
)


def run_bookkeeping(view: str, target: Path, *, set_id: str | None = None,
                    incumbent: str | None = None) -> dict[str, Any]:
    if view not in {"room", "room-grade", "bass", "frequency", "inventory"}:
        return {"view": view, "status": "unavailable", "reason": (
            "inputs_required" if view in ARTIFACT_BY_VIEW else "verb_not_registered")}
    try:
        inputs = round_inputs(target)
        path = default_out(inputs, target, ARTIFACT_BY_VIEW[view].artifact, set_id)
        if view == "room":
            payload = room_payload(inputs, set_id)
            summary = {**{key: payload["median"][key] for key in (
                "set_id", "ceiling_hz", "n_positions", "spatial_support", "coverage_hz")},
                "features": len(payload["persistence"]["features"]),
                "incumbent": payload["incumbent"], "incumbent_reason": payload["incumbent_reason"]}
        elif view == "room-grade":
            payload = room_grade_payload(inputs, target, set_id, incumbent_id=incumbent)
            summary = payload
        elif view == "bass":
            payload = bass_payload(inputs, set_id)
            summary = {"takes": len(payload["takes"])}
        elif view == "frequency":
            payload, series = frequency_payload(analyzed_frequency_run(target))
            summary = {"runs": [run["id"] for run in payload["runs"]], "series": series}
        else:
            payload = inventory_payload(inputs, target, set_id)
            summary = inventory_summary(payload)
    except (RoundSetRefused, RoundCapturesRefused, RoomPrescriptionRefused) as exc:
        return {"view": view, "status": "unavailable", "reason": exc.reason, "detail": exc.detail}
    except CrossoverV2Refused as exc:
        _, action = refusal_copy_for(exc.code)
        return {"view": view, "status": "unavailable", "reason": exc.code, "code": exc.code,
                "detail": str(exc), "next_action": action}
    except ROUND_INPUT_ERRORS as exc:
        return {"view": view, "status": "unavailable", "reason": REASON_UNREADABLE, "detail": str(exc)}
    try:
        atomic_write_json(path, payload)
        if view == "frequency":
            summary.update(frequency_image(payload, target / "frequency.png", low_end=True))
    except OSError as exc:
        return {"view": view, "status": "unavailable", "reason": REASON_UNWRITABLE, "detail": str(exc)}
    return {**summary, "view": view, "status": "written", "out": str(path), "bytes": path.stat().st_size}
