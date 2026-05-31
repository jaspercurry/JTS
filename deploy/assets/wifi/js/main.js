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
//   * jsonHeaders() now comes from the shared http.js module (was injected as
//     csrf_fetch_helpers_js); it reads the CSRF token from the <meta name=
//     "jts-csrf"> tag and attaches X-CSRF-Token to every mutating POST.
//   * jtsConfirm / jtsAlert come from the shared dialog.js module (was the
//     inline dialog_helpers_js twin) — never window.confirm/alert, which the
//     browser can suppress (that silently defeated the radio-kill guard).
//
// SSIDs and NM profile names are UNTRUSTED. Every interpolation into innerHTML
// goes through escapeHtml(); per-row Connect/Forget targets ride in escaped
// data-ssid / data-name attributes read by one delegated click handler — never
// inline onclick with a network name.
//
// Lockout safety (preserved exactly): toggleRadio() blocks turning Wi-Fi off
// behind a stark caps-lock jtsConfirm when the Pi has no Ethernet fallback;
// connect flows surface the rollback warning; the server still rolls the
// previous profile back up on a failed connect.

import { jsonHeaders } from "/assets/shared/js/http.js";
import { jtsConfirm, jtsAlert } from "/assets/shared/js/dialog.js";

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
let stateTimer = null;
function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}
function cssIdSafe(s) { return String(s).replace(/[^a-zA-Z0-9]/g, '_'); }
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
    document.getElementById('current').innerHTML =
      '<div class="current-card disconnected">' +
      '<div class="ssid">Status unavailable</div>' +
      '<div class="meta">Could not reach the Wi-Fi backend.</div>' +
      '</div>';
  }
}

function maybeAutoScan() {
  if (autoScanned || scanning || !state.adapterPresent || !state.radioOn) return;
  autoScanned = true;
  rescan();
}

function schedulePoll(ms) {
  if (stateTimer !== null) clearInterval(stateTimer);
  stateTimer = setInterval(fetchState, ms);
}

function renderCurrent() {
  const wrap = document.getElementById('current');
  if (!state.adapterPresent) {
    wrap.innerHTML = '<div class="current-card disconnected">' +
      '<div class="ssid">No Wi-Fi adapter detected</div>' +
      '<div class="meta">This Pi has no wireless interface ' +
      'NetworkManager can drive.</div></div>';
    return;
  }

  const cur = state.current;
  const cardClass = cur ? 'current-card' : 'current-card disconnected';

  let inner = '';
  if (cur) {
    const bars = cur.signal != null ? signalBars(cur.signal) : '';
    inner += '<div class="ssid">' + escapeHtml(cur.ssid) +
             '  <span class="bars">' +
             bars + '</span></div>';
    inner += '<div class="meta">';
    if (cur.ip) {
      inner += '<div class="row"><span class="key">IP</span>' +
               '<span class="val">' + escapeHtml(cur.ip) + '</span></div>';
    }
    inner += '<div class="row"><span class="key">Security</span>' +
             '<span class="val">' + escapeHtml(cur.security) + '</span></div>';
    if (cur.signal != null) {
      inner += '<div class="row"><span class="key">Signal</span>' +
               '<span class="val">' + cur.signal + ' / 100</span></div>';
    }
    inner += '</div>';
  } else if (!state.radioOn) {
    inner += '<div class="ssid">Wi-Fi is off</div>';
    inner += '<div class="meta">Turn Wi-Fi on to scan and connect.</div>';
  } else {
    inner += '<div class="ssid">Not connected</div>';
    inner += '<div class="meta">No active Wi-Fi connection.</div>';
  }

  // Radio toggle row. No always-visible warning copy — the lockout
  // warning ONLY appears in the confirm() dialog that fires when the
  // user actually tries to turn the radio off (see toggleRadio()).
  // Persistent red copy here just spooks people who weren't going to
  // touch it.
  const checked = state.radioOn ? ' checked' : '';
  inner += '<div class="radio-row">' +
           '  <div class="label">Wi-Fi radio</div>' +
           '  <label class="toggle">' +
           '    <input type="checkbox" id="radio-toggle" ' +
                  'aria-label="Wi-Fi radio"' + checked + '>' +
           '    <span class="track"></span>' +
           '  </label>' +
           '</div>';

  wrap.className = '';
  wrap.innerHTML = '<div class="' + cardClass + '">' + inner + '</div>';
  const radioToggle = document.getElementById('radio-toggle');
  if (radioToggle) radioToggle.addEventListener('change', toggleRadio);
}

