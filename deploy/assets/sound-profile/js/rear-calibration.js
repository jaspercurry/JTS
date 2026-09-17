// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Sound profile — the cardioid rear-calibration wizard panel (ADR-0318).
//
// A `jts_rear_calibration` document is authored data, not a form: this panel
// is a JSON textarea plus Seed / Validate / Bank, following the existing
// driver-research paste-and-validate pattern. Seed and Validate never touch
// the candidate bank; Bank composes and banks a candidate carrying the
// document as the applied baseline's `rear_calibration` section, but never
// applies it — the page's existing apply flow adopts a banked fingerprint.

import { getJSON, postJSON } from "/assets/shared/js/http.js";
import { escapeHtml } from "/assets/shared/js/escape.js";
import { el, outputTopology } from "/assets/sound-profile/js/state.js";
import { outputGroups } from "/assets/sound-profile/js/topology.js";

var rearCalibration = {
  text: '',
  pending: '',   // '' | 'seed' | 'validate' | 'bank'
  result: null,  // the last server response, whichever action produced it
  localError: '' // a local JSON-parse failure, never sent to the server
};

// The panel only ever answers about the SAVED topology (outputTopology.payload),
// not a working draft — a draft rear output has no applied baseline to bank onto.
function savedTopologyHasRearOutput() {
  return outputGroups(outputTopology.payload).some(function(group) {
    return (group.channels || []).some(function(channel) {
      return channel.output_variant === 'rear';
    });
  });
}

function resultHtml() {
  if (rearCalibration.localError) {
    return '<p class="setting-row__hint driver-research__error">' +
      escapeHtml(rearCalibration.localError) + '</p>';
  }
  if (rearCalibration.pending) {
    return '<p class="setting-row__hint">' + {
      seed: 'Seeding…', validate: 'Validating…', bank: 'Banking…'
    }[rearCalibration.pending] + '</p>';
  }
  var result = rearCalibration.result;
  if (!result) return '';
  if (!result.ok) {
    return '<p class="setting-row__hint driver-research__error">' +
      escapeHtml(result.error || 'The calibration document was refused.') + '</p>';
  }
  if ('candidate_fingerprint' in result) {
    var issues = Array.isArray(result.issues) ? result.issues : [];
    return '<div class="driver-research__summary">' +
      '<span class="status-pill status-pill--ready">banked</span>' +
      '<p class="setting-row__hint">' + escapeHtml(
        'Candidate ' + result.candidate_fingerprint.slice(0, 12) + '… banked. ' +
        'It is not applied — apply it from the crossover session like any other candidate.'
      ) + '</p>' +
      (issues.length ? '<div class="driver-research__notes"><ul>' +
        issues.map(function(issue) {
          return '<li>' + escapeHtml(String(issue.message || issue.code || issue)) + '</li>';
        }).join('') + '</ul></div>' : '') +
    '</div>';
  }
  if ('case' in result) {
    return '<div class="driver-research__summary">' +
      '<span class="status-pill status-pill--ready">valid</span>' +
      '<p class="setting-row__hint">' + escapeHtml(String(result.summary || result.case)) + '</p>' +
    '</div>';
  }
  return '';
}

function repaint() {
  var node = el('rear-calibration-result');
  if (node) node.innerHTML = resultHtml();
  var buttons = document.querySelectorAll('[data-rear-calibration-action]');
  for (var i = 0; i < buttons.length; i += 1) {
    buttons[i].disabled = !!rearCalibration.pending;
  }
}

function parseRearCalibrationText() {
  try {
    return {document: JSON.parse(rearCalibration.text || '{}')};
  } catch (e) {
    return {error: 'That is not valid JSON: ' + e.message};
  }
}

export function setRearCalibrationText(value) {
  rearCalibration.text = value;
  rearCalibration.localError = '';
  rearCalibration.result = null;
}

export function rearCalibrationSeed() {
  rearCalibration.pending = 'seed';
  rearCalibration.localError = '';
  rearCalibration.result = null;
  repaint();
  return getJSON('./active-speaker/rear-calibration/seed')
    .then(function(payload) {
      rearCalibration.text = JSON.stringify(payload.calibration, null, 2);
      var textarea = el('rear-calibration-text');
      if (textarea) textarea.value = rearCalibration.text;
    })
    .catch(function(err) {
      rearCalibration.localError = 'Could not seed a starting document: ' + err.message;
    })
    .then(function() {
      rearCalibration.pending = '';
      repaint();
    });
}

function runOnParsedDocument(path) {
  var parsed = parseRearCalibrationText();
  if (parsed.error) {
    rearCalibration.localError = parsed.error;
    rearCalibration.result = null;
    repaint();
    return;
  }
  rearCalibration.pending = path.action;
  rearCalibration.localError = '';
  repaint();
  return postJSON(path.url, parsed.document)
    .then(function(payload) {
      rearCalibration.result = payload;
    })
    .catch(function(err) {
      rearCalibration.localError = 'Could not reach the speaker: ' + err.message;
    })
    .then(function() {
      rearCalibration.pending = '';
      repaint();
    });
}

export function rearCalibrationValidate() {
  return runOnParsedDocument({url: './active-speaker/rear-calibration/validate', action: 'validate'});
}

export function rearCalibrationBank() {
  return runOnParsedDocument({url: './active-speaker/rear-calibration/bank', action: 'bank'});
}

export function renderRearCalibrationPanel() {
  if (!savedTopologyHasRearOutput()) return '';
  var pending = !!rearCalibration.pending;
  return '<details class="driver-research__advanced-editor" data-rear-calibration>' +
    '<summary><span>Rear calibration</span><small>Seed, validate, and bank a cardioid ' +
    'rear-output document. Nothing here applies to the speaker.</small></summary>' +
    '<div class="driver-research__advanced-body">' +
      '<section class="driver-research__advanced-section">' +
        '<div class="driver-research__panel">' +
          '<div class="row-between">' +
            '<div><p class="setting-row__title">jts_rear_calibration document</p>' +
              '<p class="setting-row__hint">Paste a document, or seed an explicitly ' +
              'untuned, muted starting point.</p></div>' +
            '<div class="driver-research__actions">' +
              '<button type="button" class="btn btn--ghost" data-act="rear-calibration-seed" ' +
                'data-rear-calibration-action' + (pending ? ' disabled' : '') + '>Seed</button>' +
              '<button type="button" class="btn btn--ghost" data-act="rear-calibration-validate" ' +
                'data-rear-calibration-action' + (pending ? ' disabled' : '') + '>Validate</button>' +
              '<button type="button" class="btn btn--ghost" data-act="rear-calibration-bank" ' +
                'data-rear-calibration-action' + (pending ? ' disabled' : '') + '>Bank</button>' +
            '</div>' +
          '</div>' +
          '<textarea id="rear-calibration-text" class="driver-research__textarea" ' +
            'data-rear-calibration-text rows="10" placeholder="{...}" ' +
            'aria-label="Rear calibration JSON document">' +
            escapeHtml(rearCalibration.text) + '</textarea>' +
          '<p class="setting-row__hint">Mute or solo a branch for listening tests with the ' +
          'document’s own <code>rear_muted</code> and each chain’s <code>muted</code> ' +
          'boolean — there is no separate test control here.</p>' +
          '<p class="setting-row__hint">Wall gap is <code>geometry.cabinet_back_wall_m</code>, in ' +
          'metres — multiply millimetres by 0.001 or inches by 0.0254.</p>' +
          '<div id="rear-calibration-result">' + resultHtml() + '</div>' +
        '</div>' +
      '</section>' +
    '</div>' +
  '</details>';
}
