// SPDX-FileCopyrightText: 2026 Jasper Curry
// SPDX-License-Identifier: Apache-2.0

import assert from 'node:assert/strict';
import { test } from 'node:test';
import { buildFunction, repoPath } from './_loader.mjs';
import { element } from './_dom.mjs';

const nodes = node => [node, ...(node.children || []).flatMap(nodes)];
const text = node => nodes(node).map(n => n.textContent || '').join('');
const visible = node => (node.tag === 'details' && !node.open
  ? (node.children || []).filter(n => n.tag === 'summary') : node.children || []).map(visible).join('') + (node.textContent || '');
const make = tag => Object.assign(element(tag), {
  value: '', open: false, selected: false,
  appendChild(node) { this.children.push(node); },
  replaceChildren(...children) { this.children = children.map(child => typeof child === 'object' ? child : {textContent: String(child)}); },
  removeAttribute(key) { delete this[key]; },
});
globalThis.Node = class { static [Symbol.hasInstance](value) { return !!value?.appendChild; } };
const start = buildFunction([
  repoPath('deploy/assets/shared/js/dom.js'), repoPath('deploy/assets/sound-profile/js/speaker.js')
], { stripImports: true, stripExports: true,
  params: ['document', 'getJSON', 'postJSON', 'copyText', 'jtsConfirm'] });
const flush = () => new Promise(resolve => setImmediate(resolve));
const state = stage => ({
  stage, next_action: {id: 'copy_research', label: 'Copy prompt'}, issues: [],
  layout: {choices: {layout: 'mono', crossover: 'active', channels: 2, cardioid: false},
    topology: {speaker_groups: []}, outputs: [], driver_styles: []},
  draft: {operator_inputs: {}, manual_settings: {}, targets: [{target_id: 'main:woofer', role: 'woofer', label: 'Woofer', model: 'W6', values: {}}],
    prompt: 'Research the W6', enclosures: [], pads: [{value: 'none', label: 'No resistor or L-pad'}],
    installation_fields: {}, driver_fields: {}, resolved: {}},
  base_preview: {crossovers: [{between_roles: ['woofer', 'tweeter'], proposed_frequency_hz: 2500}], trims: [{role: 'tweeter', gain_db: -23, source: 'Estimated'}]},
  applied: {config_path: '/var/lib/camilladsp/private-config.yml', candidate_fingerprint: 'opaque-identity'},
  programs: [{id: 'speaker', title: 'Driver linearization', description: 'Fit the drivers.'}, {id: 'room', title: 'Room', description: 'Fit the room.'}],
});
function setup(initial = state('research'), handler = async () => ({setup: state('apply')})) {
  const root = make('view-body'), status = make('status'), requests = [], copies = [];
  const document = {getElementById: id => id === 'view-body' ? root : status, createElement: make,
    createTextNode: textContent => ({textContent})};
  start(document, async path => path === './setup' ? initial : {prompt: 'Tune this speaker using /opt/jasper'},
    async (path, body) => { requests.push({path, body: structuredClone(body)}); return handler(path, body); },
    async input => { copies.push(input.value); return true; }, async () => true);
  const button = label => nodes(root).find(n => n.tag === 'button' && text(n) === label);
  return {root, status, requests, copies, button};
}

test('pasting and applying use server state and reveal the tuning menu without reload', async () => {
  const ui = setup(undefined, async path => path.endsWith('/apply')
    ? {result: {status: 'applied'}, setup: state('tune')} : {setup: state('apply')});
  await flush();
  assert.doesNotMatch(visible(ui.root), /private-config|opaque-identity|\/var\/lib|false/);
  assert.equal(ui.button('Save to speaker'), undefined);
  const input = nodes(ui.root).find(n => n['aria-label'] === 'Paste research result');
  input.value = '```json\n{"driver": "raw result"}\n```';
  await ui.button('Load values').click();
  assert.equal(ui.requests[0].body.text, input.value);
  assert.match(visible(ui.root), /2500 Hz/);
  assert.match(visible(ui.root), /-23 dB · Estimated/);
  await ui.button('Save to speaker').click();
  assert.match(visible(ui.root), /Driver linearization/);
  assert.match(visible(ui.root), /Room/);
  assert.doesNotMatch(visible(ui.root), /measured|private-config|opaque-identity/);
  const tuning = nodes(ui.root).find(n => n.className === 'speaker-program');
  for (const program of nodes(ui.root).filter(n => n.className === 'speaker-program')) {
    assert.deepEqual(nodes(program).filter(n => n.tag === 'button').map(text), ['Copy prompt']);
    assert.equal(nodes(program).filter(n => n.tag === 'a').length, 0);
  }
  await nodes(tuning).find(n => n.tag === 'button').click();
  assert.deepEqual(ui.copies, ['Tune this speaker using /opt/jasper']);
  assert.doesNotMatch(visible(ui.root), /\/opt\/jasper/);
  const reloaded = setup(state('tune')); await flush();
  assert.match(visible(reloaded.root), /Driver linearization/);
});

test('failed import keeps pasted text; failed apply never reports an active setup', async () => {
  const ui = setup(undefined, async () => { throw new Error('Wrong driver target'); });
  await flush();
  const input = nodes(ui.root).find(n => n['aria-label'] === 'Paste research result');
  input.value = 'the complete rejected reply';
  await ui.button('Load values').click();
  assert.equal(input.value, 'the complete rejected reply');
  assert.equal(ui.status.textContent, 'Wrong driver target');
  const failed = setup(state('apply'), async () => ({result: {status: 'blocked'}, setup: state('apply')}));
  await flush();
  await failed.button('Save to speaker').click();
  assert.doesNotMatch(visible(failed.root), /setup is active/);
  assert.match(failed.status.textContent, /could not be applied/);
});
