# Cluster: connectivity and music sources (/wifi/ /bluetooth/ /airplay/ /spotify/ /google/ /sources/)

## Cluster summary

**Ranked — most work needed first**

1. **`/spotify/`** — the management state buries the account list under four blocks of explanatory prose; the OAuth-mode picker is JS-dependent and one-way (reset credentials to change a radio); it is the only page still styling bare `<h2>` at an off-ladder 17px.
2. **`/sources/`** — its error banner uses a CSS class that exists nowhere, so the alert paints neutral; USB copy is written twice; the page has three names; and the hub for four sources has no link into any of them.
3. **`/google/`** — a 190-line dual-mode wizard body rendered twice on every management load, setup-time localStorage leaking into the read-only copy, eight inline layout styles and twelve `em` type steps.
4. **`/wifi/`** — solid page; a documented fallback ("Join by name") is given main-path prominence with a competing primary CTA, and the lockout warning appears in two different affordances.
5. **`/bluetooth/`** — the best flow here (staged progress, real terminal states, correct disabled logic); pair progress names BlueZ internals, and its master toggle silently shadows the `/sources/` row.
6. **`/airplay/`** — right-sized for a single knob and the cleanest code in the cluster; only gap is that it never says whether AirPlay is currently on.

**Cross-page patterns (one fix, six pages)**

- **`.disclosure` is implemented three times in this cluster alone** — `wifi.css:229`, `google.css:200`, `airplay.css:73` — with three body class names (`.disclosure-body` twice, `.disclosure__body` once) and three different chevrons. `app.css` has no disclosure primitive. Nine page stylesheets repo-wide carry a copy.
- **Four badge vocabularies**: `app.css:446` `.badge` (used correctly with inline `--tone` by spotify/google), plus `bluetooth.css:121` `.device .badge` and `spotify.css:142` `.health-badge`, each with its own geometry and casing.
- **The same two widgets, implemented twice, differently**: the `data-confirm` submit guard (`google/js/main.js:28-42` document-delegated vs `spotify/js/main.js:146-160` per-form) and copy-to-clipboard (`data-copy` + `data-copy-feedback` id vs `data-copy-target` + sibling span). Both also exist in `home-assistant`, `voice`, `sound-profile`. Neither is in `shared/js/`.
- **Three hand-rolled secret masks** (`spotify_setup.py:425`, `google_setup.py:474,561`) while `_common.mask_secret()` exists and three other wizards use it. Otherwise **secret handling across the cluster is sound** — PSKs never round-trip to the browser, the Google Client Secret is a password field never re-rendered, and no page prints a token.
- **Off-ladder type on all six pages**: 18px, 17px, 10px, 0.9rem, 0.85rem, 0.72rem, 0.7rem, and twelve `em` steps in `google.css`.
- **Legacy `./?msg=` redirects** in `/spotify/` (~40) and `/google/` (~19), round-tripped through `_common.redirect_with_legacy_msg` (`_common.py:1229`); the other four pages use `send_see_other(flash=…)`.
- **Set-up vs already-set-up is handled well**: `/wifi/`, `/bluetooth/`, `/google/` and `/spotify/` all lead with status once configured, and the two OAuth wizards each render a distinct no-credentials / no-accounts / management state. The gap is the inverse — the two pages that own on/off (`/sources/`, `/airplay/`) show no status of their own, and `/sources/` is a dead end.
- **Multi-step flows are the cluster's strength**: `/bluetooth/` pairing and `/google/` setup both have genuine progress, completion and error states. Nobody has invented stepper chrome for a page that isn't a flow.

**Estimated PR breakdown** (single-concern, < 400 changed lines each)

