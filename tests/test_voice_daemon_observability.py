# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import asyncio
from types import SimpleNamespace

import pytest

from jasper.tts_routing import FANIN_TTS_SOCKET
from jasper.voice.daemon_main import _serve_while_connecting, _tts_ready_detail


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
    not outlive it — the unit's TimeoutStopSec is 5 s."""
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
