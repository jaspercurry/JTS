// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Sound profile — driver and crossover setting field markup.
//
// The HTML builders behind the driver-research panels: safety limits, pads,
// enclosure and class fields, manual crossover rows and the echo-back panel.

import { escapeHtml } from "/assets/shared/js/escape.js";
import {
  DEFAULT_SUB_CROSSOVER_HZ,
  SUB_CROSSOVER_HZ_HI,
  SUB_CROSSOVER_HZ_LO,
  humanRole,
  subwooferCrossoverFcHz
} from "/assets/sound-profile/js/active-speaker-ui.js";
import {
  crossoverAlignmentDetailText,
  currentCrossoverFrequency,
  driverClassHasRadiatingDiameter,
  driverClasses,
  driverEchoBackFields,
  driverEchoDelegationText,
  driverEvidenceForTarget,
  driverProvenanceState,
  driverResearchRoleLabel,
  driverResearchTargets,
  driverSafetyWarnings,
  driverStyleLabel,
  hfDriverStyleEntry,
  hfDriverStyles,
  kaBeamingOnsetHz,
  padKinds,
  targetModel
} from "/assets/sound-profile/js/driver-model.js";
import {
  fmtDb,
  fmtFreq,
  manualNumberValue
} from "/assets/sound-profile/js/format.js";
import {
  crossoverVocabulary,
  driverResearch
} from "/assets/sound-profile/js/state.js";
import {
  activeCrossoverPairs,
  crossoverSetting,
  crossoverSettingKey,
  driverSetting
} from "/assets/sound-profile/js/topology.js";

function driverClassGeometryFieldHtml(targetId, setting) {
  if (!driverClassHasRadiatingDiameter(setting.driver_class || 'unknown')) return '';
  return driverSafetyNumberField(targetId, setting, 'radiating_diameter_mm',
    'Radiating diameter', {min: 1, placeholder: 'mm'});
}

// Crossover (bass-management corner) control for the routed local subwoofer.
// Fc persists onto the sub channel's crossover_fc_hz via the same topology save
// POST the add/remove button uses (the household saves the draft to apply it).
// Slope/filter mirror the emitter's fixed Linkwitz-Riley 24 dB/oct and are shown
// read-only so the vocabulary matches the active-crossover card without exposing
// a knob the backend ignores.
function renderSubwooferCrossoverControl(topology) {
  var fc = subwooferCrossoverFcHz(topology);
  return '<div class="driver-settings driver-settings--crossovers">' +
    '<div class="driver-settings__row driver-settings__row--crossover">' +
      '<div class="driver-settings__pair">' +
        '<strong>' + escapeHtml('Subwoofer / mains') + '</strong>' +
        '<span>Bass-management crossover</span>' +
      '</div>' +
      '<label class="driver-research__field">' +
        '<span>Crossover point</span>' +
        '<input type="number" inputmode="numeric" min="' + escapeHtml(String(SUB_CROSSOVER_HZ_LO)) +
          '" max="' + escapeHtml(String(SUB_CROSSOVER_HZ_HI)) + '" step="1" ' +
          'data-sub-crossover-fc value="' +
          escapeHtml(fc == null ? '' : String(Math.round(Number(fc)))) +
          '" placeholder="' + escapeHtml(String(Math.round(DEFAULT_SUB_CROSSOVER_HZ))) + '"></label>' +
      '<label class="driver-research__field">' +
        '<span>Slope</span>' +
        '<input type="text" value="24 dB/oct" readonly aria-readonly="true"></label>' +
      '<label class="driver-research__field">' +
        '<span>Filter</span>' +
        '<input type="text" value="Linkwitz-Riley" readonly aria-readonly="true"></label>' +
    '</div>' +
    '<p class="setting-row__hint">' + escapeHtml(
      'Bass below ' + Math.round(Number(fc)) + ' Hz goes to the subwoofer; the mains get a ' +
      'matching high-pass at the same point. Save the draft to apply.'
    ) + '</p>' +
  '</div>';
}

