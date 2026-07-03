#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# arm-ring-a.sh — wire Ring A (jasper-fanin -> CamillaDSP capture, via SHM
# ping-pong ring) into the live audio chain on a lab Pi.
#
# ============================================================================
# SUPERSEDED FOR THE END-TO-END RING BY P2 (audio-graph consolidation).
# The PRODUCT path to arm BOTH rings (Ring A + Ring B) coherently is:
#
#     sudo /opt/jasper/.venv/bin/jasper-fanin-coupling-reconcile shm_ring
#
# See arm.sh's banner for the full rationale. This script remains useful ONLY for
# the isolated Ring-A-only lab experiment (fan-in's Ring A capture WITHOUT
# outputd's Ring B); for the real end-to-end ring, use the reconciler above.
# ============================================================================
#
# THIS IS A PROTOTYPE, LAB-ONLY PROCEDURE, the CAPTURE mirror of arm.sh
# (Ring B). It never touches product Camilla emitters, reconcilers,
# /sound wizard, multiroom, or install.sh — everything it writes is a
# marked block this script can find and strip again, and disarm.sh
# --ring-a is the unconditional rollback. If ANY step below fails, this
# script calls disarm.sh --ring-a itself before exiting non-zero, so a
# failed arm never leaves the box half-wired.
#
# Roles vs Ring B: here the WRITER is jasper-fanin (Rust RingWriter, via
# JASPER_FANIN_CAMILLA_COUPLING=shm_ring) and the READER is CamillaDSP's
# capture (the CAPTURE direction of the jts_ring ioplug, pcm.jts_ring_capture).
# This replaces the fan-in -> camilla dsnoop capture hop.
#
# Preconditions checked (refuses to proceed, no changes made, if any fail):
#   - libasound_module_pcm_jts_ring.so is already built and installed
#     (run build-on-pi.sh first).
#   - jasper-fanin is running and its resolved coupling is NOT already a
#     non-loopback mode (this script only arms on top of the packaged
#     loopback default; a transport_pipe or already-armed shm_ring box
#     is a clean refusal).
#   - the SSH host is REACHABLE — an unreachable host is a FAILURE here,
#     never a skip (a silent skip could leave a partially-armed box).
#
# Steps, in order (each is individually observable; see the printed
# "verify:" line after each one):
#   1. Preflight (read-only over SSH, BatchMode). Unreachable => FAIL.
#   2. Install /etc/alsa/conf.d/98-jts-ring-a-proto.conf (registers
#      pcm.jts_ring_capture as a CAPTURE reader of the SHM ring — mode
#      0644, resolvable by the CamillaDSP runtime user).
#   3. Resolvability probe: `arecord -D jts_ring_capture ... `. Terminates
#      via the reader's writer-dead timer-paced silence path (fan-in's
#      writer is not yet arming the ring at this point).
#   4. Flip jasper-fanin to shm_ring (marked block ->
#      /var/lib/jasper/fanin.env: JASPER_FANIN_CAMILLA_COUPLING=shm_ring +
#      JASPER_FANIN_RING_PATH + JASPER_FANIN_RING_SLOTS), restart fanin
#      (ordered, reset-failed + spacing guard), verify it comes up active
#      and creates the ring file.
#   5. Enforce ring perms (program.ring root:jasper 0664, dir 0775) so the
#      CamillaDSP runtime user can rw the header (reader writes read_seq +
#      heartbeat). This is the EACCES class the outputd fix round hit.
#   6. Build + load the Ring A hand Camilla config (make-camilla-ring-config.sh
#      --ring-a: devices.capture -> {type Alsa, device jts_ring_capture,
#      format S16_LE}), point the statefile at it, restart jasper-camilla
#      (ordered).
#   7. Final verify + next steps.
#
# Usage:
#   bash scripts/ring-proto/arm-ring-a.sh
#   PI_HOST=jts3.local bash scripts/ring-proto/arm-ring-a.sh
#   JASPER_RING_PROTO_SLOTS=8 bash scripts/ring-proto/arm-ring-a.sh   # default 8

