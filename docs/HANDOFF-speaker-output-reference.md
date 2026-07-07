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
       (or outputd_active_content_* for active-output profiles)
    -> jasper-outputd final sink
    -> DAC(s) / amp(s) / speaker(s)
```

The production paths converge inside `jasper-fanin`, pass through
CamillaDSP, then enter `jasper-outputd`, which is the only normal writer
to the physical DAC. Active-output profiles keep the same TTS/cue
semantics: a single Apple dongle can run a width-2 active lane,
DAC8x-family profiles can run up to width 8, and the dual Apple composite
emits a four-channel active lane that `jasper-outputd` splits to two
Apple DACs.

**Passive/dumb bonded multiroom member (Increment 5 PR-2):** the
assistant path above is the solo/active-output topology. While a
non-sub passive speaker is a bond member, the grouping reconciler points
voice's `JASPER_TTS_OUTPUTD_SOCKET` at `/run/jasper-outputd/tts.sock`
instead — outputd serves fanin's exact TTS wire protocol
(`rust/jasper-outputd/src/tts.rs`) and mixes the member's own TTS/cues
into the post-round-trip `dac_content` lane, pre-reference (inv-A
holds; `PROGRAM_DUCK` ducks the content lane member-locally). Music
keeps the synced snapcast path; only assistant audio goes local. Active
endpoints deliberately do **not** arm this outputd TTS socket: voice
stays on fan-in upstream of CamillaDSP so assistant audio is crossed
over/protected at the endpoint's active width. Wireless sub followers
park voice and keep outputd TTS unarmed.
Canonical home: [HANDOFF-multiroom.md](HANDOFF-multiroom.md) §0 /
Increment 5 PR-2, plus
[HANDOFF-distributed-active.md](HANDOFF-distributed-active.md) for the
active-endpoint safety exception. The legacy `pcm.jasper_out`
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
  provider/model/voice profile metadata to the active TTS IPC socket,
  tracks expected drain, and supports `flush()` for interruption. In the
  solo packaged topology that socket is `/run/jasper-fanin/tts.sock`; on
  a passive bonded non-sub multiroom member, the grouping reconciler instead
  points voice at `/run/jasper-outputd/tts.sock` so assistant audio mixes
  post-round-trip at the final output owner.
- Fan-in and outputd speak the same `jasper-tts-protocol` wire
  vocabulary and share the same assistant loudness policy. `jasper-fanin`
  owns assistant gain in the solo packaged topology: it snapshots
  pre-duck content loudness, applies the provider source-loudness profile
  and peak-capped gain policy, emits `event=fanin.assistant_loudness`, and
  publishes the latest decision under `tts.assistant_loudness` in its
  STATUS payload. On a bonded member, outputd applies the same loudness
  policy at the post-round-trip mix point. Python seeds and learns
  profiles but does not set final gain.
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

## Robust Barge-In Contract

As of 2026-06-09, robust assistant-speech barge-in is the next product
contract to wire, and it is explicitly **JTS-owned**. Provider-native
interruption is useful, but it does not replace local output ownership:
JTS must stop audible assistant audio first, then reconcile provider
conversation state to match what the listener actually heard.

The runtime sequence should be:

1. Detect real user speech while assistant audio is playing. The first
   production trigger should be local speech activity on the active
   chip-AEC input, not a provider-only event, because local TTS flush is
   the latency-critical action.
2. Send a synchronous flush through the active TTS transport. In the
   packaged topology that is fan-in's TTS socket
   (`/run/jasper-fanin/tts.sock`), using the outputd-compatible command
   protocol.
3. The TTS owner advances the TTS epoch and drops queued assistant
   frames. Provider truncation must additionally consume a final playout
   ledger acknowledgement containing local segment id, provider item id
   when available, and `audio_played_ms`.
4. The voice-provider adapter sends the provider-specific cancel /
   truncate operation using the playout-ledger acknowledgement, not
   arrival timestamps or queued-frame estimates.
5. The interrupted user utterance continues into the current live
   session and can produce the next response without requiring another
   wake word.

Acceptance test for the first slice:

- Assistant is speaking.
- User says a local command such as "volume down" without saying the
  wake word again.
- The local TTS path stops assistant audio immediately.
- The volume command executes once.
- Provider conversation history is truncated/canceled to match the
  `audio_played_ms` reported by the final playout ledger.
- JTS may give a short confirmation, but it must not replay stale
  assistant audio after the flush.

Provider semantics live in
[HANDOFF-voice-providers.md](HANDOFF-voice-providers.md#provider-interruption-contract).
The output-side invariant lives here: **provider truncation must be
driven from the final playout ledger, not provider event arrival time or
queued-frame estimates**.

**Status (2026-06-21).** Steps 1-3 — the provider-agnostic *detection +
local-flush spine* — have landed behind a per-provider feature flag that
**defaults OFF**. While the assistant is speaking,
`WakeLoop._handle_playback_frame` (in `jasper/voice_daemon.py`) runs local
Silero VAD on the AEC-cleaned "on" leg and, on a sustained speech run at
or above `JASPER_VAD_BARGE_IN_THRESHOLD`, calls
`LiveTurn.request_local_interrupt()`, which `_play_responses` races (now
including the `wait_drained()` drain-tail window) to `flush()` local TTS.
The flag is `JASPER_BARGE_IN_<PROVIDER>` in
`/var/lib/jasper/voice_provider.env`, read fresh per turn via
`jasper.voice.provider_state.read_barge_in_enabled` (never an os.environ
cache); a runtime guard hard-disables it for the session when the active
profile has no AEC reference (`direct_mic`), to avoid self-tripping on
un-cancelled TTS bleed. Step 4 — **provider cancel/truncate** — is
deliberately *not* wired yet: a real-time provider may resume speaking
after the local flush until that increment lands. Off-device validation
cannot exercise false-barge from TTS bleed; that is a hardware step.

## Codebase Validation

Rechecked against the current tree on 2026-06-01:

- `jasper/audio_io.py` + `TtsPlayout` is the migration boundary for
  assistant audio. The voice daemon expects a small operation set to keep
  its semantics: `write_segment()`/`write()`, `end_segment()`,
  `flush()`, `expected_drain_at()`, and `wait_drained()`.
- `jasper/voice/turn_playback.py` + `_play_responses` races each TTS
  write against the turn's interrupt event — set by a provider
  server-interrupt or, with barge-in enabled, by the daemon's local
  `request_local_interrupt()` — and calls `flush()` (via the shared
  `_flush_for_interrupt`, which emits `event=barge.flush_failed` on a
  failed flush rather than crashing the turn). When `barge_in_enabled`,
  the same race also covers the `wait_drained()` drain-tail window; with
  it off the function is byte-identical to its pre-barge-in shape.
  `_idle_watchdog` and `jasper/voice_daemon.py`'s `_end_turn` rely on
  `expected_drain_at()` to
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
- Content input: on ring-eligible stereo boxes the product default is
  `JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring`; CamillaDSP writes Ring B through
  `jts_ring_playback`, and outputd reads `/dev/shm/jts-ring/content.ring`
  one 128-frame slot per DAC period. The legacy/fail-safe `direct` bridge
  still reads `outputd_content_capture`, backed by snd-aloop substream 6
  (`hw:Loopback,1,6`), for ring-ineligible, operator-frozen, and active-N-ch
  paths.
- Lab bridge: the opt-in `rate_match` mode keeps the DAC as timing owner, drains
  `outputd_content_capture` into an explicit bounded ring, and renders
  DAC-sized periods through a ppm-clamped windowed-sinc rate matcher.
  Use this for DAC/content-lane clock-slip validation only until it has
  passed long jts3 soaks; it is not a broad DAC abstraction.
  Default lab settings add 4096 frames of content latency (~85 ms at
  48 kHz) while leaving direct TTS/cue playout on the normal outputd
  path. The sinc table is precomputed at startup; steady state should
  be multiply/add work only, but Pi 5 CPU and xrun behavior still need
  hardware soak before enabling it outside the lab.
- Multi-room round-trip content lane (OFF by default, inert until a
  grouping bond activates it in Increment 5): when
  `JASPER_OUTPUTD_DAC_CONTENT_FIFO` is set, a grouping leader feeds its
  DAC from that raw-PCM FIFO (the member round-trip written by a
  localhost snapclient) instead of `outputd_content_capture`, picking
  one channel of the shared stereo program via
  `JASPER_OUTPUTD_DAC_CONTENT_CHANNEL` (`stereo`/`left`/`right`/`mono`/`sub`).
  `sub` is the wireless-subwoofer pick: outputd mono-sums the program
  (clip-safe) then applies a 4th-order Linkwitz-Riley **low-pass** at
  `JASPER_OUTPUTD_DAC_CONTENT_SUB_HZ` (default 80 Hz, the reconciler emits it
  only for a `sub` member) before the DAC — the one place outputd does spectral
  DSP, deliberately, because the dumb-follower lane bypasses CamillaDSP (the
  brainy-sub-via-CamillaDSP home awaits the sub lane in the active compiler,
  gap 6a). It is fail-closed: a `sub` never plays full-range on the FIFO path,
  the inv-B fallback (`apply_pick_to_fallback_period`), or a missing filter
  (→ silence). See [HANDOFF-distributed-active.md](HANDOFF-distributed-active.md)
  "Subwoofer — two different subs".
  The complementary half of bass management rides
  `JASPER_OUTPUTD_DAC_CONTENT_HP_HZ`: a main pick
  (`stereo`/`left`/`right`/`mono`) applies a 4th-order Linkwitz-Riley
  **high-pass** at that same corner so the mains shed the low end the sub now
  carries. The reconciler emits it only for a non-`sub` member in a bond that
  has a sub (bass management on); it is fail-closed — an absent/out-of-range
  corner plays the mains full-range, and a `sub` ignores it by construction.
  It falls back to the direct `outputd_content_capture` read whenever
  the FIFO starves, so the leader is never silenced (inv-B). Unset =
  byte-identical to the direct path above. The reference still equals
  what the DAC plays, so AEC is unaffected. Design + invariants live in
  [HANDOFF-multiroom.md](HANDOFF-multiroom.md) §2; this doc owns only
  the outputd knobs. Mutually exclusive with `rate_match` and the
  dual-Apple sink (both fail loud at startup).
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
  `outputd_dac` PCM name. It also writes
  `/run/jasper-output-hardware/output_hardware.json`, a structured observed-hardware
  artifact with the current profile id, status, physical output count,
  Apple child-device facts, and selected card/PCM for single-device
  profiles. `/state` exposes this artifact as `audio.output_hardware`,
  `/sound/output-topology` returns it alongside the topology draft, and
  `jasper-doctor` has a first-line "Output hardware state" check for
  missing, partial, blocked, or ready hardware. Runtime selection remains
  owned by `/etc/jasper/jasper.env` plus `/var/lib/jasper/outputd.env`.
  `jasper-doctor` uses the observed hardware profile to make
  Apple-specific USB/headphone-gain checks active for one Apple dongle or
  the dual-Apple pair, checks every Apple child card in the pair, and
  skips those checks for DAC8x-family or other output roles. For
  recognized DAC8x/DAC8x Studio hardware, `outputd_dac` renders directly to
  the detected final-output card. The old DAC8x route env (`mono:N` /
  `stereo:L,R`) has been removed; active crossover channel ownership lives in
  the active-speaker `channel_map`, not in an ALSA alias. The product
  speaker-output topology substrate is separate again:
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
  Active driver commissioning composes that topology evidence with
  active-speaker environment, safe-session, calibration-level, clock-domain,
  Stop-control, and tone-backend evidence through the commission ramp. The
  topology itself still grants no playback authority; the separate generic
  `aplay` backend is lab-only and requires explicit enablement plus a dedicated
  non-daemon test PCM.
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
  when the active-speaker runtime contract proves the already-loaded endpoint
  graph targets `outputd_active_content_playback` and its width fits the
  profile cap.
  Until that graph evidence is present, the observed output-hardware
  profile remains dual Apple for UI/diagnostics, but the runtime DAC role
  is parked with `JASPER_OUTPUTD_BACKEND=fake` and logs
  `event=audio_hardware_reconcile.dual_apple_detected action=park_until_active_graph`.
  Active-speaker load and rollback trigger the reconciler once, matching
  the existing udev/install triggers without adding a poller. This is not
  a generic endorsement of ALSA
  `multi`/`dmix`/`plug` or CamillaDSP multi-device output.
  The configured output role takes effect when deploy, boot/udev reconcile, or a
  manual `jasper-audio-hardware-reconcile` run re-renders the managed
  ALSA template; hardware validation artifacts report the observed
  output identity in `dac_identity`. A recognized role renders the ALSA template first,
  then publishes the active DAC env values. If the env/template changed, the
  reconciler restarts `jasper-outputd`; if the replug is value-neutral, it
  still `reset-failed` + `start`s `jasper-outputd` so a unit parked by the
  missing-card `ExecCondition` recovers when the DAC returns. An
  unknown/no-output role does **not** render `outputd_dac` to a guessed card;
  it writes `JASPER_OUTPUTD_BACKEND=fake` so outputd keeps its sockets and
  `/state` surface alive without opening ALSA, and stops `jasper-voice` plus
  any stale outputd instance so final-output ALSA ownership cannot keep running
  against removed hardware or burn the outputd reboot escalation budget.
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
  outputd statefile is selected by
  `jasper.active_speaker.runtime_contract`, via
  `jasper-active-speaker runtime-safe-graph`, after deploy copies the
  packaged configs. That boundary reads the saved
  `jasper.output_topology` contract before choosing a fallback: an absent
  saved topology or explicit stereo full-range/passive topology may use the
  flat outputd graph, but explicit mono full-range topology cannot be driven
  by a wider flat stereo graph, and any topology with a tweeter, protected
  output, or subwoofer roleful assignment must preserve/select a matching
  all-muted active startup graph. Guarded commissioning graphs are active test
  surfaces, not persisted boot/deploy fallbacks. If no legal guarded graph
  exists, install and recovery helpers fail closed rather than repointing
  Camilla at flat stereo. `jasper-doctor` uses the same runtime classifier and
  reports a failure when a saved tweeter/protected topology is running
  `outputd-cutover.yml`, `v1.yml`, or another flat full-range graph.
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
- TTS interruption: the active TTS IPC flush is epoch-based. In the
  packaged topology this is fan-in's TTS socket. A flush advances the
  TTS epoch, clears the already-enqueued assistant buffer, and ignores
  any pre-flush audio commands that had been accepted onto the bounded
  IPC queue but not yet mixed. That keeps barge-in from resurrecting
  stale assistant audio after the interrupt path returns. Python uses a
  synchronous `FLUSH_SYNC` path for interruption and bounds this ack
  wait; on timeout it closes the ordered TTS socket so a late stale ack
  cannot be mistaken for a later flush.
- Playout ledger: outputd's core carries the DAC-clock-true per-segment
  ledger (provider item id, flushed frames, and `audio_played_ms` estimated
  from the output clock and DAC delay rather than treating written frames as
  heard frames), used on a bonded multiroom member where assistant audio
  mixes at the final output stage. In the solo packaged topology the
  assistant path is owned by `jasper-fanin` (pre-CamillaDSP), which now
  carries its OWN per-segment playout ledger
  ([`rust/jasper-fanin/src/playout.rs`](../rust/jasper-fanin/src/playout.rs)):
  the `FLUSH_SYNC` ack reports per-segment provider item id, queued/flushed
  frames, and a real `max_audio_played_ms` plus `events[]` — replacing the
  former hardcoded `max_audio_played_ms=0` / `events=[]`. Because fan-in
  cannot see the DAC clock, its `audio_played_ms` is the MIX-COMMIT count:
  frames popped into the program toward snd-aloop, paced by the blocking
  snd-aloop write and therefore DAC-rate-paced, NOT a queued-frame estimate.
  It over-reads true acoustic playout by the FIXED downstream pipeline depth
  (CamillaDSP + the snd-aloop rings + outputd's content ring + the DAC
  buffer/hw delay), which is the conservative direction for truncation;
  closing that offset to exact DAC-clock precision (subtracting outputd's
  reported DAC delay) is the remaining follow-up. Robust provider truncation
  therefore now needs the provider adapters wired to CONSUME this
  acknowledgement.
- Runtime unit: `deploy/systemd/jasper-outputd.service` is enabled by
  `deploy/install.sh` and sets the ALSA/socket defaults.
  Optional lab retuning belongs in `/var/lib/jasper/outputd.env`; the
  unit loads it after the packaged defaults, and the AirPlay renderer
  reads the same file when deriving backend latency offset.
  Startup is also gated by the reconciler-owned final-output card:
  `JASPER_OUTPUTD_BACKEND=fake` passes because it opens no ALSA device,
  an empty card passes for composite/parked shapes, and an `alsa` backend
  with a configured card missing from `/proc/asound` logs
  `event=outputd.output_device_gate.park reason=missing_dac` and parks
  inactive instead of restart-looping into the reboot escalation. DAC
  arrival through the audio-hardware reconciler un-parks that state with
  an idempotent `reset-failed` + `start` even when the env values are
  unchanged.
  If outputd cannot stay up after its restart burst, systemd reboots
  cleanly via `StartLimitAction=reboot` rather than leaving the speaker
  without its final-output owner.
  During install, likely audio clients (`jasper-voice`,
  `jasper-aec-bridge`, outputd, camilla#2, Snapcast, AirPlay, Spotify,
  Bluetooth aplay, and the mux) are parked before fan-in/Camilla/outputd
  restart so old graph owners cannot keep legacy or current ALSA endpoints
  open. The AEC, grouping, and renderer restart steps then restore the
  appropriate runtime state. The installer treats missing outputd source or
  binary as fatal; a transient outputd STATUS miss is logged loudly and
  rechecked by the doctor summary so nginx, `/system/`, and recovery
  surfaces still get installed.
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
  Outputd also exposes passive DAC/chip-reference timing diagnostics
  for AEC bring-up: `dac.snd_pcm_delay_*` and
  `reference_outputs.chip_ref_writer.*` report DAC presentation delay,
  chip-ref queue depth, ALSA write progress/delay, drop/xrun/recovery
  counts, and reference-sequence lag. Details and field units live in
  [AEC-DIAG-02-observability.md](AEC-DIAG-02-observability.md). The
  optional `JASPER_OUTPUTD_CHIP_REF_TEE_PATH` raw-sample tee is
  diagnostic only, should point under `/run/jasper-outputd` or
  `/var/lib/jasper` in the packaged systemd sandbox, and must not be
  treated as a production reference path.
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
- Provider truncation is wired for the OpenAI/Grok pack (PR-4). The local
  TTS flush PRODUCES the `audio_played_ms` and provider item identity
  (fan-in solo + outputd bonded), and `turn_playback._flush_for_interrupt`
  now drives the active provider's adapter to CONSUME that acknowledgement —
  `response.cancel` then `conversation.item.truncate(audio_end_ms=played-ms)`,
  a no-op + WARN when the ledger reports `max_audio_played_ms=0` so it never
  truncates on bytes-received. The contract is documented here and in
  `HANDOFF-voice-providers.md`. Remaining: Gemini's obey-`interrupted` pack
  (PR-5; it self-truncates server-side, so no client truncate), Grok
  verification (PR-6), and the on-hardware AEC gate before default-on (PR-7).
- The latest TTS ledger refinements (provider item id over IPC,
  synchronous flush acknowledgement, and DAC-delay-based drain
  accounting) still need Pi validation after an operator-approved
  deploy.

## DAC-agnostic active-output transport (design-of-record)

> **Status: design-of-record, 2026-06-17 — Rust transport cleanup mostly built;
> hardware verification pending.** Finalized after a multi-agent design pass
> (3 architects + 6 adversarial critics) and an external hardware-grounded
> review. The Stage-7 outputd cleanup now routes single ALSA and paired composite
> through one `run_alsa` loop; Linux/ALSA and dual-Apple hardware regression are
> still the required proof. This is the canonical transport design for active
> crossover; the commissioning flow that rides it is in
> [HANDOFF-active-speaker-dsp.md](HANDOFF-active-speaker-dsp.md) "Single audio
> path commissioning". The principle that governs every line below: **dispatch
> on clock-domain *shape* (single coherent device vs paired composite), with
> channel width and the channel map as DATA from the `DacProfile`/topology — never
> a per-DAC code branch.** Adding a DAC of an established shape is a `DacProfile`
> row; a new shape pays transport code once.

### One path, and why outputd stays in it

`fan-in (stereo) → CamillaDSP (the sole 2→N width fan-out) → outputd (reads the
N-channel content lane, demuxes to physical DAC channel(s), publishes the AEC
reference) → DAC`. CamillaDSP owns all width/EQ/gain/delay/limiter authority
(`emit_active_speaker_*` already sizes `playback channels: {output_count}`).
outputd stays the final owner because (a) a *composite* DAC needs an aggregator
that CamillaDSP — which targets one ALSA device — cannot be, and (b) outputd
owns the AEC reference, the playout ledger, and clip accounting. **TTS/cues are
NOT an outputd concern in active mode:** they enter at fan-in (stereo,
pre-CamillaDSP — `jasper-voice.service` `JASPER_TTS_OUTPUTD_SOCKET` →
`/run/jasper-fanin/tts.sock`), so voice rides the crossover/protection chain at
every width. The active loop therefore needs **no** TTS lane; `OutputCore`/the
TTS mixer stays conditional on `tts_socket_path` being set and is fail-closed to
stereo single-ALSA output. The old dual/composite loop gap was **real clip
accounting** (it hardwired `clipped_samples=0`) plus sharing the same
reference/state path as single ALSA; the Stage-7 cleanup moved composite into
the unified `run_alsa` loop so both sink shapes now record the written period's
full-scale sample count.

### The transport debt this change paid down

The original active-lane transport, `DualAppleBackend`
(`rust/jasper-outputd/src/alsa_backend.rs`), was welded to two stereo USB DACs:
two child PCMs, `snd_pcm_link`, inter-DAC drift tracking,
`deinterleave_4ch_to_dual_stereo` (ch0/1→DAC A, ch2/3→DAC B). Stage 1 renamed the
shape to `PairedCompositeSink` and `SinkMode::Composite` while keeping the
`dual_apple` wire value stable; Stage 7 removed the separate runtime loop and
wrapped `PairedCompositeSink` behind `RuntimeAlsaSink` beside the coherent single
`AlsaBackend`. The pair remains exactly two children. M>2 composite output is
still out of scope.

### The change set (build to this)

**1. Transport dispatches on clock-domain shape via a small runtime sink
boundary.** One loop body serves both widths; both get the state + reference +
clip path:

```rust
enum RuntimeAlsaSink {
    Single(AlsaBackend),
    Composite(PairedCompositeSink),
}

