# ADR-0388: A declared rig geometry that carries `front_wall_m` refuses by that field

- **Date:** 2026-09-29
- **Status:** Accepted. Supersedes (partial) [ADR-0317](0317-wall-placement-starts-at-the-cabinet-back.md):
  "Existing `front_wall_m` records retain their baffle meaning; loading them never converts or relabels
  their numbers", and the consequence "Saved rig snapshots retain their meanings". Carries the owner's
  no-backward-support ruling ([#2902](https://github.com/jaspercurry/JTS/issues/2902)).
- **Context:** Since ADR-0317, `jasper-declare-geometry set` takes the wall behind the speaker as
  `cabinet_back_wall_m` and never writes `front_wall_m`. The reader still took a stored `front_wall_m` as
  the boundary prior's front wall. On 2026-09-27 the owner ruled: "We don't care about old speaker configs
  or old measurements; we're still in development. Looking forward, not backward. No legacy branches, no
  migrations, no tolerant readers for old shapes."
- **Decision:** A declared geometry that carries `front_wall_m` refuses with `GeometryFieldError`, which
  names that field whatever its value. This holds for the rig file and for a round's banked copy. The
  refusal names the fix: declare the rig again with `jasper-declare-geometry set`. The boundary prior's
  front wall comes only from `cabinet_back_wall_m`, `cabinet_depth_m` and `toe_in_degrees`. `side_wall_m`
  is unchanged. There is no migration and no tolerant reader.
- **Consequences:** Such a file is unreadable wherever it is loaded until the rig is declared again.
  `show` refuses, and a measurement take banks no trusted band. The room, rear and near-field views of a
  round that banked a copy refuse too. Rejected: converting the old number into a cabinet-back gap. That
  needs the cabinet depth and toe-in, which the old record never held.
