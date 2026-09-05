# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Wired measurement-mic capture: the Pi records its own excitation (#2662 W2b).

The ONE wired capture kernel: parameterized ALSA capture from a Pi-attached measurement-class
microphone, with the frame accounting and dropout scanning that make a wired take gradeable by
the same ladder as browser capture takes, and :func:`mint_wired_answer` — the single place a
recording becomes the capture seam's answer (audio + device identity + calibration reference +
integrity counters). Every wired take mints here: the wizard's plan walk, the engine's play
seam, and the ``jasper-null`` door. No web/session knowledge lives here; host policy (the
household calibration hint) arrives as the minter's ``host`` argument, never as an import.

**Device resolution is registry-anchored, probe-at-use.** :func:`resolve_wired_mic` matches
``/proc/asound/card*/usbid`` against :data:`jasper.audio_measurement.mic_identity.SUPPORTED_MODELS`
— the ONLY authority on "this USB device is a measurement microphone" (shared with the
reconciler's voice-candidate exclusion, #2703), so a voice array or USB DAC can never be
selected. Probed fresh at session prepare, no reconciler: presence is a per-session fact.

**CLOCK RULE** (inherited from :mod:`jasper.route_latency.mic_readers`): the mic is its own USB
clock master (ASYNC endpoint), drifting against both ``CLOCK_MONOTONIC`` and the DAC clock. The
reader takes a fresh ``time.monotonic_ns()`` after every blocking read rather than extrapolating
from a stream-start anchor; cross-clock drift within one capture is the analyzer's business, not
"corrected" here.

**Frame accounting mirrors the browser's, into the same wire keys** (the seam contract,
:mod:`jasper.active_speaker.crossover_v2.capture_source`): ``frames`` (ALSA-accumulated,
counted in the read loop), ``encoded_frames`` (counted INDEPENDENTLY at encode time so a
dropped frame unbalances the ledger instead of vanishing), ``block_gaps`` (EXACT discontinuity
count: overruns and zero-length reads), and ``block_gap_frames`` (an ESTIMATE derived from
per-chunk monotonic timestamps, floored at 1/event — an upper bound, but its bias can't change
a verdict since the ledger fails on ANY nonzero value).

**The zero-run scan is the browser's dropout detector, re-homed** (#2557: a capture-FIFO
dropout writes an unbroken run of >=128 exact digital zeros into a live room's noise floor,
13/13 glitch events vs 0/3 clean controls). :func:`scan_zero_runs` mirrors the page's
``scanZeroFillRuns`` with the same threshold and wire keys, minus a ``phase`` field (no render
grid on this chain) and scanning the SOURCE format (S32) before any width conversion.

**Format is preserved end-to-end** — the UMIK-2 descriptor is S32_LE only; the pipeline pins
the RATE, not the width, so a phone's 16-bit WAV and this module's 32-bit WAV both decode
correctly through :func:`decode_wav_to_mono`.

**Bounds.** ``hw:`` ALSA capture devices are exclusive-open, so a second recorder fails loudly
(EBUSY). ``max_capture_s`` bounds memory; hitting it is GRADED, not merely disclosed — it books
one discontinuity so the same render-gap check that fails an overrun fails a truncated take,
with ``truncated`` naming which class it was. Every wait is bounded: start fails loudly if the
first chunk never arrives, and a reader thread that will not join is an error, never a hang.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from jasper.audio_measurement.frame_ledger import (
    REPORT_KEY_ENCODED_FRAMES,
    REPORT_KEY_FRAMES,
    REPORT_KEY_RENDER_GAPS,
    REPORT_KEY_RENDER_GAP_FRAMES,
)
from jasper.log_event import log_event

__all__ = [
    "CODE_WIRED_MIC_MISSING",
    "WIRED_CAPTURE_CHAIN",
    "WIRED_POST_ROLL_S",
    "WIRED_PRE_PLAY_ALLOWANCE_S",
    "ZERO_RUN_MIN_SAMPLES",
    "ZERO_RUN_RECORD_CAP",
    "WiredCaptureAnswer",
    "WiredCaptureError",
    "WiredMicDevice",
    "WiredMicMissing",
    "WiredRecorder",
    "WiredRecording",
    "build_capture_integrity_report",
    "decode_wav_to_mono",
    "encode_wav_s32",
    "make_wired_recorder",
    "mint_wired_answer",
    "require_wired_mic",
    "resolve_wired_mic",
    "scan_zero_runs",
    "select_capture_channel",
]

