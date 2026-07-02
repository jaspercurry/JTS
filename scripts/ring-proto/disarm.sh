#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# disarm.sh — unconditional rollback for the SHM ring prototype. Two modes:
#   default / --ring-b: rolls back Ring B (arm.sh — CamillaDSP writes,
#     jasper-outputd reads; JASPER_OUTPUTD_CONTENT_BRIDGE in outputd.env).
#   --ring-a:           rolls back Ring A (arm-ring-a.sh — jasper-fanin writes,
#     CamillaDSP capture reads; JASPER_FANIN_CAMILLA_COUPLING in fanin.env).
# Restores the box to the exact state the matching arm script found it in.
#
# IDEMPOTENT AND SAFE TO RUN COLD: every step below checks whether its
# own target exists before touching it, so running this against a box
# that was never armed (or was only partially armed, or was already
# disarmed) is a no-op for anything not present — never an error, never
# a change to something the arm script did not touch.
#
# This is what arm.sh / arm-ring-a.sh themselves call on any mid-arm
# failure (see fail_and_rollback there), so it must never assume every
# marked block is present.
#
# Steps, in REVERSE order of arm.sh (statefile/Camilla first, since
# that is the most audible thing a household would notice if this
# rollback itself half-fails):
#   1. Restore the CamillaDSP statefile's config_path from the recorded
#      rollback state (or leave it alone if no rollback state was ever
#      recorded — nothing to restore, not an error), then restart
#      jasper-camilla.
#   2. Strip the marked block from /var/lib/jasper/outputd.env (or
#      no-op if absent), then restart jasper-outputd.
#   3. Remove /etc/alsa/conf.d/98-jts-ring-proto.conf (or no-op if
#      absent).
#   4. Remove /dev/shm/jts-ring/ (tmpfs — safe, it is recreated by
#      whichever side opens next; removing it here guarantees no stale
#      ring geometry survives into a later arm attempt).
#   5. Remove the rollback state file itself — but ONLY if step 1
#      confirmed the statefile is back at its original config_path (or
#      there was nothing recorded to restore). If the restore failed,
#      the record is preserved so a re-run can still find the original.
#   6. With --purge: also remove the installed .so and the bench
#      working directory (the two artifacts build-on-pi.sh created).
#      Without --purge, those are left in place so a re-arm does not
#      need a rebuild.
#
# Final verify: confirms jasper-outputd and jasper-camilla are both
# active and that outputd's content source is back to "alsa" (not
# "shm_ring" or "local_pipe") via its journal.
#
# Usage:
#   bash scripts/ring-proto/disarm.sh
#   bash scripts/ring-proto/disarm.sh --purge
#   PI_HOST=jts3.local bash scripts/ring-proto/disarm.sh

set -uo pipefail
# Not `set -e`, same rationale as arm.sh: this script's job is to make a
# best-effort pass through every rollback step even if an earlier step
# hits something unexpected, rather than stopping partway through a
# rollback because one step errored.

# Captured BEFORE sourcing _lib.sh — see _guard.sh for why. When arm.sh
# invokes this script internally as its own rollback path, PI_HOST is
# already exported by arm.sh's own (already-validated) resolution, so
# this capture sees it as non-empty and the guard passes without a
# second prompt — correct, since arm.sh already validated the target
# once. A standalone `bash disarm.sh` with no PI_HOST set still refuses.
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

PURGE=0
RING_MODE="ring_b"
for arg in "$@"; do
    case "${arg}" in
        --purge) PURGE=1 ;;
        --ring-a) RING_MODE="ring_a" ;;
        --ring-b) RING_MODE="ring_b" ;;
        *)
            echo "usage: $(basename "$0") [--ring-a|--ring-b] [--purge]" >&2
            exit 1
            ;;
    esac
done

# Ring B (default): CamillaDSP writes the ring -> the writer daemon flipped is
# jasper-outputd (JASPER_OUTPUTD_CONTENT_BRIDGE in outputd.env).
# Ring A (--ring-a): jasper-fanin writes the ring -> the daemon flipped is
# jasper-fanin (JASPER_FANIN_CAMILLA_COUPLING in fanin.env). Both share the
# statefile-restore + camilla-restart shape; only the env file, marker, ring
# path, conf.d file, and the "other" daemon differ.
ROLLBACK_STATE_DIR="/var/lib/jasper/ring-proto"
REMOTE_WORK_DIR="${JASPER_RING_PROTO_REMOTE_DIR:-/home/${PI_USER}/jts-ring-proto}"
ALSA_PLUGIN_DIR="/usr/lib/aarch64-linux-gnu/alsa-lib"

