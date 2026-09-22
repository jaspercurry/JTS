# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Ephemeral level match from the applied rear section and banked pair evidence."""
from __future__ import annotations

import logging
import math
import threading
from typing import Any

from jasper.atomic_io import atomic_write_json, read_json_mapping
from jasper.log_event import log_event

from .audition import MAX_COMPARE_TRIM_DB, audition_state_path

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_levels: dict[tuple[str, str, int | None, str | None], dict[str, Any]] = {}


def _valid_cached_level(level: Any) -> bool:
    if not isinstance(level, dict) or set(level) != {"status", "trim_db", "louder", "reason", "round_id", "banked_at"}:
        return False
    if not isinstance(level["reason"], str) or any(
        level[k] is not None and not isinstance(level[k], str) for k in ("round_id", "banked_at")
    ):
        return False
    trim = level["trim_db"]
    if level["status"] == "unavailable":
        return trim is None and level["louder"] is None
    return (level["status"] == "matched" and type(trim) is float
            and 0 <= trim <= MAX_COMPARE_TRIM_DB
            and (level["louder"] in ("on", "off") if trim else level["louder"] is None))


def rear_compare_level(*, cached_only: bool = False) -> dict[str, Any]:
    """Read the per-tune fact; flips never compute or wait for a preview (ADR-0329)."""
    level: dict[str, Any] = {"status": "unavailable", "trim_db": None, "louder": None,
                             "reason": "level_error", "round_id": None, "banked_at": None}
    try:
        from .baseline_profile import load_applied_baseline_profile_state  # lazy: numpy startup cost
        from .bundles import _detect_build_sha  # lazy: numpy startup cost
        from .round_bank import DEFAULT_CAMPAIGN_ROOT  # lazy: campaign reader import cost

        applied = load_applied_baseline_profile_state() or {}
        section = (applied.get("recomposition_snapshot") or {}).get("rear_calibration")
        if not section:
            return {**level, "reason": "no_applied_rear"}
        try:
            mtime = DEFAULT_CAMPAIGN_ROOT.stat().st_mtime_ns
        except FileNotFoundError:
            mtime = None
        key = (str(applied.get("candidate_fingerprint")), str(applied.get("applied_at")), mtime, _detect_build_sha())
        path = audition_state_path().with_name("rear_compare_level.json")
        if key in _levels:
            return dict(_levels[key])
        saved = read_json_mapping(path) or {}
        if saved.get("key") == list(key) and _valid_cached_level(saved.get("level")):
            _levels[key] = saved["level"]
            return dict(_levels[key])
        if cached_only:
            return {**level, "reason": "cache_miss"}
        with _lock:
            if key not in _levels:
                from .crossover_v2.round_inputs import read_run_manifest, round_inputs  # lazy: cold preview only
                from .crossover_v2.rear_pair_round import newest_rear_pair_round  # lazy: cold preview only
                from .crossover_v2.rear_preview import preview_rear_section, rear_compare_delta_db  # lazy: numpy startup cost

                try:
                    pair = newest_rear_pair_round()
                    if pair is None:
                        level["reason"] = "no_pair_round"
                    else:
                        level.update(round_id=pair["round_id"], banked_at=pair["banked_at"])
                        inputs = round_inputs(pair["round_dir"])
                        preview = preview_rear_section(section, inputs=inputs, manifest=read_run_manifest(inputs))
                        delta = rear_compare_delta_db(preview)
                        if delta is None:
                            level["reason"] = "no_front_pose"
                        elif not math.isfinite(delta) or abs(delta) > MAX_COMPARE_TRIM_DB:
                            level["reason"] = "delta_out_of_range"
                        else:
                            level.update(status="matched", reason="", trim_db=0.0 if abs(delta) < 0.05 else round(float(abs(delta)), 2),
                                         louder=None if abs(delta) < 0.05 else "on" if delta > 0 else "off")
                except Exception:  # noqa: BLE001 - mute must survive level failures (ADR-0329)
                    level["reason"] = "preview_refused"
                    log_event(logger, "active_speaker.rear_compare_level", result="preview_refused",
                              level=logging.ERROR, exc_info=True)
                _levels.clear()  # only the current tune's key is ever read
                _levels[key] = level
                try:
                    atomic_write_json(path, {"key": key, "level": level})
                except OSError:
                    pass
            return dict(_levels[key])
    except Exception:  # noqa: BLE001 - mute must survive level failures (ADR-0329)
        log_event(logger, "active_speaker.rear_compare_level", result="level_error",
                  level=logging.ERROR, exc_info=True)
        return level
