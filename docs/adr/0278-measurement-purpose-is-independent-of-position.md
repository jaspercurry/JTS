# ADR-0278: Measurement purpose is independent of position

- **Date:** 2026-09-09
- **Status:** Accepted. Amends [ADR-0260](0260-poses-are-flexible-and-categorized-and-bass-extension-has-no-nearfield-rung.md).
- **Context:** The lab arm moves a microphone around a fixed speaker. Its
  bearing positions can measure the room as well as the speaker. Geometry
  therefore cannot decide playback scope or acoustic gating.
- **Decision:** Extend the existing measurement program contract with capture
  purpose, stimulus regime and optional screen copy. One bundled configuration
  owns layouts and default sizes; counts derive from its ordered positions.
  Purpose selects the baseline layer and analysis. Mover reach stays separate.
  Preserve existing program identities and infer purpose from geometry only
  when reading older untagged requests or records.
- **Consequences:** Room tools can consume explicitly tagged arm captures while
  retaining their true geometry. The same runner, protected playback, capture,
  position-ready protocol and evidence bank serve all plans. This adds no bass
  boost playback mode or changes to driver protection.
