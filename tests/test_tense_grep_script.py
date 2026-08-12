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
branches carelessly). Every test below that plants a match OUTSIDE the
branch diff and asserts `--all` still finds it would fail under that
regression, because the file would vanish from `--all`'s output exactly
as it already vanishes from the default scope's.

The default scope's OWN output must stay byte-identical to how it
behaved before `--all` existed -- AGENTS.md rule 11 and every "run
scripts/tense-grep.sh before publishing a PR" callsite depend on that
not silently narrowing or widening.
test_default_scope_output_shape_is_unchanged pins the exact strings,
not just "still finds the match".

These tests spawn real `git` and `bash` subprocesses (one script
invocation is a handful of git/grep/cut forks, not the long nmcli/awk/sed
chains test_wifi_guardian_script.py works around) -- if this file ever
flakes with a bare "posix_spawn"/EAGAIN/EMFILE-shaped failure on a loaded
macOS box, that's the same transient fork-exhaustion class documented
there, not a real regression; re-run before investigating the script.
"""
from __future__ import annotations

from pathlib import Path
import re
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "tense-grep.sh"

_GIT_ID = ["-c", "user.email=t@test", "-c", "user.name=t"]

# One phrase per `|`-alternative in the script's `pattern` variable.
_ALL_PATTERN_PHRASES = (
    "not yet wired",
    "until it lands",
    "until that lands",
    "until this lands",
    "a later step",
    "will add support",
    "will land soon",
    "today this returns",
    "currently disabled",
    "no writer for this",
    "nothing writes here",
    "nothing refuses this",
)

_GROUP_HEADER_RE = re.compile(r"^(\S.*) \((\d+)\)$")


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


def _group_body(all_mode_stdout: str, filename: str) -> list[str] | None:
    """The indented lines under `filename`'s `--all`-mode group header,
    or None if that file has no group (no matches)."""
    lines = all_mode_stdout.splitlines()
    for i, line in enumerate(lines):
        header = _GROUP_HEADER_RE.match(line)
        if header and header.group(1) == filename:
            body = []
            for follow in lines[i + 1 :]:
                if follow.startswith("  "):
                    body.append(follow[2:])
                else:
                    break
            return body
    return None


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


def test_default_scope_misses_a_match_outside_the_branch_diff(branch_repo):
    result = _run(branch_repo)

    assert result.returncode == 0, result.stderr
    assert "changed_with_match.py" in result.stdout
    assert "untouched_with_match.py" not in result.stdout


def test_default_scope_output_shape_is_unchanged(branch_repo):
    """Pins the exact strings a no-arg run produced before --all
    existed, not just "the right file is mentioned somewhere"."""
    result = _run(branch_repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "tense-grep: advisory sweep of 1 changed file(s) for roadmap-dated phrasing.\n"
        "Comments and docs must state what IS — every match below is a hypothesis that the\n"
        "claim went stale once a later PR landed. Read each one; most will be fine.\n"
        "\n"
        "changed_with_match.py:1:# currently a stub\n"
    )
    assert result.stderr == ""


def test_all_mode_catches_a_match_outside_the_branch_diff(branch_repo):
    """The fix, and the mutation guard: if --all silently reused the
    changed-files scoping, untouched_with_match.py would disappear from
    this output exactly like it disappears from the default-mode test
    above."""
    result = _run(branch_repo, "--all")

    assert result.returncode == 0, result.stderr
    assert "changed_with_match.py" in result.stdout
    assert "untouched_with_match.py" in result.stdout
    assert "clean.py" not in result.stdout  # no match in it -> no group
    assert "3 repo-tracked file(s)" in result.stdout


def test_all_mode_groups_by_file_with_a_per_file_count(make_repo):
    """Deliberately gives changed.py TWO matches and untouched.py ONE, so
    the header counts differ from each other. A one-match-per-file
    fixture can't distinguish a genuinely computed count from a count
    hardcoded to any single constant (gate finding: printing a literal
    `999`, or pinning every count to `1`, flipped no existing test --
    every prior fixture happened to have exactly one match per file)."""
    repo = make_repo(
        {
            "untouched_with_match.py": (
                "# nothing writes this file yet -- placeholder\n"
            ),
            "clean.py": "def g():\n    return 2\n",
        },
        {"changed_with_match.py": "# currently a stub\n# not yet finished\n"},
    )

    result = _run(repo, "--all")

    assert _group_body(result.stdout, "changed_with_match.py") == [
        "1:# currently a stub",
        "2:# not yet finished",
    ]
    assert _group_body(result.stdout, "untouched_with_match.py") == [
        "1:# nothing writes this file yet -- placeholder"
    ]
    # The exact header strings, not just the parsed body length: a count
    # that's off by one in the printf format (or hardcoded) can still
    # leave `_group_body` parsing the right NUMBER of indented lines
    # while the header text itself is wrong.
    assert "changed_with_match.py (2)" in result.stdout
    assert "untouched_with_match.py (1)" in result.stdout
    assert "Total: 3 match(es) in 2 of 3 repo-tracked file(s)." in result.stdout


def test_all_mode_no_matches(make_repo):
    repo = make_repo({"clean.py": "def g():\n    return 2\n"}, {})

    result = _run(repo, "--all")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "tense-grep --all: no matches in 1 repo-tracked file(s).\n"


def test_all_mode_zero_tracked_files(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")

    result = _run(repo, "--all")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "tense-grep --all: 0 repo-tracked file(s) in scope.\n"


def test_default_scope_with_no_commits_still_exits_zero(tmp_path):
    """No HEAD yet -> no merge base -> the pre-existing graceful skip,
    unaffected by --all's existence."""
    repo = tmp_path / "empty"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")

    result = _run(repo)

    assert result.returncode == 0, result.stderr
    assert "no merge base with origin/main found" in result.stderr


