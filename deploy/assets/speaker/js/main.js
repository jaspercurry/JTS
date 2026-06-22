// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// main.js — /speaker/ rename confirm guard.
//
// The page is a plain server-rendered form that POSTs to ./save. This module
// only intercepts submit to confirm the rename first, because saving restarts
// audio / Bluetooth / voice services and the user may need to reconnect. The
// confirm uses the shared <dialog> helper (never window.confirm, which the
// browser can suppress — see /assets/shared/js/dialog.js); if the user
// confirms, we let the native form POST proceed unchanged.
//
// The default speaker name rides in the form's data-default attribute (escaped
// server-side) rather than being baked into this cacheable module.

import { jtsConfirm } from "/assets/shared/js/dialog.js";

const form = document.getElementById("speaker-name-form");
const input = document.getElementById("speaker-name");

if (form && input) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = input.value.trim() || form.dataset.default || "";
    const ok = await jtsConfirm(
      `Rename speaker to "${name}"? This restarts audio, Bluetooth, and ` +
        "voice services. You may need to reconnect from your phone or computer."
    );
    if (ok) form.submit();
  });
}
