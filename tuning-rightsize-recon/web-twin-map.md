# Web twin map — row 2.4 (recon by a read-only Opus agent at main edcca6282)

Targets: `jasper/web/correction_crossover_v2.py` (7,601) + `_status.py` (858) + `_wired.py` (1,142) + `_republish.py` (374) = 9,975 lines.
Route dispatch lives in `jasper/web/correction_setup.py` (paths declared ~340-361; dispatch ~3077-3170, ~4074-4323; lazy `from . import correction_crossover_v2 as v2host`).
Three of the plan's six destinations DO NOT EXIST: no `crossover_v2/session_assembly.py`, `apply_transaction.py`, `capture_wired.py`. `playback_transaction.py` (137) is a Protocol + stage vocabulary only; the implementation is `program_transaction.py`.
Pure twin duplication ≈ 2,080 lines (~21%); ≈ 4,900 lines are engine work with no engine home (extraction = creation, not deletion).
Three shared fixture modules patch the web host by name and must be re-pointed on every extraction: `tests/crossover_v2_fixtures.py` (2,154), `crossover_v2_round_harness.py` (371), `crossover_v2_banked_round.py` (351).
Test surface: 56 modules, 92,464 lines; biggest: test_crossover_v2_conductor 12,097; test_correction_crossover_v2_endpoints 9,308; test_crossover_envelope_v2 6,214; test_correction_setup 4,146; test_correction_crossover_v2_wired 2,057.
No open PR touches the four files (checked 2026-09-03 23:30Z).

## Main file by category (6,958 body lines)
| Category | Lines | pure TWIN |
|---|---|---|
| session assembly | 2,416 | 30 |
| save/bank | 1,549 | 309 |
| playback | 901 | 464 |
| apply/rollback | 695 | 167 |
| level lease | 536 | 23 |
| grading | 422 | 348 |
| status projection | 246 | 76 |
| page render/copy | 111 | 95 |
| other | 82 | 20 |
What legitimately stays web-side: `refusal_next_action`, the `set_*_for_tests` seams, the lazy-import shim role — 100-150 lines. `_status.py` ends as an ~80-line adapter.

