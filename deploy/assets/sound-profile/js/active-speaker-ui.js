// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Pure active-speaker setup helpers for /sound/.
//
// This module deliberately contains no DOM or fetch state. The large sound
// profile module owns rendering and IO; this file owns the product vocabulary
// and step-state policy so the active-crossover flow has one small contract.

export function outputStatusClass(statusValue) {
  if (statusValue === 'verified' || statusValue === 'valid' ||
      statusValue === 'ready' || statusValue === 'preview ready') {
    return ' status-pill--ready';
  }
  if (statusValue === 'blocked') return ' status-pill--blocked';
  return ' status-pill--planned';
}

export function humanMode(modeValue) {
  return {
    full_range_passive: 'Passive/full range',
    active_2_way: 'Active 2-way',
    active_3_way: 'Active 3-way',
    subwoofer: 'Subwoofer'
  }[modeValue] || modeValue || 'Unknown';
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

// Sensitivity → level-trim derivation. PARITY CONTRACT with the Python source
// jasper/active_speaker/baseline_profile.py::_derive_corrections (the
// datasheet_trims block). The /sound/ form pre-fills a starting level trim from
// the driver sensitivity gap (optimistic UI) so a hotter compression/horn driver
// is never left at full level relative to the woofer; the server re-derives the
// same fail-safe authoritatively on save. The two MUST agree, so the pure math
// lives here behind one function and is pinned by scripts/check-sensitivity-trim-parity.mjs
// against tests/fixtures/sensitivity_trim_fixture.json (the same fixture a Python
// test asserts the source matches — the eq-math.js parity model).
export var SENSITIVITY_TRIM_EPS_DB = 0.05;   // _SENSITIVITY_TRIM_EPS_DB
export var MAX_DRIVER_ATTENUATION_DB = -60.0;  // _MAX_ATTENUATION_DB

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

// Given a {role: sensitivity_db} map (only roles with a known datasheet
// sensitivity), return {role: trim_db} attenuating the hotter drivers down to the
// least-sensitive (reference) driver. Mirrors _derive_corrections exactly:
//   - needs >= 2 known sensitivities, else {} (nothing to balance against)
//   - reference = min(sensitivities); trim = reference - sensitivity (<= 0)
//   - the reference driver and ties (trim within EPS of 0) stay at unity (omitted)
//   - each trim is round(_,1) then floored at MAX_DRIVER_ATTENUATION_DB
// Roles the caller wants to exclude (an explicit operator/research gain) must be
// dropped from the input map before calling, matching the server's
// explicit-gain-wins precedence.
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
  var hasLayout = !!ctx.hasLayout;
  var dirty = !!ctx.dirty;
  var hardwareMatchesSaved = ctx.hardwareMatchesSaved !== false;
  var driverChecksComplete = !!(
    ctx.driverChecksComplete || ctx.driverMeasurementsComplete
  );
  if (step === 'layout') return hasLayout && !dirty && hardwareMatchesSaved ? 'done' : 'active';
  if (!hardwareMatchesSaved) return 'todo';
  if (step === 'research') return hasLayout && !dirty ?
    (ctx.driverResearchSatisfied ? 'done' : 'active') : 'todo';
  if (step === 'map') return ctx.outputIdentityComplete ? 'done' :
    (hasLayout && !dirty ? 'active' : 'todo');
  if (step === 'safety') return driverChecksComplete ? 'done' :
    (ctx.outputIdentityComplete ? 'active' : 'todo');
  if (step === 'profile') return ctx.baselineProfileApplied &&
    !ctx.baselineProfileNeedsRevalidation ? 'done' :
    (driverChecksComplete ? 'active' : 'todo');
  return 'todo';
}

export function defaultActiveSpeakerStep(ctx) {
  ctx = ctx || {};
  var driverChecksComplete = !!(
    ctx.driverChecksComplete || ctx.driverMeasurementsComplete
  );
  if (!ctx.hasLayout || ctx.dirty || ctx.hardwareMatchesSaved === false) return 'layout';
  if (!ctx.driverResearchSatisfied) return 'research';
  if (!ctx.outputIdentityComplete) return 'map';
  if (!driverChecksComplete) return 'safety';
  return 'profile';
}

export function outputStepTitle(step) {
  return {
    layout: 'Choose speaker layout',
    research: 'Add driver and crossover values',
    map: 'Confirm outputs',
    safety: 'Test each driver',
    profile: 'Validate and apply'
  }[step] || 'this card';
}

