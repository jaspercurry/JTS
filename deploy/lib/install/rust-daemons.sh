#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Rust daemon build/install helpers for deploy/install.sh.
#
# Functions assume install.sh globals (REPO_DIR, BUILD_USER) and
# set -euo pipefail from the sourcing shell.

FANIN_BIN="/opt/jasper/bin/jasper-fanin"
OUTPUTD_BIN="/opt/jasper/bin/jasper-outputd"
USBSINK_AUDIO_BIN="/opt/jasper/bin/jasper-usbsink-audio"
OUTPUTD_SOURCE_MISSING_ERROR="ERROR: jasper-outputd source missing"
USBSINK_AUDIO_SOURCE_MISSING_ERROR="ERROR: jasper-usbsink-audio source missing"
RUST_LOW_MEMORY_BUILD_THRESHOLD_KB=1200000

# Space-separated set of daemon names whose installed binary content
# changed during this install run (sha256 compared before/after the
# `install` below). Consumed by restart_services_for_changed_rust_daemons
# from the systemd step; a plain string (not an array) so it stays safe
# under `set -u` on old bash.
JASPER_RUST_CHANGED_BINS=""

rust_build_memtotal_kb() {
    local meminfo="${JASPER_RUST_MEMINFO_FILE:-/proc/meminfo}"
    awk '/^MemTotal:/ { print $2; exit }' "${meminfo}" 2>/dev/null || true
}

rust_low_memory_build_enabled() {
    local setting="${JASPER_RUST_LOW_MEMORY_BUILD:-auto}"
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
            echo "  WARN: invalid JASPER_RUST_LOW_MEMORY_BUILD=${setting}; using auto" >&2
            ;;
    esac

    local mem_kb
    mem_kb="$(rust_build_memtotal_kb)"
    case "${mem_kb}" in
        ""|*[!0-9]*)
            return 1
            ;;
    esac
    [[ "${mem_kb}" -lt "${RUST_LOW_MEMORY_BUILD_THRESHOLD_KB}" ]]
}

rust_cargo_build_env() {
    if ! rust_low_memory_build_enabled; then
        return 0
    fi

    # 1 GB and Zero-class boxes have enough CPU for the runtime daemons, but not
    # enough RAM to reliably compile the normal release profile with fat LTO or
    # LLVM's heavier optimization passes. Keep 2 GB+ full speakers on the
    # normal release profile and relax Cargo only on constrained hosts.
    printf '%s\n' \
        "CARGO_BUILD_JOBS=1" \
        "CARGO_PROFILE_RELEASE_LTO=false" \
        "CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16" \
        "CARGO_PROFILE_RELEASE_OPT_LEVEL=0"
}

