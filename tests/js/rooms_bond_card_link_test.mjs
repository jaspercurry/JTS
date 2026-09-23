// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { element } from "./_dom.mjs";
import { loadEsm, repoPath } from "./_loader.mjs";

class Style {
  setProperty(name, value) { this[name] = value; }
}
class El {
  constructor(tag) {
    const { addEventListener, click, classList } = element(tag);
    Object.assign(this, { addEventListener, click, classList });
    this.tagName = tag;
    this.children = [];
    this.attributes = {};
    this.style = new Style();
    this.dataset = {};
    this.className = "";
  }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) {
    return Object.prototype.hasOwnProperty.call(this.attributes, key)
      ? this.attributes[key] : null;
  }
}

const timers = new Map();
let nextTimer = 0;
globalThis.setTimeout = (fn, ms) => { timers.set(++nextTimer, { fn, ms }); return nextTimer; };
globalThis.clearTimeout = (id) => timers.delete(id);
async function fire(ms) {
  const runs = [];
  for (const [id, timer] of [...timers]) {
    if (timer.ms !== ms) continue;
    timers.delete(id);
    runs.push(timer.fn());
  }
  await Promise.all(runs);
}
const pending = [];
let gets = 0;
globalThis.__getJSON = (url) => {
  assert.equal(url, "rooms.json");
  gets++;
  return new Promise((resolve) => pending.push(resolve));
};
const posts = [];
globalThis.__postJSON = async (...args) => { posts.push(args); return { ok: true }; };
const appRoot = new El("div");
globalThis.Node = El;
globalThis.location = { hostname: "test-speaker.local" };
globalThis.document = {
  body: element(), visibilityState: "visible", addEventListener() {},
  createElement: (tag) => new El(tag),
  createElementNS: (_ns, tag) => new El(tag),
  createTextNode: (text) => Object.assign(new El("#text"), { textContent: String(text) }),
  getElementById: (id) => (id === "app" ? appRoot : null),
};

const httpUrl = pathToFileURL(repoPath("deploy/assets/shared/js/http.js")).href;
const domUrl = pathToFileURL(repoPath("deploy/assets/shared/js/dom.js")).href;
const pbcUrl = pathToFileURL(repoPath("deploy/assets/rooms/js/pair-balance-controller.js")).href;
const groupingUrl = pathToFileURL(repoPath("deploy/assets/rooms/js/grouping-view.js")).href;

const { refs } = await loadEsm(repoPath("deploy/assets/rooms/js/main.js"), {
  rewrite: [
    [/^import \{ getJSON, postJSON, startPolling \} from "\/assets\/shared\/js\/http\.js";\n/m, `import { startPolling } from "${httpUrl}";\n`],
    [/^import \{ jtsConfirm \} from "\/assets\/shared\/js\/dialog\.js";\n/m, ""],
    [/^import \{ localWebHost \} from "\/assets\/shared\/js\/local-web-host\.js";\n/m, ""],
    [/"\/assets\/shared\/js\/dom\.js"/, `"${domUrl}"`],
    [/"\.\/pair-balance-controller\.js"/, `"${pbcUrl}"`],
    [/"\.\/grouping-view\.js"/, `"${groupingUrl}"`],
  ],
  prelude:
    "const getJSON = globalThis.__getJSON;\n" +
    "const postJSON = globalThis.__postJSON;\n" +
    "const jtsConfirm = async () => true;\n" +
    "const localWebHost = () => '';\n",
  exportNames: ["refs"],
});

function collectAnchors(node, out) {
  if (!node || !Array.isArray(node.children)) return out;
  if (node.tagName === "a") out.push(node);
  for (const child of node.children) collectAnchors(child, out);
  return out;
}

const hrefs = collectAnchors(refs.bondCard.el, []).map(
  (a) => a.getAttribute("href"),
);
assert.deepEqual(hrefs, ["sync/"], "bond card links only its timing child");
for (const href of hrefs) {
  assert.ok(
    !/^([a-z][a-z0-9+.-]*:|\/\/)/i.test(href),
    `bond card anchor must stay on this origin, got ${href}`,
  );
}

function buttons(node) {
  return [node, ...node.children.flatMap(buttons)].filter((el) => el.tagName === "button");
}
const dissolve = buttons(refs.bondCard.el).find((el) =>
  el.children.some((child) => child.textContent === "Dissolve group"));
assert.ok(dissolve);
await dissolve.click();
await dissolve.click();
assert.deepEqual(posts, [["unbond", {}], ["unbond", {}]]);
assert.equal([...timers.values()].filter((t) => t.ms === 1200).length, 2);
const actions = fire(1200);
assert.equal(gets, 1);
assert.equal(pending.length, 1);
pending.shift()({});
await actions;
await Promise.resolve();
assert.deepEqual([...timers.values()].map((t) => t.ms), [7000]);
const tick = fire(7000);
await dissolve.click();
const action = fire(1200);
assert.equal(gets, 2);
assert.equal(pending.length, 1);
pending.shift()({});
await Promise.all([tick, action]);
assert.deepEqual([...timers.values()].map((t) => t.ms), [7000]);
console.log(JSON.stringify({ ok: true }));
