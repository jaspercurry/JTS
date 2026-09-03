# Tuning-scope census (mechanical, HEAD)

Repo: /home/user/JTS. Branch `claude/busy-goodall-mz0gvv`, rebased on
origin/main 2026-09-02 (see BRIEF.md). This is a **purely mechanical**
census — every number below comes from a small re-runnable Python script
(ast + tokenize, no judgment calls beyond the classification rules stated in
each script's docstring). Scripts live in `scratchpad/recon/census/` next to
this report; re-run any of them to reproduce a table.

## Scope definition and file counts

Scope = jasper/active_speaker/, jasper/audio_measurement/, jasper/correction/,
jasper/attribution/, jasper/calibration_agent/, jasper/web/correction_*.py +
active_speaker_flow.py + balance_*.py, the 25 tuning CLIs listed in
BRIEF.md under jasper/cli/, and experiments/usb-turntable/. Enumerated by
`census/scope_files.py` (deterministic glob, no exclusions).

```
python3 census/scope_files.py        # scope .py files, repo-relative, one per line
python3 census/scope_files.py tests  # scope's test files (see Table 10 methodology)
```

**316 Python files** in scope, by package:

| package | files |
|---|---:|
| jasper/active_speaker | 172 |
| jasper/audio_measurement | 38 |
| jasper/correction | 31 |
| jasper/cli (25 named tuning CLIs) | 25 |
| jasper/web (correction_*/active_speaker_flow/balance_*) | 19 |
| jasper/calibration_agent | 13 |
| experiments/usb-turntable | 10 |
| jasper/attribution | 8 |
| **TOTAL** | **316** |

This matches BRIEF.md's prior-analysis file/line counts closely (active_speaker
172 files/168k lines here vs. 167,841; audio_measurement 32k vs. 31,943;
correction 17k vs. 16,883; attribution 2.7k vs. 2,688) — the two analyses
agree on scope boundaries.

All 316 files parsed cleanly with `ast.parse` (zero syntax errors) at HEAD.

---

## 1. Per-file line census

### Table 1a — per-file line census (sorted by total lines desc)

| file | total | code | docstring | comment | blank | prose % |
|---|---:|---:|---:|---:|---:|---:|
| jasper/active_speaker/crossover_v2_flow.py | 7839 | 3188 | 1883 | 2170 | 598 | 51.7 |
| jasper/web/correction_crossover_v2.py | 7831 | 3865 | 1875 | 1403 | 688 | 41.86 |
| jasper/web/correction_setup.py | 7480 | 5219 | 876 | 666 | 719 | 20.61 |
| jasper/audio_measurement/program_analysis.py | 6572 | 3010 | 1519 | 1485 | 558 | 45.71 |
| jasper/active_speaker/runtime_contract.py | 5529 | 3902 | 708 | 473 | 446 | 21.36 |
| jasper/active_speaker/camilla_yaml.py | 4551 | 2767 | 813 | 559 | 412 | 30.15 |
| jasper/active_speaker/crossover_envelope_v2.py | 4417 | 1840 | 1119 | 1080 | 378 | 49.78 |
| jasper/active_speaker/baseline_profile.py | 4192 | 2711 | 598 | 607 | 276 | 28.75 |
| jasper/active_speaker/crossover_v2/evidence_packet.py | 3753 | 2097 | 847 | 512 | 297 | 36.21 |
| jasper/active_speaker/linearization_fit.py | 3567 | 1165 | 1036 | 1055 | 311 | 58.62 |
| jasper/active_speaker/commissioning_evidence.py | 3501 | 3226 | 37 | 10 | 228 | 1.34 |
| jasper/active_speaker/driver_safety.py | 3258 | 2383 | 345 | 320 | 210 | 20.41 |
| jasper/active_speaker/crossover_v2/spatial.py | 3148 | 1101 | 1308 | 395 | 344 | 54.1 |
| jasper/active_speaker/crossover_v2/capture_plan.py | 3050 | 1186 | 787 | 773 | 304 | 51.15 |
| jasper/active_speaker/web_commissioning.py | 2916 | 2371 | 154 | 159 | 232 | 10.73 |
| jasper/correction/session.py | 2881 | 1964 | 371 | 306 | 240 | 23.5 |
| jasper/active_speaker/crossover_v2/feature_classifier.py | 2599 | 1516 | 403 | 382 | 298 | 30.2 |
| jasper/active_speaker/crossover_v2/driver_prescription.py | 2508 | 1078 | 648 | 581 | 201 | 49.0 |
| jasper/active_speaker/staging.py | 2498 | 1906 | 182 | 218 | 192 | 16.01 |
| jasper/active_speaker/delta_probe.py | 2494 | 777 | 633 | 903 | 181 | 61.59 |
| jasper/active_speaker/startup_load.py | 2352 | 1951 | 85 | 177 | 139 | 11.14 |
| jasper/active_speaker/seat_level_ramp.py | 2245 | 1240 | 436 | 365 | 204 | 35.68 |
| jasper/audio_measurement/spatial_combine.py | 2228 | 843 | 653 | 524 | 208 | 52.83 |
| jasper/active_speaker/crossover_v2/durable_state.py | 2198 | 794 | 481 | 756 | 167 | 56.28 |
| jasper/active_speaker/crossover_v2/verification.py | 2186 | 1087 | 578 | 259 | 262 | 38.29 |
| jasper/audio_measurement/program.py | 2159 | 1092 | 576 | 271 | 220 | 39.23 |
| jasper/active_speaker/commissioning_run.py | 2141 | 1882 | 87 | 18 | 154 | 4.9 |
| jasper/audio_measurement/interference_nulls.py | 1900 | 702 | 486 | 538 | 174 | 53.89 |
| jasper/active_speaker/commissioning_receipt.py | 1831 | 1616 | 70 | 19 | 126 | 4.86 |
| jasper/active_speaker/crossover_v2/round_views.py | 1810 | 882 | 565 | 148 | 215 | 39.39 |
| jasper/cli/active_speaker.py | 1797 | 1390 | 144 | 132 | 131 | 15.36 |
| jasper/active_speaker/measurement.py | 1792 | 1396 | 171 | 64 | 161 | 13.11 |
| jasper/audio_measurement/ramp.py | 1752 | 1045 | 316 | 255 | 136 | 32.59 |
| jasper/web/correction_crossover_backend.py | 1748 | 1443 | 76 | 81 | 148 | 8.98 |
| jasper/active_speaker/crossover_v2/intervention.py | 1718 | 909 | 186 | 485 | 138 | 39.06 |
| jasper/active_speaker/crossover_v2/blend_prescription.py | 1688 | 799 | 399 | 329 | 161 | 43.13 |
| jasper/cli/crossover_prescriber.py | 1628 | 950 | 336 | 176 | 166 | 31.45 |
| jasper/active_speaker/crossover_v2/contracts.py | 1617 | 814 | 307 | 264 | 232 | 35.31 |
| jasper/audio_measurement/playback.py | 1611 | 1398 | 41 | 30 | 142 | 4.41 |
| jasper/active_speaker/crossover_v2/refusal_copy.py | 1578 | 596 | 273 | 618 | 91 | 56.46 |
| jasper/active_speaker/crossover_v2/sweep_spec.py | 1576 | 1030 | 195 | 212 | 139 | 25.82 |
| jasper/active_speaker/design_draft.py | 1570 | 1284 | 105 | 62 | 119 | 10.64 |
| jasper/correction/envelope.py | 1567 | 1104 | 203 | 131 | 129 | 21.31 |
| jasper/active_speaker/commissioning_capture.py | 1520 | 1052 | 283 | 76 | 109 | 23.62 |
| jasper/active_speaker/commissioning_apply.py | 1460 | 1343 | 12 | 23 | 82 | 2.4 |
| jasper/active_speaker/commissioning_evidence_store.py | 1433 | 1245 | 36 | 19 | 133 | 3.84 |
| jasper/active_speaker/crossover_v2/coordinator.py | 1410 | 517 | 350 | 401 | 142 | 53.26 |
| jasper/active_speaker/web_measurement.py | 1382 | 1159 | 74 | 45 | 104 | 8.61 |
| jasper/active_speaker/commission_ramp.py | 1366 | 1021 | 113 | 102 | 130 | 15.74 |
| jasper/active_speaker/setup_status.py | 1346 | 960 | 144 | 146 | 96 | 21.55 |
| jasper/cli/measure.py | 1321 | 841 | 213 | 124 | 143 | 25.51 |
| jasper/active_speaker/commissioning_service.py | 1317 | 1222 | 16 | 15 | 64 | 2.35 |
| jasper/active_speaker/session_volume_plan.py | 1291 | 604 | 451 | 84 | 152 | 41.44 |
| jasper/active_speaker/flat_spec.py | 1288 | 507 | 575 | 86 | 120 | 51.32 |
| jasper/active_speaker/crossover_v2/harmonic_evidence.py | 1264 | 673 | 337 | 132 | 122 | 37.1 |
| jasper/active_speaker/playback.py | 1245 | 1026 | 50 | 61 | 108 | 8.92 |
| jasper/active_speaker/commissioning_admission.py | 1238 | 1054 | 49 | 67 | 68 | 9.37 |
| jasper/audio_measurement/calibration.py | 1223 | 795 | 202 | 85 | 141 | 23.47 |
| jasper/active_speaker/commissioning_coordinator.py | 1218 | 842 | 81 | 216 | 79 | 24.38 |
| jasper/active_speaker/driver_acoustics.py | 1207 | 722 | 251 | 124 | 110 | 31.07 |
| jasper/active_speaker/graph_safety.py | 1201 | 698 | 289 | 78 | 136 | 30.56 |
| jasper/web/correction_crossover_v2_wired.py | 1186 | 618 | 283 | 178 | 107 | 38.87 |
| jasper/active_speaker/flat_spec_views.py | 1169 | 529 | 508 | 18 | 114 | 45.0 |
| jasper/active_speaker/bundles.py | 1150 | 744 | 194 | 53 | 159 | 21.48 |
| jasper/active_speaker/commissioning_runtime.py | 1145 | 967 | 27 | 34 | 117 | 5.33 |
| jasper/active_speaker/crossover_v2/gate_sweep.py | 1140 | 695 | 176 | 133 | 136 | 27.11 |
| jasper/audio_measurement/gating.py | 1128 | 432 | 372 | 196 | 128 | 50.35 |
| jasper/web/correction_crossover_v2_relay.py | 1099 | 494 | 272 | 247 | 86 | 47.22 |
| jasper/active_speaker/path_safety.py | 1092 | 915 | 47 | 39 | 91 | 7.88 |
| jasper/correction/level_match.py | 1082 | 664 | 214 | 84 | 120 | 27.54 |
| jasper/cli/active_speaker_attempts_replay.py | 1079 | 777 | 127 | 57 | 118 | 17.05 |
| jasper/active_speaker/driver_protection.py | 1061 | 456 | 236 | 248 | 121 | 45.62 |
| jasper/active_speaker/crossover_level_run.py | 1038 | 876 | 45 | 25 | 92 | 6.74 |
| jasper/active_speaker/arm_walk.py | 1036 | 574 | 204 | 117 | 141 | 30.98 |
| jasper/active_speaker/angle_capture.py | 1027 | 423 | 295 | 165 | 144 | 44.79 |
| jasper/cli/null_door.py | 1023 | 625 | 148 | 127 | 123 | 26.88 |
| jasper/active_speaker/crossover_v2/round_evidence.py | 1001 | 436 | 274 | 194 | 97 | 46.75 |
| jasper/active_speaker/excitation_safety_plan.py | 1001 | 608 | 248 | 54 | 91 | 30.17 |
| jasper/active_speaker/crossover_v2/planning.py | 996 | 374 | 295 | 254 | 73 | 55.12 |
| jasper/audio_measurement/excitation_artifacts.py | 988 | 844 | 30 | 3 | 111 | 3.34 |
| jasper/correction/coordinator.py | 987 | 590 | 166 | 154 | 77 | 32.42 |
| jasper/active_speaker/crossover_v2/topology_prescription.py | 964 | 398 | 366 | 109 | 91 | 49.27 |
| jasper/calibration_agent/response.py | 964 | 797 | 41 | 44 | 82 | 8.82 |
| jasper/active_speaker/crossover_v2/prescription_spool.py | 953 | 339 | 275 | 239 | 100 | 53.93 |
| jasper/active_speaker/crossover_v2/capture_dispatch.py | 936 | 295 | 403 | 100 | 138 | 53.74 |
| jasper/active_speaker/commissioning_isolated_producer.py | 925 | 870 | 13 | 3 | 39 | 1.73 |
| jasper/correction/strategy.py | 924 | 599 | 117 | 130 | 78 | 26.73 |
| jasper/active_speaker/attempts_loop.py | 923 | 445 | 269 | 78 | 131 | 37.59 |
| jasper/active_speaker/bench/loop.py | 923 | 558 | 170 | 88 | 107 | 27.95 |
| jasper/active_speaker/profile.py | 893 | 703 | 39 | 57 | 94 | 10.75 |
| jasper/active_speaker/measured_crossover_candidate.py | 892 | 469 | 294 | 49 | 80 | 38.45 |
| jasper/active_speaker/crossover_preview.py | 879 | 714 | 23 | 66 | 76 | 10.13 |
| jasper/active_speaker/linearization_envelope.py | 860 | 308 | 358 | 76 | 118 | 50.47 |
| jasper/audio_measurement/null_walk.py | 860 | 668 | 84 | 7 | 101 | 10.58 |
| jasper/active_speaker/branch_chain.py | 859 | 285 | 267 | 215 | 92 | 56.11 |
| jasper/web/correction_crossover_v2_status.py | 858 | 297 | 311 | 181 | 69 | 57.34 |
| jasper/active_speaker/commissioning_verification.py | 845 | 724 | 34 | 25 | 62 | 6.98 |
| jasper/active_speaker/measured_candidate.py | 840 | 762 | 16 | 3 | 59 | 2.26 |
| jasper/audio_measurement/snr_policy.py | 840 | 371 | 332 | 45 | 92 | 44.88 |
| jasper/web/balance_flow.py | 831 | 626 | 97 | 28 | 80 | 15.04 |
| jasper/audio_measurement/distortion.py | 828 | 397 | 267 | 65 | 99 | 40.1 |
| experiments/usb-turntable/jts_turntable.py | 821 | 565 | 94 | 65 | 97 | 19.37 |
| jasper/audio_measurement/excitation_admission.py | 820 | 652 | 63 | 5 | 100 | 8.29 |
| jasper/audio_measurement/analysis.py | 798 | 442 | 226 | 25 | 105 | 31.45 |
| jasper/correction/bundles.py | 790 | 672 | 37 | 21 | 60 | 7.34 |
| jasper/active_speaker/environment.py | 780 | 646 | 25 | 35 | 74 | 7.69 |
| jasper/cli/angle_capture.py | 766 | 531 | 105 | 57 | 73 | 21.15 |
| jasper/active_speaker/crossover_v2/blend_correction.py | 763 | 255 | 235 | 173 | 100 | 53.47 |
| jasper/active_speaker/crossover_v2/alignment_prescription.py | 751 | 329 | 266 | 83 | 73 | 46.47 |
| jasper/correction/confidence.py | 750 | 647 | 16 | 17 | 70 | 4.4 |
| jasper/cli/seat_level.py | 738 | 452 | 150 | 52 | 84 | 27.37 |
| jasper/audio_measurement/delay_graph.py | 734 | 594 | 62 | 7 | 71 | 9.4 |
| jasper/active_speaker/crossover_v2/session.py | 733 | 249 | 333 | 44 | 107 | 51.43 |
| jasper/active_speaker/program_admission.py | 723 | 479 | 94 | 72 | 78 | 22.96 |
| jasper/correction/runtime_integrity.py | 718 | 605 | 30 | 23 | 60 | 7.38 |
| jasper/active_speaker/capture_geometry.py | 706 | 460 | 79 | 71 | 96 | 21.25 |
| jasper/calibration_agent/correction_advisor.py | 702 | 467 | 119 | 27 | 89 | 20.8 |
| jasper/active_speaker/crossover_v2/diagnostics.py | 700 | 456 | 79 | 116 | 49 | 27.86 |
| jasper/active_speaker/branch_peak.py | 692 | 400 | 141 | 84 | 67 | 32.51 |
| jasper/cli/round_views.py | 681 | 452 | 102 | 49 | 78 | 22.17 |
| jasper/active_speaker/bench/compare.py | 675 | 286 | 223 | 72 | 94 | 43.7 |
| jasper/audio_measurement/wired_capture.py | 674 | 339 | 182 | 69 | 84 | 37.24 |
| jasper/active_speaker/crossover_v2/close_reference.py | 673 | 463 | 79 | 67 | 64 | 21.69 |
| jasper/active_speaker/audition.py | 654 | 410 | 115 | 32 | 97 | 22.48 |
| jasper/audio_measurement/admitted_playback.py | 654 | 512 | 52 | 9 | 81 | 9.33 |
| jasper/correction/evidence.py | 652 | 563 | 23 | 11 | 55 | 5.21 |
| jasper/correction/artifacts.py | 646 | 584 | 16 | 10 | 36 | 4.02 |
| experiments/usb-turntable/vendor/usb_turntable/protocol.py | 640 | 571 | 7 | 8 | 54 | 2.34 |
| jasper/active_speaker/crossover_contract.py | 640 | 538 | 40 | 10 | 52 | 7.81 |
| jasper/correction/acoustic_quality.py | 633 | 474 | 51 | 67 | 41 | 18.64 |
| jasper/active_speaker/crossover_v2/admission.py | 617 | 203 | 213 | 126 | 75 | 54.94 |
| jasper/active_speaker/crossover_v2/journey.py | 606 | 203 | 163 | 156 | 84 | 52.64 |
| jasper/active_speaker/bench/derivation.py | 605 | 351 | 152 | 16 | 86 | 27.77 |
| jasper/active_speaker/angle_capture_spool.py | 604 | 244 | 183 | 89 | 88 | 45.03 |
| jasper/active_speaker/repeat_admission.py | 604 | 445 | 52 | 47 | 60 | 16.39 |
| jasper/active_speaker/crossover_v2/delay_landscape.py | 603 | 354 | 118 | 53 | 78 | 28.36 |
| jasper/active_speaker/crossover_v2/feature_classification.py | 602 | 229 | 132 | 179 | 62 | 51.66 |
| jasper/active_speaker/graph_evidence.py | 598 | 376 | 119 | 46 | 57 | 27.59 |
| jasper/attribution/promotion.py | 597 | 198 | 164 | 185 | 50 | 58.46 |
| jasper/active_speaker/crossover_v2/position_cycle.py | 587 | 235 | 233 | 36 | 83 | 45.83 |
| jasper/audio_measurement/gate_disclosure.py | 585 | 273 | 197 | 38 | 77 | 40.17 |
| jasper/active_speaker/safe_playback.py | 577 | 479 | 22 | 3 | 73 | 4.33 |
| jasper/correction/acceptance.py | 565 | 292 | 157 | 55 | 61 | 37.52 |
| jasper/attribution/findings.py | 554 | 388 | 66 | 44 | 56 | 19.86 |
| jasper/correction/variance_cap.py | 537 | 158 | 291 | 18 | 70 | 57.54 |
| jasper/audio_measurement/evidence_identity.py | 530 | 448 | 22 | 3 | 57 | 4.72 |
| jasper/active_speaker/test_signal_plan.py | 523 | 362 | 66 | 36 | 59 | 19.5 |
| jasper/calibration_agent/cli.py | 523 | 490 | 1 | 3 | 29 | 0.76 |
| jasper/correction/bundle_tools.py | 521 | 401 | 43 | 20 | 57 | 12.09 |
| jasper/web/correction_crossover_flow.py | 521 | 356 | 67 | 45 | 53 | 21.5 |
| jasper/calibration_agent/model_client.py | 503 | 397 | 28 | 30 | 48 | 11.53 |
| jasper/active_speaker/crossover_v2/programs.py | 501 | 151 | 199 | 87 | 64 | 57.09 |
| jasper/active_speaker/crossover_v2/accountability.py | 493 | 240 | 73 | 140 | 40 | 43.2 |
| jasper/active_speaker/crossover_v2/delta_probe_run.py | 489 | 268 | 98 | 95 | 28 | 39.47 |
| jasper/active_speaker/crossover_v2/session_graph.py | 486 | 222 | 166 | 36 | 62 | 41.56 |
| jasper/calibration_agent/advisor_context.py | 483 | 408 | 27 | 13 | 35 | 8.28 |
| jasper/active_speaker/crossover_v2/commanded.py | 481 | 206 | 199 | 17 | 59 | 44.91 |
| jasper/active_speaker/__init__.py | 480 | 465 | 8 | 3 | 4 | 2.29 |
| jasper/active_speaker/crossover_v2/priors.py | 471 | 158 | 236 | 12 | 65 | 52.65 |
| jasper/attribution/position_evidence.py | 466 | 309 | 78 | 39 | 40 | 25.11 |
| jasper/cli/basic_profile.py | 465 | 316 | 57 | 35 | 57 | 19.78 |
| jasper/active_speaker/driver_base_trim.py | 463 | 220 | 128 | 66 | 49 | 41.9 |
| jasper/correction/autolevel.py | 463 | 357 | 47 | 16 | 43 | 13.61 |
| jasper/correction/status.py | 462 | 395 | 12 | 14 | 41 | 5.63 |
| jasper/active_speaker/bringup.py | 454 | 413 | 7 | 3 | 31 | 2.2 |
| jasper/audio_measurement/alignment.py | 453 | 252 | 112 | 32 | 57 | 31.79 |
| jasper/calibration_agent/actions.py | 450 | 389 | 15 | 8 | 38 | 5.11 |
| jasper/active_speaker/crossover_declaration.py | 448 | 223 | 133 | 25 | 67 | 35.27 |
| jasper/active_speaker/crossover_v2/measure_spec.py | 443 | 181 | 150 | 52 | 60 | 45.6 |
| jasper/audio_measurement/bundles.py | 443 | 303 | 63 | 10 | 67 | 16.48 |
| jasper/active_speaker/crossover_v2/operator_notes.py | 436 | 197 | 112 | 80 | 47 | 44.04 |
| jasper/web/correction_room_flow.py | 436 | 369 | 1 | 28 | 38 | 6.65 |
| jasper/active_speaker/crossover_v2/plan_assembly.py | 425 | 254 | 77 | 28 | 66 | 24.71 |
| jasper/active_speaker/crossover_alignment.py | 422 | 245 | 97 | 34 | 46 | 31.04 |
| jasper/active_speaker/capture_provenance.py | 409 | 172 | 136 | 46 | 55 | 44.5 |
| jasper/active_speaker/wizard_client.py | 408 | 211 | 104 | 41 | 52 | 35.54 |
| jasper/active_speaker/commissioning_host.py | 405 | 345 | 14 | 3 | 43 | 4.2 |
| jasper/active_speaker/crossover_v2/forward_model.py | 405 | 201 | 137 | 10 | 57 | 36.3 |
| jasper/audio_measurement/deconv.py | 403 | 236 | 89 | 25 | 53 | 28.29 |
| jasper/active_speaker/playback_route.py | 395 | 203 | 105 | 27 | 60 | 33.42 |
| jasper/active_speaker/seat_level_reference.py | 394 | 222 | 83 | 23 | 66 | 26.9 |
| jasper/active_speaker/crossover_v2/program_transaction.py | 388 | 144 | 127 | 69 | 48 | 50.52 |
| jasper/active_speaker/crossover_v2/door.py | 387 | 194 | 95 | 48 | 50 | 36.95 |
| jasper/audio_measurement/olive_metrics.py | 387 | 205 | 110 | 19 | 53 | 33.33 |
| jasper/active_speaker/crossover_v2/ring_projection.py | 385 | 203 | 103 | 22 | 57 | 32.47 |
| jasper/active_speaker/crossover_v2/round_captures.py | 383 | 242 | 72 | 17 | 52 | 23.24 |
| jasper/active_speaker/model_error_store.py | 380 | 235 | 64 | 21 | 60 | 22.37 |
| jasper/web/correction_crossover_v2_republish.py | 374 | 158 | 107 | 77 | 32 | 49.2 |
| jasper/active_speaker/crossover_eligibility.py | 371 | 256 | 26 | 48 | 41 | 19.95 |
| jasper/audio_measurement/correction_lane.py | 369 | 53 | 228 | 46 | 42 | 74.25 |
| jasper/audio_measurement/frame_fit.py | 361 | 93 | 203 | 14 | 51 | 60.11 |
| jasper/cli/read_distortion.py | 353 | 269 | 37 | 19 | 28 | 15.86 |
| jasper/active_speaker/runtime_convergence.py | 352 | 268 | 27 | 20 | 37 | 13.35 |
| jasper/audio_measurement/sweep.py | 350 | 206 | 80 | 23 | 41 | 29.43 |
| jasper/calibration_agent/tools.py | 340 | 283 | 6 | 9 | 42 | 4.41 |
| jasper/active_speaker/calibration_level.py | 337 | 259 | 39 | 3 | 36 | 12.46 |
| jasper/web/correction_tuning.py | 335 | 221 | 34 | 24 | 56 | 17.31 |
| jasper/active_speaker/commissioning_lifecycle.py | 332 | 293 | 3 | 5 | 31 | 2.41 |
| jasper/web/balance_volume_guard.py | 331 | 271 | 15 | 8 | 37 | 6.95 |
| jasper/active_speaker/crossover_v2/record_store.py | 330 | 164 | 91 | 24 | 51 | 34.85 |
| jasper/cli/forward_model.py | 327 | 248 | 27 | 10 | 42 | 11.31 |
| jasper/correction/applied_speaker_evidence.py | 326 | 155 | 78 | 42 | 51 | 36.81 |
| jasper/cli/round.py | 324 | 241 | 32 | 17 | 34 | 15.12 |
| jasper/active_speaker/crossover_v2/proposal.py | 321 | 152 | 112 | 15 | 42 | 39.56 |
| jasper/active_speaker/volume_latch.py | 317 | 125 | 114 | 38 | 40 | 47.95 |
| jasper/active_speaker/branch_target.py | 312 | 73 | 136 | 65 | 38 | 64.42 |
| jasper/audio_measurement/frame_ledger.py | 312 | 88 | 147 | 39 | 38 | 59.62 |
| jasper/correction/household_mic.py | 310 | 160 | 105 | 9 | 36 | 36.77 |
| jasper/correction/peq.py | 308 | 136 | 96 | 30 | 46 | 40.91 |
| jasper/correction/fir_runtime.py | 301 | 253 | 13 | 3 | 32 | 5.32 |
| jasper/cli/classify_features.py | 300 | 221 | 38 | 20 | 21 | 19.33 |
| jasper/cli/audition.py | 291 | 195 | 32 | 15 | 49 | 16.15 |
| jasper/attribution/mechanisms.py | 287 | 113 | 84 | 63 | 27 | 51.22 |
| jasper/cli/delay_sweep.py | 286 | 200 | 39 | 7 | 40 | 16.08 |
| jasper/audio_measurement/timeline_slip.py | 285 | 69 | 111 | 74 | 31 | 64.91 |
| jasper/cli/active_speaker_emit_bench.py | 285 | 209 | 37 | 9 | 30 | 16.14 |
| jasper/calibration_agent/proposal_sim.py | 283 | 187 | 41 | 16 | 39 | 20.14 |
| jasper/cli/arm_walk.py | 283 | 202 | 46 | 8 | 27 | 19.08 |
| jasper/audio_measurement/quality.py | 282 | 213 | 27 | 8 | 34 | 12.41 |
| jasper/active_speaker/crossover_v2/volume_claim.py | 280 | 85 | 144 | 12 | 39 | 55.71 |
| jasper/cli/close_reference.py | 280 | 198 | 35 | 13 | 34 | 17.14 |
| jasper/active_speaker/measurement_document.py | 277 | 233 | 6 | 3 | 35 | 3.25 |
| jasper/active_speaker/round_bank.py | 277 | 152 | 72 | 14 | 39 | 31.05 |
| jasper/active_speaker/crossover_v2/session_seams.py | 274 | 38 | 181 | 3 | 52 | 67.15 |
| jasper/cli/correction_bundle.py | 273 | 246 | 1 | 3 | 23 | 1.47 |
| jasper/active_speaker/reset.py | 270 | 132 | 43 | 54 | 41 | 35.93 |
| jasper/active_speaker/candidate_bank.py | 268 | 104 | 100 | 22 | 42 | 45.52 |
| jasper/attribution/storage.py | 261 | 101 | 104 | 9 | 47 | 43.3 |
| jasper/attribution/session_identity.py | 259 | 109 | 88 | 19 | 43 | 41.31 |
| jasper/active_speaker/crossover_v2/composition.py | 255 | 126 | 86 | 7 | 36 | 36.47 |
| jasper/audio_measurement/measurement_geometry.py | 248 | 113 | 87 | 8 | 40 | 38.31 |
| jasper/active_speaker/topology_tone.py | 244 | 211 | 6 | 3 | 24 | 3.69 |
| jasper/active_speaker/crossover_v2/candidates.py | 237 | 72 | 122 | 3 | 40 | 52.74 |
| jasper/active_speaker/commission_wiring.py | 236 | 122 | 61 | 3 | 50 | 27.12 |
| jasper/active_speaker/crossover_v2/frequency_view.py | 234 | 189 | 8 | 9 | 28 | 7.26 |
| jasper/active_speaker/speech_stimulus.py | 232 | 192 | 3 | 3 | 34 | 2.59 |
| experiments/usb-turntable/vendor/usb_turntable/controller.py | 231 | 195 | 1 | 2 | 33 | 1.3 |
| jasper/audio_measurement/wired_level_meter.py | 230 | 150 | 33 | 19 | 28 | 22.61 |
| jasper/cli/gate_sweep.py | 223 | 165 | 25 | 8 | 25 | 14.8 |
| jasper/active_speaker/crossover_v2/__init__.py | 222 | 37 | 171 | 3 | 11 | 78.38 |
| jasper/active_speaker/crossover_v2/playback_transaction.py | 220 | 61 | 96 | 29 | 34 | 56.82 |
| jasper/active_speaker/_common.py | 215 | 67 | 51 | 55 | 42 | 49.3 |
| jasper/correction/failures.py | 214 | 179 | 14 | 9 | 12 | 10.75 |
| jasper/correction/browser_audio.py | 212 | 165 | 11 | 9 | 27 | 9.43 |
| jasper/active_speaker/driver_pad.py | 209 | 105 | 58 | 17 | 29 | 35.89 |
| jasper/audio_measurement/mic_identity.py | 206 | 54 | 48 | 91 | 13 | 67.48 |
| jasper/active_speaker/measurement_archive.py | 204 | 164 | 8 | 5 | 27 | 6.37 |
| jasper/audio_measurement/quality_model.py | 202 | 28 | 80 | 63 | 31 | 70.79 |
| jasper/active_speaker/crossover_v2/record_index.py | 200 | 87 | 70 | 7 | 36 | 38.5 |
| jasper/active_speaker/crossover_v2/round_inputs.py | 200 | 112 | 50 | 11 | 27 | 30.5 |
| jasper/active_speaker/crossover_v2/capture_source.py | 196 | 34 | 96 | 33 | 33 | 65.82 |
| jasper/active_speaker/repeat_floor.py | 195 | 110 | 49 | 6 | 30 | 28.21 |
| jasper/cli/bass_extension_bench.py | 194 | 122 | 37 | 3 | 32 | 20.62 |
| jasper/active_speaker/startup_hold.py | 193 | 62 | 87 | 12 | 32 | 51.3 |
| jasper/correction/spatial.py | 189 | 131 | 14 | 17 | 27 | 16.4 |
| jasper/audio_measurement/comparison_bands.py | 183 | 67 | 86 | 4 | 26 | 49.18 |
| jasper/active_speaker/program_playback.py | 179 | 83 | 56 | 8 | 32 | 35.75 |
| jasper/active_speaker/frequency_view.py | 173 | 138 | 10 | 3 | 22 | 7.51 |
| jasper/calibration_agent/key_provisioning.py | 172 | 75 | 53 | 13 | 31 | 38.37 |
| jasper/cli/project_ring.py | 167 | 116 | 23 | 10 | 18 | 19.76 |
| jasper/correction/interop.py | 166 | 121 | 20 | 3 | 22 | 13.86 |
| jasper/cli/declare_geometry.py | 165 | 110 | 14 | 12 | 29 | 15.76 |
| jasper/active_speaker/measurement_programs.py | 160 | 82 | 30 | 10 | 38 | 25.0 |
| jasper/active_speaker/crossover_v2/tuning_scope.py | 158 | 37 | 83 | 8 | 30 | 57.59 |
| jasper/correction/replay_artifacts.py | 157 | 125 | 9 | 3 | 20 | 7.64 |
| jasper/correction/playback.py | 154 | 95 | 19 | 13 | 27 | 20.78 |
| jasper/audio_measurement/room_boundary.py | 148 | 13 | 100 | 17 | 18 | 79.05 |
| jasper/active_speaker/tuning_handoff.py | 145 | 92 | 26 | 10 | 17 | 24.83 |
| jasper/active_speaker/capture_entry_anchor.py | 143 | 76 | 43 | 3 | 21 | 32.17 |
| jasper/active_speaker/crossover_v2/feature_optics.py | 143 | 60 | 33 | 27 | 23 | 41.96 |
| experiments/usb-turntable/vendor/usb_turntable/discovery.py | 140 | 113 | 3 | 1 | 23 | 2.86 |
| jasper/active_speaker/crossover_v2/fc_sweep.py | 138 | 35 | 70 | 14 | 19 | 60.87 |
| jasper/web/correction_bass_flow.py | 137 | 85 | 16 | 14 | 22 | 21.9 |
| jasper/attribution/closed_sets.py | 136 | 65 | 18 | 44 | 9 | 45.59 |
| jasper/audio_measurement/__init__.py | 136 | 0 | 128 | 3 | 5 | 96.32 |
| jasper/correction/runtime_safety.py | 133 | 94 | 8 | 11 | 20 | 14.29 |
| jasper/active_speaker/measurement_emit.py | 132 | 41 | 68 | 3 | 20 | 53.79 |
| jasper/cli/round_bank.py | 131 | 97 | 12 | 6 | 16 | 13.74 |
| jasper/web/balance_level.py | 131 | 100 | 8 | 4 | 19 | 9.16 |
| jasper/attribution/__init__.py | 128 | 83 | 33 | 3 | 9 | 28.12 |
| experiments/usb-turntable/vendor/usb_turntable/transport.py | 124 | 107 | 1 | 1 | 15 | 1.61 |
| jasper/active_speaker/controllability_ledger.py | 113 | 45 | 31 | 15 | 22 | 40.71 |
| jasper/active_speaker/crossover_v2/measurement_phase.py | 111 | 31 | 43 | 19 | 18 | 55.86 |
| experiments/usb-turntable/vendor/usb_turntable/cli.py | 105 | 91 | 1 | 1 | 12 | 1.9 |
| jasper/correction/state_guard.py | 101 | 79 | 3 | 7 | 12 | 9.9 |
| jasper/web/correction_report.py | 100 | 73 | 9 | 3 | 15 | 12.0 |
| jasper/web/correction_measurements.py | 99 | 74 | 3 | 3 | 19 | 6.06 |
| jasper/calibration_agent/sound_actions.py | 93 | 67 | 7 | 3 | 16 | 10.75 |
| jasper/web/active_speaker_flow.py | 90 | 22 | 49 | 3 | 16 | 57.78 |
| jasper/active_speaker/crossover_v2/attempt_grading.py | 88 | 9 | 11 | 60 | 8 | 80.68 |
| jasper/calibration_agent/prompt.py | 82 | 60 | 7 | 3 | 12 | 12.2 |
| jasper/correction/target.py | 82 | 19 | 35 | 7 | 21 | 51.22 |
| jasper/active_speaker/audible_policy.py | 80 | 56 | 5 | 5 | 14 | 12.5 |
| jasper/active_speaker/delay_sweep.py | 75 | 34 | 17 | 12 | 12 | 38.67 |
| jasper/active_speaker/level_trim.py | 67 | 45 | 3 | 9 | 10 | 17.91 |
| experiments/usb-turntable/vendor/usb_turntable/errors.py | 65 | 36 | 8 | 1 | 20 | 13.85 |
| jasper/active_speaker/bench/__init__.py | 63 | 1 | 52 | 3 | 7 | 87.3 |
| jasper/web/correction_crossover_context.py | 63 | 25 | 4 | 26 | 8 | 47.62 |
| jasper/active_speaker/passive_profile.py | 62 | 28 | 18 | 3 | 13 | 33.87 |
| jasper/active_speaker/restore_wait.py | 58 | 24 | 15 | 8 | 11 | 39.66 |
| experiments/usb-turntable/vendor/usb_turntable/commands.py | 55 | 40 | 1 | 1 | 13 | 3.64 |
| jasper/active_speaker/crossover_envelope.py | 52 | 20 | 17 | 3 | 12 | 38.46 |
| jasper/active_speaker/revalidation.py | 52 | 32 | 9 | 3 | 8 | 23.08 |
| jasper/active_speaker/alignment_walk.py | 49 | 24 | 12 | 3 | 10 | 30.61 |
| jasper/audio_measurement/level_solver.py | 48 | 9 | 13 | 17 | 9 | 62.5 |
| jasper/active_speaker/crossover_v2/handoff_doors.py | 45 | 30 | 4 | 3 | 8 | 15.56 |
| jasper/cli/measurement_mic.py | 45 | 10 | 21 | 3 | 11 | 53.33 |
| jasper/active_speaker/tone_plan.py | 42 | 28 | 2 | 3 | 9 | 11.9 |
| jasper/web/correction_hub.py | 36 | 22 | 1 | 7 | 6 | 22.22 |
| experiments/usb-turntable/vendor/usb_turntable/__init__.py | 33 | 27 | 1 | 1 | 4 | 6.06 |
| jasper/correction/__init__.py | 31 | 0 | 25 | 3 | 3 | 90.32 |
| jasper/calibration_agent/curves.py | 23 | 13 | 2 | 3 | 5 | 21.74 |
| jasper/correction/_numbers.py | 21 | 11 | 2 | 3 | 5 | 23.81 |
| jasper/calibration_agent/__init__.py | 15 | 0 | 10 | 3 | 2 | 86.67 |
| jasper/audio_measurement/excitation.py | 13 | 1 | 6 | 3 | 3 | 69.23 |
| experiments/usb-turntable/vendor/usb_turntable/__main__.py | 5 | 2 | 0 | 1 | 2 | 20.0 |

