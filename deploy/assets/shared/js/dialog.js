// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// dialog.js — accessible, styled stand-ins for the browser's native
// confirm()/alert() popups, shared across the canonical (app.css) pages.
//
// Why not window.confirm / window.alert: the browser can suppress them
// outright ("prevent this page from creating more dialogs"). That silently
// defeated the speaker's restart/reboot guards — a click did nothing and the
// user had no idea why. A <dialog>.showModal() can't be suppressed, and gives
// a real focus trap, ESC-to-cancel, an inert backdrop, and aria-modal for
// free. Styling lives in app.css (.jts-dialog*); this module only builds
// the markup and resolves a Promise with the user's choice:
//
//     if (await jtsConfirm("Reboot the speaker?", { danger: true })) { … }
//     await jtsAlert("Enter the password first.");
//
// These are async and DO NOT block (unlike native confirm/alert): a non-awaited
// call returns immediately while the modal is open, so await if later code must
// run only after the user dismisses it.
//
// <dialog>.showModal() is assumed (every browser since 2022 has it — Chrome
// 37, Firefox 98, Safari 15.4); no native-popup fallback, since reintroducing
// window.confirm would bring back the suppression bug this module exists to
// kill. Pages that need a dialog import this static shared module.

function openDialog({ message, title, buttons }) {
  const dlg = document.createElement("dialog");
  dlg.className = "jts-dialog";

  // <form method="dialog">: clicking a submit button closes the dialog and
  // sets dialog.returnValue to that button's value — no per-button wiring.
  const form = document.createElement("form");
  form.method = "dialog";
  form.className = "jts-dialog__form";

  if (title) {
    const heading = document.createElement("h2");
    heading.className = "jts-dialog__title";
    heading.textContent = title;
    form.appendChild(heading);
  }

  // textContent, never innerHTML: messages interpolate untrusted strings
  // (SSIDs, Bluetooth/device names, profile names). CSS `white-space: pre-line`
  // renders the \n in multi-line messages as real breaks.
  const body = document.createElement("p");
  body.className = "jts-dialog__body";
  body.textContent = message;
  form.appendChild(body);

  const actions = document.createElement("div");
  actions.className = "jts-dialog__actions";
  for (const spec of buttons) {
    const btn = document.createElement("button");
    btn.type = "submit";
    btn.value = spec.value;
    btn.textContent = spec.label;
    btn.className = "btn " + spec.btnClass;
    if (spec.autofocus) btn.autofocus = true;
    actions.appendChild(btn);
  }
  form.appendChild(actions);
  dlg.appendChild(form);
  document.body.appendChild(dlg);

  return new Promise((resolve) => {
    // A button click sets returnValue to its value; ESC closes with an empty
    // returnValue. Either way: resolve with it, then tear the element down.
    dlg.addEventListener("close", () => {
      const value = dlg.returnValue;
      dlg.remove();
      resolve(value);
    }, { once: true });
    dlg.showModal();
  });
}

// Drop-in async replacement for window.confirm(). Resolves true/false.
// `danger: true` (destructive actions — reboot, delete) styles the confirm
// button red and autofocuses Cancel so a stray Enter can't fire it; ESC
// always cancels.
export function jtsConfirm(message, opts = {}) {
  const {
    title = "",
    confirmLabel = "Confirm",
    cancelLabel = "Cancel",
    danger = false,
  } = opts;
  return openDialog({
    message,
    title,
    buttons: [
      { label: cancelLabel, value: "cancel", btnClass: "btn--ghost", autofocus: danger },
      {
        label: confirmLabel,
        value: "confirm",
        btnClass: danger ? "btn--danger" : "btn--primary",
        autofocus: !danger,
      },
    ],
  }).then((value) => value === "confirm");
}

// Drop-in async replacement for window.alert(). Resolves when acknowledged.
export function jtsAlert(message, opts = {}) {
  const { title = "", okLabel = "OK" } = opts;
  return openDialog({
    message,
    title,
    buttons: [{ label: okLabel, value: "ok", btnClass: "btn--primary", autofocus: true }],
  }).then(() => undefined);
}

// Accessible, styled replacement for window.prompt() — collects one line of
// text. Resolves the entered string on Submit, or null on Cancel/ESC (so a
// caller can tell "" — explicitly emptied — from "dismissed"). Reuses the
// app.css `.field` input vocabulary so it needs no new CSS. The value is read
// off the input node directly (never interpolated into markup), and the
// message/title go through textContent like jtsConfirm — no innerHTML, no
// untrusted-string injection. `secret: true` masks the input (type=password)
// for tokens/keys.
export function jtsPrompt(message, opts = {}) {
  const {
    title = "",
    label = "",
    okLabel = "Save",
    cancelLabel = "Cancel",
    placeholder = "",
    secret = false,
  } = opts;

  const dlg = document.createElement("dialog");
  dlg.className = "jts-dialog";

  const form = document.createElement("form");
  form.method = "dialog";
  form.className = "jts-dialog__form";

  if (title) {
    const heading = document.createElement("h2");
    heading.className = "jts-dialog__title";
    heading.textContent = title;
    form.appendChild(heading);
  }

  const body = document.createElement("p");
  body.className = "jts-dialog__body";
  body.textContent = message;
  form.appendChild(body);

  // `.field` (app.css) gives the label + themed input styling for free.
  const field = document.createElement("div");
  field.className = "field";
  const input = document.createElement("input");
  input.type = secret ? "password" : "text";
  input.placeholder = placeholder;
  input.autofocus = true;
  if (label) {
    const lbl = document.createElement("label");
    lbl.textContent = label;
    const id = "jts-prompt-" + Math.random().toString(36).slice(2);
    lbl.htmlFor = id;
    input.id = id;
    field.appendChild(lbl);
  }
  field.appendChild(input);
  form.appendChild(field);

  const actions = document.createElement("div");
  actions.className = "jts-dialog__actions";
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "submit";
  cancelBtn.value = "cancel";
  cancelBtn.textContent = cancelLabel;
  cancelBtn.className = "btn btn--ghost";
  const okBtn = document.createElement("button");
  okBtn.type = "submit";
  okBtn.value = "submit";
  okBtn.textContent = okLabel;
  okBtn.className = "btn btn--primary";
  actions.appendChild(cancelBtn);
  actions.appendChild(okBtn);
  form.appendChild(actions);
  dlg.appendChild(form);
  document.body.appendChild(dlg);

  return new Promise((resolve) => {
    dlg.addEventListener("close", () => {
      const submitted = dlg.returnValue === "submit";
      const value = submitted ? input.value : null;
      dlg.remove();
      resolve(value);
    }, { once: true });
    dlg.showModal();
  });
}
