# ADR-0194: The flat-spec reference is the low-mid band, and the graded ceiling follows the microphone

- **Date:** 2026-08-29
- **Status:** Accepted

## Context

Two hand-set numbers in `flat_spec.py` had drifted away from what the tuning
program actually does.

**The top edge.** `SPEC_BANDS[-1]` stopped at 16 kHz and `BEST_EFFORT_ABOVE_HZ`
repeated that literal. The 2026-08-29 horn-droop correction ruling widened the
`reference` mic tier's trust to `(12 k, 20 k)` in `linearization_envelope`, so
the fitter is permitted to command up to 20 kHz and the delta probe's ceiling
moved with it. The spec table did not. Since then the band 16–20 kHz has been
**commandable** by the fit, **relatively** graded by the probe
(realized-vs-commanded), and **absolutely graded by nothing** — no check asked
whether the level up there was where it should be. The design doc named this
directly: a "hand-set analysis edge... which makes it a convention rather than
one derived quantity."

**The frame.** `REFERENCE_BAND_HZ` was `(250, 8000)` — `SPEC_BANDS[0] ∪
SPEC_BANDS[1]` — chosen so "a top-octave deficit cannot re-center the target."
That put the 2–8 kHz band *inside* the frame its own deviation is stated
against. Measured: a +1.00 dB elevation confined to 2–8 kHz lifts the reference
+0.474 dB, so the elevated band reports **+0.53** — half its real size — while
two untouched bands report **−0.47** out of nothing. On this speaker the real
2–8 kHz bulge moved the reference ~+0.35 dB and flattered every verdict,
including its own. This is issue #1857's mechanism, which the codebase had
already reproduced and then deliberately left standing: which anchor to use was
recorded as an open owner question (#1857's Q-E).

## Decision

**The graded ceiling is the session's microphone-trust ceiling.**
`evaluate_flat_spec` takes `trusted_ceiling_hz`, the mirror of the existing
`trusted_floor_hz`. The top band's upper edge and the best-effort boundary are
one number and move together — up on a mic trusted past 16 kHz, down on one
trusted below it; lower bands' upper edges are only ever lowered. The value is
consumed from `CrossoverV2Session._mic_trust_ceiling_hz`, which reads the taper
zero of `linearization_envelope.mic_trust_limit` — the same number that decides
where the fitter may command and where the probe may grade (#2649). No parallel
constant. `BEST_EFFORT_ABOVE_HZ` survives as the nominal value and
`SPEC_BANDS[-1]` references it rather than repeating the literal.

**Q-E is decided: the reference is `SPEC_BANDS[0]`** — the low-mid band alone,
the region a listener anchors tonality on and the tightest-toleranced row in the
table. No band above 2 kHz is pooled into the zero it is measured from.

This moves graded verdicts. That is the ruling, not a side effect. It does not
abolish the effect — a defect inside 250 Hz–2 kHz still drags its own frame —
which is why `spec_band_tilt`, frame-invariant by construction, remains the
reading to trust when the two disagree.

**Nothing here gates.** `_QUALITY_TABLE` is untouched and still keyed on
`(realization, benefit)` alone. Every field added is disclosure. The VERIFY
absolute claim (`verify_absolute_tolerance_db`) deliberately keeps reading the
nominal table, so it does not move with a session's ceiling; the consequence —
on a 20 kHz-trusted session the spec grades a region that claim still declines —
is recorded beside it as a separate question.

## Consequences

* **A trusted FLOOR can no longer leave a band unevaluable.** The reference band
  is now exactly `SPEC_BANDS[0]`, so a floor high enough to empty the low band
  empties the frame with it and `evaluate_flat_spec` raises — the existing
  "whole spec ungradeable" path, whose threshold moved from 8 kHz to 2 kHz. The
  ceiling is the clamp that reaches the unevaluable-band state now. Real floors
  on this rig are ~357 Hz, so nothing observable changes.
* **`blend_correction` was re-pointed.** It intersected its region with
  `report.reference_band_hz` under a comment saying it wanted "the span the flat
  spec actually grades," which that field never was; left alone it would have
  silently stopped correcting above 2 kHz. It reads the new
  `FlatSpecReport.graded_band_hz`. Its residual is still measured against
  `reference_db`, so its convergence loop reads a defect inside the frame more
  slowly — same fixed point, one to two more rounds on the worst case.
* **The #1857 corpus pin was re-frozen.** `VERDICT_GOLDEN` existed to prove the
  attribution split moved no graded number; that claim is no longer true by
  design. It is re-frozen at the new anchor as the post-ruling baseline.
  `max_ripple_db` (bit-identical) and `spec_band_tilt` (within 2 ULP across 16
  corpus shapes) did not move, as their frame-invariance requires.
* **Superseded:** the campaign's 250 Hz–8 kHz reference span
  (`docs/historical/linearization-campaign-2026-07.md`, "The spec — what 'flat'
  means here") and #1857's Q-E as an open question
  (`docs/historical/attribution-stage-plan.md` §9). The per-band tolerances,
  the 250 Hz lower edge's ownership in `room_boundary`, and their S0-contingent
  status are unchanged.
