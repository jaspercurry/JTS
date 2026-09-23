# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the control-token core + CLI.

The token gates jasper-control's high-impact mutations behind an X-JTS-Token
header. The primitive still fails open when no non-empty token file exists, while
production startup ensures one automatically. These tests pin: enforced detection
(absent / empty / present), constant-time verify semantics, and the
enable/show/disable CLI (including the 0640 group-jasper mode, the
refuse-to-clobber guard, and the installed landing page following a change).
The route-level HTTP behaviour is covered separately in test_control_server.py
against the real ThreadingHTTPServer.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from jasper.cli import control_token as cli
from jasper.control import control_token
from jasper.install_profile import system_capabilities_for_profile
from jasper.web import chrome
from jasper.web.landing import render_landing
from tests._web_test_helpers import assert_verify_uses_constant_time_compare

# An installed landing page around its token meta, carrying bytes a text round
# trip or a loose match would alter: CRLF, non-ASCII, a second content=.
INSTALLED_LANDING = (
    '<meta name="viewport" content="width=device-width">\r\n'
    '  <meta name="jts-control-token" content="{}">\n'
    "  <title>JTS — home</title>\n"
)
LANDING_TEMPLATE = Path(__file__).resolve().parents[1] / "deploy" / "index.html"


# --- core: token_enforced / verify ----------------------------------------


def test_not_enforced_when_file_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(control_token, "TOKEN_FILE", str(tmp_path / "nope"))
    assert control_token.token_enforced() is False


def test_not_enforced_when_file_empty_or_whitespace(monkeypatch, tmp_path):
    path = tmp_path / "control_token"
    path.write_text("   \n\t\n")  # whitespace-only strips to ""
    monkeypatch.setattr(control_token, "TOKEN_FILE", str(path))
    assert control_token.token_enforced() is False


def test_enforced_when_file_has_content(monkeypatch, tmp_path):
    path = tmp_path / "control_token"
    path.write_text("s3cr3t\n")
    monkeypatch.setattr(control_token, "TOKEN_FILE", str(path))
    assert control_token.token_enforced() is True


def test_verify_default_off_allows_everything(monkeypatch, tmp_path):
    """No token file -> verify() is True for any input (incl. None). This is
    the default-off invariant the rest of the system relies on."""
    monkeypatch.setattr(control_token, "TOKEN_FILE", str(tmp_path / "nope"))
    assert control_token.verify(None) is True
    assert control_token.verify("") is True
    assert control_token.verify("anything") is True


def test_verify_enforced_exact_match(monkeypatch, tmp_path):
    path = tmp_path / "control_token"
    # Trailing newline on disk must not break the compare.
    path.write_text("the-token-value\n")
    monkeypatch.setattr(control_token, "TOKEN_FILE", str(path))
    assert control_token.verify("the-token-value") is True


def test_verify_enforced_mismatch_and_missing_header(monkeypatch, tmp_path):
    path = tmp_path / "control_token"
    path.write_text("the-token-value")
    monkeypatch.setattr(control_token, "TOKEN_FILE", str(path))
    assert control_token.verify("wrong") is False
    assert control_token.verify(None) is False
    assert control_token.verify("") is False


def test_verify_uses_constant_time_compare(monkeypatch, tmp_path):
    """compare_digest, never ==, so the token length/prefix cannot leak via timing."""
    assert_verify_uses_constant_time_compare(
        monkeypatch, tmp_path, control_token, "TOKEN_FILE", "the-token-value"
    )


# --- CLI: enable / show / disable -----------------------------------------


def _point_cli_at(monkeypatch, path):
    monkeypatch.setattr(control_token, "TOKEN_FILE", str(path))
    monkeypatch.setattr(cli, "LANDING_PAGE", path.parent / "index.html")


def test_cli_enable_writes_0640_token(monkeypatch, tmp_path, capsys):
    path = tmp_path / "control_token"
    _point_cli_at(monkeypatch, path)
    rc = cli.main(["--enable"])
    assert rc == 0
    assert path.exists()
    # 0640 group jasper so the non-root jasper-control/jasper-web can read the
    # gate token — an unreadable token fails safe to gate-OFF, silently
    # disabling the mandatory gate.
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o640, f"expected 0640, got {oct(mode)}"
    token = path.read_text().strip()
    assert len(token) >= 32  # token_urlsafe(32) -> ~43 chars
    out = capsys.readouterr().out.strip()
    assert out == token  # the token is printed to stdout


def test_cli_enable_refuses_to_clobber_without_force(monkeypatch, tmp_path):
    path = tmp_path / "control_token"
    _point_cli_at(monkeypatch, path)
    assert cli.main(["--enable"]) == 0
    first = path.read_text()
    # Second --enable without --force must refuse and leave the token intact.
    assert cli.main(["--enable"]) == 1
    assert path.read_text() == first


def test_cli_enable_force_overwrites(monkeypatch, tmp_path):
    path = tmp_path / "control_token"
    _point_cli_at(monkeypatch, path)
    assert cli.main(["--enable"]) == 0
    first = path.read_text()
    assert cli.main(["--enable", "--force"]) == 0
    assert path.read_text() != first  # a fresh token was generated


def test_cli_show_prints_token(monkeypatch, tmp_path, capsys):
    path = tmp_path / "control_token"
    _point_cli_at(monkeypatch, path)
    cli.main(["--enable"])
    token = path.read_text().strip()
    capsys.readouterr()  # drain the enable output
    assert cli.main(["--show"]) == 0
    assert capsys.readouterr().out.strip() == token


def test_cli_show_when_disabled_says_disabled(monkeypatch, tmp_path, capsys):
    _point_cli_at(monkeypatch, tmp_path / "nope")
    assert cli.main(["--show"]) == 0
    assert "disabled" in capsys.readouterr().out.lower()