if [[ "${RING_MODE}" == "ring_a" ]]; then
    CONF_D_PATH="/etc/alsa/conf.d/98-jts-ring-a-proto.conf"
    OTHER_ENV="/var/lib/jasper/fanin.env"           # holds the coupling marked block
    OTHER_UNIT="jasper-fanin"
    RING_PATH="${JASPER_RING_PROTO_RING_PATH:-/dev/shm/jts-ring/program.ring}"
    ROLLBACK_ENV="${ROLLBACK_STATE_DIR}/rollback-a.env"
    BEGIN_MARKER="# BEGIN jts-ring-a-proto (scripts/ring-proto/arm-ring-a.sh)"
    END_MARKER="# END jts-ring-a-proto"
    OTHER_SOURCE_TOKEN="JASPER_FANIN_CAMILLA_COUPLING"
    RING_LABEL="Ring A"
else
    CONF_D_PATH="/etc/alsa/conf.d/98-jts-ring-proto.conf"
    OTHER_ENV="/var/lib/jasper/outputd.env"         # holds the content-bridge marked block
    OTHER_UNIT="jasper-outputd"
    RING_PATH="/dev/shm/jts-ring"                    # Ring B removes the whole dir (see step 4)
    ROLLBACK_ENV="${ROLLBACK_STATE_DIR}/rollback.env"
    BEGIN_MARKER="# BEGIN jts-ring-proto (scripts/ring-proto/arm.sh)"
    END_MARKER="# END jts-ring-proto"
    OTHER_SOURCE_TOKEN="JASPER_OUTPUTD_CONTENT_BRIDGE"
    RING_LABEL="Ring B"
fi

echo "=== ${RING_LABEL} prototype: DISARM on ${PI_USER}@${PI_HOST} ==="
[[ "${PURGE}" -eq 1 ]] && echo "  (--purge: also removing the built ioplug .so and bench working dir)"
echo ""

if ! ssh -o BatchMode=yes -o ConnectTimeout=8 "${PI_USER}@${PI_HOST}" true; then
    echo "error: cannot reach ${PI_USER}@${PI_HOST} over SSH (BatchMode)." >&2
    exit 1
fi
ssh_ok() { ssh -o BatchMode=yes -o ConnectTimeout=8 "${PI_USER}@${PI_HOST}" "$@"; }

overall_ok=1
# Whether the CamillaDSP statefile is known to be back at its original
# config_path — gates step 5's deletion of the rollback record. Only the
# two SAFE outcomes set this to 1: (a) restore succeeded, or (b) there was
# no rollback state to begin with (nothing recorded to lose). A present but
# empty/unreadable record, or a failed restore, leaves it 0 so step 5
# preserves the only copy of the original config_path.
statefile_restore_ok=0

# ---------------------------------------------------------------------
# Step 1 — restore the CamillaDSP statefile + restart jasper-camilla
# ---------------------------------------------------------------------
echo "--- Step 1/6: restore CamillaDSP statefile ---"
rollback_present="$(ssh_ok "test -f ${ROLLBACK_ENV} && echo yes || echo no")"
if [[ "${rollback_present}" == "yes" ]]; then
    original_config_path="$(ssh_ok "sed -n 's/^ORIGINAL_CAMILLA_CONFIG_PATH=//p' ${ROLLBACK_ENV} | head -1")"
    if [[ -z "${original_config_path}" ]]; then
        echo "  WARNING: ${ROLLBACK_ENV} exists but has no" \
            "ORIGINAL_CAMILLA_CONFIG_PATH — leaving the statefile as-is." >&2
        overall_ok=0
        # statefile_restore_ok stays 0: the record is broken but present;
        # do NOT delete it in step 5 (it is all we have of the original).
    else
        statefile_block=$(cat <<PYEOF
import sys
sys.path.insert(0, "/opt/jasper")
from jasper.active_speaker.runtime_contract import write_camilla_statefile
write_camilla_statefile(
    "/var/lib/camilladsp/outputd-statefile.yml",
    "${original_config_path}",
)
print("statefile restored: config_path=${original_config_path}")
PYEOF
)
        if printf '%s\n' "${statefile_block}" | ssh_ok "sudo /opt/jasper/.venv/bin/python"; then
            echo "  OK   statefile restored to ${original_config_path}"
            statefile_restore_ok=1
        else
            echo "  ERROR: could not restore the statefile to ${original_config_path}." \
                "The box may still be pointed at the ring config — check by hand:" \
                "ssh ${PI_USER}@${PI_HOST} cat /var/lib/camilladsp/outputd-statefile.yml" >&2
            overall_ok=0
            # statefile_restore_ok stays 0: keep the record so a later
            # disarm re-run can still find the original config_path.
        fi
    fi
