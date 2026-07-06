# Handoff: USB-in low latency — production `usb_low_latency_48k`

Current operational truth for the first production low-latency USB route.
The shipped route is **not** the old lean-FIFO bypass plan: it keeps USB in
the shared fan-in/Camilla/outputd protection path and earns any low-latency
claim only through measured route-latency evidence.

## Current Production Route (2026-07-02)

`usb_low_latency_48k` is the claiming profile:

```
UAC2 gadget capture
  → jasper-usbsink-audio (Rust, 256 frames / 3 periods, S32_LE→S16_LE high-word truncation)
  → usbsink_substream
  → jasper-fanin USB input resampler (target 512 + cushion 1536, ring 4096)
  → fan-in output ALSA loopback
  → CamillaDSP ALSA capture
  → CamillaDSP protection/correction
  → outputd content ALSA loopback
  → outputd final DAC owner + final-speaker reference
```

Apple USB-C DAC tuned floor after the 2026-07-02 jts.local pass:

| Layer | Shipped floor | Rejected lower setting |
|---|---:|---|
| Rust USB bridge | 256 frames / 3 periods | 128 frames, 256/2 |
| fan-in input buffer | 4096 frames | 512/1024/2048/3072 failed lock/acquisition |
| fan-in USB resampler | target 512 + cushion 1536 (held target 2048) | held target 1920 and below relocked/silenced |
| CamillaDSP | chunksize 256 / target 1536 | target 1024 caused bridge playback xruns |
| outputd | period 128 / DAC buffer 256 | 64/128 caused bridge playback xruns |
| outputd content capture | buffer 1536 | 640/768/1024/1280 caused content xruns |

Best values to keep for the current Apple USB-C DAC fallback:

```text
JASPER_USBSINK_BLOCK_FRAMES=256
JASPER_USBSINK_RING_PERIODS=3
JASPER_FANIN_INPUT_BUFFER_FRAMES=4096
JASPER_FANIN_USB_RESAMPLER_TARGET_FRAMES=512
JASPER_FANIN_USB_RESAMPLER_WARMUP_CUSHION_FRAMES=1536
JASPER_FANIN_USB_RESAMPLER_RING_FRAMES=4096
JASPER_FANIN_USB_RESAMPLER_MAX_ADJUST_PPM=500
JASPER_FANIN_OUTPUT_BUFFER_FRAMES=1024
JASPER_CAMILLA_CHUNKSIZE=256
JASPER_CAMILLA_TARGET_LEVEL=1536
JASPER_OUTPUTD_PERIOD_FRAMES=128
JASPER_OUTPUTD_DAC_BUFFER_FRAMES=256
JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES=1536
JASPER_OUTPUTD_CONTENT_BRIDGE=direct
JASPER_FANIN_CAMILLA_COUPLING=loopback
```

Clean hardware evidence so far: a 5-minute jts.local steady-state sample with
outputd content buffer 1536 had zero new outputd content xruns/empty reads,
zero outputd DAC xruns, zero fan-in output xruns, zero fan-in USB resampler
relocks/unlocks/silence/overruns, and zero CamillaDSP warnings. A 2048-frame
content-buffer sample was also clean. Lower content-buffer probes at 640, 768,
1024, and 1280 each produced a content-side xrun. The 1280 test window also had
a host-output handoff nearby, but the repeat still showed the same content-side
failure mode with USB playback active, so 1280 is not accepted.

This proves stability of the tuned loopback floor, **not** the 40 ms end-to-end
p95 target. The configured buffers alone exceed that target: the fan-in USB
resampler held target is 2048 frames (~42.7 ms at 48 kHz), before the observed
fan-in output delay (~16-19 ms), outputd DAC delay (~10-11 ms), CamillaDSP
targeting, and bridge/ALSA boundary costs. Doctor must keep failing
`route latency evidence` until a click/capture artifact certifies p95 <= 40 ms
with >=200 impulses over >=5 minutes; p99 promotion requires >=1000 impulses
over >=30 minutes with jittered spacing and p99 <= 60 ms.

The claiming route now hard-fails if it is combined with legacy low-latency lab
transports: `JASPER_FANIN_CAMILLA_COUPLING=transport_pipe` or
`JASPER_OUTPUTD_CONTENT_BRIDGE=rate_match`. Those paths remain available only as
default-off diagnostics until they are removed or replaced; they cannot carry
`usb_low_latency_48k` certification.

The artifact writer is `sudo /opt/jasper/.venv/bin/jasper-route-latency-artifact`.
It does **not** measure audio by itself; the click-in/capture-back harness that
produces real per-impulse latencies (JSON/CSV/text, milliseconds) or aggregate
p95/p99 metrics is `jasper-route-latency-harness` (source:
`jasper/cli/route_latency_harness.py` + `jasper/route_latency/`) — see
[`docs/testing-tooling.md` "Route-latency click/capture harness"](testing-tooling.md#route-latency-clickcapture-harness)
for the architecture and the end-to-end quick/promotion walkthrough below.
Run the artifact writer with `sudo` on the Pi because it must read root-owned
runtime env files and write `/var/lib/jasper/audio-validation/*.json`. The writer
binds the measured numbers to the live `jasper.audio_runtime_plan` route identity
and updates `latest.json`. Raw sample inputs are recorded with source path,
byte count, and SHA-256 of the parsed file. Aggregate-only inputs require
`--harness-id` so the artifact cannot anonymously certify externally computed
percentiles.

**End-to-end quick gate** (generates the samples file the artifact writer
needs, using `jasper-route-latency-harness`; see
[`docs/testing-tooling.md` "Route-latency click/capture harness"](testing-tooling.md#route-latency-clickcapture-harness)
for the full architecture):

Invoke both CLIs by their absolute venv path (`/opt/jasper/.venv/bin/...`) —
under `sudo` the venv `bin/` is not on `secure_path`, so a bare command name
won't resolve. (The harness's own `--invoke-artifact` passthrough resolves the
sibling artifact writer automatically once the harness itself is launched this
way.)

```sh
# 1. Generate the click-track WAV + schedule.
/opt/jasper/.venv/bin/jasper-route-latency-harness generate quick --out-dir /tmp/route-latency

# 2. On the Pi: arm the tap, capture the mic for the schedule's duration
#    while the WAV plays on the host at a modest, comfortable volume
#    (start very quiet — CamillaDSP's volume_limit stays the 0 dB
#    ceiling), then analyze and shell out to the artifact writer. `run`
#    loads the schedule directly, so it needs no --duration-seconds /
#    --impulse-spacing-jittered flags (those exist only on `analyze`,
#    which has no schedule file to read them from). --confirm-route-health-ok
#    is the harness's OWN flag — read the printed health-delta report first;
#    it is never inferred automatically:
sudo /opt/jasper/.venv/bin/jasper-route-latency-harness run \
  /tmp/route-latency/quick-schedule.json \
  --out-dir /tmp/route-latency \
  --invoke-artifact \
  --confirm-route-health-ok
```

Or drive `jasper-route-latency-artifact` directly once a samples file already
exists (equivalent to what `--invoke-artifact` above shells out to, once the
health deltas justify `--route-health-ok` on THAT CLI):

```sh
sudo /opt/jasper/.venv/bin/jasper-route-latency-artifact \
  --samples /tmp/route-latency/latency-samples.json \
  --duration-seconds 360 \
  --harness-id jts-click-capture-v1 \
  --route-health-ok
```

**End-to-end promotion gate** (`generate promotion` instead of `quick`; `run`
reads jitteredness straight off the loaded schedule, so no
`--impulse-spacing-jittered` flag is needed here — see `analyze`'s own
example below for where that flag lives):

```sh
/opt/jasper/.venv/bin/jasper-route-latency-harness generate promotion --out-dir /tmp/route-latency
sudo /opt/jasper/.venv/bin/jasper-route-latency-harness run \
  /tmp/route-latency/promotion-schedule.json \
  --out-dir /tmp/route-latency \
  --measurement-id RUN_ID \
  --invoke-artifact \
  --confirm-route-health-ok \
  --require-pass
```

or the artifact writer alone, once a samples file exists:

```sh
sudo /opt/jasper/.venv/bin/jasper-route-latency-artifact \
  --samples /tmp/route-latency/latency-samples.json \
  --duration-seconds 1800 \
  --impulse-spacing-jittered \
  --harness-id jts-click-capture-v1 \
  --measurement-id RUN_ID \
  --route-health-ok \
  --require-pass
```

Only pass `--route-health-ok` when the same measurement window had clean
bridge/fan-in/outputd deltas: no bridge capture/playback xruns, no bridge
underflow/overflow/drops, no fan-in USB resampler unlock/silence/overrun, and no
outputd/fan-in xruns. Without that declaration, the artifact records
`route_health_anomaly` and doctor rejects the low-latency claim. With the
declaration, the artifact writer and doctor still compare live Rust bridge
period/ring state and fan-in USB resampler lock/target state against the route
identity; any mismatch records/fails the claim as live route-health drift.
`jasper-route-latency-harness analyze` prints exactly this delta (every
nonzero usbsink/fan-in/outputd counter change across the measurement
window) and states whether the declaration *would* be justified — it never
asserts `--route-health-ok` on the operator's behalf; read the printed
deltas and decide.

## USB DIRECT (combo mode) — delete the bridge hop + aloop cable (P3: DEFAULT-ON on gadget boxes)

> **Default status (P3 default-flip, landed).** The USB combo
> (`JASPER_FANIN_USB_DIRECT` + `JASPER_FANIN_HOST_CLOCK` +
> `JASPER_FANIN_RESAMPLER_CUSHION_DECAY`, all `enabled`) is now the SHIPPED
> DEFAULT on any box whose USB gadget stack is enabled
> (`dtoverlay=dwc2,dr_mode=peripheral` present). The boot/deploy reconciler pass
> `jasper-fanin-coupling-reconcile --auto` writes those three keys into
> `/var/lib/jasper/fanin.env` (single writer), and clears them off a non-gadget
> box. The `USBSINK_STANDBY` half is armed by the same reconcile path via the
> gadget standby signal. The prose below still describes HOW the combo works and
> its safety matrix; where it says "DEFAULT-OFF / hand-armed" read that as the
> pre-P3 posture. **To revert:** set `JASPER_FANIN_COUPLING_CHOICE=operator` and
> unset the three combo keys (see `.env.example`) — the auto pass then no-ops and
> the revert sticks. The floor default is now the validated **576**
> (`DEFAULT_CUSHION_DECAY_FLOOR_FRAMES`) so a combo-armed default constructs.

`JASPER_FANIN_USB_DIRECT=enabled` + `JASPER_USBSINK_AUDIO_STANDBY=1` removes the
usbsink **bridge hop + the snd-aloop cable** (~25 ms measured) from the USB path:
fan-in captures `hw:UAC2Gadget` **directly** and narrows S32→S16 itself, feeding
the SAME per-input `LaneResampler` the aloop path used. The bridge drops to
state/HTTP-only standby (opens NO PCM, leaving the gadget free), so the DSP /
crossover / correction / protection chain downstream of fan-in is unchanged. The
one deliberate exception is source arbitration + renderer-state truth — see the
arbitration caveat below the flag matrix.

```
UAC2 gadget capture
  → jasper-fanin DIRECT capture (hw:UAC2Gadget, S32_LE→S16 high-word truncation, period 256/buffer 768)
  → jasper-fanin USB input resampler (same target/cushion/ring)  ← bridge hop + aloop cable GONE
  → fan-in output → CamillaDSP → outputd  (unchanged)
```

Both halves are DEFAULT-OFF and fail-safe (only the exact literals arm them:
`JASPER_FANIN_USB_DIRECT=enabled`, `JASPER_USBSINK_AUDIO_STANDBY=1`).

### Flag matrix (C6)

| `FANIN_USB_DIRECT` | `USBSINK_STANDBY` | Result |
|---|---|---|
| off (default) | off (default) | **Today's lane** — byte-identical. Bridge bridges gadget→aloop; fan-in reads aloop. |
| `enabled` | `1` | **PoC target.** Fan-in captures the gadget directly; bridge is state/HTTP-only. The bridge hop + aloop cable (~25 ms) are gone. |
| `enabled` | off | Misconfig, **safe**: the bridge holds `hw:UAC2Gadget`, so fan-in's direct open fails → the lane goes silent-idle with a 2 s reopen retry (`/state` fan-in `usbsink.direct.present=false`, `retries` grows, one transition log). USB source is SILENT (the direct lane never opens its aloop PCM). Recover by fixing the flags — no crash. |
| off | `1` | Misconfig, **safe**: the bridge doesn't bridge; fan-in reads an unfed aloop substream → silence via EAGAIN. Observable: bridge `standby:true` while fan-in lane `source:"lane"`. |

**Arbitration caveat (combo opts out of source arbitration + renderer-state
truth).** In standby the bridge always writes `playing:false` (no audio loop;
pinned by `test_usbsink_state.py`), so from the rest of the system USB appears
idle even while fan-in is audibly mixing its direct lane:

- **Mux never sees USB playing**, so latest-source-wins auto preemption doesn't
  fire on a USB start, and mux's preempt POST to the bridge silences nothing on
  the audio path (fan-in reports `Obs.preempted = false` by design). Another
  source can layer on top instead of preempting.
- **The landing-page Source UI shows USB idle** while it's mixing, because the
  renderer state it reads is the bridge's `playing:false`.

**This gap is now LIVE, not lab-only (P3 default-flip).** As of P3 the combo is
the SHIPPED default on any gadget box, so the arbitration/UI gap above applies to
every such household — not just a hand-armed lab box. It was validated as
acceptable on jts.local, where the wired Mac is effectively the SOLE source (USB
rarely contends with AirPlay/Spotify/BT simultaneously), which is the common
gadget-box shape. But on a gadget box that DOES mix sources, USB will not preempt
and the Source UI will show it idle while it plays. **Wiring standby to publish an
honest playing/arbitration signal remains the top P3 follow-up** — it was
originally gated as "the follow-up before combo could ship on by default," and the
default-flip shipped ahead of it on the strength of the solo-box validation. Track
it before promoting combo to multi-source households. To opt a contended box out,
revert per the default-status callout at the top of this section.

### Host-slaved USB clock in combo mode (fan-in owns the ctl)

The Stage 1 host-slaved USB clock (steer the gadget's `Capture Pitch 1000000`
ctl so the host tracks the DAC clock, closing the standing rate offset at its
source) has **one home per mode**, decided by the invariant *the daemon that
owns the gadget capture owns the pitch ctl*:

- **solo (aloop) mode** — the usbsink bridge owns `hw:UAC2Gadget`, so it drives
  the ladder: `JASPER_USBSINK_HOST_CLOCK=enabled` (see "Host-slaved USB clock
  (Stage 1)" below).
- **combo (USB DIRECT) mode** — fan-in owns the capture, so a dedicated
  `fanin-host-clock` thread drives it: `JASPER_FANIN_HOST_CLOCK=enabled`.

Both run the **same** shared ladder/probe/servo (`rust/jasper-host-clock`,
one servo core; the per-daemon differences are the `event=` log prefix —
`usbsink_audio` vs `fanin` — which `JASPER_*` keys each parses, and the
**observable mode** below). Combo mode pins the DIRECT lane's resampler fill at
target, removing the standby-mode drift wander (the ~9 ms "standby gap" measured
below). The setpoint is the resampler's HELD target
(`JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES +
JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES`) — one setpoint shared with
the inner rate controller, so the outer loop never fights the inner integrator
(the ≥10× bandwidth separation of the cascade is derived in the
`jasper-host-clock` module docstring).

#### Observable mode — fill slope (solo) vs resampler correction (combo)

The one servo core runs on **two different observables**, chosen by a TYPED
`ObsMode` on the shared `HostClockConfig` (never inferred): usbsink solo passes
`ObsMode::Fill`, fan-in combo passes `ObsMode::Correction`. This is the fix for
the hardware-diagnosed combo-mode defect (jts.local 2026-07-03).

- **`Fill` (usbsink solo, aloop).** No rate-matching stage sits between the
  gadget ring and playback, so the gadget FILL slope is a faithful readout of the
  host-vs-DAC rate error. The probe reads the fill-slope response to the pitch
  step; the L0 servo drives `fill − target → 0`. **Unchanged** from the original
  servo.
- **`Correction` (fan-in combo, USB DIRECT).** The lane resampler (±500 ppm
  authority) sits between the gadget ring and the mix and ABSORBS host-clock
  drift to hold its fill at the held target — so the fill observable is
  structurally dead: the resampler flattens the slope the probe wants to measure
  and pins the fill by its OWN action, not by the pitch commands. On jts.local
  the fill-based post-lock probe reliably failed (`response_ratio=-0.88`) and the
  ladder parked in `l2_fallback`; even a prior `l0_locked` had a dead fill-error
  signal (fill sat ~500 pinned BY THE RESAMPLER). In combo mode the honest
  observable is the resampler's OWN live correction ppm (its `ratio_milli_ppm`
  gauge, the same atomic STATUS reads — single source of truth, owned by the
  resampler on the mixer thread, threaded into `Obs.correction_ppm`). The probe
  reads how far the resampler's mean correction MOVES between the neutral baseline
  window and the step window; the L0 servo drives `correction_ppm → 0`. When the
  correction is ~0 sustained the host is truly slaved to the DAC, the resampler is
  idle, and the fill rides the resampler's held target for free.

  **The `Correction` outer control law is a PURE INTEGRAL, not the `Fill`-mode
  DLL** (`CORRECTION_INTEGRAL_GAIN` in `jasper-host-clock`). This is the structural
  correction the observable choice demanded (staff review of PR #1144): the two
  modes present the outer loop DIFFERENT plants. `Fill`-mode's plant is an
  INTEGRATOR (a pitch command sets the gadget-fill *slope*), so the DLL's own
  integrators plus that plant integrator is the well-behaved cascade. `Correction`-
  mode's plant is near-UNITY DC gain through the inner resampler's lag (a ppm
  command becomes, after the inner `RateController` settles, ~the same ppm of
  correction — ppm→ppm, no integrator). Driving that unity-gain-plus-lag plant with
  the `Fill`-tuned third-order DLL puts loop gain > 1 past 180° of phase, so it
  limit-cycles — verified against the REAL inner controller (a compliant Mac at
  +20 ppm crystal railed correction ±460 ppm on a ~21 s period). A single slow
  integrator around a near-unity plant is unconditionally stable at a small gain;
  the feed-forward seed (`−baseline_correction`) carries the DC crystal cancel and
  the integrator only trims the residual. Anti-windup is conditional integration
  (skip the step when the total command is railed and this step would push it
  further into the rail). The servo-sim tests close the ladder against
  `jasper_resampler::RateController` (built exactly as `lane_resampler` does), so
  they measure the composite loop's ACTUAL dynamics and pin that it converges
  without oscillating across the crystal / lag / noise / 3600 s matrix.

  **The `Correction` probe uses a longer step window and an adaptive step
  direction** (`CORRECTION_PROBE_STEP_SECS`, `CORRECTION_PROBE_FLIP_DEADBAND_PPM`)
  because the inner-loop observable is slower than the fill slope and bounded by
  the resampler's ±500 ppm authority. A 6 s step reads a compliant host only
  partway through the inner loop's slew (a −250 ppm crystal measured
  `response_ratio ≈ +0.16` — a false FAIL), so `Correction` holds the step for
  15 s. And a fixed `+probe_ppm` step against a host already near a rail (e.g.
  +450 ppm crystal) pushes a compliant host past the +500 clamp so the observable
  can't show the full response (`+0.19` — another false FAIL); so `Correction`
  steps AWAY from the nearer rail (down when the baseline correction is strongly
  positive, up when strongly negative), and normalizes the verdict by the SIGNED
  step. A non-compliant host's natural crystal drift runs OPPOSITE the away-from-
  rail step, so it reads a clearly negative ratio and still fails. `Fill` mode is
  unchanged (always `+probe_ppm`, `probe_step_secs`, hardware-validated at 6 s).

Both observables share the same sign property, so the feed-forward seed is
byte-identical across modes: a compliant host commanded a `+step` moves the
observable `+step` (fill climbs in `Fill`; the resampler must consume faster to
hold fill, so its correction ppm rises in `Correction`), giving
`response_ratio ≈ +1`; a host that ignores the step moves it ~0, giving `≈ 0`
(same `>= 0.5` pass band, same demotion). The neutral baseline value is the host's
natural excess rate, cancelled by the `-baseline_obs` feed-forward. STATUS
surfaces the mode as `host_clock.obs_mode` (`"fill"`/`"correction"`) and the live
correction as `host_clock.correction_ppm` (additive; 0 in `Fill` mode), so the
combo L0 end-state (`correction_ppm → ~0` at `l0_locked`) is directly observable.
The `dll` block in the STATUS fragment reads idle in `Correction` mode — the DLL
is not the controller there, so its `err_frames`/`locked` are diagnostic zeros,
not a live signal.

#### `HOST_CLOCK × USB_DIRECT` flag matrix (fan-in)

| `FANIN_HOST_CLOCK` | `FANIN_USB_DIRECT` | Result |
|---|---|---|
| off (default) | any | **Inert.** No `fanin-host-clock` thread; `/state` fan-in `host_clock.enabled=false`. In solo mode usbsink owns the clock (its own flag). |
| `enabled` | `enabled` | **Combo target.** fan-in owns the gadget capture and steers `Capture Pitch`; per-session probe → L0 pins the DIRECT lane fill at target. `/state.audio_graph.fanin.host_clock` carries the ladder/DLL/probe block. |
| `enabled` | off | **Inert, warned.** One `event=fanin.host_clock.noop reason=usb_direct_off`; zero ctl writes ever — in aloop mode the usbsink bridge owns the clock. No thread spawned. |
| `enabled` | `enabled`, but no direct-lane resampler | **Inert, warned.** One `event=fanin.host_clock.noop reason=no_direct_resampler` (resampler construction fell back to none — fail-soft). No thread. |

**Double-enable misconfig (R5):** `JASPER_FANIN_HOST_CLOCK=enabled` +
`JASPER_FANIN_USB_DIRECT=enabled` while the usbsink bridge is NOT in standby
(both own-the-clock daemons armed at once). fan-in's direct open fails (the
bridge holds `hw:UAC2Gadget`), so no session ever starts and the ladder holds
neutral — but fan-in's **one** startup neutralize can stomp an active usbsink L0
command once. usbsink self-recovers via its own probe / L2 machinery, and audio
is unaffected either way. Fix by putting the bridge in standby
(`JASPER_USBSINK_AUDIO_STANDBY=1`) — the intended combo posture.

