# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""install_web_assets (deploy/lib/install/web-assets.sh).

The /assets/ copy (app.css, fonts, per-page CSS + ES modules) + the
.install-manifest contract that jasper-doctor's check_web_design_assets
verifies file-by-file. Run the real bash function against a sandbox
repo/web root — same extraction pattern as
test_install_voice_provider_migration.py.
"""
from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_ASSETS_LIB = ROOT / "deploy" / "lib" / "install" / "web-assets.sh"
DOCTOR_WEB = ROOT / "jasper" / "cli" / "doctor" / "web.py"
ASSETS_DIR = ROOT / "deploy" / "assets"

MANIFEST_NAME = ".install-manifest"


def _extract_function() -> str:
    helper = subprocess.run(
        [
            "bash",
            "-c",
            rf"sed -n '/^install_web_assets()/,/^}}/p' '{WEB_ASSETS_LIB}'",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "install_web_assets()" in helper
    return helper


def _run(repo_dir: Path, web_root: Path) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "REPO_DIR": str(repo_dir),
        "JASPER_WEB_SHARE_DIR": str(web_root),
    }
    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            f"set -euo pipefail\n{_extract_function()}\ninstall_web_assets",
        ],
        env=env,
        capture_output=True,
        text=True,
    )


def _fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    assets = repo / "deploy" / "assets"
    (assets / "alpha" / "js").mkdir(parents=True)
    (assets / "app.css").write_text("/* design system */")
    (assets / "alpha" / "alpha.css").write_text("/* css */")
    (assets / "alpha" / "js" / "main.js").write_text("// module")
    (assets / "alpha" / "js" / "extra.js").write_text("// secondary module")
    (assets / "beta").mkdir(parents=True)
    (assets / "beta" / "beta.css").write_text("/* css only */")
    (assets / "shared" / "js").mkdir(parents=True)
    (assets / "shared" / "js" / "escape.js").write_text("// shared")
    (assets / "fonts").mkdir(parents=True)
    (assets / "fonts" / "font.woff2").write_text("not really a font")
    (assets / "fonts" / "OFL.txt").write_text("license rides with the font")
    return repo


def test_copies_assets_and_writes_exact_sorted_manifest(tmp_path: Path):
    repo = _fake_repo(tmp_path)
    web_root = tmp_path / "web"
    r = _run(repo, web_root)
    assert r.returncode == 0, r.stderr

    expected = [
        "alpha/alpha.css",
        "alpha/js/extra.js",
        "alpha/js/main.js",
        "app.css",
        "beta/beta.css",
        "fonts/OFL.txt",
        "fonts/font.woff2",
        "shared/js/escape.js",
    ]
    assets = web_root / "assets"
    for rel in expected:
        assert (assets / rel).is_file(), f"{rel} not installed"

    manifest = (assets / MANIFEST_NAME).read_text().splitlines()
    assert manifest == sorted(expected)


def test_empty_page_dir_is_tolerated_under_strict_mode(tmp_path: Path):
    """compgen guards the globs: an asset dir with no css/js must not
    abort the deploy under set -euo pipefail (the documented contract)."""
    repo = _fake_repo(tmp_path)
    (repo / "deploy" / "assets" / "empty-page").mkdir()
    web_root = tmp_path / "web"
    r = _run(repo, web_root)
    assert r.returncode == 0, r.stderr
    manifest = (web_root / "assets" / MANIFEST_NAME).read_text()
    assert "empty-page" not in manifest
    # The page dir itself is still created — matches the historical loop.
    assert (web_root / "assets" / "empty-page").is_dir()


def test_manifest_name_parity_between_installer_and_doctor():
    """The installer writes and the doctor reads the same literal name."""
    assert MANIFEST_NAME in WEB_ASSETS_LIB.read_text(encoding="utf-8")
    assert MANIFEST_NAME in DOCTOR_WEB.read_text(encoding="utf-8")


def test_manifest_lands_atomically_and_orphaned_temps_are_swept(tmp_path: Path):
    """The manifest is temp-written next to itself and renamed into place.

    A killed install must never leave a truncated manifest for the doctor
    to trust, and a temp orphaned by that kill is swept on the next run
    rather than accumulating.
    """
    repo = _fake_repo(tmp_path)
    web_root = tmp_path / "web"
    assets = web_root / "assets"
    assets.mkdir(parents=True)
    orphan = assets / f"{MANIFEST_NAME}.tmp.stale"
    orphan.write_text("left behind by a killed install")

    r = _run(repo, web_root)
    assert r.returncode == 0, r.stderr

    assert not orphan.exists()
    assert (assets / MANIFEST_NAME).is_file()
    leftovers = [p.name for p in assets.glob(f"{MANIFEST_NAME}.tmp.*")]
    assert leftovers == []
    mode = stat.S_IMODE((assets / MANIFEST_NAME).stat().st_mode)
    assert mode == 0o644


def test_every_repo_asset_matches_the_copy_shape():
    """Every file under deploy/assets/ must be copyable by the function.

    The contract is root app.css, fonts/*, and per-page root *.css +
    js/*.js. A file outside that shape (a nested module, a stray .svg)
    would be silently skipped — never installed, never manifested —
    which is exactly the silent-404 class the manifest exists to kill,
    so fail in CI instead.
    """
    offenders: list[str] = []
    for path in sorted(ASSETS_DIR.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(ASSETS_DIR)
        parts = rel.parts
        if parts == ("app.css",):
            continue
        if parts[0] == "fonts" and len(parts) == 2:
            continue
        if len(parts) == 2 and parts[1].endswith(".css"):
            continue
        if len(parts) == 3 and parts[1] == "js" and parts[2].endswith(".js"):
            continue
        offenders.append(str(rel))
    assert not offenders, (
        "asset(s) outside the installer's copy shape (root app.css, "
        f"fonts/*, per-page *.css or js/*.js) would never reach the Pi: "
        f"{offenders}; extend deploy/lib/install/web-assets.sh if the "
        "shape must grow"
    )


def test_install_sh_sources_and_calls_the_helper():
    install_sh = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert "deploy/lib/install/web-assets.sh" in install_sh
    assert re.search(r"^\s*install_web_assets\b", install_sh, re.M)


def test_real_repo_assets_round_trip_through_doctor(monkeypatch, tmp_path: Path):
    """Installer writes, doctor reads — against the real asset tree.

    Runs the actual bash function over deploy/assets/ into a tmp web
    root, then points check_web_design_assets at it. Catches any drift
    between the manifest the bash writes and the format the Python
    parses that the unit tests (each faking one side) cannot.
    """
    from jasper.cli.doctor import web as doctor_web

    web_root = tmp_path / "web"
    r = _run(ROOT, web_root)
    assert r.returncode == 0, r.stderr

    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(web_root))
    result = doctor_web.check_web_design_assets()
    assert result.status == "ok", result.detail
    assert MANIFEST_NAME in result.detail

    manifest = (web_root / "assets" / MANIFEST_NAME).read_text().splitlines()
    # Spot-pin the load-bearing entries: the design system, the fonts it
    # @font-faces, and the shared modules every page hard-imports.
    assert "app.css" in manifest
    for woff in sorted(
        p.name for p in (ASSETS_DIR / "fonts").glob("*.woff2")
    ):
        assert f"fonts/{woff}" in manifest
    for shared in sorted(
        p.name for p in (ASSETS_DIR / "shared" / "js").glob("*.js")
    ):
        assert f"shared/js/{shared}" in manifest
