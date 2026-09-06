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
# ServerAlive keepalives bound a severed transport (issue #2340) to a
# ~60s ssh error instead of an unbounded hang, so a poll that lost its
# link fails fast enough to be retried on the next tick.
SSH_BATCH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=15 -o ServerAliveCountMax=4)
SUDO_INTERACTIVE=0
HOSTNAME_FOR_INSTALL=""
REMOTE_REPO_DIR="${REMOTE_REPO_DIR:-}"
# Pi-clock epoch captured immediately before install.sh, so the post-
# install OOM-collateral scan can bound its journal window precisely
# (laptop and Pi clocks differ). 0 = not captured (scan skips).
DEPLOY_START_EPOCH=0
# Set to 1 by report_oom_collateral when a live production daemon was
# OOM-killed during the install window. See ADR-0174.
OOM_PRODUCTION_HIT=0
# What run_install_detached learned: exited (install_rc is install.sh's
# own status), running (still going on the Pi) or unknown.
INSTALL_OUTCOME=exited
# Set by preflight_deploy_direction (same/forward/downgrade/diverged/
# unknown_installed) so the post-install verification can call out a
# same-SHA (no-op) redeploy distinctly (#2447) without a second
# manifest read.
DEPLOY_DIRECTION=""

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

# One remote shell body plus its arguments, quoted for whatever login
# shell sshd starts. The body must contain NO newline: printf %q renders
# one as bash-only $'\n', which a /bin/sh login shell cannot parse — so
# build bodies with `printf -v body '%s; '`, never '%s\n'.
remote_sh() {
    local name="$1" body="$2"
    shift 2
    printf 'sh -c %s %s' "$(shell_quote "$body")" "$(quote_args "$name" "$@")"
}

# Root-owned Pi facts under attended sudo.
#
# sudo's timestamp is per-tty, so every `ssh -tt` session prompts again,
# and a prompt written into a $(...) capture is invisible to the operator
# AND lands in the captured value. So on that channel each stage's facts
# are staged ONCE by an UNCAPTURED privileged command — the prompt is on
# screen and the operator types normally — into a directory the deploy
# user owns, and every read then rides the plain BatchMode channel with
# no sudo at all. Passwordless sudo reads the originals directly, as
# before. No-op on that channel; every caller is unconditional.
REMOTE_FACTS_DIR=""
publish_root_facts() {
    local stage="$1" since_epoch="${2:-0}" body
    local -a args
    [[ "$SUDO_INTERACTIVE" == "1" ]] || return 0
    if [[ -z "$REMOTE_FACTS_DIR" ]]; then
        # mktemp -d: an unpredictable name, created 0700, and nothing is
        # ever removed by name — a concurrent deploy from another checkout
        # must never blank facts this one is still reading.
        REMOTE_FACTS_DIR="$(ssh_remote 'mktemp -d "$HOME/.jts-deploy-facts.XXXXXXXX"')" || {
            echo "deploy-to-pi: could not stage privileged reads on ${SSH_TARGET}" >&2
            exit 1
        }
        REMOTE_FACTS_DIR="${REMOTE_FACTS_DIR%%$'\n'*}"
        trap 'cleanup_remote_facts' EXIT
    fi
    if [[ "$stage" == "journal" ]]; then
        args=("$since_epoch")
        printf -v body '%s; ' \
            'dir="$1"; owner="$2"; since="$3"' \
            'journalctl -k --since "$(date -d @"${since}" "+%Y-%m-%d %H:%M:%S")" --no-pager > "${dir}/journal" 2>/dev/null || true' \
            'chown "${owner}" "${dir}/journal"' \
            'chmod 0600 "${dir}/journal"'
    else
        case "$stage" in
            pre)  args=(peer_id build.txt) ;;
            post) args=(build.txt install_profile) ;;
        esac
        printf -v body '%s; ' \
            'dir="$1"; owner="$2"; shift 2' \
            'for f in "$@"; do [ -r "/var/lib/jasper/${f}" ] || continue; install -m 0600 -o "${owner}" "/var/lib/jasper/${f}" "${dir}/${f}" || exit 1; done'
    fi
    echo "==> Staging privileged reads on ${PI_HOST} (${stage}); sudo may prompt"
    if run_remote_sudo "$(remote_sh jts-deploy-facts "$body" "$REMOTE_FACTS_DIR" "$PI_USER" "${args[@]}")"; then
        return 0
    fi
    # The journal slice is advisory (its caller decides); the guards that
    # read the other stages are not, so an unstaged fact fails the deploy
    # rather than reading as absent.
    if [[ "$stage" == "journal" ]]; then
        return 1
    fi
    echo "deploy-to-pi: could not stage the ${stage} root facts on ${SSH_TARGET}" >&2
    exit 1
}