else
    echo "  SKIP no rollback state recorded (${ROLLBACK_ENV} absent) —" \
        "nothing to restore. This is normal if arm.sh never got past its" \
        "own preflight, or if disarm.sh already ran once."
    # Nothing recorded, so nothing to lose by cleaning up in step 5.
    statefile_restore_ok=1
fi

if ssh_ok "systemctl is-active --quiet jasper-camilla"; then
    if ssh_ok "sudo systemctl restart jasper-camilla"; then
        sleep 2
        camilla_active="$(ssh_ok 'systemctl is-active jasper-camilla' 2>/dev/null)"
        if [[ "${camilla_active}" == "active" ]]; then
            echo "  OK   jasper-camilla restarted and active"
        else
            echo "  ERROR: jasper-camilla is ${camilla_active} after restart — check by hand:" \
                "ssh ${PI_USER}@${PI_HOST} journalctl -u jasper-camilla -n 40" >&2
            overall_ok=0
        fi
    else
        echo "  ERROR: systemctl restart jasper-camilla failed." >&2
        overall_ok=0
    fi
else
    echo "  SKIP jasper-camilla is not currently active — not restarting it" \
        "(disarm.sh does not start services that were not already running)."
fi
echo ""

# ---------------------------------------------------------------------
# Step 2 — strip the writer daemon's env marked block + restart it
#   Ring B: outputd.env (JASPER_OUTPUTD_CONTENT_BRIDGE) + jasper-outputd
#   Ring A: fanin.env  (JASPER_FANIN_CAMILLA_COUPLING) + jasper-fanin
# ---------------------------------------------------------------------
echo "--- Step 2/6: strip ${OTHER_ENV} marked block ---"
has_block="$(ssh_ok "test -f ${OTHER_ENV} && grep -qF '${BEGIN_MARKER}' ${OTHER_ENV} && echo yes || echo no")"
if [[ "${has_block}" == "yes" ]]; then
    # Delete the inclusive BEGIN..END range in place. sed -i without a
    # backup suffix is GNU sed's in-place syntax, which Raspberry Pi OS
    # ships (util-linux/coreutils toolchain, not BSD sed).
    if ssh_ok "sudo sed -i '/^${BEGIN_MARKER//\//\\/}\$/,/^${END_MARKER//\//\\/}\$/d' ${OTHER_ENV}"; then
        echo "  OK   removed the marked block from ${OTHER_ENV}"
    else
        echo "  ERROR: sed failed to strip the marked block from ${OTHER_ENV} —" \
            "inspect and clean up by hand: ssh ${PI_USER}@${PI_HOST} cat ${OTHER_ENV}" >&2
        overall_ok=0
    fi
else
    echo "  SKIP no marked block found in ${OTHER_ENV}" \
        "(absent file, or block already removed)."
fi

