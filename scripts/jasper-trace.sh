#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Live tail of cross-daemon "event=" log lines from the Pi. Streams
# one event per line, timestamp + unit + message, until Ctrl-C.
#
# Usage:
#   bash scripts/jasper-trace.sh                # last 5 min, follow
#   SINCE='1 hour ago' bash scripts/jasper-trace.sh
#   PI_HOST=192.168.1.42 bash scripts/jasper-trace.sh
#
# Pattern is wide enough to catch both the structured `event=` prefix
# (duck holders, control /volume handlers) and the pre-existing
# high-signal log strings ("wake detected", "turn ended",
# "source transition", "preempting", "active source"). Add more
# patterns as new event log lines land.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
. "${SCRIPT_DIR}/_lib.sh"
SINCE="${SINCE:-5 minutes ago}"

PATTERN='event=|wake detected|turn ended|source transition|preempting|active source'

# journalctl expands the unit glob; grep filters before logs cross SSH.
remote_cmd="$(quote_args journalctl --output=short-iso -f --since "$SINCE" -u 'jasper-*') \
    | $(quote_args grep --line-buffered -E "$PATTERN")"

exec ssh -o BatchMode=yes -o ConnectTimeout=5 \
    "${PI_USER}@${PI_HOST}" "${remote_cmd}"
