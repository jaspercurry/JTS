# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-tier detection + arch preflight guards (Workstream D).

`detect_hardware_tier` / `_hardware_tier_arch_supported` /
`hardware_tier_preflight` are small bash helpers in
deploy/install.sh that name the box's hardware tier (RAM/CPU/arch) once,
up front, and fail fast on an unsupported architecture before any
mutation. They sit on the deploy path and are easy to regress, so the
SKU matrix is pinned here with synthetic-/proc injection — the same
no-hardware pattern tests/test_install_profile_tiers.py uses for the
Zero-2-W profile default and the low-memory Cargo build.

Design note: docs/install-hardware-tier-and-staleness.md.
"""
from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
INSTALL_SH = REPO_ROOT / "deploy" / "install.sh"


def _run_install_helper(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f"source {shlex.quote(str(INSTALL_SH))} >/dev/null && {script}"],
        capture_output=True,
        text=True,
        timeout=5,
    )


def _detect(
    *,
    mem_kb: int | None,
    cpus: str = "4",
    arch: str = "aarch64",
    tmp_path: Path,
) -> dict[str, str]:
    """Run detect_hardware_tier with injected RAM/CPU/arch; parse its line."""
    env = f"JASPER_HW_NPROC={shlex.quote(cpus)} JASPER_HW_ARCH={shlex.quote(arch)}"
    if mem_kb is not None:
        meminfo = tmp_path / "meminfo"
        meminfo.write_text(f"MemTotal:       {mem_kb} kB\n")
        env += f" JASPER_HW_MEMINFO_FILE={shlex.quote(str(meminfo))}"
    else:
        env += f" JASPER_HW_MEMINFO_FILE={shlex.quote(str(tmp_path / 'missing'))}"
    r = _run_install_helper(f"{env} detect_hardware_tier")
    assert r.returncode == 0, r.stderr
    line = r.stdout.strip()
    fields = dict(re.findall(r"(\w+)=(\S+)", line))
    assert {"ram_mb", "cpus", "arch", "tier"} <= fields.keys(), line
    return fields


# ---------- tier classification across the SKU range ----------


@pytest.mark.parametrize(
    "mem_kb,expected_tier",
    [
        (524288, "low"),        # 512 MB Zero 2 W
        (786431, "low"),        # just under the 768 MB Rust threshold
        (786432, "constrained"),  # exactly 768 MB
        (1014784, "constrained"),  # ~991 MB — the jts2 box that OOM'd
        (2097151, "constrained"),  # just under 2 GB
        (2097152, "standard"),  # exactly 2 GB ("2GB recommended" Pi 5)
        (8388608, "standard"),  # 8 GB Pi 5
        (16777216, "standard"),  # 16 GB Pi 5
    ],
)
def test_tier_classification_by_ram(mem_kb, expected_tier, tmp_path):
    fields = _detect(mem_kb=mem_kb, tmp_path=tmp_path)
    assert fields["tier"] == expected_tier, fields
    assert fields["ram_mb"] == str(mem_kb // 1024)


def test_tier_reports_cpus_and_arch(tmp_path):
    fields = _detect(mem_kb=1014784, cpus="4", arch="aarch64", tmp_path=tmp_path)
    assert fields["cpus"] == "4"
    assert fields["arch"] == "aarch64"


def test_unreadable_meminfo_is_unknown_not_a_crash(tmp_path):
    """Fail-soft: a missing/garbage meminfo reports tier=unknown, ram_mb=0,
    and still exits 0 — the reporter must never abort the install."""
    fields = _detect(mem_kb=None, tmp_path=tmp_path)
    assert fields["tier"] == "unknown"
    assert fields["ram_mb"] == "0"


def test_garbage_meminfo_value_is_unknown(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       not-a-number kB\n")
    r = _run_install_helper(
        f"JASPER_HW_ARCH=aarch64 JASPER_HW_MEMINFO_FILE={shlex.quote(str(meminfo))} "
        "detect_hardware_tier"
    )
    assert r.returncode == 0, r.stderr
    fields = dict(re.findall(r"(\w+)=(\S+)", r.stdout.strip()))
    assert fields["tier"] == "unknown"
    assert fields["ram_mb"] == "0"


# ---------- arch support predicate ----------


@pytest.mark.parametrize("arch", ["aarch64", "arm64"])
def test_arch_guard_accepts_64bit_arm(arch):
    r = _run_install_helper(f"JASPER_HW_ARCH={arch} _hardware_tier_arch_supported")
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("arch", ["armv7l", "armhf", "x86_64", "i686", "unknown"])
def test_arch_guard_rejects_non_64bit_arm(arch):
    r = _run_install_helper(f"JASPER_HW_ARCH={arch} _hardware_tier_arch_supported")
    assert r.returncode == 1


# ---------- preflight: log + fail-fast (overridable) ----------


def _run_preflight(arch: str, *, allow: bool, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       1014784 kB\n")
    env = (
        f"JASPER_HW_ARCH={shlex.quote(arch)} "
        f"JASPER_HW_NPROC=4 "
        f"JASPER_HW_MEMINFO_FILE={shlex.quote(str(meminfo))}"
    )
    if allow:
        env += " JASPER_ALLOW_UNSUPPORTED_ARCH=1"
    return _run_install_helper(f"{env} hardware_tier_preflight")


def test_preflight_passes_on_arm64_and_logs_tier(tmp_path):
    r = _run_preflight("aarch64", allow=False, tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "hardware tier:" in r.stdout
    assert "tier=constrained" in r.stdout


def test_preflight_aborts_on_unsupported_arch(tmp_path):
    r = _run_preflight("armv7l", allow=False, tmp_path=tmp_path)
    assert r.returncode == 2
    assert "unsupported architecture" in r.stderr
    assert "64-bit" in r.stderr
    # Must still have logged the tier before aborting, so the transcript
    # shows what it saw.
    assert "hardware tier:" in r.stdout


def test_preflight_override_proceeds_with_warning(tmp_path):
    r = _run_preflight("armv7l", allow=True, tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "JASPER_ALLOW_UNSUPPORTED_ARCH=1 set" in r.stderr


# ---------- the dry-run plan surfaces the tier (both profiles) ----------


@pytest.mark.parametrize("profile", [None, "full", "streambox"])
def test_dry_run_plan_shows_hardware_tier(profile):
    import os

    env = os.environ.copy()
    if profile is None:
        env.pop("JASPER_INSTALL_PROFILE", None)
    else:
        env["JASPER_INSTALL_PROFILE"] = profile
    r = subprocess.run(
        ["bash", str(INSTALL_SH), "--dry-run"],
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )
    assert r.returncode == 0, r.stderr
    assert "Hardware tier (detected on this host):" in r.stdout


def test_dry_run_does_not_enforce_arch_guard():
    """The arch guard must run ONLY in the real-install path (after the
    --dry-run early return), never during dry-run. This is load-bearing:
    the plan/drift-guard tests run `install.sh --dry-run` on x86_64 CI,
    where uname -m is an unsupported arch — if the guard fired in dry-run,
    every dry-run test would abort with rc=2. Inject an unsupported arch
    AND --dry-run and assert the plan still prints cleanly."""
    import os

    env = os.environ.copy()
    env.pop("JASPER_INSTALL_PROFILE", None)
    env["JASPER_HW_ARCH"] = "armv7l"
    r = subprocess.run(
        ["bash", str(INSTALL_SH), "--dry-run"],
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )
    assert r.returncode == 0, r.stderr
    assert "==> JTS install plan (dry run)" in r.stdout
    assert "unsupported architecture" not in r.stderr
