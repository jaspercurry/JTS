# P3 — adversarial verification of DELETION claims

Repo @ `2d571e6b8`, read-only. Scripts: `scratchpad/P3-deletions/{census_barrel,census2,census_lazy,census_pa,census_wcs,check_orphans}.py`.
Every "zero callers" below was re-run against `jasper/ rust/ c/ deploy/ scripts/ tests/ .github/ docs/ pyproject.toml`,
including systemd `Exec*=`, `deploy/bin/`, nginx confs, `deploy/lib/install/*.sh`, `deploy/assets/**/*.{js,mjs}`,
doctor `@doctor_check`/`MODULE_ROSTER`, `jasper/tools`, `jasper/cues/registry.py`, and `importlib`/`getattr` string dispatch.

Verdict key: **C-dead** = no reference anywhere; **C-test** = referenced only from `tests/`;
**REFUTED** = a real production caller exists; **PARTIAL** = the claim's boundary is wrong, corrected inline.

---

## 1 — p1-T09 · peering dead mDNS/STATUS half

| sub-claim | verdict | evidence |
|---|---|---|
| `peering/discovery.py` (287) dead | **C-test** | only importers: `peering/daemon.py:177` (lazy, inside the dead wiring) and `tests/test_peering_discovery.py:16`. |
| `_on_discovery_event`/`_known_peers`/`_handle_status` chain feeds nothing | **C-dead** | `_known_peers` (daemon.py:125) is written at `:283,338,345`, read **only** by `_handle_status` (`:422-436`); `_handle_status` is reachable only via UDS `STATUS`. `voice_daemon._peering_send` is the only client and is called at `:4011,4032,4038` with `ARBITRATE` / `SESSION_STARTED` / `SESSION_ENDED` — never `STATUS`/`PING`. `/rooms.json` peers come from `jasper.mdns.browse_once` (`web/rooms_setup.py:71,255`, `CONTROL_MDNS_TYPE`), not the socket. |
| `uds.py:22` "PING used by doctor's liveness check" | **C-dead / docstring is FALSE** | `cli/doctor/peering.py` has exactly two checks: `check_peering_mode` (env parse) and `check_peering_discovery` (`shutil.which("avahi-browse")` + `_run([...,"_jasper-peer._udp"])`, `:82`). No socket, no `send_request`. |
| `avahi.py` must stay | **CONFIRMED (keep)** | its advert `_jasper-peer._udp` is what `check_peering_discovery` browses (`doctor/peering.py:83`), i.e. read by *other* speakers' doctor. |

**Order:** delete `tests/test_peering_discovery.py` → daemon `_prune_stale_peers`/`_on_discovery_event`/`_handle_status`/`_known_peers`/`_last_decision`/`STALE_PEER_THRESHOLD_SEC` + the `:177-186` and `:246-251` wiring + `status=` at `:194` → `uds.py` `status` param + `STATUS`/`PING` branches (`:111-115`) + the docstring lines `:20-23` → `discovery.py`. `zeroconf` stays a dep (`jasper/mdns.py`).
**LOC:** product 287 + 52 (3 daemon methods, AST) + ~20 wiring/const + ~14 uds ≈ **373**. Tests 405 + ~60 (`test_peering_daemon.py:324,360-388`) + ~20 (`test_peering_uds.py:114-121`) ≈ **485**.

---

## 2 — p1-T15 / p0-orphans · `jasper/bass_extension/` boundary

The claim's "zero importers" is wrong for four of the six files. Corrected boundary:

| file | LOC | verdict | evidence |
|---|---|---|---|
| `limiter_evidence.py` | 1213 | **C-test (transitively)** | one in-package importer: `ladder.py:609` (lazy, in-function). `ladder.py` itself has zero production callers. Direct test importers: `tests/test_bass_extension_limiter_evidence.py:18`, `test_bass_extension_ladder.py:25`, `test_bass_extension_bench_executor.py:1196`. |
| `bench/executor.py` | 1200 | **C-test** — *not* zero-importer | `tests/test_bass_extension_bench_executor.py:40,847`. Zero production importers confirmed. `cli/bass_extension_bench.py:166` imports only `runner.Stop`; `_run_live` unconditionally `raise SystemExit` naming #1738 — verified at `:181-190`. |
| `bench/stimulus.py` | 285 | **C-test** | `tests/test_bass_extension_bench_stimulus.py:21`, `test_..._executor.py:40`. |
| `bench/live_proof.py` | 250 | **C-test** | `tests/test_bass_extension_bench_live_proof.py:15`, `test_..._executor.py:40`. |
| `bench/cross_check.py` | 353 | **C-test** | `tests/test_bass_extension_bench_cross_check.py:22`. |
| `bench/excitation.py` | 85 | **C-dead** (only true zero-importer) | repo-wide: one hit, a docstring bullet at `bench/__init__.py:22`. The `from .excitation import` at `audio_measurement/sweep.py:23` is a *different* `excitation` module. |
| `__init__.py` | 730 | **PARTIAL — file is LIVE** | `apply_bass_extension`/`bypass_bass_extension`/`recover_pending_bass_extension_apply` have zero production callers (tests only), but `BASS_EXTENSION_APPLY_INTENT_PATH` from the same file is imported by **6 production modules**: `active_speaker/commissioning_apply.py:911`, `active_speaker/runtime_contract.py:4378,4666`, `dsp_apply.py:571`, `web/correction_setup.py:1359`, `multiroom/follower_config.py:523`. The 730 is not removable; the apply pathway inside it is. |
| live set (`profile`, `targets`, `alignment`, `adapters/*`, `bench/{render,derivation,manifest,activation,context}`) | — | **CONFIRMED live** | `bench/render` ← `active_speaker/bench/{compare,loop}.py`, `cli/{bass_extension_bench,active_speaker_emit_bench}.py`; `bench/derivation` ← `active_speaker/bench/{loop,derivation}.py`, `active_speaker/branch_peak.py:35`; `bench/{context,manifest}` ← `cli/bass_extension_bench.py:32,37`; `activation` ← `derivation.py:108` (transitive-live). |

