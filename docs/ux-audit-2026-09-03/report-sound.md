# UX / IA audit — SOUND PROFILE / EQ cluster

Routes audited: `/sound/` (redirect), `/eq/`, `/sound/setup/`.
All three are one daemon (`jasper-web` thread on 127.0.0.1:8784), one Python
module, one JS module, one stylesheet. nginx picks the page with a header:
`deploy/nginx-jasper.conf:149-190` — `= /sound` and `= /sound/` both 302 to
`/sound/setup/`; `/eq/` and `/sound/setup/` proxy to the same origin with
`X-JTS-Sound-Page: eq|setup`; `/sound/` is a catch-all compatibility ingress.

`jasper/web/active_speaker_flow.py` (90 lines) is **not** a renderer — it is the
commissioning <-> measurement mutual-exclusion advisory (`active_phase()` /
`blocking_measurement_phase()`). Nothing to audit for UX; it is well-scoped and
well-documented. Not mentioned again.

Shared files (counted once):
`jasper/web/sound_setup.py` (5,544),
`deploy/assets/sound-profile/sound.css` (1,012),
`deploy/assets/sound-profile/js/main.js` (7,649 / 363 KB),
`js/active-speaker-ui.js` (677), `js/eq-math.js` (91).

---

## /sound/  —  not a page

`deploy/nginx-jasper.conf:149-163`. Both `= /sound` and `= /sound/` return 302
to `/sound/setup/`. Correct and cheap. **Verdict: KEEP.**

Links out to the sibling cluster: **there are none from this cluster's code.**
`grep` for `sound/room|sound/crossover|sound/bass|sound/measurements` across
`deploy/assets/sound-profile/js/*` and `jasper/web/sound_setup.py` returns
nothing. The only connective tissue is the landing page's Sound section
(`deploy/index.html:634-676`), which lists EQ / Sound setup / Active speaker /
Room correction / Bass as five peer rows. Traffic goes the *other* way: the
measurement wizard hard-stops deep-link **into** this cluster at
`/sound/setup/#confirm-safety-limits`
(`jasper/active_speaker/crossover_v2/refusal_copy.py`, pinned by
`tests/test_sound_profile_confirm_deeplink.py`). See finding SND-1 — that link
is currently broken in practice.

---

## /eq/  —  "pick, tweak and save a preference-EQ profile; the tab you are on is what you hear"

Files: `jasper/web/sound_setup.py:1565-1632` (`_index_html`, eq branch ~45
lines of HTML) + `main.js` lines ~43-830 / 4808-5756 (the ~1,900 lines that
actually execute here) + `sound.css:1-308`.

Primary user task: audition and apply an EQ profile.
Secondary: build/edit a custom profile (simple 5-band or up to 8 PEQ bands).

Screen inventory (top to bottom):
1. **App header** — back chevron to `/`, title "EQ".
2. **Tab strip** (`sound_setup.py:1580-1588`) — segmented Off / Saved / Draft,
   `role="tablist"`, buttons carry `aria-pressed` (not `role="tab"`). The tabs
   are *not* navigation: switching one writes the DSP
   (`main.js:5030-5045 reconcileLiveSource` -> `/apply` or `/live-draft`).
3. **Now playing** (`sound_setup.py:1591-1601`) — eyebrow + live label
   ("Bypass" / profile name / "Editing: X · edited") + SVG response graph
   (`#plot`, 620x200, drawn client-side by `renderGraph`, `main.js:507`), plus
   an `sr-only` live summary.
4. **`#view-body`** — one of three:
   - **Off** (`main.js:623`): icon, one line of copy, two buttons — "Try a
     stock profile" (ghost) / "Create custom profile" (primary).
   - **Saved** (`main.js:665`): "Your profiles" list-card (+ inline "New") or
     an empty-card; "Presets" list-card; each row = select / edit / delete.
     Then the **Match loudness** toggle (`main.js:741`).
   - **Draft** (`main.js:4896`): Mode segmented (Simple | PEQ); Bands section
     (5 vertical sliders, or collapsible band rows with Type/Freq/Gain/Width +
     Delete + "Add band"); footer button set that varies by
     `editing.kind` (new / user / preset) plus an inline naming card.
