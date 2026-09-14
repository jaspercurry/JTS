# ADR-0295: Prepare a fresh Live session within a fixed idle window

- **Date:** 2026-09-14
- **Status:** Accepted.
- **Context:** Dialling Live on wake adds latency. Reassigning a used socket
  to a new conversation cannot isolate late audio: the primary audio stream
  provides no response identity or completion barrier.
- **Decision:** Close each used session. When the Live-only warm-session
  setting is enabled, prepare one fresh session after release and expire it
  within 60 seconds. Send no microphone audio or synthesized silence until
  a wake acquires it. Never reuse a session that served another conversation.
  The connection owns the socket, expiry, usage and response deduplication;
  a turn owns one conversation and freezes its usage at release.
- **Consequences:** The setting defaults off, shows the approximate idle cost
  beside it, and is read fresh before preconnect and acquisition. The existing
  spend cap gates preparation; idle sessions close when spending permission
  or the saved toggle is revoked. A failed
  preconnect does not cue or retry; a wake can dial normally. Shutdown cancels
  preparation and closes the socket. Each session has one billable interval,
  including any prepared idle time. `session_reused` means a wake acquired
  that previously unused prepared session; `/state.voice.live_session_warm_until`
  reports its expiry. A wake during preparation still waits for the dial.
