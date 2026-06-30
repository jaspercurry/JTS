# Handoff: JTS audio-latency foundation

Canonical reference for JTS's local-audio-latency work: lowering latency on
the music path while keeping the speaker resilient and supporting flexible
output/mic hardware. Read this before touching the lean lane, the USB-input
bridge latency, or the snapcast bond buffer.

**Targets:** USB-audio-input lip-sync under ~60 ms (including CamillaDSP);
AirPlay (Apple TV → bonded pair) staying within the ~2 s presentation budget;
and, in general, *only* adding latency where a specific piece of hardware
genuinely requires it.

---

## The chain, and where latency lives

```
renderer → snd-aloop fan-in ring → jasper-fanin → (capture) → CamillaDSP
         → outputd_content_playback (snd-aloop) → jasper-outputd → DAC
```

- The **fan-in input ring** (~85 ms) is the WiFi-burst absorber — load-bearing
  for networked sources (AirPlay/Spotify), *not* needed by a wired USB source.
- The **fan-in output queue** is fixed downstream latency. As of the
  2026-06-29 JTS2 retune, the loopback-path production floor is 1024 frames
  (~21.3 ms at 48 kHz). A 512-frame trial failed fast with fan-in output
  xruns, so sub-1024 remains lab-only.
- **CamillaDSP** owns `chunksize` / `target_level` (config-baked in
  [`jasper/camilla_config_contract.py`](../jasper/camilla_config_contract.py)).
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
and coupling route-support policy. Mux's lean-lane and adaptive-buffer consumers
share the plan's `decide_source_low_latency_route` source-exclusivity decision,
and `jasper.usbsink.output_mode_reconcile` asks the plan for the
`JASPER_USBSINK_OUTPUT_MODE` env action. Sound runtime asks the plan for both
lean RawFile capture kwargs (`lean_capture_kwargs`) and shared fan-in coupling
capture kwargs (`fanin_coupling_capture_kwargs`), so the RawFile/AsyncSinc shape
does not live in separate staged/live/reconcile code paths. The carrier also
asks the plan's `apply_capture_precedence` helper whether lean capture, grouped
pipe-sink playback, or shared fan-in coupling owns capture for this emit. Other
reconcilers still write their existing env files; move those decisions behind
the plan as the next migration steps.

## The lean lane (Stage 4)

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
CLI `jasper-fanin-coupling-reconcile <loopback|fifo>`):

1. **Fan-in-first arm ordering — BUILT.** The apply's `camilladsp --check` (and
   the load) OPENS the pipe, so on ARM the reconciler writes `=fifo` → restarts
   fan-in (creates+writes the pipe) → reconciles CamillaDSP (RawFile). On DISARM
   it reverses: reconcile CamillaDSP to Alsa FIRST → then restart fan-in to
   loopback, so disarming never strands camilla on a RawFile config whose pipe
   has lost its writer (the crash-loop). Any ARM failure rolls the whole box back
   to loopback. On a clean reboot the systemd order (fan-in `Before` camilla)
   gives the same rendezvous, so an armed box survives a cold boot with no
   reconciler run (validated on jts5: `camilla NRestarts=0`).
2. **Reconnect (RawFile EOF) — characterized + self-healing.** fan-in
   auto-reopens the pipe on `reader_gone` (a CamillaDSP reload); a fan-in restart
   EOFs camilla's RawFile capture, which self-heals via camilla's own restart
   (a brief gap, `reopen_count`/`dropped_periods` in `/state`). A *coordinated*
   reload (so a fan-in bounce is gap-free) is a possible smoothing follow-up, not
   a blocker.

