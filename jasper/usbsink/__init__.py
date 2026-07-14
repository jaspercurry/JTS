# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""USB gadget audio source helpers for JTS.

Surfaces a connected host computer as a fourth music source alongside
AirPlay, Spotify Connect, and Bluetooth A2DP. The host sees the
configured speaker name as a USB audio output device (UAC2 gadget);
jasper-fanin DIRECT-captures the gadget and sums those frames into the same
music chain as the other renderers.

Production audio data plane:
  rust/jasper-fanin
                    ALSA UAC2Gadget DIRECT capture, bounded periods, live
                    activity/level/mute/resampler telemetry over STATUS.
  jasper-usbsink.service
                    Process-free oneshot readiness/lifecycle marker. Its
                    bounded card gate is not data-plane liveness.
  jasper.usbgadget  Import-light kernel UDC connection-state reader used by
                    management surfaces instead of a copied daemon state file.

Package layout:
  volume_bridge.py  amixer-cget mixer poller → jasper-control. Runs in
                    jasper-usbsink-volume while USB Audio Input is enabled.

See docs/HANDOFF-usbsink.md for the full design.
"""
