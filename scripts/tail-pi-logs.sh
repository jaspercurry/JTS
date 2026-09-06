#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Live-tail jasper logs from the Pi, redacted. Ctrl-C to stop.
#
# Usage:
#   bash scripts/tail-pi-logs.sh
#   bash scripts/tail-pi-logs.sh jasper-voice  # tail one unit
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
. "${SCRIPT_DIR}/_lib.sh"
# shellcheck disable=SC1091
. "${SCRIPT_DIR}/_diagnostic_redaction.sh"

if [[ $# -gt 0 ]]; then
    # Operator passed explicit unit names — tail just those.
    units=()
    for u in "$@"; do
        units+=(-u "$u")
    done
    remote_cmd="journalctl -f --output=short-iso ${units[*]}"
else
    # Default: every jasper-* unit, plus the renderers and their deps.
    # Uses systemd unit-name globbing (-u 'jasper-*', supported since
    # journalctl v245) so new daemons land in the tail automatically.
    remote_cmd="journalctl -f --output=short-iso \
        -u 'jasper-*' -u librespot -u shairport-sync -u nqptp \
        -u bluealsa -u bluealsa-aplay -u bt-agent"
fi

# Line-by-line through the shared redactor, not a piped `sed -u`: GNU's
# unbuffered flag (-u) and BSD's (-l) aren't the same flag, and this
# script runs on both. A read loop is unbuffered on either sed build and
# fine for a human-paced tail; `ssh -t` still owns the remote pty, so
# Ctrl-C still stops journalctl.
ssh -t "${PI_USER}@${PI_HOST}" "$remote_cmd" \
    | while IFS= read -r line; do
        printf '%s\n' "$line" | redact_jasper_diagnostics
    done