**Neutrality belt-and-braces — BOTH belts are owner-gated (the epsilon-desync
class is symmetric).** Each USB-clock owner carries a `ExecStopPost` that resets
the pitch to `1000000` on SIGKILL / OOM / watchdog abort, and **each gates on
being the current owner** so it never stomps the *other* daemon's live command
(which would desync that daemon's >10 ppm write-suppression epsilon — it believes
its last written value is still live and won't rewrite until real drift crosses
the gate, leaving the host un-slaved for minutes):

| Unit | Belt gate | Fires when… | Would-desync-if-unconditional |
|---|---|---|---|
| `jasper-fanin.service` | `$JASPER_FANIN_HOST_CLOCK = enabled` **AND** `$JASPER_FANIN_USB_DIRECT = enabled` | fan-in owns the ctl (combo mode) | a **solo-mode** usbsink L0 command (fan-in restarts every deploy) |
| `jasper-usbsink.service` | `$JASPER_USBSINK_AUDIO_STANDBY != 1` | usbsink owns the ctl (solo/aloop mode) | a **combo-mode** fan-in L0 command while usbsink stands by (deploy try-restarts usbsink on binary change; operators restart it) |

Both gates are load-bearing and mirror each other: the owner is exactly the
daemon holding `hw:UAC2Gadget`, and only the owner ever writes the ctl. Both
target the same element by (iface, name), never numid.

- **fan-in's belt requires BOTH flags** (F2): fan-in owns the ctl only when
  `HOST_CLOCK` **and** `USB_DIRECT` are enabled (it resolves
  `host_clock_enabled && !usb_direct_off` and issues zero ctl writes with only
  `HOST_CLOCK` set — `noop reason=usb_direct_off`). Gating on `HOST_CLOCK` alone
  would fire the belt on a part-rolled-back combo box (unset `USB_DIRECT`, left
  `HOST_CLOCK=enabled`) while solo usbsink's DLL is the live writer — the same
  every-deploy desync the gate exists to prevent.
