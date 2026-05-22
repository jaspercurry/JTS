#!/usr/bin/env bash
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
# offline. See docs/HANDOFF-mic-quality-v2.md "Testing methodology"
# for the methodology + docs/testing-tooling.md for the catalog of
# related scripts (scripts/wake-rate-test.sh covers the
# fixed-phone-track case; this script covers the live-user-speech
# case).
#
# Mechanism (mirrors scripts/wake-rate-test.sh):
#   1. Writes a systemd drop-in to /run/systemd/system/jasper-aec-bridge.service.d/
#      that sets JASPER_AEC_DEBUG_RECORD_DIR — the bridge's
#      built-in debug-record mode writes all three WAVs (see
#      jasper/cli/aec_bridge.py `_aec_loop` docstring).
#   2. Stops jasper-voice so a stray wake fire won't trigger TTS
#      playback that contaminates the capture.
#   3. Restarts jasper-aec-bridge with the override applied;
#      warms up 5 s; opens the capture window for `seconds`.
#   4. `trap cleanup EXIT` removes the override + restarts the
#      bridge + restarts jasper-voice — even if the script
#      crashes / Ctrl-C'd. Production state is always restored.
#   5. Rsyncs WAVs back, renames to aec-on/aec-off/reference,
#      reports sanity stats.
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

if [[ $# -lt 1 ]]; then
    cat >&2 <<USAGE
Usage: $0 <condition> [seconds]
  conditions per HANDOFF-mic-quality-v2.md "Testing methodology":
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

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
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

# Pi-side capture: stop jasper-voice, drop in JASPER_AEC_DEBUG_RECORD_DIR
# override, restart bridge, wait, restore. Uses single-quoted heredoc so
# laptop shell doesn't try to expand $variables.
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

echo "Bridge in debug-record; jasper-voice stopped. Warmup 5s ..."
sleep 5

echo ""
echo "  ▶ SPEAK NOW — ${DURATION}-second capture window is open"
echo ""
sleep "$DURATION"

echo "Capture done."
REMOTE_SCRIPT

# Pull artifacts back
rsync -avz "${PI_USER}@${PI_HOST}:${OUT_REMOTE}/" "$OUT_LOCAL/"

# Rename to functional names matching scripts/wake-rate-test.sh's convention.
# Bridge writes its internal names; we rename downstream so analysis
# tooling sees consistent labels regardless of capture source.
[[ -f "$OUT_LOCAL/aec_output.wav" ]] && mv "$OUT_LOCAL/aec_output.wav" "$OUT_LOCAL/aec-on.wav"
[[ -f "$OUT_LOCAL/mic_ch1.wav"   ]] && mv "$OUT_LOCAL/mic_ch1.wav"   "$OUT_LOCAL/aec-off.wav"
[[ -f "$OUT_LOCAL/ref.wav"       ]] && mv "$OUT_LOCAL/ref.wav"       "$OUT_LOCAL/reference.wav"

# Pi-side cleanup
ssh -q "${PI_USER}@${PI_HOST}" "sudo rm -rf '${OUT_REMOTE}'" 2>/dev/null || true

echo
echo ">>> sanity stats:"
PY="${REPO_ROOT}/.venv/bin/python"
[[ -x "$PY" ]] || PY=python3
"$PY" - "$OUT_LOCAL" <<'PYEOF'
import math, os, struct, sys, wave
out_dir = sys.argv[1]
for name in sorted(os.listdir(out_dir)):
    if not name.endswith(".wav"):
        continue
    path = os.path.join(out_dir, name)
    try:
        with wave.open(path, "rb") as w:
            n = w.getnframes()
            sr = w.getframerate()
            ch = w.getnchannels()
            sw = w.getsampwidth()
            data = w.readframes(n)
        if sw == 2 and ch == 1 and n > 0:
            s = struct.unpack(f"<{n}h", data[: n * 2])
            peak = max(abs(x) for x in s) if s else 0
            rms = math.sqrt(sum(x * x for x in s) / max(n, 1))
            print(
                f"  {name:18s} {n/sr:5.1f}s {sr:5d} Hz {ch}ch  "
                f"peak={peak:6d} ({20*math.log10(max(peak,1)/32768):+5.1f} dBFS)  "
                f"RMS={rms:6.0f} ({20*math.log10(max(rms,1)/32768):+5.1f} dBFS)"
            )
        else:
            print(f"  {name}  (sr={sr} ch={ch} sw={sw} frames={n})")
    except Exception as exc:
        print(f"  {name}  (failed: {exc})")
PYEOF

echo
echo "Done. ${OUT_LOCAL}"
