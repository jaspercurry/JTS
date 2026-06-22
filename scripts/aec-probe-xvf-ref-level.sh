#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Probe XVF3800 chip-reference format, level, and output-channel behavior.
#
# This is a diagnostic-only hardware probe. It briefly stops the bridge and
# voice services so it can bind outputd's reference UDP port and open the XVF
# capture endpoint directly. It does not write or persist XVF chip parameters.
#
# The stimulus travels through the normal production output path:
#   correction_substream -> fanin -> CamillaDSP -> outputd -> DAC/speaker
# and, when chip-AEC is active, outputd also feeds the same final reference to
# the XVF USB-IN chip-reference PCM.
#
# Usage:
#   bash scripts/aec-probe-xvf-ref-level.sh
#   PI_HOST=192.168.1.74 CHIRP_GAIN=0.18 bash scripts/aec-probe-xvf-ref-level.sh
#
# Tunables:
#   CAPTURE_SECONDS=2.2
#   PROBE_TIMEOUT_SECONDS=15

set -euo pipefail

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"
MIC_DEVICE="${MIC_DEVICE:-hw:CARD=Array,DEV=0}"
MIC_CHANNELS="${MIC_CHANNELS:-6}"
CAPTURE_SECONDS="${CAPTURE_SECONDS:-2.2}"
SEARCH_MS="${SEARCH_MS:-120}"
CHIRP_GAIN="${CHIRP_GAIN:-0.18}"
REF_UDP_HOST="${REF_UDP_HOST:-127.0.0.1}"
REF_UDP_PORT="${REF_UDP_PORT:-9891}"
PROBE_TIMEOUT_SECONDS="${PROBE_TIMEOUT_SECONDS:-15}"

remote_cmd=$(
  printf 'bash -s -- %q %q %q %q %q %q %q %q' \
    "${MIC_DEVICE}" "${MIC_CHANNELS}" "${CAPTURE_SECONDS}" \
    "${SEARCH_MS}" "${CHIRP_GAIN}" "${REF_UDP_HOST}" "${REF_UDP_PORT}" \
    "${PROBE_TIMEOUT_SECONDS}"
)

ssh "${PI_USER}@${PI_HOST}" "${remote_cmd}" <<'REMOTE'
set -euo pipefail

MIC_DEVICE="$1"
MIC_CHANNELS="$2"
CAPTURE_SECONDS="$3"
SEARCH_MS="$4"
CHIRP_GAIN="$5"
REF_UDP_HOST="$6"
REF_UDP_PORT="$7"
PROBE_TIMEOUT_SECONDS="$8"

shairport_was_active=0
nqptp_was_active=0
voice_was_active=0
bridge_was_active=0

unit_active() {
  sudo systemctl is-active --quiet "$1"
}

stop_if_active() {
  local unit="$1"
  local state_var="$2"
  if unit_active "${unit}"; then
    printf -v "${state_var}" '1'
    sudo systemctl stop "${unit}"
  fi
}

restore_services() {
  local restore_rc=0
  set +e
  if [[ "${bridge_was_active}" == "1" ]]; then
    sudo systemctl reset-failed jasper-aec-bridge.service || restore_rc=1
    sudo systemctl start jasper-aec-bridge.service || restore_rc=1
  fi
  if [[ "${voice_was_active}" == "1" ]]; then
    sudo systemctl start jasper-voice.service || restore_rc=1
  fi
  if [[ "${nqptp_was_active}" == "1" ]]; then
    sudo systemctl restart nqptp.service || restore_rc=1
  fi
  if [[ "${shairport_was_active}" == "1" ]]; then
    sudo systemctl restart shairport-sync.service || restore_rc=1
  fi
  return "${restore_rc}"
}

on_exit() {
  local rc=$?
  restore_services
  local restore_rc=$?
  if [[ "${rc}" -eq 0 ]]; then
    exit "${restore_rc}"
  fi
  exit "${rc}"
}
trap on_exit EXIT

stop_if_active shairport-sync.service shairport_was_active
stop_if_active jasper-voice.service voice_was_active
stop_if_active jasper-aec-bridge.service bridge_was_active
if unit_active nqptp.service; then
  nqptp_was_active=1
fi
sleep 1

sudo env \
  AEC_PROBE_MIC_DEVICE="${MIC_DEVICE}" \
  AEC_PROBE_MIC_CHANNELS="${MIC_CHANNELS}" \
  AEC_PROBE_CAPTURE_SECONDS="${CAPTURE_SECONDS}" \
  AEC_PROBE_SEARCH_MS="${SEARCH_MS}" \
  AEC_PROBE_CHIRP_GAIN="${CHIRP_GAIN}" \
  AEC_PROBE_REF_UDP_HOST="${REF_UDP_HOST}" \
  AEC_PROBE_REF_UDP_PORT="${REF_UDP_PORT}" \
  AEC_PROBE_TIMEOUT_SECONDS="${PROBE_TIMEOUT_SECONDS}" \
  timeout --kill-after=2s "${PROBE_TIMEOUT_SECONDS}s" \
  /opt/jasper/.venv/bin/python <<'PY'
