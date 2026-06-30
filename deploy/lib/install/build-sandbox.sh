#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Unified, RAM/CPU-aware, production-isolated build policy for
# deploy/install.sh and its sourced libs.
#
# One invariant: no installer build step may starve or kill a live
# production daemon. A build that runs out of memory must die itself —
# never nginx, jasper-voice, jasper-camilla, or any other running
# service. Canonical design: docs/HANDOFF-build-sandbox.md.
#
# Two complementary levers:
#   1. build_sandbox_jobs <kb_per_job>  — RAM-aware `-j`. Generalizes the
#      PR #899 webrtc point-fix (_webrtc_compile_jobs delegates here).
#      Lower parallelism -> lower peak RAM -> lower OOM *probability*.
#   2. run_contained_build <label> -- <cmd...>  — run the build inside a
#      transient `systemd-run --scope` whose properties make it the
#      preferred OOM victim and yield CPU/IO to audio daemons. Changes
#      *who dies* under pressure (the build, not a daemon). Works without
#      the memory cgroup controller because OOMScoreAdjust is a per-PID
#      /proc knob — the protection holds even on a never-rebooted,
#      far-behind box where the per-unit cgroup limits are still no-ops.
#
# The build policy is the INVERSE of the audio-daemon policy: builds get
# a positive OOMScoreAdjust (kill me first), are ALLOWED to swap (slow is
# fine; completion matters), and run at low CPU/IO weight (yield to
# playback) — where audio daemons get a negative OOMScoreAdjust and
# MemorySwapMax=0. This split is pinned by tests.
#
# Functions assume install.sh's `set -euo pipefail`. Designed to be
# sourced by install.sh AND by tests (bash -c "source install.sh && ...").

# Per-toolchain RAM budget per compile job, in kB. These ARE the policy —
# the single place that answers "what -j budget does a C / C++ build get?"
# Named (not bare literals at the call sites) for the same reason
# rust-daemons.sh names RUST_LOW_MEMORY_BUILD_THRESHOLD_KB. Calibrated to
# the worst-case translation unit per toolchain: webrtc's -O3
# audio_processing_impl.cc peaks > 1 GB in cc1plus; a C autotools -O2 TU
# (shairport-sync/nqptp) peaks a few hundred MB.
#
# Consumed by the installer files that source this lib (install.sh's
# _webrtc_compile_jobs, renderers.sh's makes); shellcheck lints this lib
# standalone and can't follow the dynamic `source` path, so it reports
# SC2034 (appears unused) — suppressed per constant below.
# shellcheck disable=SC2034
BUILD_SANDBOX_KB_PER_JOB_CPP=1500000   # C++ -O3 (webrtc-audio-processing)
# shellcheck disable=SC2034
BUILD_SANDBOX_KB_PER_JOB_C=400000      # C -O2 autotools (shairport-sync, nqptp)
BUILD_SWAP_CREATED=0
BUILD_SWAP_PATH_ACTIVE=""

# clamp(memtotal_kb / kb_per_job, 1, nproc). All args injectable so the
# math is unit-testable across the full Pi SKU range and per-toolchain
# per-job RAM budgets. $1=MemTotal kB, $2=nproc, $3=kB budget per job.
_ram_bounded_jobs() {
    local memtotal_kb="${1:-0}"
    local ncpu="${2:-1}"
    local kb_per_job="${3:-1500000}"
    awk -v m="${memtotal_kb}" -v n="${ncpu}" -v k="${kb_per_job}" '
        BEGIN {
            if (k < 1) k = 1
            if (n < 1) n = 1
            jobs = int(m / k)
            if (jobs < 1) jobs = 1
            if (jobs > n) jobs = n
            printf "%d\n", jobs
        }
    '
}

# Resolve a RAM-aware job count for THIS host. Reads MemTotal + nproc
# (both overridable for tests, mirroring rust-daemons.sh's
# JASPER_RUST_MEMINFO_FILE). $1 = kB budget per compile job.
build_sandbox_jobs() {
    local kb_per_job="${1:-1500000}"
    local meminfo="${JASPER_BUILD_MEMINFO_FILE:-/proc/meminfo}"
    local memtotal_kb ncpu
    memtotal_kb="$(awk '/^MemTotal:/ { print $2; exit }' "${meminfo}" 2>/dev/null || true)"
    ncpu="${JASPER_BUILD_NPROC:-$(nproc 2>/dev/null || echo 1)}"
    _ram_bounded_jobs "${memtotal_kb:-0}" "${ncpu:-1}" "${kb_per_job}"
}

