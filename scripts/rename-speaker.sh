#!/usr/bin/env bash
# Rename a speaker — the supported way to change a JTS hostname.
#
# A speaker's name fans out into the OS hostname, Avahi's mDNS
# advertisement, JASPER_HOSTNAME (management allowlist, spoken URLs,
# OAuth bounce), and the /correction/ TLS cert SAN. Renaming by hand
# (`hostnamectl` alone) converges only the first two and leaves the
# rest drifted — the exact fragile state docs/HANDOFF-identity.md
# describes. This script converges all of them in one deliberate
# operation:
#
#   1. preflight: target reachable; new name not already claimed on
#      the LAN (probed via avahi from the Pi itself)
#   2. Pi: hostnamectl set-hostname + /etc/hosts 127.0.1.1 line
#   3. Pi: JASPER_HOSTNAME=<new>.local in /etc/jasper/jasper.env
#   4. Pi: restart avahi-daemon (re-advertise) + run
#      jasper-identity-reconcile (refresh identity.env immediately)
#   5. laptop: .env.local + CLAUDE.local.md flip (scripts/use shape)
#   6. full deploy under the new name — regenerates the TLS leaf cert
#      SAN and ends with the management-surface verification probe, so
#      a rename that breaks the UI fails loudly here, not days later
#      (skip with --no-deploy if you must, e.g. offline)
#
# Usage:
#   bash scripts/rename-speaker.sh jts4            # or jts4.local
#   bash scripts/rename-speaker.sh jts4 --no-deploy
#
# The current target comes from .env.local (PI_HOST/PI_USER), like
# every other laptop-side script. Other checkouts pointing at the old
# name need a one-shot `bash scripts/use <new>.local` themselves.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_lib.sh
. "${SCRIPT_DIR}/_lib.sh"

NEW_RAW="${1:-}"
DEPLOY=1
if [[ "${2:-}" == "--no-deploy" ]]; then
    DEPLOY=0
fi

if [[ -z "$NEW_RAW" ]]; then
    echo "rename-speaker: new hostname required" >&2
    echo "  usage: bash scripts/rename-speaker.sh <new-name> [--no-deploy]" >&2
    echo "  example: bash scripts/rename-speaker.sh jts4" >&2
    exit 2
fi

if ! NEW_FQDN="$(normalize_speaker_hostname "$NEW_RAW")"; then
    echo "rename-speaker: '$NEW_RAW' is not a usable hostname (IPs not allowed)" >&2
    exit 2
fi
NEW_BASE="${NEW_FQDN%.local}"
if ! [[ "$NEW_BASE" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]]; then
    echo "rename-speaker: '$NEW_BASE' is not a valid mDNS label" \
         "(lowercase letters, digits, inner hyphens)" >&2
    exit 2
fi

PI_HOST="${PI_HOST:-jts.local}"
PI_USER="${PI_USER:-pi}"
SSH_TARGET="${PI_USER}@${PI_HOST}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8)

remote() {
    ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "$@"
}
remote_sudo() {
    remote "sudo -n $1"
}

echo "==> Renaming ${PI_HOST} → ${NEW_FQDN}"

# Preflight: current target answers, with passwordless sudo (the same
# posture deploy-to-pi.sh requires — see BRINGUP.md Phase 2.5).
if ! remote_sudo "true" >/dev/null 2>&1; then
    echo "rename-speaker: cannot reach ${SSH_TARGET} with passwordless sudo" >&2
    exit 1
fi

OLD_BASE="$(remote "hostname" | tr -d '[:space:]')"
if [[ "$OLD_BASE" == "$NEW_BASE" ]]; then
    echo "==> Hostname is already '${NEW_BASE}' — converging the derived surfaces only."
fi

# Preflight: is the new name already claimed by a DIFFERENT device?
# Probed from the Pi via avahi so we test the LAN segment the speaker
# actually lives on (the laptop may be on another subnet/VPN).
RESOLVED_IP="$(remote "avahi-resolve-host-name -4 ${NEW_FQDN} 2>/dev/null" \
    | awk '{print $2}' || true)"
if [[ -n "$RESOLVED_IP" ]]; then
    OWN_IPS="$(remote "hostname -I" || true)"
    if ! grep -qw "$RESOLVED_IP" <<<"$OWN_IPS"; then
        echo "rename-speaker: ${NEW_FQDN} already resolves to ${RESOLVED_IP}" \
             "(not this speaker) — pick a different name" >&2
        exit 1
    fi
