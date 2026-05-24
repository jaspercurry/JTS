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


def _zram_test_mocks(zram_bytes: int, rpi_swap_installed: bool = True):
    """Mock both Path.read_text() (for /sys/block/zram0/disksize) and
    Path.exists() (for /etc/rpi/swap.conf)."""
    def fake_read(self):
        s = str(self)
        if s == "/sys/block/zram0/disksize":
            return str(zram_bytes)
        raise FileNotFoundError(s)
    def fake_exists(self):
        s = str(self)
        if s == "/etc/rpi/swap.conf":
            return rpi_swap_installed
        return False
    return fake_read, fake_exists


def test_zram_size_warns_when_over_60pct_of_ram_with_rpi_swap():
    """rpi-swap installed + zram > 60% of RAM → actionable warn:
    reboot to apply the JTS drop-in."""
    fake_read, fake_exists = _zram_test_mocks(
        zram_bytes=1014767616,  # ~990 MB zram
        rpi_swap_installed=True,
    )
    with patch("pathlib.Path.read_text", fake_read), \
         patch("pathlib.Path.exists", fake_exists), \
         patch("builtins.open", return_value=_mock_meminfo({
             "MemTotal": 1014768,
         })):
        r = doctor.check_zram_size_ratio()
    assert r.status == "warn"
    assert "old default" in r.detail
    assert "reboot" in r.detail


def test_zram_size_skips_when_rpi_swap_not_installed():
    """Bookworm / non-Trixie / forked-onto-another-distro: rpi-swap
    isn't installed, so JTS's drop-in is inert — no actionable fix
    from the operator's side. Skip with ok rather than warn forever."""
    fake_read, fake_exists = _zram_test_mocks(
        zram_bytes=1014767616,  # ~990 MB zram, 99% of RAM
        rpi_swap_installed=False,
    )
    with patch("pathlib.Path.read_text", fake_read), \
         patch("pathlib.Path.exists", fake_exists), \
         patch("builtins.open", return_value=_mock_meminfo({
             "MemTotal": 1014768,
         })):
        r = doctor.check_zram_size_ratio()
    # NOT warn — no actionable resolution. Operator can't fix from
    # this side without changing distros.
    assert r.status == "ok"
    assert "rpi-swap not installed" in r.detail or "different zram package" in r.detail


def test_zram_size_ok_at_50pct():
    fake_read, fake_exists = _zram_test_mocks(
        zram_bytes=520 * 1024 * 1024,  # ~520 MB zram
        rpi_swap_installed=True,
    )
    with patch("pathlib.Path.read_text", fake_read), \
         patch("pathlib.Path.exists", fake_exists), \
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


def test_sysctl_drift_warns_on_unsubstituted_template_placeholder():
    """Defensive: if install.sh's sed step failed, the conf file
    contains the literal '__VM_MIN_FREE_KBYTES__' placeholder. The
    kernel will silently use its default for that knob (NOT what we
    wanted). Doctor must surface this — silent "ok" would hide a
    real config-broken state."""
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
            "min_free_kbytes": "16384",
            "dirty_background_ratio": "2",
            "dirty_ratio": "10",
            "vfs_cache_pressure": "200",
            "overcommit_memory": "0",
        },
    )
    with patch("pathlib.Path.exists", fake_exists), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_sysctl_drift()
    assert r.status == "warn"
    assert "placeholder" in r.detail
    assert "min_free_kbytes" in r.detail
    # And the actionable hint
    assert "re-run install.sh" in r.detail


# --- check_oom_score_adj -------------------------------------------------


_PID_MAP = {
    "jasper-camilla": "1001",
    "jasper-aec-bridge": "1002",
    "jasper-control": "1003",
    "jasper-voice": "1004",
    "jasper-mux": "1005",
    "jasper-input": "1006",
    "ssh": "1007",
}

_EXPECTED_CONFIG = {
    "jasper-camilla": "-900",
    "jasper-aec-bridge": "-700",
    "jasper-control": "-600",
    "jasper-voice": "-500",
    "jasper-mux": "-300",
    "jasper-input": "-300",
    "ssh": "-1000",
}


