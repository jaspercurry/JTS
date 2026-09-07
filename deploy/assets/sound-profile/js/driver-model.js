// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Sound profile — driver-research and crossover model.
//
// Derives the driver/crossover working state from the topology plus the
// research record: targets, safety settings, validation and the payload
// summaries the setup flow reads. No rendering, no IO.

import {
  activeCommissionGroup,
  humanRole,
  sensitivityTrimsFromGap
} from "/assets/sound-profile/js/active-speaker-ui.js";
import {
  activeRoleLabel,
  fmtDb,
  fmtFreq,
  joinListText,
  manualNumberValue,
  roleListText
} from "/assets/sound-profile/js/format.js";
import {
  crossoverPreview,
  crossoverVocabulary,
  driverResearch,
  outputTopology
} from "/assets/sound-profile/js/state.js";
import {
  activeCrossoverPairs,
  crossoverSetting,
  crossoverSettingKey,
  driverSetting,
  outputGroups,
  outputRoleSummary,
  pairRoleKey,
  physicalOutputLabel
} from "/assets/sound-profile/js/topology.js";

var DRIVER_RESEARCH_NOTE_MAX_CHARS = 2048;

function driverResearchRoles(topology) {
  var pairs = activeCrossoverPairs(topology);
  if (!pairs.length) return ['full_range'];
  var roles = [];
  pairs.forEach(function(pair) {
    pair.forEach(function(role) {
      if (roles.indexOf(role) < 0) roles.push(role);
    });
  });
  var order = {full_range: 0, woofer: 1, mid: 2, tweeter: 3};
  return roles.sort(function(a, b) {
    return (order[a] || 99) - (order[b] || 99);
  });
}
function driverResearchTargets(topology) {
  var allowedRoles = driverResearchRoles(topology);
  var hasActivePairs = activeCrossoverPairs(topology).length > 0;
  var targets = [];
  outputGroups(topology).forEach(function(group) {
    if (!hasActivePairs && group.mode !== 'full_range_passive') return;
    (Array.isArray(group.channels) ? group.channels : []).forEach(function(channel) {
      var role = String(channel.role || '');
      if (allowedRoles.indexOf(role) < 0) return;
      targets.push({
        target_id: String(group.id || '') + ':' + role,
        role: role,
        group_id: String(group.id || ''),
        group_label: String(group.label || group.id || 'Speaker'),
        output_index: channel.physical_output_index,
        output_label: channel.human_output_label ||
          (channel.physical_output_index == null ? 'Unassigned output' :
            physicalOutputLabel(topology, channel.physical_output_index)),
        driver_style: channel.driver_style || null
      });
    });
  });
  return targets;
}
function targetModel(target, topology) {
  var targetModels = driverResearch.inputs.target_models || {};
  var explicit = String(targetModels[target.target_id] || '').trim();
  if (explicit) return explicit;
  var sameRole = driverResearchTargets(topology).filter(function(item) {
    return item.role === target.role;
  });
  return sameRole.length === 1
    ? String(driverResearch.inputs[target.role] || '').trim()
    : '';
}

function driverResearchRoleLabel(role) {
  return {
    woofer: 'Woofer / midbass',
    mid: 'Midrange',
    tweeter: 'Tweeter / high-frequency driver',
    subwoofer: 'Subwoofer'
  }[role] || humanRole(role);
}
// Operator-facing catalog of tweeter driver styles. Display-only: the
// authoritative table lives in jasper/active_speaker/driver_protection.py
// (_STYLE_HIGH_PASS_HZ). Keep the floor_hz values here in sync with it
// (tests/test_driver_style_floor_contract.py enforces that).
// The `floor_hz` KEY NAME predates #2603 and is now a misnomer worth reading
// carefully: since that ruling the figure is not a floor the declaration must
// clear. It is the default when a datasheet publishes nothing, the anchor of
// the plausibility band, and — since #2874 — the commissioning-tone gate's
// FALLBACK for a driver that declares nothing. A published figure BELOW it is
// accepted outright, and gates a tone at its own value.
// "horn_compression_driver" is a valid style value (same 2000 Hz figure as
// compression_driver) but is not offered as a separate option here — one
// driver type should not appear twice in the picker.
function hfDriverStyles() {
  return [
    {value: 'dome_tweeter', label: 'Dome tweeter', floor_hz: 3000},
    {value: 'amt_tweeter', label: 'AMT tweeter (Air Motion Transformer)', floor_hz: 3000},
    {value: 'planar_tweeter', label: 'Planar-magnetic tweeter', floor_hz: 3500},
    {value: 'ribbon_tweeter', label: 'Ribbon tweeter', floor_hz: 5000},
    {value: 'compression_driver', label: 'Compression driver (horn-loaded)', floor_hz: 2000},
    {value: 'supertweeter', label: 'Supertweeter', floor_hz: 8000}
  ];
}
function hfDriverStyleEntry(style) {
  return hfDriverStyles().filter(function(item) { return item.value === style; })[0] || null;
}
function driverStyleLabel(style) {
  var entry = hfDriverStyleEntry(style);
  return entry ? entry.label : String(style || '').replace(/_/g, ' ');
}
// #1665 advanced driver detail: the driver's physical technology, which feeds
// jasper.active_speaker.linearization_envelope.compose_envelope's
// class_prior_limit() term (a more conservative correction ceiling for a
// class known to run out of linear excursion or HF extension sooner).
// Distinct from driver_style above (topology-owned; it drives the tweeter's
// default minimum crossover, the plausibility band, and the commissioning-
// tone gate's fallback — see driver_protection.py): driver_class applies to
// every role and is saved on manual_settings.drivers, mirroring DRIVER_CLASSES
// in jasper/active_speaker/_common.py.
function driverClasses() {
  return [
    {value: 'unknown', label: 'Unknown'},
    {value: 'soft_dome', label: 'Soft dome'},
    {value: 'metal_dome', label: 'Metal dome'},
    {value: 'beryllium_diamond_dome', label: 'Beryllium / diamond dome'},
    {value: 'ribbon_amt', label: 'Ribbon / AMT'},
    {value: 'compression_horn', label: 'Compression horn'}
  ];
}
// A round radiator has a radiating diameter, and that diameter is what the
// ka beaming guidance below reads. Horn-loaded and ribbon/AMT drivers do
// not: no simple piston diameter describes either, so the wizard asks for
// no geometry at all and the guidance stays silent for that driver.
// Waveguide identity and nominal coverage belong in this driver's notes as
// prose (#2872) -- there is no structured coverage field, because nothing
// ever computed from one. See design_draft.py's _normalise_driver_common.
function driverClassHasRadiatingDiameter(driverClass) {
  return driverClass !== 'compression_horn' && driverClass !== 'ribbon_amt';
}

