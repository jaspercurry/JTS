"""Unit tests for jasper-doctor's usbsink checks.

The three checks are hardware-side (systemctl, /proc/asound,
/boot/firmware/config.txt) so we monkeypatch the helpers and reads.
Pi-side smoke testing happens via jasper-doctor itself.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from jasper.cli import doctor


# ----------------------------------------------------------------------
# check_usbsink_dtoverlay
# ----------------------------------------------------------------------


def test_usbsink_dtoverlay_present(monkeypatch, tmp_path):
    cfg = tmp_path / "config.txt"
    cfg.write_text(
        "[pi5]\ndtoverlay=dwc2,dr_mode=peripheral\ncountry=US\n",
    )
    # Patch Path resolution by patching the literal in the function.
    with patch.object(doctor, "Path", autospec=True) as mock_path:
        mock_path.side_effect = lambda p: cfg if p == "/boot/firmware/config.txt" else Path(p)
        r = doctor.check_usbsink_dtoverlay()
    assert r.status == "ok"
    assert "enabled" in r.detail.lower()


def test_usbsink_dtoverlay_missing_returns_warn(monkeypatch, tmp_path):
    cfg = tmp_path / "config.txt"
    cfg.write_text("[pi5]\ncountry=US\n")  # no dtoverlay
    with patch.object(doctor, "Path") as mock_path:
        mock_path.side_effect = lambda p: cfg if p == "/boot/firmware/config.txt" else Path(p)
        r = doctor.check_usbsink_dtoverlay()
    assert r.status == "warn"
    assert "re-run" in r.detail.lower() or "reboot" in r.detail.lower()


def test_usbsink_dtoverlay_missing_config_file(monkeypatch, tmp_path):
    """Not on a Pi → config.txt missing → warn (not a fail)."""
    nonexistent = tmp_path / "config.txt"
    with patch.object(doctor, "Path") as mock_path:
        mock_path.side_effect = lambda p: nonexistent if p == "/boot/firmware/config.txt" else Path(p)
        r = doctor.check_usbsink_dtoverlay()
    assert r.status == "warn"


# ----------------------------------------------------------------------
# check_usbsink_state — service active path
# ----------------------------------------------------------------------


def _patch_active(monkeypatch, active: bool):
    monkeypatch.setattr(doctor, "_systemd_is_active", lambda unit: active)


def _patch_libcomp_loaded(monkeypatch, loaded: bool):
    monkeypatch.setattr(doctor, "_module_loaded", lambda name: loaded)


def test_usbsink_state_disabled_no_libcomposite(monkeypatch):
    _patch_active(monkeypatch, False)
    _patch_libcomp_loaded(monkeypatch, False)
    r = doctor.check_usbsink_state()
    assert r.status == "ok"
    assert "disabled" in r.detail.lower()


def test_usbsink_state_disabled_libcomposite_loaded_is_warn(monkeypatch):
    """RAM drift detection: service stopped but libcomposite still
    loaded means the previous stop didn't tear cleanly."""
    _patch_active(monkeypatch, False)
    _patch_libcomp_loaded(monkeypatch, True)
    r = doctor.check_usbsink_state()
    assert r.status == "warn"
    assert "libcomposite" in r.detail.lower()
    assert "ram" in r.detail.lower() or "drift" in r.detail.lower()


def test_usbsink_state_active_no_state_file(monkeypatch, tmp_path):
    _patch_active(monkeypatch, True)
    _patch_libcomp_loaded(monkeypatch, True)
    nonexistent = tmp_path / "state.json"
    with patch.object(doctor, "Path") as mock_path:
        def _path(p):
            if p == "/run/jasper-usbsink/state.json":
                return nonexistent
            return Path(p)
        mock_path.side_effect = _path
        r = doctor.check_usbsink_state()
    assert r.status == "fail"
    assert "missing" in r.detail.lower()


def test_usbsink_state_active_fresh_state(monkeypatch, tmp_path):
    _patch_active(monkeypatch, True)
    _patch_libcomp_loaded(monkeypatch, True)
    state_path = tmp_path / "state.json"
    # Very recent (within 1 s of now).
    now = datetime.now(timezone.utc).isoformat()
    state_path.write_text(json.dumps({
        "playing": True, "preempted": False,
        "host_connected": True, "rms_dbfs": -14.2,
        "updated_at": now,
    }))
    with patch.object(doctor, "Path") as mock_path:
        def _path(p):
            if p == "/run/jasper-usbsink/state.json":
                return state_path
            return Path(p)
        mock_path.side_effect = _path
        r = doctor.check_usbsink_state()
    assert r.status == "ok"
    assert "active" in r.detail.lower()
    assert "playing=True" in r.detail


def test_usbsink_state_active_stale_state_is_warn(monkeypatch, tmp_path):
    _patch_active(monkeypatch, True)
    _patch_libcomp_loaded(monkeypatch, True)
    state_path = tmp_path / "state.json"
    # 30 seconds old → way past the 10-s tolerance.
    stale = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    state_path.write_text(json.dumps({
        "playing": False, "preempted": False, "host_connected": False,
        "rms_dbfs": -120.0,
        "updated_at": stale,
    }))
    with patch.object(doctor, "Path") as mock_path:
        def _path(p):
            if p == "/run/jasper-usbsink/state.json":
                return state_path
            return Path(p)
        mock_path.side_effect = _path
        r = doctor.check_usbsink_state()
    assert r.status == "warn"
    assert "stale" in r.detail.lower()


def test_usbsink_state_active_corrupt_state_is_fail(monkeypatch, tmp_path):
    _patch_active(monkeypatch, True)
    _patch_libcomp_loaded(monkeypatch, True)
    state_path = tmp_path / "state.json"
    state_path.write_text("{not valid")
    with patch.object(doctor, "Path") as mock_path:
        def _path(p):
            if p == "/run/jasper-usbsink/state.json":
                return state_path
            return Path(p)
        mock_path.side_effect = _path
        r = doctor.check_usbsink_state()
    assert r.status == "fail"
    assert "parse" in r.detail.lower()


# ----------------------------------------------------------------------
# check_usbsink_card
# ----------------------------------------------------------------------


def test_usbsink_card_disabled_skips(monkeypatch):
    _patch_active(monkeypatch, False)
    r = doctor.check_usbsink_card()
    assert r.status == "ok"
    assert "skipped" in r.detail.lower() or "disabled" in r.detail.lower()


def test_usbsink_card_active_present(monkeypatch, tmp_path):
    _patch_active(monkeypatch, True)
    fake_card = tmp_path / "UAC2Gadget"
    fake_card.mkdir()
    with patch.object(doctor, "Path") as mock_path:
        def _path(p):
            if p == "/proc/asound/UAC2Gadget":
                return fake_card
            return Path(p)
        mock_path.side_effect = _path
        r = doctor.check_usbsink_card()
    assert r.status == "ok"
    assert "UAC2Gadget" in r.detail


def test_usbsink_card_active_missing_is_fail(monkeypatch, tmp_path):
    _patch_active(monkeypatch, True)
    nonexistent = tmp_path / "missing"
    with patch.object(doctor, "Path") as mock_path:
        def _path(p):
            if p == "/proc/asound/UAC2Gadget":
                return nonexistent
            return Path(p)
        mock_path.side_effect = _path
        r = doctor.check_usbsink_card()
    assert r.status == "fail"
    assert "init" in r.detail.lower() or "missing" in r.detail.lower()
