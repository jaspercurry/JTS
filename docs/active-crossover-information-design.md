# Active Crossover Builder: product and architecture specification

> **Status: design of record.** This document owns the intended user experience,
> product states, parameter-ownership rules, and implementation boundaries for
> manually or automatically commissioning an active crossover in JTS. Low-level
> DSP and hardware-safety contracts remain canonical in
> [`HANDOFF-active-speaker-dsp.md`](HANDOFF-active-speaker-dsp.md); shared capture
> and analysis primitives remain canonical in
> [`HANDOFF-audio-measurement-core.md`](HANDOFF-audio-measurement-core.md); room
> correction remains canonical in [`HANDOFF-correction.md`](HANDOFF-correction.md).
> Those documents should link here for crossover-builder product behavior rather
> than restating it.

## Product goal

JTS should let a user commission an active two-way or three-way speaker in one
of two equally supported ways:

1. **Manual:** the user enters the crossover frequency, filter family and slope,
   driver trim, polarity, and relative delay they want. JTS previews, validates,
   applies, and verifies that exact crossover.
2. **Automatic:** JTS guides the user through calibrated-microphone placement,
   measures each driver independently and in combination, proposes those same
   crossover values from acoustic evidence, and applies them only after the user
   reviews an explicit before/after comparison.

The two paths must converge on the same crossover model, compiler, application
transaction, verification step, and rollback behavior. Automatic commissioning
is not a second kind of crossover and must not create a second settings store.

The feature should be powerful enough for an experienced builder to understand
and control the result, while the normal path feels like a calm sequential
setup—not a measurement laboratory. At any point the user should understand:

- what JTS needs them to do next;
- whether sound is about to play and through which driver;
- which values are manual, measured, proposed, or currently applied;
- why JTS accepted or rejected a measurement;
- what will change if they press Apply; and
- how to return to the previously working crossover.

## Product promise

The crossover builder makes four promises:

1. **Manual control is first-class.** A knowledgeable user can set the values
   directly without performing microphone measurements or invoking an AI helper.
2. **Automatic means measured.** Automatic values come from the user's current
   speaker, microphone calibration, applied test graph, and captured acoustic
   evidence—not merely driver datasheets or an LLM guess.
3. **Overwrite is explicit.** An automatic result may replace a manually tuned
   crossover, but only through an explicit comparison and confirmation. It never
   silently rewrites live settings.
4. **Room correction comes second.** Room correction may begin only after the
   driver-domain crossover is applied and its combined response has been
   verified. Room correction must not be used to disguise a broken driver
   handoff.

## Scope

### In scope

- Passive/full-range, active two-way, and active three-way layouts, with the
  existing optional-subwoofer topology represented where the runtime supports
  it.
- Manual crossover frequency, family/order, slope, trim, polarity, and delay.
- A guided calibrated-microphone flow for per-driver level, response, usable
  overlap, polarity, delay, and combined-response evidence.
- Deterministic automatic proposals for crossover values.
- Preview, explicit apply, post-apply verification, and rollback.
- Evidence and state surfaces sufficient to diagnose a failed run without
  repeating it blindly.
- A clean handoff into room correction after commissioning succeeds.

### Not in the first working release

- Automatic limiter, excursion, or thermal-model design.
- Automated high-output compression testing.
- Arbitrary optimizer plug-ins or a general-purpose loudspeaker CAD framework.
- Unbounded per-driver EQ or automatic positive-gain correction.
- Listening-preference EQ or room-response target curves; those belong to later
  DSP layers.
- An LLM directly authoring or applying live DSP. Research may suggest safe
  starting constraints, but measured deterministic code owns the proposal.

These are deliberate boundaries, not a claim that the omitted work is
unimportant. The first release must work reliably and be understandable. More
safety and analysis can be added behind the same contracts once the measurement
and application loop is proven on hardware.

## First principles

An electrical crossover setting is not the acoustic crossover the user hears.
For each driver, the acoustic result is the product of the mounted driver's
response, the electrical filter, EQ, gain, polarity, and propagation delay. The
speaker output is the complex sum of all active drivers.

Therefore a crossover is commissioned only when JTS has addressed:

1. each driver's protected and usable frequency range;
2. a crossover frequency inside a clean overlap region;
3. the **acoustic** high-pass and low-pass shapes, not only their electrical
   labels;
