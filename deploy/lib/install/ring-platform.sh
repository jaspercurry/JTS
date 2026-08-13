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
#      renderer-user resolvable) pcm.jts_ring_capture / pcm.jts_ring_playback /
#      pcm.jts_ring_active_playback definitions. Shipped from
#      deploy/alsa/conf.d/60-jts-ring.conf verbatim.
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
# The installer's record of WHICH ioplug it installed and what that plugin can
# parse. Mirrored as jasper.ring_assets.RING_IOPLUG_PROVENANCE (the Python
# reader); the two literals are pinned equal by
# tests/test_ring_ioplug_provenance.py.
JTS_RING_IOPLUG_PROVENANCE="${JTS_RING_IOPLUG_PROVENANCE:-/var/lib/jasper/ring-ioplug.provenance}"

# Compile + install the jts_ring ALSA ioplug from the on-Pi checkout.
#
# Degrade-to-warn contract (campaign risk #5): a build failure MUST NOT
# fail the install. In P1 the ring platform is inert and loopback remains
# the transport, so a missing/stale .so changes nothing operationally. This
# is deliberately the OPPOSITE of build_install_rust_daemon's required=1
# fatal path; after P9 (when the ioplug becomes load-bearing) this gate
# flips to fatal, tracked in the campaign done-criteria.
#
# Two build-failure shapes, and what the doctor sees for each:
#   - First-ever build fails (no prior .so): so_dest is absent, so the
#     doctor's `ring platform` check goes `warn` (asset missing). Honest.
#   - Rebuild fails on a box with a prior good deploy: the pre-build rm -f
#     below only cleans the CACHE copy, so the PREVIOUS .so stays installed
#     at so_dest and still open-probes fine. What separates it from a fresh
#     build is the PROVENANCE record: every path that does not produce the
#     installed plugin revokes it, so the doctor reports an unvouched ioplug
#     rather than `ok`, and the coupling reconciler's capability gate fails
#     closed for a wire needing a conf.d field the plugin may not parse (the
#     2026-07-02 stale-binary class). The record states which .so and which
#     conf.d fields — it is not a protocol-version handshake with the Rust
#     ring, which stays P2's job.
build_install_jts_ring_ioplug() {
    local src_dir="${REPO_DIR}/${JTS_RING_IOPLUG_SRC_SUBDIR}"
    local cache_dir="/var/cache/jts-ring-ioplug-build"
    local so_dest="${JTS_RING_ALSA_PLUGIN_DIR}/${JTS_RING_IOPLUG_SO}"

    if declare -F prepare_first_party_runtime_bundle >/dev/null; then
        prepare_first_party_runtime_bundle || return 1
        if first_party_runtime_artifact_installed "jts-ring-ioplug"; then
            echo "  jts_ring ioplug: using verified first-party ARM64 runtime bundle"
            # The bundle path installs a plugin this deploy DID produce, so it
            # gets a record like the source build does — capabilities read off
            # the artifact, which is what makes the probe indifferent to how the
            # binary arrived.
            if [[ -f "${so_dest}" ]]; then
                record_ring_ioplug_provenance \
                    "${so_dest}" "$(sha256sum "${so_dest}" | awk '{print $1}')"
            else
                revoke_ring_ioplug_provenance
            fi
            return 0
        fi
    fi

    if [[ ! -d "${src_dir}" ]]; then
        # A branch predating the ioplug source. Non-fatal: the ring
        # platform simply is not available; doctor warns. Any .so still at
        # so_dest is from an earlier deploy and this one cannot vouch for it.
        echo "  jts_ring ioplug source missing at ${src_dir}; skipping ring platform build (ring stays unavailable)"
        revoke_ring_ioplug_provenance
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
        # STALE-BINARY HAZARD (the 2026-07-02 class): the pre-build rm -f above
        # only cleans the CACHE copy, so on a box with a prior good deploy the
        # PREVIOUS .so is still installed at so_dest. We do NOT remove the stale
        # .so: an installed-but-stale ioplug is strictly less broken than none
        # while loopback carries audio either way.
        #
        # What we DO remove is the installer's VOUCH for it. The provenance
        # record is this function's claim about what it installed; a failed
        # build means the claim can no longer be made, so it is revoked rather
        # than left describing a binary this deploy did not produce. That is
        # what turns a stale ioplug from invisible into loud: the doctor reports
        # an unvouched plugin instead of `ok`, and the coupling reconciler's
        # capability gate fails closed for any wire needing a conf.d field the
        # ioplug might not parse.
        revoke_ring_ioplug_provenance
        if [[ -e "${so_dest}" ]]; then
            echo "  WARN: a previously-installed ${so_dest} REMAINS in place and this deploy could not rebuild it; its provenance record has been revoked so the doctor reports an unvouched ioplug — treat the two WARN lines above as the real signal for this deploy" >&2
        fi
        return 0
    fi

    local built_so="${cache_dir}/${JTS_RING_IOPLUG_SO}"
    if [[ ! -f "${built_so}" ]]; then
        echo "  WARN: make plugin finished but ${built_so} is missing; ring platform unavailable" >&2
        revoke_ring_ioplug_provenance
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
    record_ring_ioplug_provenance "${so_dest}" "${new_sha}"
}

