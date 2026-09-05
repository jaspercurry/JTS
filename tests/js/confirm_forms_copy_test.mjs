// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Pins the two shared widgets extracted from four/three per-page copies
// (issue #4031): confirm-forms.js submits a form[data-confirm] only after
// jtsConfirm resolves true, and busies the submit button first; copy.js
// swaps a copy button's own label to "Copied" on a successful clipboard
// write. jtsConfirm and the clipboard are stubbed — this pins the modules'
// own wiring, not the shared <dialog> or the browser clipboard.
//
//   node tests/js/confirm_forms_copy_test.mjs
import assert from "node:assert/strict";
import { loadEsm, repoPath } from "./_loader.mjs";

// ---- confirm-forms.js ------------------------------------------------------

function makeForm({ confirm, danger }) {
  const dataset = { confirm };
  if (danger) dataset.confirmDanger = "1";
  const btn = { disabled: false, textContent: "Save", dataset: {} };
  const listeners = [];
  const form = {
    dataset,
    submitted: false,
    addEventListener: (type, fn) => listeners.push(fn),
    closest: () => form,
    querySelector: () => btn,
    submit() { form.submitted = true; },
    async fireSubmit() {
      const event = { target: form, submitter: undefined, preventDefault() { event.defaultPrevented = true; } };
      for (const fn of listeners) await fn(event);
      return event;
    },
  };
  return { form, btn };
}

globalThis.__confirmResult = true;
const { wireConfirmForms } = await loadEsm(repoPath("deploy/assets/shared/js/confirm-forms.js"), {
  rewrite: [[/^import \{ jtsConfirm \} from "\/assets\/shared\/js\/dialog\.js";\n/m, ""]],
  prelude:
    "function jtsConfirm(msg, opts) { globalThis.__lastConfirm = { msg, opts }; " +
    "return Promise.resolve(globalThis.__confirmResult); }\n",
});

{
  // OK: dialog resolves true → submits, busies the button, marks confirmed.
  const { form, btn } = makeForm({ confirm: "Sure?", danger: true });
  wireConfirmForms(form);
  globalThis.__confirmResult = true;
  const event = await form.fireSubmit();
  assert.equal(event.defaultPrevented, true);
  assert.equal(form.submitted, true, "form.submit() must be called on OK");
  assert.equal(form.dataset.confirmed, "1");
  assert.equal(btn.disabled, true);
  assert.equal(btn.textContent, "Working…");
  assert.equal(globalThis.__lastConfirm.opts.danger, true);
}

{
  // Cancel: dialog resolves false → no submit, no busy state.
  const { form, btn } = makeForm({ confirm: "Sure?" });
  wireConfirmForms(form);
  globalThis.__confirmResult = false;
  await form.fireSubmit();
  assert.equal(form.submitted, false, "form.submit() must NOT be called on cancel");
  assert.notEqual(form.dataset.confirmed, "1");
  assert.equal(btn.disabled, false);
}

// ---- copy.js ----------------------------------------------------------------

const sourceInput = { value: "hello world", select() {} };
globalThis.document = { getElementById: (id) => (id === "src" ? sourceInput : null) };
// Node's own `navigator` global is a getter-only accessor — redefine it.
Object.defineProperty(globalThis, "navigator", {
  value: { clipboard: { writeText: async () => {} } },
  configurable: true,
});

const { wireCopyButtons } = await loadEsm(repoPath("deploy/assets/shared/js/copy.js"));

{
  const listeners = [];
  const btn = {
    dataset: { copyTarget: "src" },
    textContent: "Copy",
    addEventListener: (type, fn) => listeners.push(fn),
    closest: () => btn,
  };
  wireCopyButtons(btn);
  for (const fn of listeners) await fn({ target: btn });
  assert.equal(btn.textContent, "Copied", "successful copy must swap the button label");
}

console.log(JSON.stringify({ ok: true }));
