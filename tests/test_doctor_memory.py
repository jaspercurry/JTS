# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor memory domain.

These checks verify that the configs installed by
deploy/lib/install/memory-resilience.sh are actually applied at runtime. Every
check reads a kernel interface (/proc, /sys, /sys/fs/cgroup), so those are
mocked; each must skip gracefully on a dev host where the paths do not exist.
"""
from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from jasper.cli import doctor
from jasper.cli.doctor import _evidence
from jasper.cli.doctor import memory as doctor_memory

from .doctor_test_support import _make_unit_states_fake

ROOT = Path(__file__).resolve().parents[1]


def _mock_meminfo(values: dict[str, int]):
    """Mock `open('/proc/meminfo')` returning the kernel's "Field: NNN kB"."""
    lines = [f"{k}: {v} kB\n" for k, v in values.items()]
    m = MagicMock()
    m.__enter__.return_value = io.StringIO("".join(lines))
    m.__exit__.return_value = None
    return m


# ------------------------------------------------------------------ check_ram


@pytest.mark.parametrize(
    "profile, status, reason",
    [
        ("full", "warn", doctor_memory.REASON_RAM_UNDERSIZED),
        # Streambox is the deliberately-light tier a Zero 2 W resolves to, so
        # the board-size warn is a false positive there; live pressure stays
        # covered SKU-agnostically by check_memory_headroom.
        ("streambox", "ok", doctor_memory.REASON_RAM_STREAMBOX_TIER),
        # A marker-read glitch must not silently suppress the warn on a real
        # full speaker: _install_profile_is_streambox fails toward False.
        (OSError("marker unreadable"), "warn", doctor_memory.REASON_RAM_UNDERSIZED),
    ],
    ids=["full", "streambox", "profile-unreadable"],
)
def test_check_ram_warns_only_for_an_undersized_full_install(profile, status, reason):
    kwargs = (
        {"side_effect": profile}
        if isinstance(profile, Exception)
        else {"return_value": profile}
    )
    with patch(
        "builtins.open",
        return_value=_mock_meminfo({"MemTotal": 426076}),  # ~416 MB
    ), patch("jasper.cli.doctor.memory.read_install_profile", **kwargs):
        r = doctor.check_ram()

    assert r.status == status
    assert r.reason == reason


# ------------------------------------------------------- check_memory_headroom
#
# Thresholds are proportional: warn at max(100 MB, 10% of MemTotal), fail at
# max(30 MB, 3%). A fixed "total_mb < 1500" gate used to miss a 2 GB Pi sitting
# at 78 MB available (3.8% headroom).


@pytest.mark.parametrize(
    "total_kb, available_kb, status",
    [
        (1014768, 300000, "ok"),  # ~991 MB total, ~293 MB free
        (1014768, 80000, "warn"),  # ~78 MB, under the 100 MB floor
        (1014768, 20000, "fail"),  # ~19 MB, under the 30 MB floor
        (2097152, 80000, "warn"),  # 2 GB: warn 200 MB, fail 60 MB
        (8388608, 500000, "warn"),  # 8 GB: warn 800 MB, fail 240 MB
        (8388608, 2097152, "ok"),  # 8 GB with 25% headroom
        (16777216, 400000, "fail"),  # 16 GB: fail 480 MB
    ],
    ids=["1gb-ok", "1gb-warn", "1gb-fail", "2gb-warn", "8gb-warn", "8gb-ok",
         "16gb-fail"],
)
def test_memory_headroom_thresholds_scale_with_total_ram(
    total_kb, available_kb, status
):
    with patch(
        "builtins.open",
        return_value=_mock_meminfo(
            {"MemTotal": total_kb, "MemAvailable": available_kb}
        ),
    ):
        assert doctor.check_memory_headroom().status == status


