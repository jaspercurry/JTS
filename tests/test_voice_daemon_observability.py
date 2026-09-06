# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import asyncio
from types import SimpleNamespace

import pytest

from jasper.tts_routing import FANIN_TTS_SOCKET
from tests._live_turn_fake import _prep_session_status, silent_frame
from jasper.voice.daemon_main import _tts_ready_detail
from jasper.voice.daemon_main import _serve_while_connecting


def test_tts_ready_detail_reports_outputd_socket() -> None:
    cfg = SimpleNamespace(tts_outputd_socket=FANIN_TTS_SOCKET)

    detail = _tts_ready_detail(cfg)

    assert detail == f"tts_socket={FANIN_TTS_SOCKET}"


def test_wake_ready_detail_names_the_model_when_legs_are_planned() -> None:
    from jasper.voice.daemon_main import _wake_ready_detail

    cfg = SimpleNamespace(wake_model="jarvis_v2")
    assert _wake_ready_detail(cfg, [("on", "Array")]) == "jarvis_v2"


def test_wake_ready_detail_says_disabled_when_no_leg_is_planned() -> None:
    """The startup line must not name a wake model on a speaker that built
    no detector at all — an operator reading `wake=jarvis_v2` on a
    push-to-talk-only box would chase a wake problem that does not exist.

    The exact string is the operator's evidence: the owed #2205 hardware run
    greps the journal for it, so it is pinned here rather than left to drift
    inside run()'s log call.
    """
    from jasper.voice.daemon_main import _wake_ready_detail

    cfg = SimpleNamespace(wake_model="jarvis_v2")
    assert _wake_ready_detail(cfg, []) == "disabled(no wake leg)"


def test_require_usable_input_raises_when_nothing_opened() -> None:
    """A daemon with no wake leg AND no manual mic is permanently deaf.

    Reachable since #2205: a no-room-mic speaker plans zero legs, and the
    manual-mic loop SKIPS a source it cannot open rather than raising. Without
    this guard the daemon would log "ready", keep patting its watchdog on the
    keepalive tick, and never hear anything — a speaker that looks healthy and
    is not. It must park the same clean way a primary mic-open failure does.
    """
    import pytest

    from jasper.audio_io import InputDeviceUnavailable
    from jasper.voice.daemon_main import _require_usable_input

    with pytest.raises(InputDeviceUnavailable) as exc:
        _require_usable_input([], [], ["udp:9892"])
    # The declared-but-unopenable device is named, so the journal says WHICH
    # source was expected rather than just "no mic".
    assert "udp:9892" in str(exc.value)

    with pytest.raises(InputDeviceUnavailable) as exc:
        _require_usable_input([], [], [])
    assert "<none>" in str(exc.value)


def test_require_usable_input_accepts_either_input_alone() -> None:
    """Control: one wake leg is enough, and so is one manual mic on its own —
    the second is the whole point of the accessory-only speaker."""
    from jasper.voice.daemon_main import _require_usable_input

    _require_usable_input([object()], [], [])          # room mic only
    _require_usable_input([], [object()], ["udp:9892"])  # remote only


def test_unpriced_research_model_warns(caplog) -> None:
    # Pins the documented C2-5 behavior: an unpriced research model (e.g.
    # JASPER_RESEARCH_OPENAI_MODEL overridden to a model with no rate) records
    # $0 cost and the daily spend cap can't bound it, so the daemon emits a
    # WARNING. `gpt-realtime-3` is the canonical unknown/unpriced id (shared
    # with test_usage.test_unknown_model_is_unpriced_not_invented).
    import logging

    from jasper.usage import load_pricing_overrides
    from jasper.voice.daemon_main import _warn_if_research_model_unpriced

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        fired = _warn_if_research_model_unpriced(
            "gpt-realtime-3",
            pricing_overrides=load_pricing_overrides(),
        )

    assert fired is True
    assert any(
        "event=pricing.unpriced" in r.getMessage()
        and "surface=research" in r.getMessage()
        and "model=gpt-realtime-3" in r.getMessage()
        for r in caplog.records
    )


