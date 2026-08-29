# ADR-0193: The audition door is a runtime-only swap

- **Date:** 2026-08-29
- **Status:** Accepted

## Context

The owner cannot hear what the driver-linearization stage is doing. The applied
graph carries measured correction — per-driver linearization filters and the
summed blend correction — and the only way to judge them by ear was to un-apply
the profile, listen, and apply it again. That is a durable write in each
direction, on the DSP output path, to answer a question that is not a decision.

An inline audition was deliberately not resurrected once before
(`web_commissioning.py`, the automatic-driver restore path) because the version
being asked for then would have left transient graphs the durable restore path
had to reason about.

## Decision

An audition swaps the RUNNING graph and nothing else.

**Two named layers.** `full` is the applied graph, byte-for-byte off disk.
`baseline` is the same graph with the two measured-correction stages omitted and
nothing else changed — crossover, trims, delays, polarity, protection, limiters,
headroom, mixer, room PEQ and preference EQ all survive. One axis, so the ear
can attribute what it hears. The reduced graph is re-emitted from the live
applied profile's immutable snapshot (`recompose_applied_baseline_yaml`, new
`drop_measured_correction` flag) with `out_path=None`; no candidate is read and
no bytes reach disk.

**Crash-safety is structural, not procedural.** The swap is
`set_active_config_raw`, which leaves CamillaDSP's persisted `config_file_path`
alone. Nothing on the path writes the baseline-profile SSOT, the canonical YAML
or the outputd statefile, so the durable anchor still selects the applied graph
for the whole audition. A CamillaDSP restart, a reboot or a `kill -9` of the
owner reverts by doing nothing. `stop` is the fast path, not the safe one.

**One process owns the swap and puts it back** — at a 30-minute deadline, on an
interrupt, or on any error, with the restore run to completion before a
cancellation propagates (ADR-0179's shape). A record in `/run` carries a token
so a replacement `start` takes over cleanly and the displaced owner stands down
instead of un-swapping it.

**The interlock is the existing door.** `live_measurement_session` answers
"may an operator door act right now", and this consumes it rather than reading
the volume statefile itself.

## Consequences

- Auditioning costs no durable write and no apply transaction, so the worst
  outcome of any failure is a speaker that reverts on its own.
- The two layers are **not level-matched**: removing a cut hands its depth back
  in that band, and the linearization headroom charge comes back broadband.
  `level_give_back_db` discloses both. Compensating was rejected — it would move
  a trim, and identical trims are what makes the comparison mean anything.
- The audition is foreground-owned, so an SSH drop ends it. That was chosen over
  a daemon-held deadline because it needs no new machinery and makes the
  watchdog structural rather than something a supervisor must remember to run.
  `AUDITION_DEADLINE_S` exists only while that holds.
- A third natural layer (baseline plus linearization, without blend) was
  rejected: the emitter composes the two stages independently and it would have
  been nearly free, but it answers the same question and costs a third name.
