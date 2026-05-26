#!/usr/bin/env bash
# Measure end-to-end ref-to-mic delay through the music chain.
#
# Plays a 200 ms log chirp via correction_substream (the private
# correction/probe fan-in lane, so it travels through the same path
# music does: snd-aloop → jasper-fanin → CamillaDSP → dmix → dongle
# → speakers → mic), captures both the digital reference
# (via pcm.jasper_capture dsnoop) and the chip's ASR beam mic channel
# simultaneously, then cross-correlates to find the peak lag.
#
# Result is the delay AEC3 should be hinted with (set as the
# constructor default in jasper_aec3/src/aec3_binding.cpp).
#
# Usage:
#   bash scripts/aec-probe-latency.sh
#   PI_HOST=192.168.1.42 bash scripts/aec-probe-latency.sh
#
# Side effects: briefly stops shairport-sync and jasper-voice (both
# hold devices the probe needs). Restores them at the end. ~5 sec
# total disruption. Any active AirPlay session will drop and need
# to be re-established by the sender after the probe completes.

set -euo pipefail

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"

ssh "${PI_USER}@${PI_HOST}" 'sudo systemctl stop shairport-sync jasper-voice; sleep 1
sudo /opt/jasper/.venv/bin/python <<EOF
import time, threading, subprocess, wave
import numpy as np, alsaaudio, sounddevice as sd
import scipy.signal as ss

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
chirp_i16 = (chirp * 0.32 * 32767).astype(np.int16)
stereo = np.column_stack([chirp_i16, chirp_i16]).flatten()
with wave.open("/tmp/aec-probe-chirp.wav", "wb") as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(fs_c)
    w.writeframes(stereo.tobytes())

# Capture for 1.5 sec, both ref (jasper_capture, 48k stereo
# downsampled to 16k mono left) and mic (chip ch 1, 16k ASR beam).
fs = 16000
cap_dur = 1.5
ref_buf, mic_buf = [], []

def mic_th():
    def cb(indata, frames, ti, status):
        mic_buf.append(indata[:, 1].copy())
    with sd.InputStream(device="Array", samplerate=fs, channels=6,
                       dtype="int16", blocksize=320, callback=cb):
        time.sleep(cap_dur)

def ref_th():
    pcm = alsaaudio.PCM(type=alsaaudio.PCM_CAPTURE, mode=alsaaudio.PCM_NORMAL,
                        device="jasper_capture", rate=48000, channels=2,
                        format=alsaaudio.PCM_FORMAT_S16_LE, periodsize=960)
    end = time.time() + cap_dur
    accum = np.empty(0, dtype=np.float32)
    while time.time() < end:
        length, data = pcm.read()
        if length > 0:
            arr = np.frombuffer(data, dtype=np.int16)
            left48 = arr[::2].astype(np.float32)
            accum = np.concatenate([accum, left48])
            while accum.size >= 960:
                chunk = accum[:960]; accum = accum[960:]
                ref_buf.append(ss.resample_poly(chunk, 1, 3).astype(np.int16))
    pcm.close()

mt = threading.Thread(target=mic_th, daemon=True); mt.start()
rt = threading.Thread(target=ref_th, daemon=True); rt.start()
time.sleep(0.3)  # warmup before chirp
subprocess.run(["aplay", "-D", "correction_substream", "/tmp/aec-probe-chirp.wav"],
              check=True, capture_output=True)
mt.join(); rt.join()

ref = np.concatenate(ref_buf) if ref_buf else np.zeros(0, dtype=np.int16)
mic = np.concatenate(mic_buf) if mic_buf else np.zeros(0, dtype=np.int16)
print(f"ref={len(ref)} samples ({len(ref)/fs*1000:.0f} ms), "
      f"mic={len(mic)} samples ({len(mic)/fs*1000:.0f} ms)")
print(f"ref RMS={np.sqrt(np.mean(ref.astype(np.float32)**2)):.0f}, "
      f"mic RMS={np.sqrt(np.mean(mic.astype(np.float32)**2)):.0f}")

n_min = min(len(ref), len(mic))
ref_z = ref[:n_min].astype(np.float32) - ref[:n_min].mean()
mic_z = mic[:n_min].astype(np.float32) - mic[:n_min].mean()
xc = ss.correlate(mic_z, ref_z, mode="full")
lags = ss.correlation_lags(len(mic_z), len(ref_z), mode="full")
mask = (lags >= 0) & (lags <= int(0.1 * fs))  # search window: 0-100 ms
xc_m = np.abs(xc[mask])
lags_m = lags[mask]
peak = np.argmax(xc_m)
delay_samples = lags_m[peak]
delay_ms = delay_samples / fs * 1000
print(f"peak lag: {delay_samples} samples = {delay_ms:.1f} ms (mic vs ref)")
print(f"peak/median ratio: {xc_m[peak] / max(np.median(xc_m), 1):.1f}x  "
      f"(>3 is a clean peak)")
EOF
sudo systemctl start jasper-voice
# Restart (not just start) shairport-sync to force a fresh AP2 state.
# Stopping shairport leaves clients (e.g. a Mac AirPlaying to JTS)
# with a half-open AP2 session; on resume shairport sometimes refuses
# new SETUPs. A clean restart guarantees the session state is reset.
# nqptp restarts too because AP2 PTP can desync from shairport.
sudo systemctl restart shairport-sync nqptp'
