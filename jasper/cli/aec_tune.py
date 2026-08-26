# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Calibrate XVF3800 AEC bulk delay — `jasper-aec-tune`.

Round-trip latency from the host writing reference audio to the
XVF chip until that same audio comes back into the chip's mics
(via dongle → amp → speakers → air → mic) is variable per
install. The chip's adaptive filter only handles ±40 samples of
residual delta after AUDIO_MGR_SYS_DELAY compensation. Get this
wrong and the AEC fails to converge, residual echo stays loud,
and wake-word fires on the speaker's own playback.

Two modes:

  PASSIVE (default, safe). Records both the reference signal
  (jasper-outputd's final speaker-monitor UDP datagrams) and the XVF
  mic for ~5 seconds, then cross-correlates. NO test signal injected
  — uses whatever you're already playing. Requires music or other
  audio to be audible during the test. Volume is not modified.

  ACTIVE (`--inject-noise`). Plays a brief, low-level noise burst
  through the correction lane resolved by `correction_play_device()`.
  Volume is RELATIVELY ducked from the current
  level by `--duck-by` dB (default 20 dB quieter); the code refuses
  to ever raise the volume above the current setting. Use only
  when nothing is playing.

Procedure (passive mode):

  1. Read the current `main_volume` so we can sanity-check that
     audio is actually flowing.
  2. Stop whichever managed services currently own or consume the XVF
     capture endpoint (jasper-voice and, on supported profiles,
     jasper-aec-bridge) for the duration. Stopping the bridge is also
     what frees the reference monitor port this tool then binds.
  3. For 5 seconds, capture from BOTH:
        - jasper-outputd's final speaker-reference UDP monitor
          (see "Where the reference is tapped" below)
        - the detected supported XVF card, device 0 (the processed
          mic — what the chip actually hears from the room)
  4. Cross-correlate (200-3400 Hz bandpass to focus on speech-band
     echo). Lag in samples = AUDIO_MGR_SYS_DELAY.
  5. Restore every service that was active and print the diagnostic candidate.

Where the reference is tapped, and what the number is comparable to
-------------------------------------------------------------------

The reference leg reads jasper-outputd's final speaker monitor —
headerless little-endian interleaved stereo int16 datagrams, one
playout period each, at outputd's fixed 48 kHz core rate. The target
is `JASPER_OUTPUTD_REFERENCE_UDP_TARGET` (shipped default
`127.0.0.1:9891`), which is also the address `jasper-aec-bridge`
binds in production, so this tool and production AEC see the exact
same reference stream.

That tap point is deliberate. `AUDIO_MGR_SYS_DELAY` is defined
against what the chip receives on its USB-IN reference, and outputd
publishes the UDP monitor and the chip-reference writer from ONE
narrowed period — pinned Rust-side by
`both_reference_taps_consume_one_narrowing_of_the_same_period`. So
the reference sampled here is co-located, at outputd's publish point,
with the chip's own reference.

Before U4/P7-2 this leg read `pcm.jasper_capture`, the aloop dsnoop
tap on jasper-fanin's summed output — a point UPSTREAM of CamillaDSP
and of outputd. Numbers printed by that older tool are NOT comparable
with numbers printed by this one: the old tap observed the same audio
earlier, so its lag carried an extra CamillaDSP-plus-outputd offset
that was never part of the quantity being estimated. Treat any
recorded pre-P7-2 reading as archaeology and re-run rather than
compare.

No persisted artifact is affected. The alignment artifact that gates
chip-AEC arming (`/var/lib/jasper/chip-aec-alignment.json`) is written
ONLY by `jasper-aec-commission`, which measures on its own capture path
(the mic card direct, against a known commissioning stimulus and
outputd's own STATUS reference queue) and never consults this tool's
measurement — the two share only two CamillaDSP volume helpers, imported
the other way. A commission measures fresh every time it runs; nothing
here changes what an existing artifact means or how `jasper-aec-init`
verifies it.

Honest limit, unchanged by P7-2: the two legs are started
independently, so whatever start skew exists between the socket and
the mic capture is added straight onto the reported lag. And the UDP
monitor is outputd's final ELECTRICAL reference, not the XVF USB-IN
chip-reference PCM — the chip leg diverges downstream of the shared
period (16 kHz downsample, then USB transport), so this measurement
does not prove chip-ref writer or chip USB-IN timing. And the wire
carries no sequence number, so a dropped or late datagram is an
undetectable splice in the reference — it surfaces only as lowered
correlation confidence, alongside the received-duration line this tool
logs after the capture. All of these are why this stays a diagnostic
and why `--apply` is volatile.

The command is diagnostic-only by default. `--apply` performs one
explicit, volatile write after checking confidence, the firmware's
confirmed -64..256 sample range, USB presence, and readback. The next
`jasper-aec-init` run (including an AEC reconcile or reboot) overwrites
that value from the profile-owned `JASPER_AEC_*_CHIP_SYS_DELAY` setting;
this tool never persists configuration and never calls the XVF brick-risk
SAVE_CONFIGURATION or REBOOT commands.

Run from the Pi with `jasper-outputd` up — it is the reference
producer, and this tool stops `jasper-aec-bridge` for the duration
rather than needing it running. Idempotent — re-run any time room
layout changes.

Usage:
    sudo /opt/jasper/.venv/bin/jasper-aec-tune
    sudo /opt/jasper/.venv/bin/jasper-aec-tune --inject-noise --duck-by 20
    sudo /opt/jasper/.venv/bin/jasper-aec-tune --apply
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Awaitable, Callable

import numpy as np

from jasper.active_speaker.volume_latch import (
    READBACK_TOLERANCE_DB,
    fader_matches,
)
from jasper.audio_measurement.correction_lane import popen_correction_play
from jasper.camilla import CamillaController, CamillaUnavailable, primary_controller
from jasper.env_load import merged_env_files
from jasper.mics import xvf3800

logger = logging.getLogger("jasper.aec_tune")

TEST_DURATION_SEC = 5
SAMPLE_RATE = 16000  # XVF internal AEC rate
# jasper-outputd's core is fixed at 48 kHz stereo (`rust/jasper-outputd`
# `types.rs`: SAMPLE_RATE/CHANNELS, and `Config::from_env` refuses any other
# rate), and it publishes the reference as one narrowed S16 period per
# datagram. Both halves of this tool's reference contract read from here.
REFERENCE_RATE = 48000
REFERENCE_CHANNELS = 2
# Where jasper-outputd publishes that monitor. The reconciler is the single
# writer of the key and leaves it EMPTY on its parked branches, which is the
# difference between "outputd is not publishing" and "nobody was playing".
REFERENCE_UDP_TARGET_ENV = "JASPER_OUTPUTD_REFERENCE_UDP_TARGET"
DEFAULT_REFERENCE_UDP_TARGET = "127.0.0.1:9891"
REFERENCE_RECV_BYTES = 65536
REFERENCE_POLL_SEC = 0.5
NOISE_AMPLITUDE_FS = 0.02  # 2% FS = ~ -34 dBFS — quiet even before ducking
MIN_APPLY_CONFIDENCE = 0.001
MIN_SYS_DELAY = -64
MAX_SYS_DELAY = 256
PROCESS_EXIT_GRACE_SEC = 3.0
SYSTEMCTL_TIMEOUT_SEC = 10.0
AUDIO_CONTROL_ERRORS = (
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    TimeoutError,
    subprocess.SubprocessError,
)
SUBPROCESS_CLEANUP_ERRORS = (OSError, subprocess.SubprocessError)

# Stop the consumer before its producer, then restore in reverse order. In
# profile-managed XVF modes the bridge owns the hardware capture endpoint and
# voice consumes its UDP output. In direct-mic mode the bridge is inactive and
# voice itself is the owner. Tracking both active units covers either topology
# without trying to re-derive reconciler policy here.
CAPTURE_OWNER_STOP_ORDER: tuple[tuple[str, str], ...] = (
    ("jasper-voice.service", "voice capture consumer"),
    ("jasper-aec-bridge.service", "XVF capture owner"),
)


class CamillaVolumeError(RuntimeError):
    """Raised when active-mode volume cannot be changed and verified safely."""


class TuneError(RuntimeError):
    """Raised when a diagnostic cannot produce a trustworthy candidate."""


def _positive_channel_count(value: str) -> int:
    try:
        channel_count = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if channel_count <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return channel_count


def _positive_finite_db(value: str) -> float:
    try:
        db = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(db) or db <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return db


def _generate_noise(duration_s: float, rate_hz: int, amplitude: float) -> np.ndarray:
    """Stereo low-amplitude white noise as int16. Defaults to ~ -34 dBFS,
    which combined with at-least-20-dB ducking lands ~ -54 dBFS at the
    DAC. Quiet — closer to room tone than music."""
    n = int(duration_s * rate_hz)
    rng = np.random.default_rng(seed=0)
    mono = (rng.standard_normal(n) * amplitude * 32767).astype(np.int16)
    return np.stack([mono, mono], axis=1)


def _write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(samples.shape[1] if samples.ndim == 2 else 1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())


def _read_wav_int16(path: Path) -> tuple[np.ndarray, int, int]:
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        n = w.getnframes()
        raw = w.readframes(n)
    arr = np.frombuffer(raw, dtype=np.int16)
    if channels > 1:
        arr = arr.reshape(-1, channels)
    return arr, rate, channels


def _select_mic_channel(
    samples: np.ndarray, recorded_channels: int, channel_index: int
) -> np.ndarray:
    """Select one channel using the recorded WAV header as authority."""
    if channel_index < 0 or channel_index >= recorded_channels:
        if recorded_channels == 1:
            raise ValueError(
                f"mic channel {channel_index} is invalid for the recorded mono WAV; "
                "only channel 0 is available"
            )
        raise ValueError(
            f"mic channel {channel_index} is invalid for the recorded "
            f"{recorded_channels}-channel WAV; choose 0 through "
            f"{recorded_channels - 1}"
        )
    if recorded_channels == 1:
        if samples.ndim != 1:
            raise ValueError("recorded mono WAV has an inconsistent sample layout")
        return samples.astype(np.float32)
    if samples.ndim != 2 or samples.shape[1] != recorded_channels:
        raise ValueError(
            f"recorded {recorded_channels}-channel WAV has an inconsistent "
            "sample layout"
        )
    return samples[:, channel_index].astype(np.float32)


async def _with_controller(
    work: Callable[[CamillaController], Awaitable[float | None]],
) -> float | None:
    """Run one fader operation on a short-lived primary controller.

    ``CamillaController`` is where the tree's ONE fader door lives, and it
    brings its own bounds: a 2 s socket timeout per exchange, a 5 s composite
    attempt budget that aborts a wedged websocket, and one transparent retry.
    A CLI-local SIGALRM watchdog used to supply the first of those and none of
    the rest, so this both closes the bypass and deletes the weaker guard
    rather than keeping two.
    """
    controller = primary_controller()
    try:
        return await work(controller)
    except CamillaUnavailable as exc:
        raise CamillaVolumeError(f"CamillaDSP is unreachable: {exc}") from exc
    finally:
        await controller.close()


def _camilla_get_volume() -> float:
    async def read(controller: CamillaController) -> float | None:
        return await controller.get_volume_db()

    volume = asyncio.run(_with_controller(read))
    if volume is None or not math.isfinite(volume):
        raise CamillaVolumeError(f"Camilla main_volume is not finite: {volume!r}")
    return float(volume)


def _camilla_set_volume(db: float) -> None:
    """Declare the main fader's level through the tree's one owner.

    ``CamillaController.set_volume_db`` already ran every write through
    ``_coerce_main_volume_db``, so this path has seen the 0 dB ceiling since
    the two hardware doors became one. W18's remainder was the OWNER: this
    module still wrote the fader beside the arbiter rather than through it.

    A DECLARATION rather than a claim, and the reason is this process's shape.
    ``main()`` brackets a duck and a restore across two separate
    ``asyncio.run`` calls, so a claim held between them would outlive the loop
    it was taken on. Nothing else writes the fader in a one-shot CLI, so there
    is nothing to arbitrate against — what routing buys here is the single
    door and its ``best_effort`` contract, not ranking.

    Still fail-closed on the readback, and the clamp makes that stricter
    rather than looser — a request above the ceiling lands at the ceiling and
    the confirm then refuses, instead of the caller believing a level the
    speaker never played.
    """
    if not math.isfinite(db):
        raise CamillaVolumeError(f"refusing non-finite Camilla volume: {db!r}")

    from jasper.volume_owner import volume_owner

    owner = volume_owner()
    if owner is None:
        raise CamillaVolumeError(
            "the speaker volume owner is not registered in this process"
        )

    async def read_back(controller: CamillaController) -> float | None:
        return await controller.get_volume_db()

    async def write() -> float | None:
        # The write is the owner's; the readback stays a direct read, because
        # a read is not a writer and this function's contract is to name the
        # exact number it saw.
        await owner.declare_household_level_db(db)
        return await _with_controller(read_back)

    actual = asyncio.run(write())
    if actual is None or not math.isfinite(actual):
        raise CamillaVolumeError(
            f"Camilla volume readback is not finite after setting {db:.2f} dB"
        )
    if not fader_matches(actual, db, tolerance_db=READBACK_TOLERANCE_DB):
        raise CamillaVolumeError(
            f"Camilla volume readback mismatch: wrote {db:.2f} dB, read {actual:.2f} dB"
        )


def _correlate_and_find_lag(
    mic: np.ndarray, ref: np.ndarray, max_lag_samples: int = 4000
) -> tuple[int, float]:
    """Return (lag, peak_normalized) where lag is in samples at SAMPLE_RATE
    (positive = mic delayed relative to ref) and peak_normalized is the
    correlation peak height in [0,1] — confidence indicator."""
    from scipy.signal import butter, correlate, sosfiltfilt

    sos = butter(4, [200, 3400], btype="band", fs=SAMPLE_RATE, output="sos")
    mic_f = sosfiltfilt(sos, mic).astype(np.float32)
    ref_f = sosfiltfilt(sos, ref).astype(np.float32)
    full = correlate(mic_f, ref_f, mode="full")
    center = len(ref_f) - 1
    lo = max(0, center - max_lag_samples)
    hi = min(len(full), center + max_lag_samples)
    window = full[lo:hi]
    abs_window = np.abs(window)
    peak_in_window = int(np.argmax(abs_window))
    lag = (lo + peak_in_window) - center
    # Normalize by autocorrelation peaks
    mic_energy = float(np.sqrt(np.sum(mic_f * mic_f)))
    ref_energy = float(np.sqrt(np.sum(ref_f * ref_f)))
    if mic_energy * ref_energy == 0:
        return int(lag), 0.0
    peak_normalized = float(abs_window[peak_in_window] / (mic_energy * ref_energy))
    return int(lag), peak_normalized


def _service_is_active(unit: str) -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", unit],
        capture_output=True,
        text=True,
        timeout=SYSTEMCTL_TIMEOUT_SEC,
    )
    state = result.stdout.strip()
    if state == "active":
        return True
    if state in {"inactive", "failed", "unknown"}:
        return False
    raise RuntimeError(
        f"could not determine {unit} state: rc={result.returncode} state={state!r}"
    )


def _stop_service(unit: str, label: str) -> None:
    logger.info("stopping %s to free %s", unit, label)
    result = subprocess.run(
        ["systemctl", "stop", unit],
        check=False,
        timeout=SYSTEMCTL_TIMEOUT_SEC,
    )
    if result.returncode != 0:
        raise RuntimeError(f"failed to stop {unit}: rc={result.returncode}")
    if _service_is_active(unit):
        raise RuntimeError(f"failed to stop {unit}: unit remains active")


def _start_service(unit: str) -> None:
    logger.info("starting %s", unit)
    result = subprocess.run(
        ["systemctl", "start", unit],
        check=False,
        timeout=SYSTEMCTL_TIMEOUT_SEC,
    )
    if result.returncode != 0:
        raise RuntimeError(f"failed to start {unit}: rc={result.returncode}")
    if not _service_is_active(unit):
        raise RuntimeError(f"failed to start {unit}: unit is not active")


def _terminate_and_reap(proc: subprocess.Popen | None, label: str) -> None:
    """Bounded best-effort cleanup for an owned audio subprocess."""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=PROCESS_EXIT_GRACE_SEC)
        return
    except subprocess.TimeoutExpired:
        logger.warning("%s did not terminate; killing it", label)
    except SUBPROCESS_CLEANUP_ERRORS as exc:
        logger.warning("failed to terminate %s cleanly: %s", label, exc)
    try:
        proc.kill()
    except SUBPROCESS_CLEANUP_ERRORS as exc:
        logger.warning("failed to kill %s: %s", label, exc)
    try:
        proc.wait(timeout=PROCESS_EXIT_GRACE_SEC)
    except SUBPROCESS_CLEANUP_ERRORS as exc:
        logger.error("failed to reap %s: %s", label, exc)