1. **Promote `.disclosure` and `.badge` into `app.css`; delete the per-page copies.** Pick one chevron/geometry, migrate `wifi.css` / `google.css` / `airplay.css` / `spotify.css` / `bluetooth.css` and their markup. Net deletion. — *Opus to choose the canonical shape, then mechanical.*
2. **`shared/js/confirm-forms.js` + `shared/js/copy.js`.** Extract the two duplicated widgets, settle on one attribute contract, delete the copies from `/spotify/` and `/google/`; note `home-assistant`/`voice`/`sound-profile` as a follow-up. — *Sonnet.*
3. **`/sources/` mechanical:** move `_PAGE_CSS` to `deploy/assets/sources/sources.css`, fix the error banner class (s-1), swap `style="display:none"` for `hidden` / `el.hidden`, drop the duplicated `code` rule. — *Sonnet.*
4. **`/sources/` IA:** cut the duplicated USB copy (s-2), settle the page's name (s-3), add per-source chevron links respecting the 44px pitch (s-4). — *Opus.*
5. **`/spotify/` management-page IA:** sp-1 + sp-2 + sp-3, and move `.page h2/h3` onto `.section__title` / `.eyebrow`. — *Opus.*
6. **`/google/`: single-source the setup guide.** g-1 + g-2, plus lifting the eight inline styles into `google.css`. — *Opus for the extraction.*
7. **`/wifi/`: fold Join-by-name behind disclosure, unify the lockout warning.** wifi-1 + wifi-2, and build the connect panel from `.field`. — *Opus (the degraded-scan pointer has to keep working).*
8. **`/bluetooth/` copy + weight:** bt-1 (cross-link), bt-2 (three pair-stage labels), bt-3 (ghost Forget — apply to `/wifi/` saved rows too), bt-4 (one line on Ready). — *Sonnet, with the copy supplied.*
9. **Retire `./?msg=`:** convert ~60 call sites in `/spotify/` + `/google/` to `send_see_other(..., flash=…)` and delete `redirect_with_legacy_msg` from `_common.py`. Pure deletion; may need splitting per page to stay under 400 lines. — *Sonnet.*
10. **(Optional) type-ladder sweep across the cluster.** Mechanical, but design-language §1 says the doc "is not a codebase-wide compliance program" — file only if the owner wants §3 pulled beyond the measurement flow. — *Sonnet.*

*Not a finding (protected page, `deploy/index.html`): the `/airplay/`, `/spotify/` and `/wifi/` landing rows carry hardcoded status strings ("Synced", "Household routing", "Network profiles") that never reflect actual state — unlike `#status-playback-source` and `#status-ha`, which are live. Recorded as information only; no change proposed.*

---

# Full per-page report

## /wifi/  —  "See what network the speaker is on, and change it"

Files: `jasper/web/wifi_setup.py` (1506; render `_landing_html` 1219–1293, 75 lines), `deploy/assets/wifi/wifi.css` (278), `deploy/assets/wifi/js/main.js` (681)

Primary user task(s): confirm the current network; switch to another; forget a saved one.

Screen inventory (top to bottom):
1. `.app-header` "Wi-Fi" + back chevron.
2. One-line intro hint (`wifi_setup.py:1238`).
3. **Current network card** (`#current`, JS-rendered). States: no-adapter / radio-off / not-connected / connected (SSID + signal bars, IP, security, signal) / backend-unreachable. Bottom row: **Wi-Fi radio** toggle.
4. Region head "Available networks" + **Scan** button (idle / `Scanning` with spinner / hidden when `scanHealth.hideScanButton`).
5. `#scan-health` note (empty / suspect / degraded-warn).
6. Available-network list. Empty states: "Tap Scan…" / "Scanning…" / "No other networks found" / "Scan degraded". Row tap → inline panel: risk warning, password + Show password, Connect / Cancel; in-flight spinner "(up to 3 minutes including rollback)"; result ok (auto-clears 3 s) or error + Dismiss.
7. Region head "Join by name" + always-open `.info-card`: Network name, Password + Show password, Hidden network checkbox, result slot, **Connect** (`btn--primary`).
8. `<details>` "Saved networks (N)" → rows with **Forget** (`btn--danger`) → inline confirm panel.

