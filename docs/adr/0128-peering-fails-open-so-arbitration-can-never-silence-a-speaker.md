# ADR-0128: Peering fails open, so arbitration can never silence a speaker

- **Date:** 2026-08-26
- **Status:** Accepted (recorded when HANDOFF-peering.md was trimmed to its
  operational spine)

## Context

With peering on, the wake handler asks a *second daemon* — over a Unix
socket — whether this speaker may answer. That inserts a dependency directly
in front of the only thing the product must always do: respond to its wake
word. The peering daemon can be absent, wedged, mid-restart, or answering
garbage.

The natural instinct for arbitration is to fail closed: if you cannot prove
you won, stay quiet, because two speakers answering at once is the visible
bug peering exists to fix.

## Decision

**Every failure of the arbitration path resolves to "WIN".** Missing socket,
connect refused, timeout, malformed response, unexpected exception — each
returns WIN, and the speaker answers as if it were alone. Peering can only
ever take a speaker *out* of a response when it affirmatively hears a better
peer.

The arbitration call also runs off the main mic loop, as a background task
with a hard ceiling on the round-trip: while it waits, the main loop keeps
iterating, frames buffer, and the systemd watchdog keeps being patted. A slow
peering daemon delays a reply; it never stalls the loop that would otherwise
stop feeding the watchdog.

## Consequences

- **This is the no-silent-deafness rule applied to peering** (AGENTS.md
  non-negotiable 6). A broken coordination layer degrades the household to
  the pre-peering behavior — every speaker answers — which is noisy but not
  deaf. Fail-closed would have turned one wedged daemon into a speaker that
  looks broken and says nothing, with no cue to explain it.
- The failure mode is therefore **duplicate answers, never silence.** Anyone
  adding a new early-return to the arbitration path must keep WIN as its
  default; a `return "LOSE"` on an error path is a bug of the highest
  severity in this subsystem, and it will not show up in single-speaker
  testing.
- Losing is the *only* silent outcome, and it is deliberate: a loser plays no
  chirp at all. This moved the chirp from "fires immediately on wake" to
  "fires only on WIN".
- Because arbitration takes real time, the pre-wake gates (user mute, an open
  room-correction window) are re-checked *after* it as well as before. A gate
  that closed during arbitration must still cancel the response.
- Session stickiness inherits the same posture in the other direction: once a
  peer is in a live session it ignores foreign claims, so an unrelated wake
  elsewhere in the house cannot tear down a conversation in progress. The
  escape hatch is a local wake above a break threshold — speak directly to
  the speaker you want and it contests.
