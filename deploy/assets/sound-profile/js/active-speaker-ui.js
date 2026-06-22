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

export function activeSpeakerStepState(step, ctx) {
  ctx = ctx || {};
  var hasLayout = !!ctx.hasLayout;
  var dirty = !!ctx.dirty;
  var driverChecksComplete = !!(
    ctx.driverChecksComplete || ctx.driverMeasurementsComplete
  );
  if (step === 'layout') return hasLayout && !dirty ? 'done' : 'active';
  if (step === 'research') return hasLayout && !dirty ?
    (ctx.driverResearchSatisfied ? 'done' : 'active') : 'todo';
  if (step === 'map') return ctx.outputIdentityComplete ? 'done' :
    (hasLayout && !dirty ? 'active' : 'todo');
  if (step === 'safety') return driverChecksComplete ? 'done' :
    (ctx.outputIdentityComplete ? 'active' : 'todo');
  if (step === 'profile') return ctx.baselineProfileApplied ? 'done' :
    (driverChecksComplete ? 'active' : 'todo');
  return 'todo';
}

export function defaultActiveSpeakerStep(ctx) {
  ctx = ctx || {};
  var driverChecksComplete = !!(
    ctx.driverChecksComplete || ctx.driverMeasurementsComplete
  );
  if (!ctx.hasLayout || ctx.dirty) return 'layout';
  if (!ctx.driverResearchSatisfied) return 'research';
  if (!ctx.outputIdentityComplete) return 'map';
  if (!driverChecksComplete) return 'safety';
  return 'profile';
}

export function outputStepTitle(step) {
  return {
    layout: 'Choose speaker layout',
    research: 'Add driver and crossover info',
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
  var floor = commission.floor || {};
  var target = load.target || {};
  var pending = ramp.pending && typeof ramp.pending === 'object' ? ramp.pending : null;
  var roles = activeCommissionRolesForGroup(group);
  var armed = load.status === 'loaded';
  var stale = load.status === 'stale' ||
    (load.runtime_status && load.runtime_status.status === 'stale');
  var floorStatus = floor.status || 'floor_required';
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
    floorStatus: floorStatus,
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
    payload.status === 'tone_failed' ||
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

function commissionIssueCodes(payload) {
  var codes = [];
  [
    payload && payload.issues,
    payload && payload.load && payload.load.issues,
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
  if (codes.indexOf('stage5_ramp_gate_blocked') >= 0) {
    return 'JTS did not start the tone because the speaker test was no longer open for that driver. Start the tone again to reopen it quietly.';
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

export function nearfieldCaptureHint(roleLabel) {
  return 'Optional — hold the phone 2–5 cm from the ' + (roleLabel || 'driver') +
    ', centred, to capture its tone for a measured level match.';
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
  'For delay and polarity, use a calibrated measurement mic (Dayton iMM-6/UMM-6, ' +
  'miniDSP UMIK, or an uploaded REW curve): select it under “Calibrated mic”, hold ' +
  'it near-field per driver, then capture the summed crossover. A phone can ' +
  'level-match but cannot set delay or polarity — those need calibrated phase.';

function humanPolarityAction(action) {
  return {
    keep: 'Polarity looks correct — keep it',
    invert: 'Invert one driver (proposed)',
    review: 'Polarity needs review'
  }[action] || '—';
}

// Summarise the /active-speaker/crossover-alignment proposal for the L2 card. Pure:
// main.js owns the DOM and the per-driver/summed curve plot from payload.curves. A
// proposal is a PROPOSAL — the maintainer reviews the surfaced evidence and confirms;
// delay/polarity are gated on a calibrated (phase_aware) measurement, level stays the
// separate attenuation-only level match.
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
  var delayText;
  if (!authorized) {
    delayText = '—';
  } else if (proposal.delay_confidence === 'aligned') {
    delayText = 'Drivers already time-aligned';
  } else if (proposal.delay_target_role && typeof proposal.delay_ms === 'number') {
    delayText = 'Delay ' + humanRole(proposal.delay_target_role) + ' ' +
      proposal.delay_ms.toFixed(2) + ' ms (estimate — validate with the null)';
  } else {
    delayText = 'No delay (capture both drivers near-field to estimate)';
  }
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
      'Proposal from a calibrated measurement — review the evidence, then Apply to ' +
        'fold delay/polarity into the baseline.' :
      'Delay and polarity need a calibrated measurement mic; with a phone you can ' +
        'still level-match each driver.',
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
