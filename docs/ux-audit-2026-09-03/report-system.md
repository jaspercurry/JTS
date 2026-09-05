# UX / IA audit — INTEGRATIONS, ROOMS, SYSTEM cluster

Routes audited: `/ha/`, `/transit/`, `/weather/`, `/rooms/`, `/balance/`,
`/sync/`, `/speaker/`, `/system/`.

---

## /ha/ — "Connect this speaker to Home Assistant, then keep the connection healthy"

Files: `jasper/web/home_assistant_setup.py` (1358),
`deploy/assets/home-assistant/home-assistant.css` (170),
`deploy/assets/home-assistant/js/main.js` (400)

Primary user task(s): get a URL + token saved; later, confirm it still works
and (rarely) change the conversation agent or disconnect.

Screen inventory (top to bottom) — three server-chosen states
(`_state_machine`, `home_assistant_setup.py:445`):

**State 1 "none"** (`:509`)
1. Intro paragraph (`.form-hint`).
2. Card "Choose a Home Assistant instance": `Find Home Assistant on this
   network` (`btn--default`) + inline status; empty `#discover-results`;
   optional "Recent" URL chips.
3. Manual form: URL field + hint + `Continue →` (`btn--primary`).
States: idle / scanning (spinner + "Scanning the network…") / results /
empty ("No Home Assistant instances found…") / scan-failed.

**State 2 "partial"** (`:564`)
1. "Step 2 of 2 — paste a token…".
2. Card showing the saved URL.
3. Token textarea + deep link to `<HA>/profile/security`; HTTPS-only
   self-signed checkbox.
4. `Verify and save` (`btn--primary`) + `Use a different URL` (`btn--ghost`).
States: idle / server-side validation failure via flash banner.

**State 3 "connected"** (`:882`)
1. "Connected." hint.
2. Card: one sentence linking to `/tools/pack/home-assistant/`.
3. Card: `deflist` URL / Token (masked) / Agent + `Test connection`
   (`btn--default`) + status.
4. Card "Make voice phrases work for your setup": two paragraphs, `Copy
   prompt` (`btn--primary`) + `Copy with HA credentials` (`btn--default`) +
   feedback, plus a backup recommendation.
5. `<details>` "Conversation agent (advanced)" — select + `Save agent
   override` (`btn--primary`).
6. Danger card: Disconnect (confirmed via `jtsConfirm`).
States: restarting (`?restarting=1` → busy chip → ok / warn-timeout),
test ok / test fail, copy ok / copy fail.

Verdict: **TIDY**

Findings:
- [M] HA-1 — One purpose per screen / progressive disclosure — the connected
  state is six stacked cards, and the largest one
  (`home_assistant_setup.py:940-963`) is a "copy a 240-line prompt into your
  coding agent" pitch that is not a Home-Assistant *connection* task at all.
  It outranks the agent picker and sits above Disconnect, so the screen's
  answer to "what is this page for?" changes after you connect. → Move the
  voice-pack card behind a `<details>` ("Set up voice phrases") below the
  agent override, or relocate it to `/tools/pack/home-assistant/` — which the
  card at `:918-921` already points at, so the two would merge into one home.
  (effort S)
- [M] HA-2 — Primary action is obvious (Von Restorff) — state 3 renders three
  `btn--primary` (`Copy prompt` `:952`, `Save agent override` `:981`) plus a
  `btn--danger`, and the one action a returning user actually wants
  (`Test connection`, `:906`) is a quiet `btn--default`. Nothing is dominant.
  → Demote `Copy prompt` to `btn--default`; if the connected screen needs a
  primary at all it is `Test connection`. (effort S)
- [M] HA-3 — Feedback beside what it affects (Fitts) — the post-save
  "Configuring… the speaker is finishing its restart" chip is inserted after
  `document.querySelector(".info-card")` (`js/main.js:213`), which in state 3
  is the *Voice tools* link card, not the connection card. The progress
  message for the save the user just made lands attached to an unrelated
  block. → Give the connection card an id and insert relative to it.
  (effort S)
- [L] HA-4 — Prevent errors / recoverability — the connected-state agent form
  posts `token=""` and relies on "blank means keep" (`:975-978`). Correct, but
  the visible field list (URL / Token / Agent) gives the user no signal that a
  blank token is safe here; there is no note next to the override form.
  → One line in the `<details>` body: "Saving here doesn't change the token."
  (effort S)

Hygiene:
- [M] HA-H1 — `VOICE_PACK_PROMPT` (`home_assistant_setup.py:640-879`) is 240
  lines of invariant Markdown prose — 18% of the module — living in the web
  renderer purely so `json_island` can ship it to the browser. → Move to
  `deploy/assets/home-assistant/voice-pack-prompt.md` (or a `.txt` asset the
  module fetches on click); the island then carries only `currentAgent`.
  (effort S)
- [M] HA-H2 — Duplication — `.ha-chip` / `.ha-chip--busy|ok|warn`
  (`home-assistant.css:126-150`) re-implements `.banner` /
  `.banner--ok|info|danger` from `app.css:581-592`: same tone-token +
  `color-mix` recipe, same role. → Delete `.ha-chip*` and emit
  `class="banner banner--info"` etc. from `js/main.js:210`. (effort S)
- [M] HA-H3 — Duplication across pages — `wireConfirmForms()`
  (`js/main.js:383-395`) is the fourth hand-rolled copy of the
  `form[data-confirm]` submit interceptor (also `spotify/js/main.js:153`,
  `google/js/main.js:36`, `voice/js/main.js:35`). → Export one
  `wireConfirmForms()` from `/assets/shared/js/dialog.js` and delete the four
  copies. (effort S)

---

## /transit/ — "Set your location, then pick the stops the voice assistant reads out"

