#!/bin/sh
# Configure /etc/bluetooth/main.conf for Jasper speaker mode.
#
# Idempotent: each sed line replaces the existing key (whether
# commented out with `#` or not) with the desired value. Safe to re-run.
#
# Run as part of install.sh; not expected to be invoked manually.

set -eu

CONF=/etc/bluetooth/main.conf
SPEAKER_NAME_FILE=${JASPER_SPEAKER_NAME_FILE:-/var/lib/jasper/speaker_name.env}

if [ -r "$SPEAKER_NAME_FILE" ]; then
    # shellcheck disable=SC1090
    . "$SPEAKER_NAME_FILE" 2>/dev/null || true
fi
speaker_name=${JASPER_SPEAKER_NAME:-JTS}
speaker_name_sed=$(printf '%s' "$speaker_name" | sed -e 's/[\/&]/\\&/g')

if [ ! -f "$CONF" ]; then
    echo "ERROR: $CONF not found — is bluez installed?" >&2
    exit 1
fi

# One-time backup (preserves whatever Pi OS shipped before our edits)
if [ ! -f "${CONF}.bak.orig" ]; then
    sudo cp "$CONF" "${CONF}.bak.orig"
fi

# Name visible to phones in their BT picker.
sudo sed -i "s/^#\?Name = .*/Name = ${speaker_name_sed}/" "$CONF"

# Class of Device: 0x200414 = audio service + audio/video major +
# loudspeaker minor. Tells phones we're a speaker so they enable
# A2DP-sink-friendly UI (e.g. iOS shows the speaker icon).
sudo sed -i 's/^#\?Class = .*/Class = 0x200414/' "$CONF"

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
sudo sed -i 's/^#\?DiscoverableTimeout = .*/DiscoverableTimeout = 300/' "$CONF"
sudo sed -i 's/^#\?PairableTimeout = .*/PairableTimeout = 300/' "$CONF"

echo "$CONF updated. Restart bluetooth with: sudo systemctl restart bluetooth"
