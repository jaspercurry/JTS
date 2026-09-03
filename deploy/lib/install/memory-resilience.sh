#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Memory-pressure resilience migrations for deploy/install.sh.
#
# Extracted verbatim from install.sh (the installer remains the only
# caller; it sources this file REPO_DIR-relative from the rsync
# checkout). Functions assume install.sh's globals (REPO_DIR) and
# `set -euo pipefail` from the sourcing shell.

# --- Stage 1 memory-pressure resilience helpers ---
#
# Split into focused per-step functions to make each step
# individually testable + readable. Coordinator is
# `migrate_memory_resilience` below. All log via stdout (deploy-to-pi
# transcript capture) AND `logger -t jasper-install` (structured
# journald lines tagged `event=memory_resilience.*` for later
# `journalctl -t jasper-install` queries).


# Emit a structured event line to both stdout and journald.
# Args: $1=event_name (without the prefix), $2=detail (free text).
_mem_log() {
    local event="$1" detail="$2"
    echo "  memory_resilience: ${detail}"
    # Best-effort journald log — never fails the install.
    logger -t jasper-install -- "event=memory_resilience.${event} ${detail}" 2>/dev/null || true
}


# --- Reboot-required marker (issue #2110) ---
#
# The zram and cgroup-memory migrations below change boot-only state
# (kernel cmdline args, zram device sizing) that only takes effect after
# a reboot. Rather than leave callers (onboard.sh, deploy-to-pi.sh)
# parsing this file's log prose for "REBOOT REQUIRED", each migration
# records its own reason line here — one canonical, machine-readable
# marker. /run is tmpfs, so an actual reboot clears it on its own (same
# idiom as jasper.active_speaker.startup_hold's /run marker); no
# separate cleanup step is needed.
REBOOT_REQUIRED_MARKER="${JTS_REBOOT_REQUIRED_MARKER:-/run/jasper-install/reboot_required}"

# Set (non-empty reason) or clear (empty reason) this step's line in the
# marker, identified by `key`. Each migration calls this unconditionally
# on every run — clearing when its own condition is no longer true — so
# the file always reflects only what THIS run still needs a reboot for,
# regardless of the other migration's call order.
_set_reboot_required_reason() {
    local key="$1" reason="$2"
    local dir tmp
    dir="$(dirname "${REBOOT_REQUIRED_MARKER}")"
    install -d -m 0755 "${dir}"
    tmp="${REBOOT_REQUIRED_MARKER}.tmp.$$"
    { [[ -f "${REBOOT_REQUIRED_MARKER}" ]] \
        && grep -v "^${key}=" "${REBOOT_REQUIRED_MARKER}" || true; } > "${tmp}"
    if [[ -n "${reason}" ]]; then
        printf '%s=%s\n' "${key}" "${reason}" >> "${tmp}"
    fi
    if [[ -s "${tmp}" ]]; then
        chmod 0644 "${tmp}"
        mv "${tmp}" "${REBOOT_REQUIRED_MARKER}"
    else
        rm -f "${tmp}" "${REBOOT_REQUIRED_MARKER}"
    fi
}

# The one line that surfaces the marker, printed once per install.sh run
# after both Stage 1 (zram) and Stage 2 (cgroup) migrations have had a
# chance to write to it.
_print_reboot_required_marker() {
    if [[ -s "${REBOOT_REQUIRED_MARKER}" ]]; then
        echo "  REBOOT REQUIRED (${REBOOT_REQUIRED_MARKER}): $(paste -sd';' "${REBOOT_REQUIRED_MARKER}")"
    fi
}


# Compute vm.min_free_kbytes from MemTotal_kB.
# Formula: clamp(0.02 × memtotal_kb, 8192, 262144) — 2% of total RAM,
# with an 8 MB floor (Pi Foundation default; never reduce below) and
# 256 MB ceiling. See deploy/sysctl/99-jts-vm.conf header for rationale.
# Args:  $1 = memtotal_kb (integer)
# Output: integer to stdout
#
# Extracted as a standalone function so tests can drive it with
# synthetic memtotal values across the full Pi 5 SKU range.
_compute_min_free_kbytes() {
    local memtotal_kb="$1"
    awk -v m="${memtotal_kb}" '
        BEGIN {
            v = int(m * 0.02 + 0.5)
            if (v < 8192) v = 8192
            if (v > 262144) v = 262144
            printf "%d\n", v
        }
    '
}


