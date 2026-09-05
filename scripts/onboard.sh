#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# JTS Pi onboarding — single command from "I flashed a Pi" to
# "speaker is running." Run on the laptop, after Pi Imager has
# flashed the Pi and the Pi has booted and joined the LAN.
#
# Prerequisites:
#   - Raspberry Pi Imager 2.0.6 or later (older 2.0.x have an open
#     pubkey-breaks-customization bug on Trixie — see QUICKSTART.md)
#   - Imager's OS Customization sets hostname + WiFi + a password.
#     The beginner/friendly path uses --adopt to install this laptop's
#     pubkey with ssh-copy-id. Pre-populated pubkey SSH without
#     --adopt is the advanced/unattended path.
#   - Pi powered on, joined WiFi, reachable on the LAN
#   - A local SSH keypair (~/.ssh/id_ed25519.pub or id_rsa.pub)
#
# Usage:
#   # Beginner/friendly path (password SSH once, then pubkey forever):
#   bash scripts/onboard.sh jts.local --adopt
#   bash scripts/onboard.sh 192.168.1.55 --adopt --speaker-hostname jts.local
#
#   # Advanced/unattended path (Pi already has your pubkey):
#   bash scripts/onboard.sh jts.local
#   bash scripts/onboard.sh jts2.local             # multi-Pi: same command, new host
#
#   bash scripts/onboard.sh jts.local --user pi    # advanced; see note below
#   bash scripts/onboard.sh jts.local --no-install # state-only, skip install.sh
#   bash scripts/onboard.sh --help
#
# User boundary: the beginner/fresh-appliance path is username `pi`.
# --user / PI_USER is advanced and currently supported for onboarding
# and deploy only; some diagnostics/operator scripts still assume `pi`
# or `/home/pi`.
#
# Advanced CI / headless mode: for fully unattended re-imaging,
# pre-populate the Pi's ~/.ssh/authorized_keys via Pi Imager's pubkey
# field, enable passwordless sudo, and omit --adopt; deploy-to-pi.sh
# explicitly preflights `sudo -n true` before upload. Friendly mode
# adopts a password-only Pi and can later prompt for sudo over ssh -tt
# without storing the password. The structured `event=onboard.<phase>`
# log lines parse with the same tools that consume the Pi-side daemon
# logs.
#
# What it does, in order:
#   1. probe        — pings hostname; on failure, prints the four-rung
#                     fallback ladder (Imager version, router page,
#                     arp scan, USB-C gadget rescue)
#   2. auth         — verifies pubkey SSH; --adopt runs ssh-copy-id
#                     first (interactive password prompt) for password-
#                     only Pis being adopted
#   3. persist      — appends Host alias to ~/.ssh/config (idempotent),
#                     writes .env.local and CLAUDE.local.md at the repo
#                     root (both gitignored). PI_HOST is the SSH target;
#                     JASPER_HOSTNAME is the speaker identity. If the
#                     SSH target is an IP, query the Pi hostname or use
#                     --speaker-hostname.
#   4. install      — calls scripts/deploy-to-pi.sh (duration varies by
#                     hardware; native source builds dominate)
#   5. validate     — runs jasper-doctor on the Pi and surfaces verdict
#
# Idempotency contract: running this twice in a row is a no-op that
# re-verifies state. If something is already configured we don't
# re-do it; we confirm and move on. The Host alias in ~/.ssh/config
# is preserved if already present; .env.local and CLAUDE.local.md
# are unconditionally rewritten (they're cheap and the only writers
# are this script + scripts/use); install.sh is idempotent itself.
# Pass --no-install to skip the deploy step on subsequent runs.
#
# Observability: each phase emits a structured `event=onboard.<phase>
# status=<ok|fail|...>` line alongside the human-readable `==>`
# headers, so a `bash scripts/onboard.sh ... 2>&1 | tee logs/x.log`
# can later be grep-debugged with the same convention as the
# Pi-side daemons.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_lib.sh
JTS_LIB_TARGET_OPTIONAL=1 . "${SCRIPT_DIR}/_lib.sh"  # the target is this script's argument, not _lib.sh's

