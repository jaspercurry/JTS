# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The heal supervisor: its fact→case map, its episode dedupe, its dry run."""
from __future__ import annotations

import logging
from typing import Any

import pytest

from jasper.control import heal_supervisor as heal

from ._log_events import event_fields, event_records

_NOW = 1_800_000_000.0
_DAY = 24 * 60 * 60.0


_CAMILLA = "jasper-camilla.service"
_RECONCILER = heal.AUDIO_HARDWARE_RECONCILE_UNIT


def _units(
    *,
    camilla_active: bool = True,
    camilla_result: str = "success",
    reconciler_failed: bool = False,
) -> dict[str, dict[str, Any]]:
    """The `systemctl show` records one tick reads, in their real shape."""
    return {
        "jasper-fanin.service": {"load_state": "loaded", "active_state": "active"},
        "jasper-outputd.service": {"load_state": "loaded", "active_state": "active"},
        _CAMILLA: {
            "load_state": "loaded",
            "active_state": "active" if camilla_active else "inactive",
            "result": camilla_result,
        },
        _RECONCILER: {
            "load_state": "loaded",
            "active_state": "failed" if reconciler_failed else "inactive",
            "result": "exit-code" if reconciler_failed else "success",
        },
    }


def _supervisor(
    monkeypatch: pytest.MonkeyPatch,
    *,
    code: str = "clean",
    status: str = "ok",
    warmup: bool = False,
    guards_ok: bool = True,
    units: dict[str, dict[str, Any]] | None = None,
    gate_refused: bool = False,
    voice: dict[str, Any] | None = None,
    profile_expects_wake: bool = True,
) -> heal.HealSupervisor:
    supervisor = heal.HealSupervisor()
    monkeypatch.setattr(
        heal, "read_audio_path_units",
        lambda: _units() if units is None else units,
    )
    if units is None:
        monkeypatch.setattr(heal, "guards_active", lambda _units: guards_ok)
    monkeypatch.setattr(heal, "profile_expects_wake", lambda: profile_expects_wake)

    async def _gate() -> bool:
        return gate_refused

    monkeypatch.setattr(supervisor, "gate_refused", _gate)
    monkeypatch.setattr(
        supervisor, "audio_health",
        lambda: {
            "signal_path": {"code": code, "status": status},
            "technical": {"sampler": {"warmup_active": warmup}},
        },
    )

    async def _voice() -> dict[str, Any] | None:
        return voice

    monkeypatch.setattr(supervisor, "voice_status", _voice)
    return supervisor


def _wake(
    age_sec: float, *, muted: bool = False, ptt_only: bool = False,
) -> dict[str, Any]:
    return {
        "last_wake_at": _NOW - age_sec,
        "mic_muted": muted,
        "push_to_talk_only": ptt_only,
    }


@pytest.mark.parametrize(
    ("code", "status", "warmup", "guards_ok", "voice", "expects_wake", "case"),
    [
        ("output_deaf", "issue", False, True, _wake(60.0), True, "silent"),
        ("path_stalled", "issue", False, True, None, True, "silent"),
        # Warmup is outputd priming an empty ring, not a silent speaker.
        ("output_deaf", "issue", True, True, _wake(60.0), True, None),
        # A down guard unit is systemd's fault to restart, not heal's.
        ("output_stalled", "issue", False, False, _wake(60.0), True, None),
        # A warn-level code is not one restart-audio answers.
        ("path_pressured", "warn", False, True, _wake(60.0), True, None),
        # outputd is not delivering at all; restart-audio does not carry it.
        ("output_backend_inactive", "issue", False, True, _wake(60.0), True, None),
        ("clean", "ok", False, True, _wake(2 * _DAY), True, "deaf"),
        # A muted mic explains the missing wake word by itself.
        ("clean", "ok", False, True, _wake(2 * _DAY, muted=True), True, None),
        # The daemon's own "this box arms zero wake legs by design".
        ("clean", "ok", False, True, _wake(2 * _DAY, ptt_only=True), True, None),
        # Voice down: `last_wake_at` cannot be read as a fault.
        ("clean", "ok", False, True, None, True, None),
        # A tier that cannot run always-on wake inference at all.
        ("clean", "ok", False, True, _wake(2 * _DAY), False, None),
        ("clean", "ok", False, True, _wake(0.5 * _DAY), True, None),
        # The silent case outranks a stale wake word on the same tick.
        ("output_ring_stalled", "issue", False, True, _wake(2 * _DAY), True, "silent"),
    ],
)
async def test_one_fact_map_from_the_tick_to_one_would_act_line(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    code: str,
    status: str,
    warmup: bool,
    guards_ok: bool,
    voice: dict[str, Any] | None,
    expects_wake: bool,
    case: str | None,
) -> None:
    monkeypatch.setattr(heal.time, "time", lambda: _NOW)
    supervisor = _supervisor(
        monkeypatch, code=code, status=status, warmup=warmup,
        guards_ok=guards_ok, voice=voice, profile_expects_wake=expects_wake,
    )
    caplog.set_level(logging.INFO)

    await supervisor._tick()

    if case is None:
        assert event_records(caplog, "heal.would_act") == []
        assert supervisor.snapshot()["would_act"] is None
        return
    action = (
        heal.ACTION_RESTART_VOICE if case == "deaf"
        else heal.ACTION_RESTART_AUDIO
    )
    assert event_fields(caplog, "heal.would_act") == {
        "case": case,
        "reason": code if case == "silent" else "wake_stale",
        "action": action,
    }


