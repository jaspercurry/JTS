"""Tests for jasper.control.system_metrics.

Stdlib-only; runs anywhere with a /proc filesystem (i.e. Linux CI).
The /proc readers handle missing/malformed files gracefully so a
test on macOS still imports the module — the readers just return
zeros. Sample-loop tests don't actually run the thread.
"""
from __future__ import annotations

import json
import os
import platform
import socket
import sys
import threading
import time
import urllib.request
from array import array
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import pytest

from jasper.control import system_metrics
from jasper.control.server import _make_handler
from jasper.control.system_metrics import SystemSampler, read_build_info


# ---------- ring buffer + snapshot --------------------------------------


def test_snapshot_shape_with_no_samples_yet() -> None:
    s = SystemSampler()
    snap = s.snapshot()
    assert "sample_interval_sec" in snap
    assert "history_points" in snap
    assert snap["last_sample_at"] is None
    # History present but empty.
    for key in (
        "t", "mem_available_mb", "mem_used_mb", "swap_used_mb", "load_1m",
        "fan_rpm", "fan_pwm", "temp_c",
    ):
        assert snap["history"][key] == []
    # Current present with sensible defaults.
    cur = snap["current"]
    for key in ("mem_total_mb", "disk_used_pct", "temp_c", "throttled_now",
                "throttled_history", "net_rx_bytes", "net_tx_bytes",
                "fan_present", "fan_rpm", "fan_pwm", "fan_pwm_max"):
        assert key in cur
    # Fan defaults: absent until proven present by a tick.
    assert cur["fan_present"] is False
    assert cur["fan_rpm"] is None
    assert cur["fan_pwm"] is None
    assert cur["fan_pwm_max"] == 255
    # Per-service cgroup list: present but empty until first tick.
    assert snap["services"] == []
    # Per-core CPU and memory-cgroup-enabled: present in current,
    # both inert until first tick has run.
    assert cur["per_core_cpu_pct"] == []
    assert cur["memory_cgroup_enabled"] is None


def test_append_rotates_after_history_points() -> None:
    s = SystemSampler(history_points=4)
    for i in range(10):
        s._append(s._t, float(i))  # type: ignore[arg-type]
    assert list(s._t) == [6.0, 7.0, 8.0, 9.0]


def test_snapshot_returns_independent_copy() -> None:
    """Mutating the snapshot dicts must not corrupt the sampler's
    internal arrays. Important: the dashboard JSON-serializes the
    snapshot under load while the sampler keeps ticking."""
    s = SystemSampler(history_points=8)
    for i in range(5):
        s._append(s._t, float(i))  # type: ignore[arg-type]
    snap = s.snapshot()
    snap["history"]["t"].append(999.0)
    assert list(s._t) == [0.0, 1.0, 2.0, 3.0, 4.0]


# ---------- /proc readers handle real data ------------------------------


@pytest.mark.skipif(
    platform.system() != "Linux",
    reason="/proc is Linux-only",
)
def test_read_meminfo_returns_sensible_values() -> None:
    out = SystemSampler._read_meminfo()
    assert out["total_mb"] > 0
    assert out["available_mb"] >= 0
    assert out["available_mb"] <= out["total_mb"]
    assert out["used_mb"] >= 0
    assert out["swap_used_mb"] >= 0


@pytest.mark.skipif(
    platform.system() != "Linux",
    reason="/proc is Linux-only",
)
def test_read_loadavg_returns_float() -> None:
    val = SystemSampler._read_loadavg_1m()
    assert isinstance(val, float)
    assert val >= 0.0


@pytest.mark.skipif(
    platform.system() != "Linux",
    reason="/proc is Linux-only",
)
def test_read_uptime_returns_float() -> None:
    val = SystemSampler._read_uptime()
    assert isinstance(val, float)
    assert val > 0


@pytest.mark.skipif(
    platform.system() != "Linux",
    reason="/proc is Linux-only",
)
def test_read_net_dev_excludes_loopback() -> None:
    out = SystemSampler._read_net_dev()
    assert "rx_bytes" in out
    assert "tx_bytes" in out
    # Loopback often dominates traffic; verify it's excluded by
    # comparing against the raw file containing the lo line.
    with open("/proc/net/dev") as f:
        raw = f.read()
    assert "lo:" in raw, "expected /proc/net/dev to mention lo"


