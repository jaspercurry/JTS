# Tuning toolbox — operator runbook

## Entry contract

Register the wired microphone with `jasper-mic-calibration`; set its capture control to 100%, and confirm its serial and calibration. Run `jasper-seat-level` once at the mark with the current microphone. Each run holds that session gain; `--level-offsets-db` selects non-positive offsets. The 85 dB SPL commissioning stop watches every take. Code owns capture, limits, graph composition, and evidence. The human or arm owns microphone movement. The LLM chooses the experiment, candidate, and interpretation. Never claim an unmeasured graph or moved microphone.

## The loop

1. Run `jasper-round run --program <speaker|room|bass>`. No `--candidates` makes a measurement run; supplied fingerprints make a trial. Use `--dry-run` first when you need the resolved schedule and refusals without sound.
2. Join at each pose. With `--mover human`, open the returned page, follow its pose prompt, and use its in-place, Retake, or Done action. With `--mover arm`, run `jasper-angle-capture serve`. With `--mover confirmed`, call `jasper-round placed --run <id>` only after the person confirms placement.
3. Run `jasper-round wait --run <id>`. The executor assesses each take, performs only its bounded recovery, finishes the manifest, and banks the run automatically. Use `status` to inspect progress without granting placement.
4. Select evidence by set. `speaker-fit`, `room`, and `repeat` use `jasper-round-views <verb> <round-dir> --set <set-id>`. Use `jasper-round-views sweep <round-dir> --scope round --set <set-id>`. Use `jasper-round-views bass-fit-table <round-dir> --run <run-id> --candidate <candidate.json> --target <target.json> --tolerance-db <db>`. `inventory` lists exact available commands.
5. Author one prescription document. Run `jasper-crossover-prescriber judge <doc> --round <round-dir> --set <set-id>`, then `compose <doc> --base <fingerprint|saved> --round <round-dir> --set <set-id>`.
6. Trial the composed fingerprint with the same loop. Then run `jasper-round apply <fingerprint>`. Apply requires a banked complete trial of that graph, intact trial evidence, matching identity, and a proved layer stack. Its verification dimensions are advice, not another gate.

## Speaker

`speaker/mark` takes two measurements at the design mark. Driver caps still bind the fader. Use `speaker-fit`, `repeat`, and `sweep`; measure the composed full graph before apply.

## Room

`room/cloud` uses the default 11-pose `seat/cloud`, summed and ungated through the accepted Speaker layer with Room and bass off. The commissioning stop still applies. The room layer stops at the applied speaker's trusted floor, clamped to room bounds. Use `room` for the document and trial at the same poses.

## Bass

`bass/cloud` uses the same 11 poses, through accepted Speaker and Room with bass off. Each window holds the session gain plus its offset. Trial baseline and bass candidate in one run. Use `bass-fit-table`; never apply between the paired captures.

## Evidence and recovery

Keep completed valid takes. Do not pool changed poses, levels, graphs, or calibration. Start with `jasper-round status`, the manifest's take verdict, and the named fault below. Fix its action, then continue with the same loop. After an apply timeout, inspect saved state before another write. A losing candidate stays banked.