if ssh_ok "systemctl is-active --quiet ${OTHER_UNIT}"; then
    # reset-failed clears any start-limit state so this restart is not refused.
    if ssh_ok "sudo systemctl reset-failed ${OTHER_UNIT} 2>/dev/null; sudo systemctl restart ${OTHER_UNIT}"; then
        sleep 2
        other_active="$(ssh_ok "systemctl is-active ${OTHER_UNIT}" 2>/dev/null)"
        if [[ "${other_active}" == "active" ]]; then
            # Confirm the daemon's resolved runtime no longer carries the ring
            # token: read /proc/<MainPID>/environ (the layered EnvironmentFile
            # surface, not `systemctl show`). If the token still resolves to a
            # ring value the strip did not take — warn.
            other_pid="$(ssh_ok "systemctl show ${OTHER_UNIT}.service -p MainPID --value" 2>/dev/null | tr -dc '0-9')"
            token_value=""
            if [[ -n "${other_pid}" && "${other_pid}" != "0" ]]; then
                token_value="$(ssh_ok "sudo sh -c 'tr \"\\0\" \"\\n\" < /proc/${other_pid}/environ'" 2>/dev/null | sed -n "s/^${OTHER_SOURCE_TOKEN}=//p" | head -1)"
            fi
            echo "  OK   ${OTHER_UNIT} restarted and active (${OTHER_SOURCE_TOKEN}=${token_value:-<unset/default>})"
            case "${token_value}" in
                shm_ring)
                    echo "  WARNING: ${OTHER_UNIT} still resolves ${OTHER_SOURCE_TOKEN}=shm_ring" \
                        "after disarm — the ${OTHER_ENV} strip may not have taken effect." \
                        "Check by hand: ssh ${PI_USER}@${PI_HOST} cat ${OTHER_ENV}" >&2
                    overall_ok=0
                    ;;
            esac
        else
            echo "  ERROR: ${OTHER_UNIT} is ${other_active} after restart — check by hand:" \
                "ssh ${PI_USER}@${PI_HOST} journalctl -u ${OTHER_UNIT} -n 40" >&2
            overall_ok=0
        fi
    else
        echo "  ERROR: systemctl restart ${OTHER_UNIT} failed." >&2
        overall_ok=0
    fi
else
    echo "  SKIP ${OTHER_UNIT} is not currently active — not restarting it."
fi
echo ""

# ---------------------------------------------------------------------
# Step 3 — remove the ALSA plugin registration drop-in
# ---------------------------------------------------------------------
echo "--- Step 3/6: remove ${CONF_D_PATH} ---"
if ssh_ok "test -f ${CONF_D_PATH}"; then
    if ssh_ok "sudo rm -f ${CONF_D_PATH}"; then
        echo "  OK   removed ${CONF_D_PATH}"
    else
        echo "  ERROR: could not remove ${CONF_D_PATH}." >&2
        overall_ok=0
    fi
else
    echo "  SKIP ${CONF_D_PATH} does not exist."
fi
echo ""

# ---------------------------------------------------------------------
# Step 4 — remove the SHM ring (tmpfs; safe, self-heals)
#   Ring B: removes the whole /dev/shm/jts-ring dir (content.ring is the only
#     tenant of the dir in a Ring-B-only arm).
#   Ring A: removes ONLY the program.ring FILE, never the dir — Ring B's
#     content.ring may share /dev/shm/jts-ring, and blowing away the dir would
#     take an armed Ring B down with it. Removing just the file guarantees no
#     stale ring geometry survives into a later Ring A arm.
# ---------------------------------------------------------------------
echo "--- Step 4/6: remove ${RING_PATH} (${RING_LABEL}) ---"
if [[ "${RING_MODE}" == "ring_a" ]]; then
    if ssh_ok "test -e ${RING_PATH}"; then
        if ssh_ok "sudo rm -f ${RING_PATH}"; then
            echo "  OK   removed ${RING_PATH} (tmpfs file — recreated on next arm-ring-a.sh;" \
                "the /dev/shm/jts-ring dir is left alone so an armed Ring B is untouched)"
        else
            echo "  ERROR: could not remove ${RING_PATH}." >&2
            overall_ok=0
        fi
    else
        echo "  SKIP ${RING_PATH} does not exist."
    fi
else
    if ssh_ok "test -e ${RING_PATH}"; then
        if ssh_ok "sudo rm -rf ${RING_PATH}"; then
            echo "  OK   removed ${RING_PATH} (tmpfs — will be recreated on next" \
                "arm.sh run, or ignored entirely while the flag is off)"
        else
            echo "  ERROR: could not remove ${RING_PATH}." >&2
            overall_ok=0
        fi
    else
        echo "  SKIP ${RING_PATH} does not exist."
    fi
fi
echo ""

