# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Guard: deploy/constraints-pi.pins must co-resolve with pyproject's
runtime requirements — the #1275 cross-ecosystem drift class.

Background (#1275). ``deploy/constraints-pi.pins`` is a Pi-generated pip
constraints overlay (``scripts/generate-pi-constraints.sh``) that
``install.sh`` passes to pip via ``-c`` on every deploy
(``pip install -c deploy/constraints-pi.pins -e .[full]`` — see
``deploy/lib/install/python-runtime.sh``). It is a SEPARATE dependency
ecosystem from ``uv.lock``/``pyproject.toml``: pip-side dependabot PRs
edit this file, while uv-side PRs edit ``uv.lock``. On 2026-07-11 four
pip-side bumps landed here WITHOUT co-resolving ``uv.lock`` and made
every fresh deploy's pip install a ``ResolutionImpossible``:

* ``#745`` pydantic-core 2.46.4 -> 2.47.0, but ``pydantic==2.13.4``
  hard-pins ``pydantic-core==2.46.4``.
* ``#864`` googleapis-common-protos 1.73.0 -> 1.75.0 and ``#744``
  proto-plus 1.27.1 -> 1.28.0 and ``#746`` onnxruntime 1.26.0 -> 1.27.0,
  all of which floor ``protobuf>=4.25.8``, while ``nyct-gtfs==2.1.0``
  hard-pinned ``protobuf==4.25.3``.

The protobuf chain was resolved together later that day: the subway
fallback moved to ``gtfs-realtime-bindings``, protobuf became an explicit
``[full]`` pin, and the ONNX/Google proto consumers moved with it. The
offline guard therefore tracks the whole cross-ecosystem chain, not only
packages currently held back.

Each PR was green alone; NO CI check pip-resolved the file, so the
unresolvable combination shipped. These three guards close that gap:

1. ``test_pin_matches_uv_lock`` (parametrized) — DETERMINISTIC + OFFLINE.
   Every package pinned in BOTH ``constraints-pi.pins`` and the co-resolved
   ``uv.lock`` (the authoritative resolution CI already validates via ``uv
   sync --locked``) must agree exactly, except the documented ``EXCEPTIONS``
   in ``scripts/align-pi-constraint-pins.py``. This is the guard that fails
   offline on the broken state — no network, no third-party deps.

2. ``test_pip_dry_run_resolves_constraints`` — FAITHFUL + NETWORK.
   Reproduces install.sh's ``pip install -c constraints-pi.pins <full
   runtime reqs>`` with pip's real resolver in ``--dry-run`` mode, so it
   catches ANY conflict, including future classes the hard-pin list in
   guard 1 does not enumerate. Skips cleanly when PyPI is unreachable
   (offline dev), when neither pip nor uv is available, or when uv is
   the only option but has no environment to resolve into (no
   ``$VIRTUAL_ENV`` and no ``.venv/`` at the repo root — the case for a
   plain ``git worktree add`` checkout), so it never spuriously fails;
   it runs for real in CI, which has network and, after ``uv sync``, a
   ``.venv/`` at the repo root.

3. ``test_uv_dry_run_resolves_pi_platform`` — PI-TARGETED + NETWORK.
   Resolves the same versioned requirements for Linux aarch64 / Python
   3.13, including Linux-only markers, so an x86-only wheel cannot make
   the faithful current-runner probe green while the Pi remains broken.
   Shares guard 2's environment-precondition skips (PyPI reachability,
   uv on PATH, uv having a venv to resolve into).
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_CONSTRAINTS = _ROOT / "deploy" / "constraints-pi.pins"
_UV_LOCK = _ROOT / "uv.lock"
_PYPROJECT = _ROOT / "pyproject.toml"

_ALIGN_SCRIPT = _ROOT / "scripts" / "align-pi-constraint-pins.py"
_spec = importlib.util.spec_from_file_location(
    "align_pi_constraint_pins", _ALIGN_SCRIPT
)
assert _spec is not None and _spec.loader is not None
_align = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_align)

# canon/parsing live once in the align script; this module reuses them so
# the guard and the tool that repairs its findings can never disagree about
# scope. Guards 2 and 3 below are what catch anything a parsing bug here
# would miss.
_canon = _align.canon


def _parse_uv_lock() -> dict[str, str]:
    return _align.uv_lock_versions(_UV_LOCK.read_text(encoding="utf-8"))


_CONSTRAINTS_TEXT = _CONSTRAINTS.read_text(encoding="utf-8")
_CONS_VERSIONS = _align.constraint_versions(_CONSTRAINTS_TEXT)
_LOCK_VERSIONS = _parse_uv_lock()
_WALKED_PACKAGES = sorted(_align.walked_packages(_CONSTRAINTS_TEXT, _LOCK_VERSIONS))


