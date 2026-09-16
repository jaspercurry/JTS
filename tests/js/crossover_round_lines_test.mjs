// SPDX-FileCopyrightText: 2026 Jasper Curry
// SPDX-License-Identifier: Apache-2.0
import assert from 'node:assert/strict';
import {CROSSOVER_IDS, crossoverMainModule} from './_dom.mjs';

globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};
const posted = [];
const action = {id: 'run_program', label: 'server label', endpoint: '/server/run', body: {plan: {program: 'tournament/full'}}};
const choice = {id: 'tournament/full', label: 'tournament/full', default: true, lines: ['server summary'], action};
let env = {capture: null, round_choices: [choice], round_lines: []};
const {elements, render} = await crossoverMainModule({
  ids: [...CROSSOVER_IDS, ...['lines', 'choice', 'select', 'summary', 'start'].map(id => `crossover-round-${id}`)],
  extraStubs: {getJSON: async () => env, postJSON: async (endpoint, body) => {posted.push({endpoint, body}); return {};},
    renderCloud: () => {}, redrawCloudChart: () => {}},
});
render(env);
assert.equal(elements.get('crossover-round-select').value, choice.id);
assert.deepEqual(elements.get('crossover-round-summary').children.map(n => n.textContent), choice.lines);
const start = elements.get('crossover-round-start').children[0];
render(env);
assert.equal(elements.get('crossover-round-start').children[0], start);
await start.click();
assert.deepEqual(posted, [{endpoint: action.endpoint, body: action.body}]);
env = {...env, round_choices: [], round_lines: ['server progress'], capture: null, busy: true,
  pending: {actions: [{...action, id: 'retake'}]}};
render(env);
assert.equal(elements.get('crossover-round-choice').hidden, true);
assert.deepEqual(elements.get('crossover-round-lines').children.map(n => n.textContent), env.round_lines);
assert.equal(elements.get('crossover-action').children[0].textContent, action.label);
render({...env, busy: false, pending: null, next_action: action});
assert.equal(elements.get('crossover-action').children[0].textContent, action.label);
console.log(JSON.stringify({ok: true, passed: 8}));
