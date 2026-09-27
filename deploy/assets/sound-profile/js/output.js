// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import { escapeHtml } from "/assets/shared/js/escape.js";
import { getJSON, postJSON } from "/assets/shared/js/http.js";
import { clamp, fmtTrim } from "/assets/sound-profile/js/format.js";
import { el, status } from "/assets/sound-profile/js/state.js";

var LIMIT_DEFAULTS = {
  headroom_trim_max_db: 12,
  // volume_floor_default_db is owned by the backend (volume_curve.
  // DEFAULT_VOLUME_FLOOR_DB → /state limits) and read via volumeFloorDefault().
  // These three are the payload-absent fallbacks only.
  volume_floor_min_db: -60, volume_floor_max_db: -10, volume_floor_default_db: -50
};
// What the settings card says while the loaded graph refuses to carry EQ.
// The per-reason remedy belongs on /sound/eq/, not on a setting's card.
var EQ_BLOCKED_CARD_MESSAGE = 'The setting is saved, but sound EQ is not ' +
  'audible until this speaker’s setup can carry it.';

// The page's record: the saved sound settings it edits plus its in-flight picks.
var outputPage = {
  // volume_floor_db is absent until /state carries it: savedVolumeFloorDb()
  // then falls back to volumeFloorDefault() (backend-owned) rather than this
  // module keeping a second copy of the default.
  soundSettings: {headroom_trim_db: 0, match_loudness: false},
  blocked: false,          // ./settings: the graph refused to carry EQ
  i2sHat: null,
  volumeFloorDraftDb: null,
};
var limits = Object.assign({}, LIMIT_DEFAULTS);
var volumeFloorSaving = false;
var volumeFloorTone = {
  active: false,
  timer: null,
  inFlight: false,
  pending: null,
  generation: 0,
  savedNotice: false
};

function render() {
  el('view-body').innerHTML = '<div class="saved-stack">' +
    renderI2sHatSetting() + renderSetupSoundSettings() + '</div>';
}

function renderI2sHatSetting() {
  var hat = outputPage.i2sHat;
  if (!hat || hat.visibility === 'hidden') return '';
  var profiles = hat.profiles || [];
  var selectedId = hat.desired_profile_id || '';
  var issue = hat.intent_error ? 'Saved setting could not be read: ' + hat.intent_error : hat.reason;
  var warnings = hat.warnings || [];
  var options = '<option value=""' + (selectedId ? '' : ' selected') + '>None / unmanaged</option>' +
    profiles.map(function(p) {
      return '<option value="' + escapeHtml(p.id) + '"' +
        (selectedId === p.id ? ' selected' : '') + '>' + escapeHtml(p.label) + '</option>';
    }).join('');
  // A HAT that identifies itself is applied for the operator, so the picker
  // is shown only for the HATs that cannot be detected (ADR-0234).
  var control = hat.detected_profile_id ?
    '<p class="setting-row__hint"><strong>I²S audio HAT.</strong> Detected: ' +
      escapeHtml(hat.detected_label || hat.detected_profile_id) +
      ' — managed automatically.</p>' :
    '<div class="field">' +
      '<label for="set-i2s-hat">I²S audio HAT</label>' +
      '<select id="set-i2s-hat"' + (!hat.available ? ' disabled' : '') +
        ' aria-label="I²S audio HAT">' + options + '</select>' +
      '<p class="setting-row__hint">Pick the HAT you fitted. Only HATs that cannot identify themselves are listed.</p>' +
    '</div>';
  return '<section class="sound-settings">' +
    '<div class="setting-row setting-row--stack">' +
      control +
      (issue ? '<p class="setting-row__hint">' + escapeHtml(issue) + '</p>' : '') +
      warnings.map(function(w) {
        return '<p class="setting-row__hint output-template-warning">' + escapeHtml(w) + '</p>';
      }).join('') +
      (hat.restart_required ? '<div class="info-card info-card--accent" role="status">' +
        '<p><strong>Restart required.</strong> The saved boot setting changed.</p>' +
        '<a class="btn btn--primary" href="/system/">Open Restart control</a></div>' : '') +
      (hat.shared_usb_data_port ? '<p class="setting-row__hint"><strong>Shared USB data port:</strong> Enabling this setting reserves this shared port for gadget/peripheral use after restart, so it can no longer host a USB output DAC and output moves to the HAT. While the HAT powers the Pi, do not connect an ordinary powered micro-USB host cable: it supplies 5 V and can back-power the Pi. Use a VBUS-isolated data connection/adapter or leave the port disconnected.</p>' : '') +
      '<p class="setting-row__hint"><strong>Hardware safety:</strong> Shut down and remove all power ' +
        'before installing or removing the HAT. Never power the Pi through the HAT and another power input at the same time. ' +
        'Never hot-plug. Start the first playback at a very low level.</p>' +
    '</div></section>';
}