// #1665: an operator-declared in-line pad (L-pad / series resistor / a
// purchased fixed attenuator). A PHYSICAL fact about how the driver is
// wired -- distinct from gain_offset_db (a level trim baked into the
// crossover filter) -- so it is never AI-researched; only the operator
// knows what resistors they actually wired in. Mirrors PAD_KINDS in
// jasper/active_speaker/driver_pad.py.
function padKinds() {
  return [
    {value: 'none', label: 'No pad'},
    {value: 'l_pad', label: 'L-pad (series + shunt resistor)'},
    {value: 'series_resistor', label: 'Series resistor only'},
    {value: 'direct_db', label: 'Known attenuation (dB)'}
  ];
}
// #1675 (simple v1): ka-beaming guidance. f_ka1 is the frequency at which
// a circular piston of this diameter starts to narrow its directivity
// (ka=1, the classic onset heuristic); f_ka2 (ka=2) is where it is
// beaming outright -- a geometry limit no EQ curve can correct. f_ka1 is
// rounded to an integer FIRST so the displayed "2x" relationship is always
// exact (343/(2*pi*r) computed then doubled can differ from the isolated
// ka=2 formula by a rounding unit at the last digit; rounding once here
// avoids ever showing two numbers whose ratio looks like a bug). Mirrored
// in Python by test_ka_beaming_onset_hz_matches_the_js_closed_form in
// tests/test_active_speaker_driver_pad.py -- keep the two in lockstep.
function kaBeamingOnsetHz(diameterMm) {
  var d = Number(diameterMm);
  if (!isFinite(d) || d <= 0) return null;
  var radiusM = d / 2000;
  var ka1Hz = Math.round(343 / (2 * Math.PI * radiusM));
  return {ka1Hz: ka1Hz, ka2Hz: ka1Hz * 2};
}

function candidateMatchesPair(candidate, pair) {
  return candidate && Array.isArray(candidate.between_roles) &&
    candidate.between_roles.length === 2 &&
    pairRoleKey(candidate.between_roles) === pairRoleKey(pair);
}
function candidateFrequency(candidate) {
  var frequency = manualNumberValue(candidate && candidate.frequency_hz);
  return frequency != null && frequency > 0 ? frequency : null;
}
function designDraftCandidates() {
  var draftPayload = driverResearch.designDraft || {};
  var manual = draftPayload.manual_settings || {};
  var research = draftPayload.driver_research || {};
  return []
    .concat(Array.isArray(manual.crossover_candidates) ? manual.crossover_candidates : [])
    .concat(Array.isArray(research.crossover_candidates) ? research.crossover_candidates : []);
}
function designDraftDrivers() {
  var draftPayload = driverResearch.designDraft || {};
  var manual = draftPayload.manual_settings || {};
  var research = draftPayload.driver_research || {};
  return []
    .concat(Array.isArray(manual.drivers) ? manual.drivers : [])
    .concat(Array.isArray(research.drivers) ? research.drivers : []);
}
function draftCrossoverFrequency(pair) {
  var candidates = designDraftCandidates();
  for (var i = 0; i < candidates.length; i += 1) {
    if (candidateMatchesPair(candidates[i], pair)) {
      var frequency = candidateFrequency(candidates[i]);
      if (frequency != null) return frequency;
    }
  }
  return null;
}
function currentCrossoverFrequency(pair) {
  var crossovers = driverResearch.settings && driverResearch.settings.crossovers || {};
  var key = crossoverSettingKey(pair);
  var setting = crossovers[key] || {};
  if (Object.prototype.hasOwnProperty.call(setting, 'frequency_hz')) {
    return candidateFrequency(setting);
  }
  return draftCrossoverFrequency(pair);
}
function driverForTarget(target, topology) {
  var setting = driverResearch.settings && driverResearch.settings.drivers &&
    driverResearch.settings.drivers[target.target_id] || {};
  var drivers = designDraftDrivers();
  var draftDriver = {};
  for (var i = 0; i < drivers.length; i += 1) {
    if (drivers[i] && String(drivers[i].target_id || '') === target.target_id) {
      draftDriver = drivers[i];
      break;
    }
  }
  if (!draftDriver.target_id) {
    var sameRoleTargets = driverResearchTargets(topology).filter(function(item) {
      return item.role === target.role;
    });
    if (sameRoleTargets.length === 1) {
      for (var j = 0; j < drivers.length; j += 1) {
        if (drivers[j] && !drivers[j].target_id &&
            String(drivers[j].role || '') === target.role) {
          draftDriver = drivers[j];
          break;
        }
      }
    }
  }
  return Object.assign({}, draftDriver, setting, {
    target_id: target.target_id,
    role: target.role,
    model: targetModel(target, topology) || setting.model || draftDriver.model || ''
  });
}
function driverSafetyNoteRoles(topology) {
  return driverResearchTargets(topology).filter(function(target) {
    var driver = driverForTarget(target, topology);
    return !!(driver.recommended_highpass_hz != null ||
      driver.recommended_lowpass_hz != null ||
      driver.do_not_test_below_hz != null ||
      driver.gain_offset_db != null ||
      driver.notes);
  }).map(function(target) { return target.role; });
}
function workingCrossoverSummary(topology) {
  var pairs = activeCrossoverPairs(topology);
  if (!pairs.length) {
    return {
      ready: true,
      text: 'no active crossover point is needed for this layout'
    };
  }
  var entries = [];
  var missing = [];
  pairs.forEach(function(pair) {
    var frequency = currentCrossoverFrequency(pair);
    if (frequency == null) {
      missing.push(pair);
      return;
    }
    entries.push({
      pair: pair,
      label: activeRoleLabel(pair[0]) + '/' + activeRoleLabel(pair[1]),
      frequency: frequency
    });
  });
  if (!entries.length) {
    return {
      ready: false,
      text: 'Add crossover points before previewing the active crossover.'
    };
  }
  var text = entries.length === 1 && pairs.length === 1
    ? 'crossover ' + fmtFreq(entries[0].frequency)
    : 'Crossovers: ' + entries.map(function(entry) {
      return entry.label + ' ' + fmtFreq(entry.frequency);
    }).join(', ');
  if (missing.length) {
    text += '. Add the remaining crossover point before previewing the active crossover.';
  }
  return {ready: !missing.length, text: text};
}
function workingSetupSummary(topology) {
  if (!topology || !outputGroups(topology).length) {
    return 'Choose a speaker layout to start the working setup. No filters are active yet.';
  }
  var roles = outputRoleSummary(topology);
  var crossover = workingCrossoverSummary(topology);
  var text = 'Working setup: ' + roleListText(roles);
  if (crossover.text.indexOf('Crossovers:') === 0 ||
      crossover.text.charAt(crossover.text.length - 1) === '.') {
    text += '. ' + crossover.text;
  } else {
    text += ', ' + crossover.text + '.';
  }
  if (crossover.ready) text += ' No filters are active yet.';
  return text;
}
function driverResearchHasPreviewInputs(topology) {
  if (!topology || !outputGroups(topology).length) return false;
  var rolesReady = driverResearchTargets(topology).every(function(target) {
    var driver = driverForTarget(target, topology);
    return !!(driver.model || driver.sensitivity_db_2v83_1m != null ||
      driver.nominal_impedance_ohm != null || driver.recommended_highpass_hz != null ||
      driver.recommended_lowpass_hz != null || driver.do_not_test_below_hz != null ||
      driver.gain_offset_db != null || driver.notes);
  });
  var pairs = activeCrossoverPairs(topology);
  var crossoversReady = !pairs.length || pairs.every(function(pair) {
    return currentCrossoverFrequency(pair) != null;
  });
  return rolesReady && crossoversReady;
}

