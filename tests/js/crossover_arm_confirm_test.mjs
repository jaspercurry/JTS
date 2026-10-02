// SPDX-FileCopyrightText: 2026 Jasper Curry
// SPDX-License-Identifier: Apache-2.0
//
// An arm plan's "Start measurement" first asks that the arm's path is clear.
// Cancel posts nothing; the confirmation posts the start with the operator's
// word. A plan a person walks starts at once, with no question.
import assert from 'node:assert/strict';
import {CROSSOVER_IDS, crossoverMainModule} from './_dom.mjs';

globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};
const posted = [], asked = [];
let answer = false;
const confirm = {title: 'Is the arm\'s path clear?', message: 'Check the path.', confirm_label: 'The path is clear',
  attest: 'attest_rig_clear'};
const arm = {id: 'bass/axis', label: 'bass/axis', default: true, lines: ['plan'], action: {id: 'run_program',
  label: 'Start measurement', endpoint: '/v2/session', body: {request: {program: 'bass/axis', layout: 'bass_axis'}}, confirm}};
const person = {id: 'bass/axis@seat_express', label: 'bass/axis@seat_express', default: false, lines: ['plan'],
  action: {id: 'run_program', label: 'Start measurement', endpoint: '/v2/session',
    body: {request: {program: 'bass/axis', layout: 'seat_express'}}}};
const env = {capture: null, round_choices: [arm, person], round_lines: []};
const {elements, render, renderRoundChoice} = await crossoverMainModule({
  ids: [...CROSSOVER_IDS, ...['lines', 'choice', 'select', 'summary', 'start'].map(id => `crossover-round-${id}`)],
  extraStubs: {
    getJSON: async () => env,
    postJSON: async (endpoint, body) => { posted.push({endpoint, body}); return {}; },
    jtsConfirm: async (message, options) => { asked.push({message, ...options}); return answer; },
  },
  exportNames: ['render', 'renderRoundChoice'],
});
const start = () => elements.get('crossover-round-start').children[0];

render(env);
await start().click();
assert.deepEqual(asked, [{message: confirm.message, title: confirm.title, confirmLabel: confirm.confirm_label}]);
assert.deepEqual(posted, [], 'a cancelled confirmation posts nothing');

answer = true;
await start().click();
assert.deepEqual(posted, [{endpoint: arm.action.endpoint, body: {...arm.action.body, attest_rig_clear: true}}]);

posted.length = 0;
asked.length = 0;
elements.get('crossover-round-select').value = person.id;
renderRoundChoice();
await start().click();
assert.deepEqual(asked, [], 'a person walks it: no question');
assert.deepEqual(posted, [{endpoint: person.action.endpoint, body: person.action.body}]);
console.log(JSON.stringify({ok: true, passed: 5}));