def test_memory_headroom_warns_when_meminfo_is_unreadable():
    with patch("builtins.open", side_effect=OSError("permission denied")):
        r = doctor.check_memory_headroom()
        assert r.status == "warn"
        assert r.reason == doctor_memory.REASON_MEMORY_HEADROOM_UNREADABLE


# ------------------------------------------------------- check_zram_size_ratio


def _zram_mocks(zram_bytes: int, *, rpi_swap_installed: bool = True):
    def fake_read(self):
        if str(self) == "/sys/block/zram0/disksize":
            return str(zram_bytes)
        raise FileNotFoundError(str(self))

    def fake_exists(self):
        return str(self) == "/etc/rpi/swap.conf" and rpi_swap_installed

    return fake_read, fake_exists


@pytest.mark.parametrize(
    "zram_bytes, rpi_swap_installed, status",
    [
        # rpi-swap installed and zram over 60% of RAM: reboot applies the
        # JTS drop-in, so the warn is actionable.
        (1014767616, True, "warn"),
        # Without rpi-swap the drop-in is inert — the operator cannot fix this
        # without changing distros, so warning forever would be a nanny.
        (1014767616, False, "ok"),
        (520 * 1024 * 1024, True, "ok"),
    ],
    ids=["over-60pct", "no-rpi-swap", "at-50pct"],
)
def test_check_zram_size_ratio_verdicts(zram_bytes, rpi_swap_installed, status):
    fake_read, fake_exists = _zram_mocks(
        zram_bytes, rpi_swap_installed=rpi_swap_installed
    )
    with patch("pathlib.Path.read_text", fake_read), patch(
        "pathlib.Path.exists", fake_exists
    ), patch(
        "builtins.open", return_value=_mock_meminfo({"MemTotal": 1014768})
    ):
        assert doctor.check_zram_size_ratio().status == status


def test_check_zram_size_ratio_skips_without_a_zram_device():
    """Dev host / older RPi OS — no /sys/block/zram0."""
    with patch("pathlib.Path.read_text", side_effect=FileNotFoundError):
        r = doctor.check_zram_size_ratio()
        assert r.status == "skipped"
        assert r.reason == doctor_memory.REASON_ZRAM_ABSENT


# ------------------------------------------------------ audio-slice protection


@pytest.mark.parametrize(
    "controllers, cgroup_present, status",
    [
        ("cpu io memory pids\n", True, "ok"),
        # Without the memory controller the slices' MemorySwapMax=0 is a no-op,
        # so the audio protection is simply gone: fail, not warn.
        ("cpu io pids\n", True, "fail"),
        (None, False, "skipped"),  # no /sys/fs/cgroup — not Linux
    ],
    ids=["enabled", "disabled", "dev-host"],
)
def test_check_cgroup_memory_enabled_verdicts(
    monkeypatch, controllers, cgroup_present, status
):
    monkeypatch.setattr(Path, "exists", lambda self: cgroup_present)
    if controllers is not None:
        monkeypatch.setattr(Path, "read_text", lambda self, **kw: controllers)

    assert doctor.check_cgroup_memory_enabled().status == status


