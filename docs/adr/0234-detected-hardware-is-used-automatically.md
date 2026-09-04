# ADR-0234: Detected hardware is used automatically; only undetectable hardware gets a toggle

- **Date:** 2026-09-04
- **Status:** Accepted
- **Context:** The generalized I2S HAT intent selector offered a `<select>`
  over every registered I2S profile plus a "Use detected" button, so an
  operator who fitted a HAT that already declares its own identity in its ID
  EEPROM still had to name it in the wizard, and the wizard carried two
  competing answers (saved intent, detected suggestion) to one question.
  PR #3917 added the EEPROM reader and the registry's `hat_products`
  declaration; the boot-config reconciler read only the intent file.
  Meanwhile the HATs JTS supports split cleanly: HiFiBerry's Studio DAC8x
  publishes `vendor=HiFiBerry`/`product=StudioDAC8x`, and the InnoMaker HiFi
  AMP Pro publishes no ID EEPROM at all.
- **Decision:** Hardware JTS can detect is applied without an operator step.
  `reconcile_boot_config` resolves the desired I2S HAT profile in one order:
  the registry profile whose `hat_products` claims the fitted HAT's EEPROM
  product; else the per-box intent file's profile; else nothing, and the boot
  config is not touched at all. Hardware JTS cannot detect keeps exactly one
  control: the wizard's `<select>` lists only the I2S profiles that declare
  no `hat_products`, plus "None / unmanaged". There is no save/restore/undo
  machinery, no detected-suggestion hint and no "use detected" action — a
  detected HAT is reported read-only ("Detected: <label> — managed
  automatically") and the picker is not rendered. The refusal PR #3922
  introduced is unchanged: when any registered I2S overlay is hand-written
  outside JTS's managed block, nothing is written and the collision is
  reported instead.
- **Consequences:** An EEPROM-bearing HAT needs no wizard step on any box —
  fit it, boot, and the managed `dtoverlay=` block follows the silicon.
  The wizard shrinks to the one case that needs a human. A box carrying a
  hand-written overlay line keeps it: jts3, whose `config.txt` names the base
  `dtoverlay=hifiberry-dac8x` by hand, still gets nothing written and reports
  the collision, so
  [ADR-0232](0232-studio-driver-stack-is-canonical-for-hifiberry-studio-silicon.md)'s
  Phase 1 reduces to deleting that hand-written line and deploying — this
  supersedes that ADR's Phase 1 step 3 (setting the intent to
  `hifiberry_dac8x_studio`), which is no longer needed or offered. An intent
  file that names a detectable profile is inert while that HAT is fitted:
  detection wins, and a stale saved intent can no longer contradict the
  hardware in the slot. The cost is that an operator cannot override
  detection from the wizard; the escape hatch is physical (fit different
  hardware) or a hand-written overlay line, which JTS then refuses to
  compound.