### Table 1b — per-package totals

| package | files | total | code | docstring | comment | blank | prose % |
|---|---:|---:|---:|---:|---:|---:|---:|
| jasper/active_speaker | 172 | 167841 | 98009 | 32285 | 21508 | 16039 | 32.05 |
| jasper/audio_measurement | 38 | 31943 | 17010 | 7433 | 4175 | 3325 | 36.34 |
| jasper/web | 19 | 23686 | 14338 | 4104 | 3026 | 2218 | 30.1 |
| jasper/correction | 31 | 16883 | 11792 | 2246 | 1246 | 1599 | 20.68 |
| jasper/cli | 25 | 13425 | 9183 | 1838 | 972 | 1432 | 20.93 |
| jasper/calibration_agent | 13 | 4633 | 3633 | 357 | 175 | 468 | 11.48 |
| jasper/attribution | 8 | 2688 | 1366 | 635 | 406 | 281 | 38.73 |
| experiments/usb-turntable | 10 | 2219 | 1747 | 117 | 82 | 273 | 8.97 |
| **TOTAL** | 316 | 263318 | 157078 | 49015 | 31590 | 25635 | 30.61 |

### Table 2 — files > 1,500 lines: largest class / largest function

(44 files)

| file | total lines | largest class (lines) | largest function (lines) |
|---|---:|---|---|
| jasper/active_speaker/crossover_v2_flow.py | 7839 | CrossoverV2Session (6248) | __init__ (796) |
| jasper/web/correction_crossover_v2.py | 7831 | PositionGate (319) | prepare_v2_session (958) |
| jasper/web/correction_setup.py | 7480 | Handler (1114) | _make_handler (1117) |
| jasper/audio_measurement/program_analysis.py | 6572 | ProgramAnalysis (127) | _build_candidate (438) |
| jasper/active_speaker/runtime_contract.py | 5529 | SafeGraphDecision (43) | _active_graph_evidence (779) |
| jasper/active_speaker/camilla_yaml.py | 4551 | ActiveEmitDevices (23) | emit_active_speaker_program_config (335) |
| jasper/active_speaker/crossover_envelope_v2.py | 4417 | — | build_crossover_envelope_v2 (663) |
| jasper/active_speaker/baseline_profile.py | 4192 | — | build_baseline_profile_candidate (986) |
| jasper/active_speaker/crossover_v2/evidence_packet.py | 3753 | CrossoverEvidencePacketError (2) | build_crossover_evidence_packet (299) |
| jasper/active_speaker/linearization_fit.py | 3567 | LinearizationFit (257) | fit_driver_linearization (605) |
| jasper/active_speaker/commissioning_evidence.py | 3501 | DelayWalkEvidence (247) | __post_init__ (196) |
| jasper/active_speaker/driver_safety.py | 3258 | DriverSafetyProfileEvaluation (28) | _validate_driver_safety_profile_shape (335) |
| jasper/active_speaker/crossover_v2/spatial.py | 3148 | PositionGeometry (70) | assemble_cloud_group_result (360) |
| jasper/active_speaker/crossover_v2/capture_plan.py | 3050 | V2PlanShape (161) | build_v2_capture_plan (285) |
| jasper/active_speaker/web_commissioning.py | 2916 | FaninGateContext (22) | play_driver_capture_sweep (397) |
| jasper/correction/session.py | 2881 | MeasurementSession (2555) | __init__ (213) |
| jasper/active_speaker/crossover_v2/feature_classifier.py | 2599 | RoundPoseCurve (22) | classify_round (273) |
| jasper/active_speaker/crossover_v2/driver_prescription.py | 2508 | DriverPrescription (153) | driver_prescription_response_format (256) |
| jasper/active_speaker/staging.py | 2498 | StagedAnchorLockContended (2) | prepare_driver_commissioning_config (380) |
| jasper/active_speaker/delta_probe.py | 2494 | DeltaProbeMap (369) | classify_delta_probe (779) |
| jasper/active_speaker/startup_load.py | 2352 | — | load_driver_commissioning_config (472) |
| jasper/active_speaker/seat_level_ramp.py | 2245 | _WindowTrace (45) | _walk_to_the_band (560) |
| jasper/audio_measurement/spatial_combine.py | 2228 | EchoDiagnostic (115) | detect_echo (607) |
| jasper/active_speaker/crossover_v2/durable_state.py | 2198 | V2ConductorSnapshot (87) | build_conductor_state (852) |
| jasper/active_speaker/crossover_v2/verification.py | 2186 | MeasurementComparand (53) | evaluate_applied_safety (155) |
| jasper/audio_measurement/program.py | 2159 | ProgramSegment (136) | build_measure_program (284) |
| jasper/active_speaker/commissioning_run.py | 2141 | CommissioningRunStore (1193) | __post_init__ (166) |
| jasper/audio_measurement/interference_nulls.py | 1900 | InterferenceNullReport (92) | identify_interference_nulls (286) |
| jasper/active_speaker/commissioning_receipt.py | 1831 | CommissioningEligibilityReceipt (257) | __post_init__ (192) |
| jasper/active_speaker/crossover_v2/round_views.py | 1810 | EntryStateGrade (89) | spec_with_gate_sensitivity (103) |
| jasper/cli/active_speaker.py | 1797 | — | build_parser (373) |
| jasper/active_speaker/measurement.py | 1792 | — | record_summed_validation (163) |
| jasper/audio_measurement/ramp.py | 1752 | RampController (905) | run (299) |
| jasper/web/correction_crossover_backend.py | 1748 | CrossoverLevelLease (917) | set_durable_repeat_progress (189) |
| jasper/active_speaker/crossover_v2/intervention.py | 1718 | LinearizationRequest (141) | plan_linearization (836) |
| jasper/active_speaker/crossover_v2/blend_prescription.py | 1688 | BlendPrescription (62) | prescription_response_format (125) |
| jasper/cli/crossover_prescriber.py | 1628 | — | _next_actions (129) |
| jasper/active_speaker/crossover_v2/contracts.py | 1617 | InterventionProposal (204) | __init__ (105) |
| jasper/audio_measurement/playback.py | 1611 | TonePlayer (194) | _play_wav_source (167) |
| jasper/active_speaker/crossover_v2/refusal_copy.py | 1578 | PhaseVerdict (55) | round_restore_reason (75) |
| jasper/active_speaker/crossover_v2/sweep_spec.py | 1576 | CaptureSpec (439) | build_crossover_sweep_spec (354) |
| jasper/active_speaker/design_draft.py | 1570 | ActiveSpeakerDesignDraftRevisionConflict (6) | build_design_draft (176) |
| jasper/correction/envelope.py | 1567 | _ReadinessUnset (2) | _next_action_for (128) |
| jasper/active_speaker/commissioning_capture.py | 1520 | — | build_crossover_alignment_proposal (233) |