def test_cli_disable_removes_file(monkeypatch, tmp_path):
    path = tmp_path / "control_token"
    _point_cli_at(monkeypatch, path)
    cli.main(["--enable"])
    assert path.exists()
    assert cli.main(["--disable"]) == 0
    assert not path.exists()
    assert control_token.token_enforced() is False


def test_cli_disable_when_already_off_is_noop(monkeypatch, tmp_path, capsys):
    _point_cli_at(monkeypatch, tmp_path / "nope")
    assert cli.main(["--disable"]) == 0
    assert "already disabled" in capsys.readouterr().out.lower()


@pytest.mark.parametrize("installed", [True, False], ids=["landing", "no-landing"])
@pytest.mark.parametrize(
    "argv",
    [["--enable"], ["--enable", "--force"], ["--disable"]],
    ids=["enable", "enable-force", "disable"],
)
def test_cli_token_change_reaches_the_installed_landing_page(
    monkeypatch, tmp_path, capsys, argv, installed
):
    """nginx serves the landing page from disk with the token baked in, so a
    change must land there too or its Pause button is refused until the next
    deploy; with no landing page the token change itself is unaffected."""
    token_file = tmp_path / "control_token"
    page = tmp_path / "index.html"
    _point_cli_at(monkeypatch, token_file)
    if argv != ["--enable"]:
        token_file.write_text("old-token\n")
    if installed:
        page.write_bytes(INSTALLED_LANDING.format("old-token").encode())
        page.chmod(0o640)  # not the writer's 0644 default, so a kept mode shows

    assert cli.main(argv) == 0

    if argv == ["--disable"]:
        assert not token_file.exists()
        token = ""
    else:
        token = token_file.read_text().strip()
        assert token not in ("", "old-token")
    if installed:
        assert page.read_bytes() == INSTALLED_LANDING.format(token).encode()
        assert stat.S_IMODE(page.stat().st_mode) == 0o640
    else:
        assert not page.exists()
    err = capsys.readouterr().err
    assert not any(t in err for t in ("old-token", token) if t)


def test_cli_rewrite_of_the_shipped_landing_page_matches_a_fresh_render(
    monkeypatch, tmp_path
):
    """The CLI finds deploy/index.html's token meta by pattern, so the tag must
    keep the shape that pattern reads, around a token the renderer escaped."""
    token_file = tmp_path / "control_token"
    page = tmp_path / "index.html"
    _point_cli_at(monkeypatch, token_file)
    token_file.write_text("\"&<>'\n")
    template = LANDING_TEMPLATE.read_text(encoding="utf-8")

    def render() -> bytes:
        return render_landing(
            template,
            app_css_version="abc1234",
            caps=system_capabilities_for_profile("full"),
            control_token=control_token.ensure_token(),
        ).encode("utf-8")

    installed = render()
    page.write_bytes(installed)

    assert cli.main(["--enable", "--force"]) == 0

    rotated = render()
    assert rotated != installed
    assert page.read_bytes() == rotated


# --- ensure_token() makes the gate mandatory + invisible. -----------------


def test_ensure_token_generates_when_absent(monkeypatch, tmp_path):
    path = tmp_path / "control_token"
    monkeypatch.setattr(control_token, "TOKEN_FILE", str(path))
    assert control_token.token_enforced() is False
    token = control_token.ensure_token()
    assert token and len(token) >= 16
    # Now the gate is armed: the file exists with the generated token.
    assert path.read_text().strip() == token
    assert control_token.token_enforced() is True
    assert control_token.verify(token) is True
    assert control_token.verify("nope") is False


def test_ensure_token_is_0640(monkeypatch, tmp_path):
    path = tmp_path / "control_token"
    monkeypatch.setattr(control_token, "TOKEN_FILE", str(path))
    control_token.ensure_token()
    # 0640 group jasper. The non-root jasper-control may not OWN this file
    # (StateDirectory recursive-chown can make the owner jasper-voice), and
    # jasper-web reads it via canonical_page() — group read is what keeps the
    # mandatory gate from silently fail-OFF'ing post-drop.
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o640, f"token file is {oct(mode)}, expected 0o640"


def test_ensure_token_is_idempotent_and_never_rotates(monkeypatch, tmp_path):
    path = tmp_path / "control_token"
    path.write_text("household-set-token\n")
    monkeypatch.setattr(control_token, "TOKEN_FILE", str(path))
    # An existing token (operator-set or previously generated) is returned
    # unchanged — never rotated out from under a stored browser copy.
    assert control_token.ensure_token() == "household-set-token"
    assert control_token.ensure_token() == "household-set-token"
    assert path.read_text().strip() == "household-set-token"


def test_current_token_matches_verify_path(monkeypatch, tmp_path):
    path = tmp_path / "control_token"
    monkeypatch.setattr(control_token, "TOKEN_FILE", str(path))
    assert control_token.current_token() == ""  # absent -> empty, no raise
    token = control_token.ensure_token()
    assert control_token.current_token() == token


def test_canonical_page_embeds_token_meta_only_when_present(monkeypatch, tmp_path):
    """canonical_page auto-delivers the token as a meta tag once it exists, and
    emits nothing while the gate is off (pages stay byte-identical)."""
    path = tmp_path / "control_token"
    monkeypatch.setattr(control_token, "TOKEN_FILE", str(path))

    off = chrome.canonical_page("T", "<main>x</main>").decode()
    assert "jts-control-token" not in off

    token = control_token.ensure_token()
    on = chrome.canonical_page("T", "<main>x</main>").decode()
    assert f'<meta name="jts-control-token" content="{token}">' in on
