// render.js — pure tool -> HTML-string builders for the /tools/ catalog.
//
// Kept side-effect-free: main.js owns the fetch / filter / toggle wiring and
// the event delegation; this module only turns a catalog entry into the card
// markup. Every tool field (name, description, labels, setup_url) is treated
// as UNTRUSTED and run through escapeHtml before it lands in innerHTML — the
// catalog is the marketplace's future home, and good hygiene + the conventions
// test require it for first-party text too.
//
// The on/off control is the canonical .toggle checkbox (same markup
// jasper.web._common.toggle_html() renders server-side); the toggle key rides
// in an escaped data-tool attribute that main.js's delegated change listener
// reads — never an inline onclick with an interpolated name.

import { escapeHtml } from "/assets/shared/js/escape.js";

// status -> { label, --tone } for the .badge pill. needs_setup uses the idle
// tone (it's not an error, just unconfigured); off is muted; active is green.
const STATUS_BADGE = {
  active: { label: "On", tone: "var(--status-ok)" },
  off: { label: "Off", tone: "var(--status-idle)" },
  needs_setup: { label: "Needs setup", tone: "var(--status-warn)" },
};

function badge(status) {
  const spec = STATUS_BADGE[status] || STATUS_BADGE.off;
  return (
    '<span class="badge" style="--tone: ' + spec.tone + '">' +
    escapeHtml(spec.label) + "</span>"
  );
}

function labelChips(labels) {
  if (!Array.isArray(labels) || labels.length === 0) return "";
  const chips = labels
    .map((l) => '<span class="tool-label">' + escapeHtml(l) + "</span>")
    .join("");
  return '<div class="tool-labels">' + chips + "</div>";
}

// The right-hand control. A needs_setup tool can't be enabled usefully (its
// backend isn't configured), so it shows a "Set up" link to its wizard instead
// of a live toggle. Configured tools (active/off) show the canonical toggle,
// checked when active.
function control(tool) {
  if (tool.status === "needs_setup") {
    if (tool.setup_url) {
      return (
        '<a class="btn btn--ghost tool-setup" href="' +
        escapeHtml(tool.setup_url) + '">Set up</a>'
      );
    }
    // No wizard for a core tool that somehow reports needs_setup — show a
    // disabled toggle so the row still reads as a control, not a dead end.
    return (
      '<label class="toggle"><input type="checkbox" disabled>' +
      '<span class="track"></span></label>'
    );
  }
  const checked = tool.status === "active" ? " checked" : "";
  return (
    '<label class="toggle"><input type="checkbox" data-tool="' +
    escapeHtml(tool.name) + '"' + checked + ">" +
    '<span class="track"></span></label>'
  );
}

// One catalog entry -> card markup.
export function toolCard(tool) {
  return (
    '<div class="info-card tool-card">' +
    '<div class="tool-card__head">' +
    '<div class="tool-card__id">' +
    '<span class="tool-name">' + escapeHtml(tool.name) + "</span>" +
    badge(tool.status) +
    "</div>" +
    control(tool) +
    "</div>" +
    '<p class="tool-desc">' + escapeHtml(tool.description) + "</p>" +
    labelChips(tool.labels) +
    "</div>"
  );
}

// The whole list (or an empty / unavailable state) for a filtered set.
export function toolList(tools, { unavailable } = {}) {
  if (unavailable) {
    return (
      '<div class="info-card tool-empty">' +
      "<p>Tool catalog isn&rsquo;t ready yet &mdash; jasper-voice writes it " +
      "at startup. If this persists, check the voice daemon on the " +
      '<a href="/system/">System</a> page.</p>' +
      "</div>"
    );
  }
  if (!tools.length) {
    return '<div class="info-card tool-empty"><p>No tools match.</p></div>';
  }
  return tools.map(toolCard).join("");
}