## Main-file symbol table (line, size, symbol, category, engine owner, verdict)
- 156-175 (20) CrossoverV2Refused — other — contracts.CrossoverV2FlowError / refusal_copy — TWIN
- 178-193 (16) refusal_next_action — copy — refusal_copy.REASON_REGISTRY — THIN (stays web)
- 196-206 (11) CrossoverV2LocalSeamError — other — NOHOME
- 209-282 (74) classify_program_failure — grading — refusal_copy/contracts — NOHOME
- 285-328 (44) refused_from_flow_error — copy — refusal_copy — TWIN
- 331-361 (31) profile_refusal_code — copy — refusal_copy — TWIN
- 369-377 (7) _state_path, set_state_path_for_tests — save/bank — durable_state.DEFAULT_V2_STATE_PATH — THIN
- 380-490 (105) load_v2_state, save_v2_state, _update_current_review, clear_v2_state — save/bank — durable_state owns shape not file — NOHOME
- 493-515 (21) _attempt_loop_store_snapshot, _record_live_model_error — model_error_store — THIN
- 518-651 (134) reset_v2_journey_state — durable_state/journey — NOHOME
- 654-745 (92) observe_apply_success — durable_state — NOHOME
- 755-816 (60) observe_review_decline, review_declined — durable_state — NOHOME
- 819-875 (53) _applied_gate, _applied_offset_gate, _apply_failure_gate — durable_state — NOHOME
- 883-902 (18) session_volume_plan, set_volume_plan_for_tests — level lease — session_volume_plan.py:634 — THIN
- 935-983 (43) 4× session measurement pause — correction/coordinator — THIN
- 986-1042 (55) _under_measurement_isolation, _play_under_session_pause — playback — NOHOME
- 1055-1077 (23) _session_volume_read — level lease — session_volume_plan.py:605 FaderVolumeDoor — TWIN (NN: set_volume_db path at :1061)
- 1080-1125 (44) _refuse_without_a_volume_owner, _session_volume_claim — volume_claim.py:37 — THIN
- 1128-1178 (49) _volume_door, _session_measurement_claim_held — volume_claim.py:96 OwnerVolumeDoor — THIN
- 1181-1221 (41) _release_pause_best_effort — NOHOME
- 1224-1289 (64) enforce_session_volume_ceiling_if_stale, v2_volume_recovery_active — session_volume_plan.enforce_ceiling — THIN
- 1300-1394 (93) recover_session_volume, reconcile_session_volume_for_new_session — session_volume_plan — NOHOME
- 1452-1483 (32) _spatial_grade — grading — spatial.py / verification.py:975 — TWIN
- 1486-1801 (316) _post_apply_grade — grading — verification.py:664 verification_result, :975 evaluate_round_quality, attempt_grading — TWIN
- 1842-1935 (94) _take_staged_prescription — assembly — prescription_spool.py:376 — THIN wrapper
- 1938-2199 (262) _take_staged_angle_walk — assembly — angle_capture_spool / arm_walk — NOHOME
- 2202-2244 (43) _resolve_measurement_level_trims — assembly — commanded.py / session.py — NOHOME
- 2249-2257 (9) _fc_hz_label — copy — crossover_envelope_v2.py:1315 _frequency_label — TWIN
- 2261-2384 (122) persist_conductor_state, _persist_terminal_failure — durable_state.py:1029 build_conductor_state — THIN + web write
- 2392-2478 (81) _wav_bytes_to_samples, resolve_setup_calibration, default_setup_calibration_for_v2, _setup_calibration_observation — correction/household_mic, audio_measurement/wired_capture — THIN/TWIN
- 2481-2571 (87) CaptureEvidenceCarry, _bankable, _add_capture_block — evidence_packet / durable_state.py:189 decimators — TWIN(_bankable)/NOHOME
- 2574-2631 (58) _capture_evidence_blocks — evidence_packet — TWIN
- 2634-2803 (170) bind_production_analyze — composition.py:80 bind_engine_seams / program_analysis — NOHOME
- 2806-2828 (23) open_v2_evidence_store — commissioning_evidence_store — THIN
- 2831-2892 (62) bind_evidence_publishers — record_store.py:176 BankedRecordStore — NOHOME
- 2895-2930 (36) bind_round_receipt — round_evidence.py:676 — THIN
- 2933-3149 (217) bind_position_retention — record_store (JSON half moved), bundles.py — TWIN (WAV half)
- 3152-3173 (22) v2_session_identity — attribution/session_identity — THIN
- 3176-3470 (291) _publish_findings, _bank_household_findings, bind_findings_publisher — attribution/storage, evidence_packet — NOHOME
- 3473-3520 (48) bind_cloud_publisher — spatial / record_store — NOHOME
- 3524-3564 (38) _HeldSession, ProductionPlay — session.py:170 TuningSession; playback_transaction.py:107 — TWIN(ProductionPlay)
- 3567-4003 (437) bind_production_play — composition.py:142 bind_program_playback_seams; program_transaction.py:144; program_playback.play_program — TWIN (NN: carries session_volume_db/safety_profile/declared_sensitivities/protection_sections_by_role into program_admission)
- 4017-4022 (6) V2VolumeHooks — session_volume_plan — other
- 4025-4097 (71) drive_group_close, _start_speculative_group_close — spatial / admission — NOHOME
- 4105-4172 (68) ensure_crossover_preview_ready — crossover_preview — THIN
- 4175-4247 (71) _resolve_driver_class_by_role, _resolve_radiating_diameter_by_role — driver_prescription / design_draft — NOHOME
- 4250-4480 (231) resolve_conductor_context — capture_plan (consumer), commission_wiring.resolve_capture_preset, crossover_v2_flow.py:4551 — NOHOME
- 4490-4565 (76) attach_stage2_preflight — crossover_envelope_v2.py:835 _stage2_preflight — TWIN
- 4632-4950 (319) PositionGate — position_cycle.py:477, capture_plan — NOHOME
- 4954-4976 (23) V2PreparedSession — session_seams — other
- 4979-5133 (155) _volume_hooks — session_volume_plan / volume_claim — NOHOME (NN)
- 5152-5205 (54) _active_graph_fingerprint — baseline_profile — NOHOME
- 5208-5383 (170) _previous_candidate_known/_paired, _applied_graph_boosts, _applied_profile_now — candidate_bank / baseline_profile — THIN
- 5386-5436 (51) bind_v2_engine_seams — composition.py:80 / session_seams — THIN
- 5439-5565 (127) bind_v2_stage_seams — crossover_v2_flow.V2FlowSeams / composition — NOHOME
- 5568-5631 (58) _resolve_prepare_wired_mic, _hand_released_plan_shape, _mint_wired_session, _wired_stimulus_capture — _wired / capture_plan — THIN
- 5634-5837 (204) _bind_engine_measure_leg — session.py:170 TuningSession.measure / program_transaction.py:144 — NOHOME
- 5840-5911 (63) _build_wired_run, _verify_plan_shape — _wired / capture_plan — THIN
- 5914-6832 (919) prepare_v2_session — capture_plan, plan_assembly.py:384, journey.open_stage, coordinator — NOHOME (the session_assembly.py target)
- 6840-6890 (51) _assert_stage_2_can_open — door.py:323 / admission — NOHOME
- 6893-7293 (401) handle_v2_apply — baseline_profile.apply_baseline_profile, crossover_declaration — NOHOME (the apply_transaction.py target)
- 7296-7462 (167) bind_delta_probe_rollback — delta_probe_run.py:421 / verification.py:1059 — TWIN
- 7465-7487 (23) _consume_auto_revert_pairing — durable_state — NOHOME
- 7490-7500 (11) _crossover_label — crossover_envelope_v2.py:1315 — TWIN
- 7503-7566 (60) _blocking_apply_issue, _dsp_apply_is_known_inactive, _persist_apply_blocked — refusal_copy / dsp_apply.DSP_PROOF_INACTIVE_RESULTS — THIN/NOHOME
- 7569-7601 (33) _reopen_candidate_artifact — measured_crossover_candidate / record_store — THIN