4. relative driver level through the handoff region;
5. polarity and relative acoustic delay;
6. the combined response with both drivers playing;
7. gross off-axis or vertical-lobing problems where the measurement tier can
   observe them; and
8. headroom for the proposed filters and trims.

A tone at one frequency is useful for setting a safe capture level. It is not
enough to design a crossover. A protected logarithmic sweep provides the
broadband magnitude and impulse evidence; separate and summed measurements
provide the handoff evidence.

## One model, three states

The existing `ActiveSpeakerPreset` vocabulary is the semantic source of truth
for crossover values. Extend that vocabulary when the product gains a real
parameter; do not introduce a parallel automatic-crossover schema.

The product has three states with different authority:

| State | Purpose | Can affect sound? |
|---|---|---:|
| **Working crossover** | The user's editable manual values or the latest reviewed automatic proposal | No |
| **Candidate** | A frozen, validated snapshot compiled from the working crossover and its evidence | No |
| **Applied crossover** | The active speaker preset confirmed by the DSP runtime | Yes |

Measurement records are evidence, not a fourth settings store. They may produce
a new working proposal, but they never become live merely because a capture
completed.

The applied crossover is authoritative for playback. The working crossover is
authoritative for the form. The candidate records exactly what would be applied.
The UI must never merge values from those states implicitly.

### Parameter provenance

Every candidate should be able to explain where each value came from:

- `manual` — entered or edited by the user;
- `measured` — derived from the current measurement session;
- `recommended_start` — a conservative starting value from driver/profile
  constraints, not yet proven acoustically; or
- `preserved` — carried unchanged from the applied crossover.

Provenance is metadata on the canonical value, not a second value. It exists so
the comparison and diagnostics can be honest.

### Replacement semantics

- Applying a manual candidate replaces the applied crossover with the visible
  manual values.
- Running automatic commissioning produces a proposal; it does not edit the
  applied crossover.
- **Replace with measured crossover** shows every changed parameter and then
  applies the automatic candidate atomically.
- Returning to Manual after an automatic proposal edits the same working
  crossover.
- Re-running automatic commissioning creates new evidence and a new proposal;
  it does not rewrite historical evidence.
- The immediately previous applied crossover remains the rollback target.

The first release should use one all-or-nothing comparison and Apply action. A
matrix of per-field locks or optimizer weights would add complexity before a
named user need justifies it. An expert who wants a hybrid result can accept the
proposal, switch to Manual, edit it, and apply the resulting manual candidate.

## User experience

The `/sound/` surface owns speaker layout, output identity, and manual editing.
The HTTPS `/correction/crossover/` surface owns microphone permission and
acoustic commissioning. They are two views of the same working and applied
crossover, not independent wizards.

The normal experience presents one primary action at a time.

```mermaid
flowchart TD
    A["Choose speaker layout and outputs"] --> B["Create a protected starting crossover"]
    B --> C{"How do you want to tune it?"}
    C -->|Manual| D["Edit crossover values"]
    C -->|Use microphone| E["Select calibrated microphone"]
    E --> F["Place microphone on the fixed reference axis"]
    F --> G["Auto-level and measure each driver"]
    G --> H["Measure the combined crossover"]
    H --> I["Build measured proposal"]
    D --> J["Review candidate"]
    I --> J
    J --> K["Apply atomically"]
    K --> L["Verify the applied crossover"]
    L --> M["Continue to room correction"]
```

### Step 1: choose the speaker and outputs

Ask only what is needed to establish the physical topology:

- mono or stereo;
- passive, active two-way, or active three-way;
- optional subwoofer when supported;
- driver role on each physical DAC/amplifier output; and
- confirmation that the output actually feeds the named driver.

Hardware discovery supports this step but should not dominate it. The user sees
physical language such as **Woofer · Output 1**, not internal channel-map nouns.
Changing an output assignment invalidates measurement evidence tied to the old
assignment.

### Step 2: establish a protected starting crossover

Automatic commissioning cannot safely discover a tweeter's lower limit by
sending it an unrestricted sweep. Before any driver plays, JTS needs a
conservative starting graph containing:

- output routing;
- an initial crossover frequency and slope;
- driver-specific high-pass protection where required;
- conservative trims; and
- the existing test-volume bounds.

