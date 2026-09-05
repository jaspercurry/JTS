// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// main.js — /spotify/ wizard behaviour.
//
// The page is server-rendered (forms POST and redirect); this module adds
// the following progressive enhancements:
//   1. highlight the picked OAuth-mode radio card and mirror it into the
//      hidden form field (the picker is a sibling of the credentials form),
//   2. copy the redirect URL to the clipboard, wired by the shared copy.js
//      module,
//   3. live-preview a pasted playlist's name before enabling its Add button,
//   4. reveal the inline "add playlist" form,
//   5. route every destructive submit through the shared data-confirm guard
//      (confirm-forms.js — never window.confirm, which the browser can
//      suppress).
//
// All of it degrades gracefully: with JS off the forms still submit, the
// redirect URL is still selectable, and Add is simply always enabled.
//
// Shared helpers come from the canonical layer by absolute path — we never
// re-declare the CSRF/JSON/dialog plumbing here.

import { wireConfirmForms } from "/assets/shared/js/confirm-forms.js";
import { wireCopyButtons } from "/assets/shared/js/copy.js";

wireConfirmForms();
wireCopyButtons();

// ---------------------------------------------------------------------------
// 1. OAuth-mode picker (bounce / manual) — highlight + mirror into the form.
// ---------------------------------------------------------------------------
// The radios live in a .mode-picker block that is a SIBLING of the
// credentials <form> (the form only carries a hidden <input name="mode">),
// so we copy the chosen value across on change.
const modeInput = document.getElementById("mode-input");
document.querySelectorAll(".mode-picker input[type=radio]").forEach((radio) => {
  radio.addEventListener("change", () => {
    document
      .querySelectorAll(".mode-picker label")
      .forEach((label) => label.classList.remove("selected"));
    if (radio.parentElement) radio.parentElement.classList.add("selected");
    if (modeInput) modeInput.value = radio.value;
  });
});

// Let a readonly URL field select-all on focus/click without an inline handler.
document.querySelectorAll("input[data-select-on-click]").forEach((input) => {
  input.addEventListener("click", () => input.select());
});

// ---------------------------------------------------------------------------
// 3 + 4. Per-account playlist editor: reveal + live name preview.
// ---------------------------------------------------------------------------
// As the user pastes a playlist URL we debounce and ask the read-only
// /playlist-preview endpoint for the name; the Add button enables only once a
// name comes back. A sequence counter discards out-of-order responses.
document.querySelectorAll("form.pl-add").forEach((form) => {
  const section = form.closest(".pl-section");
  const account = section ? section.dataset.account : "";
  const input = form.querySelector(".pl-input");
  const preview = form.querySelector(".pl-preview");
  const submit = form.querySelector(".pl-submit");
  if (!input || !preview || !submit) return;

  let timer = null;
  let seq = 0;

  function reset() {
    submit.disabled = true;
    preview.textContent = "";
    preview.className = "pl-preview";
  }

  input.addEventListener("input", () => {
    clearTimeout(timer);
    reset();
    const value = input.value.trim();
    if (!value) return;
    timer = setTimeout(async () => {
      const mySeq = ++seq;
      preview.textContent = "Looking up…";
      // Build the request URL relative to the current page so it works behind
      // nginx's /spotify/ prefix. account + url ride as query params.
      const url = new URL("playlist-preview", window.location.href);
      url.searchParams.set("account", account);
      url.searchParams.set("url", value);
      try {
        const response = await fetch(url, { cache: "no-store" });
        const data = await response.json();
        if (mySeq !== seq) return; // a newer keystroke superseded this lookup
        if (data.error) {
          preview.textContent = data.error;
          preview.classList.add("error");
          return;
        }
        // data.name is set via textContent (never innerHTML) so a playlist
        // name with markup-looking characters can't inject into the page.
        preview.textContent = "✓ " + data.name;
        preview.classList.add("success");
        submit.disabled = false;
      } catch (err) {
        if (mySeq !== seq) return;
        preview.textContent = "Couldn't reach speaker.";
        preview.classList.add("error");
      }
    }, 350);
  });
});

// Reveal the (hidden) add-playlist form when its button is clicked.
document.querySelectorAll(".add-playlist-btn").forEach((button) => {
  button.addEventListener("click", () => {
    const target = document.getElementById(button.dataset.target);
    if (!target) return;
    target.hidden = false;
    button.style.display = "none";
    const field = target.querySelector(".pl-input");
    if (field) field.focus();
  });
});