function renderSaved() {
  const list = document.getElementById('saved-list');
  const countEl = document.getElementById('saved-count');
  const saved = state.saved || [];
  countEl.textContent = saved.length ? '(' + saved.length + ')' : '(none)';
  if (!saved.length) {
    list.innerHTML = '<div class="empty">No saved networks yet.</div>';
    return;
  }
  const curName = state.current ? state.current.profileName : null;
  list.innerHTML = saved.map(p => {
    const isCurrent = p.name === curName;
    const idsafe = cssIdSafe(p.name);
    const badge = isCurrent
      ? '<span class="badge">In use</span>' : '';
    // Display the SSID (what the user knows the network as); the
    // profile NAME goes through the API as the operate-on key.
    return '<div class="net-row" id="sv-' + idsafe + '">' +
      '<div class="head">' +
      '  <div class="info">' +
      '    <div class="ssid">' + escapeHtml(p.ssid || p.name) + badge + '</div>' +
      '  </div>' +
      '  <div class="actions">' +
      '    <button class="btn btn--danger" data-action="open-forget" ' +
             'data-name="' + escapeHtml(p.name) + '">Forget</button>' +
      '  </div>' +
      '</div>' +
      '<div id="sv-panel-' + idsafe + '"></div>' +
      '</div>';
  }).join('');

  // Re-open any panel that was open before this render so the user's
  // in-flight Forget confirmation isn't yanked by a poll.
  if (openSavedName) {
    openForget(openSavedName, /*keepOpen*/true);
  }
}

// Available networks list --------------------------------------------
function renderScanHealth() {
  const box = document.getElementById('scan-health');
  const btn = document.getElementById('scan-btn');
  if (!box) return;
  if (btn) {
    btn.style.display = scanHealth && scanHealth.hideScanButton ? 'none' : '';
  }
  if (!scanHealth) {
    box.innerHTML = '';
    return;
  }
  const debug = scanHealth.debug || {};
  if (scanHealth.degraded) {
    let msg = 'Wi-Fi scanning looks degraded. ';
    if (scanHealth.reason === 'driver_scan_suppressed') {
      msg += 'The Pi radio is reporting scan suppression, so nearby networks may not appear.';
    } else {
      msg += 'The scan command did not complete cleanly.';
    }
    msg += ' Join by name still works and keeps rollback enabled.';
    box.innerHTML = '<div class="scan-note warn">' + escapeHtml(msg) + '</div>';
    return;
  }
  if (scanHealth.suspect || debug.onlyCurrentNetwork) {
    box.innerHTML = '<div class="scan-note">Scan only found the current network. Join by name is available below.</div>';
    return;
  }
  box.innerHTML = '';
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
    list.innerHTML = '<div class="empty">' + escapeHtml(msg) + '</div>';
    return;
  }
  list.innerHTML = scanResults.map(n => {
    const idsafe = cssIdSafe(n.ssid);
    const lock = n.secured ? ' 🔒' : '';
    const inUseBadge = n.inUse ? '<span class="badge">Connected</span>' : '';
    return '<div class="net-row" id="av-' + idsafe + '">' +
      '<div class="head" data-action="open-connect" ' +
           'data-ssid="' + escapeHtml(n.ssid) + '">' +
      '  <div class="info">' +
      '    <div class="ssid">' + escapeHtml(n.ssid) + lock + inUseBadge + '</div>' +
      '    <div class="meta">' + escapeHtml(n.security) +
              ' · ch ' + escapeHtml(n.channel) + '</div>' +
      '  </div>' +
      '  <div class="signal">' + signalBars(n.signal) + '</div>' +
      '</div>' +
      '<div id="av-panel-' + idsafe + '"></div>' +
      '</div>';
  }).join('');
  // Re-open any panel that was open before this render.
  if (openSsid) {
    openConnect(openSsid, /*keepOpen*/true);
  }
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
  btn.innerHTML = '<span class="btn-spinner"></span>Scanning';
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
    btn.innerHTML = 'Scan';
    btn.disabled = false;
    renderScanHealth();
    renderAvail();
  }
}

