# Handoff: USB-in low latency — production `usb_low_latency_48k`

Operational truth for the USB-in route: the shipped topology, the settings that
are coupled to each other, how the route earns its low-latency claim, and the
combo arming/host-clock machinery around it. The route keeps USB inside the
shared fan-in → CamillaDSP → outputd protection path; it is not a bypass.

Neighbouring owners — do not restate them here:
[HANDOFF-usb-latency-measurement.md](HANDOFF-usb-latency-measurement.md) (how to
measure, the current measured numbers, the per-stage breakdown, what a fresh
install ships) · [HANDOFF-usbsink.md](HANDOFF-usbsink.md) (the USB source's data
plane and observed state) ·
[testing-tooling.md](testing-tooling.md#route-latency-clickcapture-harness) (the
harness architecture). Decisions: the evidence gate is
[ADR-0108](adr/0108-a-latency-claim-is-earned-by-a-measured-artifact.md), the
host-clock observable is
[ADR-0109](adr/0109-the-combo-host-clock-servo-observes-resampler-correction.md),
and the single-capture-pipeline rule is
[ADR-0107](adr/0107-usb-gadget-audio-has-one-capture-pipeline.md).

## Current production route

`usb_low_latency_48k` is the claiming profile. `jasper-fanin` DIRECT-captures
the gadget as the sole USB ingress. The capture is `S32_LE` at the gadget;
whether fan-in narrows it follows the box's program wire
(`Config::program_wire_is_wide`), which defaults wide — so the gadget's `i32`
reaches the summed write intact.

```
UAC2 gadget capture
  → jasper-fanin DIRECT capture (hw:UAC2Gadget, period 256 / buffer 768 requested, gadget rounds to 1024, ≈21.3 ms at 48 kHz — rust/jasper-fanin/src/mixer.rs)
  → jasper-fanin USB input resampler (target 512 + warm-up cushion, ring 4096)
  → Ring A program.ring (jts_ring_capture, 2 slots × 128 frames)
  → CamillaDSP protection/correction (chunk 128 / target 128 / queue 1, rate_adjust off)
  → Ring B content.ring (jts_ring_playback, 2 slots × 128 frames)
  → outputd final DAC owner + final-speaker reference
```

Ring-coupled geometry is one coherent set; do not change one value without the
others:

| Axis | Product default | Latency / reason |
|---|---:|---|
| Ring A `jts_ring_capture` | 2 slots × 128 frames | ≈5.3 ms at 48 kHz |
| CamillaDSP ring emit | chunk 128 / target 128 / queue 1 | chunk is one slot; target is one chunk |
| CamillaDSP ring `rate_adjust` | off | one-clock blocking chain; rate-adjust on Ring A+B packed the queues and measured ≈194 ms |
| Ring B `jts_ring_playback` | 2 slots × 128 frames | already minimal |

The shipped warm-up cushion default is **2048** frames
(`JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES`). It is the acquisition
ceiling the held target starts at and, with cushion decay armed, descends from
toward the 576-frame floor (`DEFAULT_CUSHION_DECAY_FLOOR_FRAMES`), so it shapes
cold-start descent and underrun margin — **not** the steady-state floor or the
measured steady numbers. jts.local runs `1536` as a box tuning. Both clear the
resampler churn floor by a wide margin (`target + cushion >= minimum_safe_fill +
period + 32` = 562 at the default geometry).

There is no outputd content-buffer knob: outputd reads Ring B directly and
never opens an ALSA content capture PCM, so the key that once sized one was
retired along with its `>= 2× period` validation. outputd's `/state` publishes
the honest Ring B capacity in `content.ring.capacity_frames` next to a
synthetic period-sized `content.buffer_frames`, and `jasper-doctor` validates
ring geometry rather than mis-applying the ALSA `>= 2× period` jitter floor to
the synthetic.

## Earning the claim

A low-latency claim is earned by a measured route-latency artifact, never by
configuration — the rule and its gates are
[ADR-0108](adr/0108-a-latency-claim-is-earned-by-a-measured-artifact.md).
Certification needs p95 ≤ 40 ms over ≥ 200 impulses / ≥ 5 minutes; p99
promotion needs ≥ 1000 jittered impulses over ≥ 30 minutes with p99 ≤ 42 ms.

**Short of that, the gate discloses rather than parks (ADR-0101).**
`route_latency_gate_status` runs every leg before it classifies once, so a
measured breach can never hide behind a disclosure:

