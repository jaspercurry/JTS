# Phase 0 cartography — ORPHANS AND DEAD CODE

Tree: `/home/user/JTS` @ `2d571e6b8`. Read-only; `git status` clean at start and end (no
`vulture` install — an AST/token reference scan was used instead, see Method).

**Headline: the tree is far cleaner than its size suggests.** 749 Python modules under
`jasper/`, and after resolving every indirection listed in the brief there are **zero
module-level orphans** outside the ADR-0018-parked bass-extension half. No unreferenced
scripts, no uninstalled systemd units, no nginx location without a server, no
unreferenced `deploy/assets` file, no dead Rust crate, no test importing a deleted
module, no data asset without a reader. What is left is small and specific: a handful of
dead `main()`s pinned by a meta-test, ~100 lines of genuinely uncalled functions, and
~1.1K lines of unreferenced historical docs.

**Total removable: ~440 LOC of product code (high confidence) + ~1,126 doc lines +
~615 LOC medium confidence.** Detail and sums in §11.

---

## 1. Python module orphans (task item 1)

Method: AST import graph over every `.py` in the repo (absolute + relative + function-local
`ImportFrom`, `importlib.import_module`/`__import__`/`spec_from_file_location` string args),
plus a textual scan of every non-`.py` file for `jasper.<dotted>` and repo-relative paths,
plus `[project.scripts]`, plus `python -m` in `deploy/systemd/*`, plus the two dynamic
registries that string-dispatch modules (`jasper/cli/doctor/_registry.py::MODULE_ROSTER`,
`jasper/active_speaker/__init__.py::_LAZY_ATTRS`), plus `-m` subprocess spawns inside
`jasper/`. Reachability = BFS from those roots.

### (a) True orphans — zero importers, no entry point

**None**, except the parked set below.

| Module | LOC | Status |
|---|---|---|
| `jasper/bass_extension/bench/{analysis,cross_check,excitation,executor,live_proof,stimulus}.py`, `jasper/bass_extension/{ladder,limiter_evidence}.py` | **4,149** | Unreachable from any shipping root. **PARKED by [ADR-0018](../../adr/0018-bass-extension-stays-parked.md), Accepted 2026-08-25** — "a right-sizing pass, a deletion mandate, an orphan sweep, or an audit finding is not sufficient authority". Reported for the ledger only. Do not delete. |

Evidence that it is structurally unreachable, not merely unreferenced:
`jasper/cli/bass_extension_bench.py:184` — the one CLI that could reach it ends
`_run_live()` with an unconditional `raise SystemExit("live bench execution requires
binding … neither is wired here yet")`. `bench/excitation.py` has zero importers of any
kind including tests (ADR-0018 §3 already names it as such).

Package scope for the record: `jasper/bass_extension/` = 10,134 LOC product +
`tests/test_bass_extension*.py` = 10,035 LOC test.

### (b) Test-only survivors

**None at module granularity.** Every candidate the first pass produced was a false
positive; see Appendix A.

### (c) Modules whose only importer is an `__init__.py` re-export

22 modules match structurally (`cli/round_views/*`, `control/handlers/*`,
`bluetooth/handlers/*`, `audio_measurement/program_analysis/dispatch`,
`transit/providers/nyc_bus`, `cli/doctor/_cli`). **All verified consumed** — the package
`__init__` is the deliberate public facade and its names are imported by name from
outside (e.g. `jasper/active_speaker/crossover_v2_flow.py:214` imports from
`jasper.audio_measurement.program_analysis`; `nyc_bus` is dispatched by string id at
`jasper/web/transit_setup.py:346` `transit.by_id("nyc_bus")`). Not orphans.

---

## 2. Dead public functions/classes in `jasper/` (task item 2)

Method: `vulture` not installed and installing it would touch the repo's `.venv`, so:
every top-level public `def`/`class` in `jasper/` (4,304 of them) was cross-referenced by
identifier token against **every file in the repo** (code, tests, docs, shell, systemd,
nginx, JS, TOML — so `getattr`, registry strings and string dispatch all count as
references), then narrowed by an AST pass that distinguishes real code references from
docstring/comment mentions.

