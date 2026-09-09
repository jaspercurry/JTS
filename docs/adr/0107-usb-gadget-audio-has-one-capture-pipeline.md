# ADR-0107: USB gadget audio has one capture pipeline, and no hidden fallback

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

USB Audio Input shipped with two capture paths. In the "solo/aloop" path a
resident bridge (`jasper-usbsink-audio`, Rust, itself a rewrite of a
Python/PortAudio daemon) opened `hw:UAC2Gadget`, wrote the samples into an
snd-aloop lane, published `/run/jasper-usbsink/state.json`, served an `:8781`
HTTP listener for preempt/impulse-tap/status, and drove the gadget's
`Capture Pitch 1000000` control itself. In the "combo" path `jasper-fanin`
DIRECT-captures the gadget and the bridge does nothing.

Two capture owners meant two of everything: two host-clock actuators contending
for one pitch ctl, two impulse taps, two liveness surfaces that could disagree
about whether a host was streaming, and an extra bridge hop plus an snd-aloop
cable measured at roughly 25 ms on the USB path. The failure modes were
arbitration bugs and disagreeing telemetry, not lost audio.

## Decision

**`jasper-fanin`'s DIRECT capture of `hw:UAC2Gadget` is the only USB audio data
plane.** The bridge is deleted — process, `state.json`, `:8781` listener,
`host_clock.rs`, `usbsink_substream` write alias, and the two doctor checks that
watched them. `jasper-usbsink.service` survives as a hardened `Type=oneshot`,
`RemainAfterExit=yes` readiness marker with no resident process: reaching
active (exited) proves the role permits local USB audio, the gadget composed
`uac2.usb0`, and the kernel registered the ALSA card.

**There is deliberately no aloop capture fallback.** When USB Audio Input is
off, or the arming pass has not run, fan-in's `usbsink` lane opens
`hw:Loopback,1,3` — which nobody writes — so the source is silently *idle*, and
a sustained DIRECT-capture failure makes USB *unavailable* rather than quietly
degraded. Fan-in owns bounded reopen and self-heal of its own handle; reopen
counters and `direct.health` are telemetry, never authorization to recompose
USB functions.

Observed state has exactly two owners: fan-in `STATUS` (the identity-bound
`label="usbsink"` DIRECT entry) owns `playing` / `rms_dbfs` / `muted` and the
direct/resampler counters; `/sys/class/udc/*/state` owns `host_connected`.

## Consequences

- One actuator owner per gadget resource. The pitch-ctl neutralize belt lives
  on `jasper-fanin.service` alone and gates on fan-in actually owning the ctl;
  a stop/start of the readiness marker cannot stomp a live pitch command.
- Mux's preempt is fan-in's `MUTE`/`UNMUTE usbsink` over the existing control
  socket — load-bearing rather than defense-in-depth, since the `:8781`
  `/preempt` POST it used to layer over is gone.
- Given up: the ability to keep playing USB audio when fan-in cannot open the
  gadget. That is the intended direction — a silent, *reported* source beats a
  second capture owner kept alive for a case that self-heals.
- The old path is not a rollback target. Reviving it means reviving two
  actuator owners; rebuild against the then-current topology instead. The
  removed code is in git history.

## Addendum, 2026-09-08: the volume model, and why nothing writes back

Added to the record above, which it does not change.

USB behaves like AirPlay. CamillaDSP's `main_volume` is the user-perceived
speaker volume, and the host's slider is an upstream *observation*, not the
master: `Source.USBSINK` is declared `VolumeMode.CAMILLA_MASTER` alongside
`AIRPLAY` and `IDLE`. `jasper/usbsink/volume_bridge.py` translates the gadget
mixer's step index into a percent and calls
`VolumeCoordinator.observe_source_volume(...)`; the translation and its ALSA
unit constraints live in that module and nowhere else. Outbound — remote twist,
voice "louder" — goes through the ordinary `_set_camilla` path.

**There is deliberately no write back to the gadget mixer**, and no
`link_volume_control` binding of the host's slider to CamillaDSP volume, for
two reasons:

1. `main_volume` is also where ducking happens. Wiring the host's slider
   straight to it would make every voice turn visibly drag the Mac's slider
   down and back up.
2. The remote/voice/"louder" path must stay authoritative. Bidirectional sync
   needs either echo prevention on both ends or an always-wins rule on one —
   complexity or confusion, pick one.

The accepted UX consequence: the host slider is a remote control. Touching it
overrides whatever JTS was set to, exactly as an AirPlay sender's slider does.
