// Unit tests for the pure active-speaker level-match UI helpers.
//
// active-speaker-ui.js is a dependency-free ES module, so node can import it
// directly (no harness/DOM stubbing needed). Run via test_sound_setup.py.
import assert from "node:assert/strict";

import {
  NEARFIELD_LEVEL_MATCH_GUIDANCE,
  levelMatchSummary,
  nearfieldCaptureHint,
} from "../../deploy/assets/sound-profile/js/active-speaker-ui.js";

// Measured override: each driver's trim is "Measured", config is not provisional.
{
  const s = levelMatchSummary({
    corrections: { woofer: { gain_db: 0 }, tweeter: { gain_db: -18 } },
    corrections_source: { woofer: "measured", tweeter: "measured" },
    provisional: false,
  });
  assert.equal(s.available, true);
  assert.equal(s.provisional, false);
  assert.equal(s.rows.length, 2);
  assert.equal(s.rows[0].role, "woofer");
  assert.equal(s.rows[1].role, "tweeter");
  assert.equal(s.rows[1].trimDb, -18);
  assert.equal(s.rows[1].sourceLabel, "Measured");
}

// Provisional datasheet fallback: tweeter trim flagged a datasheet estimate, and
// the near-field guidance is surfaced.
{
  const s = levelMatchSummary({
    corrections: { woofer: { gain_db: 0 }, tweeter: { gain_db: -25.2 } },
    corrections_source: { woofer: "none", tweeter: "sensitivity" },
    provisional: true,
  });
  assert.equal(s.provisional, true);
  assert.equal(s.rows[1].sourceLabel, "Datasheet estimate");
  assert.ok(s.guidance.includes("2–5 cm"));
  assert.ok(/datasheet estimates/i.test(s.note));
  // The datasheet fallback is framed as a fine, optional-to-improve state.
  assert.ok(/optional/i.test(s.note));
}

// Blocked / empty baseline payloads render nothing.
assert.equal(levelMatchSummary({}).available, false);
assert.equal(levelMatchSummary(null).available, false);
assert.equal(levelMatchSummary({ corrections: {} }).available, false);

// Near-field copy — the level match is OPTIONAL and the copy must say so.
assert.ok(nearfieldCaptureHint("Tweeter").includes("Tweeter"));
assert.ok(nearfieldCaptureHint("Tweeter").includes("2–5 cm"));
assert.ok(/optional/i.test(nearfieldCaptureHint("Tweeter")));
assert.ok(NEARFIELD_LEVEL_MATCH_GUIDANCE.includes("2–5 cm"));
assert.ok(/optional/i.test(NEARFIELD_LEVEL_MATCH_GUIDANCE));
assert.ok(/skip/i.test(NEARFIELD_LEVEL_MATCH_GUIDANCE));

console.log(JSON.stringify({ ok: true }));
