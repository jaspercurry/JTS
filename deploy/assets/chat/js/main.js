// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// main.js — /chat/ dashboard entry point.
//
// Reads the CSRF meta tag like the other migrated pages, fetches data.json via
// the shared HTTP helper, and self-schedules polling without overlapping
// requests. Rendering lives in views.js and uses text nodes only.

import { jtsAlert, jtsConfirm } from "/assets/shared/js/dialog.js";
import { getJSON, postJSON } from "./api.js";
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
  csrfToken: readCsrfToken(),
  since: normalizeSince(new URLSearchParams(window.location.search).get("since")),
  lastError: "",
};

let pollTimer = null;
let loading = false;
let refreshPending = false;
let refs = null;

const handlers = {
  applyFilter(value) {
    state.since = dateValueToSince(value);
    syncUrl();
    refreshSoon();
  },
  clearFilter() {
    state.since = "";
    syncUrl();
    refreshSoon();
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
      refreshSoon();
    } catch (err) {
      await jtsAlert(errorMessage(err), { title: "Conversation history" });
      refreshSoon();
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
      refreshSoon();
    } catch (err) {
      await jtsAlert(errorMessage(err), { title: "Conversation history" });
    } finally {
      setBusy(false);
    }
  },
};

refs = buildPage(root, handlers, {
  csrfPresent: !!state.csrfToken,
  initialDate: sinceToDateValue(state.since),
});

refreshSoon();

function readCsrfToken() {
  const meta = document.querySelector('meta[name="jts-csrf"]');
  return meta ? meta.content : "";
}

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

function refreshSoon() {
  if (pollTimer !== null) window.clearTimeout(pollTimer);
  pollTimer = window.setTimeout(refresh, 0);
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
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer);
    pollTimer = null;
  }
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
      refreshSoon();
    } else {
      pollTimer = window.setTimeout(refresh, POLL_MS);
    }
  }
}
