# ADR-0394: The distortion reading banks at capture, on the take

- **Date:** 2026-09-29
- **Status:** Accepted. Supersedes (partial)
  [ADR-0346](0346-analysis-views-never-write-a-rounds-evidence.md)'s rejection of "computing … the
  H2/H3 reading at bank time as evidence".
- **Context:** `jasper-round-views distortion` read the capture ring, a copy of every take record and
  its WAV that the round bank wrote. For each MEASURE or branch take it rebuilt the program from the
  flow state, proved it by stimulus id, and replayed the take's analysis on the decoded WAV as a
  fidelity gate. Only then did it read H2/H3. It needed the state, the applied profile and an operator
  calibration file, and it could not read a live bundle. The capture host now analyses every take once
  and banks its curves (ADR-0383) and its bass reading
  ([#5737](https://github.com/jaspercurry/JTS/issues/5737) C4 PR 1). The kept impulse (ADR-0354) cannot
  stand in for the reading: its 0.25 s pre-guard does not hold the harmonic images.
- **Decision:**
  1. The capture host's analysis seam reads H2/H3 on every per-driver sweep of a take. It uses the
     samples, anchors, drift and calibration the take's analysis read. A level probe, and a program
     with no per-driver sweep, bank `null`.
  2. `analysis.distortion` banks each role's block pooled over its sweeps: the probe rows, the worst
     point and the floor-limited fraction, as the view published them. The probe ladder and the
     pooling rules are fixed when the take banks.
  3. A reading that raises banks a coded gap (`harmonic_window_out_of_range`,
     `sweep_grids_disagree` or `coverage_short`) and logs `event=active_speaker.distortion_not_banked`.
     The take keeps its curves.
  4. The view reads a bundle's MEASURE and candidate-branch records, whatever their verdict, and opens
     no recording. It adds each take's WAV id and drive label, and carries the take's
     `capture_calibration`. A take whose analysis failed, or whose reading is a gap, is listed as
     refused. With no readable take, the view refuses by the one reason every refused take shares,
     else `no_admissible_captures`. A take banked before this field refuses the view with
     `take_curves_not_banked`, `field: analysis.distortion`.
  5. The band and calibration flags go, and the capture ring goes with its last reader. The artifact
     stays a view beside the round (ADR-0346), now `jts_harmonic_distortion/5`.
- **Consequences:**
  - Cost, measured on an M1 Max under heavy load: the reading adds 0.27 s of CPU to a 2-way MEASURE
    take (6 sweeps) and 0.40 s to a branch take (4 sweeps). That is +19% and +28% of the take's bank
    (decode, analysis, blocks and JSON). The traced peak memory does not rise, because the reading
    runs after the analysis peak; the process peak rose 2 to 18 MB between runs. A block is about
    5 to 6 KB of JSON.
  - The view reads a round in milliseconds, where the replay took 2.5 to 3.4 s per take, and it reads
    live bundles.
  - A new probe ladder or pooling rule needs new takes (#2902).
  - Rejected: banking every sweep's full-resolution reading, as JSON (about 2.4 MB per MEASURE take)
    or as a binary file beside the impulses (exact only to float32, and the pooling stays in the view).