- **usbsink stays fully hands-off in standby** (F1): in standby usbsink opens
  no ctl and skips even its one-shot startup/exit neutralize (`owns_host_clock_ctl()`
  = `!audio_standby`). A clean stop/start cycle of the standby daemon — a deploy
  try-restart on binary change, or an operator restart — therefore never resets
  fan-in's live combo command. The `SIGKILL != 1` ExecStopPost belt is the
  belt-and-braces for the un-clean paths only. Before F1, standby's neutralizes
  still stomped fan-in's command on every clean cycle; before F2 only fan-in's
  belt was gated (usbsink's unconditional belt was the reverse leak on SIGKILL).

Combo host-clock telemetry:

```sh
curl -s http://jts.local:8780/state | jq .audio_graph.fanin.host_clock
```

### Observability

- Fan-in STATUS (`/run/jasper-fanin/control.sock` `STATUS`, surfaced on `/state`):
  every input gains `"source":"lane"|"direct"`; the direct lane also gains
  `"direct":{"device","present","opens","retries","reopens"}`. The lane's
  frames/xruns ride the existing `frames_read`/`xrun_count`; its rate-lock rides
  the existing `resampler{}` block. `reopens` is the ZOMBIE-handle forced-reopen
  counter (C): a growing value means the gadget function is being rebuilt
  underneath fan-in (UDC rebind / usbsink stop-start) and the lane is self-healing
  the deaf `Ok(0)`-forever capture handle instead of needing a manual fan-in
  restart.
- Bridge STATUS/state.json gains additive `"standby":true|false` (schema_version
  stays 1); in standby `playing:false`, `rms_dbfs:-120`, ring/counters zero, and
  `host_connected` is best-effort from sysfs (`/sys/class/udc/*/state ==
  "configured"`). A misdirected harness run is diagnosable from `standby:true`.
- Transition logs: `event=fanin.usb_direct.present` / `.absent` (one line per
  presence change, device + errno + cumulative retries), `event=fanin.usb_direct.armed`
  at config load, `event=usbsink_audio.standby active=true` at bridge start.
  `event=fanin.usb_direct.reopen reason=zombie_handle` fires when a Present handle
  that had been feeding the lane goes deaf — `avail_update()` returns exactly 0 for
  ~2 s **after** frames had flowed on that handle (the gadget was rebuilt underneath
  it, no errno) — and the lane force-closes + re-opens the capture; the host-clock
  servo's ctl handle is dropped on the same edge (`event=fanin.host_clock_ctl_error
  ... action=drop_stale_handle`) so the next session edge re-opens it against the
  rebuilt card. The **flowing→dead** gate (`frames_flowed_since_open`, added
  2026-07-05) is load-bearing: an ordinary attached-but-silent host streams
  `avail≈0` drains indefinitely with no gadget rebuild (see "attached-idle drains
  record `avail≈0`" below), and firing on raw zero-avail alone would churn a reopen
  every ~2 s of idle on a Mac-wired box — journal spam plus a `reopens` counter that
  no longer means "gadget rebuilt underneath." Because a fresh reopen clears the
  latch, a reopen that lands back on a still-zombie gadget does not immediately
  re-fire; `reopens` counts one increment per real rebuild.

### Impulse tap moves to fan-in (C4)

In direct mode the certified route's ingress is fan-in's `hw:UAC2Gadget`
capture, so the impulse tap is **relocated into fan-in** (ported verbatim from
`jasper-usbsink-audio`: same JSONL schema, same detector, same arm validation).
It runs inline in the direct read over the converted S16 slice, before the
resampler. **The bridge's own tap is DEAD in direct mode** (the bridge is in
standby and opens no capture), so the fan-in JSONL is the ONLY ingress evidence.

- Path: `/run/jasper-fanin/impulse-tap.jsonl` (the JSONL schema is unchanged:
  `{"monotonic_ns","frame_index","ring_fill_frames","peak"}`).
- Arm/disarm are **control-socket verbs** (not HTTP): `TAP_ARM {json}` /
  `TAP_DISARM` on `/run/jasper-fanin/control.sock`. STATUS gains a top-level
  `"tap":{armed,events_written,events_dropped,threshold,refractory_ms,max_events,auto_disarm_at_epoch_ms,path}`.

**Director commands (PoC).** The route-latency harness's `analyze --tap-events`
already reads any JSONL path, so no HTTP port is needed — arm via the socket
verb, point `--tap-events` at the fan-in JSONL:

```sh
# 1. Arm (disarm with TAP_DISARM):
printf 'TAP_ARM {"threshold":0.2,"refractory_ms":250}\n' \
  | socat - UNIX-CONNECT:/run/jasper-fanin/control.sock
printf 'TAP_DISARM\n' | socat - UNIX-CONNECT:/run/jasper-fanin/control.sock

# 2. Analyze against the fan-in JSONL (mic-wav / other args as today):
python -m jasper.cli.route_latency_harness analyze \
  --tap-events /run/jasper-fanin/impulse-tap.jsonl \
  --mic-detections <capture>.jsonl <other args as today>
```

### Status (PoC bar)

Correct + observable + flag-gated default-off; **hardware-validated on
jts.local 2026-07-02** (Apple dongle, electrical `:9891` reference mode).
Conversion parity with the bridge is by construction (both consume
`jasper_resampler::s32_high_word_to_s16`, pinned by an identical sign-boundary
vector in all three crates). The direct open uses the bridge's proven envelope
(S32LE/2ch/48k, period 256, buffer-near 768). Gadget absence/unplug is
silent-idle with a bounded ~2 s reopen retry (period-counted, never a daemon
error). Hardening (deploy wiring, doctor surface, wizard toggle) comes next.

### Final state — 2026-07-03 overnight productization (where we landed and why)

**Everything below is merged to main** as the reviewed PR train #1137 (jasper-host-clock
crate) → #1138 (fan-in USB-DIRECT platform + DLL + jasper-ring + usbsink standby) →
#1139 (ring consumers: ioplug + outputd reader + lab tooling, EXPERIMENTAL) → #1140
(drain-dwell instrumentation + tunable gadget period) → #1141 (cushion decay,
default-off) → #1142 (probe lock-gate). Every PR passed a separate adversarial staff
review with zero unresolved Blockers/Should-fixes before merge. All features are
**default-off**; a box that opts into nothing is byte-identical to the pre-train build
(hardware-verified on jts3: default-off main, doctor clean, AirPlay pass).

**Final measured floor** (jts.local, Apple USB-C dongle, electrical `:9891` reference,
final settings = USB DIRECT + rings 2-slot + camilla chunk 128/queuelimit 1/target 128 +
resampler target 256 / cushion **≥ 306** (held **≥ 562**), host-clock and decay OFF —
see "why" below). A diagnostic
unlock-counter burst (~295 in one 2-min window) occurred mid-20-min-run with zero
measured effect (all impulses in the window matched; percentiles tight). **Now
diagnosed and guarded (2026-07):** the burst is an HONEST count of real
lock→silence→relock cycles caused by the *lab* resampler geometry — a held target of
256+256 = 512 sits only `512 − 256(period) − 274(minimum_safe_fill) = −18` frames
of headroom below the underfill-unlock threshold after each render, so ordinary USB
delivery coalescing (the `max_avail≈516`, 2-period drain-entry signature) unlocks
the lane every burst; relock is ~1 period, so it is diagnostic-visible but
measurably harmless. The counter is not double-counting (one increment per genuine
unlock event). The production defaults (held 2560) have ~2030 frames of margin and
are immune. A fail-loud config guard now rejects a churny `target+cushion` geometry
when the resampler is armed (`STATIC_CUSHION_JITTER_MARGIN_FRAMES` in
`rust/jasper-fanin/src/config.rs` — the static-cushion sibling of the decay-floor
guard), so this knob-set cannot ship silently again.

> **⚠️ Deploy trap — read before deploying #1145 to a box running the lab recipe.**
> The guard is fail-LOUD: `Config::from_env` `bail!`s at startup for period 256 /
> ±500 ppm whenever the armed held target is below **562** (`minimum_safe_fill 274 +
> period 256 + jitter margin 32`). The old lab recipe (`TARGET_FRAMES=256` +
> `WARMUP_CUSHION_FRAMES=256` → held 512) is **below** that floor, and
> `jasper-fanin.service` carries `Restart=on-failure` + `StartLimitAction=reboot`, so
> a box whose live env still has the 256+256 geometry will **reboot-loop** after this
> lands (the remedy is only visible in the journal between reboots). **The minimum
> passing geometry at period 256 / ±500 ppm is held ≥ 562** — i.e. keep
> `TARGET_FRAMES=256` and raise `JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES`
> to **≥ 306** (or raise `TARGET_FRAMES` so target+cushion ≥ 562), OR lower
> `MAX_ADJUST_PPM` / `PERIOD_FRAMES`. **jts.local specifically** runs USB DIRECT +
> resampler 256+256 as its documented lab route, so bump its
> `JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES` to ≥ 306 in
> `/etc/jasper/jasper.env` **before** deploying this PR. The blast radius is identical
> to the existing decay-floor guard (same fail-loud-at-boot class).

| run | measured p50/p95/p99 | end-to-end p50/p95/p99 |
|---|---|---|
| 5-min (160/160, unlocks 4, 0 overruns) | 34.99 / 36.13 / 37.21 | 46.1 / 47.2 / 48.3 |
| **20-min FINAL (640/640, 100 %, zero USB-lane xruns)** | **34.71 / 36.18 / 36.97** | **45.8 / 47.3 / 48.1** |

End-to-end = measured span + 1.2 ms gadget dwell (delta-windowed drain-entry evidence,
#1140 instrumentation; replaces the earlier conservative 3.9) + 9.9 ms DAC-side delay
(probe: 477 fr = 256-fr ring + ~220-fr snd-usb-audio URB queue on the dongle).

**Lever outcomes (systematic, each hardware-gated):**
- *Gadget drain dwell*: H2 confirmed — true frame-dwell ≈ 1.2 ms (accounting win, −2.7 on
  the honest number). Period-64 variant REFUTED (entry backlog doubled + xrun storm —
  URB-cadence class). Instrumentation is permanent (`drain_avail` in STATUS).
- *DAC-side trim*: REFUTED both halves. outputd period is graph-quantum-coupled (period
  64 → Ring B slot mismatch → camilla EINVAL, fail-closed park verified); the dongle is a
  FULL-SPEED device (192-byte/1 ms packets) with `lowlatency=Y` already active — the
  ~4.6 ms URB queue is FS transport physics. I2S/HAT outputs never pay this term
  (equivalent graph there ≈ −4.6 ms).
- *Cushion decay (the ~5 ms lever)*: built, reviewed, merged default-off (#1141). Its
  engagement was blocked by a diagnosed ladder design gap: with the lane resampler
  locked, its inner controller absorbs the probe's pitch step, so the fill-based
  post-lock probe (#1142) reliably failed (ratio −0.88) and the ladder never reached l0.
  **Fixed by the combo-mode observable redesign** (#1144): combo mode now runs
  `ObsMode::Correction` — the probe and L0 servo observe the resampler's OWN correction
  ppm (its `ratio_milli_ppm` gauge threaded into `Obs.correction_ppm`), not the dead
  fill slope. The `Correction` outer control law is a PURE INTEGRAL
  (`CORRECTION_INTEGRAL_GAIN`), not the `Fill`-mode DLL — the observable choice changed
  the plant from an integrator to a near-unity-gain one, and the `Fill`-tuned DLL
  limit-cycles against it (staff review of PR #1144 caught this by composing the real
  crates; fixed in the same branch). The probe also uses a longer step window and steps
  away from the nearer inner-authority rail so a compliant host at any crystal offset
  (including near ±500 ppm) passes. Servo-sim tests close the ladder against the REAL
  `jasper_resampler::RateController` (built as `lane_resampler` does), pinning that the
  probe passes compliant / fails non-compliant AND that L0 converges without a limit
  cycle across the crystal/lag/noise/3600 s matrix. On-hardware validation
  (STATUS `host_clock.obs_mode=correction`, `correction_ppm → ~0` at `l0_locked`, no
  periodic `correction_ppm` in a soak) is still owed before decay is re-armed and
  re-measured.
  A second observation — arming decay (even frozen) *appeared* to raise unlock churn
  (16/115 vs 0-5 baseline) — was **diagnosed as NOT an armed-path bug** (2026-07).
  A cross-mode sim (drain→render→tick_decay, faithful to `mixer::step`) proves an
  ARMED+frozen(`not_l0`) decay is BIT-IDENTICAL to disabled over the same delivery
  trace (unlocks/locks/held/silence/output **and a per-period FNV checksum of the
  rendered PCM** all equal), and the code path confirms it: in the UNPRIMED case
  (`floor_prime_pending == false`), `tick_decay` with `dll_l0=false` snaps the held
  target back to the ceiling every tick, so the setpoint never differs from the
  disabled path — mechanically inert. (This scoping is the #1145 invariant as it
  stands after #1161: a FLOOR-PRIMED lane instead HOLDS the floor on `dll_l0=false`
  — frozen_reason `prime_hold` — a documented, separately-pinned divergence, NOT the
  ceiling snap; see the prime-aware `NotL0` hold in the floor-prime bullet below. The
  bit-identical pin's own trace is never primed, so it exercises exactly the unprimed
  branch this sentence describes.) The 16-vs-115
  spread is the same environmental USB-coalescing variance that moves the
  default-path counter (1 ↔ ~295); correlating it with the decay env was coincidental
  at n=2. Pinned by
  `armed_frozen_decay_is_bit_identical_to_disabled_over_the_same_trace` in
  `rust/jasper-fanin/src/lane_resampler.rs`. The pin's trace has two regimes: an
  initial coalescing-churn window (keeps the identity non-vacuous — `disabled` really
  unlocks) followed by a long clean **locked** tail with `dll_l0=false` throughout.
  The tail is load-bearing: it is the only regime where `stable_periods` accrues past
  the ~1875-period warm-up window, so deleting the `not_l0` snap-back makes the armed
  run decay `held` down over the tail while the shipped code holds it at the ceiling —
  a divergence the churn window alone can NOT catch (every unlock resets
  `stable_periods`, so decay could never step there regardless). The churn itself is
  the static-cushion geometry issue guarded above, not the decay code.
- *Why host-clock is OFF in final settings*: the fill-based probe could not pass in
  combo mode, so its probe steps perturbed each session start for zero benefit; the
  resampler alone (±500 ppm) carries stability, proven across 5-min and 20-min runs
  free-running. The correction-observable redesign (#1144) removes the "cannot pass
  its own probe" blocker; host-clock stays OFF by default pending the on-hardware
  validation and decay re-measurement above.

**Fleet validation (2026-07-03):** AirPlay PASS on jts3 (HiFiBerry), jts4 (Pi Zero 2 W
streambox), jts5 (post-deploy; pre-deploy receiver was wedged — reset cleared it), and
jts.local (after a shairport AP2-wedge reset — the known Tier-3 class). Spotify PASS on
jts.local (router `start_playback` → librespot) and jts3 (`transfer_playback`). Snapcast:
no bond configured fleet-wide — nothing to regress.

**Perceptual context (why we stopped here):** ITU-R BT.1359 audio-lag detectability is
−125 ms; the floor sits ~2.6× below it. Between ~46 and the theoretical-report's 20 ms,
no supported use case changes state (live-monitoring needs ≤10–15 ms, unreachable on any
variant of this product). Remaining engineered headroom if ever needed: probe redesign +
decay (−3..5), HAT output profile (−4.6).

### Measured results — 2026-07-02 descent campaign (jts.local, Apple dongle)

Full ring graph (fan-in → Ring A → CamillaDSP → Ring B → outputd) + USB DIRECT
combo mode + queuelimit 1 + both rings at 2 slots + DAC 128/256. Click impulses
via the Mac gadget lane; span = fan-in ingress tap → outputd `:9891` reference
tap; ALL-IN adds probe-measured gadget dwell (+3.9 ms, mean avail ~186 f) and
DAC delay (+9.9 ms, mean ~477 f = 256-frame ring + USB URB queue).

| config | measured p50/p95 (ms) | end-to-end p50/p95 (ms) |
|---|---|---|
| pre-campaign baseline (aloop chain) | 173.6 / 181.5 | ~187 / ~195 |
| host-slaved + cushion (certified) | 139.3 / 156.7 | ~153 / ~170 |
| full ring graph, chunk 128, 4-slot | 70.1 / 73.5 | 83.7 / 87.1 |
| + USB DIRECT (bridge deleted from path) | 45.1 / 46.8 | 58.7 / 60.5 |
| **+ both rings 2-slot (floor, 1-min)** | **35.4 / 36.7** | **≈49 / ≈50** |
| **floor, 5-min confirmation (159 impulses)** | **34.8 / 36.8 / p99 37.1** | **≈48.6 / ≈50.6** |
| floor + fan-in host-clock DLL live (1-min) | 34.6 / 36.8 | ≈48.4 / ≈50.6 |
| **floor + DLL, 5-min closing run (160/160, 100 %)** | **35.0 / 36.6 / p99 37.2** | **≈48.8 / ≈50.4** |

First 5-min confirmation: 99.4 % match, zero xruns, zero problem journal
lines, resampler locked throughout with the gadget **free-running** (bridge
standby had the DLL off — the gap that motivated the fan-in relocation below).
Closing 5-min run (DLL relocated into fan-in, `JASPER_FANIN_HOST_CLOCK=enabled`):
100 % match; probe passed and the ladder ran `l0_locked` with fill pinned near
setpoint, then **demoted to `l2_fallback` at a stream-restart transient and the
floor held anyway** — the fail-safe posture works, and at cushion 256 the lane
resampler's ±500 ppm authority alone carries 5-minute stability. Cushion 128
*under* the DLL locks (85 unlocks vs 15,513 free-run) but regresses latency
(+1.9 ms p50): lock churn re-primes fill above setpoint. **The config floor is
final at cushion 256**; shrinking the resampler pool needs post-lock
cushion-decay product work in `lane_resampler`, with the DLL holding the
decayed target.

Refuted knobs (each a clean 1-min negative): resampler cushion 128/128 (lock
never holds — the 256 floor is lock-hold hysteresis, not aloop burstiness);
CamillaDSP `target_level` 384→256 (no effect under queuelimit 1); chunk-64 slot
geometry as config (`RING_SLOT_FRAMES = 128` is a compile-time constant).

The remaining latency to a 40 ms end-to-end target is located, all product code.
Note the host-clock DLL relocation into fan-in is **already shipped** (it landed
with the fan-in platform / combo change, not remaining work): the "floor + DLL"
rows in the table above measured ≈0 ms delta versus the free-running floor — the
DLL's win is removing the standby drift *wander* (fill no longer walks ~500 f off
the 256 target across a 5-min window), not a step reduction in the steady-state
floor. The real remaining levers are:

1. **Resampler post-lock cushion decay** (`lane_resampler`): shrink the resampler
   pool below the cushion lock-hold floor by decaying the held target *after* the
   DLL locks, with the DLL holding the decayed target so lock churn does not
   re-prime fill above setpoint (the cushion-128-under-DLL run locked but
   regressed +1.9 ms p50 without decay). Est. −2.7..5.3 ms. **Shipped
   (DEFAULT-OFF, still lab-only, awaiting the hardware dial-in below):**
   `CushionDecay` (a pure, render-period-clocked state machine in
   [`lane_resampler.rs`](../rust/jasper-fanin/src/lane_resampler.rs)) lowers the
   held target from the acquisition ceiling (`target + warmup cushion`) toward a
   floor, ONE `_DECAY_STEP_FRAMES` step every `_DECAY_INTERVAL_MS`, but ONLY while
   the lane is locked AND the host-clock DLL ladder is `l0_locked` AND a 10 s
   stability window has passed AND `|commanded_ppm| ≤ 400` (the cascade guard). It
   SNAPS BACK to the ceiling in one tick on any unlock / DLL demotion / stream
   stop (a raised setpoint refills naturally — no glitch). The DLL setpoint tracks
   the resampler's LIVE held target via a single shared `held_target_frames`
   gauge (single source of truth — the servo thread re-pins `set_target_fill_frames`
   from it each tick, so the two controllers can never disagree). Env (all
   default-off / current-behaviour): `JASPER_FANIN_RESAMPLER_CUSHION_DECAY=enabled`
   plus `_DECAY_FLOOR_FRAMES` (default and min = `max(target, minimum_safe_fill) +
   32`, max the ceiling), `_DECAY_STEP_FRAMES` (16, range 1..=64),
   `_DECAY_INTERVAL_MS` (1000, range 250..=10000), all fail-loud-validated when
   armed. STATUS surfaces `inputs[].resampler.held_target_frames` (live) and
   `inputs[].resampler.decay{active,floor_frames,frozen_reason}`. Requires the
   host-clock DLL armed (decay gates on `l0_locked`), so it only engages in USB
   DIRECT combo mode.

   **The floor cannot descend onto the physical unlock threshold.** The lane
   underfill-unlocks the instant the cursor-relative fill drops below
   `minimum_safe_fill_frames = ceil(period × max_ratio) + kernel_radius + 1` (=
   274 at period 256 / ±500 ppm) — the same threshold the render loop's underfill
   gate uses (shared `jasper_resampler::minimum_safe_fill_frames`). A held target
   at/below that value is churn-by-construction: ordinary per-period fill jitter
   crosses it and the lane thrashes (audible gap → snap-back → relock → 10 s
   warm-up → re-descend, on repeat). So the floor's lower bound is
   `max(target, minimum_safe_fill) + 32`, not the bare `target + 32` — for a small
   base target `target + 32` alone can land below the physical floor. Config
   validation rejects a churny floor fail-loud when armed; `DecayParams::build`
   also clamps defensively as belt-and-braces.

   **Hardware dial-in protocol (owed):** with the DLL locked (`l0_locked` in
   `/state`), arm decay with defaults and run 1-min playback windows watching
   `held_target_frames` descend from the ceiling toward ~`target+32` and the
   route-latency harness confirming the fill/latency drop (expect ~−3.5 ms at the
   default floor). Then try a lower/tighter floor ONLY if unlock_count stays 0
   during the steady window. Finish with a 5-min run on the best floor: WAV-loop
   restarts are natural stream stops (fill must re-prime at the ceiling and
   re-decay each loop with no lock churn); if loop restarts thrash the decay,
   raise `CUSHION_DECAY_STABILITY_MS`. Failure mode = revert the env flag (the
   default path is byte-identical to today).

   **Two cascade-guard observations to expect during dial-in** (both honest /
   conservative — no code change owed, but watch for them via
   `decay.frozen_reason="cascade"` in STATUS):
   - The cascade guard only *pauses* decay; it never escalates. A sustained
     `|commanded_ppm| > 400` excursion that stays L0 (not railed, so no L2
     demotion / snap-back) holds the *current* held target indefinitely — the only
     escalation below the guard is the audible underfill unlock. If a run parks at
     `frozen_reason="cascade"` and the latency win stalls, the DLL is steering hard
     at that setpoint; investigate the host clock offset rather than lowering the
     floor.
   - `commanded_ppm` includes the probe's feed-forward seed, so a host whose
     natural clock offset exceeds ~400 ppm (still legal — the DLL's L1 warn is
     2500 ppm) sits *permanently* above the cascade guard and decay never engages
     there (`frozen_reason="cascade"` from the first stable tick). That is the
     correct conservative behaviour (a large steady bias means the fill is not
     truly calm), but it means the decay win is host-clock-dependent: verify the
     observed host sits well inside ±400 ppm before concluding decay is broken.
2. Gadget drain cadence: standing avail ~186 f → ~64 f (~2.6 ms). **Lever-2
   instrumentation + knob shipped (default-preserving, still lab-only):**
   `drain_direct_capture` now records the drain-ENTRY `avail` into a since-boot
   `DrainStats` (count/sum/max + a fixed 6-bucket 64-frame-step histogram,
   boundaries `[0,64,128,192,256,320,+]`), surfaced additively in STATUS at
   `inputs[].direct.drain_avail{count,mean,max,hist}` and logged every 2048
   drains as `event=fanin.direct.drain_stats`. The gadget OPEN period is now
   tunable via `JASPER_FANIN_USB_DIRECT_PERIOD_FRAMES` (default 256 =
   byte-identical to today; fail-loud range 32..=1024). The capture buffer stays
   DEEP regardless (`resolve_direct_buffer_frames`: ≥ 3 periods AND ≥ 768 frames,
   period-aligned) — a small period rides a deep buffer, NOT the refuted shallow
   2-period URB-headroom failure.

   **Read the stats as WINDOW DELTAS, not the raw lifetime `mean`.** `drain_stats`
   is since-boot cumulative, and one drain is sampled *every* render cycle the
   gadget PCM is open — including while the host is attached but silent (Mac
   wired, nothing playing). Those attached-idle drains record `avail≈0` into
   bucket 0 and into the `sum`/`count` denominator, so the lifetime `mean`
   **understates the real playback dwell in proportion to idle time**. On
   jts.local (Mac wired 24/7) a 10-min idle before a 1-min playback run buries
   ~11k playback samples under ~112k zeros → STATUS `mean` reads ≈17 f even if the
   true playback dwell is unchanged at ~186 f. Do NOT read the lifetime `mean`
   directly. Instead poll STATUS twice — once immediately before the playback
   window, once immediately after — and compute the window mean from the deltas:
   `Δsum/Δcount`, with the `Δhist` bucket deltas as the window's distribution.
   `count`/`sum`/`hist` are proper monotonic counters, so the bracketed deltas
   isolate the playback dwell from idle zeros. (An attached-idle `avail≈0` sample
   *during playback* is itself the H1 quantization signal — the recording is
   correct; only the lifetime aggregate is diluted.)

   On-hardware decision rules (all reads are window deltas per the note above):
   - **H1 (period granularity):** set `=64`, run ≥1 min of playback bracketed by
     two STATUS polls, and look at the WINDOW mean (`Δsum/Δcount`) and `Δhist`. If
     the window mean drops toward ~64 f (and the `Δhist` distribution de-quantizes
     off the 0/256 bimodal) **with zero new capture xruns**
     (`event=fanin.xrun … usb_direct lane`), the pointer-granularity hypothesis
     holds — keep 64. Any new capture xruns → revert (`unset`, back to 256).
   - **H2 (drain-phase artifact):** if the WINDOW mean (`Δsum/Δcount` across the
     playback bracket, NOT the idle-diluted lifetime mean) is already ~64 f
     (0..128) while the older probe read ~186, the standing dwell was a probe
     sampling artifact, not real latency ahead of the tap — the honest fix is
     accounting (this instrumentation IS the evidence), not a period change.
3. DAC URB queue: `delay` ~477 f against a 256-frame ring (~2–3 ms in
   snd-usb-audio queueing).

## Host-slaved USB clock (Stage 1)

Default-**OFF** mechanism + telemetry + evidence, landed alongside the Stage 0
click/capture harness above. It commands the HOST's USB audio clock instead of
only reconciling the offset in software on our side — a structurally different
lever from the fan-in USB input resampler, which absorbs the same standing
rate offset in the digital domain. Source:
[`rust/jasper-usbsink-audio/src/host_clock.rs`](../rust/jasper-usbsink-audio/src/host_clock.rs)
(the module docstring there is the authoritative derivation; this section is
the operational summary).

### Mechanism

The Pi's UAC2 gadget already exposes a writable ALSA control on the capture
device — `"Capture Pitch 1000000"`, iface=PCM, numid=1 — that both macOS and
Windows honor dynamically as an asynchronous-feedback pitch command (verified
live on jts.local, kernel 6.12.75: range `750000..1005000`, `fb_max=5`,
`c_sync=async`, `req_number=2`). Writing a value above/below `1000000`
(identity) tells the host to run its USB audio clock faster/slower.

Stage 1 closes a delay-locked loop over that control:

- **Error signal**: gadget capture ring fill (frames) minus a target,
  computed from the *existing* `SharedState` atomics — the audio thread is
  untouched, no new capture/playback code path.
- **Control loop**: `jasper_clock::Dll` (the same PipeWire-`spa_dll` port
  `jasper-fanin`'s lane resampler and `jasper-outputd`'s reference clock use),
  ticked at a fixed 1 Hz on the state-publisher thread (not the audio thread).
- **Actuator**: an ALSA ctl write to `"Capture Pitch 1000000"`, rate-limited
  to <=1 Hz and only when the commanded change is >=10 ppm (no ctl spam), and
  clamped to <sup>±</sup>1000 ppm total (feed-forward + DLL trim combined) —
  independent of the wider hardware range above. The ctl handle lives ONLY on
  the state-publisher thread: single writer by construction, the audio thread
  and the preempt listener never touch it. The element is resolved by its
  `(iface=PCM, name)` tuple, **never by numid** — numid 1 is a `u_audio.c`
  registration-order artifact, not ABI, so pinning it could silently retarget
  a future kernel's write (e.g. onto `PCM Capture Volume`); matching by name
  keeps the daemon path aligned with the unit's name-based `ExecStopPost`.

### Two controllers in cascade — the defense

With the feature enabled, the fan-in `lane_resampler` (fast inner loop,
`rust/jasper-fanin/src/lane_resampler.rs`) and the outer pitch loop both
discipline the same chain. JTS has a documented oscillation failure class when
two rate controllers fight (the CamillaDSP `rate_adjust` + `AsyncSinc` incident,
above). This is a legitimate CASCADE instead — a fast inner loop absorbing
residual + jitter, a slow outer loop removing the standing offset at its source
(the host). **The two modes defend against a fighting cascade with DIFFERENT
outer control laws**, because they present the outer loop different plants:

- **`Fill` mode (usbsink solo) — the DLL, defended by bandwidth separation.**
  The plant is an integrator (a pitch command sets the gadget-fill *slope*), so a
  slow DLL is the right outer law, and the defense is bandwidth separation derived
  from the actual inner-loop constant, not asserted:
  - **Inner loop**: `RateController::with_max_resync` → `DllConfig::for_rate(256,
    48000)`, updated once per rendered period (≈5.33 ms). Adaptive bandwidth
    clamped to `[BW_MIN, BW_MAX] = [0.016, 0.128] Hz`. Locked floor **0.016 Hz**,
    acquiring maximum **0.128 Hz**.
  - **Outer loop**: `DllConfig{period:4800, rate:48000, initial_bw:BW_MIN,
    bw_retune_period:0}` ticked at exactly 1 Hz, retune disabled so the number is
    fixed and testable. Effective bandwidth = `0.016 × (4800/48000) / 1s =
    0.0016 Hz`.
  - **Separation**: 10× below the inner loop's locked floor, 80× below its
    acquiring maximum — ≥10× in every inner-loop state.
- **`Correction` mode (fan-in combo) — a pure integral, NOT the DLL.** The plant
  is near-UNITY DC gain through the inner loop's lag (ppm→ppm, no integrator), and
  a third-order DLL against that plant limit-cycles regardless of its bandwidth —
  bandwidth separation is the wrong defense here. The outer law is a single slow
  integrator (`CORRECTION_INTEGRAL_GAIN`, chosen a factor of ~5 below the measured
  ring threshold against the real inner controller); one integrator around a near-
  unity plant is unconditionally stable at that gain. See the mode's control-law
  discussion in the mode-selection section above, and the servo-sim tests that
  close the ladder against the real `RateController`.

The slow settle is deliberate: PipeWire's docs warn UAC2 pitch control oscillates
at a normal DLL bandwidth. In both modes the per-session probe's neutral baseline
phase measures the raw host offset and seeds the commanded bias with
`-baseline_obs` on entering `L0_LOCKED` (feed-forward), so coarse correction is
immediate and the slow outer law only trims the residual.

**The falsifier**: `fill_variance` (EW variance of the gadget fill) and
`fill_slope_ppm` are published on every enabled tick precisely so a soak can
detect a cascade limit-cycle — a two-controller oscillation shows up as periodic
fill variance the counters make visible. In combo (`Correction`) mode
`correction_ppm` is the additional limit-cycle tell: a fighting cascade shows
periodic correction ppm even though the servo's L0 target is `correction → 0`.
Watch all three across a soak before trusting L0 lock long-term; if any shows
periodicity, the remediation is mode-specific — widen the bandwidth separation in
`Fill` mode, LOWER `CORRECTION_INTEGRAL_GAIN` in `Correction` mode, or leave the
feature off.

**Cascade interaction with cushion decay (holds under the integral law).** The
DEFAULT-OFF post-lock cushion decay gates on the servo's REVERSE signals —
`ladder_l0` (decay only steps while `l0_locked`) and `commanded_milli_ppm` (the
`|commanded_ppm| > 400` cascade-stability freeze guard). Both signals keep their
exact meaning under the correction-observable servo: `commanded_ppm` is still the
pitch command (same units, same ±1000 clamp), so the 400-ppm guard still freezes
decay while the servo is working hard, and the stability gate still reads `l0`
exactly as before. With the pure-integral `Correction` law a settled L0 genuinely
sits at a small, STEADY `commanded_ppm` (the feed-forward that cancels the crystal
offset) with the correction relaxed to ~0 — verified against the real inner loop
across the crystal/lag/noise/3600 s matrix, NOT the limit cycle the earlier DLL
law produced — so it is well inside the guard and decay proceeds normally once the
post-lock warm-up window elapses. (Under the pre-fix DLL law this paragraph was
false: the composite loop railed `commanded_ppm` ±350 ppm on a ~21 s period, so
the freeze guard flapped twice per cycle and decay never advanced.)

### Host-compliance persistence — prime at floor (DEFAULT-OFF, rides the decay flag)

The cushion decay's ~2.5-min descent (held-target ceiling → floor) is otherwise
paid at EVERY session start, because priming always begins at the full ceiling.
Host-compliance persistence removes that recurring cost: once a session **proves**
the host and the decay has **landed at the floor cleanly**, the proof is written to
`/var/lib/jasper/fanin/host_compliance.json`, and every subsequent session (whether
across a reboot OR later in the SAME daemon lifetime) primes the resampler AT the
decay floor — the descent is skipped. There is **no new top-level flag**: this
extends `JASPER_FANIN_RESAMPLER_CUSHION_DECAY` (it is inert on a decay-off box).
Path override: `JASPER_FANIN_HOST_COMPLIANCE_PATH`.

> **The prime is PER-SESSION, not construction-only (fixed 2026-07-03).** The
> common real-world case is a Mac that sleeps nightly and wakes to a NEW session
> with the fan-in daemon up for months. That every-morning session must prime at
> the floor, not pay the ~110 s descent again. The prime is therefore seeded at
> BOTH lane construction AND at every session boundary: the session-end snap-back
> (`snap_decay_back_honoring_proof`) re-primes at the floor when a live, unrevoked
> proof is present, and at the ceiling otherwise. See "Prime-at-floor" and
> "session-boundary snap" below. (Before this fix the prime ran only in
> `Mixer::new`; session B in one daemon lifetime silently descended from the
> ceiling — hardware-diagnosed on jts.local, two sessions in one daemon lifetime.)

Schema (v1, atomic tempfile+rename, world-readable 0644):
`{schema, proved_at_epoch_s, probe_response_ratio, floor_frames, consecutive_failures}`.

**Write condition (the FULL proof, not just a probe pass).** Written once per
session, when ALL hold together for a settle window (the same
`CUSHION_DECAY_STABILITY_MS` the decay warm-up uses): the decay has reached
`at_floor` (descent complete) AND the DLL has held `l0_locked` AND the resampler's
unlock count has NOT advanced across the window (zero churn). Any disqualifier
re-arms the window. Owned by the pure `ComplianceProof` state machine in
`rust/jasper-fanin/src/host_compliance.rs`, ticked once per render
period by `mixer::step`.

**Prime-at-floor (per session).** The prime is seeded at the floor
(`CushionDecay::prime_at_floor`) at TWO points: (1) at lane build time
(`build_host_compliance_state` → `resampler.prime_decay_at_floor`) if a valid proof
is on disk whose `floor_frames` equals the live decay floor (a floor retune between
sessions invalidates it — descend normally); and (2) at every **session boundary**
within the daemon lifetime, via `snap_decay_back_honoring_proof` — the primitive
`reset()` (idle/host-pause/device-loss/xrun-recovery — the aloop and usb-direct
xrun-recovery paths `recover_resampler_input_xrun` / `recover_direct_xrun` call it
too) and `unlock_for_underfill()` (starvation = the natural session end, e.g. the
Mac stops streaming) both call. In both cases the held target starts at the floor
and a `floor_prime_pending` latch holds it there across the not-yet-locked prime
periods so `try_lock` seats the cursor AT the floor (the deep-prefill arm). The
ceiling is unchanged, so a REVOKE still snaps all the way back to it.

**Session-boundary snap destination (the single source of truth).** At a session
boundary the snap goes to the FLOOR iff a live, unrevoked proof is present, else the
CEILING. The "is the proof live" signal is the SAME `flag_present` atomic the mixer
sets on a valid load / successful write (`on_written`) and CLEARS on a revoke
(`on_revoked`) — read at snap time by `LaneResampler::live_proof_present`. There is
no second copy of "is the proof valid": a revoke clears `flag_present` before any
subsequent snap, so the very next session boundary after a revoke lands at the
ceiling; a clean floor session lands at the floor. The **unconditional** ceiling
snap (`snap_decay_back` via `snap_decay_to_ceiling`) is reserved for the
revalidation-failure escape below — it always re-acquires deep. The same
`flag_present` also drives the per-lock `floor_primed` the revalidation tracker
re-samples at each rising edge (below), so the snap destination and the
revalidation gate can never disagree.

**Revalidation triggers (one strike for evidence, two for a probe measurement).**
The servo's per-session probe (the #1142 post-lock `AwaitLock` gate) runs on EVERY
session start — that IS the revalidation. For a floor-primed session, three
triggers can revoke the proof: a LIVE probe FAIL, a DLL demotion to L2, and a
CONFIRMED early-window CHURN cycle (below). The revalidation runs in the pure
`RevalidationTracker` (`host_compliance.rs`), driven each render period by
`mixer::service_host_compliance` from the resampler's live `is_locked()` /
`unlock_count()`.

> **Two-strike probe fail (jts.local 2026-07-03).** A probe FAIL is a
> MEASUREMENT, not proof the host changed — the lock-gated probe can spuriously
> fail if it runs while the resampler's correction is railed (the 2026-07-03
> false-fail: a floor-primed session whose held target snapped to the ceiling
> post-lock railed at −500 ppm while the DLL rebuilt the fill, so a probe reading
> against that rail would see baseline ≈ step ≈ −500 → response_ratio ≈ 0 → FAIL).
> The rail's exact mechanism — the post-lock `NotL0` snap — is now root-FIXED by the
> prime-aware hold (see the floor-prime bullet), so that specific rail no longer
> occurs. (An earlier CORRECTION-mode unrailed-settle guard also targeted this, but
> was REMOVED 2026-07-05 — it deadlocked beyond-authority hosts whose correction
> rails steady-state; see the settle-guard bullet in the productization section.)
> Even so,
> costing the household the ~2.5-min
> descent on ONE ambiguous read is the wrong trade. So a probe fail is
> handled by the pure `classify_strike` (`PROBE_FAIL_STRIKE_LIMIT = 2`): the FIRST
> fail RETAINS the proof, persisting an incremented `consecutive_failures` (and
> leaving `flag_present` TRUE), and only the SECOND consecutive fail — two
> independent sessions disagreeing with the proof, which IS a host change worth
> distrusting — deletes it. A probe PASS resets the counter to 0 (persisted, via
> `on_pass_reset` on a live pass at L0, and also naturally via the clean
> descent-settle re-write's `on_written`). **`DllDemotion` and a confirmed
> `EarlyUnlock` churn cycle stay ONE-strike** — they are direct positive evidence
> the floor itself is failing on this host, not an ambiguous probe read. The mixer
> emits `event=fanin.host_compliance.strike_retained` (proof kept, counter bumped),
> `.revoked` (deleted), and `.pass_reset` (counter cleared) as distinct events.
>
> **The current session ALWAYS snaps back to the ceiling on any strike** (retained
> or delete) and re-descends — so the audible behaviour of the session that took
> the strike is identical to today either way; only the on-disk proof and the NEXT
> session's prime differ. In the steady state the on-disk `consecutive_failures`
> is 0 (a healthy proof) or the file is absent (revoked); it transiently reads 1
> between a first spurious probe fail and the next session's pass/second-fail.
>
> **Interaction with the #1154 snap SSOT (intended semantics — state it
> explicitly).** A counter=1 (retained-strike) session keeps `flag_present == true`,
> so its NEXT session's session-boundary snap still lands at the FLOOR and it primes
> at the floor again. This is the point of the two-strike design: one bad
> measurement must NOT cost the floor. `flag_present` is only cleared on an actual
> REVOKE (delete) — the second consecutive fail, a DLL demotion, or a confirmed
> churn — after which the next snap lands at the ceiling and the session descends +
> re-proves, exactly as before. STATUS surfaces the counter as
> `resampler.compliance.consecutive_failures`.

**Guarding the railed-acquisition failure mode.** The 2026-07-03 rail is
root-fixed by the prime-aware `NotL0` hold (above), so the probe no longer
baselines against that snap rail. Floor-prime seating below is
defense-in-depth for an exotic geometry and a no-op on default boxes. (An
unrailed-settle guard also lived here briefly but was REMOVED 2026-07-05 — it
deadlocked beyond-authority hosts; see the settle-guard bullet in the
productization section.)

- **Floor-prime seating (`lane_resampler::try_lock`) — DEFENSE-IN-DEPTH for an
  exotic geometry, NOT the fix for the observed rail.** When a lane is floor-primed,
  `try_lock` gates OFF the shallow bounded-prime fall-through so the lock can only
  seat AT the full floor depth (`floor + kernel radius + 1`). **In default geometry
  this is a no-op.** The fall-through arm requires `fill >=
  fallthrough_prefill_frames()` while the deep arm (checked first) requires `fill >=
  floor + radius + 1`; whenever `floor <= fallthrough − radius − 1` (~1024 fr at the
  default period 256 / target 512) the deep arm is the ONLY arm a floor-primed lane
  can reach, so the fall-through was already unreachable while primed. jts.local
  (target 512, period 256, floor 576, ceiling 2560) has deep-prefill 593 and
  fallthrough 1041, so a floor-primed lock ALWAYS seated exactly at the floor (593
  fr) via the deep arm — **at the parent commit too**. The gate only bites when the
  operator raises the decay floor above ~1024 (the geometry where the fall-through
  prefill sits below the floor prefill); there it stops a shallow seat that would
  rail while the fill built up to the higher floor, and its regression test
  (`floor_primed_lock_does_not_seat_shallow_via_fallthrough`) constructs exactly
  that geometry (floor 1100). COST: **zero vs the parent in default geometry** (the
  deep seat was already the only reachable path — byte-identical seat, lock-error,
  and first-audio latency); only the exotic deep-floor case pays `floor −
  minimum_safe` extra buffered frames before first audio. **Honesty:** in that same
  exotic geometry, a trickle producer stalled with fill in `[fallthrough_prefill,
  floor_prefill)` previously locked (railed but AUDIBLE) and now primes SILENTLY
  with no bound and no cue — the fall-through's slow-producer guard IS load-bearing
  at that depth. STATUS's fill / `held_target_frames` gauges expose it; acceptable
  for the operator-set deep-floor case. NON-PRIMED (ceiling) sessions are unchanged —
  `is_floor_primed()` is false, so the deep prefill is the full ceiling and the
  fall-through still fires on the bounded-prime expiry exactly as before.

  > **The observed jts.local rail's true cause — PINNED and FIXED (the prime-aware
  > `NotL0` hold).** The false-fail sat at baseline≈step≈−500 ppm FLAT for ≥19 s
  > (through the 4 s baseline AND the 15 s step). A floor-height held target (576)
  > cannot produce that — from the deepest shallow seat the underfill-unlock
  > threshold (`minimum_safe_fill` ≈274) bounds the deficit at (576−274)=302 fr, so
  > the rail could last at most 302/(48000·5e-4)≈12.6 s and would SHRINK as the fill
  > builds, never reading −500.0 flat through the step tail. A ≥19 s exact rail
  > requires a CEILING-SCALE held target (~2560) relative to the fill during the
  > probe — i.e. something RAISED the held target AFTER lock. It was the decay's
  > `snap_back`: a floor-primed lock seats AT the floor, but at session start the
  > outer ladder is NECESSARILY still Probing (`dll_l0_locked == false`; l0 arrives
  > only after the ~21 s probe), so the FIRST locked `tick_decay` cleared the prime
  > latch and took the `NotL0` branch — snapping the held target floor→ceiling and
  > railing the DLL to rebuild the fill 576→~2560 for ~40 s. Race-dependent: a probe
  > that completed before the first snap saw a quiescent baseline (the 2026-07-03
  > primed-session PASS, baseline −9.6). **FIX:** while a floor prime is live for the
  > session, `CushionDecay::tick`'s `NotL0` branch HOLDS the held target at the floor
  > (frozen_reason `prime_hold`) instead of snapping to the ceiling — a floor prime
  > is a deliberate divergence the `NotL0` branch must respect. The prime-hold ends
  > when (a) the ladder reaches l0 (normal `at_floor`/warm-up accounting resumes),
  > (b) any revocation fires (`snap_decay_to_ceiling` wins, latch cleared), or (c)
  > the session ends (boundary snap re-primes at the floor if the proof is still
  > live, else the ceiling). The old ARMED+frozen(`NotL0`) == disabled invariant
  > (#1145) STILL HOLDS for the UNPRIMED case and is scoped explicitly; the primed
  > case is a documented, tested divergence. The two-strike ProbeFail (#1160) and the
  > churn discriminator (#1156) remain the safety net for a genuinely bad host riding
  > the held floor during Probing. Pinned by
  > `primed_notl0_lock_holds_floor_until_l0_arrives` (decay level) and
  > `primed_notl0_probe_window_stays_quiescent_no_rail` (LaneResampler level — the
  > ≥19 s −500 ppm rail is impossible by construction), both-ways mutation-guarded.

- **Unrailed-settle guard (`jasper-host-clock`, CORRECTION mode) — REMOVED
  2026-07-05 (`settle_regime_ok` reverts to `obs.locked` in both modes).** This
  guard briefly required the smoothed correction to be UNRAILED (below
  `CORRECTION_SETTLE_RAIL_GUARD_PPM = 450`) before the settle window could accrue.
  Hardware on jts.local falsified it: the Mac host runs ~+600 ppm fast — BEYOND the
  lane's ±500 ppm inner authority — so under the NEUTRAL pitch AwaitLock commands
  the correction rails at +500 STEADY-STATE and can only come off the rail with the
  servo's pitch help. The guard waited for the unrail; the unrail needed the pitch
  authority the guard withheld — a deadlock. Two consecutive sessions sat in
  `ProbePhase::AwaitLock` for 6+ min (`correction_mean_ppm` pinned 500.0,
  `pitch_ppm_commanded` 0.0, no compliance proof written). Lock-only settle is
  correct because the probe steps AWAY from the nearer rail (a railed baseline stays
  measurable — an earlier jts.local session probed `baseline=500 → step=258`,
  `response_ratio=0.807` PASS) and a STEADY-STATE clipped rail is fail-biased, never
  pass-biased (a stationary clipped baseline understates demand, so a truly
  non-compliant host reads `ratio≈0 → FAIL`; for a stationary rail, removing the
  guard cannot manufacture a false PASS). A DECAYING transient rail is the one case
  that can INFLATE the ratio (its observable slews toward the step direction as it
  decays, mimicking a compliant response) — but that is a latent class the 450 guard
  never closed either (it only delayed baselining until the smoothed correction fell
  below 450; a slow decay keeps moving through the step window regardless). The
  transient the guard was built for (the 2026-07-03 `NotL0` snap-back rail, below)
  was root-fixed by the prime-aware `NotL0` hold, so it no longer exists. Transient-
  decay false passes are closed by root-fixing transient causes (#1161) plus the
  two-strike ProbeFail (#1160) and churn discriminator (#1156) — the standing safety
  net for a genuinely bad host, NOT the settle gate. Pinned by
  `correction_probe_settle_accrues_at_the_rail`
  + `beyond_authority_railed_host_probes_pass_then_fail` in the `jasper-host-clock`
  crate.

**The EarlyUnlock churn discriminator — a relock is required (#1156, hardware-diagnosed
on jts.local 2026-07-03).** The underfill-unlock trigger is TWO-PHASE, not a bare
falling-edge revoke. This is the fix for a false-revocation that #1154 shipped: EVERY
session end presents as an underfill unlock — when the host stops streaming, deliveries
stop and the cursor-relative fill drains below `minimum_safe_fill` within *milliseconds*,
long before any idle classification. So the earlier "any early-window underfill unlock
revokes on the falling edge" rule burned the proof on EVERY session shorter than the
60 s window. macOS makes that the COMMON case: CoreAudio stops the UAC2 device stream
seconds after the last client, so a notification ding / a preview / a short clip is a
sub-60 s session that always ends this way — the proof would be revoked forever and the
next session would always re-descend from the ceiling, defeating the whole feature. The
discriminator distinguishes **churn** (the host is STILL delivering yet the floor cannot
hold — the lane unlocks *and relocks*) from a **terminal stream-end** (the host stopped —
the lane unlocks and never relocks):

- An early-window underfill unlock **ARMS a pending strike** (records the strike and
  resets the tick-clock `periods_since_arm`) — it does NOT revoke.
- The strike **CONFIRMS** (revoke `EarlyUnlock`) only if a **RELOCK** (rising edge)
  arrives within `HOST_COMPLIANCE_CHURN_CONFIRM_SECS` (5 s, converted to render periods
  and compared purely in ticks — never a wall clock). Unlock→relock cycling is the
  evidence: the host is present and the floor is genuinely failing.
- If no relock arrives within the horizon, the pending strike **EXPIRES harmlessly**
  (the stream died — no churn); the next lock is armed clean. This is a bound, NOT an
  absolute "never survives a session" — see the accepted-residual note below.

A churn STORM (many unlock/relock cycles) revokes on the FIRST confirmed cycle: the
confirming relock clears `flag_present` (via `on_revoked`), and the tracker latches
`floor_primed = floor_primed_now && revoke.is_none()`, so the relocked session is no
longer floor-primed and does not revalidate again — exactly one revoke.

**This INVERTS the old "won't self-heal a periodic-stall host" caveat.** The earlier
version of this doc noted that a floor-fatal-but-ceiling-survivable stall spaced >60 s
apart would never trip the (window-bounded) underfill trigger, so the lane ran shallow
with one dropout per stall and the proof never self-revoked. Under the discriminator a
periodic stall on a host that *keeps streaming* — stall → underfill unlock (arm) → the
host recovers and delivers again → relock within a second or two (well inside the 5 s
horizon) → CONFIRM — now **does** revoke and revert to the ceiling. That is correct: a
present-but-stalling host IS churn worth revoking. Two profiles still never revoke on the
underfill path: (1) the one this fix is FOR — a session that simply ended (the host
stopped; no relock at all); and (2) a host that stalls **longer than the 5 s horizon** and
then resumes — the strike expires before the resume-relock arrives, so that relock finds no
armed strike. (2) is acceptable, not a gap: a >5 s delivery stall underruns at ANY fill
depth (the deepest cushion buys only tens of ms), so revoking would not have prevented the
dropout — the proof gains nothing from tripping. Persistent non-compliance on such a host
is still caught by the per-session live probe FAIL and mid-stream `saturated_slope`
DllDemotion, which revert the proof regardless, as before.

**Accepted residual: a quick restart inside the horizon is indistinguishable from
churn.** The strike "expires harmlessly" only when nothing relocks within the horizon.
It is NOT an absolute "never survives a session": a strike survives into any relock that
arrives ≤ `HOST_COMPLIANCE_CHURN_CONFIRM_SECS` (~5 s) after the arming unlock, and the
tracker has no signal to tell a genuinely-new stream's first lock apart from a churn
relock — both present as "an armed strike, then a rising edge inside the horizon." So the
ordinary human timeline *ding at t=0 → CoreAudio stops the device stream at t≈2 s (arm)
→ start music at t≈4–6 s → gadget stream restarts and the lane relocks* CONFIRMS the
prior (compliant) session's strike: **one spurious revoke**, self-healing — that session
runs from the ceiling and re-proves over the ~2.5-min descent, and the next session primes
at the floor again. This is the accepted cost of the horizon, not a bug: the horizon can't
shrink much below ~2× the bounded-prime fall-through (`max_prime_periods` ≈ 1 s) without
missing genuine bursty-host churn, and the tracker cannot distinguish a Δ≤5 s restart from
churn. Anyone debugging a `revoked reason=early_unlock` line that fired shortly after a
quick stop-then-play should read it as this residual, not a discriminator bug. (It is
mechanically the same as the churn test — a new stream starting inside the window IS a
confirmed cycle from the tracker's point of view.)

**Three correctness details the tracker encodes.** (1) The arming underfill is evaluated
on the lock-LOSS edge, because `unlock_for_underfill` sets `locked=false` in the SAME
render period it bumps `unlock_count` — so the period that carries the churn evidence is
the one where `locked` is already false (a `locked`-only window gate would make the arm
unreachable). Arming is gated on the unlock count actually ADVANCING, so an idle `reset()`
(host pause — `unlock_count` unchanged) does not arm, and a subsequent resume-relock is a
clean new session, not a confirmed churn. (2) A probe FAIL only revokes when it is LIVE —
the servo leaves `probe_result=Fail` across a session boundary, so a fresh lock on a new
compliant host reads the stale FAIL until its own probe runs; the tracker gates on the
ladder sitting at L2 (which always accompanies a live fail) so a stale carryover FAIL
(ladder back in `Probing`, `ladder_l2=false`) is ignored. (3) The revoke-before-relock
ORDERING is pinned (interaction with #1154's snap-destination SSOT): on the relock that
CONFIRMS a churn strike, `step` latches `floor_primed=false` even though `floor_primed_now`
is still true, because the mixer clears `flag_present` right after `step` returns. This
makes the revoke "win" the relocked lock's floor consideration — lock B is not
floor-primed, so it does not run a redundant second strike, and the very next
session-boundary snap lands at the ceiling, matching the flag the mixer is about to clear.

Because the prime is per-session, `floor_primed` is **re-sampled per lock** from the live
`flag_present` at each rising edge (the mixer passes it into `RevalidationTracker::step`):
a session B that primed at the floor off session A's fresh proof runs the revalidation
exactly as a construction-time prime would; the session after a revoke (proof cleared) is
NOT floor-primed and descends + re-proves without it. The per-lock revoke latch likewise
resets on every fresh lock, so a re-proven session can strike again if the host later
misbehaves — both are per-lock, not per-daemon-lifetime. On any strike the mixer snaps the
held target back to the full ceiling via the **unconditional** escape
(`snap_decay_to_ceiling`, distinct from the proof-honouring session-boundary snap). What it
does to the FILE then depends on `classify_strike`: a DELETE revoke (DLL demotion, confirmed
churn, or the SECOND consecutive probe fail) clears `flag_present`, removes the file, and
logs `event=fanin.host_compliance.revoked reason=…`; a RETAIN strike (the FIRST probe fail)
persists a bumped `consecutive_failures`, LEAVES `flag_present` true (so the next session
still primes), and logs `event=fanin.host_compliance.strike_retained`. The normal descent
then re-proves and re-writes (clearing the counter). **USB re-enumeration / a new host / a
new port need NO special handling** — a new session simply re-probes, and the probe verdict
revalidates; that is the new-machine/new-port answer. A missing/corrupt/stale file means
"no proof" (descend as today) — fail toward today's behaviour, never a crash.

**Session-start is content-agnostic — calibration runs on pure silence.** macOS
holds the UAC2 output stream open and always-streaming even with no audio playing
(silent frames). Every gate this feature depends on is **fill/rate-based, never
RMS-gated**: the resampler's lock is `ring.fill_frames() >= prefill` (a byte-count,
`lane_resampler::try_lock`); the servo probe/L0 run on the fill slope
(solo) or the resampler's correction ppm (combo), not amplitude; and the write
condition reads `decay_at_floor` / `dll_l0_locked` / `unlock_count`. The usbsink
`rms_dbfs` gauge feeds ONLY the STATUS `playing` flag / metering, never audio flow
or the lock/probe. So the whole prove→persist→prime→revalidate cycle exercises on a
silent-but-streaming host; the on-device validation run proves it on hardware.

**Cold-start validation protocol (jts.local, Apple dongle, decay armed).**
1. Arm decay (`JASPER_FANIN_RESAMPLER_CUSHION_DECAY=enabled`) + combo host-clock,
   delete any existing `host_compliance.json`, restart fan-in.
2. Start the Mac USB output (music OR just an open silent stream — the
   silence-fact means either works). Watch
   `curl -s http://jts.local:8780/state | jq '.audio_graph.fanin.inputs[]|select(.resampler).resampler.compliance'`
   and `journalctl -u jasper-fanin | grep host_compliance`.
3. Confirm the descent runs (~2.5 min, `decay.frozen_reason` walks
   `warmup→""(decaying)→at_floor`), then `event=fanin.host_compliance.written` and
   `compliance.flag_present=true`, `proved_at` set.
4. **Per-session win (SAME daemon lifetime):** stop + restart the Mac stream (a new
   session, fan-in still up). This is the every-morning-wake case. The
   session-boundary re-prime is SILENT in the journal (the resampler is log-free by
   design) — verify it from `/state` instead: the held target sits at the floor from
   the first lock (`resampler.held_target_frames` == the floor, `decay.frozen_reason`
   == `at_floor`) with no 2.5-min descent, `compliance.flag_present` stays `true`, the
   probe passes, and no revoke fires. Measure e2e latency — expect the settled floor
   number (~46.1), not the ceiling. (`event=fanin.host_compliance.prime_at_floor` is
   the CONSTRUCTION-time signal — it fires once at daemon start when a boot proof is
   loaded, step 4b below — not on a within-lifetime session boundary.)
4b. **Cold-start win (across a reboot):** restart fan-in (or reboot) with the proof
   on disk. Confirm `event=fanin.host_compliance.prime_at_floor` at startup, the held
   target at the floor from the first lock, the probe passes, and no revoke fires.
4c. **CONFIRM ON-DEVICE — the prime-aware `NotL0` hold holds the floor (root cause
   FIXED; this is the on-device confirmation of the hardware-free proof).**
   During a floor-primed session's FIRST ~30 s, poll `/state` at high cadence
   (e.g. every 200 ms:
   `curl -s http://jts.local:8780/state | jq '.audio_graph.fanin.inputs[]|select(.resampler).resampler|{held_target_frames,decay:.decay.frozen_reason}'`)
   and grab the fan-in journal for the same window. The floor-prime bullet above
   pins the observed −500-ppm rail as the decay's `NotL0` snap on the FIRST locked
   tick (ladder still Probing, `dll_l0=false`) having raised the held target
   floor→ceiling — now fixed by the prime-aware hold. The EXPECTED (fixed) signature:
   `decay.frozen_reason` stays `prime_hold` (then `at_floor` once l0 arrives) and
   `held_target_frames` HOLDS at the floor (576) through the whole settle window,
   with `correction_ppm` quiescent (nowhere near the ±500 rail). The REGRESSION
   signature (should be impossible now): `frozen_reason` transitions to `not_l0`,
   `held_target_frames` JUMPS toward the ceiling (2560), and `correction_ppm` rails
   at ±500 while it rebuilds — if that ever appears, the prime-aware hold has
   regressed (the `primed_notl0_*` tests would already be red) — capture the trace
   and re-diagnose. The latency win the feature exists to deliver (no ceiling rebuild
   + re-descent on a primed session) now lands from the first lock.
5. **Silence check:** repeat step 4 with the Mac output OPEN but PLAYING NOTHING
   (pure silence). The lane must still lock, the probe still run, and the prime
   still hold — proving the calibration is content-agnostic.
6. **Revocation check (two-strike probe fail):** with a proof present, force a
   probe FAIL (e.g. a non-compliant host / a `PROBE_PPM` under the Windows deadband
   on a Windows box, or unplug-replug to a different machine). The FIRST fail emits
   `event=fanin.host_compliance.strike_retained`, snaps the held target back to the
   ceiling for that session, but KEEPS the file (now `consecutive_failures=1`) — so
   the next session still primes at the floor. A SECOND consecutive fail emits
   `event=fanin.host_compliance.revoked reason=probe_fail`, deletes the file, and
   the next session descends from the ceiling. A `DllDemotion` /
   `reason=early_unlock` revoke deletes on the FIRST strike (one-strike, unchanged).
   A probe PASS between fails emits `event=fanin.host_compliance.pass_reset` and
   clears the counter back to 0.

State machine + I/O: `rust/jasper-fanin/src/host_compliance.rs`; two-strike policy:
`classify_strike` / `PROBE_FAIL_STRIKE_LIMIT`; floor-prime seating:
`lane_resampler::try_lock` (`is_floor_primed()`); pre-probe settle gate:
`jasper-host-clock` `settle_regime_ok` (lock-only in both modes — the
CORRECTION-mode rail guard was removed 2026-07-05, see the settle-guard bullet
above); mixer wiring: `mixer::service_host_compliance`; STATUS:
`resampler.compliance {flag_present, proved_at, revoked_reason_last,
consecutive_failures}`.

### Cross-platform conditions

- **macOS**: honors asynchronous feedback well — the gold path for this
  feature.
- **Windows** (`usbaudio2.sys`): honors feedback dynamically but with a
  ~163 ppm reaction deadband, and IGNORES commanded values outside roughly
  nominal ±1 sample/interval — hence the ±1000 ppm servo clamp above sits
  inside that validity window with margin, not at the wider hardware range.
  That same deadband floors `JASPER_USBSINK_HOST_CLOCK_PROBE_PPM` at 200
  (config-rejected below that): a probe at or under ~163 ppm would measure
  near-zero response on a compliant Windows host and falsely fail every
  session. Even the default 300 leaves modest margin against a full-deadband
  subtraction ((300−163)/300 ≈ 0.46 vs the 0.5 pass ratio), so a Windows lab
  box that demotes spuriously should raise `PROBE_PPM` toward 500–600. Windows
  validation is deferred (macOS is the shipping-gold target); this is the
  caveat to keep in mind when it happens.
- Both react slowly, which is why the outer loop's bandwidth must stay this
  low rather than matching the inner loop's.

**Per-session probe rationale**: the host OS or the playing application can
change between sessions (a Mac unplugged and a Windows box plugged in later;
an app that opens the endpoint in a mode that pins the rate). Compliance is
therefore re-measured on every `(host_connected && playing)` edge rather than
trusted once at boot — a probe commands a bounded step (default +300 ppm for
a 4 s baseline + 6 s step window) and measures the observable's response (the
fill-slope in `Fill` mode, the resampler-correction mean in `Correction` mode —
see "Observable mode" above); a response under half the commanded step demotes
straight to `L2_FALLBACK` (neutral pitch) without ever entering `L0_LOCKED`.

