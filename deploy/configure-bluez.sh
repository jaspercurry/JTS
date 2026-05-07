#!/bin/sh
# Configure /etc/bluetooth/main.conf for JTS speaker mode.
#
# Idempotent: each sed line replaces the existing key (whether
# commented out with `#` or not) with the JTS value. Safe to re-run.
#
# Run as part of install.sh; not expected to be invoked manually.

set -eu

CONF=/etc/bluetooth/main.conf

if [ ! -f "$CONF" ]; then
    echo "ERROR: $CONF not found — is bluez installed?" >&2
    exit 1
fi

# One-time backup (preserves whatever Pi OS shipped before our edits)
if [ ! -f "${CONF}.bak.orig" ]; then
    sudo cp "$CONF" "${CONF}.bak.orig"
fi

# Name visible to phones in their BT picker.
sudo sed -i 's/^#\?Name = .*/Name = JTS/' "$CONF"

# Class of Device: 0x200414 = audio service + audio/video major +
# loudspeaker minor. Tells phones we're a speaker so they enable
# A2DP-sink-friendly UI (e.g. iOS shows the speaker icon).
sudo sed -i 's/^#\?Class = .*/Class = 0x200414/' "$CONF"

# Stay discoverable + pairable indefinitely (we want the speaker to
# always be findable for re-pairing if the phone forgets it). For a
# typical home network this is fine; on hostile networks set these
# to non-zero (timeout in seconds).
sudo sed -i 's/^#\?DiscoverableTimeout = .*/DiscoverableTimeout = 0/' "$CONF"
sudo sed -i 's/^#\?PairableTimeout = .*/PairableTimeout = 0/' "$CONF"

echo "$CONF updated. Restart bluetooth with: sudo systemctl restart bluetooth"
