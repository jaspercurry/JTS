# JTS web UI — cross-cutting audit (navigation/IA, shared primitives, interaction patterns, guards, doc question)

Scope: the system the ~25 sub-pages share. Per-page verdicts belong to the other
agents. Every finding cites `file:line`. `deploy/index.html` is protected — it is
read here as the reference, never proposed for change.

---

## 1. The site map (inferred from `deploy/nginx-jasper.conf` + `deploy/index.html`)

### 1.1 Landing page organisation (`deploy/index.html`)

Hero (`.hero`, index.html:495) — four `.control-section` blocks, all
conditionally `hidden` behind `data-requires` capability flags:
Stereo pair (`:497`, links `/rooms/`) · Source (`:508`) · Volume (`:546`, links
`/sound/setup/`) · Voice assistant (`:572`).

Then `<nav class="groups">` (index.html:592) — six labelled sections, 23 rows,
22 distinct destinations:

| Section | Rows (label -> route) |
|---|---|
| **Sources** `:593` | Playback sources -> `/sources/` · Spotify accounts -> `/spotify/` · Bluetooth devices -> `/bluetooth/` · AirPlay sync -> `/airplay/` |
| **Sound** `:631` | EQ -> `/eq/` · Sound setup -> `/sound/setup/` · Active speaker -> `/sound/crossover/` · Room correction -> `/sound/room/` · Bass -> `/sound/bass/` |
| **Assistant** `:677` | Voice -> `/voice/` · Voice assistant -> `/wake/` · Chat history -> `/chat/` · Tools -> `/tools/` |
| **Integrations** `:715` | Weather -> `/weather/` · Transit -> `/transit/` · Google -> `/google/` · Home Assistant -> `/ha/` |
| **Network** `:753` | Wi-Fi -> `/wifi/` · Speakers -> `/rooms/` |
| **System** `:775` | Status -> `/system/` · Speaker name -> `/speaker/` · **Software -> `/system/`** (same target as Status) · Developer tools -> `/wake-corpus/` |
| Footer `:813` | system pill -> `/system/` |

The ordering is sound (serial position): the things you touch daily are in the
hero, the settings groups descend roughly by frequency, System last. No IA
complaint about the landing page itself.

### 1.2 Full route inventory

Port 80 (`deploy/nginx-jasper.conf:31`-493) unless marked. HTTPS block starts at
`:509`.

```
/                       landing (static)                       [protected]
├─ Sources
│  ├─ /sources/         sources_setup.py           :134   linked
│  ├─ /spotify/         spotify_setup.py           :72    linked
│  ├─ /bluetooth/       bluetooth_setup.py         :118   linked
│  └─ /airplay/         airplay_setup.py           :106   linked
├─ Sound  ── daemon :8784 (sound_setup.py) ────────────────────────────
│  ├─ /eq/              sound_setup page_mode=eq   :167   linked
│  ├─ /sound/setup/     sound_setup page_mode=setup :175  linked
│  └─ /sound/           302 -> /sound/setup/       :157
├─ Sound  ── daemon :8770 (correction_setup.py, HTTP + HTTPS) ─────────
│  ├─ /sound/room/          correction_room_flow      :432  linked
│  ├─ /sound/crossover/     correction_crossover_flow :441  linked
│  ├─ /sound/bass/          correction_bass_flow      :459  linked
│  ├─ /sound/measurements/  correction_measurements   :450  TAB-ONLY
│  ├─ /balance/             balance_flow              :587  ORPHAN (HTTPS only)
│  ├─ /sync/                sync_flow                 :604  ORPHAN (HTTPS only)
│  └─ /correction/…         alias of ALL SIX above    :468  UNLINKED ALIAS
├─ Assistant
│  ├─ /voice/           voice_setup.py             :80    linked
│  ├─ /wake/            wake_setup.py              :208   linked
│  ├─ /chat/            chat_setup.py              :349   linked
│  └─ /tools/           tools_setup.py             :279   linked
│     ├─ /tools/pack/<id>/   tools_setup :199              in-page
│     └─ /tools/guide/       tools_setup :418              header slot
├─ Integrations: /weather/ :306 · /transit/ :291 · /google/ :91 · /ha/ :320
├─ Network: /wifi/ :247 · /rooms/ :265
├─ System
│  ├─ /system/          system_setup.py            :334   linked (x2 rows)
│  └─ /system/audio/    same module, view=audio    (system_setup.py:120)  tab-only
├─ /speaker/            speaker_setup.py           :193   linked
├─ /wake-corpus/        wake_corpus_setup.py       :223   linked
└─ API (not pages, consumed by the landing page + /system/):
   /volume :359 · /grouping :371 · /mic :384 · /source :395 · /debug :405
   /jts-root-ca.crt :483
```

**25 pages, 22 linked from the landing page, 2 orphans, 1 tab-only, plus a
6-page shadow namespace (`/correction/*`).**

### 1.3 IA findings

