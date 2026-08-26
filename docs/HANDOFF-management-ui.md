# HANDOFF — Management UI

Canonical for the `jts.local` management surface: the design system every
wizard renders through, the rules for migrating a page onto it, the surface
inventory, and the anti-patterns worth resisting. Visual tokens and component
styles are owned by [design-language.md](design-language.md) and
`deploy/assets/app.css`; the 2026-05 redesign proposal and the competitor/UX
research behind it are archived in
[historical/management-ui-redesign-2026-05.md](historical/management-ui-redesign-2026-05.md).

## One frontend, gated by capability

The Zero-class `streambox` and full-speaker profiles use **one** landing page,
one design system, and one card vocabulary; differences are capability gates,
never a second frontend —
[ADR-0120](adr/0120-one-management-frontend-gated-by-capability.md).
`jasper.install_profile.system_capabilities_for_profile` is the single source of
truth (`local_sources`, `content_dsp`, `voice_brain`, `network_settings`,
`speaker_settings`, `pair_management`, `developer_tools`), feeding both
`jasper-control`'s `/system/snapshot.system_capabilities` and `install.sh`,
which bakes the map into `deploy/index.html` at install time. The page applies
it **synchronously at first paint**; `/system/data.json` refreshes live values
only and never drives layout. Gates fail closed — every gated card ships
`hidden`.

The shared frontend rule does **not** extend to systemd activation. Full
speakers install `jasper-web.service` + `.socket`; streamboxes install
`jasper-web-streambox.service` + `.socket` under the same runtime unit names, so
a streambox never binds voice/wake/integration ports or sources assistant-only
env files. A box bonded as a multiroom **follower** behaves like the old
"endpoint" at runtime — grouping lands its role and data plane, the source
coordinator parks its renderer stack, grouping derives the voice-park flag — but
that is a runtime role, not an install tier; it still serves the shared page
gated by its own capabilities. The legacy `endpoint` / `satellite` tokens
normalise to `streambox`. Zero-2-W bring-up:
[dumb-endpoint-bringup.md](dumb-endpoint-bringup.md).

## Rendering a page: the canonical system

Every server-rendered wizard renders through `canonical_page()` +
`/assets/app.css`, with behaviour delivered as static ES modules under
`deploy/assets/<page>/js/` — **no inline `<script>`**. Shared primitives live in
[`jasper/web/_common.py`](../jasper/web/_common.py):

- **`canonical_header(title, *, back_href="/", back_label="Home",
  right_html="")`** → the `.app-header` sticky bar. Single source of truth for
  the back button (`#icon-back` sprite symbol), centred title, and an optional
  right slot (default an empty `<span>` so the 3-column grid stays balanced).
  `title`/`back_href`/`back_label` are escaped; **`right_html` is
  caller-trusted** — escape untrusted strings before passing.
- **`canonical_banner(message)`** → the `.banner` flash, with severity derived
  from the string so the same message reads the same everywhere: contains
  "error"/"fail" → `banner--danger`; starts with "saved"/"cleared" →
  `banner--ok`; else `banner--info`. Blank → `""`, so it can drop into a body
  unconditionally.
- **Switches reuse `toggle_html()`** — there is no canonical switch helper. The
  `.toggle` CSS in `app.css` styles the same native-checkbox markup
  `toggle_html()` already emits.

`app.css` carries the shared form vocabulary: `.field` (a labelled stack whose
`<label>` is the EYEBROW tier), themed text inputs and `select`/`textarea`
(the management UI intentionally suppresses browser focus outlines),
`.form-actions`, `.form-hint`; `.banner` + its three tones, driven by the same
`--status-*` tokens as the rest of the UI; and the `.toggle` / `.track`
vocabulary with checked, `:disabled`, and `prefers-reduced-motion` states.

Two cross-page ES modules:

- [`shared/js/dialog.js`](../deploy/assets/shared/js/dialog.js) —
  `jtsConfirm`/`jtsAlert`, Promise-based, styled by `.jts-dialog`. It replaces
  `window.confirm`/`alert`, which the browser can suppress ("prevent this page
  from creating more dialogs") — that suppression silently defeated `/system/`'s
  restart and reboot guards. `_common.py` ships no legacy inline dialog copy.
- [`shared/js/http.js`](../deploy/assets/shared/js/http.js) — `csrfHeaders`,
  `jsonHeaders`, `getJSON`, `postJSON`, `postControlAction`. CSRF-aware fetch
  helpers that read the token from the `<meta name="jts-csrf">` tag **at call
  time**, so the cacheable module bakes in no secret. It also exports two 403
  classifiers so a caller can tell one rejection from another:
  `isControlTokenRequired(err)` (a JSON `control_token_required` body → prompt
  for the token and retry once) and `isStaleSessionRejection(err)` (a non-JSON
  403, which is what `reject_csrf`/`guard_mutating_host` answer with → show the
  stale-page copy and reload). Import by absolute path
  (`/assets/shared/js/http.js`).

**Adding a page needs no `install.sh` edit.** The asset-copy step discovers
every directory under `deploy/assets/` (each page slug plus `shared`; `fonts`
are copied separately) and copies the same per-dir shape. This closed the silent
404 where a new page's CSS/JS never reached the Pi. `jasper-doctor`'s
`check_web_design_assets` keys off a fixed required-file list, so new shared
modules do not trip a false warning.

**`/assets/` is served from both nginx server blocks.** The port-80 block is
obvious; **the 443 block needs its own copy** because the correction measurement
pages — served over HTTPS when `getUserMedia` needs a secure context — link
`/assets/app.css` and their ES modules by absolute path. Without a 443
`/assets/` location those subresources fall through to the HTTP-downgrade
catch-all, redirect to `http://`, and are blocked as mixed content, leaving the
measurement UI unstyled with dead mic-capture JS. Keep the two blocks' caching
identical; `test_nginx_serves_assets_over_https_no_mixed_content` in
[`tests/test_landing_page_html.py`](../tests/test_landing_page_html.py) pins it.

**The plain-HTTP correction preflight is canonical too.** `/correction/` is two
surfaces on one path: a static preflight
([`deploy/correction-preflight.html`](../deploy/correction-preflight.html))
explaining the HTTPS switch, then the HTTPS measurement UI. Being static (nginx
`try_files`, no Python) it cannot call `canonical_page()`; it links
`/assets/app.css?v=__APP_CSS_VERSION__` directly and `install.sh` stamps the
build SHA exactly as it does for `deploy/index.html`, inlining only the one
`#icon-back` sprite symbol it needs. Its Proceed button targets
`/correction/proceed` with a build-token fallback query string that JavaScript
replaces with a fresh `jts_cb` token per load; nginx temporarily redirects that
to `https://$host/correction/` with `Cache-Control: no-store` and preserved
query args, so non-default hostnames like `jts3.local` do not depend on
client-side JS to survive the HTTP → HTTPS hop and mobile browsers cannot cache
a stale hostname or scheme rule. The static preflight's allowlist stays closed
to `/correction/*`; the canonical Room page uses nginx's `/sound/proceed/room`
handoff instead.

### Restyle-in-place: the rule for migrating a page

`/system/` and the two Sound page modes were built as client-side render
targets. Most wizards do not need that — they are plain server-rendered forms,
and the cheap path is a **restyle-in-place**: keep the form and its POST
request/response flow, swap only the document wrapper and CSS classes, move any
inline `<script>` into an ES module. `/speaker/`
([`jasper/web/speaker_setup.py`](../jasper/web/speaker_setup.py)) is the
reference migration.

Concretely: render with `canonical_page()`, build the body from the shared
helpers, keep `csrf_field_html()` inside the `<form>`, preserve the existing
routes, save handler, and flash, and extract inline JS to
`deploy/assets/<page>/js/main.js`. **Reuse the shared layer; do not re-declare
chrome that already exists.**

### Archetype recipes

Each names the exact canonical classes; all wrap the body in
`canonical_header(title)` + `canonical_banner(flash)` + `<main class="page">`.

- **Settings form** (`/speaker/`, `/weather/`, `/airplay/`): one
  `<form method="post" action="./save">` carrying `csrf_field_html()`; each
  input a `.field` (EYEBROW `<label>` + themed control + `.form-hint`); close
  with a `.form-actions` row holding a `.btn.btn--primary`. A destructive
  secondary action is `.btn--ghost` or `.btn--danger` in the same row.
- **Toggle list** (`/sources/`, `/wake/` advanced fusion): an `.info-card` (or
  one per group) of "label + `toggle_html(...)`" rows laid out with
  `.control-head`/flex; the checkbox POSTs or fetches via `postJSON`. One
  `.section__title` per card; row labels stay EYEBROW.
- **Integration card** (`/ha/`, `/google/`, `/spotify/`): one `.info-card` whose
  body switches on connection state — not configured (a `.form-hint` explainer +
  a primary "Find…"/"Connect"), connect/paste (a `.field` + a `.form-actions`
  submit), connected (a `.deflist` of status rows + a `.badge` toned
  `--status-ok` + a ghost disconnect). Soft-unlock and error notices use
  `canonical_banner`.
- **Provider cards** (`/voice/`): a stack of `.info-card`s, one per provider,
  each with a `.section__title`, a `.field` API-key input, and `.field`
  `<select>`s for model and voice; one radio group at the top chooses the active
  provider; one `.form-actions` submit saves all. Pricing goes in `.form-hint`.
- **Scan + connect list** (`/wifi/`, `/bluetooth/`): a current-state
  `.info-card` on top (`.deflist` + `.badge`); a ghost scan button that fetches
  via `getJSON` and renders result rows; a "join by name" `.field` fallback
  form; a collapsible saved list with per-row `.btn--danger` forget. **Escape
  every device-provided name** before `innerHTML`, pass connect/forget targets
  via escaped `data-*` plus a delegated handler (never inline JS), and confirm
  destructive actions with `jtsConfirm(msg, {danger:true})`.

### Typographic grammar (three tiers)

Semantic levels use different *combinations* of type axes so hierarchy reads
without parsing the words:

| Tier | Element | Style | Examples |
|---|---|---|---|
| Region header | labels a region with no card chrome | EYEBROW — `.eyebrow` (font-display, 11px, 600, uppercase, muted) | "Rooms" on `/`; "Per-service usage" on `/system/` |
| Card title | names a contained panel | cased display — `.section__title` (font-display, 14px, 600, tracking-tight) | "Software", "Voice spend cap", "AirPlay" |
| Row label | a field label inside a card | EYEBROW — `.deflist dt` | "Version", "Branch", "Uptime" |
| Value | data / content | plain — `.deflist dd` (normal weight, `tabular-nums`) | "13a8d65-dirty", "4h ago" |

Uppercase plus tracking is a wayfinding tool — scan without reading — which
suits region headers and field labels but fails for object names, because it
strips the word-shape recognition that aids reading. Cased display names an
object you read once to orient; values are content, so plain weight. **The
consistency across pages is the grammar** (does this element label a region,
name a card, or label a field?), not a shared class — which is why `/`'s eyebrow
region headers and `/system/`'s cased card titles coexist correctly. Stat-tile
labels ("MEMORY", "CPU USAGE") stay EYEBROW: they are field labels.

### Tracked follow-up — split `/sound/`'s JS (hardware-gated)

`/system/`'s behaviour is split into layered ES modules
(`dom`/`format`/`charts`/`components`/`sections`/`views`/`api`/`actions`/`main`).
`/sound/` is still mostly one module, relocated verbatim from the old inline
`_SOUND_JS`. Two pure DOM-free pieces are carved out — RBJ biquad math
([`eq-math.js`](../deploy/assets/sound-profile/js/eq-math.js), shared with a node
parity check and mirrored in Python) and active-speaker vocabulary/step policy
([`active-speaker-ui.js`](../deploy/assets/sound-profile/js/active-speaker-ui.js)).
Splitting the rest was **deliberately deferred, not blind-refactored**: the
editor's ~25 mutable state vars are woven through its math, `innerHTML`
rendering, and live-draft IO, and the live-draft path coordinates rapid edits to
CamillaDSP via debounce and sequence guards whose correctness *and audio effect*
can only be verified on the Pi. Do it as a focused change with hardware in the
loop (deploy → exercise Off/Saved/Draft, band add/drag/delete, live draft,
save/rename → confirm audio and zero console errors), then merge.

## The surfaces

`/` is static HTML under `deploy/`, served by nginx. Its rows are grouped into
labelled `.eyebrow` sections — Sources, Sound, Assistant, Integrations, Network,
System — with a top control card carrying the **volume slider**, **mic toggle**,
and a lightweight **source selector** (the three "every visit" controls). The
selector posts to `jasper-control`'s `/source/*` routes and is distinct from the
`/sources/` on/off wizard. `deploy/index.html` is the source of truth for the
exact rows. Integrations is an inline section; there is **no** `/integrations`
page (removed 2026-05-31 with nothing linking to it).

Lifecycle switches on `/sources/` represent persisted **desired** state, not a
best-effort process probe; their status copy separately renders effective `on`,
`off`, `degraded`, `parked`, or `unavailable`. The state and convergence
contract is canonical in
[HANDOFF-source-lifecycle.md](HANDOFF-source-lifecycle.md); only the
presentation rule lives here.

`/system/` and `/system/audio/` are two addressable views of one Status
document. The segmented header keeps real links (`aria-current="page"`) so
direct navigation, modified clicks, and open-in-new-tab work, while ordinary
clicks switch lazy retained panels through the History API. **One** polling loop
updates only the active panel from the latest cached `/system/data.json`
snapshot, so changing views neither reloads the document nor creates a second
sampler. The System view is the host/operator surface (an Audio alert card that
appears only while `audio_health.overall.status` is `issue`, metric tiles,
software, Home Assistant status, network, actions, diagnostics, per-service
usage); the Audio view is the single household-facing audio-health surface. **The
browser never authors health prose or infers health from raw counters** — it
renders the normalized `/system/snapshot.audio_health` contract, each sentence of
which has exactly one writer in
[`jasper/control/audio_health.py`](../jasper/control/audio_health.py), and fails
soft to an explicit unavailable card when the block is missing. The System view's
opt-in USB forensics card consumes the same snapshot; its device behaviour and
resource bounds are canonical in
[HANDOFF-usb-gadget.md](HANDOFF-usb-gadget.md#opt-in-rolling-usb-forensics).

Wizard surfaces under `jasper/web/` are stdlib `http.server` modules, mostly
socket-activated and LAN-only. Some run inside the combined `jasper-web`
process; older or heavier surfaces (`/bluetooth/`, `/system/`, `/chat/`, the
correction measurement routes) keep their own service/socket wrappers.

| Path | Module | Port |
|---|---|---|
| `/spotify/` | `spotify_setup.py` | 8765 |
| `/voice/` | `voice_setup.py` | 8767 |
| `/google/` | `google_setup.py` | 8768 |
| `/bluetooth/` | `bluetooth_setup.py` | 8769 |
| `/sound/room/`, `/sound/crossover/`, `/sound/bass/` (`/correction/*` aliases) | `correction_setup.py` | 8770 |
| `/airplay/` | `airplay_setup.py` | 8771 |
| `/system/`, `/system/audio/` | `system_setup.py` | 8772 |
| `/sources/` | `sources_setup.py` | 8773 |
| `/wake/` | `wake_setup.py` | 8774 |
| `/wifi/` | `wifi_setup.py` | 8775 |
| `/transit/` | `transit_setup.py` | 8777 |
| `/ha/` | `home_assistant_setup.py` | 8778 |
| `/weather/` | `weather_setup.py` | 8779 |
| `/wake-corpus/` | `wake_corpus_setup.py` | 8782 |
| `/speaker/` | `speaker_setup.py` | 8783 |
| `/eq/`, `/sound/setup/` (`/sound/*` APIs) | `sound_setup.py` | 8784 |
| `/rooms/` | `rooms_setup.py` | 8785 |
| `/tools/` | `tools_setup.py` | 8786 |
| `/chat/` | `chat_setup.py` | 8787 |

`GET /volume`, `/mic`, `/source`, and `/grouping` are GET-only exact-match
same-origin proxies into `jasper-control`. **CamillaGUI binds loopback-only**
(`127.0.0.1:5005`, was `0.0.0.0:5005` — [#2319](https://github.com/jaspercurry/JTS/issues/2319)):
the unauthenticated, root-backed GUI can author and live-apply CamillaDSP
configs naming any device, so it is no longer LAN-reachable. The landing page's
Advanced DSP row went with it, because a household-facing link that always
connection-refuses is a silent failure. Reach it with
`ssh -L 5005:localhost:5005 <pi-host>`; `jasper-doctor`'s "CamillaGUI socket
bind" check pins the posture.

## What NOT to do

Anti-patterns easy to default to that the 2026-05 research surfaced as mistakes.

1. **No tab bar.** Tabs suit ~3-5 equal-weight modes; this surface is
   settings-dominant, so tabs would hide most of it. Vertical sections scroll.
2. **Don't try to be the Sonos app.** This is an admin page (the Plex/eero
   archetype), not a consumer remote. Phones already do play/skip/queue better.
3. **Don't gate the page behind setup.** Even pre-setup, volume and mic mute
   must work. Settings is a tool, not a gate.
4. **Don't auto-show a setup wizard for returning users.** Persistent banner is
   a light touch; auto-modal is friction. Once dismissed, stay dismissed.
5. **Don't put state behind an extra click.** Showing `Gemini · Aoede` on the
   row is the point. Clicking in to see a setting means the IA is wrong.
6. **Don't merge `/system/` into `/`.** Different modes: `/` configures the
   thing, `/system/` monitors and fixes it.
7. **Don't add a third level of disclosure.** `/` → a wizard is the budget.
   Wanting a sub-wizard or a tab inside a wizard means restructure instead.
8. **Don't write descriptions on the index.** If a row needs 20 words, they
   live on the destination. The index is for recognition, not learning.
9. **Use "Assistant", not "Voice & Skills".** If service connections outgrow
   that row, split to **Integrations**, not "Skills".
10. **Don't use green as the brand accent.** Green already means healthy,
    enabled, success, and Spotify. Keep semantic colours semantic.
11. **Don't make icons carry meaning alone.** Wi-Fi, Bluetooth, source,
    assistant, and integration icons are too ambiguous without text labels.
12. **Don't spend Pi budget on polish nobody needs.** No framework bundle, no
    runtime font request, no icon font, no client router, no animated gradients,
    no live charts on `/`. Elegance comes from hierarchy, spacing, type, and
    restraint. Local cacheable WOFF2 fonts with in-repo licences are fine.

Three principles those rest on, kept because they are the ones people
re-litigate: **state first, action second** (an admin page shows what *is*, then
offers what to change); **status text is a noun phrase, not a sentence**
(`Voice · Gemini · Aoede`, not "The voice provider is currently set to…"),
which compresses the page by roughly half without information loss; and
**reversibility beats discoverability** for admin actions — anything that drops
WiFi, restarts voice, or flips AEC confirms first. The full ten, with sources,
are in the
[archive](historical/management-ui-redesign-2026-05.md#2-grounding-principles).

Last verified: 2026-08-26 (triage pass — `canonical_page` / `canonical_header` /
`canonical_banner` / `toggle_html` / `csrf_field_html`, the two shared ES
modules, `system_capabilities_for_profile`, `check_web_design_assets`,
`test_nginx_serves_assets_over_https_no_mixed_content`, the preflight page, and
the `eq-math.js` / `active-speaker-ui.js` split rechecked against their owning
files. The 2026-05 redesign proposal, the pre-Phase-1 inventory, the phased
rollout, the open decisions, and the competitor/UX research moved to
`docs/historical/management-ui-redesign-2026-05.md`; the one-frontend rule
became ADR-0120.)
