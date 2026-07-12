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
  (pcm.jasper_capture, the pre-Camilla fan-in diagnostic tap)
  and the XVF mic for ~5 seconds, then cross-correlates. NO test
  signal injected — uses whatever you're already playing. Requires
  music or other audio to be audible during the test. Volume is
  not modified.

  ACTIVE (`--inject-noise`). Plays a brief, low-level noise burst
  through pcm.correction_substream, the canonical pre-Camilla fan-in
  lane. Volume is RELATIVELY ducked from the current
  level by `--duck-by` dB (default 20 dB quieter); the code refuses
  to ever raise the volume above the current setting. Use only
  when nothing is playing.

Procedure (passive mode):

  1. Read the current `main_volume` so we can sanity-check that
     audio is actually flowing.
  2. Stop whichever managed services currently own or consume the XVF
     capture endpoint (jasper-voice and, on supported profiles,
     jasper-aec-bridge) for the duration.
  3. For 5 seconds, capture from BOTH:
        - pcm.jasper_capture (the pre-Camilla fan-in reference)
        - the detected supported XVF card, device 0 (the processed
          mic — what the chip actually hears from the room)
  4. Cross-correlate (200-3400 Hz bandpass to focus on speech-band
     echo). Lag in samples = AUDIO_MGR_SYS_DELAY.
  5. Restore every service that was active and print the diagnostic candidate.

The command is diagnostic-only by default. `--apply` performs one
explicit, volatile write after checking confidence, the firmware's
confirmed -64..256 sample range, USB presence, and readback. The next
`jasper-aec-init` run (including an AEC reconcile or reboot) overwrites
that value from the profile-owned `JASPER_AEC_*_CHIP_SYS_DELAY` setting;
this tool never persists configuration and never calls the XVF brick-risk
SAVE_CONFIGURATION or REBOOT commands.

Run from the Pi after `jasper-camilla` and `jasper-aec-bridge` are
both up. Idempotent — re-run any time room layout changes.

Usage:
    sudo /opt/jasper/.venv/bin/jasper-aec-tune
    sudo /opt/jasper/.venv/bin/jasper-aec-tune --inject-noise --duck-by 20
    sudo /opt/jasper/.venv/bin/jasper-aec-tune --apply
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import logging
import math
import signal
import subprocess
import sys
import tempfile
import time
import wave
from collections.abc import Iterator
from pathlib import Path
from types import FrameType

import numpy as np

from jasper.mics import xvf3800

logger = logging.getLogger("jasper.aec_tune")

TEST_DURATION_SEC = 5
SAMPLE_RATE = 16000  # XVF internal AEC rate
NOISE_AMPLITUDE_FS = 0.02  # 2% FS = ~ -34 dBFS — quiet even before ducking
MIN_APPLY_CONFIDENCE = 0.001
MIN_SYS_DELAY = -64
MAX_SYS_DELAY = 256
PROCESS_EXIT_GRACE_SEC = 3.0
VOLUME_READBACK_TOLERANCE_DB = 0.05
SYSTEMCTL_TIMEOUT_SEC = 10.0
CAMILLA_OPERATION_TIMEOUT_SEC = 5.0
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


@contextmanager
def _bounded_sync_operation(label: str, timeout_sec: float) -> Iterator[None]:
    """Hard-bound one synchronous hardware/client operation on Linux.

    `jasper-aec-tune` is a foreground Pi CLI and executes on the main thread,
    so SIGALRM is the one mechanism that can interrupt pycamilladsp calls even
    when the underlying websocket has wedged. Restore any caller alarm on exit.
    """

    def _raise_timeout(_signum: int, _frame: FrameType | None) -> None:
        raise TimeoutError(f"{label} timed out after {timeout_sec:g}s")

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _raise_timeout)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout_sec)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)


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


def _camilla_get_volume() -> float:
    try:
        from camilladsp import CamillaClient
    except ImportError as exc:
        raise CamillaVolumeError("camilladsp client is not available") from exc
    c = CamillaClient("localhost", 1234)
    connected = False
    try:
        with _bounded_sync_operation(
            "CamillaDSP connect",
            CAMILLA_OPERATION_TIMEOUT_SEC,
        ):
            c.connect()
        connected = True
        with _bounded_sync_operation(
            "CamillaDSP main_volume read",
            CAMILLA_OPERATION_TIMEOUT_SEC,
        ):
            volume = float(c.volume.main_volume())
    finally:
        if connected:
            with _bounded_sync_operation(
                "CamillaDSP disconnect",
                CAMILLA_OPERATION_TIMEOUT_SEC,
            ):
                c.disconnect()
    if not math.isfinite(volume):
        raise CamillaVolumeError(f"Camilla main_volume is not finite: {volume!r}")
    return volume


