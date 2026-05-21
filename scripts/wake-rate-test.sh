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
#   bash scripts/wake-rate-test.sh 1                                   # default phrase + model (Jarvis + jarvis_v2)
#   bash scripts/wake-rate-test.sh 2                                   # capture test 2
#   SESSION=evening bash scripts/wake-rate-test.sh 1                   # custom session
#   PHRASE="Hey Buddy" MODEL=/var/lib/jasper/wake/hey_buddy_en_medium.onnx \
#     bash scripts/wake-rate-test.sh hey-buddy-1                       # alt phrase + model
#
# Captures live under logs/wake-rate/<session>/test-<N>/
#   aec-on.wav      what voice consumes with AEC enabled (post-AEC)
#   aec-off.wav     what voice consumes with AEC disabled (chip raw mic, pre-AEC)
#   reference.wav   the music reference signal AEC subtracts
#   result.txt      wake counts (AEC ON vs AEC OFF) + chip/bridge state + phrase/model
#   capture.log     pi-side log
#
# Within one capture, aec-on.wav vs aec-off.wav compares the SAME mic
# input — only the bridge processing differs. Run the script 2-3 times
# (test 1, test 2, test 3) for replicate captures.
#
# Environment:
#   SESSION        session folder name (default: today's UTC date)
#   PHRASE         wake phrase (default: Jarvis). Used to find the
#                  per-utterance template at
#                  logs/wake-test-track/<slug>/<slug>.wav (xcorr finder)
#   MODEL          Pi-side path to a wake-word ONNX (overrides default
#                  jarvis_v2.onnx). E.g. /var/lib/jasper/wake/hey_buddy_en_medium.onnx
#   DURATION       seconds to capture (default 120 — covers 108s track + reaction)
#   THRESHOLD      override wake threshold (default reads from /etc/jasper/jasper.env)

set -euo pipefail

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"
TEST_NUM="${1:-1}"
SESSION="${SESSION:-$(date -u +%Y-%m-%d)}"
DURATION="${DURATION:-120}"
THRESHOLD="${THRESHOLD:-}"
PHRASE="${PHRASE:-Jarvis}"
SLUG=$(echo "$PHRASE" | tr '[:upper:] ' '[:lower:]-')
# MODEL — Pi-side path to the wake-word ONNX. Empty = let
# _offline_wake_count.py use its compiled-in default (jarvis_v2).
MODEL="${MODEL:-}"

# Accept "1", "test-1", or "test1" — normalize to "test-N"
case "$TEST_NUM" in
    test-*) TEST_LABEL="$TEST_NUM" ;;
    test*) TEST_LABEL="test-${TEST_NUM#test}" ;;
    *) TEST_LABEL="test-${TEST_NUM}" ;;
esac

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$REPO_ROOT/logs/wake-rate/${SESSION}"
mkdir -p "$LOG_DIR"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_LOCAL="$LOG_DIR/${TEST_LABEL}"
OUT_REMOTE="/tmp/wake-rate-${SESSION}-${TEST_LABEL}-${TS}"

# If a prior run of this test exists, archive it rather than clobber
if [[ -d "$OUT_LOCAL" ]]; then
    mv "$OUT_LOCAL" "${OUT_LOCAL}.prev.${TS}"
fi
mkdir -p "$OUT_LOCAL"

LOCAL_PY="$REPO_ROOT/scripts/_offline_wake_count.py"
# Template path for the cross-correlation utterance finder. Prefer the
# per-phrase subdirectory (logs/wake-test-track/<slug>/<slug>.wav) if
# present; fall back to the legacy flat path (logs/wake-test-track/<slug>.wav)
# for backward compat with pre-2026-05-21 layouts.
LOCAL_TEMPLATE="$REPO_ROOT/logs/wake-test-track/${SLUG}/${SLUG}.wav"
if [[ ! -f "$LOCAL_TEMPLATE" ]]; then
    LOCAL_TEMPLATE="$REPO_ROOT/logs/wake-test-track/${SLUG}.wav"
fi
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
sudo cat /var/lib/jasper/aec_mode.env 2>/dev/null || echo '(default auto)'
")

cat <<HEADER

═══════════════════════════════════════════════════════
  Wake-rate test — session: $SESSION / $TEST_LABEL
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

# Rename Pi-side debug-record outputs to intuitive labels for easy
# listening. mic_ch1 = chip-direct mic (pre-AEC = "AEC OFF" leg);
# aec_output = post-AEC ("AEC ON" leg); ref = music reference.
[[ -f "$OUT_LOCAL/aec_output.wav" ]] && mv "$OUT_LOCAL/aec_output.wav" "$OUT_LOCAL/aec-on.wav"
[[ -f "$OUT_LOCAL/mic_ch1.wav"   ]] && mv "$OUT_LOCAL/mic_ch1.wav"   "$OUT_LOCAL/aec-off.wav"
[[ -f "$OUT_LOCAL/ref.wav"       ]] && mv "$OUT_LOCAL/ref.wav"       "$OUT_LOCAL/reference.wav"

# Run offline wake detection on the Pi (where openwakeword is installed).
# Pass threshold + model overrides if requested.
THRESH_ARG=""
[[ -n "$THRESHOLD" ]] && THRESH_ARG="--threshold $THRESHOLD"
MODEL_ARG=""
[[ -n "$MODEL" ]] && MODEL_ARG="--model $MODEL"

echo ""
echo "Running offline wake-word detection on AEC ON output (phrase='$PHRASE', model=${MODEL:-default}) ..."
WAKE_RESULT=$(ssh "${PI_USER}@${PI_HOST}" \
    "sudo /opt/jasper/.venv/bin/python /tmp/_offline_wake_count.py \
        $TEMPLATE_ARG $THRESH_ARG $MODEL_ARG '${OUT_REMOTE}/aec_output.wav'" 2>&1)

# Save + display
{
  echo "Wake-rate test — $SESSION / $TEST_LABEL"
  echo "Phrase:  $PHRASE  (slug=$SLUG)"
  echo "Model:   ${MODEL:-(default jarvis_v2)}"
  echo "Capture: ${DURATION}s"
  echo ""
  echo "$PRE_STATE"
  echo ""
  echo "─── AEC ON   (aec-on.wav — post-AEC, what voice consumes today) ───"
  echo "$WAKE_RESULT"
} | tee "$OUT_LOCAL/result.txt"

# Detect on the chip-direct mic (pre-AEC) too — same physical capture,
# just without AEC processing. This is the "AEC OFF" leg.
echo ""
echo "─── AEC OFF  (aec-off.wav — chip raw mic 1, pre-AEC) ───"
RAW_RESULT=$(ssh "${PI_USER}@${PI_HOST}" \
    "sudo /opt/jasper/.venv/bin/python /tmp/_offline_wake_count.py \
        $TEMPLATE_ARG $THRESH_ARG $MODEL_ARG '${OUT_REMOTE}/mic_ch1.wav'" 2>&1)
echo "$RAW_RESULT" | tee -a "$OUT_LOCAL/result.txt"

echo ""
echo "Files in $OUT_LOCAL/:"
ls -l "$OUT_LOCAL/" | awk 'NR>1 {print "  " $NF}'
echo ""
echo "  aec-on.wav      — what voice consumes with AEC enabled"
echo "  aec-off.wav     — what voice consumes with AEC disabled (chip-direct)"
echo "  reference.wav   — music signal AEC subtracts (listen for HF rolloff)"
