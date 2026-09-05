// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// main.js — the landing page's own behaviour: the hero controls (volume, the
// stereo-pair banner, the source selector, the assistant pause). Capability
// gating and the status-* sublabels are the settings-surface behaviour it
// shares with the area hubs, and live in shared/js/settings-status.js.

import { jsonHeaders } from "/assets/shared/js/http.js";
import { localWebHost } from "/assets/shared/js/local-web-host.js";
import {
  bakedCaps,
  initSettingsStatus,
  setStatusText,
} from "/assets/shared/js/settings-status.js";

// Volume slider. Talks to jasper-control's /volume endpoints
// (proxied through nginx so this stays same-origin). Optimistic UI:
// the fill follows local gestures immediately; local intent owns
// the slider while a write is pending.
function initVolume() {
  var hit = document.getElementById('vol-control');
  var fill = document.getElementById('vol-fill');
  var percentEl = document.getElementById('vol-percent');
  var safetyNote = document.getElementById('volume-safety-note');
  var dragging = false;
  var pending = null;
  var inFlight = false;
  var flushing = false;
  var safetyMuted = false;
  var desiredPct = null;
  var ignorePollUntil = 0;
  var pollInFlight = false;
  var lastSentAt = 0;
  var THROTTLE_MS = 150;
  // External controls (Mac USB slider, paired remote, voice) converge
  // through the same /volume snapshot. Poll at 2 Hz while this page is
  // visible so those changes feel immediate without adding a persistent
  // connection or doing any network work from a hidden tab.
  var POLL_MS = 500;
  var SAFETY_POLL_MS = 5000;
  var SETTLE_MS = 1500;
  // Last level the server CONFIRMED — the truth the UI falls back to
  // when a write fails, so the fill never keeps displaying a drag the
  // speaker rejected (on a bonded follower with its leader down,
  // every write 502s; an optimistic fill would lie indefinitely).
  var lastServerPct = null;
  var pollFails = 0;

  function markWriteFailed() {
    if (dragging || pending !== null) return; // don't fight a live drag
    if (lastServerPct !== null) setUI(lastServerPct);
    percentEl.textContent = '\u2014'; // em dash: "no response"
  }

  function clampPct(pct) {
    return Math.max(0, Math.min(100, Math.round(pct)));
  }

  function setUI(pct) {
    pct = clampPct(pct);
    fill.style.width = pct + '%';
    percentEl.textContent = pct + '%';
    hit.setAttribute('aria-valuenow', String(pct));
    hit.setAttribute('aria-valuetext', pct + '%');
  }

  function activeSpeakerSafetyMuted(data) {
    var safety = data && data.active_speaker_output_safety;
    if (safety && typeof safety.safety_muted === 'boolean') {
      return safety.safety_muted;
    }
    if (safety && typeof safety.volume_allowed === 'boolean') {
      return !safety.volume_allowed;
    }
    var audio = (data && data.audio) || {};
    var sound = audio.sound || {};
    var runtime = sound.runtime || {};
    var airplay = (data && data.airplay_health) || {};
    var airplayCurrent = airplay.current || {};
    var camilla = airplayCurrent.camilla || {};
    var activePath = String(
      camilla.config_path ||
      audio.camilla_active_config_path ||
      sound.active_config_path ||
      runtime.active_config_path ||
      ''
    );
    return /(^|\/)active_speaker_staged_startup\.yml$/.test(activePath);
  }

  function setSafetyMuted(muted) {
    safetyMuted = !!muted;
    hit.classList.toggle('safety-muted', safetyMuted);
    if (safetyNote) safetyNote.hidden = !safetyMuted;
    if (safetyMuted) {
      dragging = false;
      pending = null;
      desiredPct = null;
      ignorePollUntil = 0;
      hit.setAttribute('aria-describedby', 'volume-safety-note');
      hit.setAttribute('aria-disabled', 'true');
    } else {
      hit.removeAttribute('aria-describedby');
      hit.removeAttribute('aria-disabled');
    }
  }

  function xToPercent(clientX) {
    var rect = hit.getBoundingClientRect();
    if (!rect.width) return 0;
    return ((clientX - rect.left) / rect.width) * 100;
  }

  function setFromPointer(e) {
    if (safetyMuted) return;
    var pct = clampPct(xToPercent(e.clientX));
    setUI(pct);
    sendThrottled(pct);
  }

  function localVolumeDirty() {
    return dragging || flushing || inFlight || pending !== null ||
           Date.now() < ignorePollUntil;
  }

  function sendThrottled(pct) {
    if (safetyMuted) return;
    desiredPct = clampPct(pct);
    pending = desiredPct;
    ignorePollUntil = Date.now() + SETTLE_MS;
    if (flushing) return;
    flush();
  }

  async function flush() {
    if (flushing) return;
    flushing = true;
    try {
      while (pending !== null) {
        var wait = Math.max(0, lastSentAt + THROTTLE_MS - Date.now());
        if (wait > 0) await new Promise(function(r) { setTimeout(r, wait); });
        var toSend = pending;
        pending = null;
        inFlight = true;
        lastSentAt = Date.now();
        try {
          var resp = await fetch('/volume/set', {
            method: 'POST',
            headers: jsonHeaders(),
            body: JSON.stringify({percent: toSend}),
          });
          if (resp.ok) {
            var data = await resp.json();
            if (typeof data.percent === 'number') {
              lastServerPct = data.percent;
            }
            if (!dragging && pending === null && toSend === desiredPct &&
                typeof data.percent === 'number') {
              setUI(data.percent);
            }
          } else {
            markWriteFailed();
          }
        } catch (_) {
          markWriteFailed();
        } finally {
          inFlight = false;
        }
      }
    } finally {
      flushing = false;
    }
  }

  hit.addEventListener('pointerdown', function(e) {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    if (safetyMuted) {
      e.preventDefault();
      return;
    }
    dragging = true;
    try { hit.focus({preventScroll: true}); } catch (_) { hit.focus(); }
    try { hit.setPointerCapture(e.pointerId); } catch (_) {}
    setFromPointer(e);
    e.preventDefault();
  });
  hit.addEventListener('pointermove', function(e) {
    if (!dragging) return;
    setFromPointer(e);
    e.preventDefault();
  });
  function endDrag(e) {
    if (!dragging) return;
    dragging = false;
    try { hit.releasePointerCapture(e.pointerId); } catch (_) {}
    setFromPointer(e);
  }
  hit.addEventListener('pointerup', endDrag);
  hit.addEventListener('pointercancel', endDrag);
  hit.addEventListener('lostpointercapture', function() {
    dragging = false;
  });

  hit.addEventListener('keydown', function(e) {
    var current = parseInt(hit.getAttribute('aria-valuenow') || '0', 10);
    var next = current;
    switch (e.key) {
      case 'ArrowLeft':
      case 'ArrowDown':  next = current - 5;  break;
      case 'ArrowRight':
      case 'ArrowUp':    next = current + 5;  break;
      case 'PageDown':   next = current - 10; break;
      case 'PageUp':     next = current + 10; break;
      case 'Home':       next = 0;            break;
      case 'End':        next = 100;          break;
      default: return;
    }
    e.preventDefault();
    if (safetyMuted) return;
    next = clampPct(next);
    setUI(next);
    sendThrottled(next);
  });

  async function poll() {
    if (localVolumeDirty()) return;
    if (document.visibilityState === 'hidden') return;
    if (pollInFlight) return;
    pollInFlight = true;
    try {
      var resp = await fetch('/volume');
      if (resp.ok) {
        var data = await resp.json();
        pollFails = 0;
        if (typeof data.percent === 'number') {
          lastServerPct = data.percent;
          setUI(data.percent);
        }
        return;
      }
    } catch (_) {
    } finally {
      pollInFlight = false;
    }
    // Volume truth unavailable (e.g. a bonded follower whose pair
    // leader is down — the forward 502s). After three misses show
    // an honest dash instead of a stale number; the next good poll
    // restores it.
    pollFails += 1;
    if (pollFails >= 3) percentEl.textContent = '\u2014';
  }

  async function pollSafetyMuted() {
    if (document.visibilityState === 'hidden') return;
    try {
      var resp = await fetch('/system/data.json', {cache: 'no-store'});
      if (resp.ok) {
        setSafetyMuted(activeSpeakerSafetyMuted(await resp.json()));
      }
    } catch (_) {}
  }
  setInterval(poll, POLL_MS);
  setInterval(pollSafetyMuted, SAFETY_POLL_MS);
  poll();
  pollSafetyMuted();
}

