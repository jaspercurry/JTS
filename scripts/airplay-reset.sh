#!/usr/bin/env bash
# Reset the AirPlay receive chain on the Pi. Use when the Pi shows up
# in your Mac's AirPlay picker but won't accept connections, or when
# audio plays for a moment then drops. Both symptoms are typically
# shairport-sync stuck in a wedged AP2 connection state — process is
# alive, but won't take new SETUPs. This interrupts any active AirPlay
# session; the automatic supervisor uses a no-active-session gate.
#
# Restart of nqptp is included because AirPlay 2 PTP can desync from
# shairport, and the two need to be in agreement for a session to
# sustain.
#
# After running, you may need to click somewhere else in your Mac's
# AirPlay picker and then back on the Pi to clear the Mac-side cache.
#
# This intentionally restarts shairport-sync + nqptp, not avahi-daemon.
# shairport-sync re-registers its _airplay._tcp / _raop._tcp records on
# restart; if the speaker is missing from the picker entirely, diagnose
# discovery separately via docs/HANDOFF-airplay.md Pattern F.
#
# Usage:
#   bash scripts/airplay-reset.sh
#   PI_HOST=192.168.1.42 bash scripts/airplay-reset.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "${SCRIPT_DIR}/_lib.sh"

ssh "${PI_USER}@${PI_HOST}" 'sudo systemctl restart shairport-sync nqptp
sleep 1
echo "shairport-sync: $(systemctl is-active shairport-sync)"
echo "nqptp:          $(systemctl is-active nqptp)"
speaker_name="$(. /etc/jasper/jasper.env 2>/dev/null; printf "%s" "${JASPER_SPEAKER_NAME:-JTS}")"
airplay_count="$(timeout 2 avahi-browse -rt _airplay._tcp 2>/dev/null | grep -F -c "${speaker_name}" || true)"
raop_count="$(timeout 2 avahi-browse -rt _raop._tcp 2>/dev/null | grep -F -c "${speaker_name}" || true)"
echo "${speaker_name} _airplay._tcp: ${airplay_count} services"
echo "${speaker_name} _raop._tcp:    ${raop_count} services"'
