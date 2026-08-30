// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import { loadEsm } from "./_loader.mjs";

const { toggleScanRequest } = await loadEsm(process.argv[2]);
const { deviceActionDisabled } = await loadEsm(process.argv[3], {
  stripImports: true,
  guardNoImports: true,
  truncateBefore: "// -------- pair flow --------",
  exportNames: ["deviceActionDisabled"],
});

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function harness(overrides = {}) {
  const intents = [];
  const alerts = [];
  const posts = [];
  const deferred = [];
  let renders = 0;
  let refreshes = 0;
  const config = {
    discovering: false,
    setIntentUntil(value) { intents.push(value); },
    render() { renders += 1; },
    async postScan(action) { posts.push(action); },
    async refreshState() { refreshes += 1; },
    async showAlert(message) { alerts.push(message); },
    now: () => 1000,
    defer(callback, delay) { deferred.push({ callback, delay }); },
    ...overrides,
  };
  return {
    config,
    intents,
    alerts,
    posts,
    deferred,
    get renders() { return renders; },
    get refreshes() { return refreshes; },
  };
}

{
  const h = harness();
  const ok = await toggleScanRequest(h.config);
  assert(ok === true, "successful start should report success");
  assert(JSON.stringify(h.intents) === JSON.stringify([4000]),
    "start should set the bounded optimistic intent");
  assert(JSON.stringify(h.posts) === JSON.stringify(["start"]),
    "start action drifted");
  assert(h.renders === 1, "start should render optimistic state once");
  assert(h.deferred.length === 1 && h.deferred[0].delay === 200,
    "success should schedule the state refresh");
  await h.deferred[0].callback();
  assert(h.refreshes === 1, "deferred success refresh did not run");
}

{
  const error = new Error("controller I/O failure");
  error.status = 502;
  const h = harness({
    async postScan(action) {
      h.posts.push(action);
      throw error;
    },
  });
  const ok = await toggleScanRequest(h.config);
  assert(ok === false, "HTTP failure should report failure");
  assert(JSON.stringify(h.intents) === JSON.stringify([4000, 0]),
    "HTTP failure should clear optimistic intent");
  assert(h.renders === 2, "HTTP failure should render restored state");
  assert(h.alerts[0] === "Bluetooth scan failed: controller I/O failure",
    "HTTP failure detail was not surfaced");
  assert(h.refreshes === 1 && h.deferred.length === 0,
    "HTTP failure should refresh immediately without a delayed refresh");
}

{
  const h = harness({
    discovering: true,
    async postScan(action) {
      h.posts.push(action);
      throw new TypeError("offline");
    },
  });
  const ok = await toggleScanRequest(h.config);
  assert(ok === false, "network failure should report failure");
  assert(JSON.stringify(h.posts) === JSON.stringify(["stop"]),
    "discovering state should request stop");
  assert(JSON.stringify(h.intents) === JSON.stringify([0, 0]),
    "network failure should leave no optimistic intent");
  assert(h.alerts[0] === "Network error talking to the Bluetooth backend.",
    "network failure copy drifted");
  assert(h.refreshes === 1, "network failure should refresh current state");
}

{
  const poweredOff = {
    powered: false, desired: false, available: true, parked: false,
  };
  for (const action of ["disconnect", "forget", "connect", "pair"]) {
    assert(deviceActionDisabled(action, poweredOff, false),
      `${action} must be disabled while adapter power is known off`);
  }

  const ready = {
    powered: true, desired: true, available: true, parked: false,
  };
  for (const action of ["disconnect", "forget", "connect", "pair"]) {
    assert(!deviceActionDisabled(action, ready, false),
      `${action} must be enabled when the adapter is ready`);
  }

  const degradedButPowered = {
    powered: true, desired: true, available: false, parked: false,
  };
  assert(!deviceActionDisabled("disconnect", degradedButPowered, false),
    "generic unavailability must not trap disconnect cleanup");
  assert(!deviceActionDisabled("forget", degradedButPowered, false),
    "generic unavailability must not trap forget cleanup");
  assert(deviceActionDisabled("connect", degradedButPowered, false),
    "generic unavailability must still block connect activation");
  assert(deviceActionDisabled("pair", degradedButPowered, false),
    "generic unavailability must still block pair activation");

  const startupState = {
    powered: null, desired: false, available: true, parked: false,
  };
  assert(!deviceActionDisabled("forget", startupState, false),
    "startup must keep cleanup available until adapter power is known");
  assert(deviceActionDisabled("forget", degradedButPowered, true),
    "an active mutation must block every device action");
}

console.log(JSON.stringify({ ok: true }));