def test_unknown_argument_exits_nonzero_without_scanning(branch_repo):
    result = _run(branch_repo, "--bogus")

    assert result.returncode == 2
    assert result.stdout == ""
    assert "unknown argument: --bogus" in result.stderr


@pytest.mark.parametrize("mode_args", ((), ("--all",)))
def test_normal_runs_always_exit_zero_even_with_matches(branch_repo, mode_args):
    """The advisory contract: a match is a hypothesis, never a failure."""
    result = _run(branch_repo, *mode_args)

    assert result.returncode == 0, result.stderr


def test_all_mode_finds_every_pattern_alternative_default_mode_finds(make_repo):
    """Functional pattern-parity check, not just a structural read of the
    script: one phrase per `|`-alternative, in a file that's part of the
    branch diff (so default mode sees it) and duplicated verbatim as a
    file origin/main already has (so --all is exercised on its own,
    never-diffed copy). Both scopes must flag the identical phrase set --
    proving neither scope quietly uses a narrower or wider pattern than
    the other."""
    content = "\n".join(f"# {phrase}" for phrase in _ALL_PATTERN_PHRASES) + "\n"
    repo = make_repo({"untouched.py": content}, {"changed.py": content})

    default_result = _run(repo)
    all_result = _run(repo, "--all")

    assert default_result.returncode == 0, default_result.stderr
    assert all_result.returncode == 0, all_result.stderr

    default_changed_lines = [
        line.split(":", 2)[2]
        for line in default_result.stdout.splitlines()
        if line.startswith("changed.py:")
    ]
    all_changed_lines = [
        line.split(":", 1)[1] for line in _group_body(all_result.stdout, "changed.py")
    ]
    all_untouched_lines = [
        line.split(":", 1)[1]
        for line in _group_body(all_result.stdout, "untouched.py")
    ]

    assert len(default_changed_lines) == len(_ALL_PATTERN_PHRASES)
    assert default_changed_lines == all_changed_lines == all_untouched_lines


def test_pattern_is_defined_exactly_once_for_both_scopes():
    """Structural guard alongside the functional parity test above: the
    script must share ONE `pattern` variable between scopes, never fork
    it into two literals that could drift apart under a future edit."""
    source = SCRIPT.read_text(encoding="utf-8")

    # `\s*` so an indented fork (e.g. a copy pasted inside the `--all`
    # branch, which is itself indented) still counts as a second
    # definition -- an unindented-only anchor would miss exactly that
    # shape, since the `--all` branch is the one place a fork would
    # plausibly be added.
    assert len(re.findall(r"^\s*pattern='", source, flags=re.MULTILINE)) == 1
    assert source.count("all_mode") >= 2  # parsed once, tested at least once


def test_script_is_valid_bash():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True, timeout=10)
