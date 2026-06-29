# Handoff: USB-in low latency — the USB-only (lean-fifo) path

What it takes to get the **lowest** latency for USB-in, and why the shared
(fan-in) path can't get there. Written after the Phase 1 catch-up drop fix
(2026-06-28), which made USB near-drop-free but revealed the latency cost.

## The latency problem (measured, shared/fan-in path)

With USB routed through the shared mixer (the one-path design), the steady-state
Mac→DAC budget measured on jts.local is **~70–100 ms, and variable** — not the
<60 ms target. Contributors:

| Stage | Latency | Note |
|---|---|---|
| usbsink lane snd-aloop ring | **5–75 ms (sawtooth)** | the catch-up lets a free-running lane fill 1→14 periods before resyncing (`CATCHUP_HIGH_WATER_PERIODS=14`); measured at 43 ms mid-soak |
| usbsink→fan-in snd-aloop hop | ~one ring | first loopback |
| fan-in→CamillaDSP snd-aloop hop | ~one ring | second loopback (current `loopback` coupling) |
| CamillaDSP chunksize | ~5–20 ms | depends on the active chunksize |
| jasper-outputd DAC buffer | **20.7 ms** | `snd_pcm_delay`, buffer/period 512/256 |

Two structural costs dominate: the **catch-up sawtooth** (a drop-control tradeoff —
the high-water of 14 periods is sized to never false-trigger a healthy AirPlay
burst+stall, so it inherently buffers up to ~75 ms on the USB lane) and the **two
snd-aloop hops**. Neither is cheaply removable on the shared path.

## The USB-only answer: the lean-fifo path

When USB is the *sole* active source, route it through the already-built lean lane
instead of the mixer:

```
usbsink (OUTPUT_MODE=fifo) → /run/jasper-usbsink/lean.pipe → CamillaDSP RawFile-capture
   (enable_rate_adjust + AsyncSinc) → jasper-outputd → DAC
```

This **deletes both snd-aloop hops AND the catch-up sawtooth**: CamillaDSP's async
resampler becomes the rate-correcting consumer disciplined by the real DAC clock, so
the pipe sits at a small fixed fill (no sawtooth, no drift overflow). Budget:
CamillaDSP chunksize (~5 ms) + a small fifo + outputd DAC (~15–21 ms). **Measured
~57 ms at 256/1024 (drop-free); <40 ms needs the #27 floor sweep — see Validation
status below.**

Tradeoff: the lean lane **bypasses the fan-in mixer**, so it is SOLO-only — AirPlay/
Spotify/BT/TTS don't mix while it's armed. The mux ladder switches solo↔shared.

## Validation status (2026-06-28, on-device root-armed lab build)

**The production arming path is BLOCKED.** `jasper-mux` runs privilege-separated
(`User=jasper-mux`, `ProtectSystem=strict`, `ReadWritePaths=/var/lib/jasper
/var/lib/jasper-intsecrets`), so its in-process `apply_lean_capture_config()` write to
`/var/lib/camilladsp/configs/` fails with `[Errno 30] EROFS`. The lean lane (4b) was
built when the mux ran as root; the WS1 sandbox came after, and the hardware-free tests
don't exercise it — so 4b-iv passed CI but **cannot arm on a current box**. **Fix:** the
mux must DELEGATE the lean-config apply to the privileged owner (via jasper-control's
restart-broker / a control endpoint), mirroring how it already delegates the usbsink
restart — not write camilla configs in-process. Also harden the usbsink-FIFO-no-reader
crash-loop (usbsink crash-loops writing the pipe until camilla opens it for reading).

**Root-armed lab measurement** (mux bypassed, apply run as root, USB solo, 256/1024):
- **Latency ≈ 57 ms** (camilla target 21.3 + chunk 5.3 + outputd DAC 20.7 + usbsink ~10) —
  under the 60 ms target, ~20–40 ms better than the shared path. NOT yet <40 ms; the
  camilla `target_level` (21 ms) and DAC buffer (21 ms) dominate, both needing the #27
  floor sweep (a smaller stable target + a 384/128 DAC buffer) to reach <40.
- **30-min soak: drop-free** (`usbsink dropped_full` delta = 0, path up the whole time,
  outputd `dac xrun = 0`) — BUT **~155 residual camilla underruns** (~5/min, episodic,
  peaked ~14/min). The clockless lean pipe is tighter than the shared path's snd-aloop, so
  target 1024 is marginal for it; raising the target trades the latency back. Root-cause
  the episodic underruns (USB send-jitter vs rate_adjust hunting vs load) before settling
  the floor.

Bottom line: the lean-fifo is **validated as working, drop-free, and ~57 ms**, but it is
**not production-ready** — it needs the mux-delegation fix, the residual-underrun
resolution, and the <40 ms floor sweep.

## What needs to be done (ordered)

1. **Arm the lean lane through the mux ladder, not raw env.** Wire `decide_lean_route`
   (`jasper/lean_lane.py`) + `JASPER_LEAN_LANE=enabled` so the mux flips USB→lean-fifo
   when USB is solo and back to the shared mixer when another source or TTS starts
   (solo-gated, fail-loud→buffered). The pieces exist (tasks 4b-ii/iii/iv); validate the
   live switch end-to-end and the TTS-while-solo handoff.
