# Crossover measurement v2 — the campaign record

> **Status: historical.** Almost everything below this heading is the v2
> campaign's dated narrative — bench sessions, hardware results, and the
> decision archaeology behind the live spine. Snapshots from
> 2026-07-17 through 2026-08-11. Read it for "why is it like this," never for
> current state: specific facts here (thresholds, env defaults, file
> responsibilities, "what's working" lists) drift, and names that no longer
> exist in the code — chiefly the `CrossoverV2Conductor` class, dissolved in
> #2291 Phase 5c-iv, and `prepare_v2_verify`, folded into
> `prepare_v2_session(verify_only=True)` in #3166 — are kept where they are
> what the entry was about at the time. Current operational truth is
> [`tuning-operator-runbook.md`](../tuning-operator-runbook.md), and the file
> map in [`crossover-v2-engine-design.md`](../crossover-v2-engine-design.md) is
> the current shape.
>
> **Two sections are the exception, and this tag does not cover them:**
> "[Failure taxonomy & debugging](#failure-taxonomy--debugging)" and
> "[Gotchas — the W6 bug-class
> catalog](#gotchas--the-w6-bug-class-catalog-do-not-reintroduce)". They are
> where the spine's Debugging section delegates its deeper catalogs, and they
> are maintained against the code. Each says so at its own heading, with the
> caveats that apply to it.

### #2291 — the crossover-v2 contract migration (2026-08-10 → 2026-08-13)

A mutable session object owned measurement context, candidate context,
prescription policy, verification semantics, and lifecycle at once. On
2026-08-10 that produced a candidate whose crossover sections said 1,648.7 Hz
while its trim arithmetic still read the session's configured 2,000 Hz, and a
trim recorded as rejected that was nevertheless committed — two
single-source-of-truth failures from one object. The migration answered them
by giving every decision its own pure, separately-testable owner, then
dissolving the object that had held them.

Twenty-five merged PRs, in order:

| Phase | What it moved | PR |
|---|---|---|
| 0 | Characterization pins: stage bridge, incident replay fixture, strangler baseline | [#2299](https://github.com/jaspercurry/JTS/pull/2299), [#2298](https://github.com/jaspercurry/JTS/pull/2298), [#2304](https://github.com/jaspercurry/JTS/pull/2304) |
| 1 | Immutable contracts + the planner facade | [#2307](https://github.com/jaspercurry/JTS/pull/2307) |
| 2a | The pure intervention planner, with dual-run equivalence evidence | [#2313](https://github.com/jaspercurry/JTS/pull/2313) |
| 2b | Cutover — the pure planner becomes production truth | [#2317](https://github.com/jaspercurry/JTS/pull/2317) |
| 3a | Stage-bridge repair: commanded delta survives, rollback binds where VERIFY runs | [#2316](https://github.com/jaspercurry/JTS/pull/2316) |
| 3b | The verification/adoption evaluator — four independent verdicts | [#2318](https://github.com/jaspercurry/JTS/pull/2318) |
| 3c | The honest loop: entry baseline, wired verdicts, exactly-once restore, round receipt | [#2323](https://github.com/jaspercurry/JTS/pull/2323) |
| 4 | The journey state machine; hosts become thin adapters | [#2328](https://github.com/jaspercurry/JTS/pull/2328) |
| 5a-i | The round's tail moves behind a coordinator | [#2331](https://github.com/jaspercurry/JTS/pull/2331) |
| 5a-ii | Level policy and program composition get an owner | [#2333](https://github.com/jaspercurry/JTS/pull/2333) |
| 5a-iii | The analyzer's priors get an owner | [#2336](https://github.com/jaspercurry/JTS/pull/2336) |
| 5a-iv | The capture-consuming organs: cloud group, lateral, entry baseline | [#2341](https://github.com/jaspercurry/JTS/pull/2341) |
| 5a-v(a) | The candidate organ's values and the accountability gate | [#2352](https://github.com/jaspercurry/JTS/pull/2352) |
| 5a-v(b) | The fc sweep — ports, not patches | [#2356](https://github.com/jaspercurry/JTS/pull/2356) |
| 5a-v(c) | Candidate build + planning | [#2360](https://github.com/jaspercurry/JTS/pull/2360) |
| 5a-vi | Admission/retry | [#2366](https://github.com/jaspercurry/JTS/pull/2366) |
| 5a-vii | Capture dispatch | [#2377](https://github.com/jaspercurry/JTS/pull/2377) |
| 5b | The residual seams — the remainder becomes scaffolding | [#2382](https://github.com/jaspercurry/JTS/pull/2382) |
| 5b-ii | The verify grade leaves; the durable write stays exactly where it was | [#2385](https://github.com/jaspercurry/JTS/pull/2385) |
| 5c-i | The test surface leaves | [#2388](https://github.com/jaspercurry/JTS/pull/2388) |
| 5c-ii | The vocabulary and the flow library find their homes | [#2391](https://github.com/jaspercurry/JTS/pull/2391) |
| 5c-iii | The carried code decisions land; the relocation is measured and re-scoped | [#2394](https://github.com/jaspercurry/JTS/pull/2394) |
| 5c-iv | `CrossoverV2Conductor` dissolves — the flow file becomes the session owner | [#2396](https://github.com/jaspercurry/JTS/pull/2396) |
| 5c-v | This rewrite: the doc describes what exists | this PR |

Three findings from the campaign are worth carrying forward, because each
corrected a plausible belief with a measurement:

- **The "87 delegates are scaffolding" count was wrong, and deleting them
  would have been a regression.** The forwarders sampled were defensive
  copies — `dict(...)` on the way out — so removing them would have handed the
  web host live mutable references into session state. Only the measured-dead
  set was deleted; the kept forwarders were tabled with per-item reasons
  ([#2396](https://github.com/jaspercurry/JTS/pull/2396)).
- **The household vocabulary had to move with the spine.** An earlier ruling
  kept it in the flow file. When the verdict-building spine landed in the
  package — which is forbidden from importing the flow — that ruling stopped
  being available. Where the vocabulary *philosophically* belongs is still
  open ([#2390](https://github.com/jaspercurry/JTS/issues/2390)).
- **The Phase-1 planner facade was write-only in production.** Its result
  reached no production reader, and the round receipt's
  `proposal_fingerprint` came from elsewhere. It was deleted rather than
  documented, and the wiring landed separately in
  [#2392](https://github.com/jaspercurry/JTS/issues/2392) — a lean
  `crossover_v2/proposal.py` assembler called from the commit seam, whose
  fingerprint the receipt now names. What that field identifies, and how a
  banked receipt says which regime wrote it, is in "The receipt" below.

What #2291 did **not** close: the hardware run. The definition-of-done's
Pi-side budget line and its live-speaker items need a real speaker, phone,
relay and CamillaDSP, and the issue stays open until that run happens.

### R15 candidate status (2026-08-05)

> [#2106](https://github.com/jaspercurry/JTS/issues/2106)
> owns the re-ratified atomic contract. The branch is under independent
> three-lens adversarial review; nothing here is deployed and no hardware
> measurement is claimed. When it lands, stage 1 is exactly CHECK then MEASURE
> through confirmed role protection, limiter, 0 dB commissioning headroom, and
> physical routing. Configured crossover/delay/polarity, linearization, bass,
> Room, and preference filters are absent; analysis reconstructs
> `M*C_configured/P` across the whole analysis support — as one filter, never
> spliced into a sub-band — for the existing fitter/review/Apply/Undo path.
> Playback is gated on a fresh semantic graph readback normalized through
> CamillaDSP's own `ReadConfig` (see **Confirming a program graph is live**
> below). Recovery is TWO layers, not one: the existing in-process restore
> transaction plus the persisted crash anchor handle an ordinary abort or
> reboot, and a correction-web startup claim converges an abandoned program
> graph that no in-process `finally` could reach because the process died.
> Pre-apply cloud is skipped, while the reusable cloud machinery remains
> available for future Room work. R15 adds no durable anchor/schema/module.
> After R15 comes a hardware checkpoint and then a fresh Gate 0; later round
> labels are provisional, and the [canonical
> plan](linearization-campaign-2026-07.md) owns that contract. The deployed
> pre-R15 flow remains described below.
#### Composing the configured-Fc path

`program_analysis._compose_configured_path_ir` turns a protected-neutral
capture into what the fitter needs: `S = M * C_configured / P` (design §4.2),
applied as **one filter across the whole rfft support**.

It is deliberately not a masked sub-band. Masking the composition to the driven
band steps the spectrum by `|C/P|` at the edges — a pre-fix diagnostic
evaluation over JTS3's declared role parameters put that step at **−41.7 dB**
(woofer top) and **−52.2 dB** (tweeter bottom); no hardware was measured — and
that step rings through the IR and folds back into the analysis band once the
IR is re-gated, which is this campaign's known splice-artefact class. The
pinned number is the golden fixture's: restoring the splice moves **all 8193**
of its bins, by up to **86.999 dB**.

**Where the conditioning policy binds.** §4.2 scopes its finiteness and ±12 dB
rules to *candidate-required trusted fitting or comparison bins*. The mask is
therefore the driven band intersected with what a candidate actually consumes —
its branch radiating span unioned with the trim/alignment overlap band, both
computed host-side in `CrossoverV2Conductor._measure_priors` because they are
crossover policy the measurement kernel may not import. The driven band alone
is a **superset**, and over-refusing a frozen contract is a deviation whose
direction is household-visible hard stops: an analog LR4 `|P|` crosses −12 dB
at `0.7610·fc`, so a declared sweep floor roughly 0.39 octave under the
protection corner would refuse MEASURE on a shape §4.2 admits.

**Since #1654 that is a live margin, not a hypothetical.** The HF sweep floor
now follows the declared HARD band, so JTS3 sweeps its tweeter from 1600 Hz
against a 2000 Hz protection corner: `|P(1600)| = −10.79 dB`, **1.21 dB above
the −12 dB refusal**. Re-derived with this repo's own
`crossover_response_complex`, whose bilinear prewarp makes that ratio
frequency-dependent rather than scale-invariant — `0.7631·fc` at this 1600 Hz
floor, just *above* the analog `0.7610` — the admissible corner is
`≤ 1.31053 · max(sweep floor, Fc/2)` (a *continuous* solve at exactly 1600 Hz;
on the 16384-point analysis grid the edge bin is 1602.54 Hz and `K` is
`1.31052`), solving to 2096.84 Hz here. Solve `K` directly rather than
inverting a ratio: the earlier `1.3108` here was simply the reciprocal of the
ratio this sentence used to carry (`0.7629`), which answers the
**corner-first** question — where `|P|` falls to −12 dB below a *fixed* 2000 Hz
corner, at 1525.74 Hz — not the **floor-first** one `K` asks, the largest
corner admissible above a *fixed* 1600 Hz floor. Inverting across that swap is
the whole 0.0003; the 4-dp rounding is incidental, and in fact moved the figure
0.00005 *toward* the true value. The margin is deterministic (filter math over
declared numbers, not a measurement) but it is thin, and the DECLARATION
spends it: a household declaring a hard floor more than ~0.39 octave under its
protection corner gets a hard MEASURE stop. The corner now follows the
DECLARED low limit rather than a fixed class default: since the 2026-08-17
ruling (#2603), the code-owned class default
(`driver_protection._STYLE_HIGH_PASS_HZ`, 2000 Hz for a compression driver)
is no longer enforced as a minimum by `driver_safety._target_issues` — a
published manufacturer figure wins outright, including below the table's own
number. The table keeps three jobs: the default answer when nothing is
published; the plausibility anchor, which since #2874 refuses a garbage figure
in a *research reply* at intake and DISCLOSES one a human typed rather than
refusing it; and the **commissioning-tone gate's fallback** —
`driver_protection._highpass_satisfied` compares the staged high-pass against
`tone_gate_low_limit`, which resolves declared → legacy protection filter →
this table, so lowering the table still moves an *audible-test* gate and not
only a confirmation one, but only for a driver whose manufacturer publishes
nothing. (Until #2874 that gate compared against `min_highpass_hz` directly,
which refused a tone whose protective high-pass sat at exactly the published
1.6 kHz — the #2603 bug alive on the one surface that ruling missed.) On top of
those, `code_owned_policy` is still fingerprint-checked against it, so lowering
the table's own number still un-confirms every stored profile of that style.
Consequence: #1654's compression-driver instance no longer needs the table
lowered at all — the operator declares B&C's published 1.6 kHz instead, and
the corner follows it — but the general #1654 question stays open.

**Outside those bins** the same exact ratio still applies, magnitude-saturated
at the policy's own +12 dB ceiling — provably inactive where the policy binds,
since a ceiling breach there has already refused. Bins where `P` is exactly
zero (an LR pass at DC or Nyquist) compose to zero, which is what `plant·C` is
there too on the shipped shapes, where `C` and `P` share a filter kind; a `P`
carrying a high-pass under a low-pass-only `C` would zero one genuine
out-of-band bin.

**Residual gap.** The golden fixture supplies both arms through the same
`irfft(rfft(...) * factor)` grid, so the composition's circular wrap cancels
identically between them. Production `full_ir` comes from a real deconvolution
window, so wrap behaviour on a realistic IR is part of the outstanding hardware
gap, not something the fixture can close.

#### Confirming a program graph is live

`crossover_v2_flow.confirm_graph_is_live` is the one policy function that
proves the graph CamillaDSP is running is the graph just submitted. It must
prove the submitted graph is live, tolerate benign serializer normalization,
and reject a different graph.

Comparing the submitted YAML text against `GetConfig` cannot do that. A
2026-08-05 read-only hardware probe on `jts.local` (CamillaDSP 4.1.3, zero
mutation) measured the readback as a strict **superset** of what was
submitted — every optional schema field default-filled, mostly null across
`devices`, `filters`, `mixers`, and `pipeline` steps — and value-normalizing,
with a submitted `gain: 0` coming back as `0.0`. Text equality there refuses
every load, on every box.

So CamillaDSP canonicalizes for us. `ReadConfig` (wrapped as
`CamillaController.normalize_config_raw`) parses, validates, and default-fills
**without applying anything**, and the same probe measured its output exactly
equal to `GetConfig`'s readback for identical content. Normalizing the
submitted graph through it keeps **strict** fingerprint equality — stronger
than a subset or projection comparison — rather than loosening the check.

Not measured: the `SetConfig` → `GetConfig` round trip itself, because the
probe box was playing USB audio and the probe stayed read-only. It no longer
matters: both sides of the comparison now come back through CamillaDSP's single
deserialization path, so the check does not depend on what `SetConfig` does to
the text. The two refusals stay distinct — normalization failure means the YAML
we submitted is invalid, mismatch means something else is live.

### Current status (2026-08-05)

#### Live attempts loop (2026-08-03)

An accepted applied-candidate VERIFY now crosses one lifecycle seam in
`CrossoverV2Conductor`: `ProgramAnalysis.capture_integrity` and the shipped
`max_db_notch_excluded` tracking grade become a realized `AttemptRecord`, and
the pure [`attempts_loop.py`](../../jasper/active_speaker/attempts_loop.py) kernel
judges that record against the immediately preceding accepted candidate. The
record also carries `verify_tracking.frame.n_bins`, the analyzer-owned count
of validity-clamped, notch-excluded bins, so the kernel refuses an apparent
improvement won by grading a narrower denominator. The candidate fingerprint
is the attempt identity; it survives the verify-session rebind, and the store
treats an identical retry as an idempotent no-op while refusing a conflicting
reuse, so a crash/recovery re-verify cannot double-write or masquerade as
another tune. A conflicting recovery capture does not bank a second journey
record or decision under the store-owned identity. Journey-scoped attempt
history
survives the relay-session rebind between apply and VERIFY; capture-phase
evidence remains session-scoped as before. This is rung P4's live seam.

That history survives further than the rebind, and stating only the rebind
understated it: `reset_v2_journey_state` — "Start over", the household's route
to a second tune — deliberately preserves `attempts_loop` so the next VERIFY
has a predecessor to be graded against. Since a new tune re-runs CHECK and
MEASURE, the phone was put down and re-placed in between, while the claim floor
was measured with the mic bolted in place and repeats ~21 s apart
(`captures/repeat-floor-20260731`). So each `AttemptRecord` carries the relay
session that captured it as its `sitting_id`, and `FloorStats.scope`
(`within_sitting` / `across_sittings`) says which comparisons that floor
licenses. A within-sitting floor — the only kind measured — makes `decide_next`
answer `stop_evidence / sitting_mismatch` on a cross-sitting pair rather than
reporting an improvement no study supports; an unrecorded sitting (any state
written before this landed) answers `sitting_unrecorded` and is refused on the
same terms, never read as a match. The done screen names the microphone (never
"the phone" -- #1941 R4) as having been in a different position; the attempt is
still banked, so the next tune is unaffected.
Issue #2081.

The conductor performs no persistence itself. Its injected writer calls
`model_error_store.record_model_error` once for an accepted VERIFY, before
banking the journey projection,
the tracking model's predicted error (`0`) and the analyzer's realized error;
ordinary I/O failures warn at
`event=correction.crossover_v2_model_error_write_failed`, any other store
`Exception` is contained at
`event=correction.crossover_v2_model_error_write_unexpected` (ERROR), and
neither reverses an accepted flow verdict — nor lets the next capture of the
same applied candidate re-fire the write, since the attempt is banked in
history either way (#2386). An identity/value conflict instead warns at
`event=correction.crossover_v2_model_error_identity_conflict` and leaves both
journey record and decision unbanked, rather than publishing two grades for
one candidate identity. The store serializes floor adoption and record writes
on one cross-process lock, so neither read-modify-write transaction can erase
the other's fact. Its `active_speaker.model_error_recorded` event
carries the explicit speaker, attempt, metric, and signed error. Every loop
judgment is visible at
`event=correction.crossover_v2_attempt_decision`; the existing
`crossover_v2` state block exposes only the last decision and the store-owned
record count read fresh at that boundary.
`crossover_envelope_v2.attempt_loop_verdict_sentence` is the sole household
copy writer and formats the kernel's reason, provenance, delta, and the floor
carried on its decision rather than recomputing any of them. Because this live
grade measures applied-response agreement with the prediction—not flatness or
target quality—the sentence says whether prediction tracking moved closer or
farther, never that the crossover itself improved.

Comparability is defense in depth, not a replacement for VERIFY's existing
pre-tracking integrity gate. Any evaluated capture-integrity failure maps to
`comparable=False` and carries both failed and not-evaluated check names into
the kernel's `STOP_EVIDENCE` record. A clean one-sweep VERIFY necessarily has
repeat-only checks marked `not_evaluated`; those names remain disclosed but do
not by themselves make every live VERIFY incomparable.

The claim floor still has one owner and one adoption path:
`model_error_store.stored_floor`, populated only by the offline repeat-study
path (`adopt_floor` / the replay CLI). The live flow never adopts or invents a
floor. When none exists it stores the realized attempt and model error but
labels the decision `ungraded_no_floor`; the household surface explicitly
makes no improvement claim. Capture-integrity refusal outranks that status: a
glitched no-floor VERIFY still reports the kernel's
`STOP_EVIDENCE / attempt_not_comparable`, rather than hiding failed evidence
behind the missing floor.

**2026-07-24 — Layer-1a linearization now EMITS (#1668 PR-D), not yet
hardware-validated.** The fit engine's output (PR-C) now actually reaches
the applied graph — see "Linearization EMISSION" and "Flatness-verify"
below, and [`active-speaker-tuning-layers-design.md`](../active-speaker-tuning-layers-design.md)
for the full Layer-1a design. Landed on a branch, adversarially reviewed
in-session, and hardware-free-tested; it is explicitly gated on JTS3
validation and the owner's listening-ladder protocol before merge — do not
treat this section as confirming an audible result yet.

**2026-07-24 — VERIFY-prediction coherence bug found in that same JTS3
validation, fixed.** Every eligible+fitted candidate failed VERIFY at a
deterministic ~1.7 dB tracking mismatch (three-attempt repeatability
1.688–1.699 dB against the ±1.5 dB `VERIFY_TOLERANCE_DB`): the persisted
prediction `_verify_priors` hands VERIFY was still built from the RAW
measured branches even though the emitted graph carried the Layer-1a
correction filters (PR-D, above) — so the correctly-linearized measured
summation was compared against a prediction that never modeled them. Fixed
in `crossover_v2.intervention.plan_linearization`
([`intervention.py`](../../jasper/active_speaker/crossover_v2/intervention.py)):
whenever it fits (the same eligibility that emits), it now also rebuilds
the persisted prediction from the SAME linearized branches (`W_lin`/
`T_lin`, reusing `program_analysis.predicted_branch_sum` — no second
implementation) at the trim this attempt actually committed to. The
ineligible/raw path and pre-fix persisted eras are untouched — pinned by
`tests/test_crossover_v2_conductor.py`. Offline replay on the real #1667
N=3 capture confirms the mechanism: the predicted tracking mismatch
collapses from a nonzero value (matching the fitted filters' own response)
to exactly 0 dB once the persisted prediction models the same curves the
emitter realizes. Still gated on the SAME JTS3 re-validation as the
paragraph above — this closes a known-bad comparison, it does not by
itself confirm an audible result.

Waves W1–W6 complete (PRs #1578–#1604). Hardware-validated on JTS3 +
UMIK-2: first fully-calibrated run 2026-07-19. **W5b deleted the legacy
flow** (2026-07-24) — v2 is the only flow, per "Only flow — v2" above.
The v2 acoustic playback binding
(`bind_program_playback_seams`) is exercised on real CamillaDSP
hardware; every orchestration test injects fakes. T2's summed-flatness
delay refinement merged via PR #1647 on 2026-07-22. Its first JTS3 run failed
VERIFY, but a clean hardware delay sweep then isolated the wrong-lobe prior and
a one-sided VERIFY smoothing bug. The corrected selector subsequently applied a
53.669 µs woofer delay and passed a calibrated JTS3 VERIFY at 1.279 dB max
(1.5 dB gate); the pre-merge T2-specific adversarial re-review cleared 0
blockers / 0 should-fixes.

The required post-merge UMIK-2 repeat did **not** reproduce that result
(MEASURE railed to a signed −299.948 µs correction at the flatness search
bound; three VERIFY captures failed at 5.264–6.454 dB max; Undo restored the
prior profile). The follow-up diagnosis proved the flatness objective's comb
basin ordering is capture-noise-dependent and replaced the selector: **the
drift-corrected physical peak-gap anchor now owns lobe selection and the
primary delay, refined only by a bounded nearest-GCC-local-peak snap
(±period/6); flatness is evidence, never a selector**. (Superseded 2026-08-16
by issue #2598, which makes summed flatness the selector for the polarity and
delay PAIR with the anchor still centring the search — current state in
"(Polarity, delay) selection" below; this paragraph is the 2026-07-22 record.)
The replacement cleared an independent adversarial review at
0 blockers / 0 should-fixes / 0 nits and its on-device confirmation:
three fresh headless JTS3 flows selected 32.411 / 31.013 / 33.783 µs —
2.77 µs total spread together with the two replayed hardware-anchored
captures — with VERIFY passing at 1.233 and **0.597 dB max** (best recorded
on this rig); the one VERIFY-failed run was a measured room-noise event
(CHECK woofer SNR 17.4 dB vs 23.3 nominal) with the selector still
in-cluster. **The stop rule was then met the same day** (owner-delegated
controlled campaign, quieted room): six consecutive measurement verdicts all
passed — worst 1.106 dB max, five of six ≤ 0.55 — with selections spanning
**1.22 µs** total (median 27.7 µs) against the ±20.8 µs criterion, every
phase single-attempt. One session was a relay-layer transport void (capture
uploaded, never analyzed — issue #1650); ambient-noise events measurably
degrade VERIFY while leaving selection unaffected, and CHECK's woofer-band
SNR predicts VERIFY health — productized as the anomaly-detection/discard-UX
workstream, issue #1652. Final dispositions: Fix 4 shelved (revival trigger:
phone-mic-era cluster spread or `snap_found=false` — since #2598 that flag
describes the SEED's provenance, not the committed delay, so read it beside
`alignment_objective`/`left_anchor_lobe`), T2-robust retired (its
phase-slope core rails systematically on as-crossed branches, +388 ± 38 µs
16/16; its predictive-confidence goal lives on in #1652). The
reproducibility working plan is archived as decision archaeology. See
[`crossover-measurement-reproducibility-plan.md`](crossover-measurement-reproducibility-plan.md)
§10–§11 for the exact evidence and gate state.

The first phone-class-mic series (Dayton iMM-6C on a computer, same headless
path, same evening) mapped the next frontier: woofer-band SNR matches a
reference mic, but its ~8–10 dB lower **tweeter-band** SNR scatters the
anchor/correlation — accepted selections spanned 22.4 µs (vs the UMIK's
1.22 µs), brushing the ±1-sample budget, with the confidence gate refusing
honestly twice (the first such refusals ever — including one snap capped at
its radius edge). Two confounds are being attributed offline before any
hardening decision: an audible `event=outputd.xrun` playback glitch
(15:52:26) in one refusal's window, and hallway transients behind the one
VERIFY fail. Offline forensics then attributed every anomaly (see the
#1652/#1654 comment threads): the xrun capture's sweep segments located
−25…−28 ms off schedule while `glitch_detected` stayed False — the
repeat-pair gate is structurally blind to uniform whole-capture shifts, and
the per-segment location residual/confidence the analysis already computes
is a free detector for it (now enforced — see the measurement-honesty gates
below); the residual mic signal is a single unambiguous
correlation peak at LOW prominence whose position wanders (not lobe
ambiguity). **The naive sub-sample anchor upgrade is refuted** — it left the
iMM-6C span unchanged and degraded the UMIK span 12× in direct testing — so
the standing levers are tweeter-sweep bandwidth (Fix 4, #1654) and/or
energy, decided after the iPhone-chain series. **Bandwidth has since been
pulled** — #1654 landed 2026-08-06, but for the R17 scoring-band unblock, not
for this scatter; whether the extra correlation bandwidth actually sharpens
the phone-mic snap peak is **unmeasured**, so the question above stays open
and energy remains untried. Live trail: #1654 (Fix 4
shelf + mechanism data), #1652 (anomaly detection/attribution), #1650
(relay voids), #1656 (calibration identity — the iMM-6C series silently ran
under the UMIK's calibration curve. **That application is fixed**: the stored
calibration now refuses a device whose label matches a *different* registered
model (#1660 threaded `device` into the room-relay guard, landed #2036). The
**surviving, narrower** fact is that identity binds by **model family**, not by
physical device — two same-model mics with different serials still cross-bind,
and per-serial entry is unbuilt (the serial-entry remainder is #2053).

**A glitch verdict is a timeline SPLICE, not clock drift (2026-07-27,
issue #1765).** Across the whole 2026-07-22…27 JTS3 journal (57 MEASURE
captures) exactly two captures tripped `glitch_detected`, and **both fired
on the residual guard with epsilon comfortably inside `MAX_DRIFT_PPM`** —
2026-07-23 at 30.53 ppm / 798.46 samples, 2026-07-27 at 94.12 ppm / 32.36
samples. The 500 ppm clock-drift bound has never fired in production. Read
`epsilon_ppm` on a *glitched* capture as an artefact, not a measurement.

The 2026-07-27 capture was recovered from the retained dump and
cross-correlated (ncc 0.92–0.99) against the two captures that PASSED
minutes later on the same program and rig: five of its six located sweeps
agreed within 0.8 samples while the first woofer sweep sat 64.3 samples
apart — **one +64-sample (1.333 ms) insertion between `sweep_w` and
`sweep_t`**. That single step predicts every reported number: the woofer
pair straddles it (32.1 ppm true drift + 64/1_036_800 = 93.8 vs 94.118
observed), the tweeter pair sits entirely after it (32.097 ppm = the true
drift), demeaned residual 32.2 predicted vs 32.357 observed, and the
primary-sweep W↔T misalignment drove `delay_us` +15 → −847.6 µs and
`predicted_ripple_db` 4.4 → 16.8.

Attribution: **the leading hypothesis is Pi-side playback fill — and
`event=outputd.xrun` does NOT rule it out.** An earlier pass on this issue
concluded "capture side" from the absence of xrun lines in the playback
window; that inference is invalid, and the owner refuted it directly by
hearing audible tears during sweeps launched from the **command line**,
with no browser in the path at all.

The mechanism: on a short content read `jasper-outputd` zero-fills the
deficit and still writes a full period to the DAC
(`read_content_period` → `out[active..].fill(0)`,
`rust/jasper-outputd/src/alsa_backend.rs`). That INSERTS
`requested − frames` samples into the emitted timeline — audible as a brief
tear, and an *arbitrary* frame count rather than a fixed granule, so a
64-frame fill is entirely ordinary. Production confirms the insertion is
real: `dac_frames_written` exceeds `frames_read` by thousands of frames
(8,576 → 10,752 → 12,928 across three consecutive log lines on 2026-07-27).

**Why the journal looked clean: the `event=outputd.xrun` eprintln fires ONLY
in the `EPIPE`/`ESTRPIPE` branch.** Partial-period fills and `EAGAIN` empty
periods increment `content_partial_period_count` /
`content_empty_period_count` and log *nothing* — those counters surface only
as fields on a later line that some other condition happened to emit
(visible in the data: `empty_periods` climbs by 2 while `count` climbs by 1).
The exact sample-inserting event is structurally silent. Compounding it,
`jasper/correction/runtime_integrity.py` — the layer whose whole job is
"did the audio path glitch during this capture?" — gates on `xrun_count`
and never reads the partial/empty period counters, so a measurement that
spans a fill passes its integrity check.

The fill was periodic on this box: the content hop between CamillaDSP and
outputd absorbed no clock offset, so it accumulated at the ~43 ppm
crystal-vs-crystal rate until the capture ring drained. What matters for
*this* doc: the last fill event before the 2026-07-27 session was 12:39:40
and the next was due inside the failing capture's playback window. That is
NOT proof for that capture — outputd restarted around the session (counter
reset 89 → 1) and the counters were never sampled then — but the signature,
the arbitrary fill size, and the audible tears all fit.

The fill is no longer silent: `event=outputd.content_fill` plus the
`outputd_content_fill_increased` gate in
[`jasper/correction/runtime_integrity.py`](../../jasper/correction/runtime_integrity.py)
mean a capture that spans one is now flagged rather than passing its own
integrity check (#1768). Still open: step-aware recovery using the N=3
redundancy already paid for — a located step lets the analysis pick a
step-free sub-window instead of retrying. `_locate_discontinuity` now names
the step (size + which segment it landed after) on every MEASURE capture,
which is the input that work needs — see the diagnostics section below.

**Measurement-honesty gates (2026-07-22 night).** Three additive acceptance
gates convert the corrupted-capture signatures above into honest
refusals/retries — no selection math and no VERIFY comparison semantics
changed. **G1 is no longer one of the refusals: since the owner's 2026-08-03
ruling (#2087) it DISCLOSES.** MEASURE accepts a candidate whose
`predicted_ripple_db` exceeds `MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB`
(15 dB; the corrupted phone solve predicted 27.3 dB where every clean capture
that day predicted 4.4–9.0) and banks a reservation the household reads as one
plain sentence on the review and done screens — see "G1 discloses, it does not
refuse" below. MEASURE
rejects-and-auto-retries as a glitch when any sweep locates off schedule
(`_sweep_schedule_ok`: |residual| > 5 ms; the xrun signature was −25…−28 ms
vs ≤1.5 ms on every clean capture — reuses `drift_baselines_disagree`).
**The locate-confidence half of that gate split off in #1838** into
`_sweep_locate_confidence_ok` (< 0.3; the xrun signature was 0.07–0.12 vs
≥0.69 on every clean capture): a sweep the locator can barely find is not a
splice, so it answers `locate_failed` and does NOT auto-retry a level that
cannot win.

**It is not necessarily a capture that was too quiet, either — that was the
#1838 reading, and #2085 measured it false.** A JTS3 sitting on 2026-08-03
produced three `locate_failed` captures whose own leading pilot pair cleared
the room floor by 13.9–15.5 dB (`pilot_snr_ok=true`) while the sweeps scored
0.019–0.097. The speaker had been heard. Forensics then found the audio
pristine as well: `_global_offset` anchors on `pilot_lo` (the quietest segment
in the program), those three missed `_earliest_strong_peak`'s gate by an NCC
margin of 0.005–0.049, the anchor snapped to `pilot_hi` — +1296.5 ms, exactly
the pilot spacing, on all three — and the ±30 ms `SEGMENT_SEARCH_S` window
then guaranteed every sweep was "not found". Re-scored with a whole-capture
search the same WAVs give 0.67–0.82. **The anchor is being fixed separately;
the copy change is what stops the household being blamed meanwhile.**

So the gate measures locate CONFIDENCE and nothing more, and BOTH available
causal stories ("too quiet", "capture corrupted") can be false at once. The
copy is therefore no longer a literal on `REASON_LOCATE_FAILED`:
`locate_failed_message` picks it from `analysis.pilot_snr_ok` — heard ⇒ report
that JTS could not line up the tones, name no cause, ask for a retry; unheard
or unmeasured ⇒ the original level/microphone copy. One writer, read by every
surface that narrates the failure — the relay verdict, terminal exhaustion,
the defensive replay refusal in `authorize_begin`, the apply-seam refusal, and
the envelope. Retryable copy and terminal copy share the diagnosis selected
from this capture's evidence; only the available action differs. That
completeness is the point, not tidiness.

**Both of those gates are MEASURE-only, and VERIFY got its own in #1971.**
They filter `KIND_SWEEP`; VERIFY plays one `KIND_SUMMED_SWEEP`, so until
2026-08 no splice/schedule check on this system had ever looked at a VERIFY
capture — and `glitch_detected` there came from `_estimate_drift`, whose
three inputs all compare a role's repeated sweeps and so cannot exist on a
single mono sweep. It was structurally `False` on every VERIFY analysis ever
taken: a False that meant *nobody looked*. `program_analysis.
_verify_capture_integrity` now produces a per-check
`ProgramAnalysis.capture_integrity` record on every verify-shaped analysis —
**heard** (the same 0.3 floor), **on-schedule** (the same 5 ms ceiling,
inherited from the MEASURE evidence above and *not* re-derived on a VERIFY
corpus), **unclipped** — plus an explicit `not_evaluated` entry, with a
reason, for each MEASURE-era check that structurally cannot run there. The
conductor gates on it ahead of everything the tracking verdict rests on
(`_verify_verdict`), so a spliced or clipped capture never produces a
pass/fail tracking answer: unheard ⇒ `locate_failed`, spliced or clipped ⇒
`drift_baselines_disagree`. The third substitute the P0 bench used by hand,
gate-window comparability, was already the inconclusive rule below and is
**not** restated in the record — the conductor is the only holder of the
MEASURE window. VERIFY refuses
with the new `verify_level_shift` reason (verify-fail template, budget 2)
when a later attempt's summed-pilot transfer steps more than 0.35 dB from
the session's first verify attempt (the phone chain stepped 0.75–0.82 dB
across the dishonest 1.19→2.11→2.84 dB attempt sequence; the one clean
multi-attempt session stepped ≤0.05 dB). **That reference is SESSION-SCOPED
since #1927** (owner ruling 2026-07-31): every session sets its own from its
own first usable attempt, and a previous session's number travels forward only
as dated history — the disclosure line `level reference reset for this session
(the previous one, <when>, was <x> dB away)`, rendered in the verify screens'
collapsed expert details on every outcome and logged as
`event=correction.crossover_v2_level_reference_reset`. It used to be
rehydrated from `verify_priors` and never re-baselined, which made a
day-later "verify later" dead-end on ordinary mic re-placement (2026-07-30
bench, 0.775 / 0.777 dB, deterministic to 0.002 dB). A session whose plan
contains MEASURE drops the history rather than risk carrying it across an
apply, since a pilot transfer is captured through the applied graph. All thresholds are PROVISIONAL
named constants in `crossover_v2_flow.py`; the per-capture diag events
carry the new numbers plus a `guard` disambiguation field. Offline proof
(45-capture retention archive + both hardware-anchored overlay runs): zero
false fires, every must-refuse capture refused — evidence + replay scripts
in `captures/xover-e0-2026-07-21/honesty-guards-proof-20260722/` (session
artifact, not in-repo).

---

### Architecture detail — the capture flow, the groups, and the graded round

> **AUTO-APPLY IS GONE (two-stage commission work order D1, PR-T3,
> 2026-07-29).** Everything below that describes the host "firing the
> auto-apply on a background thread once the candidate-carrying verdict
> lands", VERIFY being "soft-held until the auto-apply completes", or an
> "auto-apply failure" draining the volume, is **history, not current
> behaviour**. No session applies anything: the pre-apply cloud's close is
> now the household's explicit `complete_capture_set` signal, it produces a
> PROPOSAL, and the only path that applies is their own POST to
> `/crossover/v2/apply` from the `review` screen. `_fire_auto_apply`, its
> thread, and its idle-exit hold are deleted; the `awaiting_apply` deferral
> and `REVIEW_HOLD_BUDGET_S` are RETAINED but unreached (D10). The
> accountability veto (`_assert_accountable`) is untouched and now refuses
> on the confirmation instead.

#### The capture flow — TWO sessions since the two-stage split

The journey is **two relay sessions** with an untimed household decision
between them (two-stage commission work order D1/D2, PR-T3). Both use
`crossover_v2:session` / `crossover_v2:verify`; the conductor hands
`authorize_begin` / `on_armed` / `consume_capture` to `run_capture_plan`
in each.

**Stage 1 — 3 captures at either tier.** `STAGE1_INCLUDES_ENTRY_BASELINE` is
`True` (#2291 Phase 3c) and `STAGE1_INCLUDES_CLOUD_MEASURE` (R15, #2106) is
`False`, and no stage-1 plan builds a `lateral` group, so a shipped session runs
the anchor pair, then one summed capture at the mark, and emits no
`cloud_measure` phase and no `lateral` phase of its own (a staged angle walk
adds one — see "Only a walk the FIT reads" below). Production passes
the same resolved protection mapping to the protected-neutral emitter and
configured-path analysis. Stage 2 is unchanged. (R15's two-capture stage 1 —
`check` then `measure`, hardware-proven 2026-08-05 — is what this replaced.)

R17 (#2173) flipped `STAGE1_INCLUDES_LATERAL` on, because Gate 0 pairs every
producer with a current consumer and the walk's is
`fc_selector.score_candidate`'s lateral robustness term — the only evidence in
a session that a candidate's handoff survives off the design axis. The
**structural** blocker cleared first: #1654 made the HF sweep floor follow the
declared hard band, so on JTS3 `overlap_band_hz` clamps to 1600 Hz instead of
2 kHz and a sub-2 kHz candidate's own handoff is inside its scoring band
(Fc 1700 → `(1600, 3400)`, was `(2000, 3400)`). Candidates at or below the
sweep floor itself stay unscorable — at Fc 1600 the handoff sits exactly on the
band edge — so the honest downward limit is **Fc strictly above the declared
hard floor**.

**The walk was paused on 2026-08-18**, owner-ratified on a recompute over the
8 banked rounds carrying an `fc_selection`: it was 59.4% of all banked session
audio (1,649 of 2,776 s) and the largest retake source (9 of 13 rejected
captures), it never changed an outcome (8 of 8 committed the configured Fc),
and the max-over-poses scalar it feeds adjudicates below its own noise —
3.54 dB same-candidate repeat noise against rank-1-to-rank-2 gaps of 0.004–2.13 dB,
with the closing at-mark repeat frequently carrying the argmax in 4 of the 8.
The pose measurements are sound (inter-driver drift 0.6–1.9 dB against a
0.09–0.32 dB mark-return floor; ±40 cm ~2.2× the ±12 cm pair in 8 of 8), so
the machinery below is intact and unmodified — this is a paused producer
awaiting a redesigned statistic, not a retired one. **The Fc candidate sweep
is dormant with it**, since it fires only for a walk whose consumer
adjudicates.

> **Superseded 2026-08-21.** The sweep was deleted, not left dormant, and the
> stage-1 arming with it — see the spine's "Stage 1" section and
> `docs/tuning-master-plan.md` ticket 2.3. The pose machinery below is still
> what an operator's staged angle walk runs.

Everything from here to the end of this subsection describes the walk **as it
runs when the flag is forced back to `True`** — indexes 3–8, stage 1 back to
9 captures:

| index | phase | gate | what it is |
|---|---|---|---|
| 1 | `check` | tap | microphone check |
| 2 | `measure` | tap | design-axis anchor, per-driver |
| 3–8 | `lateral` | tap each | 6 prompted poses (plan §4.4) — **paused, flag-off today** |
| 9 | `entry_baseline` | tap | summed sweep at the mark, the round's measured "before" (#2291) |

The walk is the mark, ±12 cm and ±40 cm left/right, and a return to the mark.
Four things about it are load-bearing and easy to undo by accident:

- **It replays the ANCHOR's program object**, not the summed sweep every cloud
  position plays (`program_for_phase`). §4.4's uses are per-driver
  comparisons, which a summed curve cannot answer; and the return-to-mark
  bracket is only a repeat measurement because the stimulus and the solved
  gains are identical. So `lateral` is deliberately NOT in `SUMMED_SWEEP_PHASES`
  — a pose plays through the protected-neutral commissioning graph.
- **A pose is analyzed NEUTRALLY.** `_lateral_priors` withholds the three
  configured-path composition maps the anchor gets, so the retained curve is
  `M = plant · P` and §4.2's `sign_c · M · C_c / P` stays a per-candidate step
  in the consumer. `configured_path_composed` is therefore `False` on a pose,
  and the fitter's uncomposed-capture rail is what keeps one out of a
  prescription: a pose is never fitted.
- **The anchor solution is held fixed.** `LateralPose` has no trim/delay/
  polarity field, and the three MEASURE gates that judge the alignment SOLVE
  (delay-search status, GCC trust floor, plausibility backstop) do not run at a
  pose — a microphone 40 cm to the side legitimately fails them, and refusing on
  them would keep only the poses that align like the anchor. Every other MEASURE
  capture-integrity screen does run, in MEASURE's order.
- **Its position floor is ZERO** (`_group_position_floor`). A cloud below
  `MIN_RESOLVED_CLOUD_POSITIONS` has nothing to combine and ends the session; a
  dropped pose costs a robustness sample and nothing else, so the walk continues
  and the consumer discloses that it decided on fewer positions than planned.

Retention is `CrossoverV2Conductor.lateral_poses` — in memory, no durable
schema. Each pose holds one `LateralPoseCurve` per role: complex values
**sampled** (never interpolated) at the nearest native bin of a fixed
1/12-octave 20 Hz–20 kHz basis, plus that role's driven sweep band. ~120 points
× 2 roles × 6 poses. Deliberately coarse — #1968 calls lateral samples a coarse
gate, and this is not a polar measurement.

The RAW-WAV side is not free, though: flag-on takes stage 1 from 3 captures to
9, so an operator running with the dump ring enabled ("Operator capture
retention" below) keeps roughly a third as many past sessions in the same
fixed-size ring. Worth knowing before a debugging session that expects last
week's captures to still be there. It is also the largest single cost of the
walk, and half the reason for the pause above: six replays of the anchor
program were 59.4% of all banked session audio.

`lateral_mark_return_drift_db()` is the walk's own honesty screen: per-role
worst |Δ dB| between the two at-mark poses, in band. **Reported, never gated** —
no evidence in this campaign fixes a threshold — and `None`, never `0.0`, when
a bracket pose is missing. Journalled at the walk's close as
`event=correction.crossover_v2_lateral_walk_closed`.

**The fit runs at the last capture of the last GROUP**, and R16 makes the
walk's close that capture (`_close_lateral_walk`) rather than MEASURE's accept.
Same rule the cloud's own deferral implements, for the same reason: a proposal
built before the walk would predate five minutes of evidence the household was
just asked to produce. A walk whose FINAL pose is dropped still closes — the
anchor's coefficients were never the poses' to withhold. With the walk paused
there is no group to wait for, so the fit runs at MEASURE's accept; #2291's
entry baseline follows it without deferring it, because that capture is the
round's "before" rather than an input to the fit.

**And the one lateral group that is NOT the flag's — only a walk the FIT reads
defers it.** Since #2732's take (2026-08-19) a lateral group declares its
consumer, and a walk an operator staged with `jasper-angle-capture` declares the
offline forward model. Nothing in-session reads that one as a whole, so MEASURE
publishes at its own accept, `_close_lateral_walk` suppresses itself by name
(`event=correction.crossover_v2_lateral_close_suppressed`), and R17's candidate
sweep never arms. Every OTHER statement about the walk in this subsection —
above and below — is the selector walk's, and unchanged.

> **Superseded 2026-08-21.** Both walks now close the same way: one event,
> `correction.crossover_v2_lateral_walk_closed`, and nothing published. See the
> spine's "Stage 1" section.

**Deployed pre-R15 Stage 1 (`POST /crossover/v2/session`), 10 captures at Full:**

| index | phase | gate | what it is |
|---|---|---|---|
| 1 | `check` | tap | microphone check |
| 2 | `measure` | tap | design-axis anchor, per-driver |

**Deployed pre-R15 Stage 1 (`POST /crossover/v2/session`), 10 captures at Full:**

| index | phase | gate | what it is |
|---|---|---|---|
| 1 | `check` | tap | microphone check |
| 2 | `measure` | tap | design-axis anchor, per-driver |
| 3–10 | `cloud_measure` | tap each | 8 prompted pre-apply positions |

Index 10 is also `capture_target`, so the runner would ordinarily end the
set on its acceptance. It does not: the host **holds** the set open
(`completion_signal_required` / `on_completion_signal`) until the phone
posts `complete_capture_set` — the household's "Continue" on the "all
spots measured" screen. That signal is what closes the group and
publishes the candidate; until it arrives the final position is
still retakeable, and no `capture_set_complete` is posted (which is what
keeps the phone on the confirm screen rather than an end screen).

**The FIT usually already ran by then** (eager-fit rider, owner UX
direction 2026-07-30). Index 10's acceptance starts it speculatively on
a background thread, so the household's Continue normally commits a
finished candidate instead of paying for the several seconds the fit
costs while they stand at a browser. A voluntary retake of the final
position discards that build and the next acceptance re-fits, so the
signal still publishes a candidate fitted from exactly the cloud the
household accepted. What did *not* move: the retake window (the held-set
predicate now asks `_group_confirmed`, "has the household confirmed?",
precisely so an early build cannot close it), the trust gates, or the
rule that nothing applies inside this session.
**Nothing is applied inside this session.** The household returns to
jts.local and lands on the `review` screen.

**Stage 2 — verify (`POST /crossover/v2/verify` with
`{"stage": "post_apply"}`), 6 captures at Full:**

| index | phase | gate | what it is |
|---|---|---|---|
| 1 | `verify` | tap (confirm-then-tone) | design-axis anchor, summed |
| 2–6 | `cloud_verify` | tap each | 5 prompted post-apply positions |

The same endpoint with no `stage` (every shipped caller, including
`verify_retry` on a failed screen) is the **1-entry recovery re-verify**,
byte-identical to what it has always been.

**Express tier (`TIER_EXPRESS`, flow-simplification PR-U1)** is the same
layout at a smaller shape — (N=5, M=1), so stage 1 is 6 captures (index 1
`check`, index 2 `measure` anchor, indexes 3–6 four prompted
`cloud_measure` positions) and stage 2 is 1 (the anchor at the mark).
`M=1` means **no `cloud_verify`
phase at all** — the done screen rides stage 2's VERIFY entry itself, and
there is no post-apply cross-position claim (see
[`linearization-campaign-2026-07.md`](linearization-campaign-2026-07.md)
§1.3's degraded-claims table). `resolve_plan_shape` in
`crossover_v2_flow.py` is the single place both counts are resolved from
a tier id; `express_cloud_measure_positions()` derives the 5 (the
`cloud_measure` group's total captures — the anchor plus 4 prompted
positions) from `CLOUD_POSITION_PROMPTS`' wide-offset guarantee, never a
hardcoded literal (N3 fix, adversarial review of PR #1780: the function
derives the position COUNT, not the curve count the anchor is excluded
from — see "Positions are not curves" below).

The two `cloud_*` phases are **position groups** (flat-linearization
PR-3b): one phase spanning many capture indexes, one prompted mic move
each. They exist because
[`linearization-campaign-2026-07.md`](linearization-campaign-2026-07.md) fundamental 1
makes the spatial cloud *the* measurement and demotes single-point
capture to a diagnostic — spatial averaging is what removes early
boundary interference that gating alone cannot. Read that doc for the
physics; this section is the operational shape.

What is **not** cloud-averaged, by plan doctrine: alignment, level,
polarity, and per-driver linearization stay single-position at the
design-axis anchor (MEASURE). VERIFY's tracking comparator likewise
stays at the anchor — it answers "did apply do what the model
predicted", which a moved mic cannot answer.

**When the fit runs** (owner decision, 2026-07-27). The fit still reads
MEASURE's single-position analysis — the line above is unchanged — but it
RUNS at the pre-apply group's close (index 10), not at MEASURE's accept
(index 2), and takes the closed cloud's honesty verdict as an extra
constraint. That is what makes the correction envelope's two cloud terms
(`spatial_exclusion_limit`, `position_stability_limit`) reachable at all:
before the move the candidate was built eight captures before the cloud
it was supposed to consume, and auto-apply fired while positions 3–10
were still being walked — so the "pre-apply cloud" was captured through
an already-corrected speaker. The move closes both. Every MEASURE trust
gate stays at index 2 (they read the analysis, not the candidate), so a
doomed session still fails at sweep two rather than after a nine-position
walk. A session with **no** pre-apply group — the pre-cloud 3-entry shape
the conductor defaults to, and the 1-entry re-verify path — still builds
at MEASURE with the same accept, payload keys and apply timing it had
before: the rule is "the fit runs at the last capture before the apply".
No wire bytes, screens, or plan entries changed; only conductor-internal
timing. (Those shapes' `candidate.json` does gain an always-empty
`exclusion_evidence` key, which is omitted from the fingerprinted core
when empty, so the fingerprint is unchanged.)

The deferring shape holds MEASURE's analysis across the prompted walk,
because it is the fit's input. That retention is scoped tightly on
purpose: the object is dominated by per-occurrence float64/complex128
arrays on the analysis FFT grid — one two-occurrence `DriverResponse`
measured **33.6 MB** on the S0 corpus's own grid (524,289 bins;
production MEASURE uses a different program and grid, so read this as
the order of magnitude, not the number) — so a session that never defers
does not store it at all, and the FIT releases it as soon as it has
consumed it.

Re-consumption cannot strand on that release even though a cloud
group's CLOSE is not exactly-once (issue #1872): a geometry-locked
retry's own retake, or a voluntary retake (§2.6), can re-close the
group, and `_close_cloud_group` recomputes the honest-instrument
pipeline on every close — only the durable evidence-artifact publish is
a per-phase singleton (the write-once evidence store). What IS
exactly-once is the FIT itself: `confirm_cloud_measure_group` refuses a
second call once `self._candidate` is set, and every retake shape lands
BEFORE that confirm — a geometry retake returns REJECTED well before any
confirm, and a voluntary retake is only admitted while the confirm has
not happened yet — so the analysis release can never be reached twice.

1. **CHECK** (~25 s, one tap). Ambient silence + two band-limited pilot
   chirps per driver at two levels (−10 dB apart). Yields the ambient
   floor, the behavioral AGC/linearity verdict, channel-map sanity, and
   the **solved gain plan** for MEASURE — per driver, at the quietest
   level that still clears the SNR the fit needs in that driver's own
   band (gotcha #22). Replaces the legacy per-driver level ramps and
   ambient waits.
2. **MEASURE** (~40 s, one tap — the longest capture of the session, and
   the one that can be the loudest, so it asks before it plays; issue
   #1823).
   2-channel routing: pilot pair + guard silence + **three interleaved
   woofer/tweeter sweep cycles** — `w1 → t1 → w2 → t2 → w3 → t3`
   (sweep-composition PR-A, #1668; was one woofer-only repeat, ~+15 s
   program length). Every cycle past the first is bit-identical to that
   driver's first sweep — the repeats form the in-capture drift estimator
   + glitch detector, now for BOTH drivers. Yields per-driver gated complex
   responses (cal applied), relative
   delay, polarity, trims, per-band SNR — folded into a
   `MeasuredCrossoverCandidate`. GCC-PHAT supplies a drift/parallax-corrected
   seed, polarity, and capture confidence. The delay actually selected and
   applied is the minimum-ripple applied delay
   within the crossover region's declared `delay_range_ms` magnitude range
   (plus the same plausibility margin used by the conductor). The
   drift-corrected physical peak gap supplies the sign and centers one
   ±half-period comb lobe inside the range; GCC is deliberately not the lobe
   prior because its periodic peak can identify a neighboring comb basin.
**Layer 1a driver linearization (#1668 PR-C).** MEASURE additionally fits
a per-driver linearization (a rising-slope Highshelf + Peaking cuts, plus the
CD-horn top-octave give-back stage — a Lowshelf backbone + optional trailing
Highshelf taper, #1668 — honoring the correction envelope's per-bin depth
ceiling). It is **cut-only unless the evidence gate grants a lift vocabulary**
— see `allow_boost` in `crossover_v2.intervention.plan_linearization`; the
same envelope ceiling bounds a boost as bounds a cut. Fitted whenever the mic
resolved to the "reference" trust tier AND both
drivers cleared a paired ≥3-occurrence gate — otherwise the candidate is
byte-identical to the plain trims-only shape from before this PR. When eligible,
the fit is applied to each branch in the linear domain BEFORE the trim solve, so
trim reflects the linearized (not raw) response — the ordering that structurally
defuses #1667's band-average bias. The linearized trim is then ANCHORED (#1668,
after the 2026-07-24 JTS3 runs): each branch's raw committed trim plus the level
its own emitted cascade removed from its reference band
(`LinearizationFit.correction_giveback_db`), normalized non-positive — a
level-preserving give-back that replaced the `solve_branch_trims` overlap-band
seed, which returned only 5.81 dB of a 9.27 dB spend and left the tweeter band
~3 dB low. A ripple scan that then drifts >6 dB from that anchor is discarded in
favor of the anchored pair.
The fit result travels on `MeasuredCrossoverCandidate.linearization` (empty
dict = not attempted), and — since the 2026-07-27 timing move — the cloud
inputs that constrained it travel beside it on
`MeasuredCrossoverCandidate.exclusion_evidence`: the merged honesty intervals
handed to `spatial_exclusion_limit`, the `band_spread`/`n_positions` behind
`position_stability_limit`, and the identified-null registry with its τ/r per
null. Empty dict = no cloud evidence entered this fit, which is also every
pre-move candidate's implicit claim. Enough to re-derive both envelope terms
from `candidate.json` alone; it deliberately duplicates data in
`cloud_measure.json`, because that file is prunable session evidence while this
travels with the correction it justifies. Same optional-field conventions as
`linearization` (omitted from the fingerprint when empty, fingerprinted when
present, accepted absent on `from_mapping` — that last one is load-bearing
and was a caught blocker: `to_dict()` always writes the key, so the reopen
comparison must `setdefault` it or every pre-PR-6b candidate refuses as
`candidate_tampered` on the live apply path). Measured cost: **5,294 bytes**
of `candidate.json` on the S0 ten-position cloud — registry 3,307,
`band_spread` 1,596, intervals 287 — scaling with what the honesty
instruments found, not with capture length. Design and fitting-policy SSOT:
[`active-speaker-tuning-layers-design.md`](../active-speaker-tuning-layers-design.md)
"Layer 1a concretely"; engine at
[`jasper/active_speaker/linearization_fit.py`](../../jasper/active_speaker/linearization_fit.py),
correction-envelope core at
[`jasper/active_speaker/linearization_envelope.py`](../../jasper/active_speaker/linearization_envelope.py).

**Linearization EMISSION (#1668 PR-D).** The fit result is no longer
evidence-only: `emit_active_speaker_baseline_config` (`camilla_yaml.py`)
gained a `linearization` parameter — one Peaking/Highshelf/Lowshelf chain per
role (the CD-horn stage adds a leading Lowshelf backbone and an optional
trailing Highshelf taper, #1668), emitted immediately after that driver's
crossover HP/LP and before
bass-extension, via the shared `emit_filter_spec` primitive. The two
RICH-candidate seams —
`measured_crossover_candidate.compile_candidate_config` and
`baseline_profile.build_baseline_profile_candidate` (which also carries the
reduced result in the applied profile's `recomposition_snapshot` and
top-level payload) — thread a persisted `LinearizationFit` result through
the shared reduction, `linearization_fit.linearization_filters_by_role`.
`baseline_profile.recompose_applied_baseline_yaml` (the `/sound`
preference-EQ seam) — the fix for a since-closed silent-reversion gap —
deliberately does NOT call that helper: its snapshot's `linearization` key
is already in the reduced shape `build_baseline_profile_candidate` wrote,
so it re-validates that shape inline, era-tolerantly, instead of re-reducing
it (calling the shared helper on an already-reduced mapping silently
returns nothing for every role). The net effect is the same — recompose no
longer silently drops the stage on every EQ/room recompose — but the
reduction path differs; do not "consolidate" the two onto one helper call.
The runtime-safety verifier
(`runtime_contract._baseline_output_chain`) independently re-proves any
linearization-named filter in the emitted chain (Peaking/Highshelf/Lowshelf
type, non-positive gain, one leading shelf + one optional trailing Highshelf
taper) — self-proving from the graph text alone, so no new
evidence parameter needed threading through `classify_camilla_graph`'s
other callers. Emission is empty by default; a candidate/snapshot with no
`linearization` key, or an empty one, stays byte-identical to the
pre-PR-D graph.

**Gauge fix (2026-07-24): the WHY travels alongside the WHAT, and the
OBSERVE-layer honesty ladder reaches the wizard.** Before this fix, the
conductor's linearization outcome (one of "fitted" /
"trim_rejected" / "ineligible_mic_tier" / "ineligible_repeats" /
"fit_failed" / "" — since #2291 Phase 2b a field on the build's own
`_LinearizationState`, a `_last_*` conductor attribute before that)
lived only in memory, logged once per MEASURE attempt —
linearization could silently not run while every screen looked the same.
`MeasuredCrossoverCandidate.linearization_outcome` now carries the SAME
verdict as a new, era-tolerant, fingerprinted field, threaded verbatim
through `build_baseline_profile_candidate` (top-level `linearization_outcome`,
sibling to `linearization`) and `_frozen_applied_profile` (same allowlist
fix `linearization` itself needed for Gap 3c) into
`setup_status.read_active_speaker_setup_status`'s `protected_profile`
block, surfaced on `/state.active_speaker_setup.protected_profile.
linearization_outcome`. Separately, `jasper.web.correction_crossover_v2.
_candidate_summary` reads each role's `observe_octave_summary` (the
OBSERVE-layer honesty-ladder field above) straight off the live session's
rich candidate and threads the 8k/12k/16k values into the wizard's RESULT
screen (`candidate_review.linearization_octaves`,
`candidate_review.linearization_outcome`) — the number that says "the top
octave is 9 dB down and nothing corrected it." Each octave's
`reason_summary` verdict rides alongside it since #2638
(`candidate_review.linearization_octaves[].reason`), because the same
subtraction past a driver's own band returns a large POSITIVE number that is
the crossover's rolloff rather than performance; what the screen does with
that is owned by `crossover_envelope_v2._linearization_octave_rows` and the
renderer beside it. That reduction is
session-scoped only (mirrors `linearization`'s own reduced applied-profile
copy, which strips the honesty-ladder fields via
`linearization_filters_by_role` — see that function's docstring); it does
not thread into the durable applied-profile artifact.
**The level-match frame (PR-L3, 2026-07-27).** `solve_branch_trims`
([`jasper/audio_measurement/program_analysis.py`](../../jasper/audio_measurement/program_analysis.py))
reads **each branch on its own side of Fc** — `[Fc/ρ, Fc]` for the woofer,
`[Fc, Fc·ρ]` for the tweeter, log-symmetric by construction
(`branch_level_bands_hz`), with `ρ ≤ 1 octave` narrowed by whichever
branch's own validity span binds first. It is never handed the SHARED
both-branches-excited overlap band.

Why: band-power-averaging is a level match only when each branch is
weighted symmetrically about Fc. The shared band's lower edge is clamped UP
to the tweeter's sweep floor (`overlap_band_hz`), and a tweeter swept from
Fc upward leaves `[Fc, 2·Fc]`, entirely on the side
where the woofer is inside its crossover skirt. That measures skirt depth,
not sensitivity: **+10.59 dB on an ideal LR4 pair with two equal-sensitivity
drivers** (closed form, pinned by
`test_one_sided_overlap_band_biases_the_level_match`). On the archived JTS3
MEASURE captures it put the tweeter trim 10.9 dB (2026-07-27 session
`d5b171fa81a5`) and 13.1 dB (2026-07-25 run 5) below the same analysis's own
per-driver `target_level_db` frame — the ideal-pair figure accounts for the
observed error to within **0.27–2.47 dB** across the two sessions, the
remainder being each real driver's own rolloff on top of the filter's. That
trim is what the linearized give-back anchors on — the origin of the
~10 dB-dark tweeter. Widening back
to the nominal `[Fc/2, 2·Fc]` is not a fix either (+3.03 dB residual on the
same ideal pair, and it averages the tweeter over bins it was never excited
in).

**What #1654 changed here, and what it did not.** The archived numbers above
were measured when the tweeter's sweep floor WAS Fc, making the shared band
exactly one-sided. It no longer is: the floor follows the declared hard band,
so on JTS3 the shared band starts at 1600 Hz and the sub-Fc bins are now
genuinely excited rather than absent. The log-symmetric
`branch_level_bands_hz` frame is unchanged and still correct — it never read
the shared band — so this is context for the archived figures, not a reason to
re-open the frame. A box whose declared hard and analysis floors coincide still
gets the fully one-sided band.

Every MEASURE analysis now discloses the frame:
`event=program_analysis.branch_level_match` carries `level_w_db`,
`level_t_db`, and both bands, to be read beside the per-role
`target_level_db` that `correction.crossover_v2_linearization_giveback`
carries for the same capture.

**Ripple-optimal trim polish (#1667 Phase 3, scoped by PR-L3).**
`solve_ripple_optimal_trim` re-solves the tweeter trim for minimum
summed-response ripple, scanned in a bounded window (±10 dB,
`RIPPLE_TRIM_SEARCH_WINDOW_DB`) around the band-average seed and clamped to
the physically valid attenuation range; a result more than
`REALIZED_LEVEL_MATCH_TOLERANCE_DB` (3 dB) from the seed is distrusted and
discarded in favor of band-average, with a WARNING and a
`ripple_polish_rejected_delta_db` on the candidate (never a silent wild trim).
That bound is the realized-level gate's own tolerance and not a number this
seam owns: the excursion passes straight through to the committed pair, so a
polish admitted past it produces a round the gate can only report against. It
was `RIPPLE_TRIM_SANITY_MARGIN_DB` (6 dB) until the realized-level demotion
([`measurement-loop-doctrine.md`](../measurement-loop-doctrine.md) deviation (i))
deleted that constant and coupled the two. Selection is flat-minimum-regularized (architect follow-up): among
every scanned candidate within `RIPPLE_TRIM_FLAT_MINIMUM_EPSILON_DB`
(0.25 dB) of the scan's global minimum ripple, the one closest to the
band-average seed wins — a shallow ripple bowl's exact minimizer is
sensitive to measurement noise and would otherwise wander session to
session, which is a worse product property than a fraction-of-a-dB of extra
ripple.

The polish runs **only where its own band straddles Fc** — at BOTH call
sites: the raw candidate's
(`event=program_analysis.ripple_trim_skipped`) and the Layer 1a linearized
re-solve's (`event=correction.crossover_v2_linearization_ripple_trim_skipped`),
the one whose result becomes `role_attenuations_db`. Both carry
`reason=ripple_band_one_sided`. #1667 was written against a
biased seed and its whole corpus was one-sided geometry, where the summed
ripple is the tweeter's own and barely responds to the tweeter's gain: it
recovered only 2.1–4.1 dB of the 10.9–13.1 dB frame error, and once PR-L3
fixed the seed the same objective pulled the other way (replayed on the
2026-07-25 run-5 capture it moved an unbiased −12.368 dB seed 7.9 dB back
down, stopped only by the sanity guard). A selector that cannot see the
woofer does not set the woofer's handoff level. Both the scan and its
straddle guard are wired into BOTH paths — the raw candidate
(`CrossoverCandidate.trim_db`, with the band-average seed preserved as
`trim_band_average_db` evidence) and the Layer 1a linearized re-solve above
— so consumer/phone-tier captures, ineligible for linearization, get the
same treatment. The linearized-path trim is correct
only with the linearization filters emitted (#1668 PR-D); the two land
together. Design rationale:
[`active-speaker-tuning-layers-design.md`](../active-speaker-tuning-layers-design.md)
"Decisions already made" #2 and "Execution plan" Phase 3.
3. **CLOUD-MEASURE** (8 × ~16 s, one tap each). The pre-apply spatial
   cloud, between MEASURE and APPLYING: the same mono summed sweep
   VERIFY plays, captured at prompted positions around the mark.
   Per-position work is deliberately light — the same locate/linearity
   screens plus "did this yield a usable summed response"; the heavy
   pass runs once at group end. See "Position groups" below.
4. **APPLYING** (control page, no capture — auto, since 2026-07-20). The
   conductor itself evaluates the candidate: alignment confidence
   `< ALIGNMENT_CONFIDENCE_TRUST_FLOOR` (0.6) rejects MEASURE with
   `low_alignment_confidence` (guidance to re-measure at a cleaner mic
   position — never a question); otherwise it fires the SAME apply
   transaction a household's tap used to trigger
   (`jasper.web.correction_crossover_v2.handle_v2_apply`) on its own
   background thread. **Superseded 2026-07-29 (PR-T3):** stage 1 ends at
   the household's `complete_capture_set` signal, which closes the group
   and publishes a proposal; the apply is their own POST from the `review`
   screen, and stage 2 is a separate session. VERIFY's soft hold
   (`CaptureBeginDeferred`, screen `awaiting_apply`) is retained machinery
   that no shipped session reaches — stage 1 has no VERIFY index and stage
   2 is constructed `applied=True`. See gotcha #18 for the history.
5. **VERIFY** (~15 s, auto-arms on the apply-complete host event). A mono
   summed sweep through the **applied production graph** + a pilot pair,
   captured back at the mark (the apply hold's copy is where the
   household is told to walk back). VERIFY records **two independent
   claims** (R18, #1868): notch-excluded, validity-floor-clamped tracking
   error ≤ ±1.5 dB against the model, and the measured sum against the
   crossover region's derived tolerance of the candidate's own crossover
   target. Tracking remains the proof that the applied graph realized its
   prediction. The absolute claim feeds the terminal outcome without being
   collapsed into a generic failed capture. The applied graph **stays in
   force** on failure (proof-checked safe); this round adds no automatic
   restore or cross-service mutation. See "The two absolute grades" below
   for which instrument owns which question.

6. **CLOUD-VERIFY** (5 × ~16 s, one tap each). The post-apply cloud,
   walking its OWN pose set (`CLOUD_VERIFY_POSE_PROMPTS` — the design axis
   plus the four side poses) since the 2026-08-24 geometry ruling; before
   that it walked a prefix of the pre-apply table.

#### Position groups — the operational rules

- **Constants** (`crossover_v2_flow.py`, each with its rationale in
  place): `DEFAULT_CLOUD_MEASURE_POSITIONS` 9 (min 6, max 11 — it came
  down from 12 when #2291's entry baseline took a relay blob index),
  `DEFAULT_CLOUD_VERIFY_POSITIONS` 6, `GEOMETRY_RETRY_POSITIONS` 2 — the
  **Full tier's** rules; `MIN_CLOUD_MEASURE_POSITIONS` never moves for
  Express. The counts are wall-clock choices, not statistical optima —
  S0's stability data says more positions is strictly better. **Express**
  (`TIER_EXPRESS`) is a distinct named shape, not a loosened Full floor:
  `express_cloud_measure_positions()` (= 5, derived —
  `_min_positions_for_two_wide_offsets()`) and
  `EXPRESS_CLOUD_VERIFY_POSITIONS` (= 1, no cloud-verify group at all).
  `resolve_plan_shape(tier)` is the one function both `build_v2_capture_plan`
  and `build_v2_cloud_index_phase_map` resolve their (N, M) from — see "The
  capture flow" above.
- **Positions are not curves.** Both counts include the group's anchor,
  so each group combines `N − 1` / `M − 1` **summed curves**: MEASURE's
  anchor is a per-driver capture with no `summed_response` at all, and
  VERIFY's anchor does capture one but is consumed by the tracking
  verdict rather than joined to the group. At the shipped defaults the
  pre-apply cloud combines 8 curves and the post-apply cloud 5 — the 8 is
  why the default is 9 and not 8 (adjudication 3a). Compare
  those numbers, not the position counts, against
  [`linearization-campaign-2026-07.md`](linearization-campaign-2026-07.md)
  fundamental 1's "N≈8–12 gated sweeps".
- **Prompt copy** is `CLOUD_POSITION_PROMPTS` — numeric ABSOLUTE poses in
  inches and centimetres, each stating a complete target (distance, bearing,
  and height) measured from the mark, with "the microphone" as the actor.
  Two owner rulings landed on top of the S0 studio session's body-part
  register: the 2026-07-28 field session (#1805) withdrew hand-widths and
  forearms in favour of numbers, and the 2026-07-29 one (#1806) withdrew
  relative deltas in favour of absolute poses. Distances live on the row as
  `offset_cm` and the sentence is GENERATED from them, so a row cannot state
  a distance it does not carry; `wide` is likewise derived
  (`offset_cm >= WIDE_OFFSET_MIN_CM`), which is what makes narrowing a wide
  move move the group floors instead of quietly voiding the LF guarantee.
  Each row also carries a `role` (`onax` / `offax` / `xovr`), persisted with
  the position for the attribution stage. The
  ORDER is load-bearing: the PRE-apply group walks this table from the
  front, so two wide (~forearm) offsets must sit inside the first
  `MIN_CLOUD_MEASURE_POSITIONS − 1` entries or the LF half of the
  measurement quietly disappears. (The post-apply group walked it too until
  the 2026-08-24 geometry ruling gave it `CLOUD_VERIFY_POSE_PROMPTS`, whose
  own two-wide guarantee is checked by its own import-time guard.) Express
  walks the SAME table from the
  front at its own, shorter length (`express_cloud_measure_positions()`),
  derived from exactly where the table's second wide offset falls — a
  reordered table moves Express's floor with it rather than shipping a
  silently one-wide "quick tune". Pinned by test
  (`test_cloud_prompts_front_load_the_wide_offsets`).
- **Geometry-locked retake.** At group end the conductor runs
  `combine_cloud_positions` once and reads its `geometry` field for this
  retake decision (N3 review finding, 2026-07-27: this bullet's prior
  "reads ONE field" wording was stale against the "Landed as shipped"
  paragraph further down, which is the current, accurate description — the
  SAME combined result also feeds PR-4's honest-instrument pipeline, not
  just this retake gate). A `locked`
  verdict that is not `thin_evidence`-qualified rejects the group's last
  position with `cloud_geometry_locked` and a wider-spot instruction —
  at most `GEOMETRY_RETRY_POSITIONS` times, then it accepts and
  RECORDS the verdict — journal plus the durable state's `cloud` block.
  PR-4 carries the verdict (and its plain-language guidance copy) onto the
  envelope and `/state`; no household-facing surface renders it yet — PR-7
  does — so do not read "records" (or "carries") as "discloses". Bounded
  because a source-fixed comb (S0's horn
  rim) never decorrelates no matter how far the mic moves. The replaced
  take is dropped from the cloud — that is what the protocol's retake
  lever means (the same index is measured again), not a claim that
  dropping beats appending. That claim was made and withdrawn in review:
  appending a wide position actually fills a null further (−6.1 dB vs
  −7.7 dB on the reviewer's power-mean counterexample) and lowers
  `clustered_fraction` more. Replacing is what the runner permits, not
  what the estimator prefers.
- **Retry budget is per POSITION, not per group.** `_slot_of_index`
  keys attempt bookkeeping by `phase:index` inside a group and by the
  bare phase everywhere else, so CHECK/MEASURE/VERIFY bookkeeping is
  unchanged and a retake at position 2 cannot refuse position 7. Since
  the bounded-retry ruling (#2086) that budget is the planned capture
  plus three extras, pooled across every initiator — see "Retries are
  bounded per POSITION" under **Failure taxonomy & debugging**. A
  position whose extras run out is dropped from the group with its
  observed condition recorded, and the walk continues; the geometry
  retake above spends one of those extras, booked to the speaker.
- **Session budget.** `session_wall_clock_ceiling_s(plan)` scales the
  walked-away measurement-volume ceiling with plan length
  (1800 s + 120 s per capture beyond the 3-entry baseline), and each STAGE
  arms its own from its own plan since the two-stage split: Full 1800 s
  (stage 1, 3 captures) / 2160 s (stage 2, 6), Express 1800 s / 1800 s,
  hard-capped by `session_volume_plan.MAX_WALL_CLOCK_CEILING_S` = 3600 s.
  Stage 1 sits at the baseline itself now that the lateral walk is paused, so
  the longest stage in the journey is Full's stage 2. The
  restore ladder and the restore-once latch are unchanged: a walked-away
  household can never leave the speaker at measurement volume.
- **Resume is unchanged (§5.6).** A new relay session invalidates every
  capture phase including the clouds; a group interrupted mid-way
  resumes only within the same session. `V2ConductorSnapshot.
  session_phases` records which phases a session actually runs, so a
  verify-only re-arm (still 1 entry, still byte-identical on the wire)
  reaches DONE instead of waiting on a group it never had.
- **Artifacts.** Every accepted cloud position writes its WAV plus a
  metadata sidecar (prompt text, index, timestamps, QC verdict, and — since
  the 2026-08-24 geometry ruling — `position_deg` / `position_axis` /
  `mark_distance_m`) into the session bundle via `bind_position_retention`; the closing geometry
  verdict lands in the durable state's `cloud` block. Retention rose
  256 MiB → 1 GiB for this; the publish-time free-space floor
  (`MIN_FREE_SPACE_AFTER_PUBLISH_BYTES`) was deliberately *decoupled*
  and frozen at 256 MiB — see its comment.
- **PR-4's seam** is `combine_cloud_positions(positions)` →
  `CombinedResponse | None`. `cloud_position_capture` is the
  per-position assembly underneath and does not change. The
  gated-IR reconstruction those functions perform is validated against
  the S0 corpus by
  [`tests/test_crossover_v2_cloud_geometry_corpus.py`](../../tests/test_crossover_v2_cloud_geometry_corpus.py)
  (`JTS_FLAT_LIN_S0`-gated), not asserted.

  **Landed as shipped (2026-07-26).** `_close_cloud_group` calls
  `combine_cloud_positions` exactly ONCE per group close and derives BOTH
  the retry-gating verdict (`_geometry_verdict_from_combined`, a pure
  function of the already-combined result) and the honest-instrument
  pipeline (`assemble_cloud_group_result`) from that single object — the
  contract this section's opening paragraph states. An earlier revision of
  this wiring called `combine_cloud_positions` a SECOND time from the
  pipeline step (justified then as "byte-for-byte deterministic, so it
  agrees with the retry-gating call anyway"); round-1 review measured the
  actual cost — seconds-per-combine, 3-6 s across runs/hosts on the S0
  ten-position corpus (interpreter-bound `smooth_fractional_octave`, worse
  on a Pi 5) — and reversed it: real operator seconds are not worth
  spending on a claim that was true but unnecessary to rely on. With
  `GEOMETRY_RETRY_POSITIONS = 2` allowing up to 3 close attempts per group
  (2 retries + the accepting close), the pre-fix worst case was 3 × 2 = 6
  combines (round-2 review, 2026-07-27, correcting an earlier "up to 4x"
  claim, and restating the earlier "5.6-6.2 s" point figure as a regime —
  it did not reproduce across hosts/runs).
  `cloud_geometry_verdict(positions)` (PR-3b's original seam) is now a
  positions-only convenience wrapper the conductor itself does not call
  (kept for `test_crossover_v2_cloud_geometry_corpus.py` and any other
  direct caller); `test_crossover_v2_conductor.py`'s `_lock` monkeypatch was
  updated to patch `_geometry_verdict_from_combined` instead (the seam the
  conductor actually calls now), with the real (unmocked) combine still
  running underneath it. The wiring contract itself — the mask/geometry/
  registry consumed TOGETHER — is `assemble_cloud_group_result`, issue
  #1742 item 4's single result-assembly function, called once per closed
  group and never read from in pieces.

**Flatness — a SIBLING claim, report-only, and since the flat-linearization
plan's PR-5 it comes from the CLOUD, not from the VERIFY capture.** It
answers a different question ("is the speaker flat") than integration-verify
("did the applied crossover realize its own predicted summation") — the
design doc's finding was that integration-verify alone cannot see an
uncompensated tweeter rolloff, because the prediction shares that rolloff.

*History, because the shape changed twice.* #1668 PR-D computed it per
VERIFY capture (`ProgramAnalysis.flatness_tracking`, that capture's own grid
from its own validity floor to `FLATNESS_VERIFY_HI_HZ` = 16 kHz, graded
against its own band mean, tolerance `FLATNESS_VERIFY_TOLERANCE_DB` = 3.0,
PROVISIONAL and never bench-derived). The 2026-07-24 gauge fix persisted it
to `verify.flatness` and rendered it on the verify_fail and "done" screens.
**PR-5 retired that whole construction** — function, both constants, the
`ProgramAnalysis` field, the `PhaseVerdict` relay, the conductor's
`flatness_evidence` stash, the `verify.flatness` state key, and the
`flatness_*` fields on `event=correction.crossover_v2_verify_diag`. A single
mic position cannot answer "is the speaker flat", and having it answer
anyway produced two disagreeing numbers per session (the plan's
MEASURE-vs-VERIFY ledger-discrepancy class).

*Current shape.* One construction: `combine_positions`' power-mean spec
curve → the merged honesty mask (screen ∪ null registry) plus the group's
gate-validity clamp → `flat_spec.evaluate_flat_spec` →
`flat_spec.spec_flatness_gauge`, all inside
`assemble_cloud_group_result`, which publishes it as the pipeline's
`flatness` key. Every surface COPIES that dict: `_compact_cloud_status`
(`/state` + the envelope's `cloud` block), the doctor's
`check_crossover_v2_cloud_pipeline` detail, and
`crossover_envelope_v2._flatness_details_lines`, which renders it into
`expert_details` on the review, closing, verify, verify_fail and
"done"/RESULT screens. It reads `PHASE_CLOUD_VERIFY` **whenever a post-apply
cloud exists**, and falls back to `PHASE_CLOUD_MEASURE` under an explicit
`"Measured before tuning: …"` lead when none does — which is Express on every
screen (M = 1 never produces one) and every tier at stage 1 (issue #1965).
`cloud_measure` is the uncorrected pre-apply baseline, so it is never rendered
bare, which would report a corrected speaker as bad forever.
Logged on every close of the group (including a retake's re-close, issue
#1872) as `event=correction.crossover_v2_cloud_spec` — always describing
whichever cloud is currently retained.
The tolerances are the spec table's own per-band values
(`flat_spec.SPEC_BANDS`) rather than one provisional constant. Contract test:
[`tests/test_flat_spec_ssot.py`](../../tests/test_flat_spec_ssot.py).

**The worst-band pointer is frame-dependent, and travels with a reading that
is not** (issue #1857). Every graded number above is a distance from
`reference_db`, a power mean pooled across `REFERENCE_BAND_HZ`, so a band
that is uniformly off drags that zero and inflates the others — on the
2026-07-29 corpus session a woofer flat to ±0.1 dB read `+4.84 dB @
1339.6 Hz` and got named as the worst band while the ~5 dB dark tweeter's own
bands passed. WHICH anchor the frame should use is an open owner decision
(Q-E, [`docs/attribution-stage-plan.md`](attribution-stage-plan.md) §9) and
re-anchoring would move verdicts, so it has not been re-anchored. What ships
instead is an attribution split that no anchor choice can move:
`BandResult.level_deviation_db` (where the whole band sits) and
`max_ripple_db`/`max_ripple_hz` (what the curve does relative to *that band's
own* level), reduced by `flat_spec.spec_band_tilt` into the largest level
step between two graded bands. It rides `SpecFlatness.tilt` plus
`max_band_level_deviation_db`/`max_band_ripple_db`, renders under the pointer
via `crossover_envelope_v2._attribution_lines`, and logs as `flatness_tilt`.
No verdict moved: pinned shape-for-shape against pre-change numbers by
[`tests/test_flat_spec_attribution.py`](../../tests/test_flat_spec_attribution.py).

**No longer report-only** (linearization-integrity PR-L4). It still does not
gate `_verify_verdict`'s accepted/code logic — that stays a tracking judgement
— but the spec verdict now has three consumers that act on it:

* `CrossoverV2Conductor._assert_accountable` grades the RAW and LINEARIZED
  predicted sums through the same `evaluate_flat_spec` and banks the verdict
  when the correction does not materially better its own model
  (`accountability.LEDGER_NOT_AN_IMPROVEMENT`). It REFUSED on that verdict
  until the nanny burn-down retired the refusal — see the retired row in the
  refusal table below;
* `crossover_envelope_v2` reads a failing spatial grade into the done screen's
  PRIMARY copy and swaps the "Verified." badge for one that names which
  instrument passed — previously the verdict reached only a line inside the
  collapsed disclosure. Since R19 it reads that verdict off `post_apply_grade`
  rather than off the cloud entry's own `overall_passed` (see below), and since
  #2738 a failing grade CAPS the terminal result code too: the one `ok` code,
  `verified_target`, yields its badge and its copy to the failing grade, while
  the three `warn` codes keep both because none of them claims verified;
* `crossover_v2_status_block` folds it into the new `post_apply_grade` key
  (see below).

**`/state.crossover_v2.post_apply_grade`** (PR-L4 item 4) answers "was the
correction now on the speaker ever checked after it landed?" — `state` is one
of `not_applied` / `graded` (a walked post-apply position group) /
`mark_verified` (VERIFY passed at the mark; express's whole grade) /
`inconclusive` / `failed` / `unverified`. Read it, do not re-derive it:
`jasper-doctor`'s `check_crossover_v2_applied_is_graded` is its second consumer
and warns on an applied profile that was never graded — the silence
`check_crossover_v2_cloud_pipeline` structurally cannot see, because that check
gates on a FAILING `PHASE_CLOUD_VERIFY` verdict and a missing one renders as no
phase at all.

That doctor line also discloses `post_apply_grade.outcome` as `result=<code>`
beside `capture verify=` (the four codes are tabled under "Terminal grading has
one owner" below) — a disclosure and never a gate, because the check grades the
*checking* rather than the result: a `keep_previous` sitting behind a clean,
complete grade stays `ok` and simply names the code the done screen's badge is
reading, and a pre-R18 state that recorded no result evidence prints no code at
all.

**A failed mark-VERIFY caps `state` at `failed` whatever the spatial group
says** (#2464, ruled 2026-08-19). A closed group used to be tested first, so it
masked the `failed` and `inconclusive` answers entirely and a re-verify that
failed against a carried-forward passing group read as `graded`. The failure
fact is the union of two instruments that cannot see each other:
`verify.outcome` (capture and tracking health) and the `verify.claims` record's
`integration` and `absolute` verdicts (a VERIFY grades no others — its one
summed sweep leaves both per-branch claims structurally `not_evaluated`). So a
crossover-region claim that missed its tolerance caps the badge on a capture
whose own outcome is `pass`, and `verify_outcome` keeps reporting that `pass`.
A state file with no claims block is graded on its outcome alone.
Per #2160's rider (ratified 2026-08-17) the cap co-locates nothing: `spatial`,
`post_apply_spec_passed` and `verify_outcome` each still carry their own
instrument's answer.

**Three more keys carry what `state` structurally cannot** (R19; #2098, #2160
— plan §7's scope/completeness fact). `state` answers *was it checked*, and
both `graded` and `mark_verified` are honest answers to that in cases that are
not equivalent, so `graded` is **no longer** a boolean a surface may key "all
clear" on by itself:

* `scope` — how wide the evidence behind `state` actually reached: `none` /
  `mark` / `spatial`. Delivered width, never the tier's promise.
* `spatial` — the post-apply spatial gauge's own state: `absent` / `passed` /
  `failed` / `unmeasurable`. `overall_passed` is a bool and cannot separate a
  graded miss from a spectrum no band survived to grade (`SpecFlatness.passed`
  is `False` for both); `unmeasurable` is claimed only on `flatness.evaluable`
  being explicitly `False`, never on the gauge merely being absent.
  `spatial_worst_db` / `spatial_worst_hz` ride a `failed` grade, copied
  verbatim from that same gauge.
* `complete` — the producer's own comparison of `scope` against what the tier
  promised: Full needs `spatial`, Express's promise IS the mark. An
  unrecognised tier is judged on delivery alone rather than manufacturing a
  warning about a promise this build never read.

A failed spatial grade **grades and discloses; it never gates** (#2160's
ruling): the session completes, the applied tune stays, and doctor/`/state`/
the wizard each say so. On jts3 2026-08-07 the doctor printed `applied and
graded (state=graded, verify=pass)` one row under a cloud line reading
`spec=fail worst=-4.63dB excluded_intervals=4 geometry_locked=False` — the
defect these keys close.

Since #2291 Phase 3c that spatial report is also the SPEC verdict's input on
the round receipt (see "The round, graded" below), so the failure is recorded
as one of four graded answers rather than as disclosure alone. It still does
not gate: spec reads "any" in every row of the adoption table, because a first
pass may honestly be improved and out of spec — and since #2537 there is a
second reason it must not (the trusted-floor intersection, described below).
What #2537 added is that each failing band rides the receipt as a next-round
target, which is disclosure and not a gate.

**`/state.crossover_v2.prediction`** (two-stage commission work order D4,
issue #1806) carries the PREDICTED post-apply response and the spec verdict it
was graded with: `curve` (decimated through the same
`_decimate_curve_for_chart` owner and the same 256-point ceiling the
`cloud_chart` curves use, so all three curves in one chart are at one density),
plus `spec_bands` / `overall_passed` / `reference_db` in the compact `cloud`
block's own vocabulary. Both halves already existed and neither reached a
surface — the curve was persisted at `MAX_PERSISTED_SUM_POINTS`, and
`_assert_accountable` graded the full-resolution tuple and threw the report
away.

**The verdict is graded ONCE, at full resolution, and stored.** What survives
to `verify_priors.predicted_sum` is a 512-point reduction (issue #1858: a
block average through the same owner `spec_report_for_predicted_sum` itself
uses, `spatial_combine.decimate_curve_to_analysis_grid`, at a smaller
`max_bins` — a raw stride before that fix, which aliased below ~500 Hz);
re-grading it is a *different* instrument from the one the accountability
veto refused on, and the two disagree (on the shipped conductor fixture,
180/617/823 graded bins against 45/154/206). So `_assert_accountable` stashes
the report it computed, `verify_priors.predicted_spec` persists it verbatim,
and `prepare_v2_verify` rehydrates it so a re-arm's own persist cannot blank
it. The persisted curve is a drawing; the persisted report is the instrument.
`None` throughout means
ungradeable — **never a pass** — and an ungradeable prediction emits
`event=correction.crossover_v2_prediction_ungradeable` with `why=no_prediction`
(nothing was predicted) or `why=evaluator_refused` (the evaluator would not
grade the curve). The `review` screen below renders it.

**The stage bridge.** Stage 1 and stage 2 are two relay sessions with two
conductors and nothing shared in memory, so `verify_priors` — written by
`persist_conductor_state`, read by `prepare_v2_verify` — is the *only* channel
between them. It carries eight keys: `predicted_sum`, `predicted_spec`,
`gate_window_ms`, `pilot_transfer_reference`, (since #2291 Phase 3a)
`commanded_delta`, the delta probe's commanded axis, (since #2291 Phase 3c)
`entry_baseline`, the summed at-the-mark capture stage 1 takes immediately
before apply so stage 2 has a measured "before" to grade its "after" against,
(since #2392) `proposal_fingerprint`, the identity of the `InterventionProposal`
stage 1 committed and the round receipt names, and (since #2522)
`verify_measured`, the MEASURED verify curve pair — the third side of the
comparison whose other two sides this bridge already carried, retained so a
disputed probe verdict can be re-graded offline instead of needing another
hardware run. `verify_measured` is the one key written by the stage that
VERIFIES rather than the stage that fits, so a stage-1 persist writes `None`
there. Before Phase 3a the `commanded_delta`
curve was produced in stage 1 and consumed in stage 2 with no key to travel in,
so every shipped stage 2 reported the probe `unavailable` — grading a correction
with the shortfall-vs-model-error discriminator switched off. It is reduced to
the same 512-point ceiling on the same block grid as `predicted_sum`, but
averaged **in dB rather than in linear power**: it is a difference of two dB
predictions, and over a block the arithmetic mean is that difference's
unbiased estimator where the power mean is biased upward by Jensen — by an
amount that grows with the within-block spread and vanishes across a flat
block. Measured through the production response owner (200 cascades,
1,024–8,192-bin grids): worst block disagreement **1.60 dB**, and **5 of
100,762** persisted bins change side of the 0.5 dB commanded floor. Rare, but
it does reach band membership rather than a third decimal. `verify_measured`
rides the same ceiling and the same dB block average, and there the linearity is
what makes the record re-gradable: the difference of the two decimated curves is
exactly the decimated difference, which is the quantity the probe grades. It
costs about **36 KB** in the state file as written (512 points × 3 arrays, at
`json.dumps(indent=2)`), against ~25 KB for `commanded_delta` beside it.

Which seams each stage binds is declared once, in `STAGE_MEASURE_CAPABILITIES` /
`STAGE_VERIFY_CAPABILITIES` (in `crossover_v2/journey.py` since #2291 Phase 4,
re-exported from the host), and built by `bind_v2_stage_seams`; the two
preparers each open their stage through one `open_stage(...)` call and pass the
resulting `StageOpening` straight to the binder. Only two seams differ — the findings publisher
(stage 1, because only the MEASURE candidate's gate banks one) and `rollback`
(stage 2, because only it reaches the delta probe). Each stage open emits
`event=correction.crossover_v2_stage_capabilities` naming what it provides and
requires, plus
`event=correction.crossover_v2_stage_capability_unavailable` when a required
prior did not cross. `requires` is observability, never a gate: a state file
written before Phase 3a still opens stage 2 and still verifies, it just cannot
run the probe, and the journal says so instead of leaving an `unavailable`
verdict unexplained.

#### The round, graded

Since #2291 Phase 3c a correction round answers the question it exists for —
*did the speaker get better* — and acts on the answer. Before it, a round could
report that the applied graph tracked its model and stop there; the 2026-08-10
jts3 round did exactly that while the post-apply cloud failed all three spec
bands.

**The measured "before".** Stage 1's last capture is `PHASE_ENTRY_BASELINE`:
one summed sweep at the design-axis mark, taken immediately before apply. Its
comparability is structural, not asserted — membership in `SUMMED_SWEEP_PHASES`
routes it to the same `_verify_program` object the post-apply VERIFY replays,
so both stamp one `program_id`, and `REFERENCE_MARK_DESIGN_AXIS` (one owner) is
the second identity. It crosses the bridge as `verify_priors.entry_baseline`,
the sixth key, and — like `predicted_sum` and `commanded_delta` — it needs **no
carry-forward**, because it is seeded into the same field its own capture
writes, so a stage-2 persist re-writes the record its conductor was constructed
with. (A carry-forward branch was written here first and deleted:
mutation-verified as unreachable, and keeping it would have weakened the pin
that actually matters — a MEASURING session replaces the previous round's
"before", so this round's "after" is never differenced against a stale one.)

**Four independent verdicts**, composed once by
`crossover_v2/round_evidence.evaluate_round` and never by a host:

| verdict | asks |
|---|---|
| capture | was this capture usable at all? |
| realization | did the graph do what its own filters commanded? |
| benefit | is the speaker measurably better than the entry baseline? |
| spec | is the result inside the target envelope? |

An unusable capture short-circuits rather than being graded and then
overwritten, so the journal cannot show a benefit no usable capture supports.

**Adoption is a table over three axes (#2537).** The four verdicts above are
*evidence*; the three axes are the questions a decision is actually made on, and
each has its own evaluator in `verification.py`:

| axis | asks | DECIDES on | discloses |
|---|---|---|---|
| `evaluate_evidence_trust` | did we measure the state we applied? | capture validity, realization availability | — |
| `evaluate_applied_safety` | is that state safe to leave on? | the delta probe's directional findings, capture integrity | which instruments looked |
| `evaluate_round_quality` | how good is it, and what is left to fix? | **`(realization, benefit)` only** | spec, each failing band, the probe's reason |

**The quality axis's STATUS is #2291's own table, unchanged in what it reads.**
Same two statuses, same nine cells, same nine causes. What #2537 changed is what
a non-keep cell resolves to, not what decides it.

**Spec is still deliberately not a decision factor**, and there are now two
independent reasons, both of which have to hold. It is an *outcome, not a proxy
for benefit* — every row reads "any" for spec, and the permutation pin is
load-bearing. AND the spec verdicts available today are computed over the raw
250 Hz-2 kHz band with **no intersection against the session's own trusted
floor** (357.1 Hz on a 7 ms gate), so a series keyed on them would rank rounds
partly on sub-trusted-floor evidence the same session's delta probe refuses to
grade — a term the E4 sweep measured moving ~2 dB with gate length alone. That
intersection is a **separate filed fix and must land before any axis is allowed
to decide on a spec verdict.** Spec and the per-band deviations ride the receipt
as next-round TARGETS, which costs nothing and inherits none of it.

`decide_adoption` selects one of five rows. Every row id is on the decision, the
`…_round_graded` journal line, and the receipt, so a driver chaining rounds
branches on a symbol rather than parsing a reason:

| row | condition | outcome |
|---|---|---|
| `row1_trusted_safe_passed` | nothing outstanding | `keep` |
| `row2_trusted_safe_missed` | something outstanding | `keep_for_iteration` |
| `row3_unsafe` | a hazard was measured | `restore` |
| `row4_untrusted_evidence` | the applied state was not measured | `restore` |
| `row5_trusted_safe_regressed` | the entry baseline measured flatter | `restore` |

(`row0_restore_failed` is outside the table: a restore was attempted and did not
complete, which is not a decision about the evidence at all.)

**What each row is for, in the owner's own terms** (ruling, 2026-08-15: *we're
looking for the least bad MEASURED tune. reverting to an unknown measured state
seems dumb… the first application is not the end point, it is just the start*):

* **`keep_for_iteration` keeps the graph on the speaker** and records the misses
  as the next round's targets — including each failing spec band by its own
  edges and measured deviation. A round that measured a real state and did not
  reach target has produced the best measured tune available; reverting it
  trades that for a state nobody measured.
* **`row4` restores anything unmeasured**, because "least bad MEASURED tune"
  cannot include a state nobody measured. `unproven_boost_failed_closed`
  survives *only* here — with trusted evidence a boost is judged
  realized-vs-declared on the safety axis instead.
* **`row5` still restores a measured regression.** The ruling turns on *unknown*
  previous states; a regression is the one case where the previous state's own
  measurement is the evidence.

**Safety is the only axis that pulls a measured graph off, and direction is its
discriminator.** A −2.3 dB uncommanded level shift is `row2` (the household
loses some output; the next round learns something); a +2.3 dB one is `row3`
(energy nobody asked for). Same magnitude, opposite answer. The three hazards
are: a boost realized above the probe's tolerance
(`delta_probe.boost_overshoot`, the one directional exceedance rule in that
module), an uncommanded shift measured LOUDER than declared, and a clipped
capture. A band-scoped level claim (#2533) narrows *where* a level was
measured, never *whether* it happened, so a positive band-scoped shift is still
a hard stop.

**All three are measurements of the SPEAKER, and the first one had to be
repaired to become one (series-2 D1).** Until 2026-08-18 it graded
`realized − commanded`, in which the commanded term cancels identically — so the
quantity was `(measured − predicted) − expected_offset`, the acoustic model's
own error. On 2026-08-17 that restored the flattest tune this program has
measured, for a +3.9 dB model error at 1384 Hz that both rounds of that series
shared (corr 0.954, 0.350 dB rms apart), in a band the applied graph declares a
3.67 dB CUT in, on a round whose probe verdict was `matched` and which measured
2.42 dB QUIETER. The finding is now the **anchored** excess —
`(measured_post − measured_pre) − expected_offset − commanded`, differenced
against the entry capture per bin — so a standing model error cancels and
delivered energy does not. The old reason string
`boost_realized_above_declared_bound` named a "declared bound" that was the
probe's own 1.5 dB measurement tolerance; it is now
`boost_realized_above_probe_tolerance`. Receipts banked before this carry the
old string, and they were reporting the old quantity, so the two spellings mark
two instruments.

**No anchor, no finding — and the round SAYS so.** Without a pre-apply capture
there is only the model's error, so the two directional findings are not made.
That is not an edge case: a **first-ever round reaches it by construction** (no
prior applied profile ⇒ no nameable previous graph ⇒ `state_axis_only`), as does
every committed alternative-Fc round and every capture with too few quiet bins
to anchor. So the safety axis has two SAFE reasons, not one:
`no_unsafe_finding` (the realized-energy check looked, found nothing) and
`no_unsafe_finding_realized_energy_unmeasured` (it could not look). The status
and the adoption row are identical — refusing on an absent measurement would
revert every first round — and what differs is what the receipt and the journal
claim was checked. #1868's rule on this axis: *"we do not know" must have
somewhere to live rather than defaulting to the success value.*

It is on five surfaces: the axis reason (on the round receipt),
`safety_anchored` in the safety evidence and on the map, `safety_anchored=` on
`event=correction.crossover_v2_delta_probe`, `safety_reason=` on
`event=correction.crossover_v2_round_graded`, and `safety_anchored` in the
durable `verify.delta_probe` summary. That last one is a **forensic state key**
— it is where `/state`, the doctor and the done screen would read it from, and
**no renderer reads it today**; it is there so a live surface can, which a
write-once receipt cannot support.
`event=correction.crossover_v2_delta_probe_no_entry_anchor` names which arm
produced it (`no_entry_baseline` / `incomparable_program` /
`incomparable_reference_mark` / `unusable_record`), at WARNING — every one of
those three is exceptional, including the first: a **first-ever round never
reaches that arm at all**, because it has no commanded axis and takes the
`state_axis_only` branch without calling `_entry_delta_db`.

What still holds with no anchor: on an ordinary round the **level** rule does —
`residual_offset_db` is gated on having quiet bins, not on having an anchor. On
the `safety_only` path it does not (`residual_offset_db` is `None` there). The
clipped check always does, and underneath all three sits the graph's own
electrical bound — a deterministic biquad chain whose peak cost is computed and
pre-paid under `devices.volume_limit = 0.0`.

**The anchor must be COMPARABLE, and that is checked.** An anchor is a
subtraction, so a curve measured through another program cancels a real finding
as readily as a phantom. `crossover_v2_flow._entry_delta_db` refuses a baseline
whose `program_id` disagrees with this round's VERIFY program — the same two
identity fields `round_evidence` uses and `evaluate_benefit` refuses on, asked
here rather than re-derived. Unknown on either side is "nothing known" and does
not refuse.

**What the anchored rule can and cannot see.** It catches a hazard the moment it
APPEARS: a band this apply left alone whose output rises across the apply reads
its full size (#2614's case). It does **not** see one already present in BOTH
captures — a band running hot since an earlier round subtracts to zero here,
identically, because "nothing changed" is what the two captures say. That is the
price of an instrument that cannot be fooled by the model. The onset is where a
standing hazard is catchable, and it is caught there.

**The model's departure is still measured, and lands on QUALITY.**
`DeltaProbeMap.model_departure_over_tolerance` / `max_signed_error_db` is the
unanchored reading — exactly what `realized_louder_than_commanded` carried
before D1 — and `evaluate_round_quality` appends it as a next-round target
(`model_departure:<dB>@<Hz>`). It is a real defect, and the blend region is
where this model is known blind (#2600); it is not a hazard, and it moves no
status.

**What "safe" does not claim.** `SAFETY_NO_FINDING` means no instrument that ran
reported a hazard — an absent or ungraded probe reports no finding rather than
one, matching `DELTA_PROBE_ROLLBACK_VERDICTS`'s own rule that an absent
measurement is not evidence. Since D1 the *reason* carries half of that
distinction on its own: `SAFETY_NO_FINDING_UNMEASURED` is the same SAFE status
with the realized-energy check unrun. The verdict's evidence carries
`probe_graded`, `probe_shape_graded` and `safety_anchored` for the rest, so a
reader can tell "safe because nothing was found" from "safe because nothing
looked."

**The same direction rule reaches the SHAPE axis (#2559).** The delta probe's
own seam-bound rollback preempts this table — a seam refusal ends the session
before `decide_adoption` runs at all — so on 2026-08-15 a `model_error` whose
realized deviation pointed entirely quieter (a −3.32 dB dip at 1330 Hz, nothing
realized louder than commanded anywhere, tracking passed, 2.399 dB of measured
improvement) came off the speaker without the table ever seeing it. Owner ruling
the same day: quieter-direction `model_error` defers to the table.
`delta_probe.seam_rollback_deferral` owns that one class; everything else the
seam restored on, it still restores on — `level_dependent_shortfall`,
`spatially_costly`, any graded bin realized louder than commanded past
tolerance (unstructured, so one bin withholds the deferral),
`boost_over_declared_bound`, and every ungradeable map, which never reached a
seam rollback in the first place. The measurement behind it is
`DeltaProbeMap.realized_louder_than_commanded`, taken on the raw curves for
`boost_overshoot`'s reason: this asks how much energy reached the driver, not
whether the shape is right. It is measured over the probe's SAFETY bins since
#2614 — this apply's graded changes UNION the applied graph's own declared
transfer — for the same reason, and over the ANCHORED excess since series-2 D1:
the fence withholds lenience on a positive bin, so a fence fed by model error
withheld it wrongly.

**An unanchored map does NOT simply defer**, and the fence has a third guard
for it. A fence's polarity is the opposite of a finding's: "no anchor, no claim"
is right for a hazard, and applied here it would make absence *grant* the
lenience — a round measured +20 dB louder taking `row2_trusted_safe_missed`
with `model_error_quieter_than_commanded` banked on it. So an unanchored map
falls back to `model_departure_over_tolerance`, which is exactly what the fence
read before D1: an unanchored louder map does not defer, and an unanchored
quieter-only one still gets #2559's lenience. A deferral is never silent —
it journals `event=correction.crossover_v2_delta_probe_seam_deferred` (WARNING)
and rides the safety axis's evidence as `seam_deferred`, so the receipt records
the restore that did **not** happen.

**Ordering:** a failed restore outranks everything; then safety; then trust;
then quality. Safety before trust because both restore, so the order only
decides which name the receipt carries — and a clipped capture is both, where
naming the hazard beats naming the absence.

The conductor grades once per session — at the end of `_consume_verify` on a
tier that walks no post-apply cloud, and at the close of the post-apply cloud
group on one that does. **A round never overwrites an existing refusal**: a
capture that already failed keeps its own, more specific code, and the round
contributes its verdicts and its receipt without acting. The acting half is
for the round that would otherwise have been reported as a success.

**Exactly-once restore.** Three sites can reach the rollback seam in one Full
session. `handle_v2_restore` is not idempotent — a successful restore sets
`applied = False`, so a second call refuses and the household would be told the
correction is still applied when it is not. `bind_delta_probe_rollback`
therefore attempts the restore once and hands every later caller the FIRST
outcome verbatim (`event=correction.crossover_v2_delta_probe_restore_repeat`).

**The restore's TARGET is the round's own snapshot (#2537).** "The previous
sound" used to have two owners that could silently diverge — the global
applied-baseline-profile record, which apply/restore reads, and the saved sound
intent, which `jasper-sound reconcile-current-dsp` renders. On 2026-08-15 an
operator reconciled the running config at 01:00; at 07:30 a round's restore
faithfully put back the profile record's answer, which was run 2's candidate
from six and a half hours earlier. The mechanics were right; the target was not.

So `observe_apply_success` now stashes a `round_anchor` beside
`pre_apply_profile`, in the same durable write and re-stamped by every apply:
`{displaced: {config_path, sha256}, applied: {config_path, sha256}}`, both read
off the apply transaction's own result (`DspApplyState.prior_config_path` is the
running graph at apply moment, by construction). Round N+1's `displaced` is
therefore exactly round N's `applied`, which is the chain a chained series
needs.

`rollback_anchor_refusal` has **five** preconditions. Two of them are anchor
checks, and they ask about two different moments:

| code | asks | when it is answerable |
|---|---|---|
| `ANCHOR_STASH_NOT_DISPLACED` (#2559) | was the stash right when it was TAKEN — does `pre_apply_profile`'s config name the graph this round displaced? | static; both facts came from the same apply |
| `ANCHOR_RUNNING_CONFIG_DIVERGED` (#2537) | is it still right NOW — is the graph about to be REPLACED still the one this round applied? | needs a live CamillaDSP reading |

Both compare by path **and** by digest, and both **refuse** rather than
re-anchoring — the flow knows the restore is aimed at the wrong sound and cannot
know what a household wants done about that, and stomping a config an operator
deliberately reconciled to is the one outcome nobody asked for. Journal:
`event=correction.crossover_v2_restore_stash_not_displaced` and
`…_restore_running_config_diverged` (both ERROR); the first names both paths,
because the whole finding is that they disagree.

**Why the second was not enough.** On 2026-08-15 at 14:47 the same speaker
resurrected the same stale candidate a third time, ninety-seven seconds after an
apply. The divergence check passed, correctly — nothing had moved the running
graph in ninety-seven seconds. The staleness was already INSIDE the stash,
because `pre_apply_profile` is frozen from the applied-baseline-profile record
and that record had been stale since 00:34.

The live check is a parameter because it needs a live reading: `handle_v2_restore`
takes it from CamillaDSP at the moment of action, and the `rollback_available`
capability probe deliberately does not (a camilla hiccup would flip a whole
round to `recovery_required`). The two can disagree, and the disagreement is
bounded and loud: a divergence found at the endpoint returns "not restored",
which re-grades the round into `recovery_required`. The stash check is static,
so the probe DOES answer it and a round learns before it decides that it has no
restorable anchor. **Every way either question cannot be answered — no live
reading, no anchor, a state written before #2537 — reports "cannot compare",
never "it moved".**

A stale applied-profile record therefore needs **no operator repair for
correctness**: it can no longer aim a restore, and the next apply overwrites it
as it always did.

**The apply says when it inherited one (#2859).** The refusal above is right,
but it fires at a restore — hours after the divergence was created, which is
how four field occurrences each cost a reconstruction from the journal.
`observe_apply_success` now runs the same static predicate on the stash it just
wrote, against the anchor it wrote beside it, and logs
`event=correction.crossover_v2_apply_inherited_stale_anchor` (WARNING, naming
both paths) when they disagree. It **refuses nothing and re-anchors nothing** —
the apply is legitimate and the restore door already holds the line; what
changed is that the moment is on the record. Re-pointing the stash at what the
apply actually displaced is still open, and is the design question the
2026-08-15 resurrection bounds.

**The no-anchor sentence names no cause (#2859).** `rollback_anchor_available`
is one bool over the FOUR static preconditions the capability probe can
answer — it calls `rollback_anchor_refusal` with no `running_config_path`,
so the live fifth check cannot fire there — and its household sentence used
to end
"this was its first measured crossover" — true of `ANCHOR_NO_PRE_APPLY_PROFILE`
and of no other code on the list. On 2026-08-22 a jts3 speaker with an intact
stash and an intact displaced record hit `ANCHOR_STASH_NOT_DISPLACED` and was
told it had never been corrected, sending the operator after the wrong
diagnosis. Both surfaces — `refusal_copy.correction_rollback_failed_message`
and `crossover_envelope_v2._DURABLE_STATE_FACTS_NO_ANCHOR` — now state the
remedy without the cause. A third state that distinguishes absent from
stale-divergent is a capability-probe change, not a copy one, and is not made
here.

**Read-side provenance.** `baseline_profile.applied_profile_displacement`
compares the applied record's `config.path` against the running CamillaDSP
statefile. Where that record is read as authority — today
`_active_graph_fingerprint`, the receipt's `applied_graph_fingerprint` — a
displaced record answers `""` (the coordinator's `unknown` word) rather than
naming a graph the speaker is not playing, and logs
`event=correction.crossover_v2_applied_profile_displaced`. **This adds a reader,
never a second writer:** `reconcile-current-dsp` stays entirely ignorant of the
active-speaker profile system, which is the separation that makes it safe to run
at deploy time, and the record's only writer is still the apply path. That is
#2537's option (b); option (a) would have traded the separation away.

**The receipt** is one immutable record per round at
`crossover_v2/<relay_session_id>/round_receipt.json` in the evidence bundle,
written through `publish_json_artifact` + reopen-and-compare (the R21 pattern):
write-once, canonical-JSON, fsync'd file and parent, tamper-checked, beside the
evidence its own identities name. `round_id` is the stage-2 relay session id —
one graded post-apply session is one round, and a recovery re-verify writes its
own rather than amending this one. Its identity lands in `state.round_receipt`
so the next round resolves it without scanning bundles. Writing is fail-soft —
a receipt that could not be written is never a lost verdict — but it is logged
at **ERROR**, not WARN
(`event=correction.crossover_v2_round_receipt_failed`).

**EVERY round banks one, including a probe rollback.** The three delta-probe
rollback classes used to restore from a seam of their own
(`_delta_probe_refusal`) that ran BEFORE `run_round` and ended the session on
its own code — so a rollback round wrote no receipt at all, which is the bug
the ethos's fifth principle names by its date ("no round receipt was written on
the failed verify, leaving that round's realization only in journal events" —
that 2026-08-16 incident record stays in `audio-commissioning-roadmap.md` as
archaeology; the banking rule it forced into writing is the guiding principle in
[`measurement-loop-doctrine.md`](../measurement-loop-doctrine.md)).
That seam is deleted. The probe reports, `evaluate_round_quality` escalates a
non-deferred rollback class to `REGRESSED`, the adoption table restores through
its one restore owner, and the receipt records what the restore did. **The
restore SET is unchanged** — same three classes, same #2559 deferral for the
quieter-only `model_error` — and each keeps its own household sentence, because
the cause carries the class (`delta_probe_rollback_class:<verdict>`) and
`round_restore_reason` reads it back through
`DELTA_PROBE_REASON_BY_VERDICT`. Narrowing the set (§2.2's re-audit: gating
`level_dependent_shortfall` on band-resolved realization, `model_error` on
measured-worse) is deliberately NOT part of that move.

**Since #2609 the IDENTITY survives what the ARTIFACT does not.** It is
assembled from the round's own evaluation and returned on every path,
including an unbound or raising publish seam; only the two fingerprint fields
depend on the artifact, and they are `""` when none was banked. That split is
load-bearing because the identity is also the series' memory — the ordinal,
the objectives, and the trusted floor `series_position_from_state` reads back —
so a durably broken evidence store used to pin every round at 1 and silently
disable both the cap and the plateau stop. `coordinator.py`'s own
handler records why: this is the exact event that would have fired on every
shipped round for a whole phase while nobody looked, and a fail-soft path whose
only trace is a WARNING is one nobody reads. Its sibling, the no-anchor recovery
path, is already ERROR.

**What `proposal_fingerprint` identifies, and how to tell (#2392).** Since
#2392 the field carries `InterventionProposal.fingerprint` — the proposal the
round *made* — rather than the candidate that happened to be committed. The
proposal is assembled at the one commit seam
(`crossover_v2/proposal.py:plan_intervention_proposal`) and its fingerprint
crosses to the grading stage through `verify_priors.proposal_fingerprint`,
because the stage that writes the receipt builds a fresh session and holds no
candidate. **Old and new receipts are not distinguishable by value** — both a
candidate fingerprint and a proposal fingerprint are 64-hex SHA-256 from
`json_fingerprint` — so the receipt says which, in
`proposal_fingerprint_kind`, and the three states are total: **key absent** =
written before #2392, therefore a candidate; `candidate` = written after, but
the session had no proposal (a pre-#2392 stage 1, or a refused assembly);
`intervention_proposal` = the proposal's own. The applied candidate stays on
the record either way, at `evidence_identities.candidate_fingerprint`. The
closed vocabulary is `contracts.PROPOSAL_FINGERPRINT_KINDS`, and the marker is
inside the receipt's digest, so a banked receipt cannot be relabelled without
its fingerprint moving.

**Journal:** `event=correction.crossover_v2_round_graded` carries all four
verdicts AND the three axes WITH their evidence (the *why*, which the statuses
alone cannot say), plus `row=` — the stable identifier of the rule that fired,
since three of the five rows restore and two keep, so `adoption=` alone cannot
say which. `…_round_restore` carries the restore, `…_round_receipt` where it
landed. The receipt itself carries the three axes at `round_axes`, because the
receipt is what the NEXT round reads and "keep, and here is what to fix" is only
actionable if the targets travel with it.

**The rollback anchor is durable.** `save_v2_state` takes `durable=`, and the
two writes that own `pre_apply_profile` — `observe_apply_success`, which
creates it while the new graph is already live, and `observe_restore`, which
clears it while the old graph is already back — pass `durable=True`, as does
the receipt-identity write. Everything else stays cheap; `persist_conductor_state`
runs after every consumed capture and an fsync per capture buys nothing.

**The `closing` screen** (PR-T3) is the measuring session's own TAIL, and it
exists because the review screen used to render there. Accepting the final
cloud position marks every stage-1 phase accepted, so `_phase_from_state`
resolved the instant that capture landed — while the runner was parked in D1's
held-set window with the relay live and the wizard polling at 1.5 s. The
household read *"JTS measured your speaker but has no correction to propose —
measure again to try afresh"* over a measurement that was still running, with a
destructive "Measure again" beside it: an absence being reported as a verdict
when it was only a timestamp. `cloud_close` on the durable state
(`awaiting_confirm` / `running` / `""`) is what tells those moments apart from a
session that genuinely ended with nothing, and the host persists `running`
BEFORE the close runs so the seconds it can cost are a named state
rather than a stale "confirm on the measurement page". The screen offers **no actions at
all** — every one it could offer destroys work in progress — and sets `busy`
on the fit-in-flight moment, the flow's one genuinely machine-paced wait.

Since the eager-fit rider (2026-07-30) `running` is usually a sub-second
flash: the fit ran on index 10's acceptance, so the close is a commit. It
still costs a full fit whenever there was no banked build — a retake
discarded it, or the eager attempt failed — which is exactly when the named
state earns its keep. `awaiting_confirm` deliberately keeps rendering while
that eager fit works: the household still has something to do and it is on
their phone, so `running` would both misreport whose move it is and have to
be walked backwards on a retake.

**The `review` screen** (two-stage commission work order D3/D6, PR-T2) is the
household's apply decision point, and the terminal a MEASURE-ONLY session
resolves to once its candidate exists. `_phase_from_state` used to walk a stage-1 session's
`session_phases` and fall through to `PHASE_DONE` — the RESULT screen, "Your
speaker is tuned" — over a speaker that had been measured and never touched;
its one special case (VERIFY unaccepted ⇒ `PHASE_APPLYING`) cannot fire when
VERIFY is not in the recorded phases at all. A session whose walked phases
contain no VERIFY and that is not `applied` now resolves to `PHASE_REVIEW`
instead. `applied` still wins (the decision has been made, and PHASE_DONE's
"applied implies graded" ladder owns that copy), and a corrupt `session_phases`
still walks `PRE_CLOUD_CAPTURE_PHASES` — which contains VERIFY — so a garbled
state file cannot reach the review screen either.

The screen renders the pre-apply cloud (measured), the candidate summary
including the era-stamped `headroom_cost` level charge, the predicted curve
drawn DASHED in the same deviation frame (it is a model, not a measurement),
and the spec verdict stated plainly — naming the band and the margin past its
tolerance when the prediction misses. An improved-but-spec-failing fit is
**presented, never applied silently**: the miss is named and Apply stays
available, because the decision is the household's. Actions are Apply and
verify / Measure again / Keep current sound, and **never Undo** (D6 — stage 1
replaced nothing, so there is nothing to restore).

**"Keep current sound" is an action, not a link (#2641).** It POSTs
`/correction/crossover/v2/decline`, guarded on
`expected_candidate_fingerprint` like Apply, and records the household's
answer at `state.review_decision`. It still changes nothing on the speaker and
still does not delete the candidate. It was minted href-only until #2641
measured what that cost: the click reloaded the page back onto the same
decision screen, and the record could not tell a decline from a household that
never looked — the fact a series needs before it offers another bite. Its
`href` is retained as a presentation hint; the client prefers `endpoint`
whenever an action carries both, which is what makes every in-flow action
performable by a driver as well as by a browser. Once recorded,
`_phase_from_state` resolves to the journey's resting screen instead of
`PHASE_REVIEW` — bound to the candidate the decline answered, so a newer
measurement brings the review back rather than inheriting a stale "no".

**"Measure again" inherits the lapsed session's tier (#2639).** Every
re-measure action the envelope mints posts an empty body, because the action
does not know what the session was, and `resolve_plan_shape` resolved an absent
tier to `TIER_FULL`. A remote session's own retry therefore minted a tier the
turntable rig cannot walk, and Express households were demoted by the same
line. `prepare_v2_session` now reads the durable tier when the body omits one,
matching `prepare_v2_verify`'s `_verify_plan_shape`; an explicit body tier still
wins, so the tier chooser and the Express done screen's "Run a Full
measurement" are unaffected.

Apply is enabled only when all three hold: a candidate with a fingerprint
exists, the prediction is gradeable (`overall_passed is not None` — a graded
MISS still qualifies; only unknown does not), and the **stage-2 openability
preflight** resolved. That last one closes the hole the work order's premise 5
named: `prepare_v2_verify`'s next line is `resolve_conductor_context(status)`,
which is fail-closed and carries seven refusal sites plus
`ensure_crossover_preview_ready()`'s — so a box can be applied and still be
unable to open stage 2, leaving it corrected with no verdict.
`attach_stage2_preflight` (`jasper.web.correction_crossover_v2`) runs that SAME
predicate on the envelope path, **only** for `PHASE_REVIEW`, and stamps
`crossover_v2.stage2_preflight`; the envelope reads it and never treats an
absent key as permission. A refusal renders verbatim with its registry-declared
resolution control beside it and emits
`event=correction.crossover_v2_stage2_preflight_refused`. It is computed in the
web layer because `jasper.active_speaker` never imports from `jasper.web`, and
gated to one phase because the predicate is not cheap and
`ensure_crossover_preview_ready()` can rewrite the preview.

**What it costs now that PR-T3 has made the screen reachable.** T2 wrote that
this cost nothing because no `index_phase_map` could omit VERIFY; stage 1's map
now omits it by design, so the preflight runs — once per envelope GET while the
review interlude is on screen. One call is roughly six JSON reads, a
canonical-JSON profile fingerprint, and a preset compile, and it is not free of
side effects: `ensure_crossover_preview_ready()` can rewrite the preview file
and the topology file, though only when the preview is absent, stale or blocked.
Two things bound it, both structural rather than a cache: it runs ONLY on the
review phase, and **the interlude is not a polled screen** — the wizard polls
only while a relay is in flight (`schedulePoll(relayIsActive(env.relay) ? …)`)
and stage 1's session has ENDED by the time this renders, so the calls stop
within seconds of the relay winding down and a household sitting on the decision
costs nothing. T2 named that second bound as T3's to re-check; it holds.
Pinned by `test_a_stage_1_map_has_no_verify_and_a_stage_2_map_does`, the
re-derivation of T2's tripwire.

**The pre-POST half of that preflight is PR-T3's and has landed**:
`handle_v2_apply` runs `_assert_stage_2_can_open(status)` immediately before
the transaction commits — after the freshness gates, so a stale candidate still
gets its own specific refusal — and fails closed on an unexpected error as well
as on a refusal (`event=correction.crossover_v2_apply_stage2_preflight_refused`
/ `…_failed`). A disabled control is not a security boundary: a stale page, a
second tab, or a direct POST all reach the endpoint, so the screen's honesty
layer and this refusal are two halves of one guarantee. `status` is a REQUIRED
keyword-only argument, so no caller can quietly skip it.

*The carve-out disclosure* (flat-linearization plan PR-6b, owner decision 1 of
2026-07-25). The gauge says how flat the speaker measured and how many
spec-band bins left grading; `carve_outs_by_band` — same registry, same
`evaluate_flat_spec` report, published as the pipeline's `carve_outs` key —
says WHICH ranges left and why. One entry per spec band, always all of them and
in the report's own order, each holding the ranges that overlap that band
tagged with the instrument that carved them: `identified_null` rows carry
τ/r/rung/depth/classification (the exclusion reason of record), `position_screen`
rows carry none of that because the screen measures disagreement, not an
arrival. The two are listed separately rather than merged, so "both instruments
flagged this range" stays visible; `merged_excluded_bands_hz` remains the merged
view for counting. Two copy registers per band, both owned in
`crossover_v2_flow` beside `_geometry_guidance_copy` so a chart callout and the
expert disclosure cannot diverge: a plain-language `disclosure` headline, and an
`expert` line carrying τ and both r estimates. The rows ride the same chain the
gauge does (`_compact_cloud_status` → `/state` + the envelope's `cloud` block →
`_flatness_details_lines`' `expert_details`), from whichever cloud that
function read — carve-outs are a post-apply-persistent fact ("EQ cannot fill
these") disclosed on every tier, so they ride the pre-apply block too when no
post-apply cloud exists. **The spec table is not
changed by any of this** — 8–16 kHz still reads ±2.5 dB, applied to whatever
survives the carve-out. `carve_outs` is the largest key on a `/state` cloud
entry (3162 of 4056 JSON bytes on the S0 ten-position cloud, measured
2026-07-27) because the copy strings ARE the disclosure; that cost is stated in
`_compact_cloud_status`'s docstring and pinned by
[`tests/test_crossover_v2_cloud_pipeline.py`](../../tests/test_crossover_v2_cloud_pipeline.py),
which also pins the copy discipline (no hardware nouns; the `position_invariant`
wording names travels-with-the-speaker OR a fixed path, never one of the two).

*The per-position members* (attribution plan WO-1, `positions` key). Everything
above is an aggregate. The combiner has always computed each position's own
analysed curve and echo diagnostic — `CombinedResponse.per_position_diag_db`
and `.per_position_echo` — and `assemble_cloud_group_result` used to drop them,
persisting one 512-point power-mean curve plus `n_confident` /
`clustered_fraction`. That is why the 2026-07-29 corpus retrospective had to
rebuild every feature-stability figure from raw WAVs pulled off the Pi, and why
P2 (position-variance) was not the free probe the attribution plan's §5 calls
it. `jasper.attribution.position_evidence.position_evidence_block` now
serializes the members alongside the aggregate: per position, its curve on a
shared **1/12-octave log grid from the group's validity floor up**, its echo
scalars (τ, confidence, concentration, refusal), and — joined from the
conductor's own retained metadata — the gate actually applied, the summed
ripple, which attempt survived (`take_id`), the capture's SHA-256, and (since
the 2026-08-24 geometry ruling) the pose it was taken at, as `position_deg` /
`position_axis` / `mark_distance_m`. Cost
measured on the S0 ten-position cloud, **per closed group, and it depends on
which serialization you mean** — the two stores write the same block
differently, so quoting one figure alone understates the other by ~13 KiB:

| Store | Serialization | Before → after | Added |
|---|---|---|---|
| Bundle artifact (`cloud_<phase>.json`) | canonical JSON (`separators=(",",":")`) | 28,047 → 43,706 B | **+15,659 B (+15.3 KiB, +56 %)** |
| Durable v2 state file | `json.dumps(..., indent=2, sort_keys=True)` | 38,420 → 67,209 B | **+28,789 B (+28.1 KiB, +75 %)** |

That is ~1.57 kB per position canonical (89 grid points), of which ~1.6 kB per
group is the inline `field_descriptions` the attribution plan's §6 requires so
the artifact reads without code. Serialization only — no new signal, no
threshold, no verdict —
and it never raises, degrading to `{"available": false, "reason": …}` like the
rest of this block. It deliberately does **not** ride `_compact_cloud_status`:
`/state` stays the shape-scoped projection, pinned by
`tests/test_attribution_persistence.py`.

*The before/after chart and anomaly callouts* (flat-linearization plan PR-7).
`jts.local`'s `/correction/` crossover page renders the gauge and the
carve-outs above rather than only disclosing them in `expert_details` text:
`deploy/assets/correction/js/crossover/chart.js` draws the combined cloud
curve for both `cloud_measure` and `cloud_verify` on one canvas, and
`cloud.js` renders the carve-outs' `disclosure`/`expert` strings verbatim as
callout cards (server-owned copy; the frontend never phrases anomaly text
itself). Two payload additions feed it: `_compact_cloud_status` gained
`reference_db` (report-level) and `spec_bands[].tolerance_db` — both were
already computed by `evaluate_flat_spec`, only not previously projected —
and a new, separate `_chart_cloud_status`/`cloud_chart` key carries the
decimated curve at its OWN 256-point ceiling (half of
`crossover_v2_flow.CLOUD_CURVE_MAX_JSON_POINTS`'s persisted-artifact
resolution, since this key rides every ~1.5 s envelope poll — measured
20,653 bytes for both phases on the S0 corpus, against 41,161 unhalved).
Kept off the compact `cloud` key so the doctor (which reads only `cloud`)
never has to parse curve-shaped data mixed into it; it still rides the same
envelope response as `cloud`, so the split does not reduce that response's
byte cost, only its shape for a `cloud`-only reader.

**The chart plots each phase relative to its OWN `reference_db`, never a
shared one.** The two references differ — when the fit is cut-only,
`cloud_verify`'s sits at or below `cloud_measure`'s, and since the boost
ruling (#2106) a permitted lift can move it the other way, which the
per-phase reference handles without caring about the direction. An early
draft plotted both curves in
absolute dB against `cloud_verify`'s reference alone, which displaced the
whole "Before" curve by a level change the spec never grades, under a
corridor that was not testing what it claimed to (caught by review,
2026-07-27). The corridor is `0 ± tolerance_db` per band in this deviation
frame — the same window for both curves — with the y-domain bounded to the
spec's own GRADED frequency range (derived from `specBands`, e.g.
250–16,000 Hz, never hardcoded): the curve's worst point sits inside
`flat_spec.BEST_EFFORT_ABOVE_HZ`'s "never specced" region on real hardware
data (a driver's natural top-octave rolloff), and letting an ungraded
extreme set the y-scale reproduces the same failure one band further out.
The wider displayed range (20 Hz–20 kHz) is still drawn at full resolution
and canvas-clipped where it exceeds that bound.

**Provenance.** A re-armed verify-only session can carry a `cloud` group
forward from an earlier session verbatim (see "Session binding" above) —
`_cloud_summary` stamps each closed group with its producing session id,
and `_compact_cloud_status` compares it against the caller's current
session to render a `provenance_note` (empty unless the data is genuinely
stale, mirroring `_geometry_guidance_copy`'s own "silent unless actionable"
rule). Geometry's own "spread the mic further" guidance
(`geometry_guidance`) renders as a plain caption; `thin_evidence` softens
the copy server-side, so the frontend never branches on it. Contract tests:
[`tests/test_crossover_v2_cloud_visualization.py`](../../tests/test_crossover_v2_cloud_visualization.py)
(page-shell ids, hardware-noun discipline over this PR's own authored copy)
and [`tests/js/crossover_cloud_callouts_test.mjs`](../../tests/js/crossover_cloud_callouts_test.mjs)
(rendered-HTML pins for the callout/provenance/geometry text, including the
`position_invariant` cannot-classify phrasing, verbatim). The chart's own
pixel rendering is verified on-device only — CI cannot see pixels — and is
still owed to the HW product smoke below.

*The gate's trusted-floor intersection (#2551).* `cloud_validity_floor_hz`
takes the WORST (highest) reflection-gate floor across the group's positions
— the same "worse of the two" rule `_measure_validity_floor_hz` applies to
the driver branches — and `cloud_trusted_floor_hz` turns that `1/T` into the
`2.5/T` the gate disclosure prints and the delta probe already refuses to
grade below. **That** trusted number is what `evaluate_flat_spec` intersects
every spec band's lower edge with, and the reference band's: a bin the gate
cannot support must not set a verdict, and must not re-centre the frame the
surviving verdicts are stated against either. Both floors are published —
`validity_floor_hz` for provenance, `trusted_floor_hz` as the number the
verdicts were taken above — and both are carried through
`_compact_cloud_status` onto `/state`, the envelope, and the doctor.

The intersection is a band **edge**, not a mask entry, so:

- a band's `n_excluded` stays *exactly* the honesty instruments' own count,
  and `merged_excluded_bands_hz` (and so `/state`'s
  `excluded_interval_count`) remains the "how much interference did we find"
  number — a short window can never inflate either;
- the edge each band was graded from rides on it as `graded_lo_hz`, beside
  the nominal `f_lo_hz`, the same nominal-vs-honest pair the gate's delta
  probe publishes as `literal_band_hz` beside `eval_band_hz`; and
- a band left wholly below the floor is `evaluable=False` with
  `graded_lo_hz >= f_hi_hz`, **never `passed=False`** — absent evidence is
  not a failure. `overall_passed` still counts it as not-passed, so nothing
  is flattered by the distinction; and
- a band the floor CUT but did not swallow carries `max_at_graded_edge`
  (#2599) when its reported extremum landed on the lowest graded bin. Those
  two conjuncts are the whole test — no slope is measured — and what follows
  from them is that the reported maximum is a maximum over a SUBSET of the
  band, i.e. a lower bound on its real worst deviation. Round 3 on jts3 read
  `+4.49 dB @ 358` from a band graded at 357.14 while the ungraded region
  below held `+5.08 dB @ 329`. Disclosure only — `passed` does not read it.

Measured on the S0 main leg (all pinned by
[`tests/test_flat_spec_ssot.py`](../../tests/test_flat_spec_ssot.py)): **all
ten** positions gate to a 142.857 Hz validity floor, i.e. a **357.14 Hz**
trusted floor. 142.857 Hz sits *below* the 250 Hz spec edge — which is why,
while the evaluator was handed the validity floor, the clamp changed no
graded number at all and this section used to call it a no-op. 357.14 Hz
sits *above* that edge and costs 73 graded bins, re-centring the reference
−27.2386 → −27.2997 dB and moving the headline `max_db` **+0.0611 dB in the
flattering direction** — exactly the reference shift, since the worst bin
survives. No verdict is bought: all three bands still fail on their merits.

The mechanism's cost at a higher floor is pinned separately, at
`CLAMP_TRUSTED_FLOOR_HZ` = 1777.8 Hz (the value `cloud_04` produced before
PR #1991 rejected it as a #1790 early fire). Clamping there moves **987
bins** out of the 250 Hz–2 kHz band, re-centres the reference **−27.2386 →
−28.3062 dB**, moves the **headline `max_db` −8.9389 → −7.8713 dB (+1.0676
dB, the flattering direction)**, moves the pooled RMS 3.8031 → 3.1740 dB,
and **flips the 250 Hz–2 kHz band verdict** from +4.2458 dB (fail) to
−1.2146 dB (pass). The headline number therefore moves *further* than the
RMS, and the direction is response-shape dependent — measured on this
corpus, not a property of the clamp. It is the same speaker graded on fewer
bins, visible in the gauge's own `n_bins`.

*Deferred alternative:* per-position, per-bin validity masking inside
`combine_positions` (mask each position below its own floor, keep the other
positions' good data) is strictly better and is deferred only because it is a
`spatial_combine` signature/estimator change, not a wiring one. Revisit
trigger: a real session where one short gate meaningfully shrinks the graded
band — the S0 corpus is already that evidence now that the floor is the
trusted one.

**The fit's honesty ladder is NOT this claim.** `LinearizationFit`'s
fit/verify/observe levels (including `observe_octave_summary`, rendered by
`crossover_envelope_v2._linearization_octave_rows`) are per-driver
diagnostics on the fit's envelope grid from the single design-axis MEASURE
capture. PR-5 relabeled the rendered line accordingly ("fit residual vs
target (design-axis capture, not the spatial measurement)") so no surface
presents a per-driver fit residual as the measurement. The two will
legitimately disagree.

**One MARK for the whole session: ~1 m on the listening axis, tweeter
height, facing the speaker.** The placement screen encodes a tolerance
window (~±0.3 m distance, ±10 cm height) for that mark. The session
starts there, returns to it for MEASURE and VERIFY, and prompts small
moves around it for the two position groups — so the mic is stationary
*per sweep*, never *per session*. Taps: CHECK is the one tap before any
measuring, MEASURE takes a second one (same spot, but it is the longest
capture and the one that can be the loudest — issues #1823/#1825), a
trusted candidate auto-arms
VERIFY with no household action in between, and each prompted cloud
position needs its own tap because the household has to move the mic
first.

**Pre-session courtesy tone (issue #1677) and the program's audible
order.** The capture that OPENS a session plays three short ~1 kHz beeps +
~3 s of silence from the speaker under test itself — a "quiet please, a
measurement is starting" warning from the speaker the room can hear,
replacing the 2026-07-23 lab-only interim (a Mac-side `osascript beep`,
then a fan-in-TTS-lane 3-beep burst).

**It announces a session, not a capture (trimmed 2026-08-18).** It shipped on
every capture — 9 of them on a Full journey, 32.4 s of held-still silence — and
`courtesy_prelude_for_phase()` in
[`jasper/active_speaker/crossover_v2/programs.py`](../../jasper/active_speaker/crossover_v2/programs.py)
is now the one rule that decides. It answers **yes** for `check` (stage 1's
opener), `verify` (stage 2's), and `entry_baseline`; **no** for everything
else. The entry baseline is not an opener: it is there because its program
object must stay identical to stage 2's anchor, and that `program_id` equality
is #2291's before→after comparison and the delta probe's anchor check. What
justifies dropping the repeat is that the mux measurement window is held for
the whole session (no household audio can start mid-session for a sweep to
collide with — the incident's own hazard is closed at the session boundary) and
every later capture is begun deliberately: by the household's tap, by the
remote tier's position gate, or — on a hand-walked round running on the WIRED
source (#2879) — by both, the tap behind a gate the person releases. Five of a
Full journey's captures no longer
pay it: 18.0 s. The 37.2 s this paragraph also credited to a shorter verify
walk was the 2026-08-18 trim's, and the 2026-08-24 geometry ruling spent that
capture again on the design axis — so the prelude saving stands and the walk
saving does not.

One consequence to know when reading `program_for_phase`: the summed sweep is
now TWO held objects, not one. The compared pair (`verify` / `entry_baseline`)
gets the announced program; the position groups (`cloud_measure` /
`cloud_verify`) get its prelude-less twin. Restoring the prelude to the groups
reproduces the anchor's `program_id` byte for byte, which is how
`tests/test_crossover_v2_programs.py` states that the two differ in the prelude
and in nothing else — not in the min-cap clamp that is their only level guard.

The audible order, since 2026-07-28 (issues #1810/#1812 — the previous
revision of this paragraph still described the pre-#1771 "prepended
group" shape, one revision stale):

| Phase | Order |
|---|---|
| CHECK | 12 s session-ambient window (silent) → **beeps** → ~3 s settle → pilots |
| VERIFY / the entry baseline | **beeps** → ~3 s settle → 1 s ambient window (silent) → pilots → guard → sweep |
| MEASURE / a lateral pose / every cloud position | 1 s ambient window (silent) → pilots → guard → sweep(s) — no beeps |

Two rules hold on every phase that HAS a prelude, both pinned by composition
tests in
[`tests/test_audio_measurement_program.py`](../../tests/test_audio_measurement_program.py):
**nothing audible precedes the first beep** (PR #1771 left MEASURE/VERIFY
opening on two full-gain pilot chirps ahead of the quieter beeps — the
owner heard "sweeps then beep beep beep" on 2026-07-28), and **the beeps
are followed by the settle and nothing else** except that 1 s ambient
window, which is silence and must be measured *inside* the quiet the
beeps just asked for. `courtesy_beep_to_stimulus_gap_s` derives the real
interval from any composed program; `COURTESY_MAX_BEEP_TO_STIMULUS_GAP_S`
is the bound.

**What the phone says while that order plays (issue #1824).** The host
posts one phase per audible thing, on the program's own clock:
`prelude_started` → `ambient_started` → `sweep_started` → `sweep_complete`.
The offsets come from the composed program's segment table
(`program_phase_schedule`), so the table above and the phone's copy cannot
drift apart, and the ladder is anchored at the play path's WAV handoff
(`PlaybackStartSignal`) rather than at arm time. Before this,
`sweep_started` was posted synchronously in `on_armed`: the phone read
"Playing the measurement tone…" for the whole ~4.6 s of beeps, settle and
ambient window on MEASURE — silence, labelled as the tone.

`ambient_started` carries **`duration_s`** (the page renders a live
countdown) and **`quiet_requested`**, which is a measurement fact rather
than a presentation one. The two windows in the table above are opposites:
MEASURE/VERIFY's 1 s pre-pilot window sits after the beeps and must be
quiet, so the phone asks; CHECK's 12 s window is the SESSION's room-noise
measurement, *deliberately* taken before the household is asked to go
quiet, and the ambient band-floor report and the gain solve both read it.
A phone that asked for quiet during CHECK's window would edit the floor it
is measuring — a copy string silently changing a measurement — so the host
derives the flag from the composed order and the page never asks on its
own initiative.

The anchor leads real audio by the verified-source read plus the output
prefill, so every step carries a `PHASE_LADDER_START_SKEW_S` bias
*intended* to land late rather than early. **ON-DEVICE:** that interval has
not been measured; the skew is a safe-direction estimate, not a guarantee,
and is named in
[`jasper/web/correction_crossover_v2_relay.py`](../../jasper/web/correction_crossover_v2_relay.py).
Measure it on hardware before tuning, and prefer an observed playback start
over a smaller guess.

The prelude is composed as ordinary segments on the SAME
`ExcitationProgram` the phase already plays and admits — never a second
playback path — so it rides the session's existing volume/admission
machinery for free. Its level is derived per
program channel from that channel's own loudest scheduled stimulus gain
(`courtesy_tone_gain_db`, 6 dB below, clamped to never exceed it and never
positive), and its kind (`KIND_COURTESY_TONE`) is deliberately excluded from
`STIMULUS_KINDS` so the locate/deconvolution machinery treats it exactly
like a silence segment — invisible to analysis, present in the recording.
No config switch (see `courtesy_prelude_for_phase` in
`crossover_v2/programs.py`); the phone's per-entry `duration_ms` budget asks
the same rule for the same phase, so it is not a second thing to keep in
sync. See the "courtesy-tone prelude" section of
[`jasper/audio_measurement/program.py`](../../jasper/audio_measurement/program.py)'s
module docstring for the segment shape.

**The pre-pilot ambient window (issue #1810).** The 1 s silence in the
MEASURE/VERIFY row above is not cosmetic: it is what makes the pilot SNR
guard exist on those phases. `PilotObservation.snr_valid` gates the
behavioural-linearity verdict on the quiet pilot clearing
`PILOT_MIN_SNR_DB` (≈12.4 dB) over the room's in-band floor — but until
this window shipped, those programs had no floor to measure, the guard's
input was `+inf` by construction, and it could never fire. A JTS3 session
on 2026-07-28 hit exactly that: a freshly-applied correction dropped the
pilot band 14–18 dB, the quiet pilot landed ~5 dB over the room floor,
noise compressed the captured two-pilot delta from 10 dB to 6 dB, and the
household was told *"Your phone's microphone changed its own levels
mid-measurement"* while `pilot_transfer_step_db` — the only direct
recording-chain evidence path — was null. MEASURE, VERIFY and every cloud
position now check `pilot_snr_ok` **before** their linearity branch and
answer with `pilot_level_collapse` ("the room was too loud, or the speaker
too quiet"), the same discriminator W6.12 gave CHECK. The window feeds the
level/SNR path only, never `_channel_map_ok`'s ±12 dB rise test — that
threshold was calibrated against CHECK's long framed estimator, and
keeping the short window out of it also pre-empts arming a hard-stop flag
nothing routes on yet (see gotcha #20). `agc_behavioral_fail`'s own copy was amended in the
same change to state what it observes (the two tones came back at the wrong
levels) rather than assert a cause it never measures; the definite mic
accusation now lives only on `verify_level_shift`, which holds the
cross-attempt transfer step.

**Both ambient windows CLIP to the capture, never SLIDE along it (issue
#1818).** A window's end is computed from its own schedule position, not
from a start clamped at 0, so a capture that began late yields a SHORTER
window rather than one that has walked forward onto whatever the program
scheduled next. CHECK's 12 s window is butted directly against the courtesy
beeps at [12.0, 12.6) s, and a sliding window read them as room noise: on the
shipped geometry a 0.6 s late start measured the floor 39.5 dB hot (−70.00 →
−30.52 dBFS window RMS), poisoning both `snr_floor_ok` and the gain solve, and
by ~5.9 s late the window had slid onto the PILOTS and refused a quiet room
outright. Below `AMBIENT_MIN_USABLE_FRACTION` (0.5) of the scheduled window
both helpers degrade the same honest way — no samples and an EMPTY band
report, so `snr_floor_ok` is False and the gain solve discloses
`no_ambient_evidence` — rather than estimating a floor from a fragment. One
constant, one policy, both windows.

The RESULT screen (phone end screen + wizard `done` screen) states the
outcome plainly first ("Your speaker is tuned. If it sounds worse than
before, you can undo.") with the measured numbers (trims/delay/polarity/
confidence/ripple) folded into a collapsed "Technical details" disclosure,
and Undo given the PRIMARY button on the wizard so the safety net is the
most visible thing on the screen — **when there is one to give** (#1863). A
first-ever apply never stashed a `pre_apply_profile`, so `handle_v2_restore`
refuses; the screen omits Undo entirely rather than leading with a button
that cannot work, and promotes the head of its alternates instead ("Try
again with what we learned" on an iterating round, "Continue to Room
correction" otherwise). The verdict COPY above still says "you can undo"
in that case — a known gap tracked at `crossover_envelope_v2._can_undo`.

**The wizard owns the VERDICT; the phone owns only the shared headline**
(issue #1964). The phone's end copy is the `done_title`/`done_body` baked into
the stage-2 capture plan when stage 2 is ARMED — before the first tone — so it
cannot know the post-apply cloud's spec verdict, which is computed from the
last capture and can fail while tracking passes. Full's copy therefore states
"Your speaker is tuned" (the claim all seven of jts.local's done verdicts
open with) and points at the speaker page, rather than pre-committing
"Verified and applied." The relay cannot repair this late: its host-event
slot is last-write-wins, and `capture_set_complete` routinely overwrites the
final `capture_result` before the page's ~250 ms poll reads it.

### Recommending an Fc

> **DELETED 2026-08-22 — read this section as archaeology only.** The sweep
> that recommended an Fc, its candidate set and its compute budget are gone
> (`docs/tuning-master-plan.md` plan ruling R1, ticket 2.3), and the
> `fc_selector` module that scored and adjudicated them went with ticket 2.4,
> along with the review screen's **Use N Hz and apply** button and the
> `SELECTION_*` verdicts the grade read. A round crosses at the corner the
> household declared or an operator pinned; no round produces an `fc_selection`,
> the field is absent from the persisted record rather than written null, and
> no product read path parses one a round banked while a selector existed —
> only the offline archaeology tooling still does, on purpose
> ([testing-tooling.md](../testing-tooling.md)). What survives in
> `fc_sweep.py` is corner ADMISSIBILITY — the spine's file map says what.
> Everything below described the sweep and its selector while they existed.
>
> **The apply path below is NOT gated on that record** (2026-08-19, and this
> half still holds). It asks the candidate being applied what crossover it
> carries and compares that with what `/sound` declares, so ANY producer of a
> candidate measured somewhere other than the declaration reaches it with no
> further wiring — the sweep was one such producer while it existed, an
> operator's topology pin is the live one, and whatever comes next needs no
> change here. An ordinary round still writes nothing to Sound, because its
> candidate is measured at exactly the declared crossover.

R17. The session evaluates the crossover frequencies the DECLARATIONS admit and
tells the household which one measured best. A configured-Fc winner keeps the
existing **Apply and verify** path unchanged. An alternative winner offers one
exact action — **Use N Hz and apply** — with no copy/paste step.

The declared crossover in `/sound` remains the crossover's only writer, and
`jasper.active_speaker.crossover_declaration` is where the two spellings of one
crossover — the declaration's `frequency_hz`/`filter_type`/`slope_db_per_octave`
and a preset's `fc_hz`/`target_type`/`order` — are compared. When they differ,
the apply first asks Sound to save the candidate's crossover against the draft
revision captured when the measurement opened, then reopens Sound's current
draft/preview and passes the same candidate-specific preset through
`baseline_profile`'s existing apply transaction. That ordering is required, not
incidental: `baseline_profile`'s `measured_candidate_preset_mismatch` guard is a
whole-preset equality, so the declaration must already carry the candidate's
crossover before the recompose. A stale Sound revision refuses before DSP. If
Sound saves but DSP fails before load or proves rollback, the screen says
saved/not applied and a retry skips the already-completed Sound save (the
inverse it would otherwise lose is persisted in the same state write as the
revision that save produced). An unconfirmed transaction instead says its
current DSP result is unknown and asks the household to review the speaker
state before retrying. There is no automatic next-Fc loop and no cross-service
rollback of Sound on an apply failure; **Keep current sound** remains a
non-mutating exit.

**Frequency AND slope, through one writer.** `sound_setup.
apply_measured_crossover_geometry` carries both, because slope compiles into
`CrossoverRegion.order` and therefore fails the same equality guard a moved
corner does. Its compare-and-swap checks all three declared fields; a
declaration that omits any of them is left alone rather than completed with a
number nobody declared.

**A crossover below the tweeter's declared protection floor is refused BEFORE
the declaration is written.** The L0 emit gate refuses the same condition, but
it can only do so after that write (see the ordering above), which would leave
`/sound` declaring a corner the speaker is not playing and cannot be made to
play. So the apply path re-checks the shared predicate
(`driver_protection.protection_highpass_floor_satisfied`, against
`test_signal_plan`'s two owner-supplied numbers) at the boundary and refuses
with `reason=crossover_below_declared_protection_floor`, displacing nothing.
This is the one deliberate duplication on the path: the proposer, the boundary
and the emitter check one rule against three different failures.

**Undo reverses the declaration too, or says why it did not** (#2292). Because
that accept WRITES Sound, an Undo that reloaded only the DSP graph left the
speaker playing one crossover while `/sound` declared another — and the next
session reads that declaration as its configured Fc. So a successful apply
records the inverse of its own Sound write in the durable v2 state
(`sound_declaration_undo`: the revision the accept produced, the driver pair,
and both the geometry it wrote and the geometry Sound declared before it —
frequency, filter type and slope on each side), and `handle_v2_restore` replays
it backwards through the SAME in-process `apply_measured_crossover_geometry`
writer — Sound stays the crossover's only writer, no cross-service hop is
involved, and the slope comes back with the frequency because one writer wrote
both. The household sentence names the slope only when the slope is what moved.

**One state write owns both halves of an Undo.** `observe_apply_success` writes
`sound_declaration_undo` on the same line-pair as `pre_apply_profile`, and
**every** successful apply re-stamps both — an ordinary configured-Fc apply
passes `None`, which CLEARS a record it has just superseded. That co-location
is the contract, not a convenience: while the record was written on the
alternative accept instead, an alternative apply → Start over → ordinary apply
left the graph half describing the newest apply and the declaration half
describing the older one, so Undo restored the recent graph and wrote a
two-applies-old frequency into `/sound` while reporting success (gate finding,
2026-08-10; the apply→apply regression test is what now holds it). Downstream,
both keys are carried forward unconditionally by `persist_conductor_state` (the
post-apply VERIFY re-arm persists under a new session id), preserved together
by Start over, and cleared together by `observe_restore` —
`test_every_host_owned_apply_key_survives_persist_conductor_state` derives that
class mechanically, so the record is covered by it rather than by a list
somebody has to remember.

The two legs are reported separately, because they can honestly disagree:
`status` stays the graph's answer and `payload["sound_declaration"]` carries the
declaration's — `declaration_restored`, `declaration_refused_sound_moved` (the
draft revision moved since the accept: Sound is left byte-identical rather than
discarding somebody's edit), `declaration_not_applicable` (this apply never
wrote Sound — every configured-Fc winner), or `declaration_restore_failed` (the
writer itself failed). The declaration leg runs only AFTER the graph is back —
reverting it first and then failing the restore would invent the same
inconsistency pointing the other way — so an Undo whose graph half did NOT
succeed omits the key entirely. It never raises, so a declaration that could
not be put back is reported beside a successful restore instead of turning a
working Undo into a 500. The two actionable outcomes ship a household sentence
in `sound_declaration_message`, which the wizard shows in place of "Updated."
**and re-asserts after its own refresh** — `renderRelay`'s terminal branch
writes "Capture complete." over the status line, and after an Undo the
post-apply VERIFY's relay is sitting there complete, so setting it once would
show it for one turn of the event loop. The journal line is
`event=correction.crossover_v2_restore_sound_declaration outcome=…`. On an
AUTOMATIC rollback (`bind_delta_probe_rollback`) the outcome is journal-only:
that seam returns a bool. #2291 Phase 3a bound the seam on the stage that
actually reaches the delta probe — the two halves used to sit on different
stages, with the measuring conductor holding the binding it could never use and
the verifying conductor reaching the probe with no binding at all — so an
automatic rollback now genuinely runs. Which stage binds it is declared once, in
`STAGE_VERIFY_CAPABILITIES`; see "The stage bridge" above.

**Where each piece lives.**

- `jasper/active_speaker/fc_selector.py` (deleted by ticket 2.4; unlinked
  because the path no longer resolves)
  — the pure kernel: `band_flatness` (R18's mean-removed signed-worst
  arithmetic), `predict_pose_sum_db`, `score_candidate`, `select_fc`. No
  conductor state, no I/O. `FcCandidateEvaluation` is the memory contract.
  A retained winning record also carries its compact executable candidate and
  bounded prediction so publication never re-analyzes a different Fc.
- `fc_candidate_set` / `resolve_fc_search_band` in `crossover_v2/fc_sweep.py`
  (both deleted — the candidate set with the sweep at ticket 2.3, the
  search-band resolver by the 2026-08-22 owner ruling, #2870; neither name is
  exported or re-exported any more) — the set, bounded by four declarations:
  the HF driver's hard floor, the lower driver's ceiling, the INTERSECTED
  declared `crossover_search_band_hz` (a two-way Fc puts both drivers at Fc,
  so every participating role had to admit it, and an undeclared role meant no
  proposal), and the ka beaming ceiling from the declared diameter. Candidate
  order was the configured Fc first, then sorted unique alternatives, and the
  configured Fc was mandatory even when a bound excluded it — otherwise the
  speaker had no golden candidate. Only two of those four bounds outlived the
  sweep, both of them declared hard-excitation edges: the search band named no
  damage mechanism, and #1675 had already made the beaming ceiling disclosure
  rather than a fence. The spine's file map says what `fc_sweep.py` holds now.
- `CrossoverV2Conductor._sweep_fc_candidates` — runs at MEASURE-consume,
  because the raw capture is alive only there. Per candidate:
  re-corner the sections, re-point THREE prior fields (`crossover_fc_hz`, the
  configured crossover response, the candidate-required bands — polarity and
  the protection map are preset/safety-derived and ride unchanged), re-analyze,
  fit against that candidate's own branch target, reduce to one small record,
  **release**. Fit scratch state is isolated between candidates; a fit-failed
  Fc is refused rather than borrowing the preceding candidate's prediction.
  The branch TARGET is per-candidate; the fit's Fc-driven level and ripple
  terms are not — issue #2291 owns that gap, and
  [`tests/test_crossover_v2_incident_replay.py`](../../tests/test_crossover_v2_incident_replay.py)
  characterizes it.
- `_adjudicate_fc` at the close of an ADJUDICATING walk — §4.4's rule that
  anything reading the whole walk waits for the whole walk. A walk whose
  consumer is not this selector never reaches it (#2732).

**Two bounds worth knowing before you touch it.**

*Compute and result waits have separate, explicit owners.* The serial sweep's
one-time compute budget is derived per sweep by `fc_sweep_budget_s(planned)` —
`planned × FC_CORNER_COMPUTE_COST_S` (16 s, rounded up from the measured
per-corner ceiling), so a plan narrowed by declarations asks for less wall than
a full one. `FC_SWEEP_COMPUTE_BUDGET_S` is the largest budget any plan can ask
for (six corners, 96 s).

The per-capture result wait is **minted from that ceiling on the Pi**
(`fc_sweep_result_wait_s` = ceiling + `FC_SWEEP_RESULT_OVERHEAD_S`, the measured
10.48–12.46 s of blob pull, anchor analysis, publication and polling) and
carried to the capture consumer on `CaptureSpec.result_wait_s`.

The configured candidate always starts. Later candidates start only when the
slowest attempt so far forecasts they fit; otherwise each is represented as
`evaluation_budget_spent`.

`FcSelection.comparison_complete` is the one completeness fact: true only when
every deliberately selected candidate was attempted. An attempted but invalid
candidate remains an honest refusal and does not make the sweep incomplete. A
budget-skipped candidate does. An incomplete sweep may retain the safe
configured-Fc linearization, but cannot select, persist, or present an
alternative as best; its durable summary labels the comparison incomplete.

*Memory is bounded by releasing, not by caching.* Ten banked jts3 rounds
(2026-08-17/18) attempted 45 alternative corners; the 42 with a timed cost span
11.65–15.52 s. The configured corner runs 1.7–2.3 s (it reuses the anchor's
analysis rather than re-running it), a complete all-six sweep 62.81–69.84 s, and
the phone-visible capture-complete→`crossover_v2_result` wall
67.95–81.28 s (73.29–81.28 s for the five all-six rounds); analysis workspaces
are around 400–500 MB. The derived budget is a bounded deployment ceiling, not a
claimed runtime — it is sized so completion is the normal case, not so it is
guaranteed. Each
candidate's intermediates are released before the next allocates. The retained
set holds compact executable candidates, bounded full prediction/delta arrays,
and ~120-point lateral scoring arrays; all six are capped below 512 kB by test.

**Debugging.** `event=correction.crossover_v2_fc_sweep` and
`event=correction.crossover_v2_fc_selection` disclose candidate order,
attempted and reasoned skipped candidates, elapsed/budget, and
`comparison_complete`, plus the verdict and limits. A candidate that could not
be scored is disclosed with a reason code, never dropped: `fit_refused`
(no candidate, or the session is not Layer-1a eligible), `no_trusted_crossover_region`,
`evaluation_budget_spent`.

The sweep line is emitted at **WARNING** when `comparison_complete=false`,
because that is the state in which the selector can no longer move Fc whatever
the evidence says. Its `budget_short_by_s` field says how much more wall the
forecast wanted (`0.0` when nothing was declined) — the number that separates a
momentarily loaded Pi from a budget that no longer fits its work.

### Failure taxonomy & debugging

> **Live reference — the historical tag above does not cover this section.**
> The runbook's "[Debugging — where to look
> first](../tuning-operator-runbook.md#debugging--where-to-look-first)"
> delegates its deeper catalogs here,
> and they are maintained against the code rather than frozen with the
> campaign. Read that spine section first; it flags what in here is still
> dated. One clause of the tag above still applies here: campaign-era class
> names are kept where they are what an entry was about — chiefly
> `CrossoverV2Conductor`, dissolved in #2291 Phase 5c-iv, whose `_refuse`,
> `_log_*_diag`, and `_safe_log_diag` are methods on `CrossoverV2Session`
> today. The catalogs are maintained; the names around them are not renamed
> under you.

Terminal verdicts are **internal reason codes, not screens.**
`REASON_REGISTRY` (in `crossover_v2/refusal_copy.py`, re-exported by
`crossover_v2_flow.py`) maps each code to one of
four templates (`silent_auto_retry` / `fix_and_retry` / `hard_stop` /
`session_restart`) plus the two special screens (`verify_fail`,
`volume_recovery`), its owning phase, and whether it is retriable at all
(`retry_budget == 0` ⇒ `NON_RETRIABLE_CODES`; the retry COUNT is
per-position, not per-code — see "Retries are bounded per POSITION"
below). The conductor decides the code; the envelope renders the copy —
one copy source, no drift.

**A failure screen has a lifetime (#1942).** The persisted `failure`
record carries its own `at` stamp (epoch float, written by
`persist_conductor_state`), and `build_crossover_envelope_v2` renders the
terminal screen only while that stamp is inside
`crossover_envelope_v2.FAILURE_FRESH_WINDOW_S` (30 min). Older than that,
the household gets the ordinary entry screen plus ONE dated `info` nudge
— "Your last measurement ended yesterday — the check didn't pass." — and
Undo whenever `applied` is true. Before this, any persisted failure
re-rendered its terminal screen on every build forever, so a fresh page
load the next day was answered with a dead session's screen *and* that
session's numbers presented as the live verdict (the owner's 2026-07-30
"level error 3.82 dB" greeting). The discriminator is the record's own
age because nothing structural distinguishes the two cases: `session_id`
/ `accepted_phases` / `applied` are frozen by the terminal persist and
read identically a second later and a week later, and relay liveness
moves the wrong way (a terminal failure purges its own relay within 3 s).
A record with **no** `at` — anything written before #1942 — reads as
aged; the state file's `STATE_SCHEMA_VERSION` is deliberately NOT bumped
for the new key, because a bump makes `load_v2_state` reject every
deployed Pi's file and would take `pre_apply_profile` (and with it Undo)
down with it.

**There are TWO numbers surfaces, and the aged path suppresses both.**
`expert_details` is the obvious one. The other is the before/after chart
card: `_envelope` copies `cloud` / `cloud_chart` / `tier` through from
`status` on *every* screen, `persist_conductor_state` writes `cloud`
beside `failure` in the same state file, and `crossover/main.js` calls
`renderCloud` with no screen switch — so a resume rendered the dead
session's curve, its spec-band numbers, and the caption "the
after-correction curve appears once the second measurement pass
finishes", a live-progress claim about a session that ended yesterday.
`_aged_failure_envelope` nulls those three keys, which is simply the
honest value for a screen with no session (`tier`'s own contract already
reads `None` as unknown). Note that `cloud[<phase>]["session_id"]` cannot
substitute for this: on a resume that stamp *equals* the state's own
`session_id` — it is the same dead session — so provenance filtering
downstream would not have caught it. The aged branch declaring "no
session" is what does.

**One reason code is exempt from the generic aged copy.** Most reason
copy describes a session outcome, which stops being actionable when the
session ends. `correction_rollback_failed` does not: it says the delta
probe found the correction faulty, failed to roll it back, and the
speaker is *still playing it* until somebody presses Undo. That is true
tomorrow too, so `_DURABLE_STATE_FACTS` keeps its fact and its
instruction inside the dated history line (gated on `applied`, so the
claim is never printed over a state that no longer corroborates it). The
registry was audited in full for this; the line is "a change JTS itself
made and did not undo". Notably the `program_profile_*` family is *not*
exempt — that copy states configuration JTS re-checks every session, so
replaying it as current would be the #1942 defect itself, and the live
`_setup_ready` gate above the failure branch already catches a genuinely
unready setup.

| Code | Phase | Budget | Meaning |
|---|---|---|---|
| `agc_behavioral_fail` | CHECK / MEASURE / VERIFY | 1 | the captured two-pilot level delta did not match the programmed one, at an SNR where it should have. The phone's input chain riding gain OR the speaker's own output compressing — the copy names the observation, not a cause it never measures (#1810) |
| `noisy_room_linearity` | CHECK | 1 | linearity failed *and* the ambient SNR floor failed — room, not phone |
| `pilot_level_collapse` | MEASURE / cloud / VERIFY | 1 | the quiet pilot never cleared the room's in-band floor, so no level comparison from the pair is evidence — room too loud, or the playback level collapsed (e.g. a correction that dropped the pilot band). Checked BEFORE the linearity branch on all three phases, so a collapsed pair can never surface as the phone's fault (#1810) — and on MEASURE, before the **glitch** branch too (#1838): low SNR causes the glitch signal, so asking the glitch first reported "capture glitched" for a capture nobody could hear |
| `snr_floor` | CHECK | 1 | room too loud / phone too far; also the quiet pilot's own in-band SNR too low to trust the linearity estimate (gotcha #16). **Third producer:** an *unusable* CHECK ambient window — below `AMBIENT_MIN_USABLE_FRACTION` (0.5) of the scheduled window, e.g. a very late capture start — degrades to an EMPTY band report, which `_snr_floor_ok` reads as False and this code then reports. That case means **"we never heard the room"**, not "the room was loud", and the two are indistinguishable in the code but not to the household; the log (`event=program_analysis.ambient_window_unusable`) carries how much window survived. Fuller explanation in the clips-not-slides section (#1818) above. CHECK-only — the other phases use `pilot_level_collapse` |
| `anchor_ambiguous` | CHECK | 1 | the analyzer could not decide WHICH scheduled tone the capture's first arrival was, so it cannot say which driver played what. Sits ABOVE `channel_map_mismatch` in the ladder (#2644): a mis-anchored capture reads every per-driver window one pilot spacing from where that driver played, which on 2026-08-16 turned a correctly-wired speaker into a rewire instruction. Fires when the top two anchor hypotheses BOTH clear the locate floor and the winner's witness is less than `ANCHOR_DISCRIMINATION_RATIO` (50x) more PRESENT than the runner-up's; `ambiguous=true` on `event=program_analysis.anchor` is the field to read when triaging one, the `presence=`/`runner_up_presence=` pair is what it judged, and `runner_up_anchor=`/`runner_up_shift_ms=` name the timeline that nearly won. **Not a catch-all for mis-anchoring** — a capture whose runner-up misses the locate floor never reaches this rung and is refused by the ones below it. Copy names the recording, never the speaker — nothing about the hardware is known to be wrong here |
| `channel_map_mismatch` | CHECK | 0 (hard stop) | drivers played out of order (wiring, or a very noisy/quiet room). Since #2644 only reachable behind an anchor the arbitration did NOT flag ambiguous — which is not the same as a confidently-anchored one. `ambiguous=false` means only "the guard did not fire", and several routes reach it on thin evidence — a capture whose candidates both score BELOW the locate floor is one (nothing to corroborate against, so the pre-#2093 reading stands and the capture fails on its own merits), a runner-up alone below it is another. Any of them can still reach this row. See the row above |
| `clipped` | MEASURE | 1 | auto quieter retry (gain −3 dB). MEASURE-only: VERIFY replays the *identical* program on every attempt (that invariant is what makes the `verify_level_shift` baseline mean anything), so there is no quieter retry to offer — a clipped VERIFY capture is refused as a capture glitch instead (#1971). This row said "MEASURE / VERIFY" until then; no VERIFY path ever returned this code |
| `drift_baselines_disagree` | MEASURE / VERIFY | 1 | glitch/dropped-buffer, or woofer-repeat level disagreement — auto retry. One code covers the whole capture-glitch class by design; `glitch_inputs` in the diag says which bound actually tripped on MEASURE (#1765), and `integrity=` says which check tripped on VERIFY (#1971, where the class is a spliced timeline or a clipped run). Since #1838 a merely weakly-located sweep is NOT in this class on either phase — it answers `locate_failed` ahead of this branch |
| `delay_exceeds_search_window` | MEASURE | 1 | mic likely off the pictured spot |
| `locate_failed` | any | 1 | the capture's stimuli could not be located. Since #1838 this requires EVERY stimulus role to clear `LOCATE_MIN_CONFIDENCE` (it was `max()` over the whole capture, so one confidently-located driver cleared the gate for a driver nobody heard), and on MEASURE it also carries the split-out sweep locate-confidence floor (`guard=sweep_locate_confidence` in the diag). Since #1971 VERIFY carries the same 0.3 floor for its summed sweep (`integrity=summed_sweep_heard` in the diag). **Since #2085 its copy is not a literal**: `locate_failed_message` reads `pilot_snr_ok` — a pilot that WAS heard refutes "couldn't hear the speaker", so that capture is told JTS could not line up the test tones, naming no cause, instead of being sent to the volume control (`pilot_heard=` on `event=correction.crossover_v2_result` says which). Relay verdict, terminal exhaustion, defensive replay refusal, apply-seam refusal, and envelope all select the diagnosis from the same paired evidence; retry versus terminal state changes only the action. Since #2093 the timeline ANCHOR those confidences are measured against is cross-checked before this code can be reached — a knife-edge mis-anchor used to fabricate this verdict on pristine captures; see "Timeline anchor" and read `event=program_analysis.anchor` first when triaging one |
| `program_unplayable` | play seam | 0 (hard stop) | admission refused the program (bug/tamper/infeasible profile). Every ADMISSION refusal except `program_profile_not_confirmed` lands here, and the underlying admission slugs ride out in `state["failure"]["refusals"]` so a support read can tell which one fired (#1820). The play seam has one other refusal that is not an admission refusal at all and does not land here — `measurement_volume_drift`, below |
| `measurement_volume_drift` | play seam | 0 (hard stop) | #2925: the main fader was not at the volume this session declared when a stimulus was about to play, and re-asserting it could not be proven, so the capture is refused before any audio. The OPPOSITE claim to `program_unplayable` — the program was admissible; the speaker's level was not the one it was admitted against — which is why it is its own code rather than another slug riding that one. **TWO conditions reach it**, which is why the copy names the observation and never a cause (`locate_failed`'s #2085 lesson): the fader was read and would not hold, or it could not be read at all. Read `event=active_speaker.measurement_fader_drift result=refused` to tell them apart — an empty `observed_db` is the unreadable one. Carries no refusal slugs; the observed/expected pair is on that same line, and `set_confirmed` there is the set-AND-confirm's verdict, not the setter's — `true` on a refusal is the interesting one (the repair landed and the fader moved AGAIN), while `false` covers both "the setter refused" and "the setter said yes and nothing moved". Terminal because a safety refusal is not something to retry around, and the re-assert has already been tried. A drift the hold DOES repair is not a refusal — it plays; see "The fader hold mostly does NOT refuse" below. Since #2929 a healthy routed capture produces `result=held` and NEITHER the `repairing` nor the `repaired` line — read both halves, because absence alone cannot say the hold ran |
| `program_profile_not_confirmed` | session open / play seam | 0 (hard stop) | JTS cannot use the saved driver-safety declaration (evaluation `stale` — the outputs moved underneath it — or `malformed`). Both are cleared by one ordinary save, which rebuilds the profile from the visible values. Split out of `program_unplayable` (#1820) because it is deterministic and one edit away, so its copy names *review the limits and save them again* and its `next_action` deep-links `/sound/#confirm-safety-limits` instead of inheriting "re-check the driver details", which is the one action that makes it worse. The slug keeps its wire name; `unconfirmed` no longer occurs, because saving the declaration IS declaring it. Normally refused at session open (see pre-flight below), so the phone screen is the backstop, not the usual path |
| `program_profile_missing` | session open | 0 (hard stop) | evaluation `missing` — no profile exists (never-saved / unreadable / pre-crossover draft). `/sound/` deliberately renders **no** safety callout here, so "review the limits" would name a panel that is not on the page; copy says *finish the driver details in speaker setup* and the action is `/sound/` with no fragment. Pre-flight only: the play-seam admission vocabulary carries one `PROFILE_NOT_CONFIRMED` slug for every un-playable profile state, so only the gate holding the full `DriverSafetyProfileEvaluation` can tell these apart |
| `program_profile_incomplete` | session open | 0 (hard stop) | evaluation `incomplete` — declared values are still missing or do not line up. A save is allowed here (the operator keeps their work) but rebuilds the same `incomplete` profile, so "save again" would be a circle. Copy names the same action `/sound/`'s own callout names in this state: add them under Advanced, then save |
| `internal_error` | any host fault | 0 | catch-all cleanup arm caught a seam raise |
| `relay_timeout` | any | new session | link/session died — Start over mints a fresh one |
| `user_stopped` | any | new session | the household tapped Stop on the phone — honest copy, not a manufactured "timed out" (gotcha #18) |
| `volume_unresolved` | session | — | the `volume_recovery` screen |
| `verify_out_of_tolerance` / `verify_inconclusive` | VERIFY | 2 | Try again / Undo / Re-measure |
| `verify_level_shift` | VERIFY | 2 | G3, session-scoped since #1927 — the recording chain moved DURING this sitting, so the capture is not evidence about the speaker. Structurally unreachable on a session's FIRST usable attempt. Same verify-fail screen; its copy (#1924) is written for the fact that ONE string renders where "try again" is two different controls — the measurement page's in-session re-arm, which re-compares the same reference and can repeat, and the wizard's fresh session, which re-baselines and settles it in one capture. So it names the visible primary and makes Re-measure / Undo the escalation *if the retry repeats*, commanding neither |
| `low_alignment_confidence` | MEASURE | 1 | **TWO causes, since #2087 took away a third.** Alignment confidence below the trust floor, OR the measured delay falls outside the crossover region's declared `delay_range_ms` search bound (± a modest margin) — a confidently-wrong GCC estimate. Either way: re-measure at a cleaner mic position (gotcha #18). The **G1 predicted-ripple** check reused this code as an undocumented third cause until the owner's 2026-08-03 ruling, and that reuse is exactly what made the ruling necessary: a high ripple says the two branches summed incoherently on this rig, which the copy above answers by telling a household to move a microphone that was often already right (#2085), and the refusal then consumed the attempt budget until the session died (#2086). G1 now discloses instead — see "G1 discloses, it does not refuse" below. The `guard` diag field still separates the two remaining causes (empty for both) from G1's `ripple_disclosure`, which now rides an **accepted** capture |
| `apply_failed` | APPLYING | new session | the conductor's own auto-apply came back blocked or errored (gotcha #18). Unlike every other "new session" row, MEASURE's OWN evidence is NOT invalidated (`_persist_terminal_failure`'s §5.6 reset is scoped away from this one code) — an apply failure says nothing about the mic position, and keeping MEASURE accepted is what lets the specific blocked-issue nudge actually render (adversarial review SF2, 2026-07-20) |
| ~~`driver_levels_disagree`~~ | — | — | **RETIRED by the nanny burn-down** ([`measurement-loop-doctrine.md`](../measurement-loop-doctrine.md) deviation (i)). PR-L4 item 1 refused here when, after the committed trim, the two drivers' *realized* levels — read on their own mirrored ±1-octave half-bands about Fc, not across each whole passband — sat further than `REALIZED_LEVEL_MATCH_TOLERANCE_DB` apart. The MEASUREMENT is unchanged and still fires, now as `event=…_level_match_finding` plus a banked level-frame finding, and the round PROCEEDS. Why it went: it graded inter-driver tonal balance and named no component-damage mechanism, so §4's closed list never covered it — and its cost was a refusal the session manufactured for itself. The number it grades IS the MEASURE ripple polish's trim excursion (`difference_db == polish_delta_db[tweeter] − polish_delta_db[woofer]`, since the give-back is a per-role constant that passes each role's excursion straight through — and the woofer term is 0.0 in the shipped program, which ripple-polishes only the tweeter trim), and the polish was admitted out to `RIPPLE_TRIM_SANITY_MARGIN_DB` (6.0 dB) against this gate's 3.0 — so every polish landing in that dead band produced a round certain to be refused. jts3's landed at 3.9 dB. The polish admission is now COUPLED to this tolerance (`RIPPLE_TRIM_SANITY_MARGIN_DB` is deleted) and a rejected polish falls back to the band-average trim, disclosed on `event=program_analysis.ripple_trim_rejected` and on the candidate's `ripple_polish_rejected_delta_db`. **The gate that used to share this code was already deleted** by the single-datum-owner migration (#2609): `event=…_level_frame_refused` fired when two per-driver level estimates disagreed past 3.0 dB *and* the realized check also failed. The level datum has one owner — the **raw per-branch trim solve**, carried as `LinearizationRequest.raw_trim_db` and placed by `intervention.anchor_trims` (fed `anchor_base_db=raw_trim`). The summed at-the-mark capture is explicitly **not** that owner: it rides the applied incumbent graph while the per-branch sweeps ride the protected-neutral one, so combining them double-counts the incumbent's own trims; making it the owner needs a frame reconciliation and an anchor re-place deferred together behind [#2653](https://github.com/jaspercurry/JTS/issues/2653). The two per-driver estimates became an advisory consistency check (`intervention.check_level_consistency`) reporting on `event=…_level_estimator_finding`. A round persisted before either change can still carry this code; readers of a historical failure resolve it through `REASON_REGISTRY.get`, which now misses and falls back |
| ~~`correction_not_an_improvement`~~ | — | — | **RETIRED by the nanny burn-down** ([`measurement-loop-doctrine.md`](../measurement-loop-doctrine.md) deviation (c)). PR-L4 item 2 refused here when the PREDICTED post-apply response failed the flat spec and was not better than its own pre-fit model by `PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB`. A forecast vetoing the measurement that would have settled it, which the doctrine's authority model forbids; it took jts3's first prescribed-boost round on 2026-08-22 (`improvement_db=-0.703`) one log line after disclosing its own level estimators 11.635 dB apart. Item 2 now banks `accountability.LEDGER_NOT_AN_IMPROVEMENT` on `event=…_prediction_gate` and the round proceeds. Its prescribed-class sibling `prescribed_correction_not_an_improvement` went with it — one ledger value for both bars, told apart by `required_db`. **Since rung P3 / R10b both of the comparison's terms carry the committed residual delay, which does not cancel between them and narrows the margin as the residual grows** — see "VERIFY compares the applied response with the summed model at the committed delay" below for the measured curve. A round persisted before the burn-down can still carry this code; readers of a historical failure resolve it through `REASON_REGISTRY.get`, which now misses and falls back |
| `correction_model_error` | VERIFY / post-apply group | 0 (hard stop) | linearization-integrity PR-L5: the delta probe's realized-vs-commanded map does not match in SHAPE — the emitted filters are not doing what the fit's model of them says. Catches the PR-L2 shelf-Q class permanently. **Fires AFTER the apply**, so it rolls the correction back first and then names itself. **Since #2521 it fires only on an exceedance that survives the capture's trusted band AND the removal of the fitted frame** — one that does not survive is the non-rollback `frame_mismatch` finding instead (see "The delta probe verifies the apply" below) |
| `correction_level_shortfall` | VERIFY / post-apply group | 0 (hard stop) | PR-L5: the shape landed but the depth did not — realized/commanded scale below `DELTA_PROBE_SHORTFALL_GAIN_CEILING` on a commanded LIFT. A driver-compression diagnostic. Rolled back. **Since #2521 it sits behind the same frame gate `correction_model_error` does** — a real but in-tolerance depth shortfall riding a room tilt demotes to `frame_mismatch` rather than reverting a tuning on evidence that is entirely instrument |
| `correction_spatially_costly` | post-apply group | 0 (hard stop) | PR-L5: the map matched at the mark and the cross-position level spread WIDENED past `DELTA_PROBE_SPREAD_WIDENING_TOLERANCE_DB` — the correction fitted one position's interference rather than the speaker. Placement, not filters. Rolled back |
| `correction_rollback_failed` | VERIFY / post-apply group | 0 (hard stop) | PR-L5: the probe found one of the three defects above AND the automatic rollback could not run (no binding, a refused restore, or a seam that raised). The correction is therefore **still applied**, and this row exists so the copy says so instead of promising a restore that did not happen. Names Undo as the manual action |

**The fader hold mostly does NOT refuse — four things to know about it
(#2925).** `measurement_volume_drift` above is its rare arm; these are the
ordinary ones.

1. **A repair is an ANOMALY. In a healthy run neither line ever fires
   (#2929).** `event=active_speaker.measurement_fader_drift
   result=repairing` / `result=repaired` used to fire on every CHECK/MEASURE
   capture, and #2925 wrote that down as architectural steady state on a
   mechanism that turned out to be wrong. CamillaDSP does not discard
   `main_volume` on a config replace — it has no config field for a volume at
   all, and the fader survives the reload. The writer was JTS's own graph-swap
   duck: `_duck_release_target_db` released to `min(canonical, released)` with
   `canonical` = the HOUSEHOLD level (`percent_to_db(listening_level)`), which
   is quieter than any measurement volume and so won that `min` every time.
   Both dB values #2925 attributed to "the config" are exactly `percent_to_db`
   outputs (−9.59596 = level 81, −18.181818 = level 64) — that arithmetic is
   what identified the real writer. #2929 hands both the load and the restore
   swap a READER for the level the session plan owns
   (`SessionVolumePlan.owned_measurement_volume_db_nowait`, asked at the moment
   the duck releases), so the fader lands on the declared volume by
   construction.

   **The on-device acceptance criterion is therefore inverted, and it takes
   TWO lines, not one.** A healthy routed run shows
   `result=held` on every capture — the hold ran and found the level already
   established — and `result=repairing`/`result=repaired` on none. Read them
   together: "no repair pair" ALONE cannot tell the fix working from the hold
   never being reached (the #2198 instrument-silence lesson, and the criterion
   this one replaced carried that liveness proof for free). So the bar is a
   `held` line per capture AND zero repair pairs; a `repairing` line means
   something really did move the fader and is worth investigating rather than
   filing as normal, and a MISSING `held` line means the hold is not where this
   doc says it is. `held` is INFO on the same `event=` as the drift lines, so
   one `journalctl` grep answers both halves.
2. **The summed path has no readiness gate, on purpose.** The routed path
   reaches `play_program`, which calls `SessionVolumePlan.assert_ready` and
   refuses a plan that owns no volume; the summed path has no such call, so
   when the plan is not ready its capture PLAYS and BANKS unheld, disclosing
   `event=correction.crossover_v2_capture_volume_unheld`. A second gate here
   would be a second owner of `assert_ready`'s question. That capture's level
   is then whatever the fader happened to be at, and its provenance record is
   what says so (`result=volume_disagreement`).
3. **A person turning the volume down mid-session is overridden at the next
   capture.** Authoritative writes — the management UI, a HID remote — are
   deliberately still allowed to move the fader (the no-nanny rule); the hold
   then puts it back before the next stimulus, because a capture at a level
   the ledger did not admit is not a measurement. "I turned it down and it
   went back up" is correct behaviour, and the `result=repairing` line names
   the value it found. To measure quieter, end the session and re-open it —
   the session volume is derived once, at open.
4. **The summed path's hold runs OUTSIDE the DSP writer lock.** The routed one
   is inside it (it rides the `play_wav` seam), so nothing else can load a
   config between proving the fader and emitting. The summed branch takes no
   lock, so a cross-process fader writer landing in that window can still move
   the level under the stimulus. **The bound is not "quieter than declared",
   and NEITHER racer shape is bounded.** A racing GRAPH SWAP releases its own
   duck to `min(its canonical, current + |its depth|)` — the second operand is
   the *live* fader plus the attenuation that racer applied, not this session's
   entry snapshot (the snapshot appears only in the no-provider fallback) — so
   it effectively lands on ITS canonical, the household level. A direct volume
   writer (the coordinator's reconcile, the management UI, a HID remote) has no
   bound of its own either. Both therefore inherit the household level's sign,
   and it has none against the declared one: it was quieter in the night's
   configuration (−21.2 against −12.5) and it can be LOUDER — this branch's own
   `test_a_loud_household_is_pulled_down_before_any_audio` fixture is that
   case, and in it a racing swap lands the fader ABOVE the declared level. The
   only hard ceilings are `MAX_MAIN_VOLUME_DB` (0.0 dB) and the running
   config's `volume_limit`, which `ensure_volume_limit_db` refuses above 0 dB,
   so the worst case is up to (0 − declared) dB above the admitted level,
   12.5 dB on the campaign's numbers.
   (The pre-2026-08-22 era is neither direction: household and measurement
   COINCIDED at −8.0, which is exactly why the bug stayed masked then.) The
   provenance tripwire catches the move whenever retention is
   on. Closing the window properly means giving the summed branch the same
   lock; not done in #2925, which deliberately did not alter verify-phase
   behaviour beyond routing it through the shared seam, and not in #2929,
   which changed only where a swap's duck releases.

**G1 discloses, it does not refuse (owner ruling, 2026-08-03, #2087).** A
predicted ripple above `MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB` accepts the
capture, banks `{predicted_ripple_db, threshold_db}`, and carries an honest
reservation to the household. The threshold survives as the disclosure
trigger; only what crossing it *does* changed.

Where it surfaces: `event=correction.crossover_v2_ripple_disclosed` (WARNING)
in the journal; `guard=ripple_disclosure` on the existing per-capture
`correction.crossover_v2_measure_diag` line — **paired with `accepted=true`,
so that field can no longer be read as "a check refused"**; one `info` nudge
(`crossover_v2_ripple_reservation`) on the review and done screens; and the
measured pair in those screens' collapsed expert details. State lives at
`state["measure"]["ripple_reservation"]` and carries across the stage-2 bundle
hop on the same `PHASE_MEASURE`-absent predicate the banked-findings
projection uses — without that the caveat would reach the screen where a
household DECIDES and not the one that tells them the speaker is tuned.

Why it was a bad refusal, from the live 2026-08-03 bench validation: a capture
at 15.244 dB was refused 58 s after an identically-positioned 11.324 dB
capture was accepted, at alignment confidences 0.677 and 0.677 — both well
clear of the 0.6 trust floor, so confidence was never the discriminator the
reused reason code claimed. The household was told to move a correctly-placed
microphone, and the attempt meter then ended the session (that second half is
fixed separately — see "Retries are bounded per POSITION" below; it is quoted
here as the incident, not as current behaviour). The threshold's own
calibration corpus was collected on clean rigs at 4.4–9.0 dB; a room and
recording chain that simply sit at 11–15 dB are the case the ruling addresses.

The reservation is scoped deliberately narrowly. It qualifies the EVIDENCE,
never the outcome: `predicted_ripple_db` says how coherently two branches can
sum at all, not how the speaker will sound, and every accountability gate
below still grades the correction itself on this candidate.

**The accountability seam GRADES the confirm seam; it refuses nothing.** PR-6b's
`_publish_measure_candidate` returned `auto_apply: True` on the reasoning that
MEASURE's trust gates had already decided — true about the CAPTURE, silent
about the CORRECTION built from it. (That key is DELETED since PR-T3: nothing
auto-applies.) `_assert_accountable` runs THREE checks between the
candidate build and the publish, most-specific-first — the level-frame
agreement of the two per-driver level estimates, then PR-L4's item 1 (the
committed pair's realized inter-driver level) and item 2 (the spec-graded
prediction) — because that is where the numbers they grade exist. **None of the
three refuses**; the module's own docstring is "Three disclosures and no
refusals, most-specific-first". The agreement check flags the CAPTURE as
retriable on `event=…_level_estimator_finding` (WARNING) and proceeds; item 2
stopped refusing with the nanny burn-down
([`measurement-loop-doctrine.md`](../measurement-loop-doctrine.md) deviation (c))
and item 1 with the realized-level demotion (deviation (i)), and both now bank
what they measured — `event=…_prediction_gate` carrying an
`accountability.LEDGER_*` value, `event=…_level_match_finding` plus a level-frame
finding. The round proceeds to the review screen either way. What the
single-datum-owner migration (#2609) deleted was the agreement check's REFUSAL
ARM, not the check: that arm asked one more question — does the realized-level
check pass on the pair about to ship? — and refused under item 1's own
`driver_levels_disagree` code, so its deletion "removed a second owner of one
refusal, not a stop", and the check itself still runs first on every round.

What that leaves refusing in this flow: the four `correction_*` rows below, which
fire AFTER the apply from the delta probe, and the admission seam, which refuses
a capture on its own codes. Those build `CaptureBeginRefused` where they classify
it. `CrossoverV2Conductor._refuse` — the constructor that stamps
`_last_failure_code`, which the host's `CaptureBeginRefused` arm reads INSTEAD of
the exception, falling back to `relay_timeout` when unset — had exactly one
production caller, the accountability refusal, and has none since deviation (i).
The stamp rule it exists to enforce is unchanged and still the reason a refusal
raised any other way would render as a manufactured timeout.

**The delta probe verifies the apply; the ROUND rolls it back** (PR-L5, rerouted
by the fifth principle). The three `correction_*` rows above are the only
refusals in this flow that fire after the speaker has already changed, so each
one UNDOES the correction before it names itself — the household copy says "the
previous sound has been put back" and that is already true when they read it.
The probe used to fire that restore from a seam of its own, which was a second
owner of "restore the previous graph" AND preempted the adoption table, so a
rollback round wrote no receipt. Now the probe reports, `evaluate_round_quality`
escalates a non-deferred rollback class, and
`coordinator._run_round_restore` runs the same `handle_v2_restore` the Undo
button runs (bound as the conductor's `rollback` seam by
`bind_delta_probe_rollback`) — one restore path, and the receipt records what it
did. A conductor with no binding still refuses and says so on
`event=correction.crossover_v2_round_recovery_required`; a successful restore is
`event=correction.crossover_v2_round_restore`.

What the probe is, and what it is not: it classifies
`measured − predicted` — the SAME comparison the `verify_out_of_tolerance`
tracking check gates on, read off `ProgramAnalysis.verify_tracking_curve` so
there is one construction and two consumers — but over **the band the
correction actually commands something in**, where tracking looks only at the
`[Fc/2, 2·Fc]` handoff window. On JTS3 that is the difference between seeing
a 5–12 kHz shelf-realization defect and not: 2026-07-27's lived an octave and a
half above tracking's band and no tolerance there could have caught it. Design
and the verdict-priority rule: `jasper/active_speaker/delta_probe.py`.

**The commanded axis is the applied graph minus the graph it REPLACES** (#2611).
Every element of an apply is on it — the emitted filters, the per-role gains,
the polarity and the delay — because the two sides are the same measured branch
pair evaluated under two different graphs. It used to be the linearized branch
prediction minus the raw one with BOTH sides at the applied candidate's own
polarity, delay and gains, so those three cancelled by construction and the
curve carried the filters alone. On 2026-08-16 that cost a household a tune the
room had measured better: a commanded +3.3209 dB tweeter step (−10.2141 →
−6.8932) landed in bins the axis called quiet and was reported as
`residual_offset_db = +3.2198`, and the frame that decides the rollback question
was fitted entirely ABOVE the band it was then applied to. Owner:
`jasper/active_speaker/crossover_v2/commanded.py`. The pre-split program
headroom is the one level term that axis cannot carry — it is applied before
the branch split — and it stays where it was, as `expected_offset_db`.

**A commanded CHANGE is not a declared STATE, and the two directional safety
rules need the second one** (#2614). `boost_over_declared_bound` and
`realized_louder_than_commanded` ask *is the speaker putting more energy into a
driver than the applied graph declares, anywhere* — including bands this apply
did not touch. A repeat round commands ~0 across every such band, so masking
those two rules on the change axis alone left an untouched +5 dB boost realized
4 dB hot reading `matched` / `boost_overshoot_db=None`. `classify_delta_probe`
therefore takes a second, optional `declared_transfer_db` — the applied graph's
own predicted transfer against the uncorrected crossover, which is what the
retired axis was — and masks those two rules on the UNION of the two axes'
graded bins. Everything else the probe measures is untouched, because
`realized − commanded` is `measured_post − predicted_post` whichever graph the
two sides are stated against. The curve crosses the stage bridge beside the
commanded one as `verify_priors.declared_transfer`; its absence narrows those
two rules back to the change axis and says so on the journal
(`event=correction.crossover_v2_declared_transfer_unavailable`).

Neither curve contributes a VALUE to those rules — they choose bins, and since
series-2 D1 the value is the anchored excess. That is also why a union mask
bridging a run the graded mask would break is sound for them and is not for
`_structured_exceedance`: a bin the correction commanded nothing at corroborates
nothing about the model's SHAPE, but a speaker measuring 4 dB hotter there than
before the apply is direct evidence about a driver wherever it sits.

**That band is intersected with the capture's own TRUSTED band, and there is no
fallback** (#2521). The band comes from
`gate_disclosure.evaluation_band_hz` — this capture's gate-derived trusted floor
intersected with the band its stimulus actually radiated — read off the gating
block by `build_gate_disclosure` and threaded in by `_gate_trusted_band_hz`. The
probe used to pass the raw grid edges, which were wider at BOTH ends: the first
remote JTS3 session (2026-08-14) disclosed `357-20000 Hz` and the probe graded
`325-22,480`, then rolled a correction back on a `max_error_db=23.4` whose worst
bin sat at 21,266 Hz — a frequency nothing had measured anything at. A capture
with no trusted band leaves the probe `unavailable`
(`event=correction.crossover_v2_delta_probe_no_trusted_band`), the same answer
`_verify_absolute_result` gives for the same missing fact; falling back to the
grid would apply the widest band to the least trustworthy capture. The commanded
floor is tiered on the same argument: `DELTA_PROBE_MIN_COMMANDED_DB` (0.5 dB)
below `DELTA_PROBE_HF_SPLIT_HZ` and `DELTA_PROBE_MIN_COMMANDED_HIGH_DB`
(2.5 dB, the HF tolerance) above it, because a bin commanding less than the
uncertainty the tolerance already concedes there cannot answer "did the speaker
do what we asked". Lifting the LOW tier too would delete the defect the probe
exists for — the 2026-07-27 shelf's commanded curve passes through 0.5–1.5 dB
across the octaves its error lives in, and a flat 1.0 dB floor takes its
exceedance run from 0.575 to 0.307 octaves, under the width rule.

Unlike the tracking check, this comparison is **not** level-offset-invariant —
a level shortfall is one of the things it classifies — so it takes the apply
boundary's declared move (`expected_offset_db`,
[invariant 10a](../crossover-v2-engine-design.md)) and removes it
before classifying. What survives is measured where the correction commanded
nothing and reported as `residual_offset_db`; a material, sufficient residual
is the `level_mismatch` verdict, which is a finding, not a rollback.

**That residual is a CHANGE, and its claim is bounded by where it was measured**
(#2533). `measured − predicted` is an in-room gated capture against an on-axis
two-branch model, and their level anchors do not agree; that disagreement is a
standing property of the comparison, present before the apply and unchanged by
it, so a residual that reports it is not reporting a level move. The probe
therefore also takes the PRE-apply capture in the same frame — `entry_delta_db`,
built by `_run_delta_probe`'s `_entry_delta_db` from `verify_priors.entry_baseline`
(#2291's key; nothing new is retained) — and the standing term cancels. It is
disclosed as `entry_anchor_offset_db`; `None` there means no pre-apply curve was
usable, and then nothing is removed and the standing offset stays visible, on
exactly `expected_offset_db`'s "nothing known" rule. The decomposition is an
identity the record carries: `residual_offset_db == frame.offset_db −
entry_anchor_offset_db`.

**Both curves are stated against the ENTRY graph, which is what makes the
subtraction an identity** (#2611 closed the #2545 chained-round hazard).
`commanded_delta` is the applied graph's predicted sum minus **the predicted sum
of the graph it replaces**, and the entry capture is a measurement of that same
graph, so the model term cancels and the residual is
`mean(measured_post − measured_pre − commanded)` over the quiet bins.

It did not always. While `commanded_delta` was the new correction's transfer
relative to the RAW crossover, a REPEAT round's residual carried
`−mean(previous round's commanded curve over this round's quiet bins)` — zero
when that round commanded nothing there, otherwise unbounded, and invisible to
the probe. It could **fabricate** a shift (a previous round correcting out to
20 kHz against a new one stopping at 8 kHz measured a +6.000 dB phantom) and it
could **mask** one (a genuine −2.2 dB uncommanded shift re-grading to residual
0.000 and `frame_mismatch`). Both shapes were constructed by the adversarial
gate on PR #2545; neither is reachable through the production caller now,
because the previous GRAPH is an explicit input to the axis. The classifier's
behaviour on a mismatched pair is still pinned — it is a public function and a
direct caller can hand it one — by
`test_a_repeat_round_carries_the_previous_rounds_command_into_the_residual`.

**What the caller still owes** is that its previous side describe the graph the
entry capture actually went through, and the crossover corner is the part of
that which is CHECKED rather than assumed (#2614). The branches are composed
through whichever crossover the capture was analysed at, so a previous graph
modelled on the wrong corner omits a term measured at up to 5.88 dB against a
1.5 dB tolerance. **Two doors reach it, not one** — an earlier revision of this
section named only the first and called the second benign:

1. the household edits Fc in `/sound` between rounds;
2. an operator's topology pin, which opens the session AT the pinned corner
   (`apply_topology_pin`), so the round's branches carry a crossover the applied
   profile never ran.

(A third door, the alternative-Fc sweep, closed with the corner hunt on
2026-08-21 — `docs/tuning-master-plan.md` ticket 2.3. The guard below is
unchanged: it was never counting doors, it compares corners.)

`commanded.profile_crossover_fc_hz` reads the applied graph's own corner off its
snapshot preset and `CrossoverV2Session._previous_graph_predicted_sum` refuses
the previous side on a mismatch — `event=…previous_graph_unavailable
reason=crossover_corner_moved`, and `reason=applied_profile_names_no_corner` for
an era-older record that cannot say. `entry_anchor_offset_db` discloses what was
removed and is **not** a warrant that the residual beside it is clean.

**A refused change axis costs the SHAPE grade, and since series-2 D1 the
hearing-safety one too — disclosed, not silently** (#2614). A round opened at an
operator's **topology pin** hits that refusal by construction — its branches are
composed through a corner the applied profile never ran — as did every committed
alternative-Fc candidate while the corner hunt existed. While `_run_delta_probe`
bailed on that refusal nothing ran at all: `evaluate_applied_safety` answered
SAFE on a round where nothing had looked, and no surface said so. The STATE axis
needs no corner match — both of its sides are the candidate's own — so it is
computed and persisted whatever corner the round ran at, and the probe runs on
that alone:

- verdict `safety_only`, reason `commanded_axis_unavailable`
  (`delta_probe.VERDICT_SAFETY_ONLY`), journalled at WARNING;
- `model_departure_over_tolerance` / `max_signed_error_db` are real: how far the
  room sat from a two-branch model just rebuilt at a different corner;
- **`boost_over_declared_bound` / `realized_louder_than_commanded` are NOT.**
  This path has no pre-apply capture to difference against, so the anchored
  excess does not exist and `safety_anchored` is false. #2614 made those two
  fire here on the unanchored curve, which on this path carries no change term
  at all — the D1 defect at its purest. What still holds: the clipped check, and
  the graph's own pre-paid electrical bound;
- **no shape or level scalar at all** — residual, gain, frame and exceedance
  would each be a claim in the state frame, where the residual is the
  chained-round contaminant #2611 removed;
- the safety evidence carries `probe_shape_graded: false` and
  `safety_anchored: false` beside `probe_graded`, and the done screen shows a
  third caveat ("This check could not confirm the correction's shape or its
  loudness this round.") beside the Verified badge. That copy names no CAUSE on
  purpose: four paths reach this verdict — corner moved, applied record
  displaced, record names no graph, record names no corner — and the journal is
  where the specific one is named.

With neither axis the probe is absent exactly as before, and
`event=correction.crossover_v2_declared_transfer_unavailable` names why.

The two conditions that reach the verdict subtract **different** numbers, and
have to: (a) "did the level move" is a change question and reads the anchored
residual, while (b) "do the quiet bins explain the graded failure" is measured
against the model and so removes the quiet bins' whole absolute disagreement
(`residual + anchor`, which is `frame.offset_db`). Handing (b) the anchored
number would leave the standing offset inside the levelled error, and a genuine
uncommanded shift on any speaker whose model is not perfectly anchored would
arrive one gate later as `frame_mismatch` — true, but less specific.

A third condition bounds the CLAIM rather than the finding. A residual is
measured in the quiet bins and asserted across the graded ones, and nothing
checked that those two sets meet: on 2026-08-15 the correction commanded
463 Hz–12 kHz, so the quiet set was a 12–20 kHz sliver whose level was reported
as a whole-band `uncommanded_level_shift`. The set's own span did not show it
either — two strays at 493 Hz and 1.9 kHz made 158-of-160 bins above 12 kHz span
463 Hz–20 kHz on paper — which is why `quiet_core_band_hz` is the INTERQUARTILE
span and not the min/max `frame.band_hz` already reports. `quiet_probe_coverage`
is that span in octaves over **the same statistic taken across every graded-band
bin on the same grid** — never over the band's whole span, which measures the
GRID rather than the evidence. Production grids are structurally linear
(`rfftfreq`; `spatial_combine` refuses anything else because
`smooth_fractional_octave` assumes linear bins), so bin density rises with
frequency and any interquartile span is pulled toward the top octaves: on the
retained 2026-08-15 grid a quiet set sampling a 357 Hz–10 kHz graded band
perfectly and uniformly scores **0.303** against that band's whole span, so a
whole-span ratio is unclearable on real data at any band width. The like-for-like
ratio is grid-invariant by construction — an interleaved co-spanning quiet set
measures **1.000** on the production linear grid and **1.000** on a log one.
`DELTA_PROBE_MIN_QUIET_COVERAGE = 0.5` is then a **judgment, not a derivation**:
what is derived is the 1.0 reference, and the bar admits evidence at least half
as spread as the band's own bins. It sits in a wide measured gap — on the
production grid the tightest passing shape scores 0.800 and the shape this exists
to catch scores 0.249, so any bar between roughly 0.3 and 0.8 makes the same
calls.
Under it, the verdict, the rollback answer and the household surface are all
unchanged — narrowing them would make the instrument stricter on evidence it had
just called unrepresentative, the inverse of the "a gate may only narrow"
asymmetry below — and the reason alone narrows, to
`uncommanded_level_shift_outside_probe_band`, with the covered band beside it.

**A broadband TILT gets the same treatment, for the same reason** (#2521, owner
ruling 2026-08-15: least-bad is adoptable with disclosure, hard stops are
reserved for the safety class). `measured − predicted` is a cross-frame
difference, so a level offset *and* a slope between the two frames are the
ordinary state of it, not a defect in the correction — and with the tilt left
in, every speaker, room, and microphone with a broadband slope failed this probe
however well its filters realized. The probe therefore fits `offset + tilt` over
the **quiet bins** — the same set `residual_offset_db` is measured in, where the
correction commanded nothing and any level or slope is uncommanded by
construction — and re-asks the exceedance with that frame removed. The RAW grade
still decides whether there is a finding (`matched` is unchanged, and the
reported `max_error_db`/`rms_error_db`/`exceedance_octaves` are still the raw
ones); only the ROLLBACK question is re-asked, so a frame fitted from a noisy
quiet region can fail to demote but can never fabricate a rollback. An
exceedance that does not survive is `frame_mismatch`, a finding and not a
rollback. Fitting the frame over the GRADED bins instead would let the defect
subtract itself: on the keystone shelf-Q fixture a graded-bin fit takes its
exceedance from 0.575 octaves to zero (pinned by
`test_fitting_the_frame_over_the_graded_bins_would_delete_the_keystone`). Too
few quiet bins means no frame is measured, and then nothing is demoted.

**The gate sits ahead of BOTH rollback doors**, and that is load-bearing rather
than tidy. `level_dependent_shortfall` is as much a rollback as `model_error`,
so guarding only the shape door leaves the whole #2521 class walking through the
scale one: a 4 dB lift realized at 80 % depth is `matched` on its own — the
0.8 dB miss never clears tolerance — and becomes a `level_dependent_shortfall`
ROLLBACK once a −0.9 dB/octave room frame is added, with its frame-removed
exceedance still exactly 0.0. Over a randomized sweep, 203 of 4,000 draws rolled
back on evidence that was entirely frame (adversarial gate on PR #2530; the
sweep is now a property test,
`test_no_rollback_survives_a_zero_frame_removed_exceedance`). `spatially_costly`
is the one rollback deliberately outside the gate and structurally cannot be
behind it: it is reached only from the branch where the mark map MATCHED, and it
compares two real measurements of the room's spread with no model between them
for a frame to explain away.

`gain_factor` carries an INTERCEPT for the same class of reason: through the
origin, on a commanded curve that is mostly one sign, a constant level shift
arrives as apparent SCALE — the 2026-08-14 session's −7.8 dB against a
76.5 %-negative commanded curve read as ≈2.02 and drove the
shortfall-vs-model-error branch. The regression runs on the frame-removed curve
and reports `gain_intercept_db` beside the scale.

Because neither `level_mismatch` nor `frame_mismatch` is a rollback, neither
reaches a refusal screen, so both are surfaced three other ways instead of
passing silently: the probe logs at WARNING (band it was handed, band it
graded, the frame's terms, the frame-removed grades, the gain and its
intercept), the verdict is persisted as `verify.delta_probe` — the small
durable summary: verdict, reason, the two level numbers, the standing anchor
removed from the residual, the quiet evidence it was measured over (bin count,
interquartile band, coverage — because a band-scoped reason is a claim ABOUT how
little of the graded band its evidence covered), the frame's offset and tilt,
and the bin count and span they were fitted over (a tilt from a narrow quiet
region can be large and mean nothing, so `frame_fit`'s own ill-conditioning
defence travels with the terms it qualifies) — and the
done screen carries a caveat nudge alongside its "Verified." badge. When
there are too few quiet bins to run the level discriminator at all, the verdict
below it carries a `|level_check_unavailable` suffix in its `reason` — a
rollback decided without that check says so.

**VERIFY discloses the FRAME it compared across** (rung P1, correction-program
ladder). `measured − predicted` is a difference between two *instruments*: an
on-axis two-branch model composed from the MEASURE sitting, against an in-room
gated point measurement from the VERIFY sitting. `_analyze_verify` therefore
least-squares fits `offset + tilt·log2(f)` and publishes it as
`verify_tracking["frame"]` — a
`jasper.audio_measurement.frame_fit.FrameComparison`: the two terms plus the
pivot and span they were fitted over, the raw grade pair, and the same pair
re-graded after the frame is removed. **Two scalars, no more** — this is a
disclosure, not a curve-warping framework, and it is deliberately not a
goodness-of-fit test.

**The frame is fitted over the bins the comparison TRUSTS**, not merely the
graded band: `analysis.notch_excluded_band_mask` — the single owner of that bin
choice, extracted from `notch_excluded_tracking_error_db` so the two cannot
drift — drops deep-predicted-notch bins, and the fit inherits that set on top
of the validity-floor clamp. Inside a modelled notch the depth is
hypersensitive to sub-dB branch differences (the W6.7 finding the exclusion
exists for), so a straight line through one lets the notch lever the slope. Two
measured cautions, both on the product path with a 25 dB notch: fitting the
whole band recovered an injected −0.800 dB/oct as **+5.7** at a band edge,
while the shipped trusted-bin fit recovers **+0.31** — 18× better and *still
the wrong sign*. The exclusion bounds a notch's DEPTH (12 dB), not its skirt
WIDTH, so a wide enough surviving skirt still biases the estimate. Treat a
disclosed tilt measured over a notch-heavy prediction as indicative only; see
issue #1990. `n_bins`/`band_hz` ride the record precisely so a reader can see
how much was trusted.

Nothing about the TRACKING grade moved. `max_db_notch_excluded` is still the
number `VERIFY_TOLERANCE_DB` gates on; the tilt-removed pair sits BESIDE it,
computed by re-running the same graders on a frame-removed curve so the two are
one construction over two inputs. (The delta probe went further in #2521 — its
rollback question is re-asked with a frame removed — but it fits its OWN frame
over its OWN bins; see the paragraph above and the next one.) Why it exists: on
the 2026-07-29 corpus the replay scorecard's headline
"predictions are 2.02× optimistic" was ~84 % a single −0.79 dB/octave frame
tilt (2026-07-31 first-principles panel W1 — raw 2.054 dB rms, 1.335 dB with
the one scalar out, crossover-band shape correlation +0.97), and the product
could not tell instrument tilt from model error. It travels on every outcome
including a pass (`verify.frame` in the durable state, `frame offset … , tilt …`
+ `raw: …` + `tilt-removed: …` in the expert-details disclosure, `frame_*` /
`*_tilt_removed` on the verify diag line and in the retained-capture sidecar),
for the same reason the graded band does: a pass is exactly when an unstated
frame would be read as model agreement. **A tilt-removed grade never renders
alone** — it is the friendlier number by construction, so the done screen (which
has no `evidence` block, that being persisted only on a non-pass) prints the raw
pair from the same record; the verify_fail screen already has it above and does
not repeat it. An unfitted frame is disclosed as ABSENT, never as a flat one.

**The delta probe fits a SECOND frame, over a different bin set, and that is
deliberate** (#2521). This one is fitted over `notch_excluded_band_mask`'s bins
inside the tracking band — the bins a MODEL-tracking comparison trusts. The
probe's is fitted over its own QUIET bins, the ones where the correction
commanded nothing, because its question is different: it needs a frame no
commanded defect can have contributed to, and the tracking band is neither
necessarily quiet nor necessarily wide enough to be one. Both read the same
`verify_tracking_curve`, so the two frames are estimated from one comparison
over two bin sets, and both travel in the record (`verify.frame` and
`verify.delta_probe.frame`) rather than one being derived from the other.

**VERIFY judges the crossover region against the DESIGN, not only the model**
(R18, issues #1868 / #1654). Tracking grades measured-vs-`predicted_sum`, and
`predicted_sum` is built from the measured branches — so a real crossover null
the model faithfully reproduces cancels out of that comparison and passes.
Worse, the band it grades (`overlap_band_hz`) clamps its lower edge UP to the
tweeter's MEASURE sweep floor, which on a box whose tweeter is swept from Fc
is Fc itself: the sub-Fc half of the crossover region was structurally
ungradeable. The 2026-08-05 JTS3 checkpoint is the worked case — accepted at
`max_db_notch_excluded = 0.919 dB` over `[2000, 4000] Hz` while its own
post-apply cloud measured **−4.80 dB at 1656 Hz** (signal-derived, 1/3-octave),
344 Hz below the graded floor.

So VERIFY now also grades an ABSOLUTE claim:
[`crossover_region_band_hz`](../../jasper/audio_measurement/program_analysis.py)
gives the region a SUMMED capture can honestly be judged over — `[Fc/2, 2Fc]`
intersected with the capture's own gate-derived trusted floor and radiated band
— which reaches BELOW the tweeter's sweep floor precisely because the composite
is real there even though the tweeter branch alone is not. Widening the
*tracking* band there would be dishonest and was not done. The reference is
`Σ_role sign_role · C_role(f)`, the candidate's own committed crossover
transfers with their configured polarity, so the question becomes "did this
crossover hand off as DESIGNED" rather than "did the graph reproduce a model".
The tolerance is derived, never chosen: `verify_absolute_tolerance_db` returns
the loosest `flat_spec.SPEC_BANDS` entry the region overlaps (2.0 dB for the
shipped 2 kHz two-way), and returns `None` — claim not-evaluated — where the
spec table sets no tolerance.

All four of plan §7's claims now travel as one record (`verify.claims`, the
diag line's `claims=`, and the expert-details disclosure), each `pass` / `fail`
/ `not_evaluated` with its numbers. **Two of them are always
`not_evaluated`**: VERIFY plays ONE mono summed sweep, so there is no
woofer-alone or HF-alone response to compare against its branch. R18
deliberately did not widen the capture plan; it refuses to let "Verified."
imply those two were proved. A not-evaluated claim never gates — refusing on a
measurement nobody made is the same dishonesty pointed the other way.

**The two absolute grades, and which owns what.** `assemble_cloud_group_result`
already produces a `flatness` gauge (`flat_spec.spec_flatness_gauge`). It is
NOT a peer of the crossover-region claim and neither was retired:

| | cloud `flatness` | VERIFY `claims.absolute` |
|---|---|---|
| question | is the speaker flat? | did THIS crossover hand off as designed? |
| curve | spatial mean, post-apply cloud | one design-axis VERIFY capture |
| reference | its own 250 Hz–8 kHz mean | the candidate's crossover transfer |
| band | `SPEC_BANDS`, 250 Hz–16 kHz | `[Fc/2, 2Fc]` ∩ trusted |
| gates? | no (flips the done badge) | classified at the terminal result |

The gauge structurally cannot own §7 claim 3: it is assembled when a position
group CLOSES, i.e. after `_verify_verdict` has already run, and Express and the
R15 driver-only path have no post-apply cloud at all. Its spatial mean also
understates a design-axis null (#1868's forensics: the 8-position mean was
shallower than any single position in it).

**Terminal grading has one owner and four honest outcomes.** The durable result
owner `correction_crossover_v2._post_apply_grade` classifies from the candidate
fingerprint, the VERIFY outcome and its reason code, the tracking claim, the
independent absolute claim, and the predicted-spec comparison's
material-improvement margin; it neither creates a second state machine nor
alters the audition transaction. It reads **no corner-selector record**: the Fc
selector and the `fc_selection` it wrote are retired
([tuning-master-plan.md](../tuning-master-plan.md) ticket 2.4), so comparison
completeness and per-candidate scores are no longer inputs, and a round banked
while a selector existed grades on its own VERIFY evidence like any other.

| Terminal proof | Tracking | Absolute | Outcome |
|---|---|---|---|
| published candidate (fingerprint) + VERIFY pass | pass | pass | `verified_target` |
| published candidate + VERIFY pass + material improvement, with both miss numbers stated | pass | fail | `verified_best_evaluated` |
| tracking failure, a VERIFY regression outside the crossover region, or the forecast's `LEDGER_NOT_AN_IMPROVEMENT` / a sub-bar margin | fail/any | any | `keep_previous` |
| unevaluable — VERIFY inconclusive, a VERIFY fail coded `REASON_VERIFY_CROSSOVER_REGION`, an outcome string this build does not recognise, no fingerprint, a tracking/absolute claim that is neither pass nor fail, or **any other evidence combination without the definitive keep/pass evidence above** | any | any | `inconclusive` |

An **un-applied** round reaches none of the four: it publishes no outcome at
all. Both instruments that could once say a round DELIBERATELY kept the previous
tune are gone — `accountability`'s item 2 stopped refusing and became a grade
(#2854), and the corner selector's `recommend_alternative` retired with ticket
2.4 — so that arm states nothing rather than inventing a verdict.

`verified_best_evaluated` means only that the applied candidate beat its own
predicted baseline by the material margin while missing the absolute target: a
claim about THIS candidate against its own forecast, never about a field of
alternatives, because no round evaluates one.
The target remains visibly failed with miss magnitude/frequency; the copy never says within spec, perfect, or best achievable. Tracking error
`1.398262557 dB <= 1.5 dB` plus a `4.3139 dB` miss near `1.590 kHz` is
`verified_best_evaluated` when the margin clears
`PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB` and both miss numbers are present, and
`inconclusive` when they are not. These outcomes only report:
they do not apply, undo, retry, or recapture valid stored evidence.

**VERIFY discloses WHAT THE GATE DID, and the inconclusive copy speaks from
it** (issues #1966 / #1974). A record printing `gate_window_ms = 7.0` and
nothing else reads as "reflections removed" to every consumer, and across the
whole 2026-07-30 corpus it meant the opposite — no reflection found, window
capped at the search ceiling, nothing gated out. The sentence separating those
two states has one writer,
[`gate_disclosure.describe_gate`](../../jasper/audio_measurement/gate_disclosure.py),
and until R9 it reached no household surface at all: the analysis summary's
copy dead-ended in an off-by-default operator sidecar and the position-evidence
copy in bundle artifacts, both dropped by every projection a screen reads. It
now travels like the frame above — `_gate_record` composes it once at verdict
time, the host persists it as `verify.gate`
(`{disclosure, reflection_measured, moved_rms_db, reflection_delay_ms}` — the
screens read only the first pair; the two numbers were added by tuning-plan
ticket 1.5 so a READER of the banked round gets them without parsing the
sentence), and `_verify_gate_lines` renders the
sentence **verbatim** into `expert_details` on the verify_fail and done
screens, on every outcome including a pass. Verbatim is the contract:
re-phrasing the fields at a render site is how the two states started printing
identically. Absent stays absent — a pre-R9 state file or an ungateable capture
renders no gate line rather than a fabricated one.

The record is written with the outcome and code it belongs to, in one call
(`_set_verify_outcome`), so all three always describe one capture; it therefore
**survives an early-return retry** (`locate_failed` / `pilot_level_collapse` /
`agc_behavioral_fail` conclude nothing) where `verify.evidence` /
`graded_band_hz` / `frame` are cleared. That is why the **done** screen renders
it unconditionally but the **verify_fail** screen renders it only when
`failure.code` equals `verify.code`: `_failure_envelope` routes any code through
that template once the crossover is applied, and one of `describe_gate`'s four
sentences is deictic ("*this capture* could not be gated") — under a later
attempt's headline, with the sibling lines already cleared, it would render as
the screen's only expert line and point at the wrong capture.

`reflection_measured` beside it is
[`GateDisclosure.gated_anything`](../../jasper/audio_measurement/gate_disclosure.py),
the single owner of "may this record claim a reflection was removed", and it is
the one fact the `verify_inconclusive` household copy branches on. That copy
used to assert "the room reflection cut the window short" without ever
consulting it — on BOTH roads to an inconclusive outcome, including the pilot
level-shift road where no reflection and no window are involved. The clause now
has one writer (`crossover_v2_flow.verify_inconclusive_cause`) shared by the
verify_fail screen and the done screen's ungraded verdict, so the two surfaces
cannot drift apart again; `REASON_REGISTRY`'s entry is that writer's
cause-unknown rendering rather than a second copy of the words. Which road was
taken is read from `verify.code`, persisted beside `verify.outcome` in one
write (`_set_verify_outcome`) — **not** from `failure.code`, which is the most
recent rejection of any phase and is nulled by a later persist while the
outcome stands.

**Retries are bounded per POSITION, pooled, and honest** (owner ruling
2026-08-03, issue #2086). One prompted position gets its planned capture
plus at most `MAX_EXTRA_ATTEMPTS_PER_POSITION` (3) extra attempts,
counted in one `SlotAttempts` ledger no matter who asked — a household
"Try again", a voluntary retake, or a geometry rung. `ReasonSpec.
retry_budget` no longer supplies a count: zero still means "no extra
attempt can help" (`NON_RETRIABLE_CODES`), and any non-zero value now
means only "retriable". The relay plan's `max_attempts` still bounds the
whole session.

*Where these live now (#2291 Phase 5c):* the meter and the settle ladder
are `crossover_v2/admission.py` (`MAX_EXTRA_ATTEMPTS_PER_POSITION`,
`SlotAttempts`, `assess_begin`, `settle_spent_slot`); the retry vocabulary
is `crossover_v2/refusal_copy.py` (`ReasonSpec`, `NON_RETRIABLE_CODES`).
`crossover_v2_flow.py` re-exports the first two under their historical
names and keeps `_resolve_spent_slot` as the thin caller.

Every verdict carries the count on the wire as `attempts`
(`{used, allowed, left, by_speaker, by_household}`), and the capture page
renders it — "Measurement 6 of 6 — extra try 2 of 3", plus a note naming
the speaker's own share. `by_speaker` is read off the rejection that kept
the plan alive, never the relay's `retake` flag: a geometry rung travels
the ordinary begin path with `retake=false`, so the flag would bill every
system-forced take to the household.

**A rejection the next begin would refuse is settled AT THAT REJECTION;
it does not end a session with copy that says "measure again."** Two
conditions close a slot and `authorize_begin` refuses on both, so the
ladder (`admission.settle_spent_slot` → `_resolve_spent_slot`) answers for
both — otherwise the same lie returns by the second door.

*The condition rung comes first* (`terminal_outcome=condition_not_retriable`,
journal `event=correction.crossover_v2_position_not_retriable`): a rejection
whose code is in `NON_RETRIABLE_CODES` is terminal however many extras the
slot still has, because nothing about it changes before the next begin. It
keeps the code's own registry sentence — never the exhaustion sentence,
which would be false about a take rejected on its first attempt — and the
`attempts` block rides out unspent, which is exactly what
`terminal_outcome` distinguishes. Two producers reach it: `check_screens`'
`channel_map_mismatch`, and the post-apply round/delta-probe `correction_*`
refusals (`_round_refusal_for` — the probe's own `_delta_probe_refusal` was
the second producer until the fifth-principle routing deleted it, and its
classes now reach this through the round). Both were
reproduced riding out as ordinary retryable verdicts before #2086's second
half — the page rendered "Try again" over `attempts.left: 3`, and the tap
raised `CaptureBeginRefused` pre-play.

*Then exhaustion.* It is settled at the verdict that spends the
last extra, not at the next begin, so the phone
is never handed a retry screen whose button only leads to a pre-play
refusal. Three outcomes, in order: an earlier accepted take at that index
still stands, so it is kept (`kept_earlier_take`); or the group can still
reach `MIN_RESOLVED_CLOUD_POSITIONS` (2 — the floor
`linearization_envelope.position_stability_limit` imposes, *not* the 6/5
plan-declaration floors), so the position is recorded in
`_group_unresolved` with the observed code and the group advances
(`unresolved` on the wire, `accepted` because that is the fixed-length
runner's only "this slot is done" signal); or it cannot, and **that final
capture_result is terminal immediately** (`terminal=true`,
`terminal_outcome=below_position_floor|phase_cannot_proceed`). The generic
relay runner publishes it and returns, so the phone never receives a live
retry button while the conductor waits to refuse the next begin;
`authorize_begin` keeps only a replay/old-page backstop.

Diagnosis and action are distinct for **every retriable reason**. Each
positive-budget `ReasonSpec` is built from one `RetryableReasonCopy`
(diagnosis + still-available action), so its historical full `message` or
`banner` and `reason_diagnosis` have one prose source. The two evidence-keyed
reasons (`locate_failed`, `verify_inconclusive`) substitute the addressed
slot's paired pilot/reflection fact into that same seam. Exhaustion preserves
the resulting observation X, then replaces retry advice with the measured
count and exact outcome.

The unresolved wire payload carries diagnosis beside code. On the final group
index the relay's last-write-wins `capture_set_complete` repeats that payload,
so the page cannot lose it behind completion. Stage 1 renders the final spot
as left out before its Continue confirmation; stage 2 renders it as left out
instead of "All measurements done". Neither terminal nor unresolved surface
offers an unavailable retake. The stable
`event=correction.crossover_v2_position_attempts_spent` record carries
`observed`, `diagnosis`, `pilot_heard`, and `reflection_measured`, because a
settled accepted verdict deliberately has no final reason code. A cloud
position never reaches the cannot-continue branch while the group can still
be completed.

*Why this shape*: on 2026-08-03 two live sessions died at
`CaptureBeginRefused` raised before any audio played, while the household
screen read "step 6 … one last time" and the flow was on attempt 12 —
three reason codes each holding their own budget, a geometry discount
forgiving two more, and an accepted capture leaving the cumulative
counter standing (#2083 entries 4 and 6).

Key `event=` lines (via `jasper.log_event`):

```sh
# Conductor phase walk (the /correction/ wizard runs under jasper-correction-web).
# cloud_group_complete and cloud_spec both fire on EVERY close of a cloud
# group, including a retake's re-close (issue #1872) — seeing either twice
# for one phase is the retake contract working, not a bug. publish_cloud is
# the one part of a close that is a per-phase singleton: a successful
# publish is not journalled directly (see cloud_publish_failed for a failed
# attempt), but a SKIPPED one — a re-close whose phase already published —
# is, via cloud_publish_skipped, which is the line to grep for "the durable
# artifact now lags the candidate":
journalctl -u jasper-correction-web | grep -E 'event=correction\.crossover_v2_(authorized|play|result|apply|apply_complete|restored|cloud_group_complete|cloud_geometry_retry|cloud_spec|cloud_publish_skipped)'
# Session volume lifecycle (fail-closed). ``persist_failed`` is CRITICAL and
# means the durable intent could not be written — it belongs in any sweep of
# this family, not just the happy three:
journalctl -u jasper-correction-web | grep -E 'event=correction\.session_volume_(opened|restored|restore_failed|persist_failed)'
# Apply boundary (#1811): the declared level move, the proactive volume close
# when the auto-apply dies, and the CRITICAL line when that close could not be
# confirmed (a speaker possibly still at measurement volume — sweep for it):
journalctl -u jasper-correction-web | grep -E 'event=correction\.crossover_v2_(applied|volume_close_failed|volume_abandon_failed)'
# Calibration handoff / uncalibrated warnings:
journalctl -u jasper-correction-web | grep -E 'event=correction\.crossover_v2_(calibration_resolve_failed|uncalibrated_capture|default_calibration_hint_failed)'
# Accountability + delta probe (PR-L4/L5) — what accountability graded and
# banked, why a session refused (the delta probe half; accountability refuses
# nothing now), and what the speaker actually did with the correction:
journalctl -u jasper-correction-web | grep -E 'event=correction\.crossover_v2_(level_estimator_finding|level_match_finding|prediction_gate|realized_level_match|delta_probe|round_restore|round_recovery_required|delta_probe_restore)'
```

`event=correction.crossover_v2_level_estimator_finding` is the banked-and-
proceeded arm — the ONLY arm since #2609, because a disagreement no longer
refuses anything — and it is the one to grep for when a session COMPLETED but a
per-driver level estimate disagreed with the OTHER one, neither of them having
placed the pair (the raw per-branch trim did). It carries both estimators' own
readings (`trim_band_average_db` / `core_level_db`), their per-role distance
from **each other** (`estimator_delta_db`, i.e. |overlap-band placement −
core-median placement|), the largest of those (`estimator_worst_delta_db`)
against `estimator_tolerance_db`, and `realized_difference_db` (the outcome
check on the committed pair, which since the realized-level demotion banks into
this same record instead of refusing on its own — so the record is now built
when EITHER check fires, and its `reason` says which). The durable copy is the
bundle's `findings_measure.json` — one M7 finding, `confidence=unsure`, and a
`fix_class` that follows that `reason`: `refit` when the two estimators
disagreed (the error is upstream in the fit's own frame) and `eq` when only the
realized levels did (the frame is not in dispute; the committed pair is simply
not level). Written by `bind_findings_publisher` right after the candidate
artifact it cites. Two adjacent events say when that did NOT happen:
`…_level_frame_publish_failed` (the store refused; the tune is unaffected) and
`attribution.level_frame_promotion_refused` (the record could not become a
finding).

**The household is told** (2026-07-31, panel lens C CC1). The publisher reopens
that set through `read_finding_set` and projects its `household_copy` — the one
register a household may read, hardware-noun-free and slug-free by construction
— into the durable state, where the envelope's `findings` key renders it as one
quiet line on the **review** and **done** screens, dated once the record is
older than the freshness window. `event=…_findings_readback` reports the
projection; `…_findings_readback_failed` reports a set that could not be
confirmed (a citation the bundle can no longer produce), in which case nothing
is rendered rather than a diagnosis resting on nothing. Before this the ruling's
"bank a finding and proceed" was, in the household's experience, "proceed":
disclosure went from one sentence to none at the exact moment the flow began
proceeding on disputed evidence.

`event=correction.crossover_v2_linearization_giveback` carries the trim it
produced beside the consistency verdict on the two subordinate estimators
(`raw_trim_db`, `anchored_trim_db`, `normalize_shift_db`, `target_level_db`,
`level_estimator_suspect`, `level_estimator_worst_delta_db`) — a large gap
between the two estimators is the 10 dB-dark shape being CORRECTED, not a new
problem, and neither of them moved `anchored_trim_db`.

Two further events cover what the correction COSTS (#1808, #1809):

- `event=correction.crossover_v2_linearization_fit_band` — the band each
  driver was allowed to add level in, solved from its own crossover
  (`radiating_band_hz`, `crossover_order`). A boost outside it is the #1809
  defect. Cuts outside it are ordinary, but only out to half an octave past:
  since #2523 the whole solve runs over that widened band
  (`linearization_fit._solve_band_mask`), so the branch's deep stopband is
  excluded from the objective rather than corrected into. This event carries
  the LIFT band; the reported `fit_band_hz` on each role's fit is where the
  solve actually ran.
- `event=correction.crossover_v2_linearization_headroom` — per role,
  `chain_peak_db` (what the emitted `crossover ⊗ linearization ⊗ trim` chain
  actually puts above unity), `headroom_cost_db` (that peak plus the 1.0 dB
  `margin_db`, or 0.0 for a chain that never exceeds unity — this is what the
  emitter attenuates the program by), `trim_db`, and `sum_of_positives_db`
  (what the retired pre-#1808 rule WOULD have charged, kept so the reclaimed
  loudness is visible in the journal). A large gap between the last two is the
  correction spending nothing where it used to spend a lot.
- `event=correction.crossover_v2_linearization_no_crossover` (WARNING) — a
  role the session's preset carries no crossover region for. Its branch is
  treated as running full range: no lift bound and no headroom credit, which
  is what the emitter would build for it. On a 2-way conductor this is a
  defect in the supplied preset, which is why it is a warning rather than
  silence.

**Reading a `headroom_cost_db` from before 2026-07-28.** A candidate
persisted under the retired sum-of-positives rule discloses a much larger
number than re-emitting it would charge today — ~22.5 dB vs ~5 on the
2026-07-28 JTS3 profile. The stamp is deliberately not re-derived on load
(it records what that graph was emitted with); a recommission replaces it.

**Reading one from before the 2026-08-22 grid widening (#2758) — and the new
direction.** There are now THREE eras, named by the `headroom_cost_basis`
stamped beside the number
(`linearization_fit.HEADROOM_COST_BASIS_*`):

| basis | era | how it can be wrong today |
|---|---|---|
| absent → `unknown` | before #1808 | over-states, often by an order (sum of positive gains) |
| `realized_peak` | #1808 → #2758 | realized peak on a 20 Hz – 20 kHz grid |
| `realized_peak_full_domain` | #2758 onward | realized peak, whole evaluated domain |

The middle era is the one to read carefully, because it can be wrong in the
direction the earlier ones never were: its grid had no sample between 20 kHz
and Nyquist (or below 20 Hz), so a mixed-sign cascade peaking there was
under-read. A `realized_peak` stamp can therefore be **smaller** than
re-emitting the identical filters charges today — 1.8596 dB stamped against
7.8305 dB charged, on the cascade #2758 was filed for.

That matters for a reader, not only for an archivist. `sections_by_role`'s
docstring calls "a disclosure smaller than its own charge" the impossible
direction; that sentence is about the role → sections derivation it describes,
and is not a claim about a stamp read across this boundary. And the pairing is
reachable rather than theoretical: the republish path stamps
`headroom_cost_basis` unconditionally, so a candidate reopened after the deploy
carries a current-era basis beside per-branch numbers stamped under the old
grid. **Trust the basis, not the neighbourhood.**

**Migration.** A graph already on hardware re-proves against the allowance
baked into its own bytes, and the condition is the runtime's, not the margin
alone: `peak_new <= headroom_charge_db(peak_old) + 1e-3`. Two classes come out
of that:

- **The ordinary chain.** `headroom_charge_db` is peak + `HEADROOM_MARGIN_DB`,
  so it has the full 1.0 dB of room. Over a corpus of chains the pre-#2758 gate
  would have admitted, sampled at the fit engine's own per-filter rail, the
  worst move measures 0.3101 dB at the seed
  `test_a_graph_the_old_gate_accepted_still_proves_after_the_widening` pins.
  A more clustered population runs higher — all filters within ±5 % of one
  centre at the same rails reaches 0.3143 dB — and a wider search may find more,
  which is why the runtime guard below is the backstop and that corpus is
  evidence rather than a proof over the whole space.
- **The near-unity chain.** `headroom_charge_db` returns **0.0** at or under
  `_PEAK_EPS_DB` (0.01 dB) — a chain that never exceeded unity was charged
  nothing — so its tolerance is the 1e-3 float slack, not 1.0 dB. This class is
  MOST of that corpus (~80 % at the pinned seed; the trims put a majority under
  unity), and what it does is a separate fact: **no** member of it refuses at
  that seed, and the test caps refusals at 2 % rather than forbidding them,
  because at a 1e-3 tolerance a future sampler will find some.

Both refusals, and the two cascades whose peak lived in the old grid's hole,
land in the same place: the graph stops proving,
`safe_graph_for_current_topology` **refuses** rather than silently selecting the
all-muted startup graph (which would be a green deploy onto a silent speaker),
and the remedy is `jasper-active-speaker baseline-reemit` — not a recommission.
Having the deploy run that re-emit itself is issue #2847; until then a refused
box is left with its renderers parked, which is loud on purpose.

#### Per-capture diagnostics — every capture logs its numbers

Before this, `event=correction.crossover_v2_result` carried only
`accepted`/`code` — a failed hardware run left no numbers to look at, and
only a *glitch* MEASURE capture got a partial view via
`event=program_analysis.glitch` (WARN level, glitch captures only). That
separate event carries `epsilon_ppm`, `max_residual_samples`,
`repeat_level_delta_db`, `glitch_inputs`, `discontinuity_samples`, and
`discontinuity_after_segment`; the last three are not fields on the
per-capture event below. Its VERIFY twin is
`event=program_analysis.capture_integrity` (#1971) — same WARN level, same
failures-only cadence — carrying `failed`, `not_evaluated`,
`locate_confidence_min`, `schedule_residual_ms_worst`, and
`clipped_segments`.

`event=program_analysis.anchor` (#2093) sits one step upstream of both, and
unlike them it fires on EVERY analyzed capture (INFO; WARNING when it
corrects one) — because the anchor decision was the single unobservable step
in the chain, and a fabricated failure read exactly like a real one. It
carries `anchor` (the stimulus segment the whole timeline is pinned to),
`witness` (the independent segment that corroborated it), `presence` and
`runner_up_presence` (how present that witness was under each surviving
interpretation — the terms the choice is made on), `confidence` and
`runner_up` (their peakedness margins: `corroborated` grades the WINNER's
against the locate floor, and the ambiguity guard grades the runner-up's),
`corroborated`, `corrected`, `ambiguous`, and `shift_ms`. Read it FIRST when a capture
reports `locate_failed` or `summed_sweep_heard`: `corrected=true` means a
mis-anchor was caught and repaired, and a `confidence` near `runner_up` with
`corroborated=false` means the capture gave the analyzer nothing to pin to —
a genuinely unlocatable recording, not a knife edge. See "Timeline anchor"
below.

The verify diag below carries the same record on EVERY
capture, pass or fail, as `integrity` / `integrity_not_evaluated` /
`integrity_locate_confidence_min` / `integrity_residual_ms_worst`;
`integrity=unavailable` there is its own value and means the analysis
carried no record at all, never a clean capture. `CrossoverV2Conductor` now emits one
additional `log_event` per consumed capture, **on the accepted path AND
every rejection**, carrying that phase's full numeric diagnostics (pure
additive observability — none of these calls choose a verdict). The two
position groups emit `crossover_v2_cloud_diag`, which is 13 of the 16
captures in a Full-tier JOURNEY (4 of Express's 7) — both stages, since
the two-stage split put the two groups in different sessions — grep for
`check|measure|verify` alone and you see three:

```sh
journalctl -u jasper-correction-web | grep -E 'event=correction\.crossover_v2_(check|measure|verify|cloud)_diag'
```

- `correction.crossover_v2_check_diag` — `accepted`, `code`,
  `pilot_snr_ok`, `channel_map_min_isolation_db`,
  `channel_map_isolation_judged_above_db`, plus per-role
  (`woofer_`/`tweeter_`) `snr_db`, `captured_delta_db`,
  `programmed_delta_db`, `channel_map_target_rise_db`,
  `channel_map_cross_rise_db`, `channel_map_isolation_db`.
  **`channel_map_isolation_db` (`target_rise - cross_rise`) is the number the
  CROSS half of `channel_map_mismatch` is decided on** since 2026-08-21 —
  but only when that role's `channel_map_target_rise_db` is at or above
  `channel_map_isolation_judged_above_db`. **Read those two together or you
  will misread the line:** below the threshold an isolation figure is still
  published and still decided nothing, so a sub-bound number there is not the
  cause of a refusal. `channel_map_min_isolation_db` is the bound it was graded
  against. Both constants are printed rather than implied so a journal of old
  lines cannot be silently reinterpreted when either moves. The two raw rises
  stay published beside the ratio: only they say WHICH half moved, and they
  keep a sweep comparable across the metric switch. Household-facing copy stays
  number-free, so this line is the operator's record of a refusal.
- `correction.crossover_v2_measure_diag` — `session_id`, `accepted`, `code`,
  `alignment_confidence`, `alignment_confidence_source`,
  `alignment_seed_delay_us`, `alignment_refinement_delta_us`,
  `gate_window_ms`, `gate_floor_source` (WHY that window — a measured
  reflection onset vs the search ceiling with nothing found; both print the
  same window, issue #1966), `validity_floor_hz`,
  `epsilon_ppm`, `max_residual_samples`, `repeat_level_delta_db`,
  `woofer_repeat_epsilon_ppm`, `tweeter_repeat_epsilon_ppm`,
  `delay_us`, `delay_role`, `polarity`, `predicted_ripple_db`,
  `trim_ripple_gain_db`, `alignment_seed_ripple_db`,
  `flatness_improvement_db`, `anchor_delay_us`, `snap_delta_us`,
  `snap_found`, plus
  per-role `woofer_snr_db`/`woofer_snr_verdict`/`woofer_snr_band`/
  `tweeter_snr_db`/`tweeter_snr_verdict`/`tweeter_snr_band` (WHICH band
  produced that role's pair, `null` when the selected band carried no id —
  see `_driver_snr_fields` for why #2613 needed it), `sweep_residual_ms_worst`,
  `sweep_locate_confidence_min`, `guard`, `pilot_snr_ok`, and `pilot_snr_db`.
  (A `linearization` field rode this line until the
  2026-07-27 timing move. The fit now runs eight captures later, so this line
  could only ever have reported `""` — the field moved to
  `..._candidate_built` below rather than being kept as a permanently-empty
  one, the same treatment PR-5 gave the per-capture `flatness_*` fields.)
- `correction.crossover_v2_candidate_built` — once per built candidate, from
  whichever capture is the last before the apply: `candidate_fingerprint`,
  `linearization` (the outcome — fitted / trim_rejected / ineligible_* /
  fit_failed), `cloud_evidence` (did a cloud verdict reach the envelope),
  `excluded_bands`, `cloud_positions`. Absent entirely when a measurement was
  rejected — a session with no candidate makes no linearization claim.
  Its sibling `correction.crossover_v2_fit_without_cloud` (WARNING) names the
  honest degradation: a group whose pipeline never became available, so the
  fit ran with no cloud terms.
- `correction.crossover_v2_verify_diag` — `accepted`, `code`,
  `max_db_notch_excluded` (the number the tolerance actually gates on),
  `verify_tolerance_db`, `verify_gate_window_ms`, `verify_gate_floor_source`,
  `measure_gate_window_ms`
  (the comparability pair behind `verify_inconclusive`), `validity_floor_hz`,
  `tracking_band_lo_hz`/`tracking_band_hi_hz`, `rms_db`,
  and the rung-P1 frame disclosure — `frame_offset_db`,
  `frame_tilt_db_per_octave`, `rms_db_tilt_removed`, `max_db_tilt_removed`
  (all `None` on a verdict that reached no tracking comparison, and the last
  two also when no frame could be fitted; `max_db_tilt_removed` is the twin of
  `max_db_notch_excluded`, the number that gates). There is
  deliberately no `measure_gate_floor_source` beside `measure_gate_window_ms`:
  that window is RESTORED from persisted state on a resumed session and the
  floor source is not persisted, so the pair could only be reported as a real
  window beside a null source. MEASURE's own source is on its own diag line
  and in the retained sidecar.
- `correction.crossover_v2_cloud_spec` — the spec verdict, logged on every
  CLOSE of the group (not per capture within it — a retake's re-close logs
  it again, with the recomputed numbers; issue #1872): `phase`, `available`,
  `reason`, `spec_passed`,
  `spec_evaluable`, `flatness_max_db`, `flatness_max_hz`,
  `flatness_reference_band_lo_hz`/`flatness_reference_band_hi_hz` (the frame
  the deviation is stated against — a power mean over `REFERENCE_BAND_HZ`
  with its lower edge raised to the session's trusted floor, #2551, so this
  pair is read off the report rather than assumed to be the constant; the
  pointer moves in sign and frequency under a different frame, issue
  #1857), `flatness_bands` (issue #1857 — EVERY graded band's own deviation
  from that same reference, not just the one `flatness_max_db` picked, as one
  compact `lo-hiHz:+dev.ddB:pass|fail` token per band joined by `;`; a
  uniformly-off band can drag the shared reference toward itself and make an
  unrelated band's ordinary ripple read as the larger deviation, so a reader
  of this line is no longer limited to the single band the pointer flagged —
  see `crossover_v2_flow._per_band_flatness_log_field`'s docstring),
  `flatness_tilt` (issue #1857 — the one figure on this line the FRAME
  CANNOT MOVE: the largest level step between two graded bands, as
  `<step>dB:<lo>-<hi>Hz><lo>-<hi>Hz` with the higher-sitting band first. Every
  other flatness field here is a distance from the pooled reference, so a
  uniformly-off band drags them all; the reference cancels in a
  band-to-band subtraction, so this token reads the same under whichever
  anchor Q-E eventually picks. `""` when the gauge carried no tilt — an
  older persisted block, or fewer than two bands with a measured level. See
  `crossover_v2.verification._flatness_tilt_log_field` and
  `flat_spec.spec_band_tilt`), `flatness_rms_db`,
  `spec_n_excluded`, `validity_floor_hz`. Emitted from `_run_cloud_pipeline`,
  and since the flat-linearization plan's PR-5 it is the ONLY place a
  flatness number is logged (see "Flatness" above).
- `correction.crossover_v2_cloud_group_complete` — the group's geometry
  verdict, logged on every close of the group for the same reason as
  `cloud_spec` above (a re-close is a real close): `phase`, `positions`,
  `geometry_locked`, `geometry_reason`, `thin_evidence`, `geometry_retries`.
  The durable evidence-artifact PUBLISH behind both of these log lines
  (`publish_cloud`) is the one part of the close that IS a per-phase
  singleton — the evidence store is write-once, so a re-close's recomputed
  (and normally different) bytes would be refused if a second write were
  attempted; `_run_cloud_pipeline` skips that attempt outright once a phase
  has one successful publish recorded, rather than spend it on a call that
  cannot succeed.
- `correction.crossover_v2_cloud_publish_failed` (WARNING) — a publish
  ATTEMPT failed: `phase`, `exc_info` (a full disk, a write-once conflict
  against evidence this session did not write, or similar). Fires on EVERY
  failed attempt, not just the first — a failure does not mark
  `_group_cloud_published`, so the group's next close retries, and if the
  underlying problem persists (the disk is still full) the retry fails too
  and this logs again. That is the opposite reading from
  `cloud_group_complete`/`cloud_spec` above, where seeing either twice for
  one phase is the retake contract working as designed: a REPEATED
  `cloud_publish_failed` for one phase is not noise, it means the problem
  is still there. Fail-soft either way by design — the group's own accept
  is decided before this seam ever runs (see the S4/N1 review-finding
  comments on `_close_cloud_group`), so a publish failure never costs the
  accept.
- `correction.crossover_v2_cloud_publish_skipped` (INFO) — a re-close's
  publish was skipped outright, because an EARLIER close of this phase
  already published successfully: `phase`. This is the retake contract
  working as designed, not a failure — but it is the one fact nothing else
  in the journal states directly: everything above it in `_run_cloud_pipeline`
  (`_group_cloud_result`, the `cloud_spec` line) just recomputed fresh, so
  from this line on, the durable evidence artifact this phase already
  published LAGS that fresh result until the session ends. Without this
  line a reader can only infer the gap by counting `cloud_spec` lines
  against `cloud_publish_failed`'s absence.
- `correction.crossover_v2_cloud_pipeline_call_failed` (WARNING) — the
  broader case: any named-family exception anywhere in
  `_run_cloud_pipeline`, not just the publish seam. `phase`, `exc_info`.

Source: the `_log_check_diag` / `_log_measure_diag` / `_log_verify_diag`
methods on `CrossoverV2Conductor` in `crossover_v2_flow.py`, called from thin
`_consume_<phase>` wrappers around the unchanged `_<phase>_verdict` logic.
Two small threads-through landed alongside this so the numbers were actually
on the object: `program_analysis.DriftEstimate.repeat_level_delta_db` and
`PilotObservation.snr_db` / `.channel_map_target_rise_db` /
`.channel_map_cross_rise_db` (previously local variables inside
`_estimate_drift` / `_channel_map_ok`, logged transiently or not at all).

#### Timeline anchor — which stimulus the whole capture is pinned to

`program_analysis._global_offset` recovers ONE integer offset G for the whole
capture by locating a single stimulus; `_locate_segments` then searches every
other segment only at `scheduled ± SEGMENT_SEARCH_S` (±30 ms). That tight
window is deliberate — it is what stops a segment locking onto a different
occurrence of itself — but it means **the anchor is a single point of failure
for the entire timeline**: get it wrong by more than 30 ms and every segment
reads as "not found."

The anchor is located by `_earliest_strong_peak`, an energy-normalized
(therefore level-blind) matched filter that takes the earliest lag within
0.6× of its max, **scored inside the anchor stimulus's own declared
`(f1_hz, f2_hz)` band since #2644** (below). Level-blindness is the right call
for MEASURE's bit-identical sweep repeats — equal-level siblings score alike,
so "earliest" robustly picks the first. It is a **knife edge** for the v2
programs' leading pilot pair, whose lo member is deliberately the quietest
thing in the program (VERIFY's `pilot_summed_lo`, 10 dB under
`pilot_summed_hi`): whether the quiet pilot clears 0.6× is then decided by its
local SNR rather than by its waveform.

That fired in production on 2026-08-03 (#2093). Across 11 live cloud VERIFY
captures of one speaker, eight cleared the gate by +0.019…+0.185 NCC and
three missed it by −0.0048…−0.0490 — nearest pass and nearest fail 0.024
apart. On all three the anchor snapped to `pilot_summed_hi`, shifting the
timeline by exactly the pilot spacing (+1296.5 ms, identical on all three),
the summed sweep then located at 0.019–0.097, `summed_sweep_heard` failed,
and households were told "Couldn't hear the speaker clearly. Check the
volume…" about pristine recordings. Re-anchored correctly those same sweeps
score 0.7313 / 0.8202 / 0.6671 — squarely among the eight that passed. The
audio never distinguished pass from fail; the anchor did.

So `_resolve_anchor` no longer trusts that one gate. Two shapes are
identical to a level-blind correlator exactly when they share
`(f1_hz, f2_hz, n_samples)` — the stimuli then differ by one scalar gain, so
the NCC curve *cannot* tell them apart even in principle. That makes the
ambiguity set provable rather than guessed. Each shape-sibling of the first
stimulus is tried as an interpretation of the located arrival, and each
resulting timeline is scored by locating an independent **witness** (the
longest stimulus whose shape is NOT in that set — for VERIFY, the 6 s summed
sweep) through the very same `_locate_in_window` the downstream locate uses.
The winner is the timeline the rest of the program agrees with.

Two properties this deliberately keeps:

- **It cannot manufacture a pass.** Re-anchoring changes only WHERE the
  analyzer looks. The confidence reported is still the real measured
  correlation at the chosen place, and `SWEEP_LOCATE_CONFIDENCE_FLOOR`, the
  residual ceiling, and the linearity gates are untouched.
- **It re-anchors only on positive evidence.** The winning candidate's
  witness must itself clear that same "was this even heard" floor. When the
  witness never played — a silent driver in CHECK, or a capture with no
  program in it at all — every candidate scores in the noise, the cross-check
  declines to move, and the capture fails exactly as it did before. A
  garbage capture must still fail honestly; that is pinned by
  `tests/test_audio_measurement_anchor_resolution.py`, which also keeps a
  permanent mutation guard recomputing the pre-#2093 offset and asserting
  THAT timeline collapses.

Programs with nothing to arbitrate (a legacy pilot-less VERIFY, whose unique
summed sweep is the anchor) take the `_resolve_anchor` early-out unchanged and
log nothing. Their *locate* is not byte-identical to the pre-#2093 path any
more — #2644 band-limits it for **every** program, and a pilot-less VERIFY's
anchor declares 150 Hz–20 kHz, so `_bandlimit` runs during its locate too. No
outcome can differ (a single-occurrence stimulus has no rival interpretation to
move to), but the arithmetic is not the same arithmetic.

**#2644 — the witness discriminates in only one direction, and the score was
measuring the wrong thing.** 2026-08-16's round-3 CHECK hit both gaps at once
on a correctly-wired speaker that had passed the identical program two hours
earlier, and the verdict it produced was `channel_map_mismatch` — a hard stop
telling the household to check its wiring.

- **The score.** The NCC denominator was the capture's TOTAL local energy, so
  room noise the pilot never occupied suppressed it: the quiet
  `pilot_woofer_lo` scored 0.3932 against a 0.4176 gate (missed by 0.024)
  while its IN-BAND SNR, 27.6 dB, was *better* than the accepted round's
  26.9 dB. Both locates (the `LOCATOR_RATE_HZ` coarse pass and the full-rate
  refinement) now band-limit capture and stimulus to the anchor segment's own
  declared band before correlating — the same `f1_hz`/`f2_hz` `_channel_map_ok`
  and `_band_power` read, so there is no second statement of what a pilot
  contains. A caller with no band, or a band that survives no FFT bin at that
  rate, keeps the full-band behaviour exactly.
- **The witness.** `_resolve_anchor`'s witness must not be confusable with
  ITSELF under the shift being arbitrated, and the longest-then-**earliest**
  witness rule buys exactly one of the two shift directions — nothing of the
  same shape sits one gap BEFORE a pair's `lo` member. One gap AFTER, CHECK's
  chosen witness `pilot_tweeter_lo` has its own twin `pilot_tweeter_hi`, which
  is where a mis-located arrival puts the rival windows. Both readings then
  land the witness on a real pilot and score alike — the fixture reconstruction
  separates such a pair by **3.5x**. No witness CHOICE fixes it — CHECK's only
  non-sibling stimuli are the other role's pair, and both members are twins.

So a third property joins the two above: **it says when it cannot tell.** Two
candidates both clearing `SWEEP_LOCATE_CONFIDENCE_FLOOR` whose witness presence
is within `ANCHOR_DISCRIMINATION_RATIO` (50x) leave the committed anchor exactly
where it was — a near-tie is not a reason to pick the other one — and set
`ProgramAnalysis.anchor_ambiguous`, which CHECK's ladder reads at a rung
ABOVE the channel map and refuses as the retriable `anchor_ambiguous`.

**#2644 fixed the locate; 2026-08-21 found that the arbitration ABOVE it was
scoring the wrong quantity.** A jts3 per-driver MEASURE round failed 3/3 with
`drift_baselines_disagree` (guard `sweep_schedule`), logging
`anchor=pilot_woofer_hi confidence=0.7386 runner_up=0.7310 corrected=true
ambiguous=true shift_ms=-1309.9` — a whole pilot spacing moved on a 0.0076
lead. `_locate_in_window` was returning only `AlignmentResult.confidence`, the
peakedness margin `(peak - secondary) / peak`. Over the ~61 ms of lags a
per-segment search window spans, room noise correlated against a 4 s sweep is
as sharply peaked as the sweep itself, so that margin **cannot tell an empty
window from an occupied one**: the winning hypothesis's `sweep_w` window held
nothing but guard silence and still scored 0.7386. Every sweep then located
29.92 ms off its schedule against a 5.0 ms ceiling, and the household was
refused three times about a recording whose own woofer sweep measures 46.1 dB
SNR.

The seam now returns **both** of the aligner's scores. Candidates are ranked on
`presence` — `AlignmentResult.peak`, the normalized correlation similarity,
which does answer "is the witness here": the same two windows read **0.5394**
and **0.0025**. `confidence` still grades `corroborated` (is this a locate at
all, on the quantity that floor was calibrated for), and on the garbage-capture
fixture it is the *only* thing that stops a move — the two are not redundant.

**The discrimination guard is a RATIO because the ABSOLUTE scale is unusable —
not because the ratio is invariant.** Presence is normalized by its own
window's energy, so a correctly-anchored capture reads 0.3572 in a quiet room
and 0.0018 in a loud one across the fixture's 0.003–0.65 room ramp: no absolute
number sits above the quiet end and below the loud one. Comparing the two
readings cancels most of that level term but not all of it — they are different
slices seconds apart, whose norms measure 13.3% apart on both 2026-08-21
captures. What justifies 50 is the measured population gap, which stands on its
own: cannot-discriminate 1.07 / 3.50–3.51 / 12.4 against resolved 197–11500
(ramp), 214.17 and 404.40 (the two real jts3 captures), 61857 (VERIFY) — 50 is
the round number nearest its geometric centre, √(12.4×197) ≈ 49.4. Only the
TWIN side is flat across room level; the resolved side spans 58× over the same
ramp and a low-SNR capture can walk it below 50, which is a known limit rather
than a refutation (see the defence in depth below). The full derivation lives
at `ANCHOR_DISCRIMINATION_RATIO` in `program_analysis.py`, and
`test_the_ratio_brackets_the_two_measured_populations`,
`test_the_separation_is_flat_across_room_level`, and
`test_the_guard_compares_a_ratio_and_not_a_difference` are where the claims are
pinned. It remains a floor sanity check on the constant, not a claim that the
populations are clean everywhere.

Defence in depth still holds underneath it and is still worth knowing: the room
level that degrades the anchor independently fails the rungs below. #2644's
panel built ~160 flip-controlled captures and found ZERO rows where removing
only the mis-lock removed the wiring hard stop; the repo fixture's own room ramp
is already past the gain solve's `snr_floor_ok` one step BEFORE its first
mis-lock. `test_a_mislocking_room_has_already_failed_a_rung_below` is where that
claim lives.

Read `ambiguous=` on `event=program_analysis.anchor` when triaging one — the
line also names the losing interpretation (`runner_up_anchor=`,
`runner_up_shift_ms=`, on the same baseline as `shift_ms`) so the alternative
timeline does not have to be re-derived from the banked WAVs. `presence=` and
`runner_up_presence=` are the terms the choice was made on; `confidence=` and
`runner_up=` keep their old meaning, so on a POST-fix line `runner_up=`
exceeding `confidence=` is the 2026-08-21 shape itself — the margin preferred a
window the presence refused. It cannot appear on a line banked BEFORE the fix:
the old ranker sorted on `confidence`, so `runner_up <= confidence` was an
invariant there, and the incident's own line above shows 0.7386 against 0.7310.
The pre-fix signature to grep for instead is `corrected=true` with a
`shift_ms=` equal to one pilot spacing. A field population of near-ties is how
the constant gets re-derived.

**Design note, not yet a change:** the same quietest-first pilot design sits
close to its own detection floor at real household volumes — 2026-08-03's
fourth failure was a `pilot_level_collapse` at 11.27 dB. The anchor no longer
depends on the quiet pilot, but the linearity evidence still does. Worth
revisiting the pilot level plan on its own merits.

#### Operator capture retention — raw WAVs for offline analysis

Off by default. An operator debugging a hardware failure creates the marker
file, and every subsequent capture's raw WAV + a diagnostic sidecar lands on
disk for offline analysis (this productizes a hot-patch that used to live
directly in `bind_production_analyze._analyze` and kept getting silently
wiped by every deploy — runtime Python is copied fresh from the rsync
checkout into `/opt/jasper`, see AGENTS.md "Runtime Python lives in
/opt/jasper").

```sh
# Enable — creates the dir + marker; next capture onward is retained:
ssh pi@jts.local 'sudo mkdir -p /var/lib/jasper/xover-capture-dump && \
  sudo touch /var/lib/jasper/xover-capture-dump/ENABLED'

# Inspect what landed:
ssh pi@jts.local 'ls -la /var/lib/jasper/xover-capture-dump/'
scp 'pi@jts.local:/var/lib/jasper/xover-capture-dump/*' ./captures/

# Disable — delete the marker (or the whole directory); the very next
# capture goes back to zero retention behavior, no restart needed:
ssh pi@jts.local 'sudo rm -f /var/lib/jasper/xover-capture-dump/ENABLED'
```

Each retained capture is two files, `<timestamp>_<phase>_<device>.wav` +
`<timestamp>_<phase>_<device>.json`. The JSON sidecar carries `phase`,
`device_label`, `wav_bytes`, `wav_sha256_12`, `setup_mode`,
`setup_calibration_id`, and `diagnostic` — the same
`program_analysis.analysis_diagnostic_summary(analysis)` numbers as the
per-capture diag events above (keyed by each response's own role string
rather than a hardcoded woofer/tweeter label, since this runs at the
`analyze` seam, before the conductor's role mapping exists — so it has no
`accepted`/`code`, only the analysis's own numbers). `phase` (filename and
sidecar) is the FLOW's own phase — `check`/`measure`/`verify`/
`cloud_measure`/`cloud_verify` all appear post-#1855 — never the underlying
`ExcitationProgram.phase`, which is only ever `check`/`measure`/`verify`
since every cloud position plays the verify-shaped summed sweep.

##### `provenance` — the config label is not the graph

The sidecar also carries a `provenance` block: `main_volume_db`,
`session_volume_db`, `graph` (`kind` / `config_path` / `fingerprint`) and
`stimulus` (`program_id` / `phase` / `wav_sha256` / `peak_dbfs`). Each field
is read from exactly one LIVE owner at the moment the stimulus is emitted —
the fader from `CamillaController.get_volume_db`, the held volume from
`SessionVolumePlan.measurement_volume_db`, the running graph from
`get_config_file_path` + `running_graph_fingerprint(get_active_config_raw())`,
the stimulus from the `ExcitationProgram` and its published WAV artifact.
Owner and code: `jasper/active_speaker/capture_provenance.py`.

**Why `graph.kind` exists.** A CHECK/MEASURE program plays through the
transient per-driver ROUTING graph (`emit_active_speaker_program_config` —
each program channel straight to its driver's physical output, with the
crossover, delays and linearization left OUT), loaded inline with `SetConfig`
and restored after. That loader deliberately never repoints the statefile, so
`config_path` reads the SAME durable anchor whether a capture went through the
routing graph or through the standing applied one — two radically different
transfer functions. On 2026-08-19 a jts3 forensic session spent hours comparing
levels across those two graphs with nothing on disk able to tell them apart,
and reconstructed the night's −27.5 dB fader out of the journal because no
capture recorded it either. (That session reported a +7…+15 dB per-branch
difference — its observation, attributed, not a measured property of the code.) `kind` is therefore
stated by the playback branch that did (or did not) perform the swap —
`program_routing` or `applied` — never re-inferred from a path; `fingerprint`
is the running graph's own hash, so the two corroborate. `config_path` is
recorded precisely BECAUSE it is the misleading label: side by side with the
other two it shows what the statefile claimed versus what was playing.

**Two key names now appear twice in one sidecar, meaning different things —
read the path, not the leaf.** `phase` at the top level (and in the filename)
is the FLOW's phase, while `stimulus.phase` and `diagnostic.phase` are the
`ExcitationProgram`'s: those DISAGREE by design on every cloud capture, since
every cloud position plays the verify-shaped summed sweep — which is exactly
the #1855 inference that once mislabeled 32 of 45 sidecars, so do not read a
`stimulus.phase` of `verify` as "this was a VERIFY". `wav_sha256` at the top
level is the digest of the CAPTURE this ring retained (the corpus join key);
`stimulus.wav_sha256` is the digest of the program WAV that was PLAYED. They
are different files and differ on every sidecar.

**Do not join `graph.fingerprint` to a round receipt's
`entry_graph_fingerprint`.** They are different namespaces answering different
questions: this one is `running_graph_fingerprint` over CamillaDSP's own
re-serialization of the graph that was playing, while the receipt's is the
applied Layer-A profile record's `candidate_fingerprint`
(`_active_graph_fingerprint`). Sidecar fingerprints compare to each other —
same graph or not — and to nothing else.

Every field except `kind` can read `null` — `kind` is structural knowledge the
branch holds, not a probe that can fail. An unreadable surface nulls only its
own field and contributes its name to one WARN
`event=active_speaker.capture_provenance result=incomplete unreadable=…`;
provenance never blocks or fails a capture. `session_volume_db` is `null`
whenever no measurement session is open, which is an answer rather than a
failed read, and does not appear in that WARN.

That event has a **second** WARN outcome since #2925: `result=volume_disagreement`,
when a record's own `main_volume_db` and `session_volume_db` disagree past the
shared readback tolerance. It is the tripwire for the defect those two fields
printed from the first capture of the 2026-08-24 campaign and nobody compared
— and it is a disclosure, not a gate, because nothing in this module may break
a capture. The fail-CLOSED half runs earlier, in the play path's own fader
hold; see `measurement_volume_drift` in the reason-code table above.

Observation is bought only while the enable marker exists
(`capture_dump_enabled()` is the one reader of it): with retention off — every
household — the play path spends one `Path.exists()` and no provenance
round-trips. It is NOT free of CamillaDSP round-trips: since #2925 every
capture, retained or not, also reads the fader once to prove the declared
measurement volume (and writes once, only when it has drifted — which since
#2929 is no longer the routine case). That read is the safety ledger's, not
the forensic record's, which is why retention does not gate it, and it is also
why the hold's own `result=held` line — not the provenance record — is the
liveness half of the acceptance criterion: provenance is retention-gated and
therefore absent on the household runs the criterion has to be readable
against. The block is absent, not `null`, for a capture no play was
observed for; the recorder's `take()` consumes, so a second analyze with no
play between it and the last one reports nothing rather than the previous
capture's context.

Ring-buffered by **both** file count (`XOVER_CAPTURE_DUMP_MAX_FILES = 90`)
and total bytes (`XOVER_CAPTURE_DUMP_MAX_BYTES = 300 MB`), oldest-first
deletion, so a forgotten marker cannot fill the SD card. The enable marker
itself (`XOVER_CAPTURE_DUMP_ENABLED_MARKER`) is excluded from both caps and
never a prune candidate — without that, the ring buffer would eventually
delete its own on/off switch (it's typically the oldest file in the
directory) and silently re-disable retention. Because the intended operator
workflow is `ls`/`scp`/`rm`-ing this directory *while captures keep
landing*, a file can legitimately vanish between one step of a prune pass
and the next; every `.stat()`/`.unlink()` in `_prune_capture_dump` is
individually guarded (skip a vanished file, don't fail the pass), and the
whole prune body is additionally wrapped so any other `OSError` still
degrades to a WARN instead of propagating — genuinely never-raise, not
merely best-effort by convention. A write OR prune failure is caught and
logged at `event=correction.crossover_v2_capture_retain_failed` (WARN) and
never affects the measurement itself; a successful retain logs
`event=correction.crossover_v2_capture_retained` (`phase`, `bytes`, `path`).
Diagnostic-logging failures (Part 1) are guarded the same way, through
`CrossoverV2Conductor._safe_log_diag` — a bug in a `_log_*_diag` method logs
`event=correction.crossover_v2_diag_log_failed` (WARN) instead of crashing
the capture or changing the verdict already decided.
Source: `_maybe_retain_capture` / `_prune_capture_dump` /
`capture_dump_enabled` in `jasper/web/correction_crossover_v2.py`; constants
`XOVER_CAPTURE_DUMP_DIR` / `_MAX_FILES` / `_MAX_BYTES` at the top of that
module. The `provenance` block's own owner is
`jasper/active_speaker/capture_provenance.py`, recorded from
`bind_production_play` and retained by `bind_production_analyze`; tests in
`tests/test_capture_provenance.py`.

Session state on the Pi (both mode 0640, atomic writes):

- **Conductor/flow state:**
  `/var/lib/jasper/active_speaker_crossover_v2_state.json` — phase,
  candidate, verify, failure, `apply_blocked`, `pre_apply_profile`,
  `applied`, evidence refs, `session_id`. Threaded into the envelope as
  `status["crossover_v2"]`. The `failure` record carries `code`, its own
  `at` stamp (see "a failure screen has a lifetime" above), and — for the
  program family — `refusals`.
- **Session volume state:**
  `/var/lib/jasper/active_speaker_crossover_session_volume.json` —
  `status`, `opened_at`, `measurement_volume_db`,
  `original_main_volume_db`. A missing/malformed file hydrates
  fail-closed.

Endpoints (POST, dispatched from `correction_setup`):
`/correction/crossover/v2/session`, `/apply`, `/verify`, `/restore`,
and the shared `/correction/crossover/recover-volume`.

### Hardware benchmarks (campaign results, 2026-07-18/19, JTS3 + UMIK-2)

Attributed as campaign measurements, not code guarantees:

- **Start → applied crossover: 75 s** (run 7, scripted full pass, 2026-07-18).
- **ε (clock drift):** ≈30 ppm, repeatable 29.90–30.02 ppm across runs
  (0.68 µs equivalent delay repeat), agreeing with an independent bench
  probe to 0.1 ppm. Uncorrected, the same rig would accumulate
  ~200–300 µs across a program — why the repeat is mandatory.
- **Trim repeatability:** 0.02 dB. First calibrated run applied a
  tweeter trim of **−16.41 dB**, with the calibration id resolved and
  applied across all three phases (recorded under `evidence.calibration`
  in the v2 state file).
- **Failure honesty verified:** a deliberately bad desk placement gave a
  0.667 ms gate window → 1500 Hz validity floor, and the flow returned
  `verify_inconclusive` rather than a false pass — the design working as
  intended.
- Reference drivers: Dayton Epique E150HE-44 woofer (~83.3 dB) + B&C
  DE250-8 compression tweeter (~108.5 dB), LR4 @ 2000 Hz — a 25.2 dB
  sensitivity spread that drove the W6.5 sensitivity-derived ceiling
  ruling.

Analysis tuning constants live at the top of `program_analysis.py`
(linearity `LINEARITY_TOLERANCE_DB`, repeat `REPEAT_LEVEL_TOLERANCE_DB`,
channel-map `CHANNEL_MAP_TARGET_RISE_DB`/`CHANNEL_MAP_MIN_ISOLATION_DB`
(with the derived `CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB`),
alignment `DEFAULT_ALIGN_SEARCH_MS`/`GCC_UPSAMPLE`, VERIFY
`VERIFY_NOTCH_EXCLUSION_DB`) and `crossover_v2_flow.py`
(`VERIFY_TOLERANCE_DB`, `MEASUREMENT_DISTANCE_M`). All are **PROVISIONAL**
pending broader ~1 m runs — a constants-tuning pass is owed (Future work).

The GCC alignment band, flatness-delay objective, trim solve, predicted
ripple, and VERIFY-tracking band are all clamped to the true driver-sweep
overlap —
`[max(Fc/2, tweeter_sweep_lo), min(2·Fc, woofer_sweep_hi)]` — rather than
trusting the nominal `Fc ± 1 octave` span, since a driver's MEASURE sweep
only ever excites its own declared band (e.g. a tweeter sweep starting AT
Fc leaves `[Fc/2, Fc)` as pure deconvolution noise for that branch). One
SSOT helper, `overlap_band_hz` in `program_analysis.py`, computes the
clamp; every consumer reads the real sweep bounds off the program's own
segments rather than re-deriving the nominal edges.

**The per-driver capture-SNR verdict takes the same clamp, per branch**
(`branch_snr_band_hz`, #2613). `_driver_snr_block` used to hand
`band_snr_verdicts` the bare nominal window, and that window enfranchises any
`CROSSOVER_SNR_BANDS_HZ` row it merely *overlaps*. On the geometry of the
2026-08-15/16 rounds above — Fc 1648.7, tweeter declared `[1600, 20000]` —
the nominal window is `[824.35, 3297.4]`, so the `transition` row `[350, 1000]`
was enfranchised (1000 > 824.35) into 650 Hz the tweeter sweep never enters.
That row read the room against itself and verdicted `insufficient` on
arithmetic no room and no drive level could change — firing the declared-design
commitment below on 14 of 14 rounds. The **woofer is the geometric control**:
its `[150, 4000]` sweep spans the whole window, no row is empty for it, the
clamp is a no-op, and it passed on those same captures — an asymmetry a
broadband noise floor cannot produce.

**What is hardware and what is replay, kept apart.** The box recorded only the
worst-relevant SCALAR per branch — tweeter **−1.2 dB / `insufficient`**, woofer
**44.0 dB / `ok`** — because `_driver_snr_fields` drops `worst_relevant`'s
`band_id` before logging. So no jts3 artifact records a per-row SNR, and WHICH
row produced either number is derived from the geometry above, not read off a
log. The per-row figures (`transition` −2.6 dB, `mid` **66.4 dB**) come from
replaying that geometry through the analyzer on synthetic IRs, and 66.4 dB is
the margin the residual below is judged against. Persisting `band_id` would
make the next such argument readable rather than re-derived.

The clamp is stated for one branch that does not know its role (whichever edge
binds, binds) and applies to BOTH decision classes; the 35 dB law and the
fail-safe are untouched — only the window they read. Named residual, always
erring toward refusal: a row the window keeps can still be wider than the
sweep's coverage of it, understating SNR by
`10*log10(row_width / covered_width)` — 0.97 dB for jts3's tweeter and 0.00 dB
for its woofer, but **not** bounded by a small constant, since it grows as the
sweep edge lands deeper inside a wide row (14.77 dB for a woofer ceasing just
above `mid`'s 1000 Hz floor). It is tolerable because of where the margin sits,
not because the number is small. See the function's docstring for what removing
it would cost.

#### (Polarity, delay) selection — one objective, correlation as seed

**The pair is chosen jointly, on predicted summed blend flatness (issue #2598,
2026-08-16).** `_select_alignment_pair` in `program_analysis.py` scores both
polarities across a delay grid — `ALIGNMENT_FLATNESS_STEP_US` steps spanning
±`ALIGNMENT_FLATNESS_SPAN_PERIODS` period at Fc around the physical peak-gap
anchor, intersected with the declared `delay_range_ms` — and commits the
flattest pair. Correlation (the GCC-PHAT polarity sign and the gated local-peak
snap below) supplies the SEED pair and the tie-break; it no longer decides.

Why it changed: polarity was the sign of a GCC-PHAT correlation peak and the
delay was a correlation snap — two correlation answers, neither asking whether
the pair SUMS FLAT. On the 2026-08-15/16 jts3 rounds that shipped an LR4 at
1648.7 Hz with the tweeter inverted, from a correlation peak read at −292 µs
while the applied delay was +96 µs, on a tweeter IR the capture's own SNR
policy called `insufficient` (−1.2 dB). An inverted Linkwitz-Riley pair
commands a null; three rounds of trim and linearization then fitted around a
dip the alignment had ordered. The flat-sum discriminator existed
(`_flatter_sum_polarity`) and was computed and discarded every round.

**What survives from the 2026-07-22 methodology decision**
([crossover-measurement-reproducibility-plan.md](crossover-measurement-reproducibility-plan.md)
§10), which retired an earlier flatness selector because its basin ordering was
capture-noise dependent and preferred a wrong comb lobe on a hardware repeat:

- The **anchor still centres the search**, so lobe selection stays physically
  anchored rather than starting from a correlation peak.
- The **flat-minimum regularization** (`ALIGNMENT_FLAT_MINIMUM_EPSILON_DB`,
  0.25 dB — the same shape and value the ripple-optimal trim search uses)
  keeps the SEED pair whenever nothing beats it by more than epsilon. A capture
  where flatness cannot separate the answers is therefore byte-identical to the
  pre-#2598 path.
- What is genuinely reopened: the grid reaches one period either side of the
  anchor, so a committed delay CAN cross a lobe boundary (half a period at Fc)
  when the objective is that sure. **The #2607 adversarial panel split on this
  and the conductor ruled: KEEP ±1 period — declined on cost/benefit, NOT
  evaluated and found harmful.** The safety lens's Monte Carlo measured
  narrowing as a ~3% mitigation of a ~30% in-lobe ambiguity problem: about 90%
  of the competing answers already sit INSIDE the anchor's own lobe (p50
  209 µs against a 303 µs half-period), so a narrower span leaves the great
  majority of them reachable and buys little. It also found the wider span
  reaches genuinely different candidates — the redundancy hypothesis was tested
  and refuted. The correctness lens's stated fallback, raise the disclosure,
  was adopted alongside it. *(An earlier revision of this paragraph added
  "sometimes worsens left-lobe rates" to that list. The lens never claimed it —
  it was the conductor's misreading — and a follow-up 3,000-trial run across 10
  configurations measured it FALSE: zero instances, narrowing is strictly
  better on left-lobe rate. Deleted, because a ruling propped up by a made-up
  harm is a ruling nobody can revisit honestly. The two reasons above are
  genuine and sufficient on their own.)* So a lobe-leaving commitment is carried on the
  CANDIDATE (`left_anchor_lobe`, into `analysis_json`, the retention sidecar
  and `measure_diag`) and raises the selection log to WARNING. Candidate-level
  and not journal-only because the mode is magnitude-flat and time-wrong: an
  on-axis VERIFY cannot contradict it, so the receipt is the only place a later
  reader finds it. The first post-fix round's ±22° verify positions are the
  deliberate off-axis check.
- The objective also differs from the retired one in scope: it is the ripple of
  the same full overlap-band summed model `predicted_ripple_db` reads, not a
  narrowband search.

**The frame is gated on the aligner's STATUS, not on declared bounds.**
`anchor_delay_us` — the reference the objective's residual is measured against
— follows the same gate the shipped model uses, because they must be the same
frame or the objective grades a curve nothing emits. Requiring declared bounds
here (as the first cut did) meant a preset with no `delay_range_ms` scored
every pair at residual 0 while the emitted model carried `committed − anchor`:
a constructed case took a 20.37 dB penalty and chose its polarity at a residual
the speaker never runs (#2607 C1). The delay ESTIMATOR question is separate and
still bounds-gated: with no declared bounds the SEED stays the bare GCC
estimate, exactly as before.

**The SNR precondition — on the alignment law, not the magnitude one.** When a
branch feeding the alignment carries an `insufficient` verdict from the
**ALIGNMENT decision class** (`snr_policy.DECISION_CLASS_ALIGNMENT`:
`DRIVER.alignment_snr_ok_db` = 35 dB, ok-or-insufficient with no `reduced`
rung, per-band evidence required), the pair is not searched. It is COMMITTED to
the declared design — relative polarity `+1`, which in the configured-polarity
frame the branch TFs already carry means "the polarity the preset declares",
the same target VERIFY grades against — at a delay this capture did not supply.
Disclosed, never a hard stop: the household still gets an alignment, the
declined seed is still scored (so `flatness_improvement_db` goes honestly
negative), and the selection log line is a WARNING.

> **Superseded on the DELAY half by issue #2617 (2026-08-16), after this
> entry was written.** It used to commit the physical anchor here — a fresh
> number off the capture the verdict had just refused, which is exactly the
> quantity a low-SNR capture gets wrong (#2611: six of nine jts3 positions
> read ≈ −211 µs against a true +59.6). It now holds the delay the applied
> graph already carries, or commits none when there is nothing to hold or
> nothing readable, and those are three different `alignment_objective`
> values. The summed model that round ships also stops carrying
> `committed − anchor`, so an anchor the capture disowned cannot refuse a
> candidate through the accountability gate or fail a round through VERIFY
> tracking — while every MEASURED-vs-spec verdict stays live. The contract,
> the priors seam it arrives through, and the disclosure are **code-owned**:
> read `_select_alignment_pair`'s and `summed_model_residual_delay_us`'s
> docstrings and `MeasurementPriors.applied_alignment` in
> [`program_analysis.py`](../../jasper/audio_measurement/program_analysis.py),
> not a restatement here.

`_driver_snr_block` computes BOTH classes off one set of band measurements and
files the alignment one under `DRIVER_SNR_ALIGNMENT_KEY`; the magnitude verdict
keeps the block's top level and every existing surface reads it unchanged. The
readers are named apart: `driver_snr_verdict` (magnitude, for display) and
`driver_alignment_snr_verdict` (the refusal). Reading the magnitude verdict for
this decision left a **15 dB window** — 20 to 35 dB — in which a polarity read
off a capture the repo's own law calls unusable shipped unrefused (#2607 S1).
`unknown`/absent still does NOT refuse: it means the verdict was never
computed, ordinary for a session whose CHECK carried no ambient window.

**What the candidate records.** `alignment_objective` (one of
`ALIGNMENT_COMMITMENTS` — the set is code-owned and gained
`applied_alignment_held_after_low_snr` in #2617; read the constants block in
[`program_analysis.py`](../../jasper/audio_measurement/program_analysis.py) rather
than this list), `seed_polarity_sign` and
`left_anchor_lobe`, plus `AlignmentEstimate.polarity_agrees_with_sum` — the
cross-check that had no production reader before #2598 and now travels in
`analysis_json`, the retention sidecar's `analysis_diagnostic_summary`, and
`correction.crossover_v2_measure_diag`. A disagreement between correlation and
the objective is ordinary operation, not a fault. `polarity_agrees_with_sum` is
`None` — not `False` — on any commitment the flat-sum objective did not make on
the polarity axis (the low-SNR path, both seed fallbacks, and — since the
`alignment_prescription` basin pin — any round whose polarity axis the request
held to one sign): reporting disagreement for a comparison that never ran is
the same dishonesty this issue is about. That last one is why the selection
carries `polarity_pinned` at all: a pinned round still commits
`explicit_prescription_committed`, so the objective alone cannot say whether
the question was put.

**The household surface.** The objective reaches the review screen through
`_candidate_summary` → `_candidate_review_payload` → the wizard's
`renderCandidateReview`, which words a declared-design commitment as *"As
designed — this measurement could not check it"* rather than *"Inverted
(measured)"*. "Measured" is the one word a household reads as "we checked", and
on that path nothing checked (#2607 S3).

`polarity_pinned` rides that same path and is checked **first**, wording a
pinned round as *"Inverted (pinned for this round)"*. It is a second fact
rather than a fifth objective because the objective genuinely cannot carry it —
a pinned round commits the same `explicit_prescription_committed` an unpinned
prescription does — and because membership in the declared-design set also
governs the anchor withdrawal, which a pinned round does not get. The two
overlap on one arm (a pinned prescription on a refused capture), and the pin
wins there because the pin is what shipped. The list-comparison guard cannot
see a payload key, so
`test_the_browser_and_python_agree_on_the_pinned_polarity_key` guards this one.

`_estimate_alignment` remains the coarse, drift-corrected GCC-PHAT source for
the seed polarity and capture-quality confidence, and computes the fine stage.
Two steps produce the SEED delay:

1. **Anchor (primary value; owns lobe selection).** The drift-corrected
   physical peak gap `(argmax|tweeter IR| − argmax|woofer IR|)/fs` with the
   inter-sweep clock term removed, plus declared parallax, in
   `AlignmentEstimate`'s signed frame. The anchor is non-periodic, so it selects
   the comb lobe outright — it cannot land on a neighbouring lobe the way GCC's
   periodic correlation peak can.
2. **Gated local-peak snap (fine step).** `_gcc_local_peak_snap` snaps the
   anchor to the nearest local maximum of the SAME upsampled GCC-PHAT
   correlation `_estimate_alignment` already computed (shared `_gcc_correlation`
   core — one correlation, never a second formula), searching only within
   ±(period/6) at Fc (`GCC_SNAP_RADIUS_PERIODS`, ≈83 µs at Fc = 2 kHz — the λ/6
   GPS lobe-selection budget). Magnitude finds the peak; the same ±1-bin
   `_parabolic_peak` sub-sample refine as the global-peak path applies. No local
   maximum inside the radius ⇒ the bare anchor is kept (`snap_found=False`). The
   snap is bounded closed-form, so it can never rail onto a neighbouring lobe,
   and it heals the ±1–2-sample integer-argmax jitter of the bare anchor (the
   reproducibility clause — bake-off: a 44.7 µs anchor jump collapsed to 6.9 µs).

The seed delay is `alignment.snapped_delay_us` when the snap found a peak, else
the bare anchor; `_select_alignment_pair` then scores it against the grid and
`_build_candidate` commits the winner. GCC's global correlation peak stays the
capture-quality seed (`seed_delay_us`,
`confidence_source='gcc_phat_seed'`) and is NOT the applied delay. The declared
`delay_range_ms` (expanded by `ALIGNMENT_DELAY_PLAUSIBILITY_MARGIN_MS`) is the
outer plausibility rail (Fix 3): a final selected value outside it routes to
`low_alignment_confidence` re-measure guidance in `crossover_v2_flow`, never
auto-apply. `delay_target_driver` is intentionally not required — a fresh preset
has no applied-delay target until this measurement chooses one.

The complex branch TFs are independently argmax-peak-referenced. The raw
deconvolved-IR argmax gap must first have the inter-sweep clock term
`ε × (tweeter_start − woofer_start)` removed. The remaining physical peak gap is
retained: the listening-plane prediction phases the tweeter by
`objective_reference_gap + selected_signed_delay` (the residual relative to the
argmax-referenced frame — never the full applied delay, the reverted fix-2).
Removing the whole peak gap loses real driver timing; retaining its clock-drift
component recreates the 2026-07-22 JTS3 mismatch. After selection, the alignment
record preserves `delay_us == raw_delay_us - parallax_us`; `seed_delay_us`
retains the corrected GCC seed. `alignment_confidence` remains GCC seed/capture
confidence, labelled `gcc_phat_seed` — it is not a confidence score for any
flatness minimum.

Flatness evidence on the candidate: `alignment_seed_ripple_db` is the summed
ripple at the SEED PAIR (correlation's polarity at the delay the pre-#2598 path
would have applied) and `flatness_improvement_db` is
`seed_ripple − committed_ripple` — non-negative on the flat-sum path, since the
seed pair is always one of the scored candidates, and legitimately NEGATIVE on
the low-SNR path, where it discloses what declining a noise-derived flatness
claim cost on paper. `anchor_delay_us` / `snap_delta_us` / `snap_found` record
the anchor and the committed residual (`snap_found` is the seed's provenance,
not the commitment's). `flatness_at_bound` is retired.

VERIFY compares the applied response with the summed model **at the committed
delay** (rung P3 / R10b, 2026-08-01): the two measured branches at the trim AND
the delay this candidate commits, so the tracking comparison grades model
FIDELITY — did the emitted graph do what was modelled — against a curve some
delay selection actually realizes. Until R10b the reference was the
independently aligned *zero-residual* target sum, which no selection realizes;
on the banked 2026-07-30 JTS3 capture the two references differ by 0.250 dB rms
and up to 0.653 dB over the band the tracking check actually differences
(1/6-octave smoothed, [2000, 4000] Hz;
`captures/r10b-alignment-20260801/committed_delay_numbers.json`)
against a 1.5 dB `VERIFY_TOLERANCE_DB` —
that gap was being spent as tracking error.

**This is a deliberate reversal of a prohibition, and here is why it is safe.**
The retired rule read "do not phase that reference by a candidate-specific
delay: doing so lets a wrong comb-lobe apply explain itself and recreates the
fix-2 false-pass class." Four things make the delay-carrying model a different
proposition from fix-2 (`0b7ab5eb7`, reverted 2026-07-21):

1. **It is the residual, not the applied delay.** Fix-2 phased by the FULL
   `alignment.delay_us`, double-counting the peak gap `_aligned_branch_tf` had
   already removed. The term now is `committed − anchor`
   (`program_analysis.summed_model_residual_delay_us`), a residual about the
   physically-anchored frame rather than a re-application of the measured gap.
   *(Written when the ±(period/6) snap radius bounded that residual below one
   lobe. Since #2598 the search spans a period either side of the anchor, so
   the structural bound is gone and the reachable-lobe question is answered by
   the seed-preferring regularization plus the `left_anchor_lobe` disclosure
   above — read this clause for the frame argument, which is unchanged, not for
   the bound.)*
2. **The candidate cannot make its own prediction MATCH the measurement.**
   Fix-2 was contemporary with a flatness search choosing the delay, so the
   search could pick a lobe and the prediction would agree with it. Since #2598
   flatness selects again — but for FLATNESS, not for agreement with what the
   room did, and a flat prediction is the most demanding tracking reference
   there is, not the most forgiving. VERIFY still differences measured against
   predicted; nothing in the objective moves the measurement. The closed loop
   fix-2 opened would need the selection to be scored on measured-vs-predicted
   error, which no objective here is.
3. **The number a candidate could formerly talk past is still on the old
   instrument.** `CrossoverCandidate.predicted_ripple_db` — the sole input to
   the G1 `MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB` capture-quality threshold —
   is still measured on the independently aligned sum, deliberately, so a
   candidate cannot use its own delay term to lower it. That threshold
   discloses rather than refuses since #2087, and the argument survives the
   change intact: a reservation a capture can talk its way out of is as
   dishonest as a veto it could. This is a real evasion path, not
   a theoretical one: sweeping the residual across the ±(period/6) radius on
   the banked 2026-07-30 capture, **32 of 84** sampled residuals score BELOW
   the zero-residual 14.8831 dB, bottoming at 14.0744 dB — and that capture
   sits 0.12 dB under the 15.0 dB threshold.
4. **Fix 3's plausibility rail is unchanged**: the applied delay still has to
   sit inside the preset's declared `delay_range_ms` ± margin.

The selected applied delay is still what proves the correction realizes the
physical alignment in the original time origin — that is the anchor's job, and
`test_snap_production_path_preserves_parallax_contract` still closes that loop.

**One gate's verdict DOES move: the predicted-spec improvement gate**
(the retired `correction_not_an_improvement` row in the refusal table above;
it banks `LEDGER_NOT_AN_IMPROVEMENT` now rather than refusing). The residual now
enters BOTH of its terms — the raw pre-fit prediction and the linearized one —
and does not cancel between them, because a comb costs the corrected, flatter
model more than the already-rippled uncorrected one. So `improvement_db` falls
monotonically as the residual grows: **0.643 → 0.238 dB across 0 → 83.3 µs** on
the conductor fixture, against a 0.5 dB requirement. The crossing ONSET is
fixture-specific — it scales with each capture's own improvement headroom — so
read the ~50 µs figure from that sweep as an illustration, not a threshold.
Residuals this large are reachable on real hardware (a 30.023 µs `snap_delta_us`
is recorded in `docs/research/2026-07-29-attribution/04-mechanism-frequency.md`).
This is the gate becoming honest — the pre-R10b model flattered every correction
by assuming the committed delay was perfect — and whether
`PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB` should be re-sized against
delay-carrying models is **deferred**, not answered here.

Both measured and predicted magnitude curves receive the same 1/6-octave
smoothing before tracking error is computed. The unsmoothed prediction is used
only to identify the interior of a genuine modeled notch for the established
notch-exclusion mask. Comparing a smoothed capture with a raw prediction caused
a false 1.99 dB failure at the hardware-best delay; like-for-like comparison of
that same capture is 0.490 dB max (raw-to-raw is 0.606 dB).

### Gotchas — the W6 bug-class catalog (do not reintroduce)

> **Live reference — the historical tag above does not cover this section.**
> The runbook's "[Debugging — where to look
> first](../tuning-operator-runbook.md#debugging--where-to-look-first)"
> delegates its bug-class list here,
> and "do not reintroduce" is a current instruction, not a campaign result.

Each was found on hardware and fixed at root cause (no wrapper layers,
no retries-as-bodge). Treat these as regression fences.

1. **Read the playback device through `resolve_active_playback_device`,
   never a nonexistent `topology.playback_device`** (#1590). The
   topology has no such attribute; `resolve_conductor_context` resolves
   it via `playback_route.resolve_active_playback_device`.
2. **Session volume is `min(−20, max(caps))`.** `min(caps)` starved the
   woofer ~40 dB; the emergency-floor invariant would also catch the
   inverted derivation at runtime (#1591-adjacent).
3. **Hold the measurement pause + volume for the whole session.** The
   jasper-voice idle reconciler reverted the session volume within
   ~200 ms when it was protected only per-play; open the pause *before*
   the first set and register the abort target (#1591). Seam raises
   (`ProgramPlaybackRefused`, `CamillaUnavailable` — a bare `Exception`)
   must hit the catch-all cleanup arm, not escape leaving volume active
   and the phone frozen.
4. **Use `DEFAULT_CAMILLA_CONFIG_DIR` as the writer-lock SSOT** (#1592).
   Creating `.dsp_apply.lock` under a read-only path raised `EROFS`; a
   local seam `OSError` is wrapped (`CrossoverV2LocalSeamError`) so it is
   never misclassified as a relay-transport death.
5. **Pipeline references and mixer names must close.** The emitter
   produced mixer `program_route_2way` while the pipeline referenced
   `split_active_2way`; CamillaDSP rejected it only at LOAD time (#1593).
   `pipeline_reference_closure_errors` (`graph_safety.py`) is now a build
   gate that reports *every* dangling reference before apply.
6. **Channel-map is band-relative, not total-energy** (#1594). LF room
   rumble vetoed a total-energy discriminator; identification needs
   target-band rise ≥12 dB over that channel's own ambient, plus a CROSS
   test. That cross-band half shipped here as a flat `<6 dB` rise and was
   **superseded by an isolation ratio on 2026-08-21** — see gotcha #25, which
   is why the additive form had to go. The target-band floor is unchanged.
7. **The −65 dB tweeter cap is a relic** (#1595). The HF measurement
   ceiling is derived from sensitivity (invariants 1–2 in
   [`crossover-v2-engine-design.md`](../crossover-v2-engine-design.md)); the old
   seed read near-inaudible (27 dB in-band SNR) on the DE250. Since
   2026-08-23 the research ask no longer emits it and the field is
   optional, so a profile saved from here on says the same thing by
   leaving `max_effective_peak_dbfs` OUT; a stored seed is still read as
   that same delegation. The derived cap sets each driver's composed
   segment level — it never bounds the seat-level volume ceiling, which
   is digital headroom.
8. **Apply must translate fingerprint vocabularies** (#1596). The seam's
   freshness guard compares the *baseline* candidate's fingerprint;
   forwarding the *measured* fingerprint made every apply refuse
   `baseline_candidate_fingerprint_mismatch`.
9. **Never compare depths inside a predicted notch** (#1597). VERIFY
   tracking excludes predicted-notch regions (keyed on predicted level)
   and clamps to this capture's own validity floor — comparing notch
   depths is meaningless (a run-7 27.83 dB raw max against a predicted
   sum whose own ripple was ~30 dB).
10. **Undo must reach a v2-aware path, not the legacy 500** (#1598).
    `/crossover/v2/restore` reloads the stashed `pre_apply_profile`; the
    legacy `/crossover/restore` expects a pending commissioning-run apply
    a v2 apply never creates.
11. **Predictions must share the adaptive reflection gate** (#1600). A
    fixed-65 ms prediction window baked a desk-bounce null into the
    predicted sum, invisible to the gate-comparability rule; the
    prediction now uses the same adaptive gate as `_driver_response`
    (verified rms 1.496 dB / max 5.115 dB on a real WAV).
12. **The deferred REVIEW hold has its own watchdog budget** (#1601). A
    stale deployed capture page (pre-v3 contract) deadlocked Chrome and
    the watchdog killed the review hold; the deferred hold rescopes to
    `REVIEW_HOLD_BUDGET_S` (900 s) and the page gained hold/countdown
    states.
13. **Ensure the crossover preview at session start** (#1602). A missing
    preview baked the generic bundled preset into the candidate and
    blocked apply forever; `ensure_crossover_preview_ready` (one
    generator, two callers via `save_crossover_preview`) runs at the top
    of `resolve_conductor_context`.
14. **`pre_apply_profile` is carried forward unconditionally** (#1603).
    A VERIFY re-arm used to wipe it, losing Undo.
15. **Calibration piggybacks on every begin** (#1604). The phone posted
    its mic setup (including calibration id) only once, racing the armed
    state, so calibration was never applied. `main.js` now attaches
    `setup: setupWirePayload()` to every `begin_capture` post — a
    last-write-wins slot the Pi reads on each arm.
16. **Linearity is band-relative + ambient-compensated, not full-band
    peak** (2026-07-20). Sibling of gotcha #6/#1594's channel-map fix —
    the linearity gate hadn't gotten the same treatment yet. Two real
    hardware captures (Dayton iMM-6C and UMIK-2, same room/placement)
    both failed `agc_behavioral_fail`: continuous LF room rumble ~30 dB
    above the tweeter-band ambient inflated the quiet woofer pilot's
    full-band PEAK enough to compress the captured 10 dB delta past the
    0.5 dB tolerance, even though both mics agreed the driver was linear
    once measured in its own declared band with ambient-subtracted RMS
    (9.8-10.0 dB on both). `_pilot_observations` now measures each
    pilot's level in its own band (`_band_power`, the same mechanism
    `_channel_map_ok` uses) with the CHECK ambient window's in-band power
    subtracted (power domain) before converting to dB. When the quiet
    pilot's own in-band SNR doesn't clear `PILOT_MIN_SNR_DB` (≈12.4 dB,
    derived from the tolerance + a bounded ambient-nonstationarity model
    — see the constant's comment), the estimate isn't trustworthy either
    way: `linearity_ok` is `None` — UNKNOWN (#1838; it was forced *True*
    until then, which kept it out of the FAILURE branch but read as a PASS
    to anything that did not also check `pilot_snr_ok`, and is how a session
    published `linearity_ok=true` beside a −60.9 dB captured delta against a
    programmed 10.0 dB) — and
    `PilotObservation.snr_valid` / `ProgramAnalysis.pilot_snr_ok` flag it
    so `crossover_v2_flow._consume_check` routes to `snr_floor`, never
    `agc_behavioral_fail`. The aggregate over roles is tri-state
    (`_aggregate_linearity_ok`): FAILURE wins, then UNKNOWN, then PASS.
17. **A pilot level used ABSOLUTELY needs a peak reference, not the
    ambient-subtracted linearity estimate** (2026-07-20, same PR as #16,
    caught in review). `_solve_gain_plan` computes `k = level - gain_db` —
    an absolute estimate of the whole capture chain's dB gain, not a
    delta — then aims `MeasurementPriors.target_capture_dbfs` (documented
    as a capture-PEAK target) through it. Gotcha #16's ambient-subtracted
    `level_*_dbfs` briefly fed this too, silently shifting `k` by however
    much ambient power was subtracted (measured 13-17 dB on the two real
    captures once measured — worse than a synthetic-fixture reviewer
    estimate of ~7 dB — because a real room's ambient floor is far from
    flat across bands). `PilotObservation` now carries a SEPARATE
    `peak_lo_dbfs`/`peak_hi_dbfs` — the exact pre-#16 full-band peak,
    verbatim — for this one absolute-use consumer; `level_*_dbfs` stays
    ambient-subtracted for the (delta-safe) linearity verdict only. An
    in-band (band-limited) peak was tried as a "more robust" replacement
    but empirically introduced its own bandlimiting-leakage bias (up to
    ~1.3 dB on a real capture, windowed or not) — worse than a few tenths
    — so the verbatim pre-#16 computation was kept instead of trading one
    subtle bug for a smaller one.
18. **The human mid-flow Apply gate was a dead end — removed** (owner
    ruling, 2026-07-20). A hardware session proved it out: phone-only
    users cannot bounce to a second browser tab to tap Apply, and "apply
    this?" is unanswerable the moment after measuring — the household has
    no basis to judge a raw candidate. Prior art (Sonos Trueplay, Genelec
    GLM, Anthem ARC) all measure → apply → verify automatically, with the
    human judgment happening AFTER, by ear, with undo available. Fixed by
    promoting the review-screen's confidence nudge
    (`ALIGNMENT_CONFIDENCE_NUDGE_FLOOR`, informed consent) into a hard
    MEASURE-phase gate (`ALIGNMENT_CONFIDENCE_TRUST_FLOOR`, now owned by
    `crossover_v2_flow.py` — the decision-maker, not the renderer) and
    having the conductor fire the SAME apply transaction a household's tap
    used to trigger (`handle_v2_apply`, unchanged, now called from a
    background thread right after the candidate-carrying accept instead of
    from an HTTP handler alone — that accept was MEASURE's when this landed
    and is the pre-apply cloud group's close since 2026-07-27, see "When the
    fit runs"). The `CaptureBeginDeferred` soft-hold mechanism
    before VERIFY is UNCHANGED — only the release trigger
    moved from a human tap to the auto-apply completing, and its copy
    changed from "waiting for the household to apply" to "Applying to
    your speaker…". `REVIEW_HOLD_BUDGET_S` shrank from 900 s (sized for a
    human review) to 30 s (sized for the apply transaction's own latency).
    A separate, unrelated fix landed in the same PR: a deliberate phone
    Stop (`CaptureAborted`, `reason == "stopped"`) was bucketed into the
    same `relay_timeout` ("link timed out") catch-all as a genuine
    transport death — `CaptureAborted` now carries a structured `reason`
    attribute so the two can be told apart, and Stop gets its own honest
    `user_stopped` code.

    **Adversarial review (SF1, same PR): the auto-apply worker didn't
    coordinate with session death.** The background thread had no idea a
    Stop (host-driven `stop_event`, or a phone Stop the relay loop's own
    poll already turned into a persisted terminal failure) had landed, so
    the interleaving could produce incoherent durable state — `applied=True`
    silently clobbered back to a "nothing happened" story, or a `failure`
    code silently clobbering a genuine `applied=True`. Three-part fix: (a)
    a best-effort cooperative pre-apply check (`stop_event.is_set()` OR an
    already-persisted failure code) skips the transaction entirely before
    it starts — logged `event=correction.crossover_v2_auto_apply_skipped_stopped`;
    (b) `observe_apply_success` no longer blindly clobbers an existing
    `failure` code to `None` (the reverse race — `_persist_terminal_failure`
    already preserved `applied=True` once observed, for the same session —
    was already correct); (c) the envelope now appends an honest "the
    crossover was already applied" acknowledgment to any
    `TEMPLATE_SESSION_RESTART` code's copy (`relay_timeout`, `user_stopped`)
    rendered once applied, since that copy's own "start over…" framing is
    written for the pre-apply phases and is actively wrong once something
    genuinely got applied. Neither check can fully close the race (an
    in-flight DSP write can't be safely interrupted mid-transaction) — (b)
    is what guarantees the FINAL DURABLE STATE is always coherent regardless
    of which side of the race wins. (A second adversarial pass found that
    claim did not extend to the RENDER — see immediately below.)

    **Second adversarial pass, same PR: durable-state coherence did not
    imply render honesty ("interleaving A").** (b) guarantees `applied` and
    `failure` end up coherent together, but says nothing about
    `accepted_phases` — and (c)'s acknowledgment originally fired on
    `active_step == "verify"`, DERIVED from `_phase_from_state`, not from
    `applied` directly. When a Stop's `_persist_terminal_failure` call lands
    WHILE the auto-apply transaction is still mid-flight, `applied` reads
    False at that instant, so the §5.6 reset (correctly scoped away from
    `apply_failed` alone, per SF2) fires for `user_stopped` and clears
    `accepted_phases`. The auto-apply's own success can then land moments
    later and flip `applied` True — but `accepted_phases` stays cleared, so
    `_phase_from_state` resolves the combination to `PHASE_CHECK`, not
    `PHASE_VERIFY`. (c)'s acknowledgment, keyed on that derived phase, never
    fired: the household saw "You stopped the measurement. Start over,"
    with no Undo, over a genuinely-changed crossover. Fix:
    `crossover_envelope_v2._failure_envelope` now takes `applied` as an
    explicit parameter — the RAW `status["crossover_v2"]["applied"]` state
    fact — and keys its override on that alone, never on `active_step`/phase.
    This is the general form of the rule the PR should have shipped the
    first time: **any failure screen rendered while `applied` is durably
    True says the crossover was applied and offers Undo, regardless of what
    phase/active_step/template says** — because phase derivation is exactly
    the kind of thing this same race can corrupt.

19. **The repeat-level drift gate is band-relative RMS, not full-band
    peak** (2026-07-20). The THIRD sibling of the same full-band-estimator
    class as gotcha #6/#1594 (channel-map) and #16 (linearity) — this one
    hadn't gotten the treatment. MEASURE plays two bit-identical woofer
    sweeps bracketing the tweeter sweep; `_estimate_drift` rejects
    (`drift_baselines_disagree`) when their captured levels disagree past
    `REPEAT_LEVEL_TOLERANCE_DB` (0.3 dB), the guard against browser AGC
    riding the gain mid-program. It compared `w1.peak_dbfs` vs
    `w2.peak_dbfs` — full-band single-sample PEAK, which is unstable for a
    low-frequency room-mode-excited sweep: the loudest sample jumps between
    otherwise-identical sweeps. Two real captures — a Dayton iMM-6C
    (iPhone) AND a UMIK-2 (computer, no AGC, exonerating the mic path) —
    both false-rejected at ~0.64 dB by peak while agreeing to ≤0.24 dB by
    in-band RMS. `_estimate_drift` now measures each woofer sweep's level
    as in-band RMS in the sweep's own declared band (`_band_power`, the
    #1615 helper, with the composer's edge fade trimmed) — the failing
    0.64 dB drops to 0.14 dB (pass). Teeth kept: a genuine uniform
    (AGC-shaped) gain difference survives band-limiting and still trips the
    gate (`test_repeat_level_step_is_flagged_as_glitch`); a peak-only LF
    transient no longer does (`test_repeat_level_lf_transient_does_not_false_reject`).
    The epsilon/residual timing sub-conditions are untouched.

20. **A guard whose input is a constant is not a guard** (#1810,
    2026-07-28). The FOURTH sibling of the same family, and the nastiest,
    because it looked shipped. Gotcha #16 built the pilot SNR guard and
    wired its ambient window from `_analyze_check` only — every other
    phase called `_pilot_observations` with no ambient, so
    `_pilot_in_band_snr_db` returned its documented `+inf` "nothing to
    validate against" sentinel and `snr_valid = snr_db >= PILOT_MIN_SNR_DB`
    was satisfied *unconditionally* on MEASURE, VERIFY and every cloud
    position — the code read as guarded and executed as unguarded from
    2026-07-20 until it was found on 2026-07-28. It surfaced on JTS3 when
    a freshly-applied correction dropped
    the pilot band 14–18 dB, the quiet pilot landed ~5 dB over the room
    floor, and VERIFY reported `agc_behavioral_fail` — telling the
    household its phone's microphone had misbehaved, with
    `pilot_transfer_step_db` (the only direct recording-chain evidence)
    null in the same log line. Fix: MEASURE/VERIFY programs now carry their
    own 1 s ambient window immediately ahead of the pilot pair
    (`PILOT_AMBIENT_WINDOW_S`), `_pilot_verdicts` reads it, and all three
    verdicts branch to `pilot_level_collapse` before their linearity check.
    Two design constraints worth keeping. First, the window sits AFTER the
    courtesy settle — a floor measured before the "go quiet" warning is not
    the floor the pilots play into. Second, it feeds the level/SNR path
    ONLY, never `_channel_map_ok`'s ±12 dB rise test: that threshold was
    calibrated against CHECK's long framed estimator, and — note the
    precise reason, since an earlier draft of this entry overstated it —
    threading the 1 s window there would change **no verdict today**,
    because `analysis.channel_map_ok` is routed on at exactly one site
    (`_check_verdict`, through `capture_dispatch.check_screens`, which
    refuses on an explicit `False` only). MEASURE/cloud/VERIFY compute the
    flag and never branch on it. What it would do is leave a False flag ARMED on those
    analyses for whoever next adds a routing branch, at which point a pilot
    pair a few dB over the floor would hard-stop with copy blaming the
    speaker wiring. **The general rule:** when a guard's input
    can take a value that makes its comparison vacuously true, a test must
    assert the input is a real measurement on every path that claims the
    guard — `test_pilot_snr_is_measured_not_infinite` is that test.

21. **The courtesy beeps must precede every audible thing, not just the
    sweep** (#1812, 2026-07-28). The flow-simplification §2.5 fix (#1771)
    moved the prelude from the head of the program to directly in front of
    the first SWEEP, on the stated premise that a leading pilot pair is
    inert lead-in. It is not: a pilot is a full-gain band-limited chirp.
    MEASURE and VERIFY therefore shipped a program whose first sound was
    two chirps at t=0, with the (6 dB quieter) "quiet please" beeps
    arriving ~4 s later — "sweeps then beep beep beep", as the owner heard
    it. Nothing caught it because the acceptance test measured only the
    FORWARD interval (beeps → stimulus) and had nothing to say about what
    preceded them. Both directions are pinned now
    (`test_no_audible_content_precedes_the_first_courtesy_beep` is the
    missing half). PR #1771 owed an on-device listen; the session that
    provided it is the one that failed.
22. **MEASURE's level is solved against the ROOM now, not the ADC**
    (#1825, 2026-07-28). MEASURE is the only phase in a v2 session whose
    level is solved — CHECK's pilots and every summed-sweep phase (VERIFY
    and both cloud groups) ride `BASE_STIMULUS_PEAK_DBFS` clamped by the
    driver cap — and `_solve_gain_plan` used to drive each driver until
    its capture peak hit `MeasurementPriors.target_capture_dbfs`
    (−10.5 dBFS) no matter how quiet the room was. That made capture 2 of
    every session structurally the loudest thing the household hears
    ("measurement 2 is way louder than everything else", owner
    2026-07-28). The solve now targets the SNR the fit actually needs, per
    driver, in that driver's own measurement band: the worst overlapping
    ambient band from CHECK's own report plus that band's requirement from
    the shipped split-SNR policy (`DRIVER.alignment_snr_ok_db` inside the
    crossover overlap window, where MEASURE's delay/polarity estimate
    lives; `DRIVER.snr_ok_db` outside it) plus
    `MEASURE_SNR_SOLVE_MARGIN_DB`, plus — since #1838 — the stimulus's own
    crest factor (`sweep_band_crest_factor_db`), which converts that
    band-RMS demand into the capture-PEAK units `k_db` and the flat target
    are expressed in. Three things bound it: the old flat
    target is now the CEILING (this can only make MEASURE quieter or
    leave it alone), the leading pilot pair's own `PILOT_MIN_SNR_DB` guard
    is a floor, and `DRIVER.peak_too_low_dbfs` is the degeneracy tripwire —
    if it is the winning arm, the ambient evidence is not solvable at all,
    so the solve is REFUSED and the role falls back to the flat target with
    `bound_by=degenerate_ambient` and a WARNING (#1838 D2; before that it
    shipped the floor, which is what the field session did). Per-role
    gains diverge; per-REPEAT gains do not (the drift estimator needs
    bit-identical repeats — `build_measure_program` applies one gain per
    role to every occurrence). `GainPlan.role_solves` carries the
    derivation into `check.json` and into
    `event=correction.crossover_v2_measure_level_solve`, including the
    disclosed `no_ambient_evidence` and `degenerate_ambient` fallbacks.
    **Not hardware-validated** — the room-noise floor a real phone mic
    reports is what decides how much this actually backs off.

    **What to expect per driver — REVISED by #1838.** The prediction that
    stood here ("the reduction is mostly the *tweeter's*; expect woofers to
    report `bound_by=flat_target` far more often") was computed against a
    `band_levels_dbfs` that read every band 18–39 dB too quiet and clamped
    the upper ones flat at `DBFS_FLOOR`. It should not be trusted. With the
    estimator Parseval-correct and the crest term carried, the solve
    re-run on the measured JTS3 room of session `cap_-Us10xORVNlFa_dgi-sP7g`
    (`sub_bass −57.86`, `bass −69.35`, `upper_bass −68.13`,
    `transition −71.24`, `mid −81.51`, `treble −90.99` dBFS) gives
    `bound_by=room_snr` on BOTH roles: woofer −8.3 dB, tweeter −20.5 dB
    against their flat targets. The mechanism is intact (the tweeter's band
    is ~12 dB quieter *and* carries the 41 dB alignment demand, so it moves
    further) but the woofer is no longer expected to pin at `flat_target`.
    Treat any per-role `bound_by` distribution as unpredicted until bench
    data lands. **Ambient numbers logged before #1838 are on a different
    scale and additionally window-length-dependent — re-derive them, never
    diff them against a new log line.**

    The ambient table's two coarsenesses are unchanged, and both still err
    LOUD — toward today's behavior. Its rows are wide and overlap is
    overlap, so a woofer swept from 150 Hz clips the 80–160 Hz `bass` row by
    10 Hz and inherits that row's full, LF-heavy level; the crest term now
    compounds that (it is computed over the covered 10 Hz, correctly, while
    the ambient level is still the whole row's), which is why `bass` is the
    binding row in the JTS3 numbers above. The table also stops at 12 kHz,
    so a tweeter's top ~2/3 octave contributes no demand; room noise there
    is below every lower band in any real room, so the omitted rows cannot
    be the ones that would have won. Neither is worth a finer table — nor
    is restricting the ambient level to the covered slice, which would err
    QUIET — before bench data asks for one.

    **That instrument is live again (issue #1830, fixed).**
    `DriverResponse.snr` — the per-driver band-SNR block that most directly
    answers "did the quieter sweep still clear the floor?" — was structurally
    `None` on the whole v2 path for as long as the path existed: nothing
    threaded an `ambient_report` into `MeasurementPriors` for MEASURE, so the
    verdict never computed even though CHECK had already measured the room
    floor and written it to `check.json`. `_check_verdict` now holds that
    report at CHECK's accept and `_measure_priors` passes it, so the verdict
    populates — read it as `woofer_snr_db` / `woofer_snr_verdict` /
    `woofer_snr_band` (and the tweeter triple) on
    `correction.crossover_v2_measure_diag`, and as `<role>_snr_db` /
    `<role>_snr_verdict` / `<role>_snr_band` in the retained sidecar. It is a
    REPORTED verdict, not a gate: nothing in the v2 flow fails a capture on
    it, so a session that starts saying `reduced` or `insufficient` is the
    instrument working, not a new rejection.

    **Both sides of that subtraction are read in ONE domain, and which one
    the noise report picks (second half of #1830).** Threading the report
    made the verdict populate, but it was still subtracted from the
    DECONVOLVED transfer function — and the deconvolution divides the capture
    by the reference regenerated at the segment's own `gain_db`, so
    `magnitude_db` is invariant to how loud MEASURE played (the pinned
    contract `test_measure_analysis_is_invariant_to_the_programmed_drive_gain`).
    A fixed room floor subtracted from an invariant is an invariant: measured
    through the production analyzer, a MEASURE played 20 dB quieter into an
    unchanged room reported the SAME worst-band SNR and the same `ok`, while
    the same-domain reading fell the full 20 dB. Against that same-domain
    reading the old number ran **roughly +17 to +65 dB high** — band-dependent,
    growing as the measurement quietens, and in the quiet arm the honest
    per-band reading goes a few dB BELOW zero while the old one still said
    `ok`. A bound rather than a decimal on purpose: those figures come from
    fixtures at different ambient sigmas and no single number reproduces
    across them; the load-bearing claim is the trend, pinned exactly by
    `test_measure_snr_verdict_moves_with_the_measurement_level`. So the
    sentence above ("did the quieter sweep still clear the floor?") was the
    one thing it could not answer.

    `program_analysis._driver_snr_block` now makes the branch
    `driver_acoustics.analyze_driver_capture` always made and #2024 set
    for the summed gate: a `"raw"` noise report — which is every report
    `framed_ambient_band_report` emits, so every ambient a v2 CHECK hands
    forward — pairs with the RAW captured sweep's band levels
    (`window="rectangular"`, #1847's fix for a non-stationary sweep); a
    `"deconvolved"` report still reads the transfer function; a raw report
    with no captured segment to pair against yields NO verdict rather than a
    cross-domain one. **Raw is the right side here because that is the domain
    #1829's level solve aims in** — `_solve_role_gain`'s room arm is
    `ambient_band_level + required_snr_db + crest_factor_db` off the raw
    ambient table — so the verdict reads back the solve's own target and can
    actually check it. #2024 resolved the same "read one domain" rule the
    other way because the summed gate has no level solve aimed at it. The
    rectangular window's `10*log10(sweep_len/capture_len)` duty-cycle offset
    (the reason `_capture_band_levels` warns it is not a drop-in) is 0 dB here
    because `_raw_sweep_segment` slices exactly `segment.n_samples`.

    The verdict now tracks the played level dB-for-dB, so more sessions will
    disclose `reduced` / `insufficient` than did before — still reported,
    still not a gate.
    It is deliberately absent (not guessed) on a conductor rehydrated past
    CHECK, which has no ambient of its own — the report is not persisted
    across that boundary because a noise floor measured at another mic
    position is a stale claim about this one.
    The corroborating readouts remain what they were, and are still the
    right cross-check: the disclosure event's `bound_by` + `solved_gain_db` +
    `ambient_dbfs` (what the solve believed and why), then
    `DriverResponse.validity_floor_hz` and the gate window, the alignment
    estimate's `confidence` / `status`, and VERIFY's own tracking residual.
    A capture that got too quiet degrades those visibly — a rising validity
    floor, a collapsing alignment confidence, a widening VERIFY residual —
    before it degrades anything the household would hear.

    Two premises in the filed issue did **not** survive tracing, and are
    recorded here so they are not re-derived: (a) `build_v2_capture_plan`'s
    `nominal_gains = BASE_STIMULUS_PEAK_DBFS` is a *duration budget* — that
    program is measured with `_program_duration_ms` and never played, and
    sweep/gap lengths are gain-independent; (b) the `branch_level_match`
    reading (`level_w` −18.8 vs `level_t` −7.3) is **not** a drive-level
    difference. `program.segment_stimulus` regenerates the deconvolution
    reference at the segment's own `gain_db`, and
    `deconv.regularized_deconvolution_full`'s Tikhonov epsilon is relative
    to `|X|²`, so the drive cancels *mathematically exactly* in the
    deconvolution and every downstream consumer (`solve_branch_trims`,
    `realized_branch_level_match`, the Layer-1a shared level frame) works
    per-unit-drive. That 11.5 dB is a sensitivity delta, not a drive delta.
    Pinned since 2026-07-28 by
    `test_measure_analysis_is_invariant_to_the_programmed_drive_gain`
    (`tests/test_audio_measurement_program_analysis.py`): the same synthetic
    drivers analyzed twice with the tweeter driven 10 dB quieter agree on
    `trim_band_average_db`, `trim_db`, and every `DriverResponse.magnitude_db`
    to 0.01 dB. Before that this paragraph was the only thing holding the
    claim — and a reference built at unit amplitude would have re-introduced
    an inter-driver error of exactly the gain skew, silently.

23. **A deterministic pre-condition must be checked before the link is
    minted** (#1820, #1821, 2026-07-28). `resolve_conductor_context`'s
    driver-safety gate checked only that a profile object was PRESENT
    while its refusal text claimed confirmation had been checked; the
    real confirmation gate lived four screens later, inside
    `prepare_driver_excitation_plan` at CHECK-phase program admission.
    A household whose profile had gone un-confirmed (an enclosure-kind
    declaration rotated the fingerprint) therefore minted a phone link,
    walked to the speaker, and hit a refusal that was knowable at the
    tap. It now calls `evaluate_driver_safety_profile(...)
    .confirmed_and_current` at session open — before relay registration,
    on both `prepare_v2_session` and `prepare_v2_verify` — and raises
    `CrossoverV2Refused` carrying the SAME registry copy the phone's
    failure screen would have rendered. The crossover preview is not a
    substitute: `save_crossover_preview` never looks at the safety
    profile's confirmation, so a preview can be
    `ready_for_protected_staging` while the profile is un-confirmed.
    The same session also exposed three refusal-surface defects fixed
    together: the raw slug leak (`ProgramPlaybackRefused`'s `str(exc)`
    reached the wizard's relay status line — `_relay_failure_message`
    had no branch for the program family, and the "never a bare code
    reaches the household" contract had no test), the classification
    collapse (see the `program_profile_not_confirmed` row above), and
    `/sound/`'s confirm control being demoted into a default-closed
    Advanced disclosure by #1819 while the invalidating enclosure
    selector was promoted into the always-visible form.

    **A pre-flight refusal can never reach the envelope.** The envelope
    renders from a PERSISTED `failure`, and this gate refuses before any
    state is written — that is the whole point. So the hard-stop screen's
    `next_action` (the confirm button) was unreachable on the very path
    that became primary, and the household read the remedy as flat text.
    A coded `CrossoverV2Refused` now carries its reason code, the
    dispatcher puts that reason's `next_action` in the 400 body, and the
    wizard's `setStatus` renders it as a control beside the message —
    same registry entry the screen would have read, so the two surfaces
    cannot offer different buttons. An **uncoded** refusal still answers
    with a bare sentence; most refusals' only honest answer is prose, and
    inventing a button for them would be worse than none.

    **One verb is not always the right verb.** The states need different
    actions — `missing` (`/sound/` renders no safety callout at all) and
    `incomplete` (a save just rebuilds the same incomplete profile) — so
    the pre-flight resolves the evaluation status to one of three reason
    codes via `profile_refusal_code`. The play seam cannot do this: its
    admission vocabulary carries a single `PROFILE_NOT_CONFIRMED` slug for
    all three. The gate that holds the evidence is the gate that names the
    action.

    **The confirm ceremony this entry is about no longer exists.** Saving
    the declaration IS declaring it, so a fingerprint rotation no longer
    strands a household — see the refusal table above. Everything above stays true of the incident and of the
    session-open gate, which still runs and still fails closed on
    `missing` / `incomplete` / `stale` / `malformed`.

24. **Background work must hold the wizard's idle exit** (#1854,
    2026-07-29). correction-web is socket-activated and `os._exit(0)`s
    after 600 s with no INBOUND request (`IdleShutdownTracker`,
    `jasper/web/_systemd.py`). A v2 session's only inbound traffic is the
    POST that starts it plus whatever the operator tab polls — and when
    the operator's browser IS the phone that then navigates to the
    relay's capture page, there is none at all: relay polling, sweep
    playback, analysis, auto-apply and verify are all outbound from
    background workers. On JTS3 the process therefore exited exactly
    600.2 s after the last envelope GET, 5 s after the verify capture
    arrived, and the verify analysis died with it (no
    `crossover_v2_result phase=verify` was ever logged; the candidate was
    left applied but unverified). The relay session's own ~900 s budget
    EXCEEDS the idle threshold, so a full-length relay-only session was
    guaranteed to be killed. Fixed with
    `IdleShutdownTracker.hold()` — the same busy counter an in-flight
    request takes, so there is one idle decision, not two. The relay
    orchestrator (`_run_relay_capture`) takes it on the request thread
    before scheduling the runner and releases it in the runner's
    `finally`. (A second holder existed until PR-T3: the auto-apply worker
    thread, which could outlive the runner and so took its own hold before
    `Thread.start()`. That thread is gone — the apply is a household POST
    served in-request, which the tracker's ordinary in-flight-request
    accounting already holds — so the runner's hold is the only one this
    flow takes. The eager-fit rider (2026-07-30) started a background
    thread again — the speculative group close — and deliberately gives it
    NO hold: it is daemonised, it only builds a candidate, and its result
    is an optimisation the confirm never depends on, so a shutdown that
    discards it mid-fit costs the household one re-fit and nothing else.
    #1860 later added the two level-ramp kinds.) A held-open
    exit is reported once per 5 minutes
    (`systemd idle-exit deferred: … holds: …`), escalating to WARNING once
    the process has been continuously busy past
    `_systemd.HOLD_LEAK_WARN_AFTER_SEC` (7200 s = 2× the volume plan's
    `MAX_WALL_CLOCK_CEILING_S`, so no legitimate session — not even the
    longest stage, Full's 2160 s stage 2 — can trip it), so a leaked hold
    can never buy
    silent immortality. **The escalation is a log level, not a reaper.**
    Before this fix the 600 s exit incidentally killed a *wedged* worker
    too (the unbounded `drained.wait()` tail in
    `correction_setup._run_async`); it no longer does, and that is
    deliberate — `_run_async`'s fail-closed invariant says a terminal
    response must never release measurement ownership while the
    graph/volume finalizer can still mutate the speaker. A wedge gets
    fixed at its own layer. **Do not raise the threshold instead** — that
    only moves the cliff; and do not read "idle" as "abandoned" anywhere
    that background work can be in flight (`_idle_exit_restore_capture_entry`,
    the abandoned-capture production restore, is correct again precisely
    because the exit now only fires when nothing is running).
25. **A threshold tuned against a masked noise floor is level-dependent —
    say the RATIO, not the difference** (2026-08-21, jts3). Gotcha #6's
    CROSS half shipped as a fixed additive bound,
    `CHANNEL_MAP_CROSS_RISE_DB = 6.0` — "the other driver's band must not rise
    6 dB over ITS ambient." That constant was calibrated in the OLD, quieter
    measurement frame, whose room floor MASKED the cross-band content every
    honest capture carries. Raise the session level, the mask lifts, and the
    SAME healthy speaker fails `channel_map_mismatch` — a **hard stop with a
    zero retry budget** that told the household to rewire a correctly-wired
    speaker and blocked every measurement round in the louder frame. One
    speaker, one basin-2 config, byte-identical graph, three levels (read off
    `event=correction.crossover_v2_check_diag`; the executable copy of these
    rows is `test_channel_map_accepts_every_measured_session_level`):

    | session ref | seat SPL | woofer target/cross | tweeter target/cross | verdict |
    |---|---|---|---|---|
    | −27.5 (old frame) | 68.1 dB | 53.4 / −0.79 | (healthy) | pass |
    | −9.77 | 73.3 dB | 48.5 / 4.13 | 71.7 / 10.81 | FAIL |
    | −6.80 | 78.6 dB | 51.4 / 7.27 | 73.1 / 15.23 | FAIL |

    The cross energy is NOT electrical crosstalk, and that was established
    before the fix rather than assumed: a two-level discriminator through the
    **BASELINE** graph held cross-band rise at ≤3 dB across both the −16.8 and
    −6.8 faders while own-band rises tracked the fader exactly, so the chain
    is linear. Through the per-driver **ROUTING** graph — which strips no
    crossover filters — CHECK instead sees program-segment SKIRT content plus
    modest driver nonlinearity: content at a roughly fixed RELATIVE level.
    That is what an additive bound cannot describe and a ratio can, so the
    CROSS test is now the ISOLATION RATIO `target_rise − cross_rise` against
    `CHANNEL_MAP_MIN_ISOLATION_DB`. On the rows above it reads 54.2 / 44.4 /
    60.9 / 44.1 / 57.9 dB — flat across a 10.5 dB span of session level.

    **What the CROSS half actually catches — measured, and NOT what an
    earlier draft of this entry claimed.** It is not the mis-wire detector.
    Seven wiring shapes were run through the real validator and the cross rise
    stayed within ±0.4 dB on every one, because a wiring fault changes which
    DRIVER radiates, not which BAND carries the energy. The **TARGET floor**
    (unchanged, ≥12 dB) is what fires on a mis-wire. The CROSS half guards
    abnormal cross-band ENERGY — bleed, skirt and nonlinearity classes, plus
    the degenerate case of one signal reaching both bands at once — and fails
    closed on it. A realistically-rolled-off swap clears the whole channel map
    on `main` and on this change alike; that is a pre-existing gap, tracked as
    [#2800](https://github.com/jaspercurry/JTS/issues/2800), not something the
    ratio regressed.

    **The second half of this gotcha is the guard, and it is the part that
    bites.** The CROSS test refuses when `target_rise − cross_rise < BOUND`,
    i.e. when `target_rise < BOUND + cross_rise` — so it does not merely sit
    beside the TARGET floor, it RAISES it, to `max(FLOOR, BOUND + cross_rise)`,
    eating the floor by `cross_rise` dB. A bound at or below
    `CHANNEL_MAP_TARGET_RISE_DB` does **not** prevent that; the argument that
    it does holds only at `cross_rise ≤ 0`, and an earlier draft of this entry
    made exactly that mistake. Measured end-to-end: a capture at target 13.50 /
    cross 1.72 — both values from the suite's own noisy-room fixture — gives
    isolation 11.78 and refused as the NON-retriable `channel_map_mismatch`
    where `main` refused it as the retriable `snr_floor`. A hard stop telling a
    household to open its speaker, on a capture whose only real problem was
    that it was quiet: the #2052/#2644 class, one rung up.

    The fix is `CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB` (= FLOOR + BOUND): the
    cross rises are always MEASURED and published, but only JUDGED above that
    threshold. Below it the TARGET floor governs alone, because a ratio taken
    where the target barely cleared its own floor is not a meaningful quantity.
    What the guard buys is a self-justifying refusal — above the threshold,
    refusing requires `cross_rise ≥ CHANNEL_MAP_TARGET_RISE_DB`, so the WRONG
    band cleared the very bar we demand of a driver that played, and nothing
    merely quiet can manufacture it. The named residual: for `target_rise` in
    [FLOOR, FLOOR + BOUND) abnormal cross-band energy goes unremarked, which is
    the deliberate trade against the proven false accusation.
    `test_channel_map_cross_test_never_eats_the_target_floor` pins it, and
    `test_channel_map_isolation_boundary_is_inclusive_at_the_bound` pins the
    boundary direction (isolation exactly AT the bound passes — the safe
    direction for a non-retriable hard stop).

    12.0 is the bound because margins are ≥32 dB under the hardware table
    above, it refuses the degenerate both-bands case (~0 dB) and a heavy bleed
    (10 dB), and a LARGER bound is not free: it raises the judged threshold
    too, shrinking the region where the cross half looks at all.

### Future work — the post-W6 follow-ups issue

Tracked in the post-W6 follow-ups GitHub issue (filed 2026-07-19):

- **W5b — delete the legacy flow outright:** the `crossover_envelope`
  legacy body, the `correction_crossover_flow` legacy handlers, the
  selector, and the legacy test suite. This is the big deletion, gated
  on W6's green hardware run (now met).
- Smaller nits: the `apply_blocked` session-gating detail; a
  topology-fingerprint guard on restore; a candidate-config retention
  story; a hub HTTP-routing nit; placement copy improvements; the
  verify-fail expert disclosure. (Stop-control "timed out" copy — fixed,
  gotcha #18.)
- **Constants tuning pass** once real ~1 m runs accumulate (VERIFY pilot
  band, gate-comparability margin, confidence floor, and the PROVISIONAL
  constants above).
- **Driver-spacing input for parallax correction.** `driver_spacing_m`
  is threaded but stays `0.0` today (topology/preset carry no spacing),
  so the §3.2 parallax correction is inert in production. The flatness
  refinement preserves the nonzero-geometry raw/corrected-frame contract,
  pinned on both signed delay lobes by a production-path test. Parallax is
  self-cancelling at the
  mic position (baked into both MEASURE and VERIFY) but the *listening
  position* carries the full geometric error.
- **`sound_current.yml` does NOT update on a v2 apply — by decision, not
  omission (#1605).** `sound_current.yml` means "the last durable `/sound`
  render," never "the config CamillaDSP is currently running." A v2 apply
  writes the source-fingerprinted `active_speaker_baseline_candidate_<fp>.yml`
  and points CamillaDSP at it; the runtime truth is whatever CamillaDSP's
  statefile reports, and the Layer-A truth is
  `active_speaker_baseline_profile.json`. Mirroring v2 applies into
  `sound_current.yml` would create a second mutable Layer-A artifact and
  weaken the source-fingerprinted Apply/Undo ownership, so we deliberately do
  not converge the bytes. Readers treat it accordingly: `graph_carrier`
  recognizes generated configs by content (the fixed name matters only for
  the PR #1009 stale-bake recovery), `jasper-doctor` uses it as a
  last-resort fallback and recognizes source-fingerprinted active-baseline
  names, and `multiroom.leader_config` stashes/restores whatever CamillaDSP
  reports live rather than opening a fixed name. (Deferred cleanups, not required by this
  decision: drop the doctor's fixed-file fallback in favor of an explicit
  active-path-unavailable report, and a name migration to
  `sound_preferences_current.yml` — both owner-gated.)

---

### The W1–W6 rebuild campaign (2026-07-17 → 2026-07-19)

Snapshot narrative, for "why did we end up here," not current state.

The v2 rebuild ran 2026-07-17 → 2026-07-19 (PRs #1578–#1604), architected
by Fable. Its motivation and full decision record are in
[`crossover-measurement-productization-design.md`](../crossover-measurement-productization-design.md);
the first-principles research is
[`crossover-measurement-deep-research-2026-07-18.md`](../crossover-measurement-deep-research-2026-07-18.md);
the on-hardware log that motivated it is
[`crossover-room-e2e-validation-log.md`](../crossover-room-e2e-validation-log.md).

**Why v2 exists.** The legacy flow's cost was structural, not
parametric: a full automatic 2-way run was ~17 page actions + ~12
phone-capture round-trips across two mic geometries, and its delay/
polarity machinery was never wired into the wizard. The ~86 fix-PRs it
absorbed in 2026-07 concentrated in exactly the machinery that
multiplicity demands (repeat admission, geometry handoff, identity
validation, volume restore) — the measurement *math* was never the bug
source. v2's lever was collapsing the interaction topology, not tuning
steps: fewer/richer captures, one mic position, zero user-facing
leveling, all intelligence server-side in pure functions. (History, as
written in 2026-07: "one mic position" held until flat-linearization
PR-3b traded it back for the spatial cloud — deliberately, because the
plan's own evidence says a single point cannot separate the speaker
from the room. The *interaction* lever it describes is intact; the mic
count is not.) This mirrors
every shipping calibrator that owns its output chain (Genelec GLM,
Trinnov, Anthem ARC, Sonos Trueplay) — none exposes a level control;
Dirac/REW push leveling onto the user precisely because they don't own
the chain.

**Wave plan (each wave: implementer in an isolated worktree →
hardware-free tests in the same PR → adversarial-review gate (0
blockers / 0 should-fixes) → green CI → squash-merge). Contracts frozen
so waves could run in parallel.**

- **W1 — measurement core (pure).** `program.py` composer + locator /
  segmenter + drift estimator + GCC-PHAT sub-sample alignment +
  `analyze_program_capture` + prediction. Synthetic-fixture round-trips
  with injected ε / delay / polarity / noise / glitch.
- **W2 — playback + safety.** Channel-routed commissioning graph variant
  + multi-segment excitation admission + `SessionVolumePlan` (fail-closed
  latch reuse) + admitted playback. The W2 adversarial gate caught the
  `min(caps)` misreading and reframed it as `min(−20, max(caps))`.
- **W3 — protocol.** `CapturePlanEntry` (spec + session loop + capture
  page); per-entry locator windows; the relay worker stayed opaque.
- **W4 — apply extension.** `MeasuredCrossoverCandidate` — measured
  polarity/delay through the preset → `camilla_yaml` → delay/graph-safety
  proofs; candidate fingerprint over the new evidence.
- **W5a — the v2 happy path.** The conductor phase orchestration, the
  schema-7 envelope, the auto-advance tap policy, the four failure-screen
  templates, phase persistence + session binding, and the MEASURE/VERIFY
  leading pilot pair + repeat-agreement acceptance. Legacy kept as the
  fallback.
- **W6 — hardware validation (JTS3 + UMIK-2).** The scripted-then-Chrome
  validation ladder: first a scripted bench probe (five trials through
  the mux test-gate → `correction` lane → production chain) established
  ε ≈ 30 ppm and the longest-baseline rule; then full runs through real
  Chrome + relay + the phone. W6 surfaced the bug catalog above across
  run rounds — the first runs (W6.1) caught five cap/cleanup/volume
  defects; W6.5 was the sensitivity-derived-ceiling ruling; W6.7/W6.9
  were the measurement-honesty (notch-aware, gate-consistent prediction)
  fixes; W6.10–W6.12 closed the Chrome-round deadlock, the calibration
  race, and the Undo/`pre_apply_profile` forward-carry. Run 7 reached
  start→applied in 75 s; the first fully-calibrated run (2026-07-19)
  applied a −16.41 dB tweeter trim with calibration resolved on all
  three phases.
- **W5b — deletions + polish.** Gated on W6's first green run (now met);
  see Future work. Deleting the only working flow before the replacement
  touched hardware was the one sequencing risk the plan refused.

The default flipped to `v2` on 2026-07-19. W5b (2026-07-24) then deleted the
legacy flow and the `JASPER_CROSSOVER_FLOW` selector outright — v2 is the only
crossover-measurement flow now.

**2026-08-05 verification scope:** opening/capture-flow only against the current
R15 diff; no review, merge, deployment, or measurement claim. Remaining
operational detail and history were not re-verified. A later same-day R18 pass
re-verified the VERIFY-phase grading section against the shipped
`_analyze_verify` / `_verify_verdict` and added the absolute crossover-region
claim and the two-absolute-grades table; nothing else was re-verified, and no
hardware ran — R18's evidence is offline (synthetic fixtures + the 2026-08-05
checkpoint's own journal).

**2026-08-05 R16 scope:** the capture-flow section only, rewritten against the
R16 lateral-evidence diff and its tests. The lateral walk is code-complete,
**shipped OFF**, and **hardware-unproven** — no session has walked it and none
can until `STAGE1_INCLUDES_LATERAL` flips with R17, so every claim about it is
about what the code does, not about what a household or a microphone did.
Position groups, failure taxonomy, benchmarks, and history were not re-verified.
*Superseded twice — R17 (#2173) flipped the flag on, and the 2026-08-18 pause
flipped it back off; the capture-flow section above has the shipped shape.
Note the walk is still hardware-unproven: the pause landed before the owner-run
selection walk this note was waiting on.*

**2026-08-06 #1654 scope:** three sections only — the conditioning-policy
margin, the R16-stage-1 blocker paragraph, and the level-match frame's
one-sided-band context — re-verified against the shipped
`resolve_driver_excitation_ceilings` / `overlap_band_hz` and re-derived with
this repo's `crossover_response_complex`. **No hardware ran**: the widened
sweep is offline-proven only (unit tests + closed-form filter math), and the
owner's CHECK/MEASURE slice is still owed. Nothing else was re-verified.
A later same-day pass corrected three constants in the conditioning-policy
margin. The analog crossing ratio was a wrong value rather than a rounding:
`0.765` puts an analog LR4 `|P|` at −11.87 dB, and −12 dB falls at `0.761`.
The digital ratio was solved corner-first, and `K` was taken as its reciprocal;
the floor-first solve those numbers are actually used for gives `1.31053`, not
`1.3108`. The `|P(1600)|` and margin figures were unaffected. Still no
hardware.

**Prior verification passes (through 2026-08-11).** #2291 Phase 3c re-verified only the sections it
changed: "The round, graded" (new), the stage-bridge key list (now six keys,
with `entry_baseline` and why it needs no carry-forward), the stage-1
capture table (now 9
captures), the `_decimate_delta` dB-domain paragraph (figures re-derived
through `linearization_fit.complex_correction_response`), and the #2160
spatial-grade paragraph — each against `crossover_v2_flow` /
`correction_crossover_v2` / `crossover_v2/round_evidence` /
`crossover_v2/verification` and their tests. No live-Pi run; no other section
was re-read, and the historical appendix below was not. Earlier scopes, kept
for provenance: 2026-08-10 — #2291 Phase 3a re-verified only the stage-bridge
prose (the new "The stage bridge" block and the automatic-rollback sentence in
the Undo/declaration contract) against `persist_conductor_state` /
`prepare_v2_verify` / `bind_v2_stage_seams` and
`tests/test_crossover_v2_stage_bridge.py`; no live-Pi run, and no other section
was re-read. 2026-08-10: #2292 re-verified only "Recommending an Fc" (the
Undo/declaration two-leg contract) and gotcha 8 against `handle_v2_apply` /
`handle_v2_restore` / `persist_conductor_state` / `reset_v2_journey_state` and
focused offline tests; no live-Pi run. 2026-08-09: R21 re-verified only
"Recommending an Fc" against
the Sound-owned CAS save, exact-candidate apply, Review envelope, and focused
offline tests; no live-Pi run. P0.4 re-verified the VERIFY claim and terminal grading
sections; the four outcomes remain offline-tested with no live-Pi run. P0.3 verified only "Relay sequence and terminal
precedence" and its page-first/Pi-second release note against the relay client,
session verifier, and v2 cleanup/persistence seams. P0.2 re-verified only "Recommending an Fc" against
the selector, conductor, durable summary, household copy, and capture wait;
the post-P0.1 live-Pi all-six timing remains explicitly unverified. The prior
2026-08-04 pass added and verified the planning-vs-shipped
orientation above against the current phase routing; the prior 2026-08-03 pass
re-verified the MEASURE-phase acceptance section,
the terminal-code cause table's `low_alignment_confidence` row, and the
predicted-ripple frame claim against `crossover_v2_flow.py` /
`crossover_envelope_v2.py` / `correction_crossover_v2.py` while landing the
#2087 ruling; separately re-verified the per-capture diagnostics section and
wrote the new "Timeline anchor" section against `program_analysis.py`
(`_global_offset` / `_resolve_anchor` / `_locate_in_window`) while landing
#2093, with its measured numbers re-derived from the 11 retained 2026-08-03
cloud VERIFY captures; and re-verified the retry/refusal contract (the position-group
retry-budget bullet, the reason-registry paragraph, and the attempt-meter
paragraph under Failure taxonomy) against `crossover_v2_flow.py`
while landing the #2086
ruling; then re-verified that same retry/refusal contract after #2097's
adversarial review against the structured all-reason diagnosis model, the
final-capture terminal runner path, final-index stage-1/stage-2 rendering, and
the stable spent-event evidence pairing. R17 added the "Recommending an Fc"
section, written and verified against `fc_selector.py` and the conductor's
`_fc_candidate_set` / `_sweep_fc_candidates` / `_adjudicate_fc` as landed, with
the phone-deadline figures re-derived from the shipped capture page and the
memory/wall numbers quoted from the #1894 on-Pi profile rather than re-measured
here. Sections outside those paths carry their 2026-07-30 verification.

