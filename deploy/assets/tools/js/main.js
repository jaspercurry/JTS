// main.js — /tools/ catalog wizard behaviour.
//
// Fetches /tools/catalog.json (voice's catalog metadata with the fresh
// disabled-set overlaid + a `pending` flag), renders the grouped tool library
// view, and wires search + a delegated toggle handler + an explicit Apply.
//
// Two-step on purpose (see tools_setup.py): a toggle only STAGES (writes the
// disabled-set, no restart), so ticking boxes is instant and never nukes an
// in-progress conversation. The overlay makes the list converge immediately —
// no waiting on, or being raced by, a restart. Apply restarts jasper-voice
// once to make staged changes live; during that ~10s window catalog.json
// briefly reads "unavailable" (the daemon wipes+rewrites /run/jasper), so we
// keep the last-known list on screen and poll until it reflects the change.
//
// All mutating POSTs go through postJSON (X-CSRF-Token via jsonHeaders), all
// tool text is escaped by render.js before innerHTML, and the toggle key rides
// in a data-tool attribute read by a delegated listener — no inline handlers,
// no native dialogs.

import { getJSON, postJSON } from "/assets/shared/js/http.js";
import { jtsAlert } from "/assets/shared/js/dialog.js";
import { toolList } from "./render.js";

const listEl = document.getElementById("tools-list");
const searchEl = document.getElementById("tools-search");
const statusEl = document.getElementById("status");
const applyBar = document.getElementById("tools-apply");
const applyBtn = document.getElementById("tools-apply-btn");

// Last GOOD catalog (tools + pending). Kept so a transient "unavailable"
// read during an Apply restart doesn't blank the list out from under the user.
let catalog = { tools: [], pending: false, unavailable: true };

function toolsOf(c) {
  // Defend against a valid-JSON-but-wrong-shape payload ({}, {tools:null},
  // {tools:"..."}): never let .filter / .map throw and freeze the page.
  return Array.isArray(c.tools) ? c.tools : [];
}

// Lowercased "name description labels" haystack for the substring filter.
function haystack(tool) {
  return [
    tool.name || "",
    tool.description || "",
    (tool.labels || []).join(" "),
  ].join(" ").toLowerCase();
}

function render() {
  const q = (searchEl.value || "").trim().toLowerCase();
  const all = toolsOf(catalog);
  const tools = q ? all.filter((t) => haystack(t).includes(q)) : all;
  listEl.innerHTML = toolList(tools, { unavailable: catalog.unavailable });
  listEl.removeAttribute("aria-busy");
  applyBar.hidden = !catalog.pending;
}

// Fetch the catalog. On a usable payload, replace `catalog`. On an
// unavailable/failed read, KEEP the last-known tools (so the list doesn't
// blank) but surface the unavailable flag so the empty-state shows only when
// we never had anything. Returns the freshly-read view (or null on failure).
async function load({ keepStale = false } = {}) {
  try {
    const next = await getJSON("/tools/catalog.json");
    if (next && next.unavailable && keepStale && toolsOf(catalog).length) {
      // Mid-restart: hold the existing list, just reflect that it's settling.
      catalog = { ...catalog, pending: !!next.pending };
    } else {
      catalog = {
        tools: toolsOf(next),
        pending: !!(next && next.pending),
        unavailable: !!(next && next.unavailable),
      };
    }
    render();
    return next;
  } catch (e) {
    if (!keepStale) {
      catalog = { tools: [], pending: false, unavailable: true };
      statusEl.textContent = "Couldn't load the catalog (" + e.message + ").";
    }
    render();
    return null;
  }
}

// Delegated toggle handler: a .toggle checkbox carries the tool name in
// data-tool. POST the staged state, then re-read the overlay so the badge +
// Apply affordance converge. No restart happens here, so the re-fetch can't be
// raced — it's the daemon-independent overlay.
async function onToggle(e) {
  const input = e.target;
  if (!input.matches("input[type=checkbox][data-tool]")) return;
  const name = input.dataset.tool;
  const enabled = input.checked;
  input.disabled = true;
  statusEl.textContent = (enabled ? "Enabling " : "Disabling ") + name + "…";
  try {
    await postJSON("toggle", { name, enabled });
    statusEl.textContent =
      (enabled ? "Enabled " : "Disabled ") + name +
      " — Apply to restart the assistant.";
    await load();
  } catch (err) {
    input.disabled = false;
    input.checked = !enabled; // revert the optimistic flip
    statusEl.textContent = "";
    await jtsAlert("Couldn't save: " + err.message);
  }
}

// Explicit Apply: restart jasper-voice once so staged changes go live. Then
// poll the catalog until `pending` clears (the daemon rewrote tools.json), or
// give up after a bounded window. The list is held stable throughout.
async function onApply() {
  applyBtn.disabled = true;
  statusEl.textContent = "Applying…";
  let res;
  try {
    res = await postJSON("apply", {});
  } catch (err) {
    applyBtn.disabled = false;
    statusEl.textContent = "";
    await jtsAlert("Couldn't apply: " + err.message);
    return;
  }
  if (!res || res.restarted !== true) {
    // No restart happened (no provider / bonded follower / throttled). The
    // change is still saved; surface the server's honest reason.
    applyBtn.disabled = false;
    statusEl.textContent = (res && res.message) || "Saved.";
    return;
  }
  statusEl.textContent =
    "Restarting the assistant to apply your changes — about 10–15 seconds…";
  // Poll for convergence: a healthy catalog has NO `unavailable` key (only the
  // failure path adds it), so test `!view.unavailable` — `=== false` would
  // never match a healthy read and every Apply would falsely time out.
  // pending=false means the live registry now matches the staged set.
  for (let i = 0; i < 20; i++) {
    await new Promise((r) => setTimeout(r, 1500));
    const view = await load({ keepStale: true });
    if (view && !view.unavailable && view.pending === false) {
      statusEl.textContent = "Changes applied.";
      applyBtn.disabled = false;
      return;
    }
  }
  // Timed out waiting — the restart may still be settling.
  applyBtn.disabled = false;
  statusEl.textContent =
    "Still applying — if the assistant doesn't come back, check the " +
    "System page.";
}

searchEl.addEventListener("input", render);
listEl.addEventListener("change", onToggle);
applyBtn.addEventListener("click", onApply);
load();
