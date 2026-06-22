/*
 * SPDX-FileCopyrightText: 2026 Jasper Curry
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include "status.h"

// SH8601 AMOLED bring-up + connection-status indicator. Phase 1.2:
// no LVGL — direct GFX draws are sufficient for a colored circle +
// label. Adding LVGL later requires a flush_cb against the same
// Arduino_GFX surface; the panel + bus init here doesn't change.
//
// All functions assume:
//   - Wire.begin(SDA, SCL) was called before displayInit() (the
//     TCA9554 reset poke happens over I²C on the shared bus).
//   - Caller is in the Arduino loop / setup task — no thread safety.

// Bring up the TCA9554 expander, release the SH8601 from reset over
// the expander, init the QSPI bus + panel, clear to black, set a
// modest brightness. Returns false on TCA9554 init failure (panel
// won't come up if the expander didn't ACK). SH8601 init failures
// inside Arduino_GFX are not exposed by its Arduino-style begin()
// API; assume optimistic on a true return.
bool displayInit();

// Draw the status indicator for `s`: colored circle in the centre,
// short label below. Cheap to call repeatedly with the same value
// (caller dedupes) but no internal change-detection.
void displayShowStatus(Status s);
