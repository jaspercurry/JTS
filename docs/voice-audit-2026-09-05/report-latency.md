# Audit: voice-loop latency budget

## 1. Latency budget

Default production shape assumed: XVF3800 → `jasper-aec-bridge` (WebRTC AEC3) → UDP `:9876` → `jasper-voice` → fan-in → CamillaDSP → outputd → Apple-dongle DAC; peering off; server-VAD off; barge-in off; context-reset off. **Bold** = derived from a cited constant. *(est.)* = my estimate, not derivable at HEAD.

### A. Mic ADC → `_handle_wake_frame`

| Sub-stage | added ms (min/typ/max) | source | notes |
|---|---|---|---|
| XVF3800 chip DSP + UAC2 transport | 5 / 10 / 15 *(est.)* | — | not derivable in-repo |
| PortAudio/ALSA input buffer | ~30 / **35** / 45 *(est.)* | `jasper/cli/aec_bridge_config.py:167`; `jasper/cli/aec_bridge_capture.py:136-143` | `JASPER_AEC_CAPTURE_LATENCY` **unset by default** → sounddevice's `latency='high'` → PortAudio `defaultHighInputLatency`. The real value is already logged: `event=aec.mic_stream_latency` (`aec_bridge_capture.py:145-160`) |
| Capture block | 0 / 10 / **20** | `aec_bridge_engines.py:30` (`FRAME_SAMPLES = 320`) | 20 ms at 16 kHz |
| `mic_q` thread hop | 0 / 0.5 / 640 | `aec_bridge.py:179` (`QUEUE_MAXSIZE=32`), `:650` | 32 × 20 ms = 640 ms of hidden slack |
| AEC3 `process()` | 0 alg. / 2 / 4 *(est.)* | `aec_bridge_engines.py:78-131` | block-in/block-out, 2 × 10 ms windows — no algorithmic delay |
| UDP packetization to 1280 samples | 0 / 30 / **60** | `aec_bridge_telemetry.py:40, 378-397` | 4 × 20 ms blocks batched into one 80 ms packet |
| UDP → `asyncio.Queue(64)` → wake loop | 0 / 0.5 / 5120 | `jasper/audio_io.py:305, 355-366` | 64 frames = 5.12 s of hidden backlog; **no depth metric anywhere** |
| **Stage A total (a given sample)** | **~35 / ~88 / ~145** | | Newest sample in a delivered frame is only ~38 ms old; the 0–60 ms batching is the spread |

DTLN (`jasper/aec_engines/dtln.py:46-47`) would add **24 ms** algorithmic (`BLOCK_LEN 512 − BLOCK_SHIFT 128`) — it is an opt-in corpus leg, not the production `on` leg. `jasper/enhanced_aec.py` is an *installer/fingerprint* module, not on the audio path.

### B. Wake fire → listening cue

