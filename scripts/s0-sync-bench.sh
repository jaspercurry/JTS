#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# =============================================================================
# s0-sync-bench.sh — S0-SYNC de-risk gate BENCH harness (THROWAWAY)
# =============================================================================
#
# This is a *throwaway measurement deliverable*, NOT product. It exists to
# answer ONE question from docs/HANDOFF-distributed-active.md "Multi-Pi
# validation":
#
#     Does a WIRELESS ACTIVE FOLLOWER stay sample-locked when the active
#     follower's NEW seam — snapclient -> ALSA loopback -> crossover-only
#     CamillaDSP -> real DAC — is inserted between the snapcast receiver and
#     the speaker?
#
# The dumb-follower path (snapclient -> DAC, no re-entry) is ALREADY validated
# by scripts/multiroom-spike.sh. S0 de-risks the ONE thing the active follower
# adds and the dumb path deliberately avoids (the production dumb path uses
# `--player file` -> raw FIFO precisely to dodge snd-aloop): the snd-aloop +
# CamillaDSP re-entry, and the rate_adjust(no-resampler) capture-from-loopback
# clock seam against the DAC clock.
#
# THE CLOCK CONTRACT under test (docs/HANDOFF-distributed-active.md
# "Clock domain + fail-closed"):
#   * real DAC      = camilla PLAYBACK = clock master
#   * snd-aloop     = camilla CAPTURE  = slaved (snapclient writes it)
#   * enable_rate_adjust=true, NO resampler  (HEnquist bit-perfect loopback:
#     camilla nudges snd-aloop's "PCM Rate Shift" control to hold target_level)
#   * chunksize >= 1024, fixed target_level, NO SIGHUP during playback
#   * snapclient --latency nulls camilla's fixed pipeline latency
#
# ACCEPTANCE (written back into the doc by the operator):
#   * p99 inter-speaker offset < 5 ms over a 2-hour run, no audible resync
#   * >= 24 h snd-aloop xrun soak, journal clean
#   * (fallback if snd-aloop xruns: constructed/hardware loopback)
#
# TOPOLOGY (two throwaway ACTIVE followers):
#   LEADER/server  = jts3 : snapserver reads a hand-fed FIFO carrying a known
#                           1 Hz broadband CLICK (acoustic cross-correlation)
#                           PLUS jts3's own localhost active follower (#1).
#   FOLLOWER #1    = jts3 : snapclient -h 127.0.0.1 -> hw:Loopback,0,S
#                           -> camilla [crossover-only] -> HifiBerry DAC8x
#   FOLLOWER #2    = jts4 : snapclient -h jts3        -> hw:Loopback,0,S
#                           -> camilla [crossover-only] -> USB dongle DAC
#   Acoustic GT    = jts3's XVF mic hears BOTH speakers; the single-mic
#                    autocorrelation secondary peak = inter-speaker offset
#                    (scripts/s0-sync-measure.py; method from
#                    scripts/multiroom-spike-measure.py acoustic mode).
#
# ⚠ HEARING-SAFETY / CONTENTION: this bench drives the DACs OUTSIDE
# jasper-outputd, so it bypasses outputd's runtime path — but the throwaway
# CamillaDSP keeps volume_limit:0.0 and negative-only gains, AND a protective
# Layer-A high-pass, so a tweeter still cannot see full-range or positive gain.
# Still: it needs exclusive DAC ownership, so `--up` STOPS the live JTS audio
# stack on both Pis and `--teardown` restores it. START QUIET, ramp on an
# explicit OK only.
#
# THROWAWAY POSTURE (mirrors scripts/multiroom-spike.sh):
#   * Everything under /run/jts-s0 (tmpfs) + /tmp/jts-s0. One `--teardown`
#     removes every transient unit/file and restores the live stack.
#   * All bench processes are TRANSIENT systemd-run units named jts-s0-* so
#     they never collide with product units and a reboot wipes them.
#   * Touches NO jasper/ product code, NO reconciler, NO grouping.env. This is
#     a bench that proves the seam; Slice 3 wires the reconciler.
#
# USAGE
#   bash scripts/s0-sync-bench.sh --up                 # stop stack, build chain
#   bash scripts/s0-sync-bench.sh --smoke [secs]       # short acoustic + status
#   bash scripts/s0-sync-bench.sh --soak 24            # 24h xrun + budget monitor
#   bash scripts/s0-sync-bench.sh --collect            # pull soak logs + WAVs
#   bash scripts/s0-sync-bench.sh --status             # chain + lock + xruns
#   bash scripts/s0-sync-bench.sh --teardown           # stop bench, restore stack
#
# Host config (flags > env > defaults):
#   --leader   / S0_LEADER     (default jts3.local)   snapserver + follower #1
#   --follower / S0_FOLLOWER   (default jts4.local)   follower #2
#   --user     / S0_USER       (default pi)
#   --volume-db DB             (default -40)  throwaway camilla output trim
#   --sub S                    (default 0)    snd-aloop substream index to use
#   --resampler none|synchronous|async  (default none — per the spec)
# =============================================================================