def test_read_disk_returns_pct_and_total() -> None:
    used_pct, total_gb = SystemSampler._read_disk()
    assert 0.0 <= used_pct <= 100.0
    assert total_gb > 0


# ---------- vcgencmd readers (mock the subprocess) ----------------------


def test_read_temp_c_parses_vcgencmd_output() -> None:
    fake = type("R", (), {"stdout": "temp=47.7'C\n"})()
    with patch.object(system_metrics.subprocess, "run", return_value=fake):
        assert SystemSampler._read_temp_c() == 47.7


def test_read_temp_c_returns_zero_on_missing_vcgencmd() -> None:
    with patch.object(
        system_metrics.subprocess, "run", side_effect=FileNotFoundError(),
    ):
        assert SystemSampler._read_temp_c() == 0.0


def test_read_temp_c_returns_zero_on_unparseable() -> None:
    fake = type("R", (), {"stdout": "garbage"})()
    with patch.object(system_metrics.subprocess, "run", return_value=fake):
        assert SystemSampler._read_temp_c() == 0.0


def test_read_throttled_splits_current_vs_history() -> None:
    # Bit 0 = under-voltage NOW; bit 16 = under-voltage SINCE BOOT.
    # 0x10001 → (current=1, history=1).
    fake = type("R", (), {"stdout": "throttled=0x10001\n"})()
    with patch.object(system_metrics.subprocess, "run", return_value=fake):
        now, hist = SystemSampler._read_throttled()
        assert now == 0x1
        assert hist == 0x1


def test_read_throttled_zero_means_healthy() -> None:
    fake = type("R", (), {"stdout": "throttled=0x0\n"})()
    with patch.object(system_metrics.subprocess, "run", return_value=fake):
        assert SystemSampler._read_throttled() == (0, 0)


def test_read_throttled_handles_missing_vcgencmd() -> None:
    with patch.object(
        system_metrics.subprocess, "run", side_effect=FileNotFoundError(),
    ):
        assert SystemSampler._read_throttled() == (0, 0)


def test_vcgencmd_tick_records_temperature_history() -> None:
    s = SystemSampler(
        sample_interval_sec=5.0,
        vcgencmd_interval_sec=30.0,
        history_points=12,
    )
    with patch.object(
        SystemSampler,
        "_read_temp_c",
        side_effect=[41.0, 42.0, 43.0],
    ), patch.object(
        SystemSampler,
        "_read_throttled",
        return_value=(0, 0),
    ):
        s._tick_vcgencmd()
        s._tick_vcgencmd()
        s._tick_vcgencmd()

    snap = s.snapshot()
    assert snap["current"]["temp_c"] == 43.0
    # 12 points at the 5-second main cadence covers one minute; with
    # vcgencmd sampled every 30 seconds, the temperature history keeps
    # the matching two most recent samples.
    assert snap["history"]["temp_c"] == [42.0, 43.0]


# ---------- fan reader (fake sysfs trees) -------------------------------


def _make_fake_hwmon(root, entries: list[tuple[str, dict]]) -> str:
    """Build a fake /sys/class/hwmon tree under `root`. Each entry is
    (subdir_name, {filename: contents}) — mirrors how Linux exposes
    hwmon devices. Returns the path to use as hwmon_dir."""
    hwmon_dir = os.path.join(root, "hwmon")
    os.makedirs(hwmon_dir)
    for name, files in entries:
        sub = os.path.join(hwmon_dir, name)
        os.makedirs(sub)
        for fname, contents in files.items():
            with open(os.path.join(sub, fname), "w") as f:
                f.write(contents)
    return hwmon_dir


def test_read_fan_finds_pwmfan_by_name(tmp_path) -> None:
    # Realistic layout from a Pi 5: hwmon2 is pwmfan, others aren't.
    hwmon = _make_fake_hwmon(str(tmp_path), [
        ("hwmon0", {"name": "cpu_thermal\n", "temp1_input": "47400\n"}),
        ("hwmon1", {"name": "rp1_adc\n"}),
        ("hwmon2", {
            "name": "pwmfan\n",
            "fan1_input": "2404\n",
            "pwm1": "75\n",
        }),
    ])
    assert SystemSampler._read_fan(hwmon) == {"rpm": 2404, "pwm": 75}


