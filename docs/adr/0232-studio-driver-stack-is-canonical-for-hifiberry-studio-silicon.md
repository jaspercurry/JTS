# ADR-0232: Studio driver stack is canonical for HiFiBerry Studio silicon

- **Date:** 2026-09-04
- **Status:** Accepted
- **Context:** jts3 (lab Pi 5) carries a HiFiBerry DAC8x Studio (HAT EEPROM
  vendor=HiFiBerry, product=StudioDAC8x) but has always run the BASE overlay
  `hifiberry-dac8x` — the config HiFiBerry's own datasheet prescribes — so it
  loads driver `snd_rpi_hifiberry_dac8x` and classifies as JTS profile
  `hifiberry_dac8x`. Every measured value on that row (S32_LE, the 128/256
  and 256/1536 floors, active-crossover commissioning, chip-AEC approval) was
  therefore measured on Studio silicon under the base driver, not on a
  fictional non-Studio DAC8x — but the row's prose attributed it to "JTS3
  known-good" without saying so, and the Studio row's prose called this
  configuration a "misroute" needing correction, when it is the vendor-
  documented config and has run S32_LE for months at zero xruns. On
  rpi-6.18.y and later, the kernel makes the Studio driver stack
  (`hifiberry-studio-dac8x` → `hifiberry_studio.c`) unroutable on its own:
  every board in the Studio family presents the single card name "Hifiberry
  Studio Soundcard", carrying no DAC8x token and no width, so JTS cannot tell
  a Studio DAC8x from a Studio Digi/AES by label alone. Issue #2575 tracks
  the discriminator gap; ADR-0106 records that verification artifacts never
  migrate in place; ADR-0190 excludes `final_edge_format` from chip-AEC
  identity comparison, which is what let jts3 run the base row's S32_LE for
  months without nagging.
- **Decision:** The Studio driver stack is canonical for Studio silicon.
  `hifiberry-studio-dac8x` / profile `hifiberry_dac8x_studio` is the row a
  HiFiBerry Studio DAC8x should classify into; the base row
  (`hifiberry-dac8x` / `hifiberry_dac8x`) is retained as-is for genuine base
  boards and for the vendor-documented base config, keyed by driver stack
  (overlay → driver → card label), not by silicon identity. jts3 migrates to
  the Studio stack in Phase 1, with the owner present. The HAT EEPROM
  product string (`vendor`/`product`, e.g. `HiFiBerry`/`StudioDAC8x`) is the
  discriminator that closes the 6.18 label collision for the unified
  "Hifiberry Studio Soundcard" name; PR #3917 adds the `hat_products` /
  `eeprom_gated_card_matches` fields on the Studio row that consume it. The
  Studio hardware's own gain stage is pinned at 0 dB / unmuted via the
  registry's `mixer_controls`; PR #3924 adds that pin. The Studio row keeps
  the global default floors/format/commissioning/chip-AEC (no per-board
  measurement) until the jts3 Studio soak below runs; the removal condition
  is stated beside those defaulted fields: **flip
  floors/format/commissioning/chip-AEC on the Studio row after the jts3
  Studio soak.**

  **Phase 1 migration (jts3, owner present):**
  1. Back up `config.txt`, `output_topology.json`, `outputd.env`, and
     `asound.state`.
  2. Delete the hand-written base `dtoverlay=hifiberry-dac8x` line from
     `config.txt`.
  3. Set the I2S HAT intent to `hifiberry_dac8x_studio` and deploy.
  4. Reboot; expect the park (no measured floor/format yet on this row).
  5. Run silent probes; confirm the hardware volume pin took effect.
  6. Declare provisional floors/format on the Studio row from the probe
     results.
  7. One-off full-payload topology save that keeps the existing speaker
     groups.
  8. Soak via `jasper-audio-hw-validate hifiberry_dac8x_outputd_stability`.
  9. Recheck the chip-AEC delay.
  10. Flip the Studio row from provisional to validated.

  **Rollback:** restore the `config.txt` overlay line and the topology
  backup, reboot.
- **Consequences:** The base row's evidence prose now says what silicon and
  driver stack it was measured on, so a future reader does not need to
  reconstruct that jts3 was Studio hardware from git history. The Studio
  row's provisional status is explicit and load-bearing rather than
  aspirational — floors/format/commissioning/chip-AEC on that row stay
  defaulted, and the doctor/floorless-DAC contract tests that assert the
  Studio row ships no floor keep passing, until the soak above lands and
  flips them. jts3 loses its months of base-row soak history at the driver
  level when it migrates — ADR-0106 already establishes that verification
  artifacts don't migrate in place, so the Studio row starts its own
  evidence base from Phase 1 rather than inheriting the base row's. Until
  Phase 1 completes, a Studio board configured with the base overlay (the
  vendor default) keeps classifying into the base row and inherits its
  S32_LE and approved chip-AEC — that is accepted vendor-config behavior,
  not a defect to route around.
