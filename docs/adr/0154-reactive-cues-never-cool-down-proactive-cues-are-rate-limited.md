# ADR-0154: Reactive cues never cool down; proactive cues are rate-limited

- **Date:** 2026-08-26
- **Status:** Accepted (recorded when HANDOFF-audible-feedback.md was trimmed
  to its operational spine; sits under non-negotiable #6, "no silent deafness")

## Context

Cues have two triggers, and the obvious "don't repeat yourself" instinct is
wrong for one of them.

A **reactive** cue fires because the user woke the speaker and hit a
wake-blocking state. A **proactive** cue fires from a background supervisor that
noticed sustained failure, with no user action at all — five consecutive
identical reconnect errors, a research job that died, a measurement that could
not run.

A single cooldown policy for both either spams the room during an outage or
reintroduces silence on the exact wake the user is standing there waiting on.

## Decision

**Reactive cues have no cooldown across wakes. Proactive cues are rate-limited
to once per hour, per supervisor.**

If the user wakes the speaker ten times during a failure, they hear the same cue
ten times. Rate state for proactive cues is per-supervisor and in-memory, and
`WakeLoop.play_supervisor_cue` does not rate-limit — the supervisor must.

## Consequences

- **A wake always gets an answer.** Muting after the first cue is the silent
  -second-wake behaviour this whole subsystem exists to prevent; repetition is
  the point, not a defect. A user who pressed the doorbell ten times gets told
  ten times.
- **A long outage does not yell at the room.** Without the proactive limit, "I'm
  having trouble reaching the cloud" would replay on every backoff cycle — the
  exact spam pattern proactive cues were meant to eliminate. One per hour keeps
  the user informed without the room becoming hostile.
- **A fresh boot during a sustained outage fires once.** In-memory rate state
  resets on daemon restart. That is deliberate: a restart is itself evidence
  something changed, and one cue is the right amount of "still broken."
- **Rate-limiting lives with the supervisor that knows the failure.** Putting it
  in `play_supervisor_cue` would impose one window on every proactive path, and
  a research-job failure and a cloud outage are not the same event shape. Each
  new proactive owner must supply its own limit — forgetting is the known
  failure mode, which is why it is called out at the wiring step.
- **Proactive cues yield to active output.** `play_supervisor_cue` skips
  entirely while any assistant output episode is active, so a supervisor cannot
  garble an in-progress reply by layering a second WAV onto the single TTS
  stream. A skipped proactive cue is acceptable; a garbled reply is not.
