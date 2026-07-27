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

What makes express *honest* now: the new horn removed the deep 8–16 kHz
comb (2026-07-27 session: null depths 5.4–7.0 → ≤1.6 dB, all below the
2.5 dB materiality floor). The biggest historical reason for many
positions was HF-null decorrelation; with the comb gone, a small cloud's
remaining job is LF support and outlier rejection, which fewer — but
wide — positions can carry, with the degradation disclosed.

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
  its wide (≥ 30 cm) moves at table offsets 2 and 3
  (`test_cloud_prompts_front_load_the_wide_offsets`). A 4-position group
  walks offsets 0–3 and picks up both wide moves from the already-shipped,
  already-validated table — hand-width left/right plus forearm left/right,
  which is the owner's "middle, left, right" at two scales. Three prompted
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
- **The envelope's evidence discount actually binds, honestly.** The
  S0-calibrated `position_stability_limit` (σ/√N,
  `tests/test_active_speaker_linearization_envelope.py` calibration) sits
  around 12.3 dB at 10 positions — above the fit's 12 dB per-filter cut
  cap, so a full cloud pays nothing — but ~7.9 dB at 4 positions. An
  express fit is automatically a lighter-touch fit. That is the honest
  machinery working, not a new rule.

Geometry retakes stay enabled with the same budget
(`GEOMETRY_RETRY_POSITIONS = 2`), so a locked express cloud can grow to 9
captures worst case — still comfortably under the relay ceiling
(`max_attempts = 7 + 2 + 5 = 14 ≤ 32`) and the wall-clock ceiling
(`1800 + (7−3)·120 = 2280 s`).

**Duration.** By the capture page's own estimator (per-entry
`duration_ms` + its 20 s/capture allowance,
`capture-page/js/main.js::wakeLockHintText`), 7 captures ≈ **4 minutes**.
The estimate the user sees is derived from the plan — nothing hardcoded.

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
- Tier-aware validation: express admits exactly (N = 5, M = 1); full keeps
  the existing (6 ≤ N ≤ 12, M ≥ 5) rules. An M = 1 plan emits no
  cloud-verify entries and moves the `done_title`/`done_body` screen onto
  the VERIFY entry.