# ---- argument parsing ---------------------------------------------------

HOST=""
USER_ARG=""
SPEAKER_HOSTNAME_ARG=""
ADOPT=0
NO_INSTALL=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --adopt)         ADOPT=1; shift ;;
        --user)
            if [[ $# -lt 2 ]]; then
                echo "onboard: --user requires a value" >&2
                exit 2
            fi
            USER_ARG="$2"; shift 2
            ;;
        --speaker-hostname)
            if [[ $# -lt 2 ]]; then
                echo "onboard: --speaker-hostname requires a value" >&2
                exit 2
            fi
            SPEAKER_HOSTNAME_ARG="$2"; shift 2
            ;;
        --no-install)    NO_INSTALL=1; shift ;;
        --help|-h)
            # Print the in-source Usage block. ERE for portability —
            # BSD sed (macOS) doesn't support GNU's `\?` quantifier in BRE.
            sed -E -n '/^# Usage:/,/^$/p' "$0" | sed -E 's/^# ?//'
            exit 0
            ;;
        -*)
            echo "onboard: unknown flag: $1 (try --help)" >&2
            exit 2
            ;;
        *)
            if [[ -z "$HOST" ]]; then
                HOST="$1"
            else
                echo "onboard: unexpected positional arg: $1" >&2
                exit 2
            fi
            shift
            ;;
    esac
done

if [[ -z "$HOST" ]]; then
    echo "onboard: hostname required (try: bash scripts/onboard.sh jts.local --adopt)" >&2
    echo "        Run with --help for full usage." >&2
    exit 2
fi

# Override PI_USER from --user if provided. _lib.sh already exported
# PI_USER from .env.local + fallback chain; --user takes precedence.
if [[ -n "$USER_ARG" ]]; then
    PI_USER="$USER_ARG"
fi

# Alias derivation: hostname.local → hostname. Skip alias for raw
# IPs (an `ssh 192-168-1-55` alias would be more confusing than
# helpful — just use the raw IP).
if is_ipv4_host "$HOST"; then
    ALIAS=""
    IS_IP=1
else
    ALIAS="${HOST%%.*}"
    IS_IP=0
fi

SSH_CONFIG="${HOME}/.ssh/config"

