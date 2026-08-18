# Active speaker tuning — the layer model (design)

> **Status: adopted direction.** Owner-approved 2026-07-23 after the
> "no sparkle" investigation (issues #1666–#1668; forensic evidence in
> `captures/xover-e0-2026-07-21/OVERNIGHT-REPORT.md`, session-artifact).
> This doc is the execution handoff for the implementing session: the
> architecture is decided; per-phase design details are decided during
> implementation within these boundaries. Companion operational truth for
> today's shipped flow stays in
> [HANDOFF-crossover-measurement-v2.md](HANDOFF-crossover-measurement-v2.md).
> **Instrument update (2026-07-25):** the speaker layer's measurement
> instrument and the "top of the table" tolerances are concretized by
> [flat-linearization-plan.md](flat-linearization-plan.md) (adopted): the
> single-point gated sweep becomes a spatially-averaged gated capture
> cloud with declared per-band tolerances; that plan wins on instrument
> and spec details, this doc stays canonical for the layer architecture.
> **Commissioning revision (2026-08-04; not shipped):**
> [crossover-linearization-80-20-plan.md](crossover-linearization-80-20-plan.md)
> now owns the next protected-raw measurement, bounded-Fc selection, and
> candidate-specific verification campaign. The layer ownership below is
> unchanged.
> **Series-1 rulings (2026-08-17):** five owner rulings after the jts3
> overnight convergence series add decisions 8–12 below and one new section,
> ["The region-based adjustment contract"](#the-region-based-adjustment-contract-2026-08-17).
> The five-layer ownership is unchanged. What changes: a driver's low limit
> gets exactly one declared owner, and correction inside the crossover blend
> region moves from the (blind) per-driver fit to the summed response.
> **Capture-source ruling (2026-08-17, later the same day):** a sixth
> ruling adds decision 13 — the commissioning flow gets two first-class
> capture sources, a microphone plugged into the Pi beside the existing relay
> flow. Layer ownership is again unchanged; the seam is anchored by #2662.
> **Boundary ruling (2026-08-17):** a corner exactly at the declared limit is
> legal — the recommended crossover is a sanctioned operating point, no nanny
> margin.
> **Measurement Program v2 (ratified 2026-08-18, not built):** a seventh
> ruling adds decision 14 and one new section,
> ["Measurement Program v2 — the capture schedule"](#measurement-program-v2--the-capture-schedule-ratified-2026-08-18).
> The five-layer ownership is unchanged again. What it rules on is the
> *instrument's schedule* — what is played, at which angles, how often, and in
> which unit — not who owns which correction. **Nothing in that section
> describes shipped behavior**; the shipped flow stays
> [HANDOFF-crossover-measurement-v2.md](HANDOFF-crossover-measurement-v2.md).

## Why this exists (one paragraph of history)

The v2 crossover flow tunes trims/delay/polarity and verifies crossover
integration — and nothing else. On a compression-driver horn that left the
top octaves uncompensated (measured raw, analysis-bypassed, 58 dB SNR:
≈−4.6 dB in the 8–16 kHz octave re 2–4 kHz, −8.8 dB @12 k on JTS3), which
the owner heard immediately as "missing sparkle" while every VERIFY passed —
because VERIFY's band caps at 2·Fc AND it is a tracking metric against a
prediction that shares the rolloff. The gap is a missing, nameable layer:
**driver linearization**. Two secondary findings ride along: the trim solve
band-averages inside the woofer's rolloff skirt (#1667, estimated ≈3.4 dB
horn over-attenuation at the time — PR-L3's 2026-07-27 offline replay of the
archived JTS3 captures measured the real figure at **10.9–13.1 dB** and fixed
the frame at its source; see HANDOFF-crossover-measurement-v2.md), and the
apply transaction can activate without durably promoting (#1666).

## The five layers

**The order below is the commissioning order — what must be measured and
applied before what.** Each layer is its own artifact with its own owner,
measurement instrument, and re-run cadence. One fact, one owner — shape
never hides inside a level knob, level never hides inside a shape. All five
compose into one CamillaDSP graph, but the *signal* order inside that graph
is different — room and preference ride the stereo bus ahead of the split
mixer, so they are emitted before everything numbered 1a–2. Do not read
these numbers as filter order; the graph shape is in the
["Layer Boundary"](HANDOFF-active-speaker-dsp.md#layer-boundary)
section of HANDOFF-active-speaker-dsp.md.

| # | Layer | Job | Instrument | Re-runs when |
|---|---|---|---|---|
| 1a | **Driver linearization** | each driver flat *within its own band* on the design axis (CD-horn compensation, baffle step, breakup) | gated/quasi-anechoic sweep at the listening axis (already captured to 18 kHz every MEASURE); optional near-field supplement for the woofer below the gate validity floor | hardware changes (driver, horn, pad) |
| 1b | **Crossover integration** | drivers sum correctly: crossover filters, **scalar** trim per driver, relative delay, polarity | same gated session as 1a | hardware/geometry changes |
| 2 | **Bass** | extension/sub integration below the gated validity floor | near-field (extension); in-room, ungated (sub integration) | hardware/placement |
| 3 | **Room correction** | what the room does: modal peaks below the transition (~300–500 Hz here), at most a gentle broadband tilt above | in-room at the listening position | placement/room changes |
| 4 | **Preference** | declared taste on top of honest-flat | the household's ears | whenever |

Layers 1a+1b together are **the speaker layer**: they make the *device*
measure flat in direct sound, like a factory-tuned active monitor, and they
travel with the speaker. Layer 3 belongs to a room+position. Keeping that
boundary is load-bearing: 1a/1b are measured gated (reflections excluded);
3 is measured in-room; conflating them EQs directivity artifacts and ruins
off-axis sound. Room correction may *lightly* touch speaker-response
residuals only for speaker classes that have no Layer 1 (passive — #1671),
inside its conservative-above-transition philosophy.

**The "top of the table" contract (the owner's flatness vision), stated
precisely:** after Layers 1a+1b, the gated direct-sound magnitude on the
design axis is flat within a declared tolerance from the measurement
validity floor (≈143–200 Hz in the JTS3 room; set by the reflection-gate
window) up to ≈16 kHz. Below the floor, flatness is Layers 2–3's contract
with in-room instruments. Preference (4) then deviates deliberately and
visibly.

## Decisions already made (do not re-litigate)

1. **Linearization lives in the crossover program** (same wizard surface,
   same gated instrument, one commissioning session) — the surface gets a
   more honest name, "Active speaker" tuning (#1670). It produces a
   *separate artifact* from the trim: per-driver EQ curves.
2. **The trim stays a scalar** level anchor. Frequency-dependent balance is
   linearization's job. Corollary: implement 1a first — flattened branches
   structurally defuse most of #1667's band-average bias; the
   ripple-optimal trim fix lands after, as robustness for un-linearized
   tiers.
3. **Verification splits into two named claims.** Integration-verify (the
   existing 1–4 kHz tracking gate: "the correction realized the predicted
   summation") and a NEW **flatness-verify** ("gated response within
   tolerance from validity floor to 16 kHz"). Envelope/report copy must
   never let one imply the other — that conflation is how this gap stayed
   invisible.
4. **Safety posture unchanged:** per-driver linearization gains may be
   positive, up to `MAX_LINEARIZATION_BOOST_DB` (12 dB) per filter —
   refused rather than clamped above that cap, and absorbed by
   `active_baseline_headroom` (PR-L5); an HF shelf is emitted as attenuation
   elsewhere + headroom accounting, never a positive ceiling raise; the
   two-invariant protection model and declared-sensitivity ceilings stand
   (#1665 adds pad/component declarations so effective sensitivities track
   reality — the L-pad lesson).
5. **Simple-first execution:** everything proves out on JTS3 with the
   UMIK-2 over the headless direct-Pi drive path (no capture relay). The
   relay/phone/product UX hardening comes after the acoustics are right —
   same pattern that worked for the measurement campaign.
6. **The correction envelope replaces every fixed fit-ceiling number**
   (see Layer 1a section). Session enforcement rides with it: 0°
   orientation confirmed + on-axis aim for fit-eligible sessions (the
   90°-file-loaded case hard-fails), sweep upper edge decoupled from the
   declared driver band. The declared-band invariant is refined
   asymmetrically: the LOWER edge + proven high-pass stay absolute
   (excursion protection); the UPPER edge is not a protection boundary —
   low-level ultrasonic sweep content has no damage mechanism, and the
   sweep needs headroom past the analysis band.
7. **Sources:** the three verbatim research artifacts live in
   [`research/2026-07-23-driver-linearization/`](research/2026-07-23-driver-linearization/README.md);
   this doc is the adopted synthesis and wins where they disagree. The
   fact-check (artifact 03) also validates the post-amp L-pad as textbook
   gain structure (feed #1665: predict the hiss reduction from the
   declared pad) and assesses the closed-loop + multi-level + excess-phase
   combination as largely novel among shipping DIY tools.

**The five rulings below were ratified by the owner on 2026-08-17**, the
morning after the series-1 convergence run on jts3.

8. **A driver's low limit has exactly ONE declared owner (2026-08-17).**
   The bottom allowed frequency for a driver *is* the manufacturer's minimum
   recommended crossover frequency, carrying whatever slope condition the
   manufacturer attaches to it, entered ONCE through the operator's
   driver-research response at component entry (#1665). One field, one
   owner. Every consumer **derives** from it: the linearization fit band, the
   protection posture, the Fc sweep bounds, and the grading bands. The jts3
   preset as it stood for the series-1 run violates this —
   `tweeter.protection_highpass_floor_hz: 2000.0` while the Fc sweep consumed
   `tweeter.band.lower_hz: 1600`, two declared values for one driver's low
   limit, with the shipped 1648.7 Hz corner legal under one and illegal under
   the other (#2603, which also records that this one constant shapes both
   the safety posture and the integration-claim grading band — which is why
   it gets one owner rather than a reconciliation rule).
   That both values originate in a single research-response ingestion is the
   owner's reading on 2026-08-17, not something #2603 establishes. This
   ruling decides which way the collapse goes; #2603 owns doing it.

   **The manufacturer's figure, checked (2026-08-17):** B&C publishes the
   DE250's *Recommended Crossover* as **1.6 kHz**, footnoted **"12 dB/oct. or
   higher slope high-pass filter"**, alongside a 1.0–18.0 kHz frequency range
   ([bcspeakers.com DE250](https://www.bcspeakers.com/en/products/hf-driver/1/8/DE250)).
   The owner's attested 1600 Hz is therefore **confirmed as the published
   fact**, and **2000 Hz is not a published figure for this driver** — no
   source checked cites it. Provenance: the number reads off B&C's own live
   product page, and the slope footnote reads cleanly from B&C's own
   print-PDF endpoint (`/en/products/hf-driver/1-0/8/de250.pdf`, a genuine
   text layer); the Parts Express mirror of the same sheet agrees.
   Corroborated at 1.6 kHz by US Speaker's reseller listing; no contradicting
   figure found anywhere. Note the shape of the fact: the number is
   meaningless without its slope condition, which is why the field carries
   both.
9. **A research prompt asks for published facts; margins are computed
   downstream (2026-08-17).** The upstream driver-research prompt produced
   the 2000 Hz figure and presented it as datasheet data. The rule: a
   research prompt asks the manufacturer's published facts — minimum
   crossover frequency and its slope condition — and nothing else. A derived
   safety margin is computed downstream by named code with a named rationale,
   and is never smuggled into a datasheet field as though the manufacturer
   had published it. The prompt fix itself rides #2603; this entry is only
   the rule it implements.

   **How the fact is actually published, so the prompt asks for the right
   field (researched 2026-08-17).** Horn/compression-driver makers give it a
   dedicated spec-sheet line, as a single frequency. Three of the five horn
   makers checked print the exact phrase "Recommended Crossover" (B&C, BMS,
   18 Sound); the other two use "Minimum Crossover Frequency" or
   "Recommended min. crossover" (FaitalPro, Celestion). The slope condition
   usually rides along in a numbered footnote: B&C and 18 Sound print the
   *identical* sentence, "12 dB/oct. or higher slope high-pass filter."
   (verbatim; 18 Sound repeats it on the ND1460, so it is house style), and
   FaitalPro uses the same template. Celestion folds it into the field name
   instead. It is not universal — BMS's 4590 field carries no slope
   qualifier at all. **Dome tweeters usually have no such line whatsoever.**
   SB Acoustics and Scan-Speak instead express it as the test condition
   footnoted to the *power-handling* rating ("IEC 268-5, high-pass
   Butterworth, 2600 Hz, 12 dB/oct."; "X-over: 2. order HP Butterworth,
   2.5 kHz"), phrased as a filter order rather than a dB/octave figure.
   Consequences for the prompt: ask for the number **and** a separately
   reported slope/filter-order qualifier; do not key on the literal phrase
   "recommended crossover"; and for a dome tweeter, look in the
   power-handling footnote. Absent is a legitimate answer — a driver whose
   maker publishes nothing must report absent, never a guess.
10. **The crossover blend region is summed-response-owned correction
    territory (2026-08-17).** Per-driver fitting is deliberately blind
    across the blend, and stays that way — that honesty is correct and is
    not the defect. What moves is who is allowed to act there. Full
    contract, with the series-1 evidence:
    ["The region-based adjustment contract"](#the-region-based-adjustment-contract-2026-08-17).
11. **The contract is prescriber-agnostic; the harness is deterministic
    forever (2026-08-17).** The region vocabulary and its bounds are defined
    independently of who prescribes. Measurement, safety clamps,
    keep/restore on measured evidence, and receipts carrying
    commanded-vs-realized per band never become a model's judgement call.
    The prescription *policy* is a pluggable seam, and which policy fills it
    is an OPEN decision — see
    ["The prescriber seam"](#the-prescriber-seam-open-decision).
12. **Near-term mission, and its sequence (2026-08-17).** The bare minimum
    before any prescriber work: the drivers linearized across the ENTIRE
    spectrum *including* the crossover region, on corrected upstream data.
    In order: **(i)** upstream truth — decisions 8 and 9 land, so the fit
    band and the protection posture derive from one honest number; **(ii)**
    the blend-region contract — decision 10; **(iii)** a hardware series that
    proves the dip moves. Only after (iii) is prescriber policy worth
    deciding.

**The ruling below was ratified later the same day.**

13. **Two capture sources, both first-class (2026-08-17).** The commissioning
    flow supports a microphone plugged directly into the Pi — the Pi plays and
    records on one clock, which removes the relay/upload/cross-device-desync
    class structurally rather than diagnosing it — *and* the existing
    web/phone relay flow, kept with its known unreliability accepted and
    disclosed. Where a local mic is detected the flow may recommend it:
    disclose-and-recommend, never nanny. The shape is a **capture-source
    seam** under the existing tier/capture-plan machinery — the conductor asks
    for a capture of program X at position Y, a provider answers with WAV plus
    metadata — so the relay choreography becomes the relay provider's private
    internals. Named consequence: this is the designated slimming path for
    [`jasper/web/correction_crossover_v2.py`](../jasper/web/correction_crossover_v2.py),
    the risk site named in
    `captures/xover-series1-2026-08-17/wheels-report.md` (session-artifact) —
    the host sheds relay interleaving and trends toward transport-only, by
    strangler rather than rewrite; the wheels report's recommended file-size
    ratchet (G2, unimplemented) is the intended interim guard. Sequenced after
    decision 12's hardware series (iii), with a pull-forward trigger if
    lateral aborts recur during it. #2662 is the anchor and owns the HOW.
    Companion ruling: `e0_capture.py` and the driver tools promote into
    `experiments/` — tracked, usable, explicitly experimental, deprecated only
    by the owner — while the browser flow stays first-class for human drivers
    (#2636).

**The ruling below was ratified on 2026-08-18, and is not built.**

14. **A measurement is (stimulus regime) × (angle) × (level); angle is an
    ATTRIBUTE, not an identity (2026-08-18).** The schedule the flow grew —
    a CHECK, a MEASURE, a lateral walk, a cloud — named captures after the
    *role of a position*, which is how an angle became four different kinds of
    measurement instead of one measurement's tag. Under this ruling every
    capture is banked as `{regime, angle_deg, repeats, level_re_anchor}`, two
    stimulus regimes cover the whole grid, and the session walks it
    position-major. The unit is degrees everywhere, for a measurement reason
    rather than a convenience one. Full contract, its evidence, and its
    sequencing:
    ["Measurement Program v2 — the capture schedule"](#measurement-program-v2--the-capture-schedule-ratified-2026-08-18).

## Layer 1a concretely — UX and data flow

> **Amendment (flat-linearization PR-3b, 2026-07-26).** "The household UX
> does not change" was true of LINEARIZATION, which is what this section is
> about — it still adds no flow, no wizard, and no sweep of its own. It is
> no longer true of the commissioning flow as a whole: a separate change,
> the spatial cloud, took it from ~3 captures at one position to 16 across
> a guided walk (the "one tap" counts below moved with it). Read the claims
> here as scoped to Layer 1a's own footprint; the flow's shape lives in
> [`HANDOFF-crossover-measurement-v2.md`](HANDOFF-crossover-measurement-v2.md).

**The household UX does not change.** One commissioning flow, the same
phone-tap ethos (set the mic, press Go, ~3 captures, fast honest verdicts):
CHECK → MEASURE → auto-apply → VERIFY, exactly as today. Linearization is
not a separate flow, a second wizard, or an extra sweep — it is a new
consumer of data every session already records:

1. **MEASURE (one tap, richer capture + richer analysis).** The per-driver
   gated sweeps grow two composition changes (still ONE phone tap, ~30–60 s
   longer): each driver's sweep repeats **N≥3 times at the identical
   position** (the σ(f) repeatability input — today's single repeat already
   exists for drift), and every sweep runs **past the analysis band**
   (~22–24 kHz at 48 k fs with proper fades) so deconvolution edge
   artifacts fall outside anything analyzed — the current 18 kHz sweep end
   parks fade artifacts inside the band. The analysis then fits each
   driver's linearization curve under the **correction envelope** (below),
   **then** computes integration (trim/delay/polarity) against the
   LINEARIZED branch responses — the ordering that structurally defuses
   #1667. Linearization *aims* to extend ~an octave past Fc so acoustic
   slopes approach textbook LR, but only opportunistically within the
   envelope and boost caps — for a driver rolling off AT Fc (the JTS3
   Epique), full through-region flattening is unreachable within safe
   boost, and **empirical integration on the actual responses remains the
   backstop**; textbook slopes are never assumed. The candidate grows a
   `linearization` member; re-runs refit atomically; profiles without the
   artifact stay valid (absent = no stage emitted).
2. **APPLY (one more emitted stage).** The baseline emission gains one
   per-role linearization filter stage, same transaction, same safety
   posture (non-positive gains + headroom accounting).
3. **VERIFY (same capture, more claims — three honesty levels).** *Fit* to
   the envelope, *verify* roughly an octave above the fit band's top, and
   *observe/report* to 20 kHz — the top octave appears in the technical
   disclosure as the driver's measured natural response, never as a
   pass/fail. Verification itself splits by target: **gated per-driver and
   summed checks verify against FLAT; in-room (Layer 3) verification uses a
   downward-sloping target (~1 dB/oct Harman-class, directivity-aware,
   user-adjustable)** — the target is an explicit parameter of the verify
   function, never a shared default (research artifact 03, claim K: an
   in-room check against flat over-brightens every result). Closed-loop
   linearization verification (achieved-vs-predicted per band, back off
   divergent bands, ≤2 iterations) rides the same auto-apply→re-measure
   machinery and is the mechanism that turns every contested modeling
   question into a per-session empirical test.

**The correction envelope (adopted 2026-07-23 after research round 2 —
this supersedes any fixed fit-ceiling number earlier in this doc).** No
hardcoded ceiling. Per driver, per session, per frequency bin:

```
allowed_depth(f) = min(
    mic_trust_limit(f, tier),      # prior: artifact 01's metrology table
    repeatability_limit(f, σ(f)),  # measured across the N in-capture repeats
    linearity_limit(f),            # two-level test (extends existing pilots)
    invertibility_limit(f),        # excess-phase ADVISORY — build last
    class_prior_limit(f, class)    # artifact 02 §5 driver-class table
)
```

**Two further terms join that `min` when — and only when — the session has
a spatial capture cloud** (flat-linearization productization plan, PR-6a;
that plan's interpretation call (A)). `spatial_exclusion_limit(f)` zeroes
allowed depth on the merged honesty mask (the combiner's power-vs-median
screen ∪ the identified-null registry), which is how the plan's "no EQ of
interference-flagged bins, ever" reaches the fit; `position_stability_limit(f)`
shrinks allowed depth where the cloud's cross-position band levels disagree,
reading the standard error `σ_band/√N` through the *same* σ→depth mapping and
per-tier tolerance table `repeatability_limit` uses. Both are optional, both
can only narrow, and a per-driver session with no cloud composes exactly the
five-term envelope above. Their two reason codes
(`LIMITED_BY_SPATIAL_EXCLUSION`, `LIMITED_BY_POSITION_STABILITY`) join the
same closed vocabulary. The exclusion is applied *after* the smoothing pass
rather than inside it, so masking a null cannot bleed correction depth out of
the correctable response beside it — see `compose_envelope`'s docstring in
[`jasper/active_speaker/linearization_envelope.py`](../jasper/active_speaker/linearization_envelope.py)
for the measured counterfactual.

Correction is clamped to the envelope, which tapers smoothly (no cliffs).
Cold-start priors: artifact 01's per-tier table (reference: full correction
to 8 kHz, taper to zero by 16 k; consumer: 6 k/12 k; phone: 3 k/8 k) and
artifact 02 §5's driver-class rows. **Evidence can EARN depth beyond the
priors** (clean measured excess phase + closed-loop verification passing —
artifact 03's softened boost stance), **but never beyond what the
measurement chain resolves: the repeatability and mic-trust terms always
bind.** No class-table row is permission. Fitting policy: cut-preferred /
normalize-downward (spend the sensitivity headroom — this IS the existing
non-positive-gain posture), boost caps per artifact 02 §6 (global +6 max,
Q≤2 boosts, 0 above the envelope, 0 near horn cutoff / into flagged
nonlinear or excess-phase bands), cuts generous (−12 dB, Q≤8), smoothing
widening with frequency (1/6 oct to 4 k → 1/3 oct to 10 k → 1/2–1 oct
above), fit-against-smoothed / verify-against-less-smoothed. Every band
emits a reason code (`FITTED`, `LIMITED_BY_REPEATABILITY`, …) — the same
honesty-guard culture as the acceptance gates, per frequency. Build order
(artifact 02 §15): repeatability gate → closed-loop verify →
cut-preferred fitting + caps → tier/class priors → multi-level linearity →
excess-phase advisory → UX reason codes. Steps 1–3 alone beat any
hardcoded ceiling.

**Consistency without extra user steps:** σ(f) comes from the in-capture
repeats — no extra taps. Before choosing the fit form, Phase 2 quantifies
fit-to-fit variance OFFLINE against the 2026-07-22/23 corpus (15+ archived
measure captures) and seeds the σ thresholds (starting points: 0.5/1.0/1.5
dB by tier). The woofer's low edge honestly stops at the gate validity
floor (~150–200 Hz here); below that is Layers 2–3 by contract. Fix-4
(#1654) composes naturally but is not a prerequisite. Do NOT average
across mic positions for linearization — position-averaging is Layer-3
practice and smears genuine on-axis HF detail here (artifact 01 Q2).

**Measurement instrument, in one paragraph (details are canonical in the
HANDOFF's invariants):** the gated far-field sweep at the listening axis is
the Layer-1 instrument. The analysis finds the direct arrival per driver,
windows the IR before the first strong reflection (adaptive per capture —
~7 ms on the JTS3 rig, i.e. a ~143 Hz validity floor via `f_valid = 1/T`),
and claims nothing below the floor; VERIFY refuses comparison when its own
gate is forced shorter than MEASURE's. Near-field is a supplement, never
the instrument: valid only where the driver is acoustically small, so it
may extend the WOOFER's linearization below the floor via the classic
near-field-splice — while integration and horn linearization must stay
far-field (near-field destroys inter-driver geometry, and a horn's response
does not exist at its mouth). Below the near-field splice's own limits,
bass and room layers own the problem with in-room instruments.

### CD-horn compensation — the top-octave HF stage (#1668)

The tweeter-on-a-horn measures a falling top octave (calibrated deficit
≈ −2.5 dB @10k, −5.8 @12k, −8.6 @14k, −11.5 @16k relative to its trusted
4–8 kHz band). The 4-lens review panel ruled this is the **horn's
constant-directivity rolloff, not driver mass** — the compression driver is
flat to ~16 k on its datasheet horn — so it is a broad, real, EQ-able trend
**sized from measurement, not from the driver class.** The per-serial-calibrated
UMIK-2 protocol resolves that band to ±1.5 dB @12k / ±2.3 @16k, and the deficit
exceeds that 4–6×, so correcting the measured trend is objectively justified up
to a ~16 kHz confidence ceiling. **Owner ethos: no subjectivity** — sizing is
measurement; where measurement runs out, a declared-driver-type continuation
policy takes over, disclosed as such.

The stage (`_hf_continuation_stage`,
[jasper/active_speaker/linearization_fit.py](../jasper/active_speaker/linearization_fit.py))
runs AFTER the flattening peaking loop:

- **Confidence ceiling from mic trust.** The ceiling is the mic-trust term's
  taper-zero (~16.4 kHz reference tier); the knee is where its taper begins
  (~8.2 kHz). Eligible only when the fit band reaches the ceiling region
  (`fit_hi ≥ knee`) — woofers/mids fall out with no per-role branch.
- **Repeat-agreement gate (objective, replaces judgment).** Per-bin spread
  across the capture's repeats must stay under 1.0 dB below 10 kHz / 2.0 dB in
  [10 k, ceiling] (the measurement-uncertainty research), else the stage is
  suppressed (`repeat_disagreement`); <2 repeats → `insufficient_repeats`.
- **Class-blind sizing (measured inverse).** C(f) = max(0, target − working)
  over [onset, ceiling], rescaled to `spend = min(measured deficit, remaining
  budget)`. Identical for two hold-class drivers on the same curve — the
  mic-trust taper dominates the per-bin cap in the taper region, so the class
  never touches the sizing. `measured_deficit_at_ceiling_db` reports the
  UNCAPPED deficit so a budget-bound partial correction stays visible.
- **Cut-domain realization + give-back.** cut_target = C − spend (≤ 0
  everywhere) is realized with a Lowshelf backbone near the onset + peaking cuts
  in the TRUSTED band; the top octave gets no filter. Cutting everything below
  the compensation region by `spend` lets the flow's trim give-back level the
  branches back, raising the top octave RELATIVELY — the acoustic lift with
  cut-only (hardware-safe) filters. A fit-quality gate (realized vs cut_target,
  ±1.5 dB) suppresses a mis-shaped correction; its residual peaking fit uses a
  tighter flatness target than the flattening loop so `design_peq`'s RMS-based
  early stop cannot leave shelf-transition error unfitted while slots are free.
  The shelf's own gain is CLAMPED at the 12 dB per-filter cut cap (a hard
  per-filter invariant that the now-larger total budget may exceed); when spend
  is deeper, the peaking residual absorbs the remainder.
- **Plateau vs taper by declared type — the class's ONLY authority.** Above the
  ceiling nothing is measurable, so correction must not RISE.
  `HF_CONTINUATION_POLICY`: **hold** (compression horn, soft/beryllium/diamond
  dome, ribbon/AMT) keeps the lift constant; **taper** (metal dome, unknown)
  appends one trailing Highshelf CUT at ceiling×1.25 that walks the lift back
  down over the unseen band. Unknown → taper is the conservative default.
- **Budget 18 (6 → 12 → 18) — a max-SPL ledger.** `MAX_NORMALIZATION_SPEND_DB`
  is 18 dB so the spend can actually REACH the measured deficit: the live JTS3
  tweeter measured 14.2–14.3 dB at the ceiling but the 12 dB budget capped spend
  at ~9.2 on both quiet-room runs, leaving the treble sloping away. Total ledger
  = (plateau − target) + spend ≈ 17.3 on that rig. The spend drops the system's
  absolute ceiling by ~spend (ordinary listening recovers via the volume knob),
  it is NOT a listening-level cost, and it is disclosed. The literal-boost
  realization that reclaims the physical L-pad margin instead of spending
  sensitivity is deferred until the closed-loop verify layer (PR-E) can bound an
  unverified boost claim. At this budget the practical binding constraint for
  realistic horn shapes is the realization fit-quality gate, not the budget.
- **Single-shelf realization ceiling (measured).** The spend is bounded by three
  independent ceilings: the measured deficit, the remaining ledger budget, and
  `HF_SINGLE_SHELF_SPEND_CAP_DB` (11 dB) — what ONE Lowshelf plus bell residuals
  can actually realize on a real curve. The third is the binding one today.
  Measured live on JTS3 2026-07-24 by probing run-6's own capture offline
  through the real fit at a spend ladder: the realization passes the quality
  gate at spend 11.27 (4 filters) and fails from ~11.9 upward — the cliff sits
  just *below* the per-filter clamp, so raising the ledger budget alone buys no
  extra correction. Run 6 proved it the hard way: the raised budget sent spend to
  14.33 and the whole stage suppressed with `fit_quality`, delivering nothing.
  Capping at 11 yields a realized partial correction instead.

  This caps what one shelf can deliver, not what the driver needs. The same
  ladder: spend 11.27 → OBSERVE 12k −0.7 / 16k −2.7, versus spend 14.33 → 12k
  +0.9 / 16k −0.0. **The last ~3 dB toward true tabletop needs a different
  REALIZATION, not a bigger number** — either a stacked-shelf realization (two
  cascaded shelves sharing the depth; a contract extension, future PR) or the
  literal-boost realization once closed-loop verify (PR-E) can bound a boost
  claim. The realization gate itself was also relaxed 1.5 → 2.0 dB: it guards
  against mis-SHAPE, and real curves realize at ~1.3 dB even at moderate spend
  (runs 4/5), so isolated 1.5–2.0 dB excursions at the smoothing scale are curve
  raggedness rather than shape failure.
- **Anchored give-back (the trim).** Each branch's linearized trim is its own
  COMMITTED raw trim plus `LinearizationFit.correction_giveback_db` — the fit
  engine's SSOT: the **measured before-vs-after level delta** of that branch's
  reference (core) band (power-domain average of the pre-correction curve minus
  the same average of the corrected curve, over the `_core_or_fallback_mask`
  region, positive, computed for every fit with filters). Because the quantity
  added back *is* the measured level change of the band being restored, it is
  exact by definition — no flat-core assumption. (Averaging the *correction*
  alone would be power-domain-approximate instead: exact only for a flat core,
  and up to ~1.1 dB under-return on a 12 dB-tilted woofer-shaped core.) A shared
  shift then normalizes the pair non-positive so a branch whose give-back exceeds
  its raw attenuation can never become a boost; the shift preserves relative
  leveling exactly and is honest extra ledger.

  The realization gate bounds the correction's *shape* only over
  [onset, ceiling]; below-onset divergence (e.g. a clamped shelf the residual
  only partly absorbs) is absorbed by the anchor consuming the measured value —
  it shrinks the achieved lift, it never breaks the leveling.

  This replaced the `solve_branch_trims` overlap-band seed after the 2026-07-24
  JTS3 runs, where that seed returned only **5.81 dB of a 9.27 dB spend** (raw
  −22.21 → seed −16.396) and left the whole tweeter band ~3 dB low. Two reasons
  the overlap band is the wrong reference for a top-octave correction: the
  tweeter's LR4 skirt lives there, and `_band_average_db` is a POWER-domain mean
  that weights the loudest (least-cut) bins hardest — together dragging the
  average toward the region the shelf barely touches, where its wide RBJ
  transition is not at full depth either.
- **Guard.** The wild-trim guard in `crossover_v2.intervention.decide_trim`
  ([crossover_v2_flow.py](../jasper/active_speaker/crossover_v2_flow.py)) now
  measures the ripple scan's drift from that ANCHOR (±6 dB), and falls back to
  the anchored pair — never raw + emitted filters (the known VERIFY-mismatch
  class). The anchor is measured give-back, not a prediction, so only the scan
  can drift. Under the old seed the guard actively blocked the fix: the scan
  tried to push to −8.796 (+13.4) and was rejected at 7.6 > 6.0 on BOTH live
  runs, so the under-returning seed shipped twice. Magnitude protection lives in
  the fit engine's structural caps (per-filter 12, total budget, realization
  tolerance) plus the VERIFY gate.

Disclosure: octave centers above the ceiling report
`envelope_beyond_measurement_confidence`; beyond the ceiling the lift is
declared best-effort, never a measured claim.

## The region-based adjustment contract (2026-08-17)

**One paragraph of history.** Series 1 — four rounds on jts3 overnight
2026-08-17, reported in `captures/xover-series1-2026-08-17/series1-report.md`
(session-artifact) — asked whether the drivers converge toward flat. They did
not, and the machinery said so honestly: zero unsafe findings, zero rollbacks,
every round kept `for iteration` rather than claiming a win. The commanded
tweeter trim across the four rounds was **−7.2017, −7.1849, −6.9464, −7.3111
dB — a total spread of 0.365 dB**, the woofer was asked for `0.0` throughout,
and the peak deviation stayed parked at **1947 Hz** in every round after the
first, while the series ended on its own round cap with `headroom.status:
exhausted`. The trim layer converged on a prescription; the response did not
converge to flat. A **scalar level knob cannot fill a localized notch**, so
the honest reading is that the loop had no lever aimed at the thing it kept
measuring. Three mechanisms, each evidenced, say why:

- **The per-driver instrument is blind exactly there.** Three of the four
  candidates (r1/r2/r4) carry a woofer `blind_zone_placements` record over
  **1291.4–2077.2 Hz** — the gap between the woofer's core-band top and the
  tweeter's core-band bottom (#2600 item 4) — with the placement at 1857.4 Hz,
  acknowledging `measured_excess_db` of 2.09–2.26 dB and emitting only −0.51
  to −0.64 dB against it. That damping is the fit being careful on evidence
  it does not have, not a bug. `_blind_zone_placements` in
  [`jasper/active_speaker/linearization_fit.py`](../jasper/active_speaker/linearization_fit.py)
  already carries the full argument — including why it reports rather than
  refuses — and already names this section's conclusion: the honest separator
  "needs the SUM, which only the alignment/crossover layer sees."
- **No lever of the right shape reached the dip.** The tweeter fit asked for a
  lift of 0.738 / 0.988 / 0.730 dB in r1/r3/r4 and got
  `lift_suppressed_reason: no_realizable_boost` each time (r2 asked for none);
  that lift is the HF-continuation stage's, not a notch filler. A boost *did*
  realize — the woofer emitted a +2.38 to +2.68 dB Peaking filter in all four
  rounds — but at 422–434 Hz, more than two octaves below the dip. (That is a
  positive gain in the emitted graph: decision 4 above and the boost-path
  prose were trued up by roadmap item 6's sweep, #2603.) So with the
  tweeter's lift refused and the woofer's only
  realizable boost that far away, what was left near 1947 Hz were cuts —
  damped in the blind zone, per the bullet above — and the whole-driver scalar
  trim, which is the wrong shape for a notch.
- **The prescription never consumed the banked trend.** What accumulates feeds
  the STOP test, not the prescription. Rounds 1–2 were started with
  `--reset-first`, which clears run state, so `previous_objectives` was null
  and the plateau verdict carried no information; from round 3 the continued
  rounds did bank it, and it drove the plateau/headroom verdict. The trim,
  though, is absolute and re-derived from each round's own capture either way
  — so round N+1 measured the same baseline and proposed the same number
  again.

**The contract.** The frequency axis divides into regions with different
measurement trust and therefore different allowed tools. This is what ANY
prescriber consumes — the regions, the vocabulary, and the bounds are the
contract; the code that fills them in is not.

**(a) Inside each driver's own band, away from the crossover.** Per-driver
linearization under the correction envelope. Existing, unchanged — Layer 1a
above owns it.

**(b) The crossover/blend region — summed-response-owned.** Per-driver
fitting stays instrument-blind here by design. What changes is the owner: the
summed at-the-mark measurement *is* trusted in this region and sees the dip
at every position, so it owns **bounded shape correction** there. The initial
posture is **cuts-first**. Every existing safety cap is unchanged, the
verification is the same summed verify, and the outcome is banked in the same
receipts. This is a change of owner and of allowed tool — not a new safety
class, not a new instrument, and not a new flow.

**(c) Level, alignment, and Fc keep their own tools.** Level stays a scalar
per-driver trim. Alignment (delay/polarity) and Fc selection stay their own
tools with their own evidence. Nothing about the safety class changes.

**What this contract does not decide.** The HOW of (b) — filter form, band
edges, how much depth the summed evidence earns — is the implementing
session's, inside these boundaries. One named prerequisite: reading per-role
quantities out of a summed capture needs #2653's frame-coherence condition,
because the summed capture rides the applied incumbent graph while per-branch
sweeps ride the protected-neutral one.

### The prescriber seam (open decision)

The contract above is deliberately defined without saying who prescribes.

- **Deterministic forever:** the harness. Measurement, the safety clamps,
  keep/restore on measured evidence, and receipts carrying
  commanded-vs-realized per band. None of this becomes a model's judgement
  call, ever.
- **Pluggable:** the prescription *policy* — what to try next, given the
  banked trend.

Two candidates, recorded neutrally, with the decision **deliberately
deferred** until the upstream truth (decisions 8–9) and the contract
(decision 10) land and a hardware series proves the blend region is
correctable:

1. **A deterministic trend engine** — closes the third mechanism above by
   making the prescription read the round-over-round objectives the plateau
   test already banks.
2. **An LLM prescriber** —
   [`llm-native-tuning-workbench-plan.md`](llm-native-tuning-workbench-plan.md)
   is the planning authority for agent-assisted tuning and is where that
   option's shape lives.

The owner's stated concern is **complexity**. The observation that makes
deferring cheap: everything except the trend engine itself — the contract,
the vocabulary, the bounds, the harness, the receipts — is **common to both
paths**, so nothing built now is wasted by either choice.

## Measurement Program v2 — the capture schedule (ratified 2026-08-18)

> **Status: ratified design, NOT built.** Owner-ratified 2026-08-18. Every
> sentence below states what the ratified program *is*, not what the speaker
> does — no part of it is implemented. Today's shipped flow is
> [HANDOFF-crossover-measurement-v2.md](HANDOFF-crossover-measurement-v2.md),
> and the two disagree on purpose until this ships. Sequencing and what it
> supersedes are at the end of this section.

### The schema

A measurement is **(stimulus regime) × (angle) × (level)**. Angle is an
**attribute** of a capture, not an identity: every capture banks as
`{regime, angle_deg, repeats, level_re_anchor}`.

That is the whole ruling, and it is a vocabulary ruling before it is a
schedule. The shipped phase names — `check`, `measure`, `lateral`,
`cloud_measure`, `cloud_verify`, `entry_baseline`, `verify`
(`CAPTURE_PHASES` / `GROUP_PHASES` in
[`jasper/active_speaker/crossover_v2/journey.py`](../jasper/active_speaker/crossover_v2/journey.py))
— name a *position's role* rather than what is played, which is how one angle
became several kinds of measurement instead of one measurement's tag. Those
names are **superseded as a design frame** and survive as implementation
internals until the capture-plan rebuild lands.

### Two stimulus regimes, and the session's one-timers

- **D — per-driver.** Each driver swept alone. **×3 repeats at 0°, ×1 at every
  other angle.** Yields that angle's per-driver transfer function, phase,
  distortion, and drift.
- **S — summed.** One sweep through the applied (or candidate) graph, **×1 at
  every angle.** Yields the system response at that angle — the before/after
  evidence.

Three things are **session-level one-timers at 0°**, not per-capture work:

1. the room-noise listen and the chain-gain tones;
2. the **SPL anchor** — `RampController`
   ([`jasper/audio_measurement/ramp.py`](../jasper/audio_measurement/ramp.py))
   re-targeted to **~75–80 dB SPL at the microphone**, read through the
   calibration file's sensitivity, with an operator abort control. This rides
   the leveling redesign; it is not a separate build. Two prerequisites belong
   to that build and are named here so they are not discovered late: the ramp
   settles on a **dBFS window** today (`window_low_dbfs = -20.0` /
   `window_high_dbfs = -12.0` in `MeasurementRamp`), and the calibration reader
   ([`jasper/audio_measurement/calibration.py`](../jasper/audio_measurement/calibration.py))
   carries a frequency-shaped `correction_db` curve, **not** an absolute
   sensitivity — so the anchor's input has to be read before it can be
   targeted. The band itself is legal under the declared commissioning
   ceiling: `SafetyEnvelope` declares `initial_sweep_level_db_spl` (default
   65.0) and `max_commissioning_level_db_spl` (default 85.0), each validated
   into 45–85
   ([`jasper/active_speaker/profile.py`](../jasper/active_speaker/profile.py)),
   and the preset staging path writes both — while the ramp reads neither;
3. **~2 s per-driver drift sentinels between phases**, which replace the
   per-capture pilot on the wired path (see constraint 4 below).

### The position set, and the unit

**0°, ±7°, ±22°.** The numbers are tunable; the **structure** is what is
ratified — one center plus symmetric pairs. That is why an odd total is
correct rather than an off-by-one: 5 = 1 center + 2 mirrored pairs.

**Degrees everywhere**, for a measurement reason and not a convenience one:

- The lab arm speaks degrees natively, and the shipped flow already derives
  these exact bearings — `position_angle_deg` reads ±7° from 12 cm and ±22°
  from 40 cm at `MARK_DISTANCE_M = 1.0`
  ([`jasper/active_speaker/crossover_v2_flow.py`](../jasper/active_speaker/crossover_v2_flow.py)).
  The angles are not new; the canonical unit is.
- The household method is the owner's **string-and-protractor** technique: a
  string fixed under the speaker, cut to listening distance, swung to the
  protractor angle. Its virtue is geometric, not ergonomic — **a taut string
  is a constant-radius arc**, the same geometry the arm has, which removes the
  distance-change confound the lateral-cm prompts carry. At the shipped 1 m
  mark a 40 cm lateral slide puts the microphone 107.7 cm out: **≈0.64 dB** of
  pure inverse-square level change with no acoustics in it (12 cm ≈ 0.06 dB).
  Today that confound is addressed in prose — `_WIDE_LATERAL_DETAIL` asks the
  walker to "step a little toward the speaker as you go out", and
  `position_angle_deg`'s own docstring names the chord-versus-arc gap a
  hand-walked session settles for. A string makes it structural.
- The cm-based prompts retire with the old walk.

### The schedule — position-major (the microphone moves once per stop)

| stop | angle | what runs | budget |
|---|---|---|---|
| 1 | 0° | room-noise listen + chain-gain tones + SPL anchor | ~40 s |
| 2 | 0° | **D ×3 + S** — the deep reference | ~55 s |
| 3 | +7° | D + S | ~30 s |
| 4 | −7° | D + S | ~30 s |
| 5 | +22° | D + S | ~30 s |
| 6 | −22° | D + S | ~30 s |
| 7 | 0° (return) | short D probe — **the session's noise floor** | short |
| after apply | all five | **S at each angle** | — |

Two properties fall out of the grid rather than needing a measurement of their
own:

- **The session's own noise floor.** Stop 7 repeats stop 2's position, so
  anything that "moved" between them is measurement noise, not the speaker.
- **Moved-versus-fixed attribution.** A feature that tracks angle belongs to
  the speaker; one that stays put belongs to the room.

And post-apply verification happens **across angles**, not on-axis only.

**The budget claim, and exactly what it is.** ≈**6 min** of measurement,
against **13 min** for a Full journey today — that 13 is the flow's own
derived estimate (`tier_display_info()['full']`, 15 captures; re-derived at
HEAD 2026-08-18), and the session trims in flight take it to 12
([PR #2715](https://github.com/jaspercurry/JTS/pull/2715)). The ≈6 min is a
**design projection** from measured 2026-08-18 session evidence — that day's
per-capture census plus gate evidence, with the capture-relay per-capture
overhead (≈11.5 s) measured across the day's ten banked walk journals — not a
stopwatch on a built flow. When it is built, `CapturePlan.estimated_minutes()`
owns the number, exactly as it does today.

**What is genuinely new, stated against the shipped walk**, because most of
this grid already exists and the record should not overclaim. Stage 1's
lateral walk already replays the MEASURE program verbatim at 0°, ±7°, ±22°,
0° — six poses at 42.18 s each, and `lateral` is absent from
`SUMMED_SWEEP_PHASES`, so those poses are per-driver captures. **The D half of
the grid is substantially shipped.** What v2 adds is the **S half at every
angle** (today only the at-mark entry baseline is summed before apply), the
session-level one-timers, the explicit repeat and noise-floor structure, and a
schedule that stops paying per-capture overhead at every pose.

### Constraints carried forward

Four, each from evidence this section cites rather than restates:

1. **Off-axis pose data must not feed a selector statistic** until it clears
   the re-introduction bar set by the 2026-08-18 lateral-statistic redesign
   study: (i) candidate dependence enters through the *operator*, not through
   the band; (ii) the rank-1-versus-rank-2 gap exceeds same-arm repeat noise;
   (iii) band-edge neutrality; (iv) immunity to a zero-offset pose. Nothing
   computable from what is banked today clears it — every such statistic is
   exactly candidate-blind — and the **enabling change is banking
   `branch_operator_by_role` per candidate**
   ([#2711](https://github.com/jaspercurry/JTS/issues/2711), which holds the
   study's finding and the retention-guard caveat).
2. **Inter-driver phase read off these captures is contaminated** by per-role
   integer-sample alignment quantization (±20.833 µs at 48 kHz) —
   [#2710](https://github.com/jaspercurry/JTS/issues/2710). It is to be located
   before the D14 aligner root fix leans on measured inter-driver timing.
3. **D's repeats stay 3.** Three shipped policy floors read that structure, so
   the repeat count is not a schedule knob: the linearization eligibility gate
   (`ineligible_reason` → `ineligible_repeats`, against
   `LINEARIZATION_MIN_PAIRED_OCCURRENCES = 3`), the σ composition that feeds
   the envelope's repeatability term (`compose_sigma_db` returns *no evidence*
   below the floor — and absent σ is the tightest constraint, not the
   loosest), and the HF agreement gate (`_HF_MIN_OCCURRENCES = 3` →
   `insufficient_repeats`). `MEASURE_REPEAT_COUNT = 3` is the composer's
   matching default.
4. **The per-capture pilot cut is wired-path-only** and rides the leveling
   build, not this schedule. What it removes is the ~2.6 s behavioural-linearity
   pilot pair (plus its 1.0 s pre-pilot ambient window) that every MEASURE- and
   VERIFY-shaped capture opens on; what replaces it is the drift sentinel
   above. The relay path keeps its per-capture pilots, because on that path the
   pilot is also the evidence that the phone heard the speaker at all.

### Deferred axis — elevation (v2+)

Flagged by the owner on 2026-08-18 as a planned future extension, **not built
and not scheduled here**. Four things are worth recording now. The ratified
schema accommodates it without redesign: the angle attribute generalizes from
one bearing to (azimuth, elevation) with the same center-plus-symmetric-pairs
structure. The household method does **not** generalize as stated — a floor
protractor reads azimuth only; a candidate technique, noted and not committed,
is the same taut string swung upward (constant radius still holds) with a phone
laid along it as an inclinometer. The interim state is worth knowing plainly:
once the cloud verify walk sits at its floor of five positions
([PR #2715](https://github.com/jaspercurry/JTS/pull/2715)), the measurement
program samples **zero** vertical offsets — that PR's own disclosure names the
position it drops as the Full journey's only above/below-mark-height sample,
and owns the reasoning. And the lab arm's elevation capability is
**undetermined** (mast height is set by hand), so a v2 elevation build states
its rig support before it states a schedule.

### Sequencing, and what this supersedes when it ships

Ratified 2026-08-18; **not implemented**. It builds on decision 13's
capture-source seam ([#2662](https://github.com/jaspercurry/JTS/issues/2662);
slice 1 is [PR #2701](https://github.com/jaspercurry/JTS/pull/2701)) — **after**
the wired provider (W2b) and **together with** the SPL-anchor leveling build,
which shares its machinery. The wired-only ruling is the frame: speaker
calibration is wired-microphone only, while the relay/phone path survives for
room correction, a later rework where the string method serves seat-position
prompts too.

Interim steps already landed or in flight, each superseded by this schedule
when it ships:

- the cloud 6→5 trim and the courtesy-prelude grouping
  ([PR #2715](https://github.com/jaspercurry/JTS/pull/2715));
- the lateral walk's pause (in flight 2026-08-18);
- the walk's future 4-capture reporter form — subsumed by stops 3–6 plus the
  stop-7 return.

## Session operating model (how the implementing session runs)

Fable is the brains, not the hands: architect, coordinator, debugger, and
the owner's collaborator. Fable designs, decomposes, dispatches, interprets
evidence, holds the review gate, and talks to the owner — and **delegates
the doing**: implementation, replay/evidence-gathering, and drive-tooling
to **Sonnet-5 (high)** subagents; adversarial reviews (always) and any
unusually subtle DSP-adjacent core work to **Opus 4.8 (high)**. Token
discipline is a design constraint: Fable context is spent only where
Fable-level judgment is worth it — subagents carry the file-level work, and
their reports (not raw transcripts) come back to the coordinator. Every
merge passes the canonical adversarial review gate at 0 blockers / 0
should-fixes, rerun until clean; hardware claims are validated on JTS3
before merge when the change touches the acoustic path. The owner decides
taste, thresholds-by-ear, and anything physical at the rig.

## Composition & code seams (verified present)

The config emitter already composes in the right order and most seams
exist empty: `emit_active_speaker_baseline_config`
([jasper/active_speaker/camilla_yaml.py](../jasper/active_speaker/camilla_yaml.py))
emits per-role `[crossover, delay, baseline_gain, limiter]`; the
`/sound/` recomposition (`_recompose_active_baseline_with_eq` in
[jasper/sound/graph_carrier.py](../jasper/sound/graph_carrier.py)) already
threads `preference_filters` + `room_peqs` slots (audited live: currently
empty). Layer 1a adds a per-role linearization stage to the *baseline*
emission (owned by the speaker layer, NOT injected through the sound-profile
seam — different owner, different cadence). The measured tweeter/woofer TFs
that the fit consumes are already produced by every MEASURE
(`analyze_program_capture` → `DriverResponse` to 18 kHz with per-serial cal
applied) — the data pipeline needs zero new capture work for 2-way.

**The capture-source seam (decision 13, #2662 — slice 1 landed).** The
capture → analysis layer contract is: one provider per source answers each
capture with WAV + metadata (mic identity, mic/cal identity reference, and
the frame-ledger integrity counters), the provider mints the session id the
durable state and evidence key on, and the host owns the mapping onto the
persisted failure codes — the provider speaks only the flow's reason
vocabulary. The contract itself lives in
[jasper/active_speaker/crossover_v2/capture_source.py](../jasper/active_speaker/crossover_v2/capture_source.py)
(do not restate it here); the relay provider's private choreography is
[jasper/web/correction_crossover_v2_relay.py](../jasper/web/correction_crossover_v2_relay.py),
and the wired (Pi-mic) provider is the seam's next occupant.

## Speaker-class applicability (#1671)

Component entry (#1665) declares the class; the class drives which layers'
wizard steps exist. **Landed so far (2026-07-24, the component-entry
slice):** per-driver `driver_class` (compression_horn/soft_dome/metal_dome/
beryllium_diamond_dome/ribbon_amt/unknown — `DRIVER_CLASSES` in
[`jasper/active_speaker/_common.py`](../jasper/active_speaker/_common.py))
feeds the correction-envelope's `class_prior_limit` term, which takes the
declared class and nothing else; the declared in-line pad
(`jasper/active_speaker/driver_pad.py`) feeds the effective-sensitivity
readers (`declared_effective_driver_sensitivities`); `radiating_diameter_mm`
feeds #1675's simple-v1 ka-beaming crossover hint in `/sound/`; and
`horn_coverage_deg` is a declaration/display surface only until #1675's
Bessel beamwidth match lands. **Still open:** the
SPEAKER-level class this table's columns describe (2-way / 3-way / passive)
is not yet driven by a component-entry step — that routing is #1669/#1671's
job — and the research-prefill auto-populate + full consumed-value-audit
parts of #1665 (items 1–3 in the issue) are unstarted.

| Class | 1a | 1b | 2 | 3 | 4 |
|---|---|---|---|---|---|
| Active 2-way (today) | ✓ | ✓ | ✓ | ✓ | ✓ |
| Active 3-way (#1669) | ✓ ×3 | ✓ ×2 regions | ✓ | ✓ | ✓ |
| Passive | — | — | ✓ | ✓ (may absorb gentle speaker residuals) | ✓ |

## Microphone doctrine (with one open arbitration — #1672)

Both household mics carry **per-serial** calibrations (miniDSP by serial
…8494; Dayton resolves iMM-6C serials per-unit — CMM31555 verified live).
The distinction that matters is NOT per-serial-vs-generic; it is:

- **Pedigree/uncertainty above ~8 kHz** — the two calibrated readings of
  the same horn disagree by ~4.7 dB up top; at most one is right.
- **Incidence-angle sensitivity** — the iMM-6C's readings (delay family,
  HF tilt) shifted when it was physically handled; the deliberate re-aim
  test (owner's hands, 2 minutes) is the pending discriminator.
- **Empirical scatter** — quantified 2026-07-22/23: UMIK ≈0.3 µs
  repeatability vs iMM ≈8–12 µs; 25% honest-refusal rate on the iMM tier.

Rules until #1672 resolves: Layer-1a HF fitting requires the
reference-tier mic; consumer-tier mics remain integration-tier (1b) under
the shipped honesty gates. Future option with real leverage: the 17-run
same-sweep witness corpus supports deriving a unit-specific **transfer
calibration** for a consumer mic against the reference — a
"calibrate-your-cheap-mic-once" product story.

## Execution plan for the implementing session

Phase 0 — read this doc, the HANDOFF, and issues #1666–#1672; JTS3 +
UMIK-2 is the rig; headless drive tooling per
`captures/xover-e0-2026-07-21/drive-tooling/` (no relay).
Phase 1 — **#1666** (apply promotion durability + doctor divergence check):
small, isolated, protects everything after it.
Phase 2 — **#1668 Layer 1a for 2-way**: fit per-driver linearization from
the existing measured TFs (fit shape/order per-driver within its band;
tolerance + smoothing decided against the JTS3 curves), emit into the
baseline, add flatness-verify, wizard copy per #1670. Owner listening
session validates (ladder protocol from the overnight report), with the
ripple-optimal trim stop (−23.0) as the first rung.
Phase 3 — **#1667** trim solve fix (landed 2026-07-24: the applied trim is
now `solve_ripple_optimal_trim`'s minimum-ripple solve — seeded by and
sanity-bounded ±6 dB against the band-average value, wired into both the
raw candidate and the Layer 1a linearized re-solve; see
HANDOFF-crossover-measurement-v2.md's trim-solve section. The
linearized-path trim is correct only with the linearization filters
emitted (PR-D); the two land together. JTS3 hardware re-verify against
the listening ladder's ripple-optimal stop remains open) + re-verify.
**Superseded 2026-07-27 by PR-L3**: changing the OBJECTIVE was not enough —
the band itself was the defect. `solve_branch_trims` now reads each branch
on its own side of Fc, and the ripple polish runs only where its band
straddles Fc. Hardware re-verify still open, now against the corrected
frame.
Phase 4+ — #1669 (3-way), #1671 (passive UX), #1665 (component entry —
the driver-class/geometry/pad declaration schema + envelope wiring landed
2026-07-24 out of sequence, ahead of this phase order; hardware validation
and the research-prefill-audit portion remain open), #1672 (mic
arbitration/transfer-cal), relay-tier productization.
Every phase: PR flow, adversarial review to 0/0, hardware validation on
JTS3, issues for anything parked.

## Issue ledger (all open threads, one place)

#1650 relay voids (two located causes) · #1652 anomaly/quality program ·
#1654 Fix-4 tweeter-sweep energy (revival trigger fired ×3) · #1656
crossover-v2 wrong-cal primary scope · #1658 capture-page on-device pass +
optional nits · #1660 room-relay device threading · #1664 worktree hygiene ·
#1665 component entry + pad declarations (schema + pad/class-declaration
slice landed 2026-07-24; JTS3 hardware validation and the research-prefill-
audit portion still open) · #1666 apply promotion · #1667 trim-band bias
(ripple-optimal solve landed 2026-07-24, Phase 3; JTS3 hardware re-verify
still open) · #1668 driver linearization (this doc's Phase 2) · #1669 3-way · #1670
rename · #1671 passive-class UX · #1672 mic HF arbitration · #1675 ka-
beaming crossover guidance (simple v1 landed alongside #1665; Bessel
beamwidth-vs-horn-coverage matching and the JTS3 Fc re-tune bench
experiment remain open).

Opened since, and load-bearing for the 2026-08-17 rulings: #2600 blend-window
instrument blindness · #2603 the driver low-limit's two declared values ·
#2636 the headless lab capture client's revival · #2653 the level datum's
frame-coherence condition · #2662 the capture-source seam. Campaign-wide wave
state lives in
[`audio-commissioning-roadmap.md`](audio-commissioning-roadmap.md), not here.

Load-bearing for decision 14 (2026-08-18), both from that day's
lateral-statistic redesign study: #2710 per-role integer-sample alignment
quantization · #2711 bank `branch_operator_by_role` per Fc candidate — the
enabling change for any candidate-sensitive lateral statistic.

This pass (#2603) verified decisions 8–9 and the low-limit claims — including
correcting decision 4's cut-only claim above — against the code in this
change: `driver_protection.py`'s owner-ruling derivation, the B&C DE250
published-figure fact, and the linearization boost cap in `camilla_yaml.py` /
`linearization_fit.py`. Decisions 1–3, 5–7, and 10–13, the region-based
adjustment contract, the prescriber seam, and the rest of the historical/
appendix material were not re-verified in this pass.

The 2026-08-18 pass added decision 14 and the Measurement Program v2 section
only, and verified that section's own code claims at HEAD: the phase
vocabulary and `SUMMED_SWEEP_PHASES` membership, `position_angle_deg`'s
±7°/±22° derivation at `MARK_DISTANCE_M`, the shipped Full-tier plan
(15 captures, 13 displayed minutes, six 42.18 s lateral poses), the ramp's
dBFS window and the calibration reader's lack of a sensitivity term, the
declared commissioning SPL fields, the three N≥3 policy floors, and the pilot
pair's duration. Nothing else in this doc was re-verified in that pass.

Last verified: 2026-08-18
