# ADR-0381: A stored bass section in the ADR-0352 form refuses by its field

- **Date:** 2026-09-28
- **Status:** Accepted. Supersedes (partial)
  [ADR-0359](0359-the-bass-boost-plays-at-every-volume-and-gives-way-only-near-clip.md): §1's plain-shelf
  clause ("An ADR-0352 plain section plays its full-boost Loudness shelf ..."; the block now always has 3
  filters, not "at most 3"), §3's "A stored ADR-0352 section still loads. Its `reference_level_db` is
  ignored, and its normalized payload keeps its bytes", and its downgrade advice ("Before a downgrade past
  this ADR, `jasper-round apply` an ADR-0352 tune"). Carries the owner's no-backward-support ruling
  ([#2902](https://github.com/jaspercurry/JTS/issues/2902)).
- **Context:** ADR-0359 kept stored ADR-0352 sections loading: a plain one played its full-boost
  `Lowshelf`, and its payload kept its bytes. On 2026-09-27 the owner ruled: "We don't care about old
  speaker configs or old measurements; we're still in development. Looking forward, not backward. No
  legacy branches, no migrations, no tolerant readers for old shapes."
- **Decision:**
  1. Only the ADR-0359 form loads: `linkwitz_transform` and `delta_highpass_hz` are required.
  2. A section with `low_boost_db`, `reference_level_db` or any other unknown field refuses
     `bass_descriptor_malformed` before any graph is built or loaded. The validator
     (`DynamicBassDescriptorError.field`) and the prescription read (its refusal's `field` evidence) name
     that field; the candidate constructor and `jasper-doctor`'s bass extension row name it in their
     detail. Two stored-tune doors do not: the runtime contract answers `bass_extension_block_invalid`
     (follow-up when its split, audit row R-048, lands), and the candidate bank skips the artifact, so
     its readers answer `composition_saved_tune_unavailable` or `not_found` (follow-up: #2902's
     other-readers row).
  3. The `Lowshelf` boost, its model and the old section's payload are gone.
- **Consequences:**
  - A box whose applied tune carries the old form parks after the deploy at the proven parked graph
    (`volume_limit` 0.0, every output muted), because the code that played that tune is deleted. That is
    the cost #2902's ruling accepts, not a proof gone stale (ADR-0101). The first guard is LC's check of
    every box before the deploy: apply an ADR-0359 tune first.
  - On a parked box the way out is a tune whose lineage never carried the old form.
    `jasper-crossover-prescriber status` lists the banked tunes that still load: `jasper-round apply` one,
    or `jasper-crossover-prescriber compose` a document with its fingerprint as `base` and an ADR-0359
    `bass` section, then apply what that banks. A composition keeps its base's bass section unless the
    document names it (`compose_candidate`), so a tune composed from the old one refuses too. What reads
    the saved tune refuses: `jasper-round reset` (`reset_compose_failed`), `"base": "saved"`
    (`composition_saved_tune_unavailable`), a round's base stop (`measurement_baseline_unavailable`) and
    the web's re-apply. A bank holding only the old lineage has no way out at this head.
  - The bass contract drops `bass_low_boost_db_invalid` and `bass_reference_level_db_invalid`.