# Probe an ioplug .so for the conf.d fields it can PARSE.
#
# Each capability is proven by a diagnostic string literal the parse site emits,
# which the compiler embeds in the binary — so this interrogates the ARTIFACT,
# not the source tree it was supposedly built from. A pre-ring-v2 .so carries
# neither literal and reports no capabilities, which is the honest answer: it
# refuses both fields with "jts_ring: unknown field %s" at open().
#
# The literals are pinned against c/jts-ring-ioplug/pcm_jts_ring.c by
# tests/test_ring_ioplug_provenance.py, so a reworded SNDERR cannot silently
# turn a capable plugin into an uncapable-looking one.
_jts_ring_ioplug_caps() {
    local so="$1"
    local caps=()
    if LC_ALL=C grep -aqF 'format %s unsupported (S16_LE|S32_LE)' "${so}"; then
        caps+=("wire_format")
    fi
    if LC_ALL=C grep -aqF 'channels out of range 2..=8' "${so}"; then
        caps+=("wire_channels")
    fi
    local IFS=,
    # `${caps[*]-}`, not `${caps[*]}`: under `set -u` bash 3.2 (the macOS system
    # bash the test lane runs on) treats an EMPTY array expansion as an unbound
    # variable and aborts. An uncapable .so — the whole point of this probe — is
    # exactly the empty case, so the guard is on the load-bearing path, not a
    # corner. bash 5 on the Pi is happy either way; this keeps one script that
    # runs on both.
    printf '%s' "${caps[*]-}"
}

# Record what this install just put at ${so_dest}: the sha256 of the installed
# file plus the conf.d fields it can parse.
#
# WHY A RECORD AND NOT A PROBE. The coupling reconciler must know, before it
# arms, whether the installed ioplug can open the conf.d the resolved wire
# renders — a conf.d carrying a field the plugin does not know is refused at
# open() with -EINVAL and CamillaDSP cannot start against the ring. It cannot
# find that out by opening the PCM: on an armed box the ioplug's SPSC guard
# EBUSYs the probe, and probing a live ring is the disturbance the doctor's
# armed-skip already exists to avoid. So the installer states what it installed
# and the reconciler compares records.
#
# The sha is what binds the claim to a specific binary: the ioplug build is
# degrade-to-warn, so a later failed deploy can leave a DIFFERENT .so at this
# path, and a record that could not name which file it described would vouch for
# whatever happened to be there.
#
# Reader: jasper.ring_assets.read_ring_ioplug_provenance (the key names and the
# path are mirrored there and pinned by tests/test_ring_ioplug_provenance.py).
record_ring_ioplug_provenance() {
    local so_dest="$1" sha="$2"
    local caps
    caps="$(_jts_ring_ioplug_caps "${so_dest}")"
    install -d -m 0755 "$(dirname "${JTS_RING_IOPLUG_PROVENANCE}")"
    local tmp="${JTS_RING_IOPLUG_PROVENANCE}.tmp.$$"
    {
        echo "# Written by deploy/lib/install/ring-platform.sh. Do not hand-edit:"
        echo "# this file is the INSTALLER's statement about the ioplug it built,"
        echo "# and jasper.ring_assets refuses to arm a wide ring wire whose"
        echo "# capability it cannot find here. Regenerate by redeploying."
        echo "JTS_RING_IOPLUG_SHA256=${sha}"
        echo "JTS_RING_IOPLUG_CAPS=${caps}"
    } >"${tmp}"
    chmod 0644 "${tmp}"
    mv -f "${tmp}" "${JTS_RING_IOPLUG_PROVENANCE}"
    echo "  -> recorded ioplug provenance (caps=[${caps:-none}]) at ${JTS_RING_IOPLUG_PROVENANCE}"
}

