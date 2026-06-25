#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
# Pi-clock epoch captured immediately before install.sh, so the post-
# install OOM-collateral scan can bound its journal window precisely
# (laptop and Pi clocks differ). 0 = not captured (scan skips).
DEPLOY_START_EPOCH=0
# Set to 1 by report_oom_collateral when a live production daemon was
# OOM-killed during the install window (problem #2/#5).
OOM_PRODUCTION_HIT=0

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

# Fetch origin at most once per run so origin/main is fresh for both the
# deploy-direction reclassification and the behind-origin/main advisory,
# without paying for a second network round-trip. Best-effort: a failure
# (offline, or no `origin` remote) leaves whatever ref we already have,
# and the callers degrade gracefully — the advisory skips, the direction
# guard keeps its pre-fetch classification.
ORIGIN_FETCHED=0
ensure_origin_fetched() {
    if [[ "$ORIGIN_FETCHED" == "1" ]]; then
        return 0
    fi
    ORIGIN_FETCHED=1
    git fetch --quiet origin >/dev/null 2>&1 || true
}

# Direction guard: never move the Pi's code BACKWARDS silently.
# Multiple checkouts/worktrees (and multiple agent sessions) deploy to
# the same Pi. On 2026-06-11 a stale parallel checkout deployed four
# minutes after a bugfix build and silently reverted it; the operator's
# hardware retest then ran the old code and the fix looked broken.
# Compare the local commit against the Pi's installed build manifest
# BEFORE rsync: a downgrade aborts unless JASPER_DEPLOY_ALLOW_DOWNGRADE=1
# (deliberate rollback/bisect); diverged sibling branches warn and
# proceed (routine lab flow, but the replaced work is named).
#
# This function also emits a separate, NON-BLOCKING behind-origin/main
# advisory at the end (the staleness-advisory block below): the guard
# above only relates the Pi to THIS checkout, so a current checkout can
# still be deploying onto a box whose installed build trails origin/main.
preflight_deploy_direction() {
    local remote_manifest installed_sha installed_branch installed_at
    local direction installed_short local_date installed_date
    # Same interactive-sudo capture hazard as the identity guard above:
    # `ssh -tt` merges the password prompt into captured stdout, so the
    # manifest would parse as garbage (and report a misleading "no
    # build manifest — first deploy?"). Skip explicitly instead.
    if [[ "$SUDO_INTERACTIVE" == "1" ]]; then
        echo "    deploy direction: skipped (interactive sudo cannot capture the build manifest cleanly)"
        return 0
    fi
    remote_manifest="$(run_remote_sudo 'cat /var/lib/jasper/build.txt 2>/dev/null' 2>/dev/null || true)"
    installed_sha="$(build_manifest_value "$remote_manifest" JASPER_GIT_SHA_FULL)"
    installed_branch="$(build_manifest_value "$remote_manifest" JASPER_GIT_BRANCH)"
    installed_at="$(build_manifest_value "$remote_manifest" JASPER_INSTALL_AT)"
    if [[ -z "$installed_sha" ]]; then
        echo "    deploy direction: no build manifest on the Pi (first deploy?) — proceeding"
        return 0
    fi

    direction="$(classify_deploy_direction "$SHA_FULL" "$installed_sha")"
    if [[ "$direction" == "unknown_installed" ]]; then
        # The installed commit may simply be newer than our last fetch.
        ensure_origin_fetched
        direction="$(classify_deploy_direction "$SHA_FULL" "$installed_sha")"
    fi
    installed_short="${installed_sha:0:8}"

    case "$direction" in
        same)
            echo "    deploy direction: redeploying installed ${installed_short}${DIRTY:+ (plus local uncommitted changes)}"
            ;;
        forward)
            echo "    deploy direction: forward (${installed_short} → ${SHA}${DIRTY})"
            ;;
        downgrade)
            if [[ "${JASPER_DEPLOY_ALLOW_DOWNGRADE:-}" == "1" ]]; then
                echo "    deploy direction: DOWNGRADE ${installed_short} → ${SHA}${DIRTY} (allowed by JASPER_DEPLOY_ALLOW_DOWNGRADE=1)"
                return 0
            fi
            local_date="$(git show -s --format=%ci "${SHA_FULL}" 2>/dev/null || true)"
            installed_date="$(git show -s --format=%ci "${installed_sha%-dirty}" 2>/dev/null || true)"
            cat <<EOF >&2
