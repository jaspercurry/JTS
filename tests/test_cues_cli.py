from __future__ import annotations

from unittest.mock import patch

import pytest

from jasper.cues import cli
from jasper.cues.generator import TTSResult


class _FakeBackend:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def synthesise(self, text: str) -> TTSResult:
        self.calls.append(text)
        return TTSResult(pcm_24k=b"\x00\x00" * 240)


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """Standard env for CLI tests: writable sounds dir, deterministic
    hostname/voice, fake API key (so the factory builds a backend),
    and monkey-patched factory that doesn't hit the network."""
    monkeypatch.setenv("JASPER_SOUNDS_DIR", str(tmp_path))
    monkeypatch.setenv("JASPER_MANAGEMENT_URL", "https://test.local")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")
    monkeypatch.setenv("JASPER_GEMINI_VOICE", "Aoede")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
    fake = _FakeBackend()
    # The CLI builds its backend via `build_cue_tts_backend(cfg)` —
    # patch that to return our deterministic fake regardless of
    # provider so tests don't hit any provider's network.
    monkeypatch.setattr(
        cli, "build_cue_tts_backend", lambda cfg: (fake, "Aoede"),
    )
    return tmp_path, fake


# --- list ---


def test_list_returns_1_when_cues_missing(cli_env, capsys):
    code = cli.main(["list"])
    assert code == 1  # all cues missing on a fresh sounds dir
    captured = capsys.readouterr()
    assert "spend_cap_reached" in captured.out
    assert "cant_connect" in captured.out
    assert "MISSING" in captured.out


def test_list_returns_0_after_regen(cli_env, capsys):
    cli.main(["regenerate"])
    code = cli.main(["list"])
    assert code == 0
    captured = capsys.readouterr()
    assert "MISSING" not in captured.out
    assert "cached" in captured.out


# --- regenerate ---


def test_regenerate_writes_all_cues_by_default(cli_env, capsys):
    code = cli.main(["regenerate"])
    assert code == 0
    captured = capsys.readouterr()
    assert "wrote spend_cap_reached" in captured.out
    assert "wrote cant_connect" in captured.out


def test_regenerate_idempotent_second_run(cli_env, capsys):
    cli.main(["regenerate"])
    capsys.readouterr()  # discard
    code = cli.main(["regenerate"])
    assert code == 0
    captured = capsys.readouterr()
    assert "all cues already cached" in captured.out


def test_regenerate_force_re_renders_cached(cli_env, capsys):
    _, fake = cli_env
    cli.main(["regenerate"])
    fake.calls.clear()
    capsys.readouterr()
    code = cli.main(["regenerate", "--force"])
    assert code == 0
    assert len(fake.calls) >= 2  # both cues re-rendered


def test_regenerate_single_cue(cli_env, capsys):
    code = cli.main(["regenerate", "--cue", "spend_cap_reached"])
    assert code == 0
    captured = capsys.readouterr()
    assert "wrote spend_cap_reached" in captured.out
    assert "wrote cant_connect" not in captured.out


def test_regenerate_unknown_cue_returns_error(cli_env, capsys):
    code = cli.main(["regenerate", "--cue", "definitely_not_a_real_cue"])
    assert code == 2  # ValueError mapped to exit 2
    captured = capsys.readouterr()
    assert "definitely_not_a_real_cue" in captured.err


def test_regenerate_without_api_key_reports_runtime_error(tmp_path, monkeypatch, capsys):
    """With no provider key configured anywhere, the CLI degrades
    gracefully (no backend, regen exits non-zero with explanation)
    rather than crashing on a NoneType attribute access."""
    monkeypatch.setenv("JASPER_SOUNDS_DIR", str(tmp_path))
    monkeypatch.setenv("JASPER_MANAGEMENT_URL", "https://test.local")
    monkeypatch.setenv("JASPER_GEMINI_VOICE", "Aoede")
    # All three provider keys must be absent for the factory to
    # return None — otherwise the fallback path picks one of them.
    for key in (
        "GEMINI_API_KEY", "JASPER_GEMINI_API_KEY",
        "OPENAI_API_KEY", "XAI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    # Also keep the file-loader from picking up real /etc/jasper
    # creds on a developer machine that has them.
    monkeypatch.setattr(
        "jasper.cues.cli.load_env_files", lambda *_: None,
    )
    code = cli.main(["regenerate"])
    assert code == 3  # RuntimeError mapped to exit 3
    captured = capsys.readouterr()
    assert "TTS backend" in captured.err or "no TTS backend" in captured.err


# --- play ---


def test_play_unknown_slug_returns_2(cli_env, capsys):
    code = cli.main(["play", "definitely_not_a_real_cue"])
    assert code == 2
    captured = capsys.readouterr()
    assert "unknown" in captured.err.lower()


def test_play_valid_slug_routes_to_control_endpoint(cli_env, capsys):
    """Happy path: a valid slug must POST to jasper-control's /cue/play
    and report success — exercising the code past find_cue() that used
    to raise NameError on the undefined `_env`. Mocks the typed control
    client's POST so no network is touched."""
    from jasper.control import client as control

    captured = {}

    def _fake_post(path, body=None, *, timeout=None, **kw):
        captured["path"] = path
        captured["body"] = body
        captured["timeout"] = timeout
        return control.ControlResponse(200, b'{"result": "ok"}')

    with patch.object(control, "post", _fake_post):
        code = cli.main(["play", "cant_connect"])

    assert code == 0
    out = capsys.readouterr().out
    assert "played cant_connect" in out
    assert captured["path"] == "/cue/play"
    assert captured["body"] == {"slug": "cant_connect"}
    # The play call uses the cue endpoint's generous 35 s timeout.
    assert captured["timeout"] == 35


def test_play_reports_failure_on_non_ok_result(cli_env, capsys):
    """If jasper-control answers but the body's result isn't 'ok', the
    CLI reports failure and exits non-zero (the `result == 'ok'` check
    survived the migration to the typed client)."""
    from jasper.control import client as control

    def _fake_post(path, body=None, *, timeout=None, **kw):
        return control.ControlResponse(200, b'{"result": "error"}')

    with patch.object(control, "post", _fake_post):
        code = cli.main(["play", "cant_connect"])

    assert code == 1
    assert "play failed" in capsys.readouterr().err


def test_play_reports_unreachable_on_control_error(cli_env, capsys):
    """When jasper-control is down the client raises ControlError; the
    CLI surfaces the 'could not reach jasper-control' guidance and exits
    non-zero rather than crashing."""
    from jasper.control import client as control

    def _fake_post(path, body=None, *, timeout=None, **kw):
        raise control.ControlError("connection refused")

    with patch.object(control, "post", _fake_post):
        code = cli.main(["play", "cant_connect"])

    assert code == 1
    err = capsys.readouterr().err
    assert "could not reach jasper-control" in err
    assert "/cue/play" in err
