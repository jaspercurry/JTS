# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Coverage for the 5 doctor checks added by Stage 1 of the
memory-resilience plan (docs/HANDOFF-resilience.md).

These are drift detectors — they verify the configs installed by
`migrate_memory_resilience` (in deploy/lib/install/memory-resilience.sh,
sourced by deploy/install.sh) are actually
applied at runtime. The check functions all read kernel
interfaces (/proc, /sys, /sys/fs/cgroup), so we mock those.

The bar: each check should (a) work on Linux where the paths
exist, (b) skip gracefully on dev hosts where they don't,
(c) emit useful detail when drift is found.
"""
from __future__ import annotations

import io
import os
from unittest.mock import MagicMock, patch


from jasper.cli import doctor
from jasper.conversation_history import (
    CAPTURE_ENABLED_ENV,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_RETENTION_MAX_ROWS,
    ConversationStore,
    ConversationTurn,
    DB_PATH_ENV,
    RETENTION_DAYS_ENV,
    make_turn_id,
)


# --- check_ram -----------------------------------------------------------


def test_ram_warns_on_small_full_install():
    with patch("builtins.open", return_value=_mock_meminfo({
        "MemTotal": 426076,  # ~416 MB: too small for the full brain stack
    })), patch(
        "jasper.cli.doctor.memory.read_install_profile", return_value="full",
    ):
        r = doctor.check_ram()

    assert r.status == "warn"
    assert "recommend 2GB Pi 5" in r.detail


def test_ram_ok_on_small_streambox_board():
    # Streambox is the deliberately-light tier a Zero 2 W resolves to, so the
    # full-speaker "recommend 2GB Pi 5" board-size warn is a false positive
    # there. Live pressure is still covered SKU-agnostically by
    # check_memory_headroom.
    with patch("builtins.open", return_value=_mock_meminfo({
        "MemTotal": 426076,  # ~416 MB: a Zero 2 W board running streambox
    })), patch(
        "jasper.cli.doctor.memory.read_install_profile",
        return_value="streambox",
    ):
        r = doctor.check_ram()

    assert r.status == "ok"
    assert "streambox tier" in r.detail
    assert "recommend 2GB Pi 5" not in r.detail


def test_ram_warn_survives_install_profile_read_failure():
    # A marker-read glitch must NOT silently suppress the warn on a real
    # full speaker — _install_profile_is_streambox fails toward False.
    with patch("builtins.open", return_value=_mock_meminfo({
        "MemTotal": 426076,
    })), patch(
        "jasper.cli.doctor.memory.read_install_profile",
        side_effect=OSError("marker unreadable"),
    ):
        r = doctor.check_ram()

    assert r.status == "warn"
    assert "recommend 2GB Pi 5" in r.detail


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
    "jasper-outputd": "1001",
    "jasper-camilla": "1002",
    "jasper-fanin": "1003",
    "jasper-aec-bridge": "1004",
    "jasper-control": "1005",
    "jasper-voice": "1006",
    "jasper-mux": "1007",
    "jasper-input": "1008",
    "ssh": "1009",
}

_EXPECTED_CONFIG = {
    "jasper-outputd": "-950",
    "jasper-camilla": "-900",
    "jasper-fanin": "-800",
    "jasper-aec-bridge": "-700",
    "jasper-control": "-600",
    "jasper-voice": "-500",
    "jasper-mux": "-300",
    "jasper-input": "-300",
    "ssh": "-250",
}


def _make_oom_run(pid_map, config_map, load_map=None):
    """Build a `_run` mock for check_oom_score_adj's BATCHED systemctl
    calls (LoadState, MainPID, OOMScoreAdjust). Real wire format: when
    called with multiple units AND --value, systemctl uses `\\n\\n`
    (blank line) between values — NOT a single newline. Reproduce that
    here so tests catch regressions in the parser.

      `systemctl show -p MainPID --value u1 u2 u3` →
        "1234\\n\\n5678\\n\\n9012\\n"

    `load_map` overrides LoadState per unit (default: every unit
    "loaded", i.e. installed). Pass "not-found"/"masked" to simulate a
    profile that doesn't install a unit (e.g. streambox + voice/AEC).
    """
    def fake_run(cmd, **kwargs):
        prop = cmd[3]
        units = [c.rsplit(".", 1)[0] for c in cmd[5:]]
        result = MagicMock()
        if prop == "LoadState":
            values = [(load_map or {}).get(u, "loaded") for u in units]
        elif prop == "MainPID":
            values = [pid_map.get(u, "0") for u in units]
        elif prop == "OOMScoreAdjust":
            values = [config_map.get(u, "0") for u in units]
        else:
            values = []
        # Real systemctl emits \n\n between values + trailing \n.
        result.stdout = "\n\n".join(values) + "\n" if values else "\n"
        return result
    return fake_run


def test_oom_score_adj_skips_units_not_installed_on_streambox():
    """A streambox does not install the voice/AEC stack; those units are
    LoadState=not-found and must NOT be reported as OOM drift (the
    full-speaker EXPECTED map applies only to units this profile runs)."""
    absent = {
        "jasper-voice": "not-found",
        "jasper-aec-bridge": "not-found",
        "jasper-input": "not-found",
    }
    # Absent units default to 0 (would be drift if not skipped); present
    # units match expected and have no live process drift.
    config = dict(_EXPECTED_CONFIG)
    pid_map = dict(_PID_MAP)
    for u in absent:
        config[u] = "0"
        pid_map[u] = "0"

    def fake_read(self):
        pid_str = str(self).split("/")[2]
        return _LIVE_OK.get(pid_str, "0") + "\n"

    with patch.object(doctor._shared, "_run",
                      side_effect=_make_oom_run(pid_map, config, absent)), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_oom_score_adj()

    assert r.status == "ok", r.detail
    for unit in absent:
        assert unit not in r.detail
    # The remaining 6 installed daemons are still verified.
    assert "6 critical daemons protected" in r.detail


def test_oom_score_adj_warns_on_present_drift_with_others_absent():
    """Mixed profile: the installed-unit filter must not swallow REAL drift
    on a present unit. Streambox (voice/AEC/input absent) + a present,
    config-drifted jasper-mux → warn naming only the present unit."""
    absent = {
        "jasper-voice": "not-found",
        "jasper-aec-bridge": "not-found",
        "jasper-input": "not-found",
    }
    config = dict(_EXPECTED_CONFIG)
    pid_map = dict(_PID_MAP)
    for u in absent:
        config[u] = "0"
        pid_map[u] = "0"
    config["jasper-mux"] = "0"   # present unit drifted (want -300)

    def fake_read(self):
        pid_str = str(self).split("/")[2]
        return _LIVE_OK.get(pid_str, "0") + "\n"

    with patch.object(doctor._shared, "_run",
                      side_effect=_make_oom_run(pid_map, config, absent)), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_oom_score_adj()

    assert r.status == "warn"
    assert "jasper-mux unit=0 (want -300)" in r.detail
    for unit in absent:
        assert unit not in r.detail


_LIVE_OK = {
    "1001": "-950", "1002": "-900", "1003": "-800",
    "1004": "-700", "1005": "-600", "1006": "-500",
    "1007": "-300", "1008": "-300",
    "1009": "-250",   # ssh recovery path, still killable
}


def test_oom_score_adj_all_match():
    """All critical daemons running with both unit-file and live
    values matching expected. Includes ssh as the recovery path, but
    with moderate protection so SSH-launched diagnostics are killable."""
    def fake_read(self):
        pid_str = str(self).split("/")[2]
        return _LIVE_OK.get(pid_str, "0") + "\n"

    with patch.object(doctor._shared, "_run",
                      side_effect=_make_oom_run(_PID_MAP, _EXPECTED_CONFIG)), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_oom_score_adj()
    assert r.status == "ok"
    assert "9 critical daemons protected" in r.detail


def test_oom_score_adj_warns_if_sshd_drifts():
    """sshd dropped to default 0. This is still worth surfacing because
    the configured recovery-path bias was lost, even though sshd is
    intentionally no longer immortal."""
    def fake_read(self):
        pid_str = str(self).split("/")[2]
        live = dict(_LIVE_OK)
        live["1009"] = "0"  # sshd drifted to default
        return live.get(pid_str, "0") + "\n"

    # Also reflect the drift in the unit file's configured value, so
    # this surfaces as the more-serious "UNIT FILE drift" message.
    drifted_config = dict(_EXPECTED_CONFIG)
    drifted_config["ssh"] = "0"
    with patch.object(doctor._shared, "_run",
                      side_effect=_make_oom_run(_PID_MAP, drifted_config)), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_oom_score_adj()
    assert r.status == "warn"
    assert "ssh" in r.detail


def test_oom_score_adj_ignores_openssh_listener_self_protection():
    """OpenSSH may keep the root listener at -1000 while sessions inherit
    the unit's -250. If the unit file is correct, do not warn on the
    listener's live value."""
    def fake_read(self):
        pid_str = str(self).split("/")[2]
        live = dict(_LIVE_OK)
        live["1009"] = "-1000"
        return live.get(pid_str, "0") + "\n"

    with patch.object(doctor._shared, "_run",
                      side_effect=_make_oom_run(_PID_MAP, _EXPECTED_CONFIG)), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_oom_score_adj()

    assert r.status == "ok"
    assert "ssh live=-1000" not in r.detail


