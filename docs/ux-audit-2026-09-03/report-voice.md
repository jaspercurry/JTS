# UX / IA audit — VOICE cluster

Routes audited: `/voice/`, `/wake/`, `/wake-corpus/`, `/chat/`, `/tools/`.
All five are linked from `deploy/index.html`; four sit in the **Assistant**
section (`deploy/index.html:677-712`), `/wake-corpus/` sits in **System** as
"Developer tools · Wake corpus" (`deploy/index.html:802`).

---

## /voice/ — "Configure which paid realtime-voice backend the speaker uses, and cap what it can spend"

Files: `jasper/web/voice_setup.py` (1815), `deploy/assets/voice/voice.css` (213),
`deploy/assets/voice/js/main.js` (92).

Primary user tasks:
1. Paste an API key for a provider and make that provider active. (first run)
2. Change the active provider / model / TTS voice. (occasional)
3. Check "is voice about to stop working because of the spend cap?" (recurring)
4. Correct the per-model $/token table so the cap is accurate. (rare)

### Screen inventory (top to bottom, with 3 providers in `jasper/voice/catalog.py:157`)

1. `canonical_header("Voice provider")` + bonded-follower pair banner
   (`voice_setup.py:1037-1038`).
2. Flash banner — the page's **only** feedback channel (`voice_setup.py:1040`).
3. One-line page hint (`voice_setup.py:1041`).
4. **"1. Enter API keys"** — hint + 3 × `_provider_key_card_html`
   (`voice_setup.py:858-920`): provider name, vendor eyebrow, status badge,
   cost hint + "Get key ↗", key `<input>` labelled with the raw env var name,
   masked "Saved:" line, optional `/etc/jasper/jasper.env` provenance line,
   and a red **Clear key** button.
5. **"2. Select provider"** — `<form id="save-form">` holding only CSRF + the
   3 radio rows (`voice_setup.py:264-309`). Rows for un-keyed providers are
   disabled with status text `no JASPER_GROK_API_KEY yet — add a key first`.
6. **"3. Select model and voice"** — hint + 3 × `_provider_model_card_html`
   (`voice_setup.py:921-983`): **the same** provider name, vendor eyebrow and
   status badge again, then Model `<select>`, a discovery-status hint, a
   **Refresh available models** button, TTS voice `<select>`, and for OpenAI a
   Reasoning-effort `<select>`.
7. Save bar: **[Save and restart voice]** (primary) **[Save and Test]**
   (default) — `voice_setup.py:1070-1073`.
8. **"Voice spend cap"** — 6-row deflist (Status badge / Rolling 24h /
   Cap comparison / Remaining / Month to date / Turns today), 1-2 hint lines,
   0-3 error notes, two number fields, **[Save spend cap]**
   (`voice_setup.py:427-504`).
9. **"Advanced pricing"** — 3 × `<details>`, one per provider, each listing
   every catalog ∪ discovered model × up to 6 rate inputs, each with its own
   **[Save <provider> rates]** (`voice_setup.py:673-737`).
10. **"Refresh pricing rates"** — 2 more `<details>`: a read-only prompt
    textarea + **[Copy prompt]**, and a paste textarea +
    **[Validate & import rates]** (`voice_setup.py:783-826`).

States: flash banner only (ok/info/danger, `_common.py:358`). No inline field
errors, no loading/pending state on any of the ~14 buttons, no empty state.

### Verdict: **RESTRUCTURE**

The page is organised by *implementation step* (keys, then selection, then
models, then rates) instead of by *the thing the user is thinking about*
(a provider). Each of the three providers therefore appears three times, in
three different stacks, ~1.5 screens apart, each time re-rendering the same
name + vendor + status badge. To turn on Grok the user must scroll to card 3
of stack 1, then to row 3 of stack 2, then optionally to card 3 of stack 3 —
holding "which one was I doing" across two screens each way (Miller). The
`1. / 2. / 3.` numbering makes it read as a wizard, but sections 4-6 are
unnumbered and the page is a settings screen you return to.

