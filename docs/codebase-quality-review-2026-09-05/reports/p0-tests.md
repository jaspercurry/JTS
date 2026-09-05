# Phase 0 cartography — test suite health

Checkout `2d571e6b8`. Method: static AST/regex analysis of `tests/` (960 .py
files) — **no `pytest --collect-only` run**. `uv run pytest --collect-only`
needs `camilladsp @ https://github.com/HEnquist/pycamilladsp/archive/...`
(`[project.optional-dependencies].streambox`), and the sandbox proxy returns
403 on that GitHub archive URL (confirmed with a direct `curl`, also 403).
No cached wheel/sdist exists on disk. All counts below are AST-derived
(scripts in this scratchpad dir: `collect_stats.py` + `sectionN_*.py`);
anywhere a claim needed cross-checking against real production code I read
the file (paths cited). Test-function/case counts are therefore a **static
lower bound**, not pytest's collected-item count.

## 1. Size

| Metric | Value |
|---|---|
| Files under `tests/` (any depth) | 960, 585,440 LOC |
| `test_*.py` files | 902, 574,399 LOC |
| Helper/fixture `.py` (non-`test_`, non-`conftest`) directly under `tests/` | 48 files, 8,816 LOC |
| `conftest.py` (+ `voice_eval/conftest.py`) | 569 + 128 = 697 LOC |
| `def test_*` functions (AST count) | **19,461** |
| — parametrized (`@pytest.mark.parametrize`) | 1,848 functions |
| — parametrize cases from a literal list/tuple | 6,695 |
| — parametrize decorators with dynamic argvalues (uncountable statically) | 243 |
| Static lower-bound case count | ≈24,308 (undercounts — dynamic argvalues unresolved) |

Product code: `jasper/` = 424,462 LOC. **Test tree (585k) is 1.38x product
code**, consistent with the brief's figures.

### Top 30 largest test files

| LOC | tests | file |
|---:|---:|---|
|9367|190|tests/test_correction_crossover_v2_endpoints.py|
|8084|186|tests/test_audio_measurement_program_analysis.py|
|7773|157|tests/test_sound_setup.py|
|6934|121|tests/test_active_speaker_baseline_profile.py|
|6208|249|tests/test_crossover_envelope_v2.py|
|5483|171|tests/test_active_speaker_runtime_contract.py|
|5174|120|tests/test_active_speaker_seat_level.py|
|4744|165|tests/test_crossover_v2_driver_prescription.py|
|4458|127|tests/test_audio_health.py|
|4210|83|tests/test_ring_active_endpoint.py|
|4145|132|tests/test_correction_setup.py|
|4004|76|tests/test_active_speaker_driver_safety.py|
|3959|132|tests/test_active_speaker_linearization_fit.py|
|3823|92|tests/test_aec_reconcile.py|
|3743|152|tests/test_multiroom_reconcile.py|
|3568|91|tests/test_spatial_combine.py|
|3460|72|tests/test_audio_hardware_reconcile.py|
|3287|72|tests/test_crossover_v2_round_wiring.py|
|3258|132|tests/test_crossover_v2_blend_prescription.py|
|3196|135|tests/test_web_rooms_setup.py|
|3062|111|tests/test_install_helpers.py|
|3036|85|tests/test_openai_session.py|
|2822|94|tests/test_active_speaker_crossover_v2_round_views.py|
|2662|86|tests/test_active_speaker_commissioning_capture.py|
|2654|97|tests/test_volume_coordinator.py|
|2601|76|tests/test_crossover_v2_prescription_spool.py|
|2585|109|tests/test_crossover_v2_verification.py|
|2552|54|tests/test_correction_status_and_bundles.py|
|2542|110|tests/test_active_speaker_delta_probe.py|
|2448|64|tests/test_fanin_coupling_reconcile.py|

Top 5 alone = 38,366 LOC (6.7% of the whole test tree), all in the
**active-speaker / audio-measurement / correction / crossover-v2 tuning
program** — consistent with in-flight waves w6–w9.

### Test LOC per product package (primary-jasper-import attribution)

Method: for each test file, the most-imported top-level `jasper.<pkg>`
package across its `from jasper... import` statements is its "primary"
package; the file's whole LOC is attributed there. 138 test files had no
static `jasper` import at all (deploy/systemd/shell/CI/rust-contract tests —
expected, not a gap).

| package | code LOC | test LOC | ratio | files | tests |
|---|---:|---:|---:|---:|---:|
|active_speaker|140,932|210,518|1.49|206|6407|
|(top-level `jasper/*.py`)|58,273|127,675|**2.19**|204|4496|
|audio_measurement|25,427|35,669|1.40|49|1207|
|web|45,183|31,202|0.69|44|1214|
|cli|46,194|20,861|0.45|45|795|
|control|18,959|19,918|1.05|32|741|
|correction|13,872|12,197|0.88|16|409|
|multiroom|7,979|10,864|1.36|16|496|
|tools|5,599|8,406|1.50|18|353|
|bass_extension|10,134|7,190|0.71|19|279|
|voice|9,269|7,183|0.77|21|248|
|**chip_aec**|1,728|6,055|**3.50**|8|188|
|sound|4,156|5,816|1.40|10|217|
|bluetooth|2,920|5,393|1.85|8|168|
|accessories|3,002|5,373|1.79|6|192|
|fanin|4,719|4,113|0.87|5|120|
|route_latency|2,091|3,515|1.68|10|163|
|wake_corpus|5,065|3,461|0.68|4|120|
|calibration_agent|4,521|2,430|0.54|9|87|
|peering|2,746|2,041|0.74|7|96|
|audio_hardware|2,274|1,980|0.87|2|66|
|research|1,318|1,441|1.09|5|43|
|attribution|2,048|1,197|0.58|1|40|
|transit|1,665|1,122|0.67|6|71|
|cues|1,649|1,115|0.68|4|56|
|usbsink|624|923|1.48|1|32|
|**local_sources**|341|818|**2.40**|5|37|
|xvf|473|387|0.82|1|17|
|aec_engines|369|166|0.45|2|13|
|**mics**|929|**0**|**0.00**|0|0|
|data|3|0|0.00|0|0|

