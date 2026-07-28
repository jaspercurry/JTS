# Two-stage commission flow — work order (issue #1806)

> **Status: adopted work order (2026-07-28).** Anchors
> [issue #1806](https://github.com/jaspercurry/JTS/issues/1806) and the
> owner ruling recorded on
> [issue #1813](https://github.com/jaspercurry/JTS/issues/1813).
> Subsumes the UX backlog filed from the 2026-07-28 field sessions:
> #1804 (orientation page), #1805 (position pattern), #1807 (time
> budgets), #1835 (CHECK pre-arm copy), and #1840's page half (retry
> dead air). Composes with — does not re-open —
> [`docs/flat-linearization-flow-simplification-plan.md`](flat-linearization-flow-simplification-plan.md)
> (screen grammar, tier chooser) and
> [`docs/gating-v2-plan.md`](gating-v2-plan.md) /
> [`docs/room-correction-regime-plan.md`](room-correction-regime-plan.md)
> (the layers this flow commissions).

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
  still proceeds, provided it improves its own model's residual by
  `PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB = 0.5`. Both sides come from
  the same instrument (`spec_report_for_predicted_sum`), so the room
  cancels; this is a model-vs-model comparison, never model-vs-measured.
  That asymmetry is precisely the hole the review screen closes.
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
- **`prepare_v2_verify` needs exactly two things** (verified by
  reading its preconditions): `session_volume_plan().needs_recovery`
  false, and `state["applied"]` truthy. **No fingerprint match, no prior
  session in the same process.** Since apply completes during the
  interlude, stage 2's precondition is satisfied by construction.
- **`_phase_from_state` returns `PHASE_DONE` for a measure-only
  session** (verified by reading the loop): it walks the recorded
  `session_phases` and returns `PHASE_DONE` once each is accepted. Its
  one special case — `phase == PHASE_VERIFY and PHASE_MEASURE in
  accepted and not applied → PHASE_APPLYING` — cannot fire when VERIFY
  is not in the recorded phases. **A stage-1 session would render
  today's "Your speaker is tuned" done screen with `applied=False`.**
  This is a direct collision, not a theoretical one.
- **The wizard JS never branches on `env.screen`.** `render(env)` in
  [`deploy/assets/correction/js/crossover/main.js`](../deploy/assets/correction/js/crossover/main.js)
  is fully data-driven off `verdict_text / applied / steps / nudges /
  expert_details / candidate_review / cloud / cloud_chart / relay /
  next_action / alternate_actions`. A review screen is a **new envelope
  branch with new actions — zero new JS screen logic.**

Two more facts that shape the ladder:

- **`REVIEW_HOLD_BUDGET_S = 30 s` is the tightest window in the
  session**, not the widest — the name is historical. Any design that
  keeps a human decision *inside* the relay session dies in 30 seconds.
  This is the strongest structural argument for the split, and the
  reason the two-stage flow **deletes** the `awaiting_apply` deferred-
  begin path from the critical path rather than tuning it.
- **The predicted response is persisted but not on the wire.**
  `state["verify_priors"]["predicted_sum"]` exists (decimated to
  `MAX_PERSISTED_SUM_POINTS = 512`); `spec_report_for_predicted_sum`
  already grades it. Neither is projected onto
  `crossover_v2_status_block()`. That projection is this ladder's one
  genuinely new plumbing.

## Adopted decisions

**D1 — Stage 1 ends at the group-close confirm; nothing is applied.
ADOPT.** The shipped group-close confirm gate is the seam: the
conductor already sets `payload["awaiting_confirm"] = True` for
`PHASE_CLOUD_MEASURE`, and the phone already renders
`renderPlanGroupConfirm`. Two changes: the host's `authorize()` stops
calling `_fire_auto_apply` (it keeps calling
`confirm_cloud_measure_group`, which is what runs the fit and builds
the candidate — the candidate is the *proposal* the review screen
shows), and the phone's primary control stops calling
`advanceAfterAccepted` and instead closes the session and sends the
household back to the speaker page. The existing `retakeControl` /
"keep the earlier measurement" escape hatches survive unchanged: the
final position stays retakeable, which was an explicit owner
requirement. **The PR-6b apply-timing invariant is preserved and
strengthened** — the pre-apply cloud was already captured clean, and
apply now moves further away from it, not closer.