**The probe does NOT baseline at the session edge — it waits for the lane to
leave its warmup ramp first.** A session begins the instant audio starts
flowing, but at that instant the lane is still filling: the fan-in resampler's
held target ramps from empty (0 → held target) and the gadget ring primes.
Baselining THEN measures that one-time warmup fill ramp as if it were the
host's natural rate slope — hardware-diagnosed on jts.local 2026-07-03, where
the 4 s baseline read `baseline_slope_ppm=1460.6` (the ramp, not clock drift),
the step then read `step_slope_ppm=-1397.6`, `response_ratio=-9.5` ⇒
`probe_fail` ⇒ `l2_fallback` for the whole stream. Prior "passes" (ratios 0.78,
2.97) were the same contamination landing luckily inside the pass band. So the
probe now opens in an **await-lock** wait, commanding neutral, and does not
begin the baseline until the lane reports LOCKED AND that lock has held
continuously for a 2 s settle. The two daemons map LOCKED differently by
construction:
- **fan-in (combo)**: resampler `locked_state`. The fan-in warmup ramp is a
  genuine 0 → held-target fill climb that must complete before baselining, so a
  live lock signal is the right gate.
- **usbsink solo**: simply `playing` (settle-only). usbsink's only
  start-of-session contaminant is the sub-second gadget-ring prime + one-time
  capture-backlog slurp, which the 2 s settle covers. It is deliberately **NOT**
  a live `fill >= target` gate: nothing steers the ring toward target while the
  probe holds neutral (the DLL servo that pins fill only runs post-probe in L0),
  so a host slower than our DAC keeps the ring at its underflow floor for the
  whole session — a fill-level gate would leave the probe stuck in await-lock
  forever and the feature silently inert. `fill_frames` is still published for
  telemetry/the slope falsifier; it just does not gate the probe.

