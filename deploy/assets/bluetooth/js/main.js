// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// main.js — /bluetooth/ generic Bluetooth control panel.
//
// Phone-Settings-style live device list: pair anything, connect/disconnect/
// forget, toggle the adapter on/off and pairing mode. The page is rendered
// server-side by jasper.web.bluetooth_setup; this module owns ONLY the live
// behaviour:
//
//   * jsonHeaders()/postJSON() (CSRF X-CSRF-Token + Content-Type) from shared
//     http.js — the same contract guard_mutating_request() accepts as a hidden
//     form field. The token rides in canonical_page()'s jts-csrf meta tag.
//   * jtsConfirm / jtsAlert (accessible <dialog>, never window.confirm/alert,
//     which the browser can suppress) from the shared dialog.js.
//
// Device names, MACs, and the bluez `icon` slug are UNTRUSTED — every value
// that lands in innerHTML goes through escapeHtml()/cssIdSafe()/iconSlug(), and
// per-row action targets ride in escaped data-* attributes consumed by a single
// delegated click handler (never inline onclick), exactly as before.

import { jsonHeaders, postJSON } from "/assets/shared/js/http.js";
import { jtsConfirm, jtsAlert } from "/assets/shared/js/dialog.js";
import { escapeHtml, cssIdSafe } from "/assets/shared/js/escape.js";
import { toggleScanRequest } from "./scan.js";

let state = {
  desired: false,
  effective: "off",
  available: true,
  parked: false,
  powered: false,
  discoverable: false,
  discovering: false,
};
let devices = new Map(); // path → device
let evtSrc = null;
let pairStreams = new Map(); // mac → EventSource
let stateTimer = null;
let scanIntentUntil = 0;  // ms; client-side window where we treat
                           // the button as scanning even before the
                           // server polling catches up
let powerIntentUnknown = false;
// Every mutating route can legitimately wait on a bounded systemd/BlueZ
// transition. Suppress background GETs and overlapping writes for the whole
// window so one slow household action cannot amplify into more backend work.
let mutationInFlight = false;
let stateFetchPromise = null;

// -------- adapter state + toggles --------

async function fetchState(force = false) {
  if (mutationInFlight && !force) return;
  if (stateFetchPromise !== null) return stateFetchPromise;
  stateFetchPromise = (async () => {
    try {
      const r = await fetch('state', { cache: 'no-store' });
      const payload = await r.json().catch(() => ({}));
      if (!r.ok) {
        state = {
          ...state,
          available: false,
          effective: 'unavailable',
          powered: false,
          discoverable: false,
          discovering: false,
          error: payload.error || `Bluetooth state request failed (${r.status})`,
        };
        renderToggles();
        return;
      }
      state = payload;
      powerIntentUnknown = false;
      renderToggles();
    } catch (e) {
      state = {
        ...state,
        available: false,
        effective: 'unavailable',
        powered: false,
        discoverable: false,
        discovering: false,
      };
      renderToggles();
    }
  })();
  try {
    return await stateFetchPromise;
  } finally {
    stateFetchPromise = null;
  }
}

function renderToggles() {
  const unavailable = state.available === false || state.effective === 'unavailable';
  const parked = !!state.parked || state.effective === 'parked';
  const power = document.getElementById('sw-power');
  if (!powerIntentUnknown) power.checked = !!state.desired;
  // Missing hardware blocks On, never the safer persisted Off repair.
  power.disabled = mutationInFlight || powerIntentUnknown || parked
    || (unavailable && !state.desired);
  const sd = document.getElementById('sw-disc');
  sd.checked = !!state.discoverable;
  // Activation needs a ready radio; an already-active pairing window must
  // remain switchable Off as cleanup even when availability later degrades.
  sd.disabled = mutationInFlight || parked || (!state.discoverable
    && (unavailable || !state.desired || !state.powered));
  let hint;
  if (parked) {
    hint = 'Managed by this speaker’s stereo pair.';
  } else if (unavailable) {
    hint = state.unavailableReason || state.error || 'Bluetooth state unavailable.';
  } else if (state.effective === 'degraded') {
    hint = state.degradedReason || (state.desired
      ? 'Set to on, but the Bluetooth radio is not ready.'
      : 'Set to off, but the Bluetooth radio is still active.');
  } else {
    hint = state.powered ? `On — adapter ${state.adapter || 'hci0'}` : 'Off';
  }
  if (!unavailable && !parked && state.discovering) hint += ' · scanning…';
  document.getElementById('bt-hint').textContent = hint;

  // Treat the button as "scanning" if the server reports Discovering
  // OR we just clicked Scan in the last ~3 s — bridges the gap
  // between optimistic click and the polling cycle confirming it.
  const intent = Date.now() < scanIntentUntil;
  const scanning = !parked && (state.discovering || intent);
  const btn = document.getElementById('scan-btn');
  // As with pairing mode, degraded availability blocks Start but not Stop.
  btn.disabled = mutationInFlight || parked || (!scanning
    && (unavailable || !state.desired || !state.powered));
  btn.classList.toggle("scanning", scanning);
  btn.innerHTML = scanning
    ? '<span class="spinner spinner--button"></span>Scanning'
    : "Scan";
  renderDevices();

  // While scanning, poll faster so the button reverts promptly when
  // the auto-stop fires server-side.
  schedulePoll(scanning ? 1500 : 5000);
}

