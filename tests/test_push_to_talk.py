# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for `jasper.voice.push_to_talk.PushToTalk`: the zero-wake-leg
derivation and the hold-cap decision, constructed and read directly with no
`WakeLoop` involved (#2205)."""
from __future__ import annotations

import logging

import pytest

from jasper.voice.push_to_talk import (
    HARD_RECORDING_CAP_SEC,
    PTT_MIN_INPUT_CAP_SEC,
    PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC,
)
from tests._log_events import event_fields, event_records
from tests._manual_mics import remote_mic


def _remote_runtime():
    return [remote_mic()]


def test_push_to_talk_only_is_derived_from_resolved_runtime():
    """The daemon knows it is push-to-talk from what it actually opened —
    zero wake legs plus at least one manual mic source — never from a config
    string it might have inherited from a default."""
    from jasper.voice.push_to_talk import PushToTalk

    assert PushToTalk(_remote_runtime(), have_wake_legs=False).only is True
    # Zero legs and no manual source is a broken speaker, not a PTT one.
    assert PushToTalk([], have_wake_legs=False).only is False
    # A remote on a speaker that also has a room mic is additive.
    assert PushToTalk(_remote_runtime(), have_wake_legs=True).only is False


def test_push_to_talk_only_is_the_single_derivation_its_consumers_read():
    """Pins only what `PushToTalk` itself computes: `.only` and `.sources`
    from the given runtime, at construction time, with no re-derivation on
    read. That every consumer — `WakeLoop.session_status`, `run()`'s
    keepalive branch, and the source-less start refusal — actually reads
    `WakeLoop._push_to_talk.only`/`.sources` rather than re-deriving the
    mode from `self._mic is None` is pinned at the loop level by
    test_session_status_surfaces_the_ptt_keys_from_a_real_loop and
    test_zero_leg_run_ticks_the_heartbeat_without_a_primary_mic (both in
    tests/test_voice_daemon_push_to_talk_only.py) and by
    test_source_less_refusal_reads_the_single_derivation in
    tests/test_voice_daemon_manual_start_guard.py."""
    from jasper.voice.push_to_talk import PushToTalk

    ptt = PushToTalk(_remote_runtime(), have_wake_legs=False)
    assert ptt.only is True
    assert list(ptt.sources) == ["wiim_remote_2"]

    # A speaker WITH a room mic reports the mode off.
    other = PushToTalk(_remote_runtime(), have_wake_legs=True)
    assert other.only is False


def _daemon_heartbeat_stale_threshold() -> float:
    """The stale threshold the DAEMON actually runs with.

    Read from `jasper/voice/daemon_main.py`'s own `Heartbeat(...)` call, not
    from the constructor's signature default: those two happen to be the same
    number today, so a guard that read the signature would be correct only by
    coincidence and would keep passing if the daemon started asking for a
    tighter threshold. Parsed with `ast` rather than by line number so a
    refactor moves it for free (AGENTS.md documentation rule 5).
    """
    import ast
    import inspect
    from pathlib import Path

    import jasper
    from jasper.watchdog import Heartbeat

    source = (
        Path(jasper.__file__).parent / "voice" / "daemon_main.py"
    ).read_text(encoding="utf-8")
    calls = [
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Heartbeat"
    ]
    assert len(calls) == 1, (
        f"expected exactly one Heartbeat(...) construction in daemon_main.py, "
        f"found {len(calls)} — this guard must read the live one"
    )
    for kw in calls[0].keywords:
        if kw.arg == "stale_threshold_sec":
            return float(ast.literal_eval(kw.value))
    # No explicit value: the daemon runs on the constructor default.
    return float(
        inspect.signature(Heartbeat).parameters["stale_threshold_sec"].default
    )


def test_ptt_keepalive_stays_inside_heartbeat_stale_threshold():
    """Load-bearing relationship: with no mic frames to bump the progress
    sentinel, the keepalive tick IS the liveness proof. If its interval ever
    drifts past the threshold the daemon asks for, the heartbeat thread stops
    patting systemd and WatchdogSec=30s reaps a perfectly healthy daemon."""
    from jasper.voice.push_to_talk import PTT_KEEPALIVE_INTERVAL_SEC

    stale = _daemon_heartbeat_stale_threshold()
    assert PTT_KEEPALIVE_INTERVAL_SEC < stale, (
        f"keepalive {PTT_KEEPALIVE_INTERVAL_SEC}s must stay under the "
        f"{stale}s heartbeat stale threshold jasper-voice constructs with"
    )


def _shipped_idle_timeout_default() -> int:
    """The `JASPER_IDLE_TIMEOUT_SEC` default, taken from a real
    `Config.from_env` with the knob unset rather than restated here — a
    duplicated literal is exactly how the ordering these tests pin would
    silently stop holding.
    """
    import os
    from unittest import mock

    from jasper.config import Config

    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"JASPER_IDLE_TIMEOUT_SEC", "JASPER_VOICE_PROVIDER"}
    }
    env["JASPER_VOICE_PROVIDER"] = "gemini"
    env.setdefault("GEMINI_API_KEY", "test-key")
    with mock.patch.dict(os.environ, env, clear=True):
        return Config.from_env().idle_timeout_sec


