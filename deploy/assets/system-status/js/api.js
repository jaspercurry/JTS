// api.js — CSRF-aware fetch helpers.
//
// The CSRF token rides in the <meta name="jts-csrf"> tag that
// canonical_page() renders into the (no-store) HTML; we read it at call time
// so the cacheable module never bakes in a secret. Same X-CSRF-Token contract
// as the inline wizards.

function csrfToken() {
  const meta = document.querySelector("meta[name=jts-csrf]");
  return meta ? meta.content : "";
}

export function csrfHeaders(headers) {
  const out = headers || {};
  const token = csrfToken();
  if (token) out["X-CSRF-Token"] = token;
  return out;
}

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
