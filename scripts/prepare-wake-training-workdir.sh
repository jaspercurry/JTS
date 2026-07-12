#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Stage JTS real-positive feature banks into a trainer workdir.
#
# Usage:
#   bash scripts/prepare-wake-training-workdir.sh logs/wake-corpus-export/20260609T120000Z/feature-bank
#   bash scripts/prepare-wake-training-workdir.sh logs/features logs/train-workdir --positive-weight 3
#
# This is an offline data-prep tool. It requires numpy and consumes the output
# from scripts/build-wake-feature-bank.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_lib.sh
. "${SCRIPT_DIR}/_lib.sh"

PY="$(resolve_repo_python)"

exec "$PY" "${REPO_ROOT}/scripts/_prepare_wake_training_workdir.py" "$@"
