#!/usr/bin/env bash
# Reset the AirPlay receive chain on the Pi. Use when the Pi shows up
# in your Mac's AirPlay picker but won't accept connections, or when
# audio plays for a moment then drops. Both symptoms are typically
# shairport-sync stuck in a wedged AP2 connection state — process is
# alive, but won't take new SETUPs.
#
# Restart of nqptp is included because AirPlay 2 PTP can desync from
# shairport, and the two need to be in agreement for a session to
# sustain.
#
# After running, you may need to click somewhere else in your Mac's
# AirPlay picker and then back on the Pi to clear the Mac-side cache.
#
# Usage:
#   bash scripts/airplay-reset.sh
#   PI_HOST=192.168.1.42 bash scripts/airplay-reset.sh

set -euo pipefail

PI_HOST="${PI_HOST:-jts.local}"
PI_USER="${PI_USER:-pi}"

ssh "${PI_USER}@${PI_HOST}" 'sudo systemctl restart shairport-sync nqptp
sleep 1
echo "shairport-sync: $(systemctl is-active shairport-sync)"
echo "nqptp:          $(systemctl is-active nqptp)"
echo "JTS advertised: $(timeout 2 avahi-browse -rt _airplay._tcp 2>&1 | grep -c "JTS") services"'
