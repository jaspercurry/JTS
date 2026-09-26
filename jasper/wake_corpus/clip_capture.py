# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One clip's multi-leg UDP capture for the wake-corpus recorder."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import AsyncExitStack
from typing import Any

import numpy as np

from jasper.aec_sweep import AEC3_SWEEP_SOURCE_XVF

from .bridge_session import build_capture_health
from .runtime_probe import read_bridge_stats_snapshot

logger = logging.getLogger("jasper-wake-corpus-web")


def compute_rms_dbfs(frame: np.ndarray) -> float:
    """Return the RMS of an int16 PCM frame in dBFS.

    -100.0 dBFS for near-silent or empty frames (avoids -inf from
    log(0)). 0.0 dBFS = full-scale int16. Used by the SSE level-meter
    endpoint so the UI can show a live "is your voice reaching the
    mic?" bar while recording.
    """
    if len(frame) == 0:
        return -100.0
    mean_sq = float(np.mean(frame.astype(np.float64) ** 2))
    if mean_sq < 1.0:
        return -100.0
    rms = mean_sq ** 0.5
    return 20.0 * float(np.log10(rms / 32768.0))


class RecordingTask:
    """Open-ended audio recording from multiple UDP captures.

    Constructed on each Start click; cancelled on Stop click. Background
    asyncio task streams frames into per-leg buffers. `stop()` cancels
    cleanly + returns the captured PCM bytes per leg.

    Side effect: while recording, updates `current_rms_dbfs` on every
    AEC-ON frame so the SSE level meter can read it. Only the AEC ON
    leg is metered (it's the canonical wake-detection signal); cost
    is one numpy reduction per ~80 ms.

    Memory bound: at 16 kHz mono int16 ≈ 32 KB/s per leg × 3 legs ≈
    96 KB/s. Capped to MAX_RECORDING_DURATION_SEC by the backend, so
    worst-case footprint is bounded.
    """

    def __init__(
        self,
        ports: dict[str, int],
        *,
        aec3_sweep_source: str = AEC3_SWEEP_SOURCE_XVF,
    ) -> None:
        self._ports = ports
        self._aec3_sweep_source = aec3_sweep_source
        self._buffers: dict[str, list[np.ndarray]] = {leg: [] for leg in ports}
        self._captures: dict[str, Any] = {}
        self._task: asyncio.Task | None = None
        self._stack: AsyncExitStack | None = None
        self._start_monotonic: float = 0.0
        self._bridge_stats_start: dict[str, Any] | None = None
        self._bridge_stats_stop: dict[str, Any] | None = None
        # Live RMS of the most recent AEC ON frame, read by the SSE
        # level-meter handler. Written from the asyncio loop thread,
        # read from HTTP handler threads — single-float reads/writes
        # are atomic in CPython so no lock needed.
        self.current_rms_dbfs: float = -100.0

    async def start(self) -> None:
        from jasper.mic_capture import UdpMicCapture  # lazy: test seam — tests/wake_corpus_setup_fixtures.py patches jasper.mic_capture.UdpMicCapture at call time

        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        try:
            for leg, port in self._ports.items():
                cap = await self._stack.enter_async_context(
                    UdpMicCapture(port=port),
                )
                self._captures[leg] = cap
        except Exception:  # noqa: BLE001
            # If any leg fails to bind, clean up the ones that succeeded
            # so the user can retry without a "port already in use"
            # cascade on the next start.
            await self._stack.__aexit__(None, None, None)
            raise

        self._start_monotonic = time.monotonic()
        self._bridge_stats_start = read_bridge_stats_snapshot()
        self._task = asyncio.create_task(self._collect_all())

    async def _collect_all(self) -> None:
        async def _per_leg(leg: str, cap: Any) -> None:
            is_aec_on = (leg == "on")
            async for frame in cap.frames():
                self._buffers[leg].append(frame)
                # Live-meter the AEC ON leg only — it's the canonical
                # wake-detection signal. Single-float atomic write; no
                # lock needed (CPython guarantee).
                if is_aec_on:
                    self.current_rms_dbfs = compute_rms_dbfs(frame)

        await asyncio.gather(*[
            _per_leg(leg, cap) for leg, cap in self._captures.items()
        ])

    def elapsed_sec(self) -> float:
        if self._start_monotonic == 0:
            return 0.0
        return time.monotonic() - self._start_monotonic

    def request_stop(self) -> None:
        """Cancel frame collection without waiting for clip publication.

        Called on this task's event loop when a privacy/automatic stop races a
        lifecycle owner. ``stop()`` later gathers the buffered PCM and closes
        the capture stack; cancellation here ensures no more audio is retained
        while that bounded-backoff publication retry is pending.
        """
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def stop(self) -> dict[str, bytes]:
        """Cancel the collection task, return PCM bytes per leg.

        Idempotent: calling twice is a no-op on the second call (the
        task + stack sentinels are cleared after first cleanup, so we
        skip both double-await and double-exit which AsyncExitStack
        would error on).
        """
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001
                logger.warning("recording task raised on cancel: %s", e)
        self._task = None

        result: dict[str, bytes] = {}
        for leg, frames in self._buffers.items():
            if frames:
                pcm = np.concatenate(frames).astype(np.int16).tobytes()
            else:
                pcm = b""
            result[leg] = pcm

        if self._stack is not None:
            try:
                await self._stack.__aexit__(None, None, None)
            except Exception as e:  # noqa: BLE001
                logger.warning("cleanup raised: %s", e)
            self._stack = None
        self._bridge_stats_stop = read_bridge_stats_snapshot()
        return result

    def capture_health(self, wall_duration_sec: float) -> dict[str, Any]:
        return build_capture_health(
            wall_duration_sec=wall_duration_sec,
            buffers=self._buffers,
            bridge_start=self._bridge_stats_start,
            bridge_stop=self._bridge_stats_stop,
            aec3_sweep_source=self._aec3_sweep_source,
        )