def _make_oom_run(pid_map, config_map):
    """Build a `_run` mock for check_oom_score_adj's two BATCHED
    systemctl calls. Real wire format: when called with multiple
    units AND --value, systemctl uses `\\n\\n` (blank line) between
    values — NOT a single newline. Reproduce that here so tests
    catch regressions in the parser.

      `systemctl show -p MainPID --value u1 u2 u3` →
        "1234\\n\\n5678\\n\\n9012\\n"
    """
    def fake_run(cmd, **kwargs):
        prop = cmd[3]
        units = [c.rsplit(".", 1)[0] for c in cmd[5:]]
        result = MagicMock()
        if prop == "MainPID":
            values = [pid_map.get(u, "0") for u in units]
        elif prop == "OOMScoreAdjust":
            values = [config_map.get(u, "0") for u in units]
        else:
            values = []
        # Real systemctl emits \n\n between values + trailing \n.
        result.stdout = "\n\n".join(values) + "\n" if values else "\n"
        return result
    return fake_run


_LIVE_OK = {
    "1001": "-900", "1002": "-700", "1003": "-600",
    "1004": "-500", "1005": "-300", "1006": "-300",
    "1007": "-1000",   # ssh (Debian default)
}


def test_oom_score_adj_all_match():
    """All critical daemons running with both unit-file and live
    values matching expected. Includes ssh (Debian openssh-server
    default of -1000) as the recovery-path lifeline."""
    def fake_read(self):
        pid_str = str(self).split("/")[2]
        return _LIVE_OK.get(pid_str, "0") + "\n"

    with patch.object(doctor, "_run",
                      side_effect=_make_oom_run(_PID_MAP, _EXPECTED_CONFIG)), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_oom_score_adj()
    assert r.status == "ok"
    assert "7 critical daemons protected" in r.detail


def test_oom_score_adj_warns_if_sshd_drifts():
    """sshd dropped to default 0 (e.g. Debian openssh-server packaging
    changed). This is exactly the resilience gap we want to surface —
    if sshd is OOM-killable, the operator loses their recovery path
    during an OOM event."""
    def fake_read(self):
        pid_str = str(self).split("/")[2]
        live = dict(_LIVE_OK)
        live["1007"] = "0"  # sshd drifted to default
        return live.get(pid_str, "0") + "\n"

    # Also reflect the drift in the unit file's configured value, so
    # this surfaces as the more-serious "UNIT FILE drift" message.
    drifted_config = dict(_EXPECTED_CONFIG)
    drifted_config["ssh"] = "0"
    with patch.object(doctor, "_run",
                      side_effect=_make_oom_run(_PID_MAP, drifted_config)), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_oom_score_adj()
    assert r.status == "warn"
    assert "ssh" in r.detail


def test_oom_score_adj_live_drift_only():
    """jasper-camilla was started before the new unit landed but
    the unit file IS correct — live-only drift, fixable by restart."""
    def fake_read(self):
        pid_str = str(self).split("/")[2]
        live = dict(_LIVE_OK)
        live["1001"] = "0"  # jasper-camilla drifted live to 0
        return live.get(pid_str, "0") + "\n"

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
        return _LIVE_OK.get(pid_str, "0") + "\n"

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


def test_systemctl_show_property_parses_double_newline_separator():
    """Regression test for the wire-format bug discovered on jts2.local
    post-cleanup-deploy (2026-05-24): when called with multiple units
    AND --value, systemctl emits values separated by \\n\\n (blank
    line), NOT a single \\n.

    Pre-fix: parser split on \\n and got 2N-1 elements for N units,
    triggered the "len mismatch → return None" fallback, and
    check_oom_score_adj reported "systemctl unavailable — skipped
    (not Linux?)" on a real Pi. Bad UX.

    This test pins the parser to the actual wire format so the
    failure cannot recur silently. Verified directly via
    `systemctl show -p MainPID --value u1 u2 | cat -A` on the Pi."""
    # Mock that emits the real systemctl format
    def fake_run(cmd, **kwargs):
        result = MagicMock()
        # Real format: value1\n\nvalue2\n\nvalue3\n
        result.stdout = "1001\n\n1002\n\n1003\n"
        return result

    with patch.object(doctor, "_run", side_effect=fake_run):
        result = doctor._systemctl_show_property(
            "MainPID", ["unit-a", "unit-b", "unit-c"],
        )
    # Must return 3 values (one per unit), NOT None / 5 / 6.
    assert result == ["1001", "1002", "1003"]


