# ADR-0102: The bridge→voice microphone transport is UDP localhost, not a second snd-aloop card

- **Date:** 2026-08-25
- **Status:** Accepted

## Context

The AEC bridge originally handed its processed microphone frames to
`jasper-voice` over a second snd-aloop card (`LoopbackAEC`). On 2026-05-11
the bridge's PortAudio input callback stopped firing after a USB underrun on
the XVF capture endpoint while its main thread was blocked in a PortAudio
write to that card. A blocked C call holding the GIL cannot observe Python's
`SIGTERM` handler, so systemd waited out `TimeoutStopSec` and sent `SIGKILL`
— which killed the process while it held the loopback fd open. snd-aloop's
kernel-side `loopback_cable` struct is module-global and assumes a
cooperative close: it was left half-bound, its `hw_ptr` timer never re-armed,
and every fresh bridge blocked on its second write. Only `rmmod snd_aloop &&
modprobe snd_aloop` (after stopping all six consumers) or a reboot cleared
it. The wake path was silently dead for ~10 minutes; no cue fired, because
cues are gated on a wake event and wake events need the mic that was gone.

Three fragilities composed: PortAudio's `InputStream` is one-shot once the
PCM reaches `SND_PCM_STATE_DISCONNECTED` (unrecoverable per the ALSA
contract); blocking I/O in a Python daemon defeats `SIGTERM`; and `SIGKILL`
of an snd-aloop consumer corrupts kernel state that survives process
restarts.

## Decision

**The bridge sends AEC'd mono int16 frames to `jasper-voice` over
non-blocking UDP on localhost** (`127.0.0.1:JASPER_AEC_UDP_PORT`, default
9876). `UdpMicCapture` (`jasper/audio_io.py`) binds the port via
`asyncio.DatagramProtocol` and yields the same 1280-sample frames
`MicCapture` does, so `WakeLoop` is transport-agnostic. The second snd-aloop
card is deleted, not hardened.

The failure class is eliminated **structurally** rather than defended
against: there is no kernel-side state for a crash to corrupt, either side
can die without affecting the other, and `sendto()` never blocks at this
rate (~256 kbps on `lo`), so the bridge's main thread can always observe
`SIGTERM` inside a 5 s `TimeoutStopSec`.

The music-side snd-aloop card stays. CamillaDSP is a well-behaved C++ daemon
that handles `SIGTERM` and never gets `SIGKILL`'d, so its loopback never
wedges — the retirement is scoped to the transport that actually broke.

## Consequences

- Kernel-state recovery (`rmmod` + `modprobe`) never had to be built. It sits
  on the deferred list with a concrete trigger: if the music-chain loopback
  ever wedges the same way.
- AEC stays out of `jasper-voice`'s address space. In-process AEC3 would be
  simpler but a crash in the canceller would take wake detection down with
  it; the process boundary is bought with a UDP hop.
- Deliberately given up: ALSA's timing semantics on that leg. Localhost UDP
  loss is effectively zero at this rate, but it is best-effort by contract,
  and a receiver that stops draining drops frames rather than back-pressuring
  the sender. That is the intended direction — the sender must never block.
- Rejected: hardening snd-aloop (the cable struct's cooperative-close
  assumption is upstream and structural), and a PipeWire migration (replaces
  the whole userspace audio stack to fix one leg).
