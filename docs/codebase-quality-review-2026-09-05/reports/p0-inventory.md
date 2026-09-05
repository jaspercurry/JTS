# Phase 0 Cartography: Inventory and Hotspots

JTS @ `2d571e6b8` (branch `claude/codebase-quality-review-do78rn`). Mechanical, ast-based (no radon/lizard —
sandbox has no network access to fetch them; `uv pip install` failed the same way, verified `git status` stayed
clean). CC = McCabe (decision points + 1, computed from `ast`, nested funcs counted separately). LOC = code lines
(blank/comment/pure-whitespace tokens excluded; Python via `tokenize`, others via a strip heuristic).

## 1. Inventory

### Per top-level dir

| dir | files | LOC |
|---|---:|---:|
| tests | 1031 | 493,540 |
| jasper | 769 | 353,900 |
| docs | 297 | 67,717 |
| deploy | 244 | 46,207 |
| rust | 59 | 40,678 |
| scripts | 97 | 22,686 |
| c | 8 | 4,987 |
| experiments | 16 | 2,519 |
| .github | 8 | 866 |
| jasper_aec3 | 7 | 765 |
| .claude | 4 | 415 |
| wake_training | 2 | 358 |
| release | 2 | 328 |
| LICENSES | 4 | 308 |
| LICENSE | 1 | 169 |
| NOTICE | 1 | 7 |
| logs | 1 | 0 |

Root markdown/doc files (BRINGUP.md 974, QUICKSTART.md 391, etc.) omitted above; see raw JSON for the full list.

### Per `jasper/<package>` (+ top-level `jasper/*.py` modules as one bucket)

Test LOC = heuristic match on `tests/test_<pkg>*.py` / `tests/test_<topmodule>*.py` stems.

| package | files | LOC | test LOC (n files) | ratio |
|---|---:|---:|---:|---:|
| jasper/active_speaker | 176 | 117,315 | 75,276 (95) | 0.64 |
| jasper/(top-level modules) | 117 | 47,751 | 63,674 (159) | 1.33 |
| jasper/cli | 95 | 39,197 | 2,659 (7) | 0.07 |
| jasper/web | 41 | 37,487 | 16,148 (32) | 0.43 |
| jasper/audio_measurement | 47 | 21,096 | 18,417 (26) | 0.87 |
| jasper/control | 37 | 15,775 | 7,987 (13) | 0.51 |
| jasper/correction | 28 | 12,050 | 27,042 (36) | 2.24 |
| jasper/bass_extension | 26 | 8,703 | 7,965 (24) | 0.92 |
| jasper/voice | 18 | 7,015 | 9,815 (31) | 1.40 |
| jasper/multiroom | 18 | 6,302 | 8,417 (15) | 1.34 |
| jasper/calibration_agent | 25 | 5,073 | 2,060 (10) | 0.41 |
| jasper/tools | 19 | 4,573 | 4,537 (16) | 0.99 |
| jasper/wake_corpus | 4 | 4,444 | 5,116 (7) | 1.15 |
| jasper/fanin | 8 | 3,897 | 4,584 (10) | 1.18 |
| jasper/sound | 7 | 3,380 | 13,063 (16) | 3.86 |
| jasper/accessories | 10 | 2,449 | 1,119 (2) | 0.46 |
| jasper/bluetooth | 15 | 2,424 | 2,451 (9) | 1.01 |
| jasper/peering | 9 | 2,061 | 1,693 (9) | 0.82 |
| jasper/audio_hardware | 5 | 1,674 | 2,801 (1) | 1.67 |
| jasper/route_latency | 10 | 1,666 | 1,950 (9) | 1.17 |
| jasper/attribution | 8 | 1,643 | 1,997 (2) | 1.22 |
| jasper/cues | 6 | 1,397 | 905 (4) | 0.65 |
| jasper/chip_aec | 6 | 1,381 | 881 (5) | 0.64 |
| jasper/transit | 10 | 1,292 | 1,307 (5) | 1.01 |
| jasper/research | 7 | 1,106 | 963 (4) | 0.87 |
| jasper/mics | 3 | 779 | 0 (0) | 0.00 |
| jasper/data | 3 | 567 | 0 (0) | 0.00 |
| jasper/usbsink | 2 | 445 | 1,156 (5) | 2.60 |
| jasper/xvf | 2 | 384 | 546 (2) | 1.42 |
| jasper/aec_engines | 4 | 290 | 0 (0) | 0.00 |
| jasper/local_sources | 3 | 284 | 103 (1) | 0.36 |

## 2. Largest 40 source files (jasper/ rust/ c/ deploy/ scripts/)