def test_read_fan_returns_none_when_directory_missing(tmp_path) -> None:
    # No hwmon tree at all — e.g. macOS dev box.
    assert SystemSampler._read_fan(str(tmp_path / "no-such-dir")) is None


def test_read_fan_returns_none_when_no_pwmfan(tmp_path) -> None:
    # A Pi without an Active Cooler attached: hwmon exists, but no
    # pwmfan entry. We should NOT misclassify another device as the fan.
    hwmon = _make_fake_hwmon(str(tmp_path), [
        ("hwmon0", {"name": "cpu_thermal\n", "temp1_input": "47400\n"}),
        ("hwmon1", {"name": "rpi_volt\n"}),
    ])
    assert SystemSampler._read_fan(hwmon) is None


def test_read_fan_skips_malformed_entry(tmp_path) -> None:
    # A pwmfan whose fan1_input is garbage — keep searching, don't crash.
    hwmon = _make_fake_hwmon(str(tmp_path), [
        ("hwmon0", {
            "name": "pwmfan\n",
            "fan1_input": "not-a-number\n",
            "pwm1": "75\n",
        }),
    ])
    assert SystemSampler._read_fan(hwmon) is None


def test_tick_with_fan_present_populates_history_and_current(tmp_path) -> None:
    """End-to-end: when _read_fan returns data, _tick appends to the
    history ring and snapshot() exposes current values + fan_present."""
    s = SystemSampler(history_points=5)
    with patch.object(
        SystemSampler, "_read_fan",
        return_value={"rpm": 2404, "pwm": 75},
    ):
        # Force the rest of _tick's readers to work even on macOS.
        with patch.object(
            SystemSampler, "_read_meminfo",
            return_value={"total_mb": 2048, "available_mb": 1024,
                          "used_mb": 1024, "swap_used_mb": 0},
        ), patch.object(
            SystemSampler, "_read_loadavg_1m", return_value=0.5,
        ), patch.object(
            SystemSampler, "_read_net_dev",
            return_value={"rx_bytes": 0, "tx_bytes": 0},
        ), patch.object(
            SystemSampler, "_read_disk", return_value=(50.0, 30.0),
        ), patch.object(
            SystemSampler, "_read_uptime", return_value=3600.0,
        ):
            s._tick()
    snap = s.snapshot()
    assert snap["current"]["fan_present"] is True
    assert snap["current"]["fan_rpm"] == 2404
    assert snap["current"]["fan_pwm"] == 75
    assert snap["history"]["fan_rpm"] == [2404.0]
    assert snap["history"]["fan_pwm"] == [75.0]
    # History stays aligned with t.
    assert len(snap["history"]["fan_rpm"]) == len(snap["history"]["t"])


def test_tick_without_fan_keeps_history_aligned() -> None:
    """When the fan isn't present, history arrays must stay the same
    length as `t` so the dashboard's sparkline doesn't desync."""
    s = SystemSampler(history_points=5)
    with patch.object(SystemSampler, "_read_fan", return_value=None), \
         patch.object(SystemSampler, "_read_meminfo",
                      return_value={"total_mb": 2048, "available_mb": 1024,
                                    "used_mb": 1024, "swap_used_mb": 0}), \
         patch.object(SystemSampler, "_read_loadavg_1m", return_value=0.5), \
         patch.object(SystemSampler, "_read_net_dev",
                      return_value={"rx_bytes": 0, "tx_bytes": 0}), \
         patch.object(SystemSampler, "_read_disk", return_value=(50.0, 30.0)), \
         patch.object(SystemSampler, "_read_uptime", return_value=3600.0):
        s._tick()
        s._tick()
    snap = s.snapshot()
    assert snap["current"]["fan_present"] is False
    assert snap["current"]["fan_rpm"] is None
    assert snap["current"]["fan_pwm"] is None
    assert len(snap["history"]["t"]) == 2
    assert len(snap["history"]["fan_rpm"]) == 2
    assert len(snap["history"]["fan_pwm"]) == 2


# ---------- per-service cgroup sampler ----------------------------------


def _make_fake_slice(root, services: dict[str, dict]) -> str:
    """Build a fake /sys/fs/cgroup/system.slice tree. `services` maps
    service-dir name → {"cpu.stat": "...", "memory.current": "..."}.
    Returns the path to use as slice_dir."""
    slice_dir = os.path.join(root, "system.slice")
    os.makedirs(slice_dir)
    for name, files in services.items():
        sub = os.path.join(slice_dir, name)
        os.makedirs(sub)
        for fname, contents in files.items():
            with open(os.path.join(sub, fname), "w") as f:
                f.write(contents)
    return slice_dir


