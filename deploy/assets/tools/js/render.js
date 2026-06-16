// render.js — pure catalog -> HTML-string builders for the /tools/ UI.
//
// main.js/detail.js own fetches, POSTs, and event delegation. This module
// turns catalog packs/tools into markup only. Every catalog field is
// untrusted and is escaped or URL-validated before landing in innerHTML.

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

const STATUS_BADGE = {
  partial: { label: "Partial", tone: "var(--status-warn)" },
  needs_setup: { label: "Needs setup", tone: "var(--status-warn)" },
};

// A setup_url is only ever a same-origin wizard path ("/transit/", "/ha/",
// "/google/"). Require an absolute path ("/..."), then RESOLVE it against the
// page origin and demand the result stay on that origin over http(s). A
// character-level guard is not enough: the WHATWG URL parser folds "\" -> "/"
// AND strips ASCII tab/newline from the whole input BEFORE parsing, so "//host",
// "/\\host", and even "/<TAB>/host" / "/<LF>/host" all normalize to a
// scheme-relative, OFF-ORIGIN URL while slipping past a regex that only inspects
// the second character. Delegating to the real parser closes that whole
// obfuscation class — and the `javascript:`/`data:` schemes, which an
// absolute path can't form — in one place. escapeHtml escapes characters but
// never validates origin/scheme, so this is the href's own boundary. The
// catalog is the marketplace's future home for third-party setup links.
// (`location` is absent in the Node test harness; fall back to a fixed
// same-origin base so the same code path is exercised — Node's URL parser
// matches the browser's for these cases.)
function safeSetupUrl(u) {
  if (typeof u !== "string" || !u.startsWith("/")) return null;
  try {
    const base = (typeof location !== "undefined" && location.href) ||
      "http://jts.local/";
    const url = new URL(u, base);
    if (url.origin !== new URL(base).origin) return null;
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  } catch {
    return null;
  }
  return u;
}

function safePackUrl(id) {
  return typeof id === "string" && id
    ? "/tools/pack/" + encodeURIComponent(id) + "/"
    : null;
}