def _camilla_set_volume(db: float) -> None:
    if not math.isfinite(db):
        raise CamillaVolumeError(f"refusing non-finite Camilla volume: {db!r}")
    try:
        from camilladsp import CamillaClient
    except ImportError as exc:
        raise CamillaVolumeError("camilladsp client is not available") from exc
    c = CamillaClient("localhost", 1234)
    connected = False
    try:
        with _bounded_sync_operation(
            "CamillaDSP connect",
            CAMILLA_OPERATION_TIMEOUT_SEC,
        ):
            c.connect()
        connected = True
        with _bounded_sync_operation(
            "CamillaDSP main_volume write",
            CAMILLA_OPERATION_TIMEOUT_SEC,
        ):
            c.volume.set_main_volume(db)
        with _bounded_sync_operation(
            "CamillaDSP main_volume readback",
            CAMILLA_OPERATION_TIMEOUT_SEC,
        ):
            actual = float(c.volume.main_volume())
    finally:
        if connected:
            with _bounded_sync_operation(
                "CamillaDSP disconnect",
                CAMILLA_OPERATION_TIMEOUT_SEC,
            ):
                c.disconnect()
    if not math.isfinite(actual):
        raise CamillaVolumeError(
            f"Camilla volume readback is not finite after setting {db:.2f} dB"
        )
    if not math.isclose(
        actual,
        db,
        rel_tol=0.0,
        abs_tol=VOLUME_READBACK_TOLERANCE_DB,
    ):
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


def _capture_simultaneous(
    duration_sec: float,
    ref_wav: Path,
    mic_wav: Path,
    mic_device: str,
    mic_channels: int,
) -> bool:
    """Capture both legs, with bounded cleanup for every child-start outcome."""
    # Capture from pcm.jasper_capture — the dsnoop fan-out on the
    # renderer→Camilla loopback. Camilla and optional diagnostic readers can
    # share this tap; production AEC normally consumes outputd's final-speaker
    # UDP monitor instead. The tuner is one temporary diagnostic reader.
    ref_proc: subprocess.Popen | None = None
    mic_proc: subprocess.Popen | None = None
    capture_timeout = int(duration_sec) + 1 + PROCESS_EXIT_GRACE_SEC
    try:
        ref_proc = subprocess.Popen(
            [
                "arecord",
                "-q",
                "-D",
                "jasper_capture",
                "-d",
                str(int(duration_sec) + 1),
                "-f",
                "S16_LE",
                "-r",
                "48000",
                "-c",
                "2",
                str(ref_wav),
            ],
        )
        mic_proc = subprocess.Popen(
            [
                "arecord",
                "-q",
                "-D",
                mic_device,
                "-d",
                str(int(duration_sec) + 1),
                "-f",
                "S16_LE",
                "-r",
                str(SAMPLE_RATE),
                "-c",
                str(mic_channels),
                str(mic_wav),
            ],
        )
        ref_ok = _wait_for_audio_process(ref_proc, "reference arecord", capture_timeout)
        mic_ok = _wait_for_audio_process(
            mic_proc, "microphone arecord", capture_timeout
        )
        files_ok = (
            ref_wav.exists()
            and ref_wav.stat().st_size > 1024
            and mic_wav.exists()
            and mic_wav.stat().st_size > 1024
        )
        return ref_ok and mic_ok and files_ok
    finally:
        _terminate_and_reap(mic_proc, "microphone arecord")
        _terminate_and_reap(ref_proc, "reference arecord")


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
    if ref_rate != 48000:
        raise TuneError(f"ref captured at {ref_rate} Hz, expected 48000")
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
    ref_mono16 = resample_poly(ref_mono48, up=1, down=3)

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
                    play_proc = subprocess.Popen(
                        [
                            "aplay",
                            "-q",
                            "-D",
                            "correction_substream",
                            str(noise_wav),
                        ],
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
