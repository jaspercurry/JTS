# Brief: lane A — retire the room product and the bass wizard (wave 1, rows 1.1–1.3)

You are running the **retire** lane of the JTS seat-matched tuning program
(`seat-tuning-program/PLAN.md` on branch `claude/loudspeaker-tuning-architecture-iephfa`;
tracking issue #4502). You move the shared pieces the toolbox keeps, then delete
what ADR-0259 §3 retires, with a verdict per module. Four PRs, in order, each
from a fresh `origin/main` branch `claude/seat-w1-<row>-<slug>`. Deletion tier:
`scripts/test-merge`, not just `test-fast`. Load the `web-ui` skill before
touching `jasper/web/` or `deploy/assets/`.

## 0. Read first

`AGENTS.md`; the plan §1, §2, §7; **ADR-0259** (what retires, §3; what moves
first, §4; §5 on the bass apply pathway) and **ADR-0260** (no nearfield rung),
both merged via PR #4517; ADR-0255 §Consequences (the browser path's file list);
ADR-0222 (the deletion rules: no shims, no flags, no "removed" comments);
`docs/adr/0018-*.md` for what the bass deadness tests were guarding. Every
`file:line` below was verified at main `f84d7da`–`72bc347` (2026-09-08);
re-verify at your HEAD and stop a row whose premise is false.

## 1. Facts (verified 2026-09-08)

**The shared daemon.** `jasper/web/correction_setup.py` is the HTTPS measurement
daemon for room, crossover, sync and bass routes (its docstring, `:1-30`);
route table paths include `/start /status /upload-capture /upload-noise
/next-position /repeat-position /verify /apply /propose /propose/apply
/interpret /envelope /entry-status /sessions /session/delete /session-report
/reset /autolevel/* /local-capture/setup /measurements /measurements/data
/sound/room/` (room) beside `/crossover/*`, `/crossover/v2/*`, `/sync/*`,
`/bass`, `/bass/status`, `/calibration/*`, `/test-tone`, `/healthz`,
`/sound/setup/` (stay). `jasper/web/correction_handlers.py` holds the room
handlers (`_handle_start/_status/_upload_capture/_upload_noise/_next_position/
_repeat_position/_verify/_apply/_propose/_propose_apply/_interpret/_envelope/
_entry_status/_sessions/_session_delete/_session_report/_reset/_autolevel_*/
_local_capture_setup`, `_maybe_auto_revert`, `_write_no_room_correction_config`,
`_room_graph_artifact_path`, `_run_locked_room_reset`,
`_snapshot_running_room_graph`, `_running_graph_*`, `_load_measurement_baseline`,
`_pre_measurement_restore_target`, `_wait_for_new_autolevel_run`) beside the
speaker walk's (`_handle_crossover_v2_*`, `_handle_crossover_*`,
`_handle_calibration_*`, `_handle_test_tone`). `jasper/web/correction_capture.py`
holds the shared capture slot (`_run_capture`, `_begin/_get/_set/_clear_capture_slot`,
`_reserve_start_slot`, `_publish_capture_waiting`, `_request_capture_stop`)
beside room-only readiness and household-mic helpers
(`_room_correction_readiness*`, `_read_room_correction_readiness*`,
`_normalize_room_readiness`, `_get_or_create_session`, `_replace_session`,
`_schedule_measurement_sweep`, `_schedule_repeat_sweep`,
`_run_session_background_audio`, `_active_state_for_session`,
`_assert_level_match_level`, `_assert_room_authority_current`,
`_household_level_door`, `_resolved_household_mic`, `_save_household_mic`,
`_household_mic_prefill_payload`, `_calibration_root` :844,
`_household_mic_path` :853, `_default_setup_calibration_for_spec` :946,
`_calibration_device_mismatch`, `_device_id_hash`).

**Shared pieces misfiled in the room product.** `jasper/correction/level_match.py`
(live through `web/correction_crossover_backend.py`); the household mic record
`jasper/correction/household_mic.py` (`read_household_mic` :183,
`resolve_household_mic_calibration` :240) with the three helpers above and
`web/correction_crossover_v2.py:2396` `default_setup_calibration_for_v2`;
`correction/acoustic_quality.SNR_BANDS_HZ`, identity-pinned to
`snr_policy.CROSSOVER_SNR_BANDS_HZ`'s first four rows
(`tests/test_audio_measurement_snr_policy.py:59-62`); `correction/variance_cap.py:53-59,196-266`
(depth-cap rule) and `correction/target.py` (`flat_target` :19, `harman_target`
:24, `house_curve` :48) — **lane C moves these two into
`jasper/audio_measurement/room_limits.py` as its first commit**; check
`origin/main` for that module before touching them (see 1.1 step 3).

