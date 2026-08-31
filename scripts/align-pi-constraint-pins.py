#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Align deploy/constraints-pi.txt's cross-ecosystem pins to uv.lock.

``deploy/constraints-pi.txt`` is Pi-generated (``generate-pi-constraints.sh``)
and ``uv.lock`` is laptop/CI-resolved, so Dependabot can only ever move one
side: it bumps ``uv.lock`` and leaves the Pi overlay behind, and
``tests/test_constraints_pi_resolvable.py`` then fails the PR on the #1275
drift class. That is the guard working — a fresh deploy really would hit
``ResolutionImpossible`` — but co-resolving it by hand meant reading two
lockfiles and editing pins one at a time (see the 2026-08-08 note in the
constraints header). This makes that step one command.

It rewrites ONLY the packages in ``CROSS_ECOSYSTEM_PIN_CHAIN``, which is the
same table the guard asserts on; every other line is left byte-identical.
A full refresh still comes from a real Pi via ``generate-pi-constraints.sh``.

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
CONSTRAINTS = ROOT / "deploy" / "constraints-pi.txt"
UV_LOCK = ROOT / "uv.lock"

_PIN_RE = re.compile(r"^([A-Za-z0-9._-]+)==([^\s;]+)$")

# Packages whose constraints-pi.txt pin must track the co-resolved uv.lock.
# Reasons are load-bearing — verified against live PyPI metadata on
# 2026-07-11 (#1275). Names are PEP 503 canonical. This is the single owner
# of the table; tests/test_constraints_pi_resolvable.py imports it.
CROSS_ECOSYSTEM_PIN_CHAIN: dict[str, str] = {
    # pydantic hard-pins pydantic-core to an EXACT ==version
    # (pydantic 2.13.4 -> pydantic-core==2.46.4). Bumping pydantic-core
    # alone (dependabot #745) is ResolutionImpossible.
    "pydantic": "drives the pydantic-core exact pin",
    "pydantic-core": "pydantic pins this with ==; must track pydantic (#745)",
    # Protobuf is an explicit [full] pin shared by the MTA parser, ONNX
    # Runtime, and the Google API proto stack. Keep every member aligned
    # across uv and the Pi overlay so Dependabot cannot move one side alone.
    "protobuf": "explicit shared runtime pin",
    "gtfs-realtime-bindings": "subway fallback wire-schema binding",
    "google-api-core": "2.31.0+ admits protobuf 7 (<8)",
    "googleapis-common-protos": "1.74.0+ floor protobuf>=4.25.8",
    "proto-plus": "1.28.0+ floor protobuf>=4.25.8",
    "onnxruntime": "1.27.0+ floor protobuf>=4.25.8",
    # CamillaController deliberately uses websocket-client's private _ws
    # handle plus abort()/default-timeout semantics to stop and drain pinned
    # pycamilladsp workers. CI and the Pi must exercise the same version.
    "websocket-client": "private CamillaDSP abort/timeout transport contract",
}


def canon(name: str) -> str:
    """PEP 503 normalization, so constraints-pi.txt's ``pydantic_core``
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


def align(constraints_text: str, lock: dict[str, str]) -> tuple[str, list[str]]:
    """Return the rewritten constraints text and one line per changed pin.

    The original name spelling is preserved; only the version moves.
    """
    changes: list[str] = []
    lines = constraints_text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        match = _PIN_RE.match(line.strip())
        if not match:
            continue
        name, current = match.group(1), match.group(2)
        key = canon(name)
        if key not in CROSS_ECOSYSTEM_PIN_CHAIN:
            continue
        wanted = lock.get(key)
        if wanted is None or wanted == current:
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
    required = set(CROSS_ECOSYSTEM_PIN_CHAIN)
    missing_lock = sorted(required - set(lock))
    missing_constraints = sorted(required - set(constraint_versions(original)))
    if missing_lock:
        print(
            f"pin-chain packages missing from uv.lock: {missing_lock}",
            file=sys.stderr,
        )
    if missing_constraints:
        print(
            "pin-chain packages missing from deploy/constraints-pi.txt: "
            f"{missing_constraints}",
            file=sys.stderr,
        )
    if missing_lock or missing_constraints:
        return 2

    updated, changes = align(original, lock)

    if not changes:
        print("deploy/constraints-pi.txt: pin chain already matches uv.lock")
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
    print(f"deploy/constraints-pi.txt: aligned {len(changes)} pin(s) to uv.lock")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
