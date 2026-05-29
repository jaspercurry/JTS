# HANDOFF — VAD / mic-stream test matrix (May 2026)

> **Active workstream.** Pick this up tomorrow in a fresh context window.
> The session that produced this doc spent ~6 hours diagnosing why weather
> queries weren't working reliably, found and fixed three real bugs, ran a
> structured A/B test matrix across 5 stream/VAD configurations, and
> identified the remaining open questions. This doc has everything needed
> to continue without rebuilding context.

**Last verified: 2026-05-24** — claims here reflect the deployed state at
`build.txt` SHA `597f6df-dirty` on `jts2.local`.

---

## TL;DR — what's the speaker running RIGHT NOW

| Setting | Value | Where |
|---|---|---|
| Primary mic | AEC stream (`udp:9876`) | `JASPER_MIC_DEVICE` default |
| Secondary wake leg | raw chip (`udp:9877`) | OR-gated for wake detection only |
| VAD | local Silero (sustained 240 ms, threshold 0.15) | `JASPER_SERVER_VAD_ENABLED=0` in `/etc/jasper/jasper.env` |
| AEC bridge | AEC3 v2 BEST_A + NS=low + AGC1 (target -9, max 18 dB) | bridge defaults |
| SimpleAGC on raw leg | OFF | `JASPER_AEC_RAW_AGC_ENABLED` default 0 |
| Debug WAV recording | OFF | `JASPER_DEBUG_RECORD_OPENAI_AUDIO` default 0 |

**This is "Cell 0" from the test matrix below.** It's the only config that
got 4/4 perfect transcripts. Three other configurations were tested and all
failed in specific, identifiable ways. Server VAD is currently disabled on
the production speaker per a line added to `/etc/jasper/jasper.env`.

---

## Three bugs fixed this session (already shipped)

All three were live regressions when the session started. The first two
broke the voice pipeline entirely; the third was a measurement gap.

### 1. `session.type` missing from `set_turn_detection`

[`jasper/voice/openai_session.py`](../jasper/voice/openai_session.py) —
`OpenAIRealtimeConnection.set_turn_detection`

OpenAI's Realtime API requires `session.type` on every `session.update`
event, not just the first. The initial session payload includes
`"type": "realtime"` correctly, but the per-turn switcher from PR #283
omitted it. Result: the API rejected every server-VAD activation with
`missing_required_parameter` (logged as a `RealtimeError` warning, not
propagated as a turn failure), the daemon believed server VAD was active,
and waited for `speech_started` events the server was never going to send.
5-second `no_speech` timeout → turn killed → no answer.

Fix: add `"type": "realtime"` to the session payload. Regression test in
[`tests/test_openai_session.py`](../tests/test_openai_session.py) asserts
both code paths (server_vad on + manual restore) include the field.

### 2. `_server_vad_response_trigger` killing the turn

[`jasper/voice_daemon.py`](../jasper/voice_daemon.py) —
`_server_vad_response_trigger`

`WakeLoop._handle_session_frame` treats any completed task in `_bg_tasks`
as "turn over" and calls `_end_turn`. The vad-trigger task was being added
to `_bg_tasks`, then it returned immediately after firing `response.create`.
Next mic frame → daemon saw a done task → tore down the turn ~50 ms after
asking OpenAI for a response. Response arrived ~1.7 s later, after turn
release, all audio deltas dropped. Manifested as "tool ran, no audio
played" in logs.

Fix: trigger task now `await asyncio.Event().wait()` after firing
`response.create`, so it stays "alive" until cleanup cancels it.

### 3. Local Silero not scored on primary stream during server-VAD mode

`_handle_session_frame` `if self._server_vad_this_turn: ... return`
short-circuits *before* the local Silero prediction. So when server VAD
was on, `_max_silero_score_in_turn` stayed at 0 and
`wake_events.max_silero_aec` stayed NULL. We had no way to compare
"what our Silero would have said" against "what OpenAI's server VAD said"
on the same audio.

