# ADR-0134: Wake labelling is post-hoc, never real-time

- **Date:** 2026-08-26
- **Status:** Accepted (recorded when HANDOFF-wake-telemetry.md was trimmed to
  its operational spine; the decision dates to the 2026-05-21 design
  conversation)

## Context

A wake corpus is only as useful as its labels, and the tempting way to get
labels is to ask the person who was standing there: a "that was a false
alarm" control in a web UI, or an "I just said Jarvis" button to mark a miss
the moment it happens.

## Decision

**Labels are applied after the fact, from captured events. The speaker offers
no real-time labelling control, and no button for reporting a missed wake.**

The two label paths are the operator editing `label` / `label_notes` on
captured rows, and the in-conversation diagnostic tool: saying "flag that"
writes `voice_flagged` on the offending event with the complaint in
`label_notes`, and marks the wake of the flagging utterance itself
`flag_action` so it can be filtered out of interaction rollups.

## Consequences

- **A miss button would not have worked anyway.** By the time a person reaches
  a phone the in-memory pre-roll ring has already rolled past the moment they
  want to report, so the button would capture the wrong audio. False negatives
  stay the domain of offline scoring against the gold corpus.
- **Voice is the only in-the-moment channel**, and it is a label on an event
  that already exists rather than a new capture path — no extra UI surface, no
  extra wake-path work.
- **Post-hoc review needs no product surface.** Browsing and labelling the
  day's captures happens off the Pi against the fetched corpus, which is where
  the analysis tooling already lives. A dedicated on-device review wizard was
  planned and deliberately never built.
