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

// Add the X-CSRF-Token header (when a token is present) to an existing
// headers object, returning it. Pass nothing to start from a bare object.
export function csrfHeaders(headers) {
  const out = headers || {};
  const token = csrfToken();
  if (token) out["X-CSRF-Token"] = token;
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

// POST a JSON body with the CSRF header; parse + return the JSON response.
// Throws on a non-2xx status or transport failure, mirroring getJSON.
export async function postJSON(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify(body === undefined ? {} : body),
  });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