# Withdraw the installer's vouch for whatever .so is installed.
#
# Called on every path where this deploy did NOT produce the installed plugin.
# Absence of a record is fail-closed for the capability gate and reads as an
# unvouched ioplug in the doctor, which is the correct verdict: the last thing
# that could describe the binary on disk just failed.
revoke_ring_ioplug_provenance() {
    if [[ -e "${JTS_RING_IOPLUG_PROVENANCE}" ]]; then
        rm -f "${JTS_RING_IOPLUG_PROVENANCE}"
        echo "  WARN: revoked ${JTS_RING_IOPLUG_PROVENANCE} — this deploy cannot vouch for the installed ioplug" >&2
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
        echo "  Installed /etc/alsa/conf.d/60-jts-ring.conf (pcm.jts_ring_capture + pcm.jts_ring_playback + pcm.jts_ring_active_playback; inert)"
    else
        echo "  WARN: ${conf_src} missing; jts_ring PCM definitions not installed" >&2
    fi

    # 1b. Renderer-ingress lane PCMs (U3 / P6). Same shape and same reason as
    #     the block above: system-wide 0644 so the non-root renderer users can
    #     resolve the names. INERT until a lane is armed — a PCM definition is
    #     not an open, and no renderer points at one of these until
    #     `jasper-audio-config renderer-lanes --arm` has written the matching
    #     device override.
    local lanes_src="${REPO_DIR}/deploy/alsa/conf.d/61-jts-renderer-lanes.conf"
    if [[ -f "${lanes_src}" ]]; then
        install -d -m 0755 /etc/alsa/conf.d
        install -m 0644 "${lanes_src}" /etc/alsa/conf.d/61-jts-renderer-lanes.conf
        echo "  Installed /etc/alsa/conf.d/61-jts-renderer-lanes.conf (pcm.jts_ring_lane_spotify + pcm.librespot_ring_lane; inert)"
    else
        echo "  WARN: ${lanes_src} missing; renderer-ingress lane PCMs not installed" >&2
    fi

    # 2. /dev/shm/jts-ring directory via tmpfiles.d. Group-writable +
    #    setgid so the (root today) ring writer + reader share it, and a
    #    non-root RENDERER in group `jts-ring` can create and write its lane's
    #    ring there (U3 / P6). Applied immediately so a deploy
    #    does not have to wait for a reboot; the tmpfiles entry also
    #    recreates it on every boot (tmpfs is volatile). SHM files created
    #    under it survive an outputd restart because the DIRECTORY persists
    #    (the guardrail from the ring train).
    local tmpfiles_src="${REPO_DIR}/deploy/tmpfiles/jts-ring.conf"
    if [[ -f "${tmpfiles_src}" ]]; then
        install -m 0644 "${tmpfiles_src}" /etc/tmpfiles.d/jts-ring.conf
        # --create is idempotent; a failure here (e.g. group `jts-ring` not
        # yet present on a partial box) must not fail the install — the
        # dir self-heals on the next boot/apply. Capture stderr into the WARN
        # so a PERSISTENT failure (not the transient partial-box case) carries
        # its reason instead of a bare "deferred to next boot" with no cause.
        # `if ! ...` keeps set -e from aborting on the non-zero exit.
        local tmpfiles_err
        if ! tmpfiles_err="$(systemd-tmpfiles --create --prefix=/dev/shm/jts-ring 2>&1)"; then
            # Single-line the reason so the WARN stays one line.
            tmpfiles_err="${tmpfiles_err//$'\n'/ }"
            echo "  WARN: systemd-tmpfiles --create for /dev/shm/jts-ring deferred to next boot${tmpfiles_err:+: ${tmpfiles_err}}" >&2
        fi
        echo "  Installed /etc/tmpfiles.d/jts-ring.conf and applied /dev/shm/jts-ring (inert)"
    else
        echo "  WARN: ${tmpfiles_src} missing; /dev/shm/jts-ring lifecycle not installed" >&2
    fi
}

# One entry point for install.sh main() — build + place all three assets.
install_jts_ring_platform() {
    build_install_jts_ring_ioplug
    install_jts_ring_conf_assets
    # Program/content ring files are tmpfs transport state, never user data.
    # Delete both explicit files before install_systemd_units can restart
    # fan-in/outputd: an existing 8-slot mmap from an older default would be a
    # fatal attach mismatch after the new 2-slot default lands. CamillaDSP keeps
    # reading its old unlinked fd until the later DSP reconcile reloads it onto
    # the freshly-created ring, which is inside the normal deploy audio bounce.
    rm -f /dev/shm/jts-ring/program.ring
    rm -f /dev/shm/jts-ring/content.ring
}