build_swap_memtotal_kb() {
    local meminfo="${JASPER_BUILD_MEMINFO_FILE:-/proc/meminfo}"
    awk '/^MemTotal:/ { print $2; exit }' "${meminfo}" 2>/dev/null || true
}

build_swap_required() {
    local setting="${JASPER_BUILD_SWAP:-auto}"
    case "${setting}" in
        1|true|TRUE|yes|YES|on|ON)
            return 0
            ;;
        0|false|FALSE|no|NO|off|OFF)
            return 1
            ;;
        ""|auto|AUTO)
            ;;
        *)
            echo "  WARN: invalid JASPER_BUILD_SWAP=${setting}; using auto" >&2
            ;;
    esac

    local mem_kb threshold_kb
    mem_kb="$(build_swap_memtotal_kb)"
    threshold_kb="${RUST_LOW_MEMORY_BUILD_THRESHOLD_KB:-1200000}"
    case "${mem_kb}:${threshold_kb}" in
        *[!0-9:]*|":")
            return 1
            ;;
    esac
    [[ "${mem_kb}" -lt "${threshold_kb}" ]]
}

_build_swap_cmd() {
    local name="$1"
    if command -v "${name}" >/dev/null 2>&1; then
        command -v "${name}"
    elif [[ -x "/sbin/${name}" ]]; then
        printf '/sbin/%s\n' "${name}"
    elif [[ -x "/usr/sbin/${name}" ]]; then
        printf '/usr/sbin/%s\n' "${name}"
    else
        return 1
    fi
}

setup_build_swap_if_needed() {
    build_swap_required || return 0

    local path="${JASPER_BUILD_SWAP_PATH:-/var/tmp/jasper-build.swap}"
    local size_mb="${JASPER_BUILD_SWAP_SIZE_MB:-2048}"
    local priority="${JASPER_BUILD_SWAP_PRIORITY:-200}"
    case "${size_mb}:${priority}" in
        *[!0-9:]*|":")
            echo "  ERROR: invalid build swap size/priority: size=${size_mb} priority=${priority}" >&2
            return 2
            ;;
    esac

    if grep -qsE "^${path//\//\\/}[[:space:]]" /proc/swaps 2>/dev/null; then
        _build_sandbox_log "swap_present" "path=${path} already active"
        return 0
    fi

    local swapon_cmd swapoff_cmd mkswap_cmd
    swapon_cmd="$(_build_swap_cmd swapon)" || {
        echo "  ERROR: swapon not found; cannot create low-memory build swap" >&2
        return 1
    }
    swapoff_cmd="$(_build_swap_cmd swapoff)" || {
        echo "  ERROR: swapoff not found; cannot safely clean build swap" >&2
        return 1
    }
    mkswap_cmd="$(_build_swap_cmd mkswap)" || {
        echo "  ERROR: mkswap not found; cannot create low-memory build swap" >&2
        return 1
    }

    install -d -m 0755 "$(dirname "${path}")"
    rm -f "${path}"
    if command -v fallocate >/dev/null 2>&1; then
        fallocate -l "${size_mb}M" "${path}" 2>/dev/null \
            || dd if=/dev/zero of="${path}" bs=1M count="${size_mb}" status=none
    else
        dd if=/dev/zero of="${path}" bs=1M count="${size_mb}" status=none
    fi
    chmod 0600 "${path}"
    "${mkswap_cmd}" "${path}" >/dev/null
    "${swapon_cmd}" -p "${priority}" "${path}"
    BUILD_SWAP_CREATED=1
    BUILD_SWAP_PATH_ACTIVE="${path}"
    _build_sandbox_log "swap_enabled" \
        "path=${path} size_mb=${size_mb} priority=${priority} mem_kb=$(build_swap_memtotal_kb)"
}