def test_walked_package_set_is_not_vacuous() -> None:
    """A parsing regression must not silently empty the walk — pytest would
    then collect zero parametrized cases below and the guard would pass
    while checking nothing."""
    assert len(_WALKED_PACKAGES) > 50


@pytest.mark.parametrize("pkg", _WALKED_PACKAGES)
def test_pin_matches_uv_lock(pkg: str) -> None:
    """The deterministic offline guard for the #1275 drift class.

    Every package pinned in both constraints-pi.pins and the co-resolved
    uv.lock (excluding documented EXCEPTIONS) must agree exactly. A
    mismatch means a pip-side bump landed without co-resolving uv.lock and
    a fresh ``pip install -c deploy/constraints-pi.pins`` will
    ResolutionImpossible.
    """
    assert _CONS_VERSIONS[pkg] == _LOCK_VERSIONS[pkg], (
        f"{pkg}: constraints-pi.pins=={_CONS_VERSIONS[pkg]} but "
        f"uv.lock=={_LOCK_VERSIONS[pkg]} — deploy/constraints-pi.pins has "
        "drifted from the co-resolved uv.lock (see #1275); a fresh deploy's "
        "`pip install -c deploy/constraints-pi.pins -e .[full]` will fail "
        "with ResolutionImpossible.\n"
        "Fix: `python3 scripts/align-pi-constraint-pins.py` co-resolves it, "
        "or add a one-line EXCEPTIONS entry in that script if this "
        "divergence is legitimate."
    )


def test_alignment_command_rewrites_drifted_pins_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constraints_text = (
        "# preserved header\n"
        "alpha==1\n"
        "beta_pkg==1\n"
        "only-in-constraints==1\n"
        "unrelated==7\n"
    )
    lock_text = (
        '[[package]]\nname = "alpha"\nversion = "2"\n'
        '[[package]]\nname = "beta-pkg"\nversion = "1"\n'
        '[[package]]\nname = "only-in-lock"\nversion = "9"\n'
    )
    constraints = tmp_path / "constraints-pi.pins"
    lock = tmp_path / "uv.lock"
    constraints.write_text(constraints_text, encoding="utf-8")
    lock.write_text(lock_text, encoding="utf-8")
    monkeypatch.setattr(_align, "CONSTRAINTS", constraints)
    monkeypatch.setattr(_align, "UV_LOCK", lock)

    assert _align.main(["--check"]) == 1
    assert constraints.read_text(encoding="utf-8") == constraints_text

    assert _align.main([]) == 0
    expected = constraints_text.replace("alpha==1", "alpha==2")
    assert constraints.read_text(encoding="utf-8") == expected

    assert _align.main([]) == 0
    assert constraints.read_text(encoding="utf-8") == expected


def test_alignment_command_leaves_documented_exceptions_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constraints = tmp_path / "constraints-pi.pins"
    lock = tmp_path / "uv.lock"
    constraints.write_text("held-back==1\n", encoding="utf-8")
    lock.write_text(
        '[[package]]\nname = "held-back"\nversion = "2"\n', encoding="utf-8"
    )
    monkeypatch.setattr(_align, "CONSTRAINTS", constraints)
    monkeypatch.setattr(_align, "UV_LOCK", lock)
    monkeypatch.setattr(_align, "EXCEPTIONS", {"held-back": "test fixture"})

    assert _align.main(["--check"]) == 0
    assert constraints.read_text(encoding="utf-8") == "held-back==1\n"


def _pypi_reachable() -> bool:
    try:
        urllib.request.urlopen("https://pypi.org/simple/pip/", timeout=5).close()
        return True
    except OSError:
        # urllib network failures (URLError, timeout, SSL) all subclass
        # OSError; anything else is a real bug, not "offline".
        return False


def _uv_venv_available() -> bool:
    """Whether a bare ``uv pip install`` (no ``--system``/``--python``)
    has a target environment to resolve into: an activated venv
    (``$VIRTUAL_ENV``) or a ``.venv/`` at the repo root. These are the
    two cases this repo's own lanes actually produce — uv's real
    discovery is broader (it also honors ``$CONDA_PREFIX`` and walks up
    from cwd rather than stopping at the repo root), but every lane
    here (``scripts/test-fast``, ``scripts/test-merge``, CI) ``cd``s to
    the repo toplevel before running, and this repo doesn't use conda,
    so the narrower pair below is sufficient. Without either, uv exits
    before resolving anything ("No virtual environment found; run `uv
    venv` ... or pass `--system`") — an environment precondition, not a
    resolution failure, so it belongs in the skip list next to
    ``_pypi_reachable``. A ``git worktree add`` checkout has neither by
    default; the main checkout and CI (post ``uv sync``) have the
    ``.venv/``.
    """
    if os.environ.get("VIRTUAL_ENV"):
        return True
    return (_ROOT / ".venv").is_dir()


