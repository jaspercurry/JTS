# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Find a previously-minted candidate in the on-box bundle store, by fingerprint.

Owns the fingerprint SCAN glob; a second reader should import it from here, not restate
it. Integrity is the candidate model's alone
(:meth:`MeasuredCrossoverCandidate.from_mapping`) -- this module adds only bounds and
identity resolution, no second hasher. Lookup is keyed on bundle id *and* minting capture
session id together, both carried on :class:`BankedCandidate`. Kept out of
``crossover_v2/`` to avoid that package's numpy-pulling ``__init__``; lazy-imports the
candidate model instead.
"""

from __future__ import annotations

import heapq
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from jasper.log_event import log_event

logger = logging.getLogger(__name__)

#: Published candidate artifact path, relative to bundle root. First ``*`` is the bundle
#: id (``bundles.open_bundle``'s 12-hex session id); second is the MINTING capture session
#: id -- distinct namespaces, do not conflate.
CANDIDATE_ARTIFACT_GLOB = "*/evidence/v1/artifacts/crossover_v2/*/candidate.json"

#: Bound the discovery listing, not exact retrieval.
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
    and ``capture_session_id`` together are the ONLY way back to this artifact.
    """

    candidate: Any
    bundle_session_id: str
    capture_session_id: str
    path: Path

    @property
    def fingerprint(self) -> str:
        return str(self.candidate.fingerprint)


def _directories(root: Path) -> Iterator[Path]:
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.is_dir():
                    yield Path(entry.path)
    except OSError:
        return


def _iter_candidate_paths(root: Path) -> Iterator[Path]:
    for store in _candidate_roots(root):
        for directory in _directories(store):
            yield from directory.glob(CANDIDATE_ARTIFACT_GLOB.split("/", 1)[1])
            nested = directory if directory.name == "bundle" else directory / "bundle"
            for bundle in _directories(nested):
                yield from bundle.glob(CANDIDATE_ARTIFACT_GLOB.split("/", 1)[1])


def candidate_artifact_paths(root: Path) -> list[Path]:
    """A bounded lexical listing; exact retrieval uses the streaming iterator."""
    return sorted(heapq.nlargest(MAX_CANDIDATE_ARTIFACTS_SCANNED, _iter_candidate_paths(root)))


def _bank_root(root: Path | None) -> Path:
    """Where the bank IS, defaulting to the on-box bundle store both readers scan."""
    from jasper.active_speaker.bundles import sessions_dir

    return Path(root) if root is not None else sessions_dir()


def _candidate_roots(root: Path) -> tuple[Path, ...]:
    from jasper.active_speaker.bundles import DEFAULT_SESSIONS_DIR  # lazy: keep discovery imports cheap
    from jasper.active_speaker.round_bank import DEFAULT_CAMPAIGN_ROOT  # lazy: commissioning import cost

    root = Path(root)
    paired = {
        DEFAULT_SESSIONS_DIR.name: DEFAULT_CAMPAIGN_ROOT.name,
        DEFAULT_CAMPAIGN_ROOT.name: DEFAULT_SESSIONS_DIR.name,
    }.get(root.name)
    if root == _bank_root(None):
        paired = DEFAULT_CAMPAIGN_ROOT.name
    return (root, root.parent / paired) if paired else (root,)


def banked_candidates(*, root: Path | None = None) -> list[BankedCandidate]:
    """The bounded discovery listing, with each candidate verified."""
    return _verified_candidates(candidate_artifact_paths(_bank_root(root)))


def _verified_candidates(paths: list[Path]) -> list[BankedCandidate]:
    """Those of ``paths`` that parse, verify, and resolve both halves of an identity."""
    found: list[BankedCandidate] = []
    for path in paths:
        candidate = load_candidate_artifact(path)
        if candidate is None:
            continue
        bundle_session_id, capture_session_id = _identity_from_path(path)
        if not bundle_session_id or not capture_session_id:
            continue
        found.append(
            BankedCandidate(
                candidate=candidate,
                bundle_session_id=bundle_session_id,
                capture_session_id=capture_session_id,
                path=path,
            )
        )
    return found


