// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// main.js — /wifi/ network management (Wi-Fi settings).
//
// This is the live, fetch-driven half of the page: it polls ./state every 7 s,
// runs ./scan on demand, and opens inline Connect / Forget panels that POST to
// ./connect / ./forget / ./radio. The server-rendered markup is just the static
// shell (current-network slot, scan list, join-by-name fields, saved-networks
// collapse); everything that changes at runtime is rendered here.
//
// Relocated verbatim from the page's old inline <script> when /wifi/ moved onto
// the canonical design system. Two seams changed, nothing else:
//   * jsonHeaders() comes from the shared http.js module; it reads the CSRF
//     token from the <meta name="jts-csrf"> tag and attaches X-CSRF-Token to
//     every mutating POST.
//   * jtsConfirm / jtsAlert come from the shared dialog.js module — never
//     window.confirm/alert, which the
//     browser can suppress (that silently defeated the radio-kill guard).
//
// SSIDs and NM profile names are UNTRUSTED. Every value reaches the DOM only
// as an h() text child or a DOM property (never innerHTML); per-row
// Connect/Forget targets ride in data-ssid / data-name attributes set
// through h() props, read by one delegated click handler — never inline
// onclick with a network name.
//
// Lockout safety (preserved exactly): toggleRadio() blocks turning Wi-Fi off
// behind a stark caps-lock jtsConfirm when the Pi has no Ethernet fallback;
// connect flows surface the rollback warning; the server still rolls the
// previous profile back up on a failed connect.

import { jsonHeaders, startPolling } from "/assets/shared/js/http.js";
import { jtsConfirm, jtsAlert } from "/assets/shared/js/dialog.js";
import { cssIdSafe } from "/assets/shared/js/escape.js";
import { h, appendChildren } from "/assets/shared/js/dom.js";

// State + DOM helpers ------------------------------------------------
let state = { adapterPresent: true, radioOn: false, hasEthernet: false,
              lockoutRisk: "high", current: null, saved: [] };
let scanResults = [];
let scanHealth = null;
let scanning = false;
let hasScanned = false;
let autoScanned = false;
let openSsid = null;     // available-list inline panel currently open
let openSavedName = null;// saved-list inline panel currently open
function signalBars(sig) {
  if (sig == null) return '';
  if (sig >= 70) return '●●●●';
  if (sig >= 50) return '●●●○';
  if (sig >= 30) return '●●○○';
  if (sig >= 10) return '●○○○';
  return '○○○○';
}

// State fetch + render -----------------------------------------------
async function fetchState() {
  try {
    const r = await fetch('./state', { cache: 'no-store' });
    state = await r.json();
    renderCurrent();
    renderScanHealth();
    renderSaved();
    maybeAutoScan();
  } catch (e) {
    document.getElementById('current').replaceChildren(
      h("div.current-card.disconnected", null,
        h("div.ssid", null, "Status unavailable"),
        h("div.meta", null, "Could not reach the Wi-Fi backend.")));
  }
}

function maybeAutoScan() {
  if (autoScanned || scanning || !state.adapterPresent || !state.radioOn) return;
  autoScanned = true;
  rescan();
}