─────────────────────────────────────────────────────────────
 DEPLOY ABORTED: this would move ${PI_HOST}'s code BACKWARDS.
   installed: ${installed_short}  branch: ${installed_branch:-?}
              committed: ${installed_date:-unknown}
              installed: ${installed_at:-unknown}
   deploying: ${SHA}${DIRTY}  branch: ${BRANCH}
              committed: ${local_date:-unknown}
 The installed build contains commits this checkout lacks, so
 deploying would silently revert them. The Pi was not modified.
 If the downgrade is deliberate (rollback, bisect):
   JASPER_DEPLOY_ALLOW_DOWNGRADE=1 bash scripts/deploy-to-pi.sh
 Otherwise update this checkout first:
   git fetch origin && git rebase origin/main
─────────────────────────────────────────────────────────────
EOF
            exit 1
            ;;
        diverged)
            cat <<EOF >&2
    deploy direction: WARNING — diverged histories (proceeding)
      installed: ${installed_short} (branch: ${installed_branch:-?}, installed: ${installed_at:-unknown})
      deploying: ${SHA}${DIRTY} (branch: ${BRANCH})
      Neither commit contains the other: this deploy replaces a sibling
      checkout's build. If that other session's work matters, coordinate
      before redeploying.
EOF
            ;;
        unknown_installed)
            cat <<EOF >&2
    deploy direction: WARNING — installed ${installed_short} (branch: ${installed_branch:-?})
      is not in this checkout's history even after fetch; cannot compare
      (deployed from an un-pushed or foreign checkout?). Proceeding.
EOF
            ;;
    esac

    # Binary staleness advisory, separate from the blocking direction
    # guard above: is the build the Pi runs current with origin/main, or
    # behind it? The guard above only compares against THIS checkout; a
    # checkout that is itself current can still be deploying onto a box
    # whose installed build drifted far behind main. The signal is
    # BINARY (behind vs current) — never a commit count — because the
    # action is identical whether it is 1 or 120 commits behind: update
    # it. Advisory only: it never blocks the deploy (we are usually about
    # to update the box anyway) and degrades to a quiet skip when
    # origin/main cannot be resolved (offline, no remote).
    ensure_origin_fetched
    case "$(classify_installed_vs_main "$installed_sha" origin/main)" in
        behind)
            echo "    ⚠ ${PI_HOST} is behind origin/main (installed ${installed_short}) — update it." >&2
            ;;
        unknown)
            echo "    behind origin/main: skipped (origin/main unavailable to compare)"
            ;;
        current)
            : # up to date — stay quiet
            ;;
    esac
    return 0
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