| Sub-stage | added ms | source | notes |
|---|---|---|---|
| Frame quantization for detection | 0 / 40 / **80** | `audio_io.py:166` | wake-word end lands anywhere in the 80 ms frame (this *is* the packetization row above, counted once) |
| openWakeWord model delay | 0 / 100 / 200 *(est.)* | — | intrinsic; `jasper/wake.py:62-64` |
| `score_frame` per leg | 2 / 4 / 8 *(est.)* | `voice_daemon.py:3389` | ONNX melspec+embed+classify, ×N legs |
| detector resets (all legs) | ~0 | `voice_daemon.py:3430-3431` | |
| `_fuser.verify()` | **0** | `jasper/wake_fusion.py:74` — `return True` | unconditional no-op today |
| condition/RMS/ring-floor | 0.5 / 1 / 3 *(est.)* | `voice_daemon.py:3536, 3597-3601` | numpy over ≤100 frames |
| `os.environ` × 9 for `bridge_config` | **~0** | `voice_daemon.py:3565-3575` | dict lookups, **not** file reads |
| `await store.begin_event()` | 0.5 / **1** / 50+ | `voice_daemon.py:3604`; `wake_events.py:13, 411-413` | synchronous SQLite INSERT **on the event loop, inline, before arbitration and before the cue** |
| task hop → `_arbitrate_acquire_drain` | 0 / 1 / 5 *(est.)* | `voice_daemon.py:3635` | |
| peer arbitration | **0** solo / 150 / 500 | `voice_daemon.py:4171-4172`; `peering/config.py:60`; `peering/daemon.py:96`; ADR-0127 | returns WIN synchronously with peering off, without yielding |
| `_output_gate.begin_turn()` | 0 / 0 / unbounded | `voice_daemon.py:4857`; `output_gate.py:88-104` | waits on `_idle` only if a cue owns output |
| `_prepare_assistant_loudness_context()` **before** the cue | 3 / 8 / **5000** | `voice_daemon.py:4858`; `volume_coordinator.py:1500-1545` (×3 retry loop at `:1513`); `camilla.py:49, 492-511` | **a CamillaDSP websocket round-trip (2 commands) + a state-file load + a UDS `PREPARE_ASSISTANT`, all awaited before the chirp task exists.** Bounded by `CAMILLA_ATTEMPT_BUDGET_S = 5.0` |
| chirp task schedule + 2 × `to_thread` + first AUDIO write | 1 / 3 / 8 *(est.)* | `audio_io.py:1687-1699, 879` | chirp is 3 IPC chunks; pacing does not engage |
| **wake fire → chirp bytes at fan-in** | **~5 / ~14 / 5000+** | | ~165 ms with peering on |
| output chain to speaker (below) | 20 / 25 / 55 | | |
| **wake fire → audible cue** | **~25 / ~40 / —** | | user-perceived: + stage A + model delay ≈ **150–250 ms typ** from the end of "Hey Jarvis" |

**Does the cue wait on anything?** Yes: peer arbitration, the output gate, and one CamillaDSP websocket round-trip + one UDS round-trip. It does **not** wait on ducking, `acquire_turn`, or the provider — those all happen after the fire-and-forget spawn (`voice_daemon.py:4862-4865`).

### C. Turn acquire + first audio to provider

| Sub-stage | added ms | source |
|---|---|---|
| `_resolve_barge_in_for_turn()` | ~0 | `voice_daemon.py:4930`; `provider_state.py:309-312` — mtime-gated, one `os.stat` |
| `content_activity.refresh_now()` | 2 / 5 / 5000 | `voice_daemon.py:4934, 627` — **a second CamillaDSP WS round-trip** |
| `_prepare_assistant_loudness_context()` (again) | 3 / 8 / 5000 | `voice_daemon.py:4935` — **third + fourth Camilla WS commands of the same turn** |
| `tts.pause_content_meter()` | 0.5 / 1 / 1000 | `voice_daemon.py:4936`; `audio_io.py:684` |
| `ducker.duck()` | 0.5 / 2 / 1000 | `voice_daemon.py:4948, 293-297` — `to_thread` + UDS connect/send/close, 1 s sock timeout |
| `usage_store.open_session()` | 0.3 / 1 / 50 | `voice_daemon.py:4950`; `usage.py:556-560` — synchronous SQLite INSERT |
| `acquire_turn()` **warm** | **~1 / 3 / 10** | `openai_session.py:1020-1046`; `gemini_session.py:749-785`; `_supervisor.py:436-437` — pre-opened supervised WS; `_maybe_reset_context` returns instantly (`config.py:761-770`, default 0) |
| `acquire_turn()` **paused** | — / 350 / **1200** then up to 15 s | `voice_daemon.py:371` (`PAUSED_CONNECTION_WAIT_SEC=1.2`), `:4570` (50 ms poll); `_supervisor.py:439-447` |
| pre-roll: 7 × `send_audio` | 2 / 5 / 20 | `voice_daemon.py:422, 4995-5003` — 560 ms of audio |
| **wake fire → first audio at provider** | **~15 / ~35 / 1200+** | already logged: `voice_daemon.py:5019-5028` |

Acquire latency **never clips the command**: frames buffer from wake fire (`voice_daemon.py:3626-3627`, `:2472-2476`) into a 250-frame / 20 s deque (`audio_buffer.py:52`) and drain FIFO (`:113-129`).

