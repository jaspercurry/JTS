// main.js — /voice/ page behaviour.
//
// The page is server-rendered: every form POSTs to its own endpoint and the
// server re-renders. This module only adds small client affordances and
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
//   3. First-time provider selection. The server disables radios for providers
//      with no saved key. When a user pastes a key, locally enable that radio
//      so one deliberate "Save and Test" submit can save and select it.
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

// 3. Enable a provider radio once the user has typed a key into that provider.
function updateProviderRadioForKey(input) {
  const provider = input.dataset.providerKey;
  if (!provider) return;
  const radio = document.querySelector(`[data-provider-radio="${provider}"]`);
  const row = document.querySelector(`[data-provider-radio-row="${provider}"]`);
  const status = document.querySelector(`[data-provider-radio-status="${provider}"]`);
  if (!radio || !row) return;
  const hasTypedKey = input.value.trim().length > 0;
  if (hasTypedKey) {
    radio.disabled = false;
    row.classList.remove("is-disabled");
    row.removeAttribute("aria-disabled");
    if (status) status.textContent = "ready to save";
    return;
  }
  if (row.dataset.providerRadioOriginallyDisabled === "1") {
    radio.disabled = true;
    radio.checked = false;
    row.classList.add("is-disabled");
    row.setAttribute("aria-disabled", "true");
    if (status) status.textContent = "paste a key below first";
  }
}

document.addEventListener("input", (event) => {
  const input = event.target.closest("[data-provider-key]");
  if (!input) return;
  updateProviderRadioForKey(input);
});