Verdict: **TIDY**

Findings:
- [M] wifi-1 — *Progressive disclosure / Von Restorff* — "Join by name" is a permanently expanded card with its own `btn--primary` Connect (`wifi_setup.py:1252-1276`), while a tapped scan row renders a second `btn--primary` Connect in its inline panel (`deploy/assets/wifi/js/main.js`, `openConnect`). Two identical primary CTAs on screen for the same verb, and the fallback path (documented as being for scan-suppressed radios and hidden SSIDs, `wifi_setup.py:9-11`) has equal weight to the main one. → Wrap it in the page's existing `details.disclosure`, opened by the module when `scanHealth.degraded` or `hasScanned && !scanResults.length` — the degraded copy already says "Join by name is available below", so the pointer survives. (S)
- [M] wifi-2 — *Consistency* — the same lockout decision is warned about in two different affordances: the list-row connect renders an inline `.warn` box (`main.js` `connectRiskWarningHtml`), the join-by-name path uses a blocking `jtsConfirm` (`main.js` `confirmManualLockoutRisk`). One risk, two mental models. → Use the modal for both and delete `connectRiskWarningHtml`; a warning the user can scroll past is not what this risk deserves. (S)

Hygiene:
- [L] `deploy/assets/wifi/wifi.css:58-60` `.current-card .ssid` is `18px` — a sixth type step (design-language §3 ladder is 11/12/13/14/16).
- [L] `wifi.css:136-160` `.panel label` and `.panel input[type=password|text]` re-implement `app.css`'s `.field > label` / `.field input` verbatim. → build the panel from `.field`.
- [L] `main.js` `renderCurrent()` rebuilds the whole card's `innerHTML` on every 7 s poll, destroying and re-binding the radio toggle twice a minute. → render once, then update text nodes and `input.checked` in place.
- [L] 681-line single module, entirely `innerHTML` string concatenation (safe — every interpolation goes through `escapeHtml`/`cssIdSafe`) where `shared/js/dom.js` `h()` exists precisely to remove that class of risk. Not worth a rewrite on its own; worth it if the file is opened for wifi-1.

---

## /bluetooth/  —  "Turn Bluetooth on, put the speaker in pairing mode, manage devices"

Files: `jasper/web/bluetooth_setup.py` (1368; render `_landing_html` 454–516, 63 lines), `deploy/assets/bluetooth/bluetooth.css` (212), `deploy/assets/bluetooth/js/main.js` (832), `.../js/scan.js` (33)

Primary user task(s): pair a phone or a remote; reconnect/disconnect/forget a paired device.

Screen inventory:
1. Header "Bluetooth", intro line (`bluetooth_setup.py:465-466`).
2. Adapter card: **Bluetooth** toggle + live hint (loading / parked / unavailable / degraded / "Off — turn Bluetooth on…" / "On — adapter hci0" / "· scanning…"); **Pairing mode** toggle + static hint ("Auto-turns off after 5 min" — matches `DISCOVERABLE_AUTO_OFF_SEC = 300`).
3. "My devices" list: icon, name, badge (Connected / Not connected / Connecting / Pair required / in-flight label), Battery + Signal metrics, Connect|Disconnect + Forget. Empty: "No paired devices yet."
4. "Other devices" head + **Scan** button; nearby list with **Pair** (+ Remove for stale BlueZ records). Empty: "Nothing nearby. Try scanning."
5. Footnote about pairing surviving Bluetooth-off.
   Pair flow states: inline `.pair-card` with staged progress → `✓ Ready` (auto-hides 4 s) or error head + message (auto-hides 8 s). Device mutations stream terminal state and surface failure / unknown-outcome via `jtsAlert`.

Verdict: **KEEP** (the strongest flow in the cluster — real progress, real terminal states, real disabled logic, real destructive confirms)