### 2a. Genuinely dead — zero code reference anywhere, own file included

| Severity | File:line | Def | LOC | Evidence |
|---|---|---|---|---|
| Should-fix | `jasper/audio_measurement/delay_graph.py:169` | `DelayCandidateConfirmation` (frozen dataclass + `to_dict`) | 36 | No construction, no annotation, no test, no string. |
| Should-fix | `jasper/audio_measurement/calibration.py:70` | `supported_model_options()` | 14 | Docstring asserts "The capture page consumes these via CaptureSpec" — no such consumer exists. Dead code **and** a false comment. |
| Should-fix | `jasper/multiroom/snapcast_rpc.py:163,186` | `set_client_volume()`, `set_group_mute()` | 35 | Docstring: "Used only by guarded calibration sessions" — zero callers in `jasper/`; only `tests/test_multiroom_snapcast_rpc.py` (≈40 test lines) reaches them. |
| Nit | `jasper/spotify_oauth.py:20`, `jasper/google_oauth.py:20` | `default_spotify_redirect_uri`, `default_google_redirect_uri` | 8 | Both docstrings say "for callers that name a hostname other than this speaker's" — no such caller. Test-only (`tests/test_oauth_redirect.py`). |
| Nit | `jasper/active_speaker/crossover_v2/sweep_spec.py:296` | `ui_level_meter()` | 2 | Only member of the `ui_*` DSL family with no consumer. |

Cleanest fix: delete each with its test; for the two OAuth builders, fold the one-liner
into `resolved_*_redirect_uri` and drop the parametrized default-uri test arm.

### 2b. Public surface with no external consumer (visibility, not death)

345 public defs / **6,879 LOC** across 180 files are referenced *nowhere outside their own
file* yet are used inside it — i.e. `pub`-by-habit API surface that should be
module-private. This is the AGENTS.md "leave the file smaller" lever, not deletable code.
Top files by impact:

| File | defs | LOC | Largest |
|---|---|---|---|
| `jasper/active_speaker/crossover_v2/round_views.py` | 10 | 325 | `FrozenReferenceResult`, `ForwardModelDeltaResult`, `AudibilityMetrics` |
| `jasper/audio_runtime_plan.py` | 13 | 271 | `AudioRouteProfile`, `route_config_hash_for_plan`, `RuntimeSetting` |
| `jasper/calibration_agent/cli.py` | 1 | 229 | `render_markdown` (one 229-line function, used once at `:506`) |
| `jasper/active_speaker/flat_spec_views.py` | 7 | 220 | `PositionFlatness`, `DirectivityBand`, `DirectivityRow` |
| `jasper/fanin/ring_health.py` | 4 | 211 | `ring_wire_declarations` (126), `graph_wire_declarations` (46), `RingWireDeclaration` (32) |
| `jasper/active_speaker/crossover_level_run.py` | 4 | 204 | `CrossoverLevelRunRequest` (172) |
| `jasper/active_speaker/web_measurement.py` | 7 | 181 | `enforce_capture_retention`, `driver_analysis_input_evidence` |
| `jasper/active_speaker/crossover_v2/gate_sweep.py` | 7 | 149 | `window_bias_db`, `fit_notch` |
| `jasper/output_topology.py` | 8 | 142 | `PhysicalOutput`, `TopologyRouting`, `SpeakerPosition` |
| `jasper/active_speaker/commissioning_admission.py` | 2 | 127 | `ActiveCaptureAdmissionHandoff` (124) |
| `jasper/bass_extension/adapters/sealed.py` | 1 | 123 | `SealedAdapter` |
| `jasper/speaker_name_discovery.py` | 2 | 122 | `find_bluetooth_conflicts`, `find_mdns_conflicts` |
| `jasper/web/correction_crossover_v2.py` | 4 | 121 | `refused_from_flow_error`, `CaptureEvidenceCarry` |

Severity: **Nit** individually, **Should-fix** as a policy (10 of the 13 rows are in the
tuning stack, which is the in-flight w6..w9 rightsize zone — hand this table to that
program rather than opening a separate PR).

