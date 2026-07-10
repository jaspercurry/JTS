# Handoff: JTS audio-latency foundation

> **Status: largely superseded (2026-07-03).** The USB-input latency
> workstream this doc planned has shipped: the One-Clock Ring Graph
> (SHM rings + `jts_ring` ioplug + fan-in USB DIRECT capture) merged as
> the #1137–#1142 PR train, hardware-validated at an end-to-end floor of
> ~46 ms p50 on the Apple-dongle profile (~2.6× below the −125 ms
> audio-lag detectability threshold). Current operational truth,
> measured ladder, lever outcomes, and the remaining engineered headroom
> live in [HANDOFF-usb-low-latency.md](HANDOFF-usb-low-latency.md)
> "Final state — 2026-07-03". This doc remains the archaeology for the
> lean-lane/transport_pipe/snapcast-buffer investigations and the
> 16 KiB-page-floor finding that motivated the SHM-ring design.

Canonical reference for JTS's local-audio-latency work: lowering latency on
the music path while keeping the speaker resilient and supporting flexible
output/mic hardware. Read this before touching the lean lane, the USB-input
bridge latency, or the snapcast bond buffer.

**Targets:** USB-audio-input end-to-end latency at p95 <= 40 ms all-in
(source → DAC, including CamillaDSP and room correction), with promotion
requiring p99 <= 60 ms from a statistically adequate route-latency artifact;
AirPlay (Apple TV → bonded pair) staying within the ~2 s presentation budget;
and, in general, *only* adding latency where a specific piece of hardware
genuinely requires it.

## 2026-07-01 checkpoint: FIFO is not the endgame

The `transport_pipe` lab path proved the right *clock question* and the wrong
transport primitive. On `jts.local`, `getconf PAGESIZE` reports **16384** on the
Pi 5 `rpi-2712` kernel. Linux pipe/FIFO capacity cannot shrink below one page,
so `F_SETPIPE_SZ` requests for 4/8/12 KiB all floor to **16 KiB**. At 48 kHz
stereo, that is:

| Pipe wire format | Bytes/frame | 16 KiB floor |
|---|---:|---:|
| S16_LE stereo | 4 | 4096 frames = 85.3 ms |
| S32_LE / float32 stereo | 8 | 2048 frames = 42.7 ms |
| float64 stereo | 16 | 1024 frames = 21.3 ms |

The transport-pipe test also showed CamillaDSP `File` playback keeps outputd's
local FIFO filled continuously. A reader-side "drop old backlog to one period"
experiment in `jasper-outputd` re-anchored every period and caused continuous
audio drops, then was reverted. So the failure is structural: a page-granular
byte-stream pipe is not a frame-bounded realtime transport, and dropping stale
FIFO backlog is not a viable latency fix.

Current implication:

- Do **not** default `transport_pipe` on for the low-latency claim.
- Do **not** keep tuning FIFO size as the strategy; the Pi page size is the
  floor.
- Treat the shipped transport-pipe code as a failed/default-off lab path until
  it is either removed or repurposed for non-low-latency diagnostics.
- Preserve the latency goal: **p95 <= 40 ms all-in USB → DAC**, with p99 <= 60 ms
  for promotion, proven by a click-in/capture-back sample count and not by summed
  buffer math alone.

Design direction from the checkpoint:

1. Keep `jasper-outputd` as the physical DAC owner / hardware clock adapter.
   DAC variance (async USB, synchronous USB, I2S/HAT clocks, future custom HATs)
   belongs behind the DAC profile + outputd timing layer.
2. Give every foreign clock exactly one rate matcher: USB-host input, network
   renderers, and mic capture for software AEC each cross into the DAC domain
   explicitly. TTS is clockless generated audio and simply gets consumed by the
   DAC-paced graph.
3. After ingress, keep fan-in/TTS/CamillaDSP/outputd in one DAC-paced domain.
   Remaining boundaries should be sized in **frames**, expose occupancy and
   latency, and avoid opaque byte-stream buffering.
4. Borrow PipeWire/JACK principles, not necessarily their full runtime:
   graph-shaped nodes/ports, one driver/pacer per graph cycle, fixed quantum,
   shared-memory/frame rings, explicit latency accounting, policy separate from
   transport.
5. Near-term spike: tune the clocked ALSA/snd-aloop path with RT hardening and
   measure real round trip. If that cannot approach the target, prototype a
   frame-bounded shared-memory/ALSA-facing transport or a JACK/PipeWire graph
   with outputd's DAC-owner role redesigned deliberately.

2026-07-02 tuning result: the best stable loopback values on jts.local are Rust
USB bridge 256/3, fan-in USB resampler held target 2048, fan-in output buffer
1024, CamillaDSP 256/1536, outputd 128/256, and outputd content buffer 1536.
Those values are useful as the stable fallback floor, but they do not make a
credible 40 ms end-to-end route: the resampler held target alone is ~42.7 ms
before fan-in output, CamillaDSP, outputd content, and DAC delay. Further work
should treat the 40 ms target as an architecture/transport problem, not as more
blind loopback tuning.

2026-07-06 resilience update: the `1536` outputd content buffer is emitted only
with a coherent outputd period. During Apple-dongle re-enumeration, if the DAC
profile floor disappears and period falls back to `1024`, the runtime plan
suppresses the low-latency content buffer so outputd keeps a valid buffer/period
pair. The audio-hardware reconciler validates staged `outputd.env` candidates'
outputd buffer/period pairs (content and DAC buffers) before installing them,
and the outputd failure helper gives exit 78 one bounded re-reconcile + retry
instead of permanently wedging on a transient shear.

