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
const make = tag => {
  const node = element(tag), listen = node.addEventListener;
  return Object.assign(node, {
    value: '', open: false, selected: false, on: {},
    addEventListener(event, fn) { this.on[event] = fn; listen(event, fn); },
    appendChild(node) { this.children.push(node); node.parent = this; },
    remove() { this.parent.children = this.parent.children.filter(node => node !== this); },
    focus() {},
    select() { this.selected = true; },
    replaceChildren(...children) { this.children = children.map(child => typeof child === 'object' ? child : {textContent: String(child)}); },
    removeAttribute(key) { delete this[key]; },
  });
};
globalThis.Node = class { static [Symbol.hasInstance](value) { return !!value?.appendChild; } };
const navigations = [];
globalThis.location = {hash: '', assign: url => navigations.push(url)};
const start = buildFunction([
  repoPath('deploy/assets/shared/js/dom.js'), repoPath('deploy/assets/shared/js/frequency-scale.js'),
  repoPath('deploy/assets/sound-profile/js/speaker.js'),
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
  geometry: {fields: {speaker_height_m: 'Speaker height (m)', cabinet_back_wall_m: 'Cabinet back to wall (m)'}, values: {speaker_height_m: 1}},
});
function setup(initial = state('research'), handler = async () => ({setup: state('apply')}), clipboard = {ok: true}, confirm = async () => true) {
  const root = make('view-body'), status = make('status'), requests = [], copies = [];
  const document = {body: make('body'), getElementById: id => id === 'view-body' ? root : status, createElement: make,
    createTextNode: textContent => ({textContent})};
  start(document, async path => path === './setup' ? initial : {prompt: 'Tune this speaker using /opt/jasper'},
    async (path, body) => { requests.push({path, body: structuredClone(body)}); return handler(path, body); },
    async input => { assert.ok(document.body.children.includes(input)); copies.push(input.value); return clipboard.ok; }, confirm);
  const button = label => nodes(root).find(n => n.tag === 'button' && text(n) === label);
  return {root, status, requests, copies, button, document};
}

test('pasting and applying use server state and reveal the tuning menu without reload', async () => {
  const ui = setup(undefined, async path => path.endsWith('/apply')
    ? {result: {status: 'applied'}, setup: state('tune')} : {setup: state('apply')});
  await flush();
  assert.doesNotMatch(visible(ui.root), /private-config|opaque-identity|\/var\/lib|false/);
  assert.equal(ui.button('Save to speaker'), undefined);
  const copyStyle = ui.button('Copy prompt').className;
  await ui.button('Copy prompt').click();
  assert.deepEqual(ui.copies, ['Research the W6']);
  assert.equal(ui.document.body.children.length, 0);
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
    assert.equal(nodes(program).find(n => n.tag === 'button').className, copyStyle);
    assert.equal(nodes(program).filter(n => n.tag === 'a').length, 0);
  }
  await nodes(tuning).find(n => n.tag === 'button').click();
  assert.deepEqual(ui.copies, ['Research the W6', 'Tune this speaker using /opt/jasper']);
  assert.equal(ui.document.body.children.length, 0);
  assert.equal(nodes(ui.root).filter(n => n.tag === 'summary' && text(n) === 'View prompt').length, 0);
  assert.doesNotMatch(visible(ui.root), /\/opt\/jasper/);
  const reloaded = setup(state('tune')); await flush();
  assert.match(visible(reloaded.root), /Driver linearization/);
});

for (const [source, values, drivers, shown] of [
  ['research', {usable_frequency_range_hz: [40, 3000]}, [], '40 Hz – 3.0 kHz (research)'],
  ['custom', {usable_frequency_range_hz: [45.4, 2800]},
    [{target_id: 'main:woofer', role: 'woofer', usable_frequency_range_hz: [45.4, 2800]}], '45 Hz – 2.8 kHz (custom)'],
  ['nowhere', {usable_frequency_range_hz: null}, [], 'Not specified'],
]) test(`a driver card names its usable range from ${source}`, async () => {
  const initial = state('details');
  initial.draft.targets[0].values = values;
  initial.draft.manual_settings = {drivers};
  const ui = setup(initial);
  await flush();
  const row = nodes(ui.root).find(n => n.tag === 'dl');
  assert.deepEqual(row.children.map(text), ['Usable range', shown]);
});

