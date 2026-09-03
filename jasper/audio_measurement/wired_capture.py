# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Wired measurement-mic capture: the Pi records its own excitation (#2662 W2b).

The capture ENGINE behind the wired capture source
(:mod:`jasper.web.correction_crossover_v2_wired`): parameterized ALSA capture
from a Pi-attached measurement-class microphone (UMIK-class USB), with the
frame accounting and dropout scanning that make a wired take gradeable by the
same screening ladder that grades phone-relay takes. No web/session knowledge
lives here — this module records, counts, and encodes; the provider decides
when and what.

**Device resolution is registry-anchored, probe-at-use.**
:func:`resolve_wired_mic` matches ``/proc/asound/card*/usbid`` against the
``usb_ids`` the curated model registry declares
(:data:`jasper.audio_measurement.mic_identity.SUPPORTED_MODELS` — the same
identity vocabulary ``jasper-aec-reconcile`` uses to keep a measurement mic
OUT of the voice-input candidate set, #2703). The registry is the ONLY
authority on "this USB device is a measurement microphone", so a voice
array (XVF3800), a USB DAC, or any other capture card can never be
selected. Probed fresh at session prepare, no reconciler: the mic is
plugged in for a measurement and unplugged after, so presence is a
per-session fact, not steady state to converge on.

**CLOCK RULE (inherited from** :mod:`jasper.route_latency.mic_readers` **).**
The mic is its own USB clock master (the UMIK-2 capture endpoint is ASYNC), so
its sample clock drifts against ``CLOCK_MONOTONIC`` and against the DAC clock
the excitation plays on. The reader takes a fresh ``time.monotonic_ns()``
after every blocking read rather than extrapolating from a stream-start
anchor plus a sample count; the overrun-loss estimate below is derived from
those per-chunk timestamps. Cross-clock drift within one capture is the
analyzer's business — the deconvolution measures and handles it — and is not
"corrected" here.

**Frame accounting mirrors the browser's, into the same wire keys.** The
frame ledger (:mod:`jasper.audio_measurement.frame_ledger`) reconciles the
four counters the capture page reports; a wired capture counts its own chain
into the same keys (the seam contract —
:mod:`jasper.active_speaker.crossover_v2.capture_source`):

* ``frames`` — frames the reader accumulated from ALSA (the worklet-
  accumulated analog). Counted in the read loop.
* ``encoded_frames`` — frames actually encoded into the WAV. Counted
  INDEPENDENTLY at encode time, so a frame dropped between read and encode
  unbalances the ledger instead of vanishing.
* ``block_gaps`` — ALSA capture discontinuities: overruns (the ring
  overflowed and data was lost) and zero-length reads. The count is EXACT —
  each event is one discontinuity.
* ``block_gap_frames`` — frames those discontinuities account for. An
  ESTIMATE, and stated as one: ALSA does not report how many frames an
  overrun destroyed, so it is derived from the per-chunk monotonic
  timestamps (elapsed wall-clock across the gap × sample rate, minus the
  frames actually delivered in that window), floored at 1 per event. The
  estimate is an upper bound — the ring's still-buffered remainder is not
  subtracted — and its bias cannot change a verdict: the ledger's
  render-gap check fails on ANY nonzero value (no small amount of splice,
  frame_ledger's own doctrine), exactly as it does for a browser render gap.

Like the browser's hop-B loss, an overrun leaves ``frames`` ==
``encoded_frames`` == received while the capture is short of wall-clock —
which is why the gap counters are reported separately rather than folded in.

**The zero-run scan is the browser's dropout detector, re-homed.** Issue
#2557's verdict: a capture-FIFO dropout writes an unbroken run of ≥128 exact
digital zeros into a live room's noise floor (13/13 glitch events; 0/3 clean
controls, longest natural run 13 samples). ``capture-page/js/
capture-integrity.js`` (``scanZeroFillRuns``) scans the phone's assembled
buffer before encode; :func:`scan_zero_runs` scans the wired capture's
assembled int32 channel before encode, with the same ≥128-exact-zeros
threshold and the same wire keys (``zero_run_count`` / ``zero_runs`` /
``zero_run_quantum``), so one future grader reads both sources. Two wired
deltas, both stated: runs carry no ``phase`` field (there is no render grid
to phase against — 128 here is the detector class's run-length threshold,
not a grid modulus), and exact zero means integer sample value 0 in the
SOURCE format (S32), scanned before any width conversion so a conversion can
neither create nor hide a run.

**Format is preserved end-to-end — no dither/truncation decision to make.**
The UMIK-2 capture descriptor is S32_LE only; the engine records native
S32_LE and :func:`encode_wav_s32` writes 32-bit integer PCM. The pipeline
pins the RATE (``crossover_v2.sweep_spec.REQUIRED_SAMPLE_RATE_HZ``), not the
width: the analyze seam decodes format from the container
(``scipy.io.wavfile`` reads int32 and normalizes by the dtype max — see
:func:`decode_wav_to_mono` below, this module's own inverse), and
the phone's 16-bit WAV is its encoder's choice, not a contract. Keeping the
source width means the one place precision could be spent is a place it
isn't.

**Bounds.** One capture at a time is enforced by the hardware — ``hw:`` ALSA
capture devices are exclusive-open, so a second recorder fails loudly with
EBUSY. Memory is bounded by ``max_capture_s`` (the caller sizes it from the
plan entry's declared duration plus named allowances); the reader stops
filling at the budget, and the truncation is GRADED, not merely disclosed —
it books one discontinuity (floor 1 frame, the unmeasurable-loss rule
above), so the same render-gap check that fails an overrun fails the
truncated take, with ``truncated`` on the report naming which discontinuity
class it was. Every wait is bounded: start fails loudly if the first chunk
does not arrive, and a reader thread that will not join is an error, never a
hang.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from jasper.audio_measurement.frame_ledger import (
    REPORT_KEY_ENCODED_FRAMES,
    REPORT_KEY_FRAMES,
    REPORT_KEY_RENDER_GAPS,
    REPORT_KEY_RENDER_GAP_FRAMES,
)

__all__ = [
    "WIRED_CAPTURE_CHAIN",
    "ZERO_RUN_MIN_SAMPLES",
    "ZERO_RUN_RECORD_CAP",
    "WiredCaptureError",
    "WiredMicDevice",
    "WiredRecorder",
    "WiredRecording",
    "build_capture_integrity_report",
    "decode_wav_to_mono",
    "encode_wav_s32",
    "resolve_wired_mic",
    "scan_zero_runs",
    "select_capture_channel",
]

#: Disclosure tag carried in the integrity report: WHICH capture chain
#: produced these counters. The phone's report carries no such tag (absence
#: means the browser chain); a wired report names itself so forensics never
#: have to infer the chain from the counters' shape.
WIRED_CAPTURE_CHAIN = "alsa_s32le"

#: Minimum run of exact digital zeros counted as a dropout — the same 128 the
#: browser scan uses (one Web Audio render quantum, the #2557 signature).
#: There is no render grid on the ALSA side; this is the detector class's
#: run-length threshold, kept identical so one grader covers both sources.
ZERO_RUN_MIN_SAMPLES = 128

#: Recorded-run cap, mirroring the page's ``ZERO_RUN_RECORD_CAP``: a mic that
#: died mid-sweep must not grow the report without limit. ``zero_run_count``
#: stays exact past the cap.
ZERO_RUN_RECORD_CAP = 8

# S32_LE interleaved: 4 bytes per sample per channel.
BYTES_PER_SAMPLE = 4

# How long start() waits for the first chunk before declaring the device
# dead. Generous relative to a ~21 ms period (1024 frames at 48 kHz) so a
# scheduling hiccup cannot misclassify a live mic, bounded so a wedged
# device fails before any excitation plays — the same posture
# jasper.route_latency.mic_readers.DEFAULT_UDP_READ_TIMEOUT_SECONDS takes,
# and the same value.
START_TIMEOUT_S = 5.0

# Consecutive failed reads (overrun / zero-length) before the reader gives
# up. pyalsaaudio recovers an overrun internally (snd_pcm_prepare) and
# returns the negative once, so a healthy stream never chains failures;
# a chain this long means the device is gone, and the honest move is a loud
# error rather than a capture that is mostly gaps.
MAX_CONSECUTIVE_READ_FAILURES = 8


class WiredCaptureError(RuntimeError):
    """The wired capture chain failed loudly (device absent, dead, or lost).

    Callers must treat this as a hard failure — never fall back to another
    source mid-session or synthesize samples (the
    ``route_latency.mic_readers.MicSourceUnavailableError`` rule, same
    reason).
    """


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
        """The exact hw PCM the recorder opens — no plug layer, so ALSA can
        neither resample nor convert behind the accounting."""
        return f"hw:CARD={self.card_id},DEV=0"


def resolve_wired_mic(
    *, proc_asound: str | os.PathLike[str] = "/proc/asound",
) -> WiredMicDevice | None:
    """The first measurement-class capture card present, or ``None``.

    Matches ``/proc/asound/card<N>/usbid`` against the ``usb_ids`` declared
    by :data:`jasper.audio_measurement.mic_identity.SUPPORTED_MODELS` (the
    stdlib-only registry leaf — one owner for "is this hardware a
    measurement microphone?", shared with the reconciler's voice-candidate
    exclusion, #2703) and requires a capture stream (``pcm0c`` present), so
    a playback-only device with a coincidental id can never resolve. Lowest
    card index wins when several match — deterministic, and the multi-mic
    case is not a real household shape.

    Fail-soft to ``None`` on any probe error — including an unreadable or
    non-UTF-8 proc file (the ring_assets precedent for reads of
    kernel-owned text). ``None`` means "no mic answered", never a source
    choice: what a caller does with it is the caller's (the v2 flow
    discloses and refuses to measure — ``resolve_v2_wired_mic``).
    """
    from jasper.audio_measurement.mic_identity import SUPPORTED_MODELS

    # id → model, DERIVED per call from the one registry owner (never a
    # second declaration; ``measurement_mic_usb_ids`` flattens the same
    # field for the reconciler, which needs no model attribution).
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


class CapturePcm(Protocol):
    """What the recorder needs from a capture PCM (the injectable seam).

    ``read()`` returns ``(frames, data)`` with pyalsaaudio semantics: a
    negative frame count signals an overrun (data was lost; the binding has
    already re-prepared the stream), zero signals an empty read.
    """

    def read(self) -> tuple[int, bytes]: ...

    def close(self) -> None: ...


def open_alsa_capture_pcm(
    device: str, *, sample_rate_hz: int, channels: int, period_frames: int,
) -> CapturePcm:
    """The production PCM: blocking ALSA capture, native S32_LE.

    ``alsaaudio`` is imported lazily so importing this module never requires
    ALSA (the ``AlsaMicReader`` pattern) — only actually recording does.
    """
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
    """One finished wired capture, exactly as the reader accounted for it."""

    #: Raw interleaved S32_LE frames, in read order.
    chunks: tuple[bytes, ...]
    #: Frames accumulated across reads (the ``frames`` counter's source).
    frames: int
    #: Exact count of capture discontinuities (overruns / empty reads).
    gap_count: int
    #: Monotonic-clock estimate of frames those discontinuities lost
    #: (module docstring: exact count, estimated size, floored at 1/event).
    gap_frames: int
    #: True when the byte budget stopped the reader before ``stop()`` did.
    truncated: bool
    sample_rate_hz: int
    channels: int


class WiredRecorder:
    """Record one bounded wired capture on a background reader thread.

    Lifecycle: ``start()`` (opens the PCM, confirms the first chunk arrived)
    → the caller plays the excitation → ``finish(tail_s=...)`` (records the
    post-roll tail, joins, returns the :class:`WiredRecording`) — or
    ``abort()`` on any failure path. One instance is one capture; the hw
    device's exclusive-open enforces one live capture per mic.

    ``pcm_factory`` is the test seam: production omits it and gets the real
    ALSA PCM.
    """

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
        # CLOCK RULE anchor: every loss estimate is derived from fresh
        # per-read monotonic timestamps, never from a sample-count
        # extrapolation (module docstring).
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
                    # Overrun (negative, pyalsaaudio has re-prepared) or an
                    # empty read: one discontinuity, sized from the clock.
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
                    # Budget guard, not a normal stop: the caller's play/tail
                    # schedule should always finish first, so tripping this
                    # means the schedule broke. A truncated take is a splice
                    # by another name — everything after the stop is missing
                    # — so it is BOOKED as a discontinuity and graded by the
                    # same render-gap check that fails an overrun (gate fix
                    # round S4: graded, never a bare disclosure). The size is
                    # unknowable (the loss is the whole un-recorded tail), so
                    # it books the same ≥1 floor an unmeasurable overrun
                    # gets: any nonzero fails identically. ``truncated``
                    # stays on the report as the disclosure naming WHICH
                    # discontinuity class this was.
                    self._truncated = True
                    self._gap_count += 1
                    self._gap_frames += 1
                    return
        except WiredCaptureError as exc:
            self._reader_error = exc
            # Wake a start() that is still waiting on the first chunk so it
            # fails loudly now instead of at its timeout.
            self._first_chunk.set()

    # -- caller side -------------------------------------------------------- #

    def start(self, *, ready_timeout_s: float = START_TIMEOUT_S) -> None:
        """Open the device and block until capture is confirmed live.

        Returning means at least one real chunk has been read — the
        pre-roll guarantee: a caller that starts playback after this cannot
        emit excitation into a dead recorder. Raises
        :class:`WiredCaptureError` (device unopenable, or no audio within
        ``ready_timeout_s``) BEFORE any excitation has played.
        """
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
        # Post-roll: keep reading for the tail. stop() interrupts early only
        # via abort(); a plain finish always grants the full tail.
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
            # Bounded: a blocking period read returns within ~one period on a
            # live device and errors on a dead one; a thread still alive after
            # this is a wedged kernel read, which the close below unblocks or
            # the loud error reports.
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

    The UMIK-2 presents 2 channels (FL FR) around ONE physical capsule;
    which slot carries signal is a firmware fact this code must not assume
    (the analyzer's own multichannel rule — take channel 0 — would silently
    analyze silence if the capsule sat on channel 1). Selection is by
    energy: highest RMS wins, ties (including all-silent) resolve to
    channel 0, and the per-channel RMS figures ride the answer's device
    metadata so the choice is auditable. A both-silent capture selects 0 and
    is then refused by the analyzer's own sweep-not-heard gate — selection
    never manufactures a verdict.

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
    """Count runs of ≥``ZERO_RUN_MIN_SAMPLES`` exact digital zeros.

    The browser's ``scanZeroFillRuns`` re-homed (module docstring): exact
    zeros only — no epsilon, because a tolerance band would match quiet
    passages and manufacture false positives (#2557's ruling, #1765's banked
    lesson). Returns ``(count, runs)`` where ``runs`` is the first
    :data:`ZERO_RUN_RECORD_CAP` as ``{"offset", "len"}`` (no ``phase`` — no
    render grid exists on this chain); ``count`` stays exact past the cap.
    """
    import numpy as np

    samples = np.asarray(mono_int32)
    if samples.size == 0:
        return 0, []
    zero = samples == 0
    # Run boundaries via the diff of the padded mask: +1 marks a run start,
    # -1 one-past-its-end.
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
    """Decode a capture WAV to ``(float64 mono samples, sample_rate_hz)``.

    :func:`encode_wav_s32`'s inverse, and its neighbour so the two halves of
    the WAV round trip have one owner. Width is read from the container, never
    assumed: the pipeline pins the RATE, not the sample width, so a phone's
    16-bit file and this module's 32-bit file both arrive here correctly.

    Channel 0 of a multichannel file, because the analysis compares FRAMES —
    an interleaved read would inflate the count the frame ledger checks.
    """
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
    """Encode the selected channel as a 32-bit PCM mono WAV.

    Returns ``(wav_bytes, encoded_frames)`` with ``encoded_frames`` counted
    from the array actually written — the independent count the ledger
    compares against the reader's ``frames`` (module docstring).
    """
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
    """The wired capture's per-take account, in the ledger's wire spelling.

    The four counters are the seam's ``INTEGRITY_COUNTER_KEYS`` — spelled
    through the frame ledger's own constants, never re-quoted — plus the
    zero-run disclosure keys the page also sends, plus the chain tag.
    ``truncated`` is reported only when true (the page's convention: absent
    means nothing to disclose, never "checked and clean").
    """
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