set -uo pipefail
# NOT `set -e`: this script runs a step, checks its own exit code, and rolls
# everything back on any failure — `set -e` would abort mid-cleanup on the very
# failure path we handle gracefully. Every step checks $? itself. (Same rationale
# as arm.sh.)

# Captured BEFORE sourcing _lib.sh: _lib.sh's fallback silently resolves an unset
# PI_HOST to jts.local. This capture lets _guard.sh tell "the caller explicitly
# set PI_HOST" apart from "PI_HOST is only the ambient default" — see _guard.sh.
export JASPER_RING_PROTO_CALLER_PI_HOST="${PI_HOST:-}"

# RING_PROTO_DIR, not SCRIPT_DIR: _lib.sh (sourced below) defines its own
# SCRIPT_DIR pointing at scripts/, and sourcing clobbers a same-named variable in
# this shell scope — see the full explanation in arm.sh.
RING_PROTO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${RING_PROTO_DIR}/../.." && pwd)"
# shellcheck source=../_lib.sh
. "${REPO_ROOT}/scripts/_lib.sh"
# shellcheck source=./_guard.sh
. "${RING_PROTO_DIR}/_guard.sh"
require_explicit_ring_proto_target

CAPTURE_DEVICE="${JASPER_RING_PROTO_CAPTURE_DEVICE:-jts_ring_capture}"
# Default 8 slots for Ring A (the capture BufferManager negotiates 1024 @
# chunksize 256 => 8 slots * 128 = 1024-frame buffer; see the plan). Overridable
# 2..16 for degraded/experimental widths. n_slots <-> JASPER_FANIN_RING_SLOTS is
# the drift axis; the ring header's own validation is the runtime backstop.
RING_SLOTS="${JASPER_RING_PROTO_SLOTS:-8}"
RING_PATH="${JASPER_RING_PROTO_RING_PATH:-/dev/shm/jts-ring/program.ring}"
CONF_D_PATH="/etc/alsa/conf.d/98-jts-ring-a-proto.conf"
FANIN_ENV="/var/lib/jasper/fanin.env"
ROLLBACK_STATE_DIR="/var/lib/jasper/ring-proto"
ROLLBACK_ENV="${ROLLBACK_STATE_DIR}/rollback-a.env"
BEGIN_MARKER="# BEGIN jts-ring-a-proto (scripts/ring-proto/arm-ring-a.sh)"
END_MARKER="# END jts-ring-a-proto"
# Minimum seconds between the two daemon restarts this script issues (fanin then
# camilla). A too-tight back-to-back restart of two daemons that share the ring
# can race the ring create/attach handshake; the spacing guard + reset-failed
# keep systemd's start-limit counters clean across the pair.
RESTART_SPACING_SEC="${JASPER_RING_PROTO_RESTART_SPACING_SEC:-90}"

fail_and_rollback() {
    echo "" >&2
    echo "!!! arm-ring-a.sh FAILED at: $* — rolling back via disarm.sh --ring-a !!!" >&2
    echo "" >&2
    bash "${RING_PROTO_DIR}/disarm.sh" --ring-a || echo "warning: disarm.sh --ring-a itself reported a problem — inspect ${PI_USER}@${PI_HOST} by hand" >&2
    exit 1
}

ssh_ok() { ssh -o BatchMode=yes -o ConnectTimeout=8 "${PI_USER}@${PI_HOST}" "$@"; }

# Ordered restart of a unit: reset-failed first (clear any prior start-limit
# state so this restart is not refused), then restart, then verify active.
# Returns 0 on active, 1 otherwise (caller decides rollback). Enforces the
# spacing guard against the LAST restart this script issued.
LAST_RESTART_EPOCH=0
ordered_restart() {
    local unit="$1"
    local now elapsed wait
    now="$(date +%s)"
    if (( LAST_RESTART_EPOCH > 0 )); then
        elapsed=$(( now - LAST_RESTART_EPOCH ))
        if (( elapsed < RESTART_SPACING_SEC )); then
            wait=$(( RESTART_SPACING_SEC - elapsed ))
            echo "  (spacing guard: waiting ${wait}s before restarting ${unit} — two daemons share the ring)"
            sleep "${wait}"
        fi
    fi
    ssh_ok "sudo systemctl reset-failed ${unit} 2>/dev/null; sudo systemctl restart ${unit}" || return 1
    LAST_RESTART_EPOCH="$(date +%s)"
    sleep 2
    local active
    active="$(ssh_ok "systemctl is-active ${unit}" 2>/dev/null)"
    [[ "${active}" == "active" ]]
}