def test_list_jasper_cgroups_filters_by_prefix(tmp_path) -> None:
    slice_dir = _make_fake_slice(str(tmp_path), {
        "jasper-voice.service": {},
        "jasper-camilla.service": {},
        # Non-jasper unit: must be excluded.
        "shairport-sync.service": {},
        # Wrong suffix: must be excluded.
        "jasper-not-a-service": {},
        # Slices, scopes, mount units: not relevant.
        "system-getty.slice": {},
    })
    assert SystemSampler._list_jasper_cgroups(slice_dir) == [
        "jasper-camilla.service", "jasper-voice.service",
    ]


def test_list_service_cgroups_finds_nested_jts_and_audio_units(tmp_path) -> None:
    root = tmp_path / "cgroup"
    system_slice = root / "system.slice"
    audio_slice = root / "jts.slice" / "jts-audio.slice"
    mic_slice = root / "jts.slice" / "jts-mic.slice"
    for unit_dir in (
        system_slice / "jasper-control.service",
        root / "user.slice" / "user-1000.slice" / "dbus.service",
        audio_slice / "shairport-sync.service",
        audio_slice / "jasper-fanin.service",
        mic_slice / "jasper-aec-bridge.service",
        system_slice / "not-tracked.service",
    ):
        unit_dir.mkdir(parents=True)
        (unit_dir / "cpu.stat").write_text("usage_usec 1\n")

    services = SystemSampler._list_service_cgroups(str(root))
    by_unit = {s["unit"]: s for s in services}

    assert by_unit["jasper-aec-bridge.service"]["group"] == "Mic"
    assert by_unit["jasper-aec-bridge.service"]["cgroup"] == (
        "/jts.slice/jts-mic.slice/jasper-aec-bridge.service"
    )
    assert by_unit["jasper-fanin.service"]["group"] == "Audio"
    assert by_unit["shairport-sync.service"]["group"] == "Audio"
    assert by_unit["jasper-control.service"]["group"] == "Control"
    assert "not-tracked.service" not in by_unit
    assert "dbus.service" not in by_unit


def test_list_jasper_cgroups_returns_empty_when_slice_missing(tmp_path) -> None:
    # macOS dev box, or cgroup-v1 system — slice dir simply isn't there.
    assert SystemSampler._list_jasper_cgroups(
        str(tmp_path / "no-such-slice"),
    ) == []


def test_read_cgroup_cpu_usec_parses_usage_line(tmp_path) -> None:
    # cgroup-v2 cpu.stat shape, copied from a live Pi 5 service:
    cpu_stat = (
        "usage_usec 1234567890\n"
        "user_usec 1000000000\n"
        "system_usec 234567890\n"
        "nr_periods 0\n"
        "nr_throttled 0\n"
        "throttled_usec 0\n"
    )
    slice_dir = _make_fake_slice(str(tmp_path), {
        "jasper-voice.service": {"cpu.stat": cpu_stat},
    })
    assert SystemSampler._read_cgroup_cpu_usec(
        slice_dir, "jasper-voice.service",
    ) == 1234567890


def test_read_cgroup_cpu_usec_returns_none_when_missing(tmp_path) -> None:
    slice_dir = _make_fake_slice(str(tmp_path), {
        "jasper-voice.service": {},  # no cpu.stat
    })
    assert SystemSampler._read_cgroup_cpu_usec(
        slice_dir, "jasper-voice.service",
    ) is None


def test_read_cgroup_memory_bytes_parses(tmp_path) -> None:
    slice_dir = _make_fake_slice(str(tmp_path), {
        "jasper-voice.service": {"memory.current": "157286400\n"},
    })
    assert SystemSampler._read_cgroup_memory_bytes(
        slice_dir, "jasper-voice.service",
    ) == 157286400


def test_read_cgroup_memory_bytes_returns_none_on_missing(tmp_path) -> None:
    slice_dir = _make_fake_slice(str(tmp_path), {"jasper-voice.service": {}})
    assert SystemSampler._read_cgroup_memory_bytes(
        slice_dir, "jasper-voice.service",
    ) is None


