// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import { h } from "/assets/shared/js/dom.js";
import { getJSON, postJSON } from "/assets/shared/js/http.js";

export const trimSteps = (levelDb, otherLevelDb, stepDb) =>
  Math.min(0, Math.round((otherLevelDb - levelDb) / stepDb));
export const targetPercent = (basePercent, steps) => Math.max(1, basePercent + steps);
export function flipOrder(current, target) {
  const volume = current.percent === target.percent ? [] : ['volume'];
  const apply = current.fingerprint === target.fingerprint ? [] : ['apply'];
  return target.percent < current.percent ? [...volume, ...apply] : [...apply, ...volume];
}

let abMountedCard;
export function renderAbListenCard() {
  return '<section class="info-card" id="ab-listen-card"></section>';
}
export function initAbListen() {
  const card = document.getElementById('ab-listen-card');
  if (!card) return;
  if (abMountedCard) { card.replaceWith(abMountedCard); return; }
  abMountedCard = card;
  let data, round, pair, current, basePercent, running = false, busy = false, blind = null, error = '';
  const label = key => blind ? blind[key] : key;
  const name = tune => tune.fingerprint.slice(0, 8) + (tune.base ? ' · base' : '') +
    (tune.fingerprint === current?.fingerprint ? ' · applied' : '');
  const button = (act, text, primary = false, disabled = false) => h('button.btn', {
    type: 'button', className: primary ? 'btn--primary' : 'btn--ghost',
    dataset: {act}, disabled: busy || disabled
  }, text);
  function select(title, value, options, change) {
    return h('div.field', {}, h('label', {htmlFor: 'ab-' + title}, title),
      h('select', {id: 'ab-' + title, disabled: busy || running, onchange: change},
        options.map(([id, text]) => h('option', {value: id, selected: id === value}, text))));
  }
  function chooseRound(id) {
    round = data.rounds.find(r => r.round_id === id);
    const a = round.tunes.find(t => t.fingerprint === current.fingerprint) ||
      round.tunes.find(t => t.base) || round.tunes[0];
    pair = {A: a, B: round.tunes.find(t => t !== a)};
  }
  function draw() {
    const nodes = [h('h2.eyebrow', {}, 'A/B listen')];
    if (!data) nodes.push(h('p.form-hint', {}, 'Compare two trialled tunes at matched levels.'),
      button('setup', busy ? 'Loading…' : 'Set up'));
    else if (!round) nodes.push(h('p.form-hint', {}, 'No trial round with two tunes yet.'));
    else {
      if (!blind) {
        if (data.rounds.length > 1) nodes.push(select('Round', round.round_id,
          data.rounds.map(r => [r.round_id, [r.round_id, r.program, r.banked_at].filter(Boolean).join(' · ')]),
          e => { chooseRound(e.target.value); draw(); }));
        for (const key of ['A', 'B']) nodes.push(select(key, pair[key].fingerprint,
          round.tunes.map(t => [t.fingerprint, name(t)]),
          e => { pair[key] = round.tunes.find(t => t.fingerprint === e.target.value); draw(); }));
        const louder = pair.A.level_db > pair.B.level_db ? 'A' : 'B';
        const diff = Math.abs(pair.A.level_db - pair.B.level_db);
        const steps = -trimSteps(pair[louder].level_db, pair[louder === 'A' ? 'B' : 'A'].level_db, data.volume_step_db);
        nodes.push(h('p.form-hint', {}, `${louder} is ${diff.toFixed(2)} dB louder. It plays ${steps} volume ` +
          `step${steps === 1 ? '' : 's'} (${(steps * data.volume_step_db).toFixed(2)} dB) lower. ` +
          `Difference left: ${Math.abs(diff - steps * data.volume_step_db).toFixed(2)} dB.`));
      }
      if (running) {
        const playing = Object.keys(pair).find(k => pair[k].fingerprint === current.fingerprint);
        nodes.push(h('span.badge.badge--ok', {}, playing ? 'Playing ' + label(playing) +
          (blind ? '' : ' · ' + pair[playing].fingerprint.slice(0, 8)) : 'Selected tune has not started'),
          h('div.form-actions', {}, button('flip', busy ? 'Switching…' : 'Flip to ' + label(playing === 'A' ? 'B' : 'A'), true), button('end', 'End')),
          h('div.setting-row', {}, h('span', {}, 'Blind'), h('label.toggle', {},
            h('input', {type: 'checkbox', checked: !!blind, disabled: busy, 'attr:aria-label': 'Blind',
              onchange: e => { blind = e.target.checked ? (Math.random() < 0.5 ? {A: 'X', B: 'Y'} : {A: 'Y', B: 'X'}) : null; draw(); }}),
            h('span.track'))));
      } else nodes.push(button('start', busy ? 'Starting…' : 'Start', true, pair.A === pair.B));
    }
    if (error) nodes.push(h('p.banner.banner--error', {role: 'status'}, error));
    card.replaceChildren(...nodes);
  }
  function failure(err) {
    const body = err.body || {};
    const issue = body.issue;
    return [body.error || (typeof issue === 'string' ? issue : issue?.message) || err.message,
      body.next_action].filter(Boolean).join(' ');
  }
  async function volume(percent) {
    await postJSON('/volume/set', {percent});
    current.percent = percent;
  }
  async function move(key, percent) {
    const target = {fingerprint: pair[key].fingerprint, percent};
    const previous = current.percent;
    for (const action of flipOrder(current, target)) {
      if (action === 'volume') await volume(percent);
      else {
        try {
          const body = await postJSON(data.apply_path, {expected_candidate_fingerprint: target.fingerprint});
          if (body.status !== 'applied') throw Object.assign(new Error('Tune was not applied.'), {body});
          current.fingerprint = target.fingerprint;
        } catch (err) {
          if (current.percent !== previous) {
            try { await volume(previous); } catch (restore) { error = failure(err) + ' ' + failure(restore); }
          }
          throw err;
        }
      }
    }
  }
  card.addEventListener('click', async e => {
    const act = e.target.closest('[data-act]')?.dataset.act;
    if (!act) return;
    busy = true; error = ''; draw();
    try {
      if (act === 'setup') {
        data = await getJSON('./ab-listen/state');
        current = {fingerprint: data.applied_fingerprint};
        if (data.rounds.length) chooseRound(data.rounds[0].round_id);
      } else {
        if (act === 'start') { basePercent = (await getJSON('/volume')).percent; current.percent = basePercent; running = true; }
        const key = act === 'flip' && current.fingerprint === pair.A.fingerprint ? 'B' : 'A';
        const steps = trimSteps(pair[key].level_db, pair[key === 'A' ? 'B' : 'A'].level_db, data.volume_step_db);
        await move(key, act === 'end' ? basePercent : targetPercent(basePercent, steps));
        if (act === 'end') { running = false; blind = null; }
      }
    } catch (err) { error = error || failure(err); }
    busy = false; draw();
  });
  draw();
}
