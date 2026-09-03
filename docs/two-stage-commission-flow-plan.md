# Two-stage commission flow — work order (issue #1806)

> **Status: adopted work order (2026-07-28).** Anchors
> [issue #1806](https://github.com/jaspercurry/JTS/issues/1806) and the
> owner ruling recorded on
> [issue #1813](https://github.com/jaspercurry/JTS/issues/1813).
> Subsumes the UX backlog filed from the 2026-07-28 field sessions:
> #1804 (orientation page), #1805 (position pattern), #1807 (time
> budgets), #1835 (CHECK pre-arm copy), and #1840's page half (retry
> dead air).
>
> **Supersedes §2.2 and §2.6 of**
> [`docs/historical/linearization-campaign-2026-07.md`](historical/linearization-campaign-2026-07.md)
> — see D10 and D1 respectively. Composes with — does not re-open — the
> rest of that document (screen grammar §2.1/§2.3, tier chooser §3) and
> [`docs/gating-v2-plan.md`](gating-v2-plan.md) /
> [`docs/room-correction-regime-plan.md`](room-correction-regime-plan.md)
> (the layers this flow commissions).
>
> **Symbol note (2026-08-26).** The second preparer this document calls
> `prepare_v2_verify` was folded into `prepare_v2_session(verify_only=True)`
> by #3166; the two stages are one function under one flag. Every claim
> below has been re-pointed to say so **except D2's quoted owner ruling**,
> which keeps the wording it was given on 2026-07-29.

## Why (the ruling, in one paragraph)

Today the flow decides for the household. When the cloud-measure group
closes, the conductor fits a correction and the host applies it
**unconditionally** — inside the same relay session, three seconds before
VERIFY, with the household holding a phone. The 2026-07-28 session
(`cap__wwcU5n3Cj7P_xNQiDpj5g`) showed what that costs: a fit that failed
its own spec by +6.04 dB was applied because it improved its own model's
residual, the apply dropped the chain 8 dB mid-session, VERIFY failed on
the consequence, and the box was left applied-and-ungraded with no
household-visible decision point anywhere in the sequence. The owner's
ruling: **the review interlude IS the apply decision point.** After the
measure cloud closes, the household returns to jts.local and sees the
measurement, the proposed correction, the predicted response, and the
spec verdict stated honestly — including "this fails our spec by X in
band Y" — and *then* chooses apply-and-verify, re-measure, or stop.
Verify becomes its own short session. Auto-apply goes away.

## Current state (verified against code at `07896df48` — the review bar)

Seven premises that earlier drafts of this design got wrong. Each was
read in the tree, not inferred from a name.

> **One premise has since been overtaken by a ruling, and is annotated in
> place rather than rewritten** — this block records what was read at
> `07896df48` and is dated for that reason. `_assert_accountable` is no
> longer a veto: item 2 stopped refusing with the nanny burn-down
> ([`measurement-loop-doctrine.md`](measurement-loop-doctrine.md) deviation
> (c)) and item 1 with the realized-level demotion (deviation (i)). Both now
> grade, bank what they measured, and let the round proceed. The premise's
> conclusion is unaffected: what it argued is that removing auto-apply must
> not remove the accountability machinery, and that machinery is still there
> — as a disclosure rather than a stop.

- **There is no "auto-apply-on-improved" gate.** Apply is
  **unconditional**: `"auto_apply": True` is a hardcoded literal in
  `_close_measure_cloud_candidate`
  ([`jasper/active_speaker/crossover_v2_flow.py`](../jasper/active_speaker/crossover_v2_flow.py)),
  carrying the comment *"this is unconditionally True here, not a second
  decision."* What exists is `_assert_accountable`, a **veto** that can
  refuse an apply; `reason="improved"` is a *ledger log reason meaning
  the veto did not fire*, not a trigger. Removing auto-apply is
  therefore "stop calling `_fire_auto_apply` from `authorize`", not
  "delete a condition". **The veto stays** — it is PR-L4 machinery and
  orthogonal to this change.
- **The spec verdict is already consulted — as an exemption, not a
  bar.** The prediction gate abstains early on
  `if after.overall_passed: return`. A prediction that *fails* the spec
  is additionally compared against its own model's residual and must beat
  it by `PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB = 0.5` to be recorded as a
  material improvement. Both sides come from
  the same instrument (`spec_report_for_predicted_sum`), so the room
  cancels; this is a model-vs-model comparison, never model-vs-measured.
  That asymmetry is precisely the hole the review screen closes. (Falling
  short used to refuse the round; since the nanny burn-down it banks
  `LEDGER_NOT_AN_IMPROVEMENT` and the round proceeds — see
  [`measurement-loop-doctrine.md`](measurement-loop-doctrine.md),
  deviation (c).)
- **`provisional` on a baseline profile means something else.** In
  [`jasper/active_speaker/baseline_profile.py`](../jasper/active_speaker/baseline_profile.py)
  it means *"per-driver level match is an unmeasured estimate — run the
  guided level-match"*. It is **not** a verify-outcome flag. "Demote to
  provisional" must **not** reuse it; overloading it would corrupt a
  shipped meaning that `/state.protected_profile` and the sound-profile
  UI already render. The verify-outcome vocabulary that *does* exist is
  `_post_apply_grade` → `GRADE_FAILED` / `GRADE_UNVERIFIED` /
  `GRADE_INCONCLUSIVE` / `GRADE_GRADED` / `GRADE_MARK_VERIFIED`, whose
  docstring already chose **"surface, not auto-restore."**
- **The apply endpoint the review screen needs already exists.**
  `handle_v2_apply` is already an HTTP handler
  (`POST /correction/crossover/v2/apply`), already fingerprint-gated on
  `expected_candidate_fingerprint`, and already stashes
  `pre_apply_profile` — the only place Undo's restore target survives.
  The review screen does not need a new apply path; it needs a button
  that posts to the one that is there.
