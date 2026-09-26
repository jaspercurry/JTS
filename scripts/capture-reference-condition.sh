#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Capture one reference voice condition for the mic-quality-v2
# baseline (`reference-conditions/<condition>/`). Lands three
# time-aligned WAVs per condition (named to match the convention
# from scripts/wake-rate-test.sh):
#
#   aec-off.wav    raw chip mic 0 — pre-AEC (XVF ch 2 of 6-ch firmware)
#   aec-on.wav     post-AEC3 output — what jasper-voice consumes today
#   reference.wav  playback reference signal — the AEC far-end
#
# These form the stable A/B baseline for evaluating AEC engine
# swaps (DTLN-aec, RS-tuned AEC3, etc.) and wake-word model swaps
# offline. See docs/testing-tooling.md for the catalog of
# related scripts (scripts/wake-rate-test.sh covers the
# fixed-phone-track case; this script covers the live-user-speech
# case).
#
# The Pi-side recording is aec_debug_record_capture (scripts/_lib.sh).
#
# Usage:
#   bash scripts/capture-reference-condition.sh <condition> [seconds]
#
# Examples:
#   bash scripts/capture-reference-condition.sh whisper-quiet
#   bash scripts/capture-reference-condition.sh music-yell 40
#
# Music condition prerequisite: have music playing at your normal
# listening volume for ~5-10 s BEFORE invoking, so AEC3 has
# converged when the capture window opens.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
. "${SCRIPT_DIR}/_lib.sh"

if [[ $# -lt 1 ]]; then
    cat >&2 <<USAGE
Usage: $0 <condition> [seconds]
  conditions:
    normal-quiet / normal-music
    whisper-quiet / whisper-music
    yell-quiet / yell-music
    fast-quiet / fast-music
    slow-quiet / slow-music
  any label works — it's just a directory name under reference-conditions/.
  default seconds: 30
USAGE
    exit 2
fi

CONDITION="$1"
DURATION="${2:-30}"
case "$DURATION" in
    ''|*[!0-9]*) echo "duration must be a positive integer (seconds)" >&2; exit 2 ;;
esac

OUT_LOCAL="${REPO_ROOT}/reference-conditions/${CONDITION}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_REMOTE="/tmp/jts-refcap-${CONDITION}-${TS}"

# Archive any prior capture for this condition rather than clobber.
# Match wake-rate-test.sh's pattern.
if [[ -d "$OUT_LOCAL" ]] && find "$OUT_LOCAL" -maxdepth 1 -name '*.wav' -print -quit | grep -q .; then
    mv "$OUT_LOCAL" "${OUT_LOCAL}.prev.${TS}"
fi
mkdir -p "$OUT_LOCAL"

# Pre-capture state for the log so a future reader knows what
# bridge config the baseline was captured under.
PRE_STATE=$(ssh "${PI_USER}@${PI_HOST}" "
echo 'chip SHF_BYPASS:'
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host SHF_BYPASS 2>&1 | grep SHF_BYPASS || echo '  (xvf_host unavailable)'
echo 'bridge:'
systemctl is-active jasper-aec-bridge.service
echo 'aec_mode.env:'
sudo cat /var/lib/jasper/aec_mode.env 2>/dev/null || echo '  (default auto)'
echo 'jasper.env tuning:'
sudo grep -E '^JASPER_AEC_' /etc/jasper/jasper.env 2>/dev/null || echo '  (no overrides)'
")

cat <<HEADER

═══════════════════════════════════════════════════════
  Reference capture — condition: ${CONDITION}
═══════════════════════════════════════════════════════

  Output:    $OUT_LOCAL/
  Capture:   ${DURATION}s
  Pi:        ${PI_USER}@${PI_HOST}

$PRE_STATE

HEADER

if [[ "$CONDITION" == *music* ]]; then
    cat <<INFO
⚠ MUSIC condition prerequisite:
  Music must already be playing on the speaker at your normal
  listening volume. AEC3 needs ~5 s to converge — give it that
  before pressing Enter.

INFO
else
    echo "Speak naturally during the ${DURATION}-second window."
    echo ""
fi
read -r -p "Press Enter when ready..."

aec_debug_record_capture "$OUT_REMOTE" 5 "$DURATION" stop \
    "▶ SPEAK NOW — ${DURATION}-second capture window is open" \
    2>&1 | tee "$OUT_LOCAL/capture.log"

# Pull artifacts back
rsync -avz "${PI_USER}@${PI_HOST}:${OUT_REMOTE}/" "$OUT_LOCAL/"

# Rename to functional names matching scripts/wake-rate-test.sh's convention.
# Bridge writes its internal names; we rename downstream so analysis
# tooling sees consistent labels regardless of capture source.
[[ -f "$OUT_LOCAL/aec_output.wav" ]] && mv "$OUT_LOCAL/aec_output.wav" "$OUT_LOCAL/aec-on.wav"
[[ -f "$OUT_LOCAL/mic_ch1.wav"   ]] && mv "$OUT_LOCAL/mic_ch1.wav"   "$OUT_LOCAL/aec-off.wav"
[[ -f "$OUT_LOCAL/ref.wav"       ]] && mv "$OUT_LOCAL/ref.wav"       "$OUT_LOCAL/reference.wav"

# Pi-side cleanup
cleanup_remote_capture "/tmp/jts-refcap-*" "$OUT_REMOTE"

echo
echo ">>> sanity stats:"
PY="${REPO_ROOT}/.venv/bin/python"
[[ -x "$PY" ]] || PY=python3
"$PY" "${REPO_ROOT}/scripts/_wav_stats.py" "$OUT_LOCAL"

echo
echo "Done. ${OUT_LOCAL}"
