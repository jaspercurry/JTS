#!/usr/bin/env bash
# Wake-rate test harness: captures the bridge's AEC output while the
# operator plays a wake-rate test track on their phone, then runs
# openWakeWord OFFLINE on the captured audio to count detections.
#
# Why offline?
#  - MEASURE_PAUSE on the voice daemon drops mic frames before wake
#    detection, so wake events never fire — defeats the test.
#  - Running jasper-voice live opens a Gemini session per wake
#    (~$0.05) and plays a TTS response that contaminates the next
#    wake's audio via the speaker→mic path.
# Stopping jasper-voice + capturing the bridge output + counting
# wakes offline gives a clean number using the SAME model + threshold
# as production.
#
# Usage:
#   bash scripts/wake-rate-test.sh "AEC_ON_SHF_1"
#   bash scripts/wake-rate-test.sh "AEC_ON_SHF_0"
#   bash scripts/wake-rate-test.sh "AEC_OFF"
#
# Environment:
#   DURATION       seconds to capture (default 120 — covers 108s track + reaction)
#   THRESHOLD      override wake threshold (default reads from /etc/jasper/jasper.env)

set -euo pipefail

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"
LABEL="${1:-test}"
DURATION="${DURATION:-120}"
THRESHOLD="${THRESHOLD:-}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$REPO_ROOT/logs/wake-rate"
mkdir -p "$LOG_DIR"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_LOCAL="$LOG_DIR/${LABEL}-${TS}"
OUT_REMOTE="/tmp/wake-rate-${TS}"
mkdir -p "$OUT_LOCAL"

LOCAL_PY="$REPO_ROOT/scripts/_offline_wake_count.py"
LOCAL_TEMPLATE="$REPO_ROOT/logs/wake-test-track/jarvis.wav"
if [[ ! -f "$LOCAL_PY" ]]; then
    echo "ERROR: $LOCAL_PY missing" >&2
    exit 1
fi
scp -q "$LOCAL_PY" "${PI_USER}@${PI_HOST}:/tmp/_offline_wake_count.py"

# Copy the wake-test template too if we have one — enables the
# template-based per-utterance metadata mode (sees silent misses).
TEMPLATE_ARG=""
if [[ -f "$LOCAL_TEMPLATE" ]]; then
    scp -q "$LOCAL_TEMPLATE" "${PI_USER}@${PI_HOST}:/tmp/_wake_test_template.wav"
    TEMPLATE_ARG="--template /tmp/_wake_test_template.wav"
else
    echo "WARN: $LOCAL_TEMPLATE not present — will fall back to peak-only "
    echo "      detection (no silent-miss tracking). Run make-wake-test-track.sh first."
fi

# Capture pre-test state for the log
PRE_STATE=$(ssh "${PI_USER}@${PI_HOST}" "
echo 'chip SHF_BYPASS:'
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host SHF_BYPASS 2>&1 | grep SHF_BYPASS
echo 'bridge:'
systemctl is-active jasper-aec-bridge.service
echo 'aec_mode.env:'
cat /var/lib/jasper/aec_mode.env 2>/dev/null || echo '(default auto)'
")

cat <<HEADER

═══════════════════════════════════════════════════════
  Wake-rate test: $LABEL
═══════════════════════════════════════════════════════

  Output:    $OUT_LOCAL/
  Capture:   ${DURATION}s

$PRE_STATE

HEADER

# Run capture: stop jasper-voice (no sessions, no TTS), bridge in
# debug-record mode, wait DURATION, restore everything.
ssh "${PI_USER}@${PI_HOST}" "sudo bash -s '$DURATION' '$OUT_REMOTE'" <<'REMOTE_SCRIPT' 2>&1 | tee "$OUT_LOCAL/capture.log"
set -euo pipefail
DURATION="$1"
OUT="$2"

mkdir -p "$OUT"
chmod 0777 "$OUT"

OVERRIDE_DIR=/run/systemd/system/jasper-aec-bridge.service.d
mkdir -p "$OVERRIDE_DIR"
cat > "$OVERRIDE_DIR/debug-record.conf" <<EOF
[Service]
Environment=JASPER_AEC_DEBUG_RECORD_DIR=$OUT
EOF

cleanup() {
    echo "Cleanup: restoring jasper-voice + bridge to production state ..."
    rm -f "$OVERRIDE_DIR/debug-record.conf"
    rmdir "$OVERRIDE_DIR" 2>/dev/null || true
    systemctl daemon-reload
    systemctl restart jasper-aec-bridge.service
    systemctl start jasper-voice.service
}
trap cleanup EXIT

systemctl stop jasper-voice.service
systemctl daemon-reload
systemctl restart jasper-aec-bridge.service

echo "Bridge in debug-record; jasper-voice stopped. Warmup 10s ..."
sleep 10

echo ""
echo "  ▶ START THE PHONE TRACK NOW. Capturing for ${DURATION}s."
echo ""
sleep "$DURATION"

sleep 1
echo "Capture done."
REMOTE_SCRIPT

# Pull artifacts back
rsync -avz "${PI_USER}@${PI_HOST}:${OUT_REMOTE}/" "$OUT_LOCAL/"

# Run offline wake detection on the Pi (where openwakeword is installed).
# Pass threshold override if requested.
THRESH_ARG=""
[[ -n "$THRESHOLD" ]] && THRESH_ARG="--threshold $THRESHOLD"

echo ""
echo "Running offline wake-word detection on aec_output.wav ..."
WAKE_RESULT=$(ssh "${PI_USER}@${PI_HOST}" \
    "sudo /opt/jasper/.venv/bin/python /tmp/_offline_wake_count.py \
        $TEMPLATE_ARG $THRESH_ARG '${OUT_REMOTE}/aec_output.wav'" 2>&1)

# Save + display
{
  echo "Wake-rate test result"
  echo "Label:        $LABEL"
  echo "Capture:      ${DURATION}s"
  echo ""
  echo "$PRE_STATE"
  echo ""
  echo "─── Offline wake detection on aec_output.wav ───"
  echo "$WAKE_RESULT"
} | tee "$OUT_LOCAL/result.txt"

# Also detect on the raw mic (pre-AEC) for comparison — answers
# "would chip-direct mic have worked?" without needing a separate
# capture.
echo ""
echo "─── Offline wake detection on mic_ch1.wav (chip raw, pre-AEC) ───"
RAW_RESULT=$(ssh "${PI_USER}@${PI_HOST}" \
    "sudo /opt/jasper/.venv/bin/python /tmp/_offline_wake_count.py \
        $TEMPLATE_ARG $THRESH_ARG '${OUT_REMOTE}/mic_ch1.wav'" 2>&1)
echo "$RAW_RESULT" | tee -a "$OUT_LOCAL/result.txt"

echo ""
echo "Files: $OUT_LOCAL/"
ls "$OUT_LOCAL/"
