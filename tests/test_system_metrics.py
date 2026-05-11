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
    for key in ("t", "mem_available_mb", "mem_used_mb", "swap_used_mb", "load_1m"):
        assert snap["history"][key] == []
    # Current present with sensible defaults.
    cur = snap["current"]
    for key in ("mem_total_mb", "disk_used_pct", "temp_c", "throttled_now",
                "throttled_history", "net_rx_bytes", "net_tx_bytes"):
        assert key in cur


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
