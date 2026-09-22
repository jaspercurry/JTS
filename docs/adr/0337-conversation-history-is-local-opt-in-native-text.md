# ADR-0337: Conversation history is local, opt-in native text

- **Date:** 2026-09-22
- **Status:** Accepted

## Context

The conversation-history build plan mixed decisions with a stale account of
implementation. Retiring it must preserve why capture has its own store and
privacy controls without creating a replacement handoff tier
([ADR-0199](0199-the-handoff-doc-corpus-is-deleted.md)). This record retains
those decisions; [privacy.md](../privacy.md#conversation-history) owns the
public data contract. Current behavior is checked from code, not this record.

## Decision

Conversation history is a household read-back surface: the perceived command
(the provider's speech recognition) paired with the assistant's reply. It
also helps explain misheard commands. It is not an interactive text assistant
or an LLM-callable tool, and it has no per-member attribution or diarization.

Use native provider text through `LiveTurn.capture() -> TurnCapture | None`.
The host owns the write; providers expose text or bounded metadata. Do not
add an audio recording, a second local/cloud speech-to-text pass, a resident
model, or the test-only voice trace machinery to implement history. Metadata
fallbacks must not collect raw provider payloads, prompts, or tool arguments.

Use a dedicated SQLite `ConversationStore`, separate from wake-event data
and spend accounting: these have different privacy and retention rules, and
`usage.db` keeps its no-transcript promise. Retain nullable `tool_calls_json`
and `data_json` fields for later tool context and links/rich content. Reserved
columns do not mean those features have shipped.

Capture is default-off, opt-in, and gated by voice-assistant pause. Read the
wizard-owned switch fresh for each write. Keep history local, do not copy
transcripts into the journal, and provide clear-all plus write-time retention
(defaults: 30 days and 500 rows; either limit can be disabled). Capture and
retention failure must leave the voice turn usable.

The history page is a dedicated socket-activated wizard with a static
ES-module renderer, newest-first rows, a date filter, and shared read/mutation
guards. Treat all transcript text as untrusted DOM text. Its current route is
`/assistant/chat/`; the original top-level `/chat` choice did not claim the
room-correction server's scoped interactive routes. If a future interactive
assistant needs the same route, consider `/history` then.

Build this first Feature concretely. Extract shared store, web, scheduler,
or provider facilities only when a second Feature needs them; do not build a
generic Feature framework from this one case. See
[extensibility.md](../extensibility.md#4-the-feature-contract).

## Shipped evidence and deferred work

Checked at `04a2a9bc949223079dc8d39371e7c9cd16cc1744`:

- [Turn teardown](../../jasper/voice/turn_lifecycle.py) reads capture after
  provider release and passes it to the single
  [capture writer](../../jasper/voice/conversation_capture.py).
- [Storage](../../jasper/conversation_history.py) and the
  [chat wizard](../../jasper/web/chat_setup.py) implement opt-in, pause gating,
  retention, and clear-all. The production writer leaves `tool_calls_json`
  null. The doctor's chat check reads the store's health; `/state.chat` is
  not a current surface, per [ADR-0270](0270-state-is-the-daemons-posture-and-a-health-fact-is-a-snapshot.md).
- Gemini input/output transcription has shipped. OpenAI, Grok, and GPT-Live
  also expose native text when received; the old Gemini metadata-only and
  Grok user-only roadmap is not a current limitation. Provider event arrival
  still determines whether a particular row has text.

Per-row deletion in the UI, history search, richer link/tool-call displays,
and per-member attribution remain deferred. Date filtering already exists.
A separate Grok assistant-text fallback is needed only if native text proves
insufficient and actual Grok use warrants it. These are not authorization to
add another transcription pass or an interactive assistant.
