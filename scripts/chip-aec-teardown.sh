#!/usr/bin/env bash
# Revert chip-aec-experiment back to production state (WebRTC bridge).
# See docs/CHIP-AEC-EXPERIMENT.md.

set -euo pipefail

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"

echo "=== chip-aec-experiment: teardown (revert to WebRTC bridge) ==="
echo

echo "==> Stopping experiment daemon"
ssh "${PI_USER}@${PI_HOST}" 'sudo pkill -f "jasper.chip_aec_experiment" 2>/dev/null || true; sleep 0.5
if pgrep -f "jasper.chip_aec_experiment" > /dev/null; then
  echo "  daemon still running, sending SIGKILL"
  sudo pkill -9 -f "jasper.chip_aec_experiment" || true
  sleep 0.5
fi
echo "  daemon stopped"'

echo
echo "==> Restoring chip params (SHF_BYPASS=1)"
ssh "${PI_USER}@${PI_HOST}" 'set -e
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host SHF_BYPASS 1 > /dev/null
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host AUDIO_MGR_SYS_DELAY 12 > /dev/null
echo "  restored:"
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host SHF_BYPASS | grep "SHF_BYPASS:"
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host AUDIO_MGR_SYS_DELAY | grep "AUDIO_MGR_SYS_DELAY:"'

echo
echo "==> Unmasking + re-starting jasper-aec-bridge"
ssh "${PI_USER}@${PI_HOST}" 'sudo systemctl unmask jasper-aec-bridge
sudo systemctl start jasper-aec-reconcile
sleep 2
echo "  bridge:"
systemctl is-active jasper-aec-bridge || true
echo "  voice:"
systemctl is-active jasper-voice || true'

echo
echo "==> Verifying revert"
ssh "${PI_USER}@${PI_HOST}" 'sudo /opt/jasper/.venv/bin/jasper-doctor 2>&1 | tail -20 || true'

echo
echo "=== teardown complete ==="
echo "Speaker is back on WebRTC AEC bridge. Verify with: jasper-doctor"
