# Management UI — redesign proposal + reference

**Status:** Reference · created 2026-05-22 · refreshed 2026-05-28.
Phase 1 IA/visual reshape implemented on 2026-05-28 in
`deploy/index.html`; the 2026-05-28 polish pass adopted the static reference
style, local Figtree/Outfit font assets, and a quieter one-column settings
surface. Setup wizard, conditional prompts, and fuller row-state hydration
remain future phases.

A research-grounded plan for restructuring the `jts.local` management surface
(today: volume/mic/source controls, 17 navigation rows on `/`, ~10 on
`/system/`, 18 dedicated setup/debug surfaces, plus CamillaGUI) into a tighter,
more navigable layout with a dismissible setup wizard for first-time
configuration and a quieter settings-list visual system.

Read this before extending the management UI redesign. The proposal and the
research that backs it both live here so you can re-justify design calls from
first principles instead of from memory. The current-state snapshot was refreshed
against `deploy/index.html`, `deploy/nginx-jasper.conf`,
`jasper/web/__main__.py`, and live `http://jts.local/` on 2026-05-28, but
re-inventory before implementation because this surface is still accruing
cards.

Implementation note (2026-05-28): §3 preserves the pre-Phase-1 inventory that
motivated the redesign. The current branch replaces the old flat card stack
with grouped settings rows while keeping the existing static landing-page
deployment model and the proven volume/mic/source JavaScript. Font polish is
served from local WOFF2 assets under `deploy/assets/fonts/`; do not reintroduce
runtime Google Fonts requests.

---

## Contents

