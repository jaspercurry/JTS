#!/usr/bin/env bash
# Deploy the local checkout to the Pi and run install.sh, capturing
# the git SHA/branch up front so the /system dashboard's "Software"
# card shows the real version instead of "unknown".
#
# The standard rsync excludes .git/ for size + speed, which means
# install.sh can't read git info on the Pi side. This wrapper captures
# the info on the laptop *before* rsync and passes it through as
# JASPER_DEPLOY_SHA / JASPER_DEPLOY_SHA_FULL / JASPER_DEPLOY_BRANCH
# env vars on the sudo invocation. install.sh's build-manifest block
# honors those over its local-git fallback.
#
# Usage:
#   bash scripts/deploy-to-pi.sh
#   PI_HOST=192.168.1.42 bash scripts/deploy-to-pi.sh
#   PI_HOST=192.168.1.42 JASPER_HOSTNAME=jts.local bash scripts/deploy-to-pi.sh
#   PI_USER=pi PI_HOST=jts.local bash scripts/deploy-to-pi.sh
#
# User boundary: PI_USER/custom remote homes are supported here for
# deploy, but the public beginner path is still username `pi`; some
# diagnostics/operator scripts outside deploy assume `pi` or `/home/pi`.
#
# Skip the install step (just rsync) with:
#   SKIP_INSTALL=1 bash scripts/deploy-to-pi.sh
#
# Sudo modes:
#   - Unattended deploys require pubkey SSH and passwordless sudo;
#     this script preflights that with `sudo -n true` before rsync.
#   - Friendly interactive deploys fall back to `ssh -tt ... sudo`
#     prompts when passwordless sudo is unavailable. The password is
#     handled by sudo on the Pi and is not stored locally.
#
# After install completes, prints the resulting build manifest from
# the Pi so you can verify the SHA landed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_lib.sh
. "${SCRIPT_DIR}/_lib.sh"

AIRPLAY_HEALTH_SUPPRESS_PATH="/run/jasper-airplay-health-suppress-until"
AIRPLAY_HEALTH_DEPLOY_SUPPRESS_SEC="${AIRPLAY_HEALTH_DEPLOY_SUPPRESS_SEC:-2700}"
AIRPLAY_HEALTH_POST_DEPLOY_SUPPRESS_SEC="${AIRPLAY_HEALTH_POST_DEPLOY_SUPPRESS_SEC:-120}"
SSH_TARGET="${PI_USER}@${PI_HOST}"
SSH_BATCH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new)
SUDO_INTERACTIVE=0
HOSTNAME_FOR_INSTALL=""
REMOTE_REPO_DIR="${REMOTE_REPO_DIR:-}"

cd "$REPO_ROOT"

ssh_remote() {
    ssh "${SSH_BATCH_OPTS[@]}" "$SSH_TARGET" "$@"
}

ssh_remote_tty() {
    ssh -tt "${SSH_BATCH_OPTS[@]}" "$SSH_TARGET" "$@"
}

run_remote_sudo() {
    local command="$1"
    if [[ "$SUDO_INTERACTIVE" == "1" ]]; then
        ssh_remote_tty "sudo ${command}"
    else
        ssh_remote "sudo -n ${command}"
    fi
}

