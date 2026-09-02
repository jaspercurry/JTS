#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Prepare a tiny LiveKit wakeword train/export/eval smoke workdir.
#
# Usage:
#   bash scripts/prepare-wake-livekit-smoke.sh logs/wake-features/training-workdir
#   bash scripts/prepare-wake-livekit-smoke.sh logs/wake-train logs/livekit-smoke --run-livekit
#
# This is an off-Pi smoke harness. Placeholder negatives are not quality
# evidence; use real negative-hours feature banks before interpreting metrics.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_lib.sh
JTS_LIB_TARGET_OPTIONAL=1 . "${SCRIPT_DIR}/_lib.sh"  # analyses a local corpus; never targets a speaker

PY="$(resolve_repo_python)"

exec "$PY" "${REPO_ROOT}/scripts/_prepare_wake_livekit_smoke.py" "$@"
