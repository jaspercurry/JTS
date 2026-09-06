# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor env domain."""

import os
from pathlib import Path

import pytest

from jasper.cli.doctor import _shared, env, grouping
from jasper.cli.doctor.env import _classify_state_group_write

from .doctor_test_support import _fresh_cfg, _pretend_group_is_jasper, _registered_check_names
from .test_secret_redaction import SECRET_ENV_NAMES


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
    out = _shared._parse_env_file(str(p))
    assert out["GEMINI_API_KEY"] == "AIzaSyABC"
    assert out["JASPER_VOICE_PROVIDER"] == "openai"
    assert out["OPENAI_API_KEY"] == "sk-quoted"
    assert out["EMPTY"] == ""
    assert out["WHITESPACE_KEY"] == "trimmed"


def test_parse_env_file_missing_returns_empty(tmp_path: Path):
    out = _shared._parse_env_file(str(tmp_path / "does-not-exist"))
    assert out == {}


def test_grouping_env_parser_uses_canonical_quote_handling():
    out = grouping._parse_env_file(
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


def os_environ_get(name: str) -> str | None:
    import os

    return os.environ.get(name)


# -------------------------------------------------- env-file secret posture


@pytest.mark.parametrize(
    "key, value, status",
    [(k, "notasecretvalue", "fail") for k in SECRET_ENV_NAMES]
    + [(k, "", "ok") for k in SECRET_ENV_NAMES],
    ids=[f"{k}-nonempty" for k in SECRET_ENV_NAMES]
    + [f"{k}-empty" for k in SECRET_ENV_NAMES],
)
def test_check_env_file_secrets_verdict_by_key_and_value(
    monkeypatch, tmp_path, key, value, status
):
    p = tmp_path / "jasper.env"
    p.write_text(f"{key}={value}\n")
    monkeypatch.setenv("JASPER_ENV_FILE", str(p))

    r = env.check_env_file_secrets()

    assert r.status == status
    if status == "fail":
        assert r.reason == env.REASON_SECRET_IN_ENV_FILE
        assert value not in r.detail


def test_check_env_file_secrets_ok_with_no_secret_keys(monkeypatch, tmp_path):
    p = tmp_path / "jasper.env"
    p.write_text("JASPER_HOSTNAME=jts.local\nJASPER_VOICE_PROVIDER=gemini\n")
    monkeypatch.setenv("JASPER_ENV_FILE", str(p))

    r = env.check_env_file_secrets()

    assert r.status == "ok"


def test_check_env_file_secrets_skipped_when_file_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_ENV_FILE", str(tmp_path / "missing.env"))

    r = env.check_env_file_secrets()

    assert r.status == "skipped"


def test_check_env_file_secrets_skipped_when_file_unreadable(monkeypatch, tmp_path):
    # Root bypasses chmod, so simulate "can't read" with a directory in
    # place of the file — Path.read_text() raises IsADirectoryError there,
    # same OSError family env_load.read_env_file_state maps to "unreadable".
    p = tmp_path / "jasper.env"
    p.mkdir()
    monkeypatch.setenv("JASPER_ENV_FILE", str(p))

    r = env.check_env_file_secrets()

    assert r.status == "skipped"
    assert r.reason == env.REASON_ENV_FILE_UNREADABLE


def test_check_env_file_secrets_is_registered():
    assert "check_env_file_secrets" in _registered_check_names()


# -------------------------------------------- shared-state group-writability
#
# The whole /var/lib/jasper state tree is group-`jasper` writable so every
# non-root daemon that writes it can. A file that regresses to group-read-only
# is the readonly-DB outage condition.


def test_state_group_write_no_files_is_ok(tmp_path):
    res = _classify_state_group_write(
        tmp_path / "usage.db", tmp_path / "speaker_volume.json"
    )
    assert res.status == "ok"


@pytest.mark.parametrize(
    "name, mode, status, reason",
    [
        ("usage.db", 0o660, "ok", ""),
        ("usage.db", 0o644, "warn", env.REASON_STATE_GROUP_WRITE_VIOLATION),
        # Readable by /chat but not writable by jasper-voice.
        (
            "conversation_history.db", 0o640, "warn",
            env.REASON_STATE_GROUP_WRITE_VIOLATION,
        ),
    ],
    ids=["group-writable", "group-readonly", "history-group-readonly"],
)
def test_state_group_write_verdicts(monkeypatch, tmp_path, name, mode, status, reason):
    _pretend_group_is_jasper(monkeypatch)
    target = tmp_path / name
    target.write_text("x", encoding="utf-8")
    target.chmod(mode)

    res = _classify_state_group_write(
        tmp_path / "usage.db", tmp_path / "speaker_volume.json"
    )

    assert res.status == status
    assert res.reason == reason


def test_state_group_write_check_is_registered():
    assert "check_state_dir_group_writable" in _registered_check_names()


@pytest.mark.parametrize("mode, status", [(0o640, "warn")], ids=["group-readonly"])
def test_state_group_write_checks_configured_volume_path(
    monkeypatch, tmp_path, mode, status
):
    """volume_state_path is whatever the caller passes, not a hardcoded
    filename — an override at a non-default name/location is still
    checked (JASPER_VOLUME_STATE_PATH)."""
    _pretend_group_is_jasper(monkeypatch)
    volume_state_path = tmp_path / "custom" / "vol_state.json"
    volume_state_path.parent.mkdir()
    volume_state_path.write_text("x", encoding="utf-8")
    volume_state_path.chmod(mode)

    res = _classify_state_group_write(tmp_path / "usage.db", volume_state_path)

    assert res.status == status


# ---------- check_state_dir: os.access(W_OK) always reports jasper-doctor's
# own root access, not the non-root jasper-voice/-mux writers'. Converged
# onto the same _shared._group_writable_dir predicate as
# audio.check_camilla_configs_writable and correction.check_correction_state_dirs,
# but require_setgid=False: ensure_state_dir (install.sh) leaves STATE_DIR
# itself plain root:jasper 0770, not setgid, because every writer here
# already declares Group=jasper in its own unit rather than relying on
# setgid inheritance.


def test_check_state_dir_warns_when_missing(monkeypatch, tmp_path):
    cfg = _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIzaSyTest",
        JASPER_USAGE_DB=str(tmp_path / "missing" / "usage.db"),
    )

    r = env.check_state_dir(cfg)

    assert r.status == "warn"
    assert r.reason == env.REASON_STATE_DIR_MISSING


def test_check_state_dir_ok_when_group_writable_without_setgid(monkeypatch, tmp_path):
    """The real STATE_DIR posture (0770, no setgid) must not be flagged —
    require_setgid=False is load-bearing here, unlike the correction/
    active_speaker and CamillaDSP-configs trees."""
    _pretend_group_is_jasper(monkeypatch)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    os.chmod(state_dir, 0o770)
    cfg = _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIzaSyTest",
        JASPER_USAGE_DB=str(state_dir / "usage.db"),
    )

    r = env.check_state_dir(cfg)

    assert r.status == "ok"


def test_check_state_dir_fails_when_not_group_writable(monkeypatch, tmp_path):
    _pretend_group_is_jasper(monkeypatch)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    os.chmod(state_dir, 0o700)
    cfg = _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIzaSyTest",
        JASPER_USAGE_DB=str(state_dir / "usage.db"),
    )

    r = env.check_state_dir(cfg)

    assert r.status == "fail"
    assert r.reason == env.REASON_STATE_DIR_NOT_WRITABLE