def test_tick_services_first_sample_has_no_cpu_pct(tmp_path) -> None:
    """First call after a service appears yields cpu_pct=None — delta
    math needs two samples. RSS works on the first tick."""
    slice_dir = _make_fake_slice(str(tmp_path), {
        "jasper-voice.service": {
            "cpu.stat": "usage_usec 1000000\n",
            "memory.current": "104857600\n",  # 100 MB
        },
    })
    s = SystemSampler()
    out = s._tick_services(slice_dir)
    assert len(out) == 1
    assert out[0]["name"] == "jasper-voice"
    assert out[0]["group"] == "Voice"
    assert out[0]["cgroup"] == "/jasper-voice.service"
    assert out[0]["cpu_pct"] is None
    assert out[0]["rss_mb"] == 100.0


def test_tick_services_second_sample_computes_cpu_pct(
    tmp_path, monkeypatch,
) -> None:
    """Two ticks 1 wall-second apart with 500 ms CPU consumed should
    yield ~50% (half of one core)."""
    slice_dir = _make_fake_slice(str(tmp_path), {
        "jasper-voice.service": {
            "cpu.stat": "usage_usec 1000000\n",
            "memory.current": "104857600\n",
        },
    })
    # Fake monotonic clock so we control the wall delta exactly.
    fake_time = [100.0]
    monkeypatch.setattr(
        system_metrics.time, "monotonic", lambda: fake_time[0],
    )
    s = SystemSampler()
    s._tick_services(slice_dir)  # baseline, cpu_pct=None

    # 1 second later, the service has consumed an additional 500_000 µs
    # of CPU time — half a core.
    fake_time[0] = 101.0
    with open(os.path.join(
        slice_dir, "jasper-voice.service", "cpu.stat",
    ), "w") as f:
        f.write("usage_usec 1500000\n")
    out = s._tick_services(slice_dir)
    assert len(out) == 1
    assert out[0]["cpu_pct"] == 50.0


def test_tick_services_drops_disappeared_services(tmp_path) -> None:
    """A service that vanishes between ticks (one-shot exited, manual
    stop) must not linger in the snapshot or the internal samples dict."""
    slice_dir = _make_fake_slice(str(tmp_path), {
        "jasper-voice.service": {
            "cpu.stat": "usage_usec 1000000\n", "memory.current": "104857600\n",
        },
        "jasper-dac-init.service": {  # one-shot
            "cpu.stat": "usage_usec 500\n", "memory.current": "1048576\n",
        },
    })
    s = SystemSampler()
    s._tick_services(slice_dir)
    assert "/jasper-dac-init.service" in s._service_samples

    # One-shot exits — systemd removes the cgroup dir.
    import shutil
    shutil.rmtree(os.path.join(slice_dir, "jasper-dac-init.service"))

    out = s._tick_services(slice_dir)
    names = [s["name"] for s in out]
    assert "jasper-voice" in names
    assert "jasper-dac-init" not in names
    assert "/jasper-dac-init.service" not in s._service_samples


def test_tick_services_handles_negative_delta(tmp_path, monkeypatch) -> None:
    """If usage_usec ever appears to decrease (clock skew, counter
    reset, or cgroup recreate with same name), floor cpu_pct at 0
    rather than rendering a nonsense negative on the dashboard."""
    slice_dir = _make_fake_slice(str(tmp_path), {
        "jasper-voice.service": {
            "cpu.stat": "usage_usec 5000000\n", "memory.current": "0\n",
        },
    })
    fake_time = [100.0]
    monkeypatch.setattr(
        system_metrics.time, "monotonic", lambda: fake_time[0],
    )
    s = SystemSampler()
    s._tick_services(slice_dir)  # baseline @ usage=5_000_000

    fake_time[0] = 101.0
    with open(os.path.join(
        slice_dir, "jasper-voice.service", "cpu.stat",
    ), "w") as f:
        f.write("usage_usec 1000000\n")  # went backwards
    out = s._tick_services(slice_dir)
    assert out[0]["cpu_pct"] == 0.0


def test_tick_services_handles_missing_slice_dir(tmp_path) -> None:
    s = SystemSampler()
    assert s._tick_services(str(tmp_path / "no-such-dir")) == []


