# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Guard: deploy/constraints-pi.txt must co-resolve with pyproject's
runtime requirements — the #1275 cross-ecosystem drift class.

Background (#1275). ``deploy/constraints-pi.txt`` is a Pi-generated pip
constraints overlay (``scripts/generate-pi-constraints.sh``) that
``install.sh`` passes to pip via ``-c`` on every deploy
(``pip install -c deploy/constraints-pi.txt -e .[full]`` — see
``deploy/lib/install/python-runtime.sh``). It is a SEPARATE dependency
ecosystem from ``uv.lock``/``pyproject.toml``: pip-side dependabot PRs
edit this file, while uv-side PRs edit ``uv.lock``. On 2026-07-11 four
pip-side bumps landed here WITHOUT co-resolving ``uv.lock`` and made
every fresh deploy's pip install a ``ResolutionImpossible``:

* ``#745`` pydantic-core 2.46.4 -> 2.47.0, but ``pydantic==2.13.4``
  hard-pins ``pydantic-core==2.46.4``.
* ``#864`` googleapis-common-protos 1.73.0 -> 1.75.0 and ``#744``
  proto-plus 1.27.1 -> 1.28.0 and ``#746`` onnxruntime 1.26.0 -> 1.27.0,
  all of which floor ``protobuf>=4.25.8``, but ``protobuf`` is HELD at
  ``4.25.3`` (``nyct-gtfs==2.1.0`` hard-pins it; the protobuf 7.x bump
  ``#865`` is deliberately held).

Each PR was green alone; NO CI check pip-resolved the file, so the
unresolvable combination shipped. These two guards close that gap:

1. ``test_held_pin_chain_matches_uv_lock`` — DETERMINISTIC + OFFLINE.
   The packages that sit in a cross-ecosystem hard-pin chain must match
   the co-resolved ``uv.lock`` (the authoritative resolution CI already
   validates via ``uv sync --locked``). Their correct version is fixed
   by package metadata that is identical on the arm64/py3.13 Pi and the
   x86 CI runner, so the Pi snapshot must agree. This is the guard that
   fails offline on the broken state — no network, no third-party deps.

2. ``test_pip_dry_run_resolves_constraints`` — FAITHFUL + NETWORK.
   Reproduces install.sh's ``pip install -c constraints-pi.txt <full
   runtime reqs>`` with pip's real resolver in ``--dry-run`` mode, so it
   catches ANY conflict, including future classes the hard-pin list in
   guard 1 does not enumerate. Skips cleanly when PyPI is unreachable
   (offline dev) or when no pip/uv resolver is available (a bare uv
   venv), so it never spuriously fails; it runs in CI, which already has
   network and ``uv`` on PATH for ``uv sync``.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_CONSTRAINTS = _ROOT / "deploy" / "constraints-pi.txt"
_UV_LOCK = _ROOT / "uv.lock"
_PYPROJECT = _ROOT / "pyproject.toml"

_PIN_RE = re.compile(r"^([A-Za-z0-9._-]+)==([^\s;]+)$")


def _canon(name: str) -> str:
    """PEP 503 normalization (lowercase, ``_``/``.`` -> ``-``) so
    constraints-pi.txt's ``pydantic_core`` matches uv.lock's
    ``pydantic-core``."""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _parse_constraints() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in _CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _PIN_RE.match(line)
        if m:
            pins[_canon(m.group(1))] = m.group(2)
    return pins


def _parse_uv_lock() -> dict[str, str]:
    data = tomllib.loads(_UV_LOCK.read_text(encoding="utf-8"))
    return {
        _canon(pkg["name"]): pkg["version"]
        for pkg in data.get("package", [])
        if pkg.get("name") and pkg.get("version")
    }


# Packages whose correct constraints-pi.txt pin is fixed by a
# cross-ecosystem hard pin that pyproject deliberately HOLDS. Each newer
# release breaks resolution against the held pin, so the Pi snapshot must
# track the co-resolved uv.lock. Reasons are load-bearing — verified
# against live PyPI metadata on 2026-07-11 (#1275). Names are PEP 503
# canonical.
_HELD_PIN_CHAIN: dict[str, str] = {
    # pydantic hard-pins pydantic-core to an EXACT ==version
    # (pydantic 2.13.4 -> pydantic-core==2.46.4). Bumping pydantic-core
    # alone (dependabot #745) is ResolutionImpossible.
    "pydantic": "drives the pydantic-core exact pin",
    "pydantic-core": "pydantic pins this with ==; must track pydantic (#745)",
    # protobuf is HELD at 4.25.3: nyct-gtfs==2.1.0 hard-pins
    # protobuf==4.25.3 and the protobuf 7.x bump (#865) is deliberately
    # held. Newer releases of the three packages below floor
    # protobuf>=4.25.8 and cannot co-resolve with the held protobuf.
    "protobuf": "held at 4.25.3 (nyct-gtfs hard pin; protobuf 7.x #865 held)",
    "googleapis-common-protos": "1.74.0+ floor protobuf>=4.25.8 (#864)",
    "proto-plus": "1.28.0+ floor protobuf>=4.25.8 (#744)",
    "onnxruntime": "1.27.0+ floor protobuf>=4.25.8 (#746)",
}


def test_held_pin_chain_matches_uv_lock() -> None:
    """The deterministic offline guard for #1275.

    Every held-pin-chain package must be pinned identically in
    constraints-pi.txt and the co-resolved uv.lock. A mismatch means a
    pip-side bump landed without co-resolving uv.lock and a fresh
    ``pip install -c deploy/constraints-pi.txt`` will ResolutionImpossible.
    """
    cons = _parse_constraints()
    lock = _parse_uv_lock()

    mismatches: list[str] = []
    for pkg, reason in _HELD_PIN_CHAIN.items():
        assert pkg in cons, f"held-pin-chain package {pkg!r} missing from constraints-pi.txt"
        assert pkg in lock, f"held-pin-chain package {pkg!r} missing from uv.lock"
        if cons[pkg] != lock[pkg]:
            mismatches.append(
                f"{pkg}: constraints-pi.txt=={cons[pkg]} but uv.lock=={lock[pkg]}  [{reason}]"
            )

    assert not mismatches, (
        "deploy/constraints-pi.txt has drifted from the co-resolved uv.lock on "
        "held-pin-chain packages — a fresh deploy's "
        "`pip install -c deploy/constraints-pi.txt -e .[full]` will fail with "
        "ResolutionImpossible (see #1275).\n"
        "Fix: regenerate from a coherent Pi via scripts/generate-pi-constraints.sh, "
        "or align these pins to uv.lock (the protobuf 7.x bump #865 stays HELD):\n  "
        + "\n  ".join(mismatches)
    )


def _pypi_reachable() -> bool:
    try:
        urllib.request.urlopen("https://pypi.org/simple/pip/", timeout=5).close()
        return True
    except OSError:
        # urllib network failures (URLError, timeout, SSL) all subclass
        # OSError; anything else is a real bug, not "offline".
        return False


def _resolver_cmd() -> list[str] | None:
    """A pip-compatible resolver invocation, or None if none is
    available. Prefer the current interpreter's pip; fall back to ``uv
    pip`` (CI's uv venvs omit pip but ship uv on PATH)."""
    if (
        subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True,
        ).returncode
        == 0
    ):
        return [sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed"]
    from shutil import which

    if which("uv"):
        return ["uv", "pip", "install", "--dry-run"]
    return None


def _full_extra_requirement_names() -> list[str]:
    """Top-level requirement names install.sh pulls via ``-e .[full]``,
    with env markers evaluated for the current interpreter/platform,
    minus install.sh's two documented special-cases: the camilladsp URL
    dep (a pinned archive, not a version-conflict source) and
    openwakeword (installed ``--no-deps`` because of its tflite dep)."""
    from packaging.requirements import Requirement

    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    names: list[str] = []
    for spec in data["project"]["optional-dependencies"]["full"]:
        if "@" in spec:  # URL dep, e.g. camilladsp @ https://...
            continue
        req = Requirement(spec)
        if _canon(req.name) == "openwakeword":
            continue
        if req.marker is not None and not req.marker.evaluate():
            continue
        names.append(req.name)
    return names


def test_pip_dry_run_resolves_constraints() -> None:
    """The faithful network guard for #1275.

    Reproduce install.sh's constrained resolve with pip's real resolver
    so any cross-ecosystem conflict (not only the enumerated hard-pin
    chain) fails the PR that introduces it. Skips when offline or when no
    resolver is available so it never spuriously fails; the deterministic
    guard above is the offline floor.
    """
    pytest.importorskip("packaging.requirements")
    if not _pypi_reachable():
        pytest.skip("PyPI unreachable — offline; the offline guard still runs")
    cmd = _resolver_cmd()
    if cmd is None:
        pytest.skip("no pip/uv resolver available in this environment")

    reqs = _full_extra_requirement_names()
    proc = subprocess.run(
        [*cmd, "-c", str(_CONSTRAINTS), *reqs],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, (
        "pip could not resolve deploy/constraints-pi.txt against pyproject's "
        "[full] runtime requirements — this is exactly what install.sh runs on "
        "every deploy (#1275). Resolver output:\n"
        + (proc.stderr or proc.stdout)[-3000:]
    )
