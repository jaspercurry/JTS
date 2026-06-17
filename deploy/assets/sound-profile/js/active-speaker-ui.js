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
  if (step === 'layout') return hasLayout && !dirty ? 'done' : 'active';
  if (step === 'research') return hasLayout && !dirty ?
    (ctx.driverResearchSatisfied ? 'done' : 'active') : 'todo';
  if (step === 'map') return ctx.outputIdentityComplete ? 'done' :
    (hasLayout && !dirty ? 'active' : 'todo');
  if (step === 'safety') return ctx.driverMeasurementsComplete ? 'done' :
    (ctx.outputIdentityComplete ? 'active' : 'todo');
  if (step === 'profile') return ctx.baselineProfileApplied ? 'done' :
    (ctx.driverMeasurementsComplete ? 'active' : 'todo');
  return 'todo';
}

export function defaultActiveSpeakerStep(ctx) {
  ctx = ctx || {};
  if (!ctx.hasLayout || ctx.dirty) return 'layout';
  if (!ctx.driverResearchSatisfied) return 'research';
  if (!ctx.outputIdentityComplete) return 'map';
  if (!ctx.driverMeasurementsComplete) return 'safety';
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
export function commissionCardState(commission, group) {
  commission = commission || {};
  var load = commission.commission_load || {};
  var ramp = commission.ramp || {};
  var floor = commission.floor || {};
  var target = load.target || {};
  var pending = ramp.pending && typeof ramp.pending === 'object' ? ramp.pending : null;
  var armed = load.status === 'loaded';
  var floorStatus = floor.status || 'floor_required';
  var awaitingAck = armed && !!pending && floorStatus === 'floor_pending_operator';
  var confirmed = Array.isArray(ramp.confirmed_roles) ? ramp.confirmed_roles : [];
  return {
    available: !!group,
    groupId: group ? (group.id || '') : '',
    armed: armed,
    armedRole: armed ? (target.role || null) : null,
    armedGainDb: armed ? target.audible_gain_db : null,
    floorStatus: floorStatus,
    awaitingAck: awaitingAck,
    pendingRole: pending ? pending.role : null,
    pendingGainDb: pending ? pending.gain_db : null,
    confirmedRoles: confirmed,
    canArm: !!group && !armed,
    canStep: armed && !awaitingAck,
    canAck: awaitingAck,
    canRemute: armed
  };
}

export function commissionFloorLabel(floorStatus) {
  return {
    floor_required: 'Not yet made audible',
    floor_pending_operator: 'Audible now — confirm by ear',
    floor_confirmed: 'Confirmed by ear'
  }[floorStatus] || 'Idle';
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

export function playbackResultMessage(playback, fallback, normalizeMessage) {
  playback = playback || {};
  var issues = Array.isArray(playback.issues) ? playback.issues : [];
  for (var i = 0; i < issues.length; i += 1) {
    var issue = issues[i] || {};
    var code = String(issue.code || '').toLowerCase();
    var message = String(issue.message || issue.label || issue.code || '').trim();
    if (
      code === 'audio_backend_not_enabled' ||
      code === 'audio_not_operator_enabled' ||
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
