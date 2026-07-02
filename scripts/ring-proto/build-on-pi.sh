#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# build-on-pi.sh — rsync the Ring B ALSA ioplug source to the lab Pi and
# build it there. The ioplug is Pi-only: it links libasound and targets
# aarch64, so it cannot be cross-compiled or tested on the laptop (see
# host-check.sh for what IS checked off-Pi — the pure-logic core via
# test_ring_core.c).
#
# This script is a PROTOTYPE build helper, not an install step. It does
# NOT touch systemd units, /etc/alsa/conf.d, or any env file — that's
# arm.sh's job, and only after this build succeeds. Nothing this script
# does survives a `disarm.sh --purge` except the compiled .so, which
# --purge explicitly removes.
#
# What it does, in order:
#   1. Confirm libasound2-dev is present on the Pi (verified present on
#      jts3.local and jts.local as of 2026-07 — this is a preflight,
#      not a first install; product install.sh already installs it).
#   2. rsync c/jts-ring-ioplug/ to a working dir under the SSH user's
#      home (NOT /opt/jasper — that's the product runtime tree; see
#      AGENTS.md "Runtime Python lives in /opt/jasper" for why product
#      code never gets hand-patched there directly. This prototype's
#      .so is a leaf artifact, so it installs straight to the ALSA
#      plugin dir instead of going through /opt/jasper at all).
#   3. `make plugin bench` in that working dir.
#   4. Install the built .so to the Pi's ALSA plugin directory (mode
#      0644, matches the system-wide plugin convention other ALSA
#      plugins on this box already use — bluealsa, jack).
#   5. Install the bench binary alongside it under the same working dir
#      (not on PATH — arm.sh and the README invoke it by full path).
#
# Usage:
#   bash scripts/ring-proto/build-on-pi.sh
#   PI_HOST=jts3.local bash scripts/ring-proto/build-on-pi.sh
#
# Idempotent: re-running rebuilds from the current source tree and
# overwrites the previous .so/bench binary. Safe to run repeatedly while
# iterating on the C core.

set -euo pipefail

# Captured BEFORE sourcing _lib.sh — see _guard.sh for why this matters.
export JASPER_RING_PROTO_CALLER_PI_HOST="${PI_HOST:-}"

# RING_PROTO_DIR, not SCRIPT_DIR: _lib.sh (sourced below) defines its own
# SCRIPT_DIR pointing at scripts/, and sourcing clobbers a same-named
# variable in this shell scope — see the full explanation in arm.sh.
RING_PROTO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${RING_PROTO_DIR}/../.." && pwd)"
# shellcheck source=../_lib.sh
. "${REPO_ROOT}/scripts/_lib.sh"
# shellcheck source=./_guard.sh
. "${RING_PROTO_DIR}/_guard.sh"
require_explicit_ring_proto_target

IOPLUG_SRC="${REPO_ROOT}/c/jts-ring-ioplug"
# Working dir on the Pi, under the SSH user's home — a prototype scratch
# tree, not the product /opt/jasper runtime.
REMOTE_WORK_DIR="${JASPER_RING_PROTO_REMOTE_DIR:-/home/${PI_USER}/jts-ring-proto}"
# ALSA's system-wide plugin dir on Raspberry Pi OS Trixie aarch64
# (confirmed live on jts3.local and jts.local, 2026-07: this is where
# bluealsa/jack's plugins already live).
ALSA_PLUGIN_DIR="/usr/lib/aarch64-linux-gnu/alsa-lib"

echo "=== Ring B prototype: build ioplug on ${PI_USER}@${PI_HOST} ==="

if [[ ! -d "${IOPLUG_SRC}" ]]; then
    cat >&2 <<EOF
error: ${IOPLUG_SRC} does not exist yet.

The ALSA ioplug source (c/jts-ring-ioplug/) is a separate, parallel piece
of this prototype (the C/Rust core track) and has not landed on this
branch yet. This script only stages + builds what already exists — it
does not generate the ioplug itself. Re-run once that source has landed.
EOF
    exit 1
fi

