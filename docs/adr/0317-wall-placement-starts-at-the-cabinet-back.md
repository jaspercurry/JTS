# ADR-0317: Wall placement starts at the cabinet back

- **Date:** 2026-09-15
- **Status:** Accepted
- **Context:** The rear-woofer work in [#5161](https://github.com/jaspercurry/JTS/issues/5161)
  needs a wall gap shared by simulation, speaker setup and room tuning. The
  existing rig declaration uses `front_wall_m` for a front-baffle distance.
  The owner requires cabinet-back measurements throughout the new workflow.
- **Decision:** New behind-speaker wall measurements use
  `cabinet_back_wall_m`: perpendicular distance from the rear-panel centre to
  the wall behind the cabinet, stored in metres, with mm/inch input. Each
  physical speaker's future placement record uses this same reference and
  retains its own value. Do not store a second editable front-wall distance.
  Existing `front_wall_m` records retain their baffle meaning; loading them
  never converts or relabels their numbers. The declaration CLI replaces its
  old `--front-wall-*` input with `--cabinet-back-wall-mm/in`; obsolete flags
  fail without writing. Records cannot supply both references.
  The shared geometry owner derives the advisory front-panel-centre distance
  as back gap plus cabinet depth projected on the wall normal. Both depth and
  orientation must be declared; zero toe-in is a value, not an implicit
  default. Missing geometry discloses an unknown front-wall prior while any
  independently declared side-wall prior remains usable.
- **Consequences:** Saved rig snapshots retain their meanings, and new room
  reports carry the cabinet geometry and derived reference. This remains the
  existing advisory baffle-based approximation: a directivity simulation must
  use each driver's actual source coordinates and acoustic/electrical transfer.
  Neither a cabinet gap nor a baffle estimate becomes a cancellation delay,
  a shelf, or a measured calibration. Existing side-wall measurements keep
  their declared reference; they are not relabelled as cabinet-back distances.
  The rig file stays under its existing single writer. A per-speaker room
  placement UI and acoustic calibration handoff remain subsequent #5161 work;
  bench rig geometry is not automatically an installation placement record.