cleanup_build_swap() {
    [[ "${BUILD_SWAP_CREATED:-0}" == "1" ]] || return 0
    local path="${BUILD_SWAP_PATH_ACTIVE:-}"
    [[ -n "${path}" ]] || return 0
    local swapoff_cmd
    if swapoff_cmd="$(_build_swap_cmd swapoff)"; then
        "${swapoff_cmd}" "${path}" >/dev/null 2>&1 \
            || _build_sandbox_log "swapoff_failed" "path=${path}"
    fi
    rm -f "${path}"
    BUILD_SWAP_CREATED=0
    BUILD_SWAP_PATH_ACTIVE=""
    _build_sandbox_log "swap_removed" "path=${path}"
}

# Default MemoryHigh (soft throttle): ~85% of MemTotal, leaving headroom
# for PID1/sshd/the running daemons. Soft, so the build leans on swap
# past it rather than being killed. Empty when MemTotal is unreadable
# (then no MemoryHigh line is emitted and OOMScoreAdjust is the sole
# protection). Echoes a kB value with a K suffix for systemd.
_build_sandbox_default_memory_high() {
    local meminfo="${JASPER_BUILD_MEMINFO_FILE:-/proc/meminfo}"
    local memtotal_kb
    memtotal_kb="$(awk '/^MemTotal:/ { print $2; exit }' "${meminfo}" 2>/dev/null || true)"
    case "${memtotal_kb}" in
        ""|*[!0-9]*) return 0 ;;
    esac
    awk -v m="${memtotal_kb}" 'BEGIN { v = int(m * 0.85); if (v < 1) v = 1; printf "%dK\n", v }'
}

# Emit the systemd-run --property=... list (one per line) for a contained
# build. Pure string construction (reads only env + the injectable
# meminfo) so the inverse-policy invariants are unit-testable without
# systemd. $1 = human label.
build_sandbox_props() {
    local label="${1:-build}"
    # NOTE: OOMScoreAdjust is intentionally NOT a --property here. It is an
    # exec/service property that a transient `systemd-run --scope` unit
    # REJECTS ("Unknown assignment: OOMScoreAdjust=..."), which fails the
    # whole build. The build's positive "kill me first" oom_score_adj is
    # applied to the build process via choom instead (build_sandbox_oom_prefix),
    # and the scope's children inherit it. MemoryAccounting / CPUWeight /
    # IOWeight / MemoryHigh ARE valid scope (cgroup) properties and stay here.
    printf '%s\n' \
        "--property=Description=JTS contained build: ${label}" \
        "--property=MemoryAccounting=yes" \
        "--property=CPUWeight=${JASPER_BUILD_SANDBOX_CPU_WEIGHT:-20}" \
        "--property=IOWeight=${JASPER_BUILD_SANDBOX_IO_WEIGHT:-20}"
    # NOTE: deliberately NO MemorySwapMax=0 — builds may legitimately need
    # swap to complete a >1 GB -O3 TU on a 1 GB Pi. This is the key
    # difference from jts-audio.slice and pi-run-diagnostic.sh.
    local mem_high="${JASPER_BUILD_SANDBOX_MEMORY_HIGH:-}"
    if [[ -z "${mem_high}" ]]; then
        mem_high="$(_build_sandbox_default_memory_high)"
    fi
    [[ -n "${mem_high}" ]] && printf '%s\n' "--property=MemoryHigh=${mem_high}"
    # MemoryMax (hard kill) is opt-in: a too-low cap would kill a
    # legitimate single-TU compile and regress installs that used to
    # squeak by on swap. RuntimeMaxSec is opt-in for the same "never kill
    # a slow-but-progressing build" reason (a Zero 2 W build can be long).
    [[ -n "${JASPER_BUILD_SANDBOX_MEMORY_MAX:-}" ]] \
        && printf '%s\n' "--property=MemoryMax=${JASPER_BUILD_SANDBOX_MEMORY_MAX}"
    [[ -n "${JASPER_BUILD_SANDBOX_RUNTIME_MAX:-}" ]] \
        && printf '%s\n' "--property=RuntimeMaxSec=${JASPER_BUILD_SANDBOX_RUNTIME_MAX}"
    return 0
}