Files: `jasper/web/transit_setup.py` (1699),
`deploy/assets/transit/transit.css` (176),
`deploy/assets/transit/js/main.js` (79)

Primary user task(s): geocode a home address once; then tick the specific
subway station / bus stops / bike docks that should answer "next train".

Screen inventory (coords present + coverage — `_index_html:1427-1445`):
1. Intro hint.
2. "Where you are" — result panel (name + coords + `Change…` ghost button) or
   the cold address form with `Find nearby stops` (`btn--primary`).
3. Cities form — one `.toggle-row` per `CityPack`, `Save cities and restart
   voice` (`btn--primary`).
4. Save form (`id="save-form"`) containing, in order: Travel time card
   (mode select + Google Routes key), Subway card (5 radio stop rows +
   direction select), Bus card (locked-with-key-prompt **or** unlocked
   cluster/direction checkbox grid + masked key + Replace-key `<details>`),
   Citi Bike card (e-bike toggle + station checkboxes), then `Save and restart
   voice` (`btn--primary`).
5. `<details>` "Advanced — enter coordinates or stop IDs manually".
6. `Clear all transit settings` (`btn--danger`, `jtsConfirm`).
States: no-coords (2 + 5 only), no-coverage (2 + 3 + travel-time save form +
`_no_coverage_html` + 5), per-card: awaiting-address / no-stops-nearby /
needs-key / fetch-error banner / external-key banner.

Verdict: **RESTRUCTURE**

Findings:
- [H] TR-1 — Silent failure / prevent errors — the Advanced disclosure's two
  override fields duplicate names already in `save-form`:
  `adv_sub_stop name="nyc_subway_stop"` (`transit_setup.py:1247`) collides
  with the subway radio group (`:1049`) and `adv_bus_stops
  name="nyc_bus_stops"` (`:1257`) collides with the always-emitted hidden
  field (`:952`). `read_form` keeps the **first** value
  (`_common.py:791` → `v[0]`) and the cards render *above* Advanced in tree
  order, so the Advanced bus field is discarded on every submit whenever the
  bus card is unlocked, and the subway field is discarded whenever a station
  radio is checked. The section's own copy promises "Useful if your stop
  didn't show up in the nearest list" (`:1254`). → Give the Advanced inputs
  distinct names (`adv_nyc_subway_stop` / `adv_nyc_bus_stops`) and have
  `_apply_save` prefer them when non-empty; or drop the two fields entirely
  now that the pickers cover the case. (effort M)
- [H] TR-2 — Silent failure — in the **no-coords** branch (`:1341-1344`) the
  body is the address section plus the Advanced section, but no `save-form`
  exists. The Advanced "Subway station ID" / "Bus stops" inputs carry
  `form="save-form"` and are therefore orphaned: there is no submit button
  that can send them, and typing into them does nothing at all. → Render the
  stop-ID fields only when `save-form` exists, or give the Advanced section
  its own form + submit. (effort S)
- [H] TR-3 — Feedback within ~400 ms (Doherty) — the GET render blocks on live
  network I/O: `provider.find_stops_near` for subway (`:562`), bus (`:775`)
  and Citi Bike (`:991`), plus a `ThreadPoolExecutor` fan-out of one SIRI
  probe per candidate bus stop (`:786-796`, up to 8 parallel MTA calls). The
  code comments admit the constraint ("keep the render under the nginx read
  timeout", `:783`). The user clicks "Transit" from the home screen and gets a
  blank tab with no spinner, no skeleton and no timeout story. → Render the
  shell + saved picks immediately, ship coords via `json_island`, and have
  `js/main.js` fetch `./stops.json` per provider with a per-card loading state
  (the page already loads an ES module). (effort L)
- [M] TR-4 — Primary action is obvious (Von Restorff) — up to four
  `btn--primary` on one screen: `Find nearby stops` (`:589`/`:611`), `Save
  cities and restart voice` (`:1319`), `Get a free API key ↗` (`:744`), `Save
  and restart voice` (`:1415`). Two of them are *saves* with near-identical
  labels attached to different forms, and nothing on screen shows which
  controls belong to which save — a user who flips a city toggle and presses
  the lower Save loses the toggle. → One save per screen: fold the cities
  toggles into `save-form` and drop `POST /cities`; demote the register link
  to `btn--default`. (effort M)
- [M] TR-5 — Progressive disclosure / "name what people control" — the page
  speaks in implementation vocabulary at user altitude: "restart voice" on two
  buttons, `/var/lib/jasper/transit.env` and `/etc/jasper/jasper.env` in the
  external-key banner (`:827-836`), "Saved key: … (/etc/jasper/jasper.env
  (external))" (`:849-851`), and the unknown-provider fallback card telling the
  reader to "Add one to `jasper/web/transit_setup.py`" (`:1404`) — developer
  documentation rendered into the product. `_no_coverage_html` (`:1198`)
  likewise instructs the user to write a Python module. → Buttons read "Save";
  keep the "voice restarts in ~5 s" fact in the existing hint beside them.
  Move the env-path detail into the Advanced disclosure. Delete the "no UI
  yet" fallback card (a registry entry with no card should render nothing).
  (effort S)
- [M] TR-6 — Essentials first / chunking — the Travel time card (a Google
  Routes API key + a travel-mode select) is unconditionally the *first* card
  inside the transit save form (`:1385`), above the subway and bus pickers the
  page is named for, and it is the only card that is not a transit provider.
  → Move it below the provider cards, or give it its own section heading so
  "Travel time" and "Transit" read as two groups. (effort S)
- [L] TR-7 — Errors say what to do next — the geocode/save failure path
  redirects with a flash and re-renders (`send_see_other`), so a rejected
  address or a rejected BusTime key discards everything the user typed.
  → Round-trip the submitted value back into the field on the error render.
  (effort M)

