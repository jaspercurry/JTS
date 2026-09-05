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

import { jsonHeaders, postJSON, startPolling } from "/assets/shared/js/http.js";
import { jtsConfirm, jtsAlert } from "/assets/shared/js/dialog.js";
import { escapeHtml, cssIdSafe } from "/assets/shared/js/escape.js";
import { toggleScanRequest } from "./scan.js";

let state = {
  desired: false,
  effective: "off",
  available: true,
  parked: false,
  powered: null,
  discoverable: false,
  discovering: false,
};
let devices = new Map(); // path → device
let evtSrc = null;
let pairStreams = new Map(); // mac → EventSource
let deviceMutations = new Map(); // mac → accepted server-owned action
let stopPoll = null;
let pollMs = 0;
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
  if ((mutationInFlight || deviceMutations.size > 0) && !force) return;
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
          powered: null,
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
        powered: null,
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
  const busy = mutationInFlight || deviceMutations.size > 0;
  const power = document.getElementById('sw-power');
  if (!powerIntentUnknown) power.checked = !!state.desired;
  // Missing hardware blocks On, never the safer persisted Off repair.
  power.disabled = busy || powerIntentUnknown || parked
    || (unavailable && !state.desired);
  const sd = document.getElementById('sw-disc');
  sd.checked = !!state.discoverable;
  // Activation needs a ready radio and, since the window exists to accept an
  // inbound bond, a running pairing agent. An already-active window must
  // remain switchable Off as cleanup even when availability later degrades.
  sd.disabled = busy || parked || (!state.discoverable
    && (unavailable || !state.desired || !state.powered
      || state.pairingReady === false));
  let hint;
  if (parked) {
    hint = state.parkReason === "bonded_follower"
      ? 'Managed by this speaker’s stereo pair.'
      : 'Bluetooth is paused while grouping changes settle.';
  } else if (unavailable) {
    hint = state.unavailableReason || state.error || 'Bluetooth state unavailable.';
  } else if (state.effective === 'degraded') {
    hint = state.degradedReason || (state.desired
      ? 'Set to on, but the Bluetooth radio is not ready.'
      : 'Set to off, but the Bluetooth radio is still active.');
  } else if (state.powered === false) {
    hint = 'Off — turn Bluetooth on to manage devices.';
  } else if (state.powered === true) {
    hint = `On — adapter ${state.adapter || 'hci0'}`;
  } else {
    hint = 'Bluetooth radio state unknown.';
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
  btn.disabled = busy || parked || (!scanning
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
  if (ms === pollMs) return;
  pollMs = ms;
  if (stopPoll) stopPoll();
  stopPoll = startPolling(fetchState, { intervalMs: ms });
}

function beginMutation() {
  if (mutationInFlight || deviceMutations.size > 0) return false;
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
    if (applyDeviceEvent(data)) renderDevices();
  };
  evtSrc.onerror = () => {};
}

function applyDeviceEvent(data, target = devices) {
  if (data.action === 'reset') {
    target.clear();
    return true;
  }
  if (!data.device || !data.device.path) return false;
  if (data.action === 'remove') target.delete(data.device.path);
  else target.set(data.device.path, data.device);
  return true;
}

// A device object's uppercased MAC, or a mac string uppercased directly.
function macKey(x) {
  return typeof x === 'string' ? x.toUpperCase() : String(x.address || '').toUpperCase();
}

function visibleDeviceRows(live = devices, mutations = deviceMutations) {
  const rows = Array.from(live.values());
  const present = new Set(rows.map(macKey));
  for (const [mac, mutation] of mutations) {
    if (mutation.device && !present.has(mac)) rows.push(mutation.device);
  }
  return rows;
}

// BlueZ keeps a Device1 object for any device that ever paired or connected
// (non-temporary) and invalidates RSSI on every device once discovery stops
// — so unpaired + no RSSI + not connected + no pending action means BlueZ
// still remembers it, not that it's actually present.
function deviceSection(d, pending = false) {
  if (d.paired) return 'mine';
  if (pending || d.connected || d.trusted || (d.rssi !== null && d.rssi !== undefined)) return 'nearby';
  return null;
}

function renderDevices() {
  const paired = [];
  const other = [];
  for (const d of visibleDeviceRows()) {
    const key = macKey(d);
    const mutation = deviceMutations.get(key);
    const section = deviceSection(d, !!mutation || pairStreams.has(key));
    if (section === 'mine') paired.push({d, pending: mutation});
    else if (section === 'nearby') other.push({d, pending: mutation});
  }
  // Paired: connected first, then by name.
  paired.sort((a, b) => (b.d.connected - a.d.connected)
    || (a.d.name || a.d.address).localeCompare(b.d.name || b.d.address));
  // Other: by RSSI desc (nulls last), then name.
  other.sort((a, b) => {
    const ar = a.d.rssi ?? -200, br = b.d.rssi ?? -200;
    if (ar !== br) return br - ar;
    return (a.d.name || a.d.address).localeCompare(b.d.name || b.d.address);
  });

  document.getElementById('paired-list').innerHTML = paired.length
    ? paired.map(({d, pending}) => deviceRow(d, pending)).join('')
    : '<div class="empty">No paired devices yet.</div>';
  document.getElementById('other-list').innerHTML = other.length
    ? other.map(({d, pending}) => deviceRow(d, pending)).join('')
    : '<div class="empty">Nothing nearby. Try scanning.</div>';
}

