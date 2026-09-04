# Testing & measurement tools — index

> **Before writing a new test or measurement script, read this doc.**
> If your question overlaps with what one of these already answers,
> **extend or reuse it** rather than writing a parallel tool.

Test lanes, CI gates and branch protection are owned by
[CONTRIBUTING.md](../CONTRIBUTING.md); the test/comment/evidence defaults are
owned by [AGENTS.md](../AGENTS.md). This file is the tool catalog and does not
restate either.

---

## Quick lookup — by question

| If you want to … | Start with |
|---|---|
| Run the local or merge test lane | [CONTRIBUTING.md](../CONTRIBUTING.md) — `scripts/test-fast`, `scripts/test-merge` |
| Format/type-check/Clippy every Rust crate locally | [Rust formatting and Clippy cross-check](#rust-formatting-and-clippy-cross-check) |
| Understand a pytest-timeout failure, or bound a slow test | [Hang backstop (pytest-timeout)](#hang-backstop-pytest-timeout) |
| Check JS↔Python math parity (PEQ, level trims) | [JS ↔ Python parity checks](#js--python-parity-checks) |
| Run a lane from a worktree and be sure it exercised THAT copy | [DEEP-AUDIT-PLAYBOOK.md](DEEP-AUDIT-PLAYBOOK.md) item 4 — pin `PYTHONPATH` and confirm a known edit is visible; the venv's editable install hardcodes the main checkout's path, so an isolated worktree silently imports the LIVE tree |
| Pin a documented invariant with a test | [Guard & contract test patterns](#guard--contract-test-patterns) |
| Preview what install.sh would mutate, or check provenance | [Install, provenance, and release artifacts](#install-provenance-and-release-artifacts) |
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
| Measure inter-speaker sync error for multi-room on WiFi | [Multi-room sync spike (P0)](#multi-room-sync-spike-p0) |
| Check the DSP realizes a linearization as the fit says, offline | [Offline emit loop](#offline-emit-loop) |
| Hold a field incident still in CI as a committed fixture | [Committed incident replay](#committed-incident-replay) |
| Find what a measurement change actually moved, at value level | [Reading comparator (pre/post value diff)](#reading-comparator-prepost-value-diff) |
| Detect, probe, or move the experimental USB turntable | [USB turntable experiment](#usb-turntable-experiment) |
| Pull a crossover-v2 round's evidence off the Pi | [Crossover-v2 round banking](#crossover-v2-round-banking) |
| Emit a round packet, or judge a prescription against it | [Crossover prescriber harness](#crossover-prescriber-harness) |
| Decide if a bump is a driver defect, interference, or the room | [Feature-classification instrument](#feature-classification-instrument) |
| Grade a round's entry state, or compare seats and sessions | [Round-grading comparison views](#round-grading-comparison-views) |
| State a capture walk at stated angles | [Angle-walk door](#angle-walk-door) |
| Have the lab turntable arm WALK a live session | [Lab-arm walk harness](#lab-arm-walk-harness) |
| Run one whole crossover-v2 round from the laptop | [Crossover round runner](#crossover-round-runner) |
| See whether a speaker ships a MEASURED per-driver level | [Measured driver base trim](#measured-driver-base-trim) |
| Find the volume that measures a stated dB SPL at the seat | [Seat-SPL leveling](#seat-spl-leveling) |
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
node scripts/check-sensitivity-trim-parity.mjs # active-speaker-ui.js vs sensitivity_trim_fixture.json
node scripts/check-balance-trim-parity.mjs
```

Python sides: `tests/test_sound_peq_response.py` and
`tests/test_active_speaker_baseline_profile.py::test_sensitivity_trim_matches_shared_parity_fixture`.

**Node-on-runner reliance.** Some browser modules are behaviourally tested by a
Node harness invoked from pytest (`tests/test_relay_worker_js.py`,
`tests/test_capture_page_js.py`, `tests/test_dialog_helper.py`,
`tests/test_landing_page_html.py`) behind a `shutil.which("node")` skip-guard.
`pytest-matrix` has no `actions/setup-node` step — it relies on the runner image
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
| Ban a literal outside its owning constant, matched by VALUE not spelling | `tests/test_correction_boundary_ssot.py`, `tests/test_correction_substream_ssot.py` — AST `ast.Constant` values, so prose mentioning the literal is never a false positive |
| Freeze a convention's offenders and block new ones, or enforce one that lives only in a comment | `tests/test_atomic_io_conventions.py` (two-sided ratchet: a stale entry fails too, so the list only shrinks), `tests/test_shell_awk_environ_convention.py` (mutation-verified, names file:line and the replacement) |
| Require every call site of a dangerous import to be preceded by its guard, or assert an import chain stays light | `tests/test_lazy_imports.py` (whole-tree AST discovery, a non-empty assertion so a broken scanner fails loudly, and a companion test that fails when an exclusion stops matching anything), `tests/test_web_wizard_import_chain.py` (subprocess import with the heavy module poisoned in `sys.modules`) |
| Enforce a convention across every handler of a class | `tests/test_web_wizard_event_audit.py` (every state-mutating wizard handler emits an `event=` line), `tests/test_web_wizard_conventions.py` (the CSRF chokepoint, route-check-before-guard ordering, and a shape-based ban on interpolation into generated inline `on<event>=` handlers) |
| Keep deploy/ artifacts and install.sh wiring in lockstep | `tests/test_deploy_wiring_guards.py` — two-sided orphan coverage, wizard-env `EnvironmentFile=` precedence, udev → unit, socket↔nginx port parity |
| Keep a registry and its call sites in set-equality | `tests/test_cue_registry_coverage.py` — cue registry ↔ `cues.play()` sites, both directions, no allowlist (AGENTS.md's no-silent-deafness rule) |
| Pin a Rust↔Python wire shape, command vocabulary, or env-knob readership | `tests/test_wire_contracts.py` — `STATUS` keys, the mux command vocabulary, socket paths, `JASPER_OUTPUTD_*`/`JASPER_FANIN_*` read-by-Rust with a two-sided exceptions list, dashboard payload keys |
| Stop a test flaking when the OS momentarily refuses a resource under load | **Retry the acquisition, narrowly and loudly** — `tests/test_wifi_guardian_script.py`, `tests/test_restart_broker.py`. Every instance owes a narrow classifier, bounded attempts with a final re-raise, one dedicated `UserWarning` subclass, and a way to tell the harness hiccup from a real failure wearing the same signature |
| Stop a loaded run running *out* of file descriptors | **Close what you open, and attribute the leak before fixing it.** `loop.stop()` frees nothing — copy [`supervisor_runtime.py`](../jasper/control/supervisor_runtime.py)'s `build_asyncio_thread` (the thread that owns the loop closes it in a `finally`). Pinned by `tests/test_lint_contracts.py`, which walks the **AST** because three text-based versions were each fooled by prose about the anti-pattern. A log line naming a resource is not evidence it ran out |
| Keep a concurrency test's coordination waits bounded | `tests/test_async_wait_contract.py` — repo-wide AST guard, two shrink-only ratchets (a bare `await <event>.wait()`, and a `wait_for(…)` bounded below the 1.0 s floor). Fix with `tests/_async_wait.py`'s `wait_signalled()`, which names the producing task's exception |
| Prove a long-lived daemon loop answers `cancel()` | `tests/test_mux.py::test_run_answers_cancellation_racing_a_wake_alert` — resolve the awaited event and `task.cancel()` with **no intervening `await`**. Prefer `async with asyncio.timeout(...)` in a loop whose only exit is cancellation, and walk the loop's awaited chains rather than grepping for `wait_for` |

---

## Install, provenance, and release artifacts

```sh
bash deploy/install.sh --dry-run          # or JASPER_INSTALL_DRY_RUN=1
python3 scripts/check-provenance.py
python3 scripts/build-first-party-arm64-release.py
python3 scripts/verify-first-party-arm64-release.py \
  dist/first-party-arm64/jts-first-party-runtime-<version>
pytest -q tests/test_first_party_arm64_release.py
```

- `--dry-run` exits before the root check and lists the major install surfaces
  (apt groups, downloads/source builds, runtime writes, env migrations,
  boot/config writes, systemd actions, restarts, post-install checks). It is a
  planning surface; host-specific no-op decisions still live in `install.sh`.
- `check-provenance.py` validates [`deploy/provenance.toml`](../deploy/provenance.toml)
  against `deploy/install.sh`, Python direct-URL dependencies, and the
  wake/DTLN model registries. Run it when touching install/build downloads.
- Pass `--expected-source-sha <full-sha>` to the ARM64 verifier when validating
  a bundle for install.

---

## Pi-side diagnostics

Live Pi state without modifying anything:

| Tool | What it gives you |
|---|---|
| `sudo /opt/jasper/.venv/bin/jasper-doctor` | Codified BRINGUP smoke tests — first command to run when something's broken. Also re-checks output-hardware observed-vs-active state and presence/hashes for staged runtime model files |
| `curl -s http://jts.local/system/diagnostics.json \| jq` | Dashboard doctor snapshot: the last root-fidelity `jasper-doctor --json` result, refreshed in the background so the page never blocks |
| `curl -s http://jts.local:8780/state \| jq` | Cross-daemon JSON snapshot (voice / audio incl. `output_hardware` / AEC profile / renderers). Fail-soft per section |
| [`scripts/fetch-pi-logs.sh`](../scripts/fetch-pi-logs.sh) | Journals + previous-boot OOM/watchdog/reboot forensics + boot timelines + configs + ALSA state into `./logs/`, redacting env-style secrets before write. Read the `*-latest.*` symlinks and `log-noise-summary-latest.txt` |
| [`scripts/journal-review.sh`](../scripts/journal-review.sh) | Read-only journal-health digest run ON the Pi over `--since` (default `7 days ago`): disk usage/retention, per-unit restart counts, warning+ volume, top `event=` keys with a week-over-week delta, OOM/watchdog and repeated-message fingerprints. `--json`; bounded, always exits 0 |
| [`scripts/pi-run-diagnostic.sh`](../scripts/pi-run-diagnostic.sh) | Safe lane for ad-hoc Pi-side diagnostics: wraps a command in `systemd-run` with `MemoryHigh`/`MemoryMax`/`MemorySwapMax=0`/`RuntimeMaxSec` and a positive `OOMScoreAdjust`. Laptop-side — it SSHes to `$PI_HOST` |
| [`scripts/tail-pi-logs.sh`](../scripts/tail-pi-logs.sh) | Live tail of all `jasper-*` units |
| [`scripts/jasper-trace.sh`](../scripts/jasper-trace.sh) | Filtered live tail of `event=` lines only (duck transitions, source preempts, volume routing, wake/turn boundaries) |
| [`scripts/airplay-latency-probe.sh`](../scripts/airplay-latency-probe.sh) | Read-only capture of the AirPlay latency budget + AP2 stream type a real sender negotiates, so you know whether a bonded leader's downstream delay fits. No config change, no restart |
| [`scripts/jasper-pipe-probe`](../scripts/jasper-pipe-probe) | Renderer clock-integrity instrument: `gen-wav`/`gen-click` write the probe WAVs, `capture` pulls outputd's post-DSP `:9891` reference tap and writes an `OUT.raw.json` manifest (tap geometry, reject tallies, `all_zero`, `START_MONOTONIC_NS`), `analyze` prints per-second dominant frequency / THD+N / phase-glitch count / pitch-offset ppm (a meter — always exits 0), and `latency` measures one lane's launch-to-tap delay for before/after only. **Exits 4** unless `/var/lib/jasper/build.txt` names this checkout's commit (`--allow-skew` overrides) and **exits 3** when the instrument was blind; an all-zero tap is reported, not failed |
| `ssh pi@jts.local sudo bash /home/pi/jts/scripts/pi-bundle.sh` | One-shot full diagnostic dump as a tarball |
| `jasper-correction-bundle inspect <session> --recompute` | Validate a copied room-correction bundle, summarize its evidence, replay raw captures into derived curves |
| `jasper-correction-bundle export <session> --output <dir>` | REW-friendly `.frd` / `.txt` curves and impulse-response WAVs from a bundle |

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
CSRF-protected POSTs for design-draft, stop, channel-identity, calibration-level,
the `commission-*` verbs, summed validation and baseline apply. **No endpoint
changes normal listening volume**, and product outputd/CamillaDSP lanes are
forbidden as direct test writers — the banned list is
`FORBIDDEN_TEST_PCM_TOKENS` in
[`playback.py`](../jasper/active_speaker/playback.py); read the tuple, not a
restatement.

---

## Correction capture diagnostic

```sh
python3 scripts/capture-correction-diagnostic.py [--ssh-host <host>] …
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

[`tests/voice_eval/`](../tests/voice_eval/) runs end-to-end scenarios against
the **live** real-time speech-to-speech provider — **costs money per run**
(~$0.075 Gemini / $0.15 Grok / $0.60 OpenAI per scenario @ pass^3). It tests
assistant *behavior* (does it call the right tool, give a sensible answer), not
wake accuracy or audio quality.

Read [`tests/voice_eval/README.md`](../tests/voice_eval/README.md) and
[AGENTS.md](../AGENTS.md)'s paid-tests non-negotiable **before running
anything**. Never wrap `harness.ask()` in a retry loop; never auto-rerun on
flake; state the estimated cost before each invocation.

For audio quality or wake detection, use the offline scorers instead.

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
| [`scripts/aec-probe-latency.sh`](../scripts/aec-probe-latency.sh) | Chirp through `correction_substream`, capture outputd's speaker-reference UDP stream plus one XVF3800 channel, report reference-to-mic lag. `MIC_CHANNEL=0\|1` for chip ASR beams, `2` for the raw channel |
| [`scripts/aec-probe-xvf-ref-level.sh`](../scripts/aec-probe-xvf-ref-level.sh) | Chip-reference legality and level: L/R reference parity, clipping, chip-ref 16 kHz mono model, `AUDIO_MGR_REF_GAIN` estimate, per-channel RMS/correlation, XVF profile readbacks. See [`AEC-DIAG-06`](AEC-DIAG-06-xvf-format-level-profile.md) |
| [`scripts/aec-probe-timing.py`](../scripts/aec-probe-timing.py) | Timing probe for explicit reference sources (`outputd_udp`, `chip_ref_tee`). JSON/CSV/Markdown + short WAVs, labeled mic channels, outputd state snapshot, optional period/buffer profiles. See [`AEC-DIAG-03`](AEC-DIAG-03-timing-probe.md) |
| [`scripts/aec-probe-pinknoise.sh`](../scripts/aec-probe-pinknoise.sh) | Stationary pink noise as far-end; RMS attenuation per 5 s window. Pink noise is AEC3's best case, so the plateau is an upper bound — music-as-far-end is typically 5–10 dB worse. Stops shairport-sync and jasper-voice and restores them; plays loud-ish noise at the remote's current volume |
| [`scripts/xvf-interrogate.sh`](../scripts/xvf-interrogate.sh) | Deep XVF3800 dump — USB descriptors, ALSA card state, all chip params, RMS levels, tagged by chip iSerial |

**All four probes silently measure silence on ring-armed boxes** — they play
into `correction_substream` unconditionally (#2767).

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

## Multi-room sync spike (P0)

Throwaway feasibility harnesses, off the live JTS audio path, that clean up
after themselves.

| Tool | Methodology | When |
|---|---|---|
| [`scripts/multiroom-spike.sh`](../scripts/multiroom-spike.sh) | Laptop-side SSH harness (`--setup`/`--sweep`/`--record-chirp`/`--teardown`). Throwaway `snapserver` + `snapclient`s reading a hand-fed FIFO; sweeps buffer `{150,300,500,800,1200}` ms × codec `{pcm,flac,opus}`, optional `--netem` WiFi stress (`wlan0` only). Results in `multiroom-spike/` | Pick the buffer/codec that holds the p99 < 5 ms L/R bound on WiFi |
| [`scripts/multiroom-spike-measure.py`](../scripts/multiroom-spike-measure.py) | Pure-stdlib analyzer: `software` (snapserver JSON-RPC latency spread), `acoustic` (single-mic cross-correlation of a click track — the authoritative comb-filtering check), `summarize` (PASS/FAIL + RAM/CPU + recommended cell) | Analyze a spike run |
| [`scripts/s0-sync-bench.sh`](../scripts/s0-sync-bench.sh) | S0-sync de-risk gate: two throwaway **active** followers whose seam is `snapclient` → snd-aloop → crossover-only CamillaDSP → real DAC, 1 Hz broadband click, soaked for xrun/CPU/temp | The loopback re-entry and its `rate_adjust`-no-resampler clock seam |
| [`scripts/s0-sync-measure.py`](../scripts/s0-sync-measure.py) | `acoustic --wav` (autocorrelation → inter-speaker offset) and `soak --dir` (xrun totals, CPU/temp/throttle/Pss, p50/p95/p99/max raw and placement-detrended, resync jumps, combined PASS/FAIL) | Analyze an `s0-sync-bench.sh` run |

- **Safety — the P0 spike rows only:** `multiroom-spike.sh` plays through a
  throwaway `snapclient`, **bypassing** CamillaDSP's `volume_limit: 0.0`
  ceiling, and can contend with `jasper-outputd` for the DAC. Run it with the
  JTS audio daemons stopped (or on bring-up hardware) and set a conservative
  volume before the first sweep.
- **The S0 rows are not evidence about a ring-backed seam.** The bench
  characterises CamillaDSP nudging `PCM Rate Shift` on an snd-aloop capture
  device; a ring PCM is an ioplug and exposes no such control (#2768). It needs
  exclusive DAC ownership — `--up` stops the live stack on both Pis and
  `--teardown` restores it.

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
- `--dry-run` runs the real emitter and derivation for both arms and writes both
  configs without resolving a binary or rendering — a genuine preflight, so an
  emitter refusal, a non-allowlisted stage, a hard-clip limiter or an over-cap
  stimulus surfaces on the laptop. The bundle keeps both arms' configs, **four
  `.raw` renders** (`<arm>.{first,repeat}.raw` — the repeat's SHA-256 is the
  determinism receipt), the stimulus WAV, and `report.json`.
- **Read `band_max_error_db` per branch, not just the verdict.** The
  classifier's tolerances are calibrated for a microphone (1.5 dB below 10 kHz)
  and are generous offline: an exact render lands at 0.003–0.013 dB while the
  shelf-Q realization defect this exists for reads 1.705 dB.

> This section says "arm" for what invariant 9 calls a candidate; `arm` is also
> a dataclass field, an `arm=` log key and an asserted CLI string
> ([#2878](https://github.com/jaspercurry/JTS/issues/2878)).

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

## USB turntable experiment

[`experiments/usb-turntable/jts_turntable.py`](../experiments/usb-turntable/jts_turntable.py)
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
CH340-attached turntable. Read the experiment's
[`README.md`](../experiments/usb-turntable/README.md) before use; coverage in
`tests/test_usb_turntable_experiment.py`.

---

## Crossover-v2 round banking

```sh
bash scripts/bank-crossover-round.sh <dest-dir>
PI_HOST=jts3.local bash scripts/bank-crossover-round.sh <dest-dir>   # explicit target
```

Pulls one crossover-v2 round's evidence off the Pi into a directory you name.
`<dest-dir>` must not already exist non-empty. Targeting is
[`scripts/_lib.sh`](../scripts/_lib.sh)'s contract, stated in that file.

The first thing written, before any Pi round-trip, is `provenance.json` — the
host and user actually banked, a UTC timestamp, and this script's own commit —
so a banked tree names its own source even if every pull fails. It then pulls
the newest session bundle, the flow state, the design draft, the applied
baseline profile (`applied-profile.json` — what the speaker is PLAYING, which
the flow state cannot say), the repeat floor, the declared rig geometry, a
bounded journal window over the four units a round speaks through, and a power
re-check. Every pull is best-effort and independently reported.

Exit `0` pulled both the round bundle and the flow state; **`3` is
incomplete** — the round's own identity failed to pull, and a bank that cannot
say which round it banked is not a bank; `4` means `<dest-dir>` exists non-empty
and nothing was pulled. Any other non-zero code is bash's own. Neither refusal
ever deletes an already-pulled file.

- **Before comparing LEVELS across banked takes**, read each one's `provenance`
  block — the live fader, the held session volume, and which DSP graph the
  capture went through. A CHECK/MEASURE capture and a summed one report the same
  `config_path` while going through different transfer functions, so
  `graph.kind` is the field that tells them apart.
- **The speaker-side capture-dump ring is gone.** Every accepted capture's WAV
  and provenance now ride the session bundle's own banked take records; corpora
  banked before the removal keep their `dumps/` tree. To feed a
  `--dumps`-taking tool from a modern bank, rebuild the layout with
  `jasper-project-ring <bundle-dir> --out <ring>`.

---

## Crossover prescriber harness

`jasper-crossover-prescriber`
([`jasper/cli/crossover_prescriber.py`](../jasper/cli/crossover_prescriber.py))
is the read side and write side of "hand a round's evidence to a reader, take a
correction back", plus one verb that says where a speaker stands. It has no
model client, no API key and no network: **who calls the model is not the tool's
business**, which is what makes it work identically for a human, a laptop agent
over SSH, or a paste into a browser.

```sh
# where this speaker stands, before touching anything. Reads only.
jasper-crossover-prescriber status <bundle-dir> --state <flow-state.json> \
    --drivers <design-draft.json> --applied-profile <applied-profile.json>

# the read side: one round's banked evidence as one versioned JSON document
jasper-crossover-prescriber packet <bundle-dir> --state <flow-state.json> \
    --applied-profile <applied-profile.json> --out round.json

# the write side: judge what came back against the SAME file `packet` wrote
jasper-crossover-prescriber propose --packet round.json \
    --prescription answer.json --json

# the door: same gate, and the accepted answer is left for the next round
jasper-crossover-prescriber stage --packet round.json \
    --state <flow-state.json> --prescription answer.json
```

- **Emit the packet once, then judge against that file.** `--packet` reads an
  already-emitted packet AS the evidence instead of rebuilding one: a rebuild on
  a laptop resolves `--drivers`/`--applied-profile` against *that* machine and
  fingerprints differently, so a prescription written against the first is
  refused against the second. The rebuild inputs are refused beside it.
- `propose` is the **dry run of** `stage` — same gate, same document; only
  `stage` banks. `status` writes nothing, and reporting a staged prescription
  does **not** consume it.
- **Arguments.** `<bundle-dir>` is a commissioning bundle (the directory holding
  `info.json` beside `evidence/v1/artifacts/crossover_v2/<relay-session-id>/`).
  `--state` is the flow state, banked separately; without it the packet cannot
  carry per-claim verify verdicts and says so. `--applied-profile` is what the
  speaker is PLAYING; without it a per-driver prescription's displacement is
  reported `unknown` rather than guessed, and the flow state does **not** stand
  in for it. A per-driver document also needs `--drivers` for the declared bands
  (absent → `driver_passband_unavailable`); `feature_classification.json` is
  auto-discovered in the round's own artifact directory, and its absence leaves
  every filter on `prescription.unvouched_filters` rather than refusing.
- **Exit codes are the contract**, and **`1` and `2` are this tool's own way
  round** — the shared read-only rule the
  [operator runbook](tuning-operator-runbook.md)'s "Exit codes" owns has them
  the other way. `--json` prints the machine-readable `reason` plus its
  evidence.

`stage` writes one single-use document to
`/var/lib/jasper/active_speaker_crossover_v2_prescription.json` stamped with the
round the flow state says is next; the next round re-validates and consumes it,
staging twice is last-wins, and the overwrite is logged on **stderr** so stdout
stays the machine channel. What each door refuses, and what it discloses
instead, is the runbook's "The doors, and what they refuse". Owners:
[`evidence_packet.py`](../jasper/active_speaker/crossover_v2/evidence_packet.py),
[`blend_prescription.py`](../jasper/active_speaker/crossover_v2/blend_prescription.py),
[`driver_prescription.py`](../jasper/active_speaker/crossover_v2/driver_prescription.py)
(each owning its class's response format *and* its gate, so the instructions and
the bar cannot describe different shapes), and
[`prescription_spool.py`](../jasper/active_speaker/crossover_v2/prescription_spool.py).

### The other two prescriptions do NOT come through this door (#2773)

Four things can be prescribed for a round, arriving by **two entry surfaces with
two severities**:

| prescription | entry surface | judged | a refusal costs |
|---|---|---|---|
| blend (`jts_crossover_blend_prescription`) — the SUMMED blend region | `jasper-crossover-prescriber stage` | at `stage` | the staging — the round runs unprescribed |
| driver (`jts_crossover_driver_prescription`) — ONE driver's own band | `jasper-crossover-prescriber stage` | at `stage` | the staging — the round runs unprescribed |
| alignment (`alignment_prescription`) — delay, optionally polarity | request-body key on `POST /crossover/v2/session` | at session open | **the whole session**, at the tap |
| topology (`topology_prescription`) — corner and order | request-body key on `POST /crossover/v2/session` (laptop: `run-crossover-round.py --topology-prescription`) | at session open | **the whole session**, at the tap |

The severity split is deliberate. A staged prescription is *an instruction the
next round may follow*, and a round that cannot follow it still measures
something real; a request-time prescription is *what the round IS*, so running a
silently different candidate is worse than running none. The first pair fails
soft; the second refuses the session before any evidence store, relay
registration or capture happens. None of the four is ever inherited from a
lapsed session's durable state, and a document names its own `kind` — there is
no `--class` flag and no inference from shape.

Alignment is bounded as a **bounded excursion** from a declared, measured basis
(at most half a period at the corner). Topology is bounded by **admissibility**
instead — an excursion bound from the incumbent would refuse a tournament by
construction — so it checks both drivers' hard excitation edges plus the
protected role's **published**
`recommended_highpass_slope_db_per_octave`, absent on an ordinary datasheet and
on any profile saved before that field existed. **Nothing downstream enforces a
slope above 12 dB/octave**, so a published condition unchecked here is unchecked
anywhere. A topology pin also **closes the Fc search** (`fc_selection` is ABSENT
from every round's record — absent rather than null, because null would read as
a comparison that ran) and carries an **authority caveat**
(`operator_pinned_no_measured_ranking`), since no shipped path ranks one
topology or corner against another. **Measuring a pinned candidate and adopting
it are two acts**: applying still needs the saved crossover declaration to name
that corner and order first. Owners:
[`alignment_prescription.py`](../jasper/active_speaker/crossover_v2/alignment_prescription.py),
[`topology_prescription.py`](../jasper/active_speaker/crossover_v2/topology_prescription.py).

---

## Feature-classification instrument

`jasper-classify-features`
([`jasper/cli/classify_features.py`](../jasper/cli/classify_features.py) over
[`feature_classifier.py`](../jasper/active_speaker/crossover_v2/feature_classifier.py))
answers the question a magnitude curve cannot: is that bump a **minimum-phase
driver defect** (a filter is at least the right kind of tool), a
**non-minimum-phase cancellation** (structurally the wrong one — a filter lowers
the direct sound and its delayed copy together), or the **room**? It runs
offline over captures a round already banked and files
`feature_classification.json` into that round's own artifact directory, where
the evidence packet reads it.

```sh
jasper-classify-features <bundle-dir> --dumps <capture-ring> [--json]
jasper-classify-features <bundle-dir> --dumps <ring> --walk-log logs/walk-1.jsonl
jasper-classify-features <bundle-dir> --dumps <ring> --at 1037 --at 4149
```

- `--dumps` is a banked capture ring, scoped to this round by the bundle's own
  `session_id`, so a ring holding several rounds needs no flag to be split
  correctly. A round banked today has no ring of its own — rebuild one with
  `jasper-project-ring <bundle-dir> --out <ring>`
  (see [Crossover-v2 round banking](#crossover-v2-round-banking)).
- `--walk-log` gives the timing test repeated angles from a turntable trail;
  `--at` classifies exactly those frequencies instead of detecting them.
- **Known answers are pushed through the identical pipeline first**, on the
  round's own IR: a minimum-phase peaking filter that must read flat, an
  all-pass that must recover its group delay, a quiet delayed copy that must
  ALSO read flat, and a loud one that must not. Failing one costs the PHASE
  class and nothing else — every row reads `egd_verdict: ambiguous` beside
  `controls_ok: false`, no row can reach `defect-*`, and `controls_disclosure`
  says so in words.
- **It refuses more often than it reports, and each refusal has a name:**
  `classification_lateral_capture_shape` (a `lateral` capture replays MEASURE
  one driver at a time and carries no summed response),
  `classification_round_shape_inadmissible`,
  `classification_captures_unreadable` (admissible shape, ring cannot hand it
  over — the remedy is the ring or the bank step, not a different round),
  `classification_no_admissible_captures`, `classification_program_missing`,
  `classification_no_features_detected`. Every one carries a `captures` table
  naming each take the ring listed with its admissibility reason.
- **Every capture shape it reads is horizontal**, and that is a fact about the
  instrument: a turntable swings at fixed height and radius and a position cloud
  is a floor plan, so a floor or ceiling bounce is invariant to every position
  it saw. It is disclosed once, in the evidence packet's `not_evaluated` block
  as `vertical_plane_response`. **Exit codes** are the shared stage-named rule
  the [operator runbook](tuning-operator-runbook.md)'s "Exit codes" owns.

Coverage is built on synthetic speakers whose answers are known before the
instrument runs: `tests/test_crossover_v2_feature_classifier.py`.

---

## Round-grading comparison views

`jasper-round-views` ([`jasper/cli/round_views/`](../jasper/cli/round_views/),
core in [`round_views.py`](../jasper/active_speaker/crossover_v2/round_views.py)):

```sh
# grade the state the round ENTERED on, from the write-once entry-baseline take,
# through the same flat-spec evaluator a round grades its own result with
jasper-round-views entry <round-dir>

# grade a round shipped AND frozen to a baseline's own reference level
jasper-round-views frozen <baseline-round-dir> <target-round-dir>

# every banked position plus the VERIFY pose on one comparable basis (each curve
# as its own deviation from its own median level)
jasper-round-views per-seat <round-dir>

# session-to-session spread of the pooled honest figures — the stop criterion
jasper-round-views repeat <round-dir> [<round-dir> ...]

# per-seat sign/magnitude testimony for every feature in the trusted sweep
jasper-round-views agreement <round-dir>
```

- **Input shapes.** Every subcommand reads either a *banked round directory*
  (what `scripts/bank-crossover-round.sh` produces) or a *live session bundle*
  still on the speaker, told apart by
  [`round_inputs`](../jasper/active_speaker/crossover_v2/round_inputs.py); a
  live bundle borrows the speaker's flow state only when that state names the
  same session.
- **This module performs no DSP of its own.** Positions and the graded spec come
  from `evidence_packet.build_crossover_evidence_packet`, grading from
  `flat_spec.evaluate_flat_spec` and the `flat_spec_views` building blocks, and
  the VERIFY pose is READ from `verify_priors.verify_measured` in the round's
  banked `state.json`, not re-derived. A round that banked no VERIFY curve gets
  a NAMED reason rather than an exception — `verify pose ABSENT (<reason>)`.
- **`entry` needs no cloud group, and that is what makes it reachable.** A cloud
  group is banked by VERIFY; the MEASURE stage banks the per-driver solos and
  the entry baseline and no cloud, so a stage-1 round has neither cloud
  positions nor a graded `spec` — and it is the only round shape that produces
  an entry baseline. The four position-graded views raise on that themselves.
- **A `position_id` stopped naming a FIXED bearing across the 2026-08-24
  geometry ruling**, so `repeat` discloses bearings beside the numbers: every
  per-position row carries `degrees` and `bearings_agree`. Read
  `bearings_agree` as THREE-VALUED — `true` all recording rounds agreed,
  `false` they differ (read the spread as instrument noise at your peril), and
  **`null` means nothing was COMPARABLE** (fewer than two rounds recorded a
  bearing), which must never be read as "nothing disagreed". A cloud position
  and a LATERAL walk pose must **never be joined by index**: both count from the
  front of their own table, so a matching number is a coincidence.
- **Agreement's sign rule is a literal threshold** (`testify >= 3` and
  `dissent <= 1`), not scaled to seat count — below 3 seats the verdict is
  `common_mode: null`, a named not-evaluable state, never a fabricated pass.
  `--lo` defaults to the round's own `trusted_floor_hz`.
- Each subcommand writes its JSON beside a round by default —
  `jasper-round-views inventory <round-dir>` names those artifacts, and the
  subcommand that produces each one, from the CLI's own `ARTIFACT_BY_VIEW`, so
  they are not enumerated here. `repeat-floor` is the exception: its `--out` is
  required. `--out PATH` writes elsewhere, `--out -` to stdout. On failure it
  publishes the shared record and the shared stage-named exit code (the
  [operator runbook](tuning-operator-runbook.md)'s "Exit codes" owns both).

---

## Angle-walk door

`jasper-angle-capture` ([`jasper/cli/angle_capture.py`](../jasper/cli/angle_capture.py))
is how an operator states a capture walk —
`{per-driver | summed} × {angles} × {arm | human-guided}` — and sees exactly
what it resolves to before anything plays. Seam:
[`angle_capture.py`](../jasper/active_speaker/angle_capture.py).

```sh
# THE DOOR: a named program owns the geometry. Prints price, handoff URL, and
# how to tell the walk landed.
jasper-angle-capture stage --program baseline --size express
jasper-angle-capture plan  --program baseline --size full     # dry run
jasper-angle-capture stage --program spot --azimuth 22 --elevation 10

# THE CANDIDATE CYCLE: the same poses once per banked candidate, adjacent
jasper-angle-capture stage --program tournament --size full --candidates fp1,fp2

# THE OPERATOR ESCAPE HATCH: a free-form angle list no program names
jasper-angle-capture plan  --angles 0,7,-7,22,-22 --regime per_driver --mover human
jasper-angle-capture stage --angles 0,7,-7,22,-22 --regime per_driver --json

# R-1's reverse-null: the design-axis MEASURE capture with one branch flipped
jasper-angle-capture stage --angles 0 --polarity inverted --inverted-role tweeter

jasper-angle-capture withdraw
```

- `--program` and `--angles` are mutually exclusive and one is required.
  `--program` names a row of
  [`measurement_programs.py`](../jasper/active_speaker/measurement_programs.py),
  the only owner of the poses; `--regime` belongs to `--angles` alone. Both
  print the same receipt: `program`, `price` (`mic_moves` / `captures` /
  `ceiling_min`), `level`, `handoff_url`. `plan` is the **dry run of** `stage` —
  same constructors, same refusals — and names, per stop, the capture index,
  signed bearing, pose prompt, program, advance policy, and (for an arm) the
  `position_deg` the position gate waits for.
- **`level` is absolute dB SPL at the microphone**, resolved by
  [`seat_level_reference.py`](../jasper/active_speaker/seat_level_reference.py)
  from the banked seat-level anchor with the mic's parsed sensitivity and the
  preset's `max_commissioning_level_db_spl` as a hard ceiling. It never falls
  back to a relative number: `plan` prints the missing input, `stage` refuses
  with `seat_anchor_unusable`, `level_over_ceiling` or `preset_unavailable`.
- **Angles are whole degrees, negative LEFT and positive RIGHT facing the
  speaker, and nothing is coerced.** `7.5`, `0.4` and `+7 deg` are all refused:
  `int(0.4)` is `0`, so a truncating parser would silently turn a just-off-axis
  request into an on-axis capture. There is no second validator in the CLI.
- **The household's tape measure has one writer and no walk flags.**
  `jasper-declare-geometry set` stores the rig's `DeclaredGeometry`; `stage`
  only echoes it, banked beside the bundle as `declared-geometry.json`.
- **`--polarity` / `--inverted-role` are WALK-level, not per angle**, and
  nothing on the staging side judges the pair — its one gate is `MeasureSpec`,
  so `--polarity inverted` with no `--inverted-role` stages cleanly and refuses
  the next open. An inverted walk needs a WIRED session: only the wired source
  binds the engine MEASURE leg the flip rides.
- **Exit codes**: `0` accepted, `2` refused (bad angle, unknown regime or mover,
  session already running), `3` an accepted request could not be banked. `2`
  means fix the request; `3` means fix the filesystem.
- **What it does not do**: it runs no capture and opens no session. `stage`
  writes one single-use, last-wins document to
  `/var/lib/jasper/active_speaker_angle_capture_request.json`
  (`event=angle_capture.request_staged`; owner
  [`angle_capture_spool.py`](../jasper/active_speaker/angle_capture_spool.py)).
  The next session open consumes it and walks its stops as that session's
  lateral group, banking each pose's raw WAV plus a sidecar carrying
  `position_deg`, `offset_cm`, `at_mark`, `regime`, `lateral_consumer`. While a
  walk is staged the `microphone_check` tier chooser prices it (`staged_walk`)
  through a peek. A taken walk is EVIDENCE: its close adjudicates nothing.

**Eight take-time refusals, each of which REFUSES THE OPEN**
([ADR-0006](adr/0006-staged-walk-refuses-the-open.md)) rather than opening the
session in its ordinary shape: `walk_regime_unsupported`,
`walk_mover_mismatch`, `walk_over_mover_envelope` (arm ±45°, person ±80° —
normally refused at the door, so reaching the take means a hand-edited or
pre-bound document), `walk_over_capture_capacity`,
`walk_lateral_group_already_planned`, `walk_stop_no_longer_valid`,
`walk_polarity_not_accepted` and `walk_polarity_needs_wired`. The document is
consumed except on the spool's two unreadable arms, so a permissions mistake
cannot destroy the evidence of itself — the `consumed=` field says which
happened; do not assume it.

Read the journal, not the code: `event=correction.crossover_v2_angle_walk_taken`
/ `…_angle_walk_refused` / `…_lateral_walk_closed`. Coverage:
`tests/test_angle_capture_{trigger,seam,take}.py`,
`tests/test_crossover_v2_lateral_evidence.py`.

---

## Lab-arm walk harness

`jasper-angle-capture serve`
([`jasper/cli/angle_capture.py`](../jasper/cli/angle_capture.py), loop in
[`arm_walk.py`](../jasper/active_speaker/arm_walk.py)) is what actually WALKS a
live measurement session with the lab turntable arm: the session publishes
`relay.position_pending` and holds every begin until something POSTs
`/correction/crossover/v2/position-ready`, and the turntable adapter moves the
microphone.

Runs **on the speaker**, in the foreground, one run per walk:

```sh
# stage the walk first (angle-walk door, above), then start this, THEN open the
# session — the first poll is what checks a walk is still waiting.
sudo -u pi /opt/jasper/.venv/bin/jasper-angle-capture serve \
    --mover turntable --attest-rig-clear --hostname jts3.local \
    --expect-angles 7,-7,22,-22 \
    --trail /tmp/arm-walk.jsonl
```

- **`pi` is the identity, not a habit**: the adapter opens a serial port, and
  `pi` is what the shipped turntable unit runs as (`User=pi` plus `dialout`).
  **`--hostname` is required** and is the speaker's own name — it becomes the
  `Host:` header, without which the wizard's management-host guard refuses the
  loopback read (see `status_unreachable`).
- One turn of the loop: poll → power preflight → move → measured settle (30 s
  default) → `position-ready`. The adapter runs as a **subprocess** at
  `/opt/jasper/experiments/usb-turntable/jts_turntable.py` (`--tool` points at a
  checkout), never as an import.
- **Attestation, not a nanny.** The adapter wants two `--confirm-*` flags per
  move, which no unattended caller can honestly answer, so the operator answers
  once with `--attest-rig-clear`; a power sign is the one thing that voids it,
  because a power event is exactly when the saved zero may have stopped being
  the acoustic axis.

**Safety invariants, each pinned by a test in
[`tests/test_arm_walk.py`](../tests/test_arm_walk.py):**

| invariant | what it does |
|---|---|
| power before every WALK move | any current flag, since-boot flag, or unreadable reading voids the run — stop, park, `power_void`. The PARK's own move is deliberately not re-checked (the walk is often parking *because* of a power sign); it still passes the adapter's own preflight |
| ±45° clamp | belt-and-braces over the adapter's refusal, so an out-of-envelope target is NAMED here instead of surfacing as a subprocess failure |
| park and verify on every exit | clean finish, exception, or any of `PARK_ON_SIGNALS`. The check is a MAGNITUDE — the readback's sign is negated upstream |
| `set-zero` is unreachable | `power`, `position` and `offset` are the complete verb set |
| the settle never goes under 10 s | refused at configuration AND checked against the settle actually MEASURED |

**The stall NAME is the contract, not a number.** `serve` exits the shared
`0/1/3` every tool in the menu does; the loop's own distinct verdict per failure
class (`EXIT_NAMES`) rides out as the refusal record's `reason`, on stdout and
in the stderr sentence. Four are worth knowing before a run:

- **A walk ends when its session does** (clean, `session_stopped`,
  `session_failed`, read off the same poll's `relay.status`) — but a terminal
  status is only this walk's verdict once it has read its session LIVE. The
  wizard keeps ONE relay slot and keeps the FINISHED session's block in it, and
  a walk is launched BEFORE its session opens, so round N+1's first polls read
  round N's terminal block.
- **A release is a request, and `release_rejected` is the session saying no** —
  a `409`, `403`, `400` or a POST that never arrived all mean *no capture began*.
- **`status_unreachable` is almost always the wrong `--hostname`**; a single
  unreadable poll is absorbed, a whole `unreadable_ceiling_s` of them is its own
  named stall.
- **`--expect-angles` is how a walk that never runs is caught**: any unserved
  angle becomes `walk_not_taken`, and with no session yet in flight a walk must
  still be staged (`walk_not_staged`).

**A signal stops the walk once — and SIGHUP is one of them**, since a remote
walk is stopped by its ssh transport going away and Python's default for SIGHUP
is death with no unwinding. SIGTERM/SIGINT/SIGHUP becomes the `SystemExit` whose
unwind IS the park, and the handler disarms itself on that first fire so a
second signal cannot abandon the arm mid-park; signal endings exit
`128 + signum`. **Observability**: `event=arm_walk.*` (`pending`, `moved`,
`released`, `release_rejected`, `power_void`, `stuck`, `status_unreachable`,
`session_ended`, `session_failed`, `parked`, `walk_not_taken`, …) — failures at
`ERROR`, progress at `INFO` — with the same fields as the `--trail` JSONL rows,
from one call site.

---

## Crossover round runner

[`scripts/run-crossover-round.py`](../scripts/run-crossover-round.py) runs ONE
crossover-v2 round end to end from the laptop, composing `jasper-angle-capture
stage`, `jasper-angle-capture serve`, the wizard's endpoints and
`bank-crossover-round.sh`. It builds nothing new on the Pi.

```sh
# measure (stage 1), lab arm walking five angles
PI_HOST=jts3.local .venv/bin/python scripts/run-crossover-round.py \
    --campaign captures/my-night --label r1 --tier remote \
    --angles 0,7,-7,22,-22 --regime per_driver \
    --attest-rig-clear --expect-angles 7,-7,22,-22

# the same five angles, three takes at each — one walk, fifteen stops
… --per-position 3 …

# …read the candidate it printed, decide, THEN apply it BY NAME
PI_HOST=jts3.local .venv/bin/python scripts/run-crossover-round.py --apply <fingerprint>

# the post-apply check (stage 2)
… --label r1-verify --stage verify --attest-rig-clear --expect-angles 7,-7,22,-22
```

`--campaign` is the campaign directory and `--label` the round's name inside it;
both are required to measure.

- **The apply gate is why the file exists.** A measurement run NEVER applies: it
  ends with the candidate's fingerprint and numbers printed on stdout and stops.
  Applying is a second invocation that must NAME the fingerprint, and the runner
  refuses **before any POST leaves the laptop** when the live candidate differs
  (`rc 11`; the test asserts nothing was sent, not merely a non-zero exit).
- **No `--complete-after`, and that is the recipe.** The session closes ITSELF
  once it has served every hold it planned, and the walk reads that terminal
  status. A laptop-side count cannot be honest — the flag counts RELEASES and
  the staged stop count is only a floor. Pass it only when a WALK has to close a
  wired stage's held set.
- **Phase order.** The walk is launched *before* the session opens, because
  `serve`'s first poll is what checks a staged walk is still waiting,
  and only with `--attest-rig-clear` — the attestation is the operator's.
  `--angles` / `--regime` / `--expect-angles` are forwarded as written; bounds
  and vocabulary are the seam's. A walk staged by an aborted round stays staged.
  Stopping a walk is a transport drop and the park happens on the speaker after
  the local ssh client is gone, so the runner reports the hangup, never the arm
  as parked. **`--tier` is ignored by `--stage verify`**: stage 2 takes the
  instrument the measuring session recorded.
- **`--per-position N`** stages each angle N times **adjacently**, so the arm
  settles and releases N times without travelling; what varies between takes is
  time and whatever you changed, never the pose. It governs a staged *measure*
  walk at any regime composing one stop per angle (`per_driver`, `summed`).
- **Every staged round banks `position_cycle.json`** — one sorted index of the
  poses the round actually measured, **derived** from the banked bundle (owner
  [`position_cycle.py`](../jasper/active_speaker/crossover_v2/position_cycle.py)),
  never from what the round *meant* to stage. When the bundle cannot support the
  index the runner names what was missing and writes nothing.
- **Refused before anything runs**, eight configurations: an `--apply` with an
  empty fingerprint or with `--per-position` at any value; `--angles` without
  `--attest-rig-clear`; an unreadable `--alignment-prescription` or
  `--topology-prescription`; `--per-position` under 1, or without `--angles`,
  or with `--stage verify`, or with a regime that does not compose exactly one
  stop per angle; and a `--complete-after` below the staged stop count. An
  **empty angle field is not** refused: `--angles 0,,7,` stages `0,7`.
- **Completion is polled, not slept**: the runner waits for the session id to
  move off the pre-open one *and* for the phase to leave the running set (every
  capture phase plus `closing` and `applying`).
- **No verdict is re-mapped.** `serve`'s exit code rides through beside the
  stall IT named, read off its refusal record (`arm_walk_exit=1
  arm_walk_exit_name=stuck`), and
  `bank-crossover-round.sh`'s `0/3/4` decides the round. A failing walk stops the
  round *before* banking, and the runner prints the one bank command that keeps
  the evidence sitting on the Pi. Its own exit codes are `EXIT_NAMES` in the
  script, tabulated in the [operator runbook](tuning-operator-runbook.md)'s
  "Exit codes"; each carries its deciding value on the phase line and in the
  `--trail` JSONL.
- **Which speaker.** The target is `scripts/_lib.sh`'s. `--hostname` overrides
  the speaker's *name* alone, which is the one way the ssh target and the name
  can come from different places — a round then ssh's to one speaker carrying
  another's `Host:` header, which the management-host guard 403s. The runner
  discloses the pair and where each half came from (the `identity` trail row).

---

## Measured driver base trim

**How much quieter must each driver be so the acoustic sum is level across every
declared crossover?** There is no verb to run: the answer is banked by the apply
itself. When a profile whose per-driver level match came from measurement is
applied, `baseline_profile.persist_applied_baseline_profile` writes the trim to
`/var/lib/jasper/active_speaker_driver_base_trim.json` — per-role trim, the
groups it covers, the evidence that produced it, and the crossover declaration
it was measured under. Applying a profile levelled on anything weaker (datasheet
sensitivity gap, an operator pin, a preserved manual crossover) CLEARS the
record, so the artifact only ever describes a level match the speaker is playing.

- **It mints no estimator and no solver.** The trim is whatever the applied
  profile resolved — a measured candidate's `role_attenuations_db`, or guided
  per-driver captures through `level_trim.attenuation_from_group_deltas`.
  `tests/test_active_speaker_driver_base_trim.py` fails if the artifact grows
  band arithmetic of its own.
- **Every trim is an attenuation and the maximum is exactly 0 dB**, so the
  quietest driver is the reference. **Attenuation-only is enforced by refusal,
  not by clamping** (`base_trim_not_attenuation`).
- **What the profile does with it.** `baseline_profile._measured_level_trims` is
  the single owner of "what is the measured per-role trim", preferring a banked
  base trim over the guided captures; `level_match.source` says which produced
  the number. The record names the declaration it was measured against, so a
  speaker whose declaration has moved gets a `driver_base_trim_not_applied`
  warning and keeps its safe existing trim. **Absent is normal.**
- **Where it sits.** Commissioning runs *rough config at `/sound` → seat-level →
  crossover candidates → driver linearization → room correction*. The apply
  banks the trim, and only for a MEASURED crossover candidate, which is
  necessarily downstream of seat-level. Declared per-driver figures no longer
  bind seat-level's ceiling (see [Seat-SPL leveling](#seat-spl-leveling)).
- **What it does not claim**: a magnitude answer only — never phase, delay or
  polarity — and a single level, so thermal compression is out of frame.

Coverage: `tests/test_active_speaker_driver_base_trim.py`,
`tests/test_active_speaker_level_match.py`,
`tests/test_active_speaker_baseline_profile.py`.

---

## Seat-SPL leveling

`jasper-seat-level` ([`jasper/cli/seat_level.py`](../jasper/cli/seat_level.py))
answers, on real hardware: **what main volume makes this speaker measure a
stated dB SPL at the listening seat?** It rolls the volume up from a quiet floor
while a calibrated measurement mic watches, stops inside the requested band, and
banks the volume as the crossover session's measurement reference.

```sh
# the ordinary run: stimulus synthesized from the drivers' declared measurement
# bands, converge on 75-80 dB SPL and bank the result
jasper-seat-level --mic-serial 810-8494

# explicit stimulus, band, calibration file; machine-readable
jasper-seat-level --stimulus-wav check.wav --calibration-file umik2.txt \
    --target-db-spl 72 --tolerance-db 2 --json

# instrumented: every window's per-sample dB SPL series, one DEBUG line per window
jasper-seat-level --mic-serial 810-8494 --verbose
```

- **Absolute SPL comes from the mic's own calibration file** — the `Sens Factor`
  header line, as `dB SPL = dBFS − sens_factor + 94`. **The precondition is
  yours to check**: that figure is quoted at the mic's MAXIMUM capture volume,
  so confirm `amixer -c <card>` shows the capture control at 100% first. No
  calibration means no absolute level and the verb refuses.
- **The ceiling is mic-independent**: `unsegmented_stimulus_ceiling_db`, digital
  full scale solved for main volume against the ACTUAL stimulus bytes, so a
  mis-calibrated microphone cannot move it. Given the applied graph as well,
  [`branch_peak.py`](../jasper/active_speaker/branch_peak.py) renders the
  stimulus through it and the first branch to reach full scale binds; it
  **refuses rather than approximates**, and every refusal falls back to the
  full-band bound, so an unmodelled graph makes the speaker quieter, never
  louder. **Declared per-driver caps do not bound it and have not since
  2026-08-23** — they are DISCLOSED on
  `event=active_speaker.unsegmented_ceiling_bound` (which also names
  `bound=per_branch` or `bound=full_band`). That field still clamps each
  driver's composed segment level: see
  [`measurement-loop-doctrine.md`](measurement-loop-doctrine.md) §4 item 3,
  where old `deviation (h)` citations also resolve.
- **A reading is settled when the instrument says so, not when a timer says
  so.** Each reading takes half-second windows until two consecutive medians
  agree within `JASPER_SEAT_LEVEL_SETTLED_AGREE_DB` (0.5 dB); the later is the
  reading. A level that never agrees inside
  `JASPER_SEAT_LEVEL_SETTLE_TIMEOUT_S` (8 s) refuses `spl_level_unsettled`
  rather than banking the last number seen, and a window with no finite sample
  is `mic_feed_lost` after **ONE** window, so a dead feed never waits that out.
  The reference itself is banked only when two consecutive READINGS agree — the
  same rule one level up, because the banked volume outlives the pass.
- **What agreement does not buy — the residual.** What is bounded is a RATE,
  never the remaining distance, so `residual ≈ (agree_db / MIC_WINDOW_S) × τ` —
  about **1 dB per second of τ**, and **unbounded in τ**. Measured on one
  reading: τ = 0.81 s reads 0.28 dB under, τ = 3 s reads 2.11 under, τ = 5 s
  reads 4.16 under. A low `windows` count is **not** evidence of stillness and
  reads most reassuring where the error is largest — read it as this chain's
  answer time, never as confidence. Raising the timeout converts an honest
  refusal into a silent under-read.
- **The room is measured before the tone, in silence**, and a climb reading
  below that floor triggers one fade-out / re-measure / fade-in, at most once
  per pass, published as `ramp.ambient_remeasured*`. **What silence does not
  buy**: room lulls autocorrelate over seconds, so a lull spanning both windows
  still banks — a large negative `remeasured_delta_db` is that shape in one grep.
- **How it climbs**: the remaining gap IS the step, saturated upward by one
  BITE = `BITE_FRACTION` (0.15) of this run's own span (`ceiling − start`) — a
  fraction, not a number of dB, because an unknown amplifier changes WHERE
  inside the span the speaker becomes audible, never how wide the span is. So
  any chain is swept in at most 7 bites, downward moves are uncapped, and no
  sample is discarded for being quiet. Audible time is bounded structurally at
  11 readings, so at most about **11 × `settle_timeout_s`** plus the fade legs.
- **Exit codes**: `0` converged and banked, `1` any refusal. Every refusal
  restores the household volume, banks nothing, and names itself — the mic ones
  (`mic_calibration_unavailable`, `measurement_mic_absent`, `mic_not_observing`,
  `mic_feed_lost`, `mic_clipping`), the target ones
  (`seat_spl_target_rejected`, `spl_target_uncapturable`,
  `spl_target_unreachable`, `spl_level_unconverged`, `spl_level_unsettled`),
  `spl_ceiling_exceeded` (one measured SAMPLE crossed
  `max_commissioning_level_db_spl`, not a settled reading), and the setup ones
  (`stimulus_wav_missing`, `measurement_session_already_live`,
  `driver_cap_ceiling_underivable`, `volume_ceiling_below_ramp_start`,
  `seat_level_watchdog_expired`, `seat_level_interrupted`,
  `measurement_isolation_unavailable`).
- **A refusal publishes the window it stopped in** — or the **fade leg**;
  `ramp.stopped_window` carries the sample count, min/median/max dB SPL and the
  tripping sample's offset. Read the median against the max: far below is ONE
  excursion on a settled level, at the max is a level that rose and stayed.

**Read the journal, not the code**: `event=active_speaker.seat_level_*` carries
`_start` (band, both ceilings, ambient, bite, settle contract, amixer
precondition), `_reading` (one per bite, including `windows`),
`_bank_unconfirmed`, `_converged`, `_refused` (slug, volume, ceiling, any
`stopped_window_*` and the `prior_*` reading to read them against),
`_ambient_remeasured`, `_window_samples` (DEBUG, behind `--verbose` — the
per-sample record exists nowhere else), `_restore_failed` and
`_teardown_abandoned`.

**What it does not do**: it designs no stimulus of its own judgment and opens no
measurement session. It writes one document to
`/var/lib/jasper/active_speaker_seat_level_reference.json`, read by the next
session as `measurement_reference_volume_db`; **absent is normal**, and
`jasper-doctor`'s `seat-SPL measurement reference` line reports which state that
file is in. One deploy-time knob, bounded and falling back to its default on a
bad value: `JASPER_SEAT_LEVEL_MIN_RISE_DB` (default 6). Coverage:
`tests/test_active_speaker_seat_level.py`, `tests/test_cli_seat_level.py`,
`tests/test_active_speaker_branch_peak.py`, `tests/test_wired_level_meter.py`,
`tests/test_active_speaker_session_volume_plan.py`,
`tests/test_doctor_correction.py`.

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
