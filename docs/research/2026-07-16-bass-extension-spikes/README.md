# Wave 0 decision memo — bass-extension hardware spikes (2026-07-16)

> Session artifact. Spikes 1–3 executed 2026-07-16 on jts.local
> (production CamillaDSP v4.1.3, config `sound_current.yml`) by
> orchestrated agents; raw structured results in the session
> transcript. Spike 4 and the ears-on transition listen remain with
> the operator. Consumed by
> [`docs/HANDOFF-bass-extension-plan.md`](../../HANDOFF-bass-extension-plan.md)
> §12/§14 and the wave-3/5 prompt gates.

## Verdicts

| Question | Verdict |
|---|---|
| Spike 1 — transition mechanism | **R1 confirmed** (`r1_clean`) — live `PatchConfig` with mandatory micro-stepping; single hard swaps are borderline and are NOT the product mechanism |
| Spike 2 — PatchConfig persistence | Survives volume writes; **reset by Reload and by SetConfigFilePath+Reload** — the Wave 5 reconciler must re-apply after every reload |
| Spike 3 — harmonic extraction | Math validated (THD recovery within 0.59 dB synthetic); two productization requirements found → Wave 1 contract rev 3 |
| Spike 4 — nearfield mic ceiling | **Deferred to operator** (needs a phone at the cone) |

## Spike 1 — transition-click bench (isolated instance + live pass)

Method: isolated CamillaDSP v4.1.3 subprocess on the Pi (Stdin
capture fed chunk-aligned, File playback, private websocket), one
named `LinkwitzTransform` biquad on both channels, transitions
61 Hz/0.72 ↔ 52 Hz/0.65 driven over the websocket while a −20 dBFS
signal ran. Metrics per transition: max sample-to-sample delta vs.
local steady state, and the peak of a 200 Hz-highpassed residual
(dBFS). Sine runs duplicated as independent process launches
(deterministic to 2 decimals); a 12-transition ABAB timeline embedded
2 reps per type per run.

Results at −20 dBFS signal (45 Hz sine; worst instance per type):

| Transition | Burst (dBFS) | Δ rel steady (dB) |
|---|---|---|
| Single hard PatchConfig (fwd) | −61.0 (range −61.0…−74.9) | 1.18 |
| Single hard PatchConfig (back) | −61.8 (range −61.8…−65) | 0.24 |
| 6-step interpolation, worst sub-step (fwd) | −76.6 (range −76.6…−87.7) | 0.41 |
| 6-step interpolation, worst sub-step (back) | −75.3 (range −75.3…−93.7) | −0.89 |
| SetConfigFilePath+Reload | ≈ identical to hard patch | — |
| Pink noise, all types | buried in the noise's own −11…−14 dBFS residual floor | ≤ +0.2 |

Reading: **micro-stepped transitions carry ≥15 dB of margin** below
the −60 dBFS bench threshold and are the product mechanism (already
what Wave 5 specifies: ≤1 dB response change per step). Hard swaps
pass with only ~1 dB margin — acceptable for the silent-branch case,
not for live audio. Reload with an unchanged pipeline topology is
numerically indistinguishable from a hard patch (delay-line state is
preserved when only filter parameters differ) — a useful continuity
fact for `apply_dsp_config`-class swaps.

Bench-methodology note kept for posterity: an initial run showed a
fabricated −38.9 dBFS "burst floor" traced to the feed thread's
chunk pacing (misaligned with CamillaDSP's chunksize, causing
buffer-underrun glitches). Chunk-aligned feeding eliminated it;
discarded numbers are not CamillaDSP's fault. Live confirmation on
the production instance (identity LT appended via validated
SetActiveConfig, one stepped transition during a −35 dB main-volume
45 Hz tone, then exact restore) completed with `clipped_samples=0`
and full state restoration verified; the `:9891` electrical tap was
unavailable (exclusively bound by jasper-aec-bridge), so
electrical-domain live confirmation and the ears-on listen remain
open operator items.

## Spike 2 — PatchConfig semantics

Patched `freq_target` 61→55 on the isolated instance, then:
SetVolume(−25) → patch **survives**; Reload (same
config_file_path) → patch **lost**; SetConfigFilePath (same path) +
Reload → patch **lost**. Repeatable. Consequence (already in the
Wave 5 spec, now evidence-backed): every config reload silently
resets the bass-extension filters to their emitted (natural) values —
fail-safe in direction — and the 1 Hz reconciler must re-apply the
scheduled target afterward.

## Spike 3 — harmonic extraction (Novak sync-ESS)

Synthetic (10–500 Hz, 8 s, injected x² / x³ nonlinearities):
harmonic-image offsets verified; recovered H2/H3 ratios within
**0.18 / 0.59 dB** of an independent analytic ground truth across
30–150 Hz. Real-data pass ran on an archived iPhone room-correction
capture from jts.local: clean linear IR, mechanically sound pipeline;
acoustic ground-truthing (REW cross-check) deferred as planned.

Three productization findings (folded into wave-1 rev 3):

1. **Window symmetry is mandatory.** Windowing harmonic images with
   Hann but the fundamental with the rectangular arrival window
   biased ratios by ~5.8 dB. The fundamental must be extracted with
   the identical window shape.
2. **The Δt = L·ln(n) offset is asymptotic.** Exact within 1 sample
   on the production full-band sweep (20–20 k, 10 s), but off by 36
   samples (~0.75 ms) on a narrow low-f1 bass sweep — the extractor
   must refine its window center by local peak search around the
   predicted offset (the ladder's sweeps ARE narrow bass sweeps).
3. **Band-edge SNR gating is a design requirement, not a detail.**
   Near the sweep's f1, the real capture read physically-impossible
   "H2" up to +8.7 dB re fundamental (fundamental-SNR collapse).
   `thd_curve` must mask grid points whose fundamental lacks SNR
   headroom; ladder THD verdicts consider unmasked points only.

## Open operator items

1. Spike 4: phone-mic clipping ceiling at the loudest ladder rung →
   mic-distance guidance for the wizard.
2. Ears-on listen of stepped transitions on the live path (bench says
   inaudible; a human should confirm once).
3. Optional: REW cross-check of the harmonic pipeline on one acoustic
   capture.

Last verified: 2026-07-16