- **fail** (`fix_route_latency_before_claim`) — no artifact at all, a measured
  `p95_exceeds_40ms`, an absent p95, `route_health_anomaly` or any `live_*`
  mismatch, and `route_binding_missing:fanin_direct_negotiated_buffer_frames`
  (presence of that binding is mandatory, never a compatible warning).
- **disclose** (warn, `run_route_latency_validation`) — facts about the
  *proof's* validity rather than the route's: `config_mismatch`,
  `p95_uncertified`, `artifact_stale`, `artifact_from_future`. Every issue
  token is preserved so a consumer still sees WHAT is stale, and the claim
  keeps running.
- **warn on the promotion ladder** — `p99_exceeds_42ms` gets
  `reduce_tail_latency_before_promotion`; `p99_missing` /
  `p99_uncertified` / `p99_spacing_unverified` get
  `run_p99_promotion_validation`.

Operationally:

`jasper-route-latency-harness` (source: `jasper/cli/route_latency_harness.py` +
`jasper/route_latency/`) produces the click-in/capture-back samples;
`sudo /opt/jasper/.venv/bin/jasper-route-latency-artifact` binds them to the live
route identity and writes `/var/lib/jasper/audio-validation/`. Invoke both by
absolute venv path — under `sudo` the venv `bin/` is not on `secure_path`.

```sh
# 1. Generate the click-track WAV + schedule (use `promotion` for the p99 gate).
/opt/jasper/.venv/bin/jasper-route-latency-harness generate quick --out-dir /tmp/route-latency

# 2. On the Pi: arm the tap, play the WAV on the host at a modest volume
#    (start very quiet — CamillaDSP's volume_limit stays the 0 dB ceiling),
#    then analyze and shell out to the artifact writer. `run` reads duration
#    and jitteredness off the schedule; those flags exist only on `analyze`.
sudo /opt/jasper/.venv/bin/jasper-route-latency-harness run \
  /tmp/route-latency/quick-schedule.json \
  --out-dir /tmp/route-latency \
  --invoke-artifact \
  --confirm-route-health-ok
```

Or drive the artifact writer directly once a samples file exists:

```sh
sudo /opt/jasper/.venv/bin/jasper-route-latency-artifact \
  --samples /tmp/route-latency/latency-samples.json \
  --duration-seconds 360 \
  --harness-id jts-click-capture-v1 \
  --route-health-ok
```

Only declare `--route-health-ok` when the same measurement window had complete,
clean fan-in/outputd telemetry: both live surfaces and the expected USB DIRECT
lane/counter shape present, with no fan-in USB resampler unlock/silence/overrun
and no outputd/fan-in xruns. `analyze` prints exactly that delta — every nonzero
fan-in/outputd counter change across the window — and states whether the
declaration *would* be justified; it never asserts it for you. The artifact
writer is strict mid-stream (an unlocked resampler fails artifact creation);
doctor is topology-aware in steady state (an explicitly `health:"idle"` direct
lane may read `locked:false`, but capturing/broken/unknown stays strict). A
static-identity change while idle is a disclosure, not a failure — see the
gate table above.

`run` defaults to `--tap-transport auto`, which always resolves to the fan-in
tap — the only ingress — and prints its choice first
(`tap transport=fanin path=/run/jasper-fanin/impulse-tap.jsonl (...)`).

## USB DIRECT (combo mode)

`JASPER_FANIN_USB_DIRECT=enabled` is the one live arming literal; anything else
fails safe to the idle aloop lane (`hw:Loopback,1,3`, unwritten → USB
unavailable, no crash).

| `FANIN_USB_DIRECT` | Result |
|---|---|
| `enabled` | **Armed (product default when USB is allowed).** Fan-in DIRECT-captures `hw:UAC2Gadget`; the oneshot marker proves bounded composition/card readiness. |
| off (`disabled`) | **Disarmed → USB unavailable.** Fan-in's `usbsink` lane falls back to the idle aloop until an `--auto` pass re-arms it. Observable as fan-in lane `source:"lane"`. |

