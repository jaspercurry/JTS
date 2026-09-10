// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// ui.js — UI primitives shared verbatim by the chat and system-status pages
// (chat/js/components.js and system-status/js/components.js re-export
// these). All build on dom.js so their arguments stay text nodes.

import { h } from "/assets/shared/js/dom.js";

// Status pill. `tone` is one of ok/warn/danger/idle and names the app.css
// modifier that sets --tone.
export function badge(text, tone = "ok") {
  return h(`span.badge.badge--${tone}`, null, text);
}

export function actionButton(label, opts = {}) {
  const { variant = "default", onClick } = opts;
  return h(`button.btn.btn--${variant}`, { type: "button", onclick: onClick }, label);
}

// A titled section: a cased card title above a card body. Returns the section
// plus the (empty) body container, so a poll loop can re-render just the
// body without rebuilding the title. `.section` / `.info-card` live in app.css.
export function titledCard(title, opts = {}) {
  const body = h(`div.info-card${opts.accent ? ".info-card--accent" : ""}`);
  const section = h("section.section", null,
    h("div.section__head", null, h("h2.section__title", null, title)),
    body,
  );
  return { section, body };
}