### 2c. Test-only public surface

625 public defs / **21,767 LOC** are referenced from `tests/` and nowhere else outside
their file. Filtering to those *also* unused inside their own file leaves 80 defs / 4,021
LOC — and **75 of those 80 are `@doctor_check`-decorated registry entries** (live; see
Appendix A). The residue after that filter is exactly §2a plus the three
`*_for_tests` seams in `jasper/web/correction_crossover_v2.py:320,845,928` (14 LOC,
test-only production hooks — Nit, and the honest kind).

---

## 3. Orphan console scripts and dead wizard `main()`s (items 1 + 3)

Fifteen `jasper/web/*_setup.py` modules define `main()`. Only four are reachable: the
three with their own unit (`bluetooth_setup`, `chat_setup`, `correction_setup`,
`system_setup`). Everything else is served by `python -m jasper.web`
(`deploy/jasper-web.service:89`, `deploy/jasper-web-streambox.service:25`), which imports
`make_server` function-locally and never `main`.

| Severity | Dead `main()` | LOC | Reachable via |
|---|---|---|---|
| Should-fix | `jasper/web/voice_setup.py:1758` | 45 | nothing |
| Should-fix | `jasper/web/google_setup.py:1079` | 44 | nothing |
| Should-fix | `jasper/web/wake_setup.py:1073` | 39 | nothing |
| Should-fix | `jasper/web/transit_setup.py:1656` | 38 | nothing |
| Should-fix | `jasper/web/home_assistant_setup.py:1328` | 27 | nothing |
| Should-fix | `jasper/web/wifi_setup.py:1479` | 24 | nothing |
| Should-fix | `jasper/web/rooms_setup.py:1771` | 20 | nothing |
| Should-fix | `jasper/web/sound_setup.py:5476` | 46 | orphan console script `jasper-sound-web` |
| Should-fix | `jasper/web/spotify_setup.py:1421` | 49 | orphan console script `jasper-web` |

Evidence for the two console scripts: `grep -rn "\.venv/bin/jasper-web\b\|bin/jasper-sound-web" deploy/ scripts/ docs/`
returns nothing; `/spotify/` is port 8765 and `/sound/` is port 8784, both bound by
`jasper/web/__main__.py`'s `WizardSpec` table. The name `jasper-web` also names a systemd
unit, a system user and `/usr/share/jasper-web` — that collision is why the orphan
survived a grep-based sweep. Note this is *not* the prior audit's `jasper-web-2#0`
finding (that one covered airplay/sources/speaker/weather/tools, and those five `main()`s
are already gone — verified).

What keeps them alive: `tests/test_console_scripts_import.py:20` imports every
`[project.scripts]` target and asserts the attribute exists, and each wizard test has an
`assert callable(x.main)` line. Deadness enforced by a meta-test that pins the table
rather than a behavior.

**Fix:** delete the nine `main()`s + their `argparse`/`__main__` tails, drop
`jasper-web` and `jasper-sound-web` from `[project.scripts]`, drop the seven
`assert callable(*.main)` lines. ~330 LOC.

| Severity | Console script | LOC | Note |
|---|---|---|---|
| Nit (medium conf.) | `jasper-aec-sweep-config` → `jasper/cli/aec_sweep_config.py` | 122 | Only self-reference is its own `prog=`. No unit, no doc, no UI mention, no test. |
| Nit (medium conf.) | `jasper-wake-corpus-web` → `jasper/web/wake_corpus_setup.py:1263` `main()` | 169 | Duplicates the `/wake-corpus/` route `__main__` already lazily binds on 8782; the module docstring documents the standalone invocation, so it is a *documented* duplicate, not an accident. |

`jasper-google-auth`, `jasper-noise-capture`, `jasper-wake-review`,
`jasper-calibration-agent`, `jasper-audio-validate` all looked orphaned by
deploy/doc reference count but are self-documented operator paths or surfaced in the UI
(`jasper/cli/doctor/aec.py:479` prints `sudo jasper-audio-validate --stdout`;
`jasper/web/wake_setup.py:510` names `jasper-wake-review`). **Earns-its-keep.**