- The tier rides the durable v2 state, the pipeline payload, and
  `/state`, so every consumer can tell which instrument produced a
  result (same unknown-vs-default rule as `echo_band_provenance`,
  issue #1763).

### 1.3 Degraded-claims table (what express claims, what it stops claiming)

| Surface | Full (N=9, M=6) | Express (N=5, M=1) |
|---|---|---|
| Correction fit evidence | 8-position power-mean cloud | 4-position power-mean cloud; envelope's position-stability cap tightens ~12.3 → ~7.9 dB (S0-calibrated) — **automatic** |
| Outlier exclusion screen | power-vs-median over 8 | over 4 (weaker; a single bad take is harder to identify) — **automatic**, disclosed |
| Echo/geometry adjudication | n = 8, `thin_evidence` cliff at 2-of-≥4 | n = 4, same cliff semantics — **automatic** |
| Null registry / carve-outs | full corroboration budget | fewer corroborations → more `insufficient_evidence` refusals — **automatic** (the detector refuses rather than guesses) |
| Pre-apply spec verdict | cloud spec gauges on the measure cloud | same gauges on the 4-position cloud, tier-qualified |
| **Post-apply spec verdict** | cloud-verify group (5 positions) re-measures the applied result across the cloud | **ABSENT.** Express verifies tracking at the mark only (±1.5 dB, `VERIFY_TOLERANCE_DB` unchanged). No cross-position post-apply claim — disclosed on the done screen, in the wizard, and in `/state` |
| Before/after chart (PR-7) | both phases | "before" cloud + VERIFY tracking; the "after" cloud panel is absent and the callouts say why |
| Wall clock | ~10–15 min | ≈ 4 min |

The disclosure rule follows the program's standing prose discipline:
never claim wider than measured. Express copy says what it verified
("tuned and confirmed at the mark") and names the upgrade path ("run a
Full measurement for the verified-everywhere result"). Nothing is
silently weakened: every row above is either the existing machinery
scaling itself down, or an absence that is stated.

### 1.4 Consent for express

The placement policy is unchanged — the mic starts at the mark, moves
only when asked, holds still during sweeps — so
`summed_guided_cloud_v1` remains the policy id and the acknowledgement
keeps deriving its capture count from `plan.capture_target` (7). The
consent screen gains one tier-derived line (quick tune vs full, with the
derived duration) so the user knows which instrument they are consenting
to. The stationary 1-entry re-verify copy stays byte-identical
(`guided_captures = 0` path).

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
[eyebrow]   Measurement 4 of 7            ← small; the ONLY counter
[headline]  A forearm's length LEFT       ← the instruction, 1.5rem+,
            of the mark                     one imperative sentence
[detail]    Step a little toward the      ← ≤ 1 short supporting clause;
            speaker; keep the phone         may be empty
            pointed at it.
[action]    [ I'm there — play the tone ] ← single full-width primary
[stop]      Stop measuring                ← small text link, not a
                                            red button
```

Server side: `CapturePlanEntry.screen` (an opaque `str → str` dict — no
relay/protocol change) carries `progress`, `title` (now the
instruction), and `body` (the detail clause). The old page renders new
plans gracefully (it already shows `title` big and `body` small — the
instruction simply becomes the headline); the new page renders old plans
via the same fallbacks. `CloudPositionPrompt` splits into
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
  Owner-reversible; flagged in §5.
- **VERIFY becomes hold-then-tap — the step-11 fix.** Today
  `AUTO_ADVANCE_ON_APPLY` fires the verify sweep with *no tap at all*
  once the apply completes, racing the user's walk back to the mark (the
  2026-07-27 session failed exactly here: collapsed-gate capture from
  mic placement, then a 1.73 dB vs 1.5 dB tracking miss with walk-back
  placement suspect). New contract: during apply, the hold screen
  instructs the walk-back ("Applying — walk back to the mark now");
  when the apply completes, the page renders the standard step grammar
  ("Back on the mark, holding still?" / "I'm there — play the tone") and
  the sweep arms only after the tap. Acceptance criterion for the
  implementation, regardless of seam: *the VERIFY tone must not fire
  until the user confirms after apply completes, and the session must
  tolerate at least a 60 s tap delay without expiring.* (The page
  already controls arming — authorization and the `armed` post are
  separate steps — so this is expected to be page policy plus a screen
  key, with no conductor state-machine change; the implementer verifies
  the authorized→armed window tolerates the delay.)
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
- Clutter removed at the same time: the dead `level_meter` component on
  crossover consent screens (only the level-ramp flow ever feeds it) is
  no longer emitted by the crossover spec builder; the mic picker
  collapses once the session mic stream exists (it is locked to one
  stream after the first capture anyway); the wake-lock hint remains the
  fallback-only line it is today.

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

---

## 3. Tier selection (wizard)

The `/correction/` wizard's `microphone_check` screen offers both tiers
with derived durations, defaulting by history:

- **No confirmed profile has ever existed on this topology** → primary
  "Full measurement (~12 min)", alternate "Quick tune (~4 min)". First
  runs deserve the full instrument.
- **Re-tune** (a profile exists or a prior session completed) → primary
  "Quick tune", alternate "Full measurement".

The choice posts `tier` to `/correction/crossover/v2/session`; the
envelope (`crossover_envelope_v2.py`) grows the second action on that one
screen. No new wizard steps — the cloud phases already map onto the
existing 5-step strip.

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
  the acceptance criterion needs); tier in durable state / pipeline
  payload / `/state`; consent tier line; re-verify "no re-walk" copy;
  `/crossover/v2/session` tier parameter. Golden wire pins re-derived by
  their documented procedure (dated notes) — full and 1-entry digests
  change (screen copy), and a third `"express"` pin is added.
- **PR-U2 — capture page (Opus).** The step-screen grammar renderer
  (eyebrow/headline/detail/primary/stop-link); counter unification
  (`#status` decounted); consent restructure with the derived
  announcement line; mic-picker collapse; dead-meter removal
  (page side renders whatever spec sends — the builder change is U1);
  VERIFY tap screen after the hold; version stamps
  (`version.json` + `?v=` + per-module, with the contract test);
  Node harness updates (`capture_plan_loop_test.mjs` + the
  screen-literal pins in `test_capture_page_js.py`).
- **PR-U3 — wizard + docs (Sonnet).** Envelope tier picker with
  history-based default; express done/verify disclosure copy; chart +
  callouts handle the absent post-apply cloud; `/state` tier surfacing;
  `HANDOFF-crossover-measurement-v2.md` (session walk, counts, prompt
  copy section); productization-plan annotation (this phase supersedes
  its UX surface, not its instrument); drift fixes found during design:
  the plan doc's stale "N = 8" (code is 9), the stale
  `"Spot 4 of 8"` comment in `crossover_envelope_v2.py`, and the
  eight prose "16"s that would read false with two tiers.

**Release order.** Relay untouched (screens are opaque; capacity 32
dwarfs express's 14). Page deploys before the Pi (repo rule;
`--branch=main` mandatory), and both directions are fallback-safe: old
page renders new plans (instruction lands in the headline slot it
already draws), new page renders old plans. Product smoke on JTS3 after:
one express session end-to-end + one full-session spot-check of the new
screens.

## 5. Scope fences and open items

Fences (do not do):
- No combiner / spec / fit math changes. Express consumes the shipped
  estimators at n = 4, inside their calibrated regimes.
- No relay protocol or Worker change; no capabilities bump.
- `MIN_CLOUD_MEASURE_POSITIONS = 6` (full) and `VERIFY_TOLERANCE_DB =
  1.5` do not move.
- No re-litigation of settled instrument decisions (pulse/TDS, two-path
  inversion, cepstral removal, max-hold, `inverted:true` — parent plan
  firewall).
- Doctor's `check_capture_relay` keeps computing capacity at full-tier
  defaults — full dominates express, so no change; noted here so the
  next reader doesn't "fix" it.

Owner-reversible decisions taken in this doc (flag, don't block):
1. Express prompted positions = **4**, not 3 (rationale §1.1 — both wide
   offsets + `thin_evidence` semantics; costs ~35 s over a literal 3).
2. Express M = 1: no post-apply cloud, VERIFY-at-mark only (§1.3 table).
3. MEASURE keeps its cancelable countdown (§2.2 — no move to confirm).
4. Tier default by history (§3): first-run → full, re-tune → express.

Last verified: 2026-07-27
