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
- **Decision:**
  1. A declared geometry that carries `front_wall_m` refuses with `GeometryFieldError`. The error names
     that field whatever its value. It also lists the file's other declared values, as data and as the
     `jasper-declare-geometry set` flags that declare the rig again.
  2. For the live rig file, the fix is to declare the rig again. A round's banked copy has no fix: that
     round's geometry stays unreadable. A reader cannot tell a banked copy from the live file, so the one
     refusal states both.
  3. The boundary prior's front wall comes only from `cabinet_back_wall_m`, `cabinet_depth_m` and
     `toe_in_degrees`. `side_wall_m` is unchanged.
  4. `declared_geometry_unreadable` is the one code a reader reports an unreadable declaration under.
  5. There is no migration and no tolerant reader.
- **Consequences:** Until the rig is declared again, each door answers as follows:
  - `jasper-declare-geometry show` refuses and prints the `set` command.
  - A measurement run is refused before it plays, by the preflight: `declared_geometry_unreadable`, with
    the field and the re-declare action.
  - The commissioning review stays reviewable. The rear calibration's wall-gap check becomes a warning
    that names the field, as ADR-0322 disclosures do.
  - The rear-calibration bank answers ok with the candidate's fingerprint and that warning.
  - Seat-level anchor provenance records `geometry_unreadable` with the field, and the anchor's pose
    mismatch reads "geometry unreadable: <field>".
  - The evidence packet's `declared_geometry` is not evaluated and carries `refused_field`.
  - For a round banked with such a file, the room, rear and near-field views refuse. The round stops
    loading its geometry, as #2902 accepts.

  Rejected: converting the old number into a cabinet-back gap. That needs the cabinet depth and toe-in,
  which the old record never held.
