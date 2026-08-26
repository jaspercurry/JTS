# ADR-0114: A JTS-native output owner, not PipeWire — and not a fan-in content mirror

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

JTS needed one answer to "what signal is *what the speaker actually emitted*,
and who publishes it?" Two candidates were live in 2026-05. A short-lived spike
mirrored fan-in's content over a Unix datagram to AEC/corpus consumers; it
solved shared-`dsnoop` pressure for music but was pre-CamillaDSP, excluded
TTS/cues/chirps, did not know what actually drained to the DAC, and could not
drive realtime truncation. The other candidate was adopting PipeWire, whose
daemon, session manager, compatibility layers, dynamic graph, desktop hotplug
policy, and plugin surface are far larger than an appliance needs.

## Decision

**Build one small JTS-native final output owner — `jasper-outputd` — and give
it the whole speaker boundary:** read the post-CamillaDSP content stream, mix
assistant audio where the topology requires it, apply final clamps, write the
physical DAC, publish exactly one `speaker_output_reference`, and report the
playout ledger. Rust owns the audio clock and reports what happened; Python
decides what should happen (provider sessions, wake/session state, tools, cue
selection, volume policy).

**Do not ship PipeWire.** Borrow its lessons instead, and only these: explicit
node/port/link vocabulary even when implemented with ALSA plus Unix sockets; one
timing driver per graph (the DAC write loop), with optional consumers never
becoming timing owners; async side consumers that receive failure-isolated
copies and drop-and-count rather than blocking playback; explicit ring semantics
(bounded storage, monotonic sequence, underrun/overrun counters, stated drop
policy); explicit rate matching at every clock boundary rather than one huge
buffer; and small backend interfaces so engines and transports stay swappable.

**Do not preserve the fan-in content mirror as a compatibility path.** It is an
investigation artifact, not an architecture.

## Consequences

- One place where audible output is mixed, measured, protected, referenced, and
  accounted for. The AEC bridge consumes the final electrical reference over
  localhost UDP, and the chip-AEC USB-IN producer is fed from the same fanout —
  so barge-in during assistant speech is structurally possible.
- Reference fanout is failure-isolated from the DAC path by construction: an
  absent optional reference writer drops and counts periods, and never fails
  playback readiness.
- Explicitly not taken from PipeWire: WirePlumber/session-manager policy,
  PulseAudio/JACK compatibility, arbitrary user-routable graphs, module loading
  as a runtime extension mechanism, PipeWire as another always-on service, and
  any hybrid "mostly ALSA plus a little PipeWire" topology.
- Non-goals that still hold: no general-purpose audio server, no dynamic plugin
  routing, AEC never mandatory for playback, and corpus/debug capture never part
  of the realtime audio clock.
- Deliberately out of reach: software can expose the final *electrical* samples,
  never the acoustic result after DAC, amp, driver, cabinet, and room. That
  needs a microphone.
