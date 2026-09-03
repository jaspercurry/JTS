# UX / IA audit — MEASUREMENT / CORRECTION cluster

Design of record consulted: `docs/room-correction-information-design.md`,
`docs/active-crossover-information-design.md`, `docs/measurement-loop-doctrine.md`,
`docs/design-language.md`. Findings that would contradict those docs are listed
under "Declined (ratified)" per page rather than filed.

Routing fact used throughout: nginx `/correction/` and `/sound/room/` both land on the
same daemon (`deploy/nginx-jasper.conf:432,468`) and the daemon serves the Room page at
`/` and `/room` (`jasper/web/correction_setup.py:4507`). **There is no `/correction/`
hub page** — the "hub" is only the four-item tab strip in `correction_hub.py`.

---

## /sound/room/ (== /correction/) — "measure your room with a mic and apply the result"

Files: `jasper/web/correction_room_flow.py` (420), `jasper/web/correction_setup.py` (5200,
the whole-daemon router — Room is only part of it), `jasper/correction/envelope.py` (1368),
`deploy/assets/correction/correction.css` (507), `deploy/assets/correction/js/main.js` (3450).

Primary user task: run a guided multi-position room measurement, review the proposed
correction, apply it, verify it, and see whether it helped.

### Screen inventory (main path, top to bottom)

The document shell is fixed; `GET /envelope` supplies the ordered `sections` list per
screen and the browser only maps names to nodes (`main.js:1675 renderSections`).

1. **Header + tab strip + page-sub** — `correction_room_flow.py:44-46`.
2. **Wizard chrome** (`:52-59`) — step spine (`Set up · Measure · Review · Apply · Verify ·
   Done`, `envelope.py:198-213`), one verdict sentence, nudge stack, ONE primary button,
   plus a `btn--danger` "Cancel measurement" while a run is live.
3. **Envelope sections**, shown/hidden per screen: `current-correction` (:61),
   `tuning` (:67), `readiness-blocker` (:76), `placement` (:81), `local-certificate-warning`
   (:92), `capture-setup` (mic/calibration + a "Capture settings" constraint table + live
   level meter, :96-177), `run-defaults` (:179-199), `level-check` (:201-213),
   `position-capture` (:215-219), `measurement-review` (chart + colour-legend prose,
   :221-241), `apply-status` (:243), `verification` (:246), `result-proof` (:250 — an empty
   host; the chart is re-parented into it), `reports` (:252-259).

States present: loading (verdict "Checking current correction…"), blocked idle
(readiness-blocker + no Start), in-progress (steps + cancel), failure (typed homeowner
sentence), success (`Done. <headline>…`), empty (reports section withheld when no bundle).

Verdict: **TIDY** — the flow architecture is genuinely good and matches the design of
record; the problems are local.

### Findings

