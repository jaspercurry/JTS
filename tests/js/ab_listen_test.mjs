// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import assert from 'node:assert/strict';
import { h, loadEsm, repoPath } from './_loader.mjs';
let live = {percent: 50, muted: true};
const calls = [];
const card = {children: [], replaceChildren(...nodes) { this.children = nodes; },
  addEventListener(_event, fn) { this.click = fn; }};
globalThis.__abTest = {
  h, document: {getElementById: () => card},
  getJSON: async path => path === '/volume' ? live : {
    applied_fingerprint: 'a', apply_path: '/apply', volume_step_db: 0.5,
    rounds: [{round_id: 'round', tunes: [
      {fingerprint: 'a', base: true, level_db: -20}, {fingerprint: 'b', level_db: -19}]}]},
  postJSON: async (path, body) => { calls.push([path, body]); return {status: 'applied'}; }
};
const {trimSteps, targetPercent, flipOrder, startKey, canStart, initAbListen} = await loadEsm(
  repoPath('deploy/assets/sound-profile/js/ab-listen.js'), {stripImports: true,
    prelude: 'const {h, document, getJSON, postJSON} = globalThis.__abTest;'});
for (const [level, other, expected] of [[-19, -20, -2], [-20, -19, 0], [-20, -20, 0], [-19.8, -20, 0]]) {
  assert.equal(trimSteps(level, other, 0.505) || 0, expected);
}
assert.equal(targetPercent(2, -5), 1);
assert.equal(targetPercent(50, -2), 48);
for (const [blind, random, key] of [[false, 0, 'A'], [false, 0.9, 'A'], [true, 0, 'A'], [true, 0.9, 'B']]) {
  assert.equal(startKey(blind, random), key);
}
assert.deepEqual(flipOrder({percent: 50, fingerprint: 'a'}, {percent: 50, fingerprint: 'a'}, true), ['apply']);
for (const [percent, muted, steps, expected] of [[50, true, [0, -1], false], [2, false, [0, -1], false],
  [3, false, [0, -1], true], [3, false, [-2, 0], false], [2, false, [0, 0], true]]) {
  assert.equal(canStart({percent, muted}, steps), expected);
}
for (const [percent, fp, expected] of [[48, 'b', ['volume', 'apply']], [52, 'b', ['apply', 'volume']],
  [50, 'b', ['apply']], [48, 'a', ['volume']], [50, 'a', []]]) {
  assert.deepEqual(flipOrder({percent: 50, fingerprint: 'a'}, {percent, fingerprint: fp}), expected);
}
const nodes = (node = card) => [node, ...(node.children || []).flatMap(nodes)];
const toggle = () => nodes().find(n => n.tag === 'input');
const click = act => card.click({target: {closest: () => ({dataset: {act}})}});
initAbListen();
await click('setup');
assert.equal(toggle().props.disabled, false);
await click('start');
assert.deepEqual(calls, []);
assert.ok(nodes().some(n => n.tag === 'p.banner.banner--danger'));
live = {percent: 3, muted: false};
await click('start');
assert.deepEqual(calls, []);
live.percent = 50;
const random = Math.random;
Math.random = () => 0;
try {
  toggle().props.onchange({target: {checked: true}});
  assert.ok(!nodes().some(n => n.tag === 'select'));
  await click('start');
} finally { Math.random = random; }
assert.deepEqual(calls.splice(0), [['/apply', {expected_candidate_fingerprint: 'a'}]]);
assert.ok(nodes().some(n => n.tag === 'span.badge.badge--ok' && n.props.role === 'status'));
assert.equal(toggle().props.disabled, false);
toggle().props.onchange({target: {checked: false}});
assert.equal(toggle().props.disabled, true);
live.percent = 40;
await click('flip');
assert.deepEqual(calls.splice(0), [['/volume/set', {percent: 38}], ['/apply', {expected_candidate_fingerprint: 'b'}]]);
live.percent = 35;
await click('end');
assert.deepEqual(calls.splice(0), [['/apply', {expected_candidate_fingerprint: 'a'}], ['/volume/set', {percent: 37}]]);
assert.equal(toggle().props.disabled, false);
assert.equal(toggle().props.checked, false);
console.log('A/B listen rules and session passed');
