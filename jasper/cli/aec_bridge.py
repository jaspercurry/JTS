"""Software AEC bridge — `jasper-aec-bridge` (Python).

REPLACES the CamillaDSP-based aec-bridge. The XVF3800's on-chip
AEC turned out to be architecturally incompatible with our
"external USB DAC for the speaker" topology — the chip's AEC
pipeline assumes the chip's own audio output drives the speaker
(see XMOS XVF3800 user guide §3.5; all "Far end" categories are
defined as I²S sources). Even with USB-IN reference + correct
volume mirroring, the chip's adaptive filter doesn't reliably
attenuate echo in our config.

This bridge does the AEC in software, with raw mic 0 (channel 2 of
the chip's 6-channel USB capture, exposed by the 6-ch firmware
variant 2.0.8) as near-end and the host-side music chain as
far-end.

Two engines are supported via `JASPER_AEC_ENGINE`:
  - `speex` (default): SpeexDSP's linear NLMS EchoCanceller. Cheap
    (~1-2 % of one core), but cannot model speaker non-linearity —
    falls over once music gets loud (Speex's own docs are explicit
    about this).
  - `webrtc3`: WebRTC's modern AEC3 via the `jasper_aec3` pybind11
    binding around Trixie's `libwebrtc-audio-processing-dev` (v1.3-3,
    which IS AEC3 — the 1.x version is package-API stability, not
    algorithm version). Adds a frequency-domain residual echo
    suppressor + drift-tolerant delay estimator. ~3-8 % of one Pi 5
    core. Required for wake-during-music.

Topology:

    pcm.jasper_capture (48k stereo, host clock)
       │  reference signal (what the speaker is being asked to play)
       ▼
    [downsample 48→16k, take left channel]                         16k mono ref
       │
       │      hw:Array,0 ch 2 (16k mono, chip clock)
       │  raw mic 0 (no AEC, no NS, no AGC, no BF)
       │       │
       ▼       ▼
    SpeexDSP EchoCanceller
       │  AEC'd mono mic
       ▼
    hw:LoopbackAEC,0  (write side)
       │                    (cross-wires within snd-aloop)
       ▼
    hw:LoopbackAEC,1  (read side)
       │
       ▼
    jasper-voice (reads via JASPER_MIC_DEVICE)

Caveats this implementation does NOT yet address:
  - Reference and mic are on independent clock domains (kernel
    timer vs XVF chip's USB UAC2 SYNC). They WILL drift over
    time — Speex's AEC tolerates some drift but not unbounded.
    Future fix: drift-compensate via resampling, or expose a
    USB-side reference channel from the chip (would require
    custom firmware).
  - Frame alignment between two PortAudio streams isn't perfectly
    synchronized — the ref-mic offset can vary by a few ms each
    restart. Speex auto-adapts but convergence takes longer when
    the offset drifts.
  - We use SpeexDSP's basic EchoCanceller (linear NLMS adaptive
    filter). A nonlinear residual suppressor (Speex's
    EchoSuppress, or a separate post-filter) would help with
    speaker non-linearity at high SPL but isn't wired up here.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from queue import Queue, Empty, Full
from typing import Protocol

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

logger = logging.getLogger("jasper.aec_bridge")

# Frame size: 320 samples @ 16 kHz = 20 ms. This is also a multiple
# of WebRTC AEC3's 10 ms frame requirement (160 samples) — the
# WebRTC engine splits 320→2×160 internally. Speex's NLMS filter is
# configured to 3200 taps (200 ms tail) at construction; that
# matches the XMOS chip's 192 ms native tail per the XVF3800
# datasheet. WebRTC AEC3 manages its own filter length.
FRAME_SAMPLES = 320
FILTER_TAPS = 3200
SAMPLE_RATE = 16000

# Capture device for the reference (host-clocked dsnoop on the
# renderer→camilla loopback). Same as jasper-aec-tune uses.
REF_DEVICE = "jasper_capture"
REF_RATE = 48000  # Loopback is locked at 48 kHz by CamillaDSP
REF_CHANNELS = 2

# Capture device for the raw mic. Chip's 6-ch firmware exposes
# channels 0=conference, 1=ASR (both post-AEC + BF + NS + AGC),
# 2-5=raw mics 0-3. We use channel 2 (raw mic 0) — clean linear
# input perfect for software AEC.
# Device names are PortAudio substring matches (sounddevice's
# backend) — NOT ALSA pcm strings. PortAudio enumerates ALSA
# cards by their card description, not by hw:CARD= syntax.
MIC_DEVICE = "Array"  # matches "Array: USB Audio (hw:N,0)"
MIC_CHANNELS = 6
MIC_CHANNEL_INDEX = 2  # raw mic 0

# Output: write AEC'd mono to LoopbackAEC card (kernel index 7 per
# /etc/modprobe.d/snd-aloop.conf). PortAudio names all snd-aloop
# devices identically ("Loopback: PCM (hw:N,M)") so the unique
# substring is the hw:N,M part. jasper-voice reads the mirror
# device hw:7,1 (configured in jasper.env as JASPER_MIC_DEVICE).
OUT_DEVICE = "hw:7,0"
OUT_CHANNELS = 1
OUT_RATE = 16000

# Drop-frame threshold. If queues fill faster than they drain,
# something's wrong (CPU starvation, clock drift exceeded our
# margin). We log and drop rather than block.
QUEUE_MAXSIZE = 32

_shutdown = threading.Event()


class _Engine(Protocol):
    """AEC engine plug-in surface.

    The bridge's main loop hands the engine equal-sized mic and ref
    byte buffers (int16 mono PCM @ SAMPLE_RATE, FRAME_SAMPLES samples
    each) and gets back the AEC'd mic of the same size. Each engine
    handles its own internal framing.
    """

    def process(self, mic: bytes, ref: bytes) -> bytes: ...
    def close(self) -> None: ...


class _SpeexEngine:
    """SpeexDSP EchoCanceller — linear NLMS, ~1-2 % of one core."""

    def __init__(self) -> None:
        # The xiongyihui/speexdsp-python package ships a broken
        # __init__.py on Python 3.13 (tries to import a .py wrapper
        # that SWIG didn't generate). install.sh patches __init__.py
        # post-install; defend against future re-installs by also
        # falling back to the raw SWIG module here.
        try:
            from speexdsp import (
                EchoCanceller_create, EchoCanceller_process,
                delete_EchoCanceller,
            )
        except ImportError:
            from speexdsp._speexdsp import (  # type: ignore[no-redef]
                EchoCanceller_create, EchoCanceller_process,
                delete_EchoCanceller,
            )
        self._ec = EchoCanceller_create(
            FRAME_SAMPLES, FILTER_TAPS, SAMPLE_RATE
        )
        self._process = EchoCanceller_process
        self._delete = delete_EchoCanceller
        logger.info(
            "engine=speex frame=%d taps=%d (%dms tail) rate=%d",
            FRAME_SAMPLES, FILTER_TAPS,
            FILTER_TAPS * 1000 // SAMPLE_RATE, SAMPLE_RATE,
        )

    def process(self, mic: bytes, ref: bytes) -> bytes:
        return self._process(self._ec, mic, ref)

    def close(self) -> None:
        self._delete(self._ec)


class _WebRtc3Engine:
    """WebRTC AEC3 via the jasper_aec3 pybind11 binding.

    Splits each FRAME_SAMPLES (20 ms) buffer into 2× 10 ms windows
    internally, calls ProcessReverseStream + ProcessStream per
    window, returns the joined AEC'd capture. Includes a residual
    echo suppressor and noise suppression at kModerate.
    """

    def __init__(self) -> None:
        # Imported lazily so the bridge doesn't fail to import on
        # systems where the binding isn't built (e.g. dev laptops
        # running the test suite — the engine selector defaults to
        # speex anyway).
        from jasper_aec3 import Aec3
        self._aec = Aec3()
        logger.info(
            "engine=webrtc3 frame=%d (split internally to 10ms windows) rate=%d",
            FRAME_SAMPLES, SAMPLE_RATE,
        )

    def process(self, mic: bytes, ref: bytes) -> bytes:
        return self._aec.process(mic, ref)

    def close(self) -> None:
        # The pybind11 wrapper's std::unique_ptr<AudioProcessing>
        # is freed when the Python Aec3 instance is GC'd. No
        # explicit teardown needed.
        pass


def _select_engine() -> _Engine:
    name = os.environ.get("JASPER_AEC_ENGINE", "speex").strip().lower()
    if name == "speex":
        return _SpeexEngine()
    if name in ("webrtc3", "webrtc", "aec3"):
        return _WebRtc3Engine()
    raise ValueError(
        f"unknown JASPER_AEC_ENGINE={name!r} — expected 'speex' or 'webrtc3'"
    )


def _ref_thread(ref_q: Queue) -> None:
    """Capture 48k stereo ref via alsaaudio (PortAudio doesn't see
    custom asoundrc PCMs like `jasper_capture`), downsample to 16k
    mono on the left channel (XMOS chip's convention: ref = left).
    Push frames of FRAME_SAMPLES bytes (mono int16) onto the queue."""
    import alsaaudio
    capture_block = FRAME_SAMPLES * (REF_RATE // SAMPLE_RATE)

    pcm = alsaaudio.PCM(
        type=alsaaudio.PCM_CAPTURE,
        mode=alsaaudio.PCM_NORMAL,  # blocking
        device=REF_DEVICE,
        rate=REF_RATE,
        channels=REF_CHANNELS,
        format=alsaaudio.PCM_FORMAT_S16_LE,
        periodsize=capture_block,
    )
    logger.info("ref capture opened: %s @ %d Hz, %d ch", REF_DEVICE, REF_RATE, REF_CHANNELS)
    try:
        while not _shutdown.is_set():
            length, data = pcm.read()
            if length <= 0:
                continue
            arr = np.frombuffer(data, dtype=np.int16)
            # interleaved stereo → take left channel
            left48 = arr[::REF_CHANNELS].astype(np.float32)
            mono16 = resample_poly(left48, up=1, down=3)
            mono16 = np.clip(mono16, -32768, 32767).astype(np.int16)
            try:
                ref_q.put_nowait(mono16.tobytes())
            except Full:
                logger.warning("ref queue full, dropping frame")
    finally:
        pcm.close()


def _mic_thread(mic_q: Queue) -> None:
    """Capture 16k 6ch from XVF chip (6-ch firmware), pluck
    channel MIC_CHANNEL_INDEX (raw mic 0). Push mono int16 frames."""
    def cb(indata, frames, time_info, status):
        if status:
            logger.debug("mic status: %s", status)
        if _shutdown.is_set():
            return
        mono = indata[:, MIC_CHANNEL_INDEX].astype(np.int16, copy=True)
        try:
            mic_q.put_nowait(mono.tobytes())
        except Full:
            logger.warning("mic queue full, dropping frame")

    with sd.InputStream(
        device=MIC_DEVICE, samplerate=SAMPLE_RATE, channels=MIC_CHANNELS,
        dtype="int16", blocksize=FRAME_SAMPLES, callback=cb,
    ):
        _shutdown.wait()


def _aec_loop(ref_q: Queue, mic_q: Queue, engine: _Engine) -> None:
    """Drain both queues frame-by-frame, run the selected AEC
    engine, write to Loopback. The two queues drift independently;
    we loosely sync by always pulling one mic frame and the
    freshest ref frame we can grab without blocking — falling back
    to silence if no ref is available (shouldn't happen if camilla
    is running).

    Periodically logs the per-frame RMS of mic, ref, and AEC out
    so we can observe whether the engine is actually attenuating
    the echo. Comparing mic_rms vs aec_rms gives the running
    attenuation in dB."""
    import math
    import time
    out_stream = sd.RawOutputStream(
        device=OUT_DEVICE, samplerate=OUT_RATE, channels=OUT_CHANNELS,
        dtype="int16", blocksize=FRAME_SAMPLES,
    )
    out_stream.start()
    silence = np.zeros(FRAME_SAMPLES, dtype=np.int16).tobytes()
    frames_processed = 0
    last_log = 0.0
    # Running sums for RMS computation across the log window.
    rms_window_frames = 0
    sum_mic_sq = 0.0
    sum_ref_sq = 0.0
    sum_aec_sq = 0.0

    try:
        while not _shutdown.is_set():
            try:
                mic_bytes = mic_q.get(timeout=1.0)
            except Empty:
                logger.warning("mic queue empty for 1s — bridge stalled")
                continue

            # Drain ref queue to its newest frame (best-effort sync).
            ref_bytes = silence
            drained = 0
            while True:
                try:
                    ref_bytes = ref_q.get_nowait()
                    drained += 1
                except Empty:
                    break
            if drained > 5:
                logger.warning("drained %d stale ref frames (drift)", drained)

            clean = engine.process(mic_bytes, ref_bytes)
            out_stream.write(clean)
            frames_processed += 1

            mic_arr = np.frombuffer(mic_bytes, dtype=np.int16).astype(np.float32)
            ref_arr = np.frombuffer(ref_bytes, dtype=np.int16).astype(np.float32)
            aec_arr = np.frombuffer(clean, dtype=np.int16).astype(np.float32)
            sum_mic_sq += float(np.mean(mic_arr * mic_arr))
            sum_ref_sq += float(np.mean(ref_arr * ref_arr))
            sum_aec_sq += float(np.mean(aec_arr * aec_arr))
            rms_window_frames += 1

            now = time.monotonic()
            if now - last_log > 5.0:
                if rms_window_frames > 0:
                    mic_rms = math.sqrt(sum_mic_sq / rms_window_frames)
                    ref_rms = math.sqrt(sum_ref_sq / rms_window_frames)
                    aec_rms = math.sqrt(sum_aec_sq / rms_window_frames)
                    if mic_rms > 1.0:
                        attn_db = 20.0 * math.log10(max(aec_rms, 1.0) / mic_rms)
                    else:
                        attn_db = 0.0
                    logger.info(
                        "rms over %.1fs: ref=%.0f mic=%.0f aec=%.0f → "
                        "attenuation=%.1f dB (frames=%d ref_q=%d mic_q=%d)",
                        rms_window_frames * FRAME_SAMPLES / SAMPLE_RATE,
                        ref_rms, mic_rms, aec_rms, attn_db,
                        frames_processed, ref_q.qsize(), mic_q.qsize(),
                    )
                last_log = now
                rms_window_frames = 0
                sum_mic_sq = sum_ref_sq = sum_aec_sq = 0.0
    finally:
        out_stream.stop()
        out_stream.close()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s aec-bridge %(levelname)s %(message)s",
    )
    logger.info(
        "starting: ref=%s@%d mic=%s@%d ch=%d->ch%d out=%s@%d",
        REF_DEVICE, REF_RATE, MIC_DEVICE, SAMPLE_RATE,
        MIC_CHANNELS, MIC_CHANNEL_INDEX, OUT_DEVICE, OUT_RATE,
    )

    engine = _select_engine()

    # Signal handlers for clean shutdown
    def on_signal(signum, _frame):
        logger.info("received signal %d, shutting down", signum)
        _shutdown.set()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    ref_q: Queue[bytes] = Queue(maxsize=QUEUE_MAXSIZE)
    mic_q: Queue[bytes] = Queue(maxsize=QUEUE_MAXSIZE)

    ref_t = threading.Thread(target=_ref_thread, args=(ref_q,), daemon=True)
    mic_t = threading.Thread(target=_mic_thread, args=(mic_q,), daemon=True)
    ref_t.start()
    mic_t.start()

    try:
        _aec_loop(ref_q, mic_q, engine)
    except Exception as e:  # noqa: BLE001
        logger.exception("aec loop crashed: %s", e)
        _shutdown.set()
        return 1
    finally:
        engine.close()
        ref_t.join(timeout=2)
        mic_t.join(timeout=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
