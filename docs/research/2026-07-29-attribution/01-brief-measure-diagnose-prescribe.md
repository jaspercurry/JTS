# Research brief: measure → diagnose → prescribe for loudspeaker correction

> Written 2026-07-29 by the round-3 architect session, at the owner's request,
> after the 2 kHz crossover-notch discussion. Purpose: a deep-research
> "dissertation" pass over prior art, feeding the design of an
> attribution/diagnosis stage for JTS (anchor issue: see the
> "attribution stage" issue filed 2026-07-29). The consumer of this
> research is a design doc, not code — extract *what to copy, what to
> skip, and why* for each finding.

## Context (one paragraph for the researcher)

JTS is a DIY smart speaker whose correction stack measures with a phone/USB
mic (multi-position "cloud" sweeps, gated, SNR-solved drive levels), fits
per-driver linearization + crossover (LR4), predicts, auto-applies, and
verifies. Its *instrument* diagnostics are strong (measurement validity,
SNR, glitch forensics, uncorrectable-band exclusion with implied path-delay
τ). What it lacks is *mechanism attribution*: naming which physics owns a
response feature (inter-driver time offset vs source-riding reflection comb
vs unfitted-overlap slope error vs measurement-frame artifact vs
nonlinearity), with evidence and confidence, BEFORE prescribing — because
each mechanism has a different fix class (delay, polarity, EQ, physical
change, document-as-physics, or measure-differently). Motivating case: a
−4 dB notch at the 2 kHz crossover plus comb nulls at 8.4/11.4/14.8 kHz
with pipeline-computed τ ≈ 0.30 ms (~10 cm), on a CD-horn two-way.

## Q1 — Diagnosis-first architectures in shipped systems

How do the serious systems structure the measure→diagnose→prescribe chain,
and what do they *name* as findings before acting?

- **Trinnov Optimizer**: what does it detect and report as named findings
  (polarity inversion, per-driver delay, level, crossover anomalies)? What
  detection methods (cross-correlation, excess-phase)? What does it refuse
  to correct? How are findings surfaced to the user?
- **Genelec GLM / AutoCal + GRADE report**: the closest consumer analogue of
  "diagnose before prescribe" — which findings map to DSP fixes vs to
  *non-DSP recommendations* (move the speaker, add absorption, fix an
  early reflection)? Structure and language of the GRADE report.
- **Dirac Live**: mixed-phase correction scope; what it deliberately
  declines to correct and why.
- **Acourate / Audiolense**: the expert-workflow decomposition (time
  alignment as an explicit separate step; what the operator diagnoses
  manually between steps).
- **REW**: the alignment tool (sub/main delay from measured phase), the
  "EQ cannot fix this" guidance embedded in its workflows.
- **Sonos Trueplay / Apple HomePod room adaptation**: what they infer about
  the room/placement and what they adapt — the fully-automated,
  zero-user-skill end of the spectrum.

Extract per system: measured → named → prescribed → refused → how
uncertainty/confidence is communicated to a non-expert.

## Q2 — The discriminating-probe catalog (highest leverage)

Standard *small tests* that separate mechanisms, i.e. the "add energy and
see how it responds" toolbox. For each: what it discriminates, capture
cost, and phone-mic feasibility.

- **Reverse-null (polarity-flip) crossover test**: flip one driver's
  polarity, re-measure; null depth at Fc measures phase-tracking quality.
  The canonical crossover-alignment probe in speaker-building practice
  (VituixCAD-era workflows). What null depth indicates "aligned"?
- **Solo-driver captures** vs summed (per-way response + anchored relative
  arrival time).
- **Multi-level sweeps (level-invariance test)**: linear mechanisms
  (interference, response errors) are drive-invariant in dB; compression,
  port noise, suspension nonlinearity are not. Two-level protocol design;
  what deltas are significant.
- **ESS harmonic separation (Farina)**: harmonic-distortion orders
  extracted free from the exponential sweep we already run (harmonic IRs
  at negative delays). What diagnosis this yields (which driver is
  distorting, onset level) and its limits with phone mics.