def test_audio_path_units_cover_every_protected_slice_unit():
    systemd_dir = ROOT / "deploy/systemd"
    slices = {"Slice=jts-audio.slice", "Slice=jts-mic.slice"}

    def _directives(path: Path) -> set[str]:
        return {
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

    expected = {
        service.stem
        for service in systemd_dir.glob("*.service")
        if _directives(service) & slices
    } | {
        drop_in.parent.name.removesuffix(".service.d")
        for drop_in in systemd_dir.glob("*.service.d/*.conf")
        if _directives(drop_in) & slices
    }

    assert set(doctor._AUDIO_PATH_UNITS) == expected
    assert len(doctor._AUDIO_PATH_UNITS) == len(set(doctor._AUDIO_PATH_UNITS))


def _audio_unit_states_fake(pids: dict[str, int]):
    overrides = {
        f"{unit}.service": {"main_pid": pid} for unit, pid in pids.items()
    }
    return _make_unit_states_fake(overrides)


def test_audio_path_no_swap_is_ok_when_every_daemon_is_swap_free(monkeypatch):
    pids = {unit: 2001 + i for i, unit in enumerate(doctor._AUDIO_PATH_UNITS)}
    monkeypatch.setattr(
        _evidence, "read_unit_states", _audio_unit_states_fake(pids),
    )
    with patch(
        "pathlib.Path.read_text",
        lambda self: "Name:\tfake\nVmRSS:\t100000 kB\nVmSwap:\t0 kB\n",
    ):
        r = doctor.check_audio_path_no_swap()
        assert r.status == "ok"
        assert r.reason == doctor_memory.REASON_AUDIO_PATH_ALL_SWAP_FREE


def test_audio_path_no_swap_names_the_swapped_daemon_and_amount(monkeypatch):
    """The 2026-05-24 signature: aec-bridge holding 42 MB of VmSwap."""

    def fake_read(self):
        pid = str(self).split("/")[2]
        swap = "43056" if pid == "2003" else "0"
        return f"Name:\tfoo\nVmRSS:\t100000 kB\nVmSwap:\t{swap} kB\n"

    pids = {
        unit: 2003 if unit == "jasper-aec-bridge" else 3000 + i
        for i, unit in enumerate(doctor._AUDIO_PATH_UNITS)
    }
    monkeypatch.setattr(
        _evidence, "read_unit_states", _audio_unit_states_fake(pids),
    )
    with patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_audio_path_no_swap()

    assert r.status == "warn"
    assert r.reason == doctor_memory.REASON_AUDIO_SWAP_DETECTED


def test_audio_path_no_swap_is_ok_without_systemctl(monkeypatch):
    monkeypatch.setattr(
        _evidence, "read_unit_states", lambda units, *, timeout: None,
    )
    r = doctor.check_audio_path_no_swap()
    assert r.status == "ok"
    assert r.reason == doctor_memory.REASON_AUDIO_PATH_SOME_NOT_RUNNING


# ------------------------------------------------------------ check_disk_space


def _fake_statvfs(*, total_bytes: int, free_bytes: int, frsize: int = 4096):
    """An os.statvfs replacement (f_blocks/f_bavail in f_frsize units)."""
    blocks = total_bytes // frsize
    avail = free_bytes // frsize

    def fake(path):
        return SimpleNamespace(f_blocks=blocks, f_bavail=avail, f_frsize=frsize)

    return fake


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, 85),
        ("70", 70),
        # A warn at or above the fixed 95% fail line, at or below 0, or
        # unparseable snaps back: a fat-fingered env line must not disable the
        # warning or invert the warn/fail band.
        ("0", 85),
        ("-5", 85),
        ("95", 85),
        ("99", 85),
        ("notanumber", 85),
    ],
    ids=["default", "custom", "zero", "negative", "at-fail", "above-fail", "junk"],
)
def test_disk_warn_percent_clamps_to_a_sane_band(monkeypatch, value, expected):
    monkeypatch.delenv("JASPER_DISK_WARN_PERCENT", raising=False)
    if value is not None:
        monkeypatch.setenv("JASPER_DISK_WARN_PERCENT", value)

    assert doctor_memory._disk_warn_percent() == expected


