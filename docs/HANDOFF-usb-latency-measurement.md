# Handoff: USB-input latency — measurement method + productized defaults

This doc is the **measurement reference** for the USB-input audio path: how we
measure end-to-end latency, the numbers we get, the exact settings that produce
them, and the host/bench setup to reproduce it. For the *design* narrative
(why the ring graph, the host-clock DLL, the compliance ladder), read
[HANDOFF-usb-low-latency.md](HANDOFF-usb-low-latency.md) — this doc links to it
rather than restating it.

Verification history through 2026-07-11 (§1 gained the certified promotion
result — p50 36.35 / p95 37.93 / p99 38.29 ms, 1094 impulses — as current
truth, and the cert budget was tightened to p95<=40ms/p99<=42ms in
`jasper/audio_runtime_plan.py`; §7's "aspirational ~40 ms gated on leads"
framing was rewritten — the goal is met at the electrical plane, the leads are
now further-improvement candidates. Same day, earlier: §1 gained the
"Certification budget" cross-reference (then p95<=48ms/p99<=60ms); §7 gained
"Documented leads (not scheduled)", "Rejected paths (do not re-chase)", and
"Windows host validation (deferred)" subsections; claims re-verified against
`rust/jasper-fanin/src/config.rs`, `mixer.rs`, `host_compliance.rs`, and
`jasper/fanin/coupling_reconcile.py`. Prior 2026-07-07: jts.local live
probes, combo mux liveness patch, 2-slot ring geometry).

---

## 1. Results (current, hardware-measured)

The USB-input chain is `Mac app → hw:UAC2Gadget → fan-in direct capture →
[host-clock DLL + varispeed resampler + cushion] → Ring A → CamillaDSP → Ring B
→ jasper-outputd → Apple USB-C dongle → analog out`.

**Certified promotion result (2026-07-11) — current certified truth.** The
promotion-length certification run on jts.local (build `d5abf5ad`; artifact
`20260711T234400.457205Z__route_latency__apple_usb_c_dongle__route_latency__pass.json`;
route_hash `3bca2569c864ad1a`) measured, at the electrical `:9891` plane with
the flow-gated streaming detector, at the **576-frame churn-safe floor**:

| p50 | p95 | p99 | max | match | n | duration |
|---|---|---|---|---|---|---|
| **36.35** | 37.93 | 38.29 | 38.48 | 100% | 1094 | 32.6 min |

Zero outliers. This is the **route JTS owns** (fan-in ingress → outputd egress)
— the plane the cert gate certifies — and it clears the tightened p95<=40 /
p99<=42 budget (below) with margin. It reads ~4 ms below the 2026-07-07 `40.73`
p50 quick number because it is a longer, flow-gated measurement (1094 impulses
/ 32.6 min vs n=40) on a later build; both are the same 576 floor / `:9891`
plane, so the delta is run-length + build, not a measurement-point change. This
supersedes the 2026-07-07 electrical row as the current certified electrical
truth; that pair is kept below as the DAC-term composition basis.

**2026-07-07 electrical+analog cross-check (the DAC-term composition basis).**
Two independent measurements, taken through different capture points, at the
same **576-frame cushion floor** (the steady low-latency state):

| Measurement | Reference point | p50 | p95 | p99 | match | n |
|---|---|---|---|---|---|---|
| **Electrical** (`:9891`) | outputd queues period to ALSA | **40.73** | 42.12 | 43.17 | 100% | 40 |
| **Analog** (Scarlett) | dongle 3.5 mm output, post-DAC | **53.96** | 55.67 | 57.94 | 96.2% | 50 |

**Internal-consistency check:** electrical `40.73` + measured DAC term `13.23`
= `53.96` — composing to the directly-measured analog p50 **exactly**. Two
independent measurements a day apart validate each other.

