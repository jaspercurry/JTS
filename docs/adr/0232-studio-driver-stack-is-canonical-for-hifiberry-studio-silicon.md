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
  documented config: S32_LE has been declared on it since early August (one
  three-window soak, plus daily use since), zero DAC xruns observed. On
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
  `eeprom_gated_card_matches` fields on the Studio row that consume it.
  PR #3922 generalizes the I2S HAT intent selector beyond InnoMaker so
  `hifiberry_dac8x_studio` can be set through it; Phase 1 step 3 below
  depends on that PR having merged. The Studio hardware's own gain stage
  is pinned at 0 dB / unmuted via the
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
  3. Set the I2S HAT intent to `hifiberry_dac8x_studio` and deploy (needs
     PR #3922). Manual fallback if #3922 has not merged: after deleting the
     base line in step 2, hand-edit `config.txt` to add
     `dtoverlay=hifiberry-studio-dac8x` directly.
  4. Reboot; expect the park on a roleful box only (flat boxes don't park):
     the Studio row declares no `latency_floor`, so `latency_floor_for`
     returns `None`, the ring conf.d is not rendered, and
     `active_ring_endpoint_proof` fails (#2575).
  5. Run silent probes; confirm the hardware volume pin took effect.
  6. Record the probe results (format, `hw-params`) in the PR that declares
     them — there is no provisional/validated state on `DacProfile` itself,
     so nothing in the registry changes yet.
  7. One-off full-payload topology save that keeps the existing speaker
     groups.
  8. Soak via
     `jasper-audio-hw-validate --profile hifiberry_dac8x_outputd_stability`.
  9. Recheck the chip-AEC delay.
  10. One registry change lands the soak's outcome: flip
      floors/format/commissioning/chip-AEC on the Studio row together, and
      update the floorless-DAC contract tests in the same PR.

  **Rollback:** restore the `config.txt` overlay line and the topology
  backup, reboot.
- **Consequences:** The base row's evidence prose now says what silicon and
  driver stack it was measured on, so a future reader does not need to
  reconstruct that jts3 was Studio hardware from git history. The Studio
  row stays floorless — no measured floor, format, commissioning, or
  chip-AEC — and the doctor/floorless-DAC contract tests that assert that
  keep passing, until step 10's single registry change lands and updates
  them together. jts3 loses its months of base-row soak history at the driver
  level when it migrates — ADR-0106 already establishes that verification
  artifacts don't migrate in place, so the Studio row starts its own
  evidence base from Phase 1 rather than inheriting the base row's. Until
  Phase 1 completes, a Studio board configured with the base overlay (the
  vendor default) keeps classifying into the base row and inherits its
  S32_LE and approved chip-AEC — that is accepted vendor-config behavior,
  not a defect to route around.
