Astra (Codex)

L7 core and dry-run are built. Bass preflight checks the declared 20–60 Hz
target band against banked ambient with the existing SNR helper and refusal
code. The ladder is session, session −5, −10, and −15 dB. An explicit
`--level-db L --dry-run` still checks just L.

Files changed:

- `jasper/active_speaker/preflight.py`: bass-band admission.
- `jasper/active_speaker/bass_levels.py`: ladder reports and execution at each
  held pose, using the existing run, level-window, and placement owners.
- `jasper/bass_extension/measurement.py`: one flat target from 20 to 60 Hz,
  relative to the existing fit reference band, with 3 dB tolerance.
- `jasper/cli/_run_request.py`: bass dry-run output.
- `jasper/cli/round_views/_bass_inputs.py`: reuse `fit_run` through
  `join_bass_rounds`; write `bass_table.json` into the last round's packet.
- `tests/test_preflight.py`, `tests/test_cli_round.py`, `tests/test_plan_run.py`,
  `tests/test_crossover_v2_frequency_view.py`: admission, dry-run, pose hold,
  cleanup, partial capture, and multi-round join checks.
- `docs/tuning-operator-runbook.md`: dry-run use; generated menu refreshed.
- `report.md`: this report.

Checks run:

- `PYTHONPATH=$PWD /Users/jaspercurry/Code/JTS/.venv/bin/pytest tests/test_preflight.py tests/test_cli_round.py tests/test_plan_run.py tests/test_crossover_v2_frequency_view.py tests/test_measurement_mover_agnostic.py tests/test_transport_endpoint_preservation.py tests/test_canonical_target_registration.py tests/test_active_speaker_emit_gate.py tests/test_log_event_conventions*.py tests/test_tuning_tool_menu_generator.py tests/test_ci_classifier.py tests/test_docs_linkcheck.py -q -p no:cacheprovider`
  — 531 passed, 1 skipped. Final sequence retest: 2 passed.
- `/Users/jaspercurry/Code/JTS/.venv/bin/ruff check` on all nine changed Python
  files — passed.
- `git diff --name-only --diff-filter=ACM e49258c57...HEAD -- 'jasper/*.py' 'jasper/**/*.py' | xargs env PYTHONPATH=$PWD /Users/jaspercurry/Code/JTS/.venv/bin/mypy`
  — passed, five source files.
- `PYTHONPATH=$PWD /Users/jaspercurry/Code/JTS/.venv/bin/python scripts/generate-tuning-tool-menu.py`
  and the same command with `--check` — passed.
- `set -o pipefail; PYTEST=/Users/jaspercurry/Code/JTS/.venv/bin/pytest RUFF=/Users/jaspercurry/Code/JTS/.venv/bin/ruff PYTHONPATH=$PWD bash scripts/test-fast`
  — sentinel `==> test-fast: FAILED`. Selected tests: 4516 passed, 59 skipped,
  3 failed, 36 errors. The failures and 12 errors come from child-process
  render bounds in `test_active_speaker_emit_bench_loop.py`; the other 24
  errors are denied socket binds in `test_measurement_hold.py`. Left for the
  conductor's outside run, as instructed. Log: `/tmp/l7-test-fast.log`.
- `git diff --check e49258c57...HEAD` — passed. Local diff/caller review done;
  the built-in `/code-review` is not exposed in this session.

Judgment call: use one round per pose and admitted level. One outer isolation
hold and one placement cover all levels at a pose. This keeps the current
single-level run contract. A capture that needs human recovery ends the
sequence with a partial manifest.

Blocked wiring: the brief reserves `jasper/cli/round.py`, so `--levels auto`,
the live host dispatch, and the call from `wait` are not connected. Neither
reserved file was edited. The conductor must call `bass_level_ladder`, bind
each round through `run_bass_levels(..., prepare=...)`, retain the returned run
IDs, bank their views at `wait`, then call
`join_bass_rounds(round_dirs, candidates=...)`. L5's `trial` path is absent at
this base. No push, deploy, SSH, fetch, or network command was run.