@pytest.mark.parametrize(
    "total_gib, free_fraction, warn_percent, status, reason",
    [
        (64, 40 / 64, None, "ok", ""),
        (32, 0.12, None, "warn", doctor_memory.REASON_DISK_NEAR_FULL),
        (16, 0.03, None, "fail", doctor_memory.REASON_DISK_FULL),
        # Fail always wins, even with the warn knob set above the fail line
        # (which itself snaps back to 85).
        (16, 0.04, "99", "fail", doctor_memory.REASON_DISK_FULL),
    ],
    ids=["plenty", "over-warn", "over-fail", "fail-beats-custom-warn"],
)
def test_check_disk_space_verdicts(
    monkeypatch, total_gib, free_fraction, warn_percent, status, reason
):
    monkeypatch.delenv("JASPER_DISK_WARN_PERCENT", raising=False)
    if warn_percent is not None:
        monkeypatch.setenv("JASPER_DISK_WARN_PERCENT", warn_percent)
    total = total_gib * 1024**3
    fake = _fake_statvfs(total_bytes=total, free_bytes=int(free_fraction * total))

    with patch.object(doctor_memory.os, "statvfs", fake):
        r = doctor.check_disk_space()

    assert r.status == status
    if reason:
        assert r.reason == reason


def test_check_disk_space_skips_on_a_non_posix_host():
    with patch.object(doctor_memory.os, "statvfs", None, create=True):
        r = doctor.check_disk_space()
        assert r.status == "skipped"
        assert r.reason == doctor_memory.REASON_DISK_NOT_POSIX


def test_check_disk_space_warns_on_statvfs_oserror():
    def boom(path):
        raise OSError("nope")

    with patch.object(doctor_memory.os, "statvfs", boom):
        r = doctor.check_disk_space()
        assert r.status == "warn"
        assert r.reason == doctor_memory.REASON_DISK_STATVFS_FAILED


def test_check_disk_space_skips_a_zero_sized_filesystem():
    with patch.object(
        doctor_memory.os, "statvfs", _fake_statvfs(total_bytes=0, free_bytes=0)
    ):
        r = doctor.check_disk_space()
        assert r.status == "skipped"
        assert r.reason == doctor_memory.REASON_DISK_ZERO_SIZED


# ------------------------------------------- _bounded_dir_size + storage checks


def test_bounded_dir_size_sums_files(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"x" * 100)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.bin").write_bytes(b"y" * 250)

    assert doctor_memory._bounded_dir_size(tmp_path) == (350, False)


def test_bounded_dir_size_missing_dir_is_zero(tmp_path):
    assert doctor_memory._bounded_dir_size(tmp_path / "nope") == (0, False)


def test_bounded_dir_size_caps_entries(tmp_path, monkeypatch):
    """The entry cap stops a runaway walk on a 1 GB Pi and flags truncation;
    the returned total is then a floor, not the real size."""
    for i in range(10):
        (tmp_path / f"f{i}.bin").write_bytes(b"z" * 10)
    monkeypatch.setattr(doctor_memory, "_STORAGE_WALK_MAX_ENTRIES", 3)

    total, truncated = doctor_memory._bounded_dir_size(tmp_path)

    assert truncated is True
    assert total < 100


def test_bounded_dir_size_caps_depth(tmp_path, monkeypatch):
    deep = tmp_path
    for i in range(5):
        deep = deep / f"d{i}"
        deep.mkdir()
    (deep / "buried.bin").write_bytes(b"q" * 999)
    (tmp_path / "top.bin").write_bytes(b"a" * 5)
    monkeypatch.setattr(doctor_memory, "_STORAGE_WALK_MAX_DEPTH", 2)

    total, truncated = doctor_memory._bounded_dir_size(tmp_path)

    assert truncated is True
    assert total == 5  # only the surface file; the buried one is past the cap


