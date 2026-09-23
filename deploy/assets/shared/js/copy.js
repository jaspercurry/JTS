// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

export async function copyText(source) {
  const text = typeof source === "string" ? source : source.value;
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {}
  let temporary;
  try {
    const el = typeof source === "string" ? (temporary = document.createElement("textarea")) : source;
    if (temporary) {
      el.value = text;
      el.setAttribute("readonly", "");
      el.style.cssText = "position:fixed;left:-9999px;top:-9999px;";
      document.body.appendChild(el);
    }
    el.select();
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    temporary?.remove();
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