build_install_rust_daemon() {
    local name="$1"
    local required="$2"
    local src_dir="${REPO_DIR}/rust/${name}"
    local cache_dir="/var/cache/${name}-build"
    local bin_dest="/opt/jasper/bin/${name}"
    local missing_source_message="${name} source missing"
    local required_reason="This tree requires ${name} as part of the audio runtime."

    if [[ "${name}" == "jasper-fanin" ]]; then
        bin_dest="${FANIN_BIN}"
    elif [[ "${name}" == "jasper-outputd" ]]; then
        bin_dest="${OUTPUTD_BIN}"
        missing_source_message="${OUTPUTD_SOURCE_MISSING_ERROR}"
        required_reason="This tree requires jasper-outputd as the final output owner."
    elif [[ "${name}" == "jasper-usbsink-audio" ]]; then
        bin_dest="${USBSINK_AUDIO_BIN}"
        missing_source_message="${USBSINK_AUDIO_SOURCE_MISSING_ERROR}"
        required_reason="This tree requires jasper-usbsink-audio for the production USB low-latency route."
    fi

    if [[ ! -d "${src_dir}" ]]; then
        if [[ "${required}" == "1" ]]; then
            echo "  ${missing_source_message} at ${src_dir}" >&2
            echo "  ${required_reason}" >&2
            return 1
        fi
        echo "  ${missing_source_message} at ${src_dir}; skipping build"
        return 0
    fi

    echo "  building ${name} (Rust daemon)..."
    mkdir -p "${cache_dir}"
    chown "${BUILD_USER}:${BUILD_USER}" "${cache_dir}"

    # rsync the source tree into the cache dir, preserving cargo's
    # incremental compile state in target/ between runs. --delete
    # removes stale source files (e.g., a renamed module).
    rsync -a --delete \
        --exclude='target/' \
        "${src_dir}/" "${cache_dir}/"
    # Stage the shared wire-protocol crate as a sibling of the cache dir
    # so `path = "../jasper-tts-protocol"` resolves like the repo layout.
    rsync -a --delete \
        --exclude='target/' \
        "${REPO_DIR}/rust/jasper-tts-protocol/" \
        "$(dirname "${cache_dir}")/jasper-tts-protocol/"
    chown -R "${BUILD_USER}:${BUILD_USER}" "$(dirname "${cache_dir}")/jasper-tts-protocol"
    # Same for the shared clock crate (jasper-clock) so jasper-outputd's
    # `path = "../jasper-clock"` resolves. Guarded by existence so a branch
    # predating the crate still builds (its daemons don't depend on it).
    if [[ -d "${REPO_DIR}/rust/jasper-clock" ]]; then
        rsync -a --delete \
            --exclude='target/' \
            "${REPO_DIR}/rust/jasper-clock/" \
            "$(dirname "${cache_dir}")/jasper-clock/"
        chown -R "${BUILD_USER}:${BUILD_USER}" "$(dirname "${cache_dir}")/jasper-clock"
    fi
    # Same for the shared resampler crate (jasper-resampler) so the
    # `path = "../jasper-resampler"` dep of jasper-outputd (content_bridge) AND
    # jasper-fanin (the DEFAULT-OFF per-input lane resampler) resolves.
    # jasper-resampler itself depends on `path = "../jasper-clock"`, which the
    # block above already stages as a sibling — so this single rsync covers the
    # transitive dep. Guarded by existence so a branch predating the crate still
    # builds.
    if [[ -d "${REPO_DIR}/rust/jasper-resampler" ]]; then
        rsync -a --delete \
            --exclude='target/' \
            "${REPO_DIR}/rust/jasper-resampler/" \
            "$(dirname "${cache_dir}")/jasper-resampler/"
        chown -R "${BUILD_USER}:${BUILD_USER}" "$(dirname "${cache_dir}")/jasper-resampler"
    fi
    # Same for the shared SHM ring crate (jasper-ring) so jasper-fanin's
    # `path = "../jasper-ring"` dep (the default-off SHM ring writer) resolves.
    # Guarded by existence so a branch predating the crate still builds.
    if [[ -d "${REPO_DIR}/rust/jasper-ring" ]]; then
        rsync -a --delete \
            --exclude='target/' \
            "${REPO_DIR}/rust/jasper-ring/" \
            "$(dirname "${cache_dir}")/jasper-ring/"
        chown -R "${BUILD_USER}:${BUILD_USER}" "$(dirname "${cache_dir}")/jasper-ring"
    fi
    # Same for the shared host-clock crate (jasper-host-clock) so the
    # `path = "../jasper-host-clock"` deps of jasper-usbsink-audio and
    # jasper-fanin (the Capture Pitch DLL extracted from the bridge)
    # resolve. Guarded by existence so a branch predating the crate still
    # builds.
    if [[ -d "${REPO_DIR}/rust/jasper-host-clock" ]]; then
        rsync -a --delete \
            --exclude='target/' \
            "${REPO_DIR}/rust/jasper-host-clock/" \
            "$(dirname "${cache_dir}")/jasper-host-clock/"
        chown -R "${BUILD_USER}:${BUILD_USER}" "$(dirname "${cache_dir}")/jasper-host-clock"
    fi
    chown -R "${BUILD_USER}:${BUILD_USER}" "${cache_dir}"

    local -a cargo_env=()
    local cargo_arg
    while IFS= read -r cargo_arg; do
        cargo_env+=("${cargo_arg}")
    done < <(rust_cargo_build_env)
    if [[ "${#cargo_env[@]}" -gt 0 ]]; then
        echo "  ${name}: low-memory Rust build profile active ($(rust_build_memtotal_kb) kB RAM; opt-level=0, lto=false, codegen-units=16, jobs=1)"
    fi

    # Contain the sudo -> pi -> cargo -> rustc subtree: cargo manages its
    # own -j (CARGO_BUILD_JOBS via the low-memory profile above), but the
    # LTO link step can still spike RAM; the scope makes an OOM kill this
    # build, never a live daemon. cargo_env stays inside the command so
    # the user-drop + profile env are unaffected by the scope.
    run_contained_build "${name}" -- \
        sudo -u "${BUILD_USER}" -H env "${cargo_env[@]}" bash -c "cd '${cache_dir}' && cargo build --release --locked --quiet" \
        || { echo "  ${name} build failed; see cargo output above"; return 1; }

    local built_bin="${cache_dir}/target/release/${name}"
    if [[ ! -x "${built_bin}" ]]; then
        echo "  ERROR: cargo build finished but ${built_bin} is missing" >&2
        return 1
    fi

    # Record whether the installed binary content actually changed. A
    # running service keeps executing the OLD inode after `install`
    # replaces the file on disk, so a content change must drive a
    # conditional service restart in the systemd step (the 2026-07-02
    # stale jasper-usbsink-audio incident: new HTTP endpoints 404'd until
    # a manual restart). Capture the pre-install hash before `install`.
    local pre_sha="" new_sha
    if [[ -e "${bin_dest}" ]]; then
        pre_sha="$(sha256sum "${bin_dest}" | awk '{print $1}')"
    fi
    mkdir -p /opt/jasper/bin
    install -m 0755 -o root -g root "${built_bin}" "${bin_dest}"
    new_sha="$(sha256sum "${bin_dest}" | awk '{print $1}')"
    if [[ "${new_sha}" != "${pre_sha}" ]]; then
        JASPER_RUST_CHANGED_BINS="${JASPER_RUST_CHANGED_BINS} ${name}"
        echo "  -> installed ${bin_dest} ($(du -h "${bin_dest}" | cut -f1)) — binary content changed"
    else
        echo "  -> installed ${bin_dest} ($(du -h "${bin_dest}" | cut -f1)) — binary content unchanged"
    fi
}

