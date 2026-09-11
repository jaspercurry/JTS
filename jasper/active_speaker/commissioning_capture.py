# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Driver repeat aggregation and its lifecycle event."""

from __future__ import annotations

import logging
import math
import statistics
import uuid
from typing import Any, Mapping, Sequence

from jasper.log_event import log_event

from ._common import finite_float as _finite_float
from .driver_acoustics import VERDICT_UNUSABLE_CAPTURE

logger = logging.getLogger(__name__)

DEFAULT_REPEAT_TARGET = 3
# |level_dbfs - running_median| beyond this rejects a repeat as an outlier.
REPEAT_OUTLIER_DB = 3.0
# spread_db_p90 above this downgrades confidence even at a full target.
REPEAT_CONFIDENCE_SPREAD_DB = 2.0

# The lane A/B SNR-verdict vocabulary ("ok"/"reduced"/"insufficient"/
# "unknown" — see snr_policy.py's band_snr_verdicts). Only the two
# non-informative ends reject a repeat outright.
_REPEAT_REJECT_SNR_VERDICTS = frozenset({"insufficient", "unknown"})


def _repeat_level_dbfs(acoustic: Mapping[str, Any]) -> float | None:
    """Broadband magnitude summary used for the median/spread computation.

    ``observed_mic_dbfs`` (the capture RMS) is the one scalar every
    analyzer result already carries, driver or summed — the natural
    "shared grid" comparison quantity across repeats taken under the same
    excitation/gain ledger.
    """

    return _finite_float(acoustic.get("observed_mic_dbfs"))


_REFERENCE_BINDING_KEYS = (
    "policy_id",
    "comparison_set_id",
    "comparison_set_fingerprint",
    "target_fingerprint",
    "speaker_group_id",
    "role",
)


def _reference_axis_repeat_binding(
    item: Mapping[str, Any], acoustic: Mapping[str, Any]
) -> tuple[str, ...] | None:
    """Return the immutable fixed-axis placement binding for one repeat.

    ``None`` means the repeat is not reference-axis. An empty tuple marks an
    explicitly reference-axis repeat without a complete server proof; callers
    reject it before it can influence the aggregate median.
    """

    if acoustic.get("capture_geometry") != "reference_axis":
        return None
    proof = item.get("placement_proof")
    if not isinstance(proof, Mapping):
        return ()
    from .capture_geometry import (
        REFERENCE_AXIS_DRIVER_PLACEMENT_POLICY_ID,
        placement_proof_shape_valid,
    )

    values = tuple(str(proof.get(key) or "") for key in _REFERENCE_BINDING_KEYS)
    if (
        not placement_proof_shape_valid(
            proof,
            policy_id=REFERENCE_AXIS_DRIVER_PLACEMENT_POLICY_ID,
            speaker_group_id=str(proof.get("speaker_group_id") or ""),
            role=str(proof.get("role") or ""),
            target_fingerprint=str(proof.get("target_fingerprint") or ""),
        )
        or any(not value for value in values)
    ):
        return ()
    return values


def _repeat_reject_reason(
    *,
    verdict: Any,
    acoustic: Mapping[str, Any],
    level_dbfs: float | None,
    running_median: float | None,
) -> str | None:
    """The outlier-rejection rule for one repeat.

    Reads the lane A/B SNR and validity-floor evidence. Legacy/near-field
    records without those sibling blocks retain level-outlier-only behavior.
    A record explicitly marked ``capture_geometry=reference_axis`` is held to
    the stronger Lane B contract: missing/ungateable validity evidence rejects
    as ``validity_floor_unknown``. Known-below-floor and insufficient SNR
    evidence also reject before the level check.
    """

    gating = acoustic.get("gating")
    overlap_entries = [
        entry
        for entry in acoustic.get("overlap_levels") or ()
        if isinstance(entry, Mapping)
    ]
    if acoustic.get("capture_geometry") == "reference_axis":
        floor_value = gating.get("f_valid_floor_hz") if isinstance(gating, Mapping) else None
        floor_hz = (
            None
            if isinstance(floor_value, bool)
            else _finite_float(floor_value)
            if isinstance(gating, Mapping)
            else None
        )
        floor_states = [
            entry.get("above_validity_floor") for entry in overlap_entries
        ]
        if (
            not isinstance(gating, Mapping)
            or gating.get("applied") is not True
            or floor_hz is None
            or floor_hz <= 0
            or any(state is None for state in floor_states)
        ):
            return "validity_floor_unknown"
        if not overlap_entries:
            return "no_usable_overlap"
    if verdict in (None, VERDICT_UNUSABLE_CAPTURE):
        return "unusable_capture"
    if bool(acoustic.get("mic_clipping")):
        return "clipping"
    if isinstance(gating, Mapping) and gating.get("above_validity_floor") is False:
        return "below_validity_floor"
    if overlap_entries:
        floor_states = [
            entry.get("above_validity_floor") for entry in overlap_entries
        ]
        if floor_states and all(state is False for state in floor_states):
            return "below_validity_floor"
        # A woofer's unusable bottom octave or a 3-way mid's unusable lower
        # handoff must not veto a clean required overlap at the other edge.
        # ``usable`` is the analyzer-owned conjunction of bins, local SNR,
        # clipping and validity floor. Reject only when no topology overlap can
        # support the trim decision.
        if not any(entry.get("usable") is True for entry in overlap_entries):
            return "no_usable_overlap"
    else:
        # Legacy/fabricated analyzers without topology overlap entries retain
        # the coarse whole-passband SNR behavior.
        snr = acoustic.get("snr")
        snr_verdict = snr.get("verdict") if isinstance(snr, Mapping) else None
        if snr_verdict in _REPEAT_REJECT_SNR_VERDICTS:
            return f"snr_{snr_verdict}"
    if level_dbfs is None:
        return "level_unavailable"
    if (
        running_median is not None
        and abs(level_dbfs - running_median) > REPEAT_OUTLIER_DB
    ):
        return "level_outlier"
    return None


