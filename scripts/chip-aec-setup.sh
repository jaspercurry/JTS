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
echo "==> Stopping + masking the production AEC service chain"
# We mask FOUR units, not one. Masking only the bridge leaves three
# silent paths that can re-flip SHF_BYPASS=1 mid-experiment:
#   - jasper-aec-reconcile.service: ANY controlC* hotplug fires the
#     udev rule deploy/udev/99-jasper-aec-reconcile.rules, which
#     calls enable_start_aec() → restart jasper-aec-init.service
#     (which unconditionally writes SHF_BYPASS=1 per
#     jasper/cli/aec_init.py).
#   - jasper-aec-init.service: even if reconcile is masked, an
#     install.sh run or manual `systemctl start jasper-aec-init`
#     re-writes SHF_BYPASS=1 + AEC_HPFONOFF + amixer.
#   - jasper-dongle-recover.service: dongle replug chains into
#     `systemctl start jasper-aec-reconcile.service` and bypasses
#     a reconciler-only mask.
ssh "${PI_USER}@${PI_HOST}" 'set -e
for unit in jasper-aec-bridge jasper-aec-reconcile jasper-aec-init jasper-dongle-recover; do
  sudo systemctl stop "${unit}.service" 2>/dev/null || true
  sudo systemctl mask "${unit}.service"
  echo "  ${unit} masked"
done'

echo
echo "==> Snapshotting voice-input env for clean dual/triple-stream behaviour"
# JASPER_MIC_DEVICE_RAW (port 9877) and JASPER_MIC_DEVICE_DTLN (port
# 9878) are default-off, but if the user has them set in
# /etc/jasper/jasper.env, jasper-voice will bind those ports and
# silently starve on them (the experiment only feeds 9876). The
# OFF/DTLN wake legs never fire and every wake-event row gets a
# misleading score_off=none. Comment them out for the experiment
# duration; teardown restores from the backup.
ssh "${PI_USER}@${PI_HOST}" 'set -e
ENV=/etc/jasper/jasper.env
BAK=/etc/jasper/jasper.env.chip-aec.bak
if [[ -f "$ENV" ]] && grep -qE "^(JASPER_MIC_DEVICE_RAW|JASPER_MIC_DEVICE_DTLN)=" "$ENV"; then
  if [[ ! -f "$BAK" ]]; then
    sudo cp -a "$ENV" "$BAK"
    echo "  backed up $ENV → $BAK"
  fi
  sudo sed -i -E "s/^(JASPER_MIC_DEVICE_RAW|JASPER_MIC_DEVICE_DTLN)=/# chip-aec.bak: \1=/" "$ENV"
  echo "  commented dual/triple-stream env vars"
else
  echo "  no dual/triple-stream env to snapshot (clean single-stream config)"
fi'

echo
echo "==> Marking wake-events timestamp for post-hoc corpus filtering"
# WakeEventStore (jasper/wake_events.py) writes every wake to
# /var/lib/jasper/wake-events/wake-events.sqlite3 + WAVs into a 1 GB
# ring. There is no env knob to disable. During the experiment the
# corpus will mix chip-AEC fires with the production WebRTC-AEC
# baseline. We write a UNIX-timestamp sentinel so the teardown can
# print a SQL one-liner that labels (or deletes) experiment-window
# events. Production code never reads this file — purely a marker.
ssh "${PI_USER}@${PI_HOST}" 'sudo mkdir -p /var/lib/jasper/wake-events
sudo bash -c "date +%s > /var/lib/jasper/wake-events/.chip-aec-experiment-start.ts"
echo "  start ts: $(sudo cat /var/lib/jasper/wake-events/.chip-aec-experiment-start.ts)"'

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