def _resolver_cmd() -> list[str] | None:
    """A pip-compatible resolver invocation, or None if none is
    available. Prefer the current interpreter's pip; fall back to ``uv
    pip`` (CI's uv venvs omit pip but ship uv on PATH) when uv has a
    venv to resolve into — see ``_uv_venv_available``."""
    if (
        subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True,
        ).returncode
        == 0
    ):
        return [sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed"]
    from shutil import which

    if which("uv") and _uv_venv_available():
        return ["uv", "pip", "install", "--dry-run"]
    return None


def _constrained_runtime_requirements() -> list[str]:
    """Versioned requirements from install.sh's constrained pip calls.

    Preserve specifiers and markers: passing only bare distribution names
    would let this guard accept a constraints pin that conflicts with
    pyproject.toml. The direct pycamilladsp URL is omitted because it is a
    hash-pinned archive rather than a version-resolution input.
    """
    from packaging.requirements import Requirement

    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    specs = [
        *data["project"]["dependencies"],
        *data["project"]["optional-dependencies"]["full"],
        # install_full_python_runtime installs these under the same
        # constraints before the editable [full] install. Most overlap the
        # full graph; scikit-learn is the important independent input.
        "requests",
        "tqdm",
        "scipy>=1.3,<2",
        "scikit-learn>=1,<2",
    ]
    requirements: list[str] = []
    for spec in specs:
        req = Requirement(spec)
        if req.url is not None:
            continue
        requirements.append(str(req))
    return requirements


def test_resolver_inputs_preserve_pyproject_specifiers_and_markers() -> None:
    """Regression for the bare-name resolver false-positive bug.

    The expected specifiers and markers are read from pyproject.toml rather
    than restated here, so a dependency bump moves one file, not two.
    """
    from packaging.requirements import Requirement

    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    declared = [
        Requirement(spec)
        for spec in (
            *data["project"]["dependencies"],
            *data["project"]["optional-dependencies"]["full"],
        )
    ]
    produced = _constrained_runtime_requirements()
    produced_names = {_canon(Requirement(spec).name) for spec in produced}

    assert any(req.url is not None for req in declared)
    for req in declared:
        if req.url is not None:
            assert _canon(req.name) not in produced_names
        else:
            assert str(req) in produced


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
        pytest.skip(
            "no usable pip/uv resolver in this environment (no pip module, or "
            "uv has no venv to resolve into — e.g. a git worktree with no "
            ".venv of its own)"
        )

    reqs = _constrained_runtime_requirements()
    proc = subprocess.run(
        [*cmd, "-c", str(_CONSTRAINTS), *reqs],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, (
        "pip could not resolve deploy/constraints-pi.pins against pyproject's "
        "[full] runtime requirements — this is exactly what install.sh runs on "
        "every deploy (#1275). Resolver output:\n"
        + (proc.stderr or proc.stdout)[-3000:]
    )


def test_uv_dry_run_resolves_pi_platform() -> None:
    """Resolve the real specs for PiOS Trixie's Python/platform markers.

    The ordinary pip dry-run uses the CI runner's platform. This companion
    probe asks uv to resolve Linux aarch64 + Python 3.13 explicitly, catching
    missing Pi wheels and Linux-only marker conflicts before deploy.
    """
    pytest.importorskip("packaging.requirements")
    if not _pypi_reachable():
        pytest.skip("PyPI unreachable — offline; the offline guard still runs")
    from shutil import which

    if which("uv") is None:
        pytest.skip("uv is required for cross-platform Pi resolution")
    if not _uv_venv_available():
        pytest.skip(
            "uv has no venv to resolve into (no $VIRTUAL_ENV and no .venv/ at "
            "the repo root) — e.g. a git worktree with no .venv of its own"
        )

    proc = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--dry-run",
            "--python-version",
            "3.13",
            "--python-platform",
            "aarch64-manylinux_2_28",
            "-c",
            str(_CONSTRAINTS),
            *_constrained_runtime_requirements(),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, (
        "uv could not resolve the Pi constraints for Linux aarch64 / "
        "Python 3.13. Resolver output:\n" + (proc.stderr or proc.stdout)[-3000:]
    )
