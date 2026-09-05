// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Optional software belongs behind one progressive-disclosure boundary on the
// Software card. This module owns that boundary and the enhanced-AEC lifecycle
// UI; views.js only mounts it. The backend remains the single source of truth
// for state, and both GET/POST return the same versioned representation.

import { h } from "/assets/shared/js/dom.js";
import { getJSON, postJSON } from "/assets/shared/js/http.js";
import { jtsConfirm } from "/assets/shared/js/dialog.js";
import { actionButton, badge, collapsible, defList } from "./components.js";

const STATUS_PATH = "/system/optional-features/enhanced-aec";
const INSTALL_PATH = STATUS_PATH + "/install";
const INSTALL_POLL_MS = 2500;

const SERVER_STATES = new Set([
  "not_installed", "installing", "installed", "stale", "failed",
  "not_needed", "unavailable",
]);

function text(value) {
  return typeof value === "string" ? value.trim() : "";
}

// Keep all product language for the backend state machine in one pure
// projection. The runtime harness exercises every state without a browser;
// render() below is deliberately just DOM composition.
export function enhancedAecPresentation(payload) {
  payload = payload && typeof payload === "object" ? payload : {};
  let state = payload.schema_version === 1 ? text(payload.state) : "unavailable";
  if (!SERVER_STATES.has(state)) state = "unavailable";
  const action = payload.action && typeof payload.action === "object"
    ? payload.action : {};
  // A missing/malformed action is not permission to start an install. The
  // versioned backend must opt in explicitly.
  const actionEnabled = action.enabled === true;
  const serverDetail = text(payload.detail) || text(payload.summary);

  const common = {
    state,
    badge: "Unavailable",
    tone: "idle",
    detail: serverDetail,
    button: "",
    installable: false,
    polling: false,
  };

  if (state === "not_installed") {
    return {
      ...common,
      badge: "Optional",
      detail: common.detail ||
        "Standard echo cancellation is already working. Install this only " +
        "for deeper tuning in difficult rooms or microphone experiments.",
      button: text(action.label) || "Install enhancement",
      installable: actionEnabled,
    };
  }
  if (state === "installing") {
    return {
      ...common,
      badge: "Installing",
      tone: "warn",
      detail: common.detail ||
        "Installing in the background. Music, measurements, and room " +
        "correction remain available.",
      button: text(action.label) || "Installing…",
      polling: true,
    };
  }
  if (state === "installed") {
    return {
      ...common,
      badge: "Installed",
      tone: "ok",
      detail: common.detail ||
        "The enhanced engine is available for advanced echo-cancellation tuning.",
    };
  }
  if (state === "stale") {
    return {
      ...common,
      badge: "Update available",
      tone: "warn",
      detail: common.detail ||
        "The installed enhancement was built for an older JTS release. " +
        "Standard echo cancellation remains available.",
      button: text(action.label) || "Update enhancement",
      installable: actionEnabled,
    };
  }
  if (state === "failed") {
    return {
      ...common,
      badge: "Install failed",
      tone: "danger",
      detail: common.detail ||
        "Standard echo cancellation is still available. You can retry when convenient.",
      button: text(action.label) || "Retry installation",
      installable: actionEnabled,
    };
  }
  if (state === "not_needed") {
    return {
      ...common,
      badge: "Not needed",
      tone: "ok",
      detail: common.detail ||
        "This speaker is using hardware echo cancellation, so the optional " +
        "software engine would not improve this setup.",
    };
  }
  return {
    ...common,
    detail: common.detail ||
      "This enhancement is not available for the current software or hardware.",
  };
}

function technicalDetails(payload, state) {
  payload = payload && typeof payload === "object" ? payload : {};
  const engine = text(payload.engine);
  const verifiedEnhancement = engine === "v2"
    ? "WebRTC AEC3 v2"
    : (engine === "chip" ? "Not needed (hardware AEC)" : "Not installed");
  const rows = [
    ["Feature", "WebRTC AEC3 v2 / BEST_A"],
    ["Install state", state],
    // `engine` describes verified capability availability, not the engine
    // currently running inside a live bridge process. Keep that distinction
    // explicit: active-engine truth belongs to bridge telemetry.
    ["Verified enhancement", verifiedEnhancement],
  ];
  if (text(payload.installed_fingerprint)) {
    rows.push(["Installed build", text(payload.installed_fingerprint)]);
  }
  if (text(payload.desired_fingerprint)) {
    rows.push(["Current JTS build", text(payload.desired_fingerprint)]);
  }
  if (text(payload.last_error)) {
    // Compiler/download diagnostics are useful to an operator, but they do
    // not belong in the ordinary product-language summary.
    rows.push(["Last install error", text(payload.last_error)]);
  }
  return h("details.optional-feature__technical", null,
    h("summary", null, "Technical details"),
    defList(rows),
  );
}