echo "=== Ring A prototype: ARM on ${PI_USER}@${PI_HOST} ==="
echo "  capture device: ${CAPTURE_DEVICE}"
echo "  ring path:      ${RING_PATH}"
echo "  ring slots:     ${RING_SLOTS}"
echo ""

# ---------------------------------------------------------------------
# Step 1 — preflight (read-only, no changes made on any failure here)
# ---------------------------------------------------------------------
echo "--- Step 1/7: preflight ---"

if ! ssh_ok true; then
    echo "error: cannot reach ${PI_USER}@${PI_HOST} over SSH (BatchMode)." >&2
    echo "An unreachable host is a FAILURE for arm-ring-a.sh (never a silent skip)." >&2
    echo "No changes made." >&2
    exit 1
fi

so_check_out="$(ssh_ok 'test -f /usr/lib/aarch64-linux-gnu/alsa-lib/libasound_module_pcm_jts_ring.so && echo present || echo missing')"
if [[ "${so_check_out}" != "present" ]]; then
    cat >&2 <<EOF
error: libasound_module_pcm_jts_ring.so is not installed on ${PI_HOST}.
Run bash scripts/ring-proto/build-on-pi.sh first.
No changes made.
EOF
    exit 1
fi
echo "  OK   ioplug .so is installed"

# jasper-fanin must be running (we are about to flip its coupling + restart it).
fanin_mainpid="$(ssh_ok 'systemctl show jasper-fanin.service -p MainPID --value' 2>/dev/null | tr -dc '0-9')"
if [[ -z "${fanin_mainpid}" || "${fanin_mainpid}" == "0" ]]; then
    echo "error: jasper-fanin has no MainPID on ${PI_HOST} — the daemon is not" \
        "running. Bring it up (systemctl start jasper-fanin) and re-run." >&2
    echo "No changes made." >&2
    exit 1
fi
# Resolve fanin's ACTUAL running coupling from /proc/<MainPID>/environ (the
# "verify at the user's surface" rule — the running daemon is governed by exactly
# these values, including the fanin.env layer systemctl show does NOT report).
fanin_env_raw="$(ssh_ok "sudo sh -c 'tr \"\\0\" \"\\n\" < /proc/${fanin_mainpid}/environ'" 2>/dev/null)"
if [[ -z "${fanin_env_raw}" ]]; then
    echo "error: could not read /proc/${fanin_mainpid}/environ for jasper-fanin" \
        "on ${PI_HOST} (permission or the process exited between calls?)." >&2
    echo "No changes made." >&2
    exit 1
fi
existing_coupling="$(printf '%s\n' "${fanin_env_raw}" | sed -n 's/^JASPER_FANIN_CAMILLA_COUPLING=//p' | head -1)"
existing_coupling="${existing_coupling:-loopback}"
if [[ "${existing_coupling}" != "loopback" ]]; then
    echo "error: jasper-fanin's resolved JASPER_FANIN_CAMILLA_COUPLING=${existing_coupling}" \
        "on ${PI_HOST} — arm-ring-a.sh only arms on top of the packaged 'loopback'" \
        "default. Disarm/clear the existing coupling first." >&2
    echo "No changes made." >&2
    exit 1
fi
echo "  OK   jasper-fanin active (pid ${fanin_mainpid}), coupling=${existing_coupling}"

if [[ ! "${RING_SLOTS}" =~ ^[0-9]+$ ]] || (( RING_SLOTS < 2 || RING_SLOTS > 16 )); then
    echo "error: JASPER_RING_PROTO_SLOTS=${RING_SLOTS} must be an integer 2..16" \
        "(8 = validated Ring A capture geometry [1024-frame buffer]; matches" \
        "JTS_RING_MAX_SLOTS)." >&2
    exit 1
fi