// Stereo-pair banner. While this speaker is an active bond member
// the banner explains who owns playback. On a FOLLOWER the source
// selector hides (sources play through the leader) and the volume slider
// is relabeled. Sound navigation stays visible: EQ/Room delegate in their
// own pages while local Setup/commissioning remains on the DAC owner.
// Volume requests are forwarded server-side by jasper-control's bonded-follower proxy,
// so it controls the PAIR volume. Untrusted grouping fields reach the
// DOM as text nodes only; the leader link is built only for a stable
// .local web host.
function initPairBanner() {
  var banner = document.getElementById('pair-banner');
  var text = document.getElementById('pair-text');
  var channelEl = document.getElementById('pair-channel');
  var leaderLink = document.getElementById('pair-leader-link');
  var sourceSection = document.getElementById('source-section');
  var volumeEyebrow = document.getElementById('volume-eyebrow');
  if (!banner || !sourceSection) return;
  var POLL_MS = 10000;
  function apply(g) {
    var bonded = !!(g && g.enabled && g.bond_id && !g.error);
    var follower = bonded && g.role === 'follower';
    banner.hidden = !bonded;
    sourceSection.style.display = follower ? 'none' : '';
    // Dumb-follower profile: voice + mic are parked while paired —
    // the leader owns the assistant. Annotate the mic card so a
    // dead /mic probe reads as intended state, not breakage. Text
    // only; the mic IIFE's own poll repaints when un-parked.
    var micSub = document.getElementById('mic-sub');
    if (micSub) {
      if (follower) {
        micSub.textContent =
          'Paired — the assistant listens on the pair leader';
      } else if (micSub.textContent.indexOf('Paired') === 0) {
        micSub.textContent = 'Voice control status';
      }
    }
    if (volumeEyebrow) {
      volumeEyebrow.textContent = follower ? 'Pair volume' : 'Volume';
    }
    if (!bonded) return;
    var ch = (g.channel === 'left' || g.channel === 'right') ? g.channel : '';
    var leaderHost = localWebHost(g.leader_addr);
    channelEl.textContent = ch ? ch + ' channel' : 'paired';
    if (follower) {
      text.textContent = 'This speaker plays the ' + (ch || 'second') +
        ' channel of a stereo pair. Music is controlled on the pair ' +
        'leader' +
        (leaderHost ? ' (' + leaderHost + ')' : '') +
        '; voice answers on the leader, and the slider below sets the ' +
        'pair volume.';
      if (leaderHost) {
        leaderLink.href = 'http://' + leaderHost + '/';
        leaderLink.hidden = false;
      } else {
        leaderLink.hidden = true;
      }
    } else {
      text.textContent = 'This speaker leads a stereo pair and plays ' +
        'the ' + (ch || 'first') + ' channel. Sources and volume here ' +
        'control the pair.';
      leaderLink.hidden = true;
    }
  }

  async function poll() {
    if (document.visibilityState === 'hidden') return;
    try {
      var resp = await fetch('/grouping');
      if (!resp.ok) return;
      var data = await resp.json();
      apply(data && data.grouping);
    } catch (_) {}
  }
  setInterval(poll, POLL_MS);
  poll();
}

