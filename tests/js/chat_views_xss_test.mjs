// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Renders hostile conversation history through the real chat views.js and
// reports every way the hostile text escaped text nodes. Transcripts can
// carry outside text (the Gmail tool feeds email bodies to the model; see
// jasper/tools/gmail.py), and the chat page also carries the control token.
//
// Usage: node chat_views_xss_test.mjs <views.js>

import { pathToFileURL } from "node:url";
import { element, FakeElement } from "./_dom.mjs";
import { loadEsm, repoPath } from "./_loader.mjs";

const viewsPath = process.argv[2];
if (!viewsPath) throw new Error("usage: node chat_views_xss_test.mjs <views.js>");

class Node {}
class Text extends Node {
  constructor(data) { super(); this.data = data; }
}
Object.setPrototypeOf(FakeElement.prototype, Node.prototype);
const asNode = (child) => (child instanceof Node ? child : new Text(String(child)));
const texts = (node) => (node instanceof Text ? [node.data] : node.children.flatMap(texts));

let created = [];
const sinks = [];
function sink(name, el) {
  // Recorded as well as thrown: views.js's renderSection catches a throwing
  // render and shows a fallback note instead.
  sinks.push(`${name} on <${el.tag}>`);
  throw new Error(`HTML-string sink ${name} on <${el.tag}>`);
}

class StrictElement extends FakeElement {
  constructor(tag) { super(tag); created.push(this); }
  get textContent() { return texts(this).join(""); }
  set textContent(value) { this.children = value === "" ? [] : [new Text(String(value))]; }
  append(...children) { this.children.push(...children.map(asNode)); }
  replaceChildren(...children) { this.children = children.map(asNode); }
  set innerHTML(_) { sink("innerHTML", this); }
  set outerHTML(_) { sink("outerHTML", this); }
  insertAdjacentHTML() { sink("insertAdjacentHTML", this); }
}

globalThis.Node = Node;
globalThis.document = {
  body: Object.assign(new StrictElement("body"), { classList: element("body").classList }),
  createElement: (tag) => new StrictElement(tag),
  createElementNS: (_ns, tag) => new StrictElement(tag),
  createTextNode: (data) => new Text(String(data)),
};

const domUrl = pathToFileURL(repoPath("deploy/assets/shared/js/dom.js")).href;
const realDom = [/"\/assets\/shared\/js\/dom\.js"/, `"${domUrl}"`];
const shared = (name) => loadEsm(repoPath(`deploy/assets/shared/js/${name}.js`), { rewrite: [realDom] });
globalThis.__shared = { ...(await shared("chrome")), ...(await shared("ui")) };
const { buildPage, sinceToDateValue, update, updateError } = await loadEsm(viewsPath, {
  rewrite: [
    realDom,
    // chrome.js and ui.js import dom.js by absolute path, which a file: URL
    // cannot resolve, so they are loaded above and handed over here.
    [/^import (\{[^}]*\}) from "\/assets\/shared\/js\/(?:chrome|ui)\.js";$/gm, "const $1 = globalThis.__shared;"],
  ],
});

const PAYLOADS = [
  (label) => `<img src=x onerror=alert('${label}')>`,
  (label) => `<script>alert('${label}')</script>`,
  (label) => `<svg onload=alert('${label}')>`,
  (label) => `javascript:alert('${label}')`,
  (label) => `"'><a href="javascript:alert('${label}')">&amp;&lt;${label}</a>`,
];
// Every payload carries alert( or markup; the page's own attributes never do.
const tainted = (value) => /javascript:|alert\(|[<>]/i.test(value);
const TURNS = [0, 1];
// What the page shows; every other field must just never reach markup.
const SHOWN = ["since", ...TURNS.flatMap((i) =>
  ["user_text", "assistant_text", "provider", "ts_utc", "tool", "tool_name"].map((field) => `${field}#${i}`))];

// One page load as main.js drives it: build, a data.json poll, then a failed poll.
function run(text) {
  created = [];
  const root = new StrictElement("div");
  const since = text("since");
  const refs = buildPage(root, {}, { initialDate: sinceToDateValue(since) });
  update(refs, {
    capture_enabled: true,
    available: true,
    limit: 50,
    since: text("echoed_since"),
    retention: text("retention"),
    stats: { turn_count: TURNS.length, last_write_ts_utc: text("last_write_ts_utc") },
    turns: TURNS.map((i) => ({
      id: text(`id#${i}`),
      ts_utc: text(`ts_utc#${i}`),
      provider: text(`provider#${i}`),
      user_text: text(`user_text#${i}`),
      assistant_text: text(`assistant_text#${i}`),
      tool_calls_json: text(`tool_calls_json#${i}`),
      data_json: JSON.stringify({
        kind: "voice_turn",
        tools: [text(`tool#${i}`), { name: text(`tool_name#${i}`) }],
      }),
      session_id: text(`session_id#${i}`),
    })),
  }, { since });
  const shown = texts(root);
  updateError(refs, new Error(text("error")), { since });
  return { created, shown: [...shown, ...texts(root)] };
}

const shape = (el) => `<${el.tag}>[${Object.keys(el.attributes).sort()}]`;
const benign = run((label) => `plain ${label}`).created.map(shape);
const attributes = (el) => [
  ...Object.entries(el.attributes),
  ...Object.entries(el.dataset).map(([key, value]) => [`data-${key}`, value]),
  ...Object.entries(el).filter(([key, value]) => typeof value === "string" && !["tag", "_textContent"].includes(key)),
];

const verdict = {
  html_sinks: sinks,
  elements_from_payload: [],
  handler_attributes: [],
  tainted_attributes: [],
  payloads_missing_from_text: [],
};
for (const payload of PAYLOADS) {
  const hostile = run(payload);

  const shapes = hostile.created.map(shape);
  const at = shapes.findIndex((s, i) => s !== benign[i]);
  if (at >= 0 || shapes.length !== benign.length) {
    const i = at >= 0 ? at : shapes.length;
    verdict.elements_from_payload.push(`${shapes[i] ?? "nothing"} where benign text made ${benign[i] ?? "nothing"}`);
  }

  for (const el of hostile.created) {
    for (const [name, value] of attributes(el)) {
      if (/^on/i.test(name)) verdict.handler_attributes.push(`<${el.tag}>[${name}]`);
      // <time datetime> is set through the DOM, never parsed or navigated.
      if (el.tag === "time" && name.toLowerCase() === "datetime") continue;
      if (tainted(String(value))) verdict.tainted_attributes.push(`<${el.tag}>[${name}]=${value}`);
    }
  }

  for (const label of SHOWN) {
    const expected = payload(label);
    if (!hostile.shown.some((data) => data.includes(expected))) {
      verdict.payloads_missing_from_text.push(expected);
    }
  }
}

console.log(JSON.stringify(verdict));