if ssh_ok "test -f ${CONF_D_PATH}"; then
    echo "error: ${CONF_D_PATH} already exists on ${PI_HOST} — looks already armed." \
        "Run disarm.sh --ring-a first if you want to re-arm." >&2
    echo "No changes made." >&2
    exit 1
fi

# Record rollback state BEFORE any mutation: the currently-live statefile
# config_path so disarm can restore it verbatim even if interrupted partway.
current_config_path_raw="$(ssh_ok "sed -n 's/^[[:space:]]*config_path:[[:space:]]*//p' /var/lib/camilladsp/outputd-statefile.yml | head -1")"
current_config_path="${current_config_path_raw//[\'\"]/}"
if [[ -z "${current_config_path}" ]]; then
    echo "error: could not read the current config_path from" \
        "/var/lib/camilladsp/outputd-statefile.yml on ${PI_HOST}." >&2
    echo "No changes made." >&2
    exit 1
fi
if ! ssh_ok "sudo install -d -m 0755 ${ROLLBACK_STATE_DIR} && printf 'ORIGINAL_CAMILLA_CONFIG_PATH=%s\n' '${current_config_path}' | sudo tee ${ROLLBACK_ENV} >/dev/null"; then
    echo "error: could not record rollback state on ${PI_HOST}." >&2
    exit 1
fi
echo "  OK   rollback state recorded: original config_path=${current_config_path}"
echo ""

# ---------------------------------------------------------------------
# Step 2 — install the ALSA plugin registration drop-in (capture)
# ---------------------------------------------------------------------
echo "--- Step 2/7: install ${CONF_D_PATH} ---"

# NOTE: unquoted heredoc (<<EOF) — the body interpolates ${CAPTURE_DEVICE}/
# ${RING_PATH}/${RING_SLOTS}/markers. An odd apostrophe anywhere in the body is a
# bash parse error even in an unquoted heredoc; check `bash -n` after edits.
conf_body="$(cat <<EOF
${BEGIN_MARKER}
# Ring A latency prototype (branch latency/combo-night). Registers
# pcm.${CAPTURE_DEVICE} as a CAPTURE reader of the SHM ping-pong ring at
# ${RING_PATH} (writer = jasper-fanin). Lab-only: installed by
# arm-ring-a.sh, removed by disarm.sh --ring-a. Never shipped by install.sh.
#
# Renderer-user resolvability (AGENTS.md): system-wide conf.d drop-in (0644),
# so the CamillaDSP runtime user can resolve pcm.${CAPTURE_DEVICE}. period_frames
# and n_slots MUST match the jasper-fanin ring geometry (JASPER_FANIN_RING_SLOTS).
pcm.${CAPTURE_DEVICE} {
    type jts_ring
    path "${RING_PATH}"
    period_frames 128
    n_slots ${RING_SLOTS}
}
${END_MARKER}
EOF
)"

if ! printf '%s\n' "${conf_body}" | ssh_ok "sudo tee ${CONF_D_PATH} >/dev/null && sudo chmod 0644 ${CONF_D_PATH}"; then
    fail_and_rollback "step 2 (install ${CONF_D_PATH})"
fi
echo "  OK   wrote ${CONF_D_PATH}"
echo "  verify: ssh ${PI_USER}@${PI_HOST} 'cat ${CONF_D_PATH}'"
echo ""

# ---------------------------------------------------------------------
# Step 3 — resolvability probe (capture)
# ---------------------------------------------------------------------
echo "--- Step 3/7: resolvability probe (arecord -D ${CAPTURE_DEVICE}) ---"
# fan-in's writer is NOT arming the ring yet, so the reader's writer-dead
# timer-paced silence path is what makes this terminate (it captures 1 s of
# silence and exits 0). A hang here would mean the ioplug's writer-dead
# detection is broken — exactly what this probe exists to catch.
if ! ssh_ok "arecord -D ${CAPTURE_DEVICE} -c 2 -r 48000 -f S16_LE -d 1 /dev/null" >/dev/null 2>&1; then
    fail_and_rollback "step 3 (arecord resolvability probe against ${CAPTURE_DEVICE})"
fi
echo "  OK   arecord opened ${CAPTURE_DEVICE} and completed (writer-dead silence path, as expected)"
echo ""