# ---------------------------------------------------------------------
# Step 5 — remove THIS mode's rollback record (never the sibling's)
# ---------------------------------------------------------------------
# ONLY when the statefile is known to be restored (or there was nothing to
# restore). If step 1's restore failed, the rollback record is the sole
# surviving copy of the original config_path while the statefile still
# points at ring_proto.yml — deleting it here would strand the box and make
# a later re-arm record ring_proto.yml as the "original". Keep it.
#
# CROSS-MODE SAFETY (combo box): the rollback state DIR (${ROLLBACK_STATE_DIR})
# is SHARED between Ring A (rollback-a.env) and Ring B (rollback.env). A combo
# box can have BOTH armed at once. So this step removes ONLY this mode's own
# record file (${ROLLBACK_ENV}) — never `rm -rf` the whole dir, which would
# destroy the sibling direction's rollback state and strand the still-armed
# other ring at a later disarm. The shared dir is rmdir'd ONLY when it is empty
# (the sibling record is absent), so a Ring-A-only or Ring-B-only box still
# cleans up fully, while a combo box keeps the sibling's record intact.
echo "--- Step 5/6: remove ${RING_LABEL} rollback record (${ROLLBACK_ENV}) ---"
if [[ "${statefile_restore_ok}" -ne 1 ]]; then
    echo "  SKIP preserving ${ROLLBACK_ENV}: the statefile was NOT confirmed" \
        "restored in step 1, so this record (the original config_path) must" \
        "survive for a later disarm re-run. Fix the statefile restore, then" \
        "re-run disarm.sh to clean up." >&2
elif ssh_ok "test -f ${ROLLBACK_ENV}"; then
    if ssh_ok "sudo rm -f ${ROLLBACK_ENV}"; then
        echo "  OK   removed ${ROLLBACK_ENV} (this mode's record only)"
        # rmdir the shared dir ONLY if now empty — preserves a sibling mode's
        # record on a combo box. `rmdir` fails (harmlessly) on a non-empty dir.
        if ssh_ok "sudo rmdir ${ROLLBACK_STATE_DIR} 2>/dev/null"; then
            echo "  OK   removed now-empty ${ROLLBACK_STATE_DIR}"
        else
            echo "  KEEP ${ROLLBACK_STATE_DIR} not empty (a sibling ring's rollback" \
                "record is still present — leaving the shared dir for it)."
        fi
    else
        echo "  ERROR: could not remove ${ROLLBACK_ENV}." >&2
        overall_ok=0
    fi
else
    echo "  SKIP ${ROLLBACK_ENV} does not exist."
    # Still attempt to clean up an orphaned empty shared dir (no records at all).
    ssh_ok "sudo rmdir ${ROLLBACK_STATE_DIR} 2>/dev/null" && \
        echo "  OK   removed now-empty ${ROLLBACK_STATE_DIR}" || true
fi
echo ""

# ---------------------------------------------------------------------
# Step 6 — optional --purge: remove build artifacts
# ---------------------------------------------------------------------
echo "--- Step 6/6: build artifacts ---"
if [[ "${PURGE}" -eq 1 ]]; then
    if ssh_ok "test -f ${ALSA_PLUGIN_DIR}/libasound_module_pcm_jts_ring.so"; then
        if ssh_ok "sudo rm -f ${ALSA_PLUGIN_DIR}/libasound_module_pcm_jts_ring.so"; then
            echo "  OK   removed ${ALSA_PLUGIN_DIR}/libasound_module_pcm_jts_ring.so"
        else
            echo "  ERROR: could not remove the installed .so." >&2
            overall_ok=0
        fi
    else
        echo "  SKIP ioplug .so was not installed."
    fi
    if ssh_ok "test -d ${REMOTE_WORK_DIR}"; then
        if ssh_ok "rm -rf ${REMOTE_WORK_DIR}"; then
            echo "  OK   removed ${REMOTE_WORK_DIR} (bench binary + build scratch)"
        else
            echo "  ERROR: could not remove ${REMOTE_WORK_DIR}." >&2
            overall_ok=0
        fi
    else
        echo "  SKIP ${REMOTE_WORK_DIR} does not exist."
    fi
else
    echo "  SKIP not requested (pass --purge to also remove the built .so and" \
        "bench working directory — left in place so a re-arm does not need a" \
        "rebuild)."
fi
echo ""

# ---------------------------------------------------------------------
# Final verify
# ---------------------------------------------------------------------
echo "--- Final verify ---"
final_outputd="$(ssh_ok 'systemctl is-active jasper-outputd' 2>/dev/null || echo unknown)"
final_camilla="$(ssh_ok 'systemctl is-active jasper-camilla' 2>/dev/null || echo unknown)"
echo "  jasper-outputd: ${final_outputd}"
echo "  jasper-camilla: ${final_camilla}"

if [[ "${overall_ok}" -eq 1 ]]; then
    echo ""
    echo "=== Disarm complete. Box restored to its pre-arm state. ==="
    exit 0
else
    echo ""
    echo "=== Disarm finished with WARNINGS/ERRORS above — verify the box by hand" \
        "before assuming it is fully back to its original state. ===" >&2
    exit 1
fi
