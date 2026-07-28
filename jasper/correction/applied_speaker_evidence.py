# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read-only seam: what the APPLIED speaker profile knows about itself.

Room correction and the active-speaker (gated) layer have always shared a
frequency region without sharing any evidence: ``jasper/correction/`` imported
*none* of the validity floor, the gated spec curve, or the interference-null
registry. This module is the one-way window between them — it resolves the
speaker profile that is **currently applied** and hands back the gated
measurement evidence that rode in with it.

Nothing in the room layer consumes this yet. It exists so the RC4 Tier B
residual-trend correction has a seam to build on: Tier B's correction target is
the in-room curve *minus* the speaker's own gated response, so that the room
layer never re-flattens voicing the speaker layer deliberately set. That
subtraction is only honest if the gated curve provably belongs to the profile
that is playing right now, which is what this module establishes and refuses
when it cannot.

The three refusals, and why each is a refusal rather than a fallback
-------------------------------------------------------------------
Every "no" here returns :class:`AppliedSpeakerEvidenceAbsent` with a machine
reason, never a partially-populated result and never a default curve. The
plan's rule is *unknown is not zero*: an absent gated curve must disable Tier B
with disclosure, because falling back to the raw in-room curve is precisely the
double-correction Tier B exists to prevent.

* ``no_applied_profile`` — nothing is applied (a passive speaker, a
  pre-commissioning room, or a recommission in progress).
* ``candidate_unresolved`` — a profile is applied but the measured candidate it
  names cannot be found or parsed. Its evidence bundle was pruned (raw session
  bundles are retention-capped), or it predates candidate publication.
* ``no_cloud_evidence`` — the applied profile came from an **electrical-only**
  commission. Those write the same ``measured_candidate_fingerprint`` field
  from a :class:`MeasuredElectricalCandidate` but publish no acoustic
  candidate, so without this case they would masquerade as ``candidate_stale``
  ("re-measured since") when nothing acoustic was ever measured.
* ``candidate_stale`` — a candidate was found but it is not the one the applied
  profile was built from. **This module does not invent a staleness rule**: it
  reuses the identity the apply path already recorded,
  ``source.measured_candidate_fingerprint``, written by
  :func:`jasper.active_speaker.baseline_profile._source_payload` and preserved
  through the frozen applied-profile projection. That fingerprint is the same
  one :meth:`MeasuredCrossoverCandidate.fingerprint` recomputes from the
  candidate's own contents on load, so a tampered or edited candidate cannot
  match.
* ``evidence_absent`` — the candidate resolved and is current, but carries no
  gated evidence. Either it predates the RC1 payload extension (era
  tolerance — see below), or its fit never consumed cloud evidence at all, in
  which case ``exclusion_evidence`` is empty by design.