<!-- BEGIN GENERATED FAULT TABLE (scripts/generate-tuning-tool-menu.py -- do not hand-edit) -->
| Code | What happened | What to do next | Screen |
|---|---|---|---|
| `agc_behavioral_fail` | The two test tones didn't come back at the levels JTS played them. Re-allow the microphone, then try again. | Re-allow the microphone, then try again. | `fix_and_retry` |
| `ambiguous` | Select a candidate with one banked identity. | Select a candidate with one banked identity | `hard_stop` |
| `anchor_ambiguous` | JTS couldn't line that recording up with the test tones it played. Try that measurement again. | Try that measurement again. | `fix_and_retry` |
| `anchor_too_quiet` | JTS heard the speaker, but the test tones were too quiet to line up. Check the volume and the microphone, then try again. | Check the volume and the microphone, then try again. | `fix_and_retry` |
| `apply_failed` | JTS could not apply the measured crossover automatically. Try again. | Try again. | `fix_and_retry` |
| `baseline_graph_safety_proof_failed` | Review the protected speaker graph. | Review the protected speaker graph. | `hard_stop` |
| `bass_fit_candidate_unreadable` | The measured bass candidate descriptor is unavailable. | Supply the candidate artifact named by the run | `hard_stop` |
| `bass_fit_capture_context_changed` | The paired bass captures used different conditions. | Measure both graphs at the same pose and settings | `hard_stop` |
| `bass_fit_common_coverage_unavailable` | The bass takes have no shared usable frequency range. | Measure both graphs over the target band | `hard_stop` |
| `bass_fit_inputs_missing` | No bass pairs are available for this fit. | Select a run with baseline and candidate takes | `hard_stop` |
| `bass_fit_pairs_unavailable` | The run has no unique baseline pair for each candidate take. | Measure baseline and candidates at matching levels and poses | `hard_stop` |
| `bass_fit_pose_missing` | The bass capture has no recorded pose. | Measure with a recorded microphone pose | `hard_stop` |
| `bass_fit_reference_band_unavailable` | The bass sweep does not cover the reference band. | Measure a sweep that covers the reference band | `hard_stop` |
| `bass_fit_requires_room_baseline_and_exact_candidate` | The bass pair does not contain the required graphs. | Select the room baseline and the measured bass candidate | `hard_stop` |
| `bass_fit_run_mismatch` | The selected run does not match this manifest. | Select the run recorded in this manifest | `hard_stop` |
| `bass_table_capture_context_changed` | The bass levels were captured under different conditions. | Measure all levels with the same stimulus and setup | `hard_stop` |
| `bass_table_capture_integrity_failed` | A bass capture failed its integrity check. | Repeat the failed capture | `hard_stop` |
| `bass_table_tolerance_invalid` | The bass target tolerance is invalid. | Supply a positive tolerance in dB | `hard_stop` |
| `bass_table_window_gain_missing` | The bass capture lacks a complete resolved window gain. | Record Main, Aux1 and program identity on each take | `hard_stop` |
| `bass_target_invalid` | The bass target curve is invalid. | Supply an ordered target curve from 20 to 200 Hz | `hard_stop` |
| `boost_over_declared_bound` | The measured boost exceeded its bound. Check the restore result before applying another tuning. |  | `hard_stop` |
| `candidate_trial_evidence_invalid` | Repeat the damaged trial set. | Repeat the damaged trial set. | `hard_stop` |
| `candidate_trial_graph_mismatch` | Trial the current compiled graph. | Trial the current compiled graph. | `hard_stop` |
| `candidate_trial_required` | Complete a trial of this candidate. | Complete a trial of this candidate. | `hard_stop` |
| `capture_slot_busy` | Another measurement holds the capture slot. Finish or cancel it, then join again. | Review the active measurement | `hard_stop` |
| `capture_timeout` | The measurement link timed out. Start over from this page to measure again — the quick microphone check runs first. |  | `session_restart` |
| `channel_map_mismatch` | JTS could not confirm that the drivers played in the expected order. Return to speaker setup and check the wiring before measuring again. |  | `hard_stop` |
| `clipped` | That was a touch loud — measuring again a bit quieter. | measuring again a bit quieter. | `silent_auto_retry` |
| `cloud_geometry_locked` | This dip looks like it belongs to the speaker rather than the room. Take this one from further out and we will use it instead. | Take this one from further out and we will use it instead. | `fix_and_retry` |
| `crossover_v2_stage2_preflight_refused` | Complete speaker setup before applying. | Complete speaker setup before applying. | `hard_stop` |
| `delay_exceeds_search_window` | The microphone may be off the spot in the picture. Re-check its placement, then try again. | Re-check its placement, then try again. | `fix_and_retry` |
| `delay_implausible` | The delay JTS measured between the drivers isn't one this speaker's geometry can produce. Measure again — if it repeats, check that nothing moved during the sweep. | Measure again — if it repeats, check that nothing moved during the sweep. | `fix_and_retry` |
| `drift_baselines_disagree` | The capture glitched — measuring again. | measuring again. | `silent_auto_retry` |
| `dry_run_requires_local_host` | Dry-run reads this machine's facts. Run it on the speaker. |  | `hard_stop` |
| `fader_above_cap` | The amplifier gain exceeds the 0 dB cap. Lower it before leveling. |  | `fix_and_retry` |
| `fingerprint_required` | Supply a candidate fingerprint. | Supply a candidate fingerprint | `hard_stop` |
| `geometry_retake_unreachable` | The room needs the microphone measured from a wider spot, and from above the mark, than this measurement can ask for. Run a Full measurement that prompts each spot on screen, and walk those spots by hand, to finish tuning this speaker. |  | `session_restart` |
| `internal_error` | Something went wrong on the speaker during that measurement. Try again. |  | `fix_and_retry` |
| `level_ambient_too_high` | The room is too loud to level. Reduce the ambient noise and try again. |  | `fix_and_retry` |
| `level_drift_at_session_gain` | The microphone read a different level at the same gain — something changed in the room. Retake. | Retake. | `fix_and_retry` |
| `level_unreachable` | The target level is unreachable at this gain. Check the amplifier and microphone. |  | `fix_and_retry` |
| `locate_failed` | Couldn't hear the speaker clearly. Check the volume and the microphone, then try again. | Check the volume and the microphone, then try again. | `fix_and_retry` |
| `measure_box_not_ready` | Finish the protected speaker setup. | Finish the protected speaker setup | `hard_stop` |
| `measure_gain_adjusted` | The driver needs a clearer timing measurement. JTS will keep this take and measure once more at a higher test level. | JTS will keep this take and measure once more at a higher test level. | `silent_auto_retry` |
| `measure_spl_calibration_required` | JTS needs microphone calibration to check the sound level during this measurement. Register calibration with microphone sensitivity, then measure again. | Register microphone calibration | `hard_stop` |
| `measurement_baseline_unavailable` | JTS could not build this program's baseline. Review the saved speaker setup before measuring. | Review speaker setup | `hard_stop` |
| `measurement_branch_channels` | This measurement needs the woofer and tweeter on separate supported outputs. Review their output assignments in speaker setup. | Review speaker outputs | `hard_stop` |
| `measurement_candidate_invalid` | JTS cannot read the selected tuning. Select a valid saved tuning, then measure again. | Select a valid tuning | `hard_stop` |
| `measurement_candidate_required` | This measurement needs a saved tuning to test. Select the tuning, then measure again. | Select a tuning | `hard_stop` |
| `measurement_candidate_speaker_mismatch` | The selected tuning uses a different speaker setup. Select a tuning for this speaker. | Review speaker outputs | `hard_stop` |
| `measurement_filters_invalid` | JTS cannot read all the filters in this tuning. Select a valid saved tuning before measuring again. | Select a valid tuning | `hard_stop` |
| `measurement_graph_unavailable` | JTS could not install or restore the measurement audio setup. Check the speaker's audio state, then start a new session. | Start a new session | `hard_stop` |
| `measurement_mic_unidentified` | Select a known measurement microphone. | Select a known measurement microphone | `hard_stop` |
| `measurement_scope_invalid` | JTS cannot measure the selected tuning layer. Select a supported measurement layer. | Select a measurement layer | `hard_stop` |
| `measurement_targets_missing` | JTS does not have a measurement target for every driver this speaker declares, so it cannot measure them. Finish speaker setup so each driver is assigned to an output, then measure again. | Finish speaker setup | `hard_stop` |
| `measurement_volume_drift` | JTS could not confirm the speaker was at the level it set for measuring, so it stopped rather than record a measurement it cannot trust. Try measuring again; if it keeps happening, restart the speaker from the system page. |  | `hard_stop` |
| `mic_clipping` | The microphone clipped. Check the microphone and lower the level. |  | `fix_and_retry` |
| `mic_feed_lost` | The microphone stopped sending samples. Check its connection and try again. |  | `fix_and_retry` |
| `mic_not_observing` | The microphone did not hear the speaker. Check its position and connection. |  | `fix_and_retry` |
| `noisy_room_linearity` | The room got loud during that measurement — quiet it and try again. | quiet it and try again. | `fix_and_retry` |
| `not_found` | Select a candidate from the bank. | Select a candidate from the bank | `hard_stop` |
| `pilot_level_collapse` | The test tones didn't rise clearly above the room — it was too loud, or the speaker too quiet, for this check. Quiet the room or move the microphone closer, then try again. | Quiet the room or move the microphone closer, then try again. | `fix_and_retry` |
| `position_hold_expired` | Nothing reported the microphone reaching its next position, so the measurement stopped waiting. Start over from this page when every position can be confirmed as the microphone arrives. |  | `session_restart` |
| `position_target_missing` | This measurement did not say where the microphone should be, so it stopped rather than record an unknown position. Start over from this page. |  | `session_restart` |
| `program_plan_shape_invalid` | JTS could not read the measurement plan. Submit a complete plan in the current format. | Review measurement settings | `hard_stop` |
| `program_profile_incomplete` | Some of this speaker's safety limits are still missing, so JTS did not play the measurement signal. Add them under Advanced in speaker setup, then save and measure again. | Add the missing limits | `hard_stop` |
| `program_profile_missing` | This speaker's driver details are not finished, so JTS has no safety limits to measure within. Finish the driver details in speaker setup, then measure again. | Finish speaker setup | `hard_stop` |
| `program_profile_not_confirmed` | JTS could not use this speaker's saved safety limits, so it did not play the measurement signal. Review the limits in speaker setup and save them again, then measure. | Review safety limits | `hard_stop` |
| `program_unplayable` | JTS could not play the measurement signal within the speaker's safe limits. Re-check the driver details in speaker setup, then measure again. |  | `hard_stop` |
| `protection_not_separable` | JTS played the measurement fine, but the safety limits it had to keep in place overlap the crossover you have set, so it cannot tell the two apart well enough to trust the result. Change the crossover frequency in speaker setup, then measure again. |  | `hard_stop` |
| `protection_sweep_too_low` | JTS played the measurement fine, but it swept this driver lower than the driver's own protection lets through, so the bottom of the sweep is too quiet to trust. Re-check this driver's protection settings in speaker setup, then measure again. |  | `hard_stop` |
| `round_manifest_missing` | Bank the run manifest with this round. |  | `hard_stop` |
| `round_manifest_unfinalized` | Wait for the run to finish. |  | `hard_stop` |
| `round_set_unknown` | Select a set listed in the run manifest. |  | `hard_stop` |
| `round_take_selection_required` | Select a retained take from this set with the take selector. |  | `hard_stop` |
| `round_take_unknown` | Select a retained take from this set. |  | `hard_stop` |
| `seat_anchor_unusable` | Run jasper-seat-level with the current microphone, then measure. | Run jasper-seat-level with the current microphone, then measure | `hard_stop` |
| `seat_level_watchdog_expired` | Leveling timed out. Check the audio connection and try again. |  | `fix_and_retry` |
| `session_ceiling_expired` | The whole measurement ran out of time while it was still waiting for the microphone to reach a position. Start over from this page once the microphone can be moved through the walk more quickly. |  | `session_restart` |
| `snr_floor` | The room is too loud right now, or the microphone is too far away. Quiet the room or move the microphone closer, then try again. | Quiet the room or move the microphone closer, then try again. | `fix_and_retry` |
| `sound_design_revision_unavailable` | Measure the current Sound design. | Measure the current Sound design. | `hard_stop` |
| `speaker_shape_unsupported` | JTS can measure a single full-range speaker or a two-way active crossover, and this speaker is neither. There is nothing to retry — check the drivers declared in speaker setup. | Open speaker setup | `hard_stop` |
| `spl_ceiling_exceeded` | The measurement stopped because the microphone heard the speaker louder than the commissioning stop. Lower the level and measure again. |  | `hard_stop` |
| `spl_level_unsettled` | The microphone level did not settle. Try again. |  | `fix_and_retry` |
| `spl_target_uncapturable` | The microphone cannot measure the requested level. Use a suitable microphone. |  | `fix_and_retry` |
| `user_stopped` | You stopped the measurement. Start over from this page when you're ready. |  | `session_restart` |
| `verify_crossover_region` | The two drivers didn't blend as designed where they hand over. Re-measure to fit it again. | Re-measure to fit it again. | `verify_fail` |
| `verify_inconclusive` | The check was inconclusive — this measurement had less usable sound to compare than the tuning did. Re-verify to try again. | Re-verify to try again. | `verify_fail` |
| `verify_level_shift` | The microphone's levels changed between measurements, so this check couldn't settle. Try again — if it repeats, re-measure. | Try again — if it repeats, re-measure. | `verify_fail` |
| `verify_out_of_tolerance` | The result didn't quite match the prediction. Try again. | Try again. | `verify_fail` |
| `volume_latch_unconfirmed` | The amplifier gain could not be confirmed. Check the audio connection. |  | `fix_and_retry` |
| `volume_restore_deferred` | Measurement stopped because another volume claim is active. | Start a new measurement after playback settles | `hard_stop` |
| `volume_unresolved` | JTS could not confirm the listening volume was restored. Recover the safe volume before continuing. |  | `volume_recovery` |
| `walk_candidate_not_measurable` | A summed tuning test must use that tuning's own levels and alignment. Remove the separate level or alignment overrides. | Use the tuning's own settings | `hard_stop` |
| `walk_commissioning_stop_unset` | This speaker has no sound level stop set for measurements. Set the stop level in speaker setup before measuring. | Set the measurement stop level | `hard_stop` |
| `walk_delay_not_accepted` | The selected driver and delay settings do not match. Correct the delay settings before starting. | Correct the delay settings | `hard_stop` |
| `walk_lateral_group_already_planned` | This session already has a plan for these positions. Start a new session for the new plan. | Start a new session | `hard_stop` |
| `walk_level_match_no_evidence` | JTS has no measured driver levels to match. Measure the driver levels before asking it to match them. | Measure the driver levels | `hard_stop` |
| `walk_level_policy_invalid` | Correct the measurement level settings before starting. | Correct the level settings | `hard_stop` |
| `walk_mover_mismatch` | Match the microphone movement settings in the plan and session. | Match the movement settings | `hard_stop` |
| `walk_nothing_playable` | This plan contains only separate driver measurements, which this runner cannot play. Run it through the guided speaker measurement. | Open guided measurement | `hard_stop` |
| `walk_over_capture_capacity` | This plan has more recordings than one session can hold. Split the positions across separate sessions. | Split the measurement plan | `hard_stop` |
| `walk_over_mover_envelope` | A measurement position is beyond the stated movement range. Move that position within the range. | Adjust the positions | `hard_stop` |
| `walk_polarity_not_accepted` | The selected driver and polarity settings do not match. Correct the polarity settings before starting. | Correct the polarity settings | `hard_stop` |
| `walk_regime_unsupported` | This session cannot run that type of measurement. Choose a measurement type the session supports. | Choose a measurement type | `hard_stop` |
| `walk_repeats_unsupported_yet` | Use one take per position in the guided measurement. | Set one take per position | `hard_stop` |
| `walk_schema_version_unsupported` | Submit the measurement plan in the current request format. | Review measurement settings | `hard_stop` |
| `walk_stimulus_not_accepted` | The test signal does not fit this measurement plan. Choose a supported signal for its positions. | Correct the test signal | `hard_stop` |
| `walk_stop_no_longer_valid` | A saved measurement position is no longer valid. Correct that position before starting. | Correct the saved position | `hard_stop` |
| `walk_template_not_accepted` | The test signal settings include position fields that the plan must set. Remove those fields from the signal settings. | Correct the signal settings | `hard_stop` |
| `wired_mic_missing` | Connect the measurement microphone. | Connect the measurement microphone | `hard_stop` |
<!-- END GENERATED FAULT TABLE -->