- **The verify-only prepare's precondition is NOT two things, and stage 2
  is NOT satisfied by construction.** Earlier drafts of this document said
  it was; that was wrong, and the error mattered — it hid a design hole
  (D3 closes it). Two refusals are local: `session_volume_plan()
  .needs_recovery` false, `state["applied"]` truthy. The very next line
  is `context = resolve_conductor_context(status)`
  ([`jasper/web/correction_crossover_v2.py`](../jasper/web/correction_crossover_v2.py)),
  which is fail-closed by design — *"every missing input is a
  `CrossoverV2Refused` naming what to finish first — never a guessed
  default"* — and carries **seven `raise CrossoverV2Refused` sites of
  its own** plus `ensure_crossover_preview_ready()`'s: no active
  crossover, setup not `ready`, a non-2-way preset, the #1821
  driver-safety-profile confirmation gate, woofer+tweeter target
  fingerprints, per-role excitation ceilings, and a declared playback
  device. Its docstring names this caller explicitly: *"`prepare_v2_
  session` calls it before the relay session is registered and the phone
  link minted, and before a verify-only re-arm."* So a box can
  be applied and still be unable to open stage 2. It remains true that
  there is **no fingerprint match and no same-process prior session** in
  the re-arm's own preconditions.
- **`_phase_from_state` returns `PHASE_DONE` for a measure-only
  session** (verified by reading the loop): it walks the recorded
  `session_phases` and returns `PHASE_DONE` once each is accepted. Its
  one special case — `phase == PHASE_VERIFY and PHASE_MEASURE in
  accepted and not applied → PHASE_APPLYING` — cannot fire when VERIFY
  is not in the recorded phases. **A stage-1 session would render
  today's "Your speaker is tuned" done screen with `applied=False`.**
  This is a direct collision, not a theoretical one.
- **The CROSSOVER wizard's own module never branches on `env.screen`.**
  `render(env)` in
  [`deploy/assets/correction/js/crossover/main.js`](../deploy/assets/correction/js/crossover/main.js)
  is fully data-driven off `verdict_text / applied / steps / nudges /
  expert_details / candidate_review / cloud / cloud_chart / relay /
  next_action / alternate_actions`; `env.screen` appears nowhere in
  `crossover/{main,chart,cloud}.js`. A review screen is a **new envelope
  branch with new actions — zero new JS screen logic.** **Narrow claim,
  and the trap is a grep away:** the ROOM-CORRECTION wizard
  [`deploy/assets/correction/js/main.js`](../deploy/assets/correction/js/main.js)
  — a different page in the same directory — branches on `env.screen`
  seven times (`=== 'idle'` ×2, `!== 'idle'`, `=== 'review'`,
  `!== 'review'`, `=== 'result'` ×2) behind a `KNOWN_ENVELOPE_SCREENS`
  allowlist that **already contains a `review` screen**. Grepping
  `env.screen === 'review'` lands in the wrong envelope entirely. The
  two wizards share no screen vocabulary; do not reuse that one's
  `review` semantics.

Two more facts that shape the ladder:

- **`REVIEW_HOLD_BUDGET_S = 30 s` is the tightest window in the
  session**, not the widest — the name is historical. Any design that
  keeps a human decision *inside* the relay session dies in 30 seconds.
  This is the strongest structural argument for the split, and the
  reason the two-stage flow **deletes** the `awaiting_apply` deferred-
  begin path from the critical path rather than tuning it.
- **The predicted response is persisted but not on the wire, and what is
  persisted is not what was graded.** `state["verify_priors"]
  ["predicted_sum"]` exists, decimated by `_decimate_sum` to
  `MAX_PERSISTED_SUM_POINTS = 512`. `spec_report_for_predicted_sum`
  exists and is already run — by `_assert_accountable`, against the
  **full-resolution in-memory tuple**, before that decimation. Nothing
  today grades the persisted curve, and no spec report is persisted at
  all. Neither curve nor verdict reaches
  `crossover_v2_status_block()`. That projection is this ladder's one
  genuinely new plumbing; D4 decides which instrument owns the verdict.

## Adopted decisions

> **D1 is superseded for stage 1 by R15** ([#2106](https://github.com/jaspercurry/JTS/issues/2106)),
> which removes the pre-apply cloud — so the group-close confirm D1 adopts has
> no cloud captures to close on that path, and the machinery below is retained
> for future Room work rather than run. The rest of this section is unaffected.
> See [`linearization-campaign-2026-07.md`](historical/linearization-campaign-2026-07.md).

**D1 — Stage 1 ends at the group-close confirm; nothing is applied.
ADOPT.** The shipped group-close confirm gate is the seam: the
conductor already sets `payload["awaiting_confirm"] = True` for
`PHASE_CLOUD_MEASURE`, and the phone already renders
`renderPlanGroupConfirm`. The existing `retakeControl` / "keep the
earlier measurement" escape hatches survive unchanged: the final
position stays retakeable, which was an explicit owner requirement.
**The PR-6b *fit*-timing invariant is preserved and strengthened** —
PR-6b moved the FIT from MEASURE's acceptance (index 2) to the
pre-apply cloud's close (index 10); the pre-apply cloud was already
captured clean, and apply now moves further away from it, not closer.

**Supersedes** [`linearization-campaign-2026-07.md`](historical/linearization-campaign-2026-07.md)
**§2.6's group-close consequence** — its adopted contract is *"apply
still runs under the same gates; one extra user tap now sits in front of
it"*, i.e. confirm → fit → auto-apply, in-session. This decision keeps
its retake window and reverses its apply. §2.6's retake machinery,
budget, and out-of-order-begin contract are untouched.

**The mechanism, named — because removing the trigger is the whole
risk.** `confirm_cloud_measure_group` is what runs the fit and builds
the candidate, and the candidate is the *proposal* the review screen
shows. It is gated on a begin at an index strictly past the cloud group
(`crossover_v2_flow.py`, `if int(index) <= last_index: return None`),
and today that begin is VERIFY's, posted by `authorize()` in
[`jasper/web/correction_crossover_v2.py`](../jasper/web/correction_crossover_v2.py).
Stage 1 has no entry past the group, so simply dropping VERIFY would
mean the fit never runs and the review screen has nothing to review.
Three changes, and PR-T3's **first and load-bearing** item:

1. **`confirm_cloud_measure_group`'s index gate is replaced by an
   explicit confirmation entry point.** Its `index` contract changes:
   it stops meaning "a begin past the group" and becomes an explicit
   host-called close. The `self._candidate` fire-at-most-once guard is
   what keeps it idempotent, and stays.

   *Amended by the eager-fit rider (2026-07-30).* The two questions
   that guard originally answered together are now separate fields,
   because they are separate questions. `self._candidate` remains the
   fire-once guard. `cloud_measure_group_awaiting_confirm()` — the
   runner's held-set predicate — now reads `_group_confirmed`, "has the
   household confirmed?", and `confirm_cloud_measure_group` asks
   `_group_combined` directly for "is there anything to confirm?".
   Without that split, a candidate built before the confirm would have
   flipped the predicate to False and shut the retake window in the
   same instant it opened.
2. **The phone's group-confirm "Continue" posts a session-completion
   signal** instead of `advanceAfterAccepted`; the host calls the group
   close directly, then ends the session and returns the household to
   the speaker page.
3. **The completion branch must be reordered.** In the pre-split 16-entry
   plan the final cloud position was index 10 of 16, so the confirm screen
   renders. In a 10-entry stage-1 plan that position IS the target,
   `run_capture_plan` emits `capture_set_complete`, and the household would
   get the "All measurements done" screen instead of the confirm the whole
   decision rests on. The confirm test must win.

**Rejected alternative: a sentinel 11th entry to carry the confirming
begin.** It hangs the session. `run_capture_plan` completes at
`capture_target` accepted captures and an entry that is never begun
parks the runner in `awaiting_begin` until `begin_budget(next_index)`
elapses — `DEFAULT_TIMEOUT_S = 120 s`, or `REVIEW_HOLD_BUDGET_S = 30 s`
if the sentinel carried `AUTO_ADVANCE_ON_APPLY` — and the session then
collapses as a timeout. Bounded, but still a manufactured failure at the
exact moment the household is being asked to decide.

**D2 — Stage 2 is a real verify session, tier-matched. ADOPTED;
owner-confirmed 2026-07-29.** The ruling names "the SHIPPED 1-entry
re-verify machinery (`prepare_v2_verify`)". That machinery builds a
**1-entry** verify at the mark. Express's post-apply phase is already
`EXPRESS_CLOUD_VERIFY_POSITIONS = 1`, so for Express the verify-only
prepare *is* stage 2, exactly. **Full tier's post-apply phase is a 6-position
cloud-verify walk** (`DEFAULT_CLOUD_VERIFY_POSITIONS = 6`), and running
Full's stage 2 as a single position would silently discard the
spatially-averaged "after" evidence that the chart's after-curve, the
post-apply spec verdict, and the delta-probe all read. Adopted:
**stage 2 preserves the tier's shipped verify shape** — Express → the
existing 1-entry re-arm; Full → a cloud-verify session of
`DEFAULT_CLOUD_VERIFY_POSITIONS`, built by the same verify-shaped entry
point generalized over the plan shape, not a second parallel builder. The
1-entry form remains what it is today: the recovery re-verify, reachable
from a failed stage 2.

**What "generalized over the plan shape" actually costs — this is real
work, not a parameter.** `build_v2_cloud_index_phase_map` and the
conductor's `index_phase_map` argument both exist, so the seam is there;
what does not exist is a map for a post-apply-only plan. The re-arm
hardcoded `index_phase_map={1: PHASE_VERIFY}` and
constructed its conductor with **no `retain_position`/`publish_cloud`
seams**, explicitly because *"this session's `index_phase_map` is
`{1: PHASE_VERIFY}` only, so it has no cloud group of any kind."*
Stage 2 at Full needs `{1: VERIFY, 2..M: CLOUD_VERIFY}`, both cloud
seams re-threaded, the group's floor honoured
(`MIN_CLOUD_VERIFY_POSITIONS`), and the same verify-priors rehydration
(`predicted_sum`, `gate_window_ms`) the 1-entry path already does. (At
plan time that list also carried the G3 pilot-transfer baseline; #1927
made that reference session-scoped, so it now travels as dated history
only — see `tuning-operator-runbook.md`.)

**Capture arithmetic (corrected — an earlier draft said 11/6).** The
Full plan is 16 entries: CHECK 1 + MEASURE 1 + `N−1` = 8 cloud-measure
(`DEFAULT_CLOUD_MEASURE_POSITIONS = 9`) + VERIFY 1 + `M−1` = 5
cloud-verify (`DEFAULT_CLOUD_VERIFY_POSITIONS = 6`). So **stage 1 is
10 captures and stage 2 is 6**. Express is 7 entries (N = 5, M = 1) →
**stage 1 is 6, stage 2 is 1**.

*(Design-time arithmetic, kept as written. The shipped stage 1 has been
smaller since R15 turned the pre-apply cloud off — today it is 3 captures at
either tier. `tier_display_info()` is the derivation of record; see
`tuning-operator-runbook.md`.)*

**The split reduces the time-budget pressure; it does not resolve it —
do not claim otherwise.** The number to compare is the *realized*
ceiling, not an observed session duration.
`session_wall_clock_ceiling_s(plan)` returns
`min(MAX_WALL_CLOCK_CEILING_S 3600, DEFAULT_WALL_CLOCK_CEILING_S 1800 +
max(0, capture_target − CAPTURE_PLAN_TARGET 3) ×
WALL_CLOCK_CEILING_PER_ENTRY_S 120)` — the walked-away volume ceiling
the session arms for itself, i.e. how long the plan is allowed to be
alive. Today's single 16-entry Full session arms **3360 s**, **3.7×**
the 900 s relay TTL. After the split: stage 1 Full (10) = **2640 s**,
stage 2 Full (6) = **2160 s**, stage 1 Express (6) = **2160 s**, stage 2
Express (1) = **1800 s** — every one still well past 900 s. So the split
lowers the worst case from 3.7× to 2.9× and gives each stage its own
fresh TTL; it does not make either stage fit. The underlying mismatch
between a plan's self-armed ceiling and the relay TTL is a separate,
unresolved duplication (see the traps); this decision inherits it and
must not be written up as having fixed it.

**OWNER CONFIRMATION — RESOLVED 2026-07-29:** Option A is confirmed.
Express remains a single post-apply position; Full remains the
six-position spatial cloud-verify walk. The rejected one-position Full
alternative would land Full in the degenerate case
[`docs/gating-v2-plan.md`](gating-v2-plan.md) already named — *"express
cloud-verify is 1 position → **0 curves → no combine at all**"* — so
Full's post-apply group would produce no combined curve, not a smaller
one. It would also make the user-facing claim inconsistent with the
evidence: `_TIER_CLAIMS` says Full *"re-checks the result at several
spots around the mark"* while Express *"confirms the result at the
mark"*. "Tier-matched" keeps that distinction honest.

**D3 — The review screen is a new envelope branch on the existing
surface. ADOPT — and this is a deliberate reversal.** A dedicated human
review screen existed and was removed on purpose:
`_candidate_review_payload`'s docstring records that its rows are
*"Reused on the RESULT (`done`) screen since the owner ruling
(2026-07-20) removed the dedicated human review screen — the same
numbers now live behind that screen's collapsed expert disclosure"*, and
`ALIGNMENT_CONFIDENCE_TRUST_FLOOR`'s comment records the consequence —
a nudge became a hard gate because *"there is no more human screen to
hand the informed-consent judgment to."* The 2026-07-28 ruling restores
that screen. **The prior art is recoverable, not archaeology:**
`show_during_relay` still exists on `verify_undo` / `verify_remeasure`,
`renderRelay`'s `suppressConnectAffordance` path still exists, and its
copy is still the right copy — *"Your phone is connected — review and
apply below."* `render(env)`'s own comment still reads *"On the review
screen the `show_during_relay` primary (Apply) owns the phone."* Rebuild
against those, do not invent a parallel mechanism.

A new screen id (`review`) emitted by
`build_crossover_envelope_v2` between `PHASE_CLOUD_MEASURE` and
`PHASE_APPLYING`, hosted by the existing
`/correction/crossover/` page and its existing DOM slots
(`#crossover-review` / `#crossover-review-body` are already in the
shell). It renders, in this order:
1. **What we measured** — the pre-apply cloud curve (already on the
   wire as `env.cloud_chart.cloud_measure.curve`) in the chart's
   existing deviation frame, with the per-band verdict from
   `env.cloud.cloud_measure.spec_bands` and the existing carve-out,
   provenance, and geometry-guidance disclosures.