- **Ratio > 2.0** (test-heavy): `(top-level)` 2.19, `chip_aec` 3.50,
  `local_sources` 2.40. `chip_aec` and `local_sources` are thin
  hardware-adjacent shims (1.7K / 0.3K LOC) with disproportionate test
  weight — likely fine (small surface, many edge cases), not re-verified
  file-by-file.
- **Ratio < 0.3** (cloc>500, thin coverage): none by this attribution.
  `jasper/mics` (929 LOC) shows **zero** primary-attributed test files —
  see §10, this is a false alarm (its one module, `xvf3800.py`, is tested
  under the `chip_aec`/xvf attribution bucket instead: `tests/test_xvf3800_profile.py`
  imports `jasper.mics.xvf3800`, but `xvf` was already the plurality
  package for that file). Not a real gap.

## 2. "Guard"/meta tests (read real repo files, assert on structure)

Naive scan (`.read_text()`/`inspect.getsource`/`Path(__file__).parents[N]`/
`git ls-files`/`subprocess` git) hit **417 files, 2,266 lines** — but most of
that is tests reading files **they themselves wrote to `tmp_path`** (checking
what the code under test produced), not source-reading guards.

Refined check (`guard_detect.py`): a `read_text()`/`open()`/`getsource`/
`subprocess git` call only counts if its target is provably rooted at the
repo tree (`Path(__file__).resolve().parents[N]`-derived, or a literal
containing `deploy/`, `jasper/`, `.github/`, `docs/`, `systemd`, `nginx`,
etc., with no `tmp_path` in the same expression) — **209 files, 857 lines**.

Top 20 by hit count:

| hits | file |
|---:|---|
|73|tests/test_outputd_wiring.py|
|41|tests/test_install_helpers.py|
|38|tests/test_renderer_ring_lanes.py|
|28|tests/test_sound_setup.py|
|23|tests/test_web_design_system.py|
|21|tests/test_camilla_systemd_unit.py|
|18|tests/test_landing_page_html.py|
|17|tests/test_system_setup.py|
|16|tests/test_build_and_ci_contracts.py|
|14|tests/test_ring_active_endpoint.py|
|14|tests/test_usbsink_systemd.py|
|14|tests/test_camilla_crossover_unit.py|
|14|tests/test_bluetooth_setup_ui.py|
|12|tests/test_wire_contracts.py|
|12|tests/test_deploy_wiring_guards.py|
|12|tests/test_source_intent_systemd.py|
|11|tests/test_usb_mic_systemd.py|
|10|tests/test_install_usbgadget_migration.py|
|10|tests/test_first_party_arm64_release.py|
|9|tests/test_audio_hardware_reconcile.py|

Rough filename-bucket of the 857 hits (mechanical regex on path, **not**
full content review beyond the files below): systemd-unit pinning 103,
wiring/cross-artifact contract 193, web HTML/CSS structural 111,
install/deploy/CI guard 96, script/shell content guard 38, doc/governance 1,
**unclassified 315 (136 files, not opened)**.

