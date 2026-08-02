#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Record a clip from the Pi's chip mic (XVF3800 ReSpeaker) at 16 kHz
# mono — the native format consumed by the wake-word and speech paths.
#
# This is the *processed* (beamformed + AGC + NS) channel that
# jasper-voice normally consumes; the chip's raw mic 0 is on a
# different channel and only available in the 6-ch firmware variant.
# The conference channel represents what the speaker's chip actually
# delivers to the wake-word detector.
#
# Usage:
#   bash scripts/capture-chip-mic.sh [seconds] [out.wav]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECONDS_=${1:-10}
TS=$(date +%Y%m%d-%H%M%S)
OUT=${2:-captures/chip-mic-${TS}.wav}
PI=${PI_HOST:-pi@${JASPER_HOSTNAME:-jts.local}}

mkdir -p "$(dirname "$OUT")"

echo "Capturing ${SECONDS_}s from Pi chip mic (XVF3800) → ${OUT}"
echo "(make noise within the next ${SECONDS_} seconds)"

ssh "$PI" "arecord -D plughw:CARD=Array,DEV=0 -f S16_LE -r 16000 -c 1 -d ${SECONDS_} -t wav 2>/dev/null" > "$OUT"

# Quick stats
python3 "${SCRIPT_DIR}/_wav_stats.py" --strict "$OUT"

echo "Wrote ${OUT}"
