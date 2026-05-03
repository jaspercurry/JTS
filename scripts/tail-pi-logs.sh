#!/usr/bin/env bash
# Live-tail jasper logs from the Pi. Ctrl-C to stop.
#
# Usage:
#   bash scripts/tail-pi-logs.sh
#   bash scripts/tail-pi-logs.sh jasper-voice  # tail one unit
set -euo pipefail

PI_HOST="${PI_HOST:-jasper.local}"
PI_USER="${PI_USER:-pi}"

if [[ $# -gt 0 ]]; then
    units=()
    for u in "$@"; do
        units+=(-u "$u")
    done
else
    units=(-u jasper-camilla -u jasper-voice -u mpd)
fi

exec ssh -t "${PI_USER}@${PI_HOST}" \
    "journalctl -f --output=short-iso ${units[*]}"