| # | file | LOC | top-level defs+classes |
|---:|---|---:|---:|
| 1 | jasper/web/correction_crossover_v2.py | 5,440 | 144 |
| 2 | jasper/web/sound_setup.py | 4,962 | 107 |
| 3 | jasper/active_speaker/runtime_contract.py | 4,465 | 102 |
| 4 | jasper/voice_daemon.py | 4,235 | 43 |
| 5 | jasper/web/correction_setup.py | 4,135 | 141 |
| 6 | jasper/active_speaker/crossover_v2_flow.py | 3,721 | 22 |
| 7 | jasper/active_speaker/baseline_profile.py | 3,379 | 56 |
| 8 | jasper/active_speaker/camilla_yaml.py | 3,347 | 99 |
| 9 | jasper/active_speaker/commissioning_evidence.py | 3,264 | 55 |
| 10 | rust/jasper-outputd/src/state.rs | 3,060 | 120 |
| 11 | rust/jasper-fanin/src/tts.rs | 2,956 | 128 |
| 12 | c/jts-ring-ioplug/test_ring_core.c | 2,923 | - |
| 13 | rust/jasper-fanin/src/mixer.rs | 2,805 | 163 |
| 14 | jasper/active_speaker/crossover_envelope_v2.py | 2,781 | 88 |
| 15 | jasper/active_speaker/crossover_v2/evidence_packet.py | 2,648 | 52 |
| 16 | jasper/control/audio_health.py | 2,645 | 51 |
| 17 | jasper/wake_corpus/bridge_session.py | 2,518 | 66 |
| 18 | rust/jasper-host-clock/src/lib.rs | 2,502 | 165 |
| 19 | rust/jasper-fanin/src/lane_resampler.rs | 2,448 | 137 |
| 20 | jasper/volume_coordinator.py | 2,371 | 12 |
| 21 | jasper/active_speaker/driver_safety.py | 2,362 | 52 |
| 22 | jasper/correction/session.py | 2,147 | 13 |
| 23 | rust/jasper-fanin/src/config.rs | 2,119 | 95 |
| 24 | jasper/output_topology.py | 2,049 | 61 |
| 25 | jasper/audio_validation.py | 2,029 | 77 |
| 26 | jasper/active_speaker/web_commissioning.py | 2,011 | 58 |
| 27 | jasper/multiroom/reconcile.py | 1,997 | 41 |
| 28 | jasper/active_speaker/staging.py | 1,968 | 33 |
| 29 | jasper/active_speaker/commissioning_run.py | 1,965 | 33 |
| 30 | jasper/active_speaker/crossover_v2/spatial.py | 1,932 | 54 |
| 31 | jasper/control/server.py | 1,929 | 83 |
| 32 | rust/jasper-fanin/src/state.rs | 1,922 | 69 |
| 33 | jasper/active_speaker/crossover_v2/feature_classifier.py | 1,916 | 49 |
| 34 | rust/jasper-outputd/src/main.rs | 1,843 | 93 |
| 35 | jasper/audio_runtime_plan.py | 1,781 | 52 |
| 36 | rust/jasper-ring/src/lib.rs | 1,779 | 118 |
| 37 | jasper/mux.py | 1,771 | 14 |
| 38 | rust/jasper-outputd/src/config.rs | 1,755 | 86 |
| 39 | jasper/wake_corpus/recording_backend.py | 1,753 | 7 |
| 40 | rust/jasper-tts-protocol/src/loudness.rs | 1,753 | 114 |

## 3. Longest 40 functions/methods

Total functions/methods parsed: Python via `ast` (incl. nested) + Rust via string/comment-aware brace
counting. Count > 100 LOC: **746**. Count > 200 LOC: **158**.

| # | LOC | lang | file:line | name |
|---:|---:|---|---|---|
| 1 | 994 | py | jasper/web/correction_setup.py:3902 | _make_handler |
| 2 | 985 | py | jasper/active_speaker/baseline_profile.py:2025 | build_baseline_profile_candidate |
| 3 | 959 | rs | rust/jasper-outputd/src/state.rs:931 | snapshot_json |
| 4 | 926 | py | jasper/web/correction_crossover_v2.py:5471 | prepare_v2_session |
| 5 | 864 | py | jasper/multiroom/reconcile.py:1596 | main |
| 6 | 836 | py | jasper/active_speaker/crossover_v2/intervention.py:792 | plan_linearization |
| 7 | 768 | py | jasper/active_speaker/runtime_contract.py:2515 | _active_graph_evidence |
| 8 | 741 | py | jasper/web/sound_setup.py:4702 | _make_handler |
| 9 | 708 | py | jasper/cli/aec_bridge.py:351 | _aec_loop |
| 10 | 643 | py | jasper/voice/daemon_main.py:648 | run |
| 11 | 633 | py | jasper/web/sound_setup.py:4808 | Handler.do_POST |
| 12 | 619 | py | jasper/web/correction_crossover_v2_wired.py:501 | build_v2_wired_run_and_consume |
| 13 | 562 | py | jasper/active_speaker/crossover_v2/durable_state.py:1030 | build_conductor_state |
| 14 | 557 | py | jasper/control/server.py:1451 | _make_handler |
| 15 | 534 | py | jasper/active_speaker/web_measurement.py:849 | record_driver_capture |
| 16 | 534 | rs | rust/jasper-outputd/src/config.rs:301 | from_env |
| 17 | 525 | rs | rust/jasper-fanin/src/config.rs:607 | from_env |
| 18 | 524 | py | jasper/web/correction_crossover_v2_wired.py:594 | _run_and_consume |
| 19 | 498 | py | jasper/config.py:461 | Config.from_env |
| 20 | 490 | py | jasper/active_speaker/setup_status.py:726 | read_active_speaker_setup_status |
| 21 | 478 | py | jasper/web/tools_setup.py:378 | _make_handler |
| 22 | 472 | py | jasper/active_speaker/commission_load.py:661 | load_driver_commissioning_config |
| 23 | 468 | py | jasper/active_speaker/seat_level_ramp.py:1229 | _walk_to_the_band |
| 24 | 457 | py | jasper/active_speaker/crossover_envelope_v2.py:2444 | build_crossover_envelope_v2 |
| 25 | 446 | py | jasper/audio_measurement/spatial_combine.py:805 | detect_echo |
| 26 | 437 | py | jasper/web/correction_crossover_v2.py:3505 | bind_production_play |
| 27 | 434 | py | jasper/active_speaker/commission_ramp.py:434 | ramp_audible_step |
| 28 | 428 | py | jasper/web/spotify_setup.py:938 | _make_handler |
| 29 | 420 | py | jasper/active_speaker/baseline_profile.py:793 | _derive_corrections |
| 30 | 420 | py | jasper/tools/spotify.py:412 | make_spotify_tools |
| 31 | 402 | py | jasper/active_speaker/delta_probe.py:763 | classify_delta_probe |
| 32 | 401 | py | jasper/web/correction_crossover_v2.py:6457 | handle_v2_apply |
| 33 | 399 | py | jasper/web/correction_setup.py:4017 | Handler._dispatch_crossover |
| 34 | 396 | py | jasper/active_speaker/commissioning_coordinator.py:581 | build_commissioning_view |
| 35 | 394 | py | jasper/active_speaker/web_commissioning.py:1645 | play_driver_capture_sweep |
| 36 | 393 | py | jasper/active_speaker/crossover_v2_flow.py:885 | CrossoverV2Session.__init__ |
| 37 | 385 | rs | rust/jasper-outputd/src/main.rs:358 | run_alsa |
| 38 | 384 | py | jasper/web/voice_setup.py:1328 | _make_handler |
| 39 | 384 | rs | rust/jasper-fanin/src/main.rs:141 | run |
| 40 | 382 | rs | rust/jasper-fanin/src/state.rs:779 | push_inputs_json |

