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
from contextlib import contextmanager
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import numpy as np

from .assistant_loudness import (
    AssistantSourceMeter,
    DEFAULT_PROFILE_PATH as ASSISTANT_LOUDNESS_PROFILE_PATH,
    confidence_for_measurement,
    profile_for_outputd,
    update_profile_from_measurement,
    upsample_2x,
)
from .assistant_volume import EffectiveVolumeContext
from .dsp_numpy import resample_poly
from .log_event import log_event
from .tts_routing import FANIN_TTS_SOCKET

# `sounddevice` is a Pi-side audio I/O dep (PortAudio bindings). It's not
# installed in the local dev venv and isn't needed by the pure-Python
# helpers in this module (parse_udp_device, UdpMicCapture, the dataclasses).
# Lazy-import inside the two places that actually open PortAudio streams
# (_log_audio_open_failure, MicCapture.__aenter__) so the module can be
# imported on a dev machine, hardware-free tests can parse it, and the
# lazy-import guards in test_lazy_imports.py can run.
# The annotation on MicCapture._stream uses `sd.InputStream`, but
# `from __future__ import annotations` above makes that a string —
# never evaluated.

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable

    import sounddevice as sd


class InputDeviceUnavailable(RuntimeError):
    """The primary microphone input could not be opened at startup.

    Raised by the voice daemon's leg factory when the must-have "on"
    wake leg's device won't open (absent card, PortAudio "No input
    device matching ...", busy capture, or a malformed/unbindable UDP
    transport). The daemon's ``main()`` catches it and exits
    ``VOICE_MIC_UNAVAILABLE_EXIT`` so systemd parks the unit cleanly
    instead of crash-looping toward ``StartLimitAction=reboot``."""

    def __init__(self, device: str, cause: BaseException | None = None) -> None:
        self.device = device
        detail = f": {type(cause).__name__}: {cause}" if cause is not None else ""
        super().__init__(
            f"primary microphone input {device!r} unavailable{detail}"
        )


