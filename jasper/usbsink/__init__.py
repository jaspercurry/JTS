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
                    ALSA UAC2Gadget capture -> usbsink_substream,
                    bounded periods, Rust state/preempt publisher.

Legacy lab-only Python bridge:
Package layout:
  audio_bridge.py   sounddevice loop: UAC2Gadget capture → Loopback
                    playback. Not used by the production systemd unit.
  daemon.py         Orchestration for the lab bridge.
  state_publisher.py /run/jasper-usbsink/state.json writer.
  preempt_listener.py localhost HTTP receiver for mux preempt.
  volume_bridge.py  (Phase 4) amixer-cget mixer poller → jasper-control.

See docs/HANDOFF-usbsink.md for the full design.
"""