def _wait_for_audio_process(
    proc: subprocess.Popen,
    label: str,
    timeout_sec: float,
) -> bool:
    try:
        return proc.wait(timeout=timeout_sec) == 0
    except subprocess.TimeoutExpired:
        logger.error("%s exceeded %.1fs timeout", label, timeout_sec)
        return False


def _resolve_reference_udp_target() -> tuple[str, int]:
    """Resolve where jasper-outputd publishes its final speaker reference.

    An explicit shell value wins (CLI-style override), then the merged env
    files this box actually runs on — `jasper-aec-reconcile` writes the key
    there, so reading `os.environ` alone would make an unarmed box look armed
    and turn a precise diagnosis into a silent five-second wait.
    """
    raw = os.environ.get(REFERENCE_UDP_TARGET_ENV)
    if raw is None:
        raw = merged_env_files().get(REFERENCE_UDP_TARGET_ENV)
    if raw is None:
        raw = DEFAULT_REFERENCE_UDP_TARGET
    raw = raw.strip()
    if not raw:
        raise TuneError(
            f"{REFERENCE_UDP_TARGET_ENV} is empty, so jasper-outputd is not "
            "publishing a speaker reference and there is nothing to correlate "
            "against. Run `sudo systemctl start jasper-aec-reconcile` and "
            "check that the AEC profile is not parked."
        )
    host, separator, port_text = raw.rpartition(":")
    if not separator or not host:
        raise TuneError(f"{REFERENCE_UDP_TARGET_ENV}={raw!r} is not HOST:PORT")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise TuneError(
            f"{REFERENCE_UDP_TARGET_ENV}={raw!r} has a non-numeric port"
        ) from exc
    if not 1 <= port <= 65535:
        raise TuneError(f"{REFERENCE_UDP_TARGET_ENV}={raw!r} port is out of range")
    return host, port