## 4. Cyclomatic complexity — top 40 (Python only; ast-based McCabe)

CC > 15: **998** functions. CC > 25: **316** functions.

| # | CC | LOC | file:line | name | test? |
|---:|---:|---:|---|---|:---:|
| 1 | 156 | 994 | jasper/web/correction_setup.py:3902 | _make_handler |  |
| 2 | 150 | 768 | jasper/active_speaker/runtime_contract.py:2515 | _active_graph_evidence |  |
| 3 | 138 | 985 | jasper/active_speaker/baseline_profile.py:2025 | build_baseline_profile_candidate |  |
| 4 | 111 | 123 | tests/test_sound_setup.py:1402 | test_sound_module_output_topology_surface_is_no_audio_and_backend_owned | T |
| 5 | 110 | 864 | jasper/multiroom/reconcile.py:1596 | main |  |
| 6 | 110 | 145 | tests/test_sound_setup.py:1255 | test_sound_module_active_speaker_status_is_explicit_read_only | T |
| 7 | 107 | 741 | jasper/web/sound_setup.py:4702 | _make_handler |  |
| 8 | 105 | 396 | jasper/active_speaker/commissioning_coordinator.py:581 | build_commissioning_view |  |
| 9 | 97 | 350 | scripts/analyze-correction-diagnostic.py:118 | main |  |
| 10 | 96 | 708 | jasper/cli/aec_bridge.py:351 | _aec_loop |  |
| 11 | 96 | 633 | jasper/web/sound_setup.py:4808 | Handler.do_POST |  |
| 12 | 88 | 326 | jasper/audio_profile_state.py:551 | build_audio_profile_status |  |
| 13 | 87 | 226 | jasper/active_speaker/crossover_contract.py:174 | summed_decision_evidence_state |  |
| 14 | 85 | 420 | jasper/active_speaker/baseline_profile.py:793 | _derive_corrections |  |
| 15 | 85 | 229 | jasper/calibration_agent/cli.py:30 | render_markdown |  |
| 16 | 82 | 836 | jasper/active_speaker/crossover_v2/intervention.py:792 | plan_linearization |  |
| 17 | 78 | 490 | jasper/active_speaker/setup_status.py:726 | read_active_speaker_setup_status |  |
| 18 | 77 | 277 | scripts/_analyze_three_leg.py:104 | main |  |
| 19 | 76 | 478 | jasper/web/tools_setup.py:378 | _make_handler |  |
| 20 | 76 | 247 | scripts/_audit_wake_corpus.py:225 | audit |  |
| 21 | 76 | 258 | scripts/_first_party_arm64_release.py:1313 | validate_build_info |  |
| 22 | 73 | 246 | jasper/usb_mic.py:255 | build_usb_mic_status |  |
| 23 | 71 | 562 | jasper/active_speaker/crossover_v2/durable_state.py:1030 | build_conductor_state |  |
| 24 | 71 | 322 | jasper/web/bluetooth_setup.py:519 | _make_handler |  |
| 25 | 71 | 189 | jasper/web/correction_crossover_backend.py:734 | CrossoverLevelLease.set_durable_repeat_progress |  |
| 26 | 70 | 254 | jasper/active_speaker/staging.py:1038 | _bind_preset_to_topology |  |
| 27 | 70 | 217 | jasper/bass_extension/limiter_evidence.py:694 | _parse_candidate |  |
| 28 | 68 | 926 | jasper/web/correction_crossover_v2.py:5471 | prepare_v2_session |  |
| 29 | 68 | 401 | jasper/web/correction_crossover_v2.py:6457 | handle_v2_apply |  |
| 30 | 68 | 619 | jasper/web/correction_crossover_v2_wired.py:501 | build_v2_wired_run_and_consume |  |
| 31 | 67 | 524 | jasper/web/correction_crossover_v2_wired.py:594 | _run_and_consume |  |
| 32 | 66 | 314 | jasper/web/correction_crossover_v2.py:1437 | _post_apply_grade |  |
| 33 | 65 | 534 | jasper/active_speaker/web_measurement.py:849 | record_driver_capture |  |
| 34 | 64 | 192 | jasper/active_speaker/commissioning_receipt.py:1579 | CommissioningEligibilityReceipt.__post_init__ |  |
| 35 | 63 | 292 | jasper/control/airplay_health.py:896 | AirPlayHealthSampler._sample_fanin |  |
| 36 | 62 | 133 | jasper/bass_extension/bench/manifest.py:172 | _read_request |  |
| 37 | 62 | 299 | jasper/web/correction_setup.py:4595 | Handler.do_POST |  |
| 38 | 62 | 428 | jasper/web/spotify_setup.py:938 | _make_handler |  |
| 39 | 59 | 150 | jasper/active_speaker/graph_safety.py:807 | bass_extension_block_valid |  |
| 40 | 59 | 138 | tests/test_active_speaker_commissioning_capture.py:2310 | test_build_proposal_rejects_unadmitted_summed_decision_evidence | T |

