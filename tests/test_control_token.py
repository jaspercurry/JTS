"""Unit tests for the opt-in control-token core + CLI.

The token gates jasper-control's four high-impact mutations behind an
X-JTS-Token header — but only when a non-empty token file exists
(default-off). These tests pin: enforced detection (absent / empty /
present), constant-time verify semantics, and the enable/show/disable
CLI (including the 0600 mode and the refuse-to-clobber guard). The
route-level HTTP behaviour is covered separately in
test_control_server.py against the real ThreadingHTTPServer.
"""
from __future__ import annotations

import inspect
import os
import stat

from jasper.cli import control_token as cli
from jasper.control import control_token


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


def test_verify_uses_constant_time_compare():
    """The compare must be hmac.compare_digest, never ==, so the token's
    length/prefix doesn't leak through timing."""
    src = inspect.getsource(control_token.verify)
    assert "compare_digest" in src
    assert "==" not in src.replace("!=", "")  # no equality compare of the secret


# --- CLI: enable / show / disable -----------------------------------------


def _point_cli_at(monkeypatch, path):
    monkeypatch.setattr(control_token, "TOKEN_FILE", str(path))


def test_cli_enable_writes_0600_token(monkeypatch, tmp_path, capsys):
    path = tmp_path / "control_token"
    _point_cli_at(monkeypatch, path)
    rc = cli.main(["--enable"])
    assert rc == 0
    assert path.exists()
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
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