# Surface collateral OOM kills during the install window — even when the
# install itself succeeded. Problem #2/#5 (the plan): on jts2 a source
# build OOM-killed nginx AND jasper-voice, and the deploy tooling exited
# silently; the collateral was only discoverable by SSHing in to read the
# journal. We bound the kernel-log scan to the Pi-clock epoch captured at
# install start, parse it with the pure _lib helpers, print what died, and
# set OOM_PRODUCTION_HIT when a live production daemon was the victim.
# Reading the kernel journal needs root, and `ssh -tt` (interactive sudo)
# corrupts captured output, so the caller gates this on passwordless sudo.
report_oom_collateral() {
    local since_epoch="$1"
    local journal units comms entry
    # journalctl accepts a formatted timestamp reliably across versions;
    # format the epoch on the Pi (GNU date). grep || true keeps an empty
    # match from tripping the remote shell, and the whole read is
    # best-effort (a missing journal must not fail a good deploy).
    journal="$(run_remote_sudo "journalctl -k --since \"\$(date -d @${since_epoch} '+%Y-%m-%d %H:%M:%S')\" --no-pager 2>/dev/null | grep -iE 'out of memory|oom-kill|oom_reaper|killed process' || true" 2>/dev/null || true)"
    if [[ -z "$journal" ]]; then
        return 0
    fi
    units="$(oom_killed_units "$journal")"
    comms="$(oom_killed_comms "$journal")"
    echo "  ⚠ OOM kills detected in the kernel log during the install window:" >&2
    if [[ -n "$comms" ]]; then
        while IFS= read -r entry; do
            [[ -z "$entry" ]] && continue
            echo "      • process killed: ${entry}" >&2
        done <<< "$comms"
    fi
    if [[ -n "$units" ]]; then
        while IFS= read -r entry; do
            [[ -z "$entry" ]] && continue
            if oom_unit_is_production "$entry"; then
                echo "      ✗ PRODUCTION daemon killed: ${entry}" >&2
                OOM_PRODUCTION_HIT=1
            else
                echo "      • unit killed: ${entry}" >&2
            fi
        done <<< "$units"
    fi
    if [[ "$OOM_PRODUCTION_HIT" != "1" ]]; then
        echo "      (no live production daemon among the victims — likely a"  >&2
        echo "       build process; Workstream A bounds build memory.)"       >&2
    fi
}

# Prove the build manifest advanced to the commit we just deployed, with a
# verified-ok status. install.sh writes the manifest ONLY as its final
# step (write_build_manifest), so a match is end-to-end proof the install
# ran to completion — and a MISMATCH means it didn't, even if the ssh
# command happened to return 0. This is the deploy-side guard for problem
# #4 (on jts2 the manifest was written early and lied after an OOM abort).
# Caller gates on passwordless sudo (clean manifest capture).
verify_manifest_advanced() {
    local manifest installed_full installed_status expected
    manifest="$(run_remote_sudo 'cat /var/lib/jasper/build.txt 2>/dev/null' 2>/dev/null || true)"
    installed_full="$(build_manifest_value "$manifest" JASPER_GIT_SHA_FULL)"
    installed_status="$(build_manifest_value "$manifest" JASPER_INSTALL_STATUS)"
    expected="${SHA_FULL}${DIRTY}"
    if [[ "$installed_full" == "$expected" && "$installed_status" == "ok" ]]; then
        echo "  ✓ build manifest advanced to ${SHA}${DIRTY} (status=ok, verified install)"
        return 0
    fi
    finish_airplay_health_maintenance
    trap - EXIT
    cat <<EOF >&2
─────────────────────────────────────────────────────────────
 DEPLOY VERIFICATION FAILED: the build manifest did not advance
 to the deployed commit on ${PI_HOST}.
   expected: ${expected} (status=ok)
   on Pi:    ${installed_full:-<none>} (status=${installed_status:-<none>})
 install.sh writes the manifest only as its final step, so this
 means the install did not run to completion (a half-updated box).
 The Pi still advertises its prior good build to the next deploy.
 Diagnose on the Pi:
   sudo /opt/jasper/.venv/bin/jasper-doctor
   journalctl -u jasper-control -n 120 --no-pager
─────────────────────────────────────────────────────────────
EOF
    exit 1
}

