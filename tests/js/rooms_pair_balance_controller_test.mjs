// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import assert from "node:assert/strict";

import { createPairBalanceController } from "../../deploy/assets/rooms/js/pair-balance-controller.js";
import { clampBalanceDb } from "../../deploy/assets/rooms/js/grouping-view.js";

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

async function flushPromises() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

function makeTimers() {
  let nextId = 1;
  const tasks = [];
  return {
    setTimeout(fn, ms) {
      const task = { id: nextId++, fn, ms, active: true };
      tasks.push(task);
      return task.id;
    },
    clearTimeout(id) {
      for (const task of tasks) {
        if (task.id === id) task.active = false;
      }
    },
    runNext() {
      const task = tasks.find((item) => item.active);
      assert.ok(task, "expected an active timer");
      task.active = false;
      return task.fn();
    },
    activeCount() {
      return tasks.filter((task) => task.active).length;
    },
  };
}

function makeHarness() {
  let value = 0;
  const calls = [];
  const statuses = [];
  const reflected = [];
  const errors = [];
  const timers = makeTimers();
  const controller = createPairBalanceController({
    commitDelayMs: 150,
    timers,
    readValue: () => value,
    reflectBalance(db) {
      value = clampBalanceDb(db);
      reflected.push(value);
    },
    postTrim(request) {
      const pending = deferred();
      calls.push({ request, ...pending });
      return pending.promise;
    },
    setStatus(message) {
      statuses.push(message);
    },
    applyMessage() {
      return "Applied.";
    },
    describeFailure(error) {
      return error.message;
    },
    logError(error) {
      errors.push(error);
    },
  });
  return {
    calls,
    controller,
    errors,
    get value() {
      return value;
    },
    reflected,
    statuses,
    timers,
  };
}

// Dragging while a previous save is in flight keeps the thumb where the user
// put it, queues one latest-value save, and only reflects the final confirmed
// backend value.
{
  const h = makeHarness();
  h.controller.input(5);
  assert.equal(h.value, 5);
  assert.equal(h.timers.activeCount(), 1);

  h.timers.runNext();
  assert.equal(h.calls.length, 1);
  assert.deepEqual(h.calls[0].request, { target: "pair", balance_db: 5 });
  assert.equal(h.statuses.at(-1), "Applying...");

  h.controller.input(10);
  assert.equal(h.value, 10);
  assert.equal(h.calls.length, 1);

  h.calls[0].resolve({ ok: true, balance: { balance_db: 5 } });
  await flushPromises();
  assert.equal(h.calls.length, 2);
  assert.deepEqual(h.calls[1].request, { target: "pair", balance_db: 10 });
  assert.equal(h.value, 10);

  h.calls[1].resolve({ ok: true, balance: { balance_db: 10 } });
  await flushPromises();
  assert.equal(h.value, 10);
  assert.equal(h.statuses.at(-1), "Applied.");
  assert.deepEqual(h.controller.snapshot(), {
    confirmedDb: 10,
    dirty: false,
    queued: false,
    saving: false,
    timerActive: false,
  });
}

// A final failed save restores the last confirmed backend value immediately,
// instead of leaving the UI to snap back on a later poll.
{
  const h = makeHarness();
  assert.equal(h.controller.syncConfirmed(6), true);
  assert.equal(h.value, 6);

  h.controller.input(18);
  h.timers.runNext();
  assert.deepEqual(h.calls[0].request, { target: "pair", balance_db: 18 });
  h.calls[0].reject(new Error("peer offline"));
  await flushPromises();

  assert.equal(h.value, 6);
  assert.equal(h.errors.length, 1);
  assert.match(h.statuses.at(-1), /restored saved value/);
  assert.match(h.statuses.at(-1), /peer offline/);
  assert.equal(h.controller.snapshot().dirty, false);
}

// Poll reconciliation updates the rollback target without moving a dirty
// slider. If the user's pending write is rejected, the controller restores the
// newest backend-confirmed value from the poll.
{
  const h = makeHarness();
  h.controller.syncConfirmed(4);
  h.controller.input(12);
  assert.equal(h.controller.syncConfirmed(8), false);
  assert.equal(h.value, 12);

  h.timers.runNext();
  h.calls[0].resolve({ ok: false, error: "pair not ready" });
  await flushPromises();

  assert.equal(h.value, 8);
  assert.match(h.statuses.at(-1), /restored saved value/);
  assert.match(h.statuses.at(-1), /pair not ready/);
}

console.log(JSON.stringify({ ok: true }));
