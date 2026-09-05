# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The memory-pressure reader and the zram target it shares with the installer.

``memory_policy`` is the one home for both facts (ADR-0233 rule 1): the
system-metrics sampler and jasper-doctor read pressure through it, and the
doctor's zram bound derives from the same percent the shell installer sizes to.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from jasper.memory_policy import (
    ZRAM_OVERSIZE_MARGIN_PERCENT,
    ZRAM_TARGET_PERCENT,
    ZRAM_WARN_PERCENT,
    memory_pressure,
    zram_usage,
)

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "deploy" / "lib" / "install" / "memory-resilience.sh"
SWAP_CONF = ROOT / "deploy" / "rpi-swap" / "50-jts.conf"

_PRESSURE = (
    "some avg10=0.00 avg60=12.34 avg300=1.20 total=987654\n"
    "full avg10=0.00 avg60=3.21 avg300=0.40 total=123456\n"
)
_VMSTAT = "nr_free_pages 12345\noom_kill 3\npgmajfault 42\n"
# A 1 GB Pi: MemTotal sits a few percent under the nominal 1024 MB.
_MEMINFO = "MemTotal:        1014768 kB\nMemFree:          123456 kB\n"


def _write(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def test_memory_pressure_parses_the_real_kernel_shapes(tmp_path):
    out = memory_pressure(
        pressure_path=_write(tmp_path, "memory", _PRESSURE),
        vmstat_path=_write(tmp_path, "vmstat", _VMSTAT),
    )
    assert out.psi_some_avg60 == 12.34
    assert out.oom_kills == 3


@pytest.mark.parametrize(
    "pressure_text, vmstat_text",
    [
        (None, None),
        ("", "nr_free_pages 12345\n"),
    ],
    ids=["files-absent", "counters-absent"],
)
def test_memory_pressure_reports_no_reading_not_a_calm_zero(
    tmp_path, pressure_text, vmstat_text,
):
    """A kernel without PSI (no ``psi=1``) or without the OOM counter must
    read as None; zero means "measured, and there is none"."""
    if pressure_text is None:
        paths = {
            "pressure_path": str(tmp_path / "nope"),
            "vmstat_path": str(tmp_path / "nope"),
        }
    else:
        paths = {
            "pressure_path": _write(tmp_path, "memory", pressure_text),
            "vmstat_path": _write(tmp_path, "vmstat", vmstat_text or ""),
        }
    out = memory_pressure(**paths)
    assert out.psi_some_avg60 is None
    assert out.oom_kills is None


def test_zram_target_percent_is_one_number_across_shell_and_python():
    """The shell installer cannot import Python, so its literal and the
    generator drop-in's RamMultiplier are pinned to the Python owner here."""
    shell = re.search(r"^_ZRAM_TARGET_PERCENT=(\d+)$", INSTALL_SH.read_text(), re.M)
    assert shell and int(shell.group(1)) == ZRAM_TARGET_PERCENT

    multiplier = re.search(r"^RamMultiplier=([\d.]+)$", SWAP_CONF.read_text(), re.M)
    assert multiplier and float(multiplier.group(1)) * 100 == ZRAM_TARGET_PERCENT


def test_zram_warn_bound_sits_above_the_installer_target():
    """A margin of zero would warn on every box: rpi-swap sizes from MemTotal,
    which is a few percent below installed RAM."""
    assert 0 < ZRAM_OVERSIZE_MARGIN_PERCENT
    assert ZRAM_TARGET_PERCENT + ZRAM_OVERSIZE_MARGIN_PERCENT < 100


@pytest.mark.parametrize(
    "disksize, meminfo, expected",
    [
        (519561216, _MEMINFO, (519561216, 1039122432, 50)),
        # rpi-swap sizes from MemTotal, so a real box lands a little above the
        # nominal target — still inside the margin, never a warn.
        (545259520, _MEMINFO, (545259520, 1039122432, 52)),
        # The old zramswap 100%-of-RAM default this check exists to catch.
        (1014767616, _MEMINFO, (1014767616, 1039122432, 97)),
        # Device present but not yet sized, and a MemTotal that would not
        # read: both are "no ratio", and the caller tells them apart by field.
        (0, _MEMINFO, (0, 1039122432, 0)),
        (519561216, "MemFree: 123456 kB\n", (519561216, 0, 0)),
    ],
    ids=["at-target", "inside-margin", "old-100pct-default", "unsized", "no-memtotal"],
)
def test_zram_usage_derives_the_percent_the_doctor_bands_against(
    tmp_path, disksize, meminfo, expected,
):
    usage = zram_usage(
        disksize_path=_write(tmp_path, "disksize", f"{disksize}\n"),
        meminfo_path=_write(tmp_path, "meminfo", meminfo),
    )
    assert usage == expected


def test_zram_usage_is_none_where_there_is_no_zram_device(tmp_path):
    """A dev laptop and an older RPi OS on dphys-swapfile have no zram0 at
    all — distinct from a device present with nothing sized into it."""
    assert zram_usage(
        disksize_path=str(tmp_path / "nope"),
        meminfo_path=_write(tmp_path, "meminfo", _MEMINFO),
    ) is None


def test_zram_warn_percent_is_the_derived_band():
    assert ZRAM_WARN_PERCENT == ZRAM_TARGET_PERCENT + ZRAM_OVERSIZE_MARGIN_PERCENT