# Broadened post-deploy health (ADVISORY — does not gate the deploy).
# The management-surface probe only exercises the web path; this surfaces
# voice / AEC bridge / renderer health via jasper-doctor so a daemon that
# is down — for a real bug OR because its hardware is absent — is never
# silently hidden behind a green deploy (problems #5/#7). Non-gating on
# purpose: the broken-vs-idle reclassification of a missing-hardware
# daemon (e.g. no mic → jasper-voice cleanly parked) lands in Workstream
# C; until it does, a doctor ✗ here is informational, not a deploy abort.
# Runs post-restart/reconcile (the authoritative runtime state), unlike
# install.sh's own pre-restart doctor summary.
surface_system_health() {
    echo "==> Post-deploy system health (advisory; does not gate the deploy)"
    echo "    Covers voice, AEC bridge, and renderers. A function idle"
    echo "    because its hardware is absent (e.g. no mic) may read ! or ✗"
    echo "    here today; Workstream C reclassifies those as expected-idle."
    run_remote_sudo '/opt/jasper/.venv/bin/jasper-doctor 2>/dev/null || true' \
        2>/dev/null || echo "    (jasper-doctor unavailable — skipped)"
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

    # Identity guard: never deploy to the WRONG Pi. mDNS names are
    # transport, not identity — after an Avahi collision rename or a
    # re-image, PI_HOST can resolve to a different speaker than this
    # checkout means. TOFU: the first deploy records the target's
    # stable peer_id (/var/lib/jasper/peer_id) into .env.local; later
    # deploys abort BEFORE rsync on a mismatch. After a deliberate
    # re-image, accept the new identity with JTS_ACCEPT_NEW_IDENTITY=1.
    #
    # Gated on passwordless sudo: under the interactive fallback,
    # `ssh -tt` merges sudo's password prompt into the captured stdout,
    # so the "identity" read here would be prompt text glued to the
    # UUID — recording garbage on first contact and then spuriously
    # aborting every later passwordless deploy. Attended deploys skip
    # verification rather than mis-verify; passwordless sudo (BRINGUP
    # Phase 2.5) is the posture that gets identity-verified deploys.
    if [[ "$SUDO_INTERACTIVE" == "1" ]]; then
        echo "    speaker identity: skipped (interactive sudo cannot capture"
        echo "      the peer_id cleanly — enable passwordless sudo for"
        echo "      identity-verified deploys, see BRINGUP Phase 2.5)"
    else
        remote_peer_id="$(run_remote_sudo 'cat /var/lib/jasper/peer_id 2>/dev/null' 2>/dev/null || true)"
        identity_outcome="$(verify_or_record_peer_id \
            "$remote_peer_id" "${REPO_ROOT}/.env.local" \
            "${JTS_ACCEPT_NEW_IDENTITY:-}")" || {
            echo "─────────────────────────────────────────────────────────────" >&2
            echo " DEPLOY ABORTED: ${PI_HOST} is not the speaker this checkout" >&2
            echo " last deployed to (${identity_outcome})."                      >&2
            echo " Likely causes:"                                               >&2
            echo "   - an mDNS collision rename made this name resolve to a"     >&2
            echo "     DIFFERENT speaker (check both Pis' /system/ pages)"       >&2
            echo "   - the Pi was re-imaged (new peer_id)"                       >&2
            echo " If this target is intentional:"                               >&2
            echo "   JTS_ACCEPT_NEW_IDENTITY=1 bash scripts/deploy-to-pi.sh"     >&2
            echo " If you meant a different speaker:"                            >&2
            echo "   bash scripts/use <correct-hostname>"                        >&2
            echo "─────────────────────────────────────────────────────────────" >&2
            exit 1
        }
        case "$identity_outcome" in
            recorded)   echo "    speaker identity: recorded peer_id (first contact)" ;;
            rerecorded) echo "    speaker identity: re-recorded peer_id (accepted new)" ;;
            match)      echo "    speaker identity: verified" ;;
            *)          : ;;  # unavailable / no_state_file — nothing to verify against
        esac
    fi

    preflight_deploy_direction
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

install_env="JASPER_DEPLOY_SHA=$(shell_quote "${SHA}${DIRTY}") \
JASPER_DEPLOY_SHA_FULL=$(shell_quote "${SHA_FULL}${DIRTY}") \
JASPER_DEPLOY_BRANCH=$(shell_quote "$BRANCH") \
JASPER_HOSTNAME=$(shell_quote "$HOSTNAME_FOR_INSTALL")"
if [[ -n "${JASPER_INSTALL_PROFILE:-}" ]]; then
    install_env="${install_env} JASPER_INSTALL_PROFILE=$(shell_quote "$JASPER_INSTALL_PROFILE")"
