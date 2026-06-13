#!/usr/bin/env bash
# Rust daemon build/install helpers for deploy/install.sh.
#
# Functions assume install.sh globals (REPO_DIR, BUILD_USER) and
# set -euo pipefail from the sourcing shell.

FANIN_BIN="/opt/jasper/bin/jasper-fanin"
OUTPUTD_BIN="/opt/jasper/bin/jasper-outputd"
OUTPUTD_SOURCE_MISSING_ERROR="ERROR: jasper-outputd source missing"

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
    chown -R "${BUILD_USER}:${BUILD_USER}" "${cache_dir}"

    sudo -u "${BUILD_USER}" -H bash -c "cd '${cache_dir}' && cargo build --release --locked --quiet" \
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
