// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Reads jts-sub-crossover-bounds on first use; importable without a DOM.
// Missing bounds fail the caller.
import { readJsonIsland } from '../../shared/js/dom.js';

export function outputStatusClass(statusValue) {
  if (statusValue === 'valid' ||
      statusValue === 'ready' || statusValue === 'preview ready') {
    return ' status-pill--ready';
  }
  if (statusValue === 'blocked') return ' status-pill--blocked';
  return ' status-pill--planned';
}

export function humanRole(role) {
  return {
    full_range: 'Full range',
    woofer: 'Woofer',
    mid: 'Mid',
    tweeter: 'Tweeter',
    subwoofer: 'Subwoofer'
  }[role] || role || 'Channel';
}

var SENSITIVITY_TRIM_EPS_DB = 0.05;   // _SENSITIVITY_TRIM_EPS_DB
var MAX_DRIVER_ATTENUATION_DB = -60.0;  // _MAX_ATTENUATION_DB

// Round to one decimal place. Driver sensitivities are datasheet values quoted
// to one decimal, so the gap between two of them is already a multiple of 0.1 and
// this round is effectively identity (it just clears IEEE-754 dust like
// -3.9999999999999996 -> -4.0). On that realistic input domain Math.round matches
// Python's round(x, 1) exactly (verified over 20k 1-decimal pairs); the half-up
// vs round-half-to-even distinction only surfaces for contrived sub-decimal
// sensitivities that don't occur on real spec sheets.
function roundTenths(x) {
  var rounded = Math.round(x * 10);
  return (rounded === 0 ? 0 : rounded) / 10;  // normalize -0 to 0 for clean JSON compares
}

export function sensitivityTrimsFromGap(sensitivities) {
  var roles = [];
  var values = [];
  Object.keys(sensitivities || {}).forEach(function(role) {
    var sens = Number(sensitivities[role]);
    if (Number.isFinite(sens)) { roles.push(role); values.push(sens); }
  });
  var trims = {};
  if (roles.length < 2) return trims;
  var reference = Math.min.apply(null, values);
  roles.forEach(function(role, i) {
    var trim = reference - values[i];  // <= 0 by construction
    if (trim >= -SENSITIVITY_TRIM_EPS_DB) return;  // reference + ties stay at unity
    trims[role] = Math.max(roundTenths(trim), MAX_DRIVER_ATTENUATION_DB);
  });
  return trims;
}

export function activeSpeakerStepState(step, ctx) {
  ctx = ctx || {};
  if (!ctx.hasLayout || ctx.dirty || ctx.hardwareMatchesSaved === false) {
    return step === 'layout' ? 'active' : 'todo';
  }
  if (!ctx.driverResearchSatisfied) {
    return step === 'layout' ? 'done' : step === 'research' ? 'active' : 'todo';
  }
  var item = (ctx.steps || []).find(function(item) { return item.id === step; });
  return item ? item.status : step === 'research' ? 'active' : 'todo';
}

export function defaultActiveSpeakerStep(ctx) {
  ctx = ctx || {};
  if (!ctx.hasLayout || ctx.dirty || ctx.hardwareMatchesSaved === false) return 'layout';
  if (!ctx.driverResearchSatisfied) return 'research';
  return ctx.currentStep || 'research';
}

export function activeCommissionGroup(topology) {
  // The single active (2/3-way) speaker group commissioning targets, if any.
  var groups = topology && Array.isArray(topology.speaker_groups) ?
    topology.speaker_groups : [];
  for (var i = 0; i < groups.length; i += 1) {
    var mode = groups[i] && groups[i].mode;
    if (mode === 'active_2_way' || mode === 'active_3_way') return groups[i];
  }
  return null;
}

