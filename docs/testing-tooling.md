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
| Format, type-check, and Clippy-lint every Rust crate locally, including ALSA-backed crates on macOS | [Rust formatting and Clippy cross-check](#rust-formatting-and-clippy-cross-check) |
| Capture the AEC bridge's three streams (raw mic / AEC ON / reference) | [Capture: 3-stream bridge captures](#capture-3-stream-bridge-captures) |
| Audit the deliberate wake-corpus recorder output after rsync | [Wake-corpus audit (deliberate recordings)](#wake-corpus-audit-deliberate-recordings) |
| Export wake-corpus recordings for off-Pi training | [Wake-corpus training bundle export](#wake-corpus-training-bundle-export) |
| Analyze wake-corpus audio artifacts / quality | [Wake-corpus quality analyzer](#wake-corpus-quality-analyzer) |
| Count wake-word detections on captured audio offline | [Wake-word scoring (offline)](#wake-word-scoring-offline) |
| Pull production wake events + clips from the Pi | [Wake-event telemetry (production)](#wake-event-telemetry-production) |
| Diagnose a bridge / AEC issue forensically | [AEC / bridge forensics](#aec--bridge-forensics) |
| Generate a fixed audio test track for repeatable testing | [Test-track generation](#test-track-generation) |
| Check live Pi state (services / config / mic / etc.) | [Pi-side diagnostics](#pi-side-diagnostics) |
| Diagnose one correction level/sweep run with synchronized UMIK audio and speaker gain state | [Correction capture diagnostic](#correction-capture-diagnostic) |
| Check that the DSP actually realizes a linearization the way the fit says it will (the shelf-Q class), offline and without a microphone | [Offline emit loop](#offline-emit-loop) |
| Replay recorded tuning attempts through the S3 improve/stop policy, and see whether the loop would have claimed an improvement that was only noise | [Attempts-loop replay](#attempts-loop-replay) |
| Find out whether a banked session's cloud null evidence actually *bound* the linearization fit, and what the fit does without it | [Severed-twin replay](#severed-twin-replay) |
| Read a driver's harmonic distortion (H2/H3 vs frequency) out of MEASURE captures already on disk, with no new recording | [Harmonic-distortion replay](#harmonic-distortion-replay) |
| Hold a specific field incident still in CI — minimize a gitignored bank to a committed fixture and characterize the defect it produced | [Committed incident replay](#committed-incident-replay) |
| Ask why a banked session's pooled flatness reads worse than its on-axis response sounds — re-read the same evaluation per octave and per position role | [Metric-honesty views](#metric-honesty-views) |
| Gather one banked crossover round into a single versioned JSON document a person or a language model can reason about | [Crossover prescriber harness](#crossover-prescriber-harness) — `jasper-crossover-prescriber packet` |
| Validate a blend-region correction someone (or something) proposed against the round it claims to answer, and see the machine-readable reason if it is refused | [Crossover prescriber harness](#crossover-prescriber-harness) — `jasper-crossover-prescriber propose` |
| Put an accepted blend-region correction where the next crossover round will apply it, once | [Crossover prescriber harness](#crossover-prescriber-harness) — `jasper-crossover-prescriber stage` |
| See exactly what a per-driver or summed capture walk at stated angles resolves to — pose, program, advance policy, banked shape — before anything plays | [Angle-walk door](#angle-walk-door) — `jasper-angle-capture plan` |
| Put a stated angle walk where the next measurement session will take it, once | [Angle-walk door](#angle-walk-door) — `jasper-angle-capture stage` |
| Find the main volume that makes this speaker measure a stated dB SPL at the listening seat, and bank it as the next session's measurement reference | [Seat-SPL leveling](#seat-spl-leveling) — `jasper-seat-level` |
| Grade the boost-permission gate's decision against a defect you injected on purpose (rather than one a room happened to produce) | [`tests/test_crossover_v2_boost_scenarios.py`](../tests/test_crossover_v2_boost_scenarios.py) — synthetic spatial scenarios, the validation ladder's third rung |
| Validate two Apple USB-C DACs as a lab-only output topology | [Dual Apple DAC lab runner](#dual-apple-dac-lab-runner) |
| Manually detect, probe, or move the experimental USB turntable on JTS3 | [USB turntable experiment](#usb-turntable-experiment) |
| Drive a crossover-measurement v2 lab round from a Mac with no browser and no phone | [E0 headless capture client](#e0-headless-capture-client) |
| Characterize whole-system CPU/memory/journal behavior over time | [System soak artifacts](#system-soak-artifacts) |
| Measure inter-speaker sync error for multi-room (stereo pair / sub) on WiFi | [Multi-room sync spike (P0)](#multi-room-sync-spike-p0) |
| Measure the AirPlay latency budget a sender negotiates (free vs. tight regime for bonded-leader lip-sync) | [Pi-side diagnostics](#pi-side-diagnostics) — [`scripts/airplay-latency-probe.sh`](../scripts/airplay-latency-probe.sh) |
| Certify (or honestly fail) `usb_low_latency_48k`'s p95/p99 route-latency claim with real click/capture impulses | [Route-latency click/capture harness](#route-latency-clickcapture-harness) |
| Certify the reverse `JTS Mic` bridge-emit→ALSA-write latency while a computer is actively recording | [USB microphone export latency artifact](#usb-microphone-export-latency-artifact) |
| Turn up logging for one subsystem on the live Pi (`/system` Debug card) | [`HANDOFF-observability.md`](HANDOFF-observability.md) |
| Diagnose speaker identity (mDNS collision rename, hostname drift, management-UI 403s) | [`HANDOFF-identity.md`](HANDOFF-identity.md) — `/state.resilience.identity`, the doctor identity checks, `event=identity_reconcile.*` |
| Get the verbose DEBUG context around a failure (in-RAM flight recorder, `event=flightrec.dump`) | [`HANDOFF-observability.md`](HANDOFF-observability.md) |
| Get a periodic read-only journal-health digest with week-over-week `event=` deltas | [Pi-side diagnostics](#pi-side-diagnostics) — [`scripts/journal-review.sh`](../scripts/journal-review.sh) |
| Preview what install.sh would mutate | [Install dry-run plan](#install-dry-run-plan) |
| Check every shipped deploy unit/rule/script has an install step (and every install reference resolves) | [`tests/test_deploy_wiring_guards.py`](../tests/test_deploy_wiring_guards.py) — two-sided orphan-artifact guard |
| Check the "wizard env file wins" EnvironmentFile= ordering across systemd units | [`tests/test_deploy_wiring_guards.py`](../tests/test_deploy_wiring_guards.py) — wizard-env precedence guard |
| Check udev SYSTEMD_WANTS hotplug targets are shipped units | [`tests/test_deploy_wiring_guards.py`](../tests/test_deploy_wiring_guards.py) — udev → unit chain guard |
| Check wizard-socket ListenStream ports match nginx upstreams (PR #118 502 class) | [`tests/test_deploy_wiring_guards.py`](../tests/test_deploy_wiring_guards.py) — two-sided socket↔nginx parity guard |
| Check install/build supply-chain provenance | [Supply-chain provenance](#supply-chain-provenance) |
| Build or verify the first-party Pi ARM64 runtime bundle | [First-party ARM64 release artifact](#first-party-arm64-release-artifact) |
| Pin a documented invariant / convention with a test (registry coverage, SSOT readers, env-var codification, cross-language wire shapes) | [Guard & contract test patterns](#guard--contract-test-patterns) |
| Point a laptop-durable flat-linearization corpus at a non-default location, or re-derive a pinned reading after a detector/reading change | [`tests/_flat_lin_corpus.py`](../tests/_flat_lin_corpus.py) — `JTS_FLAT_LIN_S0` / `JTS_FLAT_LIN_CORPUS` env vars; re-derivation procedure lives in `tests/test_spatial_combine.py::test_band_deficit_separates_honest_captures_from_stopband_residue` |
| Find out what a measurement change actually moved — including the readings a tolerance absorbed and the prose homes that restate them, neither of which any lane can go red on | [Reading comparator (pre/post value diff)](#reading-comparator-prepost-value-diff) |
| Reproduce a flake that only appears when the box is busy, without leaking a CPU burner onto a machine other agent sessions are sharing | [Reproducing a load-dependent flake](#reproducing-a-load-dependent-flake) |
| Run a lane or tool from a worktree/export and be sure it exercised THAT copy, not the main checkout | [Running a lane in an isolated checkout](#running-a-lane-in-an-isolated-checkout) |
| Fix a test that only flakes in a loaded full-suite run (spawn/thread/FD exhaustion), without papering over a real failure | [Guard & contract test patterns](#guard--contract-test-patterns) — transient-resource retry row |
| Find out *why* a loaded run runs out of file descriptors, instead of retrying around it | [Guard & contract test patterns](#guard--contract-test-patterns) — fd-leak row |
| Understand why a test failed with "Timeout … from pytest-timeout", or bound a legitimately slow test | [Hang backstop (pytest-timeout)](#hang-backstop-pytest-timeout) |
| Test the assistant's *behavior* (does it understand a question, call the right tool) | [Voice-eval (paid LLM tests)](#voice-eval-paid-llm-tests) |
| Capture directly from the raw chip path | [Capture: alternative sources](#capture-alternative-sources) |
| Sweep files changed on a branch for roadmap-dated comment/doc phrasing that may have gone stale ("not yet", "until X lands") before publishing a PR | [`scripts/tense-grep.sh`](../scripts/tense-grep.sh) — advisory, a normal run always exits 0 |
| Sweep the WHOLE repo (not just a branch diff) for roadmap-dated phrasing once per deletion/cutover PR — a changed-files-only sweep structurally can't catch a claim falsified in a module the diff never touches (#2325) | [`scripts/tense-grep.sh --all`](../scripts/tense-grep.sh) — same advisory contract, output grouped by file with a per-file count; diff a before/after baseline by hand |

---

## Rust formatting and Clippy cross-check

[`scripts/check-rust.sh`](../scripts/check-rust.sh) is the local and CI
source of truth for Rust formatting and Clippy:

```sh
scripts/check-rust.sh
```

The script reads the pinned `RUST_TOOLCHAIN` from
`.github/workflows/tests.yml`, checks all nine crates in the CI Rust job, and
uses that toolchain for both `cargo fmt --all -- --check` and release,
locked, all-target Clippy with warnings denied. `jasper-host-clock` alone is
checked with `--all-features`, matching CI's ALSA-actuator coverage.

On Linux the script uses the host's real ALSA development metadata. On
macOS it maps Apple Silicon to `aarch64-unknown-linux-gnu` and Intel to
`x86_64-unknown-linux-gnu`, then supplies `alsa-sys` a temporary stub
`alsa.pc`. The Linux target is load-bearing: the Rust `alsa` crate rejects a
Darwin target before type-checking. The stub is safe only because this lane
does not link, and the temporary directory is removed on success or failure.

The script fails before Cargo with an exact `rustup` command when the pinned
toolchain, `rustfmt`/Clippy components, or macOS cross target is missing. It
also requires `pkg-config`; Linux additionally needs real ALSA headers
(`libasound2-dev` on Debian/Ubuntu or `alsa-lib-devel` on Fedora).

This catches type errors in `#[cfg(test)]` modules because Clippy runs with
`--all-targets`, but it does **not** execute Rust unit tests. Those tests link
against ALSA and remain a Linux/CI gate (`cargo test --release --locked` in
the Rust CI job).

---

## Hang backstop (pytest-timeout)

Every test is bounded at **300 s** (`timeout` / `timeout_method` in
`[tool.pytest.ini_options]`, pinned by
`tests/test_dependency_groups.py::test_hang_backstop_is_configured_and_uses_the_signal_method`).

**What it is for.** An unbounded await whose producer dies never returns.
Before the backstop, one such test blocked the whole local suite with no
failing test to point at, and the producer's real exception stayed
swallowed on a task nobody awaited. The backstop turns any hang — an
`asyncio.Event` nobody sets, a wedged socket read, a blocking
`subprocess.run` — into a reported failure with the stuck stack.

**`timeout_method = "signal"` is load-bearing.** Measured both ways:
`thread` dumps the stack and then kills the whole pytest process, losing
every result after the stuck test; `signal` fails only that test and lets
the run continue. It also survives `pytest-xdist` (`scripts/test-merge`
runs `-n 4`). Its limits: it cannot interrupt a hang inside a C extension
or at collection/import time, so CI's job-level `timeout-minutes` stays
as the outer belt.

**300 s is a hang-breaker, not a timing assertion.** The slowest healthy
test measures ~15 s, so this is ~20x headroom, sized so a loaded dev box
does not go red. Never tighten it to make a slow test fail — assert
timing in the test itself.

**Overrides.** `@pytest.mark.timeout(N)` on a test or module; `N = 0`
disables. `tests/voice_eval/conftest.py` raises the whole paid suite to
`VOICE_EVAL_TIMEOUT_S` (900 s) because a pass^3 scenario is three
sequential live sessions.

**Related:** the backstop is the broad, slow net. For the specific
`await <event>.wait()` shape, `tests/_async_wait.py::wait_signalled()`
fails in ~10 s *and names the producing task's exception*, and the guard
in `tests/test_async_wait_contract.py` catches the pattern at CI time —
where quiet Linux runners mean the backstop never fires. See the
bounded-wait row in [Guard & contract test patterns](#guard--contract-test-patterns).

---

## Reproducing a load-dependent flake

Some flakes only appear when the box is busy (#1909, #2681, the macOS
subprocess class), so reproducing one means generating CPU load on
purpose — on a machine **shared with other agent sessions running their
own pytest lanes**, where an orphaned burner steals their wall clock and
can manufacture the very timing flake someone else is diagnosing. Three
measured traps turn the obvious cleanup into a silent no-op:

```sh
PIDS=""
for _ in 1 2 3 4 5 6 7 8; do
  ( end=$((SECONDS+55)); while [ $SECONDS -lt $end ]; do :; done ) &
  PIDS="$PIDS $!"
done
# ... run the flaky test ...
for p in ${=PIDS}; do kill "$p"; done      # NOT 2>/dev/null
```

- **A non-interactive shell reports no jobs.** With two live background
  jobs, `jobs -p | wc -l` measured `0`, so `kill $(jobs -p)` dies with
  `kill: not enough arguments` and cleans up nothing. Keep the `$!`
  values yourself.
- **zsh does not word-split** (the agent shells here are zsh 5.9), so
  `for p in $PIDS` iterates **once** with the whole space-joined string
  and calls `kill "11802 11803"` → `kill: illegal pid`. `${=PIDS}`
  splits; plain SIGTERM is then enough, and `kill -9` is not needed.
- **Never `2>/dev/null` the cleanup.** Both messages above *are* the
  diagnosis — unsuppressed they name the bug on the first run. A cleanup
  that prints nothing is not evidence it cleaned anything; confirm with
  `ps -o pid=,command= -p <pid>`.

Time-bound the loop body as well, keeping the bound comfortably above your
repro's own runtime, so a cleanup you lose anyway self-heals in under a
minute instead of burning a core until someone else notices.

---

## Running a lane in an isolated checkout

[`DEEP-AUDIT-PLAYBOOK.md`](DEEP-AUDIT-PLAYBOOK.md) item 4 owns the rule — pin
`PYTHONPATH` to the checkout under test and confirm a known edit is visible
before trusting a green run. The mechanism behind it: the venv's editable
install appends a `sys.meta_path` finder that **hardcodes the main checkout's
path**, so it answers whenever nothing earlier on `sys.path` does, and an
isolated worktree or export imports the LIVE tree with no error at all.

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
`deploy/install.sh`, Python direct URL dependencies, and the wake/DTLN
model registries.

Run it when touching install/build downloads or dependency declarations:

```sh
python3 scripts/check-provenance.py
```

The policy and update workflow live in
[`docs/HANDOFF-supply-chain.md`](HANDOFF-supply-chain.md).

## First-party ARM64 release artifact

The manual native-ARM64 release lane builds and validates the narrow compiled
runtime that a future Pi image can preload:

```sh
python3 scripts/build-first-party-arm64-release.py
python3 scripts/verify-first-party-arm64-release.py \
  dist/first-party-arm64/jts-first-party-runtime-<version>
pytest -q tests/test_first_party_arm64_release.py
```

Use `--expected-source-sha <full-sha>` when validating a bundle for install.
The artifact contract, output semantics, license scope, installer transaction,
and reproducibility limits live in
[`HANDOFF-first-party-arm64-artifacts.md`](HANDOFF-first-party-arm64-artifacts.md).

---

## Guard & contract test patterns

Reusable exemplars for AGENTS.md's "Pin promises with tests" rule —
when a comment, docstring, or doc states an invariant, one of these
shapes usually fits. All run in normal hardware-free `pytest`. Mirror
the closest one rather than inventing a new guard style:

| If you want to … | Mirror |
|---|---|
| Keep constants a bash script re-hardcodes in sync with their Python SSOT | [`tests/test_reconciler_constants_match_python.py`](../tests/test_reconciler_constants_match_python.py) — reads the Python values, parses the script's hardcoded fallbacks, fails naming the drifted constant and both values |
| Ban a literal from being re-declared outside its owning constant, matched by VALUE not spelling | [`tests/test_correction_boundary_ssot.py`](../tests/test_correction_boundary_ssot.py) (the room-correction band edge — `250.0`/`350.0`/`500.0`) and [`tests/test_correction_substream_ssot.py`](../tests/test_correction_substream_ssot.py) (the ALSA lane name `"correction_substream"`) — both AST-parse every routed/scanned file and compare `ast.Constant` node values, not source text, so prose that merely *mentions* the literal (docstrings, comments, a longer derived string sharing a prefix) is never a false positive, and spelling variants of the same value (`250` / `250.0` / `3.5e2`) all count as the same re-declaration |
| Freeze a convention's current offenders and block new ones (burn-down list) | [`tests/test_atomic_io_conventions.py`](../tests/test_atomic_io_conventions.py) — two-sided allowlist ratchet: a new offender fails, and a stale allowlist entry fails too, so the list only shrinks |
| Enforce a repo-wide code convention that otherwise lives only in a comment | [`tests/test_shell_awk_environ_convention.py`](../tests/test_shell_awk_environ_convention.py) — mutation-verified convention guard: scoped so the benign idiom stays legal while the exact bug shape fails, naming file:line and the sanctioned replacement |
| Require every call site of a dangerous import to be preceded by its guard | [`tests/test_lazy_imports.py`](../tests/test_lazy_imports.py) — `test_every_openwakeword_import_site_is_guarded`: **discovery, not a hand-list.** ASTs every `.py` in the tree *minus* a short exclusion map, finds each `import openwakeword` (plus `importlib.import_module("openwakeword")` / `__import__(...)` with a literal name), and requires an unconditional `ensure_openwakeword_import_safe()` as a **direct statement of the same function body, at an earlier line** — a call nested in `if`/`try`, or sitting in an enclosing scope, does not count. That is stricter than runtime necessity (a module-level call really would run first) on purpose: one uniform convention beats a rule whose reader has to simulate execution order. Three properties make it non-vacuous: scanning the whole tree is fail-closed (an include-list of roots silently ignores a *new* top-level directory — the same "nobody noticed" shape being guarded), the discovered set is asserted non-empty so a broken scanner fails loudly, and `test_openwakeword_scan_exclusions_are_all_live` fails when an exclusion stops matching any import, since a carve-out that excuses nothing has quietly become a blanket hole. On first run that check deleted one of the two exclusions as unnecessary. **Why the shape, not just a runtime probe:** the predecessor guard asserted "no sklearn in `sys.modules` after `import jasper.wake`" and was *vacuous* — that import never reached openwakeword at all (the `Model` import is lazy inside `__init__`), so it passed identically with the guard deleted, verified by mutation on hardware. A runtime probe only covers the entry point it happens to exercise; the static scan covers the ones nobody thought to write a probe for, which is where the two real regressions (jasper-doctor, standalone `jasper.vad`) had been hiding |
| Assert an import chain stays light (no heavy hard-deps in wizards/config) | [`tests/test_web_wizard_import_chain.py`](../tests/test_web_wizard_import_chain.py) + `tests/test_config.py::test_config_import_chain_does_not_require_httpx` — poisoned-import chain contract: import in a subprocess with the heavy module poisoned in `sys.modules`, so an installed copy can't mask a regression |
| Keep a hand-written plan/summary covering an orchestrator's real steps | [`tests/test_install_plan_covers_main.py`](../tests/test_install_plan_covers_main.py) — orchestrator/plan coverage: parses `main()`'s calls, asserts each maps to a marker in the actual `--dry-run` output; meta-assertions fail stale mappings loudly |
| Enforce an observability convention across every handler of a class | [`tests/test_web_wizard_event_audit.py`](../tests/test_web_wizard_event_audit.py) — behavior-coverage guard: every state-mutating/restarting wizard handler must emit an `event=` audit line; on first run it caught 3 unaudited voice-provider handlers that a manual sweep and three independent reviews had all missed |
| Keep deploy/ artifacts and install.sh wiring in lockstep (orphan units, wizard-env precedence, udev chains, socket↔nginx parity) | [`tests/test_deploy_wiring_guards.py`](../tests/test_deploy_wiring_guards.py) — four structural guards: two-sided orphan-artifact coverage (every shipped unit/rule/script has an install reference and vice versa), "wizard env file wins" `EnvironmentFile=` ordering, udev `SYSTEMD_WANTS` → shipped unit, wizard-socket↔nginx port parity (the PR #118 502 class) |
| Pin a Rust↔Python JSON wire shape (fan-in / outputd `STATUS`) | `test_fanin_status_keys_match_python_consumers` + `test_outputd_status_keys_match_python_consumers` in [`tests/test_wire_contracts.py`](../tests/test_wire_contracts.py) — grep-pins the Rust emitter's keys against every Python consumer; fail-soft seams drift loudly instead of degrading to null |
| Pin a cross-process command vocabulary or socket-path literal | `test_fanin_control_command_vocabulary_matches_mux` + `test_control_socket_paths_agree_across_processes` in [`tests/test_wire_contracts.py`](../tests/test_wire_contracts.py) — `STATUS`/`AUTO`/`NONE`/`SELECT` mux ↔ state.rs, plus Rust defaults / systemd env / Python consumers agreeing on socket paths |
| Detect silent no-op env knobs across a language boundary | `test_outputd_fanin_env_names_are_read_by_rust_or_excepted` + `test_env_contract_exceptions_stay_accurate` in [`tests/test_wire_contracts.py`](../tests/test_wire_contracts.py) — every `JASPER_OUTPUTD_*` / `JASPER_FANIN_*` name set by bash/units/install.sh/.env.example must be read by Rust `from_env`, with a documented-exceptions list for staged vars whose companion test fails when an exception goes dead or live |
| Keep dashboard ES-module payload keys matching the server's snapshot payload | `test_dashboard_snapshot_top_level_keys_exist_in_server_payload` + `test_dashboard_metrics_current_keys_exist_in_sampler` + `test_dashboard_airplay_card_keys_exist_in_health_sampler` in [`tests/test_wire_contracts.py`](../tests/test_wire_contracts.py) — `snap.*` / `metrics.current.*` / airplay-card nested keys read by the JS must exist in the Python payload builders |
| Enforce a single-reader rule for a wizard-owned env var | [`tests/test_voice_provider_ssot_reader.py`](../tests/test_voice_provider_ssot_reader.py) — only `Config.from_env` reads `JASPER_VOICE_PROVIDER` from `os.environ`; every other surface must go through `jasper.voice.provider_state` (AGENTS.md "one reader, never os.environ") |
| Enforce "Codify, don't memorise" — every env var read has a codification surface | [`tests/test_env_vars_codified.py`](../tests/test_env_vars_codified.py) — every `JASPER_*` env var read in `jasper/` must appear in `.env.example` prose, deploy/, scripts/, or a wizard writer; `_UNCODIFIED` allowlist for internal seams, grouped and commented |
| Keep a registry and its call sites in set-equality (no orphans either way) | [`tests/test_cue_registry_coverage.py`](../tests/test_cue_registry_coverage.py) — cue registry ↔ `cues.play()` sites, both directions, no allowlist: no orphan `CueDef`, no play call naming an unregistered slug (AGENTS.md "No silent failure paths") |
| Enforce per-tool regression-scenario coverage without running the paid suite | [`tests/test_tools_have_regression_scenarios.py`](../tests/test_tools_have_regression_scenarios.py) — static file scan only: every `@tool` in jasper/tools/ is named in a `tests/voice_eval/regression/` scenario; no allowlist remains, so a newly-added user-callable tool must land with a scenario mention in the same change |
| Ban a code pattern in non-Python runtime paths, with an audited allowlist | [`tests/test_rust_runtime_panic_freedom.py`](../tests/test_rust_runtime_panic_freedom.py) — static scan of the Rust audio daemons: `unwrap()`/`panic!`/`unreachable!`/`todo!`/`unimplemented!` banned outright outside `#[cfg(test)]`; `expect()` and the `assert!` family are each allowed only via their own (file, key)-keyed allowlist carrying each site's audit rationale; two-sided so stale entries fail too (cargo can't run everywhere, and cargo test can't tell a test-only unwrap from a runtime one) |
| Pin a documented safety literal across Python + Rust + checked-in config | [`tests/test_audio_safety_pins.py`](../tests/test_audio_safety_pins.py) — bans reintroducing a fixed TTS max-gain ceiling, pins the shared Rust loudness helper exports, and parses every static `deploy/camilladsp/*.yml` for a present, non-positive `volume_limit` (the emitters validate; the checked-in files needed their own pin) |
| Assert a plugin registry's N-way surface completeness | `tests/test_usage.py::test_every_catalog_model_has_bundled_pricing` + [`tests/test_cues_factory.py`](../tests/test_cues_factory.py) — iterate the catalog (`PROVIDERS`) and assert each entry has its per-surface leg (a bundled pricing entry; a first-class cue-TTS dispatch branch, detected behaviourally by the absence of the fallback warning), so adding a registry entry without one of its surfaces fails by construction |
| Pin a security seam + its ordering across every handler of a class | `tests/test_web_wizard_conventions.py::test_every_wizard_mutating_handler_uses_the_csrf_chokepoint` + `::test_mutating_handlers_route_check_before_csrf_guard` — AST-walks every `do_POST`/`do_DELETE` under `jasper/web`, requires the shared `guard_mutating_request()` call (one-entry bespoke allowlist), and requires the handler's *first* conditional to be the route check, never the CSRF guard ("bogus paths 404 without revealing CSRF state"); on first run it caught wake-corpus 403ing on unknown POST/DELETE paths |
| Require structured metadata in every doc of a class | [`tests/test_docs_handoff_freshness.py`](../tests/test_docs_handoff_freshness.py) — every `docs/HANDOFF-*.md` carries the `Last verified: YYYY-MM-DD` footer `scripts/doc-freshness.sh` keys on, and any `> **Status: historical**` callout sits immediately under the H1 (AGENTS.md doc rules 3 + 10) |
| Ban interpolated runtime values in generated inline `on<event>=` handlers (shape-based, not name-based) | `tests/test_web_wizard_conventions.py::test_wizard_python_does_not_interpolate_into_inline_handler_js` + `::test_static_modules_do_not_interpolate_into_inline_handler_js` — catches any `on<event>=` attribute whose value carries f-string interpolation (jasper/web/*.py) or template-literal interpolation (deploy/assets ES modules), enforcing the AGENTS.md "no untrusted strings in generated inline JavaScript" rule for handlers not yet on any fixed name list; zero allowlist, mutation-verified |
| Pin a bash-writer ↔ Python-reader file contract end-to-end | [`tests/test_install_web_assets.py`](../tests/test_install_web_assets.py) — runs the real installer bash function (sed-extracted, sandboxed via env-injected roots) over the real `deploy/assets/` tree, then has the real doctor check parse the manifest it wrote; unit tests fake one side each, the round-trip catches format drift between the two languages. Plus a tree-shape conventions guard: any repo asset the copy loop's globs would silently skip fails CI (the silent-404 class) |
| Stop a test flaking when the OS momentarily refuses a resource it needs (process spawn, handler thread) under a loaded parallel run | **Retry the acquisition, narrowly and loudly.** Two sites today, deliberately not shared: [`tests/test_wifi_guardian_script.py`](../tests/test_wifi_guardian_script.py)'s `_run_guardian` (transient `fork()`/`posix_spawn` errnos + the bash text a failed child fork prints) and [`tests/test_restart_broker.py`](../tests/test_restart_broker.py)'s `_retry_transient_broker_io` (the socketserver accept-then-close race, whose *two* client-visible shapes are a chained `BrokenPipeError`/`ConnectionResetError` and a **causeless** `BrokerUnavailable("empty broker response")`). The classifiers are genuinely different — subprocess output vs exception shape — so a shared module would be premature; the *shape* is what to copy. Every instance owes: a narrow classifier (never a blanket `except`), bounded attempts with a final re-raise, one dedicated `UserWarning` subclass per retry so a persistently-degraded machine leaves a breadcrumb in pytest's warnings summary, no warn/sleep on the final attempt, and — critically — a way to tell the harness hiccup from a **real** failure wearing the same signature. `test_restart_broker.py`'s `_HANDLER_ACTIVITY` counters are the worked example: a broker handler that runs and crashes closes the connection with nothing written, identical at the client to the race, so the retry declines a failure only when a handler both **entered and crashed**. Both conjuncts are load-bearing — "a handler ran" alone wrongly vetoes the retry when a handler completes normally and the client still blips. That over-strict version passed every serial loop and then failed 1-in-64 under 8-wide concurrent load; the real-world producer of that case has since been fixed at the source, so the conjunct is now pinned synthetically rather than by reproducing it |
| Stop a loaded run from running *out* of file descriptors, rather than retrying around the symptom | **Close what you open — and attribute the leak before fixing it.** The retry row above absorbs the symptom; this is the other half. `loop.stop()` ends `run_forever` but frees nothing: a loop's selector fd and self-pipe pair survive until GC finalizes the object, so a function-scoped fixture that stops without closing leaks 3 fds per test. A dev box hides this (soft `RLIMIT_NOFILE` ~1e6); a CI runner's is 1024, and the casualty would never be the leaker — it would be whichever unlucky test next spawns a subprocess. **Resist the tempting next step of pinning that to an incident.** Chasing issue #1935 this way produced a wrong diagnosis: the `errno=24 (Too many open files)` lines in its logs are *injected* by two intentional negative tests in [`tests/test_wifi_guardian_script.py`](../tests/test_wifi_guardian_script.py) and appear in every green run, and the suite's measured fd high-water is ~43 against that 1024 limit. Leaked descriptors are worth fixing on their own merits; a log line that names a resource is not evidence the resource ran out. **Attribute before fixing:** a ~30-line pytest plugin that samples `len(os.listdir("/dev/fd"))` in `pytest_runtest_teardown` and rolls the deltas up per file names the leaker in one serial run — theorising from grep alone had fingered the wrong file. Distinguish *accumulation* (count climbs monotonically) from *transient* spikes that GC reclaims; only the former exhausts a limit. The invariant is pinned statically by [`tests/test_lint_contracts.py`](../tests/test_lint_contracts.py)'s `test_test_event_loops_are_closed_not_just_stopped`, which walks the **AST** rather than the text — three successive text-based versions of that guard were each fooled, by comments naming the anti-pattern, then by docstrings, then by Python 3.12 splitting f-strings into sub-tokens. A guard for a rule about accuracy has to be immune to prose about itself. The shape to copy is [`jasper/control/supervisor_runtime.py`](../jasper/control/supervisor_runtime.py)'s `build_asyncio_thread` — **the thread that owns the loop closes it** (`def _run(): try: loop.run_forever() finally: loop.close()`), which survives a teardown that raises before it reaches a fixture-level `close()`. Deliver `stop()` from a `finally` too, or `run_forever` never returns and the thread's own `finally` never runs |
| Keep a concurrency test's coordination waits bounded (no infinite hangs) | [`tests/test_async_wait_contract.py`](../tests/test_async_wait_contract.py) — repo-wide AST guard with a two-sided shrink-only `KNOWN_UNBOUNDED_WAITS` ratchet: a bare `await <event>.wait()` in ANY async test's own body fails unless allowlisted, and a stale allowlist entry fails too, so the burn-down list can only shrink. The `await asyncio.Event().wait()` park-until-cancelled idiom and producer-side waits stay legal. Fix with [`tests/_async_wait.py`](../tests/_async_wait.py)'s `wait_signalled()`, which bounds the wait and reports the *producing task's* exception as the cause — a producer that dies before signalling (a missed `dsp_writer_lock` budget on a loaded box) otherwise hangs the suite forever with its real error swallowed on a task nobody awaits. A **second detector in the same file** catches the other half of the class: a `wait_for(<x>.wait(), timeout=…)` bounded **below** the `SMALL_BOUNDED_WAIT_THRESHOLD_S` floor (1.0 s) fails too, on its own two-sided `KNOWN_SMALL_BOUNDED_WAITS` ratchet that starts EMPTY and grandfathers nothing. Such a bound satisfies the first detector but nothing ever reads it as a promise, so it is only a deadline the test can lose on a loaded box — a hang-breaker set near the coordination it is breaking. 1.0 is where the tree already sat (29 of 45 bounded event waits exactly 1.0, nothing between 0.2 and 1.0). Remedy: raise the bound above the floor, or `wait_signalled()` when the wait is on an `asyncio.Event`; a real timing promise is pinned by an explicit `assert elapsed < N` in the test, never by a `wait_for` timeout. This is the CI-time net: Linux runners are quiet, so these never hang there and the [hang backstop](#hang-backstop-pytest-timeout) never fires on them |
| Prove a long-lived daemon loop actually answers `cancel()` | `tests/test_mux.py::test_run_answers_cancellation_racing_a_wake_alert` — construct the race deterministically instead of sampling it: resolve the awaited event and call `task.cancel()` with **no intervening `await`**, so both wake-ups queue in one loop iteration, then assert the task finished via `asyncio.wait({task}, timeout=…)`. Catches a swallowed `CancelledError`, which makes a `while True` task immortal and hangs every awaiter. `Task.cancelling() >= 1` on a task that is `done=False`/`cancelled=False` is the direct read-out of a swallowed cancel when diagnosing one. Note CPython ≤ 3.11's `asyncio.wait_for` swallows a cancel arriving in the tick its awaited future completes (#1935) — prefer `async with asyncio.timeout(...)` in any loop whose only exit is cancellation. Two more instances of the same race, same construction (resolve the fake reply, `task.cancel()`, no intervening `await`), against the underlying helper directly rather than the loop that calls it: `tests/test_correction_coordinator.py::test_voice_uds_command_answers_cancellation_racing_the_reply` and `tests/test_control_uds.py::test_mux_command_answers_cancellation_racing_the_reply` (#1952). A `wait_for`/`timeout` call reached only through an `asyncio.gather()` child is a different story — `gather`'s own `_cancel_requested` bookkeeping delivers the parent's cancellation regardless of whether a child swallowed its own, so that shape does not need this treatment (verified for the `_read_airplay_db`/`_read_bluetooth_volume` gather children specifically, `jasper/volume_observers.py`, #1952). That clears only those two calls, **not** the enclosing loop, which reached un-insulated `wait_for`s through **four** directly-awaited chains outside the gather, landing on **three** terminal call sites (#2003): (1) every tick, `_tick` → `VolumeCoordinator._active_source` → `RendererClient.selected_source`; (2) on a source transition, `apply_active_source_transition` → `_set_push_source_for_handoff` → `_set_bluetooth` → `bluealsa_probe.list_pcms` and `_busctl_set_property`; (3) on an accepted observation, `_maybe_observe` → `observe_source_volume`, which calls `_active_source` itself; (4) every tick, `maybe_reconcile_camilla`, likewise. Chains 3 and 4 terminate at chain 1's call, so three `asyncio.timeout()` conversions cover all four — but only an enumeration finds them, which is the transferable part: **the gather/direct-await distinction plus "walk the loop's awaited chains, don't grep it for `wait_for`."** Two of these four hide behind names that read as coordinator bookkeeping. Pinned together (not one per module — the invariant is the loop's) by the `#2003` block in `tests/test_volume_observers.py`. Measured on 3.11.15, 5 trials × 3 tick offsets, pre-fix: `_run` immortal 15/15 through `selected_source`; both subprocess helpers returned *normally* 15/15 instead of cancelling. Post-fix: cancelled 75/75. On 3.12+ these pass either way, so the py3.11 CI leg is the only one that ever goes red |

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
fixture (and adds filter-theory sanity probes). The `js` CI job runs the node
check as part of the browser-module harness set; run it locally when touching
either implementation so parity failures land before CI.

## Sensitivity → level-trim parity (JS ↔ Python)

The /sound/ active-crossover form pre-fills a starting per-driver level trim
from the driver sensitivity gap (optimistic UI,
[`deploy/assets/sound-profile/js/active-speaker-ui.js`](../deploy/assets/sound-profile/js/active-speaker-ui.js)
`sensitivityTrimsFromGap`); the server re-derives the same fail-safe
authoritatively on save
([`jasper/active_speaker/baseline_profile.py`](../jasper/active_speaker/baseline_profile.py)
`_derive_corrections`, the `datasheet_trims` block).
[`tests/fixtures/sensitivity_trim_fixture.json`](../tests/fixtures/sensitivity_trim_fixture.json)
is the shared contract:

```sh
node scripts/check-sensitivity-trim-parity.mjs   # asserts the JS matches the fixture
```

`tests/test_active_speaker_baseline_profile.py::test_sensitivity_trim_matches_shared_parity_fixture`
asserts the Python source matches the same fixture. The `js` CI job runs the node
check alongside the PEQ parity check; run it locally when touching either
implementation so parity failures land before CI.

### JS behavioural harnesses bridged through pytest (node-on-runner reliance)

Some browser/Node modules are behaviourally tested by a Node harness that a
pytest test invokes via `subprocess.run([node, harness])` with a
`shutil.which("node")` skip-guard — e.g. `tests/test_relay_worker_js.py`,
`tests/test_capture_page_js.py` (the phone-mic capture relay), and the
pre-existing `tests/test_dialog_helper.py` / `tests/test_landing_page_html.py`.
This keeps the JS behavioural gate inside the **`pytest-matrix`** lane with no
extra CI wiring.

The load-bearing assumption: **`pytest-matrix` runs on `ubuntu-latest`, which
ships Node on `PATH`** (there is no `actions/setup-node` step in that job). The
pre-existing `js` job calls bare `node` and is green, which proves the runner
image provides it. If a future change gates these jobs behind an explicit Node
install, or a runner image drops Node, these tests flip to **green-by-skip** —
losing the JS coverage silently. If you touch that CI wiring, either keep Node
preinstalled on the pytest runner or move these harnesses to a job that installs
Node explicitly. (`scripts/check-js-syntax.sh` in the `js` job only
`node --check`s syntax — it does not run the harnesses.)

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

Repository-bound wake shell wrappers use one interpreter precedence contract:
the effective `PYTHON` value (a single executable token or path), the invoking
checkout's `.venv`, the main checkout's `.venv` when invoked from a linked
worktree, then `python3`. An explicit override is authoritative and fails
visibly if invalid; wrappers do not silently replace it. Resolution is anchored
to the wrapper's checkout, so calls from another working directory neither
select that directory's venv nor import an editable `jasper` package from a
different checkout.

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

"Standalone" there means the *invocation* is standalone, not the
dependencies: both scripts import `jasper` on the scoring path (for
the openWakeWord import guard — see
[`jasper/openwakeword_guard.py`](../jasper/openwakeword_guard.py)),
so run them under `/opt/jasper/.venv/bin/python` on a speaker or the
repo venv on a laptop. `--help` still works without either.

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

## USB microphone export latency artifact

`jasper-usb-mic-latency-artifact` samples the live `jasper-usbmic` status while
a computer is actively recording from `JTS Mic`. It rejects an idle or stale
window, then writes a schema-1 JSON record bound to the installed build,
descriptor revision, resolved software/chip export source, negotiated
XVF/PortAudio capture geometry,
realized ALSA writer geometry/target, and operator-supplied host application.
Use it for the optional Pi→computer microphone direction; it does not replace
the host→speaker click/capture harness below.

```sh
sudo /opt/jasper/.venv/bin/jasper-usb-mic-latency-artifact \
  --duration-seconds 30 \
  --host-os "macOS 15" \
  --host-app "CoreAudio / sounddevice" \
  --output /tmp/jts-usb-mic-latency.json
```

The active-only 120 ms doctor budget and the exact artifact interpretation are
canonical in
[`HANDOFF-usb-gadget.md`](HANDOFF-usb-gadget.md#toggling-and-choosing-the-computer-microphone-from-wake).
Aggregation waits at least 11 seconds and also proves 512 exact source-age
appends after the first post-start relay status, so delayed status reads cannot
admit history from before the run. Use the documented 30-second command for
review artifacts, and add `--require-pass` in automation.

---

## Route-latency click/capture harness

`jasper-route-latency-harness` (source: `jasper/cli/route_latency_harness.py`
+ `jasper/route_latency/`) is the click-in/capture-back measurement producer
[`jasper-route-latency-artifact`](../jasper/cli/route_latency_artifact.py)
needs — the artifact CLI binds measured latency to the live route identity
and writes the schema-v1 validation artifact, but it has never itself played
or captured audio; this harness is what generates real per-impulse evidence.
See [`docs/HANDOFF-usb-low-latency.md`](HANDOFF-usb-low-latency.md) for the
full quick/promotion end-to-end walkthrough and current route status.

**Architecture in one paragraph.** A host (Mac/Windows, no special
software) plays a generated click-track WAV into the JTS USB audio device.
A default-off ingress tap inside `jasper-fanin`'s own `hw:UAC2Gadget` DIRECT
capture — armed/disarmed over fan-in's control UDS (`TAP_ARM` verb,
`/run/jasper-fanin/impulse-tap.jsonl`) — timestamps each click the instant it
lands in the claiming route's own capture stream, binding the measurement to
route identity by construction. Since the aloop solo path was deleted
(2026-07-10), fan-in DIRECT capture is the sole USB ingress, so the fan-in tap
is the only ingress tap: the old `jasper-usbsink-audio` bridge tap on
`127.0.0.1:8781` is gone. The harness arms it automatically — `--tap-transport
auto` (default) reads fan-in `STATUS` and always resolves to the fan-in tap
(there is no usbsink bridge tap to fall back to); force it explicitly with
`--tap-transport fanin`. See
[`docs/HANDOFF-usb-low-latency.md`](HANDOFF-usb-low-latency.md) "Harness support
(`--tap-transport`)". This harness separately reads
the AEC bridge's always-on `raw0` leg on localhost UDP `:9879` (an
unprocessed XVF3800 room-mic capture — a corpus-only leg per
`jasper.wake_legs`, consumed here but never added as a wake-detection input)
to detect the same clicks acoustically at the far end. Each impulse's
latency is the tap→mic time delta (the click's whole physical journey — ring
dwell, fan-in, CamillaDSP, outputd, DAC, air, mic — elapses between the two
timestamps, so it is captured entirely by the subtraction), optionally minus
a fixed speaker→mic acoustic-distance compensation. This measures the
Pi-internal fan-in→CamillaDSP→outputd→DAC→speaker→air→mic path: `t_tap`
anchors at the Pi's UAC2 capture read (route ingress), so host-side and
USB-transfer buffering *before* that ingress is deliberately excluded — the
number is the route JTS owns, not the host's playback stack. The tap also
records the ring's pre-read fill depth per impulse as diagnostic context, but
that is not added to the latency (doing so would double-count the ring
dwell).

**Quick gate (p95 <= 40 ms, >=200 impulses, >=5 min — budget tightened
2026-07-11 to the certified electrical floor, see
`docs/HANDOFF-usb-latency-measurement.md` §1):**

Invoke every CLI by its absolute venv path (`/opt/jasper/.venv/bin/...`):
under `sudo` the venv `bin/` is not on `secure_path`, so a bare command name
won't resolve. (The `generate` WAV render is memory-heavy for the promotion
preset — see the note below — so prefer running `generate promotion` on the
laptop and copying the WAV to the Pi/playback host.)

```sh
# 1. Generate the click-track WAV + schedule (laptop or Pi, no daemon needed):
/opt/jasper/.venv/bin/jasper-route-latency-harness generate quick --out-dir /tmp/route-latency

# 2. On the Pi: run capture, then immediately play quick-click-track.wav
#    on the host into the JTS USB device, at a modest, comfortable volume
#    (start very quiet and confirm by ear — CamillaDSP's volume_limit 0 dB
#    ceiling is the hard safety floor either way; see AGENTS.md "COAH
#    quality bar" / the safe-volume doctrine).
sudo /opt/jasper/.venv/bin/jasper-route-latency-harness capture \
  /tmp/route-latency/quick-schedule.json \
  --out-dir /tmp/route-latency

# 3. Analyze the captured evidence and emit an artifact-feedable samples file.
#    Point --tap-events at the JSONL that `capture` printed it armed: the fan-in
#    DIRECT-capture path (/run/jasper-fanin/impulse-tap.jsonl) — the sole ingress
#    tap since the aloop solo path (and its /run/jasper-usbsink tap) were deleted
#    2026-07-10. The `run`
#    one-shot below threads this automatically — only the split capture/analyze
#    flow needs the flag, since `analyze` runs offline with no tap to probe.
/opt/jasper/.venv/bin/jasper-route-latency-harness analyze \
  --tap-events /run/jasper-fanin/impulse-tap.jsonl \
  --mic-detections /tmp/route-latency/mic-detections.jsonl \
  --route-health-snapshot /tmp/route-latency/route-health-snapshot.json \
  --out-dir /tmp/route-latency \
  --duration-seconds 360

# 4. Feed the real artifact CLI (see docs/HANDOFF-usb-low-latency.md):
sudo /opt/jasper/.venv/bin/jasper-route-latency-artifact \
  --samples /tmp/route-latency/latency-samples.json \
  --duration-seconds 360 \
  --harness-id jts-click-capture-v1 \
  --route-health-ok   # only if step 3's printed deltas justify it
```

Or run steps 2-3 in one shot with `run` (`generate` still stays separate,
since the WAV only needs generating once). `run` loads the schedule file
directly, so it derives duration and jitter itself — it does not take
`--duration-seconds`/`--impulse-spacing-jittered` (those exist only on
`analyze`, which has no schedule file to read them from):

```sh
sudo /opt/jasper/.venv/bin/jasper-route-latency-harness run \
  /tmp/route-latency/quick-schedule.json \
  --out-dir /tmp/route-latency \
  --invoke-artifact
```

**Promotion gate (p99 <= 42 ms, >=1000 jittered impulses, >=30 min):**
identical flow with `generate promotion` instead of `generate quick`. On
`analyze`, add `--impulse-spacing-jittered` to declare that fact to the
artifact CLI (`run` needs no such flag — it reads jitteredness straight off
the loaded schedule):

```sh
# generate promotion on the laptop (memory-heavy render — see below), then
# copy promotion-click-track.wav to the playback host:
/opt/jasper/.venv/bin/jasper-route-latency-harness generate promotion --out-dir /tmp/route-latency
sudo /opt/jasper/.venv/bin/jasper-route-latency-harness run \
  /tmp/route-latency/promotion-schedule.json \
  --out-dir /tmp/route-latency \
  --invoke-artifact \
  --require-pass
```

**Getting the WAV to the playback host.** The click-track WAV is played by a
human on the Mac/Windows host (no JTS software runs there). Generate it where
it's convenient, then transfer it to that host — e.g. `scp` from the Pi, or
generate on the laptop and drop it on the host directly — and open it in any
media player, routing output to the JTS USB audio device. `render_wav` streams
the file one second at a time so memory stays bounded (~192 KB), but the
promotion track is still ~415 MB on disk; a laptop is the comfortable place to
generate it (the 1 GB Pi is busy running the audio stack under test).

**Route-health honesty.** `capture` snapshots the two live route owners — the
fan-in and outputd `STATUS` sockets — before and after the capture window
(writing `route-health-snapshot.json`);
`analyze` then diffs that file, prints every nonzero counter delta, and states
whether `--route-health-ok` on the artifact CLI *would* be justified — it
never asserts that for the operator. Both surfaces and the expected USB DIRECT
lane/counter shape must be present in both snapshots; incomplete telemetry is
not a clean window. The verdict disqualifies on ANY nonzero change to a curated
route-health counter (a NEGATIVE delta means the daemon restarted mid-window —
also unclean): the fan-in output xrun, the outputd content/DAC xruns, and any
fan-in USB-resampler unlock/silence/overrun or per-lane xrun. The retired
bridge/state surface is not route-health evidence. Read the printed deltas before deciding.

**Mic source.** Default is `udp:9879` (the AEC bridge's `raw0` leg — requires
an XVF3800 present with 6-channel firmware and the bridge running; the
harness fails loudly on a read timeout rather than hanging if nothing is
feeding the socket). `--mic alsa:<device>` is the fallback for boxes without
an XVF3800 or when pointing at a dedicated measurement mic.

**Clock discipline.** Both the Rust tap and this harness's mic reader
timestamp every event against `CLOCK_MONOTONIC` **freshly per packet/period**
— never a single stream-start anchor — because the mic's USB clock drifts
against the Pi's monotonic clock (~180 ms over a 30-minute run at a typical
100 ppm crystal tolerance). `tests/test_route_latency_harness.py` has a
drift-injection test proving this bounds the error to about one packet's
uncertainty regardless of run length.

**Pairing.** Nearest-match within a bounded window; a tap or mic detection
with more than one plausible partner is rejected as ambiguous rather than
guessed at, and the tool refuses to emit an artifact-feedable file below a
match-rate floor (default 90% of tap events).

**Test coverage:**
`tests/test_route_latency_click_track.py`,
`tests/test_route_latency_impulse_detect.py`,
`tests/test_route_latency_pairing.py`,
`tests/test_route_latency_harness.py`, and
`tests/test_usbsink_impulse_tap_contract.py` (the JSONL/control-UDS contract this
harness's Python side shares with the Rust tap it does not itself implement).

---

## AEC / bridge forensics

Investigative scripts for diagnosing AEC degradation, ref-path bugs,
sibilant tearing, etc. Not all are checked into the repo — some live
in `/tmp/` during a specific investigation and get promoted to
`scripts/` when stable.

| Tool | Status | Purpose |
|---|---|---|
| [`scripts/verify-ref-no-silence-bug.sh`](../scripts/verify-ref-no-silence-bug.sh) | in repo | Verifies the ref-path fixes from PRs #150 / #154 / #157 are active on the deployed build (resampler HF loss, silence fallback, drain-newest dup-frame bug). Run after any deploy that touched the bridge. |
| [`scripts/aec-probe-latency.sh`](../scripts/aec-probe-latency.sh) | in repo | Injects a chirp through `correction_substream`, captures outputd's final speaker-reference UDP stream plus one selected XVF3800 capture channel, and reports the reference-to-mic lag. Use `MIC_CHANNEL=0` or `MIC_CHANNEL=1` for chip ASR beams and `MIC_CHANNEL=2` for the raw channel used in older timing comparisons. **Ring-armed boxes: silently measures silence** — plays into `correction_substream` unconditionally (#2767). |
| [`scripts/aec-probe-xvf-ref-level.sh`](../scripts/aec-probe-xvf-ref-level.sh) | in repo | Bounded diagnostic for chip-reference legality and level. It injects a short chirp through `correction_substream`, captures outputd's final speaker-reference UDP stream plus all XVF3800 capture channels, reports L/R reference parity, clipping, chip-ref 16 kHz mono model, `AUDIO_MGR_REF_GAIN` estimate, per-channel RMS/correlation, and selected XVF profile readbacks. See [`docs/AEC-DIAG-06-xvf-format-level-profile.md`](AEC-DIAG-06-xvf-format-level-profile.md). **Ring-armed boxes: silently measures silence** — plays into `correction_substream` unconditionally (#2767). |
| [`scripts/aec-probe-timing.py`](../scripts/aec-probe-timing.py) | in repo | Diagnostic-only timing probe for explicit reference sources: `outputd_udp` and `chip_ref_tee`. Writes JSON/CSV/Markdown plus short WAV artifacts, labels mic channels (`ch0` conference/beam, `ch1` ASR beam, `ch2` raw mic0), snapshots outputd state, and can run outputd period/buffer profiles `default`, `1024/2048`, and `512/1024`. See [`docs/AEC-DIAG-03-timing-probe.md`](AEC-DIAG-03-timing-probe.md). **Ring-armed boxes: silently measures silence** — plays into `correction_substream` unconditionally (#2767). |
| [`scripts/aec-probe-pinknoise.sh`](../scripts/aec-probe-pinknoise.sh) | in repo | Runs the bridge with stationary pink noise as the far-end signal and logs RMS attenuation per 5-second window. Pink noise is AEC3's best case (stationary, broad-spectrum), so the plateau here is the engine's upper-bound attenuation for the setup — compare against music-as-far-end, typically 5–10 dB worse. Stops shairport-sync and jasper-voice for the run and restores them after; plays loud-ish noise at whatever the remote's volume is set to. **Ring-armed boxes: silently measures silence** — plays into `correction_substream` unconditionally (#2767). |
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
entirely off the live JTS audio path; cleans up after itself. The last two
rows are a later, narrower gate — the **S0-sync** bench, which characterises an
snd-aloop re-entry seam rather than P0's buffer/codec question — and carry
their own scope caveat.

| Tool | Methodology | When to use |
|---|---|---|
| [`scripts/multiroom-spike.sh`](../scripts/multiroom-spike.sh) | Laptop-side SSH harness (`--setup`/`--sweep`/`--record-chirp`/`--teardown`). Stands up a throwaway `snapserver` + `snapclient`s (leader + 2nd Pi + Pi Zero sub) reading a hand-fed FIFO, sweeps buffer `{150,300,500,800,1200}` ms × codec `{pcm,flac,opus}`, optional `--netem` WiFi stress (`wlan0` only). Results in `multiroom-spike/`. | Before P1: pick the buffer/codec that holds the p99<5 ms L/R bound on WiFi. |
| [`scripts/multiroom-spike-measure.py`](../scripts/multiroom-spike-measure.py) | Pure-stdlib analyzer. `software` (snapserver JSON-RPC latency spread), `acoustic` (single-mic cross-correlation of a click track — ground-truth inter-speaker offset), `summarize` (PASS/FAIL vs target + RAM/CPU + recommended cell). | Analyze a spike run; the acoustic mode is the authoritative comb-filtering check. |
| [`scripts/s0-sync-bench.sh`](../scripts/s0-sync-bench.sh) | Laptop-side SSH harness for the S0-sync de-risk gate (throwaway, not product). Stands up two throwaway **active** followers whose seam is `snapclient` → snd-aloop → crossover-only CamillaDSP → real DAC, feeds a 1 Hz broadband click, and soaks for the xrun / CPU / temp budget. Answers the one thing the dumb-follower path deliberately avoids: the loopback re-entry and its `rate_adjust`-no-resampler clock seam. | **Not evidence about a ring-backed seam.** The bench stands up its own throwaway snd-aloop rig and characterises the aloop clock-tracking mechanism — CamillaDSP nudging the `PCM Rate Shift` control on its snd-aloop *capture* device. A ring PCM is an ioplug and exposes no such mixer control, so a green run here answers nothing about a ring transport. See #2766 for the bonded grouping round-trip's move onto a ring, and #2768 for the owed ring-seam de-risk. |
| [`scripts/s0-sync-measure.py`](../scripts/s0-sync-measure.py) | Pure-stdlib analyzer, the measurement half of the bench. `acoustic --wav` (single-mic autocorrelation of the broadband click → inter-speaker arrival offset) and `soak --dir` (parse soak logs for snd-aloop xrun totals + CPU/temp/throttle/Pss, run the acoustic estimate over every periodic capture, report p50/p95/p99/max raw *and* placement-detrended, count resync jumps, emit the combined PASS/FAIL). | Analyze an `s0-sync-bench.sh` run. Same scope caveat as the row above — the numbers describe an snd-aloop rig, not a ring-backed seam (#2768). |

**Safety note — the P0 spike rows only:** `multiroom-spike.sh` plays a test
track/music straight through a throwaway `snapclient`, **bypassing**
CamillaDSP's `volume_limit: 0.0` ceiling, and its leader-side client can
contend with `jasper-outputd` for the DAC. Run it with the JTS audio daemons
stopped (or on bring-up hardware), and set a conservative volume before the
first sweep. See [`HANDOFF-multiroom.md`](HANDOFF-multiroom.md) §8. The S0
bench is not in that class — it drives the DACs outside outputd, but its
throwaway CamillaDSP keeps `volume_limit: 0.0`, negative-only gains, and a
protective Layer-A high-pass. It still needs exclusive DAC ownership, so
`--up` stops the live stack on both Pis and `--teardown` restores it.

---

## Pi-side diagnostics

Live Pi state without modifying anything:

| Tool | What it gives you |
|---|---|
| `sudo /opt/jasper/.venv/bin/jasper-doctor` | Codified BRINGUP smoke tests — first command to run when something's broken. Checks run with bounded parallelism while probes that would perturb one another stay serialized — ALSA opens against each other, and the voice-provider import probe against the memory-headroom sample — so the flat report keeps stable ordering without summing every subprocess timeout. Also re-checks output hardware observed-vs-active state plus presence/hashes for opaque runtime model files that JTS stages directly (required openWakeWord assets, the active wake model when registry-pinned, and configured DTLN ONNX stages when DTLN is enabled). |
| `curl -s http://jts.local/system/diagnostics.json \| jq` | Management dashboard doctor snapshot. It serves the last root-fidelity `jasper-doctor --json --out` result immediately and schedules a background refresh when the cache is stale or missing, so the dashboard does not block on a live doctor run. |
| `curl -s http://jts.local:8780/state \| jq` | Cross-daemon JSON snapshot (voice / audio including `output_hardware` / AEC runtime profile / renderers). Fail-soft per section. |
| `sudo /opt/jasper/.venv/bin/jasper-route-latency-artifact --samples <latencies.json> --duration-seconds <s> --route-health-ok` | Writes the doctor-consumed `route_latency` validation artifact from measured USB click/capture latencies and the live `jasper.audio_runtime_plan` route identity. It is not the measurement harness; only pass `--route-health-ok` when the same window had complete, clean fan-in/outputd telemetry. |
| [`scripts/fetch-pi-logs.sh`](../scripts/fetch-pi-logs.sh) | Pulls journals + previous-boot OOM/watchdog/reboot forensics + monotonic boot timelines + configs + ALSA state to `./logs/`, redacting env-style secrets before write. Read the `*-latest.*` symlinks plus `log-noise-summary-latest.txt` for line counts and repeated-message fingerprints. |
| [`scripts/journal-review.sh`](../scripts/journal-review.sh) | Read-only journal-health digest run ON the Pi for the last `--since` window (default `7 days ago`): journal disk usage + retention/truncation, per-unit auto-restart counts, warning+ volume by unit, top `event=<domain.action>` keys with a week-over-week DELTA + never-seen-before keys, OOM/watchdog fingerprints, and repeated-message fingerprints (reuses `fetch-pi-logs.sh`'s fingerprinter). `--json` for machine consumption. Bounded (windowed journalctl + awk, no full-journal scan); always exits 0; the only write is its own `/var/lib/jasper/journal-review.state.json` week-over-week baseline. |
| [`scripts/pi-run-diagnostic.sh`](../scripts/pi-run-diagnostic.sh) | Safe lane for ad-hoc Pi-side diagnostics: wraps a command in `systemd-run` with memory/runtime bounds and a positive `OOMScoreAdjust`. |
| [`scripts/pi-system-soak.sh`](../scripts/pi-system-soak.sh) | Convenience wrapper for a bounded `jasper-system-soak` run on the active Pi; writes a versioned JSON resource artifact. |
| [`scripts/tail-pi-logs.sh`](../scripts/tail-pi-logs.sh) | Live tail of all `jasper-*` units |
| [`scripts/jasper-trace.sh`](../scripts/jasper-trace.sh) | Filtered live tail showing only `event=` lines (duck transitions, source preempts, volume routing, wake/turn boundaries) |
| [`scripts/airplay-latency-probe.sh`](../scripts/airplay-latency-probe.sh) | Read-only capture of the AirPlay latency budget + AP2 stream type a real sender negotiates (from shairport's `log_verbosity = 2` journal), so you know whether a bonded leader's downstream delay fits inside it (free vs. tight regime). No config change, no restart. Rationale: [`HANDOFF-airplay.md`](HANDOFF-airplay.md). |
| `ssh pi@jts.local sudo bash /home/pi/jts/scripts/pi-bundle.sh` | One-shot full diagnostic dump as a tarball |
| `jasper-correction-bundle inspect <session> --recompute` | Validate a copied room-correction bundle, summarize confidence/runtime evidence, and replay raw captures into derived curves |
| `jasper-correction-bundle export <session> --output <dir>` | Write REW-friendly `.frd` / `.txt` curves and impulse-response WAVs from a room-correction bundle |
| `jasper-active-speaker startup-template <preset.json> --playback-device <device> --output <file.yml>` | Write a muted/protected active-speaker startup template and run `camilladsp --check` when available. It does not load or apply the config. |
| `jasper-active-speaker runtime-safe-graph [--write-statefile] [--json]` | Classify the saved output topology against the current/staged CamillaDSP graph and select the only legal persisted outputd statefile target. Flat full-range graphs are allowed only for topology shapes that can safely receive them; active/protected topologies require a validated all-muted active startup graph, and are parked silent (exit 0) when none has been staged yet — printing any topology blockers rather than refusing on them (#2145). A staged graph that exists but fails its safety proof still exits 1. |
| `jasper-active-speaker path-audit --requirements` / `path-audit <evidence.json>` | List or evaluate the active-speaker audible-path safety checklist. Operator evidence can satisfy requirements but does not permit active config loading; `ok_to_load_active_config` stays false until future hardware-probe-backed evidence passes. |
| `jasper-active-speaker path-probe [--topology <file.json>] [--current-config <file.yml>] [--output <file>] [--json]` | Generate no-audio startup-load path-safety evidence — the hardware-probe-backed evidence `environment-probe` and the load gate require. `--current-config` names the config to treat as the rollback target; **omitting it writes blocked evidence**, so the gate stays shut rather than passing on a probe that had no rollback target. Writes to `JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE` or `/var/lib/jasper` unless `--output` overrides. |
| `jasper-active-speaker environment-probe [--config <file.yml>] [--json]` | Read ALSA playback devices and the current/provided CamillaDSP config/statefile shape without playback, reloads, or mutation. Blocks the load gate unless the config is an active startup candidate, `camilladsp --check` passes, and hardware-probe-backed path-safety evidence is provided. Also reports the read-only safe-playback environment block; audible authority lives in the product routes below, not in the probe itself. |
| `jasper-active-speaker baseline-reemit [--topology <file.json>] [--applied-baseline-state <file.json>] [--endpoint ring] [--statefile <file.yml>] [--out <file.yml>] [--force] [--json]` | Re-emit this box's roleful boot graph against a playback endpoint, publishing over the live artifact and repointing the statefile. **`--endpoint ring` is the FIRST step of the active-ring arm and has no rollback** — the reconciler derives its endpoint marker from the loaded graph, so the graph must move first; `ring` is the only choice, and omitting `--endpoint` keeps whatever the box already resolves. Two graph classes are accepted: `approved_active_runtime` (a commissioned box's applied baseline, re-emitted from its immutable snapshot) and `all_muted_active_startup` (a mid-commission box's all-muted anchor, re-staged from its own design draft and crossover preview); an applied baseline wins when both are present, and any other class — parked, unrecognised, or a topology with no roleful outputs — is refused **by name** rather than guessed at. `--out` is the preview: it writes the YAML there and touches nothing else — no live artifact, no canonical copy, no statefile. `--force` re-stages the anchor even while a per-driver commissioning load is active, and is refused by default because that anchor is what `commission-rollback` and the ramp's `abort` / `ack --outcome too_loud` reload — moving it mid-load re-points the operator's own stop control. |
| `jasper-active-speaker commission-load --group <id> --role <role> [--preset <file.json>] [--topology <file.json>] [--dry-run] [--force] [--json]` | Load a per-driver commissioning config into the **running** CamillaDSP graph, arming one driver of the single active group at the protected floor — **silent**; audible level is `commission-ramp`'s job, one gated step at a time. Default preset is the saved crossover preview, matching protected staging; `--preset` is the preset-fallback override. `--dry-run` runs the guarded preflight only — it writes the candidate config, loads nothing, and emits no audio. `--force` re-arms over an already-active commissioning load (single-flight override). |
| `jasper-active-speaker commission-rollback [--json]` | Re-mute: reload the all-muted staged config, ending a per-driver commissioning load and returning every channel to muted. It always ends the operator's safe-playback session, and on a **proven** rollback (`status == "rolled_back"`) it also clears the ramp's pending step, keeping the group and the woofer-before-tweeter ordering memory. A `blocked` / `rollback_failed` rollback keeps the pending step — the driver may still be audible, so its ACK is still owed (#2669). |
| `jasper-active-speaker commission-ramp step --group <id> --role <role> [--preset <file.json>] [--topology <file.json>] [--json]` | Take one gated audible gain step on the armed driver, woofer before tweeter. Each step loads the protected one-driver graph, injects a bounded tone through the commissioning lane, and rolls back on tone failure. Refused unless every gate holds, including the prior step's operator ACK. |
| `jasper-active-speaker commission-ramp ack --outcome {heard_correct_driver,heard_wrong_driver,silent,too_loud} [--json]` | Record the operator's verdict for the pending audible step. `heard_correct_driver` confirms and advances the confirmed-role ordering authority; `too_loud` / `heard_wrong_driver` re-mute; `silent` allows a louder retry. Returns `no_pending_ramp_step` (rc=1) when no step is pending — including after a rollback cleared one, so a verdict can never confirm a driver nobody could have heard. |
| `jasper-active-speaker commission-ramp status [--topology <file.json>] [--json]` | Read-only: print the commission-load, ramp, and per-driver floor state. `--topology` merges durable confirmed-role evidence for the armed group; the handler reads it on every box that has ever armed a driver, because the armed target outlives a rollback (#2667). |
| `jasper-active-speaker commission-ramp abort [--json]` | Re-mute mid-ramp: roll back to the all-muted staged config and reset the ramp state. Stops the tone and ends the safe-playback session. |
| `/sound/active-speaker/{environment,safe-playback,commissioning-view,design-draft,channel-identity,calibration-level,stop,commission-state,commission-load,commission-rollback,commission-ramp-step,commission-ramp-ack,commission-ramp-abort,summed-test,summed-validation,baseline-profile,baseline-profile/apply}` | Web active-speaker status/session/design/identity/level/test/commissioning surface. `environment`, `safe-playback`, `commissioning-view`, `design-draft`, `channel-identity`, `calibration-level`, `commission-state`, `baseline-profile`, and related status routes are read-only GETs where exposed; `design-draft`, `stop`, `channel-identity`, `calibration-level`, the `commission-*`, summed validation, and baseline apply routes are CSRF-protected POSTs from `/sound/`. Active 2/3-way groups use `commission-load` + `commission-ramp-step`/`ack`/`abort`; each ramp step loads the protected one-driver graph, injects a bounded tone through the commissioning lane, and rolls back on tone failure. Passive/full-range groups have no separate active driver test in the product UI. `design-draft` persists operator driver names, notes, bounded research JSON, and a saved topology snapshot as non-authoritative evidence; it does not load CamillaDSP, apply filters, authorize playback, or emit sound. Generic `aplay` tone playback is explicit lab mode only and requires `JASPER_AUDIO_LAB_TONE_BACKEND=aplay` and `JASPER_AUDIO_LAB_TEST_PCM` pointing at a dedicated non-daemon test PCM. Product outputd/CamillaDSP lanes are forbidden as direct test writers. The list is owned by `FORBIDDEN_TEST_PCM_TOKENS` in `jasper/active_speaker/playback.py` — a case-insensitive substring test covering every outputd program/content lane, `jasper_out`, `outputd_dac`, and all three of the COUPLING's ring PCMs, the ACTIVE ring included (it needs its own entry because `jts_ring_playback` is not a substring of `jts_ring_active_playback`). The renderer-lane rings and the grouping-ingress ring are deliberately absent on the consequence asymmetry the tuple's own comment states — they are ingress into fan-in / CamillaDSP, not a sink past the crossover. Read the tuple rather than a restatement here. `outputd_active_content_playback`/`outputd_active_content_capture` no longer resolve to a real PCM since P9-C deleted their `asoundrc.jasper` definitions, but the ban holds for a re-introduction or a rolled-back box that still names them. No endpoint changes normal listening volume. |
| `rust/jasper-dual-dac-lab/target/release/jasper-dual-dac-lab probe` / `run` | Lab-only dual Apple USB-C DAC validator. `probe` is passive. `run` opens two serial-pinned direct `hw:` PCMs, writes silence first, caps level, and aborts both outputs on xrun/suspend/disconnect/delay divergence. Not installed as a product daemon. |

## Correction capture diagnostic

[`scripts/capture-correction-diagnostic.py`](../scripts/capture-correction-diagnostic.py)
is a laptop-side observer for one browser/relay correction run. It does not
start a measurement or change gain. It records synchronized UMIK blocks,
`/state`/crossover timelines, and—when `--ssh-host` is supplied—a bounded
snapshot of the speaker's persisted gain/DSP files while the correction lane is
active. The SSH archive runs off the capture loop with a 15-second timeout, so a
stalled Pi cannot stop mic draining. Raw room audio stays under the gitignored
`captures/` tree; the directory is `0700` and files are `0600`.

Analyze the bundle with
[`scripts/analyze-correction-diagnostic.py`](../scripts/analyze-correction-diagnostic.py).
It reports actual tone/sweep presence, clipping, callback errors, observed
speaker gain, and the configured target-window shortfall. `--state-only`
bundles remain valid speaker-state evidence but intentionally report that no raw
mic analysis was possible. Pass the actual tone frequency and policy thresholds
to the capture command when they differ from its defaults; those values are
stored in the manifest and consumed by the analyzer rather than re-guessed.

See [CLAUDE.md](../CLAUDE.md) "Debugging — fetch evidence before
guessing" for the canonical recipes.

---

## Attempts-loop replay

`jasper-active-speaker-attempts-replay`
([`jasper/cli/active_speaker_attempts_replay.py`](../jasper/cli/active_speaker_attempts_replay.py),
kernel in [`jasper/active_speaker/attempts_loop.py`](../jasper/active_speaker/attempts_loop.py))
answers one question: **would the tuning loop have called this an improvement,
or noise?** It feeds recorded attempts to the S3 improve/stop policy and writes
down the decision at every step, with the numbers each decision used.

Fully offline — no hardware, no playback, no Pi, no microphone. It reads JSON
some earlier session already analysed, so it is safe to run on a laptop from a
checkout and costs nothing but a few milliseconds.

Two banks, either or both in one invocation:

- `--repeat-floor <dir>` — an unchanged-profile repeat study
  (`captures/repeat-floor-20260731/`). Nothing was tuned between captures, so
  no consecutive change may reach the claim floor. This is the **control**: it
  is the only bank that can produce a finding, because it is the only one that
  knows the right answer in advance.
- `--sessions <dir>` — recorded commissioning bundles
  (`captures/r11-loop-proof-corpus/sessions/`), replayed oldest-first as an
  improvement arc, graded per driver role.

```sh
PYTHONPATH=. .venv/bin/python -m jasper.cli.active_speaker_attempts_replay \
  --repeat-floor captures/repeat-floor-20260731 \
  --sessions captures/r11-loop-proof-corpus/sessions \
  --out captures/r11-loop-proof-20260802
```

Writes `report.json` (machine-readable), `README.md` (the attempt table and
prose), and one model-error store per run into `--out`. **Replay never writes
to the production store** at `/var/lib/jasper/active_speaker_model_error.json`
— every store path is injected under `--out`, and a test pins that.

Exit codes are three-state, matching [Offline emit loop](#offline-emit-loop)'s:
`0` consistent, `1` a finding (the loop read a change on a profile nobody
touched), `2` no verdict (bad inputs, or nothing gradeable). Absence of
evidence never reads as a pass.

The banked proof run is `captures/r11-loop-proof-20260802/`. Read that
directory's `README.md` for what the loop decided on real data, including the
honest limits — chiefly that the repeat-floor run derives its floor from the
same pairs it grades, which makes it a self-consistency demonstration rather
than a validation of the threshold.

---

## Severed-twin replay

[`scripts/severed-twin-replay.py`](../scripts/severed-twin-replay.py) re-fits a
banked crossover-v2 session twice — once as recorded, once with the cloud
verdict cut (`excluded_bands_hz=None`, the production `cloud is None` branch)
— and diffs the two fits. It answers **did the null evidence actually bind,
and what does the fit do without it?** It cannot answer whether the wired
answer was *right*; the corpus carries no ground truth. For that, see the
synthetic scenarios in
[`tests/test_crossover_v2_boost_scenarios.py`](../tests/test_crossover_v2_boost_scenarios.py),
which inject the defect so the correct answer is known before the flow runs.

Fully offline — laptop, no Pi, no microphone. It reads banked WAVs and JSON
and runs the shipped analysis over them.

```sh
PYTHONPATH=. .venv/bin/python scripts/severed-twin-replay.py \
  --bank captures/wo0-retrospective-20260729/reanalysis-data/pi-pull \
  --ring captures/ring-snapshot-20260730 \
  --calibration captures/flat-linearization-20260725/umik2-cal/umik2-b7343c0c625b.txt
```

**Two properties worth knowing before you trust its output.**

*It binds a capture to a session by content, never by filename.* A raw-ring
sidecar carries no session id, and phase tokens changed era to era — selecting
on one produced two false claims in PR #2070's review. The tool matches the
sidecar's recorded `diagnostic` block against the banked `candidate.json`'s
`analysis` block and requires a unique hit.

*It validates itself and refuses when it cannot.* That same sidecar block is
the analysis as PERFORMED, so it is ground truth for the replay's own fidelity.
The tool reproduces 19 of its values and prints no fit unless every one matches
(exit 1 otherwise, and a field the sidecar never recorded is a refusal too), so
an era-drifted reconstruction fails loudly instead of quietly re-reading the
evidence. Its own module docstring states the two things that gate does **not**
reach — the post-T2 delay refinement, and the calibration sign convention,
which the tool *derives* from the product's own mic registry but which no
banked diagnostic can *check*. Read it before extending the tool to a delay or
level question.

The **fit engine is today's, the capture analysis is the banked era's** — those
are different claims and the tool only makes the second. A banked candidate's
filters were produced by whatever build recorded it, so its numbers and a fresh
replay's will differ wherever the fit has moved since; that is cross-era
evolution, not a fidelity failure, and the wired-vs-severed diff is a
within-run comparison precisely so it does not depend on the difference.

---

## Metric-honesty views

Severed-twin and the committed replay both ask *was the fit bound by the right
evidence*. This one asks a different question of the same banked receipts:
**is the pooled flatness number answering the question a listener asked?**

Two properties of the shipped pooling say no, and both are visible in the
receipt itself. The cloud grades on the linear `rfft` axis, so a band's graded
bin count tracks its width in hertz rather than octaves — on the 2026-08-18
arm-run that is 5,462 bins in the one octave 8–16 kHz against 1,121 across the
2.485 graded octaves of 250 Hz–2 kHz, a 12.1:1 per-octave overweight. And
`combine_positions` is an unweighted power mean, so a coverage-edge position
enters the headline with the same weight as the listening-seat one.

[`scripts/render-metric-views.py`](../scripts/render-metric-views.py) re-reads
every `cloud_verify.json` under a receipts tree without those two properties,
printing the result beside the number the product shipped:

```sh
PYTHONPATH=. .venv/bin/python scripts/render-metric-views.py \
    captures/xover-armrun-2026-08-18/receipts \
    --walk-logs captures/xover-armrun-2026-08-18/logs \
    --json /tmp/metric-views.json
```

Every **measurement** in the output comes from
[`jasper/active_speaker/flat_spec_views.py`](../jasper/active_speaker/flat_spec_views.py)
— product code, pure, pinned by
[`tests/test_flat_spec_views.py`](../tests/test_flat_spec_views.py) — so the
tool cannot print a residual, offset, or weight the product will not later
compute identically. The script owns what is lab-only: walking the tree,
rehydrating a persisted `FlatSpecReport`, joining positions to the walk log
that drove them, and two presentation-layer subtractions the table needs and
no consumer does — the `gap` column, and the bins-per-octave `ratio` in the
band-weight block. Both are differences of published numbers, and both embed
a *display* choice (which "other" role to show when there are several) that
would become product policy if it moved onto the result types.

Three views, none of which grades anything: `log_pooled_residual` re-pools the
report's own per-band figures with equal weight per octave;
`role_split_flatness` reports on-axis and off-axis as separate numbers and
never averages them; `directivity_table` normalises every position to the
on-axis reference and emits a JSON table a prescriber can consume. The
session's one verdict stays the report's `overall_passed` — a test fails the
build if any view grows a `passed` field.

`--walk-logs` is optional and best-effort. The cloud does not bank a numeric
microphone angle, only a role, so angles are recovered by joining
`(index, attempt, role)` against the walk driver's `released …` lines.
`(index, attempt)` alone is **not** unique — on the arm-run every arm carries
the same four pairs and eleven of thirteen logs contain all four, at two
different angle assignments. When the covering logs disagree the join is
declined and every view degrades to role-only, which is honest; a wrong angle
would be a plausible-looking lie on every row.

---

## Harmonic-distortion replay

[`scripts/harmonic-distortion-replay.py`](../scripts/harmonic-distortion-replay.py)
reads **H2 and H3 versus frequency** out of MEASURE captures that are already
banked. No new recording, no Pi, no microphone: every JTS sweep is
Novak-synchronized, so the harmonic images have always been sitting at exact
pre-arrival offsets in each deconvolution. The math lives in the product module
[`jasper/audio_measurement/distortion.py`](../jasper/audio_measurement/distortion.py);
this script is the lab driver over a banked corpus.

```sh
PYTHONPATH=. .venv/bin/python scripts/harmonic-distortion-replay.py \
  --state captures/xover-series2-2026-08-17/series2-state-r1b-preapply.json \
  --captures captures/xover-series2-2026-08-17/e0-r1b \
  --dumps captures/xover-series2-2026-08-17/dumps-r1b \
  --calibration captures/flat-linearization-20260725/umik2-cal/umik2-b7343c0c625b.txt
```

**It needs a corpus with per-capture sidecars.** The series-2 rounds have them
(`dumps-r1b/`, `dumps-r2/`); the 2026-08-18 armrun does not, so it cannot be
fidelity-gated and the tool will bind nothing there.

**What the output means, and what it cannot mean.** Every number is *dB below
the fundamental at the same excitation frequency, at the drive this capture
used*, printed next to that drive in dBFS — the corpus records no SPL anywhere,
so there is no absolute figure to be had. A ratio is a fraction, so it rises
wherever the FUNDAMENTAL dips: the table carries a fundΔ column (pooled
fundamental re its own band median) and the summary names the bin where the
harmonic's *absolute* energy peaks — read a ratio peak against both before
attributing it to the driver. Each row also carries a **measured noise floor**,
taken from a phantom window between the harmonic images where no image can be;
a value a majority of sweeps read within 6 dB of their own floors is starred
and is an upper bound on the driver, not a reading of it. The tweeter comes
back 100% floor-limited on both banked rounds, because MEASURE solves its gain
for room SNR and lands it ~27 dB below the woofer in stimulus gain (~17 dB at
the capture).

**Two band edges bite, and neither is Nyquist.** Order *N* is only real up to
`f2/N` — the deconvolution divides by `|X|² + ε` and the sweep puts no energy
above `f2`, so harmonic products above it are annihilated along with the noise.
A 150–4000 Hz woofer sweep therefore yields honest H2 to 2 kHz and H3 to
1333 Hz. The bottom `BAND_EDGE_TRIM_OCTAVES` (0.25 oct) is trimmed because the
sweep's fade-in puts an excursion at `f1` that a provably-linear synthetic path
shows sitting 25.8 dB above the floor.

**Why the production deconvolution window cannot be used.**
`program_analysis.DECONV_PRE_GUARD_S` is 0.25 s and the H3 image leads the linear
IR by `L·ln 3` ≈ 1.34 s, so at that window every image has wrapped off the front
of the circular deconvolution. The read re-deconvolves the same bytes at
`distortion.required_pre_guard_s` — an analysis-side value for a parameter
`_deconvolve_window` already exposes. Production behaviour is untouched, and
`tests/test_audio_measurement_distortion.py` pins the relationship in both
directions.

**It validates itself twice and refuses when it cannot.** The program is rebuilt
from the round's banked `gain_plan_db` and must reproduce the session's recorded
`program_id` (a SHA-256 over the whole schedule, so a match proves the sweep `L`
the offsets derive from); the two parameters no artifact records — the session
volume, and the courtesy-prelude vintage whose MEASURE value #2715 flipped —
are *solved* against that same id, shipped rule first, and each run names which
prelude reproduced. Then the shipped `analyze_program_capture` is
re-run and its diagnostics compared on whatever gate fields the sidecar
carries — fail-closed: a sidecar carrying none of them is refused rather than
read ungated, and the summary prints the count actually compared, never the
field-list length. `max_residual_samples` and
`glitch_detected` are deliberately excluded — D7 (`b98e9380f`) replaced the
estimator behind both *after* this corpus was banked, so comparing them would
report a product improvement as a broken reconstruction. A capture whose banked
`glitch_detected` disagrees is read but **disclosed by name**.

---

## Committed incident replay

Severed-twin above needs the gitignored bank on the machine running it, so it
cannot guard anything in CI. When an incident's defect is worth holding still
across future changes, the other shape is a **committed, minimized fixture plus
a characterization test** — the 2026-08-10 jts3 crossover incident (#2291) is
the worked example.

[`scripts/derive-crossover-incident-fixture.py`](../scripts/derive-crossover-incident-fixture.py)
reduces the 93 MB bank at `captures/jts3-incident-20260810-issue2291/` to ~17 KB
of JSON under
[`tests/fixtures/crossover_v2_incident_20260810/`](../tests/fixtures/crossover_v2_incident_20260810/),
and
[`tests/test_crossover_v2_incident_replay.py`](../tests/test_crossover_v2_incident_replay.py)
drives `CrossoverV2Session._build_candidate` with it, using the exact keyword
pair `_evaluate_fc_candidate` passes for a non-configured corner. It stops
there deliberately: the caller `_build_measure_candidate` adds a
linearized-vs-raw predicted-spec gate that passed on the incident, is
orthogonal to both defects, and cannot pass on synthetic branches without
shaping them until it does. Copy the shape, not the contents:

* **Derive, never hand-copy — and name the one field you cannot.** The script
  has a `--check` mode that re-derives and diffs, so a fixture can be proved to
  still be the session it names. It exits `2` when the bank is absent, because
  "I could not check" must not read as "the check passed" — and it is an
  operator tool, never a CI gate. Content drift and a same-content/renamed-
  directory bank both exit `1` but say so in different sentences, so neither
  sends a reader hunting for the other. **Exactly one field is exempt:
  `anchor_replay` in `expected_outcome.json` is HAND-BANKED**, and the script
  neither derives nor validates it. Since 2026-08-19 the anchor's give-back is
  measured by `solve_branch_trims` over `branch_level_bands_hz`, which needs the
  per-driver COMPLEX responses this bundle never retained (dropped for size), so
  no arithmetic over the banked scalars can reach the number — a derivation kept
  here would report drift on a correct fixture forever, and a checker known to
  fail trains a reader to ignore `--check`. The script's `main` instead splices
  the committed block through verbatim, so a plain re-run cannot delete a value
  nothing can rebuild, and `--check` matches it by construction rather than by a
  special case in the diff loop. Every other field is still derived and still
  diffed. What guards the exempt one is the replay test, which asserts
  production's own anchor equals the banked value; what is lost is only this
  script's independent second opinion on it. When a field earns this exemption,
  it carries its own provenance note in the fixture — `anchor_replay`'s says
  which formula it replaced and what the change moved.
* **Inject only what cannot be committed, and name every stub.** Every SCALAR
  the decision consumes rides in the fixture, and **two** seams are stubbed —
  `fit_driver_linearization` returns the incident's own serialized
  `LinearizationFit` pair, and `solve_ripple_optimal_trim` returns its recorded
  scan result — because their true inputs are ~5e5-bin measured responses. The
  branches underneath are synthetic and zero-phase, so the level errors the
  commit decision turns on are real code over stand-in curves; the test asserts
  that premise instead of assuming it. State the whole injection surface in the
  test's docstring rather than leaving it to be inferred.
* **Label a characterization test as one.** It pins behaviour that is WRONG, so
  each defect's docstring and its load-bearing assertion messages name the
  issue that will invert them. A green run means the incident still reproduces,
  not that the speaker is right.
* **Pin that inputs are USED, not merely accepted.** The pre-existing R17 guard
  spied `candidate_sections` being *passed* and never *used*, so a fit that
  dropped the kwarg entirely passed the whole repo. The discriminant is shape,
  not corner: a 1648.7 Hz LR4 radiates `(0.0, 1321.3)`/`(2057.2, inf)` where a
  2000.0 Hz one radiates `(0.0, 1602.9)`/`(2495.5, inf)`, so asserting the band
  handed to the fit engine (and reported on its journal line) closes the hole
  that asserting the corner cannot.
* **Mutation-verify every site you claim, and name the ones you cannot.** Of
  the six `self._fc_hz` reads behind these defects, four fail the test when
  flipped; **two cannot** — the straddle test deciding whether the ripple scan
  runs, and the `fc_hz` field of the `ripple_trim_skipped` event in that
  straddle's own else-branch. On this session's overlap band both corners
  straddle identically, so the replay takes the same branch either way and the
  else-branch never runs. Both are named in the test's docstring, because a
  half-guarded site otherwise reads as covered.

The second member of the family is the 2026-08-16 alignment incident (#2598):
[`tests/fixtures/crossover_v2_alignment_incident_20260816/`](../tests/fixtures/crossover_v2_alignment_incident_20260816/)
plus
[`tests/test_crossover_v2_alignment_incident_replay.py`](../tests/test_crossover_v2_alignment_incident_replay.py),
where a crossover shipped with one branch inverted and the summed blend nulled.
It follows the shape above with two deliberate differences, both worth knowing
before copying either one:

* **No re-derivation script, because there is no bank.** Its sources are a LIVE
  speaker's runtime state (`/var/lib/jasper/active_speaker_crossover_v2_state.json`
  and two session artifacts on jts3, read-only over ssh), not a retained
  capture bundle — a `--check` mode would be re-reading a file that keeps
  changing. Each source's absolute path and sha256 ride in the fixture's
  `_provenance` instead, which is the audit trail a reader gets.
* **The branch pair is BUILT from the declared design, not banked.** The
  retained evidence holds summed magnitude curves, and the selector reads
  complex per-branch transfers. So the test builds the preset's own
  Linkwitz-Riley filters at the banked corner and earns that substitution with
  an assertion rather than a claim — but the assertion is about SCALE, not
  precision: in the frame the box shipped, the model says 14.2 dB of blend
  ripple and the box said 10.5, and both dwarf the sub-dB the declared
  polarity leaves. Injection surface is otherwise NONE — no seam, stub or
  monkeypatch.
* **A matched frame is part of "derive, never hand-copy."** The first cut of
  that assertion claimed 0.10 dB agreement and was CROSS-FRAME: it compared a
  model carrying +34.1 us of residual against `predicted_ripple_db`, which is
  the box's ZERO-RESIDUAL quantity. Matched properly the two disagree by
  +3.8 dB at the committed delay and +46 dB at zero residual — the tight
  tolerance was pinning +/-0.63 us of delay, not model fidelity. When a fixture
  quotes a banked number, name the frame it was computed in beside it; a
  derived comparand (here `BOX_COMMITTED_RIPPLE_DB`, reconstructed from the
  banked pair's own anchor-ripple and improvement) is worth the extra line.

---

## Reading comparator (pre/post value diff)

[`scripts/compare-readings.py`](../scripts/compare-readings.py) answers **what
did this measurement change actually move?** — at value level, across the whole
set of readings a change touches, not just the ones a test happens to pin.

It exists because a lane can only ever go red on one of the three places a
reading lives. PR #2062 (issue #2045) is the worked example: PR #1991
legitimately moved one S0 capture's gate, **31 of 155 compared readings moved**
with it, and two of the three classes were invisible —

* **12 pins went red.** The corpus lane finds these.
* **1 reading was absorbed by a tolerance** — the 1.8 kHz dip depth landed
  inside `pytest.approx(5.19, abs=0.05)` with **0.0041 dB to spare**, so the
  suite passed on a stale number, and the next honest re-read would have tipped
  it red as a phantom regression.
* **7 prose homes restated the same facts**, three pinned by nothing at all,
  and one had been contradicting the very test it names.

The instrument that found classes 2 and 3 was built by hand during that
diagnosis and thrown away. This is that comparator, committed under issue
[#1884](https://github.com/jaspercurry/JTS/issues/1884) rider (d).

**It does not replace the human-executed corpus lane, and it is not CI.** The
standing ruling on #1884 is that corpus-gated tests stay laptop-local and
human-run; this sits *on top of* that lane. There is no runner, no nightly job
and no corpus in CI. It reads two JSON files and the source files those files
declare, and nothing else.

Dump the readings once on the base commit, once on the branch, then:

```sh
PYTHONPATH=. .venv/bin/python scripts/compare-readings.py before.json after.json
```

**Producing the dumps is the caller's job, deliberately.** Which readings
matter is a property of the change under test, so a measurement PR writes a
throwaway dump script that drives the shipped code paths and serializes what it
got. The tool owns the comparison and the classification, not the enumeration.
A dump maps a reading's name to a bare value, or to a record that also declares
the tolerance guarding it and the other files that restate it:

```json
{
  "s0.cloud_04.floor_hz": 1777.7777777777778,
  "nulls.dip_1800.depth_db": {
    "value": 5.144103951440755,
    "tolerance": 0.05,
    "homes": ["jasper/audio_measurement/interference_nulls.py"]
  }
}
```

`tolerance` and `homes` are read from the **after** dump, so one file owns that
metadata and the two cannot disagree. Home paths resolve relative to the
current working directory.

**Five properties worth knowing before you trust its output.**

*A tolerance-absorbed move is a reported class, not a pass.* It prints with the
headroom the move left, because that number is what says how close the pin came
to going red. Silence there was the #2062 failure, so every section prints its
count even at zero, and a reading present in only one dump is named rather than
dropped.

*Prose-home hits are candidate sites for a human to judge, not proof of drift.*
For each moved or absorbed reading, the declared homes are scanned for
renderings of the **before** value at 0–6 decimal places — that same S0 floor
is written both as "1778" (`jasper/audio_measurement/gating.py`) and as
"1777.8" (`jasper/active_speaker/crossover_v2_flow.py`) — with one hit per line
at the most specific rendering that matched. A rendering that is also a
rendering of the after value is skipped, so a site that already reads correctly
is not flagged. The scan matches a number; it cannot know which fact that
number is stating. A declared home that is not on disk is reported too.

*Two kinds of rendering carry no information, and both are dropped before the
scan.* One is under three characters — an `n_rungs` of 12 would match half a
source file. The other has rounded its last significant digit away: 0.029 at
one decimal place is `"0.0"`, which matches every ordinary `0.0` literal in
the file it scans, and on a real `interference_nulls.py` that buries the one
hit stating the reading under 35 that state nothing. Neither rule subsumes the
other, and the band matters — this corpus quotes rung deltas of `-0.029` and
`-0.004`, and #2062's own headroom is `0.0041 dB`.

*A home it could not scan is its own reported class.* When the before value has
no rendering to search for — a short int, a short string, a bool, anything that
leaves nothing to look for — the file is **never opened**, so it prints under
`HOMES NOT SCANNED` with the before value, to be checked by hand. "Not looked
at" must not print the same as "looked at, clean"; that is #2062 class 3 all
over again, in the tool built to end it. A float never lands here: its `repr`
round-trips exactly. Note the boundary against the paragraph above: a value
whose renderings all read correctly for the after value is *not* flagged and
*not* listed here — that home has nothing to find.

*It is advisory and exits 0 whatever it found.* Same contract as
[`scripts/tense-grep.sh`](../scripts/tense-grep.sh). It exits 2 only when it
could not do the comparison at all — a malformed or unreadable dump — so "I
could not compare" never reads as "nothing moved".

Hardware-free coverage is
[`tests/test_reading_comparator.py`](../tests/test_reading_comparator.py),
which grades it on one synthetic case per #2062 class.

---

## Offline emit loop

`jasper-active-speaker-emit-bench`
([`jasper/cli/active_speaker_emit_bench.py`](../jasper/cli/active_speaker_emit_bench.py),
library in [`jasper/active_speaker/bench/`](../jasper/active_speaker/bench/))
answers one question: **does the DSP realize a linearization the way the fit
claims it will?** It emits the preset twice through the real emitter — once with
the linearization under test, once with none — renders both through the real
pinned CamillaDSP binary as one-shot file-to-file batch passes, and grades the
difference against
`linearization_fit.complex_correction_response`. The difference is the
instrument: everything the two configs share (crossover, delay, per-driver gain,
split mixer, fader, stimulus) cancels exactly, so nothing has to be modelled to
grade the filters under test.

This is the offline twin of
[`jasper/active_speaker/delta_probe.py`](../jasper/active_speaker/delta_probe.py),
whose verdict vocabulary and classifier it reuses rather than duplicating. The
probe catches a realization defect **in the room**, after an apply, from a
household's post-apply sweep; this catches the same class **before** anything is
applied and without a microphone. It exists because a model cannot audit itself:
on 2026-07-27 a shipped shelf realized at Q 0.476 while every gate in the fit
evaluated it at 0.707, missing its design by up to 1.70 dB with nothing in the
loop able to see it.

The bench itself runs **on the speaker** — the binary's identity is resolved
from the running `jasper-camilla.service` unit and there is deliberately no
`--binary` override. Invoke it **from your laptop checkout**, though:
`pi-run-diagnostic.sh` is a laptop-side wrapper that SSHes to `$PI_HOST` (typed
on the Pi it would SSH from the Pi to itself). Every path in the command below
is therefore **Pi-side** — `--linearization` and `--out` are resolved on the
speaker, not on your laptop.

```sh
# from the laptop checkout; paths are on the Pi
bash scripts/pi-run-diagnostic.sh -- \
  /opt/jasper/.venv/bin/jasper-active-speaker-emit-bench \
    --linearization /var/tmp/fits.json \
    --playback-device "$(...)" \
    --out /var/tmp/emit-loop
```

**Run it through the bounded runner, not bare.** The renders are bounded in
their own child processes, but the deconvolution and FFTs run in the CLI's own
process and are not: a production-length run measures 221 MiB parent-only peak
RSS (235 MiB on an independent measurement during review), a real fraction of a
1 GB Pi. `pi-run-diagnostic.sh` gives the kernel an obvious thing to kill before
it reaches a product daemon.

**Expect to raise the runner's memory ceiling for a longer sweep.** Its defaults
(`MemoryHigh=256M`, `MemoryMax=384M`, `RuntimeMaxSec=10min`) fit the measured
221–235 MiB with little headroom, and the dominant term — the deconvolution's
zero-padded FFT — scales with `--sweep-seconds`. A longer sweep will be
OOM-killed by the cgroup, which looks exactly like a bench bug and is not one.
Raise it deliberately:

```sh
JTS_DIAG_MEMORY_HIGH=512M JTS_DIAG_MEMORY_MAX=768M \
  bash scripts/pi-run-diagnostic.sh -- ...
```

`--linearization` is a JSON object of persisted per-role `LinearizationFit`
records (`{role: {"filters": [...], ...}}`), the shape
`linearization_fit.linearization_filters_by_role` reduces.

**Exit codes are three-state, because the evidence is.** `0` — every branch that
could be graded matched, and at least one was. `1` — a graded branch did not
match; the finding. `2` — no verdict was reached: either the run refused, or it
completed and nothing in it was gradeable. A role the fit left alone commands
nothing, so its comparison reaches no verdict at all
(`delta_probe`'s `unavailable`: "not a pass … no evidence to refuse on, and no
permission granted either") — those branches are listed in the report's
`unavailable_roles`, never counted as passes or failures. `report.json` carries
`outcome` alongside the per-branch records.

The bundle keeps both arms' configs (`control.yml` and `treated.yml`, one
derivation each — each is rendered twice), **four `.raw` renders**
(`<arm>.first.raw` and `<arm>.repeat.raw`; the repeat's SHA-256 is what the
determinism receipt asserts against, so both are retained as evidence), the
stimulus WAV, and `report.json`.

`--dry-run` runs the real emitter and the real derivation for both arms and
writes both derived configs, without resolving a binary or rendering anything.
That makes it a genuine preflight rather than an echo of the arguments: an
emitter validation refusal, a stage outside the offline allowlist, a hard-clip
limiter, or a stimulus past the deconvolution's FFT cap all surface on a laptop
instead of after an SSH round trip. It does not write the (multi-megabyte)
stimulus — the derivation validates the header it is handed, not the file.

Read `band_max_error_db` per branch, not just the verdict: the classifier's
tolerances are calibrated for a microphone (1.5 dB below 10 kHz) and are
generous by orders of magnitude offline. On the stand-in-binary suite an exact
render lands at 0.003–0.013 dB while the 2026-07-27 shelf-Q defect reads
1.705 dB.

Hardware-free coverage lives in
[`tests/test_active_speaker_emit_bench_derivation.py`](../tests/test_active_speaker_emit_bench_derivation.py),
[`..._compare.py`](../tests/test_active_speaker_emit_bench_compare.py),
[`..._loop.py`](../tests/test_active_speaker_emit_bench_loop.py), and
[`..._cli.py`](../tests/test_active_speaker_emit_bench_cli.py), against the
stand-in binary [`tests/_fake_camilladsp.py`](../tests/_fake_camilladsp.py).
Those prove the plumbing and that the comparison catches a mis-realized filter;
they cannot prove what the real binary does with an emitted biquad, which is the
whole question and needs the on-device run.

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

## USB turntable experiment

[`experiments/usb-turntable/jts_turntable.py`](../experiments/usb-turntable/jts_turntable.py)
is the manual JTS3 adapter for the reusable `usb_turntable` controller package.
It provides USB detection, an identity/firmware probe, a read-only
no-motion offset-from-zero query, left/right relative movement, a
confirm-gated zero redefinition, home, the vendor stop request, and guarded
absolute measurement positions. The latter always homes first and is bounded
to `-45` through `+45` degrees from the acoustic on-axis zero. JTS owns the
Raspberry Pi power preflight, the measurement-rig guard, the `set-zero`
confirmation gate, those operator-facing names, and a bounded one-retry
recovery (against a freshly opened controller session) for the read/guarded-
idempotent commands (`offset`, `probe`, the guarded `position`) on the
vendored transport's exact `ProtocolError` base class; the upstream package
owns USB discovery, serial framing, response parsing, command completion, and
the transport session itself.

Positioning is opt-in and has no voice tool, measurement scheduler, or permanent
daemon. A full install adds only a bounded udev-triggered stop one-shot for the
known CH340-attached turntable; it verifies product identity before issuing the
stop and exits on success or after four attempts. Read the experiment's
[`README.md`](../experiments/usb-turntable/README.md) before use. Hardware-free
adapter and preflight coverage lives in
[`tests/test_usb_turntable_experiment.py`](../tests/test_usb_turntable_experiment.py).

---

## E0 headless capture client

[`experiments/e0-capture/e0_capture.py`](../experiments/e0-capture/e0_capture.py)
stands in for the browser capture page so a measurement microphone on a Mac
can drive the real Pi-side crossover-v2 conductor (CHECK → MEASURE → VERIFY)
with no browser and no phone. It speaks wire protocol v3, mints or accepts a
session, records each plan entry with `sox`, and posts the authenticated
phone events the conductor's position gate rides. It is the lab/agent path;
the browser flow stays first-class for a human driver. Promoted out of an
untracked working directory by design decision 13's companion ruling
([#2636](https://github.com/jaspercurry/JTS/issues/2636)).

Start with `--selftest`, which is offline and makes no network call:

```sh
.venv/bin/python experiments/e0-capture/e0_capture.py --selftest
```

`--start-session` / `--tap-link` reach a live Pi and make the speaker play
sweeps, so only a human hardware operator runs them.
[`preflight_noaudio.py`](../experiments/e0-capture/preflight_noaudio.py)
validates mint, spec fetch, and MAC verification against a live Pi without
playing anything. The wire contract, with `file:line` citations and a dated
revival addendum, is
[`PROTOCOL.md`](../experiments/e0-capture/PROTOCOL.md); read the experiment's
[`README.md`](../experiments/e0-capture/README.md) before a hardware round,
in particular its stated residual risk — a future capture-page change to the
`setup` payload is not refused, it degrades the round to uncalibrated data,
and the only signal is `correction.crossover_v2_uncalibrated_capture` in the
`jasper-correction-web` journal. Hardware-free coverage lives in
[`tests/test_e0_capture_experiment.py`](../tests/test_e0_capture_experiment.py),
which runs the same offline checks `--selftest` does.

---

## Crossover prescriber harness

`jasper-crossover-prescriber`
([`jasper/cli/crossover_prescriber.py`](../jasper/cli/crossover_prescriber.py))
is the read side and the write side of "hand a round's evidence to a reader,
take a correction back". It has no model client, no API key and no network:
**who calls the model is not the tool's business**, which is what makes it
work identically with a human doing the reasoning, a laptop agent over SSH, or
a paste into a browser.

```sh
# the read side: one round's banked evidence as one versioned JSON document
jasper-crossover-prescriber packet <bundle-dir> --state <flow-state.json> \
    --out round.json

# the write side: validate what came back, against the round it answers
jasper-crossover-prescriber propose <bundle-dir> --state <flow-state.json> \
    --prescription answer.json --json

# the door: same gate, and the accepted answer is left for the next round
jasper-crossover-prescriber stage <bundle-dir> --state <flow-state.json> \
    --prescription answer.json
```

`propose` is the **dry run of** `stage` — the same gate on the same document,
and the only difference is that `stage` banks the result. Run the first to see
the answer, the second to commit to it.

**Two prescription classes, one door.** A document names its own `kind` and
that is what picks its gate — there is no `--class` flag and no inference from
shape:

| `kind` | what it corrects | bounded by | lands in |
|---|---|---|---|
| `jts_crossover_blend_prescription` | the SUMMED blend region | the round's crossover region | the candidate's `blend_correction` |
| `jts_crossover_driver_prescription` | ONE driver's own full band | that driver's own declared band | the candidate's `linearization` |

The per-driver class carries **both signs**, and every filter must be aimed at
a feature a banked classification typed as a minimum-phase driver defect of the
MATCHING sign — a **peak** for a cut, a **dip** for a boost. A boost owes two
things a cut does not: the vouching verdict must not be `vertical_blind`, and it
must report a `depth_db` the boost does not exceed. Its cost is maximum SPL
rather than safety (the graph attenuates before the split), bounded at 5.0 dB by
a per-role composed budget. It needs two pieces of
evidence, and they arrive by **different routes** — one flag, one file:

```sh
# --drivers is the ONLY extra flag; the classification is found in the bundle
jasper-crossover-prescriber propose <bundle-dir> \
    --drivers /var/lib/jasper/active_speaker_design_draft.json \
    --prescription answer.json --json
```

- **The bands** come from `--drivers`, the design draft, whose confirmed
  driver-safety profile carries each role's published response range and its
  declared protective corners. The draft is banked outside the bundle, so it is
  passed in. Absent → `driver_passband_unavailable`.
- **The verdicts** are auto-discovered: the packet builder reads
  `feature_classification.json` from the round's own artifact directory,
  alongside `round_receipt.json` and `cloud_verify.json`. There is no flag for
  it and no way to point it elsewhere. Absent → `driver_feature_not_classified`.

Nothing in the product **produces** a classification today — stage P3's
instrument is not built — so that file is an operator's banked lab result
dropped into the round directory. A `defect-*` verdict says EQ is not
structurally barred at that feature, never that EQ will help; and
`defect-boostable` is a minimum-phase **dip**, which is still refused, because
cutting a dip deepens it. The **nearest** banked verdict to a filter's centre is
the one that decides — features can sit closer together than the match
tolerance, so a cuttable peak nearby cannot vouch for a filter sitting on a dip.

`stage` writes one document to
`/var/lib/jasper/active_speaker_crossover_v2_prescription.json` and stamps it
with the round the flow state says is next. The next crossover round takes it
**once**, re-validates it, and consumes it; a household Undo withdraws it
unrun; and a document staged for a round that has already run is refused as
`prescription_not_staged_for_this_round` while the round carries on with its
class's own deterministic answer — the previous round's banked blend
instruction, or the automatic per-driver fit. `--state` is required here (it is
optional for the
other two verbs) because the round ordinal is read from it — staging without
one would file a prescription against a series the command cannot see. Staging
twice is last-wins: the slot holds one instruction, and the overwrite is logged
(`event=crossover_v2.prescription_staged` carries `replaced`) so a round that
applied the second of two prescriptions can be explained. That line goes to
**stderr**, so it is visible in an operator's shell and in the journal when the
command runs under systemd; stdout stays the machine channel for `--json`. Owner:
[`prescription_spool.py`](../jasper/active_speaker/crossover_v2/prescription_spool.py).

`<bundle-dir>` is a commissioning bundle — the directory holding `info.json`
beside `evidence/v1/artifacts/crossover_v2/<relay-session-id>/`. `--state` is
the crossover-v2 flow state, which is banked **separately** from the bundle;
without it the packet cannot carry the per-claim verify verdicts, the Fc
selection, or the applied profile's incumbent, and it says so rather than
going quiet.

**Exit codes are the contract**, because the caller is often a script: `0`
accepted, `1` the evidence could not be read, `2` the prescription was refused,
`3` an accepted prescription could not be staged. A refusal is the loop
working, not a crash — `--json` prints the machine-readable `reason` slug plus
the evidence behind it, so a prescriber can correct itself rather than guess.
`3` is separate from `1` because the two send you to different places: `2`
means fix the prescription, `3` means fix the speaker's filesystem.

What the packet is for beyond the model loop: it is the single document the
deterministic trend engine and any by-hand round review both want, and its
`not_evaluated` block is the fastest way to see what a round **cannot** answer
(no numeric mic angle is banked anywhere; the reflection time exists only
inside gate-disclosure prose; no round banks a distortion reading).

Owners:
[`evidence_packet.py`](../jasper/active_speaker/crossover_v2/evidence_packet.py)
builds the document;
[`blend_prescription.py`](../jasper/active_speaker/crossover_v2/blend_prescription.py)
and
[`driver_prescription.py`](../jasper/active_speaker/crossover_v2/driver_prescription.py)
each own their class's response format *and* the gate that enforces it, so the
instructions a prescriber is given and the bar it is judged by cannot describe
different shapes;
[`feature_classification.py`](../jasper/active_speaker/crossover_v2/feature_classification.py)
is the verdict register the second of those reads (the vocabulary, deliberately
not the pipeline). Hardware-free coverage, including a hostile-input battery and
a golden against a real banked round when `captures/` is present, lives in
[`tests/test_crossover_v2_blend_prescription.py`](../tests/test_crossover_v2_blend_prescription.py)
and
[`tests/test_crossover_v2_driver_prescription.py`](../tests/test_crossover_v2_driver_prescription.py).

---

## Angle-walk door

`jasper-angle-capture`
([`jasper/cli/angle_capture.py`](../jasper/cli/angle_capture.py)) is how an
operator states a capture walk at stated angles —
`{per-driver | summed} x {angles} x {arm | human-guided}` — and sees exactly
what it resolves to before anything plays. It is the door onto the #2732 seam
([`angle_capture.py`](../jasper/active_speaker/angle_capture.py)), which had no
way for anybody to state a request.

```sh
# the read side: resolve a walk and print it. Writes nothing, plays nothing.
jasper-angle-capture plan --angles 0,7,-7,22,-22 --regime per_driver --mover human

# the door: same resolution, and the request is left for the next session
jasper-angle-capture stage --angles 0,7,-7,22,-22 --regime per_driver --json

# the undo
jasper-angle-capture withdraw
```

`plan` is the **dry run of** `stage` — the same constructors, the same
refusals, the same resolved walk — exactly as `propose` is the dry run of the
prescriber's `stage` above. Its output names, per stop, the capture index, the
signed bearing, the pose in the centimetres every shipped consumer reads, the
program that stop plays, the advance policy the mover implies, and (for an arm)
the `position_deg` the position gate will wait for.

**Angles are stated in whole degrees, negative LEFT and positive RIGHT facing
the speaker, and nothing is coerced.** `7.5`, `0.4` and `+7 deg` are all
refused, in the seam's own words: `int(0.4)` is `0`, so a truncating parser
would silently turn a just-off-axis request into an **on-axis** capture. There
is no second validator in the CLI or the mailbox — bounds, whole-degree-ness,
the regime vocabulary and the mover vocabulary are all
[`angle_capture.py`](../jasper/active_speaker/angle_capture.py)'s.

`stage` refuses while a measurement session already holds the speaker, read off
the durable session-volume state — the one cross-process fact, since the
correction web's own relay slot and measurement interlock are module-globals a
CLI cannot see. A *stale* active state is a crashed session, not a live one, and
does not block: the flow force-drains it at the next open.

**Exit codes are the contract**: `0` accepted, `2` refused (a bad angle, an
unknown regime or mover, or a session already running), `3` an accepted request
could not be banked. `2` means fix the request; `3` means fix the speaker's
filesystem.

**What it does not do**: it runs no capture and opens no session. `stage` writes
one document to `/var/lib/jasper/active_speaker_angle_capture_request.json`,
single-use and last-wins, logged as `event=angle_capture.request_staged`. Owner:
[`angle_capture_spool.py`](../jasper/active_speaker/angle_capture_spool.py).

**The next measurement session takes it, once.** Opening
`/correction/crossover/v2/session` consumes the document and walks its stops as
that session's lateral group. Every accepted pose banks its raw WAV plus a
sidecar carrying `position_deg`, `offset_cm`, `at_mark`, `regime` and
`lateral_consumer`.

**A taken walk is EVIDENCE: its last pose adjudicates nothing.** The
lateral-walk statistic paused on 2026-08-18 runs only for the fixed stage-1
selector walk. A staged walk declares the forward-model consumer, so the walk's
close is suppressed — the [#2711](https://github.com/jaspercurry/JTS/issues/2711)
bar is untouched, and the ruling behind that split is recorded in
[`active-speaker-tuning-layers-design.md`](active-speaker-tuning-layers-design.md)
under "Stage P2".

Read the journal, not the code, to find out what happened:

| `event=correction.…` | says |
|---|---|
| `crossover_v2_angle_walk_taken` | stops, angles, mover, regimes, consumer |
| `crossover_v2_angle_walk_refused` | the slug, and the arithmetic when it is a capacity refusal |
| `crossover_v2_lateral_close_suppressed` | `planned`/`captured`, and `fc_statistic_paused=true` |

**Five refusals.** The session opens in its ordinary shape after every one, and
the document is consumed — except on the spool's two unreadable arms, which
deliberately do not consume so a permissions mistake cannot destroy the evidence
of itself. The `consumed=` field says which happened; do not assume it.

| slug | why |
|---|---|
| `walk_regime_unsupported` | per-driver stops only: a lateral group plays MEASURE's program at every pose |
| `walk_mover_mismatch` | the walk's mover must match the session's tier, or the session stalls |
| `walk_over_relay_capacity` | the plan this session would emit needs more relay blob indexes than exist — reachable with a pre-apply cloud, and never for a legally staged walk on the shipped 3-capture shape |
| `walk_lateral_group_already_planned` | the session already walks a lateral group |
| `walk_stop_no_longer_valid` | a banked stop no longer satisfies the seam (a hand-edited angle); the detail carries the seam's own sentence |

The spool's own slugs (`angle_request_spool_*`, `measurement_session_already_live`)
reach the same journal line, so this table is the take's half, not the whole
vocabulary.

Hardware-free coverage: the mailbox and the CLI in
[`tests/test_angle_capture_trigger.py`](../tests/test_angle_capture_trigger.py),
the composition and its refusals in
[`tests/test_angle_capture_seam.py`](../tests/test_angle_capture_seam.py), the
take in [`tests/test_angle_capture_take.py`](../tests/test_angle_capture_take.py),
and the suppression pins in
[`tests/test_crossover_v2_lateral_evidence.py`](../tests/test_crossover_v2_lateral_evidence.py).

---

## Seat-SPL leveling

`jasper-seat-level` ([`jasper/cli/seat_level.py`](../jasper/cli/seat_level.py))
answers one question on real hardware: **what main volume makes this speaker
measure a stated dB SPL at the listening seat?** It rolls the volume slowly up
from a quiet floor while a calibrated measurement mic watches, stops inside the
requested band, and banks the volume that got there as the crossover session's
measurement reference — replacing the codified −20 dB
`MEASUREMENT_REFERENCE_VOLUME_DB` that every session held before.

```sh
# the ordinary run: converge on 75-80 dB SPL and bank the result
jasper-seat-level --stimulus-wav /var/lib/jasper/.../check.wav --mic-serial 810-8494

# a different band, an explicit calibration file, machine-readable
jasper-seat-level --stimulus-wav check.wav --calibration-file umik2.txt \
    --target-db-spl 72 --tolerance-db 2 --json
```

**It measures nothing by itself — it reuses the level-match kernel.** The ramp is
[`jasper.audio_measurement.ramp`](../jasper/audio_measurement/ramp.py)'s
`RampController` (quiet start, coarse staircase, stop-ahead pre-window, settled
two-point jump, confirm streak, clip abort, feed-liveness abort, derived safety
timeout, fade before the tone is killed); the mic feed is
[`wired_level_meter.py`](../jasper/audio_measurement/wired_level_meter.py); the
volume hold is the crossover session's own `SessionVolumePlan`. What this verb
adds is the SPL domain: the band, the ceilings, and the ambient floor.

**The ceiling is mic-independent, and that is deliberate.** The ramp's hard bound
is `unsegmented_stimulus_ceiling_db` — `min(driver caps) − stimulus peak`, the
excitation ledger solved for main volume against the ACTUAL stimulus bytes.
`min`, not `max`: nothing attenuates a flat WAV down to the quieter drivers'
ledgers the way a composed program's per-segment gains do. No measured level
enters that number, so a mis-calibrated microphone cannot move it. The profile's
`max_commissioning_level_db_spl` is a second, measured stop — softer by
construction, because it shares the calibration's fate.

**The room is measured once, before the tone, and three rules read it.** That one
ambient number is the kernel's trust threshold, the runaway guard's "did anything
actually rise", and convergence's "did the reading rise at all". Measuring rise
against ambient rather than against the first reading is what stops a speaker
quieter than the room from being falsely aborted while it climbs through the
floor — and a mic that is not listening never emerges at all, because its ambient
reading IS its signal reading.

**Absolute SPL comes from the mic's own calibration file** — the `Sens Factor`
header line that the curve parser has always skipped
([`calibration.py`](../jasper/audio_measurement/calibration.py),
`parse_calibration_sensitivity`), as `dB SPL = dBFS − sens_factor + 94`. **The
precondition is yours to check**: that figure is quoted at the mic's MAXIMUM
capture volume, so confirm `amixer -c <card>` shows the capture control at 100%
before trusting any absolute number. No calibration means no absolute level and
the verb refuses; it never guesses a sensitivity.

**Exit codes are the contract**: `0` converged and banked, `1` any refusal. Every
refusal restores the household volume and banks nothing.

| refusal | why |
|---|---|
| `mic_calibration_unavailable` | no parseable `Sens Factor` for this mic |
| `measurement_mic_absent` | no measurement-class capture card is present |
| `stimulus_wav_missing` | the named stimulus is not a file |
| `measurement_session_already_live` | a crossover session (or an unresolved one) holds the speaker — the same door `jasper-angle-capture stage` stands behind, read off the same durable state |
| `seat_spl_target_rejected` | the band's TOP exceeds the profile's commissioning ceiling |
| `driver_cap_ceiling_underivable` | no confirmed driver safety profile, or no preset |
| `spl_target_uncapturable` | the band sits above digital full scale at this mic |
| `volume_ceiling_below_ramp_start` | the ledger leaves no room to climb |
| `mic_not_observing` | the volume climbed the probe span and the mic never rose above the room |
| `spl_ceiling_exceeded` | a measured reading crossed `max_commissioning_level_db_spl` |
| `spl_target_unreachable` | the ceiling was reached without entering the band |
| `mic_feed_lost` / `mic_clipping` / `ramp_timeout` | the kernel's own aborts |

Read the journal, not the code, to find out what happened:

| `event=active_speaker.seat_level_…` | says |
|---|---|
| `_start` | band, converted dBFS window, sens factor, AGain, both ceilings, start, step, and the amixer precondition |
| `_ambient` | the measured room floor in dBFS and dB SPL, and the rise the guard will demand |
| `_abort` | which guard fired, with the climb, the ambient, and the observed rise |
| `_converged` | the banked volume, the measured dB SPL, and the recovered chain gain |
| `_refused` | the refusal slug and the ramp terminal behind it |

**What it does not do**: it designs no stimulus (point `--stimulus-wav` at the
program the session will actually measure with — choosing a safe excitation is
the admission subsystem's job) and it opens no measurement session. It writes one
document to `/var/lib/jasper/active_speaker_seat_level_reference.json`; the next
session reads it through `measurement_reference_volume_db`, and **absent is
normal** — a box that never runs this behaves exactly as it did before the verb
existed. `jasper-doctor`'s `seat-SPL measurement reference` line reports which
state that file is in.

Two deploy-time knobs, both bounded and both falling back to their defaults on a
bad value: `JASPER_SEAT_LEVEL_PROBE_DB` (how far the volume climbs before the
guard demands evidence, default 20) and `JASPER_SEAT_LEVEL_MIN_RISE_DB` (how far
above ambient counts as evidence, default 6).

Hardware-free coverage: the conversion, the guards, the ambient model and the
banked artifact in
[`tests/test_active_speaker_seat_level.py`](../tests/test_active_speaker_seat_level.py),
the verb and its pre-audio refusals in
[`tests/test_cli_seat_level.py`](../tests/test_cli_seat_level.py), the mic feed in
[`tests/test_wired_level_meter.py`](../tests/test_wired_level_meter.py), the
derivation it feeds in
[`tests/test_active_speaker_session_volume_plan.py`](../tests/test_active_speaker_session_volume_plan.py),
and the doctor line in
[`tests/test_doctor_state_files.py`](../tests/test_doctor_state_files.py).

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
  mux/voice STATUS snapshots. The tracked inventory includes the resident
  USB host-microphone export path (`jasper-usbgadget`, `jasper-usbmic`, and
  `jasper-usbnet-dhcp`); it deliberately excludes the transient
  `jasper-usbmic-apply` oneshot.
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
| [`scripts/capture-chip-mic.sh`](../scripts/capture-chip-mic.sh) | XVF3800 processed conference channel via `arecord` | Quick single-stream mic recording; does NOT use the bridge |

---

## When to add a new tool vs. extend an existing one

Default to extending. Add new only when:

- **Different audio source** the existing tools can't access (e.g. a phone
  relay vs. the XVF via USB-UAC2 vs. the WiiM Remote 2 Bluetooth mic).
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
deleted, **delete its row in the same PR** — a row for a file that no
longer exists is the stale prose this repo forbids, and where the tool
went belongs in the ticket or campaign doc that removed it, not in a
struck-through line here. Strike a row through only when the tool still
exists but is superseded, so a reader who finds it knows to reach for
the replacement named in the row. If you do a forensic
investigation that uses a `/tmp/` script you'll likely want again,
promote it to `scripts/_analyze_*.py` AND add an entry above.

The doc is in the [README.md](../README.md) documentation map and
referenced from [CLAUDE.md](../CLAUDE.md) so an AI agent picking up
the codebase sees it before writing a duplicate.
