# AI agent operational guide for JTS

> **This file is canonical.** Edit operational rules and
> per-subsystem guidance here, not in CLAUDE.md. CLAUDE.md is a
> thin Claude-Code-specific shim that imports this file via
> `@AGENTS.md`. Any operational content added to CLAUDE.md will
> be lost or ignored — make changes here instead.

**Read [README.md](README.md) first** — it has the project context
(architecture, hardware, repo layout, subsystem overview,
deployment, debugging entry points). This file adds AI-specific
operational guidance on top of that. Don't expect this file to
restate README; it doesn't.

What goes here:
- Things easy to get wrong (model gotchas, file ownership lines,
  brick hazards)
- Operational shortcuts (specific scripts, env-var formats)
- AI behavioral rules specific to this codebase
- How docs are organized (see "Documentation paradigm" below)

---

## Documentation paradigm

How docs in this repo are structured, so additions land in the
right place. Read this before adding or restructuring docs.

1. **Single source of truth.** Each concept (hardware, voice-provider
   switching, AEC tuning, etc.) lives in exactly one file. Others
   link to it; they don't restate it. Drift between files is a bug.

2. **HANDOFF shape: current state first, history below.** Every
   `docs/HANDOFF-*.md` opens with the current operational truth —
   what works today, what to touch, what not to touch — in <400
   lines. Investigation narrative (dated entries, decision
   archaeology, "how we got here") sits below as an appendix.

3. **Date every load-bearing claim. `Last verified:` footer.**
   Every HANDOFF ends with `Last verified: YYYY-MM-DD`. Bump it
   when you re-verify (re-read the doc against the current code
   and confirm claims still hold), not just on edit.
   `scripts/doc-freshness.sh` reads this footer and reports docs
   overdue for re-verification.

