# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin the Rust build-cache staging contract in rust-daemons.sh.

Cargo's freshness check is mtime-based: a unit recompiles only when a
source file is NEWER than the fingerprint stamped at the last compile.
The old staging (`rsync -a`) preserved mtimes end to end (laptop ->
checkout -> /var/cache/<name>-build), so a changed file whose checkout
mtime predated the cache's last build landed "in the past" and cargo
declared the crate Fresh — install.sh then shipped the stale binary
while reporting success. Bit twice on hardware: 2026-07-02
(jasper-usbsink-audio: new HTTP endpoints 404'd) and 2026-07-10
(jasper-outputd: the #1202 chip-ref journal-spam fix never went live;
`cargo build -v` in the poisoned cache said `Fresh` in 0.03s while the
staged source contained the fix).

Contract, enforced here:

  1. `stage_rust_crate` copies by content (--checksum) WITHOUT
     preserving times (-rlpgoD is -a minus -t): a content-changed file
     always lands newer than the last fingerprint (cargo rebuilds), an
     unchanged file is skipped and keeps its old mtime (no spurious
     rebuild churn).
  2. Every crate-staging copy in rust-daemons.sh goes through
     `stage_rust_crate` — a new sibling crate staged with a raw
     mtime-preserving rsync reintroduces the trap.
  3. `rust_build_cache_reset_if_stale_format` clears target/ exactly
     once per RUST_BUILD_CACHE_FORMAT bump, healing caches that were
     already poisoned before the staging fix (their fingerprints
     postdate correct source mtimes, so honest staging alone can never
     trigger the recompile).

Functional tests run the real shipped bash functions against the real
rsync on temp dirs (mirrors tests/test_install_rust_daemon_restart.py's
source-of-truth-is-the-script approach).
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_RUST_DAEMONS = ROOT / "deploy" / "lib" / "install" / "rust-daemons.sh"

_MARKER = ".jts-build-cache-format"

pytestmark = pytest.mark.skipif(
    shutil.which("rsync") is None or shutil.which("bash") is None,
    reason="requires rsync + bash on PATH",
)


def _run_helpers(snippet: str) -> subprocess.CompletedProcess[str]:
    """Source the shipped script and run `snippet` against its functions."""
    script = (
        "set -euo pipefail\n"
        f"source {shlex.quote(str(_RUST_DAEMONS))}\n"
        f"{snippet}\n"
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )


def _stage(src: Path, dest: Path) -> None:
    proc = _run_helpers(
        f"stage_rust_crate {shlex.quote(str(src))} {shlex.quote(str(dest))}"
    )
    assert proc.returncode == 0, proc.stderr


# --------------------------------------------------------------------------
# 1. Staging semantics (the mtime trap itself)
# --------------------------------------------------------------------------
def test_content_change_with_past_mtime_lands_newer_than_build_stamp(tmp_path):
    """The regression that shipped stale binaries: source content changes
    but its preserved mtime predates the cache's last compile. Staged
    with the shipped flags, the changed file must land with a CURRENT
    mtime (newer than the fingerprint stamp) so cargo sees it dirty.
    Under the old `rsync -a` this assertion fails."""
    src = tmp_path / "src-crate"
    cache = tmp_path / "cache"
    (src / "src").mkdir(parents=True)
    main_rs = src / "src" / "main.rs"
    main_rs.write_text("fn main() { /* old */ }\n", encoding="utf-8")

    _stage(src, cache)
    staged = cache / "src" / "main.rs"
    assert staged.exists()

    now = int(os.stat(cache).st_mtime)
    build_stamp = now - 100  # simulated fingerprint time of the last compile
    # Cache file predates the stamp, as after a normal earlier build.
    os.utime(staged, (build_stamp - 100, build_stamp - 100))

    # New content arrives carrying a checkout mtime OLDER than the stamp
    # (rsync -a from the laptop preserves it into the checkout).
    main_rs.write_text("fn main() { /* fixed */ }\n", encoding="utf-8")
    os.utime(main_rs, (build_stamp - 50, build_stamp - 50))

    _stage(src, cache)

    assert staged.read_text(encoding="utf-8") == "fn main() { /* fixed */ }\n"
    assert int(os.stat(staged).st_mtime) > build_stamp, (
        "content-changed source staged with an mtime at or before the last "
        "build stamp — cargo would declare the crate Fresh and install.sh "
        "would ship a stale binary (the 2026-07-10 jasper-outputd incident)"
    )


def test_unchanged_content_keeps_old_mtime(tmp_path):
    """The other direction: identical content must be SKIPPED (mtime
    untouched) even when the source-side mtime moved, or every deploy
    would recompile every crate from scratch on a 1 GB Pi."""
    src = tmp_path / "src-crate"
    cache = tmp_path / "cache"
    (src / "src").mkdir(parents=True)
    main_rs = src / "src" / "main.rs"
    main_rs.write_text("fn main() {}\n", encoding="utf-8")

    _stage(src, cache)
    staged = cache / "src" / "main.rs"

    planted = int(os.stat(staged).st_mtime) - 5000
    os.utime(staged, (planted, planted))
    # Same content, fresher source mtime (e.g. a re-checkout).
    os.utime(main_rs, (planted + 9000, planted + 9000))

    _stage(src, cache)

    assert int(os.stat(staged).st_mtime) == planted, (
        "unchanged file was rewritten — --checksum skip semantics lost, "
        "every deploy would dirty every crate"
    )


def test_staging_preserves_target_and_marker_but_deletes_stale_sources(tmp_path):
    src = tmp_path / "src-crate"
    cache = tmp_path / "cache"
    (src / "src").mkdir(parents=True)
    (src / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")

    (cache / "target" / "release").mkdir(parents=True)
    artifact = cache / "target" / "release" / "daemon-bin"
    artifact.write_bytes(b"\x7fELF-stale")
    marker = cache / _MARKER
    marker.write_text("1\n", encoding="utf-8")
    stale = cache / "renamed_module.rs"
    stale.write_text("// removed upstream\n", encoding="utf-8")

    _stage(src, cache)

    assert artifact.exists(), "target/ (cargo incremental state) must survive staging"
    assert marker.exists(), "cache-format marker must survive staging --delete"
    assert not stale.exists(), "--delete must drop sources removed upstream"


# --------------------------------------------------------------------------
# 2. One-time cache-format reset
# --------------------------------------------------------------------------
def _reset(cache: Path) -> subprocess.CompletedProcess[str]:
    return _run_helpers(
        "rust_build_cache_reset_if_stale_format "
        f"{shlex.quote(str(cache))} testdaemon"
    )


def _shipped_format() -> str:
    proc = _run_helpers('printf "%s" "${RUST_BUILD_CACHE_FORMAT}"')
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "RUST_BUILD_CACHE_FORMAT must be non-empty"
    return proc.stdout.strip()


def test_reset_purges_target_when_marker_missing(tmp_path):
    cache = tmp_path / "cache"
    (cache / "target" / "release").mkdir(parents=True)
    (cache / "target" / "release" / "bin").write_bytes(b"stale")

    proc = _reset(cache)
    assert proc.returncode == 0, proc.stderr
    assert not (cache / "target").exists(), (
        "legacy cache (no marker) kept its poisoned cargo state"
    )
    assert (cache / _MARKER).read_text(encoding="utf-8").strip() == _shipped_format()


def test_reset_purges_target_when_marker_outdated(tmp_path):
    cache = tmp_path / "cache"
    (cache / "target").mkdir(parents=True)
    (cache / "target" / "bin").write_bytes(b"stale")
    (cache / _MARKER).write_text("0\n", encoding="utf-8")

    proc = _reset(cache)
    assert proc.returncode == 0, proc.stderr
    assert not (cache / "target").exists()
    assert (cache / _MARKER).read_text(encoding="utf-8").strip() == _shipped_format()


def test_reset_noop_when_marker_current(tmp_path):
    cache = tmp_path / "cache"
    (cache / "target").mkdir(parents=True)
    kept = cache / "target" / "bin"
    kept.write_bytes(b"fresh")
    (cache / _MARKER).write_text(_shipped_format() + "\n", encoding="utf-8")

    proc = _reset(cache)
    assert proc.returncode == 0, proc.stderr
    assert kept.exists(), (
        "current-format cache was purged — every deploy would full-rebuild"
    )


# --------------------------------------------------------------------------
# 3. Script-shape contract
# --------------------------------------------------------------------------
def test_all_crate_staging_goes_through_stage_rust_crate():
    """A new sibling crate staged with a raw mtime-preserving rsync
    silently reintroduces the stale-binary trap. Exactly one rsync
    invocation may exist (inside stage_rust_crate) and it must compare
    by content without preserving times."""
    text = _RUST_DAEMONS.read_text(encoding="utf-8")
    invocations = [
        line.strip()
        for line in text.splitlines()
        if re.match(r"^\s*rsync\b", line)
    ]
    assert len(invocations) == 1, (
        f"expected exactly one rsync invocation (in stage_rust_crate), "
        f"found {invocations}"
    )
    assert "--checksum" in invocations[0]
    assert "-rlpgoD" in invocations[0], (
        "staging must not preserve times (-rlpgoD is -a minus -t); "
        "a bare -a resurrects the cargo false-Fresh trap"
    )
    assert re.search(r"^\s*rsync\s+-a\b", text, re.MULTILINE) is None


def test_build_calls_reset_before_staging():
    text = _RUST_DAEMONS.read_text(encoding="utf-8")
    body = text.split("build_install_rust_daemon() {", 1)[1]
    reset_at = body.find("rust_build_cache_reset_if_stale_format")
    stage_at = body.find("stage_rust_crate")
    assert reset_at != -1, "build_install_rust_daemon must reset stale-format caches"
    assert stage_at != -1, "build_install_rust_daemon must stage via stage_rust_crate"
    assert reset_at < stage_at, "format reset must run before source staging"


def test_marker_name_matches_staging_exclude():
    """The reset helper's marker lives inside the cache dir; the staging
    rsync --delete must exclude exactly that name or every deploy would
    wipe the marker and full-rebuild."""
    text = _RUST_DAEMONS.read_text(encoding="utf-8")
    assert f"--exclude='/{_MARKER}'" in text
    assert f"{{cache_dir}}/{_MARKER}" in text
