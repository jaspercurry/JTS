# Open Smart Speaker — Master Plan (v1 first, expand later)

## Context

You have a thorough product brief for a Pi-5-based DIY smart speaker (moOde streaming + CamillaDSP room correction + Gemini Live voice + optional mesh/sub/captive-portal). The plan you asked for narrows on the **smallest shippable v1**:

1. Pi 5 + moOde + always-on CamillaDSP doing real audio (with a placeholder DSP config so we know the chain works), then
2. ReSpeaker XVF3800 + custom wake word + Gemini Live API doing voice control of moOde + CamillaDSP volume.

Everything else in the brief (room-correction web tool, captive portal, Snapcast stereo, wireless sub, AP+STA mesh, USB gadget DSP) is deferred to v2+ phases. Sequencing for those is preserved at the bottom.

The brief is mostly accurate — three corrections are folded in below from sanity-checking against current docs.

---

## Implementation rules (apply throughout)

This plan is executed under the user's personal `CLAUDE.md` rules at `github.com/jaspercurry/claude-rules`. Summary of how each rule applies to this build:

1. **Think before coding** — phase boundaries are decision points. Ambiguity (Spotify OAuth quirks, ALSA Loopback rate-locking, Gemini SDK churn) gets surfaced and clarified before code, not after.
2. **Check prior art first** — every phase calls out the existing repo to copy from (`mdsimon2/RPi-CamillaDSP`, `pycamilladsp`, `dscripka/openWakeWord`, `googleapis/python-genai` `live` examples). Don't reinvent CamillaDSP control, wake-word loops, or Gemini Live event handling.
3. **Diagnose before solving** — when audio breaks (and it will: rate locks, USB enumeration order, ducking glitches), form a hypothesis, add logging, point at the specific line/condition before patching.
4. **Simplicity first** — v1 is intentionally five bites with no speculative configurability. The `VoiceSession` interface is the *only* abstraction we add up front, because it's load-bearing for the agnostic-later design. No tool-priority queues, no plugin systems, no "what if we want X later" hooks.
5. **Surgical changes** — moOde owns its config tree; we keep our files under `/opt/jasper/`, `/etc/camilladsp/`, `/etc/systemd/system/jasper-*.service`. We do not "tidy up" moOde defaults in passing.
6. **Goal-driven execution** — every phase has an explicit "Done when:" line. Don't mark a phase complete without hitting it.
7. **Close the loop** — actually play music, actually say the wake word, actually trigger a tool. The "Verification" section at the bottom is a manual smoke test that must pass on the real Pi before declaring v1 shipped. No "looks fine in `journalctl`" without listening to audio out of the speakers.

---

## Brief corrections to apply before ordering parts

| Brief claim | Reality (May 2026) | Action |
|---|---|---|
| CamillaDSP 3.0.1+ | Current is **4.1.3** (Apr 2026). YAML structure mostly compatible; websocket API unchanged. | Plan against 4.x. Note `SetConfigJson` exists alongside `SetConfigFilePath`+`Reload`. |
| Pi 5 1GB at $40 | Actually **$45**. Same SoC. | Update BOM. |
| "1GB is enough headroom" | Realistic resident set with the full v1 stack (moOde + CamillaDSP + openWakeWord + Gemini client + FastAPI later) is **550–750 MB**, leaving little for kernel cache and MPD buffers. | **Buy the 2GB Pi 5** for v1. Validate 1GB only after profiling. ~$10 difference, removes a whole class of intermittent OOM debugging. |
| Apple USB-C dongle "supports up to whatever" | On Linux it advertises **48 kHz only** (44.1 kHz is broken on the device side). | Pin the entire audio chain at 48 kHz. CamillaDSP resamples at the input. |
| moOde "alsa_cdsp starts/stops CamillaDSP per stream" — use Custom mode | Confirmed. moOde 10.x exposes Custom CamillaDSP mode in the UI; that's the supported path, not a workaround. | Use Custom mode + ALSA Loopback. Own the systemd unit + YAML outside `/var/local/www/` so moOde updates don't clobber. |
| Gemini "3.1 Flash Live" model | Real, **still Preview** (announced Apr 2026). SDK is `google-genai`. Audio session cap ~15 min, resumption tokens valid 2 hr. | Code against Preview; expect API churn. Use `client.aio.live.connect()` + `session.send_realtime_input()` (NOT `send_client_content` after the first turn — known footgun in 3.1 Flash Live). |
| Pi 5 USB gadget UAC2 | Kernel issues #6289 and #6569 **still open**. | Confirm USB gadget mode is **not** in v1. Push to a later phase. |
| Balena WiFi Connect | Requires NetworkManager; moOde uses dhcpcd; they fight on wlan0. | Defer captive portal to a later phase. v1 sets up via SSH/Ethernet. |