def test_tick_services_partial_read_skipped(tmp_path) -> None:
    """A cgroup whose cpu.stat AND memory.current are both unreadable
    (race with teardown) should be silently skipped. A cgroup with one
    of the two readable still surfaces (partial visibility is better
    than dropping the row)."""
    slice_dir = _make_fake_slice(str(tmp_path), {
        # Empty dir — listdir sees it, but both reads fail.
        "jasper-empty.service": {},
        # Only memory readable; cpu.stat absent.
        "jasper-partial.service": {"memory.current": "1048576\n"},
    })
    s = SystemSampler()
    out = s._tick_services(slice_dir)
    names = [s["name"] for s in out]
    assert "jasper-empty" not in names
    assert "jasper-partial" in names


# ---------- per-core CPU sampler ----------------------------------------


def _write_proc_stat(path, per_core: list[tuple[int, int, int, int, int]]) -> None:
    """Write a fake /proc/stat with `per_core` entries. Each entry is
    (user, nice, system, idle, iowait) — the five fields _read_per_core_jiffies
    cares about. Pads the remaining columns with zeros so the file
    parses cleanly."""
    lines = ["cpu 0 0 0 0 0 0 0 0 0 0\n"]  # aggregate, ignored by reader
    for i, (user, nice, system, idle, iowait) in enumerate(per_core):
        lines.append(
            f"cpu{i} {user} {nice} {system} {idle} {iowait} 0 0 0 0 0\n",
        )
    lines.append("intr 0\nctxt 0\nbtime 0\n")
    with open(path, "w") as f:
        f.writelines(lines)


def test_read_per_core_jiffies_parses_each_cpu_line(tmp_path) -> None:
    p = tmp_path / "stat"
    _write_proc_stat(p, [
        (100, 0, 50, 800, 50),   # cpu0: active = 100+50 = 150, total = 1000
        (200, 10, 100, 600, 90), # cpu1: active = 300, total = 1000
    ])
    out = SystemSampler._read_per_core_jiffies(str(p))
    # active = user + nice + system + irq + softirq + steal
    # total = user + nice + system + idle + iowait + irq + softirq + steal
    # iowait is idle-with-pending-IO so it's NOT in active
    assert out == [(150, 1000), (310, 1000)]


def test_read_per_core_jiffies_skips_aggregate_cpu_line(tmp_path) -> None:
    """The bare 'cpu' line is a sum of cpuN; if we double-counted it
    the percentages would all be wrong."""
    p = tmp_path / "stat"
    _write_proc_stat(p, [(100, 0, 50, 800, 50)])
    out = SystemSampler._read_per_core_jiffies(str(p))
    assert len(out) == 1  # cpu0 only, not cpu+cpu0


def test_read_per_core_jiffies_returns_empty_when_file_missing(tmp_path) -> None:
    assert SystemSampler._read_per_core_jiffies(
        str(tmp_path / "no-such-file"),
    ) == []


def test_tick_per_core_first_sample_returns_empty(tmp_path) -> None:
    """First tick can't compute a delta (no baseline) and must return
    an empty list rather than nonsense or a crash."""
    p = tmp_path / "stat"
    _write_proc_stat(p, [(100, 0, 50, 800, 50), (200, 0, 100, 600, 100)])
    s = SystemSampler()
    assert s._tick_per_core(str(p)) == []
    # Baseline stored for next call.
    assert len(s._per_core_prev) == 2


def test_tick_per_core_second_sample_computes_percentages(tmp_path) -> None:
    """Two samples 1000 jiffies apart, 500 active jiffies elapsed →
    50% utilization."""
    p = tmp_path / "stat"
    _write_proc_stat(p, [(100, 0, 0, 900, 0)])  # active=100, total=1000
    s = SystemSampler()
    s._tick_per_core(str(p))  # baseline

    # Bump active by 500, total by 1000 → 50% of the window was busy.
    _write_proc_stat(p, [(600, 0, 0, 1400, 0)])  # active=600, total=2000
    out = s._tick_per_core(str(p))
    assert out == [50.0]


def test_tick_per_core_handles_negative_delta(tmp_path) -> None:
    """If counters go backwards (kernel quirk, container reset) we
    return 0 for that core rather than a negative."""
    p = tmp_path / "stat"
    _write_proc_stat(p, [(500, 0, 0, 500, 0)])
    s = SystemSampler()
    s._tick_per_core(str(p))
    _write_proc_stat(p, [(100, 0, 0, 200, 0)])  # went backwards
    out = s._tick_per_core(str(p))
    assert out == [0.0]