cleanup_remote_facts() {
    [[ -n "$REMOTE_FACTS_DIR" ]] || return 0
    ssh_remote "rm -rf $(shell_quote "$REMOTE_FACTS_DIR")" >/dev/null 2>&1 || true
    REMOTE_FACTS_DIR=""
}

# Read one root-owned /var/lib/jasper file: the staged copy under
# attended sudo, the original under passwordless sudo. Callers keep their
# own error handling — this never swallows a failed read.
read_pi_file() {
    local name="$1"
    if [[ "$SUDO_INTERACTIVE" == "1" ]]; then
        ssh_remote "cat $(shell_quote "${REMOTE_FACTS_DIR}/${name}")"
    else
        run_remote_sudo "cat $(shell_quote "/var/lib/jasper/${name}")"
    fi
}

resolve_remote_repo_dir() {
    local remote_home
    if [[ -n "$REMOTE_REPO_DIR" ]]; then
        return 0
    fi
    if ! remote_home="$(ssh_remote 'printf "%s\n" "$HOME"')"; then
        # First contact with the Pi, so this is where a re-image's
        # changed host key surfaces — before the sudo, identity and
        # direction preflights, none of which this touches.
        if ssh_host_key_changed_advice "$SSH_TARGET"; then
            exit 1
        fi
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
    remote_manifest="$(read_pi_file build.txt 2>/dev/null || true)"
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
    DEPLOY_DIRECTION="$direction"

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

# Portable across macOS/Linux without getent (Linux-only) or dig (not
# always installed): python3's socket.getaddrinfo ships on both. Prints
# one IPv4 address per line, de-duplicated; prints nothing on any
# resolution failure (unknown host, offline) — the Python side itself
# exits 0 in that case. The `|| true` at the call site below still
# matters: if python3 itself is missing, the command substitution fails
# with a real "command not found" (127), and under `set -e` an unguarded
# `var=$(...)` assignment would propagate that as a script-aborting
# failure (verified by hand with a stubbed-missing python3; see PR body).
resolve_pi_host_ipv4_addrs() {
    python3 - "$PI_HOST" <<'PY' 2>/dev/null
import socket
import sys

host = sys.argv[1]
try:
    infos = socket.getaddrinfo(host, None, socket.AF_INET)
except OSError:
    sys.exit(0)
seen = set()
for info in infos:
    addr = info[4][0]
    if addr not in seen:
        seen.add(addr)
        print(addr)
PY
}

# USB-gadget-network deploy advisory (non-blocking, prints BEFORE rsync).
# See _lib.sh's usb_gadget_management_cidrs/ipv4_in_cidr docstrings for the
# "why" (issue #2340): a mid-install gadget rebuild can sever a deploy whose
# own ssh session is riding ncm.usb0. Since #3194 the install only rebinds when
# the live composition and the truth table actually disagree, so a converged
# gadget is not bounced — tests/test_install_usbgadget_migration.py.
# This fires regardless of that state since the laptop can't see it from
# here. Only a deploy that runs install.sh can hit that failure mode —
# SKIP_INSTALL=1 rsync-only never touches the gadget — so the caller
# invokes this only inside that guard.
# Degrades to a quiet skip whenever the subnet or PI_HOST's address can't
# be determined (a checkout missing deploy/usb-network/, offline, an
# unresolvable hostname): the goal is to warn when we KNOW, never to guess
# or fail a deploy over a DNS hiccup. When PI_HOST is a hostname with
# multiple resolved addresses (a dual-homed mDNS name), this warns on the
# first in-subnet match rather than trying to determine which one ssh
# will actually pick — ssh resolves DNS internally with no introspection
# hook, so "warn on any match, name which" is the honest simplest answer.
warn_if_pi_host_on_gadget_network() {
    local cidr cidrs matched="" matched_cidr="" addr addrs
    cidrs="$(usb_gadget_management_cidrs)" || return 0
    [[ -n "$cidrs" ]] || return 0

    if is_ipv4_host "$PI_HOST"; then
        while IFS= read -r cidr; do
            [[ -z "$cidr" ]] && continue
            if ipv4_in_cidr "$PI_HOST" "$cidr"; then
                matched="$PI_HOST"
                matched_cidr="$cidr"
                break
            fi
        done <<< "$cidrs"
    else
        addrs="$(resolve_pi_host_ipv4_addrs)" || true
        [[ -n "$addrs" ]] || return 0
        while IFS= read -r addr; do
            [[ -z "$addr" ]] && continue
            while IFS= read -r cidr; do
                [[ -z "$cidr" ]] && continue
                if ipv4_in_cidr "$addr" "$cidr"; then
                    matched="$addr"
                    matched_cidr="$cidr"
                    break 2
                fi
            done <<< "$cidrs"
        done <<< "$addrs"
    fi

    [[ -n "$matched" ]] || return 0

    cat <<EOF >&2
─────────────────────────────────────────────────────────────
 ⚠ ${PI_HOST} resolves to ${matched}, inside the USB gadget
   management allocation (${matched_cidr}).
   If that's this deploy's own transport AND the gadget's composition
   actually changes during this deploy: the rebuild tears down that
   link out from under it. A converged gadget — NCM-only, or UAC2
   with its live consumer — is not bounced at all.
   The ssh session dies with no FIN, this script's keepalive bounds
   the resulting error to about a minute (see SSH_BATCH_OPTS)
   instead of hanging forever, and the transcript will look wedged
   around event=usb_gadget.converge state=rebuilding — even though
   install.sh keeps going and succeeds on the Pi (#2340).
   Workaround: deploy over the Wi-Fi/LAN address instead, e.g.
     PI_HOST=<pi-lan-hostname-or-ip> bash scripts/deploy-to-pi.sh
   Proceeding — this is advisory only. If ssh does exit early, check
   the Pi directly before assuming the deploy failed.
─────────────────────────────────────────────────────────────
EOF
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
    [[ "$INSTALL_OUTCOME" == "running" ]] && return 0
    mark_airplay_health_maintenance "${AIRPLAY_HEALTH_POST_DEPLOY_SUPPRESS_SEC}"
}

# Surface collateral OOM kills during the install window — even when the
# install itself succeeded. See ADR-0174: on jts2 a source build OOM-killed
# nginx AND jasper-voice, and the deploy tooling exited silently; the
# collateral was only discoverable by SSHing in to read the journal. We
# bound the kernel-log scan to the Pi-clock epoch captured at
# install start, parse it with the pure _lib helpers, print what died, and
# set OOM_PRODUCTION_HIT when a live production daemon was the victim.
report_oom_collateral() {
    local since_epoch="$1"
    local journal units comms entry
    # One regex, applied twice: remotely to bound what crosses the wire,
    # then locally to what actually arrived, so a line that reached the
    # capture by some other route can never raise the banner below.
    local oom_re='out of memory|oom-kill|oom_reaper|killed process'
    # journalctl accepts a formatted timestamp reliably across versions;
    # format the epoch on the Pi (GNU date). grep || true keeps an empty
    # match from tripping the remote shell, and the whole read is
    # best-effort (a missing journal must not fail a good deploy).
    if [[ "$SUDO_INTERACTIVE" == "1" ]]; then
        publish_root_facts journal "$since_epoch" || return 0
        journal="$(read_pi_file journal 2>/dev/null || true)"
    else
        journal="$(run_remote_sudo "journalctl -k --since \"\$(date -d @${since_epoch} '+%Y-%m-%d %H:%M:%S')\" --no-pager 2>/dev/null | grep -iE '${oom_re}' || true" 2>/dev/null || true)"
    fi
    journal="$(printf '%s\n' "$journal" | grep -iE "$oom_re" || true)"
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
verify_manifest_advanced() {
    local manifest installed_full installed_status expected
    manifest="$(read_pi_file build.txt 2>/dev/null || true)"
    installed_full="$(build_manifest_value "$manifest" JASPER_GIT_SHA_FULL)"
    installed_status="$(build_manifest_value "$manifest" JASPER_INSTALL_STATUS)"
    expected="${SHA_FULL}${DIRTY}"
    if [[ "$installed_full" == "$expected" && "$installed_status" == "ok" ]]; then
        echo "  ✓ build manifest advanced to ${SHA}${DIRTY} (status=ok, verified install)"
        if [[ "$DEPLOY_DIRECTION" == "same" ]]; then
            # #2447: a fetch seconds after a merge can miss GitHub's ref
            # advance, so this checkout's HEAD — and thus what just got
            # verified above — can be a same-SHA no-op that looks like a
            # successful deploy of a change that never actually landed.
            echo "  ℹ same-SHA redeploy: ${SHA}${DIRTY} was already installed — no new commit landed"
        fi
        return 0
    fi
    finish_airplay_health_maintenance
    trap 'cleanup_remote_facts' EXIT
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
    local health_cmd
    printf -v health_cmd '%s\n' \
        'set -euo pipefail' \
        "mem_kb=\$(awk '/^MemTotal:/ { print \$2; exit }' /proc/meminfo 2>/dev/null || true)" \
        'case "${mem_kb}" in' \
        '    ""|*[!0-9]*) low_memory=0 ;;' \
        '    *) if (( mem_kb < 1200000 )); then low_memory=1; else low_memory=0; fi ;;' \
        'esac' \
        'if [[ "${low_memory}" == "1" ]]; then' \
        '    probe=__JASPER_DEPLOY_HEALTH_PROBE__' \
        '    if [[ ! -x "${probe}" ]]; then' \
        '        exit 127' \
        '    fi' \
        '    "${probe}"' \
        'else' \
        '    /opt/jasper/.venv/bin/jasper-doctor' \
        'fi'
    health_cmd="${health_cmd/__JASPER_DEPLOY_HEALTH_PROBE__/$(shell_quote "${REMOTE_REPO_DIR}/deploy/bin/jasper-deploy-health")}"
    run_remote_sudo "bash -lc $(shell_quote "${health_cmd}") 2>/dev/null || true" \
        2>/dev/null || echo "    (post-deploy health probe unavailable — skipped)"
}

# The install must outlive this ssh session (#4190): it runs as a
# transient unit on the Pi and the wrapper only observes it. The fixed
# unit name is also the single-instance guard.
INSTALL_UNIT=jts-install
INSTALL_POLL_INTERVAL_SEC=10
# A Zero 2 W cold Rust build is the slowest supported install.
INSTALL_POLL_CEILING_SEC=7200
run_install_detached() {
    local body launch_rc=0 chunk status transcript
    local load active sub result main size ended offset=1 deadline down_since=0
    local log="~${PI_USER}/.jts-install.log"
    install_rc=1
    INSTALL_OUTCOME=unknown

    # The unit is left loaded so its exit status survives it, so a
    # finished one must be cleared before the next launch — but never a
    # live one: that is the single-instance guard (exit 3).
    printf -v body '%s; ' \
        'case "$(systemctl show -p SubState --value -- "$1" 2>/dev/null)" in running|start|start-pre|start-post|reload) exit 3 ;; esac' \
        'systemctl stop -- "$1" >/dev/null 2>&1 || true' \
        'systemctl reset-failed -- "$1" >/dev/null 2>&1 || true' \
        'l=$(getent passwd "$3" | cut -d: -f6)/.jts-install.log' \
        ': > "$l"; chown "$3" "$l" 2>/dev/null || true; chmod 0600 "$l"' \
        'exec systemd-run --quiet --unit="$1" -p RemainAfterExit=yes -p StandardOutput=append:"$l" -p StandardError=inherit /bin/sh -c "$2"'
    echo "    transcript on the Pi: ${log}"
    run_remote_sudo "$(remote_sh jts-install-launch "$body" "$INSTALL_UNIT" \
        "${install_env} bash $(shell_quote "${REMOTE_REPO_DIR}/deploy/install.sh")" "$PI_USER")" \
        || launch_rc=$?
    if [[ "$launch_rc" != "0" ]]; then
        if [[ "$launch_rc" == "3" ]]; then
            echo "deploy-to-pi: event=deploy.install_busy host=${PI_HOST} unit=${INSTALL_UNIT} transcript=${log}" >&2
            INSTALL_OUTCOME=running
        else
            echo "deploy-to-pi: event=deploy.install_launch_failed host=${PI_HOST} rc=${launch_rc}" >&2
        fi
        return 0
    fi

    # The transcript is read by BYTE offset, and the remote reports the
    # size it served, so a partial line is never dropped, duplicated or
    # fused with the status block that precedes it.
    printf -v body '%s; ' \
        'l="$HOME/.jts-install.log"; size=$(wc -c < "$l" 2>/dev/null | tr -dc "0-9")' \
        '[ -n "$size" ] || size=0' \
        'printf "JTS_STATUS=%s\nJTS_LOG_SIZE=%s\nJTS_LOG\n" "$(systemctl show --value -p LoadState -p ActiveState -p SubState -p Result -p ExecMainStatus -- "$1" 2>/dev/null | sed "s/^\$/-/" | tr "\n" " ")" "$size"' \
        'if [ "$size" -ge "$2" ]; then tail -c +"$2" -- "$l" 2>/dev/null | head -c "$((size - $2 + 1))"; fi' \
        'printf .'
    deadline=$(( $(date +%s) + INSTALL_POLL_CEILING_SEC ))
    while :; do
        chunk="$(ssh_remote "$(remote_sh jts-install-poll "$body" "$INSTALL_UNIT" "$offset")" 2>/dev/null)" || chunk=""
        if [[ "$chunk" != *"JTS_LOG"$'\n'* ]]; then
            # A dropped poll is a reconnect on the next tick, not a failed
            # deploy: the install no longer belongs to this ssh session.
            [[ "$down_since" != "0" ]] || {
                down_since="$(date +%s)"
                echo "deploy-to-pi: event=deploy.install_poll_reconnect host=${PI_HOST} since=${down_since}" >&2
            }
        else
            [[ "$down_since" == "0" ]] || {
                echo "deploy-to-pi: event=deploy.install_poll_reconnected host=${PI_HOST} down_sec=$(( $(date +%s) - down_since ))" >&2
                down_since=0
            }
            status="${chunk%%JTS_LOG$'\n'*}"
            transcript="${chunk#*JTS_LOG$'\n'}"
            printf '%s' "${transcript%.}"
            read -r load active sub result main <<< "${status#*JTS_STATUS=}"
            size="$(build_manifest_value "$status" JTS_LOG_SIZE)"
            [[ -n "$size" ]] && offset=$(( size + 1 ))
            ended=""
            [[ "$active" == "failed" || "$active" == "inactive" || "$sub" == "exited" ]] && ended=1
            # A vanished unit (the Pi rebooted mid-install) reports every
            # other field as a default, so this is read first.
            if [[ "$load" == "not-found" || ( -n "$ended" && ! "$main" =~ ^[0-9]+$ ) ]]; then
                echo "deploy-to-pi: event=deploy.install_lost host=${PI_HOST} unit=${INSTALL_UNIT} transcript=${log}" >&2
                return 0
            fi
            if [[ -n "$ended" ]]; then
                INSTALL_OUTCOME=exited
                install_rc="$main"
                [[ "$result" == "signal" ]] && {
                    echo "deploy-to-pi: event=deploy.install_signal host=${PI_HOST} signal=${main}" >&2
                    install_rc=$(( 128 + main ))
                }
                return 0
            fi
        fi
        if (( $(date +%s) >= deadline )); then
            echo "deploy-to-pi: event=deploy.install_timeout host=${PI_HOST} ceiling_sec=${INSTALL_POLL_CEILING_SEC} transcript=${log}" >&2
            INSTALL_OUTCOME=running
            return 0
        fi
        sleep "$INSTALL_POLL_INTERVAL_SEC"
    done
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
    # USB-gadget-network advisory — before rsync, before sudo/ssh preflight,
    # since it only needs PI_HOST resolution. See warn_if_pi_host_on_gadget_network
    # above for the "why" (issue #2340).
    warn_if_pi_host_on_gadget_network
fi

preflight_sudo

# Identity guard: never deploy to the WRONG Pi. mDNS names are
# transport, not identity — after an Avahi collision rename or a
# re-image, PI_HOST can resolve to a different speaker than this
# checkout means. TOFU: the first deploy records the target's
# stable peer_id (/var/lib/jasper/peer_id) into .env.local; later
# deploys abort BEFORE rsync on a mismatch. After a deliberate
# re-image, accept the new identity with JTS_ACCEPT_NEW_IDENTITY=1.
#
# The recorded peer_id describes the host .env.local names, so it is only
# comparable when THIS deploy is aimed there — naming that same host
# yourself still verifies and records. A caller naming a DIFFERENT host is
# a redirect the record says nothing about: give the guard no state file,
# or recording the redirect's peer_id makes the checkout's next plain
# deploy abort against its own Pi. See _lib.sh's targeting contract.
identity_env_file="${REPO_ROOT}/.env.local"
if [[ "${JTS_TARGET_FROM:-}" == "caller" \
    && "$PI_HOST" != "${JTS_TARGET_FILE_HOST:-}" ]]; then
    identity_env_file=""
fi
publish_root_facts pre
remote_peer_id="$(read_pi_file peer_id 2>/dev/null | tail -n1 || true)"
identity_outcome="$(verify_or_record_peer_id \
    "$remote_peer_id" "$identity_env_file" \
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
echo "    DEPLOY_IDENTITY=${identity_outcome}"
case "$identity_outcome" in
    recorded)   echo "    speaker identity: recorded peer_id (first contact)" ;;
    rerecorded) echo "    speaker identity: re-recorded peer_id (accepted new)" ;;
    match)      echo "    speaker identity: verified" ;;
    no_state_file)
        if [[ -z "$identity_env_file" && -f "${REPO_ROOT}/.env.local" ]]; then
            echo "    speaker identity: skipped (you named this target;"
            echo "      the recorded identity describes .env.local's speaker)"
        fi
        ;;