## 3. Functions > 150 lines, classes > 800 lines

### Table 3a — functions/methods > 150 lines (189 found)

| file | name | lines | starts at line |
|---|---|---:|---:|
| jasper/web/correction_setup.py | _make_handler | 1117 | 6132 |
| jasper/active_speaker/baseline_profile.py | build_baseline_profile_candidate | 986 | 2035 |
| jasper/web/correction_crossover_v2.py | prepare_v2_session | 958 | 6105 |
| jasper/active_speaker/crossover_v2/durable_state.py | build_conductor_state | 852 | 1347 |
| jasper/active_speaker/crossover_v2/intervention.py | plan_linearization | 836 | 810 |
| jasper/active_speaker/crossover_v2_flow.py | CrossoverV2Session.__init__ | 796 | 1516 |
| jasper/active_speaker/delta_probe.py | classify_delta_probe | 779 | 1627 |
| jasper/active_speaker/runtime_contract.py | _active_graph_evidence | 779 | 2776 |
| jasper/web/correction_crossover_v2_relay.py | build_v2_run_and_consume | 746 | 304 |
| jasper/web/correction_crossover_v2_relay.py | build_v2_run_and_consume._run_and_consume | 674 | 374 |
| jasper/active_speaker/crossover_envelope_v2.py | build_crossover_envelope_v2 | 663 | 3755 |
| jasper/web/correction_crossover_v2_wired.py | build_v2_wired_run_and_consume | 623 | 541 |
| jasper/audio_measurement/spatial_combine.py | detect_echo | 607 | 1209 |
| jasper/active_speaker/linearization_fit.py | fit_driver_linearization | 605 | 2963 |
| jasper/active_speaker/seat_level_ramp.py | _walk_to_the_band | 560 | 1627 |
| jasper/web/correction_setup.py | _run_relay_level_match | 560 | 4481 |
| jasper/active_speaker/web_measurement.py | record_driver_capture | 534 | 849 |
| jasper/web/correction_crossover_v2_wired.py | build_v2_wired_run_and_consume._run_and_consume | 525 | 637 |
| jasper/active_speaker/setup_status.py | read_active_speaker_setup_status | 513 | 834 |
| jasper/active_speaker/startup_load.py | load_driver_commissioning_config | 472 | 1737 |
| jasper/web/correction_crossover_v2.py | bind_production_play | 463 | 3634 |
| jasper/active_speaker/commission_ramp.py | ramp_audible_step | 447 | 481 |
| jasper/audio_measurement/program_analysis.py | _build_candidate | 438 | 5461 |
| jasper/active_speaker/commissioning_coordinator.py | build_commissioning_view | 431 | 691 |
| jasper/active_speaker/baseline_profile.py | _derive_corrections | 420 | 798 |
| jasper/active_speaker/runtime_contract.py | safe_graph_for_current_topology | 408 | 4950 |
| jasper/web/correction_crossover_v2.py | handle_v2_apply | 401 | 7123 |
| jasper/web/correction_setup.py | _make_handler.Handler._dispatch_crossover | 399 | 6292 |
| jasper/active_speaker/web_commissioning.py | play_driver_capture_sweep | 397 | 2226 |
| jasper/correction/coordinator.py | measurement_window | 396 | 592 |
| jasper/active_speaker/staging.py | prepare_driver_commissioning_config | 380 | 2078 |
| jasper/cli/active_speaker.py | build_parser | 373 | 1406 |
| jasper/active_speaker/commissioning_isolated_producer.py | promote_isolated_driver_capture | 362 | 564 |
| jasper/web/correction_crossover_v2.py | prepare_v2_session._open | 362 | 6655 |
| jasper/active_speaker/crossover_v2/spatial.py | assemble_cloud_group_result | 360 | 2789 |
| jasper/web/correction_setup.py | _make_handler.Handler.do_POST | 357 | 6890 |
| jasper/active_speaker/baseline_profile.py | _apply_baseline_profile_locked | 356 | 3837 |
| jasper/active_speaker/crossover_v2/sweep_spec.py | build_crossover_sweep_spec | 354 | 1222 |
| jasper/active_speaker/seat_level_ramp.py | run_seat_level_ramp | 345 | 1117 |
| jasper/active_speaker/crossover_v2/planning.py | build_candidate | 341 | 656 |
| jasper/active_speaker/camilla_yaml.py | emit_active_speaker_program_config | 335 | 3603 |
| jasper/active_speaker/driver_safety.py | _validate_driver_safety_profile_shape | 335 | 2608 |
| jasper/active_speaker/linearization_fit.py | _lift_stage | 331 | 2617 |
| jasper/active_speaker/commissioning_apply.py | _apply_measured_candidate_owned | 317 | 1079 |
| jasper/web/correction_crossover_v2.py | _post_apply_grade | 316 | 1498 |
| jasper/active_speaker/flat_spec.py | evaluate_flat_spec | 309 | 585 |
| jasper/web/correction_crossover_v2.py | _take_staged_angle_walk | 308 | 1950 |
| jasper/active_speaker/crossover_v2/evidence_packet.py | build_crossover_evidence_packet | 299 | 3239 |
| jasper/audio_measurement/ramp.py | RampController.run | 299 | 964 |
| jasper/audio_measurement/program_analysis.py | _select_alignment_pair | 290 | 3399 |
| jasper/active_speaker/driver_acoustics.py | _capture_to_magnitude | 289 | 341 |
| jasper/active_speaker/linearization_envelope.py | compose_envelope | 288 | 573 |
| jasper/active_speaker/camilla_yaml.py | emit_active_speaker_baseline_config | 286 | 3940 |
| jasper/audio_measurement/interference_nulls.py | identify_interference_nulls | 286 | 1418 |
| jasper/active_speaker/crossover_v2/capture_plan.py | build_v2_capture_plan | 285 | 2113 |
| jasper/active_speaker/staging.py | _stage_protected_startup_config_locked | 285 | 1782 |
| jasper/audio_measurement/program.py | build_measure_program | 284 | 1170 |
| jasper/correction/confidence.py | build_confidence_report | 284 | 467 |
| jasper/active_speaker/startup_load.py | build_driver_commission_load_preflight | 281 | 1454 |
| jasper/web/correction_setup.py | _handle_start | 280 | 2534 |
| jasper/active_speaker/crossover_v2/feature_classifier.py | classify_round | 273 | 2327 |
| jasper/active_speaker/commissioning_service.py | CommissioningCaptureService.status | 270 | 1011 |
| jasper/active_speaker/staging.py | _preset_from_crossover_preview | 268 | 754 |
| jasper/cli/active_speaker.py | _reemit_staged_startup_anchor | 267 | 507 |
| jasper/active_speaker/startup_load.py | build_startup_load_preflight | 260 | 518 |
| jasper/correction/strategy.py | design_correction | 259 | 666 |
| jasper/active_speaker/baseline_profile.py | _measured_level_trims | 256 | 540 |
| jasper/active_speaker/crossover_v2/driver_prescription.py | driver_prescription_response_format | 256 | 2253 |
| jasper/active_speaker/crossover_v2_flow.py | CrossoverV2Session._close_cloud_group | 256 | 4852 |
| jasper/active_speaker/driver_safety.py | validate_driver_research_request | 255 | 1079 |
| jasper/active_speaker/staging.py | _bind_preset_to_topology | 254 | 1189 |
| jasper/active_speaker/crossover_v2/close_reference.py | compare_impulse_responses | 253 | 361 |
| jasper/active_speaker/program_admission.py | _evaluate_program | 252 | 374 |
| jasper/audio_measurement/program_analysis.py | analysis_diagnostic_summary | 252 | 6321 |
| jasper/active_speaker/web_measurement.py | _finalize_driver_repeat_set | 251 | 568 |
| jasper/active_speaker/bringup.py | build_bringup_preflight | 250 | 205 |
| jasper/active_speaker/crossover_v2/delta_probe_run.py | run_delta_probe | 249 | 241 |
| jasper/active_speaker/crossover_v2/accountability.py | assess_accountability | 246 | 248 |
| jasper/cli/active_speaker.py | _cmd_baseline_reemit | 245 | 821 |
| jasper/active_speaker/crossover_v2/capture_plan.py | build_v2_verify_capture_plan | 244 | 2425 |
| jasper/active_speaker/linearization_fit.py | _hf_continuation_stage | 244 | 1880 |
| jasper/audio_measurement/program_analysis.py | _analyze_verify | 244 | 5987 |
| jasper/audio_measurement/ramp.py | RampController._tick_state | 238 | 1264 |
| jasper/active_speaker/commissioning_admission.py | play_admitted_driver_capture | 235 | 1004 |
| jasper/active_speaker/crossover_v2_flow.py | CrossoverV2Session._grade_verify_attempt | 235 | 6857 |
| jasper/web/correction_crossover_v2_republish.py | handle_v2_republish | 235 | 140 |
| jasper/active_speaker/crossover_v2/round_evidence.py | evaluate_round | 234 | 626 |
| jasper/active_speaker/commissioning_capture.py | build_crossover_alignment_proposal | 233 | 915 |
| jasper/active_speaker/commissioning_capture.py | record_summed_acoustic_capture | 232 | 502 |
| jasper/active_speaker/driver_safety.py | _profile_core | 231 | 2218 |
| jasper/web/correction_crossover_v2.py | resolve_conductor_context | 231 | 4344 |
| jasper/active_speaker/crossover_preview.py | _build_crossover | 230 | 277 |
| jasper/active_speaker/startup_load.py | load_protected_startup_config | 230 | 812 |
| jasper/calibration_agent/cli.py | render_markdown | 229 | 30 |
| jasper/active_speaker/crossover_contract.py | summed_decision_evidence_state | 226 | 174 |
| jasper/active_speaker/baseline_profile.py | _bank_applied_base_trim | 223 | 3341 |
| jasper/active_speaker/commissioning_admission.py | issue_protection_evidence | 222 | 606 |
| jasper/active_speaker/attempts_loop.py | decide_next | 219 | 604 |
| jasper/active_speaker/crossover_v2/blend_correction.py | solve_blend_correction | 219 | 545 |
| jasper/active_speaker/test_signal_plan.py | driver_test_signal_plan_from_edges | 219 | 305 |
| jasper/active_speaker/path_safety.py | build_startup_load_path_safety_evidence | 218 | 747 |
| jasper/active_speaker/crossover_envelope_v2.py | _review_envelope | 217 | 1559 |
| jasper/web/correction_crossover_v2.py | bind_position_retention | 217 | 3000 |
| jasper/active_speaker/web_commissioning.py | _load_applied_summed_measurement_config | 215 | 1125 |
| jasper/active_speaker/driver_safety.py | build_driver_research_prompt | 214 | 1671 |
| jasper/active_speaker/seat_level_ramp.py | run_seat_level_ramp._leveled_under_isolation | 214 | 1211 |
| jasper/attribution/promotion.py | promote_level_frame_disagreement | 214 | 373 |
| jasper/correction/autolevel.py | AutolevelController.run | 214 | 225 |
| jasper/active_speaker/camilla_yaml.py | emit_active_speaker_driver_domain_config | 213 | 4228 |
| jasper/active_speaker/commissioning_capture.py | record_driver_acoustic_capture | 213 | 287 |
| jasper/correction/session.py | MeasurementSession.__init__ | 213 | 336 |
| jasper/active_speaker/playback.py | start_tone_playback | 212 | 998 |
| jasper/active_speaker/crossover_v2/feature_classifier.py | _run_controls | 211 | 1373 |
| jasper/active_speaker/crossover_alignment.py | propose_crossover_alignment | 209 | 214 |
| jasper/active_speaker/driver_acoustics.py | analyze_driver_capture | 207 | 913 |
| jasper/web/correction_crossover_v2.py | _bind_engine_measure_leg | 204 | 5807 |
| jasper/active_speaker/crossover_v2_flow.py | CrossoverV2Session._verify_verdict | 201 | 7176 |
| jasper/audio_measurement/admitted_playback.py | play_admitted_wav | 201 | 454 |
| jasper/audio_measurement/program_analysis.py | _resolve_anchor | 198 | 1833 |
| jasper/active_speaker/camilla_yaml.py | emit_active_speaker_commissioning_config | 197 | 2957 |
| jasper/web/correction_crossover_v2.py | bind_production_play._play | 197 | 3795 |
| jasper/active_speaker/commissioning_apply.py | restore_pending_candidate_apply | 196 | 804 |
| jasper/active_speaker/commissioning_evidence.py | RegionCommissioningEvidence.__post_init__ | 196 | 3147 |
| jasper/active_speaker/crossover_v2_flow.py | CrossoverV2Session._measure_verdict | 196 | 4307 |
| jasper/active_speaker/crossover_v2/feature_classifier.py | load_round_captures | 195 | 602 |
| jasper/web/correction_setup.py | _make_handler.Handler.do_GET | 195 | 6694 |
| jasper/active_speaker/commissioning_receipt.py | CommissioningEligibilityReceipt.__post_init__ | 192 | 1586 |
| jasper/active_speaker/crossover_preview.py | build_crossover_preview | 192 | 509 |
| jasper/correction/bundles.py | validate_bundle | 192 | 337 |
| jasper/active_speaker/driver_safety.py | evaluate_driver_safety_profile | 191 | 3068 |
| jasper/active_speaker/baseline_profile.py | recompose_applied_baseline_yaml | 190 | 3091 |
| jasper/calibration_agent/actions.py | _run_one_action | 190 | 107 |
| jasper/correction/acceptance.py | evaluate_acceptance | 190 | 376 |
| jasper/active_speaker/crossover_v2/evidence_packet.py | _not_evaluated | 189 | 3048 |
| jasper/active_speaker/runtime_contract.py | _flat_graph_allowed | 189 | 1305 |
| jasper/web/correction_crossover_backend.py | CrossoverLevelLease.set_durable_repeat_progress | 189 | 736 |
| jasper/web/correction_crossover_v2_status.py | _compact_cloud_status | 189 | 166 |
| jasper/active_speaker/excitation_safety_plan.py | resolve_driver_excitation_ceilings | 188 | 591 |
| jasper/audio_measurement/spatial_combine.py | combine_positions | 188 | 2041 |
| jasper/web/correction_setup.py | _handle_relay_capture | 187 | 4115 |
| jasper/active_speaker/camilla_yaml.py | emit_active_speaker_parked_config | 185 | 2512 |
| jasper/correction/status.py | describe_current_config | 185 | 99 |
| jasper/active_speaker/commission_ramp.py | record_ramp_operator_ack | 183 | 998 |
| jasper/active_speaker/runtime_contract.py | classify_camilla_graph | 183 | 3859 |
| jasper/active_speaker/commissioning_capture.py | aggregate_driver_repeats | 182 | 1305 |
| jasper/audio_measurement/program_analysis.py | _estimate_drift | 178 | 2332 |
| jasper/web/correction_crossover_v2_wired.py | build_v2_wired_run_and_consume._run_and_consume._walk | 178 | 853 |
| jasper/active_speaker/design_draft.py | build_design_draft | 176 | 1147 |
| jasper/active_speaker/topology_tone.py | build_summed_topology_tone_plan | 176 | 61 |
| jasper/correction/bundles.py | _validate_artifact_manifest | 176 | 531 |
| jasper/active_speaker/setup_status.py | _acoustic_commissioning_status | 175 | 212 |
| jasper/web/correction_setup.py | _handle_relay_verify | 175 | 4304 |
| jasper/active_speaker/crossover_v2/harmonic_evidence.py | read_round_harmonics | 174 | 1091 |
| jasper/active_speaker/crossover_v2/evidence_packet.py | _cross_seat_sigma_block | 173 | 871 |
| jasper/audio_measurement/program_analysis.py | _solve_role_gain | 172 | 4744 |
| jasper/audio_measurement/program_analysis.py | _analyze_measure | 172 | 5287 |
| jasper/active_speaker/crossover_v2/harmonic_evidence.py | rebuild_measure_program | 171 | 378 |
| jasper/active_speaker/commissioning_apply.py | _restore_failed_mutation_locked | 170 | 404 |
| jasper/web/correction_crossover_v2.py | bind_production_analyze | 170 | 2701 |
| jasper/active_speaker/crossover_v2/diagnostics.py | _log_measure_diag | 169 | 359 |
| jasper/active_speaker/crossover_v2_flow.py | CrossoverV2Session._run_cloud_pipeline | 169 | 6269 |
| jasper/web/correction_crossover_backend.py | apply_profile | 169 | 1498 |
| jasper/active_speaker/crossover_v2_flow.py | CrossoverV2Session.consume_capture | 168 | 3565 |
| jasper/audio_measurement/quality.py | assess_capture | 168 | 115 |
| jasper/audio_measurement/playback.py | _play_wav_source | 167 | 839 |
| jasper/web/correction_crossover_v2.py | bind_delta_probe_rollback | 167 | 7526 |
| jasper/active_speaker/commissioning_run.py | CommissioningLiveMutation.__post_init__ | 166 | 224 |
| jasper/active_speaker/measurement.py | record_summed_validation | 163 | 1630 |
| jasper/active_speaker/crossover_v2/gate_sweep.py | _feature_result | 162 | 676 |
| jasper/active_speaker/seat_level_ramp.py | _remeasure_silence | 161 | 1464 |
| jasper/correction/level_match.py | LevelMatchSession.run_for_geometry | 161 | 907 |
| experiments/usb-turntable/jts_turntable.py | run | 160 | 621 |
| jasper/active_speaker/crossover_v2_flow.py | CrossoverV2Session._resolve_spent_slot | 159 | 3744 |
| jasper/active_speaker/environment.py | classify_camilla_config_text | 159 | 315 |
| jasper/audio_measurement/calibration.py | migrate_stored_sign_conventions | 159 | 1065 |
| jasper/web/correction_setup.py | _normalize_room_readiness | 159 | 2288 |
| jasper/active_speaker/commissioning_runtime.py | prepare_summed_excitation | 158 | 988 |
| jasper/active_speaker/crossover_v2/topology_prescription.py | read_topology_prescription | 158 | 632 |
| jasper/audio_measurement/playback.py | TonePlayer.play | 157 | 1431 |
| jasper/web/correction_crossover_v2.py | PositionGate.gate | 156 | 4789 |
| jasper/active_speaker/crossover_v2/verification.py | evaluate_applied_safety | 155 | 820 |
| jasper/web/correction_crossover_v2.py | _volume_hooks | 155 | 5084 |
| jasper/active_speaker/commissioning_evidence.py | DelayWalkEvidence.__post_init__ | 154 | 2832 |
| jasper/active_speaker/crossover_envelope_v2.py | _failure_envelope | 154 | 3599 |
| jasper/active_speaker/crossover_v2/prescription_spool.py | _validate | 154 | 713 |
| jasper/audio_measurement/program_analysis.py | _pilot_observations | 154 | 4320 |
| jasper/cli/null_door.py | _run | 153 | 652 |
| jasper/correction/session.py | MeasurementSession.on_verify_capture_uploaded | 152 | 2211 |
| jasper/active_speaker/camilla_yaml.py | emit_active_speaker_startup_config | 151 | 2348 |

### Table 3b — classes > 800 lines (8 found)

| file | name | lines | starts at line |
|---|---|---:|---:|
| jasper/active_speaker/crossover_v2_flow.py | CrossoverV2Session | 6248 | 1489 |
| jasper/correction/session.py | MeasurementSession | 2555 | 327 |
| jasper/active_speaker/commissioning_run.py | CommissioningRunStore | 1193 | 949 |
| jasper/active_speaker/commissioning_service.py | CommissioningCaptureService | 1124 | 173 |
| jasper/web/correction_setup.py | _make_handler.Handler | 1114 | 6133 |
| jasper/active_speaker/commissioning_evidence_store.py | CommissioningEvidenceStore | 1071 | 363 |
| jasper/web/correction_crossover_backend.py | CrossoverLevelLease | 917 | 136 |
| jasper/audio_measurement/ramp.py | RampController | 905 | 848 |


## 4. Duplicate-helper census

### Table 4 — duplicate-helper census (14 names with >=2 defs; 109 distinct matching names total)

#### `_utc_now` — 12 definitions

| file | line |
|---|---:|
| jasper/active_speaker/baseline_profile.py | 214 |
| jasper/active_speaker/calibration_level.py | 41 |
| jasper/active_speaker/crossover_preview.py | 62 |
| jasper/active_speaker/design_draft.py | 135 |
| jasper/active_speaker/driver_base_trim.py | 148 |
| jasper/active_speaker/measurement.py | 82 |
| jasper/active_speaker/model_error_store.py | 98 |
| jasper/active_speaker/path_safety.py | 144 |
| jasper/active_speaker/seat_level_reference.py | 157 |
| jasper/active_speaker/staging.py | 130 |
| jasper/active_speaker/startup_load.py | 126 |
| jasper/active_speaker/web_measurement.py | 41 |