echo "--- Preflight: reachability + libasound2-dev ---"
if ! ssh -o BatchMode=yes -o ConnectTimeout=8 "${PI_USER}@${PI_HOST}" true; then
    echo "error: cannot reach ${PI_USER}@${PI_HOST} over SSH (BatchMode)" >&2
    exit 1
fi

if ! ssh -o BatchMode=yes "${PI_USER}@${PI_HOST}" \
    'dpkg -s libasound2-dev >/dev/null 2>&1'; then
    cat >&2 <<EOF
error: libasound2-dev is not installed on ${PI_HOST}.

This is unexpected — product install.sh already installs it. If this is
a from-scratch Pi that has never run install.sh, run the normal onboard
flow first (see AGENTS.md "Deploying code changes to the Pi"); this
prototype does not install base packages.
EOF
    exit 1
fi
echo "libasound2-dev: present"

echo "--- Staging source: ${IOPLUG_SRC} -> ${PI_USER}@${PI_HOST}:${REMOTE_WORK_DIR}/c/jts-ring-ioplug ---"
ssh -o BatchMode=yes "${PI_USER}@${PI_HOST}" \
    "mkdir -p '${REMOTE_WORK_DIR}/c/jts-ring-ioplug'"
rsync -az --delete \
    -e "ssh -o BatchMode=yes" \
    "${IOPLUG_SRC}/" \
    "${PI_USER}@${PI_HOST}:${REMOTE_WORK_DIR}/c/jts-ring-ioplug/"

echo "--- Building: make plugin bench ---"
ssh -o BatchMode=yes "${PI_USER}@${PI_HOST}" \
    "cd '${REMOTE_WORK_DIR}/c/jts-ring-ioplug' && make plugin bench"

echo "--- Installing plugin to ${ALSA_PLUGIN_DIR} ---"
ssh -o BatchMode=yes "${PI_USER}@${PI_HOST}" bash -s -- \
    "${REMOTE_WORK_DIR}" "${ALSA_PLUGIN_DIR}" <<'REMOTE'
set -euo pipefail
work_dir="$1"
plugin_dir="$2"
so_path="${work_dir}/c/jts-ring-ioplug/libasound_module_pcm_jts_ring.so"
if [[ ! -f "${so_path}" ]]; then
    echo "error: build did not produce ${so_path}" >&2
    exit 1
fi
sudo install -m 0644 "${so_path}" "${plugin_dir}/libasound_module_pcm_jts_ring.so"
echo "installed: ${plugin_dir}/libasound_module_pcm_jts_ring.so"
REMOTE

echo "--- Verifying bench binaries ---"
ssh -o BatchMode=yes "${PI_USER}@${PI_HOST}" bash -c "
    ok=1
    for b in ring_writer_bench ring_reader_bench; do
        if [[ -x '${REMOTE_WORK_DIR}/c/jts-ring-ioplug/'\$b ]]; then
            echo \"bench binary: ${REMOTE_WORK_DIR}/c/jts-ring-ioplug/\$b\"
        else
            echo \"error: bench binary \$b not produced\" >&2
            ok=0
        fi
    done
    [[ \$ok -eq 1 ]]
"

cat <<EOF

=== Build complete ===

Plugin:  ${ALSA_PLUGIN_DIR}/libasound_module_pcm_jts_ring.so  (on ${PI_HOST})
Bench:   ${REMOTE_WORK_DIR}/c/jts-ring-ioplug/ring_writer_bench  (Ring B writer, on ${PI_HOST})
         ${REMOTE_WORK_DIR}/c/jts-ring-ioplug/ring_reader_bench  (Ring A reader, on ${PI_HOST})

Next: bash scripts/ring-proto/arm.sh  (this is a build-only step — arm.sh
wires it into the live ALSA/outputd/Camilla chain).

Rollback: this script installed one .so and one bench binary; nothing
else on the Pi changed. 'bash scripts/ring-proto/disarm.sh --purge'
removes the installed .so; the bench binary and ${REMOTE_WORK_DIR} are
harmless scratch files you can 'rm -rf' by hand at any time.
EOF