set -euo pipefail

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
LEADER_HOST="${S0_LEADER:-jts3.local}"
FOLLOWER_HOST="${S0_FOLLOWER:-jts4.local}"
SSH_USER="${S0_USER:-pi}"
VOLUME_DB="${S0_VOLUME_DB:--40}"      # conservative; ramp on explicit OK
ALOOP_SUB="${S0_ALOOP_SUB:-0}"        # snd-aloop substream index for the chain
RESAMPLER="${S0_RESAMPLER:-none}"     # none|synchronous|async (spec: none)
SOAK_HOURS="${S0_SOAK_HOURS:-24}"
ACOUSTIC_CAP_SECS="${S0_ACOUSTIC_CAP_SECS:-20}"   # one periodic capture length
# Soak acoustic-capture cadence. DEFAULT 0 = DISABLED: jts3's onboard XVF mic
# cannot measure the inter-speaker offset (it hears its own close speaker; the
# autocorrelation locks on a ~0.29 ms self-reflection — proven on hardware), so
# capturing it during the soak would falsely "pass" the acoustic gate. The
# acoustic p99 is a separate, explicit step with a mic placed BETWEEN the
# speakers (use --smoke, or set S0_ACOUSTIC_EVERY_SECS>0 once such a mic exists).
ACOUSTIC_EVERY_SECS="${S0_ACOUSTIC_EVERY_SECS:-0}"
MIC_DEVICE="${S0_MIC_DEVICE:-plughw:Array,0}"      # jts3 XVF array (plug: 6ch->1 downmix)
# Laptop-side python for the analyzer (needs numpy). Override if your default
# python3 lacks it, e.g. S0_PYTHON=/tmp/s0-venv/bin/python.
S0_PYTHON_BIN="${S0_PYTHON:-python3}"

# Bench namespaces.
TMP_DIR="/tmp/jts-s0"
SNAPSERVER_CONF="${TMP_DIR}/snapserver.conf"
CLICK_RAW="${TMP_DIR}/click.raw"      # raw S16LE stereo, looped to the source stdout
FEED_SCRIPT="${TMP_DIR}/feed.sh"      # snapserver process:// source (writes click to stdout)
CAMILLA_CFG="${TMP_DIR}/camilla-crossover.yml"
ACOUSTIC_DIR="${TMP_DIR}/acoustic"
SOAK_LOG="${TMP_DIR}/soak.log"
SAVED_UNITS="${TMP_DIR}/saved-stack-units.txt"

# Ports.
SNAP_STREAM_PORT="1704"
SNAP_TCP_PORT="1705"
CAMILLA_WS_PORT="1240"     # throwaway camilla WS (live JTS camilla uses 1234)

# CamillaDSP binary (located on both Pis at /opt/camilladsp/camilladsp).
CAMILLA_BIN="/opt/camilladsp/camilladsp"

# Real DAC per host (raw hw: — never a plug, so no ALSA resampler contaminates
# the clock test). Width/format are detected at --up. A function (not an
# associative array) keeps this portable to macOS bash 3.2.
dac_dev_of() { case "$1" in leader) printf '%s' "${S0_LEADER_DAC:-hw:sndrpihifiberry,0}";; follower) printf '%s' "${S0_FOLLOWER_DAC:-hw:A,0}";; esac; }

# Transient unit names. (No feeder unit: snapserver's process:// source spawns
# the click generator itself and reads its stdout — far more robust than a FIFO
# rendezvous, which snapserver does NOT reliably hold the read end of.)
UNIT_SERVER="jts-s0-snapserver"
UNIT_CAMILLA="jts-s0-camilla"
UNIT_CLIENT="jts-s0-snapclient"
UNIT_MONITOR="jts-s0-monitor"

# Live JTS audio units this bench must stop to own the DAC + loopback. Restored
# at --teardown (only the ones that were active are restarted).
STACK_UNITS=(
  jasper-voice jasper-mux jasper-fanin jasper-camilla jasper-outputd
  jasper-aec-bridge jasper-usbsink librespot shairport-sync nqptp
  bluealsa-aplay
)

# The subset of STACK_UNITS that escalate to StartLimitAction=reboot (verified
# on jts3 2026-06-20). The bench MUST disarm these before stopping them, or a
# re-trigger while the bench holds the DAC/loopback fails-loops into a REBOOT
# (observed 3x). We disarm with a /run drop-in exactly like JTS's own
# jasper-bootloop-guard, which self-clears on the next boot.
REBOOT_UNITS=(
  jasper-fanin jasper-camilla jasper-outputd jasper-voice jasper-aec-bridge
)
DROPIN_NAME="zz-s0-bench.conf"

RESULTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/s0-sync-bench"

