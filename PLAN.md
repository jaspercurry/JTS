# JTS — forward roadmap

v1 is shipped (see [README.md](README.md) for the current state).
This document tracks what comes next, in a sequence chosen to
maximize feature-per-week and minimize cross-phase rework.

For the operator-facing "how do I bring this up from scratch" guide,
see [BRINGUP.md](BRINGUP.md). For deep-dives on existing subsystems,
see [docs/HANDOFF-*.md](docs/).

---

## Sequenced roadmap

| v | Adds | Why this order |
|---|---|---|
| **v1.1** | Custom "Hey Jasper" wake-word model, push-to-talk button, daily spend cap UI in management dashboard | Quick wins on top of working v1 |
| **v2** | Built-in **room correction** web tool (FastAPI + sweep + scipy + writes CamillaDSP YAML) | Highest user value; standalone and doesn't need any networking changes |
| **v2.1** | UMIK-1/2 auto-fetch + bundled phone-mic calibration profiles | Strict superset of v2 |
| **v3** | More tools: weather (Open-Meteo, no key), timers (SQLite), calendar (Google OAuth), reminders (Pushcut bridge) | Each is a 30–80 LoC tool; do as a batch |
| **v4** | First-boot **captive portal** via Balena WiFi Connect | Requires NM/dhcpcd swap — lots of integration testing; do once the rest is stable |
| **v5** | **Wireless stereo pair** via Snapcast (Pi Zero 2W slave) | Architecturally clean addition once v1–v3 stable |
| **v6** | **Wireless subwoofer** node + crossover in master CamillaDSP | Strict superset of v5; biggest video story |
| **v7** | Direct device-to-device **mesh** (master AP+STA, slave priority fallback) | Networking polish; only matters at v5+ scale |
| **v8** | **USB gadget** (UAC2) inline DSP mode | Blocked on Pi linux #6289 / #6569 being fixed; lowest priority |
| **v9** | Home Assistant bridge tool (single proxy function) | Optional; opens HA's 3000+ integrations to anyone who already runs HA |

The v1 architecture decisions that protect this sequence:
- **Always-on CamillaDSP** is the pre-req for ducking *and* room
  correction *and* sub crossover *and* per-channel slave correction.
- **Tool decorator + registry** is the pre-req for v3's tool batch
  and v9's HA bridge.
- **48 kHz everywhere** keeps resampling out of the hot path now and
  through Snapcast later.
- **Systemd-managed services in `/opt/jasper`** keep the install
  survivable.

---

## Configuration web view / management dashboard (post-v1, no specific version yet)

Grow the existing `jasper-web` service into a single management
dashboard at `https://<host>.local/` (root). The Spotify OAuth flow
at `/spotify/` becomes the first sub-page; everything below moves
into peer pages under `/settings/`, `/spend/`, `/diagnostics/`, etc.
The audio-cue subsystem already points the user at the dashboard
root: when a wake hits the spend cap, Jarvis says "visit
`{hostname}` to manage" — that landing page has to actually exist
for the cue to be useful, so this work is a soft prereq for cues
graduating from "best we can do for now" to a complete UX.

Settings the dashboard should expose — without SSHing in:

- **Per-account Spotify playlists** ✅ landed 2026-05-07. Each Spotify
  account in `/spotify` has a "Custom playlists" section: paste a
  `https://open.spotify.com/playlist/...` URL or `spotify:playlist:`
  URI, the server fetches the canonical name, persists `uri → name`
  on the account, and the spotify_play tool fuzzy-matches against it.
  Motivated by the 2026 Spotify Web API hiding algorithmic personalised
  playlists (Discover Weekly, Daily Mix, Release Radar, Daylist) from
  both `current_user_playlists` and catalog search owner-filter — see
  [docs/HANDOFF-spotify-personal-playlists.md](docs/HANDOFF-spotify-personal-playlists.md)
  if it ever gets written. First piece of this web-view work to ship.