# Structured logging helper: one event= line per phase boundary,
# alongside the human-readable `==>` headers. Matches the Pi-side
# `event=<module>.<action> status=<s>` convention so a captured
# log file is greppable by the same tools (see AGENTS.md).
log_event() {
    # $1 phase, $2 status, $3+ optional key=value detail (space-separated)
    local phase="$1" status="$2"
    shift 2
    if [[ $# -gt 0 ]]; then
        printf 'event=onboard.%s status=%s %s\n' "$phase" "$status" "$*"
    else
        printf 'event=onboard.%s status=%s\n' "$phase" "$status"
    fi
}

# True iff sudo works without a password prompt — shared by the reboot
# phase and the final doctor phase below, both of which need to run a
# privileged remote command non-interactively when they can, and fall
# back to an interactive `ssh -tt` when they can't.
pi_has_passwordless_sudo() {
    local host="$1" user="$2"
    ssh -o BatchMode=yes "${user}@${host}" 'sudo -n true' >/dev/null 2>&1
}

# Bounded wait for a reboot we just triggered (issue #2110). Two phases:
# first the link visibly dropping (bounded — a reboot that never drops
# the link, e.g. it was already down, must not block the second phase
# forever), then SSH + jasper-control both answering again on the far
# side of the reboot. ~30s + up to ~4min ceiling — comfortably above a
# Pi 5's typical reboot but never open-ended.
wait_for_pi_down() {
    local host="$1" attempt
    local attempts="${JTS_ONBOARD_REBOOT_DOWN_ATTEMPTS:-15}"
    local sleep_s="${JTS_ONBOARD_REBOOT_DOWN_SLEEP:-2}"
    for ((attempt = 1; attempt <= attempts; attempt++)); do
        ping -c 1 -W 2 "$host" >/dev/null 2>&1 || return 0
        sleep "$sleep_s"
    done
    return 0
}

wait_for_pi_up() {
    local host="$1" user="$2" attempt
    local attempts="${JTS_ONBOARD_REBOOT_UP_ATTEMPTS:-48}"
    local sleep_s="${JTS_ONBOARD_REBOOT_UP_SLEEP:-5}"
    for ((attempt = 1; attempt <= attempts; attempt++)); do
        if ping -c 1 -W 2 "$host" >/dev/null 2>&1 \
                && ssh -o BatchMode=yes -o ConnectTimeout=5 \
                    -o StrictHostKeyChecking=accept-new "${user}@${host}" \
                    'curl -fsS -m 3 http://127.0.0.1:8780/healthz' \
                    >/dev/null 2>&1; then
            return 0
        fi
        sleep "$sleep_s"
    done
    return 1
}

# ---- preflight: laptop has what we need --------------------------------

echo "==> preflight"
PUBKEYS=( "${HOME}/.ssh/id_ed25519.pub" "${HOME}/.ssh/id_rsa.pub" "${HOME}/.ssh/id_ecdsa.pub" )
HAVE_PUBKEY=0
for k in "${PUBKEYS[@]}"; do
    if [[ -f "$k" ]]; then
        HAVE_PUBKEY=1
        echo "    found ${k}"
        break
    fi
done
if [[ "$HAVE_PUBKEY" == "0" ]]; then
    cat <<EOF >&2

No SSH pubkey found at ~/.ssh/id_{ed25519,rsa,ecdsa}.pub.

Generate one (recommended: ed25519):
    ssh-keygen -t ed25519 -C "\$(whoami)@\$(hostname -s)-jts"

Then re-run this script with --adopt to install the key over a
password-authenticated SSH session:

    bash scripts/onboard.sh ${HOST:-jts.local} --adopt

For advanced/unattended rebuilds, you can instead re-flash with Pi
Imager, pre-populate the pubkey in OS Customization → SSH, enable
passwordless sudo, and run without --adopt.
EOF
    exit 1
fi

for tool in ssh rsync ping; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        log_event preflight fail "missing_tool=$tool"
        echo "onboard: missing required tool: $tool" >&2
        exit 1
    fi
done
log_event preflight ok "host=${HOST} user=${PI_USER}"

# ---- phase 1: probe reachability ---------------------------------------

echo "==> probe ${PI_USER}@${HOST}"
# -c 1 (one packet) -W 2 (2 sec timeout). The BSD ping on macOS and
# GNU ping on Linux both accept these flags.
if ! ping -c 1 -W 2 "$HOST" >/dev/null 2>&1; then
    cat <<EOF >&2
    not reachable

Could not reach ${HOST}. Try (in order):

  1. Check Pi Imager version. **Required: 2.0.6 or later.** Imager
     2.0.0-2.0.5 have an open bug where selecting public-key auth
     silently breaks all OS customization on Trixie images — the Pi
     boots into the first-boot wizard expecting keyboard+monitor.
     Update Imager, re-flash, and try again. See QUICKSTART.md.

  2. Find the Pi's IP from your router's admin page (look for a
     hostname like "raspberrypi" or whatever you set in Imager).
     Then re-run with the IP:
         bash scripts/onboard.sh 192.168.1.X --adopt --speaker-hostname jts.local

  3. ARP scan for Pi MAC OUIs from your laptop:
         arp -a | grep -iE 'b8:27:eb|d8:3a:dd|dc:a6:32|2c:cf:67'

  4. Raspberry Pi OS USB-C rescue gadget (Pi 5 only, WiFi unreachable case):
         - Power the Pi from the GPIO 5V/GND header (NOT USB-C)
         - Connect a USB-A→USB-C cable laptop → Pi USB-C port
         - Use USB-A→USB-C, NOT USB-C→USB-C (kernel bug #6289)
         - A fresh Pi Imager rescue gadget appears at 10.12.194.1 (this is
           separate from JTS's installed, per-speaker USB management /30):
             bash scripts/onboard.sh 10.12.194.1
EOF
    log_event probe fail "reason=no_ping host=${HOST}"
    exit 1
fi
echo "    reachable"
log_event probe ok "host=${HOST}"

# ---- phase 2: auth -----------------------------------------------------

if [[ "$ADOPT" == "1" ]]; then
    echo "==> ssh-copy-id ${PI_USER}@${HOST} (you'll be prompted for the password)"
    log_event adopt start "host=${HOST} user=${PI_USER}"
    if ! ssh-copy-id -o StrictHostKeyChecking=accept-new "${PI_USER}@${HOST}"; then
        if ssh_host_key_changed_advice "${PI_USER}@${HOST}"; then
            log_event adopt fail "reason=host_key_changed host=${HOST}"
            exit 1
        fi
        log_event adopt fail "ssh-copy-id_exited_nonzero"
        echo "onboard: ssh-copy-id failed (exit nonzero); cannot continue" >&2
        exit 1
    fi
    echo "    pubkey installed"
    log_event adopt ok "host=${HOST}"
fi

echo "==> verify pubkey SSH"
# BatchMode=yes disables interactive password prompts so we get a
# fast deterministic failure if pubkey auth doesn't work. ssh's stderr
# is captured rather than discarded: it is the only place ssh says the
# host key CHANGED rather than that auth failed (issue #2114).
AUTH_ERR=""
if ! AUTH_ERR="$(ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new \
       "${PI_USER}@${HOST}" true 2>&1 >/dev/null)"; then
    if ssh_host_key_changed_advice "${PI_USER}@${HOST}" "$AUTH_ERR"; then
        log_event auth fail "reason=host_key_changed host=${HOST}"
        exit 1
    fi
    cat <<EOF >&2
    pubkey auth failed

${HOST} is reachable but pubkey SSH didn't work. Most likely cause:
the Pi only accepts password auth right now (either you set a
password in Pi Imager instead of pasting a pubkey, or this is an
existing Pi that pre-dates your current laptop's keypair).

Re-run with --adopt to install your pubkey via ssh-copy-id:

    bash scripts/onboard.sh ${HOST} --adopt

If you did paste a pubkey into Imager and this still fails: check
the Imager version. 2.0.0-2.0.5 have an open bug where the pubkey
selection silently disables OS customization. Imager 2.0.6+ is
required for Trixie images.
EOF
    log_event auth fail "reason=pubkey_required"
    exit 1
fi
echo "    ok"
log_event auth ok "mode=pubkey"

# ---- phase 2.5: speaker identity --------------------------------------

resolve_speaker_hostname() {
    local normalized remote_hostname
    if [[ -n "$SPEAKER_HOSTNAME_ARG" ]]; then
        if ! normalized="$(normalize_speaker_hostname "$SPEAKER_HOSTNAME_ARG")"; then
            echo "onboard: --speaker-hostname must be a hostname, not an IP: ${SPEAKER_HOSTNAME_ARG}" >&2
            exit 2
        fi
        printf '%s\n' "$normalized"
        return 0
    fi

    if [[ "$IS_IP" == "0" ]]; then
        normalize_speaker_hostname "$HOST"
        return 0
    fi

    echo "==> resolve speaker hostname from ${PI_USER}@${HOST}" >&2
    if ! remote_hostname="$(ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new \
            "${PI_USER}@${HOST}" 'hostname -s 2>/dev/null || hostname' 2>/dev/null)"; then
        cat <<EOF >&2
onboard: connected by IP, but could not query the Pi hostname.

Re-run with the speaker's intended mDNS hostname so deploy does not
use the IP address as the speaker identity/certificate name:

    bash scripts/onboard.sh ${HOST} --speaker-hostname jts.local
EOF
        exit 1
    fi
    remote_hostname="${remote_hostname%%$'\n'*}"
    if ! normalized="$(normalize_speaker_hostname "$remote_hostname")"; then
        cat <<EOF >&2
onboard: connected by IP, but the Pi reported an unusable hostname:
    ${remote_hostname}

Re-run with the intended speaker hostname:

    bash scripts/onboard.sh ${HOST} --speaker-hostname jts.local
EOF
        exit 1
    fi
    echo "    speaker hostname: ${normalized} (from remote hostname '${remote_hostname}')" >&2
    printf '%s\n' "$normalized"
}

SPEAKER_HOSTNAME="$(resolve_speaker_hostname)"
log_event identity ok "ssh_target=${HOST} speaker_hostname=${SPEAKER_HOSTNAME}"

# ---- phase 3: persist state -------------------------------------------

echo "==> persist laptop state"

# ~/.ssh/config — add a Host alias block if missing. Hostname alias
# is skipped for raw IPs (a `Host 192-168-1-55` alias would be more
# confusing than useful; the user can just `ssh pi@192.168.1.55`).
if [[ "$IS_IP" == "0" ]]; then
    mkdir -p "$(dirname "$SSH_CONFIG")"
    touch "$SSH_CONFIG"
    chmod 600 "$SSH_CONFIG"
    if grep -qE "^Host ${ALIAS}( |$)" "$SSH_CONFIG"; then
        echo "    ${SSH_CONFIG}: Host ${ALIAS} already present (left untouched)"
    else
        echo "    ${SSH_CONFIG}: adding Host ${ALIAS}"
        cat >> "$SSH_CONFIG" <<EOF

# Added by scripts/onboard.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)
Host ${ALIAS}
    HostName ${HOST}
    User ${PI_USER}
    ServerAliveInterval 60
    ServerAliveCountMax 3
    StrictHostKeyChecking accept-new
EOF
    fi
else
    echo "    (IP target — ~/.ssh/config alias skipped)"
fi

# .env.local + CLAUDE.local.md — both written by write_laptop_state
# in scripts/_lib.sh. Single source of truth for the template so
# this script and scripts/use stay in sync.
echo "    ${REPO_ROOT}/.env.local"
echo "    ${REPO_ROOT}/CLAUDE.local.md"
write_laptop_state "$HOST" "$PI_USER" "$ALIAS" "$SPEAKER_HOSTNAME"
log_event persist ok "files=ssh_config,env.local,CLAUDE.local.md"

# ---- phase 4: install --------------------------------------------------

if [[ "$NO_INSTALL" == "1" ]]; then
    echo
    echo "==> done (state-only update; --no-install was passed)"
    if [[ "$IS_IP" == "0" ]]; then
        echo "    SSH with: ssh ${ALIAS}"
    else
        echo "    SSH with: ssh ${PI_USER}@${HOST}"
    fi
    exit 0
fi

echo
echo "==> run install.sh on ${HOST} via deploy-to-pi.sh"
echo "    first-install duration varies by hardware (native source builds dominate)"
echo
# Export PI_HOST/PI_USER for deploy-to-pi.sh. The two scripts share
# the same env-var contract; this is just being explicit so a future
# refactor of _lib.sh doesn't surprise either side.
if ! PI_HOST="$HOST" PI_USER="$PI_USER" JASPER_HOSTNAME="$SPEAKER_HOSTNAME" bash "${SCRIPT_DIR}/deploy-to-pi.sh"; then
    echo
    echo "onboard: deploy-to-pi.sh exited non-zero — see output above" >&2
    echo "         re-run after fixing; install.sh is idempotent" >&2
    log_event install fail "deploy_to_pi_nonzero"
    exit 1
fi
log_event install ok

# ---- phase 4.5: reboot if a migration requires it ----------------------
#
# install.sh's memory-resilience migrations (memory-cgroup kernel args,
# zram sizing) only take effect after a reboot. deploy/lib/install/
# memory-resilience.sh persists that fact to a /run marker (cleared by
# the reboot itself, since /run is tmpfs — no separate cleanup needed)
# rather than leaving onboard to parse install.sh's log output (#2110).
# Ordinary deploy-to-pi.sh redeploys read and print the same marker but
# never reboot on their own — only this first-run onboarding path does.
REBOOT_MARKER="$(
    ssh -o BatchMode=yes "${PI_USER}@${HOST}" \
        'cat /run/jasper-install/reboot_required 2>/dev/null' 2>/dev/null \
        | tr -d '\r' || true
)"

if [[ -n "$REBOOT_MARKER" ]]; then
    echo
    echo "==> reboot required: ${REBOOT_MARKER}"
    log_event reboot start "host=${HOST}"

    if pi_has_passwordless_sudo "$HOST" "$PI_USER"; then
        ssh -o BatchMode=yes "${PI_USER}@${HOST}" 'sudo -n reboot' >/dev/null 2>&1 || true
    elif [[ -t 0 ]]; then
        ssh -tt "${PI_USER}@${HOST}" 'sudo reboot' >/dev/null 2>&1 || true
    else
        echo "    cannot reboot automatically: sudo needs a password and this" >&2
        echo "    is not an interactive terminal. Reboot ${HOST} manually, then" >&2
        echo "    re-run: bash scripts/onboard.sh ${HOST} --no-install" >&2
        log_event reboot fail "reason=sudo_requires_tty"
        exit 1
    fi

    echo "    waiting for ${HOST} to go down and come back..."
    wait_for_pi_down "$HOST"
    if ! wait_for_pi_up "$HOST" "$PI_USER"; then
        echo "    ${HOST} did not come back within the wait window" >&2
        log_event reboot fail "reason=timeout host=${HOST}"
        exit 1
    fi
    echo "    ${HOST} is back up"
    log_event reboot ok "host=${HOST}"
fi

# Read the installer's persisted result instead of repeating its board-to-profile
# policy here. This remains useful when a future image has already installed the
# packages: the marker and reconciler state still describe what this box can do.
INSTALLED_PROFILE="$(
    ssh -o BatchMode=yes "${PI_USER}@${HOST}" \
        'cat /var/lib/jasper/install_profile 2>/dev/null' 2>/dev/null \
        | tr -d '\r' | head -n1 || true
)"
PROFILE_STATUS="ok"
case "$INSTALLED_PROFILE" in
    full|streambox)
        ;;
    *)
        echo "    warning: installed profile marker is unavailable or invalid" >&2
        echo "             completion guidance will stay capability-neutral" >&2
        INSTALLED_PROFILE="unknown"
        PROFILE_STATUS="warn"
        ;;
esac

PI_MODEL="$(
    ssh -o BatchMode=yes "${PI_USER}@${HOST}" \
        "tr -d '\\000\\r\\n' < /proc/device-tree/model 2>/dev/null" \
        2>/dev/null || true
)"
[[ -n "$PI_MODEL" ]] || PI_MODEL="unknown Raspberry Pi model"