# Step 1 — vm.* sysctls. Renders the template (substituting the
# RAM-aware min_free_kbytes value) and applies it. Returns 0 on
# success, non-zero on a step-internal failure (caller increments
# error counter).
_apply_jts_sysctls() {
    local memtotal_kb
    memtotal_kb=$(awk '/MemTotal:/ { print $2 }' /proc/meminfo 2>/dev/null)
    local min_free_kb
    if [[ -n "${memtotal_kb}" && "${memtotal_kb}" =~ ^[0-9]+$ ]]; then
        min_free_kb=$(_compute_min_free_kbytes "${memtotal_kb}")
    fi
    if [[ -z "${min_free_kb}" || ! "${min_free_kb}" =~ ^[0-9]+$ ]]; then
        # Fallback if /proc/meminfo is unreadable.
        min_free_kb=16384
        _mem_log "sysctls.fallback" \
            "couldn't read MemTotal; using fallback min_free_kbytes=${min_free_kb}"
    fi
    if ! sed -e "s/__VM_MIN_FREE_KBYTES__/${min_free_kb}/g" \
            "${REPO_DIR}/deploy/sysctl/99-jts-vm.conf" \
            > /etc/sysctl.d/99-jts-vm.conf; then
        _mem_log "sysctls.render_failed" \
            "WARN — failed to render /etc/sysctl.d/99-jts-vm.conf"
        return 1
    fi
    chmod 0644 /etc/sysctl.d/99-jts-vm.conf
    if ! sysctl --system >/dev/null 2>&1; then
        _mem_log "sysctls.apply_failed" \
            "WARN — sysctl --system failed; tunings live after reboot"
        return 1
    fi
    _mem_log "sysctls.applied" \
        "vm.* sysctls applied (min_free_kbytes=${min_free_kb} kB per RAM)"
    return 0
}


# Step 2 — MGLRU min_ttl_ms (thrashing prevention).
# On kernels without MGLRU (< 6.1), the `w-` tmpfiles directive
# silently skips the missing path — so this is safe even on older
# kernels.
_apply_jts_mglru() {
    if ! install -m 0644 "${REPO_DIR}/deploy/tmpfiles/jts-mglru.conf" \
            /etc/tmpfiles.d/; then
        _mem_log "mglru.install_failed" \
            "WARN — failed to install MGLRU tmpfiles config"
        return 1
    fi
    # --prefix scopes the apply to just our file (not the whole
    # tmpfiles tree, which would touch unrelated paths).
    if systemd-tmpfiles --create --prefix=/sys/kernel/mm/lru_gen \
            >/dev/null 2>&1; then
        _mem_log "mglru.applied" "MGLRU min_ttl_ms applied"
    else
        _mem_log "mglru.unsupported" \
            "MGLRU tmpfiles installed (no-op on kernels < 6.1)"
    fi
    return 0
}


# Step 3 — zram sizing via rpi-swap drop-in. rpi-swap is the Trixie
# standard zram manager (replaces dphys-swapfile); on older RPi OS
# the user may be on something else — skip gracefully there.
#
# IMPORTANT: rpi-swap is a systemd *generator*, not a service.
# `systemctl restart rpi-swap` is not a thing — the generator runs
# once at early boot and sizes the zram device. Per swap.conf(5):
# "After modifying any swap configuration, you must reboot the
# system for changes to take effect."
_compute_target_zram_bytes() {
    local memtotal_kb="$1"
    if [[ "${memtotal_kb}" =~ ^[0-9]+$ && "${memtotal_kb}" -gt 0 ]]; then
        echo $((memtotal_kb * 1024 / 2))
        return 0
    fi
    # Fallback for unreadable /proc/meminfo: the original 1 GB Pi target.
    echo $((520 * 1024 * 1024))
}