def test_priced_research_model_does_not_warn(caplog) -> None:
    # Contrast case: the shipped research default IS priced, so the warn path
    # must stay silent. Keeps the warn from becoming journal noise on the
    # common path and proves the guard is rate-driven, not always-on.
    import logging

    from jasper.research.providers import openai_research
    from jasper.usage import load_pricing_overrides
    from jasper.voice.daemon_main import _warn_if_research_model_unpriced

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        fired = _warn_if_research_model_unpriced(
            openai_research.DEFAULT_MODEL,
            pricing_overrides=load_pricing_overrides(),
        )

    assert fired is False
    assert not any(
        "event=pricing.unpriced" in r.getMessage() for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Per-turn latency timeline (`event=turn.timeline`, /state.voice.last_turn_ms)
# ---------------------------------------------------------------------------


def _arm_turn(wl, *, wake: bool) -> None:
    """Park `wl` mid-turn with a freshly anchored timeline.

    `wake` picks which anchor the turn gets, the same way `_begin_turn` does:
    the wake fire that opened it, or nothing — a turn no wake opened
    (push-to-talk, remote, research confirmation) anchors on itself.
    """
    import time

    from jasper.voice_daemon import State
    from tests._live_turn_fake import FakeLiveTurn

    wl._state = State.SESSION
    wl._turn = FakeLiveTurn()
    wl._session_id = 1
    wl._bg_tasks = set()
    wl._input_ended = False
    wl._silence_started_at = 0.0
    wl._turn_started_at_loop = asyncio.get_event_loop().time()
    wl._anchor_turn_timeline(time.monotonic() if wake else 0.0)


def _timeline_loop(*, wake: bool):
    """A WakeLoop parked mid-turn, configured for `wake`'s endpointer."""
    from jasper.voice_daemon import WakeLoop

    wl = WakeLoop.for_tests()
    wl._user_speech_seen = True
    wl._manual_endpoint_this_turn = not wake
    wl._server_vad_this_turn = False
    wl._barge_in_active = False
    _arm_turn(wl, wake=wake)
    return wl


async def test_wake_turn_timeline_carries_every_stage_in_order(caplog):
    """The ruler the rest of the loop is tuned against: one line per turn
    whose deltas all count from the wake fire, in the order the stages
    happen. Without the ordering pin a stage anchored on the wrong clock
    still logs a plausible-looking number."""
    import logging

    from tests._log_events import event_fields

    caplog.set_level(logging.INFO, logger="jasper.voice_daemon")
    wl = _timeline_loop(wake=True)

    # Real sleeps between stages so the ordering assertion below is a pin
    # and not six numbers that happen to round to the same millisecond.
    await wl._play_listening_chirp(going_on=True)
    await asyncio.sleep(0.002)
    await wl._send_session_audio(silent_frame())
    await asyncio.sleep(0.002)
    # Silent frame with speech already seen: starts the end-of-utterance
    # silence clock, which is the honest end-of-speech moment.
    await wl._handle_session_frame(silent_frame())
    await asyncio.sleep(0.002)
    await wl._end_session_input("test")
    await asyncio.sleep(0.002)
    await wl._record_response_started()
    await asyncio.sleep(0.002)
    await wl._record_first_write()
    await wl._end_turn("test")

    fields = event_fields(caplog, "turn.timeline")
    assert fields["anchor"] == "wake"
    assert fields["outcome"] == "complete"
    assert fields["endpointer"] == wl._endpointer_label()
    stages = [
        "cue_ms", "first_audio_to_provider_ms", "speech_end_ms",
        "end_input_ms", "first_response_ms", "first_write_ms", "total_ms",
    ]
    assert [key for key in stages if key in fields] == stages
    deltas = [int(fields[key]) for key in stages]
    assert deltas[0] >= 0
    assert deltas == sorted(deltas)


async def test_manual_turn_anchors_on_itself_and_omits_absent_stages(caplog):
    """A turn no wake opened must say what ms 0 is, and must leave out the
    stages that did not happen rather than reporting them as zero — a
    missing stage read as 0 ms is a wrong number, not a blank one."""
    import logging

    from tests._log_events import event_fields

    caplog.set_level(logging.INFO, logger="jasper.voice_daemon")
    wl = _timeline_loop(wake=False)

    await wl._send_session_audio(silent_frame())
    await wl._end_session_input("test")
    await wl._end_turn("test")

    fields = event_fields(caplog, "turn.timeline")
    assert fields["anchor"] == "manual"
    assert "speech_end_ms" not in fields
    assert "first_response_ms" not in fields
    assert int(fields["first_audio_to_provider_ms"]) >= 0
    assert int(fields["end_input_ms"]) >= 0


async def test_a_wake_that_opened_no_turn_does_not_anchor_a_later_one(caplog):
    """A wake can fire and never open a turn — late cancel, lost peer
    arbitration, spend cap, paused connection all return before
    `_begin_turn`. The stamp it leaves behind must not become the anchor of
    the next press, which would publish a multi-minute turn."""
    import logging
    import time

    from tests._log_events import event_fields

    caplog.set_level(logging.INFO, logger="jasper.voice_daemon")
    wl = _timeline_loop(wake=False)
    wl._wake_event_at_monotonic = time.monotonic() - 300.0

    wl._anchor_turn_timeline()
    await wl._end_session_input("test")
    await wl._end_turn("test")

    fields = event_fields(caplog, "turn.timeline")
    assert fields["anchor"] == "manual"
    assert int(fields["total_ms"]) < 1000


async def test_an_aborted_turn_is_journalled_but_not_published_as_the_ruler(
    caplog,
):
    """A turn can duck the music, chirp, and then die on the way into the
    session (acquire failure, peering notify, connection lost mid-open). It
    reached the household's ears, so it owes a journal line — an unrecorded
    turn reads as "the speaker did nothing" when the operator counts turns
    against `outcome`. It does NOT owe `/state.voice.last_turn_ms`, which is
    read as "how long a turn takes": a truncated ruler there is a wrong
    number, not a blank one. The aborted turn must also close its timeline,
    so the next one is not measured from the dead one's anchor."""
    import logging

    from tests._log_events import event_field_maps

    caplog.set_level(logging.INFO, logger="jasper.voice_daemon")
    wl = _timeline_loop(wake=True)
    await wl._play_listening_chirp(going_on=True)

    await wl._cleanup_after_failed_begin()

    (aborted,) = event_field_maps(caplog, "turn.timeline", outcome="aborted")
    assert aborted["anchor"] == "wake"
    assert int(aborted["cue_ms"]) >= 0
    assert wl._turn_anchor == 0.0
    assert wl.session_status()["last_turn_ms"] == {}

    # The next turn anchors fresh rather than inheriting the dead anchor.
    _arm_turn(wl, wake=False)
    await wl._end_session_input("test")
    await wl._end_turn("test")

    (served,) = event_field_maps(caplog, "turn.timeline", outcome="complete")
    assert int(served["total_ms"]) < 1000
    assert "cue_ms" not in served
    assert wl.session_status()["last_turn_ms"]["outcome"] == "complete"


async def test_push_to_talk_release_stamps_end_of_input(caplog):
    """The button release is an end-of-input like any other and must go
    through the one implementation every endpointer shares. When it did not,
    a whole class of turn had a hole where `end_input_ms` belongs."""
    import logging

    from tests._log_events import event_fields

    caplog.set_level(logging.INFO, logger="jasper.voice_daemon")
    wl = _timeline_loop(wake=False)

    assert await wl.manual_session_end() == "OK"
    await wl._end_turn("test")

    assert wl._input_ended is True
    assert wl._turn is None
    assert "end_input_ms" in event_fields(caplog, "turn.timeline")


async def test_session_status_publishes_the_last_turn_timeline():
    """`/state.voice.last_turn_ms` is the surface an operator reads without
    a journal; it stays empty until a turn has actually been served."""
    wl = _timeline_loop(wake=True)
    assert wl.session_status()["last_turn_ms"] == {}

    await wl._end_session_input("test")
    await wl._record_response_started()
    await wl._record_first_write()
    await wl._end_turn("test")

    last = wl.session_status()["last_turn_ms"]
    assert last["anchor"] == "wake"
    assert last["outcome"] == "complete"
    assert last["end_input_ms"] <= last["total_ms"]
    # The second half of the ten-turn ruler: the hand-off to fan-in is the
    # last moment this daemon can time, so the operator reads it here.
    assert last["first_response_ms"] <= last["first_write_ms"] <= last["total_ms"]


async def test_acquire_drain_stamps_first_audio_before_it_sends():
    """A sourced push-to-talk turn's first bytes reach the provider through
    the acquire drain, not `_send_session_audio`. Stamping on the way out
    would charge the whole drain — every buffered frame — to the provider."""
    wl = _timeline_loop(wake=False)
    stamped_when_sent: list[bool] = []

    async def _send_audio(_pcm) -> None:
        stamped_when_sent.append(
            "first_audio_to_provider" in wl._turn_timeline,
        )

    wl._turn.send_audio = _send_audio
    wl._acquire_buffer.extend([silent_frame(), silent_frame()])

    drained, _ = await wl._drain_acquire_audio()

    assert drained == 2
    assert stamped_when_sent[0] is True


async def test_server_vad_turn_carries_end_of_input(caplog):
    """Server VAD closes input inline rather than through
    `_end_session_input` — it must not send a second end_input — so the
    stamp has to sit beside each of those assignments or this whole class of
    turn reports a timeline with no `end_input_ms` in it."""
    import logging

    from tests._log_events import event_fields

    caplog.set_level(logging.INFO, logger="jasper.voice_daemon")
    wl = _timeline_loop(wake=True)
    wl._server_vad_this_turn = True
    wl._turn.server_speech_detected = lambda: True

    await wl._handle_session_frame(silent_frame())
    await wl._end_turn("test")

    assert wl._input_ended is True
    assert "end_input_ms" in event_fields(caplog, "turn.timeline")


async def test_background_connect_failure_ends_the_run() -> None:
    """Backgrounding the connect must not swallow it: a failure the
    supervisor cannot retry past still ends `run()` non-zero."""

    async def _connect() -> None:
        raise RuntimeError("connect failed")

    async def _serve() -> None:
        await asyncio.sleep(3600)

    with pytest.raises(RuntimeError):
        await _serve_while_connecting(_connect, _serve)


async def test_a_connect_failing_in_the_same_tick_as_the_serve_ends_the_run(
) -> None:
    """Both tasks can land in one tick — a stop that races the failure.
    The connect's exception must still reach `main()`; swallowing it
    exits 0, which `Restart=on-failure` does not restart."""

    async def _connect() -> None:
        raise RuntimeError("connect failed")

    async def _serve() -> None:
        return None

    with pytest.raises(RuntimeError):
        await _serve_while_connecting(_connect, _serve)


async def test_a_connect_that_returns_leaves_the_daemon_serving() -> None:
    """A connect handed over to the reconnect supervisor returns normally
    — the speaker keeps hearing and cues the outage, it does not exit."""
    stop = asyncio.Event()

    async def _connect() -> None:
        return None

    async def _serve() -> None:
        await stop.wait()

    task = asyncio.ensure_future(_serve_while_connecting(_connect, _serve))
    await asyncio.sleep(0.05)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, timeout=5.0)


