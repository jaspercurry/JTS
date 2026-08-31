// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import { loadCapturePage } from "./_capture_page_module.mjs";
import { runTestFunctions } from "./run_test_functions.mjs";

const PAGE_IDENTITY = JSON.parse(
  await readFile(new URL("../../capture-page/version.json", import.meta.url), "utf8"),
);

function makeNode(tag) {
  const node = {
    tagName: String(tag).toUpperCase(),
    nodeType: 1,
    className: "",
    parentNode: null,
    children: [],
    dataset: {},
    _attrs: {},
    _listeners: {},
    style: { setProperty() {} },
    appendChild(child) {
      if (child && typeof child === "object") child.parentNode = this;
      this.children.push(child);
      return child;
    },
    append(...items) {
      for (const item of items) this.appendChild(item);
    },
    replaceChildren(...items) {
      for (const child of this.children) {
        if (child && typeof child === "object") child.parentNode = null;
      }
      this.children = [];
      for (const item of items) this.appendChild(item);
    },
    insertBefore(child, before) {
      if (child && typeof child === "object") child.parentNode = this;
      const index = before ? this.children.indexOf(before) : -1;
      if (index < 0) this.children.push(child);
      else this.children.splice(index, 0, child);
      return child;
    },
    remove() {
      if (!this.parentNode) return;
      const siblings = this.parentNode.children;
      const index = siblings.indexOf(this);
      if (index >= 0) siblings.splice(index, 1);
      this.parentNode = null;
    },
    setAttribute(key, value) {
      this._attrs[String(key)] = String(value);
    },
    getAttribute(key) {
      return Object.prototype.hasOwnProperty.call(this._attrs, key)
        ? this._attrs[key]
        : null;
    },
    addEventListener(event, listener) {
      (this._listeners[event] = this._listeners[event] || []).push(listener);
    },
  };
  let text = "";
  Object.defineProperty(node, "textContent", {
    get() {
      return text;
    },
    set(value) {
      text = String(value);
      node.children.length = 0;
    },
  });
  if (node.tagName === "SELECT") {
    let selected = "";
    Object.defineProperty(node, "value", {
      get() {
        return selected;
      },
      set(value) {
        const wanted = String(value);
        selected = node.children.some((child) => child.value === wanted) ? wanted : "";
      },
    });
  }
  return node;
}

function installGlobal(name, value) {
  Object.defineProperty(globalThis, name, {
    configurable: true,
    writable: true,
    value,
  });
}

async function makeBoot({
  pageIdentity = PAGE_IDENTITY,
  protocolVersion = PAGE_IDENTITY.capture_protocol_version,
  rememberedDevice = "",
  devices = [],
} = {}) {
  const screen = makeNode("div");
  const status = makeNode("p");
  const wakeLockHint = makeNode("p");
  const body = makeNode("main");
  body.appendChild(screen);
  const elements = new Map([
    ["screen", screen],
    ["status", status],
    ["wakelock-hint", wakeLockHint],
  ]);
  const storage = new Map([["jts.capture.selected-device", rememberedDevice]]);
  const writes = [];
  const identities = [];
  const requests = [];
  let renderCount = 0;

  installGlobal("document", {
    createElement: (tag) => makeNode(tag),
    createTextNode: (text) => ({ nodeType: 3, textContent: String(text) }),
    getElementById: (id) => elements.get(id) || null,
  });
  delete globalThis.window;
  installGlobal("location", { hash: "#capture" });
  installGlobal("localStorage", {
    getItem: (key) => storage.get(key) || null,
    setItem(key, value) {
      writes.push([key, String(value)]);
      storage.set(key, String(value));
    },
  });
  installGlobal("navigator", {
    mediaDevices: {
      async enumerateDevices() {
        return devices;
      },
    },
  });
  installGlobal("fetch", async (url) => {
    requests.push(String(url));
    return { ok: true, async json() { return pageIdentity; } };
  });

  class RelayClient {
    async fetchSpecText() {
      return "signed capture spec";
    }
    setCapturePageIdentity(identity) {
      identities.push(identity);
    }
    setTransportIntegrity() {}
  }

  const spec = {
    kind: "crossover_sweep",
    capture_protocol_version: protocolVersion,
  };
  const capturePage = await loadCapturePage({
    dependencies: {
      parseFragment: () => ({
        sessionId: "capture-test",
        uploadToken: "upload-test",
        contentKeyB64: "content-test",
        specMac: "mac-test",
      }),
      RelayClient,
      verifyAndParseCaptureSpec: async () => ({ spec, integrity: {} }),
      renderScreen: () => {
        renderCount += 1;
        return {};
      },
    },
  });

  return {
    body,
    capturePage,
    identities,
    requests,
    screen,
    status,
    storage,
    writes,
    rendered: () => renderCount,
  };
}

async function flushPicker() {
  await new Promise((resolve) => setImmediate(resolve));
}

function micPickers(body) {
  return body.children.filter((node) => node.className === "mic-picker");
}

let passed = 0;
function ok() {
  passed += 1;
}

async function testBootUsesPublishedProtocolIdentity() {
  const env = await makeBoot();
  await env.capturePage.boot();
  assert.deepEqual(env.identities, [PAGE_IDENTITY]);
  assert.equal(env.rendered(), 1);
  assert.equal(env.requests.length, 1);
  assert.match(env.requests[0], /\/version\.json$/);
  ok();
}

async function testBootRejectsProtocolOutsidePublishedIdentity() {
  const env = await makeBoot({
    protocolVersion: PAGE_IDENTITY.capture_protocol_version + 1,
  });
  await env.capturePage.boot();
  assert.equal(env.rendered(), 0);
  assert.deepEqual(env.identities, []);
  assert.equal(env.status.dataset.kind, "error");
  assert.match(env.status.textContent, /incompatible/);
  ok();
}

async function testMissingRememberedMicFallsBackWithoutErasingStorage() {
  const env = await makeBoot({
    rememberedDevice: "remembered-usb",
    devices: [
      { kind: "audioinput", label: "Built-in", deviceId: "builtin" },
      { kind: "audioinput", label: "Current USB", deviceId: "current-usb" },
    ],
  });
  await env.capturePage.boot();
  await flushPicker();
  const [picker] = micPickers(env.body);
  assert.ok(picker);
  const select = picker.children.find((node) => node.tagName === "SELECT");
  assert.equal(select.value, "");
  assert.deepEqual(env.writes, []);
  assert.equal(env.storage.get("jts.capture.selected-device"), "remembered-usb");
  ok();
}

async function testSecondBootReplacesTheExistingMicPicker() {
  const env = await makeBoot({
    devices: [
      { kind: "audioinput", label: "Built-in", deviceId: "builtin" },
      { kind: "audioinput", label: "USB", deviceId: "usb" },
    ],
  });
  await env.capturePage.boot();
  await flushPicker();
  assert.equal(micPickers(env.body).length, 1);
  await env.capturePage.boot();
  await flushPicker();
  assert.equal(micPickers(env.body).length, 1);
  ok();
}

await runTestFunctions([
  testBootUsesPublishedProtocolIdentity,
  testBootRejectsProtocolOutsidePublishedIdentity,
  testMissingRememberedMicFallsBackWithoutErasingStorage,
  testSecondBootReplacesTheExistingMicPicker,
], () => passed);
