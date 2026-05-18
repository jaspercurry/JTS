#!/usr/bin/env bash
# Generate a wake-rate test audio track on the Pi (uses OpenAI TTS via
# the API key already in /etc/jasper/jasper.env), pull it back to the
# laptop for AirDrop to your phone.
#
# The track is N × "Jarvis" with fixed gaps — feed it through your
# phone's speaker during wake-rate tests so the only variable across
# A/B conditions is the chip / AEC config, not your voice level.
#
# Usage:
#   bash scripts/make-wake-test-track.sh
#   REPS=30 GAP_SEC=5 bash scripts/make-wake-test-track.sh

set -euo pipefail

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"
REPS="${REPS:-20}"
GAP_SEC="${GAP_SEC:-4}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCAL_PY="$REPO_ROOT/scripts/_make_wake_test_track.py"
OUT_LOCAL="$REPO_ROOT/logs/wake-test-track"
mkdir -p "$OUT_LOCAL"

if [[ ! -f "$LOCAL_PY" ]]; then
    echo "ERROR: $LOCAL_PY missing — repo state corrupted?" >&2
    exit 1
fi

scp -q "$LOCAL_PY" "${PI_USER}@${PI_HOST}:/tmp/_make_wake_test_track.py"
ssh "${PI_USER}@${PI_HOST}" \
    "sudo /opt/jasper/.venv/bin/python /tmp/_make_wake_test_track.py \
        --reps ${REPS} --gap-sec ${GAP_SEC}"

rsync -avz "${PI_USER}@${PI_HOST}:/tmp/wake-test-track/" "$OUT_LOCAL/"

echo ""
echo "Track ready: $OUT_LOCAL/"
ls "$OUT_LOCAL/"
echo ""
echo "Next:"
echo "  1. AirDrop $OUT_LOCAL/wake-test-track.m4a to your phone"
echo "  2. Play it back at a consistent volume on whatever speaker you"
echo "     want — phone speaker, BT speaker, whatever. The point is"
echo "     consistency across A/B legs, not realism."
echo "  3. Run: bash scripts/wake-rate-test.sh '<label>'"
