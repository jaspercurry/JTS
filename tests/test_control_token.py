"""Unit tests for the control-token core + CLI.

The token gates jasper-control's high-impact mutations behind an X-JTS-Token
header. The primitive still fails open when no non-empty token file exists, while
production startup ensures one automatically. These tests pin: enforced detection
(absent / empty / present), constant-time verify semantics, and the
enable/show/disable CLI (including the 0640 group-jasper mode and the
refuse-to-clobber guard). The route-level HTTP behaviour is covered separately
in test_control_server.py against the real ThreadingHTTPServer.
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


def test_cli_enable_writes_0640_token(monkeypatch, tmp_path, capsys):
    path = tmp_path / "control_token"
    _point_cli_at(monkeypatch, path)
    rc = cli.main(["--enable"])
    assert rc == 0
    assert path.exists()
    # WS1 Phase 3b-2: 0640 group jasper (was 0600) so the non-root
    # jasper-control/jasper-web can read the gate token — an unreadable token
    # fails safe to gate-OFF, silently disabling the mandatory gate.
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o640, f"expected 0640, got {oct(mode)}"
    token = path.read_text().strip()
    assert len(token) >= 32  # token_urlsafe(32) -> ~43 chars
    out = capsys.readouterr().out.strip()
    assert out == token  # the token is printed to stdout


def test_cli_enable_sets_token_group_to_parent_directory(
    monkeypatch, tmp_path, capsys,
):
    """A root-run rotate must not publish root:root 0640 in /var/lib/jasper.

    The runtime contract is group-read by the token directory's group (normally
    `jasper`), so the CLI must set that group before the atomic rename.
    """
    path = tmp_path / "control_token"
    _point_cli_at(monkeypatch, path)
    calls: list[tuple[str, int, int]] = []

    def fake_chown(target: str, uid: int, gid: int) -> None:
        calls.append((target, uid, gid))

    monkeypatch.setattr(cli.os, "chown", fake_chown)
    assert cli.main(["--enable"]) == 0
    capsys.readouterr()

    parent_gid = os.stat(tmp_path).st_gid
    assert calls, "CLI writer must set group before publishing the token"
    target, uid, gid = calls[-1]
    assert os.path.dirname(target) == str(tmp_path)
    assert os.path.basename(target).startswith(".control_token.")
    assert uid == -1
    assert gid == parent_gid


def test_cli_enable_cleans_temp_file_when_group_assignment_fails(
    monkeypatch, tmp_path,
):
    path = tmp_path / "control_token"
    _point_cli_at(monkeypatch, path)

    def fail_chown(_target: str, _uid: int, _gid: int) -> None:
        raise PermissionError("no group change")

    monkeypatch.setattr(cli.os, "chown", fail_chown)
    assert cli.main(["--enable"]) == 1
    assert not path.exists()
    assert list(tmp_path.glob(".control_token.*.tmp")) == []


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


# -------------------------------------------------------------------------
# WS1 Phase 2: ensure_token() makes the gate mandatory + invisible.
# -------------------------------------------------------------------------


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
    # WS1 Phase 3b-2: 0640 group jasper (was 0600). The non-root jasper-control
    # may not OWN this file (StateDirectory recursive-chown can make the owner
    # jasper-voice), and jasper-web reads it via canonical_page() — group read is
    # what keeps the mandatory gate from silently fail-OFF'ing post-drop.
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
    from jasper.web import _common

    path = tmp_path / "control_token"
    monkeypatch.setattr(control_token, "TOKEN_FILE", str(path))

    off = _common.canonical_page("T", "<main>x</main>").decode()
    assert "jts-control-token" not in off

    token = control_token.ensure_token()
    on = _common.canonical_page("T", "<main>x</main>").decode()
    assert f'<meta name="jts-control-token" content="{token}">' in on