Arming is derived, never hand-set. The fan-in USB reconciler
(`jasper.fanin.coupling_auto`) is
the **single writer** of the three fan-in keys (`JASPER_FANIN_USB_DIRECT`,
`JASPER_FANIN_HOST_CLOCK`, `JASPER_FANIN_RESAMPLER_CUSHION_DECAY`) into
`/var/lib/jasper/fanin.env`. It runs at boot and deploy, is kicked live by the
source coordinator after a USB lifecycle change (so a fresh enable arms this
session, not next reboot), and is kicked by grouping after a role apply. The
effective gate is canonical USB intent **and** current local-source role
permission, so a desired-On follower stays persisted On while its direct lane
remains disarmed until unpark. Off a combo box the reconciler writes the
explicit `disabled` value rather than unsetting — a stale `enabled` in
`/etc/jasper/jasper.env` loads first and would otherwise win.

**A combo arm/disarm restart is CamillaDSP-coordinated.** A bare fan-in restart
used to SIGKILL CamillaDSP: camilladsp captures
Ring A through the `jts_ring_capture` ioplug, and when fan-in's ring writer
detached, the capture reader busy-spun a core until camilladsp (`SCHED_FIFO`,
`LimitRTTIME=200000` µs) hit the kernel `RLIMIT_RTTIME` hard SIGKILL ~213 ms
later, cascading through `Restart=always` → start-limit → `OnFailure` → a full
core-graph bounce. The reconciler pauses
CamillaDSP with a clean SIGTERM before the fan-in restart and resumes it after
fan-in re-signals `READY=1` — mirroring the order `jasper-camilla-recover`
already proves.
The root cause was also fixed underneath: `c/jts-ring-ioplug` now does its
per-wake service work (timerfd drain plus wall-clock-paced silence arm) in
`capture_service_tick()`, called from both `poll_revents` and the capture
`pointer` callback, because camilladsp's ALSA backend raw-polls the descriptor
and never calls `snd_pcm_poll_descriptors_revents`. An uncoordinated fan-in
death now degrades to ≤2 s of capture silence instead of a kill cascade.

**Arbitration is fan-in-native.** Mux single-`SELECT`s the winner lane on every
auto-mode reconciliation, and the DIRECT usbsink lane passes the same per-lane
`input_selected`/`lane_mix_contributes` gate as every other lane — a non-winner
USB lane has never layered under a winner. On top of that, mux sends
`MUTE`/`UNMUTE usbsink` over fan-in's control socket, and fan-in drops the
lane's contribution at the **mix stage** only: the lane keeps reporting pre-mute
`frames_read`/`rms_dbfs`, so a muted-but-streaming host still reads as active
(no mute → "stopped" → release flap). Since the `:8781` preempt path was
deleted this is the only preempt transport left.
`JASPER_USBSINK_PREEMPT=disabled` skips the `MUTE` call; the SELECT gate still
excludes the losing lane from the sum.

Mux sees USB streaming from fan-in's own DIRECT-lane liveness: fan-in samples
the counter at 20 Hz off the audio thread, publishes `direct.streaming`, and
sends an edge-only `NOTIFY usbsink` wake hint. Start detection is ~0–50 ms; 2 s
of flat samples clears it. There is **no audio-level gate** — USB's frame-flow
edge participates in the same source-neutral latest-start-wins policy as every
other source (`USBSINK_PLAYING_RMS_DBFS` survives as a `/state` level readout
only, pinned by `tests/test_usbsink_playing_rms_contract.py`).

## Host-slaved USB clock in combo mode

The host-slaved clock steers the gadget's `Capture Pitch 1000000` ctl so the
host tracks the DAC clock. The invariant is *the daemon that owns the gadget
capture owns the pitch ctl*, and since the aloop solo path was deleted there is
exactly one owner: fan-in, on a dedicated `fanin-host-clock` thread, behind
`JASPER_FANIN_HOST_CLOCK=enabled`. The servo core is the shared
`rust/jasper-host-clock` crate; its observable is the resampler's own correction
ppm and its outer law is a single slow integrator — rationale and the rejected
DLL are
[ADR-0109](adr/0109-the-combo-host-clock-servo-observes-resampler-correction.md).
The setpoint is the resampler's HELD target
(`…_TARGET_FRAMES + …_WARMUP_CUSHION_FRAMES`), shared with the inner rate
controller so the loops do not fight; the bandwidth-separation derivation lives
in the crate's module docstring.

