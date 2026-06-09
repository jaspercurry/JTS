#!/usr/bin/env bash
# Build openWakeWord-compatible positive feature arrays from a wake corpus bundle.
#
# Usage:
#   bash scripts/build-wake-feature-bank.sh logs/wake-corpus-export/20260609T120000Z
#   bash scripts/build-wake-feature-bank.sh logs/wake-corpus-export/20260609T120000Z logs/features --leg chip_aec_150
#
# This is an offline data-prep tool. It requires openwakeword==0.6.0,
# onnxruntime, numpy, and staged openWakeWord ONNX feature models.

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

exec "$PY" "${REPO_ROOT}/scripts/_build_wake_feature_bank.py" "$@"
