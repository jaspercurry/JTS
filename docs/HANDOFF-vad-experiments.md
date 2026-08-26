# Handoff: VAD, endpointing, and the raw-stream question

> **Status: operational.** What decides end-of-utterance, why it is not the
> provider's server VAD, and the two experiment knobs left wired for the
> unresolved raw-stream question. The decision and its A/B evidence are
> [ADR-0152](adr/0152-local-silero-on-the-aec-stream-is-the-endpointer-server-vad-stays-off.md);
> the full 2026-05 session record — five-cell matrix, failure modes, cell
> recipes, analysis scripts, and the open raw-AGC hypothesis — is
> [historical/vad-experiments-2026-05.md](historical/vad-experiments-2026-05.md).

## What endpoints a turn

The AEC stream is the primary mic; the raw chip stream is a secondary wake leg,
OR-gated for wake detection only ([HANDOFF-aec.md](HANDOFF-aec.md) owns the
bridge). End-of-utterance is decided locally by Silero in
`jasper/voice_daemon.py`, never by the provider.

Three constants govern arming, and each carries its own recorded evidence
beside it — read them there before changing one:

- `END_OF_UTTERANCE_SPEECH_THRESHOLD` — the speech-probability floor.
- `SUSTAINED_SPEECH_TO_ARM_SEC` — how long that floor must hold *continuously*
  before trailing silence starts counting.
- the arming peak gate — the minimum peak the arming speech-run must reach, so
  wake-word tail and room reverb cannot arm the detector.

Only after the detector arms does silence count toward end-of-utterance. If it
never arms, `NO_SPEECH_ABORT_SEC` ends the turn cleanly and un-ducks.

`JASPER_SERVER_VAD_ENABLED` defaults to `0` (OpenAI/Grok only when set;
`create_response` and `interrupt_response` stay false regardless). Turning it on
is an experiment, not a supported configuration — ADR-0152 has the numbers.

## Known product bug: tool calls on empty STT

**The model hallucinates tool calls with confident arguments when STT returns
nothing.** Observed across four cells of the 2026-05 matrix. Invented calls
included `set_volume(percent=60)`, `get_subway_arrivals(line='', direction='')`,
and — the one that mattered — `home_assistant(query='turn on the bedroom
lights')`, which actually executed and turned the lights on while the user was
asking about the weather.

The shipped mitigation is prompt-level, in the Unclear Audio section of
`SYSTEM_INSTRUCTION` (`jasper/voice/prompt.py`): it enumerates the fragment
triggers ("a short fragment like 'What?' or 'That's'") and names the
empty-string-arguments anti-pattern explicitly, per the "enumerate triggers;
conditional rules over absolutes" convention in
[HANDOFF-prompting.md](HANDOFF-prompting.md).
`tests/test_system_prompt_unclear_audio.py` pins the observed fragments.

**This is a prompt-level guard, not deterministic enforcement.** The model can
still hallucinate. If it proves insufficient, the next-level fix is a dispatch
-layer refusal: do not execute a side-effecting tool when the most recent
transcript is empty *and* the arguments are empty or default.

## The raw-stream question (open)

The premise that a well-normalized raw stream plus strong ducking could make AEC
unnecessary was **not** disproven — the one cell that tested it failed on the
AGC, not on the premise.

`JASPER_AEC_RAW_AGC_ENABLED` (default `0`) gates `_SimpleAGC` in
`jasper/cli/aec_bridge.py`: a homegrown peak-tracking AGC on the raw chip stream,
mirroring WebRTC AGC1's parameter shape but not a WebRTC instance. **It
hard-clips at full scale on transients once gain has ramped up** — a WAV whose
peak reads 0.0 dB is clipped and unreliable for STT. It stays gated off for that
reason.

The archived hypothesis proposes fixing the clipping first (a soft limit plus
peak-aware gain reduction) as the cheap decisive test, and only then building a
real second `webrtc::AudioProcessing` instance for the raw path. Either way the
result is a new cell run against cell 0, not a default change.

## Instrumentation

`JASPER_DEBUG_RECORD_OPENAI_AUDIO=1` tees every byte sent to
`input_audio_buffer.append` into a per-turn WAV under
`/tmp/jasper-openai-debug/` (`jasper/voice/openai_session.py`) — the ground
truth of what the provider received, post-upsample to 24 kHz mono int16. The
unit's `PrivateTmp=yes` hides the directory outside the service namespace; the
retrieval and level-analysis recipes are in the archived record. Turn it on for
any endpointer experiment: without it you have telemetry but no ground truth.

Shadow Silero runs unconditionally on the primary stream even in server-VAD
mode, populating `wake_events.max_silero_aec` so the two endpointers can be
compared on identical audio. No env var, telemetry only.

Last verified: 2026-08-26 (`JASPER_SERVER_VAD_ENABLED` default `False` and the
threshold/silence defaults rechecked against `jasper/config.py`; the endpointer
constants against `jasper/voice_daemon.py` — the archived record's "sustained
240 ms" is stale, the shipped value is `SUSTAINED_SPEECH_TO_ARM_SEC = 0.20` plus
a peak gate; the Unclear Audio guard against `jasper/voice/prompt.py` and its
test; `_SimpleAGC` and both experiment knobs confirmed present and default-off
in `jasper/cli/aec_bridge.py` and `jasper/voice/openai_session.py`).