OUTPUT_STATUS_CMD="/opt/jasper/.venv/bin/python -c 'from jasper.output_hardware import load_state; s = load_state(); print(\"\" if s is None else s.status)'"
OUTPUT_STATUS="$(
    ssh -o BatchMode=yes "${PI_USER}@${HOST}" "$OUTPUT_STATUS_CMD" \
        2>/dev/null | tr -d '\r' | head -n1 || true
)"

echo
echo "==> installed hardware profile"
echo "    model: ${PI_MODEL}"
echo "    profile: ${INSTALLED_PROFILE} (from /var/lib/jasper/install_profile)"
log_event profile "$PROFILE_STATUS" "profile=${INSTALLED_PROFILE}"

# ---- phase 5: validate -------------------------------------------------

echo
echo "==> validate with jasper-doctor"
# Soft-fail: a warn from doctor (e.g. 2-ch firmware, no API key
# configured yet) shouldn't fail onboarding. The user sees the output
# and can decide what to address next.
DOCTOR_CMD="sudo -n /opt/jasper/.venv/bin/jasper-doctor"
if pi_has_passwordless_sudo "$HOST" "$PI_USER"; then
    DOCTOR_SSH=(ssh "${PI_USER}@${HOST}")
elif [[ -t 0 ]]; then
    DOCTOR_CMD="sudo /opt/jasper/.venv/bin/jasper-doctor"
    DOCTOR_SSH=(ssh -tt "${PI_USER}@${HOST}")
