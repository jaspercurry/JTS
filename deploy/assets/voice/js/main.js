// main.js — /voice/ page behaviour.
//
// The page is server-rendered: every form POSTs to its own endpoint and the
// server re-renders. This module only adds two small client affordances and
// adds NO state of its own:
//
//   1. Clear-key confirm. Each provider's "Clear key" form carries a
//      data-confirm message (and data-confirm-danger="1"). A delegated submit
//      listener intercepts those forms, asks the shared <dialog> confirm
//      (never window.confirm, which the browser can suppress — see
//      /assets/shared/js/dialog.js), and re-submits only if the user agrees.
//      form.submit() does not re-fire the submit event, so there's no loop.
//
//   2. Copy-prompt button. The "Copy prompt" button carries
//      data-copy-target="<textarea id>"; clicking it selects and copies that
//      textarea's text via the async Clipboard API, falling back to the
//      legacy execCommand path when clipboard access is unavailable (e.g. a
//      plain-HTTP LAN origin without a secure context).
//
// The confirm/clear targets ride in escaped data-* attributes rather than
// inline JS, so untrusted-looking interpolation can never inject script.

import { jtsConfirm } from "/assets/shared/js/dialog.js";

// 1. Delegated clear-key confirm: intercept any form[data-confirm] submit.
document.addEventListener("submit", async (event) => {
  const form = event.target.closest("form[data-confirm]");
  if (!form) return;
  event.preventDefault();
  const ok = await jtsConfirm(form.dataset.confirm, {
    danger: form.dataset.confirmDanger === "1",
  });
  if (ok) form.submit();
});

// 2. Copy-prompt: copy a textarea's text to the clipboard.
document.addEventListener("click", (event) => {
  const btn = event.target.closest("[data-copy-target]");
  if (!btn) return;
  const target = document.getElementById(btn.dataset.copyTarget);
  if (!target) return;
  target.focus();
  target.select();
  if (navigator.clipboard) {
    navigator.clipboard.writeText(target.value).catch(() => {
      // Secure-context clipboard refused (e.g. plain-HTTP LAN origin) — the
      // text is already selected, so fall through to the legacy path.
      document.execCommand("copy");
    });
  } else {
    document.execCommand("copy");
  }
});
