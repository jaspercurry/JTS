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
import { dirname, resolve } from "node:path";
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

function transform(path, { rewrite, stripImports, guardNoImports }) {
  let source = readFileSync(path, "utf8");
  source = applyRewrite(source, rewrite);
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
 *
 * Returns the constructed Function; the caller invokes it.
 */
export function buildFunction(sources, {
  rewrite = [],
  stripImports = false,
  guardNoImports = false,
  params = [],
  returns = [],
  async: isAsync = false,
} = {}) {
  const entries = Array.isArray(sources) ? sources : [sources];
  const body = entries
    .map((entry) => {
      const path = typeof entry === "string" ? entry : entry.path;
      const entryRewrite = typeof entry === "string" ? rewrite : (entry.rewrite ?? rewrite);
      return transform(path, { rewrite: entryRewrite, stripImports, guardNoImports });
    })
    .join("\n");
  const Ctor = isAsync ? Object.getPrototypeOf(async function () {}).constructor : Function;
  return new Ctor(...params, body + returnClause(returns));
}
