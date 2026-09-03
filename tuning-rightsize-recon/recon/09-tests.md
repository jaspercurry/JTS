# 09 — Tests for the tuning scope

Read-only recon. Scope = test files under `tests/` that import
`jasper.active_speaker`, `jasper.audio_measurement`, `jasper.correction`,
`jasper.attribution`, `jasper.calibration_agent`, `jasper.web.correction_*`
/ `balance_*` / `active_speaker_flow`, the 25 tuning CLIs, or
`experiments/usb-turntable`.

Scripts used (reproducible, all in
`scratchpad/recon/`): `scope.py`, `tbl.py`, `ratio.py`, `sym.py`,
`clusters.py`, `clusters2.py`. Counts below cite the command that produced
them.

**Environment note:** I could not run pytest. The repo venv is absent;
`pip install numpy scipy pytest` got collection as far as a
`pyo3_runtime.PanicException` from a pydantic-core wheel mismatch. All
runtime claims below are static estimates and are labelled as such.

---

## 0. Headline

| metric | value |
|---|---|
| test files in scope | **358** |
| test lines in scope | **356,663** |
| test functions | **10,765** |
| parametrized test functions | **1,138 (10.6 %)** |
| `assert` statements | 36,951 (**3.4 per test**) |
| avg lines per test function | **33** |
| docstring lines | **53,599 (15.0 %)** |
| comment lines | **24,843 (7.0 %)** |
| blank lines | 55,508 (15.6 %) |
| **prose (doc+comment)** | **78,442 = 22 % of the corpus** |
| production lines in scope | 262,227 (307 files) |
| **overall test : prod ratio** | **1.34 : 1** |

Issue/ruling citations inside the tests: **2,318** `#NNNN`, **109** `ADR-`,
**336** "ruling", **1,213** dates, **868** "owner". The test corpus is
carrying the project's decision history in its docstrings — the same defect
AGENTS.md removed from the doctrine file.

**The corpus's problem is not gaps, it is excess.** Only three modules in
the whole 262 k-line scope have no test at all (§2b). Everything else is
tested two to five times over, at three altitudes, mostly one finding per
test function, mostly unparametrized, mostly narrated.

---

## 1. Size table — top 40 test files

`param` = test functions carrying `@pytest.mark.parametrize`; `doc` =
docstring lines in the file; `help` = lines in module-level non-test
helpers/classes. Subject = the in-scope production module from which the
file imports the most symbols; `prodL` its size.
(`scratchpad/recon/tbl.py`)

| testL | tests | param | doc | help | test file (`tests/`) | subject (prodL) |
|---:|---:|---:|---:|---:|---|---|
| 12,176 | 326 | 16 | 2,705 | 282 | test_crossover_v2_conductor.py | crossover_v2_flow.py (7,840) |
| 9,310 | 189 | 15 | 1,690 | 1,319 | test_correction_crossover_v2_endpoints.py | crossover_v2_flow.py / web.correction_crossover_v2 |
| 8,082 | 187 | 19 | 1,733 | 938 | test_audio_measurement_program_analysis.py | audio_measurement/program_analysis.py (6,573) |
| 7,727 | 154 | 23 | 385 | 1,374 | test_sound_setup.py | active_speaker facade + web/sound_setup |
| 6,954 | 121 | 5 | 462 | 897 | test_active_speaker_baseline_profile.py | baseline_profile.py (4,193) |
| 6,629 | 186 | 21 | 374 | 144 | test_correction_setup.py | web/correction_setup.py (7,481) |
| 6,217 | 249 | 29 | 1,435 | 344 | test_crossover_envelope_v2.py | crossover_envelope_v2.py (4,418) |
| 5,484 | 171 | 31 | 340 | 627 | test_active_speaker_runtime_contract.py | runtime_contract.py (5,530) |
| 5,175 | 120 | 12 | 1,100 | 1,182 | test_active_speaker_seat_level.py | seat_level_ramp.py (2,246) |
| 4,833 | 91 | 11 | 1,455 | 412 | test_spatial_combine.py | spatial_combine.py (2,229) |
| 4,655 | 161 | 27 | 1,182 | 288 | test_crossover_v2_driver_prescription.py | driver_prescription.py (2,509) |
| 4,290 | 122 | 6 | 332 | 644 | test_audio_health.py | runtime_contract.py |
| 4,205 | 82 | 5 | 948 | 1,039 | test_ring_active_endpoint.py | runtime_contract.py |
| 4,005 | 76 | 6 | 538 | 500 | test_active_speaker_driver_safety.py | driver_safety.py (3,259) |
| 3,960 | 132 | 8 | 1,025 | 327 | test_active_speaker_linearization_fit.py | linearization_fit.py (3,568) |
| 3,889 | 156 | 10 | 332 | 330 | test_multiroom_reconcile.py | camilla_yaml.py (4,552) |
| 3,288 | 72 | 8 | 813 | 243 | test_crossover_v2_round_wiring.py | crossover_v2/verification.py (2,187) |
| 3,237 | 56 | 15 | 138 | 237 | test_active_speaker_web_commissioning.py | web_commissioning.py (2,917) |
| 3,209 | 66 | 22 | 248 | 649 | test_audio_hardware_reconcile.py | active_speaker facade |
| 3,153 | 128 | 24 | 674 | 372 | test_crossover_v2_blend_prescription.py | blend_prescription.py (1,689) |
| 3,001 | 119 | 7 | 331 | 166 | test_doctor_correction.py | runtime_contract.py |
| 2,700 | 87 | 4 | 104 | 390 | test_active_speaker_commissioning_capture.py | commissioning_capture.py (1,521) |
| 2,587 | 109 | 26 | 468 | 252 | test_crossover_v2_verification.py | crossover_v2/verification.py (2,187) |
| 2,584 | 55 | 6 | 91 | 96 | test_correction_status_and_bundles.py | correction/session.py (2,882) |
| 2,569 | 75 | 4 | 661 | 298 | test_crossover_v2_prescription_spool.py | prescription_spool.py (953) |
| 2,543 | 110 | 8 | 639 | 106 | test_active_speaker_delta_probe.py | delta_probe.py (2,495) |
| 2,412 | 40 | 16 | 85 | 134 | test_voice_daemon_measurement_inflight.py | correction/coordinator.py (988) |
| 2,389 | 41 | 5 | 564 | 472 | test_crossover_v2_stage_bridge.py | active_speaker facade |
| 2,296 | 83 | 13 | 566 | 482 | test_renderer_ring_lanes.py | correction_lane.py (370) |
| 2,231 | 73 | 6 | 251 | 280 | test_correction_crossover_v2_wired.py | crossover_v2/capture_source.py (197) |
| **2,193** | **0** | 0 | 414 | 1,600 | **crossover_v2_fixtures.py** (shared, 39 importers) | — |
| 2,120 | 77 | 4 | 398 | 260 | test_active_speaker_crossover_v2_round_views.py | round_views.py (1,811) |
| 2,102 | 42 | 6 | 167 | 253 | test_active_speaker_setup_status.py | active_speaker/measurement.py (1,793) |
| 2,098 | 50 | 4 | 128 | 86 | test_correction_session.py | correction/session.py (2,882) |
| 2,088 | 77 | 6 | 272 | 30 | test_audio_runtime_plan.py | active_speaker facade |
| 2,064 | 38 | 3 | 38 | 339 | test_sound_setup_commission.py | active_speaker facade |
| 2,038 | 76 | 3 | 467 | 161 | test_crossover_v2_remote_tier.py | crossover_v2_flow.py |
| 2,037 | 50 | 3 | 583 | 97 | test_interference_nulls.py | interference_nulls.py (1,901) |
| 1,953 | 68 | 5 | 408 | 151 | test_active_speaker_linearization_envelope.py | linearization_envelope.py (861) |
| 1,948 | 61 | 1 | 138 | 0 | test_control_server_system.py | runtime_contract.py |