function driverSafetyNumberField(targetId, setting, field, label, options) {
  options = options || {};
  var value = setting[field];
  return '<label class="driver-research__field">' +
    '<span>' + escapeHtml(label) + '</span>' +
    '<input type="number" inputmode="decimal" data-manual-driver="' +
      escapeHtml(targetId) + '" data-manual-field="' + escapeHtml(field) + '" value="' +
      escapeHtml(value == null ? '' : String(value)) + '"' +
      (options.min == null ? '' : ' min="' + escapeHtml(String(options.min)) + '"') +
      (options.max == null ? '' : ' max="' + escapeHtml(String(options.max)) + '"') +
      (options.step == null ? '' : ' step="' + escapeHtml(String(options.step)) + '"') +
      ' placeholder="' + escapeHtml(options.placeholder || '') + '">' +
    '</label>';
}
function enclosureKinds() {
  return [
    {value: 'unknown', label: 'Not sure'},
    {value: 'sealed', label: 'Sealed enclosure'},
    {value: 'vented', label: 'Ported / vented enclosure'},
    {value: 'passive_radiator', label: 'Passive-radiator enclosure'},
    {value: 'open_baffle', label: 'Open baffle'},
    {value: 'transmission_line', label: 'Transmission line'}
  ];
}
function enclosureFieldHtml(targetId, setting) {
  var enclosure = setting.enclosure_kind || '';
  return '<label class="driver-research__field">' +
    '<span>Enclosure / acoustic loading</span>' +
    '<select data-manual-driver="' + escapeHtml(targetId) +
      '" data-manual-field="enclosure_kind">' +
      '<option value="" disabled' + (enclosure ? '' : ' selected') +
        '>Choose enclosure / loading</option>' +
      enclosureKinds().map(function(item) {
        return '<option value="' + escapeHtml(item.value) + '"' +
          (enclosure === item.value ? ' selected' : '') + '>' +
          escapeHtml(item.label) + '</option>';
      }).join('') +
    '</select>' +
  '</label>';
}
function driverClassFieldHtml(targetId, setting) {
  return '<label class="driver-research__field">' +
    '<span>Driver technology class</span>' +
    '<select data-manual-driver="' + escapeHtml(targetId) +
      '" data-manual-field="driver_class">' +
      driverClasses().map(function(item) {
        return '<option value="' + escapeHtml(item.value) + '"' +
          ((setting.driver_class || 'unknown') === item.value ? ' selected' : '') +
          '>' + escapeHtml(item.label) + '</option>';
      }).join('') +
    '</select>' +
  '</label>';
}
function tweeterStyleFieldHtml(target) {
  var style = target.driver_style || '';
  return '<label class="driver-research__field">' +
    '<span>Tweeter type / loading</span>' +
    '<select data-driver-style data-save-driver-style data-group-id="' +
      escapeHtml(target.group_id) + '" data-role="' + escapeHtml(target.role) + '">' +
      '<option value="" disabled' + (style ? '' : ' selected') +
        '>Choose tweeter type</option>' +
      '<option value="unknown"' + (style === 'unknown' ? ' selected' : '') +
        '>Not sure (conservative default)</option>' +
      (style && style !== 'unknown' && !hfDriverStyleEntry(style) ?
        '<option value="' + escapeHtml(style) + '" selected>' +
          escapeHtml(driverStyleLabel(style)) + '</option>' : '') +
      hfDriverStyles().map(function(item) {
        return '<option value="' + escapeHtml(item.value) + '"' +
          (style === item.value ? ' selected' : '') + '>' +
          escapeHtml(item.label) + '</option>';
      }).join('') +
    '</select>' +
  '</label>';
}
function componentInstallationFieldHtml(target, setting) {
  if (target.role === 'tweeter') {
    return tweeterStyleFieldHtml(target);
  }
  return enclosureFieldHtml(target.target_id, setting);
}
// #2603: this hint sits exactly where the operator prepares the research
// prompt, so it must not still describe the number as a floor the
// declaration has to clear. Since the ruling the style figure is the DEFAULT
// used when a datasheet publishes nothing — a published figure wins outright,
// including a lower one. The un-styled cases name the missing declaration and
// the control that fixes it, rather than quoting 5000 Hz as if it were this
// driver's own number.
function tweeterProtectionHintHtml(target) {
  if (target.role !== 'tweeter') return '';
  if (!target.driver_style || target.driver_style === 'unknown') {
    return '<p class="setting-row__hint driver-research__field--wide">' +
      (target.driver_style === 'unknown'
        ? 'Tweeter type is not known, so JTS assumes the cautious 5000 Hz ' +
          'default. Choose a type above to use your driver type’s own figure.'
        : 'Tweeter style not set — choose a type above before copying the prompt.') +
      '</p>';
  }
  var entry = hfDriverStyleEntry(target.driver_style);
  return '<p class="setting-row__hint driver-research__field--wide">' +
    'Tweeter style: ' + escapeHtml(driverStyleLabel(target.driver_style)) +
    (entry ? ' — default minimum crossover ' +
      escapeHtml(String(entry.floor_hz)) +
      ' Hz, used only when the datasheet publishes none.' : '.') +
  '</p>';
}
function renderDriverSafetyLimits(targetId, setting, evidence) {
  return '<section class="driver-research__advanced-group">' +
    '<div><h5 class="setting-row__title">Protection and measurement limits</h5>' +
    '<p class="setting-row__hint">Hard limits are never-test-beyond edges. The measurement range must sit inside them. Filter cutoff and slope are separate because a crossover still passes some energy beyond its cutoff.</p>' +
    '<p class="setting-row__hint">Minimum crossover is the one number to enter for a driver’s bottom end — the figure its datasheet publishes. The required high-pass, the never-test-below edge and the measure-from edge are all derived from it, so a value you type into those is replaced on the next save.</p>' +
    '</div>' +
    '<div class="driver-research__fields">' +
      // #2603 decision 8: the driver's low limit is entered ONCE, here, as
      // the manufacturer's minimum recommended crossover. Until this input
      // existed the operator's only routes were pasting research or typing
      // the high-pass cutoff below — which the derivation then overwrote,
      // so a deliberate edit could vanish with no way to express it. It
      // leads the panel because everything under it derives from it.
      driverSafetyNumberField(targetId, setting, 'recommended_highpass_hz', 'Minimum crossover (datasheet)', {min: 1, placeholder: 'Hz'}) +
      driverSafetyNumberField(targetId, setting, 'recommended_highpass_slope_db_per_octave', 'Slope the datasheet states', {min: 1, max: 96, step: 6, placeholder: 'dB/oct'}) +
      driverSafetyNumberField(targetId, setting, 'hard_excitation_min_hz', 'Never test below', {min: 1, placeholder: 'Hz'}) +
      driverSafetyNumberField(targetId, setting, 'hard_excitation_max_hz', 'Never test above', {min: 1, placeholder: 'Hz'}) +
      driverSafetyNumberField(targetId, setting, 'measurement_min_hz', 'Measure from', {min: 1, placeholder: 'Hz'}) +
      driverSafetyNumberField(targetId, setting, 'measurement_max_hz', 'Measure through', {min: 1, placeholder: 'Hz'}) +
      driverSafetyNumberField(targetId, setting, 'required_highpass_cutoff_hz', 'Required high-pass cutoff (derived)', {min: 1, placeholder: 'Hz'}) +
      driverSafetyNumberField(targetId, setting, 'required_highpass_min_slope_db_per_octave', 'Minimum high-pass slope (derived)', {min: 1, max: 96, step: 6, placeholder: 'dB/oct'}) +
      '<label class="driver-research__field"><span>High-pass family / equivalent</span>' +
        '<input type="text" data-manual-driver="' + escapeHtml(targetId) + '" data-manual-field="required_highpass_family_or_equivalent" value="' + escapeHtml(setting.required_highpass_family_or_equivalent || '') + '" placeholder="equivalent or steeper"></label>' +
      driverSafetyNumberField(targetId, setting, 'required_lowpass_cutoff_hz', 'Required low-pass cutoff', {min: 1, placeholder: 'Hz'}) +
      driverSafetyNumberField(targetId, setting, 'required_lowpass_min_slope_db_per_octave', 'Minimum low-pass slope', {min: 1, max: 96, step: 6, placeholder: 'dB/oct'}) +
      '<label class="driver-research__field"><span>Low-pass family / equivalent</span>' +
        '<input type="text" data-manual-driver="' + escapeHtml(targetId) + '" data-manual-field="required_lowpass_family_or_equivalent" value="' + escapeHtml(setting.required_lowpass_family_or_equivalent || '') + '" placeholder="equivalent or steeper"></label>' +
      driverSafetyNumberField(targetId, setting, 'max_effective_peak_dbfs', 'Profile peak ceiling', {max: 0, placeholder: 'dBFS'}) +
      driverSafetyNumberField(targetId, setting, 'max_sweep_duration_s', 'Longest sweep', {min: 0.1, placeholder: 'seconds'}) +
      driverSafetyNumberField(targetId, setting, 'max_repeat_count', 'Most repeats', {min: 1, max: 16, step: 1, placeholder: 'count'}) +
      driverSafetyNumberField(targetId, setting, 'minimum_cooldown_s', 'Minimum cooldown', {min: 0, placeholder: 'seconds'}) +
    '</div>' +
    '<div><h5 class="setting-row__title">Cabinet geometry</h5>' +
      '<p class="setting-row__hint">These values refine low-frequency and directivity guidance. Unknown geometry stays explicit.</p></div>' +
    '<div class="driver-research__fields">' +
      driverSafetyNumberField(targetId, setting, 'radiator_count', 'Radiator count', {min: 1, max: 16, step: 1, placeholder: '1'}) +
      driverSafetyNumberField(targetId, setting, 'effective_radiating_diameter_mm', 'Effective radiator diameter', {min: 1, placeholder: 'mm'}) +
      driverSafetyNumberField(targetId, setting, 'baffle_width_mm', 'Baffle width', {min: 1, placeholder: 'mm'}) +
    '</div>' +
    '<section class="driver-research__evidence">' +
      '<h5 class="setting-row__title">Research evidence</h5>' +
      ((evidence && evidence.notes) ?
        '<p class="setting-row__hint"><strong>Summary:</strong> ' +
          escapeHtml(evidence.notes) + '</p>' : '') +
      ((evidence && Array.isArray(evidence.unknowns) && evidence.unknowns.length) ?
        '<p class="setting-row__hint"><strong>Explicit unknowns:</strong> ' +
          escapeHtml(evidence.unknowns.join('; ')) + '</p>' : '') +
      ((evidence && evidence.field_provenance &&
        Object.keys(evidence.field_provenance).length) ?
        '<div class="driver-research__notes"><p class="setting-row__title">Field provenance</p><ul>' +
          Object.keys(evidence.field_provenance).map(function(field) {
            var item = evidence.field_provenance[field] || {};
            return '<li>' + escapeHtml(field + ': ' + (item.basis || 'basis unknown') +
              ' [' + (item.confidence || 'unknown') + '] ' +
              (Array.isArray(item.sources) ? item.sources.join(', ') : '')) + '</li>';
          }).join('') + '</ul></div>' :
        (!(evidence && evidence.notes) &&
          !(evidence && Array.isArray(evidence.unknowns) && evidence.unknowns.length) ?
          '<p class="setting-row__hint">No research evidence loaded yet.</p>' : '')) +
    '</section>' +
    '<p class="setting-row__hint">JTS will refuse low-frequency reconstruction rather than infer a port or passive radiator.</p>' +
  '</section>';
}
// #1665: in-line pad readout. v1 is server-computed only, populated from
// the persisted record AFTER a save (setting.pad -- see
// applyDriverSafetyToSetting's docstring) -- there is no client-side
// recomputation of the L-pad formula here, so this can go briefly stale
// while the operator edits the fields below and reflects "as of last save"
// rather than the in-progress values. A live client-side preview is a
// deliberate follow-up, not this slice.
function padReadoutHtml(setting) {
  var pad = setting.pad;
  if (!pad || pad.attenuation_db == null) return '';
  var impedance = pad.effective_impedance_ohm != null
    ? ', ' + pad.effective_impedance_ohm.toFixed(1) + ' ohm effective load'
    : '';
  return '<p class="setting-row__hint">Computed on last save: ' +
    escapeHtml(fmtDb(pad.attenuation_db) + ' dB' + impedance) + '.</p>';
}
function renderDriverPadSettings(targetId, setting) {
  var kind = setting.pad_kind || 'none';
  return '<div class="component-card__pad">' +
    '<p class="setting-row__title">In-line attenuation</p>' +
    '<p class="setting-row__hint">Only if you wired a resistor pad in front of this driver to match its level to the others. Leave as No pad if it is wired straight to the amp.</p>' +
    '<div class="component-card__fields">' +
      '<label class="driver-research__field">' +
        '<span>Pad type</span>' +
        '<select data-manual-driver="' + escapeHtml(targetId) + '" data-manual-field="pad_kind">' +
          padKinds().map(function(item) {
            return '<option value="' + escapeHtml(item.value) + '"' +
              (kind === item.value ? ' selected' : '') +
              '>' + escapeHtml(item.label) + '</option>';
          }).join('') +
        '</select>' +
      '</label>' +
      (kind === 'l_pad' || kind === 'series_resistor'
        ? driverSafetyNumberField(targetId, setting, 'pad_series_ohm',
            'Series resistor', {min: 0.1, step: 0.1, placeholder: 'ohm'})
        : '') +
      (kind === 'l_pad'
        ? driverSafetyNumberField(targetId, setting, 'pad_shunt_ohm',
            'Shunt resistor', {min: 0.1, step: 0.1, placeholder: 'ohm'})
        : '') +
      (kind === 'direct_db'
        ? driverSafetyNumberField(targetId, setting, 'pad_attenuation_db',
            'Attenuation', {max: 0, step: 0.1, placeholder: 'dB'})
        : '') +
    '</div>' +
    padReadoutHtml(setting) +
  '</div>';
}