Verdict: **byte-identical across all 12**

#### `_text` — 11 definitions

| file | line |
|---|---:|
| jasper/active_speaker/commissioning_evidence.py | 97 |
| jasper/active_speaker/commissioning_receipt.py | 129 |
| jasper/active_speaker/crossover_level_run.py | 122 |
| jasper/active_speaker/crossover_v2/contracts.py | 201 |
| jasper/active_speaker/crossover_v2/feature_classification.py | 473 |
| jasper/active_speaker/crossover_v2/record_index.py | 55 |
| jasper/active_speaker/design_draft.py | 175 |
| jasper/active_speaker/driver_safety.py | 363 |
| jasper/active_speaker/measurement.py | 100 |
| jasper/audio_measurement/evidence_identity.py | 36 |
| jasper/audio_measurement/excitation_artifacts.py | 221 |

Verdict: **11 identical-clusters (sizes 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1), bodies differ meaningfully**

#### `_fingerprint` — 10 definitions

| file | line |
|---|---:|
| jasper/active_speaker/baseline_profile.py | 231 |
| jasper/active_speaker/commissioning_evidence.py | 138 |
| jasper/active_speaker/commissioning_lifecycle.py | 195 |
| jasper/active_speaker/commissioning_receipt.py | 145 |
| jasper/active_speaker/commissioning_run.py | 542 |
| jasper/active_speaker/crossover_v2/evidence_packet.py | 3540 |
| jasper/active_speaker/crossover_v2/session_graph.py | 99 |
| jasper/active_speaker/driver_safety.py | 178 |
| jasper/active_speaker/measurement.py | 111 |
| jasper/audio_measurement/evidence_identity.py | 92 |

Verdict: **9 identical-clusters (sizes 2, 1, 1, 1, 1, 1, 1, 1, 1), bodies differ meaningfully**

#### `_sha256` — 10 definitions

| file | line |
|---|---:|
| jasper/active_speaker/commissioning_evidence.py | 114 |
| jasper/active_speaker/commissioning_host.py | 63 |
| jasper/active_speaker/commissioning_receipt.py | 141 |
| jasper/active_speaker/commissioning_run.py | 499 |
| jasper/active_speaker/crossover_level_run.py | 128 |
| jasper/active_speaker/excitation_safety_plan.py | 71 |
| jasper/active_speaker/measured_candidate.py | 167 |
| jasper/audio_measurement/admitted_playback.py | 76 |
| jasper/audio_measurement/evidence_identity.py | 42 |
| jasper/audio_measurement/excitation_artifacts.py | 233 |

Verdict: **10 identical-clusters (sizes 1, 1, 1, 1, 1, 1, 1, 1, 1, 1), bodies differ meaningfully**

#### `fingerprint` — 9 definitions

| file | line |
|---|---:|
| jasper/active_speaker/candidate_bank.py | 124 |
| jasper/active_speaker/excitation_safety_plan.py | 197 |
| jasper/active_speaker/excitation_safety_plan.py | 324 |
| jasper/active_speaker/program_admission.py | 176 |
| jasper/audio_measurement/excitation_admission.py | 256 |
| jasper/audio_measurement/excitation_admission.py | 391 |
| jasper/audio_measurement/excitation_admission.py | 512 |
| jasper/audio_measurement/excitation_admission.py | 745 |
| jasper/audio_measurement/null_walk.py | 259 |

Verdict: **6 identical-clusters (sizes 1, 2, 1, 3, 1, 1), bodies differ meaningfully**

#### `_mapping` — 8 definitions

| file | line |
|---|---:|
| jasper/active_speaker/branch_peak.py | 183 |
| jasper/active_speaker/controllability_ledger.py | 55 |
| jasper/active_speaker/crossover_contract.py | 107 |
| jasper/active_speaker/crossover_envelope_v2.py | 268 |
| jasper/active_speaker/crossover_v2/evidence_packet.py | 572 |
| jasper/active_speaker/crossover_v2/frequency_view.py | 29 |
| jasper/active_speaker/design_draft.py | 145 |
| jasper/active_speaker/setup_status.py | 195 |

Verdict: **5 identical-clusters (sizes 1, 4, 1, 1, 1), bodies differ meaningfully**

#### `_read_json` — 8 definitions

| file | line |
|---|---:|
| jasper/active_speaker/bundles.py | 288 |
| jasper/active_speaker/crossover_v2/evidence_packet.py | 517 |
| jasper/audio_measurement/bundles.py | 164 |
| jasper/calibration_agent/tools.py | 45 |
| jasper/cli/active_speaker_attempts_replay.py | 213 |
| jasper/correction/bundle_tools.py | 29 |
| jasper/correction/evidence.py | 37 |
| jasper/web/balance_flow.py | 163 |

Verdict: **7 identical-clusters (sizes 2, 1, 1, 1, 1, 1, 1), bodies differ meaningfully**

#### `_load` — 5 definitions

| file | line |
|---|---:|
| jasper/active_speaker/commission_wiring.py | 58 |
| jasper/active_speaker/controllability_ledger.py | 59 |
| jasper/active_speaker/crossover_v2/record_index.py | 117 |
| jasper/active_speaker/crossover_v2/session_graph.py | 474 |
| jasper/active_speaker/repeat_admission.py | 137 |

Verdict: **5 identical-clusters (sizes 1, 1, 1, 1, 1), bodies differ meaningfully**

#### `_now` — 5 definitions

| file | line |
|---|---:|
| jasper/active_speaker/commissioning_run.py | 981 |
| jasper/active_speaker/crossover_level_run.py | 413 |
| jasper/active_speaker/playback.py | 192 |
| jasper/active_speaker/repeat_admission.py | 123 |
| jasper/active_speaker/safe_playback.py | 46 |

Verdict: **4 identical-clusters (sizes 1, 1, 2, 1), bodies differ meaningfully**

#### `_declared_fingerprint` — 3 definitions

| file | line |
|---|---:|
| jasper/active_speaker/commissioning_evidence.py | 209 |
| jasper/active_speaker/commissioning_receipt.py | 189 |
| jasper/audio_measurement/evidence_identity.py | 114 |

Verdict: **3 identical-clusters (sizes 1, 1, 1), near-identical (ratio>0.8) to each other**

#### `_placement_fingerprint` — 2 definitions

| file | line |
|---|---:|
| jasper/active_speaker/commissioning_isolated_producer.py | 214 |
| jasper/active_speaker/commissioning_service.py | 146 |

Verdict: **2 identical-clusters (sizes 1, 1), bodies differ meaningfully**

#### `_json_mapping` — 2 definitions

| file | line |
|---|---:|
| jasper/active_speaker/crossover_v2/contracts.py | 240 |
| jasper/active_speaker/runtime_contract.py | 4052 |

Verdict: **2 identical-clusters (sizes 1, 1), bodies differ meaningfully**

#### `_utc_from_epoch` — 2 definitions

| file | line |
|---|---:|
| jasper/active_speaker/playback.py | 188 |
| jasper/active_speaker/safe_playback.py | 42 |

Verdict: **byte-identical across all 2**

#### `_float` — 2 definitions

| file | line |
|---|---:|
| jasper/cli/basic_profile.py | 140 |
| jasper/web/correction_tuning.py | 242 |

Verdict: **2 identical-clusters (sizes 1, 1), bodies differ meaningfully**

#### Single-definition matches (no duplication) — 95 names

| name | file | line |
|---|---|---:|
| `_active_graph_fingerprint` | jasper/web/correction_crossover_v2.py | 5257 |
| `_atomic_write_json` | jasper/correction/replay_artifacts.py | 69 |
| `_atomic_write_text` | jasper/active_speaker/camilla_yaml.py | 4548 |
| `_bool` | jasper/active_speaker/profile.py | 167 |
| `_chain_fingerprint` | jasper/active_speaker/driver_base_trim.py | 152 |
| `_content_fingerprint` | jasper/audio_measurement/excitation_admission.py | 97 |
| `_context_fingerprint` | jasper/active_speaker/commissioning_admission.py | 422 |
| `_design_draft_fingerprint` | jasper/active_speaker/crossover_preview.py | 78 |
| `_driver_target_fingerprint` | jasper/active_speaker/web_measurement.py | 556 |
| `_dump` | jasper/cli/basic_profile.py | 198 |
| `_dump_graph` | jasper/active_speaker/commissioning_runtime.py | 801 |
| `_entry_graph_fingerprint` | jasper/active_speaker/crossover_v2_flow.py | 6565 |
| `_expected_candidate_fingerprint` | jasper/correction/applied_speaker_evidence.py | 171 |
| `_graph_fingerprint` | jasper/audio_measurement/delay_graph.py | 105 |
| `_isolated_capture_artifacts` | jasper/active_speaker/commissioning_evidence.py | 1931 |
| `_isolated_levels` | jasper/active_speaker/measured_candidate.py | 531 |
| `_json_safe_dbfs` | jasper/web/correction_crossover_v2_wired.py | 332 |
| `_jsonable` | experiments/usb-turntable/jts_turntable.py | 193 |
| `_live_fingerprint` | jasper/active_speaker/wizard_client.py | 271 |
| `_load_applied_summed_measurement_config` | jasper/active_speaker/web_commissioning.py | 1125 |
| `_load_bundle_calibration` | jasper/correction/bundle_tools.py | 236 |
| `_load_candidate` | jasper/correction/applied_speaker_evidence.py | 189 |
| `_load_declarations` | jasper/cli/seat_level.py | 240 |
| `_load_driver_commissioning_config_for_level` | jasper/active_speaker/web_commissioning.py | 1671 |
| `_load_inputs` | jasper/cli/bass_extension_bench.py | 47 |
| `_load_json_object` | jasper/cli/active_speaker.py | 75 |
| `_load_linearization` | jasper/cli/active_speaker_emit_bench.py | 68 |
| `_load_measurement_baseline` | jasper/web/correction_setup.py | 2951 |
| `_load_or_build_acoustic_quality` | jasper/correction/evidence.py | 232 |
| `_load_packet` | jasper/cli/crossover_prescriber.py | 192 |
| `_load_round` | jasper/cli/round_views.py | 195 |
| `_load_rounds` | jasper/cli/round_views.py | 333 |
| `_load_saved_state` | jasper/active_speaker/baseline_profile.py | 1283 |
| `_load_sessions` | jasper/cli/active_speaker_attempts_replay.py | 382 |
| `_load_sound_profile` | jasper/calibration_agent/advisor_context.py | 242 |
| `_load_startup_config` | jasper/active_speaker/web_commissioning.py | 334 |
| `_load_state` | jasper/active_speaker/session_volume_plan.py | 648 |
| `_load_summed_commissioning_config` | jasper/active_speaker/web_commissioning.py | 1064 |
| `_load_topology_for_correction` | jasper/correction/runtime_safety.py | 48 |
| `_load_volume_safety_state` | jasper/web/correction_crossover_backend.py | 82 |
| `_loaded_state_payload` | jasper/active_speaker/startup_load.py | 780 |
| `_loads_json_object` | jasper/calibration_agent/model_client.py | 485 |
| `_normalized_graph_fingerprint` | jasper/active_speaker/runtime_contract.py | 4062 |
| `_operation_fingerprint` | jasper/active_speaker/commissioning_apply.py | 82 |
| `_optional_fingerprint` | jasper/audio_measurement/excitation_admission.py | 78 |
| `_payload_fingerprint` | jasper/audio_measurement/null_walk.py | 190 |
| `_previous_candidate_fingerprint` | jasper/web/correction_crossover_v2_status.py | 572 |
| `_protection_fingerprint` | jasper/active_speaker/commissioning_apply.py | 239 |
| `_read_fingerprinted_payload` | jasper/audio_measurement/excitation_admission.py | 107 |
| `_read_json_body` | jasper/web/correction_setup.py | 1411 |
| `_required_fingerprint` | jasper/audio_measurement/excitation_admission.py | 72 |
| `_sha256_fd` | jasper/audio_measurement/playback.py | 300 |
| `_sha256_file` | jasper/correction/fir_runtime.py | 38 |
| `_sha256_text` | jasper/audio_measurement/calibration.py | 237 |
| `_summed_fingerprint` | jasper/active_speaker/measurement.py | 320 |
| `_target_fingerprint` | jasper/active_speaker/measurement.py | 244 |
| `_topology_authority_fingerprint` | jasper/active_speaker/commissioning_receipt.py | 202 |
| `_with_fingerprint` | jasper/audio_measurement/excitation_admission.py | 101 |
| `_write_json_atomically` | jasper/audio_measurement/bundles.py | 176 |
| `_writer_lock_fingerprint` | jasper/active_speaker/commissioning_apply.py | 227 |
| `active_layer_a_fingerprint` | jasper/active_speaker/baseline_profile.py | 388 |
| `active_region_context_fingerprint` | jasper/active_speaker/commissioning_evidence.py | 161 |
| `active_region_threshold_profile_fingerprint` | jasper/active_speaker/commissioning_evidence.py | 145 |
| `admission_decision_fingerprint` | jasper/active_speaker/commissioning_receipt.py | 1050 |
| `apply_by_fingerprint` | jasper/active_speaker/wizard_client.py | 280 |
| `authority_fingerprint` | jasper/active_speaker/commissioning_receipt.py | 1066 |
| `baseline_candidate_fingerprint` | jasper/active_speaker/baseline_profile.py | 236 |
| `candidate_fingerprint` | jasper/active_speaker/crossover_v2/contracts.py | 769 |
| `capture_attempt_context_fingerprint` | jasper/active_speaker/commissioning_evidence.py | 977 |
| `commissioning_context_fingerprint` | jasper/active_speaker/commissioning_receipt.py | 889 |
| `comparison_set_fingerprint` | jasper/active_speaker/capture_geometry.py | 509 |
| `complete_isolated_driver_evidence_fingerprint` | jasper/active_speaker/commissioning_evidence_store.py | 1263 |
| `context_base_fingerprint_for` | jasper/active_speaker/commissioning_evidence.py | 495 |
| `context_fingerprint` | jasper/active_speaker/commissioning_verification.py | 199 |
| `crossover_preview_fingerprint` | jasper/active_speaker/crossover_preview.py | 92 |
| `delay_point_context_base_fingerprint` | jasper/active_speaker/commissioning_evidence.py | 2525 |
| `delay_point_target_fingerprint` | jasper/active_speaker/commissioning_evidence.py | 2498 |
| `entry_graph_fingerprint` | jasper/active_speaker/crossover_v2/coordinator.py | 220 |
| `entry_scope_fingerprint` | jasper/active_speaker/crossover_v2/session_graph.py | 142 |
| `excitation_plan_fingerprint` | jasper/active_speaker/commissioning_receipt.py | 1070 |
| `graph_fingerprint` | jasper/active_speaker/crossover_v2/session.py | 349 |
| `isolated_capture_context_base_fingerprint` | jasper/active_speaker/commissioning_evidence.py | 1093 |
| `isolated_capture_context_fingerprint` | jasper/active_speaker/commissioning_evidence.py | 1150 |
| `isolated_driver_evidence_target_fingerprint` | jasper/active_speaker/commissioning_evidence.py | 1044 |
| `json_fingerprint` | jasper/audio_measurement/evidence_identity.py | 78 |
| `measure_proposal_fingerprint` | jasper/active_speaker/crossover_v2_flow.py | 2818 |
| `measured_candidate_fingerprint` | jasper/active_speaker/passive_profile.py | 27 |
| `read_fingerprint` | jasper/active_speaker/capture_provenance.py | 324 |
| `region_evidence_preset_fingerprint` | jasper/active_speaker/commissioning_evidence.py | 784 |
| `running_graph_fingerprint` | jasper/active_speaker/commissioning_admission.py | 471 |
| `safety_profile_fingerprint` | jasper/active_speaker/commissioning_receipt.py | 1074 |
| `startup_load_evidence_fingerprint` | jasper/active_speaker/path_safety.py | 339 |
| `target_fingerprint_for` | jasper/active_speaker/commissioning_evidence.py | 486 |
| `topology_config_fingerprint` | jasper/active_speaker/baseline_profile.py | 266 |
| `tuning_scope_fingerprint` | jasper/active_speaker/crossover_v2/tuning_scope.py | 91 |


## 5. Refusal vocabulary census

### Table 5a0 — refusal-vocabulary defs, per naming pattern

| pattern | definitions | distinct files |
|---|---:|---:|
| `_gate` | 21 | 8 |
| `_refuse` | 13 | 13 |
| `_refused` | 9 | 9 |
| `_issue` | 8 | 8 |
| `_blocked` | 5 | 5 |
| `_verdict` | 3 | 2 |

### Table 5a — refusal-vocabulary def counts per file

