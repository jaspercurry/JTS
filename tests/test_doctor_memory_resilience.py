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
    """1 GB Pi: warn threshold is max(100 MB, 10% × 991 MB ≈ 99 MB) = 100 MB."""
    with patch("builtins.open", return_value=_mock_meminfo({
        "MemTotal": 1014768,
        "MemAvailable": 80000,   # ~78 MB, below 100 MB warn threshold
    })):
        r = doctor.check_memory_headroom()
    assert r.status == "warn"
    assert "tight" in r.detail


def test_memory_headroom_fail_below_30mb_on_1gb():
    """1 GB Pi: fail threshold is max(30 MB, 3% × 991 MB ≈ 30 MB) = 30 MB."""
    with patch("builtins.open", return_value=_mock_meminfo({
        "MemTotal": 1014768,
        "MemAvailable": 20000,  # ~19 MB, below 30 MB fail threshold
    })):
        r = doctor.check_memory_headroom()
    assert r.status == "fail"
    assert "imminent" in r.detail


def test_memory_headroom_2gb_pi_uses_proportional_thresholds():
    """2 GB Pi: warn at max(100 MB, 10% × 2 GB = 200 MB) = 200 MB.
    78 MB on a 2 GB Pi IS dangerously tight (3.8% headroom) — the
    old check missed this because it gated on total_mb < 1500."""
    with patch("builtins.open", return_value=_mock_meminfo({
        "MemTotal": 2097152,    # 2 GB
        "MemAvailable": 80000,   # 78 MB — way below 200 MB warn
    })):
        r = doctor.check_memory_headroom()
    # Below 3% (60 MB) is fail; 78 MB is in warn territory.
    # 78 MB is > 60 MB so it's warn, not fail.
    assert r.status == "warn"


def test_memory_headroom_8gb_pi_uses_proportional_thresholds():
    """8 GB Pi: warn at 800 MB (10%), fail at 240 MB (3%). 500 MB
    available is tight relative to the box, so warn fires."""
    with patch("builtins.open", return_value=_mock_meminfo({
        "MemTotal": 8388608,    # 8 GB
        "MemAvailable": 500000,  # ~488 MB — below 800 MB warn threshold
    })):
        r = doctor.check_memory_headroom()
    assert r.status == "warn"
    # And NOT below fail (240 MB)
    assert "imminent" not in r.detail


def test_memory_headroom_8gb_pi_with_healthy_available():
    """8 GB Pi with 2 GB available is healthy (25%)."""
    with patch("builtins.open", return_value=_mock_meminfo({
        "MemTotal": 8388608,
        "MemAvailable": 2097152,  # 2 GB available
    })):
        r = doctor.check_memory_headroom()
    assert r.status == "ok"


def test_memory_headroom_16gb_pi_fail_threshold_scales():
    """16 GB Pi: fail threshold is 3% = 480 MB. 400 MB available
    on a 16 GB box means something is wrong — should fail, not just warn."""
    with patch("builtins.open", return_value=_mock_meminfo({
        "MemTotal": 16777216,
        "MemAvailable": 400000,  # ~390 MB — below fail threshold of 480 MB
    })):
        r = doctor.check_memory_headroom()
    assert r.status == "fail"


def test_memory_headroom_handles_meminfo_read_failure():
    with patch("builtins.open", side_effect=OSError("permission denied")):
        r = doctor.check_memory_headroom()
    assert r.status == "warn"


# --- check_zram_size_ratio -----------------------------------------------


def test_zram_size_warns_when_over_60pct_of_ram():
    """Old default of zram = 100% of RAM should warn AND mention
    that reboot is required (rpi-swap is a generator, not a service)."""
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
    assert "reboot" in r.detail   # don't tell the operator to re-run
                                  # install.sh — they need to reboot


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


# Helper: the doctor now reads expected values from
# /etc/sysctl.d/99-jts-vm.conf, so tests need to mock both that
# file's read AND the /proc/sys/vm/<key> reads.

_FAKE_INSTALLED_CONF = """\
# JTS sysctl conf as written by install.sh
vm.swappiness = 100
vm.page-cluster = 0
vm.watermark_scale_factor = 125
vm.watermark_boost_factor = 0
vm.min_free_kbytes = 20296
vm.dirty_background_ratio = 2
vm.dirty_ratio = 10
vm.vfs_cache_pressure = 200
vm.overcommit_memory = 0
"""


def _make_sysctl_drift_mocks(installed_conf: str, live_values: dict[str, str]):
    """Returns (path_exists_fn, path_read_text_fn) suitable for the
    `patch("pathlib.Path.exists" / "read_text", ...)` pattern. The
    exists fn returns True for both the conf file AND for any
    /proc/sys/vm/<key> path that the live_values dict mentions."""
    def fake_exists(self):
        s = str(self)
        if s == "/etc/sysctl.d/99-jts-vm.conf":
            return True
        if s.startswith("/proc/sys/vm/"):
            key = s.rsplit("/", 1)[-1]
            return key in live_values
        return False

    def fake_read(self):
        s = str(self)
        if s == "/etc/sysctl.d/99-jts-vm.conf":
            return installed_conf
        if s.startswith("/proc/sys/vm/"):
            key = s.rsplit("/", 1)[-1]
            return live_values.get(key, "?") + "\n"
        return ""

    return fake_exists, fake_read


