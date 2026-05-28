#!/usr/bin/env bash
# Estimate bulk reference-to-mic delay for the cheap USB corpus mic.
#
# This is an operator probe, not production code. It plays a short,
# safe chirp/click train through correction_substream, captures the
# AEC bridge's UDP ref + raw mic legs, then cross-correlates ref
# against usb_raw / XVF raw legs to estimate the delay AEC3 should be
# hinted with.
#
# Usage:
#   bash scripts/aec-probe-usb-delay.sh
#   DURATION_SEC=12 bash scripts/aec-probe-usb-delay.sh
#   PI_HOST=192.168.1.74 bash scripts/aec-probe-usb-delay.sh
#
# Side effects: briefly stops jasper-voice so this probe can bind the
# UDP receiver ports, temporarily enables corpus ref/USB bridge outputs,
# restarts jasper-aec-bridge, and restores the prior bridge env/state
# when done. Playback goes through the normal correction_substream lane.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/_lib.sh" ]]; then
  # shellcheck source=scripts/_lib.sh
  source "${SCRIPT_DIR}/_lib.sh"
fi

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"
DURATION_SEC="${DURATION_SEC:-12}"
LEVEL_DBFS="${LEVEL_DBFS:--20}"
SEARCH_MS="${SEARCH_MS:-250}"
RUNS="${RUNS:-1}"

ssh "${PI_USER}@${PI_HOST}" \
  "DURATION_SEC='${DURATION_SEC}' LEVEL_DBFS='${LEVEL_DBFS}' SEARCH_MS='${SEARCH_MS}' RUNS='${RUNS}' bash -s" <<'REMOTE'
set -euo pipefail

ENV_FILE="/var/lib/jasper/wake_corpus_bridge.env"
WORK_DIR="/tmp/jasper-aec-delay-probe-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "${WORK_DIR}"

VOICE_WAS_ACTIVE="$(systemctl is-active jasper-voice.service 2>/dev/null || true)"
SHAIRPORT_WAS_ACTIVE="$(systemctl is-active shairport-sync.service 2>/dev/null || true)"
ENV_HAD_FILE=0
if [[ -f "${ENV_FILE}" ]]; then
  ENV_HAD_FILE=1
  cp "${ENV_FILE}" "${WORK_DIR}/wake_corpus_bridge.env.before"
fi

restore() {
  set +e
  if [[ "${ENV_HAD_FILE}" == "1" ]]; then
    sudo install -m 0644 "${WORK_DIR}/wake_corpus_bridge.env.before" "${ENV_FILE}"
  else
    sudo install -m 0644 /dev/stdin "${ENV_FILE}" <<'EOF'
JASPER_AEC_CORPUS_REF_ENABLED=0
JASPER_AEC_CORPUS_USB_ENABLED=0
JASPER_AEC_CORPUS_USB_DTLN_ENABLED=0
JASPER_AEC_CORPUS_AEC3_SWEEP_ENABLED=0
EOF
  fi
  sudo systemctl reset-failed jasper-aec-bridge.service
  sudo systemctl restart jasper-aec-bridge.service
  if [[ "${VOICE_WAS_ACTIVE}" == "active" ]]; then
    sudo systemctl start jasper-voice.service
  fi
  if [[ "${SHAIRPORT_WAS_ACTIVE}" == "active" ]]; then
    sudo systemctl restart shairport-sync.service nqptp.service
  fi
}
trap restore EXIT

echo "== JTS USB AEC delay probe =="
echo "work_dir=${WORK_DIR}"
echo "duration_sec=${DURATION_SEC} level_dbfs=${LEVEL_DBFS} search_ms=${SEARCH_MS} runs=${RUNS}"
echo "stopping voice/playback holders briefly..."
sudo systemctl stop jasper-voice.service shairport-sync.service || true

if [[ -f "${ENV_FILE}" ]]; then
  sudo sh -c "grep -vE '^(JASPER_AEC_CORPUS_REF_ENABLED|JASPER_AEC_CORPUS_USB_ENABLED|JASPER_AEC_CORPUS_USB_DTLN_ENABLED|JASPER_AEC_CORPUS_AEC3_SWEEP_ENABLED)=' '${ENV_FILE}' > '${WORK_DIR}/wake_corpus_bridge.env.next' || true"
else
  : > "${WORK_DIR}/wake_corpus_bridge.env.next"
