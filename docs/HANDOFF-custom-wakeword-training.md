# HANDOFF — Custom wake-word training workflow

> **Current operational truth.** This is the canonical plan for turning
> JTS wake-corpus recordings into custom wake-word models trained off-Pi
> and deployed back into the existing JTS openWakeWord fusion runtime.
> It is a productization plan plus the current offline data-prep tooling,
> not a shipped end-to-end training pipeline.
>
> Companion docs: [wake-training experiment](HANDOFF-wake-training-experiment.md)
> for the original corpus/reliability rationale,
> [wake-corpus quality](HANDOFF-wake-corpus-quality.md) for corpus QA,
> [mic fusion](HANDOFF-mic-fusion-architecture.md) for the N-leg runtime,
> and [audio capability platform](HANDOFF-audio-capability-platform.md)
> for microphone/DAC/profile boundaries.

---

## TL;DR

Use **LiveKit wakeword only as the off-Pi trainer/exporter**. Keep the
JTS Pi runtime on the existing openWakeWord-compatible ONNX path and
multi-leg fusion architecture.

The central engineering bet is a **real-positive injection shim**:
convert JTS wake-corpus clips into the openWakeWord/LiveKit feature
contract, append those features to LiveKit's synthetic positive bank,
train off-Pi, then evaluate and deploy the exported ONNX model back to
JTS without changing the hot wake runtime.

Default training topology:

- Train **one multi-condition model per microphone/input profile**, not
  one global model and not one model per leg by default.
- Use the profile's real same-utterance legs as training/evaluation
  data: chip AEC beams, raw, WebRTC AEC3, DTLN, USB variants where
  available.
- Calibrate **per-leg thresholds** after training.
- Evaluate the final **fused** configuration, because OR-fusion can
  increase aggregate false accepts.

Budget assumption: no training on the Pi; $50 of cloud compute per
iteration is acceptable. In practice, a single wake-word run should be
well below that if the pipeline is efficient.

---

## Current Decision

### Runtime contract to preserve

JTS should continue loading wake models through its existing
openWakeWord-compatible runtime. The model contract to preserve:

- Input audio path: 16 kHz mono PCM.
- Streaming frame cadence: 1280 samples / 80 ms.
- Feature shape: `(1, 16, 96)` wake embedding window.
- Classifier output: one scalar score per model.
- Runtime behavior: existing `jasper-voice` wake loop and wake-leg
  fusion stay in control.

LiveKit wakeword is attractive because its exported ONNX models use the
same openWakeWord-style front end and are intended to be compatible with
existing openWakeWord integrations. The LiveKit value is the trainer:
synthetic generation, augmentation, conv-attention classifier head, eval,
and ONNX export.

### What not to do first

Do not:

- Train on the Pi.
- Replace the JTS wake runtime with LiveKit's listener/runtime.
- Build a one-click cloud training product before proving the feature
  injection path works.
- Start with one model per leg unless evaluation shows a clear need.
- Treat recall wins as sufficient without negative-hours false-accept
  testing.

---

## Why This Matters

Generic wake models are trained against generic microphone and room
conditions. JTS is not generic:

- It has custom final-output ownership through `jasper-outputd`.
- It can run chip-based XVF3800 AEC/beamforming.
- It can run software WebRTC AEC3.
- It can run DTLN neural cleanup.
- It may use different DACs and microphone families over time.
- It fuses several wake legs through an OR-style recall layer.

The corpus recorder now lets an operator capture the same spoken wake
utterance through the exact hardware and signal paths that production
uses. That is the asset. The training workflow should turn that asset
into a model matched to the user's real room, mic, DAC, AEC artifacts,
beamforming, music playback path, and distance conditions.

---

## Data Model

Each recorded wake utterance should be treated as a single event with
multiple synchronized leg artifacts. Required or strongly preferred
metadata:

- Stable utterance ID linking all legs for the same spoken instance.
- Speaker/consent ID, pseudonymous.
- Phrase label and label kind: positive, hard negative, background.
- Condition: quiet, ambient, music, TV/speech if added later.
- Distance bucket.
- Microphone profile and detected hardware facts.
- DAC/output profile and final speaker-reference path.
- Wake leg token: chip AEC 150, chip AEC 210, XVF raw0, XVF raw0 AEC3,
  XVF raw0 DTLN, USB raw, USB AEC3, USB DTLN, reference, etc.
- Capture graph/profile version.
- Onset/offset or segment timing when available.
- SNR and basic quality flags when available.
- Source WAV hash and resample/feature provenance.
- XVF/chip profile snapshot where relevant.

The existing `/wake-corpus/` recorder is the right collection surface.
Future training work should extend the metadata/bundle contracts rather
than inventing a parallel recorder.

---

## Training Topology

### Default: one model per input profile

Examples: `xvf_chip_aec`, future `xvf_software_aec`, and future USB
mic profiles. Rationale:

- A single global model is likely too broad across chip AEC, software
  AEC, raw USB, and future microphone families.
- One model per leg fragments a small real-positive corpus and creates
  too much operational surface area.
- Per-profile training preserves enough signal-path specificity while
  still letting the model learn invariance across the profile's normal
  production/comparison legs.

### Thresholds are per leg

Even when a profile shares one model, each leg can need a different
threshold. Chip AEC beams, raw, AEC3, and DTLN have different artifact
distributions. Calibrate thresholds per leg, then evaluate the fused
configuration as the actual product behavior.

---

## Evaluation Gates

Do not promote a trained model on recall alone.

Minimum evaluation surfaces:

- Held-out real positives, stratified by distance and condition.
- Negative-hours audio captured through JTS-like legs: music, TV,
  household speech, background noise, and silence.
- Per-leg DET/ROC or equivalent score sweep.
- Per-leg false accepts per hour.
- Fused false accepts per hour.
- Recall by condition and distance.
- Human listening review for representative wins and losses.

Working product target:

- Improve recall in the hardest useful cells, especially far + music.
- Keep fused false accepts at or below the chosen production budget.
- Do not regress quiet/near responsiveness in normal use.

Because OR-fusion can increase aggregate false accepts, each leg may
need a stricter threshold than it would use standalone.

---

## Phased Plan

### Phase 0 — Technical Proof

Goal: prove that JTS real positives can improve a LiveKit-trained,
openWakeWord-compatible model without changing the Pi runtime.

Tasks:

1. Inspect the current JTS corpus format and wake runtime.
2. Export a tiny existing corpus bundle.
3. Convert selected JTS clips to 16 kHz mono.
4. Compute openWakeWord/LiveKit-compatible `(N, 16, 96)` features.
5. Append real-positive features to LiveKit's positive feature bank.
6. Train one small `xvf_chip_aec` model off-Pi.
7. Confirm the ONNX loads through the existing JTS wake runtime.
8. Evaluate held-out positives and representative negatives.

Gate:

- New model loads in JTS with no runtime architecture change.
- New model is at least competitive with the incumbent at a controlled
  false-accept target.

If this fails, investigate openWakeWord's lower-level verifier/custom
training path before building product UI.

Current implementation state:

- `scripts/export-wake-corpus-bundle.sh` (backed by
  `scripts/_export_wake_corpus_bundle.py`) implements the first
  training-oriented export for browser-recorded `/wake-corpus/` sessions.
- It consumes `data/enrollment_positives/`, preserves same-utterance
  sibling legs in one train/eval split, copies accepted WAVs into an
  `audio/<split>/<condition>/<distance>/<leg>/<utterance>/` tree, and emits
  `bundle.json`, `manifest.jsonl`, `manifest.csv`, `rejections.jsonl`,
  and `SHA256SUMS`.
- It is intentionally not a data-prep or training tool: no resampling,
  end-alignment, feature extraction, LiveKit calls, cloud job launch, or
  model evaluation.
