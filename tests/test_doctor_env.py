# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor env domain."""

from pathlib import Path

import pytest

from jasper.cli import doctor
from jasper.cli.doctor.env import _classify_state_group_write

from .doctor_test_support import _registered_check_names


# ---------------------------------------------------------------- env loading


def test_parse_env_file_basic(tmp_path: Path):
    p = tmp_path / "jasper.env"
    p.write_text(
        "# comment line\n"
        "\n"
        "GEMINI_API_KEY=AIzaSyABC\n"
        "JASPER_VOICE_PROVIDER=openai\n"
        'OPENAI_API_KEY="sk-quoted"\n'
        "EMPTY=\n"
        "  WHITESPACE_KEY  =  trimmed  \n"
    )
    out = doctor._parse_env_file(str(p))
    assert out["GEMINI_API_KEY"] == "AIzaSyABC"
    assert out["JASPER_VOICE_PROVIDER"] == "openai"
    assert out["OPENAI_API_KEY"] == "sk-quoted"
    assert out["EMPTY"] == ""
    assert out["WHITESPACE_KEY"] == "trimmed"


def test_parse_env_file_missing_returns_empty(tmp_path: Path):
    out = doctor._parse_env_file(str(tmp_path / "does-not-exist"))
    assert out == {}


def test_grouping_env_parser_uses_canonical_quote_handling():
    out = doctor.grouping._parse_env_file(
        '# comment\nJASPER_GROUPING_ROLE="leader"\nEMPTY=\n',
    )

    assert out == {
        "JASPER_GROUPING_ROLE": "leader",
        "EMPTY": "",
    }


def test_read_env_file_state_reports_loaded_and_missing(tmp_path: Path):
    from jasper.env_load import read_env_file_state

    p = tmp_path / "jasper.env"
    p.write_text("JASPER_HOSTNAME=jts.local\n")

    loaded = read_env_file_state(str(p))
    assert loaded.status == "loaded"
    assert loaded.loaded
    assert loaded.values["JASPER_HOSTNAME"] == "jts.local"

    missing = read_env_file_state(str(tmp_path / "missing.env"))
    assert missing.status == "missing"
    assert missing.values == {}


def test_read_env_file_state_reports_unreadable(monkeypatch, tmp_path: Path):
    import jasper.env_load as env_load
    from jasper.env_load import read_env_file_state

    p = tmp_path / "jasper.env"
    p.write_text("JASPER_HOSTNAME=jts.local\n")

    def boom(self):
        raise PermissionError("blocked")

    monkeypatch.setattr(env_load.Path, "read_text", boom)

    state = read_env_file_state(str(p))
    assert state.status == "unreadable"
    assert state.values == {}
    assert "PermissionError" in state.error


def test_load_env_files_wizard_overrides_operator(monkeypatch, tmp_path: Path):
    """`/var/lib/jasper/voice_provider.env` (wizard) must override
    `/etc/jasper/jasper.env` (operator) — same precedence as the
    systemd unit's `EnvironmentFile=` ordering. Verified via the
    explicit-paths form of `load_env_files` so test fixtures don't
    have to monkeypatch a module-level constant."""
    from jasper.env_load import load_env_files

    operator = tmp_path / "jasper.env"
    operator.write_text("GEMINI_API_KEY=op-key\nJASPER_VOICE_PROVIDER=gemini\n")
    wizard = tmp_path / "voice_provider.env"
    wizard.write_text("OPENAI_API_KEY=wiz-key\nJASPER_VOICE_PROVIDER=openai\n")
    for var in ("GEMINI_API_KEY", "OPENAI_API_KEY", "JASPER_VOICE_PROVIDER"):
        monkeypatch.delenv(var, raising=False)

    load_env_files((str(operator), str(wizard)))

    assert os_environ_get("GEMINI_API_KEY") == "op-key"
    assert os_environ_get("OPENAI_API_KEY") == "wiz-key"
    assert os_environ_get("JASPER_VOICE_PROVIDER") == "openai"


def test_default_env_files_include_spotify_credentials_in_systemd_order():
    from jasper.env_load import ENV_FILES

    spotify_creds = "/var/lib/jasper-intsecrets/spotify_credentials.env"
    assert "/etc/jasper/jasper.env" in ENV_FILES
    assert spotify_creds in ENV_FILES
    assert ENV_FILES.index("/etc/jasper/jasper.env") < ENV_FILES.index(
        spotify_creds,
    )
    assert ENV_FILES.index(spotify_creds) < ENV_FILES.index(
        "/var/lib/jasper/voice_provider.env",
    )


def test_load_env_files_shell_wins_over_files(monkeypatch, tmp_path: Path):
    """A var already in the calling shell must NOT be overwritten by
    the env files. Lets an operator probe with `FOO=bar jasper-doctor`."""
    from jasper.env_load import load_env_files

    operator = tmp_path / "jasper.env"
    operator.write_text("JASPER_VOICE_PROVIDER=gemini\n")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "openai")

    load_env_files((str(operator),))
    assert os_environ_get("JASPER_VOICE_PROVIDER") == "openai"


def test_subprocess_env_with_fresh_files_overrides_stale_daemon_env(tmp_path: Path):
    """Long-lived daemons launching subprocesses need fresh wizard-file
    truth, not the process env captured when the daemon started."""
    from jasper.env_load import subprocess_env_with_fresh_files

    operator = tmp_path / "jasper.env"
    operator.write_text("JASPER_VOICE_PROVIDER=gemini\n")
    wizard = tmp_path / "voice_provider.env"
    wizard.write_text("JASPER_VOICE_PROVIDER=openai\nOPENAI_API_KEY=sk-fresh\n")

    env = subprocess_env_with_fresh_files(
        base={"PATH": "/bin", "JASPER_VOICE_PROVIDER": "gemini"},
        paths=(str(operator), str(wizard)),
    )

    assert env["PATH"] == "/bin"
    assert env["JASPER_VOICE_PROVIDER"] == "openai"
    assert env["OPENAI_API_KEY"] == "sk-fresh"


def os_environ_get(name: str) -> str | None:
    import os

    return os.environ.get(name)


# -------------------------------------------- shared-state group-writability
#
# The whole /var/lib/jasper state tree is group-`jasper` writable so every
# non-root daemon that writes it can. A file that regresses to group-read-only
# is the readonly-DB outage condition.


def _pretend_group_is_jasper(monkeypatch):
    """CI has no `jasper` group; resolve every gid to it."""
    import grp
    import types

    monkeypatch.setattr(
        grp, "getgrgid", lambda _gid: types.SimpleNamespace(gr_name="jasper")
    )


def test_state_group_write_no_files_is_ok(tmp_path):
    assert _classify_state_group_write(tmp_path / "usage.db").status == "ok"


@pytest.mark.parametrize(
    "name, mode, status",
    [
        ("usage.db", 0o660, "ok"),
        ("usage.db", 0o644, "warn"),
        # Readable by /chat but not writable by jasper-voice.
        ("conversation_history.db", 0o640, "warn"),
    ],
    ids=["group-writable", "group-readonly", "history-group-readonly"],
)
def test_state_group_write_verdicts(monkeypatch, tmp_path, name, mode, status):
    _pretend_group_is_jasper(monkeypatch)
    target = tmp_path / name
    target.write_text("x", encoding="utf-8")
    target.chmod(mode)

    res = _classify_state_group_write(tmp_path / "usage.db")

    assert res.status == status
    # A warn must name the offending file so the operator knows what to chmod.
    if status == "warn":
        assert name in res.detail


def test_state_group_write_check_is_registered():
    assert "check_state_dir_group_writable" in _registered_check_names()
