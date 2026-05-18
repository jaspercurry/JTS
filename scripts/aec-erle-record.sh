#!/usr/bin/env bash
# Capture simultaneous WAVs of the AEC bridge's input (raw chip mic
# ch1) and output (post-AEC UDP) so the analyser script can compute
# real ERLE numbers — including per-band so we can see whether the
# "attenuation" the bridge logs is broadband level drop (HPF + NS +
# MIC_GAIN math) or actual speech-band echo cancellation.
#
# Setup: play music at the listening level you care about. NO ONE
# TALKING during the capture — the analyser assumes the mic content
# is echo+ambient, not speech.
#
# Usage:
#   bash scripts/aec-erle-record.sh                 # 60 s capture
#   bash scripts/aec-erle-record.sh 90              # custom duration
#   PI_HOST=192.168.1.42 bash scripts/aec-erle-record.sh
#
# Outputs:
#   ./logs/aec-erle-<ts>/raw_array.wav    XVF capture, 16k, 6ch S16_LE
#   ./logs/aec-erle-<ts>/mic_ch1.wav      ch1 extracted (bridge input)
#   ./logs/aec-erle-<ts>/aec_output.wav   bridge UDP output post-MIC_GAIN
#   ./logs/aec-erle-<ts>/run.log          stdout + stderr of the Pi side

set -euo pipefail

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"
DURATION="${1:-60}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_LOCAL="$REPO_ROOT/logs/aec-erle-$TS"
OUT_REMOTE="/tmp/aec-erle-$TS"
mkdir -p "$OUT_LOCAL"

echo "ERLE capture starting:" >&2
echo "  Pi:       ${PI_USER}@${PI_HOST}" >&2
echo "  Duration: ${DURATION}s" >&2
echo "  Output:   $OUT_LOCAL/" >&2
echo "" >&2
echo "  ⚠  Play music at listening level. NO TALKING during capture." >&2
echo "" >&2

# Sanity check connectivity first so we don't sudo into a broken state.
if ! ssh -o BatchMode=yes -o ConnectTimeout=5 "${PI_USER}@${PI_HOST}" true; then
    echo "ERROR: cannot reach ${PI_USER}@${PI_HOST} over ssh" >&2
    exit 1
fi

# Pi-side capture. Heredoc runs as ${PI_USER}, escalates via sudo for
# systemctl and writes under /tmp (world-writable). The 6-ch arecord
# uses plughw:Array,0 because direct hw: refuses S16_LE on the 6-ch
# firmware (XVF's USB UAC2 advertises S32_LE natively).
ssh "${PI_USER}@${PI_HOST}" "sudo bash -s '$DURATION' '$OUT_REMOTE'" <<'REMOTE_SCRIPT' 2>&1 | tee "$OUT_LOCAL/run.log"
set -euo pipefail
DURATION="$1"
OUT="$2"
mkdir -p "$OUT"
chmod 0777 "$OUT"

# Pre-flight: bridge must be running for the UDP stream to exist.
BRIDGE_STATE="$(systemctl is-active jasper-aec-bridge.service 2>/dev/null || true)"
if [[ "$BRIDGE_STATE" != "active" ]]; then
    echo "ERROR: jasper-aec-bridge.service is '$BRIDGE_STATE' — start it" >&2
    echo "       first ('JASPER_AEC_MODE=auto' + reconciler) or use the" >&2
    echo "       /system/ dashboard's AEC3 card." >&2
    exit 1
fi

# Chip presence + capture ch sanity (6-ch firmware required for the
# bridge to be producing AEC output).
if ! aplay -l 2>/dev/null | grep -q 'Array'; then
    echo "ERROR: XVF Array card not visible to ALSA" >&2
    exit 1
fi

# Stop jasper-voice so UDP :9876 is free for our capture. The bridge
# keeps running because it sends-only (doesn't care if anyone listens).
echo "Stopping jasper-voice to free UDP :9876 ..."
systemctl stop jasper-voice.service

# Sanity sleep — voice's UDP socket close + bridge's next send window.
sleep 1

# Start both captures in parallel. socat reads N bytes from UDP and
# writes them straight to disk (no decoding — bridge sends raw 16k
# mono int16 LE, FRAME_SAMPLES=320 per packet = 640 bytes).
echo "Capturing for ${DURATION}s ..."
(
    socat -u UDP4-RECVFROM:9876,bind=127.0.0.1,fork=0,reuseaddr - \
        > "$OUT/aec_output.raw" 2>/dev/null &
    SOCAT_PID=$!
    arecord -D plughw:Array,0 -r 16000 -f S16_LE -c 6 -t wav \
        -d "$DURATION" "$OUT/raw_array.wav" 2>/dev/null &
    AREC_PID=$!

    # Let arecord run to completion; then kill socat. socat won't exit
    # on its own — it just keeps receiving.
    wait $AREC_PID
    kill $SOCAT_PID 2>/dev/null || true
    wait $SOCAT_PID 2>/dev/null || true
)

echo "Restarting jasper-voice ..."
systemctl start jasper-voice.service

# Wrap the raw UDP bytes in a WAV header for downstream tooling.
# socat may have written a few partial-frame leftovers; trim to a
# multiple of 2 (int16) so sox doesn't complain.
RAW_BYTES=$(stat -c %s "$OUT/aec_output.raw")
TRIM_BYTES=$(( RAW_BYTES - (RAW_BYTES % 2) ))
if (( TRIM_BYTES < RAW_BYTES )); then
    head -c "$TRIM_BYTES" "$OUT/aec_output.raw" > "$OUT/aec_output.raw.trim"
    mv "$OUT/aec_output.raw.trim" "$OUT/aec_output.raw"
fi

if ! command -v sox >/dev/null 2>&1; then
    apt-get install -y --no-install-recommends sox >/dev/null
fi
sox -r 16000 -c 1 -e signed-integer -b 16 \
    "$OUT/aec_output.raw" "$OUT/aec_output.wav"
sox "$OUT/raw_array.wav" "$OUT/mic_ch1.wav" remix 2

# Quick on-Pi sanity numbers so we know the capture isn't empty.
echo ""
echo "=== capture summary ==="
sox --i "$OUT/mic_ch1.wav" | grep -E 'Duration|Sample Rate|Channels'
echo ""
sox "$OUT/mic_ch1.wav" -n stats 2>&1 | grep -E 'RMS lev|Max level' || true
echo ""
sox --i "$OUT/aec_output.wav" | grep -E 'Duration|Sample Rate|Channels'
echo ""
sox "$OUT/aec_output.wav" -n stats 2>&1 | grep -E 'RMS lev|Max level' || true

rm -f "$OUT/aec_output.raw"
echo ""
echo "Capture done: $OUT/"
ls -la "$OUT/"
REMOTE_SCRIPT

# Pull artifacts back. mic_ch1.wav and aec_output.wav are the inputs
# the analyser wants; raw_array.wav is the full 6-ch in case we want
# to re-extract a different channel later.
echo "" >&2
echo "Pulling artifacts to $OUT_LOCAL/ ..." >&2
rsync -avz "${PI_USER}@${PI_HOST}:${OUT_REMOTE}/" "$OUT_LOCAL/" >&2

echo "" >&2
echo "Done. Analyse with:" >&2
echo "  python3 scripts/aec_erle_analyze.py \\" >&2
echo "    $OUT_LOCAL/mic_ch1.wav \\" >&2
echo "    $OUT_LOCAL/aec_output.wav" >&2
