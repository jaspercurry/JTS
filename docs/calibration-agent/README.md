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

Last verified: 2026-05-26
