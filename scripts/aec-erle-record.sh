#!/usr/bin/env bash
# Capture the AEC bridge's mic input + AEC output + reference signal
# for offline ERLE analysis. Uses the bridge's own JASPER_AEC_DEBUG_RECORD_DIR
# debug mode (added in PR #150) — couldn't use a parallel `arecord` because
# the bridge holds the Array card exclusively via PortAudio.
#
# How it works:
#   1. Inject a transient systemd drop-in so the next bridge start sees
#      JASPER_AEC_DEBUG_RECORD_DIR=<somewhere in /tmp>
#   2. Restart jasper-aec-bridge — it opens 3 WAV writers and writes
#      mic_ch1 / aec_output / ref synchronously while continuing to
#      send UDP to jasper-voice (so wake-word still works during capture)
#   3. After the duration, delete the drop-in and restart the bridge
#      again to return to production state (WAVs closed cleanly on
#      SIGTERM via the `finally:` in _aec_loop)
#   4. rsync the captures back to ./logs/
#
# Setup: play music at the listening level you care about. NO ONE
# TALKING during the capture — the analyser assumes the mic content
# is echo + ambient, not speech.
#
# Usage:
#   bash scripts/aec-erle-record.sh                 # 60 s capture
#   bash scripts/aec-erle-record.sh 90              # custom duration
#   PI_HOST=192.168.1.42 bash scripts/aec-erle-record.sh
#
# Outputs:
#   ./logs/aec-erle-<ts>/mic_ch1.wav    bridge's mic input (16k mono S16_LE)
#   ./logs/aec-erle-<ts>/aec_output.wav AEC engine output pre-gain (16k mono)
#   ./logs/aec-erle-<ts>/ref.wav        bridge's ref signal post-L+R sum (16k mono)
#   ./logs/aec-erle-<ts>/run.log        stdout + stderr of the Pi side

set -euo pipefail

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"
DURATION="${1:-60}"
WARMUP="${WARMUP:-10}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_LOCAL="$REPO_ROOT/logs/aec-erle-$TS"
OUT_REMOTE="/tmp/aec-erle-$TS"
mkdir -p "$OUT_LOCAL"

echo "ERLE capture starting:" >&2
echo "  Pi:       ${PI_USER}@${PI_HOST}" >&2
echo "  Warmup:   ${WARMUP}s  (AEC3 convergence + chip state settle)" >&2
echo "  Capture:  ${DURATION}s  (meaningful window)" >&2
echo "  Output:   $OUT_LOCAL/" >&2
echo "" >&2
echo "  ⚠  Play music at listening level NOW. NO TALKING for the next" >&2
echo "  ⚠  ~$((WARMUP + DURATION + 10)) s while warmup + capture + cleanup run." >&2
echo "" >&2

if ! ssh -o BatchMode=yes -o ConnectTimeout=5 "${PI_USER}@${PI_HOST}" true; then
    echo "ERROR: cannot reach ${PI_USER}@${PI_HOST} over ssh" >&2
    exit 1
fi

ssh "${PI_USER}@${PI_HOST}" "sudo bash -s '$DURATION' '$WARMUP' '$OUT_REMOTE'" <<'REMOTE_SCRIPT' 2>&1 | tee "$OUT_LOCAL/run.log"
set -euo pipefail
DURATION="$1"
WARMUP="$2"
OUT="$3"

# Pre-flight
BRIDGE_STATE="$(systemctl is-active jasper-aec-bridge.service 2>/dev/null || true)"
if [[ "$BRIDGE_STATE" != "active" ]]; then
    echo "ERROR: jasper-aec-bridge.service is '$BRIDGE_STATE' — start it first" >&2
    echo "       (toggle AEC on at http://jts.local/system/ and re-run)" >&2
    exit 1
fi

mkdir -p "$OUT"
chmod 0777 "$OUT"

OVERRIDE_DIR=/run/systemd/system/jasper-aec-bridge.service.d
mkdir -p "$OVERRIDE_DIR"
cat > "$OVERRIDE_DIR/debug-record.conf" <<EOF
[Service]
Environment=JASPER_AEC_DEBUG_RECORD_DIR=$OUT
EOF

# Cleanup runs on any exit (success, error, signal) — guarantees the
# bridge ends up in production state and the WAVs are flushed.
cleanup() {
    echo "Cleanup: removing systemd override + restoring production bridge ..."
    rm -f "$OVERRIDE_DIR/debug-record.conf"
    rmdir "$OVERRIDE_DIR" 2>/dev/null || true
    systemctl daemon-reload
    systemctl restart jasper-aec-bridge.service
}
trap cleanup EXIT

systemctl daemon-reload
systemctl restart jasper-aec-bridge.service

echo "Bridge restarted with debug record on. Warmup ${WARMUP}s (AEC3 convergence) ..."
sleep "$WARMUP"

echo "Capturing ${DURATION}s ..."
sleep "$DURATION"

# Allow the in-flight frame to land before SIGTERM closes the WAVs.
sleep 1

echo ""
echo "=== capture summary ==="
ls -la "$OUT/" 2>&1
if ! command -v sox >/dev/null 2>&1; then
    apt-get install -y --no-install-recommends sox >/dev/null 2>&1 || true
fi
for f in mic_ch1.wav aec_output.wav ref.wav; do
    if [[ -f "$OUT/$f" ]]; then
        echo ""
        echo "--- $f ---"
        sox --i "$OUT/$f" 2>&1 | grep -E 'Duration|Sample Rate|Channels' || true
        sox "$OUT/$f" -n stats 2>&1 | grep -E 'RMS lev|Max level' || true
    fi
done
REMOTE_SCRIPT

echo "" >&2
echo "Pulling artifacts to $OUT_LOCAL/ ..." >&2
rsync -avz "${PI_USER}@${PI_HOST}:${OUT_REMOTE}/" "$OUT_LOCAL/" >&2

echo "" >&2
echo "Done. Files in $OUT_LOCAL/:" >&2
ls "$OUT_LOCAL/" >&2
echo "" >&2
echo "Analyse with:" >&2
echo "  python3 scripts/aec_erle_analyze.py \\" >&2
echo "    $OUT_LOCAL/mic_ch1.wav \\" >&2
echo "    $OUT_LOCAL/aec_output.wav" >&2
