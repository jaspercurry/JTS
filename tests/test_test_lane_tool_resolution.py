# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Guards the executable test lanes' interpreter resolution (issue #1836).

``scripts/test-fast`` and ``scripts/test-merge`` resolve pytest (test-fast
also ruff; test-merge also mypy) from ``$PYTEST``/``$RUFF``/``$MYPY``, then
``./.venv/bin/``, then ``$PATH``. Both lanes ``cd`` to ``git rev-parse
--show-toplevel``, which in an agent worktree is the *worktree* root -- a
directory with no ``.venv`` of its own. The ``$PATH`` fallback is therefore
the common case in exactly the environment the orchestration pattern runs
implementers in, and before this guard neither lane said which interpreter it
had picked. A transcript in which nothing ran was indistinguishable from a
passing one.

These tests reproduce that environment rather than simulating it: a scratch git
repo (so the lanes' ``cd`` lands somewhere with no ``.venv``) plus a sandboxed
``PATH`` holding only the few utilities the lanes reach before resolution. The
promises pinned are the ones the fix makes: refuse with a *named* error naming
the tool, say plainly that nothing ran, exit nonzero, and on success announce
the resolved interpreter on stderr without polluting stdout.

Three related contracts share this file rather than growing their own:

* **issue #1850** -- a caller piping a lane through ``2>&1 | tail -N`` sees
  exit 0 from a failed lane whenever the shell lacks ``set -o pipefail``, so
  each lane also prints an ``==> <lane>: N passed`` / ``==> <lane>: FAILED``
  verdict sentinel as the actual last line of stdout, via an EXIT trap that
  fires on every exit path -- including the FATAL block above, which this
  file already guards separately.
* **the sentinel must not lie about a run that never finished** -- observed
  2026-08-15: a ``scripts/test-merge`` SIGTERM'd at ~23% printed ``==>
  test-merge: 0 passed``, the SUCCESS shape, because bash ran the EXIT trap
  with a STALE ZERO in ``$?`` rather than 143. The lane's process status was
  honest; only the text lied, and "0 passed" reads as "nothing to run". A
  ``status >= 128`` check alone cannot catch that, so the success shape is
  gated on a positive "the lane reached its end" marker plus a parsed pytest
  summary. What bash hands the trap is per-signal, not one rule;
  ``lane_emit_verdict`` in ``scripts/_test_lane.sh`` owns that table.
* **issue #1758** -- ``--last-failed --last-failed-no-failures none`` only
  guards an EMPTY cache; a cached node id that no longer resolves (a renamed
  or deleted test) makes pytest's own machinery silently fall back to
  collecting and running everything in scope. ``test-fast`` validates cached
  ids itself before ever handing them to pytest.
* **issue #1910** -- no local lane ran mypy at all, so a new unbaselined
  package's type errors surfaced only at CI's py3.13 gate. ``test-merge`` now
  resolves mypy the same way as pytest (a named FATAL, never a silent skip)
  and runs it as a step before pytest, composing with the #1850 sentinel so a
  mypy failure still ends the lane with ``==> test-merge: FAILED``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO / "scripts"

# Externals the lanes invoke before, during, and after tool resolution: `git`
# for the `cd`, `dirname` for the sibling-resolver `source`, `python3` for
# test-fast's checked-in routing-policy target registry, and the coreutils a
# FULL run to completion needs (temp files, the changed-file-selection
# pipeline, the last-failed cache read/prune). The FATAL block itself needs
# nothing -- it is printed with the `printf` builtin precisely so a mangled
# $PATH cannot swallow it (see test_fatal_block_survives_an_empty_path).
_SANDBOX_TOOLS = (
    "git", "dirname", "python3", "mktemp", "rm", "awk", "sort", "sed", "find", "tee",
    "grep", "tail",
)

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

    ``true`` stands in for ruff/mypy: it accepts and ignores the lanes' flags
    and exits 0, so those gates clear without doing anything. ``pytest`` is a
    RECORDING stand-in rather than ``true`` and its call log is asserted on
    below -- issue #1910's gate review caught that, with only a `true`
    stand-in and no ``$MYPY`` override, this test (and its sibling below) had
    silently degraded into a resolution-only check for ``test-merge``: the
    lane died at the mypy FATAL immediately after announcing pytest, pytest
    itself never ran, and neither original assertion here noticed because
    neither checked that it had. Recording the call closes that gap.

    The stderr assertion is on POSITION, not membership. A membership check
    is vacuous for ``test-merge``: were the announcement misdirected to
    stdout, the lane's ``exec`` would fail on a path that has the
    announcement text glued to the front of it, and bash's own error echoes
    that path back on stderr -- so ``"==> pytest: ..." in result.stderr``
    still holds while nothing is announced. Pinning it as the first stderr
    line kills that.
    """
    repo, env = lane_sandbox
    other_tool_stand_in = shutil.which("true") or "/usr/bin/true"
    calls = repo / "calls.jsonl"
    pytest_stub = _recording_stub(repo, "pytest", calls)
    result = _run(
        repo,
        {
            **env,
            "PYTEST": str(pytest_stub),
            "RUFF": other_tool_stand_in,
            "MYPY": other_tool_stand_in,
        },
        lane,
    )

    assert result.stderr.splitlines()[:1] == [
        f"==> pytest: {pytest_stub} ($PYTEST override)"
    ], result.stderr
    assert str(pytest_stub) not in result.stdout
    assert result.returncode == 0, result
    assert "pytest" in calls.read_text(encoding="utf-8").splitlines(), (
        "the pytest stand-in must actually run, not just resolve"
    )


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

    ``pytest`` is a RECORDING stand-in for the same #1910 gate-review reason
    as its sibling test above: without a working ``$MYPY``, ``test-merge``
    was silently dying at the mypy FATAL right after the stderr
    announcement, and neither original assertion here noticed that pytest
    never actually ran.
    """
    repo, env = lane_sandbox
    (repo / "tests").mkdir()
    other_tool_stand_in = shutil.which("true") or "/usr/bin/true"
    calls = repo / "calls.jsonl"
    pytest_stub = _recording_stub(repo, "pytest", calls)
    result = _run(
        repo,
        {
            **env,
            "PYTEST": str(pytest_stub),
            "RUFF": other_tool_stand_in,
            "MYPY": other_tool_stand_in,
        },
        lane,
        cwd=repo / "tests",
        argv0=f"../scripts/{lane}",
    )

    assert "_test_lane.sh: No such file or directory" not in result.stderr
    assert result.stderr.splitlines()[:1] == [
        f"==> pytest: {pytest_stub} ($PYTEST override)"
    ], result.stderr
    assert result.returncode == 0, result
    assert "pytest" in calls.read_text(encoding="utf-8").splitlines(), (
        "the pytest stand-in must actually run, not just resolve"
    )


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
    test_contents: dict[str, str] | None = None,
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
            path.write_text(
                (test_contents or {}).get(relative, ""), encoding="utf-8"
            )
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
    ("changed_path", "routed_tests", "test_contents"),
    [
        (
            "scripts/_test_lane.sh",
            (
                "tests/test_test_lane_tool_resolution.py",
                "tests/test_dependency_groups.py",
            ),
            None,
        ),
        (
            # The tests/*_fixtures.py arm routes by grepping each routed
            # test for the changed module's own basename (same idiom as the
            # deploy/bin arm's grep fallback below), so the stand-in test
            # files need that basename in them, same as a real importer.
            "tests/wake_feature_bank_fixtures.py",
            (
                "tests/test_build_wake_feature_bank.py",
                "tests/test_build_wake_negative_feature_bank.py",
                "tests/test_wake_training_feature_bank.py",
            ),
            {
                "tests/test_build_wake_feature_bank.py": (
                    "from tests.wake_feature_bank_fixtures import x\n"
                ),
                "tests/test_build_wake_negative_feature_bank.py": (
                    "from tests.wake_feature_bank_fixtures import x\n"
                ),
                "tests/test_wake_training_feature_bank.py": (
                    "from tests.wake_feature_bank_fixtures import x\n"
                ),
            },
        ),
        (
            # issue #3142: a module nested one directory deeper than the
            # package selects only the package/module arms and would miss
            # the doctor family's tests/test_doctor_<module>.py convention.
            # test_doctor_env.py is an unrelated sibling pulled in only by
            # the family-wide glob, pinning that the fix covers the whole
            # jasper/cli/doctor/ package, not just this one file.
            "jasper/cli/doctor/audio_runtime_camilla.py",
            (
                "tests/test_audio_runtime_camilla.py",
                "tests/test_cli.py",
                "tests/test_doctor_audio_runtime_camilla.py",
                "tests/test_doctor_env.py",
            ),
            None,
        ),
    ],
    ids=(
        "lane-resolver",
        "wake-feature-bank-fixtures",
        "doctor-nested-module",
    ),
)
def test_fast_lane_routes_internal_support_files_to_their_guards(
    tmp_path: Path,
    changed_path: str,
    routed_tests: tuple[str, ...],
    test_contents: dict[str, str] | None,
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
        test_contents=test_contents,
    )

    assert set(routed_tests) <= selected, selected