**Classification of the files I actually opened and read** (all
`Earns-its-keep` — none violated the letter of AGENTS.md's "never assert on
source text or prose"):

| File | What it enforces | Class |
|---|---|---|
|`tests/test_outputd_wiring.py:29+`|Env-var names and `After=`/`Wants=` ordering agree across shell scripts, systemd units and Rust config|**Architecture contract**|
|`tests/test_camilla_systemd_unit.py:32-70`|`After=`/`Wants=`/`CPUSchedulingPolicy=`/`LimitRTPRIO=` parsed via `tests/systemd_unit_helpers.py` and asserted as structured fields, cross-checked against sibling units' priorities, not literals|**Architecture contract** (exemplary — cites the 2026-05-07 incident it guards)|
|`tests/test_web_design_system.py:21-27`|`deploy/assets/app.css` is the one design-token source; landing page and per-page CSS don't duplicate the token block|**Architecture contract**|
|`tests/test_launch_blocker_docs_exist.py:31-45`|LICENSE/NOTICE/SECURITY.md/PRIVACY.md/etc. exist, are non-empty, and README links PRIVACY.md (regex on a link target, not prose)|**Architecture contract** (cites the PRIVACY.md-silently-dropped incident, #636)|
|`tests/test_ci_classifier.py`|Unit-tests the real `scripts/ci-classify.py` via `importlib`, against synthetic fixtures written to `tmp_path` — does **not** itself read repo prose|**Not a guard** — ordinary tool test|
|`tests/test_xvf_host.py:17-30`|`_FORBIDDEN_COMMANDS = {"SAVE_CONFIGURATION", ...}` — AST/regex scan of `jasper/xvf/xvf_host.py` + callers, asserting the forbidden command set never appears|**Architecture contract**, ties to non-negotiable #2|
|`tests/test_aec_probe_xvf_ref_level_script.py:38-49`|Forbidden-fragment scan (`SAVE_CONFIGURATION`, `REBOOT`, `/etc/jasper`, `tee /etc`) of the probe shell script text|**Architecture contract**, ties to non-negotiable #2|
|`tests/test_cue_registry_coverage.py:1-50`|Bidirectional static cross-check: every `CUES` registry slug has a play site, every play site names a registered slug|**Architecture contract**, ties to non-negotiable #6, exemplary|
|`tests/test_doctor_renderers.py:704-716`|Pins the exact `sudo -n -u $USER env LC_ALL=C timeout N aplay ...` **argv list** (not a string), with a comment reading "Non-negotiable #5's command, pinned as a list rather than as source text"|**Architecture contract**, ties to non-negotiable #5, exemplary|

Nine files opened, ~450 of the 857 lines accounted for — all legitimate.
**The population sampled skews strongly toward the "worth keeping" bucket**,
not the "prose pin" anti-pattern; I found no counter-example. The remaining
136/315 unclassified files were not opened — treat as unverified, not as
confirmed-clean.

## 3. Prose pins

| Pattern | Count | Files |
|---|---:|---:|
|`caplog.text` assertions|690|93|
|`match=` with prose-like text (space + two multi-letter words)|696|167|
|`assert "..." in out/stdout/capsys/result.output`|655|62|

Top offenders:

| n | file | pattern |
|---:|---|---|
|125|tests/test_crossover_v2_conductor_diagnostics.py|caplog.text|
|54|tests/test_crossover_v2_conductor_integration.py|caplog.text|
|44|tests/test_active_speaker_commissioning_receipt.py|match=|
|33|tests/test_active_speaker_commissioning_evidence.py|match=|
|28|tests/test_active_speaker_commissioning_run.py|match=|
|60|tests/test_audio_hardware_reconcile.py|assert in out|
|56|tests/test_web_transit_setup.py|assert in out|
|48|tests/test_web_spotify_setup.py|assert in out|
|40|tests/test_deploy_health_script.py|assert in out|

Two different flavors hide under "assert in out":

1. **Structured-value checks disguised as string containment** — e.g.
   `tests/test_audio_hardware_reconcile.py:876-878`:
   `assert "JASPER_OUTPUTD_SINK=single_alsa" in outputd_env` — this is a
   `KEY=VALUE` pin on an env file, not prose. Defensible, if slightly
   informal (a real env-file parser would be more robust).
2. **True prose pins on exception-message wording** — real AGENTS.md
   violations: `match='exactly equal'`, `match='disk full'`,
   `match='four attempts'`, `match='schema version'`
   (`tests/test_active_speaker_commissioning_receipt.py:427-490`,
   `tests/test_active_speaker_repeat_admission.py:81-233`,
   `tests/test_dac_profiles.py:489-516`, `tests/test_bass_extension_profile.py:239-253`)
   — fragile to a wording-only refactor; should assert a typed exception
   attribute or error code instead.
3. **CLI-stdout wording pins** on tools whose contract genuinely IS their
   stdout text (the w6-w9 "stdout is the answer" CLIs):
   `tests/test_deploy_health_script.py:852` (`"deploy health passed" in output`),
   `tests/test_s0_sync_measure.py:118-119` (`"CLOCK-LOCK gate : FAIL/INCOMPLETE"`,
   `"S0-SYNC VERDICT: FAIL"`), `tests/test_audit_wake_corpus.py:120-121`
   (`"Issues: none"`). Letter-of-the-law violations, but arguably
   intentional given the tool's actual contract — flag, don't auto-fix.
4. **Duplicated-across-files HTML boilerplate** (not itself prose, but a
   duplication smell): `test_transit_page_is_canonical_document`/
   `test_transit_page_has_shared_app_header`-shaped tests
   (`assert out.startswith("<!doctype html>")`, `assert "/assets/app.css?v=" in out`,
   `assert 'class="app-header"' in out`) are hand-copied near-verbatim
   across **10 files**: `test_web_airplay_setup.py`, `test_web_bluetooth_setup.py`,
   `test_web_home_assistant_setup.py`, `test_web_rooms_setup.py`,
   `test_web_speaker_setup.py`, `test_web_spotify_setup.py`,
   `test_web_transit_setup.py`, `test_web_voice_setup.py`,
   `test_web_weather_setup.py`, `test_web_wifi_setup.py`
   (verified: `tests/test_web_transit_setup.py:78-93` vs.
   `tests/test_web_spotify_setup.py:37-48`, near-identical). No shared
   helper exists for this exact pattern (`tests/_web_test_helpers.py`
   covers CSRF handshake and measurement-window patching, not this). See §11.

## 4. Mock-heavy tests

Top 15 files by raw `monkeypatch.setattr`/`patch(`/`patch.object(` count:

| mocks | tests | /test | file |
|---:|---:|---:|---|
|156|152|1.03|tests/test_multiroom_reconcile.py|
|150|44|**3.41**|tests/test_active_speaker_web_commissioning.py|
|142|135|1.05|tests/test_web_rooms_setup.py|
|125|64|1.95|tests/test_web_bluetooth_setup.py|
|115|157|0.73|tests/test_sound_setup.py|
|95|46|2.07|tests/test_doctor_renderers.py|
|87|190|0.46|tests/test_correction_crossover_v2_endpoints.py|
|82|25|**3.28**|tests/test_control_server_aec.py|
|81|38|2.13|tests/test_measurement_window.py|
|80|83|0.96|tests/test_ring_active_endpoint.py|

Top by mocks-per-test (≥5 tests): `test_audio_hw_validate.py` **7.80**/test
(39/5), `test_wifi_setup_scan_health.py` 3.88/test, `test_active_speaker_web_commissioning.py`
3.41/test, `test_control_server_aec.py` 3.28/test, `test_supervisor_start_wrappers.py`
3.20/test.

**Private-name (`_foo`) patching — implementation-pinning smell:**
**191 files, 1,645 occurrences.** Top 15:

| n | file | example targets |
|---:|---|---|
|65|tests/test_correction_status_and_bundles.py|`_classify_live_bass_extension_graph`, `_dsp_apply_lock`, `_read_loadavg_1m`, `_camilla`|
|61|tests/test_measurement_window.py|`_acquire_measurement_gate`, `_release_measurement_gate`, `_measurement_hold_command`|
|58|tests/test_correction_setup.py|`_classify_live_bass_extension_graph`, `_read_json_body`, `_crossover_blocking_phase`|
|58|tests/test_doctor_renderers.py|`_run` (×many)|
|57|tests/test_multiroom_reconcile.py|`_output_topology_state`, `_systemctl_unit_state`, `_write_args_file`|
|45|tests/test_control_aec_state.py|`_AEC_MODE_FILE`, `_WAKE_MODEL_FILE`, `_fresh_jasper_env`|
|40|tests/test_web_sources_setup.py|`_local_sources_allowed`, `_unit_available`, `_unit_active`|
|39|tests/test_active_speaker_web_commissioning.py|`_load_driver_commissioning_config_for_level`|
|39|tests/test_control_server_aec.py|`_AEC_MODE_FILE`, `_aec_full_status`|
|39|tests/test_sound_setup.py|`_active_speaker_stop_summed_test_tone`, `_active_speaker_stop_commission_tone`|
|38|tests/test_doctor_aec.py|`_read_outputd_status_for_aec_reference`, `_parked_follower_result`|
|38|tests/test_web_correction_setup.py|`_run_async`, `_session`, `_start_in_progress`|
|37|tests/test_sound_setup_commission.py|`_active_speaker_play_commission_tone`, `_COMMISSION_TONE_SESSION`|
|37|tests/test_web_rooms_setup.py|`_get_member_grouping_readiness`, `_read_peering_block`, `_self_address`|
|35|tests/test_system_metrics.py|`_read_mem_psi_some_avg60`, `_read_oom_kill`, `_read_meminfo`|

A test that patches `module._private_helper` is coupled to the
implementation's internal decomposition, not its public contract — a
refactor that keeps behavior identical but renames/inlines the private
helper breaks the test for no behavioral reason. `_run` (patched 58x alone
in `test_doctor_renderers.py`) and `_read_*` system-probe helpers dominate;
these look intentional (isolating from real subprocess/proc reads), but the
sheer count (1,645 across 191 files, ~21% of all test files) is a
systemic pattern worth a design conversation, not a file-by-file fix.

## 5. Shared helper sprawl

48 helper/fixture files under `tests/` (non-`test_*`), 8,816 LOC total,
usage counted by grepping `from tests.<mod> import` / `from .<mod> import`
across all 902 test files (verified against false negatives from relative
imports — an earlier pass missed `from .X import Y` and wrongly flagged 8
files as "unused"; all 8 are in fact imported by 4-19 test files each).

| file | LOC | used by N test files |
|---|---:|---:|
|tests/crossover_v2_fixtures.py|2154|44|
|tests/_flat_lin_corpus.py|588|9|
|tests/active_speaker_fixtures.py|428|55|
|tests/engine_twin.py|392|4|
|tests/crossover_v2_round_harness.py|371|2|
|tests/correction_bundle_fixtures.py|370|8|
|tests/wake_corpus_setup_fixtures.py|355|6|
|tests/control_server_fixtures.py|338|7|
|tests/_web_test_helpers.py|334|22|
|tests/doctor_test_support.py|162|17|
|tests/_async_wait.py|114|37|

**Single-use "shared" files** (extraction defeats its own purpose — should
be inlined into their one caller):

| file | LOC | sole user |
|---|---:|---|
|tests/_ring_negotiation_model.py|288|tests/test_ring_emitter_ioplug_negotiation.py|
|tests/engine_declarations.py|93|tests/test_engine_twin.py|
|tests/_voice_runtime_text.py|22|tests/test_outputd_systemd.py|

**Issue #4041 (merged same-day at HEAD, `d1a1fdd3a`)** already did a round
of this: it created `download_response_fixtures.py`, `failure_detail_fixtures.py`,
`fake_clock_fixtures.py`, `sound_camilla_fixtures.py`, `usage_store_fixtures.py`,
`wired_capture_fixtures.py` from duplicated test doubles across ~20 files. Not
re-reporting that as new. **Residual near-duplication it left behind:**
5 separate hand-rolled `class FakeClock` definitions still exist
(`tests/test_active_speaker_seat_level.py:171`, `tests/test_arm_walk.py:64`,
`tests/test_audio_measurement_ramp.py:63`, `tests/test_watchdog.py:80`,
`tests/test_wired_capture.py:134`) alongside the new shared
`tests/fake_clock_fixtures.py:12`. Verified these are **not** exact
duplicates of the shared one or each other — three different protocols
(monotonic `.now`+async `.sleep()`, a callable with a side-effect recorder,
an auto-incrementing-ns callable) — so this is a Nit (2-3 shared protocols
could cover all 5), not more #4041 work.

`tests/conftest.py`: 18 fixtures, **13 `autouse=True`** doing isolation
(`_isolate_environ`, `_isolate_tts_wire_width_cache`, `_isolate_startup_hold_marker`,
`_isolate_canonical_target_provider`, `_isolate_process_volume_owner`,
`_isolate_capture_entry_anchor`, `_isolate_seat_level_reference`,
`_isolate_identity_file`, `_isolate_driver_base_trim`,
`_isolate_output_hardware_state`, `_isolate_commissioning_disclosure`,
`_isolate_correction_volume_claims`, `_isolate_jasper_logger_level`,
lines 169-477). This is not itself a test-suite problem, but it is a
**signal**: 13 distinct pieces of process-global mutable state in `jasper/`
need resetting between tests — a proxy for how much module-level singleton
state the product carries.

## 6. Slow/flaky signals

| Signal | Count |
|---|---:|
|`time.sleep()` calls|71, across 26 files|
|files using real `socket.*`|17|
|files using real `subprocess.*`|146|
|files using real `threading.*`|75|
|`@pytest.mark.flaky`|0|
|`pytest.mark.xfail`|0|
|`pytest.mark.skipif`|45 (all 45 carry `reason=`)|
|`pytest.skip(...)`|62|
|`pytest.importorskip(...)`|15|

No flaky markers, no xfails anywhere — decent hygiene (no permanently-quieted
red tests). Heaviest sleeper: `tests/test_wake_corpus_recording.py` — 22
`time.sleep()` calls (0.03s-0.8s, e.g. lines 243, 273, 740), real-thread
recording-pipeline coordination; a genuine CI-timing/flakiness risk
candidate for a fake-clock or condition-wait rewrite.

**Real flakiness signal, not just slowness:**
`tests/test_crossover_envelope_v2.py:4667` and `:5741` —
`pytest.skip("clock is within 2 h of local midnight")` — a time-of-day
dependent skip. Worth checking this doesn't silently no-op in CI at the
wrong hour.

## 7. Example clusters (parametrization candidates)

AST clone-detection (`section7_clusters.py`): normalizes `Constant` literals
and `Store`-context variable names, groups sibling `def test_*` in the same
file/class by structural-dump equality, excludes already-parametrized
functions.

- **At the brief's threshold (≥5 identical-shape siblings): exactly 1
  cluster** — `tests/test_active_speaker_runtime_contract.py:2602-2717`,
  8 functions (`test_baseline_headroom_unwired_is_blocked`,
  `test_baseline_positive_headroom_gain_is_blocked`, ... 6 more), each
  tampering one YAML key and asserting a different refusal code. Verified
  by reading lines 2602-2625 — genuinely identical shape, different
  literals only.
- **At threshold ≥3: 35 files, 37 clusters, 123 functions, spanning 996
  LOC** (measured directly from the AST). Top examples, all spot-verified:

| n | file | example names |
|---:|---|---|
|4|tests/test_env_file.py:20-44|`test_upsert_appends_when_absent`, `test_upsert_unchanged_when_value_identical`, `test_upsert_dedupes_later_duplicate_assignments`, `test_upsert_quoted_value_compares_unquoted`|
|3|tests/test_home_assistant.py|`test_url_normalization_strips_trailing_slash` +2|
|4|tests/test_identity_reconcile_script.py|4 `test_*_jasper_hostname_*` variants|
|4|tests/test_multiroom_config.py|4 `test_invalid_*_sets_error` variants|
|4|tests/test_spotify_routing.py|4 `test_match_track_*` variants|
|4|tests/test_citibike.py|4 `test_parse_*` variants|

Spot-checked `tests/test_env_file.py:20-44` directly: 4 functions, each
calling `env_file.upsert(<literal>, "B", <literal>)` and asserting the two
return values — a textbook `@pytest.mark.parametrize` case.

This suggests hand-duplicated "literal-only" clusters are a **real but
modest** contributor (~1,000 LOC out of 574K, <0.2%) — most of the size in
§1 comes from breadth of scenarios, not copy-paste-with-different-numbers.

## 8. Tests whose subject moved or was deleted

Static check (`section8_stale_imports.py`): for every
`from jasper.X.Y import name` in `tests/`, resolve `jasper/X/Y.py` and
verify `name` is actually defined there — without executing pytest.

Two rounds of false positives found and fixed along the way (both worth
knowing about for anyone repeating this exercise):

1. `from package import submodule_file` is valid Python even when
   `__init__.py` never imports it — the import system resolves the
   submodule directly. Naive checking flagged ~1,780 fake "stale imports"
   for exactly this shape (e.g. `from jasper.active_speaker import
   crossover_v2_flow`).
2. `jasper/active_speaker/__init__.py` (and others, e.g.
   `jasper.multiroom`) use PEP 562 lazy re-exports: a `_LAZY_ATTRS: dict[str,str]`
   registry consumed by a module-level `__getattr__`
   (`jasper/active_speaker/__init__.py:19-244`) — every name in that dict is
   a valid, real export that a plain AST scan of `__init__.py`'s top-level
   assignments won't see unless it also reads the dict's keys.

After accounting for both: **0 stale-import signals across all 902 test
files.** No test statically imports a name that doesn't exist. (This is a
narrower claim than "no test targets dead functionality" — a test whose
*target still exists* but whose behavior assumptions are stale wouldn't be
caught this way; e.g. ADR-0236's same-day independent-subwoofer deletion
already had `jasper/bass_management.py` removed with its tests cleanly gone
too, confirming the method works, but that's already-landed work, not a
finding.)

## 9. `tests/voice_eval/` paid-lane isolation

Layered, and each layer independently verified:

1. **`tests/voice_eval/conftest.py:98-113`** — the `voice_eval_config`
   fixture calls `pytest.skip(...)` unless `Config.from_env()` succeeds
   *and* the active provider's API key env var is set *and*
   `OPENAI_API_KEY` is set (for TTS). Session-scoped, so one skip disables
   the whole file. Safe-by-default even under a bare `pytest tests/voice_eval/`.
2. **`scripts/test-merge:78`** — `pytest ... --ignore=tests/voice_eval`.
   This is what CI actually runs.
3. **`.github/workflows/tests.yml:367-374`** — the required `ci` job step is
   literally named `"Test (hardware-free; voice_eval explicitly excluded)"`,
   with a comment stating the exclusion is "justified solely by paid-LLM
   cost." No `GEMINI_API_KEY`/`OPENAI_API_KEY`/`XAI_API_KEY` secrets appear
   anywhere in `tests.yml` — even a `pytest` invocation that forgot
   `--ignore` would skip cleanly (no keys to find).
4. **`scripts/test-fast:191`** — the file→test routing table has an empty
   case arm for `tests/voice_eval/*` (selects **zero** tests), so editing
   voice_eval doesn't accidentally trigger it via the fast-test-selection path.
5. **`tests/test_voice_eval_registry.py`** — lives in the *top-level*
   `tests/` package (so CI *does* collect it) specifically to hardware-free
   test the harness's tool-registry-building logic (`_build_test_registry`)
   with synthetic keys, no network. Its docstring cites a real bug this
   caught: `cfg.bus_stop_id`/`cfg.subway_lines` didn't exist on `Config`,
   silently breaking the paid-only transit scenario until someone spent
   money running it.

**Residual risk:** none of this is a `pytest.mark` opt-in — it's entirely
path- and env-based. A new test file placed *outside*
`tests/voice_eval/` that imports `tests.voice_eval.harness` and calls
`harness.ask()` for real (rather than following `test_voice_eval_registry.py`'s
synthetic-construction pattern) would not be caught by `--ignore=tests/voice_eval`
and would run in CI. No such file exists today (checked: only
`test_voice_eval_registry.py` imports from `tests.voice_eval`, and it never
touches `harness.ask()`).

## 10. Coverage gaps

**Method and its limits, stated up front:** import-based coverage checking
badly undercounts in this codebase, because a lot of code is deliberately
reached only through a composed surface (an HTTP server, a CLI's `main()`
dispatch, a base-class registry) rather than by direct import in a test.
I mechanically found 71 "never imported by any test" modules (14,774 LOC),
then filtered by grepping every one of that module's public top-level names
across all test source (not just imports) — 28 modules (2,517 LOC) still
came back with **zero** name hits anywhere. I then manually verified a
sample of those 28, and most are *also* false positives:

- The entire `jasper/cli/round_views/*.py` family (11 files, ~1,438 LOC —
  `delay.py`, `close_reference.py`, `classify_features.py`, `seats.py`,
  `grades.py`, `repeat.py`, `forward_model.py`, `sweeps.py`, `distortion.py`,
  `frequency.py`, `inventory.py`) showed 0 name hits because each is
  dispatched by an argparse subcommand string, not imported by name —
  `tests/test_active_speaker_crossover_v2_round_views.py` drives all of
  them via `jasper.cli.round_views.main(["delay", ...])` (verified: 30+
  `from jasper.cli.round_views import main` / `from jasper.cli import
  round_views as cli` call sites in that one file). **Not a gap.**
- `jasper/control/handlers/{aec,volume,grouping,voice}.py` and
  `jasper/bluetooth/handlers/base.py` / `jasper/control/handlers/_base.py`
  show the same pattern — tested through the composed
  `jasper.control.server`/bluetooth-engine surface
  (e.g. `tests/test_control_server.py:555`, `tests/test_control_server_aec.py`
  docstring: "Route tests for `jasper.control.handlers.aec`"). **Not a gap.**

**After removing the verified false positives, genuinely unresolved
candidates** (public names appear nowhere in any test, and I did not find
an indirect composed-surface test covering them):

| LOC | module | note |
|---:|---|---|
|157|jasper/correction/replay_artifacts.py|Wired into production (`jasper/correction/artifacts.py:27,159` → `jasper/correction/session.py:503,942`), but its own functions/`SCHEMA_VERSION` are never named in any test; the one nearby test (`tests/test_correction_analysis_characterization.py:540`) monkeypatches the *caller* (`_write_capture_replay_artifacts`) away entirely, so this module's own derivation math looks untested even where the wiring around it is.|
|**85**|jasper/bass_extension/bench/excitation.py|**`build_requested_bass_plan` (lines 52-76) has zero callers repo-wide** — grepped `jasper/`, `tests/`, `scripts/`, `deploy/`, `docs/`; the only two hits are its own `def` and its own `__all__` entry (line 83). Its sibling export `prepare_driver_excitation_plan` (line 29, re-exported from `jasper.active_speaker.excitation_safety_plan`) *is* heavily used/tested — only the local wrapper is orphaned. Likely dead code from scaffolding that `jasper/bass_extension/bench/executor.py` never wired up (executor.py:200 reads `request.requested_stimulus_effective_peak_dbfs` directly rather than calling this helper).|
|85|jasper/web/pair_flow.py|1/2 public names found|
|**46**|**jasper/secret_redaction.py**|**`redact_secrets()` — the pattern-based credential scrubber non-negotiable #3 depends on — has zero references anywhere in `tests/`** (confirmed: `grep -rn "redact_secrets\|secret_redaction" tests/` returns nothing). Used in production by `jasper/voice/_supervisor.py`, `jasper/cli/doctor/_shared.py`, `jasper/cli/doctor/voice.py` to scrub provider HTTP error bodies before they hit logs/`/state`/doctor output. No test verifies a Bearer token, API key, or PSK is actually redacted by its three regexes. **This is the single most important gap this cartography found** — see ranked list.|
|50|jasper/tools/google_errors.py|0/2|
|47|jasper/cli/_unit_pair.py|0/2|
|30|jasper/percentiles.py|0/1|
|29|jasper/oauth_redirect.py|0/2|
|24|jasper/os_fault.py|0/1|
|23|jasper/audio_hardware/text_property.py|0/1|
|23|jasper/calibration_agent/curves.py|0/1|
|21|jasper/correction/_numbers.py|0/1 (private-style module)|
|14|jasper/accessories/_dbus.py|0/1 (private-style module)|

These remaining ~14 modules (~600 LOC) were **not** individually verified
for indirect/composed coverage the way round_views and control/handlers
were — treat as "worth a direct look," not "confirmed untested," except
`secret_redaction.py` and `bench/excitation.py`, which I did verify by
repo-wide grep.

### Non-negotiables — test coverage check (all 8, per AGENTS.md's closed list)

| # | Non-negotiable | Status | Evidence |
|---|---|---|---|
|1|`volume_limit` stays 0.0 / `set_volume_db` clamps positive writes / commissioning SPL stop|**Covered**|`tests/test_camilla_controller.py:163 test_set_volume_db_clamps_positive_gain_to_zero`; `jasper/camilla_config_contract.py:457` raises if `volume_limit_db > 0`; `volume_limit == 0.0` pinned in emitted YAML at `tests/test_active_speaker_commissioning_config.py:89`, `tests/test_active_speaker_emit_gate.py:908`, `tests/test_active_speaker_driver_domain.py:105`, +more; SPL stop: `tests/test_active_speaker_seat_level.py:1291 test_the_ramp_never_commands_above_the_stimulus_ceiling`, `:1513 test_a_measured_level_over_the_commissioning_ceiling_aborts`|
|2|Never call `SAVE_CONFIGURATION` on the XVF3800|**Covered, exemplary**|`tests/test_xvf_host.py:17-30` — static `_FORBIDDEN_COMMANDS` AST scan of `jasper/xvf/xvf_host.py`; `tests/test_aec_probe_xvf_ref_level_script.py:38-49` — forbidden-fragment scan of the probe shell script|
|3|Secrets never appear in logs/`/state`/doctor|**GAP**|`jasper/secret_redaction.py`'s `redact_secrets()` has zero test references anywhere (see above)|
|4|Deploy integrity: identity/direction guards|**Covered**|`tests/test_lib_deploy_direction.py:156-373` (forward/downgrade/diverged/unknown classification, thorough); `tests/test_laptop_onboarding_scripts.py:810` tests `JTS_ACCEPT_NEW_IDENTITY="1"` override behavior directly|
|5|Renderer ALSA devices resolve as the unit's real `User=`|**Covered, exemplary**|`tests/test_doctor_renderers.py:704-716` pins the exact `["sudo","-n","-u","pi","env","LC_ALL=C","timeout",...,"aplay",...]` argv **as a list**, comment: "Non-negotiable #5's command, pinned as a list rather than as source text"|
|6|No silent deafness — failure paths play a registered cue|**Covered, exemplary**|`tests/test_cue_registry_coverage.py:1-50` — bidirectional static cross-check, `CUES` registry slugs vs. every `.play("slug")`/`_play_cue("slug")` call site in `jasper/`, both directions|
|7|Paid tests (`tests/voice_eval/`) never loop/auto-retry|**Covered** — see §9|`tests/voice_eval/conftest.py` docstring: "the harness never retries" (line 45); no retry/loop wrapper found around `harness.ask()`|
|8|`main` protected / CI green before merge|N/A to this cartography — a branch-protection/CI-config fact, not a test-suite artifact||

**Seven of eight non-negotiables have heavy, well-designed tests — several
are genuinely exemplary guard-test design** (structured-field pins with an
explicit comment naming which non-negotiable they enforce). **One has zero
coverage: secrets-never-logged's actual scrubbing function.**

## 11. Ranked top-15 test-suite simplifications

Ranked by estimated LOC removed (structural consolidation, not behavior
change) except where noted as a quality-only fix.

| # | Sev | Finding | Fix | Est. LOC removed |
|---|---|---|---|---:|
|1|Should-fix|`redact_secrets()` (non-negotiable #3's actual mechanism) has zero test coverage — `jasper/secret_redaction.py`, used by `jasper/voice/_supervisor.py`, `jasper/cli/doctor/_shared.py`, `jasper/cli/doctor/voice.py`|Add one behavior-pin test file asserting Bearer/api_key/token/PSK/provider-key-prefix patterns get redacted and non-secret text passes through unchanged|+30 (net add, not removal — highest-priority item regardless)|
|2|Should-fix|Literal-only test clusters, 37 clusters/123 functions/996 LOC across 35 files (§7) — e.g. `tests/test_env_file.py:20-44` (4 fns), `tests/test_active_speaker_runtime_contract.py:2602-2717` (8 fns)|`@pytest.mark.parametrize` each cluster|~440|
|3|Should-fix|Web-wizard "canonical document" boilerplate hand-copied across 10 files (§3.4) — `assert out.startswith("<!doctype html>")` / `/assets/app.css?v=` / `class="app-header"` repeated per-page|One shared `assert_canonical_page(out, css_path, header_title)` helper in `tests/_web_test_helpers.py`, or one parametrized module over the 10 pages|~200|
|4|Nit|12 systemd-unit test files (4,238 combined LOC: `test_camilla_systemd_unit.py`, `test_source_intent_systemd.py`, `test_usb_mic_systemd.py`, `test_usbsink_systemd.py`, `test_bt_agent_systemd.py`, `test_enhanced_aec_systemd.py`, `test_outputd_systemd.py`, `test_control_systemd.py`, `test_correction_systemd_unit.py`, `test_aec_bridge_systemd.py`, `test_systemd_hardening.py`, `test_web_systemd.py`) each redeclare their own `UNIT_PATH`/`_value_for` plumbing on top of the already-shared `tests/systemd_unit_helpers.py` (used by only 14 files despite ~12+ systemd test files existing)|Move per-file path constants and repeated docstring boilerplate into the shared helper; keep every per-unit assertion (these are legitimate architecture contracts, don't touch the logic)|~400 (structural only)|
|5|Nit|3 single-use "shared" helper files defeat their own purpose: `tests/_ring_negotiation_model.py` (288 LOC → only `test_ring_emitter_ioplug_negotiation.py`), `tests/engine_declarations.py` (93 LOC → only `test_engine_twin.py`), `tests/_voice_runtime_text.py` (22 LOC → only `test_outputd_systemd.py`)|Inline each into its sole caller|~40 (relocates 403, net overhead removed)|
|6|Nit|5 hand-rolled `class FakeClock` shapes survive alongside the new shared `tests/fake_clock_fixtures.py` (§5) — `test_watchdog.py:80`, `test_wired_capture.py:134`, `test_active_speaker_seat_level.py:171`, `test_arm_walk.py:64`, `test_audio_measurement_ramp.py:63`|Consolidate to 2 shared protocols (callable-with-side-effect, auto-incrementing-ns) in the existing fixtures file|~60|
|7|Should-fix (quality, not size)|696 `match=` prose-like regexes across 167 files (§3) — e.g. `match='exactly equal'`, `match='disk full'`, `match='four attempts'`|Convert the highest-traffic error paths (active_speaker/crossover_v2/bass_extension refusal codes) to assert a typed exception attribute/error code instead of message text; large blast radius, don't do all 696 at once|0 (robustness fix)|
|8|Should-fix (quality, not size)|1,645 private-attribute (`_foo`) patches across 191 files (§4) — implementation-pinning; top offender `test_doctor_renderers.py` patches `_run` 58x|No blanket fix — a design conversation about which of these seams should be promoted to a public/protocol boundary vs. genuinely need to stay internal|0 (architectural)|
|9|Nit|`jasper/bass_extension/bench/excitation.py:52-76`'s `build_requested_bass_plan` has zero callers repo-wide (verified) and zero tests|Either wire it into `executor.py` (if it's the intended amplitude-derivation path) or delete it + its private helper `_amplitude_from_peak`|-25 (product code, not test code — flagged here because it surfaced via the coverage-gap check)|
|10|Nit|`tests/test_wake_corpus_recording.py` — 22 real `time.sleep()` calls (up to 0.8s), real-thread coordination|Fake-clock or condition-variable rewrite for the hottest sleeps|0 (speed/flakiness, not size)|
|11|Earns-its-keep|`tests/test_cue_registry_coverage.py`, `tests/test_xvf_host.py`, `tests/test_doctor_renderers.py:704-716` — cite as the house style for future guard tests (structured-field/argv/set pins with an explicit non-negotiable citation)|No fix — document as the pattern to imitate|0|
|12|Earns-its-keep|voice_eval paid-lane isolation (§9) — 4 independent layers, one of which (`test_voice_eval_registry.py`) is a genuinely clever hardware-free regression test for a paid-only code path|No fix|0|
|13|Nit|`tests/test_crossover_envelope_v2.py:4667,5741` — `pytest.skip("clock is within 2 h of local midnight")`|Confirm this can't silently zero out coverage in a CI run that happens to land in that window; consider freezing the clock instead of skipping|0|
|14|Nit|`jasper/correction/replay_artifacts.py` (157 LOC) — wired into production but its own derivation math untested; the one nearby test bypasses it via monkeypatching the caller|Add a direct unit test of `write_capture_replay_artifacts`'s numeric output|+small (net add)|
|15|Nit|~14 further 0-hit modules (~600 LOC: `google_errors.py`, `_unit_pair.py`, `percentiles.py`, `oauth_redirect.py`, `os_fault.py`, `text_property.py`, `curves.py`, `_numbers.py`, `_dbus.py`) not individually verified for indirect coverage|Spot-check each before assuming a gap — several may turn out covered the way round_views/handlers did|0 (needs verification, not a fix yet)|

Net LOC-removable total from items with a concrete estimate (#2-#6): **≈1,140 LOC**
(~0.2% of the test tree) — this suite's size is overwhelmingly driven by
genuine scenario breadth in the active-speaker/audio-measurement/correction
tuning program, not by copy-paste bloat. The higher-value work here is
quality (items #1, #7, #8), not shrinkage.

## Coverage

**Opened and read directly** (not just grepped): `AGENTS.md`; `CLAUDE.md`;
`pyproject.toml` (`[tool.pytest.ini_options]`, optional-deps); `tests/conftest.py`;
`tests/voice_eval/conftest.py`; `tests/test_outputd_wiring.py` (head);
`tests/test_web_design_system.py` (head); `tests/test_camilla_systemd_unit.py`
(head + 2 tests); `tests/test_launch_blocker_docs_exist.py` (full);
`tests/test_ci_classifier.py` (head + one parametrized test);
`tests/test_xvf_host.py` (head); `tests/test_aec_probe_xvf_ref_level_script.py`
(2 tests); `tests/test_cue_registry_coverage.py` (head);
`tests/test_doctor_renderers.py` (~60 lines around the argv pin + fixture
helpers); `tests/test_env_file.py` (the cluster); `tests/test_active_speaker_runtime_contract.py`
(the 8-cluster); `tests/_web_test_helpers.py` (full); `tests/test_web_transit_setup.py`
and `tests/test_web_spotify_setup.py` (the duplicated boilerplate);
`jasper/active_speaker/__init__.py` (full, the PEP 562 lazy-export pattern);
`jasper/secret_redaction.py` (full); `jasper/bass_extension/bench/excitation.py`
(full); `jasper/correction/replay_artifacts.py` (head);
`jasper/cli/round_views/__init__.py` (head); `jasper/control/__init__.py`
(grepped for lazy pattern, none found); `docs/adr/0236-*.md` (head, to
verify the in-flight subwoofer-deletion claim);  `scripts/test-fast` (routing
table section); `scripts/test-merge` (the `--ignore` line);
`.github/workflows/tests.yml` (the `ci` job's Test step); `git show --stat`
for PR #4041.

**Computed mechanically, not manually reviewed line-by-line**: all §1 size
tables (AST-derived); the 209-file/857-line guard-hit list (9 files opened
as evidence, 200 not individually read — filename-bucketed only); the
696/690/655 prose-pin counts (top ~15 per category read, the long tail not
individually judged); the 1,645 private-attribute-patch occurrences (top 15
files' target names read, not their surrounding test logic); the 71→28
coverage-gap funnel (28 checked by name-grep, ~14 of those individually
verified for false-positiveness, ~14 left as "needs a look").

**Not done**: an actual `pytest --collect-only` run (blocked — see header;
the sandboxed proxy returns 403 on the `camilladsp` GitHub archive tarball
pinned in `pyproject.toml`'s `streambox` extra, and no cached wheel exists).
All test/case counts are therefore static AST lower bounds, not pytest's
real collected-item count — parametrize decorators with dynamic argvalues
(243 of them) are not resolved, so the true collected-test count is
somewhat higher than the ~24,308 static floor reported in §1. Did not open
the 315 unclassified guard-hit lines/136 files in §2, the long tail of §3's
prose-pin files, or the ~14 unverified §10 candidates individually — flagged
explicitly wherever that applies rather than asserting confidence I don't
have. Did not run `rust/`, `c/`, or `.github/`-side dead-code checks for the
one product-code finding that surfaced (`build_requested_bass_plan`) beyond
a repo-wide grep across `jasper/`, `tests/`, `scripts/`, `deploy/`, `docs/`.