- **Location** for weather (`JASPER_DEFAULT_LOCATION`, e.g. "Sunset
  Park, Brooklyn" — needs to be specific enough that the geocoder
  doesn't land in the wrong "Sunset Park" in another state)
- **Weather units** (`JASPER_WEATHER_UNITS`: celsius/fahrenheit)
- **Subway** (NYC-specific): `JASPER_SUBWAY_STATION_ID` (GTFS stop
  ID), `JASPER_SUBWAY_LINES`, `JASPER_SUBWAY_DEFAULT_DIRECTION`
- **Mic device** (`JASPER_MIC_DEVICE` — default `Array` for
  XVF3800; would need to be different for other USB mics)
- **Spotify Connect device name** (cosmetic — what shows in the
  Spotify app's device picker)
- **Daily spend cap** in dollars
- **Volume idle-reset behaviour** — a custom default startup volume
  that the speaker uses if `last_used_at` in `speaker_volume.json`
  is older than a configurable threshold. The persistence schema
  already records `last_used_at` per user-initiated change; the
  web UI work just plumbs through two env vars
  (`JASPER_VOLUME_IDLE_THRESHOLD_SEC`, `JASPER_VOLUME_IDLE_DEFAULT_PCT`)
  and a small read of `volume_persistence.regress_listening_level_if_stale`
  arguments.
- **AirPlay reset button** — a one-click action that runs
  `systemctl restart shairport-sync nqptp`. Fixes the recurring
  symptom where the Pi shows up in the Mac's AirPlay picker but
  won't accept connections (or sustains for a half-second then
  drops). Root cause is shairport-sync's AP2 connection state
  getting wedged after abrupt client disconnects — the process is
  alive so `Restart=always` doesn't help; PTP via nqptp can also
  desync independently. Today's recovery is `bash
  scripts/airplay-reset.sh` from a laptop with SSH; a dashboard
  button removes the SSH dependency. Implementation: small POST
  endpoint on jasper-web that shells out to the systemctl command,
  plus a button on the diagnostics page. See
  `project_shairport_ap2_wedge_recovery` (memory) for the full
  symptom/cause writeup.

Same pattern as the existing Spotify OAuth web flow: jasper-web
serves the form, validates input, writes to `/etc/jasper/jasper.env`,
issues `systemctl reload jasper-voice` (or restart). Authentication
is whatever the Spotify flow uses (or none for home-LAN-only
deployments).

This is exactly the kind of thing an end-user shouldn't have to
SSH for. Not blocking anything; flagged as the next polish piece.

---

## Test/dev follow-ups (no version)

Small infrastructure items not blocking any feature; recorded so
they don't get lost in the working tree.

- **`jasper/renderer.py` constructs `asyncio.Lock()` synchronously
  in `RendererClient.__init__`.** Python 3.10+ defers event-loop
  binding until first use, so this is fine on the Pi (3.13). Python
  3.9 + macOS (the local dev venv) binds at construction time and
  raises `RuntimeError: There is no current event loop` once an
  earlier test in the suite has consumed the default loop —
  produces 11 collection errors in `tests/test_renderer.py` on a
  full `pytest` run while passing when those tests are run alone.
  Fix: lazy-construct the lock on first await, or take the loop as
  a parameter. No urgency since the Pi is unaffected and the
  failures are local-only.

---

## Risks worth re-flagging

- **Gemini 3.1 Flash Live is still Preview, not GA.** API can change
  underneath you. Pin `google-genai` SDK version; expect to chase
  one or two breaking changes per upgrade. The `VoiceSession`
  interface limits the blast radius of any churn to a single
  adapter file. Fall back to `gemini-2.5-flash-native-audio-preview-12-2025`
  if 3.1 silently breaks (see CLAUDE.md "Gemini model switching").
- **Gemini tool calling is sequential** (no parallel/non-blocking).
  A slow tool (e.g. Spotify search) will gate the next thing the
  model says. Keep tool implementations fast (5 s timeout, return
  errors quickly).
- **CamillaDSP websocket has no auth.** Bind it to 127.0.0.1 only.
  Don't expose port 1234 on the LAN.
- **Loopback locks rate at first opener.** Pin everything to
  48 kHz / S32LE on the capture side, S16LE on the dongle output
  side. shairport-sync uses `plughw:Loopback,0,0` (not raw `hw:`)
  to absorb the 44.1 → 48k conversion.
- **Long Gemini system prompt breaks session resumption** on the
  3.1 Flash Live preview. Keep system instruction under ~500 tokens.
- **`SetVolume`, not `Reload`, for ducking.** Reload reparses YAML
  and glitches audio mid-stream.
- **Idle billing on Gemini Live**: don't keep the session open
  forever. The current daemon closes after 60 s of silence
  post-last-turn.
