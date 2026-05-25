#!/usr/bin/env bash
# Audit the deliberate wake-corpus recording copied from the Pi.
#
# Usage:
#   bash scripts/audit-wake-corpus.sh
#   bash scripts/audit-wake-corpus.sh data/enrollment_positives --expect-raw0 --min-per-cell 7
#
# Run after:
#   rsync -avz --progress \
#     pi@jts.local:/var/lib/jasper/enrollment_positives/ \
#     ./data/enrollment_positives/

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
    echo "WARN: no venv with numpy found; falling back to python3" >&2
    PY="python3"
fi

exec "$PY" "${REPO_ROOT}/scripts/_audit_wake_corpus.py" "$@"