impl RuntimeAlsaSink {
    fn content_channels(&self) -> u16;
    fn read_content_period(&mut self, out: &mut [i16]) -> Result<usize>;
    fn write_period(&mut self, samples_nch: &[i16]) -> Result<()>;
    fn start(&self) -> Result<()>;
    fn dac_delay_frames(&self) -> Result<u64>;
    fn mark_runtime_status(&self, state: &OutputdState);
}
```

- `AlsaBackend` = today's single backend with the **DAC-write** `CHANNELS=2`
  literals replaced by runtime `dac_channels` at the enumerated write sites only
  (`alsa_backend.rs` open + `write_dac_period` framing). Content-read framing
  follows the same runtime width. **Width 2 is byte-identical to today.** Covers
  single Apple (2ch), DAC8x (8ch), any future coherent single DAC — zero per-DAC
  code.
- `PairedCompositeSink` = the renamed dual-Apple transport behind the same
  boundary. It keeps the existing A/B child PCM env, `snd_pcm_link`,
  delay-divergence guard, and fail-closed runtime-health behavior. **Stays
  two-child** — a pairwise drift guard cannot be half-`Vec`-ified. M>2 composite
  is a genuinely *new* sink impl (explicitly out of scope; named in the
  active-speaker doc), not a config row.
- **No `single_alsa_multi` sink string.** Width is already carried by
  `active_outputd_lane_channels`; a second "is this wide?" field invites drift.
  `config.rs` keeps `SinkMode { SingleAlsa, Composite }` (rename `DualApple` →
  `Composite`; keep `"dual_apple"` as a parse alias one release). `dac_channels`
  reads `JASPER_OUTPUTD_ACTIVE_CHANNELS` (validated `2..=8`; **required** for a
  wide single DAC — fail-closed `EXIT_CONFIG`/78 if unset; the reconciler always
  emits it from `active_outputd_lane_channels`). `types.rs CHANNELS=2` stays as
  the reference/content-read/chip width.

The loop body is now a single `run_alsa` over `RuntimeAlsaSink`: read N-channel
content → `write_period` → mark sink runtime status → read DAC delay → publish
the correct reference fold → `state.mark_period(..., clipped)`. The old
`run_alsa_dual_apple` fork and `downmix_dual_active_reference` helper were
deleted in the Stage-7 cleanup.

**2. The AEC reference is mono — verified — so the fold is trivial.** Both
consumers collapse the reference to mono: software AEC3 sums L+R→mono
(`aec_bridge.py` "L+R summed to mono"); the chip-AEC USB-IN producer downmixes
(`main.rs` `chip_ref_downsampler_downmixes_and_decimates`, the XVF USB-IN being a
2ch endpoint fed the downmixed signal — `HANDOFF-xvf3800.md` §3). **No consumer
uses L vs R separately.** Therefore:
- `fold_reference` sums **all driven active lanes** into one mono signal, then
  publishes it into the existing stereo reference (L = R) so the published
  contract (`speaker_reference_channels: 2`) is unchanged and the bridge/chip
  producer are untouched. There is **no per-DAC L/R fold to author** — the driven
  set is derived from the CamillaDSP output channel count / topology (single
  source of truth; see §data-model).
- **Clip-proof scaling.** Scale the sum by **1/N** (N = number of driven lanes):
  N correlated full-scale lanes sum to N×, so 1/N guarantees the result stays in
  range regardless of correlation. (`1/√N` is power-preserving only for
  *uncorrelated* lanes — a woofer+sub share LF, L/R are correlated — so it can
  still clip; a clipped reference is uniquely harmful because the linear AEC
  cannot model the nonlinearity.) Accumulate in `i32`; the AEC adapts its own ERL
  so the lower level costs nothing. The pairwise composite reference path is now
  named `fold_reference_pairwise_composite` and stays byte-identical to the old
  `downmix_dual_active_reference`: `[avg(ch0,ch1), avg(ch2,ch3)]` per frame
  (regression test asserts equality); the N-lane path is the new clip-proof sum.
  *Precondition note:* `1/N` is clip-proof **absolutely** (not relying on
  band-splitting). Band-splitting is why the reference rarely approaches the `N×`
  worst case — at any instant one lane is hot and the sum stays well below
  full-scale — so `1/N`'s conservatism costs no real SNR. Do **not** "optimize"
  back to `1/√N`: it is power-preserving only for uncorrelated lanes and would
  reintroduce the clip hazard the moment a future mode routes full-range content
  to multiple lanes.
- **Match the fold to what the mic can hear (don't normalize on inaudible
  energy).** A reference dominated by sub energy the mic can't pick up inflates
  the NLMS denominator without contributing correlation, slowing convergence in
  the voice band. The software AEC3 path **already** high-passes the reference at
  125 Hz (`aec_bridge.py`), so sub content is already out of *its* denominator;
  the open item is the **chip** path — verify the XVF3800 USB-IN reference band
  and the mic-array low-frequency roll-off, and high-pass the fold to match mic
  sensitivity if needed. The XVF exposes only **2** reference channels (not 3),
  so a separate sub reference is impossible — this is a "what goes into the sum"
  question, which is why the mono fold is the right shape.

**3. Reconciler computes one `OutputTransportPlan`; it dispatches on `kind`, not
DAC id.** `apply_audio_runtime_env()` reads the resolved `DacProfile`:
- `kind == "single"` with an active lane → `JASPER_OUTPUTD_SINK=single_alsa`,
  `JASPER_OUTPUTD_ACTIVE_CHANNELS=<active_outputd_lane_channels>`,
  `CONTENT_PCM=outputd_active_content_capture`, `DAC_PCM=outputd_dac`. No
  child-PCM env, no composite policy.
- `kind == "composite"` → `SINK=composite` + the child PCMs from
  `dac_channel_map`; the existing `apply_observed_composite_policy` (serial-pinned
  A/B order, drift evidence) runs **only here**.

The `OutputTransportPlan` (`sink`, `transport_channels`, `channel_map`,
`dac_pcms`, `clock_domain_contract`) is the **single env+`/state` truth**,
computed once and *read* on `/state` — never re-derived per `/state` hit.

**Stable identity + invalidation (a Stage-0 decision, not a later fix).**
`dac_pcms` and `clock_domain_contract` are exactly the fields that shift when a
USB DAC re-enumerates or the Apple dongles return with different card indices
across a reboot — a class of drift that has bitten JTS before
([HANDOFF-identity.md](HANDOFF-identity.md)). So the plan MUST key on a **stable
card identifier** — `hw:CARD=<name>` (the `DacProfile` already matches on card
*name* regex, not index), or a serial where available — **never a numeric card
index.** With `type plug` banned, a stale plan pointing at the wrong device now
fails *closed* (silent until reconcile re-runs) rather than playing remixed
content at the wrong drivers — but the cure is to not go stale: the reconciler
(the single writer) recomputes the plan on **boot and on udev add/remove/change**,
the same triggers it already self-heals on. Bake stable-identifier resolution
into the Stage-0 resolver relocation onto `OutputTopology` — that is the cheapest
place to get it right and the most expensive to get wrong later.

> **Stage 0.3 landed (Python data model + stable identity).** `OutputLayout` /
> `OutputTransportPlan` + `resolve_output_layout` live in
> [`jasper/output_topology.py`](../jasper/output_topology.py); the active-speaker
> resolvers and `ActivePlaybackRouteCapability` are thin readers of them. Every
> physical-DAC PCM is built by the single `stable_card_pcm` chokepoint
> (`hw:CARD=<name>`), and `is_stable_card_pcm` rejects numeric-index / `plug` /
> `plughw` forms at the `OutputTransportPlan` boundary, so the card-index drift
> class fails closed before the Rust transport (Stage 1) and the reconciler env
> emission (Stage 2) ride the plan. The plan is recomputed fresh from the topology
> per call (no cached index); wiring the *udev/boot env emission* of it is Stage 2.

> **Stage 2a landed (reconciler env + wide content lane + width gate + DAC8x
> profile flip).** `jasper-audio-hardware-reconcile`'s `apply_audio_runtime_env`
> emits the wide single env (item 3) — `JASPER_OUTPUTD_SINK=single_alsa`,
> `JASPER_OUTPUTD_ACTIVE_CHANNELS=<active_outputd_lane_channels>`,
> `JASPER_OUTPUTD_CONTENT_PCM=outputd_active_content_capture` — for a recognized
> coherent single DAC **only when the active-speaker runtime contract proves the
> already-loaded endpoint graph**. For solo active that endpoint is the graph in
> `outputd-statefile.yml`. For an active leader, `outputd-statefile.yml` may be
> the safe `program_bake_pipe` (`File`→`SNAPFIFO`, not a DAC); in that case the
> gate follows `crossover-statefile.yml` and requires the camilla#2 graph to be a
> re-proven `driver_domain_baseline` targeting `outputd_active_content_playback`.
> Any missing/unsafe/wrong-device/over-cap paired graph fails closed to the
> byte-identical stereo path. The gate **drives what we use**: it reads the live
> endpoint config's actual playback width W, accepts `2 ≤ W ≤ cap`
> (`active_outputd_lane_channels`), and emits **that W** as
> `JASPER_OUTPUTD_ACTIVE_CHANNELS` (a managed var cleared in every non-active
> branch). A DAC8x running a 2-way drives 2 outputs, an 8-driver speaker drives
> 8 — outputd opens the DAC at W. The active content lane (item 4) is raw
> `type hw` — card/device/subdevice only, exactly like the `outputd_dac` block;
> the ALSA `hw` plugin rejects `channels`/`rate`/`format` as unknown fields, so
> the width is set by the openers and locked by snd-aloop, with
> `type plug`/`plughw:` banned. The DAC8x/DAC8x-Studio `DacProfile`s declare the
> active lane (item 6, `supports_active_outputd_lane=True`; the Apple USB-C
> dongle declares `active_outputd_lane_channels=2`, DAC8x/DAC8x-Studio declare
> `8`, and the Stage 1 transport carries any width ≤ cap). Because the gate
> accepts the config's actual width, the existing
> per-speaker emitters (which emit the driver count) engage active mode directly
> — **no full-width-padding producer is needed.** **Load-bearing hardware fact
> (verify on jts3 at Stage 3/4):** outputd opening the DAC at W < its physical
> channel count must succeed and idle the undriven outputs safely; if a future
> DAC requires native-width opens, that becomes a per-DAC `DacProfile` property,
> not a reason to pad universally. **2b landed:** the masked commissioning emitter
> is wired into staging — `stage_protected_startup_config` stages the production
> graph with `audible_outputs=frozenset()` (the all-muted boot config), the
> software guard proves the tweeter is muted via its per-output
> `as_out{idx}_commission_mute`, and a `staged_candidate_fully_muted` gate
> enforces crash-recovery-MUTED on every staged boot config — see
> [HANDOFF-active-speaker-dsp.md](HANDOFF-active-speaker-dsp.md)
> critical-path step 2.

**4. One wide snd-aloop content substream — width on the substream, not more
substreams.** The kernel caps loopback substreams at `MAX_PCM_SUBSTREAMS=8` (you
cannot raise that without patching the module); but one substream carries up to
`channels_max=32` and adopts the playback side's channel count. So the fix is to
make the active content lane **one substream at width N**, not to add substreams:
- Render the active-content lane width from `JASPER_OUTPUTD_ACTIVE_CHANNELS`
  (`__OUTPUTD_ACTIVE_CONTENT_CHANNELS__` token in `asoundrc.jasper`).
  *(2a implementation note: the ALSA `hw` plugin rejects `channels`/`rate`/
  `format` as unknown fields, so this token approach was abandoned — the active
  lane is plain `type hw` card/device/subdevice and the width is set by the
  openers + locked by snd-aloop, NOT pinned in the conf. See the "Stage 2a
  landed" callout above.)*
- **All format adaptation is explicit and owned by CamillaDSP; the active ALSA
  path fails closed on channel, rate, AND format mismatch.** Ban `type plug`
  (and `plughw:`, which is `plug`+`hw`) on the active path — use width-exact
  `hw:`/`dmix` so any mismatch fails at `snd_pcm_hw_params` (open error) instead
  of silently remixing 8→4 (or resampling, or reformatting) onto live drivers
  (the single most dangerous fail-open in the feature; `plug` is the automatic
  channel/rate/format-conversion plugin). A contract test rejects `plug`/`plughw`
  anywhere on the active path.
- **Second, independent fail-closed layer:** CamillaDSP refuses to start if its
  mixer output channel count ≠ the playback device `channels`. Rely on both.
- **Open-ordering constraint:** snd-aloop locks rate/format/channels to the first
  opener of a substream pair, so the active-content playback (CamillaDSP) and any
  reference capture on the paired substream must agree on width; document the open
  order. If PipeWire/PulseAudio is present it may grab the loopback device —
  out of scope for the appliance, noted for dev hosts.

**5. Width-aware cutover gate — drive what we use, not the DAC's full width.**
Replace the `channels: 4` grep with a capacity check: the active config's
**actual** playback width W must be a valid active width **within the DAC's
cap** (`2 ≤ W ≤ active_outputd_lane_channels`), and the reconciler emits that
**actual W** as `JASPER_OUTPUTD_ACTIVE_CHANNELS` so outputd opens the DAC at
exactly W. `active_outputd_lane_channels` is the **cap** (the most outputs
outputd will drive on this DAC), not a fixed width — a 100-output DAC powering a
2-way drives 2, never 100. This matches the `<=` model
`ActivePlaybackRouteCapability` already uses (`required_active_output_count <=
transport_channel_count`); a config wider than the cap fails closed. Renamed
`active_four_channel_shape_missing` → `active_graph_width_out_of_range got=W
cap=N`. (Earlier drafts used a fixed `== transport width` gate that would have
forced narrow speakers to pad to the DAC's full channel count with muted lanes;
rejected — see the "Stage 2a landed" callout above.)

**6. `DacProfile` additions (pure data, IO-free, fail-closed at import).**
- `dac_channel_map: tuple[ChannelMapEntry, ...] | None` — `(camilla_out_index,
  physical_dac_channel)` permutation. **No gain field** (CamillaDSP owns gain).
- `is_coherent_single() -> bool` predicate (folds `kind=="single" and
  coherent_clock_domain`). **Device resolvers move to `OutputTopology`**, not onto
  the IO-free registry (they read env + card_id, which would break `dac.py`'s
  contract). No `reference_fold` field — the driven-channel set is the fold input
  and is derived from the topology/CamillaDSP output, validated against the
  active lane width at import.
- **Profile flip is last:** set `supports_active_outputd_lane=True` +
  `active_outputd_lane_channels` on a DAC only once the transport above can carry
  it, so the Python route never resolves a lane the transport can't serve.

### Resilience (every failure: detect → fail-closed → observable)

- **Composite child loss:** `sink.health()` checked **before** the write; a
  non-Running child is a hard fault → mute **all** children, `event=outputd.
  composite.child_lost`, `/state.composite.children[].state`. The reconciler/
  ExecCondition gates a composite on **all** child cards present.
- **Unified xrun policy:** `write_period` uses bounded per-child `try_recover`
  (mirror the single path), bailing only on recovery exhaustion or delay
  divergence — never the dual path's bail-on-first-xrun (which reboot-loops via
  `StartLimitAction`).
- **Width mismatch (CamillaDSP N vs outputd M):** `EXIT_CONFIG`/78, parked, no
  crash-loop. Belt-and-suspenders since both derive from one `OutputTransportPlan`.
- **DAC hotplug:** reconciler re-derives on udev (pattern-3 self-heal); replug
  re-arms **muted** via the masked startup config.
- **Config-shear during DAC re-enumeration:** the reconciler stages and validates
  `outputd.env` buffer/period pairs (content and DAC buffers) before replacing
  the prior file. If outputd still exits 78 from a transient hotplug shear, the
  failure helper runs one bounded
  `jasper-audio-hardware-reconcile --no-restart` pass and no-block retries
  outputd; a repeated exit 78 parks instead of looping into reboot policy.

### Observability

- **Real clip accounting at every width** — the Stage-7 cleanup removed the
  hardwired `clipped_samples=0` composite path. A clipping active period now
  reports nonzero on `/state` for single and paired-composite output alike (the
  commissioning "no clip" gate is otherwise vacuously green).
- **Width-agnostic `/state` block — decouple the wire string from the Rust type
  name.** The serialized `/state` value is a cross-language contract (Rust
  `state.rs` writes it; the Python doctor reads it). Renaming the internal type
  (`DualAppleBackend`→`PairedCompositeSink`, `SinkMode::DualApple`→`Composite`)
  must **not** be coupled to a serialization-format break. Either keep the wire
  value stable while the type is renamed, or migrate every occurrence in one
  atomic commit guarded by a **round-trip (serialize→parse) test**; either way
  rename the block to a width-agnostic `composite` shape with a per-child array
  and keep `dual_apple` as a **read alias** one release, migrating the doctor's
  `=="dual_apple"` branches + the snapshot test in the same PR.
- **"Why didn't my lane arm" via stable `issues[].code`** on `OutputHardwareState`
  (not bare stderr). `check_outputd_service` becomes table-driven keyed by
  `sink_mode` (today's 2-mode allowlist would FAIL every new DAC); the width check
  diffs reconciler-resolved width against outputd's negotiated `dac.channels`.
- **Edge-triggered hot-loop events with `*_count` companions** — no per-period
  logging in the sink loop (a flapping child must not emit 48000 lines/sec).

### Performance / resource (1 GB Pi)

- **Zero allocation on the DAC-write hot path at any width** — preallocated
  per-child period buffers; preallocated fold scratch. Test mirrors
  `steady_state_reuses_segment_write_buffer_capacity`.
- **`OutputCore`/`ReferenceFanout`/ledger-loudness stay conditional on TTS** — a
  solo stereo speaker allocates none of it; the minimal clip/ledger counters the
  active loop needs are cheap scalars, not the full `OutputCore`.
- **Composite drift-sync cost gated to composite** — `SingleAlsaSink` pays
  nothing; M=2 keeps exactly today's 2 `snd_pcm_delay` ioctls/period.
- **No new threads, no new poll loops, no new resident process** — reconciler
  reuses the existing boot/udev shell-out; N-channel passthrough is O(frames×N).
- **Pi-5 RP1 multichannel I2S headroom:** CamillaDSP-Nch + outputd + voice + AEC
  on one Pi 5 is plausible but not free (documented RP1 XRUNs at high rates under
  load). Budget 48 kHz + a comfortable period; **load-test at Stage 3/6** and
  state the per-SKU channel ceiling (a Pi Zero 2W cannot do Nch DSP + AEC). This
  is monitored on `/system`, not CPU-capped (per the JTS "visibility over
  constraints" stance).

### Safety + verification (jts3, real bi/tri-amp speaker, live drivers)

`volume_limit: 0.0` holds in the active config; per-driver limiters and the
protective tweeter high-pass live in the CamillaDSP graph. Verify on jts3 in the
staged order in the active-speaker doc, starting muted, unmuting one output at the
calibration floor woofer-first/tweeter-last, with a **live high-pass-presence
assertion** before the tweeter is unmuted. Single-DAC has no drift/link to soak;
the stereo↔active cutover and xrun behavior do.

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
  putting side work in the synchronous graph completion path. The
  multi-room **snapfifo** consumer (a grouping leader's post-clamp tap
  to `snapserver`, `rust/jasper-outputd/src/snapfifo.rs`) is a new
  instance of exactly this contract — a separate bounded, drop-on-full
  side reader handed to a dedicated FIFO-writer thread, never in the DAC
  path; off-by-default, design in
  [HANDOFF-multiroom.md](HANDOFF-multiroom.md) §2.
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
5. **Enable robust barge-in.** Wire the local TTS flush and final
   playout-ledger acknowledgement to provider truncation/cancel logic,
   capture barge-in telemetry, and use the "volume down while assistant
   is speaking" path as the first product acceptance test.

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
- Barge-in test: assistant speaks, user interrupts, the local TTS path
  flushes, and provider truncation receives an `audio_played_ms` within
  one output period of the final playout-ledger estimate.

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
- 2026-06-17: Removed the explicit DAC8x-family `JASPER_OUTPUT_DAC_ROUTE`
  render path. `outputd_dac` renders directly to the detected final-output
  card; active-speaker per-driver ownership lives in the saved topology and
  protected active graph.
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
- 2026-06-09: `jasper.output_topology` now consumes
  `jasper.audio_hardware.dac` for known DAC labels, physical output counts,
  clock-domain labels, and clock-domain reports. This makes dual Apple a known
  four-output topology shape with a measured-sync-required clock contract,
  distinguishing profile shape from aggregate runtime enablement and leaving
  runtime activation with hardware reconcile/outputd.
- 2026-06-09: Added `jasper.output_hardware` and
  `/run/jasper-output-hardware/output_hardware.json` as the observed output hardware state
  contract. `jasper-audio-hardware-reconcile` writes the artifact during
  install/boot/udev convergence, publishes outputd runtime env separately,
  and parks dual Apple with the fake backend until outputd has a real
  four-channel active graph. `/state`,
  `/sound/output-topology`, and `jasper-doctor` now read the same artifact.
- 2026-06-08: Retired outputd's then-disabled TTS IPC implementation after
  rollback no longer needed it. At that point fan-in was the sole
  production TTS/cue IPC owner; outputd owned final electrical output and
  monitor/reference fanout. Dual-Apple outputd activation was also
  graph-gated: hardware observation alone records the composite profile,
  but outputd switches to the four-channel sink only after the
  active-speaker startup config is loaded and CamillaDSP's outputd
  statefile points at that active graph.
- 2026-06-09: Documented the current robust barge-in contract:
  local speech detection triggers the active TTS transport flush first,
  the final playout ledger supplies `audio_played_ms`, and provider
  adapters reconcile conversation state after the local audio stop.
  First acceptance target is interrupting assistant speech with a local
  volume command and no second wake word.
- 2026-06-11: Multiroom Increment 5 PR-2 reintroduced an outputd TTS
  socket only for active bonded members. Solo stays fan-in-owned, while a
  bonded member mixes its own assistant audio in outputd after the
  snapcast round trip and before reference publication.
- 2026-06-21: Wired the solo fan-in TTS `FLUSH_SYNC` ack to a real
  per-segment playout ledger (`rust/jasper-fanin/src/playout.rs`),
  replacing the hardcoded `max_audio_played_ms=0` / `events=[]`. fan-in is
  pre-CamillaDSP and cannot see the DAC clock, so its `audio_played_ms` is
  the mix-commit count (frames committed to the snd-aloop program,
  DAC-rate-paced) and over-reads true playout by the fixed downstream
  pipeline depth — the conservative direction for truncation. Exact
  DAC-clock precision (subtracting outputd's reported DAC delay) and the
  provider-adapter consume side remain follow-ups.

Last verified: 2026-07-07 (ring/default outputd bridge text rechecked against
`jasper.fanin_coupling`, `jasper.fanin.coupling_auto`, and
`jasper.fanin.coupling_reconcile`; prior 2026-07-06 outputd config-shear
resilience rechecked against
`jasper.audio_runtime_plan`, `jasper.cli.audio_config validate-outputd-env`,
`deploy/bin/jasper-audio-hardware-reconcile`, and
`deploy/bin/jasper-outputd-failure-reconcile`, including content and DAC
buffer/period validation; Camilla/outputd install choreography
previously rechecked
against `deploy/lib/install/systemd-units.sh` and
`deploy/bin/jasper-camilla-recover`; 2026-06-24 active-endpoint and
wireless-sub TTS route exceptions rechecked against
`jasper.multiroom.tts_route.expected_grouping_tts_route`,
`jasper.multiroom.reconcile.outputd_grouping_env`,
`jasper.multiroom.reconcile.voice_grouping_env`, and
`jasper.cli.doctor.grouping`; fan-in solo `FLUSH_SYNC` playout-ledger ack
previously verified against rust/jasper-fanin/src/{playout,tts}.rs;
active-speaker runtime graph boundary rechecked against
`jasper.active_speaker.runtime_contract`,
`outputd_active_lane_decision`'s paired active-leader statefile proof, install
outputd-statefile selection, doctor runtime graph check, `resolve_output_layout`,
and the active-lane `DacProfile` declarations; Stage-7 outputd loop unification previously
rechecked against rust/jasper-outputd; solo fan-in TTS ownership and
passive bonded-member outputd TTS ownership previously rechecked against
rust/jasper-outputd and HANDOFF-multiroom; voice playback seam path
rechecked after `jasper/voice/turn_playback.py` extraction).
