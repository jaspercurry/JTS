# ADR-0127: Wake arbitration is hubless, and costs a solo speaker nothing

- **Date:** 2026-08-26
- **Status:** Accepted (recorded when HANDOFF-peering.md was trimmed to its
  operational spine)

## Context

When a household runs several JTS speakers on one LAN, all of them hear the
same "Hey Jarvis" and, uncoordinated, all of them answer. The owner's
constraints on fixing that were explicit: LAN-only with no cloud arbitration
and no third-party service; light enough to run several around a house; **a
single-speaker household must pay zero CPU, RAM and network**; a deliberate
binary toggle rather than autodetection; losers make no sound; the "primary
speaker" is a bias, not a hard rule; and the winner's reply plays through the
winner's own speakers.

## Decision

**Peer-to-peer arbitration with a deterministic pure ranking function, no
hub, and off by default.** Every peer broadcasts one wake report, applies the
same pure `rank()` to the same collected message set, and reaches the same
conclusion independently. There is no leader to elect and no single point of
failure.

Two transports, deliberately separated: **mDNS-SD** (`_jasper-peer._udp`,
via the Avahi daemon JTS already runs) answers "is anyone else here?", and
**multicast UDP** on an RFC 2365 admin-local group carries the arbitration
messages themselves.

The design follows the broad deterministic-selection pattern commercial
multi-speaker systems use, while staying LAN-local and hubless.

## Consequences

- **N=1 is free.** A hub-and-spoke design with an arbitration server needs
  that server even when there is one speaker; P2P does not. With the toggle
  off, the peering thread never starts, `zeroconf` is never imported, no
  socket is bound, no Avahi service file is installed, and the voice daemon's
  arbitration call returns "WIN" without doing I/O — synchronously, without
  even yielding to the event loop, which is what keeps solo chirp timing
  identical to a build with no peering at all.
- **Determinism is the whole safety property.** Anything that makes `rank()`
  impure, order-dependent, or peer-specific breaks arbitration silently: the
  peers stop agreeing, and either several answer or none does. Ranking
  therefore lives in one pure module with no I/O, tested by driving synthetic
  report sets.
- "Exactly one winner" is enforced for the case the design targets — one
  speaker physically hears the wake, multicasts it, and the others adopt that
  foreign epoch and concede. The **concurrent multi-waker race**, where two
  speakers wake on the same utterance and each mints its own epoch, converges
  best-effort and is not proven: the epoch dedup and the winner-concede path
  usually collapse duplicates before either goes ACTIVE, but nothing bounds
  that to the arbitration window, and once a peer *is* ACTIVE, session
  stickiness makes it deliberately ignore a foreign claim. A real fix needs a
  tiebreak that binds *before* ACTIVE.
- **The reply plays on the winner.** Routing it through a designated primary
  speaker would mean streaming PCM between Pis — a much larger architectural
  change, out of scope.
- **Multicast is an assumption, not a guarantee.** Some consumer mesh routers
  drop or rate-limit multicast. There is no unicast fallback wired up; the
  transport has hooks but no per-peer multicast-health detector. The symptom
  to watch for is peers visible in the directory (mDNS works) while no
  arbitration message is ever exchanged.
- Mode changes restart both daemons rather than re-reading state live. A
  SIGHUP live-toggle would need both daemons to re-read the env file on
  signal plus dynamic socket and advert teardown — worth the plumbing only if
  the few seconds of restart actually bother someone.