**D2 — Stage 2 is a real verify session, tier-matched. ADOPT, with one
owner confirmation.** The ruling names "the SHIPPED 1-entry re-verify
machinery (`prepare_v2_verify`)". That machinery builds a **1-entry**
verify at the mark. Express's post-apply phase is already
`EXPRESS_CLOUD_VERIFY_POSITIONS = 1`, so for Express `prepare_v2_verify`
*is* stage 2, exactly. **Full tier's post-apply phase is a 6-position
cloud-verify walk** (`DEFAULT_CLOUD_VERIFY_POSITIONS = 6`), and running
Full's stage 2 as a single position would silently discard the
spatially-averaged "after" evidence that the chart's after-curve, the
post-apply spec verdict, and the delta-probe all read. Adopted:
**stage 2 preserves the tier's shipped verify shape** — Express → the
existing 1-entry re-arm; Full → a cloud-verify session of
`DEFAULT_CLOUD_VERIFY_POSITIONS`, built by the same
`prepare_v2_verify`-shaped entry point generalized over the plan shape,
not a second parallel builder. The 1-entry form remains what it is
today: the recovery re-verify, reachable from a failed stage 2.
Feasibility checked: stage 1 is 11 captures and stage 2 is 6 at Full
tier, both far inside the 900 s relay TTL that a 16-capture single
session was straining. **OWNER CONFIRMATION:** if the intent was that
Full tier *also* drops to a single post-apply position (a real
simplification with a real evidence cost), say so and D2 collapses to
the 1-entry path for both tiers — with the lost spatial evidence
disclosed on the chart, never silently.

**D3 — The review screen is a new envelope branch on the existing
surface. ADOPT.** A new screen id (`review`) emitted by
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

