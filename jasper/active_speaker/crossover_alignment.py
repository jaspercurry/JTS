"""L2 phase-aware crossover alignment — measurement-mode gate + refinement proposal.

This is the L2 ("enthusiast, calibrated mic") brain on top of L1's magnitude-only
level match. It owns two things and no I/O:

1. **The phase_aware gate.** A phase/delay/polarity decision is only trustworthy
   from a real *calibrated* measurement mic — an uncalibrated phone's phase error
   is ±20–40° at the crossover (research snapshot
   ``docs/research/2026-06-19-active-crossover-calibration``). So
   :func:`resolve_measurement_mode` is **downgrade-only**: it grants
   ``phase_aware`` only when a calibrated mic is present, otherwise falls back to
   ``magnitude_only``. It never silently upgrades.

2. **The alignment proposal.** :func:`propose_crossover_alignment` turns the
   measured evidence (per-driver near-field arrival times + the summed-crossover
   null depth) into a SAFE, measurement-driven refinement: per-driver **delay**
   (delay whichever acoustic source arrives *earlier* — measured, never the
   reflexive "delay the tweeter"; a horn can make the tweeter later) and
   **polarity** (from the reverse-polarity null proof). It is a *proposal*: it
   shows the evidence and the human confirms; it never silently rewrites the
   design, and it never proposes Fc/slope changes (explicitly out of scope) or a
   positive gain (level matching is L1's attenuation-only job).

The canonical method is ``docs/HANDOFF-active-speaker-dsp.md`` "Delay, Phase, and
Null Verification". The robust gold standard is an interactive delay walk
(maximize the reverse-polarity null); this module ships the single-shot
arrival-time *estimate* + the null *check* that seed and validate it, and labels
the delay an estimate because separate near-field captures are not loop-back
timing-locked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .driver_acoustics import (
    DEFAULT_NULL_THRESHOLD_DB,
    REVERSE_NULL_MIN_DB,
    REVERSE_NULL_STRONG_DB,
)

# Measurement modes. magnitude_only is L1 (any mic); phase_aware is L2 and is
# gated on a calibrated mic.
MAGNITUDE_ONLY = "magnitude_only"
PHASE_AWARE = "phase_aware"
MEASUREMENT_MODES = frozenset({MAGNITUDE_ONLY, PHASE_AWARE})

# Delay clamp mirrors the emit/baseline contract (camilla_yaml._emit_delay_filter
# and baseline_profile both clamp 0..20 ms).
MAX_DELAY_MS = 20.0
# A measured arrival delta below this is within the capture's timing jitter, so we
# report "aligned" rather than chasing noise into a non-zero delay.
MIN_PROPOSED_DELAY_MS = 0.05

# Polarity actions (what the human is asked to confirm).
POLARITY_KEEP = "keep"      # measured in-phase + reverse-null proof both pass
POLARITY_INVERT = "invert"  # drivers measure out of phase → propose a flip
POLARITY_REVIEW = "review"  # ambiguous / failed proof → surface, don't auto-decide

# Summed in-phase blend classification.
BLEND_FLAT = "flat"
BLEND_NULL = "null"
BLEND_UNKNOWN = "unknown"

# Reverse-polarity null verdict.
REVERSE_STRONG = "strong"
REVERSE_MARGINAL = "marginal"
REVERSE_FAIL = "fail"


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

    ``phase_aware`` is granted only when ``has_calibrated_mic`` is True; otherwise
    the request is downgraded to ``magnitude_only`` with reason
    ``"no_calibrated_mic"``. An unknown/empty request resolves to
    ``magnitude_only``. This function NEVER upgrades — a caller can only ever get
    a mode at most as privileged as requested — so a phone can never authorize a
    phase/delay/polarity decision.
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

    ``authorized`` is False (no delay/polarity values) unless the effective mode
    is ``phase_aware`` (a calibrated mic). All values are PROPOSALS — the caller
    previews the resulting baseline (which re-proves the runtime_contract tweeter
    guard + the 0 dB ceiling) and the human confirms before anything is applied.
    """

    authorized: bool
    mode: str
    crossover_fc_hz: float
    lower_role: str
    upper_role: str
    # Delay — delay whichever driver arrives EARLIER (measured), clamped 0..20 ms.
    delay_ms: float | None
    delay_target_role: str | None
    delay_confidence: str  # "estimate" | "aligned" | "none"
    # Polarity — proposal only; the mixer 'inverted' flag carries it on apply.
    polarity: str  # "normal" | "invert_<role>"
    polarity_action: str  # keep | invert | review
    # Null evidence.
    in_phase_null_depth_db: float | None
    reverse_null_depth_db: float | None
    summed_blend: str  # flat | null | unknown
    reverse_verdict: str | None  # strong | marginal | fail | None
    issues: tuple[dict[str, str], ...]
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "jts_active_speaker_crossover_alignment",
            "authorized": self.authorized,
            "mode": self.mode,
            "crossover_fc_hz": self.crossover_fc_hz,
            "lower_role": self.lower_role,
            "upper_role": self.upper_role,
            "delay_ms": self.delay_ms,
            "delay_target_role": self.delay_target_role,
            "delay_confidence": self.delay_confidence,
            "polarity": self.polarity,
            "polarity_action": self.polarity_action,
            "in_phase_null_depth_db": self.in_phase_null_depth_db,
            "reverse_null_depth_db": self.reverse_null_depth_db,
            "summed_blend": self.summed_blend,
            "reverse_verdict": self.reverse_verdict,
            "issues": [dict(issue) for issue in self.issues],
            "evidence": dict(self.evidence),
        }