function fmtVolumeFloor(v) {
  v = Number(v);
  if (!isFinite(v)) v = volumeFloorDefault();
  return v.toFixed(1) + ' dB';
}
function volumeFloorLimits() {
  var floorMin = Number(limits.volume_floor_min_db);
  var floorMax = Number(limits.volume_floor_max_db);
  if (!isFinite(floorMin)) floorMin = -60;
  if (!isFinite(floorMax)) floorMax = -10;
  return {min: floorMin, max: floorMax};
}
// The reset/default volume floor, owned by the backend (volume_curve.
// DEFAULT_VOLUME_FLOOR_DB → /state limits.volume_floor_default_db). Single
// read point so the page never hardcodes the value; LIMIT_DEFAULTS supplies
// the payload-absent fallback.
function volumeFloorDefault() {
  var value = Number(limits.volume_floor_default_db);
  if (!isFinite(value)) value = Number(LIMIT_DEFAULTS.volume_floor_default_db);
  return value;
}
function savedVolumeFloorDb() {
  var bounds = volumeFloorLimits();
  var floor = Number(outputPage.soundSettings.volume_floor_db);
  if (!isFinite(floor)) floor = volumeFloorDefault();
  return clamp(floor, bounds.min, bounds.max);
}
function coerceVolumeFloorDb(value) {
  var bounds = volumeFloorLimits();
  var floor = Number(value);
  if (!isFinite(floor)) floor = savedVolumeFloorDb();
  return clamp(floor, bounds.min, bounds.max);
}
function volumeFloorValue() {
  return outputPage.volumeFloorDraftDb === null || outputPage.volumeFloorDraftDb === undefined ?
    savedVolumeFloorDb() : coerceVolumeFloorDb(outputPage.volumeFloorDraftDb);
}
function volumeFloorDirty(v) {
  return Math.abs(coerceVolumeFloorDb(v) - savedVolumeFloorDb()) >= 0.05;
}
function syncVolumeFloorControls(v) {
  var value = coerceVolumeFloorDb(v);
  var node = el('set-volume-floor-readout');
  if (node) node.textContent = fmtVolumeFloor(value);
  var resetButton = el('view-body').querySelector('[data-act="reset-volume-floor"]');
  if (resetButton) resetButton.disabled = Math.abs(value - volumeFloorDefault()) < 0.05;
  var saveButton = el('volume-floor-save-button');
  if (saveButton) {
    var dirty = volumeFloorDirty(value);
    saveButton.disabled = volumeFloorSaving || !dirty;
    saveButton.textContent = volumeFloorSaving ? 'Saving' : (dirty ? 'Save floor' : 'Saved');
  }
}
function setVolumeFloorDraft(v) {
  outputPage.volumeFloorDraftDb = coerceVolumeFloorDb(v);
  syncVolumeFloorControls(outputPage.volumeFloorDraftDb);
}
function renderMatchLoudnessSetting() {
  var ml = outputPage.soundSettings.match_loudness ? ' checked' : '';
  return '<div class="setting-row">' +
      '<div class="setting-row__text">' +
        '<p class="setting-row__title">Match loudness</p>' +
        '<p class="setting-row__hint">Level-match profiles so switching compares tone, not volume.</p>' +
      '</div>' +
      '<label class="toggle"><input type="checkbox" id="set-match-loudness"' + ml +
        ' aria-label="Match loudness"><span class="track"></span></label>' +
    '</div>';
}
function renderSetupSoundSettings() {
  var trim = Number(outputPage.soundSettings.headroom_trim_db) || 0;
  var trimMax = Number(limits.headroom_trim_max_db) || 12;  // backend clamps authoritatively
  var floorBounds = volumeFloorLimits();
  var floorMin = floorBounds.min;
  var floorMax = floorBounds.max;
  var defaultFloor = volumeFloorDefault();
  var floor = volumeFloorValue();
  var advancedOpen = trim > 0 || Math.abs(floor - defaultFloor) >= 0.05;
  var toneLabel = volumeFloorTone.active ? 'Stop tone' : 'Start tone';
  var resetDisabled = Math.abs(floor - defaultFloor) < 0.05 ? ' disabled' : '';
  var saveDisabled = (volumeFloorSaving || !volumeFloorDirty(floor)) ? ' disabled' : '';
  var saveLabel = volumeFloorSaving ? 'Saving' :
    (volumeFloorDirty(floor) ? 'Save floor' : 'Saved');
  return '<section class="sound-settings">' +
    (outputPage.blocked ? '<div class="info-card" role="status"><p>' +
      EQ_BLOCKED_CARD_MESSAGE + '</p></div>' : '') +
    renderMatchLoudnessSetting() +
    '<details class="advanced"' + (advancedOpen ? ' open' : '') + '>' +
      '<summary>Advanced</summary>' +
      '<div class="setting-row setting-row--stack">' +
        '<div class="setting-row__text">' +
          '<p class="setting-row__title">Volume floor</p>' +
          '<p class="setting-row__hint">The 1% listening level. 0% stays fully muted.</p>' +
        '</div>' +
        '<div class="headroom-control">' +
          '<input type="range" class="headroom-range" id="set-volume-floor" min="' + floorMin +
            '" max="' + floorMax + '" step="1" value="' + floor + '" aria-label="Volume floor in dB">' +
          '<button type="button" class="btn btn--ghost btn--compact" id="volume-floor-tone-button" ' +
            'data-act="toggle-volume-floor-tone">' + toneLabel + '</button>' +
          '<button type="button" class="btn btn--primary btn--compact" id="volume-floor-save-button" ' +
            'data-act="save-volume-floor"' + saveDisabled + '>' + saveLabel + '</button>' +
          '<button type="button" class="btn btn--ghost btn--compact" data-act="reset-volume-floor"' +
            resetDisabled + '>Reset floor</button>' +
          '<span class="headroom-readout" id="set-volume-floor-readout">' + fmtVolumeFloor(floor) + '</span>' +
        '</div>' +
      '</div>' +
      '<div class="setting-row setting-row--stack">' +
        '<div class="setting-row__text">' +
          '<p class="setting-row__title">Extra headroom</p>' +
          '<p class="setting-row__hint">Digital attenuation for full-volume setups into your own amp. ' +
            'Leave at Off unless you hear clipping.</p>' +
        '</div>' +
        '<div class="headroom-control">' +
          '<input type="range" class="headroom-range" id="set-headroom" min="0" max="' + trimMax +
            '" step="0.5" value="' + trim + '" aria-label="Extra headroom in dB">' +
          '<span class="headroom-readout" id="set-headroom-readout">' + fmtTrim(trim) + '</span>' +
        '</div>' +
      '</div>' +
    '</details>' +
  '</section>';
}