4. **Memory = user-private. Repo = everyone.** Rules that apply to
   every contributor go in [CONTRIBUTING.md](CONTRIBUTING.md) or
   this file. Memory (Claude Code's `~/.claude/.../memory/`) stays
   user-private — personal preferences, household composition,
   in-progress hunches. If a memory entry should apply to anyone
   touching this repo, externalize it.

5. **Code references use function names, not line numbers.** A
   reference like `jasper/voice_daemon.py` + `build_cue_tts_backend`
   survives refactors; `:172` doesn't. Use line numbers only when
   the line itself is the point (a magic number, a specific bug
   location). When a line number is the right call, treat the
   number as load-bearing — a verification pass on
   `HANDOFF-correction.md` (2026-05-23) found four stale `:N`
   refs that misled readers.

6. **Touched-subsystem rule.** If your PR touches `jasper/voice/*`,
   scan `docs/HANDOFF-voice-providers.md` (and similarly for other
   subsystems). The PR template has an "I scanned the related
   HANDOFF" checkbox — that's the enforcement hook. If you found
   anything stale while scanning, fix it inline in the same PR.

7. **README is the doc atlas.** Every shipped doc gets listed in
   README's documentation map, or is explicitly tagged elsewhere
   (session-artifact / archived / research). No orphan docs.

8. **One canonical file per agent convention.** AGENTS.md (this
   file) is canonical. CLAUDE.md is `@AGENTS.md` (Claude Code's
   `@`-import directive) plus the canonical-file banner. No
   operational content lives in CLAUDE.md. This follows the
   [agents.md](https://agents.md) cross-tool convention adopted
   by Codex, Cursor, GitHub Copilot, Gemini, Aider, and others.

---

## Deploying code changes to the Pi

From the laptop, one command:

```sh
bash scripts/deploy-to-pi.sh
```

This is the **only** supported deploy path. It does, in order:

1. `git rev-parse` → captures local SHA + branch (writes `-dirty`
   suffix if working tree has uncommitted changes)
2. `rsync` to `pi@jts.local:/home/pi/jts/` (excludes `.git/`,
   `.venv/`, `*.egg-info`, etc.)
3. `ssh ... sudo bash install.sh` with `JASPER_DEPLOY_SHA*` env vars
   set — `pip install -e`'s into `/opt/jasper/.venv` (the runtime),
   writes `/var/lib/jasper/build.txt`, migrates units to socket
   activation, conditionally enables AEC on 6-ch firmware
4. `systemctl restart jasper-control` + `systemctl start
   jasper-aec-reconcile` — picks up Python control code and lets the
   mic/AEC reconciler restart or park `jasper-voice` according to the
   hardware actually present. `jasper-camilla` is the Rust camilladsp
   binary (not restarted).

**Do NOT hand-roll `rsync + sudo bash install.sh + systemctl restart`.**
That flow exists historically but misses:
- the laptop-side SHA capture (dashboard's "Software" card shows
  "unknown")
- the post-install daemon restart on subsequent deploys (install.sh
  only conditionally restarts `jasper-voice` when the AEC default
  flips — a one-time event)

**Skip flags:** `SKIP_INSTALL=1` (rsync only), `SKIP_RESTART=1`
(install but don't restart/reconcile), `PI_HOST=...`, `PI_USER=...`.

**Adding a wizard port to `jasper-web.socket`?** `install.sh`'s
wizard-socket loop uses `systemctl restart` (not `start`) so a new
`ListenStream=` line actually re-binds the live socket on deploy. A
bare `start` is a no-op when the socket is already active, which
silently leaves the old port set live and 502s on the new wizard
until the next reboot. Verified failure mode + fix landed in PR #118
when /sources/ on port 8773 went out without the restart.

**Verify the deploy landed:**
- `http://jts.local/system/` → Software card shows the matching
  short-SHA and recent install timestamp
- Or `ssh pi@jts.local 'sudo cat /var/lib/jasper/build.txt'`

The one exception: a **fresh Pi** doing first-time setup runs
`sudo bash deploy/install.sh` natively after `git clone` on the
Pi itself (see [BRINGUP.md](BRINGUP.md)). The wrapper isn't
applicable until there's a laptop checkout.

---

## Speaker hostname — single source of truth

`JASPER_HOSTNAME` (default `jts.local`) is the canonical name other
devices type in to reach the speaker. Set in `/etc/jasper/jasper.env`.

What derives from it (so you only set it once):
- Python: `Config.hostname` plus `JASPER_MANAGEMENT_URL` and
  `JASPER_SPOTIFY_SETUP_URL` defaults (`http://${JASPER_HOSTNAME}` and
  `http://${JASPER_HOSTNAME}/spotify` respectively).
- Bash scripts under `scripts/`: every `PI_HOST` default falls back to
  `${JASPER_HOSTNAME:-jts.local}`. So if you also export
  `JASPER_HOSTNAME` in your laptop shell, `fetch-pi-logs.sh`,
  `tail-pi-logs.sh`, `switch-voice-provider.sh`, etc. all target the
  right host without per-script overrides.

What does NOT derive (intentionally):
- The Pi's actual mDNS hostname (set with `hostnamectl set-hostname`
  + Avahi). Setting `JASPER_HOSTNAME` doesn't change what the Pi
  advertises — that's a separate, OS-level concern. Run hostnamectl
  first; then point `JASPER_HOSTNAME` at it.
- The Spotify OAuth bounce page at
  `https://jaspercurry.github.io/spotify-oauth-callback/` — separate
  public repo (`jaspercurry/spotify-oauth-callback`). It's hostname-
  agnostic: the local target is passed in as `?host=<JASPER_HOSTNAME>`
  on the redirect URI registered with Spotify, validated against an
  mDNS regex, and used as the redirect target. So changing
  `JASPER_HOSTNAME` here Just Works against the same hosted page —
  no fork-and-redeploy.

---

## Renderer architecture — file map

`install.sh` source-builds shairport-sync (AirPlay 2) + nqptp,
drops in librespot (rust, via raspotify .deb) + bluez-alsa +
bt-agent, and owns the full systemd unit per renderer.

`jasper/renderer.py:RendererClient` reads renderer state from each
daemon's own surface:
- librespot → `/run/librespot/state.json` (written by the
  `--onevent` hook `/usr/local/bin/jasper-librespot-event`)
- shairport-sync → MPRIS PlaybackStatus over busctl
- bluez-alsa → `bluealsa-cli list-pcms`

`jasper-mux.service` does latest-source-wins preemption: when a
new source transitions to playing while another is already active,
it pauses the older one.

Spotify volume control goes via the Spotify Web API (the multi-
account `spotify_router`) since librespot has no local control
HTTP — see [`docs/HANDOFF-volume.md`](docs/HANDOFF-volume.md).

---

## Voice provider switching — read first

The voice loop runs against any of three real-time speech-to-speech
APIs. Architecture and per-provider trade-offs are in
[`docs/HANDOFF-voice-providers.md`](docs/HANDOFF-voice-providers.md);
this section is the operational summary.

### Single source of truth: `/var/lib/jasper/voice_provider.env`

**`JASPER_VOICE_PROVIDER` lives in exactly one file**:
[`/var/lib/jasper/voice_provider.env`](deploy/systemd/jasper-voice.service),
written by the `/voice` wizard. **Never set it in
`/etc/jasper/jasper.env`** — `install.sh` migrates any stale value
out of there into the wizard file on every run, since having a
default in BOTH led to stale-vs-runtime confusion (the wizard wrote
one value, the install template still had another, and reading
either file in isolation gave a wrong answer about "what's the
active provider").

There is **no fallback default**. Fresh installs land with the
variable unset; `jasper-voice` refuses to start with a clear error
("visit `http://jts.local/voice` and pick one") until the wizard
writes the file. The doctor and the `/system/` dashboard surface
this state. Same pattern applies to the per-provider keys
(`GEMINI_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`) and model /
voice selectors — all wizard-owned per
[`jasper/web/voice_setup.py`](jasper/web/voice_setup.py).

**To override without using the wizard** (CI, headless imaging,
operator preference): write `JASPER_VOICE_PROVIDER=<id>` to
`/var/lib/jasper/voice_provider.env` directly. systemd loads it on
the next jasper-voice start.

### Two ways to switch the active provider

**Web UI (preferred, end-user friendly)** — visit
`http://jts.local/voice/` from any device on the LAN. The page
shows one card per provider for pasting API keys, picks model and
voice from curated dropdowns, and has a single radio group at the
top for "use this provider". Saving writes
`/var/lib/jasper/voice_provider.env` and restarts `jasper-voice`.
Source: [`jasper/web/voice_setup.py`](jasper/web/voice_setup.py).

**Laptop-side script (operator-friendly, scriptable)**:

```sh
bash scripts/switch-voice-provider.sh           # show current
bash scripts/switch-voice-provider.sh gemini    # gemini-3.1-flash-live-preview
bash scripts/switch-voice-provider.sh openai    # gpt-realtime-2 (released 2026-05-07)
bash scripts/switch-voice-provider.sh grok      # grok-voice-think-fast-1.0
```

The script refuses to switch if the destination provider's API key
isn't already in `/etc/jasper/jasper.env` (`GEMINI_API_KEY`,
`OPENAI_API_KEY`, or `XAI_API_KEY`) or in the wizard-written
`/var/lib/jasper/voice_provider.env`. Set the key first via
either path; the script sets the provider and restarts
`jasper-voice` in one shot.

**Per-provider model env var** is independent of the provider switch
— `JASPER_GEMINI_MODEL`, `JASPER_OPENAI_MODEL`, `JASPER_GROK_MODEL`.
The `switch-gemini-model.sh` script (below, "Gemini model switching")
flips the *Gemini* model alias for within-Gemini fallback (3.1 ↔ 2.5)
and is independent of cross-provider switching.

**Pricing trade-off** (early 2026):

| Provider | Cost / minute | Notes |
|---|---|---|
| `gemini` | ~$0.025 | cheapest; 15-min audio cap with 2-h resumption handle |
| `openai` | ~$0.30 | reasoning levels, 128K context, 60-min hard cap, no resumption |
| `grok` | ~$0.05 | flat $3/hour; spend cap under-counts (logs warning) |

**Cue regeneration**: cue WAVs (static failure cues +
dynamic-content cues like timer fire announcements) are baked from
the **active provider's TTS endpoint** — Gemini 3.1 Flash TTS,
OpenAI gpt-4o-mini-tts, or xAI Grok TTS — picked by the factory at
[`jasper/voice_daemon.py:_build_cue_tts_backend`](jasper/voice_daemon.py).
Cues sound in the same voice the assistant uses for live replies.
Switching providers (env or web wizard) auto-invalidates baked
WAVs via the cache key (model + voice change → new hash).
Per-provider model defaults are pinned in
[`jasper/cues/generator.py`](jasper/cues/generator.py) and
overridable for Gemini via `JASPER_GEMINI_TTS_MODEL`. If the
active provider's key is missing, the factory falls back to any
other configured key with a warning so cues still play; with no
keys at all, regen is disabled and the daemon plays whatever WAVs
already exist on disk.

**Adding a fourth provider**: see the "Adding a fourth provider"
checklist in
[`docs/HANDOFF-voice-providers.md`](docs/HANDOFF-voice-providers.md).
The interface is `LiveConnection` + `LiveTurn` at
[`jasper/voice/session.py`](jasper/voice/session.py); shared
supervisor helpers (backoff, fingerprint, escalation cue) live at
[`jasper/voice/_supervisor.py`](jasper/voice/_supervisor.py).

---

## Voice prompting — read HANDOFF-prompting.md first

Before editing `SYSTEM_INSTRUCTION` in
[`jasper/voice_daemon.py`](jasper/voice_daemon.py), any tool
description in [`jasper/tools/`](jasper/tools/), or any LLM-facing
prompt surface, read
[`docs/HANDOFF-prompting.md`](docs/HANDOFF-prompting.md). It's the
canonical playbook — cross-provider principles, provider deltas,
the JTS `SYSTEM_INSTRUCTION` walk-through, a tool-prompt cookbook,
and a pitfalls catalog. Refreshed against the provider docs
2026-05-23.

The rules most often violated without it:

- **Conditional over absolute.** OpenAI's docs say "remove
  `always`/`never`/`only`/`must` rules unless truly required."
  Absolute preamble bans get ~33% compliance on gpt-realtime
  per a public community thread. Phrase rules as "When X, do
  Y" and enumerate X — the model doesn't generalize unstated
  scopes. The Gemini story is muddier (forum evidence of 3.1
  audio ignoring conditionals 2.5 honored) — documented in the
  playbook.
- **POSITIVE framing for tool calls.** "Call X when Y," not
  "Don't guess." A negative-heavy version of our prompt made
  gpt-realtime-2 skip tools across five voice-eval scenarios —
  rationale in the comment block above `SYSTEM_INSTRUCTION` in
  [voice_daemon.py](jasper/voice_daemon.py).
- **Preamble suppression is a conditional skip-list, never a
  ban.** Live version in the `Tools — preambles` section of
  `SYSTEM_INSTRUCTION`; mirrors OpenAI's documented pattern.
- **Per-tool conditional rules belong in the tool's docstring,
  not `SYSTEM_INSTRUCTION`.** `build_tool()` at
  [jasper/tools/__init__.py](jasper/tools/__init__.py) sends
  the full cleaned docstring to the LLM. When-to-call,
  voice-answer style, and response-shape handling live in each
  tool's docstring. `SYSTEM_INSTRUCTION` keeps only cross-tool
  meta-rules (`error` / `confirm` field handling, preamble
  policy, verbosity, unclear-audio handling, the small set of
  cross-tool routing rules where two similar tools need
  disambiguation).

Canonical provider sources (full list in HANDOFF-prompting.md):
- OpenAI Realtime — [Realtime Prompting Guide](https://cookbook.openai.com/examples/realtime_prompting_guide)
  + [Using realtime models](https://developers.openai.com/api/docs/guides/realtime-models-prompting)
- Gemini Live — [3.1 Flash Live Preview docs](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview)
  + [Live API best practices](https://ai.google.dev/gemini-api/docs/live-api/best-practices)
- xAI Grok Voice — [Voice agent guide](https://docs.x.ai/docs/guides/voice/agent)

---

## Wake-word switching — read first

The wake phrase the speaker listens for is one of a curated set of
openWakeWord models. As of 2026-05-16 the default is **"Jarvis"**
(the fwartner community model in `/var/lib/jasper/wake/jarvis_v2.onnx`,
which also still triggers on "Hey Jarvis"). The registry of available
models is the single source of truth at
[`jasper/wake_models.py`](jasper/wake_models.py); install.sh reads it
to know which non-bundled `.onnx` files to fetch.

**Two ways to switch.** Either works.

**Web UI (preferred)** — visit `http://jts.local/wake/` from any LAN
device. One row per registered model with pronunciation + description
+ author-reported false-fire rate. Pick one, hit Save — writes
`/var/lib/jasper/wake_model.env` at mode 0644 and restarts
`jasper-voice`. A sensitivity slider underneath the picker tunes
`JASPER_WAKE_THRESHOLD` (0.05–0.95, default 0.50 — lower wakes more
easily, higher requires a more confident match) and persists into
the same env file on the same Save. Source:
[`jasper/web/wake_setup.py`](jasper/web/wake_setup.py).

**Laptop-side script:**

```sh
bash scripts/switch-wake-word.sh             # show current + options
bash scripts/switch-wake-word.sh jarvis_v2   # community Jarvis (default)
bash scripts/switch-wake-word.sh hey_jarvis  # stock Hey Jarvis
bash scripts/switch-wake-word.sh alexa       # stock Alexa
bash scripts/switch-wake-word.sh hey_mycroft # stock Hey Mycroft
```

The script resolves the key via the Pi-side registry, refuses to
flip to a model whose `.onnx` is missing on disk (rare — install.sh
fetches them every deploy), and restarts `jasper-voice`.

**Adding a new model**: edit `REGISTRY` in
[`jasper/wake_models.py`](jasper/wake_models.py) with one
`WakeModelEntry(...)`. Bundled openWakeWord names (e.g. `alexa`) need
no `download_url` — `openwakeword.utils.download_models()` already
pulls them on install. External `.onnx` files set `download_url` to a
raw URL and `model` to an absolute path under
`/var/lib/jasper/wake/`. Re-run `bash scripts/deploy-to-pi.sh` and
the new model appears in `/wake/` and `switch-wake-word.sh`
automatically.

**Hand-rolled custom models** are still supported: set
`JASPER_WAKE_MODEL=/abs/path/to/foo.onnx` in `/etc/jasper/jasper.env`
directly. The wizard surfaces this as a "Custom: …" row and won't
overwrite it unless the household picks a registered alternative.

---

## Wi-Fi switching — read first

The household-facing way to change the speaker's Wi-Fi network is
the wizard at `http://jts.local/wifi/`. Current network at the top,
Scan button + tap-to-connect for nearby networks in the middle,
Saved networks (Forget anything) in a collapse section at the
bottom. All backed by `nmcli` subprocess calls; no new dependency.

**Lockout safety is the part to read before editing this page.**
Three layers, all in [`jasper/web/wifi_setup.py`](jasper/web/wifi_setup.py):

1. **Connect rollback.** `nmcli --wait 30 dev wifi connect …` — on
   non-zero exit we explicitly `nmcli --wait 20 connection up
   <previous-profile>` to put the user back on the network they were
   on. If that connect created a brand-new (broken) profile, we
   delete it so the saved list doesn't accumulate dead entries.
   Don't rely on NM's auto-rollback alone — it's not reliable across
   all failure modes.

2. **Forget guard.** If the user tries to forget the currently-active
   network, an extra warning fires in the inline confirm panel —
   stronger when no Ethernet is plugged in.

3. **Radio kill warning.** Toggling the Wi-Fi radio off when the Pi
   has no Ethernet path fires a confirm() dialog with stark caps-lock
   copy: "TURNING WI-FI OFF WILL DISCONNECT THIS PI". The page can't
   reach the Pi after the radio goes down, so this dialog is the
   user's only chance to bail out.

Lockout classification is driven by `_has_ethernet()` (the `lockoutRisk`
field on `/state`). If the Pi has both Wi-Fi and Ethernet, the
warnings soften — Ethernet is the fallback path.

**Operational reach:** there's no laptop-side script wrapper for this
(unlike `switch-voice-provider.sh` / `switch-wake-word.sh`). Manual
nmcli still works for SSH-driven changes:

```sh
nmcli dev wifi list
nmcli dev wifi connect "<SSID>" password "<PSK>"
nmcli connection delete "<NAME>"
```

The wizard polls `/state` every 7 s so SSH-driven changes show up in
the UI without a manual reload.

**Hidden SSIDs not supported in v1** — deferred per PLAN.md "WiFi
management — hidden SSID support". `nmcli dev wifi list` doesn't
return them; would need a manual "Connect to a hidden network" form
that posts SSID + PSK with `hidden yes`.

**Scanning returns only the connected SSID? Known Pi 5 brcmfmac
firmware bug.** When the kernel logs `brcmf_cfg80211_scan:
Scanning suppressed: status (4)` continuously and the per-phy
regdom is stuck at `country 99: DFS-UNSET`, that's the
`BRCMF_SCAN_STATUS_SUPPRESS` bit getting stuck on after a DHCP
exchange or Bluetooth-coexistence event. The driver returns
`-EAGAIN` to every scan request until the bit clears, but the
closed-source chip firmware on the Pi 5 doesn't always clear it.

The standard documented fix (`cfg80211.ieee80211_regdom=US` in
`/boot/firmware/cmdline.txt`, written by Pi Imager + `raspi-config
nonint do_wifi_country`) sets cfg80211's global regdom but
doesn't always propagate to the chip's per-phy regdom. We
verified this on a Pi 5 — cmdline has the right value, global
regdom = US, but phy0 stays at country 99. Nobody has a clean
fix per the [Raspberry Pi forum thread on this exact
issue](https://forums.raspberrypi.com/viewtopic.php?p=2371774):
*"there is no definitive upstream patch since the firmware is
closed-source."*

`jasper-doctor`'s `check_wifi_regdom` reports the stuck state.
Diagnostic:

```sh
sudo iw reg get | grep -A1 'phy#0'
# Healthy: country US: DFS-FCC   (or DE / GB / etc.)
# Stuck:   country 99: DFS-UNSET
```

Workarounds with real trade-offs: reload brcmfmac (drops WiFi;
[OpenWrt #23069](https://github.com/openwrt/openwrt/issues/23069)
documents the chip wedging after repeated reloads on Pi 5),
`sudo rpi-update` (newer firmware may help, may regress other
things), external USB WiFi dongle (100% works, hardware change).

**WPA-Enterprise (802.1X) not supported.** Home networks only. The
scan-list filter shows "WPA-Enterprise" as the security label so the
user knows why connecting won't work, but the Connect panel doesn't
expose cert/identity fields.

---

## Transit configuration — read first

The subway, bus, and Citi Bike tools (`get_subway_arrivals`,
`get_bus_arrivals`, `get_citibike_status`) are configured via the
wizard at `http://jts.local/transit/`. The wizard owns **every**
transit env var:

- `JASPER_TRANSIT_LAT`, `JASPER_TRANSIT_LON`,
  `JASPER_TRANSIT_DISPLAY_NAME` (wizard scaffolding; user's geocoded
  home, ~110 m precision)
- `JASPER_SUBWAY_STATION_ID`, `JASPER_SUBWAY_DEFAULT_DIRECTION`
  (empty = both directions)
- `JASPER_MTA_BUSTIME_KEY`, `JASPER_BUS_STOPS` (multi-stop list
  formatted as `id|label,id|label`, parsed by `jasper.bus.parse_bus_stops`)
- `JASPER_CITIBIKE_STATIONS` (multi-station, same `id|label,id|label`
  shape as bus, parsed by `jasper.citibike.parse_saved_stations`),
  `JASPER_CITIBIKE_EBIKE_ONLY` (`"1"` to suppress classic-bike
  counts in voice answers; empty / `"0"` reports both kinds)

All live in **`/var/lib/jasper/transit.env`** at mode 0640 — same
single-source-of-truth pattern as `voice_provider.env`. Never put
them in `/etc/jasper/jasper.env`. `install.sh`'s
`migrate_transit_config` moves any stale operator-set values into
the wizard file on every deploy. `jasper-voice.service` sources
both files with the wizard file last so it wins on conflicts.

**Subway behavior.** "Next train" returns every line stopping
at the station — including trains rerouted from other lines during
service changes. This works because Subway Now's `/api/stops/{id}`
endpoint aggregates across all 7 MTA GTFS-RT feeds server-side
(an N rerouted onto D tracks at a D station appears in the same
response as the regular Ds). The nyct-gtfs fallback can only see
the station's CSV-documented lines (no reroutes during fallback —
documented degradation; Subway Now outages are rare). See
[`jasper/subway.py`](jasper/subway.py) docstring for the full
prior-art chain.

**Two-step bus flow.** MTA BusTime requires a free API key
(register at the wiki — link in the wizard). The bus card is
**locked** until a key is pasted: nothing else renders, because
the stops-lookup endpoint itself needs that key. Saved → re-render
unlocks the picker. The unlocked card shows nearby stops grouped
by intersection (opposing-direction stops at one corner cluster);
**check multiple stops** if you want voice answers covering both
directions. Each arrival in the voice answer names its
`stop_label` so you can tell which bus is at which stop.

**Routes per stop come from SIRI, not OBA.** MTA's OBA
`stops-for-location` returns GTFS-static-scheduled routes only —
lagging real-world dispatch (e.g. B70 was rerouted via 4 Av/39 St
in 2023 but OBA still listed only B35 for that stop). The wizard
SIRI-probes each candidate stop in parallel during render to
enumerate the routes actually dispatching there. OBA's `routes`
field is the fallback when SIRI is silent (off-peak quiet stop).

**External-config soft-unlock.** If `JASPER_MTA_BUSTIME_KEY` is in
`os.environ` (from a hand-edited `/etc/jasper/jasper.env`) but not
yet in `transit.env`, the wizard renders the bus card unlocked
with a yellow notice — values are visible, save moves them into
the wizard's owned file.

**Citi Bike flow.** Keyless (GBFS is public CDN at
`gbfs.citibikenyc.com`). Card unlocks as soon as the user's
coordinates are inside the bbox (NYC + Jersey City + Hoboken).
A household-wide "Only mention e-bikes" toggle sits above the
multi-station picker; check it when the household only rides
e-bikes and the classic-bike count is noise. Each picker row
shows a live snapshot (`{classic} classic, {ebikes} e-bikes,
{docks} docks`) so users pick informed; the voice tool re-fetches
every time so the snapshot is informational only. **GBFS feeds
are cached in-process** at [`jasper/citibike.py`](jasper/citibike.py)
(30 s for `station_status`, 1 h for `station_information`) with
**stale-on-error**: a transient GBFS outage serves the last cached
copy at WARN log level — the voice answer degrades to "as of a
few minutes ago…" rather than going silent. Per-station response
includes `last_reported_age_seconds` so the LLM can disclose
staleness. **Station drift** (a saved station retired by Lyft)
surfaces as `status="missing"` in the tool response and is logged
at WARN by `CitiBikeClient.get_status`; `jasper-doctor`'s
`check_citibike` flags drift at boot/probe time. Full design
in [`docs/HANDOFF-transit-citibike.md`](docs/HANDOFF-transit-citibike.md).

**Address geocoding** runs against OSM Nominatim (Photon as
fallback). No API key, but the policy requires a descriptive
User-Agent + 1 req/sec throttle — both enforced in
[`jasper/transit/geocode.py`](jasper/transit/geocode.py). The
address is never persisted; only the resulting coords (3 decimals)
land in `transit.env`. The wizard discloses this inline next to
the address field.

**Modular provider registry.** The discovery layer (bbox, find-
stops-near, credential probe) is fully data-driven from `REGISTRY`
at [`jasper/transit/__init__.py`](jasper/transit/__init__.py).
Adding a new city or transit system also touches **four** other
spots — none of them load-bearing for the abstraction, but you'll
need to know they exist:
  1. New provider module under [`jasper/transit/providers/`](jasper/transit/providers/)
  2. One line in REGISTRY
  3. One `elif p.id == "<slug>":` branch in
     `_index_html` at [`jasper/web/transit_setup.py`](jasper/web/transit_setup.py)
     (each provider's wizard card is bespoke — subway has a direction
     radio, bus has the locked-on-key flow)
  4. A `make_<slug>_tools(client)` factory under
     [`jasper/tools/`](jasper/tools/) wired into `voice_daemon.py`'s
     tool registration list
  5. The `keys=(...)` bash array in `migrate_transit_config` at
     [`deploy/install.sh`](deploy/install.sh:624) — duplicates
     `transit.all_env_keys()` because install.sh runs before Python
     is available

See `nyc_subway.py` (keyless, CSV-backed) and `nyc_bus.py`
(credentialed, REST-backed) for the two shapes. The registry's own
module docstring at `jasper/transit/__init__.py` walks through these
5 steps in more detail.

**Refreshing subway data.** The bundled CSV at
[`jasper/data/mta_stations.csv`](jasper/data/mta_stations.csv) is
regenerated by `bash scripts/refresh-mta-stations.sh` from
data.ny.gov dataset `39hk-dx4f`. The refresh **preserves
hand-curated direction labels** when the existing CSV has them —
MTA's official labels are sometimes bland ("Southbound") where a
destination-anchored label ("Coney Island") makes voice answers
materially better. Edit by hand to override; the next refresh
keeps your edits.

**Transit nudge.** When the daemon boots with neither subway,
bus, nor Citi Bike configured, `_build_system_instruction` in
[`jasper/voice_daemon.py`](jasper/voice_daemon.py) appends a
conditional instruction redirecting transit questions to
`jts.local/transit`. Conditional ("if the user asks about the
next train, say X") not absolute ("never answer transit
questions"), per the provider-prompt-guide rule. Partial
configurations (e.g. subway set, Citi Bike not) don't fire the
nudge — the registered tools answer what's configured; the
absent tools just aren't visible to the model.

---

## Home Assistant integration — read first

The speaker delegates smart-home control ("turn on the bedroom
lights", "good night", "bedroom medium" → custom automation) to
whatever Home Assistant the household has on the LAN. JTS is a
relay: captures the utterance, hands it to HA's conversation
pipeline, speaks back what HA returns. HA owns NLU, entity
resolution, sentence triggers, automation dispatch — everything
that makes household-specific phrases work. Full architecture in
[`docs/HANDOFF-homeassistant.md`](docs/HANDOFF-homeassistant.md).

### Configure

Wizard at `http://jts.local/ha/`. Three states (mirrors
`/spotify/`'s shape):

1. No URL → "Find Home Assistant" mDNS scan or manual URL entry
2. URL set, no token → paste a Long-Lived Access Token from
   `<HA URL>/profile/security`
3. Connected → status card + test button + agent picker + disconnect

Persists to `/var/lib/jasper/home_assistant.env`:

```sh
JASPER_HA_URL=http://homeassistant.local:8123
JASPER_HA_TOKEN=eyJ0eXAiOi…
JASPER_HA_AGENT_ID=         # optional, empty = HA's default agent
JASPER_HA_VERIFY_SSL=0      # optional, only written when user accepts
                            # a self-signed cert in state 2. Wizard
                            # renders the checkbox only for https://
                            # URLs.
```

Both URL and token must be set for `home_assistant` to register as a
voice tool. When either is missing, the tool isn't visible to the
model and smart-home requests get answered conversationally
("smart-home isn't set up — visit jts.local/ha").

### Why the REST conversation API, not MCP

Verified against HA core source: HA's MCP server cannot trigger
automations (no `automation.trigger` tool in its catalogue;
`HassTurnOn` against an `automation.*` entity *enables* the
automation rather than running it). Sentence triggers (the
`trigger: conversation` automation pattern — HA's documented
mechanism for household phrases) only fire through
`default_agent._async_handle_message`, never through MCP. So MCP
loses two critical surfaces a household has set up. See
[`docs/HANDOFF-homeassistant.md`](docs/HANDOFF-homeassistant.md)
"Why the conversation API, not MCP" for the full case.

### Debug

```sh
# Tool registration at daemon startup:
journalctl -u jasper-voice | grep "home_assistant:"
# →  home_assistant: enabled url=http://... agent_id=(default)
# OR home_assistant: disabled (set JASPER_HA_URL + JASPER_HA_TOKEN...)

# Per-call structured events — six outcome buckets
# (ok / network / timeout / auth / agent_error / intent_miss / parse_error):
journalctl -u jasper-voice | grep "event=ha\.call"

# Live state from jasper-control:
curl -s http://jts.local:8780/state | jq .home_assistant

# Diagnostic check (skip-if-not-configured):
sudo /opt/jasper/.venv/bin/jasper-doctor | grep "Home Assistant"

# Dashboard card on http://jts.local/system/ shows:
#   ✓ Connected to <name> (<version>) — green
#   ✗ Unreachable + error detail — red
#   Not configured
```

### Common gotchas

- **`conversation_id` TTL ≈ 5 min idle**, observed not contractual.
  HAClient caches with a 4-min safety margin; HA may rotate the ID
  silently on each response, we treat what comes back as canonical.
  After daemon restart the conversation context resets — fine, HA
  mints a fresh ID.
- **`agent_id` parameter is undocumented in HA's REST API surface**
  but functional. A future HA schema-tightening could break us.
  Regression test asserts the field is accepted.
- **Footgun:** POST to `/api/conversation/process`, NOT
  `/api/services/conversation/process`. The latter returns no
  response body (HA core issues #93754, #104122 — live in 2026).
- **HA's `response_type=error` returns HTTP 200**. Caller must
  inspect the body, not just the status code. Covered by
  `HAClient._parse` — outcome bucket is `intent_miss`.
- **`no_valid_targets` is NOT a hard error**. In multi-satellite
  homes, another device may have answered the same utterance. HA's
  speech text is still useful to surface; we speak it.
- **LLM-backed HA agents add 1-3 s latency**. If the household has
  HA's default conversation agent set to OpenAI Conversation /
  Anthropic / Google, every smart-home command pays for two LLM
  hops (ours + theirs). To bypass: set `JASPER_HA_AGENT_ID=
  conversation.home_assistant` in the wizard's Advanced disclosure
  → JTS routes to HA's rule-based agent directly.

### Switch / disconnect from the laptop

No `switch-home-assistant.sh` helper yet (planned v1.1+). Manual
paths for now:

```sh
# Re-test without opening the wizard:
ssh pi@jts.local 'sudo /opt/jasper/.venv/bin/jasper-doctor' | grep "Home Assistant"

# Disable (preserves recent URLs for one-tap reconnect):
ssh pi@jts.local 'sudo rm -f /var/lib/jasper/home_assistant.env \
  && sudo systemctl restart jasper-voice'
```

---

## Mic mute — persists across restarts

User-driven mic mute is a privacy promise. When on, the wake loop
drains mic frames without feeding wake detection or any session.
State persists to `/var/lib/jasper/mic_mute.env`
(`JASPER_MIC_MUTED=0|1`, mode 0644, atomic tempfile+rename) so it
survives every daemon restart — deploys, web-wizard saves, watchdog
timeouts, AEC reconciler events, full Pi reboots. Before
[PR #119](https://github.com/jaspercurry/JTS/pull/119) the flag was
in-memory only and silently un-muted on any of those events.

Two ways to toggle (no voice tool — see footnote):

- **Dashboard** — `http://jts.local/system/`, mic chip on the top
  card. Reads the persisted state via `/state`, so it reflects the
  truth immediately after a restart.
- **HTTP** on `jasper-control` (port 8780):

  ```sh
  curl -s http://jts.local:8780/mic                          # read
  curl -s -X POST http://jts.local:8780/mic/mute \
       -H 'Content-Type: application/json' \
       -d '{"muted":true}'                                   # mute
  curl -s -X POST http://jts.local:8780/mic/mute \
       -H 'Content-Type: application/json' \
       -d '{"muted":false}'                                  # unmute
  ```

**Fail-safe direction**: a missing, unreadable, or malformed
`mic_mute.env` resolves to **unmuted** at boot. Better the speaker
respond than be silently deaf because of one bad byte on disk.

**On boot when restored as muted**, jasper-voice logs a single
`mic mute: restored from /var/lib/jasper/mic_mute.env (mic is muted
at startup)` line. If wake stops responding after a deploy/reboot,
check this first.

**No voice tool by design.** "Hey Jarvis, mute the mic" would
create a one-way trap — once muted, wake detection is off, so the
user couldn't say "Hey Jarvis, unmute" to get back. Toggle via the
dashboard or HTTP endpoint, never via the assistant itself.

---

## Gemini model switching — read first

**Preferred model: `gemini-3.1-flash-live-preview`** (latest Live
API model). Do NOT use the plain `gemini-2.5-flash` (it's not a
Live model — `Live API: Not supported` per
https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash).

**Acceptable fallback: `gemini-2.5-flash-native-audio-preview-12-2025`**
— Google's docs explicitly position 3.1 Flash Live as the
*successor* of 2.5 native-audio (see "Migrating from Gemini 2.5
Flash Live" section at
https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview).
Same Live API, same `client.aio.live.connect()` SDK path, same
prebuilt voice catalog, same `send_realtime_input(audio=Blob)`
shape. Use it when 3.1 Live Preview is silently failing for the
project (a real Google-side condition we've hit — server accepts
the WebSocket, accepts audio, sends nothing back; not surfaced
as an error in the SDK).

**Switch command** (laptop-side wrapper, SSHs to the Pi):

```sh
bash scripts/switch-gemini-model.sh        # show current model
bash scripts/switch-gemini-model.sh 3.1    # → gemini-3.1-flash-live-preview
bash scripts/switch-gemini-model.sh 2.5    # → gemini-2.5-flash-native-audio-preview-12-2025
```

The script flips `JASPER_GEMINI_MODEL` in `/etc/jasper/jasper.env`
and restarts `jasper-voice`. No code changes needed because the
daemon treats the model as opaque-string config.

**Symptoms that mean "Gemini Live is silently broken, switch to 2.5"**:

- Sessions repeatedly end with `0 input_tokens / 0 output_tokens`
  AND the daemon's `SILENT FAILURE: sent N bytes... received 0
  chunks back` warning is firing.
- Direct probe (text turn via `send_client_content`) returns no
  responses within 15s and no exception.
- Same-key non-Live `client.models.generate_content(...)` works
  (rules out auth/key issue).

When 3.1 Live unsticks, run `switch-gemini-model.sh 3.1` to flip
back.

---

## librespot — one-time OAuth claim for cold-start voice

`spotify_play "X"` from silence (no AirPlay carrying Spotify) needs
the Pi's librespot to be authenticated to a Spotify account, because
the voice tool calls `start_playback(device=JTS)` via the Web API and
JTS only appears in an account's `sp.devices()` list once that
account has logged in to it.

Two ways to authenticate librespot:

1. **Phone tap** — open Spotify on any device on the LAN, tap the
   device picker, select JTS once. The credential is then cached at
   `/var/cache/librespot` (via `--system-cache` in the systemd unit)
   and survives librespot restarts.
2. **Laptop-side OAuth script** — no phone needed:

   ```sh
   bash scripts/claim-librespot.sh
   ```

   SSH-tunnels librespot's hardcoded `127.0.0.1:8091` OAuth callback
   port to your laptop, runs `librespot --enable-oauth`, opens the
   Spotify auth page in your browser, writes credentials to the same
   `--system-cache` path. Same end state as the phone tap, just no
   phone involved.

Either path is one-time per librespot identity. After that, voice
cold-starts work indefinitely until the cache is cleared.

**Multi-user caveat**: librespot can only be logged in as one user
at a time. The household member whose account is currently cached
is the one voice cold-starts will play through. Other members can
still use their phone's Spotify Connect to claim JTS ad-hoc — that
overwrites the cache for that session, and they can also claim it
back when they want voice to play through their account. Per-user
librespot instances ("JTS-Jasper" / "JTS-Brittany") OAuth-locked
to each account is the deeper fix; deferred until the friction
actually bites.

---

## AEC bridge — reconciler toggle

Software AEC is **built by default and managed by the reconciler**:
it runs automatically only when `JASPER_AEC_MODE=auto` and the
configured AEC mic is present with 6-channel firmware. README's
"Acoustic echo cancellation" section covers the engine (WebRTC
AEC3 via the `jasper_aec3` pybind11 binding, −15 to −18 dB on
music with the production REF_GAIN/MIC_GAIN tunings) and the
~110 MB RAM cost. The full investigation is in
[`docs/HANDOFF-aec.md`](docs/HANDOFF-aec.md); the chip-side
canonical reference (firmware variants, mixer state, failure
modes, diagnostic cookbook) is
[`docs/HANDOFF-xvf3800.md`](docs/HANDOFF-xvf3800.md).

**Three layered bridge bugs were fixed on 2026-05-19.** Together
they had been silently corrupting AEC's reference signal since
the bridge shipped: (1) ALSA's linear resampler dropping HF
content in the 44.1→48 plug; (2) `_aec_loop` falling back to
`silence` when `ref_q` was empty (50 % of frames); (3) drain-
newest discarding the older frame in each ALSA burst, producing
byte-identical duplicate frames. All fixed in PRs #150, #154,
#157. Wake-rate baseline data from before 2026-05-19 is invalid
for evaluating AEC's contribution (the AEC-OFF / chip-direct
legs remain valid). See [docs/HANDOFF-aec.md](docs/HANDOFF-aec.md)
"Bridge ref starvation bug — fixed (2026-05-19)" for the full
diagnosis. The verification script
`scripts/verify-ref-no-silence-bug.sh` confirms the fixes are
active on any deployed build.

**NS=low + AGC1 — 2026-05-20 production tuning.** Post-ref-fix
wake-rate sweep surfaced two more knobs that moved the needle.
Both are now production defaults:
- `JASPER_AEC_NS_LEVEL=low` (was `moderate`) — less aggressive
  noise suppression. The wake model relies on HF speech
  consonants and aggressive NS strips them. Sweep on 2026-05-20:
  NS=low 5/20 vs prev NS=moderate 4/20 in the same data.
- `JASPER_AEC_AGC1_ENABLED=1` — WebRTC AGC1 in `kAdaptiveDigital`
  mode replaces static `MIC_GAIN_DB` for level normalization.
  Same wake rate as static +12 dB, but uniform output across
  utterances (fixes "some Jarvises overblown, some too quiet").

Knobs (env-controllable, `/etc/jasper/jasper.env`):
- `JASPER_AEC_NS_ENABLED` (default 1)
- `JASPER_AEC_NS_LEVEL` (default `low`; one of `low / moderate /
  high / very_high` — Trixie's libwebrtc doesn't expose
  `kVeryLow`)
- `JASPER_AEC_AGC1_ENABLED` (default 0 in binding; production
  has 1 in env)
- `JASPER_AEC_AGC1_TARGET_DBFS` (default 9)
- `JASPER_AEC_AGC1_MAX_GAIN_DB` (default 18)
- `JASPER_AEC_AGC2` documented as no-op on this libwebrtc;
  recommended off

Bridge startup log confirms the live config:
`engine=aec3 ns=on/low agc1=on(target=9,max=18dB) agc2=off ...`

**Prerequisite**: the XVF chip must be on the 6-channel firmware
variant — the bridge reads raw mic 0 from channel 2 of the chip's
USB capture, which only exists on that variant. The known-good
filename + repo hash are tracked in
[`jasper/mics/xvf3800.py`](jasper/mics/xvf3800.py); as of
2026-05-15 that's `respeaker_xvf3800_usb_dfu_firmware_6chl_v2.0.8.bin`,
the only 6-channel variant in upstream `master`. Check the
[upstream firmware directory](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/tree/master/xmos_firmwares/usb)
before flashing in case a newer one has shipped. If unsure
whether the chip is currently on 6-ch, check with:

```sh
# Pin to the Capture: section — Playback (Channels: 2) comes first
# in the file, so a naive `grep Channels:` returns the wrong value.
awk '/^Capture:/{c=1} c && /Channels:/{print; exit}' /proc/asound/Array/stream0
# Expect "Channels: 6"
```

DFU flash procedure is in [`BRINGUP.md`](BRINGUP.md) Phase 2A.5.
The reconciler also self-heals the post-flash ALSA mixer mute trap
(2-ch → 6-ch firmware can leave kernel-side ch2-5 muted across
reboot via `alsactl restore`); `jasper-doctor` flags drift under
"XVF mixer state".

To enable on the Pi (assumes 6-ch firmware already flashed):

```sh
printf 'JASPER_AEC_MODE=auto\n' | sudo tee /var/lib/jasper/aec_mode.env
sudo systemctl start jasper-aec-reconcile
```

`install.sh` enables and runs `jasper-aec-reconcile` automatically.
The reconciler is the source of truth for AEC mode: in `auto`, it
selects `JASPER_MIC_DEVICE=udp:9876` only when the configured AEC mic
(`JASPER_AEC_MIC_DEVICE`, default `Array`) is present with 6-channel
firmware. If the Array is absent after a previous AEC-enabled boot, it
clears stale UDP back to a direct mic candidate and stops voice rather
than letting it watchdog-loop on an unfed socket. Future direct mics can
be added to `JASPER_MIC_DEVICE_CANDIDATES` without changing this logic.

The bridge→voice transport is UDP localhost (`udp:9876`) since
May 2026; the prior snd-aloop `LoopbackAEC` topology was retired
for resilience reasons — see
[`docs/HANDOFF-resilience.md`](docs/HANDOFF-resilience.md).

To disable:

```sh
printf 'JASPER_AEC_MODE=disabled\n' | sudo tee /var/lib/jasper/aec_mode.env
sudo systemctl start jasper-aec-reconcile
```

Verify with `sudo /opt/jasper/.venv/bin/jasper-doctor` either way.

These commands are duplicated in [`BRINGUP.md`](BRINGUP.md) Phase
2A.2 — keep both in sync.

The chip control library (`jasper.xvf.xvf_host`) is useful for
diagnostics regardless of bridge state. **Never call
`SAVE_CONFIGURATION`** — brick hazard on certain firmware
versions (respeaker repo issue #8).

---

## Wake-event telemetry — capture + labeling

Every wake-word fire (and the funnel that follows) lands in a
SQLite DB + per-event audio clips at
`/var/lib/jasper/wake-events/`. Used for: knowing your actual
wake rate, which AEC leg is firing more, building a labeled
corpus for future model training.

Full design, schema, queries:
[`docs/HANDOFF-wake-telemetry.md`](docs/HANDOFF-wake-telemetry.md).

### Enable the dual-stream OR-gate

Default is single-stream (AEC ON only). The OR-gate fires wake
on **either** AEC ON or AEC OFF crossing threshold — recovers the
~60 % of wakes the `jarvis_v2` model silently misses on the AEC ON
leg (the documented 0.001-confidence failure mode). Enable with:

```sh
echo 'JASPER_MIC_DEVICE_RAW=udp:9877' | sudo tee -a /etc/jasper/jasper.env
sudo systemctl restart jasper-voice
```

Verify: `journalctl -u jasper-voice | grep UdpMicCapture` shows
both 9876 + 9877 binding. After a wake, the log line carries both
legs' scores: `event=wake.detected leg=off score_on=0.00 score_off=0.82`.

### Pull the corpus to laptop for review

```sh
bash scripts/fetch-wake-events.sh        # pops Finder open on macOS
```

Lands as `./wake-events/<UTC-timestamp>/`:
- `wake-events.sqlite3` — consistent snapshot via `.backup`
  (jasper-voice keeps writing; the snapshot is safe to read)
- `<event_id>.aec-on.wav` + `<event_id>.aec-off.wav` — 6 s windows
  (4 s pre + 2 s post wake fire)
- `index.csv` — newest-first metadata table (open in Numbers /
  Excel / Sheets); includes per-leg peak scores, peak offsets,
  RMS levels, music context, bridge config, funnel outcome
- `index.tsv` — same content as TSV (grep-friendly)
- `wake-events/latest` symlink updated each run

Skip the Finder pop-up with `NO_OPEN=1 bash scripts/fetch-wake-events.sh`.

### Sanity-check the corpus

```sh
bash scripts/audit-wake-events.sh
```

Runs three checks on `wake-events/latest`:

1. WAV integrity — format, duration, near-silent detection
2. Per-event AEC ON vs AEC OFF parity — duration match, RMS
   comparison, cross-leg time-alignment via speech-band xcorr
   (typical ≈+14 ms reflects AEC3 processing latency)
3. DB column-by-column populated count — catches "field never
   written" bugs like the AEC OFF capture-ring fill bug shipped
   in the initial dual-stream integration

Re-run after every fetch; takes ~2 s.

### Label an event

```sh
sqlite3 wake-events/latest/wake-events.sqlite3 \
  "UPDATE wake_events SET label='real_attempt' WHERE event_id='...'"
```

Suggested labels: `real_attempt`, `music`, `tv`, `ambient`,
free-form. Empty by default — fill as you listen. `label_notes`
column for longer commentary.

### Quick funnel query

```sh
sqlite3 wake-events/latest/wake-events.sqlite3 "
SELECT date(ts_utc) day, COUNT(*) wakes,
       SUM(ts_turn_opened IS NOT NULL) opened,
       SUM(ts_speech_detected IS NOT NULL) had_speech,
       SUM(ts_turn_complete IS NOT NULL) completed
FROM wake_events WHERE trigger_kind LIKE 'fire%' GROUP BY day"
```

More queries (per-leg fire breakdown, false-positive proxy, etc.)
in the HANDOFF doc's "Useful queries" section.

### Retention + privacy

- WAVs: 500 MB ring buffer, oldest-first deletion (~3-6 weeks at
  typical use). Tunable via `JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES`.
- DB rows kept forever. When audio rolls off, the row's
  `audio_*_path` becomes the literal string `'rolled_off'` (not
  NULL — preserves the historical fact that audio existed).
- Mute mic privacy preserved: when `JASPER_MIC_MUTED=1`, the
  wake-event capture rings stop filling — nothing recorded.

### Architecture in one paragraph

The AEC bridge emits **two** UDP streams: the post-AEC mono mic on
`:9876` (existing) and the chip-direct mic (pre-AEC, post chip's
own BF/NS/AGC/HPF) on `:9877`. jasper-voice opens both, runs
independent `WakeWordDetector` instances per leg, OR-gates the
fires with a shared 0.7 s refractory. `WakeEventStore`
(`jasper/wake_events.py`) writes to SQLite at wake-fire +
each funnel stage transition; a fire-and-forget task waits 2 s
post-fire then snapshots both capture rings and writes the WAVs.
Telemetry is **fail-soft** everywhere — store failures log at WARN
and never block wake / session paths.

---

## shairport-sync AP2 wedge — auto-recovers

shairport-sync's AirPlay 2 control plane occasionally wedges with the
process alive but unable to accept new SETUPs (Pi shows in the picker
but "cannot connect"). Closest upstream report:
[shairport-sync#2024](https://github.com/mikebrady/shairport-sync/issues/2024).
No upstream code fix.

The Tier 3 supervisor at
[`jasper/control/shairport_supervisor.py`](jasper/control/shairport_supervisor.py)
probes RTSP `OPTIONS *` on `127.0.0.1:7000` every 30 s; after 3
consecutive failures, gated on no active session, it restarts
shairport-sync + nqptp. Detection latency ~90 s. The gate ensures
live sessions aren't disrupted; a 10-minute rate limit prevents
restart storms.

Manual fix (still works, faster than the 90 s window):

```sh
bash scripts/airplay-reset.sh
```

Observability:

```sh
curl -s http://jts.local:8780/state | jq .resilience.shairport
ssh pi@jts.local 'journalctl -u jasper-control | grep event=shairport'
```

Off switch: set `JASPER_SHAIRPORT_SUPERVISOR=disabled` in
`/etc/jasper/jasper.env` (exact match, case-insensitive), then
restart `jasper-control`. Other values like `off` or `0` log a
warning and stay enabled.

Design rationale: [`docs/HANDOFF-resilience.md`](docs/HANDOFF-resilience.md)
(Tier 3).

---

## Satellite devices — opt-in hardware

The cross-cutting design home for ESP32 satellites (existing rotary
dial, AMOLED touchscreen mic satellite in progress, future devices)
lives in [`docs/satellites.md`](docs/satellites.md). It owns shared
protocols, multi-mic arbitration design, and per-device roadmap. Read
that first when working on satellite firmware or related Pi-side
daemons.

### Rotary dial

The CrowPanel 1.28" HMI ESP32-S3 rotary dial is a wireless physical
controller that talks to the Pi over WiFi. **Currently working
end-to-end on hardware:** volume control via encoder with an
on-screen volume gauge, transport toggle on short-press (play/pause),
hold-to-talk Gemini session on long-press. The other LVGL scenes
(clock face, listening orb, speaking waveform, now-playing card with
album art) have firmware scaffold but aren't yet validated on-device.

Pi side: `jasper-control` daemon binds `0.0.0.0:8780`, exposes
`POST /volume/adjust` (and `/volume/set`, `/healthz`). Volume
requests route through `VolumeCoordinator` (see
[`docs/HANDOFF-volume.md`](docs/HANDOFF-volume.md)), which dispatches
to the active source's own slider (AirPlay DBus, Spotify HTTP, BT
DBus) — not just CamillaDSP. Persistence is incidental — voice_daemon's debounced poller
catches external main_volume changes and writes them to the same
state file used by voice tools, so dial-driven volume survives
restarts without the control daemon knowing about the persistence
layer. Service file at `deploy/systemd/jasper-control.service`.
No auth — home LAN only.

Dial side: PlatformIO project at `firmware/dial/`. ESP32-S3, native
USB-CDC, Improv-over-Serial provisioning. WS2812 LED 0 = status
indicator (magenta=boot, yellow=connecting, dim green=online,
red blink=HTTP error, solid red=WiFi down).

To onboard a fresh dial, end-to-end:

```sh
# One-time, on any machine with PlatformIO (or via the Pi venv):
bash firmware/dial/build.sh
# Stages bin to /opt/jasper/firmware/dial/jasper-dial.bin

# Plug the dial into a Pi USB-C port, then on the Pi:
sudo /opt/jasper/.venv/bin/jasper-dial-onboard
# → flashes via esptool, reads Pi's current WiFi creds from
#   NetworkManager (or wpa_supplicant), pushes via Improv,
#   waits for dial to appear at jasper-dial.local. ~30 s.

# Unplug from Pi and connect to USB power. Dial reconnects to
# WiFi from NVS flash on every subsequent boot.
```

To re-provision after a WiFi password change: same command, same
USB plug. The dial accepts `SUBMIT_SETTINGS` over Improv whenever
it's connected to USB.

If the dial is already flashed and you just need to update creds,
pass `--no-flash`. If auto-detection of WiFi creds fails (locked-down
NM secret store, etc.), pass `--ssid` and `--password` explicitly.

The control daemon is always installed and enabled by `install.sh`,
even if there's no dial — it costs <10 MB RAM idle and the volume
endpoints are useful for any LAN client (Home Assistant, shortcuts,
etc.).

### AMOLED satellite (Phases 0, 1.1, 1.2 done; 1.3+ in progress)

Waveshare ESP32-S3-Touch-AMOLED-1.8 — touchscreen + mic satellite.
Project at `firmware/satellite-amoled/`. Both ESP32 firmware projects
(dial + satellite) on **Arduino-ESP32 v3.x via pioarduino** — see
`docs/satellites.md` "Toolchain — Arduino-ESP32 v3.x via pioarduino"
for the rationale and v2.x→v3.x deltas.

Shipped:
- Phase 0 (2026-05-08) — ES8311 mic capture, 16 kHz mono PCM over
  USB-CDC. Validated against music playback. See
  `docs/satellites.md` "Audio init footguns" for the non-obvious
  ES8311 init quirks (I²S stereo + demux for slot alignment;
  REG02 pre_multi=3 for SCLK-derived MCLK).
- Phase 1.1 (2026-05-08) — WiFi join from NVS-stored creds,
  Improv-over-Serial provisioning, mDNS-SD discovery of
  `_jasper-control._tcp`, dlog over USB-CDC + UDP `:5514`.
- Phase 1.2 (2026-05-09) — on-screen connection-status indicator
  on the 368×448 SH8601 AMOLED via Arduino_GFX. Direct draws (no
  LVGL yet); colored circle + label keyed off the `Status` enum;
  `setStatus()` helper redraws inline so PROVISION→ONLINE
  transitions show up immediately. See "Display init footguns"
  in `docs/satellites.md` for the SH8601 + TCA9554 reset
  sequence and Arduino_GFX subclass gotchas.

Next milestone: Phase 1.3+ — capacitive touch (FT3168), LVGL "Tap
to Talk" surface, control-plane HTTP, I²S mic capture gated on
press, UDP audio stream to a new Pi-side `MicSource` endpoint.

**Onboarding flow:** plug the satellite into a Pi USB-C port, then
`sudo /opt/jasper/.venv/bin/jasper-satellite-onboard`. Mirrors
`jasper-dial-onboard`: USB CDC discovery → optional flash from
`/opt/jasper/firmware/satellite-amoled/jasper-satellite-amoled.bin`
(populated by `bash firmware/satellite-amoled/build.sh`) → push
WiFi creds via Improv → wait for `jasper-satellite-amoled.local`.
The flash itself wipes NVS (factory.bin pads 0x0–0x10000 with
0xFF, including the 0x9000–0xe000 NVS region) but the cred-push
that follows refills it — no manual provisioning step.

**Local PIO setup** for the v3.x toolchain (laptop-side):
pioarduino requires Python ≥ 3.10 — the JTS project venv is
3.9 — so build inside a separate Python 3.11 venv with
`brew install python@3.11 && python3.11 -m venv /tmp/jts-pio-venv
&& /tmp/jts-pio-venv/bin/pip install platformio`. Prefix `pio`
invocations with `PATH="/opt/homebrew/bin:$PATH"` so PIO's
subprocess can find git for the Improv-WiFi library install.
The Pi already has Python 3.13 + PIO and builds cleanly without
the dance.

To capture audio for testing or SNR comparisons:

```sh
bash scripts/capture-satellite-amoled.sh 10        # 10 s → captures/<ts>.wav
bash scripts/capture-chip-mic.sh 10                # same shape, from XVF3800
```

Capture scripts assume the satellite is plugged into the Pi via
USB-C and the Pi is at `jts.local`. WAVs land in `captures/` (which
is gitignored — large binaries, regenerate as needed).

---

## Debugging — fetch evidence before guessing

When the user reports "it doesn't work" or asks about Pi-side
behaviour, **before guessing**, fetch the actual logs:

```sh
bash scripts/fetch-pi-logs.sh                # last hour, default Pi at jts.local
SINCE='10 minutes ago' bash scripts/fetch-pi-logs.sh
PI_HOST=192.168.1.42 bash scripts/fetch-pi-logs.sh
```

Output lands in `./logs/`. Read the `*-latest.*` symlinks:

- `logs/jasper-voice-latest.log` — voice daemon (wake events,
  tool calls, Gemini errors, idle timeouts, spend log)
- `logs/jasper-camilla-latest.log` — CamillaDSP (broken pipe,
  format mismatch, websocket connects)
- `logs/jasper-aec-bridge-latest.log` — software AEC bridge
  (only when enabled)
- `logs/combined-latest.log` — interleaved timeline
- `logs/alsa-devices-latest.txt` — `aplay -L` / `arecord -L`
  output. Always sanity-check actual ALSA card names against
  what the configs expect (`A` for Apple dongle, `Array` for
  ReSpeaker, `Loopback` for snd-aloop). The AEC bridge no longer
  has an ALSA output — it sends UDP to `127.0.0.1:9876` since
  May 2026; see [`docs/HANDOFF-resilience.md`](docs/HANDOFF-resilience.md)
- `logs/camilladsp-latest.yml` — current CamillaDSP config on
  the Pi
- `logs/asoundrc-latest.txt` — current `/etc/asound.conf`
  (legacy: `/root/.asoundrc`; migrated 2026-05-23 per PR #223)
- `logs/jasper.env-latest.txt` — current env (secrets redacted)
- `logs/sessions-latest.txt` — last 20 voice sessions with token
  counts and estimated cost
- `logs/systemctl-latest.txt` — `systemctl status` for all units

Live tail (interactive, Ctrl-C to stop):

```sh
bash scripts/tail-pi-logs.sh                # all jasper-* units + renderers
bash scripts/tail-pi-logs.sh jasper-voice   # just one
```

For just the cross-daemon "events" — duck transitions, source
preempts, dial volume routing, wake/turn boundaries — the
`jasper-trace.sh` wrapper filters the live tail down to the
high-signal lines:

```sh
bash scripts/jasper-trace.sh                # default: last 5 min, follow
SINCE='1 hour ago' bash scripts/jasper-trace.sh
```

For a single JSON snapshot of cross-daemon state (voice provider /
session / spend, main_volume_db / listening_level, renderer states,
dial heartbeat), hit jasper-control's `/state` aggregator:

```sh
curl -s http://jts.local:8780/state | jq
```

Each `/state` section fails soft — if a daemon is unreachable, that
section is null instead of the whole call erroring out.

For a one-shot full diagnostic dump (when something's badly
wrong), run on the Pi:

```sh
ssh pi@jts.local sudo bash /home/pi/jts/scripts/pi-bundle.sh
# prints the path to a tarball under /tmp/, scp it back to ./logs/
```

### On the Pi itself

`jasper-doctor` codifies BRINGUP.md's smoke tests:

```sh
sudo /opt/jasper/.venv/bin/jasper-doctor
```

Returns 0 if all critical checks pass. First thing to ask the
user to run when something's broken. The doctor reads
`/etc/jasper/jasper.env` and (if present)
`/var/lib/jasper/voice_provider.env` itself — no need to source
them into the calling shell.

### "The speaker restarted on its own" — hardware watchdog + cross-boot journal

The Pi has the kernel hardware watchdog enabled by Raspberry Pi
OS Trixie's `/usr/lib/systemd/system.conf.d/40-rpi-enable-watchdog.conf`
(`RuntimeWatchdogSec=1m`). When userspace wedges hard enough that
PID 1 can't ping `/dev/watchdog0` for ~60 s, `bcm2835-wdt`
hard-resets the board. This is **Tier 5** of the resilience
ladder (see [`docs/HANDOFF-resilience.md`](docs/HANDOFF-resilience.md)),
intentional for unattended recovery, and the user will perceive
it as "the speaker restarted for no reason."

The boot fingerprint of a watchdog (or any unclean) reset, on
the *recovery* boot:

```sh
sudo dmesg -T | grep "orphan cleanup"
# EXT4-fs (mmcblk0p2): orphan cleanup on readonly fs
```

To find the cause, read the **previous** boot's journal —
persistent journal was enabled in PR #160 specifically so this
works:

```sh
ssh pi@jts.local 'sudo journalctl --list-boots'
# index 0 = current boot, -1 = previous, etc. If only one boot is
# listed and PR #160 has been deployed (check
# `/etc/systemd/journald.conf.d/50-jts-persistent-storage.conf`),
# the Pi only has the post-reset boot history — wait for the next
# event, or read /run/log/journal directly if still up.

ssh pi@jts.local 'sudo journalctl -b -1 -p warning --since "-2min"'
# The 2 minutes before the wedge. Common signatures: OOM-kill log
# lines, hung-task warnings (kernel.hung_task_timeout_secs default
# 120 s), runaway jasper-* daemon, zram thrash.
```

**Self-inflicted wedges from heavy offline analysis.** Running
something like `for i in range(100): Model()` (e.g.
`openwakeword.Model()`) on the Pi can OOM the 2 GB RAM, fill
`zram0`, peg every core on compression, starve PID 1, and trip
the watchdog. Pi 5 is sized for production daemons, not analysis
bursts. For wake-rate sweeps and similar, do it on the laptop
(`pip install openwakeword onnxruntime`) and rsync the captures.

---

## Behavioral rules for working in this codebase

Per the user's CLAUDE.md
(`github.com/jaspercurry/claude-rules`) and reinforced for this
specific project:

- **Diagnose before solving.** If something's broken, fetch the
  logs and point at the specific line that produced the failure
  before proposing a fix.
- **Check prior art.** Existing helpers — `pycamilladsp`,
  `openwakeword`, `google-genai`, `spotipy` — handle most of
  the integration. Don't reinvent.
- **Surgical changes — file ownership.** Our files live under
  `/opt/jasper/`, `/etc/camilladsp/`, `/etc/jasper/`,
  `/etc/modprobe.d/snd-aloop.conf`, `/etc/asound.conf`,
  `/etc/shairport-sync.conf`, `/etc/nginx/sites-enabled/jasper.conf`,
  and `/etc/systemd/system/{jasper-*,librespot,shairport-sync,nqptp,bt-agent}.service`.
  Touch only what you must when modifying these.
- **Renderer ALSA device names must resolve as the renderer's
  runtime user.** When changing a renderer's ALSA device (the
  `--device` in librespot.service, `output_device` in
  shairport-sync.conf, `--pcm` in bluealsa-aplay's drop-in), the
  new name MUST be openable via `sudo -u $USER aplay -D $DEVICE
  -c 2 -r 48000 -f S16_LE -d 0.1 /dev/zero`. This catches the
  PR #214 bug class: a user-space ALSA PCM defined only in a
  root-readable `~/.asoundrc` fails to resolve under non-root
  renderer users (shairport-sync, pi). System-wide PCM defs go
  in `/etc/asound.conf` (mode 0644). `jasper-doctor`'s
  `check_renderer_device_resolvable` runs this probe on every
  install — but verify by hand before relying on it.
- **No silent failure paths.** Any new code path that would
  prevent the speaker from responding to a wake event MUST also
  trigger an audio cue (so the user hears why nothing happened).
  Add cues by appending a `CueDef` to
  [`jasper/cues/registry.py`](jasper/cues/registry.py) and calling
  `cues.play("<slug>")` from the failure handler — see
  [docs/HANDOFF-audible-feedback.md](docs/HANDOFF-audible-feedback.md)
  for the full pattern. Cue text must stay provider-agnostic
  (no "Google" / "Gemini" — voice backend is replaceable).
- **Codify, don't memorise.** If a runtime value matters for the
  speaker to behave correctly, it MUST be set somewhere the next
  fresh Pi will pick up automatically — either as a code default
  in [`jasper/config.py`](jasper/config.py), a seeded value in
  [`.env.example`](.env.example) (with a prose comment explaining
  what it does and why this default), or an explicit step in
  [`install.sh`](deploy/install.sh). Setting a value live on a Pi
  and not codifying it is a hidden runtime dependency — the next
  rebuild silently behaves differently. Same principle applies to
  the wizards: the wizard writes a file to `/var/lib/jasper/`,
  but the wizard itself is the codification, and absence of the
  file MUST fail loudly (not silently default). For env-var
  changes specifically: every `JASPER_*` line added to
  `.env.example` ships with a prose comment block above it (what,
  why this default, recommended ranges if it's tunable). The
  template doubles as documentation — no separate "tuning knobs"
  page.

---

## Testing

Hardware-free tests (run locally, no SDK auth needed):

```sh
.venv/bin/pytest
```

Anything Pi-specific (audio I/O, websocket, Gemini Live) needs
to run on the actual hardware via `jasper-doctor` or by tailing
logs during use.

### Test discipline — required, not optional

The repo has >1000 hardware-free pytest functions. This is a
*production embedded system* and the test suite is what lets us
swap models, change prompts, or refactor without regressions.

**Rules — apply to every PR:**

- **Every new tool** the LLM can call (anything registered via
  `make_*_tools` in [`jasper/tools/`](jasper/tools/)) ships with
  a regression scenario under
  [`tests/voice_eval/regression/`](tests/voice_eval/regression/).
  No exceptions. A tool with no scenario can't be reasoned about
  across model swaps.
- **Every reported behavioural bug** (model hallucinates / skips a
  tool / misroutes / etc.) becomes a regression scenario *before*
  the fix lands. The scenario reproduces the bug; the fix turns it
  green. This is the only way bugs stay fixed across the live
  rebuilds of prompts, model versions, and provider switches.
- **Every new subsystem** ships with hardware-free pytest coverage
  under `tests/test_*.py` — see existing `test_camilla_ducker.py`,
  `test_tools_spotify.py` etc. for the shape. Network calls and
  device I/O are mocked.

What the voice-eval harness is and how to run it:
[`tests/voice_eval/README.md`](tests/voice_eval/README.md). TL;DR
`.venv/bin/pytest tests/voice_eval/regression/` — runs each
scenario 3× (pass^3) against the currently-active voice provider.

### Voice-eval cost discipline — non-negotiable

The voice-eval harness opens **paid** real-time LLM sessions. Cost
ballpark per pass^3 scenario: ~$0.075 (Gemini), ~$0.15 (Grok),
~$0.60 (OpenAI Realtime). Full V1 suite (4 scenarios × 3 trials)
against OpenAI is ~$2.40 per run.

**Rules — apply to every PR and every session that runs the harness:**

- **Never wrap `harness.ask()` in retry loops or `while True`.**
  Failure means investigate the transcript, not re-run.
- **Never auto-rerun on flake.** Same reason. The trace tells you
  why; re-running burns money to learn nothing new.
- **Never use `pytest-repeat` / `--count=N` with N > the per-scenario
  `PASS_K`** without explicit human approval and a stated dollar
  ceiling.
- **Never add the eval suite to CI on every commit.** Nightly at
  most, after the team has reviewed cost in their context.
- **If you're an LLM agent** and the human asks you to "investigate"
  or "loop until passing", **refuse and ask for explicit scope** —
  e.g. "I'll run one trial of one scenario, ~$0.05, and report
  back" rather than open-ended budgets.
- **Announce estimated cost + read-only vs side-effecting status**
  before running anything. The Spotify scenario starts playback;
  the others are read-only.
- **Skip playback-affecting scenarios** when the household is using
  the speaker: `JASPER_VOICE_EVAL_SKIP_PLAYBACK=1`.

---

## Branch and remote

Active branch: `main`. The user's GitHub remote is
`jaspercurry/JTS` — accessible via `mcp__github__*` tools, not
the `gh` CLI.
