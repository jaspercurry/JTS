# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""`jasper.voice.daemon_main.run()` tears every resource down in reverse.

One `contextlib.AsyncExitStack` owns the whole daemon lifetime: each
resource registers its release at the site that creates (or starts) it,
so the unwind is the exact reverse of construction. That is also
dependency order — the coordinator outlives its observer, camilla
outlives everything built on it — which is why the reversal itself is
the thing worth pinning rather than a hand-written order.

Also pins ADR-0239: the shutdown mic-loss cue is spoken through the
daemon's own playout BEFORE the stack unwinds the playout and the mics.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from types import SimpleNamespace

import pytest

from jasper import wake_legs
from jasper.voice import daemon_main

TEST_TIMEOUT_SEC = 20.0


class _Trace(list):
    """Ordered ``(name, event)`` log shared by every fake resource."""

    def entered(self) -> list[str]:
        return [n for n, ev in self if ev == "enter"]

    def exited(self) -> list[str]:
        return [n for n, ev in self if ev == "exit"]

    def index_of(self, name: str, event: str) -> int:
        return self.index((name, event))


def _resource(
    trace: _Trace,
    name: str,
    teardown: str,
    *,
    is_async: bool,
    enter: bool = True,
    **attrs,
) -> SimpleNamespace:
    """A stand-in resource that logs its entry now and its teardown later."""
    if enter:
        trace.append((name, "enter"))
    obj = SimpleNamespace(**attrs)

    def _sync(*_a, **_kw) -> None:
        trace.append((name, "exit"))

    async def _async(*_a, **_kw) -> None:
        trace.append((name, "exit"))

    setattr(obj, teardown, _async if is_async else _sync)
    return obj


@contextlib.asynccontextmanager
async def _traced_cm(trace: _Trace, name: str, **attrs):
    trace.append((name, "enter"))
    try:
        yield SimpleNamespace(**attrs)
    finally:
        trace.append((name, "exit"))


class _SpyCues:
    """Stand-in cue manager: no synthesis, no playout."""

    def __init__(self) -> None:
        self.played: list[str] = []

    def attach_tts(self, _tts) -> None:
        return None

    async def prerender_text(self, _text: str) -> bool:
        return True

    async def play(self, slug: str) -> bool:
        self.played.append(slug)
        return True


class _FakeWakeLoop:
    """Only the surface `run()` wires up. Its teardown is the
    conversation-store close, which is what run() registers for it."""

    def __init__(self, trace: _Trace, *_a, **_kw) -> None:
        self._trace = trace
        trace.append(("wake_loop", "enter"))
        self.record_tool_dispatch_stage = lambda *a, **k: None
        self.play_supervisor_cue = lambda *a, **k: None
        self.record_research_delivery = lambda *a, **k: None
        self.announce_timer = lambda *a, **k: None
        self.announce_research_ready = lambda *a, **k: None

    def set_research_scheduler(self, *_a, **_kw) -> None:
        return None

    async def run(self) -> None:
        return None

    def close_conversation_store(self) -> None:
        self._trace.append(("wake_loop", "exit"))


class _FakeRegistry:
    """Records the dispatch-observer wiring as an enter/exit pair."""

    def __init__(self, trace: _Trace) -> None:
        self._trace = trace
        self.pack_outcomes = ()

    def apply_prompt_overrides(self, _overrides) -> None:
        return None

    def set_dispatch_observer(self, observer) -> None:
        self._trace.append(
            ("dispatch_observer", "enter" if observer is not None else "exit")
        )


