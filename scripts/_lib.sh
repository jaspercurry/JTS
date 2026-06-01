# Shared header for laptop-side scripts. Source from a script with:
#
#   . "$(dirname "$0")/_lib.sh"
#
# After sourcing, PI_HOST, PI_USER, optional JASPER_HOSTNAME, and
# REPO_ROOT are exported and safe to use throughout the script.
#
# Responsibilities:
#   1. Resolve REPO_ROOT from the script's own location (so scripts
#      keep working regardless of the caller's cwd).
#   2. Source .env.local if present — this is where scripts/onboard.sh
#      persists PI_HOST/PI_USER for a checkout. Gitignored.
#   3. Apply the documented SSH-target fallback chain — PI_HOST in
#      .env.local wins; JASPER_HOSTNAME from the calling shell is the
#      legacy fallback; jts.local is the final default.
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

# Documented fallback chain. The legacy form (used by every script in
# scripts/ before this lib existed) is:
#   PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
# This file centralizes it so scripts can stop reinventing the chain.
export PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
export PI_USER="${PI_USER:-pi}"

is_ipv4_host() {
    [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]
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
+ jasper-doctor): \`bash scripts/onboard.sh <hostname>\`. See
[AGENTS.md](AGENTS.md) "Laptop-side state" for the full convention.
EOF
}