Read the `param` column against `tests`: the two largest files run 326 and
189 test functions on 16 and 15 parametrized families respectively. The
`help` column is per-file scaffolding that duplicates
`tests/crossover_v2_fixtures.py` — the top 40 files carry **~18,000 lines**
of it.

### 1b. Package ratios

(`scratchpad/recon/ratio.py`, attributing each test file to the package it
imports most.)

| package | prod L | test L | files | ratio |
|---|---:|---:|---:|---:|
| jasper/active_speaker (top level) | 103,287 | 210,942 | 181 | 2.04 |
| jasper/audio_measurement | 31,981 | 56,052 | 60 | 1.75 |
| jasper/correction | 16,914 | 22,992 | 26 | 1.36 |
| jasper/active_speaker/crossover_v2 | 62,456 | 55,165 | 65 | 0.88 |
| jasper/calibration_agent | 4,646 | 4,359 | 12 | 0.94 |
| jasper/attribution | 2,696 | 2,587 | 2 | 0.96 |
| jasper/web (correction_*) | 23,705 | (tested only through active_speaker files) | | |
| **total** | **262,227** | **352,483** | **348** | **1.34** |

Caveat: `jasper/active_speaker/__init__.py` is a 480-line re-export facade,
so "active_speaker (top)" absorbs test files whose real subject is a
submodule. Treat the package rows as directional and the per-file table
above as the precise one.

### 2. Ten most over-tested subjects

Ratio = lines of the test file(s) whose *primary* import is that module,
over the module's own lines. (`scratchpad/recon/tbl.py` `subject` column.)

| ratio | prodL | testL | subject |
|---:|---:|---:|---|
| 12.4× | 197 | 2,231+198 | `crossover_v2/capture_source.py` — **and #3724 deletes it** |
| 6.2× | 370 | 2,296 | `audio_measurement/correction_lane.py` |
| 5.0× | 395 | 1,947 | `active_speaker/seat_level_reference.py` |
| 3.9× | 1,579 | 6,217 | `crossover_v2/refusal_copy.py` (via test_crossover_envelope_v2) |
| 2.7× | 953 | 2,569 | `crossover_v2/prescription_spool.py` |
| 2.5× | 988 | 2,412 | `correction/coordinator.py` |
| 2.4× | 1,689 | 3,153+1,271 | `crossover_v2/blend_prescription.py` |
| 2.3× | 2,246 | 5,175+1,164 | `active_speaker/seat_level_ramp.py` |
| 2.3× | 1,811 | 2,120+324 | `crossover_v2/round_views.py` |
| 2.2× | 2,229 | 4,833 | `audio_measurement/spatial_combine.py` |
| 2.2× | 2,187 | 3,288+2,587 | `crossover_v2/verification.py` (**two** test files) |
| 1.9× | 2,509 | 4,655 | `crossover_v2/driver_prescription.py` |