| file | refusal-vocab defs | REFUSE/REASON/... consts | Error/Refused/.../Exception classes |
|---|---:|---:|---:|
| jasper/active_speaker/crossover_v2/refusal_copy.py | 0 | 44 | 0 |
| jasper/active_speaker/attempts_loop.py | 0 | 17 | 0 |
| jasper/active_speaker/seat_level_ramp.py | 0 | 15 | 1 |
| jasper/cli/measure.py | 1 | 14 | 1 |
| jasper/active_speaker/crossover_v2/capture_dispatch.py | 8 | 6 | 0 |
| jasper/active_speaker/driver_base_trim.py | 0 | 12 | 1 |
| jasper/active_speaker/delta_probe.py | 0 | 11 | 0 |
| jasper/active_speaker/crossover_v2/journey.py | 0 | 11 | 0 |
| jasper/active_speaker/wizard_client.py | 1 | 9 | 0 |
| jasper/correction/envelope.py | 2 | 8 | 0 |
| jasper/active_speaker/audition.py | 1 | 8 | 1 |
| jasper/audio_measurement/interference_nulls.py | 0 | 9 | 0 |
| jasper/active_speaker/crossover_v2/round_captures.py | 0 | 7 | 1 |
| jasper/audio_measurement/program.py | 0 | 8 | 0 |
| jasper/active_speaker/crossover_v2/gate_sweep.py | 0 | 7 | 0 |
| jasper/cli/null_door.py | 0 | 6 | 1 |
| jasper/active_speaker/crossover_v2/close_reference.py | 2 | 5 | 0 |
| jasper/active_speaker/crossover_v2/spatial.py | 0 | 6 | 0 |
| jasper/audio_measurement/program_analysis.py | 4 | 0 | 1 |
| jasper/active_speaker/crossover_v2/delay_landscape.py | 0 | 4 | 1 |
| jasper/active_speaker/driver_acoustics.py | 0 | 4 | 1 |
| jasper/active_speaker/crossover_v2/contracts.py | 0 | 0 | 5 |
| jasper/cli/seat_level.py | 1 | 4 | 0 |
| experiments/usb-turntable/vendor/usb_turntable/errors.py | 0 | 0 | 4 |
| jasper/active_speaker/web_commissioning.py | 2 | 0 | 2 |
| jasper/active_speaker/crossover_v2/admission.py | 0 | 3 | 1 |
| jasper/active_speaker/program_admission.py | 2 | 0 | 2 |
| jasper/active_speaker/crossover_v2/door.py | 0 | 3 | 1 |
| jasper/audio_measurement/calibration.py | 0 | 1 | 3 |
| jasper/active_speaker/crossover_v2/feature_classifier.py | 2 | 1 | 1 |
| jasper/web/correction_crossover_v2.py | 2 | 0 | 2 |
| jasper/cli/delay_sweep.py | 1 | 3 | 0 |
| jasper/active_speaker/round_bank.py | 0 | 3 | 1 |
| jasper/cli/round_views.py | 0 | 3 | 0 |
| jasper/active_speaker/crossover_v2/evidence_packet.py | 2 | 0 | 1 |
| jasper/audio_measurement/playback.py | 0 | 0 | 3 |
| jasper/cli/forward_model.py | 1 | 2 | 0 |
| jasper/active_speaker/delay_sweep.py | 0 | 3 | 0 |
| jasper/active_speaker/measured_candidate.py | 1 | 0 | 2 |
| jasper/cli/gate_sweep.py | 0 | 3 | 0 |
| jasper/active_speaker/program_playback.py | 0 | 0 | 2 |
| jasper/active_speaker/crossover_alignment.py | 1 | 1 | 0 |
| jasper/active_speaker/angle_capture_spool.py | 1 | 0 | 1 |
| jasper/active_speaker/measured_crossover_candidate.py | 1 | 0 | 1 |
| jasper/active_speaker/excitation_safety_plan.py | 0 | 0 | 2 |
| jasper/active_speaker/crossover_v2/intervention.py | 0 | 0 | 2 |
| jasper/active_speaker/commissioning_runtime.py | 0 | 0 | 2 |
| jasper/active_speaker/driver_safety.py | 0 | 0 | 2 |
| jasper/active_speaker/setup_status.py | 2 | 0 | 0 |
| jasper/active_speaker/angle_capture.py | 1 | 0 | 1 |
| jasper/active_speaker/crossover_v2/measurement_phase.py | 0 | 1 | 1 |
| jasper/audio_measurement/spatial_combine.py | 1 | 0 | 1 |
| jasper/correction/evidence.py | 2 | 0 | 0 |
| jasper/cli/basic_profile.py | 2 | 0 | 0 |
| jasper/cli/round.py | 0 | 2 | 0 |
| jasper/cli/close_reference.py | 0 | 2 | 0 |
| jasper/correction/level_match.py | 0 | 0 | 2 |
| jasper/active_speaker/crossover_v2_flow.py | 1 | 0 | 1 |
| jasper/audio_measurement/delay_graph.py | 1 | 0 | 1 |
| jasper/active_speaker/crossover_level_run.py | 0 | 0 | 2 |
| jasper/audio_measurement/admitted_playback.py | 0 | 0 | 2 |
| jasper/correction/runtime_safety.py | 1 | 0 | 1 |
| jasper/active_speaker/crossover_v2/blend_prescription.py | 1 | 0 | 1 |
| jasper/active_speaker/crossover_v2/program_transaction.py | 0 | 0 | 1 |
| jasper/web/correction_crossover_v2_wired.py | 0 | 1 | 0 |
| jasper/audio_measurement/quality.py | 0 | 0 | 1 |
| jasper/active_speaker/crossover_v2/capture_source.py | 0 | 0 | 1 |
| jasper/active_speaker/arm_walk.py | 0 | 0 | 1 |
| jasper/active_speaker/bench/derivation.py | 0 | 0 | 1 |
| jasper/active_speaker/candidate_bank.py | 0 | 0 | 1 |
| jasper/correction/session.py | 0 | 0 | 1 |
| jasper/active_speaker/session_volume_plan.py | 0 | 0 | 1 |
| jasper/correction/runtime_integrity.py | 1 | 0 | 0 |
| jasper/audio_measurement/bundles.py | 0 | 0 | 1 |
| jasper/web/correction_tuning.py | 0 | 0 | 1 |
| jasper/active_speaker/frequency_view.py | 0 | 0 | 1 |
| jasper/active_speaker/crossover_v2/session.py | 0 | 0 | 1 |
| jasper/cli/crossover_prescriber.py | 1 | 0 | 0 |
| jasper/web/correction_crossover_backend.py | 0 | 0 | 1 |
| jasper/calibration_agent/actions.py | 1 | 0 | 0 |
| jasper/audio_measurement/measurement_geometry.py | 0 | 0 | 1 |
| jasper/active_speaker/crossover_v2/round_inputs.py | 0 | 0 | 1 |
| jasper/active_speaker/crossover_v2/alignment_prescription.py | 0 | 0 | 1 |
| jasper/active_speaker/commissioning_service.py | 0 | 0 | 1 |
| jasper/active_speaker/measurement_programs.py | 0 | 0 | 1 |
| jasper/audio_measurement/wired_capture.py | 0 | 0 | 1 |
| jasper/web/balance_volume_guard.py | 0 | 0 | 1 |
| jasper/active_speaker/speech_stimulus.py | 0 | 0 | 1 |
| jasper/active_speaker/seat_level_reference.py | 0 | 0 | 1 |
| jasper/active_speaker/crossover_v2/harmonic_evidence.py | 0 | 0 | 1 |
| jasper/calibration_agent/tools.py | 0 | 0 | 1 |
| jasper/calibration_agent/model_client.py | 0 | 0 | 1 |
| jasper/cli/active_speaker_attempts_replay.py | 0 | 0 | 1 |
| jasper/active_speaker/commissioning_host.py | 0 | 0 | 1 |
| jasper/active_speaker/commissioning_evidence.py | 0 | 0 | 1 |
| jasper/audio_measurement/excitation_artifacts.py | 0 | 0 | 1 |
| jasper/active_speaker/commissioning_receipt.py | 0 | 0 | 1 |
| jasper/active_speaker/crossover_v2/driver_prescription.py | 1 | 0 | 0 |
| jasper/cli/audition.py | 1 | 0 | 0 |
| jasper/active_speaker/commissioning_apply.py | 0 | 0 | 1 |
| jasper/active_speaker/crossover_v2/forward_model.py | 0 | 0 | 1 |
| jasper/active_speaker/crossover_v2/topology_prescription.py | 0 | 0 | 1 |
| jasper/active_speaker/commissioning_lifecycle.py | 0 | 0 | 1 |
| jasper/attribution/storage.py | 0 | 0 | 1 |
| jasper/active_speaker/crossover_v2/position_cycle.py | 0 | 0 | 1 |
| jasper/attribution/findings.py | 0 | 0 | 1 |
| jasper/active_speaker/crossover_v2/coordinator.py | 0 | 0 | 1 |
| jasper/correction/bundle_tools.py | 0 | 0 | 1 |
| jasper/active_speaker/profile.py | 0 | 0 | 1 |
| jasper/active_speaker/commissioning_isolated_producer.py | 0 | 0 | 1 |
| jasper/attribution/mechanisms.py | 0 | 0 | 1 |
| jasper/audio_measurement/alignment.py | 0 | 0 | 1 |
| jasper/active_speaker/baseline_profile.py | 1 | 0 | 0 |
| jasper/active_speaker/commissioning_evidence_store.py | 0 | 0 | 1 |
| jasper/active_speaker/design_draft.py | 0 | 0 | 1 |
| jasper/active_speaker/crossover_v2/feature_optics.py | 0 | 1 | 0 |
| jasper/active_speaker/commission_ramp.py | 1 | 0 | 0 |
| jasper/active_speaker/branch_peak.py | 0 | 0 | 1 |
| jasper/cli/angle_capture.py | 1 | 0 | 0 |
| jasper/active_speaker/crossover_v2/feature_classification.py | 0 | 1 | 0 |
| jasper/web/correction_crossover_v2_relay.py | 0 | 1 | 0 |
| jasper/web/correction_measurements.py | 0 | 0 | 1 |
| jasper/active_speaker/crossover_v2/ring_projection.py | 0 | 0 | 1 |
| jasper/audio_measurement/null_walk.py | 0 | 0 | 1 |
| jasper/calibration_agent/response.py | 1 | 0 | 0 |
| jasper/active_speaker/model_error_store.py | 0 | 0 | 1 |
| jasper/correction/fir_runtime.py | 0 | 0 | 1 |
| jasper/active_speaker/commission_wiring.py | 0 | 0 | 1 |
| jasper/active_speaker/crossover_v2/session_graph.py | 0 | 0 | 1 |
| jasper/active_speaker/crossover_v2/programs.py | 0 | 0 | 1 |
| jasper/active_speaker/driver_pad.py | 0 | 0 | 1 |
| jasper/active_speaker/bench/compare.py | 0 | 0 | 1 |
| jasper/active_speaker/commissioning_verification.py | 0 | 0 | 1 |
| jasper/active_speaker/commissioning_admission.py | 0 | 0 | 1 |
| jasper/attribution/session_identity.py | 0 | 0 | 1 |
| jasper/active_speaker/commissioning_run.py | 0 | 0 | 1 |
| jasper/active_speaker/crossover_v2/prescription_spool.py | 1 | 0 | 0 |
| jasper/active_speaker/level_trim.py | 0 | 0 | 1 |
| jasper/correction/coordinator.py | 0 | 0 | 1 |
| jasper/active_speaker/bench/loop.py | 0 | 0 | 1 |
| jasper/correction/interop.py | 0 | 0 | 1 |
| jasper/audio_measurement/evidence_identity.py | 0 | 0 | 1 |
| jasper/web/correction_crossover_v2_republish.py | 1 | 0 | 0 |
| **TOTAL** | 59 | 250 | 127 |

(defs matched across 39 files; consts matched across 39 files)


### Table 5b — exception-like classes (127), base class, __init__ dup check

| file | class | base | __init__ identical to |
|---|---|---|---|
| experiments/usb-turntable/vendor/usb_turntable/errors.py | TurntableError | RuntimeError | (no explicit __init__) |
| experiments/usb-turntable/vendor/usb_turntable/errors.py | PortDiscoveryError | TurntableError | (no explicit __init__) |
| experiments/usb-turntable/vendor/usb_turntable/errors.py | ProtocolError | TurntableError | (no explicit __init__) |
| experiments/usb-turntable/vendor/usb_turntable/errors.py | StartupSynchronizationError | ProtocolError | (no explicit __init__) |
| jasper/active_speaker/angle_capture.py | LateralWalkRefused | CrossoverV2FlowError | group #1 (3 identical) |
| jasper/active_speaker/angle_capture_spool.py | AngleRequestRefused | CrossoverV2FlowError | group #1 (3 identical) |
| jasper/active_speaker/arm_walk.py | ArmWalkRefused | Exception | (no explicit __init__) |
| jasper/active_speaker/audition.py | AuditionRefused | RuntimeError | group #2 (3 identical) |
| jasper/active_speaker/bench/compare.py | EmitComparisonError | ValueError | (no explicit __init__) |
| jasper/active_speaker/bench/derivation.py | EmitDerivationError | ValueError | (no explicit __init__) |
| jasper/active_speaker/bench/loop.py | EmitLoopError | RuntimeError | (no explicit __init__) |
| jasper/active_speaker/branch_peak.py | BranchPeakError | RuntimeError | (no explicit __init__) |
| jasper/active_speaker/candidate_bank.py | CandidateBankRefusal | LookupError | group #3 (7 identical) |
| jasper/active_speaker/commission_wiring.py | CommissionPresetResolutionError | ValueError | unique __init__ |
| jasper/active_speaker/commissioning_admission.py | ActiveCommissioningAdmissionError | RuntimeError | unique __init__ |
| jasper/active_speaker/commissioning_apply.py | CommissioningApplyError | RuntimeError | group #3 (7 identical) |
| jasper/active_speaker/commissioning_evidence.py | CommissioningEvidenceError | ValueError | (no explicit __init__) |
| jasper/active_speaker/commissioning_evidence_store.py | CommissioningEvidenceStoreError | RuntimeError | unique __init__ |
| jasper/active_speaker/commissioning_host.py | CommissioningHostError | RuntimeError | group #3 (7 identical) |
| jasper/active_speaker/commissioning_isolated_producer.py | IsolatedCapturePromotionError | ValueError | (no explicit __init__) |
| jasper/active_speaker/commissioning_lifecycle.py | CommissioningLifecycleError | ValueError | (no explicit __init__) |
| jasper/active_speaker/commissioning_receipt.py | CommissioningReceiptError | ValueError | (no explicit __init__) |
| jasper/active_speaker/commissioning_run.py | CommissioningRunError | RuntimeError | (no explicit __init__) |
| jasper/active_speaker/commissioning_runtime.py | CommissioningRuntimeError | ValueError | (no explicit __init__) |
| jasper/active_speaker/commissioning_runtime.py | _OperationFailure | RuntimeError | group #3 (7 identical) |
| jasper/active_speaker/commissioning_service.py | CommissioningServiceError | ValueError | group #3 (7 identical) |
| jasper/active_speaker/commissioning_verification.py | CommissioningVerificationError | RuntimeError | group #3 (7 identical) |
| jasper/active_speaker/crossover_level_run.py | CrossoverLevelRunError | RuntimeError | (no explicit __init__) |
| jasper/active_speaker/crossover_level_run.py | CrossoverLevelRunFailure | str, Enum | (no explicit __init__) |
| jasper/active_speaker/crossover_v2/admission.py | AttemptOverspendError | RuntimeError | (no explicit __init__) |
| jasper/active_speaker/crossover_v2/alignment_prescription.py | AlignmentPrescriptionRefused | ValueError | group #7 (3 identical) |
| jasper/active_speaker/crossover_v2/blend_prescription.py | BlendPrescriptionRefused | ValueError | unique __init__ |
| jasper/active_speaker/crossover_v2/capture_source.py | CaptureBeginRefused | RuntimeError | unique __init__ |
| jasper/active_speaker/crossover_v2/contracts.py | CrossoverV2FlowError | RuntimeError | (no explicit __init__) |
| jasper/active_speaker/crossover_v2/contracts.py | CrossoverV2ContractError | ValueError | (no explicit __init__) |
| jasper/active_speaker/crossover_v2/contracts.py | NoCrossoverSectionsError | CrossoverV2ContractError | (no explicit __init__) |
| jasper/active_speaker/crossover_v2/contracts.py | CandidateFcDisagreementError | CrossoverV2ContractError | (no explicit __init__) |
| jasper/active_speaker/crossover_v2/contracts.py | PlanRefusal | object | unique __init__ |
| jasper/active_speaker/crossover_v2/coordinator.py | RoundRefusal | object | (no explicit __init__) |
| jasper/active_speaker/crossover_v2/delay_landscape.py | DelayLandscapeError | ValueError | unique __init__ |
| jasper/active_speaker/crossover_v2/door.py | MeasurementDoorRefused | RuntimeError | group #2 (3 identical) |
| jasper/active_speaker/crossover_v2/evidence_packet.py | CrossoverEvidencePacketError | ValueError | (no explicit __init__) |
| jasper/active_speaker/crossover_v2/feature_classifier.py | FeatureClassificationRefused | RuntimeError | group #12 (2 identical) |
| jasper/active_speaker/crossover_v2/forward_model.py | ForwardModelError | ValueError | unique __init__ |
| jasper/active_speaker/crossover_v2/harmonic_evidence.py | HarmonicEvidenceRefused | Exception | unique __init__ |
| jasper/active_speaker/crossover_v2/intervention.py | PlannerError | CrossoverV2ContractError | (no explicit __init__) |
| jasper/active_speaker/crossover_v2/intervention.py | PlannerInputError | PlannerError | (no explicit __init__) |
| jasper/active_speaker/crossover_v2/measurement_phase.py | NoPhaseForMeasurementError | RuntimeError | unique __init__ |
| jasper/active_speaker/crossover_v2/position_cycle.py | PositionCycleError | ValueError | (no explicit __init__) |
| jasper/active_speaker/crossover_v2/program_transaction.py | StimulusCaptureError | RuntimeError | (no explicit __init__) |
| jasper/active_speaker/crossover_v2/programs.py | NoProgramForPhaseError | RuntimeError | (no explicit __init__) |
| jasper/active_speaker/crossover_v2/ring_projection.py | RingProjectionRefused | RuntimeError | group #12 (2 identical) |
| jasper/active_speaker/crossover_v2/round_captures.py | RoundCapturesRefused | Exception | unique __init__ |
| jasper/active_speaker/crossover_v2/round_inputs.py | RoundViewsError | CrossoverEvidencePacketError | (no explicit __init__) |
| jasper/active_speaker/crossover_v2/session.py | SessionStateError | RuntimeError | (no explicit __init__) |
| jasper/active_speaker/crossover_v2/session_graph.py | SessionGraphError | RuntimeError | (no explicit __init__) |
| jasper/active_speaker/crossover_v2/topology_prescription.py | TopologyPrescriptionRefused | ValueError | group #7 (3 identical) |
| jasper/active_speaker/crossover_v2_flow.py | RecordModelError | Protocol | (no explicit __init__) |
| jasper/active_speaker/design_draft.py | ActiveSpeakerDesignDraftError | ValueError | (no explicit __init__) |
| jasper/active_speaker/driver_acoustics.py | DriverAcousticsError | ValueError | (no explicit __init__) |
| jasper/active_speaker/driver_base_trim.py | DriverBaseTrimError | ValueError | group #1 (3 identical) |
| jasper/active_speaker/driver_pad.py | DriverPadError | ValueError | (no explicit __init__) |
| jasper/active_speaker/driver_safety.py | DriverSafetyProfileError | ValueError | (no explicit __init__) |
| jasper/active_speaker/driver_safety.py | DriverSafetyProfileStaleLowLimitError | DriverSafetyProfileError | (no explicit __init__) |
| jasper/active_speaker/excitation_safety_plan.py | ExcitationSafetyPlanError | ValueError | (no explicit __init__) |
| jasper/active_speaker/excitation_safety_plan.py | ExcitationSafetyPlanRefusal | str, Enum | (no explicit __init__) |
| jasper/active_speaker/frequency_view.py | FrequencyViewError | ValueError | (no explicit __init__) |
| jasper/active_speaker/level_trim.py | LevelTrimError | ValueError | (no explicit __init__) |
| jasper/active_speaker/measured_candidate.py | MeasuredCandidateError | ValueError | (no explicit __init__) |
| jasper/active_speaker/measured_candidate.py | MeasuredCandidateEvaluationError | MeasuredCandidateError | group #3 (7 identical) |
| jasper/active_speaker/measured_crossover_candidate.py | MeasuredCrossoverCandidateError | ValueError | unique __init__ |
| jasper/active_speaker/measurement_programs.py | UnknownProgramError | ValueError | unique __init__ |
| jasper/active_speaker/model_error_store.py | ModelErrorConflictError | RuntimeError | (no explicit __init__) |
| jasper/active_speaker/profile.py | ActiveSpeakerConfigError | ValueError | (no explicit __init__) |
| jasper/active_speaker/program_admission.py | ProgramAdmissionRefusal | str, Enum | (no explicit __init__) |
| jasper/active_speaker/program_admission.py | ProgramAdmissionError | ValueError | (no explicit __init__) |
| jasper/active_speaker/program_playback.py | ProgramPlaybackError | RuntimeError | (no explicit __init__) |
| jasper/active_speaker/program_playback.py | ProgramPlaybackRefused | ProgramPlaybackError | unique __init__ |
| jasper/active_speaker/round_bank.py | RoundBankError | Exception | unique __init__ |
| jasper/active_speaker/seat_level_ramp.py | SeatLevelRampError | RuntimeError | (no explicit __init__) |
| jasper/active_speaker/seat_level_reference.py | SeatLevelTargetError | ValueError | (no explicit __init__) |
| jasper/active_speaker/session_volume_plan.py | SessionVolumePlanError | RuntimeError | (no explicit __init__) |
| jasper/active_speaker/speech_stimulus.py | SpeechStimulusError | RuntimeError | (no explicit __init__) |
| jasper/active_speaker/web_commissioning.py | AutomaticDriverConfigRestoreError | RuntimeError | (no explicit __init__) |
| jasper/active_speaker/web_commissioning.py | AutomaticSummedConfigRestoreError | RuntimeError | (no explicit __init__) |
| jasper/attribution/findings.py | FindingError | ValueError | (no explicit __init__) |
| jasper/attribution/mechanisms.py | MechanismError | ValueError | (no explicit __init__) |
| jasper/attribution/session_identity.py | SessionIdentityError | ValueError | (no explicit __init__) |
| jasper/attribution/storage.py | FindingStorageError | RuntimeError | (no explicit __init__) |
| jasper/audio_measurement/admitted_playback.py | PlaybackAdmissionRefused | RuntimeError | unique __init__ |
| jasper/audio_measurement/admitted_playback.py | GeneratedStimulusError | RuntimeError | unique __init__ |
| jasper/audio_measurement/alignment.py | AlignmentError | RuntimeError | unique __init__ |
| jasper/audio_measurement/bundles.py | BundleError | RuntimeError | (no explicit __init__) |
| jasper/audio_measurement/calibration.py | CalibrationLookupError | RuntimeError | (no explicit __init__) |
| jasper/audio_measurement/calibration.py | CalibrationNotFoundError | CalibrationLookupError | (no explicit __init__) |
| jasper/audio_measurement/calibration.py | CalibrationUpstreamError | CalibrationLookupError | (no explicit __init__) |
| jasper/audio_measurement/delay_graph.py | DelayGraphProofError | NullWalkError | unique __init__ |
| jasper/audio_measurement/evidence_identity.py | EvidenceIdentityError | ValueError | (no explicit __init__) |
| jasper/audio_measurement/excitation_artifacts.py | AdmissionArtifactError | RuntimeError | unique __init__ |
| jasper/audio_measurement/measurement_geometry.py | GeometryFieldError | ValueError | unique __init__ |
| jasper/audio_measurement/null_walk.py | NullWalkError | ValueError | (no explicit __init__) |
| jasper/audio_measurement/playback.py | SweepPlaybackError | RuntimeError | unique __init__ |
| jasper/audio_measurement/playback.py | WavSourceError | RuntimeError | unique __init__ |
| jasper/audio_measurement/playback.py | _ProcessWaitFailure | RuntimeError | (no explicit __init__) |
| jasper/audio_measurement/program_analysis.py | ConfiguredPathConditioningError | ValueError | unique __init__ |
| jasper/audio_measurement/quality.py | CaptureQualityError | ValueError | unique __init__ |
| jasper/audio_measurement/spatial_combine.py | EchoInputError | ValueError | unique __init__ |
| jasper/audio_measurement/wired_capture.py | WiredCaptureError | RuntimeError | (no explicit __init__) |
| jasper/calibration_agent/model_client.py | AdvisorModelError | RuntimeError | (no explicit __init__) |
| jasper/calibration_agent/tools.py | AgentToolError | RuntimeError | (no explicit __init__) |
| jasper/cli/active_speaker_attempts_replay.py | ReplayError | Exception | (no explicit __init__) |
| jasper/cli/measure.py | MeasureFlagError | ValueError | group #2 (3 identical) |
| jasper/cli/null_door.py | NullDoorRefused | RuntimeError | group #7 (3 identical) |
| jasper/correction/bundle_tools.py | BundleToolError | RuntimeError | (no explicit __init__) |
| jasper/correction/coordinator.py | MeasurementWindowError | RuntimeError | (no explicit __init__) |
| jasper/correction/fir_runtime.py | FirRuntimeError | ValueError | (no explicit __init__) |
| jasper/correction/interop.py | InteropError | ValueError | (no explicit __init__) |
| jasper/correction/level_match.py | RampRefusal | object | (no explicit __init__) |
| jasper/correction/level_match.py | LevelMatchRefused | RuntimeError | unique __init__ |
| jasper/correction/runtime_safety.py | CorrectionRuntimeSafetyError | RuntimeError | (no explicit __init__) |
| jasper/correction/session.py | SessionBusyError | RuntimeError | (no explicit __init__) |
| jasper/web/balance_volume_guard.py | VolumeGuardError | RuntimeError | (no explicit __init__) |
| jasper/web/correction_crossover_backend.py | MeasurementJourneyResetRefused | RuntimeError | unique __init__ |
| jasper/web/correction_crossover_v2.py | CrossoverV2Refused | ValueError | unique __init__ |
| jasper/web/correction_crossover_v2.py | CrossoverV2LocalSeamError | RuntimeError | (no explicit __init__) |
| jasper/web/correction_measurements.py | MeasurementViewRequestError | ValueError | (no explicit __init__) |
| jasper/web/correction_tuning.py | TuningProviderError | RuntimeError | (no explicit __init__) |

6 groups of classes share byte-identical `__init__` bodies.



## 6. Serialization census

### Table 6a — serialization method/function counts per file