function schedulePoll(ms) {
  if (stateTimer !== null) clearInterval(stateTimer);
  stateTimer = setInterval(fetchState, ms);
}

function beginMutation() {
  if (mutationInFlight) return false;
  mutationInFlight = true;
  renderToggles();
  return true;
}

async function finishMutation() {
  try {
    if (stateFetchPromise !== null) {
      try {
        await stateFetchPromise;
      } catch (_) {
        // fetchState already owns rendering failure policy.
      }
    }
    // Keep ownership through the fresh read. Device SSE events can redraw
    // action buttons during this await; mutationInFlight must therefore stay
    // true until the authoritative snapshot has landed.
    await fetchState(true);
  } finally {
    mutationInFlight = false;
    renderToggles();
  }
}

// HID profile fragments — 0x1124 (BR/EDR HID) and 0x1812 (BLE HOGP).
// VK-01-class knobs advertise HOGP only, not classic HID. Mirrors
// jasper.bluetooth.models.is_hid_uuids so the warning fires in
// the same conditions on either side.
const HID_UUID_FRAGMENTS = ['00001124-', '00001812-'];

function pairedHidNames() {
  const names = [];
  for (const d of devices.values()) {
    if (!d.paired) continue;
    const uu = (d.uuids || []).join(' ').toLowerCase();
    if (HID_UUID_FRAGMENTS.some(f => uu.includes(f))) {
      names.push(d.name || 'Unknown device');
    }
  }
  return names;
}

async function togglePower() {
  if (mutationInFlight) return;
  const input = document.getElementById('sw-power');
  const previous = !!state.desired;
  const target = !!input.checked;
  function restoreToggle() {
    input.checked = previous;
  }
  if (target === previous) return;
  // Warn before turning Bluetooth off while a wireless remote
  // (volume knob, etc.) is paired — otherwise the remote silently
  // stops working until BT is turned back on.
  if (!target) {
    const hidNames = pairedHidNames();
    if (hidNames.length) {
      const which = hidNames.length === 1
        ? hidNames[0]
        : hidNames.length + ' paired remotes';
      const ok = await jtsConfirm(
        'Turning Bluetooth off will also disconnect ' + which +
        '. Wireless remotes will not work again until Bluetooth ' +
        'is turned back on.\n\nTurn Bluetooth off anyway?',
        {danger: true},
      );
      if (!ok) {
        restoreToggle();
        return;
      }
    }
  }
  // The confirmation yields to other controls. Acquire mutation ownership only
  // after it resolves; if another action won meanwhile, restore this optimistic
  // flip and leave the shared intent-known state untouched.
  if (!beginMutation()) {
    restoreToggle();
    return;
  }
  powerIntentUnknown = true;
  renderToggles();
  try {
    const r = await fetch('power', {
      method: 'POST', headers: jsonHeaders(),
      body: JSON.stringify({on: target}),
    });
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      if (data.state && typeof data.state === 'object') {
        state = data.state;
        powerIntentUnknown = false;
        renderToggles();
      } else {
        restoreToggle();
      }
      await jtsAlert('Bluetooth toggle failed: ' + (data.error || data.message || r.status));
    }
  } catch (e) {
    // The POST may have durably landed even when its response was lost. Keep
    // the optimistic position disabled/unknown until an authoritative GET
    // resolves it; never invent a rollback from transport ambiguity.
    powerIntentUnknown = true;
    renderToggles();
    await jtsAlert('Network error talking to the Bluetooth backend.');
  } finally {
    await finishMutation();
  }
}