If the lane un-locks mid-baseline (or mid-step), the in-flight measurement is
discarded and the wait restarts — a warmup re-entry is not a compliance
failure, so this does not demote to L2.
`/state.…​.host_clock.probe.waiting_for_lock` is `true` only while a LIVE
session's probe is holding in await-lock (it is `false` between sessions — the
ladder rests in `probing`/await-lock while idle, but `session_active` gates the
flag so an enabled-but-idle box does not read as an active-session claim). The
journal marks the wait with `event=<prefix>.host_clock_probe_wait
reason=await_lock|lock_lost` and the actual baseline start with
`event=<prefix>.host_clock_probe_start`. A session that ends while still in
await-lock (never baselined) logs `event=<prefix>.host_clock_probe_result
result=await_lock_ended` — distinct from `result=aborted`, which is reserved
for a real baseline/step measurement cut short.

### Ladder states

`DISABLED -> PROBING (await-lock -> baseline -> step) -> L0_LOCKED <-> L1_WARN`,
with any state falling to `L2_FALLBACK` on non-compliance evidence (probe
failure, or a sustained saturated-command + adverse-slope condition
mid-stream). The **await-lock** sub-phase holds neutral from the session rising
edge until the lane leaves its warmup ramp (locked for the 2 s settle), so the
baseline measures clock drift rather than the fill ramp; lock loss in any later
sub-phase returns to await-lock (no demotion). `L2_FALLBACK` only re-attempts
`PROBING` at the next idle boundary (stream stop / host disconnect) — it does
not free-run a demonstrably non-compliant host mid-session. `L1_WARN` is a
locked-but-watch state (unusually high sustained commanded ppm) with no
functional difference from `L0_LOCKED` beyond the doctor/telemetry surfacing.

