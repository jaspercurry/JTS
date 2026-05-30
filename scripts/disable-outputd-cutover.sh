#!/usr/bin/env bash
# Stop the outputd service on the active Pi before, or immediately
# after, rolling back to a pre-outputd release/branch.
#
# Why this exists: pre-outputd code does not know about
# jasper-outputd, so a rollback deploy cannot disable a unit introduced
# later. If jasper-outputd keeps the direct DAC open while older code
# returns Camilla/TTS to the legacy jasper_out dmix path, rollback
# audio can fail with "device busy".

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
. "${SCRIPT_DIR}/_lib.sh"

echo "Stopping jasper-outputd on ${PI_USER}@${PI_HOST}..." >&2

remote_script='
set -euo pipefail

if systemctl list-unit-files jasper-outputd.service >/dev/null 2>&1; then
    sudo systemctl disable --now jasper-outputd.service >/dev/null 2>&1 || true
    sudo systemctl reset-failed jasper-outputd.service >/dev/null 2>&1 || true
fi

if systemctl is-active --quiet jasper-outputd.service 2>/dev/null; then
    echo "ERROR: jasper-outputd is still active after disable --now" >&2
    exit 1
fi

echo "jasper-outputd is stopped/disabled."

if systemctl cat jasper-voice.service 2>/dev/null | grep -q "JASPER_TTS_TRANSPORT=outputd"; then
    echo "Current jasper-voice unit still contains the outputd runtime override."
    echo "Deploy the pre-outputd rollback tree next, then run this helper again if rollback audio needs a restart."
else
    sudo systemctl restart jasper-camilla.service jasper-voice.service >/dev/null 2>&1 || true
    echo "Legacy jasper-camilla/jasper-voice restart attempted."
fi
'

ssh -o BatchMode=yes -o ConnectTimeout=5 "${PI_USER}@${PI_HOST}" "bash -s" \
    <<< "${remote_script}"