### D. Endpointing (local Silero, the shipped path per ADR-0152)

| Sub-stage | added ms | source |
|---|---|---|
| `max` over sub-chunks marks a part-speech frame as speech | 0 / 40 / **80** | `jasper/vad.py:114-117` |
| clock anchored at frame **arrival**, not frame start | **+80** always | `voice_daemon.py:4520-4521` — the first silent frame already contains 80 ms of silence that is never counted |
| the silence constant | **800** | `voice_daemon.py:405` |
| evaluated only on frame boundaries | 0 / 40 / **80** | `voice_daemon.py:4522` |
| Silero pass runs **before** the frame is forwarded | 2 / 4 / 8 *(est.)* | `voice_daemon.py:4451` vs `:4531` |
| capture chain (newest-sample age) | 35 / 38 / 60 | stage A |
| **true end-of-speech → `end_input()` on the wire** | **~915 / ~1000 / ~1100** | |

So the **floor is not 800 ms + 80 ms of granularity — it is ~880 ms of audio-domain silence structurally, ~960 ms typical**, before any capture latency. Arming costs `SUSTAINED_SPEECH_TO_ARM_SEC = 0.20` + `SPEECH_RUN_PEAK_MIN = 0.60` (`:477, :490`) but that runs concurrently with speech and does not add to end-of-turn latency; it can cost a whole turn (`NO_SPEECH_ABORT_SEC = 5.0`, `:455`) if a quiet start never peaks ≥ 0.60. `HARD_RECORDING_CAP_SEC = 30` (`:413`) is a backstop only.

Server-VAD branch (`:4392-4437`) hands endpointing to the provider (`server_vad_silence_ms = 350`, `config.py:716-718`) and is **default off** (`config.py:712`) with ADR-0152's A/B behind it. PTT branch (`:4386-4389`) is closed by button release; server VAD is refused loudly on those turns (`:4967-4982`).

### E. Provider first chunk → first audible byte

| Sub-stage | added ms | source | notes |
|---|---|---|---|
| `on_response_started` awaited before the first write | 0.5 / 1 / **100** | `turn_playback.py:160-172`, `_RESPONSE_OBSERVER_TIMEOUT_SEC = 0.1` (`:19`) | a SQLite UPDATE inline on the first-audible path |
| resample 24→48k, mono→stereo, quantize | 0.5 / 1 / 3 *(est.)* | `audio_io.py:1642-1652` | |
| 2 × `to_thread` (gain, SEGMENT_START) + `to_thread` write | 1 / 2 / 6 *(est.)* | `audio_io.py:1687-1699, 879-896` | |
| **pace-ahead** | **0** | `audio_io.py:1716-1723` | `pace_excess = (queued_end − now) − 1.2`; the ledger is empty on the first chunk, so it is −1.2 → **never delays first audio** |
| **fan-in prebuffer** | **0** | `tts.rs:582-588, 678-690`; `mixer.rs:1697, 1886` | `prepare_period()` is true at `pending_frames() > 0`; mixed in the same period |
| **loudness lookahead** | **0** | `tts.rs:874-890`; `loudness.rs:434-448` | gain decided at SEGMENT_START from the stored `AssistantProfile.source_lufs`; the passive meter only copies bytes (`assistant_loudness.py:251-260`) and `finish()` runs in a post-segment task (`audio_io.py:1789-1815`) |
| **TTS fade-in** | **0** | `tts.rs:1248-1254` | `GainRamp` sets current = target on first init; the 100 ms ramp applies only to a *change* |
| Ring A | **5.3** | `fanin_coupling.py:63-64`; `deploy/alsa/conf.d/60-jts-ring.conf` geometry note | |
| CamillaDSP (certified ring pair) | **2.7 + 2.7** | `fanin_coupling.py:98-99` | but the room-correction sound graph carries the DAC profile floor **chunk 256 / target 1536** (`audio_hardware/dac.py:433`) via `camilla_config_contract.py:336-411` — read the live sum, do not trust either |
| Ring B + outputd DAC buffer | **5.3 + 5.3** | `audio_hardware/dac.py:429-432` (period 128 / buffer 256) | packaged fallback is 1024/3072 (`rust/jasper-outputd/src/config.rs:17-18`) |
| **provider chunk → speaker** | **~22 / ~28 / 55+** | live sum already on `/system/audio/`: `jasper/control/audio_health.py:1758-1779` | |