def test_systemctl_show_property_handles_single_unit():
    """Single unit still works — separator is just `\\n` then."""
    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.stdout = "1234\n"
        return result

    with patch.object(doctor, "_run", side_effect=fake_run):
        result = doctor._systemctl_show_property("MainPID", ["unit-a"])
    assert result == ["1234"]


def test_systemctl_show_property_handles_empty_values():
    """All units returned empty (e.g. all not-running) — still
    produces N entries, NOT len mismatch."""
    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.stdout = "\n\n\n\n\n"  # 3 empty values
        return result

    with patch.object(doctor, "_run", side_effect=fake_run):
        result = doctor._systemctl_show_property(
            "MainPID", ["unit-a", "unit-b", "unit-c"],
        )
    # All-empty is unusual but should still be 3 entries.
    assert result is not None
    assert len(result) == 3


# --- check_start_limit_action (T5.1) ------------------------------------


def _make_start_limit_action_run(actions: dict[str, str]):
    """Build a _run mock for `systemctl show -p StartLimitAction
    --value <unit>.service`. Returns the value from `actions`."""
    def fake_run(cmd, **kwargs):
        # cmd = ["systemctl", "show", "-p", "StartLimitAction",
        #        "--value", "X.service"]
        unit = cmd[5].rsplit(".", 1)[0]
        result = MagicMock()
        result.stdout = actions.get(unit, "none") + "\n"
        return result
    return fake_run


def test_start_limit_action_all_set_to_reboot():
    """T5.1 happy path: all 4 critical units have StartLimitAction=reboot."""
    actions = {
        "jasper-camilla": "reboot",
        "jasper-aec-bridge": "reboot",
        "jasper-voice": "reboot",
        "jasper-control": "reboot",
    }
    with patch.object(doctor, "_run",
                      side_effect=_make_start_limit_action_run(actions)):
        r = doctor.check_start_limit_action()
    assert r.status == "ok"
    assert "4 critical daemons" in r.detail


def test_start_limit_action_drift_one_unit_lost_directive():
    """A Debian/RPi-OS update edited jasper-control's unit and removed
    the directive — should warn and name the unit."""
    actions = {
        "jasper-camilla": "reboot",
        "jasper-aec-bridge": "reboot",
        "jasper-voice": "reboot",
        "jasper-control": "none",   # drifted to default
    }
    with patch.object(doctor, "_run",
                      side_effect=_make_start_limit_action_run(actions)):
        r = doctor.check_start_limit_action()
    assert r.status == "warn"
    assert "T5.1" in r.detail
    assert "jasper-control" in r.detail
    assert "want reboot" in r.detail


def test_start_limit_action_drift_wrong_action():
    """Someone set StartLimitAction=reboot-force on jasper-voice — wrong
    on a 1 GB Pi (dirty zram pages would skip sync)."""
    actions = {
        "jasper-camilla": "reboot",
        "jasper-aec-bridge": "reboot",
        "jasper-voice": "reboot-force",   # wrong shape
        "jasper-control": "reboot",
    }
    with patch.object(doctor, "_run",
                      side_effect=_make_start_limit_action_run(actions)):
        r = doctor.check_start_limit_action()
    assert r.status == "warn"
    assert "jasper-voice=reboot-force" in r.detail


def test_start_limit_action_skips_on_dev_host():
    """Dev host without systemctl — should skip cleanly rather than
    crash or false-warn."""
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("systemctl not found")

    with patch.object(doctor, "_run", side_effect=fake_run):
        r = doctor.check_start_limit_action()
    assert r.status == "ok"
    assert "skipped" in r.detail


def test_oom_score_adj_no_systemctl_is_ok():
    """Dev host without systemctl — the batched `_systemctl_show_property`
    returns None, and check_oom_score_adj fast-fails to a skip
    (rather than reporting N "not running" units). The post-cleanup
    behavior is cleaner: one "skipped — not Linux?" instead of per-
    unit noise."""
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("systemctl not found")

    with patch.object(doctor, "_run", side_effect=fake_run):
        r = doctor.check_oom_score_adj()
    assert r.status == "ok"
    assert "systemctl unavailable" in r.detail