1. [Why redesign](#1-why-redesign)
2. [Grounding principles](#2-grounding-principles)
3. [Current-state snapshot](#3-current-state-snapshot-as-of-2026-05-28)
4. [Proposal A — Information architecture](#4-proposal-a--information-architecture)
5. [Proposal B — Setup wizard](#5-proposal-b--setup-wizard)
6. [Proposal C — Copy revision](#6-proposal-c--copy-revision)
7. [What NOT to do](#7-what-not-to-do)
8. [Phased rollout](#8-phased-rollout)
9. [Open decisions](#9-open-decisions)
10. [Research foundation](#10-research-foundation)
11. [Appendix — when you're ready to build](#11-appendix--when-youre-ready-to-build)

---

## 1. Why redesign

The landing page has accumulated past its happy density. Four things
compound:

- **Flat hierarchy.** 17 navigation cards on one screen, visually equal, sorted by
  neither frequency nor topic. The user can't tell at a glance what's a
  daily knob (volume) vs. an annual setup step (room correction).
- **Verbose card copy.** Most descriptions run 1-2 sentences trying to
  explain the destination. They belong *inside* the destination, not on the
  index row. The current `Wake word ›`, `Transit ›`, and `Wake-word corpus
  recorder ›` cards are examples of the page trying to teach everything at
  once.
- **Cards are the wrong primitive.** A settings home page wants compact rows:
  icon, label, current state, chevron. Cards make every destination feel like
  a feature promo.
- **No setup gradient.** First-time and 50th-time visitors see the same
  page. There's no "you have 2 things left to set up" affordance and no
  linear path through the one-time stuff (voice provider, Spotify accounts,
  location, room correction).

What's *right* about today's page: the volume slider and mic toggle live on
the index. Those stay. The source selector has also earned its place as a
small "every visit" control. The rest of the load is ordering, naming, and
showing state without long descriptions.

---

## 2. Grounding principles

Each design call in the proposal traces back to one of these. They're
explicit so future-you can re-derive decisions instead of memorising them.

1. **State first, action second.** Admin pages live in a different
   register than consumer apps — the user is administering a thing. Show
   what *is*, then offer what to change. eero, UniFi, Synology, Plex all do
   this. Today's landing page is all action, no state.

2. **Two-and-a-half "every visit" controls; everything else is rare.**
   Volume, mic mute, and source selection are daily. The other rows
   are weekly-to-yearly. Privilege the daily, demote the rare.

3. **Max two levels of disclosure.** Nielsen: "designs that go beyond 2
   disclosure levels typically have low usability because users often get
   lost when moving between the levels." `/` → `/wizard/` is the budget —
   don't add a third tier.

4. **The recurring mental model across audio admin is Sources / Sound /
   Network / System.** Sonos, Roon, BluOS, WiiM, Plex, eero all converge on
   this shape. Add **Assistant** as a JTS-specific 5th section and
   **Accessories** as a 6th. Don't reinvent.

5. **Setup is a wizard on the critical path; a checklist everywhere else.**
   HomePod, Sonos, Stripe, Notion, GitHub all converge: linear flow until
   the device is *usable*, then a deferrable list at a stable URL. "Maybe
   later" / "Hide", never "Skip?".

6. **Status text is a noun phrase, not a sentence.** GOV.UK: "Application
   complete" not "Thank you for your application." For our rows:
   `Voice · Gemini · Aoede` — not "The voice provider is currently set to
   Gemini using the voice Aoede." Compresses the page by roughly half
   without information loss.

7. **Reversibility > discoverability for admin actions.** LAN-admin pages
   that drop WiFi mid-session, restart voice, or flip AEC need to confirm.
   Already done for Reboot; extend the model to anything destructive.

8. **Color is semantic first, accent second.** The current green reads like
   Spotify/success. Use a neutral palette with one quiet accent for
   interactivity; reserve green/amber/red for status.

9. **Performance is part of the design language.** The landing page remains
   static HTML/CSS with tiny hydration for live controls and summaries. No
   framework bundle, external font request, icon font, client router, animated
   chart, or per-row polling loop belongs on `/`. Local cacheable WOFF2 fonts
   are acceptable when their licenses are carried in-repo.

10. **Reuse primitives without over-abstracting.** The web wizards already
    have a shared primitive layer in `jasper/web/_common.py` for CSRF,
    flash/redirect hygiene, shared toggles, response helpers, and common page
    chrome. Keep that discipline during the redesign: if a row, control,
    status badge, or save pattern appears repeatedly, give it a small shared
    helper/class; if it appears once, keep it local. Avoid both copy-paste
    drift and speculative frameworks.

---

## 3. Current-state snapshot (as of 2026-05-28)

> ⚠ Re-verify this before building — new cards may have landed.
> Source: `deploy/index.html`, `deploy/integrations.html`,
> `deploy/nginx-jasper.conf`, `jasper/web/__main__.py`, live
> `http://jts.local/` on 2026-05-28.

### 3.1 Landing page `/` — 17 navigation cards

Top control card: **Volume slider** (0-100%, drag/keyboard), **Mic toggle**
(checked = listening), and a lightweight **Source selector** (Auto, AirPlay,
Bluetooth, Spotify, USB). The selector posts to `jasper-control`'s
`/source/*` routes and is distinct from the `/sources/` on/off wizard.

Then 17 navigation cards stacked equally:

| # | Title (verbatim) | Destination | One-line role |
|---|---|---|---|
| 1 | Sources › | `/sources/` | AirPlay / BT / Spotify Connect / USB on-off |
| 2 | Sound › | `/sound/` | Sound curve, Bass/Mid/Treble, advanced PEQ |
| 3 | Speaker name › | `/speaker/` | Renderer display name |
| 4 | Voice provider › | `/voice/` | Provider + API key + model + voice |
| 5 | Wake word › | `/wake/` | Wake phrase, sensitivity, detection layers |
| 6 | AirPlay sync mode › | `/airplay/` | Synced vs free-running toggle |
| 7 | Integrations › | `/integrations` | Static page → Spotify, Google, Home Assistant |
| 8 | Accessories › | `/dial/` | ESP32 dial onboarding (satellite later) |
| 9 | Bluetooth › | `/bluetooth/` | Pair phones / knobs / headphones |
| 10 | Wi-Fi › | `/wifi/` | Scan / connect / forget |
| 11 | Transit › | `/transit/` | Subway, bus, Citi Bike defaults |
| 12 | Weather › | `/weather/` | Default weather location and units |
| 13 | Speaker peering › | `/peers/` | Multi-JTS arbitration (off by default) |
| 14 | Room correction › | `https://jts.local/correction/` | iPhone room measurement (HTTPS) |
| 15 | CamillaDSP › | `:5005/` | External CamillaDSP GUI (new tab) |
| 16 | System › | `/system/` | Live metrics, cloud, actions, diagnostics |
| 17 | Wake-word corpus recorder › | `/wake-corpus/` | Operator/debug recording tool |
| — | (info panel) | — | CA cert install note for Room correction |

### 3.2 `/system/` dashboard — ~12 sub-cards

- Status line (sampler health)
- 6 metric tiles: Memory, Load, CPU, Temp, Fan (if present), Disk —
  sparklines where history is useful
- Software (sha · branch · install date · uptime · voice provider)
- Cloud activity (sessions today, 24h spend, MTD spend, per-provider table)
- Home Assistant connection status
- AirPlay health (status, recent drop/xrun summary, fan-in/Camilla state)
- Audio conversion (Medium/Best ALSA rate-converter preference)
- Network (RX / TX bytes since boot, throttle bits)
- Actions (Restart voice / Restart audio / Reboot speaker / Power off)
- Diagnostics (collapsible — runs `jasper-doctor`)
- Per-service usage (cgroup CPU + memory for JTS/audio/system support units)

### 3.3 Web surfaces under `jasper/web/`

18 stdlib-`http.server` setup/debug surfaces, mostly socket-activated and
LAN-only. Some run inside the combined `jasper-web` process; older/heavier
surfaces such as `/bluetooth/`, `/dial/`, `/system/`, and `/correction/`
still have their own service/socket wrappers.

| Path | Module | Port | Purpose |
|---|---|---|---|
| `/spotify/` | `spotify_setup.py` | 8765 | Per-household OAuth |
| `/dial/` | `dial_setup.py` | 8766 | ESP32 dial onboarding |
| `/voice/` | `voice_setup.py` | 8767 | Provider + key + model + voice |
| `/google/` | `google_setup.py` | 8768 | Calendar + Gmail OAuth |
| `/bluetooth/` | `bluetooth_setup.py` | 8769 | Adapter + pairing |
| `/correction/` | `correction_setup.py` | 8770 | Room measurement (HTTPS) |
| `/airplay/` | `airplay_setup.py` | 8771 | Sync mode |
| `/system/` | `system_setup.py` | 8772 | Dashboard |
| `/sources/` | `sources_setup.py` | 8773 | AirPlay/BT/Spotify/USB toggles |
| `/wake/` | `wake_setup.py` | 8774 | Wake-word + sensitivity + detection layers |
| `/wifi/` | `wifi_setup.py` | 8775 | NetworkManager wrapper |
| `/peers/` | `peering_setup.py` | 8776 | Multi-JTS peering |
| `/transit/` | `transit_setup.py` | 8777 | Transit address + providers |
| `/ha/` | `home_assistant_setup.py` | 8778 | Home Assistant connection |
| `/weather/` | `weather_setup.py` | 8779 | Weather default |
| `/wake-corpus/` | `wake_corpus_setup.py` | 8782 | Wake-word corpus recorder |
| `/speaker/` | `speaker_setup.py` | 8783 | Speaker display name |
| `/sound/` | `sound_setup.py` | 8784 | Sound curve + preference EQ |

Static and external companion surfaces:

- `/` and `/integrations` are static HTML under `deploy/`.
- CamillaGUI remains a separate external surface at `http://jts.local:5005/`.
- `GET /volume`, `/mic`, and `/source` are same-origin proxies into
  `jasper-control`.

### 3.4 What changed since the original proposal

- **Transit landed** as `/transit/`, including address geocoding and
  provider-specific cards. The old "future `/location/`" note is now a
  consolidation opportunity, not a prerequisite.
- **Weather landed** as `/weather/`, with its own default location/units.
  It shares enough location semantics with Transit that a future Location
  row should summarize both.
- **Home Assistant landed** under `/ha/` and currently sits behind the
  static `/integrations` umbrella.
- **Sound preferences landed** under `/sound/`; this strengthens the case
  for Sound as a top-level section.
- **Speaker name landed** under `/speaker/`; the homepage title should use
  this state and should not say `JTS speaker` (JTS already expands to
  Jasper Tech Speaker).
- **USB Audio Input landed** in `/sources/` and the landing-page source
  selector.
- **Wake-word corpus recorder landed** and should be treated as an
  operator/developer tool, not a household settings row.

---

## 4. Proposal A — Information architecture

### 4.1 The shape

```
HEADER / CONTROLS
  JTS                                  (speaker display name)
  Volume 68%      Microphone listening      Source Auto · AirPlay active

SETUP BANNER (only when incomplete or user-pinned)
  Finish setup - 4 of 7 complete        Continue        Hide

Sources
  Playback sources        AirPlay · Spotify · Bluetooth on
  Spotify accounts        2 linked
  Bluetooth devices       3 paired
  AirPlay sync            Synced

Assistant
  Voice                   OpenAI · Marin
  Wake detection          Jarvis · 0.50

Integrations
  Weather                 Sunset Park · F
  Transit                 Nearby routes
  Google                  Calendar · Gmail
  Home Assistant          Not connected

Sound
  Sound profile           Flat · EQ on
  Room correction         Off
  Advanced DSP            CamillaGUI

Network
  Wi-Fi                   Verizon_4TQ9PN · strong
  Peering                 Off

Accessories
  Dial                    Online · last seen 2m ago
  Satellites              Not paired

System
  Status                  CPU 8% · 52 C · disk OK
  Software                a139580 · main
  Diagnostics             Run doctor
  Actions                 Restart · reboot · power off
  Developer tools         Wake corpus recorder
```

### 4.2 Why this shape

- **Seven sections.** Sources / Sound / Assistant / Integrations / Network /
  Accessories / System. Still inside Miller's 7±2 with proper chunking.
  Sources/Sound/Network/System is the universal audio-admin shape (Sonos,
  Roon, BluOS, WiiM, Plex, eero). **Assistant** is the JTS-specific core:
  how JTS listens and speaks. **Integrations** is the external-service layer:
  weather, transit, Google, and Home Assistant. Accessories is its own
  top-level because the dial and satellite are **input devices for the
  speaker**, not network plumbing.

- **Use Assistant, not "Voice & Skills."** "Voice & Skills" is a bucket name
  made from implementation pieces. "Skills" is also Alexa-specific language.
  **Assistant** is the user-facing core: voice provider and wake detection.
  External services are **Integrations**. This split keeps Voice from becoming
  a junk drawer while avoiding an Alexa-style "skills marketplace" metaphor
  JTS does not actually implement.

- **State on each row.** Polaris microcopy patterns + UniFi/eero
  state-first home view. User no longer has to click in just to check
  what's set. Status is a noun phrase (`Gemini · Aoede`), not a sentence.

- **Sound is its own section, not buried in System.** Room correction and
  preference EQ are *configuration*, not *diagnostics*. CamillaGUI is advanced
  DSP, but it still belongs under Sound.

- **Kill the `/integrations` umbrella as a homepage row.** It conflates
  Spotify Connect (a source) with Google account linking and Home Assistant
  control (external services). Split: Spotify accounts under Sources; Google,
  Home Assistant, Transit, and Weather under Integrations. Keep `/integrations`
  only as a temporary compatibility surface or redirect.

- **No top-level "Now Playing".** Every consumer-audio product (Sonos,
  HomePod) leads with playback; every *admin* product (Plex, UniFi,
  Synology, eero) leads with state. JTS is admin. Phones already do
  playback better. Optional small now-playing chip in the sticky header is
  fine; making the whole page about it isn't.

- **Sticky volume + mic.** Privacy literature + the private memory note
  `feedback_silent_failure_unacceptable.md`:
  mic state must be unambiguous. Sticky placement matches Sonos/HomePod
  chrome.

- **Identity at the top.** The H1 should be the speaker display name
  (`JTS`, `Kitchen`, `Workshop`), not `JTS speaker`. JTS already expands to
  Jasper Tech Speaker, so `JTS speaker` reads as "Jasper Tech Speaker
  speaker." Drop the current "Manage your speaker." subhead unless it is
  replaced by live state.

- **Settings rows, not promo cards.** The homepage should look closer to
  iOS Settings / router admin than a landing page: grouped rows, small icons,
  one-line statuses, chevrons. Explanatory copy moves into destination pages.

### 4.3 Cards that move

| Today | New home | Why |
|---|---|---|
| Speaker name | Header identity + System/Network detail | Identity, not a section |
| Sources | Sources › Playback sources | Canonical source toggles |
| Spotify | Sources › Spotify accounts | Music-source account routing |
| AirPlay sync mode | Sources › AirPlay sync | Sub-setting, not a section |
| Bluetooth | Sources › Bluetooth devices | Multi-purpose, but one canonical BT home for now |
| Voice provider | Assistant › Voice | The assistant's speech backend |
| Wake word | Assistant › Wake detection | How the assistant starts listening |
| Transit | Integrations › Transit | External data capability tied to location |
| Weather | Integrations › Weather | External data capability tied to location |
| Google | Integrations › Google | Calendar/Gmail account capability |
| Home Assistant | Integrations › Home Assistant | Smart-home service capability |
| Integrations | Split into Sources + Integrations | Removes one ambiguous layer |
| Speaker peering | Network › Peering | Honest network concern |
| Accessories | Promoted to top-level Accessories | Input devices, not network |
| Sound | Sound › Sound profile | Preference EQ and curves |
| CamillaDSP | Sound › Advanced DSP | Configuration, not health |
| Room correction | Sound › Room correction | Configuration, not health |
| Wake-word corpus recorder | System › Developer tools | Operator/debug surface |
| System | Stays; internal cards re-grouped | Dashboard is fine |

**Bluetooth note.** The BT adapter is multi-purpose: source (phone → speaker
audio) *and* accessory bus (volume knob, headphones paired to the speaker).
Two valid presentations:

- **Option A** — One canonical Bluetooth card under Sources. Pairing flow
  reachable from there. "Devices paired to this Bluetooth adapter" listed
  inside.
- **Option B** — Sources/Bluetooth covers source on-off; Accessories/Remotes
  covers paired controllers. One adapter, two surfaces.

Lean A for simplicity in the first redesign: one Bluetooth row under Sources,
with device type labels inside `/bluetooth/`. Revisit if paired controllers
and audio devices become hard to distinguish in one list.

### 4.4 The Now Playing question, resolved

Don't make it a section. Optionally show it as a chip in the sticky header
(`Now playing — Lady Gaga · Spotify`). Skip in v1 if uncertain. The full
"now playing" UX belongs on phones; the management page's job is
configuration + health.

---

## 5. Proposal B — Setup wizard

### 5.1 Two surfaces, one source of truth

1. **A linear wizard at `/setup/`** — walks through one-time configuration
   in sequence.
2. **A dismissible banner at the top of `/`** —
   `Finish setup — 3 of 5 done · Continue ›` until the user finishes or
   hides it.

The Stripe / Notion / GitHub pattern. The wizard is for momentum; the
banner is for re-entry. Stripe's docs explicitly call the banner out as
ensuring "visibility and prompt timely action," with the checklist storing
"the state of each checkbox" so users can return anytime.

### 5.2 The steps

```
Critical path — speaker is much less useful without these:

  1. Voice provider     Pick a backend + paste API key
  2. Location           For weather, transit, sunrise/sunset, local context
  3. Spotify            Link your account (cold-start "play X")

Recommended:

  4. Speaker name       Shown in AirPlay, Spotify, Bluetooth, USB
  5. Wake detection     Default "Jarvis" works; choose / tune
  6. Room correction    ~5 min iPhone measurement
  7. Sound profile      Flat default works; pick preference curve

Conditional — only shown if the trigger fires:

  8. Google             Calendar + Gmail for assistant answers
  9. Home Assistant     Smart-home control
 10. Accessories        Dial/satellite detected? Set it up.
 11. Peering            Another JTS on the network? Pair them.
 12. USB input          Hardware supports gadget mode? Offer it.
```

Three items remain the critical path. The rest are recommended or conditional,
which keeps setup honest: the speaker is usable before room correction,
Home Assistant, or a dial. Order matches HomePod's / Sonos's "critical path
then everything else" shape. Each step is an existing wizard where practical
(`/voice/`, `/spotify/`, `/speaker/`, `/wake/`, `/sound/`) with a future
`/location/` wrapper that coordinates the already-shipped `/weather/` and
`/transit/` state.

### 5.3 Behavior

- **State**: `/var/lib/jasper/setup_state.json` (mode 0644, atomic write).
  Schema: `{ dismissed_at: null | ISO, completed: [step_id...],
  last_prompted_at: ISO }`. Each existing wizard's save handler appends to
  `completed` and removes itself from the open-loop list.

- **Banner copy**: `Finish setup — 3 of 5 done` — Zeigarnik open-loop
  framing (incomplete tasks are remembered better and drive completion).

- **Two buttons**: `Continue setup ›` and `Hide`. Hide sets `dismissed_at`
  and never auto-re-surfaces. **Never "Skip?"** — NN/g and Medium's
  onboarding research both flag the framing as making users overthink;
  "Maybe later" / "Hide" are the validated alternatives.

- **Always reachable at `/setup/`** from a small link in the page footer,
  so dismissed users can come back.

- **Re-prompt rule**: never re-show the whole banner after Hide. If a new
  conditional item becomes relevant (USB dial plugged in, second speaker
  on LAN), surface that *one* item as a chip next to the relevant section
  — not by un-hiding the banner.

- **End state**: brief success ("Setup complete. You can revisit any of
  this anytime."). Peak-end research says this final beat matters more than
  any individual step. No confetti — wrong register for admin.

### 5.4 What we explicitly don't do

- **No percentage progress bar.** "3 of 5" is more honest than 60%. NN/g
  and Apple HIG both flag false-progress percentages as a credibility risk.
- **No blocking modal.** Banner is dismissible; the rest of the page is
  fully functional underneath. Settings is a tool, not a gate.
- **No re-prompting after dismissal.** Trust the user. Otherwise the
  banner becomes the new annoying thing.
- **No "you have N tasks" badge on individual cards.** Pollutes the daily
  settings surface. The banner is the only nag.

---

## 6. Proposal C — Copy revision

### 6.1 The pattern

For each row on `/`:

- **Icon**: small, familiar, decorative unless the label is hidden.
- **Title**: noun phrase (the thing — "Voice", "Wake detection", "Peering")
- **Status line**: current value, dot-separated where multi-part
  (`Gemini · Aoede`, not "currently set to Gemini with voice Aoede")
- **Chevron**: navigates to the dedicated wizard/page.
- **No long description.** The destination explains itself.

The long explainers don't disappear — they move to where they're useful.
"Why peering" lives on `/peers/`; "what AEC does" lives on the AEC toggle
inside Advanced. The index doesn't need to teach; the destination does.

### 6.2 Side-by-side

| Today (verbose) | Proposed (terse) |
|---|---|
| **JTS speaker** / `Manage your speaker.` | **JTS** *(speaker display name; no redundant subhead)* |
| **Wake word ›** — Pick which phrase wakes the speaker — "Jarvis", "Hey Jarvis", "Alexa", or "Hey Mycroft". New models can be added by updating `jasper/wake_models.py`. | **Wake detection** · Jarvis · 0.50 |
| **Speaker peering ›** — Off by default. When you have multiple JTS speakers on the same network, turn this on so only one responds to each wake word instead of all of them at once. | **Peering** · Off |
| **Room correction ›** — Measure your room from your iPhone and apply correction filters to CamillaDSP. Browser will warn "Not Private" the first time — see the note below. | **Room correction** · Off |
| **Voice provider ›** — Choose which real-time voice backend the speaker uses (Gemini, OpenAI, or Grok) and paste API keys. | **Voice** · OpenAI · Marin |
| **AirPlay sync mode ›** — Synced (default — works for music, video A/V, and multi-room) or free-running (fallback for DAC-specific issues). | *(gone — sub-setting under Sources › AirPlay)* |
| **Sources ›** — Turn each playback source (AirPlay, Bluetooth, Spotify Connect) on or off. | **Playback sources** · AirPlay · Spotify · Bluetooth |
| **Transit ›** — Configure nearby subway and bus stops so you can ask "when's the next train?"... | **Transit** · 3 providers |
| **Weather ›** — Set the default location and units for weather questions when you do not name a city. | **Weather** · Sunset Park · F |
| **Accessories ›** — Onboard a wireless accessory — currently the ESP32 rotary dial; the AMOLED touch satellite is in progress. | **Dial** · Online |
| **Wake-word corpus recorder ›** — Tooling / debug page... | **Wake corpus** · Developer tool *(under System, collapsed/advanced)* |

### 6.3 Action button labels

Today's `Restart voice` / `Restart audio` / `Reboot speaker` are already
good — verbs lead, concrete-consequence confirms ("Wake-word will be
unavailable for ~30 s"). Keep them. The cull is on prose-status areas —
those compress to nouns.

### 6.4 Visual system

This should feel like a modern settings surface, not a marketing page and not
a 1990s directory. Recommended default:

- **Layout**: single column on phones; 2-column section grid is acceptable on
  desktop only if row order remains obvious. Sections are full-width bands or
  grouped lists, not cards inside cards.
- **Rows**: 44-52 px minimum height, icon at left, label, one-line status,
  chevron. Two-line rows only when status genuinely needs it.
- **Icons**: use small inline SVG icons (Lucide-style strokes are fine) with
  visible text labels. Do not use an external icon font or CDN dependency.
- **Color**: neutral surfaces and text first. Pick one quiet accent for
  interactive affordances, likely deep blue or blue-teal. Green is reserved
  for healthy/on/success, amber for setup-needed/warning, red for
  destructive/error.
- **Typography**: local Figtree and Outfit WOFF2 files with system fallbacks;
  no external font requests. Sentence case. Tabular numbers for percentages,
  temperatures, spend, and uptime.
- **Motion**: short hover/focus transitions only; animate transform/opacity
  when needed; respect `prefers-reduced-motion`.
- **Performance**: no framework bundle, no runtime CSS-in-JS, no client-side
  router, no charts on `/`, no per-row polling. Hydrate controls/status from
  one or two existing JSON endpoints.

---

## 7. What NOT to do

Anti-patterns to resist when building. Each one is something easy to
default to that the research surfaced as a mistake.

1. **Don't add a tab bar.** Tabs make sense for ~3-5 equal-weight modes
   (Now Playing / Devices / Settings). JTS's surface is settings-dominant
   — tabs would hide most of it. Vertical sections scroll fine.

2. **Don't try to be the Sonos app.** We're an admin page (Plex/eero
   archetype), not a consumer remote. Phones already do "play X" / "skip"
   / "queue Y" better than a webpage on `jts.local`. Resist feature creep
   toward consumer-app shape.

3. **Don't gate the page behind setup.** Even pre-setup, volume and mic
   mute must work. Settings is a tool, not a gate.

4. **Don't auto-show the wizard for returning users.** Stripe's lesson:
   persistent banner = light touch; auto-modal = friction. Once dismissed,
   stay dismissed.

5. **Don't put state behind an extra click.** Showing `Gemini · Aoede` on
   the card is the entire point. If you find yourself making users click
   in to see a setting, the IA is wrong.

6. **Don't merge `/system/` into `/`.** They're different modes — `/` is
   "configure this thing", `/system/` is "monitor / fix this thing".
   UniFi keeps Settings and Insights as separate top-level surfaces for a
   reason.

7. **Don't add a third level of disclosure.** `/` → `/wizard/` is the
   budget. If you find yourself wanting `/wizard/subwizard/` or a tab inside
   a wizard, restructure instead. Nielsen: past 2 levels users get lost.

8. **Don't write descriptions on the index.** If a card needs 20 words to
   explain itself, the words live on the destination. The index is for
   recognition (`Wake word · Jarvis`), not learning.

9. **Don't call the section "Voice & Skills."** It reads like a leftover
   drawer. Use **Assistant**. If service connections later outgrow that row,
   split to **Integrations**, not **Skills**.

10. **Don't use green as the brand accent.** Green already means healthy,
    enabled, success, and Spotify. Use a neutral UI with a separate accent;
    keep semantic colors semantic.

11. **Don't make icons carry meaning alone.** Icons are scan anchors. Text
    labels remain visible because Wi-Fi, Bluetooth, source, assistant, and
    integration icons are too ambiguous alone.

12. **Don't spend Pi budget on polish no one needs.** No React/Vue/Svelte app,
    no external font request, no icon font, no animated gradients, no
    blur-heavy glassmorphism, no live charts on the homepage. The elegance
    should come from hierarchy, spacing, type, and restraint. Local WOFF2
    fonts are fine when cacheable and licensed.

---

## 8. Phased rollout

Each phase ships independently; the page improves with every merge. Order
by "biggest visual win per hour of work."

### Phase 1 — IA + visual reshape (1 PR)
Replace the flat navigation cards on `/` with grouped settings rows:
Sources / Sound / Assistant / Integrations / Network / Accessories / System.
Apply terse copy, remove the `JTS speaker` redundancy, move the certificate
note into the Room correction row/page, and put Wake corpus under
System/Developer tools. Keep backend behavior unchanged; this is the static
landing page, CSS, and static asset-serving path. As the rows take shape,
factor only the repeated visual primitives: section headings, row layout,
row icons, status chips, chevrons, and danger/action treatments.

This delivers most of the visual win. Reversible if the grouping feels wrong.

### Phase 2 — State on the rows (1 PR)
Each row renders a noun-phrase status. Prefer existing sources:
`/source/state`, `jasper-control`'s `/state`, and `/system/data.json`.
Most state already exists: source selection, speaker name, voice, sound, Home
Assistant, dial presence, system/cloud metrics. Net new wiring should focus on
weather/transit summaries and any missing wake/correction status.

Add a `/location/` wrapper only if it truly reduces duplication between
`/weather/` and `/transit/`. It should not block Phase 1.

### Phase 3 — Setup wizard (1 PR)
- `/setup/` linear flow that chains existing wizards.
- `setup_state.json` server-side.
- Banner on `/`.
- Each existing wizard's save handler appends to `completed`.

### Phase 4 — Conditional prompts (later, polish)
- Detect newly-plugged dial → small chip under Accessories.
- Detect second JTS on LAN → chip under Network.
- Detect USB gadget-capable hardware → small chip under Sources.
- These are nice-to-haves; ship 1-3 first.

### Phase 5 — Consolidation (later)
- Move AirPlay sync into `/sources/` or make `/airplay/` feel like a subpage.
- Replace `/integrations` with redirects or a compatibility index.
- Consider `/location/` if weather/transit state keeps drifting.
- Decide whether Wake corpus should require an explicit developer-mode link.

---

## 9. Open decisions

Most major naming questions are now settled. The remaining decisions are
implementation-detail scale.

1. **Sticky vs. top-pinned volume + mic?** Implemented as top-pinned in the
   first static redesign. Revisit sticky only if users repeatedly scroll away
   from controls while making adjustments.

2. **Exact accent color.** Current landing page uses a quiet sage/spruce
   accent with semantic green reserved for listening/healthy states. Keep an
   eye on the page reading too monochrome as other wizard pages adopt the
   system.

3. **Kill `/integrations` entirely, or keep as redirect?** Keep the static
   `/integrations` compatibility index for now, but do not link it from `/`.
   Spotify lives under Sources; Google/Home Assistant/Weather/Transit live
   under Integrations.

4. **Spotify Connect "service on/off" vs. "household accounts" — one row
   or two?** Both under Sources/Spotify. Lean one row with two sub-states
   (matches "max 2 levels" principle).

5. **Bluetooth — Sources only, or Sources + Accessories?** Lean
   Sources-only with a "Paired devices" list inside (Option A in §4.3).
   Revisit if the device list grows.

6. **Room correction under Sound or System?** Sound (it's about audio
   output, not health). Card shows `Off — measure your room`; clicking
   goes to the existing HTTPS `/correction/` surface.

7. **Banner copy: "Finish setup" vs "Set up your speaker" vs "3 things
   left"?** Zeigarnik favors open-loop framing. Start with `Finish setup —
   3 of 5 done`; iterate if it feels off when seen live.

8. **Now-playing chip in the sticky header — yes or no?** Lean **no** for
   v1. Adds chrome on every page load; phones do it better. Easy to add
   later if missed.

9. **Where does the `/setup/` entry point live after the banner is
   dismissed?** Lean small text link in the page footer
   (`Run setup again →`). Also reachable from System if needed.

---

## 10. Research foundation

What I read to inform the proposal, abridged. Re-verify before building —
products evolve, especially Sonos/Google Home/Alexa apps.

### A. IA / grouping in connected-audio products

Across mature audio products, three top-level groupings recur with near-
universal regularity: **(1) per-room/device settings**, **(2) system/account
settings**, and **(3) sources/services**. Audio quality, voice assistants,
and "about" tend to sit inside (1) or (2) rather than at the top level.

**Sonos S2** splits explicitly into **System Settings** and **Room
Settings**. System Settings exposes About My System, Network, AirPlay,
Audio Compression, Date & Time, Parental Controls, Privacy & Security,
System Name, System Updates, Voice Assistants, Transfer System Ownership,
Forget Current System. The mental model is "system = the whole household;
room = this speaker." — [Sonos Community: System Settings Introduction](https://en.community.sonos.com/the-new-sonos-app-229144/system-settings-introduction-6891769),
[Sonos: Understanding the Network Details section](https://support.sonos.com/en-us/article/understanding-the-network-details-section-in-the-sonos-app).

**Apple HomePod** in the Home app: speaker-detail page as a flat list —
Room, Primary User, Reduce Bass, Personal Content, Hey Siri, Touch and
Hold for Siri, Light/Sound When Using Siri, Language, Siri Voice, Wi-Fi,
Accessibility, Doorbell Chime. Cross-device settings pulled up one level
into **Home Settings**. — [Apple: Change HomePod settings](https://support.apple.com/guide/homepod/change-settings-apde6dc8093d/homepod),
[Apple HIG: Settings](https://developer.apple.com/design/human-interface-guidelines/settings).

**Google Home** uses four top-level tabs — **Favorites, Devices, Activity,
Automations** — and pushes per-device settings into a sheet on the device
itself. Architectural bet: settings is reached through the device, not
through a global Settings menu. — [Google: What's new in Google Home](https://support.google.com/googlenest/answer/15962877).

**Amazon Alexa** routes Echo device settings inside the device detail
page; global app sidebar has **Devices, Routines, Music, More**. —
[Amazon: Alexa+ Settings on Echo Devices with a Screen](https://www.amazon.com/gp/help/customer/display.html?nodeId=T19ngFXCz1hQLePzXr).

**Home Assistant** (closest analogue to LAN-only admin): left sidebar with
**Overview, Energy, Map, Logbook, History, Media, Settings**. Settings
opens to **Devices & Services, Automations & Scenes, Areas & Zones,
People, Add-ons, Dashboards, System, About**. Power-user bias; everything
one click but nothing curated. — [HA dashboard sidebar](https://www.home-assistant.io/dashboards/sidebar/).

**Roon** Settings is vertically tabbed: **General, Storage, Services,
Audio, Library, Setup, Play Actions, Backups, Account, Extensions, About**.
— [Roon: Audio Setup Basics](https://help.roonlabs.com/portal/en/kb/articles/audio-setup-basics).

**Plex** Server Settings uses left sidebar with **Settings** (General,
Remote Access, Library, Network, Transcoder, DLNA, Languages…) and
separate **Manage** group (Libraries, Users & Sharing, Optimized
Versions). Status/health is a third grouping. — [Plex: Customizing Plex Web](https://support.plex.tv/articles/customizing-plex-web/).

**WiiM Home**: **Devices → Device Settings**, with subsections including
**Sound** (EQ — graphic, parametric, per-source), **Network**, **Audio**,
**System**. — [WiiM: Per-Source EQ guide](https://faq.wiimhome.com/en/support/solutions/articles/72000626485-how-to-use-per-source-eq-a-comprehensive-guide).

**BluOS** (Bluesound) splits **Player Settings** from **Audio Settings**.
Player covers Name, Room, WiFi, Alarms, Sleep Timer, IR Learning, Network,
Local Shares; Audio covers format/output behaviour per model. —
[BluOS: Navigating Player Settings](https://support.bluos.net/hc/en-us/articles/18062760334487).

**UniFi Network**: single **Settings** menu with sections **WiFi,
Networks, Internet, Routing, Security, Profiles, System**; distinct
**Devices/Insights/Clients** trio at the top level. Split: "things to
administer" (Settings) vs. "things to monitor" (Devices/Insights). —
[Routerhax: UniFi Controller setup](https://routerhax.com/unifi-network-controller/).

**Eero** is the cleanest: four-tab app — **Home, Devices, Activity,
Settings** — Settings holds Network Management, WiFi Credentials, Guest
Network, User Management, Software Updates, Appearance. —
[eero: Settings tab](https://support.eero.com/hc/en-us/articles/360036384611).

**Synology DSM**: windowed desktop in the browser with a single **Control
Panel** application (Connectivity, File Sharing, System, Applications).
Browser-as-OS metaphor lets one surface hold a lot of breadth without
flattening it. — [Synology: Control Panel](https://kb.synology.com/en-af/DSM/help/DSM/AdminCenter/ControlPanel_desc?version=7).

**Conclusion.** Sources, Sound/Audio, Network, About/System are
near-universal top-level slots. Voice/Assistants is sometimes a peer
(Sonos, Apple), sometimes buried (Roon). The strongest common shape:
**Now Playing (or Devices) | Sources/Services | Sound | Network/System
| About/Updates**. "Now Playing" is the *home*, not a settings section —
every product separates **playback control** (always-visible chrome) from
**settings**.

### B. First-time-setup wizard patterns

The dominant pattern across mature setup flows is **proximity-and-
momentum**: short, mandatory critical path, then a deferrable list
afterwards.

**HomePod**: Bluetooth proximity starts automatic setup; Wi-Fi, Siri,
Apple ID, Apple Music transfer from iPhone — no manual choices in the
critical path. Progression is implicit, not a progress bar. — [Apple: Set
up HomePod](https://support.apple.com/guide/homepod/set-up-homepod-apd779d9bb45/homepod).

**Apple Watch**: same proximity trigger; short linear flow language →
region → pair → tutorials for safety/cellular/gestures with explicit skip
at each tutorial step. — [Tom's Guide: Apple Watch setup](https://www.tomsguide.com/wellness/smartwatches/new-to-apple-watch-heres-how-to-set-yours-up-like-a-pro).

**Sonos**: Sonos's engineering team writes that "a short, forgettable
setup is certainly preferable to a drawn-out misadventure in home
networking, but on the Initial Configuration team, we believe your first
impression of Sonos can and should be much more." Linear, single-step:
power on → choose new/existing → sign in → detect speaker → assign room →
Wi-Fi → name speaker → optional **Trueplay** room tuning. Trueplay is
explicitly optional and deferrable. — [Sonos Tech Blog: New Wizards for
Tomorrow's Setup](https://tech-blog.sonos.com/posts/new-wizards-for-tomorrows-setup/),
[Sonos: Set up your Sonos One](https://support.sonos.com/en-us/article/set-up-your-sonos-one).

**Stripe Dashboard**: canonical "persistent banner + checklist" pattern.
The banner is "placed prominently in your application such as on the
homepage of your dashboard to ensure visibility and prompt timely action."
The account checklist "stores the state of each checkbox" so users "can
refer back to this page at any time to see what you've completed so far."
— [Stripe: Account checklist](https://docs.stripe.com/get-started/account/checklist),
[Stripe: Onboard your connected account](https://docs.stripe.com/connect/saas/tasks/onboard).

**Notion**: lightweight, fully-functional checklist embedded in the
workspace itself — not a modal, a real page the user can edit;
abandonment doesn't lose progress. — [Appcues: Notion's lightweight
onboarding](https://goodux.appcues.com/blog/notions-lightweight-onboarding).

**GitHub community-standards checklist**: prototypical "X/Y items
complete." Maintainer sees missing files (README, License, Code of
Conduct, Contributing, Issue templates, PR template) with inline "Add"
button per item. No time pressure; checklist is a tool, not a gate. —
[GitHub: community standards checklist](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/about-community-profiles-for-public-repositories).

**Specific design answers:**

- **Progress disclosure**: Mix. Apple's flows disclose nothing — linear,
  end is the signal. Sonos shows step-by-step cards. Stripe shows static
  checked/unchecked, no percentage. Web SaaS (Linear, Notion, GitHub) lean
  "X of Y" rather than %. NN/g warns against false progress: "Provide
  approximate durations to set realistic expectations" rather than
  misleading signals. — [NN/g: Smart-device app onboarding](https://www.nngroup.com/articles/smart-device-onboarding/).

- **Mandatory vs deferrable**: Mandatory path = minimum to make the device
  usable (power, network, identity, room name). Everything else
  deferrable. Recommended deferral microcopy: **"Maybe later"** — not
  "Skip" framed as a question (makes users overthink). — [Medium: Deferral
  buttons in SaaS onboarding](https://riyajawandhiya.medium.com/deferral-button-in-saas-onboarding-how-ux-copy-of-these-buttons-can-reduce-the-churn-313ed7212f2b),
  [NN/g: Onboarding — Skip it When Possible](https://www.nngroup.com/videos/onboarding-skip-it-when-possible/).

- **Dismiss and return**: Stripe's persistent banner + checklist page at
  stable URL is the most cited pattern. Notion treats checklist as a real
  artifact. Loom makes onboarding "optional, collapsible, and restorable."
  — [UX Design Institute: UX Onboarding Best Practices](https://www.uxdesigninstitute.com/blog/ux-onboarding-best-practices-guide/).

- **One step vs whole checklist**: Wizards (linear, one step) during the
  mandatory critical path; checklists (whole list visible) after. Jakob
  Nielsen on progressive disclosure: "a variant in which users step
  through a linear sequence of options, with a subset displayed at each
  step. Wizards are the classic example." — [NN/g: Progressive
  Disclosure](https://www.nngroup.com/articles/progressive-disclosure/).

- **"You're done" moment**: Peak-end rule — the ending is
  disproportionately what users remember. — [Laws of UX: Peak-End
  Rule](https://lawsofux.com/peak-end-rule/). GOV.UK: "it's fine to say
  'Application complete' rather than 'Thank you for your application.'"

- **Re-prompt cadence**: Stripe shows banner until critical items
  complete. Notion never re-prompts; checklist is opt-in. Best practice:
  light-touch — re-prompt only when context changes (new device added, key
  missing).

### C. Behavioral / IA / UX principles

The principles I lean on in the proposal:

**1. Match the system to the user's mental model.** Don Norman: "the
designer must ensure that the system image is consistent with and operates
according to the proper conceptual model" — absent that, "people are
likely to make up inappropriate ones." — [Norman, *Design of Everyday
Things*, summary](https://www.sharritt.com/CISHCIExam/norman.html).

**2. Information architecture is a language problem.** Abby Covert:
"Information architecture (IA) is the way we arrange the parts of
something to make it understandable as a whole. … most of the time, there
is no right or wrong way to make sense of a mess. Instead, there are many
ways to choose from." — [Covert, *How to Make Sense of Any
Mess*](https://abbycovert.com/make-sense/).

**3. Progressive disclosure beats up-front complexity.** Jakob Nielsen:
"Initially, show users only a few of the most important options. Offer a
larger set of specialized options upon request." Working ceiling: "designs
that go beyond 2 disclosure levels typically have low usability because
users often get lost when moving between the levels." — [NN/g: Progressive
Disclosure](https://www.nngroup.com/articles/progressive-disclosure/).

**4. Hick's Law: choice cost is real.** "The time it takes to make a
decision increases with the number and complexity of choices." Cited
examples include Apple TV Remote's deliberate physical simplicity and
Slack's progressive onboarding. — [Laws of UX: Hick's
Law](https://lawsofux.com/hicks-law/).

**5. Miller's Law and chunking.** "The average person can only keep 7
(plus or minus 2) items in their working memory" — applied to UI, this
is a mandate to chunk, not a hard cap on menu items. Stéphanie Walter's
caveat is worth holding: the rule is about short-term memory, not menu
length. — [Laws of UX: Miller's Law](https://lawsofux.com/millers-law/),
[Walter: Your menu doesn't need Miller's 7±2 rule](https://stephaniewalter.design/blog/your-menu-doesnt-need-millers-7-plus-minus-2-rule/).

**6. Zeigarnik effect drives completion.** "People remember uncompleted or
interrupted tasks better than those that have been completed" — and the
goal-gradient hypothesis says effort accelerates as the goal nears.
Progress visibility is the lever. — [Designzig: Zeigarnik
Effect](https://designzig.com/zeigarnik-effect-in-ux-design/), [Designzig:
Goal Gradient Effect](https://designzig.com/goal-gradient-effect-in-ux-design/).

**7. Peak-end rule.** "People judge an experience largely based on how
they felt at its peak and at its end, rather than the total sum or average
of every moment of the experience." For setup flows, the ending state
matters more than the steps. — [Laws of UX: Peak-End
Rule](https://lawsofux.com/peak-end-rule/).

**Method note.** Card sorting is the canonical way to validate IA against
actual users' mental models — open or closed, moderated or unmoderated.
"Generative research method most useful at the beginning stages of a
project." If the household disagrees with the proposed groupings, do an
open card sort with them. — [NN/g: Card
Sorting](https://www.nngroup.com/articles/card-sorting-definition/).

### D. Microcopy best practices

**Brevity is the rule.** GOV.UK: "put the important words first and drop
any unnecessary words. For instance, instead of 'This is the total cost',
simply say 'Total cost.'" Error messages: "do not say 'You have entered
the wrong password'. Say 'Wrong password'." And "no need to say 'sorry'
in validation error messages." — [GOV.UK: Writing for user
interfaces](https://www.gov.uk/service-manual/design/writing-for-user-interfaces).

**Buttons lead with a verb.** Shopify Polaris: "Let visuals and icons do
the talking wherever you can ('+' not '+ Add')" and "Be direct ('add
apps' not 'you can add apps')." — [Polaris: product
content](https://polaris-react.shopify.com/content/product-content). GOV.UK:
"make the purpose of the link clear from the link text alone." "Submit" →
"Send invite" is the canonical example for activation lift.

**One voice, many tones.** Mailchimp: "You have the same voice all the
time, but your tone changes." Default is informal, "but it's always more
important to be clear than entertaining." Mailchimp explicitly bans
exclamation points in failure messages. — [Mailchimp: Voice and
Tone](https://styleguide.mailchimp.com/voice-and-tone/).

**Status text is a noun, not a sentence.** GOV.UK: "it's fine to say
'Application complete' rather than 'Thank you for your application.'"
Applied to a settings row: "Voice provider is currently set to Gemini" →
**"Voice · Gemini"** or **"Gemini"** under heading "Voice." Material
Design: "Clarity is the single most important metric in UX writing. If a
user has to read a sentence twice to understand it, the design has
failed." — [Material: Content design](https://m3.material.io/foundations/content-design/overview).

**Button vs. link.** Buttons cause action and change state; links
navigate. GOV.UK and Polaris both treat misuse as a defect.

**Explainer vs. assumed knowledge.** Polaris: "Only add content that's
necessary for clarity… Find the shortest, clearest way to give merchants
only the info they need to take action." Treat copy "like Jenga: remove
everything possible before the experience breaks." Corollary: keep one
explainer near complex settings (what AEC does); drop it everywhere else.

### E. The "device admin page" archetype

The key distinction is **agency**: a consumer app is something the user
*uses*, an admin page is something the user *administers*. The user is in
a different role — confident, deliberate, occasional, comfortable with
technical labels. This shifts several defaults.

UniFi, Synology DSM, eero, Plex, and Apple AirPort Utility all share the
same archetype:

1. **State-first, action-second.** The home view shows what *is* —
   devices, status, health — not what to do. eero's Home tab "gives you a
   clear view"; UniFi's Dashboard shows clients/uplinks/throughput;
   Synology's "Information Center" shows hardware and connection state.
   Plex puts "Status" as a sidebar group.

2. **Settings is a destination, not a screen.** Synology models it as an
   entire windowed app ("Control Panel"). UniFi treats Settings as one of
   three top-level surfaces (Settings/Insights/Devices). Implication:
   settings *deserves real estate*, and the home view shouldn't try to be
   settings.

3. **Power-user expectations.** Admin pages tolerate jargon ("VLAN,"
   "DHCP," "transcoder," "AEC") that consumer apps would never use,
   because the audience self-selected. UniFi exposes Networks (VLANs),
   Profiles (RADIUS), Routing — all technical without apology. Trade-off:
   terms should still be linkable (info icon, help tooltip) —
   recognition over recall.

4. **Local-only changes the security mental model.** LAN-only admin pages
   don't need account/auth UI in the way Stripe or Notion do. The
   dashboard chrome can omit "you" identity — the user is implicitly the
   household admin.

5. **Reversibility matters more than discoverability.** Admin actions
   cause real-world effects (Wi-Fi drop, restart, brick). For a smart
   speaker this maps to mic mute, Wi-Fi change, voice-provider switch —
   actions that should confirm before firing. Today's two-stage reboot
   confirm is exactly this pattern.

6. **Single voice, no marketing.** Admin copy is more neutral and less
   "branded" than consumer copy. Tone here is closer to "your boss" than
   "your friend."

### F. Synthesis — the strongest signals

1. **Three top-level slots recur across every audio admin product worth
   citing: Sources/Services, Sound, Network/System.** Voice/Assistant and
   About/Updates are next-tier. Now Playing is the *home*, not a settings
   section. The user's mental model is built around these chunks — fight it
   at your peril.

2. **The mandatory setup path is short and linear; everything else is a
   checklist.** Critical-path-as-wizard, everything-else-as-persistent-
   checklist. Wizard ends when the device is usable; checklist lives on a
   stable URL the user can return to. Deferral copy is "Maybe later," not
   "Skip?".

3. **More than two levels of disclosure is a design smell.** Nielsen's
   most actionable single constraint. With 17 navigation cards today, the
   right move is to chunk into 6 top-level groups, max one drill-down inside
   each.

4. **Status text wants to be a noun phrase, not a sentence.** GOV.UK and
   Polaris are aligned: `Voice · OpenAI`, not "The voice provider is
   currently set to OpenAI." Compresses the page roughly in half without
   information loss and makes it scannable.

5. **An admin page is a different rhetorical register than a consumer
   app.** State-first home (what is happening), settings as a real
   destination, jargon allowed with tooltips, confirm-before-destructive.
   UniFi, Synology, eero, Plex all behave this way; the smart-speaker
   management page should too — closer to a router admin than to the
   Sonos consumer app.

### G. 2026-05-28 refresh — iOS Settings + compact row design

This refresh was added after the page grew from 13 to 17 navigation cards and
the user explicitly asked whether "Voice & Skills" was the right grouping. The
answer: no. The best current name is **Assistant**.

**iOS Settings patterns that transfer well:**

- iOS groups by user mental model more than implementation: identity/account
  at top; common connectivity near the top; device/system controls; privacy
  and security; app-specific settings pushed lower. The JTS equivalent is:
  identity/control header, then Sources, Assistant, Sound, Network,
  Accessories, System. — [Apple: Find settings on iPhone](https://support.apple.com/en-ie/guide/iphone/iph079e1fe9d/ios),
  [9to5Mac: iOS 18 Settings changes](https://9to5mac.com/2024/07/18/ios-18-settings-whats-new/)
- Apple separates quick controls from durable settings. JTS should keep
  Volume, Microphone, and Source selection pinned; everything else becomes
  a grouped settings row. — [Apple: Control Center on iPhone](https://support.apple.com/en-gb/guide/iphone/iph59095ec58/ios)
- Apple's HIG says too many settings make an experience less approachable and
  harder to search; settings should affect the overall experience, while
  task-specific controls should live near the task. That supports moving
  AirPlay sync under Sources and Room correction under Sound. —
  [Apple HIG: Settings](https://developer.apple.com/design/human-interface-guidelines/settings)
- At the list level, iOS uses short labels, familiar icons, secondary values,
  and disclosure. Explanation belongs on the destination screen, not the
  first row. Apple's writing guidance and GOV.UK's UI writing guidance both
  point in the same direction: brief, clear, front-loaded labels. —
  [Apple HIG: Writing](https://developer.apple.com/design/human-interface-guidelines/writing),
  [GOV.UK: Writing for user interfaces](https://www.gov.uk/service-manual/design/writing-for-user-interfaces)

**Settings-list guidance from broader design systems:**

- Material's settings pattern says important settings go first, section titles
  should be specific, labels should be brief, and secondary text should show
  current state rather than repeating the label. This is almost exactly the
  JTS homepage problem. — [Material Design: Settings](https://m1.material.io/patterns/settings.html)
- WCAG 2.2's target-size minimum is 24 by 24 CSS pixels; for a phone-opened
  LAN settings page, rows should be closer to 44-52 px high so they feel easy
  to tap. — [W3C: What's new in WCAG 2.2](https://www.w3.org/WAI/standards-guidelines/wcag/new-in-22/)
- SVG is the right icon format here. Inline SVG avoids icon-font downloads,
  can inherit CSS color, and can be marked `aria-hidden` when paired with a
  visible label. — [web.dev: Icons](https://web.dev/learn/design/icons/)
- JavaScript parsing/execution is render-blocking by default, and expensive
  animations can jank. Keep `/` static; animate only small opacity/transform
  changes when useful. — [MDN: JavaScript performance](https://developer.mozilla.org/en-US/docs/Learn_web_development/Extensions/Performance/JavaScript),
  [MDN: Animation performance](https://developer.mozilla.org/en-US/docs/Web/Performance/Animation_performance_and_frame_rate)
- Accent colors should not be reused for semantic meaning. If green means
  success/on, it should not also be the house brand color. — [Atlassian Design:
  Accent colors](https://atlassian.design/foundations/color-new/accents/)

**What not to copy from iOS:**

- Do not copy iOS's giant universal settings list. JTS is one device, not an
  operating system.
- Do not bury integrations under an "Apps" analogue. JTS integrations are
  assistant/source capabilities, not installed apps.
- Do not rely on search as the escape hatch. Occasional LAN users should be
  able to scan the page without remembering the right term.
- Do not create more top-level groups just because iOS can. The six-section
  JTS shape is a feature.

---

## 11. Appendix — when you're ready to build

Pre-flight checklist for future-you:

- [ ] **Re-inventory `deploy/index.html`** — new rows may have landed
      since the 2026-05-28 snapshot. Diff against §3.1 and update the
      proposal sections if necessary.
- [ ] **Re-inventory `jasper/web/*_setup.py`** — same. Check if new
      wizards exist that need a home in the IA.
- [ ] **Check `git worktree list` and `gh pr list --state open`** for
      in-flight UI work that might conflict.
- [ ] **Re-skim sections 10A and 10G** (competitor/iOS patterns) for any
      product that's shipped a major IA change since 2026-05-28. Sonos in
      particular tends to redesign their app every couple of years.
- [ ] **Decide the open questions in §9** — write the answer next to each
      one before opening the PR.
- [ ] **Spend 30 minutes doing a card sort with the household** — open
      sort, no predefined groups. If they cluster identically to §4.1,
      ship. If not, the proposal is wrong about the mental model and
      needs revision before code.
- [ ] **Landing-page scope**: keep changes inside `deploy/index.html`,
      local static assets, install copying, and nginx static serving unless
      a row truly needs new state. Backend control behavior should remain
      unchanged and easily reversible.
- [ ] **Keep shared web primitives in view** — use `jasper/web/_common.py`
      and `tests/test_web_wizard_conventions.py` as the guardrails when a
      wizard changes. Prefer tiny shared helpers/classes for repeated controls
      and states; keep one-off page layout local.

Notes specific to JTS that the research doesn't cover:

- **The mic toggle is a privacy promise**, not just a feature. Its visual
  state must be unambiguous on every viewport (captured in the private
  memory note `feedback_silent_failure_unacceptable.md`).
  Don't bury it under a fold or behind a toggle whose state can be
  misread.
- **All web pages are HTTP, not HTTPS, except `/correction/`** (which
  needs `getUserMedia`). Don't accidentally redirect the whole page to
  HTTPS — surfaces the self-signed cert warning. This comes from the
  private memory note `feedback_jts_http_not_https.md`.
- **State files live under `/var/lib/jasper/*.env`** with `EnvironmentFile=`
  chaining in the systemd units (see AGENTS.md "Voice provider switching"
  for the canonical pattern). The new `setup_state.json` should follow:
  atomic tempfile + rename, mode 0644 unless it carries secrets, fail-safe
  default if unreadable.
- **The `/state` aggregator on `jasper-control:8780`** fails soft per
  section — wire status reads off it, not off individual daemons.

Last verified: 2026-05-28 (Phase 1 landing-page implementation, local font
asset serving, landing page, integrations page, nginx route map,
`jasper/web/__main__.py`, `/system/` dashboard, live `http://jts.local/`, and
2026-05-28 design/iOS research refresh)
