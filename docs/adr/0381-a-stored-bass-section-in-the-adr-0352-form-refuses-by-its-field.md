# ADR-0381: A stored bass section in the ADR-0352 form refuses by its field

- **Date:** 2026-09-28
- **Status:** Accepted. Supersedes (partial)
  [ADR-0359](0359-the-bass-boost-plays-at-every-volume-and-gives-way-only-near-clip.md) §3: "A stored
  ADR-0352 section still loads. Its `reference_level_db` is ignored, and its normalized payload keeps
  its bytes". Carries the owner's no-backward-support ruling ([#2902](https://github.com/jaspercurry/JTS/issues/2902)).
- **Context:** ADR-0359 kept stored ADR-0352 sections loading: a plain one played its full-boost
  `Lowshelf`, and its payload kept its bytes. On 2026-09-27 the owner ruled: "We don't care about old
  speaker configs or old measurements; we're still in development. Looking forward, not backward. No
  legacy branches, no migrations, no tolerant readers for old shapes."
- **Decision:**
  1. Only the ADR-0359 form loads: `linkwitz_transform` and `delta_highpass_hz` are required.
  2. A section with `low_boost_db` or `reference_level_db` refuses `bass_descriptor_malformed`, naming
     that field, before any graph is built or loaded. Any unknown field refuses the same way.
  3. The `Lowshelf` boost, its model and the old section's payload are gone.
- **Consequences:**
  - A box whose applied tune carries the old form parks at the proven parked graph (`volume_limit`
    0.0, every output muted, never louder) until a tune in the ADR-0359 form is applied. LC checks
    every box before the deploy.
  - The bass contract drops `bass_low_boost_db_invalid` and `bass_reference_level_db_invalid`.
