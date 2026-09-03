# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Route tests for ``jasper.control.handlers.aec``.

/aec/* and the USB-mic leg routes, including the ``aec_endpoints``
persistence and reconciler-restart side effects they trigger.
"""

from __future__ import annotations

import pytest

from jasper.control import aec_endpoints

from tests.control_server_fixtures import (
    _explicit_passive_output_topology,
    _get,
    _isolate_household_secret,
    _post,
    _post_raw,
    _recording_popen,
    server_with_coordinator,
)

_IMPORTED_FIXTURES = (
    _explicit_passive_output_topology,
    _isolate_household_secret,
    server_with_coordinator,
)


class _SystemctlResult:
    """Stand-in for the `subprocess.CompletedProcess` a stubbed `systemctl` returns."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


def test_aec_leg_restarts_reconciler(monkeypatch, tmp_path, server_with_coordinator):
    """Leg changes use the same restart kick as the software-AEC3 toggle."""
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    mode_file = tmp_path / "aec_mode.env"
    mode_file.write_text("JASPER_AEC_MODE=auto\n")
    popens: list[list[str]] = []

    monkeypatch.setattr(aec_endpoints, "_AEC_MODE_FILE", str(mode_file))
    monkeypatch.setattr(srv_mod, "_aec_full_status", lambda: {"ok": True})
    monkeypatch.setattr(srv_mod.subprocess, "Popen", _recording_popen(popens))

    status, body = _post(
        f"{base}/aec/leg",
        {"leg": "chip_aec_150", "enabled": True},
    )

    assert status == 200
    assert body == {"ok": True}
    assert "JASPER_WAKE_LEG_CHIP_AEC_150=1" in mode_file.read_text()
    assert popens == [
        ["systemctl", "restart", "--no-block", "jasper-aec-reconcile.service"],
    ]


def test_json_array_body_is_treated_as_empty_body(server_with_coordinator):
    base, _ = server_with_coordinator

    status, body = _post_raw(f"{base}/aec/leg", b"[]")

    assert status == 400
    assert body["error"] == (
        "leg must be one of: chip_aec_150, chip_aec_210, dtln, raw"
    )


@pytest.mark.parametrize("profile", ["xvf_chip_aec", "xvf_chip_aec_testing"])
def test_aec_profile_restarts_reconciler(
    profile,
    monkeypatch,
    tmp_path,
    server_with_coordinator,
):
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    mode_file = tmp_path / "aec_mode.env"
    mode_file.write_text("JASPER_AEC_MODE=auto\n")
    popens: list[list[str]] = []

    monkeypatch.setattr(aec_endpoints, "_AEC_MODE_FILE", str(mode_file))
    monkeypatch.setattr(srv_mod, "_aec_full_status", lambda: {"profile": profile})
    monkeypatch.setattr(srv_mod.subprocess, "Popen", _recording_popen(popens))

    status, body = _post(
        f"{base}/aec/profile",
        {"profile": profile},
    )

    assert status == 200
    assert body == {"profile": profile}
    text = mode_file.read_text()
    assert f"JASPER_AUDIO_INPUT_PROFILE={profile}" in text
    assert "JASPER_WAKE_LEG_CHIP_AEC=1" in text
    assert popens == [
        ["systemctl", "restart", "--no-block", "jasper-aec-reconcile.service"],
    ]


def test_usb_mic_persists_intent_and_schedules_descriptor_recompose(
    monkeypatch,
    server_with_coordinator,
):
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    statuses = iter([
        {"usb_mic": {"enabled": False, "toggle_enabled": True}},
        {"usb_mic": {"enabled": True, "state": "starting"}},
    ])
    writes: list[bool] = []
    recomposes: list[bool] = []
    monkeypatch.setattr(srv_mod, "_aec_full_status", lambda: next(statuses))
    monkeypatch.setattr(srv_mod, "write_usb_mic_enabled", writes.append)
    monkeypatch.setattr(
        srv_mod,
        "_schedule_usb_gadget_recompose",
        lambda: recomposes.append(True) or True,
    )

    status, body = _post(f"{base}/aec/usb-mic", {"enabled": True})

    assert status == 200
    assert body["usb_mic"] == {"enabled": True, "state": "starting"}
    assert writes == [True]
    assert recomposes == [True]


def test_usb_mic_schedule_failure_returns_structured_502(
    monkeypatch,
    server_with_coordinator,
):
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    usb_mic = {
        "enabled": True,
        "state": "starting",
        "toggle_enabled": True,
    }
    writes: list[bool] = []
    monkeypatch.setattr(
        srv_mod,
        "_aec_full_status",
        lambda: {"usb_mic": usb_mic},
    )
    monkeypatch.setattr(srv_mod, "write_usb_mic_enabled", writes.append)
    monkeypatch.setattr(
        srv_mod,
        "_schedule_usb_gadget_recompose",
        lambda: False,
    )

    status, body = _post(f"{base}/aec/usb-mic", {"enabled": True})

    assert status == 502
    assert body == {
        "error": (
            "USB microphone preference was saved, but its hardware update "
            "could not be scheduled."
        ),
        "code": "usb_mic_recompose_schedule_failed",
        "intent_saved": True,
        "requested_enabled": True,
        "usb_mic": usb_mic,
    }
    assert writes == [True]


def test_usb_mic_refuses_enable_when_status_gate_is_closed(
    monkeypatch,
    server_with_coordinator,
):
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    monkeypatch.setattr(
        srv_mod,
        "_aec_full_status",
        lambda: {
            "usb_mic": {
                "enabled": False,
                "toggle_enabled": False,
                "detail": "Turn on USB Audio Input in Sources first.",
            },
        },
    )
    monkeypatch.setattr(
        srv_mod,
        "write_usb_mic_enabled",
        lambda _enabled: pytest.fail("unavailable switch must not persist intent"),
    )
    monkeypatch.setattr(
        srv_mod,
        "_schedule_usb_gadget_recompose",
        lambda: pytest.fail("unavailable switch must not recompose USB"),
    )

    status, body = _post(f"{base}/aec/usb-mic", {"enabled": True})

    assert status == 409
    assert body["error"] == "Turn on USB Audio Input in Sources first."


def test_raw_usb_mic_leg_persists_then_restarts_only_aec_bridge(
    monkeypatch,
    server_with_coordinator,
):
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    events = []
    choices = [
        {"value": "primary", "label": "Same as JTS voice"},
        {
            "value": "raw0",
            "label": "Raw microphone (no echo cancellation)",
        },
    ]
    final_status = {
        "usb_mic": {
            "source_selection": {
                "requested": "raw0",
                "choices": choices,
                "applied": None,
            },
        },
    }
    monkeypatch.setattr(
        srv_mod,
        "usb_mic_leg_choices",
        lambda env: choices if env == {"JASPER_AUDIO_INPUT_PROFILE": "fresh"}
        else pytest.fail(
            "choice validation must use the fresh reconciled environment"
        ),
    )
    monkeypatch.setattr(
        srv_mod._aec_endpoints,
        "_fresh_jasper_env",
        lambda: {"JASPER_AUDIO_INPUT_PROFILE": "fresh"},
    )
    monkeypatch.setattr(srv_mod, "read_usb_mic_leg", lambda: "primary")
    monkeypatch.setattr(
        srv_mod,
        "write_usb_mic_leg",
        lambda leg: events.append(("write", leg)),
    )

    def fake_manage(unit, **kwargs):
        events.append(("restart", unit, kwargs))
        return {"ok": True}

    monkeypatch.setattr(srv_mod.restart_broker, "manage_units", fake_manage)
    monkeypatch.setattr(srv_mod, "_aec_full_status", lambda: final_status)
    monkeypatch.setattr(
        srv_mod,
        "_schedule_usb_gadget_recompose",
        lambda: pytest.fail("source selection must not recompose the gadget"),
    )
    monkeypatch.setattr(
        srv_mod,
        "_kick_aec_reconciler",
        lambda: pytest.fail("source selection must not run the reconciler"),
    )

    status, body = _post(
        f"{base}/aec/usb-mic-leg",
        {"leg": "raw0"},
    )

    assert status == 200
    assert body == final_status
    assert events == [
        ("write", "raw0"),
        (
            "restart",
            "jasper-aec-bridge.service",
            {
                "verb": "reset-failed",
                "reason": "usb_mic_leg",
                "no_block": False,
                "timeout": 5.0,
            },
        ),
        (
            "restart",
            "jasper-aec-bridge.service",
            {
                "verb": "restart",
                "reason": "usb_mic_leg",
                "no_block": True,
                "timeout": 5.0,
            },
        ),
    ]


def test_usb_mic_leg_rejects_choice_not_advertised_by_server(
    monkeypatch,
    server_with_coordinator,
):
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    choices = [{"value": "primary", "label": "Same as JTS voice"}]
    monkeypatch.setattr(srv_mod._aec_endpoints, "_fresh_jasper_env", lambda: {})
    monkeypatch.setattr(srv_mod, "usb_mic_leg_choices", lambda _env: choices)
    monkeypatch.setattr(
        srv_mod,
        "write_usb_mic_leg",
        lambda _leg: pytest.fail("unavailable choice must not be persisted"),
    )
    monkeypatch.setattr(
        srv_mod.restart_broker,
        "manage_units",
        lambda *_args, **_kwargs: pytest.fail(
            "unavailable choice must not restart any unit"
        ),
    )

    status, body = _post(
        f"{base}/aec/usb-mic-leg",
        {"leg": "chip_aec_210"},
    )

    assert status == 409
    assert body["requested_leg"] == "chip_aec_210"
    assert body["choices"] == choices


def test_usb_mic_leg_same_value_is_noop(
    monkeypatch,
    server_with_coordinator,
):
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    final_status = {
        "usb_mic": {
            "source_selection": {
                "requested": "primary",
                "applied": {"value": "primary"},
            },
        },
    }
    monkeypatch.setattr(srv_mod._aec_endpoints, "_fresh_jasper_env", lambda: {})
    monkeypatch.setattr(
        srv_mod,
        "usb_mic_leg_choices",
        lambda _env: [{"value": "primary", "label": "Same as JTS voice"}],
    )
    monkeypatch.setattr(srv_mod, "read_usb_mic_leg", lambda: "primary")
    monkeypatch.setattr(
        srv_mod,
        "write_usb_mic_leg",
        lambda _leg: pytest.fail("same-value save must not write"),
    )
    monkeypatch.setattr(
        srv_mod.restart_broker,
        "manage_units",
        lambda *_args, **_kwargs: pytest.fail("same-value save must not restart"),
    )
    monkeypatch.setattr(srv_mod, "_aec_full_status", lambda: final_status)

    status, body = _post(f"{base}/aec/usb-mic-leg", {"leg": "primary"})

    assert status == 200
    assert body == final_status


def test_usb_mic_leg_coalesces_pending_apply_then_retries_after_timeout(
    monkeypatch,
    server_with_coordinator,
):
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    state = {"leg": "primary"}
    clock = {"now": 100.0}
    calls = []
    choices = [
        {"value": "primary", "label": "Same as JTS voice"},
        {"value": "chip_aec_210", "label": "Rear hardware beam"},
    ]
    pending_status = {
        "usb_mic": {
            "source_selection": {
                "requested": "chip_aec_210",
                "applied": {"value": "primary"},
            },
        },
    }
    monkeypatch.setattr(srv_mod, "_usb_mic_leg_apply_pending", None)
    monkeypatch.setattr(srv_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(srv_mod._aec_endpoints, "_fresh_jasper_env", lambda: {})
    monkeypatch.setattr(srv_mod, "usb_mic_leg_choices", lambda _env: choices)
    monkeypatch.setattr(srv_mod, "read_usb_mic_leg", lambda: state["leg"])
    monkeypatch.setattr(
        srv_mod,
        "write_usb_mic_leg",
        lambda leg: state.__setitem__("leg", leg),
    )
    monkeypatch.setattr(
        srv_mod.restart_broker,
        "manage_units",
        lambda unit, **kwargs: calls.append((unit, kwargs["verb"])) or {"ok": True},
    )
    monkeypatch.setattr(srv_mod, "_aec_full_status", lambda: pending_status)

    for _request in range(2):
        status, body = _post(
            f"{base}/aec/usb-mic-leg",
            {"leg": "chip_aec_210"},
        )
        assert status == 200
        assert body == pending_status

    assert calls == [
        ("jasper-aec-bridge.service", "reset-failed"),
        ("jasper-aec-bridge.service", "restart"),
    ]

    clock["now"] += srv_mod._USB_MIC_LEG_APPLY_COALESCE_SECONDS + 0.1
    status, body = _post(
        f"{base}/aec/usb-mic-leg",
        {"leg": "chip_aec_210"},
    )

    assert status == 200
    assert body == pending_status
    assert calls == [
        ("jasper-aec-bridge.service", verb)
        for _attempt in range(2)
        for verb in ("reset-failed", "restart")
    ]


def test_usb_mic_leg_failed_schedule_does_not_suppress_immediate_retry(
    monkeypatch,
    server_with_coordinator,
):
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    state = {"leg": "primary"}
    calls = []
    choices = [
        {"value": "primary", "label": "Same as JTS voice"},
        {"value": "chip_aec_210", "label": "Rear hardware beam"},
    ]
    pending_status = {
        "usb_mic": {
            "source_selection": {
                "requested": "chip_aec_210",
                "applied": {"value": "primary"},
            },
        },
    }
    monkeypatch.setattr(srv_mod, "_usb_mic_leg_apply_pending", None)
    monkeypatch.setattr(srv_mod._aec_endpoints, "_fresh_jasper_env", lambda: {})
    monkeypatch.setattr(srv_mod, "usb_mic_leg_choices", lambda _env: choices)
    monkeypatch.setattr(srv_mod, "read_usb_mic_leg", lambda: state["leg"])
    monkeypatch.setattr(
        srv_mod,
        "write_usb_mic_leg",
        lambda leg: state.__setitem__("leg", leg),
    )

    def manage(unit, **kwargs):
        calls.append((unit, kwargs["verb"]))
        return {"ok": len(calls) != 2}

    monkeypatch.setattr(srv_mod.restart_broker, "manage_units", manage)
    monkeypatch.setattr(srv_mod, "_aec_full_status", lambda: pending_status)

    first_status, first_body = _post(
        f"{base}/aec/usb-mic-leg",
        {"leg": "chip_aec_210"},
    )
    second_status, second_body = _post(
        f"{base}/aec/usb-mic-leg",
        {"leg": "chip_aec_210"},
    )

    assert first_status == 502
    assert first_body["code"] == "usb_mic_leg_restart_failed"
    assert second_status == 200
    assert second_body == pending_status
    assert calls == [
        ("jasper-aec-bridge.service", verb)
        for _attempt in range(2)
        for verb in ("reset-failed", "restart")
    ]


def test_usb_mic_leg_repeated_changes_reset_reboot_budget_before_restart(
    monkeypatch,
    server_with_coordinator,
):
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    state = {"leg": "primary"}
    calls = []
    choices = [
        {"value": "primary", "label": "Same as JTS voice"},
        {"value": "chip_aec_210", "label": "Rear hardware beam"},
    ]
    monkeypatch.setattr(srv_mod._aec_endpoints, "_fresh_jasper_env", lambda: {})
    monkeypatch.setattr(srv_mod, "usb_mic_leg_choices", lambda _env: choices)
    monkeypatch.setattr(srv_mod, "read_usb_mic_leg", lambda: state["leg"])
    monkeypatch.setattr(
        srv_mod,
        "write_usb_mic_leg",
        lambda leg: state.__setitem__("leg", leg),
    )
    monkeypatch.setattr(
        srv_mod.restart_broker,
        "manage_units",
        lambda unit, **kwargs: calls.append((unit, kwargs["verb"])) or {"ok": True},
    )
    monkeypatch.setattr(srv_mod, "_aec_full_status", lambda: {"usb_mic": {}})

    for leg in ("chip_aec_210", "primary") * 3:
        status, _body = _post(f"{base}/aec/usb-mic-leg", {"leg": leg})
        assert status == 200

    assert calls == [
        ("jasper-aec-bridge.service", verb)
        for _change in range(6)
        for verb in ("reset-failed", "restart")
    ]


def test_usb_mic_recompose_is_handed_to_durable_systemd_job(monkeypatch):
    import jasper.control.server as srv_mod

    commands = []
    events = []

    monkeypatch.setattr(
        srv_mod.subprocess,
        "run",
        lambda command, **_kwargs: commands.append(command) or _SystemctlResult(),
    )
    monkeypatch.setattr(
        srv_mod,
        "log_event",
        lambda _logger, event, **fields: events.append((event, fields)),
    )

    assert srv_mod._schedule_usb_gadget_recompose() is True

    assert commands == [
        ["systemctl", "reset-failed", "jasper-usbmic-apply.service"],
        [
            "systemctl", "restart", "--no-block",
            "jasper-usbmic-apply.service",
        ],
    ]
    assert events == [(
        "usb_mic.recompose_scheduled",
        {
            "unit": "jasper-usbmic-apply.service",
            "grace_ms": 350,
            "max_attempts": 4,
        },
    )]


def test_usb_mic_recompose_schedule_failure_is_observable(monkeypatch):
    import jasper.control.server as srv_mod

    events = []

    def run(command, **_kwargs):
        if "restart" in command:
            return _SystemctlResult(1, "access denied\n")
        return _SystemctlResult()

    monkeypatch.setattr(srv_mod.subprocess, "run", run)
    monkeypatch.setattr(
        srv_mod,
        "log_event",
        lambda _logger, event, **fields: events.append((event, fields)),
    )

    assert srv_mod._schedule_usb_gadget_recompose() is False

    assert events == [(
        "usb_mic.recompose_failed",
        {
            "unit": "jasper-usbmic-apply.service",
            "phase": "enqueue",
            "returncode": 1,
            "detail": "access denied",
            "level": srv_mod.logging.ERROR,
        },
    )]


def test_usb_mic_recompose_survives_reset_failed_against_a_gcd_unit(monkeypatch):
    """#3237: jasper-usbmic-apply.service is a bare oneshot with no
    RemainAfterExit, so systemd normally GCs it between runs and
    reset-failed exits nonzero as routine idle state. That must not abort
    the recompose before the restart is attempted, and it must not be
    reported through the same event as an actual scheduling failure.
    """
    import jasper.control.server as srv_mod

    commands = []
    events = []

    def run(command, **_kwargs):
        commands.append(command)
        if "reset-failed" in command:
            return _SystemctlResult(
                1, "Unit jasper-usbmic-apply.service not loaded.\n",
            )
        return _SystemctlResult()

    monkeypatch.setattr(srv_mod.subprocess, "run", run)
    monkeypatch.setattr(
        srv_mod,
        "log_event",
        lambda _logger, event, **fields: events.append((event, fields)),
    )

    assert srv_mod._schedule_usb_gadget_recompose() is True

    assert commands == [
        ["systemctl", "reset-failed", "jasper-usbmic-apply.service"],
        [
            "systemctl", "restart", "--no-block",
            "jasper-usbmic-apply.service",
        ],
    ]
    assert events == [
        (
            "usb_mic.reset_failed_skipped",
            {
                "unit": "jasper-usbmic-apply.service",
                "returncode": 1,
                "detail": "Unit jasper-usbmic-apply.service not loaded.",
                "level": srv_mod.logging.WARNING,
            },
        ),
        (
            "usb_mic.recompose_scheduled",
            {
                "unit": "jasper-usbmic-apply.service",
                "grace_ms": 350,
                "max_attempts": 4,
            },
        ),
    ]


def test_usb_mic_recompose_survives_reset_failed_raising(monkeypatch):
    """An exception from the best-effort reset-failed step (e.g. a
    subprocess timeout) must not skip the restart either.
    """
    import jasper.control.server as srv_mod

    commands = []
    events = []

    def run(command, **_kwargs):
        commands.append(command)
        if "reset-failed" in command:
            raise srv_mod.subprocess.TimeoutExpired(cmd=command, timeout=5.0)
        return _SystemctlResult()

    monkeypatch.setattr(srv_mod.subprocess, "run", run)
    monkeypatch.setattr(
        srv_mod,
        "log_event",
        lambda _logger, event, **fields: events.append((event, fields)),
    )

    assert srv_mod._schedule_usb_gadget_recompose() is True
    assert commands == [
        ["systemctl", "reset-failed", "jasper-usbmic-apply.service"],
        [
            "systemctl", "restart", "--no-block",
            "jasper-usbmic-apply.service",
        ],
    ]
    assert [event for event, _fields in events] == [
        "usb_mic.reset_failed_skipped",
        "usb_mic.recompose_scheduled",
    ]


def test_usb_mic_recompose_fails_when_restart_raises(monkeypatch):
    """The restart step stays fatal even when systemctl itself errors, and
    no recompose_scheduled event follows the failure.
    """
    import jasper.control.server as srv_mod

    commands = []
    events = []

    def run(command, **_kwargs):
        commands.append(command)
        if "restart" in command:
            raise srv_mod.subprocess.TimeoutExpired(cmd=command, timeout=5.0)
        return _SystemctlResult()

    monkeypatch.setattr(srv_mod.subprocess, "run", run)
    monkeypatch.setattr(
        srv_mod,
        "log_event",
        lambda _logger, event, **fields: events.append((event, fields)),
    )

    assert srv_mod._schedule_usb_gadget_recompose() is False
    assert commands == [
        ["systemctl", "reset-failed", "jasper-usbmic-apply.service"],
        [
            "systemctl", "restart", "--no-block",
            "jasper-usbmic-apply.service",
        ],
    ]
    assert [event for event, _fields in events] == ["usb_mic.recompose_failed"]


def test_aec_commission_starts_oneshot_when_idle(
    monkeypatch, server_with_coordinator,
):
    """POST /aec/commission on an idle box resets then no-block-starts the
    root measurement oneshot and answers with the full /aec status body."""
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    commands: list[list[str]] = []
    monkeypatch.setattr(srv_mod, "_aec_commission_running", lambda: False)
    monkeypatch.setattr(
        srv_mod.subprocess,
        "run",
        lambda command, **_kwargs: commands.append(command) or _SystemctlResult(),
    )
    monkeypatch.setattr(
        srv_mod,
        "_aec_full_status",
        lambda: {"commission": {"running": True}},
    )

    status, body = _post(f"{base}/aec/commission", None)

    assert status == 200
    assert body == {"commission": {"running": True}}
    assert commands == [
        ["systemctl", "reset-failed", "jasper-aec-commission.service"],
        ["systemctl", "start", "--no-block", "jasper-aec-commission.service"],
    ]


def test_aec_commission_409_while_a_run_is_active(
    monkeypatch, server_with_coordinator,
):
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    monkeypatch.setattr(srv_mod, "_aec_commission_running", lambda: True)
    monkeypatch.setattr(
        srv_mod.subprocess,
        "run",
        lambda *_a, **_k: pytest.fail("an active run must not be started again"),
    )

    status, body = _post(f"{base}/aec/commission", None)

    assert status == 409
    assert body["commission"] == {"running": True, "state": "", "detail": ""}


def test_aec_commission_502_when_the_unit_will_not_start(
    monkeypatch, server_with_coordinator,
):
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    monkeypatch.setattr(srv_mod, "_aec_commission_running", lambda: False)
    monkeypatch.setattr(srv_mod, "_start_aec_commission", lambda: False)

    status, body = _post(f"{base}/aec/commission", None)

    assert status == 502
    assert body["code"] == "aec_commission_start_failed"
    assert body["commission"] == {"running": False, "state": "", "detail": ""}


def test_aec_commission_concurrent_second_click_starts_nothing(
    monkeypatch, server_with_coordinator,
):
    """Two interleaved POSTs race check-then-start: exactly one start, the
    loser answers 409.

    The first start blocks while a second POST is issued. Without the lock
    the second request reaches the probe DURING the held start — the probe
    snapshots `running` before releasing the first start, so it reads False
    and a second start fires (starts == 2, both 200). With the lock the
    second request cannot probe until the first completes, so it sees
    running=True and answers 409 with nothing started."""
    import threading

    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    state = {"running": False, "starts": 0, "checks": 0}
    first_start_entered = threading.Event()
    release_first_start = threading.Event()

    def fake_running():
        value = state["running"]
        state["checks"] += 1
        if state["checks"] == 2:
            # The second click has reached the probe — only now may the
            # held first start finish. `value` was snapshotted first, so a
            # lockless overlap deterministically reads stale False.
            release_first_start.set()
        return value

    def fake_start():
        state["starts"] += 1
        first_start_entered.set()
        # Short bound: on the locked (correct) path nothing sets the event
        # while this start is held, and the expiry is what completes it.
        release_first_start.wait(timeout=0.5)
        state["running"] = True
        return True

    monkeypatch.setattr(srv_mod, "_aec_commission_running", fake_running)
    monkeypatch.setattr(srv_mod, "_start_aec_commission", fake_start)
    monkeypatch.setattr(
        srv_mod,
        "_aec_full_status",
        lambda: {"commission": {"running": True}},
    )

    results: list[tuple[int, dict]] = []
    first = threading.Thread(
        target=lambda: results.append(_post(f"{base}/aec/commission", None)),
    )
    first.start()
    assert first_start_entered.wait(timeout=5)
    second = threading.Thread(
        target=lambda: results.append(_post(f"{base}/aec/commission", None)),
    )
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)

    assert state["starts"] == 1
    assert sorted(status for status, _body in results) == [200, 409]


def test_aec_firmware_update_starts_when_required(
    monkeypatch, server_with_coordinator,
):
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    starts = []
    status_payload = {
        "firmware_update": {
            "state": "update_required",
            "detail": "2-channel firmware detected",
            "target": {"id": "legacy_square_6ch"},
            "action": {"enabled": True},
        }
    }
    monkeypatch.setattr(srv_mod, "_aec_full_status", lambda: status_payload)
    monkeypatch.setattr(
        srv_mod, "_start_xvf_firmware_update", lambda: starts.append("start"),
    )

    status, body = _post(f"{base}/aec/firmware/update", {})

    assert status == 200
    assert body == status_payload
    assert starts == ["start"]


def test_aec_firmware_update_refuses_when_not_available(
    monkeypatch, server_with_coordinator,
):
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    starts = []
    monkeypatch.setattr(
        srv_mod,
        "_aec_full_status",
        lambda: {
            "firmware_update": {
                "state": "current",
                "detail": "Microphone firmware is current",
                "action": {"enabled": False},
            }
        },
    )
    monkeypatch.setattr(
        srv_mod, "_start_xvf_firmware_update", lambda: starts.append("start"),
    )

    status, body = _post(f"{base}/aec/firmware/update", {})

    assert status == 409
    assert body["error"] == "Microphone firmware is current"
    assert starts == []


def test_enhanced_aec_get_uses_dedicated_status(
    monkeypatch, server_with_coordinator,
):
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    expected = {
        "schema_version": 1,
        "feature": "enhanced_aec",
        "state": "not_installed",
        "action": {"enabled": True, "label": "Install enhancement"},
    }
    monkeypatch.setattr(srv_mod, "_enhanced_aec_status", lambda: expected)

    status, body = _get(f"{base}/aec/enhanced-aec")

    assert status == 200
    assert body == expected


def test_enhanced_aec_post_persists_then_starts_allowlisted_oneshot(
    monkeypatch, server_with_coordinator,
):
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod
    from jasper import enhanced_aec

    statuses = iter([
        {
            "state": "not_installed",
            "action": {"enabled": True, "label": "Install enhancement"},
        },
        {
            "state": "installing",
            "requested": True,
            "action": {"enabled": False, "label": "Install enhancement"},
        },
    ])
    calls: list[tuple] = []
    monkeypatch.setattr(srv_mod, "_enhanced_aec_status", lambda: next(statuses))
    monkeypatch.setattr(
        enhanced_aec,
        "request_install",
        lambda: calls.append(("intent",)),
    )

    def fake_manage(*units, **kwargs):
        calls.append(("broker", units, kwargs))
        return {"ok": True}

    monkeypatch.setattr(srv_mod.restart_broker, "manage_units", fake_manage)

    status, body = _post(f"{base}/aec/enhanced-aec/install", {})

    assert status == 200
    assert body["state"] == "installing"
    assert calls[0] == ("intent",)
    assert calls[1][0:2] == (
        "broker",
        ("jasper-enhanced-aec-install.service",),
    )
    assert calls[1][2]["verb"] == "start"
    assert calls[1][2]["no_block"] is True