| file | to/from_dict-ish methods | serialize/deserialize/validate_*record* funcs |
|---|---:|---:|
| jasper/active_speaker/commissioning_evidence.py | 30 | 0 |
| jasper/active_speaker/commissioning_receipt.py | 18 | 0 |
| jasper/active_speaker/profile.py | 18 | 0 |
| jasper/active_speaker/crossover_v2/sweep_spec.py | 12 | 0 |
| jasper/audio_measurement/excitation_admission.py | 10 | 0 |
| jasper/active_speaker/flat_spec_views.py | 8 | 0 |
| jasper/active_speaker/crossover_v2/round_views.py | 8 | 0 |
| jasper/audio_measurement/evidence_identity.py | 8 | 0 |
| jasper/active_speaker/linearization_fit.py | 7 | 0 |
| jasper/active_speaker/crossover_v2/contracts.py | 7 | 0 |
| jasper/active_speaker/flat_spec.py | 7 | 0 |
| jasper/attribution/findings.py | 6 | 0 |
| jasper/audio_measurement/calibration.py | 5 | 0 |
| jasper/active_speaker/attempts_loop.py | 5 | 0 |
| jasper/active_speaker/runtime_contract.py | 5 | 0 |
| jasper/audio_measurement/null_walk.py | 5 | 0 |
| jasper/audio_measurement/program.py | 4 | 0 |
| jasper/audio_measurement/program_analysis.py | 3 | 0 |
| jasper/active_speaker/excitation_safety_plan.py | 3 | 0 |
| experiments/usb-turntable/vendor/usb_turntable/controller.py | 3 | 0 |
| jasper/active_speaker/crossover_v2/blend_prescription.py | 3 | 0 |
| jasper/active_speaker/measured_candidate.py | 3 | 0 |
| jasper/active_speaker/measured_crossover_candidate.py | 3 | 0 |
| jasper/active_speaker/playback_route.py | 3 | 0 |
| jasper/active_speaker/driver_acoustics.py | 3 | 0 |
| jasper/active_speaker/program_admission.py | 3 | 0 |
| jasper/active_speaker/crossover_v2/round_evidence.py | 3 | 0 |
| jasper/audio_measurement/quality.py | 2 | 0 |
| jasper/correction/acceptance.py | 2 | 0 |
| jasper/calibration_agent/proposal_sim.py | 2 | 0 |
| jasper/correction/applied_speaker_evidence.py | 2 | 0 |
| jasper/active_speaker/delta_probe.py | 2 | 0 |
| jasper/attribution/session_identity.py | 2 | 0 |
| jasper/correction/runtime_integrity.py | 2 | 0 |
| jasper/correction/strategy.py | 2 | 0 |
| jasper/audio_measurement/olive_metrics.py | 2 | 0 |
| jasper/audio_measurement/sweep.py | 2 | 0 |
| jasper/active_speaker/crossover_v2/verification.py | 2 | 0 |
| jasper/active_speaker/crossover_v2/driver_prescription.py | 2 | 0 |
| jasper/correction/household_mic.py | 2 | 0 |
| jasper/audio_measurement/admitted_playback.py | 2 | 0 |
| jasper/active_speaker/commissioning_run.py | 2 | 0 |
| jasper/active_speaker/crossover_level_run.py | 2 | 0 |
| jasper/correction/browser_audio.py | 2 | 0 |
| jasper/active_speaker/crossover_v2/blend_correction.py | 2 | 0 |
| jasper/active_speaker/commissioning_lifecycle.py | 2 | 0 |
| jasper/active_speaker/bench/loop.py | 2 | 0 |
| jasper/correction/level_match.py | 2 | 0 |
| jasper/audio_measurement/delay_graph.py | 2 | 0 |
| jasper/active_speaker/commissioning_admission.py | 2 | 0 |
| jasper/active_speaker/seat_level_reference.py | 2 | 0 |
| jasper/active_speaker/crossover_alignment.py | 2 | 0 |
| jasper/audio_measurement/frame_fit.py | 2 | 0 |
| jasper/audio_measurement/measurement_geometry.py | 2 | 0 |
| jasper/active_speaker/crossover_v2/alignment_prescription.py | 1 | 0 |
| jasper/active_speaker/crossover_v2/durable_state.py | 1 | 0 |
| jasper/correction/variance_cap.py | 1 | 0 |
| jasper/active_speaker/crossover_v2/plan_assembly.py | 1 | 0 |
| jasper/active_speaker/seat_level_ramp.py | 1 | 0 |
| jasper/audio_measurement/frame_ledger.py | 1 | 0 |
| jasper/active_speaker/bench/derivation.py | 1 | 0 |
| jasper/active_speaker/crossover_v2/delay_landscape.py | 1 | 0 |
| jasper/active_speaker/capture_provenance.py | 1 | 0 |
| jasper/active_speaker/runtime_convergence.py | 1 | 0 |
| jasper/active_speaker/measurement_archive.py | 1 | 0 |
| experiments/usb-turntable/vendor/usb_turntable/discovery.py | 1 | 0 |
| jasper/active_speaker/crossover_v2/feature_classification.py | 1 | 0 |
| jasper/active_speaker/crossover_v2/forward_model.py | 1 | 0 |
| jasper/audio_measurement/spatial_combine.py | 1 | 0 |
| jasper/active_speaker/driver_protection.py | 1 | 0 |
| jasper/active_speaker/driver_safety.py | 1 | 0 |
| jasper/correction/session.py | 1 | 0 |
| jasper/correction/replay_artifacts.py | 1 | 0 |
| jasper/cli/active_speaker_attempts_replay.py | 1 | 0 |
| jasper/correction/confidence.py | 1 | 0 |
| jasper/correction/bundles.py | 1 | 0 |
| jasper/active_speaker/frequency_view.py | 1 | 0 |
| jasper/active_speaker/bench/compare.py | 1 | 0 |
| jasper/audio_measurement/bundles.py | 1 | 0 |
| jasper/audio_measurement/analysis.py | 1 | 0 |
| jasper/calibration_agent/key_provisioning.py | 1 | 0 |
| jasper/active_speaker/crossover_v2/topology_prescription.py | 1 | 0 |
| jasper/audio_measurement/ramp.py | 1 | 0 |
| jasper/active_speaker/path_safety.py | 1 | 0 |
| **TOTAL** | 277 | 0 |

### Table 6b — breakdown by exact name

| name | count |
|---|---:|
| `to_dict` | 204 |
| `from_mapping` | 49 |
| `from_dict` | 23 |
| `to_json` | 1 |

### Table 6c — dataclass serialization coverage

- Total classes decorated `@dataclass` in scope: 446
- Methods (to_dict/from_dict/...) defined on a `@dataclass` class: 274
- Methods defined on a non-dataclass class: 3
- Dataclasses with `to_dict`: 201
- Dataclasses with `from_dict`: 23
- Dataclasses with `to_dict` but NO `from_dict`: 179

| file | class |
|---|---|
| experiments/usb-turntable/vendor/usb_turntable/controller.py | OffsetAngleResult |
| experiments/usb-turntable/vendor/usb_turntable/controller.py | OperationResult |
| experiments/usb-turntable/vendor/usb_turntable/controller.py | ProbeResult |
| experiments/usb-turntable/vendor/usb_turntable/discovery.py | DeviceInfo |
| jasper/active_speaker/attempts_loop.py | AttemptBudget |
| jasper/active_speaker/attempts_loop.py | AttemptIntegrity |
| jasper/active_speaker/attempts_loop.py | AttemptRecord |
| jasper/active_speaker/attempts_loop.py | FloorStats |
| jasper/active_speaker/attempts_loop.py | LoopDecision |
| jasper/active_speaker/bench/compare.py | BranchComparison |
| jasper/active_speaker/bench/derivation.py | BranchStep |
| jasper/active_speaker/bench/loop.py | EmitLoopReport |
| jasper/active_speaker/bench/loop.py | RenderArm |
| jasper/active_speaker/capture_provenance.py | CaptureProvenance |
| jasper/active_speaker/commissioning_admission.py | ActiveCaptureAdmissionHandoff |
| jasper/active_speaker/commissioning_evidence.py | AdmittedIsolatedDriverCapture |
| jasper/active_speaker/commissioning_evidence.py | AdmittedRegionCapture |
| jasper/active_speaker/commissioning_evidence.py | CommissioningEvidenceAuthority |
| jasper/active_speaker/commissioning_evidence.py | CompleteCommissioningEvidence |
| jasper/active_speaker/commissioning_evidence.py | CompleteIsolatedDriverEvidence |
| jasper/active_speaker/commissioning_evidence.py | DelayPointEvidence |
| jasper/active_speaker/commissioning_evidence.py | DelayWalkEvidence |
| jasper/active_speaker/commissioning_evidence.py | DriverEvidenceTarget |
| jasper/active_speaker/commissioning_evidence.py | IsolatedDriverEvidence |
| jasper/active_speaker/commissioning_evidence.py | RegionCommissioningEvidence |
| jasper/active_speaker/commissioning_evidence.py | RegionEvidencePlan |
| jasper/active_speaker/commissioning_evidence.py | RegionEvidenceTarget |
| jasper/active_speaker/commissioning_evidence.py | RegionGeometryAttestation |
| jasper/active_speaker/commissioning_evidence.py | StationaryRegionEvidence |
| jasper/active_speaker/commissioning_evidence.py | _AdmittedExcitationProofCore |
| jasper/active_speaker/commissioning_lifecycle.py | CommissioningTransition |
| jasper/active_speaker/commissioning_receipt.py | AdmittedCaptureProof |
| jasper/active_speaker/commissioning_receipt.py | AppliedCandidateProof |
| jasper/active_speaker/commissioning_receipt.py | CommissioningEligibilityReceipt |
| jasper/active_speaker/commissioning_receipt.py | CommissioningHardwareIdentity |
| jasper/active_speaker/commissioning_receipt.py | CommissioningProofProvenance |
| jasper/active_speaker/commissioning_receipt.py | CommissioningRollbackEvidence |
| jasper/active_speaker/commissioning_receipt.py | PostApplyTargetVerification |
| jasper/active_speaker/commissioning_receipt.py | RequiredTargetPlan |
| jasper/active_speaker/commissioning_receipt.py | RequiredVerificationTarget |
| jasper/active_speaker/commissioning_run.py | CommissioningLiveMutation |
| jasper/active_speaker/crossover_alignment.py | CrossoverAlignmentProposal |
| jasper/active_speaker/crossover_alignment.py | ResolvedMode |
| jasper/active_speaker/crossover_v2/alignment_prescription.py | AlignmentPrescription |
| jasper/active_speaker/crossover_v2/blend_correction.py | BlendCorrection |
| jasper/active_speaker/crossover_v2/blend_correction.py | BlendRegionReading |
| jasper/active_speaker/crossover_v2/blend_prescription.py | BlendPrescription |
| jasper/active_speaker/crossover_v2/blend_prescription.py | PositionalSupport |
| jasper/active_speaker/crossover_v2/contracts.py | AdoptionDecision |
| jasper/active_speaker/crossover_v2/contracts.py | CandidateAcousticContext |
| jasper/active_speaker/crossover_v2/contracts.py | InterventionProposal |
| jasper/active_speaker/crossover_v2/contracts.py | PlanRefusal |
| jasper/active_speaker/crossover_v2/contracts.py | RoundReceipt |
| jasper/active_speaker/crossover_v2/contracts.py | VerificationResult |
| jasper/active_speaker/crossover_v2/delay_landscape.py | DelayLandscape |
| jasper/active_speaker/crossover_v2/driver_prescription.py | ClassificationBasis |
| jasper/active_speaker/crossover_v2/driver_prescription.py | DriverPrescription |
| jasper/active_speaker/crossover_v2/durable_state.py | V2ConductorSnapshot |
| jasper/active_speaker/crossover_v2/feature_classification.py | FeatureVerdict |
| jasper/active_speaker/crossover_v2/forward_model.py | PredictedSum |
| jasper/active_speaker/crossover_v2/plan_assembly.py | LevelConsistency |
| jasper/active_speaker/crossover_v2/round_evidence.py | RoundEvaluation |
| jasper/active_speaker/crossover_v2/round_views.py | AgreementFeature |
| jasper/active_speaker/crossover_v2/round_views.py | AudibilityCoMetrics |
| jasper/active_speaker/crossover_v2/round_views.py | AudibilityMetrics |
| jasper/active_speaker/crossover_v2/round_views.py | EntryStateGrade |
| jasper/active_speaker/crossover_v2/round_views.py | FrozenReferenceResult |
| jasper/active_speaker/crossover_v2/round_views.py | PooledWindowResult |
| jasper/active_speaker/crossover_v2/round_views.py | RepeatabilityMetric |
| jasper/active_speaker/crossover_v2/round_views.py | RepeatabilityResult |
| jasper/active_speaker/crossover_v2/topology_prescription.py | TopologyPrescription |
| jasper/active_speaker/crossover_v2/verification.py | FlatnessObjectives |
| jasper/active_speaker/crossover_v2/verification.py | Verdict |
| jasper/active_speaker/delta_probe.py | DeltaProbeMap |
| jasper/active_speaker/delta_probe.py | SpatialCost |
| jasper/active_speaker/driver_acoustics.py | DriverAcousticResult |
| jasper/active_speaker/driver_acoustics.py | DriverSweep |
| jasper/active_speaker/driver_acoustics.py | SummedAcousticResult |
| jasper/active_speaker/driver_protection.py | DriverProtectionProfile |
| jasper/active_speaker/driver_safety.py | DriverSafetyProfileEvaluation |
| jasper/active_speaker/excitation_safety_plan.py | DriverSweepGeneratorPlan |
| jasper/active_speaker/excitation_safety_plan.py | PreparedDriverExcitationPlan |
| jasper/active_speaker/excitation_safety_plan.py | RequestedDriverExcitationPlan |
| jasper/active_speaker/flat_spec.py | BandTilt |
| jasper/active_speaker/flat_spec.py | ConvergenceResidual |
| jasper/active_speaker/flat_spec.py | SpecFlatness |
| jasper/active_speaker/flat_spec_views.py | BandWeight |
| jasper/active_speaker/flat_spec_views.py | DirectivityBand |
| jasper/active_speaker/flat_spec_views.py | DirectivityRow |
| jasper/active_speaker/flat_spec_views.py | DirectivityTable |
| jasper/active_speaker/flat_spec_views.py | LogPooledResidual |
| jasper/active_speaker/flat_spec_views.py | PositionFlatness |
| jasper/active_speaker/flat_spec_views.py | RoleFlatness |
| jasper/active_speaker/flat_spec_views.py | RoleSplitFlatness |
| jasper/active_speaker/frequency_view.py | FrequencySeries |
| jasper/active_speaker/linearization_fit.py | BlindZonePlacement |
| jasper/active_speaker/linearization_fit.py | BoostEvidenceDrop |
| jasper/active_speaker/linearization_fit.py | BoostExclusionDrop |
| jasper/active_speaker/linearization_fit.py | BoostExclusionResidual |
| jasper/active_speaker/linearization_fit.py | FitVocabulary |
| jasper/active_speaker/linearization_fit.py | LinearizationFilter |
| jasper/active_speaker/linearization_fit.py | LinearizationFit |
| jasper/active_speaker/measured_candidate.py | MeasuredCandidateInputContract |
| jasper/active_speaker/measured_candidate.py | MeasuredElectricalCandidate |
| jasper/active_speaker/measured_crossover_candidate.py | MeasuredCrossoverAlignment |
| jasper/active_speaker/measured_crossover_candidate.py | MeasuredCrossoverCandidate |
| jasper/active_speaker/measurement_archive.py | ArchivedMeasurement |
| jasper/active_speaker/path_safety.py | PathSafetyRequirement |
| jasper/active_speaker/playback_route.py | ActiveLaneCapabilityGap |
| jasper/active_speaker/playback_route.py | ActivePlaybackRouteCapability |
| jasper/active_speaker/playback_route.py | UnrecognizedDacProfile |
| jasper/active_speaker/profile.py | ActiveChannelMap |
| jasper/active_speaker/profile.py | ActiveSpeakerPreset |
| jasper/active_speaker/profile.py | BaselineVerification |
| jasper/active_speaker/profile.py | CrossoverRegion |
| jasper/active_speaker/profile.py | DriverSpec |
| jasper/active_speaker/profile.py | LocalSubwoofer |
| jasper/active_speaker/profile.py | OutputChannel |
| jasper/active_speaker/profile.py | SafetyEnvelope |
| jasper/active_speaker/profile.py | SpeakerBaselineProfile |
| jasper/active_speaker/program_admission.py | ChannelFacts |
| jasper/active_speaker/program_admission.py | ProgramAdmission |
| jasper/active_speaker/program_admission.py | SegmentAdmission |
| jasper/active_speaker/runtime_contract.py | GraphSafety |
| jasper/active_speaker/runtime_contract.py | OutputAssignment |
| jasper/active_speaker/runtime_contract.py | OutputContract |
| jasper/active_speaker/runtime_contract.py | OutputdActiveLaneDecision |
| jasper/active_speaker/runtime_contract.py | SafeGraphDecision |
| jasper/active_speaker/runtime_convergence.py | RuntimeConvergenceResult |
| jasper/active_speaker/seat_level_ramp.py | SeatLevelResult |
| jasper/active_speaker/seat_level_reference.py | SeatLevelTarget |
| jasper/active_speaker/seat_level_reference.py | StimulusProvenance |
| jasper/attribution/findings.py | EvidenceRef |
| jasper/attribution/findings.py | Finding |
| jasper/attribution/findings.py | FindingSet |
| jasper/attribution/session_identity.py | SessionIdentity |
| jasper/audio_measurement/admitted_playback.py | GeneratedExcitationWav |
| jasper/audio_measurement/analysis.py | ShoulderSpan |
| jasper/audio_measurement/bundles.py | ArtifactEntry |
| jasper/audio_measurement/calibration.py | MicSensitivity |
| jasper/audio_measurement/delay_graph.py | DelayCandidateConfirmation |
| jasper/audio_measurement/delay_graph.py | DelayLaneBinding |
| jasper/audio_measurement/evidence_identity.py | ArtifactIdentity |
| jasper/audio_measurement/evidence_identity.py | CaptureIdentity |
| jasper/audio_measurement/evidence_identity.py | ExactDspStateIdentity |
| jasper/audio_measurement/evidence_identity.py | NormalizedActiveRawIdentity |
| jasper/audio_measurement/frame_fit.py | FrameComparison |
| jasper/audio_measurement/frame_fit.py | FrameFit |
| jasper/audio_measurement/frame_ledger.py | FrameLedger |
| jasper/audio_measurement/null_walk.py | BoundedNullWalkSchedule |
| jasper/audio_measurement/null_walk.py | DelayCandidate |
| jasper/audio_measurement/null_walk.py | NullWalkSpec |
| jasper/audio_measurement/olive_metrics.py | NBDResult |
| jasper/audio_measurement/olive_metrics.py | SMResult |
| jasper/audio_measurement/program_analysis.py | CaptureIntegrity |
| jasper/audio_measurement/program_analysis.py | RealizedLevelMatch |
| jasper/audio_measurement/program_analysis.py | RoleGainSolve |
| jasper/audio_measurement/quality.py | CaptureQuality |
| jasper/audio_measurement/quality.py | QualityIssue |
| jasper/audio_measurement/spatial_combine.py | PositionResidual |
| jasper/calibration_agent/key_provisioning.py | TuningAvailability |
| jasper/calibration_agent/proposal_sim.py | SimIssue |
| jasper/calibration_agent/proposal_sim.py | SimResult |
| jasper/correction/acceptance.py | AcceptanceResult |
| jasper/correction/acceptance.py | BandVerdict |
| jasper/correction/applied_speaker_evidence.py | AppliedSpeakerEvidence |
| jasper/correction/applied_speaker_evidence.py | AppliedSpeakerEvidenceAbsent |
| jasper/correction/browser_audio.py | BrowserAudioIssue |
| jasper/correction/browser_audio.py | BrowserAudioReport |
| jasper/correction/bundles.py | BundleIssue |
| jasper/correction/confidence.py | ConfidenceFinding |
| jasper/correction/level_match.py | DriftResult |
| jasper/correction/level_match.py | MeasurementLevelLock |
| jasper/correction/replay_artifacts.py | ReplayArtifactSet |
| jasper/correction/runtime_integrity.py | RuntimeIssue |
| jasper/correction/session.py | SessionEvent |
| jasper/correction/strategy.py | CorrectionStrategy |
| jasper/correction/strategy.py | TargetProfile |
| jasper/correction/variance_cap.py | VarianceCapDisclosure |


## 7. Citation and stale-language census

### Table 7a — `#NNN` issue citations per file, top 30 (total 1854 across 156 files)

| file | count |
|---|---:|
| jasper/active_speaker/crossover_v2_flow.py | 205 |
| jasper/active_speaker/crossover_envelope_v2.py | 137 |
| jasper/web/correction_crossover_v2.py | 115 |
| jasper/active_speaker/linearization_fit.py | 111 |
| jasper/active_speaker/delta_probe.py | 82 |
| jasper/active_speaker/crossover_v2/durable_state.py | 72 |
| jasper/active_speaker/crossover_v2/refusal_copy.py | 59 |
| jasper/audio_measurement/program.py | 44 |
| jasper/active_speaker/crossover_v2/spatial.py | 43 |
| jasper/active_speaker/crossover_v2/contracts.py | 40 |
| jasper/active_speaker/crossover_v2/capture_plan.py | 38 |
| jasper/active_speaker/crossover_v2/coordinator.py | 36 |
| jasper/web/correction_setup.py | 34 |
| jasper/active_speaker/crossover_v2/capture_dispatch.py | 29 |
| jasper/active_speaker/runtime_contract.py | 28 |
| jasper/active_speaker/crossover_v2/planning.py | 26 |
| jasper/active_speaker/driver_safety.py | 25 |
| jasper/active_speaker/crossover_v2/round_evidence.py | 23 |
| jasper/audio_measurement/interference_nulls.py | 22 |
| jasper/active_speaker/branch_chain.py | 20 |
| jasper/active_speaker/baseline_profile.py | 19 |
| jasper/active_speaker/camilla_yaml.py | 18 |
| jasper/active_speaker/driver_protection.py | 18 |
| jasper/web/correction_crossover_v2_relay.py | 18 |
| jasper/active_speaker/branch_target.py | 16 |
| jasper/active_speaker/crossover_v2/driver_prescription.py | 16 |
| jasper/active_speaker/crossover_v2/priors.py | 15 |
| jasper/active_speaker/staging.py | 15 |
| jasper/active_speaker/crossover_v2/delta_probe_run.py | 14 |
| jasper/active_speaker/crossover_v2/diagnostics.py | 14 |

