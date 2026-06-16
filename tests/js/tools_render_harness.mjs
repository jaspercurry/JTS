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
globalThis.location = new URL("http://jts.local/tools/pack/spotify/");

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
// its own boundary — render.js's safeSetupUrl must reject anything that doesn't
// RESOLVE same-origin, dropping these entirely. Covers the scheme class
// (javascript:) AND the off-origin class: protocol-relative "//host", the
// backslash form "/\host", and the whitespace-obfuscated forms "/<TAB>/host" /
// "/<LF>/host" — the WHATWG parser folds "\" -> "/" and strips ASCII
// tab/newline before parsing, so all of these normalize to a scheme-relative
// off-origin URL that a second-character regex misses.
const evilScheme = {
  name: "evil_scheme", status: "needs_setup", setup_url: 'javascript:alert("url")',
};
const evilProtoRel = {
  name: "evil_proto_rel", status: "needs_setup", setup_url: "//evil.com/x",
};
const evilBackslash = {
  name: "evil_backslash", status: "needs_setup", setup_url: "/\\evil.com/x",
};
const evilTab = {
  name: "evil_tab", status: "needs_setup", setup_url: "/\t/evil.com",
};
const evilNewline = {
  name: "evil_newline", status: "needs_setup", setup_url: "/\n/evil.com",
};
const evilTabBackslash = {
  name: "evil_tab_bs", status: "needs_setup", setup_url: "/\t\\evil.com",
};
// A legitimately safe setup link, to prove the detail-page href path still renders.
const safeUrl = {
  name: "good_url", status: "needs_setup", setup_url: "/transit/",
};
const configuredWithSetup = {
  name: "spotify_play",
  status: "active",
  setup_url: "/spotify/",
  pack: {
    id: "spotify",
    title: "Spotify",
    summary: "Music playback tools",
    setup_url: "/spotify/",
    status: "active",
  },
};
const defaultPrompt = {
  name: "default_prompt",
  status: "active",
  description: "Default prompt body",
  summary: "Default prompt example",
  prompt_customized: false,
};
const customPrompt = {
  name: "custom_prompt",
  status: "active",
  description: "Custom prompt body",
  summary: "Customized prompt example",
  prompt_customized: true,
};
// needs_setup with NO setup_url (the flag_recent_issue case) must render an
// honest "Unavailable" badge, never a dead disabled checkbox.
const noUrl = { name: "no_setup_tool", status: "needs_setup" };
const defaultPromptCard = toolCard(defaultPrompt);
const customPromptCard = toolCard(customPrompt);
const html =
  toolCard(evil) +
  toolList([
    evil, evilScheme, evilProtoRel, evilBackslash,
    evilTab, evilNewline, evilTabBackslash, safeUrl, noUrl,
  ]) +
  toolCard(noUrl) +
  defaultPromptCard +
  customPromptCard +
  toolDetail(safeUrl) +
  toolDetail(configuredWithSetup) +
  toolDetail(evil);

// Pull every href the card markup produced and RESOLVE each against a fixed
// same-origin base, asserting none escape that origin. Node's URL parser
// matches the browser's here (it folds "\" -> "/" and strips ASCII
// tab/newline before parsing), so an obfuscated off-origin href surfaces
// exactly as it would in a real <a> — a raw-string regex on the href text
// would miss the "/<TAB>/host" forms entirely.
const BASE = "http://jts.local/tools/";
const baseOrigin = new URL(BASE).origin;
const hrefs = [...html.matchAll(/href="([^"]*)"/g)].map((m) => m[1]);
const offOrigin = hrefs.some((h) => {
  try {
    const url = new URL(h, BASE);
    return url.origin !== baseOrigin ||
      (url.protocol !== "http:" && url.protocol !== "https:");
  } catch {
    return true; // an unparseable href is not a safe same-origin path
  }
});

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
  // A real same-origin path still renders as a clickable detail-page Set up link,
  // carrying the current detail page as return context for the setup wizard.
  safeHrefRendered: html.includes('href="/transit/?return_to=%2Ftools%2Fpack%2Fspotify%2F"'),
  configuredSetupLinkRendered:
    html.includes('href="/spotify/?return_to=%2Ftools%2Fpack%2Fspotify%2F"') &&
    html.includes(">Configure</a>"),
  // needs_setup with no setup_url -> honest "Unavailable", never a checkbox.
  unavailableRendered: html.includes("tool-unavailable"),
  noDeadToggle: !/<input[^>]+data-tool="no_setup_tool"/.test(html),
  // Active/off state is conveyed by toggles now, not redundant status pills.
  noOnOffBadges: !/>On<\/span>/.test(html) && !/>Off<\/span>/.test(html),
  // Top-level pack cards are row-sized navigation targets.
  packCardsClickable: html.includes("data-pack-href="),
  // Tool counts are compact title-row metadata now, not a separate card footer.
  toolCountInTitleRow: html.includes("tool-count tool-count--title"),
  // Pack detail already has canonical header back navigation; don't duplicate it.
  noDuplicateDetailBack: !html.includes('class="tool-back"'),
  // Non-actionable metadata stays in the catalog/code, not the operator UI.
  noCustomPromptCount: !html.includes("Custom prompts") && !html.includes(" customized"),
  noTimeoutMetadata: !/>Timeout<\/dt>/.test(html),
  noRiskFlagMetadata: !/>Risk flags<\/dt>/.test(html),
  toolTitleDisclosure: html.includes('<summary class="tool-row__summary">') &&
    defaultPromptCard.includes('<span class="tool-name">default_prompt</span>') &&
    defaultPromptCard.includes('<span class="tool-desc">Default prompt example</span>') &&
    !html.includes("<summary>Prompt and schema</summary>") &&
    !html.includes("Prompt, schema, and metadata"),
  resetOnlyForCustomPrompt: customPromptCard.includes(">Reset to default</button>") &&
    !defaultPromptCard.includes('data-action="reset-prompt"') &&
    !/data-action="reset-prompt"[^>]*disabled/.test(html),
  saveStartsHiddenDisabled: /data-action="save-prompt"[^>]*hidden disabled/.test(html),
  cancelStartsHidden: /data-action="cancel-prompt"[^>]*hidden/.test(html),
}));
