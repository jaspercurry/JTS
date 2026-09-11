// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// components.js — small chat-page UI primitives.
//
// These mirror the /system/ module graph and build everything with dom.js so
// untrusted transcript strings remain text nodes.

import { h } from "/assets/shared/js/dom.js";

export { actionButton, titledCard } from "/assets/shared/js/ui.js";

export function livePill(initial = "Loading...") {
  const label = h("p.eyebrow", null, initial);
  const el = h("div.live-pill", null,
    h("span.live-pill__dot", { "attr:aria-hidden": "true" }),
    label,
  );
  return { el, label };
}