def test_sysctl_drift_warns_when_jts_conf_missing():
    """Dev host or pre-install: /etc/sysctl.d/99-jts-vm.conf
    doesn't exist — should warn (not silently report ok)."""
    with patch("pathlib.Path.exists", lambda self: False):
        r = doctor.check_sysctl_drift()
    assert r.status == "warn"
    assert "missing" in r.detail or "re-run install.sh" in r.detail


def test_sysctl_drift_detects_swappiness_off_default():
    """Conf says 100, live is 60 (stock default) — should warn."""
    fake_exists, fake_read = _make_sysctl_drift_mocks(
        _FAKE_INSTALLED_CONF,
        {  # stock defaults — none match the conf
            "swappiness": "60",
            "page-cluster": "3",
            "min_free_kbytes": "16384",
            "vfs_cache_pressure": "100",
            "watermark_scale_factor": "10",
            "watermark_boost_factor": "15000",
            "dirty_background_ratio": "10",
            "dirty_ratio": "20",
            "overcommit_memory": "0",
        },
    )
    with patch("pathlib.Path.exists", fake_exists), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_sysctl_drift()
    assert r.status == "warn"
    assert "swappiness=60" in r.detail or "vm.swappiness" in r.detail


def test_sysctl_drift_ok_when_values_match():
    """Conf and live agree — happy path."""
    fake_exists, fake_read = _make_sysctl_drift_mocks(
        _FAKE_INSTALLED_CONF,
        {
            "swappiness": "100",
            "page-cluster": "0",
            "watermark_scale_factor": "125",
            "watermark_boost_factor": "0",
            "min_free_kbytes": "20296",  # matches conf's RAM-aware value
            "dirty_background_ratio": "2",
            "dirty_ratio": "10",
            "vfs_cache_pressure": "200",
            "overcommit_memory": "0",
        },
    )
    with patch("pathlib.Path.exists", fake_exists), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_sysctl_drift()
    assert r.status == "ok"


def test_sysctl_drift_uses_installed_min_free_kbytes_value():
    """The whole point of PR1.7: RAM-aware min_free_kbytes shouldn't
    trigger drift just because it's not the old hardcoded 32768.
    A 1 GB Pi installs ~20296 kB; a 4 GB Pi installs ~81920. Both
    are 'correct' for their hardware. Doctor reads the installed
    conf to know what to expect."""
    # Simulate a 4 GB Pi install where min_free_kbytes was computed
    # to 81920 (2% of 4 GB).
    conf_4gb = _FAKE_INSTALLED_CONF.replace(
        "vm.min_free_kbytes = 20296",
        "vm.min_free_kbytes = 81920",
    )
    fake_exists, fake_read = _make_sysctl_drift_mocks(
        conf_4gb,
        {
            "swappiness": "100",
            "page-cluster": "0",
            "watermark_scale_factor": "125",
            "watermark_boost_factor": "0",
            "min_free_kbytes": "81920",  # matches the 4 GB-specific value
            "dirty_background_ratio": "2",
            "dirty_ratio": "10",
            "vfs_cache_pressure": "200",
            "overcommit_memory": "0",
        },
    )
    with patch("pathlib.Path.exists", fake_exists), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_sysctl_drift()
    assert r.status == "ok"


def test_sysctl_drift_skips_unsubstituted_template_placeholder():
    """Defensive: if install.sh's sed step failed, the conf file would
    contain the literal '__VM_MIN_FREE_KBYTES__' placeholder. Doctor
    must skip that line rather than report drift comparing a number
    to a placeholder string."""
    conf_broken = _FAKE_INSTALLED_CONF.replace(
        "vm.min_free_kbytes = 20296",
        "vm.min_free_kbytes = __VM_MIN_FREE_KBYTES__",
    )
    fake_exists, fake_read = _make_sysctl_drift_mocks(
        conf_broken,
        {
            "swappiness": "100",
            "page-cluster": "0",
            "watermark_scale_factor": "125",
            "watermark_boost_factor": "0",
            "min_free_kbytes": "16384",  # whatever the kernel default is
            "dirty_background_ratio": "2",
            "dirty_ratio": "10",
            "vfs_cache_pressure": "200",
            "overcommit_memory": "0",
        },
    )
    with patch("pathlib.Path.exists", fake_exists), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_sysctl_drift()
    # All non-placeholder values match → ok. The placeholder line
    # is correctly skipped (would have produced a confusing drift
    # message otherwise).
    assert r.status == "ok"


# --- check_oom_score_adj -------------------------------------------------