def test_at_the_shipped_default_the_hold_cap_beats_the_idle_watchdog():
    """The load-bearing ordering *at the shipped default*, read from the
    config rather than hardcoded. Deliberately not a general property —
    it does not hold at every `idle_timeout_sec`, and the two degraded
    bands below are where it stops holding.

    `_idle_watchdog`'s pre-response timer is anchored at turn OPEN and
    fires at `JASPER_IDLE_TIMEOUT_SEC` when no model chunk has arrived —
    and none can while input is open, because `last_activity_at()` tracks
    *model* activity. `_end_turn` then cancels `_play_responses` BEFORE
    calling `end_input`, so losing this race means the user gets no answer
    at all. The hold cap must close input early enough that the model can
    still start speaking inside the same window.
    """
    from jasper.voice.push_to_talk import (
        PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC,
        PushToTalk,
    )

    shipped_idle_timeout = _shipped_idle_timeout_default()

    ptt = PushToTalk([], have_wake_legs=True)
    cap = ptt.input_cap_sec(shipped_idle_timeout)

    assert cap < shipped_idle_timeout, (
        f"push-to-talk hold cap {cap}s does not fire before the idle "
        f"watchdog at {shipped_idle_timeout}s; a long hold loses its answer"
    )
    assert shipped_idle_timeout - cap >= PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC


def test_hard_recording_cap_alone_would_lose_the_race():
    """Why the cap is derived rather than just HARD_RECORDING_CAP_SEC.

    Pins the arithmetic that made the first version of this change wrong:
    at the shipped defaults the 30 s constant sits ABOVE the 20 s idle
    timeout, so on its own it can never fire.
    """
    from jasper.voice.push_to_talk import HARD_RECORDING_CAP_SEC

    assert HARD_RECORDING_CAP_SEC > _shipped_idle_timeout_default()


_BOTH_CAP_EVENTS = {
    "manual_mic.hold_cap_unreachable", "manual_mic.idle_timeout_too_low",
}


