"""Endpoint install-tier guardrails.

The Zero 2 W endpoint plan deliberately keeps one JTS package and splits
by install profile instead of creating a second endpoint codebase. These
tests are the cheap walls that make that decision safe.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).parent.parent
INSTALL_SH = REPO_ROOT / "deploy" / "install.sh"


def _run_install_plan(*, profile: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if profile is not None:
        env["JASPER_INSTALL_PROFILE"] = profile
    else:
        env.pop("JASPER_INSTALL_PROFILE", None)
    return subprocess.run(
        ["bash", str(INSTALL_SH), "--dry-run"],
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )


def _run_install_helper(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f"source {shlex.quote(str(INSTALL_SH))} >/dev/null && {script}"],
        capture_output=True,
        text=True,
        timeout=5,
    )


def test_endpoint_import_surface_avoids_brain_only_dependencies():
    """Endpoint-tier modules must import without voice/DSP extras.

    CI has the full environment installed, so simulate the endpoint tier
    with a meta-path blocker that fails if a brain-only dependency is
    imported while loading the modules the endpoint runs.
    """
    modules = [
        "jasper.control.server",
        "jasper.multiroom.config",
        "jasper.multiroom.reconcile",
        "jasper.multiroom.state",
    ]
    blocked = [
        "onnxruntime",
        "openwakeword",
        "scipy",
        "sounddevice",
        "jasper_aec3",
        "google.genai",
        "openai",
        "websockets",
    ]
    code = f"""
import importlib
import importlib.abc
import sys

blocked = {blocked!r}

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        for name in blocked:
            if fullname == name or fullname.startswith(name + "."):
                raise ImportError(f"blocked endpoint-tier dependency: {{fullname}}")
        return None

sys.meta_path.insert(0, Blocker())
for module in {modules!r}:
    importlib.import_module(module)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )

    assert result.returncode == 0, result.stderr


def test_full_install_plan_is_unchanged_when_profile_is_unset():
    """Endpoint work must not leak into the normal speaker dry-run path."""
    unset = _run_install_plan()
    explicit_full = _run_install_plan(profile="full")

    assert unset.returncode == 0, unset.stderr
    assert explicit_full.returncode == 0, explicit_full.stderr
    assert unset.stdout == explicit_full.stdout
    assert unset.stdout.startswith("==> JTS install plan (dry run)\n")


def test_endpoint_install_plan_excludes_brain_build_surfaces():
    result = _run_install_plan(profile="endpoint")

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("==> JTS endpoint install plan (dry run)\n")
    for expected in [
        "Resolve JASPER_INSTALL_PROFILE=endpoint",
        "Persist the tier",
        "Minimal runtime packages",
        "jasper-control",
        "managed JTS snapclient unit",
        "voice/wake/DSP",
    ]:
        assert expected in result.stdout
    for forbidden in [
        "cargo build --release --locked",
        "jasper-fanin Rust daemon",
        "jasper-outputd daemon",
        "shairport-sync source archive",
        "Raspotify/librespot deb",
        "CamillaDSP:",
        "openWakeWord ONNX assets",
        "Enable socket-activated setup wizards",
    ]:
        assert forbidden not in result.stdout


def test_install_profile_marker_is_reused_when_env_is_unset(tmp_path: Path):
    marker = tmp_path / "install_profile"

    write = _run_install_helper(
        f"persist_install_profile endpoint {shlex.quote(str(marker))}"
    )
    read = _run_install_helper(
        f"unset JASPER_INSTALL_PROFILE && resolve_install_profile {shlex.quote(str(marker))}"
    )

    assert write.returncode == 0, write.stderr
    assert read.returncode == 0, read.stderr
    assert read.stdout.strip() == "endpoint"


def test_install_profile_refuses_implicit_tier_change(tmp_path: Path):
    marker = tmp_path / "install_profile"
    setup = _run_install_helper(
        f"persist_install_profile endpoint {shlex.quote(str(marker))}"
    )
    change = _run_install_helper(
        "JASPER_INSTALL_PROFILE=full "
        f"resolve_install_profile {shlex.quote(str(marker))}"
    )
    override = _run_install_helper(
        "JASPER_INSTALL_PROFILE=full JASPER_ACCEPT_INSTALL_PROFILE_CHANGE=1 "
        f"resolve_install_profile {shlex.quote(str(marker))}"
    )

    assert setup.returncode == 0, setup.stderr
    assert change.returncode == 2
    assert "install profile mismatch" in change.stderr
    assert override.returncode == 0, override.stderr
    assert override.stdout.strip() == "full"


def test_live_endpoint_profile_fails_fast_until_installer_lands():
    env = os.environ.copy()
    env["JASPER_INSTALL_PROFILE"] = "endpoint"
    result = subprocess.run(
        ["bash", str(INSTALL_SH)],
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert result.returncode == 2
    assert "endpoint is not implemented for live installs yet" in result.stderr
    assert "this script must be run as root" not in result.stderr
