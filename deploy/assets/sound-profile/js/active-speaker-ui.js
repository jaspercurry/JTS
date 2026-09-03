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

const ACTIVE_SPEAKER_STEP_IDS = ['layout', 'research', 'map', 'safety', 'profile'];

// Each rung's own verdict, from the facts it owns. Whether it holds the baton
// is NOT its call — see activeSpeakerStepState below.
function stepStateInIsolation(step, ctx) {
  var hasLayout = !!ctx.hasLayout;
  var dirty = !!ctx.dirty;
  var hardwareMatchesSaved = ctx.hardwareMatchesSaved !== false;
  var driverChecksComplete = !!(
    ctx.driverChecksComplete || ctx.driverMeasurementsComplete
  );
  var driverTargetProofComplete = !!(
    ctx.driverTargetProofComplete || (ctx.outputIdentityComplete && driverChecksComplete)
  );
  var summedValidationComplete = !!ctx.summedValidationComplete;
  if (step === 'layout') return hasLayout && !dirty && hardwareMatchesSaved ? 'done' : 'active';
  if (!hardwareMatchesSaved) return 'todo';
  if (step === 'research') return hasLayout && !dirty ?
    (ctx.driverResearchSatisfied ? 'done' : 'active') : 'todo';
  if (step === 'map') return driverTargetProofComplete ? 'done' :
    (hasLayout && !dirty ? 'active' : 'todo');
  if (step === 'safety') return summedValidationComplete ? 'done' :
    (driverTargetProofComplete ? 'active' : 'todo');
  if (step === 'profile') return ctx.baselineProfileApplied &&
    !ctx.baselineProfileNeedsRevalidation ? 'done' :
    (summedValidationComplete ? 'active' : 'todo');
  return 'todo';
}

// One rung at a time. This is the client-side mirror of the backend ladder
// (jasper/active_speaker/commissioning_coordinator._derive_step_statuses) and
// main.js falls back to it whenever a draft is mid-edit, so it carried the same
// bug: each rung answered "am I active?" from its own predicate, and a speaker
// whose outputs were already confirmed lit up BOTH the values rung and the
// combined-driver test three rungs below it. Only the first live rung keeps the
// baton; a finished rung still reports 'done'.
function activeSpeakerLadder(ctx) {
  var states = {};
  var batonTaken = false;
  ACTIVE_SPEAKER_STEP_IDS.forEach(function(step) {
    var state = stepStateInIsolation(step, ctx);
    if (state === 'active') {
      if (batonTaken) state = 'todo';
      else batonTaken = true;
    }
    states[step] = state;
  });
  return states;
}

export function activeSpeakerStepState(step, ctx) {
  ctx = ctx || {};
  var states = activeSpeakerLadder(ctx);
  return Object.prototype.hasOwnProperty.call(states, step) ?
    states[step] : 'todo';
}

export function defaultActiveSpeakerStep(ctx) {
  ctx = ctx || {};
  var driverChecksComplete = !!(
    ctx.driverChecksComplete || ctx.driverMeasurementsComplete
  );
  var driverTargetProofComplete = !!(
    ctx.driverTargetProofComplete || (ctx.outputIdentityComplete && driverChecksComplete)
  );
  if (!ctx.hasLayout || ctx.dirty || ctx.hardwareMatchesSaved === false) return 'layout';
  if (!ctx.driverResearchSatisfied) return 'research';
  if (!driverTargetProofComplete) return 'map';
  if (!ctx.summedValidationComplete) return 'safety';
  return 'profile';
}

