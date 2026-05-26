# JTS Calibration Implementation Ladder

> **Status: distilled from 2026-05-25 deep-research intake and
> updated 2026-05-26 for active-speaker proposal v3.**
> This is the staged product/architecture path, not a task list for
> one PR.

## Operational Summary

JTS should grow from conservative bass correction into a richer
calibration platform by adding substrate first: bundle
reproducibility, calibrated microphones, multi-position confidence,
visualizations, and separate DSP layers. The main product lane is room
correction and preference tuning for ordinary listeners; active
speaker commissioning is an adjacent power-user lane. FIR and
LLM-assisted tuning become safer only after the measurement substrate
is trustworthy enough to explain.

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

## Current Main-Lane Priority

After the calibrated mic and strategy-audit substrate, the next room
correction work should be measurement trust rather than more filter
types. The immediate priority is Stage 3: first-class confidence
reporting, per-position variance, repeatability flags, and bundle
artifacts that let deterministic code and future LLM tools explain
what the measurement can and cannot support.

## DSP Pipeline Boundary

Active speaker DSP, room correction, target shaping, and preference
tuning should be separate filter banks. In active-speaker mode, do
not model the speaker baseline as a single stereo filter before room
correction; part of it is pre-split and part of it is per-driver:

```text
stereo_input
  -> room_correction_<session_id>        # Layer B, bypassable
  -> target_curve_<target_id>            # Layer C, bypassable
  -> preference_eq_<profile_id>          # Layer C, bypassable
  -> baseline_presplit_<speaker_profile> # Layer A ownership, e.g. BSC
  -> split_2way_or_3way_<channel_map>
  -> per_driver_baseline_<speaker_profile>
       crossover(s)
       driver_eq
       delay
       gain_trim
       limiter
  -> physical_outputs
```

Layer A is tuned and versioned before Layer B or Layer C. Layer B is
rewritten by room measurement flows. Layer C is rewritten by
user/LLM-guided taste flows. The per-driver limiter/headroom guard is
always-on and must not be bypassed by any renderer, cue path, sweep,
or test tone.

## Parallel Track: Active Speaker Commissioning

JTS speakers with separate woofer/mid/tweeter amplifier channels need
a separate commissioning path before the room-correction ladder. This
track matters for JTS hardware and DIY users, but it should not displace
the room-correction main lane:

| Stage | Capability | Ship criteria |
|---|---|---|
| A0 | Preset/schema substrate | Driver-set preset schema, active channel map, baseline profile schema, and validation, with no hardware loading. |
| A1 | Safe CamillaDSP templates | Generate checked 2-way/3-way templates with muted/protected startup state, explicit channel labels, and rollback. |
| A2 | Engineering preset interop | Import or reference REW/VituixCAD artifacts, freeze expected envelopes, safe sweep ranges, polarity, delay ranges, trims, limiters, and BSC. |
| A3 | Channel/path safety | Prove every audible path flows through the protected active baseline; verify physical outputs before drivers are connected. |
| A4 | Phone-as-mic W0 | Raw PCM WebSocket ingest, calibration blocking, browser processing sanity checks, and resumable server-side session state. |
| A5 | Per-driver near-field | Measure woofer/mid/tweeter in isolation against preset envelopes with protective filters and level gates. |
| A6 | Null-depth alignment | Walk delay and polarity per crossover region; require strong inverted-polarity null before accepting the baseline. |
| A7 | Gated summed verification | Validate the direct acoustic sum through crossover regions; mark 250-500 Hz lower crossovers reduced-confidence unless the gate supports them. |
| A8 | Speaker baseline lock | Store accepted `speaker_baseline` separately from room correction and preference profiles, with bundle provenance. |

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

Last verified: 2026-05-26
