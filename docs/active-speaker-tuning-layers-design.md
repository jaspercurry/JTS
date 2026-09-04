# Active speaker tuning — the layer model (design)

> **Status: adopted direction.** Owner-approved 2026-07-23 after the
> "no sparkle" investigation (issues #1666–#1668; forensic evidence in
> `captures/xover-e0-2026-07-21/OVERNIGHT-REPORT.md`, session-artifact).
> This doc is the execution handoff for the implementing session: the
> architecture is decided; per-phase design details are decided during
> implementation within these boundaries. Companion operational truth for
> today's shipped flow stays in
> [tuning-operator-runbook.md](tuning-operator-runbook.md).
> **Instrument update (2026-07-25):** the speaker layer's measurement
> instrument and the "top of the table" tolerances are concretized by
> [linearization-campaign-2026-07.md](historical/linearization-campaign-2026-07.md) (adopted): the
> single-point gated sweep becomes a spatially-averaged gated capture
> cloud with declared per-band tolerances; that plan wins on instrument
> and spec details, this doc stays canonical for the layer architecture.
> **Commissioning revision (2026-08-04; not shipped):**
> [linearization-campaign-2026-07.md](historical/linearization-campaign-2026-07.md)
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
> which unit — not who owns which correction. **The program is not built**;
> where that section states shipped behavior it says so and cites the symbol.
> Operational truth for the shipped flow stays
> [tuning-operator-runbook.md](tuning-operator-runbook.md).
> **The linearization pipeline (ratified 2026-08-19; no crossover parameter is
> chosen by measurement today):** an eighth ruling adds decision 15 and one new
> section,
> ["The linearization pipeline — seed → crossover science → EQ"](#the-linearization-pipeline--seed--crossover-science--eq-ratified-2026-08-19).
> The five-layer ownership is unchanged once more. What it rules on is the
> **order in which the layers get tuned** — seed the crossover from declared
> driver data, exhaust the crossover's own parameters against measurement, and
> only then EQ what is left — not who owns which correction. Every stage there
> carries an explicit EXISTS / IN FLIGHT / MISSING status, because most of the
> middle stage does not exist yet.
> **Horn-droop correction ruling (2026-08-29):** the reference-tier mic-trust
> pair (`mic_trust_limit` / `_MIC_TRUST_TABLE_HZ`, "Cold-start priors" below)
> widens from 8 k/16 k to **12 k/20 k** — full correction allowance through
> the horn's measured droop, tapering to zero only past the tweeter's own
> beaming onset rather than mid-droop. The taper still encodes beaming
> prudence, not mic distrust: the registered reference mic stays
> serial-calibrated across this whole range. Consumer/phone rows are
> unchanged.

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
the frame at its source; see tuning-operator-runbook.md), and the
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
["Layer Boundary"](historical/active-speaker-dsp-investigation-history.md#layer-boundary)
section of the active-speaker DSP investigation history.

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
    deciding. (Step (iii) has since run and both candidate prescribers were
    built and exercised on jts3 — what that series settled, and what it did
    not, is decision 15's, 2026-08-19.)

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

**The ruling below was ratified on 2026-08-19; its middle stage chooses no
crossover parameter by measurement today.**

15. **Linearization is a strictly ordered pipeline — seed, then crossover
    science, then EQ (2026-08-19).** It is BOTH-AND, never either-or, and the
    order is the ruling. **(P1)** The operator enters driver information, and
    the flow derives basic trims and a basic crossover placement from it.
    **(P2)** The crossover is then tuned with maths, science, and experiment
    until there is high confidence it is as good as it is going to get.
    **(P3)** ONLY THEN does EQ iron out the rest, across the entire trusted
    measurable region, to super flat. What the order buys: a filter spent
    flattening a summation error that a still-free crossover parameter could
    have removed is a filter aimed at the wrong cause, and it hides that cause
    from every later measurement rather than fixing it. The stages, their
    per-stage build status, and an honest inventory of what does not exist:
    ["The linearization pipeline"](#the-linearization-pipeline--seed--crossover-science--eq-ratified-2026-08-19).

## Layer 1a concretely — UX and data flow

> **Amendment (flat-linearization PR-3b, 2026-07-26).** "The household UX
> does not change" was true of LINEARIZATION, which is what this section is
> about — it still adds no flow, no wizard, and no sweep of its own. It is
> no longer true of the commissioning flow as a whole: a separate change,
> the spatial cloud, took it from ~3 captures at one position to 16 across
> a guided walk (the "one tap" counts below moved with it). Read the claims
> here as scoped to Layer 1a's own footprint; the flow's shape lives in
> [`tuning-operator-runbook.md`](tuning-operator-runbook.md).

**The household UX does not change.** One commissioning flow, the same
phone-tap ethos (set the mic, press Go, ~3 captures, fast honest verdicts):
CHECK → MEASURE → explicit household Apply → VERIFY. Apply is a separate
`POST /correction/crossover/v2/apply`, and verification follows it.
Linearization is
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
2. **APPLY (one more emitted stage).** The household explicitly posts the
   accepted candidate. Baseline emission then gains one per-role linearization
   filter stage, with the same transaction and safety posture (non-positive
   gains + headroom accounting).
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
   divergent bands, ≤2 iterations) rides the same explicit-apply→re-measure
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
to 12 kHz, taper to zero by 20 k — widened from 8 k/16 k by the 2026-08-29
horn-droop correction ruling above; consumer: 6 k/12 k; phone: 3 k/8 k) and
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

**Measurement instrument, in one paragraph:** the gated far-field sweep at the
listening axis is the Layer-1 instrument. The analysis finds the direct arrival per driver,
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
to the confidence ceiling below (20 kHz reference tier since the 2026-08-29
horn-droop correction ruling above; was ~16 kHz). **Owner ethos: no
subjectivity** — sizing is measurement; where measurement runs out, a
declared-driver-type continuation policy takes over, disclosed as such.

The stage (`_hf_continuation_stage`,
[jasper/active_speaker/linearization_fit.py](../jasper/active_speaker/linearization_fit.py))
runs AFTER the flattening peaking loop:

- **Confidence ceiling from mic trust.** The ceiling is the mic-trust term's
  taper-zero (20 kHz reference tier — the grid's own top edge, since the
  2026-08-29 horn-droop correction ruling above widened it from ~16.4 kHz);
  the knee is where its taper begins (~12.1 kHz, was ~8.2 kHz). Eligible
  only when the fit band reaches the ceiling region (`fit_hi ≥ knee`) —
  woofers/mids fall out with no per-role branch.
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
  down over the unseen band — pulled to the geometric mean of the ceiling and
  Nyquist where ×1.25 would not fit below Nyquist (on a reference mic that is
  21.9 kHz at 48 kHz). Unknown → taper is the conservative default.
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
  COMMITTED raw trim plus that branch's **level-band give-back**: the measured
  before-vs-after level delta read by `solve_branch_trims` over
  `branch_level_bands_hz` — the same estimator, averaging domain, and bands that
  solved `raw_trim_db` and that `realized_level_match` uses to grade the
  committed pair. A shared shift then normalizes the pair non-positive so a
  branch whose give-back exceeds its raw attenuation can never become a boost;
  the shift preserves relative leveling exactly and is honest extra ledger.

  **The invariant, and why the band is load-bearing.** A give-back spent against
  a trim must be measured in that trim's frame. The committed pair then lands
  level by construction: `realized = (level_t_pre − level_w_pre) + (raw_t −
  raw_w)`, which `raw` is defined to zero. The estimator's known +0.54 dB
  linear-grid systematic cannot reach that result — it *telescopes out*, since
  every term comes from the same call, which is a stronger property than
  partial cancellation (the per-role biases actually differ by ≈0.45 dB).

  **The invariant has a precondition, and it is not always met.** The give-back
  is the right adjustment for a base that came from this same solve. Usually
  `raw_trim_db` did — `solve_branch_trims` over these bands produces
  `trim_t_band_average`. But the MEASURE path may hand over the **ripple-polished**
  tweeter trim instead (`solve_ripple_optimal_trim`, a *flatness* choice),
  admitted only while it sits within `REALIZED_LEVEL_MATCH_TOLERANCE_DB` (3.0 dB)
  of the band average. When it fires, δ of polish becomes exactly δ of realized
  inter-driver level error — which is **why that bound is the level gate's own
  tolerance** rather than a number this seam picks. It was
  `RIPPLE_TRIM_SANITY_MARGIN_DB` (6.0 dB), double the gate, until the
  realized-level demotion
  ([`measurement-loop-doctrine.md`](measurement-loop-doctrine.md) deviation (i)):
  a polish legal by its own guard could push the pair past the level gate, so
  every δ in 3.0–6.0 dB was admitted and then produced a round the session was
  certain to refuse. Coupling the two closes that dead band by construction; a
  rejected polish falls back to the band-average seed, disclosed. The gate no
  longer refuses either — it banks a finding and the round proceeds — and the
  polish delta is published on every round (`polish_delta_db`), reaching that
  finding as its attribution. Whether the anchor should bind to
  `trim_band_average_db` instead is still an open design question (#2653).

  **What the fix does is level-match, not quieten.** It is tempting to read this
  as "the tweeter gets quieter"; it does not. The committed trim moves by exactly
  the realized level error the old anchor was carrying — in whichever direction
  that error sat. A branch whose correction lives inside the graded band can
  legitimately end up **hotter** than before, still under the non-positive clamps
  and still level-correct. The property being restored is equality between the two
  branches at the handoff, not a monotone reduction in level.

  Reproducible: **+8.13 dB** hotter on a correction confined to the graded span
  (level-band give-back 9.000 dB against the core band's 0.870 dB), pinned by
  `test_the_fix_can_commit_a_HOTTER_trim_and_that_is_still_correct`, which also
  asserts that the *quieter* old pair is the one that mis-levels there. A larger
  **+9.21 dB** worst case was reported by the hearing-safety review's adversarial
  corpus; that corpus is not banked in this repo, so it is cited as their
  measurement rather than reproduced. The direction claim rests on the algebra,
  not on either figure.

  **What this replaced (2026-08-19).** The anchor used to take
  `LinearizationFit.correction_giveback_db`, the same measured delta over each
  driver's own CORE band. That number is still computed and still published —
  as `core_band_giveback_db`, answering the audible-band question — but it does
  not place the trim, because the core band is not the band the verdict grades.
  On a compression-horn tweeter the two barely overlap (core 2077–7949 Hz
  against a graded 1649–3297 Hz), so the horn's 3–8 kHz correction bought back
  level where nothing is measured and the pair shipped carrying
  `giveback_t − giveback_w` as pure inter-driver error: +3.67 dB hot on jts3,
  a realized level 3.01 dB apart against a 3.0 dB tolerance, and a round refused
  by 0.01 dB. It mis-levels in EITHER direction depending on where a
  correction's energy sits — the same defect reads 1.835 dB *dull* on the
  synthetic conductor fixture. Evidence chain:
  `captures/wired-night-2026-08-19/run-log.md` §10.9.

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
2. **An LLM prescriber** — [`tuning-master-plan.md`](tuning-master-plan.md)
   is the planning authority for agent-assisted tuning and is where that
   option's shape lives (it superseded
   [`llm-native-tuning-workbench-plan.md`](historical/llm-native-tuning-workbench-plan.md)
   on 2026-08-21).

The owner's stated concern is **complexity**. The observation that makes
deferring cheap: everything except the trend engine itself — the contract,
the vocabulary, the bounds, the harness, the receipts — is **common to both
paths**, so nothing built now is wasted by either choice.

**Update (2026-08-19): the deferral condition above has been exercised, and it
did not resolve the way it was written to.** Both candidates now exist — the
deterministic solver and an LLM prescriber behind the same deterministic
validators (`jasper/cli/crossover_prescriber.py`,
[`blend_prescription.py`](../jasper/active_speaker/crossover_v2/blend_prescription.py),
[`evidence_packet.py`](../jasper/active_speaker/crossover_v2/evidence_packet.py))
— and both were run against the blend region on jts3. What the series did *not*
produce is the condition this deferral names: the blend region did not measure
correctable through that stage as it stands. The open question consequently
moved off *who prescribes* and onto *what the prescriber is allowed to shape*
and *when it may act at all* — which is stage **P3** of
[the linearization pipeline](#the-linearization-pipeline--seed--crossover-science--eq-ratified-2026-08-19).
The seam itself is unchanged and still open; point there, do not restate it
here.

## Measurement Program v2 — the capture schedule (ratified 2026-08-18)

> **Status: ratified design, NOT built.** Owner-ratified 2026-08-18. **The
> program is not implemented.** The section does state shipped behavior in
> places — deliberately, because a schedule is only legible against the one it
> replaces — and every such claim says so in the sentence and names the symbol
> it was read from. Read anything not marked that way as ratified plan.
> Operational truth is
> [tuning-operator-runbook.md](tuning-operator-runbook.md).
> Sequencing, and what this reverses or supersedes, are at the end of this
> section.

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
   targeted. The band is legal under the declared commissioning ceiling, with
   **5 dB of headroom on every shipped preset since the 2026-08-23 owner
   ruling**: `SafetyEnvelope` declares `initial_sweep_level_db_spl`
   (default 65.0) and `max_commissioning_level_db_spl` (default 85.0), each
   validated into 45–85
   ([`jasper/active_speaker/profile.py`](../jasper/active_speaker/profile.py)),
   and the ramp reads neither. The shipped preset
   (`epique_e150he44_eminence_f110m8_safe_v1.json`) declares **85**, and the
   preview-staging path rides that dataclass default rather than restating it
   — so the ratified band's top sits 5 dB under the ceiling. A band top AT
   the declared limit would still be legal by the 2026-08-17 boundary ruling
   above (a value at the declared limit is a sanctioned operating point, no
   nanny margin); the anchor simply no longer sits at it;
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
| 7 | 0° (return) | short D probe — **the session's noise floor** | short † |
| after apply | all five | **S at each angle** | † |

† Stops 1–6 are the figures the ratification fixed, and they sum to **215 s**.
The stop-7 probe and the five post-apply summed captures are the **~145 s
balance** of the ≈6 min below; the ratified number is the total, and the plan
builder owns the split once this is built.

Two properties fall out of the grid rather than needing a measurement of their
own:

- **The session's own noise floor.** Stop 7 repeats stop 2's position, so
  anything that "moved" between them is measurement noise, not the speaker.
- **Moved-versus-fixed attribution.** A feature that tracks angle belongs to
  the speaker; one that stays put belongs to the room.

And post-apply verification happens **across angles**, not on-axis only.

**The budget claim, and exactly what it is.** ≈**6 min** of measurement. The
baseline it is measured against is **moving under two PRs in flight**, so all
three numbers are stated rather than the flattering one:

| baseline | Full journey | source |
|---|---|---|
| shipped at ratification | 15 captures, **13 min** | `tier_display_info()['full']`, re-derived at HEAD 2026-08-18 |
| after the session trims | 14 captures, **12 min** | [PR #2715](https://github.com/jaspercurry/JTS/pull/2715)'s own derivation |
| after the lateral-walk pause | 9 captures, **7 min** | [PR #2717](https://github.com/jaspercurry/JTS/pull/2717); re-derived here by flipping `STAGE1_INCLUDES_LATERAL` off and clearing the display cache (stage 1: 9 → 3 captures) |

So the honest contrast is **≈6 min against 7**, near break-even — because the
pause buys its time by not measuring off-axis at all, which is exactly what
v2 reinstates. **The case for v2 does not rest on time**; it rests on the
grid's information richness (the S half at every angle, the noise floor, the
attribution property) at roughly the time cost of measuring nothing off-axis.
The ≈6 min itself is a **design projection** from measured 2026-08-18 session
evidence — that day's per-capture census plus gate evidence, with the relay's
per-capture wall-clock cost (≈11.5 s, distinct from the shipped
`CAPTURE_PLAN_PER_CAPTURE_OVERHEAD_MS = 20_000` plan-budget allowance)
measured across the day's ten banked walk journals — not a stopwatch on a
built flow. When it is built, `CapturePlan.estimated_minutes()` owns the
number, exactly as it does today.

**The machinery this reuses is built; the stage-1 arming that once gated it is
retired.** Stage 1's lateral walk replays the MEASURE program verbatim at 0°,
±7°, ±22°, 0° — `LATERAL_POSE_PROMPTS`' six poses, each a representative
~41.6 s at the display constants (`_DISPLAY_ROLES_BANDS` / `_DISPLAY_FC_HZ`;
the exact duration is topology-dependent), and `lateral` is absent from
`SUMMED_SWEEP_PHASES`, so those poses are per-driver captures. **The D half of
this grid exists in the tree**: [PR #2717](https://github.com/jaspercurry/JTS/pull/2717)
first paused it behind one flag rather than deleting it, but ticket 2.3
([tuning-master-plan.md](tuning-master-plan.md) ruling R1) has since deleted
that flag, `STAGE1_INCLUDES_LATERAL`, along with the candidate sweep it fed —
forcing it back on is no longer possible, because it no longer exists. What
survives is the poses, the prompts, and `position_angle_deg`'s bearings; an
operator's staged angle walk runs them today as forward-model evidence (see
Stage P2 below). That is still a *stronger* footing for v2 than building from
nothing: what v2 needs is already written, just no longer reachable by
flipping a stage-1 flag, and its own evidence says the poses are clean while
the max-over-poses reduction that once read them was not. What v2 adds on top
is the **S half at every angle** (today only the at-mark entry baseline is
summed before apply — `SUMMED_SWEEP_PHASES` again), the session-level
one-timers, the explicit repeat and noise-floor structure, and a schedule
that stops paying per-capture relay cost at every pose.

### Constraints carried forward

Four, each from evidence this section cites rather than restates:

1. **Off-axis pose data must not feed a selector statistic** — moot as of
   2026-08-22 rather than merely gated. The 2026-08-18 lateral-statistic
   redesign study set a re-introduction bar: (i) candidate dependence enters
   through the *operator*, not through the band; (ii) the rank-1-versus-rank-2
   gap exceeds same-candidate repeat noise; (iii) band-edge neutrality; (iv)
   immunity to a zero-offset pose. Nothing banked ever cleared it — every such
   statistic is exactly candidate-blind — and the enabling change would have
   been banking `branch_operator_by_role` per candidate
   ([#2711](https://github.com/jaspercurry/JTS/issues/2711), which holds the
   study's finding and the retention-guard caveat). The switch this bar
   governed, `STAGE1_INCLUDES_LATERAL`, and the statistic it fed,
   `fc_sweep.py`'s candidate sweep, are both deleted — cancelling the #2717
   re-flip it had planned — so #2711's bar now has nothing left to gate
   ([tuning-master-plan.md](tuning-master-plan.md) ruling R1, ticket 2.3).
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
   pilot pair every MEASURE- and VERIFY-shaped capture opens on
   (`DEFAULT_PILOT_DURATION_S` 0.8 ×2 + `DEFAULT_PILOT_GAP_S` 0.5 ×2), plus its
   1.0 s `PILOT_AMBIENT_WINDOW_S`; what replaces it is the drift sentinel
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

### Sequencing, and what this reverses or supersedes when it ships

Ratified 2026-08-18; **not implemented**. It builds on decision 13's
capture-source seam ([#2662](https://github.com/jaspercurry/JTS/issues/2662);
the layer contract and what slice 1 landed are stated once above, under
["Composition & code seams"](#composition--code-seams-verified-present)) —
**after** the wired provider (W2b) and **together with** the SPL-anchor
leveling build, which shares its machinery. The wired-only ruling is the frame: speaker
calibration is wired-microphone only, while the relay/phone path survives for
room correction, a later rework where the string method serves seat-position
prompts too.

Interim steps already landed or in flight. The verbs differ, and the
difference is the point:

- **Superseded** — the cloud 6→5 trim and the courtesy-prelude grouping
  ([PR #2715](https://github.com/jaspercurry/JTS/pull/2715)): this schedule
  replaces the walk they trim.
- **Reversed, then retired.** The lateral walk's pause
  ([PR #2717](https://github.com/jaspercurry/JTS/pull/2717), landed
  2026-08-18) was deliberately reversible — one flag, nothing deleted — for
  exactly the reason above: v2 would reinstate off-axis measurement by
  flipping it back. Ticket 2.3
  ([tuning-master-plan.md](tuning-master-plan.md) ruling R1) deleted that
  flag and the candidate sweep it fed on 2026-08-22, so the pause is no
  longer reversible by flag; any future off-axis schedule (v2 or otherwise)
  now has to be built, not un-paused.
- **Subsumed** — the shape the walk was to return as: a 4-capture per-driver
  off-axis **reporter, never a score term**, which is the redesign study's
  conclusion as recorded in
  [PR #2717](https://github.com/jaspercurry/JTS/pull/2717). Stops 3–6 plus the
  stop-7 return are that reporter, inside a schedule.

## The linearization pipeline — seed → crossover science → EQ (ratified 2026-08-19)

> **Status: ratified design; one stage exists, two are partly built.**
> Owner-ratified 2026-08-19 at the close of the wired
> overnight campaign on jts3. **Every stage below carries an explicit STATUS
> label — EXISTS / IN FLIGHT / MISSING — and nothing outside an EXISTS label
> describes what the speaker does today.** Where a stage states shipped
> behaviour it names the symbol it was read from; where it states a measured
> result it names the dated evidence. Read everything else as ratified plan,
> exactly as in Measurement Program v2 above. Operational truth for the shipped
> flow stays
> [tuning-operator-runbook.md](tuning-operator-runbook.md).

### The ruling

Three stages, run in order. In the owner's framing it is **BOTH-AND, strictly
ordered** — not a choice between tuning the crossover and applying EQ:

1. **P1 — seed.** The operator enters driver information; the flow derives
   basic trims and a basic crossover placement from it.
2. **P2 — crossover science.** The crossover is tuned with maths, science, and
   experiment until there is high confidence it is as good as it is going to
   get.
3. **P3 — EQ.** *Only then* does EQ iron out the rest, across the entire
   trusted measurable region, to super flat.

**Why the order is the ruling and not a preference.** EQ can flatten a
magnitude error whatever caused it, which is precisely the hazard: a filter
spent hiding a summation error that a still-free crossover parameter could have
removed is aimed at the wrong cause, costs headroom permanently, and — because
the correction is now baked into every subsequent measurement — removes the
evidence that the parameter was ever wrong. Ordering the stages keeps each
lever answerable for its own class of defect. It is the same separation the
five-layer model already enforces between shape and level; P1–P3 apply it along
the *time* axis of a commissioning session.

### How the stages map onto the five layers

This is not a second taxonomy. The pipeline is the **tuning order of the layers
already named above**, and each stage's contract is owned where it always was:

| stage | decides | owning layer / contract | status |
|---|---|---|---|
| **P1** seed | initial Fc, protection posture, polarity, geometry-bounded delay, first trims — all from declarations, no audio | Layer **1b**, from the component-entry declarations (#1665) and decisions 8–9 | **EXISTS** |
| **P2** crossover science | the final non-EQ parameters: polarity, per-branch delay, Fc, slopes/order, branch gains | Layer **1b** again — contract **(c)** reserves all but slopes/order, which is 1b's own | at-mark substrate **EXISTS**; per-angle replay **PAUSED**; search + guards **MISSING** (4 named gaps) |
| **P3** EQ | the minimum-phase residue across the whole trusted band | Layer **1a** per-driver, plus the blend region's summed owner under contract **(b)** | **partially EXISTS** |

Layers 2–4 (bass, room, preference) are untouched by this ruling: the pipeline
runs entirely inside the speaker layer, and its output is the flat device the
room layer then corrects for a position.

### Stage P1 — seed from driver knowledge

**STATUS: EXISTS.** This is the one stage that runs today, end to end, with no
measurement involved.

The operator enters driver information — the component-entry surface of #1665,
persisted by
[`design_draft.py`](../jasper/active_speaker/design_draft.py), which by its own
contract "records what the operator is trying to build and any externally
researched driver facts" and deliberately does not compile filters or authorize
playback. The externally-researched half arrives through the driver-research
prompt in
[`driver_safety_prompt.py`](../jasper/active_speaker/driver_safety_prompt.py),
against the request and result contract owned by
[`driver_safety.py`](../jasper/active_speaker/driver_safety.py)
(`driver_research_targets`, `validate_driver_research_result_shape`,
`finalise_research_result`), which is decision 9's rule in code: it asks for the
manufacturer's **published** facts and nothing else, and a derived margin is
never smuggled into a datasheet field.

From those declarations the seed is computed deterministically by
[`crossover_preview.py`](../jasper/active_speaker/crossover_preview.py)
(`build_crossover_preview`) — "the deterministic bridge from a saved design
draft to a future protected startup config… bounded filter intent only: no
CamillaDSP YAML, no config load, no playback authority, and no sound." What it
seeds, and from what:

- **Fc, out of the driver's safety envelope.** The tweeter's minimum
  recommended crossover frequency *with its slope condition* is decision 8's
  single declared owner, resolved by
  [`driver_protection.py`](../jasper/active_speaker/driver_protection.py)'s
  `resolve_driver_low_limit` / `declared_protection_highpass_floor_hz` off the
  `recommended_highpass_hz` field the operator's research response fills in.
  The collapse to one owner has landed: `crossover_preview.SCHEMA_VERSION` was
  bumped to 2 precisely so a preview saved before it — carrying un-derived
  driver payloads — cannot be reused (#2603). The woofer's breakup ceiling and
  the horn's coverage bound the choice from above **as acoustics**, but only
  the first of the two has a declared field the code reads:
  `radiating_diameter_mm` feeds #1675's ka-beaming hint
  (`branch_chain.beaming_onset_hz`), while coverage rides the driver notes as
  operator prose since #2872 deleted the structured field nothing consumed.
- **A protection slope, not a crossover order.** Worth separating, because the
  two are easy to conflate: the *crossover* filter's type and order are
  declared, not derived. What the low limit's slope condition derives is the
  **protective** high-pass floor, `PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE` in the
  same module. P1 seeds a protection posture; choosing the crossover's order
  against measurement is P2's.
- **Polarity, and a geometry-bounded delay.** Declared geometry does not
  produce a free delay guess; it produces a **bound around a seed** —
  `null_walk.geometry_seed_us` converts the signed path difference into
  microseconds, `delay_sweep.sweep_spec` bounds "one
  driver-to-driver walk from an a-priori geometry estimate", and
  `measured_candidate.py`'s input contract fixes `delay_bound` at
  `declared_geometry_plus_minus_half_period`. The acoustic-centre provenance is
  explicitly operator-attested
  (`commissioning_evidence.RegionGeometryAttestation`). So the physical offset
  sets the window; the value inside it is P2's, from measurement.
- **First trims,** from declared sensitivities and the declared in-line pad
  ([`driver_pad.py`](../jasper/active_speaker/driver_pad.py),
  `effective_sensitivity_db`).

**What P1 is not.** It is a *starting point*, not an answer, and this stage's
one honesty rule is that nothing it produces is ever reported as measured. The
seed is an intent artifact with no acoustic evidence behind it, which is exactly
why P2 exists.

### Stage P2 — crossover tuning by measurement

**STATUS: method ratified; the at-mark measurement substrate EXISTS, per-angle
replay RUNS when an operator stages a walk, the automatic per-angle schedule is
RETIRED (ticket 2.3), and the search and its guards are MISSING.** No
crossover parameter is chosen by measurement today. But the per-driver complex
capture this stage consumes is already shipped **at the mark** — see "how much
of it already exists" below — so the gap is narrower and differently shaped
than a reading of "P2 is unbuilt" suggests. Off the mark there is now **one**
route: capturing per-driver responses at angles as **forward-model input**,
which computes no pose-ratio statistic and so needs no re-introduction bar.
The automatic lateral WALK route is gone with its flag, not merely gated —
there is nothing left to un-pause. Since 2026-08-19 the forward-model route
has a seam —
[`angle_capture.py`](../jasper/active_speaker/angle_capture.py) resolves
`{per-driver | summed} x {angles} x {arm | human-guided}` onto the shipped
program, pose and gate machinery — and, since the same day, a **door**: an
operator states a walk with `jasper-angle-capture`
([`jasper/cli/angle_capture.py`](../jasper/cli/angle_capture.py)), which resolves
it through that seam and banks it in a single-use mailbox
([`angle_capture_spool.py`](../jasper/active_speaker/angle_capture_spool.py))
for a session to take — and, since 2026-08-19, the **take**: the next
`/correction/crossover/v2/session` open consumes that document, walks its stops
as the session's lateral group, and banks every accepted pose's raw WAV with an
angle-stamped sidecar. So per-driver data off the mark is now capturable, and
the fourth bullet below records what still is not: the fixed stage-1 walk is
retired, not merely paused, and nothing captures off-axis unless an operator
stages a walk.

**The ruling that gated it, recorded.** A per-driver stop at a pose is a real,
shipped capture path — the conductor's `_consume_lateral_pose` screens it,
builds its per-driver curves and retains it — and that path is reached only
through `PHASE_LATERAL`. The walk's **last** index runs `_close_lateral_walk`,
which used to route a stage-1 walk into R17's selector-adjudication and route
an evidence walk around it. A session wired naively onto that machinery would
have reached the statistic
[#2711](https://github.com/jaspercurry/JTS/issues/2711) bars, which is precisely
the bar-dodge #2732 built the seam to avoid. Two ways out were on the table:
(a) suppress the close for a walk that is not a stage-1 lateral walk, or
(b) give the walk its own group phase.

**Option (a) shipped, then ticket 2.3 collapsed the fork it was dodging.** A
lateral group still declares its **consumer** —
`LATERAL_CONSUMER_FC_SELECTOR` for the walk over the ratified stage-1 table,
`LATERAL_CONSUMER_FORWARD_MODEL` for a taken evidence walk over an operator's
own stated angles
([`crossover_v2/journey.py`](../jasper/active_speaker/crossover_v2/journey.py))
— but the two now differ only in **which pose table** they run, not in who
reads the result. `lateral_adjudicates()` is deleted along with R17's
candidate sweep it gated
([tuning-master-plan.md](tuning-master-plan.md) ruling R1, ticket 2.3): no
walk's close adjudicates any more, so `_close_lateral_walk` publishes nothing
for either consumer. There is one close event,
`correction.crossover_v2_lateral_walk_closed`
(`session_id`, `consumer`, `planned`, `captured`, `mark_return_drift_db`),
fired on every lateral walk — the earlier suppress-vs-adjudicate split
(`event=correction.crossover_v2_lateral_close_suppressed`,
`fc_statistic_paused=true`) is gone with the branch it recorded. Why the
consumer tag was still worth keeping rather than collapsing to nothing: a
second group phase would have duplicated the whole per-driver-at-a-pose
ladder — screens, curve build, retention, retry and settle bookkeeping — to
change one thing about who reads the result, and the two walks measure the
same thing at the same poses with the same program.
`LATERAL_CONSUMER_FC_SELECTOR`'s string is kept only because it is banked on
every round that ran one; new operator-staged walks take the forward-model
consumer. #2711's bar is now moot for this flag rather than merely untouched:
the statistic it guarded is deleted, not reachable-but-blocked. The search
and its guards remain a separate, still-unbuilt concern.

**The goal, stated as a stopping condition.** Drive the non-EQ parameters —
polarity, per-branch delay, Fc, slopes/order, and branch gains — to the point
where there is high confidence they are as good as *these drivers in this
cabinet* permit, and only then unfreeze EQ. Four of those five are the tools
region contract **(c)** above reserves to themselves — it enumerates level,
alignment (delay and polarity), and Fc. **Slopes/order is not in (c)'s
enumeration**; its owner is Layer 1b's own job description at the top of this
doc ("drivers sum correctly: crossover filters, **scalar** trim per driver,
relative delay, polarity"). So P2 is what (c)'s "their own evidence" turns out
to require, plus the one lever (c) never names.

**The measurement it needs, and how much of it already exists.** Per-driver
**complex** responses — magnitude *and* phase — at every angle. **At the mark
this ships**, and the shape it ships in removes a problem P2 was originally
scoped to solve. **Off the mark it now runs when an operator asks for it** —
the fourth bullet below is an operator-driven capability, not an automatic one,
so the angle SCHEDULE a search needs is still something a session has to be
told, not something it produces.
`build_measure_program`
([`program.py`](../jasper/audio_measurement/program.py)) schedules the woofer
and tweeter sweeps **non-overlapping inside ONE capture**, routed by channel —
that module's own docstring states the design intent: CHECK/MEASURE programs
are 2-channel WAVs, "ch0 → woofer output path, ch1 → tweeter output path", and
"per-driver sequencing lives in the WAV channels so the CamillaDSP
commissioning graph stays static and provable." Four consequences worth stating
because they are easy to re-derive wrongly:

- Per-driver **complex** transfer functions are produced and direct-arrival
  gated — `DriverResponse.complex_tf` in
  [`program_analysis.py`](../jasper/audio_measurement/program_analysis.py).
- The two drivers therefore share an **exact** common time origin: they are in
  the same capture, so **there is no cross-capture alignment problem for the
  A/B pair at all**. The USB start-offset scatter that motivates a shared
  reference never enters between woofer and tweeter. Exact as an *origin* is a
  separate question from intra-capture desync, and this is not a claim that A/B
  timing is solved — gap 1 below records a real +1.986-sample slip *inside* a
  capture that today's guard passes.
- In-capture drift is estimated (`DriftEstimate`) and the drift-corrected
  woofer-versus-tweeter anchor already ships as `anchor_delay_us`.
- **Per-angle per-driver capture is BUILT, and OPERATOR-DRIVEN — no household
  session captures off the mark on its own.** The machinery is real: a pose
  "replays MEASURE's program"
  ([`spatial.py`](../jasper/active_speaker/crossover_v2/spatial.py)), and since
  2026-08-19 an operator can point it at stated angles by staging a walk with
  `jasper-angle-capture` (the take, above). There is no AUTOMATIC route any
  more: `STAGE1_INCLUDES_LATERAL` was paused `False` on 2026-08-18 and ticket
  2.3 deleted it outright on 2026-08-22
  ([`crossover_v2_flow.py`](../jasper/active_speaker/crossover_v2_flow.py),
  [tuning-master-plan.md](tuning-master-plan.md) ruling R1), along with the
  candidate sweep it fed. `fc_selector.py`, which scored what that sweep
  gathered, was deleted the same day by ticket 2.4 — a round crosses at the
  corner the household declared or an operator pinned, so no comparison is made
  and no recommendation is produced, and there is no module left holding the
  scoring that would make one. A staged angle walk is per-driver but
  declares the forward-model consumer, so it feeds no selector; and the
  multi-position walks that run *automatically* yield nothing per-driver,
  because every cloud phase sits in `SUMMED_SWEEP_PHASES`
  ([`programs.py`](../jasper/active_speaker/crossover_v2/programs.py)) — and
  `STAGE1_INCLUDES_CLOUD_MEASURE` is `False` besides. Measurement Program v2's
  own "The machinery this reuses is built; the stage-1 arming that once gated
  it is retired" paragraph above owns that story: there is no flag left to
  re-enable, and its constraint 1 — the re-introduction bar
  ([#2711](https://github.com/jaspercurry/JTS/issues/2711)) — is now a record
  of why the deleted statistic never cleared, not a live gate. **An
  implementer scoping P2 should read the angle schedule as work, not as a
  given.**

**Branch muting is not the route, and must not be proposed as one.** A
commission-mute overlay would break the graph classifier:
`protected_neutral_program_origin`
([`camilla_yaml.py`](../jasper/active_speaker/camilla_yaml.py)) accepts the
program origin only when every commission-mute filter is exactly pass-through
(`{"gain": 0.0, "inverted": False, "mute": False}`) and returns `False`
otherwise. The interleaved-channel design exists precisely so the commissioning
graph can stay static and provable; muting a branch trades that away for
something the one-capture schedule already provides.

**So what does the timing pilot actually buy?** Its validated role narrows to
two things, neither of which is the A/B common origin:

1. **A sharper slip estimator** on the existing capture path — the gap named
   below.
2. Any future genuinely **cross-capture** comparison, such as session-to-session
   absolute phase, where two captures really must be tied to a shared event.

The method itself is unchanged: a **self-referencing acoustic timing pilot** —
the DUT's own tweeter playing a short chirp whose arrival is the reference.

> **Name collision, worth fixing before it costs someone a session.** This doc
> already uses "pilot" for a different signal: the **behavioural-linearity
> pilot** pair (`DEFAULT_PILOT_DURATION_S`) that Measurement Program v2's
> constraint 4 *removes* from the wired path, whose job is to prove the chain
> responded linearly and that the phone heard the speaker at all. P2's is a
> **timing pilot**, and its job is an arrival reference. They point in opposite
> directions — one is being deleted from the wired path, the other added to it
> — so this section says **timing pilot** every time, and the linearity pilot
> must never be reused as an arrival reference: it is not designed, placed, or
> gated for that.

The estimator's accuracy bar is loose in a useful way: what a slip measurement
needs is **repeatability, not correctness**. The pilot path is the same physical
path on both sides of any comparison, so multipath bias cancels in the
difference and only its **variance** enters the error budget. A **second timing
pilot after** the sweep yields that capture's playback-versus-capture clock
ratio, so in-capture drift is measured rather than assumed — which composes with
the shipped `DriftEstimate` rather than replacing it. The **summed-capture
self-consistency solve** — recovering the offset that makes the separately
measured branch responses sum to the measured summed response — is retained as
an independent cross-check, not as a replacement.

**The search.** An offline **complex-summation forward model** predicts the
summed response from the per-driver complex responses and a candidate parameter
set, computed with **the same biquad math CamillaDSP runs** — a model that
disagrees with the shipped filter realization is measuring its own arithmetic.
Its objective must be **commensurate with the on-device grade**: same smoothing,
same pooling, same frozen baseline reference (rule 5 of P3 below), so that a
predicted win and a measured win are the same quantity. The search is an
**outer discrete enumeration** (filter type, order, polarity) wrapped around an
**inner continuous optimization** (Fc, Q, per-branch delay and gain), bounded
throughout by the declared driver-safety envelope — P1's declared low limit with
its slope condition is a hard wall, not a starting guess.

**The on-device confirmation schedule**, coarsest lever first: **polarity →
delay → Fc → slopes → Bayesian refinement**. Each step carries a
**pre-registered acceptance** in the same shape P3 uses — a prediction banked in
the grading view's own units *before* the change is played, **2–3σ** on
frozen-reference pooled views, **≥3 pooled repeats**, and **rollback on loss**.
A **sim-to-real discrepancy term** is calibrated on **≥3–5 real trials before
the model's predicted signs are trusted at all**: this program's model
predictions have been anti-correlated with measurement more than once, so the
forward model earns its sign empirically or it does not get a vote.

**The exit criterion.** **K ≈ 3–5 consecutive rounds with no statistically real
improvement** freezes the crossover. Freezing is the event that hands control to
P3. An unfrozen crossover is a reason for P3 *not to run*, never a thing for P3
to compensate.

**What does not exist — the honest inventory.** Four gaps, naming what is
missing rather than planning how to close it. Read gap 1 against its own
heading: what is absent there is a *guard*, not the capture it guards.

1. **A sharper capture-integrity slip estimator — MISSING. The per-driver
   timed capture it would guard already EXISTS** (the interleaved program
   above). The residual gap is not a capture *mode*; it is the **sensitivity
   floor of the guard on the capture path that already runs**. Today's desync
   guard rejects a **+4-sample** silent slip and **passes a +2-sample** one —
   pinned as a parametrized boundary in
   [`tests/test_audio_measurement_program_analysis.py`](../tests/test_audio_measurement_program_analysis.py)
   (`test_desync_guard_keeps_its_teeth_after_d7`: `("sweep_w", 4, True)`,
   `("sweep_w", 2, False)`). At 48 kHz **2 samples is 41.7 µs**, which is
   **2.1× the 20 µs relative-phase budget** in gap 4 — so a capture carrying a
   phase error twice the whole budget passes the gate clean. This is not
   hypothetical: Stage-0's bank contains a real **+1.986-sample** silent USB
   slip that today's guard would pass. The fix-shape is therefore a sharper
   slip estimator **on the existing capture path** — the Stage-0 dual-pilot /
   cross-correlation algorithm is the candidate instrument — and explicitly
   **not** a new capture mode, and **not** branch muting.
2. **Vertical polar capability — MISSING.** The crossover's primary artifact is
   **vertical** lobing, and this rig measures horizontal angles only.
   ["Deferred axis — elevation (v2+)"](#deferred-axis--elevation-v2) above
   already records the gap from the instrument's side — the program samples zero
   vertical offsets, the household string-and-protractor method does not
   generalize as stated, and the lab arm's elevation capability is undetermined.
   P2 is the consumer that turns that deferral from tidy-later into blocking.
3. **The forward model — PRESENT 2026-08-19 to 2026-08-30; DELETED by ruling
   S7, as the optimizer over it was earlier, by ruling R1; RE-LANDED
   2026-08-31 by Wave 6 ticket 6.7, this time with the consumers S7 found
   missing (the 3.8 banked-solo loader, the 4.5 verify delta, and the
   `jasper-round-views forward-model` door).**
   The complex-summation predictor was `crossover_v2/forward_model.py`
   (`driver_plants` / `branch_operator` / `predict_sum`, plus the
   `XoverCandidate` it predicted for). It was pure and fixture-tested. The
   enumeration/optimization search over it landed with #2739 and never gained a
   product consumer; the tuning master plan's ruling R1 deleted it first — the
   candidate space, the objective, and the walk — rather than leaving it to
   accrete one. What remained afterward was offline **simulated evaluation**:
   corners declared by the operator, and the predictor saying what a variation
   of one would measure, at zero capture cost. The two things a ranking would
   have needed and never had — an objective in the grade's own currency, and a
   delay axis graded against measurement — are why it could never rank
   candidates, and why ruling S7 could delete it at zero product cost: it
   never gained a caller either.
4. **A Stage-0 timing acceptance test — MISSING; its bar is declared and its
   precondition is measured.** Pass bar: **relative-phase alignment residual
   ≤ 20 µs (3σ)** — the ~15° at 2 kHz that a ±0.5 dB summation prediction near
   Fc can absorb. No implementation of that test exists in the tree. **What
   the Stage-0 work unblocks has narrowed**, since the one-capture program
   supplies the A/B origin exactly: the bar now governs the **slip estimator**
   of gap 1 (and any future cross-session absolute-phase comparison), not an
   A/B alignment problem that does not exist. The numbers it produced stand,
   and they earned their keep by discovering the slip class rather than by
   validating an alignment: measured on jts3 on 2026-08-19, the chain's own
   cross-capture stability came in at **sd 7.33 µs, worst 14.51 µs** across 24
   same-angle pairs. **Do the arithmetic at the bar, because it does not obviously
   clear it:** 3 × 7.33 = **22.0 µs**, which is *above* the 20 µs bar before
   the timing pilot's own estimator has contributed anything at all. What keeps
   that from being a verdict is that the 7.33 sd is an **upper bound** —
   it sits at the integer-sample quantization floor (±0.5 sample = ±10.4 µs at
   48 kHz), so it measures the quantization as much as the chain, and the
   chain's true variance is unresolved until the quantization is removed. That
   is precisely #2710 below, which is why the next bullet escalates it from
   caveat to blocker rather than noting it. What the figure *does* settle is
   that the ±100 µs-class USB-mic jitter this rig class is usually assumed to
   have is refuted by an order of magnitude. Nothing downstream should be built
   until the test passes on a de-quantized measurement.

**One constraint this stage inherits rather than discovers.** Measurement
Program v2's constraint 2 —
[#2710](https://github.com/jaspercurry/JTS/issues/2710), per-role integer-sample
alignment quantization at ±20.833 µs on a 48 kHz chain — sits at **the same
order as P2's entire timing budget**. It is already flagged there as needing to
be located before the D14 aligner root fix leans on measured inter-driver
timing; P2 raises it from caveat to blocker, because — as the arithmetic in
gap 4 shows — a quantization floor this close to the acceptance bar consumes
the entire budget before the timing pilot's estimator is even measured.

### Stage P3 — EQ the minimum-phase residue

**STATUS: partially EXISTS.** What ships is the machinery *around* the
decisions — the fitting engine, the safety clamps and their bounds, the pooled
grading views, the predict-apply-remeasure-rollback protocol, and both
prescribers, all of which have run on hardware. What does not ship is the part
that decides: taking **"is there code in the tree that makes this rule's
decision?"** as the test, **two of the six rules below fail it outright and
three more pass only on the prescribed path** (rule 6 is a review discipline
rather than code at all), and the stage's *scope* is narrower than this ruling
requires. The per-rule table at the end of this stage makes that count
reconstructable. Read "partially" strictly.

**When it runs.** Only after P2 freezes the crossover — not before, not
alongside.

**What it covers.** The **entire trusted band**, roughly **357 Hz to 20 kHz** on
this rig — a gate-derived floor (`gating.f_trusted_floor_hz`, `2.5 / window_s`,
so 357.14 Hz at this rig's 7 ms gate) and a mic-derived ceiling
(`linearization_envelope.mic_trust_limit`'s taper zero, 20 kHz on a `reference`
mic), so both edges are now derived quantities rather than the hand-set
analysis convention this paragraph used to describe (ADR-0194;
`flat_spec.BEST_EFFORT_ABOVE_HZ` survives as the nominal value the ceiling
moves) — and not merely the crossover window. The only **shared** EQ stage
that exists today is the blend stage, safety-reviewed for the crossover
neighbourhood alone, while the overnight campaign's remaining common-mode
targets sat **outside** it. Layer 1a's *per-driver* linearization EQ is a
separate shipped stage that does reach the emitted graph
([`linearization_fit.py`](../jasper/active_speaker/linearization_fit.py),
`fit_driver_linearization`; the series-1 woofer Peaking filter recorded above
is one of its outputs) — which is why rule 3 below is about *which* of the two
owns a given defect, not about acquiring the first one. Extending correction
across the full trusted band is the scope this stage ratifies, and it is
unbuilt.

**The rules.** Each was measured on hardware on 2026-08-19 and is recorded in
`captures/wired-night-2026-08-19/run-log.md` §§8–9 (session-artifact,
laptop-side). The figures are that night's evidence, not standing constants.

1. **Classify before correcting; EQ only defects.** Every feature is typed
   first — a controls-verified **excess-group-delay minimum-phase test** (with
   positive *and* negative synthetic controls pushed through the identical
   pipeline), a **gate-invariance** check, and **cross-angle behaviour** — and
   only minimum-phase, speaker-own defects are eligible. Interference, beaming,
   and room features are **barred**, the same refusal the correction envelope's
   `spatial_exclusion_limit` term already encodes for the per-driver fit. The
   test earns its keep in both directions: that night it found all nine named
   features **minimum-phase**, which killed the comfortable hypothesis that the
   in-window failures lived in a non-minimum-phase summation zone and
   re-attributed them to rule 2. **Status: the instrument ships** as
   [`crossover_v2/feature_classifier.py`](../jasper/active_speaker/crossover_v2/feature_classifier.py)
   (`jasper-round-views classify-features`), which runs the excess-group-delay test, the
   gate-invariance check against a matched-Q null model, and the timing-scatter
   test over one round's banked captures, and refuses to emit a verdict at all
   unless its known-answer controls pass. It is an OFFLINE run over a banked
   round rather than a stage of one, so a round carries verdicts when somebody
   classified it and `per_bin_minimum_phase_class` is still disclosed as a gap
   when nobody did. What it cannot do is the vertical plane: every capture
   shape it reads is horizontal, so no verdict it emits has ever been sighted
   off that plane — disclosed once, in the evidence packet's `not_evaluated`
   block, rather than a per-verdict flag the boost bar refuses on (the full
   account is in
   [testing-tooling.md](testing-tooling.md#feature-classification-instrument)).
   `PositionalSupport` in
   [`blend_prescription.py`](../jasper/active_speaker/crossover_v2/blend_prescription.py)
   remains the cross-position half for the BLEND class.
2. **Match filter width to feature width.** The three in-window features
   measured natural Q **3.9 / 5.1 / 6.6** (2057 / 1406 / 1037 Hz), each read off
   the pooled 7 ms detrended curve with a ×1.5-narrower companion bracketing it
   — a measured width, not an assumed one. Against those, filters clamped to
   Q 2.0 delivered **28–43% on-target efficiency** (round 17: −1.2 dB nominal
   delivered −0.33 to −0.51 locally), about **3× too wide at the 1037 dip**, and
   the skirt damage to already-below-reference neighbours **exceeded the repair
   at the centre**. That makes the Q clamp **a parameter under suspicion, not
   physics**. Note for whoever sizes the narrow-Q round: **the in-window span is
   the narrow end of the problem, not its extent.** Across all nine named
   features the measured span is **Q 3.9–18.4**, and the six out-of-window ones
   (10.4–18.4) include three reading an identical Q 12.1 and two an identical
   18.4 — repeated values are the signature of the 1/12-octave smoothing floor
   rather than of distinct resonances, so those are lower bounds on how narrow
   the features really are. A ceiling chosen against 6.6 would be too wide for
   most of the band this stage is meant to cover.
   **Status: raising it for cuts is IN FLIGHT.** On `main` the prescriber's
   width bound is `BLEND_FILTER_Q = 2.0` in
   [`blend_correction.py`](../jasper/active_speaker/crossover_v2/blend_correction.py),
   re-exported unchanged into the prescription bounds;
   [PR #2730](https://github.com/jaspercurry/JTS/pull/2730) (open) replaces that
   single re-export with a **sign-split pair** — a wider ceiling for cuts, the
   old value kept for boosts — so read the bound through whichever of the two
   shapes is on `main` when you get here rather than through a symbol name. The
   declared EQ floor stands until a narrow-Q round has actually been measured.
3. **Correct in the branch that owns the defect.** A per-driver defect gets a
   per-driver filter in that branch — Layer 1a's existing per-role stage — and
   the shared stage is reserved for genuinely system-level shaping. A shared
   filter is the wrong instrument for a one-driver problem, and it charges both
   branches for it.
4. **Cuts are bounded and free; boosts pay an evidence bar.** Cuts ride the
   existing caps and the cut-preferred posture unchanged. A boost requires a
   **minimum-phase dip**, **multi-angle testimony**, and an **excursion /
   thermal / harmonic-distortion budget** showing the driver can spend it.
   **The route's design remains an owner decision** — this stage states the bar,
   not the mechanism.
5. **Grade against a FROZEN baseline reference.** Referencing each configuration
   to its own average is exactly invariant to level and therefore **flatters
   broadband cuts**: the cut lowers its own reference too (measured at
   0.4–0.75 dB), so it partially forgives itself, and per-configuration
   target-relative tables inherit the same flattery. Freezing the reference to
   the *baseline* configuration is the honest comparator — under it, apparent
   off-axis improvements reversed into losses. Predictions are **pre-banked in
   the grading view's own units**, and anything that measures worse is **rolled
   back**. Under that comparator every EQ attempt that night lost — a
   deterministic candidate by **+3.3σ**, a one-cut prescription by **+8.2σ**, a
   two-cut by **+15.2σ** — and each was rolled back by the harness rather than
   argued with. **Status: the comparator itself now SHIPS.** `evaluate_flat_spec`
   ([`flat_spec.py`](../jasper/active_speaker/flat_spec.py)) takes an explicit
   `reference_db_override: float | None = None` parameter, threaded through
   [`flat_spec_views.py`](../jasper/active_speaker/flat_spec_views.py)'s
   `_evaluate_position`;
   [`round_views.py`](../jasper/active_speaker/crossover_v2/round_views.py)'s
   `frozen_reference_grade` is the product caller that supplies it, grading a
   target round both shipped (self-referencing) and frozen to a baseline's
   per-position reference levels, operator-facing as `jasper-round-views
   frozen`. The frozen *entry-baseline curve* that `round_evidence.EntryBaseline`
   banks remains a different mechanism and still does not supply it: each side
   of that comparison still self-references its own level.
6. **Respect the audibility floors.** Broad, low-Q deviations are worth
   correcting down to roughly **0.5–1 dB**; a **narrow** feature must be several
   dB before it earns a filter at all; and nothing below the **session noise
   floor** is a target. The floor is measured per session, not assumed — that
   night the only in-window dip was small enough that a *perfect* boost
   predicted **−0.033 to −0.046 dB** on the primary score against a **0.057 dB**
   detection threshold. Correcting it would have been arithmetic, not audio.

**Where the shipped stage and the ratified stage differ.** Materially less is
built than the campaign's fluency suggests, so the count is worth making
reconstructable rather than asserting. The test is **"is there code in the tree
that makes this rule's decision?"** — deliberately not "is there anything
nearby", because something ships beside every one of them, which is exactly how
this stage reads as more finished than it is.

| rule | the decision it has to make | ships? | what ships beside it |
|---|---|---|---|
| 1 classify first | per-bin minimum-phase classification | **partial** | the **reading** ships for the prescribed per-driver class, both signs — every filter is checked against the banked verdicts (nearest decides, sign must match) and the ones no verdict backs are counted onto `prescription.unvouched_filters` ([`driver_prescription.py`](../jasper/active_speaker/crossover_v2/driver_prescription.py), vocabulary in [`feature_classification.py`](../jasper/active_speaker/crossover_v2/feature_classification.py)). It **discloses and does not refuse** since the owner's 2026-08-23 ruling: the vouch is a prediction about whether a filter will help, and a refusal on it cost more than it saved — a role whose incumbent carried a shelf could never keep it (#2863). The **instrument** ships too — [`feature_classifier.py`](../jasper/active_speaker/crossover_v2/feature_classifier.py) / `jasper-round-views classify-features`, controls-gated — but it types detected FEATURES rather than bins, runs OFFLINE over a banked round rather than inside one, and reads only horizontal captures — disclosed once in the evidence packet's `not_evaluated` block, not refused per verdict. `positional_support` remains the cross-position half for the blend boost class |
| 2 width-matched filters | choosing Q from a feature's measured width | **no** | the clamp itself, `BLEND_FILTER_Q = 2.0` — widened for cuts by [PR #2730](https://github.com/jaspercurry/JTS/pull/2730) (merged). A banked feature's `measured_q` is now *reported* to a prescriber in the packet's classification block, but nothing in the tree chooses a Q from it |
| 3 correct in the owning branch | routing a defect to per-driver vs shared | **partial** | ships for the **prescribed** path: the two classes have separate gates, separate bands and separate candidate fields, so a per-driver defect can only reach `linearization` and a region-wide one can only reach `blend_correction` — neither gate can accept the other's filter. The **deterministic** path still makes no such routing decision |
| 4 boosts pay a bar | the **blend-stage** boost route, gated on min-phase + multi-angle + budget | **no**, for the evidence half | what ships on the **per-driver prescribed** class is the SPEND half: a per-role composed budget that bounds the maximum-SPL spend at 13.0 dB, plus the per-filter caps and the crossover-knee bar ([`driver_prescription.py`](../jasper/active_speaker/crossover_v2/driver_prescription.py)) — 5.0 dB until ruling R8 widened the boost caps to 12 dB on 2026-08-22. The EVIDENCE half shipped in [PR #2754](https://github.com/jaspercurry/JTS/pull/2754) and was withdrawn on 2026-08-23: a nearest `defect-boostable` verdict reporting its own `depth_db`, and a boost no deeper than it, are now disclosed rather than required — the owner ruled that a candidate inside the caps may be tested, and NOT ONE row of the 2026-08-19 record carries a depth, so the bar refused every boost that record could have produced. Layer 1a's boost bounds also ship and were exercised in series 1 — `MAX_LINEARIZATION_BOOST_DB`, enforced in `runtime_contract.py`. The **blend** stage's own five-condition bar has still never been exercised |
| 5 frozen reference | a frozen-reference comparator | **yes** | `frozen_reference_grade` in [`round_views.py`](../jasper/active_speaker/crossover_v2/round_views.py), operator-facing as `jasper-round-views frozen` — grades a target round both shipped and frozen to a baseline's per-position references via `flat_spec`'s `reference_db_override` |
| 6 audibility floors | — | n/a | a review discipline, not code either way |

So **two of six now fail the test outright** (2, 4), and **two more ship
only on the prescribed path** (1, 3) — a gate that refuses a bad proposal,
never a stage that derives the right one. Rule 5 now ships outright rather
than only on the prescribed path — `jasper-round-views frozen` grades any
banked round pair, not just a proposal moving through the prescriber. For
rule 2 the campaign produced its verdict with laptop-side analysis and none
of that analysis has been promoted into the product. Rule 4's evidence has a
shipped producer —
[`feature_classifier.py`](../jasper/active_speaker/crossover_v2/feature_classifier.py)
emits a `depth_db` per feature — but it runs OFFLINE over a banked round rather
than inside one, so a round carries a verdict only when somebody classified it,
and since 2026-08-23 nothing on the prescription path gates on either the
verdict or the depth: both are DISCLOSED beside the proposal. Every capture that
producer reads is horizontal, disclosed the same way in the evidence packet's
`not_evaluated` block. So the shipped path to an admitted boost is **open** —
what bounds one is what it costs — and what answers whether a boost HELPS is
where it has always been: the measured round that follows, on the owner's
acceptance protocol for this route (bounded pre-registered probe boost →
all-seats non-regression → near-field spot-check, recorded in
[#2783](https://github.com/jaspercurry/JTS/issues/2783)).

**What "partial" is buying, said plainly**, because the distinction is the
whole of what rules 1, 3 and 4 are worth: a bar that refuses cannot make a round
better, it can only stop one specific way of making it worse. Rule 1's bar
turns "a reader believes this is a driver defect" into "a classifier said so
and the verdict is on the receipt"; it does not classify anything, and a
`defect-*` verdict says only that EQ is not structurally barred there — run-log
§9.2, and every EQ candidate played that night still measured worse.

**Two rulings the prescribed path forced, recorded here because they are stage
decisions rather than module details.**

*The nearest verdict decides (2026-08-19).* Rule 1's bar needs a rule for
matching a filter's centre to a classified feature, and "any eligible verdict
inside the match radius vouches" is the wrong one. Four of the record's nine
features are minimum-phase **dips**, and two of its eight gaps — 0.143 and 0.157
octaves, both peak/dip pairs — are narrower than the radius, so under that rule
a cut aimed squarely at a dip borrowed the neighbouring peak's verdict and was
accepted. Cutting a minimum-phase dip deepens it. The rule is therefore the
ordinary one for a claim about a frequency: **the closest claim owns it**, and
the tolerance's only job is to absorb the evidence's own locating error.

*Merge by role (2026-08-19).* Rule 3 routes a per-driver defect into the
role-keyed Layer-1a field, which then has two producers: the fit writes every
eligible role, a prescription names a subset. Three options, and the ruling is
the third:

| option | what it does | verdict |
|---|---|---|
| replace wholesale | a document's roles become the whole field | **rejected** — a one-role document silently discards the other driver's *fitted* filters |
| compose (append) | prescribed filters added to fitted ones | **rejected** — doubles corrections at a shared target and can breach the eight-filter branch ceiling from two authors, neither of whom sees the total |
| **merge by role** | named roles replace **their own** filters; unnamed roles keep the fit's | **adopted** — the only option under which "a role you do not name is not changed" is true, and it keeps one author per branch so the ceiling has one owner |

The seam implements the merge rather than documenting a protocol for its
caller, and its fit argument is required-and-undefaulted precisely because
forgetting it is the failure that looks like success until somebody measures.

### Provenance, and what this section supersedes

The method in P2 and the rules in P3 were assembled from two dated research
briefs written at the close of the 2026-08-19 campaign —
`RESEARCH-BRIEF-speaker-linearization-2026-08-19.md` and its companion
`RESEARCH-BRIEF-self-referencing-timing-2026-08-19.md`, both session artifacts
living at the repo root of branch `claude/night-driver`, not on `main`. They are
named here for archaeology only and are deliberately **not** linked: they are
branch-scoped, they will age, and **this section is the single source of truth
for the pipeline**. Do not copy their content back in.

This section **supersedes nothing** in the five-layer model, the correction
envelope, the region-based adjustment contract, or Measurement Program v2 — it
orders them. Two entries it does update, both trued up in place above:
decision 12's sequencing (its step (iii) has since run) and
["The prescriber seam"](#the-prescriber-seam-open-decision) (its deferral
condition was exercised and did not resolve as written).

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
(do not restate it here); the relay provider was deleted (ADR-0222) and the
wired (Pi-mic) provider is the seam's occupant.

## Speaker-class applicability (#1671)

Component entry (#1665) declares the class; the class drives which layers'
wizard steps exist. **Landed so far (2026-07-24, the component-entry
slice):** per-driver `driver_class` (compression_horn/soft_dome/metal_dome/
beryllium_diamond_dome/ribbon_amt/unknown — `DRIVER_CLASSES` in
[`jasper/active_speaker/_common.py`](../jasper/active_speaker/_common.py))
feeds the correction-envelope's `class_prior_limit` term, which takes the
declared class and nothing else; the declared in-line pad
(`jasper/active_speaker/driver_pad.py`) feeds the effective-sensitivity
readers (`declared_effective_driver_sensitivities`); and
`radiating_diameter_mm` feeds #1675's ka-beaming crossover hint in `/sound/`.
A fourth declared field, `horn_coverage_deg`, was collected for a Bessel
beamwidth match that was never built; #2872 deleted it, and a waveguide's
identity and rated coverage now travel as operator prose in that driver's
notes. **Still open:** the
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

Phase 0 — read this doc and issues #1666–#1672; JTS3 +
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
historical/crossover-measurement-v2-campaign-record.md's trim-solve section. The
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
crossover-v2 wrong-cal primary scope ·
#1660 room-relay device threading · #1664 worktree hygiene ·
#1665 component entry + pad declarations (schema + pad/class-declaration
slice landed 2026-07-24; JTS3 hardware validation and the research-prefill-
audit portion still open) · #1666 apply promotion · #1667 trim-band bias
(ripple-optimal solve landed 2026-07-24, Phase 3; JTS3 hardware re-verify
still open) · #1668 driver linearization (this doc's Phase 2) · #1669 3-way · #1670
rename · #1671 passive-class UX · #1672 mic HF arbitration. (#1675 ka-beaming
crossover guidance closed 2026-08-08: the `radiating_diameter_mm` hint
shipped, and the beamwidth-vs-horn-coverage match it once contemplated was
never built — #2872 has since deleted the coverage field left waiting for it.)

Opened since, and load-bearing for the 2026-08-17 rulings: #2600 blend-window
instrument blindness · #2603 the driver low-limit's two declared values ·
#2636 the headless lab capture client's revival · #2653 the level datum's
frame-coherence condition · #2662 the capture-source seam. Campaign-wide wave
state lives in [`tuning-master-plan.md`](tuning-master-plan.md) and its
tracking epics, not here (it replaced
[`audio-commissioning-roadmap.md`](historical/audio-commissioning-roadmap.md) whole on
2026-08-21).

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
(15 captures, 13 displayed minutes) and its 9 → 3 stage-1 shape under a
`STAGE1_INCLUDES_LATERAL` flag flip, the ramp's dBFS window and the
calibration reader's lack of a sensitivity term, the declared commissioning
SPL fields, the three N≥3 policy floors, and the pilot pair's duration. The
per-pose ~41.6 s is a representative figure at the display constants, not a
fixed one — it moves with topology — so it is cited as representative rather
than verified. Nothing else in this doc was
re-verified in that pass.

Decision 15 (2026-08-19) opens no new issues. Its two named blockers are
already in the lists above and are reused rather than restated: #2662's
capture-source seam is where the per-driver timed capture mode belongs, and
#2710's per-role alignment quantization sits at the same order as stage P2's
whole timing budget.

The 2026-08-19 pass added decision 15 and the linearization-pipeline section
only, and verified that section's own code claims at HEAD: `design_draft.py`'s
"researched driver facts, no filter compilation" contract,
`driver_safety.py`'s driver-research prompt entry points,
`driver_protection.resolve_driver_low_limit`'s `recommended_highpass_hz` owner
ordering, `crossover_preview.build_crossover_preview`'s bounded-intent
docstring and its `SCHEMA_VERSION = 2` bump for the #2603 collapse,
`measured_candidate.py`'s `declared_geometry_plus_minus_half_period` delay
bound plus `null_walk.geometry_seed_us` / `delay_sweep.sweep_spec`,
`driver_pad.effective_sensitivity_db`, `branch_chain.beaming_onset_hz`,
`gating.f_trusted_floor_hz` (2.5 / 7 ms = 357.14 Hz) with
`flat_spec.BEST_EFFORT_ABOVE_HZ = 16000.0`, and the existence of
`flat_spec.py` / `flat_spec_views.py` and the prescriber intake modules cited
under the prescriber-seam update. The stage P3 measurements and the stage P2
method are cited to dated session artifacts, not re-derived here.

That pass also checked the section's **negative** claims, which is where its
first draft was wrong and had to be corrected before merge: `evaluate_flat_spec`
took no frozen-reference argument (it derived `reference_db` from whichever
curve it was handed) — true on 2026-08-19, false now that `evaluate_flat_spec`
takes an explicit `reference_db_override` consumed by
`round_views.frozen_reference_grade` (rule 5 above carries the current
status); `evidence_packet.py` and `blend_prescription.py` both
disclosed the per-bin minimum-phase instrument as not built, and no
excess-group-delay or gate-invariance symbol existed anywhere in `jasper/`
**at that date** — both were true on 2026-08-19 and neither is now, since
[`feature_classifier.py`](../jasper/active_speaker/crossover_v2/feature_classifier.py)
shipped the instrument (rule 1 above carries the current status);
`BLEND_FILTER_Q` is `2.0`; and at that moment neither the cut-Q raise nor a
Stage-0 timing acceptance test had an open change against it, so **every "IN
FLIGHT" in that first draft was demoted to MISSING**. The campaign's analysis
was laptop-side and had not been promoted into the product, which remains the
single most load-bearing fact about this section's status labels.

**A second pass corrected stage P2's own framing**, and it moved a claim from
MISSING to EXISTS rather than the other way round. An earlier draft described
gap 1 as a missing "per-driver timed capture mode (branch mute plus pilot
injection)". That was wrong on both halves, verified at HEAD: `program.py`'s
`build_measure_program` already interleaves the two drivers non-overlapping in
ONE capture routed by channel (its module docstring is quoted in the stage), so
`DriverResponse.complex_tf`, an exact A/B common time origin, `DriftEstimate`
and `anchor_delay_us` all ship — **at the mark automatically**, since
`spatial.py`'s per-pose replay of that program runs only when an operator
stages an angle walk (`STAGE1_INCLUDES_LATERAL` was `False` at this pass and
is deleted as of 2026-08-22 — no stage-1 plan builds that walk at all, and
`fc_selector.py`, the consumer that walk fed, is deleted with it — and the
cloud walk that runs
on its own is summed-only), a distinction a later pass had to add after this
paragraph first over-claimed it;
and branch muting is not merely unnecessary but *contraindicated*, because
`camilla_yaml.protected_neutral_program_origin` classifies the program origin
only while every commission-mute filter is exactly pass-through
(`{"gain": 0.0, "inverted": False, "mute": False}`). The real residual is the
desync guard's sensitivity floor, pinned as a parametrized boundary in
`tests/test_audio_measurement_program_analysis.py`
(`test_desync_guard_keeps_its_teeth_after_d7`: 4 samples rejected, 2 samples
passed) — 41.7 µs at 48 kHz, 2.1× the relative-phase budget. The timing pilot's
role narrowed accordingly, to that estimator plus any future cross-session
comparison.

**Those labels are time-sensitive, and one turned over inside the hour.**
Rule 2's cut-Q raise went from MISSING back to IN FLIGHT when
[PR #2730](https://github.com/jaspercurry/JTS/pull/2730) opened. That is the
expected behaviour of a status label rather than a defect in one: re-derive any
label here against `main` and the open-PR list before relying on it, which is
all this pass did. The four still marked MISSING were re-checked when #2730 was
found; P2's four gaps had no branch then and have none now.

**One figure was corrected against the primary record.** Stage P3 rule 2's
feature widths are read from the campaign's own null-model block
(`analysis/classify-features.json` → `test2_null_model`, whose spec states the
Q is "MEASURED off the pooled 7 ms detrended curve for each feature, with a
×1.5-narrower companion to bracket it"): **3.9 / 5.1 / 6.6** in-window and
**3.9–18.4** across all nine. An earlier revision of the research brief's
erratum read "natural Q 3.6–6.6", which was wrong at both ends — **3.6 was never
a feature property**, it is the authored Q of a *refused* filter
(`filter_q_out_of_range: filter 0 Q 3.6 is outside 0.5-2`), and the top of that
range ignored the six out-of-window features. The brief was corrected the same
day (`bbd1b7638` on `claude/night-driver`) and now carries its own dated
correction note, so the values there and here agree. The repeated identical
readings above 4 kHz (three at 12.1, two at 18.4) are the 1/12-octave smoothing
floor rather than distinct resonances, so those are lower bounds on narrowness —
which is why rule 2 warns against sizing a Q ceiling against 6.6.

**The `Last verified:` footer below was deliberately NOT bumped**, ten times
now and for one reason: the footer is a whole-document claim, and no pass
re-read the whole document against the code. The 2026-08-18 pass added a
section and trued up two entries it contradicted. The pass that shipped the
per-driver prescription class trued up the P3 table's rules 1, 2 and 3, the
stage's own STATUS count, and recorded the two rulings that change forced —
because that change is what made them stale or newly needed. The pass that
gave the angle walk a session to take (#2732) recorded the (a)-ruling in
Stage P2 and trued up that stage's STATUS line, its lead-in, its fourth bullet
and the per-pose-replay parenthesis below. The pass that opened the per-driver
class's BOOST route trued up rules 1 and 4, the Stage P3 synopsis above them,
and the failing-rule count below. The pass that made a woofer's declared floor
answerable by the chain (#2760) trued up gap 3's status line and its
no-consumer claim, which `crossover_v2/search.py` had falsified since #2739.
The pass that removed the vertical-plane bar on the owner's 2026-08-21 ruling
trued up rule 1's last sentence, the P3 table's rows 1 and 4, and the
paragraph below the table. The pass that deleted the offline search under the
tuning master plan's ruling R1 trued up gap 3, whose two file links this very
change made dead. The pass that deleted `fc_sweep`'s sweep half under that
same ruling (ticket 2.3) trued up the Measurement Program v2 section's
"currently paused" framing, its PR #2717 bullet, and constraint 1's dangling
flag pointer, plus Stage P2's STATUS line and lead-in, its ruling-that-gated-it
and Option (a) paragraphs, its per-angle-per-driver-capture bullet, and the
per-pose-replay parenthesis below. The pass that deleted `horn_coverage_deg`
(#2872) trued up the two places this file called its Bessel-beamwidth consumer
pending: the speaker-class section's landed-so-far list, and the issue
ledger's #1675 entry, which had gone on calling a completed issue open. The
pass that raised the commissioning SPL stop to 85 under the owner's 2026-08-23
ruling trued up the SPL-anchor one-timer's headroom paragraph, which had named
the Epique preset and the preview-staging path as declaring 80, and dropped the
2026-08-18 audit note's "the two 80 dB presets" tail — that pass did verify
those fields, but restating their values is what went stale. Every
one of those was a claim the change in hand falsified; none verified anything
else here. The realized-level demotion
([`measurement-loop-doctrine.md`](measurement-loop-doctrine.md) deviation (i))
trued up the ripple-polish precondition paragraph, which named a 6.0 dB
admission bound that is now the level gate's own 3.0 dB tolerance and called
that gate an arbiter that "fails closed" when it now banks a finding and
proceeds; the "filed for the architect" tail was replaced with the ruling. That
paragraph only — nothing else here was re-verified that pass.

Last verified: 2026-08-24
