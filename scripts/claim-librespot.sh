#!/usr/bin/env bash
# One-time setup: log librespot in as a Spotify account so cold-start
# "Hey Jarvis, play X" works without needing a phone to claim the
# speaker first.
#
# Why this script exists:
#   librespot needs to be authenticated to a Spotify account for the
#   speaker to appear in that account's Spotify Web API `devices()`
#   list. Without that, the voice tool's `start_playback(device=JTS)`
#   call has nowhere to send playback. Two ways to authenticate:
#     1. Tap JTS in your phone's Spotify app → librespot's zeroconf
#        addUser endpoint receives an auth blob from your phone.
#     2. librespot's --enable-oauth flow → librespot prints a URL,
#        you sign into Spotify in any browser, librespot saves the
#        creds in --system-cache, persists across restarts.
#   With --system-cache enabled in librespot.service, EITHER method
#   produces persistent credentials. This script automates #2 so you
#   don't need to involve a phone at all.
#
# Why SSH tunnel:
#   librespot's OAuth callback is hardcoded to http://127.0.0.1:8091.
#   After Spotify auth, your browser navigates to that URL — which
#   only resolves to the machine running the browser, NOT the Pi.
#   We tunnel localhost:8091 on your laptop → 127.0.0.1:8091 on the
#   Pi so the redirect lands at librespot.
#
# Usage:
#   bash scripts/claim-librespot.sh
#
# Defaults: PI_HOST=${JASPER_HOSTNAME:-jts.local}, PI_USER=pi,
# OAUTH_PORT=8091. Override via env if needed.

set -euo pipefail

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"
OAUTH_PORT="${OAUTH_PORT:-8091}"
SYSTEM_CACHE="/var/cache/librespot"
SSH_OPTS=(-o ConnectTimeout=5 -o BatchMode=no)

TUNNEL_SOCK="/tmp/jts-claim-librespot-$$.sock"
CLAIM_LOG="/tmp/jts-claim-librespot-$$.log"
CLAIM_PID_FILE="/tmp/jts-claim-librespot-$$.pid"

ssh_pi() { ssh "${SSH_OPTS[@]}" "${PI_USER}@${PI_HOST}" "$@"; }

cleanup() {
    set +e
    # Kill the OAuth-mode librespot if it's still running.
    if [[ -f /tmp/.last-claim-pid ]]; then
        ssh_pi "sudo pkill -F ${CLAIM_PID_FILE} 2>/dev/null; sudo rm -f ${CLAIM_PID_FILE} ${CLAIM_LOG}"
    else
        ssh_pi "sudo pkill -f 'librespot --enable-oauth' 2>/dev/null; sudo rm -f ${CLAIM_PID_FILE} ${CLAIM_LOG}" 2>/dev/null
    fi
    # Always make sure the real librespot service is back up.
    ssh_pi 'sudo systemctl start librespot 2>/dev/null' >/dev/null 2>&1 || true
    # Tear down the local SSH tunnel.
    if [[ -S "$TUNNEL_SOCK" ]]; then
        ssh -S "$TUNNEL_SOCK" -O exit "${PI_USER}@${PI_HOST}" 2>/dev/null || true
    fi
    set -e
}
trap cleanup EXIT INT TERM

echo "==> Verifying SSH access to ${PI_HOST}"
ssh_pi 'echo ok' >/dev/null

echo "==> Setting up local port forward localhost:${OAUTH_PORT} → ${PI_HOST}:${OAUTH_PORT}"
ssh -fN -M -S "$TUNNEL_SOCK" "${SSH_OPTS[@]}" \
    -L "${OAUTH_PORT}:127.0.0.1:${OAUTH_PORT}" \
    "${PI_USER}@${PI_HOST}"

echo "==> Stopping the regular librespot service"
ssh_pi 'sudo systemctl stop librespot'

echo "==> Starting librespot in OAuth mode (background, logging to ${CLAIM_LOG})"
# nohup + background + record PID so cleanup can kill it cleanly.
# --backend pipe + --device /dev/null + --disable-discovery: a quiet
# OAuth-only librespot that won't compete with anything for audio or
# advertise itself on the LAN during the brief auth window.
ssh_pi "sudo -u pi -g audio bash -c '
    nohup /usr/bin/librespot \
        --enable-oauth \
        --oauth-port ${OAUTH_PORT} \
        --system-cache ${SYSTEM_CACHE} \
        --name JTS-Claim \
        --backend pipe \
        --device /dev/null \
        --disable-discovery \
        > ${CLAIM_LOG} 2>&1 &
    echo \$! > ${CLAIM_PID_FILE}
'"
touch /tmp/.last-claim-pid

echo "==> Waiting for librespot to print the OAuth URL..."
URL=""
for _ in $(seq 1 30); do
    URL=$(ssh_pi "grep -oE 'https://accounts\.spotify\.com/authorize\?[^[:space:]]+' ${CLAIM_LOG} 2>/dev/null | head -1" || true)
    [[ -n "$URL" ]] && break
    sleep 0.5
done

if [[ -z "$URL" ]]; then
    echo "ERROR: librespot did not print an OAuth URL within 15s. Tail of log:"
    ssh_pi "tail -20 ${CLAIM_LOG} 2>/dev/null" || true
    exit 1
fi

echo
echo "==> Open this URL in your browser to sign in:"
echo "    $URL"
echo
# Auto-open if we can.
if command -v open >/dev/null 2>&1; then
    open "$URL"
elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 || true
fi

echo "==> Sign in to Spotify, accept the permissions."
echo "    The browser will redirect to localhost:${OAUTH_PORT} via SSH tunnel."
echo "    Waiting up to 5 minutes for credentials to be saved..."
echo

DEADLINE=$(( $(date +%s) + 300 ))
SAVED=0
while [[ $(date +%s) -lt $DEADLINE ]]; do
    if ssh_pi "test -e ${SYSTEM_CACHE}/credentials.json"; then
        SAVED=1
        break
    fi
    sleep 2
done

if [[ $SAVED -eq 0 ]]; then
    echo "ERROR: Timed out waiting for credentials. OAuth not completed?"
    echo "       Check the librespot log:  ssh ${PI_USER}@${PI_HOST} 'cat ${CLAIM_LOG}'"
    exit 1
fi

echo "==> ✓ Credentials saved at ${SYSTEM_CACHE}/credentials.json"
echo "==> Stopping OAuth-mode librespot, restarting the real service"
ssh_pi "sudo pkill -F ${CLAIM_PID_FILE} 2>/dev/null; sudo rm -f ${CLAIM_PID_FILE} ${CLAIM_LOG}"
ssh_pi 'sudo systemctl restart librespot'
sleep 1

if ssh_pi 'sudo systemctl is-active librespot' >/dev/null; then
    echo
    echo "==> Done. librespot is logged in as your Spotify account."
    echo "    Try: 'Hey Jarvis, play Release Radar'"
else
    echo "WARNING: librespot is not active. Check:"
    echo "    ssh ${PI_USER}@${PI_HOST} 'sudo journalctl -u librespot -n 30'"
    exit 1
fi
