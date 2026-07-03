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

## USB DIRECT (combo mode) — delete the bridge hop + aloop cable (DEFAULT-OFF PoC)

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

This is acceptable for the lab arming (combo is DEFAULT-OFF and hand-armed for
measurement, not a household posture), but it is a real containment gap, not
"nothing else changes." Wiring standby to publish an honest playing/arbitration
signal is the follow-up before combo could ship on by default.

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
byte-identical semantics; the only per-daemon difference is the `event=` log
prefix — `usbsink_audio` vs `fanin` — and which `JASPER_*` keys each parses).
Combo mode pins the DIRECT lane's resampler fill at target, removing the
standby-mode drift wander (the ~9 ms "standby gap" measured below). The
setpoint is the resampler's HELD target
(`JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES +
JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES`) — one setpoint shared with
the inner rate controller, so the outer loop never fights the inner integrator
(the ≥10× bandwidth separation of the cascade is derived in the
`jasper-host-clock` module docstring).

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
  `"direct":{"device","present","opens","retries"}`. The lane's frames/xruns
  ride the existing `frames_read`/`xrun_count`; its rate-lock rides the existing
  `resampler{}` block.
- Bridge STATUS/state.json gains additive `"standby":true|false` (schema_version
  stays 1); in standby `playing:false`, `rms_dbfs:-120`, ring/counters zero, and
  `host_connected` is best-effort from sysfs (`/sys/class/udc/*/state ==
  "configured"`). A misdirected harness run is diagnosable from `standby:true`.
- Transition logs: `event=fanin.usb_direct.present` / `.absent` (one line per
  presence change, device + errno + cumulative retries), `event=fanin.usb_direct.armed`
  at config load, `event=usbsink_audio.standby active=true` at bridge start.

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
`rust/jasper-fanin/src/lane_resampler.rs`) and this pitch DLL (slow outer
loop) both discipline the same chain. JTS has a documented oscillation
failure class when two rate controllers fight (the CamillaDSP `rate_adjust` +
`AsyncSinc` incident, above). This is a legitimate CASCADE instead — a fast
inner loop absorbing residual + jitter, a slow outer loop removing the
standing offset at its source (the host) — defended by bandwidth separation
derived from the actual inner-loop constant, not asserted:

- **Inner loop**: `RateController::with_max_resync` → `DllConfig::for_rate(256,
  48000)` (`JASPER_FANIN_PERIOD_FRAMES` defaults to 256), updated once per
  rendered period (≈5.33 ms). Adaptive bandwidth clamped to
  `[BW_MIN, BW_MAX] = [0.016, 0.128] Hz` (`jasper-clock`). Locked floor
  **0.016 Hz**, acquiring maximum **0.128 Hz**.
- **Outer loop** (this module): `DllConfig{period:4800, rate:48000,
  initial_bw:BW_MIN, bw_retune_period:0}` ticked at exactly 1 Hz, adaptive
  retune disabled so the number is fixed and testable. Effective bandwidth =
  `0.016 × (4800/48000) / 1s = 0.0016 Hz`.
- **Separation**: 10x below the inner loop's locked floor, 80x below its
  acquiring maximum — >=10x in every inner-loop state.

The slow settle is deliberate: PipeWire's docs warn UAC2 pitch control
oscillates at a normal DLL bandwidth, and at 0.0016 Hz alone the DLL would
take ~100 s to correct a standing offset — long enough to rail the tiny
3×256-frame gadget ring. The per-session probe's neutral baseline phase
measures the raw host offset and seeds the commanded bias with
`-baseline_slope` on entering `L0_LOCKED` (feed-forward), so coarse
correction is immediate and the slow DLL only trims the residual.

**The falsifier**: `fill_variance` (EW variance of the gadget fill) and
`fill_slope_ppm` are published on every enabled tick precisely so a soak can
detect a cascade limit-cycle — a two-controller oscillation shows up as
periodic fill variance the counters make visible. Watch both across a soak
before trusting L0 lock long-term; if either shows periodicity, the answer is
to widen the bandwidth separation further or leave the feature off.

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
a 4 s baseline + 6 s step window) and measures the fill-slope response; a
response under half the commanded step demotes straight to `L2_FALLBACK`
(neutral pitch) without ever entering `L0_LOCKED`.

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

Last verified: 2026-07-02 (jts.local clean 5-minute steady-state sample passed
with Rust bridge 256/3, fan-in input buffer 4096, USB resampler held target
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
