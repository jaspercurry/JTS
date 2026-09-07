# ADR-0246: Arbitration is the whole of peering

- **Date:** 2026-09-07
- **Status:** Accepted (narrows the transport split in
  [ADR-0127](0127-wake-arbitration-is-hubless-and-costs-a-solo-speaker-nothing.md);
  the hubless-arbitration ruling itself stands unchanged)

## Context

ADR-0127 gave peering two deliberately separated transports: mDNS-SD to answer
"is anyone else here?", and multicast UDP to carry the arbitration messages.
Only the second was ever wired to anything. The daemon's own mDNS browser and
its periodic HELLO broadcast fed exactly one structure — a `_known_peers` dict
whose only reader was the `STATUS` RPC, which nothing calls. jasper-voice's
`PeeringClient` sends `ARBITRATE`, `SESSION_STARTED` and `SESSION_ENDED` and
nothing else; the `/sound/pair/` page reads `peering.env` off disk;
`jasper-doctor` shells `avahi-browse`. `PING`'s documented consumer, "doctor's
liveness check", does not exist: `jasper/cli/doctor/peering.py` has two checks
and neither opens the socket.

HELLO's other stated purpose — a multicast-health self-test, "did my HELLO come
back to me?" — was never implemented. The first statement of
`PeeringDaemon._on_multicast_message` discards any datagram whose sender peer id
is our own, so a looped-back HELLO returns before any counter, log line,
`/state` field or doctor check can observe it. ADR-0127 already recorded the
gap: "There is no unicast fallback wired up; the transport has hooks but no
per-peer multicast-health detector."

Arbitration never required a peer to be known first. When it is IDLE,
`PeeringStateMachine._on_peer_wake` adopts a previously unseen foreign epoch
straight off the event, so a first-contact peer's WAKE is honored on the first
datagram whether or not that peer was ever browsed or greeted.

## Decision

The peering daemon stops discovering and greeting peers.
`jasper/peering/discovery.py` is deleted whole, along with the HELLO verb
(dataclass, encoder, decoder branch, broadcast loop and interval),
`_known_peers` and its staleness pruning, and the `STATUS` and `PING` UDS
commands. What remains is arbitration:
`ARBITRATE` / `SESSION_STARTED` / `SESSION_ENDED` over the Unix socket, and
`WAKE` / `CLAIM` / `HEARTBEAT` / `END` over multicast.

Advertisement stays. The `_jasper-peer._udp` Avahi service file is still
rendered when peering turns on and removed by `stop()` when it turns off, and
`jasper-doctor`'s discovery check still browses it with `avahi-browse`. We stop
browsing, not advertising. (`stop()` only reaches the uninstall when `start()`
completed, so a speaker whose multicast bind failed keeps advertising until the
next successful start — pre-existing, tracked separately.)

`PROTO_VERSION` stays at 1: dropping a verb is not a wire-version change. A
HELLO from an older peer now falls through the decoder's existing unknown-type
drop, which is the correct outcome.

## Consequences

- ADR-0127's two transports narrow to one. Multicast UDP carries arbitration;
  mDNS-SD is now purely an advertisement that an operator reads through
  `jasper-doctor`. Nothing in the daemon browses the LAN.
- Peering imports no `zeroconf`. The dependency stays in the `full` and
  `streambox` extras for its remaining importers, `jasper/mdns.py` and
  `jasper/speaker_name_discovery.py`, but the peering package no longer
  contributes to its cost in either mode.
- The daemon can no longer render a peer list, because it no longer keeps one.
  A surface that wants the household's membership browses `_jasper-peer._udp`
  itself, as the doctor check does.
- One weak signal does go with HELLO, and it is not the self-test. Broadcasting
  every 30 s meant a broken multicast *egress* path — no route to the group
  after a NIC or route change, an AP dropping multicast — surfaced as a
  recurring `peering: send failed` warning in the journal between wakes.
  Without it the first symptom is a dropped WAKE inside the arbitration window,
  where there is no retransmit and every speaker then answers at once. We accept
  that: AGENTS.md's guard rule is that permanent machinery needs a
  non-negotiable tie or a recurrence, and there is no recorded incident here —
  a 30 s broadcast on every peering household to catch a hypothetical is the
  machinery that rule tells us to demote. If it ever recurs, the fix is
  observability at the point of failure (a counter or `/state` field on send
  errors), not a keepalive whose receive side was discarded.
- If a per-peer multicast-health detector is ever wanted, it starts from
  ADR-0127's symptom description ("peers visible in the directory while no
  arbitration message is ever exchanged") and a design that actually observes
  something, not from a HELLO loop whose output was discarded.
- `IP_MULTICAST_LOOP = 1` and the daemon's self-loopback filter now have no
  consumer beyond each other: nothing reads a looped-back datagram. They stay
  because setting `LOOP = 0` changes live wire behaviour and this change is a
  deletion, but the pair is a candidate for removal once someone can test it on
  hardware. The filter is pinned by a behaviour test so it cannot rot silently.
