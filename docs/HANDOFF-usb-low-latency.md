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
nonzero bridge/usbsink/fan-in/outputd counter change across the measurement
window) and states whether the declaration *would* be justified — it never
asserts `--route-health-ok` on the operator's behalf; read the printed
deltas and decide.

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
correctly continues to fail the low-latency claim until that run happens.)
