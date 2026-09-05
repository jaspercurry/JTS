// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// chrome.js — the client-rendered `.app-header`, byte-for-byte the same
// element tree `jasper/web/_common.py`'s `canonical_header()` emits: a round
// back button (shared `#icon-back` sprite symbol), the centred title, and an
// optional `right` slot. A page needing more (a tab strip, a status API)
// appends into the returned element rather than widening this contract.

import { h, svg } from "/assets/shared/js/dom.js";

export function appHeader({ title, backHref = "/", right = null } = {}) {
  return h("header.app-header", null,
    h("div.app-header__row", null,
      h("a.icon-button", { href: backHref, "attr:aria-label": "Home" },
        svg("svg.ico", { "aria-hidden": "true" },
          svg("use", { href: "#icon-back" }))),
      h("h1.app-header__title", null, title),
      right || h("span"),
    ),
  );
}