function driverResearchMissingPreviewMessage(topology) {
  if (!topology || !outputGroups(topology).length) {
    return 'Choose and save a speaker layout before previewing the active crossover.';
  }
  if (!activeCrossoverPairs(topology).length) {
    return 'This one-driver layout does not need an active crossover.';
  }
  var missingDrivers = driverResearchTargets(topology).filter(function(target) {
    return !driverForTarget(target, topology).model;
  });
  if (missingDrivers.length) {
    return 'Add driver info for ' + missingDrivers.map(function(target) {
      return target.group_label + ' ' + activeRoleLabel(target.role);
    }).join(', ') +
      ' before previewing the active crossover.';
  }
  return 'Add crossover points before previewing the active crossover.';
}
function driverResearchPromptReady(topology) {
  if (!topology || !outputGroups(topology).length ||
      outputTopology.dirty || outputTopology.saving) return false;
  return driverResearchTargets(topology).every(function(target) {
    if (!targetModel(target, topology)) return false;
    if (target.role === 'tweeter') return !!target.driver_style;
    return !!driverSetting(target.target_id).enclosure_kind;
  });
}
function invalidateDriverResearchBinding() {
  driverResearch.researchRequest = null;
  driverResearch.promptCopy.copied = false;
  driverResearch.promptCopy.selected = false;
}

function setManualCrossoverField(pairKey, field, value) {
  if (!driverResearch.settings.crossovers[pairKey]) {
    driverResearch.settings.crossovers[pairKey] = {};
  }
  driverResearch.settings.crossovers[pairKey][field] = value;
  driverResearch.error = '';
  driverResearch.dirty = true;
  invalidateDriverResearchBinding();
}

