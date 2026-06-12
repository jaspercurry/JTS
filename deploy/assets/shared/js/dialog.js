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
