# JTS — forward roadmap

v1 is shipped (see [README.md](README.md) for the current state).
This document tracks what comes next, in a sequence chosen to
maximize feature-per-week and minimize cross-phase rework.

For the operator-facing "how do I bring this up from scratch" guide,
see [BRINGUP.md](BRINGUP.md). For deep-dives on existing subsystems,
see [docs/HANDOFF-*.md](docs/).

---

## ⚠️ Urgent — investigate before next major work

### AEC bridge stalls under normal music playback (2026-05-11)

Observed during the 2026-05-11 deploy verification: `jasper-aec-bridge`
floods `ref queue full, dropping frame` warnings for ~1 s, then trips
`mic queue empty for 5s — InputStream is dead`, exits non-zero, and
relies on systemd `Restart=on-failure` to come back. The crash-loop
eventually trips `StartLimitBurst` and parks the unit as failed,
leaving the wake-word path silently degraded (mic = clean XVF
beamform, but no echo cancellation against music output).

Background:
- The auto-restart on `InputStream is dead` is PR #77's mitigation —
  it brings the bridge back, but it doesn't fix what caused the
  stall in the first place.
- The XVF UAC2 capture underrun was the *original* trigger, but
  the "ref queue full" flood preceding the mic-empty exit suggests
  something else: the ref-side (music chain via dsnoop) is producing
  faster than `_aec_loop` can consume, which would mean `_aec_loop`
  is starved of mic frames first (matching the eventual exit).
- Memory entry "AEC bridge mic-stall recovery" notes this pattern.
  Auto-restart catches the symptom; root cause is open.

Why urgent: the bridge is the only thing standing between music
playback and the wake-word detector. When it's down, wake-word
detection on music is back to pre-AEC baseline (works at low SPL,
fails at conversational SPL).

Starting points:
- `journalctl -u jasper-aec-bridge --since "1 hour ago" | grep -E
  "ref queue full|mic queue empty|InputStream is dead"` to see the
  recurrence frequency on a given Pi.
- Compare `ref` and `mic` queue depths over time — the asymmetry
  is the diagnostic. If ref depth grows while mic depth stays at 0,
  the XVF capture stream stopped delivering callbacks (the original
  failure mode). If both depths grow, something else is wrong with
  `_aec_loop` scheduling.
- `jasper/cli/aec_bridge.py:_aec_loop` and the queue-depth
  bookkeeping around it.
- Consider whether the queue-size logging at WARNING level is
  drowning out the eventual ERROR line — debouncing might make
  the journal more readable but isn't a fix.

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

### "Configure remotes" wizard — the satellite-onboarding sub-page

Goal end-state UX (per user, 2026-05-09):
> 1. Get the speaker set up
> 2. Hear the voice that says "Go to jts.local"
> 3. Go there and click a button that says "Configure remotes"
> 4. On that screen it basically says "Plug it in" — you plug it in
>    and that gets the firmware properly updated, gets the WiFi on
>    there, away you go.

What exists today:
- `jasper-dial-web` ([`jasper/web/dial_setup.py`](jasper/web/dial_setup.py))
  serves `https://jts.local/dial/` with this exact flow for the
  rotary dial: scan plugged-in ESP32-S3 devices, pick one, click to
  flash + provision. Shells out to `jasper-dial-onboard`.
- `jasper-satellite-onboard` ([`jasper/cli/satellite_onboard.py`](jasper/cli/satellite_onboard.py))
  is the CLI half for the AMOLED satellite. Mirrors `jasper-dial-onboard`'s
  shape so a generalized wizard can shell out to either.

What's needed:
- Generalize `dial_setup.py` → `remote_setup.py` (or fork it as
  `satellite_setup.py`). Choose: a single `/remotes/` page with a
  device-class dropdown (dial / AMOLED satellite), or two parallel
  pages (`/dial/`, `/satellite/`) linked from a `/remotes/` index.
  Single page is the user's stated dream; parallel pages is less
  refactor.