### Table 7b — `ADR-NNNN` citations per file, top 30 (total 140 across 65 files)

| file | count |
|---|---:|
| jasper/active_speaker/crossover_v2/driver_prescription.py | 9 |
| jasper/active_speaker/crossover_v2/blend_prescription.py | 7 |
| jasper/active_speaker/crossover_v2/round_views.py | 6 |
| jasper/active_speaker/baseline_profile.py | 5 |
| jasper/active_speaker/camilla_yaml.py | 4 |
| jasper/active_speaker/crossover_v2/session.py | 4 |
| jasper/active_speaker/repeat_admission.py | 4 |
| jasper/cli/audition.py | 4 |
| jasper/web/correction_setup.py | 4 |
| jasper/active_speaker/_common.py | 3 |
| jasper/active_speaker/commissioning_coordinator.py | 3 |
| jasper/active_speaker/commissioning_verification.py | 3 |
| jasper/active_speaker/crossover_envelope_v2.py | 3 |
| jasper/active_speaker/crossover_level_run.py | 3 |
| jasper/active_speaker/crossover_v2/evidence_packet.py | 3 |
| jasper/active_speaker/crossover_v2/feature_classifier.py | 3 |
| jasper/active_speaker/delta_probe.py | 3 |
| jasper/active_speaker/flat_spec.py | 3 |
| jasper/active_speaker/setup_status.py | 3 |
| jasper/audio_measurement/olive_metrics.py | 3 |
| jasper/cli/basic_profile.py | 3 |
| jasper/cli/crossover_prescriber.py | 3 |
| jasper/active_speaker/audition.py | 2 |
| jasper/active_speaker/commissioning_run.py | 2 |
| jasper/active_speaker/crossover_v2/blend_correction.py | 2 |
| jasper/active_speaker/crossover_v2/session_seams.py | 2 |
| jasper/active_speaker/repeat_floor.py | 2 |
| jasper/active_speaker/runtime_contract.py | 2 |
| jasper/active_speaker/tuning_handoff.py | 2 |
| jasper/cli/forward_model.py | 2 |

### Table 7c — stale-language lines per file, top 30 (total 1111 across 167 files)

(wordlist: superseded, owner ruling, ruling, historically, used to, no longer, legacy, deprecated, archaeology, kept for, case-insensitive; one line counted once even if it matches multiple words)

| file | lines matched |
|---|---:|
| jasper/active_speaker/crossover_v2_flow.py | 75 |
| jasper/active_speaker/driver_safety.py | 59 |
| jasper/web/correction_crossover_v2.py | 44 |
| jasper/active_speaker/crossover_envelope_v2.py | 43 |
| jasper/active_speaker/crossover_v2/driver_prescription.py | 41 |
| jasper/active_speaker/crossover_v2/capture_plan.py | 36 |
| jasper/active_speaker/baseline_profile.py | 34 |
| jasper/active_speaker/driver_protection.py | 32 |
| jasper/active_speaker/linearization_fit.py | 32 |
| jasper/active_speaker/crossover_v2/refusal_copy.py | 27 |
| jasper/active_speaker/crossover_v2/evidence_packet.py | 22 |
| jasper/web/correction_setup.py | 20 |
| jasper/active_speaker/crossover_v2/spatial.py | 17 |
| jasper/active_speaker/crossover_v2/round_views.py | 16 |
| jasper/active_speaker/measurement.py | 15 |
| jasper/active_speaker/camilla_yaml.py | 14 |
| jasper/active_speaker/crossover_v2/topology_prescription.py | 14 |
| jasper/active_speaker/crossover_v2/admission.py | 13 |
| jasper/active_speaker/design_draft.py | 13 |
| jasper/attribution/promotion.py | 13 |
| jasper/active_speaker/crossover_v2/blend_prescription.py | 12 |
| jasper/active_speaker/crossover_v2/durable_state.py | 12 |
| jasper/active_speaker/delta_probe.py | 12 |
| jasper/active_speaker/excitation_safety_plan.py | 12 |
| jasper/active_speaker/web_commissioning.py | 11 |
| jasper/audio_measurement/playback.py | 11 |
| jasper/web/correction_crossover_flow.py | 11 |
| jasper/active_speaker/crossover_v2/contracts.py | 10 |
| jasper/active_speaker/runtime_contract.py | 10 |
| jasper/active_speaker/crossover_v2/measure_spec.py | 9 |


## 8. `__all__` census

### Table 8 — `__all__` census (89 files declare `__all__`; 1418 names total; 590 with zero outside importers)

| file | names in __all__ | names with zero outside importers |
|---|---:|---:|
| jasper/active_speaker/__init__.py | 209 | 146 |
| jasper/active_speaker/crossover_v2/verification.py | 69 | 3 |
| jasper/active_speaker/crossover_v2/spatial.py | 61 | 30 |
| jasper/active_speaker/crossover_v2/contracts.py | 56 | 4 |
| jasper/active_speaker/crossover_v2_flow.py | 53 | 17 |
| jasper/active_speaker/delta_probe.py | 42 | 2 |
| jasper/active_speaker/angle_capture.py | 40 | 12 |
| jasper/attribution/__init__.py | 37 | 37 |
| jasper/active_speaker/crossover_v2/round_views.py | 34 | 12 |
| jasper/active_speaker/crossover_v2/blend_prescription.py | 28 | 8 |
| jasper/active_speaker/crossover_v2/admission.py | 25 | 19 |
| jasper/active_speaker/crossover_v2/driver_prescription.py | 24 | 4 |
| jasper/active_speaker/graph_evidence.py | 24 | 24 |
| jasper/active_speaker/crossover_v2/feature_classification.py | 23 | 1 |
| jasper/active_speaker/crossover_v2/intervention.py | 22 | 10 |
| jasper/active_speaker/crossover_v2/capture_dispatch.py | 19 | 13 |
| jasper/active_speaker/crossover_v2/prescription_spool.py | 19 | 13 |
| jasper/active_speaker/angle_capture_spool.py | 18 | 11 |
| jasper/active_speaker/bench/loop.py | 18 | 7 |
| jasper/attribution/closed_sets.py | 18 | 14 |
| jasper/active_speaker/bench/compare.py | 17 | 13 |
| jasper/active_speaker/crossover_v2/blend_correction.py | 17 | 11 |
| jasper/active_speaker/crossover_v2/durable_state.py | 17 | 14 |
| jasper/active_speaker/crossover_v2/evidence_packet.py | 17 | 2 |
| jasper/active_speaker/crossover_v2/feature_classifier.py | 17 | 11 |
| jasper/active_speaker/crossover_v2/__init__.py | 16 | 0 |
| jasper/active_speaker/crossover_v2/harmonic_evidence.py | 16 | 11 |
| jasper/active_speaker/crossover_v2/accountability.py | 15 | 12 |
| jasper/active_speaker/crossover_v2/close_reference.py | 15 | 4 |
| jasper/active_speaker/crossover_v2/delay_landscape.py | 15 | 5 |
| jasper/active_speaker/crossover_v2/forward_model.py | 15 | 4 |
| jasper/active_speaker/crossover_v2/round_inputs.py | 14 | 7 |
| jasper/active_speaker/crossover_v2/topology_prescription.py | 14 | 0 |
| experiments/usb-turntable/vendor/usb_turntable/__init__.py | 13 | 13 |
| jasper/active_speaker/crossover_declaration.py | 13 | 1 |
| jasper/active_speaker/crossover_v2/alignment_prescription.py | 13 | 0 |
| jasper/active_speaker/playback_route.py | 13 | 4 |
| jasper/audio_measurement/wired_capture.py | 13 | 2 |
| jasper/active_speaker/audition.py | 12 | 1 |
| jasper/active_speaker/crossover_v2/priors.py | 12 | 9 |
| jasper/active_speaker/crossover_v2/program_transaction.py | 12 | 1 |
| jasper/active_speaker/crossover_v2/ring_projection.py | 12 | 10 |
| jasper/active_speaker/crossover_v2/round_evidence.py | 12 | 2 |
| jasper/attribution/findings.py | 12 | 3 |
| jasper/active_speaker/crossover_v2/feature_optics.py | 10 | 8 |
| jasper/active_speaker/crossover_v2/measure_spec.py | 10 | 1 |
| jasper/active_speaker/crossover_v2/planning.py | 10 | 4 |
| jasper/audio_measurement/frame_ledger.py | 10 | 0 |
| jasper/cli/measure.py | 10 | 5 |
| jasper/active_speaker/crossover_v2/commanded.py | 9 | 6 |
| jasper/active_speaker/crossover_v2/operator_notes.py | 9 | 4 |
| jasper/active_speaker/bench/derivation.py | 8 | 5 |
| jasper/active_speaker/crossover_v2/plan_assembly.py | 8 | 5 |
| jasper/active_speaker/crossover_v2/playback_transaction.py | 8 | 1 |
| jasper/attribution/storage.py | 8 | 2 |
| jasper/active_speaker/round_bank.py | 7 | 1 |
| jasper/attribution/mechanisms.py | 7 | 1 |
| jasper/attribution/promotion.py | 7 | 1 |
| jasper/attribution/session_identity.py | 7 | 0 |
| jasper/audio_measurement/olive_metrics.py | 7 | 2 |
| jasper/active_speaker/crossover_v2/door.py | 6 | 2 |
| jasper/active_speaker/delay_sweep.py | 6 | 0 |
| jasper/active_speaker/reset.py | 6 | 2 |
| jasper/active_speaker/startup_hold.py | 6 | 0 |
| jasper/attribution/position_evidence.py | 6 | 1 |
| jasper/audio_measurement/timeline_slip.py | 6 | 0 |
| jasper/active_speaker/crossover_v2/proposal.py | 5 | 0 |
| jasper/audio_measurement/comparison_bands.py | 5 | 0 |
| jasper/audio_measurement/frame_fit.py | 5 | 0 |
| jasper/active_speaker/crossover_v2/composition.py | 4 | 0 |
| jasper/active_speaker/crossover_v2/measurement_phase.py | 4 | 0 |
| jasper/active_speaker/crossover_v2/session.py | 4 | 1 |
| jasper/active_speaker/crossover_v2/session_seams.py | 4 | 3 |
| jasper/active_speaker/runtime_convergence.py | 4 | 2 |
| jasper/active_speaker/crossover_v2/attempt_grading.py | 3 | 0 |
| jasper/active_speaker/crossover_v2/candidates.py | 3 | 0 |
| jasper/active_speaker/crossover_v2/fc_sweep.py | 3 | 0 |
| jasper/active_speaker/crossover_v2/record_store.py | 3 | 0 |
| jasper/active_speaker/controllability_ledger.py | 2 | 1 |
| jasper/active_speaker/crossover_v2/record_index.py | 2 | 1 |
| jasper/active_speaker/crossover_v2/session_graph.py | 2 | 0 |
| jasper/active_speaker/crossover_v2/tuning_scope.py | 2 | 0 |
| jasper/active_speaker/crossover_v2/volume_claim.py | 2 | 0 |
| jasper/active_speaker/measurement_emit.py | 2 | 0 |
| jasper/active_speaker/passive_profile.py | 2 | 2 |
| jasper/active_speaker/restore_wait.py | 2 | 1 |
| jasper/active_speaker/crossover_v2/handoff_doors.py | 1 | 1 |
| jasper/audio_measurement/wired_level_meter.py | 1 | 0 |
| jasper/web/correction_crossover_context.py | 1 | 0 |

#### Detail — unused names by file