---

## v1 scope (what we're building first)

A single Pi 5 (2GB) on your desk that:

- Plays music via AirPlay/Spotify Connect/Bluetooth (moOde out of the box)
- Routes that audio through an always-on CamillaDSP instance with a passthrough config (so the chain is exercised end-to-end before we ever attempt real correction)
- Listens via the XVF3800, detects a wake word locally, opens a Gemini Live session, and exposes a minimal tool set: `set_volume`, `adjust_volume`, `toggle_play_pause`, `skip_next`, `skip_previous`, `get_now_playing`, plus `spotify_play(query)` if Spotify auth is straightforward
- Ducks music via CamillaDSP `SetVolume` (mixer gain, not Reload) when a voice session opens, restores on close
- Survives daemon crash (systemd auto-restart) and Gemini disconnect (graceful "I lost connection, try again")

Out of scope for v1: room correction tool, captive portal, Snapcast/stereo pair, sub node, mesh, USB gadget mode, calendar/weather/timer/reminder tools, push-to-talk button, daily spend cap UI (just log spend; cap comes in v1.1).

---

## Voice provider comparison (May 2026)

The brief defaults to Gemini Live; sanity-checking the alternatives:

| Provider | Status | Audio pricing | Tool calling | SDK / spec | Latency / quality | Verdict for this use case |
|---|---|---|---|---|---|---|
| **Gemini 3.1 Flash Live** | Preview | $0.005/min in + $0.018/min out → **~$1–3/mo** at light use | Sequential only | `google-genai`, mid-churn; system-instruction/resumption footguns | Lowest TTFA reported (~0.63 s); voice naturalness leads | Cheapest; mild API risk from Preview status |
| **OpenAI `gpt-realtime`** | **GA** (Aug 2025) | ~$0.06/min in + ~$0.24/min out → ~$10–27/mo | **Parallel mid-stream**, best reliability of the four | `openai` SDK + `openai-agents-python` `RealtimeAgent` (decorator-based tools, batteries-included) | Strong tool-following (the explicit focus of `gpt-realtime`) | Best ergonomics; ~13× more expensive than Gemini |
| **OpenAI `gpt-realtime-mini`** | GA | ~4× cheaper than full → **~$7/mo** at light use | Same as full | Same | Slightly weaker than full but still good | Sweet spot of cost + maturity |
| **xAI `grok-voice-think-fast-1.0`** | GA-ish, ~2 mo old | $0.05/min flat → ~$15/mo at 10 min/day | Parallel + native web/X-search grounding | **OpenAI Realtime-spec compatible** (drop-in); no first-party WS wrapper | Top of τ-voice bench (Apr 2026) | Interesting but young; us-east-1 only |
| **Anthropic Claude** | No native audio API | Pipeline: Deepgram + Haiku + ElevenLabs ≈ $0.11/min → **~$17/mo** | Tool call lives in the text turn between STT and TTS — works, more code | 3 coordinated WebSockets, or use Pipecat to glue them | ~300–500 ms slower per turn; barge-in less clean | Skip for a smart speaker — wrong shape |

### Decision logic

- **Pure cost-minimum** → Gemini 3.1 Flash Live. Acceptable if you're OK chasing 1–2 breaking SDK changes during the build.
- **Most reliable tool calling and most mature Python ergonomics** → OpenAI `gpt-realtime-mini`. ~$7/mo is a fair price for "GA, well-documented, decorator-based tools." Tool-calling reliability *is* the point of a smart speaker.
- **Lowest lock-in** → write the daemon against the **OpenAI Realtime WebSocket spec**, since xAI is spec-compatible — you can swap providers via base-URL + API-key change with no code changes. Gemini stays one adapter away.
- **Anthropic** → only if you specifically want Claude's reasoning. Wrong primitive for this build.

