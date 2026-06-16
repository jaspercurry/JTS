// Renders a deliberately malicious tool object through the /tools/ catalog's
// render.js and reports whether every untrusted field was HTML-escaped before
// landing in the card markup. The /tools/ catalog is the marketplace's future
// home for third-party tool name/description/labels/setup_url, so the escaping
// is a security boundary — this turns the runtime-verified claim into a
// regression-guarded one. Driven by tests/test_tools_render_xss.py.
//
//   node tools_render_harness.mjs <path-to-escape.js> <path-to-render.js>
//
// render.js `import`s escapeHtml by absolute URL (node can't resolve that), so
// we inline escape.js ahead of it and strip the import/export keywords — the
// same "load the ESM source via new Function" trick dialog_harness.mjs uses.
import { readFileSync } from "node:fs";

// Drop `export { a as b };` re-export lines wholesale; strip the `export`
// keyword off declarations. (A bare `export ` strip would leave an invalid
// `{ a as b };` block from escape.js's escapeAttr alias.)
const stripExports = (s) =>
  s.replace(/^\s*export\s*\{[^}]*\}\s*;?\s*$/gm, "")
   .replace(/\bexport\s+(?=function|const|let|class)/g, "");
const stripImports = (s) => s.replace(/^\s*import\s.*$/gm, "");

const escapeSrc = stripExports(readFileSync(process.argv[2], "utf8"));
const renderSrc = stripExports(stripImports(readFileSync(process.argv[3], "utf8")));
const { toolCard, toolDetail, toolList } = new Function(
  escapeSrc + "\n" + renderSrc + "\nreturn { toolCard, toolDetail, toolList };",
)();

// Every field a malicious/compromised tool could control, with a distinct
// payload per HTML context (element content + the attribute / data-tool path).
const evil = {
  name: '<script>alert("name")</script>',
  description: '<img src=x onerror=alert("desc")>',
  summary: '<img src=x onerror=alert("summary")>',
  details: '<svg onload=alert("details")>',
  labels: ['<svg onload=alert("label")>'],
  category: '<script>alert("category")</script>',
  pack: {
    id: "evil-pack",
    title: '<img src=x onerror=alert("pack-title")>',
    summary: '<svg onload=alert("pack-summary")>',
    setup_url: 'javascript:alert("pack-url")',
  },
  status: "active", // exercises the data-tool attribute path
};
// needs_setup tools whose setup_url is dangerous in an <a href>. escapeHtml
// escapes characters but does NOT validate schemes/authorities, so the href is
// its own boundary — render.js's safeSetupUrl must reject anything that isn't a
// same-origin "/..." path, dropping these entirely. Covers the scheme class
// AND the off-origin class (protocol-relative "//host" and the backslash form
// "/\host", which browsers normalize to "//host").
const evilScheme = {
  name: "evil_scheme", status: "needs_setup", setup_url: 'javascript:alert("url")',
};
const evilProtoRel = {
  name: "evil_proto_rel", status: "needs_setup", setup_url: "//evil.com/x",
};
const evilBackslash = {
  name: "evil_backslash", status: "needs_setup", setup_url: "/\\evil.com/x",
};
// A legitimately safe setup link, to prove the href path still renders.
const safeUrl = {
  name: "good_url", status: "needs_setup", setup_url: "/transit/",
};
// needs_setup with NO setup_url (the flag_recent_issue case) must render an
// honest "Unavailable" badge, never a dead disabled checkbox.
const noUrl = { name: "no_setup_tool", status: "needs_setup" };
const html =
  toolCard(evil) +
  toolList([evil, evilScheme, evilProtoRel, evilBackslash, safeUrl, noUrl]) +
  toolCard(noUrl) +
  toolDetail(evil);

// Pull every href the card markup produced, to assert none point off-origin.
const hrefs = [...html.matchAll(/href="([^"]*)"/g)].map((m) => m[1]);
const offOrigin = hrefs.some(
  (h) => /^(?:[a-z]+:|\/\/|\/\\)/i.test(h.trim()),
);

// Check that no RAW payload tag survived — every untrusted `<` must have become
// `&lt;`. The card's own markup uses div/span/a/p/label/input, never
// script/img/svg, so any of those appearing means a payload's `<` escaped
// unescaped. (Do NOT test for `onerror=`/`onload=` TEXT: those words appear
// harmlessly inside the escaped `&lt;img ...&gt;` content and would false-fail.)
console.log(JSON.stringify({
  noScriptTag: !/<script/i.test(html),
  noImgTag: !/<img/i.test(html),
  noSvgTag: !/<svg/i.test(html),
  // Proof the fields actually went through escapeHtml (not just absent payloads).
  escapedEntitiesPresent: html.includes("&lt;") && html.includes("&gt;"),
  // The javascript: scheme must be dropped, NOT merely escaped into an href.
  noJavascriptScheme: !/javascript:/i.test(html),
  // No rendered href may point off-origin (scheme, "//host", or "/\\host").
  noOffOriginHref: !offOrigin,
  // A real same-origin path still renders as a clickable Set up link.
  safeHrefRendered: html.includes('href="/transit/"'),
  // needs_setup with no setup_url -> honest "Unavailable", never a checkbox.
  unavailableRendered: html.includes("tool-unavailable"),
  noDeadToggle: !/data-tool="no_setup_tool"/.test(html),
}));
