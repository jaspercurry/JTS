# Third-party attribution — firmware

Vendored / third-party source and asset files checked into `firmware/`,
with their upstream and the actual license status as best determined.

This is the firmware-scoped companion to the repo-root
[`LICENSE-third-party.md`](../LICENSE-third-party.md); the rows here
expand the firmware entries in that inventory with the upstream-license
lookups that were previously marked "needs verification".

Last reviewed: 2026-06-12.

> **No vendored third-party files remain in `firmware/`** as of
> 2026-06-12. The two components previously inventoried here were
> removed from the tree rather than cleared — see "Removed components"
> below. If a vendored third-party file is added under `firmware/`
> again, list it here with upstream and license status before merging.

## Vendored components

None currently — the last two entries (the ELECROW CST816D touch
driver and the SquareLine-generated volume-gauge images) were removed
on 2026-06-12; see below.

## Removed components

- 2026-06-12: Removed the four SquareLine-generated factory volume
  gauge images (`dial/src/assets/ui_img_*.c`) and the declaration
  header (`dial/src/assets/factory_volume.h`). The dial firmware now
  renders the volume gauge procedurally with LVGL arcs, labels, and
  simple geometric shapes, so it no longer redistributes those assets.

- 2026-06-12: Removed the vendored ELECROW CrowPanel CST816D touch
  driver (`dial/src/CST816D.h`, `dial/src/CST816D.cpp`) because
  ELECROW's repository declares no redistribution license. Touch input
  remains disabled; future touch support needs a permissively licensed
  bounded-retry driver or a small clean-room I2C implementation.

## Notes on adjacent (NOT vendored) files

These cite the ELECROW factory source for hardware facts but are
JTS-original work, so they are not listed as third-party above:

- `dial/include/config.h` — JTS-authored pin/layout constants. The pin
  numbers are taken from the CrowPanel factory firmware (cited inline);
  pin mappings are hardware facts, not copyrightable expression.
- `dial/src/display.cpp`, `dial/src/scenes.cpp`, `dial/src/main.cpp`,
  `dial/include/lv_conf.h`, `dial/platformio.ini`, `dial/build.sh` —
  JTS-original firmware that references the CrowPanel/LVGL upstreams in
  comments but copies no upstream source.

Build-time library dependencies pulled by each `platformio.ini` (LVGL,
LovyanGFX, FastLED, ArduinoJson, Improv Wi-Fi, Arduino_GFX, the
Arduino-ESP32 / pioarduino toolchain) are tracked in the repo-root
[`LICENSE-third-party.md`](../LICENSE-third-party.md), not here — those
are fetched at build time, not vendored into `firmware/`.

## Follow-up

- If ELECROW later publishes an explicit license for the removed driver
  or factory artwork, a future change may re-evaluate vendoring; record
  the license here and add matching `SPDX-License-Identifier` headers
  if that happens.
