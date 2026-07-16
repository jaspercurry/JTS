# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import logging
import select
import socket
import subprocess
import threading
import time
from typing import TYPE_CHECKING

import numpy as np

from .assistant_loudness import (
    AssistantSourceMeter,
    DEFAULT_PROFILE_PATH as ASSISTANT_LOUDNESS_PROFILE_PATH,
    confidence_for_measurement,
    profile_for_outputd,
    update_profile_from_measurement,
)
from .log_event import log_event
from .tts_routing import FANIN_TTS_SOCKET

# `sounddevice` is a Pi-side audio I/O dep (PortAudio bindings). It's not
# installed in the local dev venv and isn't needed by the pure-Python
# helpers in this module (parse_udp_device, UdpMicCapture, the dataclasses).
# Lazy-import inside the three places that actually open PortAudio streams
# (_log_audio_open_failure, MicCapture.__aenter__, TtsPlayout.__aenter__)
# so the module can be imported on a dev machine, hardware-free tests can
# parse it, and the lazy-import guards in test_lazy_imports.py can run.
# The annotations on _stream attributes use `sd.InputStream` /
# `sd.RawOutputStream`, but `from __future__ import annotations` above
# makes those strings — never evaluated.

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import sounddevice as sd


class InputDeviceUnavailable(RuntimeError):
    """The primary microphone input could not be opened at startup.

    Raised by the voice daemon's leg factory when the must-have "on"
    wake leg's device won't open (absent card, PortAudio "No input
    device matching ...", busy capture, or a malformed/unbindable UDP
    transport). The daemon's ``main()`` catches it and exits
    ``VOICE_MIC_UNAVAILABLE_EXIT`` so systemd parks the unit cleanly
    instead of crash-looping toward ``StartLimitAction=reboot``. See
    ``docs/HANDOFF-hotplug-resilience.md`` "Layer 2"."""

    def __init__(self, device: str, cause: BaseException | None = None) -> None:
        self.device = device
        detail = f": {type(cause).__name__}: {cause}" if cause is not None else ""
        super().__init__(
            f"primary microphone input {device!r} unavailable{detail}"
        )


def _log_audio_open_failure(role: str, device: str, exc: BaseException) -> None:
    """Dump environmental state when a sounddevice stream open fails.

    Called from MicCapture / TtsPlayout `__aenter__` immediately
    before re-raising. The bare exception (typically
    `ValueError: No <kind> device matching '<name>'`) doesn't tell
    us whether ALSA can see the device, whether dmesg has a recent
    USB-disconnect line, or what PortAudio actually has enumerated —
    all common when the Apple dongle de-enumerates after losing
    its analog load, or when the AEC bridge's loopback isn't fed.
    Capturing this snapshot once at failure beats blind reasoning
    from a stack trace days later.

    Best-effort: a logging helper must NEVER mask or suppress the
    underlying audio failure, so every snapshot path is wrapped in
    `try/except` and falls through to `logger.warning` rather than
    raising. The caller still re-raises the original exception.
    """
    # A missing mic is already the reconciler's single source of truth. When it
    # has confirmed "no microphone", a capture-open failure here is that same
    # expected fact — not a new incident — so log one line and skip the full
    # portaudio/arecord/aplay/dmesg snapshot. Keeps absence one flag, not a
    # cascade. Playback failures, and capture failures with a present/unknown
    # mic, still get the full snapshot below. See jasper/mic_presence.py.
    if role == "capture":
        try:
            from jasper.mic_presence import read_mic_presence
            if read_mic_presence().absent_confirmed:
                logger.warning(
                    "audio open failed (expected): role=capture device=%r — no "
                    "microphone present per the AEC reconciler; voice parked, "
                    "auto-starts on reconnect (%s)",
                    device, type(exc).__name__,
                )
                return
        except Exception:  # noqa: BLE001 — the gate must never mask the failure
            pass

    import sounddevice as sd  # Pi-side dep, lazy — see module top.

    logger.error(
        "audio open failed: role=%s device=%r exc=%s: %s",
        role, device, type(exc).__name__, exc,
    )
    try:
        # PortAudio's view — what sounddevice could see at the
        # moment of failure. If our target device isn't in this
        # list, the dongle/mic disappeared (most common cause).
        devices = sd.query_devices()
        logger.error("audio open failed: portaudio devices = %s", list(devices))
    except Exception as e:  # noqa: BLE001
        logger.warning("audio open failed: query_devices snapshot failed: %s", e)
    for cmd, label in (
        (["aplay", "-l"], "aplay -l"),
        (["arecord", "-l"], "arecord -l"),
    ):
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=2.0,
            ).stdout
            logger.error("audio open failed: %s =\n%s", label, out.strip())
        except Exception as e:  # noqa: BLE001
            logger.warning("audio open failed: %s snapshot failed: %s", label, e)
    try:
        # Last 20 lines of dmesg catches USB-disconnect / xhci
        # reset events that often correlate with dongle dropouts.
        out = subprocess.run(
            ["dmesg", "--ctime"],
            capture_output=True, text=True, timeout=2.0,
        ).stdout
        tail = "\n".join(out.strip().splitlines()[-20:])
        logger.error("audio open failed: dmesg tail =\n%s", tail)
    except Exception as e:  # noqa: BLE001
        logger.warning("audio open failed: dmesg snapshot failed: %s", e)


