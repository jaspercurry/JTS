# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contracts for scripts/tense-grep.sh's two scopes.

#2325: the default (no-arg) scope is the merge base with origin/main, so
it can only ever falsify prose in files the CURRENT branch's own diff
touches. A cutover PR that deletes or supersedes a subsystem can leave a
now-false "nothing produces this yet" sitting in a module the diff never
opens -- the concrete instance was `jasper/active_speaker/crossover_v2/
contracts.py` still declaring three types inert after a PR that never
touched the file falsified that claim. `--all` sweeps every repo-tracked
file instead, for a manual once-per-deletion-PR baseline (AGENTS.md doc
rule 11).

The critical regression this file guards against: `--all` silently
falling back to the changed-files scoping (a copy/paste that reuses the
diff instead of `git ls-files`, or a future edit that merges the two
branches carelessly). The smoke tests below plant a match OUTSIDE the
branch diff and assert `--all` still finds it while the default scope
still misses it -- either direction would fail under that regression.

This is advisory tooling (not wired into any CI lane): the smoke tests
below are the ones that would catch the script silently losing its
scoping distinction, not a full behavioral spec of its output shape.

These tests spawn real `git` and `bash` subprocesses (one script
invocation is a handful of git/grep/cut forks, not the long nmcli/awk/sed
chains test_wifi_guardian_script.py works around) -- if this file ever
flakes with a bare "posix_spawn"/EAGAIN/EMFILE-shaped failure on a loaded
macOS box, that's the same transient fork-exhaustion class documented
there, not a real regression; re-run before investigating the script.
"""
from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "tense-grep.sh"

_GIT_ID = ["-c", "user.email=t@test", "-c", "user.name=t"]


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *_GIT_ID, *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return proc.stdout.strip()


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture
def make_repo(tmp_path: Path):
    """Factory: commit `base_files` as origin/main, then (if non-empty)
    commit `branch_files` on top as the current branch -- the shape of a
    real PR, without a real clone/remote (`update-ref` is enough to make
    `git merge-base origin/main HEAD` resolve)."""
    state = {"n": 0}

    def _build(base_files: dict[str, str], branch_files: dict[str, str]) -> Path:
        state["n"] += 1
        repo = tmp_path / f"repo{state['n']}"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        for name, content in base_files.items():
            (repo / name).write_text(content, encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")
        base_sha = _git(repo, "rev-parse", "HEAD")
        _git(repo, "update-ref", "refs/remotes/origin/main", base_sha)

        for name, content in branch_files.items():
            (repo / name).write_text(content, encoding="utf-8")
        if branch_files:
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "branch")
        return repo

    return _build


@pytest.fixture
def branch_repo(make_repo) -> Path:
    """untouched_with_match.py and clean.py land on origin/main;
    changed_with_match.py is the branch's own commit on top."""
    return make_repo(
        {
            "untouched_with_match.py": (
                "# nothing writes this file yet -- placeholder\n"
            ),
            "clean.py": "def g():\n    return 2\n",
        },
        {"changed_with_match.py": "# currently a stub\n"},
    )


def test_default_scope_misses_a_match_outside_the_branch_diff(branch_repo, tmp_path):
    """#2325's regression, in the direction --all does not cover: the
    default (no-arg) scope must stay confined to the branch's own diff,
    never picking up untouched_with_match.py from origin/main."""
    result = _run(branch_repo)

    assert result.returncode == 0, result.stderr
    assert "changed_with_match.py" in result.stdout
    assert "untouched_with_match.py" not in result.stdout

    # No commits yet -> no merge base with origin/main -> the pre-existing
    # graceful skip, not a crash.
    empty_repo = tmp_path / "empty"
    empty_repo.mkdir()
    _git(empty_repo, "init", "-q", "-b", "main")
    empty_result = _run(empty_repo)
    assert empty_result.returncode == 0, empty_result.stderr


def test_all_mode_catches_a_match_outside_the_branch_diff(branch_repo):
    """The fix, and the mutation guard: if --all silently reused the
    changed-files scoping, untouched_with_match.py -- committed to
    origin/main, outside the branch diff -- would disappear from this
    output exactly like it would from the default (no-arg) scope's."""
    result = _run(branch_repo, "--all")

    assert result.returncode == 0, result.stderr
    assert "changed_with_match.py" in result.stdout
    assert "untouched_with_match.py" in result.stdout
    assert "clean.py" not in result.stdout  # no match in it -> no group
    assert "3 repo-tracked file(s)" in result.stdout


def test_script_is_valid_bash():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True, timeout=10)
