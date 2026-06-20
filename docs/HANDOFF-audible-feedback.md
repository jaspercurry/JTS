# Audible failure feedback

When the speaker can't fulfill a wake-word request — daily spend cap
hit, voice backend unreachable, or any future wake-blocking failure
mode — it plays a short pre-rendered audio cue instead of falling
silent. Silence in a living room with no admin access is unfixable
from the user's perspective; repetition beats silence.

Cues come in two flavours, distinguished by what triggers them:

- **Reactive cues** fire when a wake event hits a wake-blocking
  state. The user pressed the proverbial doorbell; we're saying
  "I heard you, but I can't do this right now."
- **Proactive cues** fire from background supervisors when
  something's wrong even if the user hasn't tried to use the
  speaker. The supervisor saw a sustained failure (e.g., 5
  consecutive identical reconnect errors) and tells the user
  "the speaker is broken, please check on me." Rate-limited so a
  long outage doesn't spam the room.

This document is the canonical reference for the cue subsystem: what
exists, how to add a new cue (reactive or proactive), where the
cached files live, and why the design is the way it is.

## Generated feedback sounds

Not every audible feedback sound is a pre-rendered spoken WAV. A few
short earcons are generated inline in `jasper.voice_daemon` because
they are sub-100 ms sine blips, not phrases worth caching through
`jasper/cues/`.

- **Mic mute/unmute click**: `WakeLoop._generate_mute_click` builds
  the lower-pitch mute and higher-pitch unmute click. WakeLoop
  pre-renders both PCM buffers at startup, measures their source
  loudness with `measure_pcm_24k_mono`, and sends playback as
  `segment_kind="cue"` with an explicit synthetic source-loudness
  profile. This means outputd level-matches the click like other
  assistant-owned cue audio: current content baseline when music is
  playing, otherwise the listening-level-derived silence target, with
  the same peak cap.
- **Wake start/end chirps**: `WakeLoop._generate_listening_chirp`
  builds the two-note ascending wake chirp and descending turn-end
  chirp. WakeLoop pre-renders both PCM buffers at startup, measures
  their source loudness with `measure_pcm_24k_mono`, and sends
  playback as `segment_kind="chirp"` with an explicit synthetic
  source-loudness profile. Outputd level-matches chirps through the
  same assistant-owned loudness policy as TTS and cue audio. The
  `chirp` segment kind is semantic now — it keeps lifecycle-specific
  ledger/log visibility without bypassing loudness matching.

---

## Architecture at a glance

```
                              Gemini TTS
                              (one-shot,
                               not Live API)
                                    │
                                    ▼
       ┌──────────────────┐    ┌──────────┐    ┌─────────────────┐
       │ jasper/cues/     │    │ /var/lib/│    │ TtsPlayout      │
       │   registry.py    │───▶│  jasper/ │───▶│ (existing audio │
       │   generator.py   │    │  sounds/ │    │  chain — duck-  │
       │   manager.py     │◀───┤  *.wav   │    │  ing, vol, etc.)│
       │   cli.py         │    └──────────┘    └─────────────────┘
       └──────────────────┘         ▲                    ▲
              ▲                     │                    │
              │ play(slug)          │                    │
       ┌──────┴───────────────────────────────────────────┐
       │                jasper.voice_daemon                │
       │  Reactive (wake-driven, via WakeLoop._play_cue):  │
       │  - on wake during spend-cap → cues.play(...)      │
       │  - on wake during reconnect → cues.play(...)      │
       │  - on turn-begin failure   → cues.play(...)       │
       │                                                   │
       │  Proactive supervisor cues (via                   │
       │  WakeLoop.play_supervisor_cue — skips if a turn   │
       │  is in flight to avoid garbling TtsPlayout):      │
       │  - on N identical reconnect failures              │
       │    → connection.set_failure_escalation_cb(...)    │
       │                                                   │
       │  Proactive announcement cues (via WakeLoop's       │
       │  owning path, with path-specific etiquette):       │
       │  - async research job failure → _play_cue(...)     │
       └───────────────────────────────────────────────────┘
```

All cue logic lives in `jasper/cues/`. Adding new cues means
editing one file (`registry.py`) and wiring either
`cues.play("<slug>")` (for reactive paths from inside WakeLoop) or
the relevant background/proactive owner (for example a supervisor
escalation callback or the research announcement path). See "Adding
a new cue" below for both patterns.

---

## What's in the registry today

