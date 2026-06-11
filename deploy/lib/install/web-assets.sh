#!/usr/bin/env bash
# Per-page web asset install for deploy/install.sh: copies each
# canonical wizard's static assets and writes the install manifest
# that jasper-doctor's check_web_design_assets verifies file-by-file.
#
# Extracted from install_nginx_site() in install.sh (the installer
# remains the only caller; it sources this file REPO_DIR-relative
# from the rsync checkout). Assumes install.sh's globals (REPO_DIR)
# and `set -euo pipefail` from the sourcing shell. The destination
# root honors JASPER_WEB_SHARE_DIR so the hardware-free tests in
# tests/test_install_web_assets.py can run it against a tmp dir —
# same seam check_web_design_assets uses.

# Page-specific static assets for the redesigned wizards (/system/,
# /sound/, /wifi/, ...): the page stylesheet (<page>.css — served
# immutable + cache-busted like app.css) and the ES module graph
# (js/*.js — served by the `location ~ \.js$` block with ETag
# revalidation, since relative imports can't be URL-busted).
# compgen -G guards each glob (files-exist, not just the dir), so an
# empty dir can't leave a literal *.css/*.js to fail `install` and
# abort the deploy under `set -euo pipefail`.
# `shared` carries the cross-page ES modules (the <dialog>
# confirm/alert helper at shared/js/dialog.js, the HTML escaper at
# shared/js/escape.js, the CSRF fetch helpers at shared/js/http.js)
# — same copy shape as a page, no .css.
#
# Discovered dynamically: every directory under deploy/assets/ (each
# canonical page's slug, plus `shared`) is copied with the same
# per-dir shape — root *.css, then js/*.js if present. `fonts` is
# excluded; install_nginx_site copies it (and app.css) directly.
# Migrating a new wizard therefore needs NO edit here — adding
# deploy/assets/<page>/ is enough, which closes the silent-404
# failure mode where a new page's CSS/JS never reached the Pi.
#
# Every copied path is also recorded (relative to assets/) in
# assets/.install-manifest, sorted, so the doctor can verify the
# installed tree without a hand-maintained list that drifts as pages
# migrate. tests/test_install_web_assets.py pins the copy shape, the
# manifest contract, and the doctor/installer manifest-name parity.
install_web_page_assets() {
    local web_root="${JASPER_WEB_SHARE_DIR:-/usr/share/jasper-web}"
    local assets_root="${web_root}/assets"
    local manifest_tmp asset_dir page f
    manifest_tmp="$(mktemp)"
    install -d -m 0755 "${assets_root}"
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
    install -m 0644 "${manifest_tmp}" "${assets_root}/.install-manifest"
    rm -f "${manifest_tmp}"
}
