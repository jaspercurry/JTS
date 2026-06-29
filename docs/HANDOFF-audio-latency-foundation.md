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
- **CamillaDSP** owns `chunksize` / `target_level` (config-baked in
  [`jasper/camilla_config_contract.py`](../jasper/camilla_config_contract.py)).
- **jasper-outputd** is the final-output owner: a blocking DAC write is the
  timing master; the content lane is read non-blocking (absent content → silence).
  Both AEC references are produced here (software AEC3 → 48 kHz UDP `:9891`;
  chip-AEC → 16 kHz USB-IN), so **nothing can delete `outputd`**.
- **DAC buffer** is env-tunable (`JASPER_OUTPUTD_DAC_BUFFER_FRAMES`); measured
  ~16 ms reliable on both the HiFiBerry DAC8x and the Apple dongle — *not* the
  bottleneck.

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
| 4b-i | `decide_lean_route` pure routing policy ([`jasper/lean_lane.py`](../jasper/lean_lane.py)) | shipped, unwired |
| 4b-ii | usbsink FIFO-output mode (`JASPER_USBSINK_OUTPUT_MODE=fifo`) | shipped, default-OFF |
| 4b-iii | stage + validate + classify the lean config (`jasper.sound.runtime.stage_lean_capture_config`) — emit + `--check` + `classify_camilla_graph`, **no live-load** | shipped, default-OFF |
| 4b-iv | the **live** lane-switch: re-emit the lean config through the carrier (preserving room PEQs + trim, [`jasper.sound.runtime.apply_lean_capture_config`](../jasper/sound/runtime.py)), arm the usbsink FIFO output at runtime ([`jasper.usbsink.output_mode_reconcile`](../jasper/usbsink/output_mode_reconcile.py) → writes `JASPER_USBSINK_OUTPUT_MODE` to `/var/lib/jasper/usbsink.env` + restarts via the broker), and swap/restore via mux `_tick` (`decide_lean_route` → `Mux._enter_lean`/`_leave_lean` ladders, fail-loud → buffered). **The camilla-config apply/restore is DELEGATED** ([`jasper.sound.lean_apply_reconcile`](../jasper/sound/lean_apply_reconcile.py) → the `jasper-lean-apply` root oneshot, blocking-started via the broker) — the non-root `jasper-mux` cannot write `/var/lib/camilladsp/configs` (EROFS), so it never applies in-process | shipped, default-OFF, **24 h soak owed** |
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
`Mux._tick` calls `decide_lean_route` after the fan-in handoff settles (AUTO
mode only; manual/test lanes route buffered) and runs the enter-lean ladder
(arm FIFO → carrier-preserved config swap; fail-loud → disarm + buffered, with a
per-episode re-arm block so a failure can't restart-storm the usbsink daemon) or
the leave-lean ladder (`restore_buffered_config` re-emits the buffered config
from saved intent — restore ALWAYS succeeds by construction — then disarms the
FIFO; NO-OP fast path when not on the lean config).

**Privileged delegation (the EROFS fix).** Both legs' *config swap* writes
`/var/lib/camilladsp/configs` (the generated YAML + the shared `.dsp_apply.lock`).
`jasper-mux` runs as the non-root `jasper-mux` user under `ProtectSystem=strict`
and does NOT own that dir (only `jasper-web` does, per WS1 Phase 3b-3), so an
in-process apply `[Errno 30] EROFS`-fails on a privilege-separated box. The mux
therefore DELEGATES the apply/restore to the `jasper-lean-apply` root oneshot —
mirroring how it already delegates the usbsink FIFO restart through the restart
broker. `Mux._lean_apply_config`/`_lean_restore_config` →
[`jasper.sound.lean_apply_reconcile.delegate`](../jasper/sound/lean_apply_reconcile.py)
writes the intent (`enter`|`leave`) to `/var/lib/jasper/lean.env` (a path the mux
owns) and BLOCKING-starts `jasper-lean-apply.service` via the broker `start`
verb (`no_block=False`, so the oneshot's exit code is the synchronous
success/failure verdict the ladder needs). The oneshot
([`jasper-lean-apply`](../jasper/sound/lean_apply_reconcile.py) `main`) runs the
real `apply_lean_capture_config`/`restore_buffered_config` at full privilege.
`jasper-lean-apply.service` is a broker `START_ONLY_UNITS` entry — the mux may
only START it. The mux gains NO `camilladsp/configs` write; the privilege
boundary stays intact. The leave-lean ordering invariant is preserved: the FIFO
is disarmed only AFTER the delegated restore returns ok.

**usbsink FIFO no-reader hardening.** Because enter-lean arms the FIFO BEFORE
the camilla apply opens the read end, the usbsink writer has a normal bounded
no-reader window. The writer thread now advances a `fifo_waiting_reader` liveness
tick on every `ENXIO` retry, and the daemon's fifo-mode watchdog sentinel is
`fifo_writes + fifo_waiting_reader` — so "waiting for the reader" counts as
forward progress and the unit does NOT go watchdog-stale and crash-loop while the
apply is in flight (previously 3 restarts in ~9 s, then unit failed). A genuinely
dead writer (neither counter moving) still trips the watchdog so systemd can
recover.

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
sound / correction emit paths now thread `coupling_capture_kwargs_from_env()`
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

Last verified: 2026-06-28 (4b-iv camilla apply/restore DELEGATED to the
`jasper-lean-apply` root oneshot via the restart broker — the non-root
`jasper-mux` cannot write `camilladsp/configs` (EROFS); usbsink FIFO no-reader
window hardened against watchdog crash-loop. Earlier 4b-iv landing, 2026-06-27:
carrier-preserved `apply_lean_capture_config` / `restore_buffered_config`,
the `output_mode_reconcile` runtime FIFO arm, and the `Mux._tick`
enter/leave-lean ladders — all hardware-free-tested; 24 h on-device soak owed
before graduating `JASPER_LEAN_LANE` out of the experiment allowlist. 4b-iii
stage_lean_capture_config + lean-lane emitter + FIFO mode + decision policy
landed earlier; resampler v4 object schema confirmed against the CamillaDSP
v4.1.3 config reference; outputd-unchanged topology confirmed against
`camilla_config_contract.DEFAULT_PLAYBACK_DEVICE` + `rust/jasper-outputd`).
