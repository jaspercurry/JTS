# Calibration Agent Corpus

> **Status: initial corpus, first distilled 2026-05-25.** This
> directory is the public, repo-owned knowledge corpus for future
> guided speaker tuning. It is intentionally separate from
> user-private runtime context such as room dimensions, household
> taste notes, and previous listening feedback.

## Purpose

The future calibration/tuning agent should be able to explain what it
knows, cite source-backed guidance, and distinguish physical room
correction from subjective preference tuning. This corpus is the
material it should read before advising a user.

The corpus is not a scratchpad for private household memory. Runtime
install-specific context belongs under `/var/lib/jasper/...`; the
schema for that context lives in
[`jts-specific/runtime-context-schema.md`](jts-specific/runtime-context-schema.md).

Raw 2026-05-25 research inputs are archived separately at
[`../research/2026-05-25-calibration-agent/README.md`](../research/2026-05-25-calibration-agent/README.md).
Use those reports for traceability and re-review, but move only
verified/distilled claims into this corpus.

Follow-up 2026-05-27 research inputs and syntheses are archived at
[`../research/2026-05-27-room-correction-research/README.md`](../research/2026-05-27-room-correction-research/README.md).
They cover mobile browser capture reliability, target/preference
tuning, FIR/phase correction, and multi-position confidence.

## Source Quality

Use this ranking when adding claims:

1. **Primary / technical references:** AES papers, books by Toole /
   Olive / Welti, CamillaDSP docs, REW docs, peer-reviewed DSP work.
2. **Strong secondary sources:** vendor manuals that describe actual
   behavior and file formats, well-maintained open-source tool docs,
   measurement-focused guides with clear methodology.
3. **Community / forum evidence:** useful for workflow and endpoint
   archaeology, but mark as such and do not treat as physics.
4. **Marketing claims:** cite only as product-positioning evidence,
   not as proof that a technique works.

Every concept file should have:

- short operational summary
- what JTS can do today
- what JTS cannot do yet
- relevant sources
- uncertainty / open debates

## Current Outline

- [`concepts/measurement-quality.md`](concepts/measurement-quality.md)
  — calibrated mics, clipping, SNR, repeatability.
- [`concepts/room-correction-limits.md`](concepts/room-correction-limits.md)
  — what room EQ can and cannot physically fix.
- [`concepts/spatial-averaging.md`](concepts/spatial-averaging.md)
  — single-point, multi-position, RMS, vector, and moving-mic
  measurement trade-offs.
- [`concepts/active-speaker-dsp.md`](concepts/active-speaker-dsp.md)
  — 2-way/3-way active crossover, driver alignment, speaker-baseline
  tuning, and the near-field/null-depth/gated measurement triad.
  Current operational truth and implementation planning live in
  [`../HANDOFF-active-speaker-dsp.md`](../HANDOFF-active-speaker-dsp.md).
- [`filter-design/fir-room-correction.md`](filter-design/fir-room-correction.md)
  — FIR fundamentals and implementation constraints.
- [`filter-design/preference-eq.md`](filter-design/preference-eq.md)
  — subjective language mapped to safe reversible tuning moves.
- [`targets/house-curves.md`](targets/house-curves.md)
  — target curves and taste.
- [`references/prior-art.md`](references/prior-art.md)
  — public/open/commercial tools and workflows JTS should learn from.
- [`jts-specific/implementation-ladder.md`](jts-specific/implementation-ladder.md)
  — staged path from current PEQ to guarded FIR and LLM-guided
  preference tuning.
- [`jts-specific/runtime-context-schema.md`](jts-specific/runtime-context-schema.md)
  — public schema for private per-install context.
- [`research-intake-template.md`](research-intake-template.md)
  — shape for external deep-research reports before distillation.

## Research Intake Log

- 2026-05-25: distilled three user-provided deep-research reports
  from Google, Anthropic, and OpenAI into the concept files above.
  Consensus: keep the current conservative bass PEQ path; improve
  bundle reproducibility; add calibrated mic and multi-position flows;
  introduce FIR first as infrastructure, then as guarded
  minimum-phase / low-band mixed-phase correction; keep LLM behavior
  advisory and parameter-bounded. Raw archive:
  [`docs/research/2026-05-25-calibration-agent/`](../research/2026-05-25-calibration-agent/README.md).
- 2026-05-25: distilled three user-provided active speaker DSP /
  crossover commissioning reports into
  [`HANDOFF-active-speaker-dsp.md`](../HANDOFF-active-speaker-dsp.md)
  and the active-speaker concept note. Consensus: treat active
  crossover tuning as a speaker-baseline commissioning module with
  separate safety gates, not as a room-correction extension. Same raw
  archive as above.