### 2b. Subjects with no tests

Almost nothing. Symbol-level scan (`scratchpad/recon/sym.py`: does any test
file mention any public top-level symbol of the module?) found 19
candidates; 16 are false positives — modules whose top level is entirely
private helpers reached indirectly (e.g. `crossover_v2/diagnostics.py`,
700 L of `_log_*` emitters, exercised through `caplog` in 27 files).

Genuinely untested:

| prodL | module | note |
|---:|---|---|
| 131 | `jasper/web/balance_level.py` | 0 test files, 1 production reference |
| 291 | `jasper/cli/audition.py` | 0 test files |
| 273 | `jasper/cli/correction_bundle.py` | 0 test files |

Thin (≤0.35× and a single test file): `web_measurement.py` (1,383 L / 157),
`crossover_v2/evidence_packet.py` (3,754 / 586), `refusal_copy.py`
(1,579 / 254), `olive_metrics.py` (388 / 100), `correction/strategy.py`
(925 / 291), `calibration_agent/model_client.py` (504 / 165).

---

## 3. Smell classification

### (a) Source-text pins — 190 tests / 5,851 lines / 107 files

AST scan for a test function whose body calls `inspect.getsource`,
`Path.read_text` or `rglob` against a `.py`/`.js`/`.rs`/`.md` path.
`inspect.getsource` alone: **49 calls in 31 files**.

Worst concentrations: `test_outputd_wiring.py` (22 tests),
`test_crossover_envelope_v2.py` (8), `test_web_wizard_conventions.py` (6),
`test_renderer_ring_lanes.py` (5), `test_correction_crossover_v2_endpoints.py`
(7 `getsource` calls).

Example — `tests/test_correction_crossover_v2_endpoints.py:2848`:

```python
def test_the_apply_endpoint_cannot_skip_the_preflight():
    source = inspect.getsource(correction_setup._handle_crossover_v2_apply)
    assert "status=correction_crossover_backend.status_payload()" in source
```

