Astra (GPT-6 Codex)

L6 is implemented. Room judge requires an explicit set on multi-set rounds.
Taper refusals include all offending bins, filter contributions and tolerance.
`judge --preview` reports margins and residuals without writes. The room view
uses the contract's feature and limit writer. The runbook was regenerated.

Files touched:

- `jasper/active_speaker/crossover_v2/prescription_contract.py`
- `jasper/active_speaker/crossover_v2/prescription_document.py`
- `jasper/active_speaker/crossover_v2/refusal_copy.py`
- `jasper/active_speaker/crossover_v2/room_analysis.py`
- `jasper/active_speaker/crossover_v2/room_prescription.py`
- `jasper/active_speaker/crossover_v2/room_views.py`
- `jasper/cli/crossover_prescriber.py`
- `jasper/cli/round_views/_common.py`
- `tests/test_crossover_v2_room_prescription.py`
- `tests/test_prescription_contract.py`
- `tests/test_round_views_room.py`
- `tests/test_round_views_set_resolution.py`
- `docs/tuning-operator-runbook.md`
- `docs/doc-map.toml`
- `report.md`

Checks used `/Users/jaspercurry/Code/JTS/.venv/bin/` tools and `PYTHONPATH=$PWD`.
Pytest used `-q -p no:cacheprovider`. The initial five-file run had 216 passes
and 15 failures from one old set-code assertion. After its fix, the focused
rerun passed all 102 tests. All named read-only guard files passed: 219 passed,
1 skipped. Ruff passed. The required base-to-HEAD mypy command passed all
8 source files, using base `c3677d689500faa2cfdc8a765abcc745e785be2c`.
`python scripts/generate-tuning-tool-menu.py` and the menu guard passed.
After the import and docs-map fixes, the final six-file run passed all 232
tests, including both failed local guards. Ruff and mypy passed again.

`bash scripts/test-fast` with the requested tool overrides ended with
`==> test-fast: FAILED` (39 failed, 9272 passed, 37 skipped,
74 errors; routing-policy phase: 85 passed). Two local failures were fixed:
the static import cycle and the report's docs-map entry. The remaining failures
are socket-bind permission errors or the renderer's blocked child-process bounds.
The conductor must rerun this lane outside the sandbox.

Judgment call: preview reports room-filter totals against the median's fixed
level reference. Negative taper margins remain data; they do not refuse preview.
Admission entries now live in `admit_boost`, beside the raw persistence features.

Blocked: this runtime does not expose the built-in `/code-review` command.
An offline review of the full diff was completed. No requested feature was omitted.