function renderCurrent() {
  const wrap = document.getElementById('current');
  if (!state.adapterPresent) {
    wrap.replaceChildren(h("div.current-card.disconnected", null,
      h("div.ssid", null, "No Wi-Fi adapter detected"),
      h("div.meta", null,
        "This Pi has no wireless interface NetworkManager can drive.")));
    return;
  }

  const cur = state.current;
  const cardTag = cur ? "div.current-card" : "div.current-card.disconnected";

  const inner = [];
  if (cur) {
    const bars = cur.signal != null ? signalBars(cur.signal) : '';
    inner.push(h("div.ssid", null, cur.ssid, "  ", h("span.bars", null, bars)));
    const metaRows = [];
    if (cur.ip) {
      metaRows.push(h("div.row", null,
        h("span.key", null, "IP"), h("span.val", null, cur.ip)));
    }
    metaRows.push(h("div.row", null,
      h("span.key", null, "Security"), h("span.val", null, cur.security)));
    if (cur.signal != null) {
      metaRows.push(h("div.row", null,
        h("span.key", null, "Signal"), h("span.val", null, `${cur.signal} / 100`)));
    }
    inner.push(h("div.meta", null, ...metaRows));
  } else if (!state.radioOn) {
    inner.push(h("div.ssid", null, "Wi-Fi is off"));
    inner.push(h("div.meta", null, "Turn Wi-Fi on to scan and connect."));
  } else {
    inner.push(h("div.ssid", null, "Not connected"));
    inner.push(h("div.meta", null, "No active Wi-Fi connection."));
  }

  // Radio toggle row. No always-visible warning copy — the lockout
  // warning ONLY appears in the confirm() dialog that fires when the
  // user actually tries to turn the radio off (see toggleRadio()).
  // Persistent red copy here just spooks people who weren't going to
  // touch it.
  inner.push(h("div.radio-row", null,
    h("div.label", null, "Wi-Fi radio"),
    h("label.toggle", null,
      h("input#radio-toggle", {
        type: "checkbox", "attr:aria-label": "Wi-Fi radio", checked: state.radioOn,
      }),
      h("span.track"))));

  wrap.className = '';
  wrap.replaceChildren(h(cardTag, null, ...inner));
  const radioToggle = document.getElementById('radio-toggle');
  if (radioToggle) radioToggle.addEventListener('change', toggleRadio);
}

function renderSaved() {
  const list = document.getElementById('saved-list');
  const countEl = document.getElementById('saved-count');
  const saved = state.saved || [];
  countEl.textContent = saved.length ? `(${saved.length})` : '(none)';
  if (!saved.length) {
    list.replaceChildren(h("div.empty", null, "No saved networks yet."));
    return;
  }
  const curName = state.current ? state.current.profileName : null;
  list.replaceChildren(...saved.map(p => savedRow(p, curName)));

  // Re-open any panel that was open before this render so the user's
  // in-flight Forget confirmation isn't yanked by a poll.
  if (openSavedName) {
    openForget(openSavedName, /*keepOpen*/true);
  }
}

function savedRow(p, curName) {
  const isCurrent = p.name === curName;
  const idsafe = cssIdSafe(p.name);
  // Display the SSID (what the user knows the network as); the
  // profile NAME goes through the API as the operate-on key.
  return h(`div.net-row#sv-${idsafe}`, null,
    h("div.head", null,
      h("div.info", null,
        h("div.ssid", null, p.ssid || p.name,
          isCurrent ? h("span.badge.badge--ok", null, "In use") : null)),
      h("div.actions", null,
        h("button.btn.btn--danger", {
          "data-action": "open-forget", "data-name": p.name,
        }, "Forget"))),
    h(`div#sv-panel-${idsafe}`));
}

// Available networks list --------------------------------------------
function renderScanHealth() {
  const box = document.getElementById('scan-health');
  const btn = document.getElementById('scan-btn');
  if (!box) return;
  if (btn) {
    btn.hidden = !!(scanHealth && scanHealth.hideScanButton);
  }
  box.replaceChildren();
  if (!scanHealth) return;
  const debug = scanHealth.debug || {};
  if (scanHealth.degraded) {
    let msg = 'Wi-Fi scanning looks degraded. ';
    if (scanHealth.reason === 'driver_scan_suppressed') {
      msg += 'The Pi radio is reporting scan suppression, so nearby networks may not appear.';
    } else {
      msg += 'The scan command did not complete cleanly.';
    }
    msg += ' Join by name still works and keeps rollback enabled.';
    box.replaceChildren(h("div.scan-note.warn", null, msg));
    return;
  }
  if (scanHealth.suspect || debug.onlyCurrentNetwork) {
    box.replaceChildren(h("div.scan-note", null,
      "Scan only found the current network. Join by name is available below."));
  }
}

