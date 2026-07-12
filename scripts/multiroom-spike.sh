#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# =============================================================================
# multiroom-spike.sh — P0 multi-room feasibility SPIKE harness (THROWAWAY)
# =============================================================================
#
# This is a *throwaway measurement deliverable*, not product. It exists to
# answer ONE question from docs/HANDOFF-multiroom.md §8:
#
#     "What Snapcast buffer depth + codec holds L/R sync on THIS household's
#      WiFi (working target p99 < 5 ms), within the 1 GB Pi's RAM/CPU budget?"
#
# It stands up a self-contained Snapcast topology, completely OFF to the side
# of the live JTS audio path, sweeps {buffer × codec}, and tears everything
# down again. It touches NOTHING in jasper/, no jasper-* daemon, no CamillaDSP,
# no snd-aloop. The owner runs this on real hardware; it cannot be run on the
# dev laptop (snapserver/snapclient aren't installed there).
#
# ⚠ HEARING-SAFETY / CONTENTION: because this bypasses CamillaDSP, it also
# bypasses JTS's volume_limit:0.0 ceiling and the negative-only gain clamp —
# snapclient drives the DAC directly. SET A CONSERVATIVE VOLUME before the
# first sweep and ramp up only on an explicit OK. The leader's spike snapclient
# also contends with jasper-outputd for the DAC, so run with the JTS audio
# daemons stopped (or on bring-up hardware), not on a speaker in active use.
#
# -----------------------------------------------------------------------------
# WHAT IT BUILDS (per docs/HANDOFF-multiroom.md §8 P0)
# -----------------------------------------------------------------------------
#   LEADER  (brainy Pi):  snapserver reading a hand-fed FIFO (/run/jts-spike/
#                         snapfifo) carrying a KNOWN test source — a periodic
#                         chirp/click track (for acoustic cross-correlation)
#                         AND a real music file (for an ear comb-filter check).
#                         The leader ALSO runs its own localhost snapclient
#                         playing to a REAL output device (default: the system
#                         default ALSA device) — NOT a Loopback PCM. This dodges
#                         the documented snd_pcm_delay-lies-on-snd-aloop trap
#                         (§2 invariant #2).
#
#   FOLLOWER (2nd brainy Pi):  a snapclient bonded to the leader (the L/R peer).
#
#   SUB     (Pi Zero):    a snapclient on the cheap-endpoint tier, exercising
#                         the loose-sub-sync path.
#
# -----------------------------------------------------------------------------
# THE SWEEP
# -----------------------------------------------------------------------------
#   buffer depth {150, 300, 500, 800, 1200} ms   (jitter-absorption lever)
#   codec        {pcm, flac, opus}               (RAM/CPU vs bandwidth)
#   transport    WiFi (the supported transport — the thing we're proving)
#                Ethernet is measured ONLY as a best-case REFERENCE line, never
#                as a fallback requirement. Pass --reference-ethernet to add it.
#
# For each (buffer, codec) cell the harness:
#   1. rewrites snapserver.conf with that buffer + codec
#   2. restarts the spike snapserver and all clients
#   3. lets it settle, then lets the analyzer poll JSON-RPC stats
#      (software-mode latency/offset) for a fixed window
#   4. records snapserver+snapclient RAM (Pss) and per-core CPU on the 1 GB Pi
#      via the BOUNDED runner (scripts/pi-run-diagnostic.sh idiom)
#
# Acoustic ground-truth (the real comb-filter check) is operator-driven: you
# put a single mic between the L/R pair, record the chirp, and feed that WAV to
# the analyzer's acoustic mode. The harness prints the exact recording command.
#
# -----------------------------------------------------------------------------
# OFF-BY-DEFAULT / SAFETY POSTURE
# -----------------------------------------------------------------------------
#   * Everything lives under /run/jts-spike (tmpfs) and /tmp/jts-spike. A single
#     `--teardown` removes every unit/file/process this script created.
#   * The spike snapserver/snapclients run as TRANSIENT systemd-run units named
#     jts-spike-* so they never collide with (or get confused for) a future
#     product snapserver.service, and a reboot wipes them with zero residue.
#   * IDEMPOTENT: re-running --setup first tears down any prior spike state.
#   * apt-installs snapserver/snapclient ONLY when missing, ONLY with explicit
#     consent (--apt-install or JTS_SPIKE_APT_INSTALL=1), and prints exactly
#     what it will install first.
#   * Audio output volume is the operator's responsibility — start the music
#     file QUIET (this harness deliberately does not touch CamillaDSP / the JTS
#     safety clamp because it is entirely off the JTS path). The test chirp is
#     generated at -20 dBFS.
#
# -----------------------------------------------------------------------------
# USAGE
# -----------------------------------------------------------------------------
#   # 0. Configure the three hosts (laptop-side; see .env.local + flags below).
#   #    LEADER defaults to PI_HOST from .env.local. Follower/sub are REQUIRED
#   #    for the L/R + sub measurements; omit them to run a leader-only smoke.
#   #
#   #      bash scripts/multiroom-spike.sh --setup \
#   #          --follower brittany-pi.local --sub pizero.local --music ~/Music/test.flac
#   #
#   # 1. Sweep (drives the whole {buffer × codec} matrix, software-mode stats):
#   #      bash scripts/multiroom-spike.sh --sweep
#   #
#   # 2. (Optional) WiFi-stress one cell with tc netem loss/jitter on the leader:
#   #      bash scripts/multiroom-spike.sh --netem '50ms 10ms loss 1%' --sweep
#   #
#   # 3. Acoustic ground truth for the headline cell — record then analyze:
#   #      bash scripts/multiroom-spike.sh --record-chirp 12   # prints WAV path
#   #      python3 scripts/multiroom-spike-measure.py acoustic --wav <recorded.wav>
#   #
#   # 4. Tear everything down:
#   #      bash scripts/multiroom-spike.sh --teardown
#   #
#   # Host config precedence: flags > JTS_SPIKE_* env > .env.local PI_HOST.
#
# Defaults: PI_HOST/PI_USER come from .env.local when present (the LEADER).
# Follower/sub have no default — pass --follower / --sub.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "${SCRIPT_DIR}/_lib.sh"