@pytest.fixture
def teardown_trace(monkeypatch, tmp_path) -> _Trace:
    """Replace every resource `run()` builds with a tracing stand-in."""
    trace = _Trace()

    for key in list(os.environ):
        if key.startswith("JASPER_") or key.endswith("_API_KEY"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaTest")

    def patch(name, value) -> None:
        monkeypatch.setattr(daemon_main, name, value)

    # --- process-wide side effects we do not want in a unit test ---
    patch("configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(daemon_main.flight_recorder, "install", lambda _s: None)
    patch("load_pricing_overrides", lambda *a, **k: {})
    patch("household_usage_reader", lambda *a, **k: (lambda: 0.0))
    patch("UsageStore", lambda *a, **k: SimpleNamespace())
    patch("_wire_billable_activity_meter", lambda **k: None)
    patch("_warn_if_research_model_unpriced", lambda *a, **k: None)
    patch("install_volume_owner", lambda *a, **k: None)
    patch("set_canonical_target_db_provider", lambda *a, **k: None)
    patch("_wake_ready_detail", lambda *a, **k: "test")
    patch("_tts_ready_detail", lambda *a, **k: "test")
    patch("WakeWordDetector", lambda *a, **k: SimpleNamespace())
    patch("build_ducker", lambda *a, **k: SimpleNamespace())
    patch("RendererClient", lambda *a, **k: SimpleNamespace())
    patch("VolumePersistence", lambda *a, **k: SimpleNamespace())
    patch("_build_router", lambda _cfg: None)
    patch("build_google_clients", lambda _cfg: None)
    patch("build_google_routes_client", lambda _env: None)
    patch("ConversationStore", lambda *a, **k: SimpleNamespace())
    patch("read_conversation_settings", lambda: SimpleNamespace(
        capture_enabled=True, db_path=str(tmp_path / "conv.db"),
    ))
    monkeypatch.setattr(
        "jasper.tool_state.read_tool_state",
        lambda *a, **k: SimpleNamespace(disabled_tools=set(), disabled_packs=set()),
    )
    monkeypatch.setattr(
        "jasper.tool_prompt_overrides.read_prompt_overrides", lambda *a, **k: {},
    )
    monkeypatch.setattr("jasper.tools.catalog.write_catalog", lambda *a, **k: None)
    monkeypatch.setattr(
        "jasper.assistant_volume.volume_context_publisher_for_runtime",
        lambda _env: None,
    )

    # --- traced resources, in construction order ---
    patch("CamillaController", lambda *a, **k: _resource(
        trace, "camilla", "close", is_async=True,
    ))
    patch("WeatherClient", lambda *a, **k: _resource(
        trace, "weather", "aclose", is_async=True,
    ))
    monkeypatch.setattr(daemon_main.transit, "enabled_pack_ids", lambda _e: [])
    monkeypatch.setattr(daemon_main.transit, "active_transit", lambda _e: _resource(
        trace, "transit", "aclose", is_async=True, tools=[], configured=False,
    ))
    patch("build_ha_client", lambda _cfg: _resource(
        trace, "ha", "aclose", is_async=True, url="http://ha", agent_id="",
    ))

    async def _initialize(**_kw):
        return (42, "test")

    patch("VolumeCoordinator", lambda **k: _resource(
        trace, "volume_coordinator", "aclose", is_async=True,
        volume_owner=SimpleNamespace(),
        get_camilla_target_db=lambda: 0.0,
        initialize=_initialize,
    ))

    def _volume_observer(*_a, **_kw):
        obs = _resource(trace, "volume_observer", "stop", is_async=True,
                        enter=False)

        async def _start() -> None:
            trace.append(("volume_observer", "enter"))
        obs.start = _start
        return obs

    patch("VolumeObserver", _volume_observer)

    def _timer_scheduler(**_kw):
        sched = _resource(trace, "timer_scheduler", "stop", is_async=True,
                          enter=False,
                          set_pre_render=lambda _f: None,
                          set_on_fire=lambda _f: None)

        async def _start() -> None:
            trace.append(("timer_scheduler", "enter"))
        sched.start = _start
        return sched

    patch("TimerScheduler", _timer_scheduler)
    patch("active_research_provider", lambda _env: _resource(
        trace, "active_research", "aclose", is_async=True,
        client=SimpleNamespace(model="research-model"), provider_id="test",
    ))

    def _research_scheduler(*_a, **_kw):
        # The SQLite store opens in __init__ and closes on close(); the
        # task lifecycle is a second, inner resource around start/stop.
        sched = _resource(trace, "research_store", "close", is_async=False,
                          set_on_done=lambda _f: None)

        async def _start() -> None:
            trace.append(("research_scheduler", "enter"))

        async def _stop() -> None:
            trace.append(("research_scheduler", "exit"))
        sched.start = _start
        sched.stop = _stop
        return sched

    patch("ResearchScheduler", _research_scheduler)
    patch("_build_cues_manager", lambda *a, **k: _SpyCues())

    def _wake_event_store(*_a, **_kw):
        store = _resource(trace, "wake_events", "close", is_async=False,
                          enter=False)
        store.open = lambda: trace.append(("wake_events", "enter"))
        return store

    patch("WakeEventStore", _wake_event_store)
    patch("_build_registry", lambda *a, **k: _FakeRegistry(trace))
    patch("outcomes_to_state", lambda _o: {})
    patch("_make_connection", lambda *a, **k: _resource(
        trace, "connection", "stop", is_async=True,
        start=lambda *a, **k: None,
        set_failure_escalation_cb=lambda _cb: None,
    ))

    # --- resources opened inside the stack ---
    patch("_configured_wake_legs", lambda *a, **k: [
        (wake_legs.by_token("on"), "udp:9876"),
    ])
    patch("make_mic_capture", lambda device, **k: _traced_cm(trace, "mic"))
    patch("TtsPlayout", lambda **k: _traced_cm(trace, "tts"))

    def _content_activity(*_a, **_kw):
        tracker = _resource(trace, "content_activity", "stop", is_async=True,
                            enter=False)

        async def _start() -> None:
            trace.append(("content_activity", "enter"))
        tracker.start = _start
        return tracker

    patch("ContentActivityTracker", _content_activity)

    def _heartbeat(**_kw):
        hb = _resource(trace, "heartbeat", "stop", is_async=False, enter=False)
        hb.start = lambda: trace.append(("heartbeat", "enter"))
        return hb

    patch("Heartbeat", _heartbeat)
    patch("_schedule_cue_regen", lambda *a, **k: trace.append(
        ("startup_tasks", "enter"),
    ))
    patch("_schedule_assistant_loudness_seed", lambda *a, **k: None)

    async def _cancel_tracked_tasks(_tasks) -> None:
        trace.append(("startup_tasks", "exit"))

    patch("_cancel_tracked_tasks", _cancel_tracked_tasks)
    patch("WakeLoop", lambda *a, **k: _FakeWakeLoop(trace, *a, **k))

    async def _start_control_socket(*_a, **_kw):
        sock = _resource(trace, "control_socket", "close", is_async=False)

        async def _wait_closed() -> None:
            return None
        sock.wait_closed = _wait_closed
        return sock

    patch("_start_control_socket", _start_control_socket)

    async def _serve_while_connecting(_connect, _serve) -> None:
        return None

    patch("_serve_while_connecting", _serve_while_connecting)

    async def _announce_mic_loss_at_shutdown(_wake_loop) -> str:
        trace.append(("shutdown_cue", "run"))
        return ""

    patch("_announce_mic_loss_at_shutdown", _announce_mic_loss_at_shutdown)
    return trace


async def _run_daemon_once(trace: _Trace) -> None:
    loop = asyncio.get_running_loop()
    real_remove = loop.remove_signal_handler

    def _add(sig, _cb) -> None:
        trace.append((f"signal:{sig.name}", "enter"))

    def _remove(sig) -> bool:
        trace.append((f"signal:{sig.name}", "exit"))
        return real_remove(sig)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(loop, "add_signal_handler", _add)
        mp.setattr(loop, "remove_signal_handler", _remove)
        async with asyncio.timeout(TEST_TIMEOUT_SEC):
            await daemon_main.run()


async def test_every_resource_exits_in_reverse_of_entry(teardown_trace) -> None:
    """The whole daemon lifetime is one stack, so teardown is a mirror.

    A resource that stops being torn down (or is torn down out of order
    against the sibling it depends on) breaks this and nothing else.
    """
    await _run_daemon_once(teardown_trace)

    entered = teardown_trace.entered()
    exited = teardown_trace.exited()
    assert entered, "the harness recorded no resources at all"
    assert exited == list(reversed(entered))


async def test_shutdown_cue_runs_before_the_playout_and_mics_close(
    teardown_trace,
) -> None:
    """ADR-0239: the mic-loss cue is spoken through the daemon's own
    playout, so it must land before the stack unwinds tts and the mics."""
    await _run_daemon_once(teardown_trace)

    cue_at = teardown_trace.index_of("shutdown_cue", "run")
    assert cue_at < teardown_trace.index_of("tts", "exit")
    assert cue_at < teardown_trace.index_of("mic", "exit")


async def test_control_socket_closes_before_the_wake_loop_it_dispatches_into(
    teardown_trace,
) -> None:
    """The socket hands commands to the wake loop, so it must stop
    accepting them before that loop's own teardown starts."""
    await _run_daemon_once(teardown_trace)

    assert (
        teardown_trace.index_of("control_socket", "exit")
        < teardown_trace.index_of("wake_loop", "exit")
    )


async def test_schedulers_stop_before_the_playout_they_announce_through(
    teardown_trace,
) -> None:
    """Timer / research announcements speak through the TtsPlayout, so
    their tasks are cancelled before it closes underneath them."""
    await _run_daemon_once(teardown_trace)

    tts_at = teardown_trace.index_of("tts", "exit")
    assert teardown_trace.index_of("timer_scheduler", "exit") < tts_at
    assert teardown_trace.index_of("research_scheduler", "exit") < tts_at
    assert teardown_trace.index_of("startup_tasks", "exit") < tts_at