function renderComponentSettings(topology) {
  var targets = driverResearchTargets(topology);
  return '<p class="setting-row__hint">Add one card for every independently amplified driver in the saved layout. JTS treats the model and physical loading you choose as authoritative.</p>' +
    '<div class="component-list">' + targets.map(function(target) {
    var role = target.role;
    var targetId = target.target_id;
    var setting = driverSetting(targetId);
    return '<section class="component-card">' +
      '<div class="component-card__head">' +
        '<div><h4 class="setting-row__title">' +
          escapeHtml(driverResearchRoleLabel(role)) + '</h4>' +
          '<p class="setting-row__hint">' +
            escapeHtml(target.group_label + ' · ' + target.output_label) + '</p></div>' +
      '</div>' +
      '<div class="component-card__fields">' +
        '<label class="driver-research__field driver-research__field--wide">' +
        '<span>Manufacturer and model</span>' +
        '<input type="text" data-driver-target="' + escapeHtml(targetId) + '" value="' +
          escapeHtml(targetModel(target, topology)) + '" placeholder="Manufacturer and model">' +
        '</label>' +
        componentInstallationFieldHtml(target, setting) +
        tweeterProtectionHintHtml(target) +
      '</div>' +
      renderDriverPadSettings(targetId, setting) +
    '</section>';
  }).join('') + '</div>';
}
// The guided bullets (ticket 1.6). `&#10;` is a literal newline inside the
// quoted attribute, which a textarea placeholder renders as a list -- so the
// prompt is a shape the operator can fill in rather than a sentence they
// have to decompose. Every line asks for a fact no measurement can recover:
// which waveguide the tweeter is on (a grader that does not know reads its
// beaming as a defect), what the box is, and why it was built that way.
var BUILD_NOTES_PLACEHOLDER = [
  'For example:',
  '- Horn or waveguide: kind, size, nominal coverage angle',
  '- Enclosure: sealed or ported, volume, port tuning',
  '- Amplifier, wiring, padding, mounting',
  '- Why you built it this way'
].join('&#10;');
// This is the ONE free-text field the page offers, and since #2871 it has
// two readers: the research assistant when a prompt is copied, and the
// tuning assistant, which reads it out of the evidence packet's quarantined
// operator_notes block while grading a measured round. The hint says so, and
// says what neither reader will do with it -- the field is exactly where
// someone would otherwise try to give an order.
function renderBuildNotes() {
  return '<section class="driver-research__section driver-research__build-notes">' +
    '<div><h3 class="setting-row__title">Build notes</h3>' +
      '<p class="setting-row__hint">Optional. Describe what a measurement cannot see. Two assistants read this: the one that researches your drivers, and the one that grades your measurements. Both treat it as information about your build, never as an instruction.</p></div>' +
    '<label class="driver-research__field driver-research__field--wide">' +
      '<span>Additional build information</span>' +
      '<textarea rows="6" maxlength="1000" data-driver-field="notes" ' +
        'placeholder="' + BUILD_NOTES_PLACEHOLDER + '">' +
        escapeHtml(driverResearch.inputs.notes || '') + '</textarea>' +
    '</label>' +
  '</section>';
}
function renderAdvancedDriverSettings(topology) {
  return '<div class="driver-research__advanced-drivers">' +
    driverResearchTargets(topology).map(function(target) {
      var targetId = target.target_id;
      var setting = driverSetting(targetId);
      return '<section class="driver-research__advanced-driver">' +
        '<div><h4 class="setting-row__title">' +
          escapeHtml(target.group_label + ' · ' + driverResearchRoleLabel(target.role)) +
          '</h4><p class="setting-row__hint">' +
          escapeHtml(target.output_label) + '</p></div>' +
        '<div class="driver-research__advanced-group">' +
          '<p class="setting-row__title">Driver specifications</p>' +
          '<div class="driver-research__fields">' +
            '<label class="driver-research__field">' +
              '<span>Sensitivity</span>' +
              '<input type="number" inputmode="decimal" data-manual-driver="' +
                escapeHtml(targetId) + '" data-manual-field="sensitivity_db_2v83_1m" value="' +
                escapeHtml(setting.sensitivity_db_2v83_1m == null ? '' :
                  String(setting.sensitivity_db_2v83_1m)) + '" placeholder="dB">' +
            '</label>' +
            driverSafetyNumberField(targetId, setting, 'nominal_impedance_ohm',
              'Nominal impedance', {min: 1, step: 0.1, placeholder: 'ohm'}) +
            driverClassFieldHtml(targetId, setting) +
            driverClassGeometryFieldHtml(targetId, setting) +
            '<label class="driver-research__field">' +
              '<span>Legacy advisory floor (not enforced)</span>' +
              '<input type="number" inputmode="numeric" min="1" data-manual-driver="' +
                escapeHtml(targetId) + '" data-manual-field="do_not_test_below_hz" value="' +
                escapeHtml(setting.do_not_test_below_hz == null ? '' :
                  String(setting.do_not_test_below_hz)) + '" placeholder="Hz">' +
            '</label>' +
            '<label class="driver-research__field">' +
              '<span>Level trim</span>' +
              '<input type="number" inputmode="decimal" data-manual-driver="' +
                escapeHtml(targetId) + '" data-manual-field="gain_offset_db" value="' +
                escapeHtml(setting.gain_offset_db == null ? '' :
                  String(setting.gain_offset_db)) + '" placeholder="dB">' +
            '</label>' +
          '</div>' +
        '</div>' +
        renderDriverSafetyLimits(targetId, setting,
          driverEvidenceForTarget(targetId)) +
      '</section>';
    }).join('') +
  '</div>';
}
// Shared by the two per-region polarity selects below. 'non-inverted' is the
// unset default (mirrors SUPPORTED_POLARITY in jasper/active_speaker/profile.py).
function manualPolarityFieldHtml(key, field, label, value) {
  return '<label class="driver-research__field">' +
    '<span>' + escapeHtml(label) + '</span>' +
    '<select data-manual-crossover="' + escapeHtml(key) + '" data-manual-field="' + field + '">' +
      [['non-inverted', 'Normal'], ['inverted', 'Inverted']].map(function(option) {
        return '<option value="' + escapeHtml(option[0]) + '"' +
          ((value || 'non-inverted') === option[0] ? ' selected' : '') +
          '>' + escapeHtml(option[1]) + '</option>';
      }).join('') +
    '</select>' +
  '</label>';
}
function renderManualCrossoverAlignment(pair, key, setting) {
  var delayValue = setting.delay_ms == null ? '' : String(setting.delay_ms);
  return '<section class="driver-research__alignment">' +
    '<div><h5 class="setting-row__title">Alignment</h5>' +
      '<p class="setting-row__hint">Review polarity and timing for this crossover region.</p></div>' +
    '<div class="driver-research__fields">' +
      manualPolarityFieldHtml(key, 'lower_polarity', humanRole(pair[0]), setting.lower_polarity) +
      manualPolarityFieldHtml(key, 'upper_polarity', humanRole(pair[1]), setting.upper_polarity) +
      '<label class="driver-research__field">' +
        '<span>Delay</span>' +
        '<input type="number" inputmode="decimal" min="0" max="20" step="0.01" ' +
          'data-manual-crossover="' + escapeHtml(key) + '" data-manual-field="delay_ms" value="' +
          escapeHtml(delayValue) + '" placeholder="ms">' +
      '</label>' +
      '<label class="driver-research__field">' +
        '<span>Delayed driver</span>' +
        '<select data-manual-crossover="' + escapeHtml(key) + '" data-manual-field="delay_target_role">' +
          [['', 'No delay'], [pair[0], humanRole(pair[0])], [pair[1], humanRole(pair[1])]]
            .map(function(option) {
              return '<option value="' + escapeHtml(option[0]) + '"' +
                ((setting.delay_target_role || '') === option[0] ? ' selected' : '') +
                '>' + escapeHtml(option[1]) + '</option>';
            }).join('') +
        '</select>' +
      '</label>' +
    '</div>' +
  '</section>';
}
// #1675 (simple v1): ka-beaming guidance for the crossover point. Whether
// the LOWER-role driver of this pair (the one reproducing UP TO the
// chosen Fc from below -- the upper/tweeter role is not evaluated, its
// own beaming onset is a separate question from where THIS crossover sits)
// is still acting as a small, uniform piston at that frequency, or has
// grown acoustically large enough to narrow its directivity (ka=1) or beam
// outright (ka=2) -- a geometry limit no EQ curve can correct. Circular-
// piston approximation, so only meaningful when the driver has a declared
// radiating_diameter_mm (never shown otherwise -- e.g. a horn-loaded or
// ribbon/AMT driver, or an undeclared one; see
// driverClassHasRadiatingDiameter above). This is the whole of #1675, which
// closed 2026-08-08: matching a woofer's beamwidth against a waveguide's
// rated coverage was never built, and the structured coverage field that
// waited for it is gone (#2872).
function kaBeamingNoteHtml(pair, fcRaw, topology) {
  var target = driverResearchTargets(topology).filter(function(item) {
    return item.role === pair[0];
  })[0];
  var diameterMm = target
    ? manualNumberValue(driverSetting(target.target_id).radiating_diameter_mm)
    : null;
  var ka = diameterMm != null ? kaBeamingOnsetHz(diameterMm) : null;
  var fc = manualNumberValue(fcRaw);
  if (!ka || fc == null || fc < ka.ka1Hz) return '';
  var text = fc < ka.ka2Hz
    ? humanRole(pair[0]) + ' is starting to narrow (ka≈1–2) by ' + fmtFreq(fc) + '.'
    : humanRole(pair[0]) + ' is beaming (ka≥2) by ' + fmtFreq(fc) +
      ' — EQ cannot fix that, only geometry (a smaller or horn-loaded driver) can.';
  return '<p class="setting-row__hint">' + escapeHtml(text) + '</p>';
}
// One picker builder for both crossover-vocabulary selects. A stored value
// outside the offer gets its own clearly-labelled option, the same way
// tweeterStyleFieldHtml carries an off-list driver style: without it the
// control would DISPLAY the first offered value while the model still held
// the stored one, so no control on the page would contain what is actually
// set — and re-picking the value already shown fires no change event, which
// leaves the operator no way to clear it. Nothing is coerced;
// manualCrossoverVocabularyValidationError still refuses the save.
function crossoverOptionsHtml(values, selected, labelFor) {
  var chosen = selected == null ? '' : String(selected);
  var offered = values.map(String);
  var offList = chosen && offered.indexOf(chosen) < 0
    ? '<option value="' + escapeHtml(chosen) + '" selected>' +
      escapeHtml((labelFor ? labelFor(selected) : chosen) + ' (not supported)') +
      '</option>'
    : '';
  return offList + values.map(function(value) {
    var raw = String(value);
    return '<option value="' + escapeHtml(raw) + '"' +
      (raw === chosen ? ' selected' : '') + '>' +
      escapeHtml(labelFor ? labelFor(value) : raw) + '</option>';
  }).join('');
}
function renderManualCrossoverSettings(topology) {
  var pairs = activeCrossoverPairs(topology);
  if (!pairs.length) {
    return '<p class="setting-row__hint">This speaker layout does not need an active crossover point.</p>';
  }
  return '<div class="driver-settings driver-settings--crossovers">' + pairs.map(function(pair) {
    var setting = crossoverSetting(pair);
    var key = crossoverSettingKey(pair);
    return '<div class="driver-settings__row driver-settings__row--crossover">' +
      '<div class="driver-settings__pair">' +
        '<strong>' + escapeHtml(humanRole(pair[0]) + ' / ' + humanRole(pair[1])) + '</strong>' +
        '<span>Starting crossover</span>' +
      '</div>' +
      '<label class="driver-research__field">' +
        '<span>Crossover point</span>' +
        '<input type="number" inputmode="numeric" min="1" data-manual-crossover="' + escapeHtml(key) + '" ' +
          'data-manual-field="frequency_hz" value="' +
          escapeHtml(setting.frequency_hz == null ? '' : String(setting.frequency_hz)) +
          '" placeholder="Hz">' +
      '</label>' +
      '<div data-ka-note="' + escapeHtml(key) + '">' +
        kaBeamingNoteHtml(pair, setting.frequency_hz, topology) +
      '</div>' +
      '<label class="driver-research__field">' +
        '<span>Slope</span>' +
        '<select data-manual-crossover="' + escapeHtml(key) + '" data-manual-field="slope_db_per_octave">' +
          crossoverOptionsHtml(
            crossoverVocabulary.slopes,
            setting.slope_db_per_octave == null ?
              crossoverVocabulary.defaultSlope : setting.slope_db_per_octave,
            function(value) { return String(value) + ' dB/oct'; }
          ) +
        '</select>' +
      '</label>' +
      '<label class="driver-research__field">' +
        '<span>Filter</span>' +
        '<select data-manual-crossover="' + escapeHtml(key) + '" data-manual-field="filter_type">' +
          crossoverOptionsHtml(
            crossoverVocabulary.filterTypes,
            setting.filter_type || crossoverVocabulary.defaultFilterType
          ) +
        '</select>' +
      '</label>' +
      renderManualCrossoverAlignment(pair, key, setting) +
    '</div>';
  }).join('') + '</div>';
}

