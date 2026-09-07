# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The service lifecycle every socket-activated wizard's ``main()`` runs.

One pin over all four wizards on the shared runner (#4328): the CLI defaults
each ``ExecStart`` relies on, socket adoption winning over a fresh bind, and
the notify_ready -> serve -> notify_stopping -> exit-0 tail. Per-wizard
behaviour (correction's capture-entry restore wiring and its own journal
bootstrap) stays pinned beside its own module.
"""
from __future__ import annotations

from typing import Any

import pytest

from jasper.web import (
    _systemd,
    bluetooth_setup,
    chat_setup,
    correction_setup,
    system_setup,
)

# module, console-script prog, ExecStart default port
WIZARDS = [
    (bluetooth_setup, "jasper-bluetooth-web", 8769),
    (chat_setup, "jasper-chat-web", 8787),
    (correction_setup, "jasper-correction-web", 8770),
    (system_setup, "jasper-system-web", 8772),
]


class _FakeTracker:
    def __init__(self, *_args, **_kwargs) -> None:
        self.started = False

    def hold(self, label: str = ""):
        raise AssertionError("no hold is taken during start-up")

    def start(self) -> None:
        self.started = True


class _FakeDispatcher:
    def start(self) -> None:
        pass


class _FakeServer:
    RequestHandlerClass = object

    def serve_forever(self) -> None:
        raise KeyboardInterrupt


@pytest.fixture
def wizard_harness(monkeypatch):
    """Neutralize systemd and each wizard's pre-start side effects.

    The returned installer takes the module under test and what
    ``adopt_systemd_sockets`` should report, and hands back the record the
    assertions read.
    """

    record: dict[str, Any] = {"ready": 0, "stopping": 0}

    monkeypatch.setattr(_systemd, "IdleShutdownTracker", _FakeTracker)
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

        def _make_server(target, **_kwargs):
            record["target"] = target
            return _FakeServer()

        monkeypatch.setattr(module, "make_server", _make_server)
        return record

    return install


@pytest.mark.parametrize("module, prog, default_port", WIZARDS)
def test_main_binds_the_execstart_defaults(
    wizard_harness, module, prog, default_port
):
    """Every unit's ExecStart passes --host 127.0.0.1 and this port, so the
    runner's defaults have to be the same two values."""

    record = wizard_harness(module, [])
    assert module.main([]) == 0
    assert record["target"] == ("127.0.0.1", default_port)
    assert module.main(["--host", "10.0.0.5", "--port", "1"]) == 0
    assert record["target"] == ("10.0.0.5", 1)


@pytest.mark.parametrize("module, prog, default_port", WIZARDS)
def test_main_serves_the_inherited_listener_not_a_fresh_bind(
    wizard_harness, module, prog, default_port
):
    """Socket activation: the adopted fd wins over --host/--port, or systemd's
    listener stays unserved and every request times out in nginx."""

    inherited = object()
    record = wizard_harness(module, [inherited])
    assert module.main(["--host", "10.0.0.5", "--port", "1"]) == 0
    assert record["target"] is inherited


@pytest.mark.parametrize("module, prog, default_port", WIZARDS)
def test_main_notifies_ready_then_stopping_once_each(
    wizard_harness, module, prog, default_port
):
    """Type=notify units hang in `activating` without READY=1, and the
    interrupted serve_forever must still emit STOPPING=1 and exit 0."""

    record = wizard_harness(module, [])
    assert module.main([]) == 0
    assert record["ready"] == 1
    assert record["stopping"] == 1


@pytest.mark.parametrize("module, prog, default_port", WIZARDS)
def test_usage_errors_name_the_console_script(
    capsys, module, prog, default_port
):
    """argparse's prog is what the operator sees when an ExecStart flag is
    wrong; it must stay the console-script name, not `__main__.py`."""

    with pytest.raises(SystemExit) as exc:
        module.main(["--no-such-flag"])
    assert exc.value.code == 2
    assert capsys.readouterr().err.startswith(f"usage: {prog}")
