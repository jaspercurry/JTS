#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# arm.sh — wire Ring B (CamillaDSP -> outputd content, via SHM ping-pong
# ring) into the live audio chain on a lab Pi.
#
# THIS IS A PROTOTYPE, LAB-ONLY PROCEDURE. It never touches product
# Camilla emitters, reconcilers, /sound wizard, multiroom, or install.sh
# — everything it writes is a marked block this script can find and
# strip again, and 'disarm.sh' is the unconditional rollback. If ANY
# step below fails, this script calls disarm.sh itself before exiting
# non-zero, so a failed arm never leaves the box half-wired.
#
# Preconditions checked (refuses to proceed, no changes made, if any
# fail):
#   - libasound_module_pcm_jts_ring.so is already built and installed
#     (run build-on-pi.sh first).
#   - the box resolves outputd's env to a full-range stereo L/R sink
#     (JASPER_OUTPUTD_SINK=single_alsa, JASPER_OUTPUTD_ACTIVE_CHANNELS
#     unset-or-2, JASPER_OUTPUTD_ACTIVE_LANE unset) — the same predicate
#     outputd itself enforces at startup for any content-bridge mode
#     (rust/jasper-outputd/src/config.rs is_full_range_stereo_lr_sink).
#     This is belt-and-suspenders: a rejection here is a clean, fast,
#     no-side-effect refusal; a rejection inside outputd after this
#     script has already restarted it would be a park (exit 78) that
#     this script would then have to notice and roll back anyway.
#   - outputd is not already running a mutually-exclusive content
#     source (local_content_pipe, dac_content round-trip FIFO, or an
#     already-armed rate_match bridge).
#   - the multi-room round-trip lane is not active
#     (JASPER_OUTPUTD_DAC_CONTENT_FIFO unset) — a bonded/grouping
#     speaker is out of scope for this prototype.
#
# Steps, in order (each is individually observable; see the printed
# "verify:" line after each one):
#   1. Preflight (read-only over SSH, BatchMode).
#   2. Install the /etc/alsa/conf.d/98-jts-ring-proto.conf drop-in
#      (system-wide ALSA plugin registration — mode 0644, resolvable by
#      any renderer user, mirrors the renderer-device-resolvability rule
#      in AGENTS.md).
#   3. Resolvability probe: `aplay -D jts_ring_playback ... /dev/zero`.
#      Terminates via the writer's free-run-drop-when-no-reader path
#      (outputd's reader isn't armed yet at this point).
#   4. Append the marked block to /var/lib/jasper/outputd.env
#      (JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring [+ optional slots
#      override]) and restart jasper-outputd. Verify /run/jasper-outputd
#      journal shows the ring opened and (if /state.shm_ring exists yet)
#      that occupancy tracking is live.
#   5. Run the bench writer for a few seconds — proves the reader path
#      end-to-end WITHOUT CamillaDSP in the loop yet.
#   6. Build the hand Camilla ring config (make-camilla-ring-config.sh)
#      and point the statefile at it; restart jasper-camilla.
#   7. Final verify: play something through the real chain, print the
#      occupancy/empty-read counters, and print the exact route-latency
#      harness re-measure command (this script does not run the harness
#      itself — see README.md for why, and for the jts3 vs jts.local
#      sequencing note).
#
# Usage:
#   bash scripts/ring-proto/arm.sh
#   PI_HOST=jts3.local bash scripts/ring-proto/arm.sh
#   JASPER_RING_PROTO_SLOTS=3 bash scripts/ring-proto/arm.sh   # degraded widening

set -uo pipefail
# Deliberately NOT `set -e` at the top level: this script's whole shape is
# "run a step, check its own exit code, and roll everything back on any
# failure" — `set -e` would abort mid-cleanup on the very failure path
# we're trying to handle gracefully. Every step below checks $? itself.

# Captured BEFORE sourcing _lib.sh: _lib.sh's own fallback
# (PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}") silently resolves
# to jts.local if unset. This capture is what lets _guard.sh tell "the
# caller explicitly set PI_HOST" apart from "PI_HOST is only the ambient
# default" — see _guard.sh for why that distinction is safety-critical
# for this script family.
export JASPER_RING_PROTO_CALLER_PI_HOST="${PI_HOST:-}"

