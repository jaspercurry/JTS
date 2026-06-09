#!/usr/bin/env bash
# Stage JTS real-positive feature banks into a trainer workdir.
#
# Usage:
#   bash scripts/prepare-wake-training-workdir.sh logs/wake-corpus-export/20260609T120000Z/feature-bank
#   bash scripts/prepare-wake-training-workdir.sh logs/features logs/train-workdir --positive-weight 3
#
# This is an offline data-prep tool. It requires numpy and consumes the output
# from scripts/build-wake-feature-bank.sh.

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

exec "$PY" "${REPO_ROOT}/scripts/_prepare_wake_training_workdir.py" "$@"