5. **Status line** (`sound_setup.py:1603`) — 12px muted, `aria-live="polite"`.

States present: loading (none — first paint is empty until `/state` returns),
empty (Off card + "No profiles yet" card), error (status line only),
in-progress ("Applying...", "Saving"), success (status line text). Blocked (the
carrier refuses EQ) is handled but only after the fact — see EQ-1.

**Verdict: TIDY.** The IA of this page is genuinely good: one job, one primary
button per state, the graph sits above the controls that change it, band rows
disclose on demand. The findings below are local.

Findings:

- **[M] EQ-1 — Prevent errors / disable impossible actions.** On a speaker with
  an active crossover the graph carrier refuses to host preference EQ
  (`jasper/sound/graph_carrier.py:52`, ~20 raise sites). `/state`
  (`sound_setup.py:247-305`) carries no flag for this, so `/eq/` renders the
  full tab strip, profile library and band editor as if they worked; the user
  only learns otherwise after tapping a profile or dragging a band, from a
  bottom-of-page line ("Sound EQ is unavailable for this speaker setup.",
  `main.js:5004`, `5052`). -> Add one boolean + message to `/state` (the carrier
  already has a `can_host_eq`-shaped check for the pre-lock fast path), and
  render an `info-card` in place of the tabs/editor when false, with a link to
  `/sound/setup/`. (effort M)

