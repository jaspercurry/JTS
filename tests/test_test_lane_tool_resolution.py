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

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO / "scripts"

# Externals the lanes invoke before (and during) tool resolution. `git` for the
# `cd`, `dirname` for the `source`, `cat` for the FATAL heredoc.
_SANDBOX_TOOLS = ("git", "dirname", "cat")

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


def _run(repo: Path, env: dict[str, str], lane: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_BASH, f"scripts/{lane}"],
        cwd=repo,
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
    result = _run(repo, {**env, "PYTEST": shutil.which("cat") or "/bin/cat"}, "test-fast")

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
    """
    repo, env = lane_sandbox
    stand_in = shutil.which("true") or "/usr/bin/true"
    result = _run(
        repo, {**env, "PYTEST": stand_in, "RUFF": stand_in}, lane
    )

    assert f"==> pytest: {stand_in}" in result.stderr
    assert "override" in result.stderr
    assert stand_in not in result.stdout


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