# -----------------------------------------------------------------------------
# Spike-wide constants. All under dedicated jts-spike namespaces so nothing
# collides with the live JTS path and teardown can be exhaustive.
# -----------------------------------------------------------------------------
SPIKE_RUN_DIR="/run/jts-spike"                 # tmpfs: FIFO + pids (wiped on reboot)
SPIKE_TMP_DIR="/tmp/jts-spike"                 # test sources + generated conf
SNAPFIFO="${SPIKE_RUN_DIR}/snapfifo"           # the hand-fed pipe snapserver reads
SNAPSERVER_CONF="${SPIKE_TMP_DIR}/snapserver.conf"
CHIRP_WAV="${SPIKE_TMP_DIR}/chirp.wav"         # known periodic chirp (acoustic GT)
SNAPSERVER_TCP_PORT="1705"                     # JSON-RPC control (default snapcast)
SNAPSERVER_STREAM_PORT="1704"                  # client audio (default snapcast)

# Transient unit names (systemd-run --unit=...). Prefixed so they are obviously
# the spike's, never the (future) product snapserver.service.
UNIT_SERVER="jts-spike-snapserver"
UNIT_FEEDER="jts-spike-feeder"
UNIT_CLIENT_LEADER="jts-spike-client-leader"
UNIT_CLIENT_FOLLOWER="jts-spike-client-follower"
UNIT_CLIENT_SUB="jts-spike-client-sub"

# The sweep matrix (docs/HANDOFF-multiroom.md §8).
BUFFERS_MS=(150 300 500 800 1200)
CODECS=(pcm flac opus)

# How long the analyzer polls each cell for software-mode stats.
SETTLE_SEC="${JTS_SPIKE_SETTLE_SEC:-8}"
POLL_SEC="${JTS_SPIKE_POLL_SEC:-20}"

# Bounded-runner caps for the RAM/CPU snapshot on the 1 GB Pi. Mirrors
# scripts/pi-run-diagnostic.sh defaults but lighter — this is a read-only probe.
DIAG_MEM_HIGH="${JTS_SPIKE_DIAG_MEM_HIGH:-128M}"
DIAG_MEM_MAX="${JTS_SPIKE_DIAG_MEM_MAX:-192M}"
DIAG_RUNTIME_MAX="${JTS_SPIKE_DIAG_RUNTIME_MAX:-2min}"

# Results land here on the laptop so the analyzer + owner can read them.
RESULTS_DIR="${REPO_ROOT}/multiroom-spike"

