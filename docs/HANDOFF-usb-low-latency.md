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
| jasper-outputd DAC buffer | **~64 ms shipped default** | `snd_pcm_delay`, buffer/period 3072/1024 (the conservative global default); the Apple-dongle codified floor is 512/256 ≈ 20.7 ms |

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
the pipe sits at a small fixed fill (no sawtooth, no drift overflow). Estimated
budget: CamillaDSP chunksize (~5 ms) + a small fifo + outputd DAC (~15–21 ms) ≈
**<40 ms achievable**, stable.

Tradeoff: the lean lane **bypasses the fan-in mixer**, so it is SOLO-only — AirPlay/
Spotify/BT/TTS don't mix while it's armed. The mux ladder switches solo↔shared.

## What needs to be done (ordered)

1. **Arm the lean lane through the mux ladder, not raw env.** Wire `decide_lean_route`
   (`jasper/lean_lane.py`) + `JASPER_LEAN_LANE=enabled` so the mux flips USB→lean-fifo
   when USB is solo and back to the shared mixer when another source or TTS starts
   (solo-gated, fail-loud→buffered). The pieces exist (tasks 4b-ii/iii/iv); validate the
   live switch end-to-end and the TTS-while-solo handoff.
2. **Drive the camilla side via the existing lean-config path** (`jasper/usbsink/
   output_mode_reconcile.py` + the lean RawFile capture in `jasper/camilla_config_contract.py`
   — RawFile, not File; the jts5 fix). Confirm `--check` valid and no crash-loop.
3. **Tune the buffer floors to the DAC's real floor.** DONE (the #27 codification, landed
   2026-06-28). The DAC's stable buffer floor is now DATA on its `DacProfile`
   (`jasper/audio_hardware/dac.py`: the `LatencyFloor` dataclass + the optional
   `latency_floor` field), so a new DAC is declaration-only and zero per-user config.
   The shipped *global* default stays conservative — CamillaDSP chunk 1024 / target 2048,
   outputd period 1024 / dac_buffer 3072 (~64 ms) — and any DAC with no declared floor
   keeps it (non-breaking). The **Apple-dongle profile** declares the measured floor
   CamillaDSP chunk 256 / target 1024, outputd period 256 / dac_buffer 512 (≈ 20.7 ms),
   the value the jts.local `jasper.env` override previously produced by hand. The floor is
   a CamillaDSP (chunksize, target_level) PAIR — target must be ≥ 4x chunk so the resampler
   has fill headroom (chunk 256 → target 1024), enforced in `LatencyFloor.__post_init__`.
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
     `JASPER_CAMILLA_CHUNKSIZE`/`_TARGET_LEVEL` (explicit operator env) > active
     profile floor > global default; a state file that is absent or unreadable simply
     keeps the global default (a fresh box before the reconciler's first write is
     non-breaking, never an unloadable config).
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
4. **Measure end-to-end on jts.local** (Mac→USB, solo): target <60 ms, ideally <40 ms,
   stable; confirm drop-free under sustained play + transitions; soak.
5. **Cross-platform + cross-DAC reliability** (the product bar): repeat the solo lean-fifo
   measurement on Windows + a second DAC. The lean-fifo's correctness rests on CamillaDSP's
   async resampler (host-agnostic, DAC-agnostic — it targets whatever DAC clock), so it
   should hold, but prove it.

## Why not just lower the catch-up high-water?
Lowering `CATCHUP_HIGH_WATER_PERIODS` would shrink the shared-path sawtooth but
re-introduce false-triggers on healthy AirPlay burst+stall transients (~12.4-period
peak) — trading latency for drops on every source. The lean-fifo gets low latency
*without* that tradeoff because it removes the sawtooth mechanism entirely.

Last verified: 2026-06-29 (#27 latency-floor codification: emitters wired to the
active profile floor end-to-end; operator-override precedence corrected)