# ---------------------------------------------------------------------
# Step 4 — flip jasper-fanin to shm_ring + restart
# ---------------------------------------------------------------------
echo "--- Step 4/7: arm jasper-fanin (JASPER_FANIN_CAMILLA_COUPLING=shm_ring) ---"

# STALE-RING GUARD (before the fanin restart). jasper-fanin is the WRITER here,
# and its unit carries StartLimitBurst=5 + StartLimitAction=reboot. If fanin
# restarts into shm_ring and finds a STALE ring at ${RING_PATH} it cannot cleanly
# attach to, it crash-loops and — after 5 failures — REBOOTS THE BOX. Two stale
# shapes reach here on a lab box:
#   (a) the step-3 arecord resolvability probe CREATES ${RING_PATH} owned by the
#       SSH user (e.g. pi:pi 0660). A non-root fanin (WS1 privilege separation)
#       then cannot open it O_RDWR -> EACCES -> crash-loop. (Step 5 fixes perms,
#       but step 5 runs AFTER this restart.)
#   (b) a ring left by a PRIOR arm with a DIFFERENT geometry (a re-arm at a new
#       JASPER_RING_PROTO_SLOTS) -> fanin's create_or_attach hits an n_slots
#       mismatch -> Fatal -> crash-loop. Root ownership does not help here.
# Removing the stale file first makes fanin CREATE a fresh ring with the exact
# requested geometry AND correct ownership (fanin's own user) on attach, closing
# both lanes. Safe: the ring is tmpfs and fanin recreates it; and this is a
# Ring-A-only removal of the FILE (never the /dev/shm/jts-ring dir), so an armed
# Ring B sharing that dir is untouched (mirrors disarm.sh --ring-a step 4).
if ssh_ok "test -e ${RING_PATH}"; then
    if ssh_ok "sudo rm -f ${RING_PATH}"; then
        echo "  OK   removed a pre-existing ${RING_PATH} (probe/stale) so fanin creates a fresh, correctly-owned ring"
    else
        echo "  jasper-fanin would restart into a stale ring it may crash-loop on" \
            "(StartLimitAction=reboot) — could not remove ${RING_PATH}." >&2
        fail_and_rollback "step 4 (remove stale ring ${RING_PATH} before fanin restart)"
    fi
fi

fanin_block="$(cat <<EOF
${BEGIN_MARKER}
JASPER_FANIN_CAMILLA_COUPLING=shm_ring
JASPER_FANIN_RING_PATH=${RING_PATH}
JASPER_FANIN_RING_SLOTS=${RING_SLOTS}
${END_MARKER}
EOF
)"

if ! printf '%s\n' "${fanin_block}" | ssh_ok "sudo mkdir -p \$(dirname ${FANIN_ENV}) && cat | sudo tee -a ${FANIN_ENV} >/dev/null"; then
    fail_and_rollback "step 4 (append shm_ring block to ${FANIN_ENV})"
fi

if ! ordered_restart jasper-fanin; then
    echo "  jasper-fanin did not come up active after the coupling flip." >&2
    ssh_ok "journalctl -u jasper-fanin -n 40 --no-pager" >&2
    fail_and_rollback "step 4 (jasper-fanin restart into shm_ring)"
fi

# Verify fanin created the ring file (the writer creates it on attach).
sleep 1
if ! ssh_ok "test -f ${RING_PATH}"; then
    echo "  jasper-fanin is active but did not create the ring file ${RING_PATH}." >&2
    ssh_ok "journalctl -u jasper-fanin -n 40 --no-pager" >&2
    fail_and_rollback "step 4 (fanin active but ring file ${RING_PATH} absent)"
fi
echo "  OK   jasper-fanin active and created ${RING_PATH}"
echo "  verify: ssh ${PI_USER}@${PI_HOST} 'journalctl -u jasper-fanin -n 20 | grep -i ring'"
echo ""