async function toggleDisc() {
  const input = document.getElementById('sw-disc');
  const previous = !!state.discoverable;
  const target = !!input.checked;
  function restoreToggle() {
    input.checked = previous;
  }
  if (target === previous) return;
  if (target && (
    state.available === false || state.parked || !state.desired || !state.powered
  )) {
    restoreToggle();
    return;
  }
  if (!beginMutation()) {
    restoreToggle();
    return;
  }
  try {
    const r = await fetch('discoverable', {
      method: 'POST', headers: jsonHeaders(),
      body: JSON.stringify({on: target}),
    });
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      restoreToggle();
      jtsAlert('Pairing mode toggle failed: ' + (data.error || data.message || r.status));
    }
  } catch (e) {
    restoreToggle();
    jtsAlert('Network error talking to the Bluetooth backend.');
  } finally {
    await finishMutation();
  }
}

async function toggleScan() {
  if (!beginMutation()) return;
  try {
    return await toggleScanRequest({
      discovering: !!state.discovering || Date.now() < scanIntentUntil,
      setIntentUntil(value) { scanIntentUntil = value; },
      render: renderToggles,
      postScan(action) { return postJSON('scan', {action}); },
      refreshState() {},
      showAlert: jtsAlert,
    });
  } finally {
    await finishMutation();
  }
}

// -------- live device list --------

function startDeviceStream() {
  if (evtSrc) evtSrc.close();
  evtSrc = new EventSource('devices/stream');
  evtSrc.onmessage = ev => {
    let data;
    try { data = JSON.parse(ev.data); } catch (e) { return; }
    if (data.action === 'remove') {
      devices.delete(data.device.path);
    } else {
      devices.set(data.device.path, data.device);
    }
    renderDevices();
  };
  evtSrc.onerror = () => {
    // Auto-reconnect on stream drop.
    setTimeout(startDeviceStream, 2000);
  };
}

function renderDevices() {
  const paired = [];
  const other = [];
  for (const d of devices.values()) {
    (d.paired ? paired : other).push(d);
  }
  // Paired: connected first, then by name.
  paired.sort((a, b) => (b.connected - a.connected)
    || (a.name || a.address).localeCompare(b.name || b.address));
  // Other: by RSSI desc (nulls last), then name.
  other.sort((a, b) => {
    const ar = a.rssi ?? -200, br = b.rssi ?? -200;
    if (ar !== br) return br - ar;
    return (a.name || a.address).localeCompare(b.name || b.address);
  });

  document.getElementById('paired-list').innerHTML = paired.length
    ? paired.map(d => deviceRow(d)).join('')
    : '<div class="empty">No paired devices yet.</div>';
  document.getElementById('other-list').innerHTML = other.length
    ? other.map(d => deviceRow(d)).join('')
    : '<div class="empty">Nothing nearby. Try scanning.</div>';
}