| `FANIN_HOST_CLOCK` | `FANIN_USB_DIRECT` | Result |
|---|---|---|
| off (default) | any | **Inert.** No thread; `/state` fan-in `host_clock.enabled=false`. |
| `enabled` | `enabled` | **Combo target.** Per-session probe → L0 pins the DIRECT lane fill at target. `/state.audio_graph.fanin.host_clock` carries the ladder/probe block. |
| `enabled` | off | **Inert, warned.** One `event=fanin.host_clock.noop reason=usb_direct_off`; zero ctl writes. |
| `enabled` | `enabled`, no direct-lane resampler | **Inert, warned.** `reason=no_direct_resampler` (fail-soft). |

`jasper-fanin.service` carries the `ExecStopPost` pitch-neutralize belt for
SIGKILL/OOM/watchdog aborts, gated on **both** flags so it never fires on a
part-rolled-back box where fan-in is not the writer. The usbsink readiness
marker owns no ctl and carries no belt.

**Ladder:** `DISABLED → PROBING (await-lock → baseline → step → optional
retry_wait) → L0_LOCKED ↔ L1_WARN`, with terminal evidence falling to
`L2_FALLBACK`. Compliance is re-measured on every `(host_connected && playing)`
edge, because the host OS or application can change between sessions; there is
no periodic probe loop. A response under half the commanded step is ambiguous
once — attempt 1 neutralizes into a fixed 10 s `retry_wait`, and only attempt 2
can produce terminal `L2_FALLBACK`. Terminal causes are
`probe_noncompliant`, `lost_authority` (sustained saturated command plus adverse
slope mid-stream), and the infrastructure-class `actuator_unavailable`, which may
recover on the same stream; the first two latch until an idle boundary or a new
capture generation. `L1_WARN` is locked-but-watch, functionally identical to L0.

**The probe waits for lock before baselining.** A session begins the instant
audio flows, but the lane is then still filling (the resampler's held target
ramps 0 → target, the gadget ring primes), and baselining there measures the
warm-up ramp as if it were host clock drift — diagnosed on jts.local, where a 4 s
baseline read `baseline_slope_ppm=1460.6` and the run fell to `l2_fallback` for
the whole stream, with earlier "passes" being the same contamination landing
inside the pass band. So the probe opens holding neutral in **await-lock** and
does not baseline until the lane reports locked and that lock has held for a 2 s
settle. An un-lock mid-measurement discards it and restarts the wait without
spending an attempt — a warm-up re-entry is not a compliance failure.
`host_clock.probe.waiting_for_lock` is true only during a live session's wait;
the journal marks it with `event=fanin.host_clock_probe_wait`, the baseline start
with `…_probe_start`, and a session that ends still waiting with
`result=await_lock_ended` (distinct from `result=aborted`, which means a real
measurement was cut short).

**Soak falsifier.** `fill_variance`, `fill_slope_ppm`, and `correction_ppm` are
published every enabled tick so a soak can detect a cascade limit cycle — a
fighting cascade shows periodic correction ppm even though L0's target is
`correction → 0`. Watch all three before trusting a long-term L0 lock; the
remediation is to lower `CORRECTION_INTEGRAL_GAIN`, or leave the feature off.
Post-lock cushion decay gates on the servo's own signals (`ladder_l0`, plus a
`|commanded_ppm| > 400` freeze guard), and a settled L0 sits at a small steady
command with correction relaxed to ~0, well inside the guard.

```sh
curl -s http://jts.local:8780/state | jq .audio_graph.fanin.host_clock
```

### Cross-platform conditions

macOS honors asynchronous feedback well and is the shipping-gold target.
**Windows is unvalidated, and one constant depends on it:** `usbaudio2.sys`
honors feedback dynamically but with a ~163 ppm reaction deadband, and ignores
commanded values outside roughly nominal ±1 sample/interval — which is why the
servo's total-command clamp (`MAX_BIAS_PPM`, ±1000 ppm) sits inside that
validity window rather than at the wider hardware range, and why
`JASPER_FANIN_HOST_CLOCK_PROBE_PPM` is config-rejected below 200. Even the
default 300 leaves modest margin against a full-deadband subtraction
((300−163)/300 ≈ 0.46 against the 0.5 pass ratio), so a Windows lab box that
demotes spuriously should raise it toward 500–600. Both platforms react slowly,
which is why the outer loop's bandwidth stays low.

Every Windows-aware constant is research-sourced and tuned only against one
+600 ppm Mac, so a first Windows session is **discovery with captured
artifacts, not certification**. Work these in order, each gating the next:

