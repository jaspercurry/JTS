#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Configure /etc/bluetooth/main.conf for Jasper speaker mode.
#
# Idempotent: each sed line replaces the existing key (whether
# commented out with `#` or not) with the desired value. Safe to re-run.
#
# Run as part of install.sh, which is root-only (require_root); not expected
# to be invoked manually.

set -eu

# shellcheck source=deploy/lib/jasper-sed-inplace.sh
. "$(dirname "$0")/lib/jasper-sed-inplace.sh"
# shellcheck source=deploy/lib/jasper-env-file.sh
. "$(dirname "$0")/lib/jasper-env-file.sh"

CONF=/etc/bluetooth/main.conf
SPEAKER_NAME_FILE=${JASPER_SPEAKER_NAME_FILE:-/var/lib/jasper/speaker_name.env}

# The wizard-owned name is operator text: a `$(…)`, backtick or space in it
# must reach sed as data, so the file is read, never sourced.
speaker_name=${JASPER_SPEAKER_NAME:-}
if speaker_name_from_file="$(jasper_env_file_get "$SPEAKER_NAME_FILE" JASPER_SPEAKER_NAME)"; then
    speaker_name="$speaker_name_from_file"
fi
speaker_name=${speaker_name:-JTS}
speaker_name_sed=$(printf '%s' "$speaker_name" | sed -e 's/[\/&]/\\&/g')

if [ ! -f "$CONF" ]; then
    echo "ERROR: $CONF not found — is bluez installed?" >&2
    exit 1
fi

# One-time backup (preserves whatever Pi OS shipped before our edits)
if [ ! -f "${CONF}.bak.orig" ]; then
    cp "$CONF" "${CONF}.bak.orig"
fi

# Name visible to phones in their BT picker.
sed_inplace "$CONF" "s/^#\?Name = .*/Name = ${speaker_name_sed}/"

# Class of Device: 0x200414 = audio service + audio/video major +
# loudspeaker minor. Tells phones we're a speaker so they enable
# A2DP-sink-friendly UI (e.g. iOS shows the speaker icon).
sed_inplace "$CONF" 's/^#\?Class = .*/Class = 0x200414/'

# Discoverable and Pairable themselves are runtime adapter properties, not
# main.conf keys; jasper-bluetooth-agent closes them through BlueZ on startup
# and when Pairable is observed outside an open pairing window. The main.conf
# safety net is only the timeout default for tools that open a window.
# These timeouts are the *default* auto-off when something flips
# Discoverable or Pairable on. Our web UI sets both per-toggle (5 min
# when user clicks the switch); the values here matter only if some
# other tool — bluetoothctl, a foreign agent — flips one without also
# setting a timeout. 300 s is the safety net for that case; 0 means
# "stay on forever," which is exactly the broadcast/pair-to-the-world
# failure mode we don't want.
sed_inplace "$CONF" 's/^#\?DiscoverableTimeout = .*/DiscoverableTimeout = 300/'
sed_inplace "$CONF" 's/^#\?PairableTimeout = .*/PairableTimeout = 300/'

echo "$CONF updated. Restart bluetooth with: sudo systemctl restart bluetooth"