Hygiene:
- [M] TR-H1 — `_bus_card_html` is 251 lines (`:713-964`) doing four jobs at
  once: HTTP fetch, a parallel SIRI probe pool, stop clustering, and HTML
  string assembly. `_citibike_card_html` (142 lines) repeats the same shape.
  → Split the data half (`fetch + cluster → list[StopRow]`) from the render
  half; the data half is what TR-3's JSON endpoint needs anyway. (effort M)
- [L] TR-H2 — `.provider-card__title` (`transit.css:33`) re-implements
  `.section__title` from `app.css:401`, and does it at `1.05rem` (16.8px) —
  a sixth type size off the ladder in design-language §3; `.cluster-heading
  strong` is `0.95rem` (15.2px). → Use `.section__title`. (effort S)

---

## /weather/ — "Where should bare weather questions be answered for, and in which units"

Files: `jasper/web/weather_setup.py` (493),
`deploy/assets/weather/weather.css` (54), no ES module

Primary user task(s): set a default weather location + °C/°F once.

Screen inventory (`_index_html:283`):
1. Intro hint.
2. Section "Current default" — accent card with name + badge
   (Saved / From transit / Legacy) + coords + a source sentence, or a
   "No weather default is set yet." note.
3. Section "Set weather location" — Location text field (+ Nominatim privacy
   hint), Temperature-units select, `<details>` "Manual coordinates" with
   lat/lon, `Save` (`btn--primary`).
4. Separate form: `Clear weather default` (`btn--danger`).
States: empty / saved / inherited-from-transit / legacy-place-name, plus
flash banners for save + error.

Verdict: **TIDY** — the cleanest page in the cluster; the three-state current
card is well judged and the transit-inheritance badge is exactly right.

Findings:
- [M] WX-1 — Warn before destructive — `Clear weather default` (`:379`) posts
  straight to `./clear` with no confirmation and no statement of consequence.
  The page ships no ES module, so it cannot reach `jtsConfirm`, while its
  sibling `/transit/` does confirm the equivalent action
  (`transit/js/main.js:69`). Same danger button, two behaviours. → Add a
  4-line ES module reusing the shared `data-confirm` wiring proposed in
  HA-H3. (effort S)
- [L] WX-2 — Sensible defaults / fewest steps — the Location input is always
  rendered empty (`:335`) even when a default is saved, so changing units
  alone means either re-typing the address or trusting that a blank location
  preserves the saved one (it does — `_apply_save:210` — but nothing says so).
  → Either prefill the saved display name, or add "Leave blank to keep the
  current location." to the field hint. (effort S)

Hygiene:
- [L] WX-H1 — `details.advanced` in `weather.css:34-49` is one of **thirteen**
  independent `<details>/<summary>` implementations across page stylesheets
  (`airplay`, `google`, `rooms`, `sound-profile`, `spotify`, `system-status`,
  `tools`, `transit`, `voice`, `wake`, `weather`, `wifi`, `correction`);
  `app.css` styles no disclosure at all. Progressive disclosure is this UI's
  main IA tool and it has no shared primitive. → Add one `.disclosure`
  component to `app.css` and delete the per-page copies. (effort M, but this
  is the single highest-leverage hygiene fix in the cluster.)

---

## /rooms/ — "See every JTS speaker on the network; pair two of them; choose who answers the wake word"

Files: `jasper/web/rooms_setup.py` (2020),
`deploy/assets/rooms/rooms.css` (249),
`deploy/assets/rooms/js/main.js` (1060), `grouping-view.js` (176),
`pair-balance-controller.js` (179); `jasper/web/pair_flow.py` (85, shared with
`/balance/` + `/sync/`)

Primary user task(s): confirm this speaker's identity, jump to another
speaker's settings, form or dissolve a stereo pair, set wake arbitration.

Screen inventory (client-rendered by `buildPage`, `js/main.js:948`):
1. `.control-head` — "SPEAKERS" eyebrow + "Live · N other speakers".
2. Section "This speaker" — deflist Name / Hostname / Address / Room (with a
   "Change in Speaker settings" link), then an eyebrow "Grouping" and either
   a Solo badge, a Misconfigured badge + error, or a Grouped/Degraded badge
   plus deflist Role / Channel / Bond / Leader / Buffer / Codec / AirPlay
   lip-sync / Snapcast + notes.
3. Bond card — one card, three faces: loading, **create** (intro + "Pair with"
   select + `Create stereo pair` primary), **paired** (intro + one-line
   summary + optional mains-high-pass toggle + balance slider block +
   `Advanced` sub button + add-sub panel + `Swap left ↔ right` + `Dissolve
   group` danger) + a shared status line.
4. Section "Speakers on this network" — peer rows linking to each peer's
   `/system/`, or "No other JTS speakers found on this network yet."
5. Section "Wake response" — explanatory paragraph, toggle, conditional
   "Primary" sub-row, status line.
6. Note card — "treat stereo pairing as a preview for now".
States: loading / live / disconnected (`body.stale`), per-card saving +
error, plus per-section render isolation.

Verdict: **RESTRUCTURE**

Findings:
- [H] RM-1 — Dead UI — the whole "Advanced → Add subwoofer" sub-panel is
  unreachable. `_rooms_view` hardcodes `"show_subwoofer_controls": False`
  (`rooms_setup.py:461`, the only producer in the tree) and `js/main.js:741`
  gates the panel on `view.show_subwoofer_controls === true`. That strands the
  `advancedSubBtn` / `addSubPanel` / `addSubSelect` / `addSubCrossover`
  controls, the `addSub()` handler (`:874-916`), `lastSubReachable`,
  `advancedSubOpen`, and `addSubPlan()` in `grouping-view.js:78` — roughly 120
  lines of JS with no path to a user. The mains-high-pass toggle
  (`js/main.js:540`) is reachable only for a sub bond that this UI cannot
  create. → Delete the add-sub panel and the `show_subwoofer_controls` flag,
  or ship it. Deleting is the smaller change and the flag's own comment says
  "The primary page must not surface sub/crossover controls". (effort M)
