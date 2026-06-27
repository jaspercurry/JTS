# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free guards for secure active-speaker web measurement orchestration."""

from __future__ import annotations

import asyncio
import time

import jasper.active_speaker.playback as active_playback
import jasper.correction.playback as correction_playback
from jasper.active_speaker import web_commissioning as web


def test_driver_capture_sweep_requires_confirmed_driver(monkeypatch):
    monkeypatch.setattr(web, "load_output_topology", lambda: object())
    monkeypatch.setattr(web, "load_measurement_state", lambda topology: {})

    payload = asyncio.run(
        web.play_driver_capture_sweep(
            {"speaker_group_id": "mono", "role": "woofer"},
            camilla_factory=lambda: object(),
        )
    )

    assert payload["status"] == "refused"
    assert payload["reason"] == "driver_floor_confirmation_required"


def test_driver_capture_sweep_refuses_expired_floor_confirmation(monkeypatch):
    measurements = {
        "summary": {
            "latest_driver_measurements": {
                "mono:woofer": {
                    "captured": True,
                    "playback_id": "play-woofer",
                    "test_level_dbfs": -72.0,
                    "issues": [],
                },
            },
        },
    }
    monkeypatch.setattr(web, "load_output_topology", lambda: object())
    monkeypatch.setattr(web, "load_measurement_state", lambda topology: measurements)
    monkeypatch.setattr(web, "load_safe_playback_state", lambda: {"status": "idle"})
    monkeypatch.setattr(
        web,
        "_load_driver_commissioning_config_for_level",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not load")),
    )

    payload = asyncio.run(
        web.play_driver_capture_sweep(
            {"speaker_group_id": "mono", "role": "woofer"},
            camilla_factory=lambda: object(),
        )
    )

    assert payload["status"] == "refused"
    assert payload["reason"] == "driver_floor_confirmation_expired"


def test_summed_capture_sweep_arms_safe_session_for_mutual_exclusion(monkeypatch):
    armed = {}
    measurements = {
        "summary": {
            "latest_summed_tests": {
                "mono": {
                    "captured": True,
                    "audio_emitted": True,
                    "summed_test_id": "sum-1",
                    "tone": {"level_dbfs": -72.0},
                    "issues": [],
                },
            },
        },
    }
    monkeypatch.setattr(web, "load_output_topology", lambda: object())
    monkeypatch.setattr(web, "load_measurement_state", lambda topology: measurements)
    monkeypatch.setattr(web, "load_safe_playback_state", lambda: {"status": "idle"})
    monkeypatch.setattr(
        web,
        "arm_safe_playback_session",
        lambda report: armed.setdefault("report", report) or {"status": "armed"},
    )
    monkeypatch.setattr(web, "resolve_commission_inputs", lambda: (object(), None))

    async def fake_load(**kwargs):
        return {
            "load": {
                "status": "blocked",
                "issues": [{
                    "severity": "blocker",
                    "code": "test_block",
                    "message": "blocked in test",
                }],
            },
        }

    monkeypatch.setattr(web, "_load_summed_commissioning_config", fake_load)

    payload = asyncio.run(
        web.play_summed_capture_sweep(
            {"speaker_group_id": "mono"},
            camilla_factory=lambda: object(),
        )
    )

    assert armed["report"]["status"] == "ready"
    assert payload["status"] == "blocked"
    assert payload["reason"] == "summed_capture_sweep_load_failed"