### Decision: provider-agnostic, with Gemini Live as the v1 default

The voice path is built behind a thin internal `VoiceSession` interface so that the rest of the daemon (wake-word handler, tool registry, audio I/O, ducking) doesn't know which provider is on the wire.

**v1 ships with one adapter** — Gemini 3.1 Flash Live via the `google-genai` SDK. Cost rationale: at the brief's "few minutes per day" usage profile, Gemini lands at ~$1–3/mo, which is a much stronger pitch in a YouTube video ("a few bucks a month") than OpenAI's $7–15/mo. The interface keeps OpenAI/xAI a future, contained addition rather than a daemon rewrite.

**OpenAI Realtime is the deferred adapter** — adding it later means writing `voice/openai_session.py` against the same interface (~150 LoC), defaulting model to `gpt-realtime-mini`. xAI Grok Voice comes for free with that adapter (OpenAI-spec compatible — different `base_url` + model name).

**Anthropic is not on the roadmap** — wrong primitive for this build (no native audio API; would require gluing 3 services together).

### Complexity cost of the agnostic shape

Essentially free for v1:
- Define a small `VoiceSession` Python protocol: `connect()`, `send_audio(pcm)`, `audio_out` async iterator, `register_tool(callable)`, `close()`, `usage_tokens()`.
- Implement `GeminiLiveSession(VoiceSession)` using `google-genai`'s `client.aio.live.connect()` underneath (~150 LoC including reconnect + ducking hooks).
- Daemon talks to `VoiceSession`, never to the provider SDK directly.

