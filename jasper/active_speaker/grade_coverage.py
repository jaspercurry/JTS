# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Read the applied run's asked coverage without inventing a legacy promise."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .bundles import sessions_dir
from .candidate_bank import CandidateBankRefusal, find_banked_candidate
from .crossover_v2.round_inputs import iter_round_sessions, round_artifact_dir
from .run_manifest import RUN_MANIFEST_FILENAME


def _manifest_path(state: Mapping[str, Any]) -> Path | None:
    # A recovery VERIFY has its own one-pose manifest. The candidate's original
    # run still owns the promise after that re-arm (#2098).
    candidate = state.get("candidate")
    if isinstance(candidate, Mapping) and candidate.get("fingerprint"):
        try:
            banked = find_banked_candidate(str(candidate["fingerprint"]))
            return banked.path.with_name(RUN_MANIFEST_FILENAME)
        except CandidateBankRefusal:
            pass
    evidence = state.get("evidence")
    bundle_id = evidence.get("bundle_session_id") if isinstance(evidence, Mapping) else None
    if not isinstance(bundle_id, str) or not bundle_id or Path(bundle_id).name != bundle_id:
        return None
    live = sessions_dir() / bundle_id
    bundles = (live,) if live.is_dir() else iter_round_sessions(live)
    for bundle in bundles:
        if bundle.name == bundle_id:
            directory, _ = round_artifact_dir(bundle)
            if directory is not None and directory.name == state.get("session_id"):
                return directory / RUN_MANIFEST_FILENAME
    return None


def asked_beyond_mark(state: Mapping[str, Any]) -> bool:
    """No manifest means no spatial promise, as for old records (ADR-0298)."""
    path = _manifest_path(state)
    if path is None:
        return False
    try:
        manifest = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    asked = manifest.get("asked") if isinstance(manifest, Mapping) else None
    poses = asked.get("poses") if isinstance(asked, Mapping) else None
    if not isinstance(poses, list) or not poses or not all(isinstance(p, Mapping) for p in poses):
        return False
    return any(
        pose.get("deg", 0) or pose.get("elevation_deg", 0)
        or any(pose.get("seat_offset_m") or ())
        or pose.get("distance_m") != poses[0].get("distance_m")
        for pose in poses
    )
