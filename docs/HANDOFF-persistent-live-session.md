# Handoff: persistent-single Gemini Live session rework

You're picking up an in-progress voice-assistant project (JTS = "Jasper Smart Speaker"). The hardware bring-up is **done and working**. The voice-loop integration with Gemini Live is **partially working but unreliable**. Your job is to do one focused architectural rework that fixes the reliability problem.

Read this whole doc first. Then read the project docs listed at the bottom in the order given. Don't start writing code until you've done both.

## Repo and branch

```
git@github.com:jaspercurry/JTS.git
branch: claude/camilla-dsp-voice-plan-QRdsE
```

All recent work is on that branch (latest commit: `6a60b08`). `main` is stale. Pull the branch and read recent commits — they explain the ground truth of what works and what's a workaround.

## What's working (don't break this)

- **Hardware bring-up**: Pi 5 (1 GB) + Raspberry Pi OS Lite Trixie + always-on CamillaDSP + Apple USB-C dongle as DAC + ReSpeaker XVF3800 as mic. AirPlay/Spotify Connect/Bluetooth all flow through CamillaDSP to the dongle. See `BRINGUP.md`.
- **Wake word**: openWakeWord + Silero VAD running locally on Pi, listens for "Hey Jarvis". Currently threshold 0.92 (high — see "what's broken" below).
- **Audio I/O plumbing**: `jasper.audio_io.MicCapture` (16 kHz mono int16 frames from XVF3800) and `TtsPlayout` (24 kHz mono PCM from Gemini → 48 kHz dmix on dongle) both fully tested.
- **Tool calls**: weather, subway, time, audio control (volume/duck/etc), Spotify, MPD transport — all work when Gemini actually responds.
- **TTS gain**: `JASPER_TTS_GAIN_DB=-8` attenuates Gemini's PCM peaks so voice doesn't dominate music.
- **Voice pinning**: Aoede prebuilt voice via `speech_config` so style is consistent across sessions.
- **Time injection**: current local time is added to system instruction at session start.
- **Logging**: per-session timing (`session connect done in Xms`, `first audio chunk from Gemini in Yms`, tool-call elapsed, payload preview truncated to 240 chars), bytes-sent / chunks-received counters, structured `SILENT FAILURE` warning when sent>0 and received==0.

## What's broken (this is what you're fixing)

### 1. Silent failures — the headline problem

About **50% of Gemini Live sessions silently fail**: WebSocket connects fine, `setupComplete` arrives, the daemon streams ~10 sec of audio, server returns ZERO chunks back, daemon's idle watchdog times out the session at 10 sec. No exception, no error code, no WebSocket close — just silence on the wire.

Symptom in logs:
```
wake detected
session connect done in 250ms
[10 sec elapsed]
idle timeout, closing session
SILENT FAILURE: sent 327680 bytes ... received 0 chunks back
session ended: {input_tokens: 0, output_tokens: 0} ... (sent=327680B, recv=0 chunks)
```

This happens on real user queries (not just music false-fires) and happens on **both** `gemini-3.1-flash-live-preview` AND `gemini-2.5-flash-native-audio-preview-12-2025`. The user has Tier 1 paid billing (50 concurrent ceiling).

### 2. 409 Conflict errors in Cloud Logging

User sees real-time 409s in Google Cloud Logging at `JTS Project gen-lang-cl...` despite our daemon's connect-step 409 retry logic catching ZERO of them — meaning 409s are happening at sub-call layers we don't currently catch.

### 3. Wake false-fires on music

Without hardware AEC reference signal wired to the XVF3800, mic captures speaker bleed at full intensity. openWakeWord's `hey_jarvis_v0.1` model occasionally false-fires on music with vocals. We've worked around with `JASPER_WAKE_THRESHOLD=0.92` and `WAKE_REFRACTORY_SEC=10.0`, but it's a workaround. **NOT in scope for this rework.**

### 4. Self-interrupts via TTS bleed (worked around with NO_INTERRUPTION)

Without hardware AEC, when the model is producing TTS, the mic captures the bleed-through, server-side VAD treats it as user activity, and Gemini interrupts itself ~1 sec into every reply. Worked around with `realtime_input_config.activity_handling=NO_INTERRUPTION` — server ignores user activity, model finishes turns. Cost: real barge-in is disabled. **NOT in scope for this rework either.**

## Why the current architecture is wrong (the rework)

