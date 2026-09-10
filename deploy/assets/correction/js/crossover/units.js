// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Page-local metric/imperial preference for the crossover walk (#3629, #1941
// Q2: "per-device, page-local"). Every walk distance already carries BOTH
// units in one string -- jasper.active_speaker.crossover_v2.capture_plan's
// format_position_distance emits "NN in (MM cm)" so neither unit is ever
// hidden (#1805) -- so this only reorders which one leads; it never hides
// either, and it never touches what the session's evidence sidecar records
// (that is the server's own text, read separately from this display pass).

const STORAGE_KEY = "jts-crossover-units";
export const UNIT_METRIC = "metric";
export const UNIT_IMPERIAL = "imperial";

function readStored() {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return value === UNIT_METRIC || value === UNIT_IMPERIAL ? value : null;
  } catch (_) {
    return null; // private mode / storage disabled -- fall back to the default
  }
}

function writeStored(unit) {
  try {
    localStorage.setItem(STORAGE_KEY, unit);
  } catch (_) {
    /* private mode / storage disabled -- the in-memory preference still works
       for the rest of this page view */
  }
}

// Matches format_position_distance's exact output ("NN in (MM cm)"),
// anywhere it appears inside a larger sentence.
const DISTANCE_RE = /(-?[\d.]+)\s*in\s*\((-?[\d.]+)\s*cm\)/g;

let current = readStored() || UNIT_IMPERIAL;

export function currentUnits() {
  return current;
}

export function setUnits(unit) {
  current = unit === UNIT_METRIC ? UNIT_METRIC : UNIT_IMPERIAL;
  writeStored(current);
}

// Reorders "NN in (MM cm)" to "MM cm (NN in)" under the metric preference;
// imperial (the default, and format_position_distance's own order) passes
// text through unchanged.
export function formatDistances(text) {
  if (current !== UNIT_METRIC || !text) return text;
  return text.replace(DISTANCE_RE, (match, inches, cm) => `${cm} cm (${inches} in)`);
}