- 2026-05-26: folded proposal-v3 active speaker commissioning into
  the same handoff and concept note. Update: preset-first generic
  2-way/3-way workflow, strict Layer A/B/C separation, phone-as-mic
  raw PCM capture, calibration as a blocking step, and lower-crossover
  confidence caveats for 3-way speakers.
- 2026-05-26: added room-correction strategy/audit substrate. Bundles
  now carry target-profile metadata, correction strategy metadata, and
  a deterministic design report with per-filter rationale. This is the
  first assistant-facing bridge from measurement math to explainable,
  bounded recommendations.
- 2026-05-27: archived and synthesized nine additional user-provided
  research reports covering browser audio reliability, target curves
  and preference tuning, FIR/phase correction, and multi-position
  confidence. Consensus: keep PEQ conservative by default; turn
  measurement confidence into a first-class per-band/per-filter
  artifact; treat sound curves as editable preference presets; build
  FIR as a staged ladder gated by bundle provenance, timing, spatial
  stability, latency, headroom, and pre-ringing risk. Raw archive and
  synthesis:
  [`docs/research/2026-05-27-room-correction-research/`](../research/2026-05-27-room-correction-research/README.md).
- 2026-05-28: durable-evidence bundle substrate expanded. Correction
  bundles now use schema v3 and write `artifact_manifest.json` with
  checksums, schema/kind metadata, generator provenance, dependencies,
  sensitivity, and recomputability for raw captures and derived
  artifacts. Bundles also write `runtime_integrity.json` with
  lightweight system/runtime snapshots, capture sample-count sanity,
  fan-in xrun deltas, and CamillaDSP runtime counters.
- 2026-05-28: agent-readiness evidence packet added.
  `acoustic_quality.json` records the current SNR/acoustic-trust
  verdict from capture quality, native pre-sweep noise WAVs, banded
  SNR estimates, direct-arrival evidence, and optional main-seat repeat
  capture; `jasper.correction.evidence` combines bundle, confidence,
  runtime, acoustic, and repeatability facts into a deterministic
  read-only packet used by `jasper-calibration-agent`.
- 2026-05-28: replay-grade and FIR Stage 0 substrate added. Successful
  captures now write manifest-tracked `analysis/` artifacts with
  derived impulse responses, raw/smoothed/final response curves,
  calibration/normalization metadata, direct-arrival evidence, and
  deconvolution settings. The evidence packet is schema v2 with
  explicit capability permissions and missing-evidence reporting, and
  `jasper-correction-bundle` can inspect/stage imported FIR coefficient
  WAVs without applying them. Remaining evidence work: real
  phone/browser/mic smoke tests and research-tuned thresholds for
  future FIR and agent analysis.
- 2026-05-29: LLM-ready advisor context packet added.
  `jasper.calibration_agent.advisor_context` derives a narrower,
  redacted v1 context from the deterministic evidence packet for
  future model calls. It includes bundle validity, sanitized mic /
  browser-device provenance, acoustic/runtime/repeatability/spatial
  confidence, target and strategy summaries, current sound-profile DSP
  shape, corpus snippets, missing evidence, and explicit advisor
  permissions. It excludes raw audio, absolute paths, secrets,
  raw mic serials, untrusted browser labels, user-entered profile
  names, unconstrained CamillaDSP YAML, FIR taps, direct apply/reset
  authority, and volume authority.
- 2026-05-29: advisor prompt + bounded action contract added.
  `jasper.calibration_agent.prompt` emits a provider-neutral prompt
  package with system instructions, the redacted context, and the
  response contract; `jasper.calibration_agent.response` validates
  future `jts_advisor_response` JSON into a safe action plan. The
  first action surface is intentionally narrow: explain, recommend
  remeasurement, propose an ephemeral preference-EQ audition, or
  request a user-confirmed preference-profile save. Validated profile
  payloads are DSP-shape-only; JTS owns profile identity and
  timestamps. Models still cannot directly apply filters, control
  volume, emit CamillaDSP YAML, generate FIR taps, or bypass JTS safety
  gates.
- 2026-05-29: human-in-the-loop action runner added.
  `jasper.calibration_agent.actions` consumes validated advisor action
  plans and runs only known, execution-ready actions. Explain and
  remeasurement actions are presentation-only. Preference auditions and
  user-approved profile commits require caller-owned executors supplied
  by a future web/voice surface; the CLI action runner wires none, so
  those actions remain pending and side-effect-free. Preference tuning
  remains subjective: JTS proposes safe options and the listener decides
  what sounds better.

Last verified: 2026-05-29