class MicCapture:
    """Continuous mono 16 kHz mic capture, exposed as an asyncio queue.

    Output frames: 1280 samples (80 ms) of 16 kHz int16 mono — the
    openWakeWord-recommended frame size and small enough to keep Gemini
    Live responsive. Consumers (wake-word, Gemini session) see 16 kHz
    mono regardless of what the underlying mic does.

    Capture-side rate/channels are configurable because not every mic
    supports 16 kHz mono natively. PortAudio (sounddevice's backend) does
    NOT do automatic ALSA `plughw` resampling — opening a 48 kHz-only mic
    at 16 kHz raises `Invalid sample rate`. So we open at the device's
    supported rate (16000 for XVF3800, 48000 for MiniDSP UMIK-2 et al.),
    take channel 0, and polyphase-downsample to 16 kHz here.
    """

    OUTPUT_RATE = 16000
    OUTPUT_FRAME_SAMPLES = 1280  # 80 ms at 16 kHz

    def __init__(
        self,
        device: str | int,
        capture_rate: int = OUTPUT_RATE,
        capture_channels: int = 1,
    ) -> None:
        if capture_rate < self.OUTPUT_RATE:
            raise RuntimeError(
                f"capture_rate {capture_rate} must be >= {self.OUTPUT_RATE}"
            )
        if capture_rate % self.OUTPUT_RATE != 0:
            raise RuntimeError(
                f"capture_rate {capture_rate} must be an integer multiple "
                f"of {self.OUTPUT_RATE} (downsample ratio must be exact)"
            )
        self._device = device
        self._capture_rate = capture_rate
        self._capture_channels = capture_channels
        self._decimation = capture_rate // self.OUTPUT_RATE
        # Block size at the capture rate that yields exactly OUTPUT_FRAME_SAMPLES
        # frames at OUTPUT_RATE after downsampling.
        self._capture_block = self.OUTPUT_FRAME_SAMPLES * self._decimation
        # Lazy queue init — see UdpMicCapture for rationale (construct
        # from sync code shouldn't fail on stale event-loop state).
        self._queue: asyncio.Queue[np.ndarray] | None = None
        self._stream: sd.InputStream | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _callback(self, indata, frames, time_info, status):  # noqa: ARG002
        if status:
            logger.debug("mic status: %s", status)
        if self._loop is None:
            return
        # Take channel 0 (mono). UMIK-2 et al. expose stereo, but the L
        # capsule is what we want for voice; R is silent or duplicate.
        ch0 = indata[:, 0]
        if self._decimation == 1:
            chunk = ch0.astype(np.int16, copy=True)
        else:
            # Polyphase resample with built-in anti-alias filter. We use
            # scipy here (already installed transitively for openwakeword)
            # rather than naive stride-decimation, which would alias voice
            # content above 8 kHz back into the audible band.
            from scipy.signal import resample_poly  # local import: keeps daemon startup fast
            resampled = resample_poly(
                ch0.astype(np.float32), up=1, down=self._decimation,
            )
            chunk = np.clip(resampled, -32768, 32767).astype(np.int16)
        # call_soon_threadsafe schedules _enqueue to run on the loop thread,
        # which is the only place asyncio.Queue.put_nowait can raise
        # QueueFull. Catching it here in the callback would never fire.
        self._loop.call_soon_threadsafe(self._enqueue, chunk)

    def _enqueue(self, chunk: np.ndarray) -> None:
        if self._queue is None:
            return  # callback fired before __aenter__ completed; drop
        try:
            self._queue.put_nowait(chunk)
        except asyncio.QueueFull:
            logger.warning("mic queue full, dropping frame")

    async def __aenter__(self) -> "MicCapture":
        import sounddevice as sd  # Pi-side dep, lazy — see module top.

        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=64)
        try:
            self._stream = sd.InputStream(
                device=self._device,
                samplerate=self._capture_rate,
                channels=self._capture_channels,
                dtype="int16",
                blocksize=self._capture_block,
                callback=self._callback,
            )
            self._stream.start()
        except Exception as e:  # noqa: BLE001
            # Common causes: chip not enumerated (USB-OUT shared
            # bus reset), or device-name typo. (The pre-PR-2
            # "bridge daemon down" failure mode is now handled by
            # UdpMicCapture's separate code path.) Dump full ALSA +
            # PortAudio state so the next restart's log shows what
            # was visible at failure.
            _log_audio_open_failure("MicCapture", self._device, e)
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    async def frames(self):
        if self._queue is None:
            raise RuntimeError("MicCapture.frames() called before __aenter__")
        while True:
            yield await self._queue.get()


