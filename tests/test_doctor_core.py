# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor core domain."""

import asyncio
import json
import sys
import threading
import time
from types import SimpleNamespace


from jasper.cli import doctor
from jasper.config import Config


def test_json_mode_reports_unhandled_check_exception(monkeypatch, capsys):
    """Machine-readable mode should stay machine-readable even if a
    diagnostic check raises unexpectedly."""
    monkeypatch.setattr(doctor, "_load_env_files", lambda: None)
    monkeypatch.setattr(Config, "from_env", staticmethod(lambda: object()))

    async def boom(_cfg):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(doctor, "run_async", boom)
    monkeypatch.setattr(sys, "argv", ["jasper-doctor", "--json"])

    try:
        doctor.main()
    except SystemExit as e:
        assert e.code == 1
    else:  # pragma: no cover - defensive, main() should always exit.
        raise AssertionError("main() did not exit")

    payload = json.loads(capsys.readouterr().out)
    assert payload["fails"] == 1
    assert payload["results"][0]["name"] == "jasper-doctor"
    assert "synthetic failure" in payload["error"]


def test_json_mode_endpoint_tier_does_not_require_voice_provider(
    monkeypatch,
    capsys,
):
    """Endpoint doctor must not build full voice Config before filtering.

    A freshly imaged dumb endpoint has no JASPER_VOICE_PROVIDER by design;
    it should still report endpoint health instead of failing at Config
    construction.
    """
    monkeypatch.setattr(doctor, "_load_env_files", lambda: None)
    monkeypatch.setattr(doctor, "read_install_profile", lambda: "endpoint")
    monkeypatch.setattr(
        Config,
        "from_env",
        staticmethod(
            lambda: (_ for _ in ()).throw(
                AssertionError("endpoint doctor must not construct full Config")
            )
        ),
    )
    monkeypatch.setenv("JASPER_USAGE_DB", "/tmp/jasper-endpoint-usage.db")

    async def fake_run_async(cfg):
        assert cfg.usage_db == "/tmp/jasper-endpoint-usage.db"
        return [doctor.CheckResult("endpoint smoke", "ok", "minimal cfg")]

    monkeypatch.setattr(doctor, "run_async", fake_run_async)
    monkeypatch.setattr(sys, "argv", ["jasper-doctor", "--json"])

    try:
        doctor.main()
    except SystemExit as e:
        assert e.code == 0
    else:  # pragma: no cover - defensive, main() should always exit.
        raise AssertionError("main() did not exit")

    payload = json.loads(capsys.readouterr().out)
    assert payload["fails"] == 0
    assert payload["results"] == [
        {
            "name": "endpoint smoke",
            "status": "ok",
            "detail": "minimal cfg",
        }
    ]


def test_doctor_check_exception_becomes_fail_result():
    def explode():
        raise RuntimeError("synthetic check failure")

    result = doctor._run_doctor_check(("explosive check", explode))

    assert result.name == "explosive check"
    assert result.status == "fail"
    assert "RuntimeError: synthetic check failure" in result.detail


def test_doctor_check_exception_redacts_secret_like_values():
    def explode():
        raise RuntimeError(
            "refresh_token=super-secret-refresh "
            "Bearer super-secret-access-token "
            "sk-super-secret-openai-key"
        )

    result = doctor._run_doctor_check(("sensitive check", explode))

    assert "super-secret-refresh" not in result.detail
    assert "super-secret-access-token" not in result.detail
    assert "sk-super-secret-openai-key" not in result.detail
    assert "refresh_token=<redacted>" in result.detail
    assert "Bearer <redacted>" in result.detail
    assert "sk-s...-key" in result.detail


def test_async_doctor_check_exception_becomes_fail_result():
    async def explode():
        raise RuntimeError("synthetic async failure")

    result = asyncio.run(
        doctor._run_async_doctor_check("async check", explode),
    )

    assert result.name == "async check"
    assert result.status == "fail"
    assert "RuntimeError: synthetic async failure" in result.detail


def test_legacy_endpoint_token_doctor_behaves_as_streambox(monkeypatch):
    """A persisted/legacy 'endpoint' token normalizes to streambox, so the
    doctor applies the streambox skip behaviour (voice/brain groups skipped,
    local audio kept)."""
    from jasper.cli.doctor._registry import RegisteredCheck

    ran: list[str] = []

    def env_check():
        ran.append("env")
        return doctor.CheckResult("env file", "ok", "ran")

    def voice_check(_cfg):
        ran.append("voice")
        return doctor.CheckResult("provider key", "fail", "should not run")

    def web_check():
        ran.append("web")
        return doctor.CheckResult("management surface", "ok", "ran")

    monkeypatch.setattr(doctor, "read_install_profile", lambda: "endpoint")
    monkeypatch.setattr(
        doctor,
        "registered_checks",
        lambda: [
            RegisteredCheck(order=0, group="env", func=env_check),
            RegisteredCheck(
                order=1,
                group="voice",
                func=voice_check,
                needs_cfg=True,
                label="provider key",
            ),
            RegisteredCheck(order=1.5, group="web", func=web_check),
        ],
    )

    results = asyncio.run(doctor.run_async(object()))

    assert ran == ["env", "web"]
    assert [(r.name, r.status, r.detail) for r in results] == [
        ("env file", "ok", "ran"),
        ("provider key", "ok", "not installed (streambox profile)"),
        ("management surface", "ok", "ran"),
    ]