// Global sound settings. Optimistic: the controls already show the user's
// input, so on success we just ingest; on failure we revert and re-render.
async function saveSettings(patch) {
  var prev = outputPage.soundSettings;
  outputPage.soundSettings = Object.assign({}, outputPage.soundSettings, patch);
  try {
    var payload = await postJSON('./settings', patch);
    // The setting is saved either way; a blocked body says the loaded graph
    // refused to carry it, and the card holds that until the next save. The
    // card is the refusal's surface, so the status line is left for the
    // warnings a save can ALSO raise (a blocked body never carries
    // `warning` — same server branch — so in practice that is volume_warning).
    var blocked = payload.status === 'blocked';
    var blockChanged = blocked !== outputPage.blocked;
    outputPage.blocked = blocked;
    ingestState(payload);
    if (blockChanged) render();
    if (payload.warning) status(payload.warning, true);
    else if (payload.volume_warning) status(payload.volume_warning, true);
    return true;
  } catch (e) {
    outputPage.soundSettings = prev;
    status('Could not save sound settings: ' + e.message, true);
    render();
    return false;
  }
}

async function saveI2sHatProfileId(profileId, input) {
  if (input) input.disabled = true;
  try {
    var payload = await postJSON('./i2s-hat', {profile_id: profileId || null});
    if ('desired_profile_id' in payload) outputPage.i2sHat = payload;
    if (payload.warnings && payload.warnings.length)
      return status(payload.warnings[0], true);
    status(payload.restart_required ?
      'I²S HAT setting saved. Restart required.' : 'I²S HAT setting saved.');
  } catch (e) {
    if (e.body && 'desired_profile_id' in e.body) {
      outputPage.i2sHat = e.body;
      return status('Setting saved, but the boot change could not be applied. Try again; if it still fails, open System and run diagnostics.', true);
    }
    status('Could not save I²S HAT setting: ' + e.message, true);
  } finally {
    render();
  }
}

