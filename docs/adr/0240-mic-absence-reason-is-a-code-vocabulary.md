# ADR-0240: The voice-input-absent marker's `reason=` is a closed code vocabulary; `detail=` carries the prose

- **Date:** 2026-09-06
- **Status:** Accepted
- Refs: #4027. Supersedes the mechanism sentence of ADR-0239's Consequences
  (`transient=1` as a second wire field); its decision — the daemon plays the
  cue, transient parks skip it — stands.
- **Context:** Every `mark_voice_input_absent` call site wrote free-form
  prose as the marker's `reason=` line, so `/state.microphone.reason` and the
  doctor headline were matching (or displaying) whole sentences, and
  `ADR-0239`'s transient class was a second `transient=1` line a caller could
  forget to set.
- **Decision:** `reason=` carries a closed snake_case code from
  `jasper.mic_presence.MIC_ABSENT_REASONS`; the prose that code cannot carry
  rides beside it as `detail=`. `/state.microphone.{reason,detail}` publish
  both. Whether a park is a round trip the reconciler itself ends is a
  property of the code (`TRANSIENT_MIC_ABSENT_REASONS`), so no second wire
  field exists for it. An unrecognised or missing code reads back as
  `unknown` — the vocabulary is closed, so a site that invents a code cannot
  silently pass its own token through.
- **Consequences:** A consumer switches on `reason`, never `detail`; the doctor
  and `/state` headlines render `detail`, falling back to generic prose
  (never the code) when it is absent. An older build's marker, written before
  this ADR, carries no `detail=` — its unrecognised `reason=` prose becomes
  `detail` instead of vanishing behind `unknown`. Adding a park site means
  adding its code to `MIC_ABSENT_REASONS` (and to
  `TRANSIENT_MIC_ABSENT_REASONS` if the park is transient); it cannot invent
  ad hoc reason text.