## 5. Nesting depth >= 5 (Python, control-flow blocks only; nested funcs counted separately)

Functions with max nesting >= 5: **137**. Worst 40 shown:

| # | depth | file:line | name |
|---:|---:|---|---|
| 1 | 16 | jasper/active_speaker/crossover_envelope_v2.py:2444 | build_crossover_envelope_v2 |
| 2 | 16 | tests/test_active_speaker_commissioning_capture.py:2310 | test_build_proposal_rejects_unadmitted_summed_decision_evidence |
| 3 | 10 | jasper/active_speaker/crossover_contract.py:434 | crossover_snapshot_state |
| 4 | 10 | tests/test_wire_contracts.py:99 | _parse_rust_emitter |
| 5 | 9 | jasper/audio_profile_state.py:551 | build_audio_profile_status |
| 6 | 9 | jasper/fanin/latency_mode.py:252 | read_state |
| 7 | 9 | jasper/mux.py:1478 | Mux._handle_control_client |
| 8 | 9 | jasper/voice/daemon_main.py:585 | handle |
| 9 | 8 | jasper/active_speaker/calibration_level.py:225 | update_calibration_level_state |
| 10 | 8 | jasper/active_speaker/commissioning_service.py:1011 | CommissioningCaptureService.status |
| 11 | 8 | jasper/active_speaker/runtime_contract.py:521 | classify_output_contract |
| 12 | 8 | jasper/multiroom/reconcile.py:1596 | main |
| 13 | 8 | scripts/capture-correction-diagnostic.py:158 | main |
| 14 | 8 | tests/test_active_speaker_measurement.py:1295 | test_malformed_repeat_summary_is_rejected_before_state_write |
| 15 | 8 | tests/test_env_example_matches_config_defaults.py:173 | test_env_example_literal_matches_config_default |
| 16 | 7 | jasper/accessories/bridge.py:536 | _read_device |
| 17 | 7 | jasper/active_speaker/baseline_profile.py:793 | _derive_corrections |
| 18 | 7 | jasper/cli/usb_mic.py:1039 | run_relay |
| 19 | 7 | jasper/fanin/latency_mode.py:197 | classify_runtime |
| 20 | 7 | jasper/log_event.py:56 | _escape_logfmt_text |
| 21 | 7 | jasper/usb_mic.py:255 | build_usb_mic_status |
| 22 | 7 | scripts/run-crossover-round.py:1339 | main |
| 23 | 7 | tests/test_correction_variance_cap.py:118 | _sweep_cases |
| 24 | 6 | jasper/active_speaker/commissioning_evidence_store.py:594 | CommissioningEvidenceStore._authoritative_total |
| 25 | 6 | jasper/active_speaker/commissioning_receipt.py:666 | CommissioningRollbackEvidence.__post_init__ |
| 26 | 6 | jasper/active_speaker/crossover_v2/sweep_spec.py:590 | _validate_screen |
| 27 | 6 | jasper/active_speaker/environment.py:315 | classify_camilla_config_text |
| 28 | 6 | jasper/active_speaker/setup_status.py:157 | _acoustic_commissioning_status |
| 29 | 6 | jasper/audio_measurement/ramp.py:1092 | RampController._tick_state |
| 30 | 6 | jasper/bass_extension/bench/derivation.py:521 | owner_path_stages |
| 31 | 6 | jasper/calibration_agent/cli.py:402 | main |
| 32 | 6 | jasper/cli/aec_bridge.py:351 | _aec_loop |
| 33 | 6 | jasper/cli/doctor/memory.py:97 | check_ram |
| 34 | 6 | jasper/cli/doctor/memory.py:464 | _bounded_dir_size |
| 35 | 6 | jasper/cli/xvf_firmware_update.py:175 | _download_and_verify |
| 36 | 6 | jasper/control/audio_health.py:2114 | compose_audio_health |
| 37 | 6 | jasper/dsp_apply.py:842 | apply_dsp_config |
| 38 | 6 | jasper/enhanced_aec.py:449 | status |
| 39 | 6 | jasper/mics/xvf3800.py:674 | firmware_update_status |
| 40 | 6 | jasper/peering/uds.py:78 | handle |

## 6. Python import graph over jasper/ (module-level + function-local `import jasper...`/`from jasper...`)

447 jasper modules have >=1 internal jasper-import edge (1835 edges total).