function deviceRow(d, pending) {
  const isPaired = !!d.paired;
  const disabled = action => deviceActionDisabled(
    action, state, mutationInFlight || deviceMutations.size > 0,
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
  if (pending) {
    badges += `<span class="badge badge--warn">${deviceMutationLabel(pending.action, true)}</span>`;
  } else if ((d.connected || d.trusted) && !d.paired) {
    badges += '<span class="badge badge--warn">Pair required</span>';
  } else if (d.connected && d.servicesResolved === false) {
    badges += '<span class="badge badge--warn">Connecting</span>';
  } else if (d.connected) {
    badges += '<span class="badge badge--ok">Connected</span>';
  }
  else if (d.paired) badges += '<span class="badge badge--idle">Not connected</span>';
  let actions = '';
  if (pending) {
    actions = `<button class="btn btn--default" disabled>${deviceMutationLabel(pending.action, true)}</button>`;
  } else if (isPaired) {
    actions = d.connected
      ? `<button class="btn btn--default" data-action="disconnect" data-mac="${escapeHtml(d.address)}"${disabled('disconnect')}>Disconnect</button>`
      : `<button class="btn btn--primary" data-action="connect" data-mac="${escapeHtml(d.address)}"${disabled('connect')}>Connect</button>`;
    actions += ` <button class="btn btn--danger" data-action="forget" data-mac="${escapeHtml(d.address)}" data-label="${escapeHtml(label)}"${disabled('forget')}>Forget</button>`;
  } else {
    actions = `<button class="btn btn--primary" data-action="pair" data-mac="${escapeHtml(d.address)}"${disabled('pair')}>Pair</button>`;
    if (canRemoveUnpaired) {
      actions += ` <button class="btn btn--danger" data-action="forget" data-mac="${escapeHtml(d.address)}" data-label="${escapeHtml(label)}"${disabled('forget')}>Remove</button>`;
    }
  }
  return `
    <div class="device" id="d-${cssIdSafe(d.address)}">
      <div class="icon icon-${iconSlug(d.icon)}"></div>
      <div class="info">
        <div class="name">${escapeHtml(label)} ${badges}</div>
        ${metaLine}
        <div id="pair-${cssIdSafe(d.address)}"></div>
      </div>
      ${metricsHtml(d)}
      <div class="actions">${actions}</div>
    </div>
  `;
}

// bluez doesn't expose RSSI for connected BLE devices (they stop
// advertising once linked, and HCI Read-RSSI is BT-Classic-only) — a
// missing metric is omitted rather than shown as a placeholder dash.
function metricsHtml(d) {
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
  return `<div class="metrics">${parts.join('')}</div>`;
}

function deviceActionDisabled(action, currentState, mutationPending) {
  if (mutationPending) return true;
  if (action === 'disconnect' || action === 'forget') {
    return currentState.powered === false;
  }
  // Forming a new bond needs the pairing agent; reconnecting an existing one
  // does not. Gate on the server's verdict, never on degradedReason prose.
  if (action === 'pair' && currentState.pairingReady === false) return true;
  return currentState.available === false || currentState.parked
    || !currentState.desired || currentState.powered !== true;
}

function deviceMutationLabel(action, pending = false) {
  const labels = {
    connect: ['Connect', 'Connecting…'],
    disconnect: ['Disconnect', 'Disconnecting…'],
    forget: ['Forget', 'Removing…'],
  };
  return (labels[action] || ['Bluetooth action', 'Working…'])[pending ? 1 : 0];
}

function deviceMutationOutcome(status) {
  if (status === 'succeeded') return 'success';
  if (status === 'interrupted') return 'unknown';
  return status === 'failed' ? 'failure' : 'pending';
}

function newDeviceMutationId() {
  const words = new Uint32Array(4);
  crypto.getRandomValues(words);
  return Array.from(words, value => value.toString(16).padStart(8, '0')).join('');
}

async function submitDeviceMutation(mutation, options = {}) {
  const post = options.post || (async current => {
    const response = await fetch(current.action, {
      method: 'POST', headers: jsonHeaders(),
      body: JSON.stringify({
        mac: current.mac,
        mutationId: current.mutationId,
      }),
    });
    return {
      ok: response.ok,
      status: response.status,
      data: await response.json().catch(() => ({})),
    };
  });
  let response;
  try {
    response = await post(mutation);
  } catch (error) {
    mutation.stream = `actions/${encodeURIComponent(mutation.mutationId)}/stream`;
    const accept = options.onAccepted || watchDeviceMutation;
    await accept(mutation);
    return 'probing';
  }
  const data = response.data || {};
  const ambiguousServerFailure = !response.ok && response.status >= 500;
  if (!response.ok && !ambiguousServerFailure) {
    const reject = options.onRejected || rejectDeviceMutation;
    await reject(mutation, data.error || data.message || response.status);
    return 'rejected';
  }
  mutation.status = data.status || 'pending';
  mutation.stream = (response.ok && data.stream)
    || `actions/${encodeURIComponent(mutation.mutationId)}/stream`;
  const accept = options.onAccepted || watchDeviceMutation;
  await accept(mutation);
  return data.stream ? 'accepted' : 'probing';
}

async function recoverUnknownDeviceMutation(mutation, options = {}) {
  const refreshDevices = options.refreshDevices || startDeviceStream;
  const drainState = options.drainState || (async () => {
    if (stateFetchPromise !== null) {
      try { await stateFetchPromise; } catch (error) {}
    }
  });
  const refreshState = options.refreshState || (() => fetchState(true));
  const release = options.release || (() => {
    if (deviceMutations.get(mutation.mac) === mutation) {
      deviceMutations.delete(mutation.mac);
      renderToggles();
    }
  });
  const notify = options.notify || (() => jtsAlert(
    `${deviceMutationLabel(mutation.action)} outcome is unknown. `
    + 'Check the current device state before trying again.',
  ));
  refreshDevices();
  await drainState();
  await refreshState();
  await release();
  await notify();
}

function bindDeviceMutationStream(mutation, {
  openStream = path => new EventSource(path),
  onStatus = () => {},
  onTerminal = () => {},
} = {}) {
  const stream = openStream(mutation.stream);
  mutation.eventSource = stream;
  stream.onmessage = async event => {
    let data;
    try { data = JSON.parse(event.data); } catch (error) { return; }
    mutation.status = data.status;
    onStatus(data);
    if (['succeeded', 'failed', 'interrupted'].includes(data.status)) {
      stream.close();
      await onTerminal(data);
    }
  };
  // EventSource reconnects on transport errors. The server still owns the
  // BlueZ call, so a dropped browser stream is not a terminal action state.
  stream.onerror = () => {};
  return stream;
}

function rssiBars(rssi) {
  if (rssi >= -60) return '●●●●';
  if (rssi >= -75) return '●●●○';
  if (rssi >= -85) return '●●○○';
  return '●○○○';
}

// -------- pair flow --------

async function startPair(mac) {
  const key = macKey(mac);
  if (pairStreams.has(key)) return; // already pairing this device
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
  pairStreams.set(key, es);
  const card = document.getElementById(`pc-${cssIdSafe(mac)}`);

  es.onmessage = async ev => {
    let data;
    try { data = JSON.parse(ev.data); } catch (e) { return; }
    renderPairStage(mac, data, card);
    if (data.stage === 'ready' || data.stage === 'error') {
      es.close();
      pairStreams.delete(key);
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
    pairStreams.delete(key);
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

function deviceByAddress(mac) {
  const wanted = macKey(mac);
  return Array.from(devices.values()).find(device => macKey(device) === wanted) || null;
}

function watchDeviceMutation(mutation) {
  try {
    bindDeviceMutationStream(mutation, {
      onStatus() { renderDevices(); },
      async onTerminal(data) {
        if (deviceMutations.get(mutation.mac) !== mutation) return;
        const outcome = deviceMutationOutcome(data.status);
        if (outcome === 'unknown') {
          await recoverUnknownDeviceMutation(mutation);
          return;
        }
        deviceMutations.delete(mutation.mac);
        renderToggles();
        if (outcome === 'failure') {
          await jtsAlert(`${deviceMutationLabel(mutation.action)} failed: `
            + (data.message || 'unknown error'));
        }
      },
    });
  } catch (error) {
    setTimeout(() => {
      if (deviceMutations.get(mutation.mac) === mutation) {
        watchDeviceMutation(mutation);
      }
    }, 2000);
  }
}

async function rejectDeviceMutation(mutation, message) {
  if (deviceMutations.get(mutation.mac) === mutation) {
    deviceMutations.delete(mutation.mac);
    renderToggles();
  }
  await jtsAlert(`${deviceMutationLabel(mutation.action)} failed: ${message}`);
}

function requestDeviceMutation(action, mac) {
  const normalizedMac = macKey(mac);
  if (deviceMutations.has(normalizedMac)) return;
  const mutation = {
    action,
    mac: normalizedMac,
    mutationId: newDeviceMutationId(),
    status: 'accepting',
    stream: '',
    device: deviceByAddress(normalizedMac),
  };
  deviceMutations.set(normalizedMac, mutation);
  renderToggles();
  submitDeviceMutation(mutation);
}

function connectDevice(mac, connect) {
  requestDeviceMutation(connect ? 'connect' : 'disconnect', mac);
}

async function forget(mac, label) {
  if (!await jtsConfirm(`Remove "${label}" from JTS? You'll need to pair it again to use it.`, {danger: true})) return;
  requestDeviceMutation('forget', mac);
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
renderDevices();
startDeviceStream();
schedulePoll(5000);