- **Repeat-capture variance**: time-variance detection (HVAC, wind,
  traffic) vs stable features.
- **Nearfield vs farfield (Keele)**: separating driver behavior from room
  below the transition; splice validity limits.
- **Gated vs ungated / windowed comparisons**: direct vs reflected energy
  attribution; position-dependence of a null (source-riding vs room
  reflection) — the discriminator between "comb rides with the speaker"
  and "comb is the room."
- **Ground-plane measurement**: when it's worth the hassle.

## Q3 — Signature → mechanism → fix-class mapping

The literature/practitioner knowledge that turns response features into
named mechanisms:

- **Crossover-region suckout taxonomy**: inter-driver delay vs polarity
  error vs vertical lobing (off design axis) vs acoustic-slope error — how
  practitioners tell them apart from magnitude + phase (and with
  multi-position data). Expected signatures of each on an LR4 design.
- **Comb filtering**: source-riding reflections (horn internal
  throat/mouth reflection, cabinet edge diffraction) vs room reflections;
  CD-horn mouth-reflection literature specifically (our τ ≈ 0.30 ms ≈
  10 cm is suspiciously horn-depth-shaped).
- **SBIR / floor and desk bounce** for a tabletop speaker; which dips are
  placement, not speaker.
- **Baffle step** signatures vs level errors.
- **Port / passive-radiator resonances and chuffing** signatures.
- For each: the fix-class verdict (delay / polarity / EQ / physical /
  document-as-physics / measure-differently) and when EQ is actively
  harmful (burning headroom into interference nulls).

## Q4 — Frames: what should correction target, and what predicts preference

- **CTA-2034 / spinorama science (Toole, Olive)**: listening window vs
  early-reflections vs sound-power curves; which frame predicts listener
  preference; implications for a *desk/tabletop* speaker (strong desk
  bounce, near-ish field) rather than a stereo pair in a living room.
- **Power-response dip at crossover** between a beaming direct radiator
  and a CD horn: when a multi-position power mean shows a dip that the
  listening window doesn't (directly our #1859 frame question — one
  desk-point measurement vs 8-position cloud disagreeing up to 7.7 dB HF).
- **Low-point-count directivity estimation**: can an N-position phone
  cloud estimate enough of a DI proxy at crossover to diagnose
  lobing-vs-power-dip? Any prior art on sparse spatial sampling for this.
- **Target curves**: Harman-style tilt vs flat anechoic + room gain;
  what a "flat linearization layer + preference layer on top" stack should
  each target (we own the layering; the research question is the target
  per layer).

## Q5 — Automated diagnosis prior art

- **Klippel** as the paradigm: stimulus → fit physical model → identify
  *which parameter* is out of spec (LSI, rub & buzz, thermal compression).
  What of that paradigm transfers to in-room, phone-mic, consumer-grade
  diagnosis?
- Published **auto-alignment/auto-polarity detection** algorithms
  (how Trinnov-class detection is done: cross-correlation, excess-phase
  unwrapping, minimum-phase extraction); robustness and failure modes.
- AES/journal work on **automated loudspeaker defect detection** (ML or
  model-based); anything on automated *room-vs-speaker* attribution.
- Documented **failure modes of automated correction** (what Dirac/ARC/
  Audyssey-class systems get wrong and why) — the cautionary catalog.

## Q6 (stretch) — Communicating diagnosis to non-experts

- How shipped products phrase findings, confidence, and refusals
  (GLM GRADE's report structure is the model); any evidence on
  "prescribe only above confidence X, otherwise recommend the
  disambiguating test" policies; honest-refusal UX in adjacent domains.

## Deliverable format

Per question: findings with primary-source citations (manuals,
whitepapers, AES papers, patents fine), then the practical extraction —
**copy / skip / why** for JTS. Close with a synthesis: "the attribution
stage the prior art would build for JTS," including which 3–5 mechanisms
and which 2–3 discriminating probes deliver the most value first for a
phone-mic + conductor architecture that already has: µs-anchored captures,
per-driver solo measurement, multi-position clouds, SNR-solved drive
levels, a 1-entry short-session pattern, and an excluded-band/τ machinery.