2. **What we propose** — the candidate summary (trims, delay,
   polarity, alignment confidence, predicted ripple) **plus
   `headroom_cost_db`**, the L5 level-cost disclosure.
3. **What we predict** — the predicted response curve and its graded
   spec verdict (D4's plumbing), drawn in the same frame as (1) so the
   two are comparable by eye.
4. **The honest verdict** — when the prediction fails the spec, the
   screen says so in the household's language and names the band and
   the margin. Improved-but-failing is **presented, never applied
   silently.** The improvement number (`before_rms_db`,
   `after_rms_db`, `improvement_db`) is shown as supporting context,
   not as the headline, because it is a model-vs-model figure.
5. **The decision** — `next_action` "Apply and verify" (posts the
   existing `/apply`, then opens stage 2), `alternate_actions`
   "Measure again" and "Leave it as it is". No default, no timer, no
   auto-advance.

**The stage-2 openability preflight (closes the hole premise 5 was
hiding).** The review screen runs `resolve_conductor_context`'s
predicate — the same fail-closed resolution the verify-only prepare will
run — **twice**: once at review render, and again server-side immediately
before the apply commits. A failed preflight names what to finish first
(the refusal text is already household-facing) and **disables the Apply
control**, exactly as D4's ungradeable prediction does. Without this,
the failure mode is precisely the applied-and-ungraded end state this
work order exists to eliminate: a household applies, stage 2 refuses at
open, and the box sits corrected with no verdict. **Shipped precedent to
compose with, not reinvent:** PR #1828 already established "preflight at
session open" as the same predicate moved earlier — `resolve_conductor_
context`'s own docstring records that the #1821 gate moved there because
*"A household with an un-confirmed profile therefore burned a link,
walked to the phone, and hit a deterministic refusal that was knowable
before any of it."* This is the same move, one screen further back.
Ladder placement is split, and the reason is in the ladder note under
PR-T3.

**Durability and staleness of the interlude.** The interlude is untimed,
so the candidate's reachability is a contract, not an accident:
- **Reachable for as long as the durable state holds it.** The candidate
  lives in `/var/lib/jasper/active_speaker_crossover_v2_state.json`
  (`load_v2_state`); there is no TTL and none is added. The review
  screen is reachable across a browser close, a page reload, and a
  `jasper-web` restart. It is NOT reachable across a schema-version bump
  or an unreadable state file — both already resolve to `None`, which
  must render as "the proposal is gone; measure again", never as a blank
  or a stale screen.
- **What invalidates it.** A newer measurement session overwrites the
  candidate. `handle_v2_apply` already refuses a stale one on
  `expected_candidate_fingerprint` mismatch with household-facing copy
  — *"the reviewed crossover is no longer current; review the newest
  measurement before applying"* — and that refusal must render on the
  review screen as itself, with "Measure again" as the primary, not as a
  generic apply failure.
- **"Leave it as it is" does not delete the candidate.** It ends the
  journey and returns to the Active speaker entry screen
  (`/correction/crossover/`); the proposal stays reviewable until
  something invalidates it. Deleting on decline would make an accidental
  tap unrecoverable without re-walking ten captures. The destination is
  named because "the speaker page" was read once as the generic
  `/correction/` hub, which greets with the Room wizard's browser-mic
  HTTPS interstitial — another subsystem's permission flow (#1985).

**`headroom_cost_db` carries a cross-era stamp, and D3 puts it in front
of the household.** Per
[`docs/historical/linearization-campaign-2026-07.md`](historical/linearization-campaign-2026-07.md)
("Cross-era disclosure"): the stamp *"is not re-derived on load,
deliberately"*, so a candidate persisted before that amendment discloses
**~22.5 dB** where re-emitting the same candidate now charges **~5**.
Handling `None` is therefore not enough — PR-T1 must handle *era*: a
pre-amendment stamp is either re-derived or disclosed as a
different-era figure. Rendering it bare would put an order-of-magnitude
wrong level cost on the one screen whose purpose is honesty.

**D4 — Project the prediction onto the wire, graded once. ADOPT.** The
predicted curve and its stored spec report become fields on
`crossover_v2_status_block()` beside the existing cloud blocks; the
curve is decimated by the existing `CHART_CURVE_MAX_JSON_POINTS = 256`
path so the chart feed keeps one decimation owner. The projection
**reads** a verdict; it does not compute one.

**WHICH `predicted_sum` is graded, decided: the grading happens at
PERSIST time and the report is persisted with it.** There are two
different instruments today. `_assert_accountable(predicted_sum, ...)`
grades the **in-memory** `(freqs, magnitudes)` tuple at full resolution
inside `_close_measure_cloud_candidate`. What survives to
`state["verify_priors"]["predicted_sum"]` is `_decimate_sum(...)` —
`MAX_PERSISTED_SUM_POINTS = 512` points, strided. Re-grading the
persisted decimation would be a *different* instrument from the one the
veto used, and the two can disagree on a narrow band — on the one screen
whose entire purpose is the honest spec verdict. Adopted: grade once,
where the full-resolution tuple still exists, persist the resulting
report beside the decimated curve, and have the review screen, the
`/state` block, and the chart all read that one stored verdict. The
persisted curve stays what it is — a drawing, not the instrument.

`None` stays load-bearing throughout: an ungradeable prediction renders
as "we could not predict this" and **disables the Apply control**,
rather than presenting an unevidenced proposal. That refusal is a
user-visible dead end, so it gets
its own named log line in the shipped `correction.crossover_v2_*`
namespace (e.g. `correction.crossover_v2_prediction_ungradeable`,
carrying why) **and a test that pins both the log and the disabled
control** — a disclosure nobody can grep for is not a disclosure.

`_candidate_review_payload` gains `headroom_cost_db` — the code comments
claim that disclosure "lives on the browser-visible candidate summary …
which the envelope's own screens read", and it does not; the review
screen is where that claim becomes true. See D3 for the era caveat that
comes with it.

*(Superseded 2026-08-31 for D5/D6, PR-T2, PR-T5 and Acceptance's Undo
affordance: the product-level Undo verb was removed — the way back is a
republish-then-apply of the prior candidate; see
[crossover-v2-engine-design.md](crossover-v2-engine-design.md) invariant 8.
The decisions and scope named in each stand; the verb they named does
not.)*

**D5 — Verify-failed demotes, and Undo is loud. ADOPT, on the existing
grade vocabulary.** `_post_apply_grade` stays the single owner of "did
this apply survive its verify". A `GRADE_FAILED` / `GRADE_UNVERIFIED`
outcome renders the review surface's failed state with `verify_undo`
as the **primary** action, the applied correction named as not having
passed its own check, and the existing `_failure_envelope` applied-
override behavior unchanged. **`baseline_profile.provisional` is not
touched** (see current-state note 3). The open checklist item already
recorded at `crossover_envelope_v2.py`'s `_failure_envelope` — a
session reset that clears durable v2 state while the graph is live
loses the Undo affordance — is **in scope for this ladder**, because
the two-stage flow makes the window between apply and verify longer
and household-visible.

**D6 — Stage-1 failures never offer Undo. ADOPT.** With a candidate
that was built but never applied, the failure surface must offer
"measure again" / "stop" and must **not** offer to restore something
that was never replaced. `_failure_envelope`'s applied-override is
keyed on `status["crossover_v2"]["applied"]`, which is correctly false
in stage 1 — this decision is mostly a pinning test plus copy, but it
is named because the override's existence makes the wrong behavior a
one-line accident.

**D7 — The orientation page owns expectations and the pattern preview.
ADOPT (#1804 + #1805).** A screen before the first tone that states,
in the household's language: what the speaker will do (tones, sweeps,
how loud), how long it takes, what the household does, and **the whole
walk up front** — every position, previewable and adjustable before
step 1, rather than discovered one prompt at a time. The walk reads as a
walk: start at the mark, out to one side, out to the other, back through
the mark at different heights.

> **D7's PRESENTATION was superseded on 2026-07-31 by
> [#1941](https://github.com/jaspercurry/JTS/issues/1941) R1; its INTENT
> was kept.** The enumerated preview shipped as a second `ui_steps` list
> of every position, stacked under the placement block, and the owner's
> 2026-07-30 field run rejected it: *"a huge block of text below another
> really big block of text… crazy dense with the 10 steps all spelled
> out. The user doesn't know what's gonna happen next, let alone 10
> things from now."* What replaced it is one derived sentence —
> `cloud_walk_shape`, how far the walk reaches from the mark plus the
> promise that each position is prompted — so "no surprises" survives
> while the wall does not. **The adjustable-spacing half of #1805 was
> never built and is not revived by this**; the wide-offset floor below
> still governs if it ever is.

**Units: a recorded owner ruling is being superseded by a newer one, and
the supersession is explicit.** The body-part register is not incidental
copy — it is a ruling recorded in the code
([`jasper/active_speaker/crossover_v2_flow.py`](../jasper/active_speaker/crossover_v2_flow.py),
above `CLOUD_POSITION_PROMPTS`): *"hand-width/forearm language was an
owner request from the **2026-07-25** studio session after numeric
prompts ('move the mic 10 cm left') proved unusable standing next to a
speaker holding a mic stand."* The newer input is field feedback on
[#1805](https://github.com/jaspercurry/JTS/issues/1805), **2026-07-28**:
*"drop body-part units — prompts should use inches and/or meters
('pretty sets of hands and forearms' notwithstanding)."* **The
2026-07-28 field ruling supersedes the 2026-07-25 studio ruling.**
Position copy moves to **inches and/or metres**.

That register is **pinned by a shipped test**, and the pin must be
**re-derived, never merely deleted** — its comment cites the ruling
being superseded, so leaving it would make the suite assert a rule the
owner has withdrawn, and deleting it would leave the new rule unpinned.
Work PR-T4 owns:
- `tests/test_crossover_v2_conductor.py` — the primary pin
  (`assert " cm" not in prompt.text and "centimet" not in
  prompt.text.lower()`, under a comment citing *"the S0 owner ruling:
  hand-widths and forearms, never centimetres"*). Re-derive: update the
  comment to cite the 2026-07-28 ruling, and re-state the assertion as
  what the new ruling requires (numeric units present, body-part units
  absent) rather than what the old one banned. The same file's
  "a two-forearm move is a wide offset" comment goes with it.
- `tests/test_correction_crossover_v2_endpoints.py` — four `"forearms"`
  assertions plus the two prompt fixtures that feed them.
- `tests/js/capture_plan_loop_test.mjs` — the `"two forearms' length"`
  geometry-retry prompt literal.
- **Two Pi-side constants carry the register, not one.**
  `CLOUD_POSITION_PROMPTS` *and* `CLOUD_GEOMETRY_RETRY_PROMPTS` (also
  "two forearms' length"), in the same module.

**The wide offsets are not negotiable, and "adjustable" is bounded by
that — the two are only compatible if the constraint is enforced, not
documented.** #1805 asks to *"let the user PREVIEW and adjust the
movement pattern before step 1 begins (show the whole walk up front,
adjustable spacing)"*. At least two walked positions must still clear
the ≥30 cm spread that the LF-decorrelation side-finding in
[`docs/historical/linearization-campaign-2026-07.md`](historical/linearization-campaign-2026-07.md) makes
load-bearing. Resolution: **adjustment has a floor, and the floor is the
same derived one the plan builder already enforces.** The preview may
widen spacing freely and may narrow the non-wide moves, but a wide
prompt's offset cannot be reduced below the ≥30 cm class, and the
adjusted walk must still satisfy `_min_positions_for_two_wide_offsets()`
— the *same* derivation `MIN_CLOUD_VERIFY_POSITIONS` and Express's
position count already read, so a preview that violates it is refused by
construction rather than by a second copy of the rule. A refused
adjustment says why ("these two spots have to be about a forearm apart —
that spread is what lets JTS tell a room echo from the speaker"), it
does not silently snap.

**SHIPPED IN PR-T4: the preview and the floor. DEFERRED: the household-facing
spacing control.** T4 shipped what the acceptance criterion names — the walk
is previewable up front, stated in inches and centimetres, wide offsets
intact — and shipped the enforcement the adjustment needs, which was the
harder half: a prompt row's distance is now DATA (`offset_cm`), `wide` is
COMPUTED from it against `WIDE_OFFSET_MIN_CM`, `_pose()` refuses below the
HF-decorrelation floor at import time, and both derived group floors re-derive
from the table rather than restating it. Any narrowing therefore moves
`_min_positions_for_two_wide_offsets()` and fails loudly, which is exactly the
"refused by construction" this decision asked for.

> **The PREVIEW half was withdrawn on 2026-07-31 by
> [#1941](https://github.com/jaspercurry/JTS/issues/1941) R1 — see the D7
> callout above.** The walk is no longer "previewable up front": the consent
> screen states its derived REACH in one sentence
> (`crossover_v2_flow.cloud_walk_shape`) and the positions arrive one screen
> at a time. **The FLOOR half is untouched and still shipped** — `offset_cm`
> as data, computed `wide`, the import-time `_pose()` refusal, and both
> re-derived group floors are exactly as described above, and they are what a
> future spacing control would still be enforced by. The deferred
> household-facing control is likewise unaffected: it was never built, and
> #1941 does not revive it.

What is NOT shipped is a control that lets a household choose the PRE-apply
group's spacing. Its home is a rung that owns the **conductor**, not this
one: the conductor still reads the module-level `CLOUD_POSITION_PROMPTS` for
that group at three sites (`_cloud_prompt`'s pre-apply branch,
`_prompt_shown_for`'s retry rungs, and `_close_measure_cloud_candidate`'s
geometry rung), so a per-session table for THIS group is still a conductor
*field* threaded through every construction site — a change in the same
class as the eager-fit rider's `_close_lock`, and one that should land where
the conductor's concurrency and lifecycle are already in hand.

> **The post-apply group already has that field, since the 2026-08-24
> geometry ruling.** `verify_prompts` threads through
> `build_v2_verify_capture_plan`, `build_v2_verify_session_spec`, and
> `CrossoverV2Session.__init__` (resolved once by `verify_pose_table()`,
> where `None` means the ratified `CLOUD_VERIFY_POSE_PROMPTS`), and
> `_cloud_prompt` reads `self._verify_prompts` instead of a module constant
> for `PHASE_CLOUD_VERIFY`. No household-facing picker renders it — the
> field is a caller's choice, not yet a UI control — so "adjustable by an
> editor under the enforced floor, and not by a household" is still the
> honest summary for both tables; only the pre-apply one remains a
> module-level constant with no per-session override at all.

**Sibling copy the reshaped walk orphans — PR-T4 owns all of it:**
- The screen-grammar exemplar in
  [`linearization-campaign-2026-07.md`](historical/linearization-campaign-2026-07.md)
  §2.1, whose worked example is literally `Measurement 4 of 7` /
  `A forearm's length LEFT of the mark` — wrong on both counts after
  this ladder.
- The tier chooser's derived description (`_tier_action`:
  `f"About {estimated_minutes} min — {capture_target} measurements; …"`)
  and the consent screen's derived tier line (`_guided_tier_step`:
  `f"{label}: {walk} measurements, about {minutes} minutes"`). Both are
  correctly derived from ONE plan; after the split there are two, and a
  household choosing a tier is choosing both stages. Neither is a
  hardcoded string to edit — the *derivation* has to learn about stages.