The user may enter this starting point manually or use research/profile data to
prefill the visible fields. Prefill is advice. The visible working crossover is
the source of truth, and the backend validates the resulting graph before it
can emit sound.

### Step 3A: manual crossover

Manual mode exposes, per crossover region:

- crossover frequency;
- filter family and order/slope;
- lower-driver and upper-driver trim;
- polarity;
- relative delay; and
- advanced bounded driver EQ only when the existing preset/compiler supports
  it as a real product parameter.

The default view can keep polarity, delay, and advanced corrections collapsed,
but they must remain reachable. Manual does not mean unvalidated: JTS still
checks topology, driver protection, graph validity, and compiler headroom.

The main action is **Review crossover**. Saving a working form must not imply
that it is active.

### Step 3B: automatic microphone commissioning

Full automatic commissioning requires a calibrated measurement microphone or a
calibration file selected for the connected microphone. A missing calibration
may support an explicitly degraded level-only diagnostic, but it must not be
represented as phase-aware, frequency-response-accurate automatic crossover
design. The calibration curve improves frequency-response accuracy; it does not
create a shared timing reference. Timing-sensitive analysis must separately use
a synchronized capture reference or a bounded measured delay/null walk.

The flow should identify the selected microphone and calibration, then explain
one authoritative placement:

> Put the microphone on the speaker's intended listening axis, approximately
> one metre away when the room permits. Aim it according to the microphone's
> calibration file. Keep the microphone and speaker completely still until all
> driver and combined measurements are finished.

The exact instruction may be specialized by a hardware/speaker profile, but the
default automatic flow must not ask the user to move the microphone from driver
to driver. A fixed reference-axis position preserves the drivers' relative
arrival-time and summation evidence. Optional near-field diagnostics are a
separate advanced tool, not the default alignment geometry.

JTS then performs this sequence:

1. Capture ambient noise and determine whether the measurement can proceed.
2. For each driver, derive a protected level-probe band from the applied
   crossover and driver-protection edges.
3. Gradually raise that driver's measurement level until the representative
   probe reaches the target SNR with clipping headroom, or explain the exact
   shortfall.
4. Play a protected logarithmic sweep through that driver only.
5. Repeat the sweep three times without moving the microphone.
6. Reject clipped, incomplete, stale-graph, wrong-driver, or low-SNR captures.
7. Aggregate accepted repeats robustly and retain their spread.
8. Continue to the next driver using its own safe probe and locked level.
9. Measure all drivers in a crossover region together, first in the candidate
   polarity and then with the bounded reverse-polarity validation needed for
   alignment.
10. Produce a measured proposal and a plain-language explanation.

Three exact-position repeats are the normal crossover default. They improve
repeatability and expose outliers without confusing crossover commissioning
with room spatial averaging. If timing is trustworthy, analysis may align and
coherently aggregate them. Otherwise, it should use robust magnitude statistics
while preserving the individual complex records. It must never average moved
microphone positions into phase/alignment evidence.

### Level control and SNR

Auto-level is driver-specific because a woofer, midrange, and tweeter have
different safe and useful bands. Tone/probe selection remains owned by
`jasper.active_speaker.test_signal_plan`; UI or relay code must not duplicate
frequency rules.

The controller should:

- begin at the bounded quiet level;
- use a short band-limited probe or preview sweep representative of the driver's
  useful measurement band;
- raise gain gradually within the existing commissioning envelope;
- target at least 25 dB SNR when practical;
- accept 20–25 dB with an explicit reduced-confidence result;
- stop below 20 dB and report how many decibels are missing;
- retain microphone peak and clipping headroom; and
- keep a visible Stop action while sound is playing.

A 1 kHz scalar level is not sufficient evidence that a broadband room or driver
sweep has 20 dB SNR. The stored ambient recording and the accepted sweep should
support band-specific quality reporting. Raising microphone gain does not fix
acoustic SNR when it raises room noise and signal equally.

### Building the automatic proposal

Automatic design should be deterministic and bounded. It should search only
candidate values allowed by the driver constraints and current topology, then
score them from measured acoustic evidence.

The proposal considers:

1. a usable overlap range in which both drivers are present and sufficiently
   clean;
