# Research brief #2: the iterative dial-in loop + measurement-position awareness

> Written 2026-07-29 by the round-3 architect session at the owner's request.
> Companion to `RESEARCH-BRIEF-measure-diagnose-prescribe.md` (whose report is
> preserved as `RESEARCH-dissertation-measure-diagnose-prescribe.md`). Feeds
> the attribution-stage plan's iterate stage (#1866) and the position-aware
> clouds issue. Same deliverable style: per-question findings with primary
> sources, then **copy / skip / why** for JTS.

## Context (one paragraph)

JTS's correction flow today is single-shot: measure → propose → apply →
verify. The owner has ruled the target shape is a **bounded dial-in loop**
(up to ~3 attempts): apply the most-aggressive-defensible change first,
re-measure, treat the predicted-vs-measured delta as *evidence* (a change
that under-delivers reveals a null, compression, or a wrong model — each
routes differently), revise, and stop honestly ("as good as it's gonna
get"). Constraint: **minimize measurement count, maximize information per
measurement** — a session must never feel like 30 minutes of mic-holding,
but 8 captures are fine when the incremental information is real. The
measurement instrument is a phone or USB mic moved by hand around a desk
speaker (8-position "cloud"), with µs-anchored captures and per-position
data persisted.

## Q1 — Iterative correction loops in the wild

Who ships (or documents) measure → apply → re-measure → revise loops, and
what can we steal about their loop design and stopping rules?

- **Audiology / hearing-aid fitting** — the richest prior art: real-ear
  measurement iterating against prescriptive targets (NAL-NL2, DSL),
  verification-then-adjustment cycles, documented convergence practice.
  What is their attempt-count norm, what triggers "stop," and how do they
  handle the patient-fatigue analogue of our session-time budget?
- **Trinnov / Dirac / GLM re-calibration practice**: do any of them
  formalize a second pass informed by the first (vs just "run it again")?
  Trinnov calibrator workflows (pro AV) may document iterative refinement.
- **Automotive audio tuning** workflows (measure-tune-measure with DSP
  rigs) — loop structure and operator heuristics.
- **Live-sound system tuning** practice (Smaart-driven): the
  measure-adjust-verify cadence, and the "aggressive first, then trim
  back" heuristic the owner proposed — is that documented practice?

## Q2 — Sequential / optimal experiment design for the next measurement

The theory of "what single measurement teaches the most, given what we
already know":

- **Optimal experiment design** (D-/A-optimality) and **active learning /
  Bayesian optimization** applied to acoustic response estimation or
  filter tuning — any audio applications in the literature?
- **Sparse room-response sampling**: compressed-sensing and interpolation
  results for reconstructing a response field from few points (Mignot,
  Daudet et al. on room impulse response interpolation; kernel/plane-wave
  models). What does point N+1 add, and where should it go?
- Practical yield: a principled-but-simple rule JTS could ship for "given
  attempt-1 results, the next session should capture positions X/Y/Z and
  probes P/Q only" — the measurement-economy criterion made operational.

## Q3 — Intervention-response as a diagnostic (the delta-probe generalized)

Using a KNOWN applied change as the stimulus and the measured delta as
the evidence:

- System-identification and model-updating literature: what does a
  shortfall between predicted and realized response indicate, and how do
  practitioners discriminate (interference null — the delta vanishes into
  a notch; compression — the delta shrinks with level; wrong plant
  model — the delta is frequency-shifted)?
- Prior art for "EQ move under-delivered ⇒ suspect a null" as an explicit
  documented rule (REW/practitioner guidance, room-EQ folklore made
  citable).
- How to design the FIRST intervention to be maximally informative
  (aggressive-first as experiment design, not just impatience): does the
  literature support largest-defensible-move-first, and what bounds it
  (hearing safety, headroom, reversibility)?

## Q4 — Measurement-position awareness: what does precision actually buy?

The owner's question, sharpened: we currently collect a scatter-shot
cloud with rough relative labels ("moved left"). Options ladder from
free to heavy:

1. **Acoustic time-of-flight (free, data we already have)**: captures are
   µs-anchored; arrival-time deltas across positions give RADIAL
   distance changes without any sensor. What ranging precision does
   sweep TOF give in-room (temperature drift, reflections), and what
   attribution value does radial-only geometry unlock (distance
   normalization for combining, boundary-distance fits for SBIR)?
2. **Phone IMU dead-reckoning**: is it categorically infeasible for
   absolute position over a multi-minute session (drift rates), or
   usable for RELATIVE step direction/magnitude ("did the user actually
   move ~2 ft left")?
3. **WebXR / ARKit / ARCore positional tracking from the BROWSER**
   (2026 state): availability on iOS Safari + Android Chrome, cm-level
   accuracy claims, whether an AR session can run concurrently with
   getUserMedia capture, battery/thermal cost, and the UX tax of a
   camera-permission AR flow inside a measurement session.
4. What do position-aware commercial systems do with position? Trinnov's
   tetrahedral mic (direction-of-arrival per reflection), GLM's
   listener-position input, Dirac's 9/13/17-point patterns — how much of
   their value is the *pattern prescription* vs *knowing* the positions?

The decision this feeds: is position precision worth ANY complexity, and
if so which rung? The owner's null hypothesis — "scatter-shot + rough
labels is enough because the speaker is the constant" — should be given a
fair shot at winning: identify exactly which attributions (SBIR distance
fits, geometry gates like #1874, window-vs-power framing, per-position
distance normalization) materially improve with position knowledge and
which don't care.

## Q5 — Stopping rules and household honesty

- Convergence criteria for bounded loops (3 attempts): what metric
  improvement justifies attempt N+1, and what does honest "as good as
  it's gonna get" copy look like (GRADE-style tiering of the residual)?
- Path-independence checks as trust builders: does the literature discuss
  re-running a calibration from a reset state to validate convergence
  (our planned UMIK reset-and-redo experiment), and what tolerance counts
  as "same answer"?

## Deliverable

Per question: findings with primary sources, then copy/skip/why. Close
with: **the iterate stage the prior art would build for JTS** — loop
structure, first-move policy, next-measurement selection rule, stopping
copy — and **a position-awareness verdict** (which rung of the ladder, if
any, is warranted at which program stage).