**The room product proper.** `jasper/correction/` 28 modules (`session.py`
2,364 lines; `acceptance`, `autolevel`, `browser_audio`, `confidence`,
`envelope` (screen UI; imports `calibration_agent`), `status`, `state_guard`,
`runtime_integrity`, `runtime_safety` (the allowlisted `correction →
active_speaker.runtime_contract` edge), `strategy`, `failures`, `evidence`,
`replay_artifacts`, `bundles`, `bundle_tools`, `interop`, `fir_runtime`,
`artifacts`, `acoustic_quality`, `_numbers`, `playback`? (verify: row 2.1 of
the right-size program may have already moved it), `spatial`);
`web/correction_room_flow.py` (419 lines, the room page; `getUserMedia`
secure-context split `:22-29`); `web/correction_tuning.py` (335 lines, the
paid tuning assistant backend for the room wizard); room JS:
`deploy/assets/correction/js/main.js` (2,899 lines; the walk's JS may share the
directory — census first) and `deploy/assets/shared/js/measurement-audio.js`
(393); `jasper-correction-bundle` entry point (`jasper/cli/correction_bundle.py`,
the only reader of `fir_runtime`); `jasper/cli/doctor/correction.py` (doctor's
room section; imports four web adapters, allowlisted at
`tests/test_correction_boundary_ssot.py:172-174` region — verify). Nothing in
`jasper/active_speaker/` imports `jasper.correction` (pinned).

**The in-product LLM client.** `jasper/calibration_agent/` (13 Python files,
4,477 lines) imported by `correction/envelope.py`, `web/correction_handlers.py`,
`web/correction_tuning.py`, `web/sound_setup.py`; it imports back into
`web/sound_setup.py` (`calibration_agent/sound_actions.py:62`). Entry point
`jasper-calibration-agent` (`jasper.calibration_agent.cli:main`).

**The bass wizard.** `jasper/bass_extension/ladder.py` (611); the
apply/bypass/recover transaction in `bass_extension/__init__.py` (665; zero
production callers); deadness tests `tests/test_bass_extension_plan_status.py`
(61), `tests/test_bass_extension_runtime_gate_ssot.py` (196),
`tests/test_bass_extension_ladder.py` (703); `tests/test_bass_extension_profile.py`
(1,560) is **mixed**: `BassExtensionProfile` tests stay, the
`apply_bass_extension`/`bypass_bass_extension` tests go — excise functions, not
the file. Docs that retire: `docs/bass-extension-waves/wave-4-commissioning-backend.md`,
`wave-6-ui.md`, `bass-commissioning-ux.md` (ADR-0229's exemption covers the
plan; ADR-0259 §3 retires these three). Stays: `alignment`, `targets`,
`adapters/`, `limiter_evidence`, `profile` (absorbed later by lane D),
`bench/` and their tests.

**Multiroom reads room PEQs from config text**, not from `jasper.correction`
(`jasper/sound/camilla_yaml.py:1012-1063` `extract_room_peqs_from_config_text`;
callers in `jasper/multiroom/`). Do not break that reader.

## 2. Rows

### 1.1 — Census, then move the shared pieces (branch `claude/seat-w1-1-1-move-shared`)

1. **Census first, in the PR body:** every importer of `jasper.correction.*`,
   `jasper.calibration_agent.*`, and every room-only name in
   `correction_setup/handlers/capture`, from `jasper/web/correction_crossover_v2*.py`,
   `jasper/active_speaker/`, `jasper/cli/` (incl. `doctor/`), `jasper/multiroom/`,
   `jasper/sound/`, `deploy/` (nginx confs, systemd units, `deploy/bin`),
   `scripts/`, `tests/`. Classify each: stays (crossover/sync/calibration path),
   moves (rule 4 of ADR-0259), retires. A verdict per symbol; "no importer" is
   not enough — the LLM operator invokes CLIs from the shell, and nginx serves
   pages.
2. **Moves**, behavior-identical, AST identity for moved bodies, deferred
   imports re-resolved (`importlib.util.resolve_name` + `find_spec`; the
   right-size program lost a lane to this once):
   - `correction/level_match.py` → `jasper/audio_measurement/level_match.py`
     beside `ramp.py`; repoint `web/correction_crossover_backend.py`.
   - Household mic record: `read_household_mic`, `resolve_household_mic_calibration`
     and the three path/hint helpers → `jasper/audio_measurement/household_mic.py`
     beside `mic_identity.py` and `calibration.py`; `default_setup_calibration_for_v2`
     becomes a thin call; the wired minter's hint path (`wired_capture.setup_from_hint`)
     unchanged. This also closes PR #4138's held-back item.
   - `SNR_BANDS_HZ` → `jasper/audio_measurement/snr_policy.py`; keep the
     prefix-equality pin, retarget the identity pin.
