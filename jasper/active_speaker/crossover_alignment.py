"""L2 phase-aware crossover alignment — measurement-mode gate + refinement proposal.

Owns two things and no I/O. The phase_aware gate is downgrade-only: a
polarity/delay decision needs a calibrated mic, because an uncalibrated phone's
phase error is ±20–40° at the crossover
(``docs/research/2026-06-19-active-crossover-calibration``). The alignment
proposal judges polarity from the reverse-vs-in-phase null margin, so the
measurement's dynamic-range cap cancels. No delay VALUE is proposed here:
browser captures have no sample-sync to the Pi's playback, so an IR arrival
delta is capture jitter, not time-of-flight — the value comes from the
timing-locked null walk, and this surfaces only a delay STATUS.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jasper.audio_measurement.quality_model import DRIVER

from .driver_acoustics import DEFAULT_NULL_THRESHOLD_DB

# Measurement modes. magnitude_only is L1 (any mic); phase_aware is L2 and is
# gated on a calibrated mic.
MAGNITUDE_ONLY = "magnitude_only"
PHASE_AWARE = "phase_aware"
MEASUREMENT_MODES = frozenset({MAGNITUDE_ONLY, PHASE_AWARE})

# The reverse-polarity null being this many dB DEEPER than the in-phase null (or
# vice-versa) is a confident call. Relative, so the smoothed-shoulder
# measurement's dynamic-range cap cancels.
POLARITY_MARGIN_DB = 8.0

# Polarity actions (what the human is asked to confirm).
POLARITY_KEEP = "keep"      # in-phase sums, reverse cancels → polarity correct
POLARITY_INVERT = "invert"  # in-phase cancels, reverse sums → propose a flip
POLARITY_REVIEW = "review"  # inconclusive → surface, don't auto-decide

# Summed in-phase blend classification (the direct time+phase alignment evidence).
BLEND_FLAT = "flat"      # sums cleanly → time-aligned AND in phase
BLEND_NULL = "null"      # deep cancellation → polarity or delay off
BLEND_UNKNOWN = "unknown"

# Delay status. The VALUE comes from the timing-locked walk; this is the
# in-phase null read.
DELAY_ALIGNED = "aligned"
DELAY_NEEDS_ALIGNMENT = "needs_alignment"
DELAY_UNKNOWN = "unknown"


@dataclass(frozen=True)
class ResolvedMode:
    """The effective measurement mode after the calibrated-mic gate."""

    mode: str  # the EFFECTIVE mode actually granted
    requested: str
    downgraded: bool
    reason: str | None  # why it was downgraded, e.g. "no_calibrated_mic"

    @property
    def phase_aware(self) -> bool:
        return self.mode == PHASE_AWARE

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "requested": self.requested,
            "downgraded": self.downgraded,
            "reason": self.reason,
        }


def resolve_measurement_mode(
    requested: str | None,
    *,
    has_calibrated_mic: bool,
) -> ResolvedMode:
    """Gate the measurement mode on a calibrated mic. Downgrade-only.

    ``phase_aware`` is granted only when ``has_calibrated_mic`` is True;
    otherwise the request downgrades to ``magnitude_only`` with reason
    ``"no_calibrated_mic"``, as does an unknown/empty request. NEVER upgrades.
    """
    req = (requested or MAGNITUDE_ONLY).strip().lower()
    if req not in MEASUREMENT_MODES:
        req = MAGNITUDE_ONLY
    if req == PHASE_AWARE and not has_calibrated_mic:
        return ResolvedMode(
            mode=MAGNITUDE_ONLY,
            requested=PHASE_AWARE,
            downgraded=True,
            reason="no_calibrated_mic",
        )
    return ResolvedMode(mode=req, requested=req, downgraded=False, reason=None)


@dataclass(frozen=True)
class CrossoverAlignmentProposal:
    """A measured, SAFE crossover refinement awaiting human confirmation.

    ``authorized`` is False (no polarity/delay decision) unless the effective
    mode is ``phase_aware``. All values are PROPOSALS: the caller previews the
    resulting baseline and the human confirms. Each instance covers ONE
    crossover region, so a 3-way has two;
    ``commissioning_capture.build_crossover_alignment_proposal`` is the caller
    that iterates the regions.
    """

    authorized: bool
    mode: str
    crossover_fc_hz: float
    lower_role: str
    upper_role: str
    # Polarity — proposal only; the mixer 'inverted' flag carries it on apply.
    polarity: str  # "normal" | "invert_<role>"
    polarity_action: str  # keep | invert | review
    polarity_margin_db: float | None  # reverse_null - in_phase_null, when both present
    # Delay STATUS (not a value — the timing-locked reverse-null walk owns the value).
    delay_status: str  # aligned | needs_alignment | unknown
    # Null evidence.
    in_phase_null_depth_db: float | None
    reverse_null_depth_db: float | None
    summed_blend: str  # flat | null | unknown
    issues: tuple[dict[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "jts_active_speaker_crossover_alignment",
            "authorized": self.authorized,
            "mode": self.mode,
            "crossover_fc_hz": self.crossover_fc_hz,
            "lower_role": self.lower_role,
            "upper_role": self.upper_role,
            "polarity": self.polarity,
            "polarity_action": self.polarity_action,
            "polarity_margin_db": self.polarity_margin_db,
            "delay_status": self.delay_status,
            "in_phase_null_depth_db": self.in_phase_null_depth_db,
            "reverse_null_depth_db": self.reverse_null_depth_db,
            "summed_blend": self.summed_blend,
            "issues": [dict(issue) for issue in self.issues],
        }


def _issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def _classify_blend(in_phase_null_depth_db: float | None) -> str:
    if in_phase_null_depth_db is None:
        return BLEND_UNKNOWN
    return BLEND_NULL if in_phase_null_depth_db >= DEFAULT_NULL_THRESHOLD_DB else BLEND_FLAT


def _alignment_snr_issue(null_depth_capped: bool) -> dict[str, str]:
    """The ``alignment_snr_insufficient`` issue for a degraded proposal.

    ``null_depth_capped`` is checked first, being the more specific fact. Names
    the REQUIRED overlap-band SNR (``DRIVER.alignment_snr_ok_db``); the ACHIEVED
    dB is not in scope here, so it is not claimed.
    """
    required = DRIVER.alignment_snr_ok_db
    if null_depth_capped:
        detail = (
            f"the measured null is deeper than the overlap-band SNR can prove "
            f"(needs roughly {required:.0f} dB there)"
        )
    else:
        detail = f"overlap-band SNR is below the ~{required:.0f} dB alignment needs"
    return _issue(
        "warning",
        "alignment_snr_insufficient",
        f"polarity/delay downgraded to review: {detail}",
    )


def propose_crossover_alignment(
    *,
    mode: str,
    crossover_fc_hz: float,
    lower_role: str,
    upper_role: str,
    in_phase_null_depth_db: float | None = None,
    reverse_null_depth_db: float | None = None,
    alignment_snr_ok: bool | None = None,
    null_depth_capped: bool = False,
) -> CrossoverAlignmentProposal:
    """Propose crossover polarity (+ a delay status) from calibrated null evidence.

    Gated: when ``mode`` is not ``phase_aware`` the proposal is **unauthorized**
    (no polarity/delay decision) with a ``requires_calibrated_mic`` issue — the
    enforcement that an uncalibrated phone can never authorize a phase decision.

    In ``phase_aware``, polarity comes from the reverse-vs-in-phase null margin
    (cap-independent) when both captures exist, with a single-capture fallback:

    * both: reverse ≫ in-phase → keep; in-phase ≫ reverse → invert; similar → review.
    * in-phase only: deep null → invert *candidate* (confirm with a reverse capture);
      flat → keep *tentatively* (capture reverse for the full proof).
    * reverse only: a null formed → keep *tentatively*; none → review.

    The delay STATUS is read straight from the in-phase null (flat = time-aligned;
    deep null = needs the alignment walk); the delay VALUE is the walk's job.

    A SECOND, independent gate sits on top of the calibrated-mic gate: even a
    calibrated (``phase_aware``) capture needs enough overlap-band SNR to trust
    a null/alignment call (see "Level control and SNR" in
    docs/active-crossover-information-design.md and
    ``jasper.audio_measurement.snr_policy``). ``alignment_snr_ok`` carries that
    verdict — ``True`` only when a real per-band SNR reading cleared the
    stricter alignment threshold, ``False`` only when real evidence proved it
    insufficient. ``None`` means unknown/no evidence and DELIBERATELY does not
    degrade: it preserves the shipped margin/blend behavior for legacy callers
    and captures without usable ambient evidence. The commissioning repeat flow
    now passes the paired ambient verdict when that evidence is available.
    When ``alignment_snr_ok is False``: any ``keep``/``invert`` polarity action
    downgrades to ``review``, and ``delay_status`` can never read ``aligned``
    (downgrades to ``unknown``). ``null_depth_capped`` (either contributing
    null depth was capped) forces the same downgrade independently, even when
    ``alignment_snr_ok`` is ``True``:
    a capped depth is, by construction, not fully proven. The margin/blend
    classification above is computed from the RAW depths regardless — only
    the outward action/status are downgraded, so the evidence stays visible
    for review.
    """
    blend = _classify_blend(in_phase_null_depth_db)
    margin = (
        reverse_null_depth_db - in_phase_null_depth_db
        if in_phase_null_depth_db is not None and reverse_null_depth_db is not None
        else None
    )

    if mode != PHASE_AWARE:
        return CrossoverAlignmentProposal(
            authorized=False,
            mode=mode,
            crossover_fc_hz=crossover_fc_hz,
            lower_role=lower_role,
            upper_role=upper_role,
            polarity="normal",
            polarity_action=POLARITY_REVIEW,
            polarity_margin_db=margin,
            delay_status=DELAY_UNKNOWN,
            in_phase_null_depth_db=in_phase_null_depth_db,
            reverse_null_depth_db=reverse_null_depth_db,
            summed_blend=blend,
            issues=(
                _issue(
                    "info",
                    "requires_calibrated_mic",
                    "polarity/delay need a calibrated measurement mic (phase_aware); "
                    "only level matching is available without one",
                ),
            ),
        )

    issues: list[dict[str, str]] = []
    polarity = "normal"

    if margin is not None:
        # Both captures: the relative margin is the robust, cap-independent call.
        if margin >= POLARITY_MARGIN_DB:
            action = POLARITY_KEEP
        elif margin <= -POLARITY_MARGIN_DB:
            action = POLARITY_INVERT
            polarity = f"invert_{upper_role}"
            issues.append(_issue(
                "warning",
                "polarity_inverted_evidence",
                f"in-phase null is {-margin:.0f} dB deeper than the reverse-polarity "
                f"null → the branches are out of phase; invert {upper_role}",
            ))
        else:
            action = POLARITY_REVIEW
            issues.append(_issue(
                "warning",
                "polarity_ambiguous",
                "neither polarity cancels clearly more than the other "
                f"(margin {margin:.0f} dB) — check delay/wiring/hardware",
            ))
    elif in_phase_null_depth_db is not None:
        # In-phase only.
        if blend == BLEND_NULL:
            action = POLARITY_INVERT
            polarity = f"invert_{upper_role}"
            issues.append(_issue(
                "warning",
                "summed_null_detected",
                "deep in-phase null at the crossover — candidate polarity flip "
                f"(invert {upper_role}); confirm with a reverse-polarity null capture",
            ))
        else:
            action = POLARITY_KEEP
            issues.append(_issue(
                "info",
                "reverse_null_not_captured",
                "in-phase sum is flat; capture a reverse-polarity sweep for the full "
                "polarity proof",
            ))
    elif reverse_null_depth_db is not None:
        # Reverse only.
        if reverse_null_depth_db >= DEFAULT_NULL_THRESHOLD_DB:
            action = POLARITY_KEEP
            issues.append(_issue(
                "info",
                "polarity_tentative_from_reverse",
                "reverse-polarity null formed (consistent with correct polarity); "
                "capture the in-phase sweep to confirm",
            ))
        else:
            action = POLARITY_REVIEW
            issues.append(_issue(
                "warning",
                "reverse_null_absent",
                "no reverse-polarity null where one was expected — "
                "check polarity/delay/wiring/hardware",
            ))
    else:
        action = POLARITY_REVIEW
        issues.append(_issue(
            "info",
            "no_summed_capture",
            "capture the summed crossover (and a reverse-polarity sweep) to judge "
            "polarity",
        ))

    # Delay status from the in-phase null (the VALUE is the deferred walk's job).
    if blend == BLEND_FLAT:
        delay_status = DELAY_ALIGNED
    elif blend == BLEND_NULL:
        delay_status = DELAY_NEEDS_ALIGNMENT
        issues.append(_issue(
            "info",
            "delay_walk_recommended",
            "a deep in-phase null remains — if it persists after the polarity "
            "decision, run the reverse-polarity delay walk to time-align",
        ))
    else:
        delay_status = DELAY_UNKNOWN

    # Second, independent gate: a confirmed insufficient SNR or a capped null
    # depth may not produce a confident keep/invert or an "aligned" delay
    # status. alignment_snr_ok=None does NOT degrade. This never tightens the
    # margin/blend classification, only the outward action/status.
    if alignment_snr_ok is False or null_depth_capped:
        degraded = False
        if action in (POLARITY_KEEP, POLARITY_INVERT):
            action = POLARITY_REVIEW
            # A "review" action always pairs with polarity="normal", so reset
            # it here too: a degraded candidate ("invert_tweeter") must not
            # leak out looking like a still-standing recommendation.
            polarity = "normal"
            degraded = True
        if delay_status == DELAY_ALIGNED:
            delay_status = DELAY_UNKNOWN
            degraded = True
        if degraded:
            issues.append(_alignment_snr_issue(null_depth_capped))

    return CrossoverAlignmentProposal(
        authorized=True,
        mode=PHASE_AWARE,
        crossover_fc_hz=crossover_fc_hz,
        lower_role=lower_role,
        upper_role=upper_role,
        polarity=polarity,
        polarity_action=action,
        polarity_margin_db=margin,
        delay_status=delay_status,
        in_phase_null_depth_db=in_phase_null_depth_db,
        reverse_null_depth_db=reverse_null_depth_db,
        summed_blend=blend,
        issues=tuple(issues),
    )
