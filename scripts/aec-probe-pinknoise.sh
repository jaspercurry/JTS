#!/usr/bin/env bash
# Run the AEC bridge with stationary pink noise as the far-end signal,
# log RMS attenuation per 5-sec window. Pink noise is AEC3's best case
# (stationary, broad-spectrum) — plateau attenuation here is the
# upper-bound performance for the engine in this setup.
#
# Compare with music-as-far-end results (which are typically -5 to
# -10 dB worse due to non-stationarity and speaker non-linearity).
#
# Usage:
#   bash scripts/aec-probe-pinknoise.sh
#   DURATION=60 bash scripts/aec-probe-pinknoise.sh
#   REF_GAIN_DB=20 bash scripts/aec-probe-pinknoise.sh
#   PI_HOST=192.168.1.42 bash scripts/aec-probe-pinknoise.sh
#
# Side effects: stops shairport-sync and jasper-voice for the run
# duration, restores at end. Plays loud-ish pink noise — main_volume
# (the dial-controlled software volume) is left alone, so noise level
# tracks whatever the dial is set to. Drop the dial first if you want
# this quieter.

set -euo pipefail

PI_HOST="${PI_HOST:-jts.local}"
PI_USER="${PI_USER:-pi}"
DURATION="${DURATION:-30}"
REF_GAIN_DB="${REF_GAIN_DB:-0}"

ssh "${PI_USER}@${PI_HOST}" "sudo systemctl stop shairport-sync jasper-voice; sleep 1
sudo /opt/jasper/.venv/bin/python <<EOF
import numpy as np, wave, scipy.signal as ss
fs, dur = 48000, ${DURATION}
n = int(fs * dur)
white = np.random.randn(n).astype(np.float32)
# Paul Kellet pink filter — 1/f spectrum, cheap and good enough for
# a far-end test signal.
b = np.array([0.049922035, -0.095993537, 0.050612699, -0.004408786])
a = np.array([1.0, -2.494956002, 2.017265875, -0.522189400])
pink = ss.lfilter(b, a, white)
pink /= np.abs(pink).max()
pink_i16 = (pink * (10 ** (-15 / 20)) * 32767).astype(np.int16)  # -15 dBFS RMS-ish
stereo = np.column_stack([pink_i16, pink_i16]).flatten()
with wave.open('/tmp/aec-probe-pink.wav', 'wb') as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(fs)
    w.writeframes(stereo.tobytes())
EOF
echo '---bridge in bg, aplay pink noise ${DURATION}s, ref gain=${REF_GAIN_DB} dB---'
sudo JASPER_AEC_ENGINE=webrtc3 JASPER_AEC_REF_GAIN_DB=${REF_GAIN_DB} \\
    timeout \$((${DURATION} + 6)) /opt/jasper/.venv/bin/jasper-aec-bridge \\
    > /tmp/aec-probe-bridge.log 2>&1 &
BG=\$!
sleep 3  # bridge warmup
sudo aplay -D plughw:Loopback,0,0 /tmp/aec-probe-pink.wav 2>&1 | tail -2
wait \$BG
echo '---bridge RMS log---'
grep -E 'rms|engine|ref capture' /tmp/aec-probe-bridge.log
sudo systemctl start jasper-voice shairport-sync"
