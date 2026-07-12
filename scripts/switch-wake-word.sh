#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Flip the wake-word model the daemon loads. Models are defined in
# jasper/wake_models.py — this script just looks one up by key, writes
# /var/lib/jasper/wake_model.env on the Pi, and restarts jasper-voice.
#
# Same effect as visiting http://jts.local/wake/ and picking a row,
# but scriptable from the laptop and inspectable in shell history.
#
# Usage:
#   bash scripts/switch-wake-word.sh                    # show current + options
#   bash scripts/switch-wake-word.sh jarvis_v2          # community "Jarvis" (default)
#   bash scripts/switch-wake-word.sh hey_jarvis         # stock "Hey Jarvis"
#   bash scripts/switch-wake-word.sh alexa              # stock "Alexa"
#   bash scripts/switch-wake-word.sh hey_mycroft        # stock "Hey Mycroft"
#
# To add a new model: add a `WakeModelEntry(...)` to REGISTRY in
# jasper/wake_models.py and re-deploy. install.sh fetches non-bundled
# `download_url`s into /var/lib/jasper/wake/ on the next install.
#
# Defaults: PI_HOST/PI_USER come from .env.local when present, then
# PI_HOST falls back to JASPER_HOSTNAME and jts.local.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
. "${SCRIPT_DIR}/_lib.sh"

SSH="ssh -o ConnectTimeout=5 ${PI_USER}@${PI_HOST}"

KEY="${1:-}"

if [[ -z "$KEY" ]]; then
    echo "Current wake model on ${PI_HOST}:"
    $SSH "sudo grep -h '^JASPER_WAKE_MODEL=' /var/lib/jasper/wake_model.env /etc/jasper/jasper.env 2>/dev/null | head -1 || echo '(unset — daemon falls back to hey_jarvis)'"
    echo
    echo "Available models (from jasper/wake_models.py):"
    $SSH "sudo /opt/jasper/.venv/bin/python -c '
from jasper.wake_models import REGISTRY
for e in REGISTRY:
    marker = \" (recommended)\" if e.recommended else \"\"
    print(f\"  {e.key:14s} → {e.label}{marker}\")
'"
    echo
    echo "Usage:  bash scripts/switch-wake-word.sh <key>"
    exit 0
fi

# Resolve the key on the Pi (so the registry is the source of truth).
# Prints `<model_string>|<bundled-bool>` for the chosen entry, or
# nothing if the key isn't in the registry.
RESOLVED=$($SSH "sudo /opt/jasper/.venv/bin/python -c '
import sys
from jasper.wake_models import by_key
e = by_key(\"${KEY}\")
if e is None:
    sys.exit(0)
print(f\"{e.model}|{1 if e.bundled else 0}\")
'")

if [[ -z "$RESOLVED" ]]; then
    echo "error: '${KEY}' is not in the wake-model registry." >&2
    echo "       Run with no arguments to see available keys." >&2
    exit 2
fi

MODEL="${RESOLVED%|*}"
BUNDLED="${RESOLVED#*|}"

# For non-bundled models, refuse to flip if the .onnx file isn't on
# the Pi yet — the daemon would crash on startup. install.sh fetches
# missing files on the next deploy.
if [[ "$BUNDLED" == "0" ]]; then
    if ! $SSH "sudo test -s '${MODEL}'"; then
        echo "error: ${MODEL} is missing on ${PI_HOST}." >&2
        echo "       Run 'bash scripts/deploy-to-pi.sh' to fetch it, then retry." >&2
        exit 3
    fi
fi

echo "Switching ${PI_HOST}:JASPER_WAKE_MODEL → ${MODEL}"
$SSH "sudo install -d -m 0750 /var/lib/jasper && \
      printf 'JASPER_WAKE_MODEL=%s\n' '${MODEL}' | sudo tee /var/lib/jasper/wake_model.env >/dev/null && \
      sudo chmod 0644 /var/lib/jasper/wake_model.env && \
      sudo systemctl restart jasper-voice && \
      sleep 2 && \
      systemctl is-active jasper-voice && \
      sudo journalctl -u jasper-voice -n 5 --no-pager 2>&1 | grep -v -E 'GetGpuDevices|device_discovery' | tail -5"
