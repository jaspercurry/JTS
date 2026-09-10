// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// components.js — small chat-page UI primitives.
//
// These mirror the /system/ module graph and build everything with dom.js so
// untrusted transcript strings remain text nodes.

import { h } from "/assets/shared/js/dom.js";

export { badge, actionButton } from "/assets/shared/js/ui.js";

export function titledCard(title, opts = {}) {
  const body = h(`div.info-card${opts.accent ? ".info-card--accent" : ""}`);
  const section = h("section.section", null,
    h("div.section__head", null, h("h2.section__title", null, title)),
    body,
  );
  return { section, body };
}

export function livePill(initial = "Loading...") {
  const label = h("p.eyebrow", null, initial);
  const el = h("div.live-pill", null,
    h("span.live-pill__dot", { "attr:aria-hidden": "true" }),
    label,
  );
  return { el, label };
}
