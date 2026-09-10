// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import { h } from '/assets/shared/js/dom.js';
import { getJSON } from '/assets/shared/js/http.js';

const els = {
  message: document.getElementById('bass-state-message'),
  list: document.getElementById('bass-state-list'),
};

const extEls = {
  section: document.getElementById('bass-extension-state'),
  message: document.getElementById('bass-extension-message'),
  list: document.getElementById('bass-extension-list'),
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

  // A sub on this speaker's DAC is only ever wired WITH its complementary
  // mains high-pass: a graph carrying one without the other is refused
  // (active_baseline_bass_mgmt_highpass_missing). So the corner alone settles
  // both rows and the server sends no separate flag.
  const corner = Math.round(Number(state.corner_hz));
  els.list.replaceChildren(
    ...row('Crossover corner', `${corner} Hz`),
    ...row('Mains high-pass', `On — speakers roll off below ${corner} Hz`),
  );
  els.list.hidden = false;
  els.message.textContent =
    'Your subwoofer and speakers hand off at this corner.';
}

function renderBassExtension(ext, error) {
  extEls.section.hidden = !ext && !error;
  extEls.list.replaceChildren();
  extEls.list.hidden = true;
  if (error) {
    extEls.message.textContent = 'Could not read the saved bass-extension setting.';
    return;
  }
  if (!ext) return;
  extEls.list.replaceChildren(
    ...row('Maximum added bass', `${ext.low_boost_db} dB`),
    ...row('Volume response', 'Added bass decreases as volume and bass demand rise'),
  );
  extEls.list.hidden = false;
  extEls.message.textContent = 'Dynamic bass extension is included in the saved speaker tune.';
}

async function load() {
  els.message.textContent = 'Loading…';
  extEls.message.textContent = 'Loading…';
  let state;
  try {
    // Page-relative: nginx strips /sound/bass/ down to the backend's /bass/.
    state = await getJSON('status');
  } catch (err) {
    // Never block — a read failure just shows a plain message. Unlike the
    // "nothing to show" case in renderBassExtension, a fetch failure is an
    // operational error, not "not commissioned" — keep the section visible
    // so it is not confused with the hidden-by-design absent state.
    els.list.hidden = true;
    els.message.textContent =
      'Could not read the bass-management state right now.';
    extEls.section.hidden = false;
    extEls.list.hidden = true;
    extEls.message.textContent =
      'Could not read the bass-extension state right now.';
    return;
  }

  render(state);
  try {
    renderBassExtension(state.bass_extension, state.bass_extension_error);
  } catch (err) {
    // A rendering bug in this newer section must never take down the
    // long-shipped bass-management rendering above, which already
    // succeeded by this point. Same operational-error reasoning as the
    // fetch-failure branch above: keep the section visible.
    extEls.section.hidden = false;
    extEls.list.hidden = true;
    extEls.message.textContent =
      'Could not render the bass-extension state right now.';
  }
}

load();