# NOTE: named RING_PROTO_DIR, not SCRIPT_DIR — _lib.sh (sourced below)
# defines its own SCRIPT_DIR pointing at scripts/ (its own location), and
# `source`/`.` runs in the same shell scope, so sourcing it after setting
# a same-named variable silently clobbers ours. Every existing caller of
# _lib.sh lives directly in scripts/, where that collision is harmless
# (both computations produce the same path); this script is the first
# caller one directory deeper (scripts/ring-proto/), where the collision
# would silently point later "${SCRIPT_DIR}/foo.sh" references at
# scripts/foo.sh instead of scripts/ring-proto/foo.sh. Caught by hand
# during this script's development (arm.sh failed to find
# make-camilla-ring-config.sh/disarm.sh with exactly that wrong path).
RING_PROTO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${RING_PROTO_DIR}/../.." && pwd)"
# shellcheck source=../_lib.sh
. "${REPO_ROOT}/scripts/_lib.sh"
# shellcheck source=./_guard.sh
. "${RING_PROTO_DIR}/_guard.sh"
require_explicit_ring_proto_target

RING_DEVICE="${JASPER_RING_PROTO_ALSA_DEVICE:-jts_ring_playback}"
RING_SLOTS="${JASPER_RING_PROTO_SLOTS:-2}"
CONF_D_PATH="/etc/alsa/conf.d/98-jts-ring-proto.conf"
OUTPUTD_ENV="/var/lib/jasper/outputd.env"
ROLLBACK_STATE_DIR="/var/lib/jasper/ring-proto"
ROLLBACK_ENV="${ROLLBACK_STATE_DIR}/rollback.env"
BEGIN_MARKER="# BEGIN jts-ring-proto (scripts/ring-proto/arm.sh)"
END_MARKER="# END jts-ring-proto"

fail_and_rollback() {
    echo "" >&2
    echo "!!! arm.sh FAILED at: $* — rolling back via disarm.sh !!!" >&2
    echo "" >&2
    bash "${RING_PROTO_DIR}/disarm.sh" || echo "warning: disarm.sh itself reported a problem — inspect ${PI_USER}@${PI_HOST} by hand" >&2
    exit 1
}

ssh_ok() { ssh -o BatchMode=yes -o ConnectTimeout=8 "${PI_USER}@${PI_HOST}" "$@"; }

echo "=== Ring B prototype: ARM on ${PI_USER}@${PI_HOST} ==="
echo "  ring device:  ${RING_DEVICE}"
echo "  ring slots:   ${RING_SLOTS}"
echo ""

# ---------------------------------------------------------------------
# Step 1 — preflight (read-only, no changes made on any failure here)
# ---------------------------------------------------------------------
echo "--- Step 1/7: preflight ---"

if ! ssh_ok true; then
    echo "error: cannot reach ${PI_USER}@${PI_HOST} over SSH (BatchMode)." >&2
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

# Resolve outputd's ACTUAL runtime env (systemctl show already merges
# Environment= directives with every EnvironmentFile= layer, honoring
# the optional-file "-" prefix) rather than grepping the packaged unit
# file, which would see the packaged JASPER_OUTPUTD_PERIOD_FRAMES=1024
# default instead of a live box's tuned override.
outputd_env_raw="$(ssh_ok 'systemctl show jasper-outputd.service -p Environment --value' 2>/dev/null)"
if [[ -z "${outputd_env_raw}" ]]; then
    echo "error: could not read jasper-outputd.service's resolved environment (is the unit installed?)." >&2
    echo "No changes made." >&2
    exit 1
fi

resolved_env_get() {
    # resolved_env_get <KEY> — extract KEY=value from the systemctl show
    # --value output, which is a single space-separated, shell-quoted line.
    local key="$1"
    python3 -c "
import shlex, sys
key = sys.argv[1]
line = sys.argv[2]
for tok in shlex.split(line):
    if '=' in tok:
        k, _, v = tok.partition('=')
        if k == key:
            print(v)
            break
" "${key}" "${outputd_env_raw}"
}

sink="$(resolved_env_get JASPER_OUTPUTD_SINK)"
active_channels="$(resolved_env_get JASPER_OUTPUTD_ACTIVE_CHANNELS)"
active_lane="$(resolved_env_get JASPER_OUTPUTD_ACTIVE_LANE)"
dac_content_fifo="$(resolved_env_get JASPER_OUTPUTD_DAC_CONTENT_FIFO)"
local_content_pipe="$(resolved_env_get JASPER_OUTPUTD_LOCAL_CONTENT_PIPE)"
existing_bridge="$(resolved_env_get JASPER_OUTPUTD_CONTENT_BRIDGE)"