async def test_stop_cancels_a_still_dialling_connect() -> None:
    """SIGTERM ends the wake loop; a connect still waiting on the WAN must
    not outlive it — the unit's TimeoutStopSec bounds the stop."""
    dialling = asyncio.Event()
    cancelled = asyncio.Event()
    stop = asyncio.Event()

    async def _connect() -> None:
        dialling.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def _serve() -> None:
        await stop.wait()

    task = asyncio.ensure_future(_serve_while_connecting(_connect, _serve))
    await asyncio.wait_for(dialling.wait(), timeout=5.0)
    stop.set()
    await asyncio.wait_for(task, timeout=5.0)
    assert cancelled.is_set()


# ---------------------------------------------------------------------------
# session_status — runtime-armed legs surfaced in /state (observability)
# ---------------------------------------------------------------------------


def _wake_loop_with_legs(*tokens):
    """A WakeLoop with exactly the given wake-input legs armed, in order."""
    from unittest.mock import MagicMock

    from jasper.voice_daemon import WakeLoop, _LegRuntime
    from jasper.wake_legs import by_token

    return WakeLoop.for_tests(legs=[
        _LegRuntime(by_token(token), MagicMock(), MagicMock(), None)
        for token in tokens
    ])


def test_session_status_reports_armed_legs_triple():
    """session_status surfaces the actually-armed leg tokens (runtime
    truth, in jasper.wake_legs order) so a startup leg-skip is visible in
    /state.voice — /aec only shows configured intent from aec_mode.env."""
    wl = _wake_loop_with_legs("on", "off", "dtln")
    _prep_session_status(wl)
    assert wl.session_status()["wake_legs"] == ["on", "off", "dtln"]


