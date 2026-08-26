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
  removed code and its rationale are in
  [historical/usbsink-implementation-appendix.md](../historical/usbsink-implementation-appendix.md).