sink="${sink:-single_alsa}"
active_channels="${active_channels:-2}"

if [[ "${sink}" != "single_alsa" && "${sink}" != "single" && "${sink}" != "alsa" ]]; then
    echo "error: JASPER_OUTPUTD_SINK=${sink} on ${PI_HOST} — Ring B requires a" \
        "single-ALSA full-range stereo sink (this box is composite/dual-DAC)." >&2
    echo "No changes made." >&2
    exit 1
fi
if [[ "${active_channels}" != "2" ]]; then
    echo "error: JASPER_OUTPUTD_ACTIVE_CHANNELS=${active_channels} on ${PI_HOST} —" \
        "Ring B requires exactly 2 channels (this box looks like an active-crossover" \
        "or wide-channel topology)." >&2
    echo "No changes made." >&2
    exit 1
fi
if [[ -n "${active_lane}" && "${active_lane}" != "0" && "${active_lane}" != "false" ]]; then
    echo "error: JASPER_OUTPUTD_ACTIVE_LANE is set on ${PI_HOST} — this is an" \
        "active-crossover lane (2ch does not distinguish it from stereo; the" \
        "explicit marker does). Ring B refuses to arm here: it would feed" \
        "full-range audio straight to what may be a tweeter." >&2
    echo "No changes made." >&2
    exit 1
fi
if [[ -n "${dac_content_fifo}" ]]; then
    echo "error: JASPER_OUTPUTD_DAC_CONTENT_FIFO is set on ${PI_HOST} — this box" \
        "is bonded into a multi-room group. Ring B is out of scope for grouped" \
        "speakers; disband the group (or pick a non-grouped lab box) first." >&2
    echo "No changes made." >&2
    exit 1
fi
if [[ -n "${local_content_pipe}" ]]; then
    echo "error: JASPER_OUTPUTD_LOCAL_CONTENT_PIPE is set on ${PI_HOST} — that" \
        "content source is mutually exclusive with the SHM ring. Unset it" \
        "first (or pick a different lab box)." >&2
    echo "No changes made." >&2
    exit 1
fi
if [[ -n "${existing_bridge}" && "${existing_bridge}" != "direct" && "${existing_bridge}" != "off" && "${existing_bridge}" != "disabled" ]]; then
    echo "error: JASPER_OUTPUTD_CONTENT_BRIDGE=${existing_bridge} is already set" \
        "on ${PI_HOST} — arm.sh only appends on top of the packaged 'direct'" \
        "default. Disarm/clear it first." >&2
    echo "No changes made." >&2
    exit 1
fi
echo "  OK   sink=${sink} active_channels=${active_channels} active_lane=${active_lane:-unset}" \
    "dac_content_fifo=${dac_content_fifo:-unset} content_bridge=${existing_bridge:-direct}"

if [[ ! "${RING_SLOTS}" =~ ^[0-9]+$ ]] || (( RING_SLOTS < 2 || RING_SLOTS > 4 )); then
    echo "error: JASPER_RING_PROTO_SLOTS=${RING_SLOTS} must be an integer 2..4" \
        "(2 = prototype ping-pong, 3 = degraded widening, 4 = negotiation headroom)." >&2
    exit 1
fi

if ssh_ok "test -f ${CONF_D_PATH}"; then
    echo "error: ${CONF_D_PATH} already exists on ${PI_HOST} — looks already armed." \
        "Run disarm.sh first if you want to re-arm." >&2
    echo "No changes made." >&2
    exit 1
fi

# Record rollback state BEFORE any mutation: the currently-live statefile
# config_path, so disarm.sh can restore it verbatim even if this run is
# interrupted partway through step 6. Strip quotes LOCALLY (bash parameter
# expansion) rather than fighting nested remote-shell quoting for a `tr -d`.
current_config_path_raw="$(ssh_ok "sed -n 's/^[[:space:]]*config_path:[[:space:]]*//p' /var/lib/camilladsp/outputd-statefile.yml | head -1")"
current_config_path="${current_config_path_raw//[\'\"]/}"
if [[ -z "${current_config_path}" ]]; then
    echo "error: could not read the current config_path from" \
        "/var/lib/camilladsp/outputd-statefile.yml on ${PI_HOST}." >&2
    echo "No changes made." >&2
    exit 1
