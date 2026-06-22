# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Regression guard: self-documenting scripts must not leak their SPDX header.

Several diagnostic/lab scripts print their own leading ``#`` comment block as
``--help`` / usage text by extracting it from ``$0`` (``sed -n '2,…' "$0"``).
The 2026-06 Apache-2.0 SPDX header sweep inserted a fixed 5-line block right
after each shebang, which shifted that block down — so every such script's
usage started printing the *license header* instead of the docs. CI caught it
for ``multiroom-spike.sh`` (test_multiroom_spike_script.py); the other four had
the identical bug with no test. The fix is uniform (``sed '2,6d' "$0" | …`` —
drop the reuse header, then the original extraction); this test pins all five so
the regression cannot return when the header is touched again.

The invariant is simple and OS-independent: a script's usage/help output must
contain no ``SPDX`` text and must not be empty. (``doc-freshness.sh --help``
additionally exercises a GNU ``date`` path that errors on BSD/macOS before its
own usage prints; the no-SPDX guard still holds there, and CI runs on Linux
where the real usage is emitted — so it carries no positive-token assertion.)
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

# (script, usage-trigger args, an expected doc token or None).
# The token is a stable word from the script's real leading-comment block; None
# means "assert only no-SPDX + non-empty" (see module docstring re: doc-freshness).
_CASES = [
    ("multiroom-spike.sh", [], "multiroom-spike.sh"),
    ("pi-run-diagnostic.sh", [], "ad-hoc Pi diagnostic"),
    ("s0-sync-bench.sh", ["--help"], "s0-sync-bench.sh"),
    ("xvf-interrogate.sh", ["--help"], "XVF3800"),
    ("doc-freshness.sh", ["--help"], None),
]


@pytest.mark.parametrize("script,args,expected", _CASES, ids=[c[0] for c in _CASES])
def test_usage_excludes_spdx_header(script, args, expected):
    proc = subprocess.run(
        ["bash", str(_SCRIPTS / script), *args],
        capture_output=True, text=True, timeout=15,
    )
    out = proc.stdout + proc.stderr
    assert "SPDX" not in out, (
        f"{script} usage leaks its SPDX license header — the 'sed 2,6d' "
        f"header-strip regressed. Output:\n{out}"
    )
    assert out.strip(), f"{script} produced no usage output at all"
    if expected is not None:
        assert expected in out, (
            f"{script} usage no longer shows its real doc block "
            f"(expected {expected!r}). Output:\n{out}"
        )