function setVolumeFloorToneButton() {
  var button = el('volume-floor-tone-button');
  if (!button) return;
  button.textContent = volumeFloorTone.active ? 'Stop tone' : 'Start tone';
}

function scheduleVolumeFloorToneUpdate(value, options) {
  options = options || {};
  value = Number(value);
  if (!isFinite(value)) return;
  if (!volumeFloorTone.active && !options.force) return;
  volumeFloorTone.pending = value;
  if (volumeFloorTone.timer) clearTimeout(volumeFloorTone.timer);
  volumeFloorTone.timer = setTimeout(function() {
    volumeFloorTone.timer = null;
    flushVolumeFloorToneUpdate();
  }, options.immediate ? 0 : 120);
}

async function flushVolumeFloorToneUpdate() {
  if (volumeFloorTone.inFlight) return;
  var value = volumeFloorTone.pending;
  var generation = volumeFloorTone.generation;
  volumeFloorTone.pending = null;
  if (value === null || value === undefined) return;
  volumeFloorTone.inFlight = true;
  try {
    var payload = await postJSON('./volume-floor/audition', {volume_floor_db: value});
    if (generation !== volumeFloorTone.generation) {
      if (!volumeFloorTone.active) stopVolumeFloorTone({quiet: true});
      return;
    }
    volumeFloorTone.active = true;
    setVolumeFloorToneButton();
    var toneStatus = '1% calibration tone at ' +
      fmtVolumeFloor(payload.volume_floor_db || value) + '.';
    if (volumeFloorTone.savedNotice) {
      toneStatus = 'Volume floor saved. ' + toneStatus;
      volumeFloorTone.savedNotice = false;
    }
    status(toneStatus);
  } catch (e) {
    volumeFloorTone.active = false;
    setVolumeFloorToneButton();
    status('Could not play volume-floor tone: ' + e.message, true);
  } finally {
    volumeFloorTone.inFlight = false;
    if (volumeFloorTone.pending !== null && volumeFloorTone.pending !== undefined) {
      flushVolumeFloorToneUpdate();
    }
  }
}

function startVolumeFloorTone() {
  volumeFloorTone.active = true;
  volumeFloorTone.generation += 1;
  volumeFloorTone.savedNotice = false;
  setVolumeFloorToneButton();
  scheduleVolumeFloorToneUpdate(volumeFloorValue(), {force: true, immediate: true});
}

async function resetVolumeFloor() {
  var floor = volumeFloorDefault();
  var floorInput = el('set-volume-floor');
  if (floorInput) floorInput.value = floor;
  setVolumeFloorDraft(floor);
  await saveVolumeFloor();
}