log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# host key -> mDNS host
host_of() { case "$1" in leader) printf '%s' "$LEADER_HOST";; follower) printf '%s' "$FOLLOWER_HOST";; esac; }
ssh_h()   { ssh -o ConnectTimeout=8 -o BatchMode=yes "${SSH_USER}@$(host_of "$1")" "$@"; }
# ssh_h takes (hostkey, cmd...) — drop the hostkey before the command.
ssh_run() { local hk="$1"; shift; ssh -o ConnectTimeout=8 -o BatchMode=yes "${SSH_USER}@$(host_of "$hk")" "$@"; }

# -----------------------------------------------------------------------------
# Known 1 Hz broadband click track on the leader (acoustic cross-correlation
# ground truth), generated as RAW S16LE stereo so the feeder can cat-loop it
# straight into the FIFO (sox is NOT installed on the Pis — verified). Identical
# L/R; 2 ms band-limited burst (1-pole-smoothed fixed-seed noise) every 1.000 s
# at -20 dBFS, 48k. Reuses scripts/multiroom-spike.sh's click rationale: a tone's
# autocorrelation is ambiguous at its own period; a broadband click has ONE
# sharp autocorr peak so the secondary peak is unambiguously the inter-speaker
# offset.
# -----------------------------------------------------------------------------
make_click() {
  log "[leader] generating 1 Hz broadband click track (raw S16LE)..."
  ssh_run leader "sudo install -d -m 0777 ${TMP_DIR}; python3 - ${CLICK_RAW}" <<'PY'
import random, struct, sys
out = sys.argv[1]; sr = 48000
click_s, gap_s, reps = 0.002, 0.998, 60
amp = 32767 * (10 ** (-20/20)); rng = random.Random(1234)
n = int(click_s*sr); raw = [rng.uniform(-1, 1) for _ in range(n)]
sm, prev = [], 0.0
for x in raw:
    prev = 0.5*prev + 0.5*x; sm.append(prev)
peak = max(abs(v) for v in sm) or 1.0
click = [int(amp*v/peak) for v in sm]
gap = b'\x00\x00\x00\x00' * int(gap_s*sr)   # 4 bytes/frame (S16 stereo)
frames = bytearray()
for _ in range(reps):
    for s in click:
        frames += struct.pack('<hh', s, s)  # identical L and R
    frames += gap
with open(out, 'wb') as f:
    f.write(bytes(frames))
print(f"click.raw: {out} ({len(frames)} bytes, {len(frames)/4/sr:.1f}s @48k S16 stereo)")
PY
}

# -----------------------------------------------------------------------------
# Detect a DAC's native channel count + format (so camilla opens it raw hw:,
# no ALSA-plug resampler). Echoes "CHANNELS FORMAT" (e.g. "8 S32LE"). The DAC
# must be FREE (live stack stopped) for the probe to open it.
# -----------------------------------------------------------------------------
detect_dac() {
  local hk="$1" dev="$2"
  ssh_run "$hk" "aplay -D ${dev} --dump-hw-params -d 0.1 /dev/zero 2>&1 || true" \
    | awk '
      /^CHANNELS:/ { for(i=2;i<=NF;i++){gsub(/[\[\]]/,"",$i); if($i ~ /^[0-9]+$/) ch[$i]=1} }
      /^FORMAT:/   { for(i=2;i<=NF;i++) fmt[$i]=1 }
      END {
        # channels: prefer 2, else the smallest offered.
        chn=""; if(ch[2]) chn=2; else { best=999; for(k in ch) if(k+0<best) best=k+0; if(best<999) chn=best }
        if(chn=="") chn=2
        # format: CamillaDSP 4.x names. Prefer S32_LE > S24 > S16_LE.
        f="S16_LE"
        if(fmt["S16_LE"]) f="S16_LE"
        if(fmt["S24_3LE"]) f="S24_3_LE"; if(fmt["S24_LE"]) f="S24_4_LE"
        if(fmt["S32_LE"]) f="S32_LE"
        print chn, f
      }'
}

# -----------------------------------------------------------------------------
# Write the crossover-only CamillaDSP config on a host. This is the SEAM under
# test: Alsa capture (snd-aloop, slaved) -> Alsa playback (real DAC, master),
# enable_rate_adjust, NO resampler (per the spec; --resampler overrides), fixed
# target_level, chunksize 1024. The "crossover" stand-in is a protective LR4
# high-pass per channel (the tweeter-safety bit of Layer A) + a 2->N silence-
# padded mixer + volume_limit:0.0 + the conservative output trim. It is a REAL
# DSP graph re-entry (not a passthrough); the clock behaviour is identical
# regardless of the biquad content, which is what the bench measures.
# -----------------------------------------------------------------------------
write_camilla_cfg() {
  local hk="$1" cap_dev="$2" dac_dev="$3" dac_ch="$4" dac_fmt="$5"
  local resampler_block=""
  case "$RESAMPLER" in
    none) resampler_block="" ;;
    synchronous) resampler_block=$'  resampler:\n    type: Synchronous' ;;
    async) resampler_block=$'  resampler:\n    type: AsyncSinc\n    profile: Balanced' ;;
    *) die "bad --resampler: $RESAMPLER" ;;
  esac
  # Build the 2->N mixer mapping: dest 0<-in0, dest 1<-in1, dest 2..N-1 silent.
  local mapping="      - dest: 0
        sources: [{channel: 0, gain: ${VOLUME_DB}, inverted: false}]
      - dest: 1
        sources: [{channel: 1, gain: ${VOLUME_DB}, inverted: false}]"
  local pipeline="  - type: Mixer
    name: stereo_to_dac
  - type: Filter
    channels: [0, 1]
    names: [hp_l1, hp_l2]"
  ssh_run "$hk" "sudo install -d -m 0777 ${TMP_DIR}; cat > ${CAMILLA_CFG}" <<EOF
