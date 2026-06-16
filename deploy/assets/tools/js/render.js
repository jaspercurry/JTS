// render.js — pure tool -> HTML-string builders for the /tools/ catalog.
//
// Kept side-effect-free: main.js owns the fetch / filter / toggle wiring and
// the event delegation; this module only turns catalog entries into cards,
// groups, and detail views. Every tool field is treated as UNTRUSTED and run
// through escapeHtml or URL validation before it lands in innerHTML — the
// catalog is the marketplace's future home, and good hygiene + the conventions
// test require it for first-party text too.
//
// The on/off control is the canonical .toggle checkbox (same markup
// jasper.web._common.toggle_html() renders server-side); the toggle key rides
// in an escaped data-tool attribute that main.js's delegated change listener
// reads — never an inline onclick with an interpolated name.

import { escapeHtml } from "/assets/shared/js/escape.js";

const CATEGORY_ORDER = [
  "Music",
  "Transit",
  "Smart Home",
  "Productivity",
  "Utilities",
  "System",
  "Other",
];

// A setup_url is only ever a same-origin wizard path ("/transit/", "/ha/",
// "/google/"). Accept ONLY an absolute path whose first char is "/" and whose
// SECOND char is neither "/" nor "\\". Both "//host" and "/\\host" are
// scheme-relative (browsers normalize "\\" to "/" in special schemes), so they
// navigate OFF-ORIGIN — the backslash form slips past a naive `!"//"` check.
// This also neutralizes `javascript:`/`data:` schemes (they don't start with
// "/") BEFORE the value reaches an <a href> — escapeHtml escapes characters
// but does not validate schemes/authorities, so it can't stop these on its
// own. The catalog is the marketplace's future home for third-party setup
// links, so the href is a real boundary.
function safeSetupUrl(u) {
  return typeof u === "string" && /^\/(?![/\\])/.test(u) ? u : null;
}

function safeDetailUrl(name) {
  return typeof name === "string" && name
    ? "/tools/tool/" + encodeURIComponent(name) + "/"
    : null;
}

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

function categoryOf(tool) {
  return typeof tool.category === "string" && tool.category.trim()
    ? tool.category.trim()
    : "Other";
}

function packOf(tool) {
  return tool && typeof tool.pack === "object" && tool.pack !== null
    ? tool.pack
    : null;
}

function summaryOf(tool) {
  return typeof tool.summary === "string" && tool.summary
    ? tool.summary
    : (tool.description || "");
}

function packKey(pack) {
  return pack && typeof pack.id === "string" && pack.id
    ? "pack:" + pack.id
    : null;
}

function groupTools(tools) {
  const categories = new Map();
  for (const tool of tools) {
    const category = categoryOf(tool);
    if (!categories.has(category)) categories.set(category, []);
    const groups = categories.get(category);
    const pack = packOf(tool);
    const key = packKey(pack);
    let group = key ? groups.find((g) => g.key === key) : null;
    if (!group) {
      group = { key: key || "standalone:" + groups.length, pack, tools: [] };
      groups.push(group);
    }
    group.tools.push(tool);
  }
  return [...categories.entries()].sort((a, b) => {
    const ai = CATEGORY_ORDER.indexOf(a[0]);
    const bi = CATEGORY_ORDER.indexOf(b[0]);
    const ar = ai === -1 ? CATEGORY_ORDER.length : ai;
    const br = bi === -1 ? CATEGORY_ORDER.length : bi;
    return ar - br || a[0].localeCompare(b[0]);
  });
}

// The right-hand control. A needs_setup tool can't be enabled usefully (its
// backend isn't configured), so it shows a "Set up" link to its wizard. With
// no (safe) wizard URL — e.g. a core tool that degraded to needs_setup, like
// flag_recent_issue when the wake-events DB won't open — there's nothing to
// toggle and nowhere to go, so it shows an honest "Unavailable" badge rather
// than a dead disabled checkbox. Configured tools (active/off) show the
// canonical toggle, checked when active.
function control(tool) {
  if (tool.status === "needs_setup") {
    const href = safeSetupUrl(tool.setup_url);
    if (href) {
      return (
        '<a class="btn btn--ghost tool-setup" href="' +
        escapeHtml(href) + '">Set up</a>'
      );
    }
    return '<span class="tool-unavailable">Unavailable</span>';
  }
  const checked = tool.status === "active" ? " checked" : "";
  // aria-label gives the checkbox an accessible name (otherwise a screen
  // reader announces a bare "checkbox"); the tool name is the right label.
  return (
    '<label class="toggle"><input type="checkbox" data-tool="' +
    escapeHtml(tool.name) + '" aria-label="Enable ' + escapeHtml(tool.name) +
    '"' + checked + ">" +
    '<span class="track"></span></label>'
  );
}