- **[H] IA-1 — Two orphan pages.** `/balance/` (`jasper/web/balance_flow.py:816`)
  and `/sync/` (`jasper/web/sync_flow.py:515`) are reachable only by typing the
  URL. Nothing links them: not `deploy/index.html`, not `/rooms/` — the page
  whose entire subject is the stereo pair (`deploy/assets/rooms/js/main.js`
  links only `/speaker/` at `:207` and peers' `/system/` at `:245`), not the
  `/sound/*` tab bar (`jasper/web/correction_hub.py:11-20` lists four sections,
  neither of these). Both are pair-only flows that the pair page never offers.
  -> Add them as rows on `/rooms/` when a bonded pair exists (the pair-banner
  already gates on that), or as two more `correction_hub.SECTIONS` entries.
  (effort S)

- **[H] IA-2 — Neither orphan has a back link, or any header at all.**
  `balance_flow.py:783` and `sync_flow.py:492` both open straight at
  `<main class="page">`; no `canonical_header()`, no `.app-header`, no
  `#icon-back`. On a phone opened from a pasted link there is no way home except
  the browser's back gesture, and after `Apply` the user is stranded on a page
  that says nothing about what to do next. Every other page in the product has
  the round back button. -> `canonical_header("Balance speakers",
  back_href="/rooms/")` / `("Measure sync", back_href="/rooms/")`. (effort S)

- **[M] IA-3 — `/sound/` is one prefix, two daemons, two navigation models.**
  `/sound/setup/` and `/eq/` are `sound_setup.py` on :8784; `/sound/room|crossover|
  bass|measurements/` are `correction_setup.py` on :8770 (nginx `:167`-`:187`
  vs `:432`-`:468`). The second group carries a shared tab bar
  (`correction_hub.section_tabs`); the first does not, and `/sound/` bare
  302s to `/sound/setup/` (`:157`), so the prefix has no hub page. Result: from
  `/sound/setup/` there is no path to `/sound/room/` except going Home, and the
  `/sound/*` tab bar silently excludes two of the five Sound rows the landing
  page lists together. -> Either give the four correction tabs a fifth "Setup"
  entry pointing at `/sound/setup/`, or stop implying a hierarchy and flatten
  `/eq/` + `/sound/setup/` out of the `/sound/` prefix. (effort M, judgement)

- **[M] IA-4 — `/eq/` and `/sound/setup/` are the same module behind two
  top-level namespaces.** `sound_setup._index_html(page_mode=…)`
  (`jasper/web/sound_setup.py:1564`) renders both; the EQ variant even carries a
  "Open local sound setup" button (`:1526`) linking the sibling. Two URLs, two
  landing rows, one file, one daemon, and the only difference is which view
  mounts. -> Low-risk consolidation: `/sound/eq/` + `/sound/setup/` under one
  prefix with the existing `.app-header__tabs` strip, `/eq/` kept as a 302.
  (effort M)

- **[M] IA-5 — The `/correction/*` alias namespace is a second spelling of six
  pages and it leaks into user-facing copy.** nginx `:468` (and `:562` on HTTPS)
  proxies the whole backend, so every surface has two URLs
  (`/sound/room/` <-> `/correction/`, `/sound/bass/` <-> `/correction/bass`, …
  route table at `jasper/web/correction_setup.py:4507`-`4611`), 12 URLs for 6
  pages. design-language.md §1 ratifies the aliases as compatibility, which is
  fine — but the secure-context recovery message hands the user the **alias**:
  `deploy/assets/correction/js/main.js:328` returns
  `'https://' + hostname + '/correction/'`, and `:968` prints it. A user who
  follows that instruction lands on the non-canonical URL, whose tab bar then
  links back into `/sound/*`. -> One-line fix: `secureCorrectionUrl()` returns
  `/sound/room/`. (effort S)

- **[M] IA-6 — `/correction/balance/` and `/correction/sync/` are reachable over
  plain HTTP.** The port-80 block proxies the whole `/correction/` prefix
  (`nginx:468`) while the canonical `/balance/`, `/sync/` locations exist only in
  the HTTPS block (`:587`, `:604`). So `http://jts.local/correction/balance/`
  renders a microphone walkthrough on a scheme where `getUserMedia` can never
  succeed. `measurement-audio.js:25` does produce a `non_secure_context` message
  when capture is attempted, so it is not silent — but the page invites a
  walkthrough it cannot run. -> Either add `/balance/`+`/sync/` to the port-80
  block with the same treatment `/correction/` gets, or 302 the HTTP alias to
  the HTTPS canonical. (effort S)

- **[M] IA-7 — Page title != landing label on 6 rows; 4 pages carry a
  "— JTS speaker" tab-title suffix nobody else uses.**

  | Route | Landing label | `<title>` | `.app-header__title` |
  |---|---|---|---|
  | `/sources/` | Playback sources | Music sources | Music sources |
  | `/wake/` | Voice assistant | Wake word | Wake word |
  | `/voice/` | Voice | Voice provider | Voice provider |
  | `/sound/crossover/` | Active speaker | Active speaker measurement — JTS speaker | **Correction** |
  | `/sound/bass/` | Bass | Bass management — JTS speaker | **Correction** |
  | `/sound/room/` | Room correction | Room correction — JTS speaker | Room correction / **Correction** |
  | `/wake-corpus/` | Developer tools | Wake-word corpus | Wake-word corpus |

  Evidence: `correction_crossover_flow.py:119` + `:26`; `correction_bass_flow.py:78`
  + `:33`; `correction_room_flow.py:280` vs `:389`; `sources_setup.py:688`;
  `wake_setup.py:539`; `voice_setup.py:1086`; `wake_corpus_setup.py:1251`.
  The worst is the correction cluster: three pages whose header all say
  "Correction" while the tab bar directly beneath them says Room / Active speaker
  / Bass — the header names the *daemon*, the tabs name the *page*. That is
  design-language §11's "name what people control, not how it's built", and
  §11's "an action keeps its name across the whole flow", broken in one screen.
  -> Set each header title to its tab label; drop the "— JTS speaker" suffix (no
  other page has it). (effort S)

- **[L] IA-8 — Two landing rows, one destination.** `Status` (index.html:778)
  and `Software` (`:794`) both link `/system/`. Not a defect (the page has both
  cards) but it is the only duplicated destination in the nav. Not actionable
  without touching the protected page — note only.

- **[L] IA-9 — Two unrelated things are called "balance".** `/balance/` is the
  phone-mic SPL walkthrough; `/rooms/`'s balance slider
  (`deploy/assets/rooms/js/grouping-view.js:24-40`, `pair-balance-controller.js`)
  is a manual L/R trim. If IA-1 is fixed by putting a link on `/rooms/`, these
  two land on the same screen. -> Name the walkthrough "Match speaker levels"
  when it is surfaced there.

- **Good, keep:** the `/tools/` -> `/tools/pack/<id>/` -> wizard round trip is the
  one properly designed cross-page flow in the product:
  `deploy/assets/tools/js/render.js:68` appends `?return_to=<current>` and five
  wizards honour it via `safe_back_href` (`google_setup.py:791`,
  `home_assistant_setup.py:1034`, `spotify_setup.py:974`,
  `transit_setup.py:1489`, `weather_setup.py:392`). It is the model the rest of
  the site's cross-links should copy.

---

## 2. Shared primitives vs per-page reinvention

### 2.1 What exists

`deploy/assets/app.css` (682 lines) owns: tokens + text ramp (`:68`-`:137`),
`.page`, `.eyebrow`, `.control-head`, `.status-val`, `.ico`, `.segmented` +
`.segmented__btn`, `.btn` + `--primary/--ghost/--default/--danger`, `.spinner` +
`.spinner--button`, `.app-header` + `__row/__title/__tabs`, `.icon-button`,
`.section` + `__head/__title`, `.info-card` (+ `--accent`, `__note`, `__hint`),
`.deflist`, `.badge` + `.badge-row`, `.jts-dialog*`, `.field`, `.form-actions`,
`.form-hint`, `.banner` (+ ok/info/danger), `.toggle`, `.sr-only`.

`deploy/assets/shared/js/`: `http.js` (212) csrf/json fetch + control-token
gate + stale-session reload · `dialog.js` (198) jtsConfirm/jtsAlert/jtsPrompt ·
`dom.js` (138) `h()`/`svg()` builder · `escape.js` (42) · `measurement-audio.js`
(393) mic/worklet/WAV · `local-web-host.js` (15).

`jasper/web/_common.py` (1410): `canonical_page`, `canonical_header`,
`safe_back_href`, `canonical_banner`, `json_island`, `toggle_html`,
`pair_banner_html`, `csrf_field_html`, PRG flash helpers.

### 2.2 Adoption (measured)

| Helper | Adopting modules | Bypassing |
|---|---|---|
| `canonical_page` | 24 of 25 pages | `correction_crossover_v2.py` (backend only, no page) |
| `canonical_header` | 19 modules | **6 pages: 3 in `sound_setup.py`, chat (JS), system (JS), balance + sync (none at all)** |
| `canonical_banner` | 9 | every fetch-driven page (by design) + **`wake_setup.py` (bug, see IX-1)** |
| `toggle_html` | 5 (`bluetooth`, `sources`, `transit`, `wake_corpus`, `wake`) | – |
| `json_island` | 5 (`correction_room_flow`, `home_assistant`, `sound_setup`, `tools`, `wake_corpus`) | – |
| `page_css_href` (static .css) | 16 | **3 still inline `page_css=`: `balance_flow.py:752`, `sync_flow.py:486`, `sources_setup.py:562`** |
| `shared/js/http.js` | 13 module dirs | `spotify/js` (fetches without it); balance/sync/bluetooth/home-assistant hand-assemble `X-CSRF-Token` headers rather than importing `jsonHeaders()` |
| `shared/js/dialog.js` | 21 module dirs | balance, sync |
| `shared/js/measurement-audio.js` | **2** (balance, sync) | **`correction/js/main.js` and `correction/js/crossover/main.js` — the two biggest mic pages — hand-roll the whole capture stack** |
| `csrf_fetch_helpers_js()` | **0** | dead (`_common.py:1101`; only tests + comments reference it) |

### 2.3 Findings

- **[H] SP-1 — Four implementations of the page header, plus two pages with
  none.** (1) `_common.canonical_header()` `:300`; (2) `sound_setup.py`
  hand-writes the identical markup three times at `:1531`, `:1572`, `:1608`;
  (3) `deploy/assets/chat/js/components.js:49` `header()`; (4)
  `deploy/assets/system-status/js/components.js:140` `appHeader()`. (3) and (4)
  are near-verbatim copies of each other. AGENTS.md: "Two implementations of one
  concern in reach: converge them or open an issue — never add a third." There
  are four. -> `sound_setup.py`'s three become `canonical_header()` calls (pure
  deletion, no behaviour change); (3)+(4) converge into one
  `/assets/shared/js/chrome.js` exporting `appHeader({title, backHref, tabs})`.
  (effort S + M)