_apply_jts_zram_dropin() {
    # Recomputed fresh every run below — clearing first means an early
    # return (skip, install failure) leaves no stale reason behind.
    _set_reboot_required_reason zram ""
    if [[ ! -d /etc/rpi ]]; then
        _mem_log "zram.skip" \
            "/etc/rpi not present (rpi-swap not installed) — skipped zram sizing"
        return 0
    fi
    install -d -m 0755 /etc/rpi/swap.conf.d
    if ! install -m 0644 "${REPO_DIR}/deploy/rpi-swap/50-jts.conf" \
            /etc/rpi/swap.conf.d/; then
        _mem_log "zram.install_failed" \
            "WARN — failed to install rpi-swap drop-in"
        return 1
    fi
    # Check whether zram is already the target size.
    local cur_zram_bytes=0
    if [[ -r /sys/block/zram0/disksize ]]; then
        cur_zram_bytes=$(cat /sys/block/zram0/disksize 2>/dev/null || echo 0)
    fi
    local memtotal_kb target_zram_bytes target_zram_mb
    memtotal_kb=$(awk '/MemTotal:/ { print $2 }' /proc/meminfo 2>/dev/null)
    target_zram_bytes=$(_compute_target_zram_bytes "${memtotal_kb:-}")
    target_zram_mb=$((target_zram_bytes / 1024 / 1024))
    local zram_diff=$((cur_zram_bytes - target_zram_bytes))
    # Within ±60 MB of target counts as "already correct."
    if [[ ${zram_diff#-} -lt 62914560 ]]; then
        _mem_log "zram.already_sized" \
            "zram drop-in installed; live size already ~50% RAM"
    else
        local reason="zram drop-in installed; resize pending (current: $((cur_zram_bytes / 1024 / 1024)) MB → target: ~${target_zram_mb} MB)"
        _mem_log "zram.reboot_required" "${reason}"
        _set_reboot_required_reason zram "${reason}"
    fi
    return 0
}


# Step 4 — live-write /proc/PID/oom_score_adj for each running
# critical daemon plus the sshd listener. The OOMScoreAdjust=
# directive in each .service file only takes effect on next process
# start; install.sh doesn't
# restart jasper-camilla (Rust binary, intentionally never auto-
# restarted per AGENTS.md) or jasper-mux (not in install.sh's
# restart list), so their running processes would sit at adj=0
# until reboot. Live-writing sets the kernel-visible value
# immediately — zero audio glitch, fully reversible.
#
# Reads the canonical target values from jasper._oom_adj.INSTALL_LIVE_WRITE
# (single source of truth shared with jasper-doctor).
_apply_jts_oom_score_adj_live() {
    # Read the canonical target values from the Python package.
    # Requires /opt/jasper/.venv to be installed (install_jasper
    # has already run by this point in main).
    local oom_adj_data
    if ! oom_adj_data=$(/opt/jasper/.venv/bin/python3 -c \
            'from jasper._oom_adj import INSTALL_LIVE_WRITE
for k, v in INSTALL_LIVE_WRITE.items():
    print(f"{k}={v}")' 2>/dev/null); then
        _mem_log "oom_score_adj.source_unavailable" \
            "WARN — couldn't read jasper._oom_adj; live-write skipped"
        return 1
    fi
    local live_writes=0 live_skips=0
    while IFS='=' read -r unit want; do
        [[ -z "${unit}" ]] && continue
        local pid
        pid=$(systemctl show -p MainPID --value "${unit}.service" 2>/dev/null || true)
        if [[ -z "${pid}" || "${pid}" == "0" ]]; then
            live_skips=$((live_skips+1))
            continue
        fi
        if [[ -w "/proc/${pid}/oom_score_adj" ]]; then
            if echo "${want}" > "/proc/${pid}/oom_score_adj" 2>/dev/null; then
                live_writes=$((live_writes+1))
            fi
        fi
    done <<< "${oom_adj_data}"
    _mem_log "oom_score_adj.applied" \
        "live-set oom_score_adj on ${live_writes} running daemon(s) (${live_skips} not running)"
    return 0
}


# Coordinator. Triggered by the 2026-05-23 incident: a PIO compile
# pushed the 1 GB Pi 5 into zram-thrash for 2+ minutes, kernel
# watchdog never fired because PID 1 stayed barely-alive.
#
# Each step is independent — a failure in one doesn't block the
# others. Stage 1 protections work today on the stock RPi kernel
# without enabling the memory cgroup controller (Stage 2 work).
# Idempotent under repeated runs.
migrate_memory_resilience() {
    local errors=0
    _apply_jts_sysctls           || errors=$((errors+1))
    _apply_jts_mglru             || errors=$((errors+1))
    _apply_jts_zram_dropin       || errors=$((errors+1))
    _apply_jts_oom_score_adj_live || errors=$((errors+1))
    if (( errors > 0 )); then
        _mem_log "summary.degraded" \
            "${errors} step(s) failed; system functional but degraded — see above"
    else
        _mem_log "summary.ok" "all 4 steps succeeded"
    fi
    return 0  # never fails install — best-effort migration
}

# Stage 2 audio-protection: enable the Linux memory cgroup controller
# so jts-audio.slice's `MemorySwapMax=0` actually enforces.
#
# Raspberry Pi OS can inject `cgroup_disable=memory` into the kernel's
# boot arguments to save accounting overhead. On real Zero 2 W hardware
# (2026-06-12), leaving that token present still disabled the controller
# even after appending `cgroup_enable=memory cgroup_memory=1`, so this
# migration must actively remove the disable token and then add the
# enable tokens.
#
# Also adds `psi=1` defensively. RPi 6.12.x ships CONFIG_PSI=y +
# CONFIG_PSI_DEFAULT_DISABLED=y; the boot param turns PSI on. No-op
# on kernels that don't support PSI. Enables `/proc/pressure/`
# observability; not required for Stage 2 audio (which uses
# MemorySwapMax=0, not PSI). The `/system/` dashboard's memory-pressure
# tile reads /proc/pressure/memory when this is in effect.
#
# IDEMPOTENT: existing non-conflicting cmdline.txt values are preserved
# unchanged. Operator-added tokens (custom kernel flags, etc.) survive.
#
# Kernel command line only re-reads at boot — a change here is recorded
# in REBOOT_REQUIRED_MARKER (see _set_reboot_required_reason above) and
# surfaced by the single _print_reboot_required_marker line at the end of
# this function, called from every return path so it reflects both this
# migration and _apply_jts_zram_dropin's (called first by every caller).
migrate_cgroup_memory_enabled() {
    local cmdline_file="${JTS_BOOT_CMDLINE_FILE:-/boot/firmware/cmdline.txt}"
    _set_reboot_required_reason cgroup_memory ""
    if [[ ! -f "${cmdline_file}" ]]; then
        echo "  cgroup_memory: WARN — ${cmdline_file} missing (not RPi OS?); skipped"
        _print_reboot_required_marker
        return 0
    fi
    local current
    current=$(tr '\n' ' ' < "${cmdline_file}")
    local changed=0
    local removed_disable=0

    local existing_tokens=()
    read -r -a existing_tokens <<< "${current}"
    local filtered_tokens=()
    local token
    for token in "${existing_tokens[@]}"; do
        if [[ "${token}" == "cgroup_disable=memory" ]]; then
            removed_disable=1
            changed=1
            continue
        fi
        filtered_tokens+=("${token}")
    done
    current="${filtered_tokens[*]}"

    local to_add=()
    for token in "cgroup_enable=memory" "cgroup_memory=1" "psi=1"; do
        if [[ " ${current} " != *" ${token} "* ]]; then
            to_add+=("${token}")
            changed=1
        fi
    done
    if (( changed == 0 )); then
        echo "  cgroup_memory: cmdline.txt already configured"
        _print_reboot_required_marker
        return 0
    fi
    # cmdline.txt is a SINGLE line — preserve that. Append the new
    # tokens with spaces. Strip trailing newline if any.
    {
        printf '%s' "${current% }"
        for t in "${to_add[@]}"; do
            printf ' %s' "${t}"
        done
        printf '\n'
    } > "${cmdline_file}.tmp"
    mv "${cmdline_file}.tmp" "${cmdline_file}"
    chmod 0644 "${cmdline_file}"
    local added="${to_add[*]}"
    [[ -n "${added}" ]] || added="none"
    local reason
    if (( removed_disable == 1 )); then
        echo "  cgroup_memory: cmdline.txt updated; removed: cgroup_disable=memory; added: ${added}"
        reason="cmdline.txt updated; removed: cgroup_disable=memory; added: ${added}"
    else
        echo "  cgroup_memory: cmdline.txt updated; added: ${added}"
        reason="cmdline.txt updated; added: ${added}"
    fi
    logger -t jasper-install -- "event=cgroup_memory.cmdline_updated removed_disable=${removed_disable} added=${added}" 2>/dev/null || true
    _set_reboot_required_reason cgroup_memory "${reason} — kernel cmdline only re-reads at boot"
    _print_reboot_required_marker
    return 0
}