logger = logging.getLogger(__name__)

#: Disclosure tag: WHICH capture chain produced these counters. The phone's report carries no
#: such tag (absence means the browser chain); a wired report names itself.
WIRED_CAPTURE_CHAIN = "alsa_s32le"

#: Same 128 the browser scan uses (one Web Audio render quantum, the #2557 signature) — no
#: render grid on the ALSA side, so this is the detector class's run-length threshold.
ZERO_RUN_MIN_SAMPLES = 128

#: Mirrors the page's ``ZERO_RUN_RECORD_CAP``. ``zero_run_count`` stays exact past the cap.
ZERO_RUN_RECORD_CAP = 8

# S32_LE interleaved: 4 bytes per sample per channel.
BYTES_PER_SAMPLE = 4

# How long start() waits for the first chunk. Generous relative to a ~21 ms period (1024
# frames at 48 kHz); same value as mic_readers.DEFAULT_UDP_READ_TIMEOUT_SECONDS.
START_TIMEOUT_S = 5.0

# Consecutive failed reads before the reader gives up. pyalsaaudio recovers an overrun
# internally and returns the negative once, so a chain this long means the device is gone.
MAX_CONSECUTIVE_READ_FAILURES = 8

#: Post-roll recorded after the play call returns. Derivation, evidence
#: named: the composed programs already END with 0.5 s of in-program tail
#: (``program.DEFAULT_MEASURE_TAIL_S`` / ``DEFAULT_VERIFY_TAIL_S``), and the
#: play call's return leads the acoustic end by the playback chain's
#: buffered depth — the same tens-to-hundreds-of-ms, device-dependent lead
#: ``PHASE_LADDER_START_SKEW_S`` (0.35 s) documents at the start edge. 1.0 s
#: covers that lead plus decay margin beyond the composed tail. A budget
#: allowance, not a measurement — the hardware smoke is where the real
#: play-return-to-silence interval gets measured.
WIRED_POST_ROLL_S = 1.0

#: Capture-budget allowance for everything BEFORE the program's first sample:
#: re-admission, the DSP writer lock, and the program-graph load all run
#: inside ``on_armed`` while the recorder is already rolling. The play seam's
#: own transport budget (``correction_setup._run_async``'s 60 s default)
#: bounds setup + program + restore together, so 20 s of setup allowance is
#: safely above the observed graph-load cost and safely inside that bound.
WIRED_PRE_PLAY_ALLOWANCE_S = 20.0

#: The structured code :class:`WiredMicMissing` carries to the journal and the
#: refused tap. Deliberately NOT a ``REASON_REGISTRY`` entry: that registry
#: holds PERSISTED terminal failures the envelope renders, and this refusal
#: fires before any durable state exists.
CODE_WIRED_MIC_MISSING = "wired_mic_missing"


class WiredCaptureError(RuntimeError):
    """Callers must treat this as a hard failure — never fall back to another source mid-session
    or synthesize samples (same rule as ``route_latency.mic_readers.MicSourceUnavailableError``).
    """


class WiredMicMissing(WiredCaptureError):
    """A session asked to measure and no mic answered.

    The disclosure as a TYPE, so the host and the suite name one thing rather
    than matching a sentence. Its message carries the way forward (plug a
    measurement mic in), because the owner ruling is disclose-and-recommend:
    this refuses to measure without a microphone, never to let the household
    measure.
    """

    code = CODE_WIRED_MIC_MISSING


