# ADR-0379: A stored driver declaration in a retired shape refuses by its field

- **Date:** 2026-09-28
- **Status:** Accepted. Supersedes (partial) [ADR-0330](0330-speaker-setup-resolves-inputs-on-the-server.md)'s
  "Legacy values keep their existing authority: … an ambiguous role-only declaration does not prove
  per-target protection" for role-only declarations. Its pinned gain provenance clause stays.
- **Context:** Stored speaker declarations, the live design draft and the drafts banked with rounds,
  still held five retired shapes, each kept readable by a tolerant reader. On 2026-09-27 the owner
  ruled: "We don't care about old speaker configs or old measurements; we're still in development.
  Looking forward, not backward. No legacy branches, no migrations, no tolerant readers for old
  shapes." ([#2902](https://github.com/jaspercurry/JTS/issues/2902))
- **Decision:** Every manual row and research driver names its physical output (`target_id`).
  Nothing binds by role. Each retired shape refuses by its field and names its fix:
  1. A research driver key the prompt retired (`horn_coverage_deg`, `crossover_search_band_hz`,
     `target_fingerprint`): `unknown_driver_fields`; import the research again.
  2. Research whose `artifact_schema_version` is not 2: `research_version_unsupported`; import it again.
  3. A role-only manual row for a role with several outputs: `manual_target_missing`, listing the
     row's values; enter them in each output's driver card.
  4. Any other manual row without a `target_id`: `manual_target_missing`, or `manual_role_unknown`
     when the layout has no output for its role, listing the row's values.
  5. A protective high-pass stored without its `recommended_highpass_hz`: `recommended_highpass_missing`;
     type the Minimum crossover or remove the stored high-pass (in research: import it again).

  There is no migration and no tolerant reader.
- **Consequences:** A stale draft refuses where it is fixed: /sound/speaker/ opens it at the section
  that holds the fix, and a refusal for a row the page cannot place lists that row's values, so a
  save never drops a declared value unseen. A banked round carrying a retired shape refuses when it
  is read again (accepted on #2902). Rejected: a save-time migration, which would rewrite or drop
  declared driver limits without showing them.