# jts-s0 throwaway crossover-only CamillaDSP (S0-sync bench). DO NOT ship.
# Clock contract: DAC playback = master; snd-aloop capture = slaved;
# enable_rate_adjust + (resampler: ${RESAMPLER}); fixed target_level.
devices:
  samplerate: 48000
  chunksize: 1024
  queuelimit: 4
  enable_rate_adjust: true
  target_level: 1024
  adjust_period: 3
  capture:
    type: Alsa
    channels: 2
    device: "${cap_dev}"
    format: S16_LE
  playback:
    type: Alsa
    channels: ${dac_ch}
    device: "${dac_dev}"
    format: ${dac_fmt}
${resampler_block}
  volume_limit: 0.0
filters:
  # Protective Layer-A high-pass (LR4 = two cascaded Butterworth highpass).
  hp_l1:
    type: Biquad
    parameters: {type: Highpass, freq: 80, q: 0.707}
  hp_l2:
    type: Biquad
    parameters: {type: Highpass, freq: 80, q: 0.707}
mixers:
  stereo_to_dac:
    channels: {in: 2, out: ${dac_ch}}
    mapping:
${mapping}
pipeline:
${pipeline}
EOF
  log "[$hk] camilla config -> ${CAMILLA_CFG} (playback ${dac_ch}ch ${dac_fmt}, resampler=${RESAMPLER})"
}

# -----------------------------------------------------------------------------
# snapserver.conf — process:// source: snapserver spawns FEED_SCRIPT and reads
# its stdout (the looped click). This sidesteps the FIFO open/rendezvous entirely
# (snapserver does NOT reliably hold a pipe's read end — verified on jts3, both
# mode=read and mode=create returned ENXIO to a writer; the stream only stayed
# fed via the process source). PCM codec (zero codec latency); buffer 1000 ms
# (generous WiFi jitter absorption — the spike already found PCM holds p99<5ms;
# this bench is about the snd-aloop seam, not codec).
# -----------------------------------------------------------------------------
write_server_conf() {
  # FEED_SCRIPT: a single long-lived process that loops the raw click to stdout
  # (no per-iteration fork gap). snapserver paces consumption via backpressure.
  ssh_run leader "sudo install -d -m 0777 ${TMP_DIR}; cat > ${FEED_SCRIPT} && chmod +x ${FEED_SCRIPT}" <<EOF
#!/usr/bin/env bash
exec python3 -c "
import sys
d = open('${CLICK_RAW}', 'rb').read()
w = sys.stdout.buffer
while True:
    w.write(d); w.flush()
"
EOF
  ssh_run leader "cat > ${SNAPSERVER_CONF}" <<EOF
# jts-s0 throwaway snapserver.conf. DO NOT ship.
[stream]
source = process://${FEED_SCRIPT}?name=jts-s0&sampleformat=48000:16:2
codec = pcm
buffer = 1000
chunk_ms = 20
[tcp]
enabled = true
bind_to_address = 0.0.0.0
port = ${SNAP_TCP_PORT}
[http]
enabled = false
[server]
threads = -1
EOF
  log "[leader] feed.sh + snapserver.conf written (process source, pcm/1000ms)"
}