- [H] RM-2 — Chunking / never make the user remember another screen —
  `/rooms/`, `/balance/` and `/sync/` are three faces of one job (make a
  stereo pair sound right) with **no** navigation between them. `/rooms/`
  offers a by-ear balance slider (`js/main.js:468-540`, `POST /trim`);
  `/balance/` offers a mic-measured balance walkthrough that writes the same
  trims (`balance_flow.py:678`); neither mentions the other. `/sync/` is
  invisible from both. → Add a "Tune this pair" row to the paired face of the
  bond card linking `/balance/` and `/sync/` (see BAL-1 for the HTTPS
  caveat), and label the slider "Quick balance by ear" so its relationship to
  the walkthrough is legible. (effort M)
- [M] RM-3 — Progressive disclosure / name what people control — the "This
  speaker" card puts `Bond` (a raw UUID), `Buffer`, `Codec`, `Snapcast` and
  `AirPlay lip-sync` at the top of a non-diagnostics page
  (`js/main.js:127-168`). Half the first screen is transport internals.
  → Keep Role/Channel and the health badge; move Bond/Buffer/Codec/Snapcast
  behind a "Details" disclosure (or onto `/system/`, which already owns
  technical evidence). (effort S)
- [M] RM-4 — Essentials first (serial position) — "Wake response" is the one
  always-working write surface on the page (its own comment says so,
  `js/main.js:16`) and it sits **fifth**, below the peer directory. The
  preview caveat ("Perfect sample-lock … is still being validated") sits
  sixth, i.e. after the user has already pressed `Create stereo pair`.
  → Order: This speaker → Wake response → Pairing (with the preview caveat
  inside the create face, above the button) → Speakers on this network.
  (effort S)
- [M] RM-5 — An action keeps its name (design-language §11) — the bond card's
  title flips between "Create a stereo pair" and "Speaker grouping"
  (`js/main.js:596`, `:632`), and the same card's copy alternates "pair" and
  "group" ("Dissolve group" button vs "Dissolve **this speaker group**?"
  confirm vs "Pair dissolved." status vs "Create stereo pair"). → Pick one
  noun ("pair" reads better for a two-speaker product) and one stable card
  title. (effort S)
- [L] RM-6 — Errors say what to do next — `describeBondFailure`
  (`js/main.js:400-428`) surfaces "a speaker needs its control token — set it
  once via the System page (open /system/ and run any action), then retry (see
  SECURITY.md)". Referring the household to a repo file, and to a workaround
  ritual, is not an action they can take from this screen. → Link `/system/`
  and drop the `SECURITY.md` reference. (effort S)

Hygiene:
- [M] RM-H1 — Duplication of a shared component — `.wake-row`
  (`rooms.css:116`) and `.toggle-row` (`transit.css:139`) are the same
  "label + hint + toggle in a row" primitive with the control on opposite
  sides (rooms puts the toggle first, transit last). Two sibling settings
  pages disagree on where a toggle lives. → Promote one to `app.css` beside
  the `.toggle` rules that `toggle_html()` already targets. (effort S)
- [L] RM-H2 — Off-system values / reinvented primitive — `.bond-select` and
  `.bond-crossover` (`rooms.css:164-181`) hand-roll a select/number input with
  a literal `border-radius: 8px` and no border or background, so they render
  as bare UA chrome next to the properly-styled `.field select` in
  `app.css:535`. → Wrap them in `.field` and delete both rules. (effort S)
- [L] RM-H3 — `js/main.js` is 1060 lines with `makeBondCard()` alone spanning
  `:437-945`. The `/system/` page already solved this shape with
  `components.js` (`titledCard`, `collapsible`, `actionButton`, `defList`,
  `badge`) — `/rooms/` rebuilds all of those by hand. → Lift
  `system-status/js/components.js` into `/assets/shared/js/` and consume it
  here; split `makeBondCard` into create-face / paired-face modules.
  (effort M)

---

## /balance/ — "Match the two speakers' loudness at the listening position using this phone's microphone"

Files: `jasper/web/balance_flow.py` (829, of which the page shell is
`:752-829`), `jasper/web/balance_level.py` (131),
`jasper/web/balance_volume_guard.py` (331),
`deploy/assets/balance/js/main.js` (403); no static stylesheet

Primary user task(s): run a two-speaker guided measurement, then apply the
recommended trims.

Screen inventory (`_PAGE_BODY`, `balance_flow.py:782`):
1. "STEREO PAIR" eyebrow + a bare `<h1>Balance speakers</h1>`.
2. One card: explanation paragraph, a 3-item ordered list of instructions,
   status line, mic meter bar + target marker + "mic level / — dB" row,
   progress list, verdict line.
3. Action row: `Start walkthrough` (primary), `Stop`, `Retry this speaker`,
   `Apply`, `Start over` — all in one flex row, four initially hidden.
States (`js/main.js`): precondition-blocked (not bonded / not leader /
session already running), idle, mic-permission, floor-check, ramping per
channel, not-heard failure + retry, analyzed + verdict, applied, stopped.

Verdict: **RESTRUCTURE**

Findings:
- [H] BAL-1 — Discoverability / Jakob — the page has **no inbound link
  anywhere in the UI**: `grep` for `/balance/` across `deploy/index.html`,
  `deploy/assets/**` and `jasper/web/**` returns only the route allowlists in
  `correction_setup.py:362-368` and the nginx blocks. Worse, nginx serves
  `/balance/` **only** inside the `listen 443` server
  (`deploy/nginx-jasper.conf:586`) — unlike `/sound/room/` and the other
  measurement pages, which are deliberately mirrored on port 80 so the
  non-capture views stay reachable without a cert prompt
  (`nginx-jasper.conf:426-431`). So the only way to reach this flow is to
  type an `https://` URL by hand and accept a self-signed certificate. → Add
  the port-80 mirror (matching the `/sound/room/` precedent) and link it from
  the `/rooms/` paired face per RM-2. (effort M)
