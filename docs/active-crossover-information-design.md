# Active Crossover Builder: product and architecture specification

> **Status: design of record.** This document owns the intended user experience,
> product states, parameter-ownership rules, and implementation boundaries for
> manually or automatically commissioning an active crossover in JTS. Low-level
> DSP and hardware-safety contracts remain canonical in
> [`HANDOFF-active-speaker-dsp.md`](HANDOFF-active-speaker-dsp.md); shared capture
> and analysis primitives remain canonical in
> [`HANDOFF-audio-measurement-core.md`](HANDOFF-audio-measurement-core.md); room
> correction's shipped operational behavior remains canonical in
> [`HANDOFF-correction.md`](HANDOFF-correction.md), while its intended product
> behavior is canonical in
> [`room-correction-information-design.md`](room-correction-information-design.md).
> Those documents should link here for crossover-builder product behavior rather
> than restating it.

> **Wave 1 implementation boundary (2026-07-13): contract-only.** JTS now has
> silent contracts for target-bound hardware research, operator-confirmed driver
> safety profiles, exact excitation admission, neutral measurement identities,
> an Active-owned commissioning lifecycle, and an exact positive eligibility
> receipt. These types have no live playback, CamillaDSP mutation, persistence,
> or `/state` producer. The existing Active commissioning gate remains live,
> but Room R1b now admits only passive/not-required and rejects every active
> topology until the later integration lane explicitly issues, persists, and
> exposes receipt-backed authority. No hardware behavior was changed or
> revalidated by these hardware-free slices.

> **Wave 3 lifecycle boundary (2026-07-14; hardware-free).** A
> fresh authoritative comparison set that carries a production bundle session
> id now starts one durable Active commissioning run. The control-plane store
> persists the exact session fingerprint, run id, process-owner generation,
> immutable target-attempt reservations, and a bounded hash-chained journal of
> the nine-state transitions. Correction-web claims that owner at service start,
> making prior-generation callbacks stale, and
> `/correction/crossover/status` reports its `commissioning_run` block as
> `not_started`, exact
> `current`, comparison-`stale`, or fail-closed `unavailable` state. This is
> lifecycle identity, not acoustic or apply authority: production currently
> starts the run at `unconfigured`; no production caller yet reserves its
> region-scoped measurement attempts or advances its transition journal.

> **Wave 3 region-evidence boundary (2026-07-14; hardware-free).**
> `jasper.active_speaker.commissioning_evidence` now derives an immutable
> group-by-region plan from the exact typed durable-run handle, topology, preset, protected profile,
> comparison set, threshold profile, and commissioning session. Its strict pure
> values require separate normal, reverse, and delay identities for every
> crossover region (including both regions of a three-way); exactly three fresh
> one-shot stationary captures for normal and reverse; and five fresh one-shot
> captures at every coordinate of the exact Shared bounded schedule. The
> schedule keeps the exhaustive fine grid capped at 25 measurements and, when
> that grid is larger, binds at most 25 symmetric coarse coordinates plus the
> two adjacent fine coordinates around one explicit coarse refinement anchor.
> It schedules measurement only and makes no winning-delay claim. Capture
> planning requires the preset's exact group set: one mono active group for a
> mono preset, or exactly left and right active groups for a stereo preset, all
> using the preset's way-count mode and complete driver-role set. Capture
> contexts retain the typed reserved-attempt handle (including owner generation,
> attempt number, and reserved target), exact graph, placement, and both
> generation and playback protection proofs. An explicit per-region operator
> attestation supplies the signed geometry seed; even `0.0` cannot be assumed.
> A complete aggregate requires exactly one region value per plan target and
> makes artifact paths, admissions, and attempts globally unique. This is an
> authority *shape*, not a production authority issuer: no live host yet
> reserves those attempts, applies the
> normal/reverse/delay graphs, captures or persists the sets, evaluates a
> candidate, or advances the lifecycle.

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

The source fingerprint remains the compile-cache/freshness key. A distinct
candidate identity covers the normalized preview plus the immutable
recomposition snapshot: manual frequency, family/order, derived or preserved
trim, polarity, delay, tuning owner, and playback/domain/capture context. Apply
carries the candidate identity the user reviewed. Under the shared DSP writer
lock, the backend recompiles and re-proves that identity, then re-hashes the
written config immediately before load. If working state is already candidate
B at that locked reread boundary, a request carrying reviewed candidate A is
refused and the review refreshes. Once A is re-proven, only A's exact config
hash may load; B can never load under A's confirmation.