function badge(status) {
  const spec = STATUS_BADGE[status];
  if (!spec) return "";
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

function countLabel(n) {
  return n === 1 ? "1 tool" : n + " tools";
}

function packControl(pack) {
  if (pack.status === "needs_setup" && !safeSetupUrl(pack.setup_url)) {
    return '<span class="tool-unavailable">Unavailable</span>';
  }
  const checked = pack.status !== "off" ? " checked" : "";
  return (
    '<label class="toggle"><input type="checkbox" data-pack="' +
    escapeHtml(pack.id) + '" aria-label="Enable ' + escapeHtml(pack.title) +
    '"' + checked + ">" +
    '<span class="track"></span></label>'
  );
}

function toolControl(tool) {
  if (tool.status === "needs_setup") {
    return '<span class="tool-unavailable">Needs setup</span>';
  }
  const checked = tool.status === "active" ? " checked" : "";
  const disabled = tool.disabled_by_pack ? " disabled" : "";
  const title = tool.disabled_by_pack
    ? ' title="Enable the parent pack before changing this tool"'
    : "";
  return (
    '<label class="toggle"><input type="checkbox" data-tool="' +
    escapeHtml(tool.name) + '" aria-label="Enable ' + escapeHtml(tool.name) +
    '"' + checked + disabled + title + ">" +
    '<span class="track"></span></label>'
  );
}

function categoryRank(category) {
  const i = CATEGORY_ORDER.indexOf(category);
  return i === -1 ? CATEGORY_ORDER.length : i;
}

function sortedPacks(packs) {
  return [...packs].sort((a, b) => {
    const ac = a.category || "Other";
    const bc = b.category || "Other";
    return categoryRank(ac) - categoryRank(bc) ||
      ac.localeCompare(bc) ||
      (a.title || a.id || "").localeCompare(b.title || b.id || "");
  });
}

function packSearchText(pack, tools) {
  return [
    pack.id || "",
    pack.title || "",
    pack.summary || "",
    pack.category || "",
    ...(tools || []).flatMap((t) => [
      t.name || "",
      t.summary || "",
      t.description || "",
      ...(Array.isArray(t.labels) ? t.labels : []),
    ]),
  ].join(" ").toLowerCase();
}

function toolsForPack(catalog, pack) {
  const names = Array.isArray(pack.tool_names) ? new Set(pack.tool_names) : null;
  const tools = Array.isArray(catalog.tools) ? catalog.tools : [];
  return names ? tools.filter((t) => names.has(t.name)) : [];
}

function packsFromTools(tools) {
  const packs = new Map();
  for (const tool of tools) {
    const source = tool.pack && typeof tool.pack === "object" ? tool.pack : null;
    const id = source && source.id ? source.id : "tool:" + (tool.name || "tool");
    if (!packs.has(id)) {
      packs.set(id, {
        id,
        title: (source && source.title) || tool.name || "Tool",
        summary: (source && source.summary) || tool.summary || "",
        setup_url: (source && source.setup_url) || tool.setup_url || null,
        category: tool.category || "Other",
        status: tool.status || "off",
        tool_names: [],
        tool_count: 0,
        customized_count: 0,
      });
    }
    const pack = packs.get(id);
    pack.tool_names.push(tool.name);
    pack.tool_count += 1;
    if (tool.prompt_customized) pack.customized_count += 1;
  }
  return [...packs.values()];
}

export function packCard(pack, tools = []) {
  const href = safePackUrl(pack.id);
  const title = pack.title || pack.id || "Tool pack";
  const linkAttrs = href
    ? ' data-pack-href="' + escapeHtml(href) +
      '" role="link" tabindex="0" aria-label="' + escapeHtml(title) + '"'
    : "";
  return (
    '<article class="info-card tool-pack-card"' + linkAttrs + ">" +
    '<div class="tool-pack-card__head">' +
    "<div>" +
    '<div class="tool-pack-card__id">' +
    '<span class="tool-pack-card__title">' + escapeHtml(title) + "</span>" +
    '<span class="tool-count tool-count--title">' +
      escapeHtml(countLabel(tools.length)) + "</span>" +
    badge(pack.status) + "</div>" +
    '<p class="tool-pack-card__summary">' +
    escapeHtml(pack.summary || "") + "</p>" +
    "</div>" +
    '<div class="tool-pack-card__actions">' + packControl(pack) + "</div>" +
    "</div>" +
    "</article>"
  );
}

function categorySection(category, cards) {
  return (
    '<section class="tool-category">' +
    '<h2 class="tool-category__title">' + escapeHtml(category) + "</h2>" +
    '<div class="tool-pack-grid">' + cards.join("") + "</div>" +
    "</section>"
  );
}

export function toolList(catalog, { query = "", unavailable } = {}) {
  if (unavailable || (catalog && catalog.unavailable)) {
    return (
      '<div class="info-card tool-empty">' +
      "<p>Tool catalog isn&rsquo;t ready yet &mdash; jasper-voice writes it " +
      "at startup. If this persists, check the voice daemon on the " +
      '<a href="/system/">System</a> page.</p>' +
      "</div>"
    );
  }
  const view = Array.isArray(catalog)
    ? { tools: catalog, packs: packsFromTools(catalog) }
    : catalog || {};
  const q = (query || "").trim().toLowerCase();
  const packs = sortedPacks(Array.isArray(view.packs) ? view.packs : []);
  const filtered = packs
    .map((pack) => ({ pack, tools: toolsForPack(view, pack) }))
    .filter(({ pack, tools }) => !q || packSearchText(pack, tools).includes(q));
  if (!filtered.length) {
    return '<div class="info-card tool-empty"><p>No tools match.</p></div>';
  }
  const byCategory = new Map();
  for (const item of filtered) {
    const category = item.pack.category || "Other";
    if (!byCategory.has(category)) byCategory.set(category, []);
    byCategory.get(category).push(packCard(item.pack, item.tools));
  }
  return [...byCategory.entries()]
    .sort((a, b) => categoryRank(a[0]) - categoryRank(b[0]) || a[0].localeCompare(b[0]))
    .map(([category, cards]) => categorySection(category, cards))
    .join("");
}

function schemaBlock(tool) {
  const schema = tool.parameters || {};
  return escapeHtml(JSON.stringify(schema, null, 2));
}

function toolRow(tool) {
  const prompt = tool.description || "";
  const customized = tool.prompt_customized
    ? '<span class="badge" style="--tone: var(--status-warn)">Custom prompt</span>'
    : "";
  const resetButton = tool.prompt_customized
    ? '<button type="button" class="btn btn--ghost" data-action="reset-prompt" data-tool="' +
      escapeHtml(tool.name) + '">Reset to default</button>'
    : "";
  return (
    '<section class="tool-row" data-tool-row="' + escapeHtml(tool.name) + '">' +
    '<div class="tool-row__head">' +
    "<div>" +
    '<div class="tool-row__id">' +
    '<span class="tool-name">' + escapeHtml(tool.name) + "</span>" +
    badge(tool.status) + customized +
    "</div>" +
    '<p class="tool-desc">' + escapeHtml(tool.summary || "") + "</p>" +
    "</div>" +
    '<div class="tool-row__actions">' + toolControl(tool) + "</div>" +
    "</div>" +
    '<details class="tool-row__details">' +
    "<summary>Prompt and schema</summary>" +
    labelChips(tool.labels) +
    '<div class="prompt-editor" data-tool="' + escapeHtml(tool.name) + '">' +
    '<div class="prompt-editor__bar">' +
    '<span class="tool-count">' + escapeHtml(String(prompt.length)) + " chars</span>" +
    '<button type="button" class="btn btn--ghost" data-action="edit-prompt" data-tool="' +
    escapeHtml(tool.name) + '">Edit</button>' +
    resetButton +
    '<button type="button" class="btn" data-action="save-prompt" data-tool="' +
    escapeHtml(tool.name) + '" hidden disabled>Save</button>' +
    '<button type="button" class="btn btn--ghost" data-action="cancel-prompt" data-tool="' +
    escapeHtml(tool.name) + '" hidden>Cancel</button>' +
    "</div>" +
    '<p class="prompt-editor__warning">Advanced: edit at your own risk. Prompt overrides can change model behavior and weaken safety guidance.</p>' +
    '<pre class="prompt-view">' +
    escapeHtml(prompt) + "</pre>" +
    '<textarea class="prompt-edit" hidden>' + escapeHtml(prompt) + "</textarea>" +
    "</div>" +
    '<h4 class="tool-section-title">Input schema</h4>' +
    '<pre class="tool-schema">' + schemaBlock(tool) + "</pre>" +
    "</details>" +
    "</section>"
  );
}

export function toolCard(tool) {
  return toolRow(tool);
}

export function packDetail(pack, tools = []) {
  if (!pack) {
    return (
      '<div class="info-card tool-empty">' +
      '<p>Tool pack not found. <a href="/tools/">Back to tools</a>.</p>' +
      "</div>"
    );
  }
  const setupHref = safeSetupUrl(pack.setup_url);
  const setupLabel = pack.status === "needs_setup" ? "Set up" : "Configure";
  const setup = setupHref
    ? '<a class="btn btn--ghost" href="' + escapeHtml(setupHref) + '">' +
      escapeHtml(setupLabel) + "</a>"
    : "";
  return (
    '<article class="info-card tool-detail">' +
    '<div class="tool-detail__head">' +
    "<div>" +
    '<div class="tool-detail__titleline">' +
    '<h2 class="tool-detail__title">' + escapeHtml(pack.title || pack.id) + "</h2>" +
    '<span class="tool-count tool-count--title">' +
      escapeHtml(countLabel(tools.length)) + "</span>" +
    "</div>" +
    '<p class="tool-detail__summary">' + escapeHtml(pack.summary || "") + "</p>" +
    "</div>" +
    '<div class="tool-detail__actions">' +
    badge(pack.status) + setup + packControl(pack) +
    "</div>" +
    "</div>" +
    '<dl class="deflist tool-detail__meta">' +
    '<div><dt>Category</dt><dd>' + escapeHtml(pack.category || "Other") + "</dd></div>" +
    "</dl>" +
    '<div class="tool-authoring-link"><a href="/tools/guide/" target="_blank" rel="noopener">Tool authoring guide</a></div>' +
    '<div class="tool-rows">' + tools.map(toolRow).join("") + "</div>" +
    "</article>"
  );
}

export function toolDetail(tool) {
  if (!tool) {
    return packDetail(null, []);
  }
  const pack = tool.pack || {
    id: "tool:" + tool.name,
    title: tool.name,
    summary: tool.summary || "",
    setup_url: tool.setup_url || null,
    category: tool.category || "Other",
    status: tool.status,
    tool_count: 1,
  };
  return packDetail(pack, [tool]);
}
