#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_lib.sh
. "${SCRIPT_DIR}/_lib.sh"

PY="$(resolve_repo_python)"
if [[ -z "${PYTHON:-}" && "$PY" == "python3" ]]; then
    echo "WARN: no venv with numpy found; falling back to python3" >&2
fi

exec "$PY" "${REPO_ROOT}/scripts/_audit_wake_corpus.py" "$@"
