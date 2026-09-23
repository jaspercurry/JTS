// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import { wireConfirmForms } from "/assets/shared/js/confirm-forms.js";
import { wireCopyButtons } from "/assets/shared/js/copy.js";

wireConfirmForms();
wireCopyButtons();

// ---------------------------------------------------------------------------
// 1. OAuth-mode picker (bounce / manual) — highlight the picked card.
// ---------------------------------------------------------------------------
// The radios carry name="mode" directly into the credentials <form>; this
// only drives the `.selected` highlight class the radio's own state can't.
document.querySelectorAll(".mode-picker input[type=radio]").forEach((radio) => {
  radio.addEventListener("change", () => {
    document
      .querySelectorAll(".mode-picker label")
      .forEach((label) => label.classList.remove("selected"));
    if (radio.parentElement) radio.parentElement.classList.add("selected");
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
    const mySeq = ++seq;
    clearTimeout(timer);
    reset();
    const value = input.value.trim();
    if (!value) return;
    timer = setTimeout(async () => {
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
