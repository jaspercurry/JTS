// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import { h } from "/assets/shared/js/dom.js";
import { postJSON } from "/assets/shared/js/http.js";

export function initCardioidCompare(nowPlaying) {
  if (!nowPlaying) return () => {};
  const card = h('section.info-card#cardioid-compare-card', {hidden: true});
  nowPlaying.after(card);
  let busy = false;
  function setBusy(value) {
    busy = value;
    card.querySelectorAll('button').forEach(button => { button.disabled = value; });
  }
  async function select(state) {
    if (busy) return;
    card.querySelector('[role="alert"]')?.remove();
    setBusy(true);
    try {
      render(await postJSON('./cardioid-compare', {state}));
    } catch (err) {
      card.appendChild(h('p.banner.banner--danger', {role: 'alert'}, err.body?.message || err.message));
    } finally {
      setBusy(false);
    }
  }
  function render(block) {
    card.hidden = !block?.available;
    if (card.hidden) { card.replaceChildren(); return; }
    const {state, tune, level_match: match, expires_in_s: expires} = block;
    const active = state === 'on' || state === 'off';
    const date = tune.applied_at && new Date(tune.applied_at).toLocaleDateString('en-US', {
      month: 'short', day: 'numeric', timeZone: 'UTC'
    });
    const button = (value, label, className) => h('button', {
      type: 'button', className, disabled: busy, dataset: {state: value},
      ...(value === 'normal' ? {} : {'attr:aria-pressed': String(value === (state === 'off' ? 'off' : 'on'))}),
      onclick: () => select(value)
    }, label);
    const status = [];
    if (state === 'off') status.push('Rear woofer muted.');
    if (match.status === 'matched') {
      status.push(`Levels matched: ${match.louder === 'off' ? 'Off' : 'On'} plays ${Math.abs(match.trim_db)} dB lower while you compare.`);
    } else {
      status.push('Level match unavailable: no front/rear pair measurement on this speaker.');
    }
    if (active && expires !== null) status.push(`Resets by itself in ${Math.ceil(expires / 60)} min.`);
    card.replaceChildren(
      h('h2.eyebrow', {}, 'Cardioid'),
      h('p.form-hint', {}, [tune.label, tune.layers.join(' + '), date && 'applied ' + date].filter(Boolean).join(' · ')),
      h('div.segmented', {role: 'group', 'attr:aria-label': 'Cardioid'},
        button('on', 'On', 'segmented__btn'), button('off', 'Off', 'segmented__btn')),
      h('p.form-hint', {role: 'status'}, status.join(' ')),
      ...(active ? [button('normal', 'Done', 'btn btn--ghost')] : [])
    );
  }
  return render;
}