fi
cat >> "${WORK_DIR}/wake_corpus_bridge.env.next" <<'EOF'
JASPER_AEC_CORPUS_REF_ENABLED=1
JASPER_AEC_CORPUS_USB_ENABLED=1
JASPER_AEC_CORPUS_USB_DTLN_ENABLED=0
JASPER_AEC_CORPUS_AEC3_SWEEP_ENABLED=0
EOF
sudo install -m 0644 "${WORK_DIR}/wake_corpus_bridge.env.next" "${ENV_FILE}"

echo "restarting bridge with ref + USB corpus outputs enabled..."
sudo systemctl reset-failed jasper-aec-bridge.service
sudo systemctl restart jasper-aec-bridge.service
sleep 3

sudo \
  DURATION_SEC="${DURATION_SEC}" \
  LEVEL_DBFS="${LEVEL_DBFS}" \
  SEARCH_MS="${SEARCH_MS}" \
  RUNS="${RUNS}" \
  WORK_DIR="${WORK_DIR}" \
  /opt/jasper/.venv/bin/python <<'PY'
from __future__ import annotations

import json
import math
import os
import socket
import subprocess
import threading
import time
import wave
from pathlib import Path

import numpy as np
from scipy import signal

FS = 16_000
PLAY_FS = 48_000
WORK_DIR = Path(os.environ["WORK_DIR"])
DURATION_SEC = float(os.environ.get("DURATION_SEC", "12"))
LEVEL_DBFS = float(os.environ.get("LEVEL_DBFS", "-20"))
SEARCH_MS = float(os.environ.get("SEARCH_MS", "250"))
SEARCH_SAMPLES = int(FS * SEARCH_MS / 1000.0)
RUNS = max(1, int(os.environ.get("RUNS", "1")))

LEGS = {
    "xvf_aec": 9876,
    "xvf_raw": 9877,
    "xvf_raw0": 9879,
    "ref": 9880,
    "usb_raw": 9881,
}


def write_wav(path: Path, data: np.ndarray, sample_rate: int = FS) -> None:
    arr = np.asarray(data, dtype=np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(arr.tobytes())


def make_stimulus() -> Path:
    """Build a modest-level repeated chirp train for robust correlation."""
    total = max(DURATION_SEC - 2.0, 4.0)
    samples = int(total * PLAY_FS)
    y = np.zeros(samples, dtype=np.float32)
    marker_dur = 0.120
    marker_n = int(marker_dur * PLAY_FS)
    t = np.arange(marker_n, dtype=np.float32) / PLAY_FS
    marker = signal.chirp(t, f0=450, f1=4_200, t1=marker_dur, method="log")
    marker *= np.hanning(marker_n)
    marker *= float(10.0 ** (LEVEL_DBFS / 20.0))
    starts = np.arange(0.75, total - 0.75, 1.25)
    for idx, start_sec in enumerate(starts):
        start = int(start_sec * PLAY_FS)
        end = min(start + marker_n, samples)
        sign = -1.0 if idx % 2 else 1.0
        y[start:end] += sign * marker[: end - start]
    y = np.clip(y, -0.95, 0.95)
    i16 = (y * 32767.0).astype(np.int16)
    stereo = np.column_stack([i16, i16]).reshape(-1)
    path = WORK_DIR / "stimulus.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(PLAY_FS)
        w.writeframes(stereo.tobytes())
    return path


class UdpRecorder:
    def __init__(self, name: str, port: int) -> None:
        self.name = name
        self.port = port
        self.chunks: list[bytes] = []
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", port))
        self._sock.settimeout(0.1)
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._sock.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data, _ = self._sock.recvfrom(65536)
            except TimeoutError:
                continue
            except OSError:
                break
            if data:
                self.chunks.append(data)

    def array(self) -> np.ndarray:
        if not self.chunks:
            return np.zeros(0, dtype=np.int16)
        return np.frombuffer(b"".join(self.chunks), dtype=np.int16).copy()


def rms_dbfs(x: np.ndarray) -> float:
    if x.size == 0:
        return float("-inf")
    xf = x.astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(xf * xf)))
    return 20.0 * math.log10(max(rms, 1e-12))


def norm(x: np.ndarray) -> np.ndarray:
    y = x.astype(np.float32)
    y -= float(np.mean(y)) if y.size else 0.0
    denom = float(np.std(y))
    if denom > 1e-6:
        y /= denom
    return y


