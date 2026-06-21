#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Flip the Gemini Live model the daemon talks to. Use when 3.1 Live
# Preview is silently failing on Google's side and you need a working
# backend, or to switch back once 3.1 unsticks.
#
# Both supported models are Live API (bidi WebSocket + audio I/O), use
# the same SDK code path (client.aio.live.connect), and accept the same
# SpeechConfig voice list. Only difference for our purposes is the model
# string. Per Google docs:
# https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview
# 3.1 is the published "successor" of 2.5-native-audio.
#
# Usage:
#   bash scripts/switch-gemini-model.sh 3.1     # gemini-3.1-flash-live-preview
#   bash scripts/switch-gemini-model.sh 2.5     # gemini-2.5-flash-native-audio-preview-12-2025
#   bash scripts/switch-gemini-model.sh         # show current
#
# Defaults: PI_HOST falls back to JASPER_HOSTNAME, then to jts.local.
# PI_USER=pi. Override either via env.

set -euo pipefail

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"
SSH="ssh -o ConnectTimeout=5 ${PI_USER}@${PI_HOST}"

ALIAS="${1:-}"

case "$ALIAS" in
    3.1|3)   MODEL="gemini-3.1-flash-live-preview" ;;
    2.5|2)   MODEL="gemini-2.5-flash-native-audio-preview-12-2025" ;;
    "")
        echo "Current model on ${PI_HOST}:"
        $SSH "sudo grep '^JASPER_GEMINI_MODEL=' /etc/jasper/jasper.env"
        echo
        echo "Usage:  bash scripts/switch-gemini-model.sh [3.1|2.5]"
        exit 0
        ;;
    *)
        echo "error: unknown model alias '$ALIAS'. Use '3.1' or '2.5'." >&2
        exit 2
        ;;
esac

echo "Switching ${PI_HOST}:JASPER_GEMINI_MODEL → ${MODEL}"
$SSH "sudo sed -i 's|^JASPER_GEMINI_MODEL=.*|JASPER_GEMINI_MODEL=${MODEL}|' /etc/jasper/jasper.env && \
      sudo grep '^JASPER_GEMINI_MODEL=' /etc/jasper/jasper.env && \
      sudo systemctl restart jasper-voice && \
      sleep 2 && \
      systemctl is-active jasper-voice && \
      sudo journalctl -u jasper-voice -n 2 --no-pager 2>&1 | grep -v -E 'GetGpuDevices|device_discovery' | tail -2"
