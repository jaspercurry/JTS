# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared wizard CLI contracts through each caller's ``main()``."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler
from types import SimpleNamespace
from typing import Any

import pytest

from jasper.platform import systemd as _systemd
from jasper.web import (
    bluetooth_setup,
    chat_setup,
    correction_setup,
    system_setup,
)

# module, console-script prog, ExecStart default port, the idle threshold the
# wizard asks the runner for, and its on-idle-exit hook.
WIZARDS = [
    (
        bluetooth_setup, "jasper-bluetooth-web", 8769,
        _systemd.DEFAULT_IDLE_SHUTDOWN_SEC, None,
    ),
    (
        chat_setup, "jasper-chat-web", 8787,
        chat_setup.IDLE_SHUTDOWN_SEC, None,
    ),
    (
        correction_setup, "jasper-correction-web", 8770,
        _systemd.DEFAULT_IDLE_SHUTDOWN_SEC, None,
    ),
    (
        system_setup, "jasper-system-web", 8772,
        system_setup.IDLE_SHUTDOWN_SEC, None,
    ),
]

_COLUMNS = "module, prog, default_port, idle_sec, on_idle_exit"

# The two whose routes start work they do not await, so they need the hold.
_HOLDERS = [row for row in WIZARDS if row[0] in (bluetooth_setup, correction_setup)]


class _RecordingTracker:
    def __init__(
        self,
        idle_threshold_sec: float = _systemd.DEFAULT_IDLE_SHUTDOWN_SEC,
        watchdog_period_sec: float = _systemd.DEFAULT_WATCHDOG_NOTIFY_SEC,
        on_idle_exit: Any = None,
    ) -> None:
        self.idle_threshold_sec = idle_threshold_sec
        self.watchdog_period_sec = watchdog_period_sec
        self.on_idle_exit = on_idle_exit
        self.started = False
        self.stopped = False

    def hold(self, label: str = ""):
        raise AssertionError("no hold is taken during start-up")

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class _FakeDispatcher:
    def start(self) -> None:
        pass


@pytest.fixture(name="wizard_harness")
def wizard_harness_fixture(monkeypatch):
    record: dict[str, Any] = {"events": []}

    def _tracker(*args, **kwargs):
        record["tracker"] = _RecordingTracker(*args, **kwargs)
        return record["tracker"]

    monkeypatch.setattr(_systemd, "IdleShutdownTracker", _tracker)
    monkeypatch.setattr(_systemd, "install_request_idle_bump", lambda *_a: None)
    monkeypatch.setattr(
        _systemd, "notify_ready", lambda: record["events"].append("ready")
    )
    monkeypatch.setattr(
        _systemd,
        "notify_stopping",
        lambda: record["events"].append("stopping"),
    )

    # Pre-start hooks reach real hardware/state: the dispatcher opens a D-Bus
    # loop thread, the claim boundary talks to CamillaDSP.
    monkeypatch.setattr(bluetooth_setup, "_AsyncDispatcher", _FakeDispatcher)
    monkeypatch.setattr(
        correction_setup, "_claim_crossover_state_owners", lambda: None
    )
    from jasper import volume_coordinator

    monkeypatch.setattr(
        volume_coordinator,
        "install_env_canonical_target_provider",
        lambda: None,
    )

    def install(module, sockets, *, serve_error=None):
        monkeypatch.setattr(_systemd, "adopt_systemd_sockets", lambda: sockets)

        def serve_forever():
            record["events"].append("serve")
            if serve_error is not None:
                raise serve_error

        def _make_server(target, **kwargs):
            record["target"] = target
            record["kwargs"] = kwargs
            return SimpleNamespace(
                RequestHandlerClass=BaseHTTPRequestHandler,
                serve_forever=serve_forever,
                server_close=lambda: record["events"].append("close"),
            )

        monkeypatch.setattr(module, "make_server", _make_server)
        return record

    return install


@pytest.mark.parametrize(_COLUMNS, WIZARDS)
def test_main_binds_the_execstart_defaults(
    wizard_harness, module, prog, default_port, idle_sec, on_idle_exit
):
    record = wizard_harness(module, [])
    assert module.main([]) == 0
    assert record["target"] == ("127.0.0.1", default_port)
    assert module.main(["--host", "10.0.0.5", "--port", "1"]) == 0
    assert record["target"] == ("10.0.0.5", 1)


@pytest.mark.parametrize(_COLUMNS, WIZARDS)
def test_main_serves_the_inherited_listener_not_a_fresh_bind(
    wizard_harness, module, prog, default_port, idle_sec, on_idle_exit
):
    inherited = object()
    record = wizard_harness(module, [inherited])
    assert module.main(["--host", "10.0.0.5", "--port", "1"]) == 0
    assert record["target"] is inherited


@pytest.mark.parametrize(_COLUMNS, WIZARDS)
def test_main_builds_the_tracker_this_wizard_asked_for(
    wizard_harness, module, prog, default_port, idle_sec, on_idle_exit
):
    record = wizard_harness(module, [])
    assert module.main([]) == 0
    tracker = record["tracker"]
    assert tracker.idle_threshold_sec == idle_sec
    assert tracker.on_idle_exit is on_idle_exit
    assert tracker.started


@pytest.mark.parametrize(_COLUMNS, _HOLDERS)
def test_a_wizard_with_background_work_gets_the_trackers_hold(
    wizard_harness, module, prog, default_port, idle_sec, on_idle_exit
):
    record = wizard_harness(module, [])
    assert module.main([]) == 0
    assert record["kwargs"]["idle_hold"] == record["tracker"].hold


@pytest.mark.parametrize(_COLUMNS, WIZARDS)
@pytest.mark.parametrize("serve_error", [None, KeyboardInterrupt(), RuntimeError()])
def test_main_releases_listener_and_idle_tracker_on_exit(
    wizard_harness, module, prog, default_port, idle_sec, on_idle_exit, serve_error
):
    record = wizard_harness(module, [], serve_error=serve_error)
    if isinstance(serve_error, RuntimeError):
        with pytest.raises(RuntimeError) as exc:
            module.main([])
        assert exc.value is serve_error
    else:
        assert module.main([]) == 0
    assert record["events"] == ["ready", "serve", "stopping", "close"]
    assert record["tracker"].stopped


@pytest.mark.parametrize(_COLUMNS, WIZARDS)
def test_usage_errors_name_the_console_script(
    capsys, wizard_harness, module, prog, default_port, idle_sec, on_idle_exit
):
    with pytest.raises(SystemExit) as exc:
        module.main(["--no-such-flag"])
    assert exc.value.code == 2
    assert capsys.readouterr().err.startswith(f"usage: {prog}")