- **[H] SP-2 — The two largest measurement pages ignore the shared measurement
  primitives.** `deploy/assets/shared/js/measurement-audio.js` exports
  `openMonoMic`, `addInlineAudioWorklet`, `float32ToWavBlob`,
  `createMonoRecorder`, `createBandpassRmsMeter`, `micCaptureSupport` — and is
  imported only by `balance/js/main.js` and `sync/js/main.js`.
  `deploy/assets/correction/js/main.js` (3450 lines) carries its own
  `float32ToWav` (`:2644`), its own inline-worklet loader (`:1056`), its own
  secure-context branch (`:959`-`:968`), and `crossover/main.js` (1090 lines) has
  no secure-context handling at all. That is why IA-5's wrong URL exists in one
  place and not the other. -> Not a rewrite: move `correction/js/main.js`'s
  capture helpers onto the shared module, one function at a time, starting with
  `float32ToWav` -> `float32ToWavBlob`. `tests/test_web_wizard_conventions.py:745`
  already asserts the shared module "owns capture primitives" — the assertion is
  currently true only for the two small pages. (effort M-L)

- **[M] SP-3 — Eight local status-pill implementations while `app.css` owns
  `.badge` with a `--tone` contract (`app.css:446`).**
  `.live-pill` (`chat.css:26` **and** `system.css:48` — same rule, different
  keyframe name), `.privacy-badge` (`correction.css:414`), `.applied-chip`
  (`crossover.css:22`), `.ha-chip` + 3 modifiers (`home-assistant.css:125`),
  `.health-badge` (`spotify.css:142`), `.credchip` (`spotify.css:240`), `.pill`
  (`wake-corpus.css:32`), `.badge--muted` (`wake.css:416` — the one that extends
  the shared primitive correctly, and the model for the rest).
  -> Converge onto `.badge` + `--tone`; keep only genuinely different shapes.
  (effort M)

