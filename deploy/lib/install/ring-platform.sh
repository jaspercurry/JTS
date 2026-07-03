#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# JTS Ring platform install helpers for deploy/install.sh.
#
# Ships the SHM slot-ring transport primitive as PRODUCT assets, but
# INERT: after this runs every box carries the `jts_ring` ALSA ioplug,
# its conf.d device definitions, and the /dev/shm/jts-ring directory,
# yet NOTHING opens them. The default coupling stays `loopback` and the
# default content bridge stays `direct`; audio behaviour is byte-identical
# to a box without these files. This is P1 of the audio-graph
# consolidation campaign — see
# docs/HANDOFF-audio-graph-consolidation.md (phase map row P1, section I).
#
# Three assets, all installed here:
#   1. libasound_module_pcm_jts_ring.so — compiled on the Pi from
#      c/jts-ring-ioplug via `make plugin`, installed to the arch ALSA
#      plugin dir. sha256-compared like the Rust daemons so an unchanged
#      build does not churn (no restart is triggered — nothing runs it
#      yet in P1).
#   2. /etc/alsa/conf.d/60-jts-ring.conf — the system-wide (0644,
#      renderer-user resolvable) pcm.jts_ring_capture / pcm.jts_ring_playback
#      definitions. Shipped from deploy/alsa/conf.d/60-jts-ring.conf verbatim.
#   3. /etc/tmpfiles.d/jts-ring.conf — the /dev/shm/jts-ring directory
#      lifecycle (group-writable, setgid), shipped from
#      deploy/tmpfiles/jts-ring.conf and applied via systemd-tmpfiles.
#
# Functions assume install.sh globals (REPO_DIR, BUILD_USER) and
# set -euo pipefail from the sourcing shell. run_contained_build is
# provided by deploy/lib/install/build-sandbox.sh (sourced before this).

# Build/install locations. ALSA_PLUGIN_DIR is the aarch64 Trixie
# multiarch plugin dir — the directory ALSA actually dlopen()s ioplugs
# from on the Pi (verified live on jts3.local / jts.local; bluealsa and
# jack already register plugins from here). Overridable for other arches.
JTS_RING_IOPLUG_SO="libasound_module_pcm_jts_ring.so"
JTS_RING_ALSA_PLUGIN_DIR="${JTS_RING_ALSA_PLUGIN_DIR:-/usr/lib/aarch64-linux-gnu/alsa-lib}"
JTS_RING_IOPLUG_SRC_SUBDIR="c/jts-ring-ioplug"

# Compile + install the jts_ring ALSA ioplug from the on-Pi checkout.
#
# Degrade-to-warn contract (campaign risk #5): a build failure MUST NOT
# fail the install. In P1 the ring platform is inert and loopback remains
# the transport, so an absent .so changes nothing operationally — the
# doctor `ring platform` check warns, and the next deploy retries. This
# is deliberately the OPPOSITE of build_install_rust_daemon's required=1
# fatal path; after P9 (when the ioplug becomes load-bearing) this gate
# flips to fatal, tracked in the campaign done-criteria.
build_install_jts_ring_ioplug() {
    local src_dir="${REPO_DIR}/${JTS_RING_IOPLUG_SRC_SUBDIR}"
    local cache_dir="/var/cache/jts-ring-ioplug-build"
    local so_dest="${JTS_RING_ALSA_PLUGIN_DIR}/${JTS_RING_IOPLUG_SO}"

    if [[ ! -d "${src_dir}" ]]; then
        # A branch predating the ioplug source. Non-fatal: the ring
        # platform simply is not available; doctor warns.
        echo "  jts_ring ioplug source missing at ${src_dir}; skipping ring platform build (ring stays unavailable)"
        return 0
    fi

    echo "  building jts_ring ALSA ioplug (C)..."
    mkdir -p "${cache_dir}"
    chown "${BUILD_USER}:${BUILD_USER}" "${cache_dir}"

    # rsync the C source into the cache dir (no target/ equivalent — the
    # Makefile builds in-tree — but --delete keeps a renamed/removed
    # source file from lingering, and a stale prior .so is cleaned below).
    rsync -a --delete "${src_dir}/" "${cache_dir}/"
    chown -R "${BUILD_USER}:${BUILD_USER}" "${cache_dir}"

    # Remove any stale .so from a prior build so a compile failure this
    # run can't leave a byte-identical old artifact that then sha-matches
    # and reads as "unchanged" (masking the failure).
    rm -f "${cache_dir}/${JTS_RING_IOPLUG_SO}"

    # `make plugin` links libasound and passes -DPIC (load-bearing: alsa
    # global.h keys SND_PCM_PLUGIN_SYMBOL on the PIC preprocessor macro,
    # not -fPIC — see the Makefile comment). gcc + libasound2-dev are
    # installed by install_deps / install_streambox_deps. Contained so an
    # OOM during the (small) cc kills this build, never a live daemon.
    if ! run_contained_build "jts-ring-ioplug" -- \
        sudo -u "${BUILD_USER}" -H bash -c "cd '${cache_dir}' && make plugin"; then
        echo "  WARN: jts_ring ioplug build failed; ring platform unavailable this deploy" >&2
        echo "  WARN: loopback coupling remains the transport (inert phase) — doctor 'ring platform' will warn" >&2
        return 0
    fi

    local built_so="${cache_dir}/${JTS_RING_IOPLUG_SO}"
    if [[ ! -f "${built_so}" ]]; then
        echo "  WARN: make plugin finished but ${built_so} is missing; ring platform unavailable" >&2
        return 0
    fi

    # sha256-compare so an unchanged .so is reported as such (honest install
    # log; matches the rust-daemons.sh idiom). In P1 nothing runs the plugin,
    # so a content change triggers no restart — the comparison is purely
    # informational until a consumer exists (P2+).
    local pre_sha="" new_sha
    if [[ -e "${so_dest}" ]]; then
        pre_sha="$(sha256sum "${so_dest}" | awk '{print $1}')"
    fi
    install -d -m 0755 "${JTS_RING_ALSA_PLUGIN_DIR}"
    install -m 0644 -o root -g root "${built_so}" "${so_dest}"
    new_sha="$(sha256sum "${so_dest}" | awk '{print $1}')"
    if [[ "${new_sha}" != "${pre_sha}" ]]; then
        echo "  -> installed ${so_dest} — content changed"
    else
        echo "  -> installed ${so_dest} — content unchanged"
    fi
}