def test_tick_per_core_resets_when_core_count_changes(tmp_path) -> None:
    """A core count change (very rare on a Pi, but possible via
    hot-plug) must reset the baseline rather than crash on len
    mismatch."""
    p = tmp_path / "stat"
    _write_proc_stat(p, [(100, 0, 0, 900, 0), (100, 0, 0, 900, 0)])
    s = SystemSampler()
    s._tick_per_core(str(p))  # baseline at 2 cores
    # Now only 1 core visible.
    _write_proc_stat(p, [(100, 0, 0, 900, 0)])
    out = s._tick_per_core(str(p))
    assert out == []  # reset, no delta this tick
    assert len(s._per_core_prev) == 1  # new baseline


def test_tick_per_core_clamps_percentages_to_100(tmp_path) -> None:
    """active > total shouldn't be possible, but if floating-point
    drift or a kernel bug ever produces it we clamp at 100 not 9999."""
    p = tmp_path / "stat"
    _write_proc_stat(p, [(0, 0, 0, 1000, 0)])
    s = SystemSampler()
    s._tick_per_core(str(p))
    # active increased by 2000, total only by 1000 — impossible, but
    # the floor/ceil keeps us sane.
    _write_proc_stat(p, [(2000, 0, 0, 1000, 0)])
    out = s._tick_per_core(str(p))
    assert out[0] == 100.0


# ---------- memory cgroup detection -------------------------------------


def test_read_memory_cgroup_enabled_returns_true_when_memory_listed(tmp_path) -> None:
    p = tmp_path / "cgroup.controllers"
    p.write_text("cpuset cpu io memory hugetlb pids rdma misc\n")
    assert SystemSampler._read_memory_cgroup_enabled(str(p)) is True


def test_read_memory_cgroup_enabled_returns_false_when_memory_absent(tmp_path) -> None:
    """Pi 5 default: cmdline carries `cgroup_disable=memory` so the
    memory controller doesn't appear in cgroup.controllers even
    though the file exists. This is the dashboard-warning trigger."""
    p = tmp_path / "cgroup.controllers"
    p.write_text("cpuset cpu io hugetlb pids rdma misc\n")
    assert SystemSampler._read_memory_cgroup_enabled(str(p)) is False


def test_read_memory_cgroup_enabled_returns_none_when_file_missing(tmp_path) -> None:
    """Non-Linux or cgroup-v1 system — file simply isn't there.
    Distinct from 'memory not in controllers' so the dashboard can
    differentiate (and only show the reboot warning on Linux)."""
    assert SystemSampler._read_memory_cgroup_enabled(
        str(tmp_path / "no-such-file"),
    ) is None


# ---------- snapshot integration of new fields --------------------------


def test_tick_populates_per_core_and_cgroup_in_snapshot(tmp_path) -> None:
    """End-to-end through _tick — per_core_cpu_pct and
    memory_cgroup_enabled appear in current{} via the same lock that
    protects the other fields."""
    stat_p = tmp_path / "stat"
    cgroup_p = tmp_path / "cgroup.controllers"
    _write_proc_stat(stat_p, [(100, 0, 0, 900, 0)])
    cgroup_p.write_text("cpuset cpu io memory pids\n")

    s = SystemSampler(history_points=5)
    with patch.object(
        SystemSampler, "_read_meminfo",
        return_value={"total_mb": 2048, "available_mb": 1024,
                      "used_mb": 1024, "swap_used_mb": 0},
    ), patch.object(
        SystemSampler, "_read_loadavg_1m", return_value=0.5,
    ), patch.object(
        SystemSampler, "_read_net_dev",
        return_value={"rx_bytes": 0, "tx_bytes": 0},
    ), patch.object(
        SystemSampler, "_read_disk", return_value=(50.0, 30.0),
    ), patch.object(
        SystemSampler, "_read_uptime", return_value=3600.0,
    ), patch.object(
        SystemSampler, "_read_fan", return_value=None,
    ), patch.object(
        SystemSampler, "_tick_per_core",
        return_value=[12.5, 87.5, 33.0, 50.0],
    ), patch.object(
        SystemSampler, "_read_memory_cgroup_enabled", return_value=False,
    ):
        s._tick()
    snap = s.snapshot()
    cur = snap["current"]
    assert cur["per_core_cpu_pct"] == [12.5, 87.5, 33.0, 50.0]
    assert cur["memory_cgroup_enabled"] is False


