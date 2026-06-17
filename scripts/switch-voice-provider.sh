#!/usr/bin/env bash
# Flip the real-time voice provider the daemon uses. Supported
# providers are read from the installed Python catalog on the Pi. They
# all sit behind the same `LiveConnection` /
# `LiveTurn` Protocols, so the daemon's wake/turn loop is unchanged
# regardless of which one is active — only the SDK calls beneath it
# differ. See docs/HANDOFF-voice-providers.md for the architecture
# and per-provider trade-offs.
#
# Each provider needs its own API key set in either the operator env
# (/etc/jasper/jasper.env) or the wizard-owned keys file
# (/var/lib/jasper-secrets/voice_keys.env — WS1 Phase 4a split the API keys
# out of voice_provider.env into the group-jasper-secrets compartment) BEFORE
# switching to it. The daemon refuses to start if the active provider's key is
# missing. Other providers' keys may stay blank.
#
# Usage:
#   bash scripts/switch-voice-provider.sh <provider-id>
#   bash scripts/switch-voice-provider.sh           # show current
#
# Defaults: PI_HOST/PI_USER come from .env.local when present, then
# PI_HOST falls back to JASPER_HOSTNAME and jts.local.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "${SCRIPT_DIR}/_lib.sh"

SSH=(ssh -o ConnectTimeout=5 "${PI_USER}@${PI_HOST}")
OPERATOR_ENV="/etc/jasper/jasper.env"
PROVIDER_ENV="/var/lib/jasper/voice_provider.env"
# WS1 Phase 4a — the provider API keys live here now (group jasper-secrets),
# split out of PROVIDER_ENV (which keeps the non-secret provider/model).
KEYS_ENV="/var/lib/jasper-secrets/voice_keys.env"
CATALOG_PY="/opt/jasper/.venv/bin/python"

PROVIDER="${1:-}"

fetch_provider_catalog() {
    "${SSH[@]}" "sudo ${CATALOG_PY} -c 'from jasper.voice.catalog import PROVIDERS
for provider in PROVIDERS:
    print(\"%s\t%s\t%s\" % (provider.id, provider.key_env, provider.model_env))'"
}

provider_ids_for_usage() {
    awk -F '\t' 'BEGIN { sep = "" } { printf "%s%s", sep, $1; sep = "|" } END { print "" }'
}

lookup_catalog_field() {
    local provider="$1"
    local column="$2"
    printf '%s\n' "$CATALOG_ROWS" \
        | awk -F '\t' -v provider="$provider" -v column="$column" '
            $1 == provider { print $column; found = 1; exit }
            END { if (!found) exit 1 }
        '
}

CATALOG_ROWS="$(fetch_provider_catalog)" || {
    echo "error: could not read the installed voice provider catalog on ${PI_HOST}." >&2
    echo "       Re-run deploy/install so ${CATALOG_PY} can import jasper.voice.catalog." >&2
    exit 2
}
PROVIDER_USAGE="$(printf '%s\n' "$CATALOG_ROWS" | provider_ids_for_usage)"

if [[ -z "$PROVIDER" ]]; then
    echo "Current voice provider on ${PI_HOST}:"
    "${SSH[@]}" "sudo sh -c 'grep -h \"^JASPER_VOICE_PROVIDER=\" \"${PROVIDER_ENV}\" 2>/dev/null || echo \"(unset — visit http://${PI_HOST}/voice/)\"'"
    echo
    MODEL_ENV_REGEX="$(printf '%s\n' "$CATALOG_ROWS" \
        | awk -F '\t' 'BEGIN { sep = "" } { printf "%s%s", sep, $3; sep = "|" } END { print "" }')"
    echo "Configured model overrides:"
    "${SSH[@]}" "sudo sh -c 'grep -h -E \"^(${MODEL_ENV_REGEX})=\" \"${OPERATOR_ENV}\" \"${PROVIDER_ENV}\" 2>/dev/null || true'"
    echo
    echo "Usage:  bash scripts/switch-voice-provider.sh [${PROVIDER_USAGE}]"
    exit 0
fi

if ! KEY_VAR="$(lookup_catalog_field "$PROVIDER" 2)"; then
    echo "error: unknown provider '$PROVIDER'. Use one of: ${PROVIDER_USAGE}" >&2
    exit 2
fi

# Sanity-check the active provider's API key BEFORE flipping the env —
# the daemon will refuse to start without it, which would leave the
# Pi voiceless if we don't catch it here. WS1 Phase 4a: the key lives in
# KEYS_ENV (group jasper-secrets) or the operator's jasper.env; PROVIDER_ENV is
# still grepped to cover a not-yet-migrated Pi (tail -1 wins on the last match).
KEY_LINE=$("${SSH[@]}" "sudo sh -c 'grep -h -E \"^${KEY_VAR}=.*\" \"${OPERATOR_ENV}\" \"${KEYS_ENV}\" \"${PROVIDER_ENV}\" 2>/dev/null | tail -1 || true'")
if [[ -z "$KEY_LINE" || "$KEY_LINE" == "${KEY_VAR}=" ]]; then
    echo "error: ${KEY_VAR} is not set for the effective voice config on ${PI_HOST}." >&2
    echo "       Set it via http://${PI_HOST}/voice/ or ${OPERATOR_ENV}, then re-run." >&2
    exit 3
fi

echo "Switching ${PI_HOST}:JASPER_VOICE_PROVIDER → ${PROVIDER}"
"${SSH[@]}" "sudo sh -s -- ${PROVIDER}" <<'REMOTE'
set -eu
provider="$1"
env="/var/lib/jasper/voice_provider.env"
install -d -m 0750 /var/lib/jasper
tmp="$(mktemp "${env}.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

if [ -f "$env" ]; then
    grep -v '^JASPER_VOICE_PROVIDER=' "$env" > "$tmp" || true
fi
printf 'JASPER_VOICE_PROVIDER=%s\n' "$provider" >> "$tmp"
# WS1 Phase 4a/3b — voice_provider.env is the NON-secret SSOT (the API keys live
# in voice_keys.env now), so it must be group-jasper readable (0640) for the
# non-root jasper-control to fresh-read the active provider for /system/. Writing
# 0600 root:root here re-broke that read until the next deploy/restart. (systemd
# StateDirectory re-owns it to <voice|mux>:jasper on the restart below; we set
# the right group+mode now so the read works in the interim.)
chown root:root "$tmp"
chgrp jasper "$tmp" 2>/dev/null || true
chmod 0640 "$tmp"
mv "$tmp" "$env"
trap - EXIT

grep '^JASPER_VOICE_PROVIDER=' "$env"
systemctl restart jasper-voice
sleep 2
systemctl is-active jasper-voice
journalctl -u jasper-voice -n 5 --no-pager 2>&1 \
    | grep -v -E 'GetGpuDevices|device_discovery' \
    | tail -5
REMOTE