// One citation, linkified only when it really is a web address. `source` is
// a free string by design (a datasheet is often a NAME, not a URL), so the
// http(s) test is what decides; everything else renders as escaped text.
// Both branches escape — this string came from an LLM reply the operator
// pasted, which is untrusted input.
function provenanceSourceHtml(entry) {
  var sources = (entry && Array.isArray(entry.sources)) ? entry.sources : [];
  var raw = (entry && entry.source) || sources[0] || '';
  var source = String(raw == null ? '' : raw).trim();
  if (!source) {
    return '<span class="driver-echo__source driver-echo__source--empty">' +
      'no source given</span>';
  }
  // target="_blank" because this panel renders BEFORE anything is saved:
  // following a citation in this tab would navigate away from the pasted
  // JSON the operator is checking. Same convention as every other external
  // link in the management UI; rel guards the opener either way.
  if (/^https?:\/\/[^\s]+$/i.test(source)) {
    return '<a class="driver-echo__source" href="' + escapeHtml(source) +
      '" target="_blank" rel="noreferrer noopener">' +
      escapeHtml(source) + '</a>';
  }
  return '<span class="driver-echo__source">' + escapeHtml(source) + '</span>';
}

function driverEchoBackRowsHtml(targetId, driver) {
  var setting = driverSetting(targetId);
  var provenance = (driver && driver.field_provenance) || {};
  var rows = driverEchoBackFields().map(function(field) {
    if (!driver || driver[field.key] == null) return '';
    var value = field.read(setting);
    var entry = provenance[field.key];
    return '<div class="driver-echo__row">' +
      '<dt>' + escapeHtml(field.label) + '</dt>' +
      '<dd>' +
        '<span class="driver-echo__value">' +
          escapeHtml(value || 'not set') + '</span>' +
        '<span class="status-pill' +
          (driverProvenanceState(entry) === 'confirmed' ?
            ' status-pill--ready' : '') + '">' +
          escapeHtml(driverProvenanceState(entry)) + '</span>' +
        provenanceSourceHtml(entry) +
      '</dd>' +
    '</div>';
  }).filter(Boolean).join('');
  var delegation = driverEchoDelegationText(targetId, setting);
  return (rows ? '<dl class="driver-echo__rows">' + rows + '</dl>' : '') +
    (delegation ? '<p class="setting-row__hint">' +
      escapeHtml(delegation) + '</p>' : '');
}
function renderDriverEchoBack(topology) {
  var payload = driverResearch.importedPayload;
  if (!payload || !Array.isArray(payload.drivers)) return '';
  // A v2 packet whose binding a visible edit invalidated is not describing
  // this speaker any more. Same currency rule driverEvidenceForTarget uses.
  if (Number(payload.artifact_schema_version || 1) === 2 &&
      !driverResearch.researchRequest) {
    return '';
  }
  var blocks = driverResearchTargets(topology).map(function(target) {
    var driver = payload.drivers.filter(function(item) {
      return item && String(item.target_id || '') === target.target_id;
    })[0];
    if (!driver) return '';
    // Once the operator edits a target's values, the reply's badges stop
    // describing them. Suppress rather than mislabel -- the same call
    // driverEvidenceForTarget makes for the Advanced evidence block.
    if (driverResearch.editedDriverTargets[target.target_id]) {
      return '<section class="driver-echo__driver">' +
        '<h4 class="setting-row__title">' +
          escapeHtml(driverResearchRoleLabel(target.role)) + '</h4>' +
        '<p class="setting-row__hint">You changed these values, so the ' +
          'research reply no longer describes them.</p>' +
      '</section>';
    }
    var rows = driverEchoBackRowsHtml(target.target_id, driver);
    if (!rows) return '';
    return '<section class="driver-echo__driver">' +
      '<h4 class="setting-row__title">' +
        escapeHtml(driverResearchRoleLabel(target.role)) + '</h4>' +
      rows +
    '</section>';
  }).filter(Boolean).join('');
  if (!blocks) return '';
  return '<div class="driver-research__panel driver-echo">' +
    // The completeness claim is scoped to the set driverEchoBackFields
    // actually renders, and it has to stay that way: an earlier draft said
    // "every value the research reply gave us" while three frozen fields
    // went unechoed, which is the one thing a check-before-you-confirm
    // surface cannot be wrong about.
    '<div><p class="setting-row__title">3. What JTS is running with</p>' +
      '<p class="setting-row__hint">Every value the research reply gave us ' +
      'that JTS asked it to source, or that gets frozen into this ' +
      'speaker&rsquo;s safety limits. Each one shows whether it was a ' +
      'published figure or an estimate. Check anything that looks wrong ' +
      'before you confirm.</p></div>' +
    blocks +
  '</div>';
}