**Certification budget (tightened 2026-07-11 to the certified floor):** these
are the numbers the route-latency cert gate now certifies against —
`USB_LOW_LATENCY_P95_BUDGET_MS = 40.0` / `_P99_BUDGET_MS = 42.0` in
`jasper/audio_runtime_plan.py`. `40.0` sits `2.1` ms over the certified p95
(`37.93`) and `1.5` ms over the observed max (`38.48`); `42.0` is the tail
budget, `2` ms above the p95 gate and `~3.7` ms over the certified p99
(`38.29`) — so any `>=2` ms regression trips the gate. It is a cert-time
tripwire only: no runtime consumer reads it. The prior `48.0` / `60.0` budget
was the honest-but-loose recalibration from the 2026-07-07 n=40 quick run,
before the promotion run proved the electrical plane holds well under 40 ms in
steady state. **Flap protocol:** a marginal fail gets ONE clean re-run
(steady-state, flow-gated) before it is treated as a regression; loosening
these numbers requires new measured evidence. **Config-hash note:** the budget
participates in `route_config_hash`, so tightening it invalidates the
2026-07-11 artifact's `config_match` — doctor reads `config_mismatch` until one
fresh certification run re-certifies against 40/42 (the measured numbers clear
it with margin; the run is ~35 min at the documented flow-gated methodology).
The ~40 ms goal is **met at the electrical plane** in steady state; the §7
"Documented leads" (`EarlyUnlock` revoke-policy tuning, DAC-side buffer trim)
are now further-improvement candidates — deliver it from the *first click* and
push below the floor — not gates on reaching it.

**Delta with the 2026-07-03 tap→ref number — explained, not a regression.**
[HANDOFF-usb-low-latency.md](HANDOFF-usb-low-latency.md) records a 2026-07-03
"FINAL" tap→ref p50 of **34.71** ms, ~6 ms below this doc's 2026-07-07
tap→`:9891` p50 of **40.73** ms. Both runs used identical ring geometry
(2-slot Ring A/B, Camilla chunk 128 / target 128 / queue 1) and the same
`:9891` tap, so the delta is not a measurement-point difference — it's the
deliberate operating-point change shipped in PR #1173 (commit `50d167e1`,
"combo becomes default"). The 34.71 ms figure was taken on the pre-productization
**lab recipe** (resampler target 256 + cushion 256 = held 512, host-clock DLL
and cushion-decay OFF, free-running); 40.73 ms is the productized **churn-safe
default** (target 512, cushion-decay floor 576, host-clock DLL + decay armed).
~3 ms of the delta is the higher steady resampler fill (~13.7 ms vs ~10.7 ms);
the rest is churn margin the lab recipe was borrowing — its lane rode the
underfill-unlock threshold (see the 256+256 guard discussion in
HANDOFF-usb-low-latency.md) — plus session-length variance (640 impulses over
20 min vs 40). Live corroboration on jts.local: the lane was observed
descending to held 644 / fill 657 (13.69 ms, matching this doc's ~13.8 ms
cushion term) then snapping back to the 2048 ceiling — direct observation of
the churn instability that makes the 576 floor the safe minimum on the
+600 ppm host. Both numbers can be treated as current: 34.71 ms describes the
lab recipe pre-#1173, 40.73 ms describes the shipped default.

**Definitive full chain (Mac app → analog out) ≈ 55.5 ms p50** = analog `53.96`
+ ~1.5 ms pre-tap ingress (Mac app → gadget URB → fan-in capture, upstream of
the tap).

Versus the previous 8-slot ring geometry (tap→ref p50 `54.3`): **−25 % on the
controllable Pi-internal path.**

### Where the latency lives (at the floor)

| Stage | ~ms | Source |
|---|---|---|
| Mac app → fan-in tap (pre-tap ingress) | ~1.5 | estimate |
| **tap → `:9891`** (queue to ALSA) | **40.73** | measured (electrical) |
|   — of which host-clock cushion (dominant) | ~13.8 | live `fill_frames` |
|   — Ring A (occupancy 2 × 128) | ~5.3 | STATUS |
|   — CamillaDSP (target 128 + chunk 128) | ~5.3 | config |
|   — Ring B (content.ring 2 × 128) | ~5.3 | STATUS |
|   — gadget capture dwell | ~0.9 | `direct.drain_avail.mean` |
|   — fan-in + outputd processing / buffer non-nominal-depth | remainder | — |
| **`:9891` → analog out** | **13.23** | **measured (analog − electrical)** |
|   — ALSA ring (`snd_pcm_delay`, 496 frames) | 10.33 | live |
|   — URB in-flight + dongle codec / analog reconstruction | 2.90 | residual |
| **Full Mac-app → analog** | **~55.5** | measured + ingress |

The single largest term is the **host-clock cushion (~13–14 ms)**, structurally
near its 576-frame floor. The next is the **DAC ring (~10 ms)** — the only
remaining shrinkable term (outputd URB-queue depth, est. −2–3 ms), the realistic
path toward ~50 ms. Note the DAC term is **larger than the 512-frame nominal
estimate**: the box runs a 496-frame `snd_pcm_delay` (10.33 ms) *plus* ~2.9 ms of
analog-domain presentation the electrical reference structurally cannot see — the
whole reason the analog capture was worth wiring up.

