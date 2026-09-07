// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// api.js — /sound/room/'s server address space and the homeowner-safe error
// its requests throw. main.js owns the transport that raises them.

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