---

## 4. Scripts under `scripts/` and `deploy/bin/` (item 3)

**No orphans.** All 103 `scripts/*` and all 22 `deploy/bin/*` are referenced by CI, a
sibling script, a systemd unit, `deploy/lib/install/*.sh`, or an operator doc. The three
that a basename-only scan flagged (`_sync_measure_audio.py`, `_wake_pipeline_common.py`,
`tuning-llm-live-check.py`) are imported as *module names* without the extension
(`scripts/s0-sync-measure.py:57`, `scripts/_run_wake_training_phase0.py:35`) or cited by
test fixtures — see Appendix A.

Sixteen `scripts/*` are referenced only from docs + tests (e.g. `aec-probe-pinknoise.sh`,
`capture-chip-mic.sh`, `journal-review.sh`, `tense-grep.sh`). These are documented
operator/diagnostic tools; **Earns-its-keep**, listed here only so a later pass does not
re-flag them.

---

## 5. systemd units (item 4)

**No orphans, no missing targets.** All 71 units/sockets/timers/paths/slices/drop-ins
under `deploy/` and `deploy/systemd/` are installed by `deploy/install.sh` or
`deploy/lib/install/*.sh`. Every `ExecStart=`/`ExecStartPre=`/`ExecStop=`/`ExecCondition=`
target resolves: `[project.scripts]` entry point, `deploy/bin/*` (→ `/usr/local/{s,}bin`),
`deploy/usbsink/*` (→ `/usr/local/sbin`, installed at
`deploy/lib/install/systemd-units.sh:494-515`), a Rust binary in `/opt/jasper/bin`, a
third-party binary, or `experiments/usb-turntable/jts_turntable.py`. All ten
`deploy/usbsink/*` helpers are referenced (`jasper-usbgadget-compose.sh` is `source`d,
the rest are Exec targets).

---

## 6. nginx and web pages (item 5)

**No orphans.** Every `proxy_pass` port in `deploy/nginx-jasper.conf` and
`deploy/nginx-jasper-streambox.conf` (8765, 8767–8775, 8777–8780, 8782–8787) maps to a
`WizardSpec` in `jasper/web/__main__.py`, a dedicated unit, or `jasper-control` on 8780.
Every `jasper/web/*.py` module is either a routed page or a helper imported by one. The
streambox conf's omission of `/wake/` and `/wake-corpus/` matches ADR-0217 (streambox has
no wake runtime).

## 7. `deploy/assets` (item 6)

**No orphans** across 85 files. The only two with no code reference are
`fonts/OFL-{Figtree,Outfit}.txt`, required by `LICENSE-third-party.md` and pinned by
`tests/test_landing_page_html.py`. (`shared/js/orbs.js`, the prior audit's
`deploy-assets-0#0`, is already gone — verified absent.)

---

## 8. Rust (item 7)

`cargo-udeps` not installed → not run (per brief). Path-dependency graph read from all 8
`Cargo.toml` files; `pub` items cross-referenced by token against the whole repo.

**No dead crates.** All eight are consumed by a shipping binary:

```
jasper-fanin   (bin) → tts-protocol, env, resampler, ring, host-clock[alsa]
jasper-outputd (bin) → tts-protocol, env, clock, resampler, ring
jasper-clock   ← host-clock, outputd, resampler
jasper-env     ← fanin, outputd      jasper-ring ← fanin, outputd
jasper-resampler ← fanin, outputd, tts-protocol, host-clock(dev)
jasper-host-clock ← fanin            jasper-tts-protocol ← fanin, outputd
```

`jasper-clock` vs `jasper-host-clock` is a real split, not duplication: `jasper-clock` is
the pure DLL, `jasper-host-clock` the ALSA-facing servo built on it. `jasper-env` is
consumed by both daemons. The single non-default feature (`jasper-host-clock/alsa`) is
enabled by `jasper-fanin`.