**D4 — Project the prediction onto the wire. ADOPT.** `predicted_sum`
and `spec_report_for_predicted_sum(predicted_sum)` become fields on
`crossover_v2_status_block()` beside the existing cloud blocks,
decimated by the existing `CHART_CURVE_MAX_JSON_POINTS = 256` path so
the chart feed keeps one decimation owner. `None` stays load-bearing
throughout: an ungradeable prediction renders as "we could not predict
this" and **disables the Apply control**, rather than presenting an
unevidenced proposal. `_candidate_review_payload` gains
`headroom_cost_db` — the code comments claim that disclosure "lives on
the browser-visible candidate summary … which the envelope's own
screens read", and it does not; the review screen is where that claim
becomes true.

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
step 1, rather than discovered one prompt at a time. Position copy
moves to **inches and centimetres**; body-part units ("a forearm's
length", "two hand-widths") are retired. The walk reads as a walk:
start at the mark, out to one side, out to the other, back through the
mark at different heights. **The wide offsets are not negotiable** —
at least two positions must clear the ≥30 cm spread that the
LF-decorrelation side-finding in
[`docs/flat-linearization-plan.md`](flat-linearization-plan.md) makes
load-bearing; "gently left, gently right" is a copy goal, not a
geometry change. See the traps: the wide-offset *indices* are a
computed input to Express's position count.

**D8 — The phone tells the truth about time, and about CHECK. ADOPT
(#1807 + #1835).** The page surfaces the current step's remaining
window and the session's remaining wall clock, in plain language
("you have about two minutes between taps; this link lasts fifteen
minutes"), and an expiry names **which** budget ran out and what
survives. Two-stage already removes most of the pressure — two short
sessions with an untimed interlude between them — so this is honesty,
not headroom. Separately, CHECK's **pre-arm** copy stops saying
"Measuring room noise — stay quiet." CHECK's ambient window is
deliberately composed to measure the room *before* the household is
asked to go quiet, and the gain solve reads it: the measurement-honest
request is **"keep doing what you were doing."** The two *in-sweep*
ambient lines, which run when quiet genuinely is wanted, are
unchanged — they are a different window with a different purpose, and
this decision must not collapse them into one string.

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
on them, and the copy that tells a household to "put the phone back on
the mark while that finishes" is rewritten for a flow where nothing is
finishing.

## PR ladder

Standard per-PR gate: implementation → independent Opus adversarial
review to 0 blockers / 0 should-fixes → CI green → merge. Serial local
lanes (no `-n`). Corpus tests must report PASSED, not SKIPPED, under
`JTS_FLAT_LIN_CORPUS` and `JTS_FLAT_LIN_S0`.

- **PR-T1 — the wire.** D4 only: project `predicted_sum` + its graded
  spec report onto `crossover_v2_status_block()`; add
  `headroom_cost_db` to `_candidate_review_payload`. No behavior
  change, no screen change — this rung exists so the review screen is
  built against data that is already proven on the wire. Pins: `None`
  propagation for an ungradeable prediction, decimation through the
  existing owner, and the payload's schema-version bump.
- **PR-T2 — the review screen.** D3 + D6: the new envelope branch, its
  copy, its actions, the stage-1 `PHASE_DONE` collision fix (a
  measure-only session must resolve to `review`, never `done` — the
  fix belongs in `_phase_from_state`'s walk, and the corrupt-state
  fallback it already documents must keep working), the JS render path
  (data-driven; no `env.screen` switch), and the chart's third curve.
  Pins: a measure-only `session_phases` resolves to `review`; a
  failed-prediction session renders the honest verdict copy and the
  Apply control's enabled/disabled state; stage-1 failure offers no
  Undo.
- **PR-T3 — the split.** D1 + D2 + D10: `authorize()` stops firing
  auto-apply; the phone's group-close confirm closes stage 1 and
  returns to the speaker; the apply button posts the existing endpoint;
  the tier-matched stage-2 entry point. **This is the rung that changes
  what the speaker does**, and it lands only after T1 and T2 are green.
  Pins: no code path applies without an explicit household POST;
  `confirm_cloud_measure_group` still fires (the candidate is still
  built); Express and Full each open the right stage-2 shape; the
  golden wire pins re-derive by their documented procedure.
- **PR-T4 — the phone's honesty layer.** D7 + D8 + D9: orientation
  page, pattern preview, inch/centimetre prompt copy, time-budget
  surfacing, CHECK pre-arm copy, retry progress. Page deploys **before**
  the Pi, per the shipped page-before-Pi ordering.
- **PR-T5 — Undo durability.** D5 plus the recorded `_failure_envelope`
  checklist item: a session reset must not strip the Undo affordance
  while the graph is live.

Sequencing: T1 → T2 → T3 strictly ordered. T4 is independent of T3 and
can land in parallel. T5 after T3.

## Traps (the review hunts these)

- **The wide-offset indices are a computed input, not just copy.**
  `_min_positions_for_two_wide_offsets()` derives Express's position
  count from the *index of the second `wide=True` entry* in
  `CLOUD_POSITION_PROMPTS` (today: wide at 0-based 2, 3, 9, 10 → second
  wide at index 3 → Express N = 5). **Reordering the prompt table
  silently changes how many positions Express walks.** Any reorder
  states the new derivation and its resulting counts in the PR body,
  and the tier-count pins must be re-derived, not adjusted to match.
- **`provisional` is not a verify flag.** Anything that writes
  `baseline_profile`'s `provisional` to express "this failed verify" is
  wrong by construction and corrupts a shipped meaning rendered
  elsewhere. Use the grade vocabulary.
- **Do not delete the accountability veto.** `_assert_accountable` is
  not auto-apply; removing it with auto-apply would drop the
  realized-level and prediction ledger together.
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
- **The relay TTL is duplicated and unpinned.** `900` is an
  independent literal in `relay/src/worker.js` and
  `jasper/capture_relay/session.py`, **and** hardcoded again in
  `jasper/capture_relay/correction_adapter.py`, which is the value the
  v2 path actually gets (the v2 callers do not pass `ttl_s`). Unlike
  `MAX_CAPTURE_PLAN_ATTEMPTS` there is no equality test. Any PR that
  changes a session's time budget resolves that duplication first, or
  states explicitly that it did not touch it.
- **`renderPlanCountdown` is dead.** #1823 made MEASURE a tap, and the
  source says no shipped plan reaches the countdown; a test pins it.
  Do not "fix" the stale-countdown symptom there — it is already
  closed, and the live countdown is the in-sweep ambient timer.
- **Page before Pi.** The capture page lives at `capture-page/` in the
  repo root (not under `deploy/assets/`), ships via its own build, and
  deploys before the Pi so a new page never meets an old conductor.

## Acceptance

The ladder is done when: no code path applies a correction without an
explicit household action; a household that measures sees its own
measurement, the proposal, the prediction, and an honest spec verdict
before deciding; an improved-but-spec-failing fit is presented rather
than applied; a verify-failed apply is named as such with Undo
primary; a stage-1 session never renders "your speaker is tuned"; the
walk is previewable up front and stated in inches and centimetres with
its wide offsets intact; the phone states which time budget is running
and what an expiry preserved; and both stages fit inside the relay TTL
with the interlude untimed.

Last verified: 2026-07-28
