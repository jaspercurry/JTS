# ADR-0137: Wake-training data prep is a chain of hash-bound stages, none of which trains

- **Date:** 2026-08-26
- **Status:** Accepted (recorded when HANDOFF-custom-wakeword-training.md was
  trimmed to its operational spine; the shape emerged across the Phase 0
  tooling slices)

## Context

Turning recorded wake utterances into a trained model is six or seven
transformations deep — export, hash, split, resample, end-align, embed, weight,
inject, train, export, evaluate. Written as one script it becomes a pile
nobody can rerun a piece of, and a model whose provenance is "it came out of
the pipeline" cannot be compared against the incumbent with a straight face.

## Decision

**Each stage is a separate tool that owns one output tree, verifies the
previous stage's artifacts by SHA-256 before reading them, and does not train.**

The chain: corpus export → positive feature bank → negative feature bank →
training workdir (real-positive injection) → LiveKit train/export/eval prep.
Every stage writes a manifest plus a JSONL of what it rejected and why. The
shared read/verify/window/extract contract lives in one place —
`wake_training/feature_bank.py`, top-level and deliberately outside the shipped
package — and new data-prep tools extend it rather than importing another
script's private helpers.

`--force` is fail-closed: a tool replaces only its own standard output tree, or
a custom directory carrying that tool's valid self-bound manifest. Protected
paths and their ancestors, final symlinks, malformed manifests, and copied
manifests whose recorded `output_dir` does not resolve to the candidate are all
refused.

## Consequences

- **Every model is reproducible from immutable artifacts.** Feature banks are
  bound to the exact exported audio bytes, so "which clips produced this model"
  is answerable months later without re-recording anything.
- **A failed stage is rerunnable in isolation**, and the rejections file makes
  a shrinking corpus visible rather than silent — the failure mode where clips
  quietly drop out between stages is exactly what would corrupt a comparison
  against the incumbent.
- **The stages stay honest about what they are not.** Export does not resample
  or extract features; feature banks do not call LiveKit or tune thresholds;
  workdir prep does not launch cloud jobs. Each tool's doc says so explicitly
  because the temptation to grow one into the next is constant.
- **Negatives must be labelled to be used.** A negative bank accepts only rows
  labelled `negative` / `hard_negative` / `ambient_negative` / `background`
  unless the operator deliberately passes `--allow-unlabeled-as` for a legacy
  negative-only corpus — wake positives leaking into the negative bank would
  train the model against itself.
- **A mechanics smoke run is not evidence.** The LiveKit harness will
  substitute deterministic placeholder negatives to prove train/export/eval
  plumbing; a model produced that way is never deployed and never quoted.