1. **Enumeration** — does the composite NCM+UAC2 gadget enumerate as UAC2 on
   `usbaudio2.sys` at all, and does alt-setting negotiation hit the dwc2
   endpoint budget [HANDOFF-usb-gadget.md](HANDOFF-usb-gadget.md) flags as
   unverified on BCM2712? Capture a descriptor dump.
2. **Feedback endpoint** — confirm the descriptor Windows sees carries the
   16.16 feedback format at `bInterval=1` and the `wMaxPacketSize`
   `usbaudio2.sys` enforces.
3. **Compliance probe** — does `PROBE_PPM=300` clear the deadband, or is the
   500–600 mitigation needed?
4. **Clock envelope** — measure the box's crystal offset/jitter and confirm the
   ladder converges; log host-clock events for the whole session.
5. **Volume** — does Windows map its slider onto the UAC2 feature unit so
   `jasper/usbsink/volume_bridge.py`'s `PCM Capture Volume` polling sees it?

## Capture generation and self-heal

The direct PCM and the pitch ctl are one logical actuator generation but stay
thread-confined: the mixer thread owns capture and publishes `direct.opens`; the
host-clock thread reads that edge and neutralizes/drops/reopens/force-neutrals,
publishing a matching control generation. This holds even when the stale
handle's next write looks successful.

Two reopen counters name two different rebuild signatures, and both are
self-heals rather than failures:

- `reopens` — the flowing→dead **zombie latch**: the handle had been feeding the
  lane, then `avail_update` returned exactly 0 for ~2 s (a UDC rebind or gadget
  restart *while a stream was flowing*).
- `card_gen_reopens` — the ~1 s `snd_pcm_status` **liveness probe** finding the
  open handle dead (`-ENODEV` / `State::Disconnected`) while `avail_update`'s
  frozen mmap still returns `Ok(0)`. It fires regardless of whether a frame ever
  flowed, which is the gap the latch structurally cannot cover: a fan-in restart
  followed by a gadget rebuild before the next playback leaves a fresh handle
  deaf with no frames ever flowed. It cannot false-fire on an attached-idle
  host, whose capture stream stays in a live state. An `avail_update` errno (a
  clean unplug's `ENODEV`) is classified as device loss and parks *before* the
  probe runs.

**On-device obligation:** the premise — a STATUS ioctl trips across a real
rebuild while `avail_update` stays `Ok(0)` — is kernel behaviour no unit test can
pin. Confirm `card_gen_reopens` ticks across a gadget-function restart on
jts.local and that the box self-heals without a manual fan-in restart.

## Impulse tap (fan-in)

The certified route's ingress is fan-in's `hw:UAC2Gadget` capture, so the impulse
tap runs inline in the direct read, before the resampler, and writes
`/run/jasper-fanin/impulse-tap.jsonl`
(`{"monotonic_ns","frame_index","ring_fill_frames","peak"}`). Arm and disarm are
control-socket verbs, not HTTP; STATUS gains a top-level `tap` block
(`armed`, `events_written`, `events_dropped`, `threshold`, `refractory_ms`,
`max_events`, `auto_disarm_at_epoch_ms`, `path`). The harness arms it natively —
the raw verbs are for a manual run:

```sh
printf 'TAP_ARM {"threshold":0.2,"refractory_ms":250}\n' \
  | socat - UNIX-CONNECT:/run/jasper-fanin/control.sock
printf 'TAP_DISARM\n' | socat - UNIX-CONNECT:/run/jasper-fanin/control.sock
```

Selection lives in `jasper/route_latency/tap_transport.py`; the fan-in UDS client
is `FaninTapClient` in `jasper/route_latency/tap_client.py`.

## Observability

Fan-in STATUS (`STATUS` on `/run/jasper-fanin/control.sock`, surfaced on
`/state.audio_graph.fanin`): every input carries `"source":"lane"|"direct"`, and
the direct lane adds
`"direct":{"device","present","health","opens","retries","reopen_pending","reopens","card_gen_reopens"}`.
`health` is the coarse classification driving fan-in's own recovery —
`"capturing"`, `"idle"` (no host, attached-but-silent, or reopening — never a
failure), or `"broken"` (the zombie signature). **These fields are observability
only: they never withdraw UAC2 or disarm direct capture.** Frames and xruns ride
the existing `frames_read`/`xrun_count`; rate lock rides the existing
`resampler{}` block; the host-clock ladder rides `host_clock{}` with `obs_mode`
and `correction_ppm`.
