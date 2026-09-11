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

# Sentinel for `wake_loop_for_tests` constructor-time knobs, so a test can
# pass an explicit empty list (no legs) and have it mean "none" rather than
# "use the default".
_UNSET = object()


class FakeTts:
    def set_emission_admission(self, _admission) -> None:
        return None

    async def write_segment(self, *_args, on_first_write=None, **_kwargs) -> bool:
        if on_first_write is not None:
            await on_first_write()
        return True

    async def resume_content_meter(self) -> None:
        return None

    async def pause_content_meter(self) -> None:
        return None

    async def pause_content_meter_for_measurement(
        self, deadline_monotonic: float,
    ) -> None:
        return None

    async def prepare_assistant_context(self, **_kwargs) -> None:
        return None

    async def end_segment(self) -> None:
        return None

    async def wait_drained(self) -> None:
        return None

    async def flush(self):
        return None

    def expected_drain_at(self) -> float:
        return 0.0

    def take_paced_sec(self) -> float:
        return 0.0


def wake_loop_for_tests(
    *,
    legs=_UNSET,
    manual_mics=None,
    tts=None,
    cfg=None,
    cues=None,
    ducker=None,
    volume_coordinator=None,
    output_gate=None,
    vad=_UNSET,
    conversation_store: ConversationStore | None = None,
    wake_event_store: WakeEventStore | None = None,
    current_event_id: str | None = None,
    **overrides,
) -> WakeLoop:
    """Inject collaborators at construction; overrides seed local turn state."""

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
            connection=_TestConnection(),
            ducker=_TestDucker() if ducker is None else ducker,
            cues=cues,
            content_activity=_TestContentActivity(),
            usage_store=_TestUsageStore(),
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
    for key, value in overrides.items():
        setattr(self, key if key.startswith("_") else f"_{key}", value)
    return self
