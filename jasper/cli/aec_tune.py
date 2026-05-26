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
  (hw:Loopback,1,sub1 capture, fed by jasper-aec-bridge's input)
  and the XVF mic for ~5 seconds, then cross-correlates. NO test
  signal injected — uses whatever you're already playing. Requires
  music or other audio to be audible during the test. Volume is
  not modified.

  ACTIVE (`--inject-noise`). Plays a brief, low-level noise burst
  via pcm.jasper_out. Volume is RELATIVELY ducked from the current
  level by `--duck-by` dB (default 20 dB quieter); the code refuses
  to ever raise the volume above the current setting. Use only
  when nothing is playing.

Procedure (passive mode):

  1. Read the current `main_volume` so we can sanity-check that
     audio is actually flowing.
  2. Stop jasper-voice for the duration so we can grab the XVF
     capture EP.
  3. For 5 seconds, capture from BOTH:
        - hw:Loopback,1,sub1 (the AEC reference signal — what the
          XVF chip is being told to expect from the speakers)
        - hw:Array,0 (the XVF processed mic — what the chip
          actually hears from the room)
  4. Cross-correlate (200-3400 Hz bandpass to focus on speech-band
     echo). Lag in samples = AUDIO_MGR_SYS_DELAY.
  5. Restart jasper-voice. Persist + apply.

Run from the Pi after `jasper-camilla` and `jasper-aec-bridge` are
both up. Idempotent — re-run any time room layout changes.

Usage:
    sudo /opt/jasper/.venv/bin/jasper-aec-tune
    sudo /opt/jasper/.venv/bin/jasper-aec-tune --inject-noise --duck-by 20
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

logger = logging.getLogger("jasper.aec_tune")

DELAY_FILE = Path("/var/lib/jasper/aec_delay.txt")
TEST_DURATION_SEC = 5
SAMPLE_RATE = 16000  # XVF internal AEC rate
NOISE_AMPLITUDE_FS = 0.02  # 2% FS = ~ -34 dBFS — quiet even before ducking


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


def _camilla_get_volume() -> float | None:
    try:
        from camilladsp import CamillaClient
    except ImportError:
        return None
    c = CamillaClient("localhost", 1234)
    c.connect()
    try:
        return c.volume.main_volume()
    finally:
        c.disconnect()


def _camilla_set_volume(db: float) -> None:
    try:
        from camilladsp import CamillaClient
    except ImportError:
        logger.warning("camilladsp client not available — skip volume management")
        return
    c = CamillaClient("localhost", 1234)
    c.connect()
    try:
        c.volume.set_main_volume(db)
    finally:
        c.disconnect()


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


def _stop_service_if_running(unit: str, label: str) -> bool:
    """Returns True if the unit was active and we stopped it."""
    check = subprocess.run(
        ["systemctl", "is-active", unit],
        capture_output=True, text=True,
    )
    if check.stdout.strip() == "active":
        logger.info("stopping %s to free %s", unit, label)
        subprocess.run(["systemctl", "stop", unit], check=False)
        time.sleep(0.5)
        return True
    return False


def _restart_service(unit: str) -> None:
    logger.info("restarting %s", unit)
    subprocess.run(["systemctl", "start", unit], check=False)


def _capture_simultaneous(
    duration_sec: float,
    ref_wav: Path,
    mic_wav: Path,
    mic_device: str,
    mic_channels: int,
) -> bool:
    """Start two arecord processes at once, wait for both. Returns True
    on success (both files exist and are non-empty)."""
    # Capture from pcm.jasper_capture — the dsnoop fan-out on the
    # renderer→camilla loopback. dsnoop accepts multiple readers
    # (jasper-camilla and jasper-aec-bridge are the existing two);
    # the tuner becomes a third reader without disrupting either.
    ref_proc = subprocess.Popen(
        [
            "arecord", "-q",
            "-D", "jasper_capture",
            "-d", str(int(duration_sec) + 1),
            "-f", "S16_LE",
            "-r", "48000",
            "-c", "2",
            str(ref_wav),
        ],
    )
    mic_proc = subprocess.Popen(
        [
            "arecord", "-q",
            "-D", mic_device,
            "-d", str(int(duration_sec) + 1),
            "-f", "S16_LE",
            "-r", str(SAMPLE_RATE),
            "-c", str(mic_channels),
            str(mic_wav),
        ],
    )
    ref_proc.wait()
    mic_proc.wait()
    return (
        ref_wav.exists() and ref_wav.stat().st_size > 1024
        and mic_wav.exists() and mic_wav.stat().st_size > 1024
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Calibrate XVF3800 AUDIO_MGR_SYS_DELAY via cross-correlation"
    )
    parser.add_argument(
        "--mic-device", default="hw:CARD=Array,DEV=0",
        help="ALSA capture device for XVF (default: hw:CARD=Array,DEV=0)",
    )
    parser.add_argument(
        "--mic-channels", type=int, default=2,
        help="XVF capture channel count. Stock 2-ch firmware: "
        "0=conference (post-AEC+BF), 1=ASR. 6-ch firmware: also "
        "raw mics on 2-5. (default: 2)",
    )
    parser.add_argument(
        "--mic-channel", type=int, default=0,
        help="Channel index to correlate. 0=conference works on both "
        "firmwares; switch to 2 (raw mic 0) on 6-ch for cleaner echo. "
        "(default: 0)",
    )
    parser.add_argument(
        "--inject-noise", action="store_true",
        help="Play a brief, quiet white-noise burst during the test. "
        "Use only when nothing is otherwise playing — passive mode is "
        "preferred.",
    )
    parser.add_argument(
        "--duck-by", type=float, default=20.0,
        help="When --inject-noise is set, duck main_volume by THIS MANY "
        "DB BELOW THE CURRENT LEVEL during the test (default: 20 dB "
        "quieter). The code never raises the volume.",
    )
    parser.add_argument(
        "--no-apply", action="store_true",
        help="Print the result but don't write the chip or persist file",
    )
    parser.add_argument(
        "--keep-voice-running", action="store_true",
        help="Don't stop jasper-voice during the test (default: stop "
        "and restart so we can grab the XVF capture EP)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s aec-tune %(levelname)s %(message)s",
    )

    original_volume = _camilla_get_volume()
    if original_volume is None:
        original_volume = 0.0
    logger.info("current main_volume = %.1f dB", original_volume)

    if args.inject_noise:
        # Compute the test volume: current minus duck_by, AND clamp to
        # never exceed the current volume. We're calibrating echo, not
        # blasting the room.
        test_volume = min(original_volume - abs(args.duck_by), original_volume)
        if test_volume >= original_volume:
            logger.error(
                "test volume %.1f dB >= current %.1f dB — refusing to "
                "raise volume. Pass --duck-by with a positive value.",
                test_volume, original_volume,
            )
            return 1
        logger.info(
            "active mode: will duck %.1f dB → %.1f dB during test",
            original_volume, test_volume,
        )
    else:
        logger.info("passive mode: no test signal injected; ducking unchanged")

    # We capture from the loopback reference tap defined in
    # /etc/asound.conf and hw:Array,0 (held by jasper-voice). The
    # bridge keeps running uninterrupted; we do still need to stop
    # jasper-voice to grab the XVF capture EP.
    voice_was_active = False
    if not args.keep_voice_running:
        voice_was_active = _stop_service_if_running(
            "jasper-voice.service", "XVF capture EP"
        )

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        ref_wav = td_path / "ref.wav"
        mic_wav = td_path / "mic.wav"

        if args.inject_noise:
            # Generate test signal once.
            noise_wav = td_path / "noise.wav"
            _write_wav(
                noise_wav,
                _generate_noise(TEST_DURATION_SEC, 48000, NOISE_AMPLITUDE_FS),
                48000,
            )
            _camilla_set_volume(test_volume)
            try:
                play_proc = subprocess.Popen(
                    ["aplay", "-q", "-D", "plug:jasper_out", str(noise_wav)],
                )
                time.sleep(0.3)
                ok = _capture_simultaneous(
                    TEST_DURATION_SEC, ref_wav, mic_wav,
                    args.mic_device, args.mic_channels,
                )
                play_proc.wait()
            finally:
                _camilla_set_volume(original_volume)
        else:
            # Passive: assume something is playing. Just record both legs.
            logger.info(
                "capturing %ds — make sure music or other audio is playing",
                TEST_DURATION_SEC,
            )
            ok = _capture_simultaneous(
                TEST_DURATION_SEC, ref_wav, mic_wav,
                args.mic_device, args.mic_channels,
            )

        if voice_was_active:
            _restart_service("jasper-voice.service")

        if not ok:
            logger.error(
                "capture failed — files missing or empty. Check arecord "
                "errors above. Likely: jasper-aec-bridge not running, or "
                "XVF capture rate/channel mismatch."
            )
            return 1

        # Load both signals at SAMPLE_RATE for correlation.
        ref48_arr, ref_rate, ref_ch = _read_wav_int16(ref_wav)
        if ref_rate != 48000:
            logger.error("ref captured at %d Hz, expected 48000", ref_rate)
            return 1
        mic_arr, mic_rate, mic_channels = _read_wav_int16(mic_wav)
        if mic_rate != SAMPLE_RATE:
            logger.error("mic captured at %d Hz, expected %d", mic_rate, SAMPLE_RATE)
            return 1

        if mic_arr.ndim == 2:
            mic_mono = mic_arr[:, args.mic_channel].astype(np.float32)
        else:
            mic_mono = mic_arr.astype(np.float32)

        from scipy.signal import resample_poly
        if ref48_arr.ndim == 2:
            ref_mono48 = ref48_arr[:, 0].astype(np.float32)
        else:
            ref_mono48 = ref48_arr.astype(np.float32)
        ref_mono16 = resample_poly(ref_mono48, up=1, down=3)

        # Sanity-check signal levels
        ref_rms = float(np.sqrt(np.mean(ref_mono16 * ref_mono16)))
        mic_rms = float(np.sqrt(np.mean(mic_mono * mic_mono)))
        logger.info("RMS — reference: %.1f, mic: %.1f", ref_rms, mic_rms)
        if ref_rms < 50:
            logger.error(
                "reference signal RMS %.1f is near zero — nothing audible "
                "during the test. Play music and re-run, or use "
                "--inject-noise.", ref_rms,
            )
            return 1
        if mic_rms < 50:
            logger.warning(
                "mic RMS %.1f is near zero — chip mic signal is silent. "
                "AEC may already be canceling perfectly, or mic is muted.",
                mic_rms,
            )

        lag, confidence = _correlate_and_find_lag(mic_mono, ref_mono16)
        logger.info(
            "cross-correlation: lag=%d samples (%.2f ms) confidence=%.4f",
            lag, lag * 1000.0 / SAMPLE_RATE, confidence,
        )

        if lag <= 0:
            logger.error(
                "non-positive lag (%d) — likely no echo captured. Verify "
                "speakers wired to dongle, jasper-aec-bridge running, "
                "and audio actually playing audibly.", lag,
            )
            return 1
        if lag > 4000:
            logger.error("lag %d > 4000 samples (250 ms) — implausible", lag)
            return 1
        if confidence < 0.001:
            logger.warning(
                "correlation confidence %.5f is very low — result may "
                "be noise. Re-run with louder/different audio playing.",
                confidence,
            )

    if args.no_apply:
        logger.info("--no-apply set, not writing chip or persist file")
        print(f"AUDIO_MGR_SYS_DELAY would be set to {lag}")
        return 0

    DELAY_FILE.parent.mkdir(parents=True, exist_ok=True)
    DELAY_FILE.write_text(f"{lag}\n")
    logger.info("persisted to %s", DELAY_FILE)

    from ..xvf import xvf_host
    dev = xvf_host.find()
    if dev is None:
        logger.error("XVF3800 not on USB — cannot apply now (will apply at boot)")
        return 1
    try:
        dev.write("AUDIO_MGR_SYS_DELAY", [lag])
        logger.info("applied AUDIO_MGR_SYS_DELAY = %d to chip", lag)
    finally:
        try:
            dev.dev.close()
        except Exception:  # noqa: BLE001
            pass

    print(
        f"\n  AUDIO_MGR_SYS_DELAY = {lag} samples "
        f"({lag * 1000.0 / SAMPLE_RATE:.1f} ms)\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
