#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Web asset install for deploy/install.sh: copies everything under
# /usr/share/jasper-web/assets/ and writes the install manifest that
# jasper-doctor's check_web_design_assets verifies file-by-file.
#
# Extracted from install_nginx_site() in install.sh (the installer
# remains the only caller; it sources this file REPO_DIR-relative
# from the rsync checkout). Assumes install.sh's globals (REPO_DIR)
# and `set -euo pipefail` from the sourcing shell. The destination
# root honors JASPER_WEB_SHARE_DIR so the hardware-free tests in
# tests/test_install_web_assets.py can run it against a tmp dir —
# same seam check_web_design_assets uses.
#
# What ships, all manifested:
# - app.css — the canonical design-system stylesheet shared by the
#   landing page and the redesigned wizards
#   (jasper.web._common.canonical_page links it, cache-busted by
#   build SHA). Served by the `location /assets/` block.
# - fonts/* — Figtree/Outfit .woff2 plus their OFL license texts
#   (the license files must accompany the fonts).
# - Per-page static assets for the redesigned wizards (/system/,
#   /sound/, /wifi/, ...): the page stylesheet (<page>.css — served
#   immutable + cache-busted like app.css) and the ES module graph
#   (js/*.js — served by the `location ~ \.js$` block with ETag
#   revalidation, since relative imports can't be URL-busted).
#   compgen -G guards each glob (files-exist, not just the dir), so
#   an empty dir can't leave a literal *.css/*.js to fail `install`
#   and abort the deploy under `set -euo pipefail`.
#   `shared` carries the cross-page ES modules (the <dialog>
#   confirm/alert helper at shared/js/dialog.js, the HTML escaper at
#   shared/js/escape.js, the CSRF fetch helpers at shared/js/http.js,
#   and the browser mic/AudioWorklet helpers at
#   shared/js/measurement-audio.js) — same copy shape as a page,
#   no .css.
#
# Page dirs are discovered dynamically: every directory under
# deploy/assets/ (each canonical page's slug, plus `shared`) is
# copied with the same per-dir shape — root *.css, then js/*.js if
# present. Migrating a new wizard therefore needs NO edit here —
# adding deploy/assets/<page>/ is enough, which closes the
# silent-404 failure mode where a new page's CSS/JS never reached
# the Pi.
#
# Every copied path is recorded (relative to assets/) in
# assets/.install-manifest, sorted, and the doctor treats a missing
# manifest as a warn in its own right — there is no fallback asset
# list to drift. The manifest is written atomically: the temp file
# lives next to it (same filesystem — /tmp is tmpfs on the Pi, so a
# rename from there couldn't be atomic) and lands via mv, so a killed
# install can never leave a truncated manifest for the doctor to
# trust; an orphaned temp from a killed run is swept on the next.
# tests/test_install_web_assets.py pins the copy shape, the manifest
# contract, the atomic-write promise, and the doctor/installer
# round-trip.
install_web_assets() {
    local web_root="${JASPER_WEB_SHARE_DIR:-/usr/share/jasper-web}"
    local assets_root="${web_root}/assets"
    local manifest="${assets_root}/.install-manifest"
    local manifest_tmp asset_dir page f
    install -d -m 0755 "${assets_root}"
    rm -f "${manifest}.tmp."*
    manifest_tmp="$(mktemp "${manifest}.tmp.XXXXXX")"

    install -m 0644 "${REPO_DIR}/deploy/assets/app.css" "${assets_root}/app.css"
    echo "app.css" >> "${manifest_tmp}"

    install -d -m 0755 "${assets_root}/fonts"
    install -m 0644 \
        "${REPO_DIR}/deploy/assets/fonts/"* \
        "${assets_root}/fonts/"
    for f in "${REPO_DIR}/deploy/assets/fonts/"*; do
        echo "fonts/$(basename "${f}")" >> "${manifest_tmp}"
    done

    for asset_dir in "${REPO_DIR}/deploy/assets/"*/; do
        page="$(basename "${asset_dir}")"
        [[ "${page}" == "fonts" ]] && continue
        install -d -m 0755 "${assets_root}/${page}"
        if compgen -G "${asset_dir}"*.css > /dev/null; then
            install -m 0644 \
                "${asset_dir}"*.css \
                "${assets_root}/${page}/"
            for f in "${asset_dir}"*.css; do
                echo "${page}/$(basename "${f}")" >> "${manifest_tmp}"
            done
        fi
        if compgen -G "${asset_dir}js/"*.js > /dev/null; then
            install -d -m 0755 "${assets_root}/${page}/js"
            install -m 0644 \
                "${asset_dir}js/"*.js \
                "${assets_root}/${page}/js/"
            for f in "${asset_dir}js/"*.js; do
                echo "${page}/js/$(basename "${f}")" >> "${manifest_tmp}"
            done
        fi
    done
    LC_ALL=C sort "${manifest_tmp}" -o "${manifest_tmp}"
    chmod 0644 "${manifest_tmp}"
    mv -f "${manifest_tmp}" "${manifest}"
}