---

## 2. The productized settings (this is what a fresh install ships)

**Every value below is the shipped code default or is armed automatically by the
install-time auto-pass.** A fresh install on any box with the USB gadget stack
present reproduces this config with no operator action; an update to an existing
box converges to it on the next deploy. This section is the single reference for
"what are the low-latency USB settings."

### Ring geometry (code defaults — PR #1186)

| Knob | Value | Home |
|---|---|---|
| Ring A slots (`JASPER_FANIN_RING_SLOTS`) | `2` | `config.rs` `env_u32(…, 2)`; `jasper/fanin_coupling.py` `DEFAULT_FANIN_RING_SLOTS`; `deploy/alsa/conf.d/60-jts-ring.conf` `n_slots`; all lockstep |
| Ring A period | `128` frames | conf.d `period_frames`; ioplug fixed |
| Ring B slots (`JASPER_OUTPUTD_SHM_RING_SLOTS`) | `2` | outputd config default |
| Camilla ring-emit chunksize | `128` | `RING_CAMILLA_CHUNKSIZE` (`fanin_coupling.py`), emitted by `emit_flat_ring_config` |
| Camilla ring-emit target_level | `128` | `RING_CAMILLA_TARGET_LEVEL` |
| Camilla ring-emit queuelimit | `1` | `RING_CAMILLA_QUEUELIMIT` |
| Camilla ring-emit `enable_rate_adjust` | `false` | one-clock ring: rate_adjust off (see `jasper/ring_negotiation.py` + HANDOFF-usb-low-latency.md "conservation law") |

The emitter↔ioplug geometry compatibility is pinned by the source-derived model
in [`jasper/ring_negotiation.py`](../jasper/ring_negotiation.py) (three layers:
ioplug constraints from `pcm_jts_ring.c`, ALSA `*_near` clamping, and
CamillaDSP 4.1.3 acceptance predicates) — a chunk/slot mismatch fails CI with a
named reason rather than crash-looping CamillaDSP on deploy.

### Host-clock combo (armed by the auto-pass on eligible gadget boxes — PR #1173)

| Knob | Value | Home |
|---|---|---|
| `JASPER_FANIN_USB_DIRECT` | `enabled` | written to `fanin.env` by `jasper-fanin-coupling-reconcile --auto` when the gadget stack + usbsink intent are present |
| `JASPER_FANIN_HOST_CLOCK` | `enabled` | same |
| `JASPER_FANIN_RESAMPLER_CUSHION_DECAY` | `enabled` | same |
| `JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES` | `576` | `config.rs` `DEFAULT_CUSHION_DECAY_FLOOR_FRAMES` (the hardware-validated floor; clamped into `[544, ceiling]` — 544 = `max(target, minimum_safe_fill) + 32` at the default geometry) |
| `JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES` | `512` | `config.rs` default |
| `JASPER_FANIN_INPUT_RESAMPLER_MAX_ADJUST_PPM` | `500` | `config.rs` default |
| `JASPER_FANIN_OUTPUT_BUFFER_FRAMES` | `1024` | `config.rs` default |

Binary defaults for the three combo flags are **OFF**; the reconciler is the
single writer and arms them only on a box that is both gadget-capable and has USB
input enabled. Boxes without the gadget (jts3 HiFiBerry, jts5 dual-DAC) stay on
loopback coupling — correctly ineligible, a no-op for the auto-pass.

### How the install guarantees it

- `deploy/install.sh` (`resolve_fanin_coupling_default`, ~line 1750): enables
  `jasper-fanin-coupling-auto.service` and runs `jasper-fanin-coupling-reconcile
  --auto --reason install` on **every deploy**.
- `jasper-fanin-coupling-auto.service` re-runs the resolution at **boot**, so a
  fresh flash converges on first boot with no operator step.
- `.env.example` carries prose blocks for `JASPER_FANIN_RING_SLOTS`, the combo
  flags, and the revert levers.

### Reverting (if a box needs the old loopback path)

Set an operator marker so the auto-pass never re-arms:
`JASPER_FANIN_COUPLING_CHOICE=operator` + `JASPER_FANIN_CAMILLA_COUPLING=loopback`
+ `JASPER_OUTPUTD_CONTENT_BRIDGE=direct`, and unset the three combo flags. The
auto-pass respects an operator choice and will not override it across deploys.

---

## 3. How to measure — electrical (`:9891`, no extra hardware)