Program duck attack is 15 ms (`rust/jasper-fanin/src/config.rs:1088`) and does not gate TTS.

### F. Barge-in (default OFF — `provider_state.py:304-319`)

| Sub-stage | added ms | source |
|---|---|---|
| capture chain | 35 / 38 / 60 | stage A |
| sustained-run gate at 80 ms granularity | **240** / 240 / 320 | `voice_daemon.py:4141-4143`, `:498` (`= SUSTAINED_SPEECH_TO_ARM_SEC = 0.20`) — fires on the 4th frame |
| interrupt → race wake → `tts.flush()` | 1 / 3 / 10 *(est.)* | `turn_playback.py:184-197`, `:57` |
| FLUSH_SYNC drained at the next fan-in period | 0 / 3 / **5.3** | `tts.rs:1096-1139`; `mixer.rs:1697` |
| already-committed downstream audio (unstoppable) | 22 / 28 / 55 | stage E output chain |
| **user speech onset → speaker silent** | **~300 / ~315 / ~450** | |

### G. Existing measurement

**What exists**

- `_wake_event_at_monotonic` (`voice_daemon.py:3483`) — the wake-fire anchor.
- `turn acquire done in %.0fms (sched_lag=… state=… loudness_prepare=… duck=… acquire=…)` (`voice_daemon.py:5019-5028`) — **the best instrument in the tree**; a real per-turn breakdown of stage C.
- `wake_events` `ts_*` columns at millisecond ISO (`wake_events.py:88-101, 261-265`): `turn_opened`, `speech_detected`, `response_started`, `turn_complete` + `silero_aec_armed_at_ms` (`voice_daemon.py:4497-4500`).
- `event=aec.mic_stream_latency` (`aec_bridge_capture.py:145-160`) — the one number that settles stage A's biggest unknown.
- `/system/audio/` live output-chain sum (`audio_health.py:1758-1779`, ADR-0185).
- `jasper/voice/trace.py` — event-ordered turn trace, but it is the **eval harness's** schema (no-op unless a trace is set active) and carries no latency assertions.
- `jasper/route_latency/`, `jasper/cli/route_latency_harness.py`, ADR-0108/0185 — these measure the **USB-in music route**, not the voice loop. Nothing there answers a voice-latency question.

**What does not exist**

- **No single per-turn timeline.** None of `wake_fire_ts`, `cue_ts`, `first_audio_to_provider_ts`, `eou_ts`, `end_input_ts`, `first_audible_ts` is recorded anywhere as a delta. `/state.voice` (`voice_daemon.py:4755-4800`) carries `endpointer`, barge-in counts and leg lists — **zero timing fields**.
- **The one line that looks like provider latency is not.** `first audio chunk from OpenAI in %.0fms (turn start→1st chunk)` (`openai_session.py:668-676`; `gemini_session.py:427-433`) is anchored at `_started_at_monotonic`, set in `acquire_turn` (`openai_session.py:1032`). It therefore includes the user's entire utterance **plus** the ~1 s endpointer. **It must not be read as provider latency, and today it is the only number that looks like one — which is very likely why the cloud providers appear slow.**
- **No mic-queue depth.** `audio_io.py:236-241, 364-366` log only on overflow; 64 frames of silent backlog is invisible.

**Where the hooks go (one field each)**