## _status.py (858) — one concern, status projection → crossover_envelope_v2.py; ~509 lines pure twin
- 35-136 (102) _phase_from_state — journey.py phase vocabulary — TWIN
- 139-163 (25) _provenance_note — capture_provenance — TWIN
- 166-354 (189) _compact_cloud_status — spatial / durable_state.py:189 — TWIN
- 373-427 (55) _decimate_curve_for_chart — durable_state._decimate_sum:189 — TWIN
- 430-478 (49) _chart_cloud_status — frequency_view — TWIN
- 481-569 (89) _prediction_status — forward_model / round_views — TWIN
- 572-623 (50) _previous_candidate_fingerprint, _offerable_previous_candidate — _republish.republish_preflight — THIN
- 626-761 (136) crossover_v2_status_block — crossover_envelope_v2.py:2440 — NOHOME
- 764-858 (93) _controllability_status, _household_findings_status — controllability_ledger / attribution/findings — THIN
Pins: test_crossover_envelope_v2.py, test_crossover_v2_cloud_visualization.py.

## _wired.py (1,142) → crossover_v2/capture_wired.py (new)
- 144-169 (26) _fallback_program_s, _RetakeRequested, WiredMicMissing — refusal_copy — TWIN(WiredMicMissing)
- 172-244 (62) resolve_v2_wired_mic, WiredCaptureSession, WiredOpened, open_wired_capture — wired_capture.resolve_wired_mic / capture_source — THIN
- 248-299 (48) WiredCaptureAnswer, _wired_setup_reference, _json_safe_dbfs — capture_source.py:134 CaptureAnswer / household_mic — THIN/TWIN
- 302-367 (64) mint_wired_answer, make_wired_recorder — capture_source / wired_capture — THIN
- 371-498 (128) WiredStimulusCapture — program_transaction.py:124 StimulusCapture — THIN (Protocol impl)
- 501-1119 (619) build_v2_wired_run_and_consume — capture_plan.run_capture_plan, capture_dispatch, coordinator — NOHOME
- 1122-1142 (21) _abandon_best_effort — session_volume_plan — NOHOME