@pytest.mark.parametrize(
    "check, dir_env, warn_env, size, warn_bytes, status",
    [
        (
            "check_correction_storage",
            "JASPER_CORRECTION_SESSIONS_DIR",
            "JASPER_CORRECTION_STORAGE_WARN_BYTES",
            1024,
            None,
            "ok",
        ),
        (
            "check_correction_storage",
            "JASPER_CORRECTION_SESSIONS_DIR",
            "JASPER_CORRECTION_STORAGE_WARN_BYTES",
            4096,
            "1024",
            "warn",
        ),
        (
            "check_wake_events_storage",
            "JASPER_WAKE_EVENTS_DIR",
            "JASPER_WAKE_EVENTS_STORAGE_WARN_BYTES",
            1024,
            None,
            "ok",
        ),
        (
            "check_wake_events_storage",
            "JASPER_WAKE_EVENTS_DIR",
            "JASPER_WAKE_EVENTS_STORAGE_WARN_BYTES",
            8192,
            "2048",
            "warn",
        ),
    ],
    ids=["correction-ok", "correction-warn", "wake-ok", "wake-warn"],
)
def test_storage_checks_warn_over_their_threshold(
    monkeypatch, tmp_path, check, dir_env, warn_env, size, warn_bytes, status
):
    d = tmp_path / "store"
    d.mkdir()
    (d / "clip.wav").write_bytes(b"0" * size)
    monkeypatch.setenv(dir_env, str(d))
    monkeypatch.delenv(warn_env, raising=False)
    if warn_bytes is not None:
        monkeypatch.setenv(warn_env, warn_bytes)

    r = getattr(doctor, check)()

    assert r.status == status
    if status == "warn":
        assert r.reason == doctor_memory.REASON_STORAGE_OVER_THRESHOLD


def test_correction_storage_absent_dir_is_skipped(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_CORRECTION_SESSIONS_DIR", str(tmp_path / "never"))

    r = doctor.check_correction_storage()
    assert r.status == "skipped"
    assert r.reason == doctor_memory.REASON_STORAGE_ABSENT


def test_wake_events_warn_threshold_scales_with_the_configured_cap(
    monkeypatch, tmp_path
):
    """A Pi that deliberately raises the audio cap must not warn forever: the
    threshold scales to that cap, not to the 128 MiB default."""
    wake = tmp_path / "wake-events"
    wake.mkdir()
    with open(wake / "clip.wav", "wb") as f:
        # Above a 128 MiB-scaled default, below a 1 GiB-scaled one.
        f.truncate(600 * 1024 * 1024)
    monkeypatch.setenv("JASPER_WAKE_EVENTS_DIR", str(wake))
    monkeypatch.delenv("JASPER_WAKE_EVENTS_STORAGE_WARN_BYTES", raising=False)
    monkeypatch.setenv("JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES", str(1024**3))

    assert doctor.check_wake_events_storage().status == "ok"


@pytest.mark.parametrize(
    "value", [None, "notint", "-1"], ids=["unset", "junk", "negative"]
)
def test_storage_warn_bytes_falls_back_on_an_unusable_value(monkeypatch, value):
    knob = "X_STORAGE_KNOB_"
    monkeypatch.delenv(knob, raising=False)
    if value is not None:
        monkeypatch.setenv(knob, value)

    assert doctor_memory._storage_warn_bytes(knob, 4242) == 4242


# ---------------------------------------------- check_journald_persistence


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("500M", 500 * 1024**2),
        ("1G", 1024**3),
        ("2048K", 2048 * 1024),
        ("200000000", 200000000),  # a bare number is bytes
        (None, None),
        ("", None),
        ("   ", None),
        ("notasize", None),
        ("-5M", None),
        ("M", None),
    ],
    ids=["mib", "gib", "kib", "bare", "none", "empty", "blank", "junk", "negative",
         "suffix-only"],
)
def test_parse_journald_size(raw, expected):
    assert doctor_memory._parse_journald_size(raw) == expected


def test_journald_setting_last_wins_across_merged_config():
    merged = (
        "# base\n"
        "[Journal]\n"
        "Storage=volatile\n"
        "SystemMaxUse=200M\n"
        "; a later drop-in\n"
        "Storage=persistent\n"
        "SystemMaxUse=500M\n"
    )

    assert doctor_memory._journald_setting_last_wins(merged, "Storage") == "persistent"
    assert doctor_memory._journald_setting_last_wins(merged, "SystemMaxUse") == "500M"
    assert doctor_memory._journald_setting_last_wins(merged, "Absent") is None


