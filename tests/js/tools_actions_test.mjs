// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import { readFileSync } from "node:fs";

const source = readFileSync(process.argv[2], "utf8");
const moduleUrl = "data:text/javascript;base64," +
  Buffer.from(source, "utf8").toString("base64");
const { createToolActions } = await import(moduleUrl);

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function input(dataset, checked) {
  return {
    dataset,
    checked,
    disabled: false,
    matches: () => true,
  };
}

function harness(overrides = {}) {
  const requests = [];
  const alerts = [];
  const reloads = [];
  const statusEl = { textContent: "" };
  const applyBtn = { disabled: false };
  const config = {
    basePath: "/tools",
    statusEl,
    applyBtn,
    postJSON: async (path, body) => {
      requests.push({ path, body });
      return {};
    },
    reload: async (options) => {
      reloads.push(options);
      return { pending: true };
    },
    showAlert: async (message) => alerts.push(message),
    sleep: async () => {},
    ...overrides,
  };
  return {
    ...createToolActions(config),
    statusEl,
    applyBtn,
    requests,
    alerts,
    reloads,
  };
}

{
  const h = harness();
  const target = input({ tool: "weather" }, true);
  await h.onToggle({ target });
  assert(h.requests.length === 1, "tool toggle should send one request");
  assert(h.requests[0].path === "/tools/toggle", "tool toggle path drifted");
  assert(JSON.stringify(h.requests[0].body) ===
    JSON.stringify({ name: "weather", enabled: true }), "tool body drifted");
  assert(h.statusEl.textContent ===
    "Enabled weather — Apply to restart the assistant.", "toggle copy drifted");
}

{
  const h = harness({ basePath: "/tools/" });
  const target = input({ pack: "music" }, false);
  await h.onToggle({ target });
  assert(h.requests[0].path === "/tools/toggle-pack", "pack path drifted");
  assert(JSON.stringify(h.requests[0].body) ===
    JSON.stringify({ id: "music", enabled: false }), "pack body drifted");
}

{
  const h = harness({ postJSON: async () => { throw new Error("offline"); } });
  const target = input({ tool: "weather" }, true);
  await h.onToggle({ target });
  assert(target.checked === false && target.disabled === false,
    "failed toggle should restore the control");
  assert(h.alerts[0] === "Couldn't save: offline", "toggle error copy drifted");
}

{
  const h = harness({ postJSON: async () => ({ message: "No provider." }) });
  await h.onApply();
  assert(h.statusEl.textContent === "No provider.", "no-restart reason was lost");
  assert(h.applyBtn.disabled === false, "no-restart Apply should re-enable");
}

{
  const h = harness({ postJSON: async () => { throw new Error("offline"); } });
  await h.onApply();
  assert(h.applyBtn.disabled === false, "failed Apply should re-enable");
  assert(h.statusEl.textContent === "", "failed Apply should clear status");
  assert(h.alerts[0] === "Couldn't apply: offline", "Apply error copy drifted");
}

{
  let reloadCount = 0;
  const h = harness({
    postJSON: async (path, body) => {
      assert(path === "/tools/apply", "Apply path drifted");
      assert(JSON.stringify(body) === "{}", "Apply body drifted");
      return { restarted: true };
    },
    reload: async (options) => {
      assert(options.keepStale === true, "Apply polling must retain stale UI");
      reloadCount += 1;
      return { pending: false };
    },
  });
  await h.onApply();
  assert(reloadCount === 1, "Apply should stop polling at convergence");
  assert(h.statusEl.textContent === "Changes applied.", "Apply success copy drifted");
  assert(h.applyBtn.disabled === false, "successful Apply should re-enable");
}

{
  let reloadCount = 0;
  const h = harness({
    postJSON: async () => ({ restarted: true }),
    reload: async () => {
      reloadCount += 1;
      return { pending: true };
    },
  });
  await h.onApply();
  assert(reloadCount === 20, "Apply polling must remain bounded at 20 reads");
  assert(h.statusEl.textContent ===
    "Still applying — if the assistant doesn't come back, check the System page.",
  "Apply timeout copy drifted");
}

console.log(JSON.stringify({ ok: true }));