def _load_ci_classifier():
    """Same load-a-hyphenated-script technique as tests/test_ci_classifier.py
    (a fresh module object under its own name, not a package import) -- so
    the landing registry below is read from the one real source rather than
    copied into this file as a second literal that could drift from it."""
    spec = importlib.util.spec_from_file_location(
        "ci_classifier_for_lane_test", _SCRIPTS / "ci-classify.py"
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ci_classifier = _load_ci_classifier()


def test_fast_lane_routes_deploy_index_html_to_the_landing_bundle(
    tmp_path: Path,
) -> None:
    """issue #1973: editing deploy/index.html must select the SAME
    registered landing-page bundle scripts/ci-classify.py's fast-landing CI
    lane runs -- not a hand-copied list in test-fast, so the two cannot
    drift apart. This reproduces the original bug at HEAD on origin/main
    (before the #1973 fix): deploy/index.html gave a green test-fast while
    tests/test_landing_page_html.py sat red at the same tree, caught only by
    running it explicitly. Verified failing on origin/main, passing here.

    Wrinkle: LANDING_PYTEST_TARGETS' one function-scoped entry is a
    `file::test_name` pytest node id. _fast_lane_selected_tests's
    changed_path/routed_tests plumbing creates each `routed_tests` entry AS
    A FILE so test-fast's `[[ -f ... ]]` existence check can see it -- the
    literal string with `::` in it is never a real path, so this stubs the
    id's bare FILE half (alongside the other registered files) and asserts
    the fully-qualified id separately against what got queued for pytest.
    """
    landing_test_files: tuple[str, ...] = _ci_classifier.LANDING_TEST_FILES
    landing_targets: tuple[str, ...] = _ci_classifier.LANDING_PYTEST_TARGETS
    qualified_targets = tuple(target for target in landing_targets if "::" in target)
    bare_targets = tuple(target for target in landing_targets if "::" not in target)
    # Sanity on the registry shape this test assumes, so a future third kind
    # of entry (or the qualified one disappearing) fails here, not silently.
    assert bare_targets == landing_test_files
    assert len(qualified_targets) == 1, qualified_targets

    stub_files = landing_test_files + tuple(
        target.split("::", 1)[0] for target in qualified_targets
    )

    selected = _fast_lane_selected_tests(
        tmp_path,
        changed_path="deploy/index.html",
        routed_tests=stub_files,
    )

    assert set(landing_test_files) <= selected, selected
    for qualified in qualified_targets:
        assert qualified in selected, (
            f"{qualified!r} not queued for pytest -- got {selected}"
        )


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
    assert 'mypy_bin="mypy"' not in body


# --------------------------------------------------------------------------- #
# issue #1850 -- terminal verdict sentinel
# --------------------------------------------------------------------------- #


def _fake_pytest_script(path: Path, *, fail_argv_substring: str | None = None) -> None:
    """Write a pytest stand-in that prints a real-looking ``-q`` summary line.

    Always reports "3 passed" and exits 0, UNLESS ``fail_argv_substring`` is
    given and appears in some argv element, in which case it reports a
    failure and exits 1. This drives ``lane_extract_passed_count`` with
    realistic input without needing an actual pytest run.
    """
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"FAIL_MARKER = {fail_argv_substring!r}\n"
        "argv = sys.argv[1:]\n"
        "if FAIL_MARKER is not None and any(FAIL_MARKER in a for a in argv):\n"
        "    print('1 failed in 0.01s')\n"
        "    raise SystemExit(1)\n"
        "print('3 passed in 0.01s')\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


@pytest.mark.parametrize("lane", _LANES)
def test_lane_prints_passed_sentinel_as_the_last_stdout_line_on_success(
    lane: str, lane_sandbox: tuple[Path, dict[str, str]]
) -> None:
    """A clean run's last stdout line names the lane and a real passed count.

    Not just silence a truncating caller could mistake for "nothing to
    report" -- the count comes from actually parsing a phase's pytest
    summary, not a hardcoded placeholder.
    """
    repo, env = lane_sandbox
    stand_in = repo / "fake-pytest"
    _fake_pytest_script(stand_in)
    # `true` stands in for both ruff (test-fast) and mypy (test-merge): both
    # just need to resolve and exit 0 so the lane reaches its pytest phase(s)
    # -- irrelevant but harmless for whichever lane doesn't read that var.
    other_tool_stand_in = shutil.which("true") or "/usr/bin/true"
    result = _run(
        repo,
        {
            **env,
            "PYTEST": str(stand_in),
            "RUFF": other_tool_stand_in,
            "MYPY": other_tool_stand_in,
        },
        lane,
    )

    assert result.returncode == 0, result
    last_line = result.stdout.rstrip("\n").splitlines()[-1]
    prefix, suffix = f"==> {lane}: ", " passed"
    assert last_line.startswith(prefix) and last_line.endswith(suffix), result.stdout
    count = last_line[len(prefix) : -len(suffix)]
    assert count.isdigit(), result.stdout
    # test-merge's one invocation, and test-fast's unconditional always-on
    # guards phase, both always run in a fresh sandbox -- each reports 3.
    assert int(count) >= 3, result.stdout


@pytest.mark.parametrize("lane", _LANES)
def test_lane_prints_failed_sentinel_as_the_last_stdout_line_on_a_real_failure(
    lane: str, lane_sandbox: tuple[Path, dict[str, str]]
) -> None:
    """A failure inside a real phase must ALSO end with the FAILED sentinel.

    Distinct from the FATAL/resolution-path tests below: this failure comes
    from `set -e` aborting mid-script on a pytest invocation's own nonzero
    exit, which the EXIT trap has to catch just as reliably as an explicit
    early `exit`.
    """
    repo, env = lane_sandbox
    stand_in = repo / "fake-pytest"
    # test-fast's always-on guards phase is the one call site every run of
    # it reaches when nothing is stale/selected, so failing there exercises
    # the LATEST point in the lane, not just the first gate. test-merge has
    # one invocation; `--ignore=tests/voice_eval` is part of its hardcoded
    # argv, so it fails that one call.
    fail_marker = (
        "test_dependency_groups.py"
        if lane == "test-fast"
        else "--ignore=tests/voice_eval"
    )
    _fake_pytest_script(stand_in, fail_argv_substring=fail_marker)
    # `true` stands in for ruff (test-fast) and mypy (test-merge) and must
    # succeed here: the failure under test is specifically the fake pytest's
    # own nonzero exit, not a resolution-path FATAL (those are covered
    # separately below) and not the mypy gate rejecting the run before
    # pytest is ever reached.
    other_tool_stand_in = shutil.which("true") or "/usr/bin/true"
    result = _run(
        repo,
        {
            **env,
            "PYTEST": str(stand_in),
            "RUFF": other_tool_stand_in,
            "MYPY": other_tool_stand_in,
        },
        lane,
    )

    assert result.returncode != 0, result
    last_line = result.stdout.rstrip("\n").splitlines()[-1]
    assert last_line == f"==> {lane}: FAILED", result.stdout


@pytest.mark.parametrize("lane", _LANES)
def test_lane_prints_failed_sentinel_as_the_last_stdout_line_when_unresolvable(
    lane: str, lane_sandbox: tuple[Path, dict[str, str]]
) -> None:
    """Composes with the #1846 FATAL block: still ends in the same sentinel.

    The FATAL block's own last line (asserted separately in
    ``test_fatal_headline_survives_tail_truncation``) is already an
    unambiguous failure on stderr. This is the SEPARATE, uniform promise:
    every exit path -- including this one -- also ends with the same
    ``==> <lane>: FAILED`` shape on stdout, so a caller can check for one
    fixed string regardless of which exit path fired.
    """
    repo, env = lane_sandbox
    result = _run(repo, env, lane)

    assert result.returncode != 0, result
    last_line = result.stdout.rstrip("\n").splitlines()[-1]
    assert last_line == f"==> {lane}: FAILED", result.stdout


@pytest.mark.parametrize("lane", _LANES)
def test_lane_verdict_sentinel_survives_tail_truncation_when_unresolvable(
    lane: str, lane_sandbox: tuple[Path, dict[str, str]]
) -> None:
    """The scenario issue #1850 is actually about: `<lane> 2>&1 | tail -3`.

    Stderr is merged into stdout via ``stderr=subprocess.STDOUT`` (an OS-level
    merge preserving real interleaving) rather than concatenating separately
    captured streams afterwards, so this matches what a real piped caller
    would see.
    """
    repo, env = lane_sandbox
    result = subprocess.run(
        [_BASH, f"scripts/{lane}"],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    assert result.returncode != 0, result
    last_three = result.stdout.rstrip("\n").splitlines()[-3:]
    assert f"==> {lane}: FAILED" in last_three, result.stdout


# --------------------------------------------------------------------------- #
# the sentinel must never claim a verdict for a run that did not finish
# --------------------------------------------------------------------------- #

# The poison shape: the sentinel a truncated transcript reads as success.
# Matched with `search`, not `fullmatch`, because real pytest `-q` progress
# dots carry no trailing newline, so the sentinel is glued to the end of the
# dot line -- exactly as in the 2026-08-15 transcript.
_PASSED_SENTINEL_RE = re.compile(r"==> \S+: \d+ passed$")

# Bounds for the signal test. Generous enough to survive a loaded CI box,
# finite so a stand-in that never starts (or a lane that never dies) fails
# the test instead of hanging the suite.
_STUB_START_DEADLINE_SEC = 30.0
_LANE_EXIT_DEADLINE_SEC = 30.0


def _blocking_pytest_stub(repo: Path, marker: Path) -> Path:
    """A pytest stand-in that reproduces the observed transcript, then blocks.

    Writes ``marker`` only once it is genuinely running, so the caller can
    wait on a fact rather than race the signal against process startup. The
    progress dots are written WITHOUT a trailing newline and no summary line
    is ever printed: that is what a real ``pytest -q`` looks like at 23% of
    a run, and it is why the sentinel ends up glued to the dot line.

    The sleep is bounded and the fallback exit is nonzero: if the signal
    never arrives, this stand-in must not hand the lane a clean exit that
    would make the test pass for the wrong reason.
    """
    path = repo / "blocking-pytest"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        f"MARKER = {str(marker)!r}\n"
        "sys.stdout.write('.' * 65)\n"
        "sys.stdout.flush()\n"
        "with open(MARKER, 'w', encoding='utf-8') as fh:\n"
        "    fh.write('running')\n"
        "time.sleep(120)\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _kill_group(proc: "subprocess.Popen[str]", sig: int) -> None:
    """Signal the whole process group, as a real Ctrl-C / job kill would.

    Signalling only the lane's own pid would leave the pytest stand-in
    running and would not reproduce the incident, in which the lane, its
    child, and its ``tee`` all took the signal together.
    """
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):  # already reaped
        pass


def test_lane_killed_by_a_signal_does_not_print_a_passed_shaped_verdict(
    lane_sandbox: tuple[Path, dict[str, str]],
) -> None:
    """THE regression: a SIGTERM'd lane must not end in the success shape.

    Observed 2026-08-15 against a real ``scripts/test-merge`` run killed at
    ~23%::

        .....................................scripts/test-merge: line 28: ...
        ==> test-merge: 0 passed

    ``0 passed`` is the success shape, and it is the worst possible output:
    not obviously wrong, and it reads as "nothing to run". Root cause is
    bash's own behaviour, not the lane's: for SIGTERM it runs the EXIT trap
    with a stale zero in ``$?``. The lane's process status is honest
    (asserted below); only the printed text lied. The per-signal picture,
    and why that defeats a ``status >= 128`` check, is owned by
    ``lane_emit_verdict`` in ``scripts/_test_lane.sh``.

    Asserted loosely on WHICH interrupted shape appears, because what bash
    leaves in ``$?`` for a signal-run EXIT trap is bash's business and may
    differ by version: either interrupted wording is a correct outcome, and
    neither is a verdict. What is asserted strictly is the promise -- never
    a ``N passed`` tail.
    """
    repo, env = lane_sandbox
    marker = repo / "pytest-started"
    stand_in = shutil.which("true") or "/usr/bin/true"
    proc = subprocess.Popen(
        [_BASH, "scripts/test-merge"],
        cwd=repo,
        env={
            **env,
            "PYTEST": str(_blocking_pytest_stub(repo, marker)),
            "MYPY": stand_in,
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + _STUB_START_DEADLINE_SEC
        while not marker.exists():
            if proc.poll() is not None:
                pytest.fail(
                    "the lane exited before its pytest stand-in ever ran:\n"
                    f"{proc.communicate()[0]}"
                )
            if time.monotonic() >= deadline:
                pytest.fail("the blocking pytest stand-in never started")
            time.sleep(0.05)
        _kill_group(proc, signal.SIGTERM)
        stdout, _ = proc.communicate(timeout=_LANE_EXIT_DEADLINE_SEC)
    finally:
        if proc.poll() is None:
            _kill_group(proc, signal.SIGKILL)
            proc.communicate(timeout=_LANE_EXIT_DEADLINE_SEC)

    assert proc.returncode != 0, (
        "the lane's own process status was honest in the incident (143); if it "
        "is 0 here the test is no longer reproducing that shape"
    )
    last_line = stdout.rstrip("\n").splitlines()[-1]
    assert not _PASSED_SENTINEL_RE.search(last_line), (
        f"a signal-killed lane printed the SUCCESS shape: {last_line!r}\n{stdout}"
    )
    assert "INTERRUPTED" in last_line and last_line.endswith("NO VERDICT"), (
        f"expected an interrupted sentinel, got {last_line!r}\n{stdout}"
    )
    assert "==> test-merge: INTERRUPTED" in last_line, stdout


def _emit_verdict(status: int, *, preamble: str = "") -> subprocess.CompletedProcess:
    """Call ``lane_emit_verdict`` directly, under the lanes' own shell options.

    ``set -euo pipefail`` matters: the function reads its two globals
    defensively precisely because ``set -u`` is in force in a real lane, and
    a test that dropped ``-u`` would not exercise that.
    """
    script = (
        "set -euo pipefail\n"
        f"source {shlex.quote(str(_SCRIPTS / '_test_lane.sh'))}\n"
        f"{preamble}\n"
        f"lane_emit_verdict test-merge {status}\n"
    )
    return subprocess.run(
        [_BASH, "-c", script], capture_output=True, text=True
    )


def test_lane_emit_verdict_names_the_signal_when_the_status_is_honest() -> None:
    """When the status IS 128+N, say which signal -- and say it first.

    Two assertions, because the branch ORDER has two competitors and the
    first assertion alone leaves one of them untested.

    The first sets the finished/summary globals to their most permissive
    values, pinning signal-branch over the FAILED and ``N passed`` shapes.

    The second is the one that pins signal-branch over the FINISH-MARKER
    branch, by leaving ``_lane_finished`` unset -- and it has to exist
    separately, because setting the marker true in the first case takes that
    competitor out of the running entirely. This is production-reachable,
    not a contrived state: a group-wide SIGINT is the measured case where
    bash hands the trap an honest 128+N *mid-run*, so the status is 130 and
    the finish marker is false at the same moment. Were the marker checked
    first, that path would collapse to the wordless interrupted shape and
    the signal number -- the only diagnostic the operator gets about WHY the
    run stopped -- would be silently lost.
    """
    result = _emit_verdict(
        143, preamble="_lane_finished=true\n_lane_summary_seen=1\n_lane_passed_total=7"
    )

    assert result.returncode == 0, result
    assert result.stdout.strip() == (
        "==> test-merge: INTERRUPTED (signal 15) -- NO VERDICT"
    ), result

    mid_run = _emit_verdict(130, preamble="_lane_summary_seen=1")

    assert mid_run.returncode == 0, mid_run
    assert mid_run.stdout.strip() == (
        "==> test-merge: INTERRUPTED (signal 2) -- NO VERDICT"
    ), (
        "an honest signal status must name its signal even when the lane never "
        f"reached its end -- got {mid_run.stdout!r}"
    )


def test_lane_emit_verdict_refuses_a_verdict_when_the_lane_never_finished() -> None:
    """A stale-zero status with real parsed passes still gets no verdict.

    This is the isolation test for the load-bearing mechanism. Everything
    except the finish marker says "success": the status is 0, a pytest
    summary was parsed, and the running total is nonzero -- exactly the
    state the 2026-08-15 trap was in, minus the count. Only the missing
    ``_lane_finished`` can produce the right answer here.

    ``_lane_finished`` is left entirely UNSET rather than set false, which
    pins the fail-closed direction claimed in the helper's comment: a future
    lane that forgets to set it prints INTERRUPTED, never a false pass. It
    also proves the defensive read survives ``set -u``.
    """
    result = _emit_verdict(0, preamble="_lane_summary_seen=1\n_lane_passed_total=7")

    assert result.returncode == 0, result
    assert result.stdout.strip() == "==> test-merge: INTERRUPTED -- NO VERDICT", result
    assert "passed" not in result.stdout, result


@pytest.mark.parametrize("lane", _LANES)
def test_lane_that_parsed_no_pytest_summary_says_so_instead_of_zero_passed(
    lane: str, lane_sandbox: tuple[Path, dict[str, str]]
) -> None:
    """A completed run that never saw a pytest summary must not say "0 passed".

    ``0 passed`` is indistinguishable from the signal-death shape this
    section exists for, and from a lane whose pytest never really reported.
    The exit status is deliberately unchanged -- this run is a clean exit 0,
    it just has nothing to claim, so the honesty is entirely in the text.

    The stand-in exits 0 while printing nothing at all, which is what a
    pytest replaced by a recording stub (or by ``true``) looks like.
    """
    repo, env = lane_sandbox
    stand_in = repo / "silent-pytest"
    stand_in.write_text(
        "#!/usr/bin/env python3\nraise SystemExit(0)\n", encoding="utf-8"
    )
    stand_in.chmod(0o755)
    other_tool_stand_in = shutil.which("true") or "/usr/bin/true"
    result = _run(
        repo,
        {
            **env,
            "PYTEST": str(stand_in),
            "RUFF": other_tool_stand_in,
            "MYPY": other_tool_stand_in,
        },
        lane,
    )

    assert result.returncode == 0, result
    last_line = result.stdout.rstrip("\n").splitlines()[-1]
    assert last_line == f"==> {lane}: NO VERDICT (no pytest summary parsed)", (
        result.stdout
    )


# --------------------------------------------------------------------------- #
# issue #1758 -- a stale --last-failed id must not fall back to a full run
# --------------------------------------------------------------------------- #


def test_fast_lane_prunes_a_stale_last_failed_id_without_a_full_suite_fallback(
    tmp_path: Path,
) -> None:
    """A renamed test's cached node id must not trigger a full-suite run.

    Reproduced (not asserted from prose): pytest's own `--last-failed`
    machinery, given a cache with a stale entry, prints "run-last-failure: N
    known failures not in selected tests" and then runs every item in
    whatever scope it was given -- `--last-failed-no-failures none` only
    guards the case where the cache is EMPTY, not this one. The lane must
    validate cached ids itself and never hand a stale one to `--last-failed`.

    The stale id is seeded alongside a live one so pruning is proven
    surgical (the live entry survives and gets run) rather than "wipe the
    whole cache on any drift".
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "tests").mkdir()
    for name in ("test-fast", "_test_lane.sh"):
        shutil.copy2(_SCRIPTS / name, repo / "scripts" / name)
    shutil.copy2(_SCRIPTS / "ci-classify.py", repo / "scripts" / "ci-classify.py")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "JTS Tests")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")

    (repo / "test_still_here.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    stale_id = "test_renamed_away.py::test_old_name"
    live_id = "test_still_here.py::test_ok"
    cache_dir = repo / ".pytest_cache" / "v" / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "lastfailed").write_text(
        json.dumps({stale_id: True, live_id: True}), encoding="utf-8"
    )

    calls = repo / "pytest-calls.jsonl"
    recorder = repo / "recording-pytest"
    # Mirrors real pytest's own observed behavior (verified against pytest
    # 9.0.3): `--collect-only` on an explicit node id whose file does not
    # exist fails with a usage error; on one that does exist, it succeeds.
    recorder.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "argv = sys.argv[1:]\n"
        "with open(os.environ['PYTEST_CALLS'], 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(argv) + '\\n')\n"
        "if '--collect-only' in argv:\n"
        "    ids = [a for a in argv if '::' in a]\n"
        "    missing = [i for i in ids if not Path(i.split('::')[0]).exists()]\n"
        "    raise SystemExit(4 if missing else 0)\n"
        "print('1 passed in 0.01s')\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    recorder.chmod(0o755)
    stand_in = shutil.which("true") or "/usr/bin/true"

    result = subprocess.run(
        [_BASH, "scripts/test-fast"],
        cwd=repo,
        env={
            **os.environ,
            "PYTEST": str(recorder),
            "PYTEST_CALLS": str(calls),
            "RUFF": stand_in,
            "TEST_BASE": "missing-base",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result
    calls_made = [
        json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()
    ]

    # The mechanism that falls back to a full run is the --last-failed FLAG
    # itself -- the lane must never pass it once it does its own validation.
    assert not any("--last-failed" in call for call in calls_made), calls_made

    exec_calls = [c for c in calls_made if "--collect-only" not in c]
    stale_in_exec = [c for c in exec_calls if any(stale_id in a for a in c)]
    assert not stale_in_exec, calls_made
    live_in_exec = [c for c in exec_calls if any(live_id in a for a in c)]
    assert live_in_exec, calls_made

    # Pruning is surgical: the stale id is gone, the live one survives.
    pruned = json.loads((cache_dir / "lastfailed").read_text(encoding="utf-8"))
    assert stale_id not in pruned, pruned
    assert live_id in pruned, pruned


def test_fast_lane_skips_last_failed_when_every_cached_id_is_stale(
    tmp_path: Path,
) -> None:
    """All-stale is "nothing to run last-failed", not "run everything".

    The degenerate case of the surgical-pruning test above: when every
    cached id is stale, the phase must fall back to the empty-cache message
    rather than either erroring or handing pytest a now-empty --last-failed
    call.
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "tests").mkdir()
    for name in ("test-fast", "_test_lane.sh"):
        shutil.copy2(_SCRIPTS / name, repo / "scripts" / name)
    shutil.copy2(_SCRIPTS / "ci-classify.py", repo / "scripts" / "ci-classify.py")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "JTS Tests")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")

    stale_id = "test_renamed_away.py::test_old_name"
    cache_dir = repo / ".pytest_cache" / "v" / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "lastfailed").write_text(
        json.dumps({stale_id: True}), encoding="utf-8"
    )

    calls = repo / "pytest-calls.jsonl"
    recorder = repo / "recording-pytest"
    recorder.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "argv = sys.argv[1:]\n"
        "with open(os.environ['PYTEST_CALLS'], 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(argv) + '\\n')\n"
        "if '--collect-only' in argv:\n"
        "    ids = [a for a in argv if '::' in a]\n"
        "    missing = [i for i in ids if not Path(i.split('::')[0]).exists()]\n"
        "    raise SystemExit(4 if missing else 0)\n"
        "print('1 passed in 0.01s')\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    recorder.chmod(0o755)
    stand_in = shutil.which("true") or "/usr/bin/true"

    result = subprocess.run(
        [_BASH, "scripts/test-fast"],
        cwd=repo,
        env={
            **os.environ,
            "PYTEST": str(recorder),
            "PYTEST_CALLS": str(calls),
            "RUFF": stand_in,
            "TEST_BASE": "missing-base",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result
    assert "no last-failed tests recorded" in result.stdout

    calls_made = [
        json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()
    ]
    exec_calls = [c for c in calls_made if "--collect-only" not in c]
    assert not any(stale_id in a for c in exec_calls for a in c), calls_made

    pruned = json.loads((cache_dir / "lastfailed").read_text(encoding="utf-8"))
    assert pruned == {}, pruned


# --------------------------------------------------------------------------- #
# issue #1910 -- test-merge carries a local mypy gate ahead of pytest
# --------------------------------------------------------------------------- #


def test_test_merge_also_refuses_on_a_missing_mypy(
    lane_sandbox: tuple[Path, dict[str, str]],
) -> None:
    """test-merge resolves two tools; mypy must be guarded like pytest.

    Mirrors ``test_test_fast_also_refuses_on_a_missing_ruff`` for the tool
    this PR adds. Pointing ``$PYTEST`` at a real executable clears the first
    gate so the failure attributable to ``mypy`` is the one observed -- this
    also pins that pytest is resolved before mypy, so an entirely bare
    sandbox (no overrides at all) still reports the pre-existing "pytest
    unresolvable" FATAL via the parametrized
    ``test_lane_refuses_loudly_when_pytest_is_unresolvable`` above, unchanged
    by this addition.
    """
    repo, env = lane_sandbox
    result = _run(
        repo, {**env, "PYTEST": shutil.which("true") or "/usr/bin/true"}, "test-merge"
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "FATAL" in combined
    # The exact resolve_lane_tool phrasing, not a bare "mypy" substring: a
    # loose membership check would also pass if the FATAL named some other
    # tool but "mypy" happened to appear elsewhere in the transcript.
    assert "could not resolve 'mypy'" in combined
    assert "NO TESTS WERE RUN" in combined


def _recording_stub(repo: Path, name: str, calls_path: Path, *, exit_code: int = 0) -> Path:
    """A stand-in executable that appends ``name`` to ``calls_path`` and exits.

    Prints a real-looking ``-q`` summary line so a stand-in used as the
    ``$PYTEST`` override does not break ``lane_extract_passed_count`` if a
    caller happens to pipe it through ``lane_pipe_pytest``.
    """
    path = repo / f"recording-{name}"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        f"NAME = {name!r}\n"
        f"CALLS = {str(calls_path)!r}\n"
        "with open(CALLS, 'a', encoding='utf-8') as f:\n"
        "    f.write(NAME + '\\n')\n"
        "print('3 passed in 0.01s')\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_test_merge_runs_mypy_before_pytest(
    lane_sandbox: tuple[Path, dict[str, str]],
) -> None:
    """The mypy gate runs, and it runs strictly before the pytest phase.

    Issue #1910: no local lane ran mypy at all. This is the positive-path
    ordering proof -- both tools are recording stand-ins, and the call order
    written to the log is the real ordering claim, not a string search over
    the script's source (which would still pass if the call were dead code).
    """
    repo, env = lane_sandbox
    calls = repo / "calls.jsonl"
    mypy_stub = _recording_stub(repo, "mypy", calls)
    pytest_stub = _recording_stub(repo, "pytest", calls)

    result = _run(
        repo,
        {**env, "MYPY": str(mypy_stub), "PYTEST": str(pytest_stub)},
        "test-merge",
    )

    assert result.returncode == 0, result
    assert calls.read_text(encoding="utf-8").splitlines() == ["mypy", "pytest"], (
        result.stdout + result.stderr
    )


def test_test_merge_mypy_failure_stops_before_pytest_and_fails_the_lane(
    lane_sandbox: tuple[Path, dict[str, str]],
) -> None:
    """A mypy failure must fail the #1850 sentinel AND never reach pytest.

    The merge lane runs the mypy invocation unguarded under `set -e`, so a
    nonzero mypy exit aborts the script immediately -- pytest must never even
    start, and the EXIT trap must still print the lane's FAILED sentinel as
    the true last stdout line, exactly composing with #1850's machinery
    rather than needing its own verdict path.
    """
    repo, env = lane_sandbox
    calls = repo / "calls.jsonl"
    mypy_stub = _recording_stub(repo, "mypy", calls, exit_code=1)
    pytest_stub = _recording_stub(repo, "pytest", calls)

    result = _run(
        repo,
        {**env, "MYPY": str(mypy_stub), "PYTEST": str(pytest_stub)},
        "test-merge",
    )

    assert result.returncode != 0, result
    last_line = result.stdout.rstrip("\n").splitlines()[-1]
    assert last_line == "==> test-merge: FAILED", result.stdout
    assert calls.read_text(encoding="utf-8").splitlines() == ["mypy"], (
        "pytest must never run once the mypy gate fails"
    )


@pytest.mark.parametrize(
    ("script", "routed_tests", "expected"),
    (
        (
            "jasper-camilla-recover",
            ("tests/test_camilla_recover_script.py",),
            "named",
        ),
        ("jasper-apply-airplay-mode", ("tests/test_airplay_render.py",), "grep"),
        (
            # issue #3846's tests/test_aec_reconcile_rule_pins.py sits beside
            # the convention match (tests/test_aec_reconcile.py) rather than
            # replacing it, so the convention branch alone silently ran only
            # one of the two -- caught only by re-deriving the same glob idiom
            # the jasper/*/*.py arm already uses for sibling test files.
            "jasper-aec-reconcile",
            ("tests/test_aec_reconcile.py", "tests/test_aec_reconcile_rule_pins.py"),
            "glob",
        ),
    ),
)
def test_fast_lane_routes_deploy_bin_scripts_to_their_tests(
    tmp_path: Path,
    script: str,
    routed_tests: tuple[str, ...],
    expected: str,
) -> None:
    """A deploy/bin shell script must select the tests that exercise it.

    These scripts are reached through subprocess, so the jasper/*.py arms
    cannot route them and before this arm a change under deploy/bin selected
    nothing at all. The three cases pin the arm's three ways of finding a
    test: `jasper-camilla-recover` has a test named after it,
    `jasper-apply-airplay-mode` has none and is covered by
    tests/test_airplay_render.py instead (whose 18 macOS-only failures
    reached main green through this gap), and `jasper-aec-reconcile` has a
    named test PLUS a sibling glob match that the named-only check used to
    miss. The `grep` case's stub carries the script's name so the sandbox
    reproduces how the real tree pins it -- by literal reference, not by
    filename.
    """
    selected = _fast_lane_selected_tests(
        tmp_path,
        changed_path=f"deploy/bin/{script}",
        routed_tests=routed_tests,
        test_contents=(
            {routed_tests[0]: f"# exercises {script}\n"}
            if expected == "grep"
            else None
        ),
    )

    assert set(routed_tests) <= selected, selected