The route-specific productization and legacy cleanup plan lives in
[HANDOFF-usb-low-latency.md](HANDOFF-usb-low-latency.md#productization-plan).
Keep this file as the clock-domain architecture reference; do not duplicate the
USB route gates here.

Open questions to answer before the next architecture turn:

- Can a frame-bounded replacement for one or both ALSA loopback boundaries hit a
  stable measured p95 <= 40 ms USB path while preserving TTS, CamillaDSP, and
  outputd as the final DAC/reference owner?
- Should USB input bypass its snd-aloop ingress and be captured directly, with
  the ingress DLL/resampler crossing host clock → DAC clock?
- If a shared-memory ring is built, how does CamillaDSP consume it without
  falling back to FIFO: ALSA ioplug/extplug, JACK, PipeWire, or embedding/moving
  the DSP boundary?
- For AEC3, what is the authoritative post-Camilla reference tap and where does
  mic-clock → DAC-clock resampling happen? For chip AEC, which hardware profiles
  actually guarantee reference/acoustic clock coherence?
- What is the correction-filter latency gate: PEQ/IIR/minimum-phase only, or a
  measured FIR group-delay budget enforced by the wizard?

---

## 2026-07-02 checkpoint: the ring transport works — measured

The frame-bounded transport the 2026-07-01 checkpoint called for now exists and
is hardware-proven (prototype branch `latency/ring-proto-shm` + the
`latency/combo-night` lab vehicle; Ring B = CamillaDSP playback → outputd over
a SHM slot ring via a custom ALSA ioplug; jasper-ring Rust crate + C core).

Measured on jts.local (electrical :9891 capture-back, 240 impulses/run, 100%
match, zero stalls unless noted):

| Config | p50 / p95 / p99 (ms) |
|---|---|
| Baseline aloop route (shipped floor) | 173.6 / 181.5 / 183.5 |
| + host-slaved clock + cushion 512-held (certified artifact) | 139.3 / 156.7 / 157.6 |
| + Ring B @ target_level 1536 (parity, spread 10→5.8 ms) | 149.9 / 154.6 / 155.7 |
| + Ring B @ target_level 768 (the Ring-B floor, spread 4.2 ms) | 134.5 / 137.5 / 138.6 |
| + resampler cushion 256 under L0 | 104.1 / 107.4 / 108.6 |

Structural findings the measurements pinned:

- **The 16 KiB page floor is irrelevant to a SHM slot ring** — occupancy is
  handshake-bounded; the transport_pipe failure class does not recur.
- **Ring targets below ~768 relocate latency upstream instead of removing it**
  (t512/t384 runs: p50 rose, spread ×4): CamillaDSP's rate controller can only
  trade ring fill against the fan-in→Camilla aloop fill. The fan-in→Camilla
  hop measures ~45–55 ms and is *hysteretic* (restart-flushed, wanders across
  runs) — it is the remaining boulder, hence Ring A (fan-in → Camilla via the
  same ring primitive, capture direction).
- **Two floor classes exist.** Drift-class floors (resampler cushion, ring
  targets, Camilla target_level) dissolve once the host clock is slaved
  (UAC2 Capture Pitch, L0-locked, macOS probe ratio ~4×). URB-cadence floors do
  NOT: the usbsink bridge at 128 frames/2 periods was re-tried under L0 and
  hard-failed (14.8 k capture xruns in 6 min) — 256/3 is a genuine floor.
- **ioplug ALSA semantics are the hard part, not the transport**: four
  adversarial rounds fixed a dishonest playback pointer (delay ≈ 0 starved
  CamillaDSP's rate controller), a too-shallow ring vs CamillaDSP's buffer
  model (n_slots×128 must be ≥ its negotiated buffer AND target_level), a
  readerless free-run wedge at the avail gate, and the classic mod-buffer
  full-lap alias (reported-advance clamp; shared translation unit so host
  tests compile the real pointer core).
- **camilla's unit ExecStartPre re-seeds the statefile from the output-topology
  contract** — a prototype config must be applied via the live websocket
  set_config_file_path, and any camilla restart safely reverts to cutover
  (fail-safe: silence, not noise). Ring mode is now a topology-contract
  citizen: a ring-armed, ring-eligible box re-seeds the ring flat config instead
  of reverting to the loopback cutover.

Ring A (fan-in → Camilla, plan pinned 2026-07-02) bounds that hop to
n_slots×128 frames with fan-in blocking-on-full as the transitively DAC-paced
writer; falsifiable target ≈ −25..35 ms plus the variance the hysteretic aloop
carried. Certification note: the route-latency artifact binder correctly
accepts the coherent Ring A + Ring B pair for `usb_low_latency_48k`; it still
rejects partial ring flips and deferred lab transports (`transport_pipe`,
`rate_match`).

## The chain, and where latency lives

```
renderer → snd-aloop fan-in lane → jasper-fanin → Ring A → CamillaDSP
         → Ring B → jasper-outputd → DAC
```

- The **fan-in input ring** (~85 ms) is the WiFi-burst absorber — load-bearing
  for networked sources (AirPlay/Spotify), *not* needed by a wired USB source.
- The legacy **fan-in output queue** is fixed downstream latency. As of the
  2026-06-29 JTS2 retune, the loopback-path production floor is 1024 frames
  (~21.3 ms at 48 kHz). A 512-frame trial failed fast with fan-in output
  xruns, so sub-1024 remains loopback-only lab work; eligible product-default
  boxes bypass that downstream queue with Ring A.
- **CamillaDSP** owns `chunksize` / `target_level`; ring-coupled emits use the
  fixed 128 / 128 / queue-1 geometry from
  [`jasper.fanin_coupling`](../jasper/fanin_coupling.py), while loopback emits
  keep the deeper historical queueing.
- **jasper-outputd** is the final-output owner: a blocking DAC write is the
  timing master; the content lane is read non-blocking (absent content → silence).
  Both AEC references are produced here (software AEC3 → 48 kHz UDP `:9891`;
  chip-AEC → 16 kHz USB-IN), so **nothing can delete `outputd`**.
- **DAC buffer** is env-tunable (`JASPER_OUTPUTD_DAC_BUFFER_FRAMES`); measured
  ~16 ms reliable on both the HiFiBerry DAC8x and the Apple dongle — *not* the
  bottleneck.

## Runtime-plan SSOT layer (2026-06-30)

The latency knobs now have a read-only explanation layer:
[`jasper.audio_runtime_plan`](../jasper/audio_runtime_plan.py). It resolves the
planned values from **operator env → DAC profile floor → packaged default** for
Camilla/outputd, and from **fanin.env → packaged fan-in default** for fan-in
buffer/coupling knobs, while reporting duplicate homes, malformed lab values,
stale generated env, and unsupported route/coupling combinations.

Temporary lab frame/buffer values belong in
`/var/lib/jasper/audio_runtime_overrides.json`, not `/etc/jasper/jasper.env`.
Manage them with `jasper-audio-config overrides-set|overrides-list|overrides-clear`.
Each override has a reason and may have an expiry; expired/invalid entries are
ignored and surfaced as plan/doctor warnings. Valid numeric overrides
intentionally win over operator env/profile/defaults so experiments are explicit
and auditable. Fan-in coupling is intentionally not overrideable here: switching
`JASPER_FANIN_CAMILLA_COUPLING` needs the ordered
`jasper-fanin-coupling-reconcile` transition.

Operator surface: `jasper-audio-config explain [--json]`.
Health surface: `jasper-doctor` includes `audio runtime plan` before the
lower-level fan-in coupling check. Writer / routing surfaces already consuming the plan:
the audio-hardware reconciler asks `jasper-audio-config outputd-floor-actions`
for `/var/lib/jasper/outputd.env` latency-floor set/unset decisions, and
`jasper.fanin.buffer_reconcile` / `jasper.fanin.coupling_reconcile` ask the
plan for fan-in output-buffer set/unset/floor decisions, adaptive lab target,
and coupling route-support policy. Mux's adaptive-buffer consumer uses the
plan's `decide_source_low_latency_route` source-exclusivity decision. Sound
runtime asks the plan for shared fan-in coupling capture kwargs
(`fanin_coupling_capture_kwargs`), so the RawFile/AsyncSinc shape does not live
in separate staged/live/reconcile code paths. The carrier also asks the plan's
`apply_capture_precedence` helper whether grouped pipe-sink playback or shared
fan-in coupling owns capture for this emit. Other reconcilers still write their
existing env files; move those decisions behind the plan as the next migration
steps. (The lean-lane consumers and `lean_capture_kwargs` /
`usbsink_output_mode_action` that this paragraph used to also describe were
deleted in the USB dead-pipeline sweep — see the callout below.)

## The lean lane (Stage 4) — REMOVED

> **Removed (USB dead-pipeline sweep).** The entire lean lane described in this
> section was **deleted**. Its only delivery mechanism was the Python
> `jasper-usbsink` bridge writing a FIFO, which was itself removed, and the
> production Rust `jasper-usbsink-audio` daemon never had a `fifo` output mode —
> so the lane was unarmable on a real box. Deleted symbols named below no longer
> exist: `Mux._enter_lean`/`_leave_lean`, `jasper.usbsink.output_mode_reconcile`,
> `jasper.sound.runtime.stage_lean_capture_config` /
> `apply_lean_capture_config` / `restore_buffered_config`,
> `lean_capture_kwargs`, `DEFAULT_LEAN_CAPTURE_FIFO`, and the `JASPER_LEAN_LANE`
> env / the `fifo` value of `JASPER_USBSINK_OUTPUT_MODE`. The text below is
> archaeology of the design. The `jasper-camilla-pipe-guard` dangling-capture
> floor survives — it now guards the live `transport_pipe` capture pipe.

The lean lane is the low-latency music path for a **single, exclusive, wired**
source (USB audio input): the source writes a named pipe, CamillaDSP
**File-captures** it directly instead of draining the fan-in summed lane,
shedding one full snd-aloop round-trip.

**Key fact:** the lean lane only swaps CamillaDSP's **capture** device
(`plug:jasper_capture` → a File pipe). Playback stays
`outputd_content_playback`, so **`jasper-outputd` is unchanged** and both AEC
references keep working. A File capture has no clock, so it requires
`enable_rate_adjust: true` **and** an async resampler (rate-adjust "method 2").

**CamillaDSP schema gotcha:** the deployed runtime is **CamillaDSP v4.x**, whose
resampler is an *object* — `resampler: {type: AsyncSinc, profile: Balanced}` —
not the pre-v2 scalar `resampler_type: BalancedAsync` (the v4 parser rejects the
scalar). The shared emitter helpers live in
[`jasper/camilla_config_contract.py`](../jasper/camilla_config_contract.py)
(`file_capture_resampler_yaml`, `is_async_resampler`,
`DEFAULT_FILE_CAPTURE_RESAMPLER_TYPE`/`_PROFILE`, `DEFAULT_LEAN_CAPTURE_FIFO`);
the stereo ([`jasper/sound/camilla_yaml.py`](../jasper/sound/camilla_yaml.py))
and active-speaker
([`jasper/active_speaker/camilla_yaml.py`](../jasper/active_speaker/camilla_yaml.py))
emitters both use them — one definition, no copy-paste twin.

**CRITICAL — capture is `RawFile`, NOT `File` (the 2026-06-27 jts5 finding).**
CamillaDSP v4 has **no `File` *capture* variant** (capture is one of
`Alsa`/`RawFile`/`WavFile`/`Stdin`/`SignalGenerator`; `File` is a *playback*-only
sink type — the multiroom snapserver pipe). Both emitters originally shipped
`capture: {type: File}`, which the DSP rejects at load with *"unknown variant
`File`"* — a **silent capture outage** that slipped past build, review AND CI
because no test ran `camilladsp --check` (the string tests asserted the wrong
`File` literal). Fixed to `type: RawFile` in both emitters + their tests, and
validated on jts5 / CamillaDSP 4.1.3: with fan-in writing the pipe, `--check` on
the RawFile config returns **"Config is valid."** The ordered transition is now
owned by a reconciler ([`jasper.fanin.coupling_reconcile`](../jasper/fanin/coupling_reconcile.py),
CLI `jasper-fanin-coupling-reconcile <loopback|transport_pipe>`):

1. **Ordered dual-pipe arm — BUILT.** On ARM the reconciler writes fanin.env +
   outputd.env, restarts outputd first so the local content-pipe reader exists,
   restarts fan-in second so the capture-pipe producer exists, then reconciles
   CamillaDSP to the RawFile/File config. fan-in's pipe writer opens lazily, so
   the actual writer attaches only after Camilla opens the RawFile reader. On
   DISARM it reverses the risky edge: reconcile CamillaDSP to Alsa FIRST, then
   restart fan-in and outputd to loopback. Any ARM failure rolls the whole box
   back to loopback. After a successful ARM or transport-pipe CONFIRM, the
   reconciler also runs a short live STATUS activation gate; if pipe occupancy,
   fan-in pipe drops, fan-in input xrun/catchup counters, or outputd DAC/content
   counters drift during that window, it immediately recovers to loopback. On a
   clean reboot the systemd order gives outputd/fan-in reader/writer rendezvous
   before Camilla loads the pipe graph.
2. **Reconnect (RawFile EOF) — characterized + self-healing.** fan-in
   auto-reopens the pipe on `reader_gone` (a CamillaDSP reload); a fan-in restart
   EOFs camilla's RawFile capture, which self-heals via camilla's own restart
   (a brief gap, `reopen_count`/`dropped_periods` in `/state`). A *coordinated*
   reload (so a fan-in bounce is gap-free) is a possible smoothing follow-up, not
   a blocker.

**Observability:** `/state .fanin.output.pipe.{path,requested/actual_pipe_bytes,
dropped_periods,reopen_count}` + transport; `/state .content.local_pipe`
reports outputd's empty/partial/reopen/read-failure counters and pipe occupancy;
`jasper-doctor`'s `check_fanin_coupling` warns when the persisted intent
(`fanin.env` + `outputd.env`) and the loaded CamillaDSP capture/playback graph
disagree (the half-applied / crash-loop precursor).

**Dangling-lean strand — two-layer floor (2026-06).** A crash BETWEEN
enter-lean and leave-lean can leave CamillaDSP's persisted `--statefile`
pointing at the lean RawFile config while its `/run` capture pipe is gone (the
producer reverted to the aloop lane). A camilla restart then reloads the
dangling config and crash-loops on the absent pipe. Two independent fixes guard
this: (1) **runtime** — `restore_buffered_config` (leave-lean) re-points off
lean whenever the LIVE camilla config OR the on-disk statefile names lean, so it
no longer no-ops on the live read alone (`event=sound.lean_leave trigger=strand`
when the on-disk statefile drove the repair); (2) **boot-time floor** —
`jasper-camilla-pipe-guard` (ExecStartPre) inspects the statefile config's
RawFile CAPTURE filename and, if it is an absent `/run` pipe, re-points the
statefile to the base config before camilla launches
(`event=camilla_pipe_guard.repaired reason=capture_pipe_absent`). The pipe-guard
is the durable floor: even if the runtime path is skipped (process killed before
leave-lean), the next restart cannot crash-loop.
**Test gap:** the string tests assert `type: RawFile` (+ `File` absent); the real
`camilladsp --check` gate runs on-device (the deploy's sound reconcile, now
env-hydrated so it sees the persisted coupling — [`jasper.cli.sound`](../jasper/cli/sound.py)).
**2026-07-01 result:** do **not** flip the default to `transport_pipe`. The
ordered dual-pipe arm and activation gate are useful safety work, but hardware
testing on the Pi 5 showed the page-sized FIFO floor and CamillaDSP's continuous
File-playback fill make this path miss the low-latency target structurally. Keep
it default-off while the next clocked/frame-bounded transport is designed.

**FIFO format:** the lean pipe carries full **S32_LE @ 48 kHz stereo** (the
usbsink bridge's normal snd-aloop lane uses the high-16 S16 view; the FIFO must
*not* — the RawFile capture declares `format: S32_LE` explicitly). One owner of
the path: `DEFAULT_LEAN_CAPTURE_FIFO` (`/run/jasper-usbsink/lean.pipe`).

## What's shipped vs owed

| Stage | What | State |
|---|---|---|
| 0 | snapcast bond buffer routed via `--stream.buffer` (was an inert URL param; bonds silently ran the 1000 ms default) | shipped |
| 2 | USB-bridge latency knobs (`JASPER_USBSINK_{QUEUE_MAXBLOCKS,LATENCY,BLOCK_FRAMES}`) | shipped, on-device tuning owed |
| 4a | File-capture CamillaDSP emitter + fail-loud guards (stereo + active) | shipped, default-OFF |
| 4b-i | `decide_source_low_latency_route` shared source policy + `low_latency_feature_flags` opt-in parsing ([`jasper.audio_runtime_plan`](../jasper/audio_runtime_plan.py)); mux consumes the plan layer directly | shipped, wired to mux consumers |
| 4b-ii | usbsink FIFO-output mode (`JASPER_USBSINK_OUTPUT_MODE=fifo`; env action owned by `jasper.audio_runtime_plan.usbsink_output_mode_action`) | shipped, default-OFF |
| 4b-iii | stage + validate + classify the lean config (`jasper.sound.runtime.stage_lean_capture_config`) — emit + `--check` + `classify_camilla_graph`, **no live-load** | shipped, default-OFF |
| 4b-iv | the **live** lane-switch: re-emit the lean config through the carrier (preserving room PEQs + trim, `jasper.sound.runtime.apply_lean_capture_config`), arm the usbsink FIFO output at runtime (`jasper.usbsink.output_mode_reconcile` → writes `JASPER_USBSINK_OUTPUT_MODE` to `/var/lib/jasper/usbsink.env` + restarts via the broker), and swap/restore via mux `_tick` (shared source-route decision → `Mux._enter_lean`/`_leave_lean` ladders, fail-loud → buffered) | removed 2026-07-10, see callout above |
| 5 | shairport-sync built `--with-pipe` (capable binary; runtime AirPlay pipe lane is future, #1318-gated) | shipped, dormant |
| 6 | `jasper-doctor` DAC USB sync-mode advisory (clock-coherence signal, *not* the chip-AEC gate) | shipped |
| 7 | **fan-in → CamillaDSP transport-pipe coupling** (`JASPER_FANIN_CAMILLA_COUPLING=transport_pipe`) — the shared-path dual-pipe lab: fan-in writes S32_LE to Camilla RawFile capture, Camilla writes S32_LE File playback to outputd's local pipe; transport ([`jasper-fanin/src/fifo.rs`](../rust/jasper-fanin/src/fifo.rs), [`jasper-outputd/src/local_content_pipe.rs`](../rust/jasper-outputd/src/local_content_pipe.rs)) + flag ([`jasper-fanin/src/config.rs`](../rust/jasper-fanin/src/config.rs) `Coupling`) + generator helper ([`jasper.fanin_coupling`](../jasper/fanin_coupling.py)) | shipped, default-OFF; dual-pipe `RawFile`/`File` contract fixed + tests; ordered arm/disarm reconciler (`jasper-fanin-coupling-reconcile`) + doctor drift check; **hardware-demoted as the low-latency endgame on 2026-07-01 because the 16 KiB pipe floor and continuous File-playback fill add too much latency** |

**Going live is soak-gated.** `JASPER_LEAN_LANE` is opt-IN
(`=enabled`), default-OFF, and is an *experiment knob* until a **24 h on-device
zero-xrun soak** passes — then it graduates to a prose-commented `.env.example`
entry. Until then it is allowlisted in
`tests/test_env_vars_codified.py::_UNCODIFIED`. 4b-iii/iv have landed
(default-OFF); the soak is the remaining gate.

**Live swap (4b-iv) carrier-fidelity + ladder.** The live lane-switch must NOT
load the 4b-iii staged config — that one is preference-ONLY and would drop the
household's room correction. `apply_lean_capture_config` instead re-emits the
lean File-capture config THROUGH the graph carrier
([`jasper.sound.graph_carrier`](../jasper/sound/graph_carrier.py),
`reemit(..., capture_kwargs=...)`), so the preserved room PEQs + output trim
ride along exactly like the durable `/sound` apply, then performs CamillaDSP's
glitch-free `set_config_file_path` swap via `apply_dsp_config`. The lean lane is
refused (typed `CarrierCannotHostEq`) on any non-solo-stereo-host graph
(active / program-bake / unknown), so it can never collapse a roleful graph.
`Mux._tick` computes one shared source-route decision after the fan-in handoff
settles (AUTO mode only; manual/test lanes route buffered). The lean consumer
runs the enter-lean ladder (arm FIFO → carrier-preserved config swap; fail-loud
→ disarm + buffered, with a per-episode re-arm block so a failure can't
restart-storm the usbsink daemon) or the leave-lean ladder
(`restore_buffered_config` re-emits the buffered config from saved intent —
restore ALWAYS succeeds by construction — then disarms the FIFO; NO-OP fast path
when not on the lean config). The adaptive-buffer consumer uses the same source
decision to shrink/restore fan-in's output buffer, so the exclusive-USB policy no
longer has two homes.

**Systemd hardening dependency:** because mux owns the live lean swap while
running under `ProtectSystem=strict`, `jasper-mux.service` MUST include
`/var/lib/camilladsp/configs` in `ReadWritePaths`. Without that grant, mux can
arm FIFO and restart `jasper-usbsink`, then fail before `apply_dsp_config` can
create `.dsp_apply.lock` / `sound_lean_current.yml`, producing audible USB
dropouts from repeated FIFO arm/rollback restarts. Guarded by
`tests/test_mux.py::test_mux_service_can_write_lean_camilla_config_dir`.

**FIFO runtime ownership dependency:** `jasper-usbsink` owns creation of
`/run/jasper-usbsink/lean.pipe`, but mux runs the CamillaDSP preflight before
loading the lean config. The pipe must therefore be group-readable by `jasper`
(`root:jasper`, `0660`), not root-only, or `camilladsp --check` rejects the
config with `Permission denied` and mux falls back through audible restarts.
Guarded by `tests/test_usbsink_fifo_writer.py::test_ensure_fifo_publishes_pipe_to_jasper_group`.

**Idle is not a lean-leave signal by itself:** `jasper-usbsink`'s published
`playing` bit is RMS-based and can drop during quiet passages. Once mux has
entered the FIFO lane, an `idle` route only unwinds lean when the USB sink state
is stale/missing or the gadget is gone; a fresh, connected-but-quiet USB state
keeps the FIFO lane latched. Competing sources, manual pins, and diagnostic
lanes still leave immediately. Guarded by
`tests/test_mux.py::test_lean_idle_leave_deferred_when_usb_state_is_fresh` and
`tests/test_source_state.py::test_usbsink_fresh_host_connected_accepts_quiet_connected_state`.

## Current JTS2 low-latency AirPlay budget (2026-06-29)

JTS2's live low-latency Apple-dongle path after the DAC-floor retune:

| Segment | Frames | Time @ 48 kHz |
|---|---:|---:|
| CamillaDSP target above chunk (`1536 - 256`) | 1280 | 26.7 ms |
| fan-in output queue | 1024 | 21.3 ms |
| outputd DAC buffer | 512 | 10.7 ms |
| **Configured downstream delay** | **2816** | **58.7 ms** |

That is the value shairport compensates:
`audio_backend_latency_offset_in_seconds = -0.058667`.

The live outputd STATUS on the same run reported DAC presentation delay around
20.7-21.3 ms. If you use that measured presentation counter instead of the
configured 512-frame DAC queue, the end-to-DAC estimate is about 68.7-69.3 ms.
Keep the two numbers separate: the shairport offset is configured from the
known downstream buffers, while the STATUS counter includes the live ALSA/DAC
presentation sample.

Audio stability evidence is not A/V sync evidence. The 1024-frame fan-in output
queue had clean audio counters, but computer-video AirPlay still showed
lip-sync problems by user observation. Do not call the AirPlay video path done
until it has a dedicated A/V measurement or Apple-side Wireless Audio Sync
calibration pass.

## Stage 7 — fan-in → CamillaDSP transport-pipe coupling (demoted lab path)

The lean lane (Stage 4) bypasses the fan-in **mixer** entirely for a single
exclusive wired source. The transport-pipe coupling was built as the attempted
convergence path for the **shared** mixer: the FULL fan-in mixer keeps running
(every renderer lane, TTS, ducking, the music-only tap) and only the local
program transport through CamillaDSP changes. Today fan-in writes the ALSA
snd-aloop substream
(`hw:Loopback,0,7`) and CamillaDSP dsnoop-captures it (`plug:jasper_capture`) —
~64 ms of loopback ring + a dsnoop hop. Under
`JASPER_FANIN_CAMILLA_COUPLING=transport_pipe`, fan-in writes a bounded S32_LE
stereo pipe (default `/run/jasper-fanin/camilla.pipe`) to CamillaDSP RawFile
capture, CamillaDSP writes S32_LE stereo File playback to outputd's local pipe
(default `/run/jasper-outputd/content.pipe`), and outputd drains that pipe once
per DAC period before its blocking DAC write. CamillaDSP `enable_rate_adjust` is
false and no async capture resampler is emitted; the pipes are transport only,
and the DAC write is the pace root. The outputd pipe is S32_LE because JTS's
Pi kernel uses 16 KiB pages, which made the previous S16_LE FIFO floor 4096
frames (~85 ms); S32_LE halves the same 16 KiB FIFO to 2048 frames (~43 ms),
with outputd down-converting to i16 only at the DAC boundary.

**2026-07-01 hardware result:** this is **not** the low-latency endgame. The Pi
5 pipe floor is exactly 16 KiB (`getconf PAGESIZE=16384`), so the intended
"small bounded pipe" cannot become a loopback-scale 128/256-frame transport.
CamillaDSP File playback also fills outputd's local FIFO continuously, which
turns the FIFO into a persistent latency reservoir rather than a tight handoff.
A reader-side backlog-drop experiment in outputd re-anchored constantly and
made audio audibly bad, proving that "drop stale FIFO" is not a safe fix. Keep
this path default-off and do not delete the lean lane / adaptive-shrink on its
behalf.

**Active-leader grouping exception.** Do not arm transport_pipe while a speaker
is an active multiroom leader. The current active-leader program bake is a
`File`→`SNAPFIFO` sink with `enable_rate_adjust: false` and intentionally keeps
capturing the ALSA fan-in loopback; if the local transport_pipe were partially
armed there, one side of camilla#1's graph would still belong to the grouped
Snapcast topology while the other belonged to the local outputd pipe. The guard
is codified twice: `jasper.multiroom.active_leader_config.precheck_active_leader`
refuses to form an active-leader bond while the persisted coupling is
`transport_pipe`, and `jasper.fanin.coupling_reconcile.reconcile_coupling`
refuses/reverts a transport_pipe arm while the box is already an active leader.
Keep such pairs on `loopback` until the grouped active-leader transport-pipe
topology is explicitly designed.

**Pacing lesson.** The mixer loop is paced ENTIRELY by its final blocking write;
there is no sleep in `run()`. The coupling swaps the blocking ALSA `writei` for a
blocking pipe `write`. That did propagate backpressure, but the buffer being
backpressured is a page-granular byte stream, not a frame-bounded realtime ring.
On this Pi that means 2048 frames for S32_LE stereo per FIFO when full, plus
Camilla chunking and outputd DAC delay. The correct next transport must preserve
DAC-paced backpressure while sizing the handoff in frames.

**Format split (load-bearing).** fan-in mixes/outputs S16_LE internally;
the shared capture is S32_LE. So the FIFO writer WIDENS each i16 sample to
i32-LE (high-16 promotion, lossless, the same scaling the loopback `plug:` did),
and the emitted File capture declares S32_LE. The wire format is pinned in
[`jasper.fanin_coupling.PIPE_WIRE_FORMAT`](../jasper/fanin_coupling.py) so the
Rust producer and the Python config consumer can never drift.

**Reader-gone resilience (CamillaDSP reload).** Rust's std runtime sets SIGPIPE
to `SIG_IGN`, so a write to a reader-gone pipe returns `EPIPE` rather than
killing the process (we do NOT re-arm `SIG_DFL`). The writer handles `EPIPE`
in-band: close the fd, reopen reader-first (non-blocking, `ENXIO`-retried, then
clear `O_NONBLOCK` to block-and-pace) on the next turn, dropping the in-flight
period. Each no-reader/reopen turn is bounded to ≤200 ms and returns `Waited` so
the loop bumps the heartbeat and re-checks shutdown — it can never hot-spin nor
wedge past the watchdog stale threshold. The pipe size is set with
`F_SETPIPE_SZ` (best-effort; the kernel rounds up to a power-of-two ≥ page size)
and the requested-vs-actual is logged (`event=fanin.fifo.pipe_sized`).

**AEC note.** Production AEC reads outputd's UDP monitor (`:9891`), so removing
the fan-in loopback write under `transport_pipe` does not break production AEC. It DOES
disable the `jasper_ref`/`jasper_capture` dsnoop diagnostic fallback — acceptable
(fallback/diagnostic only), but worth knowing during the soak.

**What's built vs learned.** Built and proven default-inert: the Rust transport
([`fifo.rs`](../rust/jasper-fanin/src/fifo.rs),
[`local_content_pipe.rs`](../rust/jasper-outputd/src/local_content_pipe.rs)), the `Coupling` flag with
fail-safe normalization matching Python ([`config.rs`](../rust/jasper-fanin/src/config.rs)),
the generator helper that returns the dual-pipe kwargs under `transport_pipe`
and `{}` (byte-identical) under `loopback`
([`jasper.fanin_coupling`](../jasper/fanin_coupling.py)).
**Live-armed (flag-gated; hardware-demoted, not default-bound):**
the reconcile / sound / correction emit paths now ask
`jasper.audio_runtime_plan.fanin_coupling_capture_kwargs()` and thread the result
through the carrier, so a `transport_pipe` box puts both the RawFile capture and
File playback pipe into the config CamillaDSP actually loads — and the
flat-profile reconcile noop is coupling-aware so it arms even a flat speaker.
Default `loopback` → `{}` → byte-identical emit, and the Rust side defaults to
`Coupling::Loopback` (the `FifoWriter` is never constructed) — so unset is
provably inert. The ORDERED arm/disarm is owned by
[`jasper.fanin.coupling_reconcile`](../jasper/fanin/coupling_reconcile.py)
(CLI `jasper-fanin-coupling-reconcile`): arm restarts outputd, restarts fan-in,
then reconciles camilla; disarm reconciles camilla then restarts fan-in and
outputd; an arm failure rolls the box back to loopback. `jasper-doctor`'s
`check_fanin_coupling` flags persisted-vs-loaded capture/playback drift.
Hardware learning: do not default this path on, and do not use a 24 h soak to
graduate it unless the transport primitive changes. The next gate belongs to a
clocked/frame-bounded transport: stable audio, no runaway latency during
`/sound/` changes, CPU-stress stability, and a measured click-in/capture-back
sample count below 60 ms all-in.

## AEC and DAC clock ownership

`jasper-outputd` should remain the physical-DAC owner for the shippable
multi-DAC architecture. That is where async USB, synchronous USB, I2S/HAT clock
behavior, buffer sizing, htimestamp/delay sampling, xrun recovery, and DAC-profile
policy belong. A CamillaDSP-owns-DAC path may be a useful latency feasibility
spike, but do not accidentally make it the product architecture without
re-deciding outputd's AEC-reference and hardware-profile responsibilities.

AEC follows the same clock-domain rule. Both stacks need a reference that matches
what is acoustically emitted **after** CamillaDSP, and a stable relationship
between that reference and the mic stream.

- Software AEC3 is the DAC-agnostic fallback: tap the post-Camilla reference
  from outputd and explicitly resample/align mic capture into the reference/DAC
  domain when the mic and DAC do not share a clock.
- Chip AEC is best only for hardware profiles whose reference input and actual
  speaker output are clock-coherent. With an external DAC on a different clock,
  the chip reference can drift away from the acoustic output; gate chip-AEC
  claims by profile evidence, not by wishful routing.
- A future custom HAT with ADC + DAC on one master clock is the cleanest voice
  hardware shape because it deletes mic/speaker clock drift by construction.

## Architecture rules and design forks

- Borrow PipeWire/JACK concepts where they fit: graph-shaped nodes/ports, a
  single driver/pacer per graph cycle, fixed quantum in frames, shared-memory
  rings, explicit latency accounting, and policy separated from transport.
- Do not adopt a full PipeWire/WirePlumber stack casually on the 1 GB Pi; it is
  still a resource/operational risk. Prototype it only if the tuned ALSA path or
  a purpose-built frame ring cannot hit the target, or if cross-domain AEC forces
  a unified graph.
- If JACK/PipeWire is prototyped, decide explicitly whether outputd becomes a
  graph sink/node or whether CamillaDSP/PipeWire owns the physical DAC. This is
  a clock-ownership decision, not a transport detail.
- snd-aloop is FULL (8/8 substream pairs) — a new lane must be a pipe/socket,
  never a 9th pair. Reusing existing loopback lanes with tighter period/buffer
  settings is still allowed as a near-term latency spike.
- Keep the fan-in input ring for networked sources (the WiFi-burst absorber).
  For USB input, evaluate direct gadget capture separately; it may remove one
  ingress boundary but still needs one host-clock → DAC-clock rate matcher.
- Never saturate all Pi cores while measuring — the hardware watchdog reboots a
  fully-wedged userspace. Measure under realistic 2-of-4-core load.

## AirPlay bonded lip-sync (open)

Stage 0 is a strict latency win (1000 ms → configured) **and** a disambiguating
experiment. Whether shairport's local offset propagates to the bonded playout is
**decoupled** in theory (snapcast re-timestamps on its own monotonic clock); the
only way to settle it is to **measure bonded Apple-TV A/V** after Stage 0. Until
that measurement exists, do not treat the offset as the bonded fix.

---

Last verified: 2026-07-07 (scoped to the resilience/routing-policy claims
below, NOT to the latency numbers the banner above marks superseded — for
current measured latency see
[HANDOFF-usb-latency-measurement.md](HANDOFF-usb-latency-measurement.md).
Ring route-policy/current-chain text rechecked
against `jasper.audio_runtime_plan`, `jasper.fanin_coupling`, and
`jasper.fanin.coupling_reconcile`; prior 2026-07-06 `outputd.env`
config-shear resilience rechecked
against the runtime plan, staged audio-hardware reconcile writer, and outputd
failure helper, including content and DAC buffer/period validation; prior ring
checkpoint and jts.local tuning evidence from
2026-07-02 found the stable loopback floor:
Rust bridge 256/3, fan-in USB resampler held target 2048, fan-in output 1024,
CamillaDSP 256/1536, outputd 128/256, outputd content buffer 1536. This is not a
40 ms end-to-end route; route-latency evidence remains missing. 2026-07-01
`jasper.audio_runtime_plan` / `jasper-audio-config
explain` / `jasper-audio-config outputd-floor-actions` / `jasper-doctor`
runtime-plan check added as the SSOT layer; numeric lab override artifact added
while fan-in coupling remains ordered-reconciler-owned; audio-hardware, usbsink
output-mode, sound capture intent/precedence, and fan-in buffer / coupling
writers plus mux low-latency source routing consume the plan; JTS2 low-latency
Apple-dongle AirPlay budget
documented: configured downstream delay 58.7 ms at Camilla 256/1536,
fan-in output 1024, outputd DAC 512; 512-frame fan-in output failed fast.
2026-07-01: transport-pipe reconcile now runs a short live activation gate, but
JTS hardware testing demoted the dual-FIFO path as the low-latency endgame:
Pi 5 page size floors FIFOs at 16 KiB, Camilla File playback continuously fills
the outputd local pipe, and reader-side backlog re-anchor caused continuous
audio drops. Documented the replacement direction: outputd owns DAC clock,
foreign sources get one explicit rate matcher, remaining post-ingress
boundaries must be clocked/frame-bounded, and sub-60 ms USB → DAC must be
proved by click-in/capture-back measurement.
2026-06-27 4b-iv live lane-switch shipped default-OFF:
carrier-preserved `apply_lean_capture_config` / `restore_buffered_config`,
the `output_mode_reconcile` runtime FIFO arm, and the `Mux._tick`
enter/leave-lean ladders — all hardware-free-tested; 24 h on-device soak owed
before graduating `JASPER_LEAN_LANE` out of the experiment allowlist. 4b-iii
stage_lean_capture_config + lean-lane emitter + FIFO mode + decision policy
landed earlier; resampler v4 object schema confirmed against the CamillaDSP
v4.1.3 config reference; outputd-unchanged topology confirmed against
`camilla_config_contract.DEFAULT_PLAYBACK_DEVICE` + `rust/jasper-outputd`).
