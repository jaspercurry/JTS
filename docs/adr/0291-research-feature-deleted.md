# ADR-0291: The background research feature is deleted

- **Date:** 2026-09-10
- **Status:** Accepted
- **Context:** JTS ran its own background lookup: a second text-model
  request the assistant started when asked to research something, held
  across turns, then announced later by interrupting the household. It
  carried a scheduler, a SQLite job store, a text-provider registry, a
  wake-window that reinterpreted mic frames mid-session, a failure cue,
  five config knobs and its own pricing rows — all to reach answers the
  voice model already has, or can fetch itself. Frontier models now
  answer general knowledge directly and search natively.
- **Decision:** There is no JTS-side research or lookup tool. The
  assistant answers general knowledge from the provider model, and gets
  current facts through the provider's own native search where it exists
  (GPT-Live delegates to the Responses API). The system prompt scopes
  tool authority to what only the tools know — device state, timers,
  weather, transit, music, actions — and says general knowledge is
  answered directly, with a few words of caveat when a figure may be
  stale.
- **Consequences:** The spend ledger keeps its provider-agnostic
  background-usage API and pricing overrides, and the text-model
  pricing rows: a backend that runs its own server-side lookup bills
  through them. Providers without native search have no lookup path
  at all; on those the assistant answers from model knowledge or says it
  does not know. Answers are no longer held and delivered later, so
  nothing barges into the room unasked. Deleting the announcer retires
  the only caller of the output-gate episode handover, so that
  primitive and its tests go with it. No ADR is superseded — the
  feature came from a plan doc, not a decision record.