<!-- BEGIN GENERATED TOOL MENU (scripts/generate-tuning-tool-menu.py -- do not hand-edit) -->
| Tool | Does | Authority | Where |
|---|---|---|---|
| `jasper-basic-profile review\|apply` | Review and apply the basic profile -- the chosen crossover plus per-driver trim, delay and polarity, with no linearization and no blend correction, replacing the live tune and deleting no evidence. | mutating-with-gates | `jasper/cli/basic_profile.py` |
| `jasper-mic-calibration models\|fetch\|upload\|show` | Register the household's measurement microphone: fetch its vendor calibration by serial or store a file you already have, and remember that mic so every measurement resolves its calibration from one record. A box with no record measures uncalibrated. | advisory (`fetch`/`upload` write; `models`/`show` do not) | `jasper/cli/mic_calibration.py` |
| `jasper-seat-level` | Play the room/bass summed measurement sweep and adjust the fader until the calibrated mic's loudest window (max_window_db_spl) reads the target; bank the session gain. PRECONDITION: `amixer -c <card>` shows the mic's capture control at 100%, where its Sens Factor is quoted, or every absolute SPL is wrong by the shortfall. | measured | `jasper/cli/seat_level.py` |
| `jasper-angle-capture serve` | Serve the microphone arm against the daemon's position gate. | mutating (`serve` moves the arm) | `jasper/cli/angle_capture.py` |
| `jasper-measure` | Measure this speaker once, bank the takes, print their ids | measured | `jasper/cli/measure.py` |
| `jasper-crossover-prescriber contract\|judge\|compose\|status` | Judge and compose prescription documents; serve contracts and read status. | advisory (judge, contract and status read; compose banks a candidate) | `jasper/cli/crossover_prescriber.py` |
| `jasper-round run\|placed\|status\|wait\|apply` | Start an inline plan, place the microphone, read progress and bank a run. | mutating-with-gates (`run`/`placed`/`wait`/`apply` write; `status` reads) | `jasper/cli/round.py` |
| `jasper-round-views entry\|frozen\|repeat\|repeat-floor\|candidates\|agreement\|co-metrics\|directivity\|per-seat\|cloud-binding\|forward-model\|sweep\|frequency\|distortion\|dsp-replay\|dsp-levels\|classify-features\|findings\|close-reference\|delay-landscape\|delay-confirm\|room\|room-grade\|bass\|bass-compare\|bass-fit-table\|inventory\|speaker-fit` | Read measured round evidence, including repeat --set spread across takes. Answers use stdout; detailed reports use files. | advisory (analysis views save artifacts) | `jasper/cli/round_views/__init__.py` |
| `jasper-null` | Play the summed reverse null and bank one row per coordinate. Measures only; grades nothing. | measured | `jasper/cli/null_door.py` |
| `jasper-audition start\|stop\|status` | Play this speaker at a reduced DSP layer, then put it back | mutating (runtime only; durable graph untouched -- ADR-0193) | `jasper/cli/audition.py` |
| `jasper-declare-geometry set\|show` | Declare measurement rig geometry: speaker/mic heights, distance and optional ceiling, so entanglement_floor_hz has a provenance-labeled, non-measured source on rigs where the measured reflection finder structurally never fires (issue #3502); and optional front/side wall distances for jasper-round-views room. | advisory (`set` writes; `show` does not) | `jasper/cli/declare_geometry.py` |
<!-- END GENERATED TOOL MENU -->

## Debugging — where to look first

Use the `link` from `jasper-round run`, or `crossover_url` from `jasper-crossover-prescriber status`; the page is `/sound/speaker/crossover/` over HTTPS with the speaker's local CA, and banked rounds are at `/sound/measurements/`. First read the structured reason, run `jasper-doctor --json`, inspect `:8780/state`, and fetch logs with `bash scripts/fetch-pi-logs.sh`. Tool details and exit codes live in each tool's `--help`; the [methodology](tuning-methodology.md) and [doctrine](measurement-loop-doctrine.md) own science and authority rules.
