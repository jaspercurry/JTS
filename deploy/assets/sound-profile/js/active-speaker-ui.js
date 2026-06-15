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
    safety: 'Measure drivers',
    profile: 'Validate and apply'
  }[step] || 'this card';
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
      return 'Audible driver tests are not enabled on this install yet.';
    }
    if (message) {
      return typeof normalizeMessage === 'function' ?
        normalizeMessage(message) :
        message;
    }
  }
  if (playback.status === 'blocked') return 'The driver test was blocked before sound could play.';
  if (playback.status === 'failed') return 'The driver test did not complete.';
  return fallback || 'No sound played.';
}