// Source selector. /source/state and /source/select are thin
// proxies to jasper-mux. The UI is optimistic on taps, then
// reconciles from mux state; a green dot marks the source that is
// actually reporting audio/connection activity.
function initSources() {
  var buttons = Array.prototype.slice.call(
    document.querySelectorAll('.source-button[data-source]')
  );
  var statusEl = document.getElementById('source-status');
  var POLL_MS = 3000;
  var dirtyUntil = 0;
  var pendingSource = null;
  var requestSeq = 0;
  var names = {
    auto: 'Auto',
    airplay: 'AirPlay',
    bluetooth: 'Bluetooth',
    spotify: 'Spotify',
    usbsink: 'USB',
    idle: 'Idle',
  };

  function label(source) {
    return names[source] || source || 'Idle';
  }

  function sourceSummary(mode, selected, active) {
    if (mode === 'manual') return label(selected) + ' manual';
    if (!active || active === 'idle') return 'Auto · idle';
    return 'Auto · ' + label(active) + ' active';
  }

  function applyState(state) {
    var mode = state && state.mode === 'manual' ? 'manual' : 'auto';
    var selected = mode === 'manual' ? state.selected_source : 'auto';
    var active = state && state.active_source ? state.active_source : 'idle';
    var sources = (state && state.sources) || {};
    if (pendingSource && Date.now() < dirtyUntil) {
      selected = pendingSource;
    } else {
      pendingSource = null;
    }
    buttons.forEach(function(btn) {
      var source = btn.dataset.source;
      var info = sources[source] || {};
      var isSelected = source === selected;
      var isCurrent = source !== 'auto' && source === active && active !== 'idle';
      var unavailable = source !== 'auto' && info.available === false;
      var off = source !== 'auto' && info.enabled === false;
      btn.classList.toggle('active', isSelected);
      btn.classList.toggle('current', isCurrent);
      btn.classList.toggle('playing', isCurrent);
      btn.disabled = unavailable || off;
      btn.title = (unavailable || off) ?
        label(source) + ' is off or unavailable' : label(source);
      btn.setAttribute('aria-pressed', String(isSelected));
    });
    statusEl.textContent = mode === 'manual' ? label(selected) :
      'Auto · ' + label(active);
    setStatusText('status-playback-source', sourceSummary(mode, selected, active));
  }

  function offline() {
    buttons.forEach(function(btn) {
      btn.disabled = true;
      btn.classList.remove('active', 'playing');
      btn.setAttribute('aria-pressed', 'false');
    });
    statusEl.textContent = 'Offline';
    setStatusText('status-playback-source', 'Unavailable');
  }

  async function fetchState() {
    if (document.visibilityState === 'hidden') return;
    try {
      var resp = await fetch('/source/state', {cache: 'no-store'});
      if (resp.ok) {
        applyState(await resp.json());
      } else {
        offline();
      }
    } catch (_) { offline(); }
  }

  async function selectSource(source) {
    var requestId = ++requestSeq;
    pendingSource = source;
    dirtyUntil = Date.now() + 1500;
    applyState({mode: source === 'auto' ? 'auto' : 'manual',
                selected_source: source === 'auto' ? null : source,
                active_source: source, sources: {}});
    try {
      var resp = await fetch('/source/select', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({source: source}),
      });
      if (resp.ok) {
        if (requestId !== requestSeq) return;
        pendingSource = null;
        applyState(await resp.json());
      } else {
        if (requestId !== requestSeq) return;
        pendingSource = null;
        await fetchState();
      }
    } catch (_) {
      if (requestId !== requestSeq) return;
      pendingSource = null;
      await fetchState();
    }
  }

  buttons.forEach(function(btn) {
    btn.addEventListener('click', function() {
      selectSource(btn.dataset.source);
    });
  });
  setInterval(fetchState, POLL_MS);
  fetchState();
}

