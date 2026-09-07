// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// api.js — /sound/room/'s server address space, the transport that reaches
// it, and the homeowner-safe error a failed request throws.
import { jsonHeaders } from "/assets/shared/js/http.js";

// nginx mounts /sound/room/ on the measurement backend's ROOT, so a page
// route and its API routes share this one public prefix.
export function endpoint(path) {
  return '/sound/room/' + String(path || '').replace(/^\/+/, '');
}

// The same page over HTTPS, where the browser will hand over the
// microphone: install.sh provisions the speaker's own certificate for this
// host (provision_correction_tls) and nginx serves /sound/room/ on 443.
// `hostname`, not `host`: nginx listens on the default port for each
// scheme, so carrying this page's port across would name a closed one.
export function secureCorrectionUrl() {
  return 'https://' + window.location.hostname + '/sound/room/';
}

// The speaker's private CA, served over plain HTTP because a browser that
// will not accept the certificate cannot fetch it over HTTPS either.
export function rootCaUrl() {
  return 'http://' + window.location.hostname + '/jts-root-ca.crt';
}

export var GENERIC_STEP_FAILURE =
  'The speaker could not continue this step. Try again.';

// Must equal jasper/correction/envelope.py's ENVELOPE_SCHEMA_VERSION — an
// envelope at any other schema version is rejected as unsupported.
export var SUPPORTED_ENVELOPE_SCHEMA = 9;

export function homeownerError(failure, fallback) {
  var err = new Error(
    failure && failure.text
      ? String(failure.text)
      : String(fallback || GENERIC_STEP_FAILURE)
  );
  err.homeownerSafe = true;
  err.failure = failure || null;
  return err;
}

export function safeErrorMessage(error, fallback) {
  return error && error.homeownerSafe
    ? String(error.message)
    : String(fallback || GENERIC_STEP_FAILURE);
}

// Closed presentation vocabulary. Codes are duplicated at this wire
// boundary deliberately: a malformed/partially deployed server must not
// smuggle arbitrary diagnostics into a block the browser treats as safe.
export var KNOWN_FAILURES = {
  speaker_setup_incomplete: {text: "Finish speaker setup first.", retryable: false},
  speaker_readiness_unavailable: {text: "Speaker setup could not be checked. Try again.", retryable: true},
  speaker_readiness_fault: {text: "The speaker's saved setup could not be read. That looks like a device fault rather than a setup step, so trying again is unlikely to help.", retryable: false},
  measurement_in_progress: {text: "A measurement is already in progress. Finish or stop it before starting again.", retryable: true},
  measurement_setup_invalid: {text: "The measurement setup changed. Review the microphone choices and try again.", retryable: true},
  speaker_measurement_unsafe: {text: "The speaker is not ready to measure safely. Review speaker setup, then try again.", retryable: false},
  microphone_setup_unavailable: {text: "The saved microphone setup is unavailable. Choose the microphone again.", retryable: true},
  measurement_stopped: {text: "Measurement stopped.", retryable: true},
  test_signal_unavailable: {text: "The speaker could not play the test sound. Try again.", retryable: true},
  measurement_analysis_failed: {text: "The speaker could not finish this measurement. Try measuring again.", retryable: true},
  correction_update_failed: {text: "The correction could not be applied. Check the current correction before trying again.", retryable: true},
  correction_restore_failed: {text: "The previous sound could not be confirmed restored. The correction may still be applied.", retryable: true},
  correction_auto_revert_failed: {text: "That measured worse, but the correction could not be removed automatically. It is STILL APPLIED. Use Reset to remove it.", retryable: true},
  tuning_busy: {text: "The tuning assistant just ran. Wait a moment, then try again.", retryable: true},
  tuning_spend_limit: {text: "The daily assistant budget is reached. Try again after the daily rollover.", retryable: false},
  tuning_unavailable: {text: "The tuning assistant is not set up yet.", retryable: false},
  tuning_request_failed: {text: "The tuning assistant could not continue. Try again.", retryable: true},
  tuning_proposal_rejected: {text: "That suggestion was not applied because it did not pass the speaker's safety checks.", retryable: true},
  unknown_failure: {text: "The speaker could not continue this step. Try again.", retryable: true},
};

export function validatePublicFailure(block) {
  if (block === null) return null;
  if (!block || typeof block !== 'object' ||
      typeof block.code !== 'string' || !KNOWN_FAILURES[block.code] ||
      typeof block.text !== 'string' || !block.text.trim() ||
      typeof block.retryable !== 'boolean') {
    throw new Error('invalid room-correction failure');
  }
  var expected = KNOWN_FAILURES[block.code];
  if (block.text !== expected.text || block.retryable !== expected.retryable) {
    throw new Error('room-correction failure presentation mismatch');
  }
  var action = block.recovery_action;
  if (action !== null) {
    if (!action || typeof action.label !== 'string' || !action.label.trim() ||
        typeof action.href !== 'string' || !action.href.startsWith('/') ||
        action.href.startsWith('//') || action.href.indexOf('\\') !== -1 ||
        /[\u0000-\u001f]/.test(action.href)) {
      throw new Error('invalid room-correction recovery action');
    }
  }
  return block;
}

export async function responseError(resp, fallback) {
  var text = '';
  var payload = null;
  try {
    text = await resp.text();
    payload = JSON.parse(text);
  } catch (_e) {}
  var failure = null;
  try {
    failure = validatePublicFailure(payload && payload.failure || null);
  } catch (_e) {}
  console.warn('room-correction request failed', {
    status: resp.status,
    failureCode: failure && failure.code,
  });
  return homeownerError(failure, fallback);
}

export async function postJson(path, body) {
  var url = endpoint(path);
  var resp;
  try {
    resp = await fetch(url, {
      method: 'POST',
      headers: jsonHeaders(),
      body: JSON.stringify(body || {})
    });
  } catch (e) {
    console.warn('room-correction request unavailable', {url: url, error: e});
    throw homeownerError(null, GENERIC_STEP_FAILURE);
  }
  if (!resp.ok) {
    throw await responseError(resp, GENERIC_STEP_FAILURE);
  }
  return await resp.json();
}

export async function fetchStatus() {
  var resp;
  try {
    resp = await fetch(endpoint('status'), {cache: 'no-store'});
  } catch (e) {
    console.warn('room-correction status unavailable', e);
    throw homeownerError(null, GENERIC_STEP_FAILURE);
  }
  if (!resp.ok) throw await responseError(resp, GENERIC_STEP_FAILURE);
  return await resp.json();
}
