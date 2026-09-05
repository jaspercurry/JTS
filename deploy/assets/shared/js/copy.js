// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// copy.js — the shared "copy this value to the clipboard" button. A button
// carrying data-copy or data-copy-target (both: id of the input/textarea to
// read) copies that element's value and swaps its own label to "Copied" for
// ~1.5s. Falls back to select() + execCommand where the async Clipboard API
// is unavailable (plain-HTTP LAN origins).

async function copyText(el) {
  try {
    await navigator.clipboard.writeText(el.value);
    return true;
  } catch {
    el.select();
    try {
      return document.execCommand("copy");
    } catch {
      return false;
    }
  }
}

export function wireCopyButtons(root = document) {
  root.addEventListener("click", async (event) => {
    const btn = event.target.closest("[data-copy], [data-copy-target]");
    if (!btn) return;
    const src = document.getElementById(btn.dataset.copy || btn.dataset.copyTarget);
    if (!src) return;
    const original = btn.textContent;
    const ok = await copyText(src);
    btn.textContent = ok ? "Copied" : "Copy failed — select and copy the text";
    setTimeout(() => { btn.textContent = original; }, 1500);
  });
}
