# Handoff: USB-input latency — measurement method + productized defaults

This doc is the **measurement reference** for the USB-input audio path: how we
measure end-to-end latency, the numbers we get, the exact settings that produce
them, and the host/bench setup to reproduce it. For the *design* narrative
(why the ring graph, the host-clock DLL, the compliance ladder), read
[HANDOFF-usb-low-latency.md](HANDOFF-usb-low-latency.md) — this doc links to it
rather than restating it.

`Last verified: 2026-07-07` (jts.local live probes, combo mux liveness patch,
2-slot ring geometry).

---

## 1. Results (current, hardware-measured)

The USB-input chain is `Mac app → hw:UAC2Gadget → fan-in direct capture →
[host-clock DLL + varispeed resampler + cushion] → Ring A → CamillaDSP → Ring B
→ jasper-outputd → Apple USB-C dongle → analog out`.

Two independent measurements, taken through different capture points, at the
**576-frame cushion floor** (the steady low-latency state):

| Measurement | Reference point | p50 | p95 | p99 | match | n |
|---|---|---|---|---|---|---|
| **Electrical** (`:9891`) | outputd queues period to ALSA | **40.73** | 42.12 | 43.17 | 100% | 40 |
| **Analog** (Scarlett) | dongle 3.5 mm output, post-DAC | **53.96** | 55.67 | 57.94 | 96.2% | 50 |

**Internal-consistency check:** electrical `40.73` + measured DAC term `13.23`
= `53.96` — composing to the directly-measured analog p50 **exactly**. Two
independent measurements a day apart validate each other.

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
mode when fan-in's direct-lane `resampler.input_frames` advances. Before
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

- **Cold-start latency is the ceiling, not the floor, for the first ~2.5 min.**
  The compliance proof is *supposed* to persist and seat future sessions at the
  floor immediately. On the jts.local Mac (a ~+600 ppm beyond-authority host) the
  proof was observed to be written then `revoked reason=early_unlock` at stream
  end, so the next session cold-descends again. Whether that early-unlock is a
  terminal-stream-end unlock leaking past the #1156 discriminator (a bug) or
  genuine floor churn on a hard host (correct backoff) is an **open
  investigation** — it is the gap between "the default gives 40 ms" and "the
  default *reliably* gives 40 ms."
- **`WARMUP_CUSHION_FRAMES`** on jts.local is `1536`; the code default is `2048`.
  This affects only the cold-start descent shape, not the steady-state floor or
  the measured numbers above.
- **Combo auto-selection fixed in current code**: in combo mode `jasper-mux`
  treats USB as live when fan-in's direct-lane `resampler.input_frames` advances
  across ticks, with lane-level `frames_read` as a fallback for older snapshots.
  If auto mode fails to promote USB, capture `/source/state` plus fan-in
  `STATUS` before forcing `/source/select`; the expected shape is
  `/source/state.usbsink.combo: true` and an advancing
  `inputs[label=usbsink].resampler.input_frames`.

---

## 7. Future work — the remaining latency ladder

We are at diminishing returns on the *controllable* path: tap→`:9891` p50
`40.73` ms. An early proof-of-concept reached ~35 ms tap→ref, but only at a
256-frame cushion that was **not churn-stable** (unlock storms on a drifting
host); the production 576-frame floor is the churn-*safe* minimum, ~7 ms higher
by a deliberate stability trade. So the remaining floor is gated by churn
stability, not by tuning. The full-chain `55.5` ms is dominated by two terms —
the host-clock cushion and the DAC-side buffering — and the honest ranking of
what's left, most-actionable first:

1. **Cold-start reliability — the highest-value item (a reliability fix, not a
   floor reduction).** §6's compliance-revoke thread: on the ~+600 ppm Mac the
   persisted proof is `revoked reason=early_unlock` at stream end, so a cold
   session runs the ~43 ms ceiling for ~2.5 min before descending to the 40 ms
   floor. Fixing this converts the measured floor from an aspirational
   steady-state number into the *actual* first-click experience. It is a
   control-loop question — is the stream-end unlock leaking past the #1156
   terminal-unlock discriminator (a bug), or genuine floor churn on a hard host
   (correct backoff)? Diagnosing that is the single most impactful next step and
   warrants a proper investigation before any code change. This is the same churn
   problem that keeps the cushion floor where it is (see #4) — solving it well
   could unlock both.

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

**Net:** #1 (cold-start reliability) and #2 (DAC URB depth) are the realistic
near-term targets — together they'd make the box *reliably* deliver ~40 ms
tap→ref / ~53 ms full-chain from the first click, instead of only in steady
state. #3 and #4 are larger and lower-priority; #4 is the research frontier for
ever pushing below the current churn-safe floor.

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

Last verified: 2026-07-07 (jts.local live probes, combo mux liveness patch,
2-slot ring geometry).
