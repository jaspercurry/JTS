# ADR-0314: The host clock has one correction observable

- **Date:** 2026-09-14
- **Status:** Accepted
- **Context:** The host-clock probe and integral servo use resampler
  correction ppm. `ObsMode` has one variant, no selection branch, and no
  production reader of its status field. The probe also publishes its final
  result and response ratio twice. Doctor reads `final_result`; the
  `last_result` and `response_ratio` aliases have no production reader.
- **Decision:** Remove `ObsMode`, its configuration field, and its status/log
  fields. Keep `Obs::correction_ppm` as the observable. Keep `final_result`
  and `final_response_ratio` as the final probe fields and remove their
  aliases. Keep the separate last-attempt fields used during retries.
- **Consequences:** No control-law, actuator, timing, or clamp changes. The
  status fragment loses three redundant keys. Tests for the removed mode
  selection disappear; the existing serialized-fragment pin follows the
  smaller shape. A future second observable requires a new decision and a
  live consumer. This supersedes only the typed mode/configuration/status
  requirement in [ADR-0109](0109-the-combo-host-clock-servo-observes-resampler-correction.md).
  [ADR-0250](0250-the-host-clock-dll-block-is-deleted-not-ticked.md) continues
  to govern the deleted outer DLL. Owner authorization: #4803 R-201, Astra
  lane B on #5061.
