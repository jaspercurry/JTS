// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Pure helpers for the capture page (build step 3) — no browser/DOM imports, so
// they are unit-testable in node (tests/js/capture_fragment_test.mjs).
//
// The Pi delivers the session handle in the URL FRAGMENT:
//   https://capture.jasper.tech/#s=<session_id>&u=<upload_token>&k=<base64url key>
// The fragment is the one part of the URL browsers never transmit to a server,
// which is exactly why the E2E content_key (`k`) rides there — the relay never
// receives it. `s` and `u` also ride the fragment so the relay logs never see
// them either.

export class FragmentError extends Error {
  constructor(message) {
    super(message);
    this.name = "FragmentError";
  }
}

// Parse the `#s=..&u=..&k=..` fragment. Accepts an optional leading '#'.
export function parseFragment(hash) {
  const raw = typeof hash === "string" ? hash : "";
  const params = new URLSearchParams(raw.startsWith("#") ? raw.slice(1) : raw);
  const sessionId = params.get("s") || "";
  const uploadToken = params.get("u") || "";
  const contentKeyB64 = params.get("k") || "";
  if (!sessionId || !uploadToken || !contentKeyB64) {
    throw new FragmentError(
      "This measurement link is incomplete or expired. Start again from your speaker.",
    );
  }
  // base64url for a 32-byte key is 43 chars (unpadded). Be lenient on padding
  // but reject anything obviously wrong so a bad link fails loud, not silently.
  if (!/^[A-Za-z0-9_-]{43,44}=?$/.test(contentKeyB64)) {
    throw new FragmentError("This measurement link is malformed. Start again from your speaker.");
  }
  return { sessionId, uploadToken, contentKeyB64 };
}

// The total record window in ms, defaulting safely if the spec omits it.
export function recordWindowMs(spec) {
  const d = spec && Number(spec.duration_ms);
  if (Number.isFinite(d) && d > 0) return d;
  return 12000;
}

// Page half of the plan's "dual size cap" (§8): the page refuses to upload a
// blob larger than spec.max_upload_bytes so a wrong/oversized spec fails loud
// locally rather than after a wasted capture + a Worker 413. An absent/invalid
// cap defers to the Worker (which always enforces its own hard ceiling).
export function withinUploadCap(byteLength, spec) {
  const cap = spec && Number(spec.max_upload_bytes);
  if (!Number.isFinite(cap) || cap <= 0) return true;
  return byteLength <= cap;
}
