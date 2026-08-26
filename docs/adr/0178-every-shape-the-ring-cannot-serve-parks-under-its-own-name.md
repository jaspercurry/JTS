# ADR-0178: Every shape the ring cannot serve parks under its own name

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

[ADR-0100](0100-one-audio-transport.md) makes `shm_ring` the only central
transport and says a topology the ring cannot serve "parks loudly — doctor
FAIL, `/state`, web banner naming the reason and the tracked issue". It names
one such shape, the dual-DAC composite, and files one issue for it (#2982).
That was the right scope for a transport decision, but it left three more
shapes carrying no name: an explicit mono full-range layout (the ring
layout's accept-set starts at two channels, so no ring geometry exists for
it), a roleful box whose ACTIVE endpoint marker has not converged onto the
ring it is otherwise eligible for, and a bonded grouping member whose
round-trip `dac_content` lane pins the direct content bridge — which outputd
already refuses against `shm_ring`, making the two mutually exclusive by
construction.

Today each of the four falls back to the snd-aloop loopback route and plays.
The transport deletion removes that route, so on the day it lands all four
stop being audible. Owner ruling, 2026-08-26: **"it is very important that we
only have one audio path"** — sophisticated shapes stay parked until they are
rebuilt on the ring, and every one of them parks loudly under a name and a
tracked issue rather than degrading, guessing, or going quiet. The owner also
ruled on the third shape specifically: a recorded one-command remedy, not a
new reconciler rung.

The refusal that already existed for the composite pointed operators at
`jasper-fanin-coupling-reconcile shm_ring` — a command that cannot help that
shape (the ring has no geometry for it) and that the transport deletion
removes outright.

## Decision

**Four named park classes, closed list, one classifier.** A fifth needs its
own ADR.

| class | fires when | names |
| --- | --- | --- |
| `passive_stereo_composite` | multi-child sink, not roleful | #2982 |
| `mono_full_range` | declared 1-channel full-range layout | #3117 |
| `roleful_active_endpoint_unconverged` | ACTIVE ring width resolves, endpoint marker has not | `jasper-active-speaker baseline-reemit --endpoint ring` |
| `grouped_dac_content_lane` | the bonded round-trip `dac_content` lane is armed | #3118 |

Every class carries exactly one of: a tracked rebuild issue to wait on, or a
command to run. A class with neither would be a park nobody can act on and
nobody can follow.

`jasper.control.transport_park` is the single owner. It **names** refusals the
eligibility SSOT already returns — `ring_channels_for_topology` /
`active_ring_channels_for_topology` in
`jasper.active_speaker.runtime_contract` — and never re-derives ring
eligibility, so the classifier cannot drift from the transport it describes.
Its three consumers (`jasper-doctor`, `/state.resilience.transport_park`, the
household audio card) read one verdict, so they cannot name different issues
for the same box.

It returns **every** matching class, not a winning one. A bonded mono speaker
waits on #3117 and #3118; a single verdict would force an invented precedence
and hide one tracked issue from the operator who has to clear both.

**A roleful composite is not the composite park.** Its post-crossover program
rides the ACTIVE ring, and jts.local runs exactly that shape today. The
passive-only discriminator is load-bearing, not decorative.

**Loudness is staged on the transport, not on a flag.** While `loopback` is
still a legal coupling the affected box plays, and calling a playing speaker
silent is the confusion ADR-0100 exists to prevent, pointed the wrong way. So
a classified park is `pending` — disclosed to the operator (doctor warn,
`/state`), withheld from the household — until the ring is the only
transport, when it becomes `parked`: doctor FAIL, `/state`, and the household
banner and incident rows. The predicate that flips it is **derived from the
coupling vocabulary itself**, so deleting the loopback coupling flips it with
no second edit and leaves no dead knob behind.

The composite refusal in `jasper-outputd`'s config parse names #2982 and
**recommends no command**. A dead remedy is worse than none.

## Consequences

The fleet inventory the transport deletion needs exists before the deletion:
`jasper-doctor` now answers "what parks when loopback goes" per box, issue by
issue. Nothing currently working changes behaviour — ring-armed boxes classify
clean, loopback boxes keep playing and are merely disclosed.

Harder: four shapes now have a documented promise to keep. #2982, #3117 and
#3118 are commitments, and a household on one of those boxes gets a speaker
that stops rather than one that degrades. That is the trade ADR-0100 already
made, made explicit and countable.

Deliberately out of scope: a roleful box whose ACTIVE ring width does not
resolve at all (duplicate or non-contiguous output assignment). It is refused
by the width guards and by `active_lane_capability_gap`, and giving it a fifth
park name here would double-report one fact. An unconfigured topology is
likewise left to the speaker-setup park (#2135).

Rejected: a per-class boolean flag flipped by the deletion slice (a knob to
forget, and dead the moment it is flipped); one winning park verdict (invents
precedence, hides tracked issues); showing `pending` parks to the household
(tells a working speaker's owner it is broken); and a new reconciler rung for
the unconverged ACTIVE endpoint — the owner chose the recorded command,
because the rung would be machinery built inside the transition ceremony
ADR-0100 deletes.
