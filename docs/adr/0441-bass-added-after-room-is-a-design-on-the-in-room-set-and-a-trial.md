# ADR-0441: Bass added after room is a design on the in-room set and a trial

- **Date:** 2026-10-03
- **Status:** Accepted: the owner's measurement plan on [#6227](https://github.com/jaspercurry/JTS/issues/6227)
  (2026-10-02), step E3 part 2. Extends [ADR-0437](0437-a-rounds-staleness-is-judged-per-set.md) §6 to a
  complete tune.

## Context

The plan says: "Adding bass later needs only a new trial, because the seat set stays valid while speaker
and cardioid are unchanged." ADR-0437 judges a round per set and makes the next-program pointer name the
set room designs on when room is next. Once room is applied the pointer answers `complete`, and bass is
an option inside the in-room program that the pointer never asks for
([ADR-0429](0429-one-in-room-program-the-room-round-plays-with-bass-and-room-off.md)). So the copied bass
prompt said `Run: … --program room/seat`, a new in-room round, and every prompt said "Re-run room after
any upstream change".

## Decision

1. **The complete answer names the in-room set.** When tuning is complete, the pointer's answer carries
   the newest current bass round's `round_dir` and `set_id` when it names one: the in-room seat set with
   bass and room off (ADR-0437 §3).
2. **The bass prompt designs on it.** The copied bass prompt designs on the in-room round and set the
   pointer names, when room is next or the tune is complete: `contract`, `judge --preview` and `compose`
   carry `--round`/`--set`, then `jasper-round trial`. It measures no new round unless the speaker, the
   seats or the microphone moved. The room prompt designs on it only when the pointer names room next;
   on a complete tune it keeps its `Run:` line.
3. **The other prompts** say "Redo room after a change under it; status names the round and set to design
   on when no new round is needed."

## Consequences

- Staleness reads layer fingerprints, so it cannot see a moved speaker, seat or microphone. The design
  line says that condition in words, and the room prompt of a complete tune still measures, so a re-tune
  after a move starts from a new round.
- The handoff binding gains `bass_round`. The `complete` answer gains `round_dir` and `set_id`, which only
  the binding reads: the page reads the answer's `program`, and `status` keeps its program and reason code.
- A bass document's trial routes to the in-room round as before (`trial_preset`,
  [ADR-0436](0436-the-cardioid-default-one-pair-take-then-one-seat-trial-against-the-rear-off-base.md)
  §6): base against the document at the seats.
