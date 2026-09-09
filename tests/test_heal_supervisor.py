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


def _supervisor(
    monkeypatch: pytest.MonkeyPatch,
    *,
    code: str = "clean",
    status: str = "ok",
    warmup: bool = False,
    guards_ok: bool = True,
    voice: dict[str, Any] | None = None,
    profile_expects_wake: bool = True,
) -> heal.HealSupervisor:
    supervisor = heal.HealSupervisor()
    monkeypatch.setattr(heal, "guards_active", lambda: guards_ok)
    monkeypatch.setattr(heal, "profile_expects_wake", lambda: profile_expects_wake)
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
        heal.ACTION_RESTART_AUDIO if case == "silent"
        else heal.ACTION_RESTART_VOICE
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