- Auto-detect device class on plug-in: the boot-log probe is the
  cleanest signal — `jasper-dial firmware` vs
  `jasper-satellite-amoled firmware` in setup() prints. Falls back
  to user picking from a dropdown for fresh chips with no firmware.
- nginx route: add `/satellite/` (or `/remotes/`) to the
  jasper.conf reverse-proxy block.
- systemd unit for the new web service (or extend `jasper-dial-web`).
- The audible cue that says "go to {hostname}" should land the user
  on the management dashboard root, which links into "Configure
  remotes" — soft prereq for the cue UX completing.

This is the obvious next step after both onboard CLIs are stable.
Single session of work.

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
  Now also tunable per-installation via the slider at
  `http://jts.local/wake/` (PR #133); this item is about whether to
  ship a lower *default*, separate from exposing the knob.

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

## Pi-side speaker identification (no version, future)

None of the three voice provider APIs we support (OpenAI Realtime,
Gemini Live, Grok Voice) expose speaker-1-vs-speaker-2 labels —
they all treat input audio as a single user. If we want the speaker
to know who's talking (rather than requiring explicit naming like
"Brittany's calendar"), diarization has to run on the Pi *before*
audio hits the voice API.

Use cases that motivate it:

- **Auto-routing personal-account commands.** Today a household
  member has to name themselves in queries like "what's on
  Brittany's calendar today" or "draft an email from Jasper to..."
  Speaker ID would let the model implicit-route by who spoke, with
  explicit naming as the fallback when confidence is low.
- **Moderator mode** (design TBD) — a conversational mode where the
  speaker mediates a multi-person discussion. Speaker ID is a hard
  prerequisite, not a nice-to-have.

Technical shape (mirror of the HA Voice community pattern):

- Per-member enrollment via the management dashboard — 30–60 s of
  audio per person → `pyannote.audio` or `resemblyzer` embedding →
  persisted to `/var/lib/jasper/speakers/<name>.npy`.
- Pi-side inference stage between the chip's processed mic channel
  and the voice API input. Each ~1 s chunk gets a speaker-ID score
  against enrolled embeddings. Output: a confidence-tagged speaker
  label attached to the wake event.
- Auto-route only when confidence ≥ threshold; below it, fall back
  to explicit-naming disambiguation ("whose calendar do you mean?").
  Open-source implementations report ~92% accuracy with
  `pyannote.audio`, ~1.3% false-accept across 7 households / 90
  days — good, but not "act on personal data without confirmation"
  good. The threshold should be conservative for write actions
  (calendar create, email send) and looser for reads.

Prior art: Apple HomePod's "Hey Siri, learn my voice" (3-utterance
enrollment + on-device neural adaptation); Google Voice Match and
Amazon Alexa Profiles (4+ profiles per device with per-account
linking, both fall back to disambiguation on uncertainty); Home
Assistant Voice community implementations using `pyannote.audio` or
`resemblyzer`.

Cost: ~1 day for the enrollment UI + embedding pipeline + voice-
loop hookup. `resemblyzer` is small enough for the 1 GB Pi 5;
`pyannote.audio` is heavier and may push to the 2 GB SKU. No cloud
cost.

Not blocking anything; flagged because it unlocks a meaningful UX
improvement on the existing per-member tool surface (Spotify,
Calendar/Gmail once landed) and is the technical prerequisite for
moderator mode.

---

## Test/dev follow-ups (no version)

Small infrastructure items not blocking any feature; recorded so
they don't get lost in the working tree.

