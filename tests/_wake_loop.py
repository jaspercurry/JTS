# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Build a fully-shaped `WakeLoop` without opening hardware.

The supported seam for unit tests that exercise individual `WakeLoop`
methods, so production code needs no defensive probes for
partially-initialised instances.
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from jasper.conversation_history import ConversationStore
from jasper.voice.catalog import InterruptReconcile
from jasper.voice.wake_detect import CAPTURE_RING_FRAMES, LegRuntime
from jasper.voice_daemon import WakeEventStore, WakeLoop
from jasper.wake_legs import by_token
from tests._playout import FakeTts

# Sentinel for `wake_loop_for_tests` constructor-time knobs, so a test can
# pass an explicit empty list (no legs) and have it mean "none" rather than
# "use the default".
_UNSET = object()


def wake_loop_for_tests(
    *,
    legs=_UNSET,
    manual_mics=None,
    tts=None,
    cfg=None,
    cues=None,
    ducker=None,
    connection=None,
    content_activity=None,
    usage_store=None,
    volume_coordinator=None,
    output_gate=None,
    vad=_UNSET,
    conversation_store: ConversationStore | None = None,
    wake_event_store: WakeEventStore | None = None,
    current_event_id: str | None = None,
) -> WakeLoop:
    """Inject collaborators at construction."""

    class _TestMic:
        async def frames(self):
            if False:
                yield None

    class _TestDetector:
        threshold = 0.5

        def score_frame(self, _frame) -> float:
            return 0.0

        def reset(self) -> None:
            return None

    class _TestConnection:
        def is_paused(self) -> bool:
            return False

        def last_failure_detail(self) -> str | None:
            return None

        def warm_session_until(self) -> float | None:
            return None

        def wake_cue(self) -> str:
            return "cant_connect"

        def request_reconnect_now(self) -> bool:
            return False

        async def acquire_turn(self):
            raise AssertionError("WakeLoop.for_tests acquire_turn stub used")

    class _TestDucker:
        is_ducked = False

        async def duck(self) -> None:
            return None

        async def restore(self) -> None:
            return None

    class _TestContentActivity:
        music_dbfs = None

        def music_is_playing(self) -> bool:
            return False

        def pause(self) -> None:
            return None

        def resume(self) -> None:
            return None

    class _TestUsageStore:
        write_degraded = False

        def open_session(self, *_args, **_kwargs) -> int:
            return 1

        def close_session(self, *_args, **_kwargs) -> float:
            return 0.0

    class _TestSpendCap:
        def allowed(self) -> bool:
            return True

    class _TestVolumeCoordinator:
        def get_listening_level(self) -> int:
            return 50

        def note_voice_session(self, *_args, **_kwargs) -> None:
            return None

        async def note_measurement_active(self, *_args, **_kwargs) -> None:
            return None

    class _TestVad:
        def predict(self, _frame) -> float:
            return 0.0

        def reset(self) -> None:
            return None

    cfg = cfg if cfg is not None else SimpleNamespace(
        active_voice_model="",
        duck_db=0.0,
        idle_timeout_sec=10.0,
        followup_timeout_sec=0.0,
        mic_device="udp:9876",
        mic_mute_state_path="/tmp/jasper-voice-daemon-test-mute.env",
        peering_enabled=False,
        peering_uds_socket="/tmp/jasper-peering-test.sock",
        response_stall_timeout_sec=120.0,
        vad_barge_in_threshold=0.5,
        voice_provider="test",
        wake_model="test_model",
    )
    mic = _TestMic()
    detector = _TestDetector()
    on_ring: deque = deque(maxlen=CAPTURE_RING_FRAMES)
    gate_factory = (
        patch("jasper.voice.assistant_output.AssistantOutputGate", return_value=output_gate)
        if output_gate is not None else nullcontext()
    )
    with gate_factory:
        self = WakeLoop(
            cfg=cfg,
            tts=FakeTts() if tts is None else tts,
            connection=_TestConnection() if connection is None else connection,
            ducker=_TestDucker() if ducker is None else ducker,
            cues=cues,
            content_activity=(
                _TestContentActivity() if content_activity is None else content_activity
            ),
            usage_store=_TestUsageStore() if usage_store is None else usage_store,
            spend_cap=_TestSpendCap(),
            stop_event=asyncio.Event(),
            volume_coordinator=(
                _TestVolumeCoordinator() if volume_coordinator is None else volume_coordinator
            ),
            legs=[
                LegRuntime(
                    by_token("on"),
                    mic,
                    detector,
                    on_ring,
                ),
            ] if legs is _UNSET else legs,
            manual_mics=manual_mics,
            vad=_TestVad() if vad is _UNSET else vad,
            conversation_store=conversation_store,
            wake_event_store=wake_event_store,
            initial_mic_muted=False,
            barge_in_reconcile=InterruptReconcile.NEEDS_CLIENT_TRUNCATE,
        )
    self._wake_telemetry._current_event_id = current_event_id
    return self
