# Bass Extension commissioning — reconciled UX specification

> **Provenance.** This document reconciles an external deep-research UX/UI
> report on the Bass Extension commissioning wizard (commissioned by the
> maintainer, delivered 2026-07-24; saved verbatim during reconciliation but
> not itself checked into the repo — it is UNTRUSTED design input, not a
> contract) against the frozen JTS contracts:
> [`wave-4-commissioning-backend.md`](wave-4-commissioning-backend.md) (the
> `/bassext/*` HTTP contract + state machine),
> [`wave-6-ui.md`](wave-6-ui.md) (the UI wave's charter),
> [`wave-5-runtime-scheduler.md`](wave-5-runtime-scheduler.md) (the runtime
> scheduler, currently blocked), and
> [`../HANDOFF-bass-extension-plan.md`](../HANDOFF-bass-extension-plan.md)
> §§5.3, 7, 10.2 (honesty copy, the state machine/ladder, and the `/state`
> status vocabulary). **Where the external report and a frozen contract
> disagree, the contract wins.** This document authorizes no implementation
> by itself — [`wave-6-ui.md`](wave-6-ui.md) and the plan's **Wave status**
> table remain the sole authorization source of truth. As of this writing,
> Wave 6 is **not started**; Wave 4's HTTP layer
> (`jasper/web/bassext_backend.py`, `GET /bassext/state`) does not exist
> yet; and the pure `ladder.py` commissioning slice this document cites
> throughout is authorized by wave-4's revision 9 but is likewise not yet
> on `main` — see wave-6-ui.md's Revision 1 note.

**In short:** keep the external report's four-act structure, its honesty
discipline, and most of its microcopy. Reject its shared-shell refactor, its
stale "mirrors Crossover" framing, its A/B audition, and its premature
"works automatically" copy. Add the screens it never specified (capture
relay handoff, bypassed/deferred/recovering states, bonded refusal). None of
this is new design freedom — it is the frozen contract, rendered as UX.

## 1. The four acts over the frozen state machine

The engine state machine (`jasper/bass_extension/ladder.py`, plan §7.2) is
`idle → characterize → fit → propose → verify_deepest → ladder →
sustain_test → derive_anchors → review → accepted`, plus `aborted` from any
state. The UI is a thin renderer of `GET /bassext/state`
(`available_actions` + snapshot); it never re-derives state logic
client-side (wave-6 requirement). Four household-facing acts group the nine
engine states:

| Act | Engine states | What the household does |
|---|---|---|
| 1 — CHECK | `idle` (+ preconditions) | place the mic, pick a margin |
| 2 — MEASURE | `characterize → fit → propose → verify_deepest → ladder → sustain_test → derive_anchors` | wait, watch a progress rail, can Stop |
| 3 — REVIEW → ACCEPT | `review` (→ `accepted` via Wave 3) | reads the measured result, explicitly accepts or discards |
| 4 — DONE | `accepted` is the terminal session state; `bypassed`/`stale` are persisted **profile status** (plan §10.2), not `ladder.py` session states | sees an honest status chip |

Polling only — no SSE/websockets (wave-4 fence). Match the sibling cadence:
crossover polls at 1500 ms foreground / 10000 ms while `document.hidden`
(`deploy/assets/correction/js/crossover/main.js`, `POLL_MS`/
`HIDDEN_POLL_MS`); bass should reuse those two constants, not invent its
own.

## 2. Act 1 — CHECK

**1.0 Landing / empty state** (`idle`, no profile). One sentence on what
Bass Extension is, deeper-not-louder, a link to Sound Preferences, one CTA:
**Set up Bass Extension**. If a profile already exists, render Act 4's
status chip + state instead (§5).

**1.1 Preflight checklist.** Render exactly the plan §7.1 preconditions —
no more: applied active baseline (current fingerprint), confirmed-and-current
driver-safety profile, measurement window available, capture reachable, and
mic calibration (**recommended, not required** — plan §7.1: "an uncalibrated
mic gets `mic_uncalibrated` WARN and blocks nothing"). **Do not add a
Room-before-Bass gate here.** Room completion is not a wave-4 precondition —
only the applied active baseline is. A future bass entry receipt (see
[`correction-journey-design.md`](../correction-journey-design.md) §9, "Bass
entry receipt") may add one later; until that ships, the UI must not invent
the gate.

**1.2 Mic placement.** Nearfield, fixed — the mic must not move once
measurement starts (plan §7.3's mic-fixity coherence check enforces this
after the fact; the UI's job is to set the expectation before). This is the
external report's strongest, best-grounded finding — JTS's mic never
"walking" during the ladder eliminates the single largest prior-art error
class (Trueplay/Audyssey mic-dance confusion) but raises the stakes on
getting placement right the first time. Carry it as the highest-priority
screen to user-test.

**1.3 Margin choice.** Three presets — conservative / normal / aggressive —
via `POST /bassext/session/start {margin}`. **Render one sentence per tier
from `MarginPolicy` field values** (`jasper/bass_extension/targets.py`
`MARGINS`), never restated prose that can drift (wave-6 rule — same
requirement as the family/anchor table in §4). At minimum surface
`boost_cap_db` and `compression_fail_db`/`thd_fail_ratio` per tier — the
numbers that actually differ tier-to-tier and explain "conservative =
smaller cushion" honestly. **Do not hardcode a duration in this or the
consent/rail copy** — `sustain_duration_s` is itself a `MarginPolicy` field
that varies 90 s (conservative) / 60 s (normal) / 30 s (aggressive); render
it per-margin rather than guessing "about a minute" for all three (the
external report's consent copy did this — corrected in §9).
**Preselecting Normal is a reasonable client-side default, not a contract
fact:** `session/start` requires an explicit `{margin}` and the frozen
contract names no server-side default (unlike `propose {margin?}`, which is
explicitly optional). Preselecting a radio button is fine; do not assume the
backend treats an omitted margin as "Normal."

## 3. Act 2 — MEASURE

**2.0 "About to get loud" consent.** A dedicated, rare consent screen —
restate specifically what will happen, name the margin-correct sustain
duration, warn others in the room, red-highlighted primary **Start
measurement**, safe default focus on **Back**. This is the single
highest-justified confirmation dialog in the whole flow: the physics case
(Linkwitz Transform boost cost — plan §1.2, roughly 12 dB of gain per octave
of extension) and NN/G's confirmation-dialog guidance (reserve for rare,
specific, non-default-risky moments) both point the same way. Do not spread
this pattern through the rest of the flow.

**2.1 Live measurement rail.** One determinate progress rail, milestones in
household language, driven entirely by polling `GET /bassext/state`:

| Engine state | Household label | Notes |
|---|---|---|
| `characterize` | "Listening to your speaker" | room correction + any prior bass extension temporarily disabled (plan §7.2) |
| `fit` | "Working out your speaker's natural limits" | |
| `propose` | "Planning the deeper targets" | |
| `verify_deepest` | "Testing the deepest setting" | |
| `ladder` | "Stepping up the volume, one level at a time" | show rung *N of total* — **total is server-driven and variable** (plan §7.4's default anchor mode ladders only the deepest target; deep mode ladders every target); "keep the mic still" persists throughout |
| `sustain_test` | "Holding a steady tone to check for strain" | countdown from the session's actual `sustain_duration_s`, not a fixed "~60 s" |
| `derive_anchors` | "Finishing your bass profile" | |

Per NN/G, use determinate/percent style for these ≥10 s waits, never a bare
spinner. If a rung fails, do not alarm — §7 has the exact copy class (a
genuine physical ceiling and a measurement-process refusal read very
differently to a household).

**Stop.** Persistent, red, one-handed, in the bottom thumb zone, live from
2.0 onward and whenever `available_actions` could still play audio. **Stop
maps only to `POST /bassext/stop`** — the sole action exempt from the
`apply_recovery_required` guard and the only one the contract guarantees
never 409s ("Must work in every state; never 409s," wave-4 HTTP contract).
`POST /bassext/ladder/abort` is a *different*, ordinary guarded action (a
mid-ladder cancel that still goes through the standard route-allowlist →
`guard_mutating_request()` chain) — do not wire the visible Stop button to
it as if the two were interchangeable. Stop is the simpler, always-safe
primitive; it is what the button must call.

## 4. Act 3 — REVIEW → ACCEPT

**3.0 Result card** (`review`). Minimum comprehensible summary first,
evidence on demand:

- **Headline result** from measurement — §7 has the exact copy class (full /
  partial / minimal), never a promised number.
- **Volume-retreat story**, 3-node: Low → deepest · Normal → still deeper ·
  Loud → natural, protected — **only true once Wave 5 ships and reports
  `runtime_armed=true`** (§5); pre-Wave-5, present it as what the saved
  profile targets, not something the speaker is doing now. (No live A/B
  before/after audition — §11.)
- **Family/anchor table — mandatory**, not a Stage-2 disclosure (wave-6):
  target, corner, boost, usable-below level, evidence tag
  (`measured`/`derived`/`spot_verified`, plan §7.4). Plus the propose
  endpoint's predicted-curves data as an inline SVG polyline if an existing
  correction-page inline-SVG pattern fits it; a table alone is acceptable
  otherwise (wave-6: "prefer less").
- **Margin re-derive without re-measuring.** `POST /bassext/propose
  {margin?}` accepts an optional margin and re-derives anchors from
  already-retained ladder evidence with no new measurement (plan §5.1). A
  margin control at Review that live-updates the anchor table via
  `propose` (never `session/start`, which would start a new session) is
  contract-supported and genuinely useful — the external report's Act 3
  never surfaced it.
- **Envelope honesty copy — mandatory, verbatim:** "measured clean operating
  envelope, not a driver warranty" (plan §5.3, wave-6). Not optional flavor
  text — the phrase wave-6's acceptance bar names explicitly.
- **Expert drawer, collapsed by default** (mandatory surface — collapsed is
  fine, absent is not): Qp (0.5–0.71), boost cap (up to the CamillaDSP-valid
  limit with "a stark warning," plan §6.2), rung step (see the open
  question in §12 — `rung_step_db` is 3.0 for every margin today despite
  plan §7.2 calling it "margin-selectable"), a **bounded** subsonic
  corner/order override — the first graph slice never permits removing the
  subsonic filter (plan §9) — and an impedance `.zma` upload **only if**
  Wave 4's fit endpoint actually accepts one (wave-6: "if Wave 4 shipped
  without the upload field, omit the control — check, don't assume").
- **Deep mode: a single pass-through toggle, nothing richer.** Wave-6's
  fence is explicit — the deep-mode UI is one toggle passed through to the
  backend, not a second ladder-visualization screen.
- Primary CTA **Accept and turn on** (`POST /bassext/accept`); secondary
  **Discard**. `accepted` is reached only by this explicit tap. Accept
  commits the measured profile; the speaker keeps playing its natural
  response after this tap until Wave 5 ships and reports
  `runtime_armed=true` (§5) — "turn on" means "save," not yet "go live."

**Refusal states that land in, or keep, `review`:**

- **Bonded-role refusal.** If a bonded program-bake or driver-domain carrier
  is active, Wave 3 refuses accept/replace/bypass before touching any
  profile or graph; Wave 4 surfaces that as a stable conflict and *leaves
  the session in `review`* (plan §8.6). Copy: name the bond, say leaving it
  is required before accepting, and that nothing has changed.
- **`apply_recovery_required=true`.** `available_actions` is empty; every
  state-advancing action is withdrawn; **Stop remains live** and can
  retire/abort the session but cannot clear the intent (wave-4 Persistence
  section). Render this as a distinct "recovering" state, not a generic
  error — it is transient transaction recovery, not a household mistake.

## 5. Act 4 — DONE / status

There is no live scheduler today (Wave 5 is blocked — plan §8.2/§10.2), so
Act 4's copy must not claim automatic runtime behavior that doesn't exist —
and must not claim it uniformly even once Wave 5 ships, because ported/PR
profiles never get a live scheduler under the current graph design (plan
§6.4/§8.1: "the first runtime slice does not arm them"). The status chip
must render the full `/state.bass_extension` vocabulary (plan §10.2), not a
binary On/Off:

| Profile state | Chip | Body copy |
|---|---|---|
| `absent` | *(hidden / "Not set up")* | — |
| `accepted`, sealed, `runtime_armed=true` | "Bass Extension: On" | "It goes deeper at low and normal volume and returns to natural as you turn up." (**only true once Wave 5 ships and reports `runtime_armed=true`**) |
| `accepted`, sealed, `runtime_armed=false` (today: always, pre-Wave-5) | "Bass Extension: Measured" | "We measured and saved your speaker's deeper-bass profile. Automatic volume-linked scheduling arrives with a later update — for now your speaker plays its natural, protected sound." |
| `accepted`, ported/PR (`runtime_deferred_reason="fixed_graph_not_defined"`) | "Bass Extension: Measured (not yet automatic for this speaker type)" | Same honest framing; unlike the row above, this one does **not** change when Wave 5 ships — ported/PR stays commissioned-but-inert until a separately designed fixed graph exists (plan §6.4). |
| `bypassed` | "Bass Extension: Off" | "Your speaker plays its natural response." |
| `stale` | "Bass Extension: Out of date" | adopted verbatim: "Something changed on your speaker, so your Bass calibration is out of date. It's back to its natural setting until you re-run it." One-tap **Re-run.** |

**Reject the external report's Act-4 copy** ("It works automatically and
adjusts with your volume — nothing else to do.") outright until the
`runtime_armed=true`/sealed row above is actually reachable — see §11.

**Runtime visibility: invisible by default, opt-in read-only detail.**
Adopted from the external report — normal listening stays clean; a static
status chip plus, behind a tap, a read-only explainer of the accepted
schedule. No live depth HUD. This resolution is independent of the table
above: even once armed, no permanent moving meter.

## 6. Screens the report never specified

- **Capture relay handoff** (`POST /bassext/capture/start {role}`
  response): a relay session payload — tap link + QR — mirroring the
  crossover flow's relay screen (`renderRelayQr`, the already-shared
  `/assets/shared/js/qr.js`). Load-bearing: nearfield capture cannot
  proceed without it, and the external report never mentioned it.
- **Locked** (preconditions unmet): names exactly what's missing, links to
  the blocking surface. Per §2, this is the plan §7.1 list only.
- **Loading**: skeleton + "Checking your speaker…", never a bare spinner.
- **Recovering** (`apply_recovery_required`) and **bonded-role refusal**:
  see §4.
- **Mic-moved mid-ladder**: refusal-branched copy per §7; a still-unresolved
  contract question (pause-and-resume vs. hard stop) is in §12.

## 7. Refusal and partial-success microcopy — the honesty core, branched by cause

Two different situations both end a ladder early, and they must **not**
share copy:

**A. Genuine physical-ceiling outcomes** — a rung actually failed its
compression/THD/digital-ceiling test, or the sustain test found sag/fc-shift
(plan §7.5–7.6). This is real information about *this unit*. Adopt the
external report's copy classes:

- Full success: "We measured your speaker and turned on deeper bass down to
  ~[X] Hz at low and normal volumes." — **only once Wave 5 ships and reports
  `runtime_armed=true`** (§5). Pre-Wave-5 the class-A headline is "We
  measured your speaker and saved a deeper-bass profile down to ~[X] Hz" —
  no volume-conditional claim, matching the gated chip copy in §5.
- Partial: "Your speaker safely reached ~[X] Hz. We tested deeper, but it
  showed [strain/distortion] there, so we stopped at the safe point. This is
  normal and specific to your speaker."
- Minimal/none: "Your speaker is already close to its safe limit down low…
  We've kept it at its natural, protected response." Framed as a
  *successful measurement*, never a failure; points to Sound Preferences
  for tonal character.

**B. Measurement-process refusals** — `CAPTURE_QUALITY_REFUSED`,
`CAPTURE_SNR_INSUFFICIENT`, `MIC_MOVED_BETWEEN_RUNGS`, `LADDER_INCOMPLETE`
(plan §5.4). These say nothing about the speaker's capability — only that
the measurement conditions were bad. **Never voice these as "your speaker's
limit."** Adopt the external report's other pattern instead: neutral,
specific, recoverable, always states "nothing changed" — e.g. "We lost a
clear signal — the mic may have moved. Nothing changed. Reposition and try
again." Ceiling-setting language ("your speaker's safe ceiling is set
here") belongs only to class A.

## 8. Accessibility & internationalization

Adopted from the external report, with one correction: **drop the implicit
reliance on focus rings.** JTS's design system deliberately suppresses
native focus outlines (AGENTS.md "Web wizard conventions"); active
Stop/Accept/Start affordances must read from component state (`.active`,
`[aria-pressed]`, checked toggle styling), never `:focus-visible` rings.
Keep: primary actions in the bottom thumb zone; progress conveyed by text +
numeric step count, not color/animation alone; countdowns in screen-reader
live regions; pass/fail never by color alone; short verb-first microcopy,
externalized for translation, no idioms; localized numeric units.

## 9. Microcopy set (corrected)

- **Deeper vs louder:** "Bass Extension makes your bass go *deeper*…
  reaching lower notes you couldn't hear before… using your speaker's spare
  power when you're not playing loud. It doesn't make bass *louder* or
  *boomier*. Want more punch or a bass boost? That lives in Sound
  Preferences." (adopted verbatim)
- **Volume-retreat behavior:** "The deeper bass is there at low and normal
  volumes. As you turn up, your speaker eases back to its natural sound so
  it always stays safe — you won't hear the change." (adopted; **not true
  until Wave 5 ships and reports `runtime_armed=true`** — do not show this
  copy against a `runtime_armed=false` profile, §5)
- **Loud-audio consent — corrected, margin-aware duration:** "We're about to
  get loud. We'll play strong bass tones and one steady tone for about
  [`sustain_duration_s`] to measure your speaker safely. Tell anyone
  nearby. You can press Stop anytime."
- **Don't-move-the-mic (persistent during ladder):** "Keep the mic still —
  we're comparing each step to the last." (adopted verbatim)
- **Review headline:** "Here's the deeper bass we unlocked for your speaker
  — and the volume it lasts up to." (adopted verbatim)
- **Envelope honesty (mandatory, plan §5.3 / wave-6):** "…measured clean
  operating envelope, not a driver warranty."
- **Accept:** "Accept and turn on." Commits the measured profile; the
  speaker keeps playing its natural response after this tap until Wave 5
  ships and reports `runtime_armed=true` (§5) — "turn on" means "save,"
  not yet "go live."
- **Done — corrected, no false automation claim:** "Measured and saved.
  Automatic deep-bass scheduling arrives with a later update — for now your
  speaker plays its natural, protected sound."
- **Stale:** "Something changed on your speaker, so your Bass calibration
  is out of date. It's back to its natural setting until you re-run it."
  (adopted verbatim)

## 10. Reconciliation deltas

| Claim (external report) | Verdict | Citation |
|---|---|---|
| Four-act IA (CHECK/MEASURE/REVIEW-ACCEPT/DONE) over the 9-state machine | ALIGNED | plan §7.2 |
| Thin renderer of `GET /bassext/state` + `available_actions`; frozen POST set; polling only | ALIGNED | wave-4 HTTP contract, wave-6 |
| Milestone progress rail, household labels, determinate ≥10 s style, sustain countdown | ALIGNED | plan §7.2; NN/G (report) |
| Loud-audio consent as rare/specific/non-default-risky; persistent Stop | ALIGNED | plan §§1.2, 7.3 |
| Margin = 3 presets; tier copy sourced from `MARGINS` values, not prose | ALIGNED (source corrected) | `targets.py`, wave-6 |
| "Normal" preselected | PLAUSIBLE-EXTENSION | no server default in `session/start` |
| Honesty core: per-unit measured results, "deeper not louder," partial framed as success | ALIGNED | plan §5.3, §7.5–7.6 |
| Envelope copy "not a driver warranty" | ALIGNED, mandatory | plan §5.3, wave-6 |
| Mic cal recommended, not required | ALIGNED | plan §7.1 |
| Ceiling = previous good rung | ALIGNED, but refusal-branched | plan §7.5; §7 above |
| Accessibility set | ALIGNED, minus focus rings | AGENTS.md |
| Status chip covers full vocabulary, not On/Off | ALIGNED (chip corrected) | plan §10.2 |
| Runtime invisible by default, opt-in detail, no HUD | PLAUSIBLE-EXTENSION | no contract requirement either way — external report's UX argument only |
| Shared "conductor shell" across Crossover/Room/Bass | REJECTED | §11 |
| "Mirrors Crossover CHECK→MEASURE→REVIEW/APPLY→VERIFY" | REJECTED (stale) | `crossover_v2_flow.py` docstring |
| Resume-after-Stop | REJECTED | plan §7.2 (no edge from `aborted`) |
| A/B before/after audition at review | REJECTED | §11 |
| Stop wired to `ladder/abort` | REJECTED | wave-4 Persistence + HTTP contract |
| Act-4 "works automatically" copy | REJECTED as written | plan §8.2/§10.2 (Wave 5 blocked) |
| Deferring family/anchor table, predicted curves, expert drawer, deep-mode toggle, LF overview to "Advanced" | REJECTED | wave-6 mandatory-surfaces list |
| Journey header "Crossover ✓ · Room ✓ · Bass •" as specced | REJECTED | `correction_hub.py` `SECTIONS`; §11 |
| Room-before-Bass gating | REJECTED as a UI-invented gate | plan §7.1; correction-journey-design.md §9 |
| Missing: capture relay/tap-link handoff, locked, recovering, bonded-refusal, bypassed/ported-deferred states | ADDED | wave-4 contract; §§4–6 above |

## 11. Rejected alternatives (reasons)

- **Shared "conductor shell" refactor across Crossover/Room/Bass.** Violates
  two written non-goals —
  [`correction-journey-design.md`](../correction-journey-design.md) §2 ("No
  shared wizard/session framework") and
  [`room-correction-information-design.md`](../room-correction-information-design.md)
  "Non-goals" ("A generic tab, session, envelope, graph, or wizard
  framework") — and the maintainer reaffirmed on 2026-07-24 (session ruling,
  not yet in a written doc) that Crossover, Bass, Room, and EQ stay separate
  concerns. What *is* sanctioned to share: `section_tabs`, the existing
  shared ES modules (`dialog.js`, `escape.js`, `dom.js`, `qr.js`), promoting
  the `.wizard-step`/`.wizard-nudge` rail CSS to `app.css` when Bass becomes
  its third consumer (today `crossover.css` mirrors `correction.css`'s room
  flow block near-verbatim, ~60 lines — the `.wizard-step` rail differs only
  in the in-progress modifier class, `.wizard-step.current` in
  `correction.css` vs `.wizard-step.active` in `crossover.css`, while the
  `.wizard-nudge__icon`/`__text` element rules exist only in
  `correction.css`; `crossover.css`'s own comment says "mirrors" — exactly
  the promotion rule's trigger, AGENTS.md), and
  the designed-but-unbuilt
  read-only journey strip.
- **"Mirrors Crossover CHECK→MEASURE→REVIEW/APPLY→VERIFY."** Stale: the
  2026-07-20 owner ruling deleted the human mid-flow Apply gate. The real
  spine is `CHECK → gain solve → MEASURE → candidate → APPLYING (auto) →
  VERIFY` (`jasper/active_speaker/crossover_v2_flow.py` docstring: "no human
  mid-flow Apply gate… a trusted candidate… is applied by the conductor
  itself"). Bass's explicit Accept is a deliberate safety commit on its own
  terms, not tab mirroring — Bass drives real speakers into thermal/
  mechanical stress in a way Crossover's alignment measurement does not, so
  an independent human-accept gate is independently justified.
- **Resume-after-Stop.** No contracted edge leaves `aborted`; the only
  forward path is a fresh `POST /bassext/session/start`. (The separate,
  unresolved mic-moved pause-vs-terminate question is in §12 — do not
  conflate the two.)
- **A/B before/after audition at Review.** Needs an endpoint the contract
  doesn't define — stop and report, per the wave charter, rather than
  inventing one client-side; would put a live graph carrying an unaccepted
  profile onto a household-facing screen (bench-only territory); and needs
  program-material playback the flow doesn't have.
- **Stop wired to `ladder/abort` during the ladder.** Stop is contractually
  `POST /bassext/stop` only — the one action guaranteed to never 409 and
  exempt from the `apply_recovery_required` guard. `ladder/abort` is an
  ordinary guarded action; conflating them would make the visible Stop
  button sometimes fail exactly when it matters most.
- **Act-4 "It works automatically and adjusts with your volume."** False
  today and false for ported/PR forever: Wave 5 (the only thing that could
  make it true for sealed profiles) is blocked behind an unresolved limiter
  safety gate (plan §8.2), and ported/PR profiles are architecturally
  excluded from runtime arming regardless (plan §6.4/§8.1).
- **Deferring the family/anchor table, predicted-curves plot, expert
  drawer, deep-mode toggle, and LF-overview upgrade to a "Stage 2/Advanced"
  tier.** wave-6 lists every one of these as mandatory (collapsed by
  default is fine; absent is not).
- **Journey header "Crossover ✓ · Room ✓ · Bass •" as specced.** Two
  distinct orderings already exist and this header conflates them: the tab
  bar (`SECTIONS` in `jasper/web/correction_hub.py`) reads Room → "Active
  speaker" → Bass (the internal slug stays `crossover`; only the
  household-facing label changed, #1670), while the *calibration* order
  used by the not-yet-built journey strip is Crossover → Room → Bass
  (`correction-journey-design.md`'s `steps` payload). A bespoke header
  invented here would be a second, differently-shaped source of truth for a
  distinction that's already easy to get wrong once, let alone twice — the
  sanctioned additive mechanism is the journey strip design
  (`correction-journey-design.md`), not yet built.
- **Room-before-Bass gating.** Not a wave-4 precondition; inventing it in
  the UI would drift from the backend's actual refusal set the first time
  the two disagree. The correct future mechanism is a bass entry receipt
  (`correction-journey-design.md` §9), not a client-side assumption.

## 12. Open questions

Carried from the external report, unresolved pending on-device hardware
validation:

1. Ladder rung spacing/count and total duration depend on measured per-unit
   behavior; the rail must handle a variable, server-driven rung count.
2. The sustain-test sag threshold (how much droop lowers the ceiling one
   rung) is measured hardware behavior, not yet characterized.
3. The transition micro-step size needed for genuinely inaudible
   transitions is unknown until on-device listening; it gates the
   runtime-visibility decision (§5).
4. Perceptibility of the volume-linked retreat near max volume — if
   inaudible as intended, runtime stays invisible; if audible, a minimal
   explanatory hint may be warranted.
5. Staleness triggers' real-world frequency affects how prominent the stale
   prompt should be; unknown pre-deployment.
6. Mic-placement tolerance and coherence-break sensitivity determine how
   strict the placement screen and "keep still" reminders must be.
7. Achievable depth range across the unit population is unknown until
   measured; all result copy must stay per-unit and measured, never a fixed
   marketing number.
8. Whether mic calibration is truly optional, or effectively required for
   reliable per-unit limits, needs validation — it currently reads
   "recommended," which may need to harden to "required."

Two contract-level ambiguities this reconciliation surfaced, both to resolve
in wave-4's file (not by UI copy inventing an answer):

9. **Mic-moved: pause-and-resume, or hard stop?** Plan §7.3 says a mic bump
   "pauses and offers re-run from the last good rung," reading as a
   resumable soft-pause. But wave-4's frozen ladder treats
   `MIC_MOVED_BETWEEN_RUNGS` as an ordinary rung stop-condition — "first
   failure ends the ladder; ceiling = previous rung" — with no contracted
   edge back into `ladder` from `review`/`aborted`. Resolve by adding a real
   resume action to wave-4, or by correcting plan §7.3's prose to match the
   terminal behavior, before wave-6 designs around either assumption.
10. **`rung_step_db` is not actually margin-selectable.** Plan §7.2
    describes the ladder step as "default +3 dB/rung, margin-selectable ±,"
    but `jasper/bass_extension/targets.py`'s `MARGINS` sets
    `rung_step_db=3.0` identically for conservative/normal/aggressive.
    Either the shipped constant should vary by margin, or the plan prose is
    stale and the expert drawer's "rung step" override is the only place
    this is actually adjustable.

---

Last verified: 2026-07-24.