def full_delay(ref: np.ndarray, mic: np.ndarray) -> dict[str, float]:
    n = min(ref.size, mic.size)
    if n < FS:
        return {"delay_ms": float("nan"), "confidence": 0.0}
    r = norm(ref[:n])
    m = norm(mic[:n])
    corr = signal.correlate(m, r, mode="full", method="fft")
    lags = signal.correlation_lags(m.size, r.size, mode="full")
    mask = (lags >= 0) & (lags <= SEARCH_SAMPLES)
    if not np.any(mask):
        return {"delay_ms": float("nan"), "confidence": 0.0}
    vals = np.abs(corr[mask])
    lag_vals = lags[mask]
    peak_i = int(np.argmax(vals))
    peak = float(vals[peak_i])
    confidence = peak / max(float(np.median(vals)), 1e-6)
    return {
        "delay_ms": float(lag_vals[peak_i]) * 1000.0 / FS,
        "confidence": confidence,
    }


def ref_events(ref: np.ndarray) -> np.ndarray:
    if ref.size < FS:
        return np.zeros(0, dtype=np.int64)
    env = np.abs(norm(ref))
    # Smooth to about 20 ms so each chirp becomes one broad peak.
    kernel = np.ones(max(1, int(0.020 * FS)), dtype=np.float32)
    smooth = np.convolve(env, kernel / kernel.size, mode="same")
    threshold = max(float(np.percentile(smooth, 90)) * 2.0, float(np.max(smooth)) * 0.25)
    peaks, props = signal.find_peaks(
        smooth,
        height=threshold,
        distance=int(0.80 * FS),
        prominence=max(threshold * 0.25, 0.05),
    )
    if peaks.size <= 1:
        peaks, props = signal.find_peaks(
            smooth,
            height=float(np.max(smooth)) * 0.15,
            distance=int(0.80 * FS),
        )
    return peaks.astype(np.int64)


def event_delays(ref: np.ndarray, mic: np.ndarray, events: np.ndarray) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    ref_pre = int(0.080 * FS)
    ref_post = int(0.260 * FS)
    for event in events:
        start = max(0, int(event) - ref_pre)
        ref_end = min(ref.size, int(event) + ref_post)
        mic_end = min(mic.size, ref_end + SEARCH_SAMPLES)
        if ref_end - start < int(0.080 * FS) or mic_end - start <= ref_end - start:
            continue
        r = norm(ref[start:ref_end])
        m = norm(mic[start:mic_end])
        corr = signal.correlate(m, r, mode="full", method="fft")
        lags = signal.correlation_lags(m.size, r.size, mode="full")
        mask = (lags >= 0) & (lags <= SEARCH_SAMPLES)
        vals = np.abs(corr[mask])
        lag_vals = lags[mask]
        if vals.size == 0:
            continue
        peak_i = int(np.argmax(vals))
        peak = float(vals[peak_i])
        out.append({
            "event_sec": float(event) / FS,
            "delay_ms": float(lag_vals[peak_i]) * 1000.0 / FS,
            "confidence": peak / max(float(np.median(vals)), 1e-6),
        })
    return out


def summarize_leg(ref: np.ndarray, mic: np.ndarray, events: np.ndarray) -> dict[str, object]:
    fd = full_delay(ref, mic)
    ed = event_delays(ref, mic, events)
    delays = np.array([item["delay_ms"] for item in ed], dtype=np.float32)
    confs = np.array([item["confidence"] for item in ed], dtype=np.float32)
    summary: dict[str, object] = {
        "samples": int(mic.size),
        "duration_sec": float(mic.size) / FS,
        "rms_dbfs": rms_dbfs(mic),
        "full_delay_ms": fd["delay_ms"],
        "full_confidence": fd["confidence"],
        "events_used": int(delays.size),
        "event_delays": ed,
    }
    if delays.size:
        x = np.array([item["event_sec"] for item in ed], dtype=np.float32)
        slope_ms_per_min = 0.0
        if delays.size >= 3 and float(np.ptp(x)) > 0.0:
            slope_ms_per_sec = float(np.polyfit(x, delays, 1)[0])
            slope_ms_per_min = slope_ms_per_sec * 60.0
        summary.update({
            "median_delay_ms": float(np.median(delays)),
            "p10_delay_ms": float(np.percentile(delays, 10)),
            "p90_delay_ms": float(np.percentile(delays, 90)),
            "jitter_std_ms": float(np.std(delays)),
            "drift_ms_per_min": slope_ms_per_min,
            "median_confidence": float(np.median(confs)) if confs.size else 0.0,
        })
    return summary


