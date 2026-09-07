# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The service lifecycle every socket-activated wizard's ``main()`` runs.

One pin over all four wizards on the shared runner (#4328): the CLI defaults
each ``ExecStart`` relies on, socket adoption winning over a fresh bind, the
idle tracker each wizard asks the runner to build, and the notify_ready ->
serve -> notify_stopping -> exit-0 tail.
"""
from __future__ import annotations

from typing import Any

import pytest

from jasper.platform import systemd as _systemd
from jasper.web import (
    bluetooth_setup,
    chat_setup,
    correction_setup,
    system_setup,
)
from tests.test_platform_systemd import _FakeServer

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
        _systemd.DEFAULT_IDLE_SHUTDOWN_SEC,
        correction_setup._idle_exit_restore_capture_entry,
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
    """Mirrors ``IdleShutdownTracker.__init__`` so a signature change fails
    here rather than leaving the assertions silently reading nothing."""

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

    def hold(self, label: str = ""):
        raise AssertionError("no hold is taken during start-up")

    def start(self) -> None:
        self.started = True


class _FakeDispatcher:
    def start(self) -> None:
        pass


@pytest.fixture(name="wizard_harness")
def wizard_harness_fixture(monkeypatch):
    """Neutralize systemd and each wizard's pre-start side effects.

    The returned installer takes the module under test and what
    ``adopt_systemd_sockets`` should report, and hands back the record the
    assertions read.
    """

    record: dict[str, Any] = {"ready": 0, "stopping": 0}

    def _tracker(*args, **kwargs):
        record["tracker"] = _RecordingTracker(*args, **kwargs)
        return record["tracker"]

    monkeypatch.setattr(_systemd, "IdleShutdownTracker", _tracker)
    monkeypatch.setattr(_systemd, "install_request_idle_bump", lambda *_a: None)
    monkeypatch.setattr(
        _systemd, "notify_ready", lambda: record.__setitem__("ready", record["ready"] + 1)
    )
    monkeypatch.setattr(
        _systemd,
        "notify_stopping",
        lambda: record.__setitem__("stopping", record["stopping"] + 1),
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

    def install(module, sockets):
        monkeypatch.setattr(_systemd, "adopt_systemd_sockets", lambda: sockets)

        def _make_server(target, **kwargs):
            record["target"] = target
            record["kwargs"] = kwargs
            return _FakeServer()

        monkeypatch.setattr(module, "make_server", _make_server)
        return record

    return install


@pytest.mark.parametrize(_COLUMNS, WIZARDS)
def test_main_binds_the_execstart_defaults(
    wizard_harness, module, prog, default_port, idle_sec, on_idle_exit
):
    """Every unit's ExecStart passes --host 127.0.0.1 and this port, so the
    runner's defaults have to be the same two values."""

    record = wizard_harness(module, [])
    assert module.main([]) == 0
    assert record["target"] == ("127.0.0.1", default_port)
    assert module.main(["--host", "10.0.0.5", "--port", "1"]) == 0
    assert record["target"] == ("10.0.0.5", 1)


@pytest.mark.parametrize(_COLUMNS, WIZARDS)
def test_main_serves_the_inherited_listener_not_a_fresh_bind(
    wizard_harness, module, prog, default_port, idle_sec, on_idle_exit
):
    """Socket activation: the adopted fd wins over --host/--port, or systemd's
    listener stays unserved and every request times out in nginx."""

    inherited = object()
    record = wizard_harness(module, [inherited])
    assert module.main(["--host", "10.0.0.5", "--port", "1"]) == 0
    assert record["target"] is inherited


@pytest.mark.parametrize(_COLUMNS, WIZARDS)
def test_main_builds_the_tracker_this_wizard_asked_for(
    wizard_harness, module, prog, default_port, idle_sec, on_idle_exit
):
    """The runner constructs the tracker now, so each wizard's threshold and
    on-idle-exit hook have to survive the scalars it passes instead."""

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
    """The tracker is built before the server precisely so these two can hand
    ``hold`` to the handler; without it a route's unawaited work looks like an
    abandoned tab and the process exits out from under it (#1854)."""

    record = wizard_harness(module, [])
    assert module.main([]) == 0
    assert record["kwargs"]["idle_hold"] == record["tracker"].hold


@pytest.mark.parametrize(_COLUMNS, WIZARDS)
def test_main_notifies_ready_then_stopping_once_each(
    wizard_harness, module, prog, default_port, idle_sec, on_idle_exit
):
    """Type=notify units hang in `activating` without READY=1, and the
    interrupted serve_forever must still emit STOPPING=1 and exit 0."""

    record = wizard_harness(module, [])
    assert module.main([]) == 0
    assert record["ready"] == 1
    assert record["stopping"] == 1


@pytest.mark.parametrize(_COLUMNS, WIZARDS)
def test_usage_errors_name_the_console_script(
    capsys, wizard_harness, module, prog, default_port, idle_sec, on_idle_exit
):
    """argparse's prog is what the operator sees when an ExecStart flag is
    wrong; it must stay the console-script name, not `__main__.py`."""

    with pytest.raises(SystemExit) as exc:
        module.main(["--no-such-flag"])
    assert exc.value.code == 2
    assert capsys.readouterr().err.startswith(f"usage: {prog}")