- The consent placement instruction
  (`cloud_walk_placement_instruction`), which promises *"Across about
  {captures} measurements the phone will ask you to move it a little
  between sweeps and to come back to the mark"* — the count is stage 1's
  now, and "come back to the mark" is stage 2's business.
  **Superseded 2026-07-31 by [#1941](https://github.com/jaspercurry/JTS/issues/1941)
  R1: the whole "Across about {captures} measurements…" clause is gone from
  the source, and the function takes no capture count at all. It answers only
  *where do I stand*, because the derived tier line one line above it already
  states the count — saying it twice was half the density the owner
  reported. The quoted sentence is preserved here as the pre-D7 wording this
  item was raised against, not as live copy.**
- `_TIER_CLAIMS`' post-apply claims (Full *"re-checks the result at
  several spots around the mark"* / Express *"confirms the result at the
  mark"*) now describe a session the household starts separately.
- The `M = 1` done-screen placement rule in `build_v2_capture_plan`
  (`if not shape.has_cloud_verify_group: verify_screen.update(done_
  screen)`), which folds the done copy onto VERIFY for Express. That
  rule is stage-2 copy and must move with stage 2.
- **The wizard's `PHASE_DONE` collision is PR-T2's; the PAGE's is not
  yet anyone's.** `renderPlanAllDone` reads the FINAL wire index's
  `done_title`/`done_body` and falls back to *"All measurements done —
  the speaker continues automatically."* A stage-1 plan's final entry is
  a cloud position with neither key, so the household is told the
  speaker continues automatically at the exact moment it deliberately
  will not. PR-T4 owns that fallback.
  **Shipped outcome:** PR-T4 replaced the fallback with the speaker-page
  handoff. Since #2097 an unresolved final group index is a distinct branch:
  stage 1 says the spot was left out before the Continue confirmation, and
  stage 2 repeats the unresolved payload through `capture_set_complete` and
  renders left-out copy instead of the generic done title.

**Recorded deferral — mic-agnostic wording in the REJECTION templates. CLOSED
by PR #1959 (#1941 R4).** The same owner ruling makes "the microphone" the
actor. PR-T4 applied it to every prompt, consent step, placement instruction,
and acknowledgement it owns; the per-reason rejection copy in `REASON_REGISTRY`
([`jasper/active_speaker/crossover_v2_flow.py`](../jasper/active_speaker/crossover_v2_flow.py))
still said "move the phone closer" and similar, and belonged to the reason
registry rather than to this ladder's copy. #1941's stage-2 sweep closed that
registry half — `snr_floor`, `pilot_level_collapse`, and
`verify_level_shift` now name the microphone — and a curated guard
([`tests/test_measurement_vocabulary.py`](../tests/test_measurement_vocabulary.py))
keeps it closed.

**The AGC exception survives, but attributed to the BROWSER rather than the
phone.** Browser AGC really is browser-specific, so that diagnosis must not
blame the capsule — a UMIK-2 has no AGC. The copy therefore names the browser
("This browser is adjusting the microphone level…"), which is both the honest
attribution and mic-agnostic; "phone" was never the load-bearing word, "not
the microphone" was.

**D8 — The phone tells the truth about time, and about CHECK. ADOPT
(#1807 + #1835).** The page surfaces the current step's remaining
window and the session's remaining wall clock, in plain language
("you have about two minutes between taps; this link lasts fifteen
minutes"), and an expiry names **which** budget ran out and what
survives. Two-stage removes some of the pressure — two shorter sessions
with an untimed interlude between them — but D2's realized ceilings say
it does not remove the mismatch, so this is honesty, not headroom. The
expiry disclosure is a user-visible failure surface: it gets a named
line in the `correction.crossover_v2_*` namespace naming which budget
expired (relay TTL / `awaiting_begin` / wall-clock ceiling) and what
survived, **and a test that pins the disclosure to the budget that
actually ran out** (issue #1807) — a disclosure nobody can grep for is
not a disclosure. Separately, CHECK's **pre-arm** copy stops saying
"Measuring room noise — stay quiet." CHECK's ambient window is
deliberately composed to measure the room *before* the household is
asked to go quiet, and the gain solve reads it: the measurement-honest
request is **"keep doing what you were doing."** The two *in-sweep*
ambient lines, which run when quiet genuinely is wanted, are
unchanged — they are a different window with a different purpose, and
this decision must not collapse them into one string. **#1835 asks two
questions and this decision must answer both:** what the phone says
pre-arm on CHECK specifically (answered above), **and whether the
phone's local floor message should differ from the speaker-window
message.** They are not obviously the same sentence — the phone is
telling one person standing in the room what to do right now, the
speaker window is describing a measurement state — and #1835 filed the
second half deliberately. PR-T4 either differentiates them or records
why one string serves both; it does not drop the question.

**#1835's second half — ANSWERED in PR-T4: they differ, and there are THREE
windows, not two.** The phone's own pre-arm window (`captureAmbientNoise`,
≤ 2 s) is a *third* measurement again: it is the phone's floor for the
upload's `noise_floor` field, taken before the speaker plays anything, and it
fires on every capture. The speaker window (`ambient_started`) is the Pi's
program-composed one, whose quiet request is the speaker's call because only
the speaker knows which of ITS two windows is playing. So one string cannot
serve both — the phone is telling one person what to do in the next two
seconds, the speaker window is describing a measurement state of a known
length. Shipped resolution: the phone's pre-arm line became per-entry copy
(`screen.noise_note`), CHECK supplies one that asks the household to carry on,
every other entry supplies none and keeps the page's default (right for them —
a sweep follows immediately), and the in-sweep lines are untouched. Pinned by
`test_check_stops_hushing_the_room_before_it_measures_it` and
`tests/js/capture_time_budget_test.mjs`.

**D9 — No dead air on retry. ADOPT (#1840's page half).** A retry
screen shows progress and its own timeout rather than sitting static
until the 120 s watchdog fires. #1840's Pi half — a retry whose cause
is a solve/level problem must re-solve or refuse loudly rather than
replay — is **out of scope here** and stays on its issue; it belongs to
the retry machinery, not the flow shape.

**D10 — The apply hold leaves the critical path. ADOPT (consequence,
recorded so it is not rediscovered).** With apply completing during the
interlude, `VERIFY_ANCHOR_HOLD_MESSAGE`, the `awaiting_apply`
`CaptureBeginDeferred` path, `renderPlanDeferred`'s 1.5 s repost loop,
and `REVIEW_HOLD_BUDGET_S` stop governing a live session. They are
**not deleted in this ladder** — the 1-entry recovery re-verify and the
`apply_failed` refusal still reach them — but no new design may depend
on them.

**Supersedes** [`linearization-campaign-2026-07.md`](historical/linearization-campaign-2026-07.md)
**§2.2's VERIFY contract.** That section's adopted contract is
begin-first with the household confirming **after apply completes** —
*"the VERIFY tone must not fire until the user confirms after apply
completes"* — which is impossible once apply moves into the interlude:
there is no apply in flight to confirm after. **This is a live collision
between two open work orders, not a historical note — §2.2 is SHIPPED,
not merely adopted.** `build_v2_capture_plan` emits
`confirm_title: "Back on the mark, holding still?"` /
`confirm_body: "Same spot, same height, pointed at the speaker."` on the
VERIFY entry, gates the arm
on the tap; and both `tests/test_crossover_v2_conductor.py` and
`tests/js/capture_plan_loop_test.mjs` pin them. So the confirm-then-tone
grammar §2.2 established **survives and is what stage 2 opens with** —
what is superseded is only its *ordering premise* (that the confirm
follows an in-session apply). PR-T3 must keep the tap and re-anchor it
to stage 2's own begin, not remove it.

**Copy PR-T4 owns, beside `VERIFY_ANCHOR_HOLD_MESSAGE`:** the
group-confirm screen's detail line —
*"JTS tunes the speaker next. Retake this spot first if you want to."*
That sentence becomes false in stage 1: JTS tunes nothing next, the
household decides next. It is the last thing a household reads before
the interlude, so it is the sentence that has to set up the interlude.

## PR ladder

Standard per-PR gate: implementation → independent Opus adversarial
review to 0 blockers / 0 should-fixes → CI green → merge. Serial local
lanes (no `-n`). Corpus tests must report PASSED, not SKIPPED, under
`JTS_FLAT_LIN_CORPUS` and `JTS_FLAT_LIN_S0`.

- **PR-T1 — the wire.** D4 only: grade the prediction where the
  full-resolution tuple lives and persist the report; project
  `predicted_sum` + that stored report onto
  `crossover_v2_status_block()`; add `headroom_cost_db` to
  `_candidate_review_payload`, **era-aware** (a pre-amendment stamp is
  re-derived or disclosed as a different-era figure — see D3). No
  behavior change, no screen change — this rung exists so the review
  screen is built against data that is already proven on the wire.
  Pins: one grading instrument (the persisted report is the one
  `_assert_accountable` saw, not a re-grade of the 512-point
  decimation); `None` propagation for an ungradeable prediction plus its
  named log line; a pre-amendment `headroom_cost_db` never renders bare;
  decimation through the existing owner; the payload's schema-version
  bump.
- **PR-T2 — the review screen.** D3 + D6: the new envelope branch, its
  copy, its actions, the render-time stage-2 openability preflight and
  the Apply control's disabled state, the stage-1 `PHASE_DONE` collision
  fix (a measure-only session must resolve to `review`, never `done` —
  the fix belongs in `_phase_from_state`'s walk, and the corrupt-state
  fallback it already documents must keep working), the JS render path
  (data-driven; no `env.screen` switch — and not the room-correction
  wizard's `review`, see premise 7), and the chart's third curve.
  Pins: a measure-only `session_phases` resolves to `review`; a
  failed-prediction session renders the honest verdict copy and the
  Apply control's enabled/disabled state; a box that cannot resolve a
  conductor context renders the named refusal and a disabled Apply;
  stage-1 failure offers no Undo.
- **PR-T3 — the split.** D1 + D2 + D10. **First and load-bearing: D1's
  three-part mechanism** — `confirm_cloud_measure_group`'s index gate
  becomes an explicit confirmation entry point (its `index` contract
  changes), the phone's group-confirm "Continue" posts a
  session-completion signal the host turns into a direct group close,
  and `runPlanCapture`'s completion branch is reordered so
  `awaitingConfirm` beats `setComplete`/`index >= target`. Then:
  `authorize()` stops firing auto-apply; the apply button posts the
  existing endpoint; the tier-matched stage-2 entry point; §2.2's
  confirm tap re-anchored to stage 2's own begin. **This is the rung
  that changes what the speaker does**, and it lands only after T1 and
  T2 are green. Pins: no code path applies without an explicit household
  POST; the candidate is still built at group close (the fit did not
  move — *re-worded by the eager-fit rider, 2026-07-30: the fit now
  STARTS on the final position's accept, so the pin is the data
  contract — the fit consumes the accepted cloud as of the close — with
  the retake-discard pinned alongside it*); a 10-entry stage-1 plan
  renders the confirm screen, not "All
  measurements done"; Express and Full each open the right stage-2
  shape; **`handle_v2_apply` refuses when the stage-2 preflight fails
  (the pre-POST half of D3's preflight — see the placement note below)**;
  the golden wire pins re-derive by their documented procedure.
- **PR-T4 — the phone's honesty layer.** D7 + D8 + D9: orientation
  page, pattern preview with its enforced wide-offset floor, the
  inch/metre prompt copy **and its re-derived pins across the four test
  files and both prompt constants**, the orphaned sibling copy D7 lists,
  time-budget surfacing with its named expiry log, CHECK pre-arm copy
  **plus #1835's local-floor-vs-speaker-window question**, retry
  progress. Page deploys **before** the Pi, per the shipped
  page-before-Pi ordering.
- **PR-T5 — Undo durability.** D5 plus the recorded `_failure_envelope`
  checklist item: a session reset must not strip the Undo affordance
  while the graph is live.

Sequencing: T1 → T2 → T3 strictly ordered. **T4 is NOT page-only and
NOT parallel with T3** — an earlier draft said it was. D7 edits
`CLOUD_POSITION_PROMPTS` and `CLOUD_GEOMETRY_RETRY_PROMPTS`, Pi-side
constants in `crossover_v2_flow.py`, the same module T3 rewrites the
group-close seam in; and D7's orphaned-copy list reaches
`crossover_envelope_v2.py` and `capture_geometry.py`. T4 lands
**after** T3. T5 after T3.

**Where the stage-2 preflight lands, and why it is split.** The
render-time half is T2 (it is the review screen's own honesty layer, and
disabling a control on a screen whose Apply is not yet the real trigger
is inert but correct). The pre-POST half is **T3**, not T2, for a
verified reason: `handle_v2_apply` is **also today's auto-apply path** —
`_fire_auto_apply`'s worker calls it directly with the candidate
fingerprint. Adding a refusal to it before T3 removes auto-apply would
newly refuse a shipped automatic path on a screen-only rung. T3 is the
rung that makes that POST the sole apply trigger, so gating it there is
exactly on scope. Both halves are in place before any household can
apply from the screen, because T3 is what makes the screen's Apply real.

## Traps (the review hunts these)

- **The wide-offset indices are a computed input, not just copy — and a
  reorder of the pre-apply table now moves ONE number, not two.**
  `_min_positions_for_two_wide_offsets()` derives from the *index of the
  second `wide=True` entry* in whatever table it is handed; called with no
  argument — as `express_cloud_measure_positions()` calls it — that table is
  `CLOUD_POSITION_PROMPTS` (today: wide at 0-based 2, 3, 9, 10 → second wide
  at index 3 → 5), so reordering `CLOUD_POSITION_PROMPTS` moves Express's
  walk length. `MIN_CLOUD_VERIFY_POSITIONS` (the post-apply group's floor)
  no longer shares that call: since the 2026-08-24 geometry ruling gave the
  post-apply group its own pose set, it is checked against
  `_min_positions_for_two_wide_offsets(CLOUD_VERIFY_POSE_PROMPTS)` instead —
  a second, independent derivation over a different table. The reorder is
  **not silent** either way —
  `test_cloud_prompts_front_load_the_wide_offsets` in
  `tests/test_crossover_v2_conductor.py` now asserts
  `MIN_CLOUD_VERIFY_POSITIONS == _min_positions_for_two_wide_offsets(flow.CLOUD_VERIFY_POSE_PROMPTS)`
  as its own check, plus `MIN_CLOUD_MEASURE_POSITIONS >= derived` and
  `express_cloud_measure_positions() == derived` against the pre-apply
  table's `derived`, and fails loudly on any reorder that moves either
  table's second wide prompt. The hazard is not detection, it is the fix:
  any reorder states the new derivation and its resulting counts in the
  PR body, and those pins must be **re-derived**, never adjusted to
  match the new output.
- **`provisional` is not a verify flag.** Anything that writes
  `baseline_profile`'s `provisional` to express "this failed verify" is
  wrong by construction and corrupts a shipped meaning rendered
  elsewhere. Use the grade vocabulary.
- **Do not delete the accountability seam.** `_assert_accountable` is
  not auto-apply; removing it with auto-apply would drop the
  realized-level and prediction ledger together. (It was a veto when this
  was written and refuses nothing now — see the note under "Current
  state" — which makes the ledger the whole of what it produces, and so
  makes this do-not stronger rather than weaker.)
- **The prediction is model-vs-model.** Any copy that presents
  `improvement_db` as evidence about the *room* is measured-narrow-
  stated-wide. The measured evidence on the review screen is the
  pre-apply cloud; the prediction is a model, and the screen says so.
- **`None` is load-bearing on every cloud/spec field.** The compact
  status block never fabricates a clean reading; the review screen must
  render absence as absence and refuse the Apply control, not
  substitute a default.
- **Two ambient windows, two purposes.** CHECK's pre-arm window and the
  in-sweep ambient windows are different measurements. D8 changes one
  string; collapsing them into one is a measurement defect.
- **`renderPlanCountdown` was dead when this was written; it is not any
  more.** #1823 made MEASURE a tap, and at the time no shipped plan reached
  the countdown. The REMOTE commission tier is its first shipped consumer:
  its entries auto-advance because an external positioner, not a hand, moves
  the microphone between them. The stale-countdown symptom named here is
  still closed — do not "fix" it — but the "dead" claim no longer holds.

## Acceptance

The ladder is done when: no code path applies a correction without an
explicit household action; a household that measures sees its own
measurement, the proposal, the prediction, and an honest spec verdict
before deciding; an improved-but-spec-failing fit is presented rather
than applied; a verify-failed apply is named as such with Undo
primary; a stage-1 session never renders "your speaker is tuned"; the
walk's reach is stated up front and its prompts are stated in inches
and/or metres with its wide offsets intact; the phone states which time
budget is running
and what an expiry preserved; **the review screen refuses Apply on any
box whose stage-2 conductor context does not resolve, and
`handle_v2_apply` refuses the same case server-side**; and **no session
in either stage carries a plan longer than today's 16-entry session —
stage 1 is 10 entries and stage 2 is 6 at Full, 6 and 1 at Express, and
each opens its own relay session with its own TTL rather than sharing
one.** (The last criterion replaces an earlier unfalsifiable "both
stages fit inside the relay TTL with the interlude untimed" — they do
not, and D2 says why: every realized wall-clock ceiling still exceeds
900 s. What the split buys is measurable and is what is stated here.
The walk criterion was amended on **2026-07-31** by
[#1941](https://github.com/jaspercurry/JTS/issues/1941) R1, from "the
walk is previewable up front" to "the walk's *reach* is stated up
front": enumerating every position before the first tone was the
density defect #1941 was filed to fix, so requiring it as acceptance
would have held this ladder to a bar the product deliberately no longer
meets. The *prompt* half — inches and/or metres, wide offsets intact —
is unchanged and still enforced by the derived floors.)

Last verified: 2026-07-30