# -----------------------------------------------------------------------------
# Host config. LEADER = PI_HOST (from .env.local). FOLLOWER/SUB via flags/env.
# -----------------------------------------------------------------------------
LEADER_HOST="${PI_HOST}"
LEADER_USER="${PI_USER}"
FOLLOWER_HOST="${JTS_SPIKE_FOLLOWER:-}"
SUB_HOST="${JTS_SPIKE_SUB:-}"
CLIENT_USER="${JTS_SPIKE_CLIENT_USER:-pi}"     # follower/sub SSH user

MUSIC_FILE="${JTS_SPIKE_MUSIC:-}"              # optional local music file to stage
LEADER_OUTPUT_DEV="${JTS_SPIKE_LEADER_OUTPUT:-default}"  # REAL device, never Loopback
NETEM_SPEC=""                                  # e.g. '50ms 10ms loss 1%'
DO_APT_INSTALL="${JTS_SPIKE_APT_INSTALL:-0}"
REFERENCE_ETHERNET=0

# -----------------------------------------------------------------------------
# Small helpers
# -----------------------------------------------------------------------------
log()  { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

# Build an SSH command array for a given host/user. We keep BatchMode off so an
# interactive operator can still get a sudo prompt the first time.
ssh_to() {
    local host="$1" user="$2"; shift 2
    ssh -o ConnectTimeout=8 "${user}@${host}" "$@"
}

leader_ssh()   { ssh_to "$LEADER_HOST"   "$LEADER_USER" "$@"; }
follower_ssh() { ssh_to "$FOLLOWER_HOST" "$CLIENT_USER" "$@"; }
sub_ssh()      { ssh_to "$SUB_HOST"      "$CLIENT_USER" "$@"; }

# Resolve the leader's LAN address as seen by the clients. We snapclient against
# the leader's mDNS name (LEADER_HOST) directly — clients dial that to connect.
leader_addr() { printf '%s' "$LEADER_HOST"; }

# -----------------------------------------------------------------------------
# apt guard — install snapserver/snapclient only when missing + only on consent
# -----------------------------------------------------------------------------
ensure_pkg() {
    # $1 = host, $2 = user, $3 = "snapserver"|"snapclient", $4 = binary name
    local host="$1" user="$2" pkg="$3" bin="$4"
    if ssh_to "$host" "$user" "command -v ${bin} >/dev/null 2>&1"; then
        log "[$host] ${bin} present"
        return 0
    fi
    log "[$host] ${bin} MISSING."
    if [[ "$DO_APT_INSTALL" != "1" ]]; then
        die "${pkg} is not installed on ${host}. Re-run with --apt-install (or
       JTS_SPIKE_APT_INSTALL=1) to allow:
         ssh ${user}@${host} sudo apt-get update && sudo apt-get install -y ${pkg}
       (left to you so the spike never silently mutates a host's package set)."
    fi
    log "[$host] installing ${pkg} via apt (consented)..."
    ssh_to "$host" "$user" "sudo apt-get update && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y ${pkg}"
}

# -----------------------------------------------------------------------------
# Test-source generation (on the LEADER): a known periodic CLICK track.
#
# WHY A CLICK, NOT A TONE: the acoustic analyzer recovers the L/R arrival
# offset from the AUTOCORRELATION of one mic hearing both speakers. A pure
# tone is self-similar at its own period (a 1 kHz sine repeats every 1.0 ms),
# so autocorrelation is AMBIGUOUS — it locks onto the tone period, not the L/R
# offset (verified: a tone reports the 1 ms cycle, not the injected delay). A
# broadband click is an impulse: its autocorrelation has ONE sharp peak, so the
# secondary peak is unambiguously the L/R path/clock offset. We use a 2 ms
# band-limited click (a few cycles of a short windowed noise burst) every
# 1.000 s at -20 dBFS, 48k/S16/stereo, IDENTICAL on L and R.
#
# Built with sox if present, else a tiny pure-stdlib WAV writer (no numpy).
# -----------------------------------------------------------------------------
make_chirp_remote() {
    if leader_ssh "command -v sox >/dev/null 2>&1"; then
        leader_ssh "sudo install -d -m 0777 ${SPIKE_TMP_DIR}; bash -s" <<REMOTE
set -eu
out='${CHIRP_WAV}'
# 60 s of: 2 ms broadband click (filtered noise burst) + 998 ms silence.
sox -n -r 48000 -c 2 -b 16 /tmp/_click.wav \
    synth 0.002 noise gain -20 sinc 300-7000
sox -n -r 48000 -c 2 -b 16 /tmp/_gap.wav synth 0.998 sine 0 gain -120
: > /tmp/_concat.txt
for i in \$(seq 1 60); do printf '/tmp/_click.wav\n/tmp/_gap.wav\n' >> /tmp/_concat.txt; done
sox \$(cat /tmp/_concat.txt) "\$out"
rm -f /tmp/_click.wav /tmp/_gap.wav /tmp/_concat.txt
REMOTE
    else
        # Pure-stdlib fallback shared with s0-sync-bench.sh. Stream the helper
        # over stdin so the Pi does not need a checkout or staged script file.
        leader_ssh \
            "sudo install -d -m 0777 ${SPIKE_TMP_DIR}; python3 - --format wav --output ${CHIRP_WAV}" \
            < "${SCRIPT_DIR}/_make_click_track.py"
    fi
    leader_ssh \
        "echo 'click track: ${CHIRP_WAV}' \"(\$(stat -c%s '${CHIRP_WAV}' 2>/dev/null || echo '?') bytes)\""
}

# -----------------------------------------------------------------------------
# Stage the operator's music file (optional) onto the leader for the ear check.
# -----------------------------------------------------------------------------
stage_music() {
    [[ -z "$MUSIC_FILE" ]] && return 0
    [[ -f "$MUSIC_FILE" ]] || die "music file not found: $MUSIC_FILE"
    log "staging music file onto leader..."
    scp -q "$MUSIC_FILE" "${LEADER_USER}@${LEADER_HOST}:${SPIKE_TMP_DIR}/music.src"
}

# -----------------------------------------------------------------------------
# snapserver.conf for one sweep cell. snapserver reads our FIFO as a `pipe`
# source. Buffer + codec are the two swept knobs. `mode=read` makes snapserver
# the FIFO reader (it opens for reading; the feeder opens for writing).
# -----------------------------------------------------------------------------
write_server_conf() {
    local buffer_ms="$1" codec="$2"
    leader_ssh "sudo install -d -m 0777 ${SPIKE_TMP_DIR}; cat > ${SNAPSERVER_CONF}" <<EOF
# jts-spike snapserver.conf (THROWAWAY, regenerated per sweep cell)
[stream]
source = pipe://${SNAPFIFO}?name=jts-spike&sampleformat=48000:16:2&mode=read
codec = ${codec}
buffer = ${buffer_ms}
chunk_ms = 20
# server.threads default; sampleformat fixed to match the FIFO + chirp track.

[http]
enabled = true
bind_to_address = 0.0.0.0
port = 1780

[tcp]
enabled = true
bind_to_address = 0.0.0.0
port = ${SNAPSERVER_TCP_PORT}

[server]
threads = -1
EOF
}

# -----------------------------------------------------------------------------
# Start/stop the spike snapserver + the FIFO feeder as TRANSIENT units. The
# feeder loops the chirp (and music, if staged) into the FIFO forever. We use
# `--collect` so units self-clean on stop; `--unit=` so teardown is name-exact.
# -----------------------------------------------------------------------------
start_server_and_feeder() {
    log "[leader] (re)starting spike snapserver + FIFO feeder..."
    leader_ssh "bash -s" <<REMOTE
set -eu
sudo install -d -m 0777 ${SPIKE_RUN_DIR}
# Kill any prior spike server/feeder (idempotent restart).
sudo systemctl reset-failed ${UNIT_SERVER}.service ${UNIT_FEEDER}.service 2>/dev/null || true
sudo systemctl stop ${UNIT_SERVER}.service ${UNIT_FEEDER}.service 2>/dev/null || true
# (Re)create the FIFO.
sudo rm -f ${SNAPFIFO}
mkfifo -m 0666 ${SNAPFIFO}

# snapserver as a transient unit reading our config.
sudo systemd-run --collect --unit=${UNIT_SERVER} \
    --property=Description='JTS spike snapserver' \
    snapserver -c ${SNAPSERVER_CONF}

# Feeder: loop the chirp (then music if present) into the FIFO as raw S16LE.
# 'sox ... -t raw' decodes any input to the FIFO sampleformat. We block until a
# reader (snapserver) is attached, so order doesn't matter.
feed_cmd='while true; do'
if [ -f ${SPIKE_TMP_DIR}/music.src ]; then
    feed_cmd="\$feed_cmd sox ${SPIKE_TMP_DIR}/music.src -r 48000 -c 2 -b 16 -t raw - 2>/dev/null > ${SNAPFIFO} || true;"
fi
feed_cmd="\$feed_cmd sox ${CHIRP_WAV} -r 48000 -c 2 -b 16 -t raw - 2>/dev/null > ${SNAPFIFO} || true; done"
sudo systemd-run --collect --unit=${UNIT_FEEDER} \
    --property=Description='JTS spike FIFO feeder' \
    bash -c "\$feed_cmd"
echo "snapserver + feeder up"
REMOTE
}

# Start a snapclient on a host as a transient unit. The leader's client MUST
# target a real output device (never Loopback) — invariant #2.
start_client() {
    local host="$1" user="$2" unit="$3" extra="$4" label="$5"
    [[ -z "$host" ]] && { log "[$label] no host configured — skipping"; return 0; }
    log "[$label] (re)starting snapclient on ${host} (extra: ${extra:-none})..."
    ssh_to "$host" "$user" "bash -s" <<REMOTE
set -eu
sudo systemctl reset-failed ${unit}.service 2>/dev/null || true
sudo systemctl stop ${unit}.service 2>/dev/null || true
sudo systemd-run --collect --unit=${unit} \
    --property=Description='JTS spike snapclient (${label})' \
    snapclient --host $(leader_addr) --port ${SNAPSERVER_STREAM_PORT} ${extra}
echo "snapclient (${label}) up"
REMOTE
}

start_all_clients() {
    # Leader's OWN localhost client → REAL device (never Loopback). Invariant #2.
    start_client "$LEADER_HOST" "$LEADER_USER" "$UNIT_CLIENT_LEADER" \
        "--soundcard ${LEADER_OUTPUT_DEV}" "leader-localhost"
    start_client "$FOLLOWER_HOST" "$CLIENT_USER" "$UNIT_CLIENT_FOLLOWER" "" "follower"
    start_client "$SUB_HOST"      "$CLIENT_USER" "$UNIT_CLIENT_SUB"      "" "sub"
}

# -----------------------------------------------------------------------------
# Optional WiFi stress on the leader's wlan0 (egress to clients). tc netem.
# NEVER touches Ethernet. Cleaned up by teardown (and harmless on reboot).
# -----------------------------------------------------------------------------
apply_netem() {
    [[ -z "$NETEM_SPEC" ]] && return 0
    log "[leader] applying tc netem '${NETEM_SPEC}' to wlan0 (WiFi stress)..."
    leader_ssh "sudo tc qdisc replace dev wlan0 root netem ${NETEM_SPEC}" \
        || log "WARN: tc netem failed (need 'sudo tc' + sch_netem); continuing without stress"
}
clear_netem() {
    leader_ssh "sudo tc qdisc del dev wlan0 root 2>/dev/null || true" || true
}

# -----------------------------------------------------------------------------
# RAM (Pss) + per-core CPU snapshot of snapserver+snapclient on the 1 GB Pi
# (the leader), via the BOUNDED runner idiom. Read-only; written to results.
# -----------------------------------------------------------------------------
budget_snapshot() {
    local buffer_ms="$1" codec="$2"
    local out="${RESULTS_DIR}/budget-${codec}-${buffer_ms}ms.txt"
    log "[leader] RAM/CPU budget snapshot (bounded) for ${codec}/${buffer_ms}ms..."
    leader_ssh "sudo systemd-run --pipe --wait --collect --quiet \
        --unit=jts-spike-budget-\$\$ \
        --property=MemoryMax=${DIAG_MEM_MAX} --property=MemoryHigh=${DIAG_MEM_HIGH} \
        --property=RuntimeMaxSec=${DIAG_RUNTIME_MAX} --property=OOMScoreAdjust=500 \
        bash -lc '
            echo \"=== snapserver/snapclient processes ===\";
            ps -o pid,comm,pcpu,rss -C snapserver -C snapclient 2>/dev/null || true;
            echo \"=== Pss (KB) from smaps_rollup ===\";
            for p in \$(pgrep -x snapserver) \$(pgrep -x snapclient); do
                pss=\$(awk \"/^Pss:/{s+=\\\$2} END{print s}\" /proc/\$p/smaps_rollup 2>/dev/null);
                comm=\$(cat /proc/\$p/comm 2>/dev/null);
                echo \"pid=\$p comm=\$comm pss_kb=\$pss\";
            done;
            echo \"=== per-core load (mpstat 1 2 if present) ===\";
            command -v mpstat >/dev/null 2>&1 && mpstat -P ALL 1 2 | tail -n +4 || cat /proc/loadavg;
            echo \"=== meminfo ===\";
            grep -E \"MemTotal|MemAvailable\" /proc/meminfo;
        '" > "$out" 2>&1 || log "WARN: budget snapshot failed (see $out)"
    log "  → ${out}"
}

# -----------------------------------------------------------------------------
# Poll JSON-RPC stats for one cell via the analyzer's software mode. The
# analyzer connects to snapserver's TCP control on the leader and summarizes
# reported per-client latency/offset. We write its JSON output to results.
# -----------------------------------------------------------------------------
poll_cell() {
    local buffer_ms="$1" codec="$2"
    local out="${RESULTS_DIR}/stats-${codec}-${buffer_ms}ms.json"
    log "settling ${SETTLE_SEC}s then polling ${POLL_SEC}s of JSON-RPC stats..."
    sleep "$SETTLE_SEC" 2>/dev/null || true   # foreground sleep is fine on the laptop
    python3 "${SCRIPT_DIR}/multiroom-spike-measure.py" software \
        --host "$LEADER_HOST" --port "$SNAPSERVER_TCP_PORT" \
        --poll-sec "$POLL_SEC" --buffer-ms "$buffer_ms" --codec "$codec" \
        --json-out "$out" \
        || log "WARN: software-mode poll failed for ${codec}/${buffer_ms}ms (see $out)"
}

# =============================================================================
# Subcommands
# =============================================================================
do_setup() {
    [[ -z "$LEADER_HOST" ]] && die "no leader host (PI_HOST unset and --leader not given)"
    mkdir -p "$RESULTS_DIR"
    log "SETUP: leader=${LEADER_HOST} follower=${FOLLOWER_HOST:-<none>} sub=${SUB_HOST:-<none>}"
    # Idempotent: clear any prior spike state before standing up fresh.
    do_teardown_quiet
    ensure_pkg "$LEADER_HOST"   "$LEADER_USER" snapserver snapserver
    ensure_pkg "$LEADER_HOST"   "$LEADER_USER" snapclient snapclient
    [[ -n "$FOLLOWER_HOST" ]] && ensure_pkg "$FOLLOWER_HOST" "$CLIENT_USER" snapclient snapclient
    [[ -n "$SUB_HOST"      ]] && ensure_pkg "$SUB_HOST"      "$CLIENT_USER" snapclient snapclient
    log "generating known chirp test track on leader..."
    make_chirp_remote
    stage_music
    log "SETUP complete. Next: bash scripts/multiroom-spike.sh --sweep"
}

do_sweep() {
    [[ -z "$LEADER_HOST" ]] && die "no leader host"
    mkdir -p "$RESULTS_DIR"
    apply_netem
    local total=$(( ${#BUFFERS_MS[@]} * ${#CODECS[@]} ))
    local i=0
    for codec in "${CODECS[@]}"; do
        for buffer_ms in "${BUFFERS_MS[@]}"; do
            i=$((i+1))
            log "===== cell ${i}/${total}: codec=${codec} buffer=${buffer_ms}ms ====="
            write_server_conf "$buffer_ms" "$codec"
            start_server_and_feeder
            start_all_clients
            poll_cell "$buffer_ms" "$codec"
            budget_snapshot "$buffer_ms" "$codec"
        done
    done
    clear_netem
    log "SWEEP complete. Raw cells in ${RESULTS_DIR}/."
    log "Summarizing + PASS/FAIL verdict:"
    python3 "${SCRIPT_DIR}/multiroom-spike-measure.py" summarize --results-dir "$RESULTS_DIR"
}

do_record_chirp() {
    local secs="${1:-12}"
    [[ -z "$LEADER_HOST" ]] && die "no leader host"
    log "ACOUSTIC GROUND TRUTH — single-mic recording for ${secs}s."
    cat >&2 <<EOF
  Position ONE mic equidistant-ish between the L and R speakers (or wherever
  you sit). The L/R pair must currently be playing the chirp (run --sweep, or
  --setup then start a single cell). Recording from the leader's default mic.
  After this records, analyze with:
    python3 scripts/multiroom-spike-measure.py acoustic --wav <path printed below>
EOF
    local remote_wav local_wav
    remote_wav="${SPIKE_TMP_DIR}/acoustic-$(date -u +%H%M%SZ).wav"
    leader_ssh "arecord -d ${secs} -f S16_LE -r 48000 -c 1 ${remote_wav} && echo recorded"
    local_wav="${RESULTS_DIR}/$(basename "$remote_wav")"
    mkdir -p "$RESULTS_DIR"
    scp -q "${LEADER_USER}@${LEADER_HOST}:${remote_wav}" "$local_wav"
    log "recorded WAV pulled to: ${local_wav}"
    echo "$local_wav"
}

# Teardown: remove every transient unit, FIFO, conf, and netem qdisc we made.
# Safe to run anytime; never touches jasper-* or product units.
do_teardown_quiet() {
    for host_user in "$LEADER_HOST:$LEADER_USER" "$FOLLOWER_HOST:$CLIENT_USER" "$SUB_HOST:$CLIENT_USER"; do
        local host="${host_user%%:*}" user="${host_user##*:}"
        [[ -z "$host" ]] && continue
        ssh_to "$host" "$user" "bash -s" <<REMOTE 2>/dev/null || true
sudo systemctl stop ${UNIT_SERVER}.service ${UNIT_FEEDER}.service \
    ${UNIT_CLIENT_LEADER}.service ${UNIT_CLIENT_FOLLOWER}.service ${UNIT_CLIENT_SUB}.service 2>/dev/null || true
sudo systemctl reset-failed ${UNIT_SERVER}.service ${UNIT_FEEDER}.service \
    ${UNIT_CLIENT_LEADER}.service ${UNIT_CLIENT_FOLLOWER}.service ${UNIT_CLIENT_SUB}.service 2>/dev/null || true
sudo rm -f ${SNAPFIFO}
sudo tc qdisc del dev wlan0 root 2>/dev/null || true
REMOTE
    done
}

do_teardown() {
    log "TEARDOWN: stopping all jts-spike units + removing FIFO/netem on all hosts..."
    do_teardown_quiet
    log "TEARDOWN complete. (Generated confs/tracks under ${SPIKE_TMP_DIR} on the"
    log " leader are left in place for inspection; they vanish on reboot if you"
    log " prefer — or remove with: ssh leader 'sudo rm -rf ${SPIKE_TMP_DIR}'.)"
}

usage() {
    awk '
        /^# SPDX-License-Identifier:/ { after_spdx = 1; next }
        !after_spdx { next }
        !in_docs {
            if ($0 ~ /^#/) in_docs = 1
            else next
        }
        /^#/ { sub(/^# ?/, ""); print; next }
        { exit }
    ' "$0" >&2
}

# =============================================================================
# Arg parse
# =============================================================================
ACTION=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --setup)              ACTION="setup" ;;
        --sweep)              ACTION="sweep" ;;
        --teardown)           ACTION="teardown" ;;
        --record-chirp)       ACTION="record-chirp"; RECORD_SECS="${2:-12}"; shift ;;
        --leader)             LEADER_HOST="$2"; shift ;;
        --follower)           FOLLOWER_HOST="$2"; shift ;;
        --sub)                SUB_HOST="$2"; shift ;;
        --music)              MUSIC_FILE="$2"; shift ;;
        --leader-output)      LEADER_OUTPUT_DEV="$2"; shift ;;
        --netem)              NETEM_SPEC="$2"; shift ;;
        --apt-install)        DO_APT_INSTALL="1" ;;
        --reference-ethernet) REFERENCE_ETHERNET=1 ;;
        -h|--help)            usage; exit 0 ;;
        *) die "unknown arg: $1 (try --help)" ;;
    esac
    shift
done

# A reference run measures the SAME sweep over Ethernet (operator plugs
# the cable; the harness doesn't switch transports). The flag's job is
# to keep the best-case reference line in its own results dir so it
# never overwrites — or gets summarized together with — the WiFi cells
# the spike actually exists to judge.
if [[ "$REFERENCE_ETHERNET" == "1" ]]; then
    RESULTS_DIR="${RESULTS_DIR}-ethernet-reference"
    log "REFERENCE-ETHERNET run: results land in ${RESULTS_DIR}"
fi

[[ -z "$ACTION" ]] && { usage; exit 2; }

case "$ACTION" in
    setup)        do_setup ;;
    sweep)        do_sweep ;;
    record-chirp) do_record_chirp "${RECORD_SECS:-12}" ;;
    teardown)     do_teardown ;;
esac
