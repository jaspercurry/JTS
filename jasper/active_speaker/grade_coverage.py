# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Read the applied run's asked coverage without inventing a legacy promise."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .bundles import sessions_dir
from .candidate_bank import CandidateBankRefusal, _candidate_roots, load_applied_candidate
from .commissioning_evidence_store import EVIDENCE_ROOT
from .run_manifest import RUN_MANIFEST_FILENAME


def _manifest_path(state: Mapping[str, Any], applied_profile: Mapping[str, Any] | None) -> Path | None:
    candidate = state.get("candidate")
    evidence = state.get("evidence")
    fingerprint = str(candidate.get("fingerprint") or "") if isinstance(candidate, Mapping) else ""
    bundle_id = str(evidence.get("bundle_session_id") or "") if isinstance(evidence, Mapping) else ""
    session_id = str(state.get("session_id") or "")
    # A recovery VERIFY has its own one-pose manifest. The candidate's original
    # run still owns the promise after that re-arm (#2098).
    if fingerprint:
        try:
            banked = load_applied_candidate(fingerprint, applied_profile=applied_profile or {})
            return banked.path.with_name(RUN_MANIFEST_FILENAME)
        except CandidateBankRefusal:
            pass
    if not all(value and Path(value).name == value for value in (bundle_id, session_id)):
        return None
    root = sessions_dir()
    relative = Path(EVIDENCE_ROOT) / "artifacts/crossover_v2" / session_id / RUN_MANIFEST_FILENAME
    live = root / bundle_id / relative
    if live.is_file():
        return live
    receipt = state.get("round_receipt") or {}
    for round_id in (receipt.get("round_id"), session_id, bundle_id):
        if not isinstance(round_id, str) or Path(round_id).name != round_id:
            continue
        for store in _candidate_roots(root):
            path = store / round_id / "bundle" / bundle_id / relative
            if path.is_file():
                return path
    return None


def asked_beyond_mark(state: Mapping[str, Any], *, applied_profile: Mapping[str, Any] | None) -> bool:
    """No manifest means no spatial promise, as for old records (ADR-0298)."""
    path = _manifest_path(state, applied_profile)
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
