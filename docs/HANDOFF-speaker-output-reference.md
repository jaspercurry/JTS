# Handoff: speaker output reference architecture

This doc answers one question:

> What signal does JTS treat as "what the speaker actually emitted," and how do
> playback, AEC, wake/corpus telemetry, and realtime-model turn-taking consume
> that signal?

Read it before changing TTS routing, AEC reference routing, `jasper-outputd`,
or Camilla output wiring. Decisions live in ADRs, not here:
[ADR-0114](adr/0114-a-jts-native-output-owner-not-pipewire.md) (a JTS-native
output owner, not PipeWire, and not a fan-in content mirror) ·
[ADR-0115](adr/0115-provider-truncation-is-driven-by-the-playout-ledger.md)
(provider truncation rides the playout ledger). The design pass behind them,
including the transport change set and the dated decision record, is
[historical/outputd-design-of-record-2026-06.md](historical/outputd-design-of-record-2026-06.md).

Neighbouring owners — do not restate them here:
[audio-paths.md](audio-paths.md#adding-a-new-music-source) (source lanes) ·
[HANDOFF-aec.md](HANDOFF-aec.md) (AEC engine and tuning) ·
[HANDOFF-barge-in.md](HANDOFF-barge-in.md#implementation-plan) (per-provider
truncation status) ·
[HANDOFF-audio-graph-consolidation.md](HANDOFF-audio-graph-consolidation.md)
(the rings, the ACTIVE-ring arm/rollback ladder, program-wire width) ·
[HANDOFF-multiroom.md](HANDOFF-multiroom.md) (the bonded round-trip lane) ·
[HANDOFF-hotplug-resilience.md](HANDOFF-hotplug-resilience.md) ·
[HANDOFF-active-speaker-dsp.md](HANDOFF-active-speaker-dsp.md) (commissioning).

## Current operational truth

JTS has one final output owner:

```text
MUSIC / CONTENT
  AirPlay / Spotify / Bluetooth / USB / correction
    -> private snd-aloop lanes
    -> jasper-fanin
    -> pcm.jasper_capture
    -> jasper-camilla
    -> Ring B (low-latency route), or a paired ALSA playback/capture lane
    -> jasper-outputd
    -> dongle / amp / speaker

ASSISTANT AUDIO
  TTS / cues / chirps
    -> OutputdTtsPlayout
    -> /run/jasper-fanin/tts.sock
    -> jasper-fanin, mixed after program duck
    -> jasper-camilla crossover/protection
    -> Ring B (armed roleful box), or the passive outputd_content_* lane
    -> jasper-outputd final sink
    -> DAC(s) / amp(s) / speaker(s)
```

`jasper-outputd` is the only normal writer to the physical DAC. Active-output
profiles keep the same TTS/cue semantics: a single Apple dongle can run a
width-2 active lane, DAC8x-family profiles up to width 8, and the dual-Apple
composite emits a four-channel active lane outputd splits to two DACs.

### One quantization, at the DAC edge

**Inside `jasper-outputd` the program is `i32`, and there is exactly one
quantization on the output path: at the DAC edge.** Every wire that crosses
outputd's boundary keeps its own declared width and converts exactly once, at
that boundary.

- **Ingress widens** whatever arrives narrow: the snapclient round-trip FIFO
  (a *source*) and the bonded-member TTS socket, whose `jasper-tts-protocol`
  wire stays S16 and is widened at gain application in
  `assistant_source::read_period_into` rather than at enqueue, so a queued reply
  does not double its resident bytes under `mlockall`. **Neither content-lane
  transport is on that list:** the snd-aloop lane arrives at spine width
  (`DEFAULT_PLAYBACK_FORMAT` = `S32_LE`), and so does the SHM ring — its wire is
  resolved by `jasper.fanin_coupling.resolve_ring_wire`, which owns the rule and
  defaults wide. The ring reaches this hop narrow only under the operator
  rollback pin `JASPER_FANIN_RING_WIRE_FORMAT=S16_LE`, which is why the widening
  path stays.
- **Egress narrows** where the wire is narrower than the spine: the `:9891`
  reference datagrams and the chip-reference leg always (both S16 by contract),
  and a paired-composite sink's two children when the registry declares an
  `S16_LE` edge.
- **The final sink converts once**, to whatever width the DAC registry declared.
  `S32_LE` converts nothing. `S16_LE` narrows round-to-nearest (bit-identical to
  the pre-spine path for S16 content at unity gain). `S24_3LE` narrows to 24
  bits and then **packs** three little-endian bytes per sample through a
  separate write path — no `IO<'_, S>` sample type can carry a 3-byte format, so
  the arm stages bytes and writes through alsa-rs's `io_bytes()` while sharing
  the one xrun policy (`write_dac_frames`, whose frame stride is in ELEMENTS:
  bytes on that path, samples on the other two). ALSA's 4-byte-word `S24_LE` is
  a different format and is NOT in the vocabulary.

Float math is **f64**, because f32's 24-bit mantissa cannot carry an i32 sample.
Conversion primitives live in `jasper-resampler` (`widen_i16_to_i32` /
`narrow_i32_to_i16_round` / `narrow_i32_to_i24_round` +
`narrow_i32_to_i24_le_slice`); the truncating `s32_high_word_to_s16` beside them
is UAC2 *capture* semantics and **must never appear on an output path**.

**A composite profile and a child profile may legitimately declare different
widths.** The Apple USB-C dongle declares `S24_3LE` (its USB descriptor
advertises exactly `S16_LE` and `S24_3LE` at 48 kHz/2ch and no 32-bit width),
while the dual-Apple composite declares `S16_LE`, because the paired transport
has no packed-24 child write path — a composite declaring `S24_3LE` is
**refused** at open (park, `EX_CONFIG` 78) rather than silently narrowed. These
are two facts about two armed configurations, not one fact stated twice: the
reconciler resolves `JASPER_OUTPUTD_DAC_FORMAT` **by id** off whichever profile
is armed (`final_edge_format_for` → `by_id`, never walking `child_profile_ids`),
and outputd asks BOTH children for that one value. What would collapse the
divergence is a packed-24 child write path in the paired sink; until then the
registry enforces the narrower invariant — no composite declares a width its
transport refuses.

### The reference contract

The AEC bridge consumes outputd's speaker monitor over localhost UDP. Outputd
publishes the final electrical samples it is about to write, with STATUS
metadata describing the contract:

- `reference_outputs.speaker_reference_source=outputd_final_electrical`
- 48 kHz stereo for software AEC/corpus/diagnostics
- `speaker_reference_active=true` only while a UDP or chip-reference consumer is
  actually active; desired-but-unavailable chip hardware is reported separately
  as `chip_ref_writer.status=degraded`
- for dual Apple active output, stereo monitor left/right are the average of the
  speaker-local low/high driver lanes for each speaker

This is the final software/electrical reference: it includes renderer content,
TTS/cues/chirps, fan-in ducking/gain, CamillaDSP filters/crossover/protection,
and outputd sink selection. It cannot include DAC analog behaviour, amp/driver/
cabinet response, or room acoustics except through the microphone. Chip-AEC
additionally needs the XVF3800 USB-IN reference PCM
(`JASPER_OUTPUTD_CHIP_REF_PCM`) — a hardware actuator separate from the software
monitor. The UDP tap stays at outputd's 48 kHz graph rate; the chip reference is
downsampled to the XVF3800 USB-IN contract (16 kHz, 320-frame periods,
1280-frame buffer).

**Reference fanout is failure-isolated from the DAC path.** An absent or
unopenable XVF3800 reference PCM never fails outputd startup: physical playback
continues, only reference periods drop while unavailable, and the optional
writer retries with bounded exponential backoff. `desired` describes
configuration, `active` describes runtime truth, and a terminal
worker/configuration fault reports `status=failed` rather than pretending a
retry is pending. Playback readiness depends only on the physical sink.

The complete fan-in → CamillaDSP → outputd transport is described once by
`AudioRuntimePlan.TransportTopology`. For ALSA loopback the outputd capture PCM
is **derived** from CamillaDSP's playback PCM by the paired endpoint registry,
never chosen independently; the staged validator rejects a candidate that would
connect an active Camilla writer to the passive outputd reader, and the doctor
applies the same coherence check to the loaded graph and live STATUS (missing
graph evidence is a warning, not false green health).

### Assistant audio

- `OutputdTtsPlayout` preserves the `TtsPlayout` contract — resample to 48 kHz
  stereo, send un-gained PCM plus provider/model/voice metadata to the active
  TTS IPC socket, track expected drain, support `flush()`. In the solo packaged
  topology that socket is `/run/jasper-fanin/tts.sock`.
- **Passive bonded multiroom member:** the grouping reconciler points voice's
  `JASPER_TTS_OUTPUTD_SOCKET` at `/run/jasper-outputd/tts.sock` instead, and
  outputd serves fan-in's exact TTS wire protocol
  (`rust/jasper-outputd/src/tts.rs`), mixing the member's own TTS/cues into the
  post-round-trip `dac_content` lane, **pre-reference**. Music keeps the synced
  path; only assistant audio goes local. Active endpoints deliberately do NOT
  arm this socket (voice stays on fan-in upstream of CamillaDSP so assistant
  audio is crossed over and protected at the endpoint's active width); wireless
  sub followers park voice and keep it unarmed. Canonical:
  [HANDOFF-multiroom.md](HANDOFF-multiroom.md) §6.
- **Volume-context parity.** Fan-in and outputd consume the SAME
  `VolumeContext` through the same `AssistantLoudness` engine; the one
  structural difference is a `MixStage`. `PreDsp` (fan-in) compensates for
  CamillaDSP's downstream gain; `PostDsp` (outputd on a passive grouped
  follower) treats `downstream_db` as **0.0** in every mixer-to-speaker
  conversion, because nothing applies volume after the outputd mix — applying
  fan-in's compensation there would double-compensate by tens of dB. Voice and
  the coordinator publish the same context and never mutate `downstream_db`;
  the structural zero belongs to the post-DSP consumer, selected by
  `JASPER_TTS_MIX_STAGE=post_dsp`.
- **Fail-closed by construction.** Voice embeds the turn-start context
  atomically in `PREPARE_ASSISTANT`; outputd clears any prior context and keeps
  speech silent if that context is absent or rejected, so a restart, read
  failure, or legacy four-field prepare cannot inherit an old unmuted state. It
  emits assistant audio only with a valid turn-start context and applies
  post-DSP compensation only when its engine is explicitly `PostDsp`; a legacy
  socket-only grouping override stays ambiguous and silent. Both ends render
  `tts.assistant_loudness` through one shared writer so the two `/state` shapes
  cannot drift.
- **Cues and chirps** route through the same `TtsPlayout` object, inheriting
  routing, drain, flush, and peak-capped gain policy without training live
  assistant profiles. A feedback sound with no wake-turn context and no measured
  content baseline uses the configured default TTS envelope, not a fixed legacy
  gain.
- **Ducking:** `JASPER_DUCK_TRANSPORT=fanin` sends `PROGRAM_DUCK_ON/OFF` to
  fan-in, which attenuates renderer/program lanes *before* mixing TTS/cues, so
  TTS stays audible and still crosses CamillaDSP protection. The mixer also
  ducks whenever TTS frames are merely *pending*, depth-aware by segment kind:
  assistant audio uses the full `JASPER_DUCK_DB` (default −25 dB), while a
  standalone earcon uses `JASPER_FANIN_TTS_CUE_DUCK_DB` (default −6 dB; 0
  disables) — a ~0.3 s tone should not slam the music like a conversation.
- **Interruption is epoch-based.** A flush advances the TTS epoch, clears the
  enqueued assistant buffer, and ignores pre-flush audio commands already
  accepted onto the bounded IPC queue, so barge-in cannot resurrect stale audio.
  Python uses a synchronous `FLUSH_SYNC` with a bounded ack wait; on timeout it
  closes the ordered socket so a late stale ack cannot be mistaken for a later
  flush. The ack carries the playout ledger that drives provider truncation
  ([ADR-0115](adr/0115-provider-truncation-is-driven-by-the-playout-ledger.md)):
  outputd's is DAC-clock-true; fan-in's
  ([`rust/jasper-fanin/src/playout.rs`](../rust/jasper-fanin/src/playout.rs)) is
  the mix-commit count, which over-reads true playout by the fixed downstream
  pipeline depth — the conservative direction.
- `JASPER_TTS_TRANSPORT=sounddevice` is intentionally rejected in this tree.
  That older PortAudio path has no dynamic content/profile matching policy;
  rollback means deploying a pre-outputd revision, not flipping the env var.

## The content lane

- **`shm_ring` (product default on ring-eligible stereo boxes).** CamillaDSP
  writes Ring B through `jts_ring_playback`; outputd reads
  `/dev/shm/jts-ring/content.ring` one 128-frame slot per DAC period and never
  opens an ALSA content capture PCM. STATUS `content.buffer_frames` is therefore
  a synthetic period-sized stand-in (NOT a jitter buffer) and the true capacity
  is published honestly in `content.ring.capacity_frames`.
  `jasper.audio_runtime_plan` does not emit
  `JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES` under this bridge.
- **`direct` (legacy/fail-safe).** Reads `outputd_content_capture`, backed by
  snd-aloop substream 6 (`hw:Loopback,1,6`), for ring-ineligible boxes,
  operator-frozen boxes, and every roleful box not explicitly armed. There the
  content buffer env is real.
- **The ACTIVE ring and the allowlist that can park outputd.** A roleful
  (active-crossover) box has a third ring, `jts_ring_active_playback` →
  `/dev/shm/jts-ring/active-content.ring`, carrying post-crossover per-driver
  channels. Reaching it the first time is the three-step ladder documented in
  [HANDOFF-audio-graph-consolidation.md](HANDOFF-audio-graph-consolidation.md).
  What matters *here* is the refusal an operator will meet: under `shm_ring`,
  `Config::from_env` enforces a **biconditional** — the active ring path may be
  read ONLY by an armed active endpoint, and an armed active endpoint may read
  ONLY that path. A crossed pair is a hard bail (outputd exits rather than
  starts: a silent speaker with a parked unit). The check is scoped to
  `shm_ring` deliberately, because under `direct` there is no ring to read and
  an unscoped check would park the documented rollback. A second,
  bridge-independent bail rejects `JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT` set
  without `JASPER_OUTPUTD_ACTIVE_LANE` — one helper writes both from one
  decision, so an incoherent pair can only mean that writer is broken. The role
  rides the NAME, not the width: on a 2-way speaker both rings are 2 channels,
  so no channel-count test can tell them apart. The allowlist is positive
  equality against a named path constant, never a denylist.
- **`rate_match` is removed.** `direct` and `shm_ring` are the whole vocabulary.
  A persisted `rate_match` (or `ratematch` / `rate-matched` / `rate_matched`)
  resolves to `direct` with an `event=outputd.content_bridge.removed_value`
  WARN rather than bailing — a deleted knob must never turn a routine deploy
  into exit 78 → a parked final-output owner → a silent speaker — and its three
  tuning keys are inert. This is **not** permission to run it: the route-latency
  policy compares the RAW literal, so a stale spelling still reports a red
  `usb_low_latency_48k` claim rather than a silently downgraded green one. Any
  other unrecognized value still fails loud.
- **Multi-room round-trip lane** (`JASPER_OUTPUTD_DAC_CONTENT_FIFO`, inert until
  a bond activates it): a member feeds its DAC from the snapclient-written
  raw-PCM FIFO instead of `outputd_content_capture`, picking a channel via
  `JASPER_OUTPUTD_DAC_CONTENT_CHANNEL`. `sub` mono-sums clip-safely then applies
  a 4th-order Linkwitz-Riley low-pass at `JASPER_OUTPUTD_DAC_CONTENT_SUB_HZ` —
  the one place outputd does spectral DSP, deliberately, because the
  dumb-follower lane bypasses CamillaDSP. `JASPER_OUTPUTD_DAC_CONTENT_HP_HZ` is
  the complementary mains high-pass. Both are fail-closed. The lane falls back
  to the direct read whenever the FIFO starves, so the leader is never silenced;
  unset is byte-identical to the direct path, and the reference still equals what
  the DAC plays. Design and invariants:
  [HANDOFF-multiroom.md](HANDOFF-multiroom.md) §4. Mutually exclusive with any
  non-`direct` content bridge and with the dual-Apple sink (both fail loud at
  startup).

## Current outputd state

What selects the DAC, what the hardware reconciler writes, and which graph a
box boots.

`outputd_dac` is a direct hardware alias for the selected final-output card.
**Every recognized single DAC profile renders as a raw `type hw` alias with no
converting `plug` in front of it**, so outputd's own client-edge readback IS the
hardware-edge proof: it REQUESTS the declared format on the raw `hw:` PCM and
fails closed unless the installed `hw_params` report it. Two profiles declare a
non-S16 final edge (`DacProfile.final_edge_format = S32_LE`): InnoMaker, whose
kernel DAI advertises only S24_LE/S32_LE, and the base HiFiBerry DAC8x (the
horn-lane crackle fix). DAC8x Studio stays at the `S16_LE` default — no lab unit
exists to run the same probe, and this program does not flip a width on inference
from a shared DAC-chip family. The two boards share neither overlay nor driver,
and the base profile's regexes were narrowed to the one card name its driver
emits so Studio silicon cannot inherit `S32_LE`; two residuals stay documented
rather than closed (a Studio configured with the base overlay is genuinely
indistinguishable, and on rpi-6.18.y the whole Studio family shares one card
name, so a Studio parks as `unknown`).

`jasper-audio-hardware-reconcile` runs at install/boot and from udev `controlC*`
events. It writes `JASPER_AUDIO_DAC_ID` / `JASPER_AUDIO_DAC_CARD` into
`/etc/jasper/jasper.env`, the observed-hardware artifact
`/run/jasper-output-hardware/output_hardware.json`, and
`/var/lib/jasper/outputd.env` for runtime selection. `/state` exposes the
artifact as `audio.output_hardware`, `/sound/output-topology` returns it beside
the topology draft, and `jasper-doctor` has a first-line "Output hardware state"
check. Apple-only analog mixer units (`jasper-dac-init`,
`jasper-headphone-monitor`) are enabled only for the recognized Apple role.

**Runtime activation is stricter than hardware observation.** The reconciler
switches `JASPER_OUTPUTD_SINK=dual_apple` for any recognized composite, but arms
the composite's ACTIVE-lane pairing only when the active-speaker runtime contract
proves the already-loaded endpoint graph targets the live ring endpoint and its
width fits the profile cap. Until that evidence exists the observed profile stays
dual Apple for UI/diagnostics while the runtime DAC role is parked with
`JASPER_OUTPUTD_BACKEND=fake` and
`event=audio_hardware_reconcile.dual_apple_detected action=park_until_active_graph`.
An unknown/no-output role likewise gets `fake` rather than a guessed card, and
stops `jasper-voice` plus any stale outputd so final-output ALSA ownership cannot
run against removed hardware. A recognized role stages and validates the outputd
candidate before re-rendering the ALSA alias; if validation rejects it, the prior
env is preserved and reconcile parks (exit 78) before any render or audio
restart, so stale active multichannel state cannot reach a passive stereo plug.

**The park is not skippable by an earlier refusal.** Before any of that, the
reconciler resolves outputd's capture half for each Camilla playback lane; when
that step cannot answer it logs `outputd_endpoint_contract_failed` and exits 66
*ahead of* the env write and the ALSA render, preserving the running env. That is
harmless only while the preserved env describes hardware the pass still sees —
and one shape does not: `JASPER_OUTPUTD_BACKEND=alsa` at
`JASPER_OUTPUTD_DAC_PCM=outputd_dac` while the ALSA artifact **already on disk**
renders that alias as `type null`. A null device accepts every write instantly
and nothing paces the content side either, so outputd's loop has no clock and
spins until `LimitRTTIME` SIGKILLs it — three per burst, then
`StartLimitAction=reboot` (observed live: three consecutive reboots absorbed by
the bootloop guard). The fallback therefore flips that one key to `fake` and logs
`outputd_env_clockless_park … action=park_backend_fake`, leaving every other
preserved shape byte-unchanged, because the preserved env is known to start and
writing more would trip outputd's own allowlist. The park is additionally gated
on the pass having **observed** the hardware at all — a pass whose observation
failed knows strictly less than the pass that wrote that env — which keeps a
broken interpreter from parking a healthy DAC. **Every conjunct reads an artifact
rather than re-deriving one**, and that is load-bearing: this exit returns well
ahead of `render_asound_if_needed`, so the alias outputd opens is whatever an
EARLIER pass left. Asking what *this* pass would render once reported "null"
while the live template still said `type hw card A`, parking a box whose DAC was
still playing; the guard greps the rendered template's `pcm.outputd_dac` block
instead, and an absent or unreadable template declines rather than assumes.

**Which graph a box boots** is chosen by `jasper.active_speaker.runtime_contract`
(via `jasper-active-speaker runtime-safe-graph`) from the saved
`jasper.output_topology` contract, and the outputd `jasper-camilla.service`
reads `/var/lib/camilladsp/outputd-statefile.yml` so the normal statefile stays
intact. Two properties matter from this side. First, **absence never implies
stereo**: a missing topology, zero speaker groups, or an incomplete non-passive
layout selects the generated DAC-less all-muted **parked** graph rather than a
flat DAC graph; only an explicit valid stereo passive topology may use the flat
carrier, and an explicit mono one uses a WIDTH-MATCHED graph that hard-mutes the
unclaimed channel, re-proved structurally off the YAML. Second, **parking is
deliberately narrow**: a staged graph that exists but fails its safety proof
still fails closed (that is a commissioning bug, not a paused household), while
a topology-level blocker does not, because the parked graph is silent by
*structure* (`File` sink, wired hard mute on every output, both re-proved off
its own bytes) — refusing on a blocker only pinned the box on whatever graph it
was already running, which mid-commission is the illegal flat one. Blockers stay
visible in the parked decision, the install transcript, and
`check_active_speaker_topology_blockers`.

**Topology save and reset are one transaction**: prove and load the parked
graph, commit the validated topology under the mutation lock, converge the
runtime graph, then ask the root reconciler to converge the boot graph. Before
that graph loads, `jasper.sound.runtime.materialise_saved_dsp_on_carrier`
recomposes the saved sound profile and headroom trim onto it and atomically
writes `sound_current.yml` — it renders only, taking no graph lock and never
asking CamillaDSP to load; the enclosing transaction re-proves and owns the load.
Reset commits **zero speaker groups**, never infers a passive layout from
detected hardware, and leaves audio parked until the household saves its intent;
it must present both the topology content revision and the detected-hardware
identity from its page snapshot, or it gets `409 Conflict` rather than letting a
stale tab clear newer intent. This composition point is also the boundary for
any future passive-speaker linearization: it may be added only after an explicit
passive layout grants flat-graph authority, and it composes as passive
speaker/program DSP — never represented as, or used to infer, an active
crossover.

> **ROLLBACK RULE — the content-lane width flip is NOT rollback-symmetric.**
> `JASPER_OUTPUTD_CONTENT_FORMAT` is a merge-write into
> `/var/lib/jasper/outputd.env` that nothing clears, and outputd has READ it
> since the declaration landed. Rolling back or bisecting a **flipped** box to
> any commit between the declaration and the flip leaves outputd requesting
> `S32_LE` while the rolled-back emitters render `S16_LE`; on a raw **active**
> lane whichever opener wins locks a width the other cannot serve. There is no
> code fix — the actor is the OLD code, which cannot know about a key that
> postdates it. **Passive/plug boxes are SAFE** (the `outputd_content_*` `plug`
> converts). Pre-step for **active-lane** boxes before deploying a pre-flip
> commit:
>
> ```sh
> ssh <box> 'sudo systemctl stop jasper-camilla.service jasper-outputd.service && \
>   sudo sed -i "/^[[:space:]]*JASPER_OUTPUTD_CONTENT_FORMAT[[:space:]]*=/d" /var/lib/jasper/outputd.env'
> JASPER_DEPLOY_ALLOW_DOWNGRADE=1 bash scripts/deploy-to-pi.sh
> ```
>
> Both stops must precede the deletion — deleting first leaves a window where a
> CamillaDSP re-exec locks S16 and fires the restart ladder.
>
> **`JASPER_OUTPUTD_DAC_FORMAT` is the second persistent width key in the same
> file, and it IS rollback-symmetric — no pre-step.** A deploy stops
> `jasper-outputd` BEFORE the reconciler rewrites it
> (`park_audio_clients_for_core_graph_restart` runs immediately ahead of
> `jasper-audio-hardware-reconcile --reason install`), and the key is re-emitted
> from the DAC registry on **every** pass that reaches the registry, so a
> rolled-back reconciler writes its OWN registry's value. Only an abnormal
> termination mid-install can strand a half-flipped value — and even then
> outputd's open-time readback parks at exit 78 rather than playing a width the
> device did not install.

## Failure, restart, and the reboot budget

- **Configuration-class faults park, they do not loop.** An initial final-sink
  open/negotiation failure, or a lane that *installs* a width other than the one
  requested (`FinalSinkStartupConfigError`), exits `EX_CONFIG` 78 into
  `RestartPreventExitStatus=78`. A lane that *refuses* the request outright — a
  raw active lane whose peer already locked the pair — fails `hw_params` and
  exits 1 onto the ordinary restart ladder, because that same failure is how
  outputd waits for CamillaDSP on every boot.
- **Content-lane open failures are parked out-of-band.** Four consecutive
  failures end in `jasper-outputd-failure-reconcile` (stop plus a record at
  `/run/jasper-outputd-content-lane.state` naming the fix), spending 4 of the 5
  starts so `StartLimitAction=reboot` is never reached. The first failures still
  restart, because on the PASSIVE lane that open is how outputd waits for
  CamillaDSP's half of the snd-aloop pair; on the ACTIVE lane it is permanent, so
  a roleful box that is not ring-armed parks here by design.
- **A missing DAC parks rather than restart-loops.** Startup is gated by the
  reconciler-owned card: `fake` passes (it opens no ALSA), an empty card passes
  for composite/parked shapes, and an `alsa` backend whose card is missing from
  `/proc/asound` logs
  `event=outputd.output_device_gate.park reason=missing_dac`. DAC arrival
  un-parks it with an idempotent `reset-failed` + `start` even when values are
  unchanged.
- **Config shear during DAC re-enumeration:** the reconciler stages and validates
  `outputd.env` buffer/period pairs before replacing the prior file; if outputd
  still exits 78 from a transient shear, the failure helper runs one bounded
  `jasper-audio-hardware-reconcile --no-restart` pass and no-block retries.
  A repeated exit 78 parks instead of looping into reboot policy.
- **Unified xrun policy.** One recovery budget (`xrun_policy` in
  `alsa_backend.rs`) is shared by the single sink's `write_dac_frames` and the
  composite's child write: three recoveries per period, then bail; `Ok(0)` rides
  the same budget. **The recovery is the LINK GROUP's, not per-child** —
  `dac_a.link(&dac_b)` makes `snd_pcm_recover` a group prepare, so the composite
  recovers once, re-primes BOTH children, then group-starts. **The prime depth
  stays one period below `start_threshold` on purpose:** a full-buffer refill
  would auto-start the group the moment child A filled, before child B was
  primed, baking in up to a full period (128 frames ≈ 2.667 ms at 48 kHz) of
  permanent A/B skew — which on an active 2-way IS the woofer/tweeter time
  alignment. The pairwise baseline is then re-latched under a magnitude bound;
  a pair outside it fails closed rather than blessing the offset. An
  **unlinked** pair keeps the bail (post-recovery skew is unbounded and
  unverifiable), which is also why `link=ok` is an arm-time precondition for a
  composite on the SHM ring. Surfaces: `event=outputd.xrun source=dual_dac_*`,
  `event=outputd.dual_apple.reprime status=ok|alignment_refused|xrun_during_reprime`,
  and `/state.dual_apple` counters.
- **Composite child loss bails; it never mutes.** No mute-all-children path
  exists. A child that *disappears* is caught on the write path but **not** by
  the recovery ladder — `write_dac_fail_closed` enters that ladder only on
  `EPIPE`/`ESTRPIPE`, so an `ENODEV`/`ENXIO` removal takes the bare propagate
  beneath it. Do not read an absent `event=outputd.xrun` as "no removal", nor a
  present one as "just an xrun": a removal can surface first as an `EPIPE`
  underrun, whose xrun line prints before `try_recover` fails on the absent
  device. Recovery is out-of-band (udev → `jasper-audio-hardware-reconcile`
  clears the child PCM env and acts on outputd), and where it lands is decided by
  the **saved** topology, not by what survived: a saved **roleful** composite
  parks behind a named `saved_composite_partially_present` blocker
  ([`jasper/output_hardware.py`](../jasper/output_hardware.py)
  `apply_saved_topology_policy`) rather than running the survivor as a stereo
  DAC, while a *passive* composite — which may legally place every declared
  speaker on one child — rewrites `JASPER_OUTPUTD_SINK=single_alsa` for the
  survivor. Rolefulness, not `kind == "composite"`, is the gate. Child-presence
  gating is the **reconciler's**: `dual_apple_runtime_mapping` refuses unless
  exactly two child devices with PCMs resolve, while the unit's single
  `ExecCondition` tests only the one resolved `JASPER_AUDIO_DAC_CARD`.
  **Known observability gap, and it is the composite's weakest promise:**
  `write_dac_fail_closed` carries exactly one `eprintln!` (the xrun line) and
  `start_dacs` none, so the bad-PCM-state bail, the group-start refusal, a
  repeated `Ok(0)`, and the non-`EPIPE` propagate are all silent — visible only
  as the bail message and the restart. **Never built — do not go looking:** a
  `sink.health()` API and an `event=outputd.composite.child_lost` line. **Still
  owed:** the per-child array under a width-agnostic `composite` `/state` block,
  keeping `dual_apple` as a read alias for one release and migrating the
  doctor's `=="dual_apple"` branches in the same PR.

## Observability

`event=outputd.*` structured logs, `/state.outputd` via `jasper-control`, the
`/system` Outputd row, and `jasper-doctor` checks. The daemon reports negotiated
ALSA period/buffer sizes, xrun counters (labelled content vs DAC on the
dashboard, since a content-capture recovery is a different risk from a physical
one), content empty/partial/EAGAIN periods, last-xrun age, uptime-normalized
xrun rate, watchdog progress, clipping, pending TTS frames, TTS over-budget
duration, dropped TTS command/audio-frame counters, and compact flush summaries.
**Real clip accounting holds at every width** — the composite path no longer
hardwires `clipped_samples=0`, without which the commissioning "no clip" gate is
vacuously green.

**A short content read is zero-filled and a full period is still written**, so
it INSERTS `requested - frames` samples into the emitted timeline — audible as a
brief tear that displaces the rest of the program in time. Every fill emits
`event=outputd.content_fill` with `source=partial|empty|eagain|xrun_recovered`,
`frames_short`, and running counters, rate-limited to one line per second
(overflow arrives as `suppressed=` on the next line). The fill is the EXPECTED
steady state on `content_bridge=direct` — nothing absorbs the
content-producer-vs-DAC offset there — so `jasper-doctor` deliberately does not
warn on it; the measurement path gates on it through `runtime_integrity`'s
`outputd_content_fill_increased`. That gate is **topology-agnostic by
construction**: the run loop drives exactly one content source per box, so it
reads the ALSA hop's `empty_periods`/`partial_periods` AND the ring's
`empty_reads`/`startup_empty_reads`. Journal coverage is deliberately
asymmetric — the ALSA hop emits `content_fill` because it had no other runtime
surface, while the ring path stays journal-quiet, since it already publishes
`writer_alive`, `occupancy`, and heartbeat age, and a dead writer makes EVERY
period empty (a per-fill line there would be a sustained stream, not a
diagnostic).

The local control socket takes one newline-delimited command per connection,
capped at 256 bytes and a two-second total monotonic deadline (not a resettable
per-byte timeout), so oversized, invalid-UTF-8, and slow-trickle requests get
bounded JSON errors without journal spam. It never runs on the DAC write loop.

Passive DAC/chip-reference timing diagnostics for AEC bring-up live under
`dac.snd_pcm_delay_*` and `reference_outputs.chip_ref_writer.*`; field units are
in [AEC-DIAG-02-observability.md](AEC-DIAG-02-observability.md).
`chip_ref_writer.recent_writes` is the one field a consumer cannot degrade past:
a bounded ring (`recent_writes_capacity`, 256) of per-write observations
(`frames_written`, `snd_pcm_delay_frames`, `reference_sequence`, `age_ms`,
oldest first). `jasper-aec-init` resolves chip-AEC `SYS_DELAY` from a *run* of
those readings and cannot assemble one from the single latest value at this
thread's ~2 reads/s, so an outputd that does not publish it parks the box by
name. outputd reports raw observations only; every acceptance rule over them
lives in `jasper/cli/aec_init.py` and `jasper/chip_aec_alignment.py`. The
optional `JASPER_OUTPUTD_CHIP_REF_TEE_PATH` raw-sample tee is diagnostic only,
belongs under `/run/jasper-outputd` or `/var/lib/jasper` in the packaged sandbox,
and must never be treated as a production reference path.

## Rules that constrain future output work

- **Dispatch on clock-domain *shape*, never on DAC id.** One `run_alsa` loop
  serves both sinks behind `RuntimeAlsaSink { Single, Composite }`; channel width
  and the channel map are DATA from the `DacProfile`/topology. Adding a DAC of an
  established shape is a registry row; a new shape pays transport code once.
  `PairedCompositeSink` **stays two children** — a pairwise drift guard cannot be
  half-`Vec`-ified, so M>2 composite output is a genuinely new sink impl, not a
  config row.
- **The AEC reference is mono, so the fold is trivial — and it scales by 1/N.**
  Both consumers collapse to mono (software AEC3 sums L+R; the chip USB-IN
  producer downmixes), so `fold_reference` sums all driven active lanes and
  publishes the result into the existing stereo reference (L = R), leaving
  `speaker_reference_channels: 2` unchanged. Scale the sum by **1/N** (N = driven
  lanes): N correlated full-scale lanes sum to N×, so 1/N is clip-proof
  regardless of correlation. **Do not "optimize" back to 1/√N** — it is
  power-preserving only for *uncorrelated* lanes (a woofer and sub share LF; L/R
  are correlated), so it would reintroduce a clipped reference, which is uniquely
  harmful because a linear AEC cannot model the nonlinearity. The AEC adapts its
  own ERL, so the lower level costs nothing. The pairwise composite path
  (`fold_reference_pairwise_composite`) stays byte-identical to its predecessor:
  `[avg(ch0,ch1), avg(ch2,ch3)]` per frame.
- **Open item on the fold's band.** A reference dominated by sub energy the mic
  cannot hear inflates the NLMS denominator without contributing correlation. The
  software path already high-passes its reference at 125 Hz; the **chip** path is
  unverified — check the XVF3800 USB-IN reference band and the array's LF
  roll-off, and high-pass the fold to match mic sensitivity if needed. The XVF
  exposes only 2 reference channels, so a separate sub reference is impossible;
  this is a "what goes into the sum" question, which is why mono is the right
  shape.
- **No `type plug` / `plughw:` on the active path.** Use width-exact `hw:` so a
  mismatch fails at `snd_pcm_hw_params` instead of silently remixing 8→4 onto
  live drivers — the single most dangerous fail-open available here. A contract
  test rejects `plug`/`plughw` anywhere on that path, and CamillaDSP's own
  refusal to start when its mixer output count ≠ the playback device `channels`
  is the independent second layer.
- **Drive what we use, not the DAC's full width.** `active_outputd_lane_channels`
  is a **cap**: the reconciler reads the live endpoint config's actual width W,
  accepts `2 ≤ W ≤ cap`, and emits that W as `JASPER_OUTPUTD_ACTIVE_CHANNELS`
  (cleared in every non-active branch). A config wider than the cap is refused
  before the render (`active_graph_width_out_of_range got=W cap=N`), and a wrong
  width that reached the raw `hw:` open anyway fails closed at `set_channels`.
  Padding a narrow speaker to the DAC's full channel count with muted lanes was
  considered and rejected.
- **Zero allocation on the DAC-write hot path at any width**; preallocated
  per-child period buffers and fold scratch. `OutputCore`, reference sequence
  tracking, and ledger loudness stay conditional on TTS, so a solo stereo speaker
  allocates none of it. No new threads, no new poll loops, no new resident
  process.
- **`volume_limit: 0.0` holds in the active config**; per-driver limiters and the
  protective tweeter high-pass live in the CamillaDSP graph. Verify on hardware
  in the staged order in
  [HANDOFF-active-speaker-dsp.md](HANDOFF-active-speaker-dsp.md): start muted,
  unmute one output at the calibration floor, woofer-first/tweeter-last, with a
  live high-pass-presence assertion before the tweeter is unmuted.

## Robust barge-in contract

Robust barge-in is JTS-owned: stop audible assistant audio locally first, then
reconcile provider conversation state to what the listener actually heard, using
the playout ledger rather than event arrival time
([ADR-0115](adr/0115-provider-truncation-is-driven-by-the-playout-ledger.md)).
The output-side invariant lives here; provider semantics live in
[HANDOFF-voice-providers.md](HANDOFF-voice-providers.md#provider-interruption-contract),
and the per-provider landing ledger — which packs self-truncate as a no-op and
what remains before default-on — is
[HANDOFF-barge-in.md](HANDOFF-barge-in.md#implementation-plan).

Detection and local flush are wired behind a per-provider flag that **defaults
OFF**: while the assistant speaks, `WakeLoop._handle_playback_frame` runs local
Silero VAD on the AEC-cleaned leg and, on a sustained run at or above
`JASPER_VAD_BARGE_IN_THRESHOLD`, calls `LiveTurn.request_local_interrupt()`,
which `_play_responses` races (including the `wait_drained()` drain tail) to
`flush()`. The flag is `JASPER_BARGE_IN_<PROVIDER>` in
`/var/lib/jasper/voice_provider.env`, read fresh per turn via
`jasper.voice.provider_state.read_barge_in_enabled` — never an `os.environ`
cache — and a runtime guard hard-disables it for a session whose active profile
has no AEC reference (`direct_mic`), to avoid self-tripping on TTS bleed.
Off-device validation cannot exercise false-barge from TTS bleed; that is a
hardware step.

## Still intentionally not done

- Software can expose the final electrical samples sent to the DAC, never the
  acoustic "what hit the room" signal. That needs microphone-side observation.
- The chip USB-IN producer stays separate from the software monitor: software
  AEC/corpus/diagnostics must not depend on chip hardware being present.
- Fan-in's `audio_played_ms` still over-reads by the fixed downstream pipeline
  depth; closing it to exact DAC-clock precision (subtracting outputd's reported
  DAC delay) is the remaining follow-up.