| Severity | Item | LOC | Evidence |
|---|---|---|---|
| Nit | `rust/jasper-ring/src/writer.rs:495` `pub fn reader_is_live_now` | 9 (incl. doc) | The **only** `pub` item in the workspace with zero references anywhere including its own file. Doc says "Exposed for the daemon's poll/observability" — no daemon calls it. |

84 further `pub` items have no reference outside their defining file (top offenders:
`jasper-host-clock/src/lib.rs` 18, `jasper-resampler/src/lib.rs` 9,
`jasper-fanin/src/impulse_tap.rs` 8). All are used in-file → **visibility over-exposure,
not death**; tighten to `pub(crate)` opportunistically, same class as §2b.

| Severity | Item | LOC | Evidence |
|---|---|---|---|
| Nit (medium conf.) | `c/jts-ring-ioplug/ring_writer_bench.c` + `ring_reader_bench.c` | 322 | Built by `make` (they are in the default `all:` target) but invoked by no CI job, script, or doc — the only mention outside the Makefile is that Makefile's own comment. `test_ring_core.c` by contrast runs in `.github/workflows/tests.yml:540`. Keep if the owner still hand-runs them; otherwise delete with their Makefile rules. |

---

## 9. Tests (item 8)

- **No test imports a non-existent `jasper` module.** AST-checked across all 960 test files.
- **One permanently-skipped test:** `tests/voice_eval/regression/test_barge_in_gemini.py:53`
  `pytestmark = pytest.mark.skip(...)`, containing a single `test_interrupt_mid_tts_gemini`
  whose own docstring says "PLACEHOLDER (skipped at module level)" and that the real
  contract "IS pinned today, in `tests/test_gemini_barge_in.py`". **Should-fix: delete the
  file** (≈95 LOC) — the module docstring already redirects to the live pin, so nothing is
  lost. Every other `pytest.skip` in the suite is a runtime-environment guard
  (`node not on PATH`, `rust source not present`, root/permissions, offline PyPI) —
  legitimate, no unconditional xfails anywhere.
- **Stale prose pointers in tests** (Nit): `tests/test_web_correction_setup.py:1181` cites
  `tests/test_cli_driver_trim.py` (deleted); six files cite
  `tests/test_crossover_v2_conductor.py` (split into `test_crossover_v2_conductor_*.py` —
  the *guard* at `tests/test_crossover_v2_journey.py:648` matches by prefix and is still
  live and correct; only the prose is stale).

---

## 10. Edge directories and data assets (items 9 + 10)

| Dir | Verdict | Evidence |
|---|---|---|
| `experiments/usb-turntable/` | **Live** — production despite the path | `deploy/systemd/jasper-turntable-autostop@.service:13`, `jasper/cli/angle_capture.py`, mypy `files=` |
| `experiments/aec3-v2-deep-tune-spike/` | **Live (README only)** | Code already deleted; README cited by `jasper_aec3/setup.py`, `jasper_aec3/src/aec3_binding_v2.cpp`, `deploy/lib/install/python-runtime.sh` |
| `jasper/research/` (1,318 LOC) | **Live** | `jasper/voice_daemon.py:72`, `jasper/voice/daemon_main.py:47`, `jasper/tools/packs.py:40`, `jasper/control/state_aggregate.py:556` |
| `wake_training/` (2 files) | **Live, not shipped** | Imported by `scripts/_build_wake_{,negative_}feature_bank.py`; excluded from the wheel by `[tool.setuptools.packages.find] include = ["jasper","jasper.*"]` |
| `jasper_aec3/` | **Live** | `jasper-enhanced-aec-install` / `deploy/systemd/jasper-enhanced-aec-install.service` |
| `docs/research/` (10 campaigns) | **Live** — 9 of 10 externally cited | `2026-07-25-flat-linearization` has 0 external citations but its siblings cite it internally; leave |
| `docs/historical/` (23 files) | 21 cited by an ADR, a doc, or code | **2 orphans**, below |
| `logs/`, `release/`, `c/` | clean | `logs/` is a tracked `.gitkeep` only |

| Severity | Orphan doc | LOC |
|---|---|---|
| Should-fix | `docs/historical/CLEANUP-moode-removal.md` | 1,040 |
| Nit | `docs/historical/LAUNCH-READINESS.md` | 86 |

