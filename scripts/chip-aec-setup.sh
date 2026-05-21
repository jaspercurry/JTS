#!/usr/bin/env bash
# Phase 1 setup for the chip-aec-experiment branch.
# See docs/CHIP-AEC-EXPERIMENT.md for the full plan and revert procedure.
#
# What this does:
#   1. Rsyncs the experimental code to the Pi (no install.sh — keeps the
#      reconciler from re-enabling the bridge mid-experiment).
#   2. Stops + masks jasper-aec-bridge (so the reconciler can't restart it).
#   3. Sets chip params for chip-AEC: SHF_BYPASS=0, AUDIO_MGR_SYS_DELAY=12.
#   4. Starts the experiment daemon (reference feeder + UDP mic pump).
#
# jasper-voice continues reading udp://127.0.0.1:9876 — same input contract
# as production with the WebRTC bridge. No voice-daemon changes.
#
# Revert: bash scripts/chip-aec-teardown.sh

set -euo pipefail

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== chip-aec-experiment: Phase 1 setup ==="
echo

echo "==> Rsync (SKIP_INSTALL=1, no reconciler re-eval)"
SKIP_INSTALL=1 PI_HOST="$PI_HOST" PI_USER="$PI_USER" bash "${SCRIPT_DIR}/deploy-to-pi.sh"

echo
echo "==> Stopping + masking jasper-aec-bridge"
ssh "${PI_USER}@${PI_HOST}" 'sudo systemctl stop jasper-aec-bridge 2>/dev/null || true
sudo systemctl mask jasper-aec-bridge
echo "  jasper-aec-bridge masked"'

echo
echo "==> Setting chip params (SHF_BYPASS=0, AUDIO_MGR_SYS_DELAY=12)"
ssh "${PI_USER}@${PI_HOST}" 'set -e
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host SHF_BYPASS 0 > /dev/null
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host AUDIO_MGR_SYS_DELAY 12 > /dev/null
echo "  current values:"
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host SHF_BYPASS | grep "SHF_BYPASS:"
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host AUDIO_MGR_SYS_DELAY | grep "AUDIO_MGR_SYS_DELAY:"'

echo
echo "==> Killing any prior experiment daemon"
ssh "${PI_USER}@${PI_HOST}" 'sudo pkill -f "jasper.chip_aec_experiment" 2>/dev/null || true; sleep 0.5'

echo
echo "==> Starting experiment daemon"
ssh "${PI_USER}@${PI_HOST}" 'sudo bash -c "nohup /opt/jasper/.venv/bin/python -m jasper.chip_aec_experiment > /var/log/chip-aec-experiment.log 2>&1 < /dev/null &"
sleep 2
if pgrep -f "jasper.chip_aec_experiment" > /dev/null; then
  echo "  daemon started (PID $(pgrep -f jasper.chip_aec_experiment))"
else
  echo "  daemon FAILED to start. Last 30 lines of log:"
  sudo tail -30 /var/log/chip-aec-experiment.log
  exit 1
fi'

echo
echo "==> Restarting jasper-voice to clear any prior state"
ssh "${PI_USER}@${PI_HOST}" 'sudo systemctl restart jasper-voice
sleep 2
systemctl is-active jasper-voice'

echo
echo "=== setup complete ==="
echo
echo "Next steps:"
echo "  1. Start music playing through the speaker at production volume"
echo "  2. Wait 30 s for chip AEC to converge"
echo "  3. Verify: bash scripts/chip-aec-poll-convergence.sh"
echo "  4. Capture ear-test recordings: bash scripts/chip-aec-capture-comparison.sh"
echo
echo "Live monitoring:"
echo "  ssh ${PI_USER}@${PI_HOST} 'sudo tail -f /var/log/chip-aec-experiment.log'"
echo "  ssh ${PI_USER}@${PI_HOST} 'sudo journalctl -u jasper-voice -f'"
echo
echo "Revert: bash scripts/chip-aec-teardown.sh"
