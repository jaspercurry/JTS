#!/usr/bin/env bash
# Live-tail jasper logs from the Pi. Ctrl-C to stop.
#
# Usage:
#   bash scripts/tail-pi-logs.sh
#   bash scripts/tail-pi-logs.sh jasper-voice  # tail one unit
set -euo pipefail

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"

if [[ $# -gt 0 ]]; then
    # Operator passed explicit unit names — tail just those.
    units=()
    for u in "$@"; do
        units+=(-u "$u")
    done
    exec ssh -t "${PI_USER}@${PI_HOST}" \
        "journalctl -f --output=short-iso ${units[*]}"
fi

# Default: every jasper-* unit, plus the renderers and their deps.
# Uses systemd unit-name globbing (-u 'jasper-*', supported since
# journalctl v245) so new daemons land in the tail automatically.
exec ssh -t "${PI_USER}@${PI_HOST}" \
    "journalctl -f --output=short-iso \
        -u 'jasper-*' -u librespot -u shairport-sync -u nqptp \
        -u bluealsa -u bluealsa-aplay -u bt-agent"
