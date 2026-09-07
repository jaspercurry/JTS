# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared pieces for driving scripts/test-fast in a scratch repo and
recording what it selects, rather than letting it actually run pytest.

Used by test_build_and_ci_contracts.py and test_test_lane_tool_resolution.py
so the argv-recording stand-in, its env wiring, and the scratch-repo harness
that drives a lane run live in exactly one place.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO / "scripts"
_BASH = shutil.which("bash") or "/bin/bash"

TRUE_BIN = shutil.which("true") or "/usr/bin/true"

RECORDING_PYTEST_SOURCE = (
    "#!/usr/bin/env python3\n"
    "import json, os, sys\n"
    "with open(os.environ['PYTEST_CALLS'], 'a', encoding='utf-8') as f:\n"
    "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    "raise SystemExit(0)\n"
)


def write_recording_pytest(path: Path) -> Path:
    """Writes the argv-recording pytest stand-in to `path`, made executable."""

    path.write_text(RECORDING_PYTEST_SOURCE, encoding="utf-8")
    path.chmod(0o755)
    return path


def lane_env(pytest_path: Path, calls_path: Path) -> dict[str, str]:
    """Env for a scripts/test-fast run using the recording pytest above.

    RUFF points at the real `true` binary rather than a hand-rolled stand-in
    script -- one fewer file to write per caller, and it always exists.
    TEST_BASE is a ref that can never resolve, so the lane's own base_ref
    diff is a no-op and only the scratch repo's working-tree/untracked state
    (which callers control directly) decides what counts as "changed".
    """

    return {
        **os.environ,
        "PYTEST": str(pytest_path),
        "PYTEST_CALLS": str(calls_path),
        "RUFF": TRUE_BIN,
        "TEST_BASE": "missing-base",
    }


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def fast_lane_selected_tests(
    tmp_path: Path,
    *,
    changed_path: str,
    routed_tests: tuple[str, ...],
    test_contents: dict[str, str] | None = None,
) -> set[str]:
    """Runs scripts/test-fast in a scratch repo with `changed_path` edited.

    `routed_tests` (and `changed_path` itself) are created under the repo
    first -- empty, or with `test_contents[relative]` -- so their content can
    name whatever the caller wants selected. Returns the flat set of every
    argument ever passed to the recording pytest stand-in, across every
    phase of the lane.
    """

    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "tests").mkdir()
    for name in ("test-fast", "_test_lane.sh"):
        shutil.copy2(_SCRIPTS / name, repo / "scripts" / name)
    shutil.copy2(_SCRIPTS / "ci-classify.py", repo / "scripts" / "ci-classify.py")
    for relative in (changed_path, *routed_tests):
        path = repo / relative
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text((test_contents or {}).get(relative, ""), encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "JTS Tests")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    changed = repo / changed_path
    changed.write_text(
        changed.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8"
    )

    calls = repo / "pytest-calls.jsonl"
    recorder = write_recording_pytest(repo / "recording-pytest")

    subprocess.run(
        [_BASH, "scripts/test-fast"],
        cwd=repo,
        env=lane_env(recorder, calls),
        check=True,
        capture_output=True,
        text=True,
    )

    return {
        arg
        for line in calls.read_text(encoding="utf-8").splitlines()
        for arg in json.loads(line)
    }