- **[M] EQ-2 — Doherty / mental model: an unsaved Draft outlives the page.**
  `/live-draft` writes the running CamillaDSP graph and deliberately persists
  nothing (`sound_setup.py:1312-1330`: "no profile persistence, no config-file
  pointer change"). `pagehide` (`main.js:7638-7646`) only stops the summed-test
  and volume-floor tones. So navigating away mid-edit leaves the speaker playing
  an EQ that no page will ever show again: a reload calls `loadState()`
  (`main.js:7606`) and opens on Off or Saved from the *persisted* profile,
  contradicting what is audible until the user touches a control. -> On
  `pagehide`, if `view === 'draft'`, re-post the persisted live source
  (`applyProfile(applied...)`) with `keepalive`, exactly as the two tone stoppers
  already do. Two lines beside the existing handler. (effort S)

- **[L] EQ-3 — Colour semantics / Von Restorff.** `renderTabs`
  (`main.js:552-558`) sets `is-live` on whichever tab is *selected* — it is a
  pure alias for `aria-pressed`. `sound.css:8-18` paints that as a 6px
  `var(--destructive)` dot, the same red as the Delete-profile action. A red
  dot on the selected tab reads as an alert, and it duplicates a state the
  segmented control already carries. -> Delete `is-live` and its rule
  (~14 lines). (effort S)

- **[L] EQ-4 — Chunking: one global setting is hidden inside one tab.**
  "Match loudness" is appended only by `renderSaved` (`main.js:680`), so it is
  invisible from Off and Draft; the other two global sound settings (volume
  floor, extra headroom) live on a different page entirely
  (`main.js:754 renderSetupSoundSettings`). Three settings of the same class,
  three different places. -> Either move Match loudness next to the other two on
  `/sound/setup/`, or render it under all three tabs. Prefer the former; it is
  a set-once preference, not a per-tab control. (effort S)

- **[L] EQ-5 — Consistency (Jakob) / a11y.** `role="tablist"`
  (`sound_setup.py:1582`) wraps buttons that use `aria-pressed`, not
  `role="tab"`/`aria-selected`, and `#view-body` is not a `tabpanel` and is not
  `aria-controls`-linked. A screen reader announces a tablist containing no
  tabs. Given the tabs also *act* (they write the DSP), `role="group"` +
  `aria-pressed` is the honest markup. -> Drop `role="tablist"`, keep the
  `aria-label`. (effort S)

---

## /sound/setup/  —  "an I2S HAT toggle, two volume-shaping sliders, and, collapsed at the bottom, the entire five-step active-crossover commissioning wizard"

Files: `jasper/web/sound_setup.py:1565-1632` (setup branch, ~18 lines of HTML)
+ `sound_setup.py:1635-4715` (~3,100 lines of active-speaker JSON payload
builders) + `main.js` lines ~803-4805 and 5757-7595 (~5,800 lines) +
`sound.css:282-1012`.

Primary user task: **commission an active speaker** — choose a layout, declare
drivers, confirm outputs, test them combined, apply the profile. This is the
destination of the landing page's "Speaker output is locked until active
crossover setup is complete. Continue setup in Sound"
(`deploy/index.html:563-566`).
Secondary tasks: enable the I2S HAT (a reboot-level hardware change); set the
volume floor; set extra headroom.

Screen inventory (top to bottom) — `main.js:588 renderSetup`:
1. **App header** — back chevron, title "Sound setup". No tabs.
2. **I2S HAT card** (`main.js:594`) — title, hint with saved/detected state, a
   toggle, optional error line, optional "Restart required" accent card with a
   link to `/system/`, an optional shared-USB-port warning paragraph, and an
   unconditional four-sentence hardware-safety paragraph.
3. **`<details>` "Advanced"** (`main.js:754`, open only if either value is
   non-default) — **Volume floor**: range + "Start tone" + "Save floor" +
   "Reset floor" + readout (four controls in one row); **Extra headroom**:
   range + readout.
4. **`<details>` "Speaker setup"** (`main.js:803`) — bare summary text, no
   status. Inside: "Active crossover setup" head, then either a loading line,
   an error block, an empty "Refresh hardware to start a speaker layout" block,
   or the five step cards (`main.js:3874 renderOutputTopologyBody`), each its
   own `<details>` with done/active/todo state and a check marker:
   1. **Choose speaker layout** — layout/mode axis pickers, subwoofer card,
      hardware card, "Refresh hardware", Save/Continue.
   2. **Add your components** — safety-review callout, safety warnings, per-
      driver component forms, build notes, an AI helper (copy a prompt ->
      paste JSON back -> echo-back table of every value with
      published/estimated badges), crossover-preview card, and a third-level
      `<details>` "Advanced" with per-driver values + crossover points.
      (~1,800 lines of renderers.)
   3. **Confirm outputs** — cross-child notice, stage card, group cards with
      per-role tone controls, identity confirmation list.
   4. **Test combined drivers** — summed-test level control, play/stop, record
      validation (or a "not required" card).
   5. **Validate and apply** — baseline profile card, level-match summary,
      save-and-apply.
   Then **"Tune with an AI operator"** (a copyable handoff prompt) and a
   **"Reset speaker setup"** danger button (properly confirmed —
   `main.js:7036-7040`).
5. **Status line** (`sound_setup.py:1619`) — receives 91 of this page's
   messages.

Follower variant (`sound_setup.py:1491-1560`): the same page is preceded by an
accent card "Sound is controlled by the pair leader" whose primary button
leaves for the leader.

**Verdict: RESTRUCTURE.**

Findings:

- **[H] SND-1 — Progressive disclosure: the measurement wizard's hard-stop
  deep link lands on a collapsed section.** `applySafetyLimitsDeepLink`
  (`main.js:3674-3683`) opens the owning *step card* (`outputStepOverride =
  'research'`) and scrolls — but never sets `activeSpeakerSetupOpen`, so the
  outer `<details data-active-speaker-setup>` (`main.js:806-812`) is still
  closed on a fresh navigation: `activeSpeakerSetupOpen` is `false`
  (`main.js:138`), `touched` is only set by a *non-silent* refresh
  (`main.js:5787`), and `loading` is already back to `false` by the time
  `applySafetyLimitsDeepLink` runs (`ingestOutputTopology` clears it,
  `main.js:5775`, and the deep link is chained off that promise,
  `main.js:7622`). `scrollIntoView` on a node inside a closed `<details>` does
  nothing useful. This is exactly the defect class
  `tests/test_sound_profile_confirm_deeplink.py` exists to prevent — its
  `test_the_review_callout_is_hoisted_out_of_the_advanced_disclosure` guards the
  *inner* Advanced editor and its
  `test_the_deeplink_opens_the_owning_step_before_scrolling` guards the *step
  card*; the outer wrapper was added later and nobody noticed the third layer.
  -> Add `activeSpeakerSetupOpen = true;` to `applySafetyLimitsDeepLink` and
  extend the existing test to assert it. Better: adopt SND-2 and the wrapper
  disappears. (effort S)

- **[H] SND-2 — One purpose per screen + serial position: the page leads with
  its rarest task and buries its only real one.** `renderSetup`
  (`main.js:588-592`) emits, in order: I2S HAT -> Advanced (volume floor,
  headroom) -> Speaker setup. The commissioning wizard is the reason this page
  is linked from the landing page's output-locked banner
  (`deploy/index.html:563-566`), and it is last, collapsed, behind a summary
  that reads only "Speaker setup" with no status. Above it sits a boot-level
  HAT toggle carrying four sentences of hardware-safety prose that render
  unconditionally (`main.js:611-616`) whether or not the user owns a HAT. A
  household arriving from "Continue setup in Sound" sees a HAT warning and two
  closed disclosures. -> Invert: active-crossover setup becomes the page body
  (no outer `<details>`, the step cards already provide the disclosure); I2S HAT
  and the volume-floor/headroom sliders move to the bottom under one "Advanced"
  disclosure. If the two concerns must stay separable, split them into two
  routes (`/sound/setup/` = commissioning; `/sound/output/` = HAT + volume
  shaping) — but one page with the right order is the smaller change. (effort M)

- **[H] SND-3 — Doherty: first paint is two empty disclosures and no loading
  state, for the whole of a seven-request boot.** `loadState`
  (`main.js:7606-7625`) renders, then calls
  `refreshOutputTopology({silent: true})`. Silent mode skips the interim
  `render()` (`main.js:5789`), and the function then `await`s **seven
  sequential** requests — `/output-topology`, `/active-speaker/design-draft`,
  `/crossover-preview`, `/measurements`, `/baseline-profile`,
  `/commission-state`, `/commissioning-view` (`main.js:5793-5821`) — before its
  single closing `render()` (`main.js:5826`). Meanwhile `i2sHat` is still
  `null`, so the HAT card renders as an empty string (`main.js:595`), and
  `outputTopology.loading` is `false` at paint time, so the Speaker-setup
  summary shows nothing. On a Pi Zero 2 W with hardware probes in that chain
  this is seconds of a page that looks broken, followed by a large layout shift.
  -> Render once after `/output-topology` returns (drop `silent` from the first
  render, or add an explicit `render()` after `ingestOutputTopology`), show a
  skeleton/"Checking your speaker..." line while the rest lands, and run the
  four independent GETs with `Promise.allSettled` instead of serially.
  (effort M)

- **[H] SND-4 — Feedback lands where nobody is looking.** Every one of this
  page's 91 `status()` calls (lines > 5700 in `main.js`) writes to a 12px muted
  line at `sound_setup.py:1619` — the very bottom of a page that, fully
  expanded, is several thousand pixels tall. `.status-line`
  (`sound.css:277-278`) is neither sticky nor fixed. Saving the volume floor,
  saving the I2S HAT, copying the research prompt, "Finish the current card
  before opening ..." (`main.js:5634`) and most commissioning errors are
  invisible to a user working at the top of the page. Cards that own an error
  slot (`outputTopology.error`, `activeSpeaker.commissionError`) partly
  compensate; the rest do not. -> Make `.status-line` a sticky bottom toast on
  this page (`position: sticky; bottom: 0`) with the existing tone colours, or
  move the message next to the control that produced it. Sticky is the
  three-line fix. (effort S)

- **[M] SND-5 — Zeigarnik / peak-end: the flow ends with no way onward.** After
  "Validate and apply" succeeds, the only thing that appears is
  `renderTuningHandoffCard` (`main.js:4783`) — a textarea telling the user to
  paste a prompt into "a fresh AI session that has an SSH connection to this
  speaker". There is no "your speaker is commissioned" confirmation and no link
  to the measurement flow (`/sound/crossover/`, `/sound/room/`) that is the
  actual next step; `grep` finds zero links from this cluster to the sibling
  measurement pages. -> After apply, render a short completion card ("Active
  crossover applied — N drivers, crossover at X Hz") with two links: "Measure
  this speaker" -> `/sound/crossover/` and "Room correction" -> `/sound/room/`.
  Keep the AI-operator prompt below it, demoted. (effort S)

- **[M] SND-6 — Fitts / one primary action: the volume-floor row has four
  competing controls.** `renderSetupSoundSettings` (`main.js:775-786`) puts
  range + "Start tone" (ghost) + "Save floor" (primary) + "Reset floor" (ghost)
  + readout in a single wrapping flex row (`.headroom-control`,
  `sound.css:302`), so on a phone they wrap into a three-button block with no
  hierarchy, and the readout — the thing you are actually adjusting — can end up
  last. The neighbouring "Extra headroom" slider, by contrast, saves with no
  button at all (`change` handler, optimistic). Two sibling settings, two
  different commit models. -> Put the readout adjacent to the slider; keep
  "Save floor" as the only button visible when clean, disclose Start tone /
  Reset only while the value is dirty; or make the floor optimistic like
  headroom and drop the button entirely. (effort S)

- **[M] SND-7 — Progressive disclosure: three levels of nesting on the busiest
  step.** "Add your components" is `<details>` (Speaker setup) -> `<details>`
  (step card `research`) -> `<details>` (Advanced driver/crossover editor,
  `main.js:3696`). At full depth the user is four scroll-regions deep in a
  card that also contains a copy-a-prompt / paste-JSON round trip with an
  external AI. The step-card layer is right; the outer layer is SND-2's
  wrapper. -> Removing the outer wrapper (SND-2) fixes this too; no separate
  work. (effort S, folded into SND-2)

- **[M] SND-8 — Follower page: the banner contradicts the page it heads.**
  `_follower_sound_html` (`sound_setup.py:1491-1556`) prepends an
  `info-card--accent` reading "Sound is controlled by the pair leader" with a
  `btn--primary` "Open leader sound" — and on the *setup* page that link points
  at the **leader's** `/sound/setup/` (`sound_setup.py:1507`). But the
  follower's setup page is precisely the surface that is **not** delegated: the
  card's own second sentence says "Local crossover and driver-protection work
  stays with the speaker that owns the DAC path", and `renderFollower`
  (`main.js:581-586`) then renders exactly that work below. The page's dominant
  action sends the user away from its actual job, to a page about a different
  speaker's hardware. -> On `page_mode == "setup"`, drop the delegation card to
  a one-line `form-hint` above the local content (or omit it — the leader link
  belongs on `/eq/`, where the delegation is real) and let the local setup be
  the page. (effort S)

- **[L] SND-9 — Copy: implementation vocabulary in UI text.** "Active crossover
  setup" (`main.js:2151`), "Loading output topology..." (`main.js:3876`),
  "Active crossover setup unavailable" (`main.js:3880`), "Refresh hardware to
  start a speaker layout" (`main.js:3888`). "Output topology" is the Python
  class name (`jasper/output_topology.py`), not a household word, and the empty
  state names the mechanism rather than inviting the action
  (design-language §11: "Empty states invite an action"). -> "Setting up your
  speaker...", "Speaker setup unavailable", and an empty state that says what
  will fill it ("JTS hasn't found your DAC yet — plug it in and tap Find
  hardware"). (effort S)

- **[L] SND-10 — Dead UI branch.** `renderI2sHatSetting` (`main.js:595-596`)
  returns early on `hat.visibility === 'hidden'`, but `_i2s_hat_payload`
  (`sound_setup.py:348`) hard-codes `"visibility": "visible"` and there is no
  other producer of the field. -> Delete the field and the branch. (effort S)

---

## Hygiene (shared across both pages)

- **[H] HYG-1 — `main.js` is two disjoint applications in one 363 KB file.**
  The module is a single IIFE (`main.js:43-7649`) whose branch point is
  `pageMode` (`main.js:223`). `/eq/` executes roughly lines 43-830 plus
  4808-5756 (~1,900 lines); `/sound/setup/` executes ~5,800 different lines
  (803-4805 rendering + 5757-7595 actions/IO) and never touches the band editor
  or the response graph. There is **no shared call path between the two halves**
  — `outputTopology` / `driverResearch` / `activeSpeaker` are referenced nowhere
  before line 803 except their own declarations. Yet `/eq/` — the page most
  likely to be opened repeatedly from a phone — downloads and parses all 363 KB.
  The seams are already clean and self-evident:

  | proposed module | current lines | ~size |
  |---|---|---|
  | `state.js` — island parse, `LIMIT_DEFAULTS`, format helpers, `status()` | 43-270 | 230 |
  | `eq-profile.js` — profile normalise/identity/library selectors | 271-368 | 100 |
  | `eq-graph.js` — preview math + SVG render (`gx/gy/pathD/drawBandMarkers/renderGraph`) | 369-550 | 180 |
  | `eq-editor.js` — Off/Saved/Draft renderers, band rows, footer, naming | 551-700, 4808-4962 | 300 |
  | `eq-api.js` — preview / live-draft / apply / profiles CRUD | 4963-5256, 5650-5756 | 400 |
  | `sound-settings.js` — match-loudness, volume floor + tone, headroom, I2S HAT | 683-802, 5124-5246 | 250 |
  | `output-topology.js` — topology model, templates, hardware/groups/identity cards | 824-2640, 3874-4340, 6117-6490, 6899-7212 | 2,400 |
  | `driver-research.js` — component forms, prompt/paste/echo-back, safety limits, crossover preview | 2642-3873, 6488-6898 | 1,600 |
  | `commissioning.js` — step cards, ramp, summed test, baseline profile, handoff | 4339-4805, 5844-6116, 7213-7595 | 1,100 |
  | `main.js` — boot, event delegation, `render()` dispatch | 552-580, 5257-5650, 7596-7649 | 450 |

  With `pageMode` known at render time, `/eq/` can dynamic-`import()` only the
  first five and `/sound/setup/` only the others — the same delivery model
  `/system/` already uses (the header comment at `main.js:12-18` acknowledges
  this and defers it on hardware-verification grounds, which applies to the
  *editor* half only: the setup half is untouched by that risk). Split the setup
  half first. (effort L, but decomposable — see PR list)

- **[M] HYG-2 — `sound_setup.py` has the same seam, uncut.** Lines 1635-4715
  (~3,100 of 5,544) are `_active_speaker_*` payload builders that `/eq/` never
  reaches; the `do_GET`/`do_POST` handler (4742-5464) is a 720-line if-ladder
  over ~40 paths. The EQ half — `_state_payload`, `_apply_profile`,
  `_apply_settings`, `_live_draft_profile`, `_index_html` — is ~1,200 lines and
  is clean, well-factored code. -> Move the active-speaker builders to
  `jasper/web/active_speaker_setup.py` and have `sound_setup.py`'s handler
  dispatch into it; `_GET_JSON_ROUTES` (`sound_setup.py:4663`) already proves
  the table-driven pattern works, so the POST ladder can become a table too.
  (effort M)

- **[M] HYG-3 — Domain logic in the web layer, imported by another web module.**
  `apply_measured_crossover_geometry` (`sound_setup.py:2055`) writes the
  crossover declaration and is imported *by the other cluster's module*
  (`jasper/web/correction_crossover_v2.py:6951`).
  `jasper/active_speaker/crossover_declaration.py:19` already documents it as
  the writer of its own artifact. -> Move it to
  `jasper/active_speaker/crossover_declaration.py`; both web modules import from
  there. (effort S)

- **[M] HYG-4 — ~450 lines of server routes with no caller.**
  `tests/test_sound_setup.py:1269-1300` explicitly pins that `main.js` no longer
  fetches `/active-speaker/environment`, `/safe-playback`, `/staged-config`,
  `/stage-config`, `/check-path-safety`, `/load-startup-config`,
  `/rollback-startup-config`, `/driver-measurement`, `/startup-load`. The routes
  and their payload builders are all still there
  (`_GET_JSON_ROUTES` entries at `sound_setup.py:4685-4711`; POST handlers at
  `5098`, `5107`, `5116`, `4944`, `4982`; builders at `1635-1655`, `1665-1711`,
  `1729-1778`, `1817-1873`, `2157-2321`, `4072-4109`). No other process calls
  port 8784 — only nginx does. `/active-speaker/calibration-level` and
  `/bringup-preflight` likewise have zero callers anywhere in the repo.
  -> Delete the routes and builders the retirement test already names; keep
  `/active-speaker/stop` (the nginx `/sound/` compatibility ingress comment at
  `deploy/nginx-jasper.conf:185-186` justifies that one, and `main.js` calls
  it). Verify against `tests/test_sound_setup.py`'s follower-403 matrix first.
  (effort M)

- **[M] HYG-5 — 29 hand-rolled `fetch` + parse sites reimplement
  `shared/js/http.js`.** `main.js` imports `jsonHeaders` but not `getJSON` /
  `postJSON` (`main.js:21`), then repeats
  `await fetch(...) -> await resp.json() -> if (!resp.ok) throw new Error(payload.error || '...')`
  29 times. `postJSON` (`shared/js/http.js:174-199`) already does exactly this
  *and* carries the parsed body on `err.body` (so the 409-conflict readers like
  `main.js:7055-7064` still work), *and* handles the control-token 403 retry and
  stale-session reload that this page currently has no answer for. -> Replace
  the 29 sites; net deletion. (effort M)

- **[L] HYG-6 — Per-page CSS re-implements primitives other pages also
  re-implement.** `.btn-row` is defined in `sound.css:46`,
  `system-status/system.css:146` and `chat/chat.css:44`; `.status-line` in
  `sound.css:277` and `tools/tools.css:359`; `details.advanced` styling in
  `sound.css:296`, `transit/transit.css:144` and three others. None are in
  `app.css`. `.setting-row`, `.list-card`, `.section-header`, `.status-pill`,
  `.text-button`, `.empty-card` and `.btn--compact` exist *only* in
  `sound.css` despite being generic. -> Promote `.btn-row`, `.status-line` and
  `details.advanced` to `app.css` (they have >=2 users today, which is the bar);
  leave the sound-only ones where they are until a second user appears. This is
  a cross-cluster fix, not a sound-only one. (effort S)

- **[L] HYG-7 — Off-ladder type in `sound.css`.** `font-size: 10px` at
  `sound.css:261`, `463`, `515`, `874`, `909` (plus `9px` inside the SVG graph
  at `:35`, which is arguably exempt as SVG chrome). design-language §3 fixes
  the ladder at 11/12/13/14/16px, and
  `tests/test_web_design_system.py:397` only enforces it on the landing page.
  Noted, not pressed: design-language §1 explicitly scopes itself to the
  measurement flow and forbids opening compliance work elsewhere on its
  authority. Fold into whichever PR already touches those rules. (effort S)

- **[L] HYG-8 — A documented contract nothing consumes.**
  `CarrierCannotHostEq`'s docstring (`jasper/sound/graph_carrier.py:57-58`)
  states "`reason_code` is stable (the UI branches on it)". `grep -rn
  reason_code deploy/assets/` returns nothing — no page branches on it. Per
  AGENTS.md ("when you can't verify a comment against the code, delete it"),
  either delete the claim or make EQ-1 the thing that branches on it. (effort S)

Good hygiene worth recording (so a later pass doesn't "fix" it):
no inline `<script>` or `<style>` in the Python module — the only `<script>`
tags are `src=` references to the static module (`sound_setup.py:1521, 1625`);
`escapeHtml` comes from `shared/js/escape.js`; `jtsConfirm` from
`shared/js/dialog.js` and is used for both destructive paths; the biquad math
and the active-speaker vocabulary are already extracted to sibling modules;
`json_island` is used correctly for the one piece of server-known data the page
needs (`sound_setup.py:1455-1489`). The render functions in Python are small
(45 and 18 lines of HTML) — the server-renders-shell / JS-renders-body split is
the right call for this page and needs no change.

---

## Cluster summary

**Ranked by need:**
1. **`/sound/setup/`** — RESTRUCTURE. It is three unrelated jobs stacked worst-
   first: the commissioning wizard that the landing page's output-locked banner
   sends people to is the *last* thing on the page, collapsed behind an
   unlabelled disclosure, reached after a seven-request serial boot that paints
   nothing. The measurement wizard's `#confirm-safety-limits` deep link is
   broken by that same disclosure (SND-1) — the exact defect class a dedicated
   test file already guards two layers of.
2. **`/eq/`** — TIDY. Genuinely well-built: one purpose, one primary action per
   state, graph above the controls that move it. Its real problems are that it
   renders a fully functional editor on speakers whose DSP graph will refuse
   every write (EQ-1), and that an unsaved draft outlives the page (EQ-2).
3. **`/sound/`** — KEEP. A correct 302.

**Cross-page patterns in this cluster:**
- **One artefact, two applications.** The `pageMode` fork runs through every
  layer — 5,544-line Python module, 7,649-line JS module, 1,012-line stylesheet
  — and in each one the EQ half and the setup half share almost nothing. A
  single split along that seam fixes the module-size, the payload-size and the
  "what is this page for" problems at once.
- **Feedback goes to the bottom of the page.** Both pages funnel every message
  into one 12px line below the fold (`sound_setup.py:1603`, `:1619`). Harmless
  on `/eq/`; a real failure on `/sound/setup/` (SND-4).
- **State the server knows, the page discovers by failing.** EQ-blocked carriers
  (EQ-1) and the whole retired-route set (HYG-4) are both cases of the client
  and server having drifted about who knows what. `/state` is the natural place
  to close the first.
- **Disclosure stacking.** `<details>` inside `<details>` inside `<details>`,
  with the outermost one added last and not accounted for by the deep-link
  logic or its tests (SND-1, SND-7).

**Estimated PR breakdown** (single-concern, < 400 changed lines each):

| # | PR | Findings | Tier |
|---|---|---|---|
| 1 | Fix the safety-limits deep link: open the outer disclosure; extend `test_sound_profile_confirm_deeplink.py` to pin it | SND-1 | Sonnet |
| 2 | `/sound/setup/` boot: render after `/output-topology`, parallelise the four independent GETs, add a loading line | SND-3 | Sonnet |
| 3 | Sticky status line on the sound pages | SND-4 | Sonnet |
| 4 | Reorder `renderSetup`: commissioning first, HAT + volume shaping under one Advanced; delete the outer Speaker-setup `<details>` (supersedes PR 1's workaround) | SND-2, SND-7 | **Opus** |
| 5 | `/eq/`: surface carrier-blocked as page state — add the flag to `_state_payload`, render an info-card instead of the editor | EQ-1 | **Opus** |
| 6 | `/eq/`: restore the persisted live source on `pagehide`; drop the `is-live` red dot; fix the tablist role | EQ-2, EQ-3, EQ-5 | Sonnet |
| 7 | Move Match loudness beside the other global sound settings; tidy the volume-floor control row | EQ-4, SND-6 | Sonnet |
| 8 | Completion card after "Validate and apply", linking to `/sound/crossover/` and `/sound/room/` | SND-5 | Sonnet |
| 9 | Follower setup page: demote the delegation banner, keep local setup as the page | SND-8 | Sonnet |
| 10 | Copy pass on the output-topology strings + delete the dead `visibility` branch | SND-9, SND-10 | Sonnet |
| 11 | Delete the retired active-speaker routes and their builders (~450 lines, net deletion) | HYG-4 | **Opus** (verify no caller) |
| 12 | Replace the 29 hand-rolled fetches with `getJSON`/`postJSON` | HYG-5 | Sonnet |
| 13 | Move `apply_measured_crossover_geometry` into `jasper/active_speaker/crossover_declaration.py` | HYG-3 | Sonnet |
| 14a-d | Split `main.js`, one seam per PR: (a) `eq-graph.js` + `eq-profile.js`; (b) `sound-settings.js`; (c) `driver-research.js`; (d) `output-topology.js` + `commissioning.js`, then `pageMode`-conditional `import()` | HYG-1 | **Opus** (a, d) / Sonnet (b, c) |
| 15 | Split `sound_setup.py` into `sound_setup.py` + `active_speaker_setup.py`; table-drive the POST ladder | HYG-2 | **Opus** |
| 16 | Cross-cluster: promote `.btn-row`, `.status-line`, `details.advanced` to `app.css` | HYG-6 | Sonnet |

PRs 1-3 are the "stop the bleeding" set and are independent of everything else.
PR 4 supersedes PR 1's fix, so land 1 first only if 4 is not imminent. PRs 11
and 15 should precede 14a-d — deleting dead routes first makes the JS split
smaller.
