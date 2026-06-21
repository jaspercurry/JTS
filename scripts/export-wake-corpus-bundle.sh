#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Export browser-recorded wake-corpus sessions into a training bundle.
#
# Usage:
#   bash scripts/export-wake-corpus-bundle.sh data/enrollment_positives
#   bash scripts/export-wake-corpus-bundle.sh data/enrollment_positives logs/export --latest 3
#
# Run after copying the corpus from a Pi, for example:
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
    PY="python3"
fi

exec "$PY" "${REPO_ROOT}/scripts/_export_wake_corpus_bundle.py" "$@"
