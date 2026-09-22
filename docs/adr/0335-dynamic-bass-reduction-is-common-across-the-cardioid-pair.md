# ADR-0335: Dynamic bass reduction is common across the cardioid pair

- **Date:** 2026-09-22
- **Status:** Accepted
- **Context:** [Issue #5161 F5](https://github.com/jaspercurry/JTS/issues/5161)
  requires common dynamic-bass reduction for the cardioid pair.
  [ADR-0316](0316-rear-woofer-outputs-have-a-physical-variant-identity.md)
  assigns the rear output to the woofer role, so the former per-owner graph
  gave the front and rear separate detectors after their signals diverged.
- **Decision:** Each declared front/rear woofer pair on the same cabinet side
  uses one detector copied from the front lane and one Compressor that applies
  the same gain reduction to both delta lanes. The detector stays after the
  rear stage and taps the front lane, never the rear. One helper derives these
  groups from declared output identities for emission, validation and replay.
  Plain owners retain their separate detectors. F5b disclosure is deferred.
- **Consequences:** The pair shares the dynamic-bass reduction; the rear-stage
  delay, inversion and filters stay unchanged. The rear cannot receive an
  independent dynamic-bass reduction. A rear-only excursion limit is out of
  scope. Existing output limiters and the hearing ceiling remain in force.
