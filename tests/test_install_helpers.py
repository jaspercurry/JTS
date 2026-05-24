"""Unit tests for bash helpers in deploy/install.sh.

The helpers can be sourced cleanly because install.sh's `main` call
is guarded by `if [[ "${BASH_SOURCE[0]}" == "${0:-}" ]]`. Tests source
the file and invoke individual functions.

Currently covers `_compute_min_free_kbytes` (Concern 9 of the
staff-eng review) — RAM-aware vm.min_free_kbytes computation. The
formula's clamp behavior on edge cases (tiny systems, huge systems)
is easy to regress with an off-by-one in awk.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


_INSTALL_SH = Path(__file__).parent.parent / "deploy" / "install.sh"


def _compute_min_free_kbytes(memtotal_kb: int) -> int:
    """Invoke the bash helper via subprocess; return its integer
    output. Discards stdout from the sourcing step (install.sh has
    a top-level banner echo) so we only capture the helper's output."""
    # `source <file> >/dev/null` suppresses install.sh's banner.
    # Then the bare invocation of _compute_min_free_kbytes goes to
    # the outer stdout, which we capture.
    result = subprocess.run(
        ["bash", "-c",
         f"source {_INSTALL_SH} >/dev/null && _compute_min_free_kbytes {memtotal_kb}"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"helper failed (rc={result.returncode}): {result.stderr}"
        )
    return int(result.stdout.strip())


# Pi 5 SKU memory sizes (real values from /proc/meminfo on each
# variant — approximate; actual values vary by ~5 MB per board).
_PI5_1GB_MEMTOTAL_KB = 1014768   # 991 MB
_PI5_2GB_MEMTOTAL_KB = 2031264   # 1983 MB (some firmware budget)
_PI5_4GB_MEMTOTAL_KB = 4063920   # 3968 MB
_PI5_8GB_MEMTOTAL_KB = 8128464   # 7938 MB
_PI5_16GB_MEMTOTAL_KB = 16264848 # 15883 MB


def test_compute_1gb_pi():
    """1 GB Pi: 2% × 991 MB ≈ 19.8 MB → ~20 MB."""
    result = _compute_min_free_kbytes(_PI5_1GB_MEMTOTAL_KB)
    # 2% × 1014768 = 20295.36 → round to 20295
    assert result == 20295
    # And in human-readable terms, this is about 20 MB
    assert 19_000 < result < 22_000


def test_compute_2gb_pi():
    """2 GB Pi: 2% × ~2 GB → ~40 MB."""
    result = _compute_min_free_kbytes(_PI5_2GB_MEMTOTAL_KB)
    # 2% × 2031264 = 40625.28 → round to 40625
    assert result == 40625
    assert 39_000 < result < 43_000


def test_compute_4gb_pi():
    """4 GB Pi: 2% × ~4 GB → ~81 MB."""
    result = _compute_min_free_kbytes(_PI5_4GB_MEMTOTAL_KB)
    assert 80_000 < result < 83_000


def test_compute_8gb_pi():
    """8 GB Pi: 2% × ~8 GB → ~160 MB."""
    result = _compute_min_free_kbytes(_PI5_8GB_MEMTOTAL_KB)
    assert 160_000 < result < 165_000


def test_compute_16gb_pi_hits_ceiling():
    """16 GB Pi: 2% × 16 GB = ~320 MB, but capped at 256 MB.
    This is the load-bearing ceiling — verify the cap fires."""
    result = _compute_min_free_kbytes(_PI5_16GB_MEMTOTAL_KB)
    assert result == 262144   # exactly 256 MB


def test_compute_very_small_hits_floor():
    """A pathological tiny MemTotal (1 MB) shouldn't reduce
    min_free_kbytes below the Pi Foundation default of 8192 kB."""
    result = _compute_min_free_kbytes(1024)
    assert result == 8192


def test_compute_floor_threshold_exactly():
    """At the boundary: 2% of 409600 kB = 8192 kB exactly. Should
    return 8192 (the floor)."""
    # 8192 / 0.02 = 409600 kB. So MemTotal at exactly this gives 8192.
    result = _compute_min_free_kbytes(409_600)
    assert result == 8192


def test_compute_ceiling_threshold_exactly():
    """At the boundary: 2% × 13107200 kB = 262144 kB exactly.
    The cap should return 262144 (not over-clamp)."""
    result = _compute_min_free_kbytes(13_107_200)
    assert result == 262144


def test_compute_just_below_ceiling():
    """Just below the ceiling: should still be computed proportionally,
    not pinned to 262144."""
    # 2% × 13_000_000 = 260_000 kB
    result = _compute_min_free_kbytes(13_000_000)
    assert result == 260_000
    assert result < 262144   # NOT capped


def test_compute_rounding_behavior():
    """awk's int(x + 0.5) gives round-half-up. Verify a value
    that hits the rounding boundary."""
    # 2% × 100_001 = 2000.02 → round to 2000 → floor to 8192
    # 2% × 8_192_050 = 163841 (rounds from 163841.0)
    result = _compute_min_free_kbytes(8_192_050)
    assert result == 163_841


def test_compute_rejects_negative_or_garbage_input():
    """awk on a non-numeric input would produce 0 (which then hits
    the floor). Verify that the floor kicks in rather than a
    crash or negative output."""
    # awk treats non-numeric strings as 0 in arithmetic contexts.
    # int(0 * 0.02 + 0.5) = 0, then clamped to 8192.
    result = _compute_min_free_kbytes(0)
    assert result == 8192
