#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Run the thin Phase 0 custom wake-word training workflow.
#
# This is an off-Pi operator runner. It orchestrates the existing corpus export,
# feature-bank, real-positive injection, and LiveKit smoke/train/eval prep tools.
# It does not deploy or activate a model.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_lib.sh
. "${SCRIPT_DIR}/_lib.sh"

PY="$(resolve_repo_python)"

exec "$PY" "${REPO_ROOT}/scripts/_run_wake_training_phase0.py" "$@"