esac

preflight_deploy_direction

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

# The captured git info reaches install.sh as env vars because the rsync
# excludes .git/. JASPER_HOSTNAME rides along so install.sh's TLS-cert
# block gets the right CN/SAN: without it every redeploy to a non-default
# Pi clobbers a correct cert with one for "jts.local". PI_HOST is only the
# SSH target — HOSTNAME_FOR_INSTALL is never derived from an IP.
echo "==> Running install.sh on ${PI_HOST}..."

# A deploy intentionally restarts shairport-sync, jasper-fanin, and
# friends. Mark a bounded AirPlay-health maintenance window so the
# dashboard keeps sampling current state but does not count self-
# inflicted restart underruns as AirPlay reliability incidents. The
# EXIT trap shortens the long in-progress TTL even if install.sh exits
# early, so stale deploy noise does not hide real problems for long.
mark_airplay_health_maintenance "${AIRPLAY_HEALTH_DEPLOY_SUPPRESS_SEC}"
trap 'finish_airplay_health_maintenance >/dev/null 2>&1 || true; cleanup_remote_facts' EXIT

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
# Forward selected env vars into the remote install.sh (non-empty only).
# SKIP_RESTART rides along with the install env, but install.sh does not read
# it: its only reader is this script's own post-install restart policy below,
# and install.sh restarts the always-on daemons either way.
for key in \
    JASPER_BUILD_SANDBOX \
    JASPER_BUILD_SANDBOX_OOM_SCORE_ADJ \
    JASPER_BUILD_SANDBOX_MEMORY_HIGH \
    JASPER_BUILD_SANDBOX_MEMORY_MAX \
    JASPER_BUILD_SANDBOX_CPU_WEIGHT \
    JASPER_BUILD_SANDBOX_IO_WEIGHT \
    JASPER_BUILD_SANDBOX_RUNTIME_MAX \
    JASPER_BUILD_SWAP \
    JASPER_BUILD_SWAP_PATH \
    JASPER_BUILD_SWAP_SIZE_MB \
    JASPER_BUILD_SWAP_PRIORITY \
    JASPER_RUST_LOW_MEMORY_BUILD \
    SKIP_RESTART
