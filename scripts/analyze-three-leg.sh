#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Weekly review of the wake-event corpus — which configured wake legs
# (AEC3, chip-direct, DTLN-aec, opt-in chip-AEC beams) are actually
# firing, where they disagree, and which events are worth listening to.
#
# Usage:
#   bash scripts/analyze-three-leg.sh                          # analyzes wake-events/latest
#   bash scripts/analyze-three-leg.sh wake-events/20260523T125330Z
#   bash scripts/analyze-three-leg.sh --top 10                 # 10 events per category
#   bash scripts/analyze-three-leg.sh --since 2026-06-01       # current validation window
#
# Run after `bash scripts/fetch-wake-events.sh` (which lands the
# corpus under wake-events/<UTC-timestamp>/ + updates the
# `wake-events/latest` symlink). Reports:
#   - Fire breakdown by fired-leg pattern
#   - Per-leg score distribution
#   - "Solo save" events (one leg only) per leg
#   - Listening playlist with afplay commands
#   - Funnel: fired → turn → speech → tool, broken down by pattern

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_lib.sh
. "${SCRIPT_DIR}/_lib.sh"

# Pass --top through; treat any non-flag arg as the corpus dir.
ARGS=()
CORPUS=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --top) ARGS+=("$1" "$2"); shift 2 ;;
        --since) ARGS+=("$1" "$2"); shift 2 ;;
        --until) ARGS+=("$1" "$2"); shift 2 ;;
        --top=*) ARGS+=("$1"); shift ;;
        --since=*) ARGS+=("$1"); shift ;;
        --until=*) ARGS+=("$1"); shift ;;
        -*) ARGS+=("$1"); shift ;;
        *) CORPUS="$1"; shift ;;
    esac
done
CORPUS="${CORPUS:-${REPO_ROOT}/wake-events/latest}"

PY="$(resolve_repo_python)"

exec "$PY" "${REPO_ROOT}/scripts/_analyze_three_leg.py" "$CORPUS" ${ARGS[@]+"${ARGS[@]}"}
