#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Run jasper-system-soak on the active Pi through the bounded diagnostic lane.
#
# This is a convenience wrapper for:
#   JTS_DIAG_RUNTIME_MAX=<duration+2min> \
#     bash scripts/pi-run-diagnostic.sh -- \
#       /opt/jasper/.venv/bin/jasper-system-soak ...
#
# It does not deploy code or change runtime config.

set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"

usage() {
    cat >&2 <<'EOF'
Usage:
  bash scripts/pi-system-soak.sh [jasper-system-soak args...]

Examples:
  bash scripts/pi-system-soak.sh --duration 10m --profile idle
  bash scripts/pi-system-soak.sh --duration 30m --profile realistic --include-pss

The command runs through scripts/pi-run-diagnostic.sh, so the diagnostic
process is bounded by systemd MemoryHigh/MemoryMax/RuntimeMaxSec.
EOF
}

duration_to_seconds() {
    local raw="$1" num unit
    if [[ "$raw" =~ ^([0-9]+)([smh]?)$ ]]; then
        num="${BASH_REMATCH[1]}"
        unit="${BASH_REMATCH[2]:-s}"
        case "$unit" in
            s|"") printf "%s\n" "$num" ;;
            m) printf "%s\n" "$((num * 60))" ;;
            h) printf "%s\n" "$((num * 3600))" ;;
            *) return 1 ;;
        esac
    else
        return 1
    fi
}

duration="10m"
args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        --duration)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --duration requires a value" >&2
                exit 2
            fi
            duration="$2"
            args+=("$1" "$2")
            shift 2
            ;;
        --duration=*)
            duration="${1#--duration=}"
            args+=("$1")
            shift
            ;;
        *)
            args+=("$1")
            shift
            ;;
    esac
done

if [[ ${#args[@]} -eq 0 ]]; then
    args=(--duration "$duration" --profile idle)
fi

duration_sec="$(duration_to_seconds "$duration" || true)"
if [[ -z "${duration_sec:-}" ]]; then
    echo "ERROR: unsupported --duration '$duration' (use integer s/m/h, e.g. 30m)" >&2
    exit 2
fi

# Give the sampler time to finish the last sample and write the artifact.
export JTS_DIAG_RUNTIME_MAX="${JTS_DIAG_RUNTIME_MAX:-$((duration_sec + 120))s}"

exec bash "$DIR/pi-run-diagnostic.sh" -- \
    /opt/jasper/.venv/bin/jasper-system-soak "${args[@]}"