import math
import os
import socket
import subprocess
import sys
import threading
import time
import wave

import alsaaudio
import numpy as np
import scipy.signal as ss

mic_device = os.environ["AEC_PROBE_MIC_DEVICE"]
mic_channels = int(os.environ["AEC_PROBE_MIC_CHANNELS"])
cap_dur = float(os.environ["AEC_PROBE_CAPTURE_SECONDS"])
search_ms = float(os.environ["AEC_PROBE_SEARCH_MS"])
chirp_gain = float(os.environ["AEC_PROBE_CHIRP_GAIN"])
ref_udp_host = os.environ["AEC_PROBE_REF_UDP_HOST"]
ref_udp_port = int(os.environ["AEC_PROBE_REF_UDP_PORT"])
probe_timeout = float(os.environ["AEC_PROBE_TIMEOUT_SECONDS"])

if mic_channels <= 0:
    raise SystemExit("MIC_CHANNELS must be positive")
if cap_dur <= 0:
    raise SystemExit("CAPTURE_SECONDS must be positive")
if search_ms <= 0:
    raise SystemExit("SEARCH_MS must be positive")
if not (0 < chirp_gain <= 1):
    raise SystemExit("CHIRP_GAIN must be in (0, 1]")
if probe_timeout <= cap_dur + 2.0:
    raise SystemExit("PROBE_TIMEOUT_SECONDS must be at least CAPTURE_SECONDS + 2")

def rms(arr):
    if arr.size == 0:
        return 0.0
    f = arr.astype(np.float64)
    return float(np.sqrt(np.mean(f * f)))

def dbfs(value):
    if value <= 0:
        return float("-inf")
    return 20.0 * math.log10(value / 32768.0)

def xvf_read(name):
    cmd = [
        "/opt/jasper/.venv/bin/python",
        "-m",
        "jasper.xvf.xvf_host",
        name,
    ]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as exc:
        return f"ERROR: {exc.output.strip()}"
    for line in out.splitlines():
        if line.startswith(f"{name}:"):
            return line.split(":", 1)[1].strip()
    return out.strip().replace("\n", " ")

profile_params = [
    "SHF_BYPASS",
    "AUDIO_MGR_SYS_DELAY",
    "AUDIO_MGR_REF_GAIN",
    "AEC_FAR_EXTGAIN",
    "AEC_ASROUTONOFF",
    "AEC_FIXEDBEAMSONOFF",
    "AEC_FIXEDBEAMSGATING",
    "AEC_AECEMPHASISONOFF",
    "AUDIO_MGR_OP_L",
    "AUDIO_MGR_OP_R",
    "AEC_AECCONVERGED",
]
profile = {name: xvf_read(name) for name in profile_params}

fs48 = 48000
dur = 0.45
n = int(fs48 * dur)
t = np.arange(n) / fs48
chirp = ss.chirp(t, f0=250, f1=4200, t1=dur, method="log")
fade = int(0.008 * fs48)
chirp[:fade] *= np.linspace(0, 1, fade)
chirp[-fade:] *= np.linspace(1, 0, fade)
tone = 0.45 * np.sin(2 * np.pi * 1000 * t)
stim = 0.75 * chirp + 0.25 * tone
stim = stim / max(np.max(np.abs(stim)), 1e-9)
stim_i16 = (stim * chirp_gain * 32767.0).astype(np.int16)
stereo = np.column_stack([stim_i16, stim_i16]).reshape(-1)
with wave.open("/tmp/aec-xvf-ref-level.wav", "wb") as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(fs48)
    w.writeframes(stereo.tobytes())

ref_packets = []
mic_packets = []
errors = []
error_lock = threading.Lock()

def capture_thread(label, fn):
    try:
        fn()
    except BaseException as exc:
        with error_lock:
            errors.append(f"{label}: {type(exc).__name__}: {exc}")

def mic_capture():
    pcm = alsaaudio.PCM(
        type=alsaaudio.PCM_CAPTURE,
        mode=alsaaudio.PCM_NORMAL,
        device=mic_device,
        rate=16000,
        channels=mic_channels,
        format=alsaaudio.PCM_FORMAT_S16_LE,
        periodsize=320,
    )
    try:
        end = time.time() + cap_dur
        while time.time() < end:
            length, data = pcm.read()
            if length <= 0:
                continue
            arr = np.frombuffer(data, dtype=np.int16)
            usable = arr.size - (arr.size % mic_channels)
            if usable <= 0:
                continue
            mic_packets.append(arr[:usable].reshape(-1, mic_channels).copy())
    finally:
        pcm.close()