2. **Drive the camilla side via the existing lean-config path** (`jasper/usbsink/
   output_mode_reconcile.py` + the lean RawFile capture in `jasper/camilla_config_contract.py`
   — RawFile, not File; the jts5 fix). Confirm `--check` valid and no crash-loop.
3. **Codify the stable buffer floor — it is a `(chunksize, target_level)` PAIR, not one
   number.** Two distinct buffers matter, and the second one bit us:
   - *outputd DAC buffer.* 512/256 = 20.7 ms is not the floor — the Apple dongle measured
     clean to 384/128 (~15 ms) and likely lower (DAC xruns stayed 0 throughout).
   - *CamillaDSP `target_level` (the async-resampler fill).* This has a **stability floor
     that scales with chunksize**: dropping chunksize toward its CPU floor (256) for latency
     forces `target_level` UP to stay stable. Measured on jts.local 2026-06-28 (live
     websocket sweep, USB playing): at chunksize 256, target 512 → **61 Camilla
     underruns/60 s** (a Camilla-playback→outputd stall storm; DAC xruns still 0), target
     **1024 → 0**, 1536/2048 → 0. So at chunk 256 the stable floor is ~4× the chunk (1024),
     not the ~2× that is safe at the committed default (chunk 1024 / target 2048). The
     committed default (`DEFAULT_CHUNKSIZE=1024`, `DEFAULT_TARGET_LEVEL=2048` in
     `jasper/camilla_config_contract.py`) is stable and conservative; jts.local's low-latency
     `chunksize=256` is a per-box `jasper.env` override whose original `target_level=512` sat
     below that floor (the storm) — corrected to 1024.

   This is the **#27 codification**: the safe `(chunksize, target_level)` pair AND the outputd
   DAC buffer belong in the **output `DacProfile`** (`jasper/audio_hardware/dac.py`), keyed by
   the *output DAC* (Apple dongle / HiFiBerry) — **not** by the USB-input host, which is a
   free-running source, not the timing master. Tier-aware (Pi 5 low / Pi Zero safe). A fresh
   box then gets the low-latency-and-stable pair with zero per-user config, instead of the
   current hand-set `jasper.env` override.
4. **Measure end-to-end on jts.local** (Mac→USB, solo): target <60 ms, ideally <40 ms,
   stable; confirm drop-free under sustained play + transitions; soak.
5. **Cross-platform + cross-DAC reliability** (the product bar): repeat the solo lean-fifo
   measurement on Windows + a second DAC. The lean-fifo's correctness rests on CamillaDSP's
   async resampler (host-agnostic, DAC-agnostic — it targets whatever DAC clock), so it
   should hold, but prove it.

## Why not the USB-gadget pitch loop, or a per-input mixer resampler?
Both came up in a 2026-06-28 research pass (verified against kernel source + CamillaDSP
docs). Neither displaces the lean-fifo:

- **USB-gadget feedback / pitch control (slave the host clock to the DAC).** The mechanism
  is real and in mainline (`u_audio.c`/`f_uac2.c`: feedback IN endpoint + a userspace-driven
  `Capture Pitch 1000000` PPM kcontrol; OUT defaults to async-with-feedback via configfs
  `c_sync`). But the part that matters — the *host* acting on the feedback to slave its send
  rate — only reliably holds on **Linux** hosts. **Windows enumerates with the feedback
  endpoint but historically mishandles the value; macOS-via-hub is anecdote.** JTS's hosts
  are Mac/Windows, so the bit-perfect pitch path would not deliver for them. It is also
  topology-blocked here: CamillaDSP's documented "virtual clock tuning for USB Audio Gadget"
  works only when CamillaDSP *captures the gadget directly* — in JTS it captures the summed
  fan-in mix, so that capability does not transfer, and the gadget is not even provisioned
  (`jasper-usbsink-gadget-up` never sets `c_sync` or drives the control). Treat a gadget-pitch
  loop as a far-future, on-device host-matrix-gated experiment, not a near-term path.
- **Per-input adaptive resampler inside the mixer.** A more capable version of the
  usbsink-edge matcher we deliberately CUT (`bcce0d1e`, "wrong tool"). Its only unique win
  over the lean-fifo is making USB low-latency *while simultaneously mixing another source*
  (plus deleting the solo↔shared mux swap). Because the mux preempts to the latest source,
  concurrent USB-mixing is rare — so this is elegance, not necessity, and is deferred until
  the lean-fifo is measured. If ever built it lives in fan-in's existing in-band RT thread
  (never a unit-level `SCHED_FIFO` on usbsink — that crash-looped the AEC bridge), uses
  **Rubato** (zita-resampler is SSE2-only, no NEON on ARM64), and a DLL with
  `b = √2·ω/2` (= ω/√2), not √2·ω.

## Why not just lower the catch-up high-water?
Lowering `CATCHUP_HIGH_WATER_PERIODS` would shrink the shared-path sawtooth but
re-introduce false-triggers on healthy AirPlay burst+stall transients (~12.4-period
peak) — trading latency for drops on every source. The lean-fifo gets low latency
*without* that tradeoff because it removes the sawtooth mechanism entirely.

Last verified: 2026-06-28