test('a cardioid speaker saves its woofer spacing with the details and its placement on its own', async () => {
  const initial = state('tune');
  initial.layout.choices = {...initial.layout.choices, channels: 3, cardioid: true};
  const ui = setup(initial, async () => ({setup: initial}));
  await flush();
  const type = (label, value) => nodes(ui.root).find(n => n.tag === 'label' && text(n) === label)
    .children.find(n => n.tag === 'input').on.input({target: {value}});
  type('Front-to-rear woofer spacing (mm)', '330');
  await ui.button('Save details').click();
  type('Cabinet back to wall (m)', '0.2');
  await ui.button('Save placement').click();
  assert.deepEqual(ui.requests.map(request => request.path), ['./setup/details', './setup/geometry']);
  assert.equal(ui.requests[0].body.manual_settings.rear_woofer_spacing_mm, 330);
  assert.deepEqual(ui.requests[1].body, {speaker_height_m: 1, cabinet_back_wall_m: 0.2});
});

test('a refused draft shows its refusal once, in the open driver details card', async () => {
  const initial = state('details');
  initial.issues = [{severity: 'blocker', code: 'manual_target_unknown', message: 'Enter this driver in its card.'}];
  const ui = setup(initial);
  await flush();
  const card = nodes(ui.root).find(n => n.id === 'driver-safety-issues');
  assert.equal(card.open, true);
  assert.match(visible(card), /Enter this driver in its card\./);
  assert.equal(nodes(ui.root).filter(n => n.tag === 'p' && text(n) === initial.issues[0].message).length, 1);
});

for (const [stage, programs, offered] of [
  ['tune', ['speaker', 'room'], true],
  ['tune', ['bass', 'room'], false],
  ['apply', ['speaker', 'room'], false],
]) test(`the ${stage} stage with ${programs.join(' and ')} ${offered ? 'offers' : 'omits'} the measurement page as an active primary button`, async () => {
  const initial = state(stage);
  initial.programs = programs.map(id => ({id, title: id, description: ''}));
  const ui = setup(initial);
  await flush();
  const control = ui.button('Take measurements');
  assert.equal(control !== undefined, offered);
  if (!offered) return;
  assert.ok(control.className.split(' ').includes('btn--primary'));
  assert.equal(control.disabled, false);
  await control.click();
  assert.deepEqual(navigations.splice(0), ['crossover/']);
});

for (const [stage, answer, asked, applied] of [['tune', false, 1, 0], ['tune', true, 1, 1], ['apply', false, 0, 1]]) test(
  `save to speaker in the ${stage} stage asks ${asked} time(s) and applies ${applied} time(s)`, async () => {
    let asks = 0;
    const ui = setup(state(stage), async () => ({result: {status: 'applied'}, setup: state('tune')}), undefined,
      async () => { asks += 1; return answer; });
    await flush();
    await ui.button('Save to speaker').click();
    assert.equal(asks, asked);
    assert.deepEqual(ui.requests.map(request => request.path), Array(applied).fill('./setup/apply'));
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

test('failed apply with a server issue shows its message and code', async () => {
  const ui = setup(state('apply'), async () => ({
    result: {status: 'needs_attention', issues: [
      {code: 'output_route_not_ready', message: 'The audio output is not ready.'},
    ]},
    setup: state('apply'),
  }));
  await flush();
  await ui.button('Save to speaker').click();
  assert.equal(ui.status.textContent, 'The audio output is not ready. (output_route_not_ready)');
});

for (const stage of ['research', 'tune']) test(`${stage} prompt can be copied manually if clipboard access fails`, async () => {
  const clipboard = {ok: false};
  const ui = setup(state(stage), undefined, clipboard);
  await flush();
  const card = stage === 'tune' ? nodes(ui.root).find(n => n.className === 'speaker-program') : ui.root;
  const copy = nodes(card).find(n => n.tag === 'button' && text(n) === 'Copy prompt');
  await copy.click();
  const prompt = nodes(card).find(n => n['aria-label'] === 'Prompt');
  assert.equal(prompt.value, ui.copies[0]);
  assert.equal(prompt.selected, true);
  assert.equal(prompt['aria-hidden'], undefined);
  assert.equal(ui.document.body.children.length, 0);
  clipboard.ok = true;
  await copy.click();
  assert.equal(nodes(card).find(n => n['aria-label'] === 'Prompt'), undefined);
});