def _issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def _classify_reverse(reverse_null_depth_db: float | None) -> str | None:
    if reverse_null_depth_db is None:
        return None
    if reverse_null_depth_db >= REVERSE_NULL_STRONG_DB:
        return REVERSE_STRONG
    if reverse_null_depth_db >= REVERSE_NULL_MIN_DB:
        return REVERSE_MARGINAL
    return REVERSE_FAIL


def _classify_blend(in_phase_null_depth_db: float | None) -> str:
    if in_phase_null_depth_db is None:
        return BLEND_UNKNOWN
    return BLEND_NULL if in_phase_null_depth_db >= DEFAULT_NULL_THRESHOLD_DB else BLEND_FLAT


def _propose_delay(
    *,
    lower_role: str,
    upper_role: str,
    lower_arrival_s: float | None,
    upper_arrival_s: float | None,
) -> tuple[float | None, str | None, str]:
    """(delay_ms, delay_target_role, confidence) from per-driver arrival times.

    Delay whichever source arrives EARLIER so both branches meet in time. A delta
    inside the timing-jitter floor reads "aligned" (no delay). Missing arrivals
    (a silent/unusable capture) → no delay. Always an estimate, never auto-applied.
    """
    if lower_arrival_s is None or upper_arrival_s is None:
        return None, None, "none"
    delta_ms = abs(upper_arrival_s - lower_arrival_s) * 1000.0
    if delta_ms < MIN_PROPOSED_DELAY_MS:
        return 0.0, None, "aligned"
    delay_ms = min(delta_ms, MAX_DELAY_MS)
    # The earlier arriver (smaller arrival time) is the one to delay.
    target = lower_role if lower_arrival_s < upper_arrival_s else upper_role
    return round(delay_ms, 3), target, "estimate"


