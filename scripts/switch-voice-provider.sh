#!/usr/bin/env bash
# Flip the real-time voice provider the daemon uses. The three
# supported providers all sit behind the same `LiveConnection` /
# `LiveTurn` Protocols, so the daemon's wake/turn loop is unchanged
# regardless of which one is active — only the SDK calls beneath it
# differ. See docs/HANDOFF-voice-providers.md for the architecture
# and per-provider trade-offs.
#
# Each provider needs its own API key set in /etc/jasper/jasper.env
# BEFORE switching to it. The daemon refuses to start if the active
# provider's key is missing. Other providers' keys may stay blank.
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
# Defaults: PI_HOST falls back to JASPER_HOSTNAME, then to jts.local.
# PI_USER=pi. Override either via env.

set -euo pipefail

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"
SSH="ssh -o ConnectTimeout=5 ${PI_USER}@${PI_HOST}"

PROVIDER="${1:-}"

case "$PROVIDER" in
    gemini|openai|grok) ;;
    "")
        echo "Current voice provider on ${PI_HOST}:"
        $SSH "sudo grep '^JASPER_VOICE_PROVIDER=' /etc/jasper/jasper.env"
        echo
        echo "Active model:"
        $SSH "sudo grep -E '^JASPER_(GEMINI|OPENAI|GROK)_MODEL=' /etc/jasper/jasper.env"
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

KEY_LINE=$($SSH "sudo grep -E \"^${KEY_VAR}=\" /etc/jasper/jasper.env || true")
if [[ -z "$KEY_LINE" || "$KEY_LINE" == "${KEY_VAR}=" ]]; then
    echo "error: ${KEY_VAR} is not set in /etc/jasper/jasper.env on ${PI_HOST}." >&2
    echo "       Set it first (visit the provider's console for a key) then re-run." >&2
    exit 3
fi

echo "Switching ${PI_HOST}:JASPER_VOICE_PROVIDER → ${PROVIDER}"
$SSH "sudo sed -i 's|^JASPER_VOICE_PROVIDER=.*|JASPER_VOICE_PROVIDER=${PROVIDER}|' /etc/jasper/jasper.env && \
      sudo grep '^JASPER_VOICE_PROVIDER=' /etc/jasper/jasper.env && \
      sudo systemctl restart jasper-voice && \
      sleep 2 && \
      systemctl is-active jasper-voice && \
      sudo journalctl -u jasper-voice -n 5 --no-pager 2>&1 | grep -v -E 'GetGpuDevices|device_discovery' | tail -5"