@pytest.mark.parametrize(
    "idle_timeout_sec, expected_cap, expected_event, expected_fields",
    [
        pytest.param(
            3, PTT_MIN_INPUT_CAP_SEC, "manual_mic.hold_cap_unreachable",
            {
                "cap_sec": PTT_MIN_INPUT_CAP_SEC,
                "idle_timeout_sec": 3.0,
                "needs_sec": (
                    PTT_MIN_INPUT_CAP_SEC + PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC
                ),
            },
            id="unreachable_floor",
        ),
        pytest.param(
            # 5 is the crossing: the watchdog has walked down TO the floor,
            # so this is the last timeout at which the cap cannot fire.
            5, PTT_MIN_INPUT_CAP_SEC, "manual_mic.hold_cap_unreachable", None,
            id="unreachable_crossing",
        ),
        pytest.param(
            # 6 is the first at which it can — one second either side of the
            # boundary must not be reported as the same verdict.
            6, PTT_MIN_INPUT_CAP_SEC, "manual_mic.idle_timeout_too_low", None,
            id="too_low_just_past_crossing",
        ),
        pytest.param(
            # The floor still wins (5 s < 10 s) but leaves the model 5 s
            # where the allowance asks for 6 — a slow first chunk still
            # loses the answer, silently, unless this warns.
            10, PTT_MIN_INPUT_CAP_SEC, "manual_mic.idle_timeout_too_low",
            {
                "cap_sec": PTT_MIN_INPUT_CAP_SEC,
                "idle_timeout_sec": 10.0,
                "needs_sec": (
                    PTT_MIN_INPUT_CAP_SEC + PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC
                ),
            },
            id="too_low_squeezed",
        ),
        pytest.param(
            # 11 = floor + allowance: the full allowance is restored, silence.
            11, PTT_MIN_INPUT_CAP_SEC, None, None,
            id="silent_allowance_restored",
        ),
        pytest.param(
            _shipped_idle_timeout_default(),
            _shipped_idle_timeout_default() - PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC,
            None, None,
            id="silent_shipped_default",
        ),
        pytest.param(
            30, 30 - PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC, None, None,
            id="silent_above_default",
        ),
        pytest.param(
            # Retuning the idle timeout moves the cap with it, but never
            # past the absolute stuck-button ceiling.
            600, HARD_RECORDING_CAP_SEC, None, None,
            id="silent_hard_ceiling",
        ),
    ],
)
def test_hold_cap_bands_and_values(
    caplog, idle_timeout_sec, expected_cap, expected_event, expected_fields,
):
    """`input_cap_sec`'s full derivation, one `idle_timeout_sec` at a time:
    the cap value, which of the two degraded-band events (if any) it
    reports, and — at the two values the per-band cases originally checked
    exactly — the WARN's fields.

    "The cap still fires but leaves the model squeezed"
    (`idle_timeout_too_low`) and "the watchdog reaps the turn before the
    cap can even fire" (`hold_cap_unreachable`) are different verdicts
    with different remedies, and an off-by-one in the comparison that
    separates them would silently report one as the other — hence the
    boundary rows (5/6) and the point the softer band clears (11).
    """
    from jasper.voice.push_to_talk import PushToTalk

    ptt = PushToTalk([], have_wake_legs=True)

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        cap = ptt.input_cap_sec(idle_timeout_sec)

    assert cap == expected_cap

    fired = {name for name in _BOTH_CAP_EVENTS if event_records(caplog, name)}
    assert fired == ({expected_event} if expected_event else set()), (
        f"idle_timeout_sec={idle_timeout_sec} should report "
        f"{expected_event or 'nothing'}, got {fired or 'nothing'}"
    )
    if expected_fields is not None:
        fields = event_fields(caplog, expected_event)
        assert float(fields["cap_sec"]) == expected_fields["cap_sec"]
        assert (
            float(fields["idle_timeout_sec"]) == expected_fields["idle_timeout_sec"]
        )
        assert float(fields["needs_sec"]) == expected_fields["needs_sec"]


@pytest.mark.parametrize(
    "idle_timeout_sec, event",
    [
        pytest.param(3, "manual_mic.hold_cap_unreachable", id="unreachable"),
        pytest.param(10, "manual_mic.idle_timeout_too_low", id="too_low"),
    ],
)
def test_hold_cap_warn_is_a_one_shot_latch(caplog, idle_timeout_sec, event):
    """`_cap_warned` is shared across both degraded bands and set on the
    first fire, so a button held — released, then held again — all night
    logs its verdict once per daemon lifetime, never once per turn."""
    from jasper.voice.push_to_talk import PushToTalk

    other = (_BOTH_CAP_EVENTS - {event}).pop()
    ptt = PushToTalk([], have_wake_legs=True)

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        ptt.input_cap_sec(idle_timeout_sec)
        ptt.input_cap_sec(idle_timeout_sec)  # one-shot latch: no second WARN

    event_fields(caplog, event)  # asserts exactly one record fired
    assert event_records(caplog, other) == []
