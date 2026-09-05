// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// main.js — /voice/ page behaviour.
//
// The page is server-rendered: every form POSTs to its own endpoint and the
// server re-renders. This module only adds small client affordances and
// adds NO state of its own:
//
//   1. Clear-key confirm. Each provider's "Clear key" form carries a
//      data-confirm message (and data-confirm-danger="1"), wired by the
//      shared confirm-forms.js module.
//
//   2. Copy-prompt button. The "Copy prompt" button carries
//      data-copy-target="<textarea id>", wired by the shared copy.js module.
//
//   3. First-time provider selection. The server disables radios for providers
//      with no saved key. When a user pastes a key, locally enable that radio
//      so one deliberate "Save and Test" submit can save and select it.
//
// The confirm/clear targets ride in escaped data-* attributes rather than
// inline JS, so untrusted-looking interpolation can never inject script.

import { wireConfirmForms } from "/assets/shared/js/confirm-forms.js";
import { wireCopyButtons } from "/assets/shared/js/copy.js";

wireConfirmForms();
wireCopyButtons();

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
    if (status) status.textContent = "add a key first";
  }
}

document.addEventListener("input", (event) => {
  const input = event.target.closest("[data-provider-key]");
  if (!input) return;
  updateProviderRadioForKey(input);
});
