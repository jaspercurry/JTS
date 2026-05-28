#!/usr/bin/env bash
# Run an ad-hoc Pi diagnostic inside a bounded transient systemd unit.
#
# This is the safe lane for memory-heavy or open-ended operator/Codex
# work on the Pi. It keeps the diagnostic killable and bounded so a
# bad script cannot starve the speaker's product daemons.
#
# Usage:
#   bash scripts/pi-run-diagnostic.sh -- /opt/jasper/.venv/bin/python - <<'PY'
#   print("hello from a bounded diagnostic")
#   PY
#
#   bash scripts/pi-run-diagnostic.sh -- bash -lc 'journalctl -b -1 -k | tail -80'
#
# Tunables:
#   JTS_DIAG_MEMORY_HIGH=256M
#   JTS_DIAG_MEMORY_MAX=384M
#   JTS_DIAG_MEMORY_SWAP_MAX=0
#   JTS_DIAG_RUNTIME_MAX=10min
#   JTS_DIAG_OOM_SCORE_ADJ=500
#   JTS_DIAG_CPU_WEIGHT=20
#   JTS_DIAG_IO_WEIGHT=20
#   JTS_DIAG_WORKDIR=/home/pi/jts

set -euo pipefail

# shellcheck disable=SC1091
. "$(dirname "$0")/_lib.sh"

usage() {
    sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//' >&2
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi
if [[ "${1:-}" == "--" ]]; then
    shift
fi
if [[ $# -eq 0 ]]; then
    usage
    exit 2
fi

shell_quote() {
    printf "%q" "$1"
}

quote_args() {
    local out="" arg q
    for arg in "$@"; do
        q="$(shell_quote "$arg")"
        out+="${out:+ }${q}"
    done
    printf "%s" "$out"
}

MEMORY_HIGH="${JTS_DIAG_MEMORY_HIGH:-256M}"
MEMORY_MAX="${JTS_DIAG_MEMORY_MAX:-384M}"
MEMORY_SWAP_MAX="${JTS_DIAG_MEMORY_SWAP_MAX:-0}"
RUNTIME_MAX="${JTS_DIAG_RUNTIME_MAX:-10min}"
OOM_SCORE_ADJ="${JTS_DIAG_OOM_SCORE_ADJ:-500}"
CPU_WEIGHT="${JTS_DIAG_CPU_WEIGHT:-20}"
IO_WEIGHT="${JTS_DIAG_IO_WEIGHT:-20}"
WORKDIR="${JTS_DIAG_WORKDIR:-/home/pi/jts}"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
UNIT="jts-diagnostic-${TS}-$$"
REMOTE_COMMAND="$(quote_args "$@")"

props=(
    "--property=Description=JTS bounded diagnostic"
    "--property=MemoryAccounting=yes"
    "--property=MemoryHigh=${MEMORY_HIGH}"
    "--property=MemoryMax=${MEMORY_MAX}"
    "--property=MemorySwapMax=${MEMORY_SWAP_MAX}"
    "--property=RuntimeMaxSec=${RUNTIME_MAX}"
    "--property=OOMScoreAdjust=${OOM_SCORE_ADJ}"
    "--property=CPUWeight=${CPU_WEIGHT}"
    "--property=IOWeight=${IO_WEIGHT}"
    "--property=WorkingDirectory=${WORKDIR}"
)

prop_text="$(quote_args "${props[@]}")"

echo "Running bounded diagnostic on ${PI_USER}@${PI_HOST} as ${UNIT}.service" >&2
echo "  MemoryHigh=${MEMORY_HIGH} MemoryMax=${MEMORY_MAX} RuntimeMaxSec=${RUNTIME_MAX} OOMScoreAdjust=${OOM_SCORE_ADJ}" >&2

remote_systemd_run=(
    sudo
    systemd-run
    --pipe
    --wait
    --collect
    --quiet
    "--unit=${UNIT}"
)

remote_prefix="$(quote_args "${remote_systemd_run[@]}")"
remote_tail="$(quote_args -- /usr/bin/bash -lc "$REMOTE_COMMAND")"

exec ssh -o BatchMode=yes -o ConnectTimeout=5 "${PI_USER}@${PI_HOST}" \
    "${remote_prefix} ${prop_text} ${remote_tail}"