// A delay entered without picking which driver it applies to would silently
// mis-shape the saved candidate (manualSettingsPayload omits both delay_ms
// and delay_target_role rather than guess). Block the save client-side with
// a specific hint instead of discarding the entered value.
function manualCrossoverDelayValidationError(topology) {
  var offending = activeCrossoverPairs(topology).filter(function(pair) {
    var setting = crossoverSetting(pair);
    if (manualNumberValue(setting.delay_ms) == null) return false;
    var target = String(setting.delay_target_role || '').trim();
    return target !== pair[0] && target !== pair[1];
  });
  if (!offending.length) return '';
  var pair = offending[0];
  return 'Pick which driver is delayed for ' +
    humanRole(pair[0]) + ' / ' + humanRole(pair[1]) + ' before saving.';
}
// The pickers only ever offer what the compiler builds, so an operator
// cannot author a refused crossover. A value can still arrive from outside
// the pickers — a draft saved before the vocabulary narrowed, or an imported
// research packet — and design_draft.py refuses that at the door. Name it
// here, with the pair and the offer, instead of letting the operator meet it
// as a server error or (before entry-time validation) as a staging blocker
// three screens later. Only pairs that will actually be saved are checked:
// manualSettingsPayload omits a pair with no frequency.
function manualCrossoverVocabularyValidationError(topology) {
  // Layout first: a passive layout has no crossover to author, so a damaged
  // island must not block its save over a vocabulary it never uses.
  var pairs = activeCrossoverPairs(topology);
  if (!pairs.length) return '';
  if (!crossoverVocabulary.filterTypes.length || !crossoverVocabulary.slopes.length) {
    return 'The crossover filter and slope options could not be read. Reload this page before saving.';
  }
  var offending = '';
  pairs.forEach(function(pair) {
    if (offending) return;
    var setting = crossoverSetting(pair);
    if (manualNumberValue(setting.frequency_hz) == null) return;
    var name = humanRole(pair[0]) + ' / ' + humanRole(pair[1]);
    var filterType = String(setting.filter_type || crossoverVocabulary.defaultFilterType);
    if (crossoverVocabulary.filterTypes.indexOf(filterType) < 0) {
      offending = 'JTS cannot build a ' + filterType + ' crossover for ' + name +
        '. Pick one of: ' + crossoverVocabulary.filterTypes.join(', ') + '.';
      return;
    }
    var slope = manualNumberValue(setting.slope_db_per_octave);
    if (slope == null) slope = crossoverVocabulary.defaultSlope;
    if (crossoverVocabulary.slopes.indexOf(slope) < 0) {
      offending = 'JTS cannot build a ' + String(slope) + ' dB/oct crossover for ' +
        name + '. Pick one of: ' + crossoverVocabulary.slopes.join(', ') + ' dB/oct.';
    }
  });
  return offending;
}
function safetyBandFromSetting(setting, prefix) {
  var low = manualNumberValue(setting[prefix + '_min_hz']);
  var high = manualNumberValue(setting[prefix + '_max_hz']);
  return low != null && high != null ? [low, high] : null;
}
function protectionFiltersFromSetting(setting) {
  return ['highpass', 'lowpass'].map(function(kind) {
    var cutoff = manualNumberValue(setting['required_' + kind + '_cutoff_hz']);
    var slope = manualNumberValue(
      setting['required_' + kind + '_min_slope_db_per_octave']
    );
    if (cutoff == null && slope == null) return null;
    return {
      kind: kind,
      cutoff_hz: cutoff,
      minimum_slope_db_per_octave: slope,
      family_or_equivalent:
        setting['required_' + kind + '_family_or_equivalent'] ||
        'equivalent_or_steeper'
    };
  }).filter(Boolean);
}
function cabinetFromSetting(setting) {
  var out = {};
  if ((setting.enclosure_kind || '').trim()) {
    out.enclosure_kind = setting.enclosure_kind;
  }
  [
    'radiator_count',
    'effective_radiating_diameter_mm',
    'baffle_width_mm'
  ].forEach(function(field) {
    var value = manualNumberValue(setting[field]);
    if (value != null) out[field] = value;
  });
  return out;
}
// #1665: mirrors cabinetFromSetting above, packing the flat pad_* operator
// inputs into the nested shape jasper.active_speaker.driver_pad.normalise_pad
// expects. Unlike cabinet, 'none' is omitted rather than always sent -- 'no
// pad' and 'field never touched' are the same fact server-side (normalise_pad
// returns None for either), so there is no default worth stating explicitly.
// Only the fields the chosen kind actually uses are packed: attenuation_db is
// NEVER sent for l_pad/series_resistor (it is server-derived, not an input --
// see applyDriverSafetyToSetting, which never writes it back into
// pad_attenuation_db for those kinds either).
function padFromSetting(setting) {
  var kind = setting.pad_kind || 'none';
  if (kind === 'none') return null;
  var out = {kind: kind};
  if (kind === 'direct_db') {
    var db = manualNumberValue(setting.pad_attenuation_db);
    if (db != null) out.attenuation_db = db;
    return out;
  }
  var series = manualNumberValue(setting.pad_series_ohm);
  if (series != null) out.series_ohm = series;
  if (kind === 'l_pad') {
    var shunt = manualNumberValue(setting.pad_shunt_ohm);
    if (shunt != null) out.shunt_ohm = shunt;
  }
  return out;
}
function levelDurationLimitsFromSetting(setting) {
  var out = {};
  [
    'max_effective_peak_dbfs',
    'max_sweep_duration_s',
    'max_repeat_count',
    'minimum_cooldown_s'
  ].forEach(function(field) {
    var value = manualNumberValue(setting[field]);
    if (value != null) out[field] = value;
  });
  return out;
}

// Mirror jasper/active_speaker/crossover_preview.py:_CONFIDENCE_RANK so the
// form selects the same candidate the preview will.
var CANDIDATE_CONFIDENCE_RANK = {high: 3, medium: 2, low: 1, unknown: 0};
function candidateConfidenceRank(candidate) {
  return CANDIDATE_CONFIDENCE_RANK[
    String((candidate && candidate.confidence) || 'unknown')
  ] || 0;
}
function proposeSensitivityTrims(driversByRole) {
  // Propose a starting level trim from the sensitivity gap so a hotter
  // compression/horn driver is never left at full level relative to the
  // woofer. The operator reviews/confirms the value; the server enforces the
  // same fail-safe (baseline_profile.py:_derive_corrections). The pure
  // sensitivity→trim math lives in sensitivityTrimsFromGap (parity-pinned to
  // that Python source); here we only collect the inputs and apply the result
  // to empty fields — never clobbering an operator/research-supplied trim.
  var sensitivities = {};
  Object.keys(driversByRole).forEach(function(role) {
    var sens = manualNumberValue(driversByRole[role].sensitivity_db_2v83_1m);
    if (sens != null) sensitivities[role] = sens;  // reference = min over ALL
  });
  var trims = sensitivityTrimsFromGap(sensitivities);
  Object.keys(trims).forEach(function(role) {
    var targetId = driversByRole[role] && driversByRole[role]._target_id;
    if (!targetId) return;
    var setting = driverSetting(targetId);
    if (manualNumberValue(setting.gain_offset_db) != null) return;  // keep explicit
    setting.gain_offset_db = trims[role];
    setting.gain_offset_db_provenance = 'sensitivity_estimate';
  });
}
function applySafetyBandToSetting(setting, prefix, band) {
  if (!Array.isArray(band) || band.length !== 2) return;
  setting[prefix + '_min_hz'] = band[0];
  setting[prefix + '_max_hz'] = band[1];
}