### Highest fan-in (most imported)

| module | fan-in |
|---|---:|
| jasper.log_event | 140 |
| jasper.atomic_io | 69 |
| jasper.output_topology | 62 |
| jasper.audio_measurement.evidence_identity | 35 |
| jasper.camilla_config_contract | 26 |
| jasper.audio_measurement | 26 |
| jasper.audio_measurement.program | 25 |
| jasper.dsp_apply | 25 |
| jasper.fanin_coupling | 25 |
| jasper.web | 25 |
| jasper.active_speaker.runtime_contract | 24 |
| jasper.camilla | 23 |
| jasper.sound.profile | 22 |
| jasper.audio_measurement.program_analysis | 22 |
| jasper.active_speaker.baseline_profile | 21 |
| jasper.audio_measurement.calibration | 21 |
| jasper.env_load | 21 |
| jasper.active_speaker.camilla_yaml | 18 |
| jasper.audio_measurement.analysis | 18 |
| jasper.json_fields | 17 |

### Highest fan-out (imports the most jasper-internal modules)

| module | fan-out |
|---|---:|
| jasper.web.correction_crossover_v2 | 69 |
| jasper.web.sound_setup | 49 |
| jasper.web.correction_setup | 37 |
| jasper.active_speaker.web_commissioning | 34 |
| jasper.active_speaker.crossover_v2_flow | 32 |
| jasper.cli.measure | 32 |
| jasper.cli.null_door | 30 |
| jasper.web.correction_crossover_backend | 28 |
| jasper.active_speaker.crossover_v2.round_views | 26 |
| jasper.cli.active_speaker | 25 |
| jasper.cli.seat_level | 22 |
| jasper.fanin.coupling_reconcile | 18 |
| jasper.sound.graph_carrier | 18 |
| jasper.active_speaker.runtime_contract | 17 |
| jasper.cli.aec_commission | 17 |
| jasper.active_speaker.audition | 16 |
| jasper.wake_corpus.bridge_session | 16 |
| jasper.active_speaker.crossover_v2.conductor_context | 15 |
| jasper.active_speaker.web_measurement | 14 |
| jasper.cli.doctor.usbsink | 14 |

### Import cycles (Tarjan SCC, size > 1): **5**

1. **2 modules** — `jasper.source_intent`, `jasper.accessories.reconcile`
2. **2 modules** — `jasper.bass_extension.targets`, `jasper.bass_extension.adapters.base`
3. **34 modules** — `jasper.multiroom.leader_config`, `jasper.sound.profile`, `jasper.sound.graph_carrier`, `jasper.camilla_stereo_prefix`, `jasper.sound.camilla_yaml`, `jasper.sound.runtime`, `jasper.active_speaker.runtime_convergence`, `jasper.output_topology_runtime`, … (+26 more)
4. **2 modules** — `jasper.active_speaker.crossover_v2_flow`, `jasper.active_speaker.attempts_loop`
5. **2 modules** — `jasper.cli.aec_bridge_corpus_lanes`, `jasper.cli.aec_bridge`

Self-loops (module importing itself via alias path): 0

### Function-local jasper-imports (deferred, often to dodge the cycles above): **1287** total

| module | local-import count |
|---|---:|
| jasper.web.correction_crossover_v2 | 143 |
| jasper.web.correction_setup | 141 |
| jasper.web.sound_setup | 118 |
| jasper.web.correction_crossover_backend | 48 |
| jasper.cli.null_door | 40 |
| jasper.cli.measure | 37 |
| jasper.sound.graph_carrier | 25 |
| jasper.active_speaker.web_commissioning | 24 |
| jasper.active_speaker.web_measurement | 24 |
| jasper.active_speaker.runtime_contract | 23 |
| jasper.fanin.ring_health | 23 |
| jasper.multiroom.follower_config | 21 |
| jasper.web.__main__ | 20 |
| jasper.cli.seat_level | 19 |
| jasper.bass_extension | 18 |

## 7. God-module candidates

### Files > 1500 LOC with > 30 top-level defs/classes

| file | LOC | defs+classes |
|---|---:|---:|
| jasper/web/correction_crossover_v2.py | 5,440 | 144 |
| jasper/web/sound_setup.py | 4,962 | 107 |
| jasper/active_speaker/runtime_contract.py | 4,465 | 102 |
| jasper/voice_daemon.py | 4,235 | 43 |
| jasper/web/correction_setup.py | 4,135 | 141 |
| jasper/active_speaker/baseline_profile.py | 3,379 | 56 |
| jasper/active_speaker/camilla_yaml.py | 3,347 | 99 |
| jasper/active_speaker/commissioning_evidence.py | 3,264 | 55 |
| jasper/active_speaker/crossover_envelope_v2.py | 2,781 | 88 |
| jasper/active_speaker/crossover_v2/evidence_packet.py | 2,648 | 52 |
| jasper/control/audio_health.py | 2,645 | 51 |
| jasper/wake_corpus/bridge_session.py | 2,518 | 66 |
| jasper/active_speaker/driver_safety.py | 2,362 | 52 |
| jasper/output_topology.py | 2,049 | 61 |
| jasper/audio_validation.py | 2,029 | 77 |
| jasper/active_speaker/web_commissioning.py | 2,011 | 58 |
| jasper/multiroom/reconcile.py | 1,997 | 41 |
| jasper/active_speaker/staging.py | 1,968 | 33 |
| jasper/active_speaker/commissioning_run.py | 1,965 | 33 |
| jasper/active_speaker/crossover_v2/spatial.py | 1,932 | 54 |
| jasper/control/server.py | 1,929 | 83 |
| jasper/active_speaker/crossover_v2/feature_classifier.py | 1,916 | 49 |
| jasper/audio_runtime_plan.py | 1,781 | 52 |
| jasper/fanin/coupling_reconcile.py | 1,750 | 37 |
| jasper/active_speaker/crossover_v2/round_views.py | 1,718 | 47 |
| scripts/_first_party_arm64_release.py | 1,650 | 41 |
| jasper/active_speaker/crossover_v2/verification.py | 1,622 | 37 |
| jasper/cli/doctor/audio.py | 1,622 | 41 |
| jasper/source_intent.py | 1,582 | 57 |
| jasper/web/voice_setup.py | 1,573 | 42 |
| jasper/active_speaker/measurement.py | 1,562 | 54 |
| jasper/active_speaker/crossover_v2/capture_plan.py | 1,544 | 47 |
| jasper/cli/doctor/aec.py | 1,539 | 41 |
| jasper/active_speaker/linearization_fit.py | 1,523 | 41 |

