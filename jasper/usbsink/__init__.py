"""USB gadget audio source for JTS.

Surfaces a connected host computer as a fourth music source alongside
AirPlay, Spotify Connect, and Bluetooth A2DP. The host sees JTS as a
USB audio output device (UAC2 gadget); JTS bridges the captured
frames into hw:Loopback,0,0 so they join the existing CamillaDSP
chain at the same sum point as the other renderers.

Package layout:
  audio_bridge.py   sounddevice loop: UAC2Gadget capture → Loopback
                    playback. Tracks last-block RMS for state pub.
  daemon.py         Orchestration: wires audio bridge + state
                    publisher + preempt listener + volume bridge.
  state_publisher.py (Phase 3) /run/jasper-usbsink/state.json writer.
  preempt_listener.py (Phase 3) localhost HTTP receiver for mux preempt.
  volume_bridge.py  (Phase 4) pyalsa mixer observer → jasper-control.

See docs/HANDOFF-usbsink.md for the full design.
"""
