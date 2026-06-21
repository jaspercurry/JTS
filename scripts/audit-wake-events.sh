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

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${1:-${REPO_ROOT}/wake-events/latest}"

# Find a Python that has scipy + numpy (needed for the speech-band
# xcorr). Try the worktree's venv first, then the main checkout's
# venv (worktrees don't have their own venv by default), then
# system python with a warning.
CANDIDATES=(
    "${REPO_ROOT}/.venv/bin/python"
    # When running from a worktree under .claude/worktrees/<name>/,
    # the main repo's venv is three levels up. Resolve via
    # `git common-dir` to land at the .git of the main checkout.
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
    echo "WARN: no venv with numpy/scipy found; falling back to python3" >&2
    PY="python3"
fi

exec "$PY" "${REPO_ROOT}/scripts/_audit_wake_events.py" "$TARGET"