def _log_audio_open_failure(role: str, device: str, exc: BaseException) -> None:
    """Dump environmental state when a sounddevice stream open fails.

    Called from MicCapture.__aenter__ immediately before re-raising
    on a real open failure. The bare exception (typically
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
    # "MicCapture" is the literal the capture caller passes — a "capture"
    # comparison here never matched and the cascade ran on absent mics too.
    if role == "MicCapture":
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
            # Polyphase resample with a built-in anti-alias filter, not
            # naive stride-decimation, which would alias voice content
            # above 8 kHz back into the audible band.
            resampled = resample_poly(ch0, up=1, down=self._decimation)
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
    """Shared TtsPlayout base: gain validation, rate bookkeeping, and
    drain-deadline timing. Playback (write/flush/segment lifecycle) is
    subclass-owned except emission admission, which `write_segment` owns
    here for every transport — `OutputdTtsPlayout` is the only one
    `make_tts_playout` can construct, since both it and `Config`'s
    `tts_transport` validation refuse anything else. The other methods
    below raise until a subclass overrides them; they stay declared so
    code typed on `TtsPlayout` (turn_playback.py, voice_daemon.py) still
    type-checks against every transport's interface.

    `output_rate` must be an exact multiple of `INPUT_RATE`: the output
    device may not natively support 24 kHz, so the subclass
    polyphase-upsamples 24 kHz → `output_rate` in its own write path.
    """

    INPUT_RATE = 24000

    # Floor — below this, TTS is effectively silent. Used when the
    # user mutes, when Camilla is unreachable at startup, or when a
    # volume reading looks malformed.
    MIN_TTS_GAIN_DB = -60.0

    def __init__(
        self,
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
        self._output_rate = output_rate
        self._upsample = output_rate // self.INPUT_RATE
        # Initial value is the floor (effectively silent) so the daemon
        # cannot accidentally play TTS loud during the brief window
        # between TtsPlayout construction and the first configured
        # gain. Until then we'd rather have inaudible TTS than blast.
        self._gain_db = self.MIN_TTS_GAIN_DB
        # Cumulative pacing-sleep time since the last take_paced_sec().
        # Only the outputd/fan-in transport paces (a device-paced transport
        # wouldn't need to), but the field lives here so every transport
        # answers take_paced_sec() and callers stay transport-agnostic.
        self._paced_total_sec = 0.0
        self._stream: _OutputdStreamAdapter | None = None
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
        # Emission-time admission authority — see set_emission_admission.
        self._emission_admission: "Callable[[], str | None] | None" = None
        self._emission_refusal_logged = False
        # Apply the constructor's gain_db through the same validation +
        # validation path as runtime updates. If a caller passes the
        # legacy "-8.0 fixed gain" value, this becomes the active level.
        self.set_gain_db(gain_db)

    def set_gain_db(self, db: float) -> None:
        """Update TTS gain. Non-finite inputs are rejected and very low
        finite values floor to the mute-equivalent minimum. Single-float
        assignment is atomic under the GIL, so no lock is needed for
        concurrent reads of `gain_db`."""
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
        raise NotImplementedError

    async def __aexit__(self, *exc) -> None:
        raise NotImplementedError

    def set_emission_admission(
        self,
        admission: "Callable[[], str | None] | None",
    ) -> None:
        """Install the authority asked before every write.

        `admission` returns a refusal code while assistant audio must not
        be heard at all (an armed room-correction window), else None. It is
        asked per write, not per episode, so a caller that passed an earlier
        check and is already mid-playout is refused too (issue #1913)."""
        self._emission_admission = admission

    async def write_segment(
        self,
        pcm: bytes,
        *,
        provider_item_id: str | None = None,
        segment_kind: str = "assistant",
        source_profile=None,
        pcm_wide: bool = False,
    ) -> None:
        """Sole emission seam: every assistant byte passes through here —
        cues, earcons, announcements and live-session TTS, directly or via
        `write`. Refused bytes are dropped, not queued, so drain accounting
        is untouched and an episode holding output still releases it."""
        admission = self._emission_admission
        refusal = admission() if admission is not None else None
        if refusal is not None:
            # Once per refusal streak: a burst-delivery provider hands over a
            # whole response as many chunks, and one line each would flood the
            # journal for the length of a held measurement session.
            if not self._emission_refusal_logged:
                self._emission_refusal_logged = True
                log_event(
                    logger,
                    "tts_write.refused",
                    reason=refusal,
                    segment_kind=segment_kind,
                )
            return
        self._emission_refusal_logged = False
        await self._write_segment(
            pcm,
            provider_item_id=provider_item_id,
            segment_kind=segment_kind,
            source_profile=source_profile,
            pcm_wide=pcm_wide,
        )

    async def _write_segment(
        self,
        pcm: bytes,
        *,
        provider_item_id: str | None = None,
        segment_kind: str = "assistant",
        source_profile=None,
        pcm_wide: bool = False,
    ) -> None:
        raise NotImplementedError

    async def end_segment(self) -> None:
        raise NotImplementedError

    async def prepare_assistant_context(
        self,
        *,
        provider: str,
        model: str,
        voice: str,
        tts_envelope_lufs: float,
        canonical_volume_db: float | None = None,
        downstream_volume_db: float | None = None,
        context_tts_envelope_lufs: float | None = None,
        muted: bool | None = None,
        context_stamp_boot_ns: int | None = None,
    ) -> None:
        raise NotImplementedError

    async def pause_content_meter(self) -> None:
        raise NotImplementedError

    async def resume_content_meter(self) -> None:
        raise NotImplementedError

    async def write(self, pcm: bytes) -> None:
        raise NotImplementedError

    async def flush(self) -> dict | None:
        raise NotImplementedError

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
        pending budget (always true for a device-paced transport, which
        never sleeps deliberately).
        """
        v = self._paced_total_sec
        self._paced_total_sec = 0.0
        return v


_OUTPUTD_AUDIO_FRAME_BYTES = 4  # stereo S16_LE — the narrow wire
_OUTPUTD_AUDIO_FRAME_BYTES_WIDE = 8  # stereo S32_LE — the wide wire
_OUTPUTD_SAMPLE_RATE = 48_000

# The exact i16 -> i32 spine-scale factor, 2^16. Named here because it is a
# CONTRACT with Rust, not a local convenience: it is the same power of two
# `jasper_resampler::widen_i16_to_i32` shifts by, so a wide payload and a narrow
# one describe the same signal at two scales and `narrow_i32_to_i16_round`
# inverts the promotion exactly. Pinned by tests/test_tts_wire_width.py.
_SPINE_SCALE = 65_536
_I32_MIN = -(2 ** 31)
_I32_MAX = 2 ** 31 - 1
_OUTPUTD_FLUSH_ACK_TIMEOUT_SEC = 3.0
# All IPC is local to the Pi. Healthy connects and control writes complete in
# milliseconds, while one second tolerates scheduler pressure without letting
# a dead owner or full Unix-socket buffer strand voice teardown indefinitely.
# Lock waits use the same ceiling: their owner is itself bounded by the socket
# timeout, and a timed-out waiter poisons the socket to wake that owner.
_OUTPUTD_IPC_CONNECT_TIMEOUT_SEC = 1.0
_OUTPUTD_IPC_IO_TIMEOUT_SEC = 1.0
_OUTPUTD_IPC_LOCK_TIMEOUT_SEC = 1.0
# MEASURE_PAUSE is a rare safety-control request, not an audio hot path. Its
# canonical adapter call runs synchronously so it cannot outlive the reply;
# 250 ms matches one IPC audio chunk and leaves ample room inside the daemon's
# aggregate pause budget without stalling the event loop for a full IPC second.
_OUTPUTD_MEASUREMENT_CONTROL_SLICE_SEC = 0.25
# Keep individual IPC messages well below the daemon's 2 MiB hard cap.
# 250 ms chunks make barge-in/flush sharper and set the granularity at
# which the writer's pacing (below) applies backpressure. Chunking alone
# applies none — the owner drops on overflow rather than blocking.
#
# This is a BYTE ceiling, and deliberately stays one on both wires: it bounds
# the allocation a single AUDIO/AUDIO32 command can ask the daemon for, and that
# bound must not double because a box declared a wider wire. So the duration it
# buys is wire-dependent — 250 ms of the narrow S16 wire, 125 ms of the wide S32
# one — while the memory it costs the daemon is the same either way. Barge-in
# granularity on a wide box is correspondingly finer, not coarser.
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


def _outputd_audio_chunks(data: bytes, frame_bytes: int = _OUTPUTD_AUDIO_FRAME_BYTES):
    """Split TTS IPC AUDIO payloads below the daemon's protocol cap.

    Rust rejects AUDIO chunks above 2 MiB before allocation. Cached cue
    WAVs are normally short, but dynamic spoken text can occasionally
    be long enough after 24 kHz mono -> 48 kHz stereo conversion to cross
    that limit. Chunking here keeps the protocol bounded without changing
    the public TtsPlayout.write contract.

    ``frame_bytes`` is the wire's stereo frame size — 4 on the narrow S16 wire,
    8 on the wide S32 one. The chunk ceiling stays a BYTE ceiling on both, which
    is the same bound the Rust parser applies, so a wide payload simply carries
    half the frames per chunk. Sizing by frames instead would double the bytes a
    single command asks the daemon to allocate.
    """
    if not data:
        return []
    if len(data) % frame_bytes != 0:
        raise ValueError("TTS IPC audio payload must contain whole stereo frames")
    chunk_size = _OUTPUTD_MAX_AUDIO_CHUNK_BYTES
    chunk_size -= chunk_size % frame_bytes
    if chunk_size <= 0:
        raise AssertionError("TTS IPC chunk size must hold at least one frame")
    for i in range(0, len(data), chunk_size):
        yield data[i:i + chunk_size]


def _quantize_to_wire(arr, *, wide: bool):
    """Quantize a resampled float array onto the box's assistant wire.

    ``arr`` is in i16 SAMPLE UNITS (the provider streams S16, and the resampler
    keeps that scale), regardless of which wire it is headed for. This is THE
    one place the assistant path leaves floating point.

    NARROW is preserved verbatim — ``np.clip(...).astype(np.int16)``, the same
    saturating truncate-toward-zero it has always been. Its bytes are a shipped
    contract; "the same thing but rounded" would be a different signal on every
    box in the fleet.

    WIDE scales to the i32 spine (``_SPINE_SCALE``, the exact 2^16 the Rust
    ``widen_i16_to_i32`` shifts by) and quantizes ROUND-TO-NEAREST saturating —
    the campaign's rule for a JTS-owned quantizer, and a new edge that does not
    inherit the narrow one's history.

    The scaling multiply runs in **float64**, and it is worth being exact about
    why, because the obvious reason is wrong. ``arr`` is float32 (the resampler
    is cast back to it), and multiplying it by 2^16 is EXACT in float32:
    a power of two changes only the exponent, so no mantissa bit moves and no
    precision is recovered by widening. The upcast buys two smaller things —
    ``np.rint`` and the clip compare against the i32 rails at a width that
    represents every i32 exactly, so the rounding decision and the saturation
    boundary are not themselves approximated — and it costs one temporary per
    chunk on a path that already allocates several. It is insurance on the
    quantizer's own arithmetic, not a wider signal. (The ``pcm_wide`` ingest
    below states the same power-of-two exactness for the inverse divide; the two
    should read alike, because they are the same fact.)

    What actually survives is therefore bounded by float32, and that is fine:
    resampling a 16-bit source produces values off the S16 grid, and float32's
    24-bit mantissa carries ~8 of those bits into the payload. The remaining 8
    bits of the i32 container sit below that mantissa and below the source's own
    resolution — the container is sized by the spine, not by a claim about the
    assistant's precision. Widening the RESAMPLE path is not proposed here: it
    would cost a real float64 pass over every chunk for bits the 16-bit source
    never had.
    """
    if wide:
        scaled = np.rint(arr.astype(np.float64) * _SPINE_SCALE)
        return np.clip(scaled, _I32_MIN, _I32_MAX).astype(np.int32)
    return np.clip(arr, -32768, 32767).astype(np.int16)


@lru_cache(maxsize=1)
def tts_wire_is_wide() -> bool:
    """Whether THIS BOX's assistant wire is wide (S32). Resolved ONCE per process.

    ONE RULE, TWO LANGUAGES. Delegates to
    :func:`jasper.fanin_coupling.assistant_wire_is_wide`, the Python mirror of
    the shared crate's ``TtsWireWidth::from_box_declaration`` that
    ``jasper-fanin``'s ``Config::program_wire_is_wide`` calls. Both halves of
    the box's declaration are required — the ``S32_LE`` wire format AND a
    coupling that leaves fan-in on the ring (an UNDECLARED one does, ADR-0100)
    — and both are read file-fresh, not from ``os.environ``: ``jasper-voice``
    never loaded ``fanin.env``, which is the stale-``os.environ`` class
    AGENTS.md canonizes.

    WHY A BAD TOKEN DOES NOT RAISE HERE. ``jasper-fanin`` already treats an
    unrecognized value as a config-class fault and parks at exit 78, and the
    doctor surfaces it. Re-raising in ``jasper-voice`` would take down the
    daemon that plays the failure cues, turning one operator typo into a silent
    speaker. So the fault is reported loudly here and resolved narrow — the
    conservative width, and the one every unarmed box uses.

    CACHED so the process has exactly ONE answer. Two callers ask — the playout
    (which quantizes provider TTS) and the daemon (which bakes earcons) — and a
    second file read between them could return a second answer, which is the
    drift this campaign exists to remove.

    WHAT BOUNDS THE STALENESS, stated as the THREE ways the answer can move
    rather than the one this used to name:

    * a COUPLING flip — ``coupling_reconcile``'s transition path ``try-restart``s
      ``jasper-voice`` when the verdict changes, so the process is replaced;
    * the RESOLVER'S DEFAULT moving (the ring wire's narrow→wide flip is one),
      which changes the answer with no coupling flip and no reconciler
      transition. Nothing in ``coupling_reconcile`` covers that — but such a move
      only ever arrives in a DEPLOY, and a deploy parks ``jasper-voice``
      (``park_audio_clients_for_core_graph_restart``) and restarts it through
      ``jasper-aec-reconcile``, so the cache is rebuilt in the same operation
      that moved the default;
    * an operator hand-editing ``JASPER_FANIN_RING_WIRE_FORMAT`` on a live box.
      That is NOT covered and never was: the documented per-box move is "set it,
      run the hardware reconciler, then arm", and the arm is what restarts the
      daemons. Until then this process keeps its old answer.

    A STALE ANSWER IS A WIDTH DISAGREEMENT, NEVER A LEVEL ERROR. The IPC verb is
    self-describing (``AUDIO`` vs ``AUDIO32``), so fan-in converts exactly
    whichever it receives and logs
    ``event=fanin.tts_wire_width_mismatch action=converted``; the failure
    direction is an unnecessary conversion and a warn, not a scale error. The
    ``except`` below resolves NARROW for the same reason — the conservative
    width every unarmed box uses.

    Tests reset it with ``tts_wire_is_wide.cache_clear()``;
    ``tests/conftest.py`` does it automatically around every test.
    """
    from .fanin_coupling import assistant_wire_is_wide

    try:
        return assistant_wire_is_wide()
    except (OSError, ValueError) as e:
        log_event(
            logger,
            "tts_wire.declaration_unreadable",
            resolved="S16_LE",
            exc_type=type(e).__name__,
            err=str(e),
            level=logging.WARNING,
        )
        return False


async def _send_outputd_audio_chunk(stream, chunk: bytes) -> bool:
    """Wait for one thread-backed write to reach a known outcome.

    ``asyncio.to_thread`` cancellation cannot stop a blocking ``sendall``.
    Defer cancellation until that worker returns so the caller can commit the
    accepted chunk to its physical-drain ledger before cancellation unwinds.
    Returns whether cancellation arrived while the worker was in flight.
    """

    write_task = asyncio.create_task(asyncio.to_thread(stream.write, chunk))
    cancelled = False
    current = asyncio.current_task()
    while not write_task.done():
        try:
            await asyncio.wait({write_task})
        except asyncio.CancelledError:
            cancelled = True
            if current is not None:
                current.uncancel()
    if write_task.cancelled():
        raise asyncio.CancelledError
    error = write_task.exception()
    if error is not None:
        if cancelled:
            raise asyncio.CancelledError from None
        raise error
    return cancelled


async def wait_tts_drained_owned(
    tts: Any,
    *,
    fallback_sec: float = 0.0,
) -> None:
    """Wait through the physical tail and defer repeated cancellation.

    Cue and feedback callers use this once PCM may have been accepted. A
    cancelled coroutine cannot revoke worker-thread socket writes, so the
    caller retains duck/output ownership until the real drain waiter finishes,
    then receives cancellation. ``fallback_sec`` supports legacy test/out-of-
    tree playout objects that predate ``wait_drained``.
    """

    async def _wait() -> None:
        wait_drained = getattr(tts, "wait_drained", None)
        if callable(wait_drained):
            await wait_drained()
        elif fallback_sec > 0.0:
            await asyncio.sleep(fallback_sec)

    drain = asyncio.create_task(_wait(), name="tts-physical-drain")
    deferred_cancel = False
    current = asyncio.current_task()
    while not drain.done():
        try:
            await asyncio.wait({drain})
        except asyncio.CancelledError:
            if current is None or current.cancelling() == 0:
                break
            deferred_cancel = True
            current.uncancel()
    if drain.cancelled():
        raise asyncio.CancelledError
    error = drain.exception()
    if error is not None:
        if deferred_cancel:
            raise asyncio.CancelledError from None
        raise error
    if deferred_cancel:
        raise asyncio.CancelledError


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

    def __init__(self, sock: socket.socket, *, wire_wide: bool = False) -> None:
        # The payload verb this connection speaks — the wire's own DECLARATION
        # of its sample width ("AUDIO" = S16LE, "AUDIO32" = S32LE at spine
        # scale). Fixed for the life of the connection because the box's wire is
        # fixed for the life of the daemon; see `TtsWireWidth` in
        # rust/jasper-tts-protocol/src/lib.rs for why the reader honours the
        # declaration rather than assuming one.
        self._audio_verb = "AUDIO32" if wire_wide else "AUDIO"
        self._sock = sock
        self._sock.settimeout(_OUTPUTD_IPC_IO_TIMEOUT_SEC)
        self._recv_buffer = bytearray()
        self._lock = threading.Lock()
        self._active_segment: tuple[str, str, tuple[str, ...] | None] | None = None
        self._closed = False
        self._timeout_logged = False

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
                    self._sendall_locked(b"SEGMENT_END\n")
                    self._active_segment = None
                self._sendall_locked(b"CLOSE\n")
        except OSError:
            pass
        self._poison(reason=None)

    def _poison(
        self,
        *,
        reason: str | None,
        timeout_sec: float | None = None,
    ) -> None:
        """Close from any thread and wake a blocked socket operation."""

        if self._closed:
            return
        self._closed = True
        self._active_segment = None
        self._recv_buffer.clear()
        try:
            # close() alone does not reliably wake a sendall blocked in a
            # different thread on every supported kernel. shutdown() does.
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        if reason is not None and not self._timeout_logged:
            self._timeout_logged = True
            if timeout_sec is None:
                timeout_sec = (
                    _OUTPUTD_IPC_LOCK_TIMEOUT_SEC
                    if reason == "lock"
                    else _OUTPUTD_IPC_IO_TIMEOUT_SEC
                )
            log_event(
                logger,
                "tts_fanin.adapter_timeout",
                phase=reason,
                timeout_sec=timeout_sec,
                action="socket_poisoned",
                level=logging.WARNING,
            )

    @staticmethod
    def _remaining_timeout(
        ceiling_sec: float,
        deadline_monotonic: float | None,
    ) -> float:
        if deadline_monotonic is None:
            return ceiling_sec
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0.0:
            raise TimeoutError("TTS IPC aggregate deadline expired")
        return min(ceiling_sec, remaining)

    @contextmanager
    def _bounded_lock(
        self,
        *,
        deadline_monotonic: float | None = None,
    ):
        try:
            timeout_sec = self._remaining_timeout(
                _OUTPUTD_IPC_LOCK_TIMEOUT_SEC,
                deadline_monotonic,
            )
        except TimeoutError:
            self._poison(reason="lock", timeout_sec=0.0)
            raise
        acquired = self._lock.acquire(timeout=timeout_sec)
        if not acquired:
            # Do not release a lock this waiter never acquired. Poisoning the
            # socket wakes the owning thread's bounded sendall; that owner
            # releases its own lock in its finally block.
            self._poison(reason="lock", timeout_sec=timeout_sec)
            raise TimeoutError(
                "TTS IPC adapter lock timed out after "
                f"{timeout_sec:.3f}s"
            )
        try:
            yield
        finally:
            self._lock.release()

    def _sendall_locked(
        self,
        data: bytes,
        *,
        deadline_monotonic: float | None = None,
    ) -> None:
        if self._closed:
            raise BrokenPipeError("TTS IPC socket is closed")
        try:
            timeout_sec = self._remaining_timeout(
                _OUTPUTD_IPC_IO_TIMEOUT_SEC,
                deadline_monotonic,
            )
        except TimeoutError:
            self._poison(reason="send", timeout_sec=0.0)
            raise
        try:
            # Every operation sets its own bound while holding the adapter
            # lock. A measurement command may carry a tighter aggregate
            # deadline than the ordinary one-second IPC ceiling; the next
            # operation resets the socket timeout from its own budget.
            self._sock.settimeout(timeout_sec)
            self._sock.sendall(data)
        except TimeoutError as e:
            self._poison(reason="send", timeout_sec=timeout_sec)
            raise TimeoutError(
                "TTS IPC send timed out after "
                f"{timeout_sec:.3f}s"
            ) from e
        except OSError:
            self._poison(reason=None)
            raise

    def set_gain_db(self, db: float) -> None:
        with self._bounded_lock():
            self._sendall_locked(f"GAIN {db:.3f}\n".encode("ascii"))

    def prepare_assistant(
        self,
        *,
        provider: str,
        model: str,
        voice: str,
        tts_envelope_lufs: float,
        volume_context: EffectiveVolumeContext | None = None,
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
        with self._bounded_lock():
            parts = [
                "PREPARE_ASSISTANT",
                provider,
                model,
                voice,
                f"{float(tts_envelope_lufs):.2f}",
            ]
            if volume_context is not None:
                parts.extend(
                    [
                        f"{volume_context.canonical_db:.3f}",
                        f"{volume_context.downstream_db:.3f}",
                        f"{volume_context.tts_envelope_lufs:.3f}",
                        "1" if volume_context.muted else "0",
                        str(int(volume_context.stamp_boot_ns)),
                    ]
                )
            self._sendall_locked((" ".join(parts) + "\n").encode("ascii"))

    def pause_content_meter(
        self,
        *,
        deadline_monotonic: float | None = None,
    ) -> None:
        with self._bounded_lock(deadline_monotonic=deadline_monotonic):
            self._sendall_locked(
                b"CONTENT_METER_PAUSE\n",
                deadline_monotonic=deadline_monotonic,
            )

    def resume_content_meter(self) -> None:
        with self._bounded_lock():
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
        with self._bounded_lock():
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
        with self._bounded_lock():
            if self._active_segment is None:
                return
            self._sendall_locked(b"SEGMENT_END\n")
            self._active_segment = None

    def write(self, data: bytes) -> None:
        with self._bounded_lock():
            self._sendall_locked(f"{self._audio_verb} {len(data)}\n".encode("ascii"))
            self._sendall_locked(data)

    def abort(self) -> None:
        self.flush_sync()

    def flush_sync(self) -> dict | None:
        with self._bounded_lock():
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
        # No-op: the stream stays open after FLUSH_SYNC. Satisfies the
        # abort()+start() shape OutputdTtsPlayout.flush falls back to
        # when a stream has no flush_sync.
        return None

    def close(self) -> None:
        if self._closed:
            return
        with self._bounded_lock():
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
        wire_wide: bool | None = None,
    ) -> None:
        if output_rate != _OUTPUTD_SAMPLE_RATE:
            raise RuntimeError(
                "fan-in TTS IPC transport requires 48 kHz stereo IPC; "
                f"got output_rate={output_rate}"
            )
        super().__init__(
            output_rate=output_rate,
            gain_db=gain_db,
            drain_tail_sec=drain_tail_sec,
        )
        self._socket_path = socket_path
        self._provider = provider
        self._model = model
        self._voice = voice
        self._profile_path = profile_path
        # Resolved ONCE, at construction: `jasper-voice` is restarted by every
        # deploy and by the wizards that could change this, and a per-write file
        # read would put an open() on the audio path. A coupling flip that
        # changes the answer restarts this daemon (`coupling_reconcile`), so the
        # window in which this value can be stale is bounded by that restart;
        # fan-in logs `event=fanin.tts_wire_width_mismatch` if a payload lands
        # inside it.
        self._wire_wide = tts_wire_is_wide() if wire_wide is None else wire_wide
        self._frame_bytes = (
            _OUTPUTD_AUDIO_FRAME_BYTES_WIDE
            if self._wire_wide
            else _OUTPUTD_AUDIO_FRAME_BYTES
        )
        # Item 4 (observability): a support read must be able to answer "what
        # width is this box speaking, and why" without journal archaeology or a
        # code read. One line, at construction, naming the resolved width AND
        # where it came from — a resolver answer or an explicit caller override.
        log_event(
            logger,
            "tts_wire.resolved",
            width="S32_LE" if self._wire_wide else "S16_LE",
            verb="AUDIO32" if self._wire_wide else "AUDIO",
            frame_bytes=self._frame_bytes,
            source="explicit" if wire_wide is not None else "box_declaration",
            socket=socket_path,
        )
        self._assistant_meter: AssistantSourceMeter | None = None
        self._profile_cache_key: tuple[str, str, str, str] | None = None
        self._profile_cache = None
        # Keeps references so scheduled profile-save tasks (see
        # _schedule_assistant_source_profile_save) aren't garbage-collected
        # mid-flight.
        self._profile_save_tasks: set[asyncio.Task] = set()
        # One publisher owns reconnect. Without this lock, simultaneous meter
        # and audio callers can each connect after the same poisoned adapter
        # and leave one live but unreachable socket behind.
        self._outputd_reconnect_lock = asyncio.Lock()

    async def _connect_stream_adapter(
        self,
    ) -> _OutputdStreamAdapter:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(_OUTPUTD_IPC_CONNECT_TIMEOUT_SEC)
        connect_task = asyncio.create_task(
            asyncio.to_thread(sock.connect, self._socket_path)
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(connect_task),
                timeout=_OUTPUTD_IPC_CONNECT_TIMEOUT_SEC,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
            # The thread is bounded by the socket timeout too, but it may
            # finish after this coroutine returns. Consume that late outcome.
            connect_task.add_done_callback(
                lambda task: None if task.cancelled() else task.exception()
            )
            if isinstance(e, asyncio.CancelledError):
                raise
            log_event(
                logger,
                "tts_fanin.connect_timeout",
                socket=self._socket_path,
                timeout_sec=_OUTPUTD_IPC_CONNECT_TIMEOUT_SEC,
                level=logging.WARNING,
            )
            raise TimeoutError(
                "TTS IPC connect timed out after "
                f"{_OUTPUTD_IPC_CONNECT_TIMEOUT_SEC:.1f}s"
            ) from None
        except Exception as e:  # noqa: BLE001
            sock.close()
            logger.error(
                "fan-in TTS IPC connect failed: socket=%s exc=%s: %s",
                self._socket_path, type(e).__name__, e,
            )
            raise
        stream = _OutputdStreamAdapter(sock, wire_wide=self._wire_wide)
        try:
            stream.set_gain_db(self.gain_db)
        except OSError:
            stream.close()
            raise
        logger.info("fan-in TTS IPC connected: socket=%s", self._socket_path)
        return stream

    async def __aenter__(self) -> "OutputdTtsPlayout":
        self._stream = await self._connect_stream_adapter()
        return self

    async def _current_outputd_stream(self):
        stream = self._stream
        if isinstance(stream, _OutputdStreamAdapter) and stream.closed:
            async with self._outputd_reconnect_lock:
                # Another waiter may have published the replacement while we
                # queued for the reconnect lock. Re-read inside ownership so
                # every caller shares that adapter and no loser socket exists.
                stream = self._stream
                if not (
                    isinstance(stream, _OutputdStreamAdapter)
                    and stream.closed
                ):
                    return stream
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
                self._stream = stream
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
        tts_envelope_lufs: float,
        canonical_volume_db: float | None = None,
        downstream_volume_db: float | None = None,
        context_tts_envelope_lufs: float | None = None,
        muted: bool | None = None,
        context_stamp_boot_ns: int | None = None,
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
                    "tts_envelope_lufs": tts_envelope_lufs,
                }
                if (
                    canonical_volume_db is not None
                    and downstream_volume_db is not None
                    and context_tts_envelope_lufs is not None
                    and muted is not None
                    and context_stamp_boot_ns is not None
                ):
                    prepare_kwargs["volume_context"] = EffectiveVolumeContext(
                        canonical_db=canonical_volume_db,
                        downstream_db=downstream_volume_db,
                        tts_envelope_lufs=context_tts_envelope_lufs,
                        muted=muted,
                        stamp_boot_ns=context_stamp_boot_ns,
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
                    and not isinstance(e, TimeoutError)
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

    async def pause_content_meter_for_measurement(
        self,
        deadline_monotonic: float,
    ) -> None:
        """Fail-closed meter pause that cannot outlive MEASURE_PAUSE.

        Do not reconnect here: isolation setup must prove the command landed
        on the canonical adapter it already owns. A poisoned/missing adapter
        rolls the window back; ordinary later access owns reconnection.
        """

        stream = self._stream
        if not isinstance(stream, _OutputdStreamAdapter) or stream.closed:
            raise OSError("canonical TTS IPC adapter unavailable")
        control_deadline = min(
            deadline_monotonic,
            time.monotonic() + _OUTPUTD_MEASUREMENT_CONTROL_SLICE_SEC,
        )
        # Deliberately synchronous: the bounded adapter critical section may
        # hold the event loop for at most 250 ms, and no worker can emit PAUSE
        # after this coroutine reports failure and voice reopens admission.
        stream.pause_content_meter(deadline_monotonic=control_deadline)

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
                    and not isinstance(e, TimeoutError)
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

    async def _write_segment(
        self,
        pcm: bytes,
        *,
        provider_item_id: str | None = None,
        segment_kind: str = "assistant",
        source_profile=None,
        pcm_wide: bool = False,
    ) -> None:
        """Send un-gained 48 kHz stereo PCM to the TTS IPC owner.

        Gain is sent as metadata and enforced by fan-in's final mix
        clamp. Drain accounting mirrors TtsPlayout.write so the voice
        daemon's turn-ending contract stays identical.

        ``pcm`` is 24 kHz mono. ``pcm_wide`` names its INPUT width, which is a
        per-caller fact rather than a per-box one: provider TTS is S16 from
        every supported API whatever this box's wire is, while a locally
        generated earcon is baked at the wire's own width (see
        ``jasper.voice.earcons._to_pcm32``). A wide input is normalized to i16
        sample units on the way in — an exact power-of-two divide — so
        everything downstream of this line is one code path at one scale.
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

        if pcm_wide:
            # /2^16 is exact in binary floating point (it changes the exponent
            # only), so this costs nothing beyond the float32 mantissa the
            # whole path already runs at.
            arr = np.frombuffer(pcm, dtype=np.int32).astype(np.float32)
            arr = arr / np.float32(_SPINE_SCALE)
        else:
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
            # __init__ pins output_rate to 48 kHz on this transport, so the
            # only ratio that reaches here is 2.
            arr = upsample_2x(arr).astype(np.float32, copy=False)
        mono = _quantize_to_wire(arr, wide=self._wire_wide)
        stereo = np.repeat(mono, 2)

        chunk_duration_sec = len(mono) / self._output_rate
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
                    and not isinstance(e, TimeoutError)
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
        for chunk in _outputd_audio_chunks(stereo.tobytes(), self._frame_bytes):
            now = time.monotonic()
            queued_end = self._ring_end_monotonic
            if queued_end is None or queued_end < now:
                queued_end = now
            pace_excess = (queued_end - now) - _OUTPUTD_PACE_AHEAD_SEC
            if pace_excess > 0:
                await _pace_sleep(pace_excess)
                paced_sec += pace_excess
                self._paced_total_sec += pace_excess
            try:
                cancelled = await _send_outputd_audio_chunk(stream, chunk)
            except OSError:
                if isinstance(stream, _OutputdStreamAdapter) and stream.closed:
                    log_event(
                        logger,
                        "tts_fanin.audio_write_failed",
                        reason="closed_socket",
                        level=logging.WARNING,
                    )
                raise
            # Commit only after this AUDIO command was accepted, and commit
            # every accepted command independently. A later command can fail
            # after this one is already queued at the IPC owner; deferring the
            # ledger until the whole write returns would then advertise idle
            # while that accepted prefix is still physically audible.
            sent_at = time.monotonic()
            committed_end = self._ring_end_monotonic
            if committed_end is None or committed_end < sent_at:
                committed_end = sent_at
            committed_end += len(chunk) / (self._output_rate * self._frame_bytes)
            self._ring_end_monotonic = committed_end
            if cancelled:
                raise asyncio.CancelledError
        queued_at = time.monotonic()
        # Exclude deliberate pacing sleeps so the warning keeps meaning
        # "the IPC itself is slow", not "the writer paced as designed".
        write_ms = (queued_at - write_start) * 1000 - paced_sec * 1000
        chunk_ms = chunk_duration_sec * 1000
        if write_ms > chunk_ms + 100:
            logger.warning(
                "fan-in TTS IPC write slow: %.0fms for %.0fms of audio "
                "(%d frames @ %d Hz)",
                write_ms, chunk_ms, len(mono), self._output_rate,
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
            self._schedule_assistant_source_profile_save()
            return
        if isinstance(stream, _OutputdStreamAdapter) and stream.closed:
            self._schedule_assistant_source_profile_save()
            return
        end = getattr(stream, "end_segment", None)
        if end is not None:
            try:
                await asyncio.to_thread(end)
            except OSError as e:
                logger.warning("fan-in TTS IPC segment end failed: %s", e)
        self._schedule_assistant_source_profile_save()

    def _pop_assistant_meter(self) -> AssistantSourceMeter | None:
        meter = self._assistant_meter
        self._assistant_meter = None
        return meter

    def _schedule_assistant_source_profile_save(self) -> None:
        """Run the profile save off the chirp's critical path.

        ``meter.finish()`` runs a pure-Python per-sample IIR filter twice
        over the reply audio (assistant_loudness.py's ``_biquad``); awaited
        inline here it blocked the loop for ~0.7s per second of reply,
        delaying the end-of-turn chirp. The meter is popped now, by value,
        so a segment that starts before this task gets to run cannot steal
        it from the segment that just ended.
        """
        meter = self._pop_assistant_meter()
        task = asyncio.create_task(self._save_assistant_source_profile(meter))
        self._profile_save_tasks.add(task)
        task.add_done_callback(self._profile_save_tasks.discard)

    async def _save_assistant_source_profile(
        self, meter: AssistantSourceMeter | None
    ) -> None:
        if meter is None or not (self._provider and self._model and self._voice):
            return
        measurement = await asyncio.to_thread(meter.finish)
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
            await self._save_assistant_source_profile(self._pop_assistant_meter())
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
        await self._save_assistant_source_profile(self._pop_assistant_meter())
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

    ``outputd`` is the only implementation `TtsPlayout` has: the base class
    is a typed contract (it owns emission admission; the rest raises until
    overridden) and `OutputdTtsPlayout` supplies the transport.
    ``sounddevice`` is refused here rather than accepted and routed
    nowhere.
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
