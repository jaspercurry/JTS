#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Shared dead-playback-pipe probe and runtime-safe statefile repair for the
# two Camilla ExecStartPre guards. The sourcing guard provides log() and the
# STATEFILE, BASE_CONFIG, RUNTIME_SAFE_GRAPH, and PROBE_TIMEOUT variables.

# camilla_guard_repair_statefile <reason> <from-config> <repair-detail>
# Always returns 0: both callers are deliberately fail-open start-path guards.
camilla_guard_repair_statefile() {
    local why="$1" from="$2" repair_detail="$3" repaired_to output
    if ! command -v "$RUNTIME_SAFE_GRAPH" >/dev/null 2>&1; then
        log skip reason=runtime_contract_unavailable "detail=$why"
        return 0
    fi
    if output="$(
        "$RUNTIME_SAFE_GRAPH" runtime-safe-graph \
            --statefile "$STATEFILE" \
            --flat-config "$BASE_CONFIG" \
            --write-statefile \
            --json 2>&1
    )"; then
        repaired_to="$(
            sed -n 's/^[[:space:]]*config_path:[[:space:]]*//p' "$STATEFILE" \
                | head -1 | tr -d '"' | tr -d "'"
        )"
        log repaired "reason=$why from=$from to=${repaired_to:-unknown}" \
            "detail=$repair_detail"
    else
        log skip reason=runtime_contract_blocked "detail=$why" \
            "message=$(printf '%s' "$output" | tr '\n' ' ' | cut -c1-240)"
    fi
    return 0
}

# camilla_guard_check_playback_pipe_or_repair \
#   <pipe> <absent-reason> <no-reader-reason> <healthy-reason> \
#   <probe-unavailable-reason> <from-config> <repair-detail>
# Handles the complete playback-pipe decision then exits the sourcing guard.
camilla_guard_check_playback_pipe_or_repair() {
    local pipe_path="$1" absent_reason="$2" no_reader_reason="$3" \
        healthy_reason="$4" probe_unavailable_reason="$5" from="$6" \
        repair_detail="$7" why
    if [ -p "$pipe_path" ]; then
        if ! command -v timeout >/dev/null 2>&1; then
            log ok "reason=$probe_unavailable_reason"
            exit 0
        fi
        # Pass the path as an argument, never interpolated shell source. A
        # writer open succeeds immediately iff a FIFO reader is present.
        # shellcheck disable=SC2016  # $1 expands inside the child bash
        if timeout "$PROBE_TIMEOUT" bash -c 'exec 3>"$1"' _ "$pipe_path" \
                2>/dev/null; then
            log ok "reason=$healthy_reason"
            exit 0
        fi
        why="$no_reader_reason"
    else
        why="$absent_reason"
    fi

    camilla_guard_repair_statefile "$why" "$from" "$repair_detail"
    exit 0
}