### Findings

- **[H] V1 — one purpose per screen / chunking / proximity — the page is
  sliced by step, so each provider is rendered three times.**
  `_provider_key_card_html` (`voice_setup.py:858`), `_active_radio_html`
  (`voice_setup.py:264`) and `_provider_model_card_html` (`voice_setup.py:921`)
  each iterate `PROVIDERS` independently in `_index_html`
  (`voice_setup.py:996-1024`), and cards 1 and 3 both call
  `_provider_status_badge_html` for the same provider
  (`voice_setup.py:864,927`). The stated reason for the split is the comment at
  `voice_setup.py:1025-1035` ("HTML forbids nested forms"), but that constraint
  does **not** require it: the key inputs already sit outside `save-form` and
  associate by `form="save-form"` (`voice_setup.py:882`, `:543`, `:594`), so a
  single per-provider card can hold the radio, key field, model, voice and
  extras *and* still have the standalone `clear-credentials` /
  `refresh-models` forms as siblings inside it — no nesting. → Collapse
  sections 1-3 into **one** "Providers" section of three cards, each card:
  header + badge, "Use this provider" radio, API key, Model, Voice, extras,
  then the two standalone forms; drop the `1./2./3.` numbering. Net effect is
  a shorter page and one scan per provider. (effort M)

- **[M] V2 — essentials first (serial position) — the page never says what is
  live, and the health readout is four screens down.** The active provider is
  discoverable only by which radio is checked (`voice_setup.py:281`), and the
  spend-cap status badge — the one thing that answers "why did the speaker
  stop answering?", including the `blocked` state that silences voice
  (`voice_daemon.py:3815-3818`) — is rendered at `voice_setup.py:1075`, after
  three provider stacks and the save bar. → Put a one-line status strip
  directly under the header: active provider · model · cap status/remaining,
  reusing the existing `_read_spend_cap_status` dict (`voice_setup.py:349`).
  Leave the editable cap fields where they are. (effort S)

- **[M] V3 — error recovery / preserve work — a rejected save silently
  discards everything the user typed.** `_apply_save` returns `current`
  unchanged on any error (`voice_setup.py:1140,1152,1156,1161`) and the handler
  answers with `send_see_other(self, "./", flash=...)`
  (`voice_setup.py:1425`), so the page re-renders from disk: the pasted API
  key, the changed model and the changed voice are all gone. Keys are long
  clipboard pastes and the most likely rejection (`_validate_key`,
  `voice_setup.py:1098`) is exactly a paste problem (stray whitespace). →
  Either validate the key client-side in `voice/js/main.js` before submit
  (it already watches `[data-provider-key]` input at `main.js:88`), or echo the
  rejected form back into the re-render instead of redirecting. (effort M)

- **[M] V4 — feedback within ~400 ms — no submit acknowledgement anywhere on
  the page, including on its slowest action.** "Save and Test" runs a live
  provider TTS round-trip plus a daemon restart (`_handle_save_test`,
  `voice_setup.py:1443-1509`) and gets no busy state; `voice/js/main.js`
  wires only confirm, clipboard and radio-enable (documented at
  `main.js:6-27`). `/wake/` solves exactly this for its own save button and
  records why — "without this the action feels like a no-op"
  (`deploy/assets/wake/js/main.js:17-20,631-639`). → Port that submit handler
  to `voice/js/main.js`, with distinct labels for the two buttons
  ("Saving…" / "Testing…"). (effort S)