| Field | Site |
|---|---|
| `wake_fire_ts` | already `voice_daemon.py:3483` |
| `cue_ts` | `_play_listening_chirp`, immediately before `self._tts.write_segment` (`voice_daemon.py:3103`) |
| `first_audio_to_provider_ts` | first `send_audio` of the pre-roll loop (`voice_daemon.py:4997`) |
| `eou_ts` (speech end) | `self._silence_started_at = now` (`voice_daemon.py:4521`) |
| `end_input_ts` | `_end_session_input` (`voice_daemon.py:4178-4192`) — one implementation, all three endpointers |
| `first_response_chunk_ts` | `_record_response_started` (`voice_daemon.py:3757`) already fires exactly there — record a monotonic delta, not just an ISO stamp |
| `first_audible_ts` | honest cheap version: `first_response_chunk_ts` + the `/system/audio` live queue sum. Exact version: fan-in's `PlayoutLedger` already counts committed frames per segment and the FLUSH_SYNC ack returns `max_audio_played_ms` (`tts.rs:1112-1131`) — a SEGMENT_START → first-committed-frame report on that same ack gives a true acoustic anchor |
| emit | one `event=turn.timeline` line in `_end_turn_inner` (`voice_daemon.py:5176`) with seven deltas, plus the last turn's deltas in the status dict (`voice_daemon.py:4755-4800`) so `/state.voice` carries it |

---

## 2. Ranked improvements (ms saved / complexity)

