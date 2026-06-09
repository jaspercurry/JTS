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
    -> /run/jasper-fanin/tts.sock
    -> jasper-fanin, mixed after program duck
    -> jasper-camilla crossover/protection
    -> outputd_content_playback/capture
       (or outputd_active_content_* for dual Apple active output)
    -> jasper-outputd final sink
    -> DAC(s) / amp(s) / speaker(s)
```

The production paths converge inside `jasper-fanin`, pass through
CamillaDSP, then enter `jasper-outputd`, which is the only normal writer
to the physical DAC. The dual Apple active-output profile keeps the same
TTS/cue semantics, but CamillaDSP emits a four-channel active lane and
`jasper-outputd` splits it to two Apple DACs. The legacy `pcm.jasper_out`
dmix remains defined in `/etc/asound.conf` for
emergency rollback and older checkouts, but current production audio
does not use it as the convergence point.

The production AEC bridge now consumes outputd's speaker monitor over
localhost UDP. Outputd publishes the final electrical samples it is about
to write to the configured DAC sink, with STATUS metadata describing the
reference contract:

- `reference_outputs.speaker_reference_source=outputd_final_electrical`
- 48 kHz stereo for software AEC/corpus/diagnostics
- `speaker_reference_active=true` when a UDP or chip-reference consumer is
  configured
- for dual Apple active output, stereo monitor left/right are the average
  of the speaker-local low/high driver lanes for each speaker

This is the final software/electrical reference. It includes renderer
content, TTS/cues/chirps, fan-in ducking/gain, CamillaDSP
filters/crossover/protection, and outputd sink selection. It still cannot
include DAC analog behavior, amp/driver/cabinet response, or room
acoustics except indirectly through the microphone.

The same outputd fanout feeds software AEC, chip-AEC, corpus capture, and
diagnostics. Chip-AEC additionally needs the XVF3800 USB-IN reference PCM
(`JASPER_OUTPUTD_CHIP_REF_PCM`), which is a hardware actuator separate
from the software UDP monitor. The UDP tap stays at outputd's 48 kHz graph
rate; the chip-reference PCM is downsampled to the XVF3800 USB-IN contract
(`16 kHz`, default `320`-frame periods, `1280`-frame buffer).

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
  provider/model/voice profile metadata to the fan-in TTS IPC socket
  (`/run/jasper-fanin/tts.sock`, using the outputd-compatible text
  protocol), tracks expected drain, and supports `flush()` for
  interruption. `jasper-outputd` no longer binds a TTS socket or keeps a
  duplicate TTS protocol parser; TTS/cue ingress is a fan-in concern.
- `jasper-fanin` owns assistant gain in the packaged topology. It
  snapshots pre-duck content loudness, applies the provider
  source-loudness profile and peak-capped gain policy, emits
  `event=fanin.assistant_loudness`, and publishes the latest decision
  under `tts.assistant_loudness` in its STATUS payload. Python seeds and
  learns profiles but does not set final gain.
- Cues and chirps route through the same `TtsPlayout` object. They
  inherit fan-in routing, drain behavior, flush behavior, and
  profile/peak-capped gain policy without training live assistant
  profiles, then pass through CamillaDSP crossover/protection with the
  rest of the audio stream. If a feedback sound arrives with no
  wake-turn context and no measured content baseline, fan-in uses the
  configured default
  silence target rather than a fixed legacy gain.

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
  `outputd_content_playback`, `outputd_content_capture`,
  `outputd_active_content_playback`, `outputd_active_content_capture`,
  and `outputd_dac`.
- `deploy/camilladsp/outputd-cutover.yml` sends Camilla playback to
  `outputd_content_playback`; `deploy/camilladsp/v1.yml` is retained
  as the pre-outputd rollback config that writes to `jasper_out`.
- `jasper/cli/aec_bridge.py` normally receives outputd's 48 kHz stereo
  speaker monitor via `JASPER_AEC_REF_SOURCE=outputd_udp`, downsamples it
  to 16 kHz mono, and tracks reference starvation/queue drops. Explicit
  `JASPER_AEC_REF_SOURCE=alsa` fallback/diagnostic mode can still open
  `jasper_ref`, a pre-DSP `pcm.jasper_capture` wrapper.
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
- DAC output: `outputd_dac`, normally a direct hardware alias for the
  selected final-output card. Public/default installs use the Apple
  USB-C dongle; DAC8x-family lab installs use the enumerated
  `snd_rpi_hifiberry_dac8x` card. `jasper-audio-hardware-reconcile`
  runs at install/boot and from udev `controlC*` add/remove/change
  events; it writes `JASPER_AUDIO_DAC_ID` (`apple_usb_c_dongle`,
  `hifiberry_dac8x`, `hifiberry_dac8x_studio`, or the raw fallback card
  token when no known role is detected) plus `JASPER_AUDIO_DAC_CARD` into
  `/etc/jasper/jasper.env` so validation artifacts and status surfaces
  have stable hardware identity instead of only the generic
  `outputd_dac` PCM name. `jasper-doctor` uses that same role identity:
  Apple-dongle USB/headphone-gain checks are active only when
  `JASPER_AUDIO_DAC_ID=apple_usb_c_dongle`, and are skipped for DAC8x-family
  or other selected output roles. For recognized DAC8x/DAC8x Studio hardware,
  `JASPER_OUTPUT_DAC_ROUTE=mono:N` renders a stereo-to-mono sum onto
  one 1-indexed physical DAC8x output, and
  `JASPER_OUTPUT_DAC_ROUTE=stereo:L,R` maps stereo left/right to two
  distinct 1-indexed physical outputs. Unset, `direct`, or
  `passthrough` keeps the direct alias. Invalid routes, duplicate
  stereo channels, or routes on non-DAC8x-family hardware are ignored with
  structured `event=audio_hardware_reconcile.route_ignored` logs. This
  route knob is for lab/single-amp/commissioning wiring before active
  speaker profiles are loaded; active crossover channel ownership lives
  in the active-speaker `channel_map`, not in this ALSA alias. The
  product speaker-output topology substrate is separate again:
  `jasper.output_topology` persists `/var/lib/jasper/output_topology.json`
  and `/sound/output-topology` exposes a complete replacement JSON
  contract for physical DAC lanes, speaker groups, passive/active modes,
  subwoofers, and identity/protection evidence. That topology model has
  no playback authority, does not rewrite ALSA, and does not load
  CamillaDSP; it only records and evaluates whether future safe sound
  tests may proceed through their own safety session. The companion
  `/sound/active-speaker/channel-identity` route records operator-confirmed
  physical channel identity for assigned topology channels. It is evidence
  about wiring only: it does not make the active path safe, does not satisfy
  tweeter protection, and does not allow any endpoint to emit sound.
  `/sound/active-speaker/playback-readiness` now composes that topology
  evidence with active-speaker environment, safe-session, calibration-level,
  clock-domain, Stop-control, and tone-backend evidence for one selected
  target. The topology itself still grants no playback authority; the separate
  active-speaker lab backend can emit only when readiness passes, explicit
  `aplay` env enablement is present, and the target is not a tweeter/
  compression driver.
  `/sound/active-speaker/channel-protection` records the separate human
  evidence that a compression-driver protection path is physically present or
  that software-guarded bring-up has been explicitly requested. Software guard
  remains a topology/playback blocker; it only lets
  `/sound/active-speaker/stage-config` consume the saved mono active 2-way
  topology and write a no-load protected Epique/F110M startup candidate plus
  startup-mute/high-pass/limiter/headroom evidence. That staging route still
  does not rewrite ALSA, load CamillaDSP, reload a graph, or emit sound.
  The same topology payload includes a clock-domain report for the detected
  final-output hardware. Supported topology hardware IDs include
  `apple_usb_c_dongle`, `hifiberry_dac8x`,
  `hifiberry_dac8x_studio`, and
  `dual_apple_usb_c_dac_4ch`. The normal playback runtime is still a
  single-device contract: a recognized DAC8x/DAC8x Studio or Apple output
  device can be described as one coherent output clock. The dual-Apple profile
  is the constrained composite exception: the hardware shape is valid only for
  exactly two Apple USB-C DAC children, each owning one speaker-local stereo
  pair, with four physical outputs total and current reconciler observation
  confirming the expected same USB controller/bus shape. Stored 900 s
  common-clock drift evidence is surfaced as validation evidence and can block
  if it failed, but missing long-run evidence is a warning rather than the
  identity of the hardware profile. Missing, partial, or mismatched live
  hardware observation blocks the composite clock report. When the live
  profile is ready, the audio-hardware reconciler writes
  `/var/lib/jasper/outputd.env` for `JASPER_OUTPUTD_SINK=dual_apple`
  with two pinned child PCMs. TTS/cues plus duck commands already route
  to fan-in for every output profile, so dual Apple does not need a
  separate TTS override. Partial
  dual states park normal output instead of routing stereo outputd to
  the first dongle. Runtime sink activation is intentionally stricter
  than hardware observation: `jasper-audio-hardware-reconcile` switches
  `/var/lib/jasper/outputd.env` to `JASPER_OUTPUTD_SINK=dual_apple` only
  when the active-speaker startup-load state is `loaded`, CamillaDSP's
  outputd statefile points at that active config, and the active config
  is the expected four-channel `outputd_active_content_playback` graph.
  Until that graph evidence is present, the observed output-hardware
  profile remains dual Apple for UI/diagnostics, but the runtime DAC role
  is parked with `JASPER_OUTPUTD_BACKEND=fake` and logs
  `event=audio_hardware_reconcile.dual_apple_detected action=park_until_active_graph`.
  Active-speaker load and rollback trigger the reconciler once, matching
  the existing udev/install triggers without adding a poller. This is not
  a generic endorsement of ALSA
  `multi`/`dmix`/`plug` or CamillaDSP multi-device output.
  The configured route takes effect when deploy, boot/udev reconcile, or a
  manual `jasper-audio-hardware-reconcile` run re-renders the managed
  ALSA template; hardware validation artifacts report the observed
  route in `dac_identity`. A recognized role renders the ALSA template
  and restarts
  `jasper-outputd` so hotplug arrival recovers from a previously parked
  state. An unknown/no-output role does **not** render `outputd_dac` to
  a guessed card; it stops `jasper-voice` and `jasper-outputd` so stale
  direct-DAC ownership cannot keep running against removed hardware or
  burn the outputd reboot escalation budget.
- Apple-only analog mixer services: `jasper-dac-init.service` and
  `jasper-headphone-monitor.service` exist to pin/watch the Apple USB-C
  dongle `Headphone` control. The audio-hardware reconciler enables
  them only when the selected final-output DAC is the recognized Apple
  dongle. DAC8x and unknown-output states disable/reset those units so
  irrelevant Apple-specific code does not run. The helpers are still
  runtime-safe if an operator starts them manually: `jasper-dac-init`
  exits cleanly when no Apple dongle is detected, and
  `jasper-headphone-monitor` waits quietly in auto-detect mode.
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
  `JASPER_TTS_OUTPUTD_SOCKET`; in the packaged topology that socket is
  `/run/jasper-fanin/tts.sock`. The outputd-compatible transport rejects any output rate
  other than 48 kHz, and Python chunks long cached WAV payloads into
  250 ms stereo-frame-aligned IPC messages before the daemon's bounded
  allocation limit. Assistant response chunks may also carry a provider
  item id over the same IPC protocol; OpenAI wires
  `response.output_item.added.item.id` into that field today, while
  providers without item ids leave it empty.
- Voice duck transport: `JASPER_DUCK_TRANSPORT=fanin` sends
  `PROGRAM_DUCK_ON/OFF` to `jasper-fanin`, which attenuates
  renderer/program lanes before mixing TTS/cues. TTS therefore remains
  audible and still flows through CamillaDSP crossover/protection.
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

- Software can expose the final electrical samples sent to the DAC, but
  cannot expose the acoustic "what hit the room" signal after DAC, amp,
  driver, cabinet, and room behavior. That still requires microphone-side
  observation.
- The chip USB-IN producer is intentionally separate from the software
  speaker monitor. Software AEC/corpus/diagnostics should not depend on
  chip hardware being present.
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
  TTS / cues / chirps -------> jasper-fanin -> jasper-camilla -> jasper-outputd
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
- TTS/cues must be mixed after content ducking. In current production
  that happens in `jasper-fanin` before CamillaDSP crossover/protection.
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
   zero output xruns. **Landed:** outputd is mandatory in the packaged
   topology and exposes STATUS/doctor surfaces for DAC/reference health.
4. **Move AEC reference.** Switch `jasper-aec-bridge` from
   `pcm.jasper_ref` to outputd's speaker monitor. Treat reference drops
   as capture-health degradation, not playback failure. **Landed
   2026-06-08:** software AEC, chip-AEC, corpus, and diagnostics consume
   the same outputd monitor contract; `pcm.jasper_ref` remains explicit
   fallback/diagnostic mode.
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
  empty/partial/EAGAIN counters, then-outputd TTS queue over-budget
  duration, aggregate `event=outputd.tts_flush` traces, and source-handoff
  IDs that correlate mux journal lines with `/source/state.last_handoff`.
  The outputd TTS pieces in this historical entry were superseded by the
  2026-06-08 fan-in TTS contract below.
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
  `jasper-audio-hardware-reconcile` now owns install/boot/udev-triggered
  DAC role convergence, and `jasper-dac-init`/
  `jasper-headphone-monitor` are enabled only for the recognized Apple
  final-output role, with runtime-safe helper scripts for
  manual/operator starts. Added the outputd-only DAC8x validation profile
  `hifiberry_dac8x_outputd_stability` for content-pipeline soaks that
  should not fail just because chip-AEC/voice is parked.
- 2026-06-02: Added the explicit DAC8x-family
  `JASPER_OUTPUT_DAC_ROUTE` render path (`mono:N`, `stereo:L,R`) so
  lab wiring like "single amp on DAC8x physical output 5" survives
  deploy/reconcile without a hand-edited `/etc/asound.conf`. The
  default remains direct; non-DAC8x-family or invalid routes are ignored and
  logged.
- 2026-06-02: Added the first product speaker-output topology contract
  (`jasper.output_topology`, `/var/lib/jasper/output_topology.json`,
  `/sound/output-topology`). It is a no-audio, no-Camilla, no-ALSA
  persistence/evaluation surface for DAC lanes, speaker groups, active
  driver roles, subwoofer routing, and identity/tweeter-protection
  evidence. Safe playback remains a separate active-speaker session.
- 2026-06-03: Added the active-speaker playback-readiness gate and the
  artifact-first topology channel-test slice. Default installs still verify
  artifacts only; an explicit lab `aplay` backend can emit short, clamped
  non-tweeter tests after readiness passes.
- 2026-06-04: `jasper-doctor` now gates Apple-dongle-specific USB and
  headphone-gain checks on `JASPER_AUDIO_DAC_ID=apple_usb_c_dongle`, so
  HiFiBerry/DAC8x systems report the selected output role instead of false
  Apple-dongle failures.
- 2026-06-08: Retired outputd's disabled TTS IPC implementation after
  rollback no longer needed it. Fan-in is the sole production TTS/cue IPC
  owner; outputd owns final electrical output and monitor/reference fanout.
  Dual-Apple outputd activation is also graph-gated: hardware observation
  alone records the composite profile, but outputd switches to the
  four-channel sink only after the active-speaker startup config is loaded
  and CamillaDSP's outputd statefile points at that active graph.

Last verified: 2026-06-08
