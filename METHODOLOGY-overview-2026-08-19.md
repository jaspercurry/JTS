# Speaker linearization — methodology overview

**Date:** 2026-08-19. **Scope:** a one-page-per-topic overview for the owner
and for research agents. The single source of truth for the pipeline design
is `docs/active-speaker-tuning-layers-design.md` (the linearization-pipeline
section, PR #2729); the two dated research briefs beside this file carry the
deep background. This file is the readable map.

## What we are doing

Make the speaker's direct sound flat across the trusted measurement band,
by measurement only — never by taste, never by a metric that grades itself
on a curve. Two tracks, both live, both driven through the same loop:
**narrow-band EQ** (fix minimum-phase response defects with filters as
narrow as the defects) and **crossover optimization** (compute the best
polarity / delay / crossover frequency / slopes from per-driver physics,
then confirm by measurement).

## The measurement

- Calibrated USB mic (UMIK-2) wired to the Pi; motorized turntable arm at
  constant radius; positions 0°, ±7°, ±22°, with settle time after moves.
- Exponential sine sweeps; impulse response by deconvolution; **7 ms
  reflection gate** (the room's first strong reflection), giving a trusted
  band of ~357 Hz – 16 kHz; 1/12-octave smoothing. Harmonic distortion
  falls out of the same sweeps (Novak method) and is banked.
- Session noise floor on the graded views ≈ 0.03–0.07 dB; anything claimed
  below that is noise, not signal.

## Time alignment — how it actually works

**No audible timing chirps are needed in the production path.** The
per-driver measurement program plays the woofer-only sweep and the
tweeter-only sweep **inside one continuous capture** (interleaved, routed
by WAV channel, DSP graph untouched). One capture = one clock = an *exact*
shared time origin for the two drivers, by construction. Relative
woofer-vs-tweeter phase — the quantity crossover optimization lives on —
comes out of sample positions, with in-capture clock drift estimated from
the sweep repeats.

What the chirp experiment (Stage-0, 2026-08-19) contributed instead:

- **It validated the timing chain**: after aligning on a shared event, our
  rig's residual timing scatter is ~1–7 µs (bar was 20 µs) — and a person
  moving around the room costs only 1.4–1.6×.
- **It discovered the silent-slip failure mode**: USB audio drops a packet
  (~7 samples ≈ 146 µs) in ~0.5% of captures with *no error reported*, and
  once dropped a ~2-sample slip (41 µs — twice the phase budget) passed
  every shipped check. The chirp's sub-sample cross-correlation estimator
  is now being built into the capture-integrity guard: a capture with a
  detected inter-segment slip is **rejected**, the same way captures with
  gaps or dropouts are rejected today.

So: alignment by construction; the chirp's *math* as the integrity guard;
audible chirps only ever again if we need cross-session absolute phase
(no current consumer needs it).

## Honest grading (the referee)

- **Frozen reference.** Every config is graded against the *baseline's*
  reference level, never its own — we measured that self-referenced grading
  flatters broadband cuts (it hid up to 6σ of damage).
- **Views.** On-axis pool (0°, ±7°) is the primary score; off-axis (±22°),
  log- and lin-pooled views alongside. Units: dB RMS deviation.
- **Prediction before play.** Every attempt banks a falsifiable prediction
  *in the grading view's units* before the speaker plays it. (Learned the
  hard way: three predictions in three currencies, only one scoreable.)
- **Statistical bar.** Accept only wins beyond 2–3× the session noise;
  anything inside the noise is a no-call, not a win.
- **Automatic rollback.** A loss reverts the config immediately. This fired
  three times on 2026-08-19 and restored perfectly each time. Being wrong
  costs one round, nothing more.

## Track 1 — narrow-band EQ

All nine measured features classified **minimum-phase** (excess-group-delay
test with synthetic positive/negative controls) — meaning EQ is the right
tool in principle. The prior EQ failures traced to *width*, not physics:
the filter Q ceiling (2.0) made every filter ~3× wider than its target
(measured natural widths: Q 3.9–6.6 in-window), so only 28–43% of each cut
landed on the feature and the skirts did net damage. The fix (PR #2730)
raises the ceiling **for cuts only** to the codebase's own pre-existing
cut ceiling (Q 8): a narrower cut has *tighter* skirts and cannot clip, so
the safety story strictly improves. Boosts keep the conservative ceiling
and remain gated behind their evidence bar until the boost route is built.

First experiment after merge: width-matched cuts at the two in-window
peaks, prediction banked, one round, keep-or-revert.

## Track 2 — crossover optimization

The literature's preferred lever for crossover-region behavior — and the
one never yet swept here. Four pieces:

1. **Per-driver data** — ships today (the interleaved capture above yields
   each driver's complex response). The per-angle schedule exists but is
   currently switched off; re-enabling it for this purpose is a small,
   explicit change.
2. **Integrity** — the sharpened slip guard (in build) so a silent USB
   glitch can never masquerade as a phase result.
3. **The forward model + search** — pure math, built as a proper tested
   module: predict the summed response at every angle for any candidate
   (polarity, per-branch delay, crossover frequency, slope/order) using
   the same filter math the DSP runs, scored with the *same* frozen-
   reference grading as the hardware rounds. Enumerate the discrete
   choices, optimize the continuous ones, offline — the search costs zero
   speaker time.
4. **The apply path** — a candidate crossover change must be applyable and
   *rolled back* with the same tested safety as EQ rounds before anything
   plays.

Then: play the top candidate, grade it, keep or revert. Repeat.

## The loop (LLM-driven, deterministically guarded)

The same harness drives both tracks: the system emits a structured
evidence packet (measured views, per-feature classifications, bounds,
history, noise floors); **an LLM proposes** — which lever, which
candidate, what prediction; **deterministic validators decide** — bounds,
safety, packet freshness, statistical acceptance, rollback. The LLM is
never trusted with safety, coefficients, or whether a win is real; the
validators are never asked to have ideas. The optimizer in Track 2 is a
tool the LLM invokes, not a second driver.

## Iteration economics

One measured round ≈ 25 minutes of speaker time. Baseline is banked.
Rollback is proven. The plan assumes several bites per track — each round
either improves the speaker or kills a hypothesis with evidence, and both
are progress. Five hypotheses died by pre-registered falsification on
2026-08-19 alone; that is the method working, not failing.

## Where the depth lives

- Pipeline design (SSOT): `docs/active-speaker-tuning-layers-design.md`
  (linearization-pipeline section, PR #2729).
- Prior-art context + findings: `RESEARCH-BRIEF-speaker-linearization-2026-08-19.md`
  (this branch; errata included).
- Timing validation deep-dive: `RESEARCH-BRIEF-self-referencing-timing-2026-08-19.md`
  (this branch) + `captures/timing-stage0-2026-08-19/RESULTS.md` (laptop).
- Campaign evidence: `captures/wired-night-2026-08-19/` (run log, charts,
  classification record).
