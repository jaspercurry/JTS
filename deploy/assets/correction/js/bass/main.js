// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Bass-management DISPLAY page (revision plan §3.3 / P5). Read-only: it fetches
// the server-computed bass-management state and renders it. No control surface,
// no DSP, no mutating POST — the corner is owned by the speaker layer, not here.

import { h } from '/assets/shared/js/dom.js';

const els = {
  message: document.getElementById('bass-state-message'),
  list: document.getElementById('bass-state-list'),
};

function row(term, value) {
  // h() makes string children text nodes, so values never reach innerHTML.
  return [h('dt', term), h('dd', value)];
}

function render(state) {
  els.list.replaceChildren();

  if (!state || !state.configured) {
    els.message.textContent =
      'No bass management is configured on this speaker. Add a subwoofer ' +
      "during setup and its crossover will appear here.";
    els.list.hidden = true;
    return;
  }

  const rows = [];
  const corner = Number(state.corner_hz);
  rows.push(...row('Crossover corner', `${Math.round(corner)} Hz`));
  if (state.owner_label) {
    rows.push(...row('Owned by', state.owner_label));
  }
  rows.push(...row('Subwoofer', state.sub_present ? 'Present' : 'None'));
  // Three honest states: actually wired on this box; deliberately off; or the
  // known gap — an active speaker grouped with a wireless sub runs its mains
  // full-range (the server reports mains_highpass_unwired_reason for that).
  let mainsHp;
  if (state.mains_highpass_enabled) {
    mainsHp = `On — speakers roll off below ${Math.round(corner)} Hz`;
  } else if (
    state.mains_highpass_unwired_reason === 'active_endpoint_wireless_sub'
  ) {
    mainsHp =
      'Not applied on this speaker — its drivers run full-range alongside ' +
      'the wireless subwoofer (a known limitation of active speakers ' +
      'grouped with a wireless sub)';
  } else {
    mainsHp = 'Off — speakers stay full-range';
  }
  rows.push(...row('Mains high-pass', mainsHp));

  els.list.replaceChildren(...rows);
  els.list.hidden = false;
  els.message.textContent =
    'Your subwoofer and speakers hand off at this corner.';
}

async function load() {
  els.message.textContent = 'Loading…';
  try {
    const resp = await fetch('/bass/status', {cache: 'no-store'});
    let data = null;
    try { data = await resp.json(); } catch (_) { /* non-JSON */ }
    if (!resp.ok) {
      throw new Error(data && data.error ? data.error : 'HTTP ' + resp.status);
    }
    render(data);
  } catch (err) {
    // Never block — a read failure just shows a plain message.
    els.list.hidden = true;
    els.message.textContent =
      'Could not read the bass-management state right now.';
  }
}

load();