def propose_crossover_alignment(
    *,
    mode: str,
    crossover_fc_hz: float,
    lower_role: str,
    upper_role: str,
    lower_arrival_s: float | None = None,
    upper_arrival_s: float | None = None,
    in_phase_null_depth_db: float | None = None,
    reverse_null_depth_db: float | None = None,
) -> CrossoverAlignmentProposal:
    """Propose per-driver delay + polarity from calibrated phase-aware evidence.

    Gated: when ``mode`` is not ``phase_aware`` the proposal is **unauthorized**
    (no delay/polarity values) with a ``requires_calibrated_mic`` issue — the
    enforcement that an uncalibrated phone can never authorize a phase decision.

    In ``phase_aware``:

    * **Delay**: delay the earlier-arriving driver by the measured arrival delta
      (clamped 0..20 ms), labeled an ``estimate`` — validate it with the
      reverse-polarity null before trusting it.
    * **Polarity**: the textbook proof is in-phase-flat + reverse-polarity-deep.
      - reverse deep (pass) + in-phase flat → ``keep`` (polarity correct).
      - in-phase deep null + reverse shallow → drivers measure out of phase →
        ``invert`` (propose flipping the upper driver).
      - anything ambiguous (only one capture, both deep, both flat, reverse fail)
        → ``review`` — surfaced for the human, never auto-flipped.
    """
    blend = _classify_blend(in_phase_null_depth_db)
    reverse_verdict = _classify_reverse(reverse_null_depth_db)
    evidence: dict[str, Any] = {
        "lower_arrival_s": lower_arrival_s,
        "upper_arrival_s": upper_arrival_s,
    }

    if mode != PHASE_AWARE:
        return CrossoverAlignmentProposal(
            authorized=False,
            mode=mode,
            crossover_fc_hz=crossover_fc_hz,
            lower_role=lower_role,
            upper_role=upper_role,
            delay_ms=None,
            delay_target_role=None,
            delay_confidence="none",
            polarity="normal",
            polarity_action=POLARITY_REVIEW,
            in_phase_null_depth_db=in_phase_null_depth_db,
            reverse_null_depth_db=reverse_null_depth_db,
            summed_blend=blend,
            reverse_verdict=reverse_verdict,
            issues=(
                _issue(
                    "info",
                    "requires_calibrated_mic",
                    "delay/polarity need a calibrated measurement mic "
                    "(phase_aware); only level matching is available without one",
                ),
            ),
            evidence=evidence,
        )

    issues: list[dict[str, str]] = []

    delay_ms, delay_target, delay_conf = _propose_delay(
        lower_role=lower_role,
        upper_role=upper_role,
        lower_arrival_s=lower_arrival_s,
        upper_arrival_s=upper_arrival_s,
    )
    if delay_conf == "estimate":
        issues.append(_issue(
            "info",
            "delay_is_estimate",
            f"delay {delay_target} ~{delay_ms:.2f} ms is a near-field arrival "
            "estimate — validate by maximizing the reverse-polarity null before apply",
        ))
    elif delay_conf == "none":
        issues.append(_issue(
            "info",
            "delay_no_arrival",
            "no per-driver arrival times available; capture both drivers near-field "
            "to estimate delay",
        ))

    # Polarity decision from the reverse-polarity null proof + the in-phase blend.
    # Truth table (in-phase summed, reverse-polarity summed):
    #   correct polarity → in-phase FLAT, reverse DEEP null → keep
    #   wrong polarity   → in-phase DEEP null, reverse FLAT → invert
    #   both deep        → contradictory (wiring/delay/hardware) → review
    #   both flat/unknown→ no clear crossover interaction → review
    polarity = "normal"
    reverse_ok = reverse_verdict in (REVERSE_STRONG, REVERSE_MARGINAL)
    if reverse_ok and blend != BLEND_NULL:
        # A deep reverse-polarity null is the canonical proof the branches meet in
        # phase; a flat (or not-yet-captured) in-phase sum is consistent with it.
        action = POLARITY_KEEP
        if reverse_verdict == REVERSE_MARGINAL:
            issues.append(_issue(
                "info",
                "reverse_null_marginal",
                f"reverse-polarity null {reverse_null_depth_db:.0f} dB is acceptable "
                f"but below the {REVERSE_NULL_STRONG_DB:.0f} dB strong-pass mark",
            ))
        elif blend == BLEND_UNKNOWN:
            issues.append(_issue(
                "info",
                "polarity_proved_by_reverse_null",
                "reverse-polarity null confirms in-phase summation (in-phase sweep "
                "not separately captured)",
            ))
    elif reverse_verdict == REVERSE_FAIL and blend == BLEND_NULL:
        # In-phase nulls AND the reverse capture does NOT null → the branches are out
        # of phase in the current config (both signals agree). Propose the flip.
        action = POLARITY_INVERT
        polarity = f"invert_{upper_role}"
        issues.append(_issue(
            "warning",
            "polarity_inverted_evidence",
            f"in-phase null + flat reverse sum → the branches are out of phase; "
            f"invert {upper_role}",
        ))
    elif reverse_verdict is None and blend == BLEND_NULL:
        # Deep in-phase null, no reverse proof captured: a polarity OR delay problem.
        # Propose a flip CANDIDATE, ask for the reverse proof, require confirm.
        action = POLARITY_INVERT
        polarity = f"invert_{upper_role}"
        issues.append(_issue(
            "warning",
            "summed_null_detected",
            "deep in-phase null at the crossover — candidate polarity flip "
            f"(invert {upper_role}); confirm with a reverse-polarity null capture",
        ))
    elif reverse_verdict is None and blend == BLEND_FLAT:
        action = POLARITY_KEEP
        issues.append(_issue(
            "info",
            "reverse_null_not_captured",
            "in-phase sum is flat; capture a reverse-polarity sweep for the full "
            "polarity proof",
        ))
    elif reverse_ok and blend == BLEND_NULL:
        # Both polarities null — contradictory; don't decide.
        action = POLARITY_REVIEW
        issues.append(_issue(
            "warning",
            "polarity_ambiguous",
            "summed null present in BOTH polarities — check wiring/delay/hardware; "
            "not proposing a flip",
        ))
    else:
        action = POLARITY_REVIEW
        issues.append(_issue(
            "info",
            "polarity_needs_review",
            "polarity evidence is inconclusive — review the surfaced curves and "
            "the null depths",
        ))

    return CrossoverAlignmentProposal(
        authorized=True,
        mode=PHASE_AWARE,
        crossover_fc_hz=crossover_fc_hz,
        lower_role=lower_role,
        upper_role=upper_role,
        delay_ms=delay_ms,
        delay_target_role=delay_target,
        delay_confidence=delay_conf,
        polarity=polarity,
        polarity_action=action,
        in_phase_null_depth_db=in_phase_null_depth_db,
        reverse_null_depth_db=reverse_null_depth_db,
        summed_blend=blend,
        reverse_verdict=reverse_verdict,
        issues=tuple(issues),
        evidence=evidence,
    )
