#!/usr/bin/env bash
# Measure end-to-end ref-to-mic delay through the music chain.
#
# Plays a 200 ms log chirp via correction_substream (the private
# correction/probe fan-in lane, so it travels through the same path
# music does: snd-aloop → jasper-fanin → CamillaDSP → outputd → dongle
# → speakers → mic), captures both the outputd final-reference stream
# and the chip's ASR beam mic channel simultaneously, then
# cross-correlates to find the peak lag. The mic capture uses ALSA
# directly so the probe does not depend on PortAudio's device-name
# aliases.
#
# Result is the delay AEC3 should be hinted with (set as the
# constructor default in jasper_aec3/src/aec3_binding.cpp).
#
# Usage:
#   bash scripts/aec-probe-latency.sh
#   PI_HOST=192.168.1.42 bash scripts/aec-probe-latency.sh
#   MIC_CHANNEL=1 bash scripts/aec-probe-latency.sh  # chip_aec_210 beam
#
# Side effects: briefly stops shairport-sync, jasper-voice, and
# jasper-aec-bridge (the bridge holds the XVF capture endpoint). Restores
# only services that were active at entry. ~5 sec total disruption. Any
# active AirPlay session will drop and need to be re-established by the
# sender after the probe completes.

set -euo pipefail

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"
MIC_DEVICE="${MIC_DEVICE:-hw:CARD=Array,DEV=0}"
MIC_CHANNELS="${MIC_CHANNELS:-6}"
MIC_CHANNEL="${MIC_CHANNEL:-0}"
CAPTURE_SECONDS="${CAPTURE_SECONDS:-1.5}"
SEARCH_MS="${SEARCH_MS:-100}"
CHIRP_GAIN="${CHIRP_GAIN:-0.32}"
REF_UDP_HOST="${REF_UDP_HOST:-127.0.0.1}"
REF_UDP_PORT="${REF_UDP_PORT:-9891}"

remote_cmd=$(
  printf 'bash -s -- %q %q %q %q %q %q %q %q' \
    "${MIC_DEVICE}" "${MIC_CHANNELS}" "${MIC_CHANNEL}" \
    "${CAPTURE_SECONDS}" "${SEARCH_MS}" "${CHIRP_GAIN}" \
    "${REF_UDP_HOST}" "${REF_UDP_PORT}"
)

ssh "${PI_USER}@${PI_HOST}" "${remote_cmd}" <<'REMOTE'
set -euo pipefail

MIC_DEVICE="$1"
MIC_CHANNELS="$2"
MIC_CHANNEL="$3"
CAPTURE_SECONDS="$4"
SEARCH_MS="$5"
CHIRP_GAIN="$6"
REF_UDP_HOST="$7"
REF_UDP_PORT="$8"

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
    # The bridge intentionally carries StartLimitAction=reboot so real
    # runtime crash loops fail loudly. This probe may stop/start it several
    # times during a tuning session, so clear the rate-limit counter before
    # this operator-requested restore.
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
    # Restart (not just start) shairport-sync to force a fresh AP2 state.
    # Stopping shairport leaves clients (e.g. a Mac AirPlaying to JTS)
    # with a half-open AP2 session; on resume shairport sometimes refuses
    # new SETUPs. A clean restart guarantees the session state is reset.
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
  AEC_PROBE_MIC_CHANNEL="${MIC_CHANNEL}" \
  AEC_PROBE_CAPTURE_SECONDS="${CAPTURE_SECONDS}" \
  AEC_PROBE_SEARCH_MS="${SEARCH_MS}" \
  AEC_PROBE_CHIRP_GAIN="${CHIRP_GAIN}" \
  AEC_PROBE_REF_UDP_HOST="${REF_UDP_HOST}" \
  AEC_PROBE_REF_UDP_PORT="${REF_UDP_PORT}" \
  /opt/jasper/.venv/bin/python <<'PY'
import os
import sys
import time
import threading
import subprocess
import socket
import wave

import numpy as np
import alsaaudio
import scipy.signal as ss

mic_device = os.environ["AEC_PROBE_MIC_DEVICE"]
mic_channels = int(os.environ["AEC_PROBE_MIC_CHANNELS"])
mic_channel = int(os.environ["AEC_PROBE_MIC_CHANNEL"])
cap_dur = float(os.environ["AEC_PROBE_CAPTURE_SECONDS"])
search_ms = float(os.environ["AEC_PROBE_SEARCH_MS"])
chirp_gain = float(os.environ["AEC_PROBE_CHIRP_GAIN"])
ref_udp_host = os.environ["AEC_PROBE_REF_UDP_HOST"]
ref_udp_port = int(os.environ["AEC_PROBE_REF_UDP_PORT"])

if mic_channels <= 0:
    raise SystemExit("MIC_CHANNELS must be positive")
if mic_channel < 0 or mic_channel >= mic_channels:
    raise SystemExit(
        f"MIC_CHANNEL={mic_channel} is outside 0..{mic_channels - 1}"
    )
if cap_dur <= 0:
    raise SystemExit("CAPTURE_SECONDS must be positive")
if search_ms <= 0:
    raise SystemExit("SEARCH_MS must be positive")
if chirp_gain <= 0 or chirp_gain > 1:
    raise SystemExit("CHIRP_GAIN must be in (0, 1]")