### Pitch neutrality — the safety invariant

A host must never be left slaved to a stale command by a crashed or stopped
daemon. Enforced in four layers, all gated on usbsink OWNING the ctl
(`owns_host_clock_ctl()` = `!audio_standby` — in combo standby fan-in owns it,
and usbsink stays fully hands-off; F1): (1) a startup neutralize
write when the ctl opens, even with the feature disabled — heals a crashed
predecessor; (2) the state-publisher's exit path resets to neutral on clean
exit, SIGTERM/SIGINT, and audio-thread error (main sets the shared shutdown
flag before joining); (3) every ladder transition into `L2_FALLBACK` or an
idle boundary force-writes neutral; (4) belt-and-braces
`ExecStopPost=-/usr/bin/amixer -c ${JASPER_USBSINK_MIXER_CARD} cset
iface=PCM,name='Capture Pitch 1000000' 1000000` in
[`deploy/systemd/jasper-usbsink.service`](../deploy/systemd/jasper-usbsink.service)
covers SIGKILL / OOM-kill / watchdog abort, which layer (2) structurally
cannot reach (the card is expanded from `JASPER_USBSINK_MIXER_CARD`, packaged
default `UAC2Gadget`, so an operator card override redirects this line too; the
belt itself is gated on `STANDBY != 1` so it doesn't stomp fan-in's combo
command either). In SOLO mode all four apply regardless of
`JASPER_USBSINK_HOST_CLOCK` — a stale non-neutral value could only exist if the
feature had been enabled and the daemon then died uncleanly. In combo standby
usbsink writes the ctl at NO layer: fan-in is the sole owner, so usbsink
neutralizing at all would reset fan-in's live command behind its back.

