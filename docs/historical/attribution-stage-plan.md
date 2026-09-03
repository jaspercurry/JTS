# Attribution stage — work order (issue #1866)

> **Status: historical.** Snapshot as of **2026-08-17**, its last substantive
> revision — not its adoption date: it was adopted 2026-07-29 (round-3
> architect session) and kept taking amendments after that (its M3 row records
> #1999's 2026-08-01 close, its M7 row #2609). Tagged historical 2026-08-22,
> superseded by [`tuning-master-plan.md`](../tuning-master-plan.md), which
> absorbed the open work orders: "P1 (reverse-null probe) → the `verify`
> program; P6/M6 (harmonic extraction) → ticket 1.4; P2 (position variance) →
> tickets 1.2/1.3; WO-7's serial dial-in loop → Loop B's tournament
> mechanics; WO-8 (room line) → Wave 5; the mechanism registry + fix-class
> vocabulary → the per-feature record join (ticket 1.10). The attribution
> package's shipped findings/promotion code is substrate for that join, not a
> parallel system" (that plan's Supersessions section). So the shipped code
> under `jasper/attribution/` stands; the open work orders below are
> **absorbed, not pursued independently from here**. Preserved for
> primary-source archaeology — specific facts (WO ladder state, seam names,
> §4's seed table) will drift over time. Read this for the narrative of the
> work orders, not for current state; the definitions and open-decision
> records the shipped modules cite (§3.1, §5, §6 and §9 among them) remain
> live.
>
> Anchors [issue #1866](https://github.com/jaspercurry/JTS/issues/1866),
> where every owner ruling and prior-art adoption below is recorded. Gate:
> independent adversarial docs review (round 2) → merge; each WO then ships
> through the standard PR gate.
>
> **Composes with — does not re-open —** the two-stage commission flow work
> order ([issue #1806](https://github.com/jaspercurry/JTS/issues/1806) /
> [`two-stage-commission-flow-plan.md`](two-stage-commission-flow-plan.md)),
> which is the chassis this stage renders on; the
> [`llm-native-tuning-workbench-plan.md`](llm-native-tuning-workbench-plan.md),
> then the planning authority for agent-assisted tuning surfaces, itself
> historical since 2026-08-21;
> [`gating-v2-plan.md`](../gating-v2-plan.md); and
> [`room-correction-regime-plan.md`](../room-correction-regime-plan.md).
> **Supersedes: nothing.**
>
> Verbatim research — the two briefs and the owner-run dissertation whose
> Stage 0–4 blueprint is this plan's skeleton — is preserved in
> [`docs/research/2026-07-29-attribution/`](../research/2026-07-29-attribution/README.md).
> Evidence base: the 2026-07-29 Fc forensics (#1867–#1870), the field and
> follow-on issues #1872–#1877, and the 2026-07 measurement corpus as swept
> by WO-0's two reported passes, whose mechanism-frequency table,
> instrument-error catalog, corpus index, and P2/P6 re-analyses are checked in
> beside the research as `04-`…`08-` and are what §4's seed table cites. Bulk
> data (CSVs, WAVs, scripts) stays in the gitignored
> `captures/wo0-retrospective-20260729/`.
>
> **Amended 2026-07-29 (round 4): the reference-library panel findings are
> folded in.** A seven-source reading panel (Toole, McCarthy, Davis/Patronis,
> Yamaha, Klippel, Everest, CLIO/D'Appolito) plus the Q-D bake-off ran on
> 2026-07-29; every adoption, correction, and ruling-pending item is recorded
> on [issue #1866](https://github.com/jaspercurry/JTS/issues/1866) (the
> durable record), with the full working synthesis laptop-side in the
> gitignored `captures/library/` (`panel-synthesis.md`). This amendment
> carries the plan-binding subset: §11 (consensus set, guard ledger, the
> never-launder register), §4's M2/M5/M6 refinements, §5's P3/P6 and
> probe-promotion criterion, §7's guard-disposition rule, and §9's Q-D
> entry — **closed by owner ruling the same evening** with the
> two-listening-profile product decision (§3.5, §9). The panel's posture is unchanged: **authors are
> hypothesis sources, never authorities** — book → hypothesis → corpus/bench
> test → machinery, never book → machinery. The speaker-building school
> (Dickason, 7th ed.) landed the same evening, mid-amendment; its
> plan-relevant deltas are folded into §11's addendum block and the
> affected rows rather than deferred.

## 1. Why (the ruling)

JTS today has strong **instrument diagnosis** (is this measurement
trustworthy: SNR-solved levels, glitch/splice forensics, honesty guards,
excluded-band τ records) and strong **prescription** (fit → predict →
apply → verify). It has almost no **mechanism attribution** — naming which
physics owns a response feature, with evidence and confidence, *before*
prescribing. The costs are already on the books: the flat-spec verdict
pointing at the wrong driver (#1857), VERIFY grading against a model that
reproduces the very defect under investigation (#1868), a reflection
mechanism whose τ the pipeline computes but whose lower model rungs it
structurally cannot see (#1867), and an alignment chain that applies its
delay but cannot yet
defend the number it applied (#1869: the anchor's 20.8 µs integer-sample
argmax quantum — large enough to flip `delay_role` between sessions on the
same hardware — PR #1649 §10's anchor↔snap disagreement gate never
implemented, and an `alignment_confidence` that excludes ±333 µs and is
therefore blind at the ±208 µs carrier lobes). Declared driver spacing
staying inert at `0.0`, and the parallax it would buy, is **#1864's** scope,
not #1869's and not this plan's.

Owner rulings this plan is bound by (recorded 2026-07-29 on #1866):

1. **Data-first.** The registry is seeded by what our own corpus proves
   happens, not by everything prior art says could happen (WO-0 precedes
   design freeze of the mechanism set).
2. **The first pass is a designed experiment.** Small deliberate
   perturbations during the first measurement session multiply diagnostic
   yield at near-zero cost.
3. **Separation of concerns.** Crossover/driver-linearization and room
   correction share measurement infrastructure and attribution *machinery*
   but keep separate targets, frames, verdicts, and household flows.
4. **Open source, no black box.** Every finding, verdict, and prescription
   is inspectable, disclosed, and derived by legible math. The anti-model
   is Trueplay-style silent adaptation.
5. **LLM-ready tooling, no LLM integration.** Deterministic first. The
   tooling a future LLM needs is identical to what the deterministic
   pipeline and a human debugger need: self-describing data and a
   scriptable measure → prescribe → apply → re-measure loop.
6. **Measurement economy** (2026-07-29 afternoon). Minimize captures;
   maximize information per capture. The owner's calibration, recorded
   verbatim on #1866: *"We should be trying to minimize the number of
   measurements and maximize the amount of information we get from
   measurements, but obviously it's a balance. I don't want someone to spend
   30 minutes taking measurements. That's no good, but also, if the
   incremental information you get from taking eight measurements is a really
   big deal, then fine, let's go for it."* So: session wall-clock is a
   first-class budget with a ~30-minute ceiling, and extra measurements are
   admissible when the incremental information is demonstrably large.

## 2. Non-goals (hard)

- No LLM anywhere in the runtime. No "AI mode." Not even a flag.
- No generic diagnosis *framework*. The shape is the repo's shipped
  **registry-of-declarations**: a pure-data mapping from a stable id to a
  spec, read by an engine with zero per-entry knowledge — the
  `REASON_REGISTRY: dict[str, ReasonSpec]` shape in
  [`jasper/active_speaker/crossover_v2_flow.py`](../../jasper/active_speaker/crossover_v2_flow.py).
  It is deliberately **not** the transit provider registry (extensibility
  doctrine pattern 2): that pattern's defining property is that each plugin
  parses its own env from a plain `Mapping`, which attribution has no
  analogue for, and its flattening step *raises* on a duplicate provider id
  — which a library whose whole point is that one mechanism can be selected
  by two lines cannot mirror. Mechanisms land declaration-only or they don't
  land.
- No speculative mechanisms: every v1 registry entry cites a session in our
  corpus, **with its evidence tier** (§4). A seed may rest on a positive
  control rather than an attributed instance — M5 does — but the entry says
  so, because an unlabelled citation reads as adjudicated.
- No polar-map / full-directivity estimation from N=8 clouds (the
  dissertation's confidence on that is low; the window-vs-power *delta*
  binary is in scope).
- No Gunness-style transient-correction filters in v1
  (document-as-physics is the v1 fix class for source-fixed reflections).
- No new hardware requirements. Phone mic + optional calibrated USB mic
  (UMIK-class) remain the only instruments.

## 3. Architecture

### 3.1 Findings are first-class persisted artifacts

A **finding** is the unit of diagnosis: `{mechanism, band, evidence,
confidence, fix_class, household_copy, probes_run, probes_recommended}`.
Findings are persisted (retention model per **Q-C** — ruled 2026-07-29:
**bundle-lifetime**, §9), rendered on the review screen, and
consumed by the prescription stage. The excluded-band τ records are the
embryo: WO-1 ships the promotion path — the carve-out records the cloud
pipeline already persists become findings with mechanism and fix class
attached — and WO-4 extends it with per-mechanism detectors.

**Two vocabularies, one artifact.** `mechanism` is *internal* taxonomy —
it names physics, it may name hardware, and it appears on ops/forensic
surfaces (the finding record, the harness artifact, `jasper-doctor`-class
output, the expert disclosure). `household_copy` stays **phenomenon-level
and hardware-noun-free**, because the shipped prohibition it inherits is
explicit — `_null_classification_copy`'s docstring in
[`crossover_v2_flow.py`](../../jasper/active_speaker/crossover_v2_flow.py) says
of its own two branches: *"No hardware noun appears here … naming one would
be the device-taxonomy guess this program forbids in shipped copy (the JTS3
rim-wave attribution is session knowledge, not measured general truth)."*
Attribution does not overturn that. Whether household copy may ever become
more specific — and on what evidence — is **Q-F**, gated on P4-class
adjudication, not on this plan.

One owner: the attribution module writes findings; the flow and UI read
them. No second computation of the same verdict elsewhere.

### 3.2 Mechanism registry

`jasper/attribution/` owns a pure-data registry plus per-mechanism
detectors, one module per mechanism: each entry declares
`signature(measurement_set) -> evidence | None`,
`discriminating_probes: tuple[ProbeRef, ...]`,
`fix_class`, `confidence(evidence, probes) -> Tier`, and
`household_copy(evidence) -> str`. The engine iterates the registry with
zero per-mechanism knowledge. Crossover-line and room-line each select a
**mechanism set** from the shared library; a mechanism used by both lines
is written once.

`discriminating_probes` is a **per-mechanism, advisory** ordering — "if this
mechanism is unsure, this probe decides it." It is not, and must not become,
a fixed global discriminator chain (see §3.4).

Confidence is a three-word tier, never a bare number in household copy:
`confident / likely / unsure` (Q6 ruling), where `unsure` carries the
single recommended disambiguating probe.

### 3.3 Fix-class vocabulary (closed set, v1)

`delay` · `polarity` · `eq` · `refit` · `carve` · `physical` ·
`document_as_physics` · `measure_differently`.

- `refit` — the fit itself is wrong (band, slope, overlap); re-run the fit
  under different constraints. Distinct from `eq`, which is "add
  correction here."
- `carve` — exclude the band from fitting and/or spec evaluation. This is
  what the pipeline already does with interference-flagged bins; naming it
  keeps a shipped behaviour inside the vocabulary instead of outside it.
- `physical` — placement, geometry, driver, or **operating point** (drive
  level / limiter policy, the M6 case).

Routing rule pinned by test: `eq` is **never** the routed class for a
position-variant null or a source-fixed interference ripple (the Dirac/SBIR
hard rule; the cautionary catalog of #1866). **Cite the physics warrant when
defending this rule, not the psychoacoustic one** (§11.3, X22): the airtight
leg is that energy added into a cancellation is itself cancelled — the null
is an interference zero, not a deficit the drive can fill — stated
independently by four schools across 37 years. The psychoacoustic leg
("narrow interference dips are the least noticeable of all," Buchlein 1962
via Toole) is headphones-only with N and blinding unstated, and must not be
the rule's load-bearing citation. The library also generalizes the rule the
two named `eq` prohibitions are instances of: **only minimum-phase anomalies
are equalizable** (three vocabularies, one gate — §11.1 B2, §11.2 G1); WO-4
dispositions it as a routing-rule generalization.

**Relation to the shipped `ReasonCode`.** `fix_class` is an *internal
routing* field; it is not a second copy vocabulary. Where a finding's
consequence is a per-bin correction limit, it must resolve to the one
shipped closed vocabulary —
[`linearization_envelope.ReasonCode`](../../jasper/active_speaker/linearization_envelope.py),
whose docstring already insists every persisted reason code, wherever
produced, stays self-identifying against that single enum — rather than mint
a parallel one (`carve`, for example, resolves through
`LIMITED_BY_SPATIAL_EXCLUSION`). Pinning that mapping at the copy/envelope
boundary is a WO-4 acceptance item.

### 3.4 Where it sits in the flow

After measurement close, before prescription. In the two-stage flow
([#1806](https://github.com/jaspercurry/JTS/issues/1806) /
[`two-stage-commission-flow-plan.md`](two-stage-commission-flow-plan.md)),
findings render on the **review screen** between "what we measured" and
"the decision" — the refuse-and-recommend-the-probe outcome ("unsure — run
the 30-second polarity test to decide") becomes a first-class session
outcome alongside apply / measure again / leave it as it is. Prescriptions
above the confidence gate proceed exactly as today; below it, the flow
refuses with the named probe instead of guessing.

**Reconciliation with the workbench plan (explicit).**
[`llm-native-tuning-workbench-plan.md`](llm-native-tuning-workbench-plan.md)
was the planning authority for agent-assisted tuning surfaces at this
snapshot, and its §12 explicitly supersedes "a mandatory structured
attribution verdict" and "a fixed diagnostic/discriminator order". This plan
does not reinstate either:

- Findings are **optional evidence artifacts**. The deterministic flow and
  the workbench may consume them; neither is *gated* on a structured
  attribution verdict existing. A session with no findings behaves exactly
  as it does today.
- Probe ordering is **per-mechanism advisory** (§3.2), never a fixed global
  discriminator chain, and never a decision tree from a user's adjective to
  a probe — which is separately forbidden by the workbench plan's §3.3
  ("what must not become executable policy").
- The confidence *gate* on prescription is a deterministic-pipeline policy
  on the deterministic pipeline's own prescriptions. It is not a bar the
  workbench's LLM must clear.

One residual overlap is deliberately left open rather than papered over:
the workbench plan's §5.5 calls its reversible experiment workspace "the one
new mutation owner", and WO-2's harness also mutates the live graph. See
**Q-G**.

### 3.5 Crossover line vs room line (SoC boundary)

Shared: capture/relay/conductor infrastructure, the findings schema, the
mechanism library, the probe primitives, the harness. Per-line: the
mechanism set, the target/frame policy (crossover line: the **adopted
design-axis flat contract** — the dissertation's flat-listening-window Q4
argument was a *proposed* change to it, resolved by **Q-D's 2026-07-29
ruling: the design-axis contract stands, for both listening profiles**.
Room line: the regime plan's boundary/transition framework), the verdict
copy, and the flow surfaces. Neither line imports the other's policy; both
import the library.

**Listening profiles (owner ruling, 2026-07-29 round 4; recorded on
#1866).** JTS serves two household-facing listening profiles — **accurate
at a spot** (desk / studio-monitor use: the speaker is right where you
sit) and **good around the room** (a listening area, not a mark) — as an
**explicit per-speaker-group setting, never auto-guessed**. This is the
product decision §11.3's E14 showed the frame question was downstream of.
The profile is a *policy input consumed above the shared foundation*:

- **Profile-independent (the shared foundation):** the crossover line
  end-to-end — the design-axis flat contract, the gated per-driver
  measurement, the mechanism registry, the carve layer, the two-stage
  flow. A speaker whose direct sound is flat and smooth is the starting
  point both profiles inherit; as scoped today, nothing in T1–T5 or
  WO-1…WO-5 branches on profile (T4's crossover-cloud position prompts
  and ONAX/OFFAX/XOVR role labels serve both profiles unchanged).
- **Profile-steered (the layers above):** the room line's target policy
  (WO-8 / the regime plan), the grading/display frame and its disclosure
  copy (WO-6), the **room line's** measurement-position *guidance* when
  it ships (tight cluster at the seat vs spread over the listening
  area — the crossover cloud remains one profile-independent instrument
  either way), and, later, the preference-tilt layer.
- **Group invariant (owner note, must not be lost):** every speaker bonded
  into one playback group — a stereo pair (wired or wireless), a 2.1 set
  with a subwoofer, any bonded set presenting one soundstage — carries the
  **same** profile declaration. A pair must never be half near-field, half
  room. Whichever rung ships the setting enforces group-level consistency
  and pins it with a test.
- **Sequencing (the 80/20):** the setting ships when its first consumer
  ships (WO-6 grading/display or WO-8 room line, whichever lands first);
  its config ownership is decided then, per the repo's config-ownership
  doctrine. It is recorded now so neither consumer implements against a
  profile-less model.

Two accuracy notes on that boundary:

- [`room-correction-regime-plan.md`](../room-correction-regime-plan.md) is an
  **adopted work order whose RC1–RC5 ladder has not shipped**. Its Schroeder
  helper is documented there as dead in practice (its only caller passes no
  arguments, and nothing measures room volume). The room line consumes that
  plan's decisions as they land, not as if they were live today.
- **"Attribution" is overloaded across the two plans.** The regime plan's
  **D2** ("Attribution: Tier B corrects only what the room added") is *layer*
  attribution — which layer owns a deviation. This plan's attribution is
  *mechanism* attribution — which physics owns a feature. They compose
  (a mechanism finding can tell the layer seam what it is looking at) but
  they are not the same noun; say which one is meant.

## 4. Seed mechanisms (observed-first)

Amended 2026-07-29 by WO-0's two reported passes (§7); every "Observed" cell cites
the corpus. Evidence tier is stated per row, in the retrospective's
vocabulary: **adjudicated** (a discriminating probe was actually run — the
speaker, the mic, or the drive level moved, and the feature responded or
didn't), **corroborating** (consistent with this mechanism *and* with at least
one other; within-session position stability is always this tier),
**model-derived** (computed from the flow's own two-branch model, so it
inherits whatever that model gets wrong — #1868), **refuted**.

*One deliberate extension of the retrospective's definition, stated rather
than made silently:* `adjudicated` also covers **a known intervention applied
and the feature responding** — which is how M7's hand-EQ before/after earns
the tier, and is the same shape WO-7's per-attempt delta evidence will take.
An arithmetic re-derivation from a persisted curve is **not** a probe and does
not earn it (see M4's #1857 row).

| # | Mechanism | Observed (WO-0 corpus, 2026-07-29) | Signature | Probe(s) | Fix class |
|---|---|---|---|---|---|
| M1 | Inter-driver time misalignment at Fc | **model-derived only** — 18/18 alignment estimates in the corpus report a delay+ripple or fail their own confidence floor; 07-29 forensics decompose the 1919 Hz dip as 5.0 dB phase / 0.9 dB slope, **at one position**. **No P1 has ever been run**, so M1 and M3 are separated nowhere in this corpus except by the model #1868 distrusts at exactly this frequency | **Not "position-stable" — but the corpus does not license the stronger claim either.** The dip's centre walks **1425 → 1980 Hz** at noon (32 % about the mean, 39 % between extremes; CV 11.8 %); the morning cloud walks only 1455 → 1969 Hz (10 % spread, **CV 4.3 %** — inside the *unsure* band, neither source-fixed nor position-variant). **The P2 pass classifies the Fc notch `insufficient_evidence` in both sessions**: it clears the 2 dB depth gate at only 7/12 and 5/10 positions. And **2 of the 12 noon positions sit on the search window's 1425 Hz edge**, so the noon spread is a *lower* bound and the pass's own widen-and-re-run trigger fired. Phase-blind sum ≈ flat | Reverse-null (P1) **and** design-axis/vertical-offset (P5) — both **required**, see the routing rule below | `delay` |
| M2 | Source-fixed HF reflection | **Adjudicated once** — S0's three-geometry physical relocation (desk / desk-edge / desk-removed-speaker-on-floor) — **corroborated** by WO-0's per-position pass. Scope the τ figures: 310.4–328.7 µs, median 321.5, is the **desk leg** (n=10, pre-swap horn); across all three geometries the spread is **270.2–342.8 µs**, the ground-plane leg falling outside the desk band at both ends. *Disclosure:* on that ground-plane leg `detect_echo` returned **confidence 0.000**, so its τ values are reconstructed and the grading rule's own arithmetic gives the opposite verdict (catalog row 23 — a `gate` bug, not evidence against the mechanism). WO-0's pass then measured τ = **310 ± 8 µs** (median 310.0, CV **2.13 %**) over 21 hand-screened positions in two sessions on the current horn, rung frequencies at CV **0.6–1.5 %**. Three independent estimators agree: the flow's `arrival_tau_us` 317.8 (+2.5 %), the forensics' 303 (−2.3 %), this pass's 310. Implied ~10.6 cm path ⇒ a horn ≈ **5.3 cm** deep if it is an internal round trip — corroborating the dissertation's arithmetic correction | **Position-invariant HF ripple with a characteristic delay**, not a τ ladder: the two solid rungs' spacing implies 346/337 µs against the 310 µs cepstral estimate, and the shipped detector independently refuses them (`classification: insufficient_evidence`, `reason: no_ladder`). Two-reflection geometry is the next hypothesis. Per-driver sweep (`sweep_t`) localizes it | Rotation (P4) **adjudicates** and would name the reflector; the RB-3 horn swap is a natural experiment whose pre-registered prediction was **opportunistically** confirmed (τ held across the 07-27 swap while r fell 0.373 → ≈0.28 — points at the horn rim) — with the source's own three caveats: it was never run as its own graded pre/post session (the #1859 frame hazard), the 0.175 figure carries #1763's uncalibrated-regime asterisk, and the two sides ran different DSP states. The loopback electrical negative control (r ≈ 0.021) is the corpus's cleanest control; position-variance (P2) corroborates | `document_as_physics` + `carve` |
| M3 | Unfitted-overlap slope error | **model-derived / synthetic — the least-grounded seed.** 0.9 dB of the 07-29 Fc dip (model); #1817's synthetic reconstruction showed the fit attracting +2.379 dB at 1570.6 Hz fitting *through* an LR4 against a flat target. **That described the pipeline as it stood when this row was written (2026-07-29). #1999 closed #1817 (2026-08-01) by giving the fit a crossover-shaped per-branch target (`jasper/active_speaker/branch_target.py`): the identical reconstruction now draws nothing under the shipped fit (residual 2.5759 → 0.0230 dB rms), and the same defect's real-corpus instance — a woofer's +2.8079 dB peak at 0.74·Fc, inside the radiating band — is gone the same way. Kept in the past tense rather than deleted, because the synthetic reconstruction is now evidence of a closed historical defect, not a live pipeline behavior.** Its own signature has never been measured | Reverse-null cannot be driven deep at any delay; broad magnitude error | Reverse-null (P1) | `refit` |
| M4 | Frame mismatch (reference frame generally, incl. window vs power) | **adjudicated (mic and speaker physically moved), and the corpus's largest disagreement.** S0's five-frame spread on **one speaker with one DSP state** reaches **17.5 dB** at 8–16 kHz (desk edge −6.94 … ground plane −24.43 dB) — but read the frames literally: three of the five are physical relocations, up to and including removing the desk and putting the speaker on the floor. Every frame is legitimate; none is "the same setup analysed differently." #1859's 3–7.7 dB cross-session case is *corroborating* only — it names geometry frame as the "leading candidate" and does not exclude a physical change over the intervening ~40 h. #1857's 3.13 dB of intra-verdict frame drag is **reproduced to the digit** from the persisted curve — arithmetic verification, not a probe, so it does not earn the `adjudicated` tier | Same speaker measures differently under different frames; window flat where cloud mean dips (or vice versa) | Design-axis capture vs cloud (P5); the matched-geometry A/B #1859 names | `measure_differently` / `document_as_physics` |
| M5 | Boundary/SBIR (desk bounce) | **Observed**, two sessions (P2 — *corroborating* for SBIR specifically, since position-variance proves "not source-fixed", not "boundary"); **adjudicated** only as a positive control (S0 ground plane); **refuted** for the 1.8 kHz dip. Observed: position-variant LF dips at **735 / 854 Hz** and **1161 / 1166 Hz**, **2.7–4.4 dB** deep, present at **75–100 %** of positions (noon's 1250 Hz candidate is 9/12), CV **15–17 %** — against the source-fixed features' 0.6–1.5 % in the same captures. That factor-of-ten separation is the classifier's whole basis. A first cancellation near 800 Hz implies a ~21 cm direct-vs-reflected path difference, which the geometry model must be checked against. Positive control: S0's ground-plane leg (deliberately created bounce at 125–146 µs, r 0.74–0.93 — the *worst* top-octave reference of the three legs). **Refuted** for the one feature it had previously been invoked on (S0's 1.8 kHz dip mispredicts by 19–70 %) — a mechanism being real does not make every invocation of it right | Position-variant null (CV > 8 % across the cloud); frequency tracks geometry | Position-variance (P2); geometry model; **move-the-speaker probe (the split's adjudicator — bench)** | **Mechanism-conditional (library panel, 2026-07-29 — this split is a detector requirement, not a doctrine choice).** Position-variant **interference null** (narrow, tracks path difference) → `physical`, never `eq`. Boundary **loading** (broad, minimum-phase-ish, tracks solid angle — the radiation-resistance change Toole treats as legitimately EQ-able, pp. 187–188) → `eq` permitted. The blanket `physical` this row shipped with was *stronger than any school*; the M5 detector must distinguish the two before routing, and the discriminating move is physical: shift the speaker relative to the desk and read whether the feature tracks solid angle (loading) or path difference (interference) — a #1870 bench item. Boundary-gain arithmetic in the geometry model **names its model** (§11.3, X1): constant-velocity (+6 dB/boundary) for this small mass-controlled woofer near a desk; Yamaha's +3 dB ladder is constant-power physics for a different regime |
| M6 | Nonlinearity (driver/port, level-dependent) | **Now measured (WO-0 Farina pass). The woofer dominates by ~9 dB *in relative THD* — a frame that must be named:** woofer H3 **−47.0 dB / 0.45 %** at 200–400 Hz vs the tweeter's worst supported band at −56.1 dB, both referred to **each driver's own fundamental**. In *absolute* harmonic level at the microphone the ordering **reverses** — the fundamentals sit ~14 dB apart (−22.1 vs −8.1 dB), putting the tweeter's H3 ~5 dB higher. Naming the frame is not pedantry in a plan whose own instrument-error catalog makes `frame` the dominant failure class. H3 > H2 **in the 200–800 Hz bands where the woofer's distortion is largest** (the symmetric-nonlinearity signature); **the ordering reverses above 800 Hz** (H2 above H3 by 1.8 dB noon, 3.9 dB morning, all supported data). The tweeter's H2 sits at or near the floor in both bands, so **the horn's even-order behaviour is currently unmeasured, not measured-as-low**. Two independent measurement paths (laptop full-range, Pi per-driver) agree **on the woofer's H3** within 1.5–3 dB. The UMIK-2 chain is *not* the sensitivity limit **at the levels that matter here** (the iLoud control resolves 0.02–0.74 % with 6–30 dB of headroom); where the fundamental itself was 10 dB down, H2 became unmeasurable. Separately, one reference-tier P3 negative: identical within 0.12 dB at 10 dB lower drive. **Onset level is NOT derivable from existing data** — the shipped two-level pilot's low leg sits at the harmonic floor (0.1–2.8 dB headroom). **Suggestive, unconfirmed:** the woofer's 200–400 Hz H3 is level-*invariant* in ratio (−0.06 dB/dB, vs +2.0 for a memoryless cubic) while 400–800 Hz grows at +1.45 and 800–1600 Hz at +2.65 dB/dB. That rests on a **clean cross-session lever of only 2.62 dB with up to 1.1 dB of DSP confound** in the bands where the harmonics land — which is exactly why it is a hypothesis. If it survives a deliberate probe, the speaker's largest distortion is a **fixed** mechanism (resonance / port / cabinet) rather than motor nonlinearity *[library inversion, 2026-07-29 — see the fix-class column: Klippel's catalog has **no** small-signal mechanism that produces a level-invariant distortion **ratio**, so the first hypothesis for genuine invariance is a linear feature in the harmonic window, not a fixed acoustic mechanism]* | Order dominance (H3 ⇒ symmetric-**supported**, H2 ⇒ asymmetric — support, not proof: asymmetric nonlinearities inside the feedback loop also generate odd order); frequency shape **in units of `fs`** (`fs` as installed is unrecorded — bench item, with sealed-vs-vented / `fp`); per-order level slope **H2–H5** (soft: slope rises with order; hard limiting: all orders rise at the same rate); ICHD crest screen (< 10 dB smooth / > 10 dB defect — screening line only, calibrate before it drives copy); `C(f,U)` on the fundamental **defines onset** — never a distortion threshold | P3 as a **≥3-leg ladder** (≥12 dB floor, 18 dB / 4 legs target, legs interleaved hi/lo/hi in one capture, one DSP state, full sweep length per leg); P6 with **EHD** (orders 2–5) + ICHD; **one near-field woofer leg at a shared drive level** (tests the linear-artefact hypothesis directly and buys ~20 dB of harmonic headroom — measure the actual floor at the near-field position rather than assuming the gain) | `physical` (operating point) / `carve` when a genuine nonlinearity is confirmed — **but the level-invariance branch is inverted from WO-0's proposal (§11.3, X28)**: a level-invariant *ratio* routes **`measure_differently` first** (no catalogued mechanism produces one below onset; the prime suspect is a linear feature — our own τ ≈ 310 µs source-fixed comb spans a 200–400 Hz fundamental and its 600–1200 Hz H3 at genuinely different points — leaking a level-independent, frequency-dependent bias into the measured ratio, the same `D(N·f₀)/D(f₀)` class that produced the `eqlow` artefact), surviving to `physical` only after **EHD + the near-field leg + a repeat** agree. The deliberate ladder adjudicates: ratio slope ≈ +2 dB/dB ⇒ strain-driven cone/surround nonlinearity, `physical`, "driver character" copy; ratio ≈ 0 after EHD + near-field ⇒ `measure_differently` — a P6 instrument finding, not a property of the speaker. **Must-not-claim** (pinned): suspension-vs-motor from a sweep alone — `Xdc` cannot exist in pressure and HD cannot separate `Kms(x)` from `Bl(x)`, so the honest v1 label is *"displacement-driven driver nonlinearity (suspension or motor)"*; and any audibility verdict |
| M7 | **Inter-driver level-frame error** | **adjudicated** (by the extended definition above — a known intervention was applied and the feature responded) **— the corpus's largest measured defect, and the only mechanism with a before/after listening verdict.** A 7–11 dB dark tweeter; 13.9 dB between the two per-driver fit targets; the trim frame sitting at the bare datasheet sensitivity gap with a −14.4 dB L-pad in circuit; #1667's 1.7–6.3 dB trim bias. An independent hand correction moved every band 300 Hz–16 kHz to within ±0.9 dB of the reference. **The two drivers' realized passband levels were never compared anywhere in the pipeline when this row was written (2026-07-29); THREE comparators have shipped since, and the row is kept in the past tense rather than deleted because they are what the mechanism is now diagnosed BY.** (1) `solve_branch_trims`' mirrored ±1-octave power average per driver, differenced into the trim (`trim_band_average_db`); (2) the fit's per-driver median over the driver's radiating band (`driver_core_level_db` since #1929), both now graded against (0) the summed at-the-mark capture that OWNS the level datum (`summed_level_reference_db`, #2609) — a disagreement past `LEVEL_ESTIMATOR_TOLERANCE_DB` flags the capture retriable and changes no committed number, where until #2609 the two were arbitrated against each other and a 3.0 dB cliff could zero the anchor's per-role offsets; (3) `realized_branch_level_match`, which grades the level the COMMITTED trim actually realizes and hard-refuses past `REALIZED_LEVEL_MATCH_TOLERANCE_DB`. The drift was named by #1870's 2026-07-30 forensic (finding 4, item 4). What the comparators do **not** yet do is WO-4's job — a general per-driver realized-passband detector against a declared-sensitivity prior — but M7 is no longer unreachable: the single-datum-owner migration (#2609) banks (1)-vs-(0) and (2)-vs-(0) disagreement as an M7 finding whenever either estimate sits past tolerance from the summed owner — the finding is advisory, the round proceeds on the owner's placement, and (3) keeps its own independent hard refusal on the pair about to ship (this SUPERSEDES the owner's 2026-07-30 frame-gate ruling on #1866, which was ratified for the era in which two estimators still voted) — which is why M7 is registered ahead of WO-4 (`jasper/attribution/mechanisms.py`, no detector; `promote_level_frame_disagreement`). *[Dickason addendum: the trap is named in 2006 print — pad/trim attenuation computed from broadband datasheet sensitivity instead of the in-passband measured curve gives "a distorted picture" (p. 180, D42) — independent confirmation of exactly the #1667 case]* | One driver's passband sits N dB off the other's against any common anchor; broad and monotonic, **not** an interference notch | Per-driver passband comparison against a declared-sensitivity prior (free, back-catalog) | `eq` (level) — and **`refit` when the level error is upstream in the fit's own frame**, which is the #1667 case: a trim solved by band-averaging inside the woofer's rolloff skirt is not fixed by adding level, it is fixed by re-solving. WO-0 proposed `eq (level)` alone; the second class is this plan's addition, on the M7-vs-M3 distinction below |
| M8 | **Vertical lobing at Fc** | **adjudicated (old horn; re-run owed).** S0 measured the Fc-region dip against mic height: **10.7 dB** at tweeter height (n=6) → **4.1 dB** a hand-width low (n=4) → **1.7 dB** on the ground plane (n=3), while the 8–16 kHz ripple held then *deepened*; Pearson r between the two features = **−0.05** (n=13). Consistent with the noon cloud's 32 % walk in the Fc dip's centre (M1's row), though those are **uncontrolled position clouds, not vertical-angle sweeps**, and the morning cloud is ambiguous — the dissertation's signature is ">~10 % **with vertical angle**," which only P5 actually tests. S0 predates the 07-27 horn swap, so this is a reason to re-run P5 on current hardware, not a finished answer. *[Dickason addendum — magnitude tension, recorded not reconciled: his baffle-position studies put position-driven variation at 1.07–3.07 dB in the 500 Hz–3 kHz band that contains Fc (Table 6.2, D30; the 2–10 kHz tweeter table, D22, shows 1.04–2.41 dB) — a factor of ~3.5–10 below the 10.7 dB measured here; pure baffle diffraction does not obviously explain a defect this size — see §11's addendum block]* | Fc-region dip depth and/or frequency tracks vertical mic offset; **uncorrelated with the HF ripple** | Design-axis / vertical-offset capture (P5) | `physical` / `measure_differently` |

**M1, M3, and M8 are three different mechanisms wearing one symptom.** All
three present as "the dip at Fc," and they route to `delay`, `refit`, and
`physical`/`measure_differently` respectively. Folding them into a single "Fc
dip" entry does not simplify the registry — it guarantees a wrong fix class.
Keeping them separate is the whole point of the stage.

**Routing rule, evidence-backed, pinned by test: a single-position Fc
measurement cannot adjudicate M1.** This rests on the P2 pass's own
conclusion, not on a lobing threshold: WO-0 measured the same speaker's Fc dip
anywhere from **1425 to 1980 Hz** depending only on where the microphone was
(noon: 1425/1425/1430/1684/1723/1775/1783/1783/1973/1978/1980/1980 Hz), and
concluded that a single-position Fc measurement therefore cannot by itself
adjudicate M1. Note what the evidence does *not* say — the pass classifies the
notch `insufficient_evidence` in both sessions, the morning cloud is
ambiguous, and these are uncontrolled position clouds rather than the
vertical-angle sweep the lobing criterion actually calls for. The conclusion
survives all three caveats, because it is a claim about *insufficiency*.
**P1 (reverse-null) and P5 (design-axis / vertical offset) are load-bearing
evidence, not optional, before any `delay` prescription.** This extends the
existing hard rule in spirit: `eq` is never routed for a position-variant
null, and **`delay` fitted from a single position at a feature this unstable
earns the same suspicion**.

Throughout §4 and §5, **"supported"** is the Farina pass's SNR gate — a band
sitting ≥6 dB above its per-order noise floor — not a member of the
evidence-tier vocabulary above. An unsupported band is unmeasured, not
measured-as-zero.

**M7 vs M3 is the same trap one level down.** #1667's trim bias and the 7–11 dB
tweeter deficit are *level* errors in the overlap band, not acoustic-slope
errors. M3 is "the shapes don't sum"; M7 is "the levels were never compared" —
which named the 2026-07-29 state of the pipeline and no longer describes it.
Since the three comparators in M7's row above shipped, the mechanism's live
form is *"the levels are compared and the comparisons do not agree"*: two
estimators of the same inter-driver placement 3.28 dB apart on ordinary
passband tilt, with a closed-loop check saying the shipped pair is level. The
contrast with M3 is unchanged; what changed is that M7 is now detected rather
than merely suspected.

M4's frame mismatch is a *measured evidence* class: it says two frames
disagree and by how much. Whether that disagreement should change the
linearization **fit anchor** is a different question, deliberately not
decided here — see **Q-D**.

**Library refinements to M2's household copy (2026-07-29 panel; §11).**
Three additions, none changing M2's fix class:

- **The desk/horn case sits inside the library's one named hard-case
  exception.** Every school's "reflections are benign" argument carries an
  explicit carve-out for the short-delay, frontal, single-dominant,
  near-field/dead-room reflection — Toole names the console/meter-bridge
  case twice, McCarthy's own guest sidebar names it, Davis/Patronis name it
  three times with a physical fix, and Everest's critical-band criterion
  predicts it (§11.1, A11). Clark 1983 (via Toole, pp. 146–148) is the
  sharpest form: of three combs with near-identical steady-state responses,
  the **source-fixed** one was "greatly degrading" while the room ones were
  benign. So the copy must say **"we can't fix this with EQ" without
  implying "this doesn't matter"** — those are different sentences and only
  the first is supported.
- **The copy gains a severity term.** All four sources agree relative level
  is the parameter that decides comb severity, and it has never been
  reported for our desk bounce or horn comb. Everest's severity ladder
  (−20 dB negligible / −8 dB problems expected / −1 dB maximum; §11.2, G5)
  supplies the scale, and the flow's own reflection coefficient `r` is a
  level ratio, so the reading is computable from the corpus today:
  `20·log₁₀(0.373) = −8.6 dB` (pre-swap horn — exactly at the
  "problems expected" line) and `20·log₁₀(0.28) ≈ −11.1 dB` (post-swap).
  *Binding caveat:* those `r` figures carry #1763's uncalibrated-regime
  asterisk and the two sides ran different DSP states — **a reading to
  verify, not a verdict** (§11.3, E8).
- **Cut-only-peaks is a CANDIDATE, ruling-pending — not adopted.** A
  possible third route between `eq` and `document_as_physics` for
  source-fixed combs: cut the peaks, never fill the nulls. Both proposers
  state its limit themselves — cutting restores nothing at the notch
  frequencies (McCarthy p. 167; Clark 1983's mechanism: every arrival
  carries the same interference pattern, so there simply is little energy
  at the notches). The synthesis reading: **if adopted it is a
  headroom/level policy, not a mechanism fix, and the verdict copy must say
  the first without implying the second.** Awaits an owner ruling on
  #1866; nothing here licenses it.

WO-0 has reported; the v1 set is freezable. M6's fix class is no longer
"provisional" in the round-3 sense — the library panel replaced the open
label with the **two-branch routing in the row above**, which the deliberate
P3 ladder adjudicates (§8). Two honesty bounds ride with M6 into WO-4:

- **Normal-for-class copy.** Our woofer's measured 0.26–0.45 % over
  200–800 Hz overlaps the ≈0.3–1 % (with 400/800 Hz break-up bumps)
  Klippel reports for his own 6-inch woofer in the same band — *frame
  caveat:* his curve is equivalent-input, ours is raw acoustic, so this is
  suggestive of "normal for the class," never a like-for-like match. The
  copy may say "not obviously anomalous for a consumer woofer"; it must not
  claim a calibrated comparison.
- **The tweeter's even-order behaviour is bounded-untestable, not
  tested-negative.** Wave steepening's primary signature is HD2 rising
  ~6 dB/oct with a level dependence, and our tweeter H2 sits at the
  measurement floor in both supported bands. A proper test needs high horn
  SPL, and whether that sits inside the safety envelope is a question for the
  measured SPL stop rather than for a level constant — the −65 dBFS figure
  this bullet used to cite is the naked-tone class default, superseded on the
  proven-high-pass path by the sensitivity derivation. Record
  **bounded-untestable** — the honest tier between "measured low" and
  "unknown."

**Instrument limitation on M6's band (§11.3, E5 — flagged for harness
verification, not asserted).** The 7 ms gate's `1/T` is a **resolution
ceiling at every frequency**, not only a validity floor at the bottom: no
feature narrower than ~143 Hz survives the gated linear analysis anywhere in
the band. Against 1/3-octave spec smoothing (bandwidth `0.2316·f`) the gate
is the binding resolution limit **below ≈617 Hz** and the smoothing above
it. Consequence for M6: a cone break-up peak at 400 Hz with Q > 2.8 — the
strain-driven branch's own predicted signature, in M6's exact band — would
be structurally invisible in the gated linear curve while still producing
harmonics. A gated-curve "no visible resonance" therefore does not
contradict a P6 harmonic finding there.

## 5. Probe primitives

| # | Probe | Cost | Needs owner? |
|---|---|---|---|
| P1 | Reverse-null delay sweep: tweeter-inverted sweeps at candidate delays; null depth at Fc adjudicates (−20 dB by delay alone ⇒ `delay`; can't reach −10 dB ⇒ `refit`; thresholds are practitioner heuristics, in-phase LR only) | 1 sweep per delay | No (fixed mic) |
| P2 | Position-variance classifier: feature-frequency stability across cloud positions (CV < 3 % ⇒ source-fixed, CV > 8 % ⇒ position-variant, between ⇒ unsure). **Corroborating only** for a source claim | Free — *once the harness persists per-position curves*; today it needs raw WAVs off the Pi | No (back catalog) |
| P3 | Two-level linearity — amended by the library panel (2026-07-29): the classifier is the **ratio slope** in dB/dB, not "identical signature." **≥12 dB** span is the floor (the shipped 10 dB in-capture pilot's low leg sits at the harmonic floor, 0.1–2.8 dB headroom, so only its high leg is measurable); the target is a **≥3-leg ladder spanning 18 dB / 4 legs** — two points cannot see the knee that separates soft (slope rises with order) from hard (all orders equal rate) from twisted-curve escapes. Legs interleaved hi/lo/hi in one capture, one DSP state, full sweep length per leg. **The 12 dB pair still works as a clean presence/absence binary even where the low leg is unmeasurable**: low leg still visible at a known floor ⇒ slope ≲ +0.5 ⇒ not a nonlinearity; low leg vanishes into the floor ⇒ slope ≳ +0.9 ⇒ genuinely nonlinear — presence at a measured floor is more robust than grading a sub-dB delta | 2–3 extra sweeps | No (fixed mic) |
| P4 | Rotation: repeat a per-driver sweep (`sweep_w`/`sweep_t`) with the speaker rotated ~30°. **The source-fixed adjudicator** | 1 capture | Yes (rotate speaker) |
| P5 | Design-axis / vertical-offset capture: window-vs-cloud delta at Fc, and the Fc dip's depth vs mic height (M8's discriminator) | 1–2 captures | Once (place mic) |
| P6 | Farina harmonic-IR extraction from the ESS sweeps we already run. **Productizable at zero measurement cost**, with the guards below | Free (reprocessing) — *but the "back catalog" is an operator-enabled 90-file ring, currently full and dropping its oldest capture on every new one, holding roughly two session-days; this pass had **three** MEASURE captures, one of them glitched* | No (back catalog) |
| P7 | Repeat-variance validity gate (Stage 0): time-variant features excluded from attribution | 1 repeat | No |

**P2 cannot name a source, and the shipped instrument says so.**
[`interference_nulls.py`](../../jasper/audio_measurement/interference_nulls.py)'s
module docstring is explicit: *"Position-invariance within one session is
consistent with an origin that travels with the speaker **or** with a path
through the room that did not change while the session ran, and a single
session cannot separate the two — S0 separated them only by physically
moving the speaker."* So P2 corroborates M2 and screens for M5 — it separates
"rides with the speaker" from "walks with the mic" decisively, and that is
genuinely most of the work, but it cannot say *boundary* rather than any
other room path, and it cannot name a reflector. **P4 (rotation) is the
adjudicator** that makes a source-fixed claim. Any finding whose only support
is P2 stays `unsure` with P4 as its recommended probe.

The same discipline binds the *signature* strings. M1's "per-position stable"
is gone from §4 because the corpus contradicts it. M2 keeps
"position-invariant" — but it is earned by S0's three physical geometries and
21 screened positions, **not** by within-session stability, which is exactly
the inference the docstring above forbids. A new mechanism may not borrow that
word on single-session evidence.

**P2's thresholds were validated as non-load-bearing by measurement** (WO-0's
pass, two sessions independently): source-fixed features held at CV
0.58–1.53 %, room features walked at CV 15.2–17.4 %. **Any value in 3–8 %
gives the same answer**, so the exact cut is not the thing to argue about.
Three gates ride with it: **a minimum of ~6 deep positions** — *analyst
judgment, not a measured floor; no n<10 case was analysed* — below which the
CV estimate cannot separate 3 % from 8 %, so refuse and say so; the search
half-width is **policy, recorded with the finding**, with `CV / half-width`
reported so a value near 1 shows the window chose the answer, and an
extremum landing *on* the window edge marks the spread a lower bound and
triggers widen-and-re-run; and **a single high-confidence discordant position
is a finding, not scatter** — one noon position read τ = 121 µs at the set's
strongest cepstral prominence (14.5×), a genuinely different nearby reflector
that the aggregate's `clustered_fraction: 1.0` did not surface. (It may have
been legitimately excluded: the flow's aggregate covered `n_confident` 6 of 8
positions and the accepted-attempt mapping is unrecoverable — which is itself
the §6 requirement.) Note the two senses of *confident* in play: the flow's
own `n_confident`, and WO-0's 21 hand-screened positions, which are **21 of
22** — the 22nd being that outlier. Per-position τ dispersion belongs in the
finding's evidence.

**P6 needs five things the current pipeline does not do.** The first three
are WO-0's own named guards; the fourth is a requirement its results imply;
the fifth is the library panel's addition (2026-07-29) and the highest
value-per-cost item the panel produced. The threshold table is in that
report and is WO-4's direct input, not restated here:

1. **High-pass the capture at `max(120 Hz, 0.8 × f1_segment)` *before*
   deconvolution.** Without it the regularized inverse's huge LF gain maps
   room rumble into a non-causal smear that buries the harmonic impulses
   15–20 dB and the method reports nothing. This is also a latent hazard for
   anything else reading the non-causal part of a deconvolution.
2. **A per-order noise-floor window** — ≥3 same-width windows placed *earlier
   in time* than the harmonic mark (its reverberant tail runs the other way),
   power-averaged. This is the guard the rest lean on: the 6 dB evidence gate,
   the "supported" label, and every "at floor" reading are all defined
   against it, and it is deliberately conservative (for H2 the floor windows
   sit between H2 and H3 and pick up H3's tail).
3. **Consume the existing glitch / ε / alignment gate — do not re-derive
   one.** The 07-28 glitched capture yields a confident, wholly wrong
   1.9–3.2 % THD; the flow had already rejected it.
4. **Handle captures taken through a non-flat applied filter.** Acoustic THD
   carries a `D(N·f₀)/D(f₀)` term, so a shelf that is not flat from `f₀` to
   `N·f₀` moves the measured ratio with no change in the driver (the `eqlow`
   artefact: H3 apparently *rising* 8.6 dB at a *lower* level, excluded with
   reason). Every finding records the applied-filter response at both
   frequencies, and a band that only appears when the DSP boosts it routes
   `measure_differently`.
5. **Report EHD alongside HD, and write findings against EHD** (§11.2, G12).
   Equivalent-input harmonic distortion divides the measured harmonic by the
   system's own small-signal response *at the harmonic frequency*,
   `H(n·jω₁)`, recovered from the same sweep — removing the whole
   `D(N·f₀)/D(f₀)` transfer-path term (applied filter, room magnitude,
   radiation, sensor) at both frequencies. Guard 4's applied-filter record
   is the special case; EHD is the general fix, it is free reprocessing, and
   our own pilot pair shows `H` is level-invariant within 0.2 dB over 10 dB
   so the correction is well-posed. Extract **orders 2–5** from the same
   deconvolution (the per-order slope vector is the soft-vs-hard
   discriminator), and run the **ICHD crest screen** before any mechanism
   claim — with its adaptation caveat: Klippel defines it for a stationary
   sinusoid, our reconstruction from ESS harmonic IRs is a related object,
   so calibrate the 10 dB threshold against a known-good and a
   known-defective capture before it drives any copy (§11.3, X15).

**Phone-mic caveat, carried from the owner's recorded adoption notes
(#1866):** phone-mic AGC and nonlinearity bound both the Farina harmonic
pass (P6) and the level-invariance sensitivity (P3). The UMIK-class lab path
is unaffected. A P3/P6 finding derived from a phone-mic capture must carry
its instrument and must not be graded as if it came from the calibrated
path. Precision the corpus adds: our own "phone series" is an *iPhone plus a
calibrated iMM-6C*, so there is **no built-in-phone-mic sweep in the corpus
at all** — the caveat stays flagged and untested by our data, claimed in
neither direction.

**Probe promotion criterion (owner ruling, 2026-07-29 evening, recorded on
#1866 — binds WO-2/WO-3/WO-4 and the two-stage T-ladder).** A probe enters
the *household* measurement flow only after (a) its information yield is
demonstrated on harness or bench data, and (b) it pays its session-time cost
under ruling 6. Experiments are cheap to try (harness) and expensive to ship
(every household session, forever) — try many, promote few. The flow already
runs two designed experiments — the solo per-driver sweeps and the
8-position cloud — and WO-0 proved their yield is computed then discarded,
so step one of the experiment culture is **harvesting the experiments we
already run** (WO-1's per-position persistence), not adding audio. The
promotion queue, by cost:

1. **Role-labelled cloud positions** (ONAX / OFFAX / XOVR — nearly free; two
   of the existing 8 positions gain deliberate labels so each position
   answers a named question instead of feeding an average; lands with T4's
   position prompts). This **supersedes** the generic height-label idea
   originally recorded for T4 — the McCarthy role vocabulary is the
   stronger form of the same move, and the XOVR role feeds M8.
2. **One deliberate same-spot repeat** (P7's validity floor — the bake-off
   measured the repeat floor at 0.22–0.30 dB, free P7 calibration).
3. **Reverse-null in-flow** (mandatory eventually — §4's routing rule
   forbids `delay` prescriptions without P1 — but it ships only after WO-3
   proves it on the harness).
4. **The ≥12 dB two-level recalibration** with a usable low leg (only after
   the #1870 bench pair demonstrates the yield).

The anti-goal stays: **no speculative captures ship into household sessions
ahead of demonstrated yield** — the observed-first rule the registry lives
under, applied to probes.

**Second admission path — flow-first (owner ruling, 2026-07-30 bench,
~15:30 EDT; recorded on #1866, amends the criterion above).** The owner
(verbatim in substance): *"I know I have that rule about [probes]
demonstrating yield on the bench before [shipping], but sometimes the bench is
on the fly. Keep that framing where we can test something in the flow and
validate that it works happily, and then once it does, we can harden it."*

So the hand-rolled-bench-yield precondition is **no longer the sole gate**. A
probe may instead be built **flow-first**: an experimental, **lab-gated**
capability standing on the production rails the flow already owns
(`SessionVolumePlan`, `excitation_safety_plan` / program admission, the
validated apply path with restore-on-exit, honest self-describing artifacts),
validated live in the flow, and **then** hardened. What prompted it: the
2026-07-30 bench's hand-rolled Phase-4 kit repeatedly re-implemented rails the
product already owns — session volume, level ceilings, safety solves (full
trail on #1870) — and the owner called the shape mid-session. **Flow-first
replaces it.**

Three things are **unchanged**, and they are what keep this an added path
rather than a lowered bar:

- **Every PR** — v0 or hardening — passes the independent adversarial gate to
  0 blockers / 0 should-fixes before merge.
- ***Household* exposure still requires the full promotion bar** above:
  demonstrated yield **and** session-time cost under ruling 6. Flow-first buys
  a lab, not a household.
- **Lab gating is the boundary** that makes the difference safe:
  `JASPER_HARNESS_LAB` or an explicit operator invocation is what lets a v0
  reach a lab box with **zero household surface**.

The two paths differ only in *where the yield evidence comes from* — a
hand-rolled bench run before the code exists, or a lab-gated run of the code
itself. Neither can put an unproven capture in front of a household.

Probe results attach to findings as evidence. In v1 a probe is a **harness
program** (§6), not a new household flow and not a re-arm of the phone
relay — see §6 for why the relay re-arm is explicitly not the carrier.

## 6. Quick-sweep harness (the tooling WO — and the LLM-ready deliverable)

The harness is the loop: *play a known program on the speaker → capture
on an attached mic → analyze → persist a self-describing artifact*, all
scriptable, no phone, no relay, no human mid-loop.

**The carrier, decided.** The harness is an **operator CLI** whose graph
lifecycle is the shape of
[`jasper/bass_extension/bench/activation.py`](../../jasper/bass_extension/bench/activation.py)
— `activate → prove → yield for measurement → restore`, where the
predecessor is snapshotted and fingerprinted first, only the *running*
config is mutated (never the on-disk file, so `reload()` is always a valid
recovery point), and the restore fires on **every** exit including
cancellation, shielded, and is re-proven. That module contributes the
lifecycle; it does **not** contribute config validation (its proofs are
bass-block-specific). So it is **composed with `dsp_apply`-grade validation
before any load**:
[`jasper/dsp_apply.py`](../../jasper/dsp_apply.py) contributes
`validate_camilla_config` — including the `devices.volume_limit` ≤ 0 dB
safety-ceiling refusal — the shared DSP writer lock, and the
re-hash-immediately-before-load check. Note the honest boundary:
`apply_dsp_config`'s own rollback is a *post-load* rollback inside one
transaction against a caller-supplied prior config, **not** a
restore-on-exit rail; the exit rail comes from the activation module. WO-2
owns making these two compose in one operator-facing helper.

**Explicitly not the carrier: `prepare_v2_verify`.** It opens a *new phone
relay session* hosting a 1-entry verify plan, refuses unless a durable
applied state exists, and resolves a fail-closed conductor context — it is a
human-gated, applied-state-gated VERIFY re-arm. §6's requirement is
no-phone / no-relay / no-human-mid-loop, and the two-stage plan already
prices generalizing that machinery as real work, not a parameter.

Requirements (each traces to a friction the 2026-07-29 agent sessions
actually hit — they are the prototype users):

- **Capture side**: calibrated USB mic on the laptop (`sox`/coreaudio)
  or on the Pi (`arecord`); device + cal file declared in the artifact.
- **Playback/config side**: candidate DSP variants (delay, polarity,
  level) are generated and applied only through the composed carrier
  above. Hand-edited YAML is prohibited (a manual delay change invalidates
  the headroom solve — #1870's do-not).
- **Artifacts**: one session dir per harness run; JSON with a
  `schema` field and inline field descriptions (no code-reading needed
  to interpret — the v2-state friction); WAVs alongside; honest labels
  (the #1855 class is a bug here, not a nit — it was *located* and then
  *fixed*: `cloud_measure_program.wav` and `verify_program.wav` were
  byte-identical, so the retention seam took its phase label from the program
  rather than the flow, mislabelling **32 of 45** retained sidecars. #1855
  closed 2026-07-30 — retention now takes its label from the conductor's flow
  phase, not from program identity. The byte-identity that made the confusion
  possible was addressed separately in #2028, which persists the shared summed
  stimulus under its own name, `summed_program.wav`, so that
  `{phase}_program.wav` presence stays a truthful "which phases this bundle
  reached" signal instead of a name two phases both answer to); an index file per corpus
  root listing sessions with one-line summaries (the organization friction).
- **Stable cross-store session identity**: one identifier that survives every
  hop. WO-0 found the corpus split across **four stores that share no
  identifier** — laptop `captures/`, the crossover-v2 bundles, the
  room-correction bundles, and the raw retention ring — with a capture's
  identity living in three unrelated namespaces at once (bundle id, capture
  session id, epoch-microsecond stamp) and **the SHA-256 of the WAV bytes as
  the only reliable join**. Directory mtime is not a fallback: it actively
  misroutes — the ~08:27 morning session lives in bundle `7f54494228cc`, while
  the bundle whose mtime *is* 08:27 (`9445639e508f`) holds the previous
  evening's #1838 run. Content hashing stays the *verifier*; it must stop
  being the *index*.
- **Per-position evidence must survive.** The flow **already computes**
  per-position τ and its confidence — it reports `n_confident: 6`,
  `clustered_fraction: 1.0`, and the household sentence built on them — and
  then **discards the per-position values**, persisting only one aggregate
  512-point curve and a `positions` list of `{attempt, index, position_id}`.
  The feature-stability figures behind §4's amended rows — M2's rung
  frequencies, M5's LF dips, the whole Fc-notch series — had to be rebuilt
  from raw WAVs pulled off the Pi (M1's 18/18 came from the sidecar sweep,
  the 5.0/0.9 dB split from the forensics, S0's τ from the laptop archive).
  So P2 is not actually free today, and
  `clustered_fraction: 1.0` is the summary of a distribution nobody can
  inspect. Required: the **per-position analysed curve** (≥1/12-octave from
  the validity floor up — ~1.5 k floats for a 12-position cloud), the
  **per-position scalars the pipeline already has** (τ, confidence/prominence,
  the gate actually used, ripple), the per-position **WAV path + SHA-256 in
  the state itself** so the state alone is replayable, and the
  **accepted-attempt ↔ position mapping** (today retries are visible only as
  skipped attempt indices in filenames).
- **Artifact ownership and mode posture**: no **root-owned** artifacts on a
  laptop-pull path — that, not the mode bits, was the 2026-07-29 friction.
  The deliberate privacy posture that already exists for raw room audio —
  `captures/` gitignored, directory `0700`, files `0600`, as
  [`testing-tooling.md`](../testing-tooling.md) records for the correction
  capture diagnostic — is **preserved**, not relaxed. Group-readable
  ownership plus the existing modes is the shape.
- **Access**: read surfaces work over plain SSH + file reads; no
  interactive state.
- **Bounded alignment (a trap, pinned by test)**: the MEASURE program repeats
  each driver's sweep **bit-identically three times**, so an *unbounded*
  cross-correlation / GCC-PHAT lag search locks onto the repeat period rather
  than the true arrival. Any alignment the harness performs bounds its lag
  search. WO-0's re-analysis hit this and had to bound it; a second
  resolution trap rode alongside (an 83 µs quefrency grid reported a spurious
  0 % τ spread with every position landing on the same bin — the corrected
  grid is 2.6 µs), so estimator grid resolution is declared in the artifact.
- **Safety**: lab-box gated in v1 (`JASPER_HARNESS_LAB=1` or explicit
  operator invocation); never runs on a speaker whose sources are
  active; measurement-gate acquisition like every other measurement
  path; bounded run time. Per the codify-don't-memorise rule,
  `JASPER_HARNESS_LAB` ships with a `.env.example` line and its prose
  comment block in the same PR that reads it (a WO-2 acceptance item).

**Prior art, and WO-2's delta over it.** Three shipped tools already own
pieces of this and must not be re-implemented:
[`scripts/capture-correction-diagnostic.py`](../../scripts/capture-correction-diagnostic.py)
+ [`analyze-correction-diagnostic.py`](../../scripts/analyze-correction-diagnostic.py)
(laptop-side UMIK capture + manifest + bounded SSH state snapshot + the
privacy posture above — but a passive *observer* of a browser/relay run: it
starts nothing and changes no gain), `jasper-bass-extension-bench` (the
runner that already drives the activation seam above — but bass-block
specific), and `jasper-route-latency-harness` (generate/capture/analyze/run
subcommand shape and schema-versioned artifacts — but a click/latency
producer, not a sweep/response one). WO-2's delta is exactly: an *active*
loop (it commands the DSP change it then measures), over the crossover
sweep programs, with the composed restore-on-every-exit + validated-load
carrier. Adding its entry to
[`docs/testing-tooling.md`](../testing-tooling.md) is a WO-2 acceptance item,
per that doc's own "add it here in the same PR" rule.

This is deliberately *also* the LLM-ready tooling: an agent that can
read the corpus index, run one harness command, and read one JSON is the
target consumer — deterministic pipeline first, smarter agents later,
same door.

The harness is also the **probe-promotion venue** (§5's promotion
criterion): a probe demonstrates its information yield here, on
harness/bench data, before any household session carries it.

## 7. Work-order ladder

> **Sequencing amendment pending (2026-07-31).** This section still owns every
> WO's *definition*, but the 2026-07-31 first-principles panel produced a
> P0–P4 course-correction ladder that reshapes the *order*: rung P0 **adds a
> floor-aware stopping constraint** to WO-7, whose only stopping rule below is
> the attempt budget; P1 gates WO-5's frame-anchor question (Q-E); P2 gates
> WO-4's mechanism freeze (§8 item 3); P3 gates WO-3's delay adoption (Q-A);
> and P1/P3/P4 all precede WO-6. **The ladder is pending owner ratification**,
> so those items are *superseded-pending*: do not execute them as written, and
> do not treat the ladder as decided either.

Every WO: implementation by delegated agent, independent adversarial
review to 0 blockers / 0 should-fixes, behavioral promises pinned by
tests in the same PR, docs-impact run, memory/issue updates.

**Guard disposition (2026-07-29 amendment).** Each WO's PR dispositions the
§11.2 guard candidates assigned to it: **adopted with a pinned test, or
declined with a stated reason — never silently dropped.** Assignment is not
adoption; the ledger exists so no guard dies of being forgotten rather than
being decided. Three ledger rows are owned by venues that are not WOs —
G23 by the #1870 bench, G30/G31 by the bass-extension program — and are
dispositioned in that venue's next session record or contract revision.
Dual-owner rows are dispositioned by the first listed (**bold**) owner to
ship; the later owner inherits the record and may extend it.

- **WO-0 — Corpus retrospective** (multi-agent, read-only; no code).
  **Reported 2026-07-29, both passes; acceptance met.** The five findings
  documents are checked in as
  [`04-mechanism-frequency.md`](../research/2026-07-29-attribution/04-mechanism-frequency.md),
  [`05-instrument-error-catalog.md`](../research/2026-07-29-attribution/05-instrument-error-catalog.md),
  [`06-reanalysis-farina.md`](../research/2026-07-29-attribution/06-reanalysis-farina.md),
  [`07-reanalysis-position-variance.md`](../research/2026-07-29-attribution/07-reanalysis-position-variance.md),
  and [`08-corpus-index.md`](../research/2026-07-29-attribution/08-corpus-index.md);
  bulk data (CSVs, WAVs, scripts, the machine-readable index) stays in the
  gitignored `captures/wo0-retrospective-20260729/`. Every seed-table citation
  above is therefore reviewable in-repo before WO-4's freeze.
  *Pass A* swept every 2026-07 session archive (laptop `captures/` roots +
  on-Pi bundles + retained dumps) and the catalog of corrections that were
  wrong, delivering the mechanism-frequency table (which M's actually appear,
  how often, at what magnitude, at what evidence tier), the instrument-error
  catalog (every case where a shipped verdict misled, with the frame/unit
  cause), and a machine-readable corpus index. *Pass B* ran the two free
  re-analyses — P6 Farina over the retained ESS sweeps and P2
  position-variance over the back-catalog clouds — and returned both the
  measurements and each probe's productization guards.
  §4 above is amended by both; the data-layer requirements they produced are
  in §6, and P2's and P6's thresholds and guards are in §5.
  Two constraints held and stay binding: findings whose only support is P2 are
  **unadjudicated** for a source claim (§5), and every harmonic pass
  **segregates by instrument** — UMIK-class vs consumer vs phone — with the
  14-of-41 unknown-mic bundles excluded from harmonic conclusions entirely.
  **Still owed, and these are bench items, not corpus re-analysis:** a
  deliberate ≥12 dB two-level pair with the DSP held constant (M6's fix class
  hangs on it), M8's re-run on the current horn, and P4 rotation to name M2's
  reflector — all of which belong to the #1870 bench day.
  Acceptance (met): bulk evidence and raw data stay in `captures/`
  (laptop-durable, gitignored); the **findings documents** land in the
  research directory so every downstream citation is reviewable in-repo;
  registry seed list amended (§4). Still open: the summary issue.
- **WO-1 — Findings schema + persistence.** The artifact of §3.1, its
  storage, and the excluded-band promotion path. Acceptance:
  schema test; one golden finding round-trip; provenance marker discipline
  (old bundles without findings readable); the **stable cross-store session
  identity** of §6 (a finding cites its evidence by an id that survives every
  hop, not by content hash or directory mtime — WO-0 proved both of those
  fail); the **per-position evidence** of §6, without which P2 is not the
  free probe §5 calls it; **and Q-C answered in the PR** — the retention
  model is stated,
  implemented, and pinned by a test (a finding must not outlive, or silently
  predecease, the evidence it cites).
  **Read half shipped 2026-07-31** (first-principles panel lens C, CC1): the
  publisher reopens what it wrote through `storage.read_finding_set` — the
  strict reader, citation re-hash included — and projects `household_copy`
  alone into the durable v2 state, which the crossover envelope renders as one
  quiet line on the review and done screens, dated once the record is no longer
  the moment the household is in. Before it, `read_finding_set` had zero
  non-test callers, so #1949's *bank a finding and proceed* reduced in practice
  to *proceed*. Scoped deliberately: the carve-out sets stay recorded and
  unprojected, because their sentence is copied from the carve-out record and
  `carve_outs_by_band` already renders that same fact on the same two screens.
  The rich WO-6 report — mechanism, evidence, confidence, and the two-panel
  visualization — is still WO-6's.
- **WO-2 — Quick-sweep harness** (§6). Acceptance: **one scripted
  apply-capture-restore cycle** end-to-end on JTS3 (the probe program it
  carries is WO-3's business, not WO-2's), with the safety rails tested
  hardware-free (fake camilla + restore-on-every-exit test +
  validation-refuses-a-bad-`volume_limit` test) and one attended hardware
  validation; `JASPER_HARNESS_LAB` codified in `.env.example`; the
  `docs/testing-tooling.md` entry added in the same PR.
- **WO-3 — Reverse-null probe (P1) as a harness program.** Adjudicates
  M1 vs M3 for the cdhorn; cross-checks the owner bench (#1870) —
  agent-runnable once a mic is attached. Acceptance: probe artifact +
  the M1/M3 decision recorded as a finding; and #1869's **three** alignment
  gaps — the 20.8 µs argmax quantum, PR #1649 §10's unimplemented
  anchor↔snap disagreement gate, and `alignment_confidence`'s blindness at
  the ±208 µs carrier lobes — each fixed or explicitly re-ticketed with the
  probe's evidence. **Parallax is #1864's scope and is not touched here**
  (#1869 delegates it there explicitly; do not duplicate that
  implementation). Reachability caveat carried from #1870: the in-model
  optimum sits at +125 µs, which is λ/4 at Fc and therefore **outside PR
  #1649's ±λ/6 snap radius — the shipped selector cannot reach it**. So a
  P1 finding that routes `delay` needs either a named rule extending the
  selector's radius or an explicit operator apply path; that choice is
  **Q-A**, and WO-3 does not adopt a delay value on model evidence alone.
- **WO-4 — Registry + detectors v1** for the frozen seed set: HF-reflection
  detector (M2), alignment detector (M1), frame detector (M4), SBIR geometry
  model (M5), linearity classifier (M6 from P3/P6), level-frame comparator
  (M7 — the GENERAL per-driver realized-passband comparison against a
  declared-sensitivity prior; note the narrower scope since 2026-07-30: three
  level comparators ship and M7 is already **registered**, minted at the frame
  gate under #1866's ruling, so WO-4 adds the detector to an existing id
  rather than seeding the mechanism), vertical-lobing detector (M8, off P5).
  P2's and P6's thresholds,
  gates, and per-order floor construction come from WO-0's two re-analysis
  reports as direct input — do not re-derive them.
  The reflection detector **composes with `detect_echo`, which owns echo
  evidence** — gating-v2's named trap is "No new echo detector … One τ
  admission rule (`usable_echo_estimates`)", and that stands. The sub-4 kHz
  rung decision (#1867) is made **there**, in the one τ owner, not in a
  parallel detector; note that the regime plan's D2 seam reads the null
  registry whose derivation band is that same gated HF echo band (≥4 kHz),
  so lowering the floor is a cross-plan change, not a local one. Note also
  that WO-0's per-position pass and the shipped ladder detector **agree** that
  the rungs do not fit one τ (`reason: no_ladder`); the detector reports a
  characteristic delay with its per-position dispersion, not a ladder.
  Acceptance: per-detector corpus-replay tests against WO-0's labeled
  examples; the no-snake_case household-copy pin extends to findings; the
  household-copy-has-no-hardware-noun pin (§3.1) extends to findings; the
  `fix_class` → `ReasonCode` mapping (§3.3) pinned at the boundary; and the
  single-position-cannot-adjudicate-M1 rule of §4 pinned as a routing test.
  **Added by the 2026-07-29 amendment:** the two cloud-legitimacy gates of
  §11.3 E4 — the **equidistance precondition** (compare per-position
  propagation delays, which the flow already computes, before reading any
  position-pair level difference as axial; its tolerance is policy,
  recorded with the finding per §5's half-width pattern, and WO-4 states
  the value in-PR) and the **two-sided 6 dB OFFAX membership gate** on
  cloud positions — implemented so each cloud position carries its gate
  verdicts into findings. These are independently useful attribution gates
  *and* the preconditions the Q-D ruling names for any later per-profile
  grading-frame re-ask (§9); landing them does not move any frame.
  **Also added, from the Q-D ruling's (c): the carve layer's load-bearing
  status pinned by test** — a test asserting that the carve/exclusion
  layer genuinely removes uncorrectable (position-variant /
  interference-flagged) bands from the **fit path**, because the margin
  against EQ-into-a-null is the carve, not the frame choice. This is a
  new test, distinct from §3.3's already-pinned *routing* rule. WO-4 also
  dispositions its §11.2 guard assignments (G1–G6, G8, G9, G12, G20–G22,
  G24, G25) per §7's disposition rule.
- **WO-5 — Verdict honesty rewires.** #1868 (VERIFY also grades an
  absolute frame at Fc, or emits the "cannot sum flat as designed"
  finding) and #1857 (spec worst-band pointer anchored honestly) —
  both re-implemented as consumers of findings, not parallel math.
  **#1654 is a named dependency of #1868's remedy (a), on WO-0 evidence.**
  VERIFY's graded `tracking_band` is `[2000, 4000]` Hz on every `verify` row
  among the 45 retained sidecars, because `overlap_band_hz` starts at
  `Fc ± 1 octave` and then clamps `lo` **up** to the tweeter's true sweep
  floor — which equals Fc, since the tweeter sweep starts *at* 2000 Hz. The
  measured notch centre is **1919 Hz, 81 Hz below the graded band's floor**.
  **There is a second clamp, and an implementer widening the band needs
  both:** `_analyze_verify` then applies `lo_clamped = max(lo,
  summed.validity_floor_hz)`
  ([`program_analysis.py`](../../jasper/audio_measurement/program_analysis.py)),
  which can only raise the floor further — so widening `overlap_band_hz`
  alone does not reach 1919 Hz at a reflective mic position. So "VERIFY also
  evaluates an absolute frame at Fc" cannot see the defect unless both clamps
  are addressed, and that is #1654, deliberately shelved. WO-5 must therefore
  either revive #1654 (extending its revival trigger to cover this) or ship
  remedy (b) — the named "cannot sum flat as designed" finding — and say
  which, in-PR. Silently implementing (a) over the clamped band would produce
  a verdict that structurally cannot fail on the defect it was built for.
  On #1857, be precise about the cause and the open decision: the issue
  reproduced the shipped verdict exactly from the persisted curve, so this
  is **the spec's own framing, not a decimation or second-computation bug**,
  and *which* anchor the reference frame should use is an explicitly open
  owner decision (**Q-E**), not something WO-5 picks. Whatever the ruling,
  it lands in the spec SSOT with a pinned test, and it must not become a
  second grading instrument beside the two-stage plan's D4 grade-once
  verdict SSOT (grade at PERSIST time at full resolution; every surface
  reads that one stored report).
  Verify-fail copy also gains the **repeatability discriminator**:
  when consecutive verify attempts agree within measurement
  repeatability (2026-07-29 noon session: 3.66 → 3.82 dB across two
  attempts), the mismatch is deterministic — the household copy must
  say the speaker genuinely differs from the prediction and route to
  the real options, never an implied "try again until it passes"
  (owner field finding, #1873).
  #1873 also files an **interim hardening** layer (phone-side
  deterministic-mismatch options, TTL-expiry warning, a reachable re-verify
  entry after a dead session). That layer is structural work the two-stage
  ladder's PR-T3 largely dissolves; what WO-5 carries from #1873 is the
  discriminator and its copy, not the relay-session plumbing.
  (**Landed ahead of WO-5** — the discriminator and its copy shipped on their
  own in the #1873 lane: `VERIFY_REPEAT_FLOOR_DB` in
  [`crossover_v2_flow.py`](../../jasper/active_speaker/crossover_v2_flow.py), the
  `verify_deterministic_mismatch` reason code, and a terminal verdict that ends
  the capture session rather than offering a retry. WO-5 inherits it rather
  than building it; its own remaining scope is #1868 / #1857, unchanged.)
  **Precondition:** #1858 — the persisted `predicted_sum` prior is
  stride-decimated without pre-smoothing and is aliased below ~500 Hz. WO-5
  reads that prior. Either #1858 lands first, or WO-5 states in-PR which
  bands it refuses to draw conclusions from and pins that refusal.
  (**Satisfied** — the fix landed on branch `claude/fix-1858-predicted-sum-aliasing`,
  pending merge: `_decimate_sum` now block-averages instead of stride-picking,
  through the same owner `spec_report_for_predicted_sum` already used to
  grade this curve. WO-5 does not need its own refusal clause for this
  precondition once that branch merges.)
  Acceptance: the 07-22 session replayed now surfaces the notch finding
  it silently passed — note it must be replayed from the laptop archive
  `captures/xover-e0-2026-07-21/capture-dump-archive-20260722/`, since it has
  already rolled off the Pi's 90-file retention ring; a repeated-verify
  fixture routes to deterministic-mismatch copy, not retry.
- **WO-6 — Review-screen report.** GRADE-style three-tier summary +
  per-finding drill-down + plain-words confidence + the
  refuse-and-recommend-probe outcome, landing on the two-stage flow's
  review screen. **PR-T2 owns that screen; WO-6 lands strictly after T2
  ships** — T1 → T2 → T3 is strictly ordered and this is not a race.
  Carry the two-stage plan's premise-7 note: the crossover wizard is
  data-driven off the envelope and gains no `env.screen` switch, and its
  `review` screen id is **not** the room-correction wizard's `review` — the
  two wizards share no screen vocabulary, and grepping
  `env.screen === 'review'` lands in the wrong envelope.
  Same #1858 precondition as WO-5 for anything drawn from the persisted
  prior. Acceptance: conventions tests (escaping, `canonical_page()`, no
  bare numbers as confidence), copy review. If this rung is the first to
  ship the listening-profile setting (§3.5), the bonded-group
  same-declaration invariant lands with a pinned test in the same PR.
- **WO-7 — The dial-in loop** (owner ruling, 2026-07-29 afternoon; #1866).
  The single-shot measure → propose → apply → verify becomes a **bounded
  iterative refinement** riding the two-stage flow's stage-2 sessions:
  - up to **~3 attempts**, then honest "as good as it's gonna get" copy in
    the Q6 vocabulary — never an implied "try again until it passes";
  - **most-aggressive-defensible change first**, then interpret the result:
    delivered-as-predicted ⇒ trim back; under-delivered ⇒ that shortfall is
    *evidence* (a null, compression, or a wrong model — each routes to a
    different fix class);
  - the **predicted-vs-measured delta of every applied change enters the
    findings stream as first-class evidence**. This generalizes the shipped
    [`delta_probe`](../../jasper/active_speaker/delta_probe.py) —
    realized-vs-commanded per-frequency map, four verdicts, three of which
    roll back — from a rollback gate to a per-attempt evidence producer.
    Where an attempt's delta must bound the next attempt's correction depth,
    it lands on the **already-reserved** `ReasonCode.LIMITED_BY_VERIFY_DIVERGENCE`
    — whose docstring reserves it "for the closed-loop verification feedback
    described in the design doc … a later PR." WO-7 is that later PR; it does
    not mint a new code;
  - ruling 6 (measurement economy) binds the loop: attempts are cheap only
    if each one earns its captures;
  - any averaging the loop performs follows the **restart-on-apply**
    contract (§11.2, G11): a fixed number of averages restarted on every
    applied change — otherwise the loop averages across its own change and
    reads the transient as damage. Prescription sequencing when multiple
    findings coexist follows the A6 consensus (§11.1): physical before
    electronic; within the electronic spectral-crossover case,
    level → delay → EQ.
  Research input:
  [`03-brief-iterative-dialin-and-position.md`](../research/2026-07-29-attribution/03-brief-iterative-dialin-and-position.md).
  Related bench work: #1876 (convergence — does a clean-slate re-run reach
  the same tune?) and #1877 (position-aware clouds; acoustic
  time-of-flight first, sensor fusion parked).
  Acceptance: loop state is pinned — the attempt budget is enforced (a
  fourth attempt is structurally unreachable, not merely discouraged), each
  attempt's predicted-vs-measured delta is persisted as evidence, and a
  stopping-copy fixture asserts the honest end-of-loop wording.
  **Sequencing:** WO-7 requires the two-stage chassis (T1–T3). The loop is
  impossible while auto-apply exists and verify is a single trapped attempt.
- **WO-8 — Room-line adoption.** The room-correction line selects its
  mechanism set (M4/M5 + regime-plan mechanisms) and frame policy
  through the same library; explicitly NOT a merge of the flows.
  Acceptance: the SoC boundary test — no cross-line policy import. If
  this rung is the first to ship the listening-profile setting (§3.5),
  the bonded-group same-declaration invariant lands with a pinned test in
  the same PR.

## 8. Sequencing and dependencies

0. **Scheduling gate, satisfied.** #1866's own ruling was that this
   direction is "NOT scheduled ahead of the measure+verify P0 arc." That
   arc closed on 2026-07-29: #1854 (the idle-exit that killed live relay
   sessions) is fixed and merged, and the 2026-07-29 noon session was the
   first in which verify ever returned a verdict end-to-end — which is what
   produced #1873. The gate is met; recording it so the ruling is carried,
   not dropped.
1. WO-0 has reported (both passes, 2026-07-29). What it could not answer
   read-only is now bench work, folded into the #1870 day: the deliberate
   ≥12 dB two-level pair, M8's re-run on the current horn, and P4 rotation.
2. WO-1 → WO-2 → WO-3 are the critical path to the first agent-driven
   answer (the M1/M3 adjudication — which, per WO-0, **no measurement in the
   corpus has ever separated**). The owner bench (#1870 + #1848 + the
   horn-depth measurement) can run before WO-3 exists — the bench is the
   manual version — or after, as its validation.
3. WO-4 can freeze the mechanism set now that WO-0 has reported, with one
   explicit open adjudication carried in the table: **M6's level-invariance
   branch waits on the deliberate P3 ladder** (a #1870 bench item). The
   2026-07-29 amendment replaced the round-3 "provisional" label with the
   two-branch routing in §4's row — and note the interpretation inverted
   (§11.3, X28): a level-invariant-in-ratio result now routes
   `measure_differently` first (a linear feature in the harmonic window,
   i.e. a P6 instrument finding), not "fixed acoustic mechanism ⇒
   `physical`" as WO-0 originally proposed. `physical` survives only after
   EHD + near-field + repeat agree.
4. WO-5 rides on WO-1's findings and WO-4's detectors.
5. WO-6 lands on PR-T2's review screen, strictly after T2 ships. WO-7
   requires the full T1–T3 chassis.

## 9. Open questions (owner decisions, tracked on #1866)

- **Q-A: WO-3 delay adoption path.** If the reverse-null lands a
  high-confidence delay, does it auto-apply through the normal prediction
  gate, or is inter-driver delay an owner-attended apply in v1? Folded in:
  #1870's reachability caveat — the in-model optimum (+125 µs) is outside
  the shipped ±λ/6 snap radius, so "auto-apply" is not even available
  without either a named rule that extends that radius or an explicit
  operator apply path. Whichever is chosen must be the same answer for both.
- **Q-B: harness invocation surface.** Operator CLI only, or also a
  `/tools/`-style lab page? (v1 proposal: CLI only.)
- **Q-C: findings retention — CLOSED by owner ruling 2026-07-29 night
  (recorded on #1866): bundle-lifetime.** A finding lives inside the session
  bundle whose evidence it cites and dies with it, structurally satisfying the
  pinned constraint (a finding must not outlive, or silently predecease, the
  evidence it cites). Longitudinal analysis reads across bundles via WO-1's
  stable cross-store session identity. A summary ledger for long-horizon
  retrospectives is **explicitly deferred** to a later WO, if a future
  WO-0-style sweep needs one. Implemented and pinned by WO-1: findings are
  published as ordinary artifacts in the commissioning evidence bundle, whose
  retention evicts whole directories, so the "must not outlive" half is a
  consequence of the storage shape rather than a retention loop; the "must not
  silently predecease" half is enforced on read, which re-resolves and
  re-hashes every bundle citation and raises rather than returning a finding
  whose support it could not confirm.
- **Q-D: proposed supersession of the design-axis fit anchor — CLOSED by
  owner ruling 2026-07-29 (see the ruling at the end of this entry; the
  body is preserved as the decision record).** The adopted
  canon is that the speaker layer's contract is *flat gated direct sound on
  the **design axis***
  ([`active-speaker-tuning-layers-design.md`](../active-speaker-tuning-layers-design.md)'s
  "top of the table" contract, immediately above its
  do-not-re-litigate list), and that **"the fit stays anchored on the
  design-axis per-driver curves … the recorded trigger for revisiting is
  S3 closed-loop evidence, nothing else"**
  ([`linearization-campaign-2026-07.md`](linearization-campaign-2026-07.md),
  interpretation call A and its risk register). That plan also records that
  an anchor-vs-cloud offset on surviving features is **expected**, bounded
  by the cloud's own `BandSpread` diagnostics, and **not a defect** — so an
  expected listening-window offset is explicitly *not* the trigger.
  The dissertation's Q4 argument (CTA-2034/Olive: the listening window
  predicts preference, so linearization should target flat
  listening-window/direct response, with an LR4 power-mean dip at Fc being
  named physics when the window is flat), #1859's 3–7.7 dB cross-frame
  disagreement, and WO-0's stronger S0 figure (a **17.5 dB** five-frame spread
  at 8–16 kHz on one speaker with one DSP state — three of those five frames
  being physical relocations, up to removing the desk entirely, so it is not a
  same-setup-analysed-differently number; §4/M4) are **inputs to that
  recorded trigger discussion — not replacements for it**; none of the three
  is S3 closed-loop residual evidence, which is what the trigger names. The
  question put to the owner is
  therefore narrow: *does this evidence justify opening the S3 revisit
  early, and if so, what closes it?* Until answered, the design axis stays
  the fit anchor and M4 stays what it is — measured evidence that two frames
  disagree, not a decision to move the anchor.

  **Ruling package delivered and RULED (2026-07-29 evening, round 4;
  recorded on #1866).** The library panel + the Q-D bake-off
  (`captures/qd-bakeoff-20260729/`, laptop-side) produced a one-page ruling
  package. Its evidence highlights: the bake-off's clearest leg shows
  source-fixed features survive **every** aggregation frame (0.33–0.68 dB
  error) while every aggregate under-renders position-variant nulls
  37–50 % — the power-mean renders the Fc notch **6.8× shallower** than the
  deepest position; the two runnable scoring legs invert structurally
  (predicting the mark favours envelope/anchor frames, leave-one-out
  favours central frames in all ten sub-bands — by construction); frames
  agree ≤2 kHz and diverge hard above a **~6 kHz knee**, where the spread
  is **directivity/coverage-driven, not null-driven** (§11.3, E3); and on a
  **one-seat geometry the two schools converge** — McCarthy's own
  equal-AREA weighting collapses to Toole's single-seat rule, so the frame
  question is downstream of a *product* decision about the target function
  (§11.3, E14). **The owner's ruling adopts the package in full:**
  **(a)** the roles split is adopted explicitly — fit = design-axis
  anchor; cloud = attribution instrument + carve + gates; per-position
  variance = a reported diagnostic; level/headroom = an explicit veto —
  approximately what ships today, so the ruling costs nothing; **(b)** the
  grading/display frame does **not** move yet — that question is gated on
  the cloud being gated first (§11.3 E4's precondition chain: equidistance
  → 6 dB OFFAX membership → a legitimate coverage sample), because the
  envelope family's case is strongest exactly where the spread is
  coverage-driven, and an envelope there performs **directivity
  selection** — hiding an aiming finding that routes `physical` behind a
  grading choice. If it moves later it moves **per listening profile
  (§3.5), above the measured divergence knee only**, after role-labelled
  re-measurement — E15's candidate shape ("aggregate below the knee,
  disclose disagreement above it, treat the disagreement as a finding") is
  the input for that re-ask; **(c)** the carve layer's load-bearing status
  is made explicit and tested — the fit frame renders position-variant
  nulls *deepest of all seven frames*, so the carve, not the frame choice,
  is the EQ-into-a-null safety mechanism (a WO-4 acceptance item, §7).
  **And the ruling settles the question the schools could not**: the
  two-listening-profile product decision of §3.5 — including the owner's
  stereo-pair/bonded-group same-declaration invariant — is what resolves
  E14's "whose seat?" residue. **The S3 fit-anchor revisit stays closed
  for both profiles**; its recorded trigger (S3 closed-loop evidence,
  nothing else) is unchanged. The settling measurement (one role-labelled
  cloud session with a summed anchor capture, a same-spot repeat,
  pre/post-apply clouds, and an ear check at the mark and one off-axis
  seat taken *before any chart is shown*) is specified on #1870 — its ear
  check now doubles as the first real-world check of the two-profile
  framing.
- **Q-E: #1857's reference-frame anchor.** Role-anchored (woofer passband),
  passband-weighted, or the current full-range mean with the verdict copy
  naming both the frame and the direction? #1857 states this as an open
  design decision and warns against patching blind. WO-5 implements the
  ruling; it does not make it. **Still open** — but the half of #1857 that
  does not depend on the ruling has landed: `flat_spec.spec_band_tilt` and
  the per-band level/ripple split state the band-to-band relationship the
  frame cannot move, and every graded number is pinned unchanged against
  pre-change values
  ([`tests/test_flat_spec_attribution.py`](../../tests/test_flat_spec_attribution.py)).
  The ruling still owns the anchor; nothing above pre-empts it.
- **Q-F: household-copy specificity.** Today household copy stays
  phenomenon-level with no hardware nouns (§3.1), because a single session
  cannot license a device-taxonomy claim. Once P4-class rotation evidence
  exists, may copy name the mechanism ("the horn's own internal
  reflection") — and under what confidence tier? Owner decision, gated on
  P4-class adjudication landing.
- **Q-G: harness vs the workbench's experiment workspace.** The workbench
  plan's §5.5 declares its reversible experiment workspace "the one new
  mutation owner"; WO-2's harness also mutates the live graph, on a
  different (operator-CLI, no-agent) path. One of them should eventually own
  the mutation seam and the other should call it. Not blocking WO-2 — the
  workbench workspace is unbuilt and WO-2 composes only shipped rails — but
  it must be resolved before both exist.

## 10. Do-nots (astronaut-engineering guardrails)

- No mechanism entry without a corpus citation carrying its evidence tier.
- No probe that isn't consumed by a registry detector or a bench issue.
- No confidence *scores* — tiers only, with the recommended probe.
- No cross-line flow merging under the banner of "shared fundamentals."
- **No new resident daemon.** Attribution runs at analysis time inside the
  process that already owns the analysis; probe analysis may live in an
  operator CLI per §6. What is forbidden is a long-lived attribution
  service, not a command.

## 11. Library panel inputs (2026-07-29)

The plan-binding subset of the seven-source reading panel (Toole *Sound
Reproduction*; McCarthy *Sound Systems: Design & Optimization*;
Davis/Patronis *Sound System Engineering*; Davis/Jones *Yamaha Sound
Reinforcement Handbook*; Klippel *Loudspeaker Nonlinearities*; Everest
*Master Handbook of Acoustics*; Audiomatica/D'Appolito CLIO LF note) plus
the Q-D bake-off run against our own corpus. Durable record:
[issue #1866](https://github.com/jaspercurry/JTS/issues/1866) (every
adoption, correction, and pending item, in thread order). Working synthesis,
per-book digests with per-claim evidence tiers, and the bake-off report live
laptop-side in the gitignored `captures/library/` and
`captures/qd-bakeoff-20260729/` — cited here by book page so every claim is
independently checkable against the primary source. Posture: **authors are
hypothesis sources, never authorities**; the §10 do-nots still bind, and
nothing in this section adds a mechanism without a corpus citation.

**Dickason addendum (7th ed., digested 2026-07-29 late evening — the
speaker-building school, closing the panel at eight sources).** The digest
(source identity-verified, 307 PDF pp; Ch. 3 passive radiators and
Chs. 5–8 read in full; Ch. 0 in part, its advanced-transducer tail
unread; Chs. 1, 2, 4 and 9 opening-pages-or-TOC only; Chs. 10–12 not
read — the digest records per-chapter coverage tiers) moved the two rows
the panel predicted, plus three seed-table touches folded in place. Page
cites are printed folios as read; the printed↔PDF offset is +12 through
early Ch. 6 and +10 from Ch. 7 onward, per the digest's header.

- **X1 (boundary fork): a fourth independent +6 dB source.** The
  single-boundary loading gain is derived twice independently in one
  section — "a doubling of the sound pressure, or 6 dB of gain" for
  full→half-space (p. 207, math-derived; library claim D58). *Conflation
  caveat:* Ch. 6's ~13 dB baffle-size sweep (D26, p. 135) measures a
  different quantity (baffle-step corner-rolloff amplitude across a
  baffle-size sweep, likely folding in edge-diffraction ripple) and is
  not a competing number. Folded into X1 below and G25.
- **The power-frame fork (panel X2) gains a third position**: Dickason
  rejects the "Constant Power Crossover" convention outright for
  same-baffle direct radiators (p. 152; D36 — *opinion-tier*, an argument
  not a derivation), landing on the amplitude-frame side for our
  near-field regime. The panel-synthesis X2 row's re-read is owed
  laptop-side; no plan text rested on the power frame.
- **M7 confirmation, 20 years early**: never compute pad/trim attenuation
  from broadband datasheet sensitivity instead of the in-passband measured
  curve (p. 180; D42) — the exact trap M7's #1667 case names. Noted in
  M7's row.
- **M8 magnitude tension, recorded not reconciled**: Dickason's
  baffle-position studies put position-driven response variation at
  **1.07–3.07 dB in the 500 Hz–3 kHz band that contains Fc** (Table 6.2;
  D30 — the 2–10 kHz tweeter table, D22, shows 1.04–2.41 dB) — a factor
  of ~3.5–10 below S0's measured 10.7 dB Fc-region dip. Pure baffle
  diffraction does not obviously explain a defect that size; readings
  (driver spacing vs wavelength per D40, filter-order vertical tilt per
  D38, or another JTS-specific factor) are checkable once #1864 lands
  real spacing. Noted in M8's row — and it reinforces §4's rule that
  M1/M3/M8 stay separate entries.
- **P1 prior art**: a ≥10 dB polarity-reversal null centred at Fc as a
  design-verification check (p. 189; D50) — corroborates the reverse-null
  approach; explicitly *not* a substitute for P1's delay-sweep
  methodology, and the book has no wrong-delay-vs-wrong-fit separation —
  M1 vs M3 remains our own problem to adjudicate.

### 11.1 Consensus adoption set (16 claims)

**Panel-consensus tiers** — a synthesis-local scale, rendered here as
`C1–C4` to avoid colliding with either §4's corpus evidence tiers
(adjudicated / corroborating / model-derived / refuted) or the two-stage
ladder's PR-T names:

| Tier | Meaning |
|---|---|
| **C1** | Physics-derivable **and** independently derived by ≥2 schools **and** corroborated in our corpus |
| **C2** | ≥2 schools (≥1 from derivation), corpus corroborates |
| **C3** | ≥2 schools, corpus corroborates weakly/partially |
| **C4** | Schools agree; **corpus silent** — adoptable on derivation (math) or nameable as a gap, never as an observation |

Set A is corpus-corroborated; set B is school-consensus/corpus-silent, split
out so nothing is laundered as observed when it is only derived.

**Status — same discipline as §11.2's ledger:** these rows are *inputs
dispositioned by their owner WOs* (§7's rule), not blanket adoptions,
"adoption set" naming the readiness of the input, not its state. Where a
row is already binding, the row or its consumer says so explicitly: A3's
*routing* rule is pinned today (§3.3/§4); A6's prescription ordering and
G11's averaging contract are adopted into WO-7's bullet on the owner's
recorded McCarthy adoptions (#1866).

| # | Claim | Sources (printed pages) | Corpus | Tier | Owner |
|---|---|---|---|---|---|
| A1 | **Comb closed forms**: nulls at `(2n−1)/(2t)`, peaks at `n/t`, spacing `1/t`, first null `1/(2t)` | McCarthy p. 79; Everest pp. 380, 493 (independently, D'Antonio); Davis/Patronis (three chapters, two authors) — four independent derivations | τ = 310 ± 8 µs over 21 screened positions (CV 2.13 %); three estimators within 2.5 % | **C1** | WO-4 (M2) |
| A2 | **Averaging attenuates position-variant features and preserves position-invariant ones** — averaging is itself a mechanism discriminator | Toole pp. 378, 187; Yamaha pp. 254–255; McCarthy p. 434 (the same rule inverted, as a validity scope) | Bake-off: **zero overlap** — CV<3 % features attenuate 2.8–16.1 % under aggregation, everything else 26.5–69.3 % | **C1** | WO-4 (P2 — free on WO-1's persisted curves) |
| A3 | **Never fill a null with EQ** | Toole p. 518; McCarthy pp. 435/437; Davis/Patronis pp. 567, 517–519; Yamaha p. 370 | Shipped carve layer; every aggregate frame under-renders position-variant nulls 37–50 %, so **the carve, not the frame, is the safety mechanism** | **C2** | WO-4 (the *routing* rule is already pinned, warrant note in §3.3; the carve **fit-path** test is new — the Q-D ruling's (c), in WO-4's §7 acceptance) |
| A4 | **Level offset changes null depth, not null frequency** | Everest (figure-structural: attenuation series shares identical null frequencies); Klippel (same algebra one level up) | The 07-27 horn swap: τ held while r fell 0.373 → ≈0.28 — depth moved, frequency did not | **C2** | WO-4 (τ-recovery robustness) |
| A5 | **A single-position measurement cannot characterise a crossover** | McCarthy pp. 92–93, 95 (vertical displacement); Davis/Patronis p. 386 (summation preconditions — the stronger derivation, see E10 note in §11.3); Toole p. 374 (his standard data set *includes* the full vertical orbit — instrument coverage, not a stated agreement; per E10, do not represent Toole as agreeing) | The same speaker's Fc dip measured 1425 → 1980 Hz on mic position alone | **C2** | WO-4 (routing test, already pinned in §4) |
| A6 | **Physical before electronic; EQ last** | Yamaha ×4 (pp. 254, 362, 370 + cost list); McCarthy pp. 307–308, 467; Toole p. 267; Davis/Patronis p. 555 | M7 (the corpus's largest measured defect) was a level-frame error; M5's nulls route `physical` | **C2** | WO-6 / WO-7 routing |
| A7 | **Plot the members alongside the aggregate; never ship the aggregate alone** | Toole (always both, Figs 4.10/13.17); McCarthy p. 433 (optical averaging); Yamaha pp. 109–110 | WO-0: per-position yield computed then discarded; the bake-off's clearest leg depends on having members | **C2** | WO-1 (persistence) / WO-6 (display) |
| A8 | **The gate trade: reflection-freedom is bought with resolution** (`Δf·ΔT ≥ 1`) | Toole pp. 382–383 (the worked 280 Hz failure); McCarthy pp. 226, 445; Davis/Patronis p. 198; CLIO's worked `f_min = 1/T` | The 143 Hz floor; per-capture gates 2.1–2.3 ms in the bake-off's own method table | **C2** | WO-2 (artifact declares gate + resolution; see E5) |
| A9 | **Verification before calibration; self-verification first** — prove the finding is in the system, not the instrument | McCarthy pp. 401–402; Davis/Patronis pp. 570, 200; Yamaha p. 312; Klippel p. 23 (contamination list) | WO-0's instrument-error catalog **is** the corpus form: #1855, #1857, #1858, catalog row 23 | **C2** | WO-2 (Stage-0 gate) |
| A10 | **Comparative beats absolute** — reduce the question to a difference under identical conditions | McCarthy p. 416; Davis/Patronis pp. 574, 445; Klippel (EHD/compression are ratios) | Loopback electrical control (r ≈ 0.021); `delta_probe` ships the shape | **C2** | WO-7 (delta-as-evidence) |
| A11 | **The short-delay frontal single-dominant near-field reflection is the named exception to every "reflections are benign" argument — and the desk bounce sits inside it** | Toole pp. 144, 149, 154, 146–148 (Clark 1983); McCarthy pp. 472–473 (Hodas sidebar) + critical-band arithmetic pp. 165–167; Davis/Patronis pp. 226, 244; Everest pp. 370–374 | M2 adjudicated once (S0's three-geometry relocation); τ ⇒ ~10.6 cm path. **Partial: the reflection's level term is unreported** (see E8) | **C2** (level leg partial) | WO-4 (M2/M5 copy) / WO-6 |
| A12 | **In-room downward tilt is a consequence of directivity, not an independent target; the −10 dB house curve is a far-field artefact** (at ~10 ft the right figure is 2–3 dB) | Toole pp. 465, 389 (derives slope from rising DI; fixed targets are "the guessing game"); Davis/Patronis pp. 574–575 | The "linearization was 10 dB dark" verdict; the JTS3 finding that HF rolloff is horn CD behaviour | **C3** | Preference/tilt layer; WO-8 |
| B1 | **Two-arrival summation algebra**: ±120° break-even; ripple bounds from level offset (4 dB ⇒ ±6 dB ripple; 10 dB ⇒ 6 dB total; 12 dB ⇒ negligible) | McCarthy pp. 66, 71–73 (re-derived in the library's verification pass); Davis/Patronis p. 242 (independently tabulated, 0.00 dB at 120°); Yamaha pp. 3–4; Everest p. 382 | Math tier — zone machinery unshipped, nothing measures it | **C4** (adopt on derivation; **two ripple constants**, G24) | WO-4 |
| B2 | **Only minimum-phase anomalies are equalizable** — the general rule behind §3.3's two named `eq` prohibitions | Davis/Patronis pp. 513–515, 567 (derived twice, two authorial voices); Toole p. 382; McCarthy p. 389 (coherence form: "EQ applies only to stable data") | **No probe measures phase class today.** Cheap derived view exists: Nyquist origin-encirclement from complex TFs we already compute (G2) | **C4** (adopt on derivation; the *evidence* is a gap) | WO-4 |
| B3 | **Tolerance must be bandwidth/Q-aware; a scalar dB budget is not a perceptual criterion** | Toole pp. 447, 450; McCarthy pp. 166–167 (critical bandwidth ≈ 1/6 oct, cited to Everest); Everest pp. 371–374; Davis/Patronis p. 205 | Our grading bands are scalar today — a named gap | **C4** | WO-4 grading bands / WO-5 |
| B4 | **EQ's benefit region is bounded and must be disclosed — with a filter-kind switch** (the proportionality is ripple-only; spectral tilt is "practically unlimited") | McCarthy pp. 459–460 (the 40-filter experiment); Davis/Patronis pp. 517–519 (the same experiment in the Laplace domain); Toole pp. 188, 518 | We disclose nothing today — a named gap | **C4** | WO-4 / WO-6 honesty guard |

### 11.2 Guard-candidates ledger (31)

**None is adopted by this table.** §7's disposition rule is the binding:
each owning WO adopts with a pinned test or declines with a stated reason.
A **bold** owner is the primary disposition venue (dual-owner tie-break in
§7). Status: `new` = no JTS analogue; `partial` = shipped in some form;
`correction` = fixes an already-recorded adoption.

| # | Guard | Source (printed pages) | Status | Owner |
|---|---|---|---|---|
| G1 | **Minimum-phase gate on `eq`** — an `eq` route carries minimum-phase evidence or discloses its absence | D/P pp. 513–515, 567; Toole p. 382; McCarthy p. 389 | new | **WO-4** |
| G2 | **Nyquist origin-encirclement** as the cheap phase-class test (also catches mis-selected arrival time) | D/P p. 200 | new (derived view, no new capture) | **WO-4** / WO-2 |
| G3 | **EQ-authority ceiling**: > 3 dB per ⅓-octave, or slopes > 18 dB/oct ⇒ route `physical` ("replace the component") — adopted as *routing* only; any per-bin correction clamp still resolves through the shipped `linearization_envelope.ReasonCode` (§3.3) rather than re-litigating envelope ceilings | D/P p. 568 | new — **would have fired on the 7–11 dB dark tweeter and the 10 dB-dark linearization** | **WO-4** |
| G4 | **Benefit-region disclosure with a filter-kind switch** — ripple filters get a disclosed region; broadband tilt does not | McCarthy p. 459; D/P pp. 517–519 | correction (to the recorded #1866 adoption, which lacked the switch) | **WO-4** / WO-6 |
| G5 | **Reflection-severity ladder** — −20 dB negligible / −8 dB problems expected / −1 dB maximum | Everest Table 17-2, pp. 376–377 | new — computable from the corpus today (E8) | **WO-4** (M2/M5 copy) |
| G6 | **"First significant reflection"** = within 6 dB of the highest-level reflection (defined for the no-reverberant-field case we are in) | D/P p. 240 | new | WO-4 |
| G7 | **Resolution honesty** — never claim phase/delay finer than the time resolution behind it (20 µs ⇒ ±72° at 20 kHz) | McCarthy **pp. 417–418** (not p. 466 — X5) | partial (#1869/#1870 carry it with the wrong page cite) | **WO-3** |
| G8 | **Equidistance precondition** on any position-pair level comparison — unequal propagation delay ⇒ the level difference is distance-contaminated, not axial | McCarthy p. 446 | new — free; per-position µs delays already computed | **WO-4** (gates Q-D, §9) |
| G9 | **6 dB OFFAX membership gate** on cloud positions, two-sided (also flags OFFAX approaching unity) | McCarthy pp. 449 (×2), 442 | partial (adopted as a routing rule; never applied to cloud membership) | **WO-4** / WO-1 |
| G10 | **Coherence gates**: (a) EQ only on stable data; (b) stable-low vs unstable-low discriminator (deterministic summation vs contamination); (c) coherence collapse tracking frequency = uncompensated delay — a free instrument self-check | McCarthy pp. 387–391 | new (JTS has no coherence surface) | **WO-2** (Stage-0), WO-5 |
| G11 | **Restart-on-apply averaging semantics** — fixed averages, restarted on change, for systems where change is expected | McCarthy pp. 370–372, 391 | new — the iterate loop's averaging contract | **WO-7** |
| G12 | **EHD before HD** — divide by `H(n·jω₁)` from the same sweep; removes the `D(N·f₀)/D(f₀)` transfer-path error at both frequencies | Klippel Eq. 12 p. 24, p. 66 | new — free reprocessing, the panel's highest value/cost item | **WO-4** (M6; lands in §5's P6 guard list) |
| G13 | **Verify constancy, not absolute response** — mic self-verification is circular; verifiable things are mic-to-mic matching and constancy under reproducible conditions | McCarthy p. 419 | new — the principled phone-mic trust answer | WO-2 |
| G14 | **Self-verification as a stage** — prove findings are in the system, not the tool (test the generator's THD before attributing THD) | McCarthy pp. 402, 408 | partial (done once as the loopback control; not a stage) | **WO-2** |
| G15 | **Per-bin amplitude thresholding** — gate bins without sufficient excitation rather than reporting them | McCarthy p. 372 | new — required for any music-sourced capture | WO-2 |
| G16 | **RMS-vs-vector averaging audit** — complex-trace RMS discards the phase discrimination separating causal from non-causal energy | McCarthy p. 370 | new (audit item) | WO-2 |
| G17 | **Post-divider reference trap** — a reference tapped after the crossover divides it out ("a low-passed electrical reference can make a subwoofer appear flat to 10 kHz"); declare the reference tap point in the harness schema | McCarthy p. 429 | new | **WO-2** |
| G18 | **Pull-away test + the two far-field criteria** (≥1λ at the lowest frequency of interest **and** ≥10× the source's longest dimension) — converts M4 from "frames disagree" into "which side of the near-field boundary we are on" | D/P pp. 444–445 | new — the library's cheapest high-yield probe; pairs with P5; harness-first per §5's promotion criterion | **WO-2** / WO-3 |
| G19 | **Probe by intervention** — apply a known change; a shortfall from the predicted move *is* the evidence | McCarthy pp. 460–461 (knob probe); D/P p. 574 (persistence probe); Klippel (level ladder) — three independent antecedents | partial (`delta_probe` ships the shape) | **WO-7** |
| G20 | **Ground-plane +6 dB coupling correction** — cross-leg level comparisons correct for it; plane size sets the LF limit | McCarthy p. 444 | partial (recorded; M4's five-frame table is the consumer) | WO-4 |
| G21 | **Onset defined on the fundamental** (`C(f,U)`), never on a distortion threshold; per-order slopes H2–H5 | Klippel pp. 32–33 | new — H4/H5 free from the same deconvolution | WO-4 (M6) |
| G22 | **ICHD crest-factor defect screen** (< 10 dB smooth / > 10 dB defect) before any mechanism claim | Klippel pp. 25–26, 54 | new — calibrate before it drives copy (X15) | WO-4 (M6) |
| G23 | **Near-field capture for distortion SNR**, then predict the far field from the small-signal `H(jω)` | Klippel p. 66 | new | #1870 bench |
| G24 | **Two ripple constants, not one** — the 4 dB zone boundary derives at ±6 dB ripple (12 dB total), the 10 dB boundary at 6 dB total (±3); a 2× relation, not one constant | McCarthy (library verification pass, §6 of its record) | **correction** (to the recorded "ONE named constant" adoption on #1866) | **WO-4** |
| G25 | **Name the boundary-loading model and quantity (SPL vs power)** — +3 dB/boundary (constant-power) vs +6 dB/boundary (constant-velocity); the small mass-controlled woofer near a desk takes the constant-velocity SPL numbers | Yamaha pp. 229–230 vs corpus/McCarthy/CLIO/Dickason p. 207; Toole p. 185 states the quantity split (X1) | **correction**, doc-level (no code hard-codes either yet) | WO-4 (M5) |
| G26 | **Instrument-noise-floor honesty** — never report a number the instrument cannot resolve | Yamaha p. 312 | partial (the honesty machinery's ethos, with a citable 1989 statement) | WO-5 |
| G27 | **"±3 dB means a 6 dB window"** — tolerance-vocabulary units guard | Yamaha p. 230 | new | WO-5 |
| G28 | **Modal Q bound from physics**: mode bandwidth = `2.2/RT60` ⇒ modal `Q = f₀·RT60/2.2` — input for the regime plan's "shape-plausible as modal" bound | Everest p. 337 | new | **WO-8** |
| G29 | **`4·F2` sanity bound on the Tier-B 1 kHz ceiling** — in a small dead room `4·F2` can land below 1 kHz, putting Tier B in the ray-acoustics region where there is no room trend to correct | Everest pp. 323–326 | new (cross-check) | **WO-8** |
| G30 | **Impedance cross-check on `f_B`** — port tuning from both the woofer-nearfield minimum and the impedance saddle; disagreement ⇒ suspect woofer/port crosstalk | CLIO/D'Appolito pp. 15–16 | new | Bass-extension program |
| G31 | **Half-space near-field error bound: up to 6 dB** — near-field level is *shape*, not ground truth | CLIO/D'Appolito pp. 4–5, 17 | new (bound) | Bass-extension program |

### 11.3 Never-launder register (binding excerpts)

The full 28-entry contradictions register lives with the synthesis
(laptop-side) and on #1866. These entries bind plan consumers directly:

- **X1 — the boundary-loading fork is two correct models, not an erratum.**
  Yamaha's +3/+6/+9 dB ladder derives from a constant-total-**power**
  source; the +6 dB-per-boundary convention (our bass-extension research,
  McCarthy's ground-plane note, CLIO's ≤6 dB half-space bound, and — added
  by the Dickason addendum — Dickason's twice-derived single-boundary
  "doubling of the sound pressure, or 6 dB" at p. 207 — **four**
  independent sources) is the constant-**velocity** result for a small
  mass-controlled woofer. Toole states the fork's *quantity* distinction
  directly (p. 185, math tier): halving the solid angle raises **SPL by
  6 dB but sound power by 3 dB** — "this is not correct" aimed squarely
  at the industry's +3 dB-SPL folklore, valid at wavelengths long against
  the source size **and the source-to-boundary separation**, with no
  other reflectors. So a "+3 dB" figure can be a
  different *model* (constant-power) **or** a different *quantity* (sound
  power), and the rule covers both: **any boundary-gain figure names its
  model and its quantity (SPL vs power).** Our desk/small-woofer regime
  takes the constant-velocity SPL numbers. Dickason is silent on the
  multi-boundary cascade, and his ~13 dB baffle-size sweep is a different
  quantity again — do not conflate (§11's addendum block). No code
  hard-codes any of these today; this rule exists so none inherits the
  wrong one.
- **X4 — McCarthy's printed averaging example is arithmetically wrong.**
  The book prints `(5 + 5.5 + 6 − 19.2)/4 = −0.4`; the correct average is
  **−0.675 dB** (the −19.2 itself is exact). **Never carry −0.4 into JTS
  code, tests, docs, or fixtures.**
- **X5 — the resolution-rule page cite propagated wrong.** #1869 and #1870
  cite the 20 µs ⇒ ±72°-at-20-kHz rule as McCarthy p. 466; it is
  **pp. 417–418**. Note the opposite valence on p. 22: 20 µs is fine as a
  delay-*setting* increment and hazardous as an *analysis* quantum.
- **X15 — Klippel's `ICHD > 10 dB` threshold is a screening line, not a
  decision boundary.** One round number, one illustrated instance, no
  distribution behind it. Calibrate against a known-good and a
  known-defective capture before it drives any household copy. Consumed
  by §5's P6 guard 5 and by G22.
- **X22 — never-fill-nulls has two warrants of unequal strength.** The
  physics warrant (energy added into a cancellation is itself cancelled) is
  airtight and multiply derived; the psychoacoustic warrant (Buchlein 1962)
  is headphones-only, N and blinding unstated, and uses the inverted-shape
  stimulus Toole criticises on the same page. **Cite the physics** (§3.3).
- **X28 — Klippel inverts a WO-0 routing decision.** No catalogued
  small-signal mechanism produces a level-invariant distortion *ratio*, so
  "level-invariant ⇒ fixed mechanism ⇒ `physical`" is wrong as a first
  hypothesis: the first hypothesis is **`measure_differently`** (a linear
  feature in the harmonic window), surviving to `physical` only after EHD +
  near-field + repeat. Folded into §4's M6 row and §8.
- **E10 (attribution note)** — cite the "one measurement can't validate a
  crossover" rule to McCarthy (vertical-displacement geometry) **and**
  Davis/Patronis (summation preconditions — the stronger form, bounding the
  valid region before any misalignment exists). **Toole is silent on it; do
  not represent him as agreeing.**

Emergent cross-book findings this amendment leans on (full set on #1866):

- **E3 — above ~6 kHz our 8-position cloud is an ANGLE cloud, not a
  position cloud**: midband level spread 3.2–3.5 dB, HF-minus-mid tilt
  spread 10.98–29.91 dB. In that band the applicable doctrine is Toole's
  off-axis rule (smoothness, not flatness) rather than summation doctrine.
- **E4 — three stacked preconditions gate the Q-D grading question, and
  none exists in JTS yet**: equidistance (free from persisted delays) →
  OFFAX level differences attributable to axial loss → the 6 dB OFFAX rule
  applicable → the cloud is a legitimate coverage sample. Folded into WO-4's
  acceptance (§7) and Q-D (§9).
- **E5 — `1/T` is a resolution ceiling at every frequency, not only a
  validity floor**: with the 7 ms gate and 1/3-octave smoothing the gate
  binds below ≈617 Hz. Folded under §4's table (M6 instrument limitation)
  and A8's owner row.
- **E8 — the desk/horn reflection's severity is computable today**: the
  flow's `r` is a level ratio; 0.373 ⇒ −8.6 dB sits exactly at Everest's
  "problems expected" line (pre-swap), 0.28 ⇒ −11.1 dB post-swap — carrying
  #1763's uncalibrated-regime asterisk. A reading to verify, not a verdict.
- **E14 — on a one-seat geometry the two schools converge** (McCarthy's
  equal-AREA weighting collapses to Toole's single-seat rule): the Q-D
  school conflict is a multi-seat artefact; the frame question is
  downstream of a product decision about the target function.
- **E15 — "aggregate below the divergence knee; disclose disagreement above
  it, and treat the disagreement itself as a finding"** — Yamaha 1989 +
  McCarthy's zone-validity scope + the bake-off's measured ~6 kHz knee,
  three independent statements of one candidate rule. **Pending** — an
  input to Q-D(b), not an adoption.

---

Last verified: 2026-07-29
