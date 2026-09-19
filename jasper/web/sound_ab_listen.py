# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read pose-balanced levels for browser-owned A/B listening."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from jasper.active_speaker.baseline_profile import load_applied_baseline_profile_state
from jasper.active_speaker.passive_profile import measured_candidate_fingerprint
from jasper.active_speaker.round_bank import DEFAULT_CAMPAIGN_ROOT
from jasper.active_speaker.wizard_client import APPLY_PATH
from jasper.volume_curve import configured_volume_floor_db, percent_to_db

LEVEL_BAND_HZ = (40.0, 16000.0)  # Same band as sound.profile.loudness_compensation_db.
MAX_ROUNDS = 8
MAX_FILES = 24


def _tunes(view: dict[str, Any]) -> list[dict[str, Any]]:
    from jasper.audio_measurement.analysis import band_levels_from_magnitude  # lazy: numpy startup cost

    poses: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    bases: dict[str, bool] = {}
    for run in view.get("runs", []):
        for series in run.get("series", []):
            fp = series.get("candidate_id")
            if (series.get("kind"), series.get("role"), series.get("window")) != (
                "measurement", "summed", "ungated"
            ) or not isinstance(fp, str):
                continue
            try:
                level = band_levels_from_magnitude(
                    series["freqs_hz"], series["magnitude_db"], [LEVEL_BAND_HZ]
                )[0]
                pose = json.dumps(series["position"], sort_keys=True, separators=(",", ":"))
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(level):
                poses[fp][pose].append(level)
                bases[fp] = bases.get(fp, False) or bool(series.get("base"))
    shared = set.intersection(*(set(p) for p in poses.values())) if poses else set()
    if len(poses) < 2 or not shared:
        return []
    return [{"fingerprint": fp, "base": bases[fp],
             "level_db": round(mean(mean(pose[p]) for p in shared), 3)}
            for fp, pose in poses.items()]


def ab_listen_state_payload(campaign_root: Path = DEFAULT_CAMPAIGN_ROOT) -> dict[str, Any]:
    opened = 0

    def read(path: Path) -> dict[str, Any]:
        nonlocal opened
        if opened >= MAX_FILES:
            return {}
        opened += 1
        try:
            value = json.loads(path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    paths = []
    for path in campaign_root.glob("*/frequency_view.json"):
        try:
            paths.append((path.stat().st_mtime, path))
        except OSError:
            continue
    rounds: list[dict[str, Any]] = []
    for mtime, path in sorted(paths, reverse=True):
        if opened >= MAX_FILES or len(rounds) >= MAX_ROUNDS:
            break
        view = read(path)
        if view.get("schema") != "jts_frequency_view/1":
            continue
        try:
            tunes = _tunes(view)
        except (AttributeError, TypeError):
            continue
        if tunes:
            packet = read(path.parent / "packet.json")
            provenance = read(path.parent / "provenance.json")
            rounds.append({"round_id": path.parent.name, "program": packet.get("program"),
                           "banked_at": provenance.get("banked_at_utc") or
                           datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
                           "tunes": tunes})
    applied = load_applied_baseline_profile_state() or {}
    floor = configured_volume_floor_db()
    return {
        "applied_fingerprint": measured_candidate_fingerprint(applied.get("source")) or None,
        "apply_path": APPLY_PATH,
        "volume_step_db": percent_to_db(51, floor_db=floor) - percent_to_db(50, floor_db=floor),
        "level_band_hz": list(LEVEL_BAND_HZ),
        "rounds": rounds,
    }