**Blocker on deletion:** ADR-0018 (`docs/adr/0018-bass-extension-stays-parked.md`, Accepted 2026-08-25) rules the package parked, names the deadness-enforcing tests as the park mechanism (§2), and explicitly names `bench/excitation.py` as "a zero-importer module to wire or delete **when bass resumes**" (§3). Deleting on orphan grounds alone is exactly what the ADR exists to prevent.
**LOC removable today: 0.** If a fresh ADR lifts the park: ~3,997 product (limiter_evidence 1213 + executor 1200 + ladder 611 + cross_check 353 + stimulus 285 + live_proof 250 + excitation 85) and ~3,915 test.

---

## 3 — p1-T14-2 · `crossover_v2_flow.py` barrel census

**PARTIAL — reproduced with different splits.** 134 `NAME = _mod.NAME` lines confirmed (`grep -c '^[A-Za-z_][A-Za-z0-9_]* = _[a-z_]*\.'` = 134, file 4,636 LOC). Reading *through the barrel* (`from …crossover_v2_flow import X` / `crossover_v2_flow.X` / `flow.X`, `census2.py`):

- **33** read by production (report said 26) — e.g. `TIER_FULL`, `tier_display_info`, `resolve_plan_shape`, `V2ConductorSnapshot`, `CloudPositionPrompt`, `announced_capture_indexes`, consumed by `jasper/web/correction_crossover_v2.py` and `cli/`.
- **87** test-only (report said 93).
- **14** read by nobody (report said 15): `DECLARED_GEOMETRY_PATH`, `DEFAULT_TIER`, `_CloudFitEvidence`, `_LATERAL_POSE`, `_LinearizationState`, `_SpeculativeClose`, `_attempt_optional_float`, `_band_edge`, `_flatness_tilt_log_field`, `_geometry_verdict_from_combined`, `_verify_evidence_from_tracking`, `_verify_frame_from_tracking`, `cloud_validity_floor_hz`, `committed_crossover_region_hz`.

Direction confirmed: 101 of 134 re-export lines carry no production reader. Note the *symbols* stay — only the alias lines go.
**LOC:** **14** removable with no other change; **+87** after pointing tests at the owning organ module (`crossover_v2/*`).

---

## 4 — p1-T14-1 · `active_speaker/__init__.py` `_LAZY_ATTRS`

**CONFIRMED, exactly.** 207 entries; `census_lazy.py` gives **143 with zero readers**, 61 test-only, and **3** with production readers: `LocalSubwoofer`, `emit_active_speaker_driver_domain_config`, `emit_active_speaker_program_bake_config`. Kept alive by `tests/test_active_speaker_package.py:14` (`@pytest.mark.parametrize("name", active_speaker.__all__)`) which resolves `_LAZY_ATTRS[name]` at `:23` — a tautology test, not a behavior pin.
**Order:** delete the 143 dict rows, then the parametrized test (it becomes a 3-name check). `__all__ = sorted(_LAZY_ATTRS)` and `__getattr__` stay.
**LOC:** **143** product, ~10 test.

---

## 5 — p1-T12-1/12-2 · `program_analysis/__init__.py` private re-exports

**PARTIAL (undercount in the claim).** 312-LOC file publishes **32** private names in `__all__` (confirmed). `census_pa.py`: **23** have zero production consumers (claim said ~20-21); **9** are live:

| private name | production consumer |
|---|---|
| `_aligned_branch_tf` | `active_speaker/crossover_v2/intervention.py` |
| `_build_candidate` | `active_speaker/crossover_v2_flow.py` |
| `_compose_configured_path_ir` | `active_speaker/crossover_v2/priors.py` |
| `_deconvolve_window` | `audio_measurement/distortion.py` |
| `_n_fft_for` | `active_speaker/crossover_v2/spatial.py`, `audio_measurement/spatial_combine.py` |
| `_verify_capture_integrity` | `crossover_v2/capture_dispatch.py`, `audio_measurement/frame_ledger.py` |
| `_estimate_drift` | `crossover_v2/harmonic_evidence.py:803`, `crossover_v2/capture_dispatch.py` |
| `_global_offset` | `crossover_v2/harmonic_evidence.py:804` |
| `_locate_segments` | `crossover_v2/harmonic_evidence.py:805` |

The three named in the claim are **CONFIRMED** cross-package private reaches — `harmonic_evidence.py:799-805` imports them in a function body and calls them at `:843-845`; `:55-56` documents the reach as deliberate.
**LOC:** **~46** (23 import lines + 23 `__all__` lines), conditional on redirecting the test importers to the owning submodules (`signals`, `locate`, `drift`, `response`, `check`).

---

## 6 — p1-T06 · `jasper/audio_hardware/__init__.py`

