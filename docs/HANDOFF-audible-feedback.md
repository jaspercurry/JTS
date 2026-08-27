# Audible failure feedback

> **Status: operational.** Canonical reference for the cue subsystem: what
> exists, how to add a cue, where the cached files live. This doc sits under
> **non-negotiable #6 — no silent deafness**: a new code path that prevents
> wake response must play a cue (`jasper/cues/registry.py`). Decisions:
> [ADR-0153](adr/0153-failure-cues-are-pre-rendered-and-content-addressed-never-streamed.md)
> (pre-rendered, content-addressed, never streamed),
> [ADR-0154](adr/0154-reactive-cues-never-cool-down-proactive-cues-are-rate-limited.md)
> (the two cooldown policies).

When the speaker can't fulfill a wake-word request — daily spend cap hit, voice
backend unreachable, or any future wake-blocking failure mode — it plays a short
pre-rendered audio cue instead of falling silent. Silence in a living room with
no admin access is unfixable from the user's perspective; repetition beats
silence.

Cues come in two flavours, distinguished by what triggers them:

- **Reactive cues** fire when a wake event hits a wake-blocking state. The user
  pressed the proverbial doorbell; we're saying "I heard you, but I can't do
  this right now."
- **Proactive cues** fire from background supervisors when something's wrong
  even if the user hasn't tried to use the speaker — a sustained failure the
  supervisor saw, telling the user "the speaker is broken, please check on me."

