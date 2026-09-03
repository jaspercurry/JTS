# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Find a previously-minted measured candidate in the on-box bundle store.

**Why this exists.** The v2 apply door is single-slot: it applies whatever
``state["candidate"]`` currently names, and that slot is rewritten by every
measure session and cleared by a failed one. The candidates themselves are
durable — each MEASURE publishes its artifact write-once into a commissioning
bundle — so "the candidate is unreachable" is a *state* problem, never a
*storage* one. This module is the read half of the fix: locate a banked
candidate by its own fingerprint so a door can make it live again.

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

#: How many candidate artifacts to PARSE before giving up. The bundle root is
#: retention-capped (``DEFAULT_SESSIONS_MAX_BUNDLES = 12``) with a handful of
#: crossover_v2 sessions each, so a healthy box is far under this. What the cap
#: bounds is the expensive half — up to 4 MB of JSON plus a fingerprint
#: recompute per artifact — so a pathological directory cannot turn one lookup
#: into unbounded work on a 1 GB Pi. It does NOT bound the directory walk: the
#: glob is fixed-depth and ``sorted()`` completes before the slice is taken,
#: which is cheap and deliberately left alone.
MAX_CANDIDATE_ARTIFACTS_SCANNED = 64

#: Largest candidate.json this reader will parse. The publisher's own artifact
#: budget is smaller; this is a defensive ceiling on untrusted-size input, not a
#: contract.
MAX_CANDIDATE_BYTES = 4 * 1024 * 1024


class CandidateBankRefusal(LookupError):
    """No single trustworthy banked candidate answers this fingerprint.

    Carries a machine ``code`` beside the household sentence so a door can map
    it to its own response contract without parsing prose.
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

    Sorted for determinism only. **Not chronological**, and deliberately not
    claimed to be: the glob's first wildcard is a bundle directory named
    ``uuid4().hex[:12]`` (:func:`~jasper.active_speaker.bundles.open_bundle`),
    so path order carries no time information. Only ``started_at`` in each
    bundle's ``info.json`` does — :mod:`~jasper.active_speaker.bundles` already
    orders by it — and this scan needs none of it: the fingerprint decides the
    match.

    :data:`MAX_CANDIDATE_ARTIFACTS_SCANNED` therefore BOUNDS work rather than
    selecting recent history, and which end it truncates is arbitrary because
    the order is. A root that overran the cap could drop an artifact that is on
    disk and report it missing; the bundle root's own retention cap (12) is
    what keeps a healthy box far under it.
    """
    try:
        found = sorted(Path(root).glob(CANDIDATE_ARTIFACT_GLOB))
    except OSError:
        return []
    return found[-MAX_CANDIDATE_ARTIFACTS_SCANNED:]


def _identity_from_path(path: Path) -> tuple[str, str]:
    """``(bundle_session_id, relay_session_id)`` for one artifact path.

    Positional rather than parsed: the glob above fixes the depth, so the
    minting relay session is the artifact's own directory and the bundle is
    five levels above it (``crossover_v2`` / ``artifacts`` / ``v1`` /
    ``evidence``).
    """
    parents = path.parents
    relay_session_id = parents[0].name
    bundle_session_id = parents[5].name if len(parents) > 5 else ""
    return bundle_session_id, relay_session_id


def load_candidate_artifact(path: Path) -> Any | None:
    """Parse and integrity-check one candidate artifact, or ``None``.

    ``None`` means "this artifact is not usable", which covers an unreadable
    file, an oversized one, malformed JSON, and — the case that matters — a
    payload whose recomputed fingerprint disagrees with the one it declares.
    Refusing rather than repairing is the whole point: a candidate that cannot
    survive an exact reopen must never become applyable.
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

    Fail-closed in all three directions the caller cannot distinguish on its
    own:

    * ``fingerprint_required`` — nothing was asked for.
    * ``not_found`` — no artifact both matches and verifies. The detail
      separates the two populations, because "none matched" and "the one that
      matched would not verify" are different problems with different fixes,
      and reporting a corrupted artifact as simply absent would send an
      operator looking for a measurement that is right there on disk.
    * ``ambiguous`` — two DIFFERENT minting lineages carry this fingerprint.
      Identical cores hash identically, so this means the same candidate was
      minted twice; the artifacts agree but their lineage does not, and lineage
      is published. Guessing which round to credit is the one thing this must
      not do.
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
            # Says the useful thing: an artifact IS there and it failed its own
            # integrity check. Without this the operator reads "not found" and
            # goes looking for a measurement that is sitting on disk, corrupted.
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
