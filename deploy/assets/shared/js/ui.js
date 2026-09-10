// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// ui.js — the two smallest UI primitives, shared verbatim by the chat and
// system-status pages (chat/js/components.js and
// system-status/js/components.js re-export these). Both build on dom.js so
// their arguments stay text nodes.

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
