# 0210 — Polarity has two frames, and one conversion owner

Date: 2026-09-01. Status: accepted.

## Decision

The polarity vocabulary is two-frame by design, and every surface must say
which frame it speaks in:

- **Action frame** (`keep` / `invert`): a flip relative to the speaker's
  DECLARED polarity. This is what an alignment prescription commits and what
  `measured_crossover_candidate.effective_preset` executes — `invert` flips
  the declared branch polarity, `keep` leaves the declaration untouched. The
  word says nothing about the graph that is currently playing.
- **Absolute frame** (`inverted` flags): the wiring truth per role, as an
  applied profile records it.

`commanded.profile_graph_summation` is the ONLY place the two frames may
meet, and its `draft_inverted_by_role` parameter is required because there is
no safe guess — the conversion owner established after the PR #2614 incident
and pinned by `tests/test_crossover_v2_commanded_axis_incident_replay.py`.
No other module may convert between the frames, and no surface may print a
polarity word without its frame being derivable from the surface's own
contract.

## Why

Paid for twice. PR #2614: a summation assumed one frame and was wrong on any
speaker whose draft declares an inverted branch. The 2026-09-01 recommission
campaign (#3484): rounds 1–4 committed `invert`, round 5 re-derived `keep`,
and applying round 5 would have flipped the tweeter while every receipt
honestly read `keep` — because the word is an action against the declaration,
not against the playing graph. No arithmetic was wrong either time; the frame
was undeclared.

## Consequences

- The evidence packet's `structural_history` block (which replaced the
  trim-only history under #3484) keeps its polarity column in the action
  frame across all rows and states so in its `note`; it must not mix in
  applied-profile `inverted` flags, which would re-create #2614's defect at
  read time.
- An incumbent-vs-candidate alignment row ("polarity SAME / FLIPPED") is the
  disclosure that would have caught round 5 directly. Building it requires
  handing the packet builder a compiled preset so the comparison can run
  through the one conversion owner — deliberately not done as part of #3484;
  it is machinery, and it waits for an owner call.
- Any future surface that needs cross-frame polarity goes through
  `profile_graph_summation` or does not exist.