- `scripts/build-wake-feature-bank.sh` (backed by
  `scripts/_build_wake_feature_bank.py`) implements the first
  real-positive feature-bank builder. It consumes the bundle manifest,
  end-aligns each accepted 16 kHz mono WAV into a 2-second /
  32,000-sample openWakeWord training window, extracts ONNX
  openWakeWord speech-embedding features in batches, and writes
  `positive_features_train.npy`, `positive_features_eval.npy`,
  `feature_manifest.jsonl`, `feature_rejections.jsonl`, and
  `feature_bank.json`.
- It verifies each source WAV against the bundle manifest hash before
  extraction, so feature banks are tied to the exact exported audio bytes.
- Positive and negative feature-bank builders share their offline data-prep
  contract through `jasper/wake_training/feature_bank.py`: bundle reads, WAV
  format checks, SHA-256 verification, end-aligned windows, batched
  openWakeWord feature extraction, and JSONL writing. New wake-training
  data-prep scripts should extend that utility rather than importing private
  helpers from another script.
- The positive feature-bank builder is intentionally still not a trainer:
  no LiveKit calls, synthetic data generation, threshold tuning, cloud job
  launch, model registry writes, or runtime changes.
- `scripts/build-wake-negative-feature-bank.sh` (backed by
  `scripts/_build_wake_negative_feature_bank.py`) implements the first
  negative-hours / hard-negative feature-bank builder. It consumes the same
  bundle manifest, verifies each source WAV hash, end-aligns each accepted
  16 kHz mono WAV into the same 2-second openWakeWord window, extracts ONNX
  speech-embedding features, and writes `negative_features_train.npy`,
  `negative_features_eval.npy`, `negative_feature_manifest.jsonl`,
  `negative_feature_rejections.jsonl`, and `negative_feature_bank.json`.
- Negative rows must be explicitly labeled as `negative`,
  `hard_negative`, `ambient_negative`, or `background` unless the operator
  passes `--allow-unlabeled-as <kind>` for a dedicated negative-only legacy
  corpus. This keeps wake-positive clips out of the negative bank by default
  while still letting old captured negative sessions be used deliberately.
- The negative feature-bank builder is intentionally still not a trainer:
  no positive generation, LiveKit calls, threshold tuning, cloud job launch,
  model registry writes, or runtime changes.
- `scripts/prepare-wake-training-workdir.sh` (backed by
  `scripts/_prepare_wake_training_workdir.py`) implements the first
  real-positive injection prep step. It consumes `feature_bank.json`,
  `feature_manifest.jsonl`, `positive_features_train.npy`, and
  `positive_features_eval.npy`, verifies the manifest/array contract,
  maps the JTS eval split to LiveKit/openWakeWord's
  `positive_features_test.npy` convention, repeats train positives with
  an explicit configurable weight (default `3x`), and writes
  `training_workdir.json`, `real_positive_injection.json`,
  `real_positive_manifest.jsonl`, and `feature_data/positive_features_*`.
- The training-workdir prep is intentionally still not a trainer: no
  LiveKit calls, synthetic data generation, negative/background feature
  banks, threshold tuning, cloud job launch, model registry writes, or
  runtime changes.
- `scripts/prepare-wake-livekit-smoke.sh` (backed by
  `scripts/_prepare_wake_livekit_smoke.py`) implements the first
  LiveKit mechanics proof harness. It consumes a JTS training workdir,
  creates a LiveKit-compatible model output directory with
  `positive_features_train.npy`, `positive_features_test.npy`,
  `negative_features_train.npy`, and `negative_features_test.npy`,
  writes a tiny LiveKit config, and can optionally run
  `livekit-wakeword train`, `export`, and `eval` when an off-Pi host has
  LiveKit training dependencies installed.
- The smoke harness uses deterministic embedding-space placeholder
  negatives unless the operator supplies real negative feature files.
  That makes it useful for proving train/export/eval mechanics, not for
  interpreting model quality. Do not deploy a smoke model.
