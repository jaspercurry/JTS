#!/usr/bin/env bash
# Phase 3: poll AEC_AECCONVERGED while music plays.
#
# The chip's AEC adaptive filter is expected to set this flag to 1 once it
# has locked onto the echo path. Treat [1] as a strong positive. Treat [0]
# as a warning, not the only verdict: the 2026-05-29 gate saw useful ch0
# attenuation even while this flag stayed [0].
#
# Pre-conditions:
#   - bash scripts/chip-aec-setup.sh has been run
#   - Music is currently playing through the speaker at production volume
#
# Usage:
#   bash scripts/chip-aec-poll-convergence.sh [duration_seconds] [interval_seconds]
#   (defaults: 90 s total, polling every 5 s)

set -euo pipefail

DURATION=${1:-90}
INTERVAL=${2:-5}
PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"

echo "Polling AEC_AECCONVERGED on ${PI_HOST}"
echo "Duration: ${DURATION}s  Interval: ${INTERVAL}s"
echo
echo "Pre-flight: confirm music is playing through the dongle right NOW."
echo "Will start polling in 3 seconds..."
sleep 3
echo

end_ts=$(( $(date +%s) + DURATION ))
iter=0
converged_at=""

while [ "$(date +%s)" -lt "$end_ts" ]; do
  iter=$((iter + 1))
  t=$(date +%H:%M:%S)

  # xvf_host prints "AEC_AECCONVERGED: [N]" where N is 0 or 1
  flag_line=$(ssh "${PI_USER}@${PI_HOST}" \
    'sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host AEC_AECCONVERGED 2>/dev/null | grep "AEC_AECCONVERGED:"' \
    || echo "AEC_AECCONVERGED: [ERR]")

  echo "[${t}] iter=${iter}  ${flag_line}"

  if [ -z "$converged_at" ] && echo "$flag_line" | grep -q "AEC_AECCONVERGED: \[1\]"; then
    converged_at="iter ${iter} (${t})"
    echo "    ✅ CONVERGED at ${converged_at}"
  fi

  sleep "$INTERVAL"
done

echo
echo "============================================="
if [ -n "$converged_at" ]; then
  echo "✅ Result: chip AEC converged (first at ${converged_at})"
  echo
  echo "Next: bash scripts/chip-aec-capture-comparison.sh"
  echo "      (capture audio files for ear-test A/B)"
else
  echo "❌ Result: chip AEC did NOT converge in ${DURATION} s"
  echo
  echo "Troubleshooting (in order):"
  echo "  1. Music actually playing?"
  echo "     ssh ${PI_USER}@${PI_HOST} 'pactl list short sinks' (if pulseaudio) or check the renderer"
  echo "  2. Experiment daemon running and feeding?"
  echo "     ssh ${PI_USER}@${PI_HOST} 'sudo tail -30 /var/log/chip-aec-experiment.log'"
  echo "     Look for 'ref feeder: N frames (Xs) RMS=...' — RMS should be >0"
  echo "  3. Chip USB-IN actually receiving frames?"
  echo "     ssh ${PI_USER}@${PI_HOST} 'awk \"/^Playback:/{p=1} p && /Status:/{print; exit}\" /proc/asound/Array/stream0'"
  echo "     Should say 'Status: Running' while feeder is active"
  echo "  4. Run the baseline gate before sweeping blindly:"
  echo "     bash scripts/chip-aec-baseline-check.sh"
  echo "     The current firmware read-back clamps AUDIO_MGR_SYS_DELAY to [-64, +256]."
  echo "  5. If no in-range value converges, still run the ch0 A/B ear test once."
  echo "     The 2026-05-29 gate saw ch0 attenuation even while this flag stayed 0."
  echo "     If ch0 does not sound materially better, treat chip AEC as negative"
  echo "     for corpus purposes and return to the WebRTC AEC3 bridge."
fi
echo "============================================="
