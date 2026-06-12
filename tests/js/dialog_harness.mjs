// Exercises a JTS dialog implementation against a minimal DOM shim, simulating
// the <dialog> `close` event that headless Chrome won't fire — so the
// Promise-resolve contract of jtsConfirm/jtsAlert gets real automated coverage.
// Driven by tests/test_dialog_helper.py against the canonical ES module
// (deploy/assets/shared/js/dialog.js).
// Prints one JSON line of observations.
//
//   node tests/js/dialog_harness.mjs <path-to-dialog-source.js>
import { readFileSync } from "node:fs";

// ---- minimal DOM shim: only what the dialog helpers actually touch ----
const doc = { activeElement: null, createElement: (t) => makeEl(t), body: null };

function makeEl(tag) {
  return {
    tagName: String(tag).toLowerCase(),
    className: "", textContent: "", type: "", value: "", method: "",
    autofocus: false, open: false, returnValue: "",
    children: [], _listeners: {},
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(ev, fn, opts) {
      (this._listeners[ev] = this._listeners[ev] || []).push({ fn, once: !!(opts && opts.once) });
    },
    setAttribute(k, v) { this[k] = v; },
    focus() { doc.activeElement = this; },
    showModal() {
      this.open = true;
      const af = buttonsOf(this).find((b) => b.autofocus);
      if (af) doc.activeElement = af; // mirrors the browser autofocusing [autofocus]
    },
    close(value) {
      this.open = false;
      if (value !== undefined) this.returnValue = value; // a button sets returnValue; ESC leaves it ""
      const ls = this._listeners.close || [];
      this._listeners.close = ls.filter((l) => !l.once);
      for (const l of ls) l.fn();
    },
    remove() { doc.body.children = doc.body.children.filter((c) => c !== this); },
  };
}
doc.body = makeEl("body");
function buttonsOf(root) {
  const out = [];
  (function walk(e) {
    for (const c of (e.children || [])) { if (c.tagName === "button") out.push(c); walk(c); }
  })(root);
  return out;
}
globalThis.document = doc;

// ---- load the implementation (strip ESM `export` so one path loads both) ----
const src = readFileSync(process.argv[2], "utf8").replace(/\bexport\s+/g, "");
const { jtsConfirm, jtsAlert, jtsConfirmSubmit } = new Function(
  src + "\nreturn { jtsConfirm, jtsAlert, " +
  "jtsConfirmSubmit: (typeof jtsConfirmSubmit !== 'undefined' ? jtsConfirmSubmit : undefined) };",
)();

const lastDialog = () => doc.body.children[doc.body.children.length - 1];
const flush = () => new Promise((r) => setTimeout(r));
const out = {};

// danger confirm: button contract + resolves true only when Confirm closes it
{
  const p = jtsConfirm("Reboot?", { danger: true });
  const dlg = lastDialog();
  out.confirmButtonValues = buttonsOf(dlg).map((b) => b.value);
  out.dangerAutofocusCancel = !!doc.activeElement && doc.activeElement.value === "cancel";
  dlg.close("confirm");
  out.resolveTrueOnConfirm = (await p) === true;
}
// non-danger confirm: autofocus Confirm; ESC (no returnValue set) resolves false
{
  const p = jtsConfirm("Sure?");
  out.nonDangerAutofocusConfirm = !!doc.activeElement && doc.activeElement.value === "confirm";
  lastDialog().close();
  out.resolveFalseOnEsc = (await p) === false;
}
// Cancel button value resolves false
{
  const p = jtsConfirm("Sure?");
  lastDialog().close("cancel");
  out.resolveFalseOnCancel = (await p) === false;
}
// alert: single OK button, resolves on acknowledge
{
  const p = jtsAlert("Saved.");
  out.alertButtonValues = buttonsOf(lastDialog()).map((b) => b.value);
  lastDialog().close("ok");
  await p;
  out.alertResolves = true;
}
out.removedAfterClose = doc.body.children.length === 0;

// legacy-only: jtsConfirmSubmit used to cancel the native submit and re-submit
// if confirmed. The canonical static module does not ship that shim, but the
// harness keeps this optional probe so historical copies can still be checked
// during archaeology.
out.hasConfirmSubmit = typeof jtsConfirmSubmit === "function";
if (out.hasConfirmSubmit) {
  let submitted = false;
  out.confirmSubmitReturnsFalse =
    jtsConfirmSubmit({ submit() { submitted = true; } }, "x", { danger: true }) === false;
  lastDialog().close("confirm");
  await flush();
  out.confirmSubmitSubmitsOnConfirm = submitted;
}

console.log(JSON.stringify(out));