async function saveVolumeFloor() {
  var floor = volumeFloorValue();
  volumeFloorSaving = true;
  syncVolumeFloorControls(floor);
  var saved = await saveSettings({volume_floor_db: floor});
  volumeFloorSaving = false;
  if (saved) {
    outputPage.volumeFloorDraftDb = null;
    syncVolumeFloorControls(savedVolumeFloorDb());
    if (volumeFloorTone.active) {
      volumeFloorTone.savedNotice = true;
      scheduleVolumeFloorToneUpdate(savedVolumeFloorDb(), {immediate: true});
    } else {
      status('Volume floor saved.');
    }
  } else {
    syncVolumeFloorControls(volumeFloorValue());
  }
}

async function stopVolumeFloorTone(options) {
  options = options || {};
  volumeFloorTone.active = false;
  volumeFloorTone.generation += 1;
  volumeFloorTone.savedNotice = false;
  volumeFloorTone.pending = null;
  if (volumeFloorTone.timer) {
    clearTimeout(volumeFloorTone.timer);
    volumeFloorTone.timer = null;
  }
  setVolumeFloorToneButton();
  try {
    await postJSON('./volume-floor/stop', {reason: options.reason || 'stop'},
      {keepalive: !!options.keepalive});
    if (!options.quiet) status('Volume-floor tone stopped.');
  } catch (e) {
    if (!options.quiet) status('Could not stop volume-floor tone: ' + e.message, true);
  }
}

function ingestState(payload) {
  limits = Object.assign({}, LIMIT_DEFAULTS, payload.limits || {});
  if (payload.sound_settings) outputPage.soundSettings = payload.sound_settings;
}

async function loadOutputHardware() {
  try { outputPage.i2sHat = (await getJSON('./output-topology')).i2s_hat; }
  catch (e) { status(e.message, true); }
  render();
}
async function loadState() {
  try {
    ingestState(await getJSON('./state'));
    render();
    // The I2S HAT reading rides the topology payload.
    loadOutputHardware();
  } catch (e) {
    status('Could not load sound profile: ' + e.message, true);
  }
}

el('back').addEventListener('click', function(e) { e.preventDefault(); window.location.href = '/sound/'; });
el('view-body').addEventListener('click', function(ev) {
  var t = ev.target.closest('[data-act]');
  if (!t) return;
  var act = t.getAttribute('data-act');
  if (act === 'toggle-volume-floor-tone') {
    if (volumeFloorTone.active) stopVolumeFloorTone();
    else startVolumeFloorTone();
  }
  else if (act === 'save-volume-floor') { saveVolumeFloor(); }
  else if (act === 'reset-volume-floor') { resetVolumeFloor(); }
});
el('view-body').addEventListener('input', function(ev) {
  if (ev.target.id === 'set-headroom') {
    var ro = el('set-headroom-readout');           // live readout; commit on 'change'
    if (ro) ro.textContent = fmtTrim(ev.target.value);
  }
  if (ev.target.id === 'set-volume-floor') {
    var floor = Number(ev.target.value);
    setVolumeFloorDraft(floor);
    scheduleVolumeFloorToneUpdate(volumeFloorValue());
  }
});
el('view-body').addEventListener('change', function(ev) {
  if (ev.target.id === 'set-match-loudness') saveSettings({match_loudness: ev.target.checked});
  else if (ev.target.id === 'set-headroom') saveSettings({headroom_trim_db: Number(ev.target.value)});
  else if (ev.target.id === 'set-i2s-hat') saveI2sHatProfileId(ev.target.value, ev.target);
  else if (ev.target.id === 'set-volume-floor') {
    var floor = Number(ev.target.value);
    setVolumeFloorDraft(floor);
    if (volumeFloorTone.active) scheduleVolumeFloorToneUpdate(volumeFloorValue(), {immediate: true});
  }
});
window.addEventListener('pagehide', function() {
  if (volumeFloorTone.active || volumeFloorTone.inFlight) {
    stopVolumeFloorTone({keepalive: true, quiet: true, reason: 'pagehide'});
  }
});
loadState();
