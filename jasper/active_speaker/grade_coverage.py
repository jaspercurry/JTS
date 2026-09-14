# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Read the applied run's asked coverage without inventing a legacy promise."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .bundles import sessions_dir
from .candidate_bank import CandidateBankRefusal, find_banked_candidate, status_bank_lookup
from .commissioning_evidence_store import EVIDENCE_ROOT
from .crossover_v2.round_inputs import iter_round_sessions, round_artifact_dir
from .run_manifest import RUN_MANIFEST_FILENAME


def _manifest_path(state: Mapping[str, Any]) -> Path | None:
    candidate = state.get("candidate")
    evidence = state.get("evidence")
    fingerprint = str(candidate.get("fingerprint") or "") if isinstance(candidate, Mapping) else ""
    bundle_id = str(evidence.get("bundle_session_id") or "") if isinstance(evidence, Mapping) else ""
    session_id = str(state.get("session_id") or "")
    root = sessions_dir()
    path = status_bank_lookup(
        ("manifest", fingerprint, bundle_id, session_id),
        lambda: _banked_manifest_path(fingerprint, root, bundle_id, session_id), root=root,
    )
    if path is not None:
        return path
    if all(value and Path(value).name == value for value in (bundle_id, session_id)):
        return root / bundle_id / EVIDENCE_ROOT / "artifacts/crossover_v2" / session_id / RUN_MANIFEST_FILENAME
    return None


def _banked_manifest_path(fingerprint: str, root: Path, bundle_id: str, session_id: str) -> Path | None:
    # A recovery VERIFY has its own one-pose manifest. The candidate's original
    # run still owns the promise after that re-arm (#2098).
    if fingerprint:
        try:
            banked = find_banked_candidate(fingerprint, root=root)
            return banked.path.with_name(RUN_MANIFEST_FILENAME)
        except CandidateBankRefusal:
            pass
    live = root / bundle_id
    if not bundle_id or Path(bundle_id).name != bundle_id or live.is_dir():
        return None
    for bundle in iter_round_sessions(live):
        if bundle.name == live.name:
            directory, _ = round_artifact_dir(bundle)
            if directory is not None and directory.name == session_id:
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
