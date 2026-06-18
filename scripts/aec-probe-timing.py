#!/usr/bin/env python3
"""Diagnostic timing probe for outputd/chip-ref/XVF capture paths.

The normal entry point runs from a laptop and ships this file to the Pi
over SSH. The hidden --run-on-pi mode does the hardware work there.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PLAYBACK_RATE_HZ = 48_000
ANALYSIS_RATE_HZ = 16_000
DEFAULT_REF_UDP_HOST = "127.0.0.1"
DEFAULT_REF_UDP_PORT = 9891
DEFAULT_MIC_DEVICE = "hw:CARD=Array,DEV=0"
DEFAULT_MIC_CHANNELS = 6
DEFAULT_REMOTE_PYTHON = "/opt/jasper/.venv/bin/python"
DEFAULT_CONTENT_BUFFER_FRAMES = 4096
OUTPUTD_CONTROL_SOCKET = "/run/jasper-outputd/control.sock"
CHIP_REF_TEE_PATH = "/run/jasper-outputd/aec-timing-probe-chip-ref.s16le"
DROPIN_DIR = "/run/systemd/system/jasper-outputd.service.d"
DROPIN_PATH = f"{DROPIN_DIR}/aec-timing-probe.conf"
DROPIN_ENV_PATH = "/run/jasper-outputd-aec-timing-probe.env"


class ProbeInterrupted(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(f"received signal {signum}")
        self.signum = signum


@dataclass(frozen=True)
class ReferenceSource:
    name: str
    label: str
    sample_rate_hz: int
    channels: int
    warning: str


@dataclass(frozen=True)
class OutputProfile:
    name: str
    period_frames: int
    dac_buffer_frames: int


REFERENCE_SOURCES: dict[str, ReferenceSource] = {
    "outputd_udp": ReferenceSource(
        name="outputd_udp",
        label="outputd UDP 48 kHz final speaker-reference monitor",
        sample_rate_hz=PLAYBACK_RATE_HZ,
        channels=2,
        warning=(
            "outputd_udp is the final electrical speaker-reference monitor, "
            "not the actual XVF USB-IN chip-reference PCM. A UDP-to-mic lag "
            "does not prove chip-ref writer or chip USB-IN timing."
        ),
    ),
    "chip_ref_tee": ReferenceSource(
        name="chip_ref_tee",
        label="outputd chip-ref writer diagnostic tee, 16 kHz dual mono",
        sample_rate_hz=ANALYSIS_RATE_HZ,
        channels=2,
        warning=(
            "chip_ref_tee records bytes dequeued by outputd's chip-ref writer. "
            "It does not prove when the XVF3800 internally consumes USB-IN "
            "reference samples."
        ),
    ),
    "jasper_capture": ReferenceSource(
        name="jasper_capture",
        label="legacy pcm.jasper_capture pre-DSP diagnostic tap",
        sample_rate_hz=PLAYBACK_RATE_HZ,
        channels=2,
        warning=(
            "jasper_capture is the old pre-Camilla/pre-outputd diagnostic tap. "
            "It must not be confused with production outputd final timing."
        ),
    ),
}


PROFILE_PRESETS: dict[str, OutputProfile] = {
    "default": OutputProfile("default", 1024, 3072),
    "1024/3072": OutputProfile("default", 1024, 3072),
    "1024/2048": OutputProfile("1024/2048", 1024, 2048),
    "512/1024": OutputProfile("512/1024", 512, 1024),
}
ALL_PROFILE_NAMES = ("default", "1024/2048", "512/1024")


def utc_stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def mic_channel_label(channel: int) -> str:
    labels = {
        0: "ch0 = conference/beam in chip-AEC mode",
        1: "ch1 = ASR beam in chip-AEC mode",
        2: "ch2 = raw mic0, preferred for acoustic timing",
    }
    return labels.get(channel, f"ch{channel} = unlabeled XVF capture channel")


def parse_profiles(value: str) -> list[OutputProfile]:
    raw = [part.strip() for part in value.split(",") if part.strip()]
    if not raw:
        raise argparse.ArgumentTypeError("at least one profile is required")
    if raw == ["all"]:
        raw = list(ALL_PROFILE_NAMES)

    profiles: list[OutputProfile] = []
    for item in raw:
        preset = PROFILE_PRESETS.get(item)
        if preset is not None:
            profiles.append(preset)
            continue
        match = re.fullmatch(r"([1-9][0-9]*)/([1-9][0-9]*)", item)
        if not match:
            raise argparse.ArgumentTypeError(
                f"profile {item!r} must be default, all, or PERIOD/BUFFER"
            )
        period = int(match.group(1))
        buffer = int(match.group(2))
        min_buffer = period * 2
        if buffer < min_buffer:
            raise argparse.ArgumentTypeError(
                f"profile {item!r} has buffer smaller than 2 x period"
            )
        if DEFAULT_CONTENT_BUFFER_FRAMES < min_buffer:
            raise argparse.ArgumentTypeError(
                f"profile {item!r} requires content buffer >= {min_buffer}, "
                f"but this probe pins content buffer to {DEFAULT_CONTENT_BUFFER_FRAMES}"
            )
        profiles.append(OutputProfile(item, period, buffer))
    return profiles


def source_warnings(ref_source: str, mic_channel: int) -> list[str]:
    warnings = [REFERENCE_SOURCES[ref_source].warning]
    if mic_channel in (0, 1):
        warnings.append(
            f"{mic_channel_label(mic_channel)} is a processed chip beam; "
            "the chip may suppress or reshape the acoustic stimulus before "
            "the probe sees it."
        )
    elif mic_channel == 2:
        warnings.append(
            f"{mic_channel_label(mic_channel)} is the preferred acoustic "
            "path for speaker-to-mic timing, but it is still not chip USB-IN "
            "reference timing."
        )
    return warnings


def _np():
    import numpy as np

    return np


def write_wav(path: Path, data: Any, sample_rate_hz: int) -> None:
    np = _np()
    arr = np.asarray(data, dtype=np.int16)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate_hz)
        wav.writeframes(arr.tobytes())


def make_stimulus(path: Path, *, gain: float) -> dict[str, Any]:
    np = _np()
    duration_s = 0.220
    start_s = 0.35
    total_s = 0.90
    f0 = 350.0
    f1 = 3_600.0

    total_n = int(total_s * PLAYBACK_RATE_HZ)
    chirp_n = int(duration_s * PLAYBACK_RATE_HZ)
    y = np.zeros(total_n, dtype=np.float32)
    t = np.arange(chirp_n, dtype=np.float32) / PLAYBACK_RATE_HZ
    ratio = f1 / f0
    phase = 2.0 * np.pi * f0 * duration_s / math.log(ratio)
    phase *= np.power(ratio, t / duration_s) - 1.0
    chirp = np.sin(phase).astype(np.float32)
    fade_n = max(1, int(0.006 * PLAYBACK_RATE_HZ))
    chirp[:fade_n] *= np.linspace(0.0, 1.0, fade_n)
    chirp[-fade_n:] *= np.linspace(1.0, 0.0, fade_n)
    chirp *= float(gain)
    start = int(start_s * PLAYBACK_RATE_HZ)
    y[start : start + chirp_n] = chirp
    y = np.clip(y, -0.95, 0.95)
    i16 = (y * 32767.0).astype(np.int16)
    stereo = np.column_stack([i16, i16]).reshape(-1)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(PLAYBACK_RATE_HZ)
        wav.writeframes(stereo.tobytes())
    return {
        "path": str(path),
        "sample_rate_hz": PLAYBACK_RATE_HZ,
        "channels": 2,
        "duration_s": total_s,
        "chirp_start_s": start_s,
        "chirp_duration_s": duration_s,
        "chirp_f0_hz": f0,
        "chirp_f1_hz": f1,
        "gain": gain,
    }


def _as_i16_bytes(data: bytes):
    np = _np()
    return np.frombuffer(data, dtype=np.int16)


def downmix_s16le(data: bytes, *, channels: int):
    np = _np()
    arr = _as_i16_bytes(data)
    if channels <= 0:
        raise ValueError("channels must be positive")
    usable = arr.size - (arr.size % channels)
    if usable <= 0:
        return np.empty(0, dtype=np.float32)
    frames = arr[:usable].reshape(-1, channels).astype(np.float32)
    return frames.mean(axis=1)


def decimate_48k_to_16k(mono48):
    np = _np()
    usable = mono48.size - (mono48.size % 3)
    if usable <= 0:
        return np.empty(0, dtype=np.int16)
    mono16 = mono48[:usable].reshape(-1, 3).mean(axis=1)
    return np.clip(mono16, -32768, 32767).astype(np.int16)


def decode_ref_bytes(data: bytes, *, source: str):
    np = _np()
    spec = REFERENCE_SOURCES[source]
    mono = downmix_s16le(data, channels=spec.channels)
    if spec.sample_rate_hz == PLAYBACK_RATE_HZ:
        return decimate_48k_to_16k(mono)
    if spec.sample_rate_hz == ANALYSIS_RATE_HZ:
        return np.clip(mono, -32768, 32767).astype(np.int16)
    raise ValueError(f"unsupported reference sample rate: {spec.sample_rate_hz}")


def audio_metrics(samples: Any, sample_rate_hz: int) -> dict[str, Any]:
    np = _np()
    arr = np.asarray(samples, dtype=np.float64)
    if arr.size == 0:
        return {
            "sample_rate_hz": sample_rate_hz,
            "samples": 0,
            "duration_s": 0.0,
            "rms": 0.0,
            "rms_dbfs": None,
            "peak": 0.0,
            "clipping_samples": 0,
            "clipping_percent": 0.0,
        }
    rms = float(np.sqrt(np.mean(arr * arr)))
    peak = float(np.max(np.abs(arr)))
    clip_count = int(np.count_nonzero(np.abs(arr) >= 32760.0))
    return {
        "sample_rate_hz": sample_rate_hz,
        "samples": int(arr.size),
        "duration_s": float(arr.size / sample_rate_hz),
        "rms": rms,
        "rms_dbfs": (20.0 * math.log10(max(rms, 1e-9) / 32768.0)),
        "peak": peak,
        "clipping_samples": clip_count,
        "clipping_percent": float(clip_count / arr.size * 100.0),
    }


def _fft_correlate_full(a: Any, b: Any):
    np = _np()
    n = len(a) + len(b) - 1
    nfft = 1 << (n - 1).bit_length()
    conv = np.fft.irfft(np.fft.rfft(a, nfft) * np.fft.rfft(b[::-1], nfft), nfft)
    return conv[:n]


def estimate_lag(reference: Any, mic: Any, *, sample_rate_hz: int, search_ms: float) -> dict[str, Any]:
    np = _np()
    ref = np.asarray(reference, dtype=np.float64)
    mic_arr = np.asarray(mic, dtype=np.float64)
    n_min = min(ref.size, mic_arr.size)
    if n_min < max(256, int(0.25 * sample_rate_hz)):
        raise ValueError(f"only {n_min} overlapping samples, capture is too short")
    ref = ref[:n_min]
    mic_arr = mic_arr[:n_min]
    ref_z = ref - float(ref.mean())
    mic_z = mic_arr - float(mic_arr.mean())
    corr = _fft_correlate_full(mic_z, ref_z)
    lags = np.arange(-(len(ref_z) - 1), len(mic_z))
    max_lag = int(search_ms / 1000.0 * sample_rate_hz)
    mask = (lags >= 0) & (lags <= max_lag)
    if not bool(mask.any()):
        raise ValueError("empty positive-lag correlation search window")
    corr_window = np.abs(corr[mask])
    lags_window = lags[mask]
    peak_index = int(np.argmax(corr_window))
    peak_abs = float(corr_window[peak_index])
    lag_samples = int(lags_window[peak_index])
    overlap = n_min - lag_samples
    mic_segment = mic_z[lag_samples : lag_samples + overlap]
    ref_segment = ref_z[:overlap]
    denom = float(np.sqrt(np.sum(mic_segment * mic_segment) * np.sum(ref_segment * ref_segment)))
    normalized_peak = peak_abs / denom if denom > 0 else 0.0
    median_peak = float(np.median(corr_window))
    peak_to_median = peak_abs / max(median_peak, 1.0)
    if normalized_peak >= 0.25 and peak_to_median >= 8.0:
        confidence = "high"
    elif normalized_peak >= 0.12 and peak_to_median >= 4.0:
        confidence = "medium"
    else:
        confidence = "low"
    return {
        "lag_samples": lag_samples,
        "lag_ms": lag_samples / sample_rate_hz * 1000.0,
        "sample_rate_hz": sample_rate_hz,
        "search_ms": search_ms,
        "correlation_peak_abs": peak_abs,
        "normalized_peak": normalized_peak,
        "peak_to_median": peak_to_median,
        "confidence": confidence,
    }


def outputd_status() -> dict[str, Any]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(2.0)
        sock.connect(OUTPUTD_CONTROL_SOCKET)
        sock.sendall(b"STATUS\n")
        chunks: list[bytes] = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
        return json.loads(b"".join(chunks).decode("utf-8"))
    finally:
        sock.close()


def run_cmd(args: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=check,
        text=True,
        capture_output=capture,
    )


def systemctl(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return run_cmd(["systemctl", *args], check=check, capture=capture)


def service_active(unit: str) -> bool:
    result = systemctl("is-active", "--quiet", unit, check=False)
    return result.returncode == 0


def write_outputd_dropin(profile: OutputProfile, *, tee_path: str | None) -> None:
    Path(DROPIN_DIR).mkdir(parents=True, exist_ok=True)
    env_lines = [
        f"JASPER_OUTPUTD_PERIOD_FRAMES={profile.period_frames}",
        f"JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES={DEFAULT_CONTENT_BUFFER_FRAMES}",
        f"JASPER_OUTPUTD_DAC_BUFFER_FRAMES={profile.dac_buffer_frames}",
        f"JASPER_OUTPUTD_CHIP_REF_TEE_PATH={tee_path or ''}",
    ]
    env_path = Path(DROPIN_ENV_PATH)
    env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    env_path.chmod(0o600)
    lines = [
        "[Service]",
        f"EnvironmentFile={DROPIN_ENV_PATH}",
    ]
    Path(DROPIN_PATH).write_text("\n".join(lines) + "\n", encoding="utf-8")


def remove_outputd_dropin() -> None:
    path = Path(DROPIN_PATH)
    if path.exists():
        path.unlink()
    env_path = Path(DROPIN_ENV_PATH)
    if env_path.exists():
        env_path.unlink()
    try:
        Path(DROPIN_DIR).rmdir()
    except OSError:
        pass


def install_termination_handlers() -> dict[int, Any]:
    previous: dict[int, Any] = {}

    def _handle(signum: int, _frame: Any) -> None:
        raise ProbeInterrupted(signum)

    for signame in ("SIGINT", "SIGTERM", "SIGHUP"):
        signum = getattr(signal, signame, None)
        if signum is None:
            continue
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, _handle)
    return previous


def ignore_termination_handlers(previous: dict[int, Any]) -> None:
    for signum in previous:
        signal.signal(signum, signal.SIG_IGN)


def restore_signal_handlers(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def restart_outputd_for_profile(profile: OutputProfile, *, ref_source: str) -> None:
    tee = CHIP_REF_TEE_PATH if ref_source == "chip_ref_tee" else None
    write_outputd_dropin(profile, tee_path=tee)
    systemctl("daemon-reload")
    systemctl("reset-failed", "jasper-outputd.service", check=False)
    systemctl("restart", "jasper-outputd.service")
    deadline = time.time() + 8.0
    last_error: BaseException | None = None
    while time.time() < deadline:
        try:
            outputd_status()
            break
        except (OSError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(0.25)
    else:
        raise RuntimeError(f"outputd did not answer STATUS after restart: {last_error}")
    if ref_source == "chip_ref_tee":
        wait_for_chip_ref_tee_ready()


def wait_for_chip_ref_tee_ready(*, timeout_s: float = 2.0) -> None:
    tee_path = Path(CHIP_REF_TEE_PATH)
    deadline = time.time() + timeout_s
    last_status: dict[str, Any] | None = None
    while time.time() < deadline:
        if tee_path.exists():
            return
        try:
            last_status = outputd_status()
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(0.1)

    status_hint = ""
    if last_status is not None:
        keys = ", ".join(sorted(str(key) for key in last_status.keys())[:16])
        status_hint = f"; outputd STATUS keys: {keys}"
    message = (
        f"chip_ref_tee was requested, but {tee_path} was not created after "
        "outputd restart. Deploy an outputd build with "
        "JASPER_OUTPUTD_CHIP_REF_TEE_PATH support, or use "
        "--ref-source outputd_udp"
    )
    if status_hint:
        message = f"{message}{status_hint}"
    else:
        message = f"{message}."
    raise RuntimeError(message)


def capture_mic(
    *,
    mic_device: str,
    mic_channels: int,
    mic_channel: int,
    duration_s: float,
    out: list[Any],
) -> None:
    np = _np()
    import alsaaudio

    pcm = alsaaudio.PCM(
        type=alsaaudio.PCM_CAPTURE,
        mode=alsaaudio.PCM_NORMAL,
        device=mic_device,
        rate=ANALYSIS_RATE_HZ,
        channels=mic_channels,
        format=alsaaudio.PCM_FORMAT_S16_LE,
        periodsize=320,
    )
    try:
        end = time.time() + duration_s
        while time.time() < end:
            length, data = pcm.read()
            if length <= 0:
                continue
            arr = np.frombuffer(data, dtype=np.int16)
            usable = arr.size - (arr.size % mic_channels)
            if usable:
                out.append(arr[:usable].reshape(-1, mic_channels)[:, mic_channel].copy())
    finally:
        pcm.close()


def capture_udp_ref(*, host: str, port: int, duration_s: float, out: list[bytes]) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((host, port))
        sock.settimeout(0.1)
        end = time.time() + duration_s
        while time.time() < end:
            try:
                data, _addr = sock.recvfrom(65536)
            except socket.timeout:
                continue
            if data:
                out.append(data)
    finally:
        sock.close()


def capture_alsa_ref(
    *,
    device: str,
    duration_s: float,
    out: list[bytes],
) -> None:
    import alsaaudio

    pcm = alsaaudio.PCM(
        type=alsaaudio.PCM_CAPTURE,
        mode=alsaaudio.PCM_NORMAL,
        device=device,
        rate=PLAYBACK_RATE_HZ,
        channels=2,
        format=alsaaudio.PCM_FORMAT_S16_LE,
        periodsize=1024,
    )
    try:
        end = time.time() + duration_s
        while time.time() < end:
            length, data = pcm.read()
            if length > 0:
                out.append(data)
    finally:
        pcm.close()


def run_capture_once(args: argparse.Namespace, profile: OutputProfile, run_index: int, out_dir: Path) -> dict[str, Any]:
    np = _np()
    if args.mic_channel < 0 or args.mic_channel >= args.mic_channels:
        raise ValueError(
            f"--mic-channel {args.mic_channel} is outside 0..{args.mic_channels - 1}"
        )

    run_id = f"{profile.name.replace('/', '-')}-run{run_index + 1}"
    stimulus_path = out_dir / f"{run_id}-stimulus.wav"
    stimulus = make_stimulus(stimulus_path, gain=args.chirp_gain)

    tee_path = Path(CHIP_REF_TEE_PATH)
    tee_offset = tee_path.stat().st_size if args.ref_source == "chip_ref_tee" and tee_path.exists() else 0

    mic_chunks: list[Any] = []
    ref_chunks: list[bytes] = []
    errors: list[str] = []
    lock = threading.Lock()

    def guarded(label: str, fn: Any, **kwargs: Any) -> None:
        try:
            fn(**kwargs)
        except Exception as exc:  # noqa: BLE001 - capture worker reports diagnostic failures for main-thread cleanup.
            with lock:
                errors.append(f"{label}: {type(exc).__name__}: {exc}")

    mic_thread = threading.Thread(
        target=guarded,
        kwargs={
            "label": "mic",
            "fn": capture_mic,
            "mic_device": args.mic_device,
            "mic_channels": args.mic_channels,
            "mic_channel": args.mic_channel,
            "duration_s": args.duration_s,
            "out": mic_chunks,
        },
        daemon=True,
    )
    ref_thread: threading.Thread | None = None
    if args.ref_source == "outputd_udp":
        ref_thread = threading.Thread(
            target=guarded,
            kwargs={
                "label": "ref",
                "fn": capture_udp_ref,
                "host": args.ref_udp_host,
                "port": args.ref_udp_port,
                "duration_s": args.duration_s,
                "out": ref_chunks,
            },
            daemon=True,
        )
    elif args.ref_source == "jasper_capture":
        ref_thread = threading.Thread(
            target=guarded,
            kwargs={
                "label": "ref",
                "fn": capture_alsa_ref,
                "device": args.jasper_capture_pcm,
                "duration_s": args.duration_s,
                "out": ref_chunks,
            },
            daemon=True,
        )

    state_before = outputd_status()
    mic_thread.start()
    if ref_thread is not None:
        ref_thread.start()
    time.sleep(args.warmup_s)
    run_cmd(["aplay", "-q", "-D", "correction_substream", str(stimulus_path)])
    mic_thread.join()
    if ref_thread is not None:
        ref_thread.join()
    state_after = outputd_status()

    if errors:
        raise RuntimeError("; ".join(errors))
    if not mic_chunks:
        raise RuntimeError(
            f"no mic samples captured from {args.mic_device} "
            f"{mic_channel_label(args.mic_channel)}"
        )

    mic = np.concatenate(mic_chunks).astype(np.int16)
    if args.ref_source == "chip_ref_tee":
        if not tee_path.exists():
            raise RuntimeError(f"chip-ref tee path was not created: {tee_path}")
        data = tee_path.read_bytes()[tee_offset:]
        ref = decode_ref_bytes(data, source=args.ref_source)
        ref_raw_bytes = len(data)
    else:
        if not ref_chunks:
            raise RuntimeError(f"no reference samples captured from {args.ref_source}")
        ref_raw = b"".join(ref_chunks)
        ref = decode_ref_bytes(ref_raw, source=args.ref_source)
        ref_raw_bytes = len(ref_raw)

    lag = estimate_lag(ref, mic, sample_rate_hz=ANALYSIS_RATE_HZ, search_ms=args.search_ms)
    ref_metrics = audio_metrics(ref, ANALYSIS_RATE_HZ)
    mic_metrics = audio_metrics(mic, ANALYSIS_RATE_HZ)
    warnings = source_warnings(args.ref_source, args.mic_channel)
    if ref_metrics["rms"] < 10.0:
        warnings.append("reference RMS is very low; correlation may be noise-dominated")
    if mic_metrics["rms"] < 10.0:
        warnings.append("mic RMS is very low; acoustic stimulus may not have reached the selected channel")
    if ref_metrics["clipping_percent"] > 0.0:
        warnings.append("reference capture contains clipped samples")
    if mic_metrics["clipping_percent"] > 0.0:
        warnings.append("mic capture contains clipped samples")

    ref_wav = out_dir / f"{run_id}-ref-{args.ref_source}.wav"
    mic_wav = out_dir / f"{run_id}-mic-ch{args.mic_channel}.wav"
    write_wav(ref_wav, ref, ANALYSIS_RATE_HZ)
    write_wav(mic_wav, mic, ANALYSIS_RATE_HZ)

    return {
        "run_id": run_id,
        "profile": {
            "name": profile.name,
            "period_frames": profile.period_frames,
            "dac_buffer_frames": profile.dac_buffer_frames,
        },
        "ref_source": {
            "name": args.ref_source,
            "label": REFERENCE_SOURCES[args.ref_source].label,
            "raw_bytes": ref_raw_bytes,
            "udp_target": (
                f"{args.ref_udp_host}:{args.ref_udp_port}"
                if args.ref_source == "outputd_udp"
                else None
            ),
            "alsa_pcm": args.jasper_capture_pcm if args.ref_source == "jasper_capture" else None,
            "tee_path": str(tee_path) if args.ref_source == "chip_ref_tee" else None,
            "tee_offset_bytes": tee_offset if args.ref_source == "chip_ref_tee" else None,
        },
        "mic": {
            "device": args.mic_device,
            "channels": args.mic_channels,
            "channel": args.mic_channel,
            "label": mic_channel_label(args.mic_channel),
        },
        "stimulus": stimulus,
        "duration_requested_s": args.duration_s,
        "lag": lag,
        "reference_metrics": ref_metrics,
        "mic_metrics": mic_metrics,
        "warnings": warnings,
        "artifacts": {
            "reference_wav": str(ref_wav),
            "mic_wav": str(mic_wav),
            "stimulus_wav": str(stimulus_path),
        },
        "outputd_state_before": state_before,
        "outputd_state_after": state_after,
    }


def write_results(out_dir: Path, payload: dict[str, Any]) -> None:
    results = payload["results"]
    json_path = out_dir / "results.json"
    csv_path = out_dir / "results.csv"
    md_path = out_dir / "summary.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    columns = [
        "run_id",
        "profile",
        "period_frames",
        "dac_buffer_frames",
        "ref_source",
        "ref_label",
        "mic_channel",
        "mic_label",
        "lag_samples",
        "lag_ms",
        "confidence",
        "normalized_peak",
        "peak_to_median",
        "ref_rms",
        "ref_rms_dbfs",
        "ref_clipping_percent",
        "mic_rms",
        "mic_rms_dbfs",
        "mic_clipping_percent",
        "ref_sample_rate_hz",
        "mic_sample_rate_hz",
        "ref_duration_s",
        "mic_duration_s",
        "warnings",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for item in results:
            writer.writerow(
                {
                    "run_id": item["run_id"],
                    "profile": item["profile"]["name"],
                    "period_frames": item["profile"]["period_frames"],
                    "dac_buffer_frames": item["profile"]["dac_buffer_frames"],
                    "ref_source": item["ref_source"]["name"],
                    "ref_label": item["ref_source"]["label"],
                    "mic_channel": item["mic"]["channel"],
                    "mic_label": item["mic"]["label"],
                    "lag_samples": item["lag"]["lag_samples"],
                    "lag_ms": f'{item["lag"]["lag_ms"]:.3f}',
                    "confidence": item["lag"]["confidence"],
                    "normalized_peak": f'{item["lag"]["normalized_peak"]:.6f}',
                    "peak_to_median": f'{item["lag"]["peak_to_median"]:.3f}',
                    "ref_rms": f'{item["reference_metrics"]["rms"]:.3f}',
                    "ref_rms_dbfs": _fmt_optional_float(item["reference_metrics"]["rms_dbfs"]),
                    "ref_clipping_percent": f'{item["reference_metrics"]["clipping_percent"]:.6f}',
                    "mic_rms": f'{item["mic_metrics"]["rms"]:.3f}',
                    "mic_rms_dbfs": _fmt_optional_float(item["mic_metrics"]["rms_dbfs"]),
                    "mic_clipping_percent": f'{item["mic_metrics"]["clipping_percent"]:.6f}',
                    "ref_sample_rate_hz": item["reference_metrics"]["sample_rate_hz"],
                    "mic_sample_rate_hz": item["mic_metrics"]["sample_rate_hz"],
                    "ref_duration_s": f'{item["reference_metrics"]["duration_s"]:.3f}',
                    "mic_duration_s": f'{item["mic_metrics"]["duration_s"]:.3f}',
                    "warnings": " | ".join(item["warnings"]),
                }
            )

    lines = [
        "# AEC Timing Probe Summary",
        "",
        f"Created: {payload['created_at']}",
        f"Host: {payload['host']}",
        f"Reference source: `{payload['config']['ref_source']}` - {REFERENCE_SOURCES[payload['config']['ref_source']].label}",
        f"Mic channel: {mic_channel_label(payload['config']['mic_channel'])}",
        "",
        "| Run | Profile | Lag ms | Confidence | Peak | Ref RMS | Mic RMS |",
        "|---|---:|---:|---|---:|---:|---:|",
    ]
    for item in results:
        lines.append(
            "| {run} | {profile} | {lag:.2f} | {confidence} | {peak:.3f} | {ref:.1f} | {mic:.1f} |".format(
                run=item["run_id"],
                profile=item["profile"]["name"],
                lag=item["lag"]["lag_ms"],
                confidence=item["lag"]["confidence"],
                peak=item["lag"]["normalized_peak"],
                ref=item["reference_metrics"]["rms"],
                mic=item["mic_metrics"]["rms"],
            )
        )
    lines.extend(
        [
            "",
            "## Warnings",
            "",
        ]
    )
    unique_warnings = sorted({warning for item in results for warning in item["warnings"]})
    for warning in unique_warnings:
        lines.append(f"- {warning}")
    lines.extend(
        [
            "",
            "## What This Proves",
            "",
            "- The probe measures correlation lag between the selected reference tap and the selected XVF capture channel while a controlled chirp travels through `correction_substream`.",
            "- It records outputd state snapshots around each run so DAC/chip-ref writer counters can be compared with the active measurement.",
            "",
            "## What This Does Not Prove",
            "",
            "- It does not directly timestamp DAC diaphragm motion, XVF USB-IN internal consumption, or chip-internal AEC alignment.",
            "- `outputd_udp` and `jasper_capture` comparisons are not chip-ref input measurements.",
            "- `chip_ref_tee` is a writer-side diagnostic sample tap, not a hardware timestamp.",
            "",
            "Machine-readable files: `results.json`, `results.csv`.",
        ]
    )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fmt_optional_float(value: Any) -> str:
    return "" if value is None else f"{float(value):.3f}"


def run_on_pi(args: argparse.Namespace) -> int:
    os.umask(0o077)
    out_dir = Path(args.remote_dir or f"/tmp/aec-timing-probe-{utc_stamp()}")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.chmod(0o700)
    print(f"remote_artifact_dir={out_dir}", flush=True)
    previous_signal_handlers = install_termination_handlers()
    interrupted: ProbeInterrupted | None = None

    tracked_units = {
        "shairport-sync.service": service_active("shairport-sync.service"),
        "nqptp.service": service_active("nqptp.service"),
        "jasper-voice.service": service_active("jasper-voice.service"),
        "jasper-aec-bridge.service": service_active("jasper-aec-bridge.service"),
        "jasper-outputd.service": service_active("jasper-outputd.service"),
    }
    if not tracked_units["jasper-outputd.service"]:
        raise RuntimeError("jasper-outputd.service is not active; cannot probe outputd timing")

    results: list[dict[str, Any]] = []
    try:
        try:
            print("stopping voice/AirPlay/AEC bridge holders for direct XVF capture...", flush=True)
            for unit in ("shairport-sync.service", "jasper-voice.service", "jasper-aec-bridge.service"):
                if tracked_units[unit]:
                    systemctl("stop", unit, check=False)
            time.sleep(1.0)

            for profile in args.profiles:
                print(
                    f"applying diagnostic outputd profile {profile.name} "
                    f"({profile.period_frames}/{profile.dac_buffer_frames})...",
                    flush=True,
                )
                restart_outputd_for_profile(profile, ref_source=args.ref_source)
                time.sleep(1.0)
                for run_index in range(args.runs):
                    print(
                        f"capturing {profile.name} run {run_index + 1}/{args.runs}: "
                        f"ref={args.ref_source} mic=ch{args.mic_channel}",
                        flush=True,
                    )
                    result = run_capture_once(args, profile, run_index, out_dir)
                    results.append(result)
                    print(
                        "  lag={:.2f} ms confidence={} peak={:.3f} ref_rms={:.1f} mic_rms={:.1f}".format(
                            result["lag"]["lag_ms"],
                            result["lag"]["confidence"],
                            result["lag"]["normalized_peak"],
                            result["reference_metrics"]["rms"],
                            result["mic_metrics"]["rms"],
                        ),
                        flush=True,
                    )
        except ProbeInterrupted as exc:
            interrupted = exc
            print(f"interrupted by signal {exc.signum}; restoring services...", file=sys.stderr, flush=True)
    finally:
        ignore_termination_handlers(previous_signal_handlers)
        try:
            print("restoring outputd/service state...", flush=True)
            remove_outputd_dropin()
            systemctl("daemon-reload", check=False)
            if tracked_units.get("jasper-outputd.service"):
                systemctl("reset-failed", "jasper-outputd.service", check=False)
                systemctl("restart", "jasper-outputd.service", check=False)
            if tracked_units.get("jasper-aec-bridge.service"):
                systemctl("reset-failed", "jasper-aec-bridge.service", check=False)
                systemctl("start", "jasper-aec-bridge.service", check=False)
            if tracked_units.get("jasper-voice.service"):
                systemctl("start", "jasper-voice.service", check=False)
            if tracked_units.get("nqptp.service"):
                systemctl("restart", "nqptp.service", check=False)
            if tracked_units.get("shairport-sync.service"):
                systemctl("restart", "shairport-sync.service", check=False)
        finally:
            restore_signal_handlers(previous_signal_handlers)
    if interrupted is not None:
        return 128 + interrupted.signum

    payload = {
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "host": socket.gethostname(),
        "config": {
            "ref_source": args.ref_source,
            "ref_label": REFERENCE_SOURCES[args.ref_source].label,
            "mic_device": args.mic_device,
            "mic_channels": args.mic_channels,
            "mic_channel": args.mic_channel,
            "mic_label": mic_channel_label(args.mic_channel),
            "duration_s": args.duration_s,
            "search_ms": args.search_ms,
            "profiles": [profile.__dict__ for profile in args.profiles],
            "runs": args.runs,
        },
        "results": results,
    }
    write_results(out_dir, payload)
    print(f"AEC_TIMING_REMOTE_DIR={out_dir}", flush=True)
    print((out_dir / "summary.md").read_text(encoding="utf-8"), flush=True)
    return 0


def safe_extract_tar(data: bytes, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        root_name = members[0].name.split("/", 1)[0] if members else ""
        dest_root = (destination / root_name).resolve()
        for member in members:
            target = (destination / member.name).resolve()
            if destination.resolve() not in target.parents and target != destination.resolve():
                raise RuntimeError(f"refusing unsafe tar member {member.name!r}")
        archive.extractall(destination)
    return dest_root


def pull_remote_dir(*, target: str, remote_dir: str, local_parent: Path) -> Path:
    remote = Path(remote_dir)
    cmd = [
        "ssh",
        target,
        shlex.join(["sudo", "tar", "-C", str(remote.parent), "-czf", "-", remote.name]),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True)
    return safe_extract_tar(result.stdout, local_parent)


def run_via_ssh(args: argparse.Namespace) -> int:
    script_text = Path(__file__).read_text(encoding="utf-8")
    target = f"{args.pi_user}@{args.pi_host}"
    remote_args = [
        "sudo",
        args.remote_python,
        "-",
        "--run-on-pi",
        "--ref-source",
        args.ref_source,
        "--mic-device",
        args.mic_device,
        "--mic-channels",
        str(args.mic_channels),
        "--mic-channel",
        str(args.mic_channel),
        "--duration",
        str(args.duration_s),
        "--search-ms",
        str(args.search_ms),
        "--warmup",
        str(args.warmup_s),
        "--chirp-gain",
        str(args.chirp_gain),
        "--profiles",
        args.profiles_arg,
        "--runs",
        str(args.runs),
        "--ref-udp-host",
        args.ref_udp_host,
        "--ref-udp-port",
        str(args.ref_udp_port),
        "--jasper-capture-pcm",
        args.jasper_capture_pcm,
    ]
    if args.remote_dir:
        remote_args.extend(["--remote-dir", args.remote_dir])
    print(f"running on {target}: ref={args.ref_source} mic=ch{args.mic_channel}")
    proc = subprocess.run(
        ["ssh", target, shlex.join(remote_args)],
        input=script_text,
        text=True,
        capture_output=True,
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        return proc.returncode
    match = re.search(r"^AEC_TIMING_REMOTE_DIR=(.+)$", proc.stdout, re.MULTILINE)
    if not match:
        print("ERROR: remote run did not report artifact directory", file=sys.stderr)
        return 2
    remote_dir = match.group(1).strip()
    if args.no_pull:
        print(f"remote artifacts left at {target}:{remote_dir}")
        return 0
    local_parent = Path(args.output_dir or "logs")
    local_dir = pull_remote_dir(target=target, remote_dir=remote_dir, local_parent=local_parent)
    print(f"local artifacts: {local_dir}")
    print(f"remote artifacts left at {target}:{remote_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Measure diagnostic timing relationships between outputd/chip-ref "
            "reference taps and selected XVF capture channels."
        )
    )
    parser.add_argument("--run-on-pi", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--pi-host", default=os.environ.get("PI_HOST", os.environ.get("JASPER_HOSTNAME", "jts.local")))
    parser.add_argument("--pi-user", default=os.environ.get("PI_USER", "pi"))
    parser.add_argument("--remote-python", default=DEFAULT_REMOTE_PYTHON)
    parser.add_argument(
        "--ref-source",
        choices=sorted(REFERENCE_SOURCES),
        default="outputd_udp",
        help="reference tap to compare against the selected mic channel",
    )
    parser.add_argument("--ref-udp-host", default=DEFAULT_REF_UDP_HOST)
    parser.add_argument("--ref-udp-port", type=int, default=DEFAULT_REF_UDP_PORT)
    parser.add_argument(
        "--jasper-capture-pcm",
        default="jasper_ref",
        help="ALSA PCM used for --ref-source jasper_capture",
    )
    parser.add_argument("--mic-device", default=DEFAULT_MIC_DEVICE)
    parser.add_argument("--mic-channels", type=int, default=DEFAULT_MIC_CHANNELS)
    parser.add_argument(
        "--mic-channel",
        type=int,
        default=2,
        help="XVF channel index; ch2 raw mic0 is preferred for acoustic timing",
    )
    parser.add_argument("--duration", dest="duration_s", type=float, default=2.0)
    parser.add_argument("--search-ms", type=float, default=300.0)
    parser.add_argument("--warmup", dest="warmup_s", type=float, default=0.25)
    parser.add_argument("--chirp-gain", type=float, default=0.25)
    parser.add_argument(
        "--profiles",
        dest="profiles_arg",
        default="default",
        help="comma list: default, 1024/2048, 512/1024, PERIOD/BUFFER, or all",
    )
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--remote-dir", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--no-pull", action="store_true")
    return parser


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.profiles = parse_profiles(args.profiles_arg)
    if args.duration_s <= 0:
        parser.error("--duration must be positive")
    if args.search_ms <= 0:
        parser.error("--search-ms must be positive")
    if args.warmup_s < 0:
        parser.error("--warmup must be non-negative")
    if not (0.0 < args.chirp_gain <= 1.0):
        parser.error("--chirp-gain must be in (0, 1]")
    if args.runs <= 0:
        parser.error("--runs must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.run_on_pi:
        try:
            return run_on_pi(args)
        except Exception as exc:  # noqa: BLE001 - remote CLI should print a concise error instead of a traceback.
            print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    return run_via_ssh(args)


if __name__ == "__main__":
    raise SystemExit(main())
