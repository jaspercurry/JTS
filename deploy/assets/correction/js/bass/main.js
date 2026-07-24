// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Bass-management DISPLAY page (revision plan §3.3 / P5). Read-only: it fetches
// the server-computed bass-management state and renders it. No control surface,
// no DSP, no mutating POST — the corner is owned by the speaker layer, not here.

import { h } from '/assets/shared/js/dom.js';
import { getJSON } from '/assets/shared/js/http.js';

const els = {
  message: document.getElementById('bass-state-message'),
  list: document.getElementById('bass-state-list'),
};

// Bass Extension (docs/HANDOFF-bass-extension-plan.md §9) is a separate,
// not-yet-household-launched feature surfaced on this same tab, read from the
// same /bass/status payload. Read-only here too — no arming, no apply; the
// scheduler that would actually run this profile has not shipped (see
// runtimeStateValue below), so this section is strictly "what is saved,"
// never "what is playing."
const extEls = {
  section: document.getElementById('bass-extension-state'),
  message: document.getElementById('bass-extension-message'),
  list: document.getElementById('bass-extension-list'),
  recovery: document.getElementById('bass-extension-recovery'),
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

// Household-readable text for each contract_status value
// evaluate_loaded_bass_extension_profile (jasper/bass_extension/profile.py)
// can return for an already-successfully-parsed profile: "stale",
// "bypassed", or "accepted" — never "missing"/"malformed" (those are only
// possible on a fresh disk read, which this re-check never does). Anything
// unrecognized falls back to the generic "Unknown" message below.
const CONTRACT_STATUS_TEXT = {
  accepted: "Accepted — matches this speaker's current setup",
  bypassed: "Bypassed — still matches this speaker's current setup",
  stale: "Stale — no longer matches this speaker's current setup",
};

// Household-readable reasons for the BassExtensionRefusal slugs a *saved
// profile* re-check can actually produce
// (evaluate_loaded_bass_extension_profile in jasper/bass_extension/profile.py
// only ever appends these four for a contract_status of "stale"). The
// enum's other members belong to the commissioning ladder, not this
// re-check, so they never reach this page; an unrecognized slug falls back
// to showing the raw slug itself.
//
// baseline_not_applied and topology_mismatch are phrased as a hedged
// mismatch ("no longer matches"), not a causal claim ("has changed since")
// — the topology/baseline loaders behind those two checks are themselves
// fail-soft (a missing/unreadable file resolves to an empty draft or
// None), so a mismatch here does not by itself prove *what* changed, only
// that it no longer matches. enclosure_unsupported and profile_stale
// compare against in-code constants (ADAPTERS, BASS_EXTENSION_ALGORITHM_VERSION),
// not a fail-soft disk read, so "has changed since" is an accurate claim
// for those two.
const REFUSAL_TEXT = {
  bass_extension_baseline_not_applied:
    "the speaker's baseline tuning no longer matches what this profile was measured against",
  bass_extension_topology_mismatch:
    "the speaker's output configuration no longer matches what this profile was measured against",
  bass_extension_enclosure_unsupported:
    'the enclosure adapter has changed or is no longer supported',
  bass_extension_profile_stale:
    'this was measured with an older version of the bass-extension algorithm',
};

// Household-readable reason for each runtime_deferred_reason value.
// "fixed_graph_not_defined" is the only value jasper/bass_extension/__init__.py
// currently sets (ported/passive-radiator adapters); an unrecognized value
// falls back to showing the raw reason.
const DEFERRED_REASON_TEXT = {
  fixed_graph_not_defined:
    'this enclosure type does not have a fixed DSP graph defined yet',
};

function formatHz(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return '?';
  const rounded = Math.round(num * 10) / 10;
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
}

function capitalize(value) {
  const text = String(value || '');
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

function contractStatusValue(ext) {
  const text = CONTRACT_STATUS_TEXT[ext.contract_status] ||
    "Unknown — could not verify against this speaker's current setup";
  const refusals = Array.isArray(ext.contract_refusals) ? ext.contract_refusals : [];
  if (!refusals.length) return text;
  const reasons = refusals.map((slug) => REFUSAL_TEXT[slug] || slug).join('; ');
  // The household-readable reasons are the primary value; the raw
  // BassExtensionRefusal slugs stay visible in a smaller, secondary style
  // for operators grepping logs against the same strings.
  return [
    `${text}: ${reasons} `,
    h('span.form-hint', h('code', refusals.join(', '))),
  ];
}

function runtimeStateValue(ext) {
  if (!ext.runtime_eligible) {
    const reason = DEFERRED_REASON_TEXT[ext.runtime_deferred_reason] ||
      ext.runtime_deferred_reason || 'not yet supported on this hardware';
    return `Saved but not active on this speaker's hardware yet — ${reason}.`;
  }
  // No scheduler exists yet (docs/HANDOFF-bass-extension-plan.md §10.2:
  // "live fields do not ship until a revised Wave 5"), so an accepted
  // sealed profile is not currently changing what plays. Say so plainly —
  // this page must never imply extended bass is live.
  return 'Not currently active — no scheduler runs this yet, so the ' +
    'speaker still plays its natural (unextended) response.';
}

function renderBassExtension(ext) {
  // No profile and no pending recovery: hide the whole section rather than
  // showing an empty card for a feature this household hasn't set up on
  // this speaker. The doctor (check_bass_extension_profile) remains the
  // operator-facing surface for a malformed profile file; this household
  // page only ever needs to say something when there is something to say.
  extEls.section.hidden = !ext;
  if (!ext) {
    return;
  }

  extEls.list.replaceChildren();
  extEls.recovery.hidden = !ext.apply_recovery_required;

  if (!ext.commissioned) {
    // A recovery marker exists (see the banner above) but there is no
    // readable profile left to describe.
    extEls.message.textContent =
      'No readable bass-extension profile is currently saved on this speaker.';
    extEls.list.hidden = true;
    return;
  }

  const rows = [];
  rows.push(...row('Profile status', capitalize(ext.status)));
  rows.push(...row('Contract status', contractStatusValue(ext)));
  rows.push(...row(
    'Depth',
    `${formatHz(ext.deepest_hz)} Hz → ${formatHz(ext.natural_hz)} Hz`,
  ));
  rows.push(...row('Margin', capitalize(ext.margin)));
  rows.push(...row('Runtime', runtimeStateValue(ext)));

  extEls.list.replaceChildren(...rows);
  extEls.list.hidden = false;
  extEls.message.textContent =
    'This speaker has a saved bass-extension profile.';
}

async function load() {
  els.message.textContent = 'Loading…';
  extEls.message.textContent = 'Loading…';
  let state;
  try {
    state = await getJSON('/bass/status');
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
    extEls.recovery.hidden = true;
    extEls.message.textContent =
      'Could not read the bass-extension state right now.';
    return;
  }

  render(state);
  try {
    renderBassExtension(state.bass_extension);
  } catch (err) {
    // A rendering bug in this newer section must never take down the
    // long-shipped bass-management rendering above, which already
    // succeeded by this point. Same operational-error reasoning as the
    // fetch-failure branch above: keep the section visible.
    extEls.section.hidden = false;
    extEls.list.hidden = true;
    extEls.recovery.hidden = true;
    extEls.message.textContent =
      'Could not render the bass-extension state right now.';
  }
}

load();