export function nextActionAct(action) {
  action = action || {};
  var program = action.program || 'speaker';
  var run = {act: '', step: 'experiment', program: program,
    command: 'sudo /opt/jasper/.venv/bin/jasper-round run --program ' + program};
  return {
    declare_speaker: {act: 'open-output-layout', step: 'layout'},
    save_driver_values: {act: 'save-driver-design', step: 'research'},
    run_speaker_program: run,
    apply_candidate: {act: 'save-apply-baseline-profile', step: 'profile'},
    run_program: run,
    copy_prompt: {act: 'copy-tuning-handoff', step: '', program: program}
  }[action.id] || {act: '', step: ''};
}

let cachedSubCrossoverBounds;
export function subCrossoverBounds() {
  const bounds = cachedSubCrossoverBounds ?? readJsonIsland('jts-sub-crossover-bounds', null);
  if (bounds == null) throw new Error('jts-sub-crossover-bounds island missing (served by jasper/web/sound_setup.py:_sound_page_island)');
  return (cachedSubCrossoverBounds = bounds);
}

// The single local-subwoofer group, if one is routed. A local sub adds a DAC
// output lane.
export function localSubwooferGroup(topology) {
  var groups = topology && Array.isArray(topology.speaker_groups) ?
    topology.speaker_groups : [];
  for (var i = 0; i < groups.length; i += 1) {
    var group = groups[i];
    if (group && (group.kind === 'subwoofer' || group.mode === 'subwoofer')) {
      return group;
    }
  }
  return null;
}

// The user-settable bass-management corner for the routed local subwoofer, read
// from the sub channel's crossover_fc_hz (falling back to the shared default when
// unset). Returns DEFAULT when no sub is routed. Pure number — the topology
// validator range-checks it server-side; this only normalizes for display/edit.
export function subwooferCrossoverFcHz(topology) {
  var group = localSubwooferGroup(topology);
  if (!group) return subCrossoverBounds().default_hz;
  var channels = Array.isArray(group.channels) ? group.channels : [];
  for (var i = 0; i < channels.length; i += 1) {
    var channel = channels[i];
    if (channel && channel.role === 'subwoofer') {
      var fc = Number(channel.crossover_fc_hz);
      if (Number.isFinite(fc)) return fc;
      break;
    }
  }
  return subCrossoverBounds().default_hz;
}

// Clamp a user-entered crossover corner into the safe bass-management band. A
// blank/non-numeric value falls back to the default; out-of-range values pin to
// the nearest bound (defense in depth — the server also fail-loud rejects them).
export function clampSubwooferCrossoverFcHz(value) {
  const bounds = subCrossoverBounds();
  // Number('') / Number('   ') coerce to 0 (finite), so reject a blank/whitespace
  // entry explicitly before the finite check — a cleared field means "default",
  // not "0 Hz" (which would otherwise pin to the low bound).
  if (typeof value === 'string' && value.trim() === '') {
    return bounds.default_hz;
  }
  var fc = Number(value);
  if (!Number.isFinite(fc)) return bounds.default_hz;
  return Math.max(bounds.lo_hz, Math.min(bounds.hi_hz, fc));
}

// The system-managed bass-management high-pass the routed local subwoofer
// applies to the mains, surfaced as ONE called-out, non-editable PEQ-style band
// so the household can SEE that a subwoofer high-pass at N Hz is shaping the
// mains. Returns null when no local sub is routed (nothing to show). The band is
// edited via the subwoofer card, never in the PEQ list — it carries no gain
// (Highpass is a GAINLESS type) and reuses the same biquad curve math.
export function subwooferCrossoverBand(topology) {
  if (!localSubwooferGroup(topology)) return null;
  var fc = clampSubwooferCrossoverFcHz(subwooferCrossoverFcHz(topology));
  return {
    type: 'Highpass',
    // Linkwitz-Riley 24 dB/oct is the bass-management default the emitter uses;
    // the drawn curve is illustrative (a 2nd-order RBJ biquad), matching how the
    // PEQ preview approximates higher-order cuts.
    freq_hz: fc,
    gain_db: 0,
    q: 0.707,
    label: 'Subwoofer crossover',
    detail: 'High-pass at ' + Math.round(fc) + ' Hz on the mains (bass goes to the sub)',
    systemManaged: true,
    editedVia: 'the subwoofer card'
  };
}

