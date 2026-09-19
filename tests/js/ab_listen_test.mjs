// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import assert from 'node:assert/strict';
import { loadEsm, repoPath } from './_loader.mjs';
const {trimSteps, targetPercent, flipOrder} = await loadEsm(
  repoPath('deploy/assets/sound-profile/js/ab-listen.js'), {stripImports: true});
for (const [level, other, expected] of [[-19, -20, -2], [-20, -19, 0], [-20, -20, 0], [-19.8, -20, 0]]) {
  assert.equal(trimSteps(level, other, 0.505) || 0, expected);
}
assert.equal(targetPercent(2, -5), 1);
assert.equal(targetPercent(50, -2), 48);
for (const [percent, fp, expected] of [[48, 'b', ['volume', 'apply']], [52, 'b', ['apply', 'volume']],
  [50, 'b', ['apply']], [48, 'a', ['volume']], [50, 'a', []]]) {
  assert.deepEqual(flipOrder({percent: 50, fingerprint: 'a'}, {percent, fingerprint: fp}), expected);
}
console.log('A/B listen rules passed');
