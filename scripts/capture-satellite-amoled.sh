#!/usr/bin/env bash
# Record a clip from the AMOLED satellite mic over USB-CDC.
#
# Reset the satellite (RTS toggle) → wait for [stream-start] → capture
# raw int16 PCM over /dev/ttyACM0 → write a 16 kHz mono WAV.
#
# The satellite must be:
#   - flashed with firmware/satellite-amoled (v0.0.3 or later)
#   - plugged into the Pi's USB-C
#
# Usage:
#   bash scripts/capture-satellite-amoled.sh [seconds] [out.wav]
#
# Examples:
#   bash scripts/capture-satellite-amoled.sh                # 10 s → captures/<ts>.wav
#   bash scripts/capture-satellite-amoled.sh 30             # 30 s
#   bash scripts/capture-satellite-amoled.sh 30 my.wav      # 30 s → my.wav
#
# Tips:
#   - Boot + warmup eats ~3 s before the marker fires; start making
#     noise (or position the device) once you see the "capturing"
#     line in the script's output.
#   - Compare against the chip mic with the same clip:
#       bash scripts/capture-chip-mic.sh 30
#     and align the two captures in Audacity / sox.

set -euo pipefail

SECONDS_=${1:-10}
TS=$(date +%Y%m%d-%H%M%S)
OUT=${2:-captures/satellite-amoled-${TS}.wav}
PI=${PI_HOST:-pi@${JASPER_HOSTNAME:-jts.local}}

mkdir -p "$(dirname "$OUT")"

echo "Capturing ${SECONDS_}s from satellite mic on ${PI} → ${OUT}"

ssh "$PI" "sudo /opt/jasper/.venv/bin/python - <<PYEOF
import serial, time, sys, wave
s = serial.Serial('/dev/ttyACM0', 115200, timeout=0.5)
s.dtr = False; s.rts = True; time.sleep(0.1); s.rts = False
s.reset_input_buffer()
buf = b''
deadline = time.time() + 5
M = b'[stream-start]'
while time.time() < deadline and buf.find(M) < 0:
    c = s.read(512)
    if c: buf += c
    else: time.sleep(0.02)
idx = buf.find(M)
if idx < 0:
    sys.stderr.write('no marker — is the satellite firmware running?\n')
    sys.exit(1)
eol = buf.find(b'\n', idx)
sys.stderr.write('boot ok; capturing now (make noise!)\n')
binary = buf[eol+1:]
TGT = 16000 * 2 * ${SECONDS_}
deadline = time.time() + ${SECONDS_} + 2
while len(binary) < TGT and time.time() < deadline:
    c = s.read(8192)
    if c: binary += c
binary = binary[:TGT]
s.close()
sys.stdout.buffer.write(binary)
PYEOF" \
  > /tmp/satellite-amoled-pcm.bin

# Wrap in WAV header on the laptop
python3 - <<PYEOF
import wave
with open('/tmp/satellite-amoled-pcm.bin', 'rb') as f:
    pcm = f.read()
with wave.open('$OUT', 'wb') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(pcm)
import math, struct
n = len(pcm) // 2
samples = struct.unpack('<' + 'h'*n, pcm[:n*2])
mean = sum(samples)/n
rms = math.sqrt(sum((x-mean)**2 for x in samples)/n)
peak = max(abs(x) for x in samples)
print(f'  {n} samples ({n/16000:.2f}s)  mean={mean:+.1f}  RMS={rms:.0f} ({20*math.log10(max(rms,1e-9)/32768):+.1f} dBFS)  peak={peak} ({20*math.log10(max(peak,1e-9)/32768):+.1f} dBFS)')
PYEOF

echo "Wrote ${OUT}"
