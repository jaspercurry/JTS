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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_lib.sh
. "${SCRIPT_DIR}/_lib.sh"

PY="$(resolve_repo_python)"

exec "$PY" "${REPO_ROOT}/scripts/_export_wake_corpus_bundle.py" "$@"
