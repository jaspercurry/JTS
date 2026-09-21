# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Ephemeral level match from the applied rear section and banked pair evidence."""
from __future__ import annotations

import math
import threading
from typing import Any

from .audition import MAX_COMPARE_TRIM_DB

_lock = threading.Lock()
_levels: dict[tuple[str, str, str], dict[str, Any]] = {}


def rear_compare_level() -> dict[str, Any]:
    level: dict[str, Any] = {"status": "unavailable", "trim_db": None, "louder": None,
                             "reason": "level_error", "round_id": None, "banked_at": None}
    try:
        from .baseline_profile import load_applied_baseline_profile_state  # lazy: numpy startup cost
        from .crossover_v2.round_inputs import read_run_manifest, round_inputs  # lazy: numpy startup cost
        from .crossover_v2.rear_pair_round import newest_rear_pair_round  # lazy: numpy startup cost
        from .crossover_v2.rear_preview import preview_rear_section, rear_compare_delta_db  # lazy: numpy startup cost

        with _lock:
            applied = load_applied_baseline_profile_state() or {}
            section = (applied.get("recomposition_snapshot") or {}).get("rear_calibration")
            if not section:
                return {**level, "reason": "no_applied_rear"}
            pair = newest_rear_pair_round()
            if pair is None:
                return {**level, "reason": "no_pair_round"}
            level.update(round_id=pair["round_id"], banked_at=pair["banked_at"])
            key = (str(applied.get("candidate_fingerprint")), str(applied.get("applied_at")), pair["round_id"])
            if key not in _levels:
                try:
                    inputs = round_inputs(pair["round_dir"])
                    preview = preview_rear_section(section, inputs=inputs, manifest=read_run_manifest(inputs))
                    delta = rear_compare_delta_db(preview)
                    if delta is None:
                        level["reason"] = "no_front_pose"
                    elif not math.isfinite(delta) or abs(delta) > MAX_COMPARE_TRIM_DB:
                        level["reason"] = "delta_out_of_range"
                    else:
                        level.update(status="matched", reason="", trim_db=0.0 if abs(delta) < 0.05 else round(abs(delta), 2),
                                     louder=None if abs(delta) < 0.05 else "on" if delta > 0 else "off")
                except Exception:  # noqa: BLE001 - mute must survive level failures (ADR-0329)
                    level["reason"] = "preview_refused"
                _levels[key] = level
            return dict(_levels[key])
    except Exception:  # noqa: BLE001 - mute must survive level failures (ADR-0329)
        return level