do
    if [[ -n "${!key:-}" ]]; then
        install_env="${install_env} ${key}=$(shell_quote "${!key}")"
    fi
done

# Capture the Pi's clock right before install so the post-install OOM scan
# can bound its kernel-log window precisely (plain ssh, no sudo needed).
DEPLOY_START_EPOCH="$(ssh_remote 'date +%s' 2>/dev/null | tr -dc '0-9')" || true
[[ -z "$DEPLOY_START_EPOCH" ]] && DEPLOY_START_EPOCH=0

# The install's status is reported, not raised, so the OOM scan below runs
# even when the build failed. (See ADR-0174: a failed build that OOM-killed
# live daemons must not exit silently.)
run_install_detached

if [[ "$DEPLOY_START_EPOCH" != "0" ]]; then
    report_oom_collateral "$DEPLOY_START_EPOCH"
fi

if [[ "$install_rc" -ne 0 ]]; then
    finish_airplay_health_maintenance
    trap 'cleanup_remote_facts' EXIT
    echo "─────────────────────────────────────────────────────────────" >&2
    if [[ "$INSTALL_OUTCOME" == "exited" ]]; then
        echo " DEPLOY FAILED: install.sh exited ${install_rc} on ${PI_HOST}." >&2
    else
        echo " DEPLOY FAILED: no exit status from install.sh on ${PI_HOST} — see" >&2
        echo " the event= line above; the install may still be running there." >&2
    fi
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
# collaterally OOM-killed mid-install (report_oom_collateral already printed
# a loud per-unit ✗ warning above). We continue through the end-state
# checks so the transcript includes manifest/management/doctor evidence,
# but a production-daemon OOM is not merge-quality deploy hygiene: the
# final verification block below fails after all evidence has been surfaced.
if [[ "$OOM_PRODUCTION_HIT" == "1" ]]; then
    echo "  ⚠ a live production daemon was OOM-killed during install (above)." >&2
    echo "    Continuing through end-state checks so the failure has complete" >&2
    echo "    evidence, then the deploy verification will fail." >&2