def publish_authored_candidate(candidate: Any, *, root: Path | None = None) -> BankedCandidate:
    """Publish an idempotent authored bundle; do not open or abandon a capture."""
    from jasper.active_speaker.bundles import (  # lazy: candidate bank is used by status before NumPy loads
        BUNDLE_FILE_MODE, BUNDLE_SCHEMA_VERSION,
    )
    from jasper.active_speaker.round_bank import DEFAULT_CAMPAIGN_ROOT  # lazy: authoring-only writer
    from jasper.audio_measurement.bundles import write_json_artifact  # lazy: authoring-only writer

    if candidate.analysis.get("measurement_status") != "unmeasured":
        raise CandidateBankRefusal("authored_status_required", "an authored candidate must be unmeasured")
    bundle_id = f"authored-{candidate.fingerprint}"
    stores = _candidate_roots(_bank_root(root))
    destination = next((store for store in stores if store.name == DEFAULT_CAMPAIGN_ROOT.name), stores[0])
    path = destination / CANDIDATE_ARTIFACT_GLOB.replace("*", bundle_id, 1).replace("*", "authored", 1)
    bundle = path.parents[5]
    if path.exists():
        existing = load_candidate_artifact(path)
        if existing is None or existing.fingerprint != candidate.fingerprint:
            raise CandidateBankRefusal("authored_candidate_conflict", f"cannot reuse {path}")
        return BankedCandidate(existing, bundle_id, "authored", path)
    info: dict[str, Any] = {
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "kind": "jts_authored_candidate_bundle",
        "session_id": bundle_id,
        "started_at": time.time(),
        "measurement_status": "unmeasured",
        "purpose": "candidate_composition",
        "captures": [],
        "summed_captures": [],
        "verification": None,
    }
    for relative, payload in (("info.json", info), (str(path.relative_to(bundle)), candidate.to_dict())):
        write_json_artifact(
            bundle, relative, payload, kind=payload["kind"],
            sensitivity="config", recomputable=False,
            generated_by="active_speaker.candidate_parts",
            schema_version=BUNDLE_SCHEMA_VERSION, file_mode=BUNDLE_FILE_MODE,
        )
    reopened = load_candidate_artifact(path)
    if reopened is None or reopened.fingerprint != candidate.fingerprint:
        raise CandidateBankRefusal("authored_candidate_unreadable", f"cannot reopen {path}")
    return BankedCandidate(reopened, bundle_id, "authored", path)


def _identity_from_path(path: Path) -> tuple[str, str]:
    """``(bundle_session_id, capture_session_id)`` for one artifact path. Positional, not parsed:
    the glob fixes the depth (capture session is the artifact's own directory, bundle is
    five levels above).
    """
    parents = path.parents
    capture_session_id = parents[0].name
    bundle_session_id = parents[5].name if len(parents) > 5 else ""
    return bundle_session_id, capture_session_id


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
    wanted = str(fingerprint or "").strip()
    if not wanted:
        raise CandidateBankRefusal(
            "fingerprint_required", "a candidate fingerprint is required"
        )

    found: BankedCandidate | None = None
    examined = unverified = 0
    for path in _iter_candidate_paths(_bank_root(root)):
        rows = _verified_candidates([path])
        if not rows:
            unverified += 1
            continue
        one = rows[0]
        examined += 1
        if one.fingerprint != wanted:
            continue
        if found is not None and (
            one.bundle_session_id, one.capture_session_id
        ) != (found.bundle_session_id, found.capture_session_id):
            raise CandidateBankRefusal("ambiguous", "multiple banked lineages claim this fingerprint")
        found = one
    if found is None:
        raise CandidateBankRefusal(
            "not_found", f"no banked candidate matches ({examined} examined; {unverified} unverified)",
        )

    log_event(
        logger,
        "correction.crossover_v2_banked_candidate_found",
        candidate_fingerprint=found.fingerprint,
        bundle_session_id=found.bundle_session_id,
        capture_session_id=found.capture_session_id,
        examined=examined,
    )
    return found
