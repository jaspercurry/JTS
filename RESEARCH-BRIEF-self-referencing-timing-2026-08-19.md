# Research brief: the speaker's own second driver as the acoustic timing reference

**Audience:** a deep-research agent evaluating one specific proposal.
**Date:** 2026-08-19. **Companion to:**
`RESEARCH-BRIEF-speaker-linearization-2026-08-19.md` (same branch), whose
Part-1 program this proposal exists to unblock.

## 1. The problem, in one page

To optimize a two-way crossover we need **per-driver complex responses**
(woofer-only and tweeter-only, magnitude *and* phase, at each angle), and
the two sweeps must share a **common time origin** — otherwise the relative
phase `arg(H_w) − arg(H_t)` is corrupted and every complex-summation
prediction near the crossover is wrong. The standard engineering answer is
a dual-channel interface with an **electrical loopback** (one output wired
back to a second input on the same clock as the mic), which is why the
VituixCAD/ARTA school deprecates single-channel USB mics for speaker
engineering outright.

Our constraint: **JTS must not lock users into extra hardware.** The
product-realistic mic is a single-channel USB measurement mic (miniDSP
UMIK-2 here). A USB mic runs its own free-running ADC clock behind USB
buffering, so each capture starts at an unknown offset — assumed in prior
art to be on the order of ±0.1 ms between runs. At 2 kHz, 100 µs is 72° of
relative phase — fatal for crossover work. (We are measuring our rig's
actual jitter from banked repeated sweeps; the number will be appended
here when it lands. The claim to evaluate is the method, not the assumed
jitter.)

The known no-extra-hardware alternative — REW's **acoustic timing
reference** (a stationary speaker plays a timing chirp before each sweep)
— normally assumes a *separate* reference speaker, which is again hardware
we don't want to require.

## 2. The proposal: the DUT references itself

The device under test is an **active** speaker: we control each driver
independently in DSP. So use one driver as the stationary timing pilot for
sweeps of the other:

- **Capture A (woofer measurement):** tweeter plays a short pilot chirp →
  brief silence → woofer plays the ESS sweep. One continuous capture.
- **Capture B (tweeter measurement):** the same tweeter pilot → silence →
  tweeter plays the ESS sweep. Same capture structure.
- In both captures, the **pilot source is the same physical driver in the
  same physical position**, and the mic has not moved between A and B
  (protocol: capture the A/B pair back-to-back at each angle before the
  turntable moves). Aligning both captures on the pilot's arrival gives
  them a common time origin, cancelling USB start-offset jitter entirely.
- **Dual pilot (pre *and* post sweep) in every capture:** the pre→post
  pilot spacing is known in playback samples; its measured spacing in
  capture samples yields the playback-vs-capture **clock ratio** for that
  capture, correcting in-capture drift (20 ppm over a 2 s sweep is 40 µs —
  not negligible; the dual pilot measures it per capture rather than
  assuming it).

### The insight that reframes the accuracy question

The pilot's estimated arrival time does **not** need to be *accurate* —
only *repeatable*. Room multipath biases any arrival estimate, but the
pilot path (tweeter → mic at fixed positions) is **identical in captures A
and B**, so any systematic bias cancels in the A−B alignment. The error
budget is therefore only the **variance** of the arrival estimate
(noise, small thermal/level differences, estimator jitter) — not its
absolute correctness. Research should evaluate the proposal under this
framing; much of the literature's pessimism about in-room time-delay
estimation concerns absolute accuracy, which we don't need.

### Probable precedent (verify)

REW's acoustic timing reference is routinely used with the timing signal
played through **the same loudspeaker's other driver/way** — most commonly
"use the mains/tweeter as the timing reference while sweeping the
subwoofer" for sub/main time alignment. If confirmed, our proposal is not
novel: it is that documented workflow, held to a tighter accuracy bar
(crossover-grade instead of subwoofer-alignment-grade). Research task:
confirm this precedent, find any reported accuracy figures, and find any
reports of it failing.

## 3. What "good enough" means (quantified)

To predict crossover summation within ±0.5 dB near Fc (~1–2 kHz for this
speaker), relative driver phase should be good to ~10–15°:

```
15° at 2 kHz  →  (15/360) / 2000 Hz  ≈  21 µs
```

**Acceptance target: cross-capture alignment residual ≤ ~20 µs (3σ).**
Secondary requirement: per-capture clock-ratio estimate good enough that
residual in-sweep drift contributes ≪ 20 µs.

For context, time-delay-estimation theory (CRLB) with a 5–20 kHz pilot at
30+ dB direct-path SNR permits *sub-microsecond* estimates; the practical
limiter is room multipath inside the correlation window — mitigated by
gating the direct arrival (our room's first strong reflection is ≈7 ms
out) and, per §2, largely cancelled as a bias anyway.

## 4. Known weaknesses the research must probe

1. **Pilot SNR at the worst angle.** A tweeter pilot is directional; at
   ±22° (and any future vertical angles) the 5–20 kHz pilot is attenuated.
   How much variance does that add? Is a lower-band pilot (e.g. 2–8 kHz)
   or a longer pilot the fix?
