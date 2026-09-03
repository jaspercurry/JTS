// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import { loadEsm } from "./_loader.mjs";

const { toggleScanRequest } = await loadEsm(process.argv[2]);
const {
  applyDeviceEvent,
  bindDeviceMutationStream,
  deviceActionDisabled,
  deviceMutationLabel,
  deviceMutationOutcome,
  deviceSection,
  recoverUnknownDeviceMutation,
  submitDeviceMutation,
  visibleDeviceRows,
} = await loadEsm(process.argv[3], {
  stripImports: true,
  guardNoImports: true,
  truncateBefore: "// -------- pair flow --------",
  exportNames: [
    "applyDeviceEvent",
    "bindDeviceMutationStream",
    "deviceActionDisabled",
    "deviceMutationLabel",
    "deviceMutationOutcome",
    "deviceSection",
    "recoverUnknownDeviceMutation",
    "submitDeviceMutation",
    "visibleDeviceRows",
  ],
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

  // Only forming a NEW bond needs the pairing agent. Reconnecting an
  // existing one does not, and cleanup must never be trapped behind it.
  const agentStopped = {
    powered: true, desired: true, available: true, parked: false,
    pairingReady: false,
  };
  assert(deviceActionDisabled("pair", agentStopped, false),
    "a stopped pairing agent must block pair");
  for (const action of ["connect", "disconnect", "forget"]) {
    assert(!deviceActionDisabled(action, agentStopped, false),
      `a stopped pairing agent must not block ${action}`);
  }

  // Absent verdict (parked, or a probe that could not read unit state) is
  // not evidence of a stopped agent -- it must not disable anything.
  const noVerdict = {
    powered: true, desired: true, available: true, parked: false,
  };
  assert(!deviceActionDisabled("pair", noVerdict, false),
    "a missing pairingReady verdict must not block pair");
}

{
  const updates = [];
  const terminal = [];
  const stream = {
    closed: false,
    close() { this.closed = true; },
  };
  const mutation = {stream: "actions/forget/device/stream"};
  bindDeviceMutationStream(mutation, {
    openStream(path) {
      assert(path === mutation.stream, "accepted stream path drifted");
      return stream;
    },
    onStatus(event) { updates.push(event.status); },
    onTerminal(event) { terminal.push(event.status); },
  });

  await stream.onmessage({data: JSON.stringify({status: "pending"})});
  stream.onerror();
  assert(stream.closed === false,
    "transport loss must leave the server-owned action resumable");
  assert(terminal.length === 0,
    "transport loss must not be treated as an action failure");

  await stream.onmessage({data: JSON.stringify({status: "succeeded"})});
  assert(stream.closed === true, "terminal state must close the progress stream");
  assert(JSON.stringify(updates) === JSON.stringify(["pending", "succeeded"]),
    "mutation status events drifted");
  assert(JSON.stringify(terminal) === JSON.stringify(["succeeded"]),
    "terminal action state was not delivered once");
  assert(deviceMutationLabel("forget", true) === "Removing…",
    "forget row must remain visibly pending");
  assert(deviceMutationOutcome("interrupted") === "unknown",
    "service restart must not be reported as device failure");
}

{
  const order = [];
  await recoverUnknownDeviceMutation({action: "connect"}, {
    refreshDevices() { order.push("devices"); },
    async drainState() { order.push("drain"); },
    async refreshState() { order.push("state"); },
    release() { order.push("release"); },
    notify() { order.push("notify"); },
  });
  assert(JSON.stringify(order)
    === JSON.stringify(["devices", "drain", "state", "release", "notify"]),
  "unknown outcome released controls before authoritative state refresh");
}

{
  const device = {path: "/device/1", address: "AA:BB:CC:DD:EE:FF"};
  const live = new Map([[device.path, device]]);
  assert(applyDeviceEvent({action: "reset"}, live),
    "stream reset event was ignored");
  assert(live.size === 0, "stream reconnect kept stale device rows");
  assert(applyDeviceEvent({action: "add", device}, live),
    "device seed event was ignored after reset");

  const pending = new Map([[device.address, {device}]]);
  live.clear();
  assert(visibleDeviceRows(live, pending)[0] === device,
    "observer removal hid the acted row before mutation terminal");
  pending.clear();
  assert(visibleDeviceRows(live, pending).length === 0,
    "terminal mutation kept the removed device snapshot");
}

{
  const mutation = {
    action: "connect",
    mac: "AA:BB:CC:DD:EE:FF",
    mutationId: "fixed-mutation-id",
  };
  let posts = 0;
  let accepted = 0;
  let rejected = 0;
  const options = {
    async post(current) {
      posts += 1;
      assert(current.mutationId === "fixed-mutation-id",
        "response-loss retry changed the idempotency key");
      throw new TypeError("response lost");
    },
    onAccepted() { accepted += 1; },
    onRejected() { rejected += 1; },
  };

  assert(await submitDeviceMutation(mutation, options) === "probing",
    "ambiguous response loss was treated as terminal");
  assert(accepted === 1 && rejected === 0 && posts === 1,
    "ambiguous response loss cleared or failed the pending action");
  assert(mutation.stream === "actions/fixed-mutation-id/stream",
    "response-loss recovery did not probe the accepted mutation id");
}

{
  const mutation = {
    action: "disconnect",
    mac: "AA:BB:CC:DD:EE:FF",
    mutationId: "new-action-id",
  };
  let accepted = 0;
  let rejected = 0;
  let rejection = '';
  const result = await submitDeviceMutation(mutation, {
    async post() {
      return {
        ok: false,
        status: 409,
        data: {
          code: "device_busy",
          status: "pending",
          action: "connect",
          mutationId: "active-action-id",
          stream: "actions/active-action-id/stream",
        },
      };
    },
    onAccepted() { accepted += 1; },
    onRejected(current, message) {
      rejected += 1;
      rejection = message;
      assert(current === mutation, "busy rejection replaced the requested action");
    },
  });
  assert(result === "rejected" && accepted === 0 && rejected === 1,
    "device_busy attached to a conflicting progress stream");
  assert(rejection === 409, "device_busy rejection did not preserve its status");
  assert(mutation.action === "disconnect"
    && mutation.mutationId === "new-action-id" && !mutation.stream,
  "busy rejection rewrote the requested action identity");
}

{
  const cases = [
    [{paired: true}, false, 'mine'],
    [{paired: false, rssi: -60}, false, 'nearby'],
    [{paired: false, connected: true}, false, 'nearby'],
    [{paired: false}, false, null],
    [{paired: false}, true, 'nearby'],
  ];
  for (const [device, pending, want] of cases) {
    assert(deviceSection(device, pending) === want,
      `deviceSection(${JSON.stringify(device)}, ${pending}) expected ${want}`);
  }
}

console.log(JSON.stringify({ ok: true }));