build_install_jasper_fanin() {
    # Fan-in is the production renderer topology; older experimental
    # branches may not carry rust/jasper-fanin, so absence remains a
    # non-fatal skip for compatibility with that historical shape.
    build_install_rust_daemon "jasper-fanin" "0"
}

build_install_jasper_outputd() {
    # outputd is the mainline final-output owner and is required.
    build_install_rust_daemon "jasper-outputd" "1"
}

build_install_jasper_usbsink_audio() {
    # The production USB low-latency route must not depend on Python
    # callbacks in the audio data plane.
    build_install_rust_daemon "jasper-usbsink-audio" "1"
}

rust_daemon_binary_changed() {
    # Membership test against the space-separated changed set. Padded
    # spaces make names exact tokens ("jasper-usbsink" must not match
    # "jasper-usbsink-audio").
    case " ${JASPER_RUST_CHANGED_BINS} " in
        *" $1 "*) return 0 ;;
    esac
    return 1
}

restart_services_for_changed_rust_daemons() {
    # A freshly installed Rust binary goes live only when its owning
    # service restarts — `install` replaces the file on disk, but the
    # running process keeps executing the old inode (observed 2026-07-02:
    # jasper-usbsink served a stale jasper-usbsink-audio build and its new
    # HTTP endpoints 404'd until a manual restart).
    #
    # jasper-fanin and jasper-outputd are ALWAYS restarted by the
    # core-graph sequence earlier in the same systemd step
    # (JASPER_CORE_GRAPH_RESTART_TARGETS + the park list +
    # require_outputd_ready), binary change or not — bouncing them again
    # here would interrupt audio twice per deploy. This step owns only the
    # Rust daemons OUTSIDE that set; today that is
    # jasper-usbsink-audio -> jasper-usbsink.service.
    # tests/test_install_rust_daemon_restart.py pins that every built Rust
    # binary's owning service is covered by one of the two mechanisms.
    if [[ "${SKIP_RESTART:-0}" == "1" ]]; then
        echo "  SKIP_RESTART=1 — leaving services on their prior Rust binaries"
        return 0
    fi
    if rust_daemon_binary_changed "jasper-usbsink-audio"; then
        # try-restart: bounce only if currently running. USB-in is an
        # opt-in source toggled by /sources/ — never START it from a
        # deploy. Blast radius when it fires: ~1-2 s gap on USB-in audio.
        echo "  jasper-usbsink-audio changed — try-restarting jasper-usbsink.service"
        systemctl try-restart jasper-usbsink.service 2>/dev/null || \
            echo "  WARN: jasper-usbsink.service try-restart failed; USB-in may keep running the prior binary (systemctl restart jasper-usbsink to recover)"
    fi
}
