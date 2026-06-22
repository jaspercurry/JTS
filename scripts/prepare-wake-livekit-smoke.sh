#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Prepare a tiny LiveKit wakeword train/export/eval smoke workdir.
#
# Usage:
#   bash scripts/prepare-wake-livekit-smoke.sh logs/wake-features/training-workdir
#   bash scripts/prepare-wake-livekit-smoke.sh logs/wake-train logs/livekit-smoke --run-livekit
#
# This is an off-Pi smoke harness. Placeholder negatives are not quality
# evidence; use real negative-hours feature banks before interpreting metrics.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

CANDIDATES=(
    "${REPO_ROOT}/.venv/bin/python"
    "$(git -C "$REPO_ROOT" rev-parse --git-common-dir 2>/dev/null | xargs -I {} dirname {} 2>/dev/null)/.venv/bin/python"
)
PY=""
for c in "${CANDIDATES[@]}"; do
    if [[ -n "$c" && -x "$c" ]]; then
        PY="$c"
        break
    fi
done
if [[ -z "$PY" ]]; then
    PY="python3"
fi

exec "$PY" "${REPO_ROOT}/scripts/_prepare_wake_livekit_smoke.py" "$@"
