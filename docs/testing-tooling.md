# Testing & measurement tools — index

> **Before writing a new test or measurement script, read this doc.**
>
> The repo has accumulated several testing/measurement tools over time
> (mic capture, wake-word scoring, wake-event telemetry, bridge
> forensics, Pi-side diagnostics, voice-eval). Each one was added to
> solve a specific question. If your current question overlaps with
> what one of them already answers, **extend or reuse it** rather than
> writing a parallel tool.
>
> This doc exists because in May 2026 a new "reference-conditions
> capture" script got added that turned out to substantially duplicate
> `scripts/wake-rate-test.sh`. The cost was a refactor + a missed
> day. The point of this index is to make that less likely next time.

---

## Quick lookup — by question

| If you want to … | Start with |
|---|---|
| Capture the AEC bridge's three streams (raw mic / AEC ON / reference) | [Capture: 3-stream bridge captures](#capture-3-stream-bridge-captures) |
| Audit the deliberate wake-corpus recorder output after rsync | [Wake-corpus audit (deliberate recordings)](#wake-corpus-audit-deliberate-recordings) |
| Export wake-corpus recordings for off-Pi training | [Wake-corpus training bundle export](#wake-corpus-training-bundle-export) |
| Analyze wake-corpus audio artifacts / quality | [Wake-corpus quality analyzer](#wake-corpus-quality-analyzer) |
| Count wake-word detections on captured audio offline | [Wake-word scoring (offline)](#wake-word-scoring-offline) |
| Pull production wake events + clips from the Pi | [Wake-event telemetry (production)](#wake-event-telemetry-production) |
| Diagnose a bridge / AEC issue forensically | [AEC / bridge forensics](#aec--bridge-forensics) |
| Generate a fixed audio test track for repeatable testing | [Test-track generation](#test-track-generation) |
| Check live Pi state (services / config / mic / etc.) | [Pi-side diagnostics](#pi-side-diagnostics) |
| Validate two Apple USB-C DACs as a lab-only output topology | [Dual Apple DAC lab runner](#dual-apple-dac-lab-runner) |
| Characterize whole-system CPU/memory/journal behavior over time | [System soak artifacts](#system-soak-artifacts) |
| Measure inter-speaker sync error for multi-room (stereo pair / sub) on WiFi | [Multi-room sync spike (P0)](#multi-room-sync-spike-p0) |
| Turn up logging for one subsystem on the live Pi (`/system` Debug card) | [`HANDOFF-observability.md`](HANDOFF-observability.md) |
| Get the verbose DEBUG context around a failure (in-RAM flight recorder, `event=flightrec.dump`) | [`HANDOFF-observability.md`](HANDOFF-observability.md) |
| Preview what install.sh would mutate | [Install dry-run plan](#install-dry-run-plan) |
| Check every shipped deploy unit/rule/script has an install step (and every install reference resolves) | [`tests/test_deploy_wiring_guards.py`](../tests/test_deploy_wiring_guards.py) — two-sided orphan-artifact guard |
| Check the "wizard env file wins" EnvironmentFile= ordering across systemd units | [`tests/test_deploy_wiring_guards.py`](../tests/test_deploy_wiring_guards.py) — wizard-env precedence guard |
| Check udev SYSTEMD_WANTS hotplug targets are shipped units | [`tests/test_deploy_wiring_guards.py`](../tests/test_deploy_wiring_guards.py) — udev → unit chain guard |
| Check wizard-socket ListenStream ports match nginx upstreams (PR #118 502 class) | [`tests/test_deploy_wiring_guards.py`](../tests/test_deploy_wiring_guards.py) — two-sided socket↔nginx parity guard |
| Check install/build supply-chain provenance | [Supply-chain provenance](#supply-chain-provenance) |
| Pin a documented invariant / convention with a test | [Guard & contract test patterns](#guard--contract-test-patterns) |
| Check optional ESP32 firmware still builds | [Optional ESP32 firmware builds](#optional-esp32-firmware-builds) |
| Test the assistant's *behavior* (does it understand a question, call the right tool) | [Voice-eval (paid LLM tests)](#voice-eval-paid-llm-tests) |
| Capture from a non-bridge source (satellite mic, raw chip) | [Capture: alternative sources](#capture-alternative-sources) |
| Pin a cross-language / cross-process name or shape (Rust↔Python JSON, env-var sets, dashboard payloads, doc-map globs) | [Guard & contract test patterns](#guard--contract-test-patterns) |

---

## Install dry-run plan

[`deploy/install.sh`](../deploy/install.sh) has a non-mutating plan
mode for contributors reviewing install/deploy changes:

```sh
bash deploy/install.sh --dry-run
# or: JASPER_INSTALL_DRY_RUN=1 bash deploy/install.sh
```

It exits before the root check and lists the major install surfaces:
apt package groups, direct downloads and source builds, runtime file
writes, env migrations, boot/config writes, systemd actions, restarts,
and post-install checks. Use it when touching deploy/install behavior
or when explaining what a fresh Pi install will do. It is a planning
surface only; real host-specific no-op decisions still live in
`install.sh` itself.

## Supply-chain provenance

[`scripts/check-provenance.py`](../scripts/check-provenance.py)
validates [`deploy/provenance.toml`](../deploy/provenance.toml)
against the fetch-bearing install/build surfaces JTS owns directly:
`deploy/install.sh`, Python direct URL dependencies, firmware
PlatformIO inputs, and the wake/DTLN model registries.

Run it when touching install/build downloads or dependency declarations:

```sh
python3 scripts/check-provenance.py
```

The policy and update workflow live in
[`docs/HANDOFF-supply-chain.md`](HANDOFF-supply-chain.md).

---

## Guard & contract test patterns

Reusable exemplars for AGENTS.md's "Pin promises with tests" rule —
when a comment, docstring, or doc states an invariant, one of these
shapes usually fits. All run in normal hardware-free `pytest`. Mirror
the closest one rather than inventing a new guard style:

| If you want to … | Mirror |
|---|---|
| Keep constants a bash script re-hardcodes in sync with their Python SSOT | [`tests/test_reconciler_constants_match_python.py`](../tests/test_reconciler_constants_match_python.py) — reads the Python values, parses the script's hardcoded fallbacks, fails naming the drifted constant and both values |
| Freeze a convention's current offenders and block new ones (burn-down list) | [`tests/test_atomic_io_conventions.py`](../tests/test_atomic_io_conventions.py) — two-sided allowlist ratchet: a new offender fails, and a stale allowlist entry fails too, so the list only shrinks |
| Enforce a repo-wide code convention that otherwise lives only in a comment | [`tests/test_shell_awk_environ_convention.py`](../tests/test_shell_awk_environ_convention.py) — mutation-verified convention guard: scoped so the benign idiom stays legal while the exact bug shape fails, naming file:line and the sanctioned replacement |
| Assert an import chain stays light (no heavy hard-deps in wizards/config) | [`tests/test_web_wizard_import_chain.py`](../tests/test_web_wizard_import_chain.py) + `tests/test_config.py::test_config_import_chain_does_not_require_httpx` — poisoned-import chain contract: import in a subprocess with the heavy module poisoned in `sys.modules`, so an installed copy can't mask a regression |
| Keep a hand-written plan/summary covering an orchestrator's real steps | [`tests/test_install_plan_covers_main.py`](../tests/test_install_plan_covers_main.py) — orchestrator/plan coverage: parses `main()`'s calls, asserts each maps to a marker in the actual `--dry-run` output; meta-assertions fail stale mappings loudly |
| Enforce an observability convention across every handler of a class | [`tests/test_web_wizard_event_audit.py`](../tests/test_web_wizard_event_audit.py) — behavior-coverage guard: every state-mutating/restarting wizard handler must emit an `event=` audit line; on first run it caught 3 unaudited voice-provider handlers that a manual sweep and three independent reviews had all missed |

---

## PEQ graph math parity (JS ↔ Python)

The /sound/ EQ graph draws real RBJ biquad magnitude in the browser
([`deploy/assets/sound-profile/js/eq-math.js`](../deploy/assets/sound-profile/js/eq-math.js)),
mirrored by the Python preview in
[`jasper/sound/profile.py`](../jasper/sound/profile.py)
(`_biquad_coeffs` / `_filter_response_db`).
[`tests/fixtures/peq_response_fixture.json`](../tests/fixtures/peq_response_fixture.json)
is the shared contract:

```sh
node scripts/check-peq-parity.mjs   # asserts eq-math.js matches the fixture
```

`tests/test_sound_peq_response.py` asserts the Python side matches the same
fixture (and adds filter-theory sanity probes). Run the node check when you
touch either implementation — if they drift, one side regressed. The Python
half runs in normal `pytest`; the node half is a maintainer check (like the
firmware builds below), not always-on CI.

---

## Optional ESP32 firmware builds

[`scripts/check-firmware-builds.sh`](../scripts/check-firmware-builds.sh)
builds the optional ESP32 satellite firmware projects without flashing
hardware:

```sh
scripts/check-firmware-builds.sh              # dial + AMOLED
scripts/check-firmware-builds.sh dial         # just the rotary dial
scripts/check-firmware-builds.sh satellite-amoled
```

Run it when touching `firmware/`, PlatformIO dependency pins, or
accessory onboarding. It is deliberately not part of always-on PR CI:
most JTS installs do not use accessory hardware, and first-run
PlatformIO toolchain setup is a large download. Normal `install.sh`
stages firmware source but only rebuilds staged binaries when the
operator opts in with `JASPER_BUILD_OPTIONAL_FIRMWARE=1`.

---

## Capture: 3-stream bridge captures

Both of these use the AEC bridge's built-in debug-record mode
(`JASPER_AEC_DEBUG_RECORD_DIR`, see [`jasper/cli/aec_bridge.py`](../jasper/cli/aec_bridge.py)
`_aec_loop` — writes three time-aligned WAVs: `mic_ch1` raw chip,
`aec_output` post-AEC3, `ref` playback reference). Both apply the
same systemd drop-in override pattern and stop `jasper-voice` during
capture for clean recordings. Outputs are renamed to functional
names: `aec-off.wav` / `aec-on.wav` / `reference.wav`.

| Tool | Methodology | Output location | When to use |
|---|---|---|---|
| [`scripts/wake-rate-test.sh`](../scripts/wake-rate-test.sh) | Fixed audio track played from a phone; cross-correlation locates each utterance; per-utterance detection status reported | `logs/wake-rate/<session>/test-<N>/` | Reproducible cross-session A/B (same audio every time eliminates "how loud was your voice this time" confound). Run when comparing bridge configs, AEC engines, or wake models on a stable input. |
| [`scripts/capture-reference-condition.sh`](../scripts/capture-reference-condition.sh) | User speaks live during the capture window; one capture per stylistic condition (whisper-quiet, music-yell, etc.) | `reference-conditions/<condition>/` | Building a personalized baseline that covers real human speech variation (whisper to yell, quiet to music). User-private, gitignored. |

**They share the same orchestration mechanism.** If you find yourself
writing a third "bridge capture" script, you almost certainly want to
add a flag to one of these two instead.

---

## Wake-word scoring (offline)

Both score with `openwakeword.model.Model`, both use 1280-sample
(80 ms @ 16 kHz) frames matching production's WakeLoop. They differ
in scope:

| Tool | Scope | Output |
|---|---|---|
| [`scripts/_offline_wake_count.py`](../scripts/_offline_wake_count.py) | **One file, per-utterance.** Template-based cross-correlation locates each utterance, then reports peak score / RMS / category (`detected` / `near_miss` / `weak_signal` / `silent_miss`) per utterance. Production-default threshold 0.5; near-miss floor 0.10 (matches wake-events DB). | text or JSON, one block per utterance |
| [`scripts/score-baseline-wakeword.py`](../scripts/score-baseline-wakeword.py) | **Batch, per-file.** Streams each file end-to-end, reports file-level peak / fires-at-three-thresholds / mean / median. Designed to run across the entire `reference-conditions/` corpus in one invocation. | CSV (one row per file) + summary table |

**Default thresholds: 0.5 / 0.3 / 0.1.** These match production
(`jasper/wake.py` default 0.5) and the wake-events DB near-miss floor
(0.10, per [`HANDOFF-wake-telemetry.md`](HANDOFF-wake-telemetry.md)).
Don't invent new threshold tiers without checking against these.

`_offline_wake_count.py` is the underscore-prefixed Python helper
called by `wake-rate-test.sh`. `score-baseline-wakeword.py` is a
top-level user-callable tool because batch scoring across a corpus
is a standalone use case.

---

## Wake-event telemetry (production)

Production wake-event capture is in [`jasper/wake_events.py`](../jasper/wake_events.py)
— writes to SQLite at `/var/lib/jasper/wake-events/wake-events.sqlite3`
with per-event WAVs (4 s pre + 2 s post wake fire, both AEC ON and
AEC OFF legs). See [`HANDOFF-wake-telemetry.md`](HANDOFF-wake-telemetry.md)
for the schema + funnel design.

| Tool | Purpose |
|---|---|
| [`scripts/fetch-wake-events.sh`](../scripts/fetch-wake-events.sh) | Pulls a consistent SQLite snapshot + all WAVs to `./wake-events/<UTC-ts>/`, generates `index.csv` + `index.tsv`, optionally opens Finder |
| [`scripts/audit-wake-events.sh`](../scripts/audit-wake-events.sh) | Wraps `_audit_wake_events.py`: WAV integrity + cross-leg parity (xcorr time-alignment) + DB column populated counts |
| [`scripts/_audit_wake_events.py`](../scripts/_audit_wake_events.py) | The forensic audit Python helper called by the .sh wrapper |

**This system is for production telemetry only.** If you have
controlled-lab WAVs (e.g. from `wake-rate-test.sh` or
`capture-reference-condition.sh`), don't try to ingest them into the
wake-events DB — different schema, different assumptions. Use offline
scoring tools instead.

---

## Wake-corpus audit (deliberate recordings)

The browser recorder at `http://jts.local/wake-corpus/` writes the
Phase 0b gold corpus under `/var/lib/jasper/enrollment_positives/`
with per-session JSON sidecars in `metadata/`. After rsyncing that
directory to `./data/enrollment_positives/`, run:

```sh
bash scripts/audit-wake-corpus.sh \
  data/enrollment_positives --expect-raw0
```

For Session A, add `--min-per-cell 7` after the recording is complete.
For Session B, use `--min-per-cell 2` for the Jarvis held-out portion;
hard negatives have a different target distribution and should be
reviewed separately from the 3 × 3 Jarvis matrix.
For optional cheap-USB sessions, add repeated leg checks such as
`--expect-leg ref --expect-leg usb_raw --expect-leg usb_webrtc`; add
`--expect-leg usb_dtln` only for sessions where USB DTLN was enabled.
For AEC3 sweep pilot sessions, the audit discovers the active sweep
legs from `jasper/aec_sweep.py` and also accepts older legacy sweep
legs so same-day pilot recordings remain auditable after the registry
is retargeted.

The audit checks:
- Session metadata readability and `include_raw_mic_0` flags
- Missing expected legs, especially raw0 in raw0-enabled sessions
- Condition × distance coverage matrix
- WAV existence, format (16 kHz mono int16), duration, RMS, and peak
- Recorder `capture_health` metadata when present: compromised clips
  fail the audit, while warning/unknown clips are surfaced for review
- Session `audio_context` summary when present: production profile,
  active mic, firmware/channel state, and validation-artifact status
- Per-clip `selected_legs` drift against the session's expected legs

This is separate from production wake-event telemetry. It does not
read `wake-events.sqlite3` and does not score wake-word models; it is
the quick "did the gold corpus record what we think it recorded?"
gate before Phase 0a/0c work.

For deeper signal-quality analysis — artifacts, tears/clicks, AGC pumping,
clipping, cross-leg event coincidence, and review prioritization — use the
[Wake-corpus quality analyzer](#wake-corpus-quality-analyzer) below; its
methodology + metric definitions live in
[`HANDOFF-wake-corpus-quality.md`](HANDOFF-wake-corpus-quality.md). Extend the
quick corpus audit above only when a new check belongs in the fast integrity
gate rather than the deeper analyzer.

---

## Wake-corpus training bundle export

Laptop-side, offline. Converts browser-recorded
`data/enrollment_positives/` sessions into the first training-oriented
artifact for the custom wake-word workflow. It copies usable WAVs into a
stable `audio/<split>/<condition>/<distance>/<leg>/<utterance>/` tree and
writes `bundle.json`, `manifest.jsonl`, `manifest.csv`, `rejections.jsonl`,
and `SHA256SUMS`.

```sh
bash scripts/export-wake-corpus-bundle.sh data/enrollment_positives
bash scripts/export-wake-corpus-bundle.sh data/enrollment_positives logs/wake-export --latest 3
```

Use this after the quick corpus audit passes and before feature extraction or
LiveKit/openWakeWord training. The exporter:

- keeps sibling legs from the same spoken utterance in the same train/eval
  split;
- preserves profile, condition, distance, capture-plan, per-leg source, and
  processing metadata;
- remaps Pi absolute WAV paths to the local rsynced corpus copy;
- hashes every accepted WAV;
- rejects missing, malformed, wrong-format, or compromised-capture clips into
  `rejections.jsonl` instead of silently training on them.

It does not resample, segment, score, extract openWakeWord features, or train.
Those later stages are owned by
[`HANDOFF-custom-wakeword-training.md`](HANDOFF-custom-wakeword-training.md).

---

## Wake-corpus feature-bank builder

Laptop-side or training-host-side, offline. Consumes the bundle produced by
`scripts/export-wake-corpus-bundle.sh` and extracts the first
openWakeWord-compatible real-positive feature arrays.

```sh
bash scripts/build-wake-feature-bank.sh logs/wake-corpus-export/20260609T120000Z
bash scripts/build-wake-feature-bank.sh logs/wake-corpus-export/20260609T120000Z logs/wake-features --leg chip_aec_150
```

Outputs:

- `positive_features_train.npy`
- `positive_features_eval.npy`
- `feature_manifest.jsonl`
- `feature_rejections.jsonl`
- `feature_bank.json`

The builder keeps the bundle split as source of truth, end-aligns each WAV into
a 2-second / 32,000-sample window, and extracts `(16, 96)` embeddings through
`openwakeword.utils.AudioFeatures` with ONNX feature models. It requires
`openwakeword==0.6.0`, `onnxruntime`, `numpy`, and staged
`melspectrogram.onnx` / `embedding_model.onnx` assets; pass
`--melspec-model` and `--embedding-model` when running outside the JTS runtime
environment. It verifies each source WAV against the bundle manifest's SHA-256
before extraction.

It does not inject the features into LiveKit, build negative banks, train,
score, or alter Pi runtime state.

---

## Wake negative feature-bank builder

Laptop-side or training-host-side, offline. Consumes the bundle produced by
`scripts/export-wake-corpus-bundle.sh` and extracts openWakeWord-compatible
negative feature arrays from natural negative-hours and hard-negative clips.

```sh
bash scripts/build-wake-negative-feature-bank.sh logs/wake-corpus-export/20260609T120000Z
bash scripts/build-wake-negative-feature-bank.sh logs/wake-corpus-export/20260609T120000Z logs/wake-negatives --label-kind hard_negative
bash scripts/build-wake-negative-feature-bank.sh logs/negative-only-bundle --allow-unlabeled-as ambient_negative
```

Outputs:

- `negative_features_train.npy`
- `negative_features_eval.npy`
- `negative_feature_manifest.jsonl`
- `negative_feature_rejections.jsonl`
- `negative_feature_bank.json`

By default, manifest rows must be explicitly labeled as non-wake:
`negative`, `hard_negative`, `ambient_negative`, or `background`.
Use `--label-kind hard_negative` to build the adversarial near-miss bank.
Use `--allow-unlabeled-as <kind>` only for a dedicated negative-only corpus
that predates first-class labels; this is the escape hatch for old sessions,
not the normal path.

The negative builder reuses the same WAV format checks, SHA-256 verification,
end-aligned 2-second window, and ONNX feature extraction contract as the
positive feature-bank builder through `jasper/wake_training/feature_bank.py`.
Its summary includes selected duration hours by label kind and leg, because
false-accept analysis is measured in hours, not clip counts. New wake-training
data-prep scripts should reuse that shared module instead of importing private
helpers from another CLI script.

It does not generate positives, train, score, launch cloud jobs, register,
deploy, activate, or alter Pi runtime state.

---

## Wake training workdir prep

Laptop-side or training-host-side, offline. Consumes the feature-bank directory
from `scripts/build-wake-feature-bank.sh` and stages the JTS real-positive
features into the LiveKit/openWakeWord positive-feature naming convention.

```sh
bash scripts/prepare-wake-training-workdir.sh \
  logs/wake-corpus-export/20260609T120000Z/feature-bank
bash scripts/prepare-wake-training-workdir.sh logs/wake-features logs/wake-train \
  --target-phrase "hey jarvis" --model-name hey_jarvis_jts --positive-weight 3
```

Outputs:

- `feature_data/positive_features_train.npy`
- `feature_data/positive_features_test.npy`
- `real_positive_manifest.jsonl`
- `real_positive_injection.json`
- `training_workdir.json`
- `README.md`

The prep step verifies the feature manifest against the source arrays, maps the
JTS `eval` split to the trainer `test` split, and repeats train positives for
real-positive up-weighting while leaving eval/test rows unweighted. The default
weight is `3x`; every repeated row is recorded in `real_positive_manifest.jsonl`
with its source feature index and repeat index.

It does not generate synthetic positives, build negative/background banks,
train, export, evaluate, call LiveKit, launch cloud jobs, or alter Pi runtime
state.

---

## Wake LiveKit smoke workdir

Laptop-side or training-host-side, offline by default. Consumes the workdir from
`scripts/prepare-wake-training-workdir.sh` and creates the smallest complete
LiveKit-compatible model directory needed to smoke-test `train → export → eval`.

```sh
bash scripts/prepare-wake-livekit-smoke.sh logs/wake-train
bash scripts/prepare-wake-livekit-smoke.sh logs/wake-train logs/livekit-smoke \
  --steps 20 --model-type conv_attention --model-size tiny
```

Outputs:

- `livekit_smoke_config.yaml`
- `livekit_smoke.json`
- `README.md`
- `livekit-output/<model>/positive_features_train.npy`
- `livekit-output/<model>/positive_features_test.npy`
- `livekit-output/<model>/negative_features_train.npy`
- `livekit-output/<model>/negative_features_test.npy`

By default, the negative arrays are deterministic embedding-space placeholders.
That is sufficient to prove LiveKit mechanics but is **not** model-quality
evidence. To make the run meaningful, build real negative feature files with
`scripts/build-wake-negative-feature-bank.sh` and pass them with
`--negative-train-features` and `--negative-test-features`.

The tool does not call LiveKit unless the operator passes `--run-livekit`.
With that flag it runs:

```sh
livekit-wakeword train livekit_smoke_config.yaml
livekit-wakeword export livekit_smoke_config.yaml --format onnx
livekit-wakeword eval livekit_smoke_config.yaml
```

It does not generate synthetic positive audio, launch cloud jobs, register,
deploy, activate, or alter Pi runtime state.

---

## Wake training Phase 0 runner

Laptop-side or training-host-side, offline except for optional local
`livekit-wakeword` execution. Orchestrates the existing export, feature-bank,
real-positive injection, and LiveKit smoke tools into one evidence directory.

```sh
bash scripts/run-wake-training-phase0.sh logs/wake-phase0 \
  --positive-corpus-dir data/enrollment_positives \
  --negative-corpus-dir data/wake_negatives \
  --positive-leg chip_aec_150 \
  --negative-label-kind hard_negative

bash scripts/run-wake-training-phase0.sh logs/wake-phase0 \
  --positive-bundle-dir logs/positive-bundle \
  --negative-bundle-dir logs/negative-bundle \
  --run-livekit
```

Outputs:

- `phase0_run.json`
- `command_log.jsonl`
- `README.md`
- `positive-bundle/`, unless `--positive-bundle-dir` was supplied
- `positive-features/`
- `negative-bundle/`, unless `--negative-bundle-dir` was supplied
- `negative-features/`
- `training-workdir/`
- `livekit-phase0/`

By default, the runner requires `--negative-corpus-dir` or
`--negative-bundle-dir` so a Phase 0 result uses real negative/hard-negative
features. Pass `--allow-placeholder-negatives` only for a mechanics smoke test;
that path is not model-quality evidence.

The runner does not generate synthetic positive audio, launch cloud jobs,
register, deploy, activate, or alter Pi runtime state. It is the repeatable
operator path for "can we train/export/eval a tiny LiveKit-compatible ONNX
candidate from JTS corpus artifacts?" The next decision is made from the
resulting `livekit-phase0/livekit_smoke.json` and held-out JTS evaluation, not
from the runner itself.

---

## Wake-corpus quality analyzer

Laptop-side, offline. Deterministic first-pass signal-quality analysis of a
fetched wake corpus (the deliberate recorder's `enrollment_positives/` and its
per-leg WAVs). It does NOT score wake-word models — it surfaces *artifacts*
(clipping, transients/clicks, AGC pumping, spectral damage) and prioritizes
clips for human listening review.

```sh
bash scripts/analyze-wake-corpus-quality.sh data/enrollment_positives --latest
# → writes metrics.csv, cross_leg.csv, events.json, summary.md to an output dir
```

Outputs:
- `metrics.csv` — one row per WAV/leg: spectral, envelope, true-peak, clipping,
  transient, LPC-confirmed transient-damage, and flag metrics, plus a bounded
  `review_priority`.
- `cross_leg.csv` — sibling-leg deltas + FFT-alignment confidence + event
  coincidence (processed-minus-baseline).
- `events.json` — flagged per-leg events + the exact analyzer config used (a
  run is reproducible from it).
- `summary.md` — human triage, newest sessions first, sorted by review
  priority, with explicit "these are review hints, not auto-reject gates"
  caveats.

Transient damage is **two-stage confirmed** (a local-MAD sample-delta candidate
AND an LPC-residual outlier within a few ms), which suppresses the
plosive/fricative false-positive mode that plain sample-delta detectors hit.
Pure stdlib + numpy/scipy; covered by `tests/test_analyze_wake_corpus_quality.py`.

---

## AEC / bridge forensics

Investigative scripts for diagnosing AEC degradation, ref-path bugs,
sibilant tearing, etc. Not all are checked into the repo — some live
in `/tmp/` during a specific investigation and get promoted to
`scripts/` when stable.

| Tool | Status | Purpose |
|---|---|---|
| [`scripts/verify-ref-no-silence-bug.sh`](../scripts/verify-ref-no-silence-bug.sh) | in repo | Verifies the ref-path fixes from PRs #150 / #154 / #157 are active on the deployed build (resampler HF loss, silence fallback, drain-newest dup-frame bug). Run after any deploy that touched the bridge. |
| [`scripts/chip-aec-baseline-check.sh`](../scripts/chip-aec-baseline-check.sh) | in repo | Chip-AEC Option D gate only. After `chip-aec-setup.sh`, injects a chirp through `correction_substream`, captures repeated reference + bypassed 6-ch chip-mic WAVs, estimates a first residual `AUDIO_MGR_SYS_DELAY` candidate from the most repeatable chip channel, and supports `REF_DELAY_MS` when Pi-side reference delay is needed to fit inside the chip's narrow tuning range. |
| `scripts/xvf-interrogate.sh` | in repo | Deep XVF3800 diagnostic — USB descriptors, ALSA card state, all chip params, RMS levels. Tagged by chip iSerial. Run when the mic seems off and you want a full dump before changing anything. |
| `/tmp/analyze_aec_distortion.py` | **NOT in repo** | Per-clip peak / RMS / crest / tanh-zone occupancy / hard-clip count. Promote to `scripts/_analyze_aec_distortion.py` when stable. |
| `/tmp/analyze_tearing.py` | **NOT in repo** | NS musical noise / RS HF gating (`hf_CV`) / frame-boundary clicks / AGC pumping / HF aliasing detectors. Promote to `scripts/_analyze_tearing.py` when stable. |

If you write a forensic analyzer and use it more than twice, promote
it to `scripts/_analyze_*.py` so future sessions can find it.

---

## Model conversion (TFLite → ONNX)

[`scripts/convert-dtln-aec.sh`](../scripts/convert-dtln-aec.sh)
downloads breizhn/DTLN-aec's TFLite pretrained models (128 / 256
unit, both stages) and converts them to ONNX so they can run with
the Pi's `onnxruntime` (tflite-runtime has no Python 3.13 wheel —
see `install.sh` comment). Verified 2026-05-22: TFLite vs ONNX
outputs match within ~5×10⁻⁵ on random input. Uses `tf2onnx 1.17`;
`tflite2onnx 0.4.1` fails on the SQUARE op DTLN-aec uses for
spectrogram magnitudes.

If a future neural-audio model ships TFLite-only, this is the
template: run `tf2onnx --tflite` with `--opset 17`, sanity-check
against the original on random input, ship the ONNX.

---

## Test-track generation

[`scripts/make-wake-test-track.sh`](../scripts/make-wake-test-track.sh) +
[`scripts/_make_wake_test_track.py`](../scripts/_make_wake_test_track.py)
generate a TTS-based fixed audio track (N × phrase with fixed gaps).
The track gets AirDropped to a phone and played back during
`wake-rate-test.sh` for reproducible across-session comparisons.

If you find yourself wanting "the same N utterances every time" for a
test, use this. Output lands at `logs/wake-test-track/<slug>/<slug>.wav`
which `wake-rate-test.sh` finds automatically.

---

## Multi-room sync spike (P0)

The throwaway feasibility harness for multi-room grouping (stereo pair,
2.1 wireless sub). Answers the one gating unknown before any product
code: **does Snapcast hold inter-speaker sync on WiFi, at what buffer
depth + codec, and what does the FLAC encode cost on a 1 GB Pi?** Runs
entirely off the live JTS audio path; cleans up after itself.

| Tool | Methodology | When to use |
|---|---|---|
| [`scripts/multiroom-spike.sh`](../scripts/multiroom-spike.sh) | Laptop-side SSH harness (`--setup`/`--sweep`/`--record-chirp`/`--teardown`). Stands up a throwaway `snapserver` + `snapclient`s (leader + 2nd Pi + Pi Zero sub) reading a hand-fed FIFO, sweeps buffer `{150,300,500,800,1200}` ms × codec `{pcm,flac,opus}`, optional `--netem` WiFi stress (`wlan0` only). Results in `multiroom-spike/`. | Before P1: pick the buffer/codec that holds the p99<5 ms L/R bound on WiFi. |
| [`scripts/multiroom-spike-measure.py`](../scripts/multiroom-spike-measure.py) | Pure-stdlib analyzer. `software` (snapserver JSON-RPC latency spread), `acoustic` (single-mic cross-correlation of a click track — ground-truth inter-speaker offset), `summarize` (PASS/FAIL vs target + RAM/CPU + recommended cell). | Analyze a spike run; the acoustic mode is the authoritative comb-filtering check. |

**Safety note:** the spike plays a test track/music straight through a
throwaway `snapclient`, **bypassing** CamillaDSP's `volume_limit: 0.0`
ceiling, and its leader-side client can contend with `jasper-outputd`
for the DAC. Run it with the JTS audio daemons stopped (or on bring-up
hardware), and set a conservative volume before the first sweep. See
[`HANDOFF-multiroom.md`](HANDOFF-multiroom.md) §8.

---

## Pi-side diagnostics

Live Pi state without modifying anything:

| Tool | What it gives you |
|---|---|
| `sudo /opt/jasper/.venv/bin/jasper-doctor` | Codified BRINGUP smoke tests — first command to run when something's broken. Also re-checks output hardware observed-vs-active state plus presence/hashes for opaque runtime model files that JTS stages directly (required openWakeWord assets, the active wake model when registry-pinned, and configured DTLN ONNX stages when DTLN is enabled). |
| `curl -s http://jts.local:8780/state \| jq` | Cross-daemon JSON snapshot (voice / audio including `output_hardware` / AEC runtime profile / renderers / satellites). Fail-soft per section. |
| [`scripts/fetch-pi-logs.sh`](../scripts/fetch-pi-logs.sh) | Pulls journals + previous-boot OOM/watchdog/reboot forensics + configs + ALSA state to `./logs/`, redacting env-style secrets before write. Read the `*-latest.*` symlinks. |
| [`scripts/pi-run-diagnostic.sh`](../scripts/pi-run-diagnostic.sh) | Safe lane for ad-hoc Pi-side diagnostics: wraps a command in `systemd-run` with memory/runtime bounds and a positive `OOMScoreAdjust`. |
| [`scripts/pi-system-soak.sh`](../scripts/pi-system-soak.sh) | Convenience wrapper for a bounded `jasper-system-soak` run on the active Pi; writes a versioned JSON resource artifact. |
| [`scripts/tail-pi-logs.sh`](../scripts/tail-pi-logs.sh) | Live tail of all `jasper-*` units |
| [`scripts/jasper-trace.sh`](../scripts/jasper-trace.sh) | Filtered live tail showing only `event=` lines (duck transitions, source preempts, dial routing, wake/turn boundaries) |
| `ssh pi@jts.local sudo bash /home/pi/jts/scripts/pi-bundle.sh` | One-shot full diagnostic dump as a tarball |
| `jasper-correction-bundle inspect <session> --recompute` | Validate a copied room-correction bundle, summarize confidence/runtime evidence, and replay raw captures into derived curves |
| `jasper-correction-bundle export <session> --output <dir>` | Write REW-friendly `.frd` / `.txt` curves and impulse-response WAVs from a room-correction bundle |
| `jasper-active-speaker startup-template <preset.json> --playback-device <device> --output <file.yml>` | Write a muted/protected active-speaker startup template and run `camilladsp --check` when available. It does not load or apply the config. |
| `jasper-active-speaker path-audit --requirements` / `path-audit <evidence.json>` | List or evaluate the active-speaker audible-path safety checklist. Operator evidence can satisfy requirements but does not permit active config loading; `ok_to_load_active_config` stays false until future hardware-probe-backed evidence passes. |
| `jasper-active-speaker environment-probe [--config <file.yml>] [--json]` | Read ALSA playback devices and the current/provided CamillaDSP config/statefile shape without playback, reloads, or mutation. Blocks the load gate unless the config is an active startup candidate, `camilladsp --check` passes, and hardware-probe-backed path-safety evidence is provided. Also reports the read-only safe-playback environment block; audible authority lives in the separate `/sound/active-speaker/playback-readiness` + tone-backend gate. |
| `/sound/active-speaker/{environment,safe-playback,commissioning-rehearsal,channel-identity,calibration-level,tone-targets,arm,stop,playback-readiness,tone-plan,play-tone,floor-audio-result}` | Web active-speaker status/session/identity/readiness/level/plan/test surface. `environment`, `safe-playback`, `commissioning-rehearsal`, `channel-identity`, `calibration-level`, and `tone-targets` are read-only GETs; `arm`, `stop`, `channel-identity`, `calibration-level`, `playback-readiness`, `tone-plan`, `play-tone`, and `floor-audio-result` are CSRF-protected POSTs from `/sound/`. Arm records a safety session when the environment load gate passes; Stop is idempotent and resets quiet-start evidence; commissioning-rehearsal derives a no-audio durable-sequence packet from existing output/staging/startup/session evidence without storing wizard progress; channel-identity marks/clears operator-confirmed physical wiring evidence on the saved output topology; calibration-level persists the backend-owned test-signal level and `auto_step` applies at most one target-aware guided transition from saved topology, mic observation, driver protection, and same-target floor evidence; playback-readiness combines the saved target, channel identity, clock domain, active config/path safety, safe session, calibration-level bounds, Stop availability, and tone-backend status. Default `play-tone` renders a bounded artifact and records `audio_emitted: false`; audible tests require explicit lab env enablement (`JASPER_ACTIVE_SPEAKER_TONE_BACKEND=aplay`, `JASPER_ACTIVE_SPEAKER_ALLOW_AUDIO=1`, `JASPER_ACTIVE_SPEAKER_TEST_PCM=<pcm>`), passed topology readiness, loaded protected startup DSP, operator-confirmed same-target quiet-start evidence for raised tests, and driver-protection policy. Artifact-only results and unconfirmed audio do not unlock raised audio. No endpoint changes normal listening volume. |
| `rust/jasper-dual-dac-lab/target/release/jasper-dual-dac-lab probe` / `run` | Lab-only dual Apple USB-C DAC validator. `probe` is passive. `run` opens two serial-pinned direct `hw:` PCMs, writes silence first, caps level, and aborts both outputs on xrun/suspend/disconnect/delay divergence. Not installed as a product daemon. |

See [CLAUDE.md](../CLAUDE.md) "Debugging — fetch evidence before
guessing" for the canonical recipes.

---

## Dual Apple DAC lab runner

[`rust/jasper-dual-dac-lab`](../rust/jasper-dual-dac-lab) is a
lab-only Rust binary for the experimental "one Apple USB-C DAC per
speaker" topology. It is intentionally outside the product output path:
no systemd unit, no install hook, and no CamillaDSP/ALSA aggregate
device.

Use it only from the Pi checkout after an explicit build:

```sh
cd /home/pi/jts/rust/jasper-dual-dac-lab
cargo build --release --locked
./target/release/jasper-dual-dac-lab probe
```

The `run` command is sound-capable and must follow
[`dual-apple-dac-lab.md`](dual-apple-dac-lab.md): product audio owners
stopped, serial-pinned Apple PCMs, dummy loads or capture inputs, no
tweeters, explicit stop path, low level, and an evidence directory for
stdout JSONL, ALSA/USB descriptors, kernel logs, and capture WAVs. The
2026-06-03 evidence bundle shows a clean 15-minute low-level non-silence
software stability pass and a Scarlett common-clock drift pass for one
analog channel from each DAC. Right-channel identity, replug/reboot
repeatability, and product-stack startup/reload safety remain unproven.

---

## System soak artifacts

Use `jasper-system-soak` when the question is whole-system resource
behavior over time: idle memory growth, CPU hot spots, service restart
changes, outputd/fanin/voice STATUS drift, or journal volume. It is a
diagnostic artifact generator, not a daemon and not part of normal
production polling.

From the laptop, prefer the bounded wrapper:

```sh
bash scripts/pi-system-soak.sh --duration 30m --profile idle
bash scripts/pi-system-soak.sh --duration 30m --profile realistic --include-pss
```

The wrapper runs `/opt/jasper/.venv/bin/jasper-system-soak` through
[`scripts/pi-run-diagnostic.sh`](../scripts/pi-run-diagnostic.sh), so
systemd applies the usual diagnostic bounds (`MemoryHigh`,
`MemoryMax`, `MemorySwapMax=0`, `RuntimeMaxSec`, positive
`OOMScoreAdjust`). The command writes JSON under
`/var/lib/jasper/diagnostics/system-soak/` by default and prints the
artifact path.

Artifact contract, schema v1:

- `samples[]`: timestamped rows with tracked unit systemd state
  (`ActiveState`, `SubState`, `NRestarts`, `MainPID`, tasks,
  `MemoryCurrent`, `CPUUsageNSec` delta-derived CPU%), cgroup
  `cpu.stat`, `memory.events`, PSI when available, and outputd/fanin/
  mux/voice STATUS snapshots.
- `journal`: count/byte summary by unit and priority for the soak
  window. It intentionally does **not** store raw message text, which
  keeps routine resource artifacts out of the log-redaction business.
- `--include-pss`: optional sparse `/proc/<pid>/smaps_rollup` sums for
  better memory attribution. Use it for leak suspicion; leave it off
  for long baseline runs unless you need PSS.

Do not turn soak sampling into `/state` or `/system/snapshot`. The
dashboard gets cheap service truth; soak gets lab-grade history.

---

## Voice-eval (paid LLM tests)

[`tests/voice_eval/`](../tests/voice_eval/) runs end-to-end scenarios
against the **live** real-time speech-to-speech LLM provider —
**costs money per run** (~$0.075 Gemini / $0.15 Grok / $0.60 OpenAI
per scenario @ pass^3). Tests assistant *behavior* (does it call
the right tool, give a sensible answer), not wake accuracy or audio
quality.

Read [`tests/voice_eval/README.md`](../tests/voice_eval/README.md)
and [CLAUDE.md](../CLAUDE.md) "Voice-eval cost discipline" **before
running anything**. Never wrap `harness.ask()` in retry loops; never
auto-rerun on flake; announce cost before each invocation.

If your question is about audio quality or wake-word detection,
voice-eval is the wrong tool — use the offline scorers instead.

---

## Capture: alternative sources

Non-bridge captures, for completeness:

| Tool | Source | Use |
|---|---|---|
| [`scripts/capture-chip-mic.sh`](../scripts/capture-chip-mic.sh) | XVF3800 processed conference channel via `arecord` | Quick single-stream mic recording for SNR comparison; does NOT use the bridge |
| [`scripts/capture-satellite-amoled.sh`](../scripts/capture-satellite-amoled.sh) | AMOLED satellite ESP32 via USB-CDC | Validating satellite mic firmware; compares against the chip mic |

---

## Guard & contract test patterns

Static, hardware-free tests that pin the *names and shapes* crossing a
language or process boundary, in the grep-pin style established by
[`tests/test_outputd_wiring.py`](../tests/test_outputd_wiring.py).
Every consumer on these seams is fail-soft (a renamed key degrades to
null / a blank card / a silently-ignored env var), so drift never
throws at runtime — these guards make it a loud test failure naming
both sides. Before adding a new one, check this catalog; extend the
matching test module rather than starting a parallel one.

| Seam | Guard |
|---|---|
| fan-in `STATUS` JSON (Rust emitter ↔ Python consumers: doctor, airplay health, correction integrity) | `test_fanin_status_keys_match_python_consumers` in [`tests/test_wire_contracts.py`](../tests/test_wire_contracts.py) |
| outputd `STATUS` JSON (Rust emitter ↔ audio validation + doctor) | `test_outputd_status_keys_match_python_consumers` (same module) |
| fan-in control-UDS command vocabulary (`STATUS`/`AUTO`/`NONE`/`SELECT`, mux ↔ state.rs) | `test_fanin_control_command_vocabulary_matches_mux` (same module) |
| control-socket path literals (Rust defaults / systemd env / every Python consumer) | `test_control_socket_paths_agree_across_processes` (same module) |
| `JASPER_OUTPUTD_*` / `JASPER_FANIN_*` env names (bash reconcilers, units, install.sh, .env.example ↔ Rust `from_env`) — silent no-op knob detector, with a documented-exceptions list for staged vars | `test_outputd_fanin_env_names_are_read_by_rust_or_excepted` + `test_env_contract_exceptions_stay_accurate` (same module) |
| `/system/snapshot` payload ↔ dashboard ES modules (`snap.*`, `metrics.current.*`, airplay-card nested keys) | `test_dashboard_snapshot_top_level_keys_exist_in_server_payload` + `test_dashboard_metrics_current_keys_exist_in_sampler` + `test_dashboard_airplay_card_keys_exist_in_health_sampler` (same module) |
| `docs/doc-map.toml` code globs match ≥1 tracked file (stale-glob → silently un-routed docs; `safety = "design-only"` entries are exempt — anticipatory globs are their point) | `test_doc_map_code_globs_match_at_least_one_tracked_file` in [`tests/test_docs_impact.py`](../tests/test_docs_impact.py) |
| env files sourced by doctor mirror `jasper-voice.service` | [`tests/test_env_load_mirrors_unit.py`](../tests/test_env_load_mirrors_unit.py) |
| CamillaDSP config shape | [`tests/test_camilla_config_contract.py`](../tests/test_camilla_config_contract.py) |
| PEQ math JS ↔ Python | [PEQ graph math parity](#peq-graph-math-parity-js--python) above |

Pattern rules, learned the hard way:
- Pin **names/shapes, not implementations** — a guard that asserts
  internal call order belongs in the subsystem's own wiring test.
- Pin **both sides**: the producer must emit the name AND the consumer
  must still reference it, so a stale pin in the guard itself fails
  loudly instead of rotting.
- **Mutation-verify** a new guard before landing it: inject the drift
  (rename the key / add the bogus env var) and confirm the failure
  message names both files.
- Intentional one-sided names (staged features, consumer-side-only
  knobs) go in an **explicit exceptions table with a reason and an
  SSOT pointer**, plus a companion test that fails when the exception
  goes dead or goes live.

---

## When to add a new tool vs. extend an existing one

Default to extending. Add new only when:

- **Different audio source** the existing tools can't access (e.g. satellite via USB-CDC vs. the XVF via USB-UAC2 vs. a future Bluetooth mic).
- **Different output target audience** (e.g. CSV for spreadsheet review vs. one-shot text report — `score-baseline-wakeword.py` vs. `_offline_wake_count.py`).
- **Fundamentally different question** (test-track generation vs. wake counting are different questions, hence different tools).

A flag on an existing tool is almost always cheaper than a new file.
Especially watch for: re-implementing the systemd drop-in /
debug-record / bridge-stop dance — that's already in
`wake-rate-test.sh` and `capture-reference-condition.sh`. Don't
write a third version.

---

## Maintaining this doc

If you add a new tool, **add it here in the same PR**. If a tool gets
superseded or removed, strike it through here. If you do a forensic
investigation that uses a `/tmp/` script you'll likely want again,
promote it to `scripts/_analyze_*.py` AND add an entry above.

The doc is in the [README.md](../README.md) documentation map and
referenced from [CLAUDE.md](../CLAUDE.md) so an AI agent picking up
the codebase sees it before writing a duplicate.