resolve_remote_repo_dir() {
    local remote_home
    if [[ -n "$REMOTE_REPO_DIR" ]]; then
        return 0
    fi
    if ! remote_home="$(ssh_remote 'printf "%s\n" "$HOME"')"; then
        cat <<EOF >&2
deploy-to-pi: pubkey SSH failed for ${SSH_TARGET}.

Run onboarding with --adopt first if this Pi was flashed for password SSH:

    bash scripts/onboard.sh ${PI_HOST} --adopt
EOF
        exit 1
    fi
    remote_home="${remote_home%%$'\n'*}"
    if [[ -z "$remote_home" || "$remote_home" != /* ]]; then
        echo "deploy-to-pi: could not determine remote home for ${SSH_TARGET}: ${remote_home}" >&2
        exit 1
    fi
    REMOTE_REPO_DIR="${remote_home}/jts"
}

resolve_speaker_hostname() {
    local normalized remote_hostname
    if [[ -n "${JASPER_HOSTNAME:-}" ]]; then
        if ! normalized="$(normalize_speaker_hostname "$JASPER_HOSTNAME")"; then
            echo "deploy-to-pi: JASPER_HOSTNAME must be a hostname, not an IP: ${JASPER_HOSTNAME}" >&2
            exit 2
        fi
        HOSTNAME_FOR_INSTALL="$normalized"
        return 0
    fi

    if ! is_ipv4_host "$PI_HOST"; then
        HOSTNAME_FOR_INSTALL="$(normalize_speaker_hostname "$PI_HOST")"
        return 0
    fi

    echo "==> Resolving speaker hostname from ${SSH_TARGET}"
    if ! remote_hostname="$(ssh_remote 'hostname -s 2>/dev/null || hostname')"; then
        cat <<EOF >&2
deploy-to-pi: PI_HOST is an IP address and the Pi hostname could not be queried.

Set the speaker identity explicitly so the IP address is not used for
JASPER_HOSTNAME/cert generation:

    PI_HOST=${PI_HOST} JASPER_HOSTNAME=jts.local bash scripts/deploy-to-pi.sh
EOF
        exit 1
    fi
    remote_hostname="${remote_hostname%%$'\n'*}"
    if ! normalized="$(normalize_speaker_hostname "$remote_hostname")"; then
        cat <<EOF >&2
deploy-to-pi: PI_HOST is an IP address and the Pi reported an unusable hostname:
    ${remote_hostname}

Set the speaker identity explicitly:

    PI_HOST=${PI_HOST} JASPER_HOSTNAME=jts.local bash scripts/deploy-to-pi.sh
EOF
        exit 1
    fi
    HOSTNAME_FOR_INSTALL="$normalized"
    echo "    speaker hostname: ${HOSTNAME_FOR_INSTALL} (from remote hostname '${remote_hostname}')"
}

preflight_sudo() {
    local mode="${JTS_DEPLOY_SUDO_MODE:-auto}"
    echo "==> Preflight sudo on ${SSH_TARGET}"
    if ssh_remote 'sudo -n true' >/dev/null 2>&1; then
        echo "    ok (passwordless sudo)"
        SUDO_INTERACTIVE=0
        return 0
    fi

    if [[ "$mode" == "passwordless" || "$mode" == "unattended" || ! -t 0 ]]; then
        cat <<EOF >&2
deploy-to-pi: ${SSH_TARGET} does not allow non-interactive sudo.

Unattended deploys need pubkey SSH plus passwordless sudo; this check
failed before rsync, so the Pi was not modified.

Friendly options:
  - Run from an interactive terminal so this script can prompt through
    ssh -tt without storing the password.
  - Or enable passwordless sudo deliberately for this user, then rerun.

This project will not install broad passwordless sudo rules for you.
EOF
        exit 1
    fi

    echo "    passwordless sudo unavailable; switching to interactive sudo"
    echo "    type the ${PI_USER} password if sudo prompts (not stored locally)"
    if ! ssh_remote_tty 'sudo -v'; then
        echo "deploy-to-pi: sudo password preflight failed on ${SSH_TARGET}" >&2
        exit 1
    fi
    SUDO_INTERACTIVE=1
}

mark_airplay_health_maintenance() {
    local ttl_sec="$1"
    local marker_command
    if [[ "${SKIP_AIRPLAY_HEALTH_SUPPRESS:-}" == "1" ]]; then
        return 0
    fi
    marker_command="printf \"%s\n\" \$((\$(date +%s) + ${ttl_sec})) > ${AIRPLAY_HEALTH_SUPPRESS_PATH}; chmod 0644 ${AIRPLAY_HEALTH_SUPPRESS_PATH}"
    run_remote_sudo "sh -c $(shell_quote "$marker_command")" || \
        echo "  (airplay health maintenance marker failed — deploy continuing)"
}

finish_airplay_health_maintenance() {
    mark_airplay_health_maintenance "${AIRPLAY_HEALTH_POST_DEPLOY_SUPPRESS_SEC}"
}

# Capture git info BEFORE rsync (which excludes .git/).
if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "deploy-to-pi: $REPO_ROOT is not a git checkout" >&2
    exit 1
fi
SHA=$(git rev-parse --short HEAD)
SHA_FULL=$(git rev-parse HEAD)
BRANCH=$(git rev-parse --abbrev-ref HEAD)
DIRTY=""
if ! git diff-index --quiet HEAD --; then
    DIRTY="-dirty"
fi

resolve_remote_repo_dir
if [[ "${SKIP_INSTALL:-}" != "1" ]]; then
    resolve_speaker_hostname
fi

echo "==> deploy-to-pi: ${SSH_TARGET}"
echo "    branch: ${BRANCH}"
echo "    sha:    ${SHA}${DIRTY} (${SHA_FULL})"
echo "    remote: ${REMOTE_REPO_DIR}"
if [[ -n "$HOSTNAME_FOR_INSTALL" ]]; then
    echo "    speaker hostname: ${HOSTNAME_FOR_INSTALL}"
fi

if [[ "${SKIP_INSTALL:-}" != "1" ]]; then
    preflight_sudo
fi

# Rsync — same exclude set documented in CLAUDE.md.
# macOS ships BSD rsync 2.6.9 (no --info= flag); use --stats which
# works on both BSD and GNU rsync. Suppress per-file output with
# --quiet so the wrapper's output is just the start/end summary.
ssh_remote "mkdir -p $(shell_quote "$REMOTE_REPO_DIR")"
rsync -az --delete --stats --quiet \
    --exclude .venv --exclude __pycache__ --exclude '.git' --exclude 'logs/*' \
    --exclude '.pio' --exclude '.claude/worktrees' --exclude '.claude/' \
    --exclude 'captures/*' --exclude 'wake-events/*' --exclude '*.pyc' \
    --exclude 'jasper_speaker.egg-info' --exclude '*.egg-info' \
    ./ "${SSH_TARGET}:${REMOTE_REPO_DIR}/"

if [[ "${SKIP_INSTALL:-}" == "1" ]]; then
    echo "==> SKIP_INSTALL=1 — rsync only, not running install.sh"
    exit 0
fi

# Run install.sh under sudo, passing the captured git info as env
# vars. sudo strips most env by default; explicitly preserve ours
# with `sudo VAR=value VAR=value command`. install.sh's build-manifest
# block reads these and prefers them over its REPO_DIR/.git fallback.
#
# JASPER_HOSTNAME is also forwarded so install.sh's TLS-cert block
# generates a server cert with the right CN/SAN for non-default
# speaker hostnames (jts2.local, jts-kitchen.local, etc.). Without
# this, every redeploy to a non-default Pi clobbers a previously
# correct cert with one for "jts.local". PI_HOST is only the SSH target:
# when it is an IP, HOSTNAME_FOR_INSTALL is resolved from JASPER_HOSTNAME
# or from the Pi's own hostname, never from the IP address.
echo "==> Running install.sh on ${PI_HOST}..."

# A deploy intentionally restarts shairport-sync, jasper-fanin, and
# friends. Mark a bounded AirPlay-health maintenance window so the
# dashboard keeps sampling current state but does not count self-
# inflicted restart underruns as AirPlay reliability incidents. The
# EXIT trap shortens the long in-progress TTL even if install.sh exits
# early, so stale deploy noise does not hide real problems for long.
mark_airplay_health_maintenance "${AIRPLAY_HEALTH_DEPLOY_SUPPRESS_SEC}"
trap 'finish_airplay_health_maintenance >/dev/null 2>&1 || true' EXIT

run_remote_sudo "JASPER_DEPLOY_SHA=$(shell_quote "${SHA}${DIRTY}") \
JASPER_DEPLOY_SHA_FULL=$(shell_quote "${SHA_FULL}${DIRTY}") \
JASPER_DEPLOY_BRANCH=$(shell_quote "$BRANCH") \
JASPER_HOSTNAME=$(shell_quote "$HOSTNAME_FOR_INSTALL") \
bash $(shell_quote "${REMOTE_REPO_DIR}/deploy/install.sh")"

echo "==> Build manifest now on Pi:"
run_remote_sudo 'cat /var/lib/jasper/build.txt 2>/dev/null || echo "(not present)"'

# Restart/reconcile the Python daemons that run application code so a
# code change in this deploy actually takes effect. install.sh already
# restarts jasper-mux + jasper-input + the wizard sockets. Voice is
# mic-hardware-dependent, so do not restart jasper-voice directly here:
# `jasper-aec-reconcile` restarts it when a valid mic path exists and
# parks it cleanly when no configured mic is present.
#
# Notable omissions:
#   - jasper-camilla — runs the Rust camilladsp binary, no Python.
#     No restart needed for Python code changes.
#   - The wizard servers — socket-activated, naturally pick up new
#     code on the next request.
if [[ "${SKIP_RESTART:-}" == "1" ]]; then
    echo "==> SKIP_RESTART=1 — leaving daemons on prior code"
    finish_airplay_health_maintenance
    trap - EXIT
    echo "==> Done."
    exit 0
fi

echo "==> Restarting code daemon: jasper-control.service"
run_remote_sudo "systemctl restart jasper-control.service" || \
    echo "  (jasper-control restart returned non-zero — see scripts/fetch-pi-logs.sh)"

echo "==> Reconciling mic/AEC/voice state"
run_remote_sudo "systemctl start jasper-aec-reconcile.service" || \
    echo "  (jasper-aec-reconcile returned non-zero — see scripts/fetch-pi-logs.sh)"

finish_airplay_health_maintenance
trap - EXIT
echo "==> Done."
