#!/usr/bin/env bash
# Flip the real-time voice provider the daemon uses. The three
# supported providers all sit behind the same `LiveConnection` /
# `LiveTurn` Protocols, so the daemon's wake/turn loop is unchanged
# regardless of which one is active — only the SDK calls beneath it
# differ. See docs/HANDOFF-voice-providers.md for the architecture
# and per-provider trade-offs.
#
# Each provider needs its own API key set in either the operator env
# (/etc/jasper/jasper.env) or the wizard-owned provider env
# (/var/lib/jasper/voice_provider.env) BEFORE switching to it. The
# daemon refuses to start if the active provider's key is missing.
# Other providers' keys may stay blank.
#
# Usage:
#   bash scripts/switch-voice-provider.sh gemini
#   bash scripts/switch-voice-provider.sh openai
#   bash scripts/switch-voice-provider.sh grok
#   bash scripts/switch-voice-provider.sh           # show current
#
# Pricing snapshot at the time of writing (2026-05):
#   gemini : ~$0.025 / minute  (3 / 12 USD per 1M audio tokens, with cap slack)
#   openai : ~$0.30  / minute  (32 / 64 / 0.40 USD per 1M tokens)
#   grok   :  $0.05  / minute  (flat $3.00 / hour, NOT token-based — note
#                                JASPER_DAILY_SPEND_CAP_USD will under-count)
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

PROVIDER="${1:-}"

case "$PROVIDER" in
    gemini|openai|grok) ;;
    "")
        echo "Current voice provider on ${PI_HOST}:"
        "${SSH[@]}" "sudo sh -c 'grep -h \"^JASPER_VOICE_PROVIDER=\" \"${PROVIDER_ENV}\" 2>/dev/null || echo \"(unset — visit http://${PI_HOST}/voice/)\"'"
        echo
        echo "Configured model overrides:"
        "${SSH[@]}" "sudo sh -c 'grep -h -E \"^JASPER_(GEMINI|OPENAI|GROK)_MODEL=\" \"${OPERATOR_ENV}\" \"${PROVIDER_ENV}\" 2>/dev/null || true'"
        echo
        echo "Usage:  bash scripts/switch-voice-provider.sh [gemini|openai|grok]"
        exit 0
        ;;
    *)
        echo "error: unknown provider '$PROVIDER'. Use 'gemini', 'openai', or 'grok'." >&2
        exit 2
        ;;
esac

# Sanity-check the active provider's API key BEFORE flipping the env —
# the daemon will refuse to start without it, which would leave the
# Pi voiceless if we don't catch it here.
case "$PROVIDER" in
    gemini) KEY_VAR=GEMINI_API_KEY ;;
    openai) KEY_VAR=OPENAI_API_KEY ;;
    grok)   KEY_VAR=XAI_API_KEY ;;
esac

KEY_LINE=$("${SSH[@]}" "sudo sh -c 'grep -h -E \"^${KEY_VAR}=.*\" \"${OPERATOR_ENV}\" \"${PROVIDER_ENV}\" 2>/dev/null | tail -1 || true'")
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
chown root:root "$tmp"
chmod 0600 "$tmp"
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