// Single-audio-path commissioning card: derive the card's affordances from the
// /active-speaker/commission-state payload + the active speaker group. Pure so
// the step policy (which buttons are live, what the floor state means) is one
// small testable contract; main.js owns the DOM + fetch.
export function commissionCardState(commission, group, checkedRoles) {
  commission = commission || {};
  var load = commission.commission_load || {};
  var ramp = commission.ramp || {};
  var target = load.target || {};
  var pending = ramp.pending && typeof ramp.pending === 'object' ? ramp.pending : null;
  var roles = activeCommissionRolesForGroup(group);
  var armed = load.status === 'loaded';
  var stale = load.status === 'stale' ||
    (load.runtime_status && load.runtime_status.status === 'stale');
  var awaitingAck = armed && !!pending;
  var confirmed = Array.isArray(checkedRoles) ?
    checkedRoles :
    (Array.isArray(ramp.confirmed_roles) ? ramp.confirmed_roles : []);
  var complete = roles.length > 0 && !pending && roles.every(function(role) {
    return confirmed.indexOf(role) >= 0;
  });
  var loadedRole = target.role || null;
  var loadedRoleConfirmed = armed && loadedRole && confirmed.indexOf(loadedRole) >= 0 && !pending;
  var nextRole = complete ? null : (armed && !loadedRoleConfirmed ?
    loadedRole :
    nextCommissionRole(roles, confirmed));
  return {
    available: !!group,
    groupId: group ? (group.id || '') : '',
    armed: armed,
    stale: stale,
    armedRole: armed ? (target.role || null) : null,
    armedGainDb: armed ? target.audible_gain_db : null,
    startRole: nextRole,
    awaitingAck: awaitingAck,
    pendingRole: pending ? pending.role : null,
    pendingGainDb: pending ? pending.gain_db : null,
    toneFrequencyHz: pending ? pending.frequency_hz : null,
    confirmedRoles: confirmed,
    complete: complete,
    canArm: false,
    canStep: !!group && !!nextRole && !awaitingAck && !complete,
    canAck: awaitingAck,
    canRemute: armed
  };
}

function activeCommissionRolesForGroup(group) {
  var seen = {};
  var order = ['woofer', 'mid', 'tweeter'];
  (group && Array.isArray(group.channels) ? group.channels : []).forEach(function(ch) {
    if (ch && ch.role) seen[ch.role] = true;
  });
  return order.filter(function(role) { return seen[role]; });
}

function nextCommissionRole(roles, confirmed) {
  roles = Array.isArray(roles) ? roles : [];
  confirmed = Array.isArray(confirmed) ? confirmed : [];
  for (var i = 0; i < roles.length; i += 1) {
    if (confirmed.indexOf(roles[i]) < 0) return roles[i];
  }
  return null;
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

// Bass-management crossover corner bounds. These MUST equal
// jasper.active_speaker.profile.DEFAULT_SUB_CROSSOVER_HZ / SUB_CROSSOVER_HZ_LO /
// _HI (and jasper.output_topology's SUB_CROSSOVER_HZ_* mirror). Duplicated here
// only so this DOM-free module stays import-light; the equality is pinned by
// test_sound_setup.py::test_sub_crossover_bounds_match_python.
export var DEFAULT_SUB_CROSSOVER_HZ = 80.0;
export var SUB_CROSSOVER_HZ_LO = 40.0;
export var SUB_CROSSOVER_HZ_HI = 200.0;

// The single local-subwoofer group, if one is routed. A local sub adds a DAC
// output lane; the wireless sub (multiroom channel) is a separate path and never
// appears in this topology.
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
  if (!group) return DEFAULT_SUB_CROSSOVER_HZ;
  var channels = Array.isArray(group.channels) ? group.channels : [];
  for (var i = 0; i < channels.length; i += 1) {
    var channel = channels[i];
    if (channel && channel.role === 'subwoofer') {
      var fc = Number(channel.crossover_fc_hz);
      if (Number.isFinite(fc)) return fc;
      break;
    }
  }
  return DEFAULT_SUB_CROSSOVER_HZ;
}