def test_oom_score_adj_live_drift_only():
    """jasper-camilla was started before the new unit landed but
    the unit file IS correct — live-only drift, fixable by restart."""
    def fake_read(self):
        pid_str = str(self).split("/")[2]
        live = dict(_LIVE_OK)
        live["1002"] = "0"  # jasper-camilla drifted live to 0
        return live.get(pid_str, "0") + "\n"

    with patch.object(doctor._shared, "_run",
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
    with patch.object(doctor._shared, "_run",
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
        live = dict(_LIVE_OK)
        live["1002"] = "0"  # live also wrong for jasper-camilla
        return live.get(pid_str, "0") + "\n"

    drifted_config = dict(_EXPECTED_CONFIG)
    drifted_config["jasper-camilla"] = "0"
    with patch.object(doctor._shared, "_run",
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

    with patch.object(doctor._shared, "_run", side_effect=fake_run):
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

    with patch.object(doctor._shared, "_run", side_effect=fake_run):
        result = doctor._systemctl_show_property("MainPID", ["unit-a"])
    assert result == ["1234"]


def test_systemctl_show_property_handles_empty_values():
    """All units returned empty (e.g. all not-running) — still
    produces N entries, NOT len mismatch."""
    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.stdout = "\n\n\n\n\n"  # 3 empty values
        return result

    with patch.object(doctor._shared, "_run", side_effect=fake_run):
        result = doctor._systemctl_show_property(
            "MainPID", ["unit-a", "unit-b", "unit-c"],
        )
    # All-empty is unusual but should still be 3 entries.
    assert result is not None
    assert len(result) == 3


# --- check_start_limit_action (critical restart policy) ------------------


def _make_start_limit_action_run(
    actions: dict[str, str],
    load_map=None,
    on_failure: dict[str, str] | None = None,
):
    """Build a `_run` mock for check_start_limit_action's BATCHED
    systemctl calls — `-p LoadState` (the installed-unit filter) and
    policy properties such as `-p StartLimitAction` / `-p OnFailure` —
    each over `u1 u2 ...`. They all go through
    `_systemctl_show_property` (i.e. `_shared._run`), so tests patch that
    one namespace. `load_map` overrides LoadState per unit (default:
    every unit "loaded")."""
    def fake_run(cmd, **kwargs):
        prop = cmd[3]
        units = [c.rsplit(".", 1)[0] for c in cmd[5:]]
        if prop == "LoadState":
            values = [(load_map or {}).get(u, "loaded") for u in units]
        elif prop == "StartLimitAction":
            values = [actions.get(u, "none") for u in units]
        else:  # OnFailure
            values = [(on_failure or {}).get(u, "") for u in units]
        result = MagicMock()
        result.stdout = "\n\n".join(values) + "\n" if values else "\n"
        return result
    return fake_run


def test_start_limit_action_policy_all_set():
    """Happy path: reboot ladder units reboot; Camilla uses recovery."""
    actions = {
        "jasper-outputd": "reboot",
        "jasper-camilla": "none",
        "jasper-aec-bridge": "reboot",
        "jasper-voice": "reboot",
        "jasper-control": "reboot",
    }
    on_failure = {"jasper-camilla": "jasper-camilla-recover.service"}
    with patch.object(doctor._shared, "_run",
                      side_effect=_make_start_limit_action_run(
                          actions,
                          on_failure=on_failure,
                      )):
        r = doctor.check_start_limit_action()
    assert r.status == "ok"
    assert "5 installed critical daemons" in r.detail


def test_start_limit_action_drift_one_unit_lost_directive():
    """A Debian/RPi-OS update edited jasper-control's unit and removed
    the directive — should warn and name the unit."""
    actions = {
        "jasper-outputd": "reboot",
        "jasper-camilla": "none",
        "jasper-aec-bridge": "reboot",
        "jasper-voice": "reboot",
        "jasper-control": "none",   # drifted to default
    }
    on_failure = {"jasper-camilla": "jasper-camilla-recover.service"}
    with patch.object(doctor._shared, "_run",
                      side_effect=_make_start_limit_action_run(
                          actions,
                          on_failure=on_failure,
                      )):
        r = doctor.check_start_limit_action()
    assert r.status == "warn"
    assert "critical restart policy drift" in r.detail
    assert "jasper-control" in r.detail
    assert "want reboot" in r.detail


def test_start_limit_action_drift_wrong_action():
    """Someone set StartLimitAction=reboot-force on jasper-voice — wrong
    on a 1 GB Pi (dirty zram pages would skip sync)."""
    actions = {
        "jasper-outputd": "reboot",
        "jasper-camilla": "none",
        "jasper-aec-bridge": "reboot",
        "jasper-voice": "reboot-force",   # wrong shape
        "jasper-control": "reboot",
    }
    on_failure = {"jasper-camilla": "jasper-camilla-recover.service"}
    with patch.object(doctor._shared, "_run",
                      side_effect=_make_start_limit_action_run(
                          actions,
                          on_failure=on_failure,
                      )):
        r = doctor.check_start_limit_action()
    assert r.status == "warn"
    assert "jasper-voice=reboot-force" in r.detail


def test_start_limit_action_warns_when_camilla_recovery_handler_drifts():
    """Camilla must stay out of the raw reboot ladder AND keep OnFailure."""
    actions = {
        "jasper-outputd": "reboot",
        "jasper-camilla": "none",
        "jasper-aec-bridge": "reboot",
        "jasper-voice": "reboot",
        "jasper-control": "reboot",
    }
    with patch.object(doctor._shared, "_run",
                      side_effect=_make_start_limit_action_run(actions)):
        r = doctor.check_start_limit_action()
    assert r.status == "warn"
    assert "jasper-camilla OnFailure=none" in r.detail
    assert "jasper-camilla-recover.service" in r.detail


def test_start_limit_action_warns_when_camilla_reverts_to_raw_reboot():
    """The JTS5 failure class needs forensics/recovery, not blind reboot."""
    actions = {
        "jasper-outputd": "reboot",
        "jasper-camilla": "reboot",
        "jasper-aec-bridge": "reboot",
        "jasper-voice": "reboot",
        "jasper-control": "reboot",
    }
    on_failure = {"jasper-camilla": "jasper-camilla-recover.service"}
    with patch.object(doctor._shared, "_run",
                      side_effect=_make_start_limit_action_run(
                          actions,
                          on_failure=on_failure,
                      )):
        r = doctor.check_start_limit_action()
    assert r.status == "warn"
    assert "jasper-camilla=reboot (want none)" in r.detail


def test_start_limit_action_skips_units_not_installed_on_streambox():
    """A streambox does not install jasper-voice/jasper-aec-bridge; those
    units are LoadState=not-found and must NOT count as escalation drift
    even though they report StartLimitAction=none."""
    actions = {
        "jasper-outputd": "reboot",
        "jasper-camilla": "none",
        "jasper-control": "reboot",
        "jasper-aec-bridge": "none",   # absent → must be ignored
        "jasper-voice": "none",        # absent → must be ignored
    }
    load_map = {"jasper-aec-bridge": "not-found", "jasper-voice": "not-found"}
    on_failure = {"jasper-camilla": "jasper-camilla-recover.service"}
    with patch.object(doctor._shared, "_run",
                      side_effect=_make_start_limit_action_run(
                          actions,
                          load_map,
                          on_failure=on_failure,
                      )):
        r = doctor.check_start_limit_action()
    assert r.status == "ok", r.detail
    assert "jasper-voice" not in r.detail
    assert "jasper-aec-bridge" not in r.detail
    assert "3 installed critical daemons" in r.detail


def test_start_limit_action_warns_on_present_drift_with_others_absent():
    """Mixed profile: the installed-unit filter must not swallow REAL drift
    on a present unit. Streambox (voice/AEC absent) + a present, drifted
    jasper-control → warn naming only the present unit."""
    actions = {
        "jasper-outputd": "reboot",
        "jasper-camilla": "none",
        "jasper-control": "none",      # present + drifted → must warn
        "jasper-aec-bridge": "none",   # absent → ignored
        "jasper-voice": "none",        # absent → ignored
    }
    load_map = {"jasper-aec-bridge": "not-found", "jasper-voice": "not-found"}
    on_failure = {"jasper-camilla": "jasper-camilla-recover.service"}
    with patch.object(doctor._shared, "_run",
                      side_effect=_make_start_limit_action_run(
                          actions,
                          load_map,
                          on_failure=on_failure,
                      )):
        r = doctor.check_start_limit_action()
    assert r.status == "warn"
    assert "jasper-control=none" in r.detail
    assert "jasper-voice" not in r.detail
    assert "jasper-aec-bridge" not in r.detail


def test_start_limit_action_skips_on_dev_host():
    """Dev host without systemctl — should skip cleanly rather than crash
    or false-warn. The installed-unit LoadState filter is the first
    systemctl call; its failure short-circuits to a clean skip."""
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("systemctl not found")

    with patch.object(doctor._shared, "_run", side_effect=fake_run):
        r = doctor.check_start_limit_action()
    assert r.status == "ok"
    assert "skipped" in r.detail


def test_start_limit_action_skips_when_directive_read_degrades():
    """Safety contract: if the StartLimitAction batch read returns a
    malformed shape (so _systemctl_show_property → None) AFTER the
    installed-unit filter already succeeded, degrade to a clean skip —
    never a false 'ok' that would hide real escalation drift."""
    def fake_run(cmd, **kwargs):
        units = [c.rsplit(".", 1)[0] for c in cmd[5:]]
        result = MagicMock()
        if cmd[3] == "LoadState":
            result.stdout = "\n\n".join("loaded" for _ in units) + "\n"
        elif cmd[3] == "StartLimitAction":
            # StartLimitAction: one fewer value → length mismatch → None
            result.stdout = "\n\n".join(["reboot"] * (len(units) - 1)) + "\n"
        else:
            result.stdout = "\n\n".join(
                "jasper-camilla-recover.service" for _ in units
            ) + "\n"
        return result

    with patch.object(doctor._shared, "_run", side_effect=fake_run):
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

    with patch.object(doctor._shared, "_run", side_effect=fake_run):
        r = doctor.check_oom_score_adj()
    assert r.status == "ok"
    assert "systemctl unavailable" in r.detail


# --- Stage 2 audio-slice checks ------------------------------------------


def test_cgroup_memory_enabled_when_controller_listed():
    """memory cgroup is on → check passes."""
    fake_read = MagicMock(return_value="cpu io memory pids\n")
    fake_exists = MagicMock(return_value=True)
    with patch("pathlib.Path.exists", fake_exists), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_cgroup_memory_enabled()
    assert r.status == "ok"
    assert "controller enabled" in r.detail


def test_cgroup_memory_disabled_fails_loudly():
    """memory NOT in cgroup.controllers → audio-slice MemorySwapMax=0
    is a no-op. This is the exact silent-failure trap we want the
    doctor to surface, so it's FAIL (not warn) — the audio protection
    is gone."""
    fake_read = MagicMock(return_value="cpu io pids\n")  # no memory
    fake_exists = MagicMock(return_value=True)
    with patch("pathlib.Path.exists", fake_exists), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_cgroup_memory_enabled()
    assert r.status == "fail"
    assert "NOT enabled" in r.detail
    assert "Reboot" in r.detail


def test_cgroup_memory_skips_on_dev_host():
    """No /sys/fs/cgroup → not Linux, skip cleanly."""
    fake_exists = MagicMock(return_value=False)
    with patch("pathlib.Path.exists", fake_exists):
        r = doctor.check_cgroup_memory_enabled()
    assert r.status == "ok"
    assert "not Linux" in r.detail


def test_audio_path_no_swap_happy_path():
    """All audio-path daemons running with VmSwap=0 (or very low):
    happy path = ok."""
    def fake_run(cmd, **kwargs):
        unit = cmd[5].rsplit(".", 1)[0]
        pid_map = {
            "jasper-fanin": "2001",
            "jasper-camilla": "2002",
            "jasper-aec-bridge": "2003",
            "shairport-sync": "2004",
            "librespot": "2005",
            "bluealsa-aplay": "2006",
        }
        result = MagicMock()
        result.stdout = pid_map.get(unit, "0") + "\n"
        return result

    def fake_read(self):
        # All audio daemons have VmSwap=0 (or tiny transient)
        return (
            "Name:\tfake\n"
            "VmRSS:\t100000 kB\n"
            "VmSwap:\t0 kB\n"
        )

    with patch.object(doctor._shared, "_run", side_effect=fake_run), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_audio_path_no_swap()
    assert r.status == "ok"
    assert "swap-free" in r.detail


def test_audio_path_no_swap_warns_on_42mb_swap():
    """Reproduce the 2026-05-24 failure-mode signature: aec-bridge
    with 42 MB of VmSwap. Should warn loudly with the daemon name
    + amount."""
    def fake_run(cmd, **kwargs):
        unit = cmd[5].rsplit(".", 1)[0]
        pid_map = {
            "jasper-fanin": "2001",
            "jasper-camilla": "2002",
            "jasper-aec-bridge": "2003",
            "shairport-sync": "2004",
            "librespot": "2005",
            "bluealsa-aplay": "2006",
        }
        result = MagicMock()
        result.stdout = pid_map.get(unit, "0") + "\n"
        return result

    def fake_read(self):
        pid_str = str(self).split("/")[2]
        # jasper-aec-bridge (pid 2003) has 42 MB swapped (the
        # 2026-05-24 signature). Others are clean.
        if pid_str == "2003":
            return "Name:\tfoo\nVmRSS:\t100000 kB\nVmSwap:\t43056 kB\n"
        return "Name:\tfoo\nVmRSS:\t100000 kB\nVmSwap:\t0 kB\n"

    with patch.object(doctor._shared, "_run", side_effect=fake_run), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_audio_path_no_swap()
    assert r.status == "warn"
    assert "jasper-aec-bridge" in r.detail
    assert "43056" in r.detail
    assert "music may glitch" in r.detail


def test_audio_path_no_swap_dev_host():
    """No systemctl → all daemons "not running", check still passes
    cleanly (doesn't crash)."""
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("systemctl not found")

    with patch.object(doctor._shared, "_run", side_effect=fake_run):
        r = doctor.check_audio_path_no_swap()
    assert r.status == "ok"
    assert "not running" in r.detail


# --- check_disk_space (disk-pressure observability) ----------------------


def _fake_statvfs(*, total_bytes: int, free_bytes: int, frsize: int = 4096):
    """Build an os.statvfs replacement returning a result with the given
    total/free byte figures. Mirrors the kernel's statvfs_result shape
    (f_blocks/f_bavail in f_frsize units) closely enough for the check."""
    from types import SimpleNamespace

    blocks = total_bytes // frsize
    avail = free_bytes // frsize

    def fake(path):
        return SimpleNamespace(f_blocks=blocks, f_bavail=avail, f_frsize=frsize)

    return fake


def test_disk_warn_percent_default():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("JASPER_DISK_WARN_PERCENT", None)
        assert doctor.memory._disk_warn_percent() == 85


def test_disk_warn_percent_custom_value_honored():
    with patch.dict(os.environ, {"JASPER_DISK_WARN_PERCENT": "70"}):
        assert doctor.memory._disk_warn_percent() == 70


def test_disk_warn_percent_out_of_range_falls_back():
    """A warn >= the fixed 95% fail line, <= 0, or unparseable must snap
    back to the 85% default — a fat-fingered env line can't disable the
    warning or invert the warn/fail band."""
    for bad in ("0", "-5", "95", "99", "notanumber"):
        with patch.dict(os.environ, {"JASPER_DISK_WARN_PERCENT": bad}):
            assert doctor.memory._disk_warn_percent() == 85, bad


def test_disk_space_ok_when_plenty_free():
    fake = _fake_statvfs(
        total_bytes=64 * 1024**3,   # 64 GiB card
        free_bytes=40 * 1024**3,    # 40 GiB free → ~37% used
    )
    with patch.object(doctor.memory.os, "statvfs", fake):
        r = doctor.check_disk_space()
    assert r.status == "ok"
    assert "37% used" in r.detail
    assert "40.0 GiB free" in r.detail
    assert r.detail.startswith("/:")


def test_disk_space_warns_over_85_percent():
    fake = _fake_statvfs(
        total_bytes=32 * 1024**3,
        free_bytes=int(0.12 * 32 * 1024**3),  # 12% free → 88% used
    )
    with patch.object(doctor.memory.os, "statvfs", fake), \
         patch.dict(os.environ, {}, clear=False):
        os.environ.pop("JASPER_DISK_WARN_PERCENT", None)
        r = doctor.check_disk_space()
    assert r.status == "warn"
    assert "88% used" in r.detail
    assert "85% warn threshold" in r.detail


def test_disk_space_fails_over_95_percent():
    fake = _fake_statvfs(
        total_bytes=16 * 1024**3,
        free_bytes=int(0.03 * 16 * 1024**3),  # 3% free → 97% used
    )
    with patch.object(doctor.memory.os, "statvfs", fake):
        r = doctor.check_disk_space()
    assert r.status == "fail"
    assert "97% used" in r.detail
    assert "corruption" in r.detail  # the SD-corruption rationale


def test_disk_space_fail_beats_a_high_custom_warn():
    """Even with the warn knob set above the fail line (which snaps back
    to 85), a 96%-full disk still FAILs — fail always takes precedence."""
    fake = _fake_statvfs(
        total_bytes=16 * 1024**3,
        free_bytes=int(0.04 * 16 * 1024**3),  # 96% used
    )
    with patch.object(doctor.memory.os, "statvfs", fake), \
         patch.dict(os.environ, {"JASPER_DISK_WARN_PERCENT": "99"}):
        r = doctor.check_disk_space()
    assert r.status == "fail"


def test_disk_space_skips_when_statvfs_unavailable():
    """Non-POSIX dev host (no os.statvfs) → skip cleanly as ok, same
    posture as the /proc and /sys checks."""
    # getattr(os, "statvfs", None) must return None.
    with patch.object(doctor.memory.os, "statvfs", None, create=True):
        # Ensure the attribute lookup yields None even though the real
        # module has it: patch sets it to None, getattr returns None.
        r = doctor.check_disk_space()
    assert r.status == "ok"
    assert "skipped" in r.detail


def test_disk_space_warns_on_statvfs_oserror():
    def boom(path):
        raise OSError("nope")

    with patch.object(doctor.memory.os, "statvfs", boom):
        r = doctor.check_disk_space()
    assert r.status == "warn"
    assert "couldn't statvfs" in r.detail


def test_disk_space_skips_zero_sized_fs():
    fake = _fake_statvfs(total_bytes=0, free_bytes=0)
    with patch.object(doctor.memory.os, "statvfs", fake):
        r = doctor.check_disk_space()
    assert r.status == "ok"
    assert "zero-sized" in r.detail


# --- _bounded_dir_size + storage checks ----------------------------------


def test_bounded_dir_size_sums_files(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"x" * 100)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.bin").write_bytes(b"y" * 250)
    total, truncated = doctor.memory._bounded_dir_size(tmp_path)
    assert total == 350
    assert truncated is False


def test_bounded_dir_size_caps_entries(tmp_path, monkeypatch):
    """The entry cap must stop a runaway walk and flag truncation rather
    than examining an unbounded number of dir entries on a 1 GB Pi."""
    for i in range(10):
        (tmp_path / f"f{i}.bin").write_bytes(b"z" * 10)
    monkeypatch.setattr(doctor.memory, "_STORAGE_WALK_MAX_ENTRIES", 3)
    total, truncated = doctor.memory._bounded_dir_size(tmp_path)
    assert truncated is True
    # We stopped early, so the total is a floor — strictly less than the
    # full 100 bytes had we walked everything.
    assert total < 100


def test_bounded_dir_size_caps_depth(tmp_path, monkeypatch):
    """Deeply nested dirs beyond the depth cap are not descended into;
    their contents are excluded and truncation is flagged."""
    deep = tmp_path
    for i in range(5):
        deep = deep / f"d{i}"
        deep.mkdir()
    (deep / "buried.bin").write_bytes(b"q" * 999)
    # Surface file that IS counted.
    (tmp_path / "top.bin").write_bytes(b"a" * 5)
    monkeypatch.setattr(doctor.memory, "_STORAGE_WALK_MAX_DEPTH", 2)
    total, truncated = doctor.memory._bounded_dir_size(tmp_path)
    assert truncated is True
    assert total == 5  # only the surface file, the buried one is past the cap


def test_bounded_dir_size_missing_dir_is_zero(tmp_path):
    total, truncated = doctor.memory._bounded_dir_size(tmp_path / "nope")
    assert total == 0
    assert truncated is False


def test_correction_storage_ok_below_threshold(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "small.wav").write_bytes(b"0" * 1024)
    with patch.dict(os.environ, {
        "JASPER_CORRECTION_SESSIONS_DIR": str(sessions),
    }):
        r = doctor.check_correction_storage()
    assert r.status == "ok"
    assert str(sessions) in r.detail


def test_correction_storage_warns_over_threshold(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "big.wav").write_bytes(b"0" * 4096)
    with patch.dict(os.environ, {
        "JASPER_CORRECTION_SESSIONS_DIR": str(sessions),
        "JASPER_CORRECTION_STORAGE_WARN_BYTES": "1024",  # 1 KiB threshold
    }):
        r = doctor.check_correction_storage()
    assert r.status == "warn"
    assert "warn threshold" in r.detail
    assert "JASPER_CORRECTION_STORAGE_WARN_BYTES" in r.detail


def test_correction_storage_absent_dir_is_ok(tmp_path):
    with patch.dict(os.environ, {
        "JASPER_CORRECTION_SESSIONS_DIR": str(tmp_path / "never_created"),
    }):
        r = doctor.check_correction_storage()
    assert r.status == "ok"
    assert "absent" in r.detail


def test_wake_events_storage_warns_over_threshold(tmp_path):
    wake = tmp_path / "wake-events"
    wake.mkdir()
    (wake / "clip.wav").write_bytes(b"0" * 8192)
    with patch.dict(os.environ, {
        "JASPER_WAKE_EVENTS_DIR": str(wake),
        "JASPER_WAKE_EVENTS_STORAGE_WARN_BYTES": "2048",
    }):
        r = doctor.check_wake_events_storage()
    assert r.status == "warn"
    assert "JASPER_WAKE_EVENTS_STORAGE_WARN_BYTES" in r.detail


def test_wake_events_storage_ok_below_default_threshold(tmp_path):
    """A healthy ring (well under the 1.3 GiB default) never warns."""
    wake = tmp_path / "wake-events"
    wake.mkdir()
    (wake / "clip.wav").write_bytes(b"0" * 1024)
    with patch.dict(os.environ, {
        "JASPER_WAKE_EVENTS_DIR": str(wake),
    }):
        os.environ.pop("JASPER_WAKE_EVENTS_STORAGE_WARN_BYTES", None)
        r = doctor.check_wake_events_storage()
    assert r.status == "ok"


def test_storage_warn_bytes_fallback_on_bad_value():
    assert doctor.memory._storage_warn_bytes("X_UNSET_KNOB_", 4242) == 4242
    with patch.dict(os.environ, {"X_BAD_KNOB_": "notint"}):
        assert doctor.memory._storage_warn_bytes("X_BAD_KNOB_", 99) == 99
    with patch.dict(os.environ, {"X_NEG_KNOB_": "-1"}):
        assert doctor.memory._storage_warn_bytes("X_NEG_KNOB_", 7) == 7


# --- /state.resilience.disk snapshot (state_aggregate) -------------------


def test_disk_snapshot_shape():
    from jasper.control import state_aggregate

    fake = _fake_statvfs(
        total_bytes=64 * 1024**3,
        free_bytes=16 * 1024**3,  # 25% free → 75% used
    )
    with patch.object(state_aggregate.os, "statvfs", fake):
        snap = state_aggregate._disk_snapshot("/")
    assert snap == {
        "path": "/",
        "percent_used": 75,
        "free_gib": 16.0,
        "total_gib": 64.0,
    }


def test_disk_snapshot_none_on_oserror():
    from jasper.control import state_aggregate

    def boom(path):
        raise OSError("denied")

    with patch.object(state_aggregate.os, "statvfs", boom):
        assert state_aggregate._disk_snapshot("/") is None


def test_disk_snapshot_none_when_statvfs_unavailable():
    from jasper.control import state_aggregate

    with patch.object(state_aggregate.os, "statvfs", None, create=True):
        assert state_aggregate._disk_snapshot("/") is None


def test_disk_snapshot_none_on_zero_total():
    from jasper.control import state_aggregate

    fake = _fake_statvfs(total_bytes=0, free_bytes=0)
    with patch.object(state_aggregate.os, "statvfs", fake):
        assert state_aggregate._disk_snapshot("/") is None


# --- /state.chat snapshot ------------------------------------------------


def test_conversation_history_state_reads_store_summary(monkeypatch, tmp_path):
    from jasper.control import state_aggregate

    db_path = tmp_path / "conversation_history.db"
    settings_path = tmp_path / "conversation_history.env"
    settings_path.write_text(
        "\n".join([
            f"{CAPTURE_ENABLED_ENV}=1",
            f"{DB_PATH_ENV}={db_path}",
            f"{RETENTION_DAYS_ENV}=30",
        ])
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JASPER_CONVERSATION_HISTORY_FILE", str(settings_path))
    store = ConversationStore(str(db_path))
    assert store.add(
        ConversationTurn(
            id=make_turn_id("2026-06-19T20:15:00Z", 1),
            ts_utc="2026-06-19T20:15:00Z",
            provider="gemini",
            user_text="hello",
            assistant_text="hi",
            tool_calls_json=None,
            data_json=None,
            session_id=1,
        ),
    )
    store.close()

    snap = state_aggregate._conversation_history_state()

    assert snap is not None
    assert snap["capture_enabled"] is True
    assert snap["turn_count"] == 1
    assert snap["last_write_age_seconds"] is not None
    # max_rows is absent from the env file, so it resolves to the code
    # default rather than disabling the row-count guard.
    assert snap["retention"] == {"days": 30, "max_rows": DEFAULT_RETENTION_MAX_ROWS}


def test_conversation_history_state_disabled_missing_db_is_not_unavailable(
    monkeypatch, tmp_path,
):
    from jasper.control import state_aggregate

    settings_path = tmp_path / "conversation_history.env"
    settings_path.write_text(f"{CAPTURE_ENABLED_ENV}=0\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CONVERSATION_HISTORY_FILE", str(settings_path))

    # Neither retention var is set, so both bounds resolve to the code
    # defaults that keep the store bounded out of the box.
    assert state_aggregate._conversation_history_state() == {
        "capture_enabled": False,
        "turn_count": None,
        "last_write_age_seconds": None,
        "retention": {
            "days": DEFAULT_RETENTION_DAYS,
            "max_rows": DEFAULT_RETENTION_MAX_ROWS,
        },
    }


def test_conversation_history_state_enabled_missing_db_is_null(
    monkeypatch, tmp_path,
):
    from jasper.control import state_aggregate

    db_path = tmp_path / "missing.db"
    settings_path = tmp_path / "conversation_history.env"
    settings_path.write_text(
        f"{CAPTURE_ENABLED_ENV}=1\n{DB_PATH_ENV}={db_path}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JASPER_CONVERSATION_HISTORY_FILE", str(settings_path))

    assert state_aggregate._conversation_history_state() is None
    assert db_path.exists() is False
