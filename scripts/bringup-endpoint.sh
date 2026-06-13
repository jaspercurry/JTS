#!/usr/bin/env bash
# Bring up a Raspberry Pi Zero 2 W as a JTS dumb audio endpoint.
#
# This is the paved laptop-side path for a freshly imaged endpoint:
# reuse onboard.sh for SSH/state, deploy the endpoint install tier, reboot
# once if boot-time memory/zram changes need it, then print a concise
# endpoint hardware/software report.
#
# Usage:
#   bash scripts/bringup-endpoint.sh jts4.local --adopt
#   bash scripts/bringup-endpoint.sh 192.168.1.162 --adopt --speaker-hostname jts4.local
#   bash scripts/bringup-endpoint.sh jts4.local --skip-onboard
#   bash scripts/bringup-endpoint.sh jts4.local --no-reboot
#   bash scripts/bringup-endpoint.sh --help
#
# Notes:
#   - No audible playback test is run. The final report shows the DAC and
#     Snapclient state; a human still chooses when to play a test tone.
#   - PI_HOST is the SSH transport target. JASPER_HOSTNAME is the endpoint
#     identity; pass --speaker-hostname when connecting by IP.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_lib.sh
. "${SCRIPT_DIR}/_lib.sh"

HOST=""
USER_ARG=""
SPEAKER_HOSTNAME_ARG=""
ADOPT=0
RUN_ONBOARD=1
RUN_DEPLOY=1
REBOOT_IF_NEEDED=1
ACTIVE_HOST=""
KNOWN_IP=""
SPEAKER_HOSTNAME=""

usage() {
    sed -E -n '/^# Usage:/,/^$/p' "$0" | sed -E 's/^# ?//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --adopt) ADOPT=1; shift ;;
        --user)
            if [[ $# -lt 2 ]]; then
                echo "bringup-endpoint: --user requires a value" >&2
                exit 2
            fi
            USER_ARG="$2"; shift 2
            ;;
        --speaker-hostname)
            if [[ $# -lt 2 ]]; then
                echo "bringup-endpoint: --speaker-hostname requires a value" >&2
                exit 2
            fi
            SPEAKER_HOSTNAME_ARG="$2"; shift 2
            ;;
        --skip-onboard) RUN_ONBOARD=0; shift ;;
        --skip-deploy) RUN_DEPLOY=0; shift ;;
        --no-reboot) REBOOT_IF_NEEDED=0; shift ;;
        --help|-h)
            usage
            exit 0
            ;;
        -*)
            echo "bringup-endpoint: unknown flag: $1 (try --help)" >&2
            exit 2
            ;;
        *)
            if [[ -z "$HOST" ]]; then
                HOST="$1"
            else
                echo "bringup-endpoint: unexpected positional arg: $1" >&2
                exit 2
            fi
            shift
            ;;
    esac
done

if [[ -z "$HOST" ]]; then
    echo "bringup-endpoint: hostname or IP required" >&2
    echo "                  try: bash scripts/bringup-endpoint.sh jts4.local --adopt" >&2
    exit 2
fi

if [[ -n "$USER_ARG" ]]; then
    PI_USER="$USER_ARG"
fi