The route-latency harness compares two timestamp streams, both on the Pi's
`CLOCK_MONOTONIC` (no cross-host clock skew in the subtraction):
- **tap** — fan-in's ingress impulse tap (`TAP_ARM` on the control socket), fires
  when a click arrives at the fan-in capture.
- **reference** — the outputd final-electrical UDP feed on `:9891`, fires when
  outputd queues the corresponding period to ALSA.

The delta is the Pi-internal path (ingress → queued-to-DAC). It excludes the DAC's
own ring+URB+analog latency (§4 measures that).

```sh
# On the Pi: arm the tap, capture the :9891 reference
printf 'TAP_ARM {"threshold":0.2}' | sudo -n nc -U -N -w3 /run/jasper-fanin/control.sock
sudo rm -f /tmp/ref9891.pcap
sudo nohup tcpdump -i lo -w /tmp/ref9891.pcap udp port 9891 >/dev/null 2>&1 &

# On the Mac: play the click WAV (see §5) — 75 s
afplay quick-final-leadin.wav

# On the Pi: harvest + analyze
sudo pkill tcpdump
printf 'TAP_DISARM' | sudo -n nc -U -N -w3 /run/jasper-fanin/control.sock
sudo cp /run/jasper-fanin/impulse-tap.jsonl /tmp/tap-events.jsonl
sudo python3 /tmp/ref9891_pcap_to_detections.py /tmp/ref9891.pcap /tmp/detections.jsonl 0.006
sudo /opt/jasper/.venv/bin/jasper-route-latency-harness analyze \
    --tap-events /tmp/tap-events.jsonl --mic-detections /tmp/detections.jsonl \
    --duration-seconds 75
```

The pcap→detections converter re-anchors pcap realtime to `CLOCK_MONOTONIC` via a
Pi-sampled offset; the `:9891` wire format is headerless interleaved-stereo int16
@ 48 kHz.

---

## 4. How to measure — analog (true end-to-end, Scarlett Solo)

This measures the **complete chain including DAC presentation** — the number the
electrical method structurally cannot reach.

### Bench wiring
- **Focusrite Scarlett Solo** USB interface plugged into the Pi (any Pi USB-A
  port). Appears as ALSA card id **`Gen`** (`arecord -l`).
- **Apple USB-C dongle's 3.5 mm analog output → Scarlett input channel 1** (a
  TRS→whatever-the-Scarlett-takes cable). The dongle already needs an analog load
  to enumerate its USB Audio class; the Scarlett input *is* that load.
- The Mac stays USB-wired to the Pi gadget as always (this is the playback path).

### Non-interference (verify before trusting the run)
The Scarlett also exposes playback endpoints — confirm the output stack ignored
them and the dongle is still the output DAC:
```sh
curl -s http://jts.local:8780/state | jq '.audio_graph.coupling'   # shm_ring, coherent:true
cat /run/jasper-output-hardware/output_hardware.json | jq '{profile,status}'  # apple_usb_c_dongle / ready
```
The output-hardware reconciler correctly leaves the dongle as the profile with a
Scarlett attached; if it ever misclassifies, stop and investigate rather than
measuring.

### The harness's native mic-capture mode
The harness captures the Scarlett directly (no pcap converter):
```sh
sudo /opt/jasper/.venv/bin/jasper-route-latency-harness capture \
    --mic alsa:plughw:CARD=Gen,0 \
    --mic-distance-cm 0 \          # direct wire, no acoustic path — disable distance comp
    --duration-seconds 75 …        # (check --help for the exact tap/threshold flags)
```
Set the detection threshold from a level check: at CamillaDSP's ~−32.8 dB volume,
the harness's −12 dBFS clicks land at ~0.17–0.30 at the Scarlett input vs a
~0.011 noise floor — **threshold 0.15** separated them cleanly. **Never raise
CamillaDSP output volume to improve the level** (hearing/equipment safety); use
the Scarlett's own input gain or lower the detection threshold.

The analyze step is identical to §3 (tap-events + mic-detections → p50/p95/p99).

### Resolving the DAC term
`analog_p50 − electrical_p50` is the true post-`:9891` DAC-side latency. Cross-
check against `outputd.dac.snd_pcm_delay_ms` read live during the run — the delta
between that and the measured DAC term is the beyond-ALSA residual (URB + dongle
analog presentation).

---

## 5. Host / bench setup (the operational realities)

These are the non-obvious steps that make a run succeed; skipping them produces
0-match runs or wrong numbers.

