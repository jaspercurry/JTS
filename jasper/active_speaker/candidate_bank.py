# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Find a previously-minted candidate in the on-box bundle store, by fingerprint.

Owns the fingerprint SCAN glob; a second reader should import it from here, not restate
it. Integrity is the candidate model's alone
(:meth:`MeasuredCrossoverCandidate.from_mapping`) -- this module adds only bounds and
identity resolution, no second hasher. Lookup is keyed on bundle id *and* minting relay
session id together, both carried on :class:`BankedCandidate`. Kept out of
``crossover_v2/`` to avoid that package's numpy-pulling ``__init__``; lazy-imports the
candidate model instead.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jasper.log_event import log_event

logger = logging.getLogger(__name__)

#: Published candidate artifact path, relative to bundle root. First ``*`` is the bundle
#: id (``bundles.open_bundle``'s 12-hex session id); second is the MINTING relay session
#: id -- distinct namespaces, do not conflate.
CANDIDATE_ARTIFACT_GLOB = "*/evidence/v1/artifacts/crossover_v2/*/candidate.json"

#: Candidate artifacts to PARSE before giving up (bounds ~4 MB JSON + a fingerprint
#: recompute each on a 1 GB Pi); does NOT bound the directory walk.
MAX_CANDIDATE_ARTIFACTS_SCANNED = 64

#: Largest candidate.json this reader will parse (input ceiling, not a contract -- the
#: publisher's own artifact budget is smaller).
MAX_CANDIDATE_BYTES = 4 * 1024 * 1024


class CandidateBankRefusal(LookupError):
    """No single trustworthy banked candidate answers this fingerprint. Carries a machine
    ``code`` so a door can map it without parsing prose.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class BankedCandidate:
    """One banked candidate plus the identity needed to re-open it later. ``bundle_session_id``
    and ``relay_session_id`` together are the ONLY way back to this artifact.
    """

    candidate: Any
    bundle_session_id: str
    relay_session_id: str
    path: Path

    @property
    def fingerprint(self) -> str:
        return str(self.candidate.fingerprint)


def candidate_artifact_paths(root: Path) -> list[Path]:
    """Every published candidate.json under the bundle root, in a stable order. Sorted for
    determinism only, NOT chronological (bundle dirs are ``uuid4().hex[:12]``);
    :data:`MAX_CANDIDATE_ARTIFACTS_SCANNED` bounds work, not recent history.
    """
    try:
        found = sorted(Path(root).glob(CANDIDATE_ARTIFACT_GLOB))
    except OSError:
        return []
    return found[-MAX_CANDIDATE_ARTIFACTS_SCANNED:]


def _identity_from_path(path: Path) -> tuple[str, str]:
    """``(bundle_session_id, relay_session_id)`` for one artifact path. Positional, not parsed:
    the glob fixes the depth (relay session is the artifact's own directory, bundle is
    five levels above).
    """
    parents = path.parents
    relay_session_id = parents[0].name
    bundle_session_id = parents[5].name if len(parents) > 5 else ""
    return bundle_session_id, relay_session_id


def load_candidate_artifact(path: Path) -> Any | None:
    """Parse and integrity-check one candidate artifact, or ``None`` (unreadable, oversized,
    malformed JSON, or a fingerprint mismatch).
    """
    from jasper.active_speaker.measured_crossover_candidate import (
        MeasuredCrossoverCandidate,
        MeasuredCrossoverCandidateError,
    )

    try:
        if path.stat().st_size > MAX_CANDIDATE_BYTES:
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return MeasuredCrossoverCandidate.from_mapping(raw)
    except (MeasuredCrossoverCandidateError, TypeError, ValueError):
        return None


def find_banked_candidate(
    fingerprint: str, *, root: Path | None = None
) -> BankedCandidate:
    """The one banked candidate with this fingerprint, or a typed refusal:
    ``fingerprint_required``, ``not_found`` (no artifact both matches and verifies), or
    ``ambiguous`` (two lineages carry this fingerprint).
    """
    from jasper.active_speaker.bundles import sessions_dir

    wanted = str(fingerprint or "").strip()
    if not wanted:
        raise CandidateBankRefusal(
            "fingerprint_required", "a candidate fingerprint is required"
        )

    bundle_root = Path(root) if root is not None else sessions_dir()
    paths = candidate_artifact_paths(bundle_root)
    matches: dict[tuple[str, str], BankedCandidate] = {}
    verified = 0
    for path in paths:
        candidate = load_candidate_artifact(path)
        if candidate is None:
            continue
        verified += 1
        if str(candidate.fingerprint) != wanted:
            continue
        bundle_session_id, relay_session_id = _identity_from_path(path)
        if not bundle_session_id or not relay_session_id:
            continue
        matches.setdefault(
            (bundle_session_id, relay_session_id),
            BankedCandidate(
                candidate=candidate,
                bundle_session_id=bundle_session_id,
                relay_session_id=relay_session_id,
                path=path,
            ),
        )

    if len(matches) > 1:
        raise CandidateBankRefusal(
            "ambiguous",
            f"{len(matches)} banked sessions claim this candidate fingerprint",
        )
    if not matches:
        unverified = len(paths) - verified
        detail = f"no banked candidate matches this fingerprint ({verified} examined)"
        if unverified:
            detail += f"; {unverified} could not be verified"
        raise CandidateBankRefusal("not_found", detail)

    found = next(iter(matches.values()))
    log_event(
        logger,
        "correction.crossover_v2_banked_candidate_found",
        candidate_fingerprint=found.fingerprint,
        bundle_session_id=found.bundle_session_id,
        relay_session_id=found.relay_session_id,
        examined=verified,
    )
    return found