- `scripts/run-wake-training-phase0.sh` (backed by
  `scripts/_run_wake_training_phase0.py`) is the first end-to-end Phase 0
  operator runner. It orchestrates corpus export, positive feature-bank
  build, negative/hard-negative feature-bank build, real-positive workdir
  prep, and LiveKit train/export/eval prep into one timestamped evidence
  directory with `phase0_run.json` and `command_log.jsonl`.
- The Phase 0 runner requires real negative input by default via
  `--negative-corpus-dir` or `--negative-bundle-dir`. Operators can pass
  `--allow-placeholder-negatives` for mechanics-only smoke tests, but that is
  explicitly not model-quality evidence.
- `--force` on the Phase 0 runner, training-workdir prep, and LiveKit smoke
  prep replaces only each tool's standard owned output tree or a custom
  directory with that tool's valid, self-bound manifest. Protected paths and
  their ancestors, final symlinks, malformed manifests, and copied manifests
  whose recorded `output_dir` does not resolve to the candidate are refused.
- The next Phase 0 slice should run this runner off-Pi with real positive
  features plus real negative feature banks, then compare the exported ONNX
  against the incumbent on held-out JTS audio.

### Phase 1 — MVP Pipeline

Goal: make the workflow repeatable for a technical operator.

Build:

- Corpus export bundle with manifest, hashes, consent metadata, profile
  facts, and capture-plan metadata.
- Data prep CLI for resampling, segmentation/alignment, feature
  extraction, and real-positive injection.
- Manual cloud training runner using LiveKit wakeword.
- Evaluation report generator.
- Model import path into the existing model registry/wake setup.
- Per-leg threshold recommendation output.

Do not build a consumer-facing one-click flow yet.

### Phase 2 — Production Hardening

Goal: make trained models safe to deploy and easy to roll back.

Build:

- Immutable model metadata sidecar.
- Staging vs production aliases.
- Shadow-mode scoring before activation.
- Per-leg and fused threshold calibration.
- Rollback path.
- Wake telemetry comparison before/after activation.
- Privacy-preserving observability: scores, legs, decisions, outcome
  metadata; raw audio only when explicitly consented.

### Phase 3 — Guided Product Flow

Goal: let a non-expert record, train, evaluate, and install safely.

Build:

- Consent UX.
- Guided corpus collection by distance/condition.
- Coverage meter: which cells are sufficient, missing, or poor quality.
- Optional hard-negative capture.
- Cloud training submission.
- Results page: recall, false accepts/hour, condition breakdown,
  recommended thresholds.
- One-click shadow, activate, and rollback.

### Phase 4 — Advanced Optimization

Only after the basic flow works:

- Per-leg specialized models where evaluation justifies them.
- Two-stage verifier/cascade for false-accept reduction.
- Active learning from confirmed false accepts/rejects.
- More realistic AEC residual/RIR simulation.
- MCU/microWakeWord path for future satellite devices.

---

## Proposed Module Boundaries

Names are illustrative; keep final names aligned with existing local
patterns.

- `jts-corpus-export`: bundle WAVs, manifest, hashes, consent, capture
  graph, hardware/profile facts. First implementation:
  `scripts/export-wake-corpus-bundle.sh`.
- `jts-wake-dataprep`: resample, normalize, segment/end-align, compute
  features, build train/validation/test banks. First implementation:
  `scripts/build-wake-feature-bank.sh` for already-16 kHz bundle WAVs.
- `jts-livekit-train`: generate synthetic positives, inject real
  positives, run LiveKit training/export off-Pi.
- `jts-wake-eval`: DET/ROC, false accepts/hour, stratified recall,
  fused-threshold simulation.
- `jts-threshold-calibrator`: per-leg and fused threshold
  recommendations.
- `jts-model-registry`: immutable model artifact, sidecar metadata,
  aliases, rollback.
