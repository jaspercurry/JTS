#!/usr/bin/env bash
# Generate a wake-rate test audio track on the Pi (uses OpenAI TTS via
# the API key already in /etc/jasper/jasper.env), pull it back to the
# laptop for AirDrop to your phone.
#
# The track is N × <PHRASE> with fixed gaps — feed it through your
# phone's speaker during wake-rate tests so the only variable across
# A/B conditions is the chip / AEC config or wake-model, not your
# voice level.
#
# Usage:
#   bash scripts/make-wake-test-track.sh                      # default Jarvis
#   PHRASE="Hey Buddy" bash scripts/make-wake-test-track.sh   # alternative phrase
#   REPS=30 GAP_SEC=5 bash scripts/make-wake-test-track.sh
#
# Each phrase gets its own subdirectory under logs/wake-test-track/
# (e.g. logs/wake-test-track/jarvis/, logs/wake-test-track/hey-buddy/)
# so multiple tracks can coexist for back-to-back A/B testing.

set -euo pipefail

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"
REPS="${REPS:-20}"
GAP_SEC="${GAP_SEC:-4}"
PHRASE="${PHRASE:-Jarvis}"
SLUG=$(echo "$PHRASE" | tr '[:upper:] ' '[:lower:]-')

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCAL_PY="$REPO_ROOT/scripts/_make_wake_test_track.py"
OUT_LOCAL="$REPO_ROOT/logs/wake-test-track/${SLUG}"
REMOTE_OUT="/tmp/wake-test-track-${SLUG}"
mkdir -p "$OUT_LOCAL"

if [[ ! -f "$LOCAL_PY" ]]; then
    echo "ERROR: $LOCAL_PY missing — repo state corrupted?" >&2
    exit 1
fi

scp -q "$LOCAL_PY" "${PI_USER}@${PI_HOST}:/tmp/_make_wake_test_track.py"
ssh "${PI_USER}@${PI_HOST}" \
    "sudo /opt/jasper/.venv/bin/python /tmp/_make_wake_test_track.py \
        --reps ${REPS} --gap-sec ${GAP_SEC} \
        --word '${PHRASE}' --out-dir '${REMOTE_OUT}'"

rsync -avz "${PI_USER}@${PI_HOST}:${REMOTE_OUT}/" "$OUT_LOCAL/"

echo ""
echo "Track ready (phrase='${PHRASE}', slug='${SLUG}'):"
echo "  $OUT_LOCAL/"
ls "$OUT_LOCAL/"
echo ""
echo "Next:"
echo "  1. AirDrop $OUT_LOCAL/wake-test-track.{m4a,wav} to your phone"
echo "     (rename to wake-test-track-${SLUG}.wav if you want both"
echo "     '${PHRASE}' and other tracks coexisting on your phone)"
echo "  2. Play it back at a consistent volume."
echo "  3. Run a wake-rate test with the right wake model:"
echo "     PHRASE='${PHRASE}' \\"
echo "     MODEL=/var/lib/jasper/wake/<model>.onnx \\"
echo "     bash scripts/wake-rate-test.sh <test-label>"
