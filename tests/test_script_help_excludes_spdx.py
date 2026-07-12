# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Regression guard for self-documenting scripts' complete usage blocks.

Several diagnostic/lab scripts print their own leading ``#`` comment block as
``--help`` / usage text by extracting it from ``$0`` (``sed -n '2,…' "$0"``).
The 2026-06 Apache-2.0 SPDX header sweep exposed two brittle extraction
strategies: fixed line ranges drifted, while a decorative ``# ===`` delimiter
truncated safety-critical help at the title box. The scripts now extract the
contiguous leading documentation block after SPDX and stop before code. Deep
tokens pin each block's footer, not merely its surviving title.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

# (script, usage-trigger args, expected return code, deep documentation tokens).
_CASES = [
    (
        "multiroom-spike.sh",
        ["--help"],
        0,
        ("HEARING-SAFETY / CONTENTION", "--teardown", "Follower/sub have no default"),
    ),
    (
        "multiroom-spike.sh",
        [],
        2,
        ("HEARING-SAFETY / CONTENTION", "--teardown", "Follower/sub have no default"),
    ),
    (
        "pi-run-diagnostic.sh",
        [],
        2,
        ("ad-hoc Pi diagnostic", "JTS_DIAG_WORKDIR=/home/pi/jts"),
    ),
    (
        "s0-sync-bench.sh",
        ["--help"],
        0,
        (
            "HEARING-SAFETY / CONTENTION",
            "--teardown",
            "--resampler none|synchronous|async",
        ),
    ),
    (
        "s0-sync-bench.sh",
        [],
        2,
        (
            "HEARING-SAFETY / CONTENTION",
            "--teardown",
            "--resampler none|synchronous|async",
        ),
    ),
    (
        "xvf-interrogate.sh",
        ["--help"],
        0,
        ("XVF3800", "10. XVF firmware artifacts", "No deploy required — pure SSH"),
    ),
    (
        "doc-freshness.sh",
        ["--help"],
        0,
        ("Last verified: YYYY-MM-DD", 'Source  "footer"', "Doc     repo-relative path"),
    ),
    (
        "doc-freshness.sh",
        ["-h"],
        0,
        ("Last verified: YYYY-MM-DD", 'Source  "footer"', "Doc     repo-relative path"),
    ),
]


@pytest.mark.parametrize(
    "script,args,expected_rc,expected_tokens",
    _CASES,
    ids=[f"{case[0]}-{'no-action' if not case[1] else case[1][0]}" for case in _CASES],
)
def test_usage_is_complete_and_excludes_spdx_header(
    script,
    args,
    expected_rc,
    expected_tokens,
):
    proc = subprocess.run(
        ["bash", str(_SCRIPTS / script), *args],
        capture_output=True,
        text=True,
        timeout=15,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == expected_rc, out
    assert "SPDX" not in out, (
        f"{script} usage leaks its SPDX license header. Output:\n{out}"
    )
    assert out.strip(), f"{script} produced no usage output at all"
    assert "set -euo pipefail" not in out, f"{script} usage leaks code:\n{out}"
    assert "date: illegal option" not in out, f"{script} parsed help as a date:\n{out}"
    assert "invalid date" not in out, f"{script} parsed help as a date:\n{out}"
    for expected in expected_tokens:
        assert expected in out, (
            f"{script} usage no longer shows its real doc block "
            f"(expected {expected!r}). Output:\n{out}"
        )


def test_doc_freshness_normal_threshold_and_all_mode_still_work():
    proc = subprocess.run(
        ["bash", str(_SCRIPTS / "doc-freshness.sh"), "90", "--all"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "Summary:" in out
    assert "threshold 90 days" in out
