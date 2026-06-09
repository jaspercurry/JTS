// escape.js — HTML-entity + DOM-id escapers shared across the canonical pages.
//
// A migrated wizard imports these by absolute path
// (`/assets/shared/js/escape.js`), the same shape as dialog.js / http.js, so
// no page re-implements the five-character entity table it sprays through
// innerHTML. These were copied near-verbatim across the wifi/bluetooth/dial/
// sound-profile/correction modules under two names (escapeHtml / escapeText)
// before this module existed.
//
// Untrusted strings (SSIDs, Bluetooth/device names, profile names, mic labels)
// land in innerHTML; escapeHtml() neutralises the five characters that matter
// in both element-content and double-quoted attribute contexts: & < > " '.
// This is the same minimal table the page modules shipped — intentionally not
// a framework or DOMPurify, which would change behaviour and add weight.
//
//     el.innerHTML = '<span>' + escapeHtml(name) + '</span>';
//     el.innerHTML = '<input value="' + escapeAttr(name) + '">';
//
// `?? ''` coerces only null/undefined to '' (preserving 0/false as their
// string form), matching the majority of the page copies this consolidates.

export function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

// Attribute-context alias. The same table escapes both content and
// double-quoted attribute values, so this is escapeHtml under a name that
// reads correctly at an attribute call site.
export { escapeHtml as escapeAttr };

// Reduce an arbitrary string to a token safe to embed in a DOM id / class
// fragment (e.g. `av-panel-${cssIdSafe(ssid)}`). Non-alphanumerics become
// underscores so getElementById round-trips with the same call.
export function cssIdSafe(s) {
  return String(s).replace(/[^a-zA-Z0-9]/g, "_");
}