2. crossover frequencies inside that range;
3. a small supported set of acoustic target families/orders;
4. relative trim across the overlap, not unrelated single-frequency levels;
5. polarity and relative delay that produce stable summation;
6. combined-response deviation around the crossover;
7. reverse-polarity null quality where applicable;
8. bounded filter/headroom cost; and
9. available off-axis evidence, when the user deliberately captures it.

The first complete implementation does not need an open-ended optimizer. A
small deterministic candidate search over the filter families already supported
by `ActiveSpeakerPreset`, followed by measured summed verification, is easier to
test and explain.

### Step 4: review

Manual and automatic modes converge on the same review screen. It shows:

- current applied values;
- proposed values;
- provenance for each proposed value;
- the measured usable overlap;
- per-driver repeatability and SNR;
- separate-driver and combined-response plots;
- the reason a frequency, slope, trim, polarity, and delay were chosen; and
- warnings that reduce confidence without being safety blockers.

Primary actions:

- **Apply manual crossover**, or
- **Replace with measured crossover**.

Secondary actions:

- **Back to edit**;
- **Measure again**; and
- **Keep current crossover**.

The comparison must make it impossible to mistake a proposal for the live DSP.

### Step 5: apply and verify

Apply uses the existing shared DSP transaction. It must:

1. freeze the candidate and applied-profile fingerprint;
2. compile through the single active-speaker compiler;
3. validate the emitted graph;
4. retain the previous known-good profile;
5. switch atomically;
6. confirm that the runtime loaded the expected graph; and
7. roll back if compilation, loading, or runtime confirmation fails.

After apply, JTS measures the combined crossover again at the same fixed
reference position. Verification compares like with like and records the result
against the applied profile fingerprint. A failed acoustic verification does
not silently declare success; the user can restore the previous crossover or
return to edit/measure.

Once the crossover is applied and verified, the UI offers **Continue to room
correction**. Room correction then defaults to five distinct listening-area
positions. Those room positions are spatial samples and are intentionally
different from the stationary repeats used for crossover commissioning.

## Architecture and ownership

Each concern has one owner. The browser orchestrates user intent; it does not
become an acoustics engine.

| Concern | Owner | Must not own |
|---|---|---|
| Editable and applied crossover semantics | `jasper.active_speaker` preset/profile models | Browser-only settings or relay state |
| Safe driver probe/sweep plan | `jasper.active_speaker.test_signal_plan` and graph-safety policy | Page-specific frequency branches |
| Sweep generation, deconvolution, calibration, quality math | `jasper.audio_measurement` | Product sequencing or profile application |
| Driver, overlap, summed-response, and alignment analysis | `jasper.active_speaker.driver_acoustics` / alignment modules | HTTP, relay transport, or live DSP mutation |
| Durable commissioning run and evidence identity | Active-speaker measurement/session layer | Filter compilation |
| Candidate derivation and provenance | Active-speaker baseline/candidate layer | Playback side effects |
| Sequential product envelope | `jasper.web.correction_crossover_*` | Acoustics math or duplicated state machine in JavaScript |
| Phone/browser capture transport | Jasper capture relay | Tone selection, analysis, or product policy |
| CamillaDSP emission | Existing active-speaker compiler | Inferring measurements or user intent |
| Atomic apply and rollback | Shared DSP transaction/runtime boundary | Editing working drafts |
| Room correction | `jasper.correction` | Driver-domain crossover repair |

### DRY invariants

The implementation should have exactly:

- one crossover parameter vocabulary (`ActiveSpeakerPreset`);
- one editable working crossover;
- one driver-safe signal planner;
- one sweep/deconvolution implementation;
- one measurement-quality model with consumer-specific policy values;
- one candidate compiler;
- one live-DSP transaction;
- one current applied-profile fingerprint; and
- one sequential server-authored commissioning envelope.

Manual and automatic entry points may render different steps, but they do not
fork any of those owners. The relay transports opaque capture intent and WAV
bytes. It does not learn what a tweeter is.

## Durable evidence and observability

Every automatic run should produce a cohesive commissioning bundle rather than
leaving raw WAVs and a global summary unrelated to one another. At minimum, the
bundle records:

- session id and schema version;
- software/build version;
- speaker topology, output assignments, and immutable graph fingerprint;
- microphone identity and calibration identity/hash;
- placement instructions acknowledged by the user;
- ambient recording and band-specific noise report;
- probe frequency/band, driver-specific locked volume, and ramp result;
- raw accepted and rejected WAVs with bounded retention;
- sweep metadata and complete played-excitation ledger;
- per-capture quality, clipping, SNR, and rejection reason;
- repeat aggregate and spread;
- individual-driver, summed, and reverse-polarity analysis;
- previous values, proposed values, and per-value provenance;
- compiler/validation result;
- apply transaction and rollback target; and
- post-apply verification result.

### Runtime surface

`/state` or the existing active-speaker state aggregation should expose a small
household/operator summary:

- idle, measuring, proposal ready, applying, verified, or failed;
- current session and applied-profile fingerprints;
- currently audible driver and locked volume while measuring;
- last accepted SNR and clipping headroom;
- last failure code and human action; and
- whether room correction is allowed to proceed.

Detailed curves and bundle paths belong in the correction session report, not
the top-level state payload.

### Structured events

Log important transitions once using stable `event=` names, including:

- `crossover.session_started`;
- `crossover.level_locked` or `crossover.level_failed`;
- `crossover.capture_accepted` or `crossover.capture_rejected`;
- `crossover.repeats_aggregated`;
- `crossover.proposal_ready`;
- `crossover.apply_started`;
- `crossover.apply_succeeded`;
- `crossover.apply_rolled_back`; and
- `crossover.verification_passed` or `crossover.verification_failed`.

Fields should include session, group, driver role, graph fingerprint, SNR,
headroom, reason code, and candidate/applied fingerprint as relevant. Do not log
per-audio-frame updates or full calibration/measurement payloads into the
journal.

The visible UI translates typed failures into specific actions: move the mic,
quiet the room, raise the external amplifier, reconnect the microphone, restore
the expected output assignment, or measure again.

## Minimum safety floor

The feature is allowed to stay simple, but the first working release still
needs a small non-negotiable safety floor:

- Never send an unrestricted or full-range stimulus to an isolated tweeter or
  midrange that requires protection.
- Build every audible measurement through the protected production graph.
- Start each driver at the bounded quiet level and raise it gradually.
- Provide immediate Stop and bounded session expiry.
- Reject clipped recordings and stale topology/graph evidence.
- Keep automatic gain attenuation-only until positive-gain headroom and driver
  limits have a proven contract.
- Apply atomically and restore the previous known-good graph on failure.
- Do not allow room correction to proceed against an unverified or stale active
  crossover.

These protections already align with established JTS boundaries and do not
require a new safety framework.

### Later safety and analysis enhancements

Once the basic loop works end to end, add independently justified improvements:

- distortion and compression measurement at more than one level;
- driver excursion/thermal models and automatic limiter commissioning;
- microphone absolute-SPL calibration and exposure guidance;
- richer off-axis/directivity capture;
- automatic all-pass or bounded per-driver PEQ design;
- hardware-specific measurement fixtures; and
- broader multi-group/stereo coherence validation.

Each enhancement must extend the existing preset, evidence, analysis, or
validation seam. It should not add a parallel wizard or optimizer framework.

## Delivery slices

### Slice 1: honest working foundation

- Complete the manual parameter surface, including polarity and delay.
- Ensure manual apply and rollback preserve exactly the reviewed values.
- Rename current automatic behavior as **automatic driver level matching** where
  it still changes trims only.
- Take three stationary sweeps per driver and aggregate them robustly.
- Persist one cohesive commissioning bundle.
- Require a post-trim summed capture and post-apply verification.
- Preserve manual frequency, family, slope, polarity, and delay when applying a
  trim-only proposal.

### Slice 2: automatic alignment

- Retain both normal- and reverse-polarity summed evidence per crossover region.
- Propose polarity deterministically.
- Implement a bounded measured delay walk rather than assuming independently
  started browser captures share sample timing.
- Support every crossover region in a three-way system, not only the lowest.
- Verify the resulting sum before offering room correction.

### Slice 3: automatic crossover design

- Derive a measured usable overlap range.
- Search a small supported set of frequencies and acoustic target slopes.
- Score combined response, alignment, headroom, and available directivity
  evidence.
- Produce the same canonical candidate and comparison used by Manual.
- Allow explicit replacement of a manual or previously automatic crossover.

This order delivers useful measured behavior at each step without pretending a
trim calculation is a complete automatic crossover.

## Acceptance criteria