- [H] BAL-2 — Consistency / navigation — the page never calls
  `canonical_header` (`balance_flow.py:821-829` passes only `_PAGE_BODY`), so
  there is no back button, no sticky title bar, and no way out except the
  browser's back gesture. It substitutes a bare `<h1>`, which `app.css:153`
  only zeroes the margin of — so it renders at the UA default 32px, far off
  the 11/12/13/14/16 ladder that every other page keeps. → Use
  `canonical_header("Balance speakers", back_href="/rooms/")` and delete the
  `<h1>`. (effort S)
- [M] BAL-3 — Errors say what to do next — the blocked-precondition copy is
  plain text: "No stereo pair is bonded — set one up at jts.local/rooms
  first." (`js/main.js:389`). It is not a link, and it hardcodes `jts.local`
  even though the speaker's hostname is configurable (`/speaker/` correctly
  renders the real one, `speaker_setup.py:385`). A user on `jts3.local` is
  sent to the wrong box. → Render a real `<a href="/rooms/">` — same-origin,
  so no hostname needs naming at all. (effort S)
- [M] BAL-4 — Primary action / peak-end — at the end of the walkthrough the
  goal action `Apply` is a plain `.btn` (`balance_flow.py:809`) while the
  now-irrelevant `Start walkthrough` keeps `btn--primary`. The completion
  moment — "The left speaker is 2.3 dB louder. Apply sets the trims above." —
  presents its own call to action as the quietest button on screen. → Swap the
  variants at the analyzed transition; and since `Apply` writes to both
  speakers, give it the `sentMessage`-style confirmation the rest of the UI
  uses. (effort S)
- [L] BAL-5 — Visible progress (Zeigarnik) — this is a genuine two-step flow
  (left, then right) but the only progress signal is a row appended after each
  lock. During the ~25 s ramp the user cannot tell whether they are on speaker
  1 or 2 of 2. → Add "Speaker 1 of 2 — left" to the status line; the channel
  is already in `session.channel`. (effort S)

Hygiene:
- [M] BAL-H1 — Inline CSS in a Python string — `_PAGE_CSS`
  (`balance_flow.py:752-780`) is 28 lines shipped via `page_css=`, against the
  documented preference for a lintable static file
  (`_common.py:265-270` names `page_css_href` "the preferred form"), and
  against the eight sibling pages that already use one. Inside it,
  `#stop { background: var(--status-danger); color: #fff; }` re-implements
  `.btn--danger` (`app.css:283`) with a hardcoded `#fff` and an id selector.
  → Create `deploy/assets/balance/balance.css`; replace the `#stop` rule with
  `class="btn btn--danger"` on the button. (effort S)

---

## /sync/ — "Measure left/right arrival timing at the listening position and apply a channel delay"

Files: `jasper/web/sync_flow.py` (523, page shell `:485-523`),
`deploy/assets/sync/js/main.js` (126); no static stylesheet

Primary user task(s): run one marker measurement and apply the recommended
delay.

Screen inventory (`_PAGE_BODY`, `sync_flow.py:490`):
1. "STEREO PAIR" eyebrow + bare `<h1>Measure sync</h1>`.
2. Card: one paragraph of explanation, a status line, an **unstyled `<pre>`
   that receives `JSON.stringify(data, null, 2)`** as the entire result
   surface (`js/main.js:32-34`), and four buttons: `Start` (primary),
   `Play marker` (disabled), `Apply` (disabled), `Stop` (enabled).
States: only what `setStatus` writes — "Opening measurement window...",
"Recording marker...", "Analyzing...", "Measurement ready." /
"Measurement needs a retry.", "Applying delay...", "Applied.", "Stopped.",
or a raw error message. No precondition check on load, no mic-level feedback,
no empty state.

Verdict: **RESTRUCTURE** (or explicitly demote to an operator tool)

Findings:
- [H] SY-1 — Discoverability — same as BAL-1: zero inbound links, and
  HTTPS-only (`nginx-jasper.conf:604`) with no port-80 mirror. → Same fix.
  (effort M)
- [H] SY-2 — One purpose per screen / prevent errors — the result of the
  measurement is a raw JSON dump into a `<pre>` with no CSS anywhere (neither
  `_PAGE_CSS` at `sync_flow.py:485` nor `app.css` styles `pre`), so it renders
  as unwrapped monospace that will scroll the page body horizontally on a
  phone. `Apply` writes a channel delay to the pair's render graph on the
  strength of that dump, with no confirmation dialog — every other write in
  this cluster confirms. → Render the recommendation as two `defList` rows
  ("Left delay / Right delay") plus a plain-language verdict; keep the JSON
  behind a "Technical detail" disclosure; add a `jtsConfirm` on Apply.
  (effort M)
- [H] SY-3 — Name what people control (design-language §11) — the page's only
  explanatory paragraph reads "recommends positive-only channel delay for the
  **leader render graph**" (`sync_flow.py:495-497`). That is a description of
  the implementation, on the one screen a household member would open to fix
  a stereo image that sounds pulled to one side. → "Corrects for one speaker
  being closer to where you sit, so the sound arrives from both at once."
  (effort S)