Findings:
- [M] bt-1 — *Consistency / one setting, two homes* — the master **Bluetooth** toggle (`bluetooth_setup.py:470-477`) writes the same persisted source intent as the **Bluetooth** row on `/sources/`: `request_source_intent(Source.BLUETOOTH, on)` at `bluetooth_setup.py:700` vs `sources_setup.py:634`. Neither page mentions the other. `/spotify/` handles the identical situation by saying where on/off lives (`spotify_setup.py:250-252`). → Keep the toggle (it is the precondition for everything else on the page) and add the same one-line pointer to `/sources/`. (S)
- [M] bt-2 — *Naming (design-language §11: name what people control, not how it's built)* — the pair progress exposes BlueZ internals: `Trusting…` (`js/main.js:701`) and a `wiring` stage whose copy is "Requested an accessory profile refresh." (`jasper/bluetooth/engine.py:591`). Five stage names for one tap, two of them meaningless to the user. → Map the engine's stage vocabulary onto three user-facing labels in `renderPairStage` — Pairing… → Connecting… → Ready. Server stages unchanged. (S)
- [M] bt-3 — *Von Restorff / Fitts* — every row in "My devices" pairs a solid Connect/Disconnect with a solid red `btn--danger` **Forget** (`js/main.js`, `deviceRow`), so the destructive action is the loudest element in the list and sits directly under the thumb. `/wifi/` saved rows do the same. → `btn--ghost` for Forget; the `jtsConfirm` already carries the danger styling. (S)
- [L] bt-4 — *Peak-end* — the pair card ends on `✓ Ready` then erases itself after 4 s (`js/main.js:664-668`) without saying what to do next (select JTS on the phone / that the device now appears under "My devices"). → one line of copy in the ready stage. (S)

Hygiene:
- [L] `bluetooth.css:121-135` `.device .badge` re-declares `app.css`'s `.badge` (`app.css:446`) with different geometry, no uppercase and no ring. Third variant is `spotify.css:142`.
- [L] `bluetooth.css:207-212` device-class icons are emoji `content:` glyphs while the rest of the UI draws from the `#icon-*` SVG sprite (which already has `#icon-bluetooth`).
- [L] Off-ladder `rem` sizes throughout: `0.85rem`, `0.72rem`, `0.7rem`, `0.95rem` (`bluetooth.css:24,48,109,125,153,160`).

---

## /airplay/  —  "Choose how the AirPlay receiver handles clock drift"

Files: `jasper/web/airplay_setup.py` (252; render `_index_html` 105–185, 81 lines), `deploy/assets/airplay/airplay.css` (126). No JS.

Primary user task(s): flip to free-running if a DAC swap causes glitches. That's it.

Screen inventory:
1. Header "AirPlay sync mode".
2. Flash banner (post-save).
3. One-line framing hint.
4. Radio group, current mode preselected: **Synced** `default` / **Free-running** `fallback`, each `.choice` card with a use-case hint.
5. **Save AirPlay mode** (single `btn--primary`).
6. `<details>` "Why this knob exists" — the shairport/snd-aloop/resync-threshold history.

Verdict: **KEEP**. Right-sized: one purpose, one primary action, sensible default preselected, implementation detail correctly behind disclosure. Best Python file and best CSS in the cluster.

Findings:
- [L] ap-1 — *Status before action* — the page never states whether AirPlay is currently enabled (that toggle lives on `/sources/`) or what name the speaker advertises. Not misleading — the page title and the landing row both say "AirPlay sync" — but it is the only page here with no status line at all. → one line: current mode + "AirPlay is on/off — change that on Sources". Pairs with s-4 below. (S)

Hygiene:
- [L] `airplay.css:73-125` is a third, independent `.disclosure` implementation (see cross-page).
- [L] `.choice__tag` is `10px` (below the ladder's floor); `var(--font-mono, …)` at `airplay.css:115` references a token defined nowhere.
- Note: `.choice` (`airplay.css:23-70`) is a well-built radio-card primitive. `/spotify/`'s `.mode-picker` is a worse re-invention of exactly it.

---

## /spotify/  —  "Link household Spotify accounts for voice playback"

Files: `jasper/web/spotify_setup.py` (1482; `_management_html` 719–785, `_account_card_html` 622–682, `_setup_wizard_html` 315–354), `deploy/assets/spotify/spotify.css` (253), `deploy/assets/spotify/js/main.js` (165)

Primary user task(s): first run — create a developer app and paste a Client ID; steady state — add/remove accounts, set the default, re-link a signed-out account, manage hidden playlists.

Screen inventory — five states, all sharing `_spotify_page` chrome (header, flash banner, disambiguation banner, `<main>`, module):
1. **State 1 (no creds):** Step 1 dashboard link → Step 2 bounce/manual `.mode-picker` → Step 3 Client ID field → Save and continue.
2. **State 2 (creds, no accounts):** "Credentials saved (…)" line → redirect-URI copy row + 3-step instructions + dashboard link → Add an account form → Reset credentials (`btn--danger`).
3. **Manual pre-warn:** "What's going to happen" 5-step callout → Open Spotify Authorization → paste-URL textarea → Finish connecting → Cancel link.
4. **State 3 (management):** disambiguation banner → intro hint → "Voice tools" info-card → `<h2>` Accounts → hint → per-account `<details>` (name, `default` badge, health badge, playlist count; body: relink notice when revoked, Set default / Remove account, custom playlists with live-preview add) → Add an account → "Voice-start playback setup" disclosure → "Spotify app settings" disclosure (mode note, redirect-URI block again, paste fallback, Reset credentials).
5. OAuth callback → flash.

Verdict: **RESTRUCTURE** (state 3 only; the setup path itself is good)

Findings:
- [M] sp-1 — *Serial position / Prägnanz* — the management page puts four prose blocks between the header and the first account: the always-on disambiguation banner (defined `spotify_setup.py:248-254`, injected for **every** state at `:274`), an intro hint (`:741`), a "Voice tools" info-card (`:743-746`), and a second hint (`:749-750`). The thing you came for — the account list — is fourth. → Render the disambiguation note in the setup states only; move the Tools link into the existing "Spotify app settings" disclosure; collapse the two hints into one lead line. Net words down. (M)
- [M] sp-2 — *Prevent errors* — the OAuth-mode radio group is rendered **outside** the credentials form (`_mode_picker_html` at `:286`, called at `:325`) and the form carries a hidden `<input name="mode" value="bounce">` (`:329`) that JS mirrors into (`deploy/assets/spotify/js/main.js:31-40`). With the module unavailable the picker is inert and the mode silently saves as `bounce` — while the module's own header claims "All of it degrades gracefully". → Move `.mode-picker` inside the `<form>`, delete the hidden input and the mirror block; keep the JS only for the `.selected` highlight. (S)
- [M] sp-3 — *Fewest steps* — the only documented way to change OAuth mode is "reset credentials and choose the other mode when re-pasting your Client ID" (`:754-756`) — a destructive round trip to change a radio button, when `setup-credentials` already accepts `mode` and the picker markup already exists. → render `_mode_picker_html(selected=mode)` + Save inside the settings disclosure. (S)
- [L] sp-4 — *Duplication / consistency* — Client ID masking is hand-rolled at `:425` (`client_id[:4] + "…" + client_id[-4:]`) while `_common.mask_secret()` (`_common.py:1254`) exists and is used by `transit_setup.py:840`, `voice_setup.py:869`, `home_assistant_setup.py:922`; the same page then interpolates the Client ID unmasked into the dashboard link (`:359`). (S)

Hygiene:
- [M] `spotify.css:25-38` styles bare `.page h2` at **17px** and `.page h3` — an off-ladder size and an element-selector heading vocabulary. Every other page in the cluster uses `.eyebrow` / `.section__title`. This is the single biggest visual-consistency gap in the cluster.
- [M] `spotify.css:52-61`, `183-192`, `214-226` — `.copy-row input`, `.pl-input` and `.paste` each re-implement `app.css`'s `.field input` treatment (padding/radius/border/background), three times in one file.
- [L] `spotify.css:142-148` `.health-badge` is a third `.badge` variant (see cross-page).
- [L] ~40 `self._redirect("./?msg=…")` call sites (`:1077-1339`) round-tripped through the `redirect_with_legacy_msg` compatibility shim (`_common.py:1229`), whose own docstring calls the pattern legacy.

---

## /google/  —  "Link Google accounts so JTS can answer Calendar and Gmail questions"

Files: `jasper/web/google_setup.py` (1152; `_setup_wizard_body` 217–406 = **190 lines**, `_connection_details_html` 458–516, `_management_html` 625–679), `deploy/assets/google/google.css` (264), `deploy/assets/google/js/main.js` (197)

Primary user task(s): first run — create a Cloud OAuth client and paste ID + Secret; steady state — link/remove household members, set the default.

Screen inventory:
1. **State 1:** Reset progress button → intro → 4-step `<details>` walkthrough (each: numbered badge, title, time estimate, instructions, callouts, "I've done this →"; step 4 carries the redirect-URI copy row and the Client ID / Client Secret form). localStorage tracks done steps, collapses them, auto-opens the next.
2. **State 2:** "Credentials saved (Client ID: …). One step left" → Add a Google account → Connection details disclosure → OAuth-client-troubleshooting disclosure (redirect URI + Reset).
3. **State 3:** intro → `.section__title` "Linked accounts" → `<ul>` rows (name, email, `default` badge, Set default / Remove) → "Add another household member" hint → Add form → Connection details disclosure → **Google Cloud setup guide** disclosure (the whole 190-line wizard again, read-only) → OAuth client settings disclosure (redirect URI + Reset credentials).

Verdict: **TIDY** (the IA is the better of the two OAuth wizards; the problems are structural/hygiene)

Findings:
- [M] g-1 — *Duplication / page weight* — `_setup_wizard_body` (`:217-406`) is rendered twice: as state 1, and again read-only inside a management-page disclosure (`:660-664`). Two rendering modes are threaded through five conditional locals (`:245-269`) inside one 190-line f-string, and every management-page load ships the complete Cloud-Console walkthrough. → Either link the guide (state-1 render behind `?guide=1`) or lift the four steps into a small data structure rendered by one loop, so the read-only variant is a flag on the renderer, not a second copy of the markup. (M)
- [M] g-2 — *State leak / Doherty* — `initWizard()` runs unconditionally on every `/google/` page (`deploy/assets/google/js/main.js:177`). On the management page it finds the read-only guide's `li.wizard-step` elements, paints done/active styling from setup-time localStorage, and force-opens the "first not-done" step inside a collapsed disclosure. The module's own comment concedes this. → gate on a `data-wizard="active"` attribute the read-only render omits. (S)
- [L] g-3 — same hand-rolled masking as sp-4, twice (`:474-477`, `:561-564`), while `mask_secret()` exists. (S)

Hygiene:
- [M] Eight inline `style="…"` attributes carrying layout out of the stylesheet: `:250`, `:260`, `:429`, `:508`, `:570`, `:641`, `:668` — spacing values (`0.3em`/`0.6em`/`1.6em`/`2em`) and a button padding override that exist nowhere in `google.css`. (`style="--tone: …"` on `.badge` at `:593` is the sanctioned pattern and is fine.)
- [M] `google.css` uses **12 distinct `em` font sizes** — 0.78/0.82/0.85/0.9/0.92/0.96/1.04 at `:38,81,89,103,126,141,166,184,196,221,240,249`. Design-language §3 exempts `em` that tracks a prose parent; a dozen values across a stylesheet is the drift it warns about.
- [L] `google.css:200-226` — second `.disclosure` implementation.
- [L] ~19 `./?msg=…` redirects (`:803-1021`) on the legacy shim.

Good, worth preserving: the Client Secret is a `type="password"` field, written to `/var/lib/jasper-secrets/`, and never re-rendered — the page offers rotation-then-reset instead of a reveal, and the copy says so (`:517`).

---

## /sources/  —  "Turn each playback source on or off"

Files: `jasper/web/sources_setup.py` (825; `_index_html` 604–689, 86 lines; inline `_PAGE_CSS` 562–578), `deploy/assets/sources/js/main.js` (282). **No stylesheet file.**

Primary user task(s): enable/disable AirPlay, Bluetooth, Spotify Connect, USB Audio Input.

Screen inventory:
1. Header "Music sources".
2. Flash banner.
3. Five-line intro hint (`:672-677`).
4. `.info-card` titled "Sources" containing, in order: hidden state-error alert, hidden stereo-pair note, then four `.source-row`s (name + notes left, toggle right). Row states: on / off / disabled-because-unavailable / degraded-with-reason text. The USB row carries a permanently visible 7-line note.

Verdict: **TIDY**

Findings:
- [M] s-1 — *Feedback / silent failure* — the state-read error banner is `class="info-card info-card--danger"` (`:617`), but `.info-card--danger` is **defined nowhere in the tree** — `grep -rn info-card--danger deploy/ jasper/` returns only that line. The one element on the page that must read as an alert paints as a neutral info card. → Use `class="banner banner--danger"` (`app.css:~600`), the canonical severity vocabulary the flash banner already uses. (S)
- [M] s-2 — *Prägnanz / §11 net-word-count* — the intro (`:672-677`) explains USB Audio Input in full, and the USB row note (`:649-660`) explains it again at greater length, including a parenthetical about the USB management-network path that has nothing to do with this toggle. → Intro becomes one line ("Turn each playback source on or off. Choices persist across reboots."); the USB row keeps a short note; the management-network aside goes. (S)
- [M] s-3 — *Jakob / §11 an action keeps its name* — one page, three names: `<h1>` "Music sources" (`:669`), landing row "Playback sources" (`deploy/index.html:607`), and `/spotify/`'s pointer calls it "the Sources page" (`spotify_setup.py:252`). → pick one and use it everywhere. (S)
- [M] s-4 — *Fewest steps / Fitts* — `/sources/` fronts four sources but no row links to that source's own page. `/bluetooth/` and `/spotify/` point at `/sources/`; nothing points back. Turning Bluetooth on here leaves the user with no path to pairing. → Give each row a chevron link (Bluetooth → `/bluetooth/`, Spotify Connect → `/spotify/`, AirPlay → `/airplay/`), matching the landing page's `.setting-row` shape. Keep the toggle as the row's own control. (M — needs care with the 44px tap-target rule: `app.css`'s toggle comment notes `/sources/` is already the tightest shipped case at 53.8px centre-to-centre.)

Hygiene:
- [M] The only page in the cluster still using inline `page_css` (`:562-578`, passed at `:688`) rather than a static `page_css_href` file. Inside it, `.source-note code` is a verbatim copy of `app.css`'s `.form-hint code`, and `.source-note` is `0.9rem` (14.4px, off ladder). → move to `deploy/assets/sources/sources.css`, drop the duplicated `code` rule.
- [L] Six `style="display:none"` attributes (`:610,618,627,634,642,663`) driven by `style.display` assignments in `js/main.js`, when `app.css` already ships `[hidden]{display:none!important}`.
- [L] `<h2 class="section__title">Sources</h2>` inside a page whose `<h1>` is "Music sources" — the card heading restates the page title.

Good, worth preserving: `js/main.js`'s failure taxonomy (persisted-but-unreconciled vs rejected-before-commit vs lost-response) is careful and never lies to the user with a fake rollback. Don't simplify that away.