class UdpMicCapture:
    """Mic capture that receives mono 16 kHz int16 frames over UDP.

    Same `frames()` async-generator contract as `MicCapture` so
    voice_daemon's WakeLoop is transport-agnostic. Pairs with
    jasper-aec-bridge sending UDP packets of `OUTPUT_FRAME_SAMPLES`
    int16 samples to `127.0.0.1:<port>` (the AEC'd mic stream).

    Why UDP instead of snd-aloop LoopbackAEC: snd-aloop's
    `loopback_cable` struct persists in kernel state across consumer
    death; a SIGKILL'd consumer leaves the cable half-bound with the
    internal timer wedged (`hw_ptr=0`), and only `rmmod && modprobe
    snd_aloop` (after stopping every consumer) or a reboot can
    recover. Hit in production 2026-05-11.  UDP localhost has no
    kernel-side state to corrupt: either side can crash without
    affecting the other, `sendto()` is non-blocking (eliminates the
    bridge SIGTERM-observability issue), and there's no module to
    reload.  ~256 kbps loopback traffic is effectively zero-loss on
    Linux's `lo`.  Standard pattern in Mumble, VoIP gateways, Snapcast.
    """

    OUTPUT_RATE = MicCapture.OUTPUT_RATE
    OUTPUT_FRAME_SAMPLES = MicCapture.OUTPUT_FRAME_SAMPLES

    def __init__(
        self, host: str = "127.0.0.1", port: int = 9876,
    ) -> None:
        self._host = host
        self._port = port
        # Queue is lazily created in __aenter__ so the class is safe
        # to construct from sync code (e.g. unit tests that just
        # assert factory dispatch). In Python 3.9 `asyncio.Queue()`
        # calls `get_event_loop()` at construction; if there's a
        # stale-closed loop in the thread (a real-world scenario in
        # test suites), it raises. Deferring keeps the class
        # construct-anywhere.
        self._queue: asyncio.Queue[np.ndarray] | None = None
        self._transport: asyncio.BaseTransport | None = None

    async def __aenter__(self) -> "UdpMicCapture":
        loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=64)
        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: _UdpMicProtocol(self._queue),
                local_addr=(self._host, self._port),
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                "UdpMicCapture bind failed: host=%s port=%d exc=%s: %s",
                self._host, self._port, type(e).__name__, e,
            )
            raise
        logger.info(
            "UdpMicCapture listening on %s:%d (frame=%d samples @ %d Hz)",
            self._host, self._port, self.OUTPUT_FRAME_SAMPLES, self.OUTPUT_RATE,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    async def frames(self):
        if self._queue is None:
            raise RuntimeError("UdpMicCapture.frames() called before __aenter__")
        while True:
            yield await self._queue.get()


class _UdpMicProtocol(asyncio.DatagramProtocol):
    """Translates UDP datagrams of int16 PCM into queue items.

    Each datagram is one mic frame (`OUTPUT_FRAME_SAMPLES` int16
    samples = 2 * 1280 = 2560 bytes by default). Out-of-order /
    lost packets are effectively impossible on `lo` at our rate, so
    no sequence number / reordering buffer.
    """

    def __init__(self, queue: asyncio.Queue[np.ndarray]) -> None:
        self._queue = queue

    def datagram_received(self, data: bytes, _addr) -> None:
        if not data:
            return
        # Defensive: a malformed sender could send odd byte counts.
        # `np.frombuffer` would raise a ValueError; we'd rather drop
        # the bad packet and keep the daemon healthy.
        if len(data) % 2 != 0:
            logger.warning(
                "UdpMicCapture: dropping malformed packet (%d bytes, odd)",
                len(data),
            )
            return
        chunk = np.frombuffer(data, dtype=np.int16)
        try:
            self._queue.put_nowait(chunk)
        except asyncio.QueueFull:
            logger.warning("UdpMicCapture queue full, dropping frame")


def parse_udp_device(device: str) -> tuple[str, int] | None:
    """If `device` denotes a UDP mic source, return (host, port).

    Accepted forms:
      - `udp://<host>:<port>`     full URL form
      - `udp:<port>`              shorthand, host = 127.0.0.1

    Returns None if the device string is not a UDP form, so callers
    fall through to the PortAudio path. Raises ValueError if the
    string starts with `udp` but is malformed (typo guard).
    """
    if not device.lower().startswith("udp"):
        return None
    rest = device[3:]
    if rest.startswith("://"):
        rest = rest[3:]
        if ":" not in rest:
            raise ValueError(
                f"udp device {device!r} missing port (expected udp://HOST:PORT)"
            )
        host, port_str = rest.rsplit(":", 1)
    elif rest.startswith(":"):
        host = "127.0.0.1"
        port_str = rest[1:]
    else:
        raise ValueError(
            f"udp device {device!r} malformed; "
            f"use 'udp:PORT' or 'udp://HOST:PORT'"
        )
    try:
        port = int(port_str)
    except ValueError as e:
        raise ValueError(
            f"udp device {device!r} has non-integer port {port_str!r}"
        ) from e
    if not (1 <= port <= 65535):
        raise ValueError(f"udp device {device!r} port {port} out of range")
    return host, port


def make_mic_capture(
    device: str | int,
    capture_rate: int = MicCapture.OUTPUT_RATE,
    capture_channels: int = 1,
):
    """Construct the right mic-capture flavour for a device string.

    `device` matching `udp:PORT` / `udp://HOST:PORT` → `UdpMicCapture`
    (the AEC bridge sends post-processed mic to that socket;
    `capture_rate` / `capture_channels` are ignored because the
    bridge has already resampled to 16 kHz mono and the format is
    fixed at the bridge↔voice transport contract).

    Anything else → `MicCapture` (PortAudio + ALSA path: chip-direct
    via `Array`, or any other USB mic).
    """
    if isinstance(device, str):
        udp = parse_udp_device(device)
        if udp is not None:
            host, port = udp
            return UdpMicCapture(host=host, port=port)
    return MicCapture(
        device, capture_rate=capture_rate, capture_channels=capture_channels,
    )


class TtsPlayout:
    """Plays provider 24 kHz int16 mono PCM stream out to an ALSA device.

    The output device may not natively support 24 kHz mono — `jasper_dongle`
    (the shared dmix wrapping the Apple USB-C dongle) is fixed at 48 kHz
    and PortAudio doesn't go through ALSA's `plug` layer for rate
    conversion. So we let the caller configure an `output_rate` and
    polyphase-upsample 24 kHz → output_rate inside `write()`.

    Gain validation for the legacy direct-device path. Current production
    sends TTS through the local IPC path before CamillaDSP, where the
    mix owner matches assistant loudness to content and applies the
    peak-aware ceiling. This Python class only rejects malformed values
    and floors extreme attenuation to the mute-equivalent minimum.
    """

    INPUT_RATE = 24000

    # Floor — below this, TTS is effectively silent. Used when the
    # user mutes, when Camilla is unreachable at startup, or when a
    # volume reading looks malformed.
    MIN_TTS_GAIN_DB = -60.0

    def __init__(
        self,
        device: str | int,
        output_rate: int = INPUT_RATE,
        gain_db: float = 0.0,
        *,
        drain_tail_sec: float = 0.085,  # production wires from cfg.tts_drain_tail_sec
    ) -> None:
        if output_rate < self.INPUT_RATE:
            raise RuntimeError(
                f"output_rate {output_rate} must be >= {self.INPUT_RATE}"
            )
        if output_rate % self.INPUT_RATE != 0:
            raise RuntimeError(
                f"output_rate {output_rate} must be an integer multiple "
                f"of {self.INPUT_RATE} (upsample ratio must be exact)"
            )
        self._device = device
        self._output_rate = output_rate
        self._upsample = output_rate // self.INPUT_RATE
        # Linear gain factor applied before resample/write. Updated at
        # runtime via set_gain_db when a caller explicitly changes it.
        # Initial value is the floor (effectively silent) so the daemon
        # cannot accidentally play TTS loud during the brief window
        # between TtsPlayout construction and the first configured
        # gain. Until then we'd rather have inaudible TTS than blast.
        self._gain_linear = float(10 ** (self.MIN_TTS_GAIN_DB / 20.0))
        self._gain_db = self.MIN_TTS_GAIN_DB
        # Cumulative pacing-sleep time since the last take_paced_sec().
        # Only the outputd/fan-in transport paces (the PortAudio path is
        # device-paced), but the field lives here so every transport
        # answers take_paced_sec() and callers stay transport-agnostic.
        self._paced_total_sec = 0.0
        self._stream: sd.RawOutputStream | None = None
        # One-shot warning latch: if a caller invokes write() before
        # entering the async context (so _stream is still None), log
        # once. The class is a context manager and the underlying
        # ALSA stream only opens in __aenter__; without that, write()
        # used to silently no-op, which was the cause of "I can't
        # hear the cue" being mis-diagnosed as routing problems.
        self._closed_stream_warned = False
        # Drain tracking — see `expected_drain_at`. None (not 0.0)
        # because CLOCK_MONOTONIC's reference is platform-defined; 0.0
        # is briefly a legitimate now() value on a freshly-booted Pi.
        self._drain_tail_sec = float(drain_tail_sec)
        self._ring_end_monotonic: float | None = None
        # Apply the constructor's gain_db through the same validation +
        # validation path as runtime updates. If a caller passes the
        # legacy "-8.0 fixed gain" value, this becomes the active level.
        self.set_gain_db(gain_db)

    def set_gain_db(self, db: float) -> None:
        """Update TTS gain. Non-finite inputs are rejected and very low
        finite values floor to the mute-equivalent minimum. Single-float
        assignment is atomic under the GIL, so no lock is needed for the
        read path in write()."""
        try:
            db = float(db)
        except (TypeError, ValueError):
            logger.warning("tts gain rejected (not a number): %r", db)
            return
        if db != db or db in (float("inf"), float("-inf")):
            logger.warning("tts gain rejected (non-finite): %r", db)
            return
        clamped = max(self.MIN_TTS_GAIN_DB, db)
        if clamped == self._gain_db:
            return
        # 0.0 dB -> 1.0 linear; floor -> ~0.001 linear. Computed once
        # per change, not per write. With no max-gain ceiling, extremely
        # large finite debug/test values can overflow the exponent; keep
        # them representable as inf so the sample path clips explicitly.
        try:
            self._gain_linear = float(10 ** (clamped / 20.0))
        except OverflowError:
            self._gain_linear = float("inf")
        self._gain_db = clamped
        # DEBUG (not INFO): the active TTS IPC owner publishes the richer
        # assistant loudness decision telemetry, and this low-level floor
        # log is noisy.
        if clamped != db:
            logger.debug(
                "tts gain set: requested %.1f dB -> floored to %.1f dB",
                db, clamped,
            )
        else:
            logger.debug("tts gain set: %.1f dB", clamped)

    @property
    def gain_db(self) -> float:
        return self._gain_db

    async def __aenter__(self) -> "TtsPlayout":
        import sounddevice as sd  # Pi-side dep, lazy — see module top.
        # Eagerly warm scipy.signal so the first runtime write() doesn't
        # pay ~1 s of module-import cost on the event loop. Before this,
        # the first wake of a fresh daemon paid for the import inside
        # write()'s polyphase resample step — blocking the loop for
        # ~941 ms and delaying _acquire_and_drain so a fast-talker's
        # whole question ended up in the acquire-buffer (sent to the
        # LLM but never seen by Silero, which then aborted the turn).
        # See voice_daemon._handle_wake_frame + the sched_lag breakdown
        # in _begin_turn for the diagnostic that surfaced this.
        from scipy.signal import resample_poly  # noqa: F401  (pre-warm only)

        # Open as STEREO even though our input is mono. The dongle's
        # dmix (`pcm.jasper_out` in /etc/asound.conf) is configured at
        # channels=2 with no plug layer; opening at channels=1
        # against it makes PortAudio do something quietly broken —
        # mono samples land in the stereo frame interleave as if they
        # were L/R pairs, and audio comes out at half speed with
        # weird amplitude. Manual mono→stereo duplication in write()
        # is unambiguous and matches the dmix's native shape.
        try:
            self._stream = sd.RawOutputStream(
                device=self._device,
                samplerate=self._output_rate,
                channels=2,
                dtype="int16",
            )
            self._stream.start()
        except Exception as e:  # noqa: BLE001
            # Most common cause of "No output device matching ..." is
            # the Apple dongle de-enumerating because nothing's
            # plugged into its 3.5 mm jack (it loses USB Audio class
            # exposure without an analog load). Dump enough state to
            # tell that case apart from "device exists but is busy"
            # or "PortAudio internal error" — the bare ValueError
            # alone wasn't enough to root-cause the 9000+ restart
            # spiral on 2026-05-10.
            _log_audio_open_failure("TtsPlayout", self._device, e)
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    async def write_segment(
        self,
        pcm: bytes,
        *,
        provider_item_id: str | None = None,
        segment_kind: str = "assistant",
        source_profile=None,
    ) -> None:
        """Write one provider audio chunk.

        The sounddevice transport has no playout ledger, so segment
        metadata is intentionally ignored here. Outputd overrides this
        to carry provider identity across the IPC boundary.
        """
        _ = (provider_item_id, segment_kind, source_profile)
        await self.write(pcm)

    async def end_segment(self) -> None:
        """Mark the current logical TTS segment complete.

        No-op for sounddevice; outputd uses it to let the ledger
        distinguish "fully queued and waiting to drain" from "still
        streaming more audio."
        """
        return None

    async def prepare_assistant_context(
        self,
        *,
        provider: str,
        model: str,
        voice: str,
        silence_target_lufs: float,
        canonical_volume_db: float | None = None,
        downstream_volume_db: float | None = None,
        muted: bool | None = None,
    ) -> None:
        """Freeze final-output loudness context before a turn starts.

        No-op for the legacy sounddevice path. Outputd overrides this
        because it owns content metering and final assistant gain.
        """
        _ = (
            provider,
            model,
            voice,
            silence_target_lufs,
            canonical_volume_db,
            downstream_volume_db,
            muted,
        )
        return None

    async def pause_content_meter(self) -> None:
        """Tell the final-output owner to ignore temporary measurement/ducking.

        No-op for sounddevice.
        """
        return None

    async def resume_content_meter(self) -> None:
        """Resume content metering after a paused section."""
        return None

    async def write(self, pcm: bytes) -> None:
        """Input is MONO int16 PCM at INPUT_RATE (24 kHz) — same shape
        live providers emit and what cue WAVs are stored at. Internally
        we apply gain + upsample + mono→stereo duplication, then
        hand off to the (stereo) sounddevice stream."""
        if self._stream is None:
            if not self._closed_stream_warned:
                logger.warning(
                    "TtsPlayout.write called on a closed stream — "
                    "%d bytes silently dropped. Did you forget "
                    "`async with tts:`? (Suppressing further such "
                    "warnings for this instance.)",
                    len(pcm),
                )
                self._closed_stream_warned = True
            return
        # Empty PCM would set the drain deadline to "now + 0", masking the silent state.
        if not pcm:
            return
        # Always go through the numpy pipeline so the mono→stereo
        # duplication at the end runs uniformly. The dropped fast
        # path was for "no gain, no upsample" which is a test-only
        # config in practice — production always has both.
        arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        if self._gain_linear != 1.0:
            arr = arr * self._gain_linear
        if self._upsample > 1:
            # Polyphase resample with built-in anti-alias filter. Same
            # reasoning as MicCapture's downsampler — naive zero-stuff
            # would create high-frequency images.
            # scipy.signal is pre-warmed in __aenter__ — this `from`
            # statement is a sys.modules cache lookup, not a real
            # import. Keep as-is to bind the symbol into local scope.
            from scipy.signal import resample_poly
            arr = resample_poly(arr, up=self._upsample, down=1)
        # Mono → stereo: each mono sample becomes a (L, R) pair with
        # L=R. np.repeat(arr, 2) interleaves correctly: [s0, s0, s1,
        # s1, …]. The stream is opened at channels=2 so this is the
        # exact byte layout it expects.
        mono_i16 = np.clip(arr, -32768, 32767).astype(np.int16)
        stereo_i16 = np.repeat(mono_i16, 2)
        # Update the drain deadline *before* the blocking write so a
        # concurrent reader (the idle watchdog) sees a consistent view.
        chunk_duration_sec = len(mono_i16) / self._output_rate
        now = time.monotonic()
        if self._ring_end_monotonic is None or now > self._ring_end_monotonic:
            self._ring_end_monotonic = now + chunk_duration_sec
        else:
            self._ring_end_monotonic += chunk_duration_sec
        write_start = now
        await asyncio.to_thread(self._stream.write, stereo_i16.tobytes())
        write_ms = (time.monotonic() - write_start) * 1000
        chunk_ms = chunk_duration_sec * 1000
        # Sustained back-pressure correlates with OS-layer underruns and
        # audible glitches. Doesn't affect drain timing (we track samples
        # queued, not write latency).
        if write_ms > chunk_ms + 100:
            logger.warning(
                "tts.write slow: %.0fms for %.0fms of audio "
                "(%d frames @ %d Hz)",
                write_ms, chunk_ms, len(mono_i16), self._output_rate,
            )

    async def flush(self) -> dict | None:
        """Drop any audio currently buffered inside sounddevice / ALSA so
        the speaker goes silent immediately. Used for barge-in: when the
        user interrupts the model, we want sub-50ms cutoff, not the
        100-300ms tail you'd get from waiting for buffered samples to
        finish playing.

        sounddevice's abort() stops the stream and discards pending
        samples (vs. stop() which finishes them). Restart with start()
        so the next write() works immediately."""
        if self._stream is None:
            return None
        try:
            await asyncio.to_thread(self._stream.abort)
            await asyncio.to_thread(self._stream.start)
        except Exception as e:  # noqa: BLE001
            logger.warning("tts flush failed: %s", e)
        # abort() discarded the ring; the tracked deadline is stale.
        self._ring_end_monotonic = None
        return None

    def expected_drain_at(self) -> float:
        """Monotonic deadline at which the last-queued sample's tail
        will have cleared the OS audio stack — i.e. the speaker is
        silent. Returns ``0.0`` when nothing is queued (the sentinel
        naturally compares as "already drained" against
        ``time.monotonic()``)."""
        if self._ring_end_monotonic is None:
            return 0.0
        return self._ring_end_monotonic + self._drain_tail_sec

    async def wait_drained(self) -> None:
        """Block until ``expected_drain_at`` has passed. Cheap when
        nothing is queued (the 0.0 sentinel yields negative remaining,
        which skips the sleep). Single ``asyncio.sleep`` otherwise —
        deadline is known up-front, no polling."""
        remaining = self.expected_drain_at() - time.monotonic()
        if remaining > 0.0:
            await asyncio.sleep(remaining)

    def take_paced_sec(self) -> float:
        """Pacing-sleep seconds accumulated since the last call; resets.

        The voice daemon reads this once per turn for the turn-ended
        accounting line. Zero means no write waited on the IPC owner's
        pending budget (always true for the device-paced PortAudio
        transport, which never sleeps deliberately).
        """
        v = self._paced_total_sec
        self._paced_total_sec = 0.0
        return v


_OUTPUTD_AUDIO_FRAME_BYTES = 4  # stereo S16_LE
_OUTPUTD_SAMPLE_RATE = 48_000
_OUTPUTD_FLUSH_ACK_TIMEOUT_SEC = 3.0
# Keep individual IPC messages well below the daemon's 2 MiB hard cap.
# 250 ms chunks make barge-in/flush sharper and set the granularity at
# which the writer's pacing (below) applies backpressure. Chunking alone
# applies none — the owner drops on overflow rather than blocking.
_OUTPUTD_MAX_AUDIO_CHUNK_BYTES = (
    _OUTPUTD_SAMPLE_RATE * _OUTPUTD_AUDIO_FRAME_BYTES // 4
)
# Pace sustained writes so the IPC owner's pending-audio queue never
# overflows. The owner (jasper-fanin's TTS lane, DEFAULT_MAX_PENDING_FRAMES
# in rust/jasper-fanin/src/tts.rs = 2 s) DROPS whole audio commands that
# arrive while its queue is full — it cannot block the socket reader,
# because a blocked reader would also stall FLUSH (barge-in) behind queued
# audio. OpenAI Realtime delivers replies faster than realtime (~11 s of
# audio in ~4 s), so an unpaced writer overflows the budget and the
# surviving chunks play as garbled "fast-forward" audio
# (event=fanin.tts_command_dropped, observed on JTS3 2026-06-11).
# Keeping ≤1.2 s queued ahead of realtime leaves 0.55 s of margin
# (2.0 s budget − 1.2 s watermark − one 0.25 s IPC chunk) against
# event-loop jitter AND the bounded drift from a concurrent same-object
# writer (the fire-and-forget listening chirp, ~0.3 s, whose ring update
# can race another write's local pacing mirror), while staying deep
# enough that a stalled writer has >1 s before audible underrun.
# tests/test_tts_ipc_pacing.py pins the watermark against the Rust
# budget so the two cannot silently drift apart.
_OUTPUTD_PACE_AHEAD_SEC = 1.2

# Pacing sleeps go through this alias so tests can substitute a spy
# without patching the global asyncio module.
_pace_sleep = asyncio.sleep


def _outputd_audio_chunks(data: bytes):
    """Split TTS IPC AUDIO payloads below the daemon's protocol cap.

    Rust rejects AUDIO chunks above 2 MiB before allocation. Cached cue
    WAVs are normally short, but dynamic spoken text can occasionally
    be long enough after 24 kHz mono -> 48 kHz stereo conversion to cross
    that limit. Chunking here keeps the protocol bounded without changing
    the public TtsPlayout.write contract.
    """
    if not data:
        return []
    if len(data) % _OUTPUTD_AUDIO_FRAME_BYTES != 0:
        raise ValueError("TTS IPC audio payload must contain whole stereo frames")
    chunk_size = _OUTPUTD_MAX_AUDIO_CHUNK_BYTES
    if chunk_size % _OUTPUTD_AUDIO_FRAME_BYTES != 0:
        raise AssertionError("TTS IPC chunk size must align to stereo frames")
    for i in range(0, len(data), chunk_size):
        yield data[i:i + chunk_size]


def _outputd_segment_kind(kind: str) -> str:
    if kind in {"assistant", "cue", "chirp"}:
        return kind
    logger.warning(
        "fan-in TTS IPC segment kind rejected: %r; falling back to assistant",
        kind,
    )
    return "assistant"


def _outputd_provider_token(provider_item_id: str | None) -> str:
    if provider_item_id is None:
        return "-"
    if _outputd_token_ok(provider_item_id):
        return provider_item_id
    logger.warning("fan-in TTS IPC provider item id rejected: %r", provider_item_id)
    return "-"


def _outputd_token_ok(value: str) -> bool:
    return bool(value) and value.isascii() and not any(ch.isspace() for ch in value)


def _outputd_profile_tokens(profile) -> list[str] | None:
    if profile is None:
        return None
    for field in (profile.provider, profile.model, profile.voice):
        if not _outputd_token_ok(field):
            logger.warning(
                "fan-in TTS IPC profile token rejected: provider=%r model=%r voice=%r",
                profile.provider, profile.model, profile.voice,
            )
            return None
    return [
        profile.provider,
        profile.model,
        profile.voice,
        f"{profile.source_lufs:.2f}",
        f"{profile.source_peak_dbfs:.2f}",
        f"{profile.confidence:.2f}",
    ]


class _OutputdStreamAdapter:
    """Tiny sync writer used by OutputdTtsPlayout.

    OutputdTtsPlayout does resample, mono-to-stereo, and drain accounting
    before calling ``self._stream.write(bytes)`` in a worker thread. This
    adapter preserves the blocking stream shape while swapping the final
    sink from PortAudio to the local TTS Unix socket.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._recv_buffer = bytearray()
        self._lock = threading.Lock()
        self._active_segment: tuple[str, str, tuple[str, ...] | None] | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def _readline_locked(self, timeout_sec: float) -> bytes:
        """Read one daemon response line while the caller holds _lock."""
        deadline = time.monotonic() + timeout_sec
        while True:
            newline_at = self._recv_buffer.find(b"\n")
            if newline_at >= 0:
                line = bytes(self._recv_buffer[: newline_at + 1])
                del self._recv_buffer[: newline_at + 1]
                return line

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            readable, _, _ = select.select([self._sock], [], [], remaining)
            if not readable:
                raise TimeoutError
            chunk = self._sock.recv(4096)
            if not chunk:
                return b""
            self._recv_buffer.extend(chunk)

    def _close_unlocked(self, *, send_close: bool) -> None:
        if self._closed:
            return
        try:
            if send_close:
                if self._active_segment is not None:
                    self._sock.sendall(b"SEGMENT_END\n")
                    self._active_segment = None
                self._sock.sendall(b"CLOSE\n")
        except OSError:
            pass
        self._closed = True
        self._recv_buffer.clear()
        self._sock.close()

    def _sendall_locked(self, data: bytes) -> None:
        if self._closed:
            raise BrokenPipeError("TTS IPC socket is closed")
        try:
            self._sock.sendall(data)
        except OSError:
            self._close_unlocked(send_close=False)
            raise

    def set_gain_db(self, db: float) -> None:
        with self._lock:
            self._sendall_locked(f"GAIN {db:.3f}\n".encode("ascii"))

    def prepare_assistant(
        self,
        *,
        provider: str,
        model: str,
        voice: str,
        silence_target_lufs: float,
        canonical_volume_db: float | None = None,
        downstream_volume_db: float | None = None,
        muted: bool | None = None,
    ) -> None:
        if not (
            _outputd_token_ok(provider)
            and _outputd_token_ok(model)
            and _outputd_token_ok(voice)
        ):
            logger.warning(
                "fan-in TTS IPC prepare rejected invalid profile identity: "
                "provider=%r model=%r voice=%r",
                provider, model, voice,
            )
            return
        with self._lock:
            parts = [
                "PREPARE_ASSISTANT",
                provider,
                model,
                voice,
                f"{float(silence_target_lufs):.2f}",
            ]
            if (
                canonical_volume_db is not None
                and downstream_volume_db is not None
                and muted is not None
            ):
                parts.extend((
                    f"{float(canonical_volume_db):.3f}",
                    f"{float(downstream_volume_db):.3f}",
                    "1" if muted else "0",
                ))
            self._sendall_locked((" ".join(parts) + "\n").encode("ascii"))

    def pause_content_meter(self) -> None:
        with self._lock:
            self._sendall_locked(b"CONTENT_METER_PAUSE\n")

    def resume_content_meter(self) -> None:
        with self._lock:
            self._sendall_locked(b"CONTENT_METER_RESUME\n")

    def start_segment(
        self,
        *,
        kind: str,
        provider_item_id: str | None,
        profile=None,
    ) -> None:
        profile_tokens = _outputd_profile_tokens(profile)
        segment = (
            _outputd_segment_kind(kind),
            _outputd_provider_token(provider_item_id),
            tuple(profile_tokens) if profile_tokens is not None else None,
        )
        with self._lock:
            if self._active_segment == segment:
                return
            if self._active_segment is not None:
                self._sendall_locked(b"SEGMENT_END\n")
            parts = ["SEGMENT_START", segment[0], segment[1]]
            if profile_tokens is not None:
                parts.extend(profile_tokens)
            self._sendall_locked((" ".join(parts) + "\n").encode("ascii"))
            self._active_segment = segment

    def end_segment(self) -> None:
        with self._lock:
            if self._active_segment is None:
                return
            self._sendall_locked(b"SEGMENT_END\n")
            self._active_segment = None

    def write(self, data: bytes) -> None:
        with self._lock:
            self._sendall_locked(f"AUDIO {len(data)}\n".encode("ascii"))
            self._sendall_locked(data)

    def abort(self) -> None:
        self.flush_sync()

    def flush_sync(self) -> dict | None:
        with self._lock:
            try:
                self._sendall_locked(b"FLUSH_SYNC\n")
                self._active_segment = None
                line = self._readline_locked(_OUTPUTD_FLUSH_ACK_TIMEOUT_SEC)
            except TimeoutError:
                logger.warning(
                    "fan-in TTS IPC flush ack timed out after %.1fs; "
                    "closing socket",
                    _OUTPUTD_FLUSH_ACK_TIMEOUT_SEC,
                )
                self._close_unlocked(send_close=False)
                return None
            except OSError as e:
                logger.warning("fan-in TTS IPC flush failed: %s", e)
                self._close_unlocked(send_close=False)
                return None
        if not line:
            return None
        try:
            ack = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            logger.warning("fan-in TTS IPC flush ack parse failed: %s", e)
            return None
        if not isinstance(ack, dict):
            logger.warning("fan-in TTS IPC flush ack had unexpected shape: %r", ack)
            return None
        return ack

    def start(self) -> None:
        # The stream remains open after FLUSH. This mirrors the
        # sounddevice RawOutputStream.start() call TtsPlayout.flush uses.
        return None

    def close(self) -> None:
        with self._lock:
            self._close_unlocked(send_close=True)


class OutputdTtsPlayout(TtsPlayout):
    """TtsPlayout-compatible client for the fan-in TTS IPC protocol.

    The transport name is historical; the packaged socket is fan-in so
    TTS/cues enter before CamillaDSP. Python's contract stays unchanged:
    provider PCM enters as 24 kHz mono, write() resamples to 48 kHz,
    duplicates mono to stereo, updates the drain deadline, and writes
    bytes to this class's socket adapter. Gain travels as metadata so the
    active TTS IPC owner can apply the final clamp at its mix boundary.
    """

    def __init__(
        self,
        socket_path: str = FANIN_TTS_SOCKET,
        output_rate: int = _OUTPUTD_SAMPLE_RATE,
        gain_db: float = 0.0,
        *,
        drain_tail_sec: float = 0.085,
        provider: str = "",
        model: str = "",
        voice: str = "",
        profile_path: str = ASSISTANT_LOUDNESS_PROFILE_PATH,
    ) -> None:
        if output_rate != _OUTPUTD_SAMPLE_RATE:
            raise RuntimeError(
                "fan-in TTS IPC transport requires 48 kHz stereo IPC; "
                f"got output_rate={output_rate}"
            )
        super().__init__(
            device=socket_path,
            output_rate=output_rate,
            gain_db=gain_db,
            drain_tail_sec=drain_tail_sec,
        )
        self._socket_path = socket_path
        self._provider = provider
        self._model = model
        self._voice = voice
        self._profile_path = profile_path
        self._assistant_meter: AssistantSourceMeter | None = None
        self._profile_cache_key: tuple[str, str, str, str] | None = None
        self._profile_cache = None

    async def _connect_stream_adapter(self) -> _OutputdStreamAdapter:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            await asyncio.to_thread(sock.connect, self._socket_path)
        except Exception as e:  # noqa: BLE001
            sock.close()
            logger.error(
                "fan-in TTS IPC connect failed: socket=%s exc=%s: %s",
                self._socket_path, type(e).__name__, e,
            )
            raise
        stream = _OutputdStreamAdapter(sock)
        try:
            stream.set_gain_db(self.gain_db)
        except OSError:
            stream.close()
            raise
        logger.info("fan-in TTS IPC connected: socket=%s", self._socket_path)
        return stream

    async def __aenter__(self) -> "OutputdTtsPlayout":
        from scipy.signal import resample_poly  # noqa: F401  (pre-warm only)

        self._stream = await self._connect_stream_adapter()  # type: ignore[assignment]
        return self

    async def _current_outputd_stream(self):
        stream = self._stream
        if isinstance(stream, _OutputdStreamAdapter) and stream.closed:
            log_event(
                logger,
                "tts_fanin.reconnect",
                reason="closed_socket",
                socket=self._socket_path,
            )
            try:
                stream = await self._connect_stream_adapter()
            except Exception as e:  # noqa: BLE001
                log_event(
                    logger,
                    "tts_fanin.reconnect_failed",
                    reason="closed_socket",
                    socket=self._socket_path,
                    exc_type=type(e).__name__,
                    err=str(e),
                    level=logging.WARNING,
                )
                return None
            self._stream = stream  # type: ignore[assignment]
        return stream

    def set_gain_db(self, db: float) -> None:
        super().set_gain_db(db)
        stream = self._stream
        if isinstance(stream, _OutputdStreamAdapter) and stream.closed:
            return
        if stream is not None and hasattr(stream, "set_gain_db"):
            try:
                stream.set_gain_db(self.gain_db)
            except OSError as e:
                logger.warning("fan-in TTS IPC gain update failed: %s", e)

    async def prepare_assistant_context(
        self,
        *,
        provider: str,
        model: str,
        voice: str,
        silence_target_lufs: float,
        canonical_volume_db: float | None = None,
        downstream_volume_db: float | None = None,
        muted: bool | None = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._voice = voice
        for attempt in range(2):
            stream = await self._current_outputd_stream()
            if stream is None:
                return
            prepare = getattr(stream, "prepare_assistant", None)
            if prepare is None:
                return
            try:
                prepare_kwargs = {
                    "provider": provider,
                    "model": model,
                    "voice": voice,
                    "silence_target_lufs": silence_target_lufs,
                }
                if (
                    canonical_volume_db is not None
                    and downstream_volume_db is not None
                    and muted is not None
                ):
                    prepare_kwargs.update(
                        canonical_volume_db=canonical_volume_db,
                        downstream_volume_db=downstream_volume_db,
                        muted=muted,
                    )
                await asyncio.to_thread(
                    prepare,
                    **prepare_kwargs,
                )
                return
            except OSError as e:
                if (
                    attempt == 0
                    and isinstance(stream, _OutputdStreamAdapter)
                    and stream.closed
                ):
                    log_event(
                        logger,
                        "tts_fanin.control_retry",
                        method="prepare_assistant",
                        reason="closed_socket",
                        exc_type=type(e).__name__,
                        err=str(e),
                    )
                    continue
                logger.warning("fan-in TTS IPC prepare assistant failed: %s", e)
                return

    async def pause_content_meter(self) -> None:
        await self._send_meter_control("pause_content_meter")

    async def resume_content_meter(self) -> None:
        await self._send_meter_control("resume_content_meter")

    async def _send_meter_control(self, method: str) -> None:
        for attempt in range(2):
            stream = await self._current_outputd_stream()
            if stream is None:
                return
            fn = getattr(stream, method, None)
            if fn is None:
                return
            try:
                await asyncio.to_thread(fn)
                return
            except OSError as e:
                if (
                    attempt == 0
                    and isinstance(stream, _OutputdStreamAdapter)
                    and stream.closed
                ):
                    log_event(
                        logger,
                        "tts_fanin.control_retry",
                        method=method,
                        reason="closed_socket",
                        exc_type=type(e).__name__,
                        err=str(e),
                    )
                    continue
                logger.warning("fan-in TTS IPC %s failed: %s", method, e)
                return

    async def write(self, pcm: bytes) -> None:
        await self.write_segment(pcm)

    async def write_segment(
        self,
        pcm: bytes,
        *,
        provider_item_id: str | None = None,
        segment_kind: str = "assistant",
        source_profile=None,
    ) -> None:
        """Send un-gained 48 kHz stereo PCM to the TTS IPC owner.

        Gain is sent as metadata and enforced by fan-in's final mix
        clamp. Drain accounting mirrors TtsPlayout.write so the voice
        daemon's turn-ending contract stays identical.
        """
        if not pcm:
            return
        if self._stream is None:
            if not self._closed_stream_warned:
                logger.warning(
                    "OutputdTtsPlayout.write called on a closed stream - "
                    "%d bytes silently dropped. Did you forget "
                    "`async with tts:`? (Suppressing further such "
                    "warnings for this instance.)",
                    len(pcm),
                )
                self._closed_stream_warned = True
            return
        stream = await self._current_outputd_stream()
        if stream is None:
            return

        arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        if (
            segment_kind == "assistant"
            and self._provider
            and self._model
            and self._voice
        ):
            if self._assistant_meter is None:
                self._assistant_meter = AssistantSourceMeter()
            self._assistant_meter.observe_pcm_24k(pcm)
        if self._upsample > 1:
            from scipy.signal import resample_poly
            arr = resample_poly(arr, up=self._upsample, down=1)
        mono_i16 = np.clip(arr, -32768, 32767).astype(np.int16)
        stereo_i16 = np.repeat(mono_i16, 2)

        chunk_duration_sec = len(mono_i16) / self._output_rate
        write_start = time.monotonic()
        for attempt in range(2):
            try:
                if hasattr(stream, "set_gain_db"):
                    await asyncio.to_thread(stream.set_gain_db, self.gain_db)
                if hasattr(stream, "start_segment"):
                    profile = self._profile_for_segment(
                        segment_kind, source_profile=source_profile,
                    )
                    await asyncio.to_thread(
                        stream.start_segment,
                        kind=segment_kind,
                        provider_item_id=provider_item_id,
                        profile=profile,
                    )
                break
            except OSError as e:
                if (
                    attempt == 0
                    and isinstance(stream, _OutputdStreamAdapter)
                    and stream.closed
                ):
                    log_event(
                        logger,
                        "tts_fanin.segment_setup_retry",
                        reason="closed_socket",
                        exc_type=type(e).__name__,
                        err=str(e),
                    )
                    stream = await self._current_outputd_stream()
                    if stream is None:
                        return
                    continue
                raise
        paced_sec = 0.0
        queued_end = self._ring_end_monotonic
        for chunk in _outputd_audio_chunks(stereo_i16.tobytes()):
            now = time.monotonic()
            if queued_end is None or queued_end < now:
                queued_end = now
            pace_excess = (queued_end - now) - _OUTPUTD_PACE_AHEAD_SEC
            if pace_excess > 0:
                await _pace_sleep(pace_excess)
                paced_sec += pace_excess
                self._paced_total_sec += pace_excess
            try:
                await asyncio.to_thread(stream.write, chunk)
            except OSError:
                if isinstance(stream, _OutputdStreamAdapter) and stream.closed:
                    log_event(
                        logger,
                        "tts_fanin.audio_write_failed",
                        reason="closed_socket",
                        level=logging.WARNING,
                    )
                raise
            queued_end += len(chunk) / (
                self._output_rate * _OUTPUTD_AUDIO_FRAME_BYTES
            )
        queued_at = time.monotonic()
        if self._ring_end_monotonic is None or queued_at > self._ring_end_monotonic:
            self._ring_end_monotonic = queued_at + chunk_duration_sec
        else:
            self._ring_end_monotonic += chunk_duration_sec
        # Exclude deliberate pacing sleeps so the warning keeps meaning
        # "the IPC itself is slow", not "the writer paced as designed".
        write_ms = (queued_at - write_start) * 1000 - paced_sec * 1000
        chunk_ms = chunk_duration_sec * 1000
        if write_ms > chunk_ms + 100:
            logger.warning(
                "fan-in TTS IPC write slow: %.0fms for %.0fms of audio "
                "(%d frames @ %d Hz)",
                write_ms, chunk_ms, len(mono_i16), self._output_rate,
            )

    def _profile_for_segment(self, segment_kind: str, *, source_profile=None):
        if source_profile is not None:
            return source_profile
        if (
            segment_kind == "chirp"
            or not (self._provider and self._model and self._voice)
        ):
            return None
        key = (self._provider, self._model, self._voice, self._profile_path)
        if self._profile_cache_key != key:
            self._profile_cache_key = key
            self._profile_cache = profile_for_outputd(
                self._provider,
                self._model,
                self._voice,
                path=self._profile_path,
            )
        return self._profile_cache

    async def end_segment(self) -> None:
        stream = self._stream
        if stream is None:
            await self._save_assistant_source_profile()
            return
        if isinstance(stream, _OutputdStreamAdapter) and stream.closed:
            await self._save_assistant_source_profile()
            return
        end = getattr(stream, "end_segment", None)
        if end is not None:
            try:
                await asyncio.to_thread(end)
            except OSError as e:
                logger.warning("fan-in TTS IPC segment end failed: %s", e)
        await self._save_assistant_source_profile()

    async def _save_assistant_source_profile(self) -> None:
        meter = self._assistant_meter
        self._assistant_meter = None
        if meter is None or not (self._provider and self._model and self._voice):
            return
        measurement = meter.finish()
        if measurement is None:
            return
        confidence = confidence_for_measurement(measurement)
        try:
            await asyncio.to_thread(
                update_profile_from_measurement,
                self._provider,
                self._model,
                self._voice,
                measurement,
                path=self._profile_path,
                method="passive_live",
                confidence=confidence,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("assistant loudness profile save failed: %s", e)
        else:
            self._profile_cache_key = None
            self._profile_cache = None

    async def flush(self) -> dict | None:
        stream = self._stream
        if stream is None:
            await self._save_assistant_source_profile()
            return None
        ack: dict | None = None
        try:
            flush_sync = getattr(stream, "flush_sync", None)
            if flush_sync is not None:
                ack = await asyncio.to_thread(flush_sync)
            else:
                await asyncio.to_thread(stream.abort)
                await asyncio.to_thread(stream.start)
        except Exception as e:  # noqa: BLE001
            logger.warning("fan-in TTS IPC flush failed: %s", e)
        self._ring_end_monotonic = None
        if ack is not None:
            log_event(
                logger,
                "tts_flush.ack",
                transport="fanin",
                ok=ack.get("ok"),
                segments=ack.get("segments"),
                flushed_frames=ack.get("flushed_frames"),
                max_audio_played_ms=ack.get("max_audio_played_ms"),
            )
        await self._save_assistant_source_profile()
        return ack

    async def __aexit__(self, *exc) -> None:
        if self._stream is not None:
            stream = self._stream
            self._stream = None
            close = getattr(stream, "close", None)
            if close is not None:
                await asyncio.to_thread(close)


def make_tts_playout(
    *,
    transport: str,
    device: str | int,
    output_rate: int,
    gain_db: float,
    drain_tail_sec: float,
    outputd_socket: str = FANIN_TTS_SOCKET,
    provider: str = "",
    model: str = "",
    voice: str = "",
    assistant_loudness_profile_path: str = ASSISTANT_LOUDNESS_PROFILE_PATH,
) -> TtsPlayout:
    """Construct the selected TTS playout transport.

    ``outputd`` is the supported runtime path. The old ``sounddevice``
    playout class remains for direct unit tests and pre-outputd archaeology,
    but this outputd-loudness tree must not silently route runtime voice audio
    through a fixed-gain PortAudio path.
    """
    if transport == "sounddevice":
        raise RuntimeError(
            "JASPER_TTS_TRANSPORT=sounddevice is not supported in this "
            "outputd-loudness tree; deploy a pre-outputd revision for "
            "that rollback path."
        )
    if transport == "outputd":
        return OutputdTtsPlayout(
            socket_path=outputd_socket,
            output_rate=output_rate,
            gain_db=gain_db,
            drain_tail_sec=drain_tail_sec,
            provider=provider,
            model=model,
            voice=voice,
            profile_path=assistant_loudness_profile_path,
        )
    raise ValueError(f"unknown TTS transport: {transport!r}")