// Name a card the way /sound/ titles it on screen. These MUST match the
// renderOutputStepCard titles in main.js — "Finish the current card before
// opening X" is useless if X is not a heading the household can find. Pinned
// (together with the backend's remedy copy) by
// tests/test_active_speaker_commissioning_coordinator.py.
export function outputStepTitle(step) {
  return {
    layout: 'Choose speaker layout',
    research: 'Add your components',
    map: 'Confirm outputs',
    safety: 'Test combined drivers',
    profile: 'Validate and apply'
  }[step] || 'this card';
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

// Map a backend commissioning-view next_action endpoint to the page's existing
// click `data-act`. Only the two next_action ids the research footer can dispatch
// on a clean draft (save the design draft / prepare the crossover preview) have a
// direct button; every later-step pointer becomes "Continue".
function nextActionAct(action) {
  var endpoint = action && typeof action === 'object' ? String(action.endpoint || '') : '';
  if (endpoint.indexOf('/design-draft') >= 0) return 'save-driver-design';
  if (endpoint.indexOf('/crossover-preview') >= 0) return 'prepare-crossover-preview';
  return '';
}

// Footer button descriptor for the active-speaker "research" and "map" setup
// steps. The backend coordinator (build_commissioning_view) already decides the
// single next obvious action from the SAVED state — `view.next_action` for the
// research label and `view.output_identity.complete` for the map readiness — so
// on a CLEAN draft the footer renders straight from that view-model instead of
// re-deriving readiness in the browser (the old driverResearchStepSatisfied /
// crossoverPreviewReadyForProtectedStaging / outputIdentityComplete duplication).
//
// The client still owns the cases the backend structurally cannot see, because
// it only reads saved state:
//   * `layoutDirty` / `draftDirty` / `saving` — unsaved edits in the browser.
//   * the crossover-preview ENABLED refinement (`previewInputsReady`): the
//     backend marks Preview crossover enabled whenever the saved design is
//     ready, but the live topology may still be missing crossover points, so the
//     page keeps the button disabled until those inputs exist (clicking an
//     enabled-but-incomplete preview would only error). The LABEL still comes
//     from the backend; only `disabled` is refined here.
//
// `view` is activeSpeaker.commissioningView (may be null before first load).
// `client` carries the browser-only signals above plus a `clientFallback`
// descriptor used when the view is unavailable or the draft is dirty/saving.
// Returns {label, primary, disabled, act, step?, source} where source is
// 'backend' on the clean view-model path and 'client' otherwise (so the parity
// test can pin which path produced the footer).
export function commissioningStepFooter(step, view, client) {
  client = client || {};
  var fallback = client.clientFallback || {};
  fallback = {
    label: String(fallback.label || ''),
    primary: fallback.primary !== false,
    disabled: !!fallback.disabled,
    act: String(fallback.act || ''),
    step: fallback.step ? String(fallback.step) : undefined,
    source: 'client'
  };
  var dirty = !!client.layoutDirty || !!client.draftDirty || !!client.saving;
  var nextAction = view && typeof view === 'object' && view.next_action &&
    typeof view.next_action === 'object' ? view.next_action : null;

  if (step === 'research') {
    if (dirty || !nextAction) return fallback;
    var act = nextActionAct(nextAction);
    if (act === 'prepare-crossover-preview') {
      return {
        label: String(nextAction.label || 'Preview crossover'),
        primary: true,
        // Backend enables whenever the saved design is ready; keep it disabled
        // until the live topology actually has the preview inputs.
        disabled: !client.previewInputsReady,
        act: act,
        source: 'backend'
      };
    }
    if (act === 'save-driver-design') {
      return {
        label: String(nextAction.label || 'Save values'),
        primary: true,
        disabled: false,
        act: act,
        source: 'backend'
      };
    }
    // next_action points past the research step (confirm outputs, driver test,
    // …) -> the saved design + preview are complete, so the footer advances.
    return {label: 'Continue', primary: true, disabled: false,
      act: 'output-step-next', step: 'research', source: 'backend'};
  }

  if (step === 'map') {
    if (dirty || !view || typeof view !== 'object') return fallback;
    var proof = view.driver_target_proof && typeof view.driver_target_proof === 'object' ?
      view.driver_target_proof : {};
    if (proof.complete === true) {
      return {label: 'Continue', primary: true, disabled: false,
        act: 'output-step-next', step: 'map', source: 'backend'};
    }
    // Output/driver proof is completed inside the step; the footer is a disabled
    // waiting affordance, not a second CTA.
    return {label: 'Confirm drivers', primary: true, disabled: true,
      act: '', source: 'backend'};
  }

  return fallback;
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
      payload.startup_setup.startup_load.load.issues,
    // A blocked ramp step reports `ramp_prepare_failed` at the top level and
    // parks the REASON it failed in a sibling array. Without this the household
    // saw the outer code and none of the codes that explain it (#2344).
    payload && payload.prepare_issues
  ].forEach(function(issues) {
    if (!Array.isArray(issues)) return;
    issues.forEach(function(issue) {
      if (issue && issue.code) codes.push(String(issue.code));
    });
  });
  return codes;
}