def aggregate_driver_repeats(
    repeats: Sequence[Mapping[str, Any]],
    *,
    target: int = DEFAULT_REPEAT_TARGET,
) -> dict[str, Any]:
    """Aggregate N per-driver (or summed) repeat captures via outlier rejection.

    Each item in ``repeats`` carries at minimum ``verdict`` and ``acoustic``
    plus whatever else the caller wants preserved
    (``artifact_path``, ``excitation``, ``placement_proof``, ...) — the
    whole item is echoed back verbatim as ``aggregate_repeat`` when it wins.

    Processes repeats IN ORDER, comparing each against the running median of
    already-*accepted* levels (:func:`_repeat_reject_reason`) — never
    against the group's final median, so an early anchor is not
    retroactively second-guessed once later repeats arrive. Magnitude only:
    there is no complex/IR input path here, and the aggregate reuses the
    ACCEPTED repeat closest to the final median's full ``acoustic`` block
    verbatim — never a synthesized average across curves, and never a
    claimed SNR improvement (repeats reject outliers; they do not reduce
    the noise floor).

    ``needed_recapture`` signals "short of target and the ONE bounded extra
    attempt hasn't been used yet" (``len(repeats) <= target``); a caller
    reads it to decide whether to capture one more attempt (append it and
    call again) or finalize with whatever is accepted so far (confidence
    degrades to ``"reduced"`` below the full ``target`` accepted or above
    the spread floor). ``recaptured`` records whether that extra attempt
    actually happened (``len(repeats) > target``).
    """

    per_repeat: list[dict[str, Any]] = []
    accepted_levels: list[float] = []
    accepted_indices: list[int] = []
    reference_axis_binding: tuple[str, ...] | None = None
    sequence_geometry: str | None = None

    for index, item in enumerate(repeats):
        acoustic = item.get("acoustic")
        acoustic = acoustic if isinstance(acoustic, Mapping) else {}
        verdict = item.get("verdict")
        level_dbfs = _repeat_level_dbfs(acoustic)
        running_median = (
            statistics.median(accepted_levels) if accepted_levels else None
        )
        binding = _reference_axis_repeat_binding(item, acoustic)
        geometry = acoustic.get("capture_geometry")
        reason: str | None
        if geometry in {"near_field", "reference_axis"} and sequence_geometry is None:
            sequence_geometry = str(geometry)
        if geometry in {"near_field", "reference_axis"} and geometry != sequence_geometry:
            reason = "capture_context_mismatch"
        elif binding == ():
            reason = "reference_axis_placement_unbound"
        elif binding is not None and reference_axis_binding not in (None, binding):
            reason = "capture_context_mismatch"
        else:
            reason = _repeat_reject_reason(
                verdict=verdict,
                acoustic=acoustic,
                level_dbfs=level_dbfs,
                running_median=running_median,
            )
        if binding and reference_axis_binding is None:
            reference_axis_binding = binding
        accepted = reason is None
        snr = acoustic.get("snr")
        worst_relevant_raw = snr.get("worst_relevant") if isinstance(snr, Mapping) else None
        worst_relevant: Mapping[str, Any] = (
            worst_relevant_raw if isinstance(worst_relevant_raw, Mapping) else {}
        )
        gating_raw = acoustic.get("gating")
        gating: Mapping[str, Any] = (
            gating_raw if isinstance(gating_raw, Mapping) else {}
        )
        overlap_floor_evidence = [
            entry.get("above_validity_floor")
            for entry in acoustic.get("overlap_levels") or ()
            if isinstance(entry, Mapping)
            and entry.get("above_validity_floor") in (True, False, None)
        ]
        if isinstance(gating.get("above_validity_floor"), bool):
            above_validity_floor: bool | None = bool(
                gating["above_validity_floor"]
            )
        elif any(value is True for value in overlap_floor_evidence):
            above_validity_floor = True
        elif overlap_floor_evidence and all(
            value is False for value in overlap_floor_evidence
        ):
            above_validity_floor = False
        elif acoustic.get("capture_geometry") == "reference_axis":
            above_validity_floor = None
        else:
            above_validity_floor = True
        per_repeat.append({
            "index": index,
            "attempt": int(item.get("attempt") or index + 1),
            "verdict": verdict,
            "accepted": accepted,
            "reject_reason": reason,
            "artifact_path": item.get("artifact_path"),
            "estimated_snr_db": _finite_float(worst_relevant.get("estimated_snr_db")),
            "clipping": bool(acoustic.get("mic_clipping")),
            "above_validity_floor": above_validity_floor,
            "level_dbfs": level_dbfs,
            "capture_admission": (
                dict(item["capture_admission"])
                if isinstance(item.get("capture_admission"), Mapping)
                else None
            ),
        })
        if accepted and level_dbfs is not None:
            accepted_levels.append(level_dbfs)
            accepted_indices.append(index)
        if len(accepted_levels) >= target:
            break  # bounded: stop once the target accepted count is reached

    accepted_count = len(accepted_levels)
    rejected_count = sum(1 for entry in per_repeat if not entry["accepted"])
    attempts = len(per_repeat)

    spread_db_p90: float | None = None
    aggregate_repeat: dict[str, Any] | None = None
    if accepted_count >= 1:
        median_level = statistics.median(accepted_levels)
        if accepted_count >= 2:
            deviations = sorted(
                abs(level - median_level) for level in accepted_levels
            )
            rank = min(
                len(deviations) - 1, max(0, math.ceil(0.9 * len(deviations)) - 1)
            )
            spread_db_p90 = deviations[rank]
        winner_local = min(
            range(accepted_count),
            key=lambda i: abs(accepted_levels[i] - median_level),
        )
        aggregate_repeat = dict(repeats[accepted_indices[winner_local]])

    # DEVIATION (flagged in the PR body): the STEP 1 CONTRACT §9 text reads
    # "confidence = normal when accepted >= 2 AND spread_db_p90 <= 2.0" —
    # taken literally, that can never actually gate anything given
    # REPEAT_OUTLIER_DB=3.0: with exactly 2 accepted, the second is always
    # checked directly against the running median built from the first, so
    # their spread is mathematically bounded to <= REPEAT_OUTLIER_DB / 2 ==
    # 1.5, always under the 2.0 floor — "two accepted" would ALWAYS read
    # "normal", indistinguishable from a full three, contradicting both the
    # required test ("refusing the re-capture -> proceeds with two,
    # confidence reduced") and the product intent of the field (fewer
    # repeats than the protocol calls for is honestly lower-confidence
    # evidence). Reading "2" as shorthand for "enough to take a median at
    # all" and gating "normal" on reaching the full `target` instead
    # resolves the contradiction and keeps the spread check meaningful as
    # an ADDITIONAL floor once target is reached.
    confidence = (
        "normal"
        if (
            accepted_count >= target
            and spread_db_p90 is not None
            and spread_db_p90 <= REPEAT_CONFIDENCE_SPREAD_DB
        )
        else "reduced"
    )
    recaptured = attempts > target
    needed_recapture = accepted_count < target and attempts <= target

    return {
        "repeat_group_id": uuid.uuid4().hex[:12],
        "target": target,
        "accepted": accepted_count,
        "rejected": rejected_count,
        "recaptured": recaptured,
        "needed_recapture": needed_recapture,
        "aggregate": "median_magnitude",
        "spread_db_p90": spread_db_p90,
        "confidence": confidence,
        "per_repeat": per_repeat,
        "aggregate_repeat": aggregate_repeat,
    }


def record_driver_repeat_aggregate(
    *,
    speaker_group_id: str,
    role: str,
    repeats: Sequence[Mapping[str, Any]],
    target: int = DEFAULT_REPEAT_TARGET,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Aggregate driver repeats and emit the repeats-aggregated lifecycle event.

    Logs ``correction.crossover_repeats_aggregated`` (SC-5) via
    :func:`jasper.log_event.log_event`. A pure evidence step: it does not
    itself call ``record_driver_measurement`` — the web orchestration layer
    uses the returned ``aggregate_repeat`` exactly to re-analyze and record
    the winning capture.  Durable measurement state receives only the compact
    counters, spread, and ``per_repeat`` projection; the process-local winner
    object and full repeat artifacts remain in the commissioning bundle.
    """

    aggregate = aggregate_driver_repeats(repeats, target=target)
    log_event(
        logger,
        "correction.crossover_repeats_aggregated",
        session=session_id,
        group=speaker_group_id,
        role=role,
        accepted=aggregate["accepted"],
        rejected=aggregate["rejected"],
        spread_db=aggregate["spread_db_p90"],
        confidence=aggregate["confidence"],
    )
    return aggregate