fi
if [[ -n "${JASPER_ACCEPT_INSTALL_PROFILE_CHANGE:-}" ]]; then
    install_env="${install_env} JASPER_ACCEPT_INSTALL_PROFILE_CHANGE=$(shell_quote "$JASPER_ACCEPT_INSTALL_PROFILE_CHANGE")"
fi

# Capture the Pi's clock right before install so the post-install OOM scan
# can bound its kernel-log window precisely (plain ssh, no sudo needed).
DEPLOY_START_EPOCH="$(ssh_remote 'date +%s' 2>/dev/null | tr -dc '0-9')" || true
[[ -z "$DEPLOY_START_EPOCH" ]] && DEPLOY_START_EPOCH=0

# Run install.sh but DON'T let set -e abort before we surface collateral:
# capture the exit code, always scan for OOM kills in the install window,
# then decide. (Problem #5: a failed build that OOM-killed live daemons
# must not exit silently.)
install_rc=0
run_remote_sudo "${install_env} bash $(shell_quote "${REMOTE_REPO_DIR}/deploy/install.sh")" || install_rc=$?

if [[ "$SUDO_INTERACTIVE" != "1" && "$DEPLOY_START_EPOCH" != "0" ]]; then
    report_oom_collateral "$DEPLOY_START_EPOCH"
fi

if [[ "$install_rc" -ne 0 ]]; then
    finish_airplay_health_maintenance
    trap - EXIT
    echo "─────────────────────────────────────────────────────────────" >&2
    echo " DEPLOY FAILED: install.sh exited ${install_rc} on ${PI_HOST}." >&2
    if [[ "$OOM_PRODUCTION_HIT" == "1" ]]; then
        echo " A live production daemon was OOM-killed during the build"   >&2
        echo " (see above) — the build is unbounded for this box's RAM."   >&2
        echo " Workstream A bounds it; for now free RAM and re-deploy."     >&2
    fi
    echo " The build manifest was NOT advanced, so the Pi still"           >&2
    echo " advertises its prior good build to the next deploy (no"         >&2
    echo " half-updated lie). Diagnose on the Pi:"                         >&2
    echo "   sudo /opt/jasper/.venv/bin/jasper-doctor"                     >&2
    echo "   journalctl -u jasper-control -n 120 --no-pager"               >&2
    echo "─────────────────────────────────────────────────────────────" >&2
    exit "$install_rc"
fi

# install.sh returned success, but a production daemon may have been
# collaterally OOM-killed mid-build (report_oom_collateral already printed
# a loud per-unit ✗ warning above). We SURFACE this, we do not gate on it:
# the build OOM is HISTORY, while pass/fail is decided by END STATE — the
# management-surface probe (nginx/control), verify_manifest_advanced, and
# the advisory doctor below. A daemon that systemd already restarted must
# not fail an otherwise-healthy deploy (the inverse false-failure trap on a
# 1 GB Pi); one still down is caught by those end-state gates. Problem #5
# asks the tooling to "say so, not exit silently" — surfacing satisfies it.
if [[ "$OOM_PRODUCTION_HIT" == "1" ]]; then
    echo "  ⚠ a live production daemon was OOM-killed during the build (above)." >&2
    echo "    The deploy continues; end-state health is checked below. If the" >&2
    echo "    box is now unhealthy the gates will fail. Free RAM / Workstream A" >&2
    echo "    bounds build memory so this stops happening." >&2
fi

echo "==> Build manifest now on Pi:"
run_remote_sudo 'cat /var/lib/jasper/build.txt 2>/dev/null || echo "(not present)"'

if ! REMOTE_INSTALL_PROFILE="$(
    run_remote_sudo 'cat /var/lib/jasper/install_profile' \
        2>/dev/null | tail -n1 | tr -d '[:space:]'
)"; then
    finish_airplay_health_maintenance
    trap - EXIT
    echo "deploy-to-pi: could not read /var/lib/jasper/install_profile after install" >&2
    echo "The deploy cannot choose the correct post-install verification path." >&2
    exit 1