fi
ssh_ok "sudo install -d -m 0755 ${ROLLBACK_STATE_DIR} && printf 'ORIGINAL_CAMILLA_CONFIG_PATH=%s\n' '${current_config_path}' | sudo tee ${ROLLBACK_ENV} >/dev/null"
if [[ $? -ne 0 ]]; then
    echo "error: could not record rollback state on ${PI_HOST}." >&2
    exit 1
fi
echo "  OK   rollback state recorded: original config_path=${current_config_path}"
echo ""

# ---------------------------------------------------------------------
# Step 2 — install the ALSA plugin registration drop-in
# ---------------------------------------------------------------------
echo "--- Step 2/7: install ${CONF_D_PATH} ---"

# period_frames MUST equal outputd's resolved JASPER_OUTPUTD_PERIOD_FRAMES
# (the shared crate treats this as one less drift axis — see the SHM
# contract doc: "Slot size derives from outputd's period_frames, not a
# hardcoded 256"). Read it the same resolved-env way as the guards above.
period_frames="$(resolved_env_get JASPER_OUTPUTD_PERIOD_FRAMES)"
period_frames="${period_frames:-1024}"
echo "  period_frames (from outputd's resolved env): ${period_frames}"

# NOTE for future edits: this heredoc's delimiter is UNQUOTED (<<EOF, not
# <<'EOF') because the body needs ${RING_DEVICE}/${period_frames}/
# ${RING_SLOTS}/${BEGIN_MARKER}/${END_MARKER} interpolated. An unquoted
# heredoc body is still tokenized for quote balance even though the quotes
# are not otherwise special — an ODD number of apostrophes anywhere in the
# body (including in a comment) is a bash parse error, not just cosmetic.
# Do not add a contraction/possessive ('s, don't, etc.) to this block
# without checking `bash -n scripts/ring-proto/arm.sh` afterward.
conf_body="$(cat <<EOF
${BEGIN_MARKER}
# Ring B latency prototype (branch latency/ring-proto-shm). Registers
# pcm.${RING_DEVICE} as a writer into the SHM ping-pong ring at
# /dev/shm/jts-ring/content.ring. Lab-only: installed by arm.sh, removed
# by disarm.sh. Never shipped by install.sh.
#
# Renderer-user resolvability (AGENTS.md): this is a system-wide
# /etc/alsa/conf.d/*.conf drop-in (mode 0644), so any user — including
# the CamillaDSP runtime user — can resolve pcm.${RING_DEVICE}. Verified
# live: bluealsa/jack already register PCMs from this same directory on
# this box.
pcm.${RING_DEVICE} {
    type jts_ring
    path "/dev/shm/jts-ring/content.ring"
    period_frames ${period_frames}
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
# Step 3 — resolvability probe
# ---------------------------------------------------------------------
echo "--- Step 3/7: resolvability probe (aplay -D ${RING_DEVICE}) ---"
# At this point outputd's reader is NOT armed yet, so the writer's
# free-run-drop-when-no-reader path is what makes this terminate (see the
# SHM contract's "writer publish" step 4). A hang here would mean the
# ioplug's no-reader detection is broken, which is exactly what this
# probe exists to catch before wiring anything further.
if ! ssh_ok "sudo aplay -D ${RING_DEVICE} -c 2 -r 48000 -f S16_LE -d 1 /dev/zero" >/dev/null 2>&1; then
    fail_and_rollback "step 3 (aplay resolvability probe against ${RING_DEVICE})"
fi
echo "  OK   aplay opened ${RING_DEVICE} and completed (writer free-ran with no reader, as expected)"
echo ""

# ---------------------------------------------------------------------
# Step 4 — arm outputd's content bridge
# ---------------------------------------------------------------------
echo "--- Step 4/7: arm jasper-outputd (JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring) ---"

outputd_block="$(cat <<EOF
${BEGIN_MARKER}
JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring
JASPER_OUTPUTD_SHM_RING_SLOTS=${RING_SLOTS}
${END_MARKER}
EOF
)"