@dataclass(frozen=True)
class WiredMicDevice:
    """One resolved measurement-class capture card, probe-time facts only."""

    #: ALSA card id (``/proc/asound/cardN/id``), e.g. ``UMIK2``.
    card_id: str
    card_index: int
    #: ``vvvv:pppp`` as the kernel wrote it (lowercased).
    usb_id: str
    #: The calibration-registry model this usb_id belongs to.
    model_key: str
    #: The registry's display label for that model.
    model_label: str

    @property
    def pcm(self) -> str:
        """No plug layer — ALSA can neither resample nor convert behind the accounting."""
        return f"hw:CARD={self.card_id},DEV=0"


def resolve_wired_mic(
    *, proc_asound: str | os.PathLike[str] = "/proc/asound",
) -> WiredMicDevice | None:
    """The first measurement-class capture card present, or ``None``.

    Requires a capture stream (``pcm0c`` present), so a playback-only device with a
    coincidental id can never resolve. Lowest card index wins when several match.

    Fail-soft to ``None`` on any probe error, including an unreadable or non-UTF-8 proc file.
    ``None`` means "no mic answered", never a source choice — the caller decides what to do.
    """
    from jasper.audio_measurement.mic_identity import SUPPORTED_MODELS

    # id -> model, DERIVED per call from the one registry owner.
    known: dict[str, str] = {}
    for registry_key, spec in SUPPORTED_MODELS.items():
        for declared in spec.get("usb_ids") or ():
            known[str(declared).strip().lower()] = registry_key
    if not known:
        return None
    root = Path(proc_asound)
    try:
        card_dirs = sorted(
            (int(item.name[4:]), item)
            for item in root.glob("card[0-9]*")
            if item.name[4:].isdigit()
        )
    except OSError:
        return None
    for card_index, card_dir in card_dirs:
        try:
            usb_id = (card_dir / "usbid").read_text().strip().lower()
        except (OSError, UnicodeDecodeError):
            continue  # not a USB card, or not a readable text file
        model_key = known.get(usb_id)
        if model_key is None:
            continue
        if not (card_dir / "pcm0c").exists():
            continue  # no capture stream — never a microphone
        try:
            card_id = (card_dir / "id").read_text().strip()
        except (OSError, UnicodeDecodeError):
            card_id = card_dir.name
        label = str(SUPPORTED_MODELS.get(model_key, {}).get("label") or model_key)
        return WiredMicDevice(
            card_id=card_id,
            card_index=card_index,
            usb_id=usb_id,
            model_key=model_key,
            model_label=label,
        )
    return None


def require_wired_mic(
    *, proc_asound: str | os.PathLike[str] = "/proc/asound",
) -> WiredMicDevice:
    """:func:`resolve_wired_mic`, plus the ONE refusal its callers share.

    Wired is THE acoustic-measurement path (ADR-0188), so absence is disclosed
    with the way forward rather than measured around. A front end that needs
    its own exit vocabulary catches :class:`WiredMicMissing` and maps it; none
    of them re-spells the sentence.
    """
    device = resolve_wired_mic(proc_asound=proc_asound)
    if device is None:
        raise WiredMicMissing(
            "no measurement microphone is plugged into the speaker — connect "
            "a registered measurement mic (e.g. miniDSP UMIK-2) and start "
            "again"
        )
    return device


class CapturePcm(Protocol):
    """``read()`` returns ``(frames, data)`` with pyalsaaudio semantics: negative signals an
    overrun (already re-prepared), zero signals an empty read."""

    def read(self) -> tuple[int, bytes]: ...

    def close(self) -> None: ...


def open_alsa_capture_pcm(
    device: str, *, sample_rate_hz: int, channels: int, period_frames: int,
) -> CapturePcm:
    """The production PCM: blocking ALSA capture, native S32_LE."""
    import alsaaudio  # lazy: ALSA-only dependency, capture path only

    try:
        return alsaaudio.PCM(
            type=alsaaudio.PCM_CAPTURE,
            mode=alsaaudio.PCM_NORMAL,
            device=device,
            rate=sample_rate_hz,
            channels=channels,
            format=alsaaudio.PCM_FORMAT_S32_LE,
            periodsize=period_frames,
        )
    except alsaaudio.ALSAAudioError as exc:
        raise WiredCaptureError(
            f"could not open {device} for wired capture "
            f"({sample_rate_hz} Hz, {channels}ch S32_LE): {exc}. Is the "
            "measurement microphone still plugged in, and is another "
            "process holding it?"
        ) from exc


