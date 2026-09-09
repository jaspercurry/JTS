# Voice evaluation

These scenarios open **paid** voice sessions and can call live tools. State a
fixed scenario count, estimated cost and tool side effects before running.
Never loop or auto-retry. Provider prices come from the production model rate
cards in `jasper/data/model_pricing.json`, with configured overrides. Grok uses
adapter-marked billable activity duration. Unknown models remain unpriced.
Prompt synthesis also costs money when its audio is not cached.

Run an explicitly selected scenario from the checkout with the configured
provider credentials and tool settings. `JASPER_VOICE_EVAL_SKIP_PLAYBACK=1`
skips scenarios that change playback. Home Assistant scenarios can perform
real actions. The `harness` fixture reuses one connection on the session event
loop; tools and provider conversation history can retain state between trials.

## What the harness proves

All providers receive the same paced 16 kHz input through `send_audio()` and
`end_input()`. Responses go through production `play_responses()` and an
immediate virtual output sink. Its acknowledgements are simulated; neither
its WAV nor its flush ledger proves speaker output or what a person heard.
Physical playback, AEC and recognition need separate hardware evidence.

Each successful `ask()` requires response audio. Native spoken text comes
from `LiveTurn.capture()`. A scenario that needs text calls
`result.require_spoken_text()`; missing native text produces **XFAIL**, which
means unavailable evidence, not a pass. Unparseable required answers fail.
Tool results are recorded at the executor boundary. Token counts come from
`LiveTurn.usage()`, including the provider's modality breakdown where present.
Unfinished token usage stays visible, but its cost is `null` with
`cost_status=incomplete` until the adapter reports server completion. Such
costs cannot count as zero in a complete run total. Grok's cost uses measured
billable activity through release, even for interrupted or timed-out turns.

Each turn writes gitignored evidence:

- `transcripts_out/*.md`: prompt, tool results and native transcript.
- `transcripts_out/*.response.wav`: PCM accepted by the simulated output sink.
- `traces_out/*.jsonl`: supporting events, including the final capture and usage.

Read these artifacts before considering another paid run. Trace deltas do not
own transcript, tool result or cost truth.

## Offline replay

Run `python -m pytest tests/test_voice_replay.py` for hardware-free composition
checks. Install the production `openwakeword==0.6.0` package with `--no-deps`
and its locked `openwakeword-onnx` dependency group for input replay. Missing
openWakeWord explicitly skips input cases; playback cases can still run.
It is outside this paid fixture tree and supplies fake transports to
the real Gemini, OpenAI and Grok adapters. It uses the real input, endpoint and
playback code, including the real openWakeWord chunk mean and `SpeechVAD`
conversion with scripted ONNX outputs. Score tapes prove state transitions, not acoustic recognition;
there are no tracked speech WAVs in this lane. Input uses the current 80 ms
delivery size. Scripted model outputs cannot validate neural inference at a
new frame size or release a hardware timing change.

Existing detailed checks remain in their owning test files:

| Behavior | Check |
|---|---|
| Quiet command, short pause, final silence, manual release, no-speech abort; immediate/delayed input through adapters and playback | `test_voice_replay.py::test_input_endpoint_adapter_and_output_replay` |
| Packet wait/write/drain interruption followed by a fresh response | `test_voice_replay.py::test_interrupt_then_fresh_turn_replay` |
| Concurrent capture during acquire, overflow and gap resets, mute during prefix upload | `test_audio_buffer_drain.py` |
| Capture overload and expired frames | `test_mic_capture.py` |
| Mute clears rolling capture history | `test_voice_daemon_mute_privacy.py` |
| Button hold cap and delayed buffered button input | `test_voice_daemon_push_to_talk_endpointer.py` |
| Flush confirmation and per-item output boundaries | `test_turn_playback_barge_in.py` |
| Late old response/input events and tool results cannot enter a new turn | `test_openai_session.py`, `test_gemini_connection.py` |

Measurement input fencing is owned by the capture tests; output admission
checks alone do not prove that a brief pause clears old input.

## Adding a scenario

Use the `harness` fixture, existing result accessors and an independent oracle
where possible. Register tools through production `ToolDeps` and
`register_packs`, as `_build_test_registry()` does. Add offline fixture checks
outside `tests/voice_eval/` so normal CI never opens paid sessions. A paid run
requires its own announced count and cost; it is not an automatic edit check.
