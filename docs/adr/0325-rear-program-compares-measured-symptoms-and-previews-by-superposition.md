# ADR-0325: The rear program compares measured symptoms and previews by superposition

- **Date:** 2026-09-18
- **Status:** Accepted
- **Extends:** ADR-0317, ADR-0318, ADR-0322, ADR-0324

## Context

The rear (cardioid) stage of ADR-0322 shipped with a simulated seed and no
way to tune it: the branch takes were gated above the band the stage works
in, no packet view compared rear settings, and the prescription contract had
no `rear` section. Issue #5330 planned a fourth tune program beside speaker,
room and bass. The first hardware rounds on jts3 (2026-09-17/18, six rounds
with the arm) fixed the shape of the evidence: the front microphone alone
cannot tell a real cardioid (less wall bounce) from an in-phase fill (more
rear energy) — both raise the wall dip — but a recording of each woofer
alone on one clock lets software predict the front response of any document
to 0.4–0.8 dB RMS over 100–400 Hz, closer than two repeats of one sweep
agree. Below 80 Hz the dynamic bass block is not a filter and the prediction
is not trusted.

## Decision

1. **A fourth program, `rear`.** Purpose `rear`, rows `rear/express`,
   `rear/wide` (summed, ungated via the seat exemption) and `rear/pair`,
   `rear/pair_behind` (regime `branches`, pair `front_rear`). It lives in the
   same registry, view table, contract, handoff and trial loop as the other
   programs; nothing in it depends on which mover moved the microphone.
2. **One band, one reference, measured symptoms only.** A summed round is
   judged on one comparison band (a windowed search for the wall dip around
   the declared geometry, else the section band, else the hand-over, else
   coverage) against one reference curve per position (the one-octave trend
   of the rear-muted take). The figures are symptoms and costs — dip depth
   and width, ripple, hand-over hole, low-bass level, absolute band level,
   headroom change, repeat spread from true repeats, worst regression across
   positions. No rejection claim, no score, no ranking: the LLM judges.
3. **The pair take is the preview's input.** `rear/pair` records front alone,
   rear alone, repeats and both together, ungated, on one clock, on a
   candidate the run composes itself with the rear section cleared (raw
   woofers through the ordinary woofer chain). Its evidence is F, R and P
   per position with the superposition residual `P − |F + R|` as the trust
   number, the arrival gap with its confidence, and the rear polarity. Any
   document is previewed as `S = F·H_front + R·(H_bass + H_cancel)` with the
   emitter's own `branch_chain.rear_stage_response`; the preview is trusted
   where the residual is small and never below the bass block's band.
4. **Cardioid check and rear-side evidence.** `gradient_residual_db` (the
   whole-chain ratio against an ideal delay-and-invert at the measured gap)
   is a diagnostic tie-break, never a certificate. The measurement that can
   certify rejection is the same pair take with the microphone behind the
   cabinet (`rear/pair_behind`, pose kind `behind`); the rear null of any
   document is then predicted by the same superposition.
5. **Geometry and the seed stay inputs.** The declared wall distance and cone
   spacing are inputs to the search window and the first-tune recipe; a
   measured arrival gap never overwrites them (ADR-0317). The simulated seed
   is the incumbent to beat, not a calibration (ADR-0318).
6. **First-tune recipe (playbook `## Rear`).** Complementary Linkwitz-Riley
   hand-over at one shared corner near 100 Hz (70–80 Hz a variant, judged on
   `low_bass` and `band_level_db`), cancellation low-pass below `c / (4·D)`
   with D from the measured gap, inverted cancellation branch with its delay
   anchored on that gap, rear gain 0 dB with a cut-only shelf when the rear
   reads louder, bass branch in phase.

## Consequences

- A geometry change is "declare it, run a round, let the LLM re-author": no
  constant moves, no setting is born.
- The front-side figures have a ceiling the hardware sets: with rear gain
  ≤ 0 dB the wall dip can rise by at most `20·log10(1 + |R|/|F|)` at the
  microphone (about +4 dB at 0° on jts3). Pushing past it from the front is
  in-phase fill, which the contract's gain bound forbids; the rear-side take
  is the honest next lever.
- The arrival gap has two physical answers (first diffracted arrival ≈ cone
  spacing; wall-assisted effective path ≈ 0.55 m on jts3). The packet
  discloses the estimator's confidence; a phase-slope figure is owed (#5364).
- Rejected: matching the rear's phase to the front at a front microphone and
  then inverting (that recipe nulls the front or builds the fill); a free
  optimiser on front-side smoothness alone (it finds backward cardioids and
  fills); a rejection claim from any front-hemisphere figure.

Evidence: issue #5330 (plan revisions 4–5a, the reviewer's clarifications,
the overnight report of 2026-09-18); PRs #5337–#5339, #5344, #5345, #5347–#5349,
#5358–#5363.