if ! printf '%s\n' "${outputd_block}" | ssh_ok "sudo mkdir -p \$(dirname ${OUTPUTD_ENV}) && cat | sudo tee -a ${OUTPUTD_ENV} >/dev/null"; then
    fail_and_rollback "step 4 (append shm_ring block to ${OUTPUTD_ENV})"
fi

if ! ssh_ok "sudo systemctl restart jasper-outputd"; then
    fail_and_rollback "step 4 (systemctl restart jasper-outputd)"
fi

# Give the unit a moment, then verify BOTH that it's active AND that its
# own startup log shows the ring opened (not just "still running" —
# outputd could restart into some other silently-broken state).
sleep 2
outputd_active="$(ssh_ok 'systemctl is-active jasper-outputd' 2>/dev/null)"
if [[ "${outputd_active}" != "active" ]]; then
    echo "  jasper-outputd status after restart: ${outputd_active}" >&2
    ssh_ok "journalctl -u jasper-outputd -n 40 --no-pager" >&2
    fail_and_rollback "step 4 (jasper-outputd did not come up active — see journal above)"
fi

ring_opened="$(ssh_ok "journalctl -u jasper-outputd -n 200 --no-pager | grep -c 'event=outputd.shm_ring.enabled'" 2>/dev/null || echo 0)"
if [[ "${ring_opened}" -lt 1 ]]; then
    echo "  jasper-outputd is active but its journal shows no" \
        "event=outputd.shm_ring.enabled line in the last 200 entries." >&2
    ssh_ok "journalctl -u jasper-outputd -n 40 --no-pager" >&2
    fail_and_rollback "step 4 (outputd active but shm_ring not confirmed opened)"
fi
echo "  OK   jasper-outputd active, journal confirms event=outputd.shm_ring.enabled"
cat <<EOF
  verify: ssh ${PI_USER}@${PI_HOST} journalctl -u jasper-outputd -n 20 \\| grep shm_ring
  verify (once /state.shm_ring lands): see the STATUS-socket query printed
    in the "Step 7" summary below — the same query works right now, this
    step does not block on it since the observability wiring may not
    have landed yet on this branch.
EOF
echo ""

# ---------------------------------------------------------------------
# Step 5 — bench writer smoke test (proves the reader WITHOUT Camilla)
# ---------------------------------------------------------------------
echo "--- Step 5/7: bench writer smoke test ---"
bench_bin="${JASPER_RING_PROTO_REMOTE_DIR:-/home/${PI_USER}/jts-ring-proto}/c/jts-ring-ioplug/ring_writer_bench"
if ! ssh_ok "test -x ${bench_bin}"; then
    echo "  SKIP bench binary not found at ${bench_bin} (run build-on-pi.sh) —" \
        "continuing to step 6 without this smoke test. This is a soft skip," \
        "not a failure: the resolvability probe (step 3) and the journal" \
        "confirmation (step 4) already proved the ring end-to-end at the" \
        "ALSA-open and reader-attach layers." >&2
else
    echo "  playing a short tone via the bench writer (listen for audio; check counters below)"
    bench_out="$(ssh_ok "sudo ${bench_bin} --path /dev/shm/jts-ring/content.ring --seconds 3 --pattern tone --freq 440 --paced" 2>&1)"
    bench_status=$?
    printf '%s\n' "${bench_out}" | sed 's/^/    /'
    if [[ "${bench_status}" -ne 0 ]]; then
        fail_and_rollback "step 5 (bench writer exited ${bench_status})"
    fi
    echo "  OK   bench writer completed — inspect the printed published/dropped counters above"
fi
echo ""

# ---------------------------------------------------------------------
# Step 6 — build + load the hand Camilla ring config
# ---------------------------------------------------------------------
echo "--- Step 6/7: build + load the hand Camilla ring config ---"

if ! JASPER_RING_PROTO_ALSA_DEVICE="${RING_DEVICE}" bash "${RING_PROTO_DIR}/make-camilla-ring-config.sh"; then
    fail_and_rollback "step 6 (make-camilla-ring-config.sh)"
fi

new_config_path="/var/lib/camilladsp/ring_proto.yml"
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