# -----------------------------------------------------------------------------
# Neutralize / restore the live JTS audio stack (the bench needs exclusive DAC +
# snd-aloop ownership). We DISARM the reboot escalation BEFORE stopping, because
# a bare `stop` is unsafe: the essential audio units have StartLimitAction=reboot
# (verified), so a re-trigger while the bench holds the DAC/loopback fails-loops
# into a REBOOT (observed on jts3 3x: jasper-fanin restart counter 1->5 ->
# "Rebooting: unit jasper-fanin.service failed"). The disarm is a /run drop-in
# (StartLimitAction=none + FailureAction=none + Restart=no) on each reboot unit
# — the EXACT mechanism JTS's own jasper-bootloop-guard uses — so it self-clears
# on the next boot (the box self-heals if this session dies). We then VERIFY the
# disarm took effect and ABORT before stopping if it did not. Masking is NOT
# used: masking a running unit then `stop` is a no-op (the unit file becomes
# /dev/null), which is exactly why the earlier masking attempt still rebooted.
# -----------------------------------------------------------------------------
neutralize_stack() {
  local hk="$1"
  log "[$hk] disarming reboot escalation on the audio stack (drop-ins)..."
  ssh_run "$hk" "sudo install -d -m 0777 ${TMP_DIR}; bash -s" <<REMOTE
set -eu
# Write the reboot-disarm drop-in for each reboot unit. Dir MUST be
# <unit>.service.d (NOT <unit>.d) or systemd ignores it.
for u in ${REBOOT_UNITS[*]}; do
  sudo mkdir -p /run/systemd/system/\$u.service.d
  printf '[Unit]\nStartLimitIntervalSec=0\nStartLimitAction=none\nFailureAction=none\n[Service]\nRestart=no\n' \
    | sudo tee /run/systemd/system/\$u.service.d/${DROPIN_NAME} >/dev/null
done
sudo systemctl daemon-reload
# VERIFY the disarm before touching anything — abort if any unit still reboots.
bad=0
for u in ${REBOOT_UNITS[*]}; do
  sla=\$(systemctl show \$u -p StartLimitAction --value 2>/dev/null)
  [ "\$sla" = "none" ] || { echo "REBOOT STILL ARMED on \$u (StartLimitAction=\$sla)"; bad=1; }
done
[ \$bad -eq 0 ] || { echo "ABORT: reboot disarm did not take — NOT stopping the stack"; exit 3; }
echo "reboot disarmed on: ${REBOOT_UNITS[*]}"
# Record the active set ONLY on the first run (a re-run of --up must not clobber
# the original restore list). Persists in ${TMP_DIR} until --teardown / reboot.
if [ ! -f ${SAVED_UNITS} ]; then
  : > ${SAVED_UNITS}
  for u in ${STACK_UNITS[*]}; do
    if systemctl is-active --quiet "\$u" 2>/dev/null; then echo "\$u" >> ${SAVED_UNITS}; fi
  done
fi
sudo systemctl stop ${STACK_UNITS[*]} 2>/dev/null || true
echo "stopped; saved restore set:"; cat ${SAVED_UNITS} 2>/dev/null || true
REMOTE
}

restore_stack() {
  local hk="$1"
  log "[$hk] restoring live JTS audio stack (remove disarm drop-ins)..."
  ssh_run "$hk" "bash -s" <<REMOTE 2>/dev/null || true
set -eu
# Restart the saved set FIRST (under the still-relaxed Restart=no), then remove
# the drop-ins so normal reboot-on-fail resilience returns.
if [ -s ${SAVED_UNITS} ]; then
  units=\$(tr '\n' ' ' < ${SAVED_UNITS})
  sudo systemctl reset-failed \$units 2>/dev/null || true
  sudo systemctl start \$units 2>/dev/null || true
  echo "restored: \$units"
else
  echo "no saved unit set (wiped by a reboot?); starting core audio units"
  sudo systemctl start jasper-camilla jasper-outputd jasper-fanin 2>/dev/null || true
fi
for u in ${REBOOT_UNITS[*]}; do sudo rm -f /run/systemd/system/\$u.service.d/${DROPIN_NAME}; sudo rmdir /run/systemd/system/\$u.service.d 2>/dev/null || true; done
sudo systemctl daemon-reload
echo "removed reboot-disarm drop-ins; normal resilience restored"
REMOTE
}

# -----------------------------------------------------------------------------
# Start snapserver + the FIFO feeder (leader only).
# -----------------------------------------------------------------------------
start_server() {
  log "[leader] starting snapserver (spawns the click process source)..."
  ssh_run leader "bash -s" <<REMOTE
set -eu
sudo systemctl reset-failed ${UNIT_SERVER}.service 2>/dev/null || true
sudo systemctl stop ${UNIT_SERVER}.service 2>/dev/null || true
# snapserver spawns FEED_SCRIPT itself (process:// source) and reads its stdout.
# No FIFO, no separate feeder unit, no open-rendezvous.
sudo systemd-run --collect --unit=${UNIT_SERVER} --property=Description='jts-s0 snapserver' \
  snapserver -c ${SNAPSERVER_CONF}
echo "snapserver up"
REMOTE
}

