# Attribution stage — work order (issue #1866)

> **Status: adopted work order (2026-07-29, round-3 architect session).**
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
> which stays the planning authority for agent-assisted tuning surfaces;
> [`gating-v2-plan.md`](gating-v2-plan.md); and
> [`room-correction-regime-plan.md`](room-correction-regime-plan.md).
> **Supersedes: nothing.**
>
> Verbatim research — the two briefs and the owner-run dissertation whose
> Stage 0–4 blueprint is this plan's skeleton — is preserved in
> [`docs/research/2026-07-29-attribution/`](research/2026-07-29-attribution/README.md).
> Evidence base: the 2026-07-29 Fc forensics (#1867–#1870), the field and
> follow-on issues #1872–#1877, and the 2026-07 measurement corpus as swept
> by WO-0's two reported passes, whose mechanism-frequency table,
> instrument-error catalog, corpus index, and P2/P6 re-analyses are checked in
> beside the research as `04-`…`08-` and are what §4's seed table cites. Bulk
> data (CSVs, WAVs, scripts) stays in the gitignored
> `captures/wo0-retrospective-20260729/`.

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
  [`jasper/active_speaker/crossover_v2_flow.py`](../jasper/active_speaker/crossover_v2_flow.py).
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
Findings are persisted (retention model per **Q-C** — undecided, and WO-1
decides it before it writes anything), rendered on the review screen, and
consumed by the prescription stage. The excluded-band τ records are the
embryo: WO-4 promotes them from "reason to refuse EQ" to findings with
mechanism and fix class attached.

**Two vocabularies, one artifact.** `mechanism` is *internal* taxonomy —
it names physics, it may name hardware, and it appears on ops/forensic
surfaces (the finding record, the harness artifact, `jasper-doctor`-class
output, the expert disclosure). `household_copy` stays **phenomenon-level
and hardware-noun-free**, because the shipped prohibition it inherits is
explicit — `_null_classification_copy`'s docstring in
[`crossover_v2_flow.py`](../jasper/active_speaker/crossover_v2_flow.py) says
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
hard rule; the cautionary catalog of #1866).

**Relation to the shipped `ReasonCode`.** `fix_class` is an *internal
routing* field; it is not a second copy vocabulary. Where a finding's
consequence is a per-bin correction limit, it must resolve to the one
shipped closed vocabulary —
[`linearization_envelope.ReasonCode`](../jasper/active_speaker/linearization_envelope.py),
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
is the current planning authority for agent-assisted tuning surfaces, and
its §12 explicitly supersedes "a mandatory structured attribution verdict"
and "a fixed diagnostic/discriminator order". This plan does not reinstate
either:

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
argument is a *proposed* change to it, not the live policy; see **Q-D**.
Room line: the regime plan's boundary/transition framework), the verdict
copy, and the flow surfaces. Neither line imports the other's policy; both
import the library.

Two accuracy notes on that boundary:

- [`room-correction-regime-plan.md`](room-correction-regime-plan.md) is an
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
| M3 | Unfitted-overlap slope error | **model-derived / synthetic — the least-grounded seed.** 0.9 dB of the 07-29 Fc dip (model); #1817's synthetic reconstruction shows the fit attracted +2.379 dB at 1570.6 Hz fitting *through* an LR4 against a flat target. Its own signature has never been measured | Reverse-null cannot be driven deep at any delay; broad magnitude error | Reverse-null (P1) | `refit` |
| M4 | Frame mismatch (reference frame generally, incl. window vs power) | **adjudicated (mic and speaker physically moved), and the corpus's largest disagreement.** S0's five-frame spread on **one speaker with one DSP state** reaches **17.5 dB** at 8–16 kHz (desk edge −6.94 … ground plane −24.43 dB) — but read the frames literally: three of the five are physical relocations, up to and including removing the desk and putting the speaker on the floor. Every frame is legitimate; none is "the same setup analysed differently." #1859's 3–7.7 dB cross-session case is *corroborating* only — it names geometry frame as the "leading candidate" and does not exclude a physical change over the intervening ~40 h. #1857's 3.13 dB of intra-verdict frame drag is **reproduced to the digit** from the persisted curve — arithmetic verification, not a probe, so it does not earn the `adjudicated` tier | Same speaker measures differently under different frames; window flat where cloud mean dips (or vice versa) | Design-axis capture vs cloud (P5); the matched-geometry A/B #1859 names | `measure_differently` / `document_as_physics` |
| M5 | Boundary/SBIR (desk bounce) | **Observed**, two sessions (P2 — *corroborating* for SBIR specifically, since position-variance proves "not source-fixed", not "boundary"); **adjudicated** only as a positive control (S0 ground plane); **refuted** for the 1.8 kHz dip. Observed: position-variant LF dips at **735 / 854 Hz** and **1161 / 1166 Hz**, **2.7–4.4 dB** deep, present at **75–100 %** of positions (noon's 1250 Hz candidate is 9/12), CV **15–17 %** — against the source-fixed features' 0.6–1.5 % in the same captures. That factor-of-ten separation is the classifier's whole basis. A first cancellation near 800 Hz implies a ~21 cm direct-vs-reflected path difference, which the geometry model must be checked against. Positive control: S0's ground-plane leg (deliberately created bounce at 125–146 µs, r 0.74–0.93 — the *worst* top-octave reference of the three legs). **Refuted** for the one feature it had previously been invoked on (S0's 1.8 kHz dip mispredicts by 19–70 %) — a mechanism being real does not make every invocation of it right | Position-variant null (CV > 8 % across the cloud); frequency tracks geometry | Position-variance (P2); geometry model | `physical` |
| M6 | Nonlinearity (driver/port, level-dependent) | **Now measured (WO-0 Farina pass). The woofer dominates by ~9 dB *in relative THD* — a frame that must be named:** woofer H3 **−47.0 dB / 0.45 %** at 200–400 Hz vs the tweeter's worst supported band at −56.1 dB, both referred to **each driver's own fundamental**. In *absolute* harmonic level at the microphone the ordering **reverses** — the fundamentals sit ~14 dB apart (−22.1 vs −8.1 dB), putting the tweeter's H3 ~5 dB higher. Naming the frame is not pedantry in a plan whose own instrument-error catalog makes `frame` the dominant failure class. H3 > H2 **in the 200–800 Hz bands where the woofer's distortion is largest** (the symmetric-nonlinearity signature); **the ordering reverses above 800 Hz** (H2 above H3 by 1.8 dB noon, 3.9 dB morning, all supported data). The tweeter's H2 sits at or near the floor in both bands, so **the horn's even-order behaviour is currently unmeasured, not measured-as-low**. Two independent measurement paths (laptop full-range, Pi per-driver) agree **on the woofer's H3** within 1.5–3 dB. The UMIK-2 chain is *not* the sensitivity limit **at the levels that matter here** (the iLoud control resolves 0.02–0.74 % with 6–30 dB of headroom); where the fundamental itself was 10 dB down, H2 became unmeasurable. Separately, one reference-tier P3 negative: identical within 0.12 dB at 10 dB lower drive. **Onset level is NOT derivable from existing data** — the shipped two-level pilot's low leg sits at the harmonic floor (0.1–2.8 dB headroom). **Suggestive, unconfirmed:** the woofer's 200–400 Hz H3 is level-*invariant* in ratio (−0.06 dB/dB, vs +2.0 for a memoryless cubic) while 400–800 Hz grows at +1.45 and 800–1600 Hz at +2.65 dB/dB. That rests on a **clean cross-session lever of only 2.62 dB with up to 1.1 dB of DSP confound** in the bands where the harmonics land — which is exactly why it is a hypothesis. If it survives a deliberate probe, the speaker's largest distortion is a **fixed** mechanism (resonance / port / cabinet) rather than motor nonlinearity | dB-signature changes with drive level; harmonic IRs at `−L·ln N` | Deliberate **≥12 dB** two-level pair with the DSP held constant (P3 — the bench item; the shipped 10 dB pilot is too quiet on its low leg); Farina (P6) | `physical` (operating point) / `carve` — **the label is provisional in its justification, not its spelling**: a fixed-mechanism outcome still routes `physical`, but for a different reason and with different copy, so the deliberate pair is what makes the entry defensible |
| M7 | **Inter-driver level-frame error** | **adjudicated** (by the extended definition above — a known intervention was applied and the feature responded) **— the corpus's largest measured defect, and the only mechanism with a before/after listening verdict.** A 7–11 dB dark tweeter; 13.9 dB between the two per-driver fit targets; the trim frame sitting at the bare datasheet sensitivity gap with a −14.4 dB L-pad in circuit; #1667's 1.7–6.3 dB trim bias. An independent hand correction moved every band 300 Hz–16 kHz to within ±0.9 dB of the reference. **The two drivers' realized passband levels are never compared anywhere in the pipeline** | One driver's passband sits N dB off the other's against any common anchor; broad and monotonic, **not** an interference notch | Per-driver passband comparison against a declared-sensitivity prior (free, back-catalog) | `eq` (level) — and **`refit` when the level error is upstream in the fit's own frame**, which is the #1667 case: a trim solved by band-averaging inside the woofer's rolloff skirt is not fixed by adding level, it is fixed by re-solving. WO-0 proposed `eq (level)` alone; the second class is this plan's addition, on the M7-vs-M3 distinction below |
| M8 | **Vertical lobing at Fc** | **adjudicated (old horn; re-run owed).** S0 measured the Fc-region dip against mic height: **10.7 dB** at tweeter height (n=6) → **4.1 dB** a hand-width low (n=4) → **1.7 dB** on the ground plane (n=3), while the 8–16 kHz ripple held then *deepened*; Pearson r between the two features = **−0.05** (n=13). Consistent with the noon cloud's 32 % walk in the Fc dip's centre (M1's row), though those are **uncontrolled position clouds, not vertical-angle sweeps**, and the morning cloud is ambiguous — the dissertation's signature is ">~10 % **with vertical angle**," which only P5 actually tests. S0 predates the 07-27 horn swap, so this is a reason to re-run P5 on current hardware, not a finished answer | Fc-region dip depth and/or frequency tracks vertical mic offset; **uncorrelated with the HF ripple** | Design-axis / vertical-offset capture (P5) | `physical` / `measure_differently` |

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
errors. M3 is "the shapes don't sum"; M7 is "the levels were never compared."

M4's frame mismatch is a *measured evidence* class: it says two frames
disagree and by how much. Whether that disagreement should change the
linearization **fit anchor** is a different question, deliberately not
decided here — see **Q-D**.

WO-0 has reported; the v1 set is freezable, with M6's fix class explicitly
provisional (§7, §8).

## 5. Probe primitives

| # | Probe | Cost | Needs owner? |
|---|---|---|---|
| P1 | Reverse-null delay sweep: tweeter-inverted sweeps at candidate delays; null depth at Fc adjudicates (−20 dB by delay alone ⇒ `delay`; can't reach −10 dB ⇒ `refit`; thresholds are practitioner heuristics, in-phase LR only) | 1 sweep per delay | No (fixed mic) |
| P2 | Position-variance classifier: feature-frequency stability across cloud positions (CV < 3 % ⇒ source-fixed, CV > 8 % ⇒ position-variant, between ⇒ unsure). **Corroborating only** for a source claim | Free — *once the harness persists per-position curves*; today it needs raw WAVs off the Pi | No (back catalog) |
| P3 | Two-level linearity: identical dB signature at two solved drive levels ⇒ linear; level-dependent ⇒ nonlinear. Needs a deliberate **≥12 dB** pair with the DSP held constant — the shipped 10 dB in-capture pilot's low leg sits at the harmonic floor (0.1–2.8 dB headroom), so only its high leg is measurable | 1 extra sweep | No (fixed mic) |
| P4 | Rotation: repeat a per-driver sweep (`sweep_w`/`sweep_t`) with the speaker rotated ~30°. **The source-fixed adjudicator** | 1 capture | Yes (rotate speaker) |
| P5 | Design-axis / vertical-offset capture: window-vs-cloud delta at Fc, and the Fc dip's depth vs mic height (M8's discriminator) | 1–2 captures | Once (place mic) |
| P6 | Farina harmonic-IR extraction from the ESS sweeps we already run. **Productizable at zero measurement cost**, with the guards below | Free (reprocessing) — *but the "back catalog" is an operator-enabled 90-file ring, currently full and dropping its oldest capture on every new one, holding roughly two session-days; this pass had **three** MEASURE captures, one of them glitched* | No (back catalog) |
| P7 | Repeat-variance validity gate (Stage 0): time-variant features excluded from attribution | 1 repeat | No |

**P2 cannot name a source, and the shipped instrument says so.**
[`interference_nulls.py`](../jasper/audio_measurement/interference_nulls.py)'s
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

**P6 needs four things the current pipeline does not do.** The first three
are WO-0's own named guards; the fourth is a requirement its results imply.
The threshold table is in that report and is WO-4's direct input, not
restated here:

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

**Phone-mic caveat, carried from the owner's recorded adoption notes
(#1866):** phone-mic AGC and nonlinearity bound both the Farina harmonic
pass (P6) and the level-invariance sensitivity (P3). The UMIK-class lab path
is unaffected. A P3/P6 finding derived from a phone-mic capture must carry
its instrument and must not be graded as if it came from the calibrated
path. Precision the corpus adds: our own "phone series" is an *iPhone plus a
calibrated iMM-6C*, so there is **no built-in-phone-mic sweep in the corpus
at all** — the caveat stays flagged and untested by our data, claimed in
neither direction.

Probe results attach to findings as evidence. In v1 a probe is a **harness
program** (§6), not a new household flow and not a re-arm of the phone
relay — see §6 for why the relay re-arm is explicitly not the carrier.

## 6. Quick-sweep harness (the tooling WO — and the LLM-ready deliverable)

The harness is the loop: *play a known program on the speaker → capture
on an attached mic → analyze → persist a self-describing artifact*, all
scriptable, no phone, no relay, no human mid-loop.

**The carrier, decided.** The harness is an **operator CLI** whose graph
lifecycle is the shape of
[`jasper/bass_extension/bench/activation.py`](../jasper/bass_extension/bench/activation.py)
— `activate → prove → yield for measurement → restore`, where the
predecessor is snapshotted and fingerprinted first, only the *running*
config is mutated (never the on-disk file, so `reload()` is always a valid
recovery point), and the restore fires on **every** exit including
cancellation, shielded, and is re-proven. That module contributes the
lifecycle; it does **not** contribute config validation (its proofs are
bass-block-specific). So it is **composed with `dsp_apply`-grade validation
before any load**:
[`jasper/dsp_apply.py`](../jasper/dsp_apply.py) contributes
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
  (the #1855 class is a bug here, not a nit — and it is now *located*:
  `cloud_measure_program.wav` and `verify_program.wav` are byte-identical, so
  the retention seam takes its phase label from the program rather than the
  flow, mislabelling **32 of 45** retained sidecars); an index file per corpus
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
  [`testing-tooling.md`](testing-tooling.md) records for the correction
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
[`scripts/capture-correction-diagnostic.py`](../scripts/capture-correction-diagnostic.py)
+ [`analyze-correction-diagnostic.py`](../scripts/analyze-correction-diagnostic.py)
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
[`docs/testing-tooling.md`](testing-tooling.md) is a WO-2 acceptance item,
per that doc's own "add it here in the same PR" rule.

This is deliberately *also* the LLM-ready tooling: an agent that can
read the corpus index, run one harness command, and read one JSON is the
target consumer — deterministic pipeline first, smarter agents later,
same door.

## 7. Work-order ladder

Every WO: implementation by delegated agent, independent adversarial
review to 0 blockers / 0 should-fixes, behavioral promises pinned by
tests in the same PR, docs-impact run, memory/issue updates.

- **WO-0 — Corpus retrospective** (multi-agent, read-only; no code).
  **Reported 2026-07-29, both passes; acceptance met.** The five findings
  documents are checked in as
  [`04-mechanism-frequency.md`](research/2026-07-29-attribution/04-mechanism-frequency.md),
  [`05-instrument-error-catalog.md`](research/2026-07-29-attribution/05-instrument-error-catalog.md),
  [`06-reanalysis-farina.md`](research/2026-07-29-attribution/06-reanalysis-farina.md),
  [`07-reanalysis-position-variance.md`](research/2026-07-29-attribution/07-reanalysis-position-variance.md),
  and [`08-corpus-index.md`](research/2026-07-29-attribution/08-corpus-index.md);
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
  storage, and the excluded-band promotion path. No UI yet. Acceptance:
  schema test; one golden finding round-trip; provenance marker discipline
  (old bundles without findings readable); the **stable cross-store session
  identity** of §6 (a finding cites its evidence by an id that survives every
  hop, not by content hash or directory mtime — WO-0 proved both of those
  fail); the **per-position evidence** of §6, without which P2 is not the
  free probe §5 calls it; **and Q-C answered in the PR** — the retention
  model is stated,
  implemented, and pinned by a test (a finding must not outlive, or silently
  predecease, the evidence it cites).
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
  (M7 — the per-driver realized-passband comparison the pipeline does not do
  today), vertical-lobing detector (M8, off P5). P2's and P6's thresholds,
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
  ([`program_analysis.py`](../jasper/audio_measurement/program_analysis.py)),
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
  **Precondition:** #1858 — the persisted `predicted_sum` prior is
  stride-decimated without pre-smoothing and is aliased below ~500 Hz. WO-5
  reads that prior. Either #1858 lands first, or WO-5 states in-PR which
  bands it refuses to draw conclusions from and pins that refusal.
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
  bare numbers as confidence), copy review.
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
    [`delta_probe`](../jasper/active_speaker/delta_probe.py) —
    realized-vs-commanded per-frequency map, four verdicts, three of which
    roll back — from a rollback gate to a per-attempt evidence producer.
    Where an attempt's delta must bound the next attempt's correction depth,
    it lands on the **already-reserved** `ReasonCode.LIMITED_BY_VERIFY_DIVERGENCE`
    — whose docstring reserves it "for the closed-loop verification feedback
    described in the design doc … a later PR." WO-7 is that later PR; it does
    not mint a new code;
  - ruling 6 (measurement economy) binds the loop: attempts are cheap only
    if each one earns its captures.
  Research input:
  [`03-brief-iterative-dialin-and-position.md`](research/2026-07-29-attribution/03-brief-iterative-dialin-and-position.md).
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
  Acceptance: the SoC boundary test — no cross-line policy import.

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
   explicit exception carried in the table: **M6's fix class is provisional**
   until the deliberate two-level pair runs. A level-invariant-in-ratio result
   would move the speaker's largest distortion from motor nonlinearity to a
   fixed mechanism, and that is a different fix.
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
- **Q-C: findings retention.** Bundle-lifetime (current proposal) or an
  independent ring like wake events? WO-1 cannot ship without an answer;
  §3.1 states none.
- **Q-D: proposed supersession of the design-axis fit anchor.** The adopted
  canon is that the speaker layer's contract is *flat gated direct sound on
  the **design axis***
  ([`active-speaker-tuning-layers-design.md`](active-speaker-tuning-layers-design.md)'s
  "top of the table" contract, immediately above its
  do-not-re-litigate list), and that **"the fit stays anchored on the
  design-axis per-driver curves … the recorded trigger for revisiting is
  S3 closed-loop evidence, nothing else"**
  ([`flat-linearization-productization-plan.md`](flat-linearization-productization-plan.md),
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
- **Q-E: #1857's reference-frame anchor.** Role-anchored (woofer passband),
  passband-weighted, or the current full-range mean with the verdict copy
  naming both the frame and the direction? #1857 states this as an open
  design decision and warns against patching blind. WO-5 implements the
  ruling; it does not make it.
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

---

Last verified: 2026-07-29