fi
case "$REMOTE_INSTALL_PROFILE" in
    full|streambox)
        ;;
    "")
        finish_airplay_health_maintenance
        trap - EXIT
        echo "deploy-to-pi: /var/lib/jasper/install_profile is empty after install" >&2
        exit 1
        ;;
    *)
        finish_airplay_health_maintenance
        trap - EXIT
        echo "deploy-to-pi: invalid installed profile '${REMOTE_INSTALL_PROFILE}'" >&2
        echo "Expected 'full' or 'streambox' in /var/lib/jasper/install_profile." >&2
        exit 1
        ;;
esac
echo "==> Installed profile: ${REMOTE_INSTALL_PROFILE}"

# Restart/reconcile the Python daemons that run application code so a
# code change in this deploy actually takes effect. install.sh already
# restarts jasper-mux + jasper-input + the wizard sockets. Socket
# activation does not replace an already-warm wizard process, so restart
# jasper-web explicitly after installing code. Voice is
# mic-hardware-dependent, so do not restart jasper-voice directly here:
# `jasper-aec-reconcile` restarts it when a valid mic path exists and
# parks it cleanly when no configured mic is present.
#
# Notable omissions:
#   - jasper-camilla — runs the Rust camilladsp binary, no Python.
#     No restart needed for Python code changes.
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

echo "==> Restarting web setup service: jasper-web.service"
run_remote_sudo "systemctl restart jasper-web.service jasper-web.socket" || \
    echo "  (jasper-web restart returned non-zero — see scripts/fetch-pi-logs.sh)"

if [[ "$REMOTE_INSTALL_PROFILE" == "streambox" ]]; then
    echo "==> Reconciling grouping state"
    run_remote_sudo "systemctl restart jasper-grouping-reconcile.service" || \
        echo "  (jasper-grouping-reconcile returned non-zero — see scripts/fetch-pi-logs.sh)"
else
    echo "==> Reconciling mic/AEC/voice state"
    run_remote_sudo "systemctl start jasper-aec-reconcile.service" || \
        echo "  (jasper-aec-reconcile returned non-zero — see scripts/fetch-pi-logs.sh)"
fi

