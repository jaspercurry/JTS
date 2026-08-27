# ADR-0184: A resolvable width with no armed endpoint signals, it does not park

- **Date:** 2026-08-27
- **Status:** Accepted

## Context

[ADR-0178](0178-every-shape-the-ring-cannot-serve-parks-under-its-own-name.md)
closed the park list at four classes and said a fifth needs its own ADR. This
is that ADR, and it declines to add one.

**The seam.** `active_ring_channels_for_topology`
(`jasper/active_speaker/runtime_contract.py`) gates on
`requires_roleful_graph` and nothing else before resolving a width.
`classify_output_contract` sets that flag for **any** roleful, protected or
subwoofer assignment; the narrower fact — that the box is an active-crossover
layout — is `active_modes`, derived a few lines above it from the speaker
groups whose mode is `active_2_way` / `active_3_way`. The two are not the same
set, and ADR-0178 already said so: it scoped the third park class to
active-crossover layouts precisely because a passive stereo box that adds a
subwoofer or a protected output is roleful with no active-speaker baseline to
re-emit.

So a passive stereo box with a subwoofer resolves a wide-ring width. In
`jasper.control.transport_park` that width has three consequences, all of them
quiet:

- `ring_unresolved` is `False` — a width of one kind resolved — so the
  composite and mono classes are never reached;
- the third class is guarded by `active_modes`, which is empty here, so the
  endpoint park does not fire either;
- with no park, `snapshot()` reports `ok` and `jasper-doctor`'s
  `check_ring_transport_park` prints its greenest line: *this box is in none
  of the four named transport parks*.

Meanwhile `jasper/fanin/converge.py` asks the **same** width function the same
question with no `active_modes` narrowing, so the unattended pass does attempt
this box — and then refuses at `ring_roleful_unattended_ready`, which admits
only a hardware-fingerprint-matched applied baseline or an all-muted staged
anchor. A passive box with a subwoofer has applied no active-speaker baseline,
so the refusal is the normal outcome, not an incident.

The box is therefore structurally unserved by the transport and reads clean on
every surface. No box on the fleet has this shape today.

## Decision

**No fifth park class. An operator-only signal instead.**

ADR-0178's bar for a class is that it carry exactly one of a tracked rebuild
issue or a runnable command, because "a class with neither would be a park
nobody can act on and nobody can follow". This shape carries neither. There is
no rebuild to wait on — the ring can carry the width; what is missing is that
anything has proved the endpoint. And there is no command that clears it: the
recorded remedy's first step is `baseline-reemit`, which needs the
active-speaker baseline this shape does not have, so recording it here would
reproduce the dead-remedy defect ADR-0178 removed. ADR-0178 also refuses a
fifth name to a roleful box whose width does not resolve, on the grounds that
the width guards already report the fact and a second name would double-report
it; the same objection lands here from the other side — a name is not what is
missing. And the repo default is not to build machinery for hypotheticals: no
fleet box is in this shape.

The signal is additive and small:

- `jasper.control.transport_park.snapshot()` gains one boolean,
  `unproven_endpoint`, alongside the existing fields. It is `True` when the
  wide ring resolves a width, `active_modes` is empty, and outputd's
  active-ring endpoint marker is not armed — the exact **complement** of the
  third park class's condition on the same two inputs, so the two can never
  both describe one box and there is nothing to double-report.
- `jasper-doctor`'s `check_ring_transport_park` warns instead of `ok` on that
  boolean, in a plain sentence naming this ADR.

**No existing status, class or field changes**, and nothing household-facing
moves. A box in this state plays today (the loopback route still exists) and,
once it does not, is a box nothing has proved rather than a box named silent —
telling a household either would be the confusion ADR-0100 exists to prevent,
pointed the wrong way, and there is no household action to offer.

**It lands before any park retirement, deliberately.** #2982's rebuild flips
passive-composite eligibility. A passive composite has no `active_modes`, so
if that flip resolves its width through the wide ring — the path a roleful
composite already rides — the composite park stops naming the box while the
endpoint is still unarmed, and the window between the two is exactly this
seam. Today the composite park covers those boxes. Building the signal after
the flip would mean building it during the window it exists to see.

## Consequences

An operator can now meet a warn on a shape no fleet box has, which is the cost
of disclosure on a playing box and not a gate on anything. In exchange the
greenest verdict stops being reachable by a box the transport does not serve.

The four-class list stays closed: a fifth still needs its own ADR, and this is
not it. The signal carries its own removal condition — when a rebuild or a
class gives this shape an issue to wait on or a command to run, it becomes a
park under ADR-0178's own rule and the boolean goes with it.

Rejected: a fifth park class (carries neither issue nor remedy, which
ADR-0178's own bar forbids); widening the third class to every roleful box
(ADR-0178 rejected exactly that — it hands a household a remedy that cannot
run); narrowing `active_ring_channels_for_topology` to active-crossover
layouts (it is the single eligibility answer the conf.d wire renderer, the
converge pass, the ring-health readers and this classifier all read, so
narrowing it would change what a subwoofer box's ACTIVE ring block declares —
a transport change smuggled in as a classifier fix); and a household surface
for the signal (the box plays, and there is no household action).