Neither is referenced by any file in the repo, **including `docs/doc-map.toml`** — so they
are outside the doc-governance registry too. `CLEANUP-moode-removal.md` additionally
points at `deploy/debian-stack/` and `jasper/cli/doctor.py`, both of which no longer exist.

**Data/model/config assets (item 10): no orphans.** All 46 non-Markdown data files under
`jasper/`, `deploy/`, `wake_training/`, `c/`, `release/` have a reader — including
`jasper/data/mta_stations.csv` (`jasper/transit/_mta_stations.py:65`
`resources.files("jasper.data")`), `jasper/data/model_pricing.json`, both
`jasper/active_speaker/presets/*.json`, all udev/polkit/tmpfiles/alsa/modprobe conf, and
`release/first-party-arm64/*`.

### Stale pointers found while checking (Nit, doc-hygiene, ~0 LOC)

| File:line | Points at | Reality |
|---|---|---|
| `jasper/cli/doctor/renderers.py:297` | `deploy/debian-stack/README.md` in an **operator-facing doctor message** | Directory does not exist — the check tells the user to source-build "per" a deleted path |
| `jasper/cli/doctor/{renderers,grouping,memory,network,usbsink}.py:7-8` | `jasper/cli/doctor.py` ("re-homed verbatim from the monolithic…") | File deleted in the doctor split; also pure history narration, which AGENTS.md bans |
| `jasper/active_speaker/crossover_envelope_v2.py:8` | `docs/crossover-measurement-productization-design.md` | Moved to `docs/historical/`; six sibling modules already use the corrected path |
| `jasper/audio_measurement/alignment.py` | `docs/crossover-measurement-reproducibility-plan.md` | Same — now under `docs/historical/` |

---

## 11. Removable LOC summary

### High confidence — delete now

| Item | Product LOC | Test LOC |
|---|---|---|
| 7 dead wizard `main()`s (voice, google, wake, transit, ha, wifi, rooms) | 237 | ~7 |
| `sound_setup.main` + `jasper-sound-web` entry point | 47 | 1 |
| `spotify_setup.main` + `jasper-web` entry point | 50 | 1 |
| `DelayCandidateConfirmation` | 36 | 0 |
| `snapcast_rpc.set_client_volume` / `set_group_mute` | 35 | ~40 |
| `calibration.supported_model_options` | 14 | 0 |
| `default_{spotify,google}_redirect_uri` | 8 | ~10 |
| `jasper-ring reader_is_live_now` (Rust) | 9 | 0 |
| `sweep_spec.ui_level_meter` | 2 | 0 |
| `tests/voice_eval/regression/test_barge_in_gemini.py` | 0 | 95 |
| **Subtotal code** | **438** | **~154** |
| `docs/historical/CLEANUP-moode-removal.md` + `LAUNCH-READINESS.md` | 1,126 (docs) | — |

### Medium confidence — owner call

| Item | LOC |
|---|---|
| `jasper-aec-sweep-config` CLI (`jasper/cli/aec_sweep_config.py` + entry point) | 122 |
| `wake_corpus_setup.main` + `jasper-wake-corpus-web` entry point (duplicate of the routed page) | 169 |
| `c/jts-ring-ioplug/ring_{writer,reader}_bench.c` + Makefile rules | 322 |
| **Subtotal** | **613** |

### Reported, not removable

| Item | LOC | Why |
|---|---|---|
| `jasper/bass_extension/` unreachable half | 4,149 (+~10,035 test) | **ADR-0018 parks it.** Needs a new owner ruling, not an audit finding. |
| Public-but-file-local surface, Python | 6,879 | Visibility tightening, not deletion; 10 of the 13 top files are in the in-flight tuning rightsize zone |
| Public-but-file-local surface, Rust | 84 items | Same; `pub` → `pub(crate)` |

**Grand total genuinely removable today: ~440 LOC product + ~154 LOC test + 1,126 doc
lines; ~615 LOC more on an owner call.** For a 424K-line `jasper/` tree that is a 0.1%
orphan rate — the dead-code axis is essentially closed, and the real mass sits in prose,
test altitude and the parked package, all of which belong to other lenses.