**C-dead. CONFIRMED.** 86 LOC, 38 re-exports from `.dac`. Repo-wide `grep -E "from [.a-z_]*audio_hardware import"` filtered to non-submodule targets returns **zero rows**. All 25 production importers go to a submodule (`audio_hardware.dac`, `.usb_port_role`, `.hat_eeprom`) or bind the submodule itself (`from jasper.audio_hardware import dac as dac_registry` — `cli/aec_init.py:30`, `cli/aec_commission.py:32`, `chip_aec/policy.py:43`).
**Order:** delete the import block + `__all__`, keep a docstring-only `__init__.py` (package marker). **LOC: ~80.**

---

## 7 — p0-orphans / p1-T17-1 / p1-T16-3 · dead wizard `main()`s + console scripts

**PARTIAL — 9 dead, not 10; and the "everything is `python -m jasper.web`" premise is half-wrong.**

Four wizards *do* run as console scripts with real `ExecStart=`: `jasper-bluetooth-web` (`deploy/jasper-bluetooth-web.service:21`), `jasper-chat-web` (`:21`), `jasper-correction-web` (`:21`), `jasper-system-web` (`deploy/jasper-system-web.service:18`). Only `deploy/jasper-web.service:89` and `jasper-web-streambox.service:25` run `python -m jasper.web`.

| `main()` | verdict |
|---|---|
| `web/spotify_setup.py:1421` (49) | **C-test** — `jasper-web` console script has no `ExecStart`; only `tests/test_web_spotify_setup.py:317` `assert callable(...)` |
| `web/sound_setup.py:5476` (46) | **C-dead** — `jasper-sound-web` has no `ExecStart` and no test reference at all |
| `web/google_setup.py:1079` (44) | **C-test** |
| `web/home_assistant_setup.py:1328` (27) | **C-dead** — no console script, no test |
| `web/rooms_setup.py:1771` (20) | **C-test** |
| `web/transit_setup.py:1656` (38) | **C-test** |
| `web/voice_setup.py:1758` (45) | **C-test** |
| `web/wake_setup.py:1073` (39) | **C-test** |
| `web/wifi_setup.py:1479` (24) | **C-test** |
| `web/wake_corpus_setup.py:1263` | **REFUTED** — `jasper-wake-corpus-web` is an operator-invoked CLI documented in its own module docstring (`:43,48` `sudo /opt/jasper/.venv/bin/jasper-wake-corpus-web …`) and named as the producer by `scripts/_audit_wake_corpus.py:7`. Keep. |

`tests/test_console_scripts_import.py` (33 LOC) only asserts each `[project.scripts]` target imports — it does not keep them alive, it just fails if you drop the `main()` without dropping the pyproject line.
**Order:** delete the two `[project.scripts]` rows (`pyproject.toml:176,218`) in the same commit as the two `main()`s; the other 7 have no pyproject row.
**LOC: 332** product + 2 pyproject lines; ~8 test assertions.

---

## 8 — p1-T11 · `correction/level_match.py`

**PARTIAL — the claim's file and the claim's dead half are different objects.**

- `level_match.py` (899) is **LIVE**: `web/correction_crossover_backend.py:60,154,441,570` (`LevelMatchOutcome`, `LevelMatchSession`, `LevelLockStore`) and `correction/session.py:60-61,340`. Not deletable; T11's own recommendation is *move to `jasper/audio_measurement/`*, not delete.
- The dead half is on **`correction/session.py::MeasurementSession`**: `run_level_match` (`:2244-2338`, 95), `ensure_level_match_volume` (`:2376-2414`, 39), `lock_level_match` (`:2416-2421`, 6) — **C-test**, only `tests/test_correction_level_match.py` / `test_crossover_driver_level_domain.py`. Sibling `restore_level_match_volume` (`:2340`) **is live** (`web/correction_setup.py:1827,3230`), so deletion must not take the whole block.
- Second dead half, inside `level_match.py`: the refusal machinery `describe_ramp_refusal` / `RampRefusal` / `LevelMatchRefused` / `_RAMP_REFUSAL_COPY` / `_NOT_LOCKED_MESSAGE` (`:534-681`, ~148) — **C-test** (`tests/test_correction_level_match.py:33,38`); the only other mention is a pointer comment at `audio_measurement/ramp.py:576`.

**LOC: 140** (session.py) + **148** (level_match refusal) = **288**; ~180 test.

---

## 9 — p1-T14-3 · audio-lab aplay tone backend

**C-test. CONFIRMED line for line.**
- `NullTonePlaybackBackend.audio_backend = False` (`playback.py:782`), `WavArtifactTonePlaybackBackend.audio_backend = False` (`:818`), `AplayTonePlaybackBackend.audio_backend = True` (`:904`) — the only `True`.
- `AplayTonePlaybackBackend` constructors outside `playback.py`: `tests/test_active_speaker_playback.py` only (10+ sites) plus the lazy-attr row `active_speaker/__init__.py:29`.
- All four production call sites pass `backend=None`: `web/sound_setup.py:3039,4316`, `active_speaker/web_commissioning.py:2100,2304`.
- `tone_backend_status` (`:273-375`, 103 LOC) hard-codes `audio_enabled = False` at `:350` and emits the `tone_backend_not_wired` blocker `"…selects a backend nothing wires"` at `:341-349`. The one dynamic read, `audio_backend = bool(getattr(selected, "audio_backend", False))` (`:1016`), can only ever see the two `False` backends.
- `jasper/audio_lab.py` is 11 LOC defining only `AUDIO_LAB_TONE_BACKEND_ENV` / `AUDIO_LAB_TEST_PCM_ENV`; its sole importer is `playback.py:39`.
- Both knobs appear nowhere outside `playback.py`, tests, and `docs/historical/`.

