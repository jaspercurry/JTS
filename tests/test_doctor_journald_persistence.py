# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Coverage for check_journald_persistence (jasper/cli/doctor/memory.py).

Pins the two silent regressions the persistent-journal drop-in
(deploy/journald/50-jts-persistent-storage.conf) exists to prevent, per
docs/HANDOFF-resilience.md Tier 5: persistence getting turned off, and the
SystemMaxUse retention cap shrinking below the value JTS installs.

The check composes small helpers (systemd-booted probe, effective config via
systemd-analyze cat-config, installed drop-in cap, disk usage) — the branch
tests monkeypatch those helpers so no filesystem/subprocess is touched; the
parsers get their own direct unit tests.
"""
from __future__ import annotations

from jasper.cli import doctor
from jasper.cli.doctor import memory as doctor_memory


# --- _parse_journald_size ------------------------------------------------


def test_parse_journald_size_suffixes():
    assert doctor_memory._parse_journald_size("500M") == 500 * 1024 ** 2
    assert doctor_memory._parse_journald_size("1G") == 1024 ** 3
    assert doctor_memory._parse_journald_size("200M") == 200 * 1024 ** 2
    assert doctor_memory._parse_journald_size("2048K") == 2048 * 1024
    # A bare number is bytes.
    assert doctor_memory._parse_journald_size("200000000") == 200000000


def test_parse_journald_size_bad_values_are_none():
    for bad in (None, "", "   ", "notasize", "-5M", "M"):
        assert doctor_memory._parse_journald_size(bad) is None, bad


# --- _journald_setting_last_wins -----------------------------------------


def test_setting_last_wins_across_merged_config():
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


def test_setting_last_wins_absent_key_is_none():
    assert doctor_memory._journald_setting_last_wins("Storage=persistent\n", "SystemMaxUse") is None


# --- check_journald_persistence branches ---------------------------------


def _patch(monkeypatch, *, booted=True, storage="persistent",
           eff_cap="500M", installed_cap="500M", usage="usage 214.0M"):
    monkeypatch.setattr(doctor_memory, "_systemd_booted", lambda: booted)
    monkeypatch.setattr(
        doctor_memory, "_journald_effective_config", lambda: (storage, eff_cap),
    )
    monkeypatch.setattr(
        doctor_memory, "_journald_installed_cap_raw", lambda: installed_cap,
    )
    monkeypatch.setattr(doctor_memory, "_journald_disk_usage", lambda: usage)


def test_journald_ok_when_persistent_and_cap_matches(monkeypatch):
    _patch(monkeypatch)
    r = doctor.check_journald_persistence()
    assert r.status == "ok"
    assert "persistent" in r.detail
    assert "500M" in r.detail
    assert "214.0M" in r.detail  # disk usage surfaced


def test_journald_skips_off_systemd_host(monkeypatch):
    _patch(monkeypatch, booted=False)
    r = doctor.check_journald_persistence()
    assert r.status == "ok"
    assert "skipped" in r.detail


def test_journald_warns_when_storage_not_persistent(monkeypatch):
    _patch(monkeypatch, storage="volatile")
    r = doctor.check_journald_persistence()
    assert r.status == "warn"
    assert "not persistent" in r.detail
    assert "reboot" in r.detail


def test_journald_warn_names_missing_dropin_when_volatile_and_absent(monkeypatch):
    _patch(monkeypatch, storage="volatile", installed_cap=None)
    r = doctor.check_journald_persistence()
    assert r.status == "warn"
    assert "not installed" in r.detail


def test_journald_warns_when_cap_regressed_below_installed(monkeypatch):
    # A later drop-in shrank the effective cap under the installed 500M.
    _patch(monkeypatch, storage="persistent", eff_cap="200M", installed_cap="500M")
    r = doctor.check_journald_persistence()
    assert r.status == "warn"
    assert "regressed" in r.detail
    assert "200M" in r.detail and "500M" in r.detail


def test_journald_ok_when_effective_cap_above_installed(monkeypatch):
    # Effective larger than installed is fine (never a regression warn).
    _patch(monkeypatch, storage="persistent", eff_cap="1G", installed_cap="500M")
    r = doctor.check_journald_persistence()
    assert r.status == "ok"


def test_journald_ok_when_cat_config_unavailable_but_dropin_present(monkeypatch):
    # systemd-analyze unavailable → storage/eff_cap None; the installed drop-in
    # alone is enough to report OK rather than warn.
    _patch(monkeypatch, storage=None, eff_cap=None, installed_cap="500M")
    r = doctor.check_journald_persistence()
    assert r.status == "ok"
    assert "500M" in r.detail


def test_journald_warns_when_dropin_absent_and_config_unreadable(monkeypatch):
    _patch(monkeypatch, storage=None, eff_cap=None, installed_cap=None)
    r = doctor.check_journald_persistence()
    assert r.status == "warn"
    assert "not installed" in r.detail


def test_journald_check_registered_once():
    from jasper.cli.doctor._registry import registered_checks
    matches = [
        c for c in registered_checks()
        if c.func.__name__ == "check_journald_persistence"
    ]
    assert len(matches) == 1
    assert matches[0].group == "memory"
