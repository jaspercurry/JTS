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
const { toolCard, toolList } = new Function(
  escapeSrc + "\n" + renderSrc + "\nreturn { toolCard, toolList };",
)();

// Every field a malicious/compromised tool could control, with a distinct
// payload per HTML context (element content, attribute, the setup_url href).
const evil = {
  name: '<script>alert("name")</script>',
  description: '<img src=x onerror=alert("desc")>',
  labels: ['<svg onload=alert("label")>'],
  status: "needs_setup", // exercises the setup_url <a href> path
  setup_url: '"><script>alert("url")</script>',
};
const html = toolCard(evil) + toolList([evil]);

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
}));