fi

echo "==> [Pi] hostnamectl set-hostname ${NEW_BASE} (+ /etc/hosts)"
remote_sudo "hostnamectl set-hostname $(shell_quote "$NEW_BASE")"
# Raspberry Pi OS resolves its own name via the 127.0.1.1 line; a stale
# entry makes every sudo print 'unable to resolve host'. Replace it when
# present, append it when an image ships without one — a silent sed
# no-op would leave that sudo noise behind. NEW_BASE is regex-validated
# above (lowercase mDNS label), so embedding it is injection-safe.
remote_sudo "bash -c 'if grep -qE \"^127\.0\.1\.1\" /etc/hosts; then \
sed -i \"s/^127\.0\.1\.1.*/127.0.1.1\t${NEW_BASE}/\" /etc/hosts; \
else printf \"127.0.1.1\t%s\n\" \"${NEW_BASE}\" >> /etc/hosts; fi'"

echo "==> [Pi] JASPER_HOSTNAME=${NEW_FQDN} in /etc/jasper/jasper.env"
remote_sudo "bash -c 'if grep -qE \"^JASPER_HOSTNAME=\" /etc/jasper/jasper.env; then \
sed -i \"s|^JASPER_HOSTNAME=.*|JASPER_HOSTNAME=${NEW_FQDN}|\" /etc/jasper/jasper.env; \
else printf \"JASPER_HOSTNAME=%s\\n\" \"${NEW_FQDN}\" >> /etc/jasper/jasper.env; fi'"

echo "==> [Pi] restart avahi-daemon + refresh identity snapshot"
remote_sudo "systemctl restart avahi-daemon"
remote_sudo "systemctl start jasper-identity-reconcile" || \
    echo "  (identity reconcile not installed yet — deploy will install it)"

echo "==> [laptop] pointing this checkout at ${NEW_FQDN}"
ALIAS="${NEW_FQDN%%.*}"
write_laptop_state "$NEW_FQDN" "$PI_USER" "$ALIAS" "$NEW_FQDN"

# Wait for the new name to resolve from the laptop before deploying.
echo "==> Waiting for ${NEW_FQDN} to answer (mDNS re-advertisement)…"
NEW_TARGET="${PI_USER}@${NEW_FQDN}"
for attempt in 1 2 3 4 5 6; do
    if ssh "${SSH_OPTS[@]}" "$NEW_TARGET" true 2>/dev/null; then
        break
    fi
    if [[ "$attempt" == 6 ]]; then
        echo "rename-speaker: ${NEW_FQDN} not reachable after rename." >&2
        echo "  The Pi answered at ${PI_HOST} moments ago; give mDNS a" >&2
        echo "  minute and retry: ssh ${NEW_TARGET}" >&2
        exit 1
    fi
    sleep 5
done

if [[ "$DEPLOY" == "1" ]]; then
    # Full deploy under the new identity: regenerates the TLS leaf cert
    # (SAN = new hostname), restarts daemons so Config.hostname-derived
    # surfaces (spoken URLs, OAuth bounce) pick up the change, and ends
    # with the management-surface probe under the new Host.
    echo "==> Deploying under the new identity (cert SAN, daemon restarts, verification)"
    PI_HOST="$NEW_FQDN" PI_USER="$PI_USER" JASPER_HOSTNAME="$NEW_FQDN" \
        bash "${SCRIPT_DIR}/deploy-to-pi.sh"
    # jasper-voice bakes Config.hostname into spoken management URLs at
    # startup; the deploy's reconcile only restarts it for mic-path
    # changes. try-restart: running → restart; parked → leave parked.
    ssh "${SSH_OPTS[@]}" "$NEW_TARGET" "sudo -n systemctl try-restart jasper-voice" \
        >/dev/null 2>&1 || true
else
    echo "==> --no-deploy: cert SAN + daemon env are still on the old name."
    echo "    Finish later with: bash scripts/deploy-to-pi.sh"
fi

echo "==> Done. Speaker is ${NEW_FQDN}"
echo "    verify: curl -s http://${NEW_FQDN}:8780/state | jq .resilience.identity"
echo "    note: an onboard-time ssh alias for the old name may linger in"
echo "    ~/.ssh/config; update its HostName if you use it."
