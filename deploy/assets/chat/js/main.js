// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// main.js — /chat/ dashboard entry point.
//
// Reads the CSRF meta tag like the other migrated pages and fetches data.json
// via the shared HTTP helper, on the shared poller. Rendering lives in views.js
// and uses text nodes only.

import { jtsAlert, jtsConfirm } from "/assets/shared/js/dialog.js";
import { getJSON, postJSON, startPolling } from "./api.js";
import {
  buildPage,
  dateValueToSince,
  normalizeSince,
  sinceToDateValue,
  update,
  updateError,
} from "./views.js";

const POLL_MS = 10000;
const root = document.getElementById("app");

const state = {
  since: normalizeSince(new URLSearchParams(window.location.search).get("since")),
  lastError: "",
};

let loading = false;
let refreshPending = false;
let refs = null;

const handlers = {
  applyFilter(value) {
    state.since = dateValueToSince(value);
    syncUrl();
    refresh();
  },
  clearFilter() {
    state.since = "";
    syncUrl();
    refresh();
  },
  async showErrorDetails() {
    await jtsAlert(state.lastError || "No error details are available.", {
      title: "Conversation history",
    });
  },
  async setCapture(enabled) {
    setBusy(true);
    try {
      await postJSON("capture", { enabled: !!enabled });
      refresh();
    } catch (err) {
      await jtsAlert(errorMessage(err), { title: "Conversation history" });
      refresh();
    } finally {
      setBusy(false);
    }
  },
  async clearHistory() {
    const ok = await jtsConfirm(
      "Clear all saved conversation turns from this speaker?",
      { title: "Clear conversation history", danger: true },
    );
    if (!ok) return;
    setBusy(true);
    try {
      await postJSON("clear", {});
      refresh();
    } catch (err) {
      await jtsAlert(errorMessage(err), { title: "Conversation history" });
    } finally {
      setBusy(false);
    }
  },
};

refs = buildPage(root, handlers, {
  initialDate: sinceToDateValue(state.since),
});

startPolling(refresh, { intervalMs: POLL_MS });

function dataPath() {
  const params = new URLSearchParams();
  if (state.since) params.set("since", state.since);
  const query = params.toString();
  return query ? `data.json?${query}` : "data.json";
}

function syncUrl() {
  const url = new URL(window.location.href);
  if (state.since) url.searchParams.set("since", state.since);
  else url.searchParams.delete("since");
  window.history.replaceState(null, "", url);
}

function setBusy(value) {
  if (!refs) return;
  if (refs.captureToggle) refs.captureToggle.disabled = !!value;
  if (refs.clearButton) refs.clearButton.disabled = !!value;
}

function errorMessage(err) {
  if (err && err.message) return err.message;
  return String(err || "Request failed.");
}

async function refresh() {
  if (loading) {
    refreshPending = true;
    return;
  }
  loading = true;
  const requestedPath = dataPath();
  try {
    const payload = await getJSON(requestedPath);
    if (requestedPath !== dataPath()) {
      refreshPending = true;
      return;
    }
    state.lastError = "";
    update(refs, payload, state);
  } catch (err) {
    state.lastError = err && err.message ? err.message : String(err);
    updateError(refs, err, state);
  } finally {
    loading = false;
    if (refreshPending) {
      refreshPending = false;
      return refresh();
    }
  }
}
