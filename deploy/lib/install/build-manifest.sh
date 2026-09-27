#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# The build SHA and the verified-install build manifest for deploy/install.sh.

# Resolve the short build SHA for THIS install run, with the same
# precedence write_build_manifest uses: deploy env var (the normal
# laptop-driven path) → git in the rsynced checkout (Pi-local installs) →
# the prior manifest → "unknown". Factored out so the landing page's
# app.css cache-bust and the build manifest agree by construction even
# though the manifest is now written LAST (see write_build_manifest).
resolve_build_sha_short() {
    local sha="${JASPER_DEPLOY_SHA:-}"
    if [[ -z "${sha}" ]] && command -v git >/dev/null 2>&1 && \
       { [[ -d "${REPO_DIR}/.git" ]] || git -C "${REPO_DIR}" rev-parse --git-dir >/dev/null 2>&1; }; then
        sha=$(git -C "${REPO_DIR}" rev-parse --short HEAD 2>/dev/null || true)
    fi
    if [[ -z "${sha}" && -f "${STATE_DIR}/build.txt" ]]; then
        sha=$(grep -E '^JASPER_GIT_SHA=' "${STATE_DIR}/build.txt" 2>/dev/null | head -1 | cut -d= -f2-)
    fi
    printf '%s\n' "${sha:-unknown}"
}

write_build_manifest() {
    # Build manifest = the VERIFIED-INSTALL success marker, NOT a "we
    # started installing X" note. It is written ONCE, as the final
    # mutation in main(), so `set -euo pipefail` guarantees every
    # build/install/migration step above ran to completion before this
    # line is reached. A mid-install abort therefore leaves the PRIOR good
    # manifest untouched — so the deploy direction-guard and the /system
    # "Software" card never advertise a SHA the box is not cleanly running.
    # See ADR-0172.
    #
    # JASPER_INSTALL_STATUS=ok records exactly that honest claim: the
    # install process for this SHA completed. (Runtime subsystem health —
    # is voice up? is the mic present? — is a separate layer the deploy
    # verifier surfaces post-restart; the install can't attest to it
    # because it doesn't restart the hardware-gated daemons.)
    local git_sha git_full git_branch
    git_sha="$(resolve_build_sha_short)"
    git_full="${JASPER_DEPLOY_SHA_FULL:-}"
    git_branch="${JASPER_DEPLOY_BRANCH:-}"
    if [[ ( -z "${git_full}" || -z "${git_branch}" ) ]] && command -v git >/dev/null 2>&1 && \
       { [[ -d "${REPO_DIR}/.git" ]] || git -C "${REPO_DIR}" rev-parse --git-dir >/dev/null 2>&1; }; then
        [[ -z "${git_full}" ]] && git_full=$(git -C "${REPO_DIR}" rev-parse HEAD 2>/dev/null || true)
        [[ -z "${git_branch}" ]] && git_branch=$(git -C "${REPO_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
    fi
    if [[ ( -z "${git_full}" || -z "${git_branch}" ) && -f "${STATE_DIR}/build.txt" ]]; then
        [[ -z "${git_full}" ]] && git_full=$(grep -E '^JASPER_GIT_SHA_FULL=' "${STATE_DIR}/build.txt" 2>/dev/null | head -1 | cut -d= -f2-)
        [[ -z "${git_branch}" ]] && git_branch=$(grep -E '^JASPER_GIT_BRANCH=' "${STATE_DIR}/build.txt" 2>/dev/null | head -1 | cut -d= -f2-)
    fi
    git_full="${git_full:-unknown}"
    git_branch="${git_branch:-unknown}"

    # Atomic write: this is the success marker, so a torn write (power loss
    # mid-cat) must never leave a half-line the direction-guard misreads.
    # Mirrors persist_install_profile's tempfile+rename. STATE_DIR already
    # exists by the end of main(); we don't re-`install -d` it so we can't
    # clobber the group-writable widening done earlier in the run.
    local tmp="${STATE_DIR}/build.txt.tmp.$$"
    cat > "${tmp}" <<EOF
JASPER_GIT_SHA=${git_sha}
JASPER_GIT_SHA_FULL=${git_full}
JASPER_GIT_BRANCH=${git_branch}
JASPER_INSTALL_AT=$(date -Iseconds)
JASPER_INSTALL_STATUS=ok
EOF
    chmod 0644 "${tmp}"
    mv -f "${tmp}" "${STATE_DIR}/build.txt"
    echo "  Build manifest (verified install): ${git_sha} on ${git_branch}"
}