Total v1 added complexity vs "just call Gemini directly": **roughly one extra Python file (~50 LoC of interface, the rest is code we'd write anyway)**. The payoff: adding OpenAI later, swapping for an A/B comparison, or pivoting to Pipecat is a contained, testable change.

Alternative considered and rejected for v1: **Pipecat**. Right tool *if* you want both providers working simultaneously from day one. For v1, the smaller interface wins; we can drop into Pipecat in v1.1 if the interface proves limiting.

### Concrete v1 implementation impacts

- Phase 2B: write `voice/session.py` (interface) and `voice/gemini_session.py` (adapter). Daemon code in `voice_daemon.py` imports only `VoiceSession`.
- Phase 3: tool decorator registers callables on the `VoiceSession`; the adapter translates them to Gemini's tool schema. Adding OpenAI later means re-translating the same registry, no daemon changes.
- Config: `JASPER_VOICE_PROVIDER=gemini|openai|xai` env var selects the adapter; v1 only supports `gemini`.

---

## Resolved decisions

- **Pi 5 RAM**: 2GB. (1GB revisit only after profiling.)
- **Wake word for v1**: stock "Hey Jarvis" — gets v1 to "done" faster. Custom "Hey Jasper" training is its own milestone in v1.1.
- **Spotify in v1**: yes, included in Phase 3. OAuth setup is part of v1 effort.
- **Hardware**: all parts on hand. No procurement step. Plan starts at Phase 1A.

(BOM kept out of this plan since hardware is in hand. If a part fails and needs replacing, Pi 5 2GB / Apple USB-C → 3.5mm / XVF3800 / TPA3255 amp are the names to re-order against.)

---

## v1 phased build (five bites)

Each is a stop-and-test checkpoint. Don't move on until the previous one is solid.

### Phase 1A — Bare moOde with passthrough audio (~half day)

1. Flash moOde 10.1.2 (PiOS-Trixie-based) onto the SD card. Boot, configure over Ethernet from a laptop browser at `http://moode.local`.
2. Plug in the Apple USB-C dongle. In moOde's audio settings, select it as the output device. Set output and source-handling rate to **48 kHz** (everything in v1 lives at 48 kHz).
3. Confirm AirPlay 2 from your phone plays through the dongle → amp → speakers. Then Spotify Connect. Then Bluetooth.

Done when: phone-streamed music plays cleanly, no DSP yet.

### Phase 1B — Always-on CamillaDSP via ALSA Loopback (~1 day)

This is the structurally important step. Default moOde mode is `alsa_cdsp` — CamillaDSP starts/stops per stream, which makes runtime websocket control unreliable. We swap to the always-on topology now so every later feature (ducking, room correction, sub crossover) just works.

1. In moOde, switch CamillaDSP mode from default to **Custom**.
2. Configure moOde's ALSA output to write to `hw:Loopback,0,0` (snd-aloop kernel module; load via `/etc/modules`).
3. Install CamillaDSP 4.1.3 separately under `/opt/camilladsp/` (binary release; not via moOde's package). Reference `mdsimon2/RPi-CamillaDSP` for systemd unit shape.
4. Write a minimal v1 YAML at `/etc/camilladsp/v1.yml`:
   - Capture: `hw:Loopback,1,0` @ 48 kHz, S32LE, 2ch
   - A single mixer named `master_gain` with one biquad (flat / Gain 0 dB) per channel — this gives us a node we can `SetVolume` against without doing any DSP yet
   - Playback: `jasper_dongle` (a dmix-shared device wrapping the Apple dongle, `hw:CARD=A,DEV=0` on Pi OS Trixie) @ 48 kHz, S16LE, 2ch — see `deploy/alsa/asoundrc.jasper`
5. Run CamillaDSP as a systemd service launched with `-p 1234 -a 127.0.0.1` so the websocket binds locally. No auth on the websocket — keep it on loopback.
6. From a Python REPL, connect with `pycamilladsp`, send `GetVolume`, `SetVolume(-10)`, `SetVolume(0)`, observe the change in real time during playback.

Done when: music streamed into moOde plays out the dongle through the always-on CamillaDSP, and you can adjust mixer volume from a websocket client without dropouts. **This is the substrate everything else depends on.**

### Phase 2A — Mic + wake word (~half day)

1. Plug XVF3800 into a Pi 5 USB-A port (avoid USB 2.0 hub on the same controller as the Apple dongle if dropouts appear). It enumerates as a generic USB audio class device — no driver install.
2. Confirm `arecord -L` shows it; capture a 5-second WAV; play it back; verify the on-board AEC by playing music while recording (the recording should show heavy attenuation of the music).
3. Install `openwakeword`. Use the stock **"Hey Jarvis"** ONNX model against XVF3800 in real time. Validate detection latency and false-positive rate at normal listening volume. (Custom "Hey Jasper" model is a v1.1 milestone — keeps v1 unblocked on Colab training.)

Done when: speaking "Hey Jarvis" into the XVF3800 with music playing reliably triggers a callback in a Python script.

### Phase 2B — Voice daemon skeleton + Gemini Live echo loop (~1 day)

Stand up the voice daemon as a systemd service. v0.1 of it does the absolute minimum:

- Define `VoiceSession` interface in `voice/session.py`: `connect()`, `send_audio(pcm)`, `audio_out` async iterator, `register_tool(fn)`, `close()`, `usage_tokens()`.
- Implement `GeminiLiveSession(VoiceSession)` in `voice/gemini_session.py` using `google-genai`'s `client.aio.live.connect()`. Model: `gemini-3.1-flash-live-preview`. Auth via `GEMINI_API_KEY` env var.
- On wake-word callback: instantiate the session, stream XVF3800 PCM (16 kHz, 16-bit) in via `session.send_realtime_input()`, stream Gemini's PCM out (24 kHz, 16-bit) to a separate ALSA Loopback that feeds the dongle.
- Close session when Gemini emits end-of-turn + 60 sec idle.
- Log token usage per session to SQLite at `/var/lib/jasper/usage.db`.

No tools registered yet. Just: wake → talk to Gemini → hear it back. Validates end-to-end audio path through the agnostic interface.

Critical implementation notes (from research):
- SDK is `google-genai`, not `google-generativeai`. Pin the version since the API is still Preview.
- `client.aio.live.connect()` for the session. After the first turn, all input goes through `session.send_realtime_input()`. **Do not call `send_client_content` after the first turn** on 3.1 Flash Live — it's a known footgun; `initial_history_in_client_content=true` only seeds initial history.
- Keep the system instruction **short** (under ~500 tokens). Long system instructions break session resumption on this Preview model.
- Close the session on idle — proactive listening modes bill input tokens while the session is open.
- Audio session cap ~15 min; resumption tokens valid 2 hr. Reconnect-and-replay is fine for our use case (sessions are short).
- For provider-agnostic config: `JASPER_VOICE_PROVIDER=gemini` is the v1 default. OpenAI/xAI selectors land when the second adapter is written.

Done when: "Hey Jarvis, what's the weather in Toronto?" → Gemini answers out the speakers, music keeps playing in parallel.

### Phase 3 — Audio ducking + first tools (~1 day)

1. Add a `master_gain` mixer hook in the voice daemon. On wake → `SetVolume(-15)` via CamillaDSP websocket. On session end → `SetVolume(0)`. **Use SetVolume, not Reload** — Reload reparses YAML and glitches audio.
2. Add a tool registry. Decorator-based. Each tool is a Python function with type hints + docstring; the decorator builds Gemini's tool schema. v1 tools, in order of effort:
   - `get_volume()` / `set_volume(level: int)` / `adjust_volume(delta: int)` — all hit CamillaDSP websocket
   - `toggle_play_pause()` / `skip_next()` / `skip_previous()` — moOde HTTP API at `/command/?cmd=...` (or MPD direct on port 6600)
   - `get_now_playing()` — moOde HTTP API
   - `spotify_play(query: str, type: str = "track")` and `spotify_queue(query: str)` — `spotipy`. Requires creating a Spotify Developer app, getting Client ID/Secret, and running the OAuth dance once on the Pi (cached refresh token thereafter). Confirmed in v1 scope.

Done when: "Hey Jarvis, skip this song" works; "Hey Jarvis, set volume to 30" works; "Hey Jarvis, play Bohemian Rhapsody" finds and plays via Spotify; music ducks during voice and restores cleanly.

### Phase 4 — Polish: spend cap + systemd + reboot survivability (~half day)

1. Add the spend logger → daily-cap circuit breaker (env-var threshold, default $1/day). Logs to `/var/lib/jasper/usage.db`. On trip: voice path returns "voice is disabled until midnight"; CamillaDSP volume + moOde transport still work via direct websocket/HTTP (no LLM in path).
2. Finalize systemd units (`jasper-camilla.service`, `jasper-voice.service`) with `Restart=on-failure`, `RestartSec=2`. CamillaDSP's unit must come up before voice's (`After=`/`Requires=`).
3. Reboot test: cold-boot the Pi, wait 30 s, verify wake word works without logging in.

Done when: unplug-replug-the-Pi survives without intervention; spend cap demonstrably trips at a low threshold.

### Phase 5 (v1.1, after v1 ships) — Custom "Hey Jasper" wake word (~half day)

1. Record 50–100 utterances of "Hey Jasper" (own voice + a couple of others if available).
2. Train custom model in openWakeWord's Colab notebook (~30 min compute).
3. Drop `hey-jasper.onnx` into `/opt/jasper/models/`. Swap daemon's wake-word config to point at it (single-line change). A/B against "Hey Jarvis" stock for false-positive rate before declaring done.

Marked v1.1 because the answer to question 2 was to ship v1 on stock and brand later.

---

## Critical files / paths to create

```
/etc/camilladsp/v1.yml                 # passthrough config with master_gain mixer
/etc/systemd/system/jasper-camilla.service
/etc/systemd/system/jasper-voice.service
/opt/camilladsp/camilladsp             # 4.1.3 binary
/opt/jasper/                           # voice daemon
  voice_daemon.py                      # main loop; talks only to VoiceSession
  wake.py                              # openWakeWord wrapper
  voice/
    session.py                         # VoiceSession interface (protocol)
    gemini_session.py                  # Gemini 3.1 Flash Live adapter (v1)
    # openai_session.py                # deferred — adds OpenAI + xAI later
  tools/
    __init__.py                        # @tool decorator + registry
    audio.py                           # set_volume, adjust_volume, get_volume
    transport.py                       # play/pause/skip via moOde HTTP
    spotify.py                         # OAuth + spotipy
  models/hey-jasper.onnx               # v1.1
/var/lib/jasper/usage.db               # SQLite spend log
```

Reference repos to pull from (do not reinvent):
- `mdsimon2/RPi-CamillaDSP` — systemd unit and ALSA conf shape for always-on CamillaDSP
- `HEnquist/pycamilladsp` — Python websocket client (skip writing your own)
- `dscripka/openWakeWord` — wake-word runner and Colab training
- `googleapis/python-genai` — current SDK; check the `live` examples folder for the right `connect`/`send_realtime_input` pattern
- `marcoevang/camilladsp-setrate` — websocket reload pattern, useful even though we mostly use SetVolume

---

## Verification (end-to-end smoke test for v1)

In order, on the actual hardware:

1. **Streaming sanity**: From phone, AirPlay a known track. Hear it. Spotify Connect a track. Hear it. Bluetooth pair, play. Hear it.
2. **DSP-in-path**: With music playing, `pycamilladsp.set_volume(-20)` from a Python REPL → music gets quieter; `set_volume(0)` → restored. No glitches.
3. **Wake word**: Music at moderate volume. Say wake phrase 10 times — count detections (target 8/10).
4. **Voice happy path**: Wake → "What time is it?" → Gemini answers + music ducks + restores.
5. **Tool calls**: Wake → "Pause." moOde transport stops. Wake → "Resume." Plays. Wake → "Skip." Next track.
6. **Crash resilience**: `systemctl kill jasper-voice` mid-conversation. Within 5 sec systemd restarts it. Wake again — works.
7. **Spend cap**: Set cap to $0.01, force a session, confirm voice disables and music control still works.

---

## What comes after v1 (sequenced)

Defer all of these until v1 is rock-solid. Sequencing chosen to maximize feature-per-week and minimize cross-phase rework:

| v | Adds | Why this order |
|---|---|---|
| **v1.1** | Custom "Hey Jasper" wake-word model, push-to-talk button, daily spend cap UI in moOde plugin | Quick wins on top of working v1 |
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
- **Always-on CamillaDSP** (Phase 1B) is the pre-req for both ducking *and* room correction *and* sub crossover *and* per-channel slave correction.
- **Tool decorator + registry** (Phase 3) is the pre-req for v3's tool batch and v9's HA bridge.
- **48 kHz everywhere** keeps resampling out of the hot path now and through Snapcast later.
- **Systemd-managed services in `/opt/jasper`** keep the install survivable across moOde updates, so v4's NM swap is the only risky migration on the horizon.

---

## Risks worth re-flagging before starting

- **Gemini 3.1 Flash Live is Preview, not GA.** API can change underneath you. Pin `google-genai` SDK version; expect to chase one or two breaking changes during v1 build. The `VoiceSession` interface limits the blast radius of any churn to a single adapter file.
- **Gemini tool calling is sequential** (no parallel/non-blocking). A slow tool (e.g. Spotify search) will gate the next thing the model says. Keep tool implementations fast (5 s timeout, return errors quickly).
- **CamillaDSP websocket has no auth.** Bind it to 127.0.0.1 only. Don't expose port 1234 on the LAN.
- **Loopback locks rate at first opener.** Pin everything to 48 kHz / S32LE on the capture side, S16LE on the dongle output side, set in moOde's ALSA conf and CamillaDSP YAML so all sources match before either side opens.
- **moOde updates can rewrite `/etc/alsa/conf.d/`.** Keep the always-on systemd unit and YAML under `/opt/jasper/` and `/etc/camilladsp/`, not under moOde-managed paths. Document the re-apply steps after each moOde update.
- **Long Gemini system prompt breaks session resumption** on the 3.1 Flash Live preview. Keep system instruction under ~500 tokens.
- **`SetVolume`, not `Reload`, for ducking.** Reload reparses YAML and glitches audio mid-stream.
- **Idle billing on Gemini Live**: don't keep the session open forever. Close after 60 s of silence post-last-turn.
