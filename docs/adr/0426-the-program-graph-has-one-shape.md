# ADR-0426: The program graph has one shape

- **Date:** 2026-10-03
- **Status:** Accepted. Supersedes in part [ADR-0227](0227-owner-rulings-the-prose-pass-surfaced.md)
  §10: its site, `camilla_yaml._assert_tweeter_crossover_hp_satisfies_floor`, goes.
- **Context:** `emit_active_speaker_program_config` built two shapes. With the confirmed driver
  protection it emits the protected-neutral graph. Without it, it emitted the preset's target
  crossover, applied the crossover regions' polarity in the role-routed mixer, and checked the
  tweeter's crossover high-pass against the floor that ADR-0227 §10 rules on (the corner refuses,
  the slope only discloses). No product path builds the second shape: the emitter's only product
  caller is the measurement graph, and its one caller always passes
  `confirmed_protection_sections(...)`, which returns a dict or refuses
  (`driver_protection_invalid`). This is follow-up 5 on
  [#6226](https://github.com/jaspercurry/JTS/issues/6226).
- **Decision:**
  1. The program emitter always takes the driver protection; its unprotected branch goes.
  2. `_assert_tweeter_crossover_hp_satisfies_floor` goes, and the program gate no longer looks up
     the preset's crossover high-pass: it needs the protective high-pass by name, and refuses a
     graph with tweeter outputs that names none.
  3. The role-routed mixer loses its region-polarity option. Its callers passed it off.
- **Hearing:** the four emitters write the same graphs for every input the product can give. A
  scratch proof replayed 61,916 inputs (every emitter call in the test files that name them, and a
  grid of 56 presets × 4 devices × options) through main and this change: 19,660 graphs
  byte-identical and 42,256 identical refusals. Every graph keeps `devices.volume_limit: 0.0` and
  its Limiter. `set_volume_db`, the graph doors, the 85 dB stop and the driver caps do not change.
- **Consequences:** ADR-0227 §10 has no site left, because no household crossover reaches the
  program graph. The protective high-pass keeps its own floor in the protected path: it refuses a
  declared protection below the code's slope figure, as main already does
  (`test_protected_neutral_emit_refuses_unsafe_tweeter_protection`). Whether that slope should
  only disclose, as §10 says for a crossover, is a separate question for the owner. Rejected:
  keeping the unprotected branch for a later caller, because no caller can reach it.
