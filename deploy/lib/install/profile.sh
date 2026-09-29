#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Install profile resolution and the hardware-tier preflight for deploy/install.sh.

normalize_install_profile() {
    # Mirror of jasper.playback_state.install_profile.normalize_install_profile.
    case "${1:-}" in
        ""|full)
            printf 'full\n'
            ;;
        streambox)
            printf 'streambox\n'
            ;;
        *)
            echo "invalid JASPER_INSTALL_PROFILE=${1:-<empty>}; use full or streambox" >&2
            return 2
            ;;
    esac
}

read_persisted_install_profile() {
    local marker="${1:-${INSTALL_PROFILE_MARKER}}"
    if [[ ! -f "${marker}" ]]; then
        return 0
    fi
    local raw
    raw="$(head -n1 "${marker}" | tr -d '[:space:]')"
    [[ -n "${raw}" ]] || return 0
    normalize_install_profile "${raw}"
}

detect_default_install_profile() {
    local model_file="${JASPER_PI_MODEL_FILE:-/proc/device-tree/model}"
    local model=""
    if [[ -r "${model_file}" ]]; then
        model="$(tr -d '\000' < "${model_file}" | tr -d '\r\n')"
    fi
    case "${model}" in
        *"Raspberry Pi Zero 2 W"*|*"Raspberry Pi Zero 2"*)
            printf 'streambox\n'
            ;;
        *)
            printf '%s\n' "${INSTALL_PROFILE_DEFAULT}"
            ;;
    esac
}

# See docs/adr/0315-hardware-tier-and-direct-updates.md.
detect_hardware_tier() {
    local meminfo="${JASPER_HW_MEMINFO_FILE:-/proc/meminfo}"
    local mem_kb
    mem_kb="$(awk '/^MemTotal:/ { print $2; exit }' "${meminfo}" 2>/dev/null || true)"
    case "${mem_kb}" in
        ""|*[!0-9]*) mem_kb=0 ;;
    esac
    # Declare then assign (not `local x="$(...)"`) so ShellCheck SC2155
    # doesn't fire and a subshell failure can't be masked.
    local cpus
    cpus="${JASPER_HW_NPROC:-$(nproc 2>/dev/null || echo 1)}"
    case "${cpus}" in
        ""|*[!0-9]*) cpus=1 ;;
    esac
    local arch
    arch="${JASPER_HW_ARCH:-$(uname -m 2>/dev/null || echo unknown)}"
    [[ -n "${arch}" ]] || arch="unknown"

    # The low boundary REUSES build-sandbox.sh's threshold (one source of
    # truth) so the label can't drift from the build knob it describes —
    # below it, the Rust low-memory build profile is already active.
    local low_kb="${RUST_LOW_MEMORY_BUILD_THRESHOLD_KB}"
    local tier
    if (( mem_kb == 0 )); then
        tier="unknown"
    elif (( mem_kb < low_kb )); then
        tier="low"
    elif (( mem_kb < 2097152 )); then
        tier="constrained"
    else
        tier="standard"
    fi
    printf 'ram_mb=%d cpus=%s arch=%s tier=%s\n' "$(( mem_kb / 1024 ))" "${cpus}" "${arch}" "${tier}"
}

# True when the detected/injected arch is a 64-bit ARM target JTS ships
# prebuilt binaries for (CamillaDSP aarch64, librespot arm64 .deb,
# CamillaGUI aarch64). 32-bit Pi OS (armv7l/armhf) — an easy Imager
# mis-pick on a Zero 2 W, which is arm64-capable but often imaged 32-bit
# — has no prebuilt path and fails deep in a fetch today.
_hardware_tier_arch_supported() {
    local arch
    arch="${JASPER_HW_ARCH:-$(uname -m 2>/dev/null || echo unknown)}"
    case "${arch}" in
        aarch64|arm64) return 0 ;;
        *) return 1 ;;
    esac
}

# Real-install preflight: log the detected tier (so the deploy transcript
# names it — closes the "failure wasn't self-evident" gap when a later
# build OOMs) and fail fast on an unsupported architecture before any
# mutation. A read-only preflight, like require_root; runs after the
# --dry-run early return so it never trips on x86 CI dry-runs.
hardware_tier_preflight() {
    local tier_line
    tier_line="$(detect_hardware_tier)"
    echo "  hardware tier: ${tier_line}"
    jasper_install_log "event=hardware_tier.detected ${tier_line}"

    if _hardware_tier_arch_supported; then
        return 0
    fi
    local arch
    arch="${JASPER_HW_ARCH:-$(uname -m 2>/dev/null || echo unknown)}"
    if _is_truthy "${JASPER_ALLOW_UNSUPPORTED_ARCH:-0}"; then
        echo "  WARN: unsupported architecture '${arch}'; JASPER_ALLOW_UNSUPPORTED_ARCH=1 set —" >&2
        echo "  proceeding, but the prebuilt CamillaDSP/librespot/CamillaGUI fetches will likely fail" >&2
        return 0
    fi
    cat >&2 <<EOF
ERROR: unsupported architecture '${arch}'.

JTS ships prebuilt 64-bit ARM binaries (CamillaDSP aarch64, librespot
arm64, CamillaGUI aarch64) and is supported only on 64-bit Raspberry Pi
OS (Trixie). Re-flash with the 64-bit image, or set
JASPER_ALLOW_UNSUPPORTED_ARCH=1 to attempt the install anyway (expect
the prebuilt fetches to fail).
EOF
    return 2
}

# Test helpers pass an alternate marker path directly; production calls use the
# canonical marker. Shellcheck only sees the production path.
# shellcheck disable=SC2120
resolve_install_profile() {
    local marker="${1:-${INSTALL_PROFILE_MARKER}}"
    local requested="${JASPER_INSTALL_PROFILE:-}"
    local persisted requested_profile

    persisted="$(read_persisted_install_profile "${marker}")" || return $?
    if [[ -n "${requested}" ]]; then
        requested_profile="$(normalize_install_profile "${requested}")" || return $?
    elif [[ -n "${persisted}" ]]; then
        requested_profile="${persisted}"
    else
        requested_profile="$(detect_default_install_profile)" || return $?
    fi

    if [[ -n "${persisted}" && "${persisted}" != "${requested_profile}" ]]; then
        echo "event=install_profile.conversion_requested previous=${persisted} profile=${requested_profile} source=explicit" >&2
    fi

    printf '%s\n' "${requested_profile}"
}

persist_install_profile() {
    local profile="$1"
    local marker="${2:-${INSTALL_PROFILE_MARKER}}"
    profile="$(normalize_install_profile "${profile}")" || return $?
    # `install -d -m` re-chmods an EXISTING dir — the marker's parent is
    # STATE_DIR itself on the default marker path, so this briefly narrowed
    # an already-widened 0770 STATE_DIR to 0750 on every deploy, the same
    # trap ensure_state_dir closed (#3879). Only create, never re-chmod.
    local marker_dir
    marker_dir="$(dirname "${marker}")"
    [[ -d "${marker_dir}" ]] || install -d -m 0750 "${marker_dir}"
    local tmp="${marker}.tmp.$$"
    printf '%s\n' "${profile}" > "${tmp}"
    chmod 0644 "${tmp}"
    mv "${tmp}" "${marker}"
}
