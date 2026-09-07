# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for `jasper.voice.push_to_talk.PushToTalk`: the zero-wake-leg
derivation and the hold-cap decision, constructed and read directly with no
`WakeLoop` involved (#2205)."""
from __future__ import annotations

import logging

import pytest

from tests._log_events import event_fields, event_records


def _remote_runtime():
    from jasper.voice.push_to_talk import ManualMicRuntime
    return [ManualMicRuntime("wiim_remote_2", object(), "udp:9892")]


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


def test_hold_cap_is_derived_from_the_operators_idle_timeout():
    """Retuning JASPER_IDLE_TIMEOUT_SEC moves the cap with it, so the two
    cannot drift apart."""
    from jasper.voice.push_to_talk import (
        HARD_RECORDING_CAP_SEC,
        PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC,
        PushToTalk,
    )

    ptt = PushToTalk([], have_wake_legs=True)

    assert ptt.input_cap_sec(20) == 20 - PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC
    assert ptt.input_cap_sec(30) == 30 - PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC

    # ...but never past the absolute stuck-button ceiling.
    assert ptt.input_cap_sec(600) == HARD_RECORDING_CAP_SEC


def test_hold_cap_warns_when_a_low_idle_timeout_squeezes_the_model(caplog):
    """`PTT_MIN_INPUT_CAP_SEC` keeps the button usable under a very low
    idle timeout, at the cost of the model's response allowance. That is a
    degraded configuration and must say so.

    The interesting case is NOT only "the cap can no longer win the race".
    At `idle_timeout_sec = 10` the cap still fires first (5 s < 10 s) but
    leaves the model 5 s where the allowance asks for 6 — a slow first
    chunk still loses the answer, silently, unless this warns.
    """
    from jasper.voice.push_to_talk import (
        PTT_MIN_INPUT_CAP_SEC,
        PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC,
        PushToTalk,
    )

    ptt = PushToTalk([], have_wake_legs=True)

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        cap = ptt.input_cap_sec(10)
        ptt.input_cap_sec(10)  # one-shot latch: no second WARN

    # The floor won, and it still beats the watchdog...
    assert cap == PTT_MIN_INPUT_CAP_SEC
    assert cap < 10
    # ...but the model is left less than its allowance, which is the point.
    assert 10 - cap < PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC
    # Exactly one record is the one-shot latch; the fields are the verdict.
    fields = event_fields(caplog, "manual_mic.idle_timeout_too_low")
    assert float(fields["needs_sec"]) == 11.0
    assert float(fields["cap_sec"]) == float(PTT_MIN_INPUT_CAP_SEC)
    assert float(fields["idle_timeout_sec"]) == 10.0
    # ...and NOT the louder band's event: here the cap does still fire.
    assert event_records(caplog, "manual_mic.hold_cap_unreachable") == []


@pytest.mark.parametrize(
    "idle_timeout, expected_event",
    [
        (3, "manual_mic.hold_cap_unreachable"),
        # 5 is the crossing: the watchdog has walked down TO the floor, so
        # this is the last timeout at which the cap cannot fire.
        (5, "manual_mic.hold_cap_unreachable"),
        # 6 is the first at which it can — one second either side of the
        # boundary must not be reported as the same verdict.
        (6, "manual_mic.idle_timeout_too_low"),
        (10, "manual_mic.idle_timeout_too_low"),
        # 11 = floor + allowance: the full allowance is restored, silence.
        (11, None),
        (20, None),  # the shipped default
    ],
)
def test_hold_cap_degraded_bands_are_reported_distinctly(
    caplog, idle_timeout, expected_event,
):
    """The band boundaries themselves, walked one second at a time.

    "The cap fires but the model is squeezed" and "the cap can never fire"
    are different verdicts with different remedies, and an off-by-one in
    the comparison that separates them silently reports one as the other.
    The spot-check tests below cover the middle of each band; this covers
    the edges, which is where a boundary bug actually lives.
    """
    from jasper.voice.push_to_talk import PushToTalk

    both = {"manual_mic.hold_cap_unreachable", "manual_mic.idle_timeout_too_low"}

    ptt = PushToTalk([], have_wake_legs=True)

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        ptt.input_cap_sec(idle_timeout)

    fired = {name for name in both if event_records(caplog, name)}
    assert fired == ({expected_event} if expected_event else set()), (
        f"idle_timeout_sec={idle_timeout} should report "
        f"{expected_event or 'nothing'}, got {fired or 'nothing'}"
    )


def test_a_very_low_idle_timeout_makes_the_cap_unreachable_and_says_so(caplog):
    """The band the first version of this docstring got wrong.

    `PTT_MIN_INPUT_CAP_SEC` is a constant floor; the watchdog is not. So a
    low enough `idle_timeout_sec` walks the watchdog down *through* the
    floor, and below the crossing the cap can never fire at all — the
    original blocker's exact failure mode, surviving in a narrow band.
    That is worse than a squeezed allowance (there, only a slow first
    chunk loses the answer; here every hold does) and gets its own,
    louder event.
    """
    from jasper.voice.push_to_talk import PTT_MIN_INPUT_CAP_SEC, PushToTalk

    ptt = PushToTalk([], have_wake_legs=True)

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        cap = ptt.input_cap_sec(3)
        ptt.input_cap_sec(3)  # one shared latch: no second WARN

    assert cap == PTT_MIN_INPUT_CAP_SEC
    assert cap >= 3  # the watchdog gets there first
    # Exactly one record is the shared one-shot latch holding.
    fields = event_fields(caplog, "manual_mic.hold_cap_unreachable")
    assert float(fields["cap_sec"]) == float(PTT_MIN_INPUT_CAP_SEC)
    assert float(fields["idle_timeout_sec"]) == 3.0
    # The two bands are distinct verdicts and must not be conflated: the
    # softer one would understate a cap that cannot fire at all.
    assert event_records(caplog, "manual_mic.idle_timeout_too_low") == []


def test_hold_cap_is_silent_when_the_allowance_is_actually_preserved(caplog):
    """Mutation of the warning above: at the shipped default the
    derivation does leave the full allowance, so the WARN must not fire —
    otherwise every household journal carries a permanent false alarm."""
    from jasper.voice.push_to_talk import PushToTalk

    ptt = PushToTalk([], have_wake_legs=True)

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        ptt.input_cap_sec(_shipped_idle_timeout_default())

    assert event_records(caplog, "manual_mic.idle_timeout_too_low") == []
    assert event_records(caplog, "manual_mic.hold_cap_unreachable") == []