// Voice-assistant pause toggle. The legacy /mic endpoints remain the
// control-plane API, but this state only governs wake detection and JTS
// voice capture. The independent USB microphone export is unaffected.
function initMic() {
  var button = document.getElementById('mic-toggle');
  // initSettingsStatus() ran first, so a tier without wake_detection has
  // already hidden this card: don't wire the 3 s /mic poll a hidden card
  // has no route for.
  var card = button && button.closest('.control-section');
  if (!button || (card && card.hidden)) return;
  var stateEl = document.getElementById('mic-state');
  var dot = document.getElementById('mic-dot');
  var title = document.getElementById('mic-title');
  var sub = document.getElementById('mic-sub');
  var muted = false;
  var available = false;
  var POLL_MS = 3000;
  var dirty = false;
  var ignorePollUntil = 0;

  function setMicState(text) {
    if (stateEl) stateEl.textContent = text;
  }

  // A pause POST that did not take effect must NEVER leave the UI showing
  // "Paused": revert to the real (unchanged) state AND surface why.
  function failMute(prevMuted, message) {
    dirty = false;
    renderMic(prevMuted, available);
    setMicState(message);
    if (sub) sub.textContent = message;
    // Hold off the 3 s poll so the failure message stays readable instead
    // of being overwritten ~1.5 s later by the next state render. The UI
    // already shows the real (unchanged) mic state, so suppressing the poll
    // briefly is safe.
    ignorePollUntil = Date.now() + 5000;
  }

  function renderMic(wantMuted, isAvailable, info) {
    var parkedByPair = !!(
      info && info.status === 'parked' &&
      info.reason === 'bonded_follower'
    );
    var starting = !!(info && info.status === 'starting');
    muted = !!wantMuted;
    available = !parkedByPair && !starting && isAvailable !== false;
    button.disabled = !available;
    button.textContent = parkedByPair ? 'Parked' :
      (starting ? 'Reloading' : (muted ? 'Resume' : 'Pause'));
    button.setAttribute('aria-pressed', String(!muted && available));
    button.setAttribute('aria-label',
      parkedByPair ? 'Voice assistant parked while paired' :
      (starting ? 'Voice control reloading' :
        (muted ? 'Resume voice assistant' : 'Pause voice assistant')));
    dot.classList.toggle('on', !muted && available);
    if (parkedByPair) {
      title.textContent = 'Paired';
      sub.textContent = info.message ||
        'Paired — the assistant listens on the pair leader';
      setMicState('Paired');
      return;
    }
    if (starting) {
      title.textContent = 'Reloading';
      sub.textContent = info.message || 'Voice control is restarting';
      setMicState('Reloading');
      return;
    }
    title.textContent = available ?
      (muted ? 'Voice assistant paused' : 'Voice assistant active') :
      'Unavailable';
    sub.textContent = available ?
      (muted ? 'JTS will not respond to the wake word' : 'Say "Jarvis" to wake') :
      'Voice control offline';
    setMicState(available ? (muted ? 'Paused' : 'Active') : 'Unavailable');
  }

  function applyMuted(nextMuted, isAvailable, info) {
    if (dirty) return;
    renderMic(nextMuted, isAvailable, info);
  }

  async function responseJson(resp) {
    try {
      return await resp.json();
    } catch (_) {
      return {};
    }
  }

  async function fetchState() {
    if (document.visibilityState === 'hidden') return;
    if (Date.now() < ignorePollUntil) return;
    try {
      var resp = await fetch('/mic', {cache: 'no-store'});
      var data = await responseJson(resp);
      if (resp.ok) {
        applyMuted(!!data.muted, data.available !== false, data);
      } else if (resp.status === 503) {
        applyMuted(false, false, data);
      }
    } catch (_) {}
  }

  async function postMute(want_muted) {
    dirty = true;
    ignorePollUntil = Date.now() + 1500;
    renderMic(want_muted, available);
    setMicState(want_muted ? 'Pausing' : 'Resuming');
    try {
      // /mic/mute is WS1 token-gated; jsonHeaders() carries the token.
      var resp = await fetch('/mic/mute', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({muted: want_muted}),
      });
      if (resp.ok) {
        var data = await resp.json();
        dirty = false;
        applyMuted(!!data.muted, true);
      } else if (resp.status === 403) {
        failMute(!want_muted, 'Pause blocked — reload to refresh access');
      } else {
        failMute(!want_muted, 'Pause failed (HTTP ' + resp.status + ')');
      }
    } catch (_) {
      failMute(!want_muted, 'Pause failed — network error');
    }
  }

  button.addEventListener('click', function() {
    if (!available) return;
    postMute(!muted);
  });

  setInterval(fetchState, POLL_MS);
  fetchState();
}

// Gating first: the mic card short-circuits on its section being hidden.
initSettingsStatus({ caps: bakedCaps(), titleFollowsSpeakerName: true });
initVolume();
initPairBanner();
initSources();
initMic();