- **[H] room-stale-csrf — Prevent/recover errors.** `main.js:2593-2628` re-implements
  `postJson`/`responseError` locally and **omits the shared module's stale-session
  recovery**. `shared/js/http.js:76,183-186` detects a 403-with-non-JSON-body (the
  `guard_mutating_request` rejection that happens after the socket-activated daemon
  idle-exits and respawns — the daemon does idle-exit, `correction_setup.py:5170`) and
  auto-reloads to mint a fresh token. Room's copy classifies it as a generic failure:
  `responseError` gets `payload = null` → `failure = null` → "The speaker could not
  continue this step. Try again." Retrying re-sends the same dead token, so a Room tab left
  open overnight is a permanent dead end with copy that tells you to retry. → Add the
  `isStaleSessionRejection(err)` branch to `postJson`/`fetchStatus` before the generic
  fallback (import from `shared/js/http.js`; do **not** replace the whole helper — the local
  copy exists to map the server's typed `failure` block, which is ratified copy policy).
  (S)

- **[M] room-raw-webrtc-table — Progressive disclosure / implementation vocabulary.**
  `correction_room_flow.py:156-163` renders a four-column table whose rows are literally
  `echoCancellation` / `noiseSuppression` / `autoGainControl` / `channelCount` /
  `sampleRate`, with values `false` and `undefined (= off)`
  (`main.js:766-804 renderConstraints`). It is shown expanded on every local mic grant
  (`main.js:980`). Directly below it, `#browser-audio-report` says the same thing in plain
  language ("Browser audio path: ready / blocked / usable with warnings",
  `main.js:807-822`). Two verdicts of one fact, the raw one first. Note the sibling page
  already does this right: `crossover/main.js:165-171` folds its expert numbers into a
  `<details>`. → Delete the table, or move it inside a `<details><summary>Technical
  details</summary>`; keep the plain card as the primary. (S)

- **[M] room-chart-legend-prose — Prägnanz / consistency.**
  `correction_room_flow.py:227-238` is a single paragraph naming six colours in prose, with
  seven hardcoded hex values inline (`#d44`, `#888`, `#1db954`, `#2b7bb9`, `#a050d0`,
  `#d68200`) that must be kept in sync by hand with seven more in
  `main.js:2541-2553`. The crossover page in the same cluster ships a proper `<ul>` legend
  with class-based swatches (`correction_crossover_flow.py:76-82`) and reads its colours
  from CSS custom properties via `cssColor()` (`crossover/chart.js:29-54`). → Replace the
  prose paragraph with the crossover's legend list; move the colours to CSS custom
  properties read through `cssColor()`. Fixes the hard-coded-colour drift at the same time.
  (M)

- **[M] room-reports-extra-click — Fewest steps.** `correction_room_flow.py:252-259`: the
  reports section only appears when the server already knows a bundle exists (ratified,
  room doc "Screen and whole-page visibility contract"), yet it renders as a heading, a
  hint, and a **"Load recent reports"** button over an empty div; nothing loads until the
  click (`main.js:3404` is the only caller of `loadSessionReports`). The server has already
  paid the discovery cost to decide the section is visible. → Auto-load when the section is
  revealed; keep the button only as a "Refresh" if wanted. (S)

- **[M] room-inflight-silence — Feedback within ~400 ms.** On tap, `renderPrimaryAction`
  hides the button entirely (`main.js:1399-1403`) and `setStateBadge` is a **no-op for every
  state except `failed`** (`main.js:1148-1159`) — so the ten `setStateBadge('preparing',
  'pausing music…')` / `('analyzing', 'applying to CamillaDSP…')` call sites render nothing.
  Between the tap and the POST returning (a graph mutation, a volume claim) the page shows a
  vanished button and stale copy. There is precedent for a bounded browser-owned in-flight
  sentence: `main.js:1436-1439` already sets one for `/local-capture/setup`. → Set the same
  bounded "Working…" verdict for every in-flight action, and delete the dead `detail`
  arguments (see hygiene below). (S)

- **[M] room-paid-call-undisclosed — Sensible defaults / no surprise commitment.**
  "Explain my room" and "Suggest a tweak" (`correction_room_flow.py:71-72`) fire a paid
  provider call on the first tap with no confirm and no cost wording
  (`main.js:1803-1847`); `envelope.py:1281-1295` emits no disclosure string. Note
  `envelope.py:66-67` asserts these endpoints are "per-tap and **confirm-gated**" — the
  confirm does not exist. Server-side risk is bounded by the daily spend cap
  (`correction_tuning.py:265`), so this is copy, not safety. → Add one disclosure sentence
  to the tuning block (server-owned, per the copy contract) and fix the stale comment. (S)

### Declined (ratified — not findings)

- No mid-run resume after a closed tab: room doc "Returning-user state" ratifies *"Incomplete
  prior run | No invented continuation."*
- The mic/calibration panel's size and the `capture-setup` section existing at all: ratified
  section vocabulary, local-backup-only.
- Room's local `postJson` instead of shared `postJSON`: it exists to map the server's typed
  `failure` block into homeowner copy, which the shared helper cannot do. Ratified by the
  "Typed homeowner failures" contract.
- Six-position / flat-target / balanced defaults shown as a sentence + "Change": ratified
  "Defaults over decisions".

### Hygiene

- **[M] room-mainjs-monolith.** `deploy/assets/correction/js/main.js` is 3450 lines in a
  **single IIFE inside an ES module** (`main.js:22`, `})();` at :3449) using `var`
  throughout — the IIFE is dead ceremony (a module already has its own scope) and is the
  thing blocking a split. The seams are already clean and non-overlapping:
  mic/calibration/device (378-760), capture constraints + browser-audio report (766-860),
  getUserMedia/AudioWorklet capture (903-1135), current-correction banner (1201-1317),
  envelope router + step/nudge/action/section render (1318-1745), tuning assistant
  (1742-1985), reports reader (2157-2402), canvas chart (2403-2565), transport helpers
  (2566-2643), WAV encode (2644-2714), measurement/autolevel/verify state machine
  (2715-3440). → Drop the IIFE, split into `js/room/{mic,capture,envelope,tuning,reports,
  chart,measure}.js` behind a thin `main.js`. Roughly 7 mechanical PRs or 2 larger ones. (L)

- **[M] room-second-chart.** `main.js:2403-2565 drawChart` is a **second** log-frequency
  canvas renderer alongside `crossover/frequency-chart.js` (169 lines), which already has
  two consumers (`crossover/chart.js`, `measurements.js`). AGENTS.md "Duplication": two
  implementations of one concern in reach → converge or open an issue. Honest caveat: they
  are not drop-in — `frequency-chart.js` plots deviation-from-reference and Room plots
  absolute dB with a target curve and a helped/hurt fill between two curves. → Converge the
  *theme, axis, grid and DPR* layer first (that is where the hardcoded `#e6e6e6`/`#bbb`/
  `#888` at `main.js:2455,2469` live); leave the fill as a Room-specific overlay. (M)

- **[L] room-dead-detail-args.** `setStateBadge(state, detail)` ignores both arguments for
  every non-`failed` state (`main.js:1148`). Ten call sites pass carefully worded detail
  strings that reach nothing. Fold into room-inflight-silence. (S)

- **[L] room-inline-styles.** 21 inline `style=` attributes in
  `correction_room_flow.py` (`:89,90,99,109,150,186,188,193,197,203,207,208,210,211,
  229-236`). No inline `<script>`/`<style>` blocks anywhere in the cluster's Python —
  that part is clean — but these one-off margins belong in `correction.css`. (S)

- **[L] room-form-dummy.** `correction_room_flow.py:183,189,194` — three selects carry
  `form="dummy"` pointing at an id that does not exist anywhere in the tree. There is no
  ancestor `<form>` either, so the attribute is a no-op incantation. Delete. (S)

- **[L] room-hidden-class.** `correction.css:29` re-declares `.hidden { display:none
  !important }` although `app.css:143` already provides `[hidden]`. The room JS uses the
  class 70 times; `crossover/main.js` (6) and `bass/main.js` (13) use the `hidden` property.
  Two visibility idioms in one cluster. → Migrate Room to `el.hidden` and drop the rule. (M)

- **[L] room-envelope-dead-field.** `envelope.py:1257` emits a top-level `headline` object
  that no consumer reads — it is already interpolated into `verdict_text` server-side
  (`:972,976`) and `main.js` never touches it. Dead payload. (S)

---

## /sound/crossover/ — "calibrate the active crossover (Active speaker)"

Files: `jasper/web/correction_crossover_flow.py` (534),
`jasper/web/correction_crossover_v2.py` (**7601**),
`correction_crossover_backend.py` (1748), `correction_crossover_v2_wired.py` (1142),
`_v2_status.py` (858), `_v2_republish.py` (374), `_context.py` (63),
`jasper/active_speaker/crossover_envelope_v2.py` (3494),
`deploy/assets/correction/crossover.css` (395),
`deploy/assets/correction/js/crossover/{main.js 1090, cloud.js 350, frequency-chart.js 169,
chart.js 57}`.

Primary user task: measure each driver with a mic, review the measured crossover candidate,
apply it, verify it, and continue to Room correction.

### Screen inventory (main path)

1. **Accent card** (`correction_crossover_flow.py:34-45`) — eyebrow "Speaker layer", title
   "Calibrate the active crossover", live verdict line, applied chip, then a **footer
   carrying "Start over" and "Remove the active crossover entirely"**.
2. **Progress card** (`:47-51`) — `.wizard-steps` spine + live nudge region.
3. **Measured crossover review** (`:53-57`) — hidden until a candidate exists; filled by
   `crossover/main.js:237-520 renderCandidateReview` (trims/delay/polarity/octave rows,
   provenance in `<details>`).
4. **Before/after chart card** (`:70-89`) — one section that reframes its own heading
   between "prediction" and "measured" (`cloud.js updateSectionFraming`), proper `<ul>`
   legend with class swatches, callouts.
5. **Action card** (`:91-108`) — the primary/alternate action row, the relay walkthrough
   (eyebrow / headline / hint / release control), stop button, status line.

States: loading ("Checking the speaker…"), not-applicable (passive speaker), tier chooser
(two equal-weight `btn--primary` cards, one badged "Recommended"), in-flight walkthrough,
review, applying, done, verify-fail with `<details>` expert numbers.

Verdict: **TIDY** (page) / **RESTRUCTURE** (the Python behind it — see hygiene).

### Findings

- **[M] xo-destructive-at-top — Serial position / Von Restorff.**
  `correction_crossover_flow.py:38-44` puts **"Start over"** and **"Remove the active
  crossover entirely"** in the very first card, before the household has seen a single step,
  and they are present in every state including the first-run start screen. Essentials
  first, escape hatches last. (The confirm copy is good and grouping-aware —
  `crossover/main.js:867`.) → Move both to a quiet footer below the action card, or into
  the review/done screens where "start over" is actually a plausible intent. (S)

- **[M] xo-no-review-step — Consistency with the design of record.** `_STEP_IDS` is
  `speaker_setup · microphone_check · measure · apply · verify`
  (`crossover_envelope_v2.py:149-162`) — there is **no Review step and no terminal Done
  step**, even though the page has a dedicated `#crossover-review` section
  (`correction_crossover_flow.py:53`) and the crossover doc ratifies *"Step 4: review"* as
  its own step. Room's spine, for the same domain, is `Set up · Measure · Review · Apply ·
  Verify · Done` (`envelope.py:198-213`). On completion the crossover spine just marks
  Verify done (`crossover_envelope_v2.py:3478-3479`) — no distinct completion marker, where
  Room gets an explicit "Done". Two step grammars for one journey the household walks
  back-to-back. → Insert `review` between `measure` and `apply`, and a terminal `done`
  aligned with the existing `screen="done"`. (S in the envelope; the renderer already
  handles arbitrary step lists.)

- **[L] xo-implementation-eyebrow — Progressive disclosure.**
  `correction_crossover_flow.py:35` uses "**Speaker layer**" as the card eyebrow. That is
  layer-architecture vocabulary; the crossover doc's own Language guide says to prefer the
  household's task wording and lists internal nouns as things to avoid as primary copy.
  → "Active speaker" or "Speaker tuning". (S)

- **[L] xo-mixed-ingress — Consistency.** The tab strip mints `/sound/crossover/`,
  `/sound/room/` etc. (`correction_hub.py:11-19`) but the done screen's forward action is
  `href: "/correction/room/"` (`crossover_envelope_v2.py:3386-3391`), and
  `publicCrossoverUrl` (`crossover/main.js:65-75`) only rewrites `/correction/crossover*`.
  Both URLs work (the daemon rstrips the trailing slash, `correction_setup.py:4666`), but a
  household walking the ratified "Continue to room correction" hand-off silently changes
  ingress family mid-journey. → Emit `/sound/room/`, or extend the rewrite. (S)

### Declined (ratified)

- Two equal-weight `btn--primary` tier-choice cards: explicitly reasoned in
  `crossover/main.js:537-556` as equal-weight peers with the Recommended badge as the only
  differentiator. A working-as-intended choice, not a competing-CTA defect.
- One section that swaps its own heading between "prediction" and "measured": documented
  and correct (`correction_crossover_flow.py:59-68`, issue #2152).
- Merging the crossover and Room envelopes / sessions / state machines: the room doc's Scope
  lists *"A generic tab, session, envelope, graph, or wizard framework"* as a non-goal and
  says the domains *"do not share feature envelopes, product sessions, state machines, or
  browser policy."* Only the policy-free presentation primitives are fair game (below).

### Hygiene

- **[H] xo-v2-monolith.** `jasper/web/correction_crossover_v2.py` is **7601 lines** in one
  module with **no rendering in it at all** — it is session orchestration living in
  `jasper/web/`. It already carries its own banner comments marking clean seams; the split
  is largely mechanical:
  | Lines | Seam | approx |
  |---|---|---|
  | 1-363 | refusal vocabulary / failure classification | 360 |
  | 364-877 | durable v2 state (`load/save/reset/observe_*`) | 510 |
  | 878-1424 | session volume plan, measurement pause, volume recovery + ceiling | 550 |
  | 1425-1803 | post-apply / spatial grading | 380 |
  | 1804-2386 | conductor persistence, staged prescription + angle walk | 580 |
  | 2387-4099 | production seam bindings (analyze/play/evidence/findings/cloud) | 1710 |
  | 4100-4567 | conductor context resolution + stage-2 preflight | 470 |
  | 4568-5135 | endpoint preparation + `PositionGate` | 570 |
  | 5136-6834 | stage capability binding + `prepare_v2_session` | 1700 |
  | 6835-7601 | apply transaction + rollback probe | 770 |

  Two functions dominate: **`prepare_v2_session` is ~920 lines** (5914-6834) and
  `handle_v2_apply` ~400 (6893-7296). → A first PR that moves 364-877 (durable state) and
  878-1424 (volume/pause) into `jasper/active_speaker/crossover_v2/` siblings is ~1000 moved
  lines with no logic change and buys the most. Splitting `prepare_v2_session` is judgement
  work, not mechanical. (L)

- **[M] xo-router-monolith.** `_make_handler` in `correction_setup.py:3911-4971` is a
  **1060-line nested class** with a flat `if path == …` chain dispatching Room, crossover
  v2, bass, measurements, balance and sync for one daemon
  (`correction_setup.py:3993-4960`). → Extract a `{(method, path): handler}` table per
  domain; the per-route bodies already delegate to the right module. (M)

- **[L] xo-css-off-ladder.** `crossover.css` font sizes: `0.9rem x3, 0.82rem x2, 1.05rem,
  0.95rem, 0.8125rem, 0.78rem, 11px` — one of nine on the 11/12/13/14/16 ladder
  (design-language §3, whose §1 mandate is *exactly* these surfaces). See the shared finding
  below. (S)

---

## /sound/bass/ — "read-only view of where the sub and mains hand off"

Files: `jasper/web/correction_bass_flow.py` (137),
`deploy/assets/correction/js/bass/main.js` (246); reuses `crossover.css`.

Primary user task: find out what the bass-management corner is, who owns it, and where the
bass-region correction actually lives.

### Screen inventory

1. Accent card — "Bass management" + a hint that says outright *"This page is read-only:
   the corner is set when your speaker is set up, not here"* (`correction_bass_flow.py:41-47`).
2. "Current bass management" — `aria-live` message + definition list (`:49-53`).
3. "Bass extension" — hidden unless commissioned; message + list + a recovery-pending
   nudge (`:55-62`).
4. "Bass-region correction" — one paragraph plus a single ghost button **"Go to Room
   measurement"** (`:64-72`).

States: loading, populated, "not configured", read-failure ("Could not read the
bass-management state right now.", `bass/main.js:216-226`), render-failure of the newer
section only (`:230-242`).

Verdict: **KEEP.**

This is the best page in the cluster. One purpose, stated in the first paragraph; a single
forward action pointing at where the work actually happens; honest and *distinct* copy for
"not commissioned" vs "could not read"; a real fail-soft on the newer sub-feature; clean
modern JS using `h()` and `getJSON` from `shared/js/`; no page-specific CSS of its own.

### Findings

None worth filing. It is display-only by design (`correction_bass_flow.py:6-18`) so the
absence of flow chrome, progress and completion state is correct, not a gap.

### Hygiene

- **[L] bass-borrows-crossover-css.** `correction_bass_flow.py:87` loads
  `/assets/correction/crossover.css` for a page that is not the crossover. It needs
  `.deflist` and card spacing only. Harmless today; it silently couples a stable read-only
  page to a stylesheet that changes with the crossover flow. → Either promote the two rules
  it uses to `app.css`, or add a 20-line `bass.css`. (S)

---

## /sound/measurements/ — "browse and A/B saved frequency-response measurements"

Files: `jasper/web/correction_measurements.py` (99),
`deploy/assets/correction/js/measurements.js` (291),
`deploy/assets/correction/measurements.css` (119).

Primary user task: pick one or two saved runs and compare their curves and metadata.

### Screen inventory

1. Accent card — "Saved measurements / Frequency response / Choose one measurement, or add
   a second for an A/B view" (`correction_measurements.py:38-42`).
2. Picker card — two labelled selects, A and B (B has a "None" option) (`:44-49`).
3. Chart card — canvas, `role="status"` line, per-series toggle legend with SVG swatches
   (`:51-58`).
4. Metadata — one `info-card` per selected run, `dl` of ten rows (`:60`).

States: loading ("Loading measurements…"), empty ("No saved speaker measurements are
available yet.", `measurements.js:260`), populated, error (`measurements.js:288-289`).

Verdict: **TIDY.** Small, modern, on-system. Two real gaps.

### Findings

- **[M] meas-raw-error-copy — Errors say what happened + what to do.** The catch renders
  `error.message` verbatim (`measurements.js:287-289`). That message comes from
  `shared/js/http.js:135-139`, which produces literally **`"HTTP 500"`** for a non-JSON
  failure — and the server hands back raw exception text on the 500 path
  (`correction_setup.py:4544-4546`: `{"error": str(exc)}` from an `OSError`/`ValueError`).
  So an unreadable bundle prints a Python exception string, or `HTTP 500`, as the page's
  status line. (The room doc bans exactly this shape for Room; the same standard should
  hold one tab over.) → Render a fixed sentence for a failed load and keep the detail in
  `console.warn` / the server log; stop returning `str(exc)` from that route. (S)

- **[M] meas-empty-state-dead-end — What next / fewest steps.** The empty state is a bare
  sentence, "No saved speaker measurements are available yet." (`measurements.js:260`) on a
  page whose only other controls are two empty selects. Nothing tells the household how a
  measurement comes to exist. → Add one line + link: "Run a room measurement to create
  one → /sound/room/". (S)

- **[L] meas-picker-exposes-ids.** `optionLabel` is `"<localised date> · <bundle id>"`
  (`measurements.js:41-43`), so the picker reads `9/2/2026, 4:12:05 PM ·
  room_20260902_161205_9f3c`. This *is* an evidence-browsing page, so a raw id in the
  metadata card (`detailRows` "Graph", `:227`) is fair; in the picker it just crowds the
  one thing that distinguishes the options. → Date + measurement family in the option;
  keep the id in the metadata card. (S)

### Hygiene

Nothing. `measurements.css` is the only stylesheet in the cluster fully on the type ladder
(13/11/12px), and the JS uses `h`, `svg`, `getJSON` from `shared/js/` plus the shared
`frequency-chart.js`. This page is the model the other two should be brought to.

---

## /correction/ (the "hub")

Files: `jasper/web/correction_hub.py` (36) — a tab strip only.
`correction_report.py` (100) and `correction_tuning.py` (335) are backend-only payload/
provider helpers with no UI surface of their own; both are cohesive and in-scope-sized.

Verdict: **KEEP** the no-landing-page decision (an extra hop for a four-tab strip would
cost a click for nothing, and the domain boundaries are ratified in the room doc's
"Organizing frame"). One finding on the strip itself.

### Findings

- **[M] hub-stateless-tabs — Essentials first / feedback.** The strip
  (`correction_hub.py:11-19`) is four static links with no state: nothing distinguishes
  "Room correction is applied", "the active crossover is not commissioned yet", "Bass is not
  configured", "there are no saved measurements". The household must open each tab to find
  out — and for an *active* speaker the ordering (Room first) is backwards from the required
  order of operations, which the crossover doc ends with "Continue to room correction" and
  which Room itself enforces via a readiness blocker. The blocker does recover the user, so
  this is friction, not a wrong mental model. → Carry a small state dot/chip per tab from
  the data each domain already publishes (`/crossover/status.applied`,
  `current-correction`, `/bass/status.configured`, catalog length). Server-rendered into
  `section_tabs`, no new endpoint. (M)

---

## Cluster summary

### Ranked — most work needed first

1. **/sound/crossover/** — the page itself is good, but `correction_crossover_v2.py` (7601
   lines, a 920-line `prepare_v2_session`) is the cluster's largest hygiene debt, and the
   step spine is missing the Review and Done steps its own design of record ratifies.
2. **/sound/room/** — the flow architecture is right and matches the doc; it carries the
   cluster's only High finding (a stale-CSRF dead end after daemon idle-exit), a raw WebRTC
   constraint table shown to a household, a dead in-flight feedback path, and a 3450-line
   single-IIFE `main.js` that is ripe for a mechanical split.
3. **/sound/measurements/** — small and well built; needs a real empty state and must stop
   printing `HTTP 500` / raw exception text as page copy.
4. **/correction/ hub** — one finding: the tab strip carries no state.
5. **/sound/bass/** — KEEP. The cluster's reference page; borrows `crossover.css` and
   nothing else is wrong with it.

### Cross-page patterns in this cluster

- **The wizard component exists twice and has already diverged.** `.wizard-steps` /
  `.wizard-step` / `.wizard-nudge` are duplicated between `correction.css:134-235` and
  `crossover.css:65-140` — `crossover.css:65-73` says so in its own comment. They diverge on
  the current-step modifier (`.current` vs `.active`) and on nudge anatomy (Room renders an
  icon span + text, `main.js:1364-1390`; crossover renders a bare `<p>`,
  `crossover/main.js:141-148`), so the same component looks different on two tabs the
  household walks in sequence. This is a **policy-free presentation primitive with a proven
  second consumer** — exactly the seam the room doc's "Organizing frame" permits — so
  converging it does not touch the ratified no-shared-envelope rule. Promote to `app.css`,
  pick one modifier name, one nudge anatomy.
- **Three canvas chart implementations, two colour disciplines.**
  `crossover/frequency-chart.js` (token-driven via `cssColor`, two consumers) vs
  `main.js drawChart` (hardcoded hex, seven more hexes duplicated in the Python legend
  paragraph). Converging the theme/axis/grid layer removes both hardcoded sets.
- **Two eras of browser code.** `measurements.js`, `bass/main.js` and `crossover/*` are
  modern ES modules using `shared/js/{dom,http,escape,dialog}.js`, `el.hidden`, and DOM
  builders (0 `innerHTML`). `correction/js/main.js` is a pre-module IIFE with `var`, 70
  `classList.add('hidden')` calls, 14 `innerHTML` sites, and a local re-implementation of
  the shared fetch helpers (which is where the High finding comes from). One page is a
  generation behind its three siblings.
- **Type ladder unenforced off the landing page.** `tests/test_web_design_system.py:399` runs
  the ladder guard against `deploy/index.html` **only**. In the cluster:
  `correction.css` has 25 `font-size` declarations across 10 distinct values, **none** on the
  ladder; `crossover.css` has 1 of 9 on it; `measurements.css` is 5 of 5 on it.
  design-language §1 names these surfaces as its mandate, so the cheap fix is to widen the
  existing guard to the cluster's stylesheets and map the values to the nearest step.
- **The brief's "four flows each re-implement measure→review→apply" hypothesis does not
  hold.** Only two of the four routes are flows; Bass is display-only by design and
  Measurements is a read-only browser. The two real flows share a *pattern* deliberately
  (`correction_crossover_flow.py:handle_envelope` docstring: "aligned with the room flow's
  envelope-driven pattern"), and merging their envelopes is a ratified non-goal. The real,
  fixable duplication is the three items above — CSS, chart, browser-module era — not the
  state machines.
- **No inline `<script>` or `<style>` anywhere in the cluster's Python.** Every page ships
  a static `page_css_href` and a static ES module. That part of the hygiene brief is already
  satisfied; the remaining Python-string issue is 21 inline `style=` attributes and 7 hex
  colours in `correction_room_flow.py`.

### Estimated PR breakdown (single-concern, < 400 changed lines each)

| # | PR | Fixes | Tier |
|---|---|---|---|
| 1 | Room: stale-session recovery in `postJson`/`fetchStatus` + real in-flight verdict; delete dead `setStateBadge` detail args | room-stale-csrf (H), room-inflight-silence, room-dead-detail-args | Sonnet |
| 2 | Room: fold the WebRTC constraint table into `<details>` (or delete, keeping the plain browser-audio card); auto-load reports on reveal | room-raw-webrtc-table, room-reports-extra-click | Sonnet |
| 3 | Promote `.wizard-steps`/`.wizard-step`/`.wizard-nudge` to `app.css`; unify on one modifier name and one nudge anatomy; delete both page copies | cluster-wide component divergence | Opus |
| 4 | Room chart: prose legend → the crossover's `<ul>` legend; hex colours → CSS custom properties read via `cssColor()`; delete 21 inline `style=` and `form="dummy"` | room-chart-legend-prose, room-inline-styles, room-form-dummy | Sonnet |
| 5 | Widen the type-ladder guard to the correction stylesheets; map `correction.css` + `crossover.css` sizes onto 11/12/13/14/16 | css-off-ladder | Sonnet |
| 6 | Measurements: fixed error copy + stop returning `str(exc)` from `/measurements/data`; empty state with a link to `/sound/room/`; drop the bundle id from the picker | meas-raw-error-copy, meas-empty-state-dead-end, meas-picker-exposes-ids | Sonnet |
| 7 | Crossover: add `review` and `done` to `_STEP_IDS` + `_PHASE_STEP`; move "Start over"/"Remove entirely" out of the first card; eyebrow rename; emit `/sound/room/` | xo-no-review-step, xo-destructive-at-top, xo-implementation-eyebrow, xo-mixed-ingress | Opus |
| 8 | Extract `correction_crossover_v2.py` lines 364-877 (durable state) into `jasper/active_speaker/crossover_v2/state.py` — pure move | xo-v2-monolith, part 1 | Sonnet |
| 9 | Extract lines 878-1424 (session volume plan / pause / recovery / ceiling) — pure move | xo-v2-monolith, part 2 | Sonnet |
| 10 | Room `main.js`: drop the IIFE, extract `js/room/{chart,reports,tuning}.js` (three self-contained leaves) behind the existing entry | room-mainjs-monolith, part 1 | Sonnet |
| 11 | Room `main.js`: extract `js/room/{mic,capture}.js` (the getUserMedia/calibration half) | room-mainjs-monolith, part 2 | Opus |
| 12 | `section_tabs`: server-rendered per-tab state chip | hub-stateless-tabs | Opus |
| 13 | Optional: converge the room chart's theme/axis/grid layer onto `frequency-chart.js`, keeping the helped/hurt fill as a Room overlay | room-second-chart | Opus |
| 14 | Optional: `correction_setup._make_handler` → per-domain dispatch tables | xo-router-monolith | Opus |
