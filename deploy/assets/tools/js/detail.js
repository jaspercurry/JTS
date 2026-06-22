// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// detail.js — generated /tools/pack/<id>/ detail page.
//
// The server only passes the requested pack id through a JSON data island.
// This module fetches the same catalog JSON as the list page and renders the
// matching pack with its child tools.

import { getJSON, postJSON } from "/assets/shared/js/http.js";
import { jtsAlert } from "/assets/shared/js/dialog.js";
import { escapeHtml } from "/assets/shared/js/escape.js";
import { packDetail, toolDetail } from "./render.js";

const mount = document.getElementById("tool-detail");
const statusEl = document.getElementById("status");
const applyBar = document.getElementById("tools-apply");
const applyBtn = document.getElementById("tools-apply-btn");

let catalog = { tools: [], packs: [], pending: false, unavailable: true };

function requestedPackId() {
  const island = document.getElementById("tool-detail-data");
  if (!island) return "";
  try {
    const data = JSON.parse(island.textContent || "{}");
    return typeof data.pack_id === "string" ? data.pack_id : "";
  } catch (_) {
    return "";
  }
}

function toolsOf(c) {
  return c && Array.isArray(c.tools) ? c.tools : [];
}

function packsOf(c) {
  return c && Array.isArray(c.packs) ? c.packs : [];
}

function unavailable(message) {
  return (
    '<div class="info-card tool-empty">' +
    "<p>" + escapeHtml(message) + ' <a href="/tools/">Back to tools</a>.</p>' +
    "</div>"
  );
}

function render() {
  const id = requestedPackId();
  if (!id) {
    mount.innerHTML = unavailable("Tool pack not found.");
    return;
  }
  if (catalog.unavailable) {
    mount.innerHTML = unavailable("Tool catalog is not ready yet.");
    return;
  }
  const pack = packsOf(catalog).find((p) => p && p.id === id);
  if (pack) {
    const names = new Set((pack && pack.tool_names) || []);
    const tools = toolsOf(catalog).filter((t) => names.has(t.name));
    mount.innerHTML = packDetail(pack, tools);
  } else if (id.startsWith("tool:")) {
    const name = id.slice(5);
    mount.innerHTML = toolDetail(toolsOf(catalog).find((t) => t && t.name === name));
  } else {
    mount.innerHTML = packDetail(null, []);
  }
  applyBar.hidden = !catalog.pending;
}