def test_summed_test_playback_does_not_block_the_correction_loop(monkeypatch):
    """C4a-6: the summed-test stimulus must play OFF the shared correction loop.

    The crossover summed test previously ran ``aplay`` via a synchronous
    ``subprocess.run`` directly on the single background correction loop
    (``jasper-correction-loop``), stalling every other correction/commissioning
    request — status polls, SSE progress, the safe-playback TTL deadman — for
    the whole stimulus duration.

    This pins the fix behaviourally: while playback is "in flight", a concurrent
    coroutine scheduled on the same loop must keep making progress. We stand in
    for the real ``aplay`` two ways at once: the off-loop primitive
    (``play_sweep``) yields via ``await asyncio.sleep``, while the old blocking
    primitive (``subprocess.run``) would ``time.sleep`` and freeze the loop
    thread. Reverting to ``subprocess.run`` makes the ticker starve and the
    assertion fail (mutation check).
    """

    playback_seconds = 0.30

    async def _fake_play_sweep(wav_path, *, alsa_device, timeout_s):
        # Off-loop: yields control so the loop can run other coroutines.
        await asyncio.sleep(playback_seconds)

    class _CompletedProc:
        returncode = 0
        stderr = ""

    class _BlockingRun:
        """Stand-in for the removed blocking ``subprocess.run`` path.

        If the code under test ever calls ``subprocess.run`` again it freezes
        the loop thread for the playback duration — exactly the bug. It returns
        a clean completed-process so the regression manifests as loop starvation
        (the ``ticks`` assertion below), not as an exception.
        """

        def __call__(self, *args, **kwargs):
            time.sleep(playback_seconds)
            return _CompletedProc()

    monkeypatch.setattr(correction_playback, "play_sweep", _fake_play_sweep)
    monkeypatch.setattr(web.subprocess, "run", _BlockingRun())

    # ``start_tone_playback`` is lazily imported inside the function, so patch
    # it on its source module.
    monkeypatch.setattr(
        active_playback,
        "start_tone_playback",
        lambda *a, **k: {"status": "completed", "tone": {"level_dbfs": -72.0}},
    )
    monkeypatch.setattr(
        web,
        "_combined_speech_stimulus_wav_path",
        lambda: ("/tmp/jts-fake-summed-stimulus.wav", {"duration_s": playback_seconds}),
    )

    async def _fake_load(**kwargs):
        return {"load": {"status": "loaded"}}

    async def _fake_rollback(**kwargs):
        return {"status": "rolled_back"}

    monkeypatch.setattr(web, "_load_summed_commissioning_config", _fake_load)
    monkeypatch.setattr(web, "_rollback_summed_commissioning_config", _fake_rollback)
    monkeypatch.setattr(web, "_commission_tone_select_fanin_lane", lambda: {"status": "ok"})
    monkeypatch.setattr(
        web,
        "_commission_tone_release_fanin_lane",
        lambda *, reason: {"status": "ok", "reason": reason},
    )

    async def _scenario():
        ticks = 0

        async def _ticker():
            nonlocal ticks
            # Tick frequently relative to the playback window. A responsive loop
            # accumulates many ticks during the ~0.30 s "playback".
            while True:
                ticks += 1
                await asyncio.sleep(0.01)

        ticker = asyncio.create_task(_ticker())
        playback = await web._play_summed_commission_tone(
            {},
            safe_session={"status": "armed"},
            topology=object(),
            speaker_group_id="mono",
            startup_gate_calibration_level=None,
            preset=object(),
            crossover_preview=None,
            camilla_factory=lambda: object(),
        )
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass
        return playback, ticks

    playback, ticks = asyncio.run(_scenario())

    # Playback completed through the off-loop primitive...
    assert playback["status"] == "completed"
    assert playback["backend"] == web.SUMMED_COMMISSION_SPEECH_BACKEND
    assert playback["audio_emitted"] is True
    # ...and the loop stayed responsive: many ticks landed during playback.
    # A blocked loop would yield ~0-1 ticks; require clearly more.
    assert ticks >= 5, f"correction loop appears blocked during playback (ticks={ticks})"


def test_summed_test_playback_dispatches_off_loop_primitive():
    """Structural mutation guard: no synchronous ``subprocess.run`` on the loop.

    Complements the behavioural test. ``_play_summed_commission_tone`` must
    dispatch the stimulus through the async off-loop primitive (``play_sweep``,
    which uses ``asyncio.create_subprocess_exec``) and must not reintroduce a
    blocking ``subprocess.run`` / ``subprocess.call`` / ``subprocess.Popen(...).wait``
    in the playback path.
    """

    src = web.__loader__.get_source(web.__name__)
    assert src is not None
    func_start = src.index("async def _play_summed_commission_tone(")
    func_end = src.index("async def start_summed_test(", func_start)
    body = src[func_start:func_end]

    assert "await play_sweep(" in body, (
        "_play_summed_commission_tone must await play_sweep (off-loop aplay)"
    )
    assert "subprocess.run(" not in body, (
        "_play_summed_commission_tone reintroduced a blocking subprocess.run "
        "on the correction loop (C4a-6 regression)"
    )
