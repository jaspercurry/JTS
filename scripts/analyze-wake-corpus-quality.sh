#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Run deterministic audio-quality analysis over a browser-recorded wake corpus.
#
# Usage:
#   bash scripts/analyze-wake-corpus-quality.sh data/enrollment_positives --latest
#   bash scripts/analyze-wake-corpus-quality.sh data/enrollment_positives --session 20260527T131954Z-7469

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_lib.sh
. "${SCRIPT_DIR}/_lib.sh"

PY="$(resolve_repo_python)"
if [[ -z "${PYTHON:-}" && "$PY" == "python3" ]]; then
    echo "WARN: no venv with numpy/scipy found; falling back to python3" >&2
fi

exec "$PY" "${REPO_ROOT}/scripts/_analyze_wake_corpus_quality.py" "$@"
