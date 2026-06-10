#!/usr/bin/env bash
# Run the thin Phase 0 custom wake-word training workflow.
#
# This is an off-Pi operator runner. It orchestrates the existing corpus export,
# feature-bank, real-positive injection, and LiveKit smoke/train/eval prep tools.
# It does not deploy or activate a model.

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

exec "$PY" "${REPO_ROOT}/scripts/_run_wake_training_phase0.py" "$@"