function renderAvail() {
  const list = document.getElementById('avail-list');
  if (!scanResults.length) {
    let msg = 'Tap Scan to look for nearby networks.';
    if (scanning) {
      msg = 'Scanning…';
    } else if (hasScanned && scanHealth && scanHealth.degraded) {
      msg = 'Scan degraded. Join by name is available below.';
    } else if (hasScanned) {
      msg = 'No other networks found.';
    }
    list.replaceChildren(h("div.empty", null, msg));
    return;
  }
  list.replaceChildren(...scanResults.map(availRow));
  // Re-open any panel that was open before this render.
  if (openSsid) {
    openConnect(openSsid, /*keepOpen*/true);
  }
}

function availRow(n) {
  const idsafe = cssIdSafe(n.ssid);
  return h(`div.net-row#av-${idsafe}`, null,
    h("div.head", { "data-action": "open-connect", "data-ssid": n.ssid },
      h("div.info", null,
        h("div.ssid", null, n.ssid, n.secured ? ' 🔒' : '',
          n.inUse ? h("span.badge.badge--ok", null, "Connected") : null),
        h("div.meta", null, n.security, ` · ch ${n.channel}`)),
      h("div.signal", null, signalBars(n.signal))),
    h(`div#av-panel-${idsafe}`));
}

// Scan ---------------------------------------------------------------
async function rescan() {
  if (scanning) return;
  if (!state.radioOn) {
    jtsAlert('Turn Wi-Fi on first.');
    return;
  }
  scanning = true;
  const btn = document.getElementById('scan-btn');
  btn.classList.add('scanning');
  btn.replaceChildren();
  appendChildren(btn, [h("span.spinner.spinner--button"), "Scanning"]);
  btn.disabled = true;
  renderAvail();
  try {
    const r = await fetch('./scan', {
      method: 'POST',
      headers: jsonHeaders(),
      body: '{}',
    });
    const data = await r.json();
    scanResults = data.networks || [];
    scanHealth = data.scan || null;
  } catch (e) {
    scanResults = [];
    scanHealth = {
      degraded: true,
      reason: 'request_failed',
      hideScanButton: false,
      debug: {},
    };
  } finally {
    hasScanned = true;
    scanning = false;
    btn.classList.remove('scanning');
    btn.textContent = 'Scan';
    btn.disabled = false;
    renderScanHealth();
    renderAvail();
  }
}

// Connect panel ------------------------------------------------------
function connectRiskWarningEl() {
  if (state.lockoutRisk === 'high' && state.current) {
    return h("div.warn", null,
      h("span.lead", null, "⚠ Lockout risk:"),
      ` You're reaching this page over Wi-Fi and the Pi has no Ethernet fallback. If the new network fails, the Pi will try to reconnect to ${state.current.ssid} automatically. The full switch and recovery attempt can take up to 3 minutes. If that also fails you'll need physical access to recover.`);
  }
  if (state.current) {
    return h("div.warn", null,
      `Switching from ${state.current.ssid}. Connection will drop briefly — page will reload.`);
  }
  return null;
}

// Shared "<result err>" panel with a Dismiss button, used by both the
// connect and forget failure paths.
function errorPanel(message, action, dataAttr, dataValue) {
  return h("div.panel", null,
    h("div.result.err", null, message),
    h("div.btns", null,
      h("button.btn.btn--ghost", { "data-action": action, [dataAttr]: dataValue },
        "Dismiss")));
}

async function confirmManualLockoutRisk(ssid) {
  if (!(state.lockoutRisk === 'high' && state.current)) return true;
  return await jtsConfirm(
    'You are reaching this page over Wi-Fi and the Pi has no Ethernet fallback.\n\n' +
    'It will try to connect to "' + ssid + '". If that fails, it will roll back to "' +
    state.current.ssid + '". If rollback also fails, you may need physical access.\n\n' +
    'Continue?',
    {danger: true},
  );
}