| slug | trigger | when it plays | template |
|---|---|---|---|
| `spend_cap_reached` | reactive | wake during spend-cap-tripped state | "Hey, I've reached today's spend cap. Visit `{hostname}` to manage." |
| `cant_connect` | reactive | wake while the voice backend is paused (reconnect/backoff), or the connection drops into paused/failed mid-turn-open | "Hey, sorry, I can't connect right now. I'll keep trying." |
| `internal_error` | reactive | turn-open hits an unexpected local/internal error (e.g. a failed state write) while the connection looks healthy — NOT a connectivity problem (the 2026-06-19 incident) | "Sorry, something went wrong on my end. Please try again." |
| `research_failed` | proactive | async research job fails or is interrupted by daemon restart; rate-limited to once per hour | "Sorry, I couldn't finish that research. Please ask me again." |
| `cant_reach_cloud` | proactive | supervisor sees 5 consecutive identical reconnect failures (~30 s on the default backoff schedule); rate-limited to once per hour | "Heads up — I'm having trouble reaching the cloud and I'll keep trying. You might want to check on me at `{hostname}`." |

Cues are **provider-agnostic** — they don't say "Google" or
"Gemini". The voice backend is replaceable; baking provider names
into audio files would mislead users post-switch.

Cues do **not** announce recovery ("you're back online"). The user
hears recovery directly when the next wake gets a normal response.

**Reactive cues have no cooldown across wakes**. If the user wakes
the speaker ten times during a failure, they hear the same cue ten
times. That's intentional — the alternative (mute after first cue,
silence on subsequent wakes) is what we're explicitly trying to
avoid.

**Proactive cues ARE rate-limited** because they fire without a
user-initiated event. Without rate-limiting, a sustained outage
would replay "I can't reach the cloud" every backoff cycle, which is
the spam pattern proactive cues are supposed to eliminate. One per
hour balances "the user gets to know" with "the room isn't yelled
at." Rate state is per-supervisor (in-memory; resets on daemon
restart), so a fresh boot during a sustained outage will fire the
cue once.

---

## Cache lifecycle

Each cue's audio is content-addressed. The file path is:

```
/var/lib/jasper/sounds/<slug>-<8charhash>.wav
```

The hash is `sha256(GENERATOR_VERSION + model + voice + WAV format + rendered_text)[:8]` —
see `cue_hash()` in `jasper/cues/generator.py` for the exact input
ordering. "Rendered text" is the template after `{hostname}`
substitution (from `JASPER_MANAGEMENT_URL`).

**Auto-invalidation**: change anything that affects the hash, the
expected filename changes, the manager looks for the new name,
doesn't find it, regenerates. Stale files are pruned at write time.

Concretely:

| change | regenerates? |
|---|---|
| edit a template in `registry.py` | yes (next startup) |
| change `JASPER_MANAGEMENT_URL` | yes (next startup) |
| change `JASPER_GEMINI_VOICE` | yes (next startup) |
| bump `GENERATOR_VERSION` in `generator.py` | yes (next startup) |
| Gemini's TTS model silently improves | no — run `jasper-cues regenerate --force` |

**Generation triggers**, in order of priority:

1. **Install time** — `deploy/install.sh` runs `jasper-cues regenerate`
   after the daemon is set up. If the install machine has no
   internet, this fails with a warning and the install continues.
2. **Daemon startup** — `jasper-voice` schedules a non-blocking
   background task that calls `AudioCueManager.regenerate()`. Failure
   logs a warning; the daemon comes up regardless.
3. **Manual** — `jasper-cues regenerate` on the Pi. See CLI
   reference below.

A cache miss at play time falls back to ANY existing
`<slug>-*.wav` (stale > silent). If even that's missing, the
manager logs a warning and `play()` returns False — back to the
original silent-failure UX, but visible in `journalctl -u jasper-voice`.

---

## CLI reference

```sh
# Show every registered cue and whether it's cached.
sudo /opt/jasper/.venv/bin/jasper-cues list

# Bake any missing cues (no-op if all cached).
sudo systemctl stop jasper-voice  # avoid concurrent regen
sudo -E /opt/jasper/.venv/bin/jasper-cues regenerate
sudo systemctl start jasper-voice

# Re-render every cue, even cached ones (use after a TTS model
# upgrade or content tweak you want to hear).
sudo -E /opt/jasper/.venv/bin/jasper-cues regenerate --force

# Just one cue.
sudo -E /opt/jasper/.venv/bin/jasper-cues regenerate --cue spend_cap_reached

# Play a cue locally to preview phrasing.
sudo -E /opt/jasper/.venv/bin/jasper-cues play spend_cap_reached
```