else
    DOCTOR_SSH=()
fi

if [[ "${#DOCTOR_SSH[@]}" -eq 0 ]]; then
    echo
    echo "    (skipping doctor: sudo needs a password and this is not an interactive terminal)"
    log_event validate warn "reason=sudo_requires_tty"
elif "${DOCTOR_SSH[@]}" "$DOCTOR_CMD"; then
    log_event validate ok
else
    echo
    echo "    (doctor reported issues — review output, but onboarding"
    echo "     itself succeeded. Visit http://${HOST}/system/ to triage.)"
    log_event validate warn "doctor_reported_issues"
fi

# ---- success banner ----------------------------------------------------

if [[ "$IS_IP" == "0" ]]; then
    SSH_HOWTO="ssh ${ALIAS}"
else
    SSH_HOWTO="ssh ${PI_USER}@${HOST}"
fi
URL_HOST="$HOST"
if [[ "$HOST" != "$SPEAKER_HOSTNAME" ]]; then
    ALT_URL_NOTE="  Speaker hostname: ${SPEAKER_HOSTNAME} (identity/cert; use http://${SPEAKER_HOSTNAME}/ once mDNS resolves)"
fi

cat <<EOF

────────────────────────────────────────────────────────────────
JTS onboarding complete: ${HOST}

  SSH:               ${SSH_HOWTO}
