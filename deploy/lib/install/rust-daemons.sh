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
OUTPUTD_SOURCE_MISSING_ERROR="ERROR: jasper-outputd source missing"
RUST_LOW_MEMORY_BUILD_THRESHOLD_KB=1200000

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

# Build-cache staging format. Bump when the staging/freshness contract
# changes in a way that requires discarding cargo's incremental state
# once (rust_build_cache_reset_if_stale_format below clears target/ on
# mismatch). Format 1 = the 2026-07-10 mtime-trap fix: caches populated
# by mtime-preserving rsync can hold fingerprints NEWER than current
# source mtimes, which cargo reads as "Fresh" forever — the purge is the
# only way an already-poisoned cache ever recompiles.
RUST_BUILD_CACHE_FORMAT=1

# Stage a crate's source into a build/staging dir without poisoning
# cargo's freshness check. Cargo rebuilds a unit only when a source file
# is NEWER than the fingerprint stamped at the last compile, and rsync's
# -a preserves mtimes end to end (laptop -> checkout -> cache). A changed
# file whose checkout mtime predates the cache's last build therefore
# lands "in the past", cargo declares the crate Fresh, and install.sh
# ships the stale binary while reporting success (the 2026-07-02
# jasper-usbsink-audio and 2026-07-10 jasper-outputd incidents — proven
# live: cache source carried a fix, `cargo build -v` said Fresh in
# 0.03s). So: compare by content (--checksum) and do NOT preserve times
# (-rlpgoD is -a minus -t) — an unchanged file is skipped and keeps its
# old mtime (no spurious rebuild), a changed file is written with the
# current time and is always newer than the last fingerprint.
stage_rust_crate() {
    local from="$1"
    local to="$2"
    rsync -rlpgoD --checksum --delete \
        --exclude='target/' \
        --exclude='/.jts-build-cache-format' \
        "${from}/" "${to}/"
}

# One-time incremental-state reset when the staging contract changes.
# stage_rust_crate keeps future syncs honest, but a cache whose
# fingerprints already postdate its (correct) source mtimes stays
# false-Fresh forever — only dropping target/ forces the recompile.
rust_build_cache_reset_if_stale_format() {
    local cache_dir="$1"
    local name="$2"
    local marker="${cache_dir}/.jts-build-cache-format"
    local have=""
    if [[ -f "${marker}" ]]; then
        have="$(<"${marker}")"
    fi
    if [[ "${have}" != "${RUST_BUILD_CACHE_FORMAT}" ]]; then
        echo "  ${name}: build-cache format '${have:-none}' != '${RUST_BUILD_CACHE_FORMAT}'; clearing ${cache_dir}/target (one-time full rebuild)"
        rm -rf "${cache_dir}/target"
        printf '%s\n' "${RUST_BUILD_CACHE_FORMAT}" >"${marker}"
    fi
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
    rust_build_cache_reset_if_stale_format "${cache_dir}" "${name}"

    # Stage the source tree into the cache dir, keeping cargo's
    # incremental compile state in target/ between runs. --delete
    # removes stale source files (e.g., a renamed module).
    stage_rust_crate "${src_dir}" "${cache_dir}"
    # Stage the shared wire-protocol crate as a sibling of the cache dir
    # so `path = "../jasper-tts-protocol"` resolves like the repo layout.
    stage_rust_crate "${REPO_DIR}/rust/jasper-tts-protocol" \
        "$(dirname "${cache_dir}")/jasper-tts-protocol"
    chown -R "${BUILD_USER}:${BUILD_USER}" "$(dirname "${cache_dir}")/jasper-tts-protocol"
    # Same for the shared clock crate (jasper-clock) so jasper-outputd's
    # `path = "../jasper-clock"` resolves. Guarded by existence so a branch
    # predating the crate still builds (its daemons don't depend on it).
    if [[ -d "${REPO_DIR}/rust/jasper-clock" ]]; then
        stage_rust_crate "${REPO_DIR}/rust/jasper-clock" \
            "$(dirname "${cache_dir}")/jasper-clock"
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
        stage_rust_crate "${REPO_DIR}/rust/jasper-resampler" \
            "$(dirname "${cache_dir}")/jasper-resampler"
        chown -R "${BUILD_USER}:${BUILD_USER}" "$(dirname "${cache_dir}")/jasper-resampler"
    fi
    # Same for the shared SHM ring crate (jasper-ring) so jasper-fanin's
    # `path = "../jasper-ring"` dep (the default-off SHM ring writer) resolves.
    # Guarded by existence so a branch predating the crate still builds.
    if [[ -d "${REPO_DIR}/rust/jasper-ring" ]]; then
        stage_rust_crate "${REPO_DIR}/rust/jasper-ring" \
            "$(dirname "${cache_dir}")/jasper-ring"
        chown -R "${BUILD_USER}:${BUILD_USER}" "$(dirname "${cache_dir}")/jasper-ring"
    fi
    # Same for the shared host-clock crate (jasper-host-clock) so jasper-fanin's
    # Capture Pitch DLL dependency resolves. Guarded by existence so a branch
    # predating the crate still builds.
    if [[ -d "${REPO_DIR}/rust/jasper-host-clock" ]]; then
        stage_rust_crate "${REPO_DIR}/rust/jasper-host-clock" \
            "$(dirname "${cache_dir}")/jasper-host-clock"
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

    mkdir -p /opt/jasper/bin
    install -m 0755 -o root -g root "${built_bin}" "${bin_dest}"
    echo "  -> installed ${bin_dest} ($(du -h "${bin_dest}" | cut -f1))"
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

retire_jasper_usbsink_audio() {
    # USB ingress is owned entirely by jasper-fanin. Remove the retired helper
    # image and its persistent Cargo cache on upgrade so deletion actually
    # reclaims disk instead of leaving an executable that looks supported.
    rm -f -- /opt/jasper/bin/jasper-usbsink-audio
    rm -rf -- /var/cache/jasper-usbsink-audio-build
}