- **[M] V5 — progressive disclosure / "name what people control" — env-var
  names are used as user-facing labels.** The key field's `<label>` is
  `API key (JASPER_GEMINI_API_KEY)` (`voice_setup.py:879`), the disabled radio
  status is `no {p.key_env} yet — add a key first` (`voice_setup.py:294`), and
  the refresh hint is `Add {provider.key_env} first.` (`voice_setup.py:955`).
  The user pastes a key; the variable name is storage detail. → Label the field
  "API key"; make the radio status "Add a key first"; keep the genuinely
  load-bearing provenance hint at `voice_setup.py:872-877` (it explains why a
  key shows configured that the wizard doesn't own). Net word count drops.
  (effort S)

- **[M] V6 — chunking / disclosure — pricing occupies two top-level sections
  and five disclosures for a feature the page itself calls "used only for
  spend estimates".** "Advanced pricing" (`voice_setup.py:1077-1081`) renders
  one `<details>` per provider, each with every model × up to 6 numeric inputs
  and its own save button, and is followed by a *separate* top-level "Refresh
  pricing rates" section (`voice_setup.py:1083`) with two more disclosures —
  and in the wrong order, since you research prices before editing them. →
  One "Pricing" section, one `<details>`, containing the research prompt,
  the paste-import box, then the per-provider editors. (effort S)

- **[L] V7 — an action keeps its name — "Save and restart voice" beside
  "Save and Test"** (`voice_setup.py:1071-1072`): two casings, two different
  descriptions of the same save. → "Save" and "Save and test"; put the restart
  fact in the hint, where it already is elsewhere. (effort S)

### Hygiene

- **[M] V-H1 — `_index_html` is a 110-line f-string assembling six other
  f-string builders** (`voice_setup.py:985-1090`); the render half of the
  module runs `voice_setup.py:264-1096` (~830 lines of HTML in Python). Server
  rendering is the right call here — the forms are plain POSTs and the page has
  no live data — but the page is a static shape over a `PROVIDERS` loop plus a
  status dict, so the honest split is: one `_provider_card_html` (after V1) and
  a single template constant, not seven interleaved builders. (effort M)

- **[L] V-H2 — `voice.css` invents a second disclosure component.**
  `.pricing-disclosure` (`voice.css:143-190`) is a full `<details>` skin;
  `.disclosure` exists separately in `airplay.css:73`, `google.css`,
  `wifi.css`; `/wake/` references `.disclosure` and gets nothing (see W1);
  `/tools/` has a third (`tools.css`, `.tool-row__details`). `app.css` ships no
  `<details>` primitive. → Promote one to `app.css`, delete four. (effort M,
  cross-page — see cluster summary.)

- **[L] V-H3 — five off-ladder font sizes** in `voice.css` (`0.85rem`,
  `0.8rem`, `1.05rem`, `0.9rem`) against the 11/12/13/14/16 ladder
  (`docs/design-language.md` §3). → Snap to the ladder. (effort S)

---

## /wake/ — "Set up the microphone, echo cancellation and wake phrase"

Files: `jasper/web/wake_setup.py` (1134), `deploy/assets/wake/wake.css` (418),
`deploy/assets/wake/js/main.js` (645).

Primary user tasks: pick the wake phrase and sensitivity; see which mic is
detected and whether echo cancellation is aligned; run a firmware update or
re-commission when the page asks for it.

### Screen inventory

1. `canonical_header("Wake word")` + pair banner (`wake_setup.py:544-545`).
2. **Microphone** (`_mic_status_card_html`, `wake_setup.py:335-399`) — a 6-cell
   read-only grid (Detected mic / Firmware / Mode / Session audio / Wake legs /
   Wake phrase), a hidden warning slot, then **"Use JTS as a computer
   microphone"** toggle + notice + a **"Computer microphone source"** `<select>`.
3. **Echo cancellation** (`_echo_card_html`, `wake_setup.py:271-300`) — status
   title/detail, an "Align echo cancellation" button, a hidden firmware-update
   card with its own button, then 4 radio profile rows, then a warning slot.
4. **Wake word** — help paragraph, the Sensitivity panel (range + value +
   its own **Save**, `wake_setup.py:401-418`), the model-picker `<form>` with
   one `.wake-row` per registry entry + **[Save and restart voice]**, then
   the "Wake recordings and privacy" `<details>`.
5. **Advanced wake fusion** `<details>` — 4 layer toggles with MB/CPU costs.

States: everything above starts as literal `checking…` (15 occurrences) and is
hydrated by a 3 s poll (`wake/js/main.js:337`); optimistic toggle flips with a
`dirty` guard; confirms via `jtsConfirm`.

### Verdict: **TIDY**

The IA is basically right — hardware truth, then processing, then the wake
phrase, then expert controls — and the backend-driven model is the correct
call. Three local problems.

### Findings

- **[H] W1 — consistency / Prägnanz — the page's two disclosures are
  completely unstyled.** `wake_setup.py:319` and `:502` emit
  `<details class="disclosure">`, but `wake.css` defines only
  `.advanced-fusion-card .disclosure-body` (`wake.css:251`) and `app.css`
  defines no `.disclosure` or `summary` rule at all. The class is styled in
  `airplay.css:73`, `google.css` and `wifi.css` — none of which `/wake/` loads
  (`canonical_page(page_css_href="/assets/wake/wake.css")`,
  `wake_setup.py:578`). So "Advanced wake fusion" and "Wake recordings and
  privacy" render as a bare native triangle and unstyled summary text, inside
  a page whose other sections are cards. → Add the `.disclosure` primitive to
  `app.css` and delete the per-page copies (same fix as V-H2). (effort S)

- **[M] W2 — one purpose per screen — the USB-microphone export feature lives
  inside the mic *status* card.** `wake_setup.py:379-398` puts a toggle
  ("Use JTS as a computer microphone"), a notice, and a "Computer microphone
  source" `<select>` inside the read-only Microphone card, and its own copy
  says it "never changes what JTS uses for voice or wake" — i.e. it is not part
  of this page's job. The source `<select>` is also always rendered, whether or
  not the export is on. → Give it its own section below Echo cancellation, or
  reveal it only when the toggle is on. (effort S)

- **[M] W3 — Jakob / naming — three names for this page.** Landing row title
  "Voice assistant", status "Wake word · microphone" (`deploy/index.html:688-694`);
  page `<title>`/header "Wake word" (`wake_setup.py:544,577`); actual content
  is mic + AEC + wake. "Voice assistant" also collides with `/voice/`, which
  the same section calls "Voice". `deploy/index.html` is the protected
  reference, so fix on the sub-page side: retitle the page to the landing row's
  own words. (effort S)

- **[L] W4 — proximity / naming — two differently-named saves in one section.**
  The sensitivity slider has its own ghost **Save** posting via fetch
  (`wake_setup.py:414`, `wake/js/main.js`), while the model list has
  **Save and restart voice** as a form POST (`wake_setup.py:568`), and the
  section's help text says "Saving restarts voice" for both. The privacy
  `<details>` then sits *after* the save button, between it and the next
  section. → Name both "Save", move the privacy disclosure to the page
  bottom beside Advanced wake fusion. (effort S)

### Hygiene

- **[M] W-H1 — 645-line single `main.js` covering six concerns** — mic status,
  USB export, firmware update, commissioning, echo profiles, fusion layers,
  sensitivity (`wake/js/main.js:88-598`), with six module-level `*Busy` flags
  (`:39-42`). `/tools/` and `/chat/` in this same cluster split into 3-5
  cohesive modules. → Split by card (`mic.js`, `echo.js`, `fusion.js`,
  `sensitivity.js`) over one shared poll. (effort M)

- **[M] W-H2 — 24 off-ladder font sizes in `wake.css`** — `0.78 / 0.82 / 0.83 /
  0.84 / 0.86 / 0.88 / 0.9 / 0.93 / 0.95 / 1 rem` (12.5-16px) across
  `wake.css:11-402`. Ten near-identical sizes in a 3.5px band is not a
  hierarchy; `docs/design-language.md` §3 says hierarchy comes from case,
  weight and colour. → Snap to 11/12/13/14/16. (effort S, mechanical)

---

## /wake-corpus/ — "Record wake-word training clips" (developer tool)

Files: `jasper/web/wake_corpus_setup.py` (1435), `wake-corpus.css` (299),
`js/main.js` (835) + `controls.js` (57) + `labels.js` (46).

Primary user task: enter corpus test mode, record N clips per
condition × distance cell, review and delete bad clips, leave test mode.

### Screen inventory

1. `canonical_header("Wake-word corpus")`.
2. Status card — Mode / jasper-voice / Extra corpus outputs / Session, plus an
   "Exit corpus test mode" button (`wake_corpus_setup.py:1110-1128`).
3. Session card — name field, 7 capture-option toggles (3 conditionally hidden
   via `controls.js:32-34`), a capture-plan preview, a storage-path note, and
   **[Enter corpus test mode & begin session]** (`:1130-1152`).
4. **Sessions** `<details>` — load/delete previous recordings (`:1154-1166`).
5. Record card (hidden until a session loads) — condition radios, distance
   radios, mic-level meter, **● RECORD**, elapsed timer, error slot (`:1168-1194`).
6. Per-cell counts matrix (hidden until session) (`:1196-1202`).
7. Recorded clips table with per-clip playback + delete (`:1204-1215`).

### Verdict: **TIDY** (IA is good; presentation is a separate design system)

For a dev tool this is well sequenced: state at the top, the destructive
mode-entry action gated behind one clearly-labelled button, recording controls
revealed only once a session exists, review at the bottom. The capture-option
hiding in `controls.js` is genuine progressive disclosure. Keep it as its own
route — it stops `jasper-voice` and takes over the UDP mic ports
(`wake_corpus_setup.py` module docstring), which is nothing `/wake/` should be
able to do by accident.

### Findings

- **[L] C1 — empty state / feedback — the deepest cards are `display:none`
  rather than an invitation.** `record-card`, `counts-card` and `clips-card`
  (`wake_corpus_setup.py:1168,1196,1204`) are simply absent before a session
  exists, so a first-time operator sees a status card and a form with no
  indication of what the page will look like after. `docs/design-language.md`
  §11: "an empty screen is a direction, not a mood". → One line under the
  session card naming what appears next. (effort S)

### Hygiene

- **[M] C-H1 — the page loads `app.css` and then overrides it with a parallel
  design system.** `wake-corpus.css` re-implements `.card` (`:17`), a bare
  `button {}` element selector (`:42`), `button.primary` (`:50`),
  `button.danger` (`:56`) and `.pill` (`:32`) — duplicates of `.info-card`,
  `.btn`, `.btn--primary`, `.btn--danger` and `.badge` from `app.css` — on ~28
  hardcoded hex colours (`#fff`, `#ddd`, `#1f7a1f`, `#a31f1f`, `#888`, `#ccc`,
  …) instead of the token set, plus 14 off-ladder `em` sizes. The bare
  `button {}` rule also lands on every canonical button the page renders,
  including the shared `jtsConfirm` dialog. → Swap to app.css primitives and
  tokens; keep only what is genuinely corpus-specific (mic meter, matrix,
  clip row). (effort M)

- **[L] C-H2 — ~20 inline `style="…"` attributes in the body template**
  (`wake_corpus_setup.py:1113,1148,1161,1163,1165,1177,1183,1197,1201,1206,1207`
  — `margin-top:0`, `color:#888`, `font-size:0.86em`, `border-bottom:2px solid
  #333`, `margin-left:auto`, …). Static CSS is the house rule. → Move to
  classes in `wake-corpus.css`. (effort S)

---

## /chat/ — "Read recent voice turns; control whether they're saved"

Files: `jasper/web/chat_setup.py` (316), `deploy/assets/chat/chat.css` (190),
`js/main.js` (148) + `views.js` (346) + `components.js` (68) + `api.js` (11).

Primary user tasks: read what was said recently; check/flip whether capture is
on; clear history.

### Screen inventory (built by `views.js:19-113`)

1. `header({title:"Chat"})` — built in **JavaScript** (`components.js:49-59`),
   not by `canonical_header`.
2. Live pill — "Live · N turns" / "Capture off." / "Disconnected. Retrying…".
3. **Capture** card — toggle + status line, then a red **Clear history** button
   + "N saved turns."
4. **Filter** card — a `<date>` "Since" field, **[Apply]** primary +
   **[Clear filter]** ghost, a status line, and a hidden "Show error" button.
5. **Recent turns** card — one `.chat-turn-card` per turn (timestamp, provider,
   optional "Research" badge, paired User/Assistant transcripts, tool note).

States: `aria-busy` boot note; loading / error / three distinct empty states
(`views.js:212-227`); memoised re-render; 10 s poll.

### Verdict: **TIDY**

Architecturally the best page in the cluster — Python serves `data.json` and a
mount point, JS renders with text nodes only, modules are small and cohesive.
The IA is one step out of order.

### Findings

- **[M] CH1 — serial position — the thing you came for is last.** Card order is
  Capture (settings) → Filter → Recent turns (`views.js:104-112`). The page
  exists to read turns; capture on/off and Clear history are settings you touch
  once. On a phone the first turn is below two full cards. → Order: live pill →
  Recent turns → Filter → Capture (or move capture/clear behind a disclosure or
  into the header's `right_html` slot). (effort S)

- **[L] CH2 — feedback / navigation — no back button until JS has run.** The
  server body is `<div id="app">Loading conversation history…</div>`
  (`chat_setup.py:38-42`) and the header is constructed client-side
  (`components.js:49`), so before hydration — or if the module fails — there is
  no way back to `/`. Every other page in the cluster server-renders
  `canonical_header`. → Emit `canonical_header("Chat")` in `_render_page` and
  delete `components.js:header`. (effort S)

### Hygiene

- **[M] CH-H1 — `components.js` duplicates two shared things.** `header()`
  (`components.js:49-59`) is a JS re-implementation of `canonical_header`
  (`_common.py:300`), and `badge` / `actionButton` / `titledCard` are
  near-copies of the same helpers in
  `deploy/assets/system-status/js/components.js`. → After CH2, delete
  `header()`; promote the rest to `deploy/assets/shared/js/components.js`.
  (effort M)

- **[L] CH-H2 — dead export.** `table()` (`components.js:29-46`) is imported by
  nothing (`views.js:12` imports only `actionButton, badge, header, livePill,
  titledCard`), and the `.table` / `.table-wrap` classes it emits are defined
  in neither `chat.css` nor `app.css`. → Delete. (effort S)

- **[L] CH-H3 — two page-shell primitives re-invented.** `.app-main`
  (`chat.css:6`) duplicates `.page` (`app.css:168`) but at `56rem` instead of
  `48rem`, making `/chat/` the only wider page in the cluster; `.btn-row`
  (`chat.css:44`) duplicates `.form-actions` (`app.css:561`). → Use the
  primitives. (effort S)

---

## /tools/ — "Turn voice capability packs on and off"

Files: `jasper/web/tools_setup.py` (879), `deploy/assets/tools/tools.css` (385),
`js/main.js` (123) + `render.js` (365) + `detail.js` (198) + `actions.js` (86).

Primary user tasks: find a pack, turn it on/off, apply; open a pack that needs
setup and get sent to its wizard.

### Screen inventory

**Index** (`tools_setup.py:163-193`): header with a **Guide** button in the
right slot → search field → pack cards grouped by category (title, summary,
tool count, status badge, toggle or "Needs setup"/"Unavailable") → an Apply bar
that appears only when changes are staged → a status line.
**Pack detail** (`:218-236` + `render.js:313-348`): pack header with badge,
"Set up"/"Configure" link, toggle; a Category deflist; a **"Tool authoring
guide"** link; then one row per tool, each with a `<details>` exposing the
model-facing prompt (editable) and the raw JSON input schema.
**Guide** (`:239-375`): ~140 lines of contributor documentation.

### Verdict: **TIDY**

The interaction model is the strongest in the cluster: toggles stage, one
Apply restarts once, with an honest 30 s convergence loop and real status copy
(`actions.js:52-83`), and the reasoning is documented at
`tools_setup.py:21-30`. Two things undercut it.

### Findings

- **[M] T1 — Von Restorff — the page's one primary action is its least
  emphatic button.** Apply renders as `class="btn"` with no variant
  (`tools_setup.py:184`, and again at `:228`); bare `.btn` in `app.css:249-263`
  sets no background, border or colour — it is uppercase text. Meanwhile the
  *Guide* link, a documentation link, gets `.btn.btn--ghost` in the header's
  action slot (`tools_setup.py:170`). → `btn--primary` on Apply. (effort S)

- **[M] T2 — one purpose per screen / progressive disclosure — contributor
  documentation is given the header action slot on an end-user page.**
  `/tools/guide/` is authoring guidance about `ToolDefinition`, `ToolExecutor`,
  `llm_description`, `untrusted_output=True` and `tests/voice_eval/`
  (`tools_setup.py:239-375`) — it is `docs/`, not UI, and
  `docs/tool-platform-plan.md:592` already tracks it. It is linked twice: the
  header button (`tools_setup.py:170`) and again inside every pack-detail card
  (`render.js:345`). → Keep at most one link, at the bottom of the guide's own
  natural home; free the header slot. (effort S)

- **[L] T3 — disclosure — the model prompt and JSON input schema sit on the
  end-user pack page.** `render.js:295-306` renders an editable `<pre>`/textarea
  of the model-facing prompt plus the raw schema per tool. They are inside a
  `<details>`, which is the right containment, but the summary should say so
  ("Model prompt and schema") rather than reading as ordinary tool detail.
  (effort S)

### Hygiene

- **[M] T-H1 — `render.js` builds markup by string concatenation into
  `innerHTML`** (`render.js:80-348`, `main.js:52`), guarded by `escapeHtml` at
  every interpolation. It is careful and reviewed, but `/chat/` in the same
  cluster builds the same kind of card with `dom.js` text nodes and *cannot*
  inject. Two idioms, one cluster. → Converge on `dom.js`. (effort L; a
  direction, not a required fix.)

- **[L] T-H2 — `_guide_html` is 140 lines of prose in a Python f-string**
  (`tools_setup.py:239-375`), with `{{"error": …}}` brace-escaping in the body.
  If T2 keeps it, it should be a static asset. (effort S)

- **[L] T-H3 — two off-ladder sizes:** `font-size: 26px` (`tools.css:199`) and
  `24px` (`tools.css:383`) against the 11-16px ladder. (effort S)

---

## Cluster summary

**Is the five-page split right?** Broadly yes, by domain: `/voice/` is the
brain, `/wake/` the ears, `/tools/` the hands, `/chat/` the log, and
`/wake-corpus/` a destructive dev rig that stops `jasper-voice` and seizes the
mic UDP ports — it must stay its own gated route, and its overlap with `/wake/`
is only the "Advanced wake fusion" toggles, which are already disclosed as
"expert controls for corpus tests". Merging `/chat/` or `/tools/` into
`/voice/` would make the cluster's worst page worse. The real IA defect is not
*between* the pages but *inside* `/voice/`, plus the naming collision the
landing page exposes: "Voice" (`/voice/`) next to "Voice assistant" (`/wake/`,
whose own title is "Wake word") in one section (`deploy/index.html:680,688`).
`deploy/index.html` is protected, so the fix belongs on the sub-page side (W3).

**Ranked, worst first**
1. **`/voice/` — RESTRUCTURE.** Sliced by implementation step, so all three
   providers are rendered three times each; the live state and spend-cap health
   are four screens below the fold; a rejected save throws away the pasted key.
2. **`/wake/` — TIDY, one visible defect.** Two `<details>` sections render
   completely unstyled because `.disclosure` is defined in three other pages'
   stylesheets and not this one; USB mic export is buried in the status card;
   `wake.css` uses ten font sizes where the system has five.
3. **`/tools/` — TIDY.** Best interaction model in the cluster, undermined by
   an unstyled primary Apply button and a contributor doc holding the header
   action slot.
4. **`/wake-corpus/` — TIDY.** Good flow for a dev tool; ships a parallel
   hex-coded design system on top of the `app.css` it loads.
5. **`/chat/` — TIDY.** Cleanest code in the cluster; only needs its cards
   reordered so the turns come first, plus a server-rendered header.

**Cross-page patterns a shared fix would solve**
- **No `<details>` primitive in `app.css`.** Five separate implementations
  (`voice.css:143`, `airplay.css:73`, `google.css`, `wifi.css`,
  `tools.css`'s `.tool-row__details`) — and `/wake/` references the class
  without loading any of them, so it renders bare (W1). One primitive in
  `app.css` fixes a live bug and deletes four copies.
- **Three rendering idioms for one kind of page**: Python f-strings
  (`/voice/`, `/wake/`, `/wake-corpus/`, `/tools/guide/`), `innerHTML`
  concatenation (`/tools/`), `dom.js` text nodes (`/chat/`).
- **Two implementations of the page header**: `canonical_header`
  (`_common.py:300`) and `header()` in both `chat/js/components.js:49` and
  `system-status/js/components.js` — and the JS one costs `/chat/` its back
  button before hydration.
- **Submit feedback is per-page folklore.** `/wake/` disables its save button
  and documents why (`wake/js/main.js:17-20`); `/tools/` has a full status
  line and convergence poll; `/voice/` — with the slowest action in the
  cluster — has nothing.
- **Font-size drift is confined to the f-string pages**: `wake.css` (24
  off-ladder values), `wake-corpus.css` (14), `voice.css` (5); `tools.css` and
  `chat.css` are on the ladder but for two title sizes.

**Proposed PRs** (each single-concern, < 400 changed lines)

| # | Scope | Files | Tier |
|---|---|---|---|
| 1 | `/voice/` V1: merge key card + radio + model card into one per-provider card; drop `1./2./3.` | `voice_setup.py`, `voice.css` | **Opus** |
| 2 | `/voice/` V2 + V6: status strip under the header; fold pricing into one section, research-prompt first | `voice_setup.py` | **Opus** |
| 3 | `/voice/` V3 + V4 + V7: submit busy states, client-side key validation, button naming | `voice/js/main.js`, `voice_setup.py` | Sonnet |
| 4 | `/voice/` V5: drop env-var names from labels and radio status | `voice_setup.py` | Sonnet |
| 5 | Shared: add a `.disclosure` primitive to `app.css`; delete the five per-page copies (fixes W1) | `app.css` + 5 page CSS files | Sonnet |
| 6 | `/wake/` W2 + W3 + W4: USB export into its own section, retitle, save naming, move privacy block | `wake_setup.py`, `wake.css` | **Opus** |
| 7 | `/wake/` W-H2 + `/voice/` V-H3 + `/tools/` T-H3: snap font sizes to the ladder | 3 CSS files | Sonnet |
| 8 | `/tools/` T1 + T2: `btn--primary` on Apply; remove the guide from the header slot and the pack card | `tools_setup.py`, `render.js` | Sonnet |
| 9 | `/chat/` CH1 + CH2 + CH-H2 + CH-H3: reorder cards, server-render the header, delete `table()`, use `.page`/`.form-actions` | `chat_setup.py`, `views.js`, `components.js`, `chat.css` | Sonnet |
| 10 | `/wake-corpus/` C-H1 + C-H2: app.css primitives and tokens, strip inline styles | `wake_corpus_setup.py`, `wake-corpus.css` | Sonnet |
| 11 | `/wake/` W-H1: split `wake/js/main.js` into per-card modules over one poll | `wake/js/*.js` | **Opus** |
| 12 | Shared: promote `badge`/`actionButton`/`titledCard` to `shared/js/components.js`; delete the chat + system-status copies | `shared/js/`, 2 pages | Sonnet |

Ordering note: PR 5 before 6, and PR 1 before 2/3/4 — they all touch
`_index_html`.