### Enabling on a lab box

```sh
printf 'JASPER_USBSINK_HOST_CLOCK=enabled\n' | sudo tee -a /etc/jasper/jasper.env
sudo systemctl restart jasper-usbsink
```

Tunables (each documented in `.env.example` with the full range/rationale):
`JASPER_USBSINK_HOST_CLOCK_TARGET_FILL_FRAMES` (default 384 ≈ 8 ms),
`JASPER_USBSINK_HOST_CLOCK_PROBE_PPM` (default 300),
`JASPER_USBSINK_HOST_CLOCK_PROBE_SECONDS` (default 6, step phase; a fixed 4 s
baseline phase always runs first, itself gated behind a fixed 2 s lock-settle
wait — see "The probe does NOT baseline at the session edge" above). The servo
clamp (±1000 ppm), write epsilon/cadence (10 ppm / <=1 Hz), tick interval
(1 Hz), and the 2 s lock-settle are fixed Rust constants, not env-tunable —
see `host_clock.rs`'s pinned-constants block.

### What evidence `/state` gives

```sh
curl -s http://jts.local:8780/state | jq .audio_graph.rust_bridge.host_clock
```

null on a pre-Stage-1 build or unreadable state file; otherwise:

```json
{
  "enabled": true,
  "ladder": "l0_locked",
  "pitch_ppm_commanded": -42.5,
  "fill_frames": 380,
  "fill_slope_ppm": 1.2,
  "fill_variance": 4.0,
  "dll": {"err_frames": -4.0, "locked": true},
  "probe": {"last_result": "pass", "response_ratio": 0.91, "waiting_for_lock": false},
  "demotions": 0,
  "transitions": 2,
  "last_transition_reason": "probe_pass"
}
```

`dll.locked` is diagnostic only (expected `false` under the 256-frame fill
quantization at this tick rate) — the ladder (`ladder` field) is the lock
authority a consumer should read. `jasper-doctor`'s `check_usbsink_host_clock`
skips when the feature is disabled or the block is absent, warns on
`l2_fallback` (with `last_transition_reason` + lifetime `demotions`) and
`l1_warn`, and otherwise reports the live `ladder`/`pitch_ppm`/`fill` numbers.
`event=usbsink_audio.host_clock_*` journal lines (probe start/result, every
ladder transition, pitch resets, saturation) give the per-event trace; there
is no per-tick log spam.

### Explicit non-goals for this stage

Does not shrink the `lane_resampler` warm-up cushion, does not bypass or
modify `lane_resampler`, does not touch fan-in / outputd / CamillaDSP.
Cushion shrink is a separate, measurement-gated follow-up once L0 lock has
been soaked and evidenced on hardware.

## Productization Plan

The current stable loopback path is the fallback floor, not the final
low-latency architecture. Productization means keeping the protection/correction
invariant while replacing measured latency bottlenecks with frame-bounded,
observable clock-domain crossings.

1. **Ship the stable fallback without a low-latency pass.** Keep
   `usb_low_latency_48k` route policy, Rust USB bridge, fan-in USB resampler,
   CamillaDSP, and outputd final reference wired as above. Doctor must continue
   to fail the low-latency claim until measured route evidence exists.
2. **Build the real measurement harness.** DONE — `jasper-route-latency-harness`
   (source: `jasper/cli/route_latency_harness.py` + `jasper/route_latency/`) is
   the click-in/capture-back producer `jasper-route-latency-artifact` binds
   samples to the live route identity from. Its `quick`/`promotion` presets are
   sized directly off the certification gates with margin (quick: 240 impulses
   over 6 minutes for p95 <= 40 ms; promotion: 1200 jittered impulses over 36
   minutes for p99 <= 60 ms). See
   [`docs/testing-tooling.md` "Route-latency click/capture harness"](testing-tooling.md#route-latency-clickcapture-harness)
   for the architecture and the quick/promotion walkthroughs above. **Still
   owed:** an on-device end-to-end run against real jts.local hardware — the
   harness is unit-tested against synthetic evidence (`tests/test_route_latency_harness.py`
   includes a clock-drift injection test) but has not yet produced a real
   artifact from an actual click-track playback + XVF3800 capture, so the
   low-latency claim remains correctly failing until that run happens.
3. **Replace the bottleneck, not the DAC owner.** The current loopback graph
   cannot meet 40 ms because the USB resampler held target alone is ~42.7 ms.
   The next architecture step is a frame-bounded transport at one or both
   ALSA-loopback boundaries. Preserve these contracts:
   - outputd remains the sole physical DAC owner and final-reference publisher;
   - CamillaDSP remains in the protection/correction path;
   - TTS/cues still enter the same protected graph and are included in the final
     reference;
   - every foreign clock has one explicit rate matcher, surfaced in `/state`.
4. **Keep source claims honest.** USB can have a low-latency profile because it is
   wired and local. AirPlay, Spotify, Bluetooth, DLNA, and future network sources
   stay buffered and observable; they must not inherit USB's route claim. Their
   `/state` surfaces should report fill/target/lock/ppm/xruns, not pretend to be
   5 ms clients.
5. **Keep DAC support declaration-driven.** A new DAC earns low-latency defaults
   through `DacProfile.latency_floor` plus hardware evidence. Unknown DACs stay on
   conservative defaults. Composite DACs need their own clock/child-identity
   contract before they can claim this route.
6. **Make AEC a profile contract, not a footnote.** The final AEC reference is
   outputd's post-Camilla/post-protection electrical signal. Any bit-perfect or
   bypass profile must explicitly declare AEC degraded/unsupported unless it
   proves final-reference truth. Software AEC must align mic-clock capture to the
   outputd/DAC reference domain; chip AEC needs a hardware profile that proves the
   chip reference is coherent with the actual speaker output.
7. **Promote only with evidence.** A route can move from fallback to production
   low-latency only after quick validation, promotion validation, and a 24-hour
   soak with no sustained USB resampler unlocks, no ring rails, no DLL clamp or
   resync storm, and no outputd/fan-in xruns.

## Legacy Cleanup Plan

Remove legacy only after the replacement is live, measured, and covered by
doctor/state/tests. Until then, keep old paths default-off or historical so the
speaker remains recoverable.

| Legacy path | Status | Cleanup trigger |
|---|---|---|
| Python/PortAudio USB audio bridge | Explicit lab only: exposed as `jasper-usbsink-python-lab`, refuses without `JASPER_USBSINK_PYTHON_LAB_ALLOW=1`, and not allowed in claiming route | Delete after Rust bridge has any missing hotplug/state coverage and no tests/docs need the old callback model |
| lean FIFO USB-only route (`JASPER_LEAN_LANE`, `USBSINK_OUTPUT_MODE=fifo`, lean RawFile capture) | Historical/deferred; solo-only and bypasses fan-in mixing | Remove after a shared frame-bounded route either meets 40/60 or the project explicitly chooses a separate solo profile with AEC-degraded semantics |
| `transport_pipe` fan-in↔Camilla dual FIFO coupling | Failed/default-off lab path for low latency; Pi page size makes it too deep | Remove or quarantine after the new frame-bounded transport replaces its diagnostic value |
| outputd `rate_match` content bridge for USB | Rejected for this route; produced content xruns/EAGAIN/partials in tuning | Keep only as a DAC/content clock-slip lab tool, or delete once no active diagnostic depends on it |
| stale low-latency prose and component estimates | Historical context only | Compress into dated appendices as product docs converge on measured route artifacts |

Before deleting any path, add a guard test that the production
`usb_low_latency_48k` route no longer emits or accepts its env knobs, and run a
Pi-side doctor pass to prove the fallback route still recovers.

## Historical Lean-FIFO Plan

## The latency problem (measured, shared/fan-in path)

Before the Rust bridge plus fan-in input resampler, USB routed through the shared
mixer had a steady-state Mac→DAC budget measured around **~70–100 ms, and
variable**. Contributors:

| Stage | Latency | Note |
|---|---|---|
| usbsink lane snd-aloop ring | **5–75 ms (sawtooth)** | the catch-up lets a free-running lane fill 1→14 periods before resyncing (`CATCHUP_HIGH_WATER_PERIODS=14`); measured at 43 ms mid-soak |
| usbsink→fan-in snd-aloop hop | ~one ring | first loopback |
| fan-in→CamillaDSP snd-aloop hop | ~one ring | second loopback (current `loopback` coupling) |
| CamillaDSP chunksize | ~5–20 ms | depends on the active chunksize |
| jasper-outputd DAC buffer | **~64 ms shipped default** | `snd_pcm_delay`, buffer/period 3072/1024 (the conservative global default); the Apple-dongle codified floor is 256/128 ≈ 10 ms |

Two structural costs dominate: the **catch-up sawtooth** (a drop-control tradeoff —
the high-water of 14 periods is sized to never false-trigger a healthy AirPlay
burst+stall, so it inherently buffers up to ~75 ms on the USB lane) and the **two
snd-aloop hops**. Neither is cheaply removable on the shared path.

## The Former USB-only Answer: the lean-fifo path

This remains historical/deferred. It is no longer the first production route.
When USB is the *sole* active source, it could route through the already-built
lean lane instead of the mixer:

```
usbsink (OUTPUT_MODE=fifo) → /run/jasper-usbsink/lean.pipe → CamillaDSP RawFile-capture
   (enable_rate_adjust + AsyncSinc) → jasper-outputd → DAC
```

This **deletes both snd-aloop hops AND the catch-up sawtooth**: CamillaDSP's async
resampler becomes the rate-correcting consumer disciplined by the real DAC clock, so
the pipe sits at a small fixed fill (no sawtooth, no drift overflow). Estimated
budget: CamillaDSP chunksize (~5 ms) + a small fifo + outputd DAC (~15–21 ms) ≈
**<40 ms achievable**, stable.

Tradeoff: the lean lane **bypasses the fan-in mixer**, so it is SOLO-only — AirPlay/
Spotify/BT/TTS don't mix while it's armed. The mux ladder switches solo↔shared.

## Historical Lean-FIFO Worklist (Superseded)

This list records the old solo-lane plan for archaeology. It is not the current
productionization sequence; use [Productization Plan](#productization-plan)
above for current work.

1. **Arm the lean lane through the mux ladder, not raw env.** DONE: mux now
   computes one shared source-route decision from
   `jasper.audio_runtime_plan.decide_source_low_latency_route`; the lean lane
   (`JASPER_LEAN_LANE=enabled`) and adaptive fan-in buffer consume that same
   USB-solo verdict, and `jasper.audio_runtime_plan.low_latency_feature_flags`
   is the single parser for both opt-in gates. Validate the live switch
   end-to-end and the TTS-while-solo handoff before default-on.
2. **Drive the camilla side via the existing lean-config path** (`jasper/usbsink/
   output_mode_reconcile.py` + the plan-owned `lean_capture_kwargs` RawFile
   capture shape — RawFile, not File; the jts5 fix). Confirm `--check` valid
   and no crash-loop.
3. **Tune the buffer floors to the DAC's real floor.** DONE (the #27 codification, landed
   2026-06-28). The DAC's stable buffer floor is now DATA on its `DacProfile`
   (`jasper/audio_hardware/dac.py`: the `LatencyFloor` dataclass + the optional
   `latency_floor` field), so a new DAC is declaration-only and zero per-user config.
   The shipped *global* default stays conservative — CamillaDSP chunk 1024 / target 2048,
   outputd period 1024 / dac_buffer 3072 (~64 ms) — and any DAC with no declared floor
   keeps it (non-breaking). The **Apple-dongle profile** declares the measured floor
   CamillaDSP chunk 256 / target 1536, outputd period 128 / dac_buffer 256 (≈ 10 ms),
   after the 2026-07-01 jts.local tuning pass rejected Camilla target 1024 and
   outputd period 64 / dac_buffer 128 due USB bridge playback xruns. The floor is
   a CamillaDSP (chunksize, target_level) PAIR — target must be ≥ 4x chunk so the resampler
   has fill headroom (chunk 256 → target 1536 on the Apple profile), enforced in
   `LatencyFloor.__post_init__`.
   Two consumers read the floor, each on its own path:
   - **The Python CamillaDSP config emitters** (`jasper/sound/camilla_yaml.py` +
     `jasper/active_speaker/camilla_yaml.py`) resolve the floor *directly from the
     active output DAC profile* — `resolve_camilla_chunksize` /
     `resolve_camilla_target_level` read the resolved output-hardware state
     (`/run/jasper-output-hardware/output_hardware.json`, the SAME state the
     reconciler / `jasper.output_hardware` use to pick a profile id) and look up that
     profile's `LatencyFloor`. This is env-independent on purpose: it reaches EVERY
     live generation path — `install.sh`'s `runtime-safe-graph`, the
     `jasper-camilla` ExecStartPre statefile guards, and `jasper-control`'s sound /
     active-speaker generation — none of which load `outputd.env`. Precedence is
     `max(JASPER_CAMILLA_CHUNKSIZE`/`_TARGET_LEVEL`, active profile floor) >
     global default: operator env may raise latency above the floor, but stale /
     over-aggressive below-floor env is clamped back to the profile floor. A state
     file that is absent or unreadable simply keeps the global default (a fresh box
     before the reconciler's first write is non-breaking, never an unloadable config).
   - **jasper-outputd (Rust)** reads `JASPER_OUTPUTD_PERIOD_FRAMES` /
     `_DAC_BUFFER_FRAMES`, which `jasper-audio-hardware-reconcile` emits from the
     active profile via `latency_floor_for(...)` into the wizard-owned `outputd.env`
     (mirroring the `JASPER_OUTPUTD_ACTIVE_CHANNELS` write). It also mirrors the two
     CamillaDSP keys there for observability. **Operator override precedence:** the
     outputd unit loads `jasper.env` BEFORE `outputd.env`, so when an operator sets a
     floor key in `jasper.env` the reconciler must *remove* that key from
     `outputd.env` entirely — writing it empty would override the operator's value
     with empty (and Rust would fall back to its hardcoded default, silently
     discarding the tune). The reconciler drops the key (via `jasper_env_file_unset`)
     so the operator's earlier-loaded value wins. A DAC with no declared floor likewise
     drops the keys so a stale floor from a previously-attached DAC cannot linger.
   DEFERRED: tier-aware chunksize (Pi 5 low / Pi Zero safe) and an install-time xrun
   auto-sweep — not yet built.
4. **Historical measurement target:** Mac→USB solo was aiming for <60 ms,
   ideally <40 ms, plus sustained-play and transition soak. The current route
   target is stricter and artifact-based: p95 <= 40 ms, promotion p99 <= 60 ms.
5. **Historical cross-platform reliability:** repeat the solo lean-fifo
   measurement on Windows + a second DAC only if this solo profile is revived.

## The shared-path alternative: per-input resampler (DEFAULT-OFF, first cut)

The lean-fifo above is SOLO-only. The *one-path* answer keeps USB in the
shared fan-in mixer but removes the catch-up sawtooth on that lane by
reconciling the host rate to the DAC clock at the fan-in **input edge** — a
per-input windowed-sinc resampler, DLL-steered to the DAC clock
(`rust/jasper-fanin/src/lane_resampler.rs`, composing the shared
`jasper-resampler` `AudioRing`/`SincTable`/`RateController`, the same crate
`content_bridge` uses). Moving reconciliation here also leaves CamillaDSP
DAC-paced without `rate_adjust` on the clockless USB input — dissolving the
underrun class that `rate_adjust` produced on-device. It is **DEFAULT-OFF**
behind `JASPER_FANIN_INPUT_RESAMPLER=enabled` (see HANDOFF-fan-in-daemon.md
"Per-input adaptive resampler") and is a **first cut owing on-device
real-time validation** — drop-free under sustained USB play + transitions,
latency below the catch-up sawtooth, lock stability, soak. It removes one
snd-aloop hop's worth of sawtooth but NOT the second snd-aloop hop or the
DAC buffer, so its floor is higher than the lean-fifo's; the eventual goal is
to make it good enough to delete the lean lane (the "converge to one path"
step), but that is gated on this validation.

## Why not just lower the catch-up high-water?
Lowering `CATCHUP_HIGH_WATER_PERIODS` would shrink the shared-path sawtooth but
re-introduce false-triggers on healthy AirPlay burst+stall transients (~12.4-period
peak) — trading latency for drops on every source. The lean-fifo gets low latency
*without* that tradeoff because it removes the sawtooth mechanism entirely.

Last verified: 2026-07-03 (20-minute final run at the merged floor: 640/640
impulses, e2e p50 45.8 / p95 47.3 / p99 48.1 ms; fleet AirPlay/Spotify pass).
Ladder "Observable mode" + "Two controllers in cascade" sections re-verified
2026-07-03 against the correction-observable redesign AND its staff-review fix
(`ObsMode::Fill`/`Correction`, `Obs.correction_ppm`, the pure-integral
`Correction` outer law `CORRECTION_INTEGRAL_GAIN`, the longer/adaptive-direction
`Correction` probe, `host_clock.obs_mode`/`correction_ppm` STATUS) on branch
`latency/combo-servo-correction`; servo-sim now closes against the real
`jasper_resampler::RateController`. Combo on-hardware validation still owed.
2048, CamillaDSP 256/1536, outputd 128/256, outputd content buffer 1536, and
direct ALSA loopback coupling. `jasper-route-latency-harness` — the
click-in/capture-back producer this doc previously described as missing — now
exists (`jasper/cli/route_latency_harness.py` + `jasper/route_latency/`,
hardware-free pytest including a clock-drift injection test) and
`sudo /opt/jasper/.venv/bin/jasper-route-latency-artifact` binds its output to
the live route identity. Neither has yet produced a real on-device artifact
from an actual click-track playback against jts.local's XVF3800, so doctor
correctly continues to fail the low-latency claim until that run happens.
The Stage 1 host-slaved USB clock mechanism/ladder/telemetry
(`rust/jasper-usbsink-audio/src/host_clock.rs`, default-OFF via
`JASPER_USBSINK_HOST_CLOCK`) landed the same day with hardware-free
pytest/cargo coverage on both sides of the state.json contract; on-device
compliance-probe validation against jts.local's Apple dongle host is a
separate, not-yet-run task.)

