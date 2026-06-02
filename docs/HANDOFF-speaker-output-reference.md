# Handoff: speaker output reference architecture

This doc is the canonical design direction for the JTS output,
reference, TTS, and barge-in architecture.

It answers one question:

> What signal does JTS treat as "what the speaker actually emitted,"
> and how do playback, AEC, wake/corpus telemetry, and realtime-model
> turn-taking consume that signal?

Read this before changing TTS routing, AEC reference routing,
`jasper-outputd`, Camilla output wiring, `pcm.jasper_out` rollback
behavior, or any future output-owner daemon. For source-specific lane
work, start with
[audio-paths.md](audio-paths.md#adding-a-new-music-source). For AEC
engine behavior and tuning, use [HANDOFF-aec.md](HANDOFF-aec.md).
For historical barge-in costing, use [HANDOFF-barge-in.md](HANDOFF-barge-in.md).

## Current Operational Truth

On current main, JTS has one final output owner:

```text
MUSIC / CONTENT
  AirPlay / Spotify / Bluetooth / USB / correction
    -> private snd-aloop lanes
    -> jasper-fanin
    -> pcm.jasper_capture
    -> jasper-camilla
    -> outputd_content_playback
    -> outputd_content_capture
    -> jasper-outputd
    -> dongle / amp / speaker

ASSISTANT AUDIO
  TTS / cues / chirps
    -> OutputdTtsPlayout
    -> /run/jasper-outputd/tts.sock
    -> jasper-outputd
    -> dongle / amp / speaker
```

The two audible paths now converge inside `jasper-outputd`, which is
the only normal writer to the physical DAC. The legacy `pcm.jasper_out`
dmix remains defined in `/etc/asound.conf` for emergency rollback and
older checkouts, but current production audio does not use it as the
convergence point.

The AEC bridge currently reads `pcm.jasper_ref`, a `plug` wrapper over
`pcm.jasper_capture`, which is a `dsnoop` on fan-in's summed music
substream. Therefore the AEC reference is:

- content/music only
- pre-CamillaDSP
- pre-ducking
- pre-TTS/cues/chirps
- a peer `dsnoop` reader beside CamillaDSP

That remains the practical compromise for music echo reduction. It is
not yet the true speaker-output reference, even though outputd now owns
the physical output loop.

**Corpus-only exception (2026-05-29).** The wake-corpus recorder's
chip-AEC comparison profile can temporarily ask outputd to publish the
exact final speaker buffer to two side outputs: the XVF3800 USB-IN
reference PCM (`JASPER_OUTPUTD_CHIP_REF_PCM`) and a localhost UDP tap
consumed by `jasper-aec-bridge`
(`JASPER_OUTPUTD_REFERENCE_UDP_TARGET`). This is deliberately not the
production AEC reference path yet; it is a recorder-owned test mode that
is enabled by `/var/lib/jasper/wake_corpus_bridge.env` and removed when
the operator exits corpus test mode. The UDP tap stays at outputd's
48 kHz graph rate for software AEC/corpus analysis; the chip-reference
PCM is downsampled to the XVF3800 USB-IN contract (`16 kHz`, default
`320`-frame periods, `1280`-frame buffer).

There is intentionally no production fan-in reference side feed. A
short-lived 2026-05-27 spike explored a Unix-datagram content mirror
from `jasper-fanin` to AEC/corpus consumers. That spike is not the
chosen direction. It solved only shared-`dsnoop` pressure for
content/music and did not solve robust barge-in during assistant
speech. The right destination is one output owner that publishes the
actual post-mix speaker reference.

TTS is already a core realtime-voice component:

- `OutputdTtsPlayout` preserves the `TtsPlayout` contract: it resamples
  provider audio to 48 kHz stereo, sends un-gained PCM plus
  provider/model/voice profile metadata to outputd, tracks expected
  drain, and supports `flush()` for interruption.
- `jasper-outputd` owns assistant gain. It measures content loudness
  before ducking, applies provider source-loudness profiles, and emits
  `event=outputd.assistant_loudness` plus STATUS telemetry for the
  latest decision. Python seeds and learns profiles but does not set
  final gain.
- Cues route through the same `TtsPlayout` object so they inherit
  outputd routing, drain behavior, and profile/peak-capped gain policy
  without training live assistant profiles. If a cue arrives with no
  wake-turn context and no measured content baseline, outputd uses the
  configured default silence target rather than a fixed legacy gain.

That is good groundwork, but it is not a complete "what did the user
hear?" ledger. Robust barge-in needs both a true AEC reference and
precise playout accounting.

## Codebase Validation

Rechecked against the current tree on 2026-06-01:

- `jasper/audio_io.py` + `TtsPlayout` is the migration boundary for
  assistant audio. The voice daemon expects a small operation set to keep
  its semantics: `write_segment()`/`write()`, `end_segment()`,
  `flush()`, `expected_drain_at()`, and `wait_drained()`.
- `jasper/voice_daemon.py` + `_play_responses` races each TTS write
  against provider interruption and calls `flush()` on interruption.
  `_idle_watchdog` and `_end_turn` rely on `expected_drain_at()` to
  avoid ending a turn before queued audio drains.
- `deploy/alsa/asoundrc.jasper` defines the outputd ALSA surfaces:
  `outputd_content_playback`, `outputd_content_capture`, and
  `outputd_dac`.
- `deploy/camilladsp/outputd-cutover.yml` sends Camilla playback to
  `outputd_content_playback`; `deploy/camilladsp/v1.yml` is retained
  as the pre-outputd rollback config that writes to `jasper_out`.
- `jasper/cli/aec_bridge.py` opens `jasper_ref`, downsamples the 48 kHz
  stereo content reference to 16 kHz mono, and tracks reference
  starvation/queue drops. In opt-in chip-AEC mode and the wake-corpus
  chip-AEC comparison profile it receives outputd's final-buffer UDP
  tap via `JASPER_AEC_REF_SOURCE=outputd_udp` while outputd also feeds
  the XVF USB-IN reference PCM. Default software AEC still uses
  `jasper_ref`.
- `rust/jasper-fanin` is already a good model for the Rust service
  style: blocking ALSA output as timing owner, non-blocking inputs,
  preallocated buffers, systemd watchdog, xrun counters, and small
  testable pure functions.

## Current Outputd State

As of 2026-05-28, outputd is the mainline output topology. Rollback to
the prior `jasper_out` topology is explicit: run
`bash scripts/disable-outputd-cutover.sh`, deploy a pre-outputd release
or rollback branch, and run the helper again if the DAC is still busy.
The helper is necessary because older code does not know about, and
therefore cannot disable, the outputd unit.

What exists:

- Rust crate: `rust/jasper-outputd`.
- Fake backend remains the default for safe developer runs:
  `jasper-outputd --once` does not open ALSA or touch `/run`.
- Real backend: systemd sets `JASPER_OUTPUTD_BACKEND=alsa`.
- Content input: `outputd_content_capture`, backed by snd-aloop
  substream 6 (`hw:Loopback,1,6`).
- Content bridge: packaged default is
  `JASPER_OUTPUTD_CONTENT_BRIDGE=direct`. The opt-in lab mode
  `rate_match` keeps the DAC as timing owner, drains
  `outputd_content_capture` into an explicit bounded ring, and renders
  DAC-sized periods through a ppm-clamped windowed-sinc rate matcher.
  Use this for DAC/content-lane clock-slip validation only until it has
  passed long jts3 soaks; it is not a broad DAC abstraction.
  Default lab settings add 4096 frames of content latency (~85 ms at
  48 kHz) while leaving direct TTS/cue playout on the normal outputd
  path. The sinc table is precomputed at startup; steady state should
  be multiply/add work only, but Pi 5 CPU and xrun behavior still need
  hardware soak before enabling it outside the lab.
- DAC output: `outputd_dac`, a direct hardware alias for the selected
  final-output card. Public/default installs use the Apple USB-C
  dongle; DAC8x lab installs use the enumerated
  `snd_rpi_hifiberry_dac8x` card. `install.sh` also writes
  `JASPER_AUDIO_DAC_ID` (`apple_usb_c_dongle` or `hifiberry_dac8x`)
  into `/etc/jasper/jasper.env` so validation artifacts and status
  surfaces have a stable hardware id instead of only the generic
  `outputd_dac` PCM name.
- Apple-only analog mixer services: `jasper-dac-init.service` and
  `jasper-headphone-monitor.service` exist to pin/watch the Apple USB-C
  dongle `Headphone` control. They are enabled and started only when
  the Apple dongle is detected at install time. DAC8x installs render
  the templates for idempotence but disable/reset those units because
  DAC8x has no Apple `Headphone` mixer control.
- Camilla outputd config: `/etc/camilladsp/outputd-cutover.yml` after
  install, copied from `deploy/camilladsp/outputd-cutover.yml`.
- Camilla rollback preservation: the outputd `jasper-camilla.service`
  reads `/var/lib/camilladsp/outputd-statefile.yml`, not the normal
  `/var/lib/camilladsp/statefile.yml`. The normal statefile, including
  any active room-correction/sound-profile path, is left intact for
  rollback by disabling outputd and deploying a pre-outputd tree. The
  outputd statefile is preserved across redeploys when it points at an
  outputd-safe config, and reset to the flat outputd baseline only when
  it is missing, stale, points at a legacy `jasper_out` playback path,
  or omits the non-positive Camilla `volume_limit` safety ceiling.
- TTS transport: `JASPER_TTS_TRANSPORT=outputd` makes Python send
  resampled 48 kHz stereo PCM plus gain metadata over
  `JASPER_TTS_OUTPUTD_SOCKET`; the outputd transport rejects any output
  rate other than 48 kHz, and Python chunks long cached WAV payloads
  into 250 ms stereo-frame-aligned IPC messages before the daemon's
  bounded allocation limit. Assistant response chunks may also carry a
  provider item id over the same IPC protocol; OpenAI wires
  `response.output_item.added.item.id` into that field today, while
  providers without item ids leave it empty.
- Runtime `JASPER_TTS_TRANSPORT=sounddevice` is intentionally rejected
  in this outputd-loudness tree. That older PortAudio path no longer has
  the dynamic content/profile matching policy; rollback means deploying a
  pre-outputd revision, not flipping the env var in current main.
- TTS interruption: outputd flushes are epoch-based. A flush advances
  the TTS epoch, clears the already-enqueued assistant buffer, and
  ignores any pre-flush audio commands that had been accepted onto the
  bounded IPC queue but not yet mixed. That keeps barge-in from
  resurrecting stale assistant audio after the interrupt path returns.
  Python uses a synchronous `FLUSH_SYNC` path for interruption and gets
  a compact JSON acknowledgement with per-segment `audio_played_ms`,
  flushed frames, provider item id, and local segment id. The Python
  client bounds this ack wait and closes the ordered TTS socket on
  timeout so a late stale ack cannot be mistaken for a later flush.
- Playout ledger: outputd keeps active assistant/cue segments plus a
  bounded recent terminal history, so long uptimes do not accumulate
  one segment per TTS chunk indefinitely. Written frames are not treated
  as heard frames: the ALSA backend reads the DAC playback delay after
  each successful write and the ledger estimates drained frames from
  that output clock.
- Runtime unit: `deploy/systemd/jasper-outputd.service` is enabled by
  `deploy/install.sh` and sets the ALSA/socket defaults.
  Optional lab retuning belongs in `/var/lib/jasper/outputd.env`; the
  unit loads it after the packaged defaults, and the AirPlay renderer
  reads the same file when deriving backend latency offset.
  If outputd cannot stay up after its restart burst, systemd reboots
  cleanly via `StartLimitAction=reboot` rather than leaving the speaker
  without its final-output owner.
  During install, `jasper-voice` is stopped before outputd is restarted
  so an old PortAudio process cannot keep the legacy DAC path open; the
  AEC reconciler then restarts or parks voice according to current mic
  hardware. The installer treats outputd as mandatory:
  missing source, missing binary, failed unit restart, or failed STATUS
  probe fails the install instead of restarting voice into a silent
  output path.
- Observability: `event=outputd.*` structured logs, `/state.outputd`
  via `jasper-control`, `/system` Outputd row, and
  `jasper-doctor` checks. The daemon reports negotiated ALSA
  period/buffer sizes, xrun counters, content empty/partial/EAGAIN
  periods, last-xrun age, uptime-normalized xrun rate, watchdog
  progress, clipping, pending TTS frames, TTS over-budget duration,
  dropped TTS command/audio-frame counters, and compact TTS flush
  summaries so producer/playback backpressure is visible without
  journal spam. The dashboard labels the two xrun
  counters as content/DAC, since a content-capture recovery is a
  different risk from a physical-output recovery.
  When the opt-in content bridge is enabled, STATUS also reports
  bridge mode, lock state, ring fill/min/max, target fill, ppm ratio,
  input/output/silence frames, underrun/overrun frames, resyncs,
  resets, and ratio-clamp count. Bridge lock/unlock/resync/reset and
  rate-limited overrun/clamp transitions also emit structured
  `event=outputd.content_bridge.*` journal lines. `jasper-doctor` and
  the `/system` Outputd row summarize the same bridge state when the
  opt-in mode is enabled, and doctor warns on concrete anomaly counters
  such as underrun, overrun, resync, reset, or ratio clamp.

What is still intentionally not done:

- Production AEC still consumes the old `pcm.jasper_ref` content
  reference.
- `speaker_reference_out` is still not a general public transport. The
  only current external side outputs are recorder-owned chip-AEC corpus
  taps, enabled by explicit env and removed on corpus-mode exit.
- Provider truncation is not yet wired to outputd flush
  acknowledgements. The transport now returns the needed
  `audio_played_ms` and provider item identity; provider-specific
  truncate/cancel commands still need to consume it.
- The latest TTS ledger refinements (provider item id over IPC,
  synchronous flush acknowledgement, and DAC-delay-based drain
  accounting) still need Pi validation after an operator-approved
  deploy.

## Problem Boundaries

This is primarily an output problem, not an input problem.

Future record-player, HDMI, USB, AirPlay, Spotify, Bluetooth, or
network sources are content inputs. They should enter the content
graph once, flow through source policy and DSP, and automatically
become part of the final speaker-output reference.

The output-reference problem starts later:

```text
all audible program material -> final mix / protection / DAC write
                              -> exact speaker_output_reference
```

The reference must represent what the speakers were asked to emit
after source selection, ducking, TTS gain, cue gain, safety clamps,
and any future output protection. It should not be reconstructed by
summing several delayed side channels inside the AEC bridge.

## North Star

The long-term architecture is a small JTS-native output owner,
provisionally named `jasper-outputd`:

```text
CONTENT PATH
  renderers -> jasper-fanin -> jasper-camilla
                                      |
                                      v
                              content_post_dsp
                                      |

ASSISTANT PATH                       v
  TTS / cues / chirps -------> jasper-outputd -------> DAC / amp / speakers
                                  |
                                  +--> speaker_output_reference
                                  |      -> AEC bridge
                                  |      -> wake/corpus capture
                                  |      -> telemetry/debug consumers
                                  |
                                  +--> tts_playout_ledger
                                         -> realtime truncation
                                         -> barge-in decisions
                                         -> corpus provenance
```

`jasper-outputd` should be boring on purpose. It is not a desktop
audio server. It owns exactly the final JTS speaker boundary:

- read the post-Camilla content stream
- accept assistant/cue/chirp PCM from the voice daemon
- apply final mix, clamps, and future speaker-protection processing
- write to the physical DAC
- publish one coherent `speaker_output_reference`
- report playout ledger events for realtime-model turn state

Once this exists, the AEC bridge consumes `speaker_output_reference`
instead of the pre-Camilla content tap. Barge-in during assistant
speech becomes structurally possible because the echo canceller sees
the assistant audio the microphone is hearing.

## Why This Is Better Than The Fan-In Mirror

The abandoned fan-in mirror would have improved one local failure
mode: AEC/corpus/debug consumers contending with Camilla on a shared
`dsnoop` surface during heavy corpus collection.

It would not have solved the product-level problem:

- it still excluded TTS/cues/chirps
- it was still pre-Camilla
- it did not know what actually drained to the DAC
- it could not drive realtime truncation decisions
- it did not help future active speaker protection

That makes it a useful investigation artifact, not the architecture.

The output-owner direction solves the deeper problem once, at the
right boundary. It gives us a single point where "audible output" is
mixed, measured, protected, referenced, and accounted for.

## What To Take From PipeWire

Do not implement PipeWire as a dependency for JTS. The daemon,
session manager, compatibility layers, arbitrary dynamic graph,
desktop hotplug policy, and plugin surface are far larger than this
appliance needs.

The useful lessons are smaller and specific:

- **Node / port / link vocabulary.** Treat `content_in`, `tts_in`,
  `dac_out`, `speaker_reference_out`, and `telemetry_out` as explicit
  ports even if they are implemented with ALSA plus Unix sockets.
- **One timing driver per graph.** The DAC write loop should drive the
  final output graph. Optional consumers must not become playback
  timing owners. PipeWire's graph scheduler describes this explicitly:
  a driver node starts each cycle, and dependent nodes run only when
  their upstream dependencies complete.
- **Async side consumers.** AEC, corpus, and debug readers receive
  copies through bounded queues/rings. If they fall behind, they drop
  frames with counters; they do not block playback. PipeWire's async
  links use the same idea and add a cycle of latency rather than
  putting side work in the synchronous graph completion path.
- **Explicit ring semantics.** Use bounded storage, monotonic sequence
  numbers, underrun/overrun counters, and clear drop policy rather
  than hidden buffering. PipeWire's `spa_ringbuffer` is only two
  atomic indices over caller-owned memory, and its read/write helpers
  explicitly report underrun/overrun conditions.
- **Rate matching at clock boundaries.** A loopback capture clock and
  a physical DAC clock can both be nominally 48 kHz while drifting by
  tens of ppm. The production shape is not "make the ALSA buffer huge";
  it is an explicit bridge with a target fill, a low-bandwidth
  controller, a high-quality variable-rate resampler, and counters for
  clamp/underrun/overrun/resync behavior.
- **Four-stream AEC shape.** Echo cancellation is easiest to reason
  about when playback/reference and capture/cleaned-mic streams are
  explicit surfaces, not incidental taps.
- **Small backend interfaces.** Keep AEC engines and output transports
  behind narrow traits/interfaces so WebRTC AEC3, future engines, ALSA
  hardware, and test fakes are swappable without changing topology.

What not to take:

- WirePlumber/session-manager policy
- PulseAudio/JACK compatibility
- arbitrary user-routable audio graphs
- module loading as a runtime extension mechanism
- PipeWire as another always-on service in the product
- a hybrid "mostly ALSA plus a little PipeWire" topology

References verified 2026-05-27:

- <https://docs.pipewire.org/page_scheduling.html>
- <https://docs.pipewire.org/ringbuffer_8h_source.html>
- <https://docs.pipewire.org/aec_8h_source.html>
- <https://docs.pipewire.org/page_module_echo_cancel.html>

## Design Requirements

Playback requirements:

- One process owns the final DAC write path.
- Music and TTS keep playing if AEC/corpus/debug consumers crash.
- Optional reference consumers are never in the blocking playback path.
- Queue sizes and drop behavior are explicit and observable.
- The normal steady-state path stays cheap enough for 1 GB Pi 5 units.

Signal requirements:

- `speaker_output_reference` includes content, TTS, cues, chirps, and
  future system sounds.
- The reference is emitted after gains, ducking, final mix, and future
  safety/protection processing.
- AEC receives one coherent reference stream, not separately delayed
  content and TTS references.
- Frames carry sample rate, channel count, frame count, sequence, and
  monotonic timestamp metadata.

Realtime requirements:

- TTS playout has a durable ledger: provider item id, local playout id,
  queued frames, written frames, estimated drained frames, flushed
  frames, and final status.
- Barge-in can answer "what part of the assistant response did the
  user actually hear?"
- Provider-specific truncation APIs stay behind the voice-provider
  abstraction where possible.
- Corpus/debug captures can mark provenance damage when reference,
  mic, or playout-ledger data was missing or dropped.

Future hardware requirements:

- Active speaker DSP/protection must sit on every audible path,
  including TTS and cues.
- TTS must not permanently bypass crossovers, limiters, driver
  protection, or level guards.
- Adding HDMI, record-player ADC, USB input, or other content sources
  should not create new AEC reference work; they join the content path
  upstream of the output owner.

## Language Boundary

Use Rust for the realtime output owner.

`jasper-outputd` should own the final DAC loop, content/TTS mixing,
bounded queues, xrun recovery, reference fanout, sequence counters,
and playout accounting. Those are realtime-ish, stateful, and easier
to make boring in Rust than in Python.

Keep Python for voice policy:

- provider sessions and provider-specific truncation/cancel events
- wake/session state machines
- tool execution
- TTS generation requests
- cue selection and text rendering
- volume policy such as "what gain should TTS target right now?"

The clean split is:

```text
Python decides what should happen.
Rust owns the audio clock and reports what actually happened.
```

## Non-Goals

- Do not build a general-purpose audio server.
- Do not support arbitrary user graphs or dynamic plugin routing.
- Do not make AEC mandatory for playback.
- Do not make corpus/debug capture part of the realtime audio clock.
- Do not re-open chip-AEC or PipeWire migration as part of this work.
- Do not preserve the fan-in content-mirror spike as a compatibility
  path.

## Implementation Specification

Build pieces off-path first, but do not leave a permanent halfway
production architecture. The production cutover should move final
content playback and assistant playback under `jasper-outputd`
together.

### Service Shape

- New Rust binary: `jasper-outputd`.
- Service style mirrors `jasper-fanin`: `Type=notify`,
  `WatchdogSec`, progress-gated watchdog pings, audio slice,
  bounded memory, no disk I/O on the hot path.
- Hot path uses preallocated buffers. No allocation, logging, file I/O,
  blocking IPC, or network I/O in the DAC write loop.
- One thread owns ALSA playback to the DAC. Side consumers are fed by
  bounded queues/rings and sender threads.
- `READY=1` means the selected backend is actually usable. For the
  ALSA backend, emit it only after the PCMs are opened, negotiated
  period/buffer state is captured, the DAC has been primed with
  silence, playback has started, and the STATUS socket has already
  bound.
- Initial sample shape: 48 kHz, stereo, S16_LE. Expose negotiated
  period/buffer sizes in state; do not hide ALSA's actual values.
- Initial period policy: keep Camilla's 1024-frame chunk shape unless
  measurement justifies changing it. Do not force a 960-frame graph
  solely to match AEC's 20 ms frame; the AEC adapter can reframe.

### Ports

`content_in`:

- Source: private post-Camilla loopback capture.
- Proposed ALSA lane: use the currently reserved snd-aloop substream 6.
  Camilla writes to `hw:Loopback,0,6`; `jasper-outputd` reads
  `hw:Loopback,1,6` through named aliases.
- Shape: 48 kHz stereo S16_LE.
- Ownership: Camilla is the only writer; `jasper-outputd` is the only
  reader. No `dsnoop` on this lane.

`tts_in`:

- Source: voice daemon and cue manager.
- Transport: local Unix socket with ordered, reliable framing. Prefer
  `SOCK_SEQPACKET` with bounded message sizes; a length-prefixed Unix
  stream is acceptable if testing shows Python support is cleaner.
- Commands: start segment, audio chunk, set target gain, end segment,
  flush segment/session.
- Large cues must be chunked by the client. Do not send multi-second
  cue files as one IPC message.
- Rust enforces the final gain clamp even if Python computed the
  target gain.

`dac_out`:

- Sink: physical Apple USB-C DAC hardware, preferably direct `hw:` or
  the smallest stable ALSA alias around it.
- Ownership: `jasper-outputd` is the only normal writer.
- `pcm.jasper_out` stops being the production convergence point after
  cutover.

`speaker_reference_out`:

- Source: exact mixed samples sent toward `dac_out`, after content
  gain, TTS gain, cue gain, clipping policy, and future protection.
- Canonical shape: 48 kHz stereo S16_LE plus metadata.
- Metadata: stream id, sequence, monotonic timestamp, sample rate,
  channels, format, frame count, clipped sample count.
- Delivery: per-consumer bounded queues. Slow consumers drop/count;
  they never block `dac_out`.
- Publish only after the corresponding DAC period write succeeds. A
  prepared-but-unwritten period must not advance reference sequence,
  playout ledger, or "frames heard" counters.
- AEC bridge initially consumes this and keeps its existing
  downmix/resample/HPF/AEC-frame logic.

`playout_events`:

- Metadata-only event stream; do not persist audio or transcript text.
- Fields: local segment id, provider item id where available, kind
  (`assistant`, `cue`, `chirp`), gain, frames queued, frames written,
  estimated frames drained, frames flushed, `audio_played_ms`, status,
  start/end/flush monotonic timestamps.
- Consumers: voice daemon, wake/corpus metadata, `/system` state.

### Mixer Semantics

- Mix content and assistant audio with saturating i32 accumulation
  followed by i16 clamp, matching `jasper-fanin`'s simple and
  testable behavior.
- Report clipped samples per period and per segment.
- TTS/cues must be mixed after content ducking, not as part of the
  Camilla `main_volume` path.
- Future protection/limiting belongs after final mix and before both
  `dac_out` and `speaker_reference_out`.

### Barge-In Contract

When user speech is detected during assistant playback:

- detect user speech while assistant audio is playing
- voice daemon sends `flush` to `jasper-outputd`
- outputd drops queued assistant frames, keeps or fades content
  according to current ducking policy, and returns per-segment
  `audio_played_ms`
- send the appropriate provider truncation/cancel event
- preserve the transcript state that matches what the user heard

The provider abstraction should hide vendor naming, but not the core
datum: how much assistant audio was actually heard.

### Rollout Plan

1. **Off-path Rust core.** Add `jasper-outputd` with fake content,
   fake TTS, fake DAC, fake reference consumers, and no deployment
   wiring. Unit-test queue behavior, clipping, sequence numbers,
   playout ledger math, and flush semantics. **Landed 2026-05-28.**
2. **Pi cutover.** Add the post-DSP loopback lane, point
   Camilla playback at it, route TTS/cues to outputd, and let outputd
   own the DAC. This is one topology cutover, not a permanent split
   mode. **Landed on main 2026-05-28:** lane aliases, cutover Camilla
   config/statefile, outputd ALSA backend, TTS socket transport, state
   socket, doctor, and system dashboard are in-tree.
3. **Soak before AEC switch.** Verify normal music, AirPlay, Spotify,
   Bluetooth, USB input, TTS, cues, duck/restore, dongle recovery, and
   zero output xruns. Keep AEC on the old reference during this soak so
   playback and AEC regressions are separable.
4. **Move AEC reference.** Switch `jasper-aec-bridge` from
   `pcm.jasper_ref` to `speaker_reference_out`. Treat reference drops
   as capture-health degradation, not playback failure.
5. **Enable robust barge-in.** Wire outputd flush acknowledgements to
   provider truncation/cancel logic and capture barge-in telemetry.

### Required Tests

- Rust unit tests for mixer saturation, no-allocation steady-state
  paths where feasible, ring full/empty behavior, sequence gaps,
  reference fanout drops, and playout ledger math.
- Python tests that the `TtsPlayout` replacement preserves
  `write/flush/expected_drain_at/wait_drained` semantics.
- ALSA config tests for the post-DSP lane names and no raw `hw:`
  readers that would steal a loopback substream.
- Integration probe on Pi: 30 minutes each of AirPlay, Spotify,
  Bluetooth, USB input, and TTS-over-music with no output xruns.
- Corpus capture-health test proving reference packet loss/drops mark
  affected clips compromised.
- Barge-in test: assistant speaks, user interrupts, outputd flushes,
  and provider truncation receives an `audio_played_ms` within one
  output period of the ledger estimate.

### Success Criteria

- Only `jasper-outputd` writes to the physical DAC during normal
  operation.
- `speaker_output_reference` includes content, TTS, cues, and chirps.
- AEC/corpus/debug can crash or fall behind without affecting audible
  playback.
- Normal outputd RSS target is under 20 MB.
- Output xrun count is zero in a realistic 24-hour soak.
- Barge-in during assistant speech produces a measured truncation point
  and does not leave the realtime model believing unheard audio was
  heard.

## Open Design Questions

- Should `jasper-outputd` be Rust from the start, matching
  `jasper-fanin`, or Python first for faster iteration? Decision:
  Rust for the realtime core; Python remains the policy/client layer.
- Should assistant PCM enter over Unix datagrams, a Unix stream, shared
  memory rings, or a small local protocol? The answer should be driven
  by backpressure and flush semantics, not convenience alone. Current
  leaning: ordered Unix socket protocol, not best-effort datagrams.
- Should `speaker_reference_out` publish post-limiter stereo, mono
  summed AEC-ready frames, or both? AEC wants mono 16 kHz; corpus and
  debugging often want higher-fidelity stereo provenance.
- What is the exact DAC target: direct hardware PCM, `plughw`, or a
  very small ALSA wrapper? The goal is to avoid using `dmix` as the
  main architecture boundary while preserving stable device setup.

## Decision Record

- 2026-05-27: Treat robust barge-in during assistant speech as a known
  product requirement, not a speculative future enhancement.
- 2026-05-27: Abandon the fan-in content-reference mirror as the
  strategic direction. It addressed corpus/debug pressure but not the
  final speaker-reference problem.
- 2026-05-27: Prefer a JTS-native output owner over adopting PipeWire.
  Borrow PipeWire's graph, scheduling, and ring-buffer lessons; do not
  ship PipeWire's desktop audio stack.
- 2026-05-27: The long-term reference must be
  `speaker_output_reference`, not `content_reference`.
- 2026-05-27: Implementation should build testable pieces off-path,
  then cut over production audio as one output-owner topology. Avoid a
  permanent TTS-only or content-only half-architecture.
- 2026-05-28: Land the real transport in `jasper-outputd`: ALSA
  capture from `outputd_content_capture`, direct DAC playback to
  `outputd_dac`, runtime xrun counters, negotiated buffer/period state,
  `/state`/doctor/dashboard surfaces, and structured `event=` logs.
- 2026-05-28: Add production-polish observability: content
  empty/partial/EAGAIN counters, TTS queue over-budget duration,
  aggregate `event=outputd.tts_flush` traces, and source-handoff IDs
  that correlate mux journal lines with `/source/state.last_handoff`.
- 2026-05-28: Convert the work into branch-as-switch form for lab
  validation. Deploying `codex/outputd-cutover` enabled outputd and
  pointed Camilla at a separate outputd statefile. This was superseded
  later the same day by the mainline merge; rollback now means
  disabling outputd and deploying a pre-outputd release or branch.
- 2026-05-28: Merge the cutover into main, then add the remaining
  playout-ledger contract polish: provider item identity on TTS
  segments, synchronous `FLUSH_SYNC` acknowledgements with
  `audio_played_ms`, and DAC-delay-based drained-frame estimation.
- 2026-06-01: Move assistant loudness policy fully into outputd. Python
  now owns only provider profile seeding/learning; outputd owns content
  loudness measurement, peak-aware gain decisions, STATUS telemetry,
  and correction-window meter pause/resume.
- 2026-06-01: Add the disabled-by-default outputd content bridge
  (`JASPER_OUTPUTD_CONTENT_BRIDGE=rate_match`) for DAC-paced
  rate-matching validation. Packaged production remains `direct`; the
  bridge is a lab-gated pipeline fix for snd-aloop content-lane drift.
- 2026-06-02: Split final-output DAC role from Apple mixer ownership.
  `outputd_dac` may target the Apple USB-C dongle or the JTS3 DAC8x;
  `jasper-dac-init`/`jasper-headphone-monitor` now run only on Apple
  dongle installs. Added the outputd-only DAC8x validation profile
  `hifiberry_dac8x_outputd_stability` for content-pipeline soaks that
  should not fail just because chip-AEC/voice is parked.

Last verified: 2026-06-02