- **`jasper.tools.transport`'s active-source resolution loses
  the recently-paused source.** Reproducible end-to-end with the
  dial: tap to pause AirPlay → `jasper.tools.transport` correctly
  routes the toggle to AirPlay and pauses it. Tap again to resume →
  the resolver re-evaluates, sees AirPlay is paused (so "not the
  active source"), falls through to "none" (no source), and the
  second toggle returns the "nothing is playing" error response
  instead of resuming. Net effect: dial short-press pauses but
  doesn't unpause. Same shape applies if the user pauses Spotify
  Connect — the source still exists but the resolver stops asking
  about it.

  Fix shape: source resolution should remember the most recently
  active source for some bounded window (~30 s seems right) and
  prefer it for `toggle` even when its current state is "paused".

  Lives in `jasper/tools/transport.py` and is intertwined with
  `RendererClient.active_renderers` semantics. See also
  `docs/HANDOFF-voice-music-control.md` for the source-routing
  context. Single-session fix; needs a bench test against AirPlay
  + Spotify Connect to confirm none of the other paths regress.

---

## Resilience ladder — deferred tiers (no version)

`docs/HANDOFF-resilience.md` documents the full five-tier ladder.
Tiers 1+2 (sd_notify watchdog + `Type=notify` + `Restart=on-watchdog`)
shipped in PRs #77 + #93 alongside the UDP transport that
eliminated the snd-aloop kernel-state failure class. Tier 5 is the
cheap remaining one we should not forget. Tiers 3–4 are noted for
completeness but have weak triggers.

### Tier 5 — BCM2712 hardware watchdog (do this next)

Set `RuntimeWatchdogSec=15s` in a `/etc/systemd/system.conf.d/`
drop-in. systemd PID 1 pats `/dev/watchdog0` (the BCM2712 driver,
exposed on Pi OS Lite Trixie 64-bit by default); if PID 1 itself
hangs, the kernel watchdog reboots the Pi. Covers kernel panics
and total userspace wedges that no userspace mechanism can recover
from. ~5 lines of config, no code, no runtime cost.

Sequencing note: stage this AFTER Tiers 1+2 have soaked on the
speaker for a stretch, so we're not chasing boot-loops caused by
our own daemons. Confirm `cat /sys/class/watchdog/watchdog0/identity`
returns `Broadcom BCM2835 Watchdog` (Pi family identifier) before
enabling. The 15 s ceiling is hardware-imposed.

### Tier 3 — `OnFailure=` cross-service chaining (deferred)

Trigger condition: we hit a failure where one daemon's repeated
restart-loops fail because a sibling is broken, and the systemd
watchdog can't fix it alone. The UDP transport eliminated the main
case (bridge↔voice no longer share kernel state). Wait for evidence
before wiring.

### Tier 4 — `rmmod snd_aloop && modprobe snd_aloop` recovery script (deferred)

Trigger condition: the music-chain `Loopback` card wedges (no
incidents of this in production; CamillaDSP is well-behaved). If
it ever happens, the recovery is a templated `jasper-recover@.service`
unit that stops the renderers + camilla + voice, reloads the
module, restarts everything. ~150 lines of bash. Worth doing if
we see this failure mode; not before.

---

## Remote software updates / CI deploy pipeline (no version, research-needed)

See [`docs/HANDOFF-remote-updates.md`](docs/HANDOFF-remote-updates.md)
for the full research write-up: option space (five OTA patterns
from `git pull` to RAUC A/B partition swap), recommended staged
build-out (CI first, auto-release second, dashboard "Check for
updates" button third), the integration points already in place
(`build.txt`, `system_setup.py`'s button pattern, `jasper-doctor`,
`install.sh` idempotency), failure-and-rollback strategy, auth
considerations, and open questions.

TL;DR: today's deploy flow is laptop-driven and manual (`bash
scripts/deploy-to-pi.sh`), with no CI gate between "works on my
laptop" and "running on the speaker". The handoff doc recommends
building CI first (high standalone value), then auto-release on
merge to `main`, then the dashboard button — and flags Tailscale
as a cheaper partial-substitute if the goal is just "deploy from
outside the LAN".

Not blocking anything. Graduates from "nice-to-have" to
"must-have" the moment a second household or a non-Jasper
operator is in the loop.

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