def _open_reference_socket(host: str, port: int) -> socket.socket:
    """Bind outputd's reference monitor, failing with the actionable reason."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((host, port))
    except OSError as exc:
        sock.close()
        raise TuneError(
            f"cannot bind jasper-outputd's reference monitor at {host}:{port}: "
            f"{exc}. jasper-aec-bridge owns this port while it runs — this tool "
            "stops the bridge first, so a bind failure here means something "
            "else is holding the port."
        ) from exc
    sock.settimeout(REFERENCE_POLL_SEC)
    return sock


def _drain_reference_socket(
    sock: socket.socket,
    deadline: float,
    out: list[bytes],
    stop: threading.Event,
) -> None:
    """Collect reference datagrams until `deadline` or `stop`. Never raises.

    Runs on its own thread while the mic `arecord` child records, started
    first so the reference is already collecting when the mic opens.

    `stop` is how the caller ends this early — on Ctrl-C, or when the mic child
    never started — instead of making a foreground operator CLI sit out the
    whole capture window before it can report. Signalling rather than closing
    the socket under a blocked `recvfrom` is the AEC bridge's own idiom
    (`_shutdown.is_set()`), and it avoids reading a reused fd: the very next
    thing this tool opens is the reference WAV. `OSError` still ends the loop
    quietly, so a close from the caller remains safe.
    """
    while not stop.is_set() and time.monotonic() < deadline:
        try:
            data, _addr = sock.recvfrom(REFERENCE_RECV_BYTES)
        except socket.timeout:
            continue
        except OSError:
            return
        if data:
            out.append(data)


def _write_reference_wav(ref_wav: Path, chunks: list[bytes]) -> None:
    """Render collected datagrams as the 48 kHz stereo S16 WAV analysis reads.

    The wire is headerless little-endian interleaved stereo int16, so the
    payloads concatenate directly. Trim to a whole number of stereo frames
    first: a short datagram would otherwise raise out of `frombuffer` or
    `reshape` rather than costing one frame.
    """
    raw = b"".join(chunks)
    frame_bytes = REFERENCE_CHANNELS * 2
    usable = len(raw) - (len(raw) % frame_bytes)
    samples = np.frombuffer(raw[:usable], dtype="<i2")
    _write_wav(ref_wav, samples.reshape(-1, REFERENCE_CHANNELS), REFERENCE_RATE)


def _capture_simultaneous(
    duration_sec: float,
    ref_wav: Path,
    mic_wav: Path,
    mic_device: str,
    mic_channels: int,
) -> bool:
    """Capture both legs, with bounded cleanup for every child-start outcome.

    The reference comes from jasper-outputd's final speaker-monitor UDP feed —
    the same stream production AEC consumes — and the mic from a bounded
    `arecord` child. See the module docstring for why the tap sits there.
    """
    host, port = _resolve_reference_udp_target()
    capture_sec = int(duration_sec) + 1
    capture_timeout = capture_sec + PROCESS_EXIT_GRACE_SEC
    ref_chunks: list[bytes] = []
    mic_proc: subprocess.Popen | None = None

    # Bind before the mic starts, so the reference is already collecting when
    # the mic opens and a port conflict is reported before anything else has
    # been disturbed. This orders the two starts; it does not align them — see
    # the module docstring on start skew.
    sock = _open_reference_socket(host, port)
    logger.info(
        "reference: jasper-outputd speaker monitor at %s:%d "
        "(%d Hz stereo S16, one playout period per datagram)",
        host,
        port,
        REFERENCE_RATE,
    )
    stop_reference = threading.Event()
    ref_thread = threading.Thread(
        target=_drain_reference_socket,
        args=(sock, time.monotonic() + capture_sec, ref_chunks, stop_reference),
        name="aec-tune-reference",
        daemon=True,
    )
    ref_thread.start()
    try:
        mic_proc = subprocess.Popen(
            [
                "arecord",
                "-q",
                "-D",
                mic_device,
                "-d",
                str(capture_sec),
                "-f",
                "S16_LE",
                "-r",
                str(SAMPLE_RATE),
                "-c",
                str(mic_channels),
                str(mic_wav),
            ],
        )
        mic_ok = _wait_for_audio_process(
            mic_proc, "microphone arecord", capture_timeout
        )
    finally:
        # The mic leg is over one way or another, so the reference window is
        # too: on the normal path the thread has already hit its own deadline,
        # and on an interrupt or a failed mic start this is what stops it
        # promptly instead of waiting the window out.
        stop_reference.set()
        _terminate_and_reap(mic_proc, "microphone arecord")
        ref_thread.join(timeout=capture_timeout)
        sock.close()

    if not ref_chunks:
        raise TuneError(
            f"no reference datagrams arrived on {host}:{port} in {capture_sec}s. "
            "jasper-outputd publishes this monitor only while it is running "
            "with a reference target armed — check `systemctl status "
            "jasper-outputd` and run `sudo systemctl start jasper-aec-reconcile`."
        )
    _write_reference_wav(ref_wav, ref_chunks)

    # Name how much of the window actually arrived. A dropped or late datagram
    # is an invisible splice — the wire carries no sequence number — and shows
    # up only as lowered correlation confidence. This says whether the
    # reference was short, so a weak result has a cause instead of a shrug.
    received_sec = (
        sum(len(chunk) for chunk in ref_chunks)
        / (REFERENCE_RATE * REFERENCE_CHANNELS * 2)
    )
    logger.info(
        "reference: received %.2fs of a %ds window in %d datagrams",
        received_sec,
        capture_sec,
        len(ref_chunks),
    )

    files_ok = (
        ref_wav.exists()
        and ref_wav.stat().st_size > 1024
        and mic_wav.exists()
        and mic_wav.stat().st_size > 1024
    )
    return mic_ok and files_ok


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Calibrate XVF3800 AUDIO_MGR_SYS_DELAY via cross-correlation"
    )
    mic_profile = xvf3800.detect_runtime_profile()
    default_mic_device = f"hw:CARD={mic_profile.alsa_card_name},DEV=0"
    default_mic_channels = mic_profile.capture_channels or 2
    parser.add_argument(
        "--mic-device",
        default=default_mic_device,
        help=f"ALSA capture device for XVF (default: {default_mic_device})",
    )
    parser.add_argument(
        "--mic-channels",
        type=_positive_channel_count,
        default=default_mic_channels,
        help="XVF capture channel count. Stock 2-ch firmware: "
        "0=conference (post-AEC+BF), 1=ASR. 6-ch firmware: also "
        f"raw mics on 2-5. (default: {default_mic_channels})",
    )
    parser.add_argument(
        "--mic-channel",
        type=int,
        default=0,
        help="Channel index to correlate. 0=conference works on both "
        "firmwares; switch to 2 (raw mic 0) on 6-ch for cleaner echo. "
        "(default: 0)",
    )
    parser.add_argument(
        "--inject-noise",
        action="store_true",
        help="Play a brief, quiet white-noise burst during the test. "
        "Use only when nothing is otherwise playing — passive mode is "
        "preferred.",
    )
    parser.add_argument(
        "--duck-by",
        type=_positive_finite_db,
        default=20.0,
        help="When --inject-noise is set, duck main_volume by THIS MANY "
        "DB BELOW THE CURRENT LEVEL during the test (default: 20 dB "
        "quieter). The code never raises the volume.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Explicitly write the validated candidate to the chip for this "
        "runtime only. The next AEC reconcile/init or reboot overwrites it.",
    )
    return parser


def _analyze_capture(
    ref_wav: Path,
    mic_wav: Path,
    mic_channel: int,
) -> tuple[int, float]:
    ref48_arr, ref_rate, _ref_channels = _read_wav_int16(ref_wav)
    if ref_rate != REFERENCE_RATE:
        raise TuneError(f"ref captured at {ref_rate} Hz, expected {REFERENCE_RATE}")
    mic_arr, mic_rate, mic_channels = _read_wav_int16(mic_wav)
    if mic_rate != SAMPLE_RATE:
        raise TuneError(f"mic captured at {mic_rate} Hz, expected {SAMPLE_RATE}")
    try:
        mic_mono = _select_mic_channel(mic_arr, mic_channels, mic_channel)
    except ValueError as exc:
        raise TuneError(str(exc)) from exc

    from scipy.signal import resample_poly

    if ref48_arr.ndim == 2:
        ref_mono48 = ref48_arr[:, 0].astype(np.float32)
    else:
        ref_mono48 = ref48_arr.astype(np.float32)
    ref_mono16 = resample_poly(ref_mono48, up=1, down=REFERENCE_RATE // SAMPLE_RATE)

    ref_rms = float(np.sqrt(np.mean(ref_mono16 * ref_mono16)))
    mic_rms = float(np.sqrt(np.mean(mic_mono * mic_mono)))
    logger.info("RMS — reference: %.1f, mic: %.1f", ref_rms, mic_rms)
    if not math.isfinite(ref_rms) or ref_rms < 50:
        raise TuneError(
            f"reference signal RMS {ref_rms:.1f} is invalid or near zero — "
            "play music and re-run, or use --inject-noise"
        )
    if not math.isfinite(mic_rms) or mic_rms < 50:
        logger.warning(
            "mic RMS %.1f is invalid or near zero — chip mic signal is "
            "silent; AEC may already be canceling perfectly, or mic is muted",
            mic_rms,
        )

    lag, confidence = _correlate_and_find_lag(mic_mono, ref_mono16)
    logger.info(
        "cross-correlation: lag=%d samples (%.2f ms) confidence=%.4f",
        lag,
        lag * 1000.0 / SAMPLE_RATE,
        confidence,
    )
    if not math.isfinite(confidence) or confidence < MIN_APPLY_CONFIDENCE:
        logger.warning(
            "correlation confidence %.5f is not sufficient for --apply; "
            "re-run with louder/different audio",
            confidence,
        )
    return lag, confidence


def _apply_volatile_delay(lag: int, confidence: float) -> bool:
    if not math.isfinite(confidence) or confidence < MIN_APPLY_CONFIDENCE:
        logger.error(
            "refusing --apply: confidence %.5f must be finite and >= %.5f",
            confidence,
            MIN_APPLY_CONFIDENCE,
        )
        return False
    if not MIN_SYS_DELAY <= lag <= MAX_SYS_DELAY:
        logger.error(
            "refusing --apply: lag %d is outside the firmware-confirmed "
            "AUDIO_MGR_SYS_DELAY range [%d, %d]",
            lag,
            MIN_SYS_DELAY,
            MAX_SYS_DELAY,
        )
        return False

    try:
        from ..xvf import xvf_host

        dev = xvf_host.find()
    except AUDIO_CONTROL_ERRORS as exc:
        logger.error("XVF3800 control unavailable; volatile apply failed: %s", exc)
        return False
    if dev is None:
        logger.error("XVF3800 not on USB; volatile apply was not attempted")
        return False
    try:
        try:
            prior = tuple(dev.read("AUDIO_MGR_SYS_DELAY"))
            if len(prior) != 1 or not isinstance(prior[0], int):
                raise ValueError(f"invalid prior value {prior!r}")
        except AUDIO_CONTROL_ERRORS as exc:
            logger.error(
                "cannot read prior AUDIO_MGR_SYS_DELAY; no write attempted: %s",
                exc,
            )
            return False

        try:
            dev.write("AUDIO_MGR_SYS_DELAY", [lag])
            actual = tuple(dev.read("AUDIO_MGR_SYS_DELAY"))
            if actual != (lag,):
                raise RuntimeError(f"wrote {lag}, read {actual!r}")
        except AUDIO_CONTROL_ERRORS as apply_exc:
            logger.error(
                "volatile AUDIO_MGR_SYS_DELAY apply failed (%s); rolling back to %d",
                apply_exc,
                prior[0],
            )
            try:
                dev.write("AUDIO_MGR_SYS_DELAY", [prior[0]])
                restored = tuple(dev.read("AUDIO_MGR_SYS_DELAY"))
                if restored != prior:
                    raise RuntimeError(
                        f"expected prior value {prior!r}, read {restored!r}"
                    )
            except AUDIO_CONTROL_ERRORS as rollback_exc:
                logger.critical(
                    "AUDIO_MGR_SYS_DELAY rollback failed; chip state is uncertain: %s",
                    rollback_exc,
                )
            else:
                logger.warning(
                    "rolled back AUDIO_MGR_SYS_DELAY to prior value %d",
                    prior[0],
                )
            return False
    finally:
        try:
            dev.close()
        except AUDIO_CONTROL_ERRORS as exc:
            logger.warning("failed to close XVF3800 control handle: %s", exc)

    logger.warning(
        "applied volatile AUDIO_MGR_SYS_DELAY=%d; jasper-aec-init will "
        "overwrite it from the active profile on the next AEC reconcile, "
        "service initialization, or reboot",
        lag,
    )
    return True


def main() -> int:
    args = _argument_parser().parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s aec-tune %(levelname)s %(message)s",
    )
    # This CLI ducks and restores the main fader, so it needs the process
    # owner those writes go through. Registered here for the OWNER rather than
    # for a graph swap — see tests/test_canonical_target_registration.py, whose
    # table now carries both reasons.
    from jasper.volume_coordinator import install_env_canonical_target_provider

    install_env_canonical_target_provider()
    status = 1
    services_to_restore: list[str] = []
    restore_volume: float | None = None
    original_volume: float | None = None
    test_volume: float | None = None

    try:
        if args.inject_noise:
            original_volume = _camilla_get_volume()
            test_volume = original_volume - args.duck_by
            if not math.isfinite(test_volume) or test_volume >= original_volume:
                raise CamillaVolumeError(
                    "active-mode attenuation did not produce a finite, lower volume"
                )
            logger.info(
                "active mode: will duck %.1f dB → %.1f dB during test",
                original_volume,
                test_volume,
            )
        else:
            try:
                current_volume = _camilla_get_volume()
            except AUDIO_CONTROL_ERRORS as exc:
                logger.warning(
                    "Camilla volume unavailable in passive diagnostic mode: %s",
                    exc,
                )
            else:
                logger.info("current main_volume = %.1f dB", current_volume)
            logger.info("passive mode: no test signal injected; ducking unchanged")

        # Record each active unit before stopping it. If systemctl returns an
        # error (or this process is interrupted) after the unit actually
        # stopped, the outer finally still restores its original active state.
        for unit, label in CAPTURE_OWNER_STOP_ORDER:
            if _service_is_active(unit):
                services_to_restore.append(unit)
                _stop_service(unit, label)

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            ref_wav = td_path / "ref.wav"
            mic_wav = td_path / "mic.wav"

            if args.inject_noise:
                assert original_volume is not None
                assert test_volume is not None
                noise_wav = td_path / "noise.wav"
                _write_wav(
                    noise_wav,
                    _generate_noise(
                        TEST_DURATION_SEC,
                        48000,
                        NOISE_AMPLITUDE_FS,
                    ),
                    48000,
                )
                # Restore even when the set call writes successfully but its
                # readback fails. Playback starts only after verified ducking.
                restore_volume = original_volume
                _camilla_set_volume(test_volume)
                play_proc: subprocess.Popen | None = None
                try:
                    # stdout/stderr None: keep aplay's stderr on the
                    # operator's terminal, as the pre-helper inline
                    # Popen did (this is a root operator CLI).
                    play_proc = popen_correction_play(
                        noise_wav,
                        stdout=None,
                        stderr=None,
                    )
                    time.sleep(0.3)
                    capture_ok = _capture_simultaneous(
                        TEST_DURATION_SEC,
                        ref_wav,
                        mic_wav,
                        args.mic_device,
                        args.mic_channels,
                    )
                    playback_ok = _wait_for_audio_process(
                        play_proc,
                        "noise aplay",
                        TEST_DURATION_SEC + PROCESS_EXIT_GRACE_SEC,
                    )
                    ok = capture_ok and playback_ok
                finally:
                    _terminate_and_reap(play_proc, "noise aplay")
            else:
                logger.info(
                    "capturing %ds — make sure music or other audio is playing",
                    TEST_DURATION_SEC,
                )
                ok = _capture_simultaneous(
                    TEST_DURATION_SEC,
                    ref_wav,
                    mic_wav,
                    args.mic_device,
                    args.mic_channels,
                )

            if not ok:
                raise TuneError(
                    "capture failed — files are missing/empty or an audio "
                    "process failed; check jasper-aec-bridge and the XVF "
                    "capture rate/channel layout"
                )
            lag, confidence = _analyze_capture(ref_wav, mic_wav, args.mic_channel)

        print(
            f"\n  Diagnostic AUDIO_MGR_SYS_DELAY candidate = {lag} samples "
            f"({lag * 1000.0 / SAMPLE_RATE:.1f} ms), "
            f"confidence={confidence:.5f}\n"
        )
        if args.apply:
            status = 0 if _apply_volatile_delay(lag, confidence) else 1
        else:
            logger.info(
                "diagnostic-only default: chip and persistent configuration unchanged"
            )
            status = 0
    except KeyboardInterrupt:
        logger.error("interrupted; cleaning up audio processes and runtime state")
        status = 130
    except Exception as exc:  # noqa: BLE001
        logger.error("AEC tune failed: %s", exc)
        status = 1
    finally:
        cleanup_failed = False
        if restore_volume is not None:
            try:
                _camilla_set_volume(restore_volume)
                logger.info(
                    "restored and verified main_volume = %.1f dB", restore_volume
                )
            except AUDIO_CONTROL_ERRORS as exc:
                logger.error("failed to restore Camilla main_volume: %s", exc)
                cleanup_failed = True
        for unit in reversed(services_to_restore):
            try:
                _start_service(unit)
            except AUDIO_CONTROL_ERRORS as exc:
                logger.error("failed to restore %s: %s", unit, exc)
                cleanup_failed = True
        if cleanup_failed:
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
