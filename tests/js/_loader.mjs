// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Shared module-loading scaffolding for tests/js. Every hand-rolled loader
// this replaces did the same three things: read a browser ES module's
// source, neutralize whatever Node can't resolve about it (a bare/absolute
// import specifier, a top-level boot call with browser-only side effects),
// then hand back the live bindings — either as a real ES module (via a
// base64 `data:` URL, so `export` / top-level `await` keep working) or, for
// sources with nothing of their own to export, as a constructed Function.
//
// Not a test suite: the underscore prefix keeps this out of the
// `crossover_*_test.mjs` glob (tests/test_crossover_wizard_js.py) and every
// hardcoded per-file pytest wrapper. scripts/check-js-syntax.sh's
// `tests/js/*.mjs` sweep still syntax-checks it, which is fine — this
// module has no side effects at import time.

import { readFileSync } from "node:fs";
import { basename, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");

// Resolve a path relative to the repo root — for the common case of loading
// a fixed deploy/assets source from a file that otherwise has no reason to
// import node:path/node:url itself. A path a caller
// already has (e.g. from process.argv, which readFileSync resolves against
// CWD exactly as before) should be passed to loadEsm/buildFunction as-is —
// this helper is for building one, not for normalizing one you have.
export function repoPath(relative) {
  return resolve(REPO_ROOT, relative);
}

// `const NAME = globalThis.__NAME;` for each name, space-joined with a
// trailing newline — the alias prelude the crossover harnesses prepend so
// the loaded module's top-level references resolve to a per-test stub the
// harness can swap out after the fact (globalThis.__NAME = ...).
export function aliasGlobals(names) {
  return names.map((name) => `const ${name} = globalThis.__${name};`).join(" ") + "\n";
}

function applyRewrite(source, rewrite) {
  return rewrite.reduce((text, [pattern, replacement]) => text.replace(pattern, replacement), source);
}

// General-purpose import-line strippers: a named/destructured import
// (possibly spanning multiple lines) and a default/namespace import. Both
// are no-ops against source with neither shape, so `stripImports: true` is
// safe even when only one style is present.
const STRIP_NAMED_IMPORT = [/^import\s+\{[\s\S]*?\}\s+from\s+["'][^"']+["'];\s*/gm, ""];
const STRIP_DEFAULT_IMPORT = [/^import\s+[^;\n]+\s+from\s+["'][^"']+["'];\s*/gm, ""];

// Bare re-export lists incl. `export { a as b };` and multi-line
// `export {\n a,\n b,\n};`, then whatever plain `export` keyword remains.
const STRIP_EXPORT_LIST = [/^\s*export\s*\{[^}]*\}\s*;?\s*$/gm, ""];
const STRIP_EXPORT_KEYWORD = [/^export\s+/gm, ""];

function importSpecifiers(source) {
  const specifiers = [];
  const pattern = /from\s+["']([^"']+)["']/g;
  let match;
  while ((match = pattern.exec(source))) specifiers.push(match[1]);
  return specifiers;
}

function transform(path, {
  rewrite, stripImports, guardNoImports,
  stripExports = false, strictImports = false, sourceBasenames,
}) {
  let source = readFileSync(path, "utf8");
  source = applyRewrite(source, rewrite);
  if (stripExports) source = applyRewrite(source, [STRIP_EXPORT_LIST, STRIP_EXPORT_KEYWORD]);
  if (strictImports) {
    for (const spec of importSpecifiers(source)) {
      if (!sourceBasenames.has(basename(spec))) {
        throw new Error(`unhandled import ${spec} in ${path} — add it to sources or stub it with a rewrite`);
      }
    }
  }
  if (stripImports) source = applyRewrite(source, [STRIP_NAMED_IMPORT, STRIP_DEFAULT_IMPORT]);
  if (guardNoImports && /^import\s/m.test(source)) {
    throw new Error(`unhandled import in ${path} — add a strip rule`);
  }
  return source;
}

function toDataUrl(source) {
  return "data:text/javascript;base64," + Buffer.from(source, "utf8").toString("base64");
}

/**
 * Load a browser ES module under Node as a real module (so its own
 * `export`s stay live) via a base64 data: URL.
 *
 *   rewrite         [[pattern, replacement], ...] applied first, in order —
 *                    e.g. a targeted substitution of one unresolvable
 *                    import with an inline reimplementation.
 *   stripImports     also strip any import line `rewrite` left behind.
 *   guardNoImports   throw if an `import` line survives — catches a new,
 *                    unhandled import instead of failing confusingly
 *                    inside the constructed module.
 *   prelude          text prepended after stripping (const aliases, stub
 *                    declarations the module's top level reaches for).
 *   truncateBefore   slice the source at the LAST occurrence of this
 *                    marker, dropping a side-effecting boot call the
 *                    harness never wants to run. Throws if not found.
 *   exportNames      appended as `export { ...names };`, for sources whose
 *                    symbols are plain top-level declarations rather than
 *                    real exports.
 *
 * Returns the import() promise — callers `await` it.
 */
