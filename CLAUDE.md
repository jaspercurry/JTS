@README.md

This file: AI-specific operational guidance for Claude Code
working in this repo. README.md (imported above) has the project
context — architecture, hardware, repo layout, subsystem overview,
deployment, debugging entry points. **Don't restate it here.** If
you find yourself explaining the project, fix README.md and
reference it from this file instead.

What goes here:
- Things easy to get wrong (model gotchas, file ownership lines,
  brick hazards)
- Operational shortcuts (specific scripts, env-var formats)
- AI behavioral rules specific to this codebase

Twin file: `AGENTS.md` mirrors this for non-Claude agents (the
`@README.md` import syntax is Claude-specific). Keep both in sync
when editing.

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
APIs behind a single env var. Architecture and per-provider
trade-offs are in
[`docs/HANDOFF-voice-providers.md`](docs/HANDOFF-voice-providers.md);
this section is the operational summary.

**Two ways to switch.** Either work; pick whichever fits the moment.

**Web UI (preferred, end-user friendly)** — visit
`http://jts.local/voice/` from any device on the LAN. The page
shows one card per provider for pasting API keys, picks model and
voice from curated dropdowns, and has a single radio group at the
top for "use this provider". Saving writes
`/var/lib/jasper/voice_provider.env` at mode 0600 and restarts
`jasper-voice`. Source: [`jasper/web/voice_setup.py`](jasper/web/voice_setup.py).

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

## Voice system prompt — read the provider's guide before editing

`SYSTEM_INSTRUCTION` in [`jasper/voice_daemon.py`](jasper/voice_daemon.py)
is what the realtime LLM sees on every turn. **Don't tune it by
intuition.** Each provider publishes a prompting guide whose
structure mirrors how their model was RLHF-trained; aligning with
that structure makes instructions stick. Fighting it (e.g. absolute
prohibitions where the model expects conditional rules) gets partial
compliance at best.

Canonical references:
- OpenAI Realtime — [Realtime Prompting Guide](https://cookbook.openai.com/examples/realtime_prompting_guide)
  + [Using realtime models](https://developers.openai.com/api/docs/guides/realtime-models-prompting).
  Defines the recommended skeleton (Role, Personality, Preambles,
  Verbosity, Tools, …) and concrete language for common patterns.
- Gemini Live — [Models guide](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview)
  + [prompt design](https://ai.google.dev/gemini-api/docs/prompting-strategies).
- xAI Grok Voice — [Voice agent guide](https://docs.x.ai/docs/guides/voice/agent).

**Preamble pitfall (worth knowing).** `gpt-realtime-2` emits short
preamble audio before tool calls by default ("checking the live
arrivals now…"). It's intentional UX, but for our sub-2-second
tools it takes longer than the tool itself. OpenAI's official
suppression pattern is **conditional, not absolute**: tell the
model the cases in which preambles should NOT appear, including
"the tool call is lightweight and the user would not benefit from
an update." Absolute bans ("never preamble") get partially ignored
because they conflict with the conditional rules the model was
trained on. See the `Preambles` block in `SYSTEM_INSTRUCTION` for
the live version.

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
- `logs/asoundrc-latest.txt` — current `/root/.asoundrc`
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
  `/etc/modprobe.d/snd-aloop.conf`, `/root/.asoundrc`,
  `/etc/shairport-sync.conf`, `/etc/nginx/sites-enabled/jasper.conf`,
  and `/etc/systemd/system/{jasper-*,librespot,shairport-sync,nqptp,bt-agent}.service`.
  Touch only what you must when modifying these.
- **No silent failure paths.** Any new code path that would
  prevent the speaker from responding to a wake event MUST also
  trigger an audio cue (so the user hears why nothing happened).
  Add cues by appending a `CueDef` to
  [`jasper/cues/registry.py`](jasper/cues/registry.py) and calling
  `cues.play("<slug>")` from the failure handler — see
  [docs/HANDOFF-audible-feedback.md](docs/HANDOFF-audible-feedback.md)
  for the full pattern. Cue text must stay provider-agnostic
  (no "Google" / "Gemini" — voice backend is replaceable).

---

## Testing

Hardware-free tests (run locally, no SDK auth needed):

```sh
.venv/bin/pytest
```

Anything Pi-specific (audio I/O, websocket, Gemini Live) needs
to run on the actual hardware via `jasper-doctor` or by tailing
logs during use.

---

## Branch and remote

Active branch: `main`. The user's GitHub remote is
`jaspercurry/JTS` — accessible via `mcp__github__*` tools, not
the `gh` CLI.