All cue logic lives in `jasper/cues/`: `registry.py` (the `CueDef` list),
`generator.py` (render + hash), `manager.py` (cache + `play`), `cli.py`, and
`factory.py` (picks the TTS backend to match the box's voice provider). Baked
WAVs land in `/var/lib/jasper/sounds/` and play through the ordinary
`TtsPlayout` chain — ducking, volume, and the rest.

## Generated feedback sounds

Not every audible feedback sound is a pre-rendered spoken WAV. A few
short earcons are rendered by `jasper/voice/earcons.py` and cached by
`jasper.voice_daemon` at startup, because they are short tone recipes,
not phrases worth caching through `jasper/cues/`.

They are rendered in float and baked ONCE, at the sample width the box's
wire declares (U2 PR-2): 24 kHz mono S16 on a narrow box, 24 kHz mono S32
at the i32 spine scale on a wide one, so the recipe's own detail is not
flattened onto the 16-bit grid before the wire could carry it. Which one a
box takes is `jasper.fanin_coupling.assistant_wire_is_wide`'s answer, off the
ring wire's format; read the rule there rather than a copy here. The wire
defaults wide, so a box bakes the wide earcon with no declaration at all. A box
an operator has pinned back with `JASPER_FANIN_RING_WIRE_FORMAT=S16_LE` bakes
narrow.
`measure_pcm_24k_mono` takes the same width and normalizes it out, so an
earcon's source-loudness profile is identical either way. Spoken cue
WAVs are NOT affected: their source is a 16-bit provider TTS render on
disk, so a wider bake would add container and no signal.

- **Mic mute/unmute click**: `jasper.voice.earcons._generate_mute_click` builds
  the lower-pitch mute and higher-pitch unmute click. WakeLoop
  pre-renders both PCM buffers at startup, measures their source
  loudness with `measure_pcm_24k_mono`, and sends playback as
  `segment_kind="cue"` with an explicit synthetic source-loudness
  profile. This means outputd level-matches the click like other
  assistant-owned cue audio: current content baseline when music is
  playing, otherwise the listening-level-derived silence target, with
  the same peak cap.
- **Wake start/end chirps**: `jasper.voice.earcons._generate_listening_chirp`
  builds the two-note ascending wake chirp and descending turn-end
  chirp. WakeLoop pre-renders both PCM buffers at startup, measures
  their source loudness with `measure_pcm_24k_mono`, and sends
  playback as `segment_kind="chirp"` with an explicit synthetic
  source-loudness profile. Outputd level-matches chirps through the
  same assistant-owned loudness policy as TTS and cue audio. The
  `chirp` segment kind is semantic now — it keeps lifecycle-specific
  ledger/log visibility without bypassing loudness matching.

Spoken cue WAVs and dynamic cue text follow the same contract. The cue
manager reads the 24 kHz mono WAV, measures that exact PCM with
`measure_pcm_24k_mono`, and passes a `source_profile` with
`segment_kind="cue"` so fan-in/outputd do not have to borrow the active
live-assistant profile. WakeLoop also prepares feedback loudness context
before standalone cue/click playback; that lets the mix owner snapshot
pre-duck content loudness when music is playing, or use the current
listening-level-derived silence target when the room is quiet.

## Duck and output ownership through the physical tail

Writing a cue or dynamic announcement is not the end of playback: accepted PCM
may still be queued in fan-in/outputd or the device buffer. WakeLoop keeps both
the duck and the exact `AssistantOutputGate` episode until
`wait_tts_drained_owned` reaches that physical drain boundary — for
reactive/admin cues in `WakeLoop._play_cue_owned` and proactive dynamic text in
`WakeLoop._play_dynamic_text`.

The ownership rule is identical across the two shipped TTS routes:

- solo and active speakers retain the pre-DSP `FanInDucker` through the tail;
- passive bonded non-sub speakers retain the local-outputd `CueDuck` snapshot
  through the tail.

Repeated task cancellation is deferred while that one drain owner finishes.
Only then is the duck restored, the output episode released, and cancellation
reported to the caller. This prevents already-accepted speech from becoming
audible after music has been restored or after a room-correction
`MEASURE_PAUSE` reply has treated assistant output as idle.

## What's in the registry today

| slug | trigger | when it plays | template |
|---|---|---|---|
| `spend_cap_reached` | reactive | wake during spend-cap-tripped state | "Hey, I've reached today's spend cap. Visit `{hostname}` to manage." |
| `cant_connect` | reactive | wake while the voice backend is paused (reconnect/backoff), or the connection drops into paused/failed mid-turn-open | "Hey, sorry, I can't connect right now. I'll keep trying." |
| `internal_error` | reactive | turn-open hits an unexpected local/internal error (e.g. a failed state write) while the connection looks healthy — NOT a connectivity problem | "Sorry, something went wrong on my end. Please try again." |
| `no_room_microphone` | reactive | a source-less session start on a speaker with no always-listening microphone — its only voice input is a paired push-to-talk remote (`NO_ROOM_MIC_CUE_SLUG`). Without it that request ducked the music, chirped, forwarded zero bytes, and died to the idle watchdog in total silence | "I don't have a microphone of my own. Hold the button on your remote to talk to me." |
| `cant_reach_cloud` | proactive | supervisor sees 5 consecutive identical reconnect failures (~30 s on the default backoff schedule) | "Heads up — I'm having trouble reaching the cloud and I'll keep trying. You might want to check on me at `{hostname}`." |
| `research_failed` | proactive | async research job fails or is interrupted by daemon restart | "Sorry, I couldn't finish that research. Please ask me again." |
| `measurement_relay_unreachable` | proactive | phone-mic capture relay: Pi cannot reach the cloud relay to run a new measurement (`RELAY_UNREACHABLE_CUE_SLUG`) | "I couldn't reach the measurement service. New measurements need internet, but anything already set up still works." |
| `measurement_failed` | proactive | phone-mic capture relay: a started measurement can't be used — phone timeout, decrypt/integrity failure, stimulus alignment failure, or phone aborted (`MEASUREMENT_FAILED_CUE_SLUG`) | "Sorry, that measurement didn't work. Visit `{hostname}` to try again." |

Cues are **provider-agnostic** — they don't say "Google" or "Gemini". The voice
backend is replaceable; baking provider names into audio files would mislead
users post-switch. `tests/test_cue_registry_coverage.py` enforces this.

Cues do **not** announce recovery ("you're back online"). The user hears
recovery directly when the next wake gets a normal response.

**Reactive cues have no cooldown across wakes; proactive cues are rate-limited
to once per hour, per supervisor** — see
[ADR-0154](adr/0154-reactive-cues-never-cool-down-proactive-cues-are-rate-limited.md).

## Cache lifecycle

Each cue's audio is content-addressed at
`/var/lib/jasper/sounds/<slug>-<8charhash>.wav`. The hash covers
`GENERATOR_VERSION`, the backend's actual synthesis model, the voice, the WAV
format, and the rendered text (the template after `{hostname}` substitution from
`JASPER_MANAGEMENT_URL`) — `cue_hash()` in `jasper/cues/generator.py` owns the
exact input ordering.

**Auto-invalidation**: change anything that affects the hash and the expected
filename changes; the manager looks for the new name, doesn't find it, and
regenerates. Stale files are pruned at write time.

| change | regenerates? |
|---|---|
| edit a template in `registry.py` | yes (next startup) |
| change `JASPER_MANAGEMENT_URL` | yes (next startup) |
| change the voice provider (`JASPER_VOICE_PROVIDER`) or its configured voice | yes (next startup) |
| bump `GENERATOR_VERSION` in `generator.py` | yes (next startup) |
| the provider's TTS model silently improves | no — run `jasper-cues regenerate --force` |

**Generation triggers**, in order of priority:

1. **Install time** — `deploy/install.sh` runs `jasper-cues regenerate` after
   the daemon is set up. No internet on the install machine warns and continues.
2. **Daemon startup** — `jasper-voice` schedules a non-blocking background task
   calling `AudioCueManager.regenerate()`. Failure logs a warning; the daemon
   comes up regardless.
3. **Manual** — `jasper-cues regenerate` on the Pi.

A cache miss at play time falls back to ANY existing `<slug>-*.wav` (stale >
silent). If even that's missing, the manager logs a warning and `play()` returns
False — back to the original silent-failure UX, but visible in
`journalctl -u jasper-voice`.

## CLI reference

```sh
# Show every registered cue and whether it's cached.
sudo /opt/jasper/.venv/bin/jasper-cues list

# Bake any missing cues (no-op if all cached).
sudo systemctl stop jasper-voice  # avoid concurrent regen
sudo -E /opt/jasper/.venv/bin/jasper-cues regenerate
sudo systemctl start jasper-voice

# Re-render every cue, even cached ones (after a TTS model upgrade or a
# content tweak you want to hear). Add --cue <slug> for just one.
sudo -E /opt/jasper/.venv/bin/jasper-cues regenerate --force

# Play a cue through jasper-control's /cue/play endpoint to preview phrasing
# (routes through the running daemon, not a local TtsPlayout).
sudo -E /opt/jasper/.venv/bin/jasper-cues play spend_cap_reached
```

`sudo -E` preserves the env vars the CLI needs (`JASPER_MANAGEMENT_URL`, the
provider settings); or source `/etc/jasper/jasper.env` first.

Exit codes are stable so `install.sh` can read them: `0` ok, `1` `list` found
missing files, `2` bad arg / unknown slug, `3` no TTS backend available (missing
API key), `4` unexpected failure.

## Adding a new cue

1. **Append a `CueDef` to `jasper/cues/registry.py`** with `slug`, `template`,
   and a `description` naming the failure path it covers. Keep messages
   provider-agnostic (no Google / Gemini / OpenAI), short (under 12 seconds at
   normal speech rate), and use `{hostname}` rather than typing a hostname —
   installs may run on a different one.

2. **Wire the failure path**, and the wiring depends on the flavour:

   - **Reactive** (fires from inside a wake handler): `await
     self._play_cue("<slug>")` directly from `WakeLoop`. It ducks music, plays
     the WAV, restores, and swallows exceptions. The wake/turn-begin handlers in
     `jasper/voice_daemon.py` show the pattern.
   - **Proactive** (fires from a background supervisor with no active wake):
     expose a `set_*_cb(callback)` on the subsystem and have it call
     `WakeLoop.play_supervisor_cue("<slug>")`. That public method does the same
     duck-play-restore but **skips when any assistant output episode is active**,
     so a supervisor can't garble an in-progress reply by layering a second WAV
     onto the single TTS stream. The `set_failure_escalation_cb` →
     `play_supervisor_cue` wiring in `jasper/voice/daemon_main.py`'s `run()` is
     the canonical example. **Rate-limit at the supervisor** —
     `play_supervisor_cue` does not.

3. **Bake the audio**: restart `jasper-voice` (startup regen catches the new
   cue) or run `jasper-cues regenerate`.

4. **Optionally** add a test in `tests/test_cues_*.py` exercising the failure
   path through to the `play()` call. `tests/test_cue_registry_coverage.py`
   already enforces the no-provider-name rule and registry coverage.

Last verified: 2026-08-26 (all eight registry slugs and their reactive/proactive
split rechecked against `jasper/cues/registry.py`; the hash inputs and
`GENERATOR_VERSION` against `jasper/cues/generator.py`; the provider-aware
backend selection against `jasper/cues/factory.py` — cue TTS is no longer
Gemini-only, so the cache table's voice row now names the provider setting; CLI
exit codes against `jasper/cues/cli.py`; the supervisor wiring against
`jasper/voice/daemon_main.py`). Prior 2026-08-15 (the earcon bake-width
paragraph, left verbatim above, against `jasper.fanin_coupling.assistant_wire_is_wide`).
Prior 2026-08-08 (accepted-PCM duck/output ownership for both `FanInDucker` and
local-outputd `CueDuck`).
