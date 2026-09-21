// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import assert from 'node:assert/strict';
import { test } from 'node:test';
import { buildFunction, repoPath } from './_loader.mjs';
import { element } from './_dom.mjs';

const nodes = node => [node, ...(node.children || []).flatMap(nodes)];
const text = node => nodes(node).map(n => n.textContent || '').join('');
const createElement = tag => Object.assign(element(tag), {
  appendChild(node) { this.children.push(node); node.remove = () => this.children.splice(this.children.indexOf(node), 1); },
  querySelectorAll(selector) { return nodes(this).filter(n => selector === 'button' && n.tag === 'button'); },
  querySelector(selector) { return nodes(this).find(n => selector === '[role="alert"]' && n.role === 'alert'); }
});
globalThis.Node = class { static [Symbol.hasInstance](value) { return !!value?.appendChild; } };
const document = {
  createElement, createTextNode: textContent => ({textContent}),
  querySelector: () => ({content: 'test-csrf'})
};
const init = buildFunction([
  'deploy/assets/shared/js/dom.js', 'deploy/assets/shared/js/http.js',
  'deploy/assets/sound-profile/js/cardioid-compare.js'
].map(repoPath), {stripImports: true, stripExports: true, strictImports: true,
  params: ['document', 'fetch'], returns: ['initCardioidCompare']});
const block = (state = 'normal') => ({
  available: true, reason: '', state,
  tune: {label: 'Current tune', layers: ['Rear', 'Bass', 'Driver'], applied_at: '2026-09-20T00:00:00Z', fingerprint: 'abcdef1234567890'},
  level_match: {status: 'matched', trim_db: 2.5, louder: 'on'},
  expires_in_s: state === 'normal' ? null : 121
});
const flush = () => new Promise(resolve => setImmediate(resolve));
function setup(initial = block(), reply = async () => block('off'), mounted = true) {
  let card;
  const calls = [];
  const {initCardioidCompare} = init(document, async (path, options = {}) => {
    calls.push({path, ...options, body: options.body && JSON.parse(options.body)});
    const data = await (options.method === 'POST' ? reply() : initial);
    return {ok: !data.error, status: data.error ? 409 : 200, json: async () => data};
  });
  initCardioidCompare(mounted ? {after: node => { card = node; }} : null);
  const button = state => nodes(card).find(n => n.dataset?.state === state);
  return {card, calls, button};
}

test('mount fetches the card endpoint without waiting', async () => {
  let finish;
  const {card, calls} = setup(new Promise(resolve => { finish = resolve; }));
  assert.equal(card.hidden, true);
  assert.deepEqual(calls.map(c => c.path), ['./cardioid-compare']);
  finish(block()); await flush();
  assert.equal(card.hidden, false);
  assert.deepEqual(setup(undefined, undefined, false).calls, []);
});
test('absent or unavailable blocks and failed fetches keep the card hidden', async () => {
  for (const value of [undefined, {available: false, reason: 'no_rear'}]) {
    const {card} = setup(Promise.resolve(value)); await flush();
    assert.equal(card.hidden, true);
    assert.equal(card.children.length, 0);
  }
  for (const value of [Promise.reject(new Error('offline')), {error: 'failed'}]) {
    const {card} = setup(value); await flush();
    assert.equal(card.hidden, true);
    assert.equal(card.children.length, 0);
  }
});
test('tune metadata excludes fingerprints and has a second hint node', async () => {
  const data = block();
  const {card, button} = setup(data); await flush();
  const line = text(card.children[1]);
  for (const field of [data.tune.label, ...data.tune.layers, 'Sep 20']) assert.ok(line.includes(field));
  assert.doesNotMatch(text(card), /[a-f0-9]{8,}/i);
  assert.equal(button('on')['aria-pressed'], 'true');
  assert.equal(button('normal'), undefined);
  assert.equal(card.children.filter(n => n.className === 'form-hint' && !n.role).length, 2);
  data.tune.applied_at = null;
  const undated = setup(data); await flush();
  assert.doesNotMatch(text(undated.card), /Invalid Date|null/);
});
test('Off and Done POST states with CSRF and render the server answer', async () => {
  let answer = block('off');
  const {card, button, calls} = setup(block(), async () => answer); await flush();
  await button('off').click();
  assert.equal(button('off')['aria-pressed'], 'true');
  assert.ok(button('normal'));
  assert.ok(text(card).includes('2.5'));
  assert.ok(text(card).includes('3'));
  answer = block(); await button('normal').click();
  assert.equal(button('on')['aria-pressed'], 'true');
  assert.equal(button('normal'), undefined);
  assert.deepEqual(calls.slice(1).map(c => [c.path, c.body]), [
    ['./cardioid-compare', {state: 'off'}], ['./cardioid-compare', {state: 'normal'}]
  ]);
  assert.ok(calls.slice(1).every(c => c.method === 'POST' && c.headers['X-CSRF-Token'] === 'test-csrf'));
  answer = block('off'); await button('on').click();
  assert.deepEqual(calls.at(-1).body, {state: 'on'});
  assert.equal(button('off')['aria-pressed'], 'true');
});
test('pending requests disable controls and do not switch early', async () => {
  let finish;
  const {button, calls} = setup(block('on'), () => new Promise(resolve => { finish = resolve; }));
  await flush();
  const pending = button('off').click();
  assert.equal(button('on')['aria-pressed'], 'true');
  await button('on').click();
  assert.equal(calls.length, 2);
  for (const state of ['on', 'off', 'normal']) assert.equal(button(state).disabled, true);
  finish(block('off')); await pending;
  for (const state of ['on', 'off', 'normal']) assert.equal(button(state).disabled, false);
});
test('refusal shows the server message and keeps the last server state', async () => {
  const refusal = {error: 'no_pair', message: 'Measure this speaker first.'};
  const {card, button} = setup(block('off'), async () => refusal); await flush();
  await button('on').click();
  assert.equal(text(card.querySelector('[role="alert"]')), refusal.message);
  assert.equal(button('off')['aria-pressed'], 'true');
  assert.equal(button('on').disabled, false);
});
