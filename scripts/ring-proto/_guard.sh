#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# shellcheck shell=bash
# _guard.sh — shared laptop-side safety guard for scripts/ring-proto/'s
# MUTATING scripts (arm.sh, disarm.sh, build-on-pi.sh,
# make-camilla-ring-config.sh). Not executable, no shebang side effects —
# source it, same convention as scripts/_lib.sh.
#
# WHY THIS EXISTS: scripts/_lib.sh's PI_HOST fallback
# (PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}") is correct and
# intentional for ordinary product scripts, which are meant to operate
# on the household's real speaker. This prototype's scripts mutate ALSA
# plugin registration, systemd env files, and the live CamillaDSP
# statefile — an operator who runs `bash scripts/ring-proto/build-on-pi.sh`
# from a checkout with no PI_HOST set and no .env.local present will
# silently land on the REAL jts.local box, not a scratch lab Pi. This
# was caught by hand during this script family's own development:
# `build-on-pi.sh` run with no PI_HOST set staged source and ran `make`
# against the production jts.local box (the build failed on unrelated
# compile errors before anything was installed, and the scratch
# directory was manually cleaned up afterward, but the near-miss is
# exactly the class of accident this guard exists to prevent).
#
# Call require_explicit_ring_proto_target AFTER sourcing scripts/_lib.sh
# (so PI_HOST/PI_USER are resolved) but BEFORE any SSH command that
# could mutate the target. It refuses to proceed unless the caller
# explicitly named a target via one of:
#   - PI_HOST set in the invoking shell's environment (before _lib.sh
#     ran) — captured as JASPER_RING_PROTO_CALLER_PI_HOST by each
#     script, immediately after its own `set -euo pipefail` and BEFORE
#     sourcing _lib.sh (see the pattern comment in arm.sh).
#   - .env.local's PI_HOST, which is itself an explicit, checked-in-once
#     laptop choice (see AGENTS.md "Laptop-side state") — not the same
#     as a bare unset default.
#
# Passing neither is a hard refusal with no side effects: this is a
# prototype for a lab box, and "just run it" must never quietly target
# whatever jts.local happens to resolve to today.
require_explicit_ring_proto_target() {
    local caller_pi_host="${JASPER_RING_PROTO_CALLER_PI_HOST:-}"
    local env_local_path="${REPO_ROOT}/.env.local"
    local env_local_has_pi_host=0
    if [[ -f "${env_local_path}" ]] && grep -Eq '^[[:space:]]*PI_HOST[[:space:]]*=' "${env_local_path}"; then
        env_local_has_pi_host=1
    fi

    if [[ -n "${caller_pi_host}" || "${env_local_has_pi_host}" -eq 1 ]]; then
        return 0
    fi

    cat >&2 <<EOF
error: no explicit PI_HOST — refusing to run against the bare
scripts/_lib.sh default (currently resolves to: ${PI_HOST}).

This script mutates ALSA plugin registration, systemd env files, and/or
the live CamillaDSP statefile on whatever box it targets. It must never
silently land on a real/production speaker because no target was named.

Set PI_HOST explicitly, for example:

  PI_HOST=jts3.local bash $(basename "$0") ...

or run 'bash scripts/onboard.sh <hostname> --adopt' / 'bash scripts/use
<hostname>' once to persist PI_HOST in .env.local for this checkout, per
AGENTS.md "Laptop-side state — .env.local and CLAUDE.local.md".

No changes made.
EOF
    exit 1
}
