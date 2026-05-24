"""Coverage for the 5 doctor checks added by Stage 1 of the
memory-resilience plan (docs/HANDOFF-resilience.md).

These are drift detectors — they verify the configs installed by
`migrate_memory_resilience` (in deploy/install.sh) are actually
applied at runtime. The check functions all read kernel
interfaces (/proc, /sys, /sys/fs/cgroup), so we mock those.

The bar: each check should (a) work on Linux where the paths
exist, (b) skip gracefully on dev hosts where they don't,
(c) emit useful detail when drift is found.
"""
from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest

from jasper.cli import doctor


# --- check_memory_headroom -----------------------------------------------


def _mock_meminfo(values: dict[str, int]):
    """Build a mock for `open('/proc/meminfo')` returning lines in
    the kernel's canonical "Field: NNN kB" format."""
    lines = [f"{k}: {v} kB\n" for k, v in values.items()]
    m = MagicMock()
    m.__enter__.return_value = io.StringIO("".join(lines))
    m.__exit__.return_value = None
    return m


def test_memory_headroom_healthy_on_1gb():
    with patch("builtins.open", return_value=_mock_meminfo({
        "MemTotal": 1014768,    # ~991 MB
        "MemAvailable": 300000,  # ~293 MB
    })):
        r = doctor.check_memory_headroom()
    assert r.status == "ok"
    assert "MB available" in r.detail


def test_memory_headroom_warn_below_100mb_on_1gb():
    with patch("builtins.open", return_value=_mock_meminfo({
        "MemTotal": 1014768,
        "MemAvailable": 80000,   # ~78 MB, below 100 MB warn threshold
    })):
        r = doctor.check_memory_headroom()
    assert r.status == "warn"
    assert "tight on 1 GB" in r.detail


def test_memory_headroom_fail_below_30mb_on_1gb():
    with patch("builtins.open", return_value=_mock_meminfo({
        "MemTotal": 1014768,
        "MemAvailable": 20000,  # ~19 MB, below 30 MB fail threshold
    })):
        r = doctor.check_memory_headroom()
    assert r.status == "fail"
    assert "imminent" in r.detail


def test_memory_headroom_does_not_warn_on_2gb_pi():
    """Same MemAvailable in absolute terms, but on a 2 GB box, no warn."""
    with patch("builtins.open", return_value=_mock_meminfo({
        "MemTotal": 2048000,
        "MemAvailable": 80000,   # 78 MB — tight, but on 2 GB it's a smaller fraction concern
    })):
        r = doctor.check_memory_headroom()
    # Stage 1 thresholds only fire on 1 GB hardware (< 1500 MB total).
    assert r.status == "ok"


def test_memory_headroom_handles_meminfo_read_failure():
    with patch("builtins.open", side_effect=OSError("permission denied")):
        r = doctor.check_memory_headroom()
    assert r.status == "warn"


# --- check_zram_size_ratio -----------------------------------------------


def test_zram_size_warns_when_over_60pct_of_ram():
    """Old default of zram = 100% of RAM should warn."""
    fake_read = MagicMock(side_effect=[
        "1014767616",  # /sys/block/zram0/disksize — ~990 MB
    ])
    with patch("pathlib.Path.read_text", fake_read), \
         patch("builtins.open", return_value=_mock_meminfo({
             "MemTotal": 1014768,   # ~991 MB
         })):
        r = doctor.check_zram_size_ratio()
    assert r.status == "warn"
    assert "old default" in r.detail


def test_zram_size_ok_at_50pct():
    fake_read = MagicMock(side_effect=[
        str(520 * 1024 * 1024),  # ~520 MB zram
    ])
    with patch("pathlib.Path.read_text", fake_read), \
         patch("builtins.open", return_value=_mock_meminfo({
             "MemTotal": 1014768,
         })):
        r = doctor.check_zram_size_ratio()
    assert r.status == "ok"


def test_zram_size_no_zram_device():
    """Dev host / older RPi OS — no /sys/block/zram0 — skip cleanly."""
    with patch("pathlib.Path.read_text",
               side_effect=FileNotFoundError):
        r = doctor.check_zram_size_ratio()
    assert r.status == "ok"
    assert "rpi-swap not active" in r.detail


# --- check_mglru_min_ttl -------------------------------------------------


def test_mglru_min_ttl_correct():
    fake_exists = MagicMock(return_value=True)
    fake_read = MagicMock(return_value="1000\n")
    with patch("pathlib.Path.exists", fake_exists), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_mglru_min_ttl()
    assert r.status == "ok"
    assert "1000 ms" in r.detail


def test_mglru_min_ttl_default_zero_warns():
    """The kernel default is 0 (disabled). Warn — Stage 1 should be 1000."""
    fake_exists = MagicMock(return_value=True)
    fake_read = MagicMock(return_value="0\n")
    with patch("pathlib.Path.exists", fake_exists), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_mglru_min_ttl()
    assert r.status == "warn"
    assert "thrash prevention disabled" in r.detail