Working crossovers and candidates are strictly silent: the crossover preview
emits no audio and no CamillaDSP YAML, may not stage or load a graph, and may
not authorize playback (enforced today by
`jasper.active_speaker.crossover_preview`'s no-audio permissions). Reviewing a
proposal must remain possible even while safety gates block staging or
playback — a blocked speaker can still show what JTS is thinking.

### Parameter provenance

Every candidate should be able to explain where each value came from:

- `manual` — entered or edited by the user;
- `measured` — derived from the current measurement session;
- `recommended_start` — a conservative starting value from driver/profile
  constraints, not yet proven acoustically; or
- `preserved` — carried unchanged from the applied crossover.

Provenance is metadata on the canonical value, not a second value. It exists so
the comparison and diagnostics can be honest.

The shipped substrate carries a narrower, per-role trim provenance
(`explicit` / `measured` / `sensitivity` / `none` in the baseline-profile
layer). Implementation migrates that vocabulary into this per-parameter one —
`sensitivity` maps to `recommended_start`, `explicit` to `manual` — rather
than running two provenance models side by side.

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

The existing `/sound/` research helper is the only research entry point. Its
legacy version-1 JSON remains accepted only as advisory prefill. A version-2
result must echo the exact server-authored request fingerprint plus every
physical target id, target fingerprint, role, and make/model; a mismatch or
stale request is refused. Both versions are untrusted proposals, not playback
permission. Before an
automatic isolated-driver measurement can run, every physical driver target
must have a separately versioned, explicitly confirmed safety profile bound to
the current topology target. That profile distinguishes:

- the hard minimum and maximum excitation frequencies;
- required high/low-pass protection, including cutoff and minimum slope;
- a conservative measurement band inside the hard excitation band;
- a crossover-search band inside the measurement band;
- bounded level, duration, repeat, and cooldown policy; and
- cabinet/radiator capability needed for any low-frequency reconstruction.

`usable_frequency_range_hz` is descriptive product information, not a hard
safety boundary. A filter cutoff is not a brick wall either: required filter
slope and the hard excitation edge remain separate facts. Missing, unknown,
unconfirmed, stale, or target-mismatched safety facts refuse new automatic
isolated-driver audio. They do not mute an already working normal-playback
graph merely because the newer commissioning profile has not been created.
Every profile field is visible and editable in `/sound/`; confirmation freezes
the normalized visible values, their target/value binding, code-owned policy
snapshot, unknowns, and field provenance. Any target, topology, output,
driver-style, make/model, or visible safety-value change invalidates the prior
confirmation. The profile always reports `authorizes_playback=false`: a later
adapter must still compose and freshly prove the stricter excitation and live
graph authorities.

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

> Put the microphone on the reference axis — the tweeter axis, or the design
> axis named by the speaker profile — at the height that axis is specified
> for, approximately one metre away when the room permits. Aim it according
> to the microphone's calibration file. Keep the microphone and speaker
> completely still until all driver and combined measurements are finished.

The instruction must state the axis and height concretely, because a few
centimetres of vertical offset moves the microphone through the crossover
lobe and produces a suck-out that looks like a design problem. The post-apply
verification capture must use the same axis and height so it compares like
with like. The exact wording may be specialized by a hardware/speaker
profile, but the default automatic flow must not ask the user to move the
microphone from driver to driver: a fixed reference-axis position is what
keeps the drivers' relative summation evidence comparable across captures.
It yields timing evidence only together with the bounded measured delay/null
walk (or a synchronized capture reference); a fixed microphone alone does
not create a shared clock.

Near-field capture of a low-frequency driver is not an advanced diagnostic —
it is the standard complement to the fixed reference-axis capture. In a
domestic room the reference-axis capture is only valid above the
reflection-free window's floor (see "Measurement validity" below), so when a
crossover region sits at or below that floor, the flow requires a near-field
capture of the lower driver, applies baffle-step/diffraction correction, and
splices it to the gated reference-axis response. What remains rejected is
the old near-field-only shape: uncorrected near-field level trims presented
as far-field truth.

JTS then performs this sequence:

1. Capture ambient noise long enough to characterize non-stationary sources
   and determine whether the measurement can proceed.
2. For each driver, derive a protected level-probe band from the applied
   crossover and driver-protection edges.
3. Gradually raise that driver's measurement level within the commissioning
   envelope until the capture shows a safe, non-clipping level with
   microphone headroom. The probe sets level only; the SNR verdict comes
   from the deconvolved sweep, per band.
4. Play a protected logarithmic sweep through that driver only, with a
   driver-appropriate sweep length (longer for woofers, bounded short for
   tweeters).
5. Repeat the sweep three times without moving the microphone.
6. Reject clipped, incomplete, stale-graph, wrong-driver, or low-SNR captures.
7. Gate each accepted impulse response to its measured reflection-free window
   and record the resulting low-frequency validity floor.
8. Aggregate accepted repeats robustly and retain their spread.
9. Continue to the next driver using its own safe probe and locked level.
10. Measure all drivers in a crossover region together, first in the candidate
    polarity and then with the bounded reverse-polarity validation needed for
    alignment.
11. Produce a measured proposal and a plain-language explanation.

Three exact-position repeats are the normal crossover default. Their purpose
is outlier rejection and a variance estimate — catching the door slam, the
furnace kick, the drifted capture — not noise-floor reduction: acoustic-noise
headroom comes from the level/SNR policy, never from raising the repeat
count. Cross-repeat coherent averaging requires a shared sample clock between
playback and capture, which today's browser and USB capture paths do not
have, so analysis defaults to robust magnitude statistics while preserving
the individual complex records; coherent aggregation may be enabled only
behind a real shared-clock/loopback gate. If one of three repeats is
rejected, the flow re-captures once to restore three, or proceeds with two
and widens the reported confidence. It must never average moved microphone
positions into phase/alignment evidence.

### Level control and SNR

Auto-level is driver-specific because a woofer, midrange, and tweeter have
different safe and useful bands. Tone/probe selection remains owned by
`jasper.active_speaker.test_signal_plan`; UI or relay code must not duplicate
frequency rules.

The controller should:

- begin at the bounded quiet level;
- use a short band-limited probe or preview sweep representative of the
  driver's useful measurement band to find a safe, non-clipping capture level
  with microphone headroom — the probe owns level safety, not the SNR
  verdict;
- raise gain gradually within the existing commissioning envelope;
- choose a driver-appropriate sweep length (longer sweeps buy low-frequency
  processing gain on a woofer; tweeter sweeps stay short and protected —
  sweep length is a protected parameter of the safety floor, like level);
- retain microphone peak and clipping headroom; and
- keep a visible Stop action while sound is playing.

The SNR verdict is computed after deconvolution, per band, from the accepted
sweep against the stored ambient capture — never from the raw probe, which
cannot see the sweep's band-dependent processing gain. The ambient capture
must be long enough (or repeated) to characterize non-stationary noise, and
the noise reference uses a percentile or maximum rather than one short quiet
window.

SNR thresholds are split by decision class, band-specific, and evaluated on
the worst band the decision depends on:

- **Magnitude and trim decisions** target at least 25 dB SNR as a floor
  (prefer 40 dB or better where the room and level allow), accept 20–25 dB
  with an explicit reduced-confidence result, and stop below 20 dB with a
  report of how many decibels are missing. These tiers correspond to roughly
  ±0.5 dB and ±0.9 dB magnitude accuracy.
- **Null and alignment decisions** (reverse-polarity depth, delay-walk
  evidence) need substantially more: a null of depth D cannot be measured
  with less than about D + 10 dB of SNR in the overlap band. Alignment
  evidence therefore requires the overlap band to reach approximately
  35–40 dB SNR, or the reported null depth is capped at the measured noise
  floor and the alignment verdict degrades to "review" — never "aligned".
- The accept/reduce/refuse verdict is per band, with defined partial-pass
  semantics: a woofer run may be good from 150–800 Hz, reduced 80–150 Hz,
  and short below 80 Hz, and the proposal consumes exactly that rather than
  refusing the whole capture over its worst octave.

A 1 kHz scalar level is not sufficient evidence that a broadband room or
driver sweep has 20 dB SNR. Raising microphone gain does not fix acoustic
SNR when it raises room noise and signal equally. Low-frequency shortfall in
the woofer's bottom octave is a NORMAL outcome in evening and urban rooms —
residential noise is heavily low-frequency-weighted — so the shortfall path
is ordinary product behavior, not an error state: name the actionable lever
(quiet the room, accept reduced low-frequency confidence, or raise the level
within the envelope).

### Measurement validity: gating and the low-frequency floor

A domestic room contaminates a far-field capture with reflections a few
milliseconds after the direct sound. Analysis must therefore gate: detect the
first strong reflection in each accepted impulse response, window the
response to the reflection-free span, and record the resulting low-frequency
validity floor (approximately the reciprocal of the window length — a 4 ms
window resolves nothing below roughly 250 Hz). At one metre in a typical
room the floor lands near 215–285 Hz, set by the floor bounce.

The validity floor is a first-class result, reported with the same honesty
as SNR:

- every derived quantity (level, magnitude shape, polarity, delay, proposal
  scoring) is computed only from data above the floor;
- when a crossover region sits at or below the floor, the reference-axis
  capture alone cannot decide it — the flow requires the near-field capture
  plus baffle-step correction and splice described in Step 3B, and says so;
- the review screen and evidence bundle record the achieved window, the
  floor, and whether each proposed crossover frequency sits above it; and
- a proposal whose frequency the room prevented measuring is marked
  reduced-confidence or refused with "the room prevented a low-frequency
  decision here", never silently emitted.

#### Low-frequency reconstruction contract: sealed single radiator v1

The first reconstruction success path is deliberately narrower than the
ordinary `near_field` capture label. That label and its placement proof are
useful for repeat identity and level diagnostics, but they attest an operator
instruction rather than a measured microphone distance. They are not by
themselves reconstruction evidence.

`sealed_single_radiator_v1` is the only admitted cabinet model. It requires all
of the following immutable, positive geometry:

- `enclosure_kind = sealed` and `radiator_count = 1`, with no port, passive
  radiator, coupled second cone, or other acoustic source;
- effective circular radiating diameter `D`, front-baffle width `w`, and
  front-baffle height `h`, all in millimetres;
- a near-field microphone distance `r_nf`, measured from the diaphragm/dust-cap
  centre to the microphone capsule; and
- a reference-axis distance `r_ff`, measured from the baffle plane to the
  microphone capsule, and a known reflection-free validity floor, using the
  same microphone/calibration identity and immutable applied crossover as the
  near-field capture.

The frozen version-1 safety profile remains the authority for enclosure kind,
source count, effective diameter, and baffle width; it is not silently extended
with baffle height. Height lives in a separately versioned, operator-confirmed
`jts_active_speaker_reconstruction_geometry` artifact bound to the exact target
id/fingerprint, topology fingerprint, and confirmed safety-profile
fingerprint. Both distances live in immutable
`jts_active_speaker_reconstruction_placement_proof` artifacts bound to their
exact capture identity and the same context. They are numeric ruler
measurements entered by the operator, not sensor claims. The current
operator-attestation placement proof and its nominal 3 cm instruction cannot
be reinterpreted or upgraded into either artifact.

The model uses `a = D/2`, metres for the equations below, and `c = 343 m/s`.
The near-field sample is valid only when `0 < r_nf < a/20`; the far-field
sample is valid only when `r_ff > 6a`; and reconstructed near-field bins stop
at `ka = 2πfa/c <= 0.8`. The distance rules follow the conservative ARTA
free-field reconstruction procedure; the `ka <= 0.8` product threshold keeps
a 20% margin inside Keele's low-frequency piston condition `ka < 1`. An
operator acknowledgement without the persisted numeric ruler value, a nominal
instruction, a driver model's frame diameter, or an inferred phone dimension
never substitutes for a measurement.

The selected cabinet correction is ARTA's rectangular-baffle low-frequency
diffraction approximation. Define the square-equivalent baffle dimension

`d_eq = w (h/w)^(1/3)`

and `f0 = 34.16/d_eq` with metres and hertz. Convert the half-space near-field
response to an on-axis full-space magnitude estimate with

`B_db(f) = 20 log10 |(1 + j f/f0) / (2 + j f/f0)|`.

This is a broad 4π-to-2π baffle-step model: its magnitude approaches -6 dB at
low frequency and 0 dB at high frequency. It is not an edge-diffraction-ripple,
off-axis, directivity, or arbitrary-box simulator. No fitted frequency-shaped
correction may be learned from the overlap. A different enclosure/source shape
needs its own reviewed reconstruction contract rather than a permissive flag.
The spliced product is magnitude evidence only. It cannot supply relative
phase, polarity, delay-walk, reverse-null, or shared-clock evidence; those stay
separate prerequisites for candidate evaluation. Do not synthesize the model's
complex phase. The later scalar overlap alignment subsumes absolute
near-to-far distance scaling. The ARTA approximation is admitted only in its
stated source-size domain, `1/40 <= (a/d_eq)^2 <= 1/10`.

Splicing is deterministic and fail closed:

1. Let `L` be the greatest of `1.25 * f_valid_floor` and both responses' lower
   support bounds. Let `U` be the least of the near-field `ka = 0.8` limit and
   both responses' upper support bounds. Refuse unless `U/L >= 2`.
2. Select exactly one octave: `f_lo = min(max(f0, L), U/2)`, `f_hi = 2 f_lo`.
   Refuse unless its overlap with the model transition `[f0, 2f0]` spans at
   least 1/12 octave:
   `log2(min(f_hi, 2f0) / max(f_lo, f0)) >= 1/12`. Resample both calibrated
   responses at exactly the 13 points
   `f_i = f_lo * 2^(i/12)`, for integer `i` from 0 through 12. Missing or
   non-finite coverage is not extrapolated.
3. Add `B_db` to the near-field level, then align it to the far-field
   response with exactly one scalar: the median far-field-minus-corrected-
   near-field level over the overlap. Do not peak-normalize either response.
4. After scalar alignment, refuse unless the overlap residual has RMS no more
   than 1.5 dB, maximum absolute error no more than 3.0 dB, and absolute
   ordinary-least-squares slope against `log2(f)` no more than 1.5 dB/octave.
   These are admission thresholds, not optimizer penalties. Both captures must
   also carry affirmative admission/quality verdicts and at least 25 dB
   magnitude-decision SNR throughout the selected octave.
5. Blend level in dB across the complete overlap with
   `x = log(f/f_lo) / log(f_hi/f_lo)` and
   `w(x) = (1 - cos(πx))/2`: corrected near field has weight `1-w`, gated far
   field has weight `w`. Below the overlap use corrected near field; above it
   use gated far field. Preserve the source identities, scalar offset, model
   version, bounds, residuals, and thresholds in the replay result.

A successful reconstruction serializes these labels exactly:

- `model_id = sealed_single_radiator_v1`;
- `response_domain = magnitude_db_only`;
- `amplitude_reference = played_excitation_normalized`;
- `peak_normalized = false`;
- `active_electrical_crossover_included = true`;
- `natural_driver_plant_isolated = false`;
- `phase_available = false`;
- `authority = admitted`; and
- `threshold_set_id = sealed_single_radiator_v1`.

The result fingerprint covers those labels plus the numeric thresholds and
input identities. `authority = admitted` is limited to the reconstructed
magnitude evidence and does not confer candidate, apply, verification, receipt,
or playback authority.

The reconstruction capability exposes stable typed refusal slugs:

- `reconstruction_profile_unconfirmed`, `reconstruction_profile_stale`,
  `reconstruction_target_mismatch`, `reconstruction_topology_mismatch`,
  `reconstruction_geometry_binding_mismatch`,
  `reconstruction_placement_binding_mismatch`,
  `reconstruction_applied_crossover_mismatch`, and
  `reconstruction_calibration_mismatch`;
- `reconstruction_capture_not_admitted`,
  `reconstruction_capture_quality_refused`, and
  `reconstruction_capture_snr_insufficient`;
- `reconstruction_enclosure_unsupported`,
  `reconstruction_source_count_unsupported`, `reconstruction_geometry_missing`,
  and `reconstruction_geometry_model_domain_unsupported`;
- `reconstruction_near_field_distance_missing`,
  `reconstruction_near_field_distance_out_of_range`,
  `reconstruction_far_field_distance_missing`, and
  `reconstruction_far_field_distance_out_of_range`;
- `reconstruction_far_field_validity_floor_unknown`,
  `reconstruction_overlap_missing`, `reconstruction_overlap_too_narrow`,
  `reconstruction_overlap_transition_uncovered`, and
  `reconstruction_overlap_non_finite`;
- `reconstruction_overlap_rms_exceeded`,
  `reconstruction_overlap_peak_exceeded`,
  `reconstruction_overlap_slope_exceeded`, and
  `reconstruction_decision_band_uncovered`.

A candidate decision that needs reconstructed lower-driver data must have
continuous admitted coverage across its complete `[Fc/2, 2Fc]` scoring band.
A refusal never falls back to an uncorrected near-field curve.

Wave 2's historical B2b captures are permanently non-admitted legacy
replay/debugging evidence. A strict replay may replay the exact current applied
winner from its immutable WAV, excitation, calibration snapshot,
placement proof, and applied crossover, normalizing only the declared scalar
playback gain. That replay must say `authoritative = false`, must not synthesize
the missing measured-distance or admission evidence, and cannot authorize a
splice, candidate, apply, verification, or eligibility receipt. Synthetic
fully admitted fixtures may exercise a pure candidate evaluator, but they do
not change current product readiness: until real captures satisfy every gate,
`ready = false`, no automatic candidate is persisted, and the UI does not
offer **Replace with measured crossover**.

Wave 2 deliberately stopped before implementing a score. Its typed input contract
pins the prerequisites that must precede any future frequency/family evaluation:
three distinct admitted stationary captures per required driver/normal/reverse
target, five distinct admitted null captures per graph-confirmed delay, a
50–100 µs walk bounded by declared geometry ± half a crossover period, and a
profile intersection tightened by measured per-band validity and SNR. Placement
is stable per topology-derived speaker group, not falsely global across stereo
cabinets. The future graph proof is topology-wide and exact: routing, complete
filter chains, gains, permanent protection, and `devices.volume_limit <= 0` all
belong to the same proof. Fresh persisted admission must be issued from Active's
exact current safety plan and independently rechecked by the playback backend.
The first Wave 3 production slice adopts those Shared boundaries for an
**isolated driver capture only**. `active_speaker.commissioning_admission`
turns the former preparation-only plan into the trusted adapter: it intersects
the code and confirmed-profile frequency/level/duration/repeat limits, binds the
exact current topology, applied profile, comparison session, volume lock, and
fresh running-graph fingerprint, then proves the required directional filters,
per-output limiter, exact admitted per-output commissioning gain, capture route,
audible/mute mask, headroom, and a `devices.volume_limit` no higher than the
locked listening volume from a non-best-effort CamillaDSP readback. That
transient ceiling makes the output envelope independent of a racing dial or
control-process main-volume write; a fresh post-play volume read rejects any
remaining quieter drift without issuing capture authority. The profile's
minimum cooldown is enforced as a cancellation-safe pre-play wait on every
one-shot attempt; values above the bounded five-second automatic ceiling are
refused rather than clipped. That ceiling is derived from the phone relay's
armed-to-sweep deadline after reserving the controlled ambient interval, the
longest protected sweep, and nine seconds for graph/relay operations. The
browser cannot supply protection or capture-admission authority.

Every fresh commissioning bundle is created once as the exact Shared admission
authority. Existing directories without its marker remain historical and the
reopen path never repairs or upgrades them. Each driver attempt mints a unique
`admission_id`, persists generation admission before allocating PCM, writes one
no-replace role-bounded WAV, and binds its exact bytes. Active holds one bounded
Shared DSP-writer lock across transient graph load, both live readbacks,
`play_admitted_wav()`, fan-in cleanup, and production-graph restoration. The
playback issuer rebuilds the current plan and protection evidence under that
same lock, so profile, topology, comparison, applied-profile, pre/post volume, graph,
filter, route, or target drift refuses before audio. A persisted playback-role
artifact—not the generation decision—is threaded through the server-side relay
flow and revalidated before capture persistence. Cancellation cannot bypass
restoration; after playback admission has persisted, timeout, cancellation, or
failure returns typed possible-audio authority and consumes the one-shot
identity.

Positive schema-v2 receipt validation treats every post-apply raw,
analysis-input, quality, generation-admission, and playback-admission identity
and relative path as one global namespace across all required speaker groups.
Cross-target reuse is refused even when a colliding raw path names different
bytes.

The isolated-driver playback slice does **not** make the candidate ready.
The later Wave 3 control-plane integration now starts and exposes a durable
`unconfigured` commissioning run only from the fresh bundle-backed comparison
set, claims its process owner at correction-web startup, and classifies a
different active comparison as stale. It still has no production measurement
attempt/transition orchestrator. The pure per-region evidence contract now
pins the exact normal/reverse/delay capture sets that orchestrator must issue,
but does not play, capture, persist, or score them. Combined/summed
capture remains pre-audio refused with
`active_summed_persisted_admission_unavailable` until the group-level protection
host and persisted per-region authority land. Historical B2b captures remain
permanently non-admitted, and current projections still expose no candidate,
apply, verification, receipt, or Room authority until the later Wave 3 gates
are satisfied.

### Wave 2 level-run correlation and timeout boundary

`jasper.active_speaker.crossover_level_run` owns schema-v1 identity and durable
progress for crossover level checks. This state is deliberately separate from
the durable listening-volume safety latch. Run correlation answers *which exact
request is this callback about?*; the volume latch remains the only authority
for exact restore or emergency attenuation after a possible volume mutation.
Neither state may stand in for the other.

Before relay transport opens, Active freezes one
`jts_active_crossover_level_run_request` containing the exact topology id,
confirmed protected-profile fingerprint, physical target id and fingerprint,
canonical capture geometry, and the complete replayable `MeasurementRamp`
configuration. The request and ramp config each have a canonical SHA-256
fingerprint. The Pi safety timeout is `ceil(ramp.safety_timeout * 1000)`; the
phone hard timeout is
`ceil((ramp.safety_timeout + 30 seconds transport/setup grace) * 1000)`. The
30-second grace is a conservative hardware-unverified Wave 2 value, not a
measured latency claim; Wave 4 must test the real phone/relay tail. Planning and
execution consume that same serialized ramp config. A changed environment after
claim cannot silently change the executing ramp.

The durable current-run phases are exactly `awaiting_phone`, `running`,
`succeeded`, `failed`, and `interrupted`. One atomic claim mints the run id; that
same id is the existing token-scoped relay `run_token`. An identical active
request returns `duplicate_active`; an identical current success returns
`duplicate_succeeded` only while the same service owner still holds the
process-local level result; invalidating the comparison context or discarding
that exact driver result removes the in-memory success marker, so the next
identical claim is new. Neither duplicate disposition may dispatch transport or
backend work. A different request cannot replace active work. Backend start is
also single flight. Only exact-current-run callbacks may mutate state; stale
terminal callbacks are ignored. At service startup, a nonterminal prior-owner
run becomes
`interrupted/service_restarted` before any retry may claim the slot. A prior
owner's terminal success does not deduplicate because the corresponding level
lock is process-local and must be reacquired after restart.

A token-matched armed batch advances the run to `running`. A token-matched
`phone_timeout` **before** backend start terminally refuses the run as
`failed/phone_aborted`: the live phone feed and clip guard have ended, so audio
must not start. A timeout recorded **after** backend start is an observation, not
premature proof of backend failure: the same already-bounded action may still
complete its final persistence. If that exact run later succeeds, it is stored
as `late_success=true`. The relay poll may observe an already-posted same-token
timeout just after success persistence; that exact-current-owner callback may
annotate the terminal success as late, but never reopens or redispatches it.
Unrelated or stale success cannot be relabeled. Success is allowed only after
both token-matched phone arming and single-flight backend start, and only after
the complete crossover action—including level-lock or comparison-set
finalization—has returned. Ordinary action exceptions become a typed terminal
failure at the adapter. The durable file and public status retain no tap link,
pull token, relay credential, raw phone events, or calibration payload;
`/state.level_match.run` exposes only safe bindings, timing fields, phase,
timeout observations, terminal reason, late-success status, and whether the
same process still holds the result needed for terminal deduplication.

The required Room-owned `correction_setup.py` transport adapter is intentionally
not part of Active's Wave 2 changes. Its exact integration is:

1. after the existing current topology/profile/target validation, call
   `CrossoverLevelLease.claim_level_match_run`; bind Room's process-local relay
   holder to both kind `level_ramp:crossover` and `claim.run_id`; if
   `should_dispatch` is false, return the existing run/status plus a tap link
   only when **both** those values match, without calling `_run_relay_capture`
   (the existing kind-prefix-only lookup is insufficient and must not return a
   prior same-kind run's link);
2. pass `claim.run_id` to `build_level_ramp_spec` as `run_token` and
   `claim.phone_hard_timeout_ms` as `hard_timeout_ms`; also pass the same id
   explicitly through `_run_relay_level_match(..., level_run_id=claim.run_id)`
   to `CrossoverLevelLease.run_level_match`; the Active lease never infers
   claimed authority from public status, and any explicit stale/mismatched id
   refuses instead of falling back to current environment config;
3. while polling, notify `mark_level_run_phone_armed` once for the matching armed
   batch and `mark_level_run_phone_timeout` only when the matching batch's abort
   reason is `phone_timeout`;
4. call `mark_level_run_succeeded` only after the complete `_run` callback and
   its lock/comparison-set persistence return; call `mark_level_run_failed` for
   ordinary exceptions, including foreground relay registration failure, before
   re-raising, using the matching `CrossoverLevelRunFailure` member
   (`RELAY_REGISTRATION_FAILED`, `PHONE_ABORTED`,
   `LEVEL_MATCH_ACTION_FAILED`, or `FINALIZATION_FAILED`); and
5. call `correction_crossover_backend.claim_level_run_owner()` beside the
   existing repeat-admission owner claim at correction-web service startup.

`_run_relay_capture`, relay credentials, capture-page lifecycle, and the generic
phone transport remain Room-owned. Active supplies only the narrow claim,
notification, terminal, status, and frozen-config consumer APIs.

Single-position capture also cannot observe vertical lobing: off-axis and
directivity behavior are outside the single-position tier's evidence (First
principles item 7 applies only where the measurement tier can observe it).
An optional vertical fan capture (roughly ±15–30°) is the smallest set that
can reveal a crossover lobe and may be offered as a deliberate extra step;
the automatic proposal treats it as a veto input when present (see "Building
the automatic proposal").

### Building the automatic proposal

Automatic design should be deterministic and bounded. It selects the best
supported **electrical** filter candidate — it does not claim to synthesize
a textbook acoustic target. Without per-driver EQ (out of scope for the
first release), a real driver's baffle step, breakup, and natural rolloff
mean an electrical Linkwitz–Riley does not produce an acoustic
Linkwitz–Riley; credible active-crossover practice treats driver
linearization as a prerequisite of acoustic-target design. The honest
first-release deliverable is therefore the best electrical
family/order/frequency plus trim, polarity, and delay, scored on the
measured summed response, and presented as a suggested starting point the
user reviews.

The proposal considers:

1. a measured usable overlap range in which both drivers are present,
   sufficiently clean, and above the low-frequency validity floor;
2. crossover frequencies inside that range;
3. the electrical filter families/orders already supported by
   `ActiveSpeakerPreset`, defaulting to even-order Linkwitz–Riley; an
   odd-order or asymmetric candidate may not be chosen on single-axis
   evidence alone — it requires off-axis (vertical-fan) evidence acting as a
   tie-breaker or veto;
4. relative trim across the overlap, not unrelated single-frequency levels;
5. polarity and relative delay that produce stable summation — a measured
   delay value from the bounded delay walk is a prerequisite for any
   automatic frequency/family choice, because lobe and null reasoning is
   untrustworthy without it;
6. combined-response deviation around the crossover;
7. reverse-polarity null quality where applicable;
8. bounded filter/headroom cost; and
9. off-axis evidence as a veto, when the user deliberately captures it.

When the measured overlap shows a driver that cannot blend without shaping —
a baffle-step-dominated region, breakup, or a steep natural-rolloff
mismatch — the proposal refuses with "this pairing needs per-driver EQ,
which the builder does not yet design" rather than emitting a confident but
unrealizable filter. The score is labeled **on-axis blend quality (single
fixed axis)** in the review screen and evidence bundle, so a good number is
not mistaken for a verdict on the whole polar behavior.

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

Wave 1 did not add a generic graph-transaction framework. The exact rollback
state identity reuses the existing
`jasper.audio_measurement.null_walk.DspPredecessor` JSON/fingerprint convention.
Holding the live writer lock,
applying, reading back, and restoring that exact predecessor remain the owning
Active adapter's later integration work.

After apply, JTS measures the combined crossover again at the same fixed
reference position (same axis and height as the commissioning captures).
Verification compares like with like and records the result
against the applied profile fingerprint. A failed acoustic verification does
not silently declare success; the user can restore the previous crossover or
return to edit/measure.

If JTS cannot confirm restoration of the listening volume after any crossover
measurement, the crossover flow fails closed: playback, capture, and apply are
replaced by one recovery action until fresh DSP readback confirms either the
exact prior level or the bounded emergency attenuation.

Once the crossover is applied and verified, the UI offers **Continue to room
correction**. Room correction then defaults to six distinct listening-area
positions. Those room positions are spatial samples and are intentionally
different from the stationary repeats used for crossover commissioning.

## Architecture and ownership

Each concern has one owner. The browser orchestrates user intent; it does not
become an acoustics engine.

| Concern | Owner | Must not own |
|---|---|---|
| Editable and applied crossover semantics | `jasper.active_speaker` preset/profile models | Browser-only settings or relay state |
| Research proposal and confirmed driver-safety profile | `jasper.active_speaker` | Applying DSP, authorizing audio, or hiding imported values from operator review |
| Safe driver probe/sweep plan | `jasper.active_speaker.test_signal_plan` and graph-safety policy | Page-specific frequency branches |
| Pure excitation-admission decision | `jasper.audio_measurement` | Speaker topology, research parsing, live DSP mutation, or product sequencing |
| Sweep generation, deconvolution, calibration, quality math | `jasper.audio_measurement` | Product sequencing or profile application |
| Driver, overlap, summed-response, and alignment analysis | `jasper.active_speaker.driver_acoustics` / alignment modules | HTTP, relay transport, or live DSP mutation |
| Durable commissioning run and evidence identity | Active-speaker measurement/session layer | Filter compilation |
| Candidate derivation and provenance | Active-speaker baseline/candidate layer | Playback side effects |
| Sequential product envelope | `jasper.web.correction_crossover_*` | Acoustics math or duplicated state machine in JavaScript |
| Phone/browser capture transport | Jasper capture relay | Tone selection, analysis, or product policy |
| CamillaDSP emission | Existing active-speaker compiler | Inferring measurements or user intent |
| Atomic apply and rollback | Shared DSP transaction/runtime boundary | Editing working drafts |
| Verified active-crossover eligibility receipt | `jasper.active_speaker` | Room-owned inference or relabeling of crossover state |
| Room correction | `jasper.correction` | Driver-domain crossover repair |

### Hardware research and confirmed safety profile

The existing `jts_active_crossover_driver_research` packet evolves in place; do
not create a parallel Room-owned research wizard or let an LLM write a live
graph. JTS binds a research request to the current physical target identities,
the returned packet echoes those identities and uses explicit unknowns, and the
server strictly validates it. Every safety-relevant value is visible and
editable. Confirmation freezes the normalized values and their field-level
provenance into an immutable fingerprint; any topology/output assignment or
value change requires confirmation again.

The version distinction is intentional: version 1 is legacy advisory research;
version 2 is bound to a current server request. Neither is authority. The
confirmed `jts_active_speaker_driver_safety_profile` is also distinct from the
older code-owned `driver_protection_profile` tone/ramp envelope and from
`/sound/active-speaker/channel-protection`'s physical/software-guard fact. Later
playback must satisfy all applicable layers; none may be relabeled as another.

The protected starting graph, excitation plans, captures, candidates, apply
records, verification records, and downstream eligibility receipt all reference
that fingerprint. Research may recommend a conservative starting crossover,
but deterministic acoustic evidence owns any automatic replacement. Research
can tighten global code-owned limits; it can never raise them.

Cabinet capability is explicit. The first low-frequency splice success path may
support a declared sealed single-radiator system with sufficient geometry.
Vented, passive-radiator, multi-radiator, or unknown systems receive a typed
refusal until their complete acoustic-source contract exists. Empirical overlap
alone never licenses JTS to infer enclosure physics.

The shared measurement layer receives only normalized immutable bounds and
returns a typed admission verdict. It never receives the design draft, the
browser, CamillaDSP, or a powerful active-speaker host object. The active-speaker
host independently verifies the exact target and required protection through
fresh graph readback before audible work. This is deliberate defense in depth,
not two competing sources of truth.

`jasper.audio_measurement.excitation_admission` is the Wave 1 leaf for that
decision. Its strict request, limits, protection evidence, and verdict bind the
exact target, safety profile, authority, excitation plan, closed frequency band,
effective peak, duration, and repeat count. Their SHA-256 fingerprints are
content identities, not signatures or capabilities. A trusted owning adapter
must intersect code-owned, profile-owned, and plan-owned limits; bind the plan
to normalized generator/effective-peak inputs; derive protection evidence from
fresh readback; and rerun admission immediately before playback. The current
isolated-driver producer performs that integration through
`active_speaker.commissioning_admission`; summed capture and the full lifecycle
remain blocked pending their distinct group-level authority. Shared's
`play_admitted_wav()` performs the independent playback-side recheck and
persistence once that trusted Active callback supplies the fresh values.

### Wave 1 evidence, lifecycle, and Room handoff contracts

The shared `ArtifactIdentity`, `CaptureIdentity`, and `ReplayIdentity` values
bind exact feature-owned artifacts, raw captures, replay inputs, and algorithm
versions. They do not move files, migrate either existing bundle format, decide
capture quality, or turn a forensic bundle into authority. Room and Active keep
owning their bundles and verdicts.

Active's lifecycle has nine explicit states: `unconfigured`, `protected`,
`measured`, `candidate_ready`, `applied_unverified`, `verified`, `blocked`,
`blocked_live_state_unknown`, and `rolled_back`. Each positive transition is
bound to the expected evidence kind. Once mutation begins, an attempted or
unknown outcome cannot fall back to ordinary pre-mutation `blocked`; it enters
`blocked_live_state_unknown` and can leave only through exact restore evidence.
That prevents durable state from forgetting that the live graph may be
uncertain.

The positive `CommissioningEligibilityReceipt` is deliberately demanding. Its
required targets are derived from a current, evaluated-`verified`
`OutputTopology`'s combined active speaker groups, not supplied by the caller;
blocked or physically unverified maps cannot create target authority. Every
required target must pass one
post-apply verdict over exactly three distinct, admitted, fixed-reference-axis
captures from one commissioning session and threshold profile. Each capture
retains its own unique admission id and canonical generation- and playback-role
artifacts; both admissions authorize exactly one playback, and the playback
decision must retain the generation request and limits. The receipt also binds
the confirmed safety profile, applied candidate, expected and freshly read
back normalized graph, exact predecessor state, and an honest retained-apply
rollback outcome bound to that same operation, mutation, and observed applied
graph. A failed, restored, attempted, or unknown mutation cannot mint the
positive receipt.

The admitted-capture, post-apply-target, and eligibility-receipt containers are
schema version 2. There is no schema-v1 migration because no production issuer
or persisted receipt exists yet; version 1 is rejected rather than guessed.

The Wave 1 transition and receipt values remain pure contracts. Wave 3 now
persists the lifecycle's exact current-run identity in
`active_speaker_commissioning_run.json` and projects it as the
`commissioning_run` block on the crossover status surface. A production run is
created only with a fresh bundle-backed comparison set; owner generation is
claimed at correction-web startup, and stale run/attempt callbacks cannot
commit. The store can persist bounded target-attempt reservations and a
hash-chained transition journal, but no production measurement orchestrator
uses those two mutation APIs yet, so the live run remains `unconfigured`.

Current Active bundles remain forensic and fail-soft, and no production code
issues or persists an eligibility receipt. Room's current
`active_speaker.setup_status` producer still derives readiness from the legacy
topology-current applied-recomposition snapshot, but the R1b Room adapter no
longer accepts that positive result for active topologies. It admits only
passive/not-required and blocks active entry until the integration lane changes
the producer and consumer together. Room neither parses the receipt nor derives
authority from historical B2b evidence; fresh excitation-admitted captures and
the measured delay walk remain Active-owned prerequisites.

### DRY invariants

The implementation should have exactly:

- one crossover parameter vocabulary (`ActiveSpeakerPreset`);
- one editable working crossover;
- one confirmed safety profile per physical driver target;
- one driver-safe signal planner;
- one pure excitation-admission contract;
- one sweep/deconvolution implementation;
- one measurement-quality model with consumer-specific policy values;
- one candidate compiler;
- one live-DSP transaction;
- one current applied-profile fingerprint;
- one Active-owned verified eligibility receipt consumed by Room; and
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

The full household summary below remains the target surface. The crossover
status now carries a narrower fail-closed `commissioning_run` control-plane
projection: exact session/run identity, owner generation, lifecycle state,
attempt count, last transition, update time, and state fingerprint are returned
only when the durable artifact validates; the comparison must also pass its
complete schema/fingerprint and match the current topology and protected
profile. An absent file is `not_started`, a comparison mismatch is `stale`, and
corrupt/unreadable state is `unavailable`.
Process owner id and raw evidence are not exposed. This block is not an
eligibility receipt and does not change Room's entry decision. Once candidate,
apply, verification, and receipt producers land, `/state` or the existing
active-speaker aggregation should additionally expose a small
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

The shipped crossover flow already logs under the `correction.crossover_*`
namespace (`correction.crossover_driver_capture_sweep`,
`correction.crossover_summed_capture`, `correction.crossover_relay_recorded`,
and friends, all via `jasper.log_event`). New lifecycle events stay under the
existing `correction.*` family rather than starting a bare parallel
`crossover.*` prefix. The durable Active store uses the
`correction.active_commissioning_*` family for successful run, owner, attempt,
and transition commits; status polling is silent.
Operators already grep the shipped correction names, and the log-event
conventions test pins the mechanism. Log important transitions once using stable `event=` names,
including:

- `correction.crossover_session_started`;
- `correction.crossover_level_locked` or `correction.crossover_level_failed`;
- `correction.crossover_capture_accepted` or
  `correction.crossover_capture_rejected`;
- `correction.crossover_repeat_attempt` for each bounded attempt and
  `correction.crossover_repeat_aborted` when a service restart durably
  invalidates an orphaned comparison-bound set;
- `correction.crossover_level_run_claimed` and
  `correction.crossover_level_run_deduplicated`;
- `correction.crossover_level_run_phone_armed` and
  `correction.crossover_level_run_phone_timeout`;
- `correction.crossover_level_run_completed` or
  `correction.crossover_level_run_interrupted`;
- `correction.crossover_repeats_aggregated`;
- `correction.crossover_proposal_ready`;
- `correction.crossover_apply_started`;
- `correction.crossover_apply_succeeded`;
- `correction.crossover_apply_rolled_back`; and
- `correction.crossover_verification_passed` or
  `correction.crossover_verification_failed`.

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
- Keep tweeter sweeps short and protected — sweep length is a bounded,
  protected parameter like level.
- Provide immediate Stop and bounded session expiry.
- Reject clipped recordings and stale topology/graph evidence.
- Keep automatic gain attenuation-only until positive-gain headroom and driver
  limits have a proven contract.
- Apply atomically and restore the previous known-good graph on failure.
- Do not allow room correction to proceed against an unverified or stale active
  crossover.

These protections extend established JTS boundaries through a target-bound
safety profile and one pure admission primitive. They do not create a parallel
wizard, graph compiler, or generic commissioning framework.

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

### Slice 0: measurement-validity substrate

Everything later depends on these; they land first, hardware-free and
CI-provable:

- Impulse-response gating and the per-capture low-frequency validity floor in
  the shared measurement layer.
- The band-specific, decision-class-split SNR gate in the single
  measurement-quality model.
- The three-repeat capture loop with robust aggregation, spread retention,
  and the defined rejection fallback.
- The cohesive commissioning bundle (session identity, manifest, hashing,
  the capture-through-apply-and-verify chain), ported from the
  room-correction session-bundle pattern.
- Polarity and delay as first-class persisted working-crossover values, so a
  trim-only apply can no longer reset them.
- The lifecycle events above and the `/state` summary block.

### Slice 1: honest working foundation

- Complete the manual parameter surface, including polarity and delay.
- Ensure manual apply and rollback preserve exactly the reviewed values.
- Rename current automatic behavior as **automatic driver level matching** where
  it still changes trims only.
- Keep the near-field low-frequency path first-class: baffle-step correction
  and splice onto the gated reference-axis capture.
- Require a post-trim summed capture and post-apply verification.
- Preserve manual frequency, family, slope, polarity, and delay when applying a
  trim-only proposal.

### Slice 2: automatic alignment

- Retain both normal- and reverse-polarity summed evidence per crossover region.
- Propose polarity deterministically from the relative reverse-versus-in-phase
  null margin, never from absolute phase. (The proposer is shipped and tested
  in `jasper.active_speaker.crossover_alignment`; this slice wires persisted
  paired evidence and application around it.)
- Implement the bounded measured delay walk rather than assuming independently
  started browser captures share sample timing: the candidate delay is applied
  in the Pi's DSP (exact, playback-clock-locked) and the browser reads only
  gated null depth — a magnitude feature immune to capture-clock drift. The
  walk is bounded by an a-priori delay estimate from driver geometry
  (optionally seeded by a coarse acoustic timing chirp through an
  already-verified driver — a seed, never the final value), searched within
  about ± half a crossover period so it cannot lock a full cycle off, stepped
  at roughly 50–100 µs, and gated by the repeatability check before any
  verdict. Never emit a delay value from per-capture impulse arrival times —
  browser capture jitter makes those physically meaningless.
- The walk is shared infrastructure: subwoofer↔mains delay/polarity in bass
  management rides the same implementation (see
  [`HANDOFF-correction-revision-plan.md`](HANDOFF-correction-revision-plan.md)
  §3.3), not a parallel one.
- Support every crossover region in a three-way system, not only the lowest.
- Verify the resulting sum before offering room correction.

### Slice 3: measured candidate selection

- Derive a measured usable overlap range above the validity floor.
- Search the supported electrical frequencies and families/orders, even-order
  Linkwitz–Riley by default.
- Score on-axis blend quality, alignment, headroom, and off-axis evidence
  where deliberately captured; refuse pairings that need per-driver EQ.
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
- The UI reports achieved per-band SNR, shortfall, clipping headroom, and next
  action.
- The review reports the reflection-free window and low-frequency validity
  floor, and no proposal rests on data below the floor.
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

As of 2026-07-14, JTS has much of the substrate but not the full product:

- **Wave 1 contract foundation (2026-07-13) is landed; lifecycle identity is
  partially integrated in Wave 3.** `/sound/`
  owns a revisioned, per-physical-target version-1 server request plus
  request-bound version-2 research result and visible confirmed version-1
  safety-profile shape; the pure measurement layer owns exact
  excitation admission and neutral evidence identities; Active owns the
  nine-state lifecycle and exact positive Room-eligibility receipt. Active now
  also owns a strict pure group-by-region evidence plan/set contract: it keeps
  every three-way region separate, binds each set to an immutable durable
  typed attempt and exact graph/protection/geometry context, requires one
  globally unique region set per plan target, and refuses capture/admission
  replay across normal, reverse, delay, and region roles. A new
  bounded, atomically persisted run store now binds a fresh bundle-backed
  comparison to exact session/run/owner-generation identity, attempt slots, and
  a hash-chained transition journal. Correction-web claims the owner at startup
  and exposes a fail-closed `commissioning_run` status; comparison drift is
  reported stale and prior-generation callbacks cannot commit. The production
  integration currently only begins an `unconfigured` run—there is no live
  attempt/transition or evidence producer—so the strict evidence values are not
  current acoustic authority and this does not yet replace the candidate,
  graph mutation, verification, receipt, or Room authority paths. Room's
  temporary R1b adapter admits passive/not-required and
  blocks every active topology rather than trusting the applied-snapshot
  positive. Live receipt production/consumption and on-device proof remain later
  slices.

- Manual setup exposes frequency, filter family/slope, and trim. ~~There is
  still no `/sound/` UI for polarity/delay authoring~~ Closed (P2a): the
  manual crossover editor's collapsed "Alignment (advanced)" section now
  authors per-region polarity and relative delay, validated through the same
  design-draft/preview/staging chain as every other manual field. Since
  Slice 0 the preview/preset/corrections chain persists polarity and relative
  delay as first-class working-crossover values end to end — including
  through a stereo apply, which no longer resets delay/inversion (corrections
  are only re-derived from measurement under explicit automatic tuning with
  fresh alignment evidence, never unconditionally).
- ~~The relay-guided automatic flow takes one accepted near-field sweep per
  driver.~~ Closed (Lane D): each role uses its own bounded sweep length after
  a 14-second controlled pre-sweep quiet interval, then the server admits
  three stationary
  repeats with at most one bounded fourth attempt and at least two accepted.
  The safe level probe chooses non-clipping playback headroom only; acoustic
  accept/reduce/refuse comes from per-band SNR after the signal and ambient
  traverse the same regularized inverse, signal-owned arrival window and
  reflection gate, and calibration domain. A bounded 16 kHz locator finds the
  sweep after relay latency; separate, real, equal-length full-rate signal and
  quiet crops traverse the same inverse and signal-owned gate. No prefix guess,
  tiling, zero padding, or noise-owned argmax enters the verdict. Interim
  bounded-four state is atomically persisted before playback by
  `active_speaker.repeat_admission`; bundles are forensic only. A restart
  preserves its attempts. The final measurement stores a compact repeat
  projection rather than the process-local winning attempt. A measurement-write
  failure moves `ready` to `aborted`; a failed admission-completion write does
  the same with a distinct reason. A successful abort immediately drives a new
  level check. If that abort write also fails, same-process `ready` stays
  fail-closed, blocking replay and automatic apply until the next service-start
  ownership claim retires the old owner. The
  explicit single-service startup claim aborts an old `active` or `ready` owner
  rather than guessing it complete. The envelope and direct automatic-apply
  boundary then require a new driver level check, so an interrupted comparison set
  cannot exceed the bounded four by retrying. The durable attempt count is also
  the UI count. If attempt four fails in relay/playback transport, two already
  accepted deconvolved repeats finalize through the same canonical finalizer at
  reduced confidence; fewer than two refuses.
- The Lane B fixed-reference orchestration is reachable: after near-field
  completion, the server envelope owns a fixed tweeter-axis re-level and a
  three-accepted-repeat target for every participating driver. A bounded fourth
  attempt may replace a rejected capture; the lower kernel can retain a
  two-accepted reduced-confidence aggregate for diagnosis, but automatic apply
  refuses fewer than three accepted repeats in either geometry.
  Geometry-specific level locks, repeat identities, relay acknowledgements, and
  one shared envelope/direct-apply eligibility gate prevent
  near-field/fixed-axis substitution or stale topology/profile evidence. Raw
  calibrated replay inputs are retained for the still-pending
  baffle-step/splice consumer.
- Crossover frequency, family, and slope remain operator-owned rather than
  measured automatically.
- The automatic success path requires a pre-apply combined listening check,
  but not reverse-polarity/delay/off-axis evidence or post-apply acoustic
  verification.
- ~~Existing alignment analysis is not fully reachable through persisted
  paired summed evidence.~~ Closed for the polarity/margin proposal (Slice 2):
  `measurement.py` now retains both in-phase and reverse-polarity summed
  evidence per crossover region (one polarity captured after the other no
  longer overwrites it), and `build_crossover_alignment_proposal` reaches
  every region in a three-way, not only the lowest. A 2026-07-12 hardening pass
  made the paired summary historical rather than authoritative: only records
  with full current comparison/profile proof, completed playback, blocker-free
  analysis, the exact current region/Fc and polarity slot, and the fixed
  tweeter-axis reference placement may contribute a null. Automatic baseline
  composition additionally requires affirmative per-band alignment SNR, an
  uncapped null, and an exact match to the applied preset plus corrections; it
  never consumes a capture-carried delay. The relay preserves the candidate metadata,
  but the wizard does not yet expose the per-region normal/reverse loop or load
  a transient reverse-polarity graph; the playback boundary refuses those
  candidates before audio so it cannot mislabel the unchanged applied graph.
  The bounded measured delay *walk* (a value, not just a status) and post-apply
  verification remain separate, not-yet-built pieces of Slice 2.
- The shared delay-walk substrate includes a pure candidate graph-content
  proof (`jasper.audio_measurement.delay_graph`). Active-crossover and bass
  hosts share one typed lane proof while retaining their own authoritative
  topology and emitter vocabularies: each target supplies its exact non-empty
  topology channel set, Delay filter, and a non-Delay identity filter from the
  same canonical chain. Mono roles use one-element sets; stereo role chains can
  span channels such as `[0, 2]`. A predecessor has both Delay lanes at numeric
  zero; the identity and Delay filters must each occur in exactly one shared
  pipeline step over that exact channel set; and a candidate may change only
  one bounded delay while retaining a real
  non-positive volume ceiling and every other graph value. The shared core does
  not parse active-speaker or bass filter names. This is deliberately not proof
  that the supplied `active_raw` is live or fresh. The pending F2b host must hold
  the DSP writer lock across apply → fresh read-back → typed confirmation, bind
  the result to the current run and capture evidence, and pass it to the F1
  runner. A stale/replayed content-identical graph is therefore a named future
  host contract gap, not a capability claimed by this slice. No CamillaDSP host
  adapter, capture playback, walk scheduling, geometry source, or three-way
  orchestration is wired yet.
- ~~Automatic trim application must not reset a manually applied delay or
  inversion when no new alignment evidence exists.~~ Closed in Slice 0:
  manual tuning never consults alignment evidence for these two
  sub-parameters at all. Automatic polarity tuning requires an admitted
  same-comparison-set normal/reverse pair bound to the current protected
  profile, exact applied-graph excitation ledger, affirmative overlap-band
  alignment SNR, and an uncapped null; it never consumes capture-carried delay.
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
- AES E-Library work on crossover networks for noncoincident drivers and on
  crossover filter design optimized for a listening window (cited by title;
  the AES library is paywalled and its URLs are unstable).
- [Rane Note 160: Linkwitz–Riley crossovers and lobing error](https://www.ranecommercial.com/legacy/note160.html)
- [KLIPPEL: transfer-function measurement](https://www.klippel.de/manuals/frequencyresponse-distortion/trf/trf.html)
- [KLIPPEL: loudspeaker directivity measurement](https://klippel.de/training/attachments/training8/Training_8_Measurement_of_Loudspeaker_Directivity_en.pdf)
- [D. B. Keele Jr.: Low-Frequency Loudspeaker Assessment by Nearfield
  Sound-Pressure Measurement](https://pearl-hifi.com/06_Lit_Archive/14_Books_Tech_Papers/Keele_D_B/LF_Near-field_Measurement.pdf)
- [ARTA Application Note 4: Loudspeaker Free-Field Response](https://www.artalabs.hr/AppNotes/AN4-FreeField-Rev03eng.pdf)
- [REW: making measurements](https://www.roomeqwizard.com/help/help_en-GB/html/makingmeasurements.html)
- [Dirac Live technical overview](https://www.dirac.com/wp-content/uploads/2024/06/Dirac-Live-a-technical-overview-white-paper.pdf)

The core assumptions of this design — fixed-axis geometry, no-shared-clock
timing, the repeat/SNR policy, the no-EQ deterministic proposal, and
auto-level feasibility at domestic levels — were independently validated
against the measurement literature and shipping calibration systems (REW,
KLIPPEL, VituixCAD, DEQX, Genelec/Neumann/Dirac/Trinnov) on 2026-07-11. The
gating/validity-floor requirement, the near-field splice reinstatement, the
split SNR policy, the probe-sets-level-only controller, the pinned delay-walk
bounds, and the electrical-candidate reframe in this revision came out of
that validation.

Last verified: 2026-07-14 (Wave 2 reconstruction, measured-candidate input,
preparation-only safety, level-run correlation contracts and terminal-result
liveness, permanent historical refusal, the reachable isolated-driver
Shared-admission/playback adapter and bounded writer transaction,
summed pre-audio refusal, durable bundle-backed commissioning-run identity,
startup owner-generation claim, fail-closed crossover status, strict pure
group-by-region evidence sets with typed run/attempt and geometry authority,
the bounded low-frequency coarse-plus-refinement schedule, complete-plan replay
guards, receipt schema-v2 one-shot roles,
and Room's temporary passive-only admission boundary checked against the current
implementation and cited measurement literature; no live audio, DSP mutation,
or hardware behavior was changed or revalidated.)