function driverResearchPrompt(topology) {
  return driverResearchPromptReady(topology)
    ? 'Copy prepares a versioned prompt bound to the current speaker outputs, components, and build notes.'
    : 'Add every component model and choose its enclosure or tweeter type before preparing the target-bound research prompt.';
}
function summarizeDriverResearchPayload(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error('Driver research must be a JSON object.');
  }
  if (payload.kind !== 'jts_active_crossover_driver_research') {
    throw new Error('Driver research kind must be jts_active_crossover_driver_research.');
  }
  var schemaVersion = Number(payload.artifact_schema_version);
  if (schemaVersion !== 1 && schemaVersion !== 2) {
    throw new Error('Driver research artifact_schema_version must be 1 or 2.');
  }
  var drivers = Array.isArray(payload.drivers) ? payload.drivers : [];
  var candidates = Array.isArray(payload.crossover_candidates) ? payload.crossover_candidates : [];
  if (!drivers.length) throw new Error('Driver research must include at least one driver.');
  drivers.forEach(function(driver, index) {
    if (!driver || driver.notes == null || driver.notes === '') return;
    var role = driver.role || 'driver ' + (index + 1);
    if (typeof driver.notes !== 'string') {
      throw new Error('Driver research notes for ' + role + ' must be a string.');
    }
    var normalized = driver.notes.trim().split(/\s+/).filter(Boolean).join(' ');
    if (normalized.length > DRIVER_RESEARCH_NOTE_MAX_CHARS) {
      throw new Error(
        'Driver research notes for ' + role + ' must be <= ' +
        DRIVER_RESEARCH_NOTE_MAX_CHARS + ' chars.'
      );
    }
  });
  // A protection filter with a null cutoff or slope is the one research
  // answer this flow cannot store: applyDriverSafetyToSetting would write
  // the halves it has, and protectionFiltersFromSetting then drops the whole
  // requirement out of the POST with nothing on screen to say so (#2186).
  // Refuse it here instead -- parseDriverResearchImport surfaces this message
  // and leaves the paste box intact, so the operator can act on it.
  // Mirrors the server twin (_positive_float + the both-present check in
  // driver_safety._normalise_protection_filters) rather than merely testing
  // for null: '' and 0 are refused there too. Deliberately NOT tighter than
  // the server -- a numeric STRING is accepted by float() server-side, so it
  // is accepted here, keeping this guard a subset of what the server refuses.
  function storableFilterNumber(value) {
    if (value === null || value === undefined || value === '') return false;
    var parsed = Number(value);
    return Number.isFinite(parsed) && parsed > 0;
  }
  drivers.forEach(function(driver, index) {
    if (!driver || !Array.isArray(driver.required_protection_filters)) return;
    var role = driver.role || 'driver ' + (index + 1);
    driver.required_protection_filters.forEach(function(filter) {
      if (!filter || typeof filter !== 'object') return;
      if (storableFilterNumber(filter.cutoff_hz) &&
          storableFilterNumber(filter.minimum_slope_db_per_octave)) return;
      throw new Error(
        'Driver research declares a ' + (filter.kind || 'protection') +
        ' filter for ' + role + ' without both a cutoff and a minimum slope. ' +
        'A required filter whose numbers are unpublished takes a conservative ' +
        'estimate, not null — ask the assistant again, or type the two numbers ' +
        'under Advanced.'
      );
    });
  });
  if (schemaVersion === 2 && !/^[0-9a-f]{64}$/.test(String(payload.request_fingerprint || ''))) {
    throw new Error('Version 2 driver research must echo the request fingerprint.');
  }
  return {
    schemaVersion: schemaVersion,
    driverCount: drivers.length,
    candidateCount: candidates.length,
    roles: drivers.map(function(driver) { return driver.role || 'unknown'; })
      .filter(function(role, index, arr) { return arr.indexOf(role) === index; }),
    unknownCount: drivers.reduce(function(count, driver) {
      return count + (Array.isArray(driver.unknowns) ? driver.unknowns.length : 0);
    }, 0),
    provenanceFieldCount: drivers.reduce(function(count, driver) {
      return count + Object.keys(driver.field_provenance || {}).length;
    }, 0),
    warnings: candidates.reduce(function(out, candidate) {
      return out.concat(Array.isArray(candidate.warnings) ? candidate.warnings : []);
    }, []).slice(0, 4)
  };
}

function crossoverPreviewReadyForProtectedStaging(payload) {
  payload = payload || {};
  var permissions = payload.permissions || {};
  return payload.kind === 'jts_active_speaker_crossover_preview' &&
    payload.status === 'ready_for_protected_staging' &&
    permissions.may_prepare_protected_startup_config === true;
}
function driverResearchStepSatisfied() {
  var draftPayload = driverResearch.designDraft || {};
  var savedStatus = draftPayload.status || '';
  return savedStatus && savedStatus !== 'not_saved' && savedStatus !== 'unreadable' &&
    !driverResearch.dirty;
}
function driverResearchFlowComplete(topology) {
  if (!activeCommissionGroup(topology)) return driverResearchStepSatisfied();
  return driverResearchStepSatisfied() &&
    crossoverPreviewReadyForProtectedStaging(crossoverPreview.payload);
}

function ingestCrossoverPreview(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return;
  crossoverPreview.payload = payload;
  crossoverPreview.preparing = false;
  crossoverPreview.error = '';
}

// Issue #2191. An 'incomplete' safety profile has two very different causes
// and only one of them is a blank field: a band-relationship issue leaves
// every declared value present, so "add the missing limits" sends the
// operator hunting for an empty box that does not exist. These are the
// relationship and policy codes _target_issues (driver_safety.py) can emit
// with nothing missing; every other code it emits ends in `_missing`.
var SAFETY_RELATIONSHIP_TEXT = {
  measurement_band_outside_hard_band:
    'measurement band reaches outside its hard excitation band',
  highpass_cutoff_outside_hard_band:
    'high-pass cutoff sits outside its hard excitation band',
  lowpass_cutoff_outside_hard_band:
    'low-pass cutoff sits outside its hard excitation band'
  // `low_limit_implausible_for_style` used to sit here. Since #2874 an
  // implausible SAVED low limit is not a refusal at all — it is a warning
  // the server renders itself, because its copy names numbers (the value,
  // the band it missed, the class anchor) that a code-to-phrase map here
  // cannot carry. renderDriverSafetyWarnings in driver-fields.js shows that
  // server text.
  //
  // `max_effective_peak_above_code_policy` used to sit here too. The
  // 2026-08-23 ruling struck that refusal: a declared level limit is a
  // published or operator figure and a class default may not overrule it,
  // so no server produces the code any more.
};
// Reason codes are `<role>:<code>` (a few are bare). Server text, so read it
// as data: only codes this page knows how to phrase produce a sentence.
function driverSafetyConflicts(reasons) {
  var out = [];
  (reasons || []).forEach(function(raw) {
    var parts = String(raw).split(':');
    var code = parts[parts.length - 1];
    if (!Object.prototype.hasOwnProperty.call(SAFETY_RELATIONSHIP_TEXT, code)) {
      return;
    }
    var text = SAFETY_RELATIONSHIP_TEXT[code];
    var line = parts.length > 1 ? 'the ' + parts[0] + "'s " + text : text;
    if (out.indexOf(line) < 0) out.push(line);
  });
  return out;
}
function driverSafetyHasMissing(reasons) {
  return (reasons || []).some(function(raw) {
    return /_missing$/.test(String(raw));
  });
}
// #2603. A profile confirmed before a driver's low limit had one declared
// owner can no longer match its own derivation, so it evaluates 'malformed'
// under this name rather than the generic schema-invalid one.
var SAFETY_LOW_LIMIT_STALE = 'driver_safety_profile_low_limit_stale';
function driverSafetyLowLimitStale(reasons) {
  return (reasons || []).indexOf(SAFETY_LOW_LIMIT_STALE) >= 0;
}
// #2870. A profile saved before JTS retired a field is not corrupt — it just
// names something this build no longer speaks, and one save rebuilds it.
// Named separately so the copy can say that, instead of the generic "JTS
// could not read these limits", which reads as damage and names no remedy.
var SAFETY_RETIRED_FIELD = 'driver_safety_profile_retired_field';
function driverSafetyRetiredField(reasons) {
  return (reasons || []).indexOf(SAFETY_RETIRED_FIELD) >= 0;
}