# 200 ms log chirp 300 → 3000 Hz, -10 dBFS, stereo 48 kHz S16_LE.
# Log chirp gives a flat-ish spectrum across the band — good for
# cross-correlation precision.
fs_c, dur = 48000, 0.2
n = int(fs_c * dur)
t = np.arange(n) / fs_c
chirp = ss.chirp(t, f0=300, f1=3000, t1=dur, method="log")
fade = int(0.005 * fs_c)
chirp[:fade] *= np.linspace(0, 1, fade)
chirp[-fade:] *= np.linspace(1, 0, fade)
chirp_i16 = (chirp * chirp_gain * 32767).astype(np.int16)
stereo = np.column_stack([chirp_i16, chirp_i16]).flatten()
with wave.open("/tmp/aec-probe-chirp.wav", "wb") as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(fs_c)
    w.writeframes(stereo.tobytes())

# Capture one chip ASR beam from the 6-ch XVF capture device and
# outputd's final speaker-reference UDP stream.
fs = 16000
ref_buf, mic_buf = [], []
errors = []
error_lock = threading.Lock()

def capture_thread(label, fn):
    try:
        fn()
    except BaseException as exc:
        with error_lock:
            errors.append(f"{label}: {type(exc).__name__}: {exc}")

def mic_capture():
    pcm = alsaaudio.PCM(type=alsaaudio.PCM_CAPTURE, mode=alsaaudio.PCM_NORMAL,
                        device=mic_device, rate=fs, channels=mic_channels,
                        format=alsaaudio.PCM_FORMAT_S16_LE, periodsize=320)
    try:
        end = time.time() + cap_dur
        while time.time() < end:
            length, data = pcm.read()
            if length <= 0:
                continue
            arr = np.frombuffer(data, dtype=np.int16)
            frames = arr[: (arr.size // mic_channels) * mic_channels]
            if frames.size == 0:
                continue
            mic_buf.append(frames.reshape(-1, mic_channels)[:, mic_channel].copy())
    finally:
        pcm.close()

def append_ref_48k_stereo(data):
    arr = np.frombuffer(data, dtype=np.int16)
    if arr.size < 2:
        return
    usable = arr.size - (arr.size % 2)
    arr = arr[:usable]
    mono48 = ((arr[0::2].astype(np.float32) + arr[1::2].astype(np.float32)) * 0.5)
    append_ref_48k_mono(mono48)

def append_ref_48k_mono(mono48):
    append_ref_48k_mono.accum = np.concatenate([append_ref_48k_mono.accum, mono48])
    while append_ref_48k_mono.accum.size >= 960:
        chunk = append_ref_48k_mono.accum[:960]
        append_ref_48k_mono.accum = append_ref_48k_mono.accum[960:]
        ref_buf.append(ss.resample_poly(chunk, 1, 3).astype(np.int16))

append_ref_48k_mono.accum = np.empty(0, dtype=np.float32)

def ref_capture_outputd_udp():
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
                append_ref_48k_stereo(data)
    finally:
        sock.close()

mt = threading.Thread(
    target=capture_thread, args=("mic", mic_capture), daemon=True,
)
rt = threading.Thread(
    target=capture_thread, args=("ref", ref_capture_outputd_udp), daemon=True,
)
mt.start(); rt.start()
time.sleep(0.3)  # warmup before chirp
subprocess.run(["aplay", "-D", "correction_substream", "/tmp/aec-probe-chirp.wav"],
              check=True, capture_output=True)
mt.join(); rt.join()

if errors:
    for error in errors:
        print(f"capture error: {error}", file=sys.stderr)
    sys.exit(2)
if not ref_buf:
    print("capture error: no reference samples captured", file=sys.stderr)
    sys.exit(2)
if not mic_buf:
    print(
        f"capture error: no mic samples captured from {mic_device} "
        f"channel {mic_channel}/{mic_channels}",
        file=sys.stderr,
    )
    sys.exit(2)

ref = np.concatenate(ref_buf)
mic = np.concatenate(mic_buf)
print(f"ref={len(ref)} samples ({len(ref)/fs*1000:.0f} ms), "
      f"mic={len(mic)} samples ({len(mic)/fs*1000:.0f} ms)")
print(f"ref RMS={np.sqrt(np.mean(ref.astype(np.float32)**2)):.0f}, "
      f"mic RMS={np.sqrt(np.mean(mic.astype(np.float32)**2)):.0f}")
print(
    f"mic source: {mic_device} channel {mic_channel}/{mic_channels}; "
    f"ref source: outputd_udp {ref_udp_host}:{ref_udp_port}; "
    f"search window: 0-{search_ms:g} ms"
)

n_min = min(len(ref), len(mic))
if n_min < int(0.5 * fs):
    print(
        f"capture error: only {n_min / fs:.2f}s of overlapping samples",
        file=sys.stderr,
    )
    sys.exit(2)

ref_z = ref[:n_min].astype(np.float32) - ref[:n_min].mean()
mic_z = mic[:n_min].astype(np.float32) - mic[:n_min].mean()
xc = ss.correlate(mic_z, ref_z, mode="full")
lags = ss.correlation_lags(len(mic_z), len(ref_z), mode="full")
mask = (lags >= 0) & (lags <= int(search_ms / 1000.0 * fs))
xc_m = np.abs(xc[mask])
lags_m = lags[mask]
if xc_m.size == 0:
    print("capture error: empty correlation search window", file=sys.stderr)
    sys.exit(2)
peak = np.argmax(xc_m)
delay_samples = lags_m[peak]
delay_ms = delay_samples / fs * 1000
print(f"peak lag: {delay_samples} samples = {delay_ms:.1f} ms (mic vs ref)")
print(f"peak/median ratio: {xc_m[peak] / max(np.median(xc_m), 1):.1f}x  "
      f"(>3 is a clean peak)")
PY
REMOTE
