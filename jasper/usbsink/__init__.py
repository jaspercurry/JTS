# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""USB gadget audio source helpers for JTS.

Surfaces a connected host computer as a fourth music source alongside
AirPlay, Spotify Connect, and Bluetooth A2DP. The host sees the
configured speaker name as a USB audio output device (UAC2 gadget);
JTS bridges the captured
frames into `usbsink_substream`, the USB-in private fan-in lane, so
they join the same summed music chain as the other renderers.

Production audio data plane:
  rust/jasper-usbsink-audio
                    ALSA UAC2Gadget capture -> usbsink_substream (aloop
                    bridge, non-combo state) OR fan-in direct capture of
                    the gadget (combo), bounded periods, Rust
                    state/preempt publisher built into the daemon.

Package layout:
  volume_bridge.py  amixer-cget mixer poller → jasper-control. Runs in
                    both the solo and combo states as jasper-usbsink-volume.

See docs/HANDOFF-usbsink.md for the full design.
"""