async function load({ keepStale = false } = {}) {
  if (!mount) return null;
  try {
    const next = await getJSON("/tools/catalog.json");
    if (next && next.unavailable && keepStale && toolsOf(catalog).length) {
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
  } catch (err) {
    mount.innerHTML = unavailable(
      "Could not load the tool catalog (" + err.message + ").",
    );
    return null;
  } finally {
    mount.removeAttribute("aria-busy");
  }
}

async function onToggle(e) {
  const input = e.target;
  if (!input.matches("input[type=checkbox][data-tool], input[type=checkbox][data-pack]")) {
    return;
  }
  const isPack = !!input.dataset.pack;
  const key = isPack ? input.dataset.pack : input.dataset.tool;
  const enabled = input.checked;
  input.disabled = true;
  statusEl.textContent = (enabled ? "Enabling " : "Disabling ") + key + "...";
  try {
    await postJSON(isPack ? "/tools/toggle-pack" : "/tools/toggle", isPack
      ? { id: key, enabled }
      : { name: key, enabled });
    const view = await load();
    statusEl.textContent = view && view.pending
      ? (enabled ? "Enabled " : "Disabled ") + key +
        " - Apply to restart the assistant."
      : "Saved.";
  } catch (err) {
    input.disabled = false;
    input.checked = !enabled;
    statusEl.textContent = "";
    await jtsAlert("Couldn't save: " + err.message);
  }
}

function editorFor(toolName) {
  return mount.querySelector('.prompt-editor[data-tool="' + CSS.escape(toolName) + '"]');
}

function updatePromptSaveState(editor) {
  if (!editor) return;
  const save = editor.querySelector('[data-action="save-prompt"]');
  const textarea = editor.querySelector(".prompt-edit");
  if (!save || !textarea || textarea.hidden) return;
  save.disabled = textarea.value === (editor.dataset.originalPrompt || "");
}

function setEditing(editor, editing) {
  if (!editor) return;
  const view = editor.querySelector(".prompt-view");
  const textarea = editor.querySelector(".prompt-edit");
  const edit = editor.querySelector('[data-action="edit-prompt"]');
  const reset = editor.querySelector('[data-action="reset-prompt"]');
  const save = editor.querySelector('[data-action="save-prompt"]');
  const cancel = editor.querySelector('[data-action="cancel-prompt"]');
  if (view) view.hidden = editing;
  if (textarea) textarea.hidden = !editing;
  if (edit) edit.hidden = editing;
  if (reset) reset.hidden = editing;
  if (save) {
    save.hidden = !editing;
    save.disabled = true;
  }
  if (cancel) cancel.hidden = !editing;
  if (!editing) delete editor.dataset.originalPrompt;
  updatePromptSaveState(editor);
}

async function onPromptClick(e) {
  const btn = e.target.closest("button[data-action][data-tool]");
  if (!btn) return;
  const tool = btn.dataset.tool;
  const editor = editorFor(tool);
  if (!editor) return;
  const action = btn.dataset.action;
  const textarea = editor.querySelector(".prompt-edit");
  const view = editor.querySelector(".prompt-view");
  if (action === "edit-prompt") {
    textarea.value = view.textContent || "";
    editor.dataset.originalPrompt = textarea.value;
    setEditing(editor, true);
    textarea.focus();
    return;
  }
  if (action === "cancel-prompt") {
    textarea.value = editor.dataset.originalPrompt || view.textContent || "";
    setEditing(editor, false);
    return;
  }
  if (action === "save-prompt") {
    const prompt = textarea.value;
    if (prompt === (editor.dataset.originalPrompt || "")) return;
    btn.disabled = true;
    try {
      await postJSON("/tools/prompt", { name: tool, prompt });
      statusEl.textContent = "Saved prompt override - Apply to restart the assistant.";
      await load();
    } catch (err) {
      btn.disabled = false;
      await jtsAlert("Couldn't save prompt: " + err.message);
    }
    return;
  }
  if (action === "reset-prompt") {
    try {
      await postJSON("/tools/prompt-reset", { name: tool });
      statusEl.textContent = "Reset prompt override - Apply to restart the assistant.";
      await load();
    } catch (err) {
      await jtsAlert("Couldn't reset prompt: " + err.message);
    }
  }
}

function onPromptInput(e) {
  if (!e.target.matches(".prompt-edit")) return;
  updatePromptSaveState(e.target.closest(".prompt-editor"));
}

async function onApply() {
  applyBtn.disabled = true;
  statusEl.textContent = "Applying...";
  let res;
  try {
    res = await postJSON("/tools/apply", {});
  } catch (err) {
    applyBtn.disabled = false;
    statusEl.textContent = "";
    await jtsAlert("Couldn't apply: " + err.message);
    return;
  }
  if (!res || res.restarted !== true) {
    applyBtn.disabled = false;
    statusEl.textContent = (res && res.message) || "Saved.";
    return;
  }
  statusEl.textContent =
    "Restarting the assistant to apply your changes - about 10-15 seconds...";
  for (let i = 0; i < 20; i++) {
    await new Promise((r) => setTimeout(r, 1500));
    const view = await load({ keepStale: true });
    if (view && !view.unavailable && view.pending === false) {
      statusEl.textContent = "Changes applied.";
      applyBtn.disabled = false;
      return;
    }
  }
  applyBtn.disabled = false;
  statusEl.textContent =
    "Still applying - if the assistant doesn't come back, check the System page.";
}

mount.addEventListener("change", onToggle);
mount.addEventListener("click", onPromptClick);
mount.addEventListener("input", onPromptInput);
applyBtn.addEventListener("click", onApply);
load();