${ALT_URL_NOTE:-}
  Build manifest:    ${SSH_HOWTO} 'sudo cat /var/lib/jasper/build.txt'
  Raspberry Pi:      ${PI_MODEL}
  Install profile:   ${INSTALLED_PROFILE}
EOF

if [[ "$OUTPUT_STATUS" == "missing" ]]; then
    cat <<EOF

Audio output is safely parked because no output DAC was detected.
Connect a supported output DAC (for the standard build, the Apple
USB-C to 3.5 mm dongle with its analog plug attached), then open:
  http://${URL_HOST}/sound/
EOF
fi

case "$INSTALLED_PROFILE" in
    streambox)
        cat <<EOF

This Streambox provides AirPlay, Spotify Connect, Bluetooth, DSP,
speaker grouping, and management. It intentionally omits the local
voice and microphone brain.

Next steps (visit from any device on the LAN):
  http://${URL_HOST}/sources/    choose and enable music sources
  http://${URL_HOST}/spotify/    connect a Spotify account (optional)
  http://${URL_HOST}/sound/      configure sound and output hardware
  http://${URL_HOST}/sound/pair/  group speakers
  http://${URL_HOST}/system/     dashboard and status
EOF
        ;;
    full)
        cat <<EOF

Next steps (visit from any device on the LAN):
  http://${URL_HOST}/sound/setup/  required: choose mono/stereo + passive/active;
                                   audio stays off until saved
  http://${URL_HOST}/voice/      pick a voice provider + paste API key
  http://${URL_HOST}/transit/    NYC subway / bus / Citi Bike (optional)
  http://${URL_HOST}/spotify/    connect a Spotify account (optional)
  http://${URL_HOST}/system/     dashboard and status
EOF
        ;;
    *)
        cat <<EOF

Next step (visit from any device on the LAN):
  http://${URL_HOST}/system/     dashboard, capabilities, and status
EOF
        ;;
esac

cat <<EOF
Future Claude Code sessions in this checkout will read
CLAUDE.local.md automatically and know that ${HOST} is the
active Pi.
────────────────────────────────────────────────────────────────
EOF