---

## Appendix A — rejected candidates (verified live)

Listed so the verifier can see each indirection was checked rather than assumed.

| Candidate | Why it looked dead | Why it is live |
|---|---|---|
| `jasper/cli/doctor/{env,voice,audio,boot_config,wake,renderers,integrations,privsep,secret_compartments,web,research,correction,memory,drift,resilience,aec,audio_runtime_*,usbsink,network,peering,grouping}.py` (24 modules, ~17K LOC) | no `import` statement names them | `jasper/cli/doctor/__init__.py:30` `importlib.import_module(f".{_name}", __package__)` over `_registry.py::MODULE_ROSTER` |
| 75 `check_*` functions in those modules (incl. `check_spotify_cache`, `check_google_tokens`, `check_avahi_daemon`, `check_hostname_avahi_consistency`, `check_wake_legs_configured`, `check_speaker_name`, `check_*_readable_inputs`, `check_supervisor_reboot_state`, `check_seat_level_reference`, `check_mux_mode_state`) | zero call sites | `@doctor_check(...)` decorator registers them at import; verified by AST decorator dump on every flagged def |
| `jasper/web/{voice,google,airplay,sources,wake,wifi,transit,home_assistant,weather,speaker,sound,rooms,tools,spotify,wake_corpus}_setup.py` | zero module-level importers | function-local `from . import X_setup` inside `jasper/web/__main__.py:247-444` |
| `jasper/web/__main__.py` | nothing imports it | `ExecStart=… python -m jasper.web` (`deploy/jasper-web.service:89`) |
| `jasper/control/ha_probe_child.py` | zero importers | `jasper/control/ha_status_cache.py:224` spawns `[python, "-m", "jasper.control.ha_probe_child"]` (ADR-0171) |
| `jasper/data` | zero importers | `resources.files("jasper.data")` at `jasper/transit/_mta_stations.py:65` |
| `jasper/voice/model_discovery.py`, `jasper/transit/geocode.py`, `jasper/audio_validation_route.py` | first-pass reachability artifact | imported by `web/voice_setup.py:76`, `web/transit_setup.py`, `cli/doctor/usbsink.py:38` — all reachable once the wizard/doctor roots are correct |
| `jasper/active_speaker/bench` (package `__init__`) | "test-only" | its submodules are imported by `jasper/cli/active_speaker_emit_bench.py:49-51`, which imports the package implicitly |
| `jasper/fanin/ring_health.py` `ring_wire_declarations` / `graph_wire_declarations` / `RingWireDeclaration` (204 LOC) | zero references outside the file | called in-file at `:491`, `:623`, `:1049` — visibility issue only (§2b) |
| `jasper/calibration_agent/cli.py::render_markdown` (229 LOC) | zero external refs | used at `:506` |
| `jasper/speaker_name_discovery.py::find_{mdns,bluetooth}_conflicts` | zero external refs | called by `find_name_conflicts`, which `jasper/web/speaker_setup.py:45` imports |
| `jasper/audio_measurement/program_analysis/dispatch.py` (1,068 LOC) and the other 21 "`__init__`-only" modules | only importer is a package `__init__` | the `__init__` is the intended facade and its names are imported from outside (e.g. `crossover_v2_flow.py:214`) |
| `jasper/transit/providers/nyc_bus.py` | only importer is `transit/__init__.py` | string dispatch `transit.by_id("nyc_bus")`, `jasper/web/transit_setup.py:346` |
| `scripts/_sync_measure_audio.py`, `scripts/_wake_pipeline_common.py` | basename appears nowhere | imported as modules: `scripts/s0-sync-measure.py:57`, `scripts/multiroom-spike-measure.py:75`, `scripts/_run_wake_training_phase0.py:35` |
| `scripts/tuning-llm-live-check.py` | no deploy/doc ref | cited by `tests/test_calibration_agent_correction_advisor.py:48` and by three captured fixtures as their provenance |
| `deploy/usbsink/{jasper-usbgadget-down,-wanted,-compose.sh,jasper-usbmic-apply-result,uac2_name_patch.py}` | not `ExecStart` targets | `ExecStop=`/`ExecCondition=`/`ExecStopPost=` in `jasper-usbgadget.service` and `jasper-usbmic-apply.service`; `compose.sh` is `source`d |
| `deploy/assets/fonts/OFL-*.txt` | no code reference | required by `LICENSE-third-party.md`, pinned by `tests/test_landing_page_html.py` |
| `tests/test_crossover_v2_conductor.py` guard in `test_crossover_v2_journey.py` | names a file that does not exist | matches the live `test_crossover_v2_conductor_*.py` family **by prefix** — guard is sound |
| `jasper/{bass_extension/runtime.py,bass_extension/scheduler.py,web/bassext_backend.py}` referenced by `tests/test_bass_extension_plan_status.py` | paths do not exist | deliberate non-existence assertions (ADR-0018 §2) |
| `rust/jasper-clock` vs `rust/jasper-host-clock`, `rust/jasper-env` | suspected dead/duplicate crates | all three are path-dependencies of a shipping binary; see §8 graph |

