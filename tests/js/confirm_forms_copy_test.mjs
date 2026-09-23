// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import assert from "node:assert/strict";
import { element } from "./_dom.mjs";
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

const { copyText, wireCopyButtons } = await loadEsm(repoPath("deploy/assets/shared/js/copy.js"));

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

const attached = new Set();
const writes = [];
let command, selectionFails = false;
Object.assign(document, {
  body: { appendChild: (el) => attached.add(el) },
  createElement: (tag) => {
    assert.equal(tag, "textarea");
    return { style: {}, setAttribute() {},
      select() { if (selectionFails) throw new Error("selection unavailable"); },
      remove() { attached.delete(this); },
    };
  },
  execCommand: (name) => {
    assert.equal(name, "copy");
    if (command === "throw") throw new Error("copy unavailable");
    return command;
  },
});
for (const clipboard of [undefined, { writeText: async () => { throw new Error("denied"); } }]) {
  navigator.clipboard = clipboard;
  for (const value of [sourceInput, "dummy credential"]) {
    for (command of [true, false, "throw"]) {
      assert.equal(await copyText(value), command === true);
      assert.equal(attached.size, 0);
    }
  }
}
selectionFails = true;
assert.equal(await copyText("dummy credential"), false);
assert.equal(attached.size, 0);
navigator.clipboard = { writeText: async (text) => { writes.push(text); } };
assert.equal(await copyText("dummy credential"), true);
assert.deepEqual(writes, ["dummy credential"]);
assert.equal(attached.size, 0);

const nodes = new Map(["copy-voice-prompt-btn", "copy-voice-prompt-creds-btn", "copy-voice-prompt-feedback"]
  .map((id) => [id, element(id)]));
document.getElementById = (id) => nodes.get(id);
let confirmed = false, fetched = 0;
globalThis.__copyText = copyText;
globalThis.__jtsConfirm = async () => confirmed;
globalThis.fetch = async () => {
  fetched++;
  return { ok: true, json: async () => ({ url: "http://dummy.local", token: "dummy-token" }) };
};
const ha = await loadEsm(repoPath("deploy/assets/home-assistant/js/main.js"), {
  stripImports: true, guardNoImports: true,
  prelude: "const copyText = globalThis.__copyText; const jtsConfirm = globalThis.__jtsConfirm; const csrfHeaders = () => ({});",
  truncateBefore: "\nwireDiscover();", exportNames: ["wireCopyButtons"],
});
ha.wireCopyButtons("{HA_URL_PLACEHOLDER} {HA_TOKEN_PLACEHOLDER}");
writes.length = 0;
await nodes.get("copy-voice-prompt-creds-btn").click();
assert.equal(fetched, 0);
assert.deepEqual(writes, []);
await nodes.get("copy-voice-prompt-btn").click();
assert.equal(fetched, 0);
assert.match(writes[0], /<your HA URL/);
assert.match(writes[0], /<paste a long-lived access token/);
confirmed = true;
await nodes.get("copy-voice-prompt-creds-btn").click();
assert.equal(fetched, 1);
assert.equal(writes[1], "http://dummy.local dummy-token");
assert.equal(nodes.get("copy-voice-prompt-feedback").classList.contains("ha-ok"), true);
console.log(JSON.stringify({ ok: true }));