function deviceRow(d) {
  const isPaired = !!d.paired;
  const mutationDisabled = mutationInFlight ? ' disabled' : '';
  const radioActionDisabled = (
    mutationInFlight || state.available === false || state.parked
    || !state.desired || !state.powered
  ) ? ' disabled' : '';
  const canRemoveUnpaired = !isPaired && (
    !!d.connected || !!d.trusted || !!d.servicesResolved
  );
  // Bluez fills Alias with a MAC-shaped string when the remote
  // doesn't broadcast a name — server side filters those out into
  // empty `name`, so we cleanly fall back to a placeholder here
  // (mirrors iPhone's "Unknown" + MAC layout).
  const hasName = !!d.name;
  const label = hasName ? d.name : 'Unknown device';
  // MAC is shown only when there's no friendly name — most users
  // don't care about MACs and showing them on every named device
  // is visual noise. Unknown devices show MAC so they can still
  // be told apart.
  const metaLine = hasName ? '' :
    `<div class="meta">${escapeHtml(d.address)}</div>`;
  let badges = '';
  // BLE HID devices can open a GATT link before pairing. JTS accessory
  // features are not usable until BlueZ has a paired record.
  if ((d.connected || d.trusted) && !d.paired) {
    badges += '<span class="badge linked">Pair required</span>';
  } else if (d.connected && d.servicesResolved === false) {
    badges += '<span class="badge connecting">Connecting</span>';
  } else if (d.connected) {
    badges += '<span class="badge connected">Connected</span>';
  }
  else if (d.paired) badges += '<span class="badge paired">Paired</span>';
  let actions = '';
  if (isPaired) {
    actions = d.connected
      ? `<button class="btn btn--default" data-action="disconnect" data-mac="${escapeHtml(d.address)}"${mutationDisabled}>Disconnect</button>`
      : `<button class="btn btn--primary" data-action="connect" data-mac="${escapeHtml(d.address)}"${radioActionDisabled}>Connect</button>`;
    actions += ` <button class="btn btn--danger" data-action="forget" data-mac="${escapeHtml(d.address)}" data-label="${escapeHtml(label)}"${mutationDisabled}>Forget</button>`;
  } else {
    actions = `<button class="btn btn--primary" data-action="pair" data-mac="${escapeHtml(d.address)}"${radioActionDisabled}>Pair</button>`;
    if (canRemoveUnpaired) {
      actions += ` <button class="btn btn--danger" data-action="forget" data-mac="${escapeHtml(d.address)}" data-label="${escapeHtml(label)}"${mutationDisabled}>Remove</button>`;
    }
  }
  // Metrics. Render each label only when bluez actually has a value
  // for it — surfacing a "—" placeholder suggests we're polling for
  // the value and coming up empty, but in fact bluez doesn't expose
  // RSSI for connected BLE devices at all (they stop advertising
  // once linked, and HCI Read-RSSI is a BT-Classic-only command).
  // Showing nothing is more honest than a perpetual dash.
  let metrics = '';
  if (isPaired) {
    const parts = [];
    if (d.battery !== null && d.battery !== undefined) {
      parts.push(`
        <div class="metric">
          <div class="label">Battery</div>
          <div class="value">${d.battery}%</div>
        </div>`);
    } else if (d.connected && d.batteryCapable) {
      parts.push(`
        <div class="metric">
          <div class="label">Battery</div>
          <div class="value">No reading</div>
        </div>`);
    }
    if (d.rssi !== null && d.rssi !== undefined) {
      parts.push(`
        <div class="metric">
          <div class="label">Signal</div>
          <div class="value"><span class="bars">${rssiBars(d.rssi)}</span></div>
        </div>`);
    }
    if (parts.length) {
      metrics = `<div class="metrics">${parts.join('')}</div>`;
    }
  } else if (d.rssi !== null && d.rssi !== undefined) {
    metrics = `<div class="rssi">${rssiBars(d.rssi)}</div>`;
  }
  return `
    <div class="device" id="d-${cssIdSafe(d.address)}">
      <div class="icon icon-${iconSlug(d.icon)}"></div>
      <div class="info">
        <div class="name">${escapeHtml(label)} ${badges}</div>
        ${metaLine}
        <div id="pair-${cssIdSafe(d.address)}"></div>
      </div>
      ${metrics}
      <div class="actions">${actions}</div>
    </div>
  `;
}

function rssiBars(rssi) {
  if (rssi >= -60) return '●●●●';
  if (rssi >= -75) return '●●●○';
  if (rssi >= -85) return '●●○○';
  return '●○○○';
}

// -------- pair flow --------

async function startPair(mac) {
  if (pairStreams.has(mac)) return; // already pairing this device
  const slot = document.getElementById(`pair-${cssIdSafe(mac)}`);
  if (!slot) return;
  if (!beginMutation()) return;
  slot.innerHTML = `<div class="pair-card" id="pc-${cssIdSafe(mac)}">
    <div class="stage active" id="ps-${cssIdSafe(mac)}-init">
      <span class="spinner"></span> Starting pair…
    </div>
  </div>`;

  try {
    const response = await fetch('pair', {
      method: 'POST', headers: jsonHeaders(),
      body: JSON.stringify({mac}),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || data.message || `Pair request failed (${response.status})`);
    }
  } catch (error) {
    slot.innerHTML = '';
    await jtsAlert(error.message || 'Pair request failed.');
    await finishMutation();
    return;
  }

  let es;
  try {
    es = new EventSource(`pair/${encodeURIComponent(mac)}/stream`);
  } catch (error) {
    slot.innerHTML = '';
    await finishMutation();
    await jtsAlert('Could not open the pairing progress stream.');
    return;
  }
  pairStreams.set(mac, es);
  const card = document.getElementById(`pc-${cssIdSafe(mac)}`);

  es.onmessage = async ev => {
    let data;
    try { data = JSON.parse(ev.data); } catch (e) { return; }
    renderPairStage(mac, data, card);
    if (data.stage === 'ready' || data.stage === 'error') {
      es.close();
      pairStreams.delete(mac);
      await finishMutation();
      // Hide card after a short delay so user can read the final state.
      setTimeout(() => {
        const slot = document.getElementById(`pair-${cssIdSafe(mac)}`);
        if (slot) slot.innerHTML = '';
      }, data.stage === 'error' ? 8000 : 4000);
    }
  };
  es.onerror = async () => {
    es.close();
    pairStreams.delete(mac);
    await finishMutation();
  };
}

