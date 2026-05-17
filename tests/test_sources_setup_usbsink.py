"""Tests for the USB-sink toggle on /sources/.

Covers:
- VALID_SOURCES contains 'usbsink'
- _usbsink_available() reads config.txt correctly
- _gather_state() returns the right shape for usbsink
- _apply('usbsink', ...) drives systemctl with the right unit

The systemctl side is tested via monkeypatch on _systemctl; we don't
exec real systemd in unit tests.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from jasper.web import sources_setup


# ----------------------------------------------------------------------
# VALID_SOURCES inclusion
# ----------------------------------------------------------------------


def test_usbsink_in_valid_sources():
    assert "usbsink" in sources_setup.VALID_SOURCES
    # Should be the only new addition — keep the surface tight.
    assert set(sources_setup.VALID_SOURCES) == {
        "airplay", "bluetooth", "spotify_connect", "usbsink",
    }


# ----------------------------------------------------------------------
# _usbsink_available — dtoverlay presence in config.txt
# ----------------------------------------------------------------------


def _patch_config(monkeypatch, content: str | None):
    """Helper that points _usbsink_available at a controlled config.txt
    (or simulates a missing file by setting content=None)."""
    if content is None:
        monkeypatch.setattr(
            "builtins.open",
            lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()),
        )
        return
    import io
    monkeypatch.setattr(
        sources_setup, "BOOT_CONFIG_PATH", "/tmp/test-config.txt",
    )
    # Use a function-scoped tempfile-like wrapper via monkeypatch on
    # `open`. Simpler: actually write to a real path.
    pass


def test_usbsink_available_returns_true_when_dtoverlay_present(monkeypatch, tmp_path):
    cfg = tmp_path / "config.txt"
    cfg.write_text(
        "# JTS install\n"
        "[pi5]\n"
        "dtoverlay=dwc2,dr_mode=peripheral\n"
        "country=US\n"
    )
    monkeypatch.setattr(sources_setup, "BOOT_CONFIG_PATH", str(cfg))
    assert sources_setup._usbsink_available() is True


def test_usbsink_available_tolerates_leading_whitespace(monkeypatch, tmp_path):
    cfg = tmp_path / "config.txt"
    cfg.write_text("    dtoverlay=dwc2,dr_mode=peripheral\n")
    monkeypatch.setattr(sources_setup, "BOOT_CONFIG_PATH", str(cfg))
    assert sources_setup._usbsink_available() is True


def test_usbsink_available_returns_false_when_dtoverlay_missing(monkeypatch, tmp_path):
    cfg = tmp_path / "config.txt"
    cfg.write_text(
        "# JTS install\n"
        "[pi5]\n"
        "country=US\n"  # no dtoverlay line
    )
    monkeypatch.setattr(sources_setup, "BOOT_CONFIG_PATH", str(cfg))
    assert sources_setup._usbsink_available() is False


def test_usbsink_available_does_not_match_commented_line(monkeypatch, tmp_path):
    """A `#dtoverlay=...` (commented) shouldn't be treated as enabled.
    Without this check, an operator's experimental comment-out leaves
    the wizard claiming the feature is ready when it actually isn't."""
    cfg = tmp_path / "config.txt"
    cfg.write_text("# dtoverlay=dwc2,dr_mode=peripheral\n")
    monkeypatch.setattr(sources_setup, "BOOT_CONFIG_PATH", str(cfg))
    assert sources_setup._usbsink_available() is False


def test_usbsink_available_returns_false_on_missing_file(monkeypatch, tmp_path):
    """Not running on a Pi? config.txt won't exist. Fail-soft to
    `available=false` so the toggle is disabled."""
    monkeypatch.setattr(
        sources_setup, "BOOT_CONFIG_PATH", str(tmp_path / "missing.txt"),
    )
    assert sources_setup._usbsink_available() is False


# ----------------------------------------------------------------------
# _gather_state — usbsink row shape
# ----------------------------------------------------------------------


def test_gather_state_includes_usbsink(monkeypatch):
    """Verify the usbsink key is present with `enabled` + `available`.
    Mock systemctl and the BT probe so we get deterministic output."""
    monkeypatch.setattr(
        sources_setup, "_systemctl",
        lambda *args, **kw: (0, "inactive"),
    )
    # BT side: pretend no hardware.
    async def fake_bt():
        return (False, False, False)
    monkeypatch.setattr(sources_setup, "_bt_state", fake_bt)
    # USB available probe: return False (not on a Pi).
    monkeypatch.setattr(
        sources_setup, "_usbsink_available", lambda: False,
    )

    state = sources_setup._gather_state()
    assert "usbsink" in state
    assert state["usbsink"]["enabled"] is False
    assert state["usbsink"]["available"] is False


def test_gather_state_usbsink_available_when_dtoverlay_set(monkeypatch):
    """When dtoverlay is present, available should be True regardless
    of unit enabled state."""
    monkeypatch.setattr(
        sources_setup, "_systemctl",
        lambda *args, **kw: (0, "inactive"),
    )
    async def fake_bt():
        return (False, False, False)
    monkeypatch.setattr(sources_setup, "_bt_state", fake_bt)
    monkeypatch.setattr(sources_setup, "_usbsink_available", lambda: True)

    state = sources_setup._gather_state()
    assert state["usbsink"]["available"] is True


# ----------------------------------------------------------------------
# _apply — systemctl drives the right unit
# ----------------------------------------------------------------------


def test_apply_usbsink_enable_calls_systemctl(monkeypatch):
    calls = []
    monkeypatch.setattr(
        sources_setup, "_systemctl",
        lambda *args, **kw: calls.append(args) or (0, ""),
    )
    sources_setup._apply("usbsink", True)
    assert calls
    # enable+start of the main unit; init.service follows via the
    # PartOf/Requires chain.
    assert calls[0] == ("enable", sources_setup.USBSINK_UNIT, "--now")


def test_apply_usbsink_disable_calls_systemctl(monkeypatch):
    calls = []
    monkeypatch.setattr(
        sources_setup, "_systemctl",
        lambda *args, **kw: calls.append(args) or (0, ""),
    )
    sources_setup._apply("usbsink", False)
    assert calls
    assert calls[0] == ("disable", sources_setup.USBSINK_UNIT, "--now")