function driverEvidenceForTarget(targetId) {
  var profileTargets = driverResearch.safetyDirty ? [] :
    (((driverResearch.designDraft || {}).driver_safety_profile || {}).targets || []);
  var importedEvidence = driverResearch.importedPayload;
  var importedEvidenceCurrent = importedEvidence &&
    (Number(importedEvidence.artifact_schema_version || 1) !== 2 ||
      !!driverResearch.researchRequest);
  var importedTargets = (importedEvidenceCurrent &&
    Array.isArray(importedEvidence.drivers))
    ? importedEvidence.drivers : [];
  return driverResearch.editedDriverTargets[targetId] ? {} :
    (profileTargets.find(function(item) {
      return item && item.target_id === targetId;
    }) || importedTargets.find(function(item) {
      return item && item.target_id === targetId;
    }) || {});
}

// --- "Here's what we got — here's what we're running with" (#2195) --------
//
// The research assistant is now asked for its BEST number rather than a
// timid one, declared as published-or-estimated with one citation. That
// trade only works if the household can arbitrate, so every value JTS
// consumed is echoed back with its badge and its source before anything is
// confirmed. This panel replaced a bare tally ("2 of these limits came from
// the research reply as estimates"), which told the operator how many
// numbers to distrust without saying which.
//
// Two rules keep it honest:
//   * The badge is DERIVED from confidence, which stays the single stored
//     writer of published-vs-estimated (there is no `state` key on the
//     wire — see driver_safety._normalise_field_provenance).
//   * Only "high"/"medium" assert a published figure. Silence is not a
//     publication claim, so a value with no provenance entry at all reads
//     "estimated" rather than being quietly promoted.
function driverProvenanceState(entry) {
  var confidence = String((entry && entry.confidence) || '');
  return (confidence === 'high' || confidence === 'medium')
    ? 'confirmed' : 'estimated';
}