// Clamp a user-entered crossover corner into the safe bass-management band. A
// blank/non-numeric value falls back to the default; out-of-range values pin to
// the nearest bound (defense in depth — the server also fail-loud rejects them).
export function clampSubwooferCrossoverFcHz(value) {
  // Number('') / Number('   ') coerce to 0 (finite), so reject a blank/whitespace
  // entry explicitly before the finite check — a cleared field means "default",
  // not "0 Hz" (which would otherwise pin to the low bound).
  if (typeof value === 'string' && value.trim() === '') {
    return DEFAULT_SUB_CROSSOVER_HZ;
  }
  var fc = Number(value);
  if (!Number.isFinite(fc)) return DEFAULT_SUB_CROSSOVER_HZ;
  if (fc < SUB_CROSSOVER_HZ_LO) return SUB_CROSSOVER_HZ_LO;
  if (fc > SUB_CROSSOVER_HZ_HI) return SUB_CROSSOVER_HZ_HI;
  return fc;
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

// Map a commission-load/ramp POST result to ONE calm, actionable sentence when
// the guard refused or blocked it. Returns '' on success (the card then shows the
// new armed/stepped/confirmed state). This is what prevents the "flicker then
// nothing" silent failure: the endpoints answer HTTP 200 even when a guard blocks
// the load, so the card must read the body's status — not only the HTTP code.
export function commissionPayloadFailure(payload) {
  if (!payload || typeof payload !== 'object') return '';
  if (payload.status === 'refused') {
    if (payload.reason === 'measurement_in_progress') {
      return 'Another measurement (room correction, balance, or sync) is running. ' +
        'Finish or stop it before testing a driver.';
    }
    return 'Another driver is already being tested. Stop it first, then try again.';
  }
  if (payload.status === 'no_pending_step') {
    return 'There is no active tone to confirm. Start a quiet step first.';
  }
  var load = payload.load && typeof payload.load === 'object' ? payload.load : null;
  var blocked = payload.status === 'blocked' || payload.status === 'failed' ||
    payload.status === 'gate_blocked' || payload.status === 'load_failed' ||
    payload.status === 'tone_failed' || payload.status === 'expired' ||
    (load && load.status && load.status !== 'loaded');
  if (!blocked) return '';
  var issueReason = commissionIssueReason(commissionIssueCodes(payload));
  if (issueReason) return issueReason;
  var preflight = payload.preflight ||
    (load && typeof load.preflight === 'object' ? load.preflight : null) || {};
  var gates = Array.isArray(preflight.required_gates) ? preflight.required_gates : [];
  for (var i = 0; i < gates.length; i += 1) {
    if (gates[i] && gates[i].passed === false) return commissionGateReason(gates[i].id);
  }
  return 'This driver can’t be tested yet — finish the earlier setup steps first.';
}

export function commissionPayloadHasIssue(payload, code) {
  return commissionIssueCodes(payload).indexOf(code) >= 0;
}

function commissionIssueCodes(payload) {
  var codes = [];
  [
    payload && payload.issues,
    payload && payload.load && payload.load.issues,
    payload && payload.load && payload.load.load && payload.load.load.issues,
    payload && payload.startup_setup && payload.startup_setup.issues,
    payload && payload.startup_setup && payload.startup_setup.load &&
      payload.startup_setup.load.issues,
    payload && payload.tone_playback && payload.tone_playback.issues,
    payload && payload.startup_setup && payload.startup_setup.startup_load &&
      payload.startup_setup.startup_load.load &&
      payload.startup_setup.startup_load.load.issues
  ].forEach(function(issues) {
    if (!Array.isArray(issues)) return;
    issues.forEach(function(issue) {
      if (issue && issue.code) codes.push(String(issue.code));
    });
  });
  return codes;
}

function commissionIssueReason(codes) {
  if (codes.indexOf('commission_live_state_stale') >= 0) {
    return 'The previous tone session expired safely. Start the tone again so JTS can reopen it quietly.';
  }
  if (codes.indexOf('commission_ramp_ack_expired') >= 0) {
    return 'That driver tone expired before it could be confirmed. Start it again so JTS can reopen it quietly.';
  }
  if (codes.indexOf('stage5_ramp_role_order_woofer_first') >= 0) {
    return 'Confirm the woofer first, then start the tweeter tone.';
  }
  if (codes.indexOf('stage5_ramp_gate_blocked') >= 0) {
    return 'JTS did not start the tone because a driver-test safety check is not satisfied yet. Finish the earlier driver step, then try again.';
  }
  if (codes.indexOf('commission_not_loaded') >= 0) {
    return 'Start the tone again so JTS can open the quiet driver test first.';
  }
  if (codes.indexOf('commission_ramp_at_limit') >= 0) {
    return 'Reached the safe test limit. If you still hear nothing, check amp gain, wiring, and the DAC output mapping.';
  }
  if (codes.indexOf('commission_output_hardware_reconcile_failed') >= 0) {
    return 'JTS could not switch the speaker output path into active-driver mode, so it did not start the tone.';
  }
  if (codes.indexOf('stage5_ramp_load_failed') >= 0) {
    return 'JTS could not keep the driver test path loaded while raising the tone, so it re-muted the driver. Start the tone again.';
  }
  if (
    codes.indexOf('commission_tone_playback_failed') >= 0 ||
    codes.indexOf('commission_tone_backend_failed') >= 0
  ) {
    return 'JTS loaded the quiet driver setup but could not play the test tone, so it re-muted the driver. Try again after checking the speaker audio path.';
  }
  if (
    codes.indexOf('tweeter_protection_unverified') >= 0 ||
    codes.indexOf('tweeter_protection_required') >= 0 ||
    codes.indexOf('high_frequency_protection_missing') >= 0
  ) {
    return 'The tweeter guard still needs to be set up before driver tests can start.';
  }
  if (codes.indexOf('commission_active_graph_not_staged') >= 0) {
    return 'JTS needs to load the silent active-speaker setup before this driver ' +
      'can be tested. Start the tone again; no sound will play until the test opens.';
  }
  for (var i = 0; i < codes.length; i += 1) {
    if (String(codes[i]).indexOf('commission_startup_anchor_') === 0) {
      return 'JTS could not load the silent active-speaker setup. No driver sound ' +
        'played — re-check the setup above, then start the tone again.';
    }
  }
  return '';
}

// The per-driver commissioning gates are a closed set; map each to consumer copy.
// Never surface the raw gate.message / issue codes — they carry snake_case tokens
// (e.g. route_verified) that don’t belong in a household-facing wizard.
export function commissionGateReason(gateId) {
  return {
    speaker_ready_for_active_load:
      'The speaker isn’t fully set up for driver tests yet — finish the earlier steps ' +
      '(confirm the DAC outputs, then run the setup step above) before testing a driver.',
    commissioning_candidate_prepared:
      'JTS couldn’t prepare this driver’s quiet test — re-check the crossover settings ' +
      'and DAC outputs above, then try again.',
    commissioning_protection_while_audible:
      'This driver isn’t ready to test yet — confirm the tweeter’s protection above first.',
    commissioning_candidate_present:
      'JTS couldn’t build this driver’s test setup — refresh the page and try again.'
  }[gateId] || 'A setup step still needs finishing before this driver can be tested.';
}

// Near-field placement guidance for the L1 phone level match. The page owns the
// measurement copy (backend stays vocabulary-free). The level match is OPTIONAL
// — confirming each driver by ear is enough to finish; the phone capture just
// refines the per-driver levels with a measurement. Holding the mic close and at
// a CONSISTENT distance for every driver is what makes the levels comparable.
export const NEARFIELD_LEVEL_MATCH_GUIDANCE =
  'Optional: for a measured level match, hold your phone’s microphone about ' +
  '2–5 cm from the centre of the driver, pointed straight at it, while its tone ' +
  'plays — keep the same distance for every driver. You can skip this and finish ' +
  'by ear; JTS then uses the datasheet levels.';

// Single generic fallback for the combined-test failure line when the backend
// commissioning view is unavailable (e.g. its fetch failed). The per-failure-code
// copy is OWNED by the backend coordinator (commissioning_coordinator.summed_test_
// failure_message, surfaced as combined_groups[].failure_message); the browser must
// not re-derive a parallel per-code ladder — that drifted ("to retry" vs "to try
// again"). When the view is present, render its failure_message; otherwise this.
export const SUMMED_TEST_GENERIC_RETRY_HINT =
  'The last combined test did not play. Press Play combined test to try again.';

// Resolve the failure hint shown under a combined-test group. The backend
// groupView.failure_message is authoritative when present (and may be ''); the
// generic string is only the degraded-view fallback. `suppress` is true once an
// audible test exists (no failure to report).
export function summedGroupFailureHint(groupView, { suppress = false } = {}) {
  if (suppress) return '';
  if (groupView && typeof groupView === 'object') {
    return String(groupView.failure_message || '');
  }
  return SUMMED_TEST_GENERIC_RETRY_HINT;
}

function levelMatchSourceLabel(source) {
  return {
    measured: 'Measured',
    sensitivity: 'Datasheet estimate',
    explicit: 'Manual',
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
  return {
    available: rows.length > 0,
    provisional: provisional,
    rows: rows,
    note: provisional ?
      'These per-driver levels are datasheet estimates — fine to keep. ' +
        'Optionally record a phone mic capture per driver in “Test each driver” ' +
        'to measure them instead.' :
      'Per-driver levels are set — the quietest driver is the 0 dB reference.',
    guidance: NEARFIELD_LEVEL_MATCH_GUIDANCE
  };
}

export const CALIBRATED_ALIGNMENT_GUIDANCE =
  'For polarity, use a calibrated measurement mic (Dayton iMM-6/UMM-6, miniDSP ' +
  'UMIK, or an uploaded REW curve): select it under “Calibrated mic”, then capture ' +
  'the summed crossover in-phase and with one driver inverted. A phone can ' +
  'level-match but cannot judge polarity — that needs calibrated phase.';

function humanPolarityAction(action) {
  return {
    keep: 'Polarity looks correct — keep it',
    invert: 'Invert one driver (proposed)',
    review: 'Polarity needs review'
  }[action] || '—';
}

function humanDelayStatus(status) {
  return {
    aligned: 'Drivers sum cleanly — time-aligned',
    needs_alignment: 'Deep crossover null — run the delay alignment walk',
    unknown: 'Capture the summed crossover to check'
  }[status] || '—';
}

// Summarise the /active-speaker/crossover-alignment proposal for the L2 card. Pure:
// main.js owns the DOM and the per-driver/summed curve plot from payload.curves. A
// proposal is a PROPOSAL — the maintainer reviews the surfaced evidence and confirms;
// polarity is gated on a calibrated (phase_aware) measurement, level stays the
// separate attenuation-only level match, and the delay VALUE comes from the
// timing-locked alignment walk (this surfaces only its status).
export function crossoverAlignmentSummary(payload) {
  payload = payload || {};
  var proposal = payload.proposal && typeof payload.proposal === 'object' ?
    payload.proposal : null;
  var mode = payload.mode && typeof payload.mode === 'object' ? payload.mode : {};
  var modeName = String(mode.mode || '');
  var needsCal = modeName !== 'phase_aware';
  if (!proposal) {
    return {
      available: false,
      authorized: false,
      needsCalibratedMic: needsCal,
      note: (payload.status === 'no_measurements' || payload.status === 'no_crossover') ?
        'Measure each driver near-field first, then capture the summed crossover.' :
        'No alignment proposal yet.',
      guidance: CALIBRATED_ALIGNMENT_GUIDANCE
    };
  }
  var authorized = !!proposal.authorized;
  var delayText = authorized ? humanDelayStatus(proposal.delay_status) : '—';
  var nullParts = [];
  if (typeof proposal.in_phase_null_depth_db === 'number') {
    nullParts.push('in-phase ' + proposal.in_phase_null_depth_db.toFixed(0) + ' dB');
  }
  if (typeof proposal.reverse_null_depth_db === 'number') {
    nullParts.push('reverse ' + proposal.reverse_null_depth_db.toFixed(0) + ' dB');
  }
  var issues = Array.isArray(proposal.issues) ?
    proposal.issues
      .map(function(entry) { return String((entry && entry.message) || ''); })
      .filter(Boolean) :
    [];
  return {
    available: true,
    authorized: authorized,
    needsCalibratedMic: needsCal,
    mode: modeName,
    delayText: delayText,
    polarityText: authorized ? humanPolarityAction(proposal.polarity_action) : '—',
    nullText: nullParts.length ? nullParts.join(', ') : 'no summed capture yet',
    issues: issues,
    note: authorized ?
      'Proposal from a calibrated measurement — review the evidence, then capture ' +
        'the summed crossover with the chosen polarity to apply it to the baseline.' :
      'Polarity needs a calibrated measurement mic; with a phone you can still ' +
        'level-match each driver.',
    guidance: CALIBRATED_ALIGNMENT_GUIDANCE
  };
}

export function playbackResultMessage(playback, fallback, normalizeMessage) {
  playback = playback || {};
  var issues = Array.isArray(playback.issues) ? playback.issues : [];
  for (var i = 0; i < issues.length; i += 1) {
    var issue = issues[i] || {};
    var code = String(issue.code || '').toLowerCase();
    var message = String(issue.message || issue.label || issue.code || '').trim();
    if (
      code === 'audio_backend_not_enabled' ||
      code === 'test_pcm_required' ||
      code === 'test_pcm_forbidden_main_lane'
    ) {
      return 'Driver tests are not available on this install yet.';
    }
    if (code === 'tone_plan_not_ready') {
      return 'JTS could not prepare that driver test. Choose the driver again so it can rebuild the safe test setup.';
    }
    if (message) {
      return typeof normalizeMessage === 'function' ?
        normalizeMessage(message) :
        message;
    }
  }
  if (playback.status === 'blocked') return 'JTS could not start that test. Choose the driver again to try.';
  if (playback.status === 'failed') return 'That test did not finish. Choose the driver again to try.';
  return fallback || 'No sound played.';
}