2. **Repeatability of the pilot path itself.** Thermal drift in the
   tweeter (voice-coil heating between captures), level dependence of the
   estimator, DSP-path latency stability between the two branch
   configurations (muting the woofer branch must not change the tweeter
   branch's group delay — verifiable in DSP, but list it).
3. **In-capture clock drift model.** Is linear (single-ratio) correction
   from the dual pilot sufficient, or do USB mics exhibit short-term
   wander that a two-point estimate misses? Any published UMIK-1/UMIK-2
   clock-stability measurements?
4. **Capture-chain gain surprises.** Any AGC/limiting in the mic path
   corrupts the estimator (we already detect `agc_behavioral_fail`
   captures; the pilot design should be robust to small gain shifts —
   e.g., normalized cross-correlation / GCC-PHAT).
5. **The B-capture special case.** In capture B the pilot and the sweep
   come from the same driver — the pilot precedes the sweep. Any issue
   with driver state (excursion/thermal) differing between pilot-then-rest
   and pilot-then-sweep captures? (Expected negligible; confirm.)
6. **Room tail contamination.** Pilot reverberation decaying into the
   sweep start — sets the minimum pilot→sweep silence gap (HF RT in a
   normal room is short; 100–300 ms expected ample; confirm and give a
   rule).

## 5. Research questions

1. **Precedent & accuracy:** Confirm REW's acoustic timing reference can
   be (and commonly is) driven through the DUT's own second way; collect
   any reported accuracy/repeatability figures, internals (what signal it
   uses, its 5–20 kHz span, clock-correction behavior), and failure
   reports. Same for ARTA/CLIO equivalents.
2. **Estimator choice:** Best practice for sub-sample arrival estimation
   of a known chirp in a mildly reverberant room — matched filter vs
   GCC-PHAT vs gated cross-correlation; expected variance vs SNR and
   bandwidth; does the bias-cancellation framing (§2) hold exactly for
   these estimators?
3. **USB-mic clock behavior:** Published measurements of UMIK-1/UMIK-2 (or
   comparable USB mics) start-offset jitter and clock wander; does REW's
   documented UMIK handling (its stated sample-rate/clock adjustments)
   already solve part of this?
4. **Precedents beyond REW:** Production-line and self-test literature
   where a multi-way speaker times itself (Klippel QC, factory EOL
   systems, smart-speaker self-calibration — e.g., how Sonos/HomePod-class
   devices phase-align internal drivers using their own mics); anything
   from the smartphone acoustic-ranging literature on chirp arrival
   repeatability in rooms.
5. **Alternative no-hardware schemes to compare against** (same
   evaluation bar): (a) simultaneous disjoint-band dual sweeps (both
   drivers driven at once in non-overlapping bands — no cross-sweep
   alignment needed at all; what does the crossover-overlap region cost?);
   (b) interleaved short sweeps (rapidly alternating A/B segments inside
   one capture); (c) exploiting the always-present summed measurement as a
   phase constraint (solve `H_sum = H_w·C_w + H_t·C_t` for the unknown
   inter-sweep offset that makes separately-measured `H_w`, `H_t` sum to
   the measured `H_sum` — a self-consistency trick that may recover the
   offset without any pilot); (d) anything else the literature offers.
6. **The verdict question:** Under the ≤20 µs (3σ) bar, is the
   self-referencing pilot **good enough** for crossover-grade per-driver
   phase with a single-channel USB mic — yes, no, or yes-with-conditions?

## 6. Deliverable

A report that (a) answers §5 with citations; (b) gives a concrete
recommended protocol if viable — pilot signal design (band, length,
level), silence gaps, estimator, gating, dual-pilot drift correction, and
the expected residual in µs with its evidence; (c) states the conditions
under which it is NOT good enough and what the cheapest sufficient
fallback is (e.g., which ≤$150-class 2-in interfaces the community has
validated for loopback speaker work, as the escape hatch we'd rather not
need); (d) evaluates the §5.5 alternatives against the same bar, since
(c) of that list — the summed-measurement self-consistency constraint —
would, if sound, need no pilot at all.

## 7. Rig facts for the researcher

Two-way active speaker, DSP per-branch control (CamillaDSP on a Raspberry
Pi; branch muting is trivial and latency-stable by construction, to be
verified). Mic: miniDSP UMIK-2 (USB, single channel, own clock; calibrated).
Playback clock: the Pi's DAC chain, independent of the mic clock. Room:
~10 ft cube, first reflection ≈7 ms, measurements gated at 7 ms, trusted
band 357 Hz–16 kHz. Angles: turntable, 0°/±7°/±22° horizontal today
(vertical planned). Sweep: exponential sine sweep, a few seconds.
Measured cross-sweep timing jitter of this exact rig: **[measurement in
flight — to be appended; treat as TBD, not assumed]**. Acoustic crossover
region: ~0.8–3.3 kHz; the phase-accuracy bar in §3 derives from it.