### Modules imported by > 25 others (jasper-internal fan-in)

| module | fan-in |
|---|---:|
| jasper.log_event | 140 |
| jasper.atomic_io | 69 |
| jasper.output_topology | 62 |
| jasper.audio_measurement.evidence_identity | 35 |
| jasper.camilla_config_contract | 26 |
| jasper.audio_measurement | 26 |

## 8. Staleness, churn, hotspots

Whole visible git history: **1140 commits**, 2026-09-02 to 2026-09-05. The brief's ~30-day assumption overstates
it: actual span is ~3 days, i.e. ~380 commits/day, consistent with `PR numbers > 4100` from AI-agent-driven
development at very high velocity.

### Staleness distribution (days since last commit touching the file, all tracked files)

| bucket | files |
|---|---:|
| 0 (today) | 509 |
| 1-5 | 2059 |
| 6-10 | 0 |
| 11-20 | 0 |
| 21-30 | 0 |
| >30 | 0 |
| no history | 1 |

No file among current tracked files is stale by the brief's >20-day threshold — the repo's whole history is
younger than that. 1 file has no recorded history (edge case in log parsing, e.g. touched only by an in-flight merge commit).

### Highest churn, last 30 days (== whole history here) — top 25

| # | commits | file |
|---:|---:|---|
| 1 | 50 | jasper/cli/doctor/__init__.py |
| 2 | 50 | docs/tuning-operator-runbook.md |
| 3 | 48 | pyproject.toml |
| 4 | 41 | docs/doc-map.toml |
| 5 | 40 | deploy/lib/install/systemd-units.sh |
| 6 | 39 | jasper/control/state_aggregate.py |
| 7 | 38 | jasper/cli/doctor/aec.py |
| 8 | 37 | jasper/web/correction_crossover_v2.py |
| 9 | 36 | jasper/voice/gemini_session.py |
| 10 | 36 | jasper/voice/openai_session.py |
| 11 | 35 | tests/test_doctor_aec.py |
| 12 | 35 | jasper/web/sound_setup.py |
| 13 | 34 | jasper/voice/_supervisor.py |
| 14 | 34 | jasper/voice_daemon.py |
| 15 | 34 | tests/test_sound_setup.py |
| 16 | 33 | tests/test_doctor_core.py |
| 17 | 33 | jasper/cli/doctor/grouping.py |
| 18 | 33 | jasper/control/server.py |
| 19 | 32 | tests/test_install_helpers.py |
| 20 | 32 | jasper/cli/aec_commission.py |
| 21 | 32 | tests/test_openai_session.py |
| 22 | 32 | tests/test_ring_active_endpoint.py |
| 23 | 32 | tests/test_active_speaker_crossover_v2_round_views.py |
| 24 | 32 | docs/testing-tooling.md |
| 25 | 32 | jasper/camilla_config_contract.py |

### Tornhill hotspots (churn × file complexity-sum) — top 25, all files

File complexity = sum of per-function ast-CC in that file (Python only; churn = commits in visible history).

| # | score | churn | Σcc | file |
|---:|---:|---:|---:|---|
| 1 | 54,332 | 34 | 1598 | tests/test_sound_setup.py |
| 2 | 39,122 | 31 | 1262 | tests/test_correction_crossover_v2_endpoints.py |
| 3 | 30,288 | 24 | 1262 | tests/test_crossover_envelope_v2.py |
| 4 | 29,489 | 37 | 797 | jasper/web/correction_crossover_v2.py |
| 5 | 28,490 | 35 | 814 | jasper/web/sound_setup.py |
| 6 | 26,316 | 34 | 774 | jasper/voice_daemon.py |
| 7 | 26,226 | 31 | 846 | jasper/web/correction_setup.py |
| 8 | 25,420 | 20 | 1271 | tests/test_audio_measurement_program_analysis.py |
| 9 | 22,352 | 22 | 1016 | tests/test_active_speaker_baseline_profile.py |
| 10 | 20,748 | 28 | 741 | tests/test_aec_reconcile.py |
| 11 | 20,660 | 20 | 1033 | jasper/active_speaker/runtime_contract.py |
| 12 | 20,277 | 27 | 751 | tests/test_correction_setup.py |
| 13 | 20,000 | 20 | 1000 | tests/test_active_speaker_seat_level.py |
| 14 | 19,950 | 30 | 665 | tests/test_audio_hardware_reconcile.py |
| 15 | 19,360 | 22 | 880 | tests/test_multiroom_reconcile.py |
| 16 | 19,125 | 25 | 765 | tests/test_audio_health.py |
| 17 | 19,104 | 32 | 597 | tests/test_install_helpers.py |
| 18 | 18,784 | 32 | 587 | tests/test_active_speaker_crossover_v2_round_views.py |
| 19 | 18,240 | 32 | 570 | tests/test_ring_active_endpoint.py |
| 20 | 17,736 | 24 | 739 | tests/test_web_rooms_setup.py |
| 21 | 17,424 | 18 | 968 | tests/test_active_speaker_runtime_contract.py |
| 22 | 16,986 | 19 | 894 | tests/test_spatial_combine.py |
| 23 | 16,169 | 19 | 851 | tests/test_crossover_v2_driver_prescription.py |
| 24 | 16,095 | 29 | 555 | jasper/active_speaker/crossover_v2_flow.py |
| 25 | 15,456 | 32 | 483 | tests/test_openai_session.py |

