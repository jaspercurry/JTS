# ADR-0271: Renderer ingress is snd-aloop lanes plus USB direct capture — nothing else

- **Date:** 2026-09-08
- **Status:** Accepted

## Context

[ADR-0100](0100-one-audio-transport.md) settled the *central* transport: the
SHM slot ring is the only route from fan-in through CamillaDSP to outputd, and
the snd-aloop route between those three daemons plus its transition ceremony
were deleted. It deliberately left the *ingress* side — how a renderer's audio
reaches fan-in — untouched.

What accumulated there was a second, parallel transition ceremony for the same
question one layer up. A per-renderer SHM ring program let an operator arm
individual lanes onto their own `/dev/shm/jts-ring/lane-<label>.ring`, written
by a `jts_ring` ioplug in the renderer and read by fan-in, with a Python-side
policy module choosing which labels were armed, an env file both ends read, and
a gate ladder plus env snapshot/restore in the reconciler to move a live box
between shapes. The fleet default was unarmed, so the shipped path was the
aloop lane and the armed path was a per-box exception. Alongside it,
`JASPER_FANIN_CAMILLA_COUPLING` remained a selector for a choice ADR-0100 had
already made. Meanwhile the USB leg had gone the other way: ADR-0107 made
fan-in's DIRECT capture of `hw:UAC2Gadget` the sole USB data plane, deleting a
bridge process and an aloop cable worth ~25 ms.

## Decision

**Renderer ingress has two shapes and no third:** an snd-aloop lane
(`hw:Loopback,0,N` → `hw:Loopback,1,N`), or fan-in opening a capture device
itself. Only the `usbsink` lane takes the second shape, opening `hw:UAC2Gadget`
under ADR-0107, and only where `JASPER_FANIN_USB_DIRECT` is armed — which is
the reconciler's automatic decision on an eligible box, not an operator toggle
(`jasper/fanin/coupling_auto.py` decides, `coupling_reconcile.py` writes). The
lane keeps its ordinary `input_pcms` entry as the unarmed read; nothing writes
that substream any more, so unarmed USB is *unavailable* rather than degraded.

**The per-renderer ring program is deleted**: `jasper/renderer_lanes.py` and
its `/var/lib/jasper/renderer_lanes.env`, the `jasper-audio-config
renderer-lanes` verb, `rust/jasper-fanin/src/mixer/ring_capture.rs`, the
`correction_ring_lane` / `shairport_ring_lane` PCMs and
`deploy/alsa/conf.d/61-jts-renderer-lanes.conf`, along with the
`EnvironmentFile=` lines the renderer units carried for it. Deleted with it is
the ceremony that moved a live box between ingress shapes — the gate ladder,
the env snapshot/restore and the staging arm. What survives in the reconciler
is the work that is not a ceremony: the geometry heal, the ordered restart
spine, the outputd bridge-key writer and the entry lock.

**The coupling is not declared.** `JASPER_FANIN_CAMILLA_COUPLING` selected
nothing once ADR-0100 left one transport, so its Python vocabulary is deleted
and the reconciler unsets a persisted value. Fan-in's accept-set is
deliberately *not* relaxed in the same step: it still refuses any token but
unset, empty or `shm_ring` as a config-class fault (exit 78, the unit parks),
so a stale `loopback` in someone's env file parks the box rather than being
ignored. Removing that refusal is a later step, once no field box can carry the
key.

**The wire is `S32_LE` on every shipped box.** `JASPER_FANIN_RING_WIRE_FORMAT`
defaults to it and nothing writes the key. The narrow `S16_LE` arm stays
compiled in on both sides for a box an operator deliberately pins.

## Consequences

- One question has one answer at each layer: a lane is aloop or it is direct,
  and which it is follows from the source and one reconciler-owned key rather
  than from per-box lane-arming state. The fan-in `input_pcms` /
  `input_renderers` default arrays and `MUSIC_SOURCE_SPECS` are the whole of the
  lane vocabulary.
- The removed ceremony is the same class ADR-0100 removed centrally, for the
  same stated reason: a transition ladder is machinery for moving between two
  shapes, and there is one shape per lane. Recovery from a bad deploy stays
  `git revert` plus redeploy.
- Given up: arming one renderer onto a ring for a latency experiment without
  touching code. That was a per-box exception the fleet never took, and a
  measured win on one lane is a reason to change that source's ingress shape,
  not to keep a switch.
- Given up for now: nothing about the narrow wire. `S16_LE` remains reachable,
  so the undithered-requantization hazard is still one deliberate pin away —
  closing it is a separate decision on the Rust arm.
- Given up for now, second: a complete sweep of the retired coupling key. The
  reconciler unsets it in the `fanin.env` it owns, but `jasper-fanin.service`
  also loads `/etc/jasper/jasper.env`, which nothing here writes — so a
  hand-set `JASPER_FANIN_CAMILLA_COUPLING` there still reaches the daemon and
  still parks it at exit 78. That is the deliberate tradeoff of keeping the
  refusal: a stale value is loud rather than ignored, and the remedy is manual.
  Before trusting a box, run
  `grep -R JASPER_FANIN_CAMILLA_COUPLING /etc/jasper/ /var/lib/jasper/` and
  remove what it finds.
- Doctor, `/state` and the fan-in `STATUS` block lose the lane-arm fields they
  published. They are the surfaces to re-read after this lands; a reader of a
  removed field must be fail-soft to an absent key.