# ---------------------------------------------------------------------
# Step 5 — enforce ring perms (reader must rw the header)
# ---------------------------------------------------------------------
echo "--- Step 5/7: enforce ring perms (root:jasper, dir 0775, file 0664) ---"
# The reader (CamillaDSP) WRITES read_seq + heartbeat into the header, so its
# runtime user needs write access to a file the fanin user created. Group jasper
# + 0664/0775 grants that; without it the second daemon fails attach with EACCES
# (the exact class the outputd fix round hit on-Pi).
ring_dir="$(dirname "${RING_PATH}")"
if ! ssh_ok "sudo chgrp jasper ${ring_dir} ${RING_PATH} && sudo chmod 0775 ${ring_dir} && sudo chmod 0664 ${RING_PATH}"; then
    fail_and_rollback "step 5 (enforce ring perms on ${RING_PATH})"
fi
perms="$(ssh_ok "stat -c '%a %U:%G' ${RING_PATH}" 2>/dev/null)"
echo "  OK   ring perms: ${RING_PATH} -> ${perms} (dir $(ssh_ok "stat -c '%a %U:%G' ${ring_dir}" 2>/dev/null))"
echo ""

# ---------------------------------------------------------------------
# Step 6 — build + load the Ring A hand Camilla capture config
# ---------------------------------------------------------------------
echo "--- Step 6/7: build + load the Ring A hand Camilla config ---"

if ! JASPER_RING_PROTO_CAPTURE_DEVICE="${CAPTURE_DEVICE}" bash "${RING_PROTO_DIR}/make-camilla-ring-config.sh" --ring-a; then
    fail_and_rollback "step 6 (make-camilla-ring-config.sh --ring-a)"
fi

new_config_path="/var/lib/camilladsp/ring_proto_a.yml"
statefile_block=$(cat <<PYEOF
import sys
sys.path.insert(0, "/opt/jasper")
from jasper.active_speaker.runtime_contract import write_camilla_statefile
write_camilla_statefile(
    "/var/lib/camilladsp/outputd-statefile.yml",
    "${new_config_path}",
)
print("statefile updated: config_path=${new_config_path}")
PYEOF
)
if ! printf '%s\n' "${statefile_block}" | ssh_ok "sudo /opt/jasper/.venv/bin/python"; then
    fail_and_rollback "step 6 (pointing the statefile at ${new_config_path})"
fi

if ! ordered_restart jasper-camilla; then
    echo "  jasper-camilla did not come up active after the capture-swap." >&2
    ssh_ok "journalctl -u jasper-camilla -n 40 --no-pager" >&2
    fail_and_rollback "step 6 (jasper-camilla restart into the Ring A capture config)"
fi
echo "  OK   jasper-camilla active on the Ring A capture config"
echo "  verify: ssh ${PI_USER}@${PI_HOST} 'journalctl -u jasper-camilla -n 10'"
echo ""

# ---------------------------------------------------------------------
# Step 7 — final verify
# ---------------------------------------------------------------------
echo "--- Step 7/7: armed. Manual verification + next steps ---"
summary="$(cat <<'SUMMARY_EOF'

Ring A is now wired into the live chain on __PI_HOST__:

  jasper-fanin (JASPER_FANIN_CAMILLA_COUPLING=shm_ring) -> SHM ring
    -> pcm.__CAPTURE_DEVICE__ (ioplug capture) -> CamillaDSP -> Ring B / outputd
    -> final DAC

Play music through the box now (any source) and check:

  # fan-in STATUS ring counters (transport:"shm_ring"):
  ssh __PI_TARGET__ 'curl -s http://127.0.0.1:8780/state | jq .fanin 2>/dev/null || echo "(not surfaced in /state)"'

  # Journal for xruns / drops / resyncs on either daemon:
  ssh __PI_TARGET__ 'journalctl -u jasper-fanin -u jasper-camilla --since "2 min ago" | grep -iE "xrun|drop|resync|error|silence"'

To roll back everything this script did:

  bash scripts/ring-proto/disarm.sh --ring-a
SUMMARY_EOF
)"
summary="${summary//__PI_HOST__/${PI_HOST}}"
summary="${summary//__CAPTURE_DEVICE__/${CAPTURE_DEVICE}}"
summary="${summary//__PI_TARGET__/${PI_USER}@${PI_HOST}}"
printf '%s\n' "${summary}"