function renderDriverSafetyWarnings() {
  var warnings = driverSafetyWarnings();
  if (!warnings.length) return '';
  return '<div class="driver-research__section driver-research__confirm">' +
    '<div><h3 class="setting-row__title">JTS is trusting your declaration</h3>' +
      warnings.map(function(issue) {
        return '<p class="setting-row__hint">' +
          escapeHtml(String(issue.message)) + '</p>';
      }).join('') +
    '</div>' +
  '</div>';
}

function renderIssueList(issues, maxItems) {
  issues = Array.isArray(issues) ? issues : [];
  if (!issues.length) return '';
  return '<ul class="active-speaker-issues">' + issues.slice(0, maxItems || 5).map(function(issue) {
    var severity = issue && issue.severity === 'warning' ? 'warning' : 'blocker';
    return '<li class="active-speaker-issue active-speaker-issue--' + escapeHtml(severity) + '">' +
      escapeHtml((issue && (issue.message || issue.code)) || 'review required') +
    '</li>';
  }).join('') + '</ul>';
}
function renderPreviewIssues(issues) {
  return renderIssueList(issues, 5);
}

function renderCrossoverPreviewRows(payload) {
  var groups = Array.isArray(payload.groups) ? payload.groups : [];
  var rows = [];
  groups.forEach(function(group) {
    (Array.isArray(group.crossovers) ? group.crossovers : []).forEach(function(crossover) {
      var roles = Array.isArray(crossover.between_roles) ? crossover.between_roles : [];
      var filter = (Array.isArray(crossover.filters) && crossover.filters[0]) || {};
      var label = (group.label || group.group_id || 'Speaker') + ': ' + roles.join(' / ');
      var detail = crossover.proposed_frequency_hz ?
        fmtFreq(crossover.proposed_frequency_hz) + ', ' +
        (filter.filter_type || 'filter') + ', ' +
        String(filter.slope_db_per_octave || 24) + ' dB/oct' :
        'needs research';
      var alignment = crossoverAlignmentDetailText(crossover, roles);
      if (alignment) detail += ', ' + alignment;
      rows.push('<div><dt>' + escapeHtml(label) + '</dt><dd>' + escapeHtml(detail) + '</dd></div>');
    });
  });
  if (!rows.length) {
    rows.push('<div><dt>Preview</dt><dd>No active crossover candidate prepared yet.</dd></div>');
  }
  return '<dl class="active-speaker-facts output-facts">' + rows.join('') + '</dl>';
}
function renderWorkingCrossoverRows(topology) {
  var pairs = activeCrossoverPairs(topology);
  if (!pairs.length) {
    return '<dl class="active-speaker-facts output-facts">' +
      '<div><dt>Proposal</dt><dd>This layout does not need an active crossover.</dd></div>' +
    '</dl>';
  }
  return '<dl class="active-speaker-facts output-facts">' + pairs.map(function(pair) {
    var setting = crossoverSetting(pair);
    var frequency = currentCrossoverFrequency(pair);
    var detail = frequency == null ? 'Waiting for researched or advanced values' :
      fmtFreq(frequency) + ', ' +
      (setting.filter_type || crossoverVocabulary.defaultFilterType) + ', ' +
      String(setting.slope_db_per_octave || crossoverVocabulary.defaultSlope) + ' dB/oct';
    var alignment = crossoverAlignmentDetailText(setting, pair);
    if (alignment) detail += ', ' + alignment;
    return '<div><dt>' + escapeHtml(humanRole(pair[0]) + ' / ' +
      humanRole(pair[1])) + '</dt><dd>' + escapeHtml(detail) + '</dd></div>';
  }).join('') + '</dl>';
}

export {
  kaBeamingNoteHtml,
  renderAdvancedDriverSettings,
  renderBuildNotes,
  renderComponentSettings,
  renderCrossoverPreviewRows,
  renderDriverEchoBack,
  renderDriverSafetyWarnings,
  renderIssueList,
  renderManualCrossoverSettings,
  renderPreviewIssues,
  renderSubwooferCrossoverControl,
  renderWorkingCrossoverRows,
};