// Connect panel ------------------------------------------------------
function connectRiskWarningHtml() {
  return (state.lockoutRisk === 'high' && state.current)
    ? '<div class="warn"><span class="lead">⚠ Lockout risk:</span>' +
      ' You\'re reaching this page over Wi-Fi and the Pi has no ' +
      'Ethernet fallback. If the new network fails, the Pi will try ' +
      'to reconnect to ' + escapeHtml(state.current.ssid) +
      ' automatically (90s timeout). If that also fails you\'ll need ' +
      'physical access to recover.</div>'
    : (state.current
        ? '<div class="warn">Switching from ' +
          escapeHtml(state.current.ssid) + '. Connection will ' +
          'drop briefly — page will reload.</div>'
        : '');
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
    if (prev) prev.innerHTML = '';
  }
  openSsid = ssid;
  const slot = document.getElementById('av-panel-' + cssIdSafe(ssid));
  if (!slot) return;
  // Don't trash a panel that's mid-flight (showing a spinner / result).
  if (slot.dataset.locked === '1') return;

  const net = scanResults.find(n => n.ssid === ssid);
  if (!net) { slot.innerHTML = ''; return; }

  const idsafe = cssIdSafe(ssid);
  const warn = connectRiskWarningHtml();

  let pwBlock = '';
  if (net.secured) {
    pwBlock =
      '<label for="pw-' + idsafe + '">Password</label>' +
      '<input id="pw-' + idsafe + '" type="password" autocomplete="off" autocapitalize="off" spellcheck="false">' +
      '<span class="show-pw" data-action="toggle-pw" ' +
           'data-ssid="' + escapeHtml(ssid) + '">' +
      'Show password</span>';
  } else {
    pwBlock = '<div class="meta open-note">Open network — no password required.</div>';
  }

  slot.innerHTML =
    '<div class="panel" id="panel-' + idsafe + '">' +
    warn +
    pwBlock +
    '<div class="btns">' +
    '  <button class="btn btn--primary" data-action="submit-connect" ' +
          'data-ssid="' + escapeHtml(ssid) + '" ' +
          'data-secured="' + (net.secured ? 'true' : 'false') + '">Connect</button>' +
    '  <button class="btn btn--ghost" data-action="close-connect" ' +
          'data-ssid="' + escapeHtml(ssid) + '">Cancel</button>' +
    '</div>' +
    '</div>';
}

function togglePw(ssid) {
  const input = document.getElementById('pw-' + cssIdSafe(ssid));
  if (!input) return;
  input.type = input.type === 'password' ? 'text' : 'password';
}

function closeConnect(ssid) {
  if (openSsid === ssid) openSsid = null;
  const slot = document.getElementById('av-panel-' + cssIdSafe(ssid));
  if (slot && slot.dataset.locked !== '1') slot.innerHTML = '';
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
  slot.innerHTML =
    '<div class="panel"><div><span class="spinner"></span> ' +
    'Connecting to ' + escapeHtml(ssid) + '… ' +
    '<span class="hint">' +
    '(up to 90s including rollback)</span></div></div>';
  try {
    const r = await fetch('./connect', {
      method: 'POST',
      headers: jsonHeaders(),
      body: JSON.stringify(password === null ? {ssid: ssid} : {ssid: ssid, password: password}),
    });
    const data = await r.json();
    if (r.ok && data.ok) {
      slot.innerHTML =
        '<div class="panel"><div class="result ok">✓ ' +
        escapeHtml(data.message || 'Connected') + '</div></div>';
      // Force a state refresh so the current-network card updates.
      setTimeout(fetchState, 500);
      // Clear the lock after a moment so the user can dismiss.
      setTimeout(function() {
        slot.dataset.locked = '';
        openSsid = null;
        slot.innerHTML = '';
      }, 3000);
    } else {
      slot.innerHTML =
        '<div class="panel"><div class="result err">' +
        escapeHtml(data.message || data.error || 'Connection failed') +
        '</div><div class="btns"><button class="btn btn--ghost" ' +
        'data-action="dismiss-connect" ' +
        'data-ssid="' + escapeHtml(ssid) + '">Dismiss</button>' +
        '</div></div>';
      slot.dataset.locked = '1';
      setTimeout(fetchState, 500);
    }
  } catch (e) {
    slot.innerHTML =
      '<div class="panel"><div class="result err">' +
      'Network error talking to the Wi-Fi backend.' +
      '</div><div class="btns"><button class="btn btn--ghost" ' +
      'data-action="dismiss-connect" ' +
      'data-ssid="' + escapeHtml(ssid) + '">Dismiss</button>' +
      '</div></div>';
    slot.dataset.locked = '1';
  }
}