def test_mglru_min_ttl_no_kernel_support_is_ok():
    """Kernel < 6.1 doesn't have MGLRU — tmpfiles config is a no-op,
    and the doctor should report ok (not warn), so the operator
    knows this is expected on older kernels."""
    fake_exists = MagicMock(return_value=False)
    with patch("pathlib.Path.exists", fake_exists):
        r = doctor.check_mglru_min_ttl()
    assert r.status == "ok"
    assert "lacks MGLRU" in r.detail


# --- check_sysctl_drift --------------------------------------------------


def test_sysctl_drift_skips_when_proc_sys_vm_missing():
    """Dev host without /proc/sys/vm/ shouldn't claim drift."""
    fake_exists = MagicMock(return_value=False)
    with patch("pathlib.Path.exists", fake_exists):
        r = doctor.check_sysctl_drift()
    assert r.status == "ok"
    assert "not Linux" in r.detail


def test_sysctl_drift_detects_swappiness_off_default():
    """Linux with stock vm.swappiness=60 should warn after our 100
    sysctl was supposed to land."""
    fake_exists = MagicMock(return_value=True)
    def fake_read(self):
        # Map by path basename — the function reads each vm.* knob
        # individually. Return "stock default" values that don't match.
        name = str(self).rsplit("/", 1)[-1]
        return {
            "swappiness": "60",          # stock default — should be 100
            "page-cluster": "3",         # stock default — should be 0
            "min_free_kbytes": "16384",  # stock default — should be 32768
            "vfs_cache_pressure": "100", # stock default — should be 200
            "watermark_scale_factor": "10",  # stock default — should be 125
        }.get(name, "?")
    with patch("pathlib.Path.exists", fake_exists), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_sysctl_drift()
    assert r.status == "warn"
    assert "swappiness=60" in r.detail or "vm.swappiness" in r.detail


def test_sysctl_drift_ok_when_values_match():
    """All vm.* values match what /etc/sysctl.d/99-jts-vm.conf
    requested — happy path."""
    fake_exists = MagicMock(return_value=True)
    def fake_read(self):
        name = str(self).rsplit("/", 1)[-1]
        return {
            "swappiness": "100",
            "page-cluster": "0",
            "min_free_kbytes": "32768",
            "vfs_cache_pressure": "200",
            "watermark_scale_factor": "125",
        }.get(name, "?")
    with patch("pathlib.Path.exists", fake_exists), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_sysctl_drift()
    assert r.status == "ok"


# --- check_oom_score_adj -------------------------------------------------


def test_oom_score_adj_all_match():
    """All critical daemons running with expected adj values."""
    # Map unit name → fake PID. _pid_of_unit calls systemctl show
    # which we mock.
    def fake_run(cmd, **kwargs):
        # cmd = ["systemctl", "show", "-p", "MainPID", "--value", "X.service"]
        unit = cmd[5].rsplit(".", 1)[0]
        pid_map = {
            "jasper-camilla": "1001",
            "jasper-aec-bridge": "1002",
            "jasper-control": "1003",
            "jasper-voice": "1004",
            "jasper-mux": "1005",
            "jasper-input": "1006",
        }
        result = MagicMock()
        result.stdout = pid_map.get(unit, "0") + "\n"
        return result

    def fake_read(self):
        # Map /proc/<pid>/oom_score_adj to expected value
        pid_str = str(self).split("/")[2]
        expected_by_pid = {
            "1001": "-900",
            "1002": "-700",
            "1003": "-600",
            "1004": "-500",
            "1005": "-300",
            "1006": "-300",
        }
        return expected_by_pid.get(pid_str, "0") + "\n"

    with patch.object(doctor, "_run", side_effect=fake_run), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_oom_score_adj()
    assert r.status == "ok"
    assert "6 critical daemons protected" in r.detail


def test_oom_score_adj_drift_on_one_daemon():
    """jasper-camilla got started before the new unit landed and
    still has the default (0). Should warn with specifics."""
    def fake_run(cmd, **kwargs):
        unit = cmd[5].rsplit(".", 1)[0]
        result = MagicMock()
        result.stdout = {
            "jasper-camilla": "1001",
            "jasper-aec-bridge": "1002",
            "jasper-control": "1003",
            "jasper-voice": "1004",
            "jasper-mux": "1005",
            "jasper-input": "1006",
        }.get(unit, "0") + "\n"
        return result

    def fake_read(self):
        pid_str = str(self).split("/")[2]
        # jasper-camilla (pid 1001) drifted to 0
        return {
            "1001": "0",
            "1002": "-700",
            "1003": "-600",
            "1004": "-500",
            "1005": "-300",
            "1006": "-300",
        }.get(pid_str, "0") + "\n"

    with patch.object(doctor, "_run", side_effect=fake_run), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_oom_score_adj()
    assert r.status == "warn"
    assert "jasper-camilla=0" in r.detail
    assert "want -900" in r.detail


def test_oom_score_adj_no_systemctl_is_ok():
    """Dev host without systemctl — _pid_of_unit returns None for
    every unit — should report 0 protected / 6 missing, NOT crash."""
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("systemctl not found")

    with patch.object(doctor, "_run", side_effect=fake_run):
        r = doctor.check_oom_score_adj()
    assert r.status == "ok"
    assert "not running" in r.detail