def _journald(monkeypatch, *, booted=True, storage="persistent", eff_cap="500M",
              installed_cap="500M", usage="usage 214.0M"):
    monkeypatch.setattr(doctor_memory, "_systemd_booted", lambda: booted)
    monkeypatch.setattr(
        doctor_memory, "_journald_effective_config", lambda: (storage, eff_cap)
    )
    monkeypatch.setattr(
        doctor_memory, "_journald_installed_cap_raw", lambda: installed_cap
    )
    monkeypatch.setattr(doctor_memory, "_journald_disk_usage", lambda: usage)


@pytest.mark.parametrize(
    "kwargs, status, reason",
    [
        ({}, "ok", ""),  # persistent, cap matches, usage surfaced
        ({"booted": False}, "skipped", doctor_memory.REASON_JOURNALD_NOT_BOOTED),
        (
            {"storage": "volatile"}, "warn",
            doctor_memory.REASON_JOURNALD_NOT_PERSISTENT,
        ),
        (
            {"storage": "volatile", "installed_cap": None}, "warn",
            doctor_memory.REASON_JOURNALD_NOT_PERSISTENT,
        ),
        # A later drop-in shrank the effective cap under the installed one.
        (
            {"eff_cap": "200M"}, "warn",
            doctor_memory.REASON_JOURNALD_CAP_REGRESSED,
        ),
        # Effective larger than installed is never a regression.
        ({"eff_cap": "1G"}, "ok", ""),
        # systemd-analyze unavailable: the installed drop-in alone is enough.
        ({"storage": None, "eff_cap": None}, "ok", ""),
        (
            {"storage": None, "eff_cap": None, "installed_cap": None}, "warn",
            doctor_memory.REASON_JOURNALD_CONFIG_UNREADABLE,
        ),
    ],
    ids=["healthy", "not-booted", "volatile", "volatile-no-dropin", "cap-regressed",
         "cap-raised", "config-unreadable", "no-dropin-no-config"],
)
def test_check_journald_persistence_verdicts(monkeypatch, kwargs, status, reason):
    _journald(monkeypatch, **kwargs)

    r = doctor.check_journald_persistence()

    assert r.status == status
    if reason:
        assert r.reason == reason


def test_check_journald_persistence_is_registered_once_in_the_memory_group():
    matches = [
        c
        for c in doctor.registered_checks()
        if c.func.__name__ == "check_journald_persistence"
    ]

    assert len(matches) == 1
    assert matches[0].group == "memory"


# ------------------------------- /state.resilience.disk (the /state mirror)
#
# state_aggregate._disk_snapshot reads the same statvfs as check_disk_space and
# publishes it on /state, so the two share this fixture and must agree on when
# the read is unusable.


def test_disk_snapshot_shape():
    from jasper.control import state_aggregate

    fake = _fake_statvfs(total_bytes=64 * 1024**3, free_bytes=16 * 1024**3)

    with patch.object(state_aggregate.os, "statvfs", fake):
        assert state_aggregate._disk_snapshot("/") == {
            "path": "/",
            "percent_used": 75,
            "free_gib": 16.0,
            "total_gib": 64.0,
        }


@pytest.mark.parametrize(
    "statvfs", ["oserror", "absent", "zero-total"],
    ids=["oserror", "unavailable", "zero-total"],
)
def test_disk_snapshot_is_none_when_the_read_is_unusable(statvfs):
    from jasper.control import state_aggregate

    def boom(path):
        raise OSError("denied")

    replacement = {
        "oserror": boom,
        "absent": None,
        "zero-total": _fake_statvfs(total_bytes=0, free_bytes=0),
    }[statvfs]

    with patch.object(state_aggregate.os, "statvfs", replacement, create=True):
        assert state_aggregate._disk_snapshot("/") is None