### Same, production code only (tests excluded) — top 25

| # | score | churn | Σcc | file |
|---:|---:|---:|---:|---|
| 1 | 29,489 | 37 | 797 | jasper/web/correction_crossover_v2.py |
| 2 | 28,490 | 35 | 814 | jasper/web/sound_setup.py |
| 3 | 26,316 | 34 | 774 | jasper/voice_daemon.py |
| 4 | 26,226 | 31 | 846 | jasper/web/correction_setup.py |
| 5 | 20,660 | 20 | 1033 | jasper/active_speaker/runtime_contract.py |
| 6 | 16,095 | 29 | 555 | jasper/active_speaker/crossover_v2_flow.py |
| 7 | 14,993 | 29 | 517 | jasper/active_speaker/crossover_envelope_v2.py |
| 8 | 14,336 | 28 | 512 | jasper/control/audio_health.py |
| 9 | 13,630 | 29 | 470 | jasper/audio_validation.py |
| 10 | 13,020 | 21 | 620 | jasper/active_speaker/baseline_profile.py |
| 11 | 12,275 | 25 | 491 | jasper/active_speaker/camilla_yaml.py |
| 12 | 12,240 | 36 | 340 | jasper/voice/openai_session.py |
| 13 | 11,666 | 19 | 614 | jasper/active_speaker/commissioning_evidence.py |
| 14 | 11,362 | 38 | 299 | jasper/cli/doctor/aec.py |
| 15 | 10,989 | 33 | 333 | jasper/control/server.py |
| 16 | 10,912 | 22 | 496 | jasper/active_speaker/driver_safety.py |
| 17 | 10,850 | 25 | 434 | jasper/wake_corpus/bridge_session.py |
| 18 | 10,071 | 27 | 373 | jasper/web/rooms_setup.py |
| 19 | 9,269 | 23 | 403 | jasper/output_topology.py |
| 20 | 9,100 | 26 | 350 | jasper/control/airplay_health.py |
| 21 | 8,773 | 31 | 283 | jasper/cli/doctor/audio.py |
| 22 | 8,316 | 27 | 308 | jasper/mux.py |
| 23 | 8,250 | 22 | 375 | jasper/web/correction_crossover_backend.py |
| 24 | 8,165 | 23 | 355 | jasper/active_speaker/staging.py |
| 25 | 8,100 | 36 | 225 | jasper/voice/gemini_session.py |

## 9. Class counts per jasper package

| package | classes | dataclasses |
|---|---:|---:|
| jasper/active_speaker | 437 | 288 |
| jasper/(top-level modules) | 223 | 126 |
| jasper/audio_measurement | 123 | 85 |
| jasper/cli | 82 | 40 |
| jasper/bass_extension | 80 | 52 |
| jasper/web | 62 | 16 |
| jasper/correction | 44 | 24 |
| jasper/control | 38 | 4 |
| jasper/peering | 34 | 27 |
| jasper/voice | 34 | 17 |
| jasper/accessories | 22 | 10 |
| jasper/sound | 21 | 13 |
| jasper/route_latency | 20 | 13 |
| jasper/chip_aec | 15 | 13 |
| jasper/multiroom | 15 | 11 |
| jasper/bluetooth | 14 | 3 |
| jasper/research | 13 | 6 |
| jasper/transit | 13 | 7 |
| jasper/fanin | 12 | 11 |
| jasper/tools | 12 | 9 |
| jasper/attribution | 10 | 5 |
| jasper/cues | 8 | 2 |
| jasper/audio_hardware | 7 | 7 |
| jasper/calibration_agent | 7 | 5 |
| jasper/wake_corpus | 7 | 3 |
| jasper/mics | 5 | 5 |
| jasper/xvf | 3 | 1 |
| jasper/aec_engines | 2 | 1 |
| jasper/usbsink | 2 | 0 |
| jasper/local_sources | 1 | 1 |

### Modules with > 15 classes

| file | classes |
|---|---:|
| jasper/active_speaker/crossover_v2/contracts.py | 21 |
| jasper/peering/state.py | 20 |
| jasper/active_speaker/commissioning_evidence.py | 16 |
| jasper/voice_daemon.py | 16 |
## 10. Counts