The **probe false-fail hardening** — the two-strike probe fail (`classify_strike` /
`PROBE_FAIL_STRIKE_LIMIT = 2`) and floor-prime seating (`lane_resampler::try_lock`,
defense-in-depth), plus a since-removed unrailed-settle guard (see below) — landed
2026-07-03 in response to the
jts.local journal evidence: a floor-primed session that had passed the probe at ratio
1.312 the prior session then read baseline=step=−500 ppm → response_ratio=0.000 → a
one-strike ProbeFail revoked the proof. **The rail's mechanism is NOT the one an
earlier draft of this doc asserted (a shallow lock ~274 fr building to the 576 floor).
That is provably impossible here** — floor-primed locks seat AT the floor (593 fr) via
the deep-prefill arm in jts.local's geometry, at the parent commit too, and a
floor-height held target bounds any shallow-seat rail below ~13 s and cannot read
−500.0 FLAT through the ≥19 s baseline+step. The real rail requires a CEILING-SCALE
held target during the probe: it was the decay's `NotL0` snap on the first locked tick
(ladder still Probing, `dll_l0=false`), which raised the held target floor→ceiling and
railed the DLL to rebuild the fill 576→2560. **That root cause is now PINNED and FIXED**
— the `NotL0` snap-back is prime-aware (see the floor-prime bullet above): while a floor
prime is live for the session, the `NotL0` branch HOLDS the floor instead of snapping to
the ceiling, so a primed session's correction stays quiescent through Probing and the
probe measures a real baseline. Two guards remain as defense-in-depth: the two-strike
policy means even one spurious probe read never costs the floor on the next session, and
floor-prime seating is a no-op in default geometry and only bites the exotic operator-set
deep floor. (A third, the CORRECTION-mode unrailed-settle guard, was REMOVED 2026-07-05 —
it deadlocked a beyond-authority host whose correction rails steady-state; see the
settle-guard bullet in the productization section and the 2026-07-05 appendix entry
below.)
Verified hardware-free: the `jasper-host-clock` crate tests (lock-only settle accrues at
the rail + the beyond-authority pass/fail composition + FILL-mode correction-oblivious,
mutation-guarded); macOS scratch crates for the `jasper-fanin` pure
logic (the prime-aware `NotL0`-hold decay tests + the LaneResampler-level hardware-
signature regression `primed_notl0_probe_window_stays_quiescent_no_rail` that makes the
≥19 s −500 ppm rail impossible by construction, both-ways mutation-guarded against the
#1145 unprimed bit-identical pin; the constructed-geometry floor-prime seat regression;
the `classify_strike` two-strike policy + on-disk lifecycle; the observability
strike-counter transitions; and the Part 1 × #1156 interaction — a floor-SEATED lock
still confirms churn on relock); and the `test_fanin_coupling_rust_contract` Python twin
(the prime-aware `NotL0`/`prime_hold` contract, STATUS `consecutive_failures`, the
strike_retained/pass_reset events, the `classify_strike`/`PROBE_FAIL_STRIKE_LIMIT`
policy). On-device re-verification of the full prove→prime→spurious-probe-fail→retain→
re-prove cycle against jts.local's Apple dongle — AND confirming the primed session now
holds the floor through the first 30 s (validation step 4c: `decay.frozen_reason` stays
`at_floor`/`prime_hold`, `held_target_frames` never jumps to the ceiling) — is a
separate, not-yet-run task (this session touched no Pi).

**Unrailed-settle guard REMOVED (jts.local 2026-07-05).** The CORRECTION-mode
rail guard added 2026-07-03 (`settle_regime_ok` refusing to accrue the AwaitLock
settle window while `|correction_mean| >= 450`) was falsified on hardware and
removed; `settle_regime_ok` reverts to `obs.locked` in both modes. **Deadlock
class — beyond-authority host.** jts.local's Mac host runs ~+600 ppm fast,
BEYOND the lane resampler's ±500 ppm inner authority. With the servo holding
NEUTRAL pitch during AwaitLock (by design), the correction rails at +500
STEADY-STATE and cannot leave the rail without the servo's pitch help — but the
servo (probe → l0 → TRIM) waits on the probe, the probe waits on the guard, and
the guard waits on the unrail that only the pitch can produce. Two consecutive
sessions sat in `ProbePhase::AwaitLock` for the full 6+ min session:
`event=fanin.host_clock_probe_wait reason=await_lock` at session start, then
NOTHING until `event=fanin.host_clock_probe_result result=await_lock_ended` at
stream stop; `correction_mean_ppm` pinned 500.0, `pitch_ppm_commanded` 0.0,
decay frozen `not_l0`, no compliance proof written. One earlier session escaped
only by racing the correction ramp-up (settle accrued while correction was still
climbing through <450 after a fresh daemon start), then probed FROM A RAILED
BASELINE — `baseline_obs_ppm=500.0, step_obs_ppm=258.0, response_ratio=0.807`
PASS — proving the probe math works from the rail. **Why removal, not bounding.**
(1) The transient the guard was built for — the floor-primed `NotL0` snap-back
rail (above) — was ROOT-FIXED by the prime-aware hold (#1161, prime-aware
`NotL0` snap-back), so it no longer occurs. (2) #1144's step-AWAY-from-the-rail
already makes a railed baseline measurable (the 0.807 pass is the hardware
proof). (3) A STEADY-STATE clipped rail is fail-biased, never pass-biased: a
stationary clipped baseline UNDERSTATES demand, so a truly non-compliant host
reads `baseline 500 → step 500 → ratio 0 → FAIL` — for a stationary rail, removing
the guard cannot create a false PASS. (The one exception is a DECAYING transient
rail, whose observable slews toward the step direction as it decays and can INFLATE
the ratio — but that class the 450 guard never closed either, since a slow decay
keeps moving through the step window after the smoothed correction drops below 450;
it is closed by root-fixing transient causes per (1) plus the two-strike/churn
revocation nets per (4), not by the settle gate.) (4) A steady
railed correction is indistinguishable from a beyond-authority host, which is
exactly the host the servo exists to serve; probing is measurement, not
commitment (fail verdicts stay retained/two-strike per #1160's other parts,
which are unchanged). Pinned by `correction_probe_settle_accrues_at_the_rail`
(railed-but-locked leaves AwaitLock and reaches a verdict, no deadlock) and
`beyond_authority_railed_host_probes_pass_then_fail` (a +600 ppm host: compliant
PASSES from the rail via the away-from-rail step, non-compliant FAILS —
fail-bias) in the `jasper-host-clock` crate.

**Fast stop→start "obs-dead" re-observation (jts.local 2026-07-05, 22:07 EDT) —
same deadlock, INVESTIGATED not re-fixed.** A separate hardware note observed
a ~0-1 s gap between a stream stop and a new stream start leaving the ladder "in
AwaitLock for the entire next session with dead observation (correction_ppm 0.0,
dll locked=false) while the lane itself was locked and railing; no session_start
transition fired." This is NOT a distinct session-edge re-arm bug — it is a
composition of the SAME beyond-authority rail deadlock above (note the "pre-rail-
guard-removal" timestamp): (1) the ladder was stuck in AwaitLock because the
correction railed at +500 under the neutral AwaitLock pitch (the deadlock), and
(2) "no session_start" is a CONSEQUENCE — across a sub-second stop→start the
resampler never lost lock, so `playing` (= resampler locked) never went false at
a 1 Hz tick, so no session falling edge and no fresh `begin_probe`; that is
CORRECT behaviour (an uninterrupted session must not re-probe), and it was only
pathological because the session it stayed in was the deadlocked AwaitLock one.
With `settle_regime_ok == obs.locked` (#1167) the railing session leaves AwaitLock
and reaches a verdict on its own, and a genuine stop→start (lock actually dropped)
re-arms the probe cleanly. Pinned by
`defect_f_fast_stop_start_does_not_deadlock_await_lock` in the `jasper-host-clock`
crate (a railing +600 ppm host reaches a verdict; a sub-second stop→start re-arms
a LIVE probe wait — never dead-in-AwaitLock). No code changed for this note.