Era tolerance
-------------
The validity floor and gated spec curve ride *inside*
``MeasuredCrossoverCandidate.exclusion_evidence``, beside the null registry
that was already candidate-borne. That placement is what makes the extension
safe with no schema bump: ``exclusion_evidence`` is a free-form mapping that
participates in the candidate fingerprint **only when non-empty**, so a
candidate written before RC1 keeps its old payload, recomputes its old
fingerprint, and still validates — it simply lacks the two new keys, and this
module reports ``evidence_absent`` for it.
"""
from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jasper.log_event import log_event

logger = logging.getLogger(__name__)

# How many candidate artifacts to examine before giving up. The bundle root is
# retention-capped (DEFAULT_SESSIONS_MAX_BUNDLES = 12) with a handful of
# crossover_v2 sessions each, so a healthy box is far under this. The cap is
# here so a pathological directory cannot turn a read into a directory walk of
# unbounded cost on a 1 GB Pi.
MAX_CANDIDATE_ARTIFACTS_SCANNED = 64

# Largest candidate.json this reader will parse. The publisher's own artifact
# budget is smaller; this is a defensive ceiling on untrusted-size input, not a
# contract.
MAX_CANDIDATE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class AppliedSpeakerEvidence:
    """Gated-measurement evidence belonging to the currently-applied profile.

    ``validity_floor_hz`` is ``None`` when the group never established one —
    "unverified", never 0 Hz. Consumers must treat ``None`` as *no floor
    known* and refuse to lean on it rather than substituting a default.
    """

    candidate_fingerprint: str
    validity_floor_hz: float | None
    gated_spec_freqs_hz: tuple[float, ...]
    gated_spec_magnitude_db: tuple[float, ...]
    null_registry: Mapping[str, Any] = field(default_factory=dict)

    @property
    def has_gated_curve(self) -> bool:
        return bool(self.gated_spec_freqs_hz) and len(
            self.gated_spec_freqs_hz
        ) == len(self.gated_spec_magnitude_db)

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": True,
            "candidate_fingerprint": self.candidate_fingerprint,
            "validity_floor_hz": self.validity_floor_hz,
            "gated_spec_points": len(self.gated_spec_freqs_hz),
        }


@dataclass(frozen=True)
class AppliedSpeakerEvidenceAbsent:
    """Typed 'no evidence', with the machine reason a surface can disclose."""

    reason: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": False,
            "reason": self.reason,
            "detail": self.detail,
        }


AppliedSpeakerEvidenceResult = AppliedSpeakerEvidence | AppliedSpeakerEvidenceAbsent


def _absent(reason: str, detail: str = "") -> AppliedSpeakerEvidenceAbsent:
    # DEBUG is deliberate WHILE NOTHING CONSUMES THIS. Today an absent result
    # changes no behaviour, so logging it at INFO would be journal noise on
    # every passive speaker and every pre-commissioning room. RC4 must revisit:
    # once Tier B depends on this, "evidence absent" becomes a user-visible
    # capability being disabled, and the plan requires that be DISCLOSED, not
    # merely debuggable — raise the level and surface the reason on the session
    # surface at the same time as the first consumer lands.
    log_event(
        logger,
        "correction.applied_speaker_evidence_absent",
        level=logging.DEBUG,
        reason=reason,
        detail=detail,
    )
    return AppliedSpeakerEvidenceAbsent(reason=reason, detail=detail)


def _float_tuple(values: Any) -> tuple[float, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    out: list[float] = []
    for value in values:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return ()
        as_float = float(value)
        if not math.isfinite(as_float):
            return ()
        out.append(as_float)
    return tuple(out)


def _expected_candidate_fingerprint(profile: Mapping[str, Any]) -> str:
    source = profile.get("source")
    if not isinstance(source, Mapping):
        return ""
    value = source.get("measured_candidate_fingerprint")
    return value if isinstance(value, str) else ""


def _candidate_artifact_paths(root: Path) -> list[Path]:
    """Every published candidate.json under the bundle root, newest last.

    Sorted so the scan is deterministic; the fingerprint decides the match, not
    the ordering.

    When there are more than :data:`MAX_CANDIDATE_ARTIFACTS_SCANNED` artifacts
    the scan keeps the **newest** ones (``[-MAX:]``, not ``[:MAX]``). Which end
    is dropped is load-bearing, not a detail: the applied profile's candidate
    is overwhelmingly likely to be among the most recent, so truncating from
    the front would discard exactly the artifact being looked for and report a
    false ``candidate_stale`` — evidence silently disabled on the boxes with
    the most measurement history.
    """
    try:
        found = sorted(root.glob("*/evidence/v1/artifacts/crossover_v2/*/candidate.json"))
    except OSError:
        return []
    return found[-MAX_CANDIDATE_ARTIFACTS_SCANNED:]


def _load_candidate(path: Path) -> Any | None:
    """Parse one candidate artifact, or None if it cannot be trusted."""
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
        # from_mapping refuses malformed / tampered candidates with its own
        # typed error. A refusal here is "this artifact is not usable
        # evidence", which is the same answer as "not found" for our purposes.
        return None


def resolve_applied_speaker_evidence(
    *,
    state_path: Path | None = None,
    sessions_dir: Path | None = None,
) -> AppliedSpeakerEvidenceResult:
    """Resolve the applied speaker profile's gated evidence, or say why not.

    Read-only: touches no runtime state, writes nothing, and never raises for
    a missing / pruned / stale / pre-extension input — every one of those is an
    :class:`AppliedSpeakerEvidenceAbsent` with a machine reason.

    ``state_path`` and ``sessions_dir`` exist for tests; production callers
    pass neither and get the real baseline-profile state and bundle root.
    """
    from jasper.active_speaker.baseline_profile import (
        load_applied_baseline_profile_state,
    )
    from jasper.active_speaker.bundles import sessions_dir as default_sessions_dir

    try:
        profile = load_applied_baseline_profile_state(state_path)
    except (OSError, TypeError, ValueError) as exc:
        # The reader already swallows a missing / unparseable file; this
        # catches a state file that is valid JSON but structurally wrong
        # (hand-edited), which must read as "nothing applied", not a crash in
        # whatever surface asked.
        return _absent("no_applied_profile", type(exc).__name__)
    if not isinstance(profile, Mapping):
        return _absent("no_applied_profile", "no applied baseline profile")

    expected = _expected_candidate_fingerprint(profile)
    if not expected:
        # Pre-candidate-era applied profiles never recorded which measured
        # candidate they came from. There is no identity to gate on, so there
        # is no honest way to claim the evidence belongs to what is playing.
        return _absent(
            "candidate_unresolved",
            "applied profile records no measured_candidate_fingerprint",
        )

    if not profile.get("linearization"):
        # An ELECTRICAL-only commission (MeasuredElectricalCandidate) writes
        # the same `source.measured_candidate_fingerprint` field as an
        # acoustic one, but publishes no crossover_v2 candidate — so scanning
        # for one would find nothing that matches and report `candidate_stale`,
        # i.e. "this speaker was re-measured since," when the truth is "this
        # speaker was never commissioned with acoustic cloud evidence at all."
        # Empty `linearization` is the honest discriminator, and it is the SAME
        # gate the producer uses to decide whether evidence rides the candidate
        # (`if cloud is not None and linearization`) — so the two agree by
        # construction. Checked before the scan: it is decisive and free.
        return _absent(
            "no_cloud_evidence",
            "applied profile was commissioned without acoustic cloud evidence",
        )

    root = sessions_dir if sessions_dir is not None else default_sessions_dir()
    paths = _candidate_artifact_paths(Path(root))
    if not paths:
        return _absent("candidate_unresolved", "no published candidate artifacts")

    seen = 0
    for path in paths:
        candidate = _load_candidate(path)
        if candidate is None:
            continue
        seen += 1
        if candidate.fingerprint != expected:
            continue
        return _evidence_from_candidate(candidate)

    if seen:
        # Candidates exist, but none is the applied one: the applied profile's
        # candidate aged out of retention, or the speaker has been re-measured
        # since (recommission in progress).
        return _absent(
            "candidate_stale",
            f"no candidate matches the applied fingerprint ({seen} examined)",
        )
    return _absent("candidate_unresolved", "no readable candidate artifacts")


def _evidence_from_candidate(candidate: Any) -> AppliedSpeakerEvidenceResult:
    evidence = candidate.exclusion_evidence
    if not isinstance(evidence, Mapping) or not evidence:
        # Empty by design when no cloud evidence entered the fit.
        return _absent("evidence_absent", "candidate carries no cloud evidence")

    curve = evidence.get("gated_spec_curve")
    floor_raw = evidence.get("validity_floor_hz")
    if "gated_spec_curve" not in evidence and "validity_floor_hz" not in evidence:
        # Era tolerance: a candidate published before the RC1 payload
        # extension. Its other evidence is still valid; these two keys simply
        # did not exist yet.
        return _absent(
            "evidence_absent",
            "candidate predates the applied-speaker evidence extension",
        )

    freqs: tuple[float, ...] = ()
    mags: tuple[float, ...] = ()
    if isinstance(curve, Mapping):
        freqs = _float_tuple(curve.get("freqs_hz"))
        mags = _float_tuple(curve.get("magnitude_db"))
    if len(freqs) != len(mags):
        freqs = ()
        mags = ()

    floor: float | None = None
    if isinstance(floor_raw, (int, float)) and not isinstance(floor_raw, bool):
        as_float = float(floor_raw)
        if math.isfinite(as_float) and as_float > 0.0:
            floor = as_float

    if floor is None and not freqs:
        return _absent(
            "evidence_absent",
            "candidate carries neither a validity floor nor a gated curve",
        )

    registry = evidence.get("null_registry")
    return AppliedSpeakerEvidence(
        candidate_fingerprint=candidate.fingerprint,
        validity_floor_hz=floor,
        gated_spec_freqs_hz=freqs,
        gated_spec_magnitude_db=mags,
        null_registry=dict(registry) if isinstance(registry, Mapping) else {},
    )