# Install the product conf.d device definitions + the /dev/shm/jts-ring
# directory lifecycle. Both are pure static files (no compile), and both
# are INERT in P1: the PCMs resolve but nothing opens them, and the
# directory exists but no ring file is created until a coupling arms in
# P2+. Kept separate from the build so the conf/tmpfiles land even on a
# box where the C build failed (the doctor can then still report exactly
# which of the three assets is missing).
install_jts_ring_conf_assets() {
    # 1. conf.d device definitions (system-wide, 0644, renderer-user
    #    resolvable — the PR #214 class). install_alsa already created
    #    /etc/alsa/conf.d; recreate defensively so ordering is not fragile.
    local conf_src="${REPO_DIR}/deploy/alsa/conf.d/60-jts-ring.conf"
    if [[ -f "${conf_src}" ]]; then
        install -d -m 0755 /etc/alsa/conf.d
        install -m 0644 "${conf_src}" /etc/alsa/conf.d/60-jts-ring.conf
        echo "  Installed /etc/alsa/conf.d/60-jts-ring.conf (pcm.jts_ring_capture + pcm.jts_ring_playback; inert)"
    else
        echo "  WARN: ${conf_src} missing; jts_ring PCM definitions not installed" >&2
    fi

    # 2. /dev/shm/jts-ring directory via tmpfiles.d. Group-writable +
    #    setgid so the (root today) ring writer + reader share it, and a
    #    future non-root reader in group `jasper` inherits write access to
    #    the header files created there. Applied immediately so a deploy
    #    does not have to wait for a reboot; the tmpfiles entry also
    #    recreates it on every boot (tmpfs is volatile). SHM files created
    #    under it survive an outputd restart because the DIRECTORY persists
    #    (the guardrail from the ring train).
    local tmpfiles_src="${REPO_DIR}/deploy/tmpfiles/jts-ring.conf"
    if [[ -f "${tmpfiles_src}" ]]; then
        install -m 0644 "${tmpfiles_src}" /etc/tmpfiles.d/jts-ring.conf
        # --create is idempotent; a failure here (e.g. group `jasper` not
        # yet present on a partial box) must not fail the install — the
        # dir self-heals on the next boot/apply.
        systemd-tmpfiles --create --prefix=/dev/shm/jts-ring 2>/dev/null || \
            echo "  WARN: systemd-tmpfiles --create for /dev/shm/jts-ring deferred to next boot" >&2
        echo "  Installed /etc/tmpfiles.d/jts-ring.conf and applied /dev/shm/jts-ring (inert)"
    else
        echo "  WARN: ${tmpfiles_src} missing; /dev/shm/jts-ring lifecycle not installed" >&2
    fi
}

# One entry point for install.sh main() — build + place all three assets.
install_jts_ring_platform() {
    build_install_jts_ring_ioplug
    install_jts_ring_conf_assets
}
