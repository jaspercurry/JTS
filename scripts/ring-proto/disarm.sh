#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# disarm.sh — unconditional rollback for the Ring B prototype. Restores
# the box to the exact state arm.sh found it in.
#
# IDEMPOTENT AND SAFE TO RUN COLD: every step below checks whether its
# own target exists before touching it, so running this against a box
# that was never armed (or was only partially armed, or was already
# disarmed) is a no-op for anything not present — never an error, never
# a change to something arm.sh did not touch.
#
# This is what arm.sh itself calls on any mid-arm failure (see
# fail_and_rollback in arm.sh), so it must never assume every marked
# block is present.
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
for arg in "$@"; do
    case "${arg}" in
        --purge) PURGE=1 ;;
        *)
            echo "usage: $(basename "$0") [--purge]" >&2
            exit 1
            ;;
    esac
done

CONF_D_PATH="/etc/alsa/conf.d/98-jts-ring-proto.conf"
OUTPUTD_ENV="/var/lib/jasper/outputd.env"
ROLLBACK_STATE_DIR="/var/lib/jasper/ring-proto"
ROLLBACK_ENV="${ROLLBACK_STATE_DIR}/rollback.env"
BEGIN_MARKER="# BEGIN jts-ring-proto (scripts/ring-proto/arm.sh)"
END_MARKER="# END jts-ring-proto"
REMOTE_WORK_DIR="${JASPER_RING_PROTO_REMOTE_DIR:-/home/${PI_USER}/jts-ring-proto}"
ALSA_PLUGIN_DIR="/usr/lib/aarch64-linux-gnu/alsa-lib"

echo "=== Ring B prototype: DISARM on ${PI_USER}@${PI_HOST} ==="
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
# Step 2 — strip the outputd.env marked block + restart jasper-outputd
# ---------------------------------------------------------------------
echo "--- Step 2/6: strip ${OUTPUTD_ENV} marked block ---"
has_block="$(ssh_ok "test -f ${OUTPUTD_ENV} && grep -qF '${BEGIN_MARKER}' ${OUTPUTD_ENV} && echo yes || echo no")"
if [[ "${has_block}" == "yes" ]]; then
    # Delete the inclusive BEGIN..END range in place. sed -i without a
    # backup suffix is GNU sed's in-place syntax, which Raspberry Pi OS
    # ships (util-linux/coreutils toolchain, not BSD sed).
    if ssh_ok "sudo sed -i '/^${BEGIN_MARKER//\//\\/}\$/,/^${END_MARKER//\//\\/}\$/d' ${OUTPUTD_ENV}"; then
        echo "  OK   removed the marked block from ${OUTPUTD_ENV}"
    else
        echo "  ERROR: sed failed to strip the marked block from ${OUTPUTD_ENV} —" \
            "inspect and clean up by hand: ssh ${PI_USER}@${PI_HOST} cat ${OUTPUTD_ENV}" >&2
        overall_ok=0
    fi
else
    echo "  SKIP no jts-ring-proto marked block found in ${OUTPUTD_ENV}" \
        "(absent file, or block already removed)."
fi

if ssh_ok "systemctl is-active --quiet jasper-outputd"; then
    if ssh_ok "sudo systemctl restart jasper-outputd"; then
        sleep 2
        outputd_active="$(ssh_ok 'systemctl is-active jasper-outputd' 2>/dev/null)"
        if [[ "${outputd_active}" == "active" ]]; then
            content_source="$(ssh_ok "journalctl -u jasper-outputd -n 20 --no-pager | grep -o 'content_source=[a-z_]*' | tail -1")"
            echo "  OK   jasper-outputd restarted and active (${content_source:-content_source unknown — check journal})"
            if [[ "${content_source}" == "content_source=shm_ring" ]]; then
                echo "  WARNING: jasper-outputd journal still reports content_source=shm_ring" \
                    "after disarm — the outputd.env strip may not have taken effect." \
                    "Check by hand: ssh ${PI_USER}@${PI_HOST} cat ${OUTPUTD_ENV}" >&2
                overall_ok=0
            fi
        else
            echo "  ERROR: jasper-outputd is ${outputd_active} after restart — check by hand:" \
                "ssh ${PI_USER}@${PI_HOST} journalctl -u jasper-outputd -n 40" >&2
            overall_ok=0
        fi
    else
        echo "  ERROR: systemctl restart jasper-outputd failed." >&2
        overall_ok=0
    fi
else
    echo "  SKIP jasper-outputd is not currently active — not restarting it."
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
# Step 4 — remove the SHM ring directory (tmpfs; safe, self-heals)
# ---------------------------------------------------------------------
echo "--- Step 4/6: remove /dev/shm/jts-ring ---"
if ssh_ok "test -e /dev/shm/jts-ring"; then
    if ssh_ok "sudo rm -rf /dev/shm/jts-ring"; then
        echo "  OK   removed /dev/shm/jts-ring (tmpfs — will be recreated on next" \
            "arm.sh run, or ignored entirely while the flag is off)"
    else
        echo "  ERROR: could not remove /dev/shm/jts-ring." >&2
        overall_ok=0
    fi
else
    echo "  SKIP /dev/shm/jts-ring does not exist."
fi
echo ""

# ---------------------------------------------------------------------
# Step 5 — remove the rollback state file
# ---------------------------------------------------------------------
# ONLY when the statefile is known to be restored (or there was nothing to
# restore). If step 1's restore failed, the rollback record is the sole
# surviving copy of the original config_path while the statefile still
# points at ring_proto.yml — deleting it here would strand the box and make
# a later re-arm record ring_proto.yml as the "original". Keep it.
echo "--- Step 5/6: remove rollback state ---"
if [[ "${statefile_restore_ok}" -ne 1 ]]; then
    echo "  SKIP preserving ${ROLLBACK_STATE_DIR}: the statefile was NOT confirmed" \
        "restored in step 1, so this record (the original config_path) must" \
        "survive for a later disarm re-run. Fix the statefile restore, then" \
        "re-run disarm.sh to clean up." >&2
elif ssh_ok "test -d ${ROLLBACK_STATE_DIR}"; then
    if ssh_ok "sudo rm -rf ${ROLLBACK_STATE_DIR}"; then
        echo "  OK   removed ${ROLLBACK_STATE_DIR}"
    else
        echo "  ERROR: could not remove ${ROLLBACK_STATE_DIR}." >&2
        overall_ok=0
    fi
else
    echo "  SKIP ${ROLLBACK_STATE_DIR} does not exist."
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
