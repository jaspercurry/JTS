# Active-speaker commissioning — 80/20 crossover + linearization revision

> **Status summary (2026-08-04): R15 software implementation awaits the
> independent gate and fixed-microphone JTS3 validation; it is not deployed,
> measured, or complete.** The single current campaign position lives in
> [HANDOFF-correction-revision-plan.md](HANDOFF-correction-revision-plan.md#current-position).
> This document owns the frozen R15–R20 contracts and implementation missions,
> not their live status. It defines the next implementation slices for active two-way speaker
> commissioning: protected raw-driver measurement, bounded LR4 crossover
> selection, candidate-specific driver linearization, and same-session
> verification. It changes no current behavior by itself. Current shipped
> behavior remains in
> [HANDOFF-crossover-measurement-v2.md](HANDOFF-crossover-measurement-v2.md);
> the layer architecture remains in
> [active-speaker-tuning-layers-design.md](active-speaker-tuning-layers-design.md);
> the program-wide round/status spine remains in
> [HANDOFF-correction-revision-plan.md](HANDOFF-correction-revision-plan.md).

## 1. Product outcome

Turn confirmed component facts into one measured, reversible speaker profile:

```text
confirmed safety facts
  -> protected neutral measurement graph
  -> raw woofer + HF evidence
  -> complete Fc-specific prescriptions
  -> one explicit apply
  -> crossed-branch + summed verification
```

The commissioning result belongs to the speaker. Room correction and taste EQ
remain later, separate layers. A new run never measures through an older
speaker correction merely because that correction is currently playing.

The 80/20 promise is deliberately narrow:

- preserve the existing Express journey's **seven capture actions** and four
  lateral moves;
- reuse the shipped conductor, safety rails, prompt table, retry budget,
  evidence bundles, fitter, transactional apply, Undo, and Full spatial verify;
- select **Fc only** from a small safe grid; keep LR4 and the current two-way
  topology fixed;
- make an honest local/spatial-robustness decision, not a polar, beamwidth,
  CTA-2034, or whole-room claim;
- abstain when the evidence cannot distinguish candidates.

## 2. One owner per fact

| Fact | Owner |
|---|---|
| Shipped phase/state behavior and operator recovery | `HANDOFF-crossover-measurement-v2.md` |
| Speaker/room/bass/preference layer boundaries | `active-speaker-tuning-layers-design.md` |
| Why spatially gated measurements, exclusions, and power averages exist | `flat-linearization-plan.md` |
| This revision's future measurement contracts, rounds, and issue disposition | **this document** |
| Program-wide current position and campaign ordering | `HANDOFF-correction-revision-plan.md` |
| Individual defect, evidence, and acceptance tests | the owning GitHub issue |

Older plans remain research and decision archaeology. They link here when this
revision supersedes their future sequencing; they are not rewritten to pretend
the revision already shipped.

## 3. What stays and what changes

| Surface | Keep | Revise |
|---|---|---|
| `CHECK` | channel identity, ambient/SNR evidence, level solve, capture-chain checks | bind its graph identity to the new session baseline |
| Anchor `MEASURE` | repeated gated per-driver sweeps, timing, calibration, trim/delay/polarity evidence | capture immutable pre-candidate responses through the protected neutral graph, then recover today's configured-Fc fitter input through the exact offline total-transfer composition |
| Express walk | mark, ±12 cm left/right, ±40 cm left/right, return to mark | capture both protected raw drivers at every side pose instead of a summed sweep through the live graph |
| Cloud science | exclusions, position stability, null/echo evidence, power/median checks | consume candidate-independent raw evidence or candidate-modeled sums; never make the cloud average the design-axis fit target |
| Fitter | crossover-shaped branch-input invariant, crossover-shaped targets, contribution/stopband guards, envelope, headroom accounting | keep the invariant; R15 supplies configured-Fc shaped input through exact offline total-transfer composition and R17 applies the same composition per candidate |
| Apply | transactional profile and retained Undo | stop after proposal; finish/reuse #1806's two-stage review and apply only one complete winner |
| `VERIFY` | measured-vs-model tracking and delta probe | also verify the crossed branches and absolute crossover-region result |
| Full tier | post-apply spatial cloud and honest spatial grade | retain; it measures the applied winner, not the old graph |
| Room | separate Room capture/target/filter line | later consume the verified speaker evidence; never borrow crossover-cloud positions as room positions |

The current program emitter carries the configured LR4 sections for both
branch shaping and HF protection, and the current fitter explicitly assumes
its inputs already contain those crossover shoulders. Therefore R15 may not
simply remove the configured crossover and call the old path compatible. It
must land the protected neutral graph **and use it immediately**: capture the
raw anchor, replace its measured protection transfer with the configured LR4
total transfer through §4.2's exact offline `M * C / P` math, and feed that result
into the existing crossover-shaped fitter/branch-target path. R17 later
evaluates other Fc values through the same seam. The separate defect being
removed is that pre-apply summed cloud phases
currently traverse the live production graph, so an old linearization,
alignment, Room filter, or preference filter can become part of the supposed
"before."

## 4. Measurement contracts

### 4.1 Session-owned protected baseline

Every commissioning journey mints one immutable measurement-graph identity
before the first stimulus. Pre-apply stimuli run through that **session-owned
commissioning graph**, not the production graph. Measurement playback may
temporarily activate the commissioning graph and then restore playback, but
the stored/committed production profile remains unchanged until explicit
Apply.

The commissioning graph contains only:

- the confirmed driver/output mapping;
- the declared hard excitation bands, including the HF driver's confirmed
  minimum safe frequency, as policy bounds;
- the exact confirmed `required_protection_filters`, including their declared
  family/slope semantics or a fail-closed conservative equivalent;
- limiters, conservative stimulus gain, and the existing volume ceiling;
- declared physical polarity and component facts, including passive L-pad
  attenuation.

It excludes:

- prior automatic trim, delay, polarity selection, and linearization PEQs;
- prior Room correction, bass extension, and preference/taste EQ;
- crossover **shaping** whose Fc is still being adjudicated. A protection-only
  transfer may share a filter family with a crossover, but its identity and
corner are derived only from the confirmed protection declaration and it must
not encode a selector candidate. Hard-band endpoints are never silently
substituted for a distinct declared filter.

"Neutral" therefore means **pre-candidate, not unprotected and not
mathematically flat**. The measurement-protection transfer is part of the raw
evidence identity. R15 must prove the neutral emitter against the same
component-safety, physical-output, limiter, volume-ceiling, and cleanup rails
that guard today's emitter; it may not weaken HF protection in order to obtain
an unshaped response.

The previously applied production profile is preserved as playback state and
Undo evidence. `Start over` discards journey evidence and mints a new
commissioning identity; it does not silently reuse the last journey's graph or
turn the old production graph into measurement truth.

Graph fingerprint, component/safety-profile fingerprint, calibration identity,
stimulus fingerprint, pose role, and capture identity travel together. A
candidate or verification result refuses mismatched identities instead of
guessing.

### 4.2 Anchor evidence owns coefficients

At the mark, one protected program captures at least three interleaved sweeps
per driver with one timing and gain ledger. These repeated responses own:

- the design-axis per-driver magnitude and phase;
- repeatability/SNR and the trustworthy analysis band;
- candidate-specific branch linearization;
- anchor trim, delay, and polarity;
- the predicted design-axis sum.

The drivers are measured broadly through their declared safe envelopes, but
they are not first forced universally flat. Each candidate later fits each
driver only against that candidate's acoustic branch target and only where the
driver contributes usefully.

R15 owns this anchor capture, the shared evidence schema below, and the
configured-Fc compatibility seam. For each role and shared-basis frequency
bin, define the measured neutral evidence and desired candidate-shaped fitter
input exactly as:

```text
M(f)   = plant(f) * P(f)
S_c(f) = M(f) * C_c(f) / P(f)
```

`P(f)` is the fingerprinted complex **linear measurement-protection** transfer
actually emitted for that role after the existing timing/gain ledger has
normalized known scalar gain and delay. It excludes the limiter: admission must
prove that the limiter did not engage and that the accepted capture remained
linear. `C_c(f)` is the role's complete desired candidate LR4 transfer,
produced from the same crossover-section primitives as the emitter. It is a
total replacement for `P` in the candidate model and eventual applied graph,
not another filter cascaded after `P`. `S_c(f)`, with magnitude rederived from
its complex values, is the only input handed to the crossover-shaped fitter and
branch-target path. The division is **offline evidence math only**: no inverse,
de-embedding, or recovery filter is emitted to hardware, and the applied graph
emits `C_c` once rather than `P * C_c`.

The v1 numerical-conditioning policy is fixed. On every candidate-required
trusted fitting or comparison bin, `P`, `C_c`, `C_c/P`, and `S_c` must all be
finite. A candidate is inadmissible with
`ill_conditioned_protection_deembedding` if any is non-finite, if
`abs(P) < 10^(-12/20)` (approximately `0.251189` linear, more than 12 dB of
measurement-protection attenuation), or if `abs(C_c/P) > 10^(12/20)`
(approximately `3.981072`, more than 12 dB of recovery). Equality at either
12 dB boundary is allowed. There is no clipping, interpolation, or silent bin
omission. Fingerprints bind `P`, `C_c`, the ratio-policy constants and formula,
the exact required-bin mask, `C_c/P`, and the resulting `S_c` transform. If
this drops every candidate, the ordinary `no_admissible_candidate` refusal
applies.

The configured-2-kHz golden fixture uses one stored complex plant fixture `H`
and one frequency basis for both arms. For each role independently, it uses the
role's actual legal measurement-protection transfer `P`, which need not equal
that role's configured crossover transfer. The new arm constructs
`M = H * P` and then `S_configured = M * C_configured / P`; the legacy arm is
`L_configured = H * C_configured`. It compares `S_configured` with
`L_configured` on the same conditioning-valid bins. A `P = C_configured`
identity-ratio case remains a required subfixture, but is not the premise of
generic or JTS3 equivalence: JTS3's tweeter protection may exercise that case
while its woofer has a different role-specific `P`. The fixture must not
compare two independent acoustic captures. It keeps all of these tolerances:

- maximum absolute complex-response difference at every compared trusted bin
  `<= 1e-6` linear amplitude and magnitude difference `<= 1e-6 dB`;
- exact equality for role, polarity, filter type/order/count, reason codes,
  and admissibility; absolute difference `<= 1e-6` for every deterministic
  emitted filter parameter or coefficient, trim dB, delay microseconds,
  headroom/filter-effort scalar, and fit metric;
- maximum predicted-sum difference `<= 1e-6 dB` at every compared bin.

Evidence fingerprints are expected to differ because the new schema names the
neutral source and total-transfer composition; fingerprint equality is not a
compatibility criterion. Banked corpus evidence may pin downstream prescription
equivalence under the same tolerances, but no test may demand that independent
acoustic captures match their noise at `1e-6`. Any optimizer or serializer
unable to meet one of the deterministic numeric tolerances must produce a
named, evidence-backed R15 re-scope before the tolerance changes; "materially
equivalent" without a number is not an exit.

### 4.3 One versioned evidence contract for R15/R16/R17

R15 freezes logical schema version 1. R16 appends four poses to the same
evidence set, and R17 consumes it unchanged. This is **not a second persistence
framework**: storage remains in the existing crossover evidence bundles and
uses the existing `DriverResponse`, `ProgramAnalysis`, `CaptureIdentity`, exact
DSP-state identity, accepted-attempt mapping, calibration, stimulus, and JSON
fingerprint primitives. The schema below specifies the versioned payload those
owners must expose and validate.

The raw-evidence record is `schema_version=1`,
`kind="jts_crossover_raw_evidence_v1"`, with these required fields:

| Field | Type / exact ownership |
|---|---|
| `session_id`, `session_fingerprint`, `evidence_set_id`, `evidence_set_fingerprint` | non-empty strings plus canonical SHA-256 fingerprints; the session fingerprint binds the immutable session inputs and the evidence-set fingerprint binds every fingerprinted field below |
| `measurement_graph` | protected-neutral graph artifact identity/fingerprint, exact DSP-state identity, and protection-proof identity |
| `measurement_protection_by_role` | ordered `woofer`/`tweeter` records containing the exact emitted complex `P(f)` on `frequency_basis_hz`, its filter identity, and canonical fingerprint |
| `total_transfer_composition_policy` | the exact `M * C / P` formula, finite-value rule, `10^(-12/20)` protection floor, `10^(12/20)` recovery cap, boundary inclusivity, and canonical policy fingerprint |
| `component_safety_profile_identity` | identity and fingerprint of the confirmed, current component declaration; absence or staleness is invalid evidence |
| `calibration_identity`, `stimulus_identity` | existing calibration and admitted-excitation artifact identities/fingerprints |
| `sample_rate_hz`, `frequency_basis_hz` | one positive integer and one strictly increasing finite float64 basis used by every response; no per-candidate interpolation grid |
| `configured_lr4_fc_hz`, `legal_fc_bounds_hz` | exact configured Fc and the confirmed closed `[lo, hi]` component-policy bounds, all finite positive Hz |
| `poses` | the ordered records below; pose order is fingerprinted |

The fingerprinted pose order and signed lateral-offset convention are frozen:

| `pose_id` | order | lateral offset | `pose_role` | current evidence role |
|---|---:|---:|---|---|
| `anchor_00` | 0 | `0 cm` | `anchor` | design axis |
| `left_12_cm` | 1 | `-12 cm` | `lateral` | `onax` |
| `right_12_cm` | 2 | `+12 cm` | `lateral` | `onax` |
| `left_40_cm` | 3 | `-40 cm` | `lateral` | `offax` |
| `right_40_cm` | 4 | `+40 cm` | `lateral` | `offax` |

Each pose record owns one `timing_gain_ledger` shared by both drivers and
exactly three `selector_repeat_groups` in stimulus/accepted order. Every group
pairs one accepted woofer response with one accepted HF response through that
common ledger and carries the two capture/take identities; it is never formed
by matching independent driver indexes after capture. Extra retained repeats
remain evidence but are not selector-v1 inputs. A missing or incomplete group,
or anything other than exactly three complete selector groups, invalidates the
whole evidence set.

The pose also has an ordered `drivers` mapping with exactly `woofer` and
`tweeter`. Each driver record requires: the existing target/role identity;
canonical `capture_id` and accepted-attempt/take identity; raw
`complex_response` (`complex128` real/imag on `frequency_basis_hz`, post the
stored role-specific `P(f)` but pre candidate transfer); magnitude derived from
that complex response; the ordered accepted repeat IDs and their raw complex
responses; the valid/trustworthy boolean mask and closed trustworthy band; SNR
evidence; and repeat uncertainty evidence. For selector v1, the nominal driver
`complex_response` is exactly the arithmetic complex mean of that driver's
three ordered selector-group responses at each shared-basis bin.
The timing/gain ledger includes the located segment/timing corrections,
inter-driver drift correction, stimulus gain, capture gain/level solve, and
anchor trim/delay/polarity frame needed to reproduce a candidate without
re-solving a pose independently.

R15 emits a complete, valid anchor-only record containing `anchor_00`. R16
atomically appends the other four records in the exact order above without
changing `schema_version`, field meanings, anchor bytes, or storage owner.
Schema/kind mismatch; any graph, safety, calibration, stimulus, sample-rate,
frequency-basis, session, evidence-set, pose, capture, or repeat identity
mismatch; a missing required field; or a fingerprint mismatch invalidates the
whole evidence set and R17 refuses with `invalid_evidence_identity`. It never
repairs, coerces, or mixes identities.

R15 also freezes the selector-result envelope that R17 will populate:
`schema_version=1`, `kind="jts_crossover_selector_result_v1"`. Its required
fields are `evidence_set_id`, `evidence_set_fingerprint`,
`grid_policy_fingerprint`, the fingerprints of every bound contributing to the
closed candidate interval, the interval itself, the serialized ascending
candidate `grid_fc` and its fingerprint, `metric_policy_fingerprint`, the
frozen common/per-role/per-candidate mask fingerprints and bin counts,
`repeat_realization_policy_fingerprint`, `comparison_policy_fingerprint`,
ordered `candidate_records` for every generated candidate, the complete
pairwise `comparison_trace`, all sensitivity-check results, and `outcome`.
Every candidate record contains Fc; fingerprints for `P`, `C_c`, the ratio
policy and exact required-bin mask, `C_c/P`, and resulting `S_c`; complete
prescription fingerprint; the six ordered scalar keys from §6.3 (including the
symmetric uncertainty half-width for each); and either `admissible=true` or one
or more named `inadmissibility_reason_codes`. It never contains a partial
usable prescription. `outcome.status` is exactly `selected`,
`abstained`, or `refused`, and carries `selected_fc_hz` only for `selected`,
`retained_fc_hz` only for `abstained`, plus ordered reason codes. Reporting
order is ascending Fc and is not decision authority.

### 4.4 Side evidence owns robustness, not the target

R16 retains the existing four moves: ±12 cm and ±40 cm left/right. At each
pose, one compact protected program captures both drivers on a common timing
ledger; a W–HF–W ordering may bracket drift without another user action.

The side captures may:

- limit correction where response is position-fragile;
- compare the woofer/HF relative falloff for each Fc candidate;
- predict each complete candidate's sum at all sampled positions;
- reject a candidate with a gross lateral handoff discontinuity;
- disclose strong left/right disagreement as room/boundary contamination or
  insufficient geometry.

They may not:

- be averaged into the design-axis per-driver fit target;
- re-solve trim or delay independently at every pose;
- establish beamwidth, directivity index, coverage, a certified listening
  window, or performance throughout a room.

Holding the anchor solution fixed at the sides is load-bearing: re-aligning at
each pose would erase the off-axis consequence the samples are meant to expose.

### 4.5 Room evidence remains a different instrument

Room correction starts only from a verified applied speaker profile. Its
positions describe the listening area, its target is profile-steered, and its
future broad residual is:

```text
in-room spatial response - applied speaker gated response
```

The existing read-only seam in
[`jasper/correction/applied_speaker_evidence.py`](../jasper/correction/applied_speaker_evidence.py)
is the intended boundary. The crossover cloud's small design-axis neighborhood
is not relabeled as a room dataset.

## 5. The seven-action Express journey

1. **CHECK at the mark.** Establish identity, ambient/SNR evidence, capture
   linearity, and safe stimulus levels.
2. **RAW ANCHOR at the mark.** Capture repeated protected woofer and HF sweeps
   in one action.
3. **12 cm left.** Capture both protected drivers.
4. **12 cm right.** Capture both protected drivers.
5. **40 cm left.** Capture both protected drivers; retain the existing
   equidistance guidance.
6. **40 cm right.** Capture both protected drivers.
7. **VERIFY back at the mark.** After explicit Apply, capture the crossed
   woofer branch, crossed HF branch, and their sum in one protected action.

Between actions 6 and 7, the Pi evaluates candidates offline and the household
reviews one proposed winner. Candidate evaluation adds compute, not a physical
walk and not one apply per candidate.

R16 appends exactly the four named lateral poses to the R15 anchor; it does not
add Full-only pre-apply positions or change the schema. Existing Full-only
pre-apply captures may continue serving their shipped purpose, but they are not
selector evidence in schema v1. Admitting another selector pose requires a
later schema version. Full retains its additional post-apply summed cloud, and
only completed post-apply positions widen the verified spatial claim. Neither
changes what owns the design-axis coefficients.

## 6. Bounded crossover + linearization solve

### 6.1 Candidate window

The user-confirmed component declaration defines the hard fence. The machine
never tests or emits outside it. Before generating a grid, R17 computes one
closed candidate interval as the intersection of the confirmed component,
measurement-protection, measured-trust, meaningful-contribution, and
fit-authority
Fc bounds. Every contributing bound and its fingerprint is serialized in the
selector result. A missing, non-finite, inverted, stale, or empty intersection
is invalid measurement authority and produces `refused`; it is not repaired by
dropping a bound.

The v1 grid policy is deterministic:

1. Enumerate every integer `k` whose resulting rounded value can fall in the
   finite interval, using `configured_fc_hz * 2^(k/6)`.
2. If the configured Fc is inside the closed interval, reserve its exact,
   unrounded value as the `k=0` candidate. Round every `k != 0` value to the
   nearest 10 Hz (positive half-way values round upward).
3. Discard values outside the closed interval and duplicate Hz values. A
   duplicate Hz value's selection priority is the lowest `abs(k)` among the
   values that produced it; no representative `k` is decision authority.
4. Reserve the legal configured candidate, then choose remaining candidates in
   `(abs(k), candidate_hz)` order until at most five total remain. When the
   configured Fc is outside the interval it is not reserved, but in-interval
   values generated from the same formula may still be evaluated.
5. Serialize the chosen candidates in ascending-Hz order and fingerprint the
   policy inputs, interval, selection order, and serialized grid. Ascending
   serialization is reporting only and never breaks a comparison tie.

Within that grid, the v1 selector:

- keeps the topology two-way and the acoustic family LR4;
- evaluates at most five Fc values from one policy-owned grid;
- includes the current configured Fc when legal, making 2 kHz the JTS3 golden
  reference and fallback;
- uses declared diameter/Sd and horn coverage only as a coarse prior/window,
  never as measured directivity;
- excludes bins outside the trustworthy gated span and the branch's meaningful
  contribution region;
- sets the speaker fit/grade lower edge to
  `max(product lower edge, 2.5 / T)`, where `T` is the accepted gate window in
  seconds; bins below it may be displayed but remain ungraded and uncorrected
  until a near-field/bass instrument extends authority;
- treats the declared 1.6 kHz floor as a possible lower bound, not evidence
  that 1.6 kHz is optimal.

Slope family, topology, asymmetric orders, FIR/all-pass correction, and 3-way
selection remain out of scope until Fc-only selection works on hardware.

### 6.2 A complete candidate is the unit of comparison

For every Fc:

1. derive `S_c = M * C_c / P` from the same immutable post-`P` raw responses
   under §4.2's finite-value, conditioning, mask, and fingerprint policy;
2. build that Fc's acoustic branch targets;
3. fit candidate-specific per-driver linearization under the existing
   correction envelope, contribution guard, and headroom ledger;
4. solve anchor trim, delay, and polarity against the corrected branches;
5. synthesize the design-axis and four side-position sums while holding the
   anchor solution fixed;
6. emit a complete proposed prescription and its evidence, or a named refusal.

There is no universal "flatten both drivers, then choose Fc" PEQ. Such a PEQ
would encode one crossover assumption and bias every later candidate.

### 6.3 Ranking and abstention

Safety, measurement authority, and runtime-contract checks are admissibility
gates, not score terms. A candidate that needs unsupported comparison bins,
stopband correction, violates protection or headroom, fails its fit/runtime
contract, or cannot emit one complete prescription is inadmissible with named
reason codes. Candidate-local invalidity drops that candidate only while at
least one other candidate remains; no admissible candidate produces `refused`.
Invalid schema, identity, or measurement authority always invalidates the
whole result and produces `refused`.

The frozen candidate-level reason-code vocabulary is
`unsupported_comparison_bins`, `ill_conditioned_protection_deembedding`,
`stopband_correction_required`, `protection_contract_violated`,
`headroom_contract_violated`, `fit_failed`, `runtime_contract_invalid`, and
`incomplete_prescription`; multiple reasons remain ordered and are not
collapsed into a generic failure. The ascending `grid_fc` generated by §6.1 is
immutable for metric construction, even when a candidate is later
inadmissible; its endpoints continue to own `B_grid`.

The comparison masks are exact:

- `B_grid = [min(grid_fc) * 2^(-1/6), max(grid_fc) * 2^(1/6)]`, projected only
  by selecting existing bins on the shared `frequency_basis_hz`.
- The **anchor absolute-sum mask** is the bins in `B_grid` that are finite,
  valid, and trustworthy for both anchor drivers and supported by every
  candidate's total-transfer composition and predicted sum.
- Each role's **anchor branch mask** is the bins in `B_grid` that are finite,
  valid, and trustworthy for that anchor role, inside that role's
  meaningful-contribution/radiating mask for every candidate, and supported by
  every candidate's total-transfer composition.
- Each candidate's **lateral mask** is the existing shared-basis bins in
  `[Fc * 2^(-1/6), Fc * 2^(1/6)]` that are finite, valid, and trustworthy for
  both roles at the anchor and all four lateral poses and are numerically valid
  under §4.2's total-transfer composition.

Before those masks are fingerprinted, apply §4.2's finite/12 dB conditioning
test for each candidate over the union of bins admitted by the masks'
non-numerical predicates: frequency range, validity/trust, and (for a branch)
meaningful-contribution/radiating membership. Candidate-local failure drops
that candidate under the ordinary rule, but never changes `B_grid`. The common
anchor masks then require support from every candidate still entering metrics.
Once those masks are frozen, later invalidity never rebuilds them around a
smaller candidate set.

Every required mask must contain at least three finite bins. An empty,
non-finite, or smaller common anchor mask is grid-wide invalid measurement
authority: the whole selector result is `refused` with the existing outcome
reason `invalid_measurement_authority`; it is never recorded as a
candidate-level `unsupported_comparison_bins` failure. An invalid candidate
lateral mask makes only that candidate inadmissible with
`unsupported_comparison_bins`; if every candidate becomes inadmissible, the
existing `no_admissible_candidate` refusal applies. Contract tests must pin
this exact common-anchor-versus-candidate-lateral mapping. Masks are
fingerprinted before metrics. They are never repaired by interpolation,
smoothing, per-candidate bin dropping, or by dropping a candidate and
rebuilding `B_grid` after metric construction.

Every RMS below is the ordinary **unweighted RMS in dB**, matching the current
fitter:

```text
sqrt(sum((pred_db - target_db)^2) / N)
```

It uses exactly the finite shared-basis bins in the frozen mask. There are no
log-frequency weights, interpolation, smoothing, or candidate-specific bin
omissions.

Every admissible candidate is compared by these lower-is-better scalar keys in
this exact lexicographic order:

1. **Worst anchor branch-target error:** the greater of the woofer and HF
   branch-target unweighted RMS errors on their frozen per-role masks, in dB.
2. **Anchor absolute-sum error:** unweighted RMS error in dB against the
   absolute summed target on the frozen common anchor absolute-sum mask.
3. **Worst lateral handoff mismatch:** over the one-third-octave band centered
   at that candidate Fc, the maximum across the four lateral poses and compared
   bins of
   `abs((woofer_side_db - woofer_anchor_db) - (hf_side_db - hf_anchor_db))`
   in dB, with the anchor trim/delay/polarity alignment held fixed.
4. **Required positive headroom:** dB of positive headroom consumed by the
   complete prescription.
5. **Filter effort, part one:** total biquad count.
6. **Filter effort, part two:** sum of the absolute gain in dB of every emitted
   filter.

The first three acoustic keys carry a symmetric uncertainty half-width `u`.
The nominal responses are the arithmetic complex means of the three selector
groups frozen in §4.3. For each anchor key, R17 enumerates the three paired
anchor groups, recomputes the same metric on the same frozen mask, and sets
`u = max(abs(realization_value - nominal_value))`. For the lateral key, it
enumerates the full Cartesian product of one paired selector group from each
of the five poses: exactly `3^5 = 243` realizations. Each realization
recomputes the same worst-lateral metric with anchor alignment fixed and the
same candidate lateral mask; `u` is again the maximum absolute departure from
nominal. Every realization evaluates the **same nominal candidate
prescription**, including its filters, trim, delay, and polarity; it never
refits or re-solves that prescription from one repeat. Index-pairing
independent driver repeats, aggregate-repeat shortcuts, sampling, and pruning
are forbidden. The maximum repeat-realization lateral metric work, including
the baseline uncertainty run, two 27-realization runs, and two 81-realization
runs below, is
`5 * (243 + 27 + 27 + 81 + 81) = 2,295` evaluations. Nominal lateral metrics
add at most five one-per-candidate baseline evaluations, so the total is at
most 2,300 lateral metric evaluations. None refits a prescription. The
deterministic headroom and filter-effort keys serialize `u=0`.

For candidates A and B at each key, A beats B only when
`A.value + A.u < B.value - B.u`; B beats A only by the symmetric inequality.
Otherwise that key is tied and comparison continues to the next key. Pairwise
comparison stops at the first non-tied key. The comparison trace records every
examined interval, each tie, the first deciding key, and the decision.
Ascending-Fc report order never chooses.

After all pairwise comparisons, exactly one unbeaten candidate enters the
unique-candidate sensitivity path. More than one unbeaten candidate produces
`abstained` with `metric_uncertainty_tie`; zero unbeaten candidates produces
`abstained` with `cyclic_comparison`. The configured-Fc fallback rule still
applies, so either abstention becomes `refused` with
`configured_fc_inadmissible_for_abstention` when that fallback is
inadmissible. R17 must pin the cycle case with this exact fixture: first-key
intervals `A=[2,4]`, `B=[1,3]`, and `C=[0,1.5]`, followed by lower-key ordering
`A < B < C`. It yields A beats B, B beats C, and C beats A, and therefore must
deterministically abstain—or refuse only because the configured fallback is
inadmissible.

Re-run the same comparison with left-only and right-only lateral evidence, and
with `left_40_cm` and `right_40_cm` each omitted in turn. Every sensitivity run
keeps the baseline grid, common/role masks, candidate prescriptions, and
per-candidate lateral mask frozen; it changes only which lateral poses feed key
3 and that key's uncertainty. Left-only and right-only each enumerate exactly
`3^3 = 27` Cartesian realizations (anchor plus the two included side poses).
Each leave-one-wide-side run enumerates exactly `3^4 = 81` (anchor plus the
three retained side poses). No run rebuilds a mask, refits an anchor
prescription, admits a previously dropped candidate, or borrows the omitted
pose's response.

Each sensitivity run applies the same unbeaten-set rule and serializes its own
pairwise trace. A zero-unbeaten cycle is recorded as `cyclic_comparison`; a
multiple-unbeaten result is recorded as `metric_uncertainty_tie`. A candidate
is `selected` only if the baseline and every sensitivity run have the same
single unbeaten candidate. A different unique candidate or a zero/multiple
unbeaten sensitivity result produces `abstained` with the applicable
`left_right_winner_instability` or
`leave_one_wide_side_winner_instability`; the sensitivity trace retains the
tie/cycle subreason. Abstention retains the exact configured Fc only when that
configured candidate is admissible. If it is inadmissible, the outcome is
`refused`.
The frozen outcome reason vocabulary includes
`unique_lexicographic_dominance`, `metric_uncertainty_tie`,
`cyclic_comparison`, `left_right_winner_instability`,
`leave_one_wide_side_winner_instability`, `invalid_evidence_schema`,
`invalid_evidence_identity`, `invalid_measurement_authority`,
`no_admissible_candidate`, and
`configured_fc_inadmissible_for_abstention`; candidate records carry the
specific named admissibility failures rather than collapsing them into an
outcome tie.

The v1 claim is "best supported among the safe candidates at the mark and four
lateral samples," never "directivity matched."

## 7. Apply, verify, and iterate

Only the winning candidate reaches the two-stage review surface planned by
#1806. R18 completes or reuses that seam; it does not invent a second wizard
framework. The household sees the measured evidence, proposal, predicted
result, headroom cost, and any abstention/disclosure before choosing Apply.

Apply is one transaction with the prior production profile retained for Undo.
The mark-position verification then proves three distinct claims:

1. **woofer branch realization** — crossed/linearized measured branch tracks
   its candidate;
2. **HF branch realization** — same, independently;
3. **integration realization and absolute result** — the measured sum tracks
   the candidate and does not merely reproduce a model-predicted crossover
   null.

Express can finish as **verified at the mark** only. Full becomes
**spatially graded** only after its post-apply cloud closes. One producer owns
that scope/completeness fact for the wizard, `/state`, and doctor; consumers do
not infer it independently.

A failed verification never becomes a silent success or an automatic second
candidate. It preserves the prior profile, names the failed claim, and offers
bounded actions: Undo, revise from the same-session evidence, or end. A later
attempt is a new complete prescription with an evidence-linked predecessor,
not a comparison against an unrelated run from another day.

## 8. Listening profiles and positioning

The adopted but unshipped listening-profile choice remains:

- **accurate at a spot**;
- **good around the room**.

It does not branch this crossover/linearization foundation. Its first consumer
belongs above the speaker layer: tight-seat versus distributed Room positions,
Room target policy, grading/display, and later preference tilt. Every bonded
speaker in a group must share the declaration.

For v1 positioning, use the shipped numeric prompts, diagrams from #1941 as
they land, and acoustic timing evidence. Browser orientation may later help
with aim/level, but ordinary browser motion sensors do not provide credible
translation. #1877's adopted order stands: acoustic timing first, sensor
fusion later. Camera fiducials or native AR remain optional future work and
require a rigid, measured phone-to-microphone transform.

## 9. Anti-spiral constraints

These are acceptance constraints, not preferences:

1. No new framework or registry for a single consumer; extend the existing
   capture-plan, evidence, candidate, fitter, and apply seams.
2. One behavior change per PR and one primary code territory per round.
3. No parallel implementation branches touching the same flow.
4. No topology/order search, 3-way generalization, Room work, phone 3D
   tracking, or new visualization framework in the core campaign.
5. No hardcoded JTS3 answer. JTS3 supplies validation evidence; confirmed
   component facts and measured responses supply product decisions.
6. No hidden fallback. Missing identity, protection, timing, or discriminating
   evidence produces an explicit refusal/abstention.
7. No speculative fix for a contradiction. Stop the round, record the exact
   conflicting evidence, and let the conductor re-scope.
8. The configured 2 kHz path remains a golden one-candidate mode until the
   multi-candidate path proves equivalence and then improvement.

## 10. Existing-ticket sweep

Snapshot: 2026-08-04. Re-fetch state before executing a round. This table is
the canonical disposition for this revision, not a copy of every issue body.

| Issue | Role in this plan | Disposition |
|---|---|---|
| [#1665](https://github.com/jaspercurry/JTS/issues/1665) component facts/L-pad | Safety input | Consume the one confirmed declaration seam; do not absorb the remaining research-prefill UX work. R14/R15 gate. |
| [#1654](https://github.com/jaspercurry/JTS/issues/1654) HF sweep to declared floor | Raw instrument | Revival trigger is now concrete: a candidate cannot be evaluated below the captured HF evidence. R15 adds declared-floor support for RAW ANCHOR; R16 only reuses that landed support for the four lateral poses. |
| [#1675](https://github.com/jaspercurry/JTS/issues/1675) ka/directivity guidance | Selector prior | R17 consumes it as a coarse prior/window; measured lateral evidence outranks it. |
| [#1894](https://github.com/jaspercurry/JTS/issues/1894) measurement-adjudicated Fc/topology | Primary selector tracker | R17 implements only its stated 80/20 Fc-within-confirmed-limits slice. Topology/order stay deferred on the same issue. |
| [#1968](https://github.com/jaspercurry/JTS/issues/1968) crossover decision research | Research authority | Input to R17; not a second implementation tracker. Household lateral samples remain a coarse gate, not the report's full polar program. |
| [#1806](https://github.com/jaspercurry/JTS/issues/1806) measure/review/apply/verify split | Apply chassis | R18 reuses it; no new wizard framework. |
| [#1868](https://github.com/jaspercurry/JTS/issues/1868) model-reproduced null passes VERIFY | Verification truth | R19 adds absolute crossover-region and branch realization beside tracking. |
| [#2098](https://github.com/jaspercurry/JTS/issues/2098) mark-only reported graded | Result scope SSOT | R19 gives one producer the mark-vs-spatial completeness fact. |
| [#1784](https://github.com/jaspercurry/JTS/issues/1784) honest before/after | Report reuse | R19 may feed the existing report; no new chart framework is required for campaign exit. |
| [#1941](https://github.com/jaspercurry/JTS/issues/1941) guided measurement UX | Prompt/diagram line | R16 reuses current poses and structured prompt work. Remaining visual polish does not block raw evidence. |
| [#1877](https://github.com/jaspercurry/JTS/issues/1877) position-aware clouds | Closed product decision | Keep acoustic timing first and phone sensor fusion parked. No revival for v1. |
| [#2092](https://github.com/jaspercurry/JTS/issues/2092) spread-blind geometry lock | Known defect | Keep separate. It blocks only if the revised path still asks this estimator to decide side-evidence admissibility; do not bundle opportunistically. |
| [#1848](https://github.com/jaspercurry/JTS/issues/1848) JTS3 acceptance | Separate controlled A/B | Keep its reference-level versus SNR-solved MEASURE-level A/B separate. R20 may contribute general commissioning evidence, but does not replace, close, or claim that controlled comparison. |
| [#1870](https://github.com/jaspercurry/JTS/issues/1870) delay/angle bench | Hardware evidence/future polar | R20 may consume the anchor/delay evidence. Precise-angle and reflector experiments stay separate. |
| [#2099](https://github.com/jaspercurry/JTS/issues/2099) LF fit/grade/layer seam | Authority gate | R14 records `max(product edge, 2.5/T)` as the v1 fit/grade floor; the issue pins implementation and evidence. It is not solved by calling all LF behavior "room." |
| [#1866](https://github.com/jaspercurry/JTS/issues/1866) attribution/listening profiles | Later consumer | Preserve explicit profile ruling; no crossover branch. |
| [#1791](https://github.com/jaspercurry/JTS/issues/1791) Room regime | Later room line | Starts only after R20 proves the speaker line. Reuse applied-speaker evidence, not crossover positions. |
| [#2104](https://github.com/jaspercurry/JTS/issues/2104) measured linearity/invertibility limits | Deferred sophistication | Does not block v1. Existing envelope behavior remains; later evidence may only narrow authority. |
| [#1703](https://github.com/jaspercurry/JTS/issues/1703) 3-way support | Generalization | Explicitly outside this campaign. Prove the two-way contract first. |

### R14-created implementation tickets

R14 created exactly the two uncovered scopes after ratification:

1. **[#2106](https://github.com/jaspercurry/JTS/issues/2106) — neutral graph +
   RAW ANCHOR + frozen v1 contracts.** Route every pre-apply
   capture through one protection-only session graph; preserve the production
   profile; emit the anchor-only raw evidence, freeze the evidence/result
   schemas, implement the configured-Fc offline `M * C / P` total-transfer
   composition, and define `Start over`, failure cleanup, fingerprinting,
   conditioning, equivalence, and negative tests.
2. **[#2107](https://github.com/jaspercurry/JTS/issues/2107) — raw per-driver
   lateral evidence.** Replace pre-apply live-graph summed
   side sweeps with protected per-driver captures on a common timing ledger;
   persist the evidence needed by the selector while preserving the current
   Express action count and retry contract.

Do not create a phone-positioning ticket, a new Room umbrella, a topology
search ticket, or a second Fc issue. Existing issues already own those facts.

## 11. Seven-round campaign

| Round | Mission | One primary territory | Exit |
|---|---|---|---|
| **R14 — Ratify** | Review this contract, reconcile issue comments, create only the two missing tickets | docs/issues; no product code | owner approval + independent docs gate at 0 blockers / 0 should-fixes |
| **R15 — Baseline + anchor contract** | Land the usable protection-only graph, route every pre-apply stimulus through it, capture RAW ANCHOR including declared-floor support, freeze both v1 schemas, and recover the configured-Fc path through offline total-transfer composition | commissioning lifecycle / anchor evidence + schema binding | old correction cannot affect evidence; anchor-only raw evidence and role-specific `P` are valid; generic same-evidence configured-Fc total-transfer equivalence, including the required `P=C` identity-ratio subfixture, meets the frozen tolerances; conditioning, production graph, and Undo contracts remain intact; focused tests + fixed-mic mechanical pass |
| **R16 — Lateral instrument** | Append only the four existing lateral raw-driver poses with no extra actions or schema change | capture program + measurement evidence | exact five-pose v1 record with three paired groups per pose; seven-action Express plan; protected bands and retry/identity contracts; no selector or new pose vocabulary |
| **R17 — Pure selector** | Build the exact bounded LR4 Fc grid, total-transfer composition, frozen-mask evaluator, comparison trace, and selection/abstention/refusal policy against R15's schemas | offline prescription/fit math | synthetic and banked-corpus tests prove configured-Fc equivalence, conditioning/grid/mask policy, bounded 243/27/81-realization uncertainty, unique dominance/cycle handling, every abstention/refusal path, and sensitivity behavior; no I/O, deploy, hardware, or apply |
| **R18 — Apply** | Thread one winner through proposal/review and explicit transactional Apply | conductor/review/apply surface | one candidate shown/applied; production profile + Undo correct; no verification redesign hidden in the PR |
| **R19 — Verify** | Verify crossed branches + sum and own honest mark/spatial result scope | verify program + result/report projection | branch + sum proof; #1868/#2098 pinned; Express/Full claims correct end-to-end |
| **R20 — Prove** | Run the fresh new-horn JTS3 campaign and reconcile docs/issues from evidence | hardware/evidence only | fixed-2 kHz golden run, bounded-selector run, retained evidence ledger, doctor/runtime checks, owner listening verdict; general evidence may inform but does not replace/close #1848's controlled level A/B; no code unless a new bounded round is opened |

The only dependency graph is:

```text
R14 -> R15 -> R16 --\
                +--> R18 -> R19 -> R20
           R17 --/
```

R16 and pure R17 may run in parallel from R15 because R15 freezes both schemas
and their exact semantics; R18 waits for both to land. Each round must leave
the current product working; no long half-migration waits for R19 to become
coherent. A round that encounters a contradiction stops at the smallest
working state and returns to the conductor for re-scoping.

## 12. Agent topology and landing protocol

The campaign uses **nine supporting Codex sessions plus the root conductor**,
with at most four active at once:

- five separate bounded implementers, one each for R15–R19; after R15, the R16
  capture implementer and pure R17 selector implementer may work concurrently
  because their territories meet only at R15's frozen schemas;
- three recurring independent reviewers: correctness/evidence, hearing-safety/
  DSP, and resilience/observability;
- one read-only R20 evidence analyst.

The correctness/evidence reviewer first runs R14's docs-only gate, then recurs
on the product rounds; it is not a tenth supporting session. None of the three
reviewers may implement a round they review.

The root session is the conductor: architecture, evidence probes, task
dispatch, spot-checking, issue/roadmap updates, merge sequencing, deploy
coordination, and final reconciliation. It does not implement product code.

Landing loop for every product round:

1. refresh `origin/main`; give the implementer one round, one file territory,
   explicit non-goals, and a measurable DONE condition. R16 and R17 both start
   from landed R15; R18 does not start until both independently reviewed
   branches have landed;
2. implement/test in an isolated worktree; one PR, no unrelated cleanup;
3. conductor spot-checks the diff and load-bearing reported numbers;
4. all three independent reviewers read
   [`.claude/commands/adversarial-review.md`](../.claude/commands/adversarial-review.md)
   and review the actual diff from their assigned lenses;
5. require **0 blockers / 0 should-fixes from every lens**; fixes return to the
   original implementer and then to the same reviewers for delta review;
6. merge only with green CI and the doc-impact contract satisfied;
7. when the round has runtime behavior, deploy in the safe compatibility order
   (capture page first whenever its contract changes), run doctor/runtime
   probes, and close the round's mechanical fixed-mic slice. Pure R17 ends at
   synthetic and banked-corpus tests: it has no deploy or hardware step;
8. update this plan's outcome, the program CURRENT POSITION, and the owning
   issues before opening the next round.

Docs-only R14 uses one independent adversarial reviewer. DSP/graph/capture
rounds use the three-lens panel because they are audio/hearing-safety critical.
Reviewers never write the branch or merge it.

## 13. Tight launch contracts

Every actual agent prompt consists of the shared contract below followed by
exactly one round mission. Neither fragment is a standalone prompt.

### Shared contract — paste into every implementation prompt

```text
You are a bounded implementation agent for one JTS round. The root session is
the conductor: it owns architecture, dispatch, review, merge, deploy, and
roadmap state. You implement only the mission below in an isolated worktree.

Read AGENTS.md, docs/crossover-linearization-80-20-plan.md, the named canonical
docs, and the owning issues before editing. Diagnose the current seam with
evidence first. Preserve the current working end-to-end flow while replacing
one behavior behind it. Reuse existing capture, evidence, candidate, fitter,
apply, and state contracts; do not add speculative abstractions or a parallel
framework.

80/20 stop rule: if the mission requires Room work, topology/order search,
3-way generalization, phone positioning, a new visualization framework, or an
unwritten design decision, stop and report the exact dependency. Do not build
around confusion. Do not open/close issues, merge, push, deploy, or modify
unrelated code.

Dependency rule: R15 freezes the evidence and selector-result schemas. R16 and
pure R17 may then run independently from R15; R18 starts only after both land.
Do not make R17 wait for R16 hardware or let either branch revise a frozen
field meaning on its own.

The owner's bar is separation of concerns, one source of truth, elegant and
modular boundaries, bounded resource use, resilience, observability,
reliability, performance, and explicit measured-vs-inferred honesty. Every
behavioral promise gets a targeted test. Report the diff, tests actually run,
remaining hardware gap, and any contradiction. Do not self-certify: the root
will dispatch an independent three-lens adversarial review using
.claude/commands/adversarial-review.md; merge requires 0 blockers and 0
should-fixes, and any fix returns to the same reviewers for delta review.
```

### R15 mission — baseline graph + RAW ANCHOR

```text
Locate exactly why CLOUD_MEASURE traverses the production graph while the
other pre-apply phases can use commissioning routing. Land the smallest usable
session-owned protection-only graph/binding that every pre-apply stimulus
shares, and make the mark-position MEASURE action emit RAW ANCHOR: repeated
neutral woofer/HF complex responses in exactly three paired selector groups on
one timing/gain ledger. Land the declared-floor capture support required by
#1654. Persist the exact emitted role-specific `P` transfers and freeze both v1
schemas exactly as this plan specifies. For the configured Fc, derive
`S = M * C_configured / P` offline before today's crossover-shaped fitter and
branch-target path; pin the finite-value, 12 dB conditioning, fingerprint, and
generic same-evidence configured-Fc total-transfer equivalence contract in
§4.2, including the required `P=C` identity-ratio subfixture. Keep
the production profile untouched until explicit Apply and retain Undo. Pin
Start over, abort, play failure, process restart, schema/identity mismatch, and
cleanup behavior. Do not capture lateral raw poses, select another Fc, change
Room, or change household layout. DONE when old linearization/Room/preference
filters provably cannot affect any pre-apply artifact, a valid anchor-only v1
record including `P` exists, and the configured-Fc total-transfer path meets
every frozen deterministic equivalence tolerance without emitting an inverse
filter.
```

### R16 mission — four lateral poses

```text
On R15 main, preserve Express's seven capture actions and the existing
±12/±40 cm prompts. Append exactly left_12_cm, right_12_cm, left_40_cm,
and right_40_cm to R15's anchor-only v1 evidence in that order, retaining both
protected raw drivers on each pose's common timing/gain ledger. Do not change
schema_version, field meanings, anchor bytes, or the storage owner. Use existing
program-analysis, retry, retention, and identity seams. Each appended pose must
expose exactly three accepted-order selector groups pairing woofer and HF; reuse
R15's already-landed declared-floor support for #1654. Do not select Fc, add
Full or other poses to selector evidence, redesign the page, fix unrelated
geometry policy, or touch Room. DONE when the same schema atomically holds the
anchor plus those four poses, every identity/retry/grouping contract is pinned,
and no side capture uses the production graph.
```

### R17 mission — pure Fc selector

```text
On R15 main, in parallel with R16, implement a pure offline evaluator against
R15's frozen evidence/result schemas. Generate at most five LR4 Fc candidates
by the exact interval/grid policy in §6.1. Each candidate gets its own offline
`M * C_c / P` total-transfer composition, conditioning verdict,
crossover-shaped branch fit, trim, delay, polarity, modeled anchor and
four-side sums, headroom/effort, complete prescription, and named
inadmissibility. Emit §6.3's frozen masks, unweighted RMS metrics, exact
three-group baseline and 243/27/81-realization uncertainty contracts,
first-deciding-key pairwise trace, unbeaten-set/cycle outcome, sensitivity
reruns, and
selected/abstained/refused result. Use frozen five-pose fixtures; do not depend
on R16 hardware or change the schema. The configured 2 kHz path uses generic
same-evidence configured-Fc total-transfer equivalence for each role-specific
`P`, including the required `P=C` identity-ratio subfixture; `P=C` is not a
generic JTS3 premise. No I/O, conductor, UI,
apply, deploy, hardware, topology/order search, or new safety policy. DONE when
synthetic and banked-corpus tests prove deterministic equivalence, grid/mask
policy, conditioning, unique dominance, candidate-invalidity handling, every
abstention/refusal class, the exact three-candidate cycle fixture, and
left/right plus leave-one-wide-side sensitivity behavior. Contract tests must
also prove that a deficient common anchor mask refuses with
invalid_measurement_authority, while a deficient candidate lateral mask uses
unsupported_comparison_bins and reaches no_admissible_candidate only when all
candidates drop.
```

### R18 mission — propose and apply

```text
On main after both R16 and R17 land, thread exactly one selector result through
#1806's planned proposal/review boundary and the existing transactional apply
machinery. Reject a result whose evidence, grid, candidate, prescription, or
outcome identity does not match the complete five-pose session record. Apply
only the chosen complete prescription; retain and surface the prior production
profile for Undo. Reuse current report/UI components and minimal copy. Do not
redesign VERIFY, add a chart framework, or touch Room. DONE when measurement
stops on one evidence-linked proposal, the household explicitly applies or
declines it, and apply/decline/restart/failure paths preserve one authoritative
candidate and rollback state.
```

### R19 mission — crossed-branch + summed verification

```text
On R18 main, extend the single mark-position VERIFY action to prove crossed
woofer realization, crossed HF realization, measured-vs-model sum tracking,
and an absolute crossover-region result; retain Full's post-apply spatial
cloud. Give one producer ownership of mark-verified versus spatially-graded
scope across wizard, /state, and doctor. Use existing report components and
minimal copy; no new chart or wizard framework. Preserve Undo and explicit
failure actions. DONE when Express and Full complete honestly end-to-end and
#1868/#2098's failure modes are pinned.
```

### R20 mission — read-only evidence audit

```text
You are the independent read-only evidence analyst for the owner-run JTS3
proof. Do not edit code, DSP, issues, or docs. Re-derive graph/candidate/capture
fingerprints; confirm every pre-apply artifact used the neutral commissioning
graph; compare the fixed-2 kHz golden run with the bounded-selector run; compare
predicted versus achieved woofer, HF, and sum; inspect side robustness,
headroom, rollback, logs, state, and doctor. Separate measurement failure,
application failure, and failure to meet target. Return one evidence ledger,
one go/conditional/no-go result for the flow, and the smallest next action. A
contradiction opens a new bounded round; it never licenses an improvised fix.
This general commissioning audit may contribute evidence to #1848, but it is
not that issue's controlled reference-level versus SNR-solved MEASURE-level
A/B and must not replace or close it.
```

**Verification scope.** On 2026-08-04, this plan was checked against current
`main` (`d742b37bec8293b72f1897194d9bf8e10b85cb08`), the shipped v2 phase
roles, Express prompt table, fitter/cloud evidence flow, layer and Room plans,
the canonical adversarial-review prompt, and the linked GitHub issue bodies and
states. No product code, issues, agent/measurement sessions, measurements, or
DSP state were changed while drafting it.

The 2026-08-04 R14 administrative closeout then verified #2105 merged as
`a7e25f3ddb43457d1b08a9205542d880f3187591`, created exactly #2106 and
#2107, updated #1654's stale shelved title, and posted the ratified scope links
to #1654/#1894. That closeout changed no product code, deployment, DSP state,
or measurement evidence.

Last verified: 2026-08-04