stimulus = make_stimulus()
with wave.open(str(stimulus)) as w:
    stimulus_sec = w.getnframes() / PLAY_FS


def run_trial(index: int) -> dict[str, object]:
    prefix = f"run_{index:02d}"
    recorders = [UdpRecorder(name, port) for name, port in LEGS.items()]
    for rec in recorders:
        rec.start()

    time.sleep(1.0)
    print(f"\n[{prefix}] playing {stimulus} via correction_substream")
    subprocess.run(["aplay", "-q", "-D", "correction_substream", str(stimulus)], check=True)
    time.sleep(max(1.0, DURATION_SEC - 1.0 - stimulus_sec))

    for rec in recorders:
        rec.stop()

    audio = {rec.name: rec.array() for rec in recorders}
    for name, arr in audio.items():
        write_wav(WORK_DIR / f"{prefix}_{name}.wav", arr)

    ref = audio["ref"]
    events = ref_events(ref)
    analysis: dict[str, object] = {
        "trial": index,
        "work_dir": str(WORK_DIR),
        "duration_sec": DURATION_SEC,
        "level_dbfs": LEVEL_DBFS,
        "search_ms": SEARCH_MS,
        "ref": {
            "samples": int(ref.size),
            "duration_sec": float(ref.size) / FS,
            "rms_dbfs": rms_dbfs(ref),
            "events_detected": int(events.size),
            "event_seconds": [float(x) / FS for x in events],
        },
        "legs": {},
    }
    for leg in ("usb_raw", "xvf_raw", "xvf_raw0", "xvf_aec"):
        analysis["legs"][leg] = summarize_leg(ref, audio[leg], events)

    with open(WORK_DIR / f"{prefix}_analysis.json", "w", encoding="utf-8") as f:
        json.dump(analysis, f, indent=2, sort_keys=True)

    print("Captured streams:")
    for name, arr in audio.items():
        print(f"  {name:8s} {arr.size / FS:6.2f}s  rms={rms_dbfs(arr):7.1f} dBFS")
    print(f"  ref events detected: {events.size}")

    print("Delay estimates vs bridge ref (positive means mic lags ref):")
    print("  leg       median   p10    p90   jitter  drift/min  conf   full")
    for leg in ("usb_raw", "xvf_raw", "xvf_raw0", "xvf_aec"):
        item = analysis["legs"][leg]
        if item["events_used"]:
            print(
                f"  {leg:8s}"
                f" {item['median_delay_ms']:7.1f}"
                f" {item['p10_delay_ms']:6.1f}"
                f" {item['p90_delay_ms']:6.1f}"
                f" {item['jitter_std_ms']:7.1f}"
                f" {item['drift_ms_per_min']:9.1f}"
                f" {item['median_confidence']:6.1f}"
                f" {item['full_delay_ms']:6.1f}"
            )
        else:
            print(
                f"  {leg:8s} no reliable per-event delay "
                f"(full={item['full_delay_ms']:.1f} ms, conf={item['full_confidence']:.1f})"
            )
    return analysis


analyses = [run_trial(i) for i in range(1, RUNS + 1)]
with open(WORK_DIR / "analysis.json", "w", encoding="utf-8") as f:
    json.dump({
        "work_dir": str(WORK_DIR),
        "duration_sec": DURATION_SEC,
        "level_dbfs": LEVEL_DBFS,
        "search_ms": SEARCH_MS,
        "runs": analyses,
    }, f, indent=2, sort_keys=True)

usb_delays = [
    float(item["legs"]["usb_raw"]["median_delay_ms"])
    for item in analyses
    if item["legs"]["usb_raw"]["events_used"]
]
if usb_delays:
    median = float(np.median(np.asarray(usb_delays, dtype=np.float32)))
    rounded = int(round(median / 10.0) * 10)
    print("\nUSB per-run median delays:", ", ".join(f"{x:.1f} ms" for x in usb_delays))
    print(f"Overall USB median delay: {median:.1f} ms")
    print(f"Suggested USB stream_delay_ms starting point: {rounded} ms")
    print("Treat this as a bulk-delay estimate; AEC3 may still prefer a nearby value.")
else:
    print("\nNo USB recommendation: not enough clean ref events were detected.")
print(f"\nWAVs + JSON saved on Pi: {WORK_DIR}")
PY
REMOTE