// One catalog entry -> card markup.
export function toolCard(tool) {
  const detailUrl = safeDetailUrl(tool.name);
  const name = escapeHtml(tool.name);
  const nameHtml = detailUrl
    ? '<a class="tool-name" href="' +
      escapeHtml(detailUrl) + '">' + name + "</a>"
    : '<span class="tool-name">' + name + "</span>";
  return (
    '<div class="info-card tool-card">' +
    '<div class="tool-card__head">' +
    '<div class="tool-card__id">' +
    nameHtml +
    badge(tool.status) +
    "</div>" +
    control(tool) +
    "</div>" +
    '<p class="tool-desc">' + escapeHtml(summaryOf(tool)) + "</p>" +
    labelChips(tool.labels) +
    "</div>"
  );
}

function countLabel(n) {
  return n === 1 ? "1 tool" : n + " tools";
}

function packGroup(group) {
  if (!group.pack) {
    return group.tools.map(toolCard).join("");
  }
  const pack = group.pack;
  const title = pack.title || pack.id || "Tool pack";
  const summary = pack.summary || "";
  const setupHref = safeSetupUrl(pack.setup_url);
  const setup = setupHref
    ? '<a class="btn btn--ghost tool-pack__setup" href="' +
      escapeHtml(setupHref) + '">Set up</a>'
    : "";
  return (
    '<section class="tool-pack">' +
    '<div class="tool-pack__head">' +
    "<div>" +
    '<h3 class="tool-pack__title">' + escapeHtml(title) + "</h3>" +
    (summary
      ? '<p class="tool-pack__summary">' + escapeHtml(summary) + "</p>"
      : "") +
    "</div>" +
    '<div class="tool-pack__meta">' +
    '<span class="tool-count">' +
    escapeHtml(countLabel(group.tools.length)) + "</span>" +
    setup +
    "</div>" +
    "</div>" +
    '<div class="tool-pack__tools">' + group.tools.map(toolCard).join("") + "</div>" +
    "</section>"
  );
}

function categorySection(category, groups) {
  return (
    '<section class="tool-category">' +
    '<h2 class="tool-category__title">' + escapeHtml(category) + "</h2>" +
    groups.map(packGroup).join("") +
    "</section>"
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
  return groupTools(tools)
    .map(([category, groups]) => categorySection(category, groups))
    .join("");
}

export function toolDetail(tool) {
  if (!tool) {
    return (
      '<div class="info-card tool-empty">' +
      '<p>Tool not found. <a href="/tools/">Back to tools</a>.</p>' +
      "</div>"
    );
  }
  const pack = packOf(tool);
  const setupHref = safeSetupUrl(tool.setup_url) ||
    safeSetupUrl(pack && pack.setup_url);
  const setup = setupHref
    ? '<a class="btn" href="' + escapeHtml(setupHref) + '">Set up</a>'
    : "";
  const providers = Array.isArray(tool.providers) && tool.providers.length
    ? tool.providers.join(", ")
    : "All voice providers";
  const packRow = pack
    ? '<div><dt>Pack</dt><dd>' +
      escapeHtml(pack.title || pack.id) + "</dd></div>"
    : "";
  return (
    '<article class="info-card tool-detail">' +
    '<div class="tool-detail__head">' +
    "<div>" +
    '<a class="tool-back" href="/tools/">Tools</a>' +
    '<h2 class="tool-detail__title">' + escapeHtml(tool.name) + "</h2>" +
    "</div>" +
    '<div class="tool-detail__actions">' + badge(tool.status) + setup + "</div>" +
    "</div>" +
    '<dl class="deflist tool-detail__meta">' +
    '<div><dt>Category</dt><dd>' + escapeHtml(categoryOf(tool)) + "</dd></div>" +
    packRow +
    '<div><dt>Providers</dt><dd>' + escapeHtml(providers) + "</dd></div>" +
    "</dl>" +
    labelChips(tool.labels) +
    '<div class="tool-detail__description">' +
    escapeHtml(tool.details || tool.description || tool.summary || "") +
    "</div>" +
    "</article>"
  );
}
