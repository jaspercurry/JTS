# Active-speaker commissioning — 80/20 crossover + linearization revision

> **Gate-0 reset (2026-08-04).** R14 remains complete. The first R15 prototype
> at `05822244` is explicitly rejected as a merge candidate: its 38-file,
> approximately 4,334-production-line expansion violated this campaign's 80/20
> constraint. It is prototype evidence only, not landed behavior, and no commit
> from it may be cherry-picked into the fresh implementation. No R15 product
> code was merged, deployed, or measured. Independent Gate-0 audits narrowed
> R15 to one atomic driver-only fixed-Fc vertical. Everything else is later and
> separately gated. Live round status lives only in the
> [program CURRENT POSITION](HANDOFF-correction-revision-plan.md#current-position).
> This document owns R15's next slice: protected-neutral driver CHECK/MEASURE,
> configured-Fc composition, and the existing review/Apply/VERIFY path.
> Crossover selection, candidate linearization, and expanded verification below
> are deferred direction pending the hardware checkpoint and fresh Gate 0.
> This changes no current behavior by itself. Current shipped
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

R15 is narrower: protected-neutral `CHECK` and `MEASURE` feed the current
configured-Fc path, followed by the existing review, Apply, Undo, and
post-apply `VERIFY`. It skips pre-apply cloud, lateral capture, and dynamic Fc.
Later work requires the fixed-Fc checkpoint and a fresh Gate 0.

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
| Anchor `MEASURE` | repeated gated per-driver sweeps, timing, calibration, trim/delay/polarity evidence | consume accepted in-session responses through the protected-neutral graph, then recover today's configured-Fc fitter input through the exact offline total-transfer composition |
| Pre-apply cloud | none in the driver-only R15 path | skip `CLOUD_MEASURE`; do not retain contaminated evidence or build neutral summed-cloud machinery |
| Later lateral walk | mark, ±12 cm left/right, ±40 cm left/right, return to mark | launch only after the fixed-Fc hardware checkpoint and a fresh Gate-0 review |
| Cloud science | exclusions, position stability, null/echo evidence, power/median checks | preserve as post-apply spatial and Room prior art; no Room implementation in R15 |
| Fitter | crossover-shaped branch-input invariant, crossover-shaped targets, contribution/stopband guards, envelope, headroom accounting | R15 supplies configured-Fc input through exact offline composition; any later candidate use requires fresh Gate 0 |
| Apply | transactional profile and retained Undo | stop after proposal; finish/reuse #1806's two-stage review and apply only one complete winner |
| `VERIFY` | measured-vs-model tracking and delta probe | also verify the crossed branches and absolute crossover-region result |
| Full tier | post-apply spatial cloud and honest spatial grade | retain; it measures the applied winner, not the old graph |
| Room | separate Room capture/target/filter line | later consume the verified speaker evidence; never borrow crossover-cloud positions as room positions |

The current program emitter carries the configured LR4 sections for both
branch shaping and HF protection, and the current fitter explicitly assumes
its inputs already contain those crossover shoulders. That assumption is today
documented, not enforced: nothing refuses an input whose shoulders are missing.
R15's composition seam must land **with** an enforcement — a violated invariant
refuses by name; it never silently prescribes. Therefore R15 may not simply
remove the configured crossover and call the old path compatible. It must land
the protected-neutral graph **and use it atomically**: capture the accepted
in-session driver responses, replace their
measured protection transfer with the configured LR4 total transfer through
§4.2's exact offline composition — which owns that formula — and feed the
result into every existing configured-Fc consumer. R15 removes `CLOUD_MEASURE`
from this driver-only path rather than measuring a supposed "before" through
the live production graph or inventing a replacement cloud.

## 4. Measurement contracts

*Sections 4.3, 5, and 6 were removed in the 2026-08-04 Gate-0 reset; numbering
is preserved so existing issue citations — notably §9 and §13 — stay stable.*

### 4.1 Session-owned protected baseline

Every commissioning journey mints one immutable measurement-graph identity
before the first stimulus. Pre-apply stimuli run through that **session-owned
commissioning graph**, not the production graph. Measurement playback may
temporarily activate the commissioning graph and then restore playback, but
the stored/committed production profile remains unchanged until explicit
Apply. Playback starts only after a successful normalized live-config
load/readback; abort, play failure, and process restart fail closed and restore
the entry graph.

The commissioning graph contains only:

- the confirmed driver/output mapping;
- the declared hard excitation bands, including the HF driver's confirmed
  minimum safe frequency;
- measurement-only high-pass/low-pass protection needed to enforce those hard
  bands;
- limiters, conservative stimulus gain, and the existing volume ceiling;
- declared physical polarity and component facts, including passive L-pad
  attenuation.

It excludes:

- prior automatic trim, delay, polarity selection, and linearization PEQs;
- prior Room correction, bass extension, and preference/taste EQ;
- crossover **shaping** whose Fc is still being adjudicated. A protection-only
  transfer may share a filter family with a crossover, but its identity and
  corner are derived only from the confirmed hard excitation envelope and it
  must not encode a selector candidate.

"Neutral" therefore means **pre-candidate, not unprotected and not
mathematically flat**. The measurement-protection transfer is part of the raw
evidence identity. R15 must prove the neutral emitter against the same
component-safety, physical-output, limiter, volume-ceiling, and cleanup rails
that guard today's emitter; it may not weaken HF protection in order to obtain
an unshaped response.

No shipped filter qualifies as `P` today. The existing bring-up protective
tweeter high-pass is derived from the configured Fc — `2.0x` the strictest
tweeter crossover corner, in `protective_tweeter_highpass_frequency_hz`
([`jasper/active_speaker/test_signal_plan.py`](../jasper/active_speaker/test_signal_plan.py))
— so it encodes the selector candidate this section forbids. R15 derives the
measurement-protection transfer fresh from the declared hard excitation bands
instead. Where that corner lands interacts with §4.2's ±12 dB conditioning bound
near the overlap band's lower edge, where a low `abs(P)` inflates `abs(C_c/P)`
exactly where candidates need trusted bins. Placement is therefore a deliberate,
reviewed choice, not an emergent one.

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

R15 uses the accepted in-session `ProgramAnalysis` and owns only the
configured-Fc compatibility seam. For each role and shared-basis frequency
bin, define the measured neutral evidence and desired candidate-shaped fitter
input exactly as:

```text
M(f)   = plant(f) * P(f)
S_c(f) = sign_c * M(f) * C_c(f) / P(f)
```

`P(f)` is the fingerprinted complex **linear measurement-protection** transfer
actually emitted for that role after the existing timing/gain ledger has
normalized known scalar gain and delay. It excludes the limiter: admission must
prove that the limiter did not engage and that the accepted capture remained
linear. `C_c(f)` is the role's complete desired candidate LR4 transfer,
produced from the same crossover-section primitives as the emitter. It is a
total replacement for `P` in the candidate model and eventual applied graph,
not another filter cascaded after `P`. `sign_c` is the role's configured
per-region polarity as `+1` or `-1`, carried as its own declared prior rather
than folded into `C_c`'s sections. It is in this composition because the
protected-neutral emitter deliberately omits region polarity — its mixer runs
with region polarity off exactly while protection sections are emitted — so `M`
is polarity-free and the sign is reinjected once, offline, over the whole role
spectrum: the composed trusted bins and the out-of-band bins carried forward but
never claimed alike, so no bin is left in a different polarity convention from
its neighbours. `S_c(f)`, with magnitude
rederived from its complex values, is the only input handed to the
crossover-shaped fitter and branch-target path. The division is **offline
evidence math only**: no inverse, de-embedding, or recovery filter is emitted
to hardware, and the applied graph emits `C_c` once rather than `P * C_c`.

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
that role's configured crossover transfer. The new arm constructs the
polarity-free `M = H * P` and then `S_configured = sign_c * M * C_configured / P`;
the legacy arm is `L_configured = sign_c * H * C_configured`, carrying the same
sign the production emitter applies. It compares `S_configured` with
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

**Boost ruling (owner, 2026-08-05).** The R15 driver-only path permits boost
when post-apply `VERIFY` runs; `allow_boost` must no longer additionally require
a pre-apply cloud. What that cloud supplied is absent until a later gated round:
the cloud-derived boost-excluded bands, and the envelope's spatial-exclusion and
position-stability terms. The accepted risk: a boost can land on a
position-specific artifact that an at-mark verification cannot detect. The
standing rails all remain — envelope depth limits, the realized-cascade
stopband-gain guard, headroom accounting, post-apply `VERIFY`, and retained
Undo. Prerequisite: the composition must be splice-free (in-band evidence
unpolluted by the out-of-band seam) before this path is trusted on hardware.

*Implemented scope (conductor decision, follow-up to #2126).* `allow_boost` is
`post_apply_verifies AND (a cloud verdict reached the envelope OR the session's
capture plan contains no pre-apply cloud phase)`. The second disjunct is the
R15 driver-only path this ruling is about, where the cloud is absent BY DESIGN.
The first keeps the clause the retired condition existed for: a session that
PLANNED a cloud and LOST it (positions could not be combined; the honesty
pipeline was unavailable) stays cut-only, because there the missing exclusions
are a failure rather than a plan. Since R15 no shipped stage-1 session plans a
pre-apply cloud, the ruling's "no longer additionally require a pre-apply
cloud" holds for every path the product walks; the retained disjunct guards the
non-R15 shape. `boost_excluded_bands_hz` is `()` on the driver-only path —
there is no spatial evidence to compose, which is the accepted risk named
above. Second, intended consequence: boost enlarges the achievable set, so a
candidate the improvement gate previously refused as
`correction_not_an_improvement` can now clear it on the same evidence — the
same post-apply `VERIFY`, household listening, and Undo adjudicate it.

### 4.4 Side evidence owns robustness, not the target

If fresh Gate 0 authorizes lateral evidence, the smallest direction reuses the
existing ±12 cm and ±40 cm left/right moves. Each pose would capture both
drivers on one timing ledger; W–HF–W may bracket drift.

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
9. Every PR is capped at 400 **gross** production additions, five production files,
   and one new production module; deletions never offset, and splits must be independently complete.
10. Report production/test/docs gross additions and reviewer-rederived
    cumulative ledgers. Every API/field/artifact needs a current producer and
    consumer; forecast crossing any limit is a STOP for owner (or
    owner-delegated conductor) re-ratification — the owner remains the ultimate
    authority, and the delegation itself is recorded on #2106. Every
    ratification (cap overage, extra production file, new module) is recorded
    on the owning issue or PR before work continues; an unrecorded ratification
    did not happen.
11. Run the exact adversarial prompt to 0/0; graph/DSP/capture/apply changes
    use three lenses. Budgets gate rather than grant scope.

Production ceilings are per-round and govern where they differ from item 9.
R15 is one PR on a ladder: **300** gross production additions is a mandatory
conductor-inspection checkpoint, **400** is the soft target, and **500** is the
absolute stop. The 400–500 band is a justification requirement, not headroom:
any line above 400 must map directly to an unmet acceptance requirement. Five
production files is soft, a sixth requires a named-file conductor/owner
ratification, and new production modules are zero absent re-ratification. Tests
are 500 gross, over which the conductor may ratify an honesty-fixture overage
recorded on the owning issue. Docs 120. R16 350, R17 400, R18 180, R19 580
across at most two independent PRs, and R20 zero. These budget a round's
**implementation** PRs; a conductor-authored campaign-governance docs PR that
amends this plan rather than implementing a round carries its own ≤120
documentation budget and does not charge the round's implementation ledger.

## 10. Existing-ticket sweep

Snapshot: 2026-08-04. Re-fetch state before executing a round. This table is
the canonical disposition for this revision, not a copy of every issue body.

| Issue | Role in this plan | Disposition |
|---|---|---|
| [#1665](https://github.com/jaspercurry/JTS/issues/1665) component facts/L-pad | Safety input | Consume the one confirmed declaration seam; do not absorb the remaining research-prefill UX work. R14/R15 gate. |
| [#1654](https://github.com/jaspercurry/JTS/issues/1654) HF sweep to declared floor | Raw instrument | R15 measures through the declared safe envelope; any later lateral reuse waits for the fixed-Fc checkpoint. |
| [#1675](https://github.com/jaspercurry/JTS/issues/1675) ka/directivity guidance | Selector prior | Owns deferred prior/window guidance; any consumer waits for fresh Gate 0. |
| [#1894](https://github.com/jaspercurry/JTS/issues/1894) measurement-adjudicated Fc/topology | Primary selector tracker | Owns deferred Fc-within-limits work; fresh Gate 0 decides any implementation vertical. |
| [#1968](https://github.com/jaspercurry/JTS/issues/1968) crossover decision research | Research authority | Authority for deferred selector research, not implementation; lateral samples remain a coarse gate. |
| [#1806](https://github.com/jaspercurry/JTS/issues/1806) measure/review/apply/verify split | Apply chassis | R15 preserves the current path; the issue owns any deferred apply integration. |
| [#1868](https://github.com/jaspercurry/JTS/issues/1868) model-reproduced null passes VERIFY | Verification truth | Owns the deferred absolute crossover-region and branch-realization gap. |
| [#2098](https://github.com/jaspercurry/JTS/issues/2098) mark-only reported graded | Result scope SSOT | Owns deferred mark-versus-spatial completeness. |
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
2. **[#2107](https://github.com/jaspercurry/JTS/issues/2107) — deferred.** Fresh
   Gate 0 must pair any lateral producer with a current consumer.

Do not create a phone-positioning ticket, a new Room umbrella, a topology
search ticket, or a second Fc issue. Existing issues already own those facts.

## 11. Campaign and provisional later labels

| Round | Mission | One primary territory | Exit |
|---|---|---|---|
| **R14 — Ratify** | Review this contract, reconcile issue comments, create only the two missing tickets | docs/issues; no product code | owner approval + independent docs gate at 0 blockers / 0 should-fixes |
| **R15 — Atomic fixed-Fc proof** | Protected-neutral CHECK/MEASURE, skip pre-apply cloud, compose exact configured-Fc evidence into every current consumer, preserve review/Apply/VERIFY | commissioning lifecycle | fail-closed restore and deterministic fixed-Fc equivalence are pinned in one PR; no durable/lateral/dynamic contract, deploy, or measurement |
| **Checkpoint — Prove** | Owner runs the reviewed fixed-2-kHz flow on hardware | hardware/evidence only | pass before any dynamic implementation; failure opens a newly bounded repair |
| **R16–R19 — provisional labels** | Fresh Gate 0 defines complete verticals pairing each producer with a current consumer; it may co-scope lateral capture + selector or name a real immediate consumer | unassigned | §9 ceilings only; no artifact-first dependency or implementation authority |
| **R20 — Audit** | Read-only owner-run campaign audit | hardware/evidence only | reconcile evidence and issues; zero production code |

The only dependency graph is:

```text
R14 -> R15 -> fixed-2-kHz hardware checkpoint -> fresh Gate 0
```

R16–R19 are provisional labels, not a dependency order. Fresh Gate 0 must pair
each later producer with its current consumer; contradiction or a cap forecast
stops for owner re-ratification.

## 12. Agent topology and landing protocol

R15 uses one bounded implementer and three independent reviewers, with at most
four active at once. Later agent count and ownership wait for fresh Gate 0.

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
   explicit non-goals, and a measurable DONE condition. After R15, stop for
   the fixed-Fc hardware checkpoint; each later round then re-enters Gate 0;
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

Dependency rule: R15 is one atomic fixed-Fc vertical and skips pre-apply cloud.
R16–R19 remain provisional labels after its hardware checkpoint. Fresh Gate 0
must pair each later producer with its current consumer.

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

Using accepted in-session ProgramAnalysis only, apply §4.2's exact offline
`sign_c * M * C_configured / P` to every current configured-Fc consumer.
Preserve the existing review, Apply, Undo, and post-apply VERIFY path. Add no
durable JSON, self-readback, lateral/dynamic schema, or future-only
API/field/artifact.

Hard R15 caps are §9's ladder: 300 gross production additions is a mandatory
conductor inspection, 400 the soft target (every line above it mapped to an
unmet acceptance requirement), 500 the absolute stop; five production files is
soft, a sixth needs a named-file ratification; zero new production modules;
tests 500, docs 120; every ratified overage recorded on the owning issue. If
preflight cannot prove the complete vertical fits, STOP without exception. Do
not deploy or measure. DONE is the complete configured-Fc path, restoration and
deterministic equivalence pinned by focused tests.
```

### Post-checkpoint launch contracts — not yet authorized

Fresh Gate 0 must define complete producer-plus-current-consumer verticals.
It may co-scope lateral capture and selector; R16–R19 are labels/ceilings only.

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

**2026-08-05 conductor architecture review scope.** Independent research lanes
verified this round's premises against code: the program emitter does carry the
configured LR sections including tweeter protection (TRUE); the fitter's
crossover-shaped-input assumption is documented but unenforced; and removing the
pre-apply cloud is not prescription-neutral, because that cloud also gated boost.
The resulting cap-ladder, boost, and governance rulings are recorded on
[#2106](https://github.com/jaspercurry/JTS/issues/2106), which this amendment
reconciles into the plan. This changed docs only; no product code, deployment,
DSP state, or measurement evidence moved.

Last verified: 2026-08-05