log_event() {
    local phase="$1" status="$2"
    shift 2
    if [[ $# -gt 0 ]]; then
        printf 'event=endpoint_bringup.%s status=%s %s\n' "$phase" "$status" "$*"
    else
        printf 'event=endpoint_bringup.%s status=%s\n' "$phase" "$status"
    fi
}

resolve_ipv4() {
    local host="$1"
    if is_ipv4_host "$host"; then
        printf '%s\n' "$host"
        return 0
    fi
    if command -v dscacheutil >/dev/null 2>&1; then
        dscacheutil -q host -a name "$host" 2>/dev/null \
            | awk '/ip_address:/ && $2 !~ /^169\.254\./ { print $2; exit }'
        return 0
    fi
    if command -v getent >/dev/null 2>&1; then
        getent ahostsv4 "$host" 2>/dev/null | awk '{ print $1; exit }'
        return 0
    fi
    if command -v dig >/dev/null 2>&1; then
        dig +short "$host" A 2>/dev/null | awk 'NF { print; exit }'
        return 0
    fi
    return 0
}

refresh_known_ip() {
    local resolved
    resolved="$(resolve_ipv4 "$HOST" || true)"
    if [[ -n "$resolved" ]]; then
        KNOWN_IP="$resolved"
    fi
}

ssh_probe_host() {
    local host="$1"
    ssh -o BatchMode=yes \
        -o ConnectTimeout=5 \
        -o ConnectionAttempts=1 \
        -o StrictHostKeyChecking=accept-new \
        "${PI_USER}@${host}" true >/dev/null 2>&1
}

choose_active_host() {
    refresh_known_ip
    if ssh_probe_host "$HOST"; then
        ACTIVE_HOST="$HOST"
        return 0
    fi
    if [[ -n "$KNOWN_IP" && "$KNOWN_IP" != "$HOST" ]] && ssh_probe_host "$KNOWN_IP"; then
        ACTIVE_HOST="$KNOWN_IP"
        echo "    using ${KNOWN_IP} for SSH because ${HOST} did not answer"
        log_event mdns fallback "host=${HOST} ip=${KNOWN_IP}"
        return 0
    fi
    return 1
}

ssh_active() {
    local command="$1"
    if [[ -z "$ACTIVE_HOST" ]]; then
        choose_active_host >/dev/null
    fi
    ssh -o BatchMode=yes \
        -o ConnectTimeout=8 \
        -o ConnectionAttempts=1 \
        -o StrictHostKeyChecking=accept-new \
        "${PI_USER}@${ACTIVE_HOST}" "$command"
}

ssh_active_tty() {
    local command="$1"
    ssh -tt \
        -o ConnectTimeout=8 \
        -o ConnectionAttempts=1 \
        -o StrictHostKeyChecking=accept-new \
        "${PI_USER}@${ACTIVE_HOST}" "$command"
}

remote_sudo() {
    local command="$1"
    if ssh_active 'sudo -n true' >/dev/null 2>&1; then
        ssh_active "sudo -n ${command}"
        return $?
    fi
    if [[ -t 0 ]]; then
        ssh_active_tty "sudo ${command}"
        return $?
    fi
    return 1
}

remote_sudo_shell() {
    local command="$1"
    local wrapped
    wrapped="sh -c $(shell_quote "$command")"
    if ssh_active 'sudo -n true' >/dev/null 2>&1; then
        ssh_active "sudo -n ${wrapped}"
        return $?
    fi
    if [[ -t 0 ]]; then
        ssh_active_tty "sudo ${wrapped}"
        return $?
    fi
    return 1
}

remote_sudo_reboot() {
    if ssh_active 'sudo -n true' >/dev/null 2>&1; then
        ssh_active "sudo -n reboot" || true
        return 0
    fi
    if [[ -t 0 ]]; then
        ssh_active_tty "sudo reboot" || true
        return 0
    fi
    return 1
}

resolve_speaker_identity() {
    local normalized remote_hostname
    if [[ -n "$SPEAKER_HOSTNAME_ARG" ]]; then
        if ! normalized="$(normalize_speaker_hostname "$SPEAKER_HOSTNAME_ARG")"; then
            echo "bringup-endpoint: --speaker-hostname must be a hostname, not an IP: ${SPEAKER_HOSTNAME_ARG}" >&2
            exit 2
        fi
        printf '%s\n' "$normalized"
        return 0
    fi
    if [[ "$RUN_ONBOARD" == "0" && -n "${JASPER_HOSTNAME:-}" ]]; then
        if normalized="$(normalize_speaker_hostname "$JASPER_HOSTNAME" 2>/dev/null)"; then
            printf '%s\n' "$normalized"
            return 0
        fi
    fi
    if ! is_ipv4_host "$HOST"; then
        normalize_speaker_hostname "$HOST"
        return 0
    fi
    choose_active_host >/dev/null
    remote_hostname="$(ssh_active 'hostname -s 2>/dev/null || hostname')"
    remote_hostname="${remote_hostname%%$'\n'*}"
    if ! normalized="$(normalize_speaker_hostname "$remote_hostname")"; then
        cat <<EOF >&2
bringup-endpoint: connected by IP, but the Pi reported an unusable hostname:
    ${remote_hostname}

Re-run with the intended endpoint identity:
    bash scripts/bringup-endpoint.sh ${HOST} --speaker-hostname jts4.local
EOF
        exit 1
    fi
    printf '%s\n' "$normalized"
}

reboot_reasons() {
    ssh_active 'reasons=""; controllers="$(cat /sys/fs/cgroup/cgroup.controllers 2>/dev/null || true)"; case " ${controllers} " in *" memory "*) ;; *) reasons="${reasons} cgroup-memory";; esac; if [ -r /sys/block/zram0/disksize ]; then mem_kb=$(awk "/MemTotal:/ {print \$2}" /proc/meminfo); zram_bytes=$(cat /sys/block/zram0/disksize 2>/dev/null || echo 0); limit=$((mem_kb * 1024 * 60 / 100)); if [ "${zram_bytes:-0}" -gt "${limit:-0}" ]; then reasons="${reasons} zram-size"; fi; fi; printf "%s\n" "${reasons# }"'
}

wait_for_ssh() {
    local attempt
    echo "==> waiting for ${HOST} to come back"
    for attempt in $(seq 1 60); do
        sleep 3
        if choose_active_host >/dev/null; then
            echo "    SSH is back (${PI_USER}@${ACTIVE_HOST})"
            log_event reboot_wait ok "attempt=${attempt} ssh_host=${ACTIVE_HOST}"
            return 0
        fi
        if (( attempt % 10 == 0 )); then
            echo "    still waiting (${attempt}/60)"
        fi
    done
    log_event reboot_wait fail "host=${HOST}"
    return 1
}

summarize_doctor_json() {
    python3 -c '
import json, sys
payload = json.load(sys.stdin)
print("  doctor: {} failed / {} warnings".format(
    payload.get("fails", 0), payload.get("warns", 0)
))
interesting = [r for r in payload.get("results", []) if r.get("status") in {"fail", "warn"}]
for row in interesting[:12]:
    print("    [{}] {}: {}".format(
        row.get("status"), row.get("name"), row.get("detail")
    ))
if len(interesting) > 12:
    print("    ... {} more non-ok checks".format(len(interesting) - 12))
'
}

print_endpoint_report() {
    local doctor_json wifi_report
    echo
    echo "==> endpoint report (${PI_USER}@${ACTIVE_HOST}, identity ${SPEAKER_HOSTNAME})"
    ssh_active 'printf "  hostname: "; hostname -f 2>/dev/null || hostname; printf "  profile: "; cat /var/lib/jasper/install_profile 2>/dev/null || echo unknown; printf "  build: "; sed -n "s/^JASPER_GIT_SHA=//p" /var/lib/jasper/build.txt 2>/dev/null | head -1'
    ssh_active 'printf "  healthz: "; curl -fsS --max-time 3 http://127.0.0.1:8780/healthz 2>/dev/null || printf "unavailable"; printf "\n"'
    ssh_active 'printf "  snapclient: "; snapclient --version 2>&1 | head -1 || echo missing'
    ssh_active 'for unit in jasper-control.service jasper-grouping-reconcile.service jasper-snapclient.service jasper-snapserver.service; do printf "  %-34s" "${unit}:"; systemctl is-active "$unit" 2>/dev/null || true; done'
    echo "  ALSA playback cards:"
    ssh_active 'aplay -l 2>/dev/null | sed "s/^/    /" || echo "    aplay unavailable or no playback cards"'
    echo "  boot audio contract:"
    ssh_active 'controllers="$(cat /sys/fs/cgroup/cgroup.controllers 2>/dev/null || true)"; case " ${controllers} " in *" memory "*) memory_ctl=yes ;; *) memory_ctl=no ;; esac; zram="absent"; [ -r /sys/block/zram0/disksize ] && zram="$(cat /sys/block/zram0/disksize)"; printf "    cgroup memory=%s zram_bytes=%s\n" "$memory_ctl" "$zram"'
    echo "  WiFi guardian:"
    if wifi_report="$(remote_sudo_shell 'if [ -f /var/lib/jasper/wifi_guardian.env ]; then sed -n "s/^JASPER_WIFI_SSID=/    stash_ssid=/p; s/^JASPER_WIFI_KEY_MGMT=/    key_mgmt=/p" /var/lib/jasper/wifi_guardian.env; else echo "    stash=missing"; fi' 2>/dev/null)"; then
        printf '%s\n' "$wifi_report"
    else
        echo "    skipped (sudo unavailable)"
    fi
    if doctor_json="$(remote_sudo '/opt/jasper/.venv/bin/jasper-doctor --json' 2>/dev/null)"; then
        printf '%s\n' "$doctor_json" | summarize_doctor_json
    else
        echo "  doctor: skipped (sudo unavailable); run:"
        echo "    ssh ${PI_USER}@${ACTIVE_HOST} 'sudo /opt/jasper/.venv/bin/jasper-doctor'"
    fi
    echo "  audio test: not run automatically; use a quiet speaker-test when safe"
}

echo "==> endpoint bring-up target"
echo "    SSH target: ${PI_USER}@${HOST}"

if [[ "$RUN_ONBOARD" == "1" ]]; then
    onboard_args=("$HOST" "--no-install")
    if [[ "$ADOPT" == "1" ]]; then
        onboard_args+=("--adopt")
    fi
    if [[ -n "$USER_ARG" ]]; then
        onboard_args+=("--user" "$USER_ARG")
    fi
    if [[ -n "$SPEAKER_HOSTNAME_ARG" ]]; then
        onboard_args+=("--speaker-hostname" "$SPEAKER_HOSTNAME_ARG")
    fi
    echo
    echo "==> onboard SSH/state only"
    bash "${SCRIPT_DIR}/onboard.sh" "${onboard_args[@]}"
    log_event onboard ok
fi

SPEAKER_HOSTNAME="$(resolve_speaker_identity)"
echo "    endpoint identity: ${SPEAKER_HOSTNAME}"
choose_active_host >/dev/null || {
    echo "bringup-endpoint: cannot reach ${PI_USER}@${HOST} by SSH" >&2
    exit 1
}

if [[ "$RUN_DEPLOY" == "1" ]]; then
    deploy_host="$ACTIVE_HOST"
    echo
    echo "==> deploy endpoint profile"
    echo "    PI_HOST=${deploy_host} JASPER_HOSTNAME=${SPEAKER_HOSTNAME}"
    if ! PI_HOST="$deploy_host" \
         PI_USER="$PI_USER" \
         JASPER_HOSTNAME="$SPEAKER_HOSTNAME" \
         JASPER_INSTALL_PROFILE=endpoint \
         bash "${SCRIPT_DIR}/deploy-to-pi.sh"; then
        log_event deploy fail
        echo "bringup-endpoint: endpoint deploy failed; see output above" >&2
        exit 1
    fi
    log_event deploy ok "ssh_host=${deploy_host}"
fi

choose_active_host >/dev/null
needs_reboot="$(reboot_reasons || true)"
if [[ -n "$needs_reboot" ]]; then
    echo
    echo "==> reboot contract pending: ${needs_reboot}"
    if [[ "$REBOOT_IF_NEEDED" == "1" ]]; then
        echo "    rebooting once so kernel/zram changes take effect"
        log_event reboot start "reason=${needs_reboot// /,}"
        remote_sudo_reboot || {
            log_event reboot fail "reason=sudo_unavailable"
            echo "bringup-endpoint: reboot needed but sudo was unavailable" >&2
            echo "                  run with --no-reboot to leave it manual" >&2
            exit 1
        }
        ACTIVE_HOST=""
        wait_for_ssh
        post_reboot_reasons="$(reboot_reasons || true)"
        if [[ -n "$post_reboot_reasons" ]]; then
            log_event reboot warn "still_pending=${post_reboot_reasons// /,}"
            echo "    warning: reboot contract still pending: ${post_reboot_reasons}"
        else
            log_event reboot ok
        fi
    else
        log_event reboot skipped "reason=${needs_reboot// /,}"
        echo "    --no-reboot set; reboot manually before relying on memory/zram checks"
    fi
else
    log_event reboot not_needed
fi

choose_active_host >/dev/null
print_endpoint_report

cat <<EOF

----------------------------------------------------------------
JTS endpoint bring-up complete: ${SPEAKER_HOSTNAME}

  SSH transport:     ${PI_USER}@${ACTIVE_HOST}
  Endpoint identity: ${SPEAKER_HOSTNAME}
  Management health: http://${SPEAKER_HOSTNAME}:8780/healthz once mDNS resolves

Next product step: bond this endpoint from the leader once the group
render path is ready. Until then, it should sit idle and quiet.
----------------------------------------------------------------
EOF