**Observability:** `/state .fanin.output.fifo.{path,requested/actual_pipe_bytes,
dropped_periods,reopen_count}` + transport; `jasper-doctor`'s
`check_fanin_coupling` warns when the persisted intent (`fanin.env`) and the
loaded CamillaDSP capture disagree (the half-applied / crash-loop precursor).

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
**Owed before flipping the default to `fifo`:** the 24 h zero-xrun on-device soak
and the real `<60 ms` USB measurement (needs a wired USB source).

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
| 4b-iv | the **live** lane-switch: re-emit the lean config through the carrier (preserving room PEQs + trim, [`jasper.sound.runtime.apply_lean_capture_config`](../jasper/sound/runtime.py)), arm the usbsink FIFO output at runtime ([`jasper.usbsink.output_mode_reconcile`](../jasper/usbsink/output_mode_reconcile.py) → writes `JASPER_USBSINK_OUTPUT_MODE` to `/var/lib/jasper/usbsink.env` + restarts via the broker), and swap/restore via mux `_tick` (shared source-route decision → `Mux._enter_lean`/`_leave_lean` ladders, fail-loud → buffered) | shipped, default-OFF, **24 h soak owed** |
| 5 | shairport-sync built `--with-pipe` (capable binary; runtime AirPlay pipe lane is future, #1318-gated) | shipped, dormant |
| 6 | `jasper-doctor` DAC USB sync-mode advisory (clock-coherence signal, *not* the chip-AEC gate) | shipped |
| 7 | **fan-in → CamillaDSP FIFO coupling** (`JASPER_FANIN_CAMILLA_COUPLING=fifo`) — the SHARED-capture endgame: fan-in writes a bounded pipe, CamillaDSP File-captures it; transport ([`jasper/fanin/src/fifo.rs`](../rust/jasper-fanin/src/fifo.rs)) + flag ([`jasper/fanin/src/config.rs`](../rust/jasper-fanin/src/config.rs) `Coupling`) + generator helper ([`jasper.fanin_coupling`](../jasper/fanin_coupling.py)) | shipped, default-OFF; capture-type `RawFile` fixed + `--check`-validated on jts5; ordered arm/disarm reconciler (`jasper-fanin-coupling-reconcile`) + doctor drift check; **24 h soak + real `<60 ms` USB measurement owed before default-on** |

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

## Stage 7 — fan-in → CamillaDSP FIFO coupling (the SHARED-capture endgame)

The lean lane (Stage 4) bypasses the fan-in **mixer** entirely for a single
exclusive wired source. The FIFO coupling is the convergence endgame for the
**shared** path: the FULL fan-in mixer keeps running (every renderer lane, TTS,
ducking, the music-only tap) and only how its *output* reaches CamillaDSP
changes. Today fan-in writes the ALSA snd-aloop substream (`hw:Loopback,0,7`)
and CamillaDSP dsnoop-captures it (`plug:jasper_capture`) — ~64 ms of loopback
ring + a dsnoop hop. Under `JASPER_FANIN_CAMILLA_COUPLING=fifo`, fan-in writes a
small bounded named pipe (default `/run/jasper-fanin/camilla.pipe`) that
CamillaDSP File-captures with an async resampler + `enable_rate_adjust` (the real
DAC clock disciplines the clockless File capture — the same shape the lean lane
already uses). That trades the loopback ring for a ~3-period pipe (~21 ms @
48 kHz S32). **Once it soaks, it supersedes BOTH the lean lane and the adaptive
output-buffer shrink** — do NOT delete either yet (superseded *after* the soak,
not before).

**Active-leader grouping exception.** Do not arm FIFO coupling while a speaker is
an active multiroom leader. The current active-leader program bake is a
`File`→`SNAPFIFO` sink with `enable_rate_adjust: false` and intentionally keeps
capturing the ALSA fan-in loopback; if fan-in writes the FIFO instead, camilla#1
keeps reading the dead loopback and the pair goes silent. The guard is codified
twice: `jasper.multiroom.active_leader_config.precheck_active_leader` refuses to
form an active-leader bond while the persisted coupling is `fifo`, and
`jasper.fanin.coupling_reconcile.reconcile_coupling` refuses/reverts a FIFO arm
while the box is already an active leader. Keep such pairs on `loopback` until
the grouped active-leader FIFO capture topology is explicitly designed.

**Pacing.** The mixer loop is paced ENTIRELY by its final blocking write — there
is no sleep in `run()`. The coupling swaps the blocking ALSA `writei` for a
blocking pipe `write`: when CamillaDSP (DAC-paced) hasn't drained, the small pipe
fills and `write` blocks, giving the same DAC-paced backpressure at ~21 ms of
pipe depth. The write is IN-BAND on the RT mixer thread (NOT the usbsink
separate-thread + silence-synthesis shape — fan-in's mixer IS the producer and
pacer, so there is no queue to starve). The watchdog is fed by
`bump_progress()` after every `step()` exactly as today, including the bounded
reopen-wait turns.

**Format split (load-bearing).** fan-in mixes/outputs S16_LE internally;
the shared capture is S32_LE. So the FIFO writer WIDENS each i16 sample to
i32-LE (high-16 promotion, lossless, the same scaling the loopback `plug:` did),
and the emitted File capture declares S32_LE. The wire format is pinned in
[`jasper.fanin_coupling.FIFO_WIRE_FORMAT`](../jasper/fanin_coupling.py) so the
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
the fan-in loopback write under `fifo` does not break production AEC. It DOES
disable the `jasper_ref`/`jasper_capture` dsnoop diagnostic fallback — acceptable
(fallback/diagnostic only), but worth knowing during the soak.

**What's built vs owed.** Built and proven default-inert: the Rust transport
([`fifo.rs`](../rust/jasper-fanin/src/fifo.rs)), the `Coupling` flag with
fail-safe normalization matching Python ([`config.rs`](../rust/jasper-fanin/src/config.rs)),
the generator helper that returns the File-capture kwargs under `fifo` and `{}`
(byte-identical) under `loopback` ([`jasper.fanin_coupling`](../jasper/fanin_coupling.py)).
**Live-armed (flag-gated; soak owed before defaulting to `fifo`):** the reconcile /
sound / correction emit paths now ask
`jasper.audio_runtime_plan.fanin_coupling_capture_kwargs()` and thread the result
through the carrier, so a `=fifo` box puts the File capture into the config
CamillaDSP actually loads — and the flat-profile reconcile noop is coupling-aware
so it arms even a flat speaker (the MB1 fix; otherwise fan-in writes the pipe
while Camilla keeps the dead loopback → silent outage). Default `loopback` → `{}`
→ byte-identical emit, and the Rust side defaults to `Coupling::Loopback` (the
`FifoWriter` is never constructed) — so unset is provably inert. The ORDERED
arm/disarm is owned by [`jasper.fanin.coupling_reconcile`](../jasper/fanin/coupling_reconcile.py)
(CLI `jasper-fanin-coupling-reconcile`): arm restarts fan-in then reconciles
camilla; disarm reconciles camilla then restarts fan-in; an arm failure rolls the
box back to loopback. `jasper-doctor`'s `check_fanin_coupling` flags persisted-vs-
loaded drift. What remains before flipping the default to `fifo`: the 24 h
on-device zero-xrun **soak** (the ~1-period drop per Camilla reload, the usbsink
term, the real `<60 ms` measurement on jts5) — then delete the now-redundant lean
lane + adaptive-shrink.

## Optionality: chip-AEC AND software-AEC, each at the lean floor

Both AEC references come from `outputd`, so one "lean `outputd`" stage serves
both at the same latency floor. The per-AEC difference is *constraints, not
latency*: chip-AEC needs a USB-SOF-locked DAC plus a static
`AUDIO_MGR_SYS_DELAY` reference-delay re-pin; software AEC3 takes any DAC plus
Pi CPU. The chip's no-drift comes from the XVF USB-SOF PLL, not from snd-aloop
or `enable_rate_adjust` — so removing inter-stage rings is safe for it.

## Hard rules — do NOT re-architect

- Swap the engine/profile, **not** the topology. No PipeWire `module-echo-cancel`,
  no replacing snd-aloop with PipeWire fanout, no WirePlumber (multi-GB RAM
  runaways → OOM on the 1 GB Pi). Targeted single-knob OS fixes are fine *when
  measurement localizes the cause to that layer*.
- snd-aloop is FULL (8/8 substream pairs) — a new lane must be a pipe/socket,
  never a 9th pair.
- Keep the fan-in input ring for networked sources (the WiFi-burst absorber).
- Never saturate all Pi cores while measuring — the hardware watchdog reboots a
  fully-wedged userspace. Measure under realistic 2-of-4-core load.

## AirPlay bonded lip-sync (open)

Stage 0 is a strict latency win (1000 ms → configured) **and** a disambiguating
experiment. Whether shairport's local offset propagates to the bonded playout is
**decoupled** in theory (snapcast re-timestamps on its own monotonic clock); the
only way to settle it is to **measure bonded Apple-TV A/V** after Stage 0. Until
that measurement exists, do not treat the offset as the bonded fix.

---

Last verified: 2026-06-30 (`jasper.audio_runtime_plan` / `jasper-audio-config
explain` / `jasper-audio-config outputd-floor-actions` / `jasper-doctor`
runtime-plan check added as the SSOT layer; numeric lab override artifact added
while fan-in coupling remains ordered-reconciler-owned; audio-hardware, usbsink
output-mode, sound capture intent/precedence, and fan-in buffer / coupling
writers plus mux low-latency source routing consume the plan; JTS2 low-latency
Apple-dongle AirPlay budget
documented: configured downstream delay 58.7 ms at Camilla 256/1536,
fan-in output 1024, outputd DAC 512; 512-frame fan-in output failed fast.
2026-06-27 4b-iv live lane-switch shipped default-OFF:
carrier-preserved `apply_lean_capture_config` / `restore_buffered_config`,
the `output_mode_reconcile` runtime FIFO arm, and the `Mux._tick`
enter/leave-lean ladders — all hardware-free-tested; 24 h on-device soak owed
before graduating `JASPER_LEAN_LANE` out of the experiment allowlist. 4b-iii
stage_lean_capture_config + lean-lane emitter + FIFO mode + decision policy
landed earlier; resampler v4 object schema confirmed against the CamillaDSP
v4.1.3 config reference; outputd-unchanged topology confirmed against
`camilla_config_contract.DEFAULT_PLAYBACK_DEVICE` + `rust/jasper-outputd`).