# Emit the argv PREFIX that gives the contained build its positive
# "kill me first" oom_score_adj. OOMScoreAdjust is an exec property a
# `systemd-run --scope` unit cannot take, so we set it on the build
# process with choom (util-linux), which the build and its children
# inherit. Pure (reads only env + PATH) so it is unit-testable without
# systemd. FAIL-OPEN: emits nothing when choom is unavailable, so the
# build still runs (just without the OOM bias) rather than being blocked.
build_sandbox_oom_prefix() {
    command -v choom >/dev/null 2>&1 || return 0
    printf '%s\n' choom -n "${JASPER_BUILD_SANDBOX_OOM_SCORE_ADJ:-900}" --
}

# Emit a structured event line to both stdout (deploy transcript) and
# journald (`journalctl -t jasper-install | grep event=build_sandbox`).
# journald is persistent (PR #160), so the containment decision survives
# the watchdog reboot that a real build-OOM can trigger — the exact case
# this module exists to make diagnosable. Mirrors memory-resilience.sh's
# _mem_log; best-effort, never fails a build.
# Args: $1=event suffix, $2=detail (free text).
_build_sandbox_log() {
    local event="$1" detail="$2"
    echo "  build-sandbox: ${detail}"
    logger -t jasper-install -- "event=build_sandbox.${event} ${detail}" 2>/dev/null || true
}

# True when this host can and should contain builds. `auto` (default)
# contains iff root AND systemd-run is on PATH AND systemd is the running
# init (/run/systemd/system exists) — false on CI, macOS, and containers
# without systemd, where builds then run directly and unchanged.
_build_sandbox_active() {
    case "${JASPER_BUILD_SANDBOX:-auto}" in
        0|false|FALSE|no|NO|off|OFF)
            return 1
            ;;
        1|true|TRUE|yes|YES|on|ON)
            command -v systemd-run >/dev/null 2>&1
            ;;
        *)  # auto
            [[ "${EUID:-$(id -u)}" -eq 0 ]] \
                && command -v systemd-run >/dev/null 2>&1 \
                && [[ -d /run/systemd/system ]]
            ;;
    esac
}

# Run a heavy build contained when possible, directly otherwise.
# Usage: run_contained_build <label> -- <cmd> [args...]
#
# No post-failure retry: the contained command's exit status propagates
# verbatim, so a real compile failure is never masked by a second
# uncontained run. systemd-run --scope inherits the caller's cwd + stdio,
# so build output still streams to the deploy transcript and a preceding
# `cd` still applies.
run_contained_build() {
    local label="${1:-build}"
    shift || true
    [[ "${1:-}" == "--" ]] && shift

    if [[ $# -eq 0 ]]; then
        echo "  build-sandbox: run_contained_build '${label}' called with no command" >&2
        return 2
    fi

    if ! _build_sandbox_active; then
        _build_sandbox_log "uncontained" \
            "label=${label} reason=systemd-unavailable-or-disabled"
        "$@"
        return $?
    fi

    local -a props=()
    local prop
    while IFS= read -r prop; do
        props+=("${prop}")
    done < <(build_sandbox_props "${label}")
    local sanitized unit
    sanitized="$(printf '%s' "${label}" | tr -c 'a-zA-Z0-9_-' '_')"
    unit="jts-build-${sanitized}-$$.scope"
    _build_sandbox_log "contained" "label=${label} unit=${unit}"
    local -a run=(
        systemd-run --scope --quiet --collect
        "--unit=${unit}" "${props[@]}" --
    )
    # choom sets the build's oom_score_adj (the scope-compatible substitute
    # for the OOMScoreAdjust property a --scope unit rejects). Empty when
    # choom is unavailable; guarded so an empty array is safe under set -u.
    local -a oom_prefix=()
    local oom_arg
    while IFS= read -r oom_arg; do
        oom_prefix+=("${oom_arg}")
    done < <(build_sandbox_oom_prefix)
    [[ ${#oom_prefix[@]} -gt 0 ]] && run+=("${oom_prefix[@]}")
    run+=("$@")
    "${run[@]}"
}