# ---------- build manifest ----------------------------------------------


def test_read_build_info_missing_file_returns_empty(tmp_path) -> None:
    out = read_build_info(str(tmp_path / "no-such-file.txt"))
    assert out == {}


def test_read_build_info_parses_install_sh_format(tmp_path) -> None:
    p = tmp_path / "build.txt"
    p.write_text(
        "JASPER_GIT_SHA=abc1234\n"
        "JASPER_GIT_SHA_FULL=abc1234567890def\n"
        "JASPER_GIT_BRANCH=main\n"
        "JASPER_INSTALL_AT=2026-05-11T15:30:00-04:00\n"
    )
    out = read_build_info(str(p))
    assert out["JASPER_GIT_SHA"] == "abc1234"
    assert out["JASPER_GIT_BRANCH"] == "main"
    assert out["JASPER_INSTALL_AT"].startswith("2026-05-11")


def test_read_build_info_ignores_blank_and_comments(tmp_path) -> None:
    p = tmp_path / "build.txt"
    p.write_text(
        "# comment\n"
        "\n"
        "JASPER_GIT_SHA=abc\n"
    )
    out = read_build_info(str(p))
    assert out == {"JASPER_GIT_SHA": "abc"}


# ---------- single tick end-to-end (Linux only) -------------------------


@pytest.mark.skipif(
    platform.system() != "Linux",
    reason="_tick exercises /proc readers",
)
def test_single_tick_populates_history() -> None:
    s = SystemSampler(history_points=10)
    s._tick()
    snap = s.snapshot()
    assert snap["last_sample_at"] is not None
    assert len(snap["history"]["t"]) == 1
    assert len(snap["history"]["mem_available_mb"]) == 1
    assert snap["history"]["mem_available_mb"][0] > 0
    assert snap["current"]["mem_total_mb"] > 0
    assert snap["current"]["disk_total_gb"] > 0
    assert snap["current"]["uptime_sec"] > 0


# ---------- /system/snapshot HTTP endpoint ----------------------------


def _http_get(url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=2) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def test_snapshot_endpoint_returns_metrics_and_build(monkeypatch) -> None:
    """End-to-end: handler returns /system/snapshot with metrics +
    build manifest. Verifies the wiring through _make_handler and
    the JSON shape the dashboard consumes."""
    # Sampler with one tick pre-loaded so history isn't empty.
    sampler = SystemSampler(history_points=4)
    sampler._append(sampler._t, 1.0)
    sampler._append(sampler._mem_available_mb, 1024.0)
    sampler._append(sampler._mem_used_mb, 700.0)
    sampler._append(sampler._swap_used_mb, 0.0)
    sampler._append(sampler._load_1m, 0.5)
    sampler._mem_total_mb = 2048
    sampler._uptime_sec = 3600.0
    sampler._last_sample_at = time.time()

    # Stub the build-file reader so the test doesn't depend on /var/lib/jasper.
    monkeypatch.setattr(
        system_metrics, "read_build_info",
        lambda *a, **kw: {"JASPER_GIT_SHA": "test123"},
    )

    handler = _make_handler("127.0.0.1", 1234, "/nonexistent.sock", sampler)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        status, body = _http_get(f"{base}/system/snapshot")
        assert status == 200
        assert body["build"] == {"JASPER_GIT_SHA": "test123"}
        m = body["metrics"]
        assert m["history"]["mem_available_mb"] == [1024.0]
        assert m["current"]["mem_total_mb"] == 2048
        assert m["current"]["uptime_sec"] == 3600.0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_snapshot_endpoint_handles_missing_sampler() -> None:
    """If the sampler hasn't been wired in (legacy code path), the
    endpoint returns metrics=None rather than 500ing. Lets tests +
    dev environments work without booting the sampler thread."""
    handler = _make_handler("127.0.0.1", 1234, "/nonexistent.sock", None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        status, body = _http_get(f"{base}/system/snapshot")
        assert status == 200
        assert body["metrics"] is None
        # build still present (read_build_info returns {} on missing file).
        assert "build" in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
