# ADR-0446: A declared tweeter protection slope discloses; the corner still refuses

- **Date:** 2026-10-04
- **Status:** Accepted. Supersedes in part [ADR-0426](0426-the-program-graph-has-one-shape.md):
  its Consequences sentence that the protective high-pass "refuses a declared protection below the
  code's slope figure". The rule of [ADR-0227](0227-owner-rulings-the-prose-pass-surfaced.md) §10
  has a site again, against a different constant (Decision 3).
- **Context:** Since ADR-0426 the program graph plays the confirmed driver protection. Its emitter
  refused a tweeter protection high-pass under 24 dB/octave (`order * 6`), so a declared
  12 dB/octave protection (order 2) at a legal corner could not play. No datasheet contains the
  24: it is a code figure. ADR-0227 §1 lets a code figure prefill, disclose and serve as a
  fallback, never refuse a declaration. §10 rules that the tweeter corner refuses and the slope
  only discloses. ADR-0426 left the protection's slope to the owner, who ruled on 2026-10-04 (Q1 on
  [#5925](https://github.com/jaspercurry/JTS/issues/5925)): the slope only discloses, and the
  corner still refuses.
- **Decision:**
  1. The program emitter plays the declared tweeter protection high-pass as declared. A slope under
     `driver_protection.PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE` (24 dB/octave) logs
     `event=active_speaker.program_emit_gate result=tweeter_hp_slope_below_commissioning_floor` at
     WARNING, the line that ADR-0227 §10's crossover site logged before ADR-0426, and the graph is
     emitted. This path has no other disclosure channel. The emitted graph records the declared
     order in the protection filter.
  2. The corner still refuses. A tweeter protection high-pass under
     `graph_safety.TWEETER_PROTECTIVE_HP_MIN_CORNER_HZ` (400 Hz) logs
     `result=blocked_tweeter_protection_below_floor` at ERROR and raises, at any slope.
  3. `camilla_yaml.gates.PROGRAM_PROTECTIVE_HP_MIN_SLOPE_DB_PER_OCTAVE` and the emitter's
     `protective_hp_min_slope_db_per_octave` parameter go. No caller passed the parameter. The
     disclosure reads `PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE`, the build's one commissioning slope
     figure, which already prefills the derived protection and the crossover preview's disclosure.
- **Hearing:** a scratch proof replayed 52,845 inputs through main and this change. 3,361 are
  every call to the four emitters and to seven protection and proof functions in 403 test files.
  49,484 are a grid of 32 presets × 4 devices × options, with every tweeter high-pass corner from
  300 to 5000 Hz at orders 1, 2, 3, 4, 6 and 8. 46,313 give byte-identical graphs or results, or
  identical refusals. The 6,532 that differ all give the program emitter a tweeter high-pass under
  24 dB/octave at a corner of 400 Hz or more, which main refused. Now order 2 (3,064) and order 3
  (1,734) emit the declared high-pass exactly, wired with the tweeter limiter, and log one
  disclosure; order 1 (1,734) refuses at `tweeter_guard_present`. A corner under 400 Hz refuses at
  every order, as before. Every graph keeps `devices.volume_limit: 0.0` and its Limiters.
  `set_volume_db`, the graph doors, the 85 dB stop and the driver caps do not change.
- **Consequences:**
  - No other check changes. The program graph plays in the drivers scope, whose admission
    (`program_admission.readmit_program_from_wav`) does not read the graph;
    `protection_requirement_present` checks only the tuning graphs. The program's high-pass meets
    the confirmed requirement because `branch_chain.confirmed_protection_sections` builds it so:
    the smallest of orders 2, 4 and 8 whose slope meets the declared slope.
    `tweeter_guard_present` (order 2 or more) and the active verifier (corner 400 Hz or more,
    orders 2, 4 and 8) do not change.
  - Every confirmed protection today comes from the driver safety profile.
    `driver_protection.apply_driver_low_limit` derives a tweeter's protective high-pass, a typed
    one included, at the published slope raised to 24 dB/octave (a prefill, ADR-0227 §1). So a
    tweeter that publishes 12 dB/octave, as B&C does for the DE250, still plays an LR4
    protection, and no product graph changes today. Playing it at LR2 needs a change to that
    prefill (`DriverLowLimit.derived_protection_slope_db_per_octave`): an owner call, not this
    decision.
  - A Linkwitz-Riley order is even. `CrossoverSection` documents 2, 4 or 8, and
    `confirmed_protection_sections` builds only those. In the program emitter, only the slope
    figure refused orders 1 and 3. Now order 1 fails `tweeter_guard_present`, and order 3 is
    emitted as given, as orders 5 and 7 already were.
  - Rejected: a disclosure field in the graph or the admission record. Neither has one, and a new
    field is a new channel for the fact that one log line carries.
