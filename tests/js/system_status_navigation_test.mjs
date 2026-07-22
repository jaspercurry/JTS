// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Executes system-status/main.js against a deliberately tiny DOM seam. The
// view builders are mocked: this test owns navigation/lifecycle behaviour,
// while the renderers keep their existing Python/static coverage.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const modulePath = process.argv[2];
if (!modulePath) throw new Error("usage: node system_status_navigation_test.mjs <main.js>");

const source = readFileSync(modulePath, "utf8").replace(/^import .*;\n/gm, "");
assert.doesNotMatch(source, /^\s*import\s/m);

const root = {
  dataset: { view: "system" },
  children: [],
  attrs: {},
  replaceChildren(...children) { this.children = children; },
  append(child) { this.children.push(child); },
  setAttribute(name, value) { this.attrs[name] = value; },
};
const bodyClasses = new Set();
const document = {
  body: {
    classList: {
      add(name) { bodyClasses.add(name); },
      remove(name) { bodyClasses.delete(name); },
    },
  },
  getElementById(id) { return id === "app" ? root : null; },
};

const location = { pathname: "/system/" };
const historyCalls = [];
const history = {
  pushState(state, _title, path) {
    historyCalls.push({ state, path });
    location.pathname = path;
  },
};
const listeners = {};
const scrollCalls = [];
const window = {
  addEventListener(type, fn) { listeners[type] = fn; },
  scrollTo(...args) { scrollCalls.push(args); },
};

let onViewClick;
const activeLog = [];
function header(options) {
  onViewClick = options.onViewClick;
  return {
    el: { id: "header" },
    setActive(view, opts) { activeLog.push({ view, opts }); },
  };
}

const builds = { system: 0, audio: 0 };
const updates = { system: 0, audio: 0 };
const panels = {};
let audioUpdateThrows = false;
function build(view) {
  builds[view] += 1;
  const panel = { id: `${view}-panel`, hidden: false };
  panels[view] = panel;
  return {
    panel,
    refs: { staleness: { textContent: "" }, actionsStatus: {} },
  };
}
const buildSystemPanel = () => build("system");
const buildAudioPanel = () => build("audio");
function update() { updates.system += 1; }
function updateAudio() {
  updates.audio += 1;
  if (audioUpdateThrows) throw new Error("malformed optional audio slice");
}

let resolveSnapshot;
let fetchCalls = 0;
const snapshotPromise = new Promise((resolve) => { resolveSnapshot = resolve; });
function getJSON() { fetchCalls += 1; return snapshotPromise; }
const timers = [];
function setTimeout(fn, ms) { timers.push({ fn, ms }); return timers.length; }
const noop = () => {};
const quietConsole = { error: noop };

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const run = new AsyncFunction(
  "document", "window", "location", "history", "setTimeout", "console",
  "buildSystemPanel", "update", "buildAudioPanel", "updateAudio", "header",
  "getJSON", "postAction", "setQuality", "runDiagnostics",
  source,
);
await run(
  document, window, location, history, setTimeout, quietConsole,
  buildSystemPanel, update, buildAudioPanel, updateAudio, header,
  getJSON, noop, noop, noop,
);

assert.equal(builds.system, 1, "initial System panel is built once");
assert.equal(builds.audio, 0, "Audio stays lazy on a System deep link");
assert.equal(fetchCalls, 1, "one poll loop starts");
assert.equal(panels.system.hidden, false);

function click(modifiers = {}) {
  const event = {
    defaultPrevented: false,
    button: 0,
    metaKey: false,
    ctrlKey: false,
    shiftKey: false,
    altKey: false,
    preventDefault() { this.defaultPrevented = true; },
    ...modifiers,
  };
  onViewClick("audio", event);
  return event;
}

const modified = click({ ctrlKey: true });
assert.equal(modified.defaultPrevented, false, "modified click keeps native link behaviour");
assert.equal(builds.audio, 0);
assert.equal(historyCalls.length, 0);

resolveSnapshot({ audio_health: { sources: [] } });
await Promise.resolve();
await Promise.resolve();
assert.equal(updates.system, 1);
assert.equal(timers.length, 1, "poll schedules one successor only");

// A target renderer failure must not split the visible view from the URL.
audioUpdateThrows = true;
const ordinary = click();
assert.equal(ordinary.defaultPrevented, true);
assert.equal(builds.audio, 1);
assert.equal(panels.system.hidden, true);
assert.equal(panels.audio.hidden, false);
assert.equal(historyCalls.at(-1).path, "/system/audio/");
assert.equal(location.pathname, "/system/audio/");
assert.match(root.children.at(-1).id, /audio-panel/);

const historyAfterFirstAudio = historyCalls.length;
assert.equal(click().defaultPrevented, true, "active link does not reload the document");
assert.equal(historyCalls.length, historyAfterFirstAudio, "active link adds no history entry");
assert.equal(builds.audio, 1, "active panel is retained");

// Return to System through an ordinary link, then use browser Back/Forward
// semantics (popstate) without manufacturing another history entry.
const systemEvent = {
  defaultPrevented: false, button: 0,
  metaKey: false, ctrlKey: false, shiftKey: false, altKey: false,
  preventDefault() { this.defaultPrevented = true; },
};
onViewClick("system", systemEvent);
assert.equal(systemEvent.defaultPrevented, true);
assert.equal(builds.system, 1, "System panel identity survives a round trip");
assert.equal(panels.system.hidden, false);
const beforePop = historyCalls.length;
location.pathname = "/system/audio/";
listeners.popstate();
assert.equal(historyCalls.length, beforePop, "popstate never pushes history");
assert.equal(panels.audio.hidden, false);
assert.equal(panels.system.hidden, true);
assert.equal(activeLog.at(-1).view, "audio");
assert.equal(fetchCalls, 1, "view switching never starts another poller");
assert.equal(scrollCalls.length, 2, "explicit view clicks reset scroll; popstate does not");

process.stdout.write(JSON.stringify({ ok: true }));
