# ADR-0138: Household audio leaves the house only on explicit consent

- **Date:** 2026-08-26
- **Status:** Accepted (recorded when HANDOFF-custom-wakeword-training.md was
  trimmed to its operational spine)

## Context

Everything else this speaker records stays on the Pi: wake-event WAVs are local
and never uploaded (ADR-0133), telemetry never leaves the device. Off-Pi
wake-model training is the one workflow that deliberately breaks that, because
the training compute does not fit in 1 GB of RAM. It uploads recordings of
people speaking in their own home to a cloud trainer.

## Decision

**Cloud training is opt-in per run, states exactly what it uploads, and every
uploaded artifact is deletable.**

- Operator consent is taken before any upload, against a clear statement of
  what leaves the device.
- Raw audio duration is minimized — features and manifests where they suffice,
  clips only where training needs them.
- Cloud storage is ephemeral and deleted after the run.
- Production telemetry stays scores and metadata by default; raw audio capture
  is a thing the operator turns on, not a default.
- API keys, signed URLs, and transcript-like content are never logged
  (AGENTS.md non-negotiable 3 already covers the keys; this extends the habit
  to the URLs and the content).
- Rollback and deletion are ordinary operations, not recovery procedures.

## Consequences

- **The cloud boundary stays small, explicit and auditable** — one stage of the
  pipeline crosses it (ADR-0137), and that stage is the only place consent and
  upload policy have to be reasoned about.
- **A trained model ships with its own provenance**: the ONNX, metadata JSON,
  training config, data-manifest hash, feature-front-end version, target phrase
  and aliases, per-leg thresholds and evaluation metrics, negative-hours corpus
  summary, training-code and trainer versions, and the retention summary for
  the source data. A model whose source data has been deleted is still
  describable.
- **Model names carry their scope**:
  `{phrase}__{profile}__{leg_or_multi}__v{major.minor.patch}__{hash}`, where
  major moves on runtime/front-end/architecture compatibility, minor on new
  data or recipe scope, and patch on a rerun or threshold-only refresh.
- **This constrains any future guided/product flow** before it is designed:
  consent UX and a visible deletion path are requirements of the first version,
  not hardening added later.
