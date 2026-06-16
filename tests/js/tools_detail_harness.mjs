// Exercises the /tools/ detail prompt editor state machine without a browser.
//
//   node tools_detail_harness.mjs deploy/assets/tools/js/detail.js
//
// The harness strips imports and the final load() call, then evaluates the
// module against a tiny DOM surface. It intentionally covers only the prompt
// editor controls; render.js has its own HTML/XSS harness.
import { readFileSync } from "node:fs";

const detailPath = process.argv[2];
const stripImports = (s) => s.replace(/^\s*import\s.*$/gm, "");

function fail(message, extra = {}) {
  throw new Error(message + " " + JSON.stringify(extra));
}

function button(action) {
  return {
    dataset: { action, tool: "spotify_play" },
    hidden: false,
    disabled: false,
    closest(selector) {
      return selector === "button[data-action][data-tool]" ? this : null;
    },
  };
}

function makeEditor() {
  const view = { hidden: false, textContent: "Original prompt" };
  const textarea = {
    hidden: true,
    value: "Original prompt",
    focused: false,
    focus() { this.focused = true; },
    matches(selector) { return selector === ".prompt-edit"; },
    closest(selector) { return selector === ".prompt-editor" ? editor : null; },
  };
  const nodes = {
    ".prompt-view": view,
    ".prompt-edit": textarea,
    '[data-action="edit-prompt"]': button("edit-prompt"),
    '[data-action="reset-prompt"]': button("reset-prompt"),
    '[data-action="save-prompt"]': button("save-prompt"),
    '[data-action="cancel-prompt"]': button("cancel-prompt"),
  };
  const editor = {
    dataset: {},
    querySelector(selector) {
      return nodes[selector] || null;
    },
  };
  return { editor, view, textarea, nodes };
}

const mount = {
  listeners: {},
  querySelector() { return current.editor; },
  addEventListener(event, fn) { this.listeners[event] = fn; },
  removeAttribute() {},
};
const statusEl = { textContent: "" };
const applyBar = { hidden: true };
const applyBtn = { disabled: false, addEventListener() {} };
const document = {
  getElementById(id) {
    if (id === "tool-detail") return mount;
    if (id === "status") return statusEl;
    if (id === "tools-apply") return applyBar;
    if (id === "tools-apply-btn") return applyBtn;
    return null;
  },
};
const CSS = { escape: (s) => String(s).replace(/"/g, '\\"') };
const getJSON = async () => ({ tools: [], packs: [], pending: false });
const postJSON = async () => ({});
const jtsAlert = async () => {};
const escapeHtml = (s) => String(s);
const packDetail = () => "";
const toolDetail = () => "";

const current = makeEditor();
const src = stripImports(readFileSync(detailPath, "utf8"))
  .replace(/\nload\(\);\s*$/m, "\n");
const api = new Function(
  "document", "CSS", "getJSON", "postJSON", "jtsAlert", "escapeHtml",
  "packDetail", "toolDetail",
  src + "\nreturn { onPromptClick, onPromptInput };",
)(
  document, CSS, getJSON, postJSON, jtsAlert, escapeHtml, packDetail, toolDetail,
);

await api.onPromptClick({ target: current.nodes['[data-action="edit-prompt"]'] });
if (!current.view.hidden) fail("view should hide in edit mode");
if (current.textarea.hidden) fail("textarea should show in edit mode");
if (!current.textarea.focused) fail("textarea should focus on edit");
if (!current.nodes['[data-action="edit-prompt"]'].hidden) fail("edit should hide in edit mode");
if (!current.nodes['[data-action="reset-prompt"]'].hidden) fail("reset should hide in edit mode");
if (current.nodes['[data-action="save-prompt"]'].hidden) fail("save should show in edit mode");
if (!current.nodes['[data-action="save-prompt"]'].disabled) fail("save should start disabled");
if (current.nodes['[data-action="cancel-prompt"]'].hidden) fail("cancel should show in edit mode");

current.textarea.value = "Changed prompt";
api.onPromptInput({ target: current.textarea });
if (current.nodes['[data-action="save-prompt"]'].disabled) {
  fail("save should enable after a prompt edit");
}

current.textarea.value = "Original prompt";
api.onPromptInput({ target: current.textarea });
if (!current.nodes['[data-action="save-prompt"]'].disabled) {
  fail("save should disable again when the edit matches the original");
}

current.textarea.value = "Changed then cancelled";
await api.onPromptClick({ target: current.nodes['[data-action="cancel-prompt"]'] });
if (current.view.hidden) fail("view should show after cancel");
if (!current.textarea.hidden) fail("textarea should hide after cancel");
if (current.nodes['[data-action="edit-prompt"]'].hidden) fail("edit should show after cancel");
if (current.nodes['[data-action="reset-prompt"]'].hidden) fail("reset should show after cancel");
if (!current.nodes['[data-action="save-prompt"]'].hidden) fail("save should hide after cancel");
if (!current.nodes['[data-action="cancel-prompt"]'].hidden) fail("cancel should hide after cancel");
if (current.textarea.value !== "Original prompt") fail("cancel should restore the original prompt");

console.log(JSON.stringify({ ok: true }));