// Code-owned protection bounds for one target, straight from the server
// (design draft `driver_protection_policy_view`, re-derived on every load
// that knows the topology — which the /sound/ endpoint always does). The
// page deliberately keeps NO copy of max_auto_level_dbfs: it is policy, and a
// second copy here would drift. (The view also carried an absolute
// measurement ceiling until 2026-08-20, on the same no-second-copy footing;
// that constant is retired and the field is gone from the wire.)
function driverProtectionPolicy() {
  var policy = (driverResearch.designDraft || {}).driver_protection_policy_view;
  return (policy && typeof policy === 'object') ? policy : null;
}
function driverProtectionPolicyForTarget(targetId) {
  var policy = driverProtectionPolicy();
  var targets = (policy && Array.isArray(policy.targets)) ? policy.targets : [];
  return targets.filter(function(item) {
    return item && item.target_id === targetId;
  })[0] || null;
}
function echoBandText(setting, prefix) {
  var low = manualNumberValue(setting[prefix + '_min_hz']);
  var high = manualNumberValue(setting[prefix + '_max_hz']);
  if (low == null || high == null) return '';
  return fmtFreq(low) + ' to ' + fmtFreq(high);
}
function echoFilterText(setting) {
  return ['highpass', 'lowpass'].map(function(kind) {
    var cutoff = manualNumberValue(setting['required_' + kind + '_cutoff_hz']);
    var slope = manualNumberValue(
      setting['required_' + kind + '_min_slope_db_per_octave']
    );
    if (cutoff == null) return '';
    return (kind === 'highpass' ? 'high-pass ' : 'low-pass ') + fmtFreq(cutoff) +
      (slope == null ? '' : ', ' + slope + ' dB/oct or steeper');
  }).filter(Boolean).join('; ');
}
// Cabinet GEOMETRY only. enclosure_kind is an operator-declared installation
// choice the research ask is forbidden to infer, and the import boundary
// strips it out of a reply's cabinet before applying it -- so it is not one
// of "the values the research reply gave us" and does not belong in a panel
// that says it is.
function echoCabinetText(setting) {
  var parts = [];
  var count = manualNumberValue(setting.radiator_count);
  var diameter = manualNumberValue(setting.effective_radiating_diameter_mm);
  var baffle = manualNumberValue(setting.baffle_width_mm);
  if (count != null) parts.push(count + (count === 1 ? ' radiator' : ' radiators'));
  if (diameter != null) parts.push(diameter + ' mm effective diameter');
  if (baffle != null) parts.push(baffle + ' mm baffle');
  return parts.join(', ');
}
function echoLevelText(setting) {
  var parts = [];
  var peak = manualNumberValue(setting.max_effective_peak_dbfs);
  var sweep = manualNumberValue(setting.max_sweep_duration_s);
  var repeats = manualNumberValue(setting.max_repeat_count);
  var cooldown = manualNumberValue(setting.minimum_cooldown_s);
  if (peak != null) parts.push(fmtDb(peak) + ' dBFS peak');
  if (sweep != null) parts.push('sweeps up to ' + sweep + ' s');
  if (repeats != null) parts.push(repeats + ' repeats');
  if (cooldown != null) parts.push(cooldown + ' s cooldown');
  return parts.join(', ');
}
// Exactly the union of two server-owned sets, in the order they matter to a
// household reading the panel:
//   * the five keys the research ask requires a source for
//     (driver_safety_prompt._PROMPT_PROVENANCE_KEYS), and
//   * the five fields _profile_core FREEZES into the confirmed safety profile
//     (its `safety_field_names`).
// Seven keys, because three overlap. The panel headline states that union as
// its completeness claim, so the two must not drift apart: the tripwire is
// tests/test_sound_profile_echo_back_contract.py, which also pins every key
// here inside _V2_RESEARCH_COMPARABLE_FIELDS.
//
// Each entry reads the value JTS is actually RUNNING WITH out of the working
// setting, not the number in the reply — those are the same until the
// operator edits one, and the setting is what gets frozen.
function driverEchoBackFields() {
  return [
    {
      key: 'hard_excitation_band_hz',
      label: 'Never test outside',
      read: function(setting) { return echoBandText(setting, 'hard_excitation'); }
    },
    {
      // #2603: this row used to echo do_not_test_below_hz, which is retired.
      // What replaced it is the low limit's OWNER -- the manufacturer's
      // minimum recommended crossover frequency -- and the slope condition
      // the manufacturer attaches to it. Both render, because the operator
      // entered both and a half-echoed declaration is exactly the round-trip
      // gap this panel exists to close. The commissioning margin JTS derives
      // from them is deliberately NOT shown: the panel echoes what the reply
      // said, never what the server computed on top of it.
      key: 'recommended_highpass_hz',
      label: 'Minimum crossover',
      read: function(setting) {
        var value = manualNumberValue(setting.recommended_highpass_hz);
        if (value == null) return '';
        var slope = manualNumberValue(
          setting.recommended_highpass_slope_db_per_octave
        );
        return fmtFreq(value) +
          (slope == null ? '' : ', ' + slope + ' dB/oct or steeper');
      }
    },
    {
      key: 'required_protection_filters',
      label: 'Protection filter',
      read: echoFilterText
    },
    {
      key: 'measurement_band_hz',
      label: 'Measure inside',
      read: function(setting) { return echoBandText(setting, 'measurement'); }
    },
    {
      key: 'level_duration_limits',
      label: 'Test level and duration',
      read: echoLevelText
    },
    {
      key: 'sensitivity_db_2v83_1m',
      label: 'Sensitivity',
      read: function(setting) {
        var value = manualNumberValue(setting.sensitivity_db_2v83_1m);
        return value == null ? '' : fmtDb(value) + ' dB';
      }
    },
    {
      key: 'cabinet',
      label: 'Cabinet geometry',
      read: echoCabinetText
    }
  ];
}
// The delegation, disclosed (#2192, folded into #2195). A high-frequency
// target that declares NO level limit is read by
// resolve_driver_excitation_ceilings as "no driver-specific level intent",
// and the measurement level is then DERIVED. Saying nothing here would leave
// the household with a level row that never mentions the loudest fact about
// it.
//
// Absence is the ordinary shape since the 2026-08-23 ruling made the field a
// published-fact-or-omit key. A stored profile written before that carries
// the class ceiling itself, which said the same thing, so both land here.
//
// The sentence named an absolute dBFS bound until 2026-08-20, when the
// provisional -35 dBFS constant behind it was retired: the bound is now this
// driver's own sensitivity derivation against its woofer's limit, which the
// server's topology-only policy view cannot compute and so no longer sends.
// Naming WHAT sets the level is the honest replacement for naming a global
// number that no longer exists; no number is quoted here, so there is none
// to fabricate or to drift.
function driverEchoDelegationText(targetId, setting) {
  var policy = driverProtectionPolicyForTarget(targetId);
  if (!policy || policy.role_class !== 'high_frequency') return '';
  var ceiling = manualNumberValue(policy.max_auto_level_dbfs);
  var peak = manualNumberValue(setting.max_effective_peak_dbfs);
  if (peak != null && (ceiling == null || peak !== ceiling)) return '';
  return 'Test level here is left to JTS. It picks the level once a ' +
    'protective high-pass is in place, from this driver’s declared ' +
    'sensitivity against the low-frequency driver’s own limit.';
}

function driverSafetyReviewHint(state) {
  if (state.status === 'incomplete') {
    var conflicts = driverSafetyConflicts(state.reasons);
    if (!conflicts.length) {
      return 'Some safety limits are still missing. Add them under Advanced, ' +
        'then save.';
    }
    return (driverSafetyHasMissing(state.reasons) ?
      'Some safety limits are still missing, and some do not line up: ' :
      'Nothing is missing, but some safety limits do not line up: ') +
      joinListText(conflicts, {two: ' and ', final: ', and '}) +
      '. Fix them under Advanced, then save.';
  }
  // #2603. Named before the generic 'stale' text, because the cause is
  // specific and so is the fix: this profile was written when a driver's
  // minimum crossover could be declared in two places, and the two no longer
  // agree. Saving rebuilds it — unless deriving the one number pushed
  // something else out of range, which the server tells us.
  if (driverSafetyLowLimitStale(state.reasons)) {
    var stale = driverSafetyConflicts(state.reasons);
    if (!stale.length) {
      return 'These limits were saved before JTS kept one declared ' +
        'minimum crossover per driver. Review the visible values, then ' +
        'save them again.';
    }
    return 'These limits were saved before JTS kept one declared ' +
      'minimum crossover per driver, and rebuilding them needs one fix ' +
      'first: ' + joinListText(stale, {two: ' and ', final: ', and '}) +
      '. Under Advanced, either enter the minimum crossover the datasheet ' +
      'publishes for that driver, or move the range that no longer fits.';
  }
  // #2870. Before the generic unreadable copy, for the same reason the
  // stale-low-limit case sits before the generic 'stale' one: the cause is
  // specific and so is the fix. These limits name a field this build no
  // longer has, so one save rewrites them in the shape it does.
  if (driverSafetyRetiredField(state.reasons)) {
    return 'These limits name a setting JTS no longer uses. Nothing is ' +
      'wrong with your speaker — review the visible values, then save them ' +
      'again to rebuild them.';
  }
  if (state.status === 'stale') {
    return 'The outputs changed since these limits were saved. Review the ' +
      'visible values, then save them again.';
  }
  return 'JTS could not read these limits. Review the visible values, then ' +
    'save them again.';
}

