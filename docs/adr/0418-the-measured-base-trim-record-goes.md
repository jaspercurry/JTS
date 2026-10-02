# ADR-0418: The measured base-trim record goes

- **Date:** 2026-10-02
- **Status:** Accepted. Supersedes in part [ADR-0212](0212-way-1-reuses-the-existing-layers-it-does-not-fork-them.md)
  (in the "No per-role trim exists" bullet, all after its first sentence: the `base_trim_no_frame`
  refusal and the seam that left a standing record alone; in the first Consequences bullet, the
  `level_match.base_trim` block and the `dsp.baseline_base_trim_banked` journal line) and
  [ADR-0227](0227-owner-rulings-the-prose-pass-surfaced.md) §7 (ruling S20) and §8 (ruling S16 (d)).
- **Context:** A measured apply banked its per-driver trims in
  `/var/lib/jasper/active_speaker_driver_base_trim.json` (`driver_base_trim.py`). The one reader,
  `measured_level_trims`, gave a measurement graph its level match. Since
  [ADR-0416](0416-the-measurement-overlays-go.md) no take carries a level match, so that reader has
  no caller. The apply still wrote or cleared the record, and nothing else reads it: no doctor
  check, page, round view or v2 state field. This is follow-up 1 on
  [#6226](https://github.com/jaspercurry/JTS/issues/6226).
- **Decision:**
  1. The record goes: `driver_base_trim.py` (its writer, reader, clear, status words and refusal
     codes), the apply's call to it, the `JASPER_ACTIVE_SPEAKER_DRIVER_BASE_TRIM_STATE` path
     override and the `dsp.baseline_base_trim_banked` event.
  2. An apply no longer writes or clears a trim record. The applied profile keeps its own trims,
     `level_match` and provenance.
  3. A record file left on a speaker stays. Nothing reads it, and there is no migration.
- **Hearing:** the apply path writes the same graphs. A scratch proof ran 17 apply-path test files
  on main and on this change. Their 66 applying tests make 80 applies: the 67 graph files those
  applies name are byte-identical, and the 80 applied profiles differ only in fields that also
  differ between two runs of main (a random `op_id` and candidate fingerprints). `volume_limit`, the
  graph doors, the clamp, the 85 dB stop and the driver caps do not change.
- **Consequences:** ADR-0212's fact stands: a way-1 speaker has no trim pair. Only the refusal and
  the journal line about the record go. ADR-0227 §§7–8 lose their last site; the applied profile
  still names its evidence in `level_match.comparison`. `source.crossover_preview_fingerprint`
  stays, because it binds the declaration into the source fingerprint, so no candidate identity
  moves. Rejected: keeping the record for a later reader, because the applied profile already holds
  the same trims.