function openConnect(ssid, keepOpen) {
  // Close any other open connect panel.
  if (openSsid && openSsid !== ssid && !keepOpen) {
    const prev = document.getElementById('av-panel-' + cssIdSafe(openSsid));
    if (prev) prev.replaceChildren();
  }
  openSsid = ssid;
  const slot = document.getElementById('av-panel-' + cssIdSafe(ssid));
  if (!slot) return;
  // Don't trash a panel that's mid-flight (showing a spinner / result).
  if (slot.dataset.locked === '1') return;

  const net = scanResults.find(n => n.ssid === ssid);
  if (!net) { slot.replaceChildren(); return; }

  const idsafe = cssIdSafe(ssid);

  const pwBlock = net.secured
    ? [
        h("label", { for: `pw-${idsafe}` }, "Password"),
        h(`input#pw-${idsafe}`, {
          type: "password", autocomplete: "off",
          "attr:autocapitalize": "off", "attr:spellcheck": "false",
        }),
        h("span.show-pw", { "data-action": "toggle-pw", "data-ssid": ssid },
          "Show password"),
      ]
    : [h("div.meta.open-note", null, "Open network — no password required.")];

  slot.replaceChildren(h(`div.panel#panel-${idsafe}`, null,
    connectRiskWarningEl(),
    ...pwBlock,
    h("div.btns", null,
      h("button.btn.btn--primary", {
        "data-action": "submit-connect", "data-ssid": ssid,
        "data-secured": net.secured ? "true" : "false",
      }, "Connect"),
      h("button.btn.btn--ghost", { "data-action": "close-connect", "data-ssid": ssid },
        "Cancel"))));
}

function togglePw(ssid) {
  const input = document.getElementById('pw-' + cssIdSafe(ssid));
  if (!input) return;
  input.type = input.type === 'password' ? 'text' : 'password';
}

function closeConnect(ssid) {
  if (openSsid === ssid) openSsid = null;
  const slot = document.getElementById('av-panel-' + cssIdSafe(ssid));
  if (slot && slot.dataset.locked !== '1') slot.replaceChildren();
}

async function submitConnect(ssid, secured) {
  const slot = document.getElementById('av-panel-' + cssIdSafe(ssid));
  if (!slot) return;
  let password = null;
  if (secured) {
    const input = document.getElementById('pw-' + cssIdSafe(ssid));
    password = input ? input.value : '';
    if (!password) {
      jtsAlert('Enter the password first.');
      return;
    }
  }
  slot.dataset.locked = '1';
  slot.replaceChildren(h("div.panel", null, h("div", null,
    h("span.spinner"), ` Connecting to ${ssid}… `,
    h("span.hint", null, "(up to 3 minutes including rollback)"))));
  try {
    const r = await fetch('./connect', {
      method: 'POST',
      headers: jsonHeaders(),
      body: JSON.stringify(password === null ? {ssid: ssid} : {ssid: ssid, password: password}),
    });
    const data = await r.json();
    if (r.ok && data.ok) {
      slot.replaceChildren(h("div.panel", null,
        h("div.result.ok", null, `✓ ${data.message || 'Connected'}`)));
      // Force a state refresh so the current-network card updates.
      setTimeout(fetchState, 500);
      // Clear the lock after a moment so the user can dismiss.
      setTimeout(function() {
        slot.dataset.locked = '';
        openSsid = null;
        slot.replaceChildren();
      }, 3000);
    } else {
      slot.replaceChildren(errorPanel(
        data.message || data.error || 'Connection failed', 'dismiss-connect', 'data-ssid', ssid));
      slot.dataset.locked = '1';
      setTimeout(fetchState, 500);
    }
  } catch (e) {
    slot.replaceChildren(errorPanel(
      'Network error talking to the Wi-Fi backend.', 'dismiss-connect', 'data-ssid', ssid));
    slot.dataset.locked = '1';
  }
}

function dismissPanel(ssid) {
  const slot = document.getElementById('av-panel-' + cssIdSafe(ssid));
  if (slot) {
    slot.dataset.locked = '';
    slot.replaceChildren();
  }
  if (openSsid === ssid) openSsid = null;
}

// Manual join --------------------------------------------------------
function toggleManualPw() {
  const input = document.getElementById('manual-password');
  if (!input) return;
  input.type = input.type === 'password' ? 'text' : 'password';
}