// Non-blocking disclosures the SERVER phrased (#2874). Unlike a blocking
// reason — a bare code this page turns into a sentence via
// SAFETY_RELATIONSHIP_TEXT — a warning's copy names the household's own
// numbers, so the server sends the sentence and this renders it. Shown
// whatever the profile status is: the whole point of a warning is that the
// declaration SAVED and is in use, which is exactly when the review callout
// (renderDriverSafetyReviewCallout in main.js) stays quiet.
function driverSafetyWarnings() {
  var profile = (driverResearch.designDraft || {}).driver_safety_profile || {};
  var issues = Array.isArray(profile.issues) ? profile.issues : [];
  return issues.filter(function(issue) {
    return issue && issue.severity === 'warning' && issue.message;
  });
}

function previewStatusClass(value) {
  if (value === 'preview ready' || value === 'ready_for_protected_staging') {
    return ' status-pill--ready';
  }
  if (value === 'stale' || value === 'unreadable') return ' status-pill--blocked';
  return '';
}
function crossoverPreviewReadyCount(payload) {
  var summary = payload && payload.summary || {};
  var ready = Number(summary.ready_crossover_count || 0);
  if (ready > 0) return ready;
  var count = 0;
  (Array.isArray(payload && payload.groups) ? payload.groups : []).forEach(function(group) {
    (Array.isArray(group.crossovers) ? group.crossovers : []).forEach(function(crossover) {
      if (crossover.status === 'ready_for_review') count += 1;
    });
  });
  return count;
}
function crossoverPreviewDisplayStatus(payload) {
  payload = payload || {};
  var raw = payload.status || 'not_prepared';
  if (crossoverPreviewReadyCount(payload) > 0) return 'preview ready';
  if (raw === 'ready_for_protected_staging') return 'preview ready';
  if (raw === 'blocked') return 'not ready yet';
  if (raw === 'stale') return 'needs refresh';
  if (raw === 'not_applicable') return 'not needed';
  return 'not prepared';
}
function crossoverPreviewReviewIssues(issues) {
  return (Array.isArray(issues) ? issues : []).filter(function(issue) {
    return issue && issue.severity === 'warning';
  });
}

// Candidate echo of the working crossover's polarity/delay, kept as a
// read-only annotation on the (working) preview row — never merged with
// the applied profile's corrections block, which is a separate state
// (spec "One model, three states": "must never merge values from those
// states implicitly").
function crossoverAlignmentDetailText(crossover, roles) {
  var parts = [];
  if (crossover.lower_polarity === 'inverted' && roles[0]) {
    parts.push(humanRole(roles[0]) + ' inverted');
  }
  if (crossover.upper_polarity === 'inverted' && roles[1]) {
    parts.push(humanRole(roles[1]) + ' inverted');
  }
  if (crossover.delay_ms != null && crossover.delay_target_role) {
    parts.push(humanRole(crossover.delay_target_role) + ' delayed ' + String(crossover.delay_ms) + ' ms');
  }
  return parts.join(', ');
}

// The research prompt asks for one ```json fenced block, and a chat UI's copy
// button copies the block's contents — but people also paste the whole reply,
// fence markers and surrounding prose included. A raw JSON.parse on that hands
// back a V8 message about a character offset, which tells an operator nothing.
// Recover the object first: prefer the first fenced block, else the widest
// {...} span, and only then parse. Both paste entry points go through here so
// the two cannot drift.
function extractDriverResearchJson(text) {
  var raw = String(text == null ? '' : text).trim();
  // Strictly additive: the untouched paste is tried first, so anything that
  // parses today still parses to exactly the same value. Only a paste that
  // already fails reaches the recovery candidates.
  var candidates = [raw];
  var fenced = raw.match(/```[^\S\n]*[A-Za-z0-9_-]*[^\S\n]*\n([\s\S]*?)```/);
  if (fenced) candidates.push(fenced[1].trim());
  var open = raw.indexOf('{');
  var close = raw.lastIndexOf('}');
  if (open !== -1 && close > open) candidates.push(raw.slice(open, close + 1));
  // Report the LAST candidate's parser message: it comes from the most
  // recovered text, so it names the junk inside the object rather than
  // complaining about the fence the operator was told to paste.
  var lastError = null;
  for (var i = 0; i < candidates.length; i++) {
    try {
      return JSON.parse(candidates[i]);
    } catch (e) {
      lastError = e;
    }
  }
  throw new Error(
    "Couldn't read that as JSON — paste the complete code block the assistant returned. (" +
    lastError.message + ')'
  );
}

export {
  applySafetyBandToSetting,
  cabinetFromSetting,
  candidateConfidenceRank,
  candidateFrequency,
  crossoverAlignmentDetailText,
  crossoverPreviewDisplayStatus,
  crossoverPreviewReadyCount,
  crossoverPreviewReadyForProtectedStaging,
  crossoverPreviewReviewIssues,
  currentCrossoverFrequency,
  driverClassHasRadiatingDiameter,
  driverClasses,
  driverEchoBackFields,
  driverEchoDelegationText,
  driverEvidenceForTarget,
  driverProvenanceState,
  driverResearchFlowComplete,
  driverResearchHasPreviewInputs,
  driverResearchMissingPreviewMessage,
  driverResearchPrompt,
  driverResearchPromptReady,
  driverResearchRoleLabel,
  driverResearchStepSatisfied,
  driverResearchTargets,
  driverSafetyConflicts,
  driverSafetyNoteRoles,
  driverSafetyReviewHint,
  driverSafetyWarnings,
  driverStyleLabel,
  extractDriverResearchJson,
  hfDriverStyleEntry,
  hfDriverStyles,
  ingestCrossoverPreview,
  invalidateDriverResearchBinding,
  kaBeamingOnsetHz,
  levelDurationLimitsFromSetting,
  manualCrossoverDelayValidationError,
  manualCrossoverVocabularyValidationError,
  padFromSetting,
  padKinds,
  previewStatusClass,
  proposeSensitivityTrims,
  protectionFiltersFromSetting,
  safetyBandFromSetting,
  setManualCrossoverField,
  summarizeDriverResearchPayload,
  targetModel,
  workingSetupSummary,
};
