// main.js — /tools/ catalog wizard behaviour.
//
// Fetches /tools/catalog.json (written by jasper-voice at startup), renders the
// flat list of first-party tools, wires a client-side search filter and a
// delegated toggle handler, and re-fetches after a successful toggle. The page
// is otherwise static — the catalog only changes when jasper-voice restarts,
// which a toggle triggers.
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

let catalog = { tools: [], unavailable: true };

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
  const tools = q
    ? catalog.tools.filter((t) => haystack(t).includes(q))
    : catalog.tools;
  listEl.innerHTML = toolList(tools, { unavailable: catalog.unavailable });
  listEl.removeAttribute("aria-busy");
}

async function load() {
  try {
    catalog = await getJSON("catalog.json");
  } catch (e) {
    catalog = { tools: [], unavailable: true };
    statusEl.textContent = "Couldn't load the catalog (" + e.message + ").";
  }
  render();
}

// Delegated toggle handler: a .toggle checkbox carries the tool name in
// data-tool. POST the new state, then re-fetch so the badge + status converge
// to the daemon's truth.
async function onToggle(e) {
  const input = e.target;
  if (!input.matches("input[type=checkbox][data-tool]")) return;
  const name = input.dataset.tool;
  const enabled = input.checked;
  input.disabled = true;
  statusEl.textContent = enabled
    ? "Enabling " + name + "…"
    : "Disabling " + name + "…";
  try {
    await postJSON("toggle", { name, enabled });
    statusEl.textContent =
      (enabled ? "Enabled " : "Disabled ") + name +
      " — takes effect on the next voice restart.";
    await load();
  } catch (err) {
    input.disabled = false;
    input.checked = !enabled; // revert the optimistic flip
    statusEl.textContent = "";
    await jtsAlert("Couldn't save: " + err.message);
  }
}

searchEl.addEventListener("input", render);
listEl.addEventListener("change", onToggle);
load();
