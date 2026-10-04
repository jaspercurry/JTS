# ADR-0448: The DAC8x base and Studio driver stacks are one DAC family

- **Date:** 2026-10-04
- **Status:** Accepted
- **Supersedes:** [ADR-0232](0232-studio-driver-stack-is-canonical-for-hifiberry-studio-silicon.md)
  (the Studio stack as canonical, and its Phase 1 move plan). Supersedes
  (partial): [ADR-0106](0106-a-verification-artifact-is-never-migrated-in-place.md)'s
  "an identity is never migrated in place", for this one same-silicon pair;
  and [ADR-0234](0234-detected-hardware-is-used-automatically.md)'s collision
  refusal, for a hand-written overlay of the detected HAT's own family.

## Context

jts3 is a HiFiBerry DAC8x Studio board on the base overlay
`dtoverlay=hifiberry-dac8x` (driver `snd_rpi_hifiberry_dac8x`, row
`hifiberry_dac8x`). Every measured DAC8x value (S32_LE, the 128/256 floor, the
chip-AEC approval) was measured on this Studio silicon under the base driver.
ADR-0232 made the Studio stack canonical and planned a move with a full
re-proof: new registry values, a topology re-save, a tune re-apply, a soak and a
chip-AEC re-commission. The move never ran. Meanwhile the Studio row stayed
unmeasured (S16_LE, no floor, chip-AEC uncalibrated), so a ring box on the
Studio driver parked, and the reconciler logged
`hardware.i2s_hat_boot_config_conflict` on every jts3 pass. The two stacks drive
the same chips on the same I2S pins, with the Pi as the clock source; the Studio
stack only adds an I2C link to the board's MCU, which reports the formats and
adds a volume and mute stage. The owner (2026-10-04): support both, and do not
redo the proofs when almost nothing changes.

## Decision

1. **One set of measured values.** The Studio row is derived from the base row
   and takes all its runtime values. It keeps only what is Studio-only: how the
   board is recognized (its card names, and the EEPROM gate of #2258), its own
   overlay, and its mixer pins. The Studio driver's gain stages (up to +24 dB)
   stay pinned at 0 dB and unmuted whenever that driver runs.
2. **One identity.** A registry row may name a `family_id`: the row it is the
   same silicon as. State stored against the DAC names the family row
   (`output_identity_id`): the saved topology's DAC match in the doctor and in
   `declared_hardware_mismatch`, the doctor's boot-overlay check, and the
   chip-AEC alignment identity's `output_id`. The classifier still picks the
   Studio row on the Studio driver: that row picks the pins and the overlay.
3. **Both overlays are supported.** One hand-written overlay of the detected
   HAT's family, with no JTS block, already runs the board: the reconciler
   writes nothing, reports no conflict, and takes that overlay's row as the
   desired profile, so no reboot is owed. Two I2S drivers are still refused. A
   fresh Studio board with no DAC8x overlay still gets the Studio overlay
   (ADR-0234), because that is the least code.
4. **A move between the overlays is one boot line and a reboot.** No topology
   re-save, no tune re-apply, no soak, no chip-AEC re-commission.

## Consequences

- jts3 on the base overlay: only the conflict warning changes (it stops). Its
  row, topology fingerprint, emitted graph and chip-AEC identity are
  byte-identical.
- A switch to the Studio overlay needs the one line, a reboot, and a silent
  check: outputd runs at S32_LE, and the doctor shows the pins at 0 dB.
- The one unknown: the S32_LE probe ran under the base driver only. If the
  Studio board's MCU does not offer S32_LE, outputd parks at exit 78 (it never
  converts silently), and the box goes back to the base overlay.
- One residual: the chip-AEC per-unit key `output_hardware_key` holds the ALSA
  card id, which the two drivers spell differently. A per-unit artifact banked
  on one stack is applied on the other with a "measured on a different unit"
  disclosure (ADR-0101), not a park. A shipped class row matches on both stacks.
- Deleted as dead: the soak tool's DAC-identity gate, and the doctor's
  floorless-DAC branch (no registered row is floorless now).
- Every other DAC row is its own one-row family, so its values, detection,
  runtime plan, doctor result and reconciler verdict do not change.
- Rejected: running ADR-0232's move (a long re-proof for almost no change), and
  deleting the Studio row (it is how the Studio driver gets its gain pins on
  rpi-6.18.y).