# jasper-camilla-pipe-guard runs as ExecStartPre on the restart below. It
# reads the same statefile we just wrote, sees a solo Alsa playback block
# (no 'filename:' key — verified by hand against a real ring config in
# this branch's development), and logs 'ok reason=solo_config'; it never
# fights this restart. See README.md "Why the pipe-guard doesn't fight
# this" for the full argument.
if ! ssh_ok "sudo systemctl restart jasper-camilla"; then
    fail_and_rollback "step 6 (systemctl restart jasper-camilla)"
fi

sleep 2
camilla_active="$(ssh_ok 'systemctl is-active jasper-camilla' 2>/dev/null)"
if [[ "${camilla_active}" != "active" ]]; then
    echo "  jasper-camilla status after restart: ${camilla_active}" >&2
    ssh_ok "journalctl -u jasper-camilla -n 40 --no-pager" >&2
    fail_and_rollback "step 6 (jasper-camilla did not come up active — see journal above)"
fi
guard_reason="$(ssh_ok "journalctl -u jasper-camilla -n 40 --no-pager | grep -o 'event=camilla_pipe_guard\\.[a-z_]* reason=[a-z_]*' | tail -1")"
echo "  OK   jasper-camilla active; pipe guard: ${guard_reason:-<no guard line found — check by hand>}"
echo "  verify: ssh ${PI_USER}@${PI_HOST} 'journalctl -u jasper-camilla -n 10 | grep camilla_pipe_guard'"
echo ""

# ---------------------------------------------------------------------
# Step 7 — final verify
# ---------------------------------------------------------------------
echo "--- Step 7/7: armed. Manual verification + next steps ---"
# Quoted heredoc delimiter ('SUMMARY_EOF'): the body below is full of
# single-quoted ssh command examples with nested double quotes (a Python
# one-liner). An UNQUOTED heredoc still tokenizes its body for quote
# balance even though it does not treat the quotes as special otherwise —
# an odd apostrophe/quote count anywhere in an unquoted heredoc body is a
# bash parse error, not just a cosmetic issue (hit and fixed earlier in
# this script's development). Quoting the delimiter makes the body
# byte-for-byte literal, so this block is immune to that class of bug
# even as it is edited. The two placeholders below are substituted with
# plain string replacement afterward instead of ${...} interpolation.
summary="$(cat <<'SUMMARY_EOF'

Ring B is now wired into the live chain on __PI_HOST__:

  fan-in -> CamillaDSP -> pcm.__RING_DEVICE__ (ioplug) -> SHM ring
    -> jasper-outputd (JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring)
    -> final DAC

Play music through the box now (any source) and check:

  # Occupancy / empty-read counters (once /state.shm_ring lands):
  ssh __PI_TARGET__ 'curl -s http://127.0.0.1:8780/state | jq .outputd.shm_ring 2>/dev/null || echo "(not yet surfaced in jasper-control /state)"'

  # Direct socket query (works regardless of jasper-control /state wiring):
  ssh __PI_TARGET__ 'sudo /opt/jasper/.venv/bin/python -c "
import socket, json
s = socket.socket(socket.AF_UNIX)
s.connect(\"/run/jasper-outputd/control.sock\")
s.sendall(b\"STATUS\\n\")
print(json.dumps(json.loads(s.recv(65536)).get(\"shm_ring\", \"not yet in /state\"), indent=2))
"'

  # Journal for xruns / drops / resyncs:
  ssh __PI_TARGET__ 'journalctl -u jasper-outputd -u jasper-camilla --since "2 min ago" | grep -iE "xrun|drop|resync|error"'

Then re-measure with the route-latency harness (see README.md for the
full command and the jts3-vs-jts.local sequencing note):

  ssh __PI_TARGET__ '/opt/jasper/.venv/bin/jasper-route-latency-harness generate quick --out-dir /tmp/route-latency'
  ssh __PI_TARGET__ 'sudo /opt/jasper/.venv/bin/jasper-route-latency-harness run /tmp/route-latency/quick-schedule.json --out-dir /tmp/route-latency --invoke-artifact --confirm-route-health-ok'

To roll back everything this script did:

  bash scripts/ring-proto/disarm.sh
SUMMARY_EOF
)"
summary="${summary//__PI_HOST__/${PI_HOST}}"
summary="${summary//__RING_DEVICE__/${RING_DEVICE}}"
summary="${summary//__PI_TARGET__/${PI_USER}@${PI_HOST}}"
printf '%s\n' "${summary}"