| # | Change | est. ms saved | Cx | Risk | file:line | ADR conflict |
|---|---|---|---|---|---|---|
| 1 | Set `JASPER_AEC_CAPTURE_LATENCY=low`. The knob exists and is validated; unset today ⇒ PortAudio `defaultHighInputLatency`. | **20–30 on every stage** (wake, EOU, barge-in) | S | M — ALSA xruns on the XVF UAC2 endpoint; the stall watchdog + systemd restart already cover it, and `event=aec.mic_stream_latency` measures the before/after | `aec_bridge_config.py:167`; `aec_bridge_capture.py:138-143` | none |
| 2 | Anchor the EOU silence clock at the frame's **start**, not its arrival (`now − 0.08`, or count silent frames). Today the first silent frame's own 80 ms is never counted. | **80** off every turn | S | S — pure bookkeeping, the 800 ms constant is untouched | `voice_daemon.py:4520-4521` | none (does not change ADR-0152's constant) |
| 3 | Make `effective_volume_context()` serve a cached snapshot refreshed on mutation instead of a live CamillaDSP round-trip. Kills 2 of the 4 Camilla WS commands per wake turn and takes a 5 s-bounded call off time-to-listen. | 5–15 typ, **up to 5000 tail** | M | M | `volume_coordinator.py:1500-1545`; `camilla.py:49` | none |
| 4 | Drop `content_activity.refresh_now()` from the turn-open path. Its only consumers are wake telemetry and the default-off server-VAD branch, and the tracker already polls at 1 Hz. | 2–5 typ, 5000 tail | S | S | `voice_daemon.py:4934`, `:520`; `config.py:712` | none |
| 5 | Make `begin_event()` fire-and-forget (the `event_id` is already generated locally at `:3546`, and `_finalize_event_audio` at `:3628` is already this shape). | 1 typ, 50+ tail, off time-to-listen | S | S — needs a small ordering guard vs `update_stage` | `voice_daemon.py:3604` | none — `wake_events.py:31-37` rejects `run_in_executor`, not fire-and-forget |
| 6 | Delete the duplicate `_prepare_assistant_loudness_context()` at turn-inner (the pre-chirp one at `:4858` is load-bearing for the chirp's level — `loudness.rs:420-448` reads the volume context). | 3–8 typ | S | M — confirm fan-in needs it once per episode | `voice_daemon.py:4935` vs `:4858` | none |
| 7 | Emit the primary leg in 20 ms UDP packets and decouple wake framing (80 ms) from transport/VAD framing. The `usb_host_mic` leg **already** emits at `frame_samples=320` through the same emitter, so the mechanism exists. | **0–60 (avg 30)** on EOU, barge-in and audio-to-provider | M | M — `PRE_ROLL_FRAMES`, `CAPTURE_RING_FRAMES`, `WAKE_STALE_SCORE_SEC` and `drain_acquire_buffer(min_consecutive_speech=3)` are all expressed in 80 ms frames | `aec_bridge.py:437-446`; `aec_bridge_telemetry.py:40`; `voice_daemon.py:422, 392, 378` | **ADR-0152** — its constants were swept at 80 ms granularity and would need a re-sweep |
| 8 | Use the **last** sub-chunk score (not `max`) for the EOU decision, keeping `max` for gating/barge-in. `max` makes a part-speech frame read as speech, starting the silence clock a frame late. | 0–80 | M | M — needs the sub-chunk vector `predict` currently collapses | `jasper/vad.py:114-117`; `voice_daemon.py:4451` | **ADR-0152** (changes the effective behaviour of a measured constant) |
| 9 | Lower `END_OF_UTTERANCE_SILENCE_SEC` 0.8 → 0.6, or make it adaptive (shorter after a complete-sounding phrase). | **200** per turn | S (constant) / L (adaptive) | H — this is the single most user-visible tuning knob and ADR-0152 states the constants are "a measured change, not a preference" | `voice_daemon.py:405` | **ADR-0152** |
| 10 | Move the ordinary sound graph onto the certified ring pair (chunk 128 / target 128). | ~25–30 off cue onset, TTS onset and barge-in stop | M | **H** — `fanin_coupling.py:114-117` explicitly calls this "a retune with a listening test, not a refactor"; xrun risk | `fanin_coupling.py:98-101`; `audio_hardware/dac.py:433` | none directly, but it is DSP on the output path ⇒ **AGENTS.md non-negotiable tier review** |
| 11 | Short-circuit `_peer_arbitrate` to WIN when the peer table is empty. | 150 (multi-speaker households only) | M | M | `voice_daemon.py:4171-4172`; `peering/config.py:60` | **ADR-0127** — determinism is the stated safety property; the concurrent-multi-waker race is already flagged unresolved there |
| 12 | Forward the session frame to the provider **before** the Silero pass. | 2–5 per frame *(est.)* | S | S — the abort/cap/EOU branches deliberately drop their frame; sending first sends one extra silent frame | `voice_daemon.py:4451` vs `:4531` | none |

**Candidates the code shows are already handled — reject:**

- *"Reduce TTS prebuffer / pace-ahead."* `_OUTPUTD_PACE_AHEAD_SEC` cannot fire on the first chunk (`audio_io.py:1716-1723`), fan-in has no prebuffer (`tts.rs:582-588`), there is no loudness lookahead (`tts.rs:874-890`), and there is no fade-in (`tts.rs:1248-1254`).
- *"Pre-warm the provider turn."* Already a supervised persistent WS; `await_connected` returns without yielding when connected (`_supervisor.py:436-437`), context reset defaults to 0 (`config.py:761-770`), and a paused connection is bounded at 1.2 s with the acquire buffer covering the gap.
- *"Play the cue before any awaits."* The cue is already fire-and-forget and overlaps turn open (`voice_daemon.py:4860-4865`); the two remaining awaits are the real target (#3, #6), not the ordering — reordering alone would put the chirp on a no-context loudness fallback, which is exactly what the comment at `:4854-4856` prevents.
- *"Drop env reads off the wake path."* There are none. `bridge_config` is nine `os.environ` dict lookups (`:3565-3575`); `read_barge_in_enabled` is mtime-gated to a single `os.stat` (`provider_state.py:309-312`). The SQLite write is real (#5); the env reads are not.
- *"Run Silero at native 32 ms."* Not reachable without #7 — the wrapper hands the whole frame to openWakeWord's VAD (`vad.py:114`). What *is* reachable at 80 ms is #8.

---

## 3. Duplicated / conflicting timing logic

1. **The arming gate exists twice in two units.** `SUSTAINED_SPEECH_TO_ARM_SEC = 0.20` (`voice_daemon.py:477`) vs `drain_acquire_buffer(min_consecutive_speech=3)` ≈ 240 ms (`audio_buffer.py:57`), kept in sync only by a comment (`audio_buffer.py:83-95`). Any frame-size change silently desyncs them, and the acquire path bypasses the live gate entirely.
2. **`_prepare_assistant_loudness_context()` runs twice per wake turn** (`voice_daemon.py:4858` and `:4935`) — two identical Camilla volume reads and two `PREPARE_ASSISTANT` sends for one turn.
3. **Four CamillaDSP websocket round-trips per wake turn**, from three call sites (`:4858`, `:4934`, `:4935`), each independently bounded at 5 s, all between the wake and `acquire_turn`.
4. **"How long is a frame" is declared independently in two processes** — `MicCapture.OUTPUT_FRAME_SAMPLES = 1280` (`audio_io.py:166`) and `OUT_FRAME_SAMPLES = 1280` (`aec_bridge_telemetry.py:40`). Nothing at runtime checks agreement; `_UdpMicProtocol` accepts any even-length datagram (`audio_io.py:355-366`).
5. **Two constants are justified by a transport that no longer exists.** `WAKE_REFRACTORY_SEC = 0.2` is sized to "the dongle dmix … 4096 frames @ 48 kHz ≈ 85 ms" (`voice_daemon.py:313-315`), and `tts_drain_tail_sec = 0.085` to "the Apple dongle dmix" (`config.py:690-698`). ADR-0100 retired that route; the path is fan-in → ring → CamillaDSP → outputd. The real number is the live chain depth `/system/audio` already sums.
6. **Turn-end is decided by two racers against one anchor** — `wait_drained()` (`turn_playback.py:222`) and `_idle_watchdog`'s `expected_drain_at()` poll (`turn_playback.py:281, 297-299`). Documented as deliberate, but on the `server_turn_complete` path the watchdog's **0.25 s sleep** is what closes the turn, adding 0–250 ms before un-duck and the end chirp.

---

## 4. Already well done

- **The acquire buffer** (`jasper/audio_buffer.py`) — provider-acquire latency never truncates the user's command, and its VAD pass pre-arms the EOU detector for fast talkers (`voice_daemon.py:3877-3884`). This is the right pattern, correctly bounded (250 frames / 20 s / ~80 KB).
- **Pre-roll of 7 × 80 ms** (`voice_daemon.py:422`) so the wake-word tail and the command's first phoneme reach the model.
- **The cue is genuinely fire-and-forget** and overlaps turn open (`voice_daemon.py:4860-4865`), and it is not a `_bg_task` so it cannot end the turn.
- **Pacing is correctly conditioned** so it never touches first audio, and it is pinned against the Rust budget by a test (`audio_io.py:719-720`).
- **No prebuffer, no lookahead, no fade-in on the output side.** The three classic "why is TTS late" bugs are all absent by construction (`tts.rs:582-588, 874-890, 1248-1254`).
- **Barge-in stops audio first and reconciles the provider second**, from the playout ledger rather than arrival time (`turn_playback.py:60-115`, ADR-0115).
- **The expensive loudness IIR is off the loop** (`audio_io.py:1789-1798`) — an explicitly-fixed 0.7 s/s regression.
- **`turn acquire done in …ms (sched_lag= state= loudness_prepare= duck= acquire=)`** (`voice_daemon.py:5019-5028`) is exactly the shape the rest of the loop is missing.
- **`/system/audio/` already sums the live output-chain queues** (`audio_health.py:1758-1779`) — stage E needs no new measurement, only a link into the turn timeline.
- **One `_send_session_audio` and one `_end_session_input`** shared by all three endpointers (`voice_daemon.py:4165-4192`) so failure handling cannot drift between them.
- **Peering costs a solo speaker literally nothing** — the arbitration call returns WIN without yielding (`voice_daemon.py:4171-4172`, ADR-0127), which is what keeps solo cue timing identical to a build with no peering.

**The single highest-value finding:** the owner's belief that the cloud is fast is probably correct, and the number that contradicts it — `first audio chunk from OpenAI in Xms` — is measured from **turn open**, so it silently contains the user's whole utterance plus ~1 s of local endpointing. Add `end_input_ts` (`voice_daemon.py:4178`) and re-anchor that log line on it before tuning anything else; it is a two-line change that will reassign several hundred ms from "provider" to "us."