fi

# Ordinary deploys keep the cautious no-surprise posture: read and print
# install.sh's reboot-required marker (memory-cgroup/zram migrations that
# only take effect after a reboot, #2110), but never reboot on our own —
# only scripts/onboard.sh's first-run path does that.
REBOOT_MARKER="$(ssh_remote 'cat /run/jasper-install/reboot_required 2>/dev/null' 2>/dev/null | tr -d '\r' || true)"
if [[ -n "$REBOOT_MARKER" ]]; then
    echo "==> reboot required (not applied by this deploy): ${REBOOT_MARKER}"
fi

publish_root_facts post

echo "==> Build manifest now on Pi:"
read_pi_file build.txt 2>/dev/null || echo "(not present)"

if ! REMOTE_INSTALL_PROFILE="$(
    read_pi_file install_profile 2>/dev/null | tail -n1 | tr -d '[:space:]'
)"; then
    finish_airplay_health_maintenance
    trap 'cleanup_remote_facts' EXIT
    echo "deploy-to-pi: could not read /var/lib/jasper/install_profile after install" >&2
    echo "The deploy cannot choose the correct post-install verification path." >&2
    exit 1
fi
case "$REMOTE_INSTALL_PROFILE" in
    full|streambox)
        ;;
    "")
        finish_airplay_health_maintenance
        trap 'cleanup_remote_facts' EXIT
        echo "deploy-to-pi: /var/lib/jasper/install_profile is empty after install" >&2
        exit 1
        ;;
    *)
        finish_airplay_health_maintenance
        trap 'cleanup_remote_facts' EXIT
        echo "deploy-to-pi: invalid installed profile '${REMOTE_INSTALL_PROFILE}'" >&2
        echo "Expected 'full' or 'streambox' in /var/lib/jasper/install_profile." >&2
        exit 1
        ;;