function renderPairStage(mac, data, card) {
  if (!card) return;
  const init = document.getElementById(`ps-${cssIdSafe(mac)}-init`);
  if (init) init.remove();
  const stageId = `ps-${cssIdSafe(mac)}-${data.stage}`;
  let stageEl = document.getElementById(stageId);
  if (!stageEl) {
    stageEl = document.createElement('div');
    stageEl.className = 'stage active';
    stageEl.id = stageId;
    card.appendChild(stageEl);
  }
  // Mark previous stage as done (visual cue).
  Array.from(card.querySelectorAll('.stage.active')).forEach(el => {
    if (el !== stageEl) {
      el.classList.remove('active');
      el.classList.add('done');
    }
  });

  if (data.stage === 'starting') {
    stageEl.innerHTML = `<span class="spinner"></span> Starting pair…`;
  } else if (data.stage === 'trusting') {
    stageEl.innerHTML = `<span class="spinner"></span> Trusting…`;
  } else if (data.stage === 'pairing') {
    stageEl.innerHTML = `<span class="spinner"></span> Pairing…`;
  } else if (data.stage === 'paired') {
    stageEl.innerHTML = '✓ Paired';
    stageEl.classList.remove('active');
    stageEl.classList.add('done');
  } else if (data.stage === 'connecting') {
    stageEl.innerHTML = `<span class="spinner"></span> Connecting…`;
  } else if (data.stage === 'wiring') {
    stageEl.innerHTML = `<span class="spinner"></span> ${escapeHtml(data.detail || 'Configuring…')}`;
  } else if (data.stage === 'ready') {
    stageEl.innerHTML = '✓ Ready';
    stageEl.classList.remove('active');
    stageEl.classList.add('done');
    card.classList.add('pair-card--ok');
    if (data.detail) {
      const det = document.createElement('div');
      det.className = 'pair-detail';
      det.textContent = data.detail;
      card.appendChild(det);
    }
  } else if (data.stage === 'error') {
    card.innerHTML = `
      <div class="pair-error-head">
        Pairing failed.
      </div>
      <div>${escapeHtml(data.message || 'Unknown error')}</div>
    `;
    card.classList.add('pair-card--error');
  }
}

// -------- connect / disconnect / forget --------

async function connectDevice(mac, connect) {
  if (!beginMutation()) return;
  const path = connect ? 'connect' : 'disconnect';
  try {
    const response = await fetch(path, {
      method: 'POST', headers: jsonHeaders(),
      body: JSON.stringify({mac}),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      await jtsAlert(`${connect ? 'Connect' : 'Disconnect'} failed: `
        + (data.error || data.message || response.status));
    }
  } catch (error) {
    await jtsAlert(`${connect ? 'Connect' : 'Disconnect'} failed: `
      + (error && error.message ? error.message : 'network error'));
  } finally {
    await finishMutation();
  }
}

async function forget(mac, label) {
  if (!await jtsConfirm(`Remove "${label}" from JTS? You'll need to pair it again to use it.`, {danger: true})) return;
  if (!beginMutation()) return;
  try {
    const r = await fetch('forget', {
      method: 'POST', headers: jsonHeaders(),
      body: JSON.stringify({mac}),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok || data.error) {
      await jtsAlert('Forget failed: ' + (data.error || data.message || r.status));
    }
  } catch (error) {
    await jtsAlert('Forget failed: '
      + (error && error.message ? error.message : 'network error'));
  } finally {
    await finishMutation();
  }
}

document.addEventListener('click', function(e) {
  const actionBtn = e.target.closest('button[data-action]');
  if (actionBtn) {
    const mac = actionBtn.dataset.mac || '';
    if (actionBtn.dataset.action === 'pair') startPair(mac);
    if (actionBtn.dataset.action === 'connect') connectDevice(mac, true);
    if (actionBtn.dataset.action === 'disconnect') connectDevice(mac, false);
    if (actionBtn.dataset.action === 'forget') {
      forget(mac, actionBtn.dataset.label || 'Unknown device');
    }
    return;
  }

});

// The Scan button is server-rendered chrome (no inline onclick); wire it here.
const scanBtn = document.getElementById('scan-btn');
if (scanBtn) scanBtn.addEventListener('click', toggleScan);

// -------- helpers --------

function iconSlug(s) { return String(s || 'device').replace(/[^a-zA-Z0-9_-]/g, '') || 'device'; }

// -------- bootstrap --------

document.getElementById('sw-power').addEventListener('change', togglePower);
document.getElementById('sw-disc').addEventListener('change', toggleDisc);
fetchState();
renderDevices();
startDeviceStream();
schedulePoll(5000);
