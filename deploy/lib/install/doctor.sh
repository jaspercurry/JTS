#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# The closing jasper-doctor summary for deploy/install.sh.

# See ADR-0242 for both properties. RAISE CONDITION for MemoryMax=96M: an
# OOM kill of this transient run-u*.service unit in the journal (the deploy
# wrapper's report_oom_collateral lists it).
run_doctor_summary() {
    echo; echo "=== jasper-doctor --core ==="
    local rc=0
    systemd-run --quiet --wait --pipe --collect \
        -p MemoryMax=96M -p RuntimeMaxSec=60 \
        /opt/jasper/.venv/bin/jasper-doctor --core || rc=$?
    jasper_install_log "event=install.doctor_core rc=${rc}"
    return "${rc}"
}

# The doctor is advisory (ADR-0242): its rc is logged by run_doctor_summary and
# swallowed here so it cannot abort the install. Removal condition: drop this
# wrapper and point the row at run_doctor_summary when the core doctor gates.
run_doctor_summary_advisory() {
    run_doctor_summary || true
}
