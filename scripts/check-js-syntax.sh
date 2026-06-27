#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Syntax-check browser ES modules and Node harnesses.
#
# `node --check a.js b.js` only checks the first script, treating the rest as
# ordinary argv. Keep the one-file-at-a-time loop here so CI and pre-commit
# share the same behavior.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

js_files=()
if (( $# > 0 )); then
    for f in "$@"; do
        rel="${f#./}"
        case "${rel}" in
            deploy/assets/*/js/*.js|tests/js/*.mjs|relay/src/*.js|capture-page/js/*.js)
                if [[ -f "${REPO_ROOT}/${rel}" || -f "${f}" ]]; then
                    js_files+=("${rel}")
                fi
                ;;
        esac
    done
else
    while IFS= read -r f; do
        js_files+=("${f}")
    done < <(
        git -C "${REPO_ROOT}" ls-files \
            'deploy/assets/**/js/*.js' \
            'tests/js/*.mjs' \
            'relay/src/*.js' \
            'capture-page/js/*.js'
    )
fi

if (( ${#js_files[@]} == 0 )); then
    if (( $# > 0 )); then
        exit 0
    fi
    echo "no JavaScript files found for syntax check" >&2
    exit 1
fi

for f in "${js_files[@]}"; do
    if [[ "${f}" = /* ]]; then
        node --check "${f}"
    else
        node --check "${REPO_ROOT}/${f}"
    fi
done

echo "node --check clean over ${#js_files[@]} JavaScript file(s)"