The `-E` to sudo preserves the env vars the CLI needs
(`JASPER_MANAGEMENT_URL`, `JASPER_GEMINI_VOICE`, etc.). Or source
`/etc/jasper/jasper.env` first.

Exit codes (stable so install.sh can read them):
- `0` — ok
- `1` — `list` found missing files
- `2` — bad arg / unknown slug
- `3` — no TTS backend available (missing API key)
- `4` — unexpected failure

---

## Adding a new cue

1. **Append a `CueDef` to `jasper/cues/registry.py`**:

   ```python
   CueDef(
       slug="mic_dropped",
       template=(
           "Hey, sorry, the microphone went away. "
           "Try unplugging and reconnecting it."
       ),
       description=(
           "Played when MicCapture's read loop sees the USB device "
           "disappear and can't reopen it."
       ),
   ),
   ```

   - Keep messages **provider-agnostic** (don't mention Google /
     Gemini / OpenAI / etc).
   - Keep them **short** (under 12 seconds at normal speech rate).
   - Use `{hostname}` if you want to point at the management
     dashboard. Don't manually type "jts.local" — installs may run
     on a different hostname.

2. **Wire the failure path** to play the cue. The right wiring
   depends on whether the cue is reactive or proactive:

   - **Reactive** (fires from inside a wake handler): call
     `await self._play_cue("<slug>")` directly from `WakeLoop`.
     `_play_cue` ducks music, plays the WAV, restores, and
     swallows exceptions. The wake/turn-begin handlers in
     `voice_daemon.py` show the pattern.
   - **Proactive** (fires from a background supervisor with no
     active wake): expose a `set_*_cb(callback)` method on the
     subsystem and have it call back into
     `WakeLoop.play_supervisor_cue("<slug>")`. That public method
     does the same duck-play-restore as `_play_cue` but **skips
     when a user-driven turn is in flight** so the supervisor
     can't garble an in-progress TTS reply by trying to layer a
     second WAV onto the single PortAudio stream. The
     `GeminiLiveConnection.set_failure_escalation_cb` →
     `WakeLoop.play_supervisor_cue` wiring in `voice_daemon.run()`
     is the canonical example. Don't forget to rate-limit at the
     supervisor — `play_supervisor_cue` itself doesn't.

3. **Bake the audio**. Either restart `jasper-voice` (its startup
   regen catches the new cue) or run `jasper-cues regenerate`
   manually.

4. **(Optional)** Add a test in `tests/test_cues_*.py` that
   exercises the failure path → `play()` call. The
   no-provider-name rule is enforced by `test_cues_are_provider_agnostic`
   automatically.

---

## Why this design

**Why one TTS provider for cues + Live, not separate?** Same voice
across everything Jarvis says. If we used (say) Google Cloud TTS
for cues and Gemini Live for conversations, the voice would
audibly switch mid-interaction.

**Why cache at all? Why not stream TTS at play time?** Two reasons.
First, the most important cue is "we can't connect to the voice
backend" — and at play time, the voice backend is exactly what's
unreachable. Second, the latency hit (1-3 seconds for one-shot
TTS) would feel broken when the cue is supposed to be a quick
"hey, I can't help right now" reply.

**Why content-addressable hashes instead of mtime tracking?** Mtime
gets the cache invalidation question wrong all the time
(timezone changes, filesystem clock drift, manual file copies).
Content addressing is unambiguous: the filename IS the contract.

**Why prune stale files at write time, not lazily?** Disk on a Pi
isn't huge. Accumulating one stale file per template/hostname/voice
permutation forever isn't catastrophic, but the `<slug>-*` listing
gets ugly fast. Pruning is cheap and keeps the directory readable.

**Why is regeneration sync (not async)?** The underlying TTS HTTP
call is blocking. Async-wrapping it is `asyncio.to_thread(...)` at
call time, which the daemon's startup hook does. The CLI runs
sync directly. Simpler than introducing an async client.

**Why doesn't the daemon REQUIRE cues to start?** A working
speaker without cues is still better than a dead speaker with
cues. If TTS regen fails (no network at boot, bad API key, quota),
the daemon comes up anyway and degrades gracefully — silent
failures on the affected paths, but every other path works.

---

Last verified: 2026-06-20