- `jasper/active_speaker/__init__.py`: ACTIVE_STARTUP_CONFIG_NAME, ACTIVE_BASELINE_KIND, ACTIVE_PRESET_KIND, BASELINE_HEADROOM_DB, BASELINE_LIMITER_CLIP_LIMIT_DB, COMMISSIONING_HEADROOM_DB, BASELINE_PROFILE_KIND, DRIVER_ACOUSTIC_KIND, DRIVER_VERDICTS, SUMMED_ACOUSTIC_KIND, SUMMED_VERDICTS, DriverAcousticResult, DriverAcousticsError, DriverSweep, SummedAcousticResult, analyze_driver_capture, write_driver_sweep_wav, DRIVER_VERDICT_TO_OUTCOME, SUMMED_VERDICT_TO_OUTCOME, driver_passband_hz, primary_crossover_fc_hz, record_driver_acoustic_capture, record_summed_acoustic_capture, build_crossover_alignment_proposal, CrossoverAlignmentProposal, ResolvedMode, MAGNITUDE_ONLY, PHASE_AWARE, propose_crossover_alignment, resolve_measurement_mode, CALIBRATION_LEVEL_KIND, DEFAULT_PRESET_RESOURCE, DEFAULT_PATH_SAFETY_EVIDENCE_PATH, DEFAULT_TEST_LEVEL_DBFS, DRIVER_PROTECTION_KIND, DRIVER_PROTECTION_POLICY_VERSION, ENVIRONMENT_REPORT_KIND, MAX_TEST_LEVEL_DBFS, MEASUREMENT_STATE_KIND, MIN_TEST_LEVEL_DBFS, PATH_SAFETY_EVIDENCE_ENV, REQUIRED_PATHS, SAFE_PLAYBACK_SESSION_KIND, SCHEMA_VERSION, STATE_PATH_ENV, STAGED_CONFIG_PATH_ENV, STAGED_METADATA_PATH_ENV, STARTUP_HEADROOM_DB, STARTUP_LIMITER_CLIP_LIMIT_DB, STARTUP_LOAD_PREFLIGHT_KIND, STARTUP_LOAD_STATE_ENV, STARTUP_LOAD_STATE_KIND, TONE_BACKEND_STATUS_KIND, TONE_PLAYBACK_ARTIFACT_KIND, TONE_PLAYBACK_RESULT_KIND, ACTIVE_PLAYBACK_DEVICE_ENV, AplayTonePlaybackBackend, ActiveChannelMap, BRINGUP_PREFLIGHT_KIND, GRAPH_ALL_MUTED_ACTIVE_STARTUP, GRAPH_DRIVER_DOMAIN_BASELINE, GRAPH_FLAT_FULL_RANGE, GRAPH_GUARDED_COMMISSIONING, CrossoverRegion, DriverSpec, OutputChannel, OutputContract, PathSafetyRequirement, GraphSafety, DriverSafetyProfileError, DriverSafetyProfileEvaluation, NullTonePlaybackBackend, SafetyEnvelope, SafeGraphDecision, WavArtifactTonePlaybackBackend, classify_camilla_config_text, classify_camilla_graph, classify_output_contract, calibration_level_payload, clamp_test_level_dbfs, classify_mic_meter, active_driver_targets, active_summed_targets, confirmed_driver_roles, active_topology_requires_roleful_graph, apply_baseline_profile, baseline_config_path, baseline_profile_state_path, declared_protection_floor_hz, declared_protection_highpass_floor_hz, driver_protection_payload, driver_protection_profile, DRIVER_RESEARCH_REQUEST_KIND, DRIVER_RESEARCH_REQUEST_SCHEMA_VERSION, DRIVER_RESEARCH_RESULT_SCHEMA_VERSION, DRIVER_SAFETY_PROFILE_KIND, DRIVER_SAFETY_PROFILE_SCHEMA_VERSION, load_calibration_level_state, evaluate_driver_safety_profile, validate_driver_research_request, validate_driver_research_result_shape, build_bringup_preflight, baseline_candidate_fingerprint, build_baseline_profile_candidate, build_driver_commission_load_preflight, build_driver_research_prompt, build_driver_research_request, build_driver_safety_profile, build_startup_load_preflight, build_passive_mains_preset, build_summed_topology_tone_plan, COMMISSION_LOAD_PREFLIGHT_KIND, COMMISSION_LOAD_STATE_ENV, COMMISSION_LOAD_STATE_KIND, protection_highpass_floor_satisfied, protective_tweeter_highpass_frequency_hz, strictest_crossover_highpass_hz, load_measurement_state, measurement_state_path, path_safety_evidence_path, load_protected_startup_config, load_startup_load_state, RAMP_ROLE_ORDER, RAMP_STATE_ENV, RAMP_STATE_KIND, effective_confirmed_roles, parse_aplay_playback_devices, probe_active_speaker_environment, normalise_driver_research, normalise_manual_settings, normalise_operator_inputs, required_driver_roles, arm_safe_playback_session, load_safe_playback_state, record_safe_playback_result, record_driver_measurement, record_summed_test_artifact, record_summed_validation, resolve_active_playback_device, rollback_protected_startup_config, safe_graph_for_current_topology, start_tone_playback, stop_safe_playback_session, stop_tone_playback, tone_backend_status, update_calibration_level_state
- `jasper/active_speaker/crossover_v2/verification.py`: CLAIM_NO_PER_BRANCH_CAPTURE, spec_band_rows, verify_absolute_tolerance_db
- `jasper/active_speaker/crossover_v2/spatial.py`: CARVE_OUT_SOURCE_IDENTIFIED_NULL, CARVE_OUT_SOURCE_POSITION_SCREEN, LATERAL_EVIDENCE_BAND_HZ, LATERAL_EVIDENCE_POINTS_PER_OCTAVE, MARK_DISTANCE_M, POSITION_AXES, POSITION_AXIS_HORIZONTAL, POSITION_AXIS_VERTICAL, POSITION_ROLES, POSITION_ROLE_OFFAX, POSITION_ROLE_ONAX, POSITION_ROLE_XOVR, SCREEN_LOCATE_FAILED, SCREEN_PILOT_LEVEL_COLLAPSE, SCREEN_LINEARITY_FAILED, SCREEN_CAPTURE_GLITCH, SCREEN_CLIPPED, SCREEN_KINDS, EntryBaselineScreen, GeometryRetake, BoostExclusion, CloudCombine, CloudGroupResult, CloudVerdict, LateralPoseCurve, lateral_evidence_grid_hz, lateral_pose_curve, MIN_RESOLVED_CLOUD_POSITIONS, take_kind, pose_curve_record
- `jasper/active_speaker/crossover_v2/contracts.py`: PLAN_REFUSAL_REASONS, RoundReceipt, VerificationResult, detached_json
- `jasper/active_speaker/crossover_v2_flow.py`: INTEGRITY_CHECK_SWEEP_HEARD, ATTEMPT_REASON_NO_FLOOR, attempt_record_from_verify, DEFAULT_TIER, normalize_tier, capture_progress_label, stage1_base_entries, stage1_plan_max_attempts, LATERAL_POSE_PROMPTS, CLOUD_VERIFY_POSE_PROMPTS, verify_pose_table, position_geometry, LATERAL_EVIDENCE_BAND_HZ, LATERAL_EVIDENCE_POINTS_PER_OCTAVE, lateral_pose_curve, VERIFY_REPEAT_FLOOR_DB, VERIFY_TERMINAL_OUTCOME_DETERMINISTIC
- `jasper/active_speaker/delta_probe.py`: DELTA_PROBE_BAND_TRUSTED_HF, SpatialCost
- `jasper/active_speaker/angle_capture.py`: MAX_ANGLE_DEG, MAX_ELEVATION_DEG, MOVER_MAX_ANGLE_DEG, MOVER_MAX_ELEVATION_DEG, ResolvedStop, program_for_stop, index_phase_map, WALK_REGIME_UNSUPPORTED, WALK_MOVER_MISMATCH, WALK_OVER_MOVER_ENVELOPE, WALK_OVER_RELAY_CAPACITY, WALK_REFUSAL_REASONS
- `jasper/attribution/__init__.py`: CONFIDENCE_TIERS, EVIDENCE_STORE_BUNDLE, EVIDENCE_STORE_CAPTURE_RING, EVIDENCE_STORE_LAPTOP_ARCHIVE, EVIDENCE_TIERS, FINDING_SCHEMA, FINDING_SET_SCHEMA, FIX_CLASSES, MECHANISM_BOUNDARY_SBIR, MECHANISM_HF_REFLECTION, MECHANISM_LEVEL_FRAME, MECHANISM_REGISTRY, POSITION_EVIDENCE_SCHEMA, PROBES, SESSION_IDENTITY_KEY, SESSION_IDENTITY_SCHEME, EvidenceRef, Finding, FindingError, FindingEvidenceMissing, FindingSet, FindingStorageError, MechanismError, MechanismSpec, SessionIdentity, SessionIdentityError, bundle_evidence_ref, findings_relative_path, mechanism_spec, position_evidence_block, promote_carve_outs, promote_level_frame_disagreement, publish_finding_set, read_finding_set, read_session_identity, stamp_session_identity, verify_finding_evidence
- `jasper/active_speaker/crossover_v2/round_views.py`: AGREEMENT_DISSENT_MAX, NOT_SWEPT_BIN_OFF_ANALYSIS_GRID, AgreementFeature, AudibilityCoMetrics, AudibilityMetrics, EntryStateGrade, ForwardModelDeltaResult, FrozenReferenceResult, PooledWindowResult, RepeatabilityResult, RoundInputs, VerifyPoseResult
- `jasper/active_speaker/crossover_v2/blend_prescription.py`: BLEND_PRESCRIPTION_PROVENANCE_MISSING, BOOST_MIN_DIP_DB, BOOST_ROUTE_UNAVAILABLE, PROHIBITED_PRESCRIPTION_KEYS, PositionalEvidence, PositionalSupport, find_prohibited_keys, prescription_route
- `jasper/active_speaker/crossover_v2/admission.py`: ATTEMPT_INITIATOR_HOUSEHOLD, ATTEMPT_INITIATOR_SPEAKER, DECISION_KINDS, SETTLE_BELOW_POSITION_FLOOR, SETTLE_CONDITION_NOT_RETRIABLE, SETTLE_GROUP_CLOSE_REQUIRED, SETTLE_GROUP_KINDS, SETTLE_KEPT_EARLIER_TAKE, SETTLE_KINDS, SETTLE_PHASE_CANNOT_PROCEED, SETTLE_POSITION_UNRESOLVED, SETTLE_RETRY_REMAINS, SETTLE_SLOT_KINDS, AttemptOverspendError, BeginDecision, SlotAttempts, extras_spent_message, reflection_measured_for, settle_group_position
- `jasper/active_speaker/crossover_v2/driver_prescription.py`: DRIVER_PRESCRIPTION_MAX_BYTES, DRIVER_PRESCRIPTION_TOO_LARGE, ClassificationBasis, DriverPassbands
- `jasper/active_speaker/graph_evidence.py`: bass_management_hp_name, channel_select_mixer_name, driver_baseline_gain_name, driver_baseline_limiter_name, driver_delay_name, driver_limiter_name, driver_linearization_peak_name, driver_linearization_shelf_name, driver_linearization_taper_name, output_commission_mute_name, protective_tweeter_hp_name, sub_baseline_gain_name, sub_baseline_limiter_name, sub_lowpass_name, sub_startup_limiter_name, filter_spec, filter_params, filter_type, driver_commission_audible_evidence, all_commission_mutes_engaged, protective_highpass_hz, running_commission_evidence, running_graph_matches_staged_anchor, software_guard_evidence
- `jasper/active_speaker/crossover_v2/feature_classification.py`: GATE_MOVED
- `jasper/active_speaker/crossover_v2/intervention.py`: DriverEvidence, LEVEL_DEFINITIONS_DIFFER_REASON, LinearizationRequest, MIN_TRIM_SANITY_MARGIN_RATIO, PlannerError, PlannerInputError, SIGMA_TOLERABLE_DB, decide_trim, realized_level_match, request_from_analysis
- `jasper/active_speaker/crossover_v2/capture_dispatch.py`: ANCHOR_SCREEN_KINDS, LOCATE_MIN_CONFIDENCE, SCREEN_ALIGNMENT_UNRESOLVED, SCREEN_ANCHOR_AMBIGUOUS, SCREEN_CHANNEL_MAP_MISMATCH, SCREEN_DELAY_IMPLAUSIBLE, SCREEN_NOISY_ROOM_LINEARITY, SCREEN_SNR_FLOOR, CheckScreens, MeasureScreen, MeasureScreens, VerifyIntegrityScreen, measure_screens
- `jasper/active_speaker/crossover_v2/prescription_spool.py`: BLEND_ONLY, CONSUMED_SUFFIX, DEFAULT_PRESCRIPTION_SPOOL_PATH, ENVELOPE_KIND_FIELD, PRESCRIPTION_CLASS_NOT_ACCEPTED, PRESCRIPTION_NOT_STAGED_FOR_THIS_ROUND, PRESCRIPTION_SPOOL_REFUSAL_REASONS, SPOOL_KIND, SPOOL_MALFORMED, SPOOL_MAX_BYTES, SPOOL_SCHEMA_VERSION, SPOOL_TOO_LARGE, set_prescription_spool_path_for_tests
- `jasper/active_speaker/angle_capture_spool.py`: CONSUMED_SUFFIX, DEFAULT_ANGLE_REQUEST_SPOOL_PATH, SPOOL_KIND, SPOOL_MALFORMED, SPOOL_MAX_BYTES, SPOOL_SCHEMA_VERSION, SPOOL_TOO_LARGE, SESSION_ALREADY_LIVE, live_measurement_session, peek_staged_angle_request, set_angle_request_spool_path_for_tests
- `jasper/active_speaker/bench/loop.py`: DEFAULT_RENDER_BOUNDS, EMIT_LOOP_OUTCOMES, OUTCOME_UNAVAILABLE, RENDER_FADER_DB, RENDER_PAIRS_PER_RUN, STIMULUS_TAIL_S, RenderArm
- `jasper/attribution/closed_sets.py`: CONFIDENCE_CONFIDENT, CONFIDENCE_LIKELY, CONFIDENCE_UNSURE, EVIDENCE_TIER_ADJUDICATED, EVIDENCE_TIER_CORROBORATING, EVIDENCE_TIER_MODEL_DERIVED, EVIDENCE_TIER_REFUTED, PROBE_DESIGN_AXIS, PROBE_FARINA_HARMONIC, PROBE_POSITION_VARIANCE, PROBE_REPEAT_VARIANCE, PROBE_REVERSE_NULL, PROBE_ROTATION, PROBE_TWO_LEVEL
- `jasper/active_speaker/bench/compare.py`: BAND_EDGE_GUARD_OCTAVES, DECODABLE_PRECISION, VALIDITY_FLOOR_DB, BranchComparison, EmitComparisonError, branch_validity_mask, compare_branch, decode_render_channel, deconvolved_ir, magnitude_fft_length, shared_arrival_window, soft_clip_fundamental_gain_db, windowed_magnitude_db
- `jasper/active_speaker/crossover_v2/blend_correction.py`: BLEND_CORRECTED, BLEND_DAMPING, BLEND_MAX_TOTAL_CUT_DB, BLEND_MIN_CUT_DB, BLEND_MIN_REGION_BINS, BLEND_NOTHING_TO_CUT, BLEND_NOT_COMPARABLE, BLEND_NO_TRUSTED_BAND, BLEND_REGION_NOT_IMPROVING, BlendCorrection, BlendRegionReading
- `jasper/active_speaker/crossover_v2/durable_state.py`: FINDING_HOUSEHOLD_REFS_KEY, MAX_PERSISTED_SUM_POINTS, ConductorState, V2ConductorSnapshot, alignment_prescription_prior_from_state, attempt_history_from_state, attempt_record_from_verify, blend_prescription_prior_from_state, blend_prescription_sha256_from_state, commanded_delta_prior_from_state, declared_transfer_prior_from_state, entry_baseline_prior_from_state, pilot_transfer_prior_from_state, topology_prescription_prior_from_state
- `jasper/active_speaker/crossover_v2/evidence_packet.py`: PACKET_KIND, RING_SIDECAR_GLOB
- `jasper/active_speaker/crossover_v2/feature_classifier.py`: CAPTURE_ADMISSIBILITY_REASONS, CAPTURES_UNREADABLE, CLASSIFICATION_REFUSAL_REASONS, CLASSIFICATION_SCHEMA_VERSION, LATERAL_CAPTURE_SHAPE, NO_ADMISSIBLE_CAPTURES, NO_FEATURES_DETECTED, PROGRAM_MISSING, ROUND_SHAPE_INADMISSIBLE, RoundCapture, RoundPoseCurve
- `jasper/active_speaker/crossover_v2/harmonic_evidence.py`: FIDELITY_FIELDS, FIDELITY_TOLERANCE, HARMONICS_ARTIFACT, HARMONICS_ARTIFACT_KIND, HARMONICS_SCHEMA_VERSION, NO_ADMISSIBLE_CAPTURES, NO_CAPTURE_PASSED_THE_GATES, PROBE_FREQUENCIES_HZ, PROGRAM_NOT_REPRODUCIBLE, RING_NOT_SCOPED_TO_ONE_SESSION, rebuild_measure_program
- `jasper/active_speaker/crossover_v2/accountability.py`: EVENT_LEVEL_ESTIMATOR_FINDING, EVENT_LEVEL_MATCH_FINDING, EVENT_PREDICTION_GATE, EVENT_PREDICTION_UNGRADEABLE, LEDGER_BASELINE_UNGRADEABLE, LEDGER_IMPROVED, LEDGER_NO_LINEARIZATION, LEDGER_PREDICTED_IN_SPEC, LEDGER_PREDICTION_UNGRADEABLE, LEDGER_RESIDUAL_UNEVALUABLE, AccountabilityDecision, level_frame_record
- `jasper/active_speaker/crossover_v2/close_reference.py`: CLOSE_REFERENCE_SCHEMA_VERSION, RESIDUAL_N_FFT, ROOM_RESIDUAL_FLOOR_DB, GENERATED_BY
- `jasper/active_speaker/crossover_v2/delay_landscape.py`: LANDSCAPE_KIND, PHASE_OVERLAY_ADDITIVE_DEG, PHASE_OVERLAY_TIGHT_DEG, REFUSAL_UNSUPPORTED, curve_shoulder_span
- `jasper/active_speaker/crossover_v2/forward_model.py`: PREDICTION_KIND, PREDICTION_SCHEMA_VERSION, REFUSAL_UNSUPPORTED, acceptance_block
- `jasper/active_speaker/crossover_v2/round_inputs.py`: APPLIED_PROFILE_FILENAME, DECLARED_GEOMETRY_FILENAME, DESIGN_DRAFT_FILENAME, REPEAT_FLOOR_FILENAME, STATE_DEFAULT_PATH, STATE_FILENAME, STATE_SESSION_UNKNOWN
- `experiments/usb-turntable/vendor/usb_turntable/__init__.py`: CommandRejected, CommunicationTimeout, CompletionTimeout, OffsetAngleResult, OperationResult, PortDiscoveryError, ProbeResult, ProtocolError, StartupSynchronizationError, TurntableController, TurntableError, __version__, discover_devices
- `jasper/active_speaker/crossover_declaration.py`: CrossoverDeclarationChange
- `jasper/active_speaker/playback_route.py`: ACTIVE_PLAYBACK_DEVICE_ENV, ACTIVE_PLAYBACK_ROUTE_KIND, EXPLICIT_SOURCE, ActivePlaybackRouteCapability
- `jasper/audio_measurement/wired_capture.py`: WIRED_CAPTURE_CHAIN, WiredRecording
- `jasper/active_speaker/audition.py`: AUDITION_DEADLINE_S
- `jasper/active_speaker/crossover_v2/priors.py`: role_transfers, configured_crossover_transfers, candidate_required_band_hz, measure_sweep_durations_s, check_priors, measure_priors, lateral_priors, cloud_priors, entry_baseline_priors
- `jasper/active_speaker/crossover_v2/program_transaction.py`: BELOW_READY_INCIDENTS
- `jasper/active_speaker/crossover_v2/ring_projection.py`: NOTHING_TO_PROJECT, SKIP_NO_CAPTURED_AT, SKIP_NO_PHASE, SKIP_NO_WAV_PATH, SKIP_UNREADABLE_RECORD, SKIP_WAV_ESCAPES_BUNDLE, SKIP_WAV_MISSING, ProjectedTake, RingProjection, SkippedTake
- `jasper/active_speaker/crossover_v2/round_evidence.py`: RoundEvaluation, evaluate_round
- `jasper/attribution/findings.py`: EVIDENCE_STORES, EVIDENCE_STORE_LAPTOP_ARCHIVE, FINDING_SET_FIELD_DESCRIPTIONS
- `jasper/active_speaker/crossover_v2/feature_optics.py`: DETREND_FRACTION, FEATURE_HALF_OCT, MAGNITUDE_SMOOTH_FRACTION, NEIGHBOURHOOD_OCT, PHASE_GATE_LEAD_MS, biquad_peaking, feature_q, read_feature
- `jasper/active_speaker/crossover_v2/measure_spec.py`: CapabilityStub
- `jasper/active_speaker/crossover_v2/planning.py`: EVENT_FIT_FAILED, EVENT_FIT_FAILED_JOURNAL_DROPPED, applied_profile_delay_us, ineligible_reason
- `jasper/cli/measure.py`: BoxNotMeasurable, MeasureInterrupted, MeasureRestoreFailed, main, read_box_declaration
- `jasper/active_speaker/crossover_v2/commanded.py`: CornerDisagreement, GraphSummation, PreviousGraph, commanded_delta, corner_disagreement, previous_graph_prediction
- `jasper/active_speaker/crossover_v2/operator_notes.py`: EXCLUDED_PROSE, GENERATED_BY, OPERATOR_NOTES_RULE, OPERATOR_NOTES_TREAT_AS
- `jasper/active_speaker/bench/derivation.py`: PROGRAM_HEADROOM_FILTER, BranchStep, DeviceGeometry, DerivedRenderConfig, assert_no_async_resampler
- `jasper/active_speaker/crossover_v2/plan_assembly.py`: FittedBranches, LevelConsistency, SummationFrame, TrimDecision, assemble_plan
- `jasper/active_speaker/crossover_v2/playback_transaction.py`: PlaybackTransaction
- `jasper/attribution/storage.py`: findings_artifact_path, verify_finding_evidence
- `jasper/active_speaker/round_bank.py`: BankedRound
- `jasper/attribution/mechanisms.py`: MechanismSpec
- `jasper/attribution/promotion.py`: SOURCE_IDENTIFIED_NULL
- `jasper/audio_measurement/olive_metrics.py`: NBDResult, SMResult
- `jasper/active_speaker/crossover_v2/door.py`: REFUSE_VOLUME_NOT_OPEN, OpenMeasurementDoor
- `jasper/active_speaker/reset.py`: ACTIVE_SPEAKER_MEASUREMENT_JOURNEY_RESET_KIND, ACTIVE_SPEAKER_SETUP_RESET_KIND
- `jasper/attribution/position_evidence.py`: DEFAULT_CURVE_FLOOR_HZ
- `jasper/active_speaker/crossover_v2/session.py`: StimulusOutcome
- `jasper/active_speaker/crossover_v2/session_seams.py`: RecordStore, SessionGraph, VolumeClaim
- `jasper/active_speaker/runtime_convergence.py`: RuntimeConvergenceResult, TopologyRuntimeMutationResult
- `jasper/active_speaker/controllability_ledger.py`: ROUND_RECEIPT_GLOB
- `jasper/active_speaker/crossover_v2/record_index.py`: Measurement
- `jasper/active_speaker/passive_profile.py`: measured_candidate_fingerprint, passive_mains_compiles_roleful
- `jasper/active_speaker/restore_wait.py`: await_restore_task_resilient
- `jasper/active_speaker/crossover_v2/handoff_doors.py`: request_time_prescriptions


## 9. Import-graph boundary check

### Table 9a — `jasper.web` imported from truth-layer dirs (active_speaker/audio_measurement/correction/attribution) — 0 found

| file | line | import |
|---|---:|---|

### Table 9b — `jasper.active_speaker` / `jasper.correction` imported from audio_measurement/ — 0 found

| file | line | import |
|---|---:|---|

### Table 9c — `crossover_v2` imported from correction/ — 0 found

| file | line | import |
|---|---:|---|


## 10. Test census

### Table 10 — test census for tuning-scope tests

- Files: 342
- Total lines: 347151
- Test functions (def test_*): 10667
- Parametrized test functions: 1090 (10.2%)
- Tests asserting equality against a string literal containing a space (likely prose pin): 172
- Tests that read source files (inspect.getsource / ast.parse / .py read_text): 85

#### Per-file breakdown (top 40 by line count)

| file | lines | test funcs | parametrized | prose-pin asserts | reads source |
|---|---:|---:|---:|---:|---:|
| tests/test_crossover_v2_conductor.py | 12175 | 326 | 15 | 4 | 1 |
| tests/test_correction_crossover_v2_endpoints.py | 9309 | 189 | 15 | 4 | 6 |
| tests/test_audio_measurement_program_analysis.py | 8081 | 186 | 19 | 0 | 0 |
| tests/test_sound_setup.py | 7726 | 154 | 23 | 5 | 1 |
| tests/test_active_speaker_baseline_profile.py | 6953 | 121 | 5 | 1 | 0 |
| tests/test_correction_setup.py | 6628 | 186 | 17 | 4 | 3 |
| tests/test_crossover_envelope_v2.py | 6216 | 249 | 28 | 16 | 1 |
| tests/test_active_speaker_runtime_contract.py | 5483 | 171 | 29 | 1 | 1 |
| tests/test_active_speaker_seat_level.py | 5174 | 120 | 12 | 0 | 1 |
| tests/test_spatial_combine.py | 4832 | 91 | 9 | 0 | 0 |
| tests/test_crossover_v2_driver_prescription.py | 4654 | 161 | 26 | 4 | 1 |
| tests/test_audio_health.py | 4289 | 122 | 6 | 15 | 0 |
| tests/test_ring_active_endpoint.py | 4204 | 82 | 5 | 10 | 2 |
| tests/test_active_speaker_driver_safety.py | 4004 | 76 | 6 | 5 | 0 |
| tests/test_active_speaker_linearization_fit.py | 3959 | 132 | 8 | 0 | 1 |
| tests/test_multiroom_reconcile.py | 3888 | 156 | 10 | 0 | 0 |
| tests/test_crossover_v2_round_wiring.py | 3287 | 72 | 8 | 0 | 0 |
| tests/test_active_speaker_web_commissioning.py | 3236 | 56 | 14 | 0 | 0 |
| tests/test_audio_hardware_reconcile.py | 3208 | 66 | 22 | 1 | 0 |
| tests/test_crossover_v2_blend_prescription.py | 3152 | 128 | 23 | 0 | 0 |
| tests/test_doctor_correction.py | 3000 | 119 | 7 | 0 | 0 |
| tests/test_active_speaker_commissioning_capture.py | 2699 | 87 | 4 | 0 | 1 |
| tests/test_crossover_v2_verification.py | 2586 | 109 | 25 | 0 | 1 |
| tests/test_correction_status_and_bundles.py | 2583 | 55 | 6 | 3 | 0 |
| tests/test_crossover_v2_prescription_spool.py | 2568 | 75 | 4 | 1 | 0 |
| tests/test_active_speaker_delta_probe.py | 2542 | 110 | 8 | 0 | 0 |
| tests/test_voice_daemon_measurement_inflight.py | 2411 | 40 | 14 | 0 | 0 |
| tests/test_crossover_v2_stage_bridge.py | 2388 | 41 | 5 | 0 | 1 |
| tests/test_renderer_ring_lanes.py | 2295 | 83 | 12 | 0 | 2 |
| tests/test_correction_crossover_v2_wired.py | 2230 | 73 | 6 | 0 | 0 |
| tests/test_active_speaker_crossover_v2_round_views.py | 2119 | 77 | 4 | 0 | 0 |
| tests/test_active_speaker_setup_status.py | 2101 | 42 | 6 | 0 | 0 |
| tests/test_correction_session.py | 2097 | 50 | 4 | 1 | 0 |
| tests/test_audio_runtime_plan.py | 2087 | 77 | 5 | 2 | 0 |
| tests/test_sound_setup_commission.py | 2063 | 38 | 3 | 1 | 0 |
| tests/test_crossover_v2_remote_tier.py | 2037 | 76 | 3 | 3 | 2 |
| tests/test_interference_nulls.py | 2036 | 50 | 2 | 0 | 1 |
| tests/test_active_speaker_linearization_envelope.py | 1952 | 68 | 5 | 0 | 0 |
| tests/test_control_server_system.py | 1947 | 61 | 1 | 4 | 1 |
| tests/test_output_topology.py | 1939 | 66 | 7 | 4 | 0 |

---

## Cross-check against BRIEF.md's prior-analysis numbers

The prior analysis (a few days old) and this fresh mechanical census agree
closely, which cross-validates both:

| claim | prior analysis | this census |
|---|---:|---:|
| `_text` re-rolls | 11 | 11 (Table 4) |
| `_mapping` re-rolls | 8 | 8 (Table 4) |
| sha256 helpers | 15 (6 signatures) | 13 (`_sha256`×10 + `_sha256_fd`/`_sha256_file`/`_sha256_text`) |
| `_refuse` def files | 22 files | 22 defs = 13×`_refuse`+9×`_refused` across 22 files (Table 5a0) |
| `_gate` def files | 7 | 8 |
| `_issue` def files | 6 | 8 |
| `_blocked` def files | 5 | 5 (exact) |
| `#NNNN` issue citations | 1,763 | 1,854 (Table 7a) |
| `ADR-NNNN` citations | 132 | 140 (Table 7b) |
| to_dict/from_dict-ish methods | ~297 | 277 (204 to_dict + 49 from_mapping + 23 from_dict + 1 to_json) |
| to_dict vs from_dict | 190 vs 16 | 204 vs 23 |
| test functions | 10,770 | 10,667 (Table 10) |
| parametrized % | 9.5% | 10.2% (Table 10) |

Small deltas are expected (a few days of merged PRs, and this census's
patterns are defined verbatim from the task brief rather than the prior
analysis's exact wordlist) — the agreement is close enough that both counts
should be trusted as directionally accurate.

## Additional boundary observation (outside the three required Table 9 checks)

Table 9's three specified checks (jasper.web from the four truth-layer dirs;
jasper.active_speaker/jasper.correction from audio_measurement/; crossover_v2
from correction/) all returned **zero** violations — the layering rule holds
for those exact edges at HEAD.

Grepping more broadly turned up two lazy (function-body) imports **from
jasper/attribution/ into jasper.active_speaker** — not one of the three
specified checks, so not counted in Table 9, but worth flagging since
REFACTOR-TUNING's target architecture has the truth layer with "no upward
import" and attribution reads out of active_speaker's evidence store:

- `jasper/attribution/promotion.py:501` — `from jasper.active_speaker.crossover_v2.intervention import (...)`
- `jasper/attribution/storage.py:106` — `from jasper.active_speaker.commissioning_evidence_store import EVIDENCE_ROOT`

## Reproduction

```
cd /home/user/JTS
S=scratchpad/recon/census   # or wherever this directory was copied
python3 $S/scope_files.py > $S/scope_files.txt
python3 $S/scope_files.py tests > $S/scope_tests.txt
python3 $S/metrics.py $S/scope_files.txt > $S/metrics.json
python3 $S/report_tables.py > $S/tables_1_2.md      # Tables 1, 2
python3 $S/big_defs.py $S/scope_files.txt           # Table 3
python3 $S/dup_helpers.py $S/scope_files.txt        # Table 4
python3 $S/refusal_census.py $S/scope_files.txt     # Table 5
python3 $S/serialization_census.py $S/scope_files.txt  # Table 6
python3 $S/citation_census.py $S/scope_files.txt    # Table 7
python3 $S/all_census.py $S/scope_files.txt         # Table 8 (needs ripgrep)
python3 $S/boundary_check.py $S/scope_files.txt     # Table 9
python3 $S/test_census.py $S/scope_tests.txt        # Table 10
```

No file was edited; this was read-only recon.
