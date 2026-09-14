Astra (GPT-6)

L3c, branch `codex/l3c-mark-distance`, base `e49258c57`.

Files changed:

- `jasper/active_speaker/plan_run.py`: pass the resolved prompt's mark distance into the planned pose. The existing manifest and host carry it into the saved take.
- `jasper/active_speaker/crossover_v2/measurement_context.py`: two seat takes with absent distance have a known basis. Missing bearing distance stays unknown.
- `jasper/active_speaker/angle_capture.py`: serialize seat offsets as JSON lists. The executor otherwise fails to fingerprint a seat request before capture.
- `tests/crossover_v2_banked_round.py`: run the executor fixture through the real plan.
- `tests/test_correction_crossover_v2_wired.py`: pin default and explicit bearing distances, and absent seat distance, including failed analysis.
- `tests/test_crossover_v2_frequency_view.py`: pin compatible executor bearing and seat bases, and unknown bearing distance.
- `docs/doc-map.toml`: classify the required root report as a session artifact.
- `report.md`: results and the review blocker.

Judgment: resolve distance where the plan already holds the pose prompt. Do not add a second geometry default in the host.

Commands and results:

```sh
PYTHONPATH=$PWD /Users/jaspercurry/Code/JTS/.venv/bin/pytest tests/test_correction_crossover_v2_wired.py tests/test_crossover_v2_frequency_view.py tests/test_round_views_set_resolution.py tests/test_plan_run.py tests/test_angle_capture_take.py tests/test_angle_capture_seam.py tests/test_angle_capture_trigger.py tests/test_round_views_speaker_fit.py tests/test_active_speaker_crossover_v2_room_grade.py -q -p no:cacheprovider
```

Tail: `645 passed in 49.32s`. After the final fixture assertion, its 16 consumer checks passed in 4.57s. The new bearing check first reproduced `None` instead of `1.0`.

```sh
/Users/jaspercurry/Code/JTS/.venv/bin/ruff check jasper/active_speaker/angle_capture.py jasper/active_speaker/crossover_v2/measurement_context.py jasper/active_speaker/plan_run.py tests/crossover_v2_banked_round.py tests/test_correction_crossover_v2_wired.py tests/test_crossover_v2_frequency_view.py
git diff --name-only --diff-filter=ACM e49258c57...HEAD -- 'jasper/*.py' 'jasper/**/*.py' | xargs env PYTHONPATH=$PWD /Users/jaspercurry/Code/JTS/.venv/bin/mypy
set -o pipefail; PYTEST=/Users/jaspercurry/Code/JTS/.venv/bin/pytest RUFF=/Users/jaspercurry/Code/JTS/.venv/bin/ruff PYTHONPATH=$PWD bash scripts/test-fast
```

Ruff: `All checks passed!`. Mypy: `Success: no issues found in 3 source files`.

Fast lane tail:

```text
12 failed, 8033 passed, 37 skipped, 12 errors in 415.40s (0:06:55)
==> test-fast: FAILED
```

Eight failures in `test_crossover_v2_remote_tier.py` could not bind sockets (`PermissionError`). Three failures and twelve setup errors in `test_active_speaker_emit_bench_loop.py` could not apply child-process render limits (`preexec_fn`). Per the brief, the conductor must run these outside the sandbox. The other failure was the required root report missing from the docs map; its classification is now added.

Retest: `PYTHONPATH=$PWD /Users/jaspercurry/Code/JTS/.venv/bin/pytest tests/test_docs_impact.py -q -p no:cacheprovider` → `10 passed in 0.42s`. The full fast lane was not repeated; only the stated sandbox failures remain unresolved. `git diff --check` passed.

Blocker: this worker exposes no built-in `/code-review` tool. A local diff review passed; the conductor must run the required built-in review. No CLI verbs, flags, or help text changed, so menu generation does not apply. No network, push, deploy, or SSH commands ran.