### Manual

- A user can enter every supported crossover parameter without a microphone.
- The preview and comparison contain exactly those visible values.
- Apply uses the shared compiler/transaction and confirms the loaded profile.
- Reloading the page clearly distinguishes working values from applied values.
- The previous applied profile can be restored.

### Automatic

- The flow identifies a calibrated microphone and gives one unambiguous fixed-
  axis placement instruction.
- Each driver receives a driver-appropriate protected level probe and sweep.
- Three stationary repeats are accepted, rejected, and aggregated visibly.
- The UI reports achieved SNR, shortfall, clipping headroom, and next action.
- Frequency, family/slope, trim, polarity, and delay proposals cite measurement
  evidence and provenance.
- The combined response is measured before and after apply.
- No automatic result becomes live without explicit replacement confirmation.

### Architecture

- Manual and automatic paths produce the same preset/candidate schema.
- Browser and relay code contain no duplicated crossover-frequency policy or
  acoustic analysis.
- Measurement evidence is fingerprinted to topology, output, graph,
  microphone/calibration, and session.
- One active profile and one rollback target are observable at runtime.
- Failures emit one typed event and an actionable UI explanation.
- Room correction consumes only the verified applied crossover and never
  rewrites driver-domain crossover parameters.

## Current implementation gap summary

As of 2026-07-11, JTS has much of the substrate but not the full product:

- Manual setup exposes frequency, filter family/slope, and trim, but not all
  polarity/delay controls described here.
- The relay-guided automatic flow takes one accepted near-field sweep per
  driver and derives attenuation-only relative trims.
- Crossover frequency, family, and slope remain operator-owned rather than
  measured automatically.
- The automatic success path does not require summed-response, reverse-polarity,
  delay, off-axis, or post-apply acoustic verification.
- Existing alignment analysis is not fully reachable through persisted paired
  summed evidence.
- Automatic trim application must not reset a manually applied delay or
  inversion when no new alignment evidence exists.
- Active-speaker evidence is less cohesive than the room-correction session
  bundle and needs a session manifest tying capture through apply and verify.

That gap should guide implementation priority. It must not be hidden by calling
trim replacement a complete automatic crossover.

## Language guide

Prefer:

- **Set crossover manually**
- **Tune with microphone**
- **Measure woofer** / **Measure tweeter**
- **Put the microphone here**
- **17.4 dB SNR; 2.6 dB more needed**
- **Review measured crossover**
- **Replace with measured crossover**
- **Apply crossover**
- **Restore previous crossover**
- **Continue to room correction**

Avoid as primary product copy:

- baseline candidate;
- automatic candidate readiness;
- output map;
- arm measurement;
- stage graph;
- in-phase sum;
- evidence fingerprint mismatch; and
- correction state managed by the grouping graph.

Those terms may remain in diagnostics when they help a maintainer, but the
normal flow should name the user's task and the next action.

## Research basis

The internal research packet that motivated this design is
[`docs/research/2026-06-19-active-crossover-calibration/`](research/2026-06-19-active-crossover-calibration/README.md).
Primary technical references include Linkwitz's discussion of acoustic
crossover behavior and polar response, AES work on noncoincident-driver phase
and listening-window optimization, KLIPPEL transfer-function/directivity
measurement guidance, REW's sweep and timing-reference guidance, and Dirac's
multi-position room-measurement guidance:

- [Linkwitz: crossovers](https://www.linkwitzlab.com/crossovers.htm)
- [AES: crossover networks for noncoincident drivers](https://secure.aes.org/forum/pubs/journal/?elib=2649)
- [AES: crossover filter design for a listening window](https://secure.aes.org/forum/pubs/conventions/?elib=20947)
- [KLIPPEL: transfer-function measurement](https://www.klippel.de/manuals/frequencyresponse-distortion/trf/trf.html)
- [KLIPPEL: loudspeaker directivity measurement](https://klippel.de/training/attachments/training8/Training_8_Measurement_of_Loudspeaker_Directivity_en.pdf)
- [REW: making measurements](https://www.roomeqwizard.com/help/help_en-GB/html/makingmeasurements.html)
- [Dirac Live technical overview](https://www.dirac.com/wp-content/uploads/2024/06/Dirac-Live-a-technical-overview-white-paper.pdf)

Last verified: 2026-07-11
