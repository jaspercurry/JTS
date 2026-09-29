# ADR-0386: A rear pair take clears the rear layer at the door

- **Date:** 2026-09-28
- **Status:** Accepted. Supersedes (partial) [ADR-0325](0325-rear-program-compares-measured-symptoms-and-previews-by-superposition.md)
  §3's "on a candidate the run composes itself with the rear section cleared", and
  [ADR-0370](0370-each-run-purpose-declares-what-it-plays-and-a-bass-run-plays-with-room-off.md) §1's
  "every other row clears nothing" for the rear row. Carries the owner's option A for #5737 P7
  ([#5925](https://github.com/jaspercurry/JTS/issues/5925)).
- **Context:** A rear pair take reads each woofer raw, so it plays the tune with its rear stage cleared
  (ADR-0325 §3). Only the CLI built that graph: `jasper-round run --program rear/pair` composed the
  applied tune with its rear section cleared and published it to the candidate bank. That banked a
  measurement-only candidate, which ADR-0370 §2 forbids, and applying its fingerprint would strip the
  rear stage from the box. The page refused every branches preset. On 2026-09-28 the owner chose "Use
  ADR-0370's own mechanism".
- **Decision:**
  1. The rear program row declares that its branches takes clear its own layer, `rear_calibration`,
     on the base and on a named candidate alike. `cleared_layers` reads the take's regime beside its
     purpose and whether it plays the base.
  2. A pair plays one candidate. A pair whose takes clear a layer may play the applied base, which is
     then its parent; any other pair names one saved candidate, else it refuses
     `measurement_candidate_required`. The page and the CLI take this refusal from the one request
     builder.
  3. The door compiles the parent with the layer emptied, as it does for a bass take, and the take
     records `cleared_layers`. Nothing is composed or banked, so the CLI's composition and publish go.
- **Consequences:**
  - The CLI and the page post the same rear pair plan, and the page's `rear/pair` choice starts a
    session.
  - The pair block of the rear view names the parent and the layers its takes played cleared
    (`pair.cleared_layers`, read from the take records). `pair.source`, which read the composed
    candidate's analysis from the bank, goes.
  - A rear-cleared candidate banked before this stays in the bank, and a pair round banked before this
    keeps the packet its bank stored.
  - Rejected: composing the rear-cleared candidate at the session door and banking it (#5980). It
    banks a fingerprint nobody should apply, which ADR-0370 already rejected for the room layer.