This asserts a *keyword-argument spelling* at a call site. Renaming the
helper breaks it; deleting the behaviour while keeping the string does not.
`tests/test_correction_crossover_v2_endpoints.py:2643` even explains in
prose why a source read was chosen ("the disclosure fails CLOSED, so
dropping this call does not break loudly") — the honest fix is an
observable: make the disclosure fail loudly, then assert the output.

**Legitimate exceptions to keep:** `tests/test_audio_safety_pins.py` (314 L,
7 tests) parses every checked-in `deploy/camilladsp/*.yml` for
`volume_limit ≤ 0.0` and greps the Rust crates for the shared TTS-gain
literals — a non-negotiable cross-language pin with no other mechanism.
Same reasoning covers the Rust-literal subset of
`tests/test_outputd_wiring.py`. That is ~30 of the 190.

**Move:** delete or convert ~160 tests / ~4,000 lines. Risk low — each is
provably redundant with a behaviour test in the same file or fails to pin
behaviour at all. Proof: the file's remaining tests still pass.

### (b) Prose pins — 805 `match=` strings + 128 free-prose log asserts

`pytest.raises(..., match=...)`: **1,019 occurrences**; **805** of the match
strings contain a space, i.e. they pin English, not a code. Top repeated
strings: `"disk full"` (×8), `"out of range"` (×5), `"response lost"` (×5),
`"WHOLE degrees"` (×5), `"no wired measurement"` (×5), `"already in
progress"` (×4), `"fingerprint does not match"` (×4). Concentrations:
`test_active_speaker_commissioning_receipt.py` (49),
`_commissioning_evidence.py` (37), `_commissioning_run.py` (32),
`test_correction_setup.py` (29).

`caplog` is mostly clean: of 517 caplog string assertions, **389 pin
`key=value` structured fields** (`event=correction.crossover_v2_*`,
`reason=pipeline_failed`) and only **128** pin free prose. Keep the
structured ones — they are exactly what AGENTS.md asks for.

**Move:** the ~110 error classes noted in the brief already carry codes;
assert `exc.value.code` / the exception type instead of the sentence.
Line-neutral, brittleness removed. Risk low. Proof: rename any refusal
sentence and the suite should stay green.

### (c) Example clusters that should be one parametrized family

80 unparametrized name-prefix clusters of ≥5 sibling tests:
**507 tests / 10,611 lines** (`scratchpad/recon/clusters2.py`). Ten worth
naming:

| tests | lines | file / family |
|---:|---:|---|
| 21 | 1,235 | `test_active_speaker_baseline_profile.py::test_apply_baseline_profile_*` |
| 18 | 607 | `test_active_speaker_staging.py::test_stage_protected_startup_*` |
| 11 | 393 | `test_sound_setup.py::test_sound_output_topology_*` |
| 11 | 386 | `test_multiroom_reconcile.py::test_main_active_leader_*` |
| 12 | 24 | `test_doctor_correction.py::test_*_registered_in_sync_checks` (2 lines each — one parametrized registry test) |
| 9 | 229 | `test_audio_measurement_program_analysis.py::test_select_alignment_pair_*` |
| 8 | 99 | `test_active_speaker_runtime_contract.py::test_baseline_*_is_blocked` (**8 byte-identical shapes**) |
| 8 | 195 | `test_doctor_correction.py::test_active_speaker_runtime_*` |
| 7 | 254 | `test_crossover_v2_conductor.py::test_measure_diag_logs_*` |
| 7 | 222 | `test_crossover_envelope_v2.py::test_the_browser_and_python_agree_on_*` |
| 6 | 153 | `test_audio_measurement_snr_policy.py::test_band_levels_dbfs_*` |
| 4 | 24 | `test_active_speaker_measured_crossover_candidate.py::test_from_mapping_rejects_*` (**4 byte-identical**) |

Structural clustering (normalised AST, literals erased) found only 4
byte-identical groups (28 tests) — the families differ in the *value* they
feed, which is precisely the parametrize case.

A second, larger family runs across files: the six `*_prescription*` test
files (`alignment` 1,890, `blend` 3,152, `driver` 4,654, `topology` 1,265,
`spool` 2,568, `prescriber_status` 1,053 = **14,582 lines**) test four
production modules that share one document/refusal/round-trip contract
(751+1,688+2,508+964+953 = 6,864 lines). One parametrized contract suite
over the four prescription kinds plus per-kind maths files is the shape.

### (d) Tests for dead or about-to-be-dead subjects

- **`crossover_v2/capture_source.py` (197 L)** — deleted by in-flight
  **#3724**. Tests riding on it: `test_crossover_v2_capture_source.py`
  (198 L, whole file), `test_correction_crossover_v2_wired.py` (2,230 L,
  primary subject), plus per-source forks inside 9 more files including
  `test_correction_crossover_v2_endpoints.py`,
  `test_correction_setup.py`, `test_crossover_v2_remote_tier.py` (2,037 L),
  `test_crossover_v2_stage_bridge.py`, `test_angle_capture_take.py`
  (1,174 L). **Do not touch these — #3724 and its stacked follow-ups own
  them.** Note the overlap and let that PR land first.
- **`tests/engine_twin.py` (392) + `tests/test_engine_twin.py` (381) +
  `tests/engine_declarations.py` (93) = 866 lines** of strangler
  scaffolding. Its own docstring says it is *"a demonstration and not a
  port"* — it walks a twin through "use classes" a census found, and pins
  nothing about the product. It has a natural expiry (the wave-2 engine
  body); nothing has removed it. Delete when the strangler lands.
- **Incident-replay corpus — 5 files / 3,769 lines + 240 KB of fixtures.**
  `test_crossover_v2_commanded_axis_incident_replay.py` (1,684),
  `test_crossover_v2_incident_replay.py` (896),
  `test_crossover_v2_alignment_incident_replay.py` (534),
  `test_crossover_v2_d1_incident_replay.py` (436),
  `test_derive_crossover_incident_fixture.py` (220), against
  `tests/fixtures/crossover_v2_*_incident_2026*/`. Each began as a
  *characterization* test of behaviour that was wrong, then had its
  assertions flipped and the original narrative kept. AGENTS.md: "a bug fix
  gets one behavior pin, not a new test file."
### (d2) Production code kept alive only by its tests — 23 symbols / 1,224 lines

A stricter scan (`scratchpad/recon/testonly.py`): a top-level symbol in the
scope whose name appears **nowhere in `jasper/`, `deploy/`, `scripts/`,
`rust/`, `c/` or `experiments/` outside its own definition**, but is
referenced from `tests/`. Verified by grep per symbol; no `getattr`,
registry string, or `pyproject.toml` entry point hides a caller.

| prodL | symbol | test refs |
|---:|---|---:|
| 362 | `active_speaker/commissioning_isolated_producer.py::promote_isolated_driver_capture` | 2 |
| 215 | `active_speaker/web_commissioning.py::_load_applied_summed_measurement_config` | 8 |
| 196 | `active_speaker/commissioning_apply.py::restore_pending_candidate_apply` | 3 |
| 97 | `active_speaker/commissioning_runtime.py::_normal_graph` | 9 |
| 73 | `active_speaker/commissioning_runtime.py::_topology_binding` | 10 |
| 61 | `active_speaker/web_commissioning.py::prepare_automatic_driver_level_match` | 2 |
| 35 | `active_speaker/repeat_admission.py::reservation_is_finished` | 2 |
| 22 | `web/correction_setup.py::_begin_relay_commit` | 3 |
| 20 | `web/correction_setup.py::_begin_relay_finishing` | 2 |
| 18 | `active_speaker/measured_crossover_candidate.py::build_and_prove_candidate_config` | 7 |
| 17 | `active_speaker/commissioning_runtime.py::_stationary_candidate` | 2 |
| 16 | `active_speaker/web_commissioning.py::restore_automatic_driver_level_match` | 2 |
| 16 | `audio_measurement/excitation_artifacts.py::refuse_historical_evidence` | 3 |
| 15 | `active_speaker/repeat_admission.py::failure_status` | 6 |
| — | 9 more, ≤14 L each | |

Concentrated in `commissioning_runtime.py` (5 symbols), `web_commissioning.py`
(3), `repeat_admission.py` (2), `web/correction_setup.py` (2, both
relay-shaped — **#3724's territory**). The 362-line
`promote_isolated_driver_capture` is the subject of the 442-line, one-test
file flagged above: a dead production path and a test file existing only to
exercise it.

**Move:** hand this list to the production-scope agents — the *production*
symbol is the deletion, the test goes with it. ~1,224 production lines and
~1,000 test lines. Risk low; the grep above is the proof, and each removal
should re-run it. Caveat: a caller assembled from string fragments would
evade this scan; I checked the six largest by hand and found none.

- **`tests/test_active_speaker_commissioning_isolated_producer.py`** —
  442 lines, **one test function**, and it imports two helpers from *other
  test modules* (`from tests.test_active_speaker_commissioning_admission
  import _context`, `from tests.test_active_speaker_profile import
  _two_way_preset`).

### (e) Mock density

**2,697 `monkeypatch.setattr`** calls in scope; 128 `class _Fake/_Stub/…`
definitions; only 66 uses of `unittest.mock`. Highest per-test density
(files ≥20 tests):

| setattr/test | file |
|---:|---|
| 3.5 | `test_active_speaker_web_commissioning.py` (197 patches, 56 tests) |
| 2.6 | `test_multiroom_follower_config.py` |
| 2.2 | `test_correction_coordinator.py` |
| 1.8 | `test_web_sync_flow.py` |
| 1.7 | `test_web_correction_tuning.py` |

Related: tests reach **688 distinct private production symbols**, 5,751
references. `test_correction_setup.py` touches 69 private symbols,
`test_audio_measurement_program_analysis.py` 54,
`test_correction_crossover_v2_endpoints.py` 44,
`test_crossover_v2_conductor.py` 43. When a test patches three private
seams and then asserts on a fourth private helper's return, it is pinning
the current decomposition, not the behaviour — and it is why the tuning
code cannot be refactored without a 300-test rewrite.

### (f) Duplicated fixtures / fakes

Shared modules already exist and are used: `crossover_v2_fixtures.py`
(2,192 L, 39 importers), `active_speaker_fixtures.py` (428 L, 58),
`correction_bundle_fixtures.py` (370 L, 8),
`correction_session_fixtures.py` (91 L, 10). The duplication is *beside*
them.

Duplicated **builders** (module-level helpers with the same name in ≥4
files): 46 names, **4,564 lines**. Worst: `_topology` ×21 files,
`_preset` ×18, `_run` ×11, `_candidate` ×10, `_context` ×9, `_capture` ×8,
`_session` ×7, `_bundle` ×6, `_stereo_topology` ×6 (279 L),
`_profile_and_targets` ×4 (322 L).

Duplicated **doubles**: `FakeCamilla` ×4 + `_FakeCamilla` ×4 (152 L across
8 files) — two names for one fake, and `tests/_fake_camilladsp.py` (239 L)
already exists and is imported by only 4 files. `_FakeOpener` ×3,
`FakeClock` ×4, `_Fader` ×3, `_FakeResponse` ×4.

Duplicated **fixtures**: `_isolated_state` ×3, `_saved_passive_layout` ×4,
`_isolated_v2_state` ×3, `_isolated_spool` ×3, `_stub_audio_hardware_
reconcile` ×3.

And the structural version of the same problem: **117 test files import
helpers from 258 other test modules.** `tests/test_active_speaker_runtime_
contract.py` is imported 85 times and `tests/test_active_speaker_profile.py`
41 times — two test files serving as de-facto fixture modules while
`tests/active_speaker_fixtures.py` sits at 428 lines.

Cross-file duplicate test *names*: 38 names, 79 definitions, 1,248 lines
(e.g. `test_a_mangled_durable_block_reads_as_absent_never_as_half_a_
prescription` ×4 across the prescription files — the parametrize case
again).

### (g) Docstrings longer than the test

58 % of tests carry a docstring; **53,599 docstring lines** in total.
**747 tests have a docstring ≥4 lines that is longer than their body**
(8,433 docstring lines). Concentrated in
`test_crossover_envelope_v2.py` (24), `test_crossover_v2_alignment_
prescription.py` (23), `test_crossover_v2_conductor.py` (21),
`test_audio_measurement_program_analysis.py` (19).

The archetype — `tests/test_crossover_v2_stage_bridge.py:634`,
**91 docstring lines guarding an 18-line assertion**:

```
"""The write side of the bridge: ``verify_priors`` has FOURTEEN keys.
 ...
 **Deliberate widening (#2291 Phase 3a): ``commanded_delta``.** ...
 **Deliberate widening (#2291 Phase 3c): ``entry_baseline``.** ...
 **Deliberate widening (#2392): ``proposal_fingerprint``.** ...
 [ten more paragraphs, one per issue ]
"""
    _conductor, state = _stage_1(monkeypatch)
    assert set(state["verify_priors"]) == { ...fourteen strings... }
```

One paragraph was appended per issue and none was ever removed. Every fact
in it is either in git history or derivable from the assertion. Same shape
at `test_active_speaker_branch_chain.py` (71/64),
`test_flat_spec_ssot.py` (55/54),
`test_crossover_v2_driver_prescription.py` (55/15),
`test_spatial_combine.py` (51/29).

---

## 4. Non-negotiable coverage — keep these heavy

| non-negotiable | tests that protect it |
|---|---|
| `volume_limit: 0.0` in **every** shipped CamillaDSP YAML | `tests/test_audio_safety_pins.py::test_every_static_camilladsp_config_caps_volume_at_zero_db`, `::test_static_camilla_volume_limit_pin_rejects_ambiguous_ownership` (314 L file — the only mechanism covering checked-in `deploy/camilladsp/*.yml`) |
| the doctor's live check of the same | `tests/test_doctor_correction.py::test_check_camilla_volume_limit_{ok,fails_when_missing,fails_when_positive,fails_when_ownership_is_ambiguous,registered_in_sync_checks}` |
| emitter-side limit + limiter/headroom constants | `tests/test_camilla_config_contract.py` (754), `tests/test_dsp_apply.py` (1,230), `tests/test_sound_camilla_yaml.py` (1,418), `tests/test_active_speaker_runtime_contract.py` (5,483, 47 limiter refs) |
| `CamillaController.set_volume_db` positive clamp | `tests/test_camilla_controller.py` (1,240), `tests/test_volume_coordinator.py` (2,654) |
| TTS gain floor / no ceiling, Python↔Rust parity | `tests/test_audio_safety_pins.py::test_fixed_tts_gain_ceiling_is_removed`, `::test_rust_daemon_loudness_modules_reexport_shared_tts_gain_policy`, `::test_assistant_gain_floor_matches_rust_and_doctor` |
| commissioning SPL / level stop | `tests/test_active_speaker_seat_level.py::test_a_measured_level_over_the_commissioning_ceiling_aborts`, `::test_a_mic_that_is_not_observing_aborts_the_climb`, `::test_a_clipped_capture_aborts_rather_than_reading_a_level`, `::test_cancelling_mid_climb_stops_the_tone_restores_and_banks_nothing`, `::test_a_clipped_capture_during_the_fade_stops_the_pass_too`, `::test_a_stop_on_the_way_back_up_leaves_the_stimulus_off`; `tests/test_seat_level_anchor.py::test_the_ceiling_comes_from_the_presets_own_declaration`; `tests/test_active_speaker_safety_envelope_ssot.py` (232) |
| declared per-driver bands / driver protection | `tests/test_active_speaker_driver_safety.py` (4,004), `tests/test_active_speaker_graph_safety.py` (844), `tests/test_active_speaker_driver_low_limit.py`, `tests/fixtures/active_speaker_protection_floor_20260814/` |
| excitation ledger + safety plan | `tests/test_active_speaker_excitation_safety_plan.py` (871), `tests/test_audio_measurement_excitation_admission.py` (459), `tests/test_audio_measurement_excitation_artifacts.py` (1,082), `tests/test_capture_frame_ledger.py` (560), `tests/test_audio_measurement_admitted_playback.py` (1,208) |
| output limiter rail (bass extension) | `tests/test_bass_extension_limiter_evidence.py` (826), `_bench_derivation.py` (479), `_bench_executor.py` (1,372) |
| XVF `SAVE_CONFIGURATION` ban | `tests/test_xvf_host.py:25`, `tests/test_aec_probe_xvf_ref_level_script.py:42` — **both assert the string appears in a refusal/denylist**. Thin, and PR **#3748** moves the fader helpers into `camilla.py`. Flag for that PR's author: the ban needs a positive pin at the new location. |

**~13,000 lines across 15 files.** These stay production-grade and are
explicitly out of the right-sizing budget below. Note that
`test_active_speaker_seat_level.py` (5,175 L) and
`test_active_speaker_driver_safety.py` (4,004 L) are *both* over-tested and
non-negotiable — trim their prose and parametrize their value families, but
do not reduce the number of distinct hazards they exercise.

---

## 5. Runtime

**There is no marker/lane split.** `[tool.pytest.ini_options]`
(`pyproject.toml:289-307`) declares `testpaths`, `python_files`,
`asyncio_mode = "auto"` and a 300 s hang backstop — **no `markers` table**.
Marker usage across the whole `tests/` tree:
`parametrize` 1,783, `skipif` 50, `usefixtures` 11, `timeout` 1, `skip` 1,
`asyncio` 1. Nothing marks slow, hardware, or integration.

The split is by *selection*, not marker: `scripts/test-fast` runs a CI
routing-policy set plus tests selected from `git diff` against
`origin/main`; `scripts/test-merge` runs mypy plus the whole
hardware-free suite. So every one of the 10,765 tuning tests is on the
merge path, and adding a test file adds unconditional merge cost.

Static slowness ranking (no marker exists to check, and I could not run
pytest — score = 3×sleeps + dsp calls + 2×subprocess + 2×http + 2×threads):

| score | file | signals |
|---:|---|---|
| 84 | `test_voice_daemon_measurement_inflight.py` | **28 real sleeps** |
| 63 | `test_audio_measurement_program_analysis.py` | 63 FFT/scipy calls, 8,082 L |
| 47 | `test_active_speaker_web_commissioning.py` | 9 sleeps + 10 subprocess |
| 45 | `test_correction_autolevel.py` | 15 sleeps in 701 L |
| 43 | `test_correction_session.py` | 12 sleeps + 7 dsp |
| 43 | `test_correction_setup.py` | 8 HTTP servers + 4 threads |
| 37 | `test_sound_setup.py` | 7 HTTP + 6 threads |
| 29 | `test_spatial_combine.py` | 29 FFT calls |

Corpus totals: **149 real `sleep` calls**, 241 FFT/scipy calls, 35 HTTP
server spin-ups, 54 threads. The sleeps are the cheapest win — each is
wall-clock time paid on every merge. Recommend a real `slow` marker and a
`-m "not slow"` iterate lane once the corpus is right-sized, *not* before
(a marker on 10,765 tests is machinery without a shape).

---

## 6. Target — what a right-sized file looks like

For the ten largest subjects. "Delete" always means the behaviour survives
in a parametrized or higher-altitude pin; never a coverage reduction on the
§4 list.

| # | subject / file | now | behaviours to pin | families to parametrize | delete | target | Δ |
|---|---|---:|---|---|---|---:|---:|
| 1 | `crossover_v2_flow.py` → `test_crossover_v2_conductor.py` | 12,176 L / 326 t | the CHECK→MEASURE→APPLY→VERIFY walk once; one refusal per §5.10 template; deferred-VERIFY release; session-death abandon; needs_recovery gate; resume-skips-accepted | the §5.10 failure templates (one `parametrize` over template × expected code), `measure_diag_logs_*` (7), `prediction_gate_*` (9), `verify_diag_*` (7), `check_diag_*` (6), `predicted_ripple_*` (6) | 2,705 doc lines → ~250; the 43 private-symbol reach-ins; caplog float pins (`predicted_ripple_db=15.244`) | ~2,800 | **−9,400** |
| 2 | `web/correction_crossover_v2.py` → `test_correction_crossover_v2_endpoints.py` | 9,310 L / 189 t | one HTTP contract per endpoint (status/prepare/analyze/apply/observe): shape in, shape out, refusal code; the auto-apply background wiring once | `production_analyze_*` (7), `alternative_apply_*` (5), `status_block_*` (5), `state_cloud_*` (4) | all 7 `inspect.getsource` pins; the 1,319 L of local helpers (→ `crossover_v2_fixtures.py`); the capture-source forks (**#3724 owns these**) | ~2,400 | **−6,900** |
| 3 | `audio_measurement/program_analysis.py` → `test_audio_measurement_program_analysis.py` | 8,082 L / 187 t | the measurement maths, at *value* altitude — this is truth-layer code and deserves property/parametrized tests, not examples | `measure_level_*` (13), `channel_map_*` (12), `build_candidate_*` (10), `select_alignment_pair_*` (9), `diagnostic_summary_*` (7) — five families, ~50 tests → 5 parametrized | 1,733 doc lines → ~200; 938 L local helpers → shared corpus; the 54 private reach-ins | ~2,600 | **−5,500** |
| 4 | active_speaker facade → `test_sound_setup.py` | 7,727 L / 154 t | the wizard's save/reconcile/apply contract per topology kind | `sound_output_topology_*` (11), `active_speaker_*` (20), `topology_save_*` (6), `apply_profile_*` (6) → 4 parametrized over topology kind | 41 `.py` `read_text` pins; 1,374 L of local helpers | ~2,300 | **−5,400** |
| 5 | `baseline_profile.py` → `test_active_speaker_baseline_profile.py` | 6,954 L / 121 t | apply is transactional; applied state is durable; the staged hold is released; a refusal leaves the previous baseline live | **`test_apply_baseline_profile_*` — 21 tests / 1,235 L → 1 parametrized family over (fault injected, expected outcome)**; `baseline_profile_*` (14), `derive_corrections_*` (9), `build_baseline_profile_*` (5) | 897 L local helpers (`_topology`/`_preset` — 21 and 18 copies exist repo-wide) | ~1,800 | **−5,150** |
| 6 | `web/correction_setup.py` → `test_correction_setup.py` | 6,629 L / 186 t | one contract per route + the relay/wired fork **once** (the fork is #3724's to delete) | `relay_calibration_stored_*` (6) and the 29 prose `match=` refusals → one parametrized refusal-code table | 69 private-symbol reach-ins; 4 `getsource` pins | ~2,000 | **−4,600** |
| 7 | `crossover_envelope_v2.py` → `test_crossover_envelope_v2.py` | 6,217 L / 249 t | the envelope's own maths; the JS↔Python parity claim | **`test_the_browser_and_python_agree_on_*` (7) → one parametrized parity table driven by a shared JSON corpus** (the pattern `tests/test_capture_quality_vocabulary.py` already uses) | 1,435 doc lines; 8 source-text pins over `deploy/assets/*.js` | ~1,900 | **−4,300** |
| 8 | `runtime_contract.py` → `test_active_speaker_runtime_contract.py` (+ `test_audio_health.py`, `test_ring_active_endpoint.py`, `test_doctor_correction.py`, `test_control_server_system.py`) | 5,530 L prod / **18,928 L across 5 files** | **one** contract suite, not five: what a runtime graph must declare, what blocks it, what the doctor reports | `test_baseline_*_is_blocked` (8 byte-identical), the 12 `*_registered_in_sync_checks` 2-liners → 1 registry test, `active_speaker_runtime_*` (8) | the file's role as an importable fixture module (85 external imports) → move builders to `tests/active_speaker_fixtures.py` | ~6,000 | **−12,900** |
| 9 | `seat_level_ramp.py` + `seat_level_reference.py` → `test_active_speaker_seat_level.py`, `test_cli_seat_level.py`, `test_seat_level_anchor.py` | 6,537 L / 148 t | **NON-NEGOTIABLE — every distinct abort/stop hazard keeps its own test.** Trim prose (1,100 doc lines) and the 1,182 L of local helpers only | value families (level thresholds, mic states) around the hazards | nothing on the hazard list | ~4,200 | **−2,300** |
| 10 | the prescription family — `alignment`/`blend`/`driver`/`topology`/`spool`/`prescriber_status` | **14,582 L / 6,864 L prod** | the shared document contract once (round-trip, digest bound, unknown-field refusal, displacement disclosure), then per-kind maths | **the 4 cross-file duplicate names** (`test_a_mangled_durable_block_…` ×4, `test_the_response_format_states_every_bound_…` ×2, `test_a_document_edited_past_a_bound_…` ×2, `test_a_long_rationale_is_truncated_…` ×2) → one parametrized contract suite over prescription kind | 1,182+674+661 doc lines | ~5,500 | **−9,000** |

Cross-cutting moves (not double-counted with the table):

| move | Δ | risk |
|---|---:|---|
| Prose bar on the whole corpus: a test docstring is one line naming the behaviour; delete issue/ruling/date narration (2,318 `#NNNN`, 1,213 dates, 336 rulings). 53,599 doc + 24,843 comment → ~15,000 + ~9,000 | **−54,000** | low |
| Parametrize the remaining 68 name-prefix clusters not covered above (~350 tests) | −6,000 | low |
| Consolidate the 46 duplicated builders and the 8 fake-Camilla copies into `tests/crossover_v2_fixtures.py` / `active_speaker_fixtures.py` / `_fake_camilladsp.py`; kill the 258 cross-test-module imports | −6,000 | med (touches 117 files) |
| Delete the 160 non-non-negotiable source-text pins | −4,000 | low |
| Collapse the 5 incident-replay files to one behaviour pin each; drop the superseded characterization narrative and 3 of the 4 fixture dirs | −3,100 | low |
| Delete `engine_twin.py` + `test_engine_twin.py` + `engine_declarations.py` when the strangler lands | −866 | low, gated |
| Convert 805 prose `match=` to exception-type/code assertions | ~0 | low |

**Corpus estimate.** Holding the production surface constant, the moves
above remove **~110,000–125,000 lines**: 356,663 → **~235,000**. If the
tuning production surface itself halves (the other recon areas' brief), a
healthy 1.0–1.2 : 1 ratio puts the tuning test corpus at **150,000–180,000
lines** — under half of today. Parametrization should land near **30–35 %**
of test functions (from 10.6 %), test count near **4,000–5,000** (from
10,765), and average lines-per-test near **35** with the prose gone.

Uncertainty: the −54,000 prose number assumes every long docstring is
narration. I sampled ~15 and all were; I did not read all 6,299. Assume
±20 % on that line. The per-file targets in the table are shape estimates,
not commitments — the honest way to size each is to write the parametrized
family first and see what falls out.

---

## 7. Ranked top moves for this area

| # | move | Δ lines | risk | proof it is safe |
|---|---|---:|---|---|
| 1 | **Prose bar on test docstrings and comments** (mechanical per-file, by hand, never by script) | −54,000 | low | suite stays green; nothing executable is touched |
| 2 | **Parametrize the 80 sibling clusters** (507 tests → ~80 families), starting with `test_apply_baseline_profile_*` (21) and `test_baseline_*_is_blocked` (8 identical) | −14,600 | low | same assertion count before/after; `--co -q` count is the receipt |
| 3 | **Split the runtime-contract five-file pile** (18,928 L) into one contract suite + a real fixture module; end the 85 imports of a *test file* | −12,900 | med | `grep -c "from tests.test_" tests` → 0 for that module |
| 4 | **Consolidate the prescription family** (6 files, 14,582 L) onto one parametrized document contract | −9,000 | med | the 4 duplicate test names become 1 parametrized test |
| 5 | **Delete the 160 non-non-negotiable source-text pins**; keep `test_audio_safety_pins.py` and the Rust-literal subset of `test_outputd_wiring.py` | −4,000 | low | each deleted test has a sibling behaviour test in the same file; name it in the PR |
| 6 | **De-duplicate builders and fakes** (46 names, 4,564 L; `FakeCamilla`/`_FakeCamilla` ×8) | −6,000 | med | `scratchpad/recon/clusters2.py` re-run shows the names gone |
| 7 | **Collapse the incident-replay corpus** to one pin each + drop 3 fixture dirs | −3,100 | low | the flipped assertions survive; the characterization prose does not |
| 7b | **Delete the 23 test-only production symbols** (§3 d2) with the tests that hold them up — coordinate with the production-scope agents; the 2 relay ones are #3724's | −1,224 prod / −1,000 test | low | re-run `scratchpad/recon/testonly.py`: the symbol is gone from both sides |
| 8 | **Convert 805 prose `match=` to codes/types** | ~0 | low | rename a refusal sentence in production; suite stays green |
| 9 | **Delete `engine_twin` scaffolding** — after the strangler lands | −866 | low | gated on REFACTOR-TUNING wave 2 |
| 10 | **Add a `slow` marker + `-m "not slow"` iterate lane** — *after* 1–7, and only if the 149 sleeps survive them | +50 | low | `pyproject.toml` gains a `markers` table (it has none today) |

**Do not do:** touch anything `capture_source`/relay-shaped until **#3724**
and its stacked follow-ups land — that is ~7,000 test lines of overlap.
Do not weaken the §4 non-negotiable files; trim their prose only. Do not
add a line-count CI gate.