export function buildEnhancedAecCard() {
  const status = h("span");
  const detail = h("p.optional-feature__detail");
  const action = actionButton("Loading…", { variant: "default" });
  const actionRow = h("div.form-actions", null, action);
  const technical = h("div");
  const live = h("p.sr-only", {
    "attr:role": "status",
    "attr:aria-live": "polite",
  });

  let open = false;
  let latest = {};
  let pollTimer = null;
  let requestInFlight = false;

  function stopPolling() {
    if (pollTimer !== null) clearTimeout(pollTimer);
    pollTimer = null;
  }

  function schedulePoll(view) {
    stopPolling();
    if (!open || !view.polling) return;
    pollTimer = setTimeout(() => refresh(), INSTALL_POLL_MS);
  }

  function render(payload) {
    latest = payload && typeof payload === "object" ? payload : {};
    const view = enhancedAecPresentation(latest);
    status.replaceChildren(badge(view.badge, view.tone));
    detail.textContent = view.detail;
    action.textContent = view.button;
    action.hidden = !view.button;
    action.disabled = requestInFlight || !view.installable;
    actionRow.hidden = !view.button;
    technical.replaceChildren(technicalDetails(latest, view.state));
    schedulePoll(view);
    return view;
  }

  async function refresh() {
    if (requestInFlight) return;
    requestInFlight = true;
    action.disabled = true;
    try {
      render(await getJSON(STATUS_PATH));
    } catch (error) {
      console.error("system: enhanced AEC status failed", error);
      render({
        schema_version: 1,
        state: "unavailable",
        detail: "Couldn’t load the enhancement status. Try reopening this section.",
      });
    } finally {
      requestInFlight = false;
      // render() ran while the request flag was true. Reconcile the button
      // once more without manufacturing another backend state.
      render(latest);
    }
  }

  action.onclick = async () => {
    const view = enhancedAecPresentation(latest);
    if (!view.installable || requestInFlight) return;
    const confirmed = await jtsConfirm(
      "Install enhanced echo cancellation? This can take several minutes, " +
      "but music, measurements, and room correction will keep working.",
      { title: "Install optional enhancement", okLabel: "Install" },
    );
    if (!confirmed) return;

    requestInFlight = true;
    action.disabled = true;
    action.textContent = "Starting…";
    live.textContent = "Starting enhanced echo cancellation installation.";
    try {
      const next = await postJSON(INSTALL_PATH, {});
      const nextView = render(next);
      live.textContent = nextView.detail;
    } catch (error) {
      console.error("system: enhanced AEC install failed", error);
      const failed = render({
        schema_version: 1,
        state: "failed",
        last_error: error.message ||
          "The installation could not be started. Standard echo cancellation is unchanged.",
        engine: latest.engine,
        action: { enabled: true, label: "Retry installation" },
      });
      live.textContent = failed.detail;
    } finally {
      requestInFlight = false;
      render(latest);
    }
  };

  const body = h("div.optional-feature", null,
    h("div.optional-feature__head", null,
      h("div", null,
        h("h3.optional-feature__title", null, "Enhanced echo cancellation"),
        h("p.optional-feature__summary", null,
          "Adds an enhanced software engine for unusually difficult rooms. " +
          "Most speakers don’t need it.")),
      status,
    ),
    detail,
    actionRow,
    technical,
    live,
  );

  const card = collapsible({
    title: "Optional features",
    open: false,
    body,
    onToggle(nextOpen) {
      open = nextOpen;
      if (open) refresh();
      else stopPolling();
    },
  });
  card.classList.add("optional-features");

  // Deliberately no eager request: ordinary users never open this disclosure,
  // so optional feature state should not add work to the dashboard hot path.
  render({
    schema_version: 1,
    state: "not_installed",
    action: { enabled: false, label: "Install enhancement" },
  });
  return card;
}