_PID_MAP = {
    "jasper-camilla": "1001",
    "jasper-aec-bridge": "1002",
    "jasper-control": "1003",
    "jasper-voice": "1004",
    "jasper-mux": "1005",
    "jasper-input": "1006",
}

_EXPECTED_CONFIG = {
    "jasper-camilla": "-900",
    "jasper-aec-bridge": "-700",
    "jasper-control": "-600",
    "jasper-voice": "-500",
    "jasper-mux": "-300",
    "jasper-input": "-300",
}


def _make_oom_run(pid_map, config_map):
    """Build a `_run` mock for check_oom_score_adj's two systemctl
    calls: `-p MainPID` and `-p OOMScoreAdjust`. Each returns the
    value from the appropriate map for the unit named in the cmd."""
    def fake_run(cmd, **kwargs):
        # cmd = ["systemctl", "show", "-p", <prop>, "--value", "X.service"]
        prop = cmd[3]
        unit = cmd[5].rsplit(".", 1)[0]
        result = MagicMock()
        if prop == "MainPID":
            result.stdout = pid_map.get(unit, "0") + "\n"
        elif prop == "OOMScoreAdjust":
            result.stdout = config_map.get(unit, "0") + "\n"
        else:
            result.stdout = "\n"
        return result
    return fake_run


def test_oom_score_adj_all_match():
    """All critical daemons running with both unit-file and live
    values matching expected."""
    def fake_read(self):
        # /proc/<pid>/oom_score_adj — return expected for each pid
        pid_str = str(self).split("/")[2]
        return {
            "1001": "-900", "1002": "-700", "1003": "-600",
            "1004": "-500", "1005": "-300", "1006": "-300",
        }.get(pid_str, "0") + "\n"

    with patch.object(doctor, "_run",
                      side_effect=_make_oom_run(_PID_MAP, _EXPECTED_CONFIG)), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_oom_score_adj()
    assert r.status == "ok"
    assert "6 critical daemons protected" in r.detail


def test_oom_score_adj_live_drift_only():
    """jasper-camilla was started before the new unit landed but
    the unit file IS correct — live-only drift, fixable by restart."""
    def fake_read(self):
        pid_str = str(self).split("/")[2]
        # jasper-camilla (pid 1001) drifted live to 0; unit file correct
        return {
            "1001": "0", "1002": "-700", "1003": "-600",
            "1004": "-500", "1005": "-300", "1006": "-300",
        }.get(pid_str, "0") + "\n"

    with patch.object(doctor, "_run",
                      side_effect=_make_oom_run(_PID_MAP, _EXPECTED_CONFIG)), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_oom_score_adj()
    assert r.status == "warn"
    assert "live-process drift" in r.detail
    assert "jasper-camilla live=0" in r.detail
    assert "next restart" in r.detail  # actionable hint


def test_oom_score_adj_unit_file_drift_is_more_serious():
    """The .service file itself doesn't have OOMScoreAdjust= (manual
    edit / install.sh hasn't been re-run after a regression). This
    is more serious than live-only drift because next restart won't
    fix it — the unit file is the source of truth."""
    # Live processes happen to show the correct value (the running
    # processes were started before the unit-file got broken).
    def fake_read(self):
        pid_str = str(self).split("/")[2]
        return {
            "1001": "-900", "1002": "-700", "1003": "-600",
            "1004": "-500", "1005": "-300", "1006": "-300",
        }.get(pid_str, "0") + "\n"

    # But the unit file says 0 for jasper-camilla — a regression
    # we'd otherwise miss until next restart.
    drifted_config = dict(_EXPECTED_CONFIG)
    drifted_config["jasper-camilla"] = "0"
    with patch.object(doctor, "_run",
                      side_effect=_make_oom_run(_PID_MAP, drifted_config)), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_oom_score_adj()
    assert r.status == "warn"
    assert "UNIT FILE drift" in r.detail
    assert "jasper-camilla unit=0" in r.detail
    assert "next restart won't fix" in r.detail


def test_oom_score_adj_unit_drift_takes_precedence_over_live_drift():
    """If BOTH kinds of drift exist, surface the unit-file one
    (it's the more dangerous shape)."""
    def fake_read(self):
        pid_str = str(self).split("/")[2]
        return {"1001": "0"}.get(pid_str, "-900") + "\n"  # live also wrong

    drifted_config = dict(_EXPECTED_CONFIG)
    drifted_config["jasper-camilla"] = "0"
    with patch.object(doctor, "_run",
                      side_effect=_make_oom_run(_PID_MAP, drifted_config)), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_oom_score_adj()
    assert r.status == "warn"
    assert "UNIT FILE drift" in r.detail  # not "live-process drift"


def test_oom_score_adj_no_systemctl_is_ok():
    """Dev host without systemctl — _pid_of_unit returns None for
    every unit — should report 0 protected / 6 missing, NOT crash."""
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("systemctl not found")

    with patch.object(doctor, "_run", side_effect=fake_run):
        r = doctor.check_oom_score_adj()
    assert r.status == "ok"
    assert "not running" in r.detail
