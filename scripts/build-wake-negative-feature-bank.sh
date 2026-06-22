#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Build openWakeWord-compatible negative feature arrays from a wake corpus bundle.
#
# Examples:
#   bash scripts/build-wake-negative-feature-bank.sh logs/wake-corpus-export/20260609T120000Z
#   bash scripts/build-wake-negative-feature-bank.sh logs/wake-corpus-export/20260609T120000Z logs/negative-features --label-kind hard_negative
#   bash scripts/build-wake-negative-feature-bank.sh logs/negative-bundle --allow-unlabeled-as ambient_negative
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  PY="${REPO_ROOT}/.venv/bin/python"
else
  PY="${PYTHON:-python3}"
fi

exec "$PY" "${REPO_ROOT}/scripts/_build_wake_negative_feature_bank.py" "$@"
