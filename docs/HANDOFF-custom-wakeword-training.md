# Custom wake-word training — operational spine

The off-Pi workflow that turns `/wake-corpus/` recordings into a custom
wake-word model the existing JTS runtime can load. This doc owns the **data-prep
tooling chain**; the experiment it feeds — corpus design, bars, and what gets
run — is
[HANDOFF-wake-training-experiment.md](HANDOFF-wake-training-experiment.md).

Decisions live in ADRs: **ADR-0129** (one model per production leg, trained
against that leg's chain, fused by the existing OR-gate — this supersedes the
older per-input-profile default), **ADR-0131** (ship and revert bars fixed
before the run), **ADR-0137** (the prep chain is hash-bound stages, none of
which trains), **ADR-0138** (household audio leaves the house only on explicit
consent). The 2026-07 four-phase productization plan, the proposed module
boundaries and the superseded per-profile topology are in
[historical/custom-wakeword-productization-plan-2026-07.md](historical/custom-wakeword-productization-plan-2026-07.md).

## The runtime contract that must not move

Whatever trains the model, the artifact has to drop into the running wake loop
with no Pi-side code change:

- 16 kHz mono PCM in;
- 1280-sample / 80 ms streaming frame cadence;
- `(1, 16, 96)` wake-embedding window — the frozen Google speech-embedding
  front end, shared by openWakeWord and livekit-wakeword;
- one scalar score out per model;
- `jasper-voice`'s wake loop and leg fusion stay in control of everything else.

Nothing trains on the Pi, and the JTS runtime is never replaced by a trainer's
own listener. The central bet is a **real-positive injection shim**: convert
corpus clips into the openWakeWord/LiveKit feature contract, append them to the
trainer's positive bank, train off-Pi, deploy the exported ONNX back.

## The prep chain

Five stages, each a `scripts/<name>.sh` wrapper over a `scripts/_<name>.py`
implementation, each verifying the previous stage by SHA-256 and each writing a
manifest plus a rejections JSONL (ADR-0137). The shared read/verify/window/
extract contract is **`wake_training/feature_bank.py`** — top-level, outside the
shipped package, deliberately not importable from `jasper/`. Extend it; never
import another script's private helpers.

| Stage | Script | Consumes → produces |
|---|---|---|
| 1. Corpus export | `export-wake-corpus-bundle.sh` | `data/enrollment_positives/` → `audio/<split>/<condition>/<distance>/<leg>/<utterance>/` plus `bundle.json`, `manifest.jsonl`, `manifest.csv`, `rejections.jsonl`, `SHA256SUMS`. Keeps same-utterance sibling legs together in one train/eval split. No resampling, alignment, features, or training. |
| 2. Positive features | `build-wake-feature-bank.sh` | Bundle manifest → `positive_features_{train,eval}.npy`, `feature_manifest.jsonl`, `feature_rejections.jsonl`, `feature_bank.json`. End-aligns each accepted 16 kHz mono WAV into a 2 s / 32,000-sample window, then batches ONNX speech-embedding extraction. Verifies each WAV against the bundle hash first. |
| 3. Negative features | `build-wake-negative-feature-bank.sh` | Same bundle contract → `negative_features_{train,eval}.npy` + matching manifest/rejections/bank JSON. Rows must be labelled `negative`, `hard_negative`, `ambient_negative` or `background`; `--allow-unlabeled-as <kind>` is the deliberate escape hatch for a legacy negative-only corpus. |
| 4. Training workdir | `prepare-wake-training-workdir.sh` | `feature_bank.json` + positive manifests/arrays → `training_workdir.json`, `real_positive_injection.json`, `real_positive_manifest.jsonl`, `feature_data/positive_features_*`. Verifies the manifest/array contract, maps the JTS eval split onto the trainer's `positive_features_test.npy` convention, and repeats train positives at a configurable weight (default 3×). |
| 5. LiveKit prep | `prepare-wake-livekit-smoke.sh` | Training workdir → a LiveKit-compatible model directory with all four feature arrays plus a tiny config; can run `livekit-wakeword train`/`export`/`eval` where an off-Pi host has the training deps. |

`run-wake-training-phase0.sh` orchestrates all five into one timestamped
evidence directory with `phase0_run.json` and `command_log.jsonl`. It requires
real negatives — `--negative-corpus-dir` or `--negative-bundle-dir` — by
default.

Two guard rails worth knowing before you run anything:

- **Placeholder negatives prove plumbing, not quality.** The smoke harness
  substitutes deterministic embedding-space negatives unless real ones are
  supplied, and the runner needs `--allow-placeholder-negatives` to use them.
  A model produced that way is never deployed and never quoted as evidence.
- **`--force` is fail-closed.** It replaces only the tool's own standard output
  tree, or a custom directory holding that tool's valid self-bound manifest.
  Protected paths and their ancestors, final symlinks, malformed manifests, and
  copied manifests whose recorded `output_dir` does not resolve to the
  candidate are refused.

## Corpus data model

Each recorded utterance is one event with several synchronized leg artifacts.
The `/wake-corpus/` recorder is the collection surface — extend its metadata and
bundle contracts rather than building a parallel recorder. What a bundle needs
to carry per utterance:

stable utterance id linking all legs; pseudonymous speaker/consent id; phrase
label and label kind (positive / hard negative / background); condition (quiet,
ambient, music, and TV/speech if added); distance bucket; mic profile and
detected hardware facts; DAC/output profile and speaker-reference path; wake-leg
token (`jasper/wake_legs.py`); capture-graph/profile version; onset/offset
timing where available; SNR and quality flags where available; source WAV hash
and resample/feature provenance; XVF/chip profile snapshot where relevant.

Corpus QA — what makes a clip acceptable in the first place — is
[HANDOFF-wake-corpus-quality.md](HANDOFF-wake-corpus-quality.md), which owns
artifact review; this chain owns dataset assembly. Do not fold one into the
other.

## Evaluation gates

Recall alone never promotes a model. The minimum surfaces:

- held-out real positives, stratified by distance and condition;
- negative-hours audio captured through JTS-like legs — music, TV, household
  speech, background noise, silence;
- per-leg DET/ROC or an equivalent score sweep;
- per-leg **and fused** false accepts per hour;
- recall by condition and distance;
- a human listening pass over representative wins and losses.

The working target is better recall in the hardest useful cells (far + music),
fused false accepts at or under the production budget, and no regression in
quiet/near responsiveness. Because OR-fusion aggregates false accepts, a leg may
need a stricter threshold in the fused configuration than it would standalone.
The hard ship/revert numbers are ADR-0131's, not this doc's.

Deployment stages — model registry, staging/production aliases, shadow-mode
scoring, rollback — are not built. ADR-0138 fixes the artifact and naming
contract they will have to satisfy.

## References

- livekit-wakeword — <https://github.com/livekit/livekit-wakeword>
  ([launch post](https://livekit.com/blog/livekit-wakeword))
- openWakeWord — <https://github.com/dscripka/openWakeWord>

Last verified: 2026-08-26 (the five-stage chain rechecked against
`scripts/export-wake-corpus-bundle.sh`, `scripts/build-wake-feature-bank.sh`,
`scripts/build-wake-negative-feature-bank.sh`,
`scripts/prepare-wake-training-workdir.sh`,
`scripts/prepare-wake-livekit-smoke.sh`, `scripts/run-wake-training-phase0.sh`
and the shared `wake_training/feature_bank.py`, which is top-level and outside
the shipped package. The per-input-profile training default was removed as
superseded by ADR-0129.)
