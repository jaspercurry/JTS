// SPDX-FileCopyrightText: 2026 Jasper Curry
// SPDX-License-Identifier: Apache-2.0

import { escapeHtml } from "/assets/shared/js/escape.js";
import { manualNumberValue } from "/assets/sound-profile/js/format.js";
import { driverResearch } from "/assets/sound-profile/js/state.js";

function installationFields() {
  return ((driverResearch.designDraft || {}).installation || {}).fields || {};
}

export function installationFromSetting(setting) {
  var out = {};
  Object.entries(installationFields()).forEach(function([key, spec]) {
    var value = setting['installation_' + key];
    value = spec.type === 'number' ? manualNumberValue(value) : String(value || '').trim();
    if (value != null && value !== '') out[key] = value;
  });
  return Object.keys(out).length ? out : null;
}

export function applyInstallationToSetting(driver, setting) {
  Object.entries(driver.installation || {}).forEach(function([key, value]) {
    setting['installation_' + key] = value;
  });
}

export function renderInstallation(targetId, setting) {
  var view = (driverResearch.designDraft || {}).installation || {};
  var fields = Object.entries(installationFields());
  if (!fields.length) return '';
  var row = (view.drivers || []).find(function(item) { return item.target_id === targetId; });
  var estimate = row && row.amplifier_estimate;
  return '<details class="component-card__pad"><summary>Amplifier and bass hardware (optional)</summary>' +
    '<p class="setting-row__hint">Enter what you know. Air volume excludes the driver, bracing and other parts. These facts guide trials; they do not change the sound.</p>' +
    '<div class="component-card__fields">' + fields.filter(function([key, spec]) {
      return !spec.enclosure || spec.enclosure === setting.enclosure_kind;
    }).map(function([key, spec]) {
      return '<label class="driver-research__field"><span>' + escapeHtml(spec.label) + '</span>' +
        '<input type="' + spec.type + '" data-manual-driver="' + escapeHtml(targetId) +
        '" data-manual-field="installation_' + key + '" value="' +
        escapeHtml(String(setting['installation_' + key] == null ? '' : setting['installation_' + key])) +
        '"' + (spec.type === 'number' ? ' step="' + (spec.integer ? '1' : 'any') + '"' : '') + '></label>';
    }).join('') + '</div>' +
    (estimate ? '<p class="setting-row__hint">From the last save: an ideal bridged amplifier could reach ' +
      escapeHtml(estimate.ideal_btl_rms_voltage_ceiling_v.toFixed(1)) + ' V RMS. ' +
      escapeHtml(estimate.assumptions + ' ' + estimate.scope) + '</p>' : '') +
    '</details>';
}
