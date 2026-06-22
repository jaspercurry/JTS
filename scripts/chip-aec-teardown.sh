#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Revert chip-aec-experiment back to production state (WebRTC bridge).
# See docs/CHIP-AEC-EXPERIMENT.md.

set -euo pipefail

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"

echo "=== chip-aec-experiment: teardown (revert to WebRTC bridge) ==="
echo

echo "==> Stopping experiment daemon"
ssh "${PI_USER}@${PI_HOST}" 'sudo pkill -f "[j]asper.chip_aec_experiment" 2>/dev/null || true; sleep 0.5
if pgrep -f "[j]asper.chip_aec_experiment" > /dev/null; then
  echo "  daemon still running, sending SIGKILL"
  sudo pkill -9 -f "[j]asper.chip_aec_experiment" || true
  sleep 0.5
fi
echo "  daemon stopped"'

echo
echo "==> Restoring chip params (SHF_BYPASS=1, AEC_HPFONOFF, mixer)"
# Production boot-time init (jasper/cli/aec_init.py) writes THREE
# chip-side things, not just SHF_BYPASS:
#   - SHF_BYPASS=1
#   - AEC_HPFONOFF=N (default 2 = 125 Hz; overridden by
#     JASPER_AEC_CHIP_HPF_HZ in /etc/jasper/jasper.env)
#   - amixer -c Array sset PCM,0/PCM,1 60 unmute
# We re-assert all three explicitly so the chip lands in the same
# state a fresh boot would produce. Restarting jasper-aec-reconcile
# below would also call aec_init via the reconciler chain, but
# explicit-here-then-via-chain is belt-and-braces.
ssh "${PI_USER}@${PI_HOST}" 'set -e
# AEC_HPFONOFF: read user override if set, else default 2 (125 Hz).
HPF=2
if [[ -f /etc/jasper/jasper.env ]]; then
  hpf_hz=$(sudo grep -E "^JASPER_AEC_CHIP_HPF_HZ=" /etc/jasper/jasper.env | tail -1 | cut -d= -f2- | tr -d "\"" || true)
  case "$hpf_hz" in
    "")        HPF=2 ;;   # unset → default 125 Hz
    "off"|"0") HPF=0 ;;
    "60")      HPF=1 ;;
    "125")     HPF=2 ;;
    "180")     HPF=3 ;;
    *)         echo "  WARN: unknown JASPER_AEC_CHIP_HPF_HZ=$hpf_hz, leaving HPF=2"; HPF=2 ;;
  esac
fi
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host SHF_BYPASS --values 1 > /dev/null
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host AEC_HPFONOFF --values "$HPF" > /dev/null
# AUDIO_MGR_SYS_DELAY is no longer written by production init (the
# write was removed pre-branch); restoring 12 puts the chip at a
# known-quiet value rather than "what boot leaves". Reconciler chain
# below will not overwrite this since aec_init no longer touches it.
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host AUDIO_MGR_SYS_DELAY --values 12 > /dev/null
sudo amixer -q -c Array sset "PCM",0 60 unmute || true
sudo amixer -q -c Array sset "PCM",1 60 unmute || true
echo "  restored:"
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host SHF_BYPASS | grep "SHF_BYPASS:"
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host AEC_HPFONOFF | grep "AEC_HPFONOFF:"
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host AUDIO_MGR_SYS_DELAY | grep "AUDIO_MGR_SYS_DELAY:"'

echo
echo "==> Restoring voice-input env from snapshot (if any)"
ssh "${PI_USER}@${PI_HOST}" 'set -e
ENV=/etc/jasper/jasper.env
BAK=/etc/jasper/jasper.env.chip-aec.bak
if [[ -f "$BAK" ]]; then
  sudo install -m 0644 -o root -g root "$BAK" "$ENV"
  sudo rm -f "$BAK"
  echo "  restored $ENV from $BAK"
else
  echo "  no snapshot to restore (none was taken at setup)"
fi'

echo
echo "==> Unmasking + restarting the production AEC service chain"
ssh "${PI_USER}@${PI_HOST}" 'set -e
for unit in jasper-aec-bridge jasper-aec-reconcile jasper-aec-init jasper-dongle-recover; do
  sudo systemctl unmask "${unit}.service" 2>/dev/null || true
  echo "  ${unit} unmasked"
done
sudo systemctl start jasper-aec-reconcile.service
sleep 2
echo "  bridge:"
systemctl is-active jasper-aec-bridge || true
echo "  voice:"
systemctl is-active jasper-voice || true'

echo
echo "==> Surfacing wake-events captured during the experiment"
# Print a SQL one-liner the operator can run to label or delete
# experiment-window wake events. We don't auto-execute it — the user
# decides whether to label (preserve forensics), delete (clean
# corpus), or leave it (accept contamination).
ssh "${PI_USER}@${PI_HOST}" 'set -e
MARKER=/var/lib/jasper/wake-events/.chip-aec-experiment-start.ts
DB=/var/lib/jasper/wake-events/wake-events.sqlite3
if [[ -f "$MARKER" ]]; then
  start_ts=$(sudo cat "$MARKER")
  end_ts=$(date +%s)
  count=$(sudo sqlite3 "$DB" "SELECT COUNT(*) FROM wake_events WHERE strftime(\"%s\", ts_utc) >= $start_ts;" 2>/dev/null || echo "?")
  echo "  experiment window: ts >= $start_ts (now $end_ts)"
  echo "  wake events captured during experiment: $count"
  echo
  echo "  To label them (recommended):"
  echo "    sudo sqlite3 $DB \"UPDATE wake_events SET label = '\''chip-aec-experiment'\'' WHERE strftime('\''%s'\'', ts_utc) >= $start_ts;\""
  echo
  echo "  To delete them (cleans corpus):"
  echo "    sudo sqlite3 $DB \"DELETE FROM wake_events WHERE strftime('\''%s'\'', ts_utc) >= $start_ts;\""
  sudo rm -f "$MARKER"
fi'

echo
echo "==> Verifying revert"
ssh "${PI_USER}@${PI_HOST}" 'sudo /opt/jasper/.venv/bin/jasper-doctor 2>&1 | tail -20 || true'

echo
echo "=== teardown complete ==="
echo "Speaker is back on WebRTC AEC bridge. Verify with: jasper-doctor"