# -----------------------------------------------------------------------------
# Bring up one active follower: camilla (crossover) FIRST (it must open the
# snd-aloop capture + lock the loopback params), then snapclient writing the
# loopback playback side. snapclient --latency nulls camilla's fixed latency.
# -----------------------------------------------------------------------------
start_follower() {
  local hk="$1" leader_addr="$2" latency_ms="$3"
  local cap_dev="hw:Loopback,1,${ALOOP_SUB}"
  local play_dev="hw:Loopback,0,${ALOOP_SUB}"
  local dac_dev; dac_dev="$(dac_dev_of "$hk")"
  log "[$hk] detecting DAC params for ${dac_dev}..."
  local det; det="$(detect_dac "$hk" "$dac_dev")"
  local dac_ch="${det%% *}" dac_fmt="${det##* }"
  log "[$hk] DAC: channels=${dac_ch} format=${dac_fmt}"
  write_camilla_cfg "$hk" "$cap_dev" "$dac_dev" "$dac_ch" "$dac_fmt"
  log "[$hk] starting throwaway camilla (capture ${cap_dev} -> ${dac_dev})..."
  ssh_run "$hk" "bash -s" <<REMOTE
set -eu
sudo systemctl reset-failed ${UNIT_CAMILLA}.service ${UNIT_CLIENT}.service 2>/dev/null || true
sudo systemctl stop ${UNIT_CAMILLA}.service ${UNIT_CLIENT}.service 2>/dev/null || true
# camilla: NO SIGHUP during playback (config loaded once at start).
sudo systemd-run --collect --unit=${UNIT_CAMILLA} --property=Description='jts-s0 camilla crossover' \
  --property=Nice=-10 \
  ${CAMILLA_BIN} -p ${CAMILLA_WS_PORT} -a 127.0.0.1 -l info ${CAMILLA_CFG}
sleep 2
# snapclient writes the loopback PLAYBACK side; camilla captures the paired
# capture side. --latency nulls camilla's fixed pipeline latency.
sudo systemd-run --collect --unit=${UNIT_CLIENT} --property=Description='jts-s0 snapclient' \
  snapclient --host ${leader_addr} --port ${SNAP_STREAM_PORT} \
  --soundcard ${play_dev} --latency ${latency_ms} --player alsa
echo "follower up on ${hk}"
REMOTE
}

# -----------------------------------------------------------------------------
# --up : stop stack on both, free DAC, build chain, verify lock.
# -----------------------------------------------------------------------------
do_up() {
  mkdir -p "$RESULTS_DIR"
  log "==== S0-SYNC BENCH --up : leader=${LEADER_HOST} follower=${FOLLOWER_HOST} ===="
  for hk in leader follower; do
    ssh_run "$hk" "command -v snapclient >/dev/null && test -x ${CAMILLA_BIN}" \
      || die "[$hk] missing snapclient or ${CAMILLA_BIN}"
  done
  neutralize_stack leader
  neutralize_stack follower
  make_click
  write_server_conf
  start_server
  sleep 2
  # Follower #1 = leader's own localhost client. Camilla latencies are equal on
  # identical configs; per-DAC fixed offset is trimmed via --latency after the
  # smoke. Start both at 0; refine from the acoustic smoke.
  start_follower leader 127.0.0.1 0
  start_follower follower "${LEADER_HOST}" 0
  sleep 4
  do_status
  log "==== --up complete. Next: --smoke, then --soak ${SOAK_HOURS} ===="
}

# -----------------------------------------------------------------------------
# --status : units + camilla lock signal + xrun counts so far.
# -----------------------------------------------------------------------------
do_status() {
  for hk in leader follower; do
    echo "================ $hk ($(host_of "$hk")) ================"
    ssh_run "$hk" "bash -s" <<REMOTE 2>&1 | sed 's/^/  /'
set +e
echo '--- units ---'
for u in ${UNIT_SERVER} ${UNIT_CAMILLA} ${UNIT_CLIENT} ${UNIT_MONITOR}; do
  st=\$(systemctl is-active \$u.service 2>/dev/null); printf '%-22s %s\n' "\$u" "\$st"
done
echo '--- camilla rate-adjust / xrun signal (last 8 lines) ---'
journalctl -u ${UNIT_CAMILLA}.service -n 8 --no-pager -o cat 2>/dev/null | sed 's/^/    /'
echo '--- xrun/underrun count (since unit start) ---'
journalctl -u ${UNIT_CAMILLA}.service --no-pager -o cat 2>/dev/null \
  | grep -icE 'xrun|underrun|overrun|buffer underflow|capture error' || true
echo '--- snapclient connected? ---'
journalctl -u ${UNIT_CLIENT}.service -n 4 --no-pager -o cat 2>/dev/null | sed 's/^/    /'
REMOTE
    echo
  done
}

