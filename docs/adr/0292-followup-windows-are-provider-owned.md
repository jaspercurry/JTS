# ADR-0292: Follow-up windows are provider-owned

- **Date:** 2026-09-11
- **Status:** Accepted; supersedes the host-window half of ADR-0290.
- **Context:** ADR-0290 gave the voice host its own follow-up window, opened
  after playout drains for any adapter declaring `host_followup_window`. No
  adapter ever set the flag, so the machinery — a second turn-transition path
  with its own deadline bookkeeping, frozen pre-roll, acquire replay and
  `conversation.followup` events — only ever ran its disabled branch.
- **Decision:** A follow-up window belongs to whichever provider can hold the
  microphone open for one. The host keeps no window of its own: an endpointed
  turn ends as soon as playout drains. `JASPER_FOLLOWUP_TIMEOUT_SEC` now shapes
  exactly one thing, the continuous watchdog's inactivity window, which GPT-Live
  runs inside its own turn. Push-to-talk keeps its button lifecycle.
- **Consequences:** The capability, the daemon's window machinery, the
  `awaiting_followup` `/state` field and the `conversation.followup` events are
  gone; `/state` still reports `followup_timeout_sec`. An adapter that later
  wants follow-ups implements them behind its own turn, once hardware proves
  them usable — the evidence ADR-0290 waited on and never got.
