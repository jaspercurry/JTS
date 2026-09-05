# Low-latency voice front-end best practices — research briefing (2026-09-05)

Sourcing note: the egress proxy blocked direct fetches to livekit.com, docs.pipecat.ai, ai.google.dev, developers.openai.com, home-assistant.io, esphome.io, huggingface.co and xmos.com. Items marked [snippet] come from search-result excerpts of the primary page, not a direct fetch.

## 1. Latency budgets
- 100 ms / 1 s / 10 s human-factors thresholds (Nielsen, NN/g): <100 ms reads as instantaneous; ~1 s breaks flow. https://www.nngroup.com/articles/response-times-3-important-limits/ — wake→cue should clear ~100–150 ms; end-of-speech→first audio well under 1 s.
- LiveKit's decomposition: STT 100–200 ms, LLM 300–500 ms, TTS 100–200 ms, network 50–150 ms → 700 ms–1.2 s end-to-end target. [snippet] https://livekit.com/blog/understand-and-improve-agent-latency — for a single speech-to-speech hop the comparable budget is EOU delay + RTT + provider TTFB.
- HA Voice PE community: ~400 ms wake-to-action for local intents; server-side openWakeWord adds ~80–120 ms. [snippet, forum] https://community.home-assistant.io/t/voice-command-latency/889423

## 2. Wake word
- openWakeWord requires a fixed 1280-sample (80 ms) window at 16 kHz baked into the ONNX graph; smaller chunks do not reduce detection latency; inference <5 ms/frame on a Pi 4. https://github.com/dscripka/openWakeWord — the 80 ms framing is the floor.
- microWakeWord (30 ms window / 20 ms stride) targets ESP32-class MCUs; no latency case on a Pi 5. https://github.com/kahrendt/esphome-on-device-wake-word
- No primary source found for a stated pre-roll duration or an ack-cue-before-cloud pattern. Unverified.

## 3. VAD / endpointing
- Silero VAD native chunk at 16 kHz is 512 samples (32 ms); library defaults threshold 0.5, min_speech 250 ms, min_silence 100 ms, speech_pad 30 ms. https://github.com/snakers4/silero-vad
- LiveKit's Silero plugin: min_silence_duration ≈ 0.55 s, prefix_padding ≈ 0.5 s, activation_threshold 0.5. https://github.com/livekit/agents (livekit-plugins-silero/.../vad.py) — 800 ms trailing silence is on the cautious side.
- Pipecat Smart Turn v3 runs after a Silero pause, classifies up to 8 s of audio; stop_secs=3 hard fallback; ~12 ms CPU on modern CPUs, ~60 ms on a small cloud instance. [snippet] https://www.daily.co/blog/announcing-smart-turn-v3-with-cpu-inference-in-just-12ms/ https://huggingface.co/pipecat-ai/smart-turn-v3 — no Pi 5 benchmark found.
- LiveKit turn-detector (Qwen2.5-0.5B INT8, <500 MB RAM) claims 50–160 ms but community reports 700 ms–1 s. [snippet] https://huggingface.co/livekit/turn-detector — too heavy for a 1 GB Pi.
- OpenAI Realtime server_vad defaults: threshold 0.5, prefix_padding 300 ms, silence_duration 500 ms; semantic_vad eagerness fallback low=8 s, medium=4 s, high=2 s. [snippet] https://developers.openai.com/api/docs/guides/realtime-vad
- Gemini Live automatic activity detection defaults silence_duration_ms=100, prefix_padding_ms=20; reported ignored on gemini-3.1-flash-live-preview. [snippet] https://ai.google.dev/gemini-api/docs/live-api/capabilities https://github.com/googleapis/python-genai/issues/2580

## 4. Streaming audio to the provider
- OpenAI input_audio_buffer.commit requires ≥100 ms buffered audio. https://community.openai.com/t/error-in-committing-input-audio-buffer-because-buffer-has-0ms-of-audio/1032360
- No verified guidance that smaller chunks improve TTFT above that floor. Unverified.

## 5. Playback path
- Lookahead limiters use 1–10 ms lookahead. https://signalsmith-audio.co.uk/writing/2022/limiter/
- Jitter buffers commonly 30–300 ms (generic VoIP; low confidence).
- ALSA USB audio: ~128-frame periods × 3 is the usual start; below ~64 risks xruns on USB DACs. [snippet] https://wiki.linuxaudio.org/wiki/raspberrypi

## 6. Barge-in
- No primary-sourced numeric interrupt window for HA Voice PE / LiveKit / Pipecat. Unverified.
- XVF3800 AEC reference must be the actual played-back signal and causal (reference reaches the chip before the acoustic echo). [snippet] https://www.xmos.com/documentation/XM-014888-PC/html/modules/fwk_xvf/doc/programming_guide/02_theory_of_operation.html

## 7. Architecture patterns
- Pipecat: linear FrameProcessor chain, VAD/turn analyzer before STT gates forwarded audio. [snippet] https://reference-server.pipecat.ai/en/stable/api/pipecat.pipeline.pipeline.html
- LiveKit Agents per-turn metrics: VAD (idle_time, inference_duration), EOU (end_of_utterance_delay, transcription_delay), LLM (ttft), TTS (ttfb); e2e_latency ≈ eou_delay + llm_ttft + tts_ttfb. https://github.com/livekit/agents/blob/main/examples/homepage/knowledge_base/products/agent-observability.md
- HA Assist pipeline: wake_word → stt → intent → tts as independently addressable Wyoming servers. [snippet] https://developers.home-assistant.io/docs/voice/pipelines/