async function submitManualConnect() {
  if (!state.radioOn) {
    jtsAlert('Turn Wi-Fi on first.');
    return;
  }
  const ssidEl = document.getElementById('manual-ssid');
  const pwEl = document.getElementById('manual-password');
  const hiddenEl = document.getElementById('manual-hidden');
  const result = document.getElementById('manual-result');
  const btn = document.getElementById('manual-connect-btn');
  const ssid = (ssidEl ? ssidEl.value : '').trim();
  const password = pwEl ? pwEl.value : '';
  const hidden = hiddenEl ? hiddenEl.checked : false;
  if (!ssid) {
    jtsAlert('Enter the network name first.');
    return;
  }
  if (!await confirmManualLockoutRisk(ssid)) return;

  const payload = {ssid: ssid, hidden: hidden};
  if (password) payload.password = password;
  if (btn) btn.disabled = true;
  result.replaceChildren(h("div", null,
    h("span.spinner"), ` Connecting to ${ssid}… `,
    h("span.hint", null, "(up to 3 minutes including rollback)")));
  try {
    const r = await fetch('./connect', {
      method: 'POST',
      headers: jsonHeaders(),
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (r.ok && data.ok) {
      result.replaceChildren(h("div.result.ok", null, `✓ ${data.message || 'Connected'}`));
      setTimeout(fetchState, 500);
    } else {
      result.replaceChildren(h("div.result.err", null,
        data.message || data.error || 'Connection failed'));
      setTimeout(fetchState, 500);
    }
  } catch (e) {
    result.replaceChildren(h("div.result.err", null,
      "Network error talking to the Wi-Fi backend."));
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Forget panel -------------------------------------------------------
function openForget(name, keepOpen) {
  if (openSavedName && openSavedName !== name && !keepOpen) {
    const prev = document.getElementById('sv-panel-' + cssIdSafe(openSavedName));
    if (prev) prev.replaceChildren();
  }
  openSavedName = name;
  const slot = document.getElementById('sv-panel-' + cssIdSafe(name));
  if (!slot) return;
  if (slot.dataset.locked === '1') return;

  const isCurrent = state.current && state.current.profileName === name;
  // Look up the SSID for the panel copy — name is the NM profile name
  // which can be a hostile string for netplan-seeded profiles.
  const profile = (state.saved || []).find(p => p.name === name);
  const displayName = (profile && profile.ssid) || name;
  const extra = isCurrent
    ? h("div.warn.kill", null,
        "⚠ This is the network the Pi is currently using. Forgetting it will disconnect Wi-Fi."
        + (state.hasEthernet
          ? " (Ethernet is connected so the Pi stays reachable.)"
          : " Pi has no Ethernet fallback — you may lose access."))
    : null;

  slot.replaceChildren(h("div.panel", null,
    extra,
    h("div", null, "Forget ", h("strong", null, displayName),
      "? You'll need the password again to reconnect."),
    h("div.btns", null,
      h("button.btn.btn--danger", { "data-action": "submit-forget", "data-name": name },
        "Forget"),
      h("button.btn.btn--ghost", { "data-action": "close-forget", "data-name": name },
        "Cancel"))));
}

function closeForget(name) {
  if (openSavedName === name) openSavedName = null;
  const slot = document.getElementById('sv-panel-' + cssIdSafe(name));
  if (slot && slot.dataset.locked !== '1') slot.replaceChildren();
}

async function submitForget(name) {
  const slot = document.getElementById('sv-panel-' + cssIdSafe(name));
  if (!slot) return;
  slot.dataset.locked = '1';
  slot.replaceChildren(h("div.panel", null,
    h("div", null, h("span.spinner"), " Forgetting…")));
  try {
    const r = await fetch('./forget', {
      method: 'POST',
      headers: jsonHeaders(),
      body: JSON.stringify({name: name}),
    });
    const data = await r.json();
    if (r.ok && data.ok) {
      slot.replaceChildren(h("div.panel", null,
        h("div.result.ok", null, `✓ ${data.message || 'Forgotten'}`)));
      setTimeout(function() {
        slot.dataset.locked = '';
        openSavedName = null;
        fetchState();
      }, 800);
    } else {
      slot.replaceChildren(errorPanel(
        data.message || data.error || 'Failed', 'dismiss-forget', 'data-name', name));
    }
  } catch (e) {
    slot.replaceChildren(errorPanel(
      'Network error talking to the Wi-Fi backend.', 'dismiss-forget', 'data-name', name));
  }
}

function dismissForget(name) {
  const slot = document.getElementById('sv-panel-' + cssIdSafe(name));
  if (slot) { slot.dataset.locked = ''; slot.replaceChildren(); }
  if (openSavedName === name) openSavedName = null;
}

// Radio toggle -------------------------------------------------------
async function toggleRadio() {
  const input = document.getElementById('radio-toggle');
  const previous = !!state.radioOn;
  const target = input ? !!input.checked : !previous;
  function restoreToggle() {
    if (input) input.checked = previous;
  }
  if (target === previous) return;
  // Off path: the kill warning. We block in two places: when there's
  // no ethernet (existential — the user loses access), and otherwise
  // a milder confirm (annoying but recoverable).
  if (!target) {
    if (!state.hasEthernet) {
      const ok = await jtsConfirm(
        '⚠ TURNING WI-FI OFF WILL DISCONNECT THIS PI.\n\n' +
        'You are reaching this page over Wi-Fi and the Pi has no ' +
        'Ethernet plugged in. As soon as Wi-Fi turns off, this page ' +
        'will stop responding and the ONLY way to turn it back on ' +
        'will be to physically access the Pi (plug in Ethernet or ' +
        'use a keyboard and monitor).\n\n' +
        'Continue?',
        {danger: true},
      );
      if (!ok) {
        restoreToggle();
        return;
      }
    } else {
      const ok = await jtsConfirm(
        'Turn Wi-Fi off? The Pi will stay reachable on Ethernet, ' +
        'but any Wi-Fi-only renderers (AirPlay from a phone, etc.) ' +
        'will disconnect.',
        {danger: true},
      );
      if (!ok) {
        restoreToggle();
        return;
      }
    }
  }
  try {
    const r = await fetch('./radio', {
      method: 'POST',
      headers: jsonHeaders(),
      body: JSON.stringify({on: target}),
    });
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      restoreToggle();
      jtsAlert('Radio toggle failed: ' + (data.message || data.error || r.status));
    }
  } catch (e) {
    // If we just turned off Wi-Fi and there's no ethernet, the fetch
    // never returns — that's expected. Don't alert.
    if (target || state.hasEthernet) {
      restoreToggle();
      jtsAlert('Network error talking to the Wi-Fi backend.');
    }
  }
  setTimeout(fetchState, 600);
}

// Bootstrap ----------------------------------------------------------
// One delegated click handler for every data-action control. Per-row
// Connect/Forget targets ride in escaped data-ssid / data-name attributes,
// so an SSID never lands inside an inline onclick. The page-level controls
// (Scan, manual Connect, Show password) use the same mechanism.
document.addEventListener('click', function(e) {
  const el = e.target.closest('[data-action]');
  if (!el) return;
  const action = el.dataset.action;
  if (action === 'rescan') rescan();
  if (action === 'submit-manual') submitManualConnect();
  if (action === 'toggle-manual-pw') toggleManualPw();
  if (action === 'open-connect') openConnect(el.dataset.ssid || '');
  if (action === 'toggle-pw') togglePw(el.dataset.ssid || '');
  if (action === 'submit-connect') {
    submitConnect(el.dataset.ssid || '', el.dataset.secured === 'true');
  }
  if (action === 'close-connect') closeConnect(el.dataset.ssid || '');
  if (action === 'dismiss-connect') dismissPanel(el.dataset.ssid || '');
  if (action === 'open-forget') openForget(el.dataset.name || '');
  if (action === 'submit-forget') submitForget(el.dataset.name || '');
  if (action === 'close-forget') closeForget(el.dataset.name || '');
  if (action === 'dismiss-forget') dismissForget(el.dataset.name || '');
});
startPolling(fetchState, { intervalMs: 7000 });
