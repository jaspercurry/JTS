#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Record a clip from the Pi's chip mic (XVF3800 ReSpeaker) at 16 kHz
# mono — same format the AMOLED satellite firmware emits, so the two
# can be compared apples-to-apples in Audacity / sox.
#
# This is the *processed* (beamformed + AGC + NS) channel that
# jasper-voice normally consumes; the chip's raw mic 0 is on a
# different channel and only available in the 6-ch firmware variant.
# For SNR comparison against the satellite, the conference channel is
# what we want anyway — it represents what the speaker's chip actually
# delivers to the wake-word detector.
#
# Usage:
#   bash scripts/capture-chip-mic.sh [seconds] [out.wav]

set -euo pipefail

SECONDS_=${1:-10}
TS=$(date +%Y%m%d-%H%M%S)
OUT=${2:-captures/chip-mic-${TS}.wav}
PI=${PI_HOST:-pi@${JASPER_HOSTNAME:-jts.local}}

mkdir -p "$(dirname "$OUT")"

echo "Capturing ${SECONDS_}s from Pi chip mic (XVF3800) → ${OUT}"
echo "(make noise within the next ${SECONDS_} seconds)"

ssh "$PI" "arecord -D plughw:CARD=Array,DEV=0 -f S16_LE -r 16000 -c 1 -d ${SECONDS_} -t wav 2>/dev/null" > "$OUT"

# Quick stats
python3 - <<PYEOF
import wave, math, struct
with wave.open('$OUT', 'rb') as w:
    n = w.getnframes()
    pcm = w.readframes(n)
samples = struct.unpack('<' + 'h'*n, pcm[:n*2])
mean = sum(samples)/n
rms = math.sqrt(sum((x-mean)**2 for x in samples)/n)
peak = max(abs(x) for x in samples)
print(f'  {n} samples ({n/16000:.2f}s)  mean={mean:+.1f}  RMS={rms:.0f} ({20*math.log10(max(rms,1e-9)/32768):+.1f} dBFS)  peak={peak} ({20*math.log10(max(peak,1e-9)/32768):+.1f} dBFS)')
PYEOF

echo "Wrote ${OUT}"