esac
echo "==> Installed profile: ${REMOTE_INSTALL_PROFILE}"

# Restart/reconcile the Python daemons that run application code so a
# code change in this deploy actually takes effect. install.sh already
# restarts jasper-control + jasper-input + the wizard sockets on both
# profiles, so SKIP_RESTART=1 below skips only what is left here: the
# jasper-web restart and the per-profile reconciler. Socket activation
# does not replace an already-warm wizard process, so restart jasper-web
# explicitly after installing code. Voice is
# mic-hardware-dependent, so do not restart jasper-voice directly here:
# `jasper-aec-reconcile` restarts it when a valid mic path exists and
# parks it cleanly when no configured mic is present.
#
# Notable omissions:
#   - jasper-camilla — runs the Rust camilladsp binary, no Python.
#     No restart needed for Python code changes.
if [[ "${SKIP_RESTART:-}" == "1" ]]; then
    echo "==> SKIP_RESTART=1 — skipping the jasper-web + reconciler restarts; install.sh already restarted jasper-control, jasper-input and the wizard sockets"
else
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
http://127.0.0.1/sound/setup/ || echo 000); \
spotify=\$(curl -s -o /dev/null -w '%{http_code}' -m 4 \
-H $(shell_quote "Host: ${HOSTNAME_FOR_INSTALL}") \
http://127.0.0.1/spotify/ || echo 000); \
[ \"\$control\" = 200 ] && [ \"\$root\" = 200 ] && \
[ \"\$system\" = 200 ] && [ \"\$sources\" = 200 ] && \
[ \"\$sound\" = 200 ] && [ \"\$spotify\" = 200 ] && exit 0; \
sleep 3; done; \
echo \"streambox probes failed: control=\$control root=\$root system=\$system sources=\$sources sound=\$sound spotify=\$spotify\" >&2; exit 1"
    if ssh_remote "$verify_cmd"; then
        echo "  ✓ /, /system/data.json, /sources/state, /sound/setup/, /spotify/, and :8780/healthz answer"
    else
        finish_airplay_health_maintenance
        trap 'cleanup_remote_facts' EXIT
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
        trap 'cleanup_remote_facts' EXIT
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

# The HTML shell, unlike /system/data.json, carries the immutable CSS cache
# key. A wizard activated during install can otherwise look healthy while
# still advertising the prior build's stylesheet. Pin the browser-visible
# surface to the exact verified build we just installed.
echo "==> Verifying Status asset version (${SHA}${DIRTY})"
asset_probe="curl -fsS -m 4 \
-H $(shell_quote "Host: ${HOSTNAME_FOR_INSTALL}") \
http://127.0.0.1/system/ | grep -Fq $(shell_quote "/assets/app.css?v=${SHA}${DIRTY}\"")"
if ssh_remote "$asset_probe"; then
    echo "  ✓ /system/ advertises current design assets"
else
    finish_airplay_health_maintenance
    trap 'cleanup_remote_facts' EXIT
    echo "DEPLOY VERIFICATION FAILED: /system/ is serving a stale asset version." >&2
    echo "Expected /assets/app.css?v=${SHA}${DIRTY}" >&2
    exit 1
fi

verify_manifest_advanced
HEALTH_START_EPOCH="$(ssh_remote 'date +%s' 2>/dev/null | tr -dc '0-9')" || true
[[ -z "${HEALTH_START_EPOCH:-}" ]] && HEALTH_START_EPOCH=0
surface_system_health
if [[ "${HEALTH_START_EPOCH}" != "0" ]]; then
    report_oom_collateral "$HEALTH_START_EPOCH"
fi
if [[ "$OOM_PRODUCTION_HIT" == "1" ]]; then
    finish_airplay_health_maintenance
    trap 'cleanup_remote_facts' EXIT
    echo "─────────────────────────────────────────────────────────────" >&2
    echo " DEPLOY VERIFICATION FAILED: a live production daemon was" >&2
    echo " OOM-killed during this deploy. The Pi may have recovered," >&2
    echo " but this is not clean enough to merge." >&2
    echo " Diagnose on the Pi:" >&2
    echo "   sudo /opt/jasper/.venv/bin/jasper-doctor" >&2
    echo "   journalctl -k --since @${DEPLOY_START_EPOCH} --no-pager | grep -Ei 'oom|killed process'" >&2
    echo "─────────────────────────────────────────────────────────────" >&2
    exit 1
fi

finish_airplay_health_maintenance
cleanup_remote_facts
trap - EXIT
echo "==> Done."