function dismissPanel(ssid) {
  const slot = document.getElementById('av-panel-' + cssIdSafe(ssid));
  if (slot) {
    slot.dataset.locked = '';
    slot.innerHTML = '';
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
  result.innerHTML =
    '<div><span class="spinner"></span> Connecting to ' +
    escapeHtml(ssid) + '… <span class="hint">' +
    '(up to 90s including rollback)</span></div>';
  try {
    const r = await fetch('./connect', {
      method: 'POST',
      headers: jsonHeaders(),
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (r.ok && data.ok) {
      result.innerHTML = '<div class="result ok">✓ ' +
        escapeHtml(data.message || 'Connected') + '</div>';
      setTimeout(fetchState, 500);
    } else {
      result.innerHTML = '<div class="result err">' +
        escapeHtml(data.message || data.error || 'Connection failed') +
        '</div>';
      setTimeout(fetchState, 500);
    }
  } catch (e) {
    result.innerHTML =
      '<div class="result err">Network error talking to the Wi-Fi backend.</div>';
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Forget panel -------------------------------------------------------
function openForget(name, keepOpen) {
  if (openSavedName && openSavedName !== name && !keepOpen) {
    const prev = document.getElementById('sv-panel-' + cssIdSafe(openSavedName));
    if (prev) prev.innerHTML = '';
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
    ? '<div class="warn kill">⚠ This is the network the Pi is ' +
      'currently using. Forgetting it will disconnect Wi-Fi.' +
      (state.hasEthernet
        ? ' (Ethernet is connected so the Pi stays reachable.)'
        : ' Pi has no Ethernet fallback — you may lose access.') +
      '</div>'
    : '';

  slot.innerHTML =
    '<div class="panel">' + extra +
    '<div>Forget <strong>' + escapeHtml(displayName) + '</strong>? ' +
    'You\'ll need the password again to reconnect.</div>' +
    '<div class="btns">' +
    '  <button class="btn btn--danger" data-action="submit-forget" ' +
          'data-name="' + escapeHtml(name) + '">Forget</button>' +
    '  <button class="btn btn--ghost" data-action="close-forget" ' +
          'data-name="' + escapeHtml(name) + '">Cancel</button>' +
    '</div></div>';
}

function closeForget(name) {
  if (openSavedName === name) openSavedName = null;
  const slot = document.getElementById('sv-panel-' + cssIdSafe(name));
  if (slot && slot.dataset.locked !== '1') slot.innerHTML = '';
}

async function submitForget(name) {
  const slot = document.getElementById('sv-panel-' + cssIdSafe(name));
  if (!slot) return;
  slot.dataset.locked = '1';
  slot.innerHTML = '<div class="panel"><div><span class="spinner"></span> ' +
                   'Forgetting…</div></div>';
  try {
    const r = await fetch('./forget', {
      method: 'POST',
      headers: jsonHeaders(),
      body: JSON.stringify({name: name}),
    });
    const data = await r.json();
    if (r.ok && data.ok) {
      slot.innerHTML = '<div class="panel"><div class="result ok">✓ ' +
                       escapeHtml(data.message || 'Forgotten') + '</div></div>';
      setTimeout(function() {
        slot.dataset.locked = '';
        openSavedName = null;
        fetchState();
      }, 800);
    } else {
      slot.innerHTML =
        '<div class="panel"><div class="result err">' +
        escapeHtml(data.message || data.error || 'Failed') + '</div>' +
        '<div class="btns"><button class="btn btn--ghost" ' +
        'data-action="dismiss-forget" ' +
        'data-name="' + escapeHtml(name) + '">Dismiss</button>' +
        '</div></div>';
    }
  } catch (e) {
    slot.innerHTML =
      '<div class="panel"><div class="result err">' +
      'Network error talking to the Wi-Fi backend.</div>' +
      '<div class="btns"><button class="btn btn--ghost" ' +
      'data-action="dismiss-forget" ' +
      'data-name="' + escapeHtml(name) + '">Dismiss</button>' +
      '</div></div>';
  }
}

function dismissForget(name) {
  const slot = document.getElementById('sv-panel-' + cssIdSafe(name));
  if (slot) { slot.dataset.locked = ''; slot.innerHTML = ''; }
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
fetchState();
schedulePoll(7000);
