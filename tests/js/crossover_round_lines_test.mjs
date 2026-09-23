// SPDX-FileCopyrightText: 2026 Jasper Curry
// SPDX-License-Identifier: Apache-2.0
import assert from 'node:assert/strict';
import {CROSSOVER_IDS, crossoverMainModule} from './_dom.mjs';

globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};
const posted = [], requested = [];
let postResponse = {};
const action = {id: 'run_program', label: 'server label', endpoint: '/server/run', body: {plan: {program: 'tournament/full'}}};
const choice = {id: 'tournament/full', label: 'tournament/full', default: true, lines: ['server summary'], action};
let env = {capture: null, round_choices: [choice], round_lines: []};
const {elements, render, refresh} = await crossoverMainModule({
  ids: [...CROSSOVER_IDS, ...['lines', 'choice', 'select', 'summary', 'start'].map(id => `crossover-round-${id}`)],
  extraStubs: {window: {location: {search: '?program=rear'}},
    getJSON: async url => {requested.push(url); return env;}, postJSON: async (endpoint, body) => {posted.push({endpoint, body}); return postResponse;},
    renderCloud: () => {}, redrawCloudChart: () => {}},
  exportNames: ['render', 'refresh'],
});
await refresh();
assert.equal(new URL(requested[0], 'http://speaker').searchParams.get('program'), 'rear');
await refresh();
assert.equal(new URL(requested[1], 'http://speaker').searchParams.get('program'), choice.id);
render(env);
assert.equal(elements.get('crossover-round-select').value, choice.id);
assert.deepEqual(elements.get('crossover-round-summary').children.map(n => n.textContent), choice.lines);
const start = elements.get('crossover-round-start').children[0];
render(env);
assert.equal(elements.get('crossover-round-start').children[0], start);
await start.click();
assert.deepEqual(posted, [{endpoint: action.endpoint, body: action.body}]);
// Its refused pair (#5321): a choice the server sent no action for renders
// its reason and no Start button.
const refused = {id: 'front_rear/express', label: 'front_rear/express', code: 'measurement_candidate_required',
  lines: ['This measurement needs a saved tuning to test. Select the tuning, then measure again.']};
elements.get('crossover-round-select').value = refused.id;
render({...env, round_choices: [choice, refused]});
assert.deepEqual(elements.get('crossover-round-summary').children.map(n => n.textContent), refused.lines);
assert.deepEqual(elements.get('crossover-round-start').children, []);
env = {...env, round_choices: [], round_lines: ['server progress'], capture: null, busy: true,
  pending: {actions: [{...action, id: 'retake'}]}};
render(env);
assert.equal(elements.get('crossover-round-choice').hidden, true);
assert.deepEqual(elements.get('crossover-round-lines').children.map(n => n.textContent), env.round_lines);
assert.equal(elements.get('crossover-action').children[0].textContent, action.label);
// Every pose's hold shows its own placement words in the walk card, not only
// the first pose's; the action row keeps the buttons (#5632 F5).
const hold = {mover: 'human', degrees: 0, vertical_deg: 0,
  prompt: {progress: 'Pose 2 of 3', title: 'Move the microphone 12 in (30 cm) FORWARD of the head centre.', body: 'Keep it still.'},
  actions: [{id: 'position_ready', label: 'Microphone is at the seat', endpoint: '/placed', body: {}}, {...action, id: 'retake'}]};
render({...env, pending: hold});
assert.equal(elements.get('crossover-walk').hidden, false);
assert.deepEqual(['progress', 'headline', 'detail'].map(id => elements.get(`crossover-walk-${id}`).textContent),
  [hold.prompt.progress, hold.prompt.title, hold.prompt.body]);
assert.deepEqual(elements.get('crossover-walk-action').children, []);
assert.deepEqual(elements.get('crossover-action').children.map(n => n.textContent), hold.actions.map(a => a.label));
render(env);
assert.equal(elements.get('crossover-walk').hidden, true);
// A take's acknowledgement ends with the take: a hold waiting for a person, or
// the round ending, clears it, and a stop never reads as a start (#5632 F9).
const status = elements.get('capture-status');
const playing = env;
render({...playing, pending: hold});
postResponse = {released: {index: 2}};
await elements.get('crossover-action').children[0].click();
assert.equal(status.textContent, 'Measurement started.');
render({...playing, pending: hold});
assert.equal(status.textContent, '');
const reset = {id: 'reset_round', label: 'Reset the round', endpoint: '/cancel', body: {}};
render({...playing, pending: {actions: [...playing.pending.actions, reset]}});
postResponse = {capture: {status: 'stopping'}};
await elements.get('crossover-action').children[1].click();
assert.equal(status.textContent, 'Updated.');
render({...playing, busy: false, pending: null});
assert.equal(status.textContent, '');
render({...env, busy: false, pending: null, next_action: action});
assert.equal(elements.get('crossover-action').children[0].textContent, action.label);
for (const reason of ['no_repeats', 'insufficient_positions', 'insufficient_agreement_seats', 'unknown_analysis_reason']) {
  render({...env, round_lines: [reason]});
  assert.deepEqual(elements.get('crossover-round-lines').children.map(n => n.textContent), [reason]);
}
console.log(JSON.stringify({ok: true, passed: 23}));
