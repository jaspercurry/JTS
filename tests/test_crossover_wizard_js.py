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

Bridging them through pytest keeps them covered by the existing Python CI
matrix with no extra CI-workflow wiring — the pytest lane already runs in
CI via ``scripts/test-merge``.

``_HARNESSES`` is discovered by glob, not a hardcoded list: a future
``crossover_*_test.mjs`` file is picked up automatically, closing the exact
gap that left this family unexecuted in the first place.

**This bridge IS the CI coverage — verified, not assumed** (two-stage PR-T2
review, 2026-07-29). The workflow's ``js`` job still runs seven explicit
``node`` invocations and none of them is a crossover harness; what executes
this family is the pytest matrix (py3.11/3.12/3.13), each leg running
``scripts/test-merge``, which ignores only ``tests/voice_eval``. ``node`` is on
``ubuntu-latest`` by default — the ``js`` job calls it with no
``actions/setup-node`` step and passes — so :data:`_NODE` resolves there and
these run rather than skip. Adding the family to the ``js`` job as well would
duplicate that on a second, hand-maintained list: exactly the drift the glob
above exists to prevent. The one hole that WAS real is closed below — a missing
``node`` in CI now fails instead of skipping quietly.
"""
from __future__ import annotations

import json
import os
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


def test_canonical_crossover_page_normalizes_server_action_urls() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "deploy/assets/correction/js/crossover/main.js"
    ).read_text()

    assert "function publicCrossoverUrl(value)" in source
    assert "pathname.indexOf('/sound/crossover/') === 0" in source
    assert "publicCrossoverUrl(action.endpoint)" in source
    assert "href: publicCrossoverUrl(action.href)" in source
    assert "publicCrossoverUrl('/correction/crossover/envelope')" in source
    assert "publicCrossoverUrl('/correction/crossover/capture-cancel')" in source
    assert "publicCrossoverUrl('/correction/crossover/reset')" in source


@pytest.mark.parametrize("harness", _HARNESSES)
def test_crossover_wizard_harness(harness: str):
    if _NODE is None:
        # A developer without node gets a skip; CI does not. This lane is the
        # ONLY place these harnesses execute (the workflow's `js` job runs an
        # explicit list that has never included this family — see the module
        # docstring), so "node disappeared from the runner" would silently turn
        # the whole family from covered into uncovered, reported as a green
        # build. Fail loudly there instead: an unexecuted test cannot catch the
        # regression it was written for, and a SKIPPED one at least has to be
        # visible to count as a decision.
        if os.environ.get("CI"):
            pytest.fail(
                "node is not on PATH in CI — the crossover wizard's JS "
                "harnesses are the only automated execution of "
                "deploy/assets/correction/js/crossover/*, and skipping them "
                "here silently drops that coverage"
            )
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
