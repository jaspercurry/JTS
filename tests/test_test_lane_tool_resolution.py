# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Guards the executable test lanes' interpreter resolution (issue #1836).

``scripts/test-fast`` and ``scripts/test-merge`` resolve pytest (and ruff) from
``$PYTEST``/``$RUFF``, then ``./.venv/bin/``, then ``$PATH``. Both lanes ``cd``
to ``git rev-parse --show-toplevel``, which in an agent worktree is the
*worktree* root -- a directory with no ``.venv`` of its own. The ``$PATH``
fallback is therefore the common case in exactly the environment the
orchestration pattern runs implementers in, and before this guard neither lane
said which interpreter it had picked. A transcript in which nothing ran was
indistinguishable from a passing one.

These tests reproduce that environment rather than simulating it: a scratch git
repo (so the lanes' ``cd`` lands somewhere with no ``.venv``) plus a sandboxed
``PATH`` holding only the few utilities the lanes reach before resolution. The
promises pinned are the ones the fix makes: refuse with a *named* error naming
the tool, say plainly that nothing ran, exit nonzero, and on success announce
the resolved interpreter on stderr without polluting stdout.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO / "scripts"

# Externals the lanes invoke before (and during) tool resolution: `git` for the
# `cd`, `dirname` for the sibling-resolver `source`, and `python3` for
# test-fast's checked-in routing-policy target registry. The FATAL block itself
# needs nothing -- it is printed with the `printf` builtin precisely so a
# mangled $PATH cannot swallow it (see test_fatal_block_survives_an_empty_path).
_SANDBOX_TOOLS = ("git", "dirname", "python3")

_LANES = ("test-fast", "test-merge")

# Invoked by absolute path: the sandbox PATH deliberately excludes bash itself.
_BASH = shutil.which("bash") or "/bin/bash"


@pytest.fixture
def lane_sandbox(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A scratch git repo with the lanes copied in, and a pytest-free PATH."""
    repo = tmp_path / "worktree"
    (repo / "scripts").mkdir(parents=True)
    for name in (*_LANES, "_test_lane.sh"):
        shutil.copy2(_SCRIPTS / name, repo / "scripts" / name)
    shutil.copy2(_SCRIPTS / "ci-classify.py", repo / "scripts" / "ci-classify.py")
    subprocess.run(
        ["git", "init", "-q"], cwd=repo, check=True, capture_output=True
    )

    # A PATH containing only the utilities above guarantees the "no pytest
    # anywhere" branch is exercised on a developer machine that happens to
    # have a global pytest installed.
    sandbox_bin = tmp_path / "bin"
    sandbox_bin.mkdir()
    for tool in _SANDBOX_TOOLS:
        resolved = shutil.which(tool)
        assert resolved, f"sandbox needs {tool} on the host PATH"
        (sandbox_bin / tool).symlink_to(resolved)

    env = {
        "PATH": str(sandbox_bin),
        "HOME": str(tmp_path),
        # Keep git from reading the developer's config into the scratch repo.
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    return repo, env


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _run(
    repo: Path,
    env: dict[str, str],
    lane: str,
    *,
    cwd: Path | None = None,
    argv0: str | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_BASH, argv0 or f"scripts/{lane}"],
        cwd=cwd or repo,
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("lane", _LANES)
def test_lane_refuses_loudly_when_pytest_is_unresolvable(
    lane: str, lane_sandbox: tuple[Path, dict[str, str]]
) -> None:
    """The false-green case: no .venv, no pytest on PATH."""
    repo, env = lane_sandbox
    result = _run(repo, env, lane)

    assert result.returncode != 0, (
        f"{lane} exited 0 with no resolvable pytest -- this is the #1836 "
        f"false-green.\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert "FATAL" in combined
    assert "pytest" in combined
    # The operator-facing promise: an unmissable statement that nothing ran.
    assert "NO TESTS WERE RUN" in combined
    assert "issue #1836" in combined


def test_test_fast_also_refuses_on_a_missing_ruff(
    lane_sandbox: tuple[Path, dict[str, str]],
) -> None:
    """test-fast resolves two tools; the second must be guarded like the first.

    Pointing ``$PYTEST`` at a real executable clears the first gate so the
    failure attributable to ``ruff`` is the one observed.
    """
    repo, env = lane_sandbox
    result = _run(
        repo, {**env, "PYTEST": shutil.which("true") or "/usr/bin/true"}, "test-fast"
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "FATAL" in combined
    assert "ruff" in combined


@pytest.mark.parametrize("lane", _LANES)
def test_lane_announces_the_resolved_interpreter_on_stderr(
    lane: str, lane_sandbox: tuple[Path, dict[str, str]]
) -> None:
    """Provenance is announced, and stdout stays pure test output.

    ``true`` stands in for pytest: it accepts and ignores the lanes' flags and
    exits 0, so the announcement is observed without running a suite.

    The assertion is on POSITION, not membership. A membership check is
    vacuous for ``test-merge``: were the announcement misdirected to stdout,
    the lane's ``exec`` would fail on a path that has the announcement text
    glued to the front of it, and bash's own error echoes that path back on
    stderr -- so ``"==> pytest: ..." in result.stderr`` still holds while
    nothing is announced. Pinning it as the first stderr line kills that.
    """
    repo, env = lane_sandbox
    stand_in = shutil.which("true") or "/usr/bin/true"
    result = _run(
        repo, {**env, "PYTEST": stand_in, "RUFF": stand_in}, lane
    )

    assert result.stderr.splitlines()[:1] == [
        f"==> pytest: {stand_in} ($PYTEST override)"
    ], result.stderr
    assert stand_in not in result.stdout


@pytest.mark.parametrize("lane", _LANES)
def test_lane_works_when_invoked_by_a_relative_path_from_a_subdirectory(
    lane: str, lane_sandbox: tuple[Path, dict[str, str]]
) -> None:
    """The lanes are cwd-independent, and that must survive the `source`.

    ``${BASH_SOURCE[0]}`` is caller-relative, so resolving the sibling resolver
    *after* the ``cd`` to the repo root re-anchors ``../scripts`` against the
    new cwd: ``cd tests && bash ../scripts/test-merge`` died with
    ``../scripts/_test_lane.sh: No such file or directory`` -- and, because the
    helper never loaded, with none of the FATAL wording that exists to make a
    non-run unmissable. The lane dir is therefore captured before the ``cd``.
    """
    repo, env = lane_sandbox
    (repo / "tests").mkdir()
    stand_in = shutil.which("true") or "/usr/bin/true"
    result = _run(
        repo,
        {**env, "PYTEST": stand_in, "RUFF": stand_in},
        lane,
        cwd=repo / "tests",
        argv0=f"../scripts/{lane}",
    )

    assert "_test_lane.sh: No such file or directory" not in result.stderr
    assert result.stderr.splitlines()[:1] == [
        f"==> pytest: {stand_in} ($PYTEST override)"
    ], result.stderr


@pytest.mark.parametrize("lane", _LANES)
def test_fatal_names_the_rejected_override_and_no_unsearched_path(
    lane: str, lane_sandbox: tuple[Path, dict[str, str]]
) -> None:
    """An override short-circuits the search; the message must say so.

    With ``$PYTEST`` set, ``./.venv`` and ``$PATH`` are never consulted --
    listing them describes a search that did not happen, and it buries the one
    fact that fixes the problem: the value that was rejected.
    """
    repo, env = lane_sandbox
    typo = str(repo / "no" / "such" / "pytest")
    result = _run(repo, {**env, "PYTEST": typo}, lane)

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert f"$PYTEST={typo}" in combined
    assert "./.venv/bin/pytest" not in combined
    assert "on $PATH" not in combined


def test_fatal_block_survives_an_empty_path(tmp_path: Path) -> None:
    """A mangled $PATH is the likeliest cause -- the message must outlive it.

    The block was a ``cat`` heredoc, so it failed with ``cat: command not
    found`` in exactly the case it is written for. ``printf`` is a bash
    builtin and needs no $PATH at all. Sourced directly here (rather than
    through a lane) because a lane's own ``cd`` needs ``git``.
    """
    shutil.copy2(_SCRIPTS / "_test_lane.sh", tmp_path / "_test_lane.sh")
    result = subprocess.run(
        [_BASH, "-c", "source ./_test_lane.sh; resolve_lane_tool test-fast pytest PYTEST"],
        cwd=tmp_path,
        env={"PATH": ""},
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "command not found" not in result.stderr
    assert "FATAL" in result.stderr
    assert "NO TESTS WERE RUN" in result.stderr


def test_fatal_headline_survives_tail_truncation(
    lane_sandbox: tuple[Path, dict[str, str]],
) -> None:
    """Operators pipe lanes through `| tail -N`; the last line must still warn.

    Under `tail -3` the opening sentence is gone, so the block bookends itself
    with the headline rather than trailing off into remediation prose.
    """
    repo, env = lane_sandbox
    last_line = _run(repo, env, "test-merge").stderr.rstrip("\n").splitlines()[-1]

    assert "NO TESTS WERE RUN" in last_line
    assert "pytest" in last_line
    assert "issue #1836" in last_line


def _fast_lane_selected_tests(
    tmp_path: Path,
    *,
    changed_path: str,
    routed_tests: tuple[str, ...],
) -> set[str]:
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
            path.write_text("", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "JTS Tests")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    changed = repo / changed_path
    changed.write_text(
        changed.read_text(encoding="utf-8") + "\n# edited\n",
        encoding="utf-8",
    )

    calls = repo / "pytest-calls.jsonl"
    recorder = repo / "recording-pytest"
    recorder.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['PYTEST_CALLS'], 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "raise SystemExit(5 if '--last-failed' in sys.argv else 0)\n",
        encoding="utf-8",
    )
    recorder.chmod(0o755)
    stand_in = shutil.which("true") or "/usr/bin/true"

    subprocess.run(
        [_BASH, "scripts/test-fast"],
        cwd=repo,
        env={
            **os.environ,
            "PYTEST": str(recorder),
            "PYTEST_CALLS": str(calls),
            "RUFF": stand_in,
            "TEST_BASE": "missing-base",
        },
        check=True,
        capture_output=True,
        text=True,
    )

    return {
        arg
        for line in calls.read_text(encoding="utf-8").splitlines()
        for arg in json.loads(line)
    }


@pytest.mark.parametrize(
    ("changed_path", "routed_tests"),
    [
        (
            "scripts/_test_lane.sh",
            (
                "tests/test_test_lane_tool_resolution.py",
                "tests/test_dependency_groups.py",
            ),
        ),
        (
            "tests/wake_feature_bank_fixtures.py",
            (
                "tests/test_build_wake_feature_bank.py",
                "tests/test_build_wake_negative_feature_bank.py",
                "tests/test_wake_training_feature_bank.py",
            ),
        ),
    ],
    ids=("lane-resolver", "wake-feature-bank-fixtures"),
)
def test_fast_lane_routes_internal_support_files_to_their_guards(
    tmp_path: Path,
    changed_path: str,
    routed_tests: tuple[str, ...],
) -> None:
    """Support-file-only edits must select their dependent test contracts.

    Driven through the lane with a recording stand-in for pytest rather than
    asserting on the script's text: a string check would still pass if the
    mapping were unreachable or pointed at paths that do not exist.
    Everything is committed first so ``changed_path`` is the only edit.
    """

    selected = _fast_lane_selected_tests(
        tmp_path,
        changed_path=changed_path,
        routed_tests=routed_tests,
    )

    assert set(routed_tests) <= selected, selected


def test_fast_lane_propagates_routing_policy_failure_before_later_work(
    tmp_path: Path,
) -> None:
    """The cheap policy gate must stop the lane before lint or broad tests."""

    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    for name in ("test-fast", "_test_lane.sh", "ci-classify.py"):
        shutil.copy2(_SCRIPTS / name, repo / "scripts" / name)
    _git(repo, "init", "-q")

    calls = repo / "pytest-calls.jsonl"
    recorder = repo / "recording-pytest"
    recorder.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['PYTEST_CALLS'], 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "raise SystemExit(23)\n",
        encoding="utf-8",
    )
    recorder.chmod(0o755)

    result = subprocess.run(
        [_BASH, "scripts/test-fast", "--collect-only", "-k", "requested_test"],
        cwd=repo,
        env={
            **os.environ,
            "PYTEST": str(recorder),
            "PYTEST_CALLS": str(calls),
            "RUFF": str(repo / "missing-ruff"),
            "TEST_BASE": "missing-base",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 23, result
    assert "==> pytest CI routing policy" in result.stdout
    assert "==> ruff" not in result.stdout
    [call] = [
        json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()
    ]
    assert call == ["-q", "--tb=short", "tests/test_ci_classifier.py"]


@pytest.mark.parametrize("lane", _LANES)
def test_lane_sources_the_shared_resolver_rather_than_reimplementing_it(
    lane: str,
) -> None:
    """One resolver, two lanes -- the drift guard.

    Both lanes previously carried their own copy of the fallback chain; the
    bare-``pytest`` fallback that produced #1836 existed twice. A third lane
    (or a well-meaning edit) must not reintroduce a private copy.
    """
    body = (_SCRIPTS / lane).read_text()
    assert "_test_lane.sh" in body
    assert "resolve_lane_tool" in body
    assert 'pytest_bin="pytest"' not in body
    assert 'ruff_bin="ruff"' not in body