export function loadEsm(path, {
  rewrite = [],
  stripImports = false,
  guardNoImports = false,
  prelude = "",
  truncateBefore = null,
  exportNames = [],
} = {}) {
  let source = transform(path, { rewrite, stripImports, guardNoImports });
  source = prelude + source;
  if (truncateBefore) {
    const cut = source.lastIndexOf(truncateBefore);
    if (cut < 0) throw new Error(`boot marker not found in ${path}: ${truncateBefore}`);
    source = source.slice(0, cut);
  }
  if (exportNames.length) source += `\nexport { ${exportNames.join(", ")} };\n`;
  return import(toDataUrl(source));
}

function returnClause(names) {
  const parts = names.map((entry) => {
    if (typeof entry === "string") return entry;
    // A symbol a source variant may not declare at all — referencing it
    // bare in the return object would throw ReferenceError instead of
    // just being undefined.
    return entry.optional
      ? `${entry.name}: (typeof ${entry.name} !== 'undefined' ? ${entry.name} : undefined)`
      : entry.name;
  });
  return `\nreturn { ${parts.join(", ")} };`;
}

/**
 * Build (without calling) a Function from one or more browser sources,
 * concatenated in order — for harnesses that need dependency-injected
 * parameters (a fake `document`, `getJSON`, ...) instead of globalThis
 * stubs, or whose module has no exports of its own to lean on.
 *
 *   sources    a path, or a list of paths / `{ path, rewrite }` entries (a
 *              per-entry rewrite overrides the top-level one for that file
 *              only — each file in the concatenation may need a different
 *              strip rule).
 *   params     formal parameter names; the caller supplies values by
 *              invoking the returned Function.
 *   returns    the trailing `return { ... };` clause. A `{ name, optional:
 *              true }` entry guards a symbol a source variant may not
 *              declare.
 *   async      use AsyncFunction instead of Function, for a source with a
 *              top-level `await`.
 *   stripExports  strip `export` syntax from every entry before stripImports.
 *
 * Returns the constructed Function; the caller invokes it.
 */
export function buildFunction(sources, {
  rewrite = [],
  stripImports = false,
  stripExports = false,
  strictImports = false,
  guardNoImports = false,
  params = [],
  returns = [],
  async: isAsync = false,
} = {}) {
  const entries = Array.isArray(sources) ? sources : [sources];
  const sourceBasenames = strictImports
    ? new Set(entries.map((entry) => basename(typeof entry === "string" ? entry : entry.path)))
    : undefined;
  const body = entries
    .map((entry) => {
      const path = typeof entry === "string" ? entry : entry.path;
      const entryRewrite = typeof entry === "string" ? rewrite : (entry.rewrite ?? rewrite);
      return transform(path, {
        rewrite: entryRewrite, stripImports, guardNoImports, stripExports, strictImports, sourceBasenames,
      });
    })
    .join("\n");
  const Ctor = isAsync ? Object.getPrototypeOf(async function () {}).constructor : Function;
  // Browser page code runs strict (it is loaded as a module); a constructed
  // Function is sloppy by default, where a bare `x = 1` silently becomes a
  // global instead of throwing. Strict keeps the harness honest about that.
  return new Ctor(...params, '"use strict";\n' + body + returnClause(returns));
}

// Flattens a children list one level at a time, dropping the arrays a
// `rows.map(...)` child spread introduces — shared by every structural h()
// double below and by callers walking the resulting tree (e.g. a `strings()`
// text-extraction helper).
export function flatten(items) {
  return items.flatMap((item) => (Array.isArray(item) ? flatten(item) : [item]));
}

// A structural double for dom.js's h(): a plain {tag, props, children} tree,
// cheap to inspect without a browser or a real DOM. `append`/`classList`/
// `style` and `dataset`/`textContent` cover the module-side calls the
// system-status harnesses exercise on a built node; they're inert where the
// module under test never reaches for them.
export function h(tag, props, ...children) {
  const node = {
    tag,
    props: props || {},
    dataset: (props && props.dataset) || {},
    children: flatten(children).filter((c) => c != null && c !== false),
    textContent: "",
  };
  node.append = (...nodes) => { node.children.push(...flatten(nodes)); };
  node.classList = { add() {} };
  node.style = { setProperty() {} };
  return node;
}