## _republish.py (374)
- 38-59 (22) _refused — refusal_copy — THIN
- 62-122 (61) _admit_banked_candidate — candidate_bank / admission — NOHOME
- 125-137 (13) republish_preflight — THIN
- 140-374 (235) handle_v2_republish — record_store / durable_state — NOHOME

## Import surface
Outbound from main file: 24 crossover_v2 modules (journey ×10 — the only EAGER engine import, ~0.29 s numpy; refusal_copy ×9; capture_plan ×7; contracts ×5), 26 other active_speaker modules (session_volume_plan ×6, crossover_v2_flow ×6, baseline_profile ×6, program_playback ×4), correction (coordinator, household_mic), attribution, audio_measurement, output_topology, dsp_apply, camilla, volume_owner. No control/ imports. Almost all lazy.
Inbound: correction_setup.py (dispatch), correction_crossover_backend.py, correction_crossover_flow.py; grep-visible back-refs (docstrings, lazy test-seam patches) from crossover_envelope_v2, crossover_v2_flow, measurement_emit, wizard_client, crossover_v2/{capture_plan,capture_source,coordinator,durable_state,evidence_packet,journey,record_store,refusal_copy,round_evidence}.

## Ordering (each PR shrinks the next; spine = durable state)
1. Status projection → crossover_envelope_v2.py (~509 twin lines; retires _status.py to an adapter). No blockers. **Wave 3.**
2. Copy/refusal twins → refusal_copy.py (~130). Independent. (Waits for prose lane P2 on refusal_copy.py.)
3. Grading → verification.py (_post_apply_grade, _spatial_grade, classify_program_failure, ~422). No NN adjacency. (Waits for P2 on verification.py.)
4. Durable state → durable_state.py (load/save/_update_current_review/reset_v2_journey_state/observe_*/persist_conductor_state, ~700). Unblocks 5-7 and 9. (Waits for P1 on durable_state.py.)
5. Save/bank publishers → record_store / evidence_packet (~800). Blocked on 4.
6. Playback → program_transaction.py behind playback_transaction.py contract (bind_production_play 437, ProductionPlay, _bind_engine_measure_leg, _play_under_session_pause, bind_production_analyze, ~900). Blocked on 5. **NN tier** (excitation ceiling path) → /adversarial-review + pins test_crossover_v2_measurement_volume_drift (997), test_crossover_v2_cleanup_drain (228), test_active_speaker_measurement_door (439).
7. _wired.py → crossover_v2/capture_wired.py (new; the 619-line walk). Blocked on 6.
8. Session assembly → new crossover_v2/session_assembly.py (prepare_v2_session 919, resolve_conductor_context 231, PositionGate 319, bind_v2_stage_seams, _take_staged_angle_walk; ~2,400). Blocked on 4-7. resolve_conductor_context and PositionGate can go one PR earlier each.
9. Apply/rollback → new crossover_v2/apply_transaction.py (handle_v2_apply 401, _assert_stage_2_can_open, bind_delta_probe_rollback, _republish.handle_v2_republish; ~880). Blocked on 4 only; parallel with 6-8 accepting one conflict window in durable_state.py.
10. Level lease LAST (~536 → session_volume_plan.py + volume_claim.py; CrossoverLevelLease at web/correction_crossover_backend.py:136, only caller correction_setup.py:4302). **NN tier**: the set_volume_db path and the volume_limit=0.0 enforcement through a session.
Commissioning SPL stop is NOT in these files (seat_level_ramp.py); unaffected.