- **[M] SP-4 — Nine local card implementations while `app.css` owns
  `.info-card`.** `.pair-card` (`bluetooth.css:171`), `.chat-turn-card`
  (`chat.css:116`), `.browser-audio-card` (`correction.css:316`), `.stat-card`
  (`system.css:60`), `.tool-pack-card` (`tools.css:50`), `.provider-card`
  (`transit.css:18` **and** `voice.css:100` — same class name, divergent rules:
  `font-weight:600` vs `700`), `.locked-card` (`transit.css:79`),
  `.spend-cap-card` (`voice.css:69`), `.card` (`wake-corpus.css:17`). Several
  are legitimately different components; `.provider-card` in two files under one
  name is not. (effort M)

- **[M] SP-5 — Byte-identical utility classes copy-pasted across sheets, and one
  of them already exists in `app.css`.**
  - `.btn-row {display:flex;flex-wrap:wrap;gap:8px}` — `chat.css:44`,
    `sound.css:46`, `system.css:146`. This *is* `app.css`'s `.form-actions`
    (`:561`) minus `margin-top:4px`.
  - `.boot-note {padding:3rem 1.5rem;text-align:center;color:var(--muted)}` —
    `chat.css:20`, `system.css:23`, identical.
  - `.live-pill` / `.live-pill__dot` — see SP-3.
  -> Promote `.boot-note` to `app.css` (it is the shared "module hasn't rendered
  yet" state), and replace `.btn-row` with `.form-actions`. Pure deletion.
  (effort S)

- **[M] SP-6 — `class="actions"` is emitted by two pages and styled by nobody.**
  `jasper/web/sound_setup.py:1547` and `jasper/web/correction_room_flow.py:291`
  both wrap their button rows in `<div class="actions">`. The only `.actions`
  rules in the tree are *scoped* — `.device .actions` (`bluetooth.css:113`),
  `.net-row .actions` (`wifi.css:131`), `ul.accounts li .actions`
  (`google.css:40`). Neither call site matches any of them, so those buttons
  render as an unstyled block stack instead of the 8px flex row every other
  action row uses. Both are the bonded-follower delegated screens — the ones a
  user only sees when something is already unusual. -> `class="form-actions"`.
  (effort S)

- **[L] SP-7 — `csrf_fetch_helpers_js()` (`_common.py:1101`) is dead.** Zero
  callers in `jasper/`; only `tests/test_web_common.py:396` (which tests it) and
  three comments referencing it as history. Its job moved to
  `shared/js/http.js`. -> Delete the helper and its test; fix the stale pointer
  in `_common.py:41` and `deploy/assets/wifi/js/main.js:16`. (effort S)

- **[L] SP-8 — Three pages still inline their CSS in a Python string.**
  `balance_flow.py:752` (28 lines), `sync_flow.py:486` (4), `sources_setup.py:562`
  (~125). `_common.canonical_page`'s own docstring (`:264`) calls
  `page_css_href` "the preferred form: a real, lintable static .css file". The
  balance/sync sheets are also outside the design-system tests' inventory, which
  is how `#stop { background: var(--status-danger); color: #fff; }`
  (`balance_flow.py:778` — a raw hex, and a fifth button variant) survived.
  (effort S)

- **[L] SP-9 — Off-ladder type is unchecked outside the landing page.**
  `tests/test_web_design_system.py:397` runs the ladder guard on
  `deploy/index.html` only. Across the 19 page stylesheets there are **97
  off-ladder `font-size` declarations in 13 sheets** (worst:
  `correction.css` 25, `wake.css` 24, `crossover.css` 9, `transit.css` 9), using
  20+ distinct values (`0.9rem` x14, `0.82rem` x9, `0.95rem` x8, `0.85rem` x8,
  `10px` x7, `1.05rem`, `17px`, `18px`, `24px`…). This is *not* filed as a defect
  — design-language.md §1 explicitly says it is not a codebase-wide compliance
  program — but it is the concrete size of the gap, and it is the cheapest thing
  to fix if the owner ever extends the pass beyond the measurement flow: the
  guard already exists and would only need its file list widened.

---

## 3. Interaction patterns

### 3.1 Submit model — two, cleanly split, no third

- **Classic POST -> 303 -> flash cookie -> `canonical_banner`** (8 pages):
  `airplay`, `google`, `home_assistant`, `speaker`, `spotify`, `transit`,
  `voice`, `weather`. `send_see_other` counts: `voice_setup.py` 22,
  `home_assistant_setup.py` 16, `transit_setup.py` 12, `speaker_setup.py` 7,
  `weather_setup.py` 6, `airplay_setup.py` 4, `wake_setup.py` 4.
- **fetch + JSON + in-place update** (13 pages): `sources`, `bluetooth`, `wifi`,
  `rooms`, `system`, `chat`, `tools`, `wake-corpus`, `sound/eq`, `correction`
  room/crossover/bass, `balance`, `sync`.
- **Mixed** (1): `/wake/` — a server-rendered `<form method="post">`
  (`wake_setup.py:557`) for the model picker, plus a fetch-driven mic/echo card.

That split is a reasonable, defensible architecture and I would not unify it.

- **[H] IX-1 — `/wake/` silently swallows every submit result.**
  `wake_setup.py:519` declares `_index_html(..., status_msg: str = "")` and
  `:779` passes `ctx["flash"]` into it — and the parameter is **never used in the
  function body** (verified: one occurrence in the whole function, the signature).
  So the flash cookie is consumed and discarded. The messages thrown away include
  `"Saved. Voice daemon restarting on {label}…"` (`:1070`),
  `"Could not save: {e}"` (`:1048`), and the validation error at `:1032`. A user
  picks a wake model, taps Save, the page reloads looking exactly the same, and
  the voice daemon restarts for ~4 s with no acknowledgement — the Doherty
  failure and the Postel failure in one place, on a page whose save has an
  audible side effect. -> `{canonical_banner(status_msg)}` above `<main>`, as the
  other eight PRG pages do. (effort S — one line + one test)

- **[L] IX-2 — Dead flash plumbing in the correction backend.**
  `correction_setup.py:950` `_render_page(hostname, csrf_token, flash="")`
  receives `ctx["flash"]` at `:4510` and drops it; the module has no
  `send_see_other` at all, so nothing ever sets a flash for this route. Vestigial
  parameter, not a user-visible bug. -> Delete the parameter.

### 3.2 Polling — every page rolls its own

Twelve `setInterval` sites across nine pages, each with its own constant:
`chat/js/main.js:22` 10 000 · `rooms/js/main.js:61` 7 000 ·
`sources/js/main.js:31` 4 000 · `system-status/js/main.js:21` 5 000 (with
`HIDDEN_POLL_MS` 60 000 at `:26`) · `wake/js/main.js:32` 3 000 ·
`correction/js/crossover/main.js:52` 1 500 (`HIDDEN_POLL_MS` 10 000 at `:58`) ·
`wake-corpus/js/main.js:832` 2 000 and `:835` 30 000 ·
`bluetooth/js/main.js:156` and `wifi/js/main.js:82` (variable) ·
`system-status/js/optional-features-card.js:17` 2 500 ·
`correction/js/main.js:2972` 50.

- **[M] IX-3 — Only 4 of 12 pollers back off when the tab is hidden.**
  `visibilitychange` appears in `correction/js/main.js`,
  `correction/js/crossover/main.js`, `system-status/js/main.js`, `wake/js/main.js`
  and nowhere else. The remaining pages keep hitting socket-activated wizards on
  a 1 GB Pi from a backgrounded phone tab indefinitely — which also defeats the
  `IdleShutdownTracker` that is supposed to let those wizards exit
  (`system_setup.py:73` sets a 30-minute idle shutdown that a background poll
  never allows to fire). -> One `startPolling(fn, {activeMs, hiddenMs})` in
  `shared/js/http.js`, adopted where the interval already exists. This is the
  single highest-value shared-JS addition, and it is ~25 lines. (effort M)

### 3.3 Loading / success / error

- **Loading:** `aria-busy="true"` + a placeholder paragraph on the four
  fully-client-rendered pages (`chat_setup.py:41`, `rooms_setup.py:484`,
  `system_setup.py:85`, `tools_setup.py`). `.spinner--button` (app.css:305) is
  the shared in-button spinner and is genuinely adopted (bluetooth, wifi, ha —
  and `tests/test_web_design_system.py:84` enforces that no page redefines it).
  Good.
- **Success/inline status:** five separate implementations of "one line of
  status text under the controls": `.status-line` defined twice
  (`sound.css:277`, `tools.css:359`), `.bal-status` (`balance_flow.py:756`),
  `.sync-status` (`sync_flow.py:489`), `.capture-status` (`crossover.css:179`),
  plus `.banner` for the PRG pages. -> Promote one `.status-line` to `app.css`
  (it is already used by two pages under two definitions) and retire the rest.
  (effort S)
- **Destructive confirmation:** genuinely converged. `jtsConfirm`/`jtsAlert`/
  `jtsPrompt` are used in 21 module files;
  `tests/test_web_wizard_conventions.py:790` and `:819` fail any native
  `confirm()`/`alert()`. This is the model the other patterns should follow.
- **Disabled-until-valid / disabled-while-in-flight:** ad hoc, per page
  (`balance/js/main.js:310` is a good example: disable -> status -> re-enable on
  error). No shared helper, and I would not add one — it is three lines and
  varies per action.

### 3.4 Inline `<script>` in Python — already zero

Grep for `<script>` in `jasper/web/*.py` returns **9 hits, all inside docstrings
or comments** asserting there is none (`google_setup.py:37`, `rooms_setup.py:471`,
`sources_setup.py:34`, `voice_setup.py:30`, `wake_setup.py:48` and `:537`,
`wake_corpus_setup.py:1237`, `_common.py:270` and `:409`). Every page ships an ES
module. `tests/test_web_wizard_conventions.py:658` and `:678` enforce it. **This
battle is won — do not re-file it.** The remaining Python-string HTML is markup,
not behaviour, and the largest renderers (`correction_crossover_v2.py` 7601,
`sound_setup.py` 5544, `correction_setup.py` 5200) are backend logic, not
f-string HTML.

### 3.5 One more consistency gap

- **[M] IX-4 — Two tab-strip placements.** `/system/` and `/eq/` put the
  `.segmented` strip inside the sticky header (`.app-header__tabs`:
  `system-status/js/components.js:148`, `sound_setup.py:1580`), so it stays put
  while the page scrolls. The four `/sound/*` correction pages put
  `section_tabs()` inside `<main class="page">`
  (`correction_bass_flow.py:39`, `correction_crossover_flow.py:32`,
  `correction_measurements.py:34`, `correction_room_flow.py:41`), so the tabs
  scroll away on the longest pages in the product. Same component, two
  behaviours. -> Move `section_tabs()` into the header's `__tabs` slot.
  (effort S)

---

## 4. Tests and guards — what is enforced, what a UX pass could cheaply add

### 4.1 Already enforced

`tests/test_web_design_system.py` (531): app.css exists and owns the shared
primitive list (`:64`); the landing page does not re-declare tokens/`@font-face`
(`:54`); no page-local spinner or `@keyframes *-spin` (`:84`); no page redeclares
`.toggle` (`:135`) and app.css owns the complete toggle contract (`:151`); no
global `svg` sizing (`:179`); `.app-header__tabs` lives in app.css only (`:188`);
focus outlines suppressed and `:focus-visible`/`:focus-within` cannot return
(`:207`, `:213`, `:227`); asset version is URL-safe and observes a mid-deploy
manifest swap (`:244`, `:252`); the type *grammar* (cased titles vs eyebrow
labels) cannot reflatten (`:290`); the text ramp has exactly three tiers (`:319`)
and **no page invents a fourth via `color-mix` on a foreground token** (`:344`);
the type ladder on the landing page (`:397`) with a self-test that the guard
actually fires (`:406`); 44/40px touch targets computed from `::after` overlays
(`:452`) with its own self-test (`:510`); design-language.md is reachable and
dated (`:521`).

`tests/test_web_wizard_conventions.py` (1061): shared JSON response/read helpers;
read guard on every `do_GET` and CSRF chokepoint on every `do_POST`/`do_DELETE`
(with routing-before-guard ordering); no inline JS built from untrusted metadata;
no native dialogs in modules *or* Python strings; `hidden` attribute not a
`.hidden` class; shared `escape.js` / `dom.js` / `http.js` exist and **no module
redefines `escapeHtml` / `h()` / `csrfHeaders` / `jsonHeaders`** (`:948`, `:996`,
`:1045`); the measurement-audio module owns capture primitives (`:745`).

That is a strong, well-shaped guard set: it pins *structure and single-ownership*,
not prose or taste — exactly AGENTS.md's test doctrine.

### 4.2 What a UX pass could add, cheaply and with a real tie

Only three, and each has a demonstrated recurrence in this audit — not a
hypothetical:

1. **Every page shell renders an `.app-header`.** Tie: IA-2 (`/balance/`,
   `/sync/` have none, and nobody noticed because the header assertions are all
   per-page — `test_web_airplay_setup.py:43`, `test_web_bluetooth_setup.py:155`,
   `test_web_google_setup.py:71`, …). One test that enumerates the page-render
   entry points and asserts `class="app-header"` in the output replaces a dozen
   scattered assertions and closes the gap for the next page. ~20 lines.
2. **No page re-declares a class `app.css` already owns.** Tie: SP-5
   (`.btn-row` x3 duplicating `.form-actions`, `.boot-note` x2). This is the
   exact shape of the guards that already work here (the spinner and toggle
   tests, `:84` / `:135`) — generalise them from two hard-coded names to "any
   selector defined in app.css must not be redefined in a page sheet", with an
   allowlist for deliberate scoped overrides (`.wake-page .btn { … }` is fine;
   a bare re-declaration is not). ~30 lines.
3. **Every class a page emits is defined somewhere.** Tie: SP-6 (`class="actions"`
   emitted by two pages, matched by no rule). Cheap version: collect class names
   from `jasper/web/*.py` HTML strings, diff against selectors in
   `app.css` + page sheets, allowlist JS-only hooks. Slightly noisy; worth it
   only if the owner wants it.

**Do not turn into guards** (human judgement, and rules stack badly per
AGENTS.md): "one purpose per screen", "one primary action per section", section
ordering, whether copy names the thing or the daemon, whether a confirmation is
warranted, whether a page needs a stepper. A lint for these produces false
positives on every dense settings page, which is most of this product.

**Borderline, my recommendation is no:** "every `<form>` renders its flash".
IX-1 is real, but it is *one* occurrence, the fix is one line, and a test that
walks Python ASTs looking for an unused `status_msg` parameter is more machinery
than the bug. A single behaviour pin on `/wake/`'s save ("POST -> GET renders the
message") is the right size.

---

## 5. The doc/skill question

### 5.1 The situation

- `docs/design-language.md` §1 explicitly disclaims codebase-wide scope, and §13
  ratifies a "deliberately not adopted" list. Bolting IA law onto it would
  contradict its own §1 and re-open §2.
- `docs/design-language.md:24` points the "which shared helper / primitive do I
  use?" question at **AGENTS.md "Web wizard conventions" — a section that no
  longer exists** (the 2026-08-26 reset removed it, ADR-0001). So the question
  this audit is about currently has *no home at all*, and the pointer is broken.
  That, more than anything, is the argument for a document.
- `.claude/` currently has `commands/` only — no `skills/` directory. A skill
  would be the repo's first.
- AGENTS.md caps itself at ~220 lines and says "rules stack badly"; it is at
  ~200. This cannot go there.

### 5.2 Recommendation — (b) + (c), not (a)

**Write one short sibling doc, `docs/web-ia.md` (<= 60 lines), and load it from a
minimal skill.** Concretely:

1. **`docs/web-ia.md`** — the IA/interaction contract: the site map's shape, the
   header/back rule, the naming rule (landing label = header title = tab label),
   which shared primitive owns which concept, the two submit models and when each
   applies, and a false-positive filter. It is the missing target of
   design-language.md:24 — **fix that link to point here**, which turns a broken
   pointer into the doc's justification. Draft below.
2. **`.claude/skills/web-ui/SKILL.md`** — ~8 lines of front-matter and body whose
   entire job is: *when the diff touches `jasper/web/`, `deploy/assets/`, or
   `deploy/nginx-jasper.conf`, read `docs/web-ia.md` and `docs/design-language.md`
   before writing markup.* Trigger on those paths, nothing else. The skill must
   not restate the rules — a skill that duplicates the doc is the
   `.interface-design/system.md` mistake design-language.md §13 already rejected.
3. **Not (a).** design-language.md stays the craft layer with its measurement-flow
   mandate intact. Two files, two questions ("how big/what colour" vs "where does
   this page live and what does it reuse"), matching the table already at
   design-language.md:22.

Cost: one ~55-line doc, one ~10-line skill, one corrected link. That is the least
new machinery that answers the question, per AGENTS.md.

---

## Appendix — proposed `docs/web-ia.md` (draft, 56 lines)

    # JTS web IA — where a page lives and what it reuses

    The craft layer (type, colour, depth, targets, motion, copy) is
    design-language.md. This file owns the layer above it: navigation, page
    shell, and which shared thing you are required to reuse. Read it before
    adding or restructuring a management page.

    ## 1. Every page is a leaf of the landing page

    `deploy/index.html` is the only hub (protected — design-language.md §2). A
    new page earns a row in one of its six sections, or it does not ship. Two
    exceptions, both already spent: the `/sound/*` measurement tabs
    (`jasper/web/correction_hub.py`) and `/tools/pack/<id>/`. Do not invent a
    third tab hub without converging it onto `correction_hub.section_tabs`.

    ## 2. One name, three places

    The landing-page row label, the `<title>`, and the `.app-header__title` say
    the **same words**. If they disagree, the row label wins — it is what the
    user tapped. Name the surface, never the daemon or the module ("Bass", not
    "Correction"). No "— JTS speaker" suffix.

    ## 3. Every page has the header, and the header has a back link

    `canonical_header(title, back_href=…)` from `jasper/web/_common.py`, or the
    shared JS equivalent for a client-rendered page. `back_href` points at the
    screen the user most plausibly came from — the landing page for a settings
    leaf, the parent for a sub-page (`/tools/pack/x/` -> `/tools/`). A
    deep-linked page passes `?return_to=` and the target honours it via
    `safe_back_href`; copy the `/tools/` -> wizard round trip, it is the
    reference implementation.

    ## 4. Reuse before you write

    Before adding a class or a helper, grep for it. These are single-owner — a
    second implementation is a defect, not a preference:

    | Concept | Owner |
    |---|---|
    | page shell, header, banner, toggle, JSON island, flash | `jasper/web/_common.py` |
    | button, card, badge, segmented, field, form-actions, spinner, dialog | `deploy/assets/app.css` |
    | CSRF fetch, control token, stale-session reload | `shared/js/http.js` |
    | confirm / alert / prompt | `shared/js/dialog.js` |
    | element building, HTML escaping | `shared/js/dom.js`, `shared/js/escape.js` |
    | mic capture, worklet, WAV encode | `shared/js/measurement-audio.js` |

    Page CSS goes in a static `deploy/assets/<page>/<page>.css` via
    `page_css_href`, never an inline `page_css=` string. Page behaviour goes in
    an ES module, never an inline `<script>`.

    ## 5. Two submit models. Pick one per page; never mix silently

    - **Form -> POST -> 303 -> flash -> `canonical_banner`.** For pages that are
      a form. If you `send_see_other(flash=…)`, the page **must** render it.
    - **fetch + JSON + in-place update.** For pages that are a live surface. The
      clicked control disables immediately, says what is happening in words, and
      re-enables on failure with a message saying what to do next.

    Either way: no click is silent, and anything that polls backs off when
    `document.hidden`.

    ## 6. False-positive filter — apply before filing an IA finding

    - A dense settings page is not a confusing one. Density is the brief.
    - The landing page and the `/correction/*` compatibility aliases are
      ratified. Not findings.
    - "I would have grouped these differently" is taste. A row a user cannot
      find, a page with no way back, a save with no acknowledgement — findings.
    - Out of the diff is out of scope; pre-existing conditions go to the ledger.

---

## Cross-cutting summary — ranked

1. **[H] IX-1** `/wake/` throws away every save/error message (`wake_setup.py:519`).
2. **[H] IA-1 / IA-2** `/balance/` and `/sync/`: orphaned, and no header or back link.
3. **[H] SP-1** four page-header implementations + two pages with none.
4. **[H] SP-2** the two biggest mic pages bypass `shared/js/measurement-audio.js`.
5. **[M] IA-7** header titles name the daemon ("Correction") on three pages whose
   tab bar names the page.
6. **[M] IX-3** 8 of 12 pollers never back off when hidden.
7. **[M] SP-3 / SP-4 / SP-5** 8 status pills, 9 cards, 3 copies of `.btn-row`.
8. **[M] IA-3 / IA-4** `/sound/` is two daemons and two nav models; `/eq/` and
   `/sound/setup/` are one module behind two namespaces.
9. **[M] IA-5 / IA-6 / IX-4** the alias namespace leaks into copy, serves mic
   pages over plain HTTP, and the tab strip sits in two different places.
10. **[L] SP-6 / SP-7 / SP-8 / IX-2** unstyled `.actions`, dead
    `csrf_fetch_helpers_js`, three inline `page_css` strings, dead flash param.

### Suggested PR breakdown (single-concern, < 400 lines each)

| # | PR | Findings | Tier |
|---|---|---|---|
| 1 | Render the wake flash; behaviour pin | IX-1 | Sonnet |
| 2 | Header + back link on `/balance/`, `/sync/`; link both from `/rooms/` | IA-1, IA-2, IA-9 | Opus (copy/placement judgement) |
| 3 | `sound_setup.py` uses `canonical_header()` (deletes 3 copies) | SP-1a | Sonnet |
| 4 | `shared/js/chrome.js`; chat + system adopt it | SP-1b | Sonnet |
| 5 | Header titles = tab labels across the correction cluster; drop "— JTS speaker"; move tabs into the header slot | IA-7, IX-4 | Sonnet |
| 6 | `startPolling()` in `http.js`; adopt on the 8 pages that lack backoff | IX-3 | Opus |
| 7 | Promote `.boot-note` + one `.status-line` to app.css; replace `.btn-row`/`.actions` | SP-5, SP-6, §3.3 | Sonnet |
| 8 | Converge status pills onto `.badge` + `--tone` | SP-3 | Opus |
| 9 | Move `float32ToWav` + secure-context branch out of `correction/js/main.js` | SP-2 (first slice) | Opus |
| 10 | `secureCorrectionUrl()` -> `/sound/room/`; `/balance/`+`/sync/` on port 80 or 302 | IA-5, IA-6 | Sonnet |
| 11 | Delete `csrf_fetch_helpers_js` + stale pointers; correction flash param | SP-7, IX-2 | Sonnet |
| 12 | `docs/web-ia.md` + `.claude/skills/web-ui/`; fix design-language.md:24 link | §5 | Opus |
| 13 | Guard: every page shell renders `.app-header` | §4.2(1) | Sonnet |
| 14 | Guard: page sheets may not re-declare an app.css selector | §4.2(2) | Sonnet |

`/sound/` restructuring (IA-3, IA-4) is deliberately not in this list: it is a
judgement call for the owner about URL stability, not a cleanup.
