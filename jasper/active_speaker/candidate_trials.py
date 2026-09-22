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