Fix: shadow Silero now runs unconditionally on the primary stream in
the server-VAD branch (telemetry only, doesn't affect turn behavior).
~10 lines, no behavioral change.

---

## Instrumentation shipped (off by default)

Three env vars added. All safe to leave defined at 0 / unset.

### `JASPER_DEBUG_RECORD_OPENAI_AUDIO`

When set to `1`, the OpenAI session adapter tees every byte it sends to
the `input_audio_buffer.append` event into a per-turn WAV file under
`/tmp/jasper-openai-debug/`. This is the **ground truth** of what
OpenAI received — post-upsample to 24 kHz, mono, int16.

Defined in `OpenAIRealtimeConnection._send_audio_chunk` and closed in
`OpenAIRealtimeTurn.release`.

`PrivateTmp=yes` in the systemd unit hides the directory from outside the
service namespace. To retrieve:

```sh
ssh pi@jts2.local "sudo bash -c 'cp /tmp/systemd-private-*-jasper-voice.service-*/tmp/jasper-openai-debug/*.wav /tmp/ && chmod 644 /tmp/2026*.wav'"
scp 'pi@jts2.local:/tmp/2026*.wav' /tmp/jasper-openai-debug-pi/
```

Filenames are `YYYYMMDDTHHMMSSZ-<turn-id-hex>.wav`. Each turn = one WAV.

### `JASPER_AEC_RAW_AGC_ENABLED`

When set to `1`, the AEC bridge applies a Python-implemented
`_SimpleAGC` (peak-tracking, asymmetric attack/release) to the raw
chip stream before emitting it on `udp:9877`. Mirrors WebRTC AGC1's
parameter shape (`target_dbfs=9, max_gain_db=18`) but is a
homegrown implementation, not a WebRTC AGC1 instance.

**Has a known bug**: hard-clips at full scale on transients after the
gain has ramped up. See "Known issues" below before using.

Bridge config logs the state on startup:

```
raw_agc=on  (target=9 max=18dB)
raw_agc=off (target=9 max=18dB)
```

Defined in `_SimpleAGC` class + `_aec_loop` in
[`jasper/cli/aec_bridge.py`](../jasper/cli/aec_bridge.py).

### Shadow Silero on primary (always-on, no env var)

Whenever server-VAD mode is active, the daemon still runs Silero on
every primary-stream frame and tracks the max score per turn. This
populates `wake_events.max_silero_aec`. No env var — always on (cheap,
telemetry-only). Edit point: `_handle_session_frame` server-VAD branch
in [`jasper/voice_daemon.py`](../jasper/voice_daemon.py).

---

## The test matrix — all 5 cells, decisive

Protocol per cell: music off, user across the room, "Hey Jarvis, what's
the weather like tomorrow" × 5, ~30 s apart. Always the same phrase.
Debug WAV recording on for every cell.

| Cell | Primary mic | VAD | Threshold | silence_ms | Result |
|---|---|---|---|---|---|
| **0** | AEC (`udp:9876`) | local Silero | 0.15 | 800 | **4/4 transcripts perfect, 2/4 tool calls correct** |
| 1 | AEC | server VAD | 0.5 | 350 (default) | 0/5 — cuts mid-sentence |
| 1b | AEC | server VAD | 0.5 | 800 | 3/5 — VAD threshold flakiness |
| 1c | AEC | server VAD | 0.3 | 800 | 0/5 — fires on wake word, commits before command |
| 3 | raw + SimpleAGC | local Silero | 0.15 | 800 | 1/7 — SimpleAGC clipping + model hallucinations |

**Cell 2 (raw + server VAD) was not formally run** as a standalone cell —
we had earlier tested this config in an ad-hoc way and observed audio token
counts of 7-50 instead of 600+, attributed to OpenAI's stricter VAD
threshold against the raw stream's lower SNR.

### What each failure mode actually looks like

**Cell 1 (server VAD, 350 ms)** — `silence_duration_ms=350` is shorter
than natural mid-word pauses ("What's [pause] the weather"). Server VAD
fires `speech_stopped` mid-sentence on the first natural pause, commits
just "What's", OpenAI's STT hallucinates the rest. Transcripts: "Thank
you", "What's the question?", "That's what I'd like tomorrow."

**Cell 1b (server VAD, 800 ms)** — `silence_duration_ms` fixed to match
local Silero's window. Works *when audio is comfortably above threshold*,
but with across-the-room voice the audio sits right at OpenAI's
threshold=0.5 cliff. 2 of 5 attempts had `event=server_vad.no_speech`
timeouts despite local Silero scoring the same audio at 0.98
confidence. **OpenAI's VAD and our local Silero disagree on borderline
audio.**

**Cell 1c (server VAD, threshold 0.3)** — Lowering threshold to make it
more permissive made it *more* aggressive on transients. Server VAD now
reliably fires `speech_started` on the wake word audio (which is in the
pre-roll sent to OpenAI), then 800 ms later fires `speech_stopped` —
committing only the wake word before the user's command even arrives.
Transcripts: "What?", "That's...", "" (empty), with model hallucinating
`get_subway_arrivals`, `get_citibike_status`, `home_assistant("turn on
the bedroom lights")` from the fragments.

**Cell 3 (raw + SimpleAGC + local Silero)** — Two compounding problems:
(1) `_SimpleAGC` hard-clips at full scale once gain ramps up across
attempts — peaks at exactly 0.0 dB on attempts 3-7, which destroys
STT-readability. (2) Empty transcripts trigger the model hallucination
pattern, this time including `home_assistant("turn on the bedroom
lights")` *which actually succeeded* — your bedroom lights physically
turned on from an empty STT input.

### Why Cell 0 wins

| Factor | Cell 0 | Best other cell |
|---|---|---|
| Audio reaching OpenAI | AEC pipeline produces clean ~15 dB SNR | Raw is ~9.5 dB SNR (SimpleAGC + no NS), server VAD is borderline on AEC |
| VAD permissiveness | Local Silero threshold 0.15 (sustained 240 ms) | Server VAD 0.5 cliff is unforgiving for across-the-room |
| Wake-word interference | Local Silero ignores wake-word audio (it's in pre-roll, before sustained-speech tracking arms) | Server VAD fires on wake word and commits before command |
| Predictability | All 4 attempts identical good outcome | Server VAD non-deterministic on borderline audio |

---

## Known product bug (independent of all of the above)

**The model hallucinates tool calls with confident arguments on empty
STT input.** Observed across Cells 0, 1, 1c, and 3. When
`openai user transcript: ''` returns empty, the model still picks a tool
and invents arguments — examples seen:

- `set_volume(percent=60)`
- `adjust_volume(delta_percent=-10)`
- `adjust_volume(delta_percent=+25)`
- `get_volume()`
- `get_subway_arrivals(line='', direction='')`
- `get_citibike_status(station_label='')`
- **`home_assistant(query='turn on the bedroom lights')` — actually executed**

Last one is the worst: the speaker turned on bedroom lights based on
empty STT input. **This is a safety-relevant failure mode** — the model
should refuse to call tools (especially side-effecting ones) when STT
returned nothing.

**Initial fix landed 2026-05-24 (same day, follow-up PR):** the Unclear
Audio section in `SYSTEM_INSTRUCTION` was extended with explicit
fragment-trigger enumeration ("a short fragment like 'What?' or 'That's'")
and an empty-string-arguments anti-pattern guard ("if you find yourself
about to call a tool with empty-string arguments or arguments you're
inventing without having heard them, you don't have enough information —
say the clarification line instead"). Tests in
[`tests/test_system_prompt_unclear_audio.py`](../tests/test_system_prompt_unclear_audio.py)
pin the literal example fragments observed in production failures.

**This is a prompt-level guard, not a deterministic enforcement.** The
model can still hallucinate — but should do so less often with the
explicit triggers. A belt-and-suspenders deterministic guard in the
dispatch layer (refuse to execute side-effecting tools when the most
recent transcript is empty *and* the args are empty/default) is the
next-level fix if the prompt guard proves insufficient. Reference
[`docs/HANDOFF-prompting.md`](HANDOFF-prompting.md) for the
prompting playbook — the guard follows that doc's "enumerate triggers;
conditional rules over absolutes" convention.

---

## How to run a cell

Each cell is a systemd drop-in or a line in `/etc/jasper/jasper.env`.
Always restart `jasper-voice` (and `jasper-aec-bridge` if bridge env
changed) after edits.

### The right way to override env vars on this systemd version

`Environment=` in a drop-in does NOT override values from
`EnvironmentFile=` directives on Trixie's systemd. You must use
`EnvironmentFile=` in the drop-in pointing to a side file. Pattern:

```sh
ssh pi@jts2.local "sudo bash -c '
tee /var/lib/jasper/cell-X.env > /dev/null <<EOF
JASPER_FOO=bar
JASPER_BAZ=qux
EOF
chmod 600 /var/lib/jasper/cell-X.env

cat > /etc/systemd/system/jasper-voice.service.d/cell-X.conf <<EOF
[Service]
EnvironmentFile=/var/lib/jasper/cell-X.env
EOF

systemctl daemon-reload && systemctl restart jasper-voice
'"
```

Verify the override actually took effect by reading `/proc/<pid>/environ`,
NOT by `systemctl show -p Environment` (which can show stale values).

### Cell recipes (env-file contents)

**Cell 0** — production default (what's deployed now)
```
JASPER_MIC_DEVICE=udp:9876
JASPER_SERVER_VAD_ENABLED=0
```

**Cell 1b** — AEC + server VAD with 800 ms silence
```
JASPER_SERVER_VAD_ENABLED=1
JASPER_SERVER_VAD_SILENCE_MS=800
```

**Cell 1c** — same as 1b but threshold 0.3
```
JASPER_SERVER_VAD_ENABLED=1
JASPER_SERVER_VAD_THRESHOLD=0.3
JASPER_SERVER_VAD_SILENCE_MS=800
```

**Cell 3** — raw + SimpleAGC + local Silero. Needs BOTH bridge and voice
drop-ins.

Bridge `/etc/systemd/system/jasper-aec-bridge.service.d/raw-agc.conf`:
```
[Service]
Environment=JASPER_AEC_RAW_AGC_ENABLED=1
```

Voice `/var/lib/jasper/cell-3.env`:
```
JASPER_MIC_DEVICE=udp:9877
JASPER_MIC_DEVICE_RAW=udp:9876
JASPER_SERVER_VAD_ENABLED=0
JASPER_DEBUG_RECORD_OPENAI_AUDIO=1
```

Voice drop-in `/etc/systemd/system/jasper-voice.service.d/cell-3.conf`:
```
[Service]
EnvironmentFile=/var/lib/jasper/cell-3.env
```

Restart in order: bridge first (it produces the AGC'd stream), then voice
(it consumes it).

### Always enable debug recording when running a cell

Add `JASPER_DEBUG_RECORD_OPENAI_AUDIO=1` to whichever env file the cell
uses. Without it, you'll have telemetry but no ground truth of what
OpenAI actually received. Skipping this step burned hours during the
session that produced this doc.

---

## Analysis recipes

### Pull telemetry after a cell finishes

```sh
PI_HOST=jts2.local SINCE='10 minutes ago' bash scripts/fetch-pi-logs.sh
PI_HOST=jts2.local NO_OPEN=1 bash scripts/fetch-wake-events.sh
ssh pi@jts2.local "sudo bash -c 'cp /tmp/systemd-private-*-jasper-voice.service-*/tmp/jasper-openai-debug/*.wav /tmp/ && chmod 644 /tmp/2026*.wav'"
scp 'pi@jts2.local:/tmp/2026*.wav' /tmp/jasper-openai-debug-pi/
```

### Find every turn-funnel event for the last N minutes

```sh
grep -E '17:[0-9]' logs/jasper-voice-latest.log \
  | grep -E 'wake.detected|server_vad|user transcript|response.done|tool|RECORDING|silence detector|end-of-utterance|shadow_vad|RealtimeError'
```

### Per-event telemetry from SQLite

```sh
sqlite3 wake-events/latest/wake-events.sqlite3 \
  "SELECT substr(event_id, -6) eid, time(ts_utc) ts,
          peak_score_aec_on aec_wk, peak_score_aec_off raw_wk,
          mic_rms_dbfs_on rms_aec, mic_rms_dbfs_off rms_raw,
          max_silero_aec sil_aec, max_silero_raw sil_raw,
          endpointer, outcome, tool_name
   FROM wake_events
   WHERE ts_utc > datetime('now', '-15 minutes')
   ORDER BY ts_utc DESC" -header
```

**Important interpretation note**: column names `_on` / `_off` and
`_aec` / `_raw` describe **which detector instance** (primary vs
secondary), NOT which underlying stream. When you swap the mic device
(Cell 3), `aec_on` actually means "raw+AGC primary leg." Re-read the
schema in [`jasper/wake_events.py`](../jasper/wake_events.py) before
interpreting if you're in a non-default mic config.

### Analyze debug WAV levels

Save as `/tmp/analyze_wavs.py`:

```python
import wave, struct, math, os
files = sorted(f for f in os.listdir("/tmp/jasper-openai-debug-pi") if f.endswith(".wav"))
print(f"{'file':<48}{'dur_s':>7}  {'rms_dB':>8}  {'peak_dB':>8}  {'>-25dB%':>9}  {'>-20dB%':>9}  {'>-15dB%':>9}")
print("-" * 92)
for f in files:
    p = f"/tmp/jasper-openai-debug-pi/{f}"
    with wave.open(p, "rb") as w:
        sr = w.getframerate(); n = w.getnframes(); data = w.readframes(n)
    samples = struct.unpack(f"<{n}h", data)
    arr = [s/32768.0 for s in samples]
    rs = sum(x*x for x in arr); rms = math.sqrt(rs/len(arr))
    peak = max(abs(x) for x in arr)
    db = lambda x: 20*math.log10(x) if x > 1e-10 else -120
    frame_n = sr // 12
    counts = {25:0, 20:0, 15:0}; nframes = 0
    for j in range(0, len(arr)-frame_n+1, frame_n):
        chunk = arr[j:j+frame_n]
        f_db = db(math.sqrt(sum(x*x for x in chunk)/len(chunk)))
        nframes += 1
        for th in counts:
            if f_db > -th: counts[th] += 1
    pct = lambda c: 100*c/nframes
    print(f"{f:<48}{n/sr:>5.2f}  {db(rms):>7.1f}  {db(peak):>7.1f}  "
          f"{pct(counts[25]):>7.0f}%  {pct(counts[20]):>7.0f}%  {pct(counts[15]):>7.0f}%")
```

**Healthy audio** for OpenAI to transcribe: RMS around -24 dB, peak
around -4 dB, ~25% of frames above -25 dB. The successful Cell 0 turns
matched these levels almost exactly. Outliers from this range correlate
with failures.

**Watch for peak = 0.0 dB** — that's hard clipping. Caused by the
SimpleAGC bug. Audio with peak = 0.0 is unreliable for STT.

### Per-frame profile of a single WAV (find where speech is)

```python
import wave, struct, math
with wave.open("/tmp/jasper-openai-debug-pi/<file>.wav", "rb") as w:
    sr = w.getframerate(); n = w.getnframes(); data = w.readframes(n)
samples = struct.unpack(f"<{n}h", data)
arr = [s/32768.0 for s in samples]
frame_n = sr // 12  # 80ms
for i in range(0, len(arr)-frame_n+1, frame_n):
    chunk = arr[i:i+frame_n]
    rms = math.sqrt(sum(x*x for x in chunk)/len(chunk))
    db = 20*math.log10(rms) if rms > 1e-10 else -120
    bar = "*" * max(0, int(60 + db))
    mark = "  SPEECH" if db > -25 else ""
    print(f"  f{i//frame_n:02d} t={i/sr:5.2f}s  {db:6.1f}  {bar}{mark}")
```

The WAV starts ~560 ms BEFORE wake-fire (pre-roll), so the wake word
audio is in the first 0.5-0.7 s of the WAV; the user's command starts
~1-2 s into the WAV (varies).

---

## Open hypothesis for tomorrow

**The user (correctly) believes the right ultimate answer is raw mic
stream + a good AGC, leveraging the speaker's strong ducking to make
AEC unnecessary.** Cell 3 didn't validate this because `_SimpleAGC`
has the hard-clipping bug and lacks NS. The actual experiment to run:

### Option B (from earlier analysis): real WebRTC AGC1 on the raw path

Create a second `webrtc::AudioProcessing` instance with:
- `echo_canceller.enabled = false` (no AEC needed, ducking handles echo)
- `noise_suppression.enabled = true, level = kLow` (mirror what AEC pipeline uses)
- `gain_controller1.enabled = true, mode = kAdaptiveDigital, target_level_dbfs = 9, compression_gain_db = 18`
- `high_pass_filter.enabled = true`

Wire it in `aec_bridge.py` parallel to the existing AEC3 path:

```python
# Existing:  chip_mic → AEC3+AGC1+NS → udp:9876 (primary)
# Existing:  chip_mic → SimpleAGC (broken) → udp:9877 (raw)
# Proposed:  chip_mic → AGC1+NS only      → udp:9877 (raw, properly normalized)
```

The v2 binding currently hardcodes `cfg.echo_canceller.enabled = true`
at [`jasper_aec3/src/aec3_binding_v2.cpp`](../jasper_aec3/src/aec3_binding_v2.cpp) —
would need to expose this as a constructor arg. Modest C++ rebuild +
new pybind11 binding update.

Once shipped: run as Cell 4 — raw+WebRTC-AGC1 + local Silero. If it
beats Cell 0, that's the new production default. If it matches Cell 0,
stick with Cell 0 (AEC is doing useful work even when ducking is strong).

### Cleanup of `_SimpleAGC` before going down this path

The current `_SimpleAGC` in `aec_bridge.py` is documented as
"experimental" and gated off by default. Before spending engineering on
Option B, consider whether to:

(a) Fix `_SimpleAGC`'s clipping bug (replace `np.clip` with `tanh`
soft-limit; add peak-aware gain reduction) and re-test Cell 3 — if it
suddenly works, we don't need Option B. Cheap test.

(b) Remove `_SimpleAGC` entirely as a misleading experiment and go
straight to Option B.

Recommendation: do (a) first, ~30 min effort, definitive test of
"is the AGC quality the only thing wrong with the raw path."

---

## Other quick fixes worth doing tomorrow

1. **Model hallucination on empty STT** — see "Known product bug"
   section above. Update `SYSTEM_INSTRUCTION` to handle empty input
   gracefully. Most-impactful single fix from this session's findings.

2. **Document the systemd `EnvironmentFile=` vs `Environment=` precedence
   gotcha** somewhere durable. Burned ~30 min during the session. Either
   add to AGENTS.md or a new `docs/HANDOFF-systemd-overrides.md`.

3. **The `peak_score_aec_on` / `_off` column naming** in
   `wake_events.sqlite3` is misleading when the mic device is swapped
   (Cell 3). Either rename to `peak_score_primary` / `peak_score_secondary`
   or add a `primary_stream_id` column that records what was actually on
   each leg.

4. **Resolved 2026-05-29 in the provider TTS flush branch.** The OpenAI
   session adapter used to emit *"Cancellation failed: no active response
   found"* whenever a turn ended without an active response. The cancel
   path now suppresses that specific no-active-response error while
   preserving real provider errors.

---

## File index — what was touched

| File | Change |
|---|---|
| [`jasper/voice/openai_session.py`](../jasper/voice/openai_session.py) | + `session.type` in `set_turn_detection`; + `_debug_wav` per-turn; + close on `release`. Comments explain both. |
| [`jasper/voice_daemon.py`](../jasper/voice_daemon.py) | + shadow Silero on primary in server-VAD branch; + `_server_vad_response_trigger` awaits forever after `response.create`. Both have explanatory comments. |
| [`jasper/cli/aec_bridge.py`](../jasper/cli/aec_bridge.py) | + `_SimpleAGC` class + `JASPER_AEC_RAW_AGC_ENABLED` gate. Has docstring + warning about hard-clipping bug. |
| [`tests/test_openai_session.py`](../tests/test_openai_session.py) | + `assert su["session"]["type"] == "realtime"` in two existing tests to catch missing-session.type regression. |
| `/etc/jasper/jasper.env` on Pi | + `JASPER_SERVER_VAD_ENABLED=0` (Cell 0 production override). Not in repo. |

---

## Cross-references

- Architecture / what each daemon does:
  [README.md](../README.md), [AGENTS.md](../AGENTS.md)
- Voice provider abstraction (Gemini / OpenAI / Grok):
  [HANDOFF-voice-providers.md](HANDOFF-voice-providers.md)
- Outputd speaker reference, playout ledger, and future robust
  barge-in contract:
  [HANDOFF-speaker-output-reference.md](HANDOFF-speaker-output-reference.md)
- The persistent Live session lifecycle:
  [HANDOFF-persistent-live-session.md](HANDOFF-persistent-live-session.md)
- AEC bridge + WebRTC AEC3 tuning history:
  [HANDOFF-aec.md](HANDOFF-aec.md)
- Wake-event telemetry SQLite schema + queries:
  [HANDOFF-wake-telemetry.md](HANDOFF-wake-telemetry.md)
- Mic mute persistence (referenced once during session as a workaround
  consideration, not used): [AGENTS.md](../AGENTS.md) "Mic mute" section
- Prompting playbook (for the model-hallucination fix):
  [HANDOFF-prompting.md](HANDOFF-prompting.md)

Last verified: 2026-05-24