def ref_capture():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((ref_udp_host, ref_udp_port))
        sock.settimeout(0.1)
        end = time.time() + cap_dur
        while time.time() < end:
            try:
                data, _addr = sock.recvfrom(65536)
            except socket.timeout:
                continue
            if data:
                arr = np.frombuffer(data, dtype=np.int16)
                usable = arr.size - (arr.size % 2)
                if usable > 0:
                    ref_packets.append(arr[:usable].reshape(-1, 2).copy())
    finally:
        sock.close()

mt = threading.Thread(target=capture_thread, args=("mic", mic_capture), daemon=True)
rt = threading.Thread(target=capture_thread, args=("ref", ref_capture), daemon=True)
mt.start()
rt.start()
time.sleep(0.35)
subprocess.run(
    ["aplay", "-D", "correction_substream", "/tmp/aec-xvf-ref-level.wav"],
    check=True,
    capture_output=True,
    timeout=max(cap_dur + 3.0, 5.0),
)
mt.join()
rt.join()

if errors:
    for error in errors:
        print(f"capture error: {error}", file=sys.stderr)
    sys.exit(2)
if not ref_packets:
    print("capture error: no outputd UDP reference packets captured", file=sys.stderr)
    sys.exit(2)
if not mic_packets:
    print("capture error: no XVF capture packets captured", file=sys.stderr)
    sys.exit(2)

ref48 = np.vstack(ref_packets)
mic = np.vstack(mic_packets)
left48 = ref48[:, 0]
right48 = ref48[:, 1]
mono48 = ((left48.astype(np.float32) + right48.astype(np.float32)) * 0.5)
mono16 = ss.resample_poly(mono48, 1, 3).astype(np.int16)

print("xvf_profile:")
for name in profile_params:
    print(f"  {name}={profile[name]}")
print("reference_udp_48k:")
for label, arr in (("left", left48), ("right", right48)):
    peak = int(np.max(np.abs(arr.astype(np.int32)))) if arr.size else 0
    clips = int(np.count_nonzero(np.abs(arr.astype(np.int32)) >= 32767))
    r = rms(arr)
    print(
        f"  {label}: samples={arr.size} rms={r:.1f} ({dbfs(r):.1f} dBFS) "
        f"peak={peak} clips={clips}"
    )
l_r_delta = dbfs(max(rms(left48), 1e-12)) - dbfs(max(rms(right48), 1e-12))
print(f"  left_right_rms_delta_db={l_r_delta:.2f}")
mono_rms = rms(mono16)
mono_peak = int(np.max(np.abs(mono16.astype(np.int32)))) if mono16.size else 0
print(
    f"chip_ref_model_16k_mono: samples={mono16.size} rms={mono_rms:.1f} "
    f"({dbfs(mono_rms):.1f} dBFS) peak={mono_peak}"
)
try:
    ref_gain = float(profile["AUDIO_MGR_REF_GAIN"].strip("[]").split(",")[0])
    print(
        f"chip_ref_after_AUDIO_MGR_REF_GAIN: linear_gain={ref_gain:.3f} "
        f"estimated_rms={mono_rms * ref_gain:.1f} "
        f"({dbfs(mono_rms * ref_gain):.1f} dBFS)"
    )
except ValueError:
    pass

n_min = min(len(mono16), len(mic))
ref_z = mono16[:n_min].astype(np.float32)
ref_z -= ref_z.mean()
max_lag = int(search_ms / 1000.0 * 16000)
print("xvf_capture_channels:")
for ch in range(mic.shape[1]):
    arr = mic[:n_min, ch]
    r = rms(arr)
    peak = int(np.max(np.abs(arr.astype(np.int32)))) if arr.size else 0
    clips = int(np.count_nonzero(np.abs(arr.astype(np.int32)) >= 32767))
    mic_z = arr.astype(np.float32)
    mic_z -= mic_z.mean()
    xc = ss.correlate(mic_z, ref_z, mode="full")
    lags = ss.correlation_lags(len(mic_z), len(ref_z), mode="full")
    mask = (lags >= 0) & (lags <= max_lag)
    xc_m = np.abs(xc[mask])
    lags_m = lags[mask]
    if xc_m.size:
        peak_idx = int(np.argmax(xc_m))
        lag = int(lags_m[peak_idx])
        ratio = float(xc_m[peak_idx] / max(np.median(xc_m), 1.0))
    else:
        lag = -1
        ratio = 0.0
    print(
        f"  ch{ch}: rms={r:.1f} ({dbfs(r):.1f} dBFS) peak={peak} clips={clips} "
        f"corr_lag_samples={lag} corr_lag_ms={lag / 16.0 if lag >= 0 else -1:.1f} "
        f"peak_median={ratio:.1f}"
    )
PY
REMOTE
