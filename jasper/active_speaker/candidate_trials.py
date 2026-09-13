# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve completed captures of an authored candidate without inheriting parent proof."""

from __future__ import annotations

import re
from typing import Any, Mapping


from ._common import blocker_issue
from .candidate_bank import CandidateBankRefusal
from .boost_protection import BOOST_OVER_DECLARED_BOUND, read_boost_finding


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


def candidate_boost_issue(graph_fingerprint: str) -> dict[str, str] | None:
    try:
        finding = read_boost_finding(graph_fingerprint)
    except CandidateBankRefusal as exc:
        return blocker_issue(exc.code, exc.detail)
    return None if finding is None else blocker_issue(
        BOOST_OVER_DECLARED_BOUND, "The measured graph exceeded its declared boost.",
    )
