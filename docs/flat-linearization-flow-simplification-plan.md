# Flow simplification: express commission + one-instruction-per-step UX

**Status: design work order (2026-07-27).** Owner-mandated next phase of the
flat-linearization program, after the 16-capture cloud instrument shipped
(PRs #1749–#1762) and its first real session ran on JTS3. This doc is the
reviewed design; the PR ladder at the bottom executes it. Parent plan:
[`flat-linearization-plan.md`](flat-linearization-plan.md); shipped
instrument: [`flat-linearization-productization-plan.md`](flat-linearization-productization-plan.md);
operational reference: [`HANDOFF-crossover-measurement-v2.md`](HANDOFF-crossover-measurement-v2.md).

## 0. The mandate (owner, 2026-07-27, after running the flow)

1. **Spoon-fed, one-instruction-per-step UX.** Today the position prompt is
   a small muted note under a headline that is just a counter. Required
   shape: before measuring, tell the user the plan ("we're going to take N
   measurements"); each step shows ONE clear instruction in the SAME
   visible place every time; the user confirms — THEN the tone plays.
2. **Fewer measurements — an express tier.** ~3–4 positions with honestly
   degraded claims. Full commission (the current 16-capture instrument)
   remains for first-run / precision work.
3. Express sessions should land well under 5 minutes (full walk today is
   ~10–15 min wall clock).

What makes express *honest* now: the owner's new horn removed the deep
8–16 kHz comb **on JTS3**. Evidence: the first real cloud session
(2026-07-27, session `cap_4NUGqx3yIzSuv4ta2ozfKw`, bundle
`d5b171fa81a5` on JTS3) measured every null depth ≤ 1.6 dB — all below
the 2.5 dB materiality floor (`DEFAULT_MIN_NULL_DEPTH_DB`,
`jasper/audio_measurement/interference_nulls.py`), registry honestly
empty — against the old horn's source-fixed 5.4–7.0 dB comb
([`flat-linearization-plan.md`](flat-linearization-plan.md) S0 ground
truth). That session ran with an uncalibrated echo-analysis band
(issue #1763, fixed by PR #1764 before this ladder), so the figure is
exploratory until the confirmatory calibrated session — and it is a
**single-speaker hardware fact**, not a product-wide one. The biggest
historical reason for many positions was HF-null decorrelation; on a
speaker whose horn (or baffle) still combs, a 4-position cloud
decorrelates HF nulls less well — which is why §1.3 carries an
HF-null row and §3 recommends Full on a first-ever commission. With
the comb gone, a small cloud's remaining job is LF support and outlier
rejection, which fewer — but wide — positions can carry, with the
degradation disclosed. (PR-U3 adds a dated pointer note to the parent
plan so its old-horn comb figures and this result don't read as
contradicting canon.)

**Owner feedback round (2026-07-27 evening, on the draft):**

4. The express-vs-full choice belongs to the **user**, explicitly —
   not just a history-picked default (→ §3, revised).
5. The courtesy tones' pacing is wrong on hardware: three beeps, then
   "quite a long gap", then the sweep. Expected: beeps → a few seconds
   → sweep (→ §2.5).
6. After each measurement the user should be able to **retake that
   measurement** (just because they want to), go **next**, or **stop**
   (→ §2.6).
7. The sweep glitch the last session logged ("The capture glitched —
   measuring again") needs a root-cause investigation — tracked
   separately, not in this ladder (§5; a dedicated session owns it).

---

## 1. Express tier — shape decision

### 1.1 The shape: 7 captures

```
CHECK (mic check, at the mark)                          1
MEASURE (design-axis anchor, at the mark)               1
CLOUD_MEASURE × 4 prompted positions (2 wide)           4
  └─ group close → fit + auto-apply (PR-6b semantics unchanged)
VERIFY (back at the mark, tracking ±1.5 dB)             1
                                                 target 7
```

In the existing count vocabulary (`cloud_capture_target = 1 + N + M`):
**express = (N = 5, M = 1)** — N counts the MEASURE anchor plus 4 prompted
positions; M = 1 is the VERIFY anchor alone, i.e. **no cloud-verify
group**. Full stays (N = 9, M = 6) = 16, untouched.

Why 4 prompted positions and not the floated 3:

- **Both wide offsets come free.** `CLOUD_POSITION_PROMPTS` front-loads
  its wide (≥ 30 cm) moves at table offsets 2 and 3 (0-based — the code
  comment above the table counts them 1-based as "3 and 4";
  `test_cloud_prompts_front_load_the_wide_offsets`). A 4-position group
  walks offsets 0–3 and picks up both wide moves from the already-shipped,
  already-validated table — a short left/right pair plus a wide left/right
  pair (12 cm then 40 cm since the two-stage ladder's D7 restated them in
  inches and centimetres), which is the owner's "middle, left, right" at two
  scales. Three prompted
  positions would capture only one wide move, violating the ≥ 2-wide
  constraint the plan adjudicated as load-bearing for the LF edge
  (the cloud's LF common-mode bounce lift does not converge with N at
  narrow spreads; wide offsets are what help — parent plan, two-path
  inversion side-finding).
- **`thin_evidence` stays reachable.** The geometry disclosure's
  qualifier is `n_confident == 2 and n_positions >= 4`
  (`spatial_combine.assess_geometry`). At 3 positions it is structurally
  unreachable — a barely-corroborated lock would be reported with full
  confidence. At 4 it keeps its designed semantics.
- **The envelope's evidence discount (σ/√N) applies at whatever express
  actually measures.** `position_stability_limit`'s S0 calibration
  (`tests/test_active_speaker_linearization_envelope.py`) is **not
  monotone in N**: 10 positions → 12.26 dB, but the one 4-position
  reading (7.92 dB) comes from a deliberately degenerate one-height
  subset (`S0_MAIN_HAND_WIDTH_LOW`) whose σ is inflated by the
  documented clean break at cloud_07, and a 6-position one-height
  subset reads 24.00 dB — *looser* than the full cloud, and since
  2026-08-02 (#2045) it sits at the ceiling sentinel outright, so the
  non-monotonicity is now as stark as it can get. Express's
  dispersed 4-position walk is structurally unlike any of those
  subsets, so **its limit is unmeasured until the JTS3 product smoke**.
  The honest statement is only this: the term is present, computed
  from express's own spread, and will bind or not accordingly — no
  code change either way.

Geometry retakes stay enabled with the same budget
(`GEOMETRY_RETRY_POSITIONS = 2`), so a locked express cloud can grow to 9
captures worst case — still comfortably under the relay ceiling
(`max_attempts = 7 + 2 + 5 = 14 ≤ 32`) and the wall-clock ceiling
(`1800 + (7−3)·120 = 2280 s`).

**Duration — two honest numbers, keep them straight.** The composed
program audio for 7 captures is ≈ 2.4 min (measured through
`build_v2_capture_plan` at JTS3-like bands: check 22.8 s + measure
40.9 s + 5 × 16.2 s). The capture page's estimator
(`capture-page/js/main.js::wakeLockHintText` — per-entry `duration_ms`
+ a deliberately generous 20 s/capture allowance, `Math.ceil` to
minutes) **displays 5 minutes** for that plan (4.75 ceiled), and 11
for the full tier. All displayed numbers derive from the plan —
nothing hardcoded — so wizard/consent copy MUST say "about 5 minutes"
(and "about 11" for full), never a prettier hand-written figure. The
mandate's "well under 5 minutes" is a statement about expected real
wall clock (audio + four short moves), not about the conservative
display.

### 1.2 The N-floor decision

`MIN_CLOUD_MEASURE_POSITIONS = 6` is the **full tier's** validated floor
and does not move. Express is not a loosened floor — it is a **distinct,
named plan shape** with its own validation:

- New tier vocabulary in `crossover_v2_flow.py`: `TIER_FULL` /
  `TIER_EXPRESS`, resolved once into a single plan-shape value (N, M,
  tier id) that is threaded to **both** `build_v2_session_spec` and
  `build_v2_cloud_index_phase_map`. (Today
  `prepare_v2_session` calls both with defaults and passes counts to
  neither; threading them from one resolved value closes the desync
  hazard.)
- Tier-aware validation: express admits exactly (N =
  `_min_positions_for_two_wide_offsets()`, M = 1) — **derived from the
  prompt table, never the literal 5**, exactly as
  `MIN_CLOUD_VERIFY_POSITIONS` already is and for the same reason: if
  the table's wide moves are ever reordered, the floor must move with
  them rather than leave express silently one-wide. Extend
  `test_cloud_prompts_front_load_the_wide_offsets` to pin the express
  constant alongside the two existing floors. Full keeps the existing
  (6 ≤ N ≤ 12, M ≥ 5) rules. An M = 1 plan emits no cloud-verify
  entries and moves the `done_title`/`done_body` screen onto the
  VERIFY entry.
- The tier rides the durable v2 state, the pipeline payload, and
  `/state`, so every consumer can tell which instrument produced a
  result (same unknown-vs-default rule as `echo_band_provenance`,
  issue #1763).

### 1.3 Degraded-claims table (what express claims, what it stops claiming)

| Surface | Full (N=9, M=6) | Express (N=5, M=1) |
|---|---|---|
| Correction fit evidence | 8-position power-mean cloud | 4-position power-mean cloud; the envelope's σ/√N position-stability term computes from express's own spread (§1.1 — its limit is unmeasured until the JTS3 smoke) — **automatic** |
| HF-null decorrelation | 8 dispersed positions | 4 positions decorrelate HF nulls less well on a speaker that still combs — the premise for express is a comb-free source (§0); on a combing speaker the registry/carve-out machinery still refuses honestly, but with less evidence. §3's first-run Full recommendation exists for this row |
| Outlier exclusion screen | power-vs-median over 8 | over 4 (weaker; a single bad take is harder to identify) — **automatic**, disclosed |
| Echo/geometry adjudication | n = 8, `thin_evidence` cliff at 2-of-≥4 | n = 4, same cliff semantics — **automatic** |
| Null registry / carve-outs | full corroboration budget | fewer corroborations → more `insufficient_evidence` refusals — **automatic** (the detector refuses rather than guesses) |
| Pre-apply spec verdict | cloud spec gauges on the measure cloud | same gauges on the 4-position cloud, tier-qualified |
| **Post-apply spec verdict** | cloud-verify group (5 positions) re-measures the applied result across the cloud | **ABSENT.** Express verifies tracking at the mark only (±1.5 dB, `VERIFY_TOLERANCE_DB` unchanged). No cross-position post-apply claim — disclosed on the done screen, in the wizard, and in `/state` |
| Before/after chart (PR-7) | both phases | "before" cloud + VERIFY tracking; the "after" cloud panel is absent and the callouts say why |
| Wall clock | ~10–15 min (page displays 11) | expected ~3–5 min real; page displays 5 (§1.1) |

The disclosure rule follows the program's standing prose discipline:
never claim wider than measured. Express copy says what it verified
("tuned and confirmed at the mark") and names the upgrade path ("run a
Full measurement for the result checked at several spots around the
mark" — B2 fix, adversarial review of PR #1780: "the verified-everywhere
result" overclaimed past what a Full measurement actually re-checks, a
handful of prompted spots around the mark, never every point in the
room). Nothing is silently weakened: every row above is either the
existing machinery scaling itself down, or an absence that is stated.

### 1.4 Consent for express

The placement policy is unchanged — the mic starts at the mark, moves
only when asked, holds still during sweeps — so
`summed_guided_cloud_v1` remains the policy id and the acknowledgement
keeps deriving its capture count from `plan.capture_target` (7). The
consent screen gains one tier-derived line (quick tune vs full, with the
derived duration) so the user knows which instrument they are consenting
to. (Review finding N6: an earlier draft of this section claimed the
stationary 1-entry re-verify's consent copy "stays byte-identical" —
false against §2.4's own "no re-walk" copy change to that exact screen;
retired here rather than left standing next to the section that
supersedes it. See §2.4 for what changes and why.)

---

## 2. UX redesign — the screen grammar (both tiers)

### 2.1 The inversion

Today's step screen headlines the **counter** ("Spot 4 of 9 — hold
still", 1.5 rem) and whispers the **instruction** (0.9 rem muted
`cap-note`). A second, disagreeing counter renders below in `#status`
("Measurement 3 of 16 done…"). The redesign inverts this. Every step
screen renders exactly this grammar, in this order, in the same DOM slots
every time:

```
[eyebrow]   Measurement 4 of 6            ← small; the ONLY counter
[headline]  Move the microphone 16 in     ← the instruction, 1.5rem+,
            (40 cm) to the LEFT of the      one imperative sentence
            mark, at mark height.           stating a COMPLETE pose
[detail]    Step a little toward the      ← ≤ 1 short supporting clause;
            speaker as you go out…          may be empty
[action]    [ I'm there — play the tone ] ← single full-width primary
[budget]    You have about 2 minutes      ← quiet; present only when the
            between taps…                   Pi published its budgets
[stop]      Stop measuring                ← small text link; its tap
                                            opens a danger-styled
                                            confirm (see below)
```

**The worked example is current as of the two-stage split** (work order D7).
Two things in it moved and both were load-bearing: the counter is stage 1's
(6 for a Quick tune, 10 for a Full measurement — not the whole journey's), and
the instruction is a numeric ABSOLUTE pose in inches and centimetres. The
body-part register the earlier example used ("A forearm's length LEFT") was
withdrawn by the 2026-07-28 field session (issue #1805); the relative phrasing
was withdrawn by the 2026-07-29 one (issue #1806), which is also the likeliest
cause of the position clustering that trips the geometry lock (issue #1874).
The `[budget]` row is D8's and is absent when the Pi published no budgets.

**Stop demotion is a deliberate reversal of a documented decision.**
`render.js` styles Stop as the page's one danger-red button *on purpose*
("the one button … whose tap is destructive to the in-progress
measurement") — this stays true for the consent-screen Stop `render.js`
still renders (room-sweep, level-ramp), where nothing is in progress yet
(nit fix, adversarial review of PR #1780: the earlier draft also named
the page-owned screens' Stop helper by its pre-redesign function name,
which PR-U2's own implementation removed). The redesign keeps the
destructiveness honored but moves it to the right layer: the
always-present control shrinks to a text link (it appears 16× per full
session and competes with the primary at equal weight today), and the
tap opens a danger-styled confirm ("Stop measuring? This abandons the
session") so a stray tap can't kill a session — which the current
full-weight button actually can. Owner-reversible (§5).

Server side: `CapturePlanEntry.screen` (an opaque `str → str` dict — no
relay/protocol change) carries `progress`, `title` (now the
instruction), and `body` (the detail clause). The old page renders new
plans gracefully (it already shows `title` big and `body` small — the
instruction simply becomes the headline); the new page renders old plans
via the same fallbacks. **One deliberate exception: the VERIFY entry's
`title`/`body` are NOT repurposed** — an old page renders them as the
apply-hold heading (`renderPlanDeferred`), and
`validate_capture_page` enforces only a build-stamp *format*, never a
minimum build, so a phone with a cached pre-redesign bundle is
admitted. The post-apply confirmation instruction therefore rides a
**new** screen key the old page ignores (§2.2), keeping the fallback
claim true for every entry. `CloudPositionPrompt` splits into
(headline, detail, wide) so each move is authored as one short imperative
plus at most one supporting clause, instead of a 2–3 sentence paragraph.

**One counter.** "Measurement N of T" (whole-session), server-derived
into `screen.progress`. The per-group "Spot i of n" vocabulary is
retired. `#status` stops echoing counts entirely — it becomes the
transient state channel only ("Playing the measurement tone…",
"Checking this measurement…").

### 2.2 Confirm-then-tone, everywhere a move happened

- **Every entry that asked the user to move the mic is tap-gated**
  (already true for cloud positions: `AUTO_ADVANCE_TAP`). The tap label
  does the confirming: "I'm there — play the tone", not "Next".
- **MEASURE keeps its 5 s cancelable countdown** — deliberate exception:
  no movement happens between CHECK and MEASURE (same mark), so there is
  no placement to confirm, and the cancel affordance preserves control.
  Owner-reversible; flagged in §5. **REVERSED 2026-07-28 (issue #1823),
  after the owner ran it:** the reasoning held for placement but missed
  level — MEASURE is the longest capture of the session and the one that
  can be its loudest, and rolling into it unasked contradicted the
  confirm-then-tone grammar's spirit. It is now `AUTO_ADVANCE_TAP` with
  copy that sets the expectation ("longer, and can be the loudest — it
  measures each driver alone at the level it needs to hear each one
  clearly"; the hedge is load-bearing since #1825/#1829 solve each
  driver's level to the SNR the fit needs, so a quiet room gets a quiet
  MEASURE — and the tail says what the level is FOR rather than naming
  the stage that asked for it, per the 2026-07-28 plain-language ruling).
  The countdown vocabulary stays in the plan grammar and on the page for a
  future same-spot transition that earns it.
> **Partially superseded (2026-07-28) by
> [`docs/two-stage-commission-flow-plan.md`](two-stage-commission-flow-plan.md)
> (issue #1806).** This section's *ordering premise* — that the user
> confirms **after apply completes** — does not survive apply moving out
> of the relay session into the review interlude. The confirm-then-tone
> tap itself is SHIPPED and survives: it becomes the tap that opens
> stage 2. Read the two-stage work order for the current contract.

- **VERIFY becomes hold-then-tap — the step-11 fix.** Today
  `AUTO_ADVANCE_ON_APPLY` fires the verify sweep with *no tap at all*
  once the apply completes, racing the user's walk back to the mark (the
  2026-07-27 session record: collapsed-gate capture from mic placement,
  then a 1.73 dB vs 1.5 dB tracking miss with walk-back placement
  suspect). New contract — **begin-first, and this ordering is
  load-bearing**: the page posts `begin_capture` immediately as today
  (each `capture_deferred` retry re-arms the host's `awaiting_begin`
  clock during the apply hold — sitting tap-first in `awaiting_begin`
  would hit `REVIEW_HOLD_BUDGET_S = 30 s` and kill the session as a
  `relay_timeout`); the hold screen instructs the walk-back
  ("Applying — walk back to the mark now"); when authorization lands,
  the page renders the standard step grammar ("Back on the mark,
  holding still?" / "I'm there — play the tone") and proceeds to
  mic/ambient/`armed` **only after the tap**. The tap wait lives in the
  host's `awaiting_arm` phase, whose budget is `DEFAULT_TIMEOUT_S =
  120 s` (`jasper/capture_relay/session.py`) — the 60 s acceptance
  criterion fits inside it. The entry KEEPS
  `auto_advance=AUTO_ADVANCE_ON_APPLY` (so `begin_budget`'s hold
  semantics and the `AUTO_ADVANCE_ON_APPLY` pins in
  `tests/test_capture_relay_plan.py` stay live, no orphaned
  constants); the post-apply confirmation copy rides a new screen key
  (e.g. `confirm_title`/`confirm_body`) that an old page ignores — an
  old page keeps today's exact behavior, hold copy included (§2.1).
  Acceptance criteria: *the VERIFY tone must not fire until the user
  confirms after apply completes; the session must tolerate at least a
  60 s tap delay without expiring; an old-build page against a new
  plan behaves byte-for-byte as today.* No conductor state-machine
  change: `authorize_begin` is bookkeeping, playback happens at
  `on_armed`, and a delayed arm is acoustically inert (the session
  wall clock already scales via `session_wall_clock_ceiling_s`).
- `VERIFY_TOLERANCE_DB = 1.5` does not move. The tap gate attacks the
  placement-rush cause; the tolerance is a spec constant, not a UX knob.
  If misses recur after this lands, that is a separate calibrated
  decision.

### 2.3 The plan announcement (before any sweep)

The consent screen is restructured to *be* the announcement, so the plan
is stated before the first tone and never only implied:

- Heading: fix the `"Crossover — crossover"` artifact (from
  `ui_heading(f"Crossover — {driver_label}")` with
  `driver_label="crossover"`) → "Tune your speaker".
- First line, tier-derived: "**N measurements, about M minutes.** First
  from the mark, then from {N−2} nearby spots your phone walks you
  through{, then a final check back at the mark}." Counts and minutes
  derive from the plan (the page already computes the minutes for the
  wake-lock hint; the consent line reuses that derivation).
- Then the existing placement instruction (mark definition), the
  acknowledgement, and the start button ("The mic is on the mark — start
  measuring").
- Clutter removed at the same time: the dead `level_meter` component
  stops being emitted by `build_crossover_sweep_spec` — **scope note:
  that builder serves every crossover consent screen** (the v2 cloud,
  the legacy per-driver sweeps, and the 1-entry re-verify), and the
  meter is dead on all of them (`updateLevelMeters` is fed only by the
  level-ramp protocol), so the removal is intentionally that wide; the
  `ui_level_meter` *builder helper* stays (it is pinned as a builder in
  `tests/test_capture_relay_spec.py` and the level-ramp flow still
  uses it). The mic picker collapses once the session mic stream
  exists (it is locked to one stream after the first capture anyway);
  the wake-lock hint remains the fallback-only line it is today.

### 2.4 Retry and recovery screens use the same grammar

- Rejected-capture retries: eyebrow "Measurement 4 of 7 — one more try",
  headline = the retry instruction (for geometry retakes, the wider-spot
  prompt), detail = the reason sentence from `REASON_REGISTRY`.
- **The 1-entry re-verify recovery says the cheap thing loudly.** Its
  consent + step copy leads with: "One sweep, back at the mark — you do
  **not** need to redo the walk." (The 2026-07-27 session abandoned this
  recovery because the screen didn't make its cheapness obvious.) This
  copy change applies to `build_v2_verify_capture_plan` and the
  re-verify spec's consent steps.
- Terminal screens (done / refused / exhausted / expired) keep their
  current set (no new terminal states), restyled to the grammar. The
  express done screen carries the tier disclosure (§1.3).

### 2.5 Courtesy-tone pacing (owner-observed defect)

Observed on hardware (2026-07-27): three courtesy beeps, then a long
gap, then the sweep. Located composition: the #1677 prelude is
*prepended to the whole program* — beeps, then the owner-specified
~3 s settle (`COURTESY_TONE_TRAILING_SILENCE_S = 3.0`,
`jasper/audio_measurement/program.py`), then the program's own lead-in
(the per-capture behavioral-linearity pilot pair with its trailing gaps,
`_append_leading_pilot_pair`) before the first sweep — so the
beep-to-sweep interval stacks well past the intended ~3 s, and any
host-side room-listening wait adds on top.

**Required outcome (acceptance criterion, not a prescribed diff):** the
interval from the last courtesy beep to the first audible measurement
content is the ~3 s settle and nothing more. Everything the program
needs that is not the stimulus itself (pilot pair, any listening
window) moves *ahead of* the beeps — the beeps' whole meaning is "the
sweep is imminent, go quiet now". Candidate reorder: pilots → beeps →
settle → sweeps (the pilot reader locates segments by recorded offsets,
so order should be free — the implementer verifies, and the analysis
tests plus a program-composition test deriving the beep-to-stimulus
interval pin it). Golden wire pins re-derive (program durations shift).
On-device listen required before merge (this defect was only audible,
never visible in tests).

> **Amendment, 2026-07-28 (issues #1810 / #1812).** The paragraph above
> is preserved as written; two of its premises turned out to be wrong,
> and the implementation that followed them (#1771, the "candidate
> reorder" taken literally) shipped a worse defect than the one it fixed.
> Recorded here rather than rewritten, because the reasoning error is the
> useful part.
>
> **Premise 1 — "the pilot pair is not the stimulus itself" — is false.**
> A leading pilot is a full-gain band-limited chirp, as loud as the sweep.
> Moving it ahead of the beeps meant MEASURE and VERIFY opened on two
> audible chirps at t=0 with the 6 dB quieter warning arriving ~4 s later.
> The deferred on-device listen happened on 2026-07-28 and the owner heard
> exactly that: "sweeps then beep beep beep." The ordering promise is
> therefore **not** "beeps immediately before the sweep" but **"the beeps
> precede every audible thing in the program"**, with the forward settle
> bound preserved on top of it. The acceptance test pinned only the
> forward interval, which is why nothing caught this; the backward pin
> ("no audible content precedes the first courtesy beep") now exists too.
>
> **Premise 2 — "any listening window" belongs ahead of the beeps — is
> false for the window MEASURE/VERIFY needed.** #1810 found the pilot SNR
> guard structurally dead on every phase but CHECK, for want of a
> room-listening window ahead of those pilot pairs. A noise floor measured
> *before* the "go quiet" warning is not the floor the pilots play into, so
> that window has to sit inside the settle, not ahead of the beeps. The
> settle bound absorbs it explicitly:
> `COURTESY_MAX_BEEP_TO_STIMULUS_GAP_S = COURTESY_TONE_TRAILING_SILENCE_S
> + PILOT_AMBIENT_WINDOW_S` (~4 s on those phases, still ~3 s on CHECK,
> whose own 12 s ambient window is silent and stays where it is).
>
> Shipped order, both rules satisfied: **beeps → settle → ambient window →
> pilots → guard → sweep** on MEASURE/VERIFY/cloud; **ambient → beeps →
> settle → pilots** on CHECK. Golden wire pins re-derived again (durations
> +1000 ms on every entry with a pilot pair; CHECK unchanged). The
> on-device listen this section required is still owed — it is owed for the
> *replacement* order now, and the two composition rules are pinned by test
> in the meantime.

### 2.6 Per-measurement control: Retake / Next / Stop

Every post-capture screen offers three controls, in the standard
grammar slots:

- **Next** (primary) — the confirm-then-tone advance, as in §2.1.
- **Retake this measurement** (secondary, quieter) — re-capture the
  measurement that *just completed*, because the user wants to (they
  sneezed, a truck passed, they weren't where they meant to be). Copy:
  "Hold still at the same spot" → tap → tone.
- **Stop** (small text link) — abandon the session (existing semantics:
  volume restored; pre-apply abandonment leaves the speaker untouched).

Retake design (no Worker change; one Pi-side runner-contract
extension):

- **The plan runner refuses out-of-order begins today** — this is a
  verified constraint, not a guess: `_poll_capture_plan`
  (`jasper/capture_relay/session.py`) hard-refuses any `begin_capture`
  whose `(index, attempt)` ≠ `(accepted_count + 1, attempts_used + 1)`
  with `begin_out_of_order`. A naive "begin the accepted index again"
  therefore never reaches the conductor.
- **The admitted-retake shape.** The runner's ordering contract gains
  exactly one new admitted shape: a begin for `accepted_count` (the
  just-accepted index) carrying a retake marker in its payload. On
  such a begin the runner does **not** rewind `accepted_count`; the
  retake capture flows through the normal
  authorize → arm → upload → verdict path for that slot, and on
  ACCEPT the conductor **replaces** the retained take (the swap is
  conductor-side, where take retention already lives — the geometry
  retry's drop machinery is the precedent). A rejected or abandoned
  retake leaves the original take standing (fail-safe: you can never
  end up with less evidence than you had). `accepted_count` never
  decrements, so the completion check and index→phase lookups are
  untouched. The Worker stays byte-opaque to all of it (events pass
  through; the marker is payload).
- Budget: retake attempts ride the just-accepted position's pooled
  `SlotAttempts` meter — the planned take plus three extras shared with
  failure and geometry retries (#2097), while the plan-level max remains the
  whole-session ceiling. The page hides the Retake control when the position's
  extras are exhausted.
- Scope: only the just-completed capture, and only until the next
  entry's begin is posted — no arbitrary-history retakes, no retake
  across a closed group. VERIFY retake is the existing `verify_retry`
  path, unchanged.
- Acceptance criteria: retake works end-to-end through the **real
  runner** in tests (`tests/test_capture_relay_plan.py` gains the
  admitted-shape cases); a rejected retake demonstrably keeps the
  original take; an old-build page (which never posts the marker)
  behaves exactly as today.

**Group-close timing consequence (deliberate change):** today the final
cloud position's *acceptance* closes the group and fires fit + apply
immediately, which would make the final position un-retakeable. The
group close therefore moves from "final position accepted" to "user
confirms past the final position": a "All {N} spots done — continue"
screen with Retake still available. No trust gate moves — the fit still
runs only after the full cloud (PR-6b semantics), apply still runs
under the same gates; one extra user tap now sits in front of it, which
also gives a natural pause before the walk-back to the mark.

> **Partially superseded (2026-07-28) by
> [`docs/two-stage-commission-flow-plan.md`](two-stage-commission-flow-plan.md)
> (issue #1806).** "Apply still runs under the same gates" is reversed:
> auto-apply at group close is removed, and the confirm screen ends the
> measure session instead of advancing past it. The retake window and
> the "continue" screen survive — they become stage 1's ending.

---

## 3. Tier selection (wizard)

**The choice is the user's, made explicitly every session** (owner
feedback item 4) — a two-option chooser on the `/correction/` wizard's
`microphone_check` screen, both tiers first-class with derived
durations and a one-line claims difference:

> **Quick tune** (about 5 min) — 7 measurements; confirms the result
> at the mark.
> **Full measurement** (about 11 min) — 16 measurements; re-checks the
> result at several spots around the mark.

(B2 fix, adversarial review of PR #1780: "across the room" overclaimed
past what the post-apply cloud actually samples — a handful of prompted
spots around the mark, never the room at large. This is the sentence
that sourced the same overclaim in the shipped wizard copy;
implementation and this example must read the same honest phrase.)

(Both durations are the page estimator's derived numbers — §1.1 — and
the wizard copy derives them the same way rather than hand-writing
prettier ones.) History picks only which option carries the
"Recommended" badge (never a silent default). **Implemented rule (S4,
coordinator ruling on the adversarial review of PR #1780): Full stays
recommended UNTIL a Full-tier commission has completed on this
topology** — keyed on the applied crossover being automatic AND the
durable v2 state's own tier recording "full" specifically. An
express-only household (an applied automatic crossover whose recorded
tier is "express", or no tier at all) still sees Full recommended —
this is the §1.3 HF-null row's mitigation, since the comb-free premise
for express is measured on JTS3, not on every speaker, and a
Quick-tune-only topology has never actually walked the wider,
comb-decorrelating cloud. Only once a Full commission has completed
does Quick tune become the recommended re-tune.

Where the choice lives, and why the wizard rather than the capture
page: the capture plan is baked into the HMAC-bound spec at session
mint time (`prepare_v2_session`), so the shape must be known before the
phone link/QR exists. Putting the chooser on the page would mean
minting after a page→Pi round trip (new endpoint, new session states)
for zero UX gain — the user is already looking at the wizard screen
that starts the session. The wizard posts `tier` to
`/correction/crossover/v2/session`; the phone simply renders whichever
plan the spec carries (it already derives all counts and durations from
the plan, §2.3). No new wizard steps — the cloud phases already map
onto the existing 5-step strip.

---

## 4. PR ladder

Standard gate per PR: implementation subagent + independent adversarial
review (0 blockers / 0 should-fixes, judgment on mechanical convergence
tails), serial local lanes, corpus tests PASSED (not SKIPPED) with both
`JTS_FLAT_LIN_CORPUS` and `JTS_FLAT_LIN_S0` roots when the touched code
reaches them.

- **PR-0 — this doc** (docs lane). Registers in `doc-map.toml`
  (`room-correction-and-calibration` subsystem) + README atlas.
- **PR-U1 — conductor + plan shape (Opus).** Tier vocabulary and the
  single resolved plan-shape value; express (N=5, M=1) validation; M=1
  plan emission (done screen on VERIFY); threading through
  `prepare_v2_session` into both the spec and the index-phase map; the
  screen grammar keys (`progress` / instruction-as-`title` / `body`
  detail) and the `CloudPositionPrompt` headline/detail split; the
  VERIFY hold-then-tap contract (screen key + whatever timeout tolerance
  the acceptance criterion needs); **the voluntary-retake semantics and
  the group-close-on-confirm move (§2.6)**; **the courtesy-tone
  reorder (§2.5, with its composition test)**; tier in durable state /
  pipeline payload / `/state`; consent tier line; re-verify "no
  re-walk" copy; `/crossover/v2/session` tier parameter. Golden wire
  pins re-derived by their documented procedure (dated notes) — full
  and 1-entry digests change (screen copy + program durations), and a
  third `"express"` pin is added.
- **PR-U2 — capture page (Opus).** The step-screen grammar renderer
  (eyebrow/headline/detail/primary/stop-link); **the
  Retake/Next/Stop control row and the retake + "all spots done —
  continue" screens (§2.6)**; counter unification (`#status`
  decounted); consent restructure with the derived announcement line;
  mic-picker collapse; dead-meter removal (page side renders whatever
  spec sends — the builder change is U1); VERIFY tap screen after the
  hold; version stamps (`version.json` + `?v=` + per-module, with the
  contract test); Node harness updates (`capture_plan_loop_test.mjs` +
  the screen-literal pins in `test_capture_page_js.py`).
- **PR-U3 — wizard + docs (Sonnet).** Envelope tier picker with
  history-based recommendation; express done/verify disclosure copy;
  chart + callouts handle the absent post-apply cloud; `/state` tier
  surfacing; `HANDOFF-crossover-measurement-v2.md` (session walk,
  counts, prompt copy section); productization-plan annotation (this
  phase supersedes its UX surface, not its instrument) **plus its
  stale "N = 8 cloud-measure positions" line
  (`docs/flat-linearization-productization-plan.md:492` — code is
  9)**; a dated new-horn pointer note in
  `docs/flat-linearization-plan.md` (§0); the stale `"Spot 4 of 8"`
  comment in `jasper/active_speaker/crossover_envelope_v2.py`; and the
  capture-count "16"s that read false once two tiers exist —
  enumerated by the review, they span `HANDOFF-crossover-measurement-v2.md`
  (~7 sites), `HANDOFF-correction.md` (2), `correction-journey-design.md`
  (1), `crossover-measurement-productization-design.md` (1), plus the
  narrative "16"s in `crossover_v2_flow.py` docstrings,
  `session_volume_plan.py`, `capture_relay/spec.py`, and
  `capture-page/js/main.js` (page prose is U2's copy to fix). The
  implementer greps rather than trusting this list to stay complete.

**Release order.** Worker untouched (screens and event payloads are
opaque to it; capacity 32 dwarfs express's 14). Page deploys before
the Pi (repo rule; `--branch=main` mandatory). Both directions are
fallback-safe **by construction, not by luck**: every behavior-changing
signal (the post-apply confirm copy, the retake marker) rides keys an
old page never reads and an old Pi never sends — the VERIFY
`title`/`body` are deliberately NOT repurposed (§2.1/§2.2) because
`validate_capture_page` enforces no minimum build and an old cached
bundle is admitted. Product smoke on JTS3 after: one express session
end-to-end + one full-session spot-check of the new screens (including
the §2.5 courtesy pacing, by ear).

## 5. Scope fences and open items

Fences (do not do):
- No combiner / spec / fit math changes. Express consumes the shipped
  estimators at n = 4. (This deliberately sits below the full tier's
  "min 6" comment and fundamental 1's N≈8–12 — the reconciliation is
  §0's comb-free premise plus §1.3's HF-null row; read those before
  calling this a contradiction.)
- No relay protocol or Worker change; no capabilities bump.
- `MIN_CLOUD_MEASURE_POSITIONS = 6` (full) and `VERIFY_TOLERANCE_DB =
  1.5` do not move.
- No re-litigation of settled instrument decisions (pulse/TDS, two-path
  inversion, cepstral removal, max-hold, `inverted:true` — parent plan
  firewall).
- Doctor's `check_capture_relay` keeps computing capacity at full-tier
  defaults — full dominates express, so no change; noted here so the
  next reader doesn't "fix" it.

Tracked outside this ladder:
- **The sweep glitch** (owner feedback item 7; `glitch_detected` =
  |ε| > 500 ppm in-capture clock-drift verdict,
  `program_analysis.glitch`, surfaced as "The capture glitched —
  measuring again"). Root cause unknown — could be phone-side capture
  discontinuity (browser audio thread) or Pi-side playback — and it
  needs evidence-first diagnosis against the 2026-07-27 session's
  journal and bundle. A dedicated session owns it (GitHub issue +
  spawned task); this ladder only keeps the retry UX honest.

Owner-reversible decisions taken in this doc (flag, don't block):
1. Express prompted positions = **4**, not 3 (rationale §1.1 — both wide
   offsets + `thin_evidence` semantics; costs ~35 s over a literal 3).
2. Express M = 1: no post-apply cloud, VERIFY-at-mark only (§1.3 table).
3. MEASURE keeps its cancelable countdown (§2.2 — no move to confirm).
   **Reversed by the owner 2026-07-28 (issue #1823) — it takes a tap.**
4. The tier chooser lives on the wizard, not the capture page (§3 —
   plan minting owns the shape; the recommended badge is the only
   history-driven part).
5. Retakes share the existing per-slot attempt budget rather than
   getting their own (§2.6 — bounded by construction; revisit only if
   real sessions exhaust it).
6. Stop demotes from an always-present danger button to a text link
   with a danger-styled confirm (§2.1 — reverses `render.js`'s
   documented styling decision; the confirm keeps the destructiveness
   honored).
7. The retake mechanism extends the Pi-side plan runner's
   begin-ordering contract by one admitted shape (§2.6) rather than
   adding a new event kind or a page→Pi mint round trip.

Last verified: 2026-07-27