- [M] SY-4 — Disable impossible actions / feedback — `Start` is the only
  enabled button and `Stop` is enabled from first paint with nothing to stop
  (`sync_flow.py:509-513`). There is no precondition check on load, so a user
  on a non-bonded or follower speaker learns that only after pressing Start —
  `/balance/` does this correctly in its `init()` (`balance/js/main.js:379`).
  There is also no mic level, so the user holds the phone still through a
  blind 3.3 s recording (`sync/js/main.js:53`). → Mirror `/balance/`'s init
  gate; disable `Stop` until a session exists; reuse the `.bal-meter`
  primitive. (effort M)
- [M] SY-5 — Consistency / navigation — no `canonical_header`, bare `<h1>` at
  UA default size. Same as BAL-2. → Same fix,
  `back_href="/rooms/"`. (effort S)

Hygiene:
- [L] SY-H1 — Inline `_PAGE_CSS` (3 rules, `sync_flow.py:485-489`) — same
  finding as BAL-H1; if the two flows converge into one "Tune this pair"
  surface they can share one stylesheet. (effort S)
- [L] SY-H2 — `sync/js/main.js:19-20` reads the CSRF meta tag by hand to build
  its own header for the `analyze` upload, while the same file imports
  `jsonHeaders` from `shared/js/http.js` for everything else. → Extend the
  shared helper with a content-type override and drop the local `csrf` const.
  (effort S)

---

## /speaker/ — "Name this speaker and say which room it's in"

Files: `jasper/web/speaker_setup.py` (533),
`deploy/assets/speaker/js/main.js` (32); no page stylesheet

Primary user task(s): rename the speaker as it appears in AirPlay / Spotify /
Bluetooth pickers; optionally set a room label.

Screen inventory (`_index_html:368`):
1. Canonical header "Speaker name" + back to `/`.
2. Flash banner slot.
3. Hint naming the four picker surfaces and the unchanged address.
4. Field "Speaker name" (maxlength, default named in the hint).
5. Field "Room (optional)" with an example and "Leave blank to clear."
6. `Save and restart` (`btn--primary`), gated by a `jtsConfirm`.
States: idle / confirm dialog / flash for saved, unchanged, validation error,
name conflict, or partial-restart failure.

Verdict: **TIDY** — the best-proportioned wizard in the cluster: one screen,
two fields, one action, primitives only, no page CSS.

Findings:
- [M] SP-1 — Prevent errors / preserve work — every failure path is
  `send_see_other(self, "./", flash=…)` (`speaker_setup.py:459`, `:466`,
  `:485`, `:497`), which redirects to a fresh GET. A rejected name, or a name
  that collides on AirPlay (`_format_conflicts:177`), is reported *and the
  user's typed value is thrown away* — they must retype it to try a variant.
  Design-language §11 asks errors to be actionable; here they are also
  destructive. → Re-render the form with the submitted values on error
  instead of redirecting. (This flash-and-redirect pattern is shared with
  `/transit/` and `/weather/` — see the cluster summary.) (effort M)
- [M] SP-2 — Copy accuracy / fewest steps — the submit guard always asks
  `Rename speaker to "X"? This restarts audio, Bluetooth, and voice services.`
  (`speaker/js/main.js:26`), even when only the **Room** field changed and the
  name is untouched. The server already distinguishes that case — it skips the
  network conflict probe for a room-only edit (`speaker_setup.py:474-476`) —
  so the browser is warning about a rename that will not happen. → Stash the
  current name in a `data-current` attribute, skip the confirm when the name
  is unchanged, and word the room-only case as "Save room?". (effort S)
- [L] SP-3 — One purpose per screen — the page edits name **and** room, but
  its `canonical_header` title, its `<title>`, and the landing-page row that
  reaches it all say "Speaker name"; `/rooms/` links here as "Change in
  Speaker settings" (`rooms/js/main.js:206`). Three names, two of which are
  wrong about the room field. → Title the page "Speaker" and keep the field
  labels doing the explaining. (effort S)

Hygiene: none. (No page CSS, primitives only, 32-line module with one job —
this is the shape the other wizards should aim at.)

---

## /system/ — "What is this speaker doing right now, and the buttons to poke it"

Judged as a diagnostics surface: density and technical vocabulary are fine
here; ordering and grouping still apply.

Files: `jasper/web/system_setup.py` (268),
`deploy/assets/system-status/system.css` (529), plus 12 ES modules
(`main.js` 192, `views.js` 222, `sections.js` 425, `audio-view.js` 187,
`audio-sections.js` 422, `components.js` 199, `format.js` 240,
`optional-features-card.js` 287, `actions.js` 162, `debug-card.js` 115,
`usb-forensics-card.js` 71, `charts.js` 65, `api.js` 16)

Primary user task(s): confirm the speaker is healthy; check the running build;
restart something; run the doctor.

Screen inventory — **System view** (`views.js:29`):
1. Sticky header (back + "Status") with a System / Audio segmented nav.
2. Live pill — "Live · sampler OK" / "stale Ns" / "Disconnected…".
3. Audio alert card — hidden while healthy.
4. Audio transport parks card — hidden while healthy.
5. Vitals stat grid — Memory, Memory pressure (conditional), Load pressure,
   CPU usage, Temperature, Fan (conditional), Disk; each a dot + value + sub +
   sparkline/bars/badge.
6. Software card — Version / Branch / Installed / Uptime / Voice provider,
   plus the Enhanced-AEC optional-install card.
7. Home Assistant card — Status badge / URL / Version + "Configure at
   jts.local/ha".
8. Network card.
9. Actions card — a note, then `Restart voice`, `Restart audio`,
   `Reboot speaker` (danger), `Power off` (danger) in one `.btn-row`, an
   aria-live status line, and a power-off caution note.
10. Run diagnostics card (accent) — button + output table.
11. USB forensics card (hidden when the USB source is off).
12. Debug logging collapsible.
13. Per-service usage collapsible — **open by default**.

