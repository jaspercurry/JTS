#!/usr/bin/env bash
# Wake-rate test harness: anchors a journal start time, waits for the
# operator to play the wake-test track on the phone, then counts
# `event=wake.detected` lines emitted by jasper-voice during that window.
#
# Workflow (do once per condition):
#   1. Set up the condition (chip SHF_BYPASS state, AEC on/off via /system/)
#   2. Start background music at the consistent volume you're testing at
#   3. Run this script with a descriptive label
#   4. When prompted, start the wake-test track on your phone
#   5. When the track finishes, press Enter
#   6. Script reports wake count + late-cancels
#
# The track has 20 'Jarvis' utterances; each successful detection
# logs one `event=wake.detected`. Compare counts across conditions.
#
# Usage:
#   bash scripts/wake-rate-test.sh "AEC_ON_SHF_1"
#   bash scripts/wake-rate-test.sh "AEC_ON_SHF_0"
#   bash scripts/wake-rate-test.sh "AEC_OFF"
#
# Tip: label includes the condition; the script appends timestamps so
# you can rerun the same condition without overwriting.

set -euo pipefail

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"
LABEL="${1:-test}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$REPO_ROOT/logs/wake-rate"
mkdir -p "$LOG_DIR"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
RESULT_FILE="$LOG_DIR/${LABEL}-${TS}.txt"

# Engage measurement mode so wake events FIRE and LOG but never open
# an LLM session — kills the "speaker talks back during wake test +
# its TTS contaminates subsequent wakes via echo path" problem at the
# source. wake.detected lines still appear in the journal (that's what
# we count); they each get followed by wake.late_cancel reason=
# measurement_active.
ssh "${PI_USER}@${PI_HOST}" \
    "sudo python3 -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(\"/run/jasper/voice.sock\"); s.sendall(b\"MEASURE_PAUSE\\n\"); print(s.recv(4096).decode())'" \
    >/dev/null

# Belt-and-suspenders: regardless of how this script exits (success,
# Ctrl-C, error), restore the voice daemon to normal so the speaker
# isn't left mute. The daemon also has an internal 2-min safety timer.
cleanup_measure() {
    if [[ -n "${REFRESHER_PID:-}" ]]; then
        kill "$REFRESHER_PID" 2>/dev/null || true
        wait "$REFRESHER_PID" 2>/dev/null || true
    fi
    ssh "${PI_USER}@${PI_HOST}" \
        "sudo python3 -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(\"/run/jasper/voice.sock\"); s.sendall(b\"MEASURE_RESUME\\n\"); print(s.recv(4096).decode())'" \
        >/dev/null 2>&1 || true
}
trap cleanup_measure EXIT

# Re-arm measurement mode every 60 s so the daemon's 2-min safety
# timer never expires while the operator is still running the test.
# Sending MEASURE_PAUSE while already active cancels the old timer
# and starts a fresh one (idempotent re-pause path in voice_daemon).
(
    while true; do
        sleep 60
        ssh "${PI_USER}@${PI_HOST}" \
            "sudo python3 -c 'import socket; s=socket.socket(socket.AF_UNIX); s.connect(\"/run/jasper/voice.sock\"); s.sendall(b\"MEASURE_PAUSE\\n\"); s.recv(4096)'" \
            >/dev/null 2>&1 || break
    done
) &
REFRESHER_PID=$!

# Anchor on the Pi's own clock to avoid laptop↔Pi drift in journalctl
# --since parsing.
START_ON_PI="$(ssh "${PI_USER}@${PI_HOST}" 'date "+%Y-%m-%d %H:%M:%S"')"

# Also capture pre-test chip state + AEC state for the log.
PRE_STATE=$(ssh "${PI_USER}@${PI_HOST}" "
echo 'chip SHF_BYPASS:'
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host SHF_BYPASS 2>&1 | grep SHF_BYPASS
echo 'bridge:'
systemctl is-active jasper-aec-bridge.service
echo 'aec_mode.env:'
cat /var/lib/jasper/aec_mode.env 2>/dev/null || echo '(default auto)'
echo 'voice measurement mode: ACTIVE (sessions suppressed; wakes log only)'
")

cat <<HEADER

═══════════════════════════════════════════════════════
  Wake-rate test: $LABEL
═══════════════════════════════════════════════════════

  Pi journal anchor: $START_ON_PI

$PRE_STATE

  ▶ START THE PHONE TRACK NOW.
  ▶ The track has 20 'Jarvis' utterances over ~100 s.
  ▶ Press Enter as soon as the track has FINISHED.

HEADER

read -p "  → Track done? Press Enter: " _DUMMY

END_ON_PI="$(ssh "${PI_USER}@${PI_HOST}" 'date "+%Y-%m-%d %H:%M:%S"')"

# Pull wake events from the journal during the test window.
# `--since` and `--until` both interpret in the system's local tz.
WAKE_LINES=$(ssh "${PI_USER}@${PI_HOST}" \
    "sudo journalctl -u jasper-voice --since '$START_ON_PI' --until '$END_ON_PI' \
     | grep -E 'event=wake\.detected|event=wake\.late_cancel' || true")

WAKE_COUNT=$(echo "$WAKE_LINES" | grep -c 'event=wake.detected' || true)
LATE_COUNT=$(echo "$WAKE_LINES" | grep -c 'event=wake.late_cancel' || true)

# Empty grep returns "0" but with set -e the || true above guards.
WAKE_COUNT=${WAKE_COUNT:-0}
LATE_COUNT=${LATE_COUNT:-0}

# Compute a rate if 20-utterance track was used (typical).
RATE_PCT="$(awk -v c="$WAKE_COUNT" 'BEGIN { printf "%.0f", c * 100 / 20 }')"

{
  echo "Wake-rate test result"
  echo "Label:        $LABEL"
  echo "Start:        $START_ON_PI"
  echo "End:          $END_ON_PI"
  echo ""
  echo "$PRE_STATE"
  echo ""
  echo "─── Counts ───"
  echo "  Wake detected (success):       $WAKE_COUNT / 20  ($RATE_PCT%)"
  echo "  Late cancels (expected match): $LATE_COUNT"
  echo "    (in measurement mode every wake should appear in both — if"
  echo "     LATE_COUNT differs much from WAKE_COUNT, something else is"
  echo "     gating sessions e.g. mic mute, peering loss.)"
  echo ""
  echo "─── Raw matching lines ───"
  if [[ -n "$WAKE_LINES" ]]; then
      echo "$WAKE_LINES"
  else
      echo "(no wake events in window — speaker likely deaf during test)"
  fi
} | tee "$RESULT_FILE"

echo ""
echo "  Saved: $RESULT_FILE"
