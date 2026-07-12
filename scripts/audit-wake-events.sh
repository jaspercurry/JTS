#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Forensic audit of the wake-event corpus — sanity-check that
# captured audio is what we think it is + the SQLite columns are
# being populated as expected.
#
# Usage:
#   bash scripts/audit-wake-events.sh                          # audits wake-events/latest
#   bash scripts/audit-wake-events.sh wake-events/20260521T215308Z
#
# Run after `bash scripts/fetch-wake-events.sh` (which lands the
# corpus under wake-events/<UTC-timestamp>/ + updates the
# `wake-events/latest` symlink). Reports:
#   - Per-WAV integrity (format, duration, near-silent detection)
#   - Per-event AEC ON vs AEC OFF parity (duration match,
#     RMS comparison, cross-leg lag via speech-band xcorr)
#   - DB column-by-column populated count (catches "field never
#     written" bugs like the AEC OFF capture-ring fill bug from
#     the v1 ship)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_lib.sh
. "${SCRIPT_DIR}/_lib.sh"
TARGET="${1:-${REPO_ROOT}/wake-events/latest}"

PY="$(resolve_repo_python)"
if [[ -z "${PYTHON:-}" && "$PY" == "python3" ]]; then
    echo "WARN: no venv with numpy/scipy found; falling back to python3" >&2
fi

exec "$PY" "${REPO_ROOT}/scripts/_audit_wake_events.py" "$TARGET"
