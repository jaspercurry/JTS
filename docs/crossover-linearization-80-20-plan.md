# Active-speaker commissioning — 80/20 crossover + linearization revision

> **R15 implementation checkpoint (2026-08-05).** R14 remains complete;
> [#2106](https://github.com/jaspercurry/JTS/issues/2106)'s bounded replacement and
> conductor checkpoint and docs alignment are complete; review/PR/merge remain. Nothing was
> deployed or measured; `05822244` stays archaeology. Live status lives in the
> [program CURRENT POSITION](HANDOFF-correction-revision-plan.md#current-position).
> R15 is protected-neutral CHECK/MEASURE, exact configured-path reconstruction,
> and the existing fitter/review/Apply/Undo/post-apply verification path. Required
> order: R15 → R16 → R17 → R18 → R19 → owner studio checkpoint →
> read-only R20. No hardware validation is claimed. Deployed behavior remains in
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

R15 is narrower: protected-neutral `CHECK` and `MEASURE` feed the current
configured-Fc path, followed by the existing review, Apply, Undo, and
post-apply `VERIFY`. It skips pre-apply cloud, lateral capture, and dynamic Fc.
R16–R19 then land sequentially before the owner studio checkpoint and R20 audit.

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
| `CHECK` | channel identity, ambient/SNR evidence, level solve, capture-chain checks | run through the confirmed protected-neutral graph |
| Anchor `MEASURE` | repeated gated per-driver sweeps, timing, calibration, trim/delay/polarity evidence | consume accepted in-session responses through the protected-neutral graph, then recover today's configured-Fc fitter input through the exact offline total-transfer composition |
| Pre-apply cloud | none in the driver-only R15 path | skip `CLOUD_MEASURE`; do not retain contaminated evidence or build neutral summed-cloud machinery |
| Later lateral walk | mark, ±12 cm left/right, ±40 cm left/right, return to mark | R16 adds four protected-neutral per-driver lateral captures and discloses configured-Fc robustness in the existing review |
| Cloud science | exclusions, position stability, null/echo evidence, power/median checks | preserve as post-apply spatial and Room prior art; no Room implementation in R15 |
| Fitter | crossover-shaped branch-input invariant, crossover-shaped targets, contribution/stopband guards, envelope, headroom accounting | R15 supplies configured-Fc input through exact offline composition; any later candidate use requires fresh Gate 0 |
| Apply | transactional profile and retained Undo | preserve the current review/Apply/Undo path |
| `VERIFY` | measured-vs-model tracking, delta probe, and post-apply spatial grade | R18 adds post-Apply crossed woofer, crossed HF, and summed-response verification |
| Full tier | post-apply spatial cloud and honest spatial grade | retain; it measures the applied winner, not the old graph |
| Room | separate Room capture/target/filter line | later consume the verified speaker evidence; never borrow crossover-cloud positions as room positions |

The current program emitter carries the configured LR4 sections for both
branch shaping and HF protection, and the current fitter explicitly assumes
its inputs already contain those crossover shoulders. Therefore R15 may not
simply remove the configured crossover and call the old path compatible. It
must land the protected-neutral graph **and use it atomically**: capture the
accepted in-session driver responses, replace their measured protection
transfer with the configured LR4
total transfer through §4.2's exact offline `M * C / P` math, and feed that result
into every existing configured-Fc consumer. R15 removes `CLOUD_MEASURE` from
this driver-only path rather than measuring a supposed "before" through the
live production graph or inventing a replacement cloud.

## 4. Measurement contracts

### 4.1 Session-owned protected baseline

R15 adds no durable anchor, schema, or module. The existing context gate resolves
one in-memory protection mapping for CHECK and MEASURE; fresh semantic readback
must match its YAML before play. Existing `play_program` restore and its
unchanged statefile crash anchor remain sole recovery.

The commissioning graph contains only confirmed per-role protection sections,
the per-role limiter, 0 dB commissioning headroom, and physical routing.

It excludes:

- configured crossover/delay/polarity/linearization; and
- bass/Room/preference or any automatic filter outside protection/limiting.

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

Exact protection/configured-section/polarity mappings live only in conductor
priors; missing/unrepresentable protection refuses preflight, with no new schema.

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

R15 uses the accepted in-session `ProgramAnalysis` and owns only the
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
omission. R15 adds no fingerprint/schema; typed conditioning failure preserves
`ill_conditioned_protection_deembedding` via the existing refusal path.

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

In-session evidence fingerprints may differ because the neutral source and
total-transfer composition differ; fingerprint equality is not a
compatibility criterion. Banked corpus evidence may pin downstream prescription
equivalence under the same tolerances, but no test may demand that independent
acoustic captures match their noise at `1e-6`. Any optimizer or serializer
unable to meet one of the deterministic numeric tolerances must produce a
named, evidence-backed R15 re-scope before the tolerance changes; "materially
equivalent" without a number is not an exit.

### 4.4 Side evidence owns robustness, not the target

R16 reuses four ±12/±40 cm protected-neutral per-driver captures; the existing
review immediately discloses configured-Fc robustness, with no schema/cloud reuse.

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

## 7. Apply, verify, and iterate

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
9. Every round prompt forecasts gross production/test/docs lines, files, and
   modules; a breach stops for owner re-ratification and deletions never offset.
10. Caps trigger review, never collapsed ownership. Each API/field/artifact has
    a current producer and consumer.
11. Run the exact adversarial prompt to 0/0; graph/DSP/capture/apply changes
    use three lenses. Budgets gate rather than grant scope.

Gross-production/file/module tripwires: R15 300 checkpoint/400 soft/500 absolute,
5/0; R16 250/4/0; R17 400/5/≤1 small pure selector only if ownership demands;
R18 400/5/0 absent proven need; R19 150/3/0; R20 zero production code.

## 10. Existing-ticket sweep

Snapshot: 2026-08-04. Re-fetch state before executing a round. This table is
the canonical disposition for this revision, not a copy of every issue body.

| Issue | Role in this plan | Disposition |
|---|---|---|
| [#1665](https://github.com/jaspercurry/JTS/issues/1665) component facts/L-pad | Safety input | Consume the one confirmed declaration seam; do not absorb the remaining research-prefill UX work. R14/R15 gate. |
| [#1654](https://github.com/jaspercurry/JTS/issues/1654) HF sweep to declared floor | Raw instrument | R15 measures through the declared safe envelope; R16 reuses that protected-neutral path laterally. |
| [#1675](https://github.com/jaspercurry/JTS/issues/1675) ka/directivity guidance | Selector prior | Owns deferred prior/window guidance; any consumer waits for fresh Gate 0. |
| [#1894](https://github.com/jaspercurry/JTS/issues/1894) measurement-adjudicated Fc/topology | Primary selector tracker | Owns deferred Fc-within-limits work; fresh Gate 0 decides any implementation vertical. |
| [#1968](https://github.com/jaspercurry/JTS/issues/1968) crossover decision research | Research authority | Authority for deferred selector research, not implementation; lateral samples remain a coarse gate. |
| [#1806](https://github.com/jaspercurry/JTS/issues/1806) measure/review/apply/verify split | Apply chassis | R15 preserves the current path; the issue owns any deferred apply integration. |
| [#1868](https://github.com/jaspercurry/JTS/issues/1868) model-reproduced null passes VERIFY | Verification truth | R18 closes the crossed-branch and summed-response verification gap. |
| [#2098](https://github.com/jaspercurry/JTS/issues/2098) mark-only reported graded | Result scope SSOT | R19 makes every consumer state the producer-owned mark-versus-spatial truth honestly. |
| [#1784](https://github.com/jaspercurry/JTS/issues/1784) honest before/after | Report reuse | Existing reuse seam for deferred reporting; no new chart framework is authorized. |
| [#1941](https://github.com/jaspercurry/JTS/issues/1941) guided measurement UX | Prompt/diagram line | Owns deferred pose prompts and diagrams; any reuse waits for fresh Gate 0. |
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

1. **[#2106](https://github.com/jaspercurry/JTS/issues/2106) — atomic fixed-Fc
   proof.** Protected-neutral CHECK/MEASURE, exact composition, and restore;
   no pre-apply cloud or durable evidence contract.
2. **[#2107](https://github.com/jaspercurry/JTS/issues/2107) — R16.** Four
   lateral producers feed the existing review's configured-Fc disclosure.

Do not create a phone-positioning ticket, a new Room umbrella, a topology
search ticket, or a second Fc issue. Existing issues already own those facts.

## 11. Campaign order and lean round contracts

| Round | Mission | One primary territory | Exit |
|---|---|---|---|
| **R14 — Ratify** | Review this contract, reconcile issue comments, create only the two missing tickets | docs/issues; no product code | owner approval + independent docs gate at 0 blockers / 0 should-fixes |
| **R15 — Atomic fixed-Fc proof** | Protected-neutral CHECK/MEASURE, skip pre-apply cloud, compose exact configured-Fc evidence into every current consumer, preserve review/Apply/VERIFY | commissioning lifecycle | fail-closed restore and deterministic fixed-Fc equivalence are pinned in one PR; no durable/lateral/dynamic contract, deploy, or measurement |
| **R16 — Lateral robustness** | Four protected-neutral per-driver captures at ±12 and ±40 cm | existing capture + review | immediately disclose configured-Fc lateral robustness; no durable schema or cloud reuse |
| **R17 — Bounded LR4-only Fc** | At most five configured-centered candidates on a 1/6-octave grid inside the confirmed search-band intersection | current fitter; winner → candidate/review/Apply/Undo | exact `M*C/P`; simple repeat-spread margin with configured fallback; no selector artifact or new Apply subsystem |
| **R18 — Applied realization** | Verify crossed woofer branch, crossed HF branch, and sum after Apply | existing admitted graph load/play/restore | all three responses checked without a generic verification or recovery framework |
| **R19 — Honest grade** | Keep `_post_apply_grade` the sole mark-versus-spatial truth producer | existing wizard/state/doctor | consumers use honest wording; no second state model |
| **Studio checkpoint** | Owner exercises the reviewed R15–R19 flow on hardware | hardware/evidence only | collect actual evidence; no validation is claimed before this run |
| **R20 — Audit** | Reconcile only the evidence actually produced at the studio checkpoint | docs/issues/evidence only | read-only; zero production code |

R15's implementation/docs checkpoint is complete; review/PR/merge remain, and hardware validation is deferred until after R19.

The only dependency graph is:

```text
R14 -> R15 -> R16 -> R17 -> R18 -> R19 -> studio checkpoint -> R20 (read-only)
```

Each production round gets a fresh seam check and merges before the next; only
read-only reconnaissance and concurrent review lenses may overlap.

## 12. Agent topology and landing protocol

Each round uses one implementer and three independent reviewers, at most four active.

- three recurring independent reviewers: correctness/evidence, hearing-safety/
  DSP, and resilience/observability;

The correctness/evidence reviewer first runs R14's docs-only gate, then recurs
on the product rounds; it is not a tenth supporting session. None of the three
reviewers may implement a round they review.

The root session is the conductor: architecture, evidence probes, task
dispatch, spot-checking, issue/roadmap updates, merge sequencing, deploy
coordination, and final reconciliation. It does not implement product code.

Landing loop for every product round:

1. refresh `origin/main`; give the implementer one round, one file territory,
   explicit non-goals, and a measurable DONE condition;
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
   probes, and close the round's mechanical fixed-mic slice;
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

Dependency: R15 → R16 → R17 → R18 → R19 → studio → read-only R20.

The owner's bar is separation of concerns, one source of truth, elegant and
modular boundaries, bounded resource use, resilience, observability,
reliability, performance, and explicit measured-vs-inferred honesty. Every
behavioral promise gets a targeted test. Report the diff, tests actually run,
remaining hardware gap, and any contradiction. Do not self-certify: the root
will dispatch an independent three-lens adversarial review using
.claude/commands/adversarial-review.md; merge requires 0 blockers and 0
should-fixes, and any fix returns to the same reviewers for delta review.
```

### R15 mission — atomic driver-only fixed-Fc proof

```text
Start from freshly fetched current remote main; cherry-pick no commit from the
rejected prototype. In ONE atomic PR, run CHECK and MEASURE through a
protected-neutral graph only after successful normalized live load/readback;
skip/remove pre-apply CLOUD_MEASURE. Fail closed and restore the entry graph on
abort, playback failure, and process restart.

Using accepted in-session ProgramAnalysis only, apply exact offline
`M * C_configured / P` to every current configured-Fc consumer. Preserve the
existing review, Apply, Undo, and post-apply VERIFY path. Add no durable anchor,
schema, new module/recovery service, or future-only API/field/artifact.

Hard R15 #2106 caps: production 300 checkpoint/400 soft/500 absolute, five files,
zero modules; tests 500/docs 120 gross. If the complete vertical cannot fit, STOP without
exception. Do not deploy or measure. DONE is the complete configured-Fc path,
restoration and deterministic equivalence pinned by focused tests.
```

### R16–R19 launch contracts

Section 11 is the SSOT. Re-check seams; do not invent R17 thresholds/policy,
R18 tolerances, or R19 doctor severity.

**Original R14 planning/contract verification scope.** On 2026-08-04, before
the R15 scope reset, this plan was checked against then-current `main`
(`d742b37bec8293b72f1897194d9bf8e10b85cb08`), the shipped v2 phase roles,
Express prompt table, fitter/cloud evidence flow, layer and Room plans, the
canonical adversarial-review prompt, and the linked GitHub issue bodies and
states. No product code, issues, agent/measurement sessions, measurements, or
DSP state were changed while drafting it.

**R14 administrative-closeout verification scope.** The 2026-08-04 closeout
verified #2105 merged as
`a7e25f3ddb43457d1b08a9205542d880f3187591`, created exactly #2106 and
#2107, updated #1654's stale shelved title, and posted the ratified scope links
to #1654/#1894. That closeout changed no product code, deployment, DSP state,
or measurement evidence.

**2026-08-04 anti-sprawl Gate-0 scope.** Checked the live contract against
`origin/main` at `220ca14889d6b4a29ff8a9d801e5fee1bcee5cac` and independent
audits. This changed docs only; R14 history remains intact.

**2026-08-05 scope:** live R15 contract/status/order only; no appendix re-check.
Last verified: 2026-08-05
