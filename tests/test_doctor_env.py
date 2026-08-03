# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor env domain."""

from pathlib import Path


from jasper.cli import doctor


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


def test_check_service_runtime_state_fails_on_failed_unit(monkeypatch):
    class FakeRun:
        stdout = (
            "Id=librespot.service\n"
            "LoadState=loaded\n"
            "ActiveState=failed\n"
            "SubState=failed\n"
            "Result=exit-code\n"
            "NRestarts=5\n"
        )

    monkeypatch.setattr(doctor._shared, "_run", lambda *a, **kw: FakeRun())

    r = doctor.check_service_runtime_state()

    assert r.status == "fail"
    assert "librespot.service state=failed/failed" in r.detail
    assert "NRestarts=5" in r.detail


def test_check_service_runtime_state_warns_on_restart_count(monkeypatch):
    class FakeRun:
        stdout = (
            "Id=jasper-voice.service\n"
            "LoadState=loaded\n"
            "ActiveState=active\n"
            "SubState=running\n"
            "Result=success\n"
            "NRestarts=2\n"
        )

    monkeypatch.setattr(doctor._shared, "_run", lambda *a, **kw: FakeRun())

    r = doctor.check_service_runtime_state()

    assert r.status == "warn"
    assert "jasper-voice.service NRestarts=2" in r.detail


def test_runtime_state_units_track_coupling_reconciler_oneshots():
    """#1233 follow-up: a failed coupling-reconcile oneshot (e.g. an arm-abort —
    env written, fan-in restart aborted because camilla would not stop) parks
    the unit in `failed` with the evidence only in `systemctl --failed` + the
    journal. The durable entry unit must be in the doctor's tracked set so state
    surfaces in one-shot diagnostics."""
    assert "jasper-fanin-coupling-auto.service" in doctor._RUNTIME_STATE_UNITS


def test_check_service_runtime_state_fails_on_failed_coupling_oneshot(monkeypatch):
    class FakeRun:
        stdout = (
            "Id=jasper-fanin-coupling-auto.service\n"
            "LoadState=loaded\n"
            "ActiveState=failed\n"
            "SubState=failed\n"
            "Result=exit-code\n"
            "NRestarts=0\n"
        )

    monkeypatch.setattr(doctor._shared, "_run", lambda *a, **kw: FakeRun())

    r = doctor.check_service_runtime_state()

    assert r.status == "fail"
    assert "jasper-fanin-coupling-auto.service state=failed/failed" in r.detail


def test_check_service_runtime_state_ignores_in_flight_oneshot(monkeypatch):
    """`activating` is a oneshot's NORMAL mid-run state (a reconcile pass in
    flight), not the stuck-start instability it signals on long-running
    daemons — a tick the doctor happens to race must not read as a failure,
    while a daemon stuck in activating still does."""

    class FakeRun:
        stdout = (
            "Id=jasper-fanin-coupling-auto.service\n"
            "LoadState=loaded\n"
            "ActiveState=activating\n"
            "SubState=start\n"
            "Result=success\n"
            "NRestarts=0\n"
            "\n"
            "Id=jasper-fanin.service\n"
            "LoadState=loaded\n"
            "ActiveState=activating\n"
            "SubState=start\n"
            "Result=success\n"
            "NRestarts=0\n"
        )

    monkeypatch.setattr(doctor._shared, "_run", lambda *a, **kw: FakeRun())

    r = doctor.check_service_runtime_state()

    assert r.status == "fail"
    assert "jasper-fanin.service state=activating/start" in r.detail
    assert "jasper-fanin-coupling-auto.service" not in r.detail


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