def test_streambox_doctor_config_does_not_require_voice_provider(monkeypatch):
    monkeypatch.delenv("JASPER_VOICE_PROVIDER", raising=False)
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.setenv("JASPER_HOSTNAME", "jts4.local")
    monkeypatch.setenv("JASPER_SPEAKER_NAME", "JTS4")

    cfg = doctor._doctor_config_from_env("streambox")

    assert cfg.usage_db
    assert cfg.camilla_host == "127.0.0.1"
    assert cfg.camilla_port == 1234
    assert cfg.spotify_enabled is False
    assert cfg.spotify_device_name == "JTS4"
    assert cfg.spotify_setup_url == "http://jts4.local/spotify"


def test_streambox_doctor_skips_voice_brain_but_keeps_local_audio_checks():
    by_name = {entry.func.__name__: entry for entry in doctor.registered_checks()}

    assert (
        doctor._doctor_skip_reason(by_name["check_provider_key"], "streambox")
        == "not installed (streambox profile)"
    )
    assert doctor._doctor_skip_reason(
        by_name["check_openwakeword_model"],
        "streambox",
    )
    assert doctor._doctor_skip_reason(
        by_name["check_aec_bridge_running"],
        "streambox",
    )
    assert doctor._doctor_skip_reason(by_name["check_mic_capture"], "streambox")
    assert doctor._doctor_skip_reason(by_name["check_tts_open"], "streambox")
    assert not doctor._doctor_skip_reason(
        by_name["check_camilla_websocket"],
        "streambox",
    )
    assert not doctor._doctor_skip_reason(
        by_name["check_librespot_running"],
        "streambox",
    )
    assert not doctor._doctor_skip_reason(
        by_name["check_correction_web_service"],
        "streambox",
    )


def test_streambox_profile_doctor_keeps_local_audio_groups(monkeypatch):
    from jasper.cli.doctor._registry import RegisteredCheck

    ran: list[str] = []

    def voice_check(_cfg):
        ran.append("voice")
        return doctor.CheckResult("provider key", "fail", "should not run")

    def check_mic_capture(_cfg):
        ran.append("mic")
        return doctor.CheckResult("mic capture", "fail", "should not run")

    def renderer_check(_cfg):
        ran.append("renderers")
        return doctor.CheckResult("librespot.service", "ok", "ran")

    def correction_check():
        ran.append("correction")
        return doctor.CheckResult("room correction service", "ok", "ran")

    monkeypatch.setattr(doctor, "read_install_profile", lambda: "streambox")
    monkeypatch.setattr(
        doctor,
        "registered_checks",
        lambda: [
            RegisteredCheck(
                order=0,
                group="voice",
                func=voice_check,
                needs_cfg=True,
                label="provider key",
            ),
            RegisteredCheck(
                order=1,
                group="audio",
                func=check_mic_capture,
                needs_cfg=True,
                label="mic capture",
            ),
            RegisteredCheck(
                order=2,
                group="renderers",
                func=renderer_check,
                needs_cfg=True,
                label="librespot.service",
            ),
            RegisteredCheck(order=3, group="correction", func=correction_check),
        ],
    )

    results = asyncio.run(doctor.run_async(SimpleNamespace()))

    assert ran == ["renderers", "correction"]
    assert [(r.name, r.status, r.detail) for r in results] == [
        ("provider key", "ok", "not installed (streambox profile)"),
        ("mic capture", "ok", "not installed (streambox profile)"),
        ("librespot.service", "ok", "ran"),
        ("room correction service", "ok", "ran"),
    ]


def test_run_async_parallelizes_blocking_checks_but_preserves_order(
    monkeypatch,
):
    from jasper.cli.doctor._registry import RegisteredCheck

    active = 0
    max_active = 0
    lock = threading.Lock()

    def make_check(name: str, delay: float):
        def check():
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(delay)
            with lock:
                active -= 1
            return doctor.CheckResult(name, "ok", "ran")

        return check

    monkeypatch.setattr(doctor, "read_install_profile", lambda: "full")
    monkeypatch.setenv("JASPER_DOCTOR_MAX_CONCURRENCY", "3")
    monkeypatch.setattr(
        doctor,
        "registered_checks",
        lambda: [
            RegisteredCheck(order=0, group="test", func=make_check("a", 0.15)),
            RegisteredCheck(order=1, group="test", func=make_check("b", 0.15)),
            RegisteredCheck(order=2, group="test", func=make_check("c", 0.15)),
            RegisteredCheck(order=3, group="test", func=make_check("d", 0.15)),
            RegisteredCheck(order=4, group="test", func=make_check("e", 0.15)),
            RegisteredCheck(order=5, group="test", func=make_check("f", 0.15)),
        ],
    )

    results = asyncio.run(doctor.run_async(SimpleNamespace()))

    assert [r.name for r in results] == ["a", "b", "c", "d", "e", "f"]
    assert max_active == 3


def test_run_async_serializes_checks_in_same_exclusive_group(monkeypatch):
    from jasper.cli.doctor._registry import RegisteredCheck

    active = 0
    max_active = 0
    lock = threading.Lock()

    def exclusive(name: str):
        def check():
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return doctor.CheckResult(name, "ok", "ran")

        return check

    def ordinary():
        time.sleep(0.05)
        return doctor.CheckResult("c", "ok", "ran")

    monkeypatch.setattr(doctor, "read_install_profile", lambda: "full")
    monkeypatch.setenv("JASPER_DOCTOR_MAX_CONCURRENCY", "3")
    monkeypatch.setattr(
        doctor,
        "registered_checks",
        lambda: [
            RegisteredCheck(
                order=0,
                group="test",
                func=exclusive("a"),
                exclusive_group="audio-probe",
            ),
            RegisteredCheck(
                order=1,
                group="test",
                func=exclusive("b"),
                exclusive_group="audio-probe",
            ),
            RegisteredCheck(order=2, group="test", func=ordinary),
        ],
    )

    results = asyncio.run(doctor.run_async(SimpleNamespace()))

    assert [r.name for r in results] == ["a", "b", "c"]
    assert max_active == 1
