# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# shellcheck shell=bash
# Shared header for laptop-side scripts. Source from a script with:
#
#   . "$(dirname "$0")/_lib.sh"
#
# After sourcing, PI_HOST, PI_USER, optional JASPER_HOSTNAME, and
# REPO_ROOT are exported and safe to use throughout the script.
# PI_HOST is the SSH transport target. JASPER_HOSTNAME is the speaker
# identity/cert hostname; new scripts should not treat it as an SSH
# target unless deliberately leaning on the compatibility fallback
# below.
#
# Responsibilities:
#   1. Resolve REPO_ROOT from the script's own location (so scripts
#      keep working regardless of the caller's cwd).
#   2. Source .env.local if present — this is where scripts/onboard.sh
#      persists PI_HOST/PI_USER for a checkout. Gitignored.
#   3. Apply the SSH-target fallback chain — PI_HOST in .env.local
#      wins; JASPER_HOSTNAME from the calling shell is kept only as a
#      compatibility fallback for older operator scripts/docs; jts.local
#      is the final default.
#
# This file is intentionally not executable and has no shebang — it
# only makes sense when sourced from a bash script that has already
# set its own `set -euo pipefail` posture.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# .env.local is sourced with `set -a` so its KEY=value lines get
# exported into the environment for subsequent commands and child
# processes. Don't fail if absent — many scripts work fine without it.
if [[ -f "${REPO_ROOT}/.env.local" ]]; then
    set -a
    # shellcheck disable=SC1091
    . "${REPO_ROOT}/.env.local"
    set +a
fi

# Compatibility fallback chain. The legacy form (used by every script in
# scripts/ before this lib existed) is:
#   PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
# This file centralizes it while the remaining legacy helpers are
# audited. New laptop-side code should read/set PI_HOST for SSH and
# reserve JASPER_HOSTNAME for speaker identity/cert URLs.
export PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
export PI_USER="${PI_USER:-pi}"