**Audio view** (`audio-view.js:77`): live pill, Current stream (accent),
USB latency *setting*, Recent issues, Other sources, Technical evidence
(collapsed), Audio quality *setting*.

Verdict: **TIDY** — the strongest engineering in the cluster (build-once +
memoised `renderSection`, per-section error isolation, hidden-tab poll
backoff, honest action feedback). The findings are ordering and one dead ref.

Findings:
- [M] SYS-1 — Progressive disclosure — "Per-service usage" is `collapsible({
  title: "Per-service usage", open: true, … })` (`views.js:104`), so the
  longest and most technical block on the page — a cgroup CPU/memory table
  that needs a horizontal scroll on a phone (`system.css:119`,
  `.table--services { min-width: 420px }`) — is expanded on every load, while
  "Debug logging" and "Technical evidence" correctly default closed. → `open:
  false`. (effort S)
- [M] SYS-2 — Essentials first (serial position) — `runDiagnostics` renders
  the full doctor table first and the verdict ("N failed, M warning(s)")
  *after* it (`actions.js:132-138`), so on a 30-check run the one line the
  user came for is below the fold and failures are wherever the doctor
  happened to emit them. → Put the summary line above the table and sort
  fail → warn → ok. (effort S)
- [M] SYS-3 — Chunking / grouping — the Audio view interleaves two write
  settings among four read-only diagnostics: USB latency sits third (between
  "Current stream" and "Recent issues") and Audio quality sits last, after the
  collapsed "Technical evidence" (`audio-view.js:88-99`). Nothing signals that
  those two cards change behaviour while the rest only report it. → Put both
  settings adjacent under one "Audio settings" heading, either directly under
  "Current stream" or at the foot of the view. (effort S)
- [L] SYS-4 — Errors say what to do next — `renderSection`'s failure text is
  "Couldn't render this section — see the console."
  (`components.js:196`) and `runDiagnostics` surfaces bare
  `"Failed: " + e.message`. Acceptable on a diagnostics surface, but "see the
  console" is not an action the phone user can take. → "Couldn't render this
  section. Reload the page; if it persists, run diagnostics." (effort S)
- [L] SYS-5 — Consistency — the HA card's footer hardcodes the hostname:
  `"Configure at ", h("a.link", { href: "/ha/" }, "jts.local/ha")`
  (`sections.js:266-267`). The href is correctly relative; only the label
  lies, and on a household with a second speaker it names the wrong box.
  Same bug class as BAL-3. → Label it "Home Assistant settings". (effort S)
- [L] SYS-6 — Jakob / deep links — `deploy/index.html:778` and `:794` are two
  distinct rows ("Status" and "Software") pointing at the same `/system/`
  top, so the Software row lands the user on the vitals grid and leaves them
  to scroll. `/system/` already supports a second view via a real URL
  (`/system/audio/`), so the mechanism exists. → Give the Software card an
  `id="software"` so a `#software` deep link works; whether the landing page
  adopts it is the owner's call (protected file — no change proposed there).
  (effort S)

Hygiene:
- [M] SYS-H1 — Fighting a shared primitive — `system.css` carries **14**
  `!important` declarations (`:165, 172, 205, 207, 214, 221, 231, 244, 250,
  255, 268, 282, 290, 296`), every one of them overriding
  `.info-card > * + * { margin-top: 12px }` (`app.css:417`) for cards that
  manage their own flow with `display: grid; gap`. → Add one
  `.info-card--flow-none` modifier to `app.css` (or scope the `> * + *` rule
  to `:not([class*="--flow"])`) and delete all 14. (effort S)
- [M] SYS-H2 — Dead reference — `capabilityNote` is created, appended, and
  then unconditionally hidden and blanked on every poll
  (`views.js:57-59`, `:158-159`); its own comment states the producer endpoint
  was removed. The element, the `refs.capabilityNote` entry and the two
  assignments are dead. → Delete all four sites. (effort S)
- [L] SYS-H3 — Reusable components stranded in a page —
  `system-status/js/components.js` is a clean, generic component library
  (`titledCard`, `defList`, `badge`, `table`, `collapsible`, `actionButton`,
  `header`, `livePill`, `renderSection`) that only this page can import,
  while `/rooms/` hand-builds the same shapes. Its `header()` is also a second
  implementation of Python's `canonical_header` (`_common.py:300`), in JS.
  → Move `components.js` under `/assets/shared/js/`. (effort S; enables
  RM-H3.)
- [L] SYS-H4 — `.cpu-bars__label { font-size: 10px }` (`system.css:96`) is
  below the 11px floor of design-language §3's ladder. → 11px. (effort S)

---

## Cluster summary

**Ranked — which pages need work most**

1. **`/sync/`** — a debug harness on a public route: raw `JSON.stringify` as
   the result surface, an unconfirmed write to the pair's render graph, copy
   written in transport vocabulary, no precondition gate, no header, and no
   inbound link at all.
2. **`/transit/`** — two silent-failure bugs in the Advanced section (duplicate
   field names lose the override; orphaned `form="save-form"` inputs in the
   cold state), a GET that blocks on up to a dozen live network calls with no
   loading state, and four competing primary buttons across two ambiguous
   save forms.
3. **`/balance/`** — a genuinely good measurement flow that nothing links to
   and that nginx serves only over HTTPS, so it is effectively unshipped;
   plus no back navigation and a secondary-styled `Apply`.
4. **`/rooms/`** — ~120 lines of permanently-unreachable add-subwoofer UI, the
   page's only reliable setting buried fifth, and transport internals (bond
   UUID, buffer, codec) as the first thing the user reads.
5. **`/ha/`** — sound three-state machine; the connected screen has drifted
   into a coding-agent-prompt marketing card with three primaries.
6. **`/system/`** — solid; ordering nits (per-service open by default,
   diagnostics verdict below the table, settings scattered through the Audio
   view) and one dead ref.
