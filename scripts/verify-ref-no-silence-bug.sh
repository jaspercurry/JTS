#!/usr/bin/env bash
# Verify that the AEC bridge's reference signal is no longer being
# poisoned by alternating-silence frames (the bug we fixed via
# "carry last_ref_bytes forward instead of falling back to silence").
#
# Workflow:
#   1. Play any music on JTS via AirPlay/Spotify/BT and let it run.
#   2. Run this script. It captures 30s of ref.wav via the bridge's
#      JASPER_AEC_DEBUG_RECORD_DIR mode, pulls it back, and analyses
#      the frame-by-frame structure.
#   3. Pass = no parity-pattern silence (< 5% silent frames at any
#      parity).
#   4. Fail = the original 50%-silent-at-one-parity bug is still
#      present.
#
# Usage:
#   bash scripts/verify-ref-no-silence-bug.sh

set -euo pipefail

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"
DURATION="${DURATION:-30}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_LOCAL="$REPO_ROOT/logs/ref-verify-$TS"
OUT_REMOTE="/tmp/ref-verify-$TS"
mkdir -p "$OUT_LOCAL"

echo "═══════════════════════════════════════════════════════"
echo "  Verify AEC ref signal has no alternating-silence bug"
echo "═══════════════════════════════════════════════════════"
echo ""
echo "  Pi:        ${PI_USER}@${PI_HOST}"
echo "  Capture:   ${DURATION}s"
echo "  Output:    $OUT_LOCAL/"
echo ""
echo "  ⚠  Music MUST be playing on JTS — any source (AirPlay,"
echo "     Spotify, BT). It doesn't matter what; we just need the"
echo "     bridge's ref pipeline to have content flowing through it."
echo ""

if ! ssh -o BatchMode=yes -o ConnectTimeout=5 "${PI_USER}@${PI_HOST}" true; then
    echo "ERROR: cannot reach ${PI_USER}@${PI_HOST}" >&2
    exit 1
fi

# Use the bridge's existing JASPER_AEC_DEBUG_RECORD_DIR mode. Inject
# via a transient systemd drop-in, restart bridge, capture, clean up.
ssh "${PI_USER}@${PI_HOST}" "sudo bash -s '$DURATION' '$OUT_REMOTE'" <<'REMOTE_SCRIPT' 2>&1 | tee "$OUT_LOCAL/run.log"
set -euo pipefail
DURATION="$1"
OUT="$2"

state="$(systemctl is-active jasper-aec-bridge.service 2>/dev/null || true)"
if [[ "$state" != "active" ]]; then
    echo "ERROR: jasper-aec-bridge.service is '$state' — start it first" >&2
    exit 1
fi

mkdir -p "$OUT"; chmod 0777 "$OUT"

OVERRIDE_DIR=/run/systemd/system/jasper-aec-bridge.service.d
mkdir -p "$OVERRIDE_DIR"
cat > "$OVERRIDE_DIR/debug-record.conf" <<EOF
[Service]
Environment=JASPER_AEC_DEBUG_RECORD_DIR=$OUT
EOF

cleanup() {
    rm -f "$OVERRIDE_DIR/debug-record.conf"
    rmdir "$OVERRIDE_DIR" 2>/dev/null || true
    systemctl daemon-reload
    systemctl restart jasper-aec-bridge.service
}
trap cleanup EXIT

systemctl daemon-reload
systemctl restart jasper-aec-bridge.service

echo "Bridge restarted with debug record. Warmup 5s + capture ${DURATION}s ..."
sleep 5
echo "Capturing now."
sleep "$DURATION"
sleep 1
echo "Capture done."
REMOTE_SCRIPT

# Pull the captured ref.wav
rsync -avz "${PI_USER}@${PI_HOST}:${OUT_REMOTE}/ref.wav" "$OUT_LOCAL/" >&2

# Analyse it
"$REPO_ROOT/.venv/bin/python" <<PY
import sys, wave
import numpy as np
path = "$OUT_LOCAL/ref.wav"
with wave.open(path) as w:
    sr = w.getframerate()
    data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64)

# Reshape into 320-sample frames (20ms each — the bridge's unit)
FRAME = 320
n_frames = len(data) // FRAME
frames = data[:n_frames * FRAME].reshape(n_frames, FRAME)
energy = np.sum(np.abs(frames), axis=1)
is_silent = energy < 10  # essentially-zero frame

silent = int(np.sum(is_silent))
even_silent = int(np.sum(is_silent[::2]))
odd_silent = int(np.sum(is_silent[1::2]))
even_total = (n_frames + 1) // 2
odd_total = n_frames // 2
even_pct = 100 * even_silent / max(1, even_total)
odd_pct = 100 * odd_silent / max(1, odd_total)
worst_parity_pct = max(even_pct, odd_pct)

print()
print(f"Captured ref.wav: {len(data)/sr:.1f}s, {n_frames} frames of 20ms each")
print(f"")
print(f"  Total silent frames:   {silent} / {n_frames}  ({100*silent/n_frames:5.1f}%)")
print(f"  Even-indexed silent:   {even_silent} / {even_total}  ({even_pct:5.1f}%)")
print(f"  Odd-indexed silent:    {odd_silent} / {odd_total}  ({odd_pct:5.1f}%)")
print(f"")

PASS_THRESHOLD = 5.0  # %
if worst_parity_pct < PASS_THRESHOLD:
    print(f"  ✓ PASS  — worst parity is {worst_parity_pct:.1f}% silent (< {PASS_THRESHOLD}%)")
    print(f"           The alternating-silence bug is fixed.")
    sys.exit(0)
elif worst_parity_pct > 40.0:
    print(f"  ✗ FAIL  — {worst_parity_pct:.1f}% of one parity is silent.")
    print(f"           This is the original 50%-alternating-silence bug.")
    print(f"           Fix A is NOT in effect on this build.")
    sys.exit(1)
else:
    print(f"  ⚠  PARTIAL  — {worst_parity_pct:.1f}% silent at worst parity.")
    print(f"               Some silence still present but not the 50% bug.")
    print(f"               Investigate further.")
    sys.exit(2)
PY