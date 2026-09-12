# ADR-0295: A Live session may stay warm for a minute, behind a toggle

- **Date:** 2026-09-11
- **Status:** Accepted; amends the "one billable Live session is owned by one
  wake conversation" rule stated in `jasper/voice/openai_live_session.py` —
  one session may now serve several consecutive conversations.
- **Context:** OpenAI Live dials a new websocket session per wake inside
  `acquire_turn`, measured p50 554 ms, p90 1531 ms, max 3875 ms, and the
  previous conversation's close costs ~2.6 s the next wake can inherit. Live
  bills per connected minute (~$0.05), so holding a session open between
  conversations is real money — an always-on session is not on the table.
- **Decision:** A finished conversation may leave its session dialled for a
  fixed `WARM_SESSION_SEC = 60`, behind `JASPER_OPENAI_LIVE_WARM_SESSION` on
  the `/assistant/voice/` page — Live-only, **default off**, read fresh per
  conversation from the wizard-owned SSOT file. A warm session is only a
  deferred close: an idle timer runs the same `_close_live_session` path with
  the same events, and a wake inside the window cancels it and reuses the
  socket. Nothing leaves the host while warm — not one frame, not synthesized
  silence; the turn's sender task dies with the turn. A window is one billable
  interval, opened on the dial and closed on the session's own meter, so idle
  minutes bill as connected minutes, which is what they are.
- **Consequences:** With the toggle on, a follow-up wake answers without the
  dial and costs about $0.05 per idle minute of quiet house. A session the
  server closes while warm is not an outage: the next wake dials as before, no
  cue, no counted failure. `provider.turn_ended` carries `session_reused`, and
  `/state.voice.live_session_warm_until` says when a warm session stops
  billing. `stop()` closes a warm session at once.
