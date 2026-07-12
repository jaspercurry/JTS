// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Shared staged-toggle and Apply behaviour for the tools catalog and detail
// pages. Rendering and catalog loading remain page-owned; this module owns the
// mutation protocol and its user-facing convergence/error states.

function endpoint(basePath, action) {
  return basePath.replace(/\/?$/, "/") + action;
}

export function createToolActions({
  basePath,
  statusEl,
  applyBtn,
  reload,
  postJSON,
  showAlert,
  sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
}) {
  async function onToggle(e) {
    const input = e.target;
    if (!input.matches(
      "input[type=checkbox][data-tool], input[type=checkbox][data-pack]",
    )) {
      return;
    }
    const isPack = !!input.dataset.pack;
    const key = isPack ? input.dataset.pack : input.dataset.tool;
    const enabled = input.checked;
    input.disabled = true;
    statusEl.textContent = (enabled ? "Enabling " : "Disabling ") + key + "…";
    try {
      await postJSON(endpoint(basePath, isPack ? "toggle-pack" : "toggle"), isPack
        ? { id: key, enabled }
        : { name: key, enabled });
      const view = await reload();
      statusEl.textContent = view && view.pending
        ? (enabled ? "Enabled " : "Disabled ") + key +
          " — Apply to restart the assistant."
        : "Saved.";
    } catch (err) {
      input.disabled = false;
      input.checked = !enabled;
      statusEl.textContent = "";
      await showAlert("Couldn't save: " + err.message);
    }
  }

  async function onApply() {
    applyBtn.disabled = true;
    statusEl.textContent = "Applying…";
    let res;
    try {
      res = await postJSON(endpoint(basePath, "apply"), {});
    } catch (err) {
      applyBtn.disabled = false;
      statusEl.textContent = "";
      await showAlert("Couldn't apply: " + err.message);
      return;
    }
    if (!res || res.restarted !== true) {
      applyBtn.disabled = false;
      statusEl.textContent = (res && res.message) || "Saved.";
      return;
    }
    statusEl.textContent =
      "Restarting the assistant to apply your changes — about 10–15 seconds…";
    for (let i = 0; i < 20; i++) {
      await sleep(1500);
      const view = await reload({ keepStale: true });
      if (view && !view.unavailable && view.pending === false) {
        statusEl.textContent = "Changes applied.";
        applyBtn.disabled = false;
        return;
      }
    }
    applyBtn.disabled = false;
    statusEl.textContent =
      "Still applying — if the assistant doesn't come back, check the " +
      "System page.";
  }

  return { onToggle, onApply };
}
