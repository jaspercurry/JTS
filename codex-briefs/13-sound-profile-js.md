# Brief 13 — sound-profile JS: fix the state-loss bug, guard the wizard, split safely

Mission: review §5.1 + the JS dive (rated 6/10, the weakest shipped surface).
deploy/assets/sound-profile/js/main.js is 4,712 lines: an excellent EQ editor
plus a ~3k-line active-crossover wizard with a REAL bug and no guards. Bugs
first, split second, and the EQ half moves verbatim or not at all (its
interactions are hardware-verified; the file header's "do not blind-refactor"
warning is binding).

Branch: `codex/sound-profile-js`. File fence: `deploy/assets/sound-profile/**`,
`tests/js/sound_profile_harness.mjs` (+ new harness files),
`deploy/lib/install/web-assets.sh` only if the js/ file list is hardcoded
(check — it glob-discovers; likely no edit). Python side untouched.

## PR 1 — defects + guards (each pinned by the node harness)

1. **State-loss bug:** 38 hand-copied full-object `activeSpeaker` rebuilds;
   three success paths (path-check/load/rollback, ~4232/4301/4375 at review
   time) drop the `rehearsal` field, silently wiping commissioning evidence
   back to "not checked". Add one `patchActiveSpeaker(patch)` helper
   (Object.assign semantics), replace ALL 38 literals, and add a harness test
   asserting every action path preserves unrelated fields (especially
   rehearsal).
2. **Concurrency guards for the wizard half:** the calibration-level slider
   fires overlapping POSTs with no seq token or in-flight gate (last response
   wins, stale UI). Reuse the EQ half's proven seq-token pattern
   (previewSeq/liveSourceSeq) or disable controls while an action is
   in-flight. Harness test: two interleaved responses, stale one discarded.
3. **9-fetch serial waterfall, all-or-nothing:** `refreshActiveSpeakerStatus`
   awaits 9 GETs sequentially and one failure nulls everything. Switch to
   `Promise.allSettled` with per-probe degradation (keep successful
   sections). Harness test: one failing probe, eight sections still render.
4. Nits in the same files: name the bare `0.05` active-gain epsilon
   (appears 5×) as a const; fix the dead ternary (`s.type === 'Peaking' ? 1.0
   : 1.0`) to its intended value or a plain `1.0` with a comment.
5. Extend `tests/js/sound_profile_harness.mjs` with active-speaker fetch
   stubs (today it throws 'unexpected fetch' for ./active-speaker/* — zero
   wizard coverage). The new `js` CI job (#635) runs the harness — keep it
   green.

## PR 2 — the split (active-speaker half only)

Per the file's own header plan, but conservatively: extract
`active-speaker/views.js`, `active-speaker/actions.js`, and a shared
`store.js`/`api.js` from the wizard half. **Leave the EQ editor + profile
library code in main.js verbatim** (it is hardware-validated; moving it is a
separate, on-device-verified follow-up — say so in the PR body and update the
file header's plan note). Imports by relative path per the repo's ES-module
convention; `node --check` everything; nginx serves js with no-cache so no
cache-busting work needed. Update the header comment to reflect the new
layout and the remaining EQ-half follow-up.

Acceptance: node harness green locally and in CI's js job; `ruff check .`
untouched-but-run; no Python diffs; PR 1 body lists each bug with its
file:line diagnosis on current main; docs-impact: HANDOFF-management-ui +
HANDOFF-sound-preferences route here — note the layout change.