---

## Coverage

**Opened / analysed directly:** `pyproject.toml` (all sections), `AGENTS.md`,
`deploy/install.sh` (grep-level) + `deploy/lib/install/*` (file list + `systemd-units.sh`
install lines), all 71 systemd units' `Exec*` lines, both nginx confs' full location list,
`deploy/usbsink/*`, `deploy/assets/**` (85 files, reference scan), all 8 Rust
`Cargo.toml`, `c/jts-ring-ioplug/Makefile`, `jasper/cli/doctor/{__init__,_registry}.py`,
`jasper/active_speaker/__init__.py` lazy map, `jasper/web/__main__.py`,
`jasper/cli/bass_extension_bench.py`, `jasper/bass_extension/bench/{__init__,executor}.py`,
`jasper/fanin/ring_health.py`, `jasper/multiroom/snapcast_rpc.py`,
`jasper/{spotify,google}_oauth.py`, `jasper/audio_measurement/{delay_graph,calibration}.py`,
`rust/jasper-{ring/src/writer.rs,resampler/src/lib.rs}`,
`tests/test_console_scripts_import.py`,
`tests/voice_eval/regression/test_barge_in_gemini.py`,
`tests/test_crossover_v2_journey.py`, ADR-0018, `docs/DEEP-AUDIT-2026-08-25.md` §1-4.1.

**Machine-analysed (whole-tree, scripts in the scratchpad):** `importgraph.py` (AST import
graph, 749 modules, 0 parse errors), `reach.py` (BFS reachability with dynamic roots),
`ship.py` (shipping-root-only reachability), `deadnames.py` + `deadnames2.py` (4,304
public defs vs. a whole-repo token index, then AST code-vs-prose disambiguation),
`testonly.py`/`testonly2.py`, `scripts.py` (script reference matrix), plus inline scans
for systemd units, nginx routes, assets, Rust `pub` items, data assets, non-existent
repo-path references, and test skip/xfail markers.

**Skipped, and why:**
- `cargo build` / `cargo udeps` — `cargo-udeps` is not installed and the brief forbids
  installing; Rust findings are therefore static-analysis only (the crate graph is exact,
  the `pub`-item scan is token-based and may over-report methods with common names — this
  affects §8's 84-item visibility list, not the single dead item).
- `vulture` — would have required writing into `.venv/` inside the read-only checkout.
  Replaced by the token+AST scan described above, which is *more* conservative (any
  identifier occurrence anywhere, including strings and docs, counts as a reference), so
  §2a under-reports rather than over-reports.
- Dynamic references through data files not present in the repo (e.g. a `/var/lib/jasper`
  state file naming a module) cannot be checked statically; none of the §11 items depend
  on such a path.
- Prose/comment volume, test-altitude duplication, config sprawl, and the tuning stack's
  internal architecture — other lenses' scope.
- Line-by-line reading of the 4,304 public defs: only the ~90 that survived the automated
  filters were opened individually.
