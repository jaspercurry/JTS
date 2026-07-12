// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// main.js — /tools/ catalog wizard behaviour.
//
// Fetches /tools/catalog.json (voice's catalog metadata with the fresh
// disabled-set overlaid + a `pending` flag), renders the pack-first tool
// library view, and wires search + delegated toggle handlers + explicit Apply.
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
import { createToolActions } from "./actions.js";
import { toolList } from "./render.js";

const listEl = document.getElementById("tools-list");
const searchEl = document.getElementById("tools-search");
const statusEl = document.getElementById("status");
const applyBar = document.getElementById("tools-apply");
const applyBtn = document.getElementById("tools-apply-btn");

// Last GOOD catalog (tools + pending). Kept so a transient "unavailable"
// read during an Apply restart doesn't blank the list out from under the user.
let catalog = { tools: [], packs: [], pending: false, unavailable: true };

function toolsOf(c) {
  // Defend against a valid-JSON-but-wrong-shape payload ({}, {tools:null},
  // {tools:"..."}): never let .filter / .map throw and freeze the page.
  return Array.isArray(c.tools) ? c.tools : [];
}

function packsOf(c) {
  return Array.isArray(c.packs) ? c.packs : [];
}

function render() {
  const q = (searchEl.value || "").trim().toLowerCase();
  listEl.innerHTML = toolList(catalog, {
    query: q,
    unavailable: catalog.unavailable,
  });
  listEl.removeAttribute("aria-busy");
  applyBar.hidden = !catalog.pending;
}

function interactiveTarget(target) {
  return !!target.closest(
    "a, button, input, label, select, textarea, summary, details",
  );
}

function onCardClick(e) {
  const card = e.target.closest("[data-pack-href]");
  if (!card || interactiveTarget(e.target)) return;
  window.location.href = card.dataset.packHref;
}

function onCardKeydown(e) {
  if (e.key !== "Enter" && e.key !== " ") return;
  const card = e.target.closest("[data-pack-href]");
  if (!card || e.target !== card) return;
  e.preventDefault();
  window.location.href = card.dataset.packHref;
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
        packs: packsOf(next),
        pending: !!(next && next.pending),
        unavailable: !!(next && next.unavailable),
      };
    }
    render();
    return next;
  } catch (e) {
    if (!keepStale) {
      catalog = { tools: [], packs: [], pending: false, unavailable: true };
      statusEl.textContent = "Couldn't load the catalog (" + e.message + ").";
    }
    render();
    return null;
  }
}

const { onToggle, onApply } = createToolActions({
  basePath: "/tools/",
  statusEl,
  applyBtn,
  reload: load,
  postJSON,
  showAlert: jtsAlert,
});

searchEl.addEventListener("input", render);
listEl.addEventListener("change", onToggle);
listEl.addEventListener("click", onCardClick);
listEl.addEventListener("keydown", onCardKeydown);
applyBtn.addEventListener("click", onApply);
load();
