# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The AEC bridge's near-end capture threads.

The XVF3800 chip stream and the optional cheap USB corpus mic are opened
here, and the 16 kHz mono int16 frames they yield are published onto the
queues `_aec_loop` drains. Capture geometry, the resampler choice the USB
card's rate forces, and the queue-drop accounting for both cards sit behind
this one surface.

Imports run one way only: nothing here reads `jasper.cli.aec_bridge`. The
process-wide `_BridgeStats` these threads count into arrives as an argument,
the way the reference transport takes its own, and the shutdown signal and
the device settings arrive from the caller that owns them.
"""
from __future__ import annotations

import math
from queue import Full, Queue
import threading
import time
from typing import Any, Optional

import numpy as np
import sounddevice as sd

from jasper.dsp_numpy import resample_poly
from jasper.log_event import log_event
from jasper.cli.aec_bridge_engines import FRAME_SAMPLES, SAMPLE_RATE
from jasper.cli.aec_bridge_telemetry import (
    DropLogDebouncer,
    _BridgeStats,
    logger,
)
from ..mics import xvf3800 as _mic_profile

# Above this, `jasper.dsp_numpy.resample_poly` runs one Python-level polyphase
# branch per unit of `max(up, down)` and stops fitting a capture callback: a
# 44.1 kHz card reduces to 160/441 and costs ~2 ms per block on a laptop
# against a 20 ms budget, where scipy's C `upfirdn` costs 0.7 ms. Every
# integer-ratio card (32/48/96 kHz -> 1/2, 1/3, 1/6) stays well under 8 and is
# 3-5x *faster* in numpy, so only the 44.1 kHz family reaches for scipy.
MAX_NUMPY_POLYPHASE_BRANCHES = 8

# Capture geometry for the mic; the profile pins the near-end lane for the
# default WebRTC AEC3 path (channels 2-5 are raw mics 0-3, no chip DSP).
# The device name is a PortAudio substring match — NOT an ALSA pcm string:
# PortAudio enumerates ALSA cards by card description, not `hw:CARD=` syntax.
# The default matches "Array: USB Audio (hw:N,0)" on the legacy square
# firmware and "L16K6Ch: USB Audio (hw:N,0)" on the Flex linear firmware.
MIC_CHANNELS = _mic_profile.RECOMMENDED_FIRMWARE.capture_channels
MIC_CHANNEL_INDEX = _mic_profile.MIC_CHANNEL_INDEX


def _usb_capture_rate(*, usb_mic_device: str, usb_mic_rate: int) -> int:
    """Return the USB mic capture rate PortAudio can actually open."""
    if usb_mic_rate > 0:
        return usb_mic_rate
    info = sd.query_devices(usb_mic_device, "input")
    rate = int(round(float(info.get("default_samplerate") or SAMPLE_RATE)))
    return rate if rate > 0 else SAMPLE_RATE


def mic_thread(
    mic_q: Queue,
    raw0_q: Optional[Queue] = None,
    chip_aec_qs: Optional[dict[str, Queue]] = None,
    chip_beam_plan: _mic_profile.ChipBeamPlan | None = None,
    *,
    mic_device: str,
    capture_latency: str,
    stats: _BridgeStats,
    shutdown: threading.Event,
) -> None:
    """Capture 16 kHz 6-ch from the XVF chip, pluck channel
    MIC_CHANNEL_INDEX and push mono int16 frames into `mic_q`.

    In chip-AEC mode `SHF_BYPASS=0` with `OP_L/R=[7,0]/[7,1]` makes channels
    0/1 the fixed 150°/210° ASR beams; in default production `SHF_BYPASS=1`
    makes them raw-ish.

    When `raw0_q` is provided, channel 2 (raw mic 0, no chip DSP) is ALSO
    extracted onto that queue for the OUT_PORT_RAW0 leg. Independent queue
    and extraction so a backlog on one cannot stall the other.
    """
    mic_drop_log = DropLogDebouncer()

    def cb(indata, frames, time_info, status):
        if status:
            logger.debug("mic status: %s", status)
        if shutdown.is_set():
            return
        mono = indata[:, MIC_CHANNEL_INDEX].astype(np.int16, copy=True)
        try:
            mic_q.put_nowait(mono.tobytes())
        except Full:
            stats.inc_nested("queue_drops", "mic")
            if outcome := mic_drop_log.record(time.monotonic()):
                drops, window_sec = outcome
                logger.warning(
                    "mic queue full, dropped %d frames in last %.1fs",
                    drops, window_sec,
                )
        if raw0_q is not None:
            # Channel 2 = raw mic 0 ADC output, bypassing the chip's
            # BF/NS/AGC/HPF. `copy=True` so the slice does not share backing
            # storage with `indata`, which sounddevice reuses.
            raw0 = indata[:, 2].astype(np.int16, copy=True)
            try:
                raw0_q.put_nowait(raw0.tobytes())
            except Full:
                # Observational leg: drop quietly rather than flood the
                # journal during a stall the mic_q path already reports.
                stats.inc_nested("queue_drops", "raw0")
                pass
        if chip_aec_qs and chip_beam_plan:
            for beam in chip_beam_plan.legs:
                q = chip_aec_qs.get(beam.token)
                if q is None:
                    continue
                pcm = indata[:, beam.channel_index].astype(np.int16, copy=True)
                try:
                    q.put_nowait(pcm.tobytes())
                except Full:
                    stats.inc_nested("queue_drops", "chip")
                    pass

    input_stream_kwargs = dict(
        device=mic_device, samplerate=SAMPLE_RATE, channels=MIC_CHANNELS,
        dtype="int16", blocksize=FRAME_SAMPLES, callback=cb,
    )
    if capture_latency:
        input_stream_kwargs["latency"] = (
            "low"
            if capture_latency == "low"
            else float(capture_latency)
        )
    with sd.InputStream(**input_stream_kwargs) as stream:
        log_event(
            logger,
            "aec.mic_stream_latency",
            latency_s=stream.latency,
            requested_latency=capture_latency or "default",
            samplerate=int(stream.samplerate),
            blocksize=int(stream.blocksize),
        )
        stats.set_capture_stream(
            sample_rate_hz=int(stream.samplerate),
            block_frames=int(stream.blocksize),
            input_latency_seconds=float(stream.latency),
        )
        shutdown.wait()


def usb_resampler(usb_rate: int) -> tuple[Any, int, int]:
    """Pick the corpus USB mic's resampler and its rational ratio.

    Returns `(None, 1, 1)` when the card already runs at `SAMPLE_RATE`.
    """
    gcd = math.gcd(usb_rate, SAMPLE_RATE)
    up = SAMPLE_RATE // gcd
    down = usb_rate // gcd
    if up == down == 1:
        return None, 1, 1
    if max(up, down) <= MAX_NUMPY_POLYPHASE_BRANCHES:
        return resample_poly, up, down
    try:
        # Imported here, never at module scope, so the resident daemon's
        # steady-state import graph stays scipy-free.
        from scipy.signal import resample_poly as scipy_resample_poly
    except ImportError:
        logger.error(
            "scipy missing; resampling the corpus USB mic %d->%d Hz with %d "
            "numpy polyphase branches, which may overrun the %d ms capture "
            "callback and drop corpus frames",
            usb_rate, SAMPLE_RATE, up,
            round(1000 * FRAME_SAMPLES / SAMPLE_RATE),
        )
        return resample_poly, up, down
    return scipy_resample_poly, up, down


def usb_mic_thread(
    usb_q: Queue,
    *,
    usb_mic_device: str,
    usb_mic_rate: int,
    stats: _BridgeStats,
    shutdown: threading.Event,
) -> None:
    """Capture optional cheap-USB-mic audio for corpus-only legs.

    Deliberately independent of the XVF mic stream so unplugging or starving
    the cheap mic cannot stall production AEC. Started only when
    JASPER_AEC_CORPUS_USB_ENABLED=1.
    """

    usb_rate = _usb_capture_rate(
        usb_mic_device=usb_mic_device, usb_mic_rate=usb_mic_rate,
    )
    capture_block = max(1, round(FRAME_SAMPLES * usb_rate / SAMPLE_RATE))
    resample, up, down = usb_resampler(usb_rate)
    accum_16 = np.empty(0, dtype=np.float32)

    def cb(indata, frames, time_info, status):
        nonlocal accum_16
        if status:
            logger.debug("usb mic status: %s", status)
        if shutdown.is_set():
            return
        mono = indata[:, 0].astype(np.float32, copy=True)
        if resample is not None:
            # float32 back: dsp_numpy returns float64 and would silently
            # promote the accumulator, scipy returns the input dtype.
            mono = resample(mono, up=up, down=down).astype(
                np.float32, copy=False,
            )
        accum_16 = np.concatenate([accum_16, mono])
        while accum_16.size >= FRAME_SAMPLES:
            chunk = accum_16[:FRAME_SAMPLES]
            accum_16 = accum_16[FRAME_SAMPLES:]
            chunk = np.clip(chunk, -32768, 32767).astype(np.int16)
            try:
                usb_q.put_nowait(chunk.tobytes())
            except Full:
                stats.inc_nested("queue_drops", "usb")
                logger.warning("usb corpus mic queue full, dropping frame")

    with sd.InputStream(
        device=usb_mic_device,
        samplerate=usb_rate,
        channels=1,
        dtype="int16",
        blocksize=capture_block,
        callback=cb,
    ):
        logger.info(
            "USB corpus mic capture opened: %s @ %d Hz mono -> %d Hz "
            "(block=%d)",
            usb_mic_device, usb_rate, SAMPLE_RATE, capture_block,
        )
        shutdown.wait()