def test_session_status_reports_only_armed_legs_when_optional_absent():
    """Dual-stream (no DTLN leg) reports exactly the armed legs — the
    field reflects what the daemon opened, not what was configured."""
    wl = _wake_loop_with_legs("on", "off")
    _prep_session_status(wl)
    assert wl.session_status()["wake_legs"] == ["on", "off"]


def test_session_status_surfaces_tool_pack_outcomes():
    """session_status surfaces the per-pack tool-registration outcomes so a
    pack that silently failed to build (event=tool_pack.build_failed) is
    visible in /state.voice + jasper-doctor, not only the journal. The
    field is opaque passthrough — whatever outcomes_to_state produced."""
    wl = _wake_loop_with_legs("on")
    _prep_session_status(wl)
    packs = [
        {"name": "audio", "status": "registered", "tool_count": 5,
         "error": None},
        {"name": "spotify", "status": "failed", "tool_count": 0,
         "error": "ImportError('spotipy')"},
    ]
    wl._tool_packs = packs
    assert wl.session_status()["tool_packs"] == packs


def test_session_status_tool_packs_defaults_empty():
    """Built without the pack walk (the test seam / a caller that omits
    tool_packs), the field is an empty list, never missing."""
    wl = _wake_loop_with_legs("on")
    _prep_session_status(wl)
    assert wl.session_status()["tool_packs"] == []
