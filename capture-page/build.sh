#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Assemble the Cloudflare Pages publish bundle for the phone-mic capture page.
#
# The page reuses the canonical JTS browser capture helper
# (deploy/assets/shared/js/measurement-audio.js) rather than forking it — the
# Pages site is a separate origin and cannot import from the Pi, so the build
# COPIES the single source of truth into the bundle. Run before `wrangler pages
# deploy capture-page/dist`. See capture-page/README.md.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${HERE}/.." && pwd)"
DIST="${HERE}/dist"
SHARED="${REPO_ROOT}/deploy/assets/shared/js/measurement-audio.js"

if [[ ! -f "${SHARED}" ]]; then
    echo "error: canonical measurement-audio.js not found at ${SHARED}" >&2
    exit 1
fi

rm -rf "${DIST}"
mkdir -p "${DIST}/js"

cp "${HERE}/index.html" "${DIST}/index.html"
cp "${HERE}"/js/*.js "${DIST}/js/"
# Single source of truth: the shared helper is copied, never forked.
cp "${SHARED}" "${DIST}/js/measurement-audio.js"

echo "built capture-page bundle -> ${DIST}"
echo "  $(find "${DIST}" -type f | wc -l | tr -d ' ') files"
echo "deploy with: npx wrangler pages deploy ${DIST} --project-name jts-capture-page"