is_ipv4_host() {
    [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

# Quote one value so it survives a remote shell (`ssh host "cmd $(shell_quote x)"`).
# Single home for the helper — deploy-to-pi.sh and pi-run-diagnostic.sh
# previously each carried their own copy.
shell_quote() {
    printf '%q' "$1"
}

# Quote every argument and join with spaces — a full command line that
# can be passed through ssh to a remote shell unmangled.
quote_args() {
    local out="" arg
    for arg in "$@"; do
        out+="${out:+ }$(shell_quote "$arg")"
    done
    printf '%s' "$out"
}

normalize_speaker_hostname() {
    local host="${1%.}"
    if [[ -z "$host" ]] || is_ipv4_host "$host"; then
        return 1
    fi
    if [[ "$host" != *.* ]]; then
        host="${host}.local"
    fi
    printf '%s\n' "$host"
}

# Write the two laptop-side state files (.env.local + CLAUDE.local.md)
# in one shot. Called by onboard.sh after a successful install and by
# scripts/use to switch the active target without re-installing.
#
# Args: $1 = SSH target (jts.local or IP), $2 = user (pi), $3 = alias
# ("jts" for hostnames; empty for raw IPs), $4 = speaker hostname
# identity (jts.local; optional for state-only IP switches).
#
# Single source of truth for the template so onboard.sh + use stay
# in sync. Both files are gitignored (see .gitignore).
write_laptop_state() {
    local host="$1" usr="$2" alias="$3"
    local speaker_hostname="${4:-}"
    local active_line ssh_guidance speaker_line env_speaker_line
    if [[ -z "$speaker_hostname" ]]; then
        speaker_hostname="$(normalize_speaker_hostname "$host" 2>/dev/null || true)"
    fi
    if [[ -n "$speaker_hostname" ]]; then
        speaker_line="- **Speaker hostname**: \`${speaker_hostname}\`"
        env_speaker_line="JASPER_HOSTNAME=${speaker_hostname}"
    else
        speaker_line="- **Speaker hostname**: not recorded yet; deploy will ask the Pi or require \`JASPER_HOSTNAME=<name>.local\`."
        env_speaker_line="# JASPER_HOSTNAME=<speaker-hostname>.local"
    fi
    if [[ -n "$alias" ]]; then
        active_line="Active SSH target: \`${host}\` (SSH alias \`${alias}\` in \`~/.ssh/config\`)."
        ssh_guidance="When running commands against this Pi, prefer \`ssh ${alias} <cmd>\` over inline \`${usr}@${host}\` references — the alias is more durable across IP changes and keeps commands consistent."
    else
        active_line="Active SSH target: \`${host}\` (IP target — no SSH alias was created)."
        ssh_guidance="When running commands against this Pi, use \`ssh ${usr}@${host} <cmd>\` directly. If you'd prefer a stable alias, re-onboard with the mDNS hostname instead of the IP once it resolves."
    fi
    cat > "${REPO_ROOT}/.env.local" <<EOF
# Laptop-side state. Gitignored. Written by scripts/onboard.sh
# (full setup) or scripts/use (quick target switch).
PI_HOST=${host}
PI_USER=${usr}
${env_speaker_line}
EOF
    cat > "${REPO_ROOT}/CLAUDE.local.md" <<EOF
# Active speaker for this checkout (gitignored)

${active_line}

${ssh_guidance}

- **SSH target**: \`${usr}@${host}\`
${speaker_line}
- **Activated**: $(date -u +%Y-%m-%dT%H:%M:%SZ)

Switch this checkout to a different speaker without re-onboarding:
\`bash scripts/use <hostname>\`. Full re-onboard (rsync + install.sh
+ jasper-doctor): \`bash scripts/onboard.sh <hostname> --adopt\`. See
[AGENTS.md](AGENTS.md) "Laptop-side state" for the full convention.
EOF
}

# Deploy-target identity guard (TOFU). mDNS names are transport, not
# identity: after an Avahi collision rename or a re-image, PI_HOST can
# resolve to a DIFFERENT speaker than this checkout means. The first
# verified deploy records the target's stable peer_id
# (/var/lib/jasper/peer_id, advertised as the peer_id= TXT record on
# _jasper-control._tcp) into .env.local; later deploys compare.
#
#   verify_or_record_peer_id <remote_id> <env_file> [accept_new]
#
# Echoes one outcome token (consumed by deploy-to-pi.sh's messaging):
#   unavailable    remote has no peer_id yet (pre-identity build) — skip
#   no_state_file  no .env.local to record into (env-var-driven deploy) — skip
#   recorded       first contact: appended PI_PEER_ID=<id>
#   match          recorded identity matches the remote
#   rerecorded     mismatch + accept_new=1: recorded id replaced
#   mismatch       recorded identity does NOT match — caller must abort
# Returns 1 only on mismatch.
verify_or_record_peer_id() {
    local remote_id="$1" env_file="$2" accept_new="${3:-}"
    remote_id="$(printf '%s' "$remote_id" | tr -d '[:space:]')"
    if [[ -z "$remote_id" ]]; then
        echo "unavailable"
        return 0
    fi
    if [[ ! -f "$env_file" ]]; then
        echo "no_state_file"
        return 0
    fi
    local recorded
    recorded="$(grep -E '^PI_PEER_ID=' "$env_file" 2>/dev/null \
        | tail -n1 | cut -d= -f2- | tr -d '[:space:]')"
    if [[ -z "$recorded" ]]; then
        # A hand-edited .env.local may lack a trailing newline; a bare
        # append would glue PI_PEER_ID onto the last line and silently
        # corrupt it (e.g. `PI_USER=piPI_PEER_ID=…`). `tail -c 1` via
        # $() is empty exactly when the last byte is a newline.
        if [[ -s "$env_file" && -n "$(tail -c 1 "$env_file")" ]]; then
            printf '\n' >> "$env_file"
        fi
        printf 'PI_PEER_ID=%s\n' "$remote_id" >> "$env_file"
        echo "recorded"
        return 0
    fi
    if [[ "$recorded" == "$remote_id" ]]; then
        echo "match"
        return 0
    fi
    if [[ "$accept_new" == "1" ]]; then
        if sed -i.bak "s/^PI_PEER_ID=.*/PI_PEER_ID=${remote_id}/" "$env_file" 2>/dev/null; then
            rm -f "${env_file}.bak"
            echo "rerecorded"
            return 0
        fi
        rm -f "${env_file}.bak"
        # Could not rewrite — fall through to mismatch so the caller
        # aborts rather than silently deploying unverified.
    fi
    echo "mismatch recorded=${recorded} remote=${remote_id}"
    return 1
}

# build_manifest_value <manifest_text> <key>
#
# Extract the value of a KEY=value line from a /var/lib/jasper/build.txt
# manifest read over ssh. Tolerates CRLF (interactive-sudo deploys read
# through `ssh -tt`, which rewrites line endings) and surrounding
# whitespace; last occurrence wins. Echoes "" when the key is absent.
# pipefail-safe: every stage exits 0 on no-match.
build_manifest_value() {
    local manifest="$1" key="$2"
    printf '%s\n' "$manifest" | tr -d '\r' \
        | sed -n "s/^${key}=//p" | tail -n1 | tr -d '[:space:]'
    return 0
}

# classify_deploy_direction <local_sha> <installed_sha>
#
# Compare the commit about to be deployed against the commit recorded in
# the Pi's build manifest, using the current checkout's git history.
# `-dirty` suffixes (build.txt records them for uncommitted-tree deploys)
# are stripped before comparison. Echoes one outcome token (consumed by
# deploy-to-pi.sh's direction preflight):
#
#   same               redeploying the installed commit
#   forward            installed is an ancestor of local — normal upgrade
#   downgrade          local is an ancestor of installed — this deploy
#                      would REVERT commits the Pi already runs (the
#                      2026-06-11 JTS3 incident: a stale parallel
#                      checkout silently reverted same-day fixes)
#   diverged           histories split — neither contains the other
#                      (two branches deploying to one Pi)
#   unknown_installed  installed SHA not in this checkout's history
#                      (caller should fetch and retry once)
#
# Always returns 0: the abort decision depends on an operator override
# that the caller owns, not this helper.
classify_deploy_direction() {
    local local_sha="${1%-dirty}" installed_sha="${2%-dirty}"
    if [[ -z "$local_sha" || -z "$installed_sha" ]]; then
        echo "unknown_installed"
        return 0
    fi
    if [[ "$local_sha" == "$installed_sha" ]]; then
        echo "same"
        return 0
    fi
    if ! git cat-file -e "${installed_sha}^{commit}" 2>/dev/null; then
        echo "unknown_installed"
        return 0
    fi
    if git merge-base --is-ancestor "$installed_sha" "$local_sha" 2>/dev/null; then
        echo "forward"
        return 0
    fi
    if git merge-base --is-ancestor "$local_sha" "$installed_sha" 2>/dev/null; then
        echo "downgrade"
        return 0
    fi
    echo "diverged"
    return 0
}

# classify_installed_vs_main <installed_sha> [main_ref]
#
# Binary staleness check for the deploy preflight: is the commit the Pi
# currently runs (from its build manifest) current relative to
# origin/main, or behind it? Bench/test Pis silently drift far behind
# main, and a stale build misses newer safety gates.
#
# The signal is intentionally BINARY (current vs behind), NEVER a commit
# count: whether a box is 1 or 120 commits behind, the action is the same
# — update it. "Current" means the installed commit IS origin/main's tip
# OR a descendant of it (`git merge-base --is-ancestor <main_ref>
# <installed>`); anything else is "behind".
#
# `-dirty` suffixes (build.txt records them for uncommitted-tree deploys)
# are stripped before comparison. Echoes one token:
#
#   current   installed is origin/main's tip or a descendant — up to date
#   behind    installed predates origin/main's tip — stale, advise update
#   unknown   cannot compare — empty SHA, origin/main does not resolve (no
#             remote / never fetched), or the installed commit is absent
#             from this checkout. The caller skips the advisory rather
#             than guessing; this never blocks a deploy.
#
# Always returns 0: this drives an advisory print, not a deploy abort.
classify_installed_vs_main() {
    local installed_sha="${1%-dirty}" main_ref="${2:-origin/main}"
    if [[ -z "$installed_sha" ]]; then
        echo "unknown"
        return 0
    fi
    if ! git rev-parse --verify --quiet "${main_ref}^{commit}" >/dev/null 2>&1; then
        echo "unknown"
        return 0
    fi
    if ! git cat-file -e "${installed_sha}^{commit}" 2>/dev/null; then
        echo "unknown"
        return 0
    fi
    if git merge-base --is-ancestor "$main_ref" "$installed_sha" 2>/dev/null; then
        echo "current"
        return 0
    fi
    echo "behind"
    return 0
}

# ── OOM-collateral parsing (deploy post-install surfacing) ───────────────
#
# Problem #2/#5 (docs/install-update-resilience-plan.md): on jts2 a source
# build OOM-killed nginx AND jasper-voice, and the deploy tooling exited
# silently — the collateral was only discoverable by SSHing in to read the
# journal. deploy-to-pi.sh now scans the kernel log for the install window
# and surfaces what was killed. These are the pure parsers behind that;
# the ssh/journalctl I/O stays in deploy-to-pi.sh so they unit-test against
# captured journal text.
#
# Two readings of one kernel OOM event:
#   - the victim's cgroup (task_memcg=/system.slice/<unit>.service) names
#     the systemd UNIT reliably — this is what we gate on, because a venv
#     console-script daemon (jasper-voice) is execve'd as the interpreter,
#     so its process `comm` reads `python3`, NOT its unit name;
#   - the process comm (`Killed process N (comm)` / `task=comm`) is the
#     human-friendly "what died" line, and is the only signal for build
#     tools (cc1plus, cargo, …) which run in a transient ssh scope, not a
#     named .service.

# oom_killed_units <kernel_log_text>
# Echo the distinct systemd unit names of OOM victims, newline-separated,
# parsed from the cgroup-v2 task_memcg=/oom_memcg= fields. Empty when the
# log carries no memcg field (older kernels) — callers fall back to comms.
# pipefail-safe: no-match stages exit 0.
oom_killed_units() {
    local text="$1"
    printf '%s\n' "$text" \
        | grep -oE '(task_memcg|oom_memcg)=[^,[:space:]]+' 2>/dev/null \
        | grep -oE '[A-Za-z0-9@._-]+\.service' 2>/dev/null \
        | sort -u || true
    return 0
}

# oom_killed_comms <kernel_log_text>
# Echo the distinct victim process names (comm), newline-separated, from
# both the classic `Killed process N (comm)` line and the structured
# `,task=comm,` field. Human context only — see oom_killed_units for the
# reliable production-daemon signal. pipefail-safe.
oom_killed_comms() {
    local text="$1"
    {
        printf '%s\n' "$text" \
            | grep -oE 'Killed process [0-9]+ \([^)]+\)' 2>/dev/null \
            | sed -E 's/.*\(([^)]+)\)/\1/' || true
        printf '%s\n' "$text" \
            | grep -oE '[, ]task=[^,]+' 2>/dev/null \
            | sed -E 's/.*task=//' || true
    } | sed '/^[[:space:]]*$/d' | sort -u
    return 0
}

# oom_unit_is_production <unit_name>
# Return 0 when the OOM-killed unit is a live production daemon whose death
# during an update is a real incident (problem #2). Build steps run in a
# transient ssh scope, not these units, so a build-tool OOM never matches.
# This is the laptop-side analog of the on-Pi production-daemon set in
# jasper/cli/doctor/_shared.py (`_RUNTIME_STATE_UNITS`); they can't share
# (bash vs Python, laptop vs Pi). The `jasper-*` glob absorbs new jasper
# daemons; only the non-jasper tail (nginx/shairport/librespot/…) is a
# hand-maintained list that could drift — keep it in sync if that set grows.
oom_unit_is_production() {
    case "$1" in
        jasper-*.service|nginx.service|shairport-sync.service|\
        librespot.service|bluealsa-aplay.service|nqptp.service)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}
