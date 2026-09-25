# Testing & measurement tools — index

> **Before writing a new test or measurement script, read this doc.**
> If your question overlaps with what one of these already answers,
> **extend or reuse it** rather than writing a parallel tool.

Test lanes, CI gates and branch protection are owned by
[.github/CONTRIBUTING.md](../.github/CONTRIBUTING.md); the test/comment/evidence defaults are
owned by [AGENTS.md](../AGENTS.md). This file is the tool catalog and does not
restate either.

---

## Quick lookup — by question

| If you want to … | Start with |
|---|---|
| Run the local or merge test lane | [.github/CONTRIBUTING.md](../.github/CONTRIBUTING.md) — `scripts/test-fast`, `scripts/test-merge` |
| Format/type-check/Clippy every Rust crate locally | [Rust formatting and Clippy cross-check](#rust-formatting-and-clippy-cross-check) |
| Understand a pytest-timeout failure, or bound a slow test | [Hang backstop (pytest-timeout)](#hang-backstop-pytest-timeout) |
| Check JS↔Python math parity (PEQ, level trims) | [JS ↔ Python parity checks](#js--python-parity-checks) |
| Run a lane from a worktree and be sure it exercised THAT copy | [DEEP-AUDIT-PLAYBOOK.md](DEEP-AUDIT-PLAYBOOK.md) item 4 — pin `PYTHONPATH` and confirm a known edit is visible; the venv's editable install hardcodes the main checkout's path, so an isolated worktree silently imports the LIVE tree |
| Pin a documented invariant with a test | [Guard & contract test patterns](#guard--contract-test-patterns) |
| Preview what install.sh would mutate, or check provenance | [Install and provenance](#install-and-provenance) |
| Check live Pi state (services, config, mic, renderer clock) | [Pi-side diagnostics](#pi-side-diagnostics) |
| Diagnose one correction run with synchronized UMIK audio | [Correction capture diagnostic](#correction-capture-diagnostic) |
| Characterize CPU/memory/journal behavior over time | [System soak artifacts](#system-soak-artifacts) |
| Test the assistant's *behavior* (does it call the right tool) | [Voice-eval (paid LLM tests)](#voice-eval-paid-llm-tests) |
| Count wake-word detections on captured audio offline | [Wake-word scoring (offline)](#wake-word-scoring-offline) |
| Pull production wake events + clips off the Pi | [Wake-event telemetry (production)](#wake-event-telemetry-production) |
| Audit the deliberate wake-corpus recorder output | [Wake-corpus audit (deliberate recordings)](#wake-corpus-audit-deliberate-recordings) |
| Analyze wake-corpus audio artifacts / quality | [Wake-corpus quality analyzer](#wake-corpus-quality-analyzer) |
| Export a corpus, build feature banks, smoke a LiveKit train | [Wake training pipeline (offline)](#wake-training-pipeline-offline) |
| Capture the AEC bridge's three streams | [Capture: 3-stream bridge captures](#capture-3-stream-bridge-captures) |
| Generate a fixed audio test track | [Test-track generation](#test-track-generation) |
| Diagnose a bridge / AEC issue forensically | [AEC / bridge forensics](#aec--bridge-forensics) |
| Measure `usb_low_latency_48k`'s real p95/p99 route latency | [Route-latency click/capture harness](#route-latency-clickcapture-harness) |
| Read the reverse `JTS Mic` emit→ALSA-write latency | [USB microphone export latency](#usb-microphone-export-latency) |
| Check the DSP realizes a linearization as the fit says, offline | [Offline emit loop](#offline-emit-loop) |
| Hold a field incident still in CI as a committed fixture | [Committed incident replay](#committed-incident-replay) |
| Find what a measurement change actually moved, at value level | [Reading comparator (pre/post value diff)](#reading-comparator-prepost-value-diff) |
| Detect, probe, or move the USB turntable | [USB turntable](#usb-turntable) |
| Pull a crossover-v2 round's evidence off the Pi | [Crossover-v2 round banking](#crossover-v2-round-banking) |
| Run, read, prescribe or apply a speaker-tuning round | [Tuning tools](#tuning-tools) |
| Fit or re-fit the cardioid rear branches of a `jts_rear_calibration` document | [`scripts/fit-rear-branches.py`](../scripts/fit-rear-branches.py) — usage, input shapes and conventions in `--help` |
| Predict a woofer pair without a room (near-field takes x a Boundary Lab cabinet solve), or fit the rear stage for the seat | [`scripts/cabinet-model/README.md`](../scripts/cabinet-model/README.md) — optional; needs Boundary Lab and a solved case from the CAD repo |
| Sweep for roadmap-dated phrasing that may have gone stale | [`scripts/tense-grep.sh`](../scripts/tense-grep.sh) — advisory, always exits 0; `--all` sweeps the whole repo |

---

## Rust formatting and Clippy cross-check

```sh
scripts/check-rust.sh
```

Local and CI source of truth for Rust formatting and Clippy. Reads the pinned
`RUST_TOOLCHAIN` from `.github/workflows/tests.yml` and runs `cargo fmt --all
-- --check` plus release/locked/all-target Clippy with warnings denied over
every crate in the CI Rust job; `jasper-host-clock` alone gets `--all-features`.

- Needs `pkg-config`; Linux needs real ALSA headers (`libasound2-dev` /
  `alsa-lib-devel`). On macOS it cross-targets Linux and stubs `alsa.pc` — the
  Linux target is load-bearing, since the `alsa` crate rejects a Darwin target
  before type-checking. The script fails before Cargo with the exact `rustup`
  command when a toolchain, component or cross target is missing.
- It does **not** execute Rust unit tests (those link against ALSA and stay a
  Linux/CI gate), but `--all-targets` does type-check `#[cfg(test)]` modules.

---

## Hang backstop (pytest-timeout)

Every test is bounded at **300 s** (`timeout` / `timeout_method` in
`[tool.pytest.ini_options]`, pinned by
`tests/test_dependency_groups.py::test_hang_backstop_is_configured_and_uses_the_signal_method`).

- **`timeout_method = "signal"` is load-bearing.** `thread` kills the whole
  pytest process and loses every later result; `signal` fails only the stuck
  test and survives `pytest-xdist`. It cannot interrupt a hang inside a C
  extension or at collection time, so CI's job-level `timeout-minutes` is the
  outer belt.
- **300 s is a hang-breaker, not a timing assertion** (~20x the slowest healthy
  test). Never tighten it to make a slow test fail — assert timing in the test.
- **Overrides:** `@pytest.mark.timeout(N)`; `N = 0` disables.
  `tests/voice_eval/conftest.py` raises the paid suite to `VOICE_EVAL_TIMEOUT_S`
  (900 s).
- For the `await <event>.wait()` shape, `tests/_async_wait.py::wait_signalled()`
  fails in ~10 s and names the producing task's exception; the bounded-wait row
  in [Guard & contract test patterns](#guard--contract-test-patterns) is the
  CI-time net.

---

## JS ↔ Python parity checks

Both are shared-fixture contracts between a browser module and its Python
twin. The `js` CI job runs them; run them locally when touching either side.

```sh
node scripts/check-peq-parity.mjs              # eq-math.js vs peq_response_fixture.json
node scripts/check-balance-trim-parity.mjs
```

Python sides: `tests/test_sound_peq_response.py` and
`tests/test_web_rooms_setup.py::test_balance_trim_python_matches_fixture`.

**`tests/js/` invocation patterns.** A file under `tests/js/` is run one of two
ways, never a third: (a) a harness the `js` CI job invokes directly by path
(`.github/workflows/tests.yml`'s `js` job: `sound_profile_harness.mjs`,
`speaker_setup_test.mjs`, `dialog_harness.mjs`, plus the parity
scripts above), or (b) a `*_test.mjs` file pytest discovers by glob, e.g.
`tests/test_crossover_wizard_js.py`'s
`_JS_DIR.glob("crossover_*_test.mjs")`. A new JS test is written as one of
these two.

**Node-on-runner reliance.** Some browser modules are behaviourally tested by a
Node harness invoked from pytest (`tests/test_dialog_helper.py`,
`tests/test_landing_page_html.py`) behind a `shutil.which("node")` skip-guard.
The `pytest` job has no `actions/setup-node` step — it relies on the runner image
shipping Node. If that wiring changes, these flip to **green-by-skip** and lose
their coverage silently; keep Node preinstalled or move the harnesses to a job
that installs it. `scripts/check-js-syntax.sh` only `node --check`s syntax.

---

## Guard & contract test patterns

Reusable exemplars for AGENTS.md's Tests default. All run in normal
hardware-free `pytest`. Mirror the closest one rather than inventing a new
guard style.

| If you want to … | Mirror |
|---|---|
| Ban a literal outside its owning constant, matched by VALUE not spelling | `tests/test_audio_measurement_boundary_ssot.py`, `tests/test_correction_substream_ssot.py` — AST `ast.Constant` values, so prose mentioning the literal is never a false positive |
| Freeze a convention's offenders and block new ones, or enforce one that lives only in a comment | `tests/test_atomic_io_conventions.py` (two-sided ratchet: a stale entry fails too, so the list only shrinks), `tests/test_shell_awk_environ_convention.py` (mutation-verified, names file:line and the replacement) |
| Require every call site of a dangerous import to be preceded by its guard, or assert an import chain stays light | `tests/test_lazy_imports.py` (whole-tree AST discovery, a non-empty assertion so a broken scanner fails loudly, and a companion test that fails when an exclusion stops matching anything), `tests/test_web_wizard_import_chain.py` (subprocess import with the heavy module poisoned in `sys.modules`) |
| Enforce a convention across every handler of a class | `tests/test_web_wizard_event_audit.py` (every state-mutating wizard handler emits an `event=` line), `tests/test_web_wizard_conventions.py` (the CSRF chokepoint, route-check-before-guard ordering, and a shape-based ban on interpolation into generated inline `on<event>=` handlers) |
| Keep deploy/ artifacts and install.sh wiring in lockstep | `tests/test_deploy_wiring_guards.py` — two-sided orphan coverage, wizard-env `EnvironmentFile=` precedence, udev → unit, socket↔nginx port parity |
| Keep a registry and its call sites in set-equality | `tests/test_cue_registry_coverage.py` — cue registry ↔ `cues.play()` sites, both directions, no allowlist (AGENTS.md's no-silent-deafness rule) |
| Pin a Rust↔Python wire shape, command vocabulary, or env-knob readership | `tests/test_wire_contracts.py` — `STATUS` keys, the mux command vocabulary, socket paths, `JASPER_OUTPUTD_*`/`JASPER_FANIN_*` read-by-Rust with a two-sided exceptions list, dashboard payload keys |
| Stop a test flaking when the OS momentarily refuses a resource under load | **Retry the acquisition, narrowly and loudly** — `tests/test_wifi_guardian_script.py`, `tests/test_restart_broker.py`. Every instance owes a narrow classifier, bounded attempts with a final re-raise, one dedicated `UserWarning` subclass, and a way to tell the harness hiccup from a real failure wearing the same signature |
| Stop a loaded run running *out* of file descriptors | **Close what you open, and attribute the leak before fixing it.** `loop.stop()` frees nothing — copy [`supervisor_runtime.py`](../jasper/control/supervisor_runtime.py)'s `_host_thread` (the thread that owns the loop closes it in a `finally`). Pinned by `tests/test_lint_contracts.py`, which walks the **AST** because three text-based versions were each fooled by prose about the anti-pattern. A log line naming a resource is not evidence it ran out |
| Keep a concurrency test's coordination waits bounded | `tests/test_async_wait_contract.py` — repo-wide AST guard, two shrink-only ratchets (a bare `await <event>.wait()`, and a `wait_for(…)` bounded below the 1.0 s floor). Fix with `tests/_async_wait.py`'s `wait_signalled()`, which names the producing task's exception |

---

## Install and provenance

```sh
bash deploy/install.sh --dry-run          # or JASPER_INSTALL_DRY_RUN=1
python3 scripts/check-provenance.py
```

- `--dry-run` exits before the root check and renders `install.sh`'s
  `INSTALL_STEPS` table for the resolved profile — one `name: phrase` line per
  step the real run would execute, in execution order. It is a planning
  surface; host-specific no-op decisions still live inside each step.
- `check-provenance.py` validates [`deploy/provenance.toml`](../deploy/provenance.toml)
  against `deploy/install.sh`, Python direct-URL dependencies, and the
  wake/DTLN model registries. Run it when touching install/build downloads.

---

## Pi-side diagnostics

Live Pi state without modifying anything:

| Tool | What it gives you |
|---|---|
| `sudo /opt/jasper/.venv/bin/jasper-doctor` | Codified BRINGUP smoke tests — first command to run when something's broken. Also re-checks output-hardware observed-vs-active state and presence/hashes for staged runtime model files |
| `curl -s http://jts.local/system/diagnostics.json \| jq` | Dashboard doctor snapshot: the last root-fidelity `jasper-doctor --json` result, refreshed in the background so the page never blocks |
| `curl -s http://jts.local:8780/state \| jq` | The daemon's own in-process posture — voice, audio (incl. `output_hardware`), fanin/outputd/source_selection, resilience supervisors, cues, measurement, debug (ADR-0270). A health fact lives in `/system/diagnostics.json` or a doctor row instead |
| [`scripts/fetch-pi-logs.sh`](../scripts/fetch-pi-logs.sh) | Journals + previous-boot OOM/watchdog/reboot forensics + boot timelines + configs + ALSA state into `./logs/`, redacting env-style secrets before write. Read the `*-latest.*` symlinks and `log-noise-summary-latest.txt` |
| [`scripts/journal-review.sh`](../scripts/journal-review.sh) | Read-only journal-health digest run ON the Pi over `--since` (default `7 days ago`): disk usage/retention, per-unit restart counts, warning+ volume, top `event=` keys with a week-over-week delta, OOM/watchdog and repeated-message fingerprints. `--json`; bounded, always exits 0. Also runs weekly from `jasper-journal-review.timer` (writes only its state file) |
| [`scripts/pi-run-diagnostic.sh`](../scripts/pi-run-diagnostic.sh) | Safe lane for ad-hoc Pi-side diagnostics: wraps a command in `systemd-run` with `MemoryHigh`/`MemoryMax`/`MemorySwapMax=0`/`RuntimeMaxSec` and a positive `OOMScoreAdjust`. Laptop-side — it SSHes to `$PI_HOST` |
| [`scripts/tail-pi-logs.sh`](../scripts/tail-pi-logs.sh) | Live tail of all `jasper-*` units |
| [`scripts/jasper-trace.sh`](../scripts/jasper-trace.sh) | Filtered live tail of `event=` lines only (duck transitions, source preempts, volume routing, wake/turn boundaries) |
| [`scripts/airplay-latency-probe.sh`](../scripts/airplay-latency-probe.sh) | Read-only capture of the AirPlay latency budget + AP2 stream type a real sender negotiates, so you know whether a bonded leader's downstream delay fits. No config change, no restart |
| [`scripts/jasper-pipe-probe`](../scripts/jasper-pipe-probe) | Renderer clock-integrity instrument: `gen-wav`/`gen-click` write the probe WAVs, `capture` pulls outputd's post-DSP `:9891` reference tap and writes an `OUT.raw.json` manifest (tap geometry, reject tallies, `all_zero`, `START_MONOTONIC_NS`), `analyze` prints per-second dominant frequency / THD+N / phase-glitch count / pitch-offset ppm (a meter — always exits 0), and `latency` measures one lane's launch-to-tap delay for before/after only. **Exits 4** unless `/var/lib/jasper/build.txt` names this checkout's commit (`--allow-skew` overrides) and **exits 3** when the instrument was blind; an all-zero tap is reported, not failed |
| `ssh pi@jts.local sudo bash /home/pi/jts/scripts/pi-bundle.sh` | One-shot full diagnostic dump as a tarball |

Read-only `jasper-active-speaker` verbs (the audible commissioning verbs are the
[operator runbook](tuning-operator-runbook.md)'s):

| Verb | What it gives you |
|---|---|
| `startup-template <preset.json> --playback-device <dev> --output <f.yml>` | Write a muted/protected startup template and run `camilladsp --check`. Loads and applies nothing |
| `runtime-safe-graph [--write-statefile] [--json]` | Classify the saved topology against the current/staged graph and pick the only legal persisted outputd statefile target. Active/protected topologies park silent (exit 0) with no validated all-muted startup graph staged; one that fails its safety proof exits 1 |
| `path-audit --requirements` / `path-audit <evidence.json>` | List or evaluate the audible-path safety checklist. Operator evidence never permits active config loading — `ok_to_load_active_config` stays false without hardware-probe-backed evidence |
| `path-probe [--current-config <f.yml>] [--output <f>]` | No-audio path-safety evidence. **Omitting `--current-config` writes blocked evidence**, so the gate stays shut rather than passing without a rollback target |
| `environment-probe [--config <f.yml>] [--json]` | Read ALSA devices and the current/provided config shape, with no playback, reload or mutation |
| `commission-ramp status [--json]` | Read-only commission-load / ramp / per-driver floor state |

`baseline-reemit`'s **`--endpoint ring` is the FIRST step of the active-ring arm
and has no rollback**; `--out` is its preview. `--force` there re-stages the
all-muted anchor mid-commission and is refused by default, because that anchor
is what `commission-rollback` and `ack --outcome too_loud` reload.

The `/sound/active-speaker/…` web surface exposes read-only status GETs plus
CSRF-protected POSTs for design-draft, stop, calibration-level,
the `commission-*` verbs, summed validation and baseline apply. **No endpoint
changes normal listening volume**.

---

## Correction capture diagnostic

```sh
python3 scripts/capture-correction-diagnostic.py --speaker http://<speaker>.local [--ssh-host <host>] …
python3 scripts/analyze-correction-diagnostic.py <bundle>
```

Laptop-side observer for one browser/relay correction run. It starts no
measurement and changes no gain: it records synchronized UMIK blocks,
`/state`/crossover timelines, and (with `--ssh-host`) a bounded snapshot of the
speaker's persisted gain/DSP files. The SSH archive runs off the capture loop
with a 15 s timeout so a stalled Pi cannot stop mic draining. Raw room audio
stays under the gitignored `captures/` tree (`0700` dir, `0600` files).

The analyzer reports tone/sweep presence, clipping, callback errors, observed
speaker gain, and the target-window shortfall. `--state-only` bundles stay valid
speaker-state evidence but report that no raw mic analysis was possible. Pass
the actual tone frequency and policy thresholds to the *capture* command when
they differ from its defaults — they ride the manifest and the analyzer consumes
them rather than re-guessing. Canonical evidence-gathering recipes live in
[AGENTS.md](../AGENTS.md)'s Evidence-first default.

---

## System soak artifacts

```sh
bash scripts/pi-run-diagnostic.sh -- \
  /opt/jasper/.venv/bin/jasper-system-soak --duration 30m --profile idle
```

Whole-system resource behavior over time: idle memory growth, CPU hot spots,
service restart changes, outputd/fanin/voice STATUS drift, journal volume. A
diagnostic artifact generator, not a daemon. Writes JSON under
`/var/lib/jasper/diagnostics/system-soak/` and prints the artifact path.

Artifact contract, schema v1: `samples[]` (per-unit systemd state, cgroup
`cpu.stat`, `memory.events`, PSI where available, outputd/fanin/mux/voice
STATUS) and `journal` (count/byte summary by unit and priority — deliberately
**no raw message text**, which keeps the artifact out of the redaction
business). `--include-pss` adds sparse `smaps_rollup` sums; use it for leak
suspicion, leave it off for long baselines. Do not turn soak sampling into
`/state` or `/system/snapshot`.

---

## Voice-eval (paid LLM tests)

[`tests/voice_eval/`](../tests/voice_eval/) runs **paid** scenarios against
live voice providers and tools. It checks tool calls and answers through
simulated output. The harness owns its
[evidence limits](../tests/voice_eval/README.md).

Read its run rules and [AGENTS.md](../AGENTS.md)'s paid-tests non-negotiable
**before running anything**. Never wrap `harness.ask()` in a retry loop; never auto-rerun on
flake; state the scenario count, estimated cost and live tool side effects
before each invocation.

---

## Wake-word scoring (offline)

```sh
python3 scripts/_offline_wake_count.py <wav> [--json]
jasper-wake-score …        # batch, per-clip CSV + aggregate by leg/condition/split
```

`_offline_wake_count.py` scores one file per utterance with
`openwakeword.model.Model` at 1280-sample (80 ms @ 16 kHz) frames matching
production's WakeLoop: template cross-correlation locates each utterance, then
peak score / RMS / category (`detected` / `near_miss` / `weak_signal` /
`silent_miss`) is reported per utterance.

- **Thresholds 0.5 / 0.3 / 0.1** match production (`jasper/wake.py` default 0.5)
  and the wake-events DB near-miss floor (0.10). Do not invent new tiers.
- It imports `jasper` on the scoring path (openWakeWord import guard), so run it
  under `/opt/jasper/.venv/bin/python` on a speaker or the repo venv on a
  laptop. `--help` works without either.
- Wake shell wrappers resolve their interpreter in one order: an explicit
  `PYTHON` (authoritative, fails visibly if invalid), the invoking checkout's
  `.venv`, the main checkout's `.venv` when invoked from a linked worktree, then
  `python3` — anchored to the wrapper's checkout, never the working directory.

---

## Wake-event telemetry (production)

Production capture is [`jasper/wake_events.py`](../jasper/wake_events.py) —
SQLite at `/var/lib/jasper/wake-events/wake-events.sqlite3` plus per-event WAVs
(4 s pre + 2 s post wake fire, AEC ON and AEC OFF legs).

| Tool | Purpose |
|---|---|
| [`scripts/fetch-wake-events.sh`](../scripts/fetch-wake-events.sh) | Consistent SQLite snapshot + all WAVs to `./wake-events/<UTC-ts>/`, with `index.csv` / `index.tsv` |
| [`scripts/audit-wake-events.sh`](../scripts/audit-wake-events.sh) | WAV integrity + cross-leg parity (xcorr alignment) + DB column populated counts (wraps `scripts/_audit_wake_events.py`) |

**Production telemetry only.** Controlled-lab WAVs (from `wake-rate-test.sh` or
`capture-reference-condition.sh`) have a different schema and different
assumptions — score them offline instead of ingesting them here.

---

## Wake-corpus audit (deliberate recordings)

The recorder at `http://jts.local/wake-corpus/` writes the gold corpus under
`/var/lib/jasper/enrollment_positives/` with per-session JSON sidecars. After
rsyncing to `./data/enrollment_positives/`:

```sh
bash scripts/audit-wake-corpus.sh data/enrollment_positives --expect-raw0
```

- `--min-per-cell N` once a session's recording is complete (7 for Session A;
  2 for Session B's Jarvis held-out portion — hard negatives have a different
  target distribution and are reviewed separately).
- `--expect-leg <leg>` repeated for cheap-USB sessions (`ref`, `usb_raw`,
  `usb_webrtc`; `usb_dtln` only where USB DTLN was enabled). AEC3 sweep pilots
  discover their legs from `jasper/aec_sweep.py` and still accept legacy sweep
  legs so same-day recordings stay auditable.

It checks session metadata and `include_raw_mic_0` flags, missing legs,
condition × distance coverage, WAV existence/format (16 kHz mono int16)/
duration/RMS/peak, recorder `capture_health` (compromised fails, warning/unknown
is surfaced), the `audio_context` summary, and per-clip `selected_legs` drift.
It is the fast integrity gate — it reads no `wake-events.sqlite3` and scores no
models; for signal quality use the
[Wake-corpus quality analyzer](#wake-corpus-quality-analyzer).

---

## Wake-corpus quality analyzer

```sh
bash scripts/analyze-wake-corpus-quality.sh data/enrollment_positives --latest
```

Laptop-side, offline, deterministic. It does not score wake models — it surfaces
*artifacts* (clipping, transients/clicks, AGC pumping, spectral damage) and
prioritizes clips for human listening review. Outputs to an output dir:

- `metrics.csv` — one row per WAV/leg, plus a bounded `review_priority`.
- `cross_leg.csv` — sibling-leg deltas, FFT-alignment confidence, event
  coincidence.
- `events.json` — flagged events plus the exact analyzer config (a run is
  reproducible from it).
- `summary.md` — human triage, newest first, with explicit "review hints, not
  auto-reject gates" caveats.

Transient damage is **two-stage confirmed** (a local-MAD sample-delta candidate
AND an LPC-residual outlier within a few ms), which suppresses the
plosive/fricative false-positive mode.

---

## Wake training pipeline (offline)

Laptop- or training-host-side, offline. Each stage consumes the previous
stage's directory. None of them alters Pi runtime state, launches cloud jobs,
registers, deploys, or activates.

```sh
# 1. corpus -> training bundle (audio/<split>/… tree + bundle.json, manifest.jsonl,
#    manifest.csv, rejections.jsonl, SHA256SUMS)
bash scripts/export-wake-corpus-bundle.sh data/enrollment_positives [outdir] [--latest 3]

# 2. bundle -> positive feature bank (positive_features_{train,eval}.npy,
#    feature_manifest.jsonl, feature_rejections.jsonl, feature_bank.json)
bash scripts/build-wake-feature-bank.sh <bundle-dir> [outdir] [--leg chip_aec_150]

# 3. bundle -> negative feature bank (negative_features_*.npy + manifests)
bash scripts/build-wake-negative-feature-bank.sh <bundle-dir> [outdir] \
  [--label-kind hard_negative] [--allow-unlabeled-as ambient_negative]

# 4. feature bank -> LiveKit/openWakeWord positive-feature workdir
bash scripts/prepare-wake-training-workdir.sh <feature-bank-dir> [outdir] \
  [--target-phrase "hey jarvis"] [--model-name hey_jarvis_jts] [--positive-weight 3]

# 5. workdir -> smallest complete LiveKit model dir for a train/export/eval smoke
bash scripts/prepare-wake-livekit-smoke.sh <workdir> [outdir] \
  [--steps 20] [--model-type conv_attention] [--model-size tiny] [--run-livekit]

# or all of 1-5 into one evidence directory
bash scripts/run-wake-training-phase0.sh logs/wake-phase0 \
  --positive-corpus-dir data/enrollment_positives \
  --negative-corpus-dir data/wake_negatives \
  --positive-leg chip_aec_150 --negative-label-kind hard_negative
```

Constraints worth knowing:

- **The exporter keeps sibling legs from one spoken utterance in the same
  train/eval split**, preserves capture metadata, remaps Pi absolute WAV paths
  to the local copy, hashes every accepted WAV, and rejects malformed or
  compromised clips into `rejections.jsonl` instead of training on them. It does
  not resample, segment, score or train.
- **Feature banks** need `openwakeword==0.6.0`, `onnxruntime`, `numpy`, and
  staged `melspectrogram.onnx` / `embedding_model.onnx` (`--melspec-model` /
  `--embedding-model` outside the JTS runtime). They keep the bundle split as
  source of truth, end-align each WAV into a 2 s / 32,000-sample window, extract
  `(16, 96)` embeddings, and verify each WAV's SHA-256 first. Both banks share
  `wake_training/feature_bank.py` — reuse that module rather than importing
  private helpers from another CLI script.
- **Negative rows must be explicitly labeled** `negative`, `hard_negative`,
  `ambient_negative` or `background`; `--allow-unlabeled-as` is the escape hatch
  for pre-label corpora. The summary reports selected duration in **hours**,
  because false-accept analysis is measured in hours, not clip counts.
- **The workdir prep** maps the JTS `eval` split to the trainer `test` split and
  repeats train positives for up-weighting (default `3x`, every repeated row
  recorded with its source index) while leaving eval/test unweighted.
- **The LiveKit smoke's default negatives are deterministic placeholders** —
  enough to prove mechanics, **not** model-quality evidence. Pass real banks via
  `--negative-{train,test}-features`. It calls LiveKit only with `--run-livekit`
  (`train`, `export --format onnx`, `eval`).
- **The Phase 0 runner requires** `--negative-corpus-dir` or
  `--negative-bundle-dir`; `--allow-placeholder-negatives` is a mechanics smoke
  test only. The decision is made from `livekit-phase0/livekit_smoke.json` plus a
  held-out JTS evaluation, not from the runner.

---

## Capture: 3-stream bridge captures

Both use the AEC bridge's debug-record mode (`JASPER_AEC_DEBUG_RECORD_DIR`, see
[`jasper/cli/aec_bridge.py`](../jasper/cli/aec_bridge.py) `_aec_loop` — three
time-aligned WAVs: `mic_ch1` raw chip, `aec_output` post-AEC3, `ref` playback
reference), apply the same systemd drop-in override, and stop `jasper-voice`
during capture. Outputs are renamed `aec-off.wav` / `aec-on.wav` /
`reference.wav`.

| Tool | Methodology | Output | When |
|---|---|---|---|
| [`scripts/wake-rate-test.sh`](../scripts/wake-rate-test.sh) | Fixed track played from a phone; cross-correlation locates each utterance; per-utterance detection status | `logs/wake-rate/<session>/test-<N>/` | Reproducible cross-session A/B of bridge configs, AEC engines or wake models |
| [`scripts/capture-reference-condition.sh`](../scripts/capture-reference-condition.sh) | Live speech, one capture per stylistic condition (whisper-quiet, music-yell, …) | `reference-conditions/<condition>/` | Personalized baseline covering real speech variation. User-private, gitignored |

**They share the same orchestration mechanism.** A third "bridge capture"
script almost certainly wants to be a flag on one of these two.

---

## Capture: alternative sources

| Tool | Source | Use |
|---|---|---|
| [`scripts/capture-chip-mic.sh`](../scripts/capture-chip-mic.sh) | XVF3800 processed conference channel via `arecord` | Quick single-stream mic recording; does NOT use the bridge |

---

## Test-track generation

```sh
bash scripts/make-wake-test-track.sh <slug>
```

TTS-based fixed track (N × phrase, fixed gaps) for "the same N utterances every
time". Output lands at `logs/wake-test-track/<slug>/<slug>.wav`, which
`wake-rate-test.sh` finds automatically. Helper:
[`scripts/_make_wake_test_track.py`](../scripts/_make_wake_test_track.py).

---

## AEC / bridge forensics

| Tool | Purpose |
|---|---|
| [`scripts/verify-ref-no-silence-bug.sh`](../scripts/verify-ref-no-silence-bug.sh) | Verifies the ref-path fixes (resampler HF loss, silence fallback, drain-newest dup-frame) are active on the deployed build |
| [`scripts/xvf-interrogate.sh`](../scripts/xvf-interrogate.sh) | Deep XVF3800 dump — USB descriptors, ALSA card state, all chip params, RMS levels, tagged by chip iSerial |

If you write a forensic analyzer and use it more than twice, promote it to
`scripts/_analyze_*.py` and add a row here.

---

## Model conversion (TFLite → ONNX)

[`scripts/convert-dtln-aec.sh`](../scripts/convert-dtln-aec.sh) downloads
breizhn/DTLN-aec's TFLite models (128/256 unit, both stages) and converts them
to ONNX for the Pi's `onnxruntime` (tflite-runtime has no Python 3.13 wheel).
Uses `tf2onnx 1.17`; `tflite2onnx 0.4.1` fails on the SQUARE op DTLN-aec uses.

Template for any future TFLite-only model: `tf2onnx --tflite` with `--opset 17`,
sanity-check against the original on random input, ship the ONNX.

---

## Route-latency click/capture harness

`jasper-route-latency-harness` ([`jasper/cli/route_latency_harness.py`](../jasper/cli/route_latency_harness.py)
plus `jasper/route_latency/`) plays real impulses through the USB route and
reports what they measured. It grades nothing — latency is monitored live and
adapted at runtime, never certified
([ADR-0185](adr/0185-latency-is-monitored-and-adapted-never-certified.md)).

A host (Mac/Windows, no JTS software) plays a generated click-track WAV into the
JTS USB audio device. A default-off ingress tap inside `jasper-fanin`'s own
`hw:UAC2Gadget` DIRECT capture — armed over fan-in's control UDS (`TAP_ARM`,
`/run/jasper-fanin/impulse-tap.jsonl`) — timestamps each click as it lands in the
claiming route's capture stream, binding the measurement to route identity by
construction; the harness separately reads the AEC bridge's `raw0` leg on
localhost UDP `:9879` to detect the same clicks acoustically. Latency is the
tap→mic delta, and `t_tap` anchors at the Pi's UAC2 capture read, so host-side
buffering before ingress is excluded.

Invoke every CLI by its absolute venv path — under `sudo` the venv `bin/` is not
on `secure_path`.

```sh
# 1. generate (laptop or Pi, no daemon needed): quick >=200 impulses / >=5 min,
#    promotion >=1000 jittered / >=30 min
/opt/jasper/.venv/bin/jasper-route-latency-harness generate quick --out-dir /tmp/route-latency

# 2+3. on the Pi: capture and analyze in one shot, then play the WAV on the host
sudo /opt/jasper/.venv/bin/jasper-route-latency-harness run \
  /tmp/route-latency/quick-schedule.json --out-dir /tmp/route-latency

# or split them; `analyze` needs the tap JSONL named explicitly
sudo /opt/jasper/.venv/bin/jasper-route-latency-harness capture \
  /tmp/route-latency/quick-schedule.json --out-dir /tmp/route-latency
/opt/jasper/.venv/bin/jasper-route-latency-harness analyze \
  --tap-events /run/jasper-fanin/impulse-tap.jsonl \
  --mic-detections /tmp/route-latency/mic-detections.jsonl \
  --route-health-snapshot /tmp/route-latency/route-health-snapshot.json \
  --out-dir /tmp/route-latency
```

- **Play at a modest volume** — start very quiet and confirm by ear.
  CamillaDSP's `volume_limit` 0 dB ceiling is the hard floor either way
  (AGENTS.md non-negotiable 1). **Generate `promotion` on the laptop**: the
  track is ~415 MB and the 1 GB Pi is busy running the stack under test.
- **Route-health honesty.** `capture` snapshots the fan-in and outputd `STATUS`
  sockets before and after; `analyze` diffs them. **Any** nonzero change to a
  curated counter marks the window unclean (a negative delta means the daemon
  restarted mid-window), and incomplete telemetry is not a clean window. It
  gates nothing — read the deltas before trusting the numbers.
- **Mic source.** Default `udp:9879` needs an XVF3800 with 6-channel firmware
  and the bridge running; it fails loudly on a read timeout rather than hanging.
  `--mic alsa:<device>` is the fallback.
- **Clock discipline.** Both the Rust tap and the mic reader timestamp against
  `CLOCK_MONOTONIC` **freshly per packet/period**, never one stream-start
  anchor — the mic's USB clock drifts ~180 ms over 30 minutes at 100 ppm.
  **Pairing** is nearest-match in a bounded window; an ambiguous detection is
  rejected rather than guessed, and no samples file is emitted below the
  match-rate floor (default 90% of tap events).

---

## USB microphone export latency

`jasper-usbmic` measures its own `bridge_emit_to_alsa_write` age continuously and
publishes p50/p95/p99 in `/run/jasper-usbmic/status.json`. `jasper-doctor`'s
"USB microphone export" check reads that live number while a computer is
actively recording from `JTS Mic` and warns above 120 ms; it deliberately does
not judge a frozen idle ring. Nothing is certified
([ADR-0185](adr/0185-latency-is-monitored-and-adapted-never-certified.md)).

```sh
ssh pi@jts.local 'jq "{host_streaming, source_age_ms_p50, source_age_ms_p95, source_age_ms_p99}" /run/jasper-usbmic/status.json'
```

The scope is `bridge_emit_to_alsa_write` — **not** physical mic→host end-to-end
latency. XVF/PortAudio capture time, gadget fill, USB transport and the host
audio stack are separate terms.

---

## Offline emit loop

`jasper-active-speaker-emit-bench`
([`jasper/cli/active_speaker_emit_bench.py`](../jasper/cli/active_speaker_emit_bench.py),
library in [`jasper/active_speaker/bench/`](../jasper/active_speaker/bench/))
answers: **does the DSP realize a linearization the way the fit claims?** It
emits the preset twice through the real emitter (with and without the
linearization), renders both through the real pinned CamillaDSP binary as
file-to-file batch passes, and grades the difference against
`linearization_fit.complex_correction_response`. Everything the two configs
share cancels exactly, so nothing has to be modelled. It is the offline twin of
[`delta_probe.py`](../jasper/active_speaker/delta_probe.py), whose verdict
vocabulary and classifier it reuses.

The bench runs **on the speaker** (the binary's identity comes from the running
`jasper-camilla.service`; there is deliberately no `--binary` override), but you
invoke it from the laptop checkout — every path below is Pi-side.

```sh
bash scripts/pi-run-diagnostic.sh -- \
  /opt/jasper/.venv/bin/jasper-active-speaker-emit-bench \
    --linearization /var/tmp/fits.json \
    --playback-device "$(...)" \
    --out /var/tmp/emit-loop

# a longer sweep needs a higher ceiling
JTS_DIAG_MEMORY_HIGH=512M JTS_DIAG_MEMORY_MAX=768M bash scripts/pi-run-diagnostic.sh -- ...
```

- **Run it through the bounded runner, not bare.** The deconvolution and FFTs
  run in the CLI's own process: a production-length run measures 221–235 MiB
  peak RSS on a 1 GB Pi, against runner defaults (`MemoryHigh=256M`,
  `MemoryMax=384M`, `RuntimeMaxSec=10min`) that fit with little headroom. The
  dominant term scales with `--sweep-seconds`, so a longer sweep is OOM-killed
  by the cgroup — which looks exactly like a bench bug and is not one.
- `--linearization` is a JSON object of persisted per-role `LinearizationFit`
  records (`{role: {"filters": [...], ...}}`).
- **Exit codes are three-state:** `0` every gradeable branch matched and at
  least one was; `1` a graded branch did not match (the finding); `2` no verdict
  (refused, or nothing gradeable). A role the fit left alone reaches no verdict
  and is listed in the report's `unavailable_roles`, never counted either way.
- `--dry-run` runs the real emitter and derivation for both candidates and writes both
  configs without resolving a binary or rendering — a genuine preflight, so an
  emitter refusal, a non-allowlisted stage, a hard-clip limiter or an over-cap
  stimulus surfaces on the laptop. The bundle keeps both candidates' configs, **four
  `.raw` renders** (`<candidate>.{first,repeat}.raw` — the repeat's SHA-256 is the
  determinism receipt), the stimulus WAV, and `report.json`.
- **Read `band_max_error_db` per branch, not just the verdict.** The
  classifier's tolerances are calibrated for a microphone (1.5 dB below 10 kHz)
  and are generous offline: an exact render lands at 0.003–0.013 dB while the
  shelf-Q realization defect this exists for reads 1.705 dB.

Coverage: `tests/test_active_speaker_emit_bench_{derivation,compare,loop,cli}.py`
against [`tests/_fake_camilladsp.py`](../tests/_fake_camilladsp.py) — the
plumbing only; what the real binary does with an emitted biquad needs the
on-device run.

---

## Committed incident replay

A replay over a gitignored bank cannot guard anything in CI. When an incident's
defect is worth holding still, the shape that can is a **committed, minimized
fixture plus a characterization test** —
[`tests/fixtures/crossover_v2_incident_20260810/`](../tests/fixtures/crossover_v2_incident_20260810/)
with `tests/test_crossover_v2_incident_replay.py`, derived by
[`scripts/derive-crossover-incident-fixture.py`](../scripts/derive-crossover-incident-fixture.py),
and the alignment pair at `..._alignment_incident_20260816/`. Copy the shape:

* **Derive, never hand-copy — and name the one field you cannot.** The deriver's
  `--check` re-derives and diffs; it exits `2` when the bank is absent, because
  "I could not check" must not read as "the check passed", and it is an operator
  tool, never a CI gate. One field is exempt and hand-banked, spliced through
  verbatim so a re-run cannot delete a value nothing can rebuild.
* **Inject only what cannot be committed and name every stub** in the test's
  docstring; **label a characterization test as one**, since it pins behaviour
  that is WRONG and a green run means the incident still reproduces; **pin that
  inputs are USED, not merely accepted** (a predecessor guard spied a kwarg
  being *passed* and never *used*); and **mutation-verify every site you claim,
  naming the ones you cannot**.
* **When there is no bank, there is no re-derivation script** — the alignment
  fixture's sources are a live speaker's runtime state read over ssh, so each
  source's path and sha256 ride in `_provenance` instead. Where evidence must be
  substituted, earn it with an assertion about SCALE and **name the frame a
  banked number was computed in beside it**.

---

## Reading comparator (pre/post value diff)

```sh
PYTHONPATH=. .venv/bin/python scripts/compare-readings.py before.json after.json
```

Answers **what did this measurement change actually move?** — at value level,
across every reading a change touches, not just the ones a test pins. A lane can
only go red on one of the three places a reading lives: pins, a value absorbed
inside a `pytest.approx` tolerance, and prose homes that restate the same fact.

- **Producing the dumps is the caller's job, deliberately.** Which readings
  matter is a property of the change, so a measurement PR writes a throwaway
  dump script that drives the shipped code paths and serializes what it got. A
  dump maps a name to a bare value or to `{"value": …, "tolerance": …,
  "homes": [...]}`; `tolerance`/`homes` are read from the **after** dump so one
  file owns that metadata. Home paths resolve relative to cwd.
- **It does not replace the human-run corpus lane and it is not CI.** A
  tolerance-absorbed move is a reported class, not a pass, printed with the
  headroom the move left; every section prints its count even at zero, and a
  reading present in only one dump is named rather than dropped.
- **Prose-home hits are candidate sites for a human to judge.** Declared homes
  are scanned for renderings of the **before** value at 0–6 decimal places, one
  hit per line at the most specific match; a rendering that also renders the
  after value is skipped, and renderings under three characters or with their
  last significant digit rounded away are dropped before the scan.
- **A home it could not scan is its own reported class** (`HOMES NOT SCANNED`),
  because "not looked at" must not print the same as "looked at, clean".
- **Advisory: exits 0 whatever it found**, same contract as
  [`scripts/tense-grep.sh`](../scripts/tense-grep.sh). Exit `2` means it could
  not do the comparison at all. Coverage: `tests/test_reading_comparator.py`.

---

## USB turntable

[`jasper/turntable/jts_turntable.py`](../jasper/turntable/jts_turntable.py)
is the manual JTS3 adapter for the reusable `usb_turntable` controller package:
USB detection, identity/firmware probe, read-only offset query, left/right
relative movement, a confirm-gated zero redefinition, home, the vendor stop, and
guarded absolute measurement positions (which always home first).

**Every motion command is bounded to −45…+45° from the acoustic on-axis zero by
one constant with no runtime override.** `position` refuses an out-of-envelope
target outright; `left`/`right` refuse a move whose predicted endpoint would
leave the envelope unless it moves back toward zero. JTS owns the Pi power
preflight, the travel envelope, the measurement-rig guard, the `set-zero`
confirmation gate and a bounded one-retry recovery on the vendored transport's
`ProtocolError`; the upstream package owns discovery, framing and parsing.

Positioning is opt-in — no voice tool, no scheduler, no permanent daemon; a full
install adds only a bounded udev-triggered stop one-shot for the known
CH340-attached turntable. Read the adapter's
[`README.md`](../jasper/turntable/README.md) before use; coverage in
`tests/test_turntable.py`.

---

## Crossover-v2 round banking

`bash scripts/bank-crossover-round.sh <dest-dir> [session-id]` pulls one
round's evidence off the Pi into a new directory. The header of the script
states its usage and exit codes.

---

## Tuning tools

Each tuning CLI's `--help` owns its calls, flags and exit codes
([ADR-0204](adr/0204-per-tool-contracts-live-in-the-tool-the-operator-surface-is-tiered.md)).
The generated tool menu in the [runbook](tuning-operator-runbook.md) lists every
tuning CLI.

- `jasper-round` runs and trials measurement plans, banks, lists and shows
  rounds, and applies candidates. See `jasper-round --help`.
- `jasper-round-views` reads the evidence of a banked round. See
  `jasper-round-views --help`.
- `jasper-crossover-prescriber` prints contracts, judges and composes
  prescription documents, and reports where the speaker stands. See
  `jasper-crossover-prescriber --help`.
- `jasper-seat-level` finds the fader level that reads the target SPL at the
  seat and banks it as the session gain. See `jasper-seat-level --help`.
- `jasper-round run --mover arm` walks the lab turntable arm.
  [`arm_walk.py`](../jasper/active_speaker/arm_walk.py) owns its loop and
  safety checks.

The adapter runs as a subprocess at
`/opt/jasper/jasper/turntable/jts_turntable.py`. Root must be able to
detect it. A loop polls the session, checks power, moves, settles for 30 seconds,
and sends `position-ready`. The adapter's confirmation flags come from the
person's attestation; a power sign voids it.

---

## Adding and maintaining tools

Default to extending. Add a new tool only for **a different audio source** the
existing ones can't reach (phone relay vs. XVF over USB-UAC2 vs. a Bluetooth
remote mic), **a different output audience** (`jasper-wake-score`'s CSV vs.
`_offline_wake_count.py`'s one-shot report), or **a fundamentally different
question**. A flag on an existing tool is almost always cheaper than a new file
— especially watch for re-implementing the systemd drop-in / debug-record /
bridge-stop dance, which `wake-rate-test.sh` and `capture-reference-condition.sh`
already own.

Add a tool here in the same PR that adds it, and delete its row in the same PR
that deletes it — a row for a file that no longer exists is stale prose. Strike
a row through only when the tool still exists but is superseded. Promote a
`/tmp/` forensic script you'd want again to `scripts/_analyze_*.py` and add a
row. This doc is in the [documentation index](README.md).
