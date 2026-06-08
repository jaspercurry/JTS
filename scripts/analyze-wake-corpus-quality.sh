#!/usr/bin/env bash
# Run deterministic audio-quality analysis over a browser-recorded wake corpus.
#
# Usage:
#   bash scripts/analyze-wake-corpus-quality.sh data/enrollment_positives --latest
#   bash scripts/analyze-wake-corpus-quality.sh data/enrollment_positives --session 20260527T131954Z-7469

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
    echo "WARN: no venv with numpy/scipy found; falling back to python3" >&2
    PY="python3"
fi

exec "$PY" "${REPO_ROOT}/scripts/_analyze_wake_corpus_quality.py" "$@"