**The click WAV** (`quick-final-leadin.wav`, 48 kHz stereo S16, 75 s): 15 s of a
440 Hz **pilot at amp 0.05**, then 60 s of **40 clicks at amp 0.3** (5 ms 4 kHz
bursts, 1.5 s apart, silence between). The pilot **must sit under the detection
threshold** — at the box's DSP volume a loud pilot arrives at the reference at
the same level as the clicks and floods the detector (a 0.3 pilot gave 56 % match;
0.05 gave 100 %). The pilot's only job is to seat the DLL/lock before the clicks.

**Mac output must be the USB gadget, pinned by UID** (the name "JTS" ambiguously
matches the AirPlay device):
```sh
SwitchAudioSource -a -t output -f json | python3 -c "import json,sys; [print(d['uid']) \
  for line in sys.stdin for d in [json.loads(line)] \
  if d.get('name')=='JTS' and 'AppleUSBAudioEngine' in d.get('uid','')]"
SwitchAudioSource -u "<AppleUSBAudioEngine…uid>" -t output
```
Trust the CoreAudio UID and the Pi's `host_connected` (usbsink `/state`), **not**
`system_profiler` (unreliable for this on macOS).

**Gadget ghost recovery** — a deploy or Scarlett plug bounces the gadget and can
leave a ghost CoreAudio device (present in the list, but Pi `host_connected:
false`). A bare UDC rebind is **not** enough; the Mac only re-enumerates after a
UDC unbind → **5-second dwell** → rebind:
```sh
ssh pi@jts.local 'G=/sys/kernel/config/usb_gadget/jts-usb-audio; U=$(ls /sys/class/udc/|head -1); \
  echo "" | sudo tee $G/UDC; sleep 5; echo $U | sudo tee $G/UDC'
```

**Source selection sanity check** — combo-aware mux builds promote USB in auto
mode from fan-in's 20 Hz frame-flow edge (`direct.streaming:true` +
`NOTIFY usbsink`), with advancing `resampler.input_frames` as the rolling-upgrade
fallback. Before
measuring, confirm `/source/state` shows `active_source: "usbsink"`. On an
older pre-fix build, or if you need to force the measurement lane explicitly:
```sh
curl -s -X POST http://jts.local:8780/source/select -H 'Content-Type: application/json' -d '{"source":"usbsink"}'
# restore afterward:  -d '{"source":null}'   (or the documented auto value)
```

**Descend to the floor first.** Measure at the 576 floor for the numbers above.
On a cold session (no live compliance proof) the lane starts at its acquisition
ceiling (`target + warm-up cushion`: 512+1536 = **2048** on jts.local, which runs
the `1536` cushion override — a fresh install's code-default `2048` cushion gives
a 2560 ceiling; see §6) and descends over **~2.5 min** of continuous playback.
Either play a long continuous-click WAV and poll fan-in STATUS until
`held_target_frames == 576` before capturing, or accept that a short run
measures the ceiling. Confirm the
floor via STATUS: `held_target_frames: 576`, `decay.frozen_reason: at_floor` (or
`prime_hold` when a proof is live).

---

## 6. Known caveats (honest current state)

- **Cold-start latency is the ceiling, not the floor, for the first ~2.5 min —
  root cause was a settle-regime deadlock, fixed 2026-07-05.** The compliance
  proof is *supposed* to persist and seat future sessions at the floor
  immediately. An earlier revision of this doc attributed the ~+600 ppm Mac's
  repeated cold-descent to an open question of whether the settle gate was
  leaking a bug; that gate (`settle_regime_ok` in
  `rust/jasper-host-clock/src/lib.rs`, ~lines 1012-1039) is now lock-only —
  the removed CORRECTION-mode rail guard was the actual deadlock (a
  beyond-authority host rails at +500 under neutral AwaitLock commands and
  can never unrail without the very servo authority the guard withheld),
  fixed and pinned by `correction_probe_settle_accrues_at_the_rail` and
  `beyond_authority_railed_host_probes_pass_then_fail`. The residual seen
  post-fix is a *different*, already-understood mechanism —
  `RevokeReason::EarlyUnlock` in
  `rust/jasper-fanin/src/host_compliance.rs` is a ONE-strike proof delete
  (`classify_strike`, ~lines 341-354), unlike the two-strike `ProbeFail`
  tolerance (`PROBE_FAIL_STRIKE_LIMIT=2`); a stop→restart within
  `confirm_horizon_periods` confirms the revoke as churn (the code calls this
  "Accepted residual"), forcing a fresh ~2.5-min re-descent. See §7 item 1 —
  the open question now is revoke *policy* (is one-strike too strict for an
  ordinary short session on a hard host?), not deadlock diagnosis.