# Post-deploy verification: the management surface must answer through
# nginx under the speaker's real hostname. This exercises the exact
# path a browser takes — nginx → socket-activated system wizard →
# jasper-control behind its management-host guard — and fails the
# deploy loudly instead of leaving a silently broken dashboard. The
# 2026-06-11 regression (every /system/ poll 403ing with
# host_not_allowed) shipped invisibly because nothing probed this path
# at deploy time. Retries cover jasper-control's restart window and
# the wizard's socket-activation cold start.
if [[ "$REMOTE_INSTALL_PROFILE" == "streambox" ]]; then
    echo "==> Verifying streambox management surface (Host: ${HOSTNAME_FOR_INSTALL})"
    verify_cmd="control=000; root=000; system=000; sources=000; sound=000; spotify=000; \
for attempt in 1 2 3 4 5; do \
control=\$(curl -s -o /dev/null -w '%{http_code}' -m 4 \
http://127.0.0.1:8780/healthz || echo 000); \
root=\$(curl -s -o /dev/null -w '%{http_code}' -m 4 \
-H $(shell_quote "Host: ${HOSTNAME_FOR_INSTALL}") \
http://127.0.0.1/ || echo 000); \
system=\$(curl -s -o /dev/null -w '%{http_code}' -m 4 \
-H $(shell_quote "Host: ${HOSTNAME_FOR_INSTALL}") \
http://127.0.0.1/system/data.json || echo 000); \
sources=\$(curl -s -o /dev/null -w '%{http_code}' -m 4 \
-H $(shell_quote "Host: ${HOSTNAME_FOR_INSTALL}") \
http://127.0.0.1/sources/state || echo 000); \
sound=\$(curl -s -o /dev/null -w '%{http_code}' -m 4 \
-H $(shell_quote "Host: ${HOSTNAME_FOR_INSTALL}") \
http://127.0.0.1/sound/ || echo 000); \
spotify=\$(curl -s -o /dev/null -w '%{http_code}' -m 4 \
-H $(shell_quote "Host: ${HOSTNAME_FOR_INSTALL}") \
http://127.0.0.1/spotify/ || echo 000); \
[ \"\$control\" = 200 ] && [ \"\$root\" = 200 ] && \
[ \"\$system\" = 200 ] && [ \"\$sources\" = 200 ] && \
[ \"\$sound\" = 200 ] && [ \"\$spotify\" = 200 ] && exit 0; \
sleep 3; done; \
echo \"streambox probes failed: control=\$control root=\$root system=\$system sources=\$sources sound=\$sound spotify=\$spotify\" >&2; exit 1"
    if ssh_remote "$verify_cmd"; then
        echo "  ✓ /, /system/data.json, /sources/state, /sound/, /spotify/, and :8780/healthz answer"
    else
        finish_airplay_health_maintenance
        trap - EXIT
        echo "─────────────────────────────────────────────────────────────" >&2
        echo " DEPLOY VERIFICATION FAILED: streambox management is not"   >&2
        echo " answering at http://${HOSTNAME_FOR_INSTALL}/."             >&2
        echo " Diagnose on the Pi:"                                       >&2
        echo "   sudo /opt/jasper/.venv/bin/jasper-doctor"                >&2
        echo "   systemctl status nginx jasper-control jasper-web.socket jasper-system-web.socket" >&2
        echo "   journalctl -u jasper-control -n 120 --no-pager"          >&2
        echo "─────────────────────────────────────────────────────────────" >&2
        exit 1
    fi
else
    echo "==> Verifying management surface (Host: ${HOSTNAME_FOR_INSTALL})"
    verify_cmd="code=000; for attempt in 1 2 3 4 5; do \
code=\$(curl -s -o /dev/null -w '%{http_code}' -m 4 \
-H $(shell_quote "Host: ${HOSTNAME_FOR_INSTALL}") \
http://127.0.0.1/system/data.json || echo 000); \
[ \"\$code\" = 200 ] && exit 0; sleep 3; done; \
echo \"management-surface probe failed: last HTTP status \$code\" >&2; exit 1"
    if ssh_remote "$verify_cmd"; then
        echo "  ✓ /system/data.json answers 200 via nginx as ${HOSTNAME_FOR_INSTALL}"
    else
        finish_airplay_health_maintenance
        trap - EXIT
        echo "─────────────────────────────────────────────────────────────" >&2
        echo " DEPLOY VERIFICATION FAILED: the management surface is not"   >&2
        echo " answering at http://${HOSTNAME_FOR_INSTALL}/system/."        >&2
        echo " Diagnose on the Pi:"                                          >&2
        echo "   sudo /opt/jasper/.venv/bin/jasper-doctor"                   >&2
        echo "   journalctl -u jasper-control | grep event=http.reject"      >&2
        echo "─────────────────────────────────────────────────────────────" >&2
        exit 1
    fi
fi

# Verified-install gate + broadened health surfacing. Both read the Pi
# over ssh and so need a clean capture: under interactive sudo, `ssh -tt`
# merges the password prompt into stdout and corrupts the manifest read
# and the doctor output. Skip with a notice, mirroring the identity and
# deploy-direction guards above; passwordless sudo (BRINGUP Phase 2.5) is
# the posture that gets fully-verified deploys.
if [[ "$SUDO_INTERACTIVE" == "1" ]]; then
    echo "==> Post-deploy verification: manifest + health checks skipped"
    echo "    (interactive sudo cannot capture them cleanly — enable"
    echo "     passwordless sudo for full verification, BRINGUP Phase 2.5)"
else
    verify_manifest_advanced
    surface_system_health
fi

finish_airplay_health_maintenance
trap - EXIT
echo "==> Done."