# -----------------------------------------------------------------------------
# --smoke [secs] : record the leader's XVF mic, pull to laptop, run the
# analyzer to confirm the single-mic autocorrelation actually sees BOTH
# speakers (a clean secondary peak). This is the go/no-go for the acoustic p99.
# -----------------------------------------------------------------------------
do_smoke() {
  local secs="${1:-${ACOUSTIC_CAP_SECS}}"
  mkdir -p "$RESULTS_DIR"
  log "[leader] smoke acoustic capture ${secs}s from ${MIC_DEVICE}..."
  local remote local_wav
  remote="${ACOUSTIC_DIR}/smoke-$(date -u +%H%M%SZ).wav"
  ssh_run leader "sudo install -d -m 0777 ${ACOUSTIC_DIR}; arecord -D ${MIC_DEVICE} -d ${secs} -f S16_LE -r 48000 -c 1 ${remote} 2>&1 | tail -1; echo recorded ${remote}"
  local_wav="${RESULTS_DIR}/$(basename "$remote")"
  scp -q "${SSH_USER}@${LEADER_HOST}:${remote}" "$local_wav" || die "scp smoke wav failed"
  log "smoke wav -> ${local_wav}"
  "$S0_PYTHON_BIN" "$(dirname "${BASH_SOURCE[0]}")/s0-sync-measure.py" acoustic --wav "$local_wav" || true
  do_status
}

# -----------------------------------------------------------------------------
# Deploy the standalone soak-monitor script to a host. Kept as a real script
# (not an inline heredoc) so the deep nesting of journalctl/awk/python doesn't
# fight shell escaping. Logs one line/min: xruns + CPU/temp/throttle/Pss PLUS
# the DIRECT clock-lock telemetry from camilla's websocket (state, buffer_level
# vs target, rate_adjust factor, raw capture rate) via pycamilladsp — the most
# direct measurement of the seam under test (does camilla hold the snd-aloop
# capture locked to the DAC). The leader role also captures periodic mic WAVs.
# -----------------------------------------------------------------------------
make_monitor_script() {
  local hk="$1"
  ssh_run "$hk" "sudo install -d -m 0777 ${TMP_DIR}; cat > ${TMP_DIR}/monitor.sh && chmod +x ${TMP_DIR}/monitor.sh" <<'MON'
#!/usr/bin/env bash
# jts-s0 soak monitor (THROWAWAY). Args: role ws_port mic cap_secs acoustic_every
set +u
ROLE="$1"; WS_PORT="$2"; MIC="$3"; CAP_SECS="$4"; ACOUSTIC_EVERY="$5"
LOG=/tmp/jts-s0/soak.log
ACDIR=/tmp/jts-s0/acoustic
CAMUNIT=jts-s0-camilla.service
PYBIN=/opt/jasper/.venv/bin/python
mkdir -p "$ACDIR"; : > "$LOG"
last_ac=0
while true; do
  now=$(date -u +%s)
  xr=$(journalctl -u "$CAMUNIT" --no-pager -o cat 2>/dev/null | grep -icE 'xrun|underrun|overrun|buffer underflow|capture error')
  temp=$(vcgencmd measure_temp 2>/dev/null | tr -dc '0-9.')
  throt=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)
  load=$(cut -d' ' -f1 /proc/loadavg)
  cpid=$(pgrep -x camilladsp | head -1); spid=$(pgrep -x snapclient | head -1)
  cpss=$(awk '/^Pss:/{s+=$2} END{print s+0}' "/proc/$cpid/smaps_rollup" 2>/dev/null)
  spss=$(awk '/^Pss:/{s+=$2} END{print s+0}' "/proc/$spid/smaps_rollup" 2>/dev/null)
  cam=$("$PYBIN" - "$WS_PORT" 2>/dev/null <<'PY'
import sys
try:
    from camilladsp import CamillaClient
    c = CamillaClient("127.0.0.1", int(sys.argv[1])); c.connect()
    print(f"{c.general.state().name} {c.status.buffer_level()} {c.status.rate_adjust():.6f} {c.rate.capture_raw()}")
except Exception:
    print("NA 0 0 0")
PY
)
  set -- $cam; cstate="$1"; cbuf="$2"; crate="$3"; ccap="$4"
  printf 'ts=%s host=%s camilla_xruns=%s temp_c=%s throttled=%s load1=%s camilla_pss_kb=%s snapclient_pss_kb=%s camilla_state=%s buffer_level=%s rate_adjust=%s capture_rate=%s\n' \
    "$now" "$ROLE" "${xr:-0}" "${temp:-NA}" "${throt:-NA}" "${load:-NA}" "${cpss:-0}" "${spss:-0}" "${cstate:-NA}" "${cbuf:-0}" "${crate:-0}" "${ccap:-0}" >> "$LOG"
  if [ "$ROLE" = leader ] && [ "${ACOUSTIC_EVERY:-0}" -gt 0 ] && [ $((now - last_ac)) -ge "$ACOUSTIC_EVERY" ]; then
    arecord -D "$MIC" -d "$CAP_SECS" -f S16_LE -r 48000 -c 1 "$ACDIR/cap-$now.wav" >/dev/null 2>&1 || true
    last_ac=$now
  fi
  sleep 60
done
MON
}