- **`WARMUP_CUSHION_FRAMES`** on jts.local is `1536`; the code default is `2048`.
  This affects only the cold-start descent shape, not the steady-state floor or
  the measured numbers above.
- **Combo auto-selection fixed in current code**: in combo mode `jasper-fanin`
  derives `direct.streaming` from its host-input counter every 50 ms and wakes
  mux on each edge. Mux retains cross-patrol `resampler.input_frames` deltas,
  with lane-level `frames_read` as a fallback for older snapshots.
  If auto mode fails to promote USB, capture `/source/state` plus fan-in
  `STATUS` before forcing `/source/select`; the expected shape is
  `/source/state.usbsink.combo: true` and an advancing
  `inputs[label=usbsink].resampler.input_frames` plus
  `inputs[label=usbsink].direct.streaming: true` on current builds.

---

## 7. Future work — the remaining latency ladder

The ~40 ms electrical goal is **met in steady state**: the 2026-07-11 promotion
cert measured tap→`:9891` p50 `36.35` / p95 `37.93` ms at the churn-safe floor
(§1), so we are at diminishing returns on the *controllable* path. An early
proof-of-concept reached ~35 ms tap→ref at a 256-frame cushion, but that cushion
was **not churn-stable** (unlock storms on a drifting host); the production
576-frame floor is the churn-*safe* minimum, only ~1 ms above that PoC in steady
state — the stability trade's real cost is the ~2.5-min cold-descent ceiling
(~43 ms) a cold session pays *before* reaching the floor, not the floor itself.
So the remaining floor is gated by churn stability, not by tuning. The full-chain
`55.5` ms is dominated by two terms — the host-clock cushion and the DAC-side
buffering — and the honest ranking of what's left, most-actionable first:

1. **Revoke-policy tuning for `EarlyUnlock` — the highest-value item (a policy
   question, not a diagnosis).** §6's compliance-revoke thread: the
   settle-regime deadlock that used to make the ~+600 ppm Mac cold-descend on
   effectively every session was root-fixed 2026-07-05 (`settle_regime_ok` is
   lock-only now; see §6). The remaining residual is `RevokeReason::EarlyUnlock`
   — a ONE-strike proof delete (`classify_strike` in
   `rust/jasper-fanin/src/host_compliance.rs`, ~lines 341-354), unlike the
   two-strike `ProbeFail` tolerance. A stop→restart within
   `confirm_horizon_periods` confirms the revoke as churn ("Accepted residual"
   per the docstring), so a cold session can still run the ~43 ms ceiling for
   ~2.5 min after a short-session restart lands inside that horizon. The open
   question is now **revoke policy**, not deadlock diagnosis: is
   one-strike-on-`EarlyUnlock` too strict for an ordinary short session (a
   notification ding, a preview clip) on a hard/beyond-authority host, or is
   it the correct conservative default given churn cannot otherwise be
   distinguished from a genuinely-new stream restarting inside the horizon?
   Tuning that trade-off (e.g. a bounded tolerance for `EarlyUnlock` mirroring
   `ProbeFail`'s two-strike net) is the single most impactful next step and
   warrants a proper design discussion before any code change.

2. **DAC-side URB/ring depth (~2–3 ms, concrete near-term win).** The measured
   `:9891`→analog term is `13.23` ms: `10.33` ms of ALSA ring occupancy
   (`snd_pcm_delay`, 496 frames) + `2.90` ms of URB-in-flight and dongle
   codec/analog-reconstruction presentation. The ALSA ring depth (the outputd URB
   queue) is tunable; earlier probes estimated ~2–3 ms recoverable by reducing it,
   bounded by the URB-cadence underrun floor. The `2.90` ms analog term is dongle
   hardware — fixed unless the output DAC changes.

3. **Resampler-bypass on proven-compliant hosts (~1–3 ms, depends on #1).** Once a
   host has a persisted compliance proof, the in-path varispeed resampler runs at
   near-unity and exists only as a safety net. A compliance-gated direct path
   (the host-clock DLL steers gadget Capture-Pitch alone, resampler bypassed)
   removes a processing stage and its CPU. It only pays off once #1 makes the
   proof reliably persistent — otherwise the bypass never engages.

4. **Cushion floor (~13–14 ms) — structurally near minimum; do not chase without a
   new clock-recovery approach.** The 576-frame floor is clamped ≥ `544` by the
   DLL-margin / churn-safe math (§2). Going lower risks the underrun churn that
   the 256-frame PoC hit. The only durable way past it is a fundamentally
   different clock-recovery design: an Adriaensen 2nd-order **timestamp-DLL**
   (observe hardware period timestamps instead of the resampler's correction-ppm
   — content-independent, so it cannot be perturbed by silence or transients), or
   the **UAC2 async feedback endpoint** (make the Mac slave to the Pi's clock,
   eliminating in-path resampling entirely). Both are research-tier — bigger lifts
   than #1–#3 — but they could make the lock content-robust (curing the cold-start
   fragility in #1) *and* permit a smaller cushion. The prior-art survey
   (Adriaensen "Using a DLL to filter time", zita-ajbridge, PipeWire's `spa_dll`,
   the `f_uac2` feedback path) is captured in the design notes; this is where those
   directions would land.

**Net:** the ~40 ms electrical goal is already met in steady state (`36.35` ms
certified p50, §1). #1 (`EarlyUnlock` revoke-policy tuning) and #2 (DAC URB
depth) are the realistic near-term targets — together they'd make the box
*reliably* deliver that floor from the *first click* (eliminating the ~2.5-min
cold-descent ceiling) and trim ~2-3 ms off the ~53 ms full-chain, rather than
push the steady floor lower. #3 and #4 are larger and lower-priority; #4 is the
research frontier for ever pushing below the current churn-safe floor.

### Documented leads (not scheduled)

Ideas from a 2026-07-11 architecture review that are solid but not committed
to a build — recorded so they aren't re-derived from scratch, not because
they're queued:

- **`EarlyUnlock` revoke-policy tolerance** (ladder item 1 above). Aligning
  the one-strike `RevokeReason::EarlyUnlock` to the two-strike `ProbeFail`
  ladder, or shortening `confirm_horizon_periods` instead of deleting the
  proof outright, would let every session start at the ~40 ms floor
  immediately instead of paying the ~43 ms cold ceiling for ~2.5 min — the
  largest user-perceivable win that needs no new clock-recovery machinery.
  **Pickup trigger:** a week of real jts.local `revoked_reason_last` / strike
  telemetry showing confirmed `EarlyUnlock` revokes on the known-compliant
  Mac dominating cold-start descents — evidence the one-strike policy is
  punishing a compliant host, not genuine churn. Until then: instrument,
  don't redesign.
- **DAC-side buffer/queue depth trim** (ladder item 2 above). `~2–3` ms
  recoverable by reducing `JASPER_OUTPUTD_DAC_BUFFER_FRAMES` / the outputd
  URB queue depth, bounded by the underrun floor — a measured trim, no new
  design. **Pickup trigger:** after a fresh `jasper-route-latency-artifact`
  run re-pins the current baseline, validated with an overnight soak showing
  zero outputd underruns. Don't trim while the measurement basis is stale —
  a regression would be invisible.
- **Resampler bypass on proven-compliant hosts** (ladder item 3 above).
  `~1–3` ms at steady state once a host's compliance proof persists
  reliably. **Pickup trigger:** after the `EarlyUnlock` item above ships —
  proof persistence is its prerequisite — gated behind the same
  `host_compliance` machinery and fail-safe to the resampled path.
- **Clock recovery below the ~13.8 ms cushion** (ladder item 4 above): a
  timestamp-based DLL or driving the UAC2 feedback endpoint as the primary
  rate actuator, per the prior-art survey already cited there. **Pickup
  trigger:** either a deliberate sub-35 ms product target is set, or the
  first Windows validation session (below) forces feedback-endpoint work
  anyway (`usbaudio2.sys` is feedback-driven) — do both in one design pass
  rather than twice.
- **Retired 2026-07-14: duplicated USB bridge/state surface.** The Rust helper
  crate/binary and its frozen `state.json` are gone. `/state.renderers.usbsink`
  now derives activity/level/mute from the identity-bound fan-in DIRECT lane and
  `host_connected` from kernel UDC sysfs. `jasper-usbsink.service` remains only
  as a process-free readiness/lifecycle marker.

### Rejected paths (do not re-chase)

- **Chasing the 34.71 ms lab recipe** (resampler target 256 + cushion 256,
  host-clock DLL and cushion-decay off). Not a regression to recover — see §1
  "explained, not a regression": this was the pre-productization
  free-running recipe, and directly-observed churn on jts.local's +600 ppm
  Mac (held fill descending to 644 then snapping back to the 2048 ceiling)
  is exactly why PR #1173 shipped target 512 / floor 576. Re-deriving it
  re-imports the instability the productization fixed.
- **Tightening the cushion floor 576 → 544** (0.67 ms). A deliberate,
  documented pad, not an oversight: `DEFAULT_CUSHION_DECAY_FLOOR_FRAMES` in
  `rust/jasper-fanin/src/config.rs` pins 576 as the hardware-validated
  jts.local gate value; 544 is only the theoretical arm-guard minimum
  (`max(target, minimum_safe_fill) + 32`) and was never proven stable on
  hardware. Sub-ms, explicitly out of scope.
- **Replacing `jasper-resampler` with a rate-matcher.** Already tried and
  proven wrong during the USB-drop root-cause investigation — the drop was a
  consumer-overflow bug, not clock drift, so a rate-matcher would not have
  fixed it. Re-chasing it re-litigates a closed diagnosis.
- **Deleting the lane-7 aloop mirror** to "finish" the aloop removal. It
  earns its keep: `RingOutput.mirror` (`rust/jasper-fanin/src/mixer.rs`) is a
  non-blocking diagnostic side-tap on `hw:Loopback,0,7` — `Option<PCM>`,
  never the pacer, the ring runs without it — feeding the AEC fallback
  dsnoop and aloop diagnostics. Removing it saves zero latency and breaks
  the software-AEC fallback reference path.
- **PipeWire / topology re-architecture** of the AEC or reference path.
  Standing constraint (AGENTS.md: swap the engine, not the topology; full
  rationale in `docs/HANDOFF-aec.md`). Nothing in this review produced
  overwhelming evidence against it.
- **Writing Windows-specific code before a hardware session.** Every
  Windows-aware constant in the tree today (the ~163 ppm deadband, the
  ±1000 ppm `MAX_BIAS_PPM` window, the UAC2 gadget's feedback-format
  assumptions) is research-sourced, never measured against this gadget, and
  all settle/churn tuning was validated only against one +600 ppm Mac. Code
  written now would be guess-calibrated twice — once wrong, once again after
  the first real trace. See "Windows host validation (deferred)" below: the
  first Windows session is discovery, code comes after it.

### Windows host validation (deferred)

Readiness: plausibly-compatible-by-construction, validated nowhere. Every
Windows-aware constant in `rust/jasper-host-clock` (the ~163 ppm deadband,
the ±1000 ppm `MAX_BIAS_PPM` window, the `PROBE_PPM` floor) is
research-sourced and tuned only against one +600 ppm Mac. The ordered
discovery checklist for the first Windows session — enumeration → feedback-
endpoint compliance → compliance probe → clock envelope → churn discriminator
→ volume, each gating the next — lives in
[HANDOFF-usb-low-latency.md](HANDOFF-usb-low-latency.md) "Cross-platform
conditions"; that section is the single source of truth, this is a pointer.

---

## Reproduce-the-number checklist

1. Box on main with 2-slot geometry (`jasper-doctor` ring-geometry check green).
2. Scarlett wired (§4); non-interference verified.
3. Mac output pinned to the gadget by UID (§5); gadget `host_connected: true`.
4. Confirm auto selected `usbsink`, or force it on a pre-fix build (§5).
5. Descend to the 576 floor (§5) — continuous playback until `held == 576`.
6. Run the analog capture (§4) for 75 s; expect p50 ≈ 54 ms tap→analog.
7. Cross-check: it should equal electrical (~40.7) + DAC (`snd_pcm_delay` ~10.3 +
   ~2.9 residual). Restore mux to auto; `TAP_DISARM`.

---

Last verified: 2026-07-14 (the duplicated USB bridge/state retirement is marked
complete and rechecked against fan-in STATUS + UDC sysfs ownership. Prior
2026-07-11: §1 gained the certified promotion result
as current truth and the cert budget was tightened to p95<=40ms/p99<=42ms in
`jasper/audio_runtime_plan.py`; §7's aspirational-~40ms framing rewritten to
"met at the electrical plane". Re-verified against `jasper/audio_runtime_plan.py`
and `jasper/audio_validation.py`. Same day, earlier: §1 gained the
"Certification budget" cross-reference (then p95<=48ms/p99<=60ms), re-verified
against those same modules. Prior 2026-07-10: §§6-7 cold-start/revoke-policy
claims rechecked against `settle_regime_ok` in `rust/jasper-host-clock/src/lib.rs`
and `classify_strike`/`RevokeReason::EarlyUnlock` in
`rust/jasper-fanin/src/host_compliance.rs`, including the
`correction_probe_settle_accrues_at_the_rail` and
`beyond_authority_railed_host_probes_pass_then_fail` tests; prior 2026-07-07
verification was jts.local live probes, combo mux liveness patch, 2-slot ring
geometry).