@dataclass(frozen=True)
class WiredRecording:
    #: Raw interleaved S32_LE frames, in read order.
    chunks: tuple[bytes, ...]
    frames: int
    #: Exact count of capture discontinuities (overruns / empty reads).
    gap_count: int
    #: Monotonic-clock ESTIMATE of frames lost, floored at 1/event.
    gap_frames: int
    #: True when the byte budget stopped the reader before ``stop()`` did.
    truncated: bool
    sample_rate_hz: int
    channels: int


class WiredRecorder:
    """Lifecycle: ``start()`` -> caller plays the excitation -> ``finish(tail_s=...)`` (or
    ``abort()`` on failure). One instance is one capture. ``pcm_factory`` is the test seam."""

    def __init__(
        self,
        device: str,
        *,
        sample_rate_hz: int,
        channels: int,
        max_capture_s: float,
        period_frames: int = 1024,
        pcm_factory: Callable[[], CapturePcm] | None = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")
        if channels <= 0:
            raise ValueError("channels must be positive")
        if not (max_capture_s > 0):
            raise ValueError("max_capture_s must be positive")
        self._device = device
        self._sample_rate_hz = int(sample_rate_hz)
        self._channels = int(channels)
        self._max_frames = int(round(max_capture_s * sample_rate_hz))
        self._pcm_factory = pcm_factory or (
            lambda: open_alsa_capture_pcm(
                device,
                sample_rate_hz=self._sample_rate_hz,
                channels=self._channels,
                period_frames=period_frames,
            )
        )
        self._clock_ns = clock_ns
        self._stop = threading.Event()
        self._first_chunk = threading.Event()
        self._thread: threading.Thread | None = None
        self._pcm: CapturePcm | None = None
        self._chunks: list[bytes] = []
        self._frames = 0
        self._gap_count = 0
        self._gap_frames = 0
        self._truncated = False
        self._reader_error: WiredCaptureError | None = None

    # -- reader thread ------------------------------------------------------ #

    def _run_reader(self) -> None:
        assert self._pcm is not None
        frame_bytes = self._channels * BYTES_PER_SAMPLE
        rate = self._sample_rate_hz
        # CLOCK RULE: every loss estimate is fresh per-read timestamps, never extrapolated.
        last_read_ns = self._clock_ns()
        consecutive_failures = 0
        try:
            while not self._stop.is_set():
                try:
                    length, data = self._pcm.read()
                except (OSError, RuntimeError) as exc:
                    raise WiredCaptureError(
                        f"wired capture read failed on {self._device}: {exc}"
                    ) from exc
                now = self._clock_ns()
                if length <= 0:
                    # Overrun or empty read: one discontinuity, sized from the clock.
                    elapsed_s = max(0.0, (now - last_read_ns) / 1e9)
                    self._gap_count += 1
                    self._gap_frames += max(1, int(round(elapsed_s * rate)))
                    last_read_ns = now
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_READ_FAILURES:
                        raise WiredCaptureError(
                            f"wired capture on {self._device} failed "
                            f"{consecutive_failures} consecutive reads — "
                            "treating the microphone as gone"
                        )
                    continue
                consecutive_failures = 0
                last_read_ns = now
                self._chunks.append(data[: length * frame_bytes])
                self._frames += length
                self._first_chunk.set()
                if self._frames >= self._max_frames:
                    # Budget guard, not a normal stop — tripping this means the caller's
                    # play/tail schedule broke. BOOKED as a discontinuity (unknowable size,
                    # same >=1 floor as an overrun) so the render-gap check fails it too;
                    # ``truncated`` names which class it was.
                    self._truncated = True
                    self._gap_count += 1
                    self._gap_frames += 1
                    return
        except WiredCaptureError as exc:
            self._reader_error = exc
            # Wake a start() still waiting on the first chunk so it fails now, not at timeout.
            self._first_chunk.set()

    # -- caller side -------------------------------------------------------- #

    def start(self, *, ready_timeout_s: float = START_TIMEOUT_S) -> None:
        """Blocks until capture is confirmed live — the pre-roll guarantee: a caller that
        starts playback after this cannot emit excitation into a dead recorder. Raises
        :class:`WiredCaptureError` BEFORE any excitation has played."""
        if self._thread is not None:
            raise WiredCaptureError("recorder already started")
        self._pcm = self._pcm_factory()
        self._thread = threading.Thread(
            target=self._run_reader, name="wired-capture-reader", daemon=True
        )
        self._thread.start()
        if not self._first_chunk.wait(ready_timeout_s):
            self.abort()
            raise WiredCaptureError(
                f"no audio arrived from {self._device} within "
                f"{ready_timeout_s:g}s — the measurement microphone is not "
                "delivering samples"
            )
        if self._reader_error is not None:
            self.abort()
            raise self._reader_error

    def finish(self, *, tail_s: float) -> WiredRecording:
        """Record the post-roll tail, stop, and hand back the recording."""
        if self._thread is None:
            raise WiredCaptureError("recorder was never started")
        # Post-roll: a plain finish always grants the full tail; only abort() cuts it short.
        if tail_s > 0:
            self._stop.wait(tail_s)
        self._stop.set()
        self._join_and_close()
        if self._reader_error is not None:
            raise self._reader_error
        return WiredRecording(
            chunks=tuple(self._chunks),
            frames=self._frames,
            gap_count=self._gap_count,
            gap_frames=self._gap_frames,
            truncated=self._truncated,
            sample_rate_hz=self._sample_rate_hz,
            channels=self._channels,
        )

    def abort(self) -> None:
        """Stop and discard — safe on every failure path, idempotent."""
        self._stop.set()
        self._join_and_close()

    def _join_and_close(self) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            # A blocking period read returns within ~one period on a live device; still
            # alive after this join means a wedged kernel read.
            thread.join(timeout=START_TIMEOUT_S)
        pcm, self._pcm = self._pcm, None
        if pcm is not None:
            try:
                pcm.close()
            except (OSError, RuntimeError):
                pass
        if thread is not None and thread.is_alive():
            self._reader_error = self._reader_error or WiredCaptureError(
                f"wired capture reader on {self._device} did not stop"
            )


# -- post-processing (lazy numpy — nothing above needs it) ------------------ #


def _as_frames_array(recording: WiredRecording) -> Any:
    """``int32 [frames, channels]`` view of the recording's raw bytes."""
    import numpy as np

    raw = b"".join(recording.chunks)
    samples = np.frombuffer(raw, dtype="<i4")
    usable = (len(samples) // recording.channels) * recording.channels
    return samples[:usable].reshape(-1, recording.channels)


def select_capture_channel(recording: WiredRecording) -> tuple[int, Any, tuple[float, ...]]:
    """Pick the channel that actually carries the microphone.

    The UMIK-2 presents 2 channels around ONE physical capsule; which slot carries signal is a
    firmware fact this code must not assume. Selection is by energy: highest RMS wins, ties
    (including all-silent) resolve to channel 0 — a both-silent capture is then refused by the
    analyzer's own sweep-not-heard gate, so selection never manufactures a verdict.

    Returns ``(channel_index, mono_int32_array, per_channel_rms_dbfs)``.
    """
    import numpy as np

    frames = _as_frames_array(recording)
    if frames.size == 0:
        return 0, frames.reshape(0), (float("-inf"),) * recording.channels
    scale = float(np.iinfo(np.int32).max)
    rms_dbfs: list[float] = []
    for channel in range(recording.channels):
        column = frames[:, channel].astype(np.float64) / scale
        rms = float(np.sqrt(np.mean(np.square(column))))
        rms_dbfs.append(20.0 * np.log10(rms) if rms > 0 else float("-inf"))
    best = int(np.argmax(rms_dbfs)) if rms_dbfs else 0
    return best, np.ascontiguousarray(frames[:, best]), tuple(rms_dbfs)


def scan_zero_runs(mono_int32: Any) -> tuple[int, list[dict[str, int]]]:
    """Count runs of >=``ZERO_RUN_MIN_SAMPLES`` exact digital zeros — no epsilon, since a
    tolerance band would match quiet passages and manufacture false positives (#2557). Returns
    ``(count, runs)``, ``runs`` capped at :data:`ZERO_RUN_RECORD_CAP`; ``count`` stays exact."""
    import numpy as np

    samples = np.asarray(mono_int32)
    if samples.size == 0:
        return 0, []
    zero = samples == 0
    # Run boundaries via diff of the padded mask: +1 marks a start, -1 one-past-its-end.
    edges = np.diff(np.concatenate(([0], zero.view(np.int8), [0])))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1)
    count = 0
    runs: list[dict[str, int]] = []
    for start, end in zip(starts, ends):
        length = int(end - start)
        if length < ZERO_RUN_MIN_SAMPLES:
            continue
        count += 1
        if len(runs) < ZERO_RUN_RECORD_CAP:
            runs.append({"offset": int(start), "len": length})
    return count, runs


def decode_wav_to_mono(wav_bytes: bytes) -> tuple[Any, int]:
    """Decode a capture WAV to ``(float64 mono samples, sample_rate_hz)``. :func:`encode_wav_s32`'s
    inverse; width is read from the container, never assumed, so a phone's 16-bit file and this
    module's 32-bit file both decode correctly. Channel 0 of a multichannel file — an
    interleaved read would inflate the count the frame ledger checks."""
    import io

    import numpy as np
    from scipy.io import wavfile

    rate, data = wavfile.read(io.BytesIO(wav_bytes))
    if data.ndim > 1:
        data = data[:, 0]
    if np.issubdtype(data.dtype, np.integer):
        scale = float(np.iinfo(data.dtype).max)
        samples = data.astype(np.float64) / scale
    else:
        samples = data.astype(np.float64)
    return samples, int(rate)


def encode_wav_s32(mono_int32: Any, *, sample_rate_hz: int) -> tuple[bytes, int]:
    """Returns ``(wav_bytes, encoded_frames)`` with ``encoded_frames`` counted from the array
    actually written — the independent count the ledger compares against ``frames``."""
    import io
    import wave

    import numpy as np

    samples = np.ascontiguousarray(np.asarray(mono_int32, dtype="<i4"))
    encoded_frames = int(samples.shape[0])
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(BYTES_PER_SAMPLE)
        writer.setframerate(int(sample_rate_hz))
        writer.writeframes(samples.tobytes())
    return buf.getvalue(), encoded_frames


def build_capture_integrity_report(
    recording: WiredRecording,
    *,
    encoded_frames: int,
    zero_run_count: int,
    zero_runs: list[dict[str, int]],
) -> dict[str, Any]:
    """The four counters spelled through the frame ledger's own constants, plus the zero-run
    disclosure keys and chain tag. ``truncated`` is reported only when true (absent means
    nothing to disclose, never "checked and clean")."""
    report: dict[str, Any] = {
        REPORT_KEY_FRAMES: int(recording.frames),
        REPORT_KEY_ENCODED_FRAMES: int(encoded_frames),
        REPORT_KEY_RENDER_GAPS: int(recording.gap_count),
        REPORT_KEY_RENDER_GAP_FRAMES: int(recording.gap_frames),
        "zero_run_count": int(zero_run_count),
        "zero_runs": list(zero_runs),
        "zero_run_quantum": ZERO_RUN_MIN_SAMPLES,
        "capture_chain": WIRED_CAPTURE_CHAIN,
    }
    if recording.truncated:
        report["truncated"] = True
    return report


@dataclass(frozen=True)
class WiredCaptureAnswer:
    """The seam's :class:`CaptureAnswer`, minted by the wired source —
    exactly the contract's four fields, nothing more."""

    wav: bytes
    device: Mapping[str, Any] | None = None
    setup: Mapping[str, Any] | None = None
    capture_integrity: Mapping[str, Any] | None = None


def _wired_setup_reference(host: Any | None) -> Mapping[str, Any] | None:
    """The mic/cal identity REFERENCE the answer carries (seam contract).

    The wired analog of the phone's one-tap confirm: the household's
    remembered mic hint (``default_setup_calibration_for_v2`` — the same
    resolver that feeds the phone's prefill) becomes
    ``{"calibration": {"mode": "stored", calibration_id, model}}``, which the
    session's UNCHANGED resolver materializes — including the
    wrong-mic mismatch guard against this capture's reported device. No
    resolvable household record ⇒ ``None`` ⇒ the existing
    annotated-uncalibrated path (WARN, analysis still runs). Cal identity
    comes from the household record, never from USB serial — a real UMIK-2
    reports the generic "00000".

    ``host`` is ``None`` where no host is in reach — the CLI doors, which have
    no household session — and that lands on the same uncalibrated path.
    """
    if host is None:
        return None
    try:
        hint = host.default_setup_calibration_for_v2()
    except (OSError, RuntimeError, ValueError):
        log_event(
            logger,
            "correction.crossover_v2_wired_setup_hint_failed",
            level=logging.WARNING,
        )
        return None
    if hint is None or not getattr(hint, "resolvable", False):
        return None
    return {
        "calibration": {
            "mode": "stored",
            "calibration_id": str(hint.calibration_id),
            "model": str(hint.model),
        }
    }


def _json_safe_dbfs(values: tuple[float, ...]) -> list[float | None]:
    """Per-channel RMS for the device metadata: rounded, ``None`` for a
    silent channel (−inf is not JSON)."""
    import math

    return [
        round(value, 1) if math.isfinite(value) else None for value in values
    ]


def mint_wired_answer(
    recording: Any, *, device: WiredMicDevice, host: Any | None,
) -> WiredCaptureAnswer:
    """One recording as the seam's full answer — the ONE minter.

    Channel selection, the zero-run scan, the 32-bit encode, the integrity
    report in the frame ledger's wire spelling, the device identity, and the
    household's stored calibration reference. Every wired take — the plan
    walk's consume path, the play seam's capture half, the null door — mints
    here, because the analyzer grades whichever path delivered it.

    ``host`` is the late-bound web host (#2662) where there is one: the
    calibration hint is host policy, and resolving it at call time keeps a
    test double patched there honored from this side of the seam. It has no
    default — a door with no household session states ``host=None`` rather
    than omitting the calibration reference by accident.
    """
    channel, mono, rms_dbfs = select_capture_channel(recording)
    zero_count, zero_runs = scan_zero_runs(mono)
    wav, encoded_frames = encode_wav_s32(
        mono, sample_rate_hz=recording.sample_rate_hz
    )
    report = build_capture_integrity_report(
        recording,
        encoded_frames=encoded_frames,
        zero_run_count=zero_count,
        zero_runs=zero_runs,
    )
    device_meta = {
        "label": f"{device.model_label} ({device.card_id})",
        "wired": True,
        "card": device.card_id,
        "usb_id": device.usb_id,
        "model_key": device.model_key,
        "pcm": device.pcm,
        "channel_selected": channel,
        "channel_rms_dbfs": _json_safe_dbfs(rms_dbfs),
    }
    return WiredCaptureAnswer(
        wav=wav,
        device=device_meta,
        setup=_wired_setup_reference(host),
        capture_integrity=report,
    )


def make_wired_recorder(
    device: WiredMicDevice, *, sample_rate_hz: int, max_capture_s: float,
) -> WiredRecorder:
    """One recorder for this microphone, at the rate the program declares.

    The channel count is the one fact about a capture card that is neither on
    the device record nor derivable from the PCM name, so it is read from
    ``SUPPORTED_MODELS`` where a model declares ``capture_channels``. No model
    declares one today, so every supported mic opens at the UMIK-2's 2 — the
    lookup is what a mono measurement mic would be registered through.
    """
    from jasper.audio_measurement.mic_identity import SUPPORTED_MODELS

    channels = int(
        SUPPORTED_MODELS.get(device.model_key, {}).get("capture_channels", 2)
    )
    return WiredRecorder(
        device.pcm,
        sample_rate_hz=sample_rate_hz,
        channels=channels,
        max_capture_s=max_capture_s,
    )
