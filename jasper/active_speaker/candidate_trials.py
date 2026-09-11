# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve completed captures of an authored candidate without inheriting parent proof."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from jasper.audio_measurement.bundles import BundleError
from jasper.json_fields import finite_float

from .candidate_bank import CandidateBankRefusal, find_banked_candidate
from .commissioning_evidence_store import CommissioningEvidenceStoreError
from .measured_crossover_candidate import candidate_trial_scope


TUNING_TRIAL_SCOPES = frozenset({"room_candidate", "bass_candidate"})


def tuning_trial_reference(candidate: Any, trial: Mapping[str, Any] | None) -> dict[str, str] | None:
    """Compact durable pointer to the exact captured tuning graph, when present."""
    scope = candidate_trial_scope(candidate)
    if scope not in TUNING_TRIAL_SCOPES:
        return None
    record = trial if isinstance(trial, Mapping) else {}
    candidate_id = str(record.get("candidate_id") or "")
    graph_fingerprint = str(record.get("graph_fingerprint") or "")
    record_path = str(record.get("record_path") or "")
    if (
        candidate_id != candidate.fingerprint
        or record.get("graph_scope") != scope
        or re.fullmatch(r"[0-9a-f]{16}", graph_fingerprint) is None
        or not record_path
    ):
        raise CandidateBankRefusal(
            "candidate_trial_required",
            "Capture this complete tuning candidate before applying it.",
        )
    return {
        "candidate_fingerprint": candidate_id,
        "graph_scope": scope,
        "graph_fingerprint": graph_fingerprint,
        "record_path": record_path,
    }


def tuning_trial_matches_candidate(reference: Any, candidate_fingerprint: Any) -> bool:
    """Whether a persisted tuning-trial pointer names this exact candidate."""
    if not isinstance(reference, Mapping):
        return False
    return (
        str(reference.get("candidate_fingerprint") or "")
        == str(candidate_fingerprint or "")
        != ""
        and reference.get("graph_scope") in TUNING_TRIAL_SCOPES
        and re.fullmatch(
            r"[0-9a-f]{16}", str(reference.get("graph_fingerprint") or "")
        ) is not None
        and bool(str(reference.get("record_path") or ""))
    )


def require_candidate_trial(
    candidate: Any,
    *,
    root: Path | None = None,
    expected_graph_fingerprint: str | None = None,
) -> dict[str, Any] | None:
    """Require a captured full graph for authored candidates; legacy fits need no new proof.

    A trial establishes that these exact settings were captured, not that their
    acoustic result passed. The graph digest is the installed graph's recorded
    identity; rebuilding it here would substitute current device configuration.
    """
    if (
        candidate.analysis.get("measurement_status") != "unmeasured"
        and candidate_trial_scope(candidate) not in TUNING_TRIAL_SCOPES
    ):
        return None
    from .commissioning_evidence_store import EVIDENCE_ROOT  # lazy: apply-only evidence reader
    from .crossover_v2.record_index import bundle_measurements, reopen_measurement_capture  # lazy: pulls the tuning engine
    from .crossover_v2.round_inputs import iter_round_sessions  # lazy: pulls the tuning engine

    scope = candidate_trial_scope(candidate)
    source = find_banked_candidate(candidate.fingerprint, root=root).path.parents[5]
    for bundle in iter_round_sessions(source):
        for row in bundle_measurements(bundle, candidate_id=candidate.fingerprint):
            path = bundle / EVIDENCE_ROOT / "artifacts" / row.path
            try:
                record, wav = reopen_measurement_capture(bundle, path)
                if (
                    wav is None
                    or record.get("candidate_id") != candidate.fingerprint
                    or record.get("graph_scope") != scope
                    or record.get("incident") != ""
                    or finite_float(record.get("level_db")) is None
                    or re.fullmatch(r"[0-9a-f]{16}", str(record.get("graph_fingerprint") or "")) is None
                    or expected_graph_fingerprint is not None
                    and record.get("graph_fingerprint") != expected_graph_fingerprint
                ):
                    continue
            except (OSError, ValueError, TypeError, AttributeError, KeyError,
                    BundleError, CommissioningEvidenceStoreError):
                continue
            return {**record, "record_path": str(path)}
    raise CandidateBankRefusal(
        "candidate_trial_required",
        "Capture this complete candidate before applying it; no intact trial of this fingerprint was found.",
    )


def candidate_boost_issue(candidate: Any) -> dict[str, str] | None:
    """Refuse a candidate whose banked measurement found excess boost (AGENTS §2)."""
    if candidate is None:
        return None
    from .crossover_v2.round_inputs import (  # lazy: apply-only evidence reader
        _read_json_mapping, iter_round_sessions, round_artifact_dir,
    )

    try:
        source = find_banked_candidate(candidate.fingerprint).path.parents[5]
    except CandidateBankRefusal as exc:
        if exc.code != "not_found":
            raise
        return None
    for bundle in iter_round_sessions(source):
        directory, _ = round_artifact_dir(bundle)
        if directory is None:
            continue
        receipt = _read_json_mapping(directory / "round_receipt.json") or {}
        if (receipt.get("evidence_identities") or {}).get("candidate_fingerprint") != candidate.fingerprint:
            continue
        safety = (receipt.get("round_axes") or {}).get("safety") or {}
        if (safety.get("evidence") or {}).get("boost_over_declared_bound") is True:
            return {
                "severity": "blocker",
                "code": "boost_over_declared_bound",
                "message": "The measured candidate exceeded its declared boost.",
            }
    return None
