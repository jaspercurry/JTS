#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Install apt packages in CI with bounded attempts, so a wedged mirror costs
# seconds instead of a job ceiling. See issue #2727 for the incident.
#
# Every apt invocation is capped by `timeout -k`: TERM first, KILL after the
# grace, because apt/dpkg can ignore TERM while stuck on socket I/O. A cap
# that kills apt mid-dpkg leaves the journal dirty and every later attempt
# then fails identically, so `dpkg --configure -a` runs between attempts.
#
# Worst-case wall clock, which the caller's `timeout-minutes` must exceed:
#   3 attempts x 2 apt commands x (30 s + 10 s kill grace) = 240 s
#   + 2 recoveries x (20 s + 10 s)                         =  60 s
#   + 2 sleeps x 5 s                                       =  10 s
#   -------------------------------------------------------------
#   = 310 s, i.e. `timeout-minutes: 6` (360 s) with room to spare.
# Keep that arithmetic true when changing any constant below.
#
# Remove the retry when apt-mirror hangs stop recurring in CI.

set -euo pipefail

ATTEMPTS=3
APT_TIMEOUT=30
DPKG_TIMEOUT=20
KILL_GRACE=10
RETRY_SLEEP=5

if [ "$#" -eq 0 ]; then
    echo "usage: $0 <package>..." >&2
    exit 2
fi

# Runners are non-root with sudo; release containers already run as root
# without it.
as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

for attempt in $(seq 1 "$ATTEMPTS"); do
    if as_root timeout -k "$KILL_GRACE" "$APT_TIMEOUT" apt-get update \
        && as_root timeout -k "$KILL_GRACE" "$APT_TIMEOUT" \
            apt-get install -y --no-install-recommends "$@"; then
        echo "apt ok on attempt ${attempt}/${ATTEMPTS}: $*"
        exit 0
    fi
    echo "apt attempt ${attempt}/${ATTEMPTS} failed for: $*" >&2
    if [ "$attempt" -lt "$ATTEMPTS" ]; then
        as_root timeout -k "$KILL_GRACE" "$DPKG_TIMEOUT" dpkg --configure -a || true
        sleep "$RETRY_SLEEP"
    fi
done

echo "apt failed after ${ATTEMPTS} attempts: $*" >&2
exit 1
