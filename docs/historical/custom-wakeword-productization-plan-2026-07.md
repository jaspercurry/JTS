# Custom wake-word training — productization plan (2026-07) — historical

> **Status: historical.** The four-phase productization plan and proposed
> module boundaries written while the Phase 0 tooling was being built, plus the
> per-profile training topology that ADR-0129 later superseded. Kept because
> the phase goals still read as the sequence a future product flow would
> follow, and because the superseded topology explains why some tooling is
> named per *profile* rather than per *leg*. **Current operational truth is
> [HANDOFF-custom-wakeword-training.md](../HANDOFF-custom-wakeword-training.md);**
> decisions are ADR-0129, ADR-0131, ADR-0137 and ADR-0138.

## Superseded: per-profile training topology

The plan's default was **one multi-condition model per microphone/input
profile** (`xvf_chip_aec`, a future `xvf_software_aec`, future USB profiles) —
explicitly *not* one model per leg. The reasoning: a single global model is too
broad across chip AEC / software AEC / raw USB / future mic families, while
one model per leg fragments a small real-positive corpus and multiplies
operational surface. Per-leg thresholds would then be calibrated after
training, with the fused configuration evaluated as the actual product
behaviour.

**ADR-0129 decided the opposite** — one specialized model per production leg,
each trained on data matched to that leg's own chain, fused by the existing
OR-gate. Where this plan's tooling or naming says "profile", read ADR-0129 for
what is actually being trained.

## Phase plan as written

Phase 0 (technical proof) is the only phase with shipped tooling; its surviving
content is in the spine.

- **Phase 1 — MVP pipeline.** Make the workflow repeatable for a technical
  operator: corpus export bundle with manifest/hashes/consent/profile facts and
  capture-plan metadata; a data-prep CLI for resampling, segmentation,
  alignment, feature extraction and real-positive injection; a manual cloud
  training runner; an evaluation report generator; a model import path into the
  existing registry and wake setup; per-leg threshold recommendations.
  Explicitly no consumer-facing one-click flow yet.
- **Phase 2 — production hardening.** Immutable model metadata sidecar;
  staging vs production aliases; shadow-mode scoring before activation; per-leg
  and fused threshold calibration; a rollback path; wake-telemetry comparison
  before and after activation; privacy-preserving observability (scores, legs,
  decisions, outcome metadata — raw audio only on explicit consent).
- **Phase 3 — guided product flow.** Consent UX; guided corpus collection by
  distance and condition; a coverage meter showing which cells are sufficient,
  missing or poor quality; optional hard-negative capture; cloud training
  submission; a results page with recall, false accepts/hour, condition
  breakdown and recommended thresholds; one-click shadow, activate and
  rollback.
- **Phase 4 — advanced optimization**, only once the basic flow works: per-leg
  specialized models where evaluation justifies them; a two-stage
  verifier/cascade for false-accept reduction; active learning from confirmed
  false accepts and rejects; more realistic AEC-residual and RIR simulation;
  lower-power wake-model variants only if a supported hardware profile ever
  requires them.

## Proposed module boundaries

Illustrative names, written before the tooling existed:

- `jts-corpus-export` — bundle WAVs, manifest, hashes, consent, capture graph,
  hardware/profile facts. Realized as `scripts/export-wake-corpus-bundle.sh`.
- `jts-wake-dataprep` — resample, normalize, segment/end-align, compute
  features, build train/validation/test banks. Realized as
  `scripts/build-wake-feature-bank.sh` (and its negative counterpart) for
  already-16 kHz bundle WAVs.
- `jts-livekit-train` — synthetic positives, real-positive injection, LiveKit
  training and export off-Pi.
- `jts-wake-eval` — DET/ROC, false accepts/hour, stratified recall,
  fused-threshold simulation.
- `jts-threshold-calibrator` — per-leg and fused threshold recommendations.
- `jts-model-registry` — immutable artifact, sidecar metadata, aliases,
  rollback.
- `jts-wake-shadow` — run candidate models in parallel without firing.

The boundary rule behind the list survives in ADR-0137: training orchestration
does not know how `jasper-voice` opens microphones, the Pi runtime does not
know how cloud jobs are launched, corpus export does not train, and evaluation
is reproducible from immutable artifacts.

## Risk register as written

- **Real-positive injection is custom** — LiveKit primarily exposes a
  synthetic-positive flow, so the injection shim was the highest-risk piece and
  had to be proven first.
- **Model compatibility must be verified in JTS**, by loading the produced ONNX
  in the real runtime rather than trusting a README claim.
- **False accepts can dominate**: a recall improvement that raises fused false
  accepts is not a win.
- **Synthetic data can overfit to TTS artifacts** — hence real positives,
  diverse voices, RIR/noise/music augmentation and a held-out real eval.
- **Do not optimize for one room forever.** The first user-specific model may be
  room-specific; the architecture should still admit other users, rooms, mics
  and DACs.

The plan also carried a "COAH review gate" — a per-slice staff-maintainer
checklist. That has been dropped: AGENTS.md's tiered review policy replaced the
mandatory-gate model repo-wide.
