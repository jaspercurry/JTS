// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// http.js — CSRF-aware fetch helpers shared across the canonical pages.
//
// The first cross-page module after dialog.js. A migrated wizard imports it
// by absolute path (`/assets/shared/js/http.js`) and uses it for every
// same-origin JSON call, so no page re-implements the CSRF/JSON plumbing.
//
// The CSRF token rides in the <meta name="jts-csrf"> tag that canonical_page()
// renders into the (no-store) HTML; we read it at call time so the cacheable
// module never bakes in a secret. Same X-CSRF-Token contract as the inline
// wizards (jasper.web._common.csrf_fetch_helpers_js / jsonHeaders) — the
// server's guard_mutating_request() accepts the header just like a hidden form field.

function csrfToken() {
  const meta = document.querySelector("meta[name=jts-csrf]");
  return meta ? meta.content : "";
}

// --- control token (WS1 Phase 2: mandatory, invisible) --------------------
// The shared token gates jasper-control's high-impact mutations (poweroff /
// reboot / restart-voice|audio / mic-mute / grouping) behind an X-JTS-Token
// header; control answers those routes 403 {error:"control_token_required"}
// without it. Phase 2 makes it invisible to the household: the page is served
// behind the read guard and embeds the token in `meta[name=jts-control-token]`
// (canonical_page), so the dashboard reads it automatically and rides it on
// every destructive POST — no prompt, no paste. We still honour a per-browser
// localStorage value as a fallback (older paste-once flow / a rotated token).
// The token is never baked into this cached JS and never logged.
const CONTROL_TOKEN_KEY = "jts-control-token";

function controlToken() {
  // Prefer the server-embedded meta tag (invisible auto-delivery); fall back to
  // a per-browser stored value. document may be absent under the node test
  // harness — guard for it.
  try {
    const meta = (typeof document !== "undefined") &&
      document.querySelector('meta[name="jts-control-token"]');
    if (meta && meta.content) return meta.content;
  } catch (_) { /* no DOM — fall through to storage */ }
  try {
    return localStorage.getItem(CONTROL_TOKEN_KEY) || "";
  } catch (_) {
    // Private-mode / disabled storage: degrade to "no stored token".
    return "";
  }
}

function storeControlToken(token) {
  try {
    localStorage.setItem(CONTROL_TOKEN_KEY, token);
  } catch (_) { /* storage unavailable — the retry still uses the in-call value */ }
}

// True when a failed response is control's "you need the token" verdict, so
// callers know to prompt rather than surface a generic error.
export function isControlTokenRequired(err) {
  return !!(err && err.status === 403 && err.body &&
            err.body.error === "control_token_required");
}

// Add the X-CSRF-Token header (when a token is present) to an existing
// headers object, returning it. Pass nothing to start from a bare object.
// Also attaches X-JTS-Token from localStorage when the browser has stored
// one for the control-token gate; absent storage adds nothing.
export function csrfHeaders(headers) {
  const out = headers || {};
  const token = csrfToken();
  if (token) out["X-CSRF-Token"] = token;
  const ctl = controlToken();
  if (ctl) out["X-JTS-Token"] = ctl;
  return out;
}

// CSRF headers + Content-Type for a JSON body.
export function jsonHeaders() {
  return csrfHeaders({ "Content-Type": "application/json" });
}

// GET + parse JSON; throws on a non-2xx status or transport failure so the
// caller can distinguish "control is down" from a successful render.
export async function getJSON(path) {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

// Parse a Response into a thrown Error carrying the server's JSON verdict on
// `.body` / `.status`, or return the parsed JSON on success. Shared by the
// one-shot poster below so success/failure shape is identical everywhere.
async function parseResponse(r) {
  if (!r.ok) {
    let parsed = null;
    try { parsed = await r.json(); } catch (_) { /* non-JSON error page */ }
    const message = (parsed && parsed.error) ? parsed.error : "HTTP " + r.status;
    const err = new Error(message);
    err.status = r.status;
    err.body = parsed;
    throw err;
  }
  return r.json();
}

// Lazy import keeps dialog.js out of the module graph for pages that never hit
// a token-gated route (the import only runs the first time we must prompt).
async function promptForControlToken() {
  const { jtsPrompt } = await import("/assets/shared/js/dialog.js");
  const token = await jtsPrompt(
    "This speaker requires a control token for power, mic-mute, and " +
    "grouping actions. Paste the token from `jasper-control-token --show`.",
    { title: "Control token required", label: "Control token", secret: true,
      okLabel: "Save & retry" },
  );
  if (token === null || token === "") return "";
  storeControlToken(token);
  return token;
}

// POST a JSON body with the CSRF header; parse + return the JSON response.
// Throws on a non-2xx status or transport failure, mirroring getJSON — but
// the thrown Error carries the server's parsed JSON verdict on `.body`
// (and `.status`), because this codebase's APIs put their actionable
// failure detail IN the body (per-member results, precondition reasons,
// rolled_back flags). Without this, every carefully built failure payload
// dies unread at the browser.
//
// Control-token gate: if the first attempt comes back 403
// control_token_required, prompt ONCE for the token, store it, and retry exactly
// once. A second 403 (wrong token) throws normally so the caller surfaces it —
// we never loop.
export async function postJSON(path, body) {
  const payload = JSON.stringify(body === undefined ? {} : body);
  const send = () => fetch(path, {
    method: "POST", headers: jsonHeaders(), body: payload,
  });
  try {
    return await parseResponse(await send());
  } catch (err) {
    if (!isControlTokenRequired(err)) throw err;
    const token = await promptForControlToken();
    if (!token) throw err;        // user dismissed the prompt — original error
    return parseResponse(await send());   // retry once with the stored token
  }
}

// POST a parameterless control action (no JSON body), returning a flat
// {ok, status, body} so callers that reflect raw status into a button label
// (the /system/ restart / reboot / power-off buttons) don't each re-implement
// the fetch + JSON-parse + control-token plumbing. Same token-gate flow as
// postJSON: a first 403 control_token_required prompts ONCE, stores, and
// retries exactly once; a second 403 (wrong token) is returned for the caller
// to surface. `body` is the parsed JSON response (or {} when non-JSON).
export async function postControlAction(path) {
  const send = () => fetch(path, { method: "POST", headers: csrfHeaders() });
  let r = await send();
  let body = await r.json().catch(() => ({}));
  if (r.status === 403 && body && body.error === "control_token_required") {
    const token = await promptForControlToken();
    if (token) {
      r = await send();
      body = await r.json().catch(() => ({}));
    }
  }
  return { ok: r.ok, status: r.status, body };
}