function commissionIssueReason(codes) {
  // First in the ladder on purpose: these are whole-speaker output-path states,
  // not steps that failed. While one holds, every later reason below would be
  // true-but-useless advice ("try again", "finish the earlier step") for
  // something the household cannot fix by retrying. Each names the state and
  // where to look WITHOUT the operator's shell command — those remedies live on
  // the CLI and journal surfaces, never here (#2344, #2412).
  //
  // The two arming codes share one sentence because the household ACTION is the
  // same for both; the two operator remedies are what differ, and they belong
  // to two different reconcilers.
  if (
    codes.indexOf('commissioning_ring_feed_unarmed') >= 0 ||
    codes.indexOf('commissioning_active_endpoint_unarmed') >= 0
  ) {
    return 'This speaker’s output path isn’t finished setting up, so driver ' +
      'tests can’t run yet. Open System status.';
  }
  // An internal defect, not a setting: the graph would play out of one output
  // connection and capture from another. Do not offer an action that does not
  // exist — point at the screen that shows the speaker's own state.
  if (codes.indexOf('commissioning_transport_ends_disagree') >= 0) {
    return 'JTS could not prepare the driver test for this speaker’s output ' +
      'connection. Open System status.';
  }
  // An operator-set output-connection override that no longer parses. Mapped
  // here for the same reason as the codes above: the backend sentence names the
  // daemon that owns the other half, which reads as a command. The household
  // cannot fix an override from this card, so the copy sends them to the one
  // place that shows it.
  if (codes.indexOf('ring_wire_declaration_invalid') >= 0) {
    return 'This speaker’s output connection is set to something JTS doesn’t ' +
      'recognise, so driver tests can’t run. Open System status.';
  }
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
    return 'JTS did not start the tone because an output-confirmation safety check is not satisfied yet. Finish the earlier output, then try again.';
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
  if (codes.indexOf('driver_target_identity_save_failed') >= 0) {
    return 'JTS heard the driver, but could not save the output confirmation. Try again before continuing.';
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
  if (codes.indexOf('staged_startup_hold_unavailable') >= 0) {
    // One step earlier than the anchor codes below: the load refused before it
    // changed anything, because it could not hold the silent setup in place.
    // Retrying cannot clear it — the speaker's own software has to be repaired —
    // so this points at System status instead of offering another attempt.
    return 'JTS could not hold the silent active-speaker setup in place, so it ' +
      'left the speaker as it was and played no driver sound. Open System status.';
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
      'JTS couldn’t build this driver’s test setup — refresh the page and try again.',
    commissioning_transport_supported:
      'JTS couldn’t prepare this driver’s test for the speaker’s output ' +
      'connection. Open System status.',
    commissioning_transport_armed:
      'This speaker’s output path isn’t finished setting up, so driver tests ' +
      'can’t run yet. Open System status.'
  }[gateId] || 'A setup step still needs finishing before this driver can be tested.';
}

// Pointer to the L1 level match for the driver-levels card. The level match is
// OPTIONAL — confirming each driver by ear is enough to finish HERE. This page
// cannot record: /sound/ is plain HTTP, so `getUserMedia` is unavailable and no
// recorder exists in this bundle. The measurement lives on the HTTPS
// /correction/ hub's "Active speaker" tab (correction_hub.SECTIONS — the
// household-facing label for the still-internally-"crossover" slug), so this
// copy is only a pointer to it. Name the TAB, not just the host: typing
// jts.local/correction lands on the sibling Room tab, and "room correction" is
// already this copy's name for the later stage you may skip to.
// Placement geometry is OWNED by jasper/active_speaker/capture_geometry.py and
// rendered by the measurement page for the capture kind in play. Do NOT
// restate a distance or an aim instruction here.
export const NEARFIELD_LEVEL_MATCH_GUIDANCE =
  'Automatic tuning option: confirming each driver by ear here is enough to ' +
  'finish. The automatic crossover measures the drivers for you — open ' +
  'jts.local/correction and choose the Active speaker tab. A safe applied ' +
  'manual crossover can proceed to room correction without that step. ' +
  'Measured values replace manual pins only when you explicitly apply the ' +
  'automatic crossover.';

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
