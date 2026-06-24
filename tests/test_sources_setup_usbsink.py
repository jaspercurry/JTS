# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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


def test_index_html_uses_shared_toggle_markup_and_csrf_meta():
    html = sources_setup._index_html("csrf-token").decode("utf-8")

    assert 'meta name="jts-csrf" content="csrf-token"' in html
    assert html.count('class="toggle"') == 4
    assert 'id="t-airplay"' in html
    assert 'id="t-bluetooth"' in html
    assert "{toggle_" not in html
    # Behaviour now ships as an ES module (post canonical-design migration);
    # the page references it and the jsonHeaders() CSRF plumbing lives there.
    assert '<script type="module" src="/assets/sources/js/main.js">' in html


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
    monkeypatch.setattr(sources_setup, "_local_sources_allowed", lambda: True)
    monkeypatch.setattr(sources_setup, "_unit_available", lambda unit: True)
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
    monkeypatch.setattr(sources_setup, "_local_sources_allowed", lambda: True)
    monkeypatch.setattr(sources_setup, "_unit_available", lambda unit: True)
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


def test_gather_state_usbsink_unavailable_when_init_unit_missing(monkeypatch):
    monkeypatch.setattr(sources_setup, "_local_sources_allowed", lambda: True)

    def available(unit: str) -> bool:
        return unit != sources_setup.USBSINK_INIT_UNIT

    monkeypatch.setattr(sources_setup, "_unit_available", available)
    monkeypatch.setattr(
        sources_setup,
        "_systemctl",
        lambda *args, **kw: (0, "inactive"),
    )

    async def fake_bt():
        return (False, False, False)

    monkeypatch.setattr(sources_setup, "_bt_state", fake_bt)
    monkeypatch.setattr(sources_setup, "_usbsink_available", lambda: True)

    state = sources_setup._gather_state()
    assert state["usbsink"]["available"] is False
    assert "init unit" in state["usbsink"]["unavailableReason"]


# ----------------------------------------------------------------------
# _apply — systemctl drives the right unit
# ----------------------------------------------------------------------


def test_apply_usbsink_enable_routes_through_broker(monkeypatch):
    # WS1 Phase 3: _set_unit routes enable/disable through jasper-control's
    # restart broker (manage_units), not a direct systemctl.
    calls = []
    monkeypatch.setattr(sources_setup, "_local_sources_allowed", lambda: True)
    monkeypatch.setattr(sources_setup, "_unit_available", lambda unit: True)
    monkeypatch.setattr(sources_setup, "_usbsink_available", lambda: True)
    monkeypatch.setattr(
        sources_setup, "manage_units",
        lambda *units, **kw: calls.append((units, kw.get("verb"))) or {"ok": True},
    )
    sources_setup._apply("usbsink", True)
    assert calls
    # enable+start of the intent unit; init.service follows via Requires=.
    assert calls[0] == ((sources_setup.USBSINK_UNIT,), "enable-now")
    assert not any(c[0] == (sources_setup.USBSINK_INIT_UNIT,) for c in calls)


def test_apply_usbsink_disable_routes_through_broker(monkeypatch):
    calls = []
    monkeypatch.setattr(sources_setup, "_local_sources_allowed", lambda: True)
    monkeypatch.setattr(sources_setup, "_unit_available", lambda unit: True)
    monkeypatch.setattr(sources_setup, "_usbsink_available", lambda: True)
    monkeypatch.setattr(
        sources_setup, "manage_units",
        lambda *units, **kw: calls.append((units, kw.get("verb"))) or {"ok": True},
    )
    sources_setup._apply("usbsink", False)
    assert calls
    assert calls[0] == ((sources_setup.USBSINK_UNIT,), "disable-now")
    assert calls[1] == ((sources_setup.USBSINK_INIT_UNIT,), "stop")


def test_apply_usbsink_rejects_missing_init_unit(monkeypatch):
    monkeypatch.setattr(sources_setup, "_local_sources_allowed", lambda: True)

    def available(unit: str) -> bool:
        return unit != sources_setup.USBSINK_INIT_UNIT

    monkeypatch.setattr(sources_setup, "_unit_available", available)
    monkeypatch.setattr(sources_setup, "_usbsink_available", lambda: True)

    try:
        sources_setup._apply("usbsink", True)
    except RuntimeError as e:
        assert "init unit" in str(e)
    else:  # pragma: no cover - assertion shape
        raise AssertionError("missing init unit should reject USB toggle")