7. **`/weather/`** — clean; one unconfirmed destructive button.
8. **`/speaker/`** — the reference shape for a small wizard; a confirm that
   lies on room-only edits, and error paths that discard typed input.

**Cross-page patterns in this cluster**

- **No shared disclosure primitive.** `app.css` styles no `<details>` at all,
  and thirteen page stylesheets each hand-roll one (`transit.css:150`,
  `weather.css:34`, plus eleven outside this cluster). Progressive disclosure
  is the main IA tool these settings pages use and it is the one component the
  design system does not own. One `.disclosure` in `app.css` retires all of
  them.
- **Flash-and-redirect destroys typed input.** `/speaker/`, `/transit/` and
  `/weather/` all answer a validation or upstream rejection with
  `send_see_other(..., flash=…)`, so the user's work is gone and they retype
  it. A shared "re-render with submitted values" helper in `_common.py` would
  fix three pages at once.
- **Confirm-on-submit is copy-pasted four times.** `form[data-confirm]`
  interception is re-implemented in `home-assistant`, `spotify`, `google` and
  `voice` modules; `/weather/` has no module and therefore no confirm on its
  danger button. One export from `shared/js/dialog.js` fixes both symptoms.
- **`jts.local` hardcoded in copy** while the hostname is configurable
  (`balance/js/main.js:389`, `system-status/js/sections.js:267`). `/speaker/`
  and `/rooms/` do it right (real identity / `localWebHost`).
- **Duplicated row primitives.** `.wake-row` (rooms) vs `.toggle-row`
  (transit) are the same label+toggle row with the control on opposite sides;
  `.provider-card__title` duplicates `.section__title`; `.ha-chip` duplicates
  `.banner`; `.bond-select` duplicates `.field select`. Each is one page
  inventing what `app.css` already ships.
- **Two page shells.** `/balance/` and `/sync/` skip `canonical_header`
  entirely and substitute bare `<h1>`s that land at the UA's 32px, off the
  type ladder — they are the only two pages in the cluster without a back
  button, and (not coincidentally) the only two nothing links to.
- **The multiroom trio is three disconnected pages, not one flow.** `/rooms/`
  creates the pair and offers a by-ear balance slider; `/balance/` measures
  the same trims with a microphone; `/sync/` measures arrival timing. Neither
  measurement page is reachable from `/rooms/`, from the landing page, or
  over plain HTTP. A user who pairs two speakers has no path to the two tools
  built to make that pair sound right.

**Estimated PR breakdown** (single-concern, < 400 changed lines each)

| # | PR | Scope | Tier |
|---|---|---|---|
| 1 | Fix `/transit/` Advanced overrides | TR-1 + TR-2: rename the duplicate fields, teach `_apply_save` to prefer them, gate them on `save-form` existing | Sonnet |
| 2 | One save per `/transit/` screen | TR-4 + TR-6: fold the cities toggles into `save-form`, retire `POST /cities`, demote the register link, reorder Travel time | Opus |
| 3 | Async stop discovery on `/transit/` | TR-3 + TR-H1: split fetch from render, add `./stops.json`, per-card loading states | Opus |
| 4 | De-jargon `/transit/` copy | TR-5: button labels, env-path detail into Advanced, delete the "no UI yet" and "write a Python module" cards | Sonnet |
| 5 | `.disclosure` primitive in `app.css` | WX-H1: one component + delete 13 per-page copies (large deletion, small addition) | Sonnet |
| 6 | Shared confirm-forms + `/weather/` clear guard | HA-H3 + WX-1: export `wireConfirmForms()`, delete 4 copies, add the `/weather/` module | Sonnet |
| 7 | Preserve form input on error | SP-1 (+ `/transit/`, `/weather/`): re-render with submitted values instead of redirecting | Opus |
| 8 | Delete `/rooms/` add-subwoofer dead UI | RM-1: drop the panel, handler, `addSubPlan`, and `show_subwoofer_controls` | Sonnet |
| 9 | Reorder + de-jargon the `/rooms/` cards | RM-3 + RM-4 + RM-5 + RM-6: details disclosure, section order, one noun for pairing, fix the token error copy | Opus |
| 10 | Reach `/balance/` and `/sync/` | BAL-1 + SY-1 + RM-2: port-80 nginx mirrors, "Tune this pair" links from the `/rooms/` paired face | Opus |
| 11 | Give `/balance/` + `/sync/` the canonical shell | BAL-2 + BAL-5 + SY-5 + BAL-H1 + SY-H1: `canonical_header`, static stylesheets, step indicator | Sonnet |
| 12 | Make `/sync/` legible | SY-2 + SY-3 + SY-4: `defList` recommendation, JSON behind a disclosure, confirm on Apply, init gate, rewritten copy | Opus |
| 13 | `/balance/` + `/ha/` primary-action pass | BAL-4 + HA-2 + HA-3: button variants at the completion moment, chip anchoring | Sonnet |
| 14 | Move the HA voice-pack out of the renderer | HA-H1 + HA-1: prompt to a static asset, card behind a disclosure or into `/tools/` | Sonnet |
| 15 | `/speaker/` confirm accuracy + title | SP-2 + SP-3 | Sonnet |
| 16 | `/system/` ordering pass | SYS-1 + SYS-2 + SYS-3 + SYS-6 | Sonnet |
| 17 | `.info-card--flow-none` + `/system/` dead ref | SYS-H1 + SYS-H2 + SYS-H4: one modifier, delete 14 `!important`s and `capabilityNote` | Sonnet |
| 18 | Share `components.js` | SYS-H3 + RM-H1 + RM-H2 + RM-H3: lift to `shared/js/`, converge `.wake-row`/`.toggle-row`, delete `.bond-select` | Opus |
