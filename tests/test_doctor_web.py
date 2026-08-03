# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor web domain."""

from pathlib import Path


from jasper.cli import doctor


from .doctor_test_support import (
    _registered_check_names,
)


def test_web_design_assets_warns_when_manifest_missing(
    monkeypatch,
    tmp_path: Path,
):
    """No manifest = unverifiable tree — warn, never guess from a stale
    built-in list (which could pass a partially-deployed tree as green)."""
    assets = tmp_path / "assets"
    assets.mkdir(parents=True)
    (assets / "app.css").write_text("/* css */")
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(tmp_path))
    r = doctor.check_web_design_assets()
    assert r.status == "warn"
    assert ".install-manifest" in r.detail
    assert "redeploy" in r.detail


def _manifest_fixture(tmp_path: Path, entries: list[str]) -> Path:
    """Lay down app.css plus a manifest listing `entries`."""
    assets = tmp_path / "assets"
    assets.mkdir(parents=True)
    (assets / "app.css").write_text("/* css */")
    (assets / ".install-manifest").write_text("\n".join(entries) + "\n")
    return assets


def test_web_design_assets_verifies_every_manifest_entry(
    monkeypatch,
    tmp_path: Path,
):
    """With the installer-written manifest present, the check covers the
    full installed tree — no hand list involved."""
    assets = _manifest_fixture(
        tmp_path, ["wifi/wifi.css", "wifi/js/main.js", "shared/js/escape.js"]
    )
    for rel in ("wifi/wifi.css", "wifi/js/main.js", "shared/js/escape.js"):
        target = assets / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("// asset")
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(tmp_path))
    r = doctor.check_web_design_assets()
    assert r.status == "ok"
    assert "4 assets verified" in r.detail  # app.css + 3 manifest entries
    assert ".install-manifest" in r.detail


def test_web_design_assets_warns_on_missing_manifest_entry(
    monkeypatch,
    tmp_path: Path,
):
    assets = _manifest_fixture(tmp_path, ["wake/js/main.js", "wake/wake.css"])
    (assets / "wake").mkdir(parents=True)
    (assets / "wake" / "wake.css").write_text("/* css */")
    # wake/js/main.js deliberately absent — the page would load blank.
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(tmp_path))
    r = doctor.check_web_design_assets()
    assert r.status == "warn"
    assert "wake/js/main.js" in r.detail


def test_web_design_assets_ignores_malformed_manifest_lines(
    monkeypatch,
    tmp_path: Path,
):
    """One bad byte in the manifest must not distort the check."""
    assets = _manifest_fixture(
        tmp_path,
        ["", "# comment", "/etc/passwd", "a/../../escape", "voice/js/main.js"],
    )
    (assets / "voice" / "js").mkdir(parents=True)
    (assets / "voice" / "js" / "main.js").write_text("// module")
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(tmp_path))
    r = doctor.check_web_design_assets()
    assert r.status == "ok", r.detail
    assert "2 assets verified" in r.detail  # app.css + the one sane entry


def test_web_design_assets_caps_the_missing_list(monkeypatch, tmp_path: Path):
    """A wiped asset tree warns with a bounded list, not journal spam."""
    _manifest_fixture(tmp_path, [f"page{i}/js/main.js" for i in range(20)])
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(tmp_path))
    r = doctor.check_web_design_assets()
    assert r.status == "warn"
    assert "(+8 more)" in r.detail
    assert r.detail.count("js/main.js") == 12


def test_web_design_assets_warns_when_stylesheet_missing(
    monkeypatch,
    tmp_path: Path,
):
    """app.css is pinned explicitly even if a manifest omits it — it is
    the design system itself."""
    assets = tmp_path / "assets"
    assets.mkdir(parents=True)
    (assets / ".install-manifest").write_text("voice/js/main.js\n")
    (assets / "voice" / "js").mkdir(parents=True)
    (assets / "voice" / "js" / "main.js").write_text("// module")
    # No app.css written — the design system can't load.
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(tmp_path))
    r = doctor.check_web_design_assets()
    assert r.status == "warn"
    assert "assets/app.css" in r.detail


def test_web_design_assets_skips_when_not_installed(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(tmp_path / "nope"))
    r = doctor.check_web_design_assets()
    assert r.status == "ok"
    assert "not installed" in r.detail


def test_web_design_assets_check_registered():
    assert "check_web_design_assets" in _registered_check_names()
