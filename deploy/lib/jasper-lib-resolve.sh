#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Shared sibling-vs-installed path decision for jasper's deploy/bin/ scripts.
# Each script must still locate and source THIS file itself (nothing can
# resolve this file's own path before it exists) — jasper_lib_resolve_path
# only collapses the decision a script makes about the libraries *it* depends
# on, so that decision lives in one place instead of being copy-pasted per
# script.

# jasper_lib_resolve_path <sibling-first|installed-first> <override> \
#   <sibling-path> <installed-path>
# Prints the library path a caller should source.
#
# An explicit override always wins outright, with no fallback: a caller
# that forces a path (an env var a test or operator set) is pinning a
# specific state, not asking for best-effort discovery, so silently
# substituting a different file on a miss would defeat the override.
#
# <order> picks which default is tried first when no override is set,
# matching how the calling script is normally invoked: sibling-first for a
# script whose packaged self and its libraries always ship and run as a
# pair (a dev checkout, or install.sh invoking the repo copy mid-install);
# installed-first for a script whose installed copy has no ../lib/ sibling
# at all, so trying the sibling path first would just be a guaranteed miss
# on every real run.
jasper_lib_resolve_path() {
    local order="$1" override="$2" sibling_path="$3" installed_path="$4"
    if [ -n "$override" ]; then
        printf '%s\n' "$override"
        return 0
    fi
    if [ "$order" = "installed-first" ]; then
        if [ -r "$installed_path" ]; then
            printf '%s\n' "$installed_path"
        else
            printf '%s\n' "$sibling_path"
        fi
    else
        if [ -r "$sibling_path" ]; then
            printf '%s\n' "$sibling_path"
        else
            printf '%s\n' "$installed_path"
        fi
    fi
}