**LOC:** ~330 product (class 900-997 = 98, `tone_backend_status` reduction ~73, the `FORBIDDEN_TEST_PCM_TOKENS` fence + `allow_audio` ladder ≈ 150, `audio_lab.py` 11) + 3 of 4 functions in `audible_policy.py` (80 LOC file). Test: most of `tests/test_active_speaker_playback.py` (1,231) — ~600 conservatively.

---

## 10 — p1-T07 · XVF `CLEAR_CONFIGURATION`

**C-dead. CONFIRMED.** Repo-wide (all file types, `.git` excluded) exactly 3 hits: the table row `xvf_host.py:89` and two *exclusions* in `tests/test_xvf_host.py:19,227`. No consumer in `jasper/ scripts/ deploy/ docs/ experiments/`. Write-only command per its own `"wo"` mode field.
**LOC: 1** product, ~2 test-line edits. (Deleting it also shrinks the `REBOOT|CLEAR_CONFIGURATION` allowlist regex — check `test_xvf_host.py:19` still matches.)

---

## 11 — p1-T04 · `bt_roles.json` write-only + fan-in AUTO

| sub-claim | verdict | evidence |
|---|---|---|
| `RoleStore.get` zero callers | **C-dead** | `roles.py:55`; engine calls only `.set` (`engine.py:582`) and `.remove` (`:690`). Nothing ever reads the mac→handler map back. |
| `BluetoothEngine.roles` property zero callers | **C-dead** | `engine.py:197-198`, no reader in `jasper/ tests/ deploy/ scripts/`. |
| whole 81-line module + installer + doctor entries | **CONFIRMED (chained)** | installer: `deploy/lib/install/env-migrations.sh:127` (`f:0640:${STATE_DIR}/bt_roles.json`); doctor: `cli/doctor/privsep.py:159,217`; tests `test_bluetooth_roles.py`, `test_doctor_privsep.py:101`, `test_install_state_group_write.py:137`. |
| `VolumePersistence.maybe_save` / `save_pre_mute_level` test-only | **C-test** | `volume_persistence.py:329-352` (24) and `:354-363` (10); readers only in `tests/test_volume_persistence.py`, `tests/test_volume_coordinator.py`. |
| `Mux._fanin_auto*` test-only ⇒ Rust `AUTO` unreachable | **C-dead (stronger than claimed)** | `_fanin_auto_best_effort` (`mux.py:1436-1440`) has **zero callers anywhere** — not even tests. `_fanin_auto` (`:1395-1396`) is called only by that dead wrapper and by `tests/test_mux.py:1263`. So `rust/jasper-fanin/src/state.rs:357` `"AUTO" =>` has no live producer. |

**Order:** engine call sites → `roles.py` → installer row → doctor rows → tests. Keep the doctor mode-check *mechanism* (it covers ~50 other paths).
**LOC:** ~90 (roles chain) + 34 (volume) + 7 (mux) = **131** product; ~15 Rust arm; ~120 test.

---

## 12 — p1-T02 · wake-corpus / tts_routing / wake_fusion / tools.audio

