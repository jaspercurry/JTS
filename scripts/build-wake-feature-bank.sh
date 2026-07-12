#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Build openWakeWord-compatible positive feature arrays from a wake corpus bundle.
#
# Usage:
#   bash scripts/build-wake-feature-bank.sh logs/wake-corpus-export/20260609T120000Z
#   bash scripts/build-wake-feature-bank.sh logs/wake-corpus-export/20260609T120000Z logs/features --leg chip_aec_150
#
# This is an offline data-prep tool. It requires openwakeword==0.6.0,
# onnxruntime, numpy, and staged openWakeWord ONNX feature models.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_lib.sh
. "${SCRIPT_DIR}/_lib.sh"

PY="$(resolve_repo_python)"

exec "$PY" "${REPO_ROOT}/scripts/_build_wake_feature_bank.py" "$@"
