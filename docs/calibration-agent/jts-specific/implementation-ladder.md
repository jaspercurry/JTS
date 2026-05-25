# JTS Calibration Implementation Ladder

> **Status: distilled from 2026-05-25 deep-research intake.**
> This is the staged product/architecture path, not a task list for
> one PR.

## Operational Summary

JTS should grow from conservative bass correction into a richer
calibration platform by adding substrate first: bundle
reproducibility, calibrated microphones, multi-position data,
visualizations, and separate DSP layers. FIR and LLM-assisted tuning
become safer only after that substrate exists.

## Ladder

| Stage | Capability | Ship criteria |
|---|---|---|
| 0 | Current bass PEQ | Cuts-first, bounded filters, CamillaDSP YAML, simple verify. |
| 1 | Measurement substrate | Raw captures, impulse responses, target data, mic metadata, audit log. |
| 2 | Calibrated mic + device picker | Known mic fetch/upload, selected browser input, bundle provenance. |
| 3 | Multi-position confidence | Per-position captures already exist; add seat variance flags, complex-response retention where needed, and clearer repeatability checks. |
| 4 | Target curve layer | B&K/Harman-style presets, user-adjustable bass and tilt, stored as data. |
| 5 | Preference EQ layer | Separate reversible profile bank after room correction. |
| 6 | FIR runtime substrate | CamillaDSP convolution import/export, latency/headroom reporting. |
| 7 | Minimum-phase FIR | Same conservative target and boost rules as PEQ, emitted as FIR. |
| 8 | FDW / mixed-phase experiments | Opt-in, high measurement confidence, pre-ringing audit, power-user first. |
| 9 | LLM advisor | Explains, asks, compares, and submits bounded strategy JSON only. |

## DSP Pipeline Boundary

Active speaker DSP, room correction, target shaping, and preference
tuning should be separate filter banks:

```yaml
pipeline:
  - mixer: stereo_in
  - filter: crossover_and_driver_alignment_<speaker_profile_id>
  - filter: room_correction_<session_id>
  - filter: target_curve_<target_id>
  - filter: preference_eq_<profile_id>
  - filter: limiter_safety
  - mixer: stereo_out
```

The active speaker DSP layer is the speaker baseline: crossover
filters, per-driver gain, polarity, delay, limiter policy, and driver
integration. It should be tuned before room correction. Room
correction is rewritten by measurement flows. Preference EQ is
rewritten by user/LLM-guided taste flows. The limiter/headroom guard
is always-on.

## Parallel Track: Active Speaker Commissioning

JTS speakers with separate woofer/tweeter amplifier channels need a
separate commissioning path before the room-correction ladder:

| Stage | Capability | Ship criteria |
|---|---|---|
| A0 | Static crossover profile | Known-safe CamillaDSP crossover, polarity, gains, and limiters from bench design. |
| A1 | Per-driver measurement mode | Measure woofer-only and tweeter-only responses safely, with guardrails around level and mute state. |
| A2 | Driver alignment | Delay/polarity/phase inspection around crossover, plus predicted summed response. |
| A3 | Crossover tuning | Deterministic candidate filters, lobe/null checks, excursion/headroom constraints. |
| A4 | Speaker baseline bundle | Store the accepted speaker profile separately from room measurements. |

This track overlaps with FIR/phase work, but it is not the same as
room phase correction. Driver alignment is usually more repeatable
and more correctable than trying to invert seat-dependent room
reflections.

## Deterministic Responsibilities

Code owns:

- sweep playback and capture validation;
- deconvolution and impulse-response derivation;
- averaging, smoothing, windowing, and FDW settings;
- minimum/excess-phase analysis when implemented;
- target application and filter optimization;
- boost caps, null detection, pre-ringing checks, and clipping checks;
- CamillaDSP YAML / WebSocket payload validation;
- bundle write/read and replay.

## LLM Responsibilities

The LLM may:

- explain plots and uncertainty;
- ask clarifying listening questions;
- map user language to a bounded preference intent;
- recommend which deterministic strategy to run;
- generate audit-log narration and user-facing summaries.

The LLM must not:

- emit FIR taps;
- write unconstrained CamillaDSP YAML;
- override safety bounds;
- call subjective preference "accuracy";
- merge preference EQ into the room-correction layer silently.

## Immediate Engineering Implications

- Bundle schema should keep raw capture artifacts even if current code
  does not use them yet.
- Known speaker/hardware profiles are a long-term advantage; JTS can
  eventually separate speaker baseline from room effects better than
  generic software.
- Active crossover commissioning should generate a stable speaker
  baseline profile before room correction. Do not let a room-correction
  flow rewrite crossover filters implicitly.
- UMIK/Dayton calibration support is not polish. It is prerequisite
  substrate for higher-confidence full-range analysis.
- UI plots should grow toward: measured/target/predicted response,
  per-position overlays, impulse, group delay, waterfall/decay, and
  correction filter response.

## Sources

- 2026-05-25 Google / Anthropic / OpenAI deep-research reports.
- CamillaDSP documentation.
- REW and HouseCurve workflows.
- DRC-FIR and rePhase prior art.
- Toole / Olive / Welti room-correction and preference research.

Last verified: 2026-05-25
