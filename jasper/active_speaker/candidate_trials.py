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

from ._common import blocker_issue
from .candidate_bank import CandidateBankRefusal, find_banked_candidate
from .boost_protection import BOOST_OVER_DECLARED_BOUND, read_boost_finding
from .commissioning_evidence_store import CommissioningEvidenceStoreError


def has_tuning_layers(candidate: Any) -> bool:
    return bool(candidate.room_correction or candidate.bass_extension)


def tuning_trial_reference(candidate: Any, trial: Mapping[str, Any] | None) -> dict[str, str] | None:
    """Compact durable pointer to the exact captured tuning graph, when present."""
    if not has_tuning_layers(candidate):
        return None
    record = trial if isinstance(trial, Mapping) else {}
    candidate_id = str(record.get("candidate_id") or "")
    graph_fingerprint = str(record.get("graph_fingerprint") or "")
    record_path = str(record.get("record_path") or "")
    if (
        candidate_id != candidate.fingerprint
        or record.get("graph_scope") != "candidate"
        or re.fullmatch(r"[0-9a-f]{16}", graph_fingerprint) is None
        or not record_path
    ):
        raise CandidateBankRefusal(
            "candidate_trial_required",
            "Capture this complete tuning candidate before applying it.",
        )
    return {
        "candidate_fingerprint": candidate_id,
        "graph_scope": "candidate",
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
        and reference.get("graph_scope") == "candidate"
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

    The digest identifies the installed graph, not a fresh rebuild.
    """
    if (
        candidate.analysis.get("measurement_status") != "unmeasured"
        and not has_tuning_layers(candidate)
    ):
        return None
    from .commissioning_evidence_store import EVIDENCE_ROOT  # lazy: apply-only evidence reader
    from .crossover_v2.record_index import bundle_measurements, reopen_measurement_capture  # lazy: pulls the tuning engine
    from .crossover_v2.round_inputs import iter_round_sessions  # lazy: pulls the tuning engine

    source = find_banked_candidate(candidate.fingerprint, root=root).path.parents[5]
    for bundle in iter_round_sessions(source):
        for row in bundle_measurements(bundle, candidate_id=candidate.fingerprint):
            path = bundle / EVIDENCE_ROOT / "artifacts" / row.path
            try:
                record, wav = reopen_measurement_capture(bundle, path)
                if (
                    wav is None
                    or record.get("candidate_id") != candidate.fingerprint
                    or record.get("graph_scope") != "candidate"
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


def candidate_boost_issue(graph_fingerprint: str) -> dict[str, str] | None:
    try:
        finding = read_boost_finding(graph_fingerprint)
    except CandidateBankRefusal as exc:
        return blocker_issue(exc.code, exc.detail)
    return None if finding is None else blocker_issue(
        BOOST_OVER_DECLARED_BOUND, "The measured graph exceeded its declared boost.",
    )
