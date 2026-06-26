// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Pair-balance save state for /rooms/.
//
// The DOM owns rendering and availability; this module owns the async contract:
// drag changes are saved live, overlapping drags queue one latest save, and any
// final failed save restores the last backend-confirmed value instead of
// waiting for a future poll to snap the slider back.

import {
  balanceTrimRequest,
  clampBalanceDb,
} from "./grouping-view.js";

function defaultDescribeFailure(error) {
  return error && error.message ? error.message : String(error || "unknown error");
}

function rejectedResponseError(data) {
  const err = new Error((data && data.error) || "server rejected balance");
  err.body = data;
  return err;
}

export function createPairBalanceController(options) {
  const opts = options || {};
  const readValue = opts.readValue;
  const reflectBalance = opts.reflectBalance;
  const postTrim = opts.postTrim;
  const setStatus = opts.setStatus || (() => {});
  const applyMessage = opts.applyMessage || (() => "Applied.");
  const describeFailure = opts.describeFailure || defaultDescribeFailure;
  const logError = opts.logError || (() => {});
  const timers = opts.timers || globalThis;
  const commitDelayMs = Number.isFinite(opts.commitDelayMs)
    ? Number(opts.commitDelayMs)
    : 150;

  if (typeof readValue !== "function") {
    throw new TypeError("readValue callback is required");
  }
  if (typeof reflectBalance !== "function") {
    throw new TypeError("reflectBalance callback is required");
  }
  if (typeof postTrim !== "function") {
    throw new TypeError("postTrim callback is required");
  }
  if (
    typeof timers.setTimeout !== "function" ||
    typeof timers.clearTimeout !== "function"
  ) {
    throw new TypeError("timers must provide setTimeout and clearTimeout");
  }

  let saving = false;
  let dirty = false;
  let queued = false;
  let timer = null;
  let confirmedDb = clampBalanceDb(opts.initialDb || 0);

  function clearTimer() {
    if (timer !== null) {
      timers.clearTimeout(timer);
      timer = null;
    }
  }

  function markConfirmed(value) {
    confirmedDb = clampBalanceDb(value);
    return confirmedDb;
  }

  function reflectConfirmed() {
    reflectBalance(confirmedDb);
  }

  function scheduleCommit(delayMs = commitDelayMs) {
    dirty = true;
    queued = true;
    if (saving) return;
    clearTimer();
    timer = timers.setTimeout(() => {
      timer = null;
      void commit();
    }, delayMs);
  }

  async function commit() {
    clearTimer();
    queued = true;
    if (saving) return;

    const request = balanceTrimRequest(readValue());
    const requestedDb = request.balance_db;
    queued = false;
    dirty = true;
    saving = true;
    setStatus("Applying...");
    try {
      const data = await postTrim(request);
      if (!data || data.ok === false) {
        throw rejectedResponseError(data);
      }
      const balance = data && data.balance;
      const appliedDb = balance && typeof balance.balance_db === "number"
        ? balance.balance_db
        : requestedDb;
      markConfirmed(appliedDb);
      if (!queued) {
        reflectConfirmed();
        dirty = false;
        setStatus(applyMessage(data, balance));
      }
    } catch (error) {
      logError(error);
      if (!queued) {
        reflectConfirmed();
        dirty = false;
        setStatus(
          "Couldn't apply balance; restored saved value - "
          + describeFailure(error),
        );
      }
    } finally {
      saving = false;
      if (queued) {
        return commit();
      }
    }
  }

  function input(value) {
    reflectBalance(value);
    setStatus("");
    scheduleCommit();
  }

  function change() {
    return commit();
  }

  function reset() {
    reflectBalance(0);
    return commit();
  }

  function syncConfirmed(value) {
    if (typeof value !== "number") return false;
    markConfirmed(value);
    if (!saving && !dirty) {
      reflectConfirmed();
      setStatus("");
      return true;
    }
    return false;
  }

  function snapshot() {
    return {
      confirmedDb,
      dirty,
      queued,
      saving,
      timerActive: timer !== null,
    };
  }

  return {
    change,
    commit,
    input,
    reset,
    scheduleCommit,
    snapshot,
    syncConfirmed,
  };
}
