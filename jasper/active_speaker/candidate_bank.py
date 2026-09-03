# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Find a previously-minted measured candidate in the on-box bundle store.

The v2 apply door is single-slot, but every published candidate is durable, so
this is the read half: locate a banked candidate by its own fingerprint. This
module owns the fingerprint SCAN — :mod:`jasper.correction.applied_speaker_evidence`
delegates here rather than restating the glob. Not the only place production
spells that path: ``correction_crossover_v2._reopen_candidate_artifact`` builds
it too, resolving an already-known session id rather than searching.

**One owner for the fingerprint SCAN.** The glob below describes the on-disk
shape
(``<bundle>/evidence/v1/artifacts/crossover_v2/<relay_session_id>/candidate.json``).
A second reader of that shape must read it from here rather than keep a copy,
because a shape restated in two places drifts on the first layout change and
both readers then disagree about what is missing.

Scoped honestly: this is not the only place production spells that path.
``correction_crossover_v2._reopen_candidate_artifact`` builds it too, and is
deliberately left alone — it resolves an ALREADY-KNOWN session id rather than
searching by fingerprint, and it sits on the apply path. Unifying the two is a
follow-up, not a claim this module already makes.

**Integrity is the candidate model's, never a second hasher.**
:meth:`~jasper.active_speaker.measured_crossover_candidate.MeasuredCrossoverCandidate.from_mapping`
recomputes the fingerprint from the artifact's own ``_core()`` and refuses
``candidate_tampered`` when the stored value disagrees, so a byte edited on
disk cannot survive a load. This module adds bounds (how many artifacts, how
many bytes) and identity resolution (which bundle, which relay session); it
computes nothing about the candidate itself. Both the mint path
(``bind_evidence_publishers.publish_candidate``) and the apply path
(``handle_v2_apply``) verify through that same ``from_mapping``, so reusing it
here introduces no second hasher rather than merely matching one.

**Lineage is part of the answer.** A banked candidate is only re-applyable if
the apply path can find its artifact again, and that lookup is keyed on the
bundle id *and* the minting relay session id together. Both ride on
:class:`BankedCandidate` for that reason, not as provenance decoration.

**Why it sits here and not under ``crossover_v2/``.** Its two collaborators —
:mod:`jasper.active_speaker.bundles` and
:mod:`jasper.active_speaker.measured_crossover_candidate` — are both at this
level, and importing the ``crossover_v2`` package runs its ``__init__`` →
``contracts`` → **numpy**. Filing the bank under ``crossover_v2/`` made it pull
numpy (measured, not assumed), spending a budget its consumers never agreed to;
this module stays import-light for the same reason, lazy-importing the candidate
model *inside* its loader rather than at the top. Keep it out — and note the
same rule pointed the other way for
``correction_crossover_v2``, which imports this lazily at the call site because
it is a wizard-hosted module.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jasper.log_event import log_event

logger = logging.getLogger(__name__)

#: Where a published crossover-v2 candidate artifact lives, relative to the
#: bundle root. ``*`` one is the bundle id (``bundles.open_bundle``'s 12-hex
#: session id, which is also the directory name); ``*`` two is the MINTING
#: relay session id. The two namespaces are distinct on disk and conflating
#: them joins the wrong round to the wrong bundle.
CANDIDATE_ARTIFACT_GLOB = "*/evidence/v1/artifacts/crossover_v2/*/candidate.json"

#: How many candidate artifacts to PARSE before giving up, bounding the
#: expensive half (up to 4 MB of JSON plus a fingerprint recompute each) on a
#: 1 GB Pi. It does NOT bound the fixed-depth directory walk.
MAX_CANDIDATE_ARTIFACTS_SCANNED = 64

#: Largest candidate.json this reader will parse: a ceiling on input size, not
#: a contract — the publisher's own artifact budget is smaller.
MAX_CANDIDATE_BYTES = 4 * 1024 * 1024


class CandidateBankRefusal(LookupError):
    """No single trustworthy banked candidate answers this fingerprint.

    Carries a machine ``code`` beside the household sentence so a door maps it
    to its own response contract without parsing prose.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class BankedCandidate:
    """One banked candidate plus the identity needed to re-open it later.

    ``bundle_session_id`` and ``relay_session_id`` together are the ONLY way
    back to this artifact: the apply path rebuilds the path from a state's
    ``evidence.bundle_session_id`` and ``session_id``, so a republish that
    dropped either would publish a candidate whose artifact nothing can find.
    """

    candidate: Any
    bundle_session_id: str
    relay_session_id: str
    path: Path

    @property
    def fingerprint(self) -> str:
        return str(self.candidate.fingerprint)


def candidate_artifact_paths(root: Path) -> list[Path]:
    """Every published candidate.json under the bundle root, in a stable order.

    Sorted for determinism only, NOT chronological: the bundle directory is
    named ``uuid4().hex[:12]``, so path order carries no time information.
    :data:`MAX_CANDIDATE_ARTIFACTS_SCANNED` therefore bounds work rather than
    selecting recent history, and which end it truncates is arbitrary — a root
    that overran the cap could drop an artifact that is on disk and report it
    missing. The bundle root's own retention cap keeps a healthy box far under.
    """
    try:
        found = sorted(Path(root).glob(CANDIDATE_ARTIFACT_GLOB))
    except OSError:
        return []
    return found[-MAX_CANDIDATE_ARTIFACTS_SCANNED:]


def _identity_from_path(path: Path) -> tuple[str, str]:
    """``(bundle_session_id, relay_session_id)`` for one artifact path.

    Positional rather than parsed: the glob fixes the depth, so the minting
    relay session is the artifact's own directory and the bundle is five levels
    above it.
    """
    parents = path.parents
    relay_session_id = parents[0].name
    bundle_session_id = parents[5].name if len(parents) > 5 else ""
    return bundle_session_id, relay_session_id


def load_candidate_artifact(path: Path) -> Any | None:
    """Parse and integrity-check one candidate artifact, or ``None``.

    ``None`` covers an unreadable file, an oversized one, malformed JSON, and a
    payload whose recomputed fingerprint disagrees with the one it declares. A
    candidate that cannot survive an exact reopen must never become applyable.
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
    """The one banked candidate with this fingerprint, or a typed refusal.

    Fail-closed three ways: ``fingerprint_required`` (nothing was asked for),
    ``not_found`` (no artifact both matches and verifies — the detail separates
    "none matched" from "the match would not verify", different problems with
    different fixes), and ``ambiguous`` (two DIFFERENT minting lineages carry
    this fingerprint; guessing which round to credit is what this must not do).
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
            # An artifact IS there and failed its own integrity check; without
            # this the operator reads "not found" and hunts for a measurement
            # that is on disk, corrupted.
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