The daemon currently opens a **fresh Gemini Live WebSocket per wake event**, streams audio for that one turn, closes, repeats. That's the per-turn pattern.

Multiple authoritative sources from May 2026 confirm Google's current best practice is **persistent-single**: open ONE WebSocket at daemon start, keep it open for the daemon's lifetime via `sessionResumption`, only stream audio during active turns:

| Source | Pattern |
|---|---|
| Google's official cookbook quickstart [`Get_started_LiveAPI.ipynb`](https://github.com/google-gemini/cookbook/blob/main/quickstarts/Get_started_LiveAPI.ipynb) | One `async with connect()` for whole program lifetime |
| [Google Developers Blog Apr 2025](https://developers.googleblog.com/en/achieve-real-time-interaction-build-with-the-live-api/) declared Live API "production ready" | Features only matter for long-lived sessions (24h state, sessionResumption, GoAway, context compression) |
| [python-genai SDK docstrings](https://github.com/googleapis/python-genai/blob/main/google/genai/live.py) | One context, multiple `send_realtime_input` |
| [LiveKit Gemini plugin](https://docs.livekit.io/agents/models/realtime/plugins/gemini/) | One session per call, kept across room lifecycle |
| [Pipecat](https://docs.pipecat.ai/guides/features/gemini-live) | "Persistent connection" with explicit reconnect logic |
| [Home Assistant Voice](https://community.home-assistant.io/t/experimental-gemini-live-streaming-voice-flow-for-home-assistant-voice-pe/1008665) | WebSocket open across tool execution |

Per-turn churn is the suspected root cause of both the silent failures and the 409s — server-side session teardown lags client-side close, so opening a new session within seconds of closing the previous can race the prior session's teardown and either hit a transient concurrent-session ceiling violation OR get a slot that's still being torn down (manifests as silent failure).

## What you're building

Refactor the session layer so the daemon holds **one persistent Gemini Live WebSocket** and turns it into a turn-based interface for the rest of the daemon.

### Architectural pieces

1. **Refactor** `jasper/voice/gemini_session.py` into:
   - `GeminiLiveConnection` — long-lived. Owns the WebSocket. Handles connect / GoAway / disconnect / reconnect with bounded exponential backoff. Survives the 15-min audio cap via `sessionResumption` (resumption handle stored, used on reconnect).
   - Conforms to a `LiveConnection` protocol so future providers (OpenAI Realtime, etc) can plug in.
   - The existing `VoiceSession` protocol in `jasper/voice/session.py` becomes "an active turn within a connection" — caller acquires a turn from the connection, sends audio, awaits response, releases.

2. **Manual VAD + activity markers** instead of automatic VAD:
   - `realtime_input_config.automatic_activity_detection.disabled = True`
   - On wake: `send_realtime_input(activity_start=...)` then stream mic frames
   - On end-of-turn (idle window passes / user done): `send_realtime_input(activity_end=...)`, stop streaming
   - Between turns: do NOT send mic frames at all (server doesn't see ambient/music)

3. **Reconnect state machine**:
   - States: `connecting → connected → in-turn → idle → reconnecting → failed → paused-for-backoff`
   - Triggers: `GoAway` message, WebSocket close codes 1006/1011, send error
   - Bounded retries with exponential backoff (1s/2s/4s/8s/cap), give up after N attempts and surface a clear error
   - Reconnect sends the latest `sessionResumption` handle so the server resumes the conversation context

4. **Idle context reset policy** — env knob `JASPER_LIVE_CONTEXT_RESET_SEC` (default 300 = 5 min). After 5 min of no turns, on the next turn open a fresh session (no resumption handle) so conversational state from earlier doesn't bleed in. Stale-context UX is a real problem otherwise: "Hey Jarvis, what time is it?" at 9am and 5pm — second one shouldn't remember weather query from morning.

5. **Keepalive** to survive the 10-min server idle timeout (per [Vertex troubleshooting docs](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/live-api/troubleshooting)). Could be a periodic empty `send_realtime_input` or whatever the SDK supports.

6. **Tests** under `tests/` for the reconnect state machine — mock the WebSocket layer, exercise GoAway / disconnect / resumption-token-rotation paths.

### What stays the same

- `voice_daemon.py`'s wake-word loop, ducker, tool registry, idle watchdog (the watchdog now ends the *turn* not the *connection*)
- `audio_io.py` — `MicCapture` and `TtsPlayout` are unchanged
- ALSA / CamillaDSP routing — don't touch `/etc/alsa/conf.d/zz-jts-loopback.conf` or `/etc/camilladsp/v1.yml`
- Tool implementations and the `ToolRegistry`
- `JASPER_GEMINI_MODEL` switching via `scripts/switch-gemini-model.sh`

### What you should explicitly skip

- Hardware AEC reference signal routing (separate architectural project, not blocked by this)
- Software VAD-based barge-in (Silero can't distinguish TTS bleed from real speech; deferred until hardware AEC exists)
- Wake-word reliability tuning (separate concern; thresholds tuned in env vars)

## Honest fragility considerations

Per the [research summary above](#why-the-current-architecture-is-wrong-the-rework):

- **`sessionResumption` is reliable for audio-only**, broken for prior audio+video sessions ([python-genai #2290](https://github.com/googleapis/python-genai/issues/2290)). We are audio-only, so this is fine.
- **No built-in reconnect in the SDK** — you write the loop. Naive retry causes `OVERLOADED_TOO_MANY_RETRIES_PER_REQUEST` ([livekit/agents #1679](https://github.com/livekit/agents/issues/1679)). Bound retries.
- **`activity_start`/`activity_end` only work with automatic VAD disabled** ([API ref](https://ai.google.dev/api/live)). Don't try to mix the two.
- **Conversation context carries across turns within a session** unless you explicitly start a fresh session. Hence the idle-reset policy (#4 above).

## How we've been measuring (logging philosophy)

The current logging in the daemon is the model to extend:

- **Structured per-session lines**, one per state transition. Every interesting event has a log line.
- **All timing in milliseconds.**
- **Counts AND timings** — bytes sent, chunks received, elapsed ms — so silent failures are detectable.
- **Truncate large payloads** — tool responses (weather, 14-day forecast) are 4-8 KB and flood journal. Cap at 240 chars.
- **INFO** for normal events, **WARNING** for service-failure heuristics (`SILENT FAILURE`, `tool TIMED OUT`, `gemini WS closed`).
- **No DEBUG-only signal** in code paths that matter for diagnosis — operator should be able to debug from the default journal level.

For your rework, add a similar set of structured logs for the connection lifecycle:
- `live connection: connect ok in Xms (resumption=...)`
- `live connection: GoAway received, time_left=Xs, will reconnect`
- `live connection: reconnect attempt N after Xs backoff`
- `live connection: disconnected (code=1006), reconnecting`
- `live connection: failed after N retries; daemon will pause`
- `live turn: started (activity_start sent)`
- `live turn: ended in Xms, M chunks received`
- `live context reset: idle for Xs > threshold; reopening with no resumption handle`

## How to actually test this

The Pi is the integration target. The development loop:

1. Make code changes locally on the laptop in `/Users/jaspercurry/Code/JTS/`
2. Sync to Pi: `rsync -avz --delete --exclude .venv --exclude __pycache__ --exclude '.git/' --exclude 'logs/*' ./ pi@jasper.local:/home/pi/jts/`
3. Deploy to `/opt/jasper/`: `ssh pi@jasper.local "sudo rsync -a --delete --exclude __pycache__ /home/pi/jts/jasper/ /opt/jasper/jasper/"`
4. Restart daemon: `ssh pi@jasper.local "sudo systemctl restart jasper-voice"`
5. Pull logs: `bash scripts/fetch-pi-logs.sh` (output lands in `./logs/*-latest.log`)

Or live tail: `ssh pi@jasper.local "sudo journalctl -u jasper-voice -f"`.

For unit testing the reconnect state machine, mock the SDK's `aio.live.connect` and exercise:
- Successful connect → in-turn → idle → in-turn cycle
- `GoAway` mid-turn → reconnect with last resumption handle → resume
- WebSocket close 1006 → reconnect with backoff → eventually succeed
- Repeated failures → eventually surface `failed` state, daemon pauses
- Idle reset: connection still healthy but `idle > JASPER_LIVE_CONTEXT_RESET_SEC` → close + reopen fresh

Hardware-free tests run with `pytest`. Don't add hardware-dependent tests.

## Project docs to read, in order

Read these on the branch (all are in the repo root or `docs/`):

1. **`CLAUDE.md`** (root) — project context, model strategy, debugging philosophy. Critical: explains the `gemini-3.1-flash-live-preview` vs `gemini-2.5-flash-native-audio-preview-12-2025` switching pattern and the symptoms checklist for "Live API silently failing."
2. **`BRINGUP.md`** (root) — full system architecture, hardware setup, common failure modes table. Explains the ALSA `_audioout` override, CamillaDSP custom-mode setup, the XVF3800 + dongle hardware, and the AEC architectural notes.
3. **`PLAN.md`** (root) — original v1 master plan, post-v1 scope.
4. **`jasper/voice/session.py`** — the `VoiceSession` protocol. The new `LiveConnection` should sit alongside it.
5. **`jasper/voice/gemini_session.py`** — current implementation. This is what gets refactored. Read every line — it has the timing logs, the silent-failure counters, the 409 retry, the speech_config.
6. **`jasper/voice_daemon.py`** — main loop, `WakeLoop`, ducker, idle watchdog. The watchdog needs minor adjustment for "end turn, not connection."
7. **`jasper/audio_io.py`** — `MicCapture` and `TtsPlayout` unchanged but useful context for sample rates.
8. **`scripts/switch-gemini-model.sh`** — operator escape hatch for when 3.1 is silently broken.
9. **Recent commits** (`git log -10`):
   - `6a60b08 ops: switch-gemini-model script + docs for 3.1↔2.5 fallback`
   - `1e761f7 deploy: scaffold AEC-mode-aware ALSA + CamillaDSP rendering`
   - `646c694 voice: TTS gain + pinned voice + timing logs + silent-failure detect + 409 retry`
   - `4a8f7a6 voice: NO_INTERRUPTION + inject current local time into system prompt`
   - `e8c9909 audio_io: support non-XVF3800 mics + dmix-direct TTS via PortAudio`
   - `8aa9222 wake: force inference_framework="onnx" on openwakeword Model`
   - `6404cad Phase 1B: route via ALSA _audioout override; CamillaDSP 4.x schema`
   - `40473ed Fix install on PiOS Trixie: camilladsp git URL + openwakeword on Py3.13`

   The commit messages explain WHY each change was made — read them, especially `646c694` and `4a8f7a6` because they explain the workarounds you're indirectly removing.

## Pi access

- SSH key already deployed: `ssh pi@jasper.local`
- sudo password if you need it interactively: `pipass`
- Service: `systemctl {status,restart,stop} jasper-voice`
- Daemon code at `/opt/jasper/jasper/`
- Env at `/etc/jasper/jasper.env`
- Repo source at `/home/pi/jts/`

## Useful commands cheat-sheet

```sh
# pull recent Pi logs
bash scripts/fetch-pi-logs.sh
# live tail
bash scripts/tail-pi-logs.sh
# switch Gemini model
bash scripts/switch-gemini-model.sh         # show current
bash scripts/switch-gemini-model.sh 3.1     # → gemini-3.1-flash-live-preview
bash scripts/switch-gemini-model.sh 2.5     # → gemini-2.5-flash-native-audio-preview-12-2025
# sync laptop → Pi → /opt/jasper
rsync -avz --delete --exclude .venv --exclude __pycache__ --exclude '.git/' --exclude 'logs/*' ./ pi@jasper.local:/home/pi/jts/
ssh pi@jasper.local "sudo rsync -a --delete --exclude __pycache__ /home/pi/jts/jasper/ /opt/jasper/jasper/ && sudo systemctl restart jasper-voice"
# tests
.venv/bin/pytest
```

## Definition of done

- One persistent Gemini Live session per daemon lifetime (verifiable by `journalctl` showing one connect at start, no connects per wake)
- 0% silent-failure rate on real user queries over a 10+-query test session
- Daemon survives intentional WebSocket disconnect (e.g., `tcpkill` against the genai endpoint) — reconnects within bounded backoff and resumes
- Daemon survives the 15-min audio cap (test by leaving running for 20 min, observe seamless re-resumption)
- 409 errors in Cloud Logging stop happening (or drop to background level)
- All workarounds in `gemini_session.py` that exist *because* of the per-wake architecture (the 409 retry on connect, the silent-failure detection on session-close) are still present but redundant — leave them in as defense in depth
- New tests for the reconnect state machine pass
- All existing tests still pass
- New work committed to the branch with clear messages, pushed

Don't break the working hardware bring-up while doing this. Don't change ALSA or CamillaDSP configs. Don't switch models. Don't pivot to a different LLM provider.