| sub-claim | verdict | evidence |
|---|---|---|
| `wake_corpus/capture_plan.py:158 build_capture_plan` dead | **C-dead. CONFIRMED** | zero importers. The `build_capture_plan` used by `web/wake_corpus_setup.py:170,635,674,749` and `wake_corpus/recording_backend.py:75,1089` is **`bridge_session.build_capture_plan` (`bridge_session.py:2395`)**, a different function. The only importers of `capture_plan` (`cli/aec_bridge_config.py:33`, `bridge_session.py:88`, `tests/wake_corpus_setup_fixtures.py:38`) take other names. Its cycle-dodging `from . import bridge_session` at `:160` dies with it. **31 LOC.** |
| `tts_routing.resolved_tts_routing_env` dead | **C-dead. CONFIRMED** | 2 hits total: the def (`tts_routing.py:36-51`) and a docstring mention at `cli/doctor/grouping.py:670`. **16 LOC.** |
| `wake_fusion.py`'s whole effect is `x + 0.0` / `return True` | **CONFIRMED (but it IS wired)** | `effective_threshold` returns `base_threshold + self._offsets.get(..., 0.0)`; `verify` returns `True` unconditionally. `voice_daemon.py:1000` constructs `WakeFuser()` with **no offsets** and calls it at `:3394,3423,3445`. So it is a live no-op seam, not an orphan — deleting it means inlining three call sites, and `verify`'s fail-open contract is on the wake path (non-negotiable #6 adjacency). **Judgment call, 74 LOC.** |
| 3 symbols in `tools/audio.py` alive only for their own test | **PARTIAL — 1 fully dead, 4 test-only** | `DEFAULT_STEP_PERCENT` (`:44`): **zero** references repo-wide. `_percent_to_db`/`_db_to_percent`/`VOLUME_MIN_DB`/`VOLUME_MAX_DB` (`:38-39,76-81`): the *tools/audio copies* are read only by `tests/test_tools_audio.py:10-13,76-83`; the identically-named production symbols live in `control/volume_ops.py:35,36,57,61`. ~12 LOC. |
| `web/wake_corpus_setup.py` 76-name facade, ≥26 unconsumed | **CONFIRMED (28)** | `census_wcs.py`: 76 names across the re-export blocks; **28** appear nowhere but the import block and are never reached as `wake_corpus_setup.<name>` — incl. `AEC_MODE_PATH`, `BRIDGE_STATS_PATH`, `CHIP_AEC_LEGS`, `DEFAULT_OUTPUT_DIR`, `MAX_RECORDING_DURATION_SEC`, `OUTPUTD_REF_UDP_PORT`, `restart_unit`, `set_voice_daemon_state`. **~28 LOC.** |

**LOC: 87** confirmed product (31 + 16 + 12 + 28), plus 74 conditional (`wake_fusion`).

---

## 13 — p1-T21 / p1-T20 · `rust/jasper-host-clock` and ring/resampler symbols

| sub-claim | verdict | evidence |
|---|---|---|
| crate consumed only by jasper-fanin, not outputd | **CONFIRMED** | the only `jasper-host-clock = { path = … }` dependency row is `rust/jasper-fanin/Cargo.toml:34`. Also staged by `deploy/lib/install/rust-daemons.sh:194` and linted by `scripts/check-rust.sh:23`. Crate is **live**, not deletable — 3,804 LOC in `src/lib.rs`. |
| `Dll` constructed but `update()` never called | **CONFIRMED** | `Dll::new(...)` at `lib.rs:810`; every `self.dll.*` call is `error_mean()` (`:896`), `is_locked()` (`:899`), `reset()` (`:1176,1236,1440,1467,1692`). No `self.dll.update`. The `.update(` hits at `:1038,2034-2038` are `self.slope.update`. So `dll_err_frames()`/`dll_locked()` publish constants into the `/state` JSON at `:1830-1831`. |
| "pinned by a Python contract test" | **REFUTED** | `grep -rn "dll_" --include=*.py tests/ jasper/` returns one unrelated comment (`tests/test_wire_contracts.py:76`, about `push_dll_rate_diff`). No Python test asserts `dll_err_frames` or `dll_locked`. |
| `RingWriter::reader_is_live_now` | **C-dead** | `rust/jasper-ring/src/writer.rs:495` — sole occurrence in the tree. |
| `RingWriter::free_slots` | **C-test + duplicate** | defined **twice** (`jasper-ring/src/lib.rs:1427` and `src/writer.rs:483`); every call site is a `#[cfg(test)]` assert (`lib.rs:1637,1644`, `writer.rs:648`). |
| `AudioRing::trim_to` | **C-test** | `jasper-resampler/src/lib.rs:667`; calls only at `:2090,2119,2121,2123` (tests) + a doc line. |
| `BlockResampler` / `resample_i16` | **C-test/example** | `BlockResampler` is constructed at `lib.rs:1029` (inside `resample_i16`) and in tests; `resample_i16` is called only from tests and `jasper-resampler/examples/golden_vector.rs:29`. No consumer in `jasper-fanin` or `jasper-outputd`. |
| `HostClockConfig::target_fill_frames` getter | **C-test** | `lib.rs:889-890`; only callers `:2934,2936,2939,2941` (one test). The `target_fill_frames` hits in `jasper-fanin/src/state.rs` are a different struct's field on the report payload. |

**LOC: ~233** (reader_is_live_now ~8, free_slots ×2 ~16, trim_to ~25, BlockResampler+resample_i16 ~180, getter ~4). The `Dll` is a **fix-or-delete decision**, not a deletion: either call `update()` or stop publishing two constants to `/state`.

---

## 14 — p1-T03 · accounts / calibration_agent / google_routes

| sub-claim | verdict | evidence |
|---|---|---|
| `accounts.Registry` vs `google_creds.GoogleRegistry` ~90 LOC **verbatim** twin | **REFUTED as verbatim; CONFIRMED as structural** | `diff` of `accounts.py:128-240` vs `google_creds.py:106-210`: same 6-method shape (`__init__`/`load`/`save`/`get`/`default`/`add_or_update`/`remove`) but different bodies — `Registry.load` parses a `playlists` map, `GoogleRegistry.save` does `os.makedirs(..., 0o750)` and a 0640-mode comment about PII. Converge-with-a-generic candidate, **0 LOC deletable as-is**. |
| `compute_schroeder` can never return `available=True` | **CONFIRMED** | `tools.py:227-244` returns `{"available": False}` unless both kwargs are truthy; the only call site is `tools.py:319` `compute_schroeder()` — no arguments. **18 LOC + 3 consumer branches.** |
| `public_dict` no callers | **C-dead** | `calibration_agent/model_client.py:69-75`, sole occurrence. **7 LOC.** |
| `commit_executor` no callers | **C-test** | threaded through `actions.py:34,81,112,248,253,260`; the only supplier is `tests/test_calibration_agent_actions.py:126`. |
| `StationInfo` no callers | **PARTIAL** | the *alias* `from .transit._mta_stations import Station as StationInfo` (`subway.py:69`) has **zero external importers**, but is used in-file at `:324,336`. Rename-in-place, ~1 net LOC. |
| `GOOGLE_ROUTES_SECRET_FILE` no callers | **REFUTED** | `jasper/web/transit_setup.py:104` (`= google_routes.GOOGLE_ROUTES_SECRET_FILE`), consumed at `:1458,1644,1673` and by `jasper/web/__main__.py:382`. Live. |
| 4 calibration_agent env knobs with zero refs | **PARTIAL — 3, not 4** | `JASPER_CALIBRATION_ADVISOR_OPENAI_BASE_URL`, `_PROVIDER`, `_TIMEOUT_SEC` appear only in `model_client.py` + the `tests/test_env_vars_codified.py:196-198` inventory list. `JASPER_CALIBRATION_ADVISOR_MODEL` is **live** (`calibration_agent/cli.py:330`). |

**LOC: ~51** (18 + 7 + ~15 commit_executor path + ~10 knobs + 1 alias).

---

## 15 — p1-T12-2 · quality_model / calibration / null_walk

| sub-claim | verdict | evidence |
|---|---|---|
| `ROOM == DRIVER == RAMP` (three names one object) | **PARTIAL** | value-equal, **not** identity: `ROOM == DRIVER` → `True`, `ROOM is DRIVER` → `False` (three separate `QualityModel()` instances, `quality_model.py:123,128,136`). `DRIVER` re-spells the dataclass defaults. All three have live importers (`DRIVER` ×6 production modules; `ROOM` ×2; `RAMP` only `tests/test_audio_measurement_kernel.py:339` which pins `RAMP == ROOM`). Converge to one name → ~14 LOC; not a deletion. |
| `calibration.supported_model_options()` zero callers | **C-dead. CONFIRMED** | `audio_measurement/calibration.py:70-83`, sole occurrence repo-wide. **14 LOC.** |
| `NullWalkSpec.candidate_delays_us` dead | **C-test. CONFIRMED** | `null_walk.py:313-334`. Production uses `coarse_candidate_delays_us` (`null_walk.py:434,479`, `active_speaker/commissioning_service.py:372`). The only readers of `candidate_delays_us` are `tests/test_audio_measurement_null_walk.py:92,239,242,249` and a docstring pointer at `crossover_v2/delay_landscape.py:372`. **22 LOC.** |

**LOC: 36** confirmed.

---

## 16 — p1-T05 · camilla contract / ring_assets

| sub-claim | verdict | evidence |
|---|---|---|
| `camilla_config_contract.ACTIVE_OUTPUTD_CAPTURE_DEVICE` dead | **C-dead. CONFIRMED** | `camilla_config_contract.py:53` is the **only** occurrence in the tree (all file types). **1 LOC.** |
| `ring_assets.ring_stall_verdict` + `RingStallVerdict` deletable in favour of `ring_flow_state` | **REFUTED** | live production caller: `jasper/cli/doctor/audio_runtime_ring.py:910` (import) and `:923` (`verdict = ring_stall_verdict(path)`), plus the class named in its docstring at `:891`. Also pinned by `tests/test_ring_stall_alarm.py:23`, `test_grouping_ring_platform.py:504-512`, `test_doctor_audio_runtime_fanin.py:627`. Converging it into `ring_flow_state` is a refactor with a real caller to migrate (~171 LOC in play), not a deletion. |

**LOC: 1.**

---

## 17 — p1-T13-1 / p1-T13-2 · crossover_v2 barrels and dead fields

| sub-claim | verdict | evidence |
|---|---|---|
| `crossover_v2/__init__.py` 16 names, zero production consumers | **C-dead. CONFIRMED** | file is 53 LOC; the `from .contracts import (…)` block + `__all__` is `:17-53`. Zero rows match `from …crossover_v2 import <one of the 16>` or `crossover_v2.<name>` in `jasper/` or `scripts/`. Every production importer reaches submodules: `crossover_v2_flow.py:174-183` binds modules (`durable_state`, `planning`, `priors`, `programs`, `spatial`, `verification`, `contracts`), and `:184` imports names from `.contracts` directly. **~37 LOC** (keep a docstring-only `__init__.py`). |
| `frequency_view.build_frequency_view` zero production callers | **C-test. CONFIRMED** | `crossover_v2/frequency_view.py:225-234` is a compat door wrapping `active_speaker/frequency_view.py:140`. Both production consumers import the **parent**: `web/correction_measurements.py:13,98` and `cli/round_views/frequency.py:20,83`. Only `tests/test_crossover_v2_frequency_view.py` uses the door. **10 LOC.** |
| four dead `InterventionProposal` fields | **C-test. CONFIRMED** | `predicted_response_before`, `predicted_spec_before`, `alternative_trim_db`: only `tests/test_crossover_v2_contracts.py:70,72,78,321,323,329`. `anchored_trim_db`: the hits at `intervention.py:1383,1428` are **dict-literal string keys** in a log payload, not reads of the dataclass field — the field itself is unread. **~14 LOC** + a fingerprint-domain bump. |
| `CapabilityStub.captured` always True dead branch | **CONFIRMED** | all three `_StubRow`s pass `captured=True` (`measure_spec.py:105,108,111`); `_ROWS` is the only producer (`_stub` at `:87-93`, `stubbed_capabilities` at `:308`). So `session.py:328` `if any(not stub.captured …)` is unreachable, while `measure_spec.py:312` documents `captured=False` semantics nothing can produce. **~6 LOC** (field + branch). |

**LOC: 67.**

---

## 18 — p1-T16-1 / p1-T16-3 · HAClient, csrf helper, web main()s

| sub-claim | verdict | evidence |
|---|---|---|
| `HAClient.healthcheck` unused | **REFUTED** | called at `jasper/home_assistant.py:800` (inside `_probe_uncached`, the live probe) and `jasper/web/home_assistant_setup.py:423`. |
| `HAClient.config` unused | **REFUTED** | called at `jasper/home_assistant.py:806` in the same live `_probe_uncached`. |
| `HAClient.list_agents` unused | **C-test. CONFIRMED** | `home_assistant.py:541-570`; only reader `tests/test_home_assistant.py:763`. **30 LOC.** |
| `csrf_fetch_helpers_js` dead | **C-dead. CONFIRMED** | `web/_common.py:1176-1199`; the only other mentions are its own module docstring (`:41`), `tests/test_web_common.py:416`, a *negative* assertion `assert "csrf_fetch_helpers_js" not in html` (`test_web_sources_setup.py:67`), and a provenance comment in `deploy/assets/shared/js/http.js:14`. No production call. **24 LOC.** |
| three dead `main()` in web tile 3 | **CONFIRMED** — already counted in claim 7 (`voice_setup` 45, `wifi_setup` 24, `wake_setup` 39) | not double-counted below. |

**LOC: 54** (new).

---

## 19 — p1-T23 · installer libs shipped-but-unused; retired-artifact one-shots

| sub-claim | verdict | evidence |
|---|---|---|
| 11 of 12 `deploy/lib/install/*.sh` shipped to the Pi with no runtime consumer | **CONFIRMED — but 0 deletable LOC** | `deploy/lib/install/systemd-units.sh:47-50` installs the whole glob to `/usr/local/lib/jasper/install/`. The only runtime source of that path is `deploy/bin/jasper-contained-build:15` → `build-sandbox.sh` (pinned by `tests/test_enhanced_aec_systemd.py:58,70`). All 12 files **are** sourced at install time by `deploy/install.sh:50-63` from the repo checkout, so none is dead code — the fix is narrowing the install glob to `build-sandbox.sh` (**1-line change**, ~5,900 lines of dead bytes off every Pi). |
| jasper.env sed-delete table: 8 of 12 keys have zero live refs | **CONFIRMED with one transcription error** | `deploy/lib/install/python-runtime.sh:372-384`. Zero live refs: `JASPER_AIRPLAY_DEVICE_NAME`, `JASPER_CAPTURE_RELAY_BASE`, `JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN`, `JASPER_CONTROL_PORT`, and `JASPER_CAPTURE_ORIGIN` (the script's actual key — the report wrote `JASPER_CAPTURE_RELAY_ORIGIN`, which does not appear in the script). `SPOTIPY_REDIRECT_URI`, `JASPER_AEC_CHIP_AEC_DAC_AUTO`, `_TRIAL` have exactly one residual ref each (inventory/doc, not a producer). |

Note: these sed lines are **fleet migrations**, not dead code — removing them changes behaviour on a box that has never installed past the retirement. The AGENTS.md-shaped fix is a `RETIRED` table with an expiry per row, not a delete. **LOC: 0.**

---

## 20 — p1-T14-4 / p0-orphans · 345 public defs "with no consumer outside their file"

**CONFIRMED as a fact, REFUTED as a deletion.** `p0-orphans.md:88,350` itself labels the row *"Visibility tightening, not deletion"* — the claim's framing ("6,879 LOC of orphans") over-reads its own source. `check_orphans.py` re-ran all 13 top-table files (27 named symbols) across `jasper/ tests/ scripts/ deploy/` incl. `.mjs`/`.js`/`.sh`:

| sampled symbol | refs outside defining file |
|---|---|
| `FrozenReferenceResult`, `ForwardModelDeltaResult`, `AudibilityMetrics` (`crossover_v2/round_views.py`) | 0 |
| `AudioRouteProfile`, `route_config_hash_for_plan`, `RuntimeSetting` (`audio_runtime_plan.py`) | 0 |
| `render_markdown` (`calibration_agent/cli.py`, 229 LOC, called once at `:506`) | 0 |
| `PositionFlatness`, `DirectivityBand`, `DirectivityRow` (`flat_spec_views.py`) | 0 |
| `ring_wire_declarations`, `graph_wire_declarations`, `RingWireDeclaration` (`fanin/ring_health.py`) | 0 |
| `CrossoverLevelRunRequest` (`crossover_level_run.py`) | 0 |
| `enforce_capture_retention`, `driver_analysis_input_evidence` (`web_measurement.py`) | 0 |
| `window_bias_db`, `fit_notch` (`crossover_v2/gate_sweep.py`) | 0 |
| `PhysicalOutput`, `TopologyRouting`, `SpeakerPosition` (`output_topology.py`) | 0 |
| `ActiveCaptureAdmissionHandoff` (`commissioning_admission.py`) | 0 |
| `SealedAdapter` (`bass_extension/adapters/sealed.py`) | 0 — instantiated in-file at `:256` as `SEALED_ADAPTER`, which *is* the live registry entry |
| `find_bluetooth_conflicts`, `find_mdns_conflicts` (`speaker_name_discovery.py`) | 0 |
| `refused_from_flow_error`, `CaptureEvidenceCarry` (`web/correction_crossover_v2.py`) | 0 |

**All 27 are used inside their own file.** "Private to its file" means **under-scoped, not deletable** — the correct change is a leading underscore (plus a `__all__` trim), which removes 0 lines. Do not let this row inflate a deletion total.

Three genuinely-dead members of the adjacent §2a list *do* verify:
- `ui_level_meter` (`crossover_v2/sweep_spec.py:296-297`) — 0 refs anywhere; the only mention is a comment at `:1098` claiming "the BUILDER stays". **2 LOC.**
- `default_spotify_redirect_uri` (`spotify_oauth.py:20`) / `default_google_redirect_uri` (`google_oauth.py:20`) — refs only in `tests/test_oauth_redirect.py`. **~16 LOC.**

**LOC: 18.**

---

## Totals

### Confirmed removable — product

| claim | LOC | note |
|---|---:|---|
| 1 peering dead half | 373 | |
| 2 bass_extension | **0** | ADR-0018 park; 3,997 if a fresh ADR lifts it |
| 3 crossover_v2_flow barrel | 14 | +87 after redirecting tests |
| 4 active_speaker `_LAZY_ATTRS` | 143 | |
| 5 program_analysis privates | 46 | conditional on test redirect |
| 6 audio_hardware `__init__` | 80 | |
| 7 nine wizard `main()`s | 334 | incl. 2 pyproject lines |
| 8 session level-match + refusal copy | 288 | `level_match.py` itself stays |
| 9 audio-lab aplay backend | 330 | |
| 10 XVF CLEAR_CONFIGURATION | 1 | |
| 11 bt_roles + volume + fan-in AUTO | 131 | +~15 Rust |
| 12 wake-corpus / tts_routing / tools.audio / facade | 87 | +74 if `wake_fusion` is inlined |
| 13 rust ring/resampler/host-clock symbols | 233 | crate itself stays |
| 14 calibration_agent / subway | 51 | |
| 15 quality_model / calibration / null_walk | 36 | |
| 16 camilla contract constant | 1 | |
| 17 crossover_v2 barrels + dead fields | 67 | |
| 18 HAClient.list_agents + csrf helper | 54 | |
| 19 installer libs / sed one-shots | 0 | 1-line install-glob fix |
| 20 file-local public surface | 18 | the 345-def / 6,879-LOC row is 0 |
| **TOTAL (product)** | **≈ 2,287** | |

Conditional additions: **+87** (barrel, after test redirect), **+74** (`wake_fusion` inline), **+3,997** (only if ADR-0018 is superseded).

### Confirmed removable — test

| claim | LOC |
|---|---:|
| 1 peering (`test_peering_discovery.py` 405 + ~80) | 485 |
| 7 console-script asserts | 8 |
| 8 level-match refusal + session pins | 180 |
| 9 `test_active_speaker_playback.py` (of 1,231) | ~600 |
| 11 bt_roles / volume / mux pins | 120 |
| 4/5/17/18/20 barrel + field + helper pins | ~120 |
| 13 rust `#[cfg(test)]` blocks | ~120 |
| **TOTAL (test)** | **≈ 1,633** |
| bass_extension test corpus if the park lifts | +3,915 |

---

## REFUTED items — drop these from the synthesis

| claim | refuted assertion | the caller |
|---|---|---|
| 2 | executor / stimulus / live_proof / cross_check are "zero-importer" | `tests/test_bass_extension_bench_{executor,stimulus,live_proof,cross_check}.py`; only `bench/excitation.py` is truly zero-importer |
| 2 | `bass_extension/__init__.py` (730) is deletable | `BASS_EXTENSION_APPLY_INTENT_PATH` ← `commissioning_apply.py:911`, `runtime_contract.py:4378,4666`, `dsp_apply.py:571`, `correction_setup.py:1359`, `follower_config.py:523` |
| 7 | *every* page is served by `python -m jasper.web` | `jasper-bluetooth-web` / `-chat-web` / `-correction-web` / `-system-web` have real `ExecStart=` |
| 7 | 10th dead `main()` = `wake_corpus_setup` | it is the documented operator CLI (`wake_corpus_setup.py:43,48`; `scripts/_audit_wake_corpus.py:7`) |
| 8 | `correction/level_match.py` (899) is dead | `web/correction_crossover_backend.py:60,154,441,570`; `correction/session.py:60-61,340` |
| 13 | dll fields are "pinned by a Python contract test" | no such test — `grep "dll_" tests/*.py` returns one unrelated comment |
| 14 | accounts/google_creds are a "~90 LOC verbatim twin" | bodies differ (playlist parsing, `makedirs`, 0640 PII mode) — converge, don't delete |
| 14 | `GOOGLE_ROUTES_SECRET_FILE` has no callers | `web/transit_setup.py:104,1458,1644,1673`; `web/__main__.py:382` |
| 14 | 4 dead calibration_agent knobs | 3 — `JASPER_CALIBRATION_ADVISOR_MODEL` is live at `calibration_agent/cli.py:330` |
| 15 | ROOM/DRIVER/RAMP are "three names one object" | three distinct instances, value-equal; all three have importers |
| 16 | `ring_stall_verdict`/`RingStallVerdict` deletable | `cli/doctor/audio_runtime_ring.py:910,923` |
| 18 | `HAClient.healthcheck` unused | `home_assistant.py:800`, `web/home_assistant_setup.py:423` |
| 18 | `HAClient.config` unused | `home_assistant.py:806` |
| 19 | 11 installer libs are dead code | all 12 are sourced by `deploy/install.sh:50-63`; only the *Pi-side copy* is unused |
| 20 | 345 public defs / 6,879 LOC are orphans | all 27 sampled are used inside their own file — visibility fix, 0 LOC removed |
