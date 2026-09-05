// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// confirm-forms.js — the shared "confirm before this form submits" guard.
// A form carrying data-confirm="<message>" (data-confirm-danger="1" for the
// destructive red style) submits only after the shared <dialog> confirm
// (jtsConfirm — never window.confirm, which the browser can suppress). On OK
// the submit button gets a busy label (data-busy-label, default "Working…")
// so the click reads as acknowledged before the page navigates away.
// form.submit() does not re-fire "submit", so there is no recursion.

import { jtsConfirm } from "/assets/shared/js/dialog.js";

export function wireConfirmForms(root = document) {
  root.addEventListener("submit", async (event) => {
    const form = event.target.closest("form[data-confirm]");
    if (!form || form.dataset.confirmed === "1") return;
    event.preventDefault();
    const ok = await jtsConfirm(form.dataset.confirm, {
      danger: form.dataset.confirmDanger === "1",
    });
    if (!ok) return;
    form.dataset.confirmed = "1";
    const btn = event.submitter || form.querySelector('[type="submit"]');
    if (btn) {
      btn.disabled = true;
      btn.textContent = btn.dataset.busyLabel || "Working…";
    }
    form.submit();
  });
}
