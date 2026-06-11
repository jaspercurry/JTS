"""Test scripts/_lib.sh deploy-direction helpers — the downgrade guard.

Multiple checkouts/worktrees (and multiple agent sessions) deploy to the
same Pi. On 2026-06-11 (jts3.local) a stale parallel checkout deployed
four minutes after a bugfix build and silently reverted it — the
operator's hardware retest then ran the old code and the fix looked
broken. deploy-to-pi.sh now classifies the deploy direction against the
Pi's installed build manifest BEFORE rsync and aborts downgrades unless
JASPER_DEPLOY_ALLOW_DOWNGRADE=1.

These tests pin the two pure helpers that decision rests on, sourced
under bash against a scratch git repo:

* ``classify_deploy_direction`` outcome tokens for every history shape
  (same / forward / downgrade / diverged / unknown), including the
  ``-dirty`` suffix build.txt records for uncommitted-tree deploys;
* ``build_manifest_value`` parsing of build.txt read over ssh,
  including the CRLF line endings ``ssh -tt`` (interactive sudo)
  produces.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "_lib.sh"

_GIT_ID = ["-c", "user.email=t@test", "-c", "user.name=t"]


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *_GIT_ID, *args],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def history(tmp_path):
    """Scratch repo: A → B on main; sibling branch with C forked from A.

        A ── B        (main)
         \\
          ── C        (sibling)
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "f").write_text("a\n")
    _git(repo, "add", "f")
    _git(repo, "commit", "-qm", "A")
    sha_a = _git(repo, "rev-parse", "HEAD")
    (repo / "f").write_text("b\n")
    _git(repo, "commit", "-qam", "B")
    sha_b = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-qc", "sibling", sha_a)
    (repo / "g").write_text("c\n")
    _git(repo, "add", "g")
    _git(repo, "commit", "-qm", "C")
    sha_c = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "main")
    return repo, sha_a, sha_b, sha_c


def _classify(repo: Path, local_sha: str, installed_sha: str) -> str:
    script = (
        f'source "{LIB}"; '
        f'classify_deploy_direction "$1" "$2"'
    )
    proc = subprocess.run(
        ["bash", "-c", script, "bash", local_sha, installed_sha],
        capture_output=True, text=True, timeout=30, cwd=repo,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_same_commit_is_a_redeploy(history):
    repo, _a, b, _c = history
    assert _classify(repo, b, b) == "same"


def test_dirty_suffixes_are_stripped_before_comparison(history):
    # build.txt records "-dirty" when the deploying tree had uncommitted
    # changes; ancestry math must run on the underlying commit.
    repo, a, b, _c = history
    assert _classify(repo, b, f"{b}-dirty") == "same"
    assert _classify(repo, f"{a}-dirty", b) == "downgrade"


def test_installed_ancestor_of_local_is_forward(history):
    repo, a, b, _c = history
    assert _classify(repo, b, a) == "forward"


def test_local_ancestor_of_installed_is_downgrade(history):
    # The incident shape: the Pi runs B (the fixes); a checkout still on
    # A deploys. This is the case the deploy must refuse by default.
    repo, a, b, _c = history
    assert _classify(repo, a, b) == "downgrade"


def test_split_histories_are_diverged(history):
    repo, _a, b, c = history
    assert _classify(repo, b, c) == "diverged"
    assert _classify(repo, c, b) == "diverged"


def test_unresolvable_installed_sha_is_unknown(history):
    repo, _a, b, _c = history
    assert _classify(repo, b, "f" * 40) == "unknown_installed"


def test_empty_installed_sha_is_unknown(history):
    repo, _a, b, _c = history
    assert _classify(repo, b, "") == "unknown_installed"


def _manifest_value(manifest: str, key: str) -> str:
    script = (
        f'source "{LIB}"; '
        f'build_manifest_value "$1" "$2"'
    )
    proc = subprocess.run(
        ["bash", "-c", script, "bash", manifest, key],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_manifest_value_extracts_key():
    manifest = (
        "JASPER_GIT_SHA=8637206e\n"
        "JASPER_GIT_SHA_FULL=8637206ea21ff1b49e8f95723793ccbb22fb96c0\n"
        "JASPER_GIT_BRANCH=HEAD\n"
        "JASPER_INSTALL_AT=2026-06-11T12:43:09-04:00\n"
    )
    assert _manifest_value(manifest, "JASPER_GIT_SHA_FULL") == (
        "8637206ea21ff1b49e8f95723793ccbb22fb96c0"
    )
    assert _manifest_value(manifest, "JASPER_GIT_BRANCH") == "HEAD"


def test_manifest_value_tolerates_crlf_from_interactive_sudo():
    # `ssh -tt` (the interactive-sudo deploy path) rewrites \n as \r\n;
    # a raw parse would smuggle \r into the SHA and break git lookups.
    manifest = "JASPER_GIT_SHA_FULL=abc123\r\nJASPER_GIT_BRANCH=main\r\n"
    assert _manifest_value(manifest, "JASPER_GIT_SHA_FULL") == "abc123"


def test_manifest_value_absent_key_is_empty_and_exits_zero():
    # pipefail-safety: an absent key must yield "" with rc 0, or the
    # deploy script's `set -euo pipefail` would abort on fresh Pis.
    assert _manifest_value("JASPER_GIT_SHA=abc\n", "JASPER_GIT_SHA_FULL") == ""