- `jts-wake-shadow`: run candidate models in parallel without firing.

Keep these boundaries separate. Training orchestration should not know
how `jasper-voice` opens microphones. The Pi runtime should not know how
cloud jobs are launched. Corpus export should not train. Evaluation
should be reproducible from immutable artifacts.

---

## Artifact Contracts

Every trained model should ship with:

- ONNX model file.
- Model metadata JSON.
- Training config.
- Data manifest hash.
- Feature-front-end version.
- Target phrase and aliases.
- Input profile target.
- Leg coverage summary.
- Recommended thresholds per leg.
- Evaluation metrics by leg and fused configuration.
- Negative-hours corpus summary.
- Training code version and LiveKit/openWakeWord version.
- Privacy/retention summary for source data.

Suggested model name:

`{phrase}__{profile}__{leg_or_multi}__v{major.minor.patch}__{hash}`

Version semantics:

- Major: runtime/feature-front-end or model architecture compatibility
  changes.
- Minor: new data scope, profile scope, or training recipe.
- Patch: rerun, threshold-only update, or equivalent low-risk refresh.

---

## Privacy and Safety

Voice recordings are sensitive. The productized workflow must be
explicitly opt-in.

Requirements:

- Operator consent before cloud training.
- Clear statement of what is uploaded.
- Minimize raw audio duration.
- Prefer ephemeral cloud storage and deletion after training.
- Hash and track artifacts.
- Do not log API keys, signed URLs, or raw transcript-like content.
- Store production telemetry as scores/metadata by default, not raw
  audio, unless the operator explicitly enables capture.
- Make deletion and rollback straightforward.

---

## COAH Review Gate

Every implementation slice for this workstream should be reviewed under
the JTS staff-maintainer bar:

- Clean separation of concerns.
- Modular boundaries that can scale to more DACs, mics, wake profiles,
  and model providers.
- Resilient failure behavior and rollback.
- Stable observability surfaces.
- Bounded CPU/memory/I/O, especially on Pi.
- Hardware/audio safety.
- Security and privacy hygiene.
- Targeted tests plus explicit hardware/cloud validation gaps.
- Docs impact scan and updates.

Do not let training-product work become a pile of one-off scripts. A
one-off spike is acceptable in Phase 0, but the contracts it proves
should be promoted into clear modules before building UX.

---

## Risks and Unknowns

- **Real-positive injection is custom.** LiveKit wakeword primarily
  exposes a synthetic-positive flow. The injection shim is the highest
  risk and must be proven first.
- **Model compatibility must be verified in JTS.** Do not rely only on
  README claims; load the produced ONNX in the actual JTS runtime.
- **False accepts can dominate.** A recall improvement that increases
  fused false accepts is not a product win.
- **Profile vs leg training is empirical.** Start per profile, but allow
  per-leg specialization if evaluation shows a real gap.
- **Synthetic data can overfit to TTS artifacts.** Use real positives,
  diverse voices, RIR/noise/music augmentation, and held-out real eval.
- **Cloud training adds privacy surface.** Keep the cloud boundary small,
  explicit, auditable, and deletable.
- **Do not optimize for one room forever.** The first user-specific model
  can be room-specific, but the architecture should support future
  users, rooms, mics, and DACs.

---

## References

- LiveKit wakeword repo:
  <https://github.com/livekit/livekit-wakeword>
- LiveKit launch post:
  <https://livekit.com/blog/livekit-wakeword>
- openWakeWord repo:
  <https://github.com/dscripka/openWakeWord>

Last verified: 2026-07-12 (updated after adding shared fail-closed `--force`
output-ownership checks for the Phase 0 runner, training-workdir prep, and
LiveKit smoke prep; previously added the corpus-bundle exporter,
openWakeWord-compatible positive and negative feature-bank builders,
training-workdir real-positive injection prep, and LiveKit train/export/eval
smoke harness; quality evaluation, registry, and deployment stages remain
future work).
