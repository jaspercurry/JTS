#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Align deploy/constraints-pi.pins's pins to uv.lock.

``deploy/constraints-pi.pins`` is Pi-generated (``generate-pi-constraints.sh``)
and ``uv.lock`` is laptop/CI-resolved. Dependabot's uv ecosystem cannot see
the ``.pins`` file at all (it only matches ``*.txt``/``*.in``/``uv.lock``),
so it only ever bumps ``uv.lock`` — this script is what carries a uv-side
bump over to the Pi overlay; ``tests/test_constraints_pi_resolvable.py``
guards the #1275 drift class if that step is skipped. Co-resolving by hand
meant reading two lockfiles and editing pins one at a time (see the
2026-08-08 note in the constraints header). This makes that step one
command.

It rewrites every package present in BOTH files (``walked_packages`` below)
to uv.lock's version, except the documented ``EXCEPTIONS``; every other line
is left byte-identical. A full refresh still comes from a real Pi via
``generate-pi-constraints.sh``.

Usage:
  python3 scripts/align-pi-constraint-pins.py            # rewrite in place
  python3 scripts/align-pi-constraint-pins.py --check     # report, exit 1 on drift
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONSTRAINTS = ROOT / "deploy" / "constraints-pi.pins"
UV_LOCK = ROOT / "uv.lock"

_PIN_RE = re.compile(r"^([A-Za-z0-9._-]+)==([^\s;]+)$")

# Packages that legitimately pin a different version in constraints-pi.pins
# than uv.lock, keyed by PEP 503 canonical name, with a one-line reason.
# Empty today (#2256) — everything present in both files is expected to
# agree. This is the single owner of the exception set;
# tests/test_constraints_pi_resolvable.py imports it, so the guard and the
# tool that repairs its findings can never disagree about scope.
EXCEPTIONS: dict[str, str] = {}


def canon(name: str) -> str:
    """PEP 503 normalization, so constraints-pi.pins's ``pydantic_core``
    matches uv.lock's ``pydantic-core``."""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def uv_lock_versions(text: str) -> dict[str, str]:
    return {
        canon(pkg["name"]): pkg["version"]
        for pkg in tomllib.loads(text).get("package", [])
        if pkg.get("name") and pkg.get("version")
    }


def constraint_versions(text: str) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in text.splitlines():
        match = _PIN_RE.match(line.strip())
        if match:
            pins[canon(match.group(1))] = match.group(2)
    return pins


def walked_packages(constraints_text: str, lock: dict[str, str]) -> set[str]:
    """Canonical names present in both files, minus documented EXCEPTIONS.

    This is the scope of pin agreement: every package the overlay and the
    lock both pin, except the ones EXCEPTIONS says may legitimately differ.
    """
    return (set(constraint_versions(constraints_text)) & set(lock)) - set(EXCEPTIONS)


def align(constraints_text: str, lock: dict[str, str]) -> tuple[str, list[str]]:
    """Return the rewritten constraints text and one line per changed pin.

    The original name spelling is preserved; only the version moves.
    """
    in_scope = walked_packages(constraints_text, lock)
    changes: list[str] = []
    lines = constraints_text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        match = _PIN_RE.match(line.strip())
        if not match:
            continue
        name, current = match.group(1), match.group(2)
        key = canon(name)
        if key not in in_scope:
            continue
        wanted = lock[key]
        if wanted == current:
            continue
        newline = "\n" if line.endswith("\n") else ""
        lines[index] = f"{name}=={wanted}{newline}"
        changes.append(f"{name}: {current} -> {wanted}")
    return "".join(lines), changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift and exit 1 without writing",
    )
    args = parser.parse_args(argv)

    original = CONSTRAINTS.read_text(encoding="utf-8")
    lock = uv_lock_versions(UV_LOCK.read_text(encoding="utf-8"))

    updated, changes = align(original, lock)

    if not changes:
        print("deploy/constraints-pi.pins: already matches uv.lock")
        return 0

    for change in changes:
        print(f"  {change}")

    if args.check:
        print(
            f"{len(changes)} pin(s) drifted from uv.lock — run "
            "`python3 scripts/align-pi-constraint-pins.py` to co-resolve",
            file=sys.stderr,
        )
        return 1

    CONSTRAINTS.write_text(updated, encoding="utf-8")
    print(f"deploy/constraints-pi.pins: aligned {len(changes)} pin(s) to uv.lock")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
