# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Run the crossover wizard's JS harnesses inside the pytest CI lane.

``deploy/assets/correction/js/crossover/main.js`` (the v2 crossover
measurement wizard's client) is JavaScript, exercised by a family of Node
harnesses under ``tests/js/crossover_*_test.mjs`` — action-row authority, the
applied chip, hidden polling, Start Over, Stop rendering, and (2026-07-24
gauge fix) the candidate-review linearization/flatness disclosure. Scoped
re-review (2026-07-24) found none of these ran in any automated lane: the CI
``js`` job's explicit harness list omits the whole family, and
``scripts/check-js-syntax.sh`` / pre-commit only ``node --check`` them (syntax
only, no assertions executed) — an unexecuted test cannot catch the
regression it was written for.

Bridging them through pytest (mirroring ``tests/test_capture_page_js.py``)
keeps them covered by the existing Python CI matrix with no extra CI-workflow
wiring — the pytest lane already runs in CI via ``scripts/test-merge``.

``_HARNESSES`` is discovered by glob, not a hardcoded list: a future
``crossover_*_test.mjs`` file is picked up automatically, closing the exact
gap that left this family unexecuted in the first place.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_JS_DIR = Path(__file__).resolve().parent / "js"
_NODE = shutil.which("node")

_HARNESSES = sorted(path.name for path in _JS_DIR.glob("crossover_*_test.mjs"))
# A broken glob (wrong directory, typo'd pattern) must fail collection loudly
# — never silently parametrize zero tests and report a hollow "0 passed".
assert _HARNESSES, f"no crossover_*_test.mjs harnesses found under {_JS_DIR}"


@pytest.mark.parametrize("harness", _HARNESSES)
def test_crossover_wizard_harness(harness: str):
    if _NODE is None:
        pytest.skip("node not on PATH")
    proc = subprocess.run(
        [_NODE, str(_JS_DIR / harness)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["ok"] is True, out
    assert out["passed"] >= 1, out
