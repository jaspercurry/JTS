# ADR-0366: One pose model, a level found at the pose, and a band stated from it

- **Date:** 2026-09-26
- **Status:** Accepted. Supersedes (partial) [ADR-0360](0360-near-field-driver-takes-are-reference-evidence-one-driver-per-pose.md)
  §1 and §3 (a driver only at a reference near-field pose, within 100 mm) and §5's key (the
  exemption follows the distance, not the driver), and
  [ADR-0260](0260-poses-are-flexible-and-categorized-and-bass-extension-has-no-nearfield-rung.md)
  §4 (the close reference as a view). Amends [ADR-0298](0298-tiers-and-stages-retire.md) (registry
  rows are presets over named layouts) and [ADR-0277](0277-the-seat-cloud-adds-eleven-positions-without-renaming-saved-cubes.md)
  (the `seat/*` rows retire; their layouts keep their names, coordinates and order).

## Context

The owner's direction for the tuning zone ([#5733](https://github.com/jaspercurry/JTS/issues/5733)
C1): a few general tools that work at any distance, for 1-, 2- and 3-driver speakers, with
measurement and analysis kept apart behind clear contracts. The LLM configures them for each
speaker and guides the person placing the microphone; the product does the measuring and the
analysis.

At `2eeeaf4be` the registry (`measurement_plans.json`) holds 28 rows. Several encode one distance
(`close/spot` at 0.3 m, `bass/nearfield` at 3 cm), one driver (`nearfield/rear`) or one pose set, and
two pairs are exact duplicates (`seat/cloud` = `room/cloud`, `seat/express` = `room/seat`). A pose
may name a driver only at a reference near-field pose. A take's level comes from four places: the
probe at a driver pose (ADR-0365), the per-driver CHECK solve, the seat anchor, and SNR retakes;
`bass/nearfield` levelled a 3 cm pose from the 1 m anchor
([#5698](https://github.com/jaspercurry/JTS/issues/5698)). The trusted band is decided per view: the
gate floor in gated readers, `STEP_BAND_HZ` in the near-field view, the far-field ceiling only in
the close-reference view, and the declared room reaches only that view
([#3665](https://github.com/jaspercurry/JTS/issues/3665) item 10).

## Decision

### 1. A take is what plays, where the microphone is, and the level found there

- **What plays** is unchanged: the purpose (which analysis the take feeds, ADR-0278), the regime,
  the stimulus band and the graph scope (the layering rule).
- **The pose** is one record, shared by the registry, the prompts, the captures and the banked
  takes. It has a kind, a distance, an angle or a seat offset, and optionally a driver.
  - The **kind** names what the distance and angle are measured from: `bearing`, the speaker's
    reference axis (default distance 1 m, the mark); `seat`, the listening head (an offset, no
    angle); `close`, a driver's or the baffle's axis, placed by hand; `behind`, the back panel's
    axis.
  - The **distance** is a setting of the take, stated in metres from that reference. Any kind may
    state one. No row exists to hold a distance.
  - The **driver** is optional: a measurement target id (`woofer`, `woofer:rear`, `tweeter`). A
    pose that names one plays that driver alone through the neutral protected drivers graph, at any
    kind and distance, and no capture that plays every driver (CHECK, the entry-baseline timing
    take) runs for that pose ([#5696](https://github.com/jaspercurry/JTS/issues/5696)). Purpose
    stays a separate choice, and each reader keeps its own admission: ADR-0360 §2 stands, so a
    near-field driver take stays `reference`.

### 2. Every take's level is found at its own pose, by one solver

- A pose that plays one driver probes first (ADR-0365), to 80 ± 2 dB at the microphone
  (ADR-0361).
- A seat pose plays at the seat reference, found at the head (75 dB, `seat_level_reference`). It
  moves onto the same solver with #5714 PR 3a.
- A per-driver schedule's CHECK capture is its probe. A lateral pose that must replay its anchor's
  excitation (the return-to-mark bracket) carries its anchor's level.
- Every other pose (bearing or behind, or close without a driver) probes what it plays; a summed or
  branch take probes its summed stimulus. That probe, and its target at the microphone, come from
  banked far-field takes with #5714's far-field slice. Until then these poses play at the
  seat-equivalent level, as today.
- The solve is `level.solve_gain`, never above the take's ceiling, the seat-equivalent level
  (ADR-0361 §1), so no pose plays louder than a seat take does today. The 85 dB stop, its single
  source, the fader clamp and the declared driver caps are unchanged (non-negotiables 1 and 2). SNR
  retakes stay: they ask whether a take is clean, not how loud it is.
- A level is carried only within one driver and one set of places, moved by the distance models in
  §4. It is never predicted from another distance's anchor or from another driver: jts3's two
  woofers of one model play 6.2 dB apart at the same drive (#5714).
- The bass ladder stays a deliberate series (ADR-0365). Its rungs step from the seat reference.
  Every bass layout sits at the seat or at the mark distance. A bass pose anywhere else would first
  need its ladder anchored at its own probe; none remains once `bass/nearfield` retires.

### 3. The analysis states each reading's trusted band from the pose

One function in `jasper/audio_measurement/` owns the band. It reads three inputs only: the take's
pose, the declared size of the drivers that played, and the declared room. It never reads a row, a
program id or a view's own constant, and every reader of a take uses it.

- **Lower edge, the gate floor:** `2.5 / T` (`f_trusted_floor_hz`), where `T` is the
  reflection-free window at the pose's distance: the declared room's first bounce there
  (`DeclaredGeometry.first_bounce_s(distance)`), or the reflection search bound
  (`SEARCH_T_MAX_MS`) when no room is declared. A take read ungated has no gate floor: a pose at a
  driver within the near-field distance (100 mm, ADR-0360: the room is about 40 dB down), or a take
  whose purpose is the room (room, bass, rear). The exemption keys on that distance, not on the
  driver's name, so a far-field one-driver take is gated.
- **Upper edge:** at a driver within the near-field distance, the near-field limit, where the
  declared cone's ka reaches 1 (`beaming_onset_hz(diameter, ka=1)`, Keele's bound). Elsewhere, the
  far-field ceiling of the largest driver that played, at that distance (`far_field_ceiling_hz`,
  the Rayleigh distance): above every sweep at the mark, binding only up close.
- **A distance in between** is where both edges bind. Each edge is published with the reading and
  with its source (`gate_floor`, `near_field_limit`, `far_field_ceiling`).
- An undeclared input is named, never guessed: no declared room gives the search bound and
  `room_undeclared`; no declared cone size gives no upper edge and `driver_size_undeclared`.
- A reader clips to the band and says so. No reader refuses a take for its band (ADR-0101).

### 4. Two distance models, one owner, and nothing between them

- Far field: the 1/r law. A reading at distance `d` states its level at 1 m as
  `L + 20·log10(d / 1 m)`.
- At a cone: the rigid piston's step between two near-field distances (`piston_step_db`).
- Both live beside the level reader, `jasper/audio_measurement/level.py`. Between the near-field
  distance and the far field there is no correction: a take there is reported inside its two edges.

### 5. The close-reference view retires; two of its helpers stay

- `jasper-round-views close-reference`, `crossover_v2/close_reference.py` and the `close/spot` row
  retire. Distance is a setting of any pose, so a close take is any pose at a short distance, read
  inside its own band. The view's two-distance room subtraction retires with it.
- Two helpers stay, at their owners: the reflection-free window at a distance
  (`DeclaredGeometry.first_bounce_s`, which the view only wrapped) and the 1/r level to 1 m (§4).

### 6. Registry rows become presets over named layouts

- A **preset** is a registry row: a purpose, what plays, and a default layout, plus the layouts it
  offers. A **layout** is a named, ordered pose list; it is never renamed or edited in place
  (ADR-0277). A run names a preset and, optionally, a layout or an inline pose list (ADR-0298). The
  menu offers presets, and each preset's layouts.
- A row that restates another row's program with only a different distance, driver, level or
  layout retires. Its layout stays a named layout. Registry program ids are banked-round
  identities: a retired id is never renamed or reused, and banked rounds that carry one stay
  readable.
- A preset's driver poses name a role and expand to the speaker's declared outputs of that role:
  one woofer on a 2-way or a 3-way, front and rear on a cardioid. A run may narrow them to one
  output.
- Nothing infers topology from a driver count. A cardioid is a declared rear output
  (`cardioid_cabinet_channels`, ADR-0316/0318). A 3-way (woofer, mid, tweeter) stays refused by
  name until [#5396](https://github.com/jaspercurry/JTS/issues/5396).

The rows at `2eeeaf4be`, and where each goes:

| Row | What it is | Where it goes |
|---|---|---|
| `speaker/mark` | speaker · per-driver · room sweep | **Preset.** Layouts `speaker_mark`, `baseline_express`, `baseline_full` |
| `baseline/express`, `baseline/full` | the same program at other layouts | Retire: `speaker/mark` at `baseline_express` / `baseline_full` |
| `tournament/express` | speaker · per-driver | **Preset.** Layouts `tournament_express`, `tournament_full` |
| `tournament/full` | the same program at another layout | Retire: `tournament/express` at `tournament_full` |
| `branches/express` | speaker · both drivers on one clock | **Preset** |
| `front_rear/express` | speaker · front and rear woofer on one clock | **Preset** (a speaker with a rear output only) |
| `rear/express` | rear · summed | **Preset.** Layouts `rear_express`, `rear_wide`, `rear_behind` |
| `rear/wide`, `rear/behind` | the same program at other layouts | Retire: `rear/express` at `rear_wide` / `rear_behind` |
| `rear/seat` | rear · summed · also room evidence (ADR-0336) | **Preset** |
| `rear/pair` | rear · front and rear woofer on one clock | **Preset.** Layouts `rear_express`, `speaker_mark`, `rear_behind` |
| `rear/pair_mark`, `rear/pair_behind` | the same program at other layouts | Retire: `rear/pair` at `speaker_mark` / `rear_behind` |
| `room/seat` | room · summed | **Preset.** Layouts `seat_express`, `seat_cloud`, `seat_cube`, `room_quick` |
| `room/cloud`, `room/arm`, `seat/cube` | the same program at other layouts | Retire: `room/seat` at `seat_cloud` / `room_quick` / `seat_cube` |
| `seat/cloud`, `seat/express` | exact duplicates of `room/cloud` and `room/seat` | Retire |
| `bass/axis` | bass · summed · level ladder · bass stimulus | **Preset.** Layouts `bass_axis`, `seat_cloud`, `room_quick`, and `seat_express` for a hand trial |
| `bass/cloud`, `bass/quick` | the same program at other layouts | Retire: `bass/axis` at `seat_cloud` / `room_quick` |
| `bass/nearfield` | one distance (3 cm), levelled from the 1 m anchor | **Delete first** (owner, #5698), with its layout |
| `close/spot` | one distance (0.3 m) | **Delete first** (owner, #3665), with its layout |
| `nearfield/woofer`, `nearfield/rear`, `nearfield/cardioid` | one near-field program, three driver lists | Retire: one new preset, `nearfield/each`, whose poses at 15 and 30 mm name the woofer role and expand to each declared woofer output |

That leaves ten presets. A one-driver far-field preset follows with #5696.

## Consequences

- A take at any distance, of any driver, of any speaker the conductor admits, is one pose and one
  level rule, and every reader states the band it trusts from the same three declarations. The LLM
  configures a round by choosing a preset, a layout or poses, and drivers; it never computes a band
  or a level.
- 28 rows become 10 presets. Menus shrink, and the tuning runbook's program table is regenerated
  from the registry. A retired id stays readable in banked rounds through one frozen table, since
  web runs persist ids such as `baseline/express` and `seat/cube`.
- The implementation is tracked in [#5737](https://github.com/jaspercurry/JTS/issues/5737): the
  band function and its readers (with #3665 item 10), one pose record, the distance-keyed
  exemption, the far-field probe (#5714), a driver on any pose (#5696), the two moved helpers and
  the close-reference retirement, and the folds.
- The near-to-far transfer and splice stays a laptop aid over banked takes (#5695, ADR-0353). It
  models the cone, not an in-between distance.
- Rejected:
  - A correction model for in-between distances (baffle step, diffraction, radiation impedance per
    driver and cabinet). It is research, not a toolkit feature; such a take is reported inside its
    two edges instead.
  - Keeping the two-distance room subtraction. It needs sub-sample alignment and a cancellation
    budget, and a take's own band already says where the room enters.
  - A preset per distance, per driver or per level, and a fourth pose kind for "in between".
  - A level predicted from another distance or another driver.
  - A driver list read from a driver count.
