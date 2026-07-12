# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ratchet: no NEW hand-rolled tempfile+rename writers outside atomic_io.

``jasper/atomic_io.py`` is the canonical atomic text-file writer (its
module docstring explains the two properties that are easy to get
subtly wrong by hand: same-filesystem rename, and chmod-before-rename
so no wider-permission window is ever visible). New code should call
``atomic_write_text`` instead of re-rolling the pattern.

This test detects the hand-rolled shape — an actual *call* to
``tempfile.mkstemp`` / ``tempfile.NamedTemporaryFile`` plus a call to
``os.replace`` / ``os.rename`` in the same module (AST-based, so
comments and docstrings mentioning the pattern don't count) — and
asserts the offender set EXACTLY matches the allowlist below.

- Added a new hand-rolled writer? The test fails: use
  ``jasper.atomic_io.atomic_write_text`` (pass ``mode=`` to match the
  permissions your file needs — tempfiles publish 0600 if you never
  chmod, so a verbatim migration usually wants ``mode=0o600``).
- Migrated one off the list? Remove it here so the ratchet tightens.
"""
from __future__ import annotations

import ast
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_JASPER = _REPO / "jasper"

# Current offenders, frozen 2026-06-10. This is a burn-down list, not an
# endorsement — migrate entries to atomic_write_text when touching them
# (preserving each writer's published mode), EXCEPT where noted.
_ALLOWLIST = {
    # Deliberately different — KEEP. Durability beyond os.replace:
    # fsyncs the tempfile AND the parent directory so the WiFi recovery
    # stash survives the exact power-yank incident it exists for.
    # atomic_write_text has no fsync (by design — most callers don't
    # want the latency); do not migrate this one.
    "jasper/wifi_guardian_persistence.py",
    # Plain burn-down candidates (no fsync; mostly text/YAML/JSON with
    # an explicit chmod) — exact fits for atomic_write_text(mode=...).
    "jasper/active_speaker/staging.py",
    "jasper/active_speaker/startup_load.py",
    "jasper/assistant_loudness.py",
    "jasper/audio_quality.py",
    "jasper/correction/replay_artifacts.py",
    "jasper/output_hardware.py",
    "jasper/output_topology.py",
    "jasper/sound/profile.py",
}


def _calls_tempfile_and_rename(tree: ast.AST) -> bool:
    has_tmp = has_rename = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else None
        )
        if name in ("mkstemp", "NamedTemporaryFile"):
            has_tmp = True
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "os"
            and func.attr in ("replace", "rename")
        ):
            has_rename = True
    return has_tmp and has_rename


def test_no_new_hand_rolled_atomic_writers():
    offenders = set()
    for path in sorted(_JASPER.rglob("*.py")):
        rel = path.relative_to(_REPO).as_posix()
        if rel == "jasper/atomic_io.py":
            continue  # the canonical implementation itself
        if _calls_tempfile_and_rename(ast.parse(path.read_text())):
            offenders.add(rel)

    new = offenders - _ALLOWLIST
    assert not new, (
        "New hand-rolled tempfile+rename writer(s) detected:\n  "
        + "\n  ".join(sorted(new))
        + "\nUse jasper.atomic_io.atomic_write_text instead (see this "
        "test's docstring for the mode-preservation note). Only add to "
        "the allowlist with a documented reason, e.g. fsync durability."
    )

    stale = _ALLOWLIST - offenders
    assert not stale, (
        "Allowlisted module(s) no longer hand-roll the pattern — remove "
        "them so the ratchet tightens:\n  " + "\n  ".join(sorted(stale))
    )