export function timingStatusLine(view, field) {
  return (((view || {}).timing || {})[field] || '');
}

// Pointer to the L1 level match for the driver-levels card. The level match is
// OPTIONAL — confirming each driver by ear is enough to finish HERE. This page
// cannot record: /sound/ is plain HTTP, so `getUserMedia` is unavailable and no
// recorder exists in this bundle. The measurement lives on the HTTPS
// /sound/speaker/crossover/ page, so this copy is only a pointer to it. BOTH
// halves are load-bearing: the full path (the host alone lands nowhere useful)
// and the destination's own label, which jasper.web.nav's NAV owns.
// Placement geometry is OWNED by jasper/active_speaker/capture_geometry.py and
// rendered by the measurement page for the capture kind in play. Do NOT
// restate a distance or an aim instruction here.
const NEARFIELD_LEVEL_MATCH_GUIDANCE =
  'Run the speaker experiment from jts.local/sound/speaker/crossover, the ' +
  'Active speaker page. Apply the candidate named in its packet to save the ' +
  'measured driver levels, delay and polarity.';

function levelMatchSourceLabel(source) {
  return {
    measured: 'Measured',
    sensitivity: 'Datasheet estimate',
    estimate: 'Suggested estimate',
    research_estimate: 'Research estimate',
    operator_pinned: 'Manual',
    explicit: 'Manual (legacy)',
    none: '—'
  }[source] || source || '—';
}

// Summarise the per-driver level trim from the baseline-profile payload for the
// "Validate and apply" card: each driver's attenuation and where it came from
// (measured phone level-match vs datasheet estimate vs manual), plus whether the
// config is provisional (datasheet estimate in effect, pending a measurement).
// Pure: main.js owns the DOM. The speaker is attenuation-only and safe either
// way; "provisional" is a quality signal, not a safety one.
export function levelMatchSummary(baseline) {
  baseline = baseline || {};
  var corrections = baseline.corrections && typeof baseline.corrections === 'object' ?
    baseline.corrections : {};
  var sources = baseline.corrections_source && typeof baseline.corrections_source === 'object' ?
    baseline.corrections_source : {};
  var rows = [];
  ['woofer', 'mid', 'tweeter'].forEach(function(role) {
    if (!Object.prototype.hasOwnProperty.call(corrections, role)) return;
    var entry = corrections[role] || {};
    var gain = typeof entry.gain_db === 'number' ? entry.gain_db : 0;
    var source = sources[role] || 'none';
    rows.push({
      role: role,
      label: humanRole(role),
      trimDb: gain,
      source: source,
      sourceLabel: levelMatchSourceLabel(source)
    });
  });
  var provisional = !!baseline.provisional;
  var sourceValues = rows.map(function(row) { return row.source; });
  var badge = sourceValues.indexOf('measured') !== -1
    ? 'measured'
    : (sourceValues.indexOf('operator_pinned') !== -1 ||
       sourceValues.indexOf('explicit') !== -1 ? 'manual' : 'estimate');
  return {
    available: rows.length > 0,
    provisional: provisional,
    badge: badge,
    rows: rows,
    note: badge === 'measured' ?
      'Per-driver levels are measured — the quietest driver is the 0 dB reference.' :
      (badge === 'manual' ?
        'These per-driver levels are manually pinned. A safe applied manual crossover is valid for room correction; automatic tuning replaces it only after explicit apply.' :
        'These per-driver levels are safe starting estimates, not acoustic measurements.'),
    guidance: NEARFIELD_LEVEL_MATCH_GUIDANCE
  };
}