async def test_an_unchanged_fact_does_not_relog_inside_one_episode(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    clock = [_NOW]
    monkeypatch.setattr(heal.time, "time", lambda: clock[0])
    supervisor = _supervisor(monkeypatch, code="output_deaf", status="issue")
    caplog.set_level(logging.INFO)

    await supervisor._tick()
    assert len(event_records(caplog, "heal.would_act")) == 1

    # Same code, a full window later: still one episode, still one line.
    clock[0] = _NOW + heal.WOULD_ACT_WINDOW_SEC + 1
    await supervisor._tick()
    assert len(event_records(caplog, "heal.would_act")) == 1
    # Unchanged posture: `heal.observed` says nothing a second time either.
    assert len(event_records(caplog, "heal.observed")) == 1


async def test_the_snapshot_publishes_the_live_verdict_and_clears_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(heal.time, "time", lambda: _NOW)
    supervisor = _supervisor(monkeypatch, code="output_stalled", status="issue")

    await supervisor._tick()

    assert supervisor.snapshot() == {
        "enabled": True,
        "last_tick": _NOW,
        "would_act": {
            "case": "silent",
            "reason": "output_stalled",
            "action": heal.ACTION_RESTART_AUDIO,
            "ts": _NOW,
        },
    }

    monkeypatch.setattr(
        supervisor, "audio_health",
        lambda: {"signal_path": {"code": "clean", "status": "ok"}},
    )
    await supervisor._tick()
    assert supervisor.snapshot()["would_act"] is None


async def test_a_failing_tick_leaves_the_recency_stamp_where_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The doctor reads `last_tick`, so a tick that raises every pass must not
    keep that row green beside a repeating `heal.tick_crash`."""
    monkeypatch.setattr(heal.time, "time", lambda: _NOW)
    supervisor = _supervisor(monkeypatch)

    async def _unreadable() -> dict[str, Any] | None:
        raise OSError("voice socket gone")

    monkeypatch.setattr(supervisor, "voice_status", _unreadable)

    with pytest.raises(OSError):
        await supervisor._tick()

    assert supervisor.snapshot()["last_tick"] is None


def test_the_module_snapshot_is_disabled_before_the_supervisor_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(heal, "_supervisor", None)
    assert heal.snapshot() == {"enabled": False}


@pytest.mark.parametrize(
    ("camilla_active", "camilla_result", "reconciler_failed", "gate", "reason"),
    [
        # The gate skipped the start: a condition skip is a SUCCESS, so no unit
        # reads failed and nothing systemd owns will re-try it.
        (False, "success", False, True, "topology_gate"),
        # A start job cancelled by a failed requirement dependency.
        (False, "dependency", False, False, "dependency_cancelled"),
        # Running: nothing to heal, whatever the record says happened before.
        (True, "dependency", False, False, None),
        # An operator stop, and a crash: neither is heal's.
        (False, "success", False, False, None),
        (False, "exit-code", False, False, None),
        # The reconciler is still failed: the fault is systemd's own to answer.
        (False, "dependency", True, False, None),
        (False, "success", True, True, None),
    ],
)
def test_only_a_camilla_no_restart_will_retry_reaches_the_stopped_case(
    camilla_active: bool,
    camilla_result: str,
    reconciler_failed: bool,
    gate: bool,
    reason: str | None,
) -> None:
    units = _units(
        camilla_active=camilla_active,
        camilla_result=camilla_result,
        reconciler_failed=reconciler_failed,
    )
    assert heal.camilla_stopped_reason(units, gate) == reason


@pytest.mark.parametrize("record", [None, {}])
def test_units_nobody_could_read_are_unknown_not_stopped(
    record: dict[str, Any] | None,
) -> None:
    """systemctl answering nothing is not evidence CamillaDSP is down."""
    assert heal.camilla_stopped_reason(record, True) is None


async def test_the_gated_graph_publishes_the_dashboard_action_that_starts_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """`restart-audio` IS the start: it carries jasper-camilla and nothing else,
    and a camilla start re-queues the hardware reconciler that re-proves the
    graph. Still observe-only — heal reaches no actuator (ADR-0271)."""
    monkeypatch.setattr(heal.time, "time", lambda: _NOW)
    supervisor = _supervisor(
        monkeypatch,
        units=_units(camilla_active=False),
        gate_refused=True,
        # A stopped CamillaDSP makes the signal path issue a fact about the
        # same outage; the stopped case must outrank it.
        code="output_deaf",
        status="issue",
    )
    caplog.set_level(logging.INFO)

    await supervisor._tick()

    assert event_fields(caplog, "heal.would_act") == {
        "case": heal.CASE_STOPPED,
        "reason": "topology_gate",
        "action": heal.ACTION_RESTART_AUDIO,
    }
    assert supervisor.snapshot()["would_act"] == {
        "case": heal.CASE_STOPPED,
        "reason": "topology_gate",
        "action": heal.ACTION_RESTART_AUDIO,
        "ts": _NOW,
    }


async def test_a_healthy_audio_path_reaches_no_stopped_verdict(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(heal.time, "time", lambda: _NOW)
    supervisor = _supervisor(monkeypatch, units=_units())
    caplog.set_level(logging.INFO)

    await supervisor._tick()

    assert event_records(caplog, "heal.would_act") == []
    assert supervisor.snapshot()["would_act"] is None
