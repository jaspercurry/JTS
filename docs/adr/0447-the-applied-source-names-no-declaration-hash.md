# ADR-0447: The applied source names no declaration hash

- **Date:** 2026-10-04
- **Status:** Accepted: the owner's answer to Q7 on [#5925](https://github.com/jaspercurry/JTS/issues/5925)
  (2026-10-04). Supersedes in part [ADR-0418](0418-the-measured-base-trim-record-goes.md): the
  Consequences sentence "`source.crossover_preview_fingerprint` stays, because it binds the
  declaration into the source fingerprint, so no candidate identity moves."
- **Context:** an applied record's identity (`baseline_candidate_fingerprint`) hashes its
  `source.fingerprint` and its recomposition snapshot. The source held
  `crossover_preview_fingerprint`, a hash of the declaration: the design draft's research, operator
  inputs and manual settings, and the crossover preview with its issues. Since ADR-0418 nothing
  else read it. Each graph input it held is also in the snapshot (the declared preset, the driver
  protection, the device), in `topology_fingerprint`, in `driver_protection_fingerprint` or in
  `measured_candidate_fingerprint`. So it moved the identity alone only for edits that change no
  graph: notes, sources, confidence, warnings, a declared range or spacing, and, under a banked
  candidate, the declared crossover and trims that the candidate replaces. A measured apply writes
  the candidate's crossover into the declaration, so the next review read the record it had just
  written as stale (`baseline_candidate_fingerprint_mismatch`), for a byte-identical graph. The
  source also held `measurements_updated_at` (always null) and `measurement_summary_fingerprint`
  (a topology hash that `topology_id` and `topology_fingerprint` already carry). Their removal
  condition was the next change that moves the source fingerprint.
- **Decision:**
  1. `source.crossover_preview_fingerprint` goes, with `crossover_preview_fingerprint`. An apply
     record no longer builds a crossover preview.
  2. `source.measurements_updated_at`, `source.measurement_summary_fingerprint` and
     `measurement.empty_driver_check_summary` go in the same move.
  3. The source keeps `topology_id`, `topology_fingerprint`, `driver_protection_fingerprint` and
     `measured_candidate_fingerprint`. A declaration edit that changes the graph still moves the
     identity.
- **Hearing:** the graphs do not change. A recorder over the 13 test files that reach the apply path
  (87 tests: 83 `persist_applied_baseline_profile` calls and 283 apply-module writes) found the 251
  graphs byte-identical on main and on this change, and the same early returns. Each record
  differs only in the three removed keys, `source.fingerprint`, `candidate_fingerprint`, and fields
  that also differ between two runs of main. `volume_limit`, the graph doors, the clamp, the 85 dB
  stop and the driver caps do not change.
- **Consequences:**
  - Every applied and candidate identity moves once. A stored record keeps its old source until the
    next speaker apply; a sound save keeps it too. Until that apply, the commissioning view's
    `applied_profile.disclosures` names `baseline_candidate_fingerprint_mismatch` and setup status
    says `matches_applied: false`. No page shows either. That apply writes a new record with a new
    `applied_at`. No banked round goes stale: staleness reads the snapshot's layer fingerprints
    (ADR-0437), and they do not move.
  - After an edit that changes no graph input, a re-apply keeps the stored record and its
    `applied_at`, because the identity, config and timing are unchanged.
  - Rejected: keep the hash. It holds no graph input that the identity does not already hold, and
    it made each measured apply that moves the crossover read stale at once.
