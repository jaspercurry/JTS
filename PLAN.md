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

## Wake-word reliability — AEC tuning roadmap (no version, ongoing)

After the WebRTC AEC3 engine landed (2026-05-08, replacing an
earlier SpeexDSP path that was removed when AEC3 became production),
measured attenuation on music is **−15 to −18 dB mean**. That's
well into "wake-word during music plausible" territory, but at
high SPL the wake-word still sometimes misses. This section tracks
what's left on the menu, ordered by expected leverage / effort.

The current production config (set 2026-05-08): `JASPER_AEC_AGC2=0`,
`JASPER_AEC_REF_GAIN_DB=25`, `JASPER_AEC_MIC_GAIN_DB=6`. See
[`docs/HANDOFF-aec.md`](docs/HANDOFF-aec.md) "Tuning findings" for
the full sweep matrix and reasoning.

### Tier 1 — cheap experiments (≤30 min each)

- **Chip's beamformed ASR channel as bridge input.** We currently
  consume channel 2 (raw mic 0, BYPASS) for clean linear input to
  AEC3. Switching to channel 1 (ASR — post-BF + NS + AGC, tuned for
  speech) gives 6–10 dB of directional speaker rejection from the
  on-chip beamformer for free, before AEC3. Trade-off: chip's AGC
  introduces non-linearity that AEC3's linear filter can't fully
  model. Risk: chip's auto-DoA might aim its beam *at* the speakers
  (loudest source) — measurable in seconds and revertable. Effort:
  one-line change to `MIC_CHANNEL_INDEX` (or env-configurable),
  plus a sweep run.
- **Soft-clip the REF_GAIN path.** Currently `np.clip` hard-clips at
  `JASPER_AEC_REF_GAIN_DB ≥ 25` and injects distortion. Replacing
  with `tanh` soft-limiting (~10 lines NumPy) lets us push to +30
  to +35 dB cleanly, putting loop gain firmly in AEC3's design
  window. Pink-noise sweep showed diminishing returns past +25 dB,
  but on music it's untested.
- **Lower `JASPER_WAKE_THRESHOLD` from 0.5 → 0.4 or 0.3.** Pure UX
  tradeoff knob — more wakes, some false positives. Easy to revert.

### Tier 2 — engineering with real upside

- **AEC3 internal config tuning (`EchoCanceller3Config`).** Research
  pass on 2026-05-08 (sub-agent) identified concrete overrides that
  should move attenuation past the −18 dB ceiling: extend filter
  length 13 → 30 partitions (~83 ms → ~192 ms), enable
  `ep_strength.bounded_erl`, enable `suppressor.use_subband_nearend_
  detection`, lower `dominant_nearend_detection.snr_threshold` 30 →
  20, etc. **Blocked in v1.3-3:** the public headers don't expose
  `EchoCanceller3Factory`, so applying these overrides requires
  either vendoring the private `modules/audio_processing/aec3/
  echo_canceller3.h` from upstream (the symbol is exported by the
  .so but the header isn't shipped) or upgrading to v2.x (not in
  Trixie stable). ~2 hrs to vendor + write a custom factory; needs
  re-checking if upstream layout changes. The research output is
  preserved in this session's transcripts; revisit when the v1.3-3
  → v2.x package transition happens or when engineering effort is
  available.
- **Software beamforming over the chip's 4 raw mics (channels
  2–5).** The cleanest path. Implement fixed-direction delay-and-sum
  or MVDR ourselves, pointed at the user's seated position, instead
  of trusting the chip's auto-DoA. Reduces speaker bleed by 6–10 dB
  *before* AEC3 with no chip-side AGC artifacts. ~1 day of work.
  Probably the highest-quality endpoint short of neural.
- **microWakeWord A/B.** Different wake-word model (TFLite-Micro,
  Hey Jarvis pretrained). Different sensitivity/robustness profile
  than openWakeWord. Lower compute footprint. ~2 hrs to integrate
  + ~30 min A/B against current openWakeWord.

### Tier 3 — heavy lifts, defer until needed

- **DeepVQE neural residual stage.** Stack a learned residual
  canceler on top of AEC3. Documented +10 to +20 dB ERLE on music
  in the literature (DeepVQE paper, Indenbom 2023). Treat as
  Stage 4 — only if Tier 1+2 are exhausted and wake-word still
  misses at high SPL. ~2–3 days of work, competes with openWakeWord
  for CPU. The richiejp/deepvqe-ggml repo ships pretrained weights
  for the full 8M-param model; DeepVQE-S (the smaller variant
  Microsoft Teams actually deploys) doesn't have public weights as
  of 2026-05.
- **Custom "Hey Jasper" wake-word model trained on this speaker's
  residual.** Already in the v1.1 lane above, but worth flagging
  here: it directly addresses the symptom rather than the underlying
  audio quality. Largest "absolutely crushing it" outcome possible.
  Substantial work (data collection, training, validation).

### Hardware / UX (free wins)

- **Move the mic farther from the speakers.** Free-floating on a
  desk currently ~3 ft away. Each doubling of distance is ~6 dB of
  speaker bleed reduction.
- **Add foam baffling between speaker and mic** if the desktop
  geometry allows it. Cheap, helps direct-path component.

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

- **`jasper.tools.transport`'s active-source resolution loses
  the recently-paused source.** Reproducible end-to-end with the
  dial: tap to pause AirPlay → jasper.tools.transport correctly
  routes the toggle to AirPlay and pauses it. Tap again to resume →
  the resolver re-evaluates, sees AirPlay is paused (so "not the
  active source"), falls back to MPD (which has nothing playing),
  and the second toggle silently no-ops on MPD. Net effect: dial
  short-press pauses but doesn't unpause. Discovered during the
  dial v3.x migration but the bug pre-dates it — v0.1.0 dial
  firmware would have hit the same path.

  Fix shape: source resolution should remember the most recently
  active source for some bounded window (~30 s seems right) and
  prefer it for `toggle` even when its current state is "paused".
  Same logic probably wants to apply to Spotify Connect when it's
  paused (the source still exists at the renderer level; the
  resolver just isn't asking it).

  Lives in `jasper/tools/transport.py` and is intertwined with
  `RendererClient.active_source` semantics. See also
  `docs/HANDOFF-voice-music-control.md` for the source-routing
  context. Single-session fix; needs a bench test against AirPlay
  + Spotify Connect + MPD to confirm none of the other paths
  regress.

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