# -----------------------------------------------------------------------------
# --soak HOURS : per-host monitor unit (clock-lock telemetry + xrun + budget +
# periodic acoustic on the leader). Runs ON the Pi as a transient unit so it
# survives the operator's session ending. --collect pulls the accumulating logs.
# -----------------------------------------------------------------------------
do_soak() {
  local hours="${1:-${SOAK_HOURS}}"
  local max_sec=$(( hours*3600 + 600 ))
  log "==== starting ${hours}h soak monitors (clock-lock + xrun + budget + acoustic) ===="
  for hk in leader follower; do
    make_monitor_script "$hk"
    ssh_run "$hk" "bash -s" <<REMOTE
set -eu
sudo systemctl reset-failed ${UNIT_MONITOR}.service 2>/dev/null || true
sudo systemctl stop ${UNIT_MONITOR}.service 2>/dev/null || true
sudo systemd-run --collect --unit=${UNIT_MONITOR} --property=RuntimeMaxSec=${max_sec} \
  --property=Description='jts-s0 soak monitor (${hk})' \
  bash ${TMP_DIR}/monitor.sh ${hk} ${CAMILLA_WS_PORT} ${MIC_DEVICE} ${ACOUSTIC_CAP_SECS} ${ACOUSTIC_EVERY_SECS}
echo "${hk} monitor up (max ${max_sec}s)"
REMOTE
  done
  log "==== soak monitors running. Use --collect to pull logs + WAVs. ===="
}

# -----------------------------------------------------------------------------
# --collect : pull soak logs + acoustic WAVs to the laptop results dir.
# -----------------------------------------------------------------------------
do_collect() {
  local stamp; stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  local out="${RESULTS_DIR}/${stamp}"; mkdir -p "$out/acoustic"
  log "collecting soak logs + acoustic WAVs -> ${out}"
  scp -q "${SSH_USER}@${LEADER_HOST}:${SOAK_LOG}" "${out}/soak-leader.log" 2>/dev/null || log "no leader soak log yet"
  scp -q "${SSH_USER}@${FOLLOWER_HOST}:${SOAK_LOG}" "${out}/soak-follower.log" 2>/dev/null || log "no follower soak log yet"
  rsync -aq -e "ssh -o ConnectTimeout=8 -o BatchMode=yes" \
    "${SSH_USER}@${LEADER_HOST}:${ACOUSTIC_DIR}/" "${out}/acoustic/" 2>/dev/null || log "no acoustic WAVs yet"
  ln -sfn "$out" "${RESULTS_DIR}/latest"
  log "collected. Analyze with: python3 scripts/s0-sync-measure.py soak --dir ${out}"
  ls -1 "${out}" "${out}/acoustic" 2>/dev/null | sed 's/^/  /' || true
}

# -----------------------------------------------------------------------------
# --teardown : stop every bench unit, remove FIFO/configs, restore live stack.
# -----------------------------------------------------------------------------
do_teardown() {
  log "==== TEARDOWN: stopping bench units + restoring live stack ===="
  for hk in leader follower; do
    ssh_run "$hk" "bash -s" <<REMOTE 2>/dev/null || true
sudo systemctl stop ${UNIT_MONITOR}.service ${UNIT_CLIENT}.service ${UNIT_CAMILLA}.service \
  ${UNIT_SERVER}.service 2>/dev/null || true
sudo systemctl reset-failed ${UNIT_MONITOR}.service ${UNIT_CLIENT}.service ${UNIT_CAMILLA}.service \
  ${UNIT_SERVER}.service 2>/dev/null || true
REMOTE
  done
  restore_stack leader
  restore_stack follower
  log "==== teardown complete. Verify live audio: http://${LEADER_HOST}/system/ ===="
}

# =============================================================================
ACTION=""; ARG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --up) ACTION="up" ;;
    --smoke) ACTION="smoke"; ARG="${2:-}"; [[ -n "$ARG" && "$ARG" != --* ]] && shift || ARG="" ;;
    --soak) ACTION="soak"; ARG="${2:-}"; [[ -n "$ARG" && "$ARG" != --* ]] && shift || ARG="" ;;
    --collect) ACTION="collect" ;;
    --status) ACTION="status" ;;
    --teardown) ACTION="teardown" ;;
    --leader) LEADER_HOST="$2"; shift ;;
    --follower) FOLLOWER_HOST="$2"; shift ;;
    --user) SSH_USER="$2"; shift ;;
    --volume-db) VOLUME_DB="$2"; shift ;;
    --sub) ALOOP_SUB="$2"; shift ;;
    --resampler) RESAMPLER="$2"; shift ;;
    -h|--help) sed '2,6d' "$0" | sed -n '2,/^# ===/p' | sed 's/^# \{0,1\}//' >&2; exit 0 ;;
    *) die "unknown arg: $1 (try --help)" ;;
  esac
  shift
done

case "$ACTION" in
  up) do_up ;;
  smoke) do_smoke "${ARG:-}" ;;
  soak) do_soak "${ARG:-}" ;;
  collect) do_collect ;;
  status) do_status ;;
  teardown) do_teardown ;;
  *) sed '2,6d' "$0" | sed -n '2,/^# ===/p' | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
esac