3. **Coordinate with lane C on `room_limits.py`.** If `jasper/audio_measurement/room_limits.py`
   exists on `origin/main`, consume it and delete `correction/variance_cap.py`
   and `target.py` in 1.2. If it does not, create it here with exactly the shape
   lane C's brief (`briefs/wave-2-room-candidate.md` §2.1 step 1) specifies —
   the depth-cap rule and constants from `variance_cap.py`, `flat_target`/
   `harman_target`/`house_curve` — and comment on #4502 so lane C consumes it
   instead of writing its own. Never two copies alive across a merge.

Proof: boundary tests green (`tests/test_correction_boundary_ssot.py`); the
crossover walk still opens a v2 session on a fixture; `scripts/test-merge`.
Gate: two-pass `/code-review` (relocations).

### 1.2 — Delete the room product (branch `claude/seat-w1-1-2-retire-room-product`)

Delete whole: `jasper/correction/` (everything not moved in 1.1 — including
`runtime_safety.py`; its allowlisted edge goes with it and the boundary test's
allowlist row is removed), `web/correction_room_flow.py`,
`web/correction_tuning.py`, `jasper/cli/correction_bundle.py` and the
`jasper-correction-bundle` entry point, the room JS modules and
`measurement-audio.js`, the `/sound/room/` nginx location if one exists, all
their tests. Delete surgically: the room routes in `correction_setup.py`'s
table and the room handlers in `correction_handlers.py` and the room-only
helpers in `correction_capture.py` (the lists in §1); keep the crossover,
sync, calibration, test-tone (one implementation) and healthz routes and the
capture slot. `jasper/cli/doctor/correction.py`: keep only what reads the
applied room PEQ state from the running config or the applied candidate; if
nothing remains, delete the section and its menu row. `docs/room-correction-information-design.md`
retires (SUPERSEDED by ADR-0259; the toolbox's runbook "Room" section replaces
it — lane B's row 1.7); fix links; `docs/doc-map.toml` rows.

Verdict per module in the PR body (SUPERSEDED by the toolbox's seat program,
views and candidate kind — cite ADR-0259 §3). ADR-0222's deletion rules: no
shims, no flags, no "removed" comments. Proof: `scripts/test-merge` green;
`python3 scripts/docs-linkcheck.py --all`; a v2 session still opens and a
calibration upload still works on fixtures; `jasper-doctor` still runs on a
fixture state; the emitter's `room_peqs` stage and `extract_room_peqs_from_config_text`
untouched (multiroom pin). Gate: `/code-review` high; D-tier (deletion).

### 1.3 — Delete the in-product LLM client (branch `claude/seat-w1-1-3-retire-calibration-agent`)

`jasper/calibration_agent/` whole, the `jasper-calibration-agent` entry point,
its hooks in `web/sound_setup.py` (both directions of the edge), its tests and
docs rows. Verdict: SUPERSEDED by the operator-is-the-LLM model (master-plan R4,
ADR-0259 §2). Proof: `scripts/test-merge`; the `/sound/setup/` page still
renders on a fixture. Gate: `/code-review` medium.

### 1.4 — Delete the bass wizard (branch `claude/seat-w1-1-4-retire-bass-wizard`)

`bass_extension/ladder.py`; the `apply_bass_extension`, `bypass_bass_extension`,
`recover_pending_bass_extension_apply` pathway in `bass_extension/__init__.py`
(leave the package importable; lane D adds the candidate kind);
`tests/test_bass_extension_plan_status.py`, `tests/test_bass_extension_runtime_gate_ssot.py`,
`tests/test_bass_extension_ladder.py`; the apply/bypass functions inside
`tests/test_bass_extension_profile.py` (keep the profile tests); the three
wave docs; update `docs/bass-extension-waves/README.md`'s status table and fix
links. Verdict: SUPERSEDED by the engine's candidate apply (ADR-0259 §5).
Proof: `scripts/test-merge`; `/state.bass_extension` summary still reports on a
fixture; `_bass_extension_emission` tests untouched. Gate: `/code-review` medium.

## 3. Rules that bind this lane

- Standing rules in the kickoff snippet and plan §7.
- Move before delete; never two copies alive across a merge; AST identity and
  import re-resolution on every move; two-pass review on relocations.
- Do not touch the crossover-v2 engine, `measurement_programs.py`, the round
  views, the candidate model or the emitter (lanes B, C, D and the tuning-flow
  agent). Do not delete `bench/`, `limiter_evidence`, `adapters`, `alignment`,
  `targets`, `profile`.
- The household mic record survives (ADR-0255 §3); calibration upload survives;
  the shared capture slot survives; the sync wizard survives.
- Big deletions are still single-concern PRs; merge in order; rebase the next
  after each merge.

## 4. Report back

Per PR: link, line delta, the census table (1.1), verdicts per module, the
moved symbols with their new homes, validation sentinels, "stale, not fixed
here", and any premise in §1 found false at HEAD with what you did instead.