| metric | count |
|---|---:|
| Distinct `JASPER_*` tokens, all files (jasper/rust/c/deploy/scripts/tests/docs/.env.example) | 856 |
| Distinct `JASPER_*` tokens, code only (jasper/rust/c/deploy/scripts, no docs/tests) | 686 |
| Distinct `JASPER_*` read via `os.environ`/`os.getenv` in jasper/ (best proxy for real env knobs) | 127 |
| `pyproject.toml` `[project.scripts]` entry points | 57 |
| systemd units in `deploy/systemd/` (.service/.timer/.socket/.path) | 52 |
| nginx `location` blocks (`deploy/nginx*.conf`) | 95 |
| Web pages under `jasper/web/` (flat, no subdirs) | 41 |
| CLI modules under `jasper/cli/` (incl. `doctor/`, `round_views/` subpackages) | 95 (51 top-level + 44 in 2 subpackages) |
| `docs/*.md` (non-ADR) | 33 |
| `docs/adr/*.md` | 157 |
| `tests/test_*.py` | 902 (884 top-level + 18 under `tests/voice_eval/regression/`) |
| Shared test helpers (`tests/_*.py` + `*_fixtures.py`, top-level) | 25 + 20 |

Note: 856 includes a handful of non-knob matches (a doc's `JASPER_ACCEPT_*` wildcard mention, `test_env_file_lib.py`'s
synthetic `JASPER_A`/`JASPER_B`/`JASPER_C` fixture names) — counted mechanically per the brief, not hand-filtered.

## 11. Reading

Ranked by impact:

1. **34-module import cycle** in the audio-routing core (`camilla`, `output_topology`, `fanin*`, `multiroom*`,
   `sound.*`, `active_speaker.{runtime_contract,baseline_profile,staging,playback_route,...}`, `bass_extension`,
   `volume_coordinator`, `dsp_apply`, `env_load`...) — confirmed via real edges, not resolver noise (e.g.
   `jasper/camilla.py:250,819` and `jasper/output_topology.py:1660` defer imports into function bodies
   specifically to survive load order). No single owner module; changes anywhere in this ring risk rippling.
2-4. **`jasper/web/{correction_crossover_v2,sound_setup,correction_setup}.py`** (5440/4962/4135 LOC) are the repo's
   3 largest files, highest jasper-internal fan-out (69/49/37) and highest function-local-import counts
   (143/118/141) — each built around its own giant `_make_handler` closure (994/741 LOC, up to CC156, the repo's
   single highest). Same shape built 3 times independently: a candidate to converge, not just shrink.
5. **`jasper/active_speaker/runtime_contract.py`** — 4465 LOC/102 defs; `_active_graph_evidence` is CC150 (2nd
   highest in repo) and the file is a top production Tornhill hotspot (churn 20 × Σcc 1033).
6. **`jasper/active_speaker/baseline_profile.py`** — `build_baseline_profile_candidate` CC138/985 LOC (3rd highest).
7. **`jasper/active_speaker/`** as a whole — 176 files, 117k LOC (33% of all jasper/ LOC), 437 classes (3.5x the
   next-largest package) — the dominant subsystem by every size metric, at 0.64 test:code ratio.
8. **`jasper/cli/`** — 39k LOC / 95 files, worst test:code ratio of any substantial package (0.07: 2.7k test LOC).
9. **`jasper.log_event` / `jasper.atomic_io` / `jasper.output_topology`** — highest fan-in (140/69/62): true
   load-bearing core; any breaking change here has the widest blast radius in the tree.
10. **`jasper/voice_daemon.py`** — top production hotspot by churn×complexity (34 × Σcc774), 16 classes in one
    file (the wake→LLM loop, non-negotiable-adjacent), 4235 LOC across only 43 top-level defs (few, very large
    functions).

## Coverage

Opened/computed directly (this file's own scripts, `ast`-based, no radon/lizard — sandbox has no network egress
to install them, confirmed via failed `uv pip install`/`uv run` and a clean `git status` after):
- Every tracked text file for LOC (2569 files scanned; binaries/locks skipped).
- All `.py` under jasper/scripts/tests/deploy via `ast` for defs, classes, function length, CC, nesting, import
  graph (module-level + function-local, including relative imports resolved against package path).
- All `.rs` via a string/comment-aware masking pass + brace counting (raw strings `r#"..."#` mask correctly —
  verified by hand against `rust/jasper-outputd/src/state.rs` where the naive first pass had misfired).
- Full `git log --name-only` (1140 commits) for last-touched date and churn per file.
- Grepped `JASPER_*`, `[project.scripts]`, systemd units, nginx locations, web/cli/docs/test file counts directly
  against the checkout.
- Spot-verified ~8 import-cycle edges by opening the actual source lines (not just trusting the graph script).

Skipped / out of scope for Phase 0:
- Semantic review of *why* any hotspot or cycle exists, or whether it's already in-flight (issue #4030 etc. per
  brief) — that's for the findings-ranking phases, not this cartography pass.
- C files' function-length/CC (only 8 files, ~5k LOC; not asked for beyond the largest-files table).
- Bash function defs/CC in `deploy/`/`scripts/` (counted LOC only; brief's defs/CC ask was Python+Rust).
- Cross-checking every one of the 34 cycle-member modules' edges by hand (spot-checked 5 of 34; the mechanism —
  AST-walked imports incl. relative-import resolution — is the same one validated on the smaller cycles).
- radon/lizard cross-validation (unavailable; see above) — CC numbers are this script's own McCabe count, not
  radon's, so treat absolute thresholds as directional, not radon-calibrated.